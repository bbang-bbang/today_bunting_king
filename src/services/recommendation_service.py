"""추천 로그 서비스 — rec_id 생성·저장·조회.

rec_id 규칙: {MARKET}-{YYYYMMDD}-{NN}  (예: KR-20260416-01)
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from src.db.connection import get_connection

ALLOWED_MARKETS = ("KR", "US", "CR")
ALLOWED_MODES = ("bunt", "squeeze")


def _compact_date(session_date: str) -> str:
    return session_date.replace("-", "")


def _next_seq(conn, market: str, session_date: str) -> int:
    prefix = f"{market}-{_compact_date(session_date)}-"
    row = conn.execute(
        "SELECT rec_id FROM recommendations "
        "WHERE rec_id LIKE ? ORDER BY rec_id DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    if not row:
        return 1
    return int(row[0].rsplit("-", 1)[-1]) + 1


def _lookup_name(conn, code: str) -> str:
    row = conn.execute(
        "SELECT name FROM instruments WHERE code = ?", (code,)
    ).fetchone()
    return row[0] if row else code


def create_recommendation(
    *,
    chat_id: int,
    market: str,
    code: str,
    strategy_mode: str,
    entry_price: int,
    target_price: int,
    stop_price: int,
    expected_return_pct: float,
    reason_summary: str,
    ensemble_score: float | None = None,
    reason_json: dict | None = None,
    name: str | None = None,
    session_date: date | None = None,
) -> str:
    if market not in ALLOWED_MARKETS:
        raise ValueError(f"market must be one of {ALLOWED_MARKETS}, got {market}")
    if strategy_mode not in ALLOWED_MODES:
        raise ValueError(f"strategy_mode must be one of {ALLOWED_MODES}, got {strategy_mode}")

    sd = (session_date or date.today()).isoformat()
    now = datetime.now().isoformat(timespec="seconds")

    conn = get_connection()
    try:
        seq = _next_seq(conn, market, sd)
        rec_id = f"{market}-{_compact_date(sd)}-{seq:02d}"
        resolved_name = name or _lookup_name(conn, code)
        conn.execute(
            """INSERT INTO recommendations
               (rec_id, chat_id, session_date, market, code, name,
                strategy_mode, entry_price, target_price, stop_price,
                expected_return_pct, ensemble_score, reason_summary,
                reason_json, sent_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec_id, chat_id, sd, market, code, resolved_name,
                strategy_mode, entry_price, target_price, stop_price,
                expected_return_pct, ensemble_score, reason_summary,
                json.dumps(reason_json, ensure_ascii=False) if reason_json else None,
                now,
            ),
        )
        return rec_id
    finally:
        conn.close()


def get_recommendation(rec_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM recommendations WHERE rec_id = ?", (rec_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ============================================================
# 행위 로그 (recommendation_actions)
# ============================================================
# action_type 별 허용 태그
BOUGHT_TAGS  = {"trust_ensemble", "intuition", "news", "other", "pending"}
SKIPPED_TAGS = {"low_trust", "no_cash", "missed_timing", "other"}
SOLD_TAGS    = {"target_hit", "stop_hit", "eod_forced", "impulsive", "news_change", "other"}

TAG_LABEL_KR = {
    "trust_ensemble": "앙상블 신뢰",
    "intuition":      "직감",
    "news":           "뉴스 반응",
    "low_trust":      "신뢰 부족",
    "no_cash":        "자금 없음",
    "missed_timing":  "타이밍 놓침",
    "target_hit":     "목표 도달",
    "stop_hit":       "손절",
    "eod_forced":     "강제 청산",
    "impulsive":      "뇌동 매도",
    "news_change":    "뉴스 변심",
    "other":          "기타",
    "pending":        "사유 미입력",
}


def _validate_tag(action_type: str, tag: str) -> None:
    allowed = {
        "bought":  BOUGHT_TAGS,
        "skipped": SKIPPED_TAGS,
        "sold":    SOLD_TAGS,
    }.get(action_type)
    if allowed is None:
        raise ValueError(f"unknown action_type: {action_type}")
    if tag not in allowed:
        raise ValueError(f"tag '{tag}' not allowed for {action_type}")


def insert_action(
    *,
    rec_id: str,
    chat_id: int,
    action_type: str,
    reason_tag: str,
    price: int | None = None,
    quantity: int | None = None,
    reason_text: str | None = None,
    realized_pnl: int | None = None,
    realized_return_pct: float | None = None,
) -> int:
    _validate_tag(action_type, reason_tag)
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO recommendation_actions
               (rec_id, chat_id, action_type, price, quantity,
                reason_tag, reason_text, realized_pnl, realized_return_pct, acted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (rec_id, chat_id, action_type, price, quantity,
             reason_tag, reason_text, realized_pnl, realized_return_pct, now),
        )
        return cur.lastrowid
    finally:
        conn.close()


def update_action_reason(
    action_id: int,
    reason_tag: str,
    reason_text: str | None = None,
) -> bool:
    """매수 시점 'pending'으로 생성된 행위 로그에 사유 태그를 기록."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT action_type FROM recommendation_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        if not row:
            return False
        _validate_tag(row[0], reason_tag)
        if reason_text is None:
            conn.execute(
                "UPDATE recommendation_actions SET reason_tag = ? WHERE id = ?",
                (reason_tag, action_id),
            )
        else:
            conn.execute(
                "UPDATE recommendation_actions SET reason_tag = ?, reason_text = ? WHERE id = ?",
                (reason_tag, reason_text, action_id),
            )
        return True
    finally:
        conn.close()


def get_unbought_recent_recs(chat_id: int, days: int = 1) -> list[dict]:
    """최근 N일 내 추천 중 아직 매수 안 한 rec 목록.

    PendingRecMonitor 가 진입가 하락 알림에 사용.
    스키마: [{rec_id, code, strategy_mode, entry_price, target_price, stop_price,
             ensemble_score, session_date}, ...]
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT r.rec_id, r.code, r.strategy_mode, r.entry_price,
                      r.target_price, r.stop_price, r.ensemble_score, r.session_date,
                      COALESCE(i.name, '')
               FROM recommendations r
               LEFT JOIN instruments i ON i.code = r.code
               WHERE r.chat_id = ?
                 AND r.session_date >= ?
                 AND NOT EXISTS (
                   SELECT 1 FROM recommendation_actions a
                   WHERE a.rec_id = r.rec_id AND a.action_type = 'bought'
                 )
               ORDER BY r.session_date DESC, r.rec_id""",
            (chat_id, cutoff),
        ).fetchall()
        return [
            {
                "rec_id": r[0], "code": r[1], "strategy_mode": r[2],
                "entry_price": int(r[3]), "target_price": int(r[4]),
                "stop_price": int(r[5]),
                "ensemble_score": float(r[6] or 0),
                "session_date": r[7],
                "name": r[8],
            }
            for r in rows
        ]
    finally:
        conn.close()


def find_pending_bought_action(rec_id: str) -> int | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT id FROM recommendation_actions
               WHERE rec_id = ? AND action_type = 'bought' AND reason_tag = 'pending'
               ORDER BY id DESC LIMIT 1""",
            (rec_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def find_latest_bought_action(rec_id: str) -> dict | None:
    """rec_id 에 매수(bought) 행위가 기록됐는지 조회.

    cb_button 에서 "만료 또는 이미 처리됨" 이후 실제 매수 여부 확인용.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT id, price, quantity, reason_tag, acted_at
               FROM recommendation_actions
               WHERE rec_id = ? AND action_type = 'bought'
               ORDER BY id DESC LIMIT 1""",
            (rec_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "action_id": row[0], "price": row[1], "quantity": row[2],
            "reason_tag": row[3], "acted_at": row[4],
        }
    finally:
        conn.close()
