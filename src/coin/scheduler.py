"""코인 봇 scheduler.

잡:
  1) job_signal_check  — 5분마다 시그널 평가 + 자동 매수
  2) job_position_check — 30초마다 보유 포지션 TP/SL 체크 + 자동 매도

운영 시간: 09~22h KST (Phase 1; 24h 는 Phase 4).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram.ext import Application, ContextTypes

from src.coin.broker import (
    fetch_current_price, get_account_state,
    execute_paper_buy, execute_paper_sell,
)
from src.coin.db import coin_log_event, get_coin_connection
from src.coin.upbit_data import fetch_candles
from src.coin.fear_greed import fetch_fear_greed_history, fng_to_hourly_index
from src.coin.ensemble import precompute_ensemble_signals

log = logging.getLogger("bunting.coin.scheduler")

KST = ZoneInfo("Asia/Seoul")

# 운영 파라미터
COIN_MARKETS = ["KRW-BTC", "KRW-ETH"]
TP_PCT = 5.0           # +5% 익절 (백테스트 v2 결과 기준)
SL_PCT = 1.5           # -1.5% 손절
SIGNAL_THRESHOLD = 0.6
PER_TRADE_PCT = 30     # 가용 KRW 의 30% 만 한 번에 매수
SIGNAL_INTERVAL_SEC = 5 * 60     # 5분
POSITION_INTERVAL_SEC = 30       # 30초

OPERATING_HOUR_START = 9         # KST
OPERATING_HOUR_END = 22


def is_operating_hour() -> bool:
    now = datetime.now(KST)
    return OPERATING_HOUR_START <= now.hour < OPERATING_HOUR_END


def _admin_chat_id() -> int | None:
    v = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "0")
    try:
        n = int(v)
        return n if n else None
    except ValueError:
        return None


# ============================================================
# 시그널 평가 + 자동 매수
# ============================================================

async def job_signal_check(ctx: ContextTypes.DEFAULT_TYPE):
    """5분마다 — 시그널 평가 → buy 시그널이면 매수."""
    if not is_operating_hour():
        return
    state = get_account_state()
    if state["paused"]:
        return

    # 동시 보유 종목 한도 — 코인은 1종목만 (파일럿)
    open_markets = {p["market"] for p in state["positions"]}
    if len(open_markets) >= 1:
        return

    # FNG 데이터 (1번에 받음, 캐시 X — 일별 갱신은 충분)
    try:
        fng = fetch_fear_greed_history(days=10)
    except Exception as e:
        log.warning("FNG 조회 실패: %s — sentiment 0 처리", e)
        fng = None

    bot: Bot = ctx.bot
    admin = _admin_chat_id()

    for market in COIN_MARKETS:
        if market in open_markets:
            continue   # 이미 보유 중
        # 최신 OHLCV 1시간봉 100개 (시그널 워밍업 + 최근)
        try:
            df = fetch_candles(market, unit_min=60, count=100)
        except Exception as e:
            log.warning("[%s] 캔들 조회 실패: %s", market, e)
            continue
        if df.empty or len(df) < 30:
            continue

        # FNG 시간봉 인덱싱
        fng_hourly = (
            fng_to_hourly_index(fng, df.index) if fng is not None and not fng.empty else None
        )

        sig_df = precompute_ensemble_signals(
            df, fng_hourly=fng_hourly, kp_hourly=None,
            threshold=SIGNAL_THRESHOLD,
            weights={"technical": 1.0, "sentiment": 0.8, "arbitrage": 0.0},
        )
        last_score = float(sig_df["total_score"].iloc[-1])
        last_buy = bool(sig_df["buy"].iloc[-1])

        log.info("[%s] score=%.2f buy=%s", market, last_score, last_buy)
        if not last_buy:
            continue

        # 매수
        ticker = fetch_current_price(market)
        if ticker is None:
            continue
        per_trade_krw = state["cash_krw"] * PER_TRADE_PCT / 100
        qty = per_trade_krw / ticker.price
        if qty <= 0:
            continue
        result = execute_paper_buy(
            market, qty,
            target_price_pct=TP_PCT, stop_price_pct=SL_PCT,
            reason=f"signal_score={last_score:.2f}",
        )
        if result["success"]:
            msg = (
                f"🪙 자동 매수 (paper)\n"
                f"\n"
                f"  종목     {market}\n"
                f"  수량     {result['quantity']:.6f}\n"
                f"  체결가   {result['buy_price']:,.0f}원\n"
                f"  🎯 TP    {result['target_price']:,.0f}  (+{TP_PCT}%)\n"
                f"  🛑 SL    {result['stop_price']:,.0f}  (-{SL_PCT}%)\n"
                f"\n"
                f"  시그널 점수 {last_score:.2f}\n"
                f"  남은 KRW   {int(get_account_state()['cash_krw']):,}원"
            )
        else:
            msg = f"⚠ 매수 실패  —  {market}\n  {result['reason']}"
        if admin:
            try:
                await bot.send_message(admin, msg)
            except Exception:
                log.exception("매수 알림 발송 실패")
        # 한 사이클에 1종목만 매수 (보수적)
        return


# ============================================================
# 보유 포지션 TP/SL 체크
# ============================================================

async def job_position_check(ctx: ContextTypes.DEFAULT_TYPE):
    """30초마다 — open 포지션의 TP/SL 도달 여부 체크 + 자동 청산."""
    if not is_operating_hour():
        return
    state = get_account_state()
    if state["paused"]:
        return
    if not state["positions"]:
        return

    bot: Bot = ctx.bot
    admin = _admin_chat_id()

    for pos in state["positions"]:
        ticker = fetch_current_price(pos["market"])
        if ticker is None:
            continue
        cur = ticker.price

        outcome: str | None = None
        if cur >= float(pos["target_price"]):
            outcome = "tp"
        elif cur <= float(pos["stop_price"]):
            outcome = "sl"

        if outcome is None:
            continue

        result = execute_paper_sell(int(pos["id"]), reason=outcome)
        if not result["success"]:
            continue

        icon = "🎯" if outcome == "tp" else "🛑"
        label = "익절" if outcome == "tp" else "손절"
        msg = (
            f"{icon} 자동 {label} (paper)\n"
            f"\n"
            f"  종목     {result['market']}\n"
            f"  수량     {result['quantity']:.6f}\n"
            f"  체결가   {result['sell_price']:,.0f}원\n"
            f"  순손익   {result['pnl']:+,.0f}원  ({result['return_pct']:+.2f}%)\n"
            f"\n"
            f"  KRW 잔고  {int(get_account_state()['cash_krw']):,}원"
        )
        if admin:
            try:
                await bot.send_message(admin, msg)
            except Exception:
                log.exception("청산 알림 발송 실패")


# ============================================================
# 등록
# ============================================================

def register_coin_jobs(app: Application) -> None:
    jq = app.job_queue
    jq.run_repeating(
        job_signal_check, interval=SIGNAL_INTERVAL_SEC,
        first=10, name="coin_signal_check",
    )
    jq.run_repeating(
        job_position_check, interval=POSITION_INTERVAL_SEC,
        first=20, name="coin_position_check",
    )
    log.info(
        "Coin scheduler: signal_check %ds / position_check %ds  "
        "(operating %d-%d KST)",
        SIGNAL_INTERVAL_SEC, POSITION_INTERVAL_SEC,
        OPERATING_HOUR_START, OPERATING_HOUR_END,
    )
