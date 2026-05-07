"""SQLite 연결 + 초기 스키마 적용."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.config import DB_PATH

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _run_migrations(conn)
    finally:
        conn.close()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """기존 DB 에 새 컬럼 보강 — idempotent.

    SQLite 는 ALTER TABLE ADD COLUMN IF NOT EXISTS 미지원 → 컬럼 존재 확인 후 추가.
    CHECK 제약은 새 행에만 적용 (기존 행은 default 로 채워짐).
    """
    def _has_column(table: str, column: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)

    # 2026-05-04: 당일매매 토글 — bot_users.holding_mode
    if not _has_column("bot_users", "holding_mode"):
        conn.execute(
            "ALTER TABLE bot_users ADD COLUMN holding_mode TEXT NOT NULL DEFAULT 'swing_week'"
        )

    # 2026-05-04: 조기 익절 토글 — bot_users.early_take_profit
    # ON 시 스윙 포지션이라도 day-TP (+3% bunt / +5% squeeze) 도달하면 즉시 매도
    if not _has_column("bot_users", "early_take_profit"):
        conn.execute(
            "ALTER TABLE bot_users ADD COLUMN early_take_profit INTEGER NOT NULL DEFAULT 0"
        )


if __name__ == "__main__":
    init_schema()
    print(f"Schema initialized at {DB_PATH}")
