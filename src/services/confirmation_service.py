"""pending_confirmations — 매수/매도 inline 버튼의 UUID + TTL 관리.

주간스윙 모드에서는 사용자가 5개 추천을 비교·검토할 시간이 필요하므로
TTL 을 10분으로 설정. 만료 시 /추천 재입력으로 캐시에서 즉시 새 UUID 재발급 가능.
"""
from __future__ import annotations

import json
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone

from src.db.connection import get_connection

TTL_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat()


def create(chat_id: int, intent: dict) -> str:
    """UUID 발급 + DB 저장. 10분 후 만료."""
    u = str(uuid_lib.uuid4())
    expires = _now() + timedelta(seconds=TTL_SECONDS)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO pending_confirmations (uuid, chat_id, intent_json, expires_at) VALUES (?, ?, ?, ?)",
            (u, chat_id, json.dumps(intent, ensure_ascii=False), _iso(expires)),
        )
    finally:
        conn.close()
    return u


def consume(uuid_str: str, chat_id: int) -> dict | None:
    """UUID 검증 + 만료·consumed 체크 + atomic consume. intent dict 반환 (or None)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT chat_id, intent_json, expires_at, consumed FROM pending_confirmations WHERE uuid = ?",
            (uuid_str,),
        ).fetchone()
        if not row:
            return None
        if row["chat_id"] != chat_id:
            return None
        if row["consumed"]:
            return None
        if row["expires_at"] < _iso(_now()):
            return None
        # 원자적 consume
        res = conn.execute(
            "UPDATE pending_confirmations SET consumed=1 WHERE uuid=? AND consumed=0",
            (uuid_str,),
        )
        if res.rowcount == 0:
            return None
        return json.loads(row["intent_json"])
    finally:
        conn.close()


def get_status(uuid_str: str, chat_id: int) -> dict:
    """UUID 현재 상태 조회 — consume 실패 원인 파악용.

    반환:
      {
        "exists": bool,          # UUID 가 DB 에 있는지
        "chat_match": bool,      # chat_id 매칭 여부
        "consumed": bool,        # 이미 소모됐는지
        "expired": bool,         # 만료됐는지
        "intent": dict | None,   # UUID 매칭되면 원래 의도 반환
      }
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT chat_id, intent_json, expires_at, consumed FROM pending_confirmations WHERE uuid = ?",
            (uuid_str,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {
            "exists": False, "chat_match": False,
            "consumed": False, "expired": False, "intent": None,
        }
    return {
        "exists": True,
        "chat_match": row["chat_id"] == chat_id,
        "consumed": bool(row["consumed"]),
        "expired": row["expires_at"] < _iso(_now()),
        "intent": json.loads(row["intent_json"]),
    }


def cleanup_expired() -> int:
    """만료된 레코드 정리. 삭제된 행 수 반환."""
    conn = get_connection()
    try:
        res = conn.execute(
            "DELETE FROM pending_confirmations WHERE expires_at < ?",
            (_iso(_now()),),
        )
        return res.rowcount
    finally:
        conn.close()
