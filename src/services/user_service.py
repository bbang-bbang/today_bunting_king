"""bot_users 관리 — 등록·모드 변경·PIN."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import bcrypt

from src.db.connection import get_connection
from src.services.audit_service import log_event


@dataclass
class BotUser:
    chat_id: int
    status: str
    pin_hash: str | None
    trade_mode: str
    strategy_mode: str
    holding_mode: str
    early_take_profit: bool
    registered_at: str
    approved_at: str | None

    def __post_init__(self):
        # SQLite INTEGER → Python bool 강제 변환
        self.early_take_profit = bool(self.early_take_profit)


def get_user(chat_id: int) -> BotUser | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT chat_id, status, pin_hash, trade_mode, strategy_mode, holding_mode, "
            "early_take_profit, registered_at, approved_at "
            "FROM bot_users WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return BotUser(*row) if row else None
    finally:
        conn.close()


def register_user(chat_id: int, trade_mode: str = "paper") -> BotUser:
    """초대코드 검증은 호출자가 수행. 여기선 INSERT·status='approved' 처리."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO bot_users (chat_id, status, trade_mode, strategy_mode, holding_mode,
                                   early_take_profit, registered_at, approved_at)
            VALUES (?, 'approved', ?, 'bunt', 'swing_week', 0, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
              status='approved',
              trade_mode=excluded.trade_mode,
              approved_at=excluded.approved_at
            """,
            (chat_id, trade_mode, now, now),
        )
    finally:
        conn.close()
    log_event(chat_id, "user_register", {"trade_mode": trade_mode})
    user = get_user(chat_id)
    assert user is not None
    return user


def update_strategy_mode(chat_id: int, mode: str) -> bool:
    if mode not in ("bunt", "squeeze"):
        return False
    conn = get_connection()
    try:
        res = conn.execute(
            "UPDATE bot_users SET strategy_mode=? WHERE chat_id=? AND status='approved'",
            (mode, chat_id),
        )
        changed = res.rowcount > 0
    finally:
        conn.close()
    if changed:
        log_event(chat_id, "mode_change", {"strategy_mode": mode})
    return changed


def update_holding_mode(chat_id: int, mode: str) -> bool:
    """holding_mode = 'day' (당일매매) | 'swing_week' (주간 스윙).

    설정 변경 후 신규 매수부터 적용. 기존 포지션은 매수 시점 모드 유지
    (positions.target_price/stop_price 가 매수 시점에 박혀있어서 영향 없음).
    """
    if mode not in ("day", "swing_week"):
        return False
    conn = get_connection()
    try:
        res = conn.execute(
            "UPDATE bot_users SET holding_mode=? WHERE chat_id=? AND status='approved'",
            (mode, chat_id),
        )
        changed = res.rowcount > 0
    finally:
        conn.close()
    if changed:
        log_event(chat_id, "holding_mode_change", {"holding_mode": mode})
    return changed


def update_early_take_profit(chat_id: int, enabled: bool) -> bool:
    """early_take_profit = True 면 스윙 포지션이라도 day-TP 도달 시 즉시 매도.

    포지션 자체에 박힌 target_price 는 그대로 둠 (자동매도 시 가격 정직 표시 위함).
    price_monitor 가 ref_high >= day_tp_threshold 체크해서 TP_HIT 우선 발사.
    """
    conn = get_connection()
    try:
        res = conn.execute(
            "UPDATE bot_users SET early_take_profit=? WHERE chat_id=? AND status='approved'",
            (1 if enabled else 0, chat_id),
        )
        changed = res.rowcount > 0
    finally:
        conn.close()
    if changed:
        log_event(chat_id, "early_take_profit_change", {"enabled": bool(enabled)})
    return changed


def set_pin(chat_id: int, pin: str) -> bool:
    if len(pin) != 6 or not pin.isdigit():
        return False
    hashed = bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()
    conn = get_connection()
    try:
        res = conn.execute(
            "UPDATE bot_users SET pin_hash=? WHERE chat_id=? AND status='approved'",
            (hashed, chat_id),
        )
        changed = res.rowcount > 0
    finally:
        conn.close()
    if changed:
        log_event(chat_id, "pin_set", {})
    return changed


def verify_pin(chat_id: int, pin: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT pin_hash FROM bot_users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return False
    try:
        return bcrypt.checkpw(pin.encode(), row[0].encode())
    except ValueError:
        return False


def is_approved(chat_id: int) -> bool:
    u = get_user(chat_id)
    return u is not None and u.status == "approved"
