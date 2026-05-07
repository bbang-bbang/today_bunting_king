"""흐름 전문가 — 투자자별 매매동향 + 가격 모멘텀.

입력 데이터:
  - investor_flow 테이블: 일자별 외인·기관·개인 순매수대금
  - ohlcv_daily: 최근 5영업일 종가 모멘텀 + 당일 거래대금

점수 배분 (총 100점 상한):
  외인 5일 순매수   25점
  기관 5일 순매수   20점
  외인 당일 순매수  15점
  기관 당일 순매수  10점
  5일 가격 모멘텀   15점
  외인+기관 동반    5점
  모드 적합도       10점
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from src.db.connection import get_connection
from src.experts.base import ExpertOpinion, Signal


@dataclass(frozen=True)
class FlowSnapshot:
    foreign_5d_net: int | None              # 외인 5영업일 순매수 합계 (원)
    institution_5d_net: int | None          # 기관 5영업일 순매수 합계 (원)
    foreign_today_net: int | None
    institution_today_net: int | None
    momentum_5d_pct: float | None           # 최근 5일 종가 변화율 %
    trading_value_today: int | None         # 당일 거래대금

    @property
    def has_flow_data(self) -> bool:
        return any(v is not None for v in (
            self.foreign_5d_net, self.institution_5d_net,
            self.foreign_today_net, self.institution_today_net,
        ))


class FlowExpert:
    name = "flow"

    # ------------------------------------------------------------
    def evaluate(self, code: str, as_of: date | None = None) -> ExpertOpinion:
        snap = self._load_snapshot(code, as_of)
        if snap is None:
            return ExpertOpinion(
                code=code, expert=self.name, score=0,
                error="흐름 데이터 없음",
            )
        return self.evaluate_snapshot(code, snap)

    # 순수 함수 (테스트용)
    def evaluate_snapshot(self, code: str, snap: FlowSnapshot) -> ExpertOpinion:
        if not snap.has_flow_data and snap.momentum_5d_pct is None:
            return ExpertOpinion(
                code=code, expert=self.name, score=0,
                error="수급·모멘텀 데이터 없음",
            )

        signals: list[Signal] = []
        signals += self._score_investor_5d("외인", snap.foreign_5d_net, max_pts=25)
        signals += self._score_investor_5d("기관", snap.institution_5d_net, max_pts=20)
        signals += self._score_investor_today("외인", snap.foreign_today_net, max_pts=15)
        signals += self._score_investor_today("기관", snap.institution_today_net, max_pts=10)
        signals += self._score_momentum(snap.momentum_5d_pct)
        signals += self._score_combo(snap)

        bunt_fit, squeeze_fit = self._compute_mode_fit(snap)
        signals.append(Signal(
            name="모드 적합도",
            score=max(bunt_fit, squeeze_fit) * 10,
            detail=f"번트 {bunt_fit:.2f} / 스퀴즈 {squeeze_fit:.2f}",
        ))

        total = min(100.0, max(0.0, sum(s.score for s in signals)))
        reason = " · ".join(
            f"{s.name}(+{s.score:.0f})" for s in signals if s.score >= 3
        )[:200]

        return ExpertOpinion(
            code=code, expert=self.name, score=total,
            signals=signals,
            mode_fit={"bunt": bunt_fit, "squeeze": squeeze_fit},
            reason_summary=reason,
        )

    # ------------------------------------------------------------
    # 세부 스코어러
    # ------------------------------------------------------------

    def _score_investor_5d(self, name: str, net: int | None, max_pts: int) -> list[Signal]:
        if net is None:
            return []
        if net <= 0:
            return [Signal(f"{name} 5일 순매도", 0, f"{net/1e8:+.1f}억 — 수급 이탈")]
        # 20억+ = max, 10억 = 70%, 5억 = 45%, 1억 = 20%, 미만 = 10%
        if net >= 2_000_000_000:
            score = max_pts
        elif net >= 1_000_000_000:
            score = max_pts * 0.7
        elif net >= 500_000_000:
            score = max_pts * 0.45
        elif net >= 100_000_000:
            score = max_pts * 0.2
        else:
            score = max_pts * 0.1
        return [Signal(f"{name} 5일 순매수", score, f"+{net/1e8:.1f}억")]

    def _score_investor_today(self, name: str, net: int | None, max_pts: int) -> list[Signal]:
        if net is None:
            return []
        if net <= 0:
            return []
        if net >= 500_000_000:
            score = max_pts
        elif net >= 100_000_000:
            score = max_pts * 0.6
        elif net >= 50_000_000:
            score = max_pts * 0.3
        else:
            score = max_pts * 0.1
        return [Signal(f"{name} 당일 순매수", score, f"+{net/1e8:.1f}억")]

    def _score_momentum(self, m: float | None) -> list[Signal]:
        if m is None:
            return []
        if m >= 10:
            return [Signal("5일 강한 상승", 15, f"+{m:.1f}%")]
        if m >= 5:
            return [Signal("5일 상승", 10, f"+{m:.1f}%")]
        if m >= 2:
            return [Signal("5일 완만 상승", 5, f"+{m:.1f}%")]
        if m <= -5:
            return [Signal("5일 급락", 0, f"{m:+.1f}% — 흐름 반전 대기")]
        return []

    def _score_combo(self, snap: FlowSnapshot) -> list[Signal]:
        fn = snap.foreign_5d_net or 0
        inn = snap.institution_5d_net or 0
        if fn > 0 and inn > 0:
            return [Signal("외인+기관 동반매수", 5, "쌍끌이 수급")]
        return []

    # ------------------------------------------------------------
    def _compute_mode_fit(self, snap: FlowSnapshot) -> tuple[float, float]:
        bunt, squeeze = 0.5, 0.5

        # 수급 급증 + 강한 모멘텀 → 스퀴즈
        if (snap.foreign_today_net or 0) > 1_000_000_000:
            squeeze += 0.2
        if (snap.institution_today_net or 0) > 500_000_000:
            squeeze += 0.15
        if snap.momentum_5d_pct is not None and snap.momentum_5d_pct >= 10:
            squeeze += 0.15

        # 꾸준한 수급 + 평온한 모멘텀 → 번트
        if (snap.foreign_5d_net or 0) > 0 and (snap.institution_5d_net or 0) > 0:
            m = snap.momentum_5d_pct
            if m is not None and 0 <= m < 5:
                bunt += 0.25

        return max(0.0, min(1.0, bunt)), max(0.0, min(1.0, squeeze))

    # ------------------------------------------------------------
    def _load_snapshot(self, code: str, as_of: date | None = None) -> FlowSnapshot | None:
        if as_of is None:
            as_of = date.today()

        conn = get_connection()
        try:
            lookback = (as_of - timedelta(days=14)).isoformat()
            flow_rows = conn.execute(
                """SELECT date, foreign_net, institution_net FROM investor_flow
                   WHERE code = ? AND date <= ? AND date >= ?
                   ORDER BY date DESC LIMIT 5""",
                (code, as_of.isoformat(), lookback),
            ).fetchall()

            # ohlcv 에서 5일 모멘텀 + 당일 거래대금
            ohlcv_rows = conn.execute(
                """SELECT date, close, value FROM ohlcv_daily
                   WHERE code = ? AND date <= ?
                   ORDER BY date DESC LIMIT 6""",
                (code, as_of.isoformat()),
            ).fetchall()
        finally:
            conn.close()

        if not flow_rows and not ohlcv_rows:
            return None

        foreign_5d = sum((r[1] or 0) for r in flow_rows) if flow_rows else None
        institution_5d = sum((r[2] or 0) for r in flow_rows) if flow_rows else None
        foreign_today = flow_rows[0][1] if flow_rows else None
        institution_today = flow_rows[0][2] if flow_rows else None

        momentum = None
        if len(ohlcv_rows) >= 6:
            today_close = ohlcv_rows[0][1]
            five_ago = ohlcv_rows[5][1]
            if five_ago and five_ago > 0:
                momentum = (today_close - five_ago) / five_ago * 100

        trading_value = ohlcv_rows[0][2] if ohlcv_rows else None

        return FlowSnapshot(
            foreign_5d_net=foreign_5d,
            institution_5d_net=institution_5d,
            foreign_today_net=foreign_today,
            institution_today_net=institution_today,
            momentum_5d_pct=momentum,
            trading_value_today=trading_value,
        )
