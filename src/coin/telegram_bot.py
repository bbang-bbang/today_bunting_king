"""코인 봇 — 별도 telegram bot (KR 봇과 다른 token).

진입점: python -m src.coin.telegram_bot
필수 환경변수:
  TELEGRAM_COIN_BOT_TOKEN   — BotFather 신규 발급
  TELEGRAM_ADMIN_CHAT_ID    — KR 봇과 동일 chat (어드민 한 명)
  COIN_DB_PATH              — ./data/coin-bunting.db (기본)
  COIN_SEED_KRW             — 300000 (기본)
  COIN_TRADE_MODE           — 'paper' (기본) / 'live'

명령어 (어드민 한정):
  /coin_status     — 현재 상태 (cash, 보유, paused, 모드)
  /coin_balance    — 보유 코인 + 미실현 손익
  /coin_pnl        — 7일 P&L 통계
  /coin_pause      — 자동매매 일시정지
  /coin_resume     — 재개
  /coin_force      — 강제 시그널 평가 (디버그)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from telegram import BotCommand, Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters,
)

from src.coin.db import init_coin_schema, get_coin_connection, coin_log_event
from src.coin.broker import (
    fetch_current_price, get_account_state, set_paused,
)

log = logging.getLogger("bunting.coin.bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _is_admin(chat_id: int) -> bool:
    admin = int(os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "0") or "0")
    return admin and chat_id == admin


# ============================================================
# 명령어 핸들러
# ============================================================

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("❌ 어드민 전용")
        return
    state = get_account_state()
    paused = "⏸ 일시정지" if state["paused"] else "▶️ 가동 중"
    lines = [
        f"🪙 코인 봇 상태",
        "",
        f"  모드        {state['trade_mode']}",
        f"  가동        {paused}",
        f"  가용 KRW    {int(state['cash_krw']):,}원",
        f"  보유        {len(state['positions'])} 포지션",
    ]
    if state["positions"]:
        lines.append("")
        for p in state["positions"]:
            lines.append(
                f"  • {p['market']}  {p['quantity']:.6f} @ {p['buy_price']:,.0f}  "
                f"TP {p['target_price']:,.0f} / SL {p['stop_price']:,.0f}"
            )
    await update.message.reply_text("\n".join(lines))


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("❌ 어드민 전용")
        return
    state = get_account_state()
    if not state["positions"]:
        await update.message.reply_text(
            f"💼 보유 0종목\n  가용 KRW   {int(state['cash_krw']):,}원"
        )
        return
    lines = [f"💼 보유 {len(state['positions'])}종목", ""]
    total_value = 0.0
    total_cost = 0.0
    for p in state["positions"]:
        ticker = fetch_current_price(p["market"])
        cur_price = ticker.price if ticker else float(p["buy_price"])
        value = cur_price * float(p["quantity"])
        cost = float(p["buy_price"]) * float(p["quantity"])
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost else 0.0
        total_value += value
        total_cost += cost
        emoji = "📈" if pnl >= 0 else "📉"
        lines.append(
            f"  {emoji} {p['market']}  {p['quantity']:.6f}\n"
            f"      매수 {p['buy_price']:,.0f} → 현재 {cur_price:,.0f}\n"
            f"      평가 {value:,.0f}원  ({pnl:+,.0f}원, {pnl_pct:+.2f}%)"
        )
    total_pnl = total_value - total_cost
    total_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
    lines += [
        "",
        f"  Σ 평가  {total_value:,.0f}원  ({total_pnl:+,.0f}원, {total_pct:+.2f}%)",
        f"  💰 가용 KRW   {int(state['cash_krw']):,}원",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("❌ 어드민 전용")
        return
    conn = get_coin_connection()
    try:
        rows = conn.execute(
            """SELECT market, quantity, buy_price, pnl, datetime(closed_at) AS closed
               FROM coin_positions
               WHERE status='closed' AND date(closed_at) >= date('now','-7 days')
               ORDER BY closed_at DESC"""
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text("📊 7일 청산 0건")
        return
    n = len(rows)
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    total_pnl = sum(float(r["pnl"] or 0) for r in rows)
    avg_pct = sum(
        (float(r["pnl"] or 0) / (float(r["buy_price"]) * float(r["quantity"])) * 100)
        for r in rows if r["buy_price"] and r["quantity"]
    ) / n if n else 0.0
    win_rate = wins / n * 100 if n else 0.0
    lines = [
        f"📊 7일 P&L  ·  {n}건",
        "",
        f"  총 손익    {total_pnl:+,.0f}원",
        f"  평균 수익률 {avg_pct:+.2f}%",
        f"  승률       {wins}/{n}  ({win_rate:.1f}%)",
        "",
        "최근 5건:",
    ]
    for r in rows[:5]:
        emoji = "✅" if (r["pnl"] or 0) > 0 else "❌"
        lines.append(
            f"  {emoji} {r['market']}  {float(r['pnl'] or 0):+,.0f}원  ({r['closed']})"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("❌ 어드민 전용")
        return
    set_paused(True)
    coin_log_event(chat_id, "coin_paused", {"by": "admin"})
    await update.message.reply_text(
        "⏸ 자동매매 일시정지\n  • 신규 매수 X\n  • 기존 보유 자동 TP/SL 도 X (일시 동결)\n"
        "  • /coin_resume 으로 재개"
    )


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("❌ 어드민 전용")
        return
    set_paused(False)
    coin_log_event(chat_id, "coin_resumed", {"by": "admin"})
    await update.message.reply_text("▶️ 자동매매 재개")


async def cmd_force(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """디버그: 다음 사이클 기다리지 말고 즉시 시그널 평가."""
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("❌ 어드민 전용")
        return
    from src.coin.scheduler import job_signal_check
    await update.message.reply_text("🔄 강제 시그널 평가...")
    await job_signal_check(ctx)
    await update.message.reply_text("✅ 평가 완료 (결과는 별도 알림으로)")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_chat.id):
        await update.message.reply_text("❌ 어드민 전용 봇")
        return
    await update.message.reply_text(
        "🪙 코인 봇 명령어\n\n"
        "  /coin_status  — 봇 상태 + 보유 요약\n"
        "  /coin_balance — 보유 + 미실현 P&L (현재가 기준)\n"
        "  /coin_pnl     — 최근 7일 P&L 통계\n"
        "  /coin_pause   — 자동매매 일시정지\n"
        "  /coin_resume  — 재개\n"
        "  /coin_force   — 즉시 시그널 평가 (디버그)\n\n"
        "💡 자동매매가 paper 모드면 가상 시드로 시뮬, live 면 실제 KRW 거래"
    )


# ============================================================
# Application 빌드 + 실행
# ============================================================

async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("coin_status",  "봇 상태"),
        BotCommand("coin_balance", "보유 + P&L"),
        BotCommand("coin_pnl",     "7일 P&L"),
        BotCommand("coin_pause",   "자동매매 정지"),
        BotCommand("coin_resume",  "재개"),
        BotCommand("coin_force",   "강제 평가 (디버그)"),
        BotCommand("help",         "명령어 목록"),
    ])
    log.info("코인 봇 BotFather 메뉴 동기화 완료")


def build_app() -> Application:
    init_coin_schema()
    token = os.environ.get("TELEGRAM_COIN_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_COIN_BOT_TOKEN 환경변수 미설정. "
            "BotFather 에서 새 봇 발급 후 .env 에 추가하세요."
        )
    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("coin_status", cmd_status))
    app.add_handler(CommandHandler("coin_balance", cmd_balance))
    app.add_handler(CommandHandler("coin_pnl", cmd_pnl))
    app.add_handler(CommandHandler("coin_pause", cmd_pause))
    app.add_handler(CommandHandler("coin_resume", cmd_resume))
    app.add_handler(CommandHandler("coin_force", cmd_force))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    # Scheduler 등록
    from src.coin.scheduler import register_coin_jobs
    register_coin_jobs(app)
    return app


def main() -> None:
    app = build_app()
    state = get_account_state()
    log.info(
        "코인 봇 시작 — mode=%s seed=%s paused=%s",
        state["trade_mode"], int(state["cash_krw"]), state["paused"],
    )
    app.run_polling()


if __name__ == "__main__":
    main()
