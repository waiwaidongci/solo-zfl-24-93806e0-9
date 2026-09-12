"""赛鸽寄养与代训管理服务 — SQLite 存储层。

只用标准库。所有多步写入都在 BEGIN IMMEDIATE 事务里完成，
任何一步失败整体回滚，不会留下半条数据。
"""
import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BOARDING_DB", os.path.join(BASE_DIR, "..", "data", "boarding.db"))

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  username   TEXT NOT NULL UNIQUE,
  role       TEXT NOT NULL CHECK (role IN ('owner', 'admin')),
  token      TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS pigeons (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ring_no    TEXT NOT NULL UNIQUE,
  owner_id   INTEGER NOT NULL REFERENCES users(id),
  name       TEXT NOT NULL,
  color      TEXT NOT NULL DEFAULT '',
  birth_year INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  code        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
  sessions    INTEGER NOT NULL CHECK (sessions > 0)
);

CREATE TABLE IF NOT EXISTS lofts (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  code     TEXT NOT NULL UNIQUE,
  capacity INTEGER NOT NULL CHECK (capacity > 0)
);

CREATE TABLE IF NOT EXISTS boarding_orders (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no          TEXT NOT NULL UNIQUE,
  pigeon_id         INTEGER NOT NULL REFERENCES pigeons(id),
  owner_id          INTEGER NOT NULL REFERENCES users(id),
  plan_days         INTEGER NOT NULL CHECK (plan_days > 0),
  daily_rate_cents  INTEGER NOT NULL CHECK (daily_rate_cents >= 0),
  status            TEXT NOT NULL CHECK (status IN
                      ('PENDING', 'BOARDING', 'QUARANTINE', 'CHECKED_OUT', 'CANCELLED')),
  loft_id           INTEGER REFERENCES lofts(id),
  check_in_at       TEXT,
  check_out_at      TEXT,
  created_at        TEXT NOT NULL
);

-- 同一只鸽子同一时间只能有一个进行中的寄养单（数据库级强约束，并发安全）
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_order_per_pigeon
  ON boarding_orders(pigeon_id)
  WHERE status IN ('PENDING', 'BOARDING', 'QUARANTINE');

CREATE TABLE IF NOT EXISTS order_courses (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    INTEGER NOT NULL REFERENCES boarding_orders(id),
  course_id   INTEGER NOT NULL REFERENCES courses(id),
  price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
  UNIQUE (order_id, course_id)
);

CREATE TABLE IF NOT EXISTS feedings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id   INTEGER NOT NULL REFERENCES boarding_orders(id),
  feed_date  TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (order_id, feed_date)
);

CREATE TABLE IF NOT EXISTS trainings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id   INTEGER NOT NULL REFERENCES boarding_orders(id),
  course_id  INTEGER NOT NULL REFERENCES courses(id),
  result     TEXT NOT NULL DEFAULT '',
  note       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id   INTEGER NOT NULL REFERENCES boarding_orders(id),
  event_type TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS medical_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id     INTEGER NOT NULL REFERENCES boarding_orders(id),
  name         TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id        INTEGER NOT NULL REFERENCES boarding_orders(id),
  amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
  method          TEXT NOT NULL DEFAULT 'cash',
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    INTEGER REFERENCES boarding_orders(id),
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  from_status TEXT,
  to_status   TEXT,
  detail      TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
"""

SEED = """
INSERT OR IGNORE INTO users (username, role, token) VALUES
  ('admin',  'admin', 'admin-token'),
  ('owner1', 'owner', 'owner1-token'),
  ('owner2', 'owner', 'owner2-token');

INSERT OR IGNORE INTO courses (code, name, price_cents, sessions) VALUES
  ('FLY-BASE', '基础家飞课',   3000, 10),
  ('FLY-MID',  '中路训放课',   8000,  6),
  ('FLY-LONG', '远程强化课',  15000,  4);

INSERT OR IGNORE INTO lofts (code, capacity) VALUES
  ('A', 2),
  ('B', 2);
"""


def connect(db_path=DB_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path=DB_PATH):
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executescript(SEED)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def tx(conn):
    """写事务：先拿写锁再动手，任何异常整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
