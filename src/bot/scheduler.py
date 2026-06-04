"""스케줄러 — JobQueue 기반 자동 발송.

국내 주식 주간스윙 전략:
  월~금  07:30  데이터 최신화
  월~금  08:00  추천 발송 (approved 전체) — 추천 계산 ~24분 → 08:24 도착, 장 36분 전
  월~금  09:05~15:15  매 3분 가격 모니터 (TP/SL 도달 시 알림)
  월~금  09:00  장 시작 시 모니터 알림 이력 리셋
  금     15:20  미청산 포지션 강제 청산 리마인더
  금     15:40  주간 회고 리포트
"""
from __future__ import annotations

import logging
from datetime import datetime, time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes

from src import config
from src.db.connection import get_connection
from src.ensemble.recommender import recommend
from src.services import (
    audit_service,
    confirmation_service,
    portfolio_service,
    recommendation_service,
    user_service,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
log = logging.getLogger("bunting.scheduler")

_T = recommendation_service.TAG_LABEL_KR


# ============================================================
# helpers
# ============================================================

def _list_candidate_codes(top_n: int | None = None) -> list[str]:
    """분석 유니버스(analysis_universe) 기준 후보 코드 반환.

    2026-04-22 B위원회 결정: 크롤링·스케줄러·추천이 동일 유니버스 사용.
    유니버스가 비어있으면 경고 로그 + 빈 리스트 반환.
    """
    from src.universe.builder import get_universe_codes
    codes = get_universe_codes(order_by="rank")
    if not codes:
        log.warning(
            "analysis_universe 비어있음 — 유니버스 빌드 필요 "
            "(python -m src.universe.builder)"
        )
        return []
    return codes[:top_n] if top_n else codes


def _get_approved_users() -> list[user_service.BotUser]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT chat_id, status, pin_hash, trade_mode, strategy_mode, holding_mode, "
            "early_take_profit, registered_at, approved_at "
            "FROM bot_users WHERE status='approved'"
        ).fetchall()
        return [user_service.BotUser(*r) for r in rows]
    finally:
        conn.close()


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


def _sell_tag_keyboard(rec_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🎯 {_T['target_hit']}",
                                 callback_data=f"rtag:d:{rec_id}:target_hit"),
            InlineKeyboardButton(f"🛑 {_T['stop_hit']}",
                                 callback_data=f"rtag:d:{rec_id}:stop_hit"),
        ],
        [
            InlineKeyboardButton(f"⏰ {_T['eod_forced']}",
                                 callback_data=f"rtag:d:{rec_id}:eod_forced"),
            InlineKeyboardButton(f"⚡ {_T['impulsive']}",
                                 callback_data=f"rtag:d:{rec_id}:impulsive"),
        ],
        [
            InlineKeyboardButton(f"📰 {_T['news_change']}",
                                 callback_data=f"rtag:d:{rec_id}:news_change"),
            InlineKeyboardButton(f"· {_T['other']}",
                                 callback_data=f"rtag:d:{rec_id}:other"),
        ],
    ])


_MODE_LABEL_KR = {"bunt": "번트", "squeeze": "스퀴즈"}


# ============================================================
# 거래일 체크
# ============================================================

from functools import lru_cache


@lru_cache(maxsize=10)
def _is_trading_day_cached(iso_date: str) -> bool:
    """KRX 거래일 여부 (날짜별 캐시).

    holidays.KR 공휴일 + 12/31(연말 폐장) → 휴장. 그 외 평일은 거래일로 간주.
    pykrx 의존 제거: KRX HTTP 응답이 빈/깨진 응답이어도 pykrx 가 빈 DataFrame 을
    던져버려 거래일을 휴장일로 오판하던 사고 방지 (2026-05-07 morning_recommend 누락).
    """
    from datetime import date as _date
    d = _date.fromisoformat(iso_date)
    if d.month == 12 and d.day == 31:
        return False
    try:
        import holidays
        if d in holidays.KR(years=d.year):
            return False
    except ImportError:
        log.warning("holidays 미설치 — 한국 공휴일 미체크 (평일은 거래일로 간주)")
    return True


# 장 운영 시간 — KRX 정규장 09:00~15:30 KST
_MARKET_OPEN = time(9, 0)
_MARKET_CLOSE = time(15, 30)


def is_kr_trading_day() -> bool:
    """오늘이 KRX 거래일(공휴일·주말 아님)이면 True.

    주의: 시간(장 운영 시간)은 보지 않음. 장중 가드가 필요하면
    is_kr_market_session_now() 사용.
    """
    now = datetime.now(KST)
    if now.weekday() > 4:
        return False
    return _is_trading_day_cached(now.date().isoformat())


def is_kr_market_session_now() -> bool:
    """지금 이 순간이 KRX 정규장 운영 중(거래일 + 09:00~15:30 KST)이면 True.

    실시간 잡(price_monitor 등)은 반드시 이걸로 가드해야 함.
    is_kr_trading_day() 만 쓰면 새벽/장전에도 통과해버림.
    """
    now = datetime.now(KST)
    if now.weekday() > 4:
        return False
    if not _is_trading_day_cached(now.date().isoformat()):
        return False
    return _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


def _params_for(mode: str, holding_mode: str = "swing_week") -> dict:
    """전략 모드(bunt/squeeze) × 보유 모드(day/swing_week) → tp_pct/sl_pct."""
    from src.risk.guard import MODE_PARAMS, SWING_MODE_PARAMS, StrategyMode
    table = SWING_MODE_PARAMS if holding_mode == "swing_week" else MODE_PARAMS
    return table[StrategyMode(mode)]


def _ratio(p: dict) -> float:
    return round(p["tp_pct"] / p["sl_pct"], 2) if p["sl_pct"] else 0.0


def _mode_explanation(mode: str, holding_mode: str = "swing_week") -> str:
    """모드 한 줄 설명 — 매수/매도 메시지 공통."""
    if mode not in ("bunt", "squeeze"):
        return ""
    p = _params_for(mode, holding_mode)
    icon, label, hint = (
        ("🟢", "번트 (안정형)", "작은 수익 빠르게")
        if mode == "bunt"
        else ("🟠", "스퀴즈 (공격형)", "강한 모멘텀 멀리 잡음")
    )
    suffix = " · 당일" if holding_mode == "day" else ""
    return (
        f"{icon} {label}{suffix} — {hint}. "
        f"+{p['tp_pct']}% 익절 / -{p['sl_pct']}% 손절  (손익비 {_ratio(p)}:1)"
    )


def _mode_explanation_block(mode: str, holding_mode: str = "swing_week") -> list[str]:
    """매수 미리보기용 — 여러 줄 자세히."""
    if mode not in ("bunt", "squeeze"):
        return []
    p = _params_for(mode, holding_mode)
    suffix = " · 당일매매" if holding_mode == "day" else ""
    if mode == "bunt":
        return [
            f"🟢 번트 모드 (안정형){suffix}",
            "  • 안정적 종목을 짧게 잡아 작은 수익. 손절도 빠르게.",
            f"  • +{p['tp_pct']}% 익절 / -{p['sl_pct']}% 손절  (손익비 {_ratio(p)}:1)",
            "  • 추천 가중치: 재무·차트 중시 (펀더 받쳐주는 종목 선호)",
        ]
    return [
        f"🟠 스퀴즈 모드 (공격형){suffix}",
        "  • 강한 모멘텀 종목을 더 멀리 잡음. 변동성 감수.",
        f"  • +{p['tp_pct']}% 익절 / -{p['sl_pct']}% 손절  (손익비 {_ratio(p)}:1)",
        "  • 추천 가중치: 차트·분봉 모멘텀 중시 (재무는 덜 중요)",
    ]


def _mode_header_text(strategy_mode: str) -> str:
    """모드별 헤더 메시지 (TP/SL 룰 포함)."""
    from src.risk.guard import SWING_MODE_PARAMS, StrategyMode
    params = SWING_MODE_PARAMS[StrategyMode(strategy_mode)]
    icon = "🟢" if strategy_mode == "bunt" else "🟠"
    label = _MODE_LABEL_KR.get(strategy_mode, strategy_mode)
    return (
        "━━━━━━━━━━━━━━━━\n"
        f"{icon} {label} 모드  (TP +{params['tp_pct']}% / SL -{params['sl_pct']}%)\n"
        "━━━━━━━━━━━━━━━━"
    )


async def _fetch_rec_meta(code: str) -> dict:
    """종목 메타 (회사명·섹터·5일 트렌드·뉴스·시총) 한 번에 조회.
    _send_unified_rec / _send_single_rec 공통 사용."""
    conn = get_connection()
    try:
        inst_row = conn.execute(
            "SELECT name, sector FROM instruments WHERE code=?", (code,)
        ).fetchone()
        company_name = inst_row[0] if inst_row else code
        sector = inst_row[1] if inst_row and inst_row[1] else ""

        trend_rows = conn.execute(
            """SELECT date, close FROM ohlcv_daily
               WHERE code=? ORDER BY date DESC LIMIT 5""",
            (code,),
        ).fetchall()
        if len(trend_rows) >= 2:
            prices = [row[1] for row in reversed(trend_rows)]
            week_change = (prices[-1] - prices[0]) / prices[0] * 100
            trend_str = " → ".join(f"{p:,}" for p in prices)
            trend_line = f"최근 5일: {trend_str} ({week_change:+.1f}%)"
        else:
            trend_line = ""

        news_rows = conn.execute(
            """SELECT title FROM news_article
               WHERE code=? ORDER BY published_at DESC LIMIT 3""",
            (code,),
        ).fetchall()
        news_lines = ""
        if news_rows:
            news_lines = "\n".join(f"  · {row[0][:40]}" for row in news_rows)

        fund_row = conn.execute(
            "SELECT per, pbr, roe, market_cap FROM fundamentals_snapshot WHERE code=?",
            (code,),
        ).fetchone()
        if fund_row and fund_row[3]:
            mcap = fund_row[3]
            mcap_str = f"{mcap/1e12:.1f}조" if mcap >= 1e12 else f"{mcap/1e8:.0f}억"
            fund_line = f"시총 {mcap_str}"
        else:
            fund_line = ""
    finally:
        conn.close()
    return {
        "company_name": company_name, "sector": sector,
        "trend_line": trend_line, "news_lines": news_lines, "fund_line": fund_line,
    }


async def _send_single_rec(
    bot,
    chat_id: int,
    *,
    rec_id: str,
    code: str,
    strategy_mode: str,
    entry_price: int,
    target_price: int,
    stop_price: int,
    expected_return_pct: float,
    ensemble_score: float,
    estimated_quantity: int,
    order_value: int,
    cross_mode: bool = False,
) -> None:
    """[deprecated — 호환용] 단일 추천 메시지. 신규 흐름은 _send_unified_rec 사용."""
    meta = await _fetch_rec_meta(code)
    company_name = meta["company_name"]; sector = meta["sector"]
    trend_line = meta["trend_line"]; news_lines = meta["news_lines"]; fund_line = meta["fund_line"]

    intent = {
        "action": "buy",
        "rec_id": rec_id,
        "code": code,
        "quantity": estimated_quantity,
        "price": entry_price,
        "strategy_mode": strategy_mode,
    }
    conf_uuid = confirmation_service.create(chat_id, intent)
    mode_label = _MODE_LABEL_KR.get(strategy_mode, strategy_mode)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"매수 ({mode_label})",
                                 callback_data=f"buy:{conf_uuid}"),
            InlineKeyboardButton("건너뜀", callback_data=f"skip:{conf_uuid}"),
            InlineKeyboardButton("취소", callback_data="cancel"),
        ]
    ])

    lines = [
        f"📊 {company_name} ({code})",
        f"⭐ {ensemble_score:.1f}점  ·  {mode_label}" + (f"  ·  {sector}" if sector else ""),
    ]
    if cross_mode:
        lines.append("🔁 양 모드 추천 — 한 모드만 매수하세요")
    if fund_line:
        lines.append(f"💰 {fund_line}")
    lines += [
        "",
        f"📌 진입가   {entry_price:,}원",
        f"🎯 목표가   {target_price:,}원  (+{expected_return_pct:.1f}%)",
        f"🛑 손절가   {stop_price:,}원",
        "",
        f"추천 수량   {estimated_quantity}주  =  {order_value:,}원",
    ]
    if trend_line:
        lines += ["", f"📉 {trend_line}"]
    if news_lines:
        lines += ["", "📰 최근 뉴스", news_lines]
    lines += ["", f"🪪 {rec_id}  ·  ⏱ 10분 내 결정"]
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=keyboard)


async def _send_unified_rec(
    bot,
    chat_id: int,
    *,
    code: str,
    by_mode: dict,
    company_name: str = "",
    sector: str = "",
    trend_line: str = "",
    news_lines: str = "",
    fund_line: str = "",
) -> None:
    """종목 단위 통합 메시지. by_mode 키 'bunt'/'squeeze' 중 있는 것만 모드별 라인 + 매수 버튼.

    by_mode[mode] = {
      "rec_id": str, "entry_price": int, "target_price": int, "stop_price": int,
      "expected_return_pct": float, "ensemble_score": float,
      "estimated_quantity": int, "order_value": int,
    }
    """
    modes_present = [m for m in ("bunt", "squeeze") if m in by_mode]
    cross = len(modes_present) == 2

    head_lines = [
        f"📊 {company_name or code} ({code})",
    ]
    score_parts = []
    for m in modes_present:
        score_parts.append(f"{_MODE_LABEL_KR[m]} ⭐{by_mode[m]['ensemble_score']:.1f}")
    score_line = "  ·  ".join(score_parts)
    if sector:
        score_line += f"  ·  {sector}"
    head_lines.append(score_line)
    if cross:
        head_lines.append("🔁 양 모드 추천 — 한 모드만 매수하세요")
    if fund_line:
        head_lines.append(f"💰 {fund_line}")

    lines = list(head_lines)

    # 모드별 진입가/TP/SL 블록 + 모드 설명 한 줄
    from src.risk.guard import SWING_MODE_PARAMS, StrategyMode
    for m in modes_present:
        d = by_mode[m]
        params = SWING_MODE_PARAMS[StrategyMode(m)]
        risk_reward = round(params['tp_pct'] / params['sl_pct'], 2)
        style = "안정형" if m == "bunt" else "공격형"
        lines += [
            "",
            f"━ {_MODE_LABEL_KR[m]} ({style}, TP +{params['tp_pct']}% / SL -{params['sl_pct']}%, 손익비 {risk_reward}:1)",
            f"  📌 진입   {d['entry_price']:,}원",
            f"  🎯 목표   {d['target_price']:,}원  (+{d['expected_return_pct']:.1f}%)",
            f"  🛑 손절   {d['stop_price']:,}원",
            f"  추천      {d['estimated_quantity']}주  =  {d['order_value']:,}원",
        ]

    if trend_line:
        lines += ["", f"📉 {trend_line}"]
    if news_lines:
        lines += ["", "📰 최근 뉴스", news_lines]

    rec_ids = [by_mode[m]["rec_id"] for m in modes_present]
    lines += ["", f"🪪 {' · '.join(rec_ids)}  ·  ⏱ 10분 내 결정"]

    # 키보드 — 모드별 매수 버튼 + 통합 건너뜀 + 취소
    # 매수 conf_uuid 는 모드별로 발급 (intent에 mode 포함)
    buy_row = []
    for m in modes_present:
        d = by_mode[m]
        intent = {
            "action": "buy", "rec_id": d["rec_id"], "code": code,
            "quantity": d["estimated_quantity"], "price": d["entry_price"],
            "strategy_mode": m,
        }
        conf_uuid = confirmation_service.create(chat_id, intent)
        label = f"매수 ({_MODE_LABEL_KR[m]})" if cross else f"매수 ({_MODE_LABEL_KR[m]})"
        buy_row.append(InlineKeyboardButton(label, callback_data=f"buy:{conf_uuid}"))

    # 건너뜀: 양 모드 rec_id 모두 한 번에 처리. intent.rec_ids 리스트.
    skip_intent = {
        "action": "skip", "code": code, "rec_ids": rec_ids,
        "rec_id": rec_ids[0],  # 단일 rec_id 호환 fallback
    }
    skip_uuid = confirmation_service.create(chat_id, skip_intent)
    nav_row = [
        InlineKeyboardButton("건너뜀", callback_data=f"skip:{skip_uuid}"),
        InlineKeyboardButton("취소", callback_data="cancel"),
    ]
    keyboard = InlineKeyboardMarkup([buy_row, nav_row])

    await bot.send_message(chat_id, "\n".join(lines), reply_markup=keyboard)


def _cached_recs_for_today(
    chat_id: int,
    strategy_mode: str,
    session_date: str,
) -> list[tuple]:
    """오늘자·해당 모드 추천을 DB 에서 조회. 없으면 [].

    같은 (chat_id, session_date, mode, code) 가 여러 rec_id 로 저장돼 있으면
    가장 늦은(=최신) rec_id 만 canonical 로 반환 — 추천 갱신/refresh 시 새 점수가 표시됨.
    옛 rec_id 는 그대로 남아 회고에서 액션 추적 가능.
    """
    conn = get_connection()
    try:
        return conn.execute(
            """SELECT r.rec_id, r.code, r.entry_price, r.target_price, r.stop_price,
                      r.expected_return_pct, r.ensemble_score
               FROM recommendations r
               INNER JOIN (
                 SELECT code, MAX(rec_id) AS canonical_rec_id
                 FROM recommendations
                 WHERE chat_id = ? AND session_date = ? AND strategy_mode = ?
                 GROUP BY code
               ) c ON r.rec_id = c.canonical_rec_id
               ORDER BY r.rec_id""",
            (chat_id, session_date, strategy_mode),
        ).fetchall()
    finally:
        conn.close()


async def _group_header_text(
    chat_id: int,
    session_date: str,
    n_bunt: int,
    n_squeeze: int,
    n_cross: int,
) -> str:
    # 가용 현금 — KIS 모드면 KIS가 진실, paper 모드면 봇 DB
    cash_label = "💵 가용 현금"
    if config.TRADE_MODE in (config.TradeMode.KIS_MOCK, config.TradeMode.LIVE):
        broker_bal = await portfolio_service.get_broker_balance(config.TRADE_MODE.value)
        if broker_bal and "error" not in broker_bal:
            cash = broker_bal["cash_available"]
            cash_label = "💵 KIS 주문가능"
        else:
            summary = portfolio_service.get_account_summary(chat_id, config.SEED_KRW)
            cash = summary["cash_available"]
    else:
        summary = portfolio_service.get_account_summary(chat_id, config.SEED_KRW)
        cash = summary["cash_available"]

    lines = [
        f"🌅 오늘의 추천 ({session_date})",
        f"🟢 번트 {n_bunt}건  ·  🟠 스퀴즈 {n_squeeze}건",
        f"{cash_label}  {cash:+,}원",
    ]
    if n_cross > 0:
        lines.append(f"🔁 양 모드 중복: {n_cross}건 — 한 모드만 매수하세요")
    # 3루 코치: 연속 손실 시 매수 자제 권유 (감정 매매 방지)
    streak = portfolio_service.get_losing_days_streak(chat_id)
    if streak >= 2:
        lines.append(f"⚠ 최근 {streak}일 연속 손실 — 오늘은 쉬는 것도 전략입니다")
    return "\n".join(lines)


async def send_cached_recommendations_dual(
    bot,
    chat_id: int,
    session_date: str,
) -> int:
    """양 모드 캐시 replay. 둘 다 비어있으면 0 반환 (caller 가 신규 계산하도록).
    한쪽만 있어도 발송. 발송 건수 반환."""
    from src.risk.guard import PER_POSITION_CAP_PCT
    bunt_rows = _cached_recs_for_today(chat_id, "bunt", session_date)
    squeeze_rows = _cached_recs_for_today(chat_id, "squeeze", session_date)
    if not bunt_rows and not squeeze_rows:
        return 0

    bunt_codes = {r[1] for r in bunt_rows}
    squeeze_codes = {r[1] for r in squeeze_rows}
    cross = bunt_codes & squeeze_codes

    await bot.send_message(
        chat_id,
        await _group_header_text(
            chat_id, session_date, len(bunt_rows), len(squeeze_rows), len(cross),
        ),
    )

    per_cap = config.SEED_KRW * PER_POSITION_CAP_PCT // 100

    # 종목별 dedup
    by_code: dict[str, dict] = {}
    for mode, rows in (("bunt", bunt_rows), ("squeeze", squeeze_rows)):
        for rec_id, code, entry_price, tp, sl, exp_ret, score in rows:
            qty = per_cap // entry_price if entry_price > 0 else 0
            if qty < 1:
                continue
            by_code.setdefault(code, {})[mode] = {
                "rec_id": rec_id,
                "entry_price": int(entry_price),
                "target_price": int(tp),
                "stop_price": int(sl),
                "expected_return_pct": float(exp_ret or 0),
                "ensemble_score": float(score or 0),
                "estimated_quantity": qty,
                "order_value": int(entry_price) * qty,
            }

    def _sort_key(item):
        code, modes = item
        is_cross = len(modes) == 2
        primary = modes.get("bunt") or modes.get("squeeze")
        return (0 if is_cross else 1, 0 if "bunt" in modes else 1, -primary["ensemble_score"])

    ordered = sorted(by_code.items(), key=_sort_key)
    total = 0
    for code, modes in ordered:
        meta = await _fetch_rec_meta(code)
        await _send_unified_rec(bot, chat_id, code=code, by_mode=modes, **meta)
        total += 1

    audit_service.log_event(chat_id, "recommend_push", {
        "mode": "dual",
        "n_picks_bunt": len(bunt_rows),
        "n_picks_squeeze": len(squeeze_rows),
        "n_unique_codes": len(by_code),
        "source": "cache",
    })
    return total


async def send_recommendations_dual(
    bot,
    chat_id: int,
    codes: list[str],
    force_fresh: bool = False,
) -> int:
    """양 모드 신규 계산 후 발송. 발송 건수 반환.
    force_fresh=True 면 오늘자 추천이 이미 있어도 새로 계산 (refresh 시나리오).
    """
    from datetime import date as _date

    today_iso = _date.today().isoformat()
    # 진입 가드: 같은 (chat_id, session_date) 추천이 이미 있으면 cache replay 로 위임.
    # 동시 다중 호출 race (예: 봇 cmd_recommend + CLI push 동시 실행) 방지.
    if not force_fresh:
        conn = get_connection()
        try:
            existing = conn.execute(
                "SELECT COUNT(*) FROM recommendations WHERE chat_id=? AND session_date=?",
                (chat_id, today_iso),
            ).fetchone()[0]
        finally:
            conn.close()
        if existing > 0:
            log.info(
                "recommend_push: chat=%s 오늘 추천 %d건 이미 존재 → cache replay 위임",
                chat_id, existing,
            )
            return await send_cached_recommendations_dual(bot, chat_id, today_iso)

    # recommend() 는 동기 함수 + 종목당 expert 평가로 4-6분 소요 → event loop 차단 방지를 위해
    # 양 모드 병렬 executor 호출. 그 사이 매수 callback / pending_rec_monitor 등 정상 동작.
    import asyncio as _asyncio
    bunt_picks, squeeze_picks = await _asyncio.gather(
        _asyncio.to_thread(
            recommend,
            codes=codes, active_seed_krw=config.SEED_KRW,
            mode="bunt", top_n=10, min_score=config.RECOMMEND_MIN_SCORE,
        ),
        _asyncio.to_thread(
            recommend,
            codes=codes, active_seed_krw=config.SEED_KRW,
            mode="squeeze", top_n=10, min_score=config.RECOMMEND_MIN_SCORE,
        ),
    )
    picks_by_mode = {"bunt": bunt_picks, "squeeze": squeeze_picks}

    bunt_codes = {p.opinion.code for p in picks_by_mode["bunt"]}
    squeeze_codes = {p.opinion.code for p in picks_by_mode["squeeze"]}
    cross = bunt_codes & squeeze_codes

    audit_service.log_event(chat_id, "recommend_push", {
        "mode": "dual",
        "n_candidates": len(codes),
        "n_picks_bunt": len(picks_by_mode["bunt"]),
        "n_picks_squeeze": len(picks_by_mode["squeeze"]),
        "source": "scheduler",
    })

    if not picks_by_mode["bunt"] and not picks_by_mode["squeeze"]:
        await bot.send_message(
            chat_id, "오늘 추천 종목 없음 (조건 미달 또는 데이터 부족)",
        )
        return 0

    await bot.send_message(
        chat_id,
        await _group_header_text(
            chat_id, today_iso,
            len(picks_by_mode["bunt"]), len(picks_by_mode["squeeze"]), len(cross),
        ),
    )

    # 1) 모든 picks 를 DB에 INSERT 하고 종목별로 dedup (rec_id 발급)
    by_code: dict[str, dict] = {}
    for mode in ("bunt", "squeeze"):
        for r in picks_by_mode[mode]:
            expected_return_pct = round(
                (r.target_price - r.last_close) / r.last_close * 100, 2
            )
            expert_scores = {
                name: round(op.score, 1)
                for name, op in r.opinion.opinions.items()
                if getattr(op, "is_valid", True)
            }
            rec_id = recommendation_service.create_recommendation(
                chat_id=chat_id,
                market="KR",
                code=r.opinion.code,
                strategy_mode=mode,
                entry_price=r.last_close,
                target_price=r.target_price,
                stop_price=r.stop_price,
                expected_return_pct=expected_return_pct,
                reason_summary=r.opinion.reason_summary,
                ensemble_score=r.opinion.ensemble_score,
                reason_json={
                    "mode_fit": r.opinion.mode_fit,
                    "fallback_used": r.opinion.fallback_used,
                    "expert_scores": expert_scores,
                },
            )
            by_code.setdefault(r.opinion.code, {})[mode] = {
                "rec_id": rec_id,
                "entry_price": r.last_close,
                "target_price": r.target_price,
                "stop_price": r.stop_price,
                "expected_return_pct": expected_return_pct,
                "ensemble_score": r.opinion.ensemble_score,
                "estimated_quantity": r.estimated_quantity,
                "order_value": r.order_value,
            }

    # 2) 종목별 통합 메시지 발송 — bunt 우선 정렬, score 내림차순
    def _sort_key(item):
        code, modes = item
        # cross 우선 → 단일모드는 bunt 우선 → score 내림차순
        is_cross = len(modes) == 2
        primary = modes.get("bunt") or modes.get("squeeze")
        return (0 if is_cross else 1, 0 if "bunt" in modes else 1, -primary["ensemble_score"])

    ordered = sorted(by_code.items(), key=_sort_key)
    total = 0
    for code, modes in ordered:
        # 종목 정보 (회사명/섹터/트렌드/뉴스/펀더) — 한 번만 조회
        meta = await _fetch_rec_meta(code)
        await _send_unified_rec(
            bot, chat_id,
            code=code, by_mode=modes,
            **meta,
        )
        total += 1
    return total


# ============================================================
# 시장 레짐 체크
# ============================================================

async def _check_market_regime(bot=None) -> tuple[bool, str]:
    """전일 코스피 등락률 확인.

    config.MARKET_DOWN_THRESHOLD_PCT 미만이면 (False, reason) 반환.
    0 이하 임계값이 0.0 으로 설정된 경우 필터 비활성화.
    pykrx 실패 시 (True, "") 반환 — 추천 차단보다 발송 누락이 더 나쁨.
    단, 실패는 관리자에게 1회 경보 (bot 전달 시) — 레짐 필터가 silent 하게
    꺼진 채 추천이 나가는 상황을 사람이 인지하도록.
    """
    if config.MARKET_DOWN_THRESHOLD_PCT >= 0:
        return True, ""  # 필터 비활성화
    try:
        from datetime import date as _date, timedelta
        from pykrx import stock as _krx
        today = _date.today()
        from_date = (today - timedelta(days=7)).strftime("%Y%m%d")
        to_date = (today - timedelta(days=1)).strftime("%Y%m%d")
        df = _krx.get_index_ohlcv(from_date, to_date, "1001")
        if df is None or len(df) < 2:
            return True, ""
        last_close = float(df["종가"].iloc[-1])
        prev_close = float(df["종가"].iloc[-2])
        if prev_close <= 0:
            return True, ""
        daily_ret = (last_close - prev_close) / prev_close * 100
        if daily_ret < config.MARKET_DOWN_THRESHOLD_PCT:
            reason = (
                f"전일 코스피 {daily_ret:.1f}% 하락 "
                f"(임계값 {config.MARKET_DOWN_THRESHOLD_PCT:.1f}%) — 오늘 추천 보류"
            )
            log.warning("market_regime: %s", reason)
            return False, reason
        log.info("market_regime: 코스피 전일 %+.1f%% → 추천 진행", daily_ret)
        return True, ""
    except Exception:
        log.warning("market_regime: 코스피 확인 실패 → 추천 진행")
        if bot is not None and config.TELEGRAM_ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    config.TELEGRAM_ADMIN_CHAT_ID,
                    "⚠ 레짐 체크 실패 — 코스피 지수 조회 불가 (pykrx 빈 응답)\n\n"
                    f"오늘 코스피 급락 필터(임계값 {config.MARKET_DOWN_THRESHOLD_PCT:.1f}%)가 "
                    "적용되지 않은 채 추천이 그대로 발송됩니다.\n"
                    "logs/bunting.err 의 market_regime 항목 점검 요망.",
                )
                audit_service.log_event(None, "market_regime_check_failed", {})
            except Exception:
                log.exception("market_regime 경보 발송 실패")
        return True, ""


# ============================================================
# 가격 모니터 (싱글톤)
# ============================================================

_price_monitor = None


def _get_price_monitor():
    global _price_monitor
    if _price_monitor is None:
        from src.services.price_monitor import PriceMonitor
        _price_monitor = PriceMonitor()
    return _price_monitor


# ============================================================
# Jobs
# ============================================================

async def _run_data_refresh(ctx: ContextTypes.DEFAULT_TYPE, *, label: str) -> None:
    """OHLCV/펀더/수급 증분 수집을 별도 스레드에서 실행.
    실패한 단계가 있으면 admin chat 에만 알림 (일반 사용자 무알림)."""
    import asyncio
    from src.crawlers.collect_all import run_pipeline

    log.info("%s: 데이터 증분 수집 시작 (skip_per_code=True)", label)
    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(
            None,
            lambda: run_pipeline(
                first_time=False, years=0, codes_limit=None,
                skip_per_code=True, per_code_days=1, continue_on_error=True,
            ),
        )
        n_ok = sum(1 for r in results if r.ok)
        n_fail = sum(1 for r in results if not r.ok)
        log.info("%s: 데이터 수집 %d/%d 단계 성공", label, n_ok, len(results))
        if n_fail and config.TELEGRAM_ADMIN_CHAT_ID:
            failed = [r.label for r in results if not r.ok]
            try:
                await ctx.bot.send_message(
                    config.TELEGRAM_ADMIN_CHAT_ID,
                    f"⚠ {label} 데이터 수집 일부 실패: {', '.join(failed)}",
                )
            except Exception:
                pass
    except Exception:
        log.exception("%s: 데이터 수집 실패", label)


async def job_morning_data_refresh(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 07:30 KST — 종목 데이터 백그라운드 최신화 (silent).

    OHLCV/펀더/수급 증분 수집. universe rebuild 는 일 23:00 그대로 (주 1회).
    08:00 추천 30분 전에 끝내서 추천 잡과 ThreadPool/KIS 컨텐션 회피
    (잔고 조회 블로킹 사고 회귀 방지).
    """
    if not is_kr_trading_day():
        log.info("morning_data_refresh: 오늘은 KRX 휴장일 — 스킵")
        return
    await _run_data_refresh(ctx, label="morning_data_refresh")


async def job_morning_recommend(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 08:00 KST — 승인된 전체 사용자에게 국내장 추천 발송.

    데이터 최신화는 07:30 의 job_morning_data_refresh 가 책임. 여기선 추천만.
    추천 계산이 dual 모드 ~24분 소요 → 08:00 시작이면 08:24 발송, 장 시작 36분 전 도착.
    """
    # 주말·공휴일·휴장일 스킵
    if not is_kr_trading_day():
        log.info("morning_recommend: 오늘은 KRX 휴장일 — 추천 발송 생략")
        return

    # 시장 레짐 체크 — 전일 코스피 급락 시 추천 보류
    market_ok, market_reason = await _check_market_regime(ctx.bot)
    if not market_ok:
        users = _get_approved_users()
        for u in users:
            try:
                await ctx.bot.send_message(
                    u.chat_id,
                    f"⚠ 오늘 추천 보류\n\n{market_reason}\n\n시장이 안정되면 내일 재개합니다.",
                )
            except Exception:
                pass
        return

    # 추천 발송
    codes = _list_candidate_codes()
    if not codes:
        log.warning("morning_recommend: 후보 종목 없음 (60일 이상 일봉 없음)")
        return

    users = _get_approved_users()
    if not users:
        log.info("morning_recommend: 승인된 사용자 없음")
        return

    log.info("morning_recommend: %d 사용자 × %d 후보 종목", len(users), len(codes))
    for u in users:
        try:
            n = await send_recommendations_dual(ctx.bot, u.chat_id, codes)
            log.info("morning_recommend: chat_id=%s sent=%d", u.chat_id, n)
        except Exception:
            log.exception("morning_recommend: chat_id=%s 실패", u.chat_id)


async def job_price_monitor(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 09:00~15:30 매 3분 — 보유 포지션 TP/SL 체크. TP/SL 도달 시 자동 매도 실행."""
    from src.services.price_monitor import AlertType
    from src.services.portfolio_service import execute_sell

    # 거래일 + 장 운영 시간(09:00~15:30) 동시 체크
    if not is_kr_market_session_now():
        return

    monitor = _get_price_monitor()
    users = _get_approved_users()

    for u in users:
        try:
            alerts = monitor.check_positions(u.chat_id)
            for alert in alerts:
                if alert.is_exit_signal:
                    # TP → 목표가 체결, SL → 손절가 체결
                    sell_price = (
                        alert.target_price if alert.alert_type == AlertType.TP_HIT
                        else alert.stop_price
                    )
                    result = await execute_sell(
                        u.chat_id, alert.position_id, sell_price, config.SEED_KRW,
                    )
                    head = f"{alert.code} {alert.name}" if alert.name else alert.code
                    pos_mode = getattr(alert, "strategy_mode", "") or ""
                    if result["success"]:
                        icon = "🎯" if alert.alert_type == AlertType.TP_HIT else "🛑"
                        label = "익절" if alert.alert_type == AlertType.TP_HIT else "손절"
                        mode_line = f"\n  {_mode_explanation(pos_mode, u.holding_mode)}" if pos_mode else ""
                        msg = (
                            f"{icon} 자동 {label} 완료\n"
                            f"\n"
                            f"  {head}{mode_line}\n"
                            f"  체결가   {result['exit_price']:,}원\n"
                            f"  순손익   {result['net_pnl']:+,}원  ({result['return_pct']:+.2f}%)"
                        )
                    elif result.get("pending"):
                        # KIS 에 매도 주문 등록됐지만 미체결 대기 — 다음 사이클 재주문 안 함 (sell_order_id 마킹됨)
                        label = "익절" if alert.alert_type == AlertType.TP_HIT else "손절"
                        msg = (
                            f"⏳ 자동 {label} 매도 주문 대기 중  —  {head}\n"
                            f"\n"
                            f"  {result['reason']}\n"
                            f"  • KIS 앱에서 미체결 매도 주문 확인\n"
                            f"  • 체결되면 자동 반영됨\n"
                            f"  • 봇은 같은 종목 추가 매도 시도 안 함"
                        )
                    elif result.get("external_closed"):
                        # KIS 잔고 0 + grace 경과 → 외부 매도 추정. DB 자동 정리 후 1회 알림.
                        msg = (
                            f"🧹 외부 매도 감지  —  {head}\n"
                            f"\n"
                            f"  KIS 잔고에 보유 없음 → DB 자동 정리\n"
                            f"  실제 손익은 KIS 거래내역에서 확인하세요"
                        )
                    else:
                        msg = (
                            f"⚠️ 자동매도 실패  [{head}]\n"
                            f"{result['reason']}\n\n"
                            + alert.format_message()
                        )
                    await ctx.bot.send_message(u.chat_id, msg)
                    audit_service.log_event(u.chat_id, "price_alert", {
                        "code": alert.code, "type": alert.alert_type.value,
                        "price": alert.current_price, "return_pct": alert.return_pct,
                        "auto_sell": True, "sell_success": result.get("success"),
                        "reason": result.get("reason"),
                    })
                else:
                    # NEAR_TP / NEAR_SL 은 알림만 발송
                    await ctx.bot.send_message(u.chat_id, alert.format_message())
                    audit_service.log_event(u.chat_id, "price_alert", {
                        "code": alert.code, "type": alert.alert_type.value,
                        "price": alert.current_price, "return_pct": alert.return_pct,
                    })
                log.info("price_alert: chat_id=%s code=%s type=%s",
                         u.chat_id, alert.code, alert.alert_type.value)
        except Exception:
            log.exception("price_monitor: chat_id=%s 실패", u.chat_id)


async def job_balance_periodic(ctx: ContextTypes.DEFAULT_TYPE):
    """장중 매 N분 — 승인된 사용자에게 잔고 스냅샷 발송."""
    # 거래일 + 장 운영 시간(09:00~15:30) 동시 체크
    if not is_kr_market_session_now():
        return

    users = _get_approved_users()
    for u in users:
        try:
            mode_label = "번트" if u.strategy_mode == "bunt" else "스퀴즈"
            lines = [f"💼 잔고 스냅샷  ·  {mode_label}", ""]

            if config.TRADE_MODE in (config.TradeMode.KIS_MOCK, config.TradeMode.LIVE):
                b = await portfolio_service.get_broker_balance(config.TRADE_MODE.value)
                if b is None or "error" in b:
                    err = b.get("error", "?") if b else "조회 실패"
                    lines.append(f"  ⚠ KIS 조회 실패: {err}")
                else:
                    lines += [
                        f"  💎 순자산        {b['total_evaluation']:,}원",
                        f"  💰 손익          {b['total_pnl']:+,}원  ({b['total_pnl_pct']:+.2f}%)",
                        f"  💵 주문가능현금  {b['cash_available']:+,}원",
                    ]
                    if b["positions"]:
                        lines += ["", f"📦 보유 {len(b['positions'])}종목"]
                        for p in b["positions"]:
                            head = f"{p['code']} {p['name']}" if p.get("name") else p["code"]
                            lines += [
                                f"  • {head} {p['quantity']}주 @{p['avg_price']:,} → {p['current_price']:,} ({p['pnl_pct']:+.2f}%)"
                            ]
                    else:
                        lines += ["", "📦 보유 종목 없음"]
            else:
                s = portfolio_service.get_account_summary(u.chat_id, config.SEED_KRW)
                lines += [
                    f"  활성 시드   {s['active_seed']:,}원",
                    f"  가용 현금   {s['cash_available']:,}원",
                    f"  누적 손익   {s['closed_pnl_total']:+,}원",
                    f"  보유        {len(s['open_positions'])}종목",
                ]

            await ctx.bot.send_message(u.chat_id, "\n".join(lines))
        except Exception:
            log.exception("balance_periodic: chat_id=%s 실패", u.chat_id)


async def job_market_open_reset(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 09:00 — 장 시작 시 모니터 알림 이력 리셋."""
    if not is_kr_trading_day():  # 주말·공휴일·휴장일 스킵
        return
    _get_price_monitor().reset_alerts()
    _get_pending_rec_monitor().reset_alerts()
    log.info("price_monitor + pending_rec_monitor: 알림 이력 리셋 완료")


# ============================================================
# PendingRecMonitor (3루 코치 A — 추천 진입가 하락 알림)
# ============================================================

_pending_rec_monitor = None


def _get_pending_rec_monitor():
    global _pending_rec_monitor
    if _pending_rec_monitor is None:
        from src.services.price_monitor import PendingRecMonitor
        _pending_rec_monitor = PendingRecMonitor()
    return _pending_rec_monitor


def _format_pending_rec_alert_single(alert) -> str:
    mode_label = _MODE_LABEL_KR.get(alert.strategy_mode, alert.strategy_mode)
    head = f"{alert.code} {alert.name}" if alert.name else alert.code
    if alert.discount_pct < 0.05:
        diff_line = f"  현재가      {alert.current_price:,}원  (진입가 도달)"
    else:
        diff_line = f"  현재가      {alert.current_price:,}원  (-{alert.discount_pct:.2f}%)"
    return (
        f"🎯 진입가 도달  —  {head}  ·  [{mode_label}]\n"
        f"\n"
        f"  추천 진입가  {alert.entry_price:,}원  ({alert.session_date})\n"
        f"{diff_line}\n"
        f"  ⭐ 점수      {alert.ensemble_score:.1f}\n"
        f"\n"
        f"  🎯 목표가  {alert.target_price:,}원\n"
        f"  🛑 손절가  {alert.stop_price:,}원\n"
        f"\n"
        f"💡 매수 결정을 내릴 시점입니다. (이 알림은 종목당 1번만 옵니다)"
    )


def _format_pending_rec_alert_dual(alerts: list) -> str:
    """같은 종목이 번트+스퀴즈 양 모드 동시 도달 시 통합 메시지."""
    a0 = alerts[0]
    head = f"{a0.code} {a0.name}" if a0.name else a0.code
    lines = [
        f"🎯 진입가 도달  —  {head}",
        "",
        "🔁 양 모드 추천 — 한 모드만 매수하세요",
        "",
    ]
    for a in sorted(alerts, key=lambda x: x.strategy_mode):  # bunt 먼저, squeeze 나중
        mode_label = _MODE_LABEL_KR.get(a.strategy_mode, a.strategy_mode)
        if a.discount_pct < 0.05:
            diff = "(진입가 도달)"
        else:
            diff = f"(-{a.discount_pct:.2f}%)"
        lines += [
            f"  [{mode_label}]  ⭐ {a.ensemble_score:.1f}  ({a.session_date})",
            f"    진입 {a.entry_price:,} → 현재 {a.current_price:,} {diff}",
            f"    🎯 {a.target_price:,}  /  🛑 {a.stop_price:,}",
            "",
        ]
    lines.append("💡 한 모드만 매수 — 한 종목 중복 매수 위험. (이 알림은 종목당 1번)")
    return "\n".join(lines)


async def job_pending_rec_monitor(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 09:00~15:30 매 3분 — 미매수 추천 종목이 진입가 -3% 이내로 떨어지면 알림.
    자동 매수 X — 알림에 매수 버튼만.

    같은 종목이 번트+스퀴즈 양쪽에서 도달하면 1개 메시지로 통합 발송 (매수 버튼 2개).
    """
    # 거래일 + 장 운영 시간(09:00~15:30) 동시 체크
    if not is_kr_market_session_now():
        return

    monitor = _get_pending_rec_monitor()
    users = _get_approved_users()
    from src.risk.guard import PER_POSITION_CAP_PCT

    for u in users:
        try:
            alerts = monitor.check_pending_recs(u.chat_id, days=1)
            if not alerts:
                continue

            # (code, mode) 중복 제거 — 중복 추천 rec_id 로 같은 종목·모드 알림이
            # 반복돼 한 메시지에 같은 줄이 여러 번 찍히고 "종목당 1번" 문구와 모순되던 사고 방지.
            _dedup: dict[tuple, object] = {}
            for a in alerts:
                _dedup.setdefault((a.code, a.strategy_mode), a)
            alerts = list(_dedup.values())

            # code 별로 그룹화 — 같은 종목 양 모드 도달 시 통합
            by_code: dict[str, list] = {}
            for a in alerts:
                by_code.setdefault(a.code, []).append(a)

            for code, group in by_code.items():
                per_cap = config.SEED_KRW * PER_POSITION_CAP_PCT // 100
                # 매수 버튼 — 모드별 conf_uuid 발급 (한 메시지 안에 둘 다)
                buttons = []
                for a in sorted(group, key=lambda x: x.strategy_mode):
                    qty = max(1, per_cap // a.current_price) if a.current_price > 0 else 1
                    intent = {
                        "action": "buy",
                        "rec_id": a.rec_id,
                        "code": a.code,
                        "quantity": qty,
                        "price": a.current_price,
                        "strategy_mode": a.strategy_mode,
                    }
                    conf_uuid = confirmation_service.create(u.chat_id, intent)
                    label = _MODE_LABEL_KR.get(a.strategy_mode, a.strategy_mode)
                    buttons.append(InlineKeyboardButton(
                        f"매수 ({label})", callback_data=f"buy:{conf_uuid}",
                    ))
                buttons.append(InlineKeyboardButton("무시", callback_data="cancel"))
                kb = InlineKeyboardMarkup([buttons])

                if len(group) == 1:
                    text = _format_pending_rec_alert_single(group[0])
                else:
                    text = _format_pending_rec_alert_dual(group)

                await ctx.bot.send_message(u.chat_id, text, reply_markup=kb)
                for a in group:
                    audit_service.log_event(u.chat_id, "pending_rec_alert", {
                        "rec_id": a.rec_id, "code": a.code,
                        "mode": a.strategy_mode,
                        "discount_pct": a.discount_pct,
                        "consolidated": len(group) > 1,
                    })
                log.info(
                    "pending_rec_alert: chat_id=%s code=%s modes=%d",
                    u.chat_id, code, len(group),
                )
        except Exception:
            log.exception("pending_rec_monitor: chat_id=%s 실패", u.chat_id)


async def job_pending_buy_polling(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 09:00~15:30 매 1분 — 매수 pending 주문 체결 확인 + 5분 미체결 알림.

    KIS 모의투자 daily-ccld 가 즉시 반영 안 되는 경우 다수 →
    submit_order 시점에 잡지 못한 체결을 백그라운드에서 polling 으로 확인.
    체결 확인되면 봇 DB position INSERT + TP/SL 자동 + 사용자 알림.
    5분 경과 시 한 번 [계속 대기]/[취소] 키보드 발송 (audit_log dedup).
    """
    # 거래일 + 장 운영 시간(09:00~15:30) 동시 체크
    if not is_kr_market_session_now():
        return

    if config.TRADE_MODE not in (config.TradeMode.KIS_MOCK, config.TradeMode.LIVE):
        return  # paper 모드는 즉시 체결 가정

    from src.services.portfolio_service import get_broker
    from src.risk.guard import RiskGuard, StrategyMode
    import json as _json
    adapter = get_broker(config.TRADE_MODE.value)

    conn = get_connection()
    try:
        # 오늘 매수 pending 주문 조회 (audit_log 와 chat_id 매핑)
        rows = conn.execute(
            """SELECT bo.id, bo.code, bo.quantity, bo.broker_order_id, bo.created_at,
                      al.chat_id, al.payload_json
               FROM broker_orders bo
               LEFT JOIN audit_log al ON al.id = bo.audit_id
               WHERE bo.side='buy' AND bo.status='pending'
                 AND date(bo.created_at)=date('now','+9 hours')
               ORDER BY bo.id""",
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return

    # 5분 알림 dedup — audit_log 의 'buy_pending_5min_alert' 이벤트 조회
    conn = get_connection()
    try:
        alert_rows = conn.execute(
            """SELECT payload_json FROM audit_log
               WHERE event_type='buy_pending_5min_alert'
                 AND date(ts)=date('now','+9 hours')""",
        ).fetchall()
    finally:
        conn.close()
    alerted_orders: set[str] = set()
    for ar in alert_rows:
        try:
            p = _json.loads(ar[0] or "{}")
            if p.get("broker_order_id"):
                alerted_orders.add(p["broker_order_id"])
        except Exception:
            continue

    for bo_id, code, qty, broker_order_id, created_at, chat_id, payload_json in rows:
        if not chat_id or not broker_order_id:
            continue
        try:
            res = await adapter.get_order_status(broker_order_id)
        except Exception as e:
            log.warning("[pending_buy_poll %s] 조회 실패: %s", broker_order_id, e)
            continue

        if res.status in ("filled", "partial"):
            fill_qty = int(res.filled_quantity) if res.filled_quantity else qty
            fill_price = int(res.filled_avg_price) if res.filled_avg_price else 0
            if fill_price <= 0 or fill_qty <= 0:
                continue
            # broker_orders UPDATE + positions INSERT
            now_iso = datetime.now().isoformat(timespec="seconds")
            try:
                payload = _json.loads(payload_json or "{}")
            except Exception:
                payload = {}
            strategy_mode = payload.get("mode") or "bunt"
            # holding_mode 는 사용자 현재 설정 사용 (매수 폴링 시점 = 매수 직후라 신규 매수와 동일 의도)
            holding = "swing_week"
            try:
                u = user_service.get_user(chat_id)
                holding = getattr(u, "holding_mode", None) or "swing_week"
            except Exception:
                pass
            try:
                mode_enum = StrategyMode(strategy_mode)
                tp, sl = RiskGuard.compute_target_stop(
                    fill_price, mode_enum, holding_mode=holding,
                )
            except Exception:
                tp, sl = 0, 0

            conn = get_connection()
            try:
                conn.execute(
                    """UPDATE broker_orders SET status=?, filled_quantity=?,
                                                  filled_avg_price=?, updated_at=?
                       WHERE id=?""",
                    (res.status, fill_qty, fill_price, now_iso, bo_id),
                )
                # 중복 INSERT 방지 — 같은 broker_order_id 의 position 이미 있으면 skip
                existing = conn.execute(
                    "SELECT id FROM positions WHERE buy_order_id=?", (bo_id,)
                ).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO positions
                           (chat_id, code, strategy_mode, buy_order_id, buy_price, quantity,
                            target_price, stop_price, status, opened_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
                        (chat_id, code, strategy_mode, bo_id, fill_price, fill_qty,
                         tp, sl, now_iso),
                    )
                conn.commit()
            finally:
                conn.close()

            audit_service.log_event(chat_id, "buy_pending_filled", {
                "code": code, "broker_order_id": broker_order_id,
                "fill_qty": fill_qty, "fill_price": fill_price,
                "mode": strategy_mode,
            })
            try:
                await ctx.bot.send_message(
                    chat_id,
                    f"✅ 매수 체결 (예약 매수)\n"
                    f"\n"
                    f"  종목     {code}\n"
                    f"  수량     {fill_qty}주\n"
                    f"  체결가   {fill_price:,}원\n"
                    f"  🎯 목표  {tp:,}원\n"
                    f"  🛑 손절  {sl:,}원\n"
                    f"\n"
                    f"봇 DB 자동 반영 — 자동매도 활성화"
                )
            except Exception:
                log.exception("buy_pending_filled 알림 실패: chat=%s", chat_id)
            continue

        # 미체결 — 5분 경과 + 아직 알림 안 보냈으면 1번만 발송
        if broker_order_id in alerted_orders:
            continue
        try:
            created = datetime.fromisoformat(created_at)
        except Exception:
            continue
        if (datetime.now() - created).total_seconds() < 5 * 60:
            continue

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("계속 대기", callback_data=f"bwait:{broker_order_id}"),
            InlineKeyboardButton("주문 취소", callback_data=f"bcncl:{broker_order_id}"),
        ]])
        try:
            await ctx.bot.send_message(
                chat_id,
                f"⏳ 매수 미체결 5분 경과\n"
                f"\n"
                f"  종목     {code}\n"
                f"  수량     {qty}주\n"
                f"  ODNO     {broker_order_id}\n"
                f"\n"
                f"계속 대기 또는 취소를 선택하세요.",
                reply_markup=kb,
            )
            audit_service.log_event(chat_id, "buy_pending_5min_alert", {
                "code": code, "broker_order_id": broker_order_id, "qty": qty,
            })
        except Exception:
            log.exception("buy_pending_5min_alert 발송 실패: chat=%s", chat_id)


async def job_buy_partial_recheck(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 09:05~15:30 매 5분 — 매수 부분체결 시간차 재검증 (P0 fix).

    KIS 모의투자가 매수 후 잔고를 단계적으로 갱신하는 결함 (2026-05-04 / 05-06):
    submit_order 의 잔고 fallback 이 단일 시점 비교라 부분만 인식하는 케이스 발생.
    백그라운드에서 잔고 재조회하여 추가 체결분을 broker_orders + positions 에 보정.

    대상: side='buy' AND status IN ('filled','partial') AND filled_quantity < quantity
          AND created_at BETWEEN now-30min AND now-5min
    동작: KIS 잔고 재조회 → 같은 종목 다른 매수의 합 빼서 귀속분 계산 →
          기존 filled_quantity 보다 크면 broker_orders + positions UPDATE + 사용자 알림
    """
    from datetime import timedelta

    if not is_kr_market_session_now():
        return
    if config.TRADE_MODE not in (config.TradeMode.KIS_MOCK, config.TradeMode.LIVE):
        return  # paper 즉시 체결 가정

    from src.services.portfolio_service import get_broker
    adapter = get_broker(config.TRADE_MODE.value)

    now = datetime.now()
    window_start = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    window_end = (now - timedelta(minutes=5)).isoformat(timespec="seconds")

    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT bo.id, bo.code, bo.quantity, bo.filled_quantity,
                      bo.broker_order_id, al.chat_id
               FROM broker_orders bo
               LEFT JOIN audit_log al ON al.id = bo.audit_id
               WHERE bo.side='buy' AND bo.status IN ('filled','partial')
                 AND bo.filled_quantity < bo.quantity
                 AND bo.created_at BETWEEN ? AND ?""",
            (window_start, window_end),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return

    try:
        bal = await adapter.get_balance()
    except Exception as e:
        log.warning("[buy_partial_recheck] 잔고 조회 실패: %s", e)
        return
    kis_by_code: dict[str, int] = {
        p["code"]: int(p["quantity"]) for p in bal.get("positions", [])
    }

    for bo_id, code, qty, filled_qty, broker_order_id, chat_id in rows:
        if not chat_id:
            continue
        cur_qty = kis_by_code.get(code, 0)

        # 같은 종목 다른 open positions 의 quantity 합 (이 매수가 아닌 것)
        conn = get_connection()
        try:
            other_held_row = conn.execute(
                """SELECT COALESCE(SUM(quantity), 0) FROM positions
                   WHERE chat_id=? AND code=? AND status='open' AND buy_order_id != ?""",
                (chat_id, code, bo_id),
            ).fetchone()
            other_held = int(other_held_row[0] or 0)

            this_pos_row = conn.execute(
                "SELECT id, quantity FROM positions WHERE buy_order_id=? AND status='open'",
                (bo_id,),
            ).fetchone()
        finally:
            conn.close()

        if not this_pos_row:
            continue  # 이미 closed 됐으면 스킵

        pos_id, _pos_qty = this_pos_row
        # 이 매수에 귀속될 수 있는 잔고 = KIS 잔고 - 다른 매수의 합
        attributable = cur_qty - other_held
        if attributable <= filled_qty:
            continue  # 추가 체결 없음
        new_filled = min(attributable, qty)  # 발주 수량 상한
        delta = new_filled - filled_qty
        if delta <= 0:
            continue

        now_iso = datetime.now().isoformat(timespec="seconds")
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE broker_orders SET filled_quantity=?, status=?, updated_at=? WHERE id=?",
                (new_filled, "filled" if new_filled == qty else "partial", now_iso, bo_id),
            )
            conn.execute(
                "UPDATE positions SET quantity=? WHERE id=?",
                (new_filled, pos_id),
            )
            conn.commit()
        finally:
            conn.close()

        log.info(
            "[buy_partial_recheck %s] quantity 보정: %d → %d (+%d)",
            broker_order_id, filled_qty, new_filled, delta,
        )
        audit_service.log_event(chat_id, "buy_partial_recheck_corrected", {
            "code": code, "broker_order_id": broker_order_id,
            "old_filled": filled_qty, "new_filled": new_filled, "delta": delta,
        })
        try:
            await ctx.bot.send_message(
                chat_id,
                f"🔧 매수 추가 체결 감지\n"
                f"\n"
                f"  종목         {code}\n"
                f"  추가 체결    +{delta}주\n"
                f"  보유 수량    {filled_qty}주 → {new_filled}주\n"
                f"\n"
                f"잔고 갱신 시간차로 누락됐던 부분 보정 완료."
            )
        except Exception:
            log.exception("buy_partial_recheck 알림 실패: chat=%s", chat_id)


# ── 매도 phantom 탈출 (잔고=진실) ──────────────────────────
# KIS 모의투자가 marketable 매도도 영영 pending 으로 응답하는 phantom 대응(2026-05, 005940 등).
# daily-ccld 신호 대신 잔고로 강제 해소: 잔고0→청산, 잔고보유→취소+시장가 재집행(상한).
_SELL_PHANTOM_ESCALATE_SEC = 5 * 60   # pending 매도 5분 경과 시 강제 해소(기존 30분 단축)
_SELL_PHANTOM_MAX_RETRY = 2           # 시장가 재집행 최대 횟수 → 초과 시 관리자 수동


def _count_phantom_escalations_today(code: str) -> int:
    import json as _j
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT payload_json FROM audit_log
               WHERE event_type='sell_phantom_escalated'
                 AND date(ts)=date('now','+9 hours')"""
        ).fetchall()
    finally:
        conn.close()
    n = 0
    for r in rows:
        try:
            if _j.loads(r[0] or "{}").get("code") == code:
                n += 1
        except Exception:
            continue
    return n


async def _escape_phantom_sell(ctx, adapter, *, bo_id, code, qty, broker_order_id,
                               chat_id, position_id, buy_price) -> bool:
    """pending 매도를 잔고(=진실)로 강제 해소. 액션했으면 True, 미처리(잔고 조회 실패)면 False.

    잔고 0  → 이미 빠짐(체결 미보고/외부) → 포지션 청산(pnl=NULL).
    잔고 보유 → 미체결 phantom → 기존 주문 취소 + 시장가 재집행(상한 후 관리자 알림, 이중매도 방지).
    """
    from src.adapters.broker_base import OrderRequest
    try:
        bal = await adapter.get_balance()
    except Exception as e:
        log.warning("[phantom_sell %s] 잔고 조회 실패: %s", broker_order_id, e)
        return False
    pos = next((p for p in bal.get("positions", []) if p.get("code") == code), None)
    held = pos.get("quantity", 0) if pos else 0
    cur_price = (pos.get("current_price") if pos else 0) or 0
    now_iso = datetime.now().isoformat(timespec="seconds")

    # --- 잔고 0: 이미 빠짐 → 청산 ---
    if held <= 0:
        try:
            await adapter.cancel_order(broker_order_id)
        except Exception:
            pass
        conn = get_connection()
        try:
            conn.execute("UPDATE broker_orders SET status='cancelled', updated_at=? WHERE id=?",
                         (now_iso, bo_id))
            conn.execute(
                "UPDATE positions SET sell_order_id=NULL, status='closed', pnl=NULL, closed_at=? "
                "WHERE id=? AND status='open'",
                (now_iso, position_id),
            )
            conn.commit()
        finally:
            conn.close()
        audit_service.log_event(chat_id, "sell_phantom_closed", {
            "code": code, "broker_order_id": broker_order_id, "qty": qty,
        })
        log.info("[phantom_sell %s] 잔고 0 → 포지션 %s 청산", broker_order_id, position_id)
        if chat_id:
            try:
                await ctx.bot.send_message(
                    chat_id,
                    f"🧹 매도 정리 — {code}\n  KIS 잔고 0 (체결/외부) → DB 청산\n  실손익은 KIS 거래내역 확인",
                )
            except Exception:
                pass
        return True

    # --- 잔고 보유: 미체결 phantom → 시장가 재집행 (상한) ---
    attempts = _count_phantom_escalations_today(code)
    if attempts >= _SELL_PHANTOM_MAX_RETRY:
        if config.TELEGRAM_ADMIN_CHAT_ID:
            try:
                await ctx.bot.send_message(
                    config.TELEGRAM_ADMIN_CHAT_ID,
                    f"🚨 매도 phantom 재집행 한도({_SELL_PHANTOM_MAX_RETRY}) 초과 — {code} {held}주\n"
                    f"  자동 시장가 재집행 모두 미체결. 수동 매도 필요.",
                )
            except Exception:
                pass
        log.warning("[phantom_sell %s] 재집행 한도 초과(%d) — 수동 개입 대기", broker_order_id, attempts)
        return True   # churn 방지: 더는 자동 재집행 안 함

    # 기존 phantom 주문 취소 + 마킹 해제
    try:
        await adapter.cancel_order(broker_order_id)
    except Exception:
        pass
    conn = get_connection()
    try:
        conn.execute("UPDATE broker_orders SET status='cancelled', updated_at=? WHERE id=?",
                     (now_iso, bo_id))
        conn.execute("UPDATE positions SET sell_order_id=NULL WHERE sell_order_id=?", (bo_id,))
        conn.commit()
    finally:
        conn.close()

    # 시장가 재발주 (체결 강제). 시장가 체결가 미보고(avg=0) → 현재가로 추정.
    sell_qty = min(held, qty)
    res = await adapter.submit_order(OrderRequest(side="sell", code=code, quantity=sell_qty, price=None))
    fill_price = res.filled_avg_price or cur_price or 0
    audit_id = audit_service.log_event(chat_id, "sell_phantom_escalated", {
        "code": code, "old_odno": broker_order_id, "new_odno": res.broker_order_id,
        "status": res.status, "qty": sell_qty, "attempt": attempts + 1,
    })
    log.info("[phantom_sell %s] 시장가 재집행 (%d/%d) → %s",
             broker_order_id, attempts + 1, _SELL_PHANTOM_MAX_RETRY, res.status)

    new_status = res.status if res.status in ("filled", "partial", "pending") else "failed"
    net_pnl = None
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, broker_order_id,
                status, filled_quantity, filled_avg_price, commission, tax, created_at, updated_at)
               VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (audit_id, config.TRADE_MODE.value, code, sell_qty, fill_price or 0,
             res.broker_order_id or "", new_status, res.filled_quantity or 0, fill_price or 0,
             res.commission or 0, res.tax or 0, now_iso, now_iso),
        )
        new_bo_id = cur.lastrowid
        if res.status in ("filled", "partial"):
            fq = res.filled_quantity or sell_qty
            net_pnl = (fill_price - (buy_price or 0)) * fq if fill_price else None
            conn.execute(
                "UPDATE positions SET sell_order_id=?, status='closed', pnl=?, closed_at=? "
                "WHERE id=? AND status='open'",
                (new_bo_id, net_pnl, now_iso, position_id),
            )
        elif res.status == "pending":
            # 다음 사이클 재평가(상한까지). sell_order_id 마킹으로 price_monitor 중복 방지.
            conn.execute("UPDATE positions SET sell_order_id=? WHERE id=? AND status='open'",
                         (new_bo_id, position_id))
        conn.commit()
    finally:
        conn.close()

    if res.status in ("filled", "partial") and chat_id:
        try:
            await ctx.bot.send_message(
                chat_id,
                f"🛑 자동손절 재집행 완료 (시장가) — {code}\n"
                f"  {res.filled_quantity or sell_qty}주 @ ~{fill_price:,}원"
                + (f"\n  순손익 {net_pnl:+,}원" if net_pnl is not None else "\n  (체결가 추정)"),
            )
        except Exception:
            pass
    return True


async def job_pending_sell_polling(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 09:00~15:30 매 1분 — 매도 pending 체결 확인 + 좀비 자동 정리.

    매수 polling 과 대칭. broker_orders.status='pending' AND side='sell' 조회 →
      • filled/partial → broker_orders + position 갱신, 사용자 알림
      • zombie (KIS '취소 수량 없음' 응답) → broker_orders.cancelled + position.sell_order_id=NULL
      • 그 외 pending → 다음 사이클로 미룸 (10분 경과 시 사용자 1회 알림)
    """
    import json as _json

    if not is_kr_market_session_now():
        return
    if config.TRADE_MODE not in (config.TradeMode.KIS_MOCK, config.TradeMode.LIVE):
        return  # paper 는 즉시 체결 가정

    from src.services.portfolio_service import get_broker
    adapter = get_broker(config.TRADE_MODE.value)

    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT bo.id, bo.code, bo.quantity, bo.broker_order_id, bo.created_at,
                      au.chat_id, p.id, p.buy_price
               FROM broker_orders bo
               JOIN audit_log au ON au.id = bo.audit_id
               LEFT JOIN positions p ON p.sell_order_id = bo.id
               WHERE bo.side='sell' AND bo.status='pending'
                 AND bo.broker_order_id IS NOT NULL"""
        ).fetchall()
    finally:
        conn.close()

    # 알림 dedup — audit_log 의 sell_pending_10min_alert 조회
    conn = get_connection()
    try:
        alert_rows = conn.execute(
            """SELECT payload_json FROM audit_log
               WHERE event_type='sell_pending_10min_alert'
                 AND date(ts)=date('now','+9 hours')"""
        ).fetchall()
    finally:
        conn.close()
    alerted_orders: set[str] = set()
    for ar in alert_rows:
        try:
            p = _json.loads(ar[0] or "{}")
            if p.get("broker_order_id"):
                alerted_orders.add(p["broker_order_id"])
        except Exception:
            continue

    for bo_id, code, qty, broker_order_id, created_at, chat_id, position_id, buy_price in rows:
        if not broker_order_id:
            continue
        try:
            res = await adapter.get_order_status(broker_order_id)
        except Exception as e:
            log.warning("[pending_sell_poll %s] 조회 실패: %s", broker_order_id, e)
            continue

        # ===== 이미 KIS 측에서 cancelled / failed → 봇 DB 동기화만 =====
        if res.status in ("cancelled", "failed"):
            now_iso = datetime.now().isoformat(timespec="seconds")
            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE broker_orders SET status=?, updated_at=? WHERE id=?",
                    (res.status, now_iso, bo_id),
                )
                conn.execute(
                    "UPDATE positions SET sell_order_id=NULL WHERE sell_order_id=?",
                    (bo_id,),
                )
                conn.commit()
            finally:
                conn.close()
            audit_service.log_event(chat_id, "sell_zombie_cleaned", {
                "code": code, "broker_order_id": broker_order_id, "qty": qty,
                "kis_status": res.status,
            })
            log.info(
                "[pending_sell_poll %s] KIS 측 %s 확인 → 봇 DB 동기화",
                broker_order_id, res.status,
            )
            continue

        # ===== 체결 (full or partial) — 봇 DB 갱신 =====
        if res.status in ("filled", "partial"):
            fill_qty = int(res.filled_quantity) if res.filled_quantity else qty
            fill_price = int(res.filled_avg_price) if res.filled_avg_price else 0
            if fill_qty <= 0 or fill_price <= 0:
                continue
            now_iso = datetime.now().isoformat(timespec="seconds")
            net_pnl = (fill_price - (buy_price or 0)) * fill_qty - (res.commission or 0) - (res.tax or 0)
            return_pct = ((fill_price - buy_price) / buy_price * 100) if buy_price else 0.0
            conn = get_connection()
            try:
                conn.execute(
                    """UPDATE broker_orders SET status=?, filled_quantity=?,
                                                  filled_avg_price=?,
                                                  commission=?, tax=?, updated_at=?
                       WHERE id=?""",
                    (res.status, fill_qty, fill_price,
                     res.commission or 0, res.tax or 0, now_iso, bo_id),
                )
                if position_id:
                    conn.execute(
                        """UPDATE positions
                           SET status='closed', pnl=?, closed_at=?
                           WHERE id=?""",
                        (net_pnl, now_iso, position_id),
                    )
                conn.commit()
            finally:
                conn.close()

            audit_service.log_event(chat_id, "sell_pending_filled", {
                "code": code, "broker_order_id": broker_order_id,
                "fill_qty": fill_qty, "fill_price": fill_price,
                "pnl": net_pnl, "return_pct": return_pct,
            })
            try:
                if chat_id:
                    await ctx.bot.send_message(
                        chat_id,
                        f"✅ 매도 체결 (예약 매도)\n"
                        f"\n"
                        f"  종목     {code}\n"
                        f"  수량     {fill_qty}주\n"
                        f"  체결가   {fill_price:,}원\n"
                        f"  순손익   {net_pnl:+,}원  ({return_pct:+.2f}%)\n"
                        f"\n"
                        f"봇 DB 자동 청산 반영 완료"
                    )
            except Exception:
                log.exception("sell_pending_filled 알림 실패: chat=%s", chat_id)
            continue

        # ===== 미체결 pending — age 기반 분기 =====
        try:
            created = datetime.fromisoformat(created_at)
        except Exception:
            continue
        age_sec = (datetime.now() - created).total_seconds()

        # ===== phantom 탈출 (잔고=진실) — pending 5분 경과 시 강제 해소 =====
        # daily-ccld 가 marketable 매도도 영영 pending 으로 응답하는 phantom 대응.
        # 해소(액션)하면 이번 사이클 종료. 미해소(잔고 조회 실패)면 아래 기존 분기로 fallback.
        if age_sec >= _SELL_PHANTOM_ESCALATE_SEC and position_id and broker_order_id:
            try:
                if await _escape_phantom_sell(
                    ctx, adapter, bo_id=bo_id, code=code, qty=qty,
                    broker_order_id=broker_order_id, chat_id=chat_id,
                    position_id=position_id, buy_price=buy_price,
                ):
                    continue
            except Exception:
                log.exception("[phantom_sell %s] 해소 중 오류", broker_order_id)

        # 10분~30분 사이: 사용자 1회 알림 (좀비 판정 전 단계)
        if 10 * 60 <= age_sec < 30 * 60 and broker_order_id not in alerted_orders:
            try:
                if chat_id:
                    await ctx.bot.send_message(
                        chat_id,
                        f"⏳ 매도 미체결 10분 경과\n"
                        f"\n"
                        f"  종목   {code}\n"
                        f"  수량   {qty}주\n"
                        f"  ODNO   {broker_order_id}\n"
                        f"\n"
                        f"KIS 앱에서 미체결 주문 확인 권장.\n"
                        f"봇은 30분 후 좀비 검사하고 자동 정리합니다."
                    )
                audit_service.log_event(chat_id, "sell_pending_10min_alert", {
                    "code": code, "broker_order_id": broker_order_id, "qty": qty,
                })
            except Exception:
                log.exception("sell_pending_10min_alert 발송 실패: chat=%s", chat_id)
            continue   # 알림만 보내고 다음 사이클

        # 30분 이상 pending: cancel_order 시도해서 KIS 가 "수량 없음" 반환하면 좀비
        if age_sec >= 30 * 60:
            try:
                cancel_res = await adapter.cancel_order_detail(broker_order_id)
            except Exception as e:
                log.warning("[pending_sell_poll %s] cancel 시도 실패: %s", broker_order_id, e)
                continue

            if cancel_res.get("is_zombie"):
                # 봇 DB 만 정리. KIS 측은 어차피 큐에 없음.
                now_iso = datetime.now().isoformat(timespec="seconds")
                conn = get_connection()
                try:
                    conn.execute(
                        "UPDATE broker_orders SET status='cancelled', updated_at=? WHERE id=?",
                        (now_iso, bo_id),
                    )
                    conn.execute(
                        "UPDATE positions SET sell_order_id=NULL WHERE sell_order_id=?",
                        (bo_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()
                audit_service.log_event(chat_id, "sell_zombie_cleaned", {
                    "code": code, "broker_order_id": broker_order_id, "qty": qty,
                    "msg_cd": cancel_res.get("msg_cd"),
                })
                log.info(
                    "[pending_sell_poll %s] 좀비 정리: bot DB cancelled, position unmark",
                    broker_order_id,
                )


def _previous_trading_day_iso() -> str:
    """직전 거래일 (월→금, 화~금→전일). 공휴일 미고려 — 0건이면 발송 안 됨."""
    from datetime import timedelta
    today = datetime.now(KST).date()
    delta = 3 if today.weekday() == 0 else 1   # 월요일이면 3일 전 (금)
    return (today - timedelta(days=delta)).isoformat()


def _format_daily_sell_report(day_iso: str, sells: list[dict]) -> list[str]:
    lines = [f"📊 어제 매도 통계  —  {day_iso}", ""]
    total_pnl = 0
    total_cost = 0
    wins = 0
    for s in sells:
        cost = s["buy_price"] * s["quantity"]
        total_cost += cost
        total_pnl += s["pnl"]
        if s["pnl"] > 0:
            wins += 1
        emoji = "✅" if s["pnl"] >= 0 else "❌"
        head = f"{s['code']} {s['name']}" if s["name"] else s["code"]
        mode_label = _MODE_LABEL_KR.get(s["strategy_mode"], s["strategy_mode"])
        lines += [
            f"  {emoji} {head}  ·  {mode_label}  ·  {s['quantity']}주",
            f"      매수 {s['buy_price']:,} → 매도 {s['sell_price']:,}"
            f"   {s['pnl']:+,}원  ({s['pnl_pct']:+.2f}%)",
        ]
    total_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
    lines += [
        "",
        f"  Σ 일계  {total_pnl:+,}원  ({total_pct:+.2f}%)  ·  승 {wins}/{len(sells)}",
    ]
    return lines


async def job_daily_sell_report(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 08:00 KST — 직전 거래일 청산 포지션 P&L 요약 발송.

    어제 매도 0건이면 발송 생략 (소음 줄이기).
    """
    if not is_kr_trading_day():
        return

    day_iso = _previous_trading_day_iso()
    users = _get_approved_users()
    for u in users:
        try:
            sells = portfolio_service.get_closed_positions_on(u.chat_id, day_iso)
            if not sells:
                continue   # 어제 매도 없음 → 발송 안 함
            lines = _format_daily_sell_report(day_iso, sells)
            await ctx.bot.send_message(u.chat_id, "\n".join(lines))
            audit_service.log_event(u.chat_id, "daily_sell_report", {
                "day": day_iso, "n_sells": len(sells),
                "total_pnl": sum(s["pnl"] for s in sells),
            })
        except Exception:
            log.exception("daily_sell_report: chat_id=%s 실패", u.chat_id)


_KIS_5XX_WINDOW_MIN = 15           # 최근 N분 카운트
_KIS_5XX_THRESHOLD = 30            # N건 이상 → 알림 (~2/min)
_KIS_5XX_ALERT_DEDUP_MIN = 60      # 알림 1회 발송 후 N분간 dedup


async def job_kis_health_check(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 09:00~15:30 매 15분 — KIS 5xx 다발 시 admin 자동 알림.

    조건:
      - 최근 15분 audit_log.kis_5xx 카운트 ≥ 30 (≈ 2/min)
      - 직전 60분 내 'kis_health_alert' 발송 안 됐을 때만 1회 발송
    """
    if not is_kr_market_session_now():
        return
    if not config.TELEGRAM_ADMIN_CHAT_ID:
        return

    import json as _json
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT payload_json FROM audit_log
                WHERE event_type='kis_5xx'
                  AND ts >= datetime('now','-{_KIS_5XX_WINDOW_MIN} minutes')"""
        ).fetchall()
    finally:
        conn.close()
    n = len(rows)
    if n < _KIS_5XX_THRESHOLD:
        return

    # dedup — 최근 60분 안에 알림 보냈으면 skip
    conn = get_connection()
    try:
        recent_alert = conn.execute(
            f"""SELECT 1 FROM audit_log
                WHERE event_type='kis_health_alert'
                  AND ts >= datetime('now','-{_KIS_5XX_ALERT_DEDUP_MIN} minutes')
                LIMIT 1"""
        ).fetchone()
    finally:
        conn.close()
    if recent_alert:
        return

    # top 종목 집계
    counts: dict[str, int] = {}
    for r in rows:
        try:
            p = _json.loads(r[0] or "{}")
            key = p.get("code") or p.get("endpoint") or "?"
            counts[key] = counts.get(key, 0) + 1
        except Exception:
            continue
    top = sorted(counts.items(), key=lambda x: -x[1])[:5]
    top_line = "\n".join(f"  • {k}  {v}건" for k, v in top)

    msg = (
        f"⚠️ KIS 5xx 다발 감지\n"
        f"\n"
        f"  최근 {_KIS_5XX_WINDOW_MIN}분: {n}건  (임계 {_KIS_5XX_THRESHOLD}건)\n"
        f"\n"
        f"━━━ top 종목/엔드포인트 ━━━\n"
        f"{top_line}\n"
        f"\n"
        f"💡 조치 검토:\n"
        f"  • 특정 종목 반복 5xx → instruments.is_tradable=0 으로 격리\n"
        f"  • 전체 KIS 측 장애일 가능성 — 약 30분 관찰\n"
        f"  • /admin_stats 로 누적 추이 확인\n"
        f"  • 다음 알림은 {_KIS_5XX_ALERT_DEDUP_MIN}분 dedup 후 가능"
    )
    try:
        await ctx.bot.send_message(config.TELEGRAM_ADMIN_CHAT_ID, msg)
        audit_service.log_event(None, "kis_health_alert", {
            "window_min": _KIS_5XX_WINDOW_MIN, "count": n,
            "top": top[:3],
        })
    except Exception:
        log.exception("kis_health_alert 발송 실패")


# ── 파이프라인 헬스 체크 (평일 08:05) ────────────────────────
# silent 데이터 실패(StepResult ok=True 인데 0건/stale)를 DB 독립 검증으로 잡는다.
# 2026-05: 종목마스터 3주 0건, 수급 4주 정지가 모두 우연히(로그 육안) 발견됨 → 자동 감지.
_HEALTH_MIN_INSTRUMENTS = 2000
_HEALTH_MIN_UNIVERSE = 400
_HEALTH_MIN_FLOW_CODES = 400
_HEALTH_MIN_FUNDAMENTALS = 400
_HEALTH_MIN_REC_DISTINCT = 3      # 오늘 추천 고유 종목수 하한 (1종목 복제 붕괴 포착)
_HEALTH_INSTRUMENT_STALE_DAYS = 2     # instruments.updated_at 허용 경과(달력일)
_HEALTH_FUND_STALE_DAYS = 7           # fundamentals_snapshot 허용 경과(달력일)
_HEALTH_DATA_STALE_TRADING_DAYS = 1   # ohlcv/flow: 직전거래일에서 추가 허용 거래일(공급자 지연 관용)


def _prev_trading_day(d):
    """d 직전(미포함) 가장 최근 거래일."""
    from datetime import timedelta
    x = d - timedelta(days=1)
    for _ in range(15):
        if _is_trading_day_cached(x.isoformat()):
            return x
        x -= timedelta(days=1)
    return d - timedelta(days=1)


def _check_pipeline_health():
    """아침 파이프라인 산출물 신선도 독립 검증 → [(이름, ok, detail)].

    장 시작 전(08:05)이라 ohlcv/flow 의 기대 최신값은 '직전 거래일'.
    instruments/추천은 당일 갱신 기대. StepResult 를 신뢰하지 않고 DB 를 직접 본다.
    """
    from datetime import date, timedelta
    today = date.today()
    floor_td = _prev_trading_day(today)
    for _ in range(_HEALTH_DATA_STALE_TRADING_DAYS):
        floor_td = _prev_trading_day(floor_td)
    floor = floor_td.isoformat()
    inst_floor = (today - timedelta(days=_HEALTH_INSTRUMENT_STALE_DAYS)).isoformat()
    fund_floor = (today - timedelta(days=_HEALTH_FUND_STALE_DAYS)).isoformat()
    out: list[tuple[str, bool, str]] = []
    conn = get_connection()
    try:
        # 1) 종목마스터 — upsert 라 count 는 안 줄고 updated_at 신선도가 진짜 신호
        n = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
        upd = (conn.execute("SELECT MAX(updated_at) FROM instruments").fetchone()[0] or "")[:10]
        out.append(("종목마스터", n >= _HEALTH_MIN_INSTRUMENTS and upd >= inst_floor,
                    f"{n}종목 · 갱신 {upd or '?'}"))

        # 2) OHLCV — 직전 거래일 봉
        mx = conn.execute("SELECT MAX(date) FROM ohlcv_daily").fetchone()[0]
        out.append(("OHLCV", bool(mx) and mx >= floor, f"최신봉 {mx or '?'} (기대 ≥{floor})"))

        # 3) 재무
        nf = conn.execute("SELECT COUNT(*) FROM fundamentals_snapshot").fetchone()[0]
        mxf = conn.execute("SELECT MAX(snapshot_date) FROM fundamentals_snapshot").fetchone()[0]
        out.append(("재무", nf >= _HEALTH_MIN_FUNDAMENTALS and bool(mxf) and mxf >= fund_floor,
                    f"{nf}건 · 최신 {mxf or '?'}"))

        # 4) 수급 — 직전 거래일 & 충분한 종목수(10종목으로 붕괴했던 실패 즉시 포착)
        mxi = conn.execute("SELECT MAX(date) FROM investor_flow").fetchone()[0]
        codes = (conn.execute(
            "SELECT COUNT(DISTINCT code) FROM investor_flow WHERE date=?", (mxi,)
        ).fetchone()[0] if mxi else 0)
        out.append(("수급", bool(mxi) and mxi >= floor and codes >= _HEALTH_MIN_FLOW_CODES,
                    f"최신 {mxi or '?'} · {codes}종목 (기대 ≥{floor}, ≥{_HEALTH_MIN_FLOW_CODES})"))

        # 5) 분석 유니버스 (1종목으로 붕괴했던 5/7 사고 포착)
        nu = conn.execute("SELECT COUNT(*) FROM analysis_universe").fetchone()[0]
        out.append(("유니버스", nu >= _HEALTH_MIN_UNIVERSE, f"{nu}종목"))

        # 6) 오늘 추천 발송 — 건수 + 고유 종목수(1종목 복제 붕괴 포착)
        nr = conn.execute(
            "SELECT COUNT(*) FROM recommendations WHERE session_date=?", (today.isoformat(),)
        ).fetchone()[0]
        ndc = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM recommendations WHERE session_date=?",
            (today.isoformat(),),
        ).fetchone()[0]
        out.append(("추천발송", nr > 0 and ndc >= _HEALTH_MIN_REC_DISTINCT,
                    f"오늘 {nr}건 · 고유 {ndc}종목"))
    finally:
        conn.close()
    return out


async def job_pipeline_health_check(ctx: ContextTypes.DEFAULT_TYPE):
    """평일 08:05 KST — 아침 파이프라인이 신선한 데이터를 만들었는지 독립 검증.

    RED 가 하나라도 있으면 관리자에게만 1회 경보 (전체 green 이면 무알림).
    """
    if not is_kr_trading_day():
        return
    if not config.TELEGRAM_ADMIN_CHAT_ID:
        return
    try:
        checks = _check_pipeline_health()
    except Exception:
        log.exception("pipeline_health_check 실패")
        return

    failed = [(name, detail) for name, ok, detail in checks if not ok]
    if not failed:
        log.info("pipeline_health_check: 전체 green (%d개 검사)", len(checks))
        return

    lines = ["🚨 파이프라인 헬스 경보", "", f"{len(failed)}/{len(checks)} 검사 실패", ""]
    for name, detail in failed:
        lines.append(f"  ❌ {name} — {detail}")
    ok_names = [name for name, ok, _ in checks if ok]
    if ok_names:
        lines += ["", f"  ✅ {', '.join(ok_names)}"]
    lines += ["", "💡 logs/bunting.err 의 morning_data_refresh 단계 점검"]
    try:
        await ctx.bot.send_message(config.TELEGRAM_ADMIN_CHAT_ID, "\n".join(lines))
        audit_service.log_event(None, "pipeline_health_alert", {
            "failed": [n for n, _ in failed],
        })
    except Exception:
        log.exception("pipeline_health 경보 발송 실패")


async def job_daily_sell_check(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 15:20 KST — KIS 보유 종목 보여주고 매도 여부 결정 유도.
    eod_kr_sell_reminder(금요일 강제) 와 별개로, 평일 결정 트리거."""
    if not is_kr_trading_day():  # 주말·공휴일·휴장일 스킵
        return

    users = _get_approved_users()
    for u in users:
        try:
            b = await portfolio_service.get_broker_balance(config.TRADE_MODE.value)
            if b is None or "error" in b:
                continue
            if not b["positions"]:
                # 보유 없으면 알림 생략 (소음 줄이기)
                continue

            # 금요일은 eod_kr_sell_reminder 가 별도로 강제 청산 메시지 보내므로 제목 다르게
            head = "🔔 매도 결정 — 장 마감 10분 전"
            lines = [head, ""]
            total_pnl = 0
            for p in b["positions"]:
                cost = p["quantity"] * p["avg_price"]
                pnl = p.get("pnl", 0)
                total_pnl += pnl
                head_p = f"{p['code']} {p['name']}" if p.get("name") else p["code"]
                emoji = "📈" if pnl >= 0 else "📉"
                lines.append(
                    f"  {emoji} {head_p}  {p['quantity']}주"
                )
                lines.append(
                    f"      매수 {p['avg_price']:,} → 현재 {p['current_price']:,} "
                    f"({p['pnl_pct']:+.2f}%)  손익 {pnl:+,}원"
                )
            lines += [
                "",
                f"📊 총 평가손익  {total_pnl:+,}원  ({b.get('total_pnl_pct', 0):+.2f}%)",
                "",
                "지금 매도하려면 KIS 앱 또는 텔레그램 매도 버튼 활용.",
            ]
            await ctx.bot.send_message(u.chat_id, "\n".join(lines))
            audit_service.log_event(u.chat_id, "daily_sell_check", {
                "positions": len(b["positions"]),
                "total_pnl": total_pnl,
            })
        except Exception:
            log.exception("daily_sell_check: chat_id=%s 실패", u.chat_id)


async def job_eod_sell_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 15:20 KST — 미청산 포지션 보유자에게 청산 리마인더.

    - 금요일: 모든 사용자에게 발송 (주말 전 강제 청산).
    - 월~목: holding_mode='day' 사용자에게만 발송 (당일매매 강제 청산).
    """
    from datetime import date as _date
    from src.services.portfolio_service import get_open_positions

    if not is_kr_trading_day():  # 주말·공휴일·휴장일 스킵
        return
    is_friday = _date.today().weekday() == 4

    users = _get_approved_users()
    for u in users:
        try:
            # 월~목요일엔 day 모드 사용자만 발송
            if not is_friday and u.holding_mode != "day":
                continue

            positions = get_open_positions(u.chat_id)
            if not positions:
                continue

            if is_friday:
                lines = ["⏰ 금요일 장 마감 10분 전 — 주말 전 청산 필수!\n"]
            else:
                lines = ["⏰ 장 마감 10분 전 — 당일매매 청산 시점!\n"]
            rec_ids_with_pos: list[tuple[str | None, object]] = []
            for p in positions:
                conn = get_connection()
                try:
                    row = conn.execute(
                        """SELECT r.rec_id FROM recommendations r
                           JOIN recommendation_actions ra ON ra.rec_id = r.rec_id
                           WHERE ra.chat_id = ? AND ra.action_type = 'bought'
                             AND r.code = ? AND r.session_date = date('now')
                           ORDER BY ra.acted_at DESC LIMIT 1""",
                        (u.chat_id, p.code),
                    ).fetchone()
                finally:
                    conn.close()
                rec_id = row[0] if row else None
                rec_ids_with_pos.append((rec_id, p))

                tag = f"[{rec_id}] " if rec_id else ""
                lines.append(
                    f"  • {tag}{p.code} {p.quantity}주 @{p.buy_price:,}  "
                    f"목표 {p.target_price:,} / 손절 {p.stop_price:,}"
                )

            lines.append("\n매도 사유를 선택하세요 (종목별 개별 기록):")
            header = "\n".join(lines)
            await ctx.bot.send_message(u.chat_id, header)

            for rec_id, p in rec_ids_with_pos:
                if not rec_id:
                    continue
                await ctx.bot.send_message(
                    u.chat_id,
                    f"[{rec_id}] {p.code} — 매도 사유:",
                    reply_markup=_sell_tag_keyboard(rec_id),
                )

            audit_service.log_event(u.chat_id, "eod_reminder", {
                "n_positions": len(positions),
            })
        except Exception:
            log.exception("eod_sell_reminder: chat_id=%s 실패", u.chat_id)


async def job_collect_daily_data(ctx: ContextTypes.DEFAULT_TYPE):
    """월~금 16:00 KST — 장마감 후 *매일* 데이터 증분 수집 (per-code 포함).

    수집 순서: 종목 마스터 → OHLCV → 재무 → 수급 → 유니버스 재빌드 → 뉴스/커뮤/유튜브.
    morning_data_refresh(07:30) 는 속도 위해 per-code 를 skip 하므로, 뉴스/커뮤/유튜브
    soft 시그널은 이 16:00 잡이 매일 수집해야 함. (2026-05: weekday()!=4 가드로 금요일만
    돌던 회귀 → 뉴스/커뮤/유튜브가 4/27 이후 방치됨. 거래일 가드만 남기고 daily 복원.)
    각 단계 실패 시 continue_on_error=True 로 다음 단계 계속 진행.
    """
    import asyncio
    from src.crawlers.collect_all import run_pipeline

    if not is_kr_trading_day():  # 거래일(월~금, 공휴일 제외)만 — 등록 스케줄도 days=(0~4)
        return

    log.info("collect_daily_data: 장마감 후 증분 수집 시작")
    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(
            None,
            lambda: run_pipeline(
                first_time=False,
                years=0,
                codes_limit=None,
                skip_per_code=False,
                per_code_days=1,
                continue_on_error=True,
            ),
        )
        n_ok = sum(1 for r in results if r.ok)
        n_fail = sum(1 for r in results if not r.ok)
        log.info("collect_daily_data: %d/%d 단계 성공", n_ok, len(results))
        if n_fail:
            failed = [r.label for r in results if not r.ok]
            log.warning("collect_daily_data: 실패 단계 %s", failed)
            # 어드민에게 실패 알림
            if config.TELEGRAM_ADMIN_CHAT_ID:
                fail_msg = f"[데이터 수집 오류] {', '.join(failed)} 단계 실패"
                try:
                    await ctx.bot.send_message(config.TELEGRAM_ADMIN_CHAT_ID, fail_msg)
                except Exception:
                    pass
    except Exception:
        log.exception("collect_daily_data 실패")


async def job_rebuild_universe(ctx: ContextTypes.DEFAULT_TYPE):
    """일요일 23:00 KST — 분석 유니버스 주간 재빌드.

    크롤러가 월요일 추천 전 최신 유니버스로 per-code 증분을 돌릴 수 있게 하려고
    일요일에 빌드. 평일 추천(08:00) 이전이면 언제든 OK.
    """
    from src.universe.builder import rebuild_universe
    try:
        n = rebuild_universe()
        log.info("job_rebuild_universe: %d 종목 저장", n)
    except Exception:
        log.exception("job_rebuild_universe 실패")


async def job_eod_review_report(ctx: ContextTypes.DEFAULT_TYPE):
    """금 15:40 KST — 이번 주 추천 vs 실제 결과 주간 회고 리포트."""
    from datetime import date as _date, timedelta

    # 금요일이자 거래일만 실행
    if _date.today().weekday() != 4 or not is_kr_trading_day():
        return

    today = _date.today().isoformat()
    users = _get_approved_users()

    for u in users:
        try:
            conn = get_connection()
            try:
                recs = conn.execute(
                    """SELECT rec_id, code, name, strategy_mode, entry_price,
                              target_price, stop_price, expected_return_pct,
                              ensemble_score
                       FROM recommendations
                       WHERE chat_id = ? AND session_date = ?
                       ORDER BY rec_id""",
                    (u.chat_id, today),
                ).fetchall()
                if not recs:
                    continue

                actions = conn.execute(
                    """SELECT rec_id, action_type, price, reason_tag,
                              realized_pnl, realized_return_pct
                       FROM recommendation_actions
                       WHERE chat_id = ? AND date(acted_at) = ?
                       ORDER BY rec_id, id""",
                    (u.chat_id, today),
                ).fetchall()

                positions_closed = conn.execute(
                    """SELECT code, buy_price, quantity, pnl, closed_at
                       FROM positions
                       WHERE chat_id = ? AND status = 'closed'
                         AND date(closed_at) = ?""",
                    (u.chat_id, today),
                ).fetchall()

                closing_prices = {}
                codes_needed = [r[1] for r in recs]
                for code in codes_needed:
                    row = conn.execute(
                        "SELECT close FROM ohlcv_daily WHERE code = ? AND date = ?",
                        (code, today),
                    ).fetchone()
                    if row:
                        closing_prices[code] = row[0]
            finally:
                conn.close()

            action_map: dict[str, list[dict]] = {}
            for a in actions:
                rec_id = a[0]
                action_map.setdefault(rec_id, []).append({
                    "type": a[1], "price": a[2], "tag": a[3],
                    "pnl": a[4], "return_pct": a[5],
                })

            pnl_by_code = {}
            for p in positions_closed:
                pnl_by_code[p[0]] = {
                    "buy_price": p[1], "qty": p[2], "pnl": p[3],
                }

            lines = [f"📊 [{today}] 회고 리포트\n"]
            total_bought = 0
            total_skipped = 0
            total_pnl = 0
            wins = 0

            for r in recs:
                rec_id, code, name, mode, entry, target, stop, exp_ret, score = r
                acts = action_map.get(rec_id, [])
                bought_acts = [a for a in acts if a["type"] == "bought"]
                skipped_acts = [a for a in acts if a["type"] == "skipped"]
                sold_acts = [a for a in acts if a["type"] == "sold"]

                close_price = closing_prices.get(code)
                if close_price:
                    actual_ret = round((close_price - entry) / entry * 100, 2)
                else:
                    actual_ret = None

                if bought_acts:
                    total_bought += 1
                    pos_info = pnl_by_code.get(code)
                    if pos_info and pos_info["pnl"] is not None:
                        pnl = pos_info["pnl"]
                        total_pnl += pnl
                        if pnl > 0:
                            wins += 1
                        ret_str = f"{pnl:+,}원"
                    elif actual_ret is not None:
                        ret_str = f"종가 기준 {actual_ret:+.2f}%"
                    else:
                        ret_str = "결과 미확인"
                    buy_tag = _T.get(bought_acts[0]["tag"], bought_acts[0]["tag"])
                    sell_tag = _T.get(sold_acts[0]["tag"], "-") if sold_acts else "-"
                    lines.append(
                        f"🟢 [{rec_id}] {name}({code})\n"
                        f"   진입 {entry:,} → {f'종가 {close_price:,}' if close_price else '?'}"
                        f"  |  {ret_str}\n"
                        f"   매수사유: {buy_tag}  매도사유: {sell_tag}"
                    )
                elif skipped_acts:
                    total_skipped += 1
                    skip_tag = _T.get(skipped_acts[0]["tag"], skipped_acts[0]["tag"])
                    hypothetical = f"(매수했으면 {actual_ret:+.2f}%)" if actual_ret is not None else ""
                    lines.append(
                        f"⏭ [{rec_id}] {name}({code})\n"
                        f"   건너뜀 ({skip_tag})  {hypothetical}"
                    )
                else:
                    total_skipped += 1
                    hypothetical = f"(종가 기준 {actual_ret:+.2f}%)" if actual_ret is not None else ""
                    lines.append(
                        f"⬜ [{rec_id}] {name}({code})  미응답  {hypothetical}"
                    )

            lines.append("")
            win_rate = round(wins / total_bought * 100, 1) if total_bought > 0 else 0
            lines.append(
                f"추천 {len(recs)} | 매수 {total_bought} | 건너뜀 {total_skipped}\n"
                f"실현 손익: {total_pnl:+,}원 | 승률: {win_rate}%"
            )

            await ctx.bot.send_message(u.chat_id, "\n".join(lines))
            audit_service.log_event(u.chat_id, "eod_review", {
                "n_recs": len(recs), "n_bought": total_bought,
                "n_skipped": total_skipped, "total_pnl": total_pnl,
                "win_rate": win_rate,
            })
        except Exception:
            log.exception("eod_review_report: chat_id=%s 실패", u.chat_id)


# ============================================================
# 등록
# ============================================================

def register_jobs(app: Application) -> None:
    jq = app.job_queue

    # 월~금 16:00 — 장마감 후 일일 데이터 증분 수집 (다음날 추천 위해)
    jq.run_daily(
        job_collect_daily_data,
        time(16, 0, tzinfo=KST),
        days=(0, 1, 2, 3, 4),  # 월~금
        name="collect_daily_data",
    )

    # 일 23:00 — 주간 분석 유니버스 재빌드 (월요일 추천 전)
    jq.run_daily(
        job_rebuild_universe,
        time(23, 0, tzinfo=KST),
        days=(6,),  # Sunday only (APScheduler 0=월, 6=일 / python-telegram-bot 기준)
        name="weekly_rebuild_universe",
    )

    # 월~금 07:30 — 백그라운드 종목 데이터 최신화 (silent, 무알림).
    # 08:00 추천 30분 전에 끝내서 잔고 조회 등과 ThreadPool/KIS 컨텐션 회피.
    jq.run_daily(
        job_morning_data_refresh,
        time(7, 30, tzinfo=KST),
        days=(0, 1, 2, 3, 4),
        name="morning_data_refresh",
    )

    # 월~금 08:00 — 직전 거래일 매도 통계 리포트 (어제 매도 0건이면 발송 생략).
    jq.run_daily(
        job_daily_sell_report,
        time(8, 0, tzinfo=KST),
        days=(0, 1, 2, 3, 4),
        name="daily_sell_report",
    )

    # 평일 08:00 — 일일 추천 (job 내부에서 거래일/시장 레짐 체크).
    # 추천 계산이 dual 모드 ~24분 소요 → 08:00 시작이면 08:24 발송, 09:00 장 시작 36분 전.
    # AUTO_RECOMMEND_ENABLED=false 면 잡 등록 자체를 건너뜀. /추천 수동 호출은 영향 없음.
    if config.AUTO_RECOMMEND_ENABLED:
        jq.run_daily(
            job_morning_recommend,
            time(8, 0, tzinfo=KST),
            name="morning_kr_recommend",
        )
    else:
        log.info("AUTO_RECOMMEND_ENABLED=false — morning_kr_recommend 잡 등록 안 함")

    # 평일 08:05 — 파이프라인 헬스 체크 (07:30 데이터 + 08:00 추천 직후).
    # 데이터 신선도 5종 + 추천 발송을 DB 독립 검증. RED 일 때만 관리자 경보.
    jq.run_daily(
        job_pipeline_health_check,
        time(8, 5, tzinfo=KST),
        days=(0, 1, 2, 3, 4),
        name="pipeline_health_check",
    )

    # 월~금 09:00 — 장 시작 시 모니터 알림 리셋
    jq.run_daily(
        job_market_open_reset,
        time(9, 0, tzinfo=KST),
        name="market_open_reset",
    )

    # 월~금 09:05~15:30 매 3분 — 가격 모니터 (TP/SL 체크)
    # last=15:30 로 스케줄러 차원에서도 종료 잠금 (잡 내부 가드와 이중 방어)
    jq.run_repeating(
        job_price_monitor,
        interval=180,       # 3분
        first=time(9, 5, tzinfo=KST),
        last=time(15, 30, tzinfo=KST),
        name="price_monitor",
    )

    # 월~금 09:07~15:30 매 3분 — 미매수 추천 진입가 하락 알림 (3루 코치 A).
    # 가격 모니터와 2분 어긋나게 first 시간을 잡아 KIS 호출 부하 분산.
    jq.run_repeating(
        job_pending_rec_monitor,
        interval=180,       # 3분
        first=time(9, 7, tzinfo=KST),
        last=time(15, 30, tzinfo=KST),
        name="pending_rec_monitor",
    )

    # 월~금 09:01~15:30 매 1분 — 매수 pending 주문 체결 polling.
    # KIS 모의투자 daily-ccld 즉시 반영 안 되는 케이스 대응.
    # 체결 확인되면 봇 DB position INSERT, 5분 미체결이면 사용자 알림.
    jq.run_repeating(
        job_pending_buy_polling,
        interval=60,        # 1분
        first=time(9, 1, tzinfo=KST),
        last=time(15, 30, tzinfo=KST),
        name="pending_buy_polling",
    )

    # 월~금 09:02~15:30 매 1분 — 매도 pending polling + 좀비 자동 정리.
    # 매수 폴링과 1분 어긋나게 first 잡아 KIS 호출 부하 분산.
    # 30분 이상 pending → cancel 시도 → "수량 없음" 응답이면 봇 DB 정리.
    jq.run_repeating(
        job_pending_sell_polling,
        interval=60,
        first=time(9, 2, tzinfo=KST),
        last=time(15, 30, tzinfo=KST),
        name="pending_sell_polling",
    )

    # 월~금 09:05~15:30 매 5분 — 매수 부분체결 시간차 재검증 (P0 fix, 2026-05-06).
    # KIS 모의투자 잔고 단계적 갱신으로 매수가 부분만 인식되는 결함 보정.
    # 30분 윈도우 (5~30분 전 row) × 5분 간격 = 6번 체크.
    jq.run_repeating(
        job_buy_partial_recheck,
        interval=5 * 60,
        first=time(9, 5, tzinfo=KST),
        last=time(15, 30, tzinfo=KST),
        name="buy_partial_recheck",
    )

    # 월~금 09:15~15:30 매 15분 — KIS 5xx 다발 감지 → admin 자동 알림.
    # 임계치 30건/15분 도달 시 1회 발송, 60분 dedup.
    jq.run_repeating(
        job_kis_health_check,
        interval=15 * 60,
        first=time(9, 15, tzinfo=KST),
        last=time(15, 30, tzinfo=KST),
        name="kis_health_check",
    )

    # 월~금 09:10~15:30 매 N분 — 잔고 스냅샷 자동 푸시
    if config.BALANCE_PERIODIC_MINUTES > 0:
        jq.run_repeating(
            job_balance_periodic,
            interval=config.BALANCE_PERIODIC_MINUTES * 60,
            first=time(9, 10, tzinfo=KST),
            last=time(15, 30, tzinfo=KST),
            name="balance_periodic",
        )

    # 월~금 15:19 — 일일 매도 결정 알림 (장 마감 11분 전)
    # 금요일 15:20 의 eod_kr_sell_reminder 와 1분 어긋나게 발사 (메시지 충돌 방지)
    jq.run_daily(
        job_daily_sell_check,
        time(15, 19, tzinfo=KST),
        name="daily_sell_check",
    )

    # 월~금 15:20 — 청산 리마인더
    #   금: 전 사용자 (주말 전 강제 청산)
    #   월~목: holding_mode='day' 사용자 한정 (당일매매 강제 청산)
    jq.run_daily(
        job_eod_sell_reminder,
        time(15, 20, tzinfo=KST),
        days=(0, 1, 2, 3, 4),
        name="eod_kr_sell_reminder",
    )

    # 금 15:40 — 주간 회고 리포트
    jq.run_daily(
        job_eod_review_report,
        time(15, 40, tzinfo=KST),
        name="eod_kr_review_report",
    )

    log.info(
        "Scheduled: data_refresh 평일 07:30, sell_report 평일 08:00, "
        "recommend 평일 08:00, monitor 09:05~15:30/3min, "
        "pending_rec 09:07~15:30/3min, pending_buy_poll 09:01~15:30/1min, "
        "collect 금 16:00, rebuild 일 23:00, "
        "sell_reminder 평일 15:20 (금=전체/평일=day모드), review 금 15:40 KST"
    )
