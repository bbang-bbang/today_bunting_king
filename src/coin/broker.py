"""코인 broker — paper / live (Phase 3 에서 live 추가).

paper:
  - Upbit 공개 API 로 실시간 가격
  - 가상 매수/매도 → coin_orders + coin_positions DB INSERT
  - 수수료 0.05% × 양방향 + 슬리피지 0.05% 시뮬

live (Phase 3):
  - Upbit 인증 API (UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY) — 추후 구현
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.coin.db import get_coin_connection, coin_log_event

log = logging.getLogger("bunting.coin.broker")

UPBIT_BASE = "https://api.upbit.com/v1"

# 시뮬 파라미터 (paper)
COMMISSION_PCT = 0.05      # 한 방향
SLIPPAGE_PCT = 0.05        # 매수 시 가산, 매도 시 차감


@dataclass
class CurrentTicker:
    market: str
    price: float           # 현재가 (KRW)
    high_24h: float
    low_24h: float
    timestamp: datetime


def fetch_current_price(market: str) -> Optional[CurrentTicker]:
    """Upbit ticker — 인증 X."""
    try:
        res = httpx.get(f"{UPBIT_BASE}/ticker", params={"markets": market}, timeout=5)
        res.raise_for_status()
        data = res.json()
    except httpx.HTTPError as e:
        log.warning("[%s] ticker 조회 실패: %s", market, e)
        return None
    if not data:
        return None
    t = data[0]
    return CurrentTicker(
        market=market,
        price=float(t["trade_price"]),
        high_24h=float(t["high_price"]),
        low_24h=float(t["low_price"]),
        timestamp=datetime.now(timezone.utc),
    )


def get_account_state() -> dict:
    """coin_account 단일 row + open positions 합산."""
    conn = get_coin_connection()
    try:
        acc = conn.execute("SELECT * FROM coin_account WHERE id=1").fetchone()
        opens = conn.execute(
            "SELECT id, market, buy_price, quantity, target_price, stop_price, opened_at "
            "FROM coin_positions WHERE status='open' ORDER BY opened_at"
        ).fetchall()
    finally:
        conn.close()
    if not acc:
        return {"cash_krw": 0, "paused": False, "trade_mode": "paper", "positions": []}
    return {
        "cash_krw": float(acc["cash_krw"]),
        "trade_mode": acc["trade_mode"],
        "paused": bool(acc["paused"]),
        "positions": [dict(p) for p in opens],
    }


def execute_paper_buy(
    market: str,
    quantity: float,
    target_price_pct: float,    # TP % (예: 5.0)
    stop_price_pct: float,      # SL %
    reason: str = "",
) -> dict:
    """paper 매수: 실시간 가격 + 슬리피지 적용 → DB INSERT.

    Returns {"success": bool, "position_id": int, "buy_price": float, "quantity": float, ...}
    """
    state = get_account_state()
    if state["paused"]:
        return {"success": False, "reason": "봇 일시정지 (paused)"}

    ticker = fetch_current_price(market)
    if ticker is None:
        return {"success": False, "reason": "Upbit ticker 조회 실패"}

    buy_price = ticker.price * (1 + SLIPPAGE_PCT / 100)
    cost = buy_price * quantity
    commission = cost * COMMISSION_PCT / 100
    total = cost + commission

    if total > state["cash_krw"]:
        return {
            "success": False,
            "reason": f"가용 KRW 부족 (필요 {int(total):,}, 보유 {int(state['cash_krw']):,})",
        }

    tp = buy_price * (1 + target_price_pct / 100)
    sl = buy_price * (1 - stop_price_pct / 100)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    audit_id = coin_log_event(None, "coin_order_buy", {
        "market": market, "qty": quantity, "price": buy_price,
        "tp": tp, "sl": sl, "reason": reason,
    })
    conn = get_coin_connection()
    try:
        cur = conn.execute(
            """INSERT INTO coin_orders
               (audit_id, market, side, trade_mode, quantity, price, status,
                filled_quantity, filled_avg_price, commission, slippage, reason,
                created_at, updated_at)
               VALUES (?, ?, 'buy', 'paper', ?, ?, 'filled', ?, ?, ?, ?, ?, ?, ?)""",
            (audit_id, market, quantity, buy_price, quantity, buy_price,
             commission, SLIPPAGE_PCT, reason, now, now),
        )
        order_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO coin_positions
               (market, buy_order_id, buy_price, quantity, target_price, stop_price,
                status, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, 'open', ?)""",
            (market, order_id, buy_price, quantity, tp, sl, now),
        )
        position_id = cur.lastrowid
        conn.execute(
            "UPDATE coin_account SET cash_krw=cash_krw-?, updated_at=? WHERE id=1",
            (total, now),
        )
        conn.commit()
    finally:
        conn.close()
    log.info("[paper buy] %s %.6f @ %.0f → pos %d", market, quantity, buy_price, position_id)
    return {
        "success": True, "position_id": position_id, "order_id": order_id,
        "market": market, "buy_price": buy_price, "quantity": quantity,
        "target_price": tp, "stop_price": sl, "commission": commission,
    }


def execute_paper_sell(position_id: int, reason: str = "tp") -> dict:
    """paper 매도: 현재가 - 슬리피지 → DB UPDATE."""
    conn = get_coin_connection()
    try:
        pos = conn.execute(
            "SELECT * FROM coin_positions WHERE id=? AND status='open'",
            (position_id,),
        ).fetchone()
    finally:
        conn.close()
    if not pos:
        return {"success": False, "reason": "open position 없음"}

    ticker = fetch_current_price(pos["market"])
    if ticker is None:
        return {"success": False, "reason": "Upbit ticker 조회 실패"}

    sell_price = ticker.price * (1 - SLIPPAGE_PCT / 100)
    qty = float(pos["quantity"])
    proceeds = sell_price * qty
    commission = proceeds * COMMISSION_PCT / 100
    net = proceeds - commission - (float(pos["buy_price"]) * qty * COMMISSION_PCT / 100)
    pnl = net - (float(pos["buy_price"]) * qty)
    return_pct = pnl / (float(pos["buy_price"]) * qty) * 100 if pos["buy_price"] else 0.0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    audit_id = coin_log_event(None, "coin_order_sell", {
        "market": pos["market"], "position_id": position_id,
        "price": sell_price, "qty": qty, "pnl": pnl, "reason": reason,
    })
    conn = get_coin_connection()
    try:
        cur = conn.execute(
            """INSERT INTO coin_orders
               (audit_id, market, side, trade_mode, quantity, price, status,
                filled_quantity, filled_avg_price, commission, slippage, reason,
                created_at, updated_at)
               VALUES (?, ?, 'sell', 'paper', ?, ?, 'filled', ?, ?, ?, ?, ?, ?, ?)""",
            (audit_id, pos["market"], qty, sell_price, qty, sell_price,
             commission, SLIPPAGE_PCT, reason, now, now),
        )
        sell_order_id = cur.lastrowid
        conn.execute(
            """UPDATE coin_positions SET sell_order_id=?, status='closed',
                                          pnl=?, closed_at=? WHERE id=?""",
            (sell_order_id, pnl, now, position_id),
        )
        conn.execute(
            "UPDATE coin_account SET cash_krw=cash_krw+?, updated_at=? WHERE id=1",
            (proceeds - commission, now),
        )
        conn.commit()
    finally:
        conn.close()
    log.info("[paper sell] pos %d @ %.0f  pnl %+.0f (%+.2f%%)  reason=%s",
             position_id, sell_price, pnl, return_pct, reason)
    return {
        "success": True, "position_id": position_id, "order_id": sell_order_id,
        "market": pos["market"], "sell_price": sell_price, "quantity": qty,
        "pnl": pnl, "return_pct": return_pct, "reason": reason,
    }


def set_paused(paused: bool) -> None:
    conn = get_coin_connection()
    try:
        conn.execute(
            "UPDATE coin_account SET paused=?, updated_at=datetime('now') WHERE id=1",
            (1 if paused else 0,),
        )
        conn.commit()
    finally:
        conn.close()
