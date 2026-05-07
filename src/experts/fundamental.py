"""재무제표 전문가.

단타/스윙 관점의 재무 평가:
  - 부실/적자 기업을 **하드 필터링**해서 제외 (가장 중요)
  - 안정적 재무(PER/PBR/ROE 양호) → 번트 선호
  - 성장성·고평가 → 스퀴즈 선호 (변동성 큼)
  - 관리종목·투자주의는 is_warning/is_watch 플래그로 하드 컷

2단계 가중치 구조:
  - 1단계 (앙상블): EnsembleWeights 에서 이 전문가 자체의 비중 결정
  - 2단계 (시그널): FundamentalWeights 에서 PER/PBR/ROE/시총 구간별 점수 결정
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.db.connection import get_connection
from src.experts.base import ExpertOpinion, Signal


@dataclass(frozen=True)
class FundamentalSnapshot:
    per: float | None
    pbr: float | None
    roe: float | None           # %
    market_cap: int | None      # 원
    is_warning: bool
    is_watch: bool

    @property
    def has_data(self) -> bool:
        return any(v is not None for v in (self.per, self.pbr, self.roe))


# ============================================================
# 시그널별 가중치 설정
# ============================================================

@dataclass
class FundamentalWeights:
    """재무 전문가 시그널별 점수. 0으로 두면 해당 시그널 비활성화."""

    # --- PER (25점 만점) ---
    per_low: float = 25           # PER ≤ 10 (저PER 우량)
    per_fair: float = 18          # PER 10~20 (적정)
    per_mid: float = 10           # PER 20~30 (보통)
    per_high: float = 3           # PER 30~50 (높음)
    per_overheat: float = 0       # PER > 50 (과열)

    # --- PBR (20점 만점) ---
    pbr_deep_value: float = 15    # PBR < 0.5 (초저)
    pbr_fair: float = 20          # PBR 0.5~1.5 (적정)
    pbr_mid: float = 10           # PBR 1.5~3.0 (보통)
    pbr_high: float = 3           # PBR 3.0~5.0 (높음)
    pbr_overheat: float = 0       # PBR > 5.0 (과열)

    # --- ROE (25점 만점) ---
    roe_high: float = 25          # ROE ≥ 15%
    roe_good: float = 18          # ROE 10~15%
    roe_mid: float = 10           # ROE 5~10%
    roe_low: float = 3            # ROE 0~5%
    roe_negative: float = 0       # ROE ≤ 0 (적자)

    # --- 시가총액/유동성 (15점 만점) ---
    cap_large: float = 15         # 10조+
    cap_mid_large: float = 13     # 1조+
    cap_mid: float = 10           # 1000억+
    cap_small: float = 5          # 300억+
    cap_micro: float = 0          # 300억 미만


DEFAULT_FUND_WEIGHTS = FundamentalWeights()


class FundamentalExpert:
    name = "fundamental"

    def __init__(self, weights: FundamentalWeights | None = None) -> None:
        self.w = weights or DEFAULT_FUND_WEIGHTS

    # ------------------------------------------------------------
    # DB 로더 (evaluate 의 통합 진입점)
    # ------------------------------------------------------------
    def evaluate(self, code: str, as_of: date | None = None) -> ExpertOpinion:
        snap = self._load_snapshot(code, as_of)
        if snap is None:
            return ExpertOpinion(
                code=code, expert=self.name, score=0,
                error="재무 스냅샷 없음",
            )
        return self.evaluate_snapshot(code, snap)

    # ------------------------------------------------------------
    # 순수 함수 (테스트에서 직접 호출)
    # ------------------------------------------------------------
    def evaluate_snapshot(self, code: str, snap: FundamentalSnapshot) -> ExpertOpinion:
        # 하드 필터 먼저
        if snap.is_warning:
            return ExpertOpinion(
                code=code, expert=self.name, score=0,
                reason_summary="관리종목 — 단타 제외",
                error="관리종목",
            )
        if snap.is_watch:
            return ExpertOpinion(
                code=code, expert=self.name, score=0,
                reason_summary="투자주의 — 단타 제외",
                error="투자주의",
            )
        if not snap.has_data:
            return ExpertOpinion(
                code=code, expert=self.name, score=0,
                error="재무 지표 없음",
            )

        signals: list[Signal] = []

        signals += self._score_per(snap.per)
        signals += self._score_pbr(snap.pbr)
        signals += self._score_roe(snap.roe)
        signals += self._score_market_cap(snap.market_cap)

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
    # 하드 필터 (다른 모듈에서 사전 필터로 호출 가능)
    # ------------------------------------------------------------
    def is_acceptable(self, code: str, as_of: date | None = None) -> tuple[bool, str]:
        snap = self._load_snapshot(code, as_of)
        if snap is None:
            return True, ""          # 데이터 없으면 관대하게 통과
        if snap.is_warning:
            return False, "관리종목"
        if snap.is_watch:
            return False, "투자주의"
        if snap.roe is not None and snap.roe < -20:
            return False, f"ROE {snap.roe:.1f}% — 적자 심각"
        if snap.per is not None and snap.per < 0:
            return False, f"적자 (PER {snap.per:.1f})"
        return True, ""

    # ------------------------------------------------------------
    # 내부 스코어러
    # ------------------------------------------------------------

    def _score_per(self, per: float | None) -> list[Signal]:
        w = self.w
        if per is None:
            return []
        if per <= 0:
            return [Signal("적자 기업 (PER ≤ 0)", 0, f"PER {per:.1f} — 비권장")]
        if per <= 10:
            return [Signal("저PER 우량", w.per_low, f"PER {per:.1f}")]
        if per <= 20:
            return [Signal("적정 PER", w.per_fair, f"PER {per:.1f}")]
        if per <= 30:
            return [Signal("PER 보통", w.per_mid, f"PER {per:.1f}")]
        if per <= 50:
            return [Signal("PER 높음", w.per_high, f"PER {per:.1f} — 고평가 주의")]
        return [Signal("PER 과열", w.per_overheat, f"PER {per:.1f} — 버블 우려")]

    def _score_pbr(self, pbr: float | None) -> list[Signal]:
        w = self.w
        if pbr is None or pbr <= 0:
            return []
        if pbr < 0.5:
            return [Signal("초저PBR (가치주)", w.pbr_deep_value, f"PBR {pbr:.2f}")]
        if pbr <= 1.5:
            return [Signal("적정 PBR", w.pbr_fair, f"PBR {pbr:.2f}")]
        if pbr <= 3.0:
            return [Signal("PBR 보통", w.pbr_mid, f"PBR {pbr:.2f}")]
        if pbr <= 5.0:
            return [Signal("PBR 높음", w.pbr_high, f"PBR {pbr:.2f}")]
        return [Signal("PBR 과열", w.pbr_overheat, f"PBR {pbr:.2f} — 고평가")]

    def _score_roe(self, roe: float | None) -> list[Signal]:
        w = self.w
        if roe is None:
            return []
        if roe >= 15:
            return [Signal("고ROE", w.roe_high, f"ROE {roe:.1f}%")]
        if roe >= 10:
            return [Signal("양호 ROE", w.roe_good, f"ROE {roe:.1f}%")]
        if roe >= 5:
            return [Signal("보통 ROE", w.roe_mid, f"ROE {roe:.1f}%")]
        if roe > 0:
            return [Signal("ROE 낮음", w.roe_low, f"ROE {roe:.1f}%")]
        return [Signal("적자 (ROE ≤ 0)", w.roe_negative, f"ROE {roe:.1f}% — 제외 권고")]

    def _score_market_cap(self, market_cap: int | None) -> list[Signal]:
        w = self.w
        if market_cap is None:
            return [Signal("시총 정보 없음", 5, "중립")]
        if market_cap >= 10_000_000_000_000:       # 10조+
            return [Signal("대형주", w.cap_large, f"시총 {market_cap/1e12:.1f}조")]
        if market_cap >= 1_000_000_000_000:        # 1조+
            return [Signal("중대형주", w.cap_mid_large, f"시총 {market_cap/1e12:.2f}조")]
        if market_cap >= 100_000_000_000:          # 1000억+
            return [Signal("중형주", w.cap_mid, f"시총 {market_cap/1e8:.0f}억")]
        if market_cap >= 30_000_000_000:           # 300억+
            return [Signal("소형주", w.cap_small, f"시총 {market_cap/1e8:.0f}억 — 유동성 주의")]
        return [Signal("초소형주", w.cap_micro, f"시총 {market_cap/1e8:.0f}억 — 위험")]

    # ------------------------------------------------------------
    # 번트/스퀴즈 모드 적합도
    # ------------------------------------------------------------
    def _compute_mode_fit(self, snap: FundamentalSnapshot) -> tuple[float, float]:
        bunt, squeeze = 0.5, 0.5

        # 안정적 재무 → 번트
        if snap.per is not None and 0 < snap.per <= 20:
            bunt += 0.2
        if snap.pbr is not None and 0 < snap.pbr <= 1.5:
            bunt += 0.15
        if snap.roe is not None and snap.roe >= 10:
            bunt += 0.2

        # 고평가/공격적 → 스퀴즈
        if snap.per is not None and snap.per > 30:
            squeeze += 0.2
        if snap.pbr is not None and snap.pbr > 3:
            squeeze += 0.15

        # 소형주는 변동성 큼 → 스퀴즈
        if snap.market_cap is not None and snap.market_cap < 100_000_000_000:
            squeeze += 0.15
            bunt -= 0.1

        return max(0.0, min(1.0, bunt)), max(0.0, min(1.0, squeeze))

    # ------------------------------------------------------------
    # DB 조회
    # ------------------------------------------------------------
    def _load_snapshot(self, code: str, as_of: date | None = None) -> FundamentalSnapshot | None:
        conn = get_connection()
        try:
            if as_of:
                q = """SELECT per, pbr, roe, market_cap, is_warning, is_watch
                       FROM fundamentals_snapshot
                       WHERE code = ? AND snapshot_date <= ?
                       ORDER BY snapshot_date DESC LIMIT 1"""
                row = conn.execute(q, (code, as_of.isoformat())).fetchone()
            else:
                q = """SELECT per, pbr, roe, market_cap, is_warning, is_watch
                       FROM fundamentals_snapshot
                       WHERE code = ?
                       ORDER BY snapshot_date DESC LIMIT 1"""
                row = conn.execute(q, (code,)).fetchone()

            if not row:
                return None

            return FundamentalSnapshot(
                per=row["per"],
                pbr=row["pbr"],
                roe=row["roe"],
                market_cap=row["market_cap"],
                is_warning=bool(row["is_warning"]),
                is_watch=bool(row["is_watch"]),
            )
        finally:
            conn.close()
