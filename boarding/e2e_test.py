#!/usr/bin/env python3
"""端到端验证：建档→下单→入舍→喂养→训练→异常隔离→缴费→离舍，
外加并发下单/并发入舍、重启保留、无效参数与权限边界。

用法: python3 e2e_test.py   (自行启动/停止服务，使用独立测试库)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

PORT = 3399
BASE = f"http://127.0.0.1:{PORT}"
ADMIN = {"Authorization": "Bearer admin-token"}
OWNER1 = {"Authorization": "Bearer owner1-token"}
OWNER2 = {"Authorization": "Bearer owner2-token"}

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {extra}")


def api(method, path, headers=None, body=None, raw=None):
    data = raw if raw is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8")
        try:
            return e.code, json.loads(payload)
        except ValueError:
            return e.code, {"raw": payload}


def wait_up(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = api("GET", "/api/courses", ADMIN)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def start_server(db_dir):
    env = dict(os.environ, PORT=str(PORT), BOARDING_DB=os.path.join(db_dir, "boarding.db"))
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_up(), "server did not start"
    return proc


def main():
    tmp = tempfile.mkdtemp(prefix="boarding-e2e-")
    proc = start_server(tmp)
    try:
        run_lifecycle()
        run_concurrency()
        run_validation_and_auth()
    finally:
        proc.terminate()
        proc.wait()

    print("\n== 重启后数据保留 ==")
    proc = start_server(tmp)  # 同一个 db 目录，模拟服务重启
    try:
        status, order = api("GET", "/api/orders/1", ADMIN)
        check("重启后寄养单仍可查询", status == 200 and order["status"] == "CHECKED_OUT", order)
        check("重启后账单完整", order["bill"]["totalCents"] > 0 and order["bill"]["balanceCents"] == 0, order.get("bill"))
        status, logs = api("GET", "/api/orders/1/audit", ADMIN)
        actions = [l["action"] for l in logs] if status == 200 else []
        check("重启后审计完整(入舍/状态变化/缴费/离舍)",
              all(a in actions for a in ("CHECK_IN", "STATUS_CHANGE", "PAYMENT", "CHECK_OUT")), actions)
        status, orders = api("GET", "/api/orders", OWNER1)
        check("重启后委托人订单列表可查", status == 200 and len(orders) >= 1, orders)
    finally:
        proc.terminate()
        proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


def run_lifecycle():
    print("== 建档 ==")
    status, pigeon = api("POST", "/api/pigeons", OWNER1,
                         {"ringNo": "CHN-2026-888", "name": "灰将军", "color": "灰", "birthYear": 2025})
    check("委托人建档", status == 201, pigeon)
    pid = pigeon["id"]
    status, dup = api("POST", "/api/pigeons", OWNER1, {"ringNo": "CHN-2026-888", "name": "重复"})
    check("重复足环号被拒绝(409)", status == 409 and dup["error"] == "ring_exists", dup)

    print("== 下单(选寄养期+代训课) ==")
    status, order = api("POST", "/api/orders", OWNER1,
                        {"pigeonId": pid, "planDays": 30, "dailyRateCents": 500,
                         "courseCodes": ["FLY-BASE", "FLY-MID"]})
    check("创建寄养单", status == 201 and order["status"] == "PENDING", order)
    oid = order["id"]
    check("下单写审计", any(l["action"] == "ORDER_CREATED" for l in api("GET", f"/api/orders/{oid}/audit", OWNER1)[1]))

    print("== 入舍 ==")
    status, _ = api("POST", f"/api/orders/{oid}/feedings", ADMIN, {"date": "2026-09-12"})
    check("未入舍不能喂养(409)", status == 409)
    status, order = api("POST", f"/api/orders/{oid}/check-in", ADMIN, {"loftId": 1})
    check("管理员办理入舍", status == 200 and order["status"] == "BOARDING", order)
    status, again = api("POST", f"/api/orders/{oid}/check-in", ADMIN, {"loftId": 1})
    check("重复入舍被拒绝(409)", status == 409, again)

    print("== 每日喂养 ==")
    status, _ = api("POST", f"/api/orders/{oid}/feedings", ADMIN, {"date": "2026-09-12", "note": "玉米+豌豆"})
    check("登记喂养", status == 201)
    status, dup = api("POST", f"/api/orders/{oid}/feedings", ADMIN, {"date": "2026-09-12"})
    check("同日重复喂养被拒绝(409)", status == 409 and dup["error"] == "feeding_exists", dup)

    print("== 训练 ==")
    course_id = order["courses"][0]["courseId"]
    status, _ = api("POST", f"/api/orders/{oid}/trainings", ADMIN,
                    {"courseId": course_id, "result": "家飞40分钟", "note": "状态好"})
    check("登记训练", status == 201)

    print("== 健康异常与隔离 ==")
    status, order = api("POST", f"/api/orders/{oid}/health-events", ADMIN,
                        {"type": "腹泻", "note": "疑似腺病毒", "quarantine": True})
    check("登记异常并转隔离", status == 201 and order["status"] == "QUARANTINE", order)
    status, blocked = api("POST", f"/api/orders/{oid}/trainings", ADMIN, {"courseId": course_id})
    check("隔离期间不能训练(409)", status == 409 and blocked["error"] == "quarantine_no_training", blocked)
    status, order = api("POST", f"/api/orders/{oid}/medical-items", ADMIN,
                        {"name": "腺病毒口服液", "amountCents": 3500})
    check("登记额外医疗项", status == 201 and order["bill"]["medicalCents"] == 3500, order)
    status, order = api("POST", f"/api/orders/{oid}/quarantine/release", ADMIN)
    check("解除隔离恢复在舍", status == 200 and order["status"] == "BOARDING", order)
    status, _ = api("POST", f"/api/orders/{oid}/trainings", ADMIN, {"courseId": course_id, "result": "恢复训练"})
    check("解除隔离后可训练", status == 201)

    print("== 缴费与结算离舍 ==")
    status, before = api("GET", f"/api/orders/{oid}", ADMIN)
    total = before["bill"]["totalCents"]
    check("费用自动汇总(天数+课程+医疗)",
          total == before["bill"]["boardingCents"] + 3000 + 8000 + 3500, before["bill"])
    status, pay1 = api("POST", f"/api/orders/{oid}/payments", OWNER1,
                       {"amountCents": total - 100, "method": "wechat", "idempotencyKey": "pay-1"})
    check("部分缴费", status == 201 and pay1["bill"]["balanceCents"] == 100, pay1)
    status, dup_pay = api("POST", f"/api/orders/{oid}/payments", OWNER1,
                          {"amountCents": total - 100, "method": "wechat", "idempotencyKey": "pay-1"})
    check("重复缴费幂等(不重复入账)", status == 200 and dup_pay["duplicated"] is True
          and dup_pay["bill"]["paidCents"] == total - 100, dup_pay)
    status, blocked = api("POST", f"/api/orders/{oid}/check-out", ADMIN)
    check("未结清不能离舍(409)", status == 409 and blocked["error"] == "unpaid_balance", blocked)
    status, pay2 = api("POST", f"/api/orders/{oid}/payments", OWNER1,
                       {"amountCents": 100, "method": "cash", "idempotencyKey": "pay-2"})
    check("补缴尾款", status == 201 and pay2["bill"]["balanceCents"] == 0, pay2)
    status, order = api("POST", f"/api/orders/{oid}/check-out", ADMIN)
    check("结清后离舍", status == 200 and order["status"] == "CHECKED_OUT", order)
    status, loft = api("GET", "/api/lofts", ADMIN)
    check("离舍后棚位释放", all(l["occupancy"] == 0 for l in loft), loft)
    status, again = api("POST", f"/api/orders/{oid}/check-out", ADMIN)
    check("重复离舍被拒绝(409)", status == 409, again)

    status, logs = api("GET", f"/api/orders/{oid}/audit", ADMIN)
    actions = [l["action"] for l in logs]
    check("审计覆盖入舍/状态变化/缴费/离舍",
          all(a in actions for a in ("CHECK_IN", "STATUS_CHANGE", "PAYMENT", "CHECK_OUT")), actions)
    check("状态变化审计含隔离进出", actions.count("STATUS_CHANGE") >= 2, actions)

    print("== 离舍后可再下单(同一鸽子) ==")
    status, order2 = api("POST", "/api/orders", OWNER1, {"pigeonId": pid, "planDays": 7})
    check("离舍后重新寄养", status == 201 and order2["status"] == "PENDING", order2)
    api("POST", f"/api/orders/{order2['id']}/check-in", ADMIN, {"loftId": 2})


def run_concurrency():
    print("== 并发下单：同一只鸽子不能重复占单 ==")
    status, pigeon = api("POST", "/api/pigeons", OWNER2, {"ringNo": "CHN-2026-999", "name": "雨点王"})
    pid = pigeon["id"]
    results = []

    def place():
        results.append(api("POST", "/api/orders", OWNER2, {"pigeonId": pid, "planDays": 10})[0])

    threads = [threading.Thread(target=place) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("10 并发下单只有 1 个成功", results.count(201) == 1 and results.count(409) == 9, results)

    print("== 并发入舍：棚位容量不超卖 ==")
    order_ids = []
    for i in range(4):
        _, p = api("POST", "/api/pigeons", OWNER2, {"ringNo": f"CHN-2026-10{i}", "name": f"鸽{i}"})
        _, o = api("POST", "/api/orders", OWNER2, {"pigeonId": p["id"], "planDays": 5})
        order_ids.append(o["id"])
    results = []

    def occupy(oid):
        results.append(api("POST", f"/api/orders/{oid}/check-in", ADMIN, {"loftId": 1})[0])

    threads = [threading.Thread(target=occupy, args=(oid,)) for oid in order_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("A棚容量2，4 并发入舍只有 2 个成功", results.count(200) == 2 and results.count(409) == 2, results)
    _, lofts = api("GET", "/api/lofts", ADMIN)
    check("A棚占用=2 未超卖", next(l for l in lofts if l["code"] == "A")["occupancy"] == 2, lofts)


def run_validation_and_auth():
    print("== 无效参数 ==")
    cases = [
        ("寄养天数为负", api("POST", "/api/orders", OWNER1, {"pigeonId": 1, "planDays": -5})),
        ("寄养天数非数字", api("POST", "/api/orders", OWNER1, {"pigeonId": 1, "planDays": "abc"})),
        ("缺字段", api("POST", "/api/orders", OWNER1, {"pigeonId": 1})),
        ("未知课程", api("POST", "/api/orders", OWNER1, {"pigeonId": 1, "planDays": 3, "courseCodes": ["NOPE"]})),
        ("缴费金额为0", api("POST", "/api/orders/1/payments", OWNER1, {"amountCents": 0, "idempotencyKey": "k0"})),
        ("医疗金额为负", api("POST", "/api/orders/1/medical-items", ADMIN, {"name": "x", "amountCents": -1})),
        ("喂养日期格式错", api("POST", "/api/orders/1/feedings", ADMIN, {"date": "09/12"})),
        ("喂养日期九十九月九十九日", api("POST", "/api/orders/2/feedings", ADMIN, {"date": "2026-99-99"})),
        ("喂养日期零年零月零日", api("POST", "/api/orders/2/feedings", ADMIN, {"date": "0000-00-00"})),
        ("喂养日期平年2月29日", api("POST", "/api/orders/2/feedings", ADMIN, {"date": "2026-02-29"})),
        ("喂养日期2月30日", api("POST", "/api/orders/2/feedings", ADMIN, {"date": "2026-02-30"})),
        ("喂养日期13月", api("POST", "/api/orders/2/feedings", ADMIN, {"date": "2026-13-01"})),
        ("非法JSON", api("POST", "/api/orders", OWNER1, raw=b"{not json")),
        ("未知路由", api("GET", "/api/nope", ADMIN)),
        ("不存在的寄养单", api("GET", "/api/orders/99999", ADMIN)),
    ]
    for name, (status, body) in cases:
        check(f"{name} → 4xx", 400 <= status < 500, (status, body))

    # 正常日期(含闰日)仍接受，同日重复仍拦截
    check("喂养正常日期(闰日2028-02-29) → 201",
          api("POST", "/api/orders/2/feedings", ADMIN, {"date": "2028-02-29"})[0] == 201)
    status, dup = api("POST", "/api/orders/2/feedings", ADMIN, {"date": "2028-02-29"})
    check("同日重复喂养仍拦截(409)", status == 409 and dup["error"] == "feeding_exists", dup)

    print("== 权限边界 ==")
    check("无令牌 → 401", api("GET", "/api/orders")[0] == 401)
    check("坏令牌 → 401", api("GET", "/api/orders", {"Authorization": "Bearer wrong"})[0] == 401)
    check("委托人不能入舍(403)", api("POST", "/api/orders/1/check-in", OWNER1, {"loftId": 1})[0] == 403)
    check("委托人不能登记训练(403)", api("POST", "/api/orders/1/trainings", OWNER1, {"courseId": 1})[0] == 403)
    check("委托人不能登记医疗项(403)", api("POST", "/api/orders/1/medical-items", OWNER1, {"name": "x", "amountCents": 1})[0] == 403)
    check("管理员不能替委托人下单(403)", api("POST", "/api/orders", ADMIN, {"pigeonId": 1, "planDays": 3})[0] == 403)
    check("跨委托人读单 → 404", api("GET", "/api/orders/1", OWNER2)[0] == 404)
    check("跨委托人缴费 → 404", api("POST", "/api/orders/1/payments", OWNER2, {"amountCents": 1, "idempotencyKey": "kx"})[0] == 404)
    check("委托人只能看自己的鸽子",
          all(p["ownerId"] != 3 for p in api("GET", "/api/pigeons", OWNER1)[1]))
    check("管理员可看全部鸽子", len(api("GET", "/api/pigeons", ADMIN)[1]) >= 6)


if __name__ == "__main__":
    main()
