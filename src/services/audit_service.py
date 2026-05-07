"""audit_log 에 이벤트 기록 (append-only)."""
from __future__ import annotations

import json
from datetime import datetime

from src.db.connection import get_connection


def log_event(chat_id: int | None, event_type: str, payload: dict) -> int:
    """audit_log 에 한 줄 추가하고 id 반환."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO audit_log (chat_id, event_type, payload_json, ts) VALUES (?, ?, ?, ?)",
            (chat_id, event_type, json.dumps(payload, ensure_ascii=False), now),
        )
        return cur.lastrowid
    finally:
        conn.close()
