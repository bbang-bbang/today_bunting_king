"""기술적 지표 기반 전문가.

compute_all() 로 산출된 49개 지표를 입력받아, 사전 정의 룰로 0~100 점 매기고
각 시그널에 대한 사용자 설명 텍스트를 생성한다.

2단계 가중치 구조:
  - 1단계 (앙상블): EnsembleWeights 에서 이 전문가 자체의 비중 결정
  - 2단계 (시그널): TechnicalWeights 에서 내부 시그널별 점수 결정

TechnicalWeights 기본값 (v2 — 2026-04-17 백테스트 기반 튜닝):
  추세:   SMA정배열 10 / 단기이평 5 / EMA 5 / 강한상승추세 10 / 약추세 3 / 이치모쿠 5
  모멘텀: MACD양전환 10 / MACD지속 12 / RSI계열 2~3 / Stoch 2
  변동성: BB스퀴즈 10 / BB상단돌파 8 / BB하단 2~3 / 낮은변동성 3
  거래량: OBV매집 8 / 거래량정상 5 / 급증 5 / MFI 3

번트/스퀴즈 모드 적합도:
  번트   : 낮은 변동성 + 명확한 추세 + 안정적 거래량
  스퀴즈 : BB bandwidth 수축 + 거래량 급증 = 돌파 임박
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.experts.base import ExpertOpinion, Signal

MIN_BARS_REQUIRED = 60
SCORE_CAP = 100.0


def _safe(val, default=float("nan")):
    if val is None:
        return default
    if isinstance(val, float) and val != val:
        return default
    return val


# ============================================================
# 시그널별 가중치 설정
# ============================================================

@dataclass
class TechnicalWeights:
    """기술 전문가 시그널별 점수. 0으로 두면 해당 시그널 비활성화."""

    # --- 추세 ---
    sma_aligned: float = 10       # SMA 5>20>60 정배열
    sma_short_up: float = 5       # SMA5 > SMA20 (정배열 아닐 때)
    ema_cross: float = 5          # EMA 20 > 60
    strong_trend: float = 10      # +DI > -DI & ADX > 20
    weak_trend: float = 3         # +DI > -DI (ADX 약함)
    ichimoku_above: float = 5     # 종가 > 이치모쿠 구름

    # --- 모멘텀 ---
    rsi_oversold: float = 3       # RSI 25~40
    rsi_neutral_bull: float = 3   # RSI 40~55
    rsi_bull: float = 2           # RSI 55~68
    macd_cross: float = 10        # MACD 히스토그램 음→양 전환
    macd_rising: float = 12       # MACD 히스토그램 양수 & 상승 중
    stoch_normal: float = 2       # Stochastic 20~80

    # --- 변동성 ---
    bb_lower: float = 3           # 볼린저 %B 0~0.25
    bb_mid_lower: float = 2       # 볼린저 %B 0.25~0.50
    bb_upper_break: float = 8     # 볼린저 %B > 1.0 상단 돌파
    bb_squeeze: float = 10        # 볼린저 bandwidth 수축
    low_volatility: float = 3     # ATR < 2.5%

    # --- 거래량 ---
    vol_surge: float = 5          # 거래량비 1.5~3.0배
    vol_overheat: float = 1       # 거래량비 3.0~5.0배
    vol_normal: float = 5         # 거래량비 0.8~1.5배
    obv_rising: float = 8         # OBV 10일 상승
    mfi_normal: float = 3         # MFI 40~70


# 기본 가중치 프리셋
DEFAULT_WEIGHTS = TechnicalWeights()


class TechnicalExpert:
    """규칙 기반 기술 전문가. compute_all() 결과를 입력으로 받음."""

    name = "technical"

    def __init__(self, weights: TechnicalWeights | None = None) -> None:
        self.w = weights or DEFAULT_WEIGHTS

    def evaluate(self, code: str, enriched: pd.DataFrame, as_of=None) -> ExpertOpinion:
        _ = as_of
        if enriched is None or enriched.empty or len(enriched) < MIN_BARS_REQUIRED:
            return ExpertOpinion(
                code=code, expert=self.name, score=0.0,
                error=f"데이터 부족 (최소 {MIN_BARS_REQUIRED}일 필요)",
            )

        latest = enriched.iloc[-1]
        prev = enriched.iloc[-2]
        signals: list[Signal] = []

        signals += self._score_trend(latest)
        signals += self._score_momentum(latest, prev)
        signals += self._score_volatility(latest, enriched)
        signals += self._score_volume(latest, enriched)

        bunt_fit, squeeze_fit = self._compute_mode_fit(latest)
        fit_bonus = max(bunt_fit, squeeze_fit) * 10
        signals.append(Signal(
            name="모드 적합도",
            score=fit_bonus,
            detail=f"번트 {bunt_fit:.2f} / 스퀴즈 {squeeze_fit:.2f}",
        ))

        total = min(SCORE_CAP, sum(s.score for s in signals))
        reason = " · ".join(
            f"{s.name}(+{s.score:.0f})" for s in signals if s.score >= 3
        )

        return ExpertOpinion(
            code=code, expert=self.name, score=total,
            signals=signals,
            mode_fit={"bunt": bunt_fit, "squeeze": squeeze_fit},
            reason_summary=reason[:200],
        )

    # ------------------------------------------------------------
    # 카테고리별 스코어러
    # ------------------------------------------------------------

    def _score_trend(self, latest: pd.Series) -> list[Signal]:
        w = self.w
        out: list[Signal] = []

        sma5 = _safe(latest.get("sma_5"))
        sma20 = _safe(latest.get("sma_20"))
        sma60 = _safe(latest.get("sma_60"))
        if sma5 == sma5 and sma20 == sma20 and sma60 == sma60:
            if sma5 > sma20 > sma60:
                out.append(Signal("SMA 정배열", w.sma_aligned, "5>20>60 단기 상승 정렬"))
            elif sma5 > sma20:
                out.append(Signal("단기 이평 상승", w.sma_short_up, f"SMA5 {sma5:.0f} > SMA20 {sma20:.0f}"))

        ema20 = _safe(latest.get("ema_20"))
        ema60 = _safe(latest.get("ema_60"))
        if ema20 == ema20 and ema60 == ema60 and ema20 > ema60:
            out.append(Signal("EMA 20 > 60", w.ema_cross, f"{ema20:.0f} > {ema60:.0f}"))

        plus_di = _safe(latest.get("plus_di"))
        minus_di = _safe(latest.get("minus_di"))
        adx_val = _safe(latest.get("adx"))
        if plus_di == plus_di and minus_di == minus_di:
            if plus_di > minus_di and adx_val == adx_val and adx_val > 20:
                out.append(Signal("강한 상승추세", w.strong_trend,
                                  f"+DI {plus_di:.1f} > -DI {minus_di:.1f} · ADX {adx_val:.1f}"))
            elif plus_di > minus_di:
                out.append(Signal("상승우세 약추세", w.weak_trend,
                                  f"+DI {plus_di:.1f} > -DI {minus_di:.1f}"))

        close = _safe(latest.get("close"))
        senkou_a = _safe(latest.get("ichi_senkou_a"))
        senkou_b = _safe(latest.get("ichi_senkou_b"))
        if close == close and senkou_a == senkou_a and senkou_b == senkou_b:
            cloud_top = max(senkou_a, senkou_b)
            if close > cloud_top:
                out.append(Signal("이치모쿠 구름 위", w.ichimoku_above, "전환선/기준선 위 돌파"))

        return out

    def _score_momentum(self, latest: pd.Series, prev: pd.Series) -> list[Signal]:
        w = self.w
        out: list[Signal] = []

        rsi = _safe(latest.get("rsi_14"))
        if rsi == rsi:
            if 25 <= rsi <= 40:
                out.append(Signal("RSI 과매도 구간", w.rsi_oversold, f"RSI {rsi:.1f} — 반등 불확실"))
            elif 40 < rsi <= 55:
                out.append(Signal("RSI 중립 강세", w.rsi_neutral_bull, f"RSI {rsi:.1f}"))
            elif 55 < rsi <= 68:
                out.append(Signal("RSI 강세", w.rsi_bull, f"RSI {rsi:.1f}"))

        macd_hist = _safe(latest.get("macd_hist"))
        prev_hist = _safe(prev.get("macd_hist"))
        if macd_hist == macd_hist and prev_hist == prev_hist:
            if macd_hist > 0 and prev_hist <= 0:
                out.append(Signal("MACD 히스토그램 양전환", w.macd_cross, "매수 전환 신호"))
            elif macd_hist > 0 and macd_hist > prev_hist:
                out.append(Signal("MACD 상승 지속", w.macd_rising, f"hist {macd_hist:.0f}"))

        stoch_k = _safe(latest.get("stoch_k"))
        if stoch_k == stoch_k:
            if 20 <= stoch_k <= 80:
                out.append(Signal("Stochastic 정상권", w.stoch_normal, f"%K {stoch_k:.1f}"))

        return out

    def _score_volatility(self, latest: pd.Series, enriched: pd.DataFrame) -> list[Signal]:
        w = self.w
        out: list[Signal] = []

        bb_pb = _safe(latest.get("bb_percent_b"))
        if bb_pb == bb_pb:
            if 0 <= bb_pb <= 0.25:
                out.append(Signal("볼린저 하단 근접", w.bb_lower, f"%B {bb_pb:.2f} — 추가 하락 위험"))
            elif 0.25 < bb_pb <= 0.50:
                out.append(Signal("볼린저 중립 하단", w.bb_mid_lower, f"%B {bb_pb:.2f}"))
            elif bb_pb > 1.0:
                out.append(Signal("볼린저 상단 돌파", w.bb_upper_break, f"%B {bb_pb:.2f} — 상승 모멘텀"))

        recent_bw = enriched["bb_bandwidth"].tail(20).dropna()
        if len(recent_bw) >= 10:
            bw_now = recent_bw.iloc[-1]
            bw_median = recent_bw.iloc[:-1].median()
            if bw_now < bw_median * 0.6:
                out.append(Signal("볼린저 스퀴즈", w.bb_squeeze,
                                  f"bandwidth {bw_now:.3f} vs 중앙값 {bw_median:.3f}"))

        atr14 = _safe(latest.get("atr_14"))
        close = _safe(latest.get("close"))
        if atr14 == atr14 and close == close and close > 0:
            atr_pct = atr14 / close * 100
            if atr_pct < 2.5:
                out.append(Signal("변동성 낮음 (번트 적합)", w.low_volatility, f"ATR {atr_pct:.1f}%"))

        return out

    def _score_volume(self, latest: pd.Series, enriched: pd.DataFrame) -> list[Signal]:
        w = self.w
        out: list[Signal] = []

        vr = _safe(latest.get("vol_ratio_20"))
        if vr == vr:
            if 1.5 <= vr <= 3.0:
                out.append(Signal("거래량 급증", w.vol_surge, f"{vr:.1f}배"))
            elif 3.0 < vr <= 5.0:
                out.append(Signal("거래량 과열 경계", w.vol_overheat,
                                  f"{vr:.1f}배 — 꼭지 위험"))
            elif 0.8 <= vr < 1.5:
                out.append(Signal("거래량 정상", w.vol_normal, f"{vr:.1f}배"))

        obv_series = enriched["obv"].tail(10).dropna()
        if len(obv_series) >= 5 and obv_series.iloc[-1] > obv_series.iloc[0]:
            out.append(Signal("OBV 상승 (매집)", w.obv_rising, "거래량 누적 양호"))

        mfi = _safe(latest.get("mfi_14"))
        if mfi == mfi:
            if 40 < mfi <= 70:
                out.append(Signal("MFI 정상권", w.mfi_normal, f"MFI {mfi:.1f}"))

        return out

    # ------------------------------------------------------------
    # 번트 / 스퀴즈 모드 적합도
    # ------------------------------------------------------------

    def _compute_mode_fit(self, latest: pd.Series) -> tuple[float, float]:
        bunt, squeeze = 0.5, 0.5

        bw = _safe(latest.get("bb_bandwidth"))
        if bw == bw:
            if bw < 0.05:
                squeeze += 0.3
                bunt += 0.05
            elif bw < 0.10:
                bunt += 0.2
            elif bw > 0.15:
                squeeze -= 0.15
                bunt -= 0.10

        adx_val = _safe(latest.get("adx"))
        if adx_val == adx_val and adx_val > 25:
            bunt += 0.20

        vr = _safe(latest.get("vol_ratio_20"))
        if vr == vr and 1.5 <= vr <= 3.0:
            squeeze += 0.15

        atr14 = _safe(latest.get("atr_14"))
        close = _safe(latest.get("close"))
        if atr14 == atr14 and close == close and close > 0:
            atr_pct = atr14 / close
            if atr_pct < 0.025:
                bunt += 0.1
            elif atr_pct > 0.05:
                squeeze += 0.1

        bunt = max(0.0, min(1.0, bunt))
        squeeze = max(0.0, min(1.0, squeeze))
        return bunt, squeeze
