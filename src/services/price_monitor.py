"""가격 모니터링 서비스 — 보유 포지션의 TP/SL 도달 감시.

주간 스윙 전략에서 주중 목표가 도달 시 즉시 알림을 보내기 위한 핵심 서비스.

사용법:
  monitor = PriceMonitor(kis_source)
  alerts = monitor.check_positions(chat_id)
  # alerts: [PriceAlert(code, alert_type='tp_hit', ...)]

스케줄링:
  장중(09:00~15:20) 매 1~5분마다 check_positions 호출.
  봇 스케줄러(scheduler.py)에서 주기적으로 실행.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from src.adapters.market_data_base import CurrentPrice
from src.adapters.market_data_kis import KISMarketDataSource
from src.services.portfolio_service import OpenPositionDTO, get_open_positions

log = logging.getLogger("bunting.monitor")


class AlertType(str, Enum):
    TP_HIT = "tp_hit"          # 목표가 도달 (고가 ≥ target_price)
    SL_HIT = "sl_hit"          # 손절가 도달 (저가 ≤ stop_price)
    NEAR_TP = "near_tp"        # 목표가 근접 (현재가가 목표가의 90% 이상)
    NEAR_SL = "near_sl"        # 손절가 근접 (현재가가 손절가의 110% 이하)


_MODE_LABEL_KR = {"bunt": "번트", "squeeze": "스퀴즈"}


@dataclass
class PriceAlert:
    code: str
    position_id: int
    alert_type: AlertType
    entry_price: int        # 매수가
    target_price: int       # 목표가
    stop_price: int         # 손절가
    current_price: int      # 현재가
    high_today: int         # 당일 고가
    low_today: int          # 당일 저가
    return_pct: float       # 현재 수익률 (%)
    checked_at: datetime
    strategy_mode: str = ""  # "bunt" / "squeeze" — 헤더에 라벨로 노출
    name: str = ""          # 회사명 — 헤더에 코드와 같이 노출

    @property
    def is_exit_signal(self) -> bool:
        return self.alert_type in (AlertType.TP_HIT, AlertType.SL_HIT)

    def format_message(self) -> str:
        coach = ""
        if self.alert_type == AlertType.TP_HIT:
            icon, title = "🎯", "목표가 도달 — 익절 체결"
        elif self.alert_type == AlertType.SL_HIT:
            icon, title = "🛑", "손절가 도달 — 손절 체결"
        elif self.alert_type == AlertType.NEAR_TP:
            icon, title = "📈", "목표가 근접 — 익절 준비"
            coach = "💡 코치: 추세 약화 시 조기 익절도 한 방법입니다."
        else:
            icon, title = "🛑", "손절가 근접 — 즉시 대응 권유"
            coach = "💡 코치: 매도세가 강해지면 SL 도달 전에 빠지는 것도 전략."

        mode_suffix = ""
        if self.strategy_mode:
            mode_label = _MODE_LABEL_KR.get(self.strategy_mode, self.strategy_mode)
            mode_suffix = f"  ·  [{mode_label}]"

        head = f"{self.code} {self.name}" if self.name else self.code
        body = (
            f"{icon} {title}  —  {head}{mode_suffix}\n"
            f"\n"
            f"  현재가   {self.current_price:,}원  ({self.return_pct:+.2f}%)\n"
            f"  고가     {self.high_today:,}원  /  저가  {self.low_today:,}원\n"
            f"\n"
            f"  📌 매수가  {self.entry_price:,}원\n"
            f"  🎯 목표가  {self.target_price:,}원\n"
            f"  🛑 손절가  {self.stop_price:,}원"
        )
        return body + (f"\n\n{coach}" if coach else "")


class PriceMonitor:
    """보유 포지션의 TP/SL 도달을 감시."""

    def __init__(self, kis: KISMarketDataSource | None = None) -> None:
        self.kis = kis or KISMarketDataSource()
        self._alerted: set[tuple[int, str]] = set()  # (position_id, alert_type) 중복 방지

    def check_positions(self, chat_id: int) -> list[PriceAlert]:
        """chat_id의 모든 보유 포지션 현재가를 체크하고, 알림 대상 반환."""
        positions = get_open_positions(chat_id)
        if not positions:
            return []

        # 사용자 설정 — early_take_profit ON 이면 day-TP 도 별도로 체크
        early_tp = False
        try:
            from src.services import user_service as _us
            _u = _us.get_user(chat_id)
            early_tp = bool(_u and _u.early_take_profit)
        except Exception:
            pass

        codes = list({p.code for p in positions})
        prices = self.kis.fetch_current_prices(codes)

        alerts: list[PriceAlert] = []
        now = datetime.now()
        # 한 사이클 안에서 같은 code 의 exit signal 은 1건만 (동일 코드 다중 lot 보호 — race + 노이즈 방지)
        exit_codes_this_pass: set[str] = set()

        for pos in positions:
            cp = prices.get(pos.code)
            if cp is None:
                log.warning("[%s] 현재가 조회 실패, 스킵", pos.code)
                continue

            alert = self._evaluate_position(pos, cp, now, early_take_profit=early_tp)
            if alert is None:
                continue

            if alert.is_exit_signal:
                # exit 알림은 (position_id, type) 기록 X — 매도 실패 시 다음 사이클에 재시도 가능해야 함.
                # 대신 같은 코드 다른 lot 은 한 사이클에 하나만 알리고 나머지는 다음 사이클로 미룸.
                if pos.code in exit_codes_this_pass:
                    log.info("[%s] 동일 코드 exit signal 다중 lot — 다음 사이클로 미룸", pos.code)
                    continue
                exit_codes_this_pass.add(pos.code)
            else:
                # 근접 알림(NEAR_*)은 (position_id, type) 단위로 sticky dedupe
                key = (pos.position_id, alert.alert_type.value)
                if key in self._alerted:
                    continue
                self._alerted.add(key)
            alerts.append(alert)

        return alerts

    # 매수 직후 GRACE_MINUTES 동안은 cp.low/cp.high 가 아닌 cp.price (현재가) 만으로 exit 판정.
    # 이유: cp.low/high 는 당일 전체 OHLC — 매수 전에 찍힌 저가가 SL 보다 낮으면 매수 직후 즉시 자동매도 발생 (2026-04-28 007340 사고).
    # 5~10분이면 1~3 cycle 만 지연 — 진짜 SL/TP 도달은 이후 사이클에 정확히 잡힘.
    GRACE_MINUTES_AFTER_BUY = 10

    def _evaluate_position(
        self, pos: OpenPositionDTO, cp: CurrentPrice, now: datetime,
        early_take_profit: bool = False,
    ) -> PriceAlert | None:
        return_pct = (cp.price - pos.buy_price) / pos.buy_price * 100

        # early_take_profit ON 시 day-TP 도 평가 — 더 낮은(=먼저 도달하는) target 우선
        # day_tp_pct: bunt=3, squeeze=5 (MODE_PARAMS). swing 모드 포지션이라도 짧은 익절 이용.
        effective_target = pos.target_price
        if early_take_profit:
            from src.risk.guard import MODE_PARAMS, StrategyMode, align_to_tick
            try:
                day_pct = MODE_PARAMS[StrategyMode(pos.strategy_mode)]["tp_pct"]
                day_tp = align_to_tick(
                    pos.buy_price * (100 + day_pct) // 100, "down",
                )
                # day_tp 가 swing target 보다 낮을 때만 의미 — 더 빠른 익절
                if day_tp < pos.target_price:
                    effective_target = day_tp
            except Exception:
                pass

        base = dict(
            code=pos.code,
            position_id=pos.position_id,
            entry_price=pos.buy_price,
            target_price=effective_target,
            stop_price=pos.stop_price,
            current_price=cp.price,
            high_today=cp.high,
            low_today=cp.low,
            return_pct=return_pct,
            checked_at=now,
            strategy_mode=pos.strategy_mode,
            name=pos.name,
        )

        # TP/SL 판정: 매수 후 grace 기간엔 현재가만 (당일 OHLC 무시), 이후엔 당일 고가/저가 기준
        try:
            opened_at = datetime.fromisoformat(pos.opened_at)
            in_grace = (now - opened_at).total_seconds() < self.GRACE_MINUTES_AFTER_BUY * 60
        except Exception:
            in_grace = False

        ref_high = cp.price if in_grace else cp.high
        ref_low = cp.price if in_grace else cp.low

        if ref_high >= effective_target:
            return PriceAlert(alert_type=AlertType.TP_HIT, **base)
        if ref_low <= pos.stop_price:
            return PriceAlert(alert_type=AlertType.SL_HIT, **base)

        # 근접 경고 (90% / 110%) — effective_target 기준 (early_tp ON 시 day-TP)
        if effective_target > pos.buy_price:
            tp_dist = (effective_target - cp.price) / (effective_target - pos.buy_price)
        else:
            tp_dist = 1.0
        sl_dist = (cp.price - pos.stop_price) / (pos.buy_price - pos.stop_price)

        if 0 < tp_dist <= 0.3:  # 목표가까지 30% 이내
            return PriceAlert(alert_type=AlertType.NEAR_TP, **base)
        if 0 < sl_dist <= 0.3:  # 손절가까지 30% 이내
            return PriceAlert(alert_type=AlertType.NEAR_SL, **base)

        return None

    def reset_alerts(self) -> None:
        """알림 이력 초기화 (매일 장 시작 시 호출)."""
        self._alerted.clear()


# ============================================================
# PendingRecMonitor — 추천 종목 진입가 하락 알림 (3루 코치 A)
# ============================================================

@dataclass
class PendingRecAlert:
    """미매수 추천 종목이 진입가보다 더 좋은 가격으로 거래 중임을 알림."""
    rec_id: str
    code: str
    strategy_mode: str
    entry_price: int        # 추천 시 진입가
    target_price: int
    stop_price: int
    current_price: int
    discount_pct: float     # (entry - current) / entry * 100, 양수일수록 더 싸짐
    ensemble_score: float
    session_date: str
    name: str = ""          # 회사명


class PendingRecMonitor:
    """오늘+어제 추천 중 아직 매수 안 한 종목이 추천 진입가에 도달(current ≤ entry)하면 알림.

    - rec당 1회만 알림 — in-memory + audit_log 기반 dedup (봇 재시작에도 유지)
    - 자동 매수 없음 — 알림에 매수 버튼만
    - 너무 많이 떨어진 종목(추세 하락)은 제외 (-MAX_DROP_PCT 초과 X)
    """

    MAX_DROP_PCT = 5.0   # 진입가보다 5% 이상 빠진 건 추세 하락 위험 → 알림 제외

    def __init__(self, kis: KISMarketDataSource | None = None) -> None:
        self.kis = kis or KISMarketDataSource()
        self._alerted: set[str] = set()  # rec_id 단위 (in-memory)

    @staticmethod
    def _get_alerted_today(chat_id: int) -> set[str]:
        """audit_log 에서 오늘 pending_rec_alert 발송된 rec_id set 반환.
        봇 재시작 시 in-memory set 가 리셋되어도 중복 알림 방지."""
        from src.db.connection import get_connection
        import json as _json
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT payload_json FROM audit_log
                   WHERE event_type='pending_rec_alert'
                     AND chat_id=?
                     AND date(ts)=date('now','+9 hours')""",
                (chat_id,),
            ).fetchall()
        finally:
            conn.close()
        out: set[str] = set()
        for r in rows:
            try:
                p = _json.loads(r[0] or "{}")
                if "rec_id" in p:
                    out.add(p["rec_id"])
            except Exception:
                continue
        return out

    def check_pending_recs(self, chat_id: int, days: int = 1) -> list[PendingRecAlert]:
        from src.services.recommendation_service import get_unbought_recent_recs
        recs = get_unbought_recent_recs(chat_id, days=days)
        if not recs:
            return []

        # in-memory set + audit_log 기반 union dedup
        already_alerted = self._alerted | self._get_alerted_today(chat_id)
        recs = [r for r in recs if r["rec_id"] not in already_alerted]
        if not recs:
            return []

        codes = list({r["code"] for r in recs})
        prices = self.kis.fetch_current_prices(codes)

        alerts: list[PendingRecAlert] = []
        for r in recs:
            cp = prices.get(r["code"])
            if cp is None:
                continue
            entry = r["entry_price"]
            if entry <= 0:
                continue
            discount = (entry - cp.price) / entry * 100  # 양수 = 진입가보다 싸짐
            # 진입가 도달 (current ≤ entry, 즉 discount ≥ 0) + 너무 많이 빠지지 않음
            if not (0 <= discount <= self.MAX_DROP_PCT):
                continue
            self._alerted.add(r["rec_id"])
            alerts.append(PendingRecAlert(
                rec_id=r["rec_id"],
                code=r["code"],
                strategy_mode=r["strategy_mode"],
                entry_price=entry,
                target_price=r["target_price"],
                stop_price=r["stop_price"],
                current_price=cp.price,
                discount_pct=round(discount, 2),
                ensemble_score=r["ensemble_score"],
                session_date=r["session_date"],
                name=r.get("name", ""),
            ))
        return alerts

    def reset_alerts(self) -> None:
        self._alerted.clear()
