"""포트폴리오 — 잔고 집계, 매수/매도 실행 (paper/live 공통 통로).

모든 주문은 RiskGuard 통과 → BrokerAdapter 실행 → DB 기록.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime

from src import config

log = logging.getLogger("bunting.portfolio")


def _get_position_opened_at(position_id: int) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT opened_at FROM positions WHERE id=?", (position_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
from src.adapters.broker_base import BrokerAdapter, OrderRequest
from src.adapters.broker_paper import PaperBrokerAdapter
from src.db.connection import get_connection
from src.risk.guard import (
    MODE_PARAMS,
    SWING_MODE_PARAMS,
    GuardContext,
    OrderIntent,
    RiskGuard,
    Side,
    StrategyMode,
)
from src.services.audit_service import log_event


@dataclass
class OpenPositionDTO:
    position_id: int
    code: str
    buy_price: int
    quantity: int
    target_price: int
    stop_price: int
    strategy_mode: str
    opened_at: str
    name: str = ""  # 회사명 — instruments 테이블 LEFT JOIN


def get_broker(trade_mode: str) -> BrokerAdapter:
    if trade_mode == "paper":
        return PaperBrokerAdapter()
    # kis_mock / live 는 KISBrokerAdapter (키 필요)
    from src.adapters.broker_kis import KISBrokerAdapter
    return KISBrokerAdapter(config.TradeMode(trade_mode))


# ============================================================
# 잔고 / 포지션 조회
# ============================================================

def get_open_positions(chat_id: int, exclude_pending_sell: bool = True) -> list[OpenPositionDTO]:
    """status='open' position 목록.

    exclude_pending_sell=True (default) 면 sell_order_id 가 이미 마킹된 (= 매도 주문 등록·체결 대기)
    position 은 제외. 자동매도 사이클 (price_monitor) 에서 같은 종목 중복 매도 주문 방지.
    """
    conn = get_connection()
    try:
        sql = """
            SELECT p.id, p.code, p.buy_price, p.quantity,
                   p.target_price, p.stop_price, p.strategy_mode, p.opened_at,
                   COALESCE(i.name, '')
            FROM positions p
            LEFT JOIN instruments i ON i.code = p.code
            WHERE p.chat_id=? AND p.status='open'
        """
        if exclude_pending_sell:
            sql += " AND p.sell_order_id IS NULL"
        sql += " ORDER BY p.opened_at"
        rows = conn.execute(sql, (chat_id,)).fetchall()
        return [
            OpenPositionDTO(
                position_id=r[0], code=r[1], buy_price=r[2], quantity=r[3],
                target_price=r[4], stop_price=r[5], strategy_mode=r[6], opened_at=r[7],
                name=r[8],
            )
            for r in rows
        ]
    finally:
        conn.close()


def get_trade_history(chat_id: int, days: int = 7, limit: int = 20) -> list[dict]:
    """최근 N일 매수·매도 히스토리. closed/open 모두 포함, 최신순.

    스키마:
      [
        {
          "code": str, "name": str, "strategy_mode": str,
          "quantity": int, "buy_price": int,
          "opened_at": "YYYY-MM-DD HH:MM:SS", "status": "open"|"closed",
          "closed_at": "..."|None, "sell_price": int|None,
          "pnl": int|None, "return_pct": float|None,
        }, ...
      ]
    """
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT p.code, COALESCE(i.name, ''), p.strategy_mode,
                   p.quantity, p.buy_price, p.opened_at,
                   p.status, p.closed_at, p.pnl
            FROM positions p
            LEFT JOIN instruments i ON i.code = p.code
            WHERE p.chat_id=?
              AND (date(p.opened_at) >= ? OR (p.closed_at IS NOT NULL AND date(p.closed_at) >= ?))
            ORDER BY COALESCE(p.closed_at, p.opened_at) DESC
            LIMIT ?
            """,
            (chat_id, cutoff, cutoff, limit),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        code, name, mode, qty, buy_price, opened_at, status, closed_at, pnl = r
        notional_buy = int(buy_price) * int(qty)
        sell_price = None
        return_pct = None
        if status == "closed" and pnl is not None and notional_buy > 0:
            sell_price = int(buy_price) + (int(pnl) // int(qty)) if qty else None
            return_pct = round(int(pnl) / notional_buy * 100, 2)
        out.append({
            "code": code, "name": name, "strategy_mode": mode,
            "quantity": int(qty), "buy_price": int(buy_price),
            "opened_at": opened_at, "status": status,
            "closed_at": closed_at,
            "sell_price": sell_price,
            "pnl": int(pnl) if pnl is not None else None,
            "return_pct": return_pct,
        })
    return out


def get_closed_positions_on(chat_id: int, day_iso: str) -> list[dict]:
    """특정 날짜(KST)에 청산된 포지션 목록.

    각 dict: code, name, strategy_mode, buy_price, sell_price, quantity, pnl, pnl_pct.
    sell_price 는 broker_orders.filled_avg_price 우선, 없으면 buy_price + pnl/qty (paper).
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT p.code,
                      i.name,
                      p.strategy_mode,
                      p.buy_price,
                      p.quantity,
                      p.pnl,
                      bo.filled_avg_price
               FROM positions p
               LEFT JOIN instruments    i  ON i.code = p.code
               LEFT JOIN broker_orders  bo ON bo.id = p.sell_order_id
               WHERE p.chat_id = ?
                 AND p.status  = 'closed'
                 AND date(p.closed_at) = ?
               ORDER BY p.closed_at""",
            (chat_id, day_iso),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for code, name, mode, buy_price, qty, pnl, sell_filled in rows:
        pnl_int = int(pnl or 0)
        if sell_filled:
            sell_price = int(sell_filled)
        elif qty:
            sell_price = int(buy_price + pnl_int // qty)  # paper fallback
        else:
            sell_price = int(buy_price)
        cost = buy_price * qty
        pnl_pct = (pnl_int / cost * 100) if cost else 0.0
        out.append({
            "code": code,
            "name": name or "",
            "strategy_mode": mode,
            "buy_price": int(buy_price),
            "sell_price": sell_price,
            "quantity": int(qty),
            "pnl": pnl_int,
            "pnl_pct": pnl_pct,
        })
    return out


def get_closed_pnl_today(chat_id: int) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(pnl), 0) FROM positions
               WHERE chat_id=? AND status='closed' AND date(closed_at)=?""",
            (chat_id, date.today().isoformat()),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def get_losing_days_streak(chat_id: int) -> int:
    """최근 연속 손실일 수. 가장 최근 거래일부터 역순으로 손실인 날을 셈.

    NULL P&L 은 미완료/미산출 (예: reconcile ghost, 5/4 일괄 close) 으로 0 취급 — streak 끊음.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT date(closed_at) AS trade_date, SUM(pnl) AS daily_pnl
               FROM positions
               WHERE chat_id=? AND status='closed'
               GROUP BY trade_date
               ORDER BY trade_date DESC
               LIMIT 10""",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()

    streak = 0
    for row in rows:
        daily_pnl = row[1]
        if daily_pnl is None:
            break  # 산출 불가 → streak 끝
        if int(daily_pnl) < 0:
            streak += 1
        else:
            break
    return streak


# 수수료/세금 (broker_kis 와 동일 계수 — 0.015% 수수료 / 0.2% 거래세)
_FEE_BPS_PER_100K  = 15   # 0.015%
_TAX_BPS_PER_100K  = 20   # 0.2% (매도만)


def net_pnl_after_fees(buy_price: int, current_price: int, qty: int) -> tuple[int, float]:
    """매수·매도 수수료 + 매도 거래세 차감한 실손익과 수익률(%) 반환.

    - 음수면 청산 시 실제 손해. UI 에서 ⚠ 표기 트리거.
    """
    if qty <= 0 or buy_price <= 0:
        return 0, 0.0
    buy_notional  = buy_price * qty
    sell_notional = current_price * qty
    buy_fee  = buy_notional  * _FEE_BPS_PER_100K // 100_000
    sell_fee = sell_notional * _FEE_BPS_PER_100K // 100_000
    sell_tax = sell_notional * _TAX_BPS_PER_100K // 100_000
    nominal  = sell_notional - buy_notional
    net      = nominal - buy_fee - sell_fee - sell_tax
    pct      = net / buy_notional * 100
    return int(net), round(pct, 2)


def get_pnl_summary(chat_id: int) -> dict:
    """손익 통계 — 오늘 실현 / 누적 실현 / 승률·평균 / 거래 횟수.

    스키마:
      {
        "today_realized": int,      # 오늘 청산 손익 합
        "today_count": int,         # 오늘 청산 건수
        "total_realized": int,      # 누적 청산 손익 합
        "total_count": int,         # 누적 청산 건수
        "win_count": int,           # pnl>0 건수
        "loss_count": int,          # pnl<0 건수
        "win_rate_pct": float,      # 승률(%) — 무승부(pnl=0)는 분모에서 제외
        "avg_pnl_per_trade": int,   # 거래당 평균 손익 (총 / 건수, 0이면 0)
      }
    """
    today = date.today().isoformat()
    conn = get_connection()
    try:
        today_row = conn.execute(
            """SELECT COALESCE(SUM(pnl), 0), COUNT(pnl)
               FROM positions
               WHERE chat_id=? AND status='closed' AND date(closed_at)=?""",
            (chat_id, today),
        ).fetchone()
        total_row = conn.execute(
            """SELECT COALESCE(SUM(pnl), 0), COUNT(pnl),
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)
               FROM positions
               WHERE chat_id=? AND status='closed'""",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()

    today_realized = int(today_row[0] or 0)
    today_count = int(today_row[1] or 0)
    total_realized = int(total_row[0] or 0)
    total_count = int(total_row[1] or 0)
    win = int(total_row[2] or 0)
    loss = int(total_row[3] or 0)
    decided = win + loss  # 무승부(pnl=0)는 승률 계산 분모에서 제외
    win_rate = (win / decided * 100) if decided > 0 else 0.0
    avg_pnl = (total_realized // total_count) if total_count > 0 else 0
    return {
        "today_realized": today_realized,
        "today_count": today_count,
        "total_realized": total_realized,
        "total_count": total_count,
        "win_count": win,
        "loss_count": loss,
        "win_rate_pct": round(win_rate, 1),
        "avg_pnl_per_trade": int(avg_pnl),
    }


def get_account_summary(chat_id: int, active_seed: int) -> dict:
    positions = get_open_positions(chat_id)
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE chat_id=? AND status='closed'",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()
    total_closed_pnl = int(row[0]) if row else 0

    used_cash = sum(p.buy_price * p.quantity for p in positions)
    cash_available = active_seed + total_closed_pnl - used_cash
    return {
        "active_seed": active_seed,
        "cash_available": cash_available,
        "open_positions": positions,
        "closed_pnl_total": total_closed_pnl,
        "estimated_equity": cash_available + used_cash,
    }


async def get_broker_balance(trade_mode: str) -> dict | None:
    """실제 브로커 계좌 잔고 조회. paper 모드에선 None 반환.

    KIS 호출 실패 시 {"error": "...msg..."} 형태로 반환 (UI 에 오류 표시).
    """
    if trade_mode == "paper":
        return None
    try:
        broker = get_broker(trade_mode)
        return await broker.get_balance()
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 매수 실행
# ============================================================

# (chat_id, code) 단위 in-flight 매수 락 — 동일 사용자가 같은 종목 중복 매수 방지.
# 텔레그램 버튼 빠른 더블클릭, 같은 종목 번트/스퀴즈 두 버튼 동시 클릭 등 race 방어.
_INFLIGHT_BUYS: set[tuple[int, str]] = set()
_INFLIGHT_LOCK = asyncio.Lock()


async def _acquire_buy_lock(chat_id: int, code: str) -> bool:
    """이미 진행 중이면 False (호출자가 거절 처리). 성공 시 True 반환."""
    async with _INFLIGHT_LOCK:
        key = (chat_id, code)
        if key in _INFLIGHT_BUYS:
            return False
        _INFLIGHT_BUYS.add(key)
        return True


async def _release_buy_lock(chat_id: int, code: str) -> None:
    async with _INFLIGHT_LOCK:
        _INFLIGHT_BUYS.discard((chat_id, code))


async def execute_buy(
    chat_id: int,
    code: str,
    quantity: int,
    price: int,
    strategy_mode: str,
    active_seed: int,
    pin_verified: bool = False,
    holding_mode: str = "swing_week",
) -> dict:
    # 동일 (사용자, 종목) 매수 in-flight 면 즉시 거절 — race 보호.
    if not await _acquire_buy_lock(chat_id, code):
        log.warning("[buy %s] 동일 종목 매수 진행 중 — 중복 시도 거절", code)
        return {
            "success": False,
            "reason": (
                f"⚠ 같은 종목({code}) 매수 진행 중입니다. "
                f"잠시 후 결과 확인 후 다시 시도하세요."
            ),
        }
    try:
        return await _execute_buy_inner(
            chat_id=chat_id, code=code, quantity=quantity, price=price,
            strategy_mode=strategy_mode, active_seed=active_seed,
            pin_verified=pin_verified, holding_mode=holding_mode,
        )
    finally:
        await _release_buy_lock(chat_id, code)


async def _execute_buy_inner(
    chat_id: int,
    code: str,
    quantity: int,
    price: int,
    strategy_mode: str,
    active_seed: int,
    pin_verified: bool = False,
    holding_mode: str = "swing_week",
) -> dict:
    summary = get_account_summary(chat_id, active_seed)
    open_codes = {p.code for p in summary["open_positions"]}
    position_value = sum(p.buy_price * p.quantity for p in summary["open_positions"])

    # 가용 현금 — KIS 모드면 실계좌가 진실(source of truth).
    # paper 모드는 봇 DB 시드 기준.
    cash_balance = summary["cash_available"]
    if config.TRADE_MODE in (config.TradeMode.KIS_MOCK, config.TradeMode.LIVE):
        broker_bal = await get_broker_balance(config.TRADE_MODE.value)
        if broker_bal and "error" not in broker_bal:
            cash_balance = max(0, int(broker_bal.get("cash_available", 0)))
        # KIS 조회 실패 시 봇 DB 기준으로 fallback (보수적)

    intent = OrderIntent(
        chat_id=chat_id, side=Side.BUY, code=code, quantity=quantity, price=price,
        strategy_mode=StrategyMode(strategy_mode), pin_provided=pin_verified,
    )
    ctx = GuardContext(
        cash_balance=cash_balance,
        position_value=position_value,
        open_position_codes=open_codes,
        daily_realized_pnl=get_closed_pnl_today(chat_id),
        losing_days_streak=get_losing_days_streak(chat_id),
    )
    is_mock = config.TRADE_MODE in (config.TradeMode.PAPER, config.TradeMode.KIS_MOCK)
    result = RiskGuard(active_seed_krw=active_seed, is_mock=is_mock).check(intent, ctx)
    if not result.approved:
        log_event(chat_id, "guard_block", {
            "action": "buy", "code": code, "qty": quantity, "price": price,
            "reason": result.reason, "requires_pin": result.requires_pin,
        })
        return {"success": False, "reason": result.reason,
                "requires_pin": result.requires_pin}

    # 브로커 실행
    broker = get_broker(config.TRADE_MODE.value)
    req = OrderRequest(side="buy", code=code, quantity=quantity, price=price)
    res = await broker.submit_order(req)

    # 'partial' 도 체결의 일종 — 부분 체결 수량만 봇 DB 에 기록.
    if res.status not in ("filled", "partial"):
        # pending = KIS 에 주문 등록(ODNO 있음)됐지만 체결 대기 (지정가 미체결 등).
        # failed  = KIS 응답 누락 또는 진짜 거절.
        is_pending = res.status == "pending" and bool(res.broker_order_id)
        event_type = "order_pending" if is_pending else "order_failed"
        log_event(chat_id, event_type, {
            "side": "buy", "code": code, "error": res.error,
            "broker_order_id": res.broker_order_id,
            "status": res.status,
        })
        reason = res.error or ("주문 대기 중" if is_pending else "주문 실패")
        if res.broker_order_id and "ODNO" not in reason:
            reason = f"{reason}\n(ODNO={res.broker_order_id})"
        return {
            "success": False,
            "reason": reason,
            "pending": is_pending,
            "needs_reconcile": (not is_pending) and ("체결" in (res.error or "")),
            "broker_order_id": res.broker_order_id,
        }

    # 실제 체결가·수량 사용 (partial 또는 시장가 체결가 변동 대응).
    fill_qty = int(res.filled_quantity) if res.filled_quantity else quantity
    fill_price = int(res.filled_avg_price) if res.filled_avg_price else price

    # DB 기록 — 국내 주식은 스윙 파라미터 사용. compute_target_stop 가 호가 정렬까지 처리.
    mode = StrategyMode(strategy_mode)
    tp, sl = RiskGuard.compute_target_stop(fill_price, mode, holding_mode=holding_mode)
    now = datetime.now().isoformat(timespec="seconds")

    audit_id = log_event(chat_id, "order_buy", {
        "code": code, "qty": fill_qty, "price": fill_price, "mode": strategy_mode,
        "broker_order_id": res.broker_order_id,
        "partial": res.status == "partial",
        "requested_qty": quantity,
    })

    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, broker_order_id,
                status, filled_quantity, filled_avg_price, commission, tax, created_at, updated_at)
               VALUES (?, ?, 'buy', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (audit_id, config.TRADE_MODE.value, code, quantity, price, res.broker_order_id,
             res.status, fill_qty, fill_price, res.commission, res.tax, now, now),
        )
        buy_order_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO positions
               (chat_id, code, strategy_mode, buy_order_id, buy_price, quantity,
                target_price, stop_price, status, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (chat_id, code, strategy_mode, buy_order_id, fill_price, fill_qty, tp, sl, now),
        )
        position_id = cur.lastrowid
    finally:
        conn.close()

    return {
        "success": True,
        "position_id": position_id, "code": code, "qty": fill_qty, "price": fill_price,
        "requested_qty": quantity,    # 요청 수량 — UI 에서 부분 체결 강조 시 사용
        "target": tp, "stop": sl, "commission": res.commission,
        "partial": res.status == "partial",
    }


# ============================================================
# 매도 실행
# ============================================================

async def execute_sell(
    chat_id: int,
    position_id: int,
    price: int,
    active_seed: int,
) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT id, code, buy_price, quantity, strategy_mode, buy_order_id
               FROM positions WHERE id=? AND chat_id=? AND status='open'""",
            (position_id, chat_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {"success": False, "reason": "보유 포지션 없음"}

    pid, code, buy_price, qty, strat, buy_order_id = row

    # KIS 모드: 외부 매도(앱·진단 등)로 KIS 보유량이 DB qty 와 다를 수 있음.
    # 매도 직전 KIS 보유량을 신뢰해 min(DB qty, KIS qty) 로 보정 — "잔고내역 없음"(40240000) 회피.
    if config.TRADE_MODE in (config.TradeMode.KIS_MOCK, config.TradeMode.LIVE):
        try:
            broker_bal = await get_broker_balance(config.TRADE_MODE.value)
            if broker_bal and "error" not in broker_bal:
                kis_qty = next(
                    (p["quantity"] for p in broker_bal["positions"] if p["code"] == code), 0
                )
                if kis_qty == 0:
                    # grace 기간 내 매수면 KIS 일시적 누락일 수 있음 — 매도 보류, 다음 사이클로 미룸
                    try:
                        opened_at = datetime.fromisoformat(_get_position_opened_at(pid) or "")
                        if (datetime.now() - opened_at).total_seconds() < 15 * 60:
                            log.warning(
                                "[sell %s] KIS qty=0 이지만 매수 후 15분 이내 — 매도 보류 (다음 사이클 재시도)",
                                code,
                            )
                            return {"success": False, "reason": f"KIS 잔고 동기화 대기 중 ({code}) — 다음 사이클 재시도"}
                    except Exception:
                        pass
                    return {"success": False, "reason": f"KIS 잔고에 {code} 없음 — DB 정합성 깨짐, reconcile 필요"}
                if kis_qty < qty:
                    log.warning(
                        "[sell %s] DB qty=%d > KIS qty=%d → KIS 기준으로 매도",
                        code, qty, kis_qty,
                    )
                    qty = kis_qty
        except Exception as e:
            log.warning("[sell %s] KIS qty 확인 실패, DB 값 사용: %s", code, e)

    intent = OrderIntent(
        chat_id=chat_id, side=Side.SELL, code=code, quantity=qty, price=price,
        strategy_mode=StrategyMode(strat), pin_provided=True,  # 매도는 PIN 면제
    )
    # sell guard 는 보유 여부·장 시간만 체크하므로 간단 ctx
    ctx = GuardContext(
        cash_balance=0,
        position_value=buy_price * qty,
        open_position_codes={code},
        daily_realized_pnl=get_closed_pnl_today(chat_id),
        losing_days_streak=get_losing_days_streak(chat_id),
    )
    is_mock = config.TRADE_MODE in (config.TradeMode.PAPER, config.TradeMode.KIS_MOCK)
    result = RiskGuard(active_seed_krw=active_seed, is_mock=is_mock).check(intent, ctx)
    if not result.approved:
        log_event(chat_id, "guard_block",
                  {"action": "sell", "code": code, "reason": result.reason})
        return {"success": False, "reason": result.reason}

    broker = get_broker(config.TRADE_MODE.value)
    req = OrderRequest(side="sell", code=code, quantity=qty, price=price)
    res = await broker.submit_order(req)

    # 매도 응답 분기:
    #   filled / partial: 정상 체결 → position close + PnL 기록
    #   pending          : KIS 등록됐지만 미체결 대기 → position 에 sell_order_id 마킹 (status='open' 유지),
    #                       price_monitor 가 재주문 안 하도록 차단
    #   else             : 진짜 실패
    if res.status == "pending" and res.broker_order_id:
        now = datetime.now().isoformat(timespec="seconds")
        audit_id = log_event(chat_id, "order_sell_pending", {
            "code": code, "qty": qty, "price": price,
            "broker_order_id": res.broker_order_id,
            "position_id": pid,
        })
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO broker_orders
                   (audit_id, trade_mode, side, code, quantity, price, broker_order_id,
                    status, filled_quantity, filled_avg_price, commission, tax, created_at, updated_at)
                   VALUES (?, ?, 'sell', ?, ?, ?, ?, 'pending', 0, 0, 0, 0, ?, ?)""",
                (audit_id, config.TRADE_MODE.value, code, qty, price, res.broker_order_id, now, now),
            )
            sell_order_row_id = cur.lastrowid
            # status 는 'open' 유지 — KIS 체결 확인 후 정합 (수동 또는 reconcile)
            # sell_order_id 마킹으로 price_monitor 가 재주문 안 함
            conn.execute(
                "UPDATE positions SET sell_order_id=? WHERE id=?",
                (sell_order_row_id, pid),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "success": False,
            "pending": True,
            "code": code, "qty": qty,
            "broker_order_id": res.broker_order_id,
            "reason": (
                f"매도 주문 등록 (ODNO={res.broker_order_id}). "
                f"체결 대기 중 — KIS 앱 미체결 확인."
            ),
        }

    if res.status not in ("filled", "partial"):
        return {"success": False, "reason": res.error or "주문 실패"}

    # 부분/완전 체결 — 실제 체결가/수량 사용
    fill_qty = int(res.filled_quantity) if res.filled_quantity else qty
    fill_price = int(res.filled_avg_price) if res.filled_avg_price else price

    gross = (fill_price - buy_price) * fill_qty
    buy_commission = buy_price * fill_qty * 15 // 100_000
    net_pnl = gross - buy_commission - res.commission - res.tax
    now = datetime.now().isoformat(timespec="seconds")

    audit_id = log_event(chat_id, "order_sell", {
        "code": code, "qty": fill_qty, "price": fill_price, "pnl": net_pnl,
        "broker_order_id": res.broker_order_id,
        "partial": res.status == "partial",
        "requested_qty": qty,
    })

    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, broker_order_id,
                status, filled_quantity, filled_avg_price, commission, tax, created_at, updated_at)
               VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (audit_id, config.TRADE_MODE.value, code, qty, price, res.broker_order_id,
             res.status, fill_qty, fill_price, res.commission, res.tax, now, now),
        )
        sell_order_id = cur.lastrowid
        # 부분 체결일 때도 일단 close (남은 수량은 reconcile 로 별도 처리). 단순화 우선.
        conn.execute(
            """UPDATE positions SET sell_order_id=?, status='closed', pnl=?, closed_at=?
               WHERE id=?""",
            (sell_order_id, net_pnl, now, pid),
        )
    finally:
        conn.close()

    return {
        "success": True, "code": code, "qty": fill_qty, "exit_price": fill_price,
        "gross_pnl": gross, "net_pnl": net_pnl,
        "return_pct": net_pnl / (buy_price * fill_qty) * 100 if fill_qty > 0 else 0,
        "partial": res.status == "partial",
    }


async def execute_sell_all_by_code(chat_id: int, code: str) -> dict:
    """KIS 보유 종목 1개를 시장가 전량 매도.

    - KIS 보유 수량 기준 (봇 DB ≠ KIS 차이 있어도 KIS 수량 매도)
    - 봇 DB의 해당 종목 모든 open row → closed (수수료/거래세 차감 PnL)
    - paper 모드에서는 봇 DB 기준
    """
    broker = get_broker(config.TRADE_MODE.value)

    # 1) 매도 대상 수량 결정 — KIS 모드면 KIS 보유 우선, paper면 봇 DB
    qty = 0
    fallback_price = 0
    if config.TRADE_MODE in (config.TradeMode.KIS_MOCK, config.TradeMode.LIVE):
        try:
            bal = await broker.get_balance()
            kis_pos = next((p for p in bal.get("positions", []) if p["code"] == code), None)
            if kis_pos:
                qty = int(kis_pos["quantity"])
                fallback_price = int(kis_pos["current_price"])
        except Exception as e:
            return {"success": False, "reason": f"KIS 조회 실패: {e}"}
    if qty <= 0:
        # paper 또는 KIS 보유 없음 → 봇 DB 기준
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT SUM(quantity) FROM positions WHERE chat_id=? AND code=? AND status='open'",
                (chat_id, code),
            ).fetchone()
        finally:
            conn.close()
        qty = int(rows[0] or 0)
    if qty <= 0:
        return {"success": False, "reason": "보유 수량 없음"}

    # 2) 시장가 매도 (price=None → ORD_DVSN=01 시장가)
    req = OrderRequest(side="sell", code=code, quantity=qty, price=None)
    res = await broker.submit_order(req)

    # pending = KIS 등록됐지만 미체결 대기 (KIS 모의투자 quirk).
    # 봇 DB position 들에 sell_order_id 마킹만 하고 close 안 함 (체결가 모름).
    if res.status == "pending" and res.broker_order_id:
        now = datetime.now().isoformat(timespec="seconds")
        audit_id = log_event(chat_id, "order_sell_all_pending", {
            "code": code, "qty": qty,
            "broker_order_id": res.broker_order_id,
        })
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO broker_orders
                   (audit_id, trade_mode, side, code, quantity, price, broker_order_id,
                    status, filled_quantity, filled_avg_price, commission, tax, created_at, updated_at)
                   VALUES (?, ?, 'sell', ?, ?, 0, ?, 'pending', 0, 0, 0, 0, ?, ?)""",
                (audit_id, config.TRADE_MODE.value, code, qty, res.broker_order_id, now, now),
            )
            sell_order_row_id = cur.lastrowid
            conn.execute(
                "UPDATE positions SET sell_order_id=? WHERE chat_id=? AND code=? AND status='open' AND sell_order_id IS NULL",
                (sell_order_row_id, chat_id, code),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "success": False,
            "pending": True,
            "code": code, "qty": qty,
            "broker_order_id": res.broker_order_id,
            "reason": (
                f"시장가 매도 주문 등록 (ODNO={res.broker_order_id}). "
                f"체결 대기 중 — KIS 앱 미체결 확인."
            ),
        }

    if res.status not in ("filled", "partial"):
        return {"success": False, "reason": res.error or "주문 실패"}

    fill_price = int(res.filled_avg_price) if res.filled_avg_price else fallback_price
    fill_qty = int(res.filled_quantity) if res.filled_quantity else qty
    if fill_price <= 0:
        fill_price = fallback_price  # 시장가 + 응답 누락 fallback

    # 3) 봇 DB 모든 open row → closed (row 단위로 PnL 분배)
    now = datetime.now().isoformat(timespec="seconds")
    audit_id = log_event(chat_id, "order_sell_all", {
        "code": code, "qty": fill_qty, "price": fill_price,
        "broker_order_id": res.broker_order_id,
        "partial": res.status == "partial",
        "requested_qty": qty,
    })
    total_net_pnl = 0
    total_buy_notional = 0
    n_rows = 0
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, broker_order_id,
                status, filled_quantity, filled_avg_price, commission, tax, created_at, updated_at)
               VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (audit_id, config.TRADE_MODE.value, code, qty, fill_price,
             res.broker_order_id, res.status, fill_qty, fill_price,
             res.commission, res.tax, now, now),
        )
        sell_order_id = cur.lastrowid

        rows = conn.execute(
            """SELECT id, buy_price, quantity FROM positions
               WHERE chat_id=? AND code=? AND status='open'""",
            (chat_id, code),
        ).fetchall()
        for r_id, buy_price, q in rows:
            buy_notional = int(buy_price) * int(q)
            sell_notional = fill_price * int(q)
            buy_fee = buy_notional * 15 // 100_000
            sell_fee = sell_notional * 15 // 100_000
            sell_tax = sell_notional * 20 // 100_000
            net = (sell_notional - buy_notional) - buy_fee - sell_fee - sell_tax
            total_net_pnl += net
            total_buy_notional += buy_notional
            n_rows += 1
            conn.execute(
                """UPDATE positions SET sell_order_id=?, status='closed', pnl=?, closed_at=?
                   WHERE id=?""",
                (sell_order_id, net, now, r_id),
            )
    finally:
        conn.close()

    return_pct = (total_net_pnl / total_buy_notional * 100) if total_buy_notional > 0 else 0.0
    return {
        "success": True, "code": code, "qty": fill_qty,
        "exit_price": fill_price, "net_pnl": int(total_net_pnl),
        "return_pct": round(return_pct, 2),
        "rows_closed": n_rows,
    }


async def liquidate_all(chat_id: int, active_seed: int) -> list[dict]:
    """모든 open position 시장가 청산. KIS 현재가 조회 후 매도."""
    positions = get_open_positions(chat_id)
    if not positions:
        return []

    # KIS 현재가 조회 시도
    prices: dict[str, int] = {}
    try:
        from src.adapters.market_data_kis import KISMarketDataSource
        kis = KISMarketDataSource()
        codes = list({p.code for p in positions})
        cp_map = kis.fetch_current_prices(codes)
        prices = {code: cp.price for code, cp in cp_map.items()}
    except Exception:
        pass  # KIS 실패 시 fallback

    results = []
    for p in positions:
        sell_price = prices.get(p.code, p.buy_price)  # 현재가 없으면 매수가 fallback
        r = await execute_sell(chat_id, p.position_id, sell_price, active_seed)
        results.append(r)
    return results
