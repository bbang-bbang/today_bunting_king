"""Telegram 봇 — 로컬 서버 + Long polling.

명령어:
  /start <초대코드>        등록
  /setpin <6자리>          PIN 설정
  /도움 | /help            명령 목록
  /추천 | /recommend       오늘의 번트/스퀴즈 추천 (inline 버튼)
  /재추천 | /rerecommend   데이터 최신화 + 재계산 후 재발송 (3-5분, 본인만)
  /잔고 | /balance         현재 포지션 + 현금
  /모드 번트|스퀴즈        전략 모드 전환
  /긴급청산 | /panic       전 포지션 즉시 청산

Inline 버튼:
  🟢 매수 → UUID 기반 확인 (10분 TTL) → RiskGuard → BrokerAdapter → DB
  취소   → 그대로 만료
"""
from __future__ import annotations

import asyncio
import logging
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src import config
from src.bot.scheduler import register_jobs
from src.db.connection import get_connection, init_schema
from src.ensemble.recommender import recommend
from src.services import (
    audit_service,
    confirmation_service,
    portfolio_service,
    recommendation_service,
    user_service,
)

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("bunting.bot")


# ============================================================
# Helpers
# ============================================================

def _qty_keyboard(code: str, price: int, rec_qty: int) -> InlineKeyboardMarkup:
    """수량 선택 인라인 키보드. 25%·50%·추천(100%) + 직접입력·취소."""
    seen: set[int] = set()
    rows = []
    for divisor, label in [(4, "25%"), (2, "50%"), (1, "추천")]:
        qty = max(1, rec_qty // divisor)
        if qty in seen:
            continue
        seen.add(qty)
        order_val = price * qty
        rows.append([InlineKeyboardButton(
            f"{label}  {qty}주  ({order_val:,}원)",
            callback_data=f"qty:{qty}",
        )])
    rows.append([
        InlineKeyboardButton("직접 입력", callback_data="qty:custom"),
        InlineKeyboardButton("취소", callback_data="qty:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _buy_price_keyboard(price_options: dict) -> InlineKeyboardMarkup:
    """매수 가격 선택 키보드.
    price_options 키:
      rec, cur, agg, pas — 고정 옵션
      sig_<N> — 시그널 옵션 (5일저가, 20일MA, 볼린저하 등). value = (label, price).
    """
    rows = []
    if price_options.get("rec"):
        rows.append([InlineKeyboardButton(
            f"📌 추천가  {price_options['rec']:,}원",
            callback_data="bprc:rec",
        )])
    if price_options.get("cur"):
        rows.append([InlineKeyboardButton(
            f"⚡ 현재가  {price_options['cur']:,}원",
            callback_data="bprc:cur",
        )])
    # 시그널 가격들 (5년치 OHLCV 기반)
    for key in sorted(price_options.keys()):
        if not key.startswith("sig_"):
            continue
        label, price = price_options[key]
        rows.append([InlineKeyboardButton(
            f"{label}  {price:,}원",
            callback_data=f"bprc:{key}",
        )])
    if price_options.get("agg"):
        rows.append([InlineKeyboardButton(
            f"🎯 즉시체결  {price_options['agg']:,}원",
            callback_data="bprc:agg",
        )])
    if price_options.get("pas"):
        rows.append([InlineKeyboardButton(
            f"🐢 더 싸게  {price_options['pas']:,}원",
            callback_data="bprc:pas",
        )])
    rows.append([
        InlineKeyboardButton("✏️ 직접 입력", callback_data="bprc:custom"),
        InlineKeyboardButton("취소", callback_data="bprc:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


async def _balance_footer_lines(chat_id: int) -> list[str]:
    """매수/매도 결과 메시지에 붙일 잔액 푸터.

    KIS 모드(kis_mock/live)면 KIS 실계좌가 진실 — 메인 표기.
    paper 모드면 봇 DB 가상 시드 표기.
    """
    summary = portfolio_service.get_account_summary(chat_id, config.SEED_KRW)
    n_positions = len(summary["open_positions"])

    broker_bal = await portfolio_service.get_broker_balance(config.TRADE_MODE.value)
    if broker_bal and "error" not in broker_bal:
        # KIS 잔고가 있으면 그것을 메인으로
        return [
            "━━━━━━━━━━━━━━━━",
            f"  💵 KIS 주문가능  {broker_bal['cash_available']:+,}원",
            f"  📊 KIS 평가금액  {broker_bal['total_evaluation']:,}원",
            f"  📦 보유 포지션   {n_positions}종목",
        ]
    # paper 또는 KIS 조회 실패 시 봇 DB 기준
    return [
        "━━━━━━━━━━━━━━━━",
        f"  💵 가용 현금    {summary['cash_available']:,}원",
        f"  📦 보유 포지션  {n_positions}종목",
    ]


def _holding_mode_for(chat_id: int) -> str:
    """user.holding_mode 조회. 사용자 없거나 컬럼 없으면 swing_week 기본."""
    try:
        u = user_service.get_user(chat_id)
        return getattr(u, "holding_mode", None) or "swing_week"
    except Exception:
        return "swing_week"


async def _send_buy_preview(send_fn, intent: dict, chat_id: int) -> None:
    """매수 확정 직전 미리보기 — 수량×가격, 수수료, TP/SL 가격, 모드별 손익."""
    from src.risk.guard import RiskGuard, StrategyMode, SWING_MODE_PARAMS, MODE_PARAMS
    code = intent["code"]
    price = intent["price"]
    qty = intent["quantity"]
    mode = intent.get("strategy_mode", "bunt")
    holding = _holding_mode_for(chat_id)

    notional = price * qty
    commission = notional * 15 // 100_000

    # 매수 후 자동매도 가격 (모드별 + 보유 모드별)
    try:
        mode_enum = StrategyMode(mode)
        param_table = SWING_MODE_PARAMS if holding == "swing_week" else MODE_PARAMS
        params = param_table[mode_enum]
        tp, sl = RiskGuard.compute_target_stop(price, mode_enum, holding_mode=holding)
        tp_pnl = (tp - price) * qty
        sl_pnl = (sl - price) * qty
    except Exception:
        tp = sl = 0
        tp_pnl = sl_pnl = 0
        params = {"tp_pct": 0, "sl_pct": 0}

    # 회사명 조회
    from src.db.connection import get_connection as _get_conn
    conn = _get_conn()
    try:
        row = conn.execute("SELECT name FROM instruments WHERE code=?", (code,)).fetchone()
        company = row[0] if row and row[0] else ""
    finally:
        conn.close()
    head = f"{code} {company}" if company else code

    from src.bot.scheduler import _mode_explanation_block
    lines = [
        f"🛒 매수 미리보기  —  {head}",
        "",
    ]
    lines += _mode_explanation_block(mode, holding)
    holding_suffix = " (당일매매)" if holding == "day" else " (주간 스윙)"
    lines += [
        "",
        f"  수량       {qty}주",
        f"  매수가     {price:,}원",
        f"  매수금액   {notional:,}원",
        f"  수수료     ~{commission:,}원 (예상)",
        "",
        f"  ━━━ 매수 후 자동매도 가격{holding_suffix} ━━━",
        f"  🎯 목표  {tp:,}원  (+{params['tp_pct']}%, 손익 {tp_pnl:+,}원)",
        f"  🛑 손절  {sl:,}원  (-{params['sl_pct']}%, 손익 {sl_pnl:+,}원)",
        "",
        f"💡 [매수 확정] 누르면 지정가 주문 → 체결되면 자동매도 활성화",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 매수 확정", callback_data="bgo:confirm")],
        [
            InlineKeyboardButton("✏️ 수량 변경", callback_data="bgo:requantity"),
            InlineKeyboardButton("취소", callback_data="bgo:cancel"),
        ],
    ])
    await send_fn("\n".join(lines), reply_markup=kb)


async def _execute_buy_intent(
    send_fn,
    chat_id: int,
    intent: dict,
    qty: int,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """수량 확정 후 매수 실행 + 결과 메시지 발송. send_fn 은 edit_message_text 또는 reply_text."""
    # 즉시 "처리 중" 피드백 — KIS 5xx retry 등으로 본 작업이 늦어져도 사용자 안심
    try:
        await send_fn(
            f"🔄 매수 처리 중...  —  {intent['code']}\n"
            f"  수량 {qty}주 @ {intent['price']:,}원\n"
            f"  KIS 응답 대기 (최대 ~15초)"
        )
    except Exception:
        pass   # 메시지 편집 실패해도 본 작업은 진행
    holding = _holding_mode_for(chat_id)
    result = await portfolio_service.execute_buy(
        chat_id=chat_id,
        code=intent["code"],
        quantity=qty,
        price=intent["price"],
        strategy_mode=intent["strategy_mode"],
        active_seed=config.SEED_KRW,
        pin_verified=False,
        holding_mode=holding,
    )
    if result["success"]:
        rec_id = intent.get("rec_id")
        if rec_id:
            recommendation_service.insert_action(
                rec_id=rec_id, chat_id=chat_id, action_type="bought",
                reason_tag="trust_ensemble",
                price=result["price"], quantity=result["qty"],
            )
        notional = result["price"] * result["qty"]
        # 회사명 조회 (없으면 코드만)
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT name FROM instruments WHERE code=?", (result["code"],)
            ).fetchone()
            company = row[0] if row and row[0] else ""
        finally:
            conn.close()
        head = f"{result['code']} {company}" if company else result["code"]
        is_partial = result.get("partial")
        title = "⚠️ 매수 부분 체결" if is_partial else "✅ 매수 체결"
        requested = int(result.get("requested_qty") or result["qty"])
        filled = int(result["qty"])
        unfilled = max(0, requested - filled)

        if is_partial:
            qty_line = (
                f"  수량     {filled}주  "
                f"⚠ 요청 {requested}주 중 미체결 {unfilled}주"
            )
        else:
            qty_line = f"  수량     {filled}주"

        lines = [
            f"{title}  —  {head}",
            "",
            qty_line,
            f"  단가     {result['price']:,}원",
            f"  금액     {notional:,}원",
            f"  수수료   {result['commission']:,}원",
            "",
            f"  🎯 목표   {result['target']:,}원",
            f"  🛑 손절   {result['stop']:,}원",
            "",
        ]
        if is_partial:
            lines += [
                f"━━━ 주의: 부분 체결 ━━━",
                f"  • 자동 익절/손절은 **체결된 {filled}주** 기준으로만 작동",
                f"  • 미체결 {unfilled}주는 KIS 앱 [미체결 주문] 확인 필요",
                f"    → 추가 체결 시 봇 DB 자동 반영 (1분 폴링)",
                f"    → 취소하려면 KIS 앱에서 직접 취소",
                f"  • 정합성 점검: /reconcile",
                "",
            ]
        lines += await _balance_footer_lines(chat_id)
        await send_fn("\n".join(lines))
    else:
        # 3분기:
        #   pending          — KIS 에 주문 등록됐지만 미체결 대기 (지정가가 시장가와 안 맞음 등)
        #   needs_reconcile  — 봇 DB ≠ KIS 정합성 깨짐 (KIS 응답 누락)
        #   else             — RiskGuard 사전 차단
        if result.get("pending"):
            title = "⏳ 매수 주문 대기 중"
            guide = [
                "👉 KIS 앱에서 [미체결 주문] 메뉴를 확인하세요.",
                "  • 체결되면 잔고에 자동 반영됩니다",
                "  • 취소하려면 KIS 앱에서 직접 취소",
                "  • 체결 후 봇 DB 정합성은 /reconcile 로 동기화",
            ]
        elif result.get("needs_reconcile"):
            title = "⚠ 매수 결과 불확실"
            guide = [
                "👉 다음 순서로 확인하세요:",
                "  1) KIS 앱에서 실제 체결 여부 확인",
                "  2) 체결됐으면 /reconcile (봇 DB 동기화)",
                "  3) 미체결이면 그대로 두세요",
            ]
        else:
            title = "❌ 매수 거절"
            guide = []
        lines = [f"{title}  —  {intent['code']}", "", result["reason"], ""]
        if guide:
            lines += guide + [""]
        lines += await _balance_footer_lines(chat_id)
        await send_fn("\n".join(lines))

async def _require_approved(update: Update) -> user_service.BotUser | None:
    chat_id = update.effective_chat.id
    user = user_service.get_user(chat_id)
    if not user or user.status != "approved":
        await update.message.reply_text("먼저 등록하세요: /start <초대코드>")
        return None
    return user


def _list_candidate_codes() -> list[str]:
    """분석 유니버스(analysis_universe) 기준 후보 코드.

    2026-04-22 B위원회 결정. 유니버스 빌드 필요.
    """
    from src.universe.builder import get_universe_codes
    return get_universe_codes(order_by="rank")


# ============================================================
# Commands
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if not args or args[0] != config.TELEGRAM_INVITE_CODE:
        await update.message.reply_text(
            "등록하려면: /start <초대코드>\n"
            "(초대코드는 .env 의 TELEGRAM_INVITE_CODE 값)"
        )
        return
    chat_id = update.effective_chat.id
    user = user_service.register_user(chat_id, trade_mode=config.TRADE_MODE.value)
    await update.message.reply_text(
        f"✅ 등록 완료\n"
        f"chat_id: {chat_id}\n"
        f"거래 모드: {user.trade_mode}\n"
        f"전략 모드: {user.strategy_mode}\n"
        f"활성 시드: {config.SEED_KRW:,}원\n\n"
        f"명령어:\n"
        f"  /recommend — 오늘의 추천\n"
        f"  /balance — 현재 포지션\n"
        f"  /pnl — 손익 현황\n"
        f"  /history — 거래 히스토리\n"
        f"  /mode bunt|squeeze\n"
        f"  /panic — 전 포지션 청산\n"
        f"  /setpin <6자리> — PIN 설정"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🤖 번팅봇 명령어 전체  ·  [{config.TRADE_MODE.value.upper()} · 시드 {config.SEED_KRW:,}원]\n"
        "\n"
        "📌 등록 / 도움\n"
        "  /start <초대코드>                          사용자 등록\n"
        "  /help · 도움 · 도움말 · 명령어              이 메시지\n"
        "\n"
        "🎯 추천 / 매매\n"
        "  /recommend · 추천 · 종목추천 · 오늘추천     오늘의 번트·스퀴즈 양 모드 추천\n"
        "  /rerecommend · 재추천                      데이터 최신화 + 재계산 (3-5분, 본인만)\n"
        "  /sell · 매도 · 팔기 · 종목매도              특정 종목 시장가 전량 매도\n"
        "  /panic · 긴급청산 · 전체매도 · 패닉         전 포지션 즉시 청산\n"
        "\n"
        "📊 조회\n"
        "  /balance · 잔고 · 내잔고 · 포지션           보유 + 현금 + KIS 실계좌 (+즉시매도 버튼)\n"
        "  /lookup <코드|이름> · 조회 삼성전자          임의 종목 정보 (앙상블·뉴스·가상 TP/SL)\n"
        "    └ 종목명·코드만 입력해도 조회됩니다 (예: 005930)\n"
        "  /pnl · 손익 · 수익 · 수익률                 오늘 청산·미실현·누적·승률\n"
        "  /history · 히스토리 · 거래내역 · 거래 · 이력  최근 7일 매수·매도\n"
        "\n"
        "⚙️ 설정\n"
        "  /mode bunt|squeeze                         전략 모드 전환\n"
        "    별칭: 모드 번트 · 모드변경 스퀴즈\n"
        "  /setpin <6자리>                             PIN 설정 (30%+ 매수용)\n"
        "\n"
        "🤝 자동 코칭 (자동 발송)\n"
        "  • TP/SL 도달 → 자동 매도 + 결과 메시지\n"
        "  • TP/SL 30% 근접 → 알림 (자동 X)\n"
        "  • 추천 진입가 -3% 이내 → 더 좋은 가격 매수 검토 알림\n"
        "  • 연속 손실 2일+ → 추천 시 '쉬는 것도 전략' 경고\n"
        "  • 금 15:20 → 미청산 포지션 청산 리마인더\n"
        "  • 금 15:40 → 주간 회고 리포트\n"
        "\n"
        "💡 매수는 추천 메시지의 [매수] 버튼으로. 직접 수량 입력 시 숫자로 응답."
    )
    if _is_admin(update.effective_chat.id):
        text += (
            "\n\n"
            "🛠 어드민\n"
            "  /refresh_all · 갱신 · 데이터갱신           데이터 증분 수집 + 추천 재발송 (본인만)\n"
            "  /refresh_all all · 전체갱신                위와 같되 승인 사용자 전체 발송\n"
            "  /reconcile · 정합성                       KIS↔봇DB 차이 점검 (dry-run)\n"
            "  /reconcile apply · 정합성적용              차이 적용 (ghost close + orphan adopt)"
        )
    await update.message.reply_text(text)


async def cmd_setpin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await _require_approved(update)
    if not user:
        return
    args = ctx.args or []
    if len(args) != 1 or not args[0].isdigit() or len(args[0]) != 6:
        await update.message.reply_text("사용법: /setpin <6자리 숫자>")
        return
    ok = user_service.set_pin(user.chat_id, args[0])
    if ok:
        await update.message.reply_text("✅ PIN 설정 완료 (30% 초과 주문 시 사용)")
    else:
        await update.message.reply_text("❌ PIN 설정 실패")


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await _require_approved(update)
    if not user:
        return
    args = ctx.args or []
    if not args:
        from src.risk.guard import MODE_PARAMS, SWING_MODE_PARAMS, StrategyMode
        sb = SWING_MODE_PARAMS[StrategyMode.BUNT]
        sq = SWING_MODE_PARAMS[StrategyMode.SQUEEZE]
        db = MODE_PARAMS[StrategyMode.BUNT]
        dq = MODE_PARAMS[StrategyMode.SQUEEZE]
        await update.message.reply_text(
            f"현재 전략: {user.strategy_mode}\n"
            f"변경: /mode bunt  또는  /mode squeeze\n\n"
            f"기본(주간 스윙) 자동매도 — 추천이 적용하는 값:\n"
            f"  🟢 bunt(안정)    +{sb['tp_pct']}% 익절 / -{sb['sl_pct']}% 손절\n"
            f"  🟠 squeeze(공격) +{sq['tp_pct']}% 익절 / -{sq['sl_pct']}% 손절\n\n"
            f"당일매매(/holding day)면 더 좁게:\n"
            f"  bunt +{db['tp_pct']}/-{db['sl_pct']}  ·  squeeze +{dq['tp_pct']}/-{dq['sl_pct']}\n\n"
            f"내게 지금 적용되는 값은 /strategy"
        )
        return
    mapping = {"번트": "bunt", "스퀴즈": "squeeze",
               "bunt": "bunt", "squeeze": "squeeze"}
    new_mode = mapping.get(args[0].lower())
    if not new_mode:
        await update.message.reply_text("모드: 번트 / 스퀴즈")
        return
    user_service.update_strategy_mode(user.chat_id, new_mode)
    await update.message.reply_text(f"✅ 전략 모드 → {new_mode}")


_HOLDING_LABEL_KR = {"day": "당일매매", "swing_week": "주간 스윙"}


def _effective_tp_sl(strategy_mode: str, holding_mode: str, early: bool) -> tuple[int, int, str]:
    """현재 설정으로 적용될 effective TP/SL %. regime label 도 함께."""
    from src.risk.guard import MODE_PARAMS, SWING_MODE_PARAMS, StrategyMode
    sm = StrategyMode(strategy_mode)
    if holding_mode == "day":
        # day 는 TP/SL 둘 다 좁음. early 효과 없음 (TP 가 이미 day).
        p = MODE_PARAMS[sm]
        return p["tp_pct"], p["sl_pct"], "당일 (양쪽 좁음)"
    # swing_week
    sw = SWING_MODE_PARAMS[sm]
    if early:
        # 조기 익절: TP 만 day 로 좁힘, SL 은 swing
        d = MODE_PARAMS[sm]
        return d["tp_pct"], sw["sl_pct"], "스윙+조기익절 (TP 좁고 SL 넓음)"
    return sw["tp_pct"], sw["sl_pct"], "스윙 기본 (양쪽 넓음)"


async def cmd_strategy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """전략 + 보유 + 조기익절 통합 조회 — 현재 effective TP/SL 명시.

    /mode, /holding, /early 의 조합 결과를 한 번에 보여줌.
    """
    user = await _require_approved(update)
    if not user:
        return

    tp, sl, regime = _effective_tp_sl(user.strategy_mode, user.holding_mode, user.early_take_profit)
    mode_icon = "🟢" if user.strategy_mode == "bunt" else "🟠"
    holding_icon = "📅" if user.holding_mode == "day" else "📆"
    early_state = "ON ⚡" if user.early_take_profit else "OFF"

    lines = [
        "📋 내 전략 설정",
        "",
        f"  {mode_icon} 전략 모드   {user.strategy_mode}  (/mode 로 변경)",
        f"  {holding_icon} 보유 모드   {_HOLDING_LABEL_KR.get(user.holding_mode, user.holding_mode)}  (/holding 으로 변경)",
        f"  ⚡ 조기 익절   {early_state}  (/early 로 변경)",
        "",
        f"  ━━━ 현재 적용되는 자동매도 ━━━",
        f"   🎯 TP   +{tp}%",
        f"   🛑 SL   -{sl}%",
        f"   ({regime})",
        "",
        "━━━ 3가지 시나리오 비교 ━━━",
        "  ① 스윙 기본               TP 넓음 / SL 넓음   (장기 +7~12% 노림)",
        "  ② 스윙 + /early on        TP 좁음 / SL 넓음   (작은 익절·손실 시간 줌)",
        "  ③ /holding day            TP 좁음 / SL 좁음   (당일 결판)",
        "",
        "💡 swing+early 와 day 의 차이는 **SL 폭**. 손실에 시간 주려면 ②, 짧게 끝내려면 ③.",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_holding(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """보유 모드(당일/주간) 토글. /holding day | swing | (인자 없음=조회)"""
    user = await _require_approved(update)
    if not user:
        return
    args = ctx.args or []
    if not args:
        tp, sl, regime = _effective_tp_sl(
            user.strategy_mode, user.holding_mode, user.early_take_profit,
        )
        early_note = " + /early ON" if user.early_take_profit else ""
        await update.message.reply_text(
            f"현재 보유 모드: {_HOLDING_LABEL_KR.get(user.holding_mode, user.holding_mode)}{early_note}\n"
            f"  → 적용 중: TP +{tp}% / SL -{sl}%  ({regime})\n"
            f"\n"
            f"변경: /holding day  또는  /holding swing\n\n"
            f"📅 당일매매 (day) — 그날 안에 청산.\n"
            f"   bunt:    +3% / -2%   |  squeeze: +5% / -3%\n"
            f"   매일 15:20 강제 청산 리마인더 발송.\n\n"
            f"📆 주간 스윙 (swing) — 5일 이내 청산.\n"
            f"   bunt:    +7% / -4%   |  squeeze: +12% / -5%\n"
            f"   금요일 15:20 강제 청산 리마인더 발송.\n\n"
            f"⚠ /early on 과 조합:\n"
            f"  • swing + /early on  → TP 만 day 수준(좁음), SL 은 swing(넓음)\n"
            f"  • day + /early on    → 차이 없음 (이미 day 가 더 좁음)\n\n"
            f"📋 통합 조회는 /strategy"
        )
        return
    arg = args[0].lower()
    mapping = {
        "day": "day", "당일": "day", "당일매매": "day",
        "swing": "swing_week", "swing_week": "swing_week",
        "스윙": "swing_week", "주간": "swing_week", "주간스윙": "swing_week",
    }
    new_mode = mapping.get(arg)
    if not new_mode:
        await update.message.reply_text("보유 모드: day / swing 중 선택")
        return
    ok = user_service.update_holding_mode(user.chat_id, new_mode)
    if ok:
        await update.message.reply_text(
            f"✅ 보유 모드 → {_HOLDING_LABEL_KR[new_mode]}\n"
            f"신규 매수부터 적용됩니다."
        )
    else:
        await update.message.reply_text("❌ 변경 실패")


async def cmd_early(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """조기 익절 토글. /early on | off | (인자 없음=조회).

    ON 시 스윙 포지션이라도 day-TP (+3% bunt / +5% squeeze) 도달하면 즉시 매도.
    스윙 큰 상승은 놓치지만 작은 수익 확정. 손절은 swing 기준 그대로.
    """
    user = await _require_approved(update)
    if not user:
        return
    args = ctx.args or []
    cur = "ON" if user.early_take_profit else "OFF"

    if not args:
        tp, sl, regime = _effective_tp_sl(
            user.strategy_mode, user.holding_mode, user.early_take_profit,
        )
        day_warning = (
            "\n⚠ 현재 /holding day 라 /early 효과 없음 (이미 day TP 가 더 좁음).\n"
            if user.holding_mode == "day" else ""
        )
        await update.message.reply_text(
            f"현재 조기 익절: {cur}\n"
            f"  → 적용 중: TP +{tp}% / SL -{sl}%  ({regime}){day_warning}\n"
            f"변경: /early on  또는  /early off\n\n"
            f"⚡ 조기 익절 ON  (swing 모드일 때만 의미)\n"
            f"   스윙 포지션이라도 당일 +3%(번트) / +5%(스퀴즈) 도달하면 즉시 매도.\n"
            f"   • 작은 수익을 빠르게 확정 → 승률 ↑\n"
            f"   • 큰 상승(+7~12%) 놓칠 수 있음\n"
            f"   • 손절은 그대로 swing 기준 (-4%/-5%) — 손실에는 시간 줌\n\n"
            f"💤 조기 익절 OFF (기본)\n"
            f"   기존 동작 — swing TP 만 평가. 5일까지 보유 가능.\n\n"
            f"📋 통합 조회는 /strategy\n"
            f"💡 /holding day 와 차이: SL 폭. day 는 SL 도 좁음 (-2/-3%), early 는 SL 넓음 (-4/-5%)."
        )
        return

    arg = args[0].lower()
    on_words = {"on", "켜", "켜기", "활성", "true", "1", "yes"}
    off_words = {"off", "꺼", "끄기", "비활성", "false", "0", "no"}
    if arg in on_words:
        enabled = True
    elif arg in off_words:
        enabled = False
    else:
        await update.message.reply_text("값: on / off")
        return

    ok = user_service.update_early_take_profit(user.chat_id, enabled)
    if ok:
        new = "ON" if enabled else "OFF"
        await update.message.reply_text(
            f"✅ 조기 익절 → {new}\n"
            f"기존 보유 포지션부터 즉시 적용됩니다."
        )
    else:
        await update.message.reply_text("❌ 변경 실패")


def _open_position_targets(chat_id: int) -> dict[str, tuple[int, int]]:
    """봇 DB 미청산 포지션의 code → (목표가, 손절가) 맵.
    KIS 모드 잔고에 자동매도가를 곁들여 보여주기 위함. 같은 종목 분할매수면 첫 레코드 기준."""
    try:
        s = portfolio_service.get_account_summary(chat_id, config.SEED_KRW)
    except Exception:
        return {}
    out: dict[str, tuple[int, int]] = {}
    for p in s.get("open_positions", []):
        if p.code not in out and p.target_price and p.stop_price:
            out[p.code] = (int(p.target_price), int(p.stop_price))
    return out


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await _require_approved(update)
    if not user:
        return

    mode_label = "번트" if user.strategy_mode == "bunt" else "스퀴즈"
    is_kis_mode = config.TRADE_MODE in (
        config.TradeMode.KIS_MOCK, config.TradeMode.LIVE,
    )
    # 보유종목별 즉시매도 버튼용 — (code, name, qty, avg, current) 수집
    sell_items: list[dict] = []

    if is_kis_mode:
        # KIS 일원화 — KIS 실계좌가 메인. 봇 DB는 백엔드에서만 (히스토리/회고 위해).
        broker_bal = await portfolio_service.get_broker_balance(config.TRADE_MODE.value)
        lines = [
            f"🏧 KIS {config.TRADE_MODE.value} 실계좌  ·  {mode_label} 모드",
            "",
        ]
        if broker_bal is None or "error" in broker_bal:
            err = broker_bal.get("error", "?") if broker_bal else "조회 실패"
            lines.append(f"  ⚠ 조회 실패: {err}")
        else:
            seed_man = config.SEED_KRW // 10_000
            lines += [
                f"  💎 순자산         {broker_bal['total_evaluation']:,}원   (시작 {seed_man:,}만 ± 손익)",
                f"  💰 손익           {broker_bal['total_pnl']:+,}원  ({broker_bal['total_pnl_pct']:+.2f}%)",
                f"  💵 주문가능현금   {broker_bal['cash_available']:+,}원",
            ]
            if broker_bal["cash_available"] < 0:
                lines.append("     ⚠ D+2 결제 대기 — 모의투자라 실제 빚 아닙니다")
            if broker_bal["positions"]:
                lines += ["", f"📦 보유 {len(broker_bal['positions'])}종목"]
                targets = _open_position_targets(user.chat_id)
                total_cost_kis = 0
                for p in broker_bal["positions"]:
                    cost = p["quantity"] * p["avg_price"]
                    total_cost_kis += cost
                    net, net_pct = portfolio_service.net_pnl_after_fees(
                        p["avg_price"], p["current_price"], p["quantity"],
                    )
                    net_icon = "⚠" if net < 0 else "💵"
                    head = f"{p['code']} {p['name']}" if p.get("name") else p["code"]
                    lines += [
                        f"  ┌ {head}  ·  {p['quantity']}주",
                        f"  │ 매수 {p['avg_price']:,}원 → 현재 {p['current_price']:,}원",
                        f"  │ 💰 매수금    {cost:,}원",
                        f"  │ 📊 평가손익  {p['pnl']:+,}원  ({p['pnl_pct']:+.2f}%)",
                        f"  │ {net_icon} 실손익    {net:+,}원  ({net_pct:+.2f}%)",
                    ]
                    ts = targets.get(p["code"])
                    if ts:
                        tp, sl = ts
                        lines.append(f"  │ 🎯 자동매도 {tp:,}  /  🛑 {sl:,}")
                    lines.append("  └─")
                    sell_items.append({
                        "code": p["code"], "name": p.get("name", ""),
                        "quantity": int(p["quantity"]),
                        "avg": int(p["avg_price"]), "current": int(p["current_price"]),
                        "net": int(net),
                    })
                lines.append(f"  Σ 총 매수금  {total_cost_kis:,}원")
            else:
                lines += ["", "📦 보유 종목 없음"]
    else:
        # paper 모드 — 봇 DB 가상 시드 표시
        s = portfolio_service.get_account_summary(user.chat_id, config.SEED_KRW)
        lines = [
            f"💼 내 계좌  |  {config.TRADE_MODE.value}  ·  {mode_label}",
            "",
            f"  활성 시드   {s['active_seed']:,}원",
            f"  가용 현금   {s['cash_available']:,}원",
            f"  누적 청산 손익   {s['closed_pnl_total']:+,}원",
        ]
        if s["open_positions"]:
            from collections import defaultdict
            groups: dict[tuple[str, str], list] = defaultdict(list)
            for p in s["open_positions"]:
                groups[(p.code, p.strategy_mode)].append(p)
            lines.append("")
            lines.append(f"📦 보유 포지션  {len(groups)}종목")
            total_cost = 0
            for (code, mode), poss in groups.items():
                total_qty = sum(pp.quantity for pp in poss)
                total_buy = sum(pp.quantity * pp.buy_price for pp in poss)
                avg_price = total_buy // total_qty if total_qty else 0
                total_cost += total_buy
                name = poss[0].name
                head = f"{code} {name}" if name else code
                qty_line = f"{total_qty}주  ·  평균 매수가 {avg_price:,}원"
                if len(poss) > 1:
                    qty_line += f"  ({len(poss)}회 분할)"
                earliest = min(pp.opened_at for pp in poss)[:10]
                tp = poss[0].target_price
                sl = poss[0].stop_price
                lines += [
                    f"  ┌ {head}  ({mode})",
                    f"  │ {qty_line}",
                    f"  │ 💰 매수금 {total_buy:,}원",
                    f"  │ 🎯 목표 {tp:,}  /  🛑 손절 {sl:,}",
                    f"  └ 진입일 {earliest}",
                ]
                # 같은 종목이 여러 모드여도 즉시매도는 전량 1회 — code 기준 1건만
                if not any(it["code"] == code for it in sell_items):
                    sell_items.append({
                        "code": code, "name": name,
                        "quantity": total_qty, "avg": avg_price,
                        "current": avg_price, "net": 0,
                    })
            lines.append(f"  Σ 총 매수금  {total_cost:,}원")
        else:
            lines += ["", "📦 보유 포지션  없음"]

    # 최근 거래 한 줄 요약 (상세는 /history)
    recent = portfolio_service.get_trade_history(user.chat_id, days=7, limit=20)
    if recent:
        n_buy = len(recent)
        n_sell = sum(1 for t in recent if t["status"] == "closed")
        lines += [
            "",
            f"🧾 최근 7일 거래  {n_buy}건 매수 · {n_sell}건 매도  (/history 상세)",
        ]

    # 보유종목이 있으면 자동매도 도달 전이라도 바로 팔 수 있게 즉시매도 버튼 첨부
    reply_markup = None
    if sell_items:
        lines += ["", "ℹ 자동매도가 도달 전에 바로 팔려면 아래 버튼 (시장가 전량)"]
        keyboard = []
        for it in sell_items:
            intent = {
                "action": "sell_all",
                "code": it["code"], "name": it["name"],
                "kis_quantity": it["quantity"],
                "kis_avg": it["avg"], "kis_current": it["current"],
            }
            uuid = confirmation_service.create(user.chat_id, intent)
            head = f"{it['code']} {it['name']}" if it["name"] else it["code"]
            keyboard.append([InlineKeyboardButton(
                f"🔴 즉시매도  {head}  ·  {it['quantity']}주",
                callback_data=f"sellpick:{uuid}",
            )])
        reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("\n".join(lines), reply_markup=reply_markup)


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """매수·매도 히스토리 (최근 7일, 최대 20건)."""
    user = await _require_approved(update)
    if not user:
        return
    rows = portfolio_service.get_trade_history(user.chat_id, days=7, limit=20)
    if not rows:
        await update.message.reply_text("최근 7일 거래 없음")
        return

    lines = [
        f"🧾 거래 히스토리  ·  최근 7일  ·  {len(rows)}건",
        "ℹ 봇이 추적한 거래만 표시 (KIS 앱 직접 매수는 미포함)",
        "",
    ]
    for t in rows:
        head = f"{t['code']} {t['name']}" if t["name"] else t["code"]
        mode_label = "번트" if t["strategy_mode"] == "bunt" else "스퀴즈"
        opened = t["opened_at"][:16] if t["opened_at"] else "-"

        if t["status"] == "open":
            lines += [
                f"📌 보유중  {head}  [{mode_label}]",
                f"   {t['quantity']}주 @ {t['buy_price']:,}원  ·  {opened}",
                "",
            ]
        else:
            closed = t["closed_at"][:16] if t["closed_at"] else "-"
            pnl = t["pnl"] or 0
            ret = t["return_pct"] if t["return_pct"] is not None else 0
            icon = "✅" if pnl >= 0 else "🔴"
            sell_str = f"@ {t['sell_price']:,}" if t["sell_price"] else ""
            lines += [
                f"{icon} 청산  {head}  [{mode_label}]",
                f"   {t['quantity']}주  매수 @ {t['buy_price']:,}  →  매도 {sell_str}",
                f"   손익  {pnl:+,}원  ({ret:+.2f}%)",
                f"   매수 {opened}  →  매도 {closed}",
                "",
            ]

    # 합계
    closed_pnl = sum((t["pnl"] or 0) for t in rows if t["status"] == "closed")
    n_closed = sum(1 for t in rows if t["status"] == "closed")
    if n_closed > 0:
        lines += ["━━━━━━━━━━━━━━━━", f"청산 {n_closed}건 합계  {closed_pnl:+,}원"]

    await update.message.reply_text("\n".join(lines))


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """손익 현황 — 오늘 실현 + 보유 미실현(KIS 기준) + 누적 실현·승률."""
    from datetime import date as _date

    user = await _require_approved(update)
    if not user:
        return

    s = portfolio_service.get_pnl_summary(user.chat_id)
    today = _date.today().isoformat()

    lines = [
        f"📈 손익 현황  ·  {today}",
        "ℹ 봇이 추적한 거래 기준 (KIS 직접 매수는 [보유 미실현]만 반영)",
        "",
    ]

    # 오늘 청산 손익 (매수만 한 종목은 [보유 미실현]에 잡힘)
    if s["today_count"] > 0:
        lines += [
            "[오늘 청산 손익]",
            f"  거래       {s['today_count']}건",
            f"  손익       {s['today_realized']:+,}원",
            "",
        ]
    else:
        lines += ["[오늘 청산 손익]  청산한 거래 없음", ""]

    # 보유 미실현 (KIS 기준 — 평가손익이 가장 정확)
    broker_bal = await portfolio_service.get_broker_balance(config.TRADE_MODE.value)
    if broker_bal is not None and "error" not in broker_bal:
        positions = broker_bal.get("positions", [])
        if positions:
            lines += [
                "[보유 미실현]  (KIS 실계좌)",
                f"  종목        {len(positions)}종목",
                f"  평가 손익   {broker_bal['total_pnl']:+,}원"
                f"  ({broker_bal['total_pnl_pct']:+.2f}%)",
            ]
            for p in positions:
                lines.append(
                    f"   • {p['code']} {p['name']}  "
                    f"{p['pnl']:+,}원 ({p['pnl_pct']:+.2f}%)"
                )
                net, net_pct = portfolio_service.net_pnl_after_fees(
                    p["avg_price"], p["current_price"], p["quantity"],
                )
                icon = "⚠" if net < 0 else "💵"
                lines.append(
                    f"     {icon} 수수료 제외 시 {net:+,}원 ({net_pct:+.2f}%)"
                )
            lines.append("")
        else:
            lines += ["[보유 미실현]  보유 종목 없음", ""]
    elif broker_bal is not None and "error" in broker_bal:
        lines += [f"[보유 미실현]  ⚠ KIS 조회 실패: {broker_bal['error']}", ""]

    # 누적 청산 손익
    if s["total_count"] > 0:
        avg_str = f"{s['avg_pnl_per_trade']:+,}원/거래"
        lines += [
            "[누적 청산 손익]",
            f"  거래       {s['total_count']}건  ({s['win_count']}승 {s['loss_count']}패)",
            f"  승률       {s['win_rate_pct']}%",
            f"  누적       {s['total_realized']:+,}원",
            f"  평균       {avg_str}",
        ]
    else:
        lines.append("[누적 청산 손익]  청산한 거래 없음")

    await update.message.reply_text("\n".join(lines))


_recommend_locks: dict[int, asyncio.Lock] = {}


def _get_recommend_lock(chat_id: int) -> asyncio.Lock:
    lock = _recommend_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _recommend_locks[chat_id] = lock
    return lock


# /재추천 (cmd_rerecommend) 와 /refresh_all 둘 다 데이터 수집 → DB 갱신 → 발송 흐름.
# 동시 실행 시 중복 크롤·DB write 충돌 막기 위해 단일 글로벌 락 공유.
_global_rerecommend_lock = asyncio.Lock()

# 데이터 수집(skip_per_code=True 모드) 보수적 예상 — KOSPI+KOSDAQ ~2700종목 OHLCV
# 증분이 가장 무거움. 운영 환경 실측 후 조정하면 됨.
PIPELINE_ETA_SEC = 25 * 60
PROGRESS_INTERVAL_SEC = 60


async def _run_pipeline_with_progress(msg, run_pipeline_kwargs: dict) -> list:
    """run_pipeline 을 executor 에 던지고 1분마다 메시지 갱신.

    워치독 코루틴이 step_results 리스트를 1분마다 읽어 진행 상황 표시.
    on_step_done 콜백은 executor 스레드에서 호출되지만 list.append 는 GIL 보호로 안전.
    """
    from src.crawlers.collect_all import run_pipeline as _run_pipeline

    t0 = time.time()
    step_results: list = []

    async def _watchdog():
        while True:
            try:
                await asyncio.sleep(PROGRESS_INTERVAL_SEC)
            except asyncio.CancelledError:
                return
            elapsed = int(time.time() - t0)
            remaining = max(0, PIPELINE_ETA_SEC - elapsed)
            last_label = step_results[-1].label if step_results else "수집 시작 중"
            lines = [
                "🔄 데이터 수집 진행 중",
                f"⏱ 경과 {elapsed//60}분 {elapsed%60:02d}초"
                f" · 잔여 약 {remaining//60}분",
                f"📍 마지막 단계: {last_label}",
            ]
            if step_results:
                lines.append("━━━━━━━━━━━━━━━━━")
                lines.append("완료된 단계")
                for r in step_results:
                    icon = "✅" if r.ok else "❌"
                    lines.append(f"  {icon} {r.label} ({r.elapsed:.0f}s)")
            try:
                await msg.edit_text("\n".join(lines))
            except Exception:
                # rate limit · 동일 텍스트 등은 무시 — 다음 분에 재시도
                log.debug("워치독 edit_text 실패", exc_info=True)

    watchdog = asyncio.create_task(_watchdog())
    loop = asyncio.get_running_loop()
    try:
        kwargs = dict(run_pipeline_kwargs)
        kwargs["on_step_done"] = step_results.append
        results = await loop.run_in_executor(
            None, lambda: _run_pipeline(**kwargs),
        )
        return results
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except (asyncio.CancelledError, Exception):
            pass


async def cmd_recommend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from datetime import date as _date
    from src.bot.scheduler import (
        send_cached_recommendations_dual,
        send_recommendations_dual,
    )

    user = await _require_approved(update)
    if not user:
        return

    today = _date.today().isoformat()
    # per-chat 락 — 같은 사용자의 /추천 동시 요청이 fresh gen 두 번 트리거 안 되게
    async with _get_recommend_lock(user.chat_id):
        # 1) 오늘자 캐시 있으면 즉시 replay (양 모드 합산)
        cached_n = await send_cached_recommendations_dual(ctx.bot, user.chat_id, today)
        if cached_n > 0:
            return

        # 2) 캐시 없음 → 유니버스 로드 후 양 모드 신규 계산
        codes = _list_candidate_codes()
        if not codes:
            await update.message.reply_text(
                "분석 유니버스가 비어있습니다.\n"
                "먼저 `python -m src.universe.builder` 로 유니버스를 빌드하세요."
            )
            return

        await update.message.reply_text(
            f"⏳ 번트·스퀴즈 양 모드 {len(codes)}종목 평가 중... 처음 요청은 4~6분 소요됩니다."
        )

        n = await send_recommendations_dual(ctx.bot, user.chat_id, codes)
        if n == 0:
            await update.message.reply_text(
                "추천 종목 없음 (조건 충족 종목 부재 또는 데이터 부족).\n"
                "시드 한도 내 매수 가능 종목이 없을 수 있습니다."
            )


async def cmd_rerecommend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """승인 사용자 누구나 — 데이터 증분 수집 + 옛 캐시 정리 + force_fresh 재계산 + 본인 발송.

    /recommend 가 캐시 우선이라 "오늘 데이터로 다시 뽑은 게 맞나" 확인이 안 되는
    문제를 풀기 위함. 데이터 신선도(OHLCV 마지막 봉)를 진행/완료 메시지에 노출.
    """
    user = await _require_approved(update)
    if not user:
        return
    chat_id = user.chat_id

    from datetime import date as _date
    from src.bot.scheduler import _list_candidate_codes, send_recommendations_dual

    if _global_rerecommend_lock.locked():
        await update.message.reply_text(
            "⏳ 다른 재추천/갱신 작업 진행 중입니다. 끝나는 즉시 이어서 처리합니다."
        )

    async with _global_rerecommend_lock:
        msg = await update.message.reply_text(
            "🔄 데이터 증분 수집 시작 (OHLCV·재무·수급)\n"
            f"⏱ 예상 소요 약 {PIPELINE_ETA_SEC//60}분 (실시간 진행 상황 1분마다 갱신)"
        )

        try:
            results = await _run_pipeline_with_progress(msg, dict(
                first_time=False,
                years=0,
                codes_limit=None,
                skip_per_code=True,        # 뉴스/커뮤/유튜브 제외 → 빠름
                per_code_days=1,
                continue_on_error=True,
            ))
        except Exception as e:
            log.exception("rerecommend: 데이터 수집 실패")
            await msg.edit_text(f"❌ 데이터 수집 실패\n{e}")
            return

        n_ok = sum(1 for r in results if r.ok)
        failed = [r.label for r in results if not r.ok]
        fail_str = f"  실패: {', '.join(failed)}\n" if failed else ""

        # OHLCV 마지막 봉 + 액션 없는 옛 추천 정리
        today = _date.today().isoformat()
        conn = get_connection()
        try:
            row = conn.execute("SELECT MAX(date) FROM ohlcv_daily").fetchone()
            last_bar = row[0] if row and row[0] else "?"
            cur = conn.execute(
                """DELETE FROM recommendations
                   WHERE session_date = ?
                     AND rec_id NOT IN (SELECT rec_id FROM recommendation_actions)""",
                (today,),
            )
            conn.commit()
            deleted = cur.rowcount
        finally:
            conn.close()

        await msg.edit_text(
            f"✅ 1/2 데이터 수집 완료 ({n_ok}/{len(results)} 단계)\n"
            f"{fail_str}"
            f"🆕 OHLCV 마지막 봉  {last_bar}\n"
            f"🧹 옛 추천 {deleted}건 정리\n"
            f"🔄 2/2 추천 재계산·발송 중..."
        )

        codes = _list_candidate_codes()
        if not codes:
            await msg.edit_text(
                f"❌ 분석 유니버스 비어있음 — 발송 불가\n"
                f"  데이터: {n_ok}/{len(results)}"
                + (f"  ({fail_str.strip()})" if fail_str else "")
            )
            return

        try:
            sent = await send_recommendations_dual(
                ctx.bot, chat_id, codes, force_fresh=True,
            )
        except Exception:
            log.exception("rerecommend: 발송 실패 chat_id=%s", chat_id)
            await msg.edit_text("❌ 추천 발송 실패 — 로그 확인 필요")
            return

        summary = [
            "✅ 재추천 완료",
            f"  📥 데이터        {n_ok}/{len(results)} 단계"
            + (f"  (실패: {', '.join(failed)})" if failed else ""),
            f"  🆕 OHLCV 마지막  {last_bar}",
            f"  🧹 옛 추천       {deleted}건 정리",
            f"  📤 발송          {sent}건",
        ]
        if sent == 0:
            summary.append("  ℹ 조건 충족 종목 없음 — 시드 한도/점수 미달일 수 있음")
        await msg.edit_text("\n".join(summary))

        audit_service.log_event(chat_id, "user_rerecommend", {
            "data_ok": n_ok, "data_total": len(results),
            "data_failed": failed,
            "deleted_recs": deleted,
            "last_bar": last_bar,
            "sent": sent,
        })


# ============================================================
# 임의 종목 조회 (#2) — 추천 외 종목 코드/이름 입력 시 정보 정리
# ============================================================

_EXPERT_KR = {
    "technical": "기술", "fundamental": "재무", "flow": "흐름",
    "news": "뉴스", "minute": "분봉", "community": "커뮤", "youtube": "유튜브",
}


def _resolve_stock(query: str) -> tuple[str | None, str, list[tuple[str, str]]]:
    """입력을 (code, name, 후보목록) 으로 해석.
    - 6자리 숫자 → 코드로 직행 (instruments 에 없어도 시도)
    - 이름 → 정확매칭 우선, 없으면 부분매칭. 다수면 후보목록 반환(code=None)."""
    q = query.strip()
    conn = get_connection()
    try:
        if q.isdigit() and len(q) == 6:
            row = conn.execute(
                "SELECT code, name FROM instruments WHERE code=?", (q,)
            ).fetchone()
            return (row[0], row[1], []) if row else (q, "", [])
        exact = conn.execute(
            "SELECT code, name FROM instruments WHERE name=? LIMIT 6", (q,)
        ).fetchall()
        rows = exact if exact else conn.execute(
            "SELECT code, name FROM instruments WHERE name LIKE ? LIMIT 6", (f"%{q}%",)
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0], rows[0][1], []
        if len(rows) > 1:
            return None, "", [(r[0], r[1]) for r in rows]
        return None, "", []
    finally:
        conn.close()


def _evaluate_single(code: str) -> dict:
    """단일 종목 7-전문가 평가 + 모드별 앙상블 점수. 무겁다 → to_thread 로 호출."""
    from src.indicators import compute_all, load_ohlcv
    from src.ensemble.scorer import EnsembleScorer
    from src.experts.technical import TechnicalExpert
    from src.experts.fundamental import FundamentalExpert
    from src.experts.flow import FlowExpert
    from src.experts.news import NewsExpert
    from src.experts.minute import MinuteExpert
    from src.experts.community import CommunityExpert
    from src.experts.youtube import YoutubeExpert

    df = load_ohlcv(code)
    if df is None or df.empty or len(df) < 60:
        return {"ok": False, "error": "일봉 60일 미만 — 분석 불가 (데이터 부족)"}
    enriched = compute_all(df)
    last_close = int(df["close"].iloc[-1])

    # 전문가별 점수 — 하드필터와 무관하게 항상 전체 표시 (조회는 정보 제공이 목적)
    raw = {
        "technical": TechnicalExpert().evaluate(code, enriched),
        "fundamental": FundamentalExpert().evaluate(code),
        "flow": FlowExpert().evaluate(code),
        "news": NewsExpert().evaluate(code),
        "minute": MinuteExpert().evaluate(code),
        "community": CommunityExpert().evaluate(code),
        "youtube": YoutubeExpert().evaluate(code),
    }
    experts = {
        _EXPERT_KR[k]: (round(v.score) if getattr(v, "is_valid", False) else None)
        for k, v in raw.items()
    }
    deficit = [k for k, v in experts.items() if v is None]

    modes = {}
    for mode in ("bunt", "squeeze"):
        op = EnsembleScorer(mode=mode).evaluate(code, enriched)
        modes[mode] = {
            "score": op.ensemble_score,
            "filtered": op.filtered,
            "reason": op.filter_reason,
        }
    return {
        "ok": True, "last_close": last_close,
        "experts": experts, "deficit": deficit, "modes": modes,
    }


async def cmd_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    """추천 외 임의 종목 조회 — 풀 앙상블 + 뉴스 + 가상 TP/SL. 정보 전용(매수 버튼 없음)."""
    user = await _require_approved(update)
    if not user:
        return

    code, name, candidates = _resolve_stock(query)
    if candidates:
        lines = ["🔎 여러 종목이 검색됐어요 — 코드로 다시 입력:"]
        lines += [f"  · {n} ({c})" for c, n in candidates]
        await update.message.reply_text("\n".join(lines))
        return
    if not code:
        await update.message.reply_text(
            f"'{query}' 종목을 찾지 못했어요.\n"
            "종목코드 6자리(예: 005930) 또는 정확한 종목명을 입력하세요."
        )
        return

    notice = await update.message.reply_text(f"🔎 {name or code} ({code}) 분석 중…")
    res = await asyncio.to_thread(_evaluate_single, code)
    from src.bot.scheduler import _fetch_rec_meta
    meta = await _fetch_rec_meta(code)
    disp_name = meta.get("company_name") or name or code

    if not res["ok"]:
        await notice.edit_text(f"🔎 {disp_name} ({code})\n\n⚠ {res['error']}")
        return

    from src.risk.guard import SWING_MODE_PARAMS, StrategyMode, align_to_tick
    lc = res["last_close"]

    head = f"🔎 {disp_name} ({code})"
    if meta.get("sector"):
        head += f"  ·  {meta['sector']}"
    lines = [head]
    if meta.get("why_line"):
        lines.append(f"💡 왜 이 종목: {meta['why_line']}")
    if meta.get("fund_line"):
        lines.append(f"💰 {meta['fund_line']}")
    lines.append(f"📌 현재가  {lc:,}원")

    # 앙상블 점수 (모드별)
    lines += ["", "📊 앙상블 점수 (7전문가 가중합 · 미래수익 보장 아님)"]
    mode_icon = {"bunt": "🟢 번트", "squeeze": "🟠 스퀴즈"}
    filt_reason = ""
    for m in ("bunt", "squeeze"):
        d = res["modes"][m]
        tag = "  ⚠ 필터탈락" if d["filtered"] else ""
        lines.append(f"  {mode_icon[m]}  {d['score']:.1f}{tag}")
        if d["filtered"] and not filt_reason:
            filt_reason = d["reason"]
    if filt_reason:
        lines.append(f"  └ 탈락 사유: {filt_reason}")

    # 섀도우 캘리브레이션 — 이 점수대가 과거(현 레짐) 실제로 어땠는지 (픽 변경 아님, 정보)
    try:
        from src.services import measurement as _meas
        _conn = get_connection()
        try:
            _regime = _meas.current_regime(_conn)
            _cidx = _meas.calibration_index(_conn, _regime) if _regime else {}
        finally:
            _conn.close()
    except Exception:
        _cidx = {}
    if _cidx:
        _rk = {"up": "상승장", "side": "횡보장", "down": "급락장", "ALL": "전체"}
        shadow = []
        for m in ("bunt", "squeeze"):
            row = _cidx.get(_meas._bucket(res["modes"][m]["score"]))
            if not row:
                continue
            reg = _rk.get(row["regime"], row["regime"])
            flag = " ⚠반복" if row["uniq_codes"] * 2 < row["n"] else ""
            shadow.append(
                f"  {mode_icon[m]} 과거 {reg}·{row['bucket']}점대: "
                f"5일 {row['avg_ret_5d']:+.1f}% · 승률 {row['win_pct']:.0f}% · n{row['n']}{flag}"
            )
        if shadow:
            lines += ["", "📐 섀도우 실측 (같은 점수대 과거 성적 · 캘리브레이션 미적용 참고)"] + shadow

    # 전문가별
    ex = res["experts"]
    ex_parts = [f"{k} {v:.0f}" if v is not None else f"{k} ✗" for k, v in ex.items()]
    lines += ["", "🧩 전문가별: " + " · ".join(ex_parts)]

    # 가상 TP/SL (양 모드)
    lines += ["", f"💵 가상 자동매도 (현재가 {lc:,} 기준 · 주간스윙)"]
    for m in ("bunt", "squeeze"):
        p = SWING_MODE_PARAMS[StrategyMode(m)]
        tp = align_to_tick(lc * (100 + p["tp_pct"]) // 100, "down")
        sl = align_to_tick(lc * (100 - p["sl_pct"]) // 100, "down")
        lines.append(
            f"  {mode_icon[m]}  🎯 +{p['tp_pct']}% {tp:,}  /  🛑 -{p['sl_pct']}% {sl:,}"
        )

    if meta.get("trend_line"):
        lines += ["", f"📉 {meta['trend_line']}"]
    if meta.get("news_lines"):
        nh = f"📰 최근 뉴스 — {meta['news_summary']}" if meta.get("news_summary") else "📰 최근 뉴스"
        lines += ["", nh, meta["news_lines"]]

    if res["deficit"]:
        lines += ["", f"⚠ 데이터 결손: {' · '.join(res['deficit'])} 미수집 "
                       "(유니버스 밖 종목일 수 있음) — 기술 중심 참고용"]
    lines += ["", "ℹ 정보 조회 전용입니다. 매수는 /추천 의 실제 추천 종목으로만 진행하세요."]

    await notice.edit_text("\n".join(lines))


async def cmd_lookup_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/lookup <코드|종목명> — 임의 종목 조회 진입점."""
    q = " ".join(ctx.args or []).strip()
    if not q:
        await update.message.reply_text(
            "사용법: /lookup 005930  또는  /lookup 삼성전자\n"
            "한글로는 '조회 삼성전자' 또는 종목명·코드만 입력해도 됩니다."
        )
        return
    return await cmd_lookup(update, ctx, q)


async def cmd_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """보유 종목 중 하나를 골라 시장가 전량 매도. 확인 다이얼로그 거침."""
    user = await _require_approved(update)
    if not user:
        return

    is_kis_mode = config.TRADE_MODE in (
        config.TradeMode.KIS_MOCK, config.TradeMode.LIVE,
    )

    # 보유 종목 — KIS 모드는 KIS 기준, paper는 봇 DB
    items: list[dict] = []
    if is_kis_mode:
        broker_bal = await portfolio_service.get_broker_balance(config.TRADE_MODE.value)
        if broker_bal is None or "error" in broker_bal:
            err = broker_bal.get("error", "?") if broker_bal else "조회 실패"
            await update.message.reply_text(f"⚠ KIS 잔고 조회 실패: {err}")
            return
        for p in broker_bal.get("positions", []):
            net, _ = portfolio_service.net_pnl_after_fees(
                p["avg_price"], p["current_price"], p["quantity"],
            )
            items.append({
                "code": p["code"], "name": p.get("name", ""),
                "quantity": int(p["quantity"]),
                "avg": int(p["avg_price"]),
                "current": int(p["current_price"]),
                "net": int(net),
            })
    else:
        s = portfolio_service.get_account_summary(user.chat_id, config.SEED_KRW)
        # paper — 봇 DB 보유 합산
        from collections import defaultdict
        groups: dict[str, list] = defaultdict(list)
        for p in s["open_positions"]:
            groups[p.code].append(p)
        for code, poss in groups.items():
            qty = sum(pp.quantity for pp in poss)
            buy_total = sum(pp.quantity * pp.buy_price for pp in poss)
            avg = buy_total // qty if qty else 0
            items.append({
                "code": code, "name": poss[0].name,
                "quantity": qty, "avg": avg, "current": avg, "net": 0,
            })

    if not items:
        await update.message.reply_text("매도할 보유 종목 없음")
        return

    # 종목별 UUID 생성 + 인라인 키보드
    keyboard = []
    for it in items:
        intent = {
            "action": "sell_all",
            "code": it["code"],
            "name": it["name"],
            "kis_quantity": it["quantity"],
            "kis_avg": it["avg"],
            "kis_current": it["current"],
        }
        uuid = confirmation_service.create(user.chat_id, intent)
        net_icon = "⚠" if it["net"] < 0 else "💵"
        head = f"{it['code']} {it['name']}" if it["name"] else it["code"]
        label = f"{head}  ·  {it['quantity']}주  {net_icon} {it['net']:+,}원"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"sellpick:{uuid}")])
    keyboard.append([InlineKeyboardButton("취소", callback_data="cancel")])

    await update.message.reply_text(
        "🔴 매도할 종목 선택\nℹ 손익은 수수료/거래세 차감 실손익 기준",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_panic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await _require_approved(update)
    if not user:
        return
    results = await portfolio_service.liquidate_all(user.chat_id, config.SEED_KRW)
    if not results:
        await update.message.reply_text("청산할 포지션 없음")
        return
    total_pnl = sum(r.get("net_pnl", 0) for r in results if r.get("success"))
    n_pending = sum(1 for r in results if r.get("pending"))
    lines = [f"🚨 긴급청산 시도  ·  {len(results)}건", ""]
    for r in results:
        if r.get("success"):
            icon = "✅" if r["net_pnl"] >= 0 else "🔴"
            lines += [
                f"{icon} {r['code']}",
                f"   {r['qty']}주  @{r['exit_price']:,}원"
                f"  →  {r['net_pnl']:+,}원  ({r['return_pct']:+.2f}%)",
                "",
            ]
        elif r.get("pending"):
            lines += [
                f"⏳ {r.get('code','?')} 매도 주문 대기 (KIS 미체결)",
                f"   ODNO={r.get('broker_order_id','?')}",
                "",
            ]
        else:
            lines += [f"⚠ {r.get('code','?')} 실패: {r.get('reason', '')}", ""]
    lines += ["━━━━━━━━━━━━━━━━", f"체결 손익  {total_pnl:+,}원"]
    if n_pending:
        lines += [
            f"⏳ {n_pending}건 미체결 대기 — KIS 앱 [미체결] 확인 필요",
        ]
    lines += [""]
    lines += await _balance_footer_lines(user.chat_id)
    await update.message.reply_text("\n".join(lines))


# ============================================================
# Admin commands (TELEGRAM_ADMIN_CHAT_ID 한정)
# ============================================================

def _is_admin(chat_id: int) -> bool:
    return bool(config.TELEGRAM_ADMIN_CHAT_ID) and chat_id == config.TELEGRAM_ADMIN_CHAT_ID


async def cmd_refresh_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """어드민 — OHLCV/펀더/수급 증분 수집 후 추천 재계산·발송.

    인자 'all' 주면 승인된 모든 사용자에게 발송. 기본은 어드민 본인 chat_id 만.
    """
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        return  # 침묵 — 일반 사용자는 명령 자체를 모름

    args = ctx.args or []
    broadcast = bool(args) and args[0].lower() == "all"

    from src.bot.scheduler import (
        _get_approved_users,
        _list_candidate_codes,
        send_recommendations_dual,
    )

    if _global_rerecommend_lock.locked():
        await update.message.reply_text(
            "⏳ 다른 재추천/갱신 작업 진행 중입니다. 끝나는 즉시 이어서 처리합니다."
        )

    async with _global_rerecommend_lock:
        msg = await update.message.reply_text(
            "🔄 데이터 증분 수집 시작 (OHLCV·재무·수급)\n"
            f"⏱ 예상 소요 약 {PIPELINE_ETA_SEC//60}분 (실시간 진행 상황 1분마다 갱신)"
        )

        # 1) 데이터 증분 수집 — executor + 워치독으로 1분마다 진행 상황 갱신
        try:
            results = await _run_pipeline_with_progress(msg, dict(
                first_time=False,
                years=0,
                codes_limit=None,
                skip_per_code=True,        # 뉴스/커뮤/유튜브 제외 → 빠름
                per_code_days=1,
                continue_on_error=True,
            ))
        except Exception as e:
            log.exception("refresh_all: 데이터 수집 실패")
            await msg.edit_text(f"❌ 데이터 수집 실패\n{e}")
            return

        n_ok = sum(1 for r in results if r.ok)
        failed = [r.label for r in results if not r.ok]
        fail_str = f"  실패: {', '.join(failed)}\n" if failed else ""

        await msg.edit_text(
            f"✅ 1/2 데이터 수집 완료 ({n_ok}/{len(results)} 단계)\n"
            f"{fail_str}"
            f"🔄 2/2 추천 재계산·발송 중..."
        )

        # 2) 옛 추천 정리 (액션 없는 것만) + force_fresh 재계산
        from datetime import date as _date
        today = _date.today().isoformat()
        conn = get_connection()
        try:
            cur = conn.execute(
                """DELETE FROM recommendations
                   WHERE session_date = ?
                     AND rec_id NOT IN (SELECT rec_id FROM recommendation_actions)""",
                (today,),
            )
            conn.commit()
            deleted = cur.rowcount
        finally:
            conn.close()

        codes = _list_candidate_codes()
        if not codes:
            await msg.edit_text(
                f"❌ 분석 유니버스 비어있음 — 발송 불가\n"
                f"  데이터: {n_ok}/{len(results)}{('  ' + fail_str.strip()) if fail_str else ''}"
            )
            return

        if broadcast:
            users = _get_approved_users()
            target_chat_ids = [u.chat_id for u in users]
        else:
            target_chat_ids = [chat_id]

        sent_total = 0
        send_failed = 0
        for cid in target_chat_ids:
            try:
                n = await send_recommendations_dual(ctx.bot, cid, codes, force_fresh=True)
                sent_total += n
            except Exception:
                send_failed += 1
                log.exception("refresh_all: chat_id=%s 발송 실패", cid)

        scope = f"승인 사용자 {len(target_chat_ids)}명" if broadcast else "어드민 본인"
        summary = [
            "✅ 갱신 완료",
            f"  📥 데이터    {n_ok}/{len(results)} 단계" + (f"  (실패: {', '.join(failed)})" if failed else ""),
            f"  🧹 옛 추천   {deleted}건 정리",
            f"  📤 발송      {scope} → {sent_total}건",
        ]
        if send_failed:
            summary.append(f"  ⚠ 발송 실패  {send_failed}명")
        await msg.edit_text("\n".join(summary))

        audit_service.log_event(chat_id, "admin_refresh_all", {
            "broadcast": broadcast,
            "data_ok": n_ok, "data_total": len(results),
            "data_failed": failed,
            "deleted_recs": deleted,
            "sent": sent_total,
            "send_failed": send_failed,
        })


async def cmd_reconcile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """어드민 — KIS 잔고 ↔ 봇 DB positions 정합성 점검·동기화.

    인자 없음 = dry-run (차이만 출력)
    인자 'apply' = ghost close + orphan INSERT 적용
    """
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        return

    args = ctx.args or []
    apply = bool(args) and args[0].lower() == "apply"

    msg = await update.message.reply_text(
        f"🔍 reconcile {'(apply)' if apply else '(dry-run)'} 실행 중..."
    )

    from scripts.reconcile_positions import reconcile_chat
    try:
        r = await reconcile_chat(chat_id, apply=apply, grace_minutes=15)
    except Exception as e:
        log.exception("cmd_reconcile 실패")
        await msg.edit_text(f"❌ reconcile 실패: {e}")
        return

    if "error" in r:
        await msg.edit_text(f"❌ reconcile 실패: {r['error']}")
        return

    lines = [
        "🔍 KIS ↔ 봇 DB 정합성 점검",
        "",
        f"  봇 DB open  {r['db_open']}건",
        f"  KIS 보유    {r['kis_held']}건",
        "",
        f"  ✓ 정상 매칭        {r['matched']}",
        f"  ⚠ 수량/가 차이      {r['mismatched']}",
        f"  ⏳ 최근매수 보호    {r['grace_protected']}",
        f"  📤 매도 진행중       {r.get('pending_sell', 0)}",
        f"  👻 ghost (DB만)    {r['ghosts']}",
        f"  🆕 orphan (KIS만)  {r['orphans']}",
    ]
    if apply:
        lines += [
            "",
            f"  → ghost close    {r['closed']}건",
            f"  → orphan adopt   {r['adopted']}건",
        ]
    elif r["ghosts"] or r["orphans"]:
        lines += ["", "💡 적용하려면 `/reconcile apply`"]
    audit_service.log_event(chat_id, "admin_reconcile", {"apply": apply, **{k: v for k, v in r.items() if k != "chat_id"}})
    await msg.edit_text("\n".join(lines))


async def cmd_admin_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """어드민 — 운영 메트릭 대시보드.

    오늘/7일 통계 + 현재 상태. audit_log + broker_orders + positions 기반.
    """
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("❌ 어드민 전용 명령")
        return

    conn = get_connection()
    try:
        # ── 현재 상태 ───────────────────────────────────────────
        row = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open'"
        ).fetchone()
        n_open = int(row[0] or 0)

        row = conn.execute(
            "SELECT COUNT(*) FROM broker_orders WHERE side='buy' AND status='pending'"
        ).fetchone()
        n_buy_pending = int(row[0] or 0)

        row = conn.execute(
            "SELECT COUNT(*) FROM broker_orders WHERE side='sell' AND status='pending'"
        ).fetchone()
        n_sell_pending = int(row[0] or 0)

        # ── 오늘 카운터 ────────────────────────────────────────
        def _count_today(event_type: str) -> int:
            r = conn.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE event_type=? AND date(ts)=date('now','+9 hours')",
                (event_type,),
            ).fetchone()
            return int(r[0] or 0) if r else 0

        n_recommend     = _count_today("recommend_push")
        n_button_buy    = _count_today("button_buy")
        n_order_buy     = _count_today("order_buy")
        n_buy_filled    = _count_today("buy_pending_filled")
        n_guard_block   = _count_today("guard_block")
        n_pending_rec   = _count_today("pending_rec_alert")
        n_zombie        = _count_today("sell_zombie_cleaned")
        n_daily_report  = _count_today("daily_sell_report")
        n_kis_5xx       = _count_today("kis_5xx")

        # KIS 5xx 가장 자주 나는 종목 top 3 (오늘)
        kis5xx_top: list[tuple[str, int]] = []
        if n_kis_5xx > 0:
            rows = conn.execute(
                """SELECT payload_json FROM audit_log
                   WHERE event_type='kis_5xx' AND date(ts)=date('now','+9 hours')"""
            ).fetchall()
            counts: dict[str, int] = {}
            import json as _json
            for r in rows:
                try:
                    p = _json.loads(r[0] or "{}")
                    code = p.get("code") or p.get("endpoint") or "?"
                    counts[code] = counts.get(code, 0) + 1
                except Exception:
                    continue
            kis5xx_top = sorted(counts.items(), key=lambda x: -x[1])[:3]

        # 자동매도 (price_alert 중 auto_sell=True)
        rows = conn.execute(
            """SELECT payload_json FROM audit_log
               WHERE event_type='price_alert'
                 AND date(ts)=date('now','+9 hours')"""
        ).fetchall()
        n_tp_hit = n_sl_hit = 0
        import json as _json
        for r in rows:
            try:
                p = _json.loads(r[0] or "{}")
                if p.get("auto_sell") and p.get("sell_success"):
                    if p.get("type") == "tp_hit":
                        n_tp_hit += 1
                    elif p.get("type") == "sl_hit":
                        n_sl_hit += 1
            except Exception:
                continue

        # ── 7일 누적 ────────────────────────────────────────────
        row = conn.execute(
            """SELECT COUNT(*) FROM positions
               WHERE date(opened_at) >= date('now','-7 days')"""
        ).fetchone()
        n_buy_7d = int(row[0] or 0)

        row = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(pnl), 0)
               FROM positions WHERE status='closed' AND pnl IS NOT NULL
                 AND date(closed_at) >= date('now','-7 days')"""
        ).fetchone()
        n_closed_7d = int(row[0] or 0) if row else 0
        total_pnl_7d = int(row[1] or 0) if row else 0

        row = conn.execute(
            """SELECT COUNT(*) FROM positions
               WHERE status='closed' AND pnl > 0
                 AND date(closed_at) >= date('now','-7 days')"""
        ).fetchone()
        n_wins_7d = int(row[0] or 0)

        row = conn.execute(
            """SELECT COUNT(*) FROM audit_log
               WHERE event_type='sell_zombie_cleaned'
                 AND date(ts) >= date('now','-7 days')"""
        ).fetchone()
        n_zombie_7d = int(row[0] or 0)

        # ── 마지막 활동 ────────────────────────────────────────
        row = conn.execute(
            "SELECT datetime(ts) FROM audit_log WHERE event_type IN "
            "('order_buy','order_sell','buy_pending_filled','sell_pending_filled') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_trade = row[0] if row else "—"

        row = conn.execute(
            "SELECT datetime(ts) FROM audit_log WHERE event_type='recommend_push' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_rec = row[0] if row else "—"
    finally:
        conn.close()

    win_rate = (n_wins_7d / n_closed_7d * 100) if n_closed_7d else 0.0

    lines = [
        "📊 운영 메트릭",
        "",
        "━━━ 현재 상태 ━━━",
        f"  📦 보유 포지션 (봇 DB)   {n_open}종목",
        f"  ⏳ 매수 pending           {n_buy_pending}건",
        f"  ⏳ 매도 pending           {n_sell_pending}건",
        "",
        "━━━ 오늘 (KST) ━━━",
        f"  📡 추천 발송            {n_recommend}",
        f"  🛒 매수 클릭            {n_button_buy}건",
        f"  ✅ 매수 주문 등록       {n_order_buy}건",
        f"  💸 매수 체결 (예약)     {n_buy_filled}건",
        f"  🚫 RiskGuard 차단       {n_guard_block}건",
        f"  🎯 자동 익절 (TP)       {n_tp_hit}건",
        f"  🛑 자동 손절 (SL)       {n_sl_hit}건",
        f"  📈 진입가 도달 알림     {n_pending_rec}건",
        f"  🧹 좀비 자동 정리       {n_zombie}건",
        f"  📊 매도 리포트 발송     {n_daily_report}",
        f"  ⚠️ KIS 5xx 발생         {n_kis_5xx}건"
        + (
            "  → top: " + ", ".join(f"{c}({n})" for c, n in kis5xx_top)
            if kis5xx_top else ""
        ),
        "",
        "━━━ 7일 누적 ━━━",
        f"  매수      {n_buy_7d}건",
        f"  청산      {n_closed_7d}건  (P&L {total_pnl_7d:+,}원)",
        f"  승률      {n_wins_7d}/{n_closed_7d}  ({win_rate:.1f}%)",
        f"  좀비      {n_zombie_7d}건",
        "",
        "━━━ 마지막 활동 ━━━",
        f"  매매     {last_trade}",
        f"  추천     {last_rec}",
    ]
    await update.message.reply_text("\n".join(lines))


# ============================================================
# Callback (매수 / 건너뜀 / 태그 / 취소 버튼)
# ============================================================

_T = recommendation_service.TAG_LABEL_KR


def _skipped_tag_keyboard(rec_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"❓ {_T['low_trust']}",
                                 callback_data=f"rtag:s:{rec_id}:low_trust"),
            InlineKeyboardButton(f"💸 {_T['no_cash']}",
                                 callback_data=f"rtag:s:{rec_id}:no_cash"),
        ],
        [
            InlineKeyboardButton(f"⏰ {_T['missed_timing']}",
                                 callback_data=f"rtag:s:{rec_id}:missed_timing"),
            InlineKeyboardButton(f"· {_T['other']}",
                                 callback_data=f"rtag:s:{rec_id}:other"),
        ],
    ])


async def cb_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # 즉시 ack — Telegram query 만료(~15s) 방지. 이후 KIS 5xx retry 등으로 본 처리가
    # 늦어져도 query.edit_message_text 는 정상 작동.
    # BadRequest (이미 답변됐거나 만료) 는 핫스팟에서 무시 — 핸들러는 계속 진행.
    try:
        await query.answer()
    except Exception as e:
        log.warning("cb_button: query.answer 실패 — %s (계속 진행)", e)
    data = query.data
    chat_id = query.from_user.id

    if data == "cancel":
        await query.edit_message_text("❌ 취소됨")
        return

    # 매도 1단계: 종목 선택 → 확인 다이얼로그
    if data.startswith("sellpick:"):
        uuid = data[len("sellpick:"):]
        intent = confirmation_service.consume(uuid, chat_id)
        if not intent:
            await query.edit_message_text("⏱ 만료 또는 이미 처리됨")
            return
        # 새 UUID — 확인 단계용
        confirm_uuid = confirmation_service.create(chat_id, intent)
        net, net_pct = portfolio_service.net_pnl_after_fees(
            intent["kis_avg"], intent["kis_current"], intent["kis_quantity"],
        )
        head = f"{intent['code']} {intent['name']}" if intent["name"] else intent["code"]
        net_icon = "⚠" if net < 0 else "💵"
        msg = (
            f"⚠ 매도 확인  —  {head}\n\n"
            f"  수량         {intent['kis_quantity']}주\n"
            f"  현재가       {intent['kis_current']:,}원\n"
            f"  평균 매수가  {intent['kis_avg']:,}원\n"
            f"  {net_icon} 실손익      {net:+,}원  ({net_pct:+.2f}%)\n\n"
            f"시장가로 전량 매도합니다."
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("진짜 매도", callback_data=f"sellgo:{confirm_uuid}"),
            InlineKeyboardButton("취소", callback_data="cancel"),
        ]])
        await query.edit_message_text(msg, reply_markup=kb)
        return

    # 매도 2단계: 확인 → 실제 KIS 매도
    if data.startswith("sellgo:"):
        uuid = data[len("sellgo:"):]
        intent = confirmation_service.consume(uuid, chat_id)
        if not intent:
            await query.edit_message_text("⏱ 만료 또는 이미 처리됨")
            return
        # 즉시 "처리 중" 피드백
        try:
            await query.edit_message_text(
                f"🔄 매도 처리 중...  —  {intent['code']}\n"
                f"  KIS 응답 대기 (최대 ~15초)"
            )
        except Exception:
            pass
        result = await portfolio_service.execute_sell_all_by_code(
            chat_id, intent["code"],
        )
        head = f"{intent['code']} {intent['name']}" if intent["name"] else intent["code"]
        if result["success"]:
            partial_marker = "  ⚠ 부분 체결" if result.get("partial") else ""
            lines = [
                f"✅ 매도 완료  —  {head}{partial_marker}",
                "",
                f"  수량       {result['qty']}주",
                f"  체결가     {result['exit_price']:,}원",
                f"  순손익     {result['net_pnl']:+,}원  ({result['return_pct']:+.2f}%)",
                "",
            ]
            lines += await _balance_footer_lines(chat_id)
            audit_service.log_event(chat_id, "manual_sell", {
                "code": intent["code"], "qty": result["qty"],
                "price": result["exit_price"], "net_pnl": result["net_pnl"],
                "partial": result.get("partial", False),
            })
            await query.edit_message_text("\n".join(lines))
        elif result.get("pending"):
            lines = [
                f"⏳ 매도 주문 대기 중  —  {head}",
                "",
                result["reason"],
                "",
                "👉 KIS 앱 [미체결 주문] 에서 확인하세요.",
                "  • 체결되면 잔고에 자동 반영",
                "  • 봇은 같은 종목 추가 매도 시도 안 함",
                "  • 정합성 동기화는 /reconcile",
                "",
            ]
            lines += await _balance_footer_lines(chat_id)
            await query.edit_message_text("\n".join(lines))
        else:
            await query.edit_message_text(
                f"❌ 매도 실패  —  {head}\n{result['reason']}"
            )
        return

    if data.startswith("buy:"):
        conf_uuid = data[4:]
        intent = confirmation_service.consume(conf_uuid, chat_id)
        if not intent:
            # 실패 원인 구분 — 사용자에게 실제 상태 알려주기
            status = confirmation_service.get_status(conf_uuid, chat_id)
            if status["exists"] and status["consumed"] and status["intent"]:
                rec_id = status["intent"].get("rec_id")
                code = status["intent"].get("code", "?")
                action = recommendation_service.find_latest_bought_action(rec_id) if rec_id else None
                if action:
                    await query.edit_message_text(
                        f"✅ 이미 매수 완료\n"
                        f"{code}  {action['quantity']}주 @ {action['price']:,}원\n"
                        f"처리 시각: {action['acted_at']}\n"
                        f"/잔고 로 현재 상태 확인"
                    )
                else:
                    await query.edit_message_text(
                        f"⚠ 매수 시도 기록은 있으나 체결 실패 ({code})\n"
                        f"/잔고 로 KIS 실계좌 상태 확인하세요."
                    )
            elif status["exists"] and status["expired"]:
                await query.edit_message_text(
                    "⏱ 10분 TTL 만료 — 매수 안 됨\n"
                    "/추천 재입력으로 새 버튼을 받으세요 (캐시라 즉시 반환)."
                )
            else:
                await query.edit_message_text(
                    "❌ 알 수 없는 버튼 (이미 정리되었거나 만료됨)\n"
                    "/잔고 로 현재 상태 확인, 필요 시 /추천 재입력."
                )
            return

        user = user_service.get_user(chat_id)
        if not user:
            await query.edit_message_text("❌ 사용자 정보 없음")
            return

        audit_service.log_event(chat_id, "button_buy", intent)

        # UUID 소비 완료 — intent 를 user_data 에 보관하고 가격 선택 화면으로
        ctx.user_data["pending_buy"] = intent
        ctx.user_data.pop("awaiting_qty", None)
        ctx.user_data.pop("awaiting_buy_price", None)

        rec_price = intent["price"]
        code = intent["code"]

        # KIS 현재가 조회
        cur_price = 0
        try:
            from src.adapters.market_data_kis import KISMarketDataSource
            kis = KISMarketDataSource()
            cp_map = kis.fetch_current_prices([code])
            if code in cp_map:
                cur_price = cp_map[code].price
        except Exception as e:
            log.warning("[buy %s] 현재가 조회 실패: %s", code, e)

        # 5년치 OHLCV 활용 — 최근 60일 일봉 가져와 시그널 가격 계산
        from src.experts.trader import TraderExpert, OHLCVRow
        ohlcv_rows: list[OHLCVRow] = []
        try:
            conn = get_connection()
            try:
                rows = conn.execute(
                    """SELECT date, open, high, low, close, volume
                       FROM ohlcv_daily WHERE code=?
                       ORDER BY date DESC LIMIT 60""",
                    (code,),
                ).fetchall()
            finally:
                conn.close()
            # DESC 로 가져왔으니 ASC 순서로 뒤집어서 trader 에 전달
            ohlcv_rows = [
                OHLCVRow(date=r[0], open=int(r[1] or 0), high=int(r[2] or 0),
                         low=int(r[3] or 0), close=int(r[4] or 0),
                         volume=int(r[5] or 0))
                for r in reversed(rows)
            ]
        except Exception as e:
            log.warning("[buy %s] ohlcv 조회 실패: %s", code, e)

        trader_sug = TraderExpert.suggest_buy_price(cur_price, ohlcv=ohlcv_rows) if cur_price > 0 else None

        # 키보드는 5옵션 단순 유지 (시그널은 참고 정보로 메시지에만 표시).
        # 사유: 매수 시점 인지 부하 최소화 — PM 결정 (2026-04-29).
        price_options: dict = {"rec": rec_price}
        if cur_price > 0:
            price_options["cur"] = cur_price
        if trader_sug:
            if trader_sug.aggressive.price > 0:
                price_options["agg"] = trader_sug.aggressive.price
            if trader_sug.passive.price > 0:
                price_options["pas"] = trader_sug.passive.price
        ctx.user_data["price_options"] = price_options

        # 모드 라벨 + TP/SL 한 줄 (사용자가 어느 모드 매수 진행 중인지 명확히)
        from src.risk.guard import SWING_MODE_PARAMS, MODE_PARAMS, StrategyMode
        mode = intent.get("strategy_mode", "bunt")
        holding = _holding_mode_for(chat_id)
        mode_icon = "🟢" if mode == "bunt" else "🟠"
        mode_label = "번트" if mode == "bunt" else "스퀴즈"
        try:
            param_table = SWING_MODE_PARAMS if holding == "swing_week" else MODE_PARAMS
            params = param_table[StrategyMode(mode)]
            mode_tpsl = f"TP+{params['tp_pct']}%/SL-{params['sl_pct']}%"
        except Exception:
            mode_tpsl = ""

        from src.bot.scheduler import _mode_explanation
        lines = [
            f"🛒 {code}  매수 가격 선택",
            "",
            _mode_explanation(mode, holding),
            "",
            f"  📌 추천 진입가   {rec_price:,}원",
        ]
        if cur_price > 0:
            diff_pct = (cur_price - rec_price) / rec_price * 100 if rec_price > 0 else 0
            lines.append(f"  ⚡ KIS 현재가     {cur_price:,}원  ({diff_pct:+.2f}%)")
        else:
            lines.append("  ⚠ KIS 현재가 조회 실패 — 추천가/직접입력만 가능")
        if trader_sug:
            # 시그널 가격은 참고 정보로 1줄만 (버튼 X) — 직접 입력 시 참고 가능
            if trader_sug.signals:
                sig_parts = [f"{s.label.split()[-1] if ' ' in s.label else s.label} {s.price:,}"
                             for s in trader_sug.signals]
                lines.append(f"  📈 5년 시그널 참고  {' / '.join(sig_parts)}")
            lines += [
                "",
                f"  🎯 {trader_sug.aggressive.price:,}원  ({trader_sug.aggressive.reason})",
                f"  🐢 {trader_sug.passive.price:,}원  ({trader_sug.passive.reason})",
            ]
        lines += ["", "💡 가격을 선택하거나 직접 입력하세요. 지정가 주문 → 체결 시 알림."]
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=_buy_price_keyboard(price_options),
        )
        return

    if data.startswith("bprc:"):
        val = data[5:]
        intent = ctx.user_data.get("pending_buy")
        if not intent:
            await query.edit_message_text("⏱ 세션 만료 — /추천 으로 다시 시작하세요")
            return

        if val == "cancel":
            ctx.user_data.pop("pending_buy", None)
            ctx.user_data.pop("price_options", None)
            ctx.user_data.pop("awaiting_buy_price", None)
            ctx.user_data.pop("awaiting_qty", None)
            await query.edit_message_text("❌ 취소됨")
            return

        if val == "custom":
            ctx.user_data["awaiting_buy_price"] = True
            code = intent["code"]
            cur = (ctx.user_data.get("price_options") or {}).get("cur", 0)
            cur_line = f"  현재가 {cur:,}원\n" if cur else ""
            await query.edit_message_text(
                f"[{code}] 매수 가격을 숫자로 입력하세요 (원)\n"
                f"{cur_line}호가 단위로 자동 정렬됩니다."
            )
            return

        price_options = ctx.user_data.get("price_options") or {}
        raw = price_options.get(val)
        # 시그널 옵션은 (label, price) 튜플, 고정 옵션은 int
        if isinstance(raw, tuple):
            chosen_price = raw[1]
        else:
            chosen_price = raw
        if not chosen_price or chosen_price <= 0:
            await query.edit_message_text("❌ 가격 옵션 없음 — /추천 다시 시작")
            return

        # intent 의 price 갱신, 수량 입력 대기 모드로
        from src.risk.guard import PER_POSITION_CAP_PCT
        per_cap = config.SEED_KRW * PER_POSITION_CAP_PCT // 100
        max_qty = max(1, per_cap // chosen_price)
        intent["price"] = chosen_price
        intent["quantity"] = max_qty   # 디폴트 (사용자가 변경)
        ctx.user_data["pending_buy"] = intent
        ctx.user_data["awaiting_qty"] = True
        ctx.user_data["max_qty"] = max_qty

        code = intent["code"]
        mode = intent.get("strategy_mode", "bunt")
        mode_icon = "🟢" if mode == "bunt" else "🟠"
        mode_label = "번트" if mode == "bunt" else "스퀴즈"
        await query.edit_message_text(
            f"🛒 {code}  매수 수량 입력  {mode_icon} {mode_label}\n"
            f"\n"
            f"  📌 매수가     {chosen_price:,}원\n"
            f"  💰 시드 한도  최대 {max_qty}주  ({chosen_price * max_qty:,}원, 시드 30%)\n"
            f"\n"
            f"💡 원하는 수량을 숫자로 입력하세요 (예: 10)\n"
            f"   입력하면 미리보기 → 확정으로 매수됩니다."
        )
        return

    if data.startswith("qty:"):
        val = data[4:]
        intent = ctx.user_data.get("pending_buy")
        if not intent:
            await query.edit_message_text("⏱ 세션 만료 — /추천 으로 다시 시작하세요")
            return

        if val == "cancel":
            ctx.user_data.pop("pending_buy", None)
            ctx.user_data.pop("awaiting_qty", None)
            await query.edit_message_text("❌ 취소됨")
            return

        if val == "custom":
            ctx.user_data["awaiting_qty"] = True
            price = intent["price"]
            code = intent["code"]
            await query.edit_message_text(
                f"[{code}] 매수 수량을 숫자로 입력하세요\n"
                f"주가 {price:,}원"
            )
            return

        try:
            qty = int(val)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await query.edit_message_text("❌ 수량 값 오류")
            return

        ctx.user_data.pop("pending_buy", None)
        ctx.user_data.pop("price_options", None)
        await _execute_buy_intent(query.edit_message_text, chat_id, intent, qty, ctx)
        return

    if data.startswith("bgo:"):
        action = data[4:]
        intent = ctx.user_data.get("pending_buy")
        if not intent:
            await query.edit_message_text("⏱ 세션 만료 — /추천 으로 다시 시작하세요")
            return

        if action == "cancel":
            ctx.user_data.pop("pending_buy", None)
            ctx.user_data.pop("price_options", None)
            ctx.user_data.pop("max_qty", None)
            await query.edit_message_text("❌ 취소됨")
            return

        if action == "requantity":
            ctx.user_data["awaiting_qty"] = True
            max_qty = ctx.user_data.get("max_qty", 0)
            await query.edit_message_text(
                f"✏️ 수량을 다시 입력하세요 (최대 {max_qty}주)\n"
                f"숫자로 응답 (예: 10)"
            )
            return

        if action == "confirm":
            qty = int(intent.get("quantity") or 0)
            if qty <= 0:
                await query.edit_message_text("❌ 수량 오류 — /추천 다시 시작")
                return
            ctx.user_data.pop("pending_buy", None)
            ctx.user_data.pop("price_options", None)
            ctx.user_data.pop("max_qty", None)
            await _execute_buy_intent(query.edit_message_text, chat_id, intent, qty, ctx)
            return

        await query.edit_message_text("❌ 알 수 없는 액션")
        return

    if data.startswith("bwait:"):
        # 매수 미체결 5분 알림에서 "계속 대기" 누름 — no-op
        broker_order_id = data[len("bwait:"):]
        await query.edit_message_text(
            f"⏳ 매수 주문 계속 대기 중\n  ODNO {broker_order_id}\n\n체결되거나 취소될 때까지 봇이 polling 합니다."
        )
        audit_service.log_event(chat_id, "buy_pending_wait", {"broker_order_id": broker_order_id})
        return

    if data.startswith("bcncl:"):
        # 매수 미체결 5분 알림에서 "주문 취소" 누름 — KIS cancel + broker_orders status 업데이트
        broker_order_id = data[len("bcncl:"):]
        from src.services.portfolio_service import get_broker
        from datetime import datetime as _dt

        adapter = get_broker(config.TRADE_MODE.value)
        try:
            ok = await adapter.cancel_order(broker_order_id)
        except Exception as e:
            ok = False
            log.warning("[bcncl] %s cancel 실패: %s", broker_order_id, e)
        now_iso = _dt.now().isoformat(timespec="seconds")
        new_status = "cancelled" if ok else "cancel_failed"
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE broker_orders SET status=?, updated_at=? WHERE broker_order_id=? AND status='pending'",
                (new_status, now_iso, broker_order_id),
            )
            conn.commit()
        finally:
            conn.close()
        audit_service.log_event(chat_id, "buy_pending_cancel", {
            "broker_order_id": broker_order_id, "kis_cancel_ok": ok,
        })
        if ok:
            await query.edit_message_text(
                f"❌ 매수 주문 취소됨\n  ODNO {broker_order_id}\n\n봇 DB 매수 미생성 (체결 안 됨)."
            )
        else:
            await query.edit_message_text(
                f"⚠ KIS 취소 실패\n  ODNO {broker_order_id}\n\nKIS 앱에서 직접 취소하세요. 봇은 더 이상 알림 안 보냄."
            )
        return

    if data.startswith("skip:"):
        conf_uuid = data[5:]
        intent = confirmation_service.consume(conf_uuid, chat_id)
        if not intent:
            await query.edit_message_text("⏱ 만료 또는 이미 처리됨")
            return
        # cross 종목은 rec_ids 리스트, 단일 모드는 rec_id 단일.
        rec_ids = intent.get("rec_ids") or ([intent["rec_id"]] if intent.get("rec_id") else [])
        if not rec_ids:
            await query.edit_message_text("⏭ 건너뜀 (기록 불가: rec_id 없음)")
            return
        audit_service.log_event(chat_id, "button_skip", intent)
        # 사유 태그 키보드는 첫 번째 rec_id 기준 (사유 태그는 rec_id별로 동일하게 적용됨)
        primary_rec_id = rec_ids[0]
        head = " · ".join(rec_ids)
        await query.edit_message_text(
            f"⏭ [{head}] 건너뜀\n건너뛴 사유를 선택하세요:",
            reply_markup=_skipped_tag_keyboard(primary_rec_id),
        )
        # cross 종목 보조 rec_id 들도 즉시 skipped 로 마킹 (사유는 primary 만 받음)
        for extra_rec_id in rec_ids[1:]:
            try:
                recommendation_service.insert_action(
                    rec_id=extra_rec_id, chat_id=chat_id,
                    action_type="skipped", reason_tag="other",
                )
            except Exception:
                pass
        return

    if data.startswith("rtag:"):
        # rtag:b:<action_id>:<tag>  |  rtag:s:<rec_id>:<tag>
        parts = data.split(":", 3)
        if len(parts) != 4:
            await query.edit_message_text("알 수 없는 태그 데이터")
            return
        _, kind, target, tag = parts
        label = _T.get(tag, tag)

        if kind == "s":
            try:
                recommendation_service.insert_action(
                    rec_id=target,
                    chat_id=chat_id,
                    action_type="skipped",
                    reason_tag=tag,
                )
            except ValueError as e:
                await query.edit_message_text(f"❌ 태그 오류: {e}")
                return
            audit_service.log_event(chat_id, "reason_tag_skipped",
                                    {"rec_id": target, "tag": tag})
            await query.edit_message_text(
                f"⏭ [{target}] 건너뜀\n📝 사유: {label}"
            )
            return

        if kind == "d":
            try:
                recommendation_service.insert_action(
                    rec_id=target,
                    chat_id=chat_id,
                    action_type="sold",
                    reason_tag=tag,
                )
            except ValueError as e:
                await query.edit_message_text(f"❌ 태그 오류: {e}")
                return
            audit_service.log_event(chat_id, "reason_tag_sold",
                                    {"rec_id": target, "tag": tag})
            await query.edit_message_text(
                f"🔴 [{target}] 매도\n📝 사유: {label}"
            )
            return

    await query.edit_message_text("알 수 없는 버튼")


# ============================================================
# Entry point
# ============================================================

def build_app() -> Application:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 미설정 (.env)")
    if not config.TELEGRAM_INVITE_CODE:
        raise RuntimeError("TELEGRAM_INVITE_CODE 미설정 (.env)")

    init_schema()

    async def _post_init(application):
        """봇 부팅 후 1회 — BotFather 자동완성 메뉴를 코드 기준으로 동기화."""
        await application.bot.set_my_commands([
            BotCommand("start",     "초대코드로 등록"),
            BotCommand("recommend",   "오늘의 추천 (번트/스퀴즈)"),
            BotCommand("rerecommend", "데이터 최신화 + 재추천 (3-5분)"),
            BotCommand("lookup",      "임의 종목 정보 조회 (코드/이름)"),
            BotCommand("balance",     "보유 포지션 + 현금"),
            BotCommand("pnl",       "손익 (오늘·미실현·누적·승률)"),
            BotCommand("history",   "거래 히스토리 (최근 7일)"),
            BotCommand("sell",      "특정 종목 시장가 전량 매도"),
            BotCommand("mode",      "전략 모드 전환 (bunt/squeeze)"),
            BotCommand("holding",   "보유 모드 전환 (day/swing)"),
            BotCommand("early",     "조기 익절 토글 (on/off)"),
            BotCommand("strategy",  "내 전략 통합 조회"),
            BotCommand("panic",     "전 포지션 즉시 청산"),
            BotCommand("setpin",    "PIN 설정 (6자리)"),
            BotCommand("help",      "도움말 / 명령어 목록"),
        ])
        log.info("BotFather 명령어 메뉴 동기화 완료")

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .concurrent_updates(True)   # 콜백 queue 처리 → query 만료 방지 (2026-05-04 사고 대응)
        .build()
    )

    # 영문 명령어
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setpin", cmd_setpin))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("holding", cmd_holding))
    app.add_handler(CommandHandler("early", cmd_early))
    app.add_handler(CommandHandler("strategy", cmd_strategy))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("recommend", cmd_recommend))
    app.add_handler(CommandHandler("rerecommend", cmd_rerecommend))
    app.add_handler(CommandHandler("lookup", cmd_lookup_entry))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("panic", cmd_panic))
    app.add_handler(CommandHandler("refresh_all", cmd_refresh_all))
    app.add_handler(CommandHandler("reconcile", cmd_reconcile))
    app.add_handler(CommandHandler("admin_stats", cmd_admin_stats))
    app.add_handler(CallbackQueryHandler(cb_button))

    # 한글 메시지 핸들러 — "추천", "잔고", "도움" 등 한글로 입력 가능
    async def _korean_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()

        # 매수 가격 직접 입력 대기 중인 경우 우선 처리
        if ctx.user_data.get("awaiting_buy_price"):
            intent = ctx.user_data.get("pending_buy")
            if intent:
                try:
                    price = int(text.replace(",", "").strip())
                    if price <= 0:
                        raise ValueError
                except ValueError:
                    await update.message.reply_text("숫자로 입력해주세요 (예: 60000)")
                    return
                # 호가 단위 정렬 (매수는 up — 즉시 체결 가능성 우선)
                from src.risk.guard import align_to_tick, PER_POSITION_CAP_PCT
                aligned = align_to_tick(price, "up")
                per_cap = config.SEED_KRW * PER_POSITION_CAP_PCT // 100
                new_qty = max(1, per_cap // aligned)
                intent["price"] = aligned
                intent["quantity"] = new_qty
                ctx.user_data["pending_buy"] = intent
                ctx.user_data.pop("awaiting_buy_price", None)
                code = intent["code"]
                aligned_note = f"  (호가단위 정렬: {price:,} → {aligned:,})" if aligned != price else ""
                await update.message.reply_text(
                    f"🛒 {code}  매수 수량 선택\n"
                    f"\n"
                    f"  주가       {aligned:,}원{aligned_note}\n"
                    f"  추천 수량  {new_qty}주  ({aligned * new_qty:,}원)\n"
                    f"\n"
                    f"수량을 선택하거나 직접 입력하세요.",
                    reply_markup=_qty_keyboard(code, aligned, new_qty),
                )
                return

        # 수량 직접 입력 대기 중인 경우 우선 처리 → 미리보기 화면으로
        if ctx.user_data.get("awaiting_qty"):
            intent = ctx.user_data.get("pending_buy")
            if intent:
                try:
                    qty = int(text.replace(",", "").strip())
                    if qty <= 0:
                        raise ValueError
                except ValueError:
                    await update.message.reply_text("숫자로 입력해주세요 (예: 10)")
                    return
                max_qty = ctx.user_data.get("max_qty", 0)
                if max_qty and qty > max_qty:
                    await update.message.reply_text(
                        f"⚠ 시드 한도 초과 (최대 {max_qty}주)\n"
                        f"한도 내에서 다시 입력하세요."
                    )
                    return
                # 미리보기 단계 — intent.quantity 갱신 + 키보드 발송
                intent["quantity"] = qty
                ctx.user_data["pending_buy"] = intent
                ctx.user_data.pop("awaiting_qty", None)
                await _send_buy_preview(
                    update.message.reply_text, intent, update.effective_chat.id,
                )
                return

        # /추천 처럼 슬래시 + 한글은 Telegram이 정식 command 로 인식 못 하므로
        # 일반 텍스트로 들어온다. 선두 슬래시를 벗겨 별칭 매칭에 태운다.
        normalized = text.lstrip("/").strip()

        if normalized in ("추천", "종목추천", "오늘추천"):
            return await cmd_recommend(update, ctx)
        elif normalized in ("잔고", "내잔고", "포지션"):
            return await cmd_balance(update, ctx)
        elif normalized in ("손익", "수익", "수익률"):
            return await cmd_pnl(update, ctx)
        elif normalized in ("히스토리", "거래내역", "거래", "이력"):
            return await cmd_history(update, ctx)
        elif normalized in ("도움", "도움말", "명령어"):
            return await cmd_help(update, ctx)
        elif normalized in ("긴급청산", "전체매도", "패닉"):
            return await cmd_panic(update, ctx)
        elif normalized in ("매도", "팔기", "종목매도"):
            return await cmd_sell(update, ctx)
        elif normalized == "재추천":
            # 일반 사용자도 호출 가능 — 본인에게만 발송
            return await cmd_rerecommend(update, ctx)
        elif normalized in ("갱신", "전체갱신", "데이터갱신"):
            # 어드민 한정 — 일반 사용자는 cmd_refresh_all 내부에서 침묵 반환
            if normalized == "전체갱신":
                ctx.args = ["all"]
            return await cmd_refresh_all(update, ctx)
        elif normalized in ("정합성", "리컨실"):
            return await cmd_reconcile(update, ctx)
        elif normalized in ("정합성적용", "리컨실적용"):
            ctx.args = ["apply"]
            return await cmd_reconcile(update, ctx)
        elif normalized in ("운영", "운영현황", "메트릭", "통계"):
            return await cmd_admin_stats(update, ctx)
        elif normalized.startswith("모드 ") or normalized.startswith("모드변경 "):
            # "모드 번트" → "/mode bunt" 로 변환
            update.message.text = "/mode " + normalized.split()[-1]
            return await cmd_mode(update, ctx)
        elif normalized in ("보유", "보유모드"):
            return await cmd_holding(update, ctx)
        elif normalized.startswith("보유 ") or normalized.startswith("보유모드 "):
            # "보유 당일" → "/holding day" / "보유 스윙" → "/holding swing"
            ctx.args = [normalized.split()[-1]]
            return await cmd_holding(update, ctx)
        elif normalized in ("조기익절", "빠른익절"):
            return await cmd_early(update, ctx)
        elif normalized.startswith("조기익절 ") or normalized.startswith("빠른익절 "):
            ctx.args = [normalized.split()[-1]]
            return await cmd_early(update, ctx)
        elif normalized in ("내전략", "전략", "설정"):
            return await cmd_strategy(update, ctx)
        elif normalized.startswith(("조회 ", "종목 ", "검색 ")):
            return await cmd_lookup(update, ctx, normalized.split(" ", 1)[1].strip())

        # catch-all — 단일 토큰(종목코드/이름)으로 보이면 임의 종목 조회 (#2)
        q = normalized.strip()
        if q and " " not in q and 2 <= len(q) <= 12:
            return await cmd_lookup(update, ctx, q)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _korean_router))
    app.add_error_handler(_error_handler)
    register_jobs(app)
    return app


async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """미처리 예외 글로벌 캐치 — 사용자 fail-silent 방지.

    어떤 핸들러에서 예외가 나도:
      1) admin chat 으로 traceback 통보 (문제 인지)
      2) 가능하면 사용자에게 "처리 실패 — /잔고 로 확인" 안내 (행동 지침)
    """
    import traceback
    log.exception("unhandled exception in handler", exc_info=ctx.error)

    # admin 알림
    if config.TELEGRAM_ADMIN_CHAT_ID:
        try:
            tb = "".join(traceback.format_exception(type(ctx.error), ctx.error, ctx.error.__traceback__))[-1500:]
            await ctx.bot.send_message(
                config.TELEGRAM_ADMIN_CHAT_ID,
                f"⚠ 봇 미처리 예외\n\n```\n{tb}\n```",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    # 사용자 안내 — update 가 callback_query / message 인 경우만
    try:
        from telegram import Update as _Update
        if not isinstance(update, _Update):
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not chat_id:
            return
        msg = (
            "⚠ 처리 중 오류가 발생했어요.\n"
            "  • 매수/매도였다면 KIS 앱에서 실제 체결 여부 확인\n"
            "  • /잔고 로 현재 상태 확인\n"
            "  • 필요 시 /reconcile 로 봇 DB 동기화"
        )
        await ctx.bot.send_message(chat_id, msg)
    except Exception:
        pass


def main() -> None:
    app = build_app()
    log.info("Bot starting · mode=%s · seed=%s",
             config.TRADE_MODE.value, f"{config.SEED_KRW:,}")
    app.run_polling()


if __name__ == "__main__":
    main()
