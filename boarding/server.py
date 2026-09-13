#!/usr/bin/env python3
"""赛鸽寄养与代训管理服务 — HTTP 接口层（仅标准库）。

角色：owner（委托人）/ admin（管理员），Bearer token 鉴权。
规则：
  - 同一只鸽子不能同时挂在两个进行中的寄养单（数据库唯一索引兜底）
  - 隔离观察（QUARANTINE）期间不能训练
  - 费用 = 寄养天数×日价 + 代训课 + 额外医疗项；未结清不能离舍
  - 入舍 / 状态变化 / 缴费 / 离舍全部写审计（同事务）
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from db import DB_PATH, connect, init_db, tx

PORT = int(__import__("os").environ.get("PORT", "3025"))

ACTIVE_STATUSES = ("PENDING", "BOARDING", "QUARANTINE")


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def billed_days(check_in_at, until=None):
    start = datetime.fromisoformat(check_in_at)
    end = datetime.fromisoformat(until) if until else datetime.now(timezone.utc)
    seconds = max(0, (end - start).total_seconds())
    return max(1, -(-int(seconds) // 86400))  # 向上取整天数，至少 1 天


# ---------- 查询与计费 ----------

def get_order(conn, order_id):
    row = conn.execute("SELECT * FROM boarding_orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        raise ApiError(404, "order_not_found", "寄养单不存在")
    return row


def get_bill(conn, order):
    boarding = course = medical = 0
    days = 0
    if order["check_in_at"]:
        days = billed_days(order["check_in_at"], order["check_out_at"])
        boarding = days * order["daily_rate_cents"]
    course = conn.execute(
        "SELECT COALESCE(SUM(price_cents), 0) AS s FROM order_courses WHERE order_id = ?",
        (order["id"],)).fetchone()["s"]
    medical = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM medical_items WHERE order_id = ?",
        (order["id"],)).fetchone()["s"]
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM payments WHERE order_id = ?",
        (order["id"],)).fetchone()["s"]
    total = boarding + course + medical
    return {
        "days": days,
        "boardingCents": boarding,
        "courseCents": course,
        "medicalCents": medical,
        "totalCents": total,
        "paidCents": paid,
        "balanceCents": total - paid,
    }


def order_view(conn, order):
    courses = [dict(r) for r in conn.execute(
        """SELECT oc.course_id AS courseId, c.code, c.name, oc.price_cents AS priceCents
           FROM order_courses oc JOIN courses c ON c.id = oc.course_id
           WHERE oc.order_id = ?""", (order["id"],)).fetchall()]
    view = {
        "id": order["id"],
        "orderNo": order["order_no"],
        "pigeonId": order["pigeon_id"],
        "ownerId": order["owner_id"],
        "planDays": order["plan_days"],
        "dailyRateCents": order["daily_rate_cents"],
        "status": order["status"],
        "loftId": order["loft_id"],
        "checkInAt": order["check_in_at"],
        "checkOutAt": order["check_out_at"],
        "createdAt": order["created_at"],
        "courses": courses,
        "bill": get_bill(conn, order),
    }
    return view


def audit(conn, order_id, actor, action, detail="", from_status=None, to_status=None):
    conn.execute(
        """INSERT INTO audit_logs (order_id, actor, action, from_status, to_status, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (order_id, actor, action, from_status, to_status, detail, now_iso()))


def loft_occupancy(conn, loft_id):
    return conn.execute(
        """SELECT COUNT(*) AS n FROM boarding_orders
           WHERE loft_id = ? AND status IN ('BOARDING', 'QUARANTINE')""",
        (loft_id,)).fetchone()["n"]


# ---------- 入参校验 ----------

def require_fields(data, fields):
    for f in fields:
        if f not in data or data[f] is None:
            raise ApiError(400, "missing_field", f"缺少字段: {f}")


def as_positive_int(value, name, maximum=10**9):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
        raise ApiError(400, "invalid_param", f"{name} 必须是 1..{maximum} 的整数")
    return value


def as_non_empty_str(value, name, max_len=200):
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ApiError(400, "invalid_param", f"{name} 必须是非空字符串(≤{max_len}字)")
    return value.strip()


# ---------- 业务处理 ----------

def create_pigeon(conn, user, data):
    require_fields(data, ["ringNo", "name"])
    ring_no = as_non_empty_str(data["ringNo"], "ringNo", 64)
    name = as_non_empty_str(data["name"], "name", 64)
    color = str(data.get("color", ""))[:32]
    birth_year = data.get("birthYear")
    if birth_year is not None:
        birth_year = as_positive_int(birth_year, "birthYear", 2100)
    try:
        with tx(conn):
            cur = conn.execute(
                """INSERT INTO pigeons (ring_no, owner_id, name, color, birth_year, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ring_no, user["id"], name, color, birth_year, now_iso()))
            pid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise ApiError(409, "ring_exists", "足环号已登记")
    return 201, {"id": pid, "ringNo": ring_no, "name": name,
                 "color": color, "birthYear": birth_year, "ownerId": user["id"]}


def list_pigeons(conn, user):
    if user["role"] == "admin":
        rows = conn.execute("SELECT * FROM pigeons ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM pigeons WHERE owner_id = ? ORDER BY id",
                            (user["id"],)).fetchall()
    return 200, [{"id": r["id"], "ringNo": r["ring_no"], "name": r["name"],
                  "color": r["color"], "birthYear": r["birth_year"],
                  "ownerId": r["owner_id"]} for r in rows]


def create_order(conn, user, data):
    require_fields(data, ["pigeonId", "planDays"])
    pigeon_id = as_positive_int(data["pigeonId"], "pigeonId")
    plan_days = as_positive_int(data["planDays"], "planDays", 365)
    daily_rate = data.get("dailyRateCents", 500)
    if isinstance(daily_rate, bool) or not isinstance(daily_rate, int) or daily_rate < 0:
        raise ApiError(400, "invalid_param", "dailyRateCents 必须是非负整数")
    course_codes = data.get("courseCodes", [])
    if not isinstance(course_codes, list) or len(course_codes) != len(set(course_codes)):
        raise ApiError(400, "invalid_param", "courseCodes 必须是不重复的数组")

    pigeon = conn.execute("SELECT * FROM pigeons WHERE id = ?", (pigeon_id,)).fetchone()
    if not pigeon or pigeon["owner_id"] != user["id"]:
        raise ApiError(404, "pigeon_not_found", "鸽子不存在或不属于当前委托人")
    courses = []
    for code in course_codes:
        c = conn.execute("SELECT * FROM courses WHERE code = ?", (as_non_empty_str(code, "courseCode"),)).fetchone()
        if not c:
            raise ApiError(400, "invalid_param", f"未知代训课: {code}")
        courses.append(c)

    order_no = f"BO-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{pigeon_id}-{int(datetime.now(timezone.utc).timestamp() * 1000) % 10**9}"
    try:
        with tx(conn):
            cur = conn.execute(
                """INSERT INTO boarding_orders
                     (order_no, pigeon_id, owner_id, plan_days, daily_rate_cents, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'PENDING', ?)""",
                (order_no, pigeon_id, user["id"], plan_days, daily_rate, now_iso()))
            oid = cur.lastrowid
            for c in courses:
                conn.execute(
                    "INSERT INTO order_courses (order_id, course_id, price_cents) VALUES (?, ?, ?)",
                    (oid, c["id"], c["price_cents"]))
            audit(conn, oid, user["username"], "ORDER_CREATED",
                  f"寄养{plan_days}天, 课程{len(courses)}门", None, "PENDING")
    except sqlite3.IntegrityError as e:
        if "uq_active_order_per_pigeon" in str(e) or "boarding_orders" in str(e):
            raise ApiError(409, "active_order_exists", "该鸽子已有进行中的寄养单")
        raise
    order = conn.execute("SELECT * FROM boarding_orders WHERE id = ?", (oid,)).fetchone()
    return 201, order_view(conn, order)


def list_orders(conn, user, query):
    sql = "SELECT * FROM boarding_orders"
    args = []
    if user["role"] != "admin":
        sql += " WHERE owner_id = ?"
        args.append(user["id"])
    if query.get("status"):
        sql += (" AND " if args else " WHERE ") + "status = ?"
        args.append(query["status"][0])
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    return 200, [order_view(conn, r) for r in rows]


def read_order(conn, user, order_id):
    order = get_order(conn, order_id)
    if user["role"] != "admin" and order["owner_id"] != user["id"]:
        raise ApiError(404, "order_not_found", "寄养单不存在")
    return 200, order_view(conn, order)


def check_in(conn, user, order_id, data):
    require_fields(data, ["loftId"])
    loft_id = as_positive_int(data["loftId"], "loftId")
    with tx(conn):
        order = get_order(conn, order_id)
        if order["status"] != "PENDING":
            raise ApiError(409, "invalid_status", f"当前状态 {order['status']} 不能入舍")
        loft = conn.execute("SELECT * FROM lofts WHERE id = ?", (loft_id,)).fetchone()
        if not loft:
            raise ApiError(404, "loft_not_found", "棚位不存在")
        if loft_occupancy(conn, loft_id) >= loft["capacity"]:
            raise ApiError(409, "loft_full", "棚位已满")
        conn.execute(
            "UPDATE boarding_orders SET status = 'BOARDING', loft_id = ?, check_in_at = ? WHERE id = ?",
            (loft_id, now_iso(), order_id))
        audit(conn, order_id, user["username"], "CHECK_IN",
              f"入住{loft['code']}棚", "PENDING", "BOARDING")
    return 200, order_view(conn, get_order(conn, order_id))


def add_feeding(conn, user, order_id, data):
    require_fields(data, ["date"])
    feed_date = as_non_empty_str(data["date"], "date", 10)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", feed_date):
        raise ApiError(400, "invalid_param", "date 格式应为 YYYY-MM-DD")
    try:
        datetime.strptime(feed_date, "%Y-%m-%d")  # 必须是真实日历日期
    except ValueError:
        raise ApiError(400, "invalid_date", f"不存在的日历日期: {feed_date}")
    note = str(data.get("note", ""))[:200]
    try:
        with tx(conn):
            order = get_order(conn, order_id)
            if order["status"] not in ("BOARDING", "QUARANTINE"):
                raise ApiError(409, "invalid_status", "鸽子不在舍，不能登记喂养")
            conn.execute(
                "INSERT INTO feedings (order_id, feed_date, note, created_at) VALUES (?, ?, ?, ?)",
                (order_id, feed_date, note, now_iso()))
    except sqlite3.IntegrityError:
        raise ApiError(409, "feeding_exists", "当天喂养已登记")
    return 201, {"orderId": order_id, "date": feed_date, "note": note}


def add_training(conn, user, order_id, data):
    require_fields(data, ["courseId"])
    course_id = as_positive_int(data["courseId"], "courseId")
    result = str(data.get("result", ""))[:200]
    note = str(data.get("note", ""))[:200]
    with tx(conn):
        order = get_order(conn, order_id)
        if order["status"] == "QUARANTINE":
            raise ApiError(409, "quarantine_no_training", "隔离观察期间不能训练")
        if order["status"] != "BOARDING":
            raise ApiError(409, "invalid_status", f"当前状态 {order['status']} 不能训练")
        enrolled = conn.execute(
            "SELECT 1 FROM order_courses WHERE order_id = ? AND course_id = ?",
            (order_id, course_id)).fetchone()
        if not enrolled:
            raise ApiError(400, "invalid_param", "该课程不在本寄养单内")
        conn.execute(
            "INSERT INTO trainings (order_id, course_id, result, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (order_id, course_id, result, note, now_iso()))
    return 201, {"orderId": order_id, "courseId": course_id, "result": result, "note": note}


def add_health_event(conn, user, order_id, data):
    require_fields(data, ["type"])
    event_type = as_non_empty_str(data["type"], "type", 64)
    note = str(data.get("note", ""))[:200]
    quarantine = bool(data.get("quarantine", False))
    with tx(conn):
        order = get_order(conn, order_id)
        if order["status"] not in ("BOARDING", "QUARANTINE"):
            raise ApiError(409, "invalid_status", "鸽子不在舍，不能登记健康异常")
        conn.execute(
            "INSERT INTO health_events (order_id, event_type, note, created_at) VALUES (?, ?, ?, ?)",
            (order_id, event_type, note, now_iso()))
        audit(conn, order_id, user["username"], "HEALTH_EVENT", f"{event_type}: {note}")
        if quarantine and order["status"] == "BOARDING":
            conn.execute("UPDATE boarding_orders SET status = 'QUARANTINE' WHERE id = ?", (order_id,))
            audit(conn, order_id, user["username"], "STATUS_CHANGE",
                  "转入隔离观察", "BOARDING", "QUARANTINE")
    return 201, order_view(conn, get_order(conn, order_id))


def release_quarantine(conn, user, order_id):
    with tx(conn):
        order = get_order(conn, order_id)
        if order["status"] != "QUARANTINE":
            raise ApiError(409, "invalid_status", "该寄养单不在隔离观察中")
        conn.execute("UPDATE boarding_orders SET status = 'BOARDING' WHERE id = ?", (order_id,))
        audit(conn, order_id, user["username"], "STATUS_CHANGE",
              "解除隔离，恢复在舍", "QUARANTINE", "BOARDING")
    return 200, order_view(conn, get_order(conn, order_id))


def add_medical_item(conn, user, order_id, data):
    require_fields(data, ["name", "amountCents"])
    name = as_non_empty_str(data["name"], "name", 64)
    amount = as_positive_int(data["amountCents"], "amountCents")
    with tx(conn):
        order = get_order(conn, order_id)
        if order["status"] not in ("BOARDING", "QUARANTINE"):
            raise ApiError(409, "invalid_status", "鸽子不在舍，不能登记医疗项")
        conn.execute(
            "INSERT INTO medical_items (order_id, name, amount_cents, created_at) VALUES (?, ?, ?, ?)",
            (order_id, name, amount, now_iso()))
        audit(conn, order_id, user["username"], "MEDICAL_ITEM", f"{name} {amount}分")
    return 201, order_view(conn, get_order(conn, order_id))


def add_payment(conn, user, order_id, data):
    require_fields(data, ["amountCents", "idempotencyKey"])
    amount = as_positive_int(data["amountCents"], "amountCents")
    method = str(data.get("method", "cash"))[:32]
    key = as_non_empty_str(data["idempotencyKey"], "idempotencyKey", 128)
    with tx(conn):
        order = get_order(conn, order_id)
        if order["owner_id"] != user["id"]:
            raise ApiError(404, "order_not_found", "寄养单不存在")
        if order["status"] in ("CHECKED_OUT", "CANCELLED"):
            raise ApiError(409, "invalid_status", "寄养单已关闭，不能缴费")
        existing = conn.execute(
            "SELECT * FROM payments WHERE idempotency_key = ?", (key,)).fetchone()
        if existing:
            if existing["order_id"] != order_id:
                raise ApiError(409, "idempotency_conflict", "幂等键已被其他单据使用")
            conn.rollback()
            return 200, {"paymentId": existing["id"], "duplicated": True,
                         "bill": get_bill(conn, order)}
        cur = conn.execute(
            """INSERT INTO payments (order_id, amount_cents, method, idempotency_key, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (order_id, amount, method, key, now_iso()))
        audit(conn, order_id, user["username"], "PAYMENT", f"{method} {amount}分")
        payment_id = cur.lastrowid
    return 201, {"paymentId": payment_id, "duplicated": False,
                 "bill": get_bill(conn, get_order(conn, order_id))}


def check_out(conn, user, order_id):
    with tx(conn):
        order = get_order(conn, order_id)
        if order["status"] not in ("BOARDING", "QUARANTINE"):
            raise ApiError(409, "invalid_status", f"当前状态 {order['status']} 不能离舍")
        bill = get_bill(conn, order)
        if bill["balanceCents"] > 0:
            raise ApiError(409, "unpaid_balance",
                           f"未结清，还差 {bill['balanceCents']} 分，不能离舍")
        conn.execute(
            "UPDATE boarding_orders SET status = 'CHECKED_OUT', check_out_at = ? WHERE id = ?",
            (now_iso(), order_id))
        audit(conn, order_id, user["username"], "CHECK_OUT",
              f"结算 {bill['totalCents']} 分(寄养{bill['boardingCents']}+课程{bill['courseCents']}"
              f"+医疗{bill['medicalCents']})", order["status"], "CHECKED_OUT")
    return 200, order_view(conn, get_order(conn, order_id))


def list_audit(conn, user, order_id):
    order = get_order(conn, order_id)
    if user["role"] != "admin" and order["owner_id"] != user["id"]:
        raise ApiError(404, "order_not_found", "寄养单不存在")
    rows = conn.execute(
        "SELECT * FROM audit_logs WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()
    return 200, [{"id": r["id"], "actor": r["actor"], "action": r["action"],
                  "fromStatus": r["from_status"], "toStatus": r["to_status"],
                  "detail": r["detail"], "createdAt": r["created_at"]} for r in rows]


def list_courses(conn, user):
    rows = conn.execute("SELECT * FROM courses ORDER BY id").fetchall()
    return 200, [{"id": r["id"], "code": r["code"], "name": r["name"],
                  "priceCents": r["price_cents"], "sessions": r["sessions"]} for r in rows]


def list_lofts(conn, user):
    rows = conn.execute("SELECT * FROM lofts ORDER BY id").fetchall()
    return 200, [{"id": r["id"], "code": r["code"], "capacity": r["capacity"],
                  "occupancy": loft_occupancy(conn, r["id"])} for r in rows]


# ---------- 路由 ----------

ROUTES = [
    ("POST",   r"/api/pigeons",                              "owner", lambda c, u, m, q, d: create_pigeon(c, u, d)),
    ("GET",    r"/api/pigeons",                              "any",   lambda c, u, m, q, d: list_pigeons(c, u)),
    ("POST",   r"/api/orders",                               "owner", lambda c, u, m, q, d: create_order(c, u, d)),
    ("GET",    r"/api/orders",                               "any",   lambda c, u, m, q, d: list_orders(c, u, q)),
    ("GET",    r"/api/orders/(\d+)",                         "any",   lambda c, u, m, q, d: read_order(c, u, int(m[1]))),
    ("POST",   r"/api/orders/(\d+)/check-in",                "admin", lambda c, u, m, q, d: check_in(c, u, int(m[1]), d)),
    ("POST",   r"/api/orders/(\d+)/feedings",                "admin", lambda c, u, m, q, d: add_feeding(c, u, int(m[1]), d)),
    ("POST",   r"/api/orders/(\d+)/trainings",               "admin", lambda c, u, m, q, d: add_training(c, u, int(m[1]), d)),
    ("POST",   r"/api/orders/(\d+)/health-events",           "admin", lambda c, u, m, q, d: add_health_event(c, u, int(m[1]), d)),
    ("POST",   r"/api/orders/(\d+)/quarantine/release",      "admin", lambda c, u, m, q, d: release_quarantine(c, u, int(m[1]))),
    ("POST",   r"/api/orders/(\d+)/medical-items",           "admin", lambda c, u, m, q, d: add_medical_item(c, u, int(m[1]), d)),
    ("POST",   r"/api/orders/(\d+)/payments",                "owner", lambda c, u, m, q, d: add_payment(c, u, int(m[1]), d)),
    ("POST",   r"/api/orders/(\d+)/check-out",               "admin", lambda c, u, m, q, d: check_out(c, u, int(m[1]))),
    ("GET",    r"/api/orders/(\d+)/audit",                   "any",   lambda c, u, m, q, d: list_audit(c, u, int(m[1]))),
    ("GET",    r"/api/courses",                              "any",   lambda c, u, m, q, d: list_courses(c, u)),
    ("GET",    r"/api/lofts",                                "any",   lambda c, u, m, q, d: list_lofts(c, u)),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else None

        conn = connect(DB_PATH)
        try:
            user = conn.execute("SELECT * FROM users WHERE token = ?", (token or "",)).fetchone()
            if not user:
                raise ApiError(401, "unauthorized", "缺少或无效的访问令牌")

            for m_method, pattern, role, fn in ROUTES:
                if m_method != method:
                    continue
                match = re.fullmatch(pattern, path)
                if not match:
                    continue
                if role != "any" and user["role"] != role:
                    raise ApiError(403, "forbidden", "当前角色无权执行此操作")
                data = {}
                if method == "POST":
                    raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                    if raw:
                        try:
                            data = json.loads(raw.decode("utf-8"))
                        except (ValueError, UnicodeDecodeError):
                            raise ApiError(400, "invalid_json", "请求体不是合法 JSON")
                        if not isinstance(data, dict):
                            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
                status, payload = fn(conn, user, match, query, data)
                return self._send(status, payload)
            raise ApiError(404, "not_found", "接口不存在")
        except ApiError as e:
            self._send(e.status, {"error": e.code, "message": e.message})
        except Exception as e:  # 未知异常也不留半条数据：事务层已回滚
            self._send(500, {"error": "internal_error", "message": str(e)})
        finally:
            conn.close()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def main():
    init_db(DB_PATH)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"赛鸽寄养与代训管理服务 listening on http://localhost:{PORT} (db: {DB_PATH})")
    server.serve_forever()


if __name__ == "__main__":
    main()
