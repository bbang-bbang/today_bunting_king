"""코인 봇 전용 DB — coin-bunting.db (KR 봇과 완전 분리).

KR 봇의 src/db/connection.py 패턴 단순화 + 코인 특화 컬럼.
주요 차이:
  - 시장: 'COIN' 단일 (KOSPI/KOSDAQ 안 씀)
  - 가격: REAL (KRW 정수가 아닌 실수, BTC/ETH 가격 큼)
  - market 컬럼 추가: 'KRW-BTC', 'KRW-ETH' 등
  - 시간: 24/7 — 거래일 컬럼 없음
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# 환경변수 또는 기본값. KR 봇의 DB_PATH 와 분리.
COIN_DB_PATH = Path(os.environ.get("COIN_DB_PATH", "./data/coin-bunting.db"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS coin_audit_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id       INTEGER,
  event_type    TEXT NOT NULL,
  payload_json  TEXT,
  ts            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_coin_audit_event ON coin_audit_log(event_type, ts);

CREATE TABLE IF NOT EXISTS coin_orders (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  audit_id        INTEGER,
  market          TEXT NOT NULL,         -- 'KRW-BTC'
  side            TEXT NOT NULL CHECK(side IN ('buy','sell')),
  trade_mode      TEXT NOT NULL CHECK(trade_mode IN ('paper','live')),
  quantity        REAL NOT NULL,
  price           REAL NOT NULL,
  status          TEXT NOT NULL CHECK(status IN ('pending','filled','partial','failed','cancelled')),
  filled_quantity REAL DEFAULT 0,
  filled_avg_price REAL DEFAULT 0,
  commission      REAL DEFAULT 0,
  slippage        REAL DEFAULT 0,
  upbit_order_id  TEXT,
  reason          TEXT,                  -- 시그널 점수 / 청산 사유
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_coin_orders_status ON coin_orders(status, side);

CREATE TABLE IF NOT EXISTS coin_positions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  market        TEXT NOT NULL,
  buy_order_id  INTEGER NOT NULL,
  sell_order_id INTEGER,
  buy_price     REAL NOT NULL,
  quantity      REAL NOT NULL,
  target_price  REAL NOT NULL,        -- TP
  stop_price    REAL NOT NULL,        -- SL
  status        TEXT NOT NULL CHECK(status IN ('open','closed')),
  pnl           REAL,                 -- 청산 시 net (수수료 차감)
  opened_at     TEXT NOT NULL DEFAULT (datetime('now')),
  closed_at     TEXT,
  FOREIGN KEY (buy_order_id) REFERENCES coin_orders(id),
  FOREIGN KEY (sell_order_id) REFERENCES coin_orders(id)
);
CREATE INDEX IF NOT EXISTS idx_coin_positions_open ON coin_positions(status, market);

CREATE TABLE IF NOT EXISTS coin_account (
  -- 단일 row — chat_id 별이 아니라 봇 전체 계좌 (1 사용자 가정)
  id            INTEGER PRIMARY KEY CHECK(id = 1),
  cash_krw      REAL NOT NULL,        -- 가용 KRW (paper 면 가상 시드)
  trade_mode    TEXT NOT NULL DEFAULT 'paper' CHECK(trade_mode IN ('paper','live')),
  paused        INTEGER NOT NULL DEFAULT 0,   -- /coin_pause 로 토글
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_coin_connection() -> sqlite3.Connection:
    COIN_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(COIN_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_coin_schema() -> None:
    conn = get_coin_connection()
    try:
        conn.executescript(SCHEMA)
        # 단일 계좌 row 보장 (없으면 INSERT)
        cur = conn.execute("SELECT COUNT(*) FROM coin_account").fetchone()
        if (cur[0] if cur else 0) == 0:
            seed = float(os.environ.get("COIN_SEED_KRW", "300000"))
            mode = os.environ.get("COIN_TRADE_MODE", "paper")
            conn.execute(
                "INSERT INTO coin_account (id, cash_krw, trade_mode) VALUES (1, ?, ?)",
                (seed, mode),
            )
    finally:
        conn.close()


def coin_log_event(chat_id: int | None, event_type: str, payload: dict | None = None) -> int:
    import json
    conn = get_coin_connection()
    try:
        cur = conn.execute(
            "INSERT INTO coin_audit_log (chat_id, event_type, payload_json) VALUES (?, ?, ?)",
            (chat_id, event_type, json.dumps(payload or {}, ensure_ascii=False)),
        )
        return cur.lastrowid
    finally:
        conn.close()


if __name__ == "__main__":
    init_coin_schema()
    print(f"Coin schema initialized at {COIN_DB_PATH}")
