"""코인 expert 프레임워크 — KR 봇 src/experts/base.py 패턴 포팅.

각 expert 는 (df + 컨텍스트) → ExpertSignal 점수(-1.0~+1.0)를 반환.
앙상블은 가중 합산 후 임계치(예: +0.5) 이상에서 매수.

KR 과 차이:
  - score 가 0~100 이 아니라 -1.0 ~ +1.0 (양/음 방향성 명시)
  - 컨텍스트에 fng (Fear & Greed) / kp (김치프리미엄) 포함 가능
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from src.coin.signals import _rsi, _ema


@dataclass
class CoinExpertSignal:
    """expert 한 명의 평가 결과.

    score: -1.0 (강한 매도) ~ 0 (중립) ~ +1.0 (강한 매수)
    weight: 앙상블 시 곱해질 가중치 (전문가 신뢰도)
    reason: 디버깅용 요약
    """
    expert: str
    score: float
    weight: float = 1.0
    reason: str = ""


class CoinExpert(ABC):
    """모든 코인 expert 의 추상 base."""
    name: str = "abstract"
    weight: float = 1.0   # 앙상블 가중치 (튜닝 대상)

    @abstractmethod
    def evaluate(self, df: pd.DataFrame, ctx: dict) -> CoinExpertSignal:
        """현재 캔들(df 마지막 행) 기준 평가.

        ctx 예: {"fng": 32, "kp_pct": 1.5, "kp_avg_7d": 2.1, ...}
        """
        ...


# ============================================================
# 1) Technical Expert — RSI + EMA + 거래량 (signals.py 의 momentum 재활용)
# ============================================================

class CoinTechnicalExpert(CoinExpert):
    name = "technical"
    weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> CoinExpertSignal:
        if len(df) < 30:
            return CoinExpertSignal(self.name, 0.0, self.weight, "데이터 부족")

        close = df["close"]
        volume = df["volume"]
        rsi = _rsi(close, 14)
        ema12 = _ema(close, 12)
        ema26 = _ema(close, 26)

        rsi_now = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-2])
        ema12_now = float(ema12.iloc[-1])
        ema26_now = float(ema26.iloc[-1])
        avg_vol_20 = float(volume.iloc[-21:-1].mean())
        vol_ratio = float(volume.iloc[-1] / max(avg_vol_20, 1e-10))

        # 점수 산정 (0 중립)
        score = 0.0
        reasons = []

        # 1. RSI 과매도 회복 → +0.4
        if rsi_prev <= 35 and rsi_now > rsi_prev:
            score += 0.4
            reasons.append(f"RSI{rsi_prev:.0f}→{rsi_now:.0f}")
        # 2. RSI 과매수 → -0.3
        elif rsi_now >= 75:
            score -= 0.3
            reasons.append(f"RSI과매수{rsi_now:.0f}")

        # 3. EMA 우상향 → +0.3
        if ema12_now > ema26_now:
            score += 0.3
            reasons.append("EMA↑")
        else:
            score -= 0.2
            reasons.append("EMA↓")

        # 4. 거래량 폭발 → +0.3
        if vol_ratio >= 1.5:
            score += 0.3
            reasons.append(f"vol{vol_ratio:.1f}x")
        elif vol_ratio <= 0.5:
            score -= 0.1

        # clip
        score = max(-1.0, min(1.0, score))
        return CoinExpertSignal(self.name, score, self.weight, "·".join(reasons))


# ============================================================
# 2) Sentiment Expert — Fear & Greed Index
# ============================================================

class CoinSentimentExpert(CoinExpert):
    """공포·탐욕 기반.

    역추세 패턴 — 시장이 과도하게 공포일 때 매수, 탐욕일 때 매도.
    역사적으로 BTC 대형 바닥은 FNG <= 20 근처에서 형성.
    """
    name = "sentiment"
    weight = 0.8

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> CoinExpertSignal:
        fng = ctx.get("fng")
        if fng is None:
            return CoinExpertSignal(self.name, 0.0, self.weight, "FNG 미제공")

        # FNG 0~100 을 -1~+1 점수로 매핑 (역방향)
        # FNG 25 → +0.5 (Fear, 매수 유리)
        # FNG 50 → 0 (중립)
        # FNG 75 → -0.5 (Greed, 매수 회피)
        score = (50 - fng) / 50.0
        score = max(-1.0, min(1.0, score))
        if fng <= 25:
            label = "Extreme Fear"
        elif fng <= 50:
            label = "Fear"
        elif fng <= 75:
            label = "Greed"
        else:
            label = "Extreme Greed"
        return CoinExpertSignal(self.name, score, self.weight, f"FNG={fng}({label})")


# ============================================================
# 3) Arbitrage Expert — 김치 프리미엄
# ============================================================

class CoinArbitrageExpert(CoinExpert):
    """김치 프리미엄이 평균보다 낮으면 매수 우호 (한국 시장 underpriced).

    역사적으로 김프 < 평균 - 1σ 일 때 한국 차익매수 압력 들어옴.
    김프 > 5% 면 매수 회피 (조정 잦음).
    """
    name = "arbitrage"
    weight = 0.6

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> CoinExpertSignal:
        kp = ctx.get("kp_pct")
        kp_avg = ctx.get("kp_avg_7d")
        kp_std = ctx.get("kp_std_7d")
        if kp is None:
            return CoinExpertSignal(self.name, 0.0, self.weight, "KP 미제공")

        # 절대값 가드 — 김프 5% 이상은 매수 회피
        if kp >= 5.0:
            return CoinExpertSignal(
                self.name, -0.5, self.weight, f"KP과열{kp:.2f}%",
            )

        # 평균/std 있으면 z-score 기반
        if kp_avg is not None and kp_std and kp_std > 0:
            z = (kp - kp_avg) / kp_std
            # z = -2 → +0.6 (평균보다 낮음, underpriced)
            # z = +2 → -0.6 (평균보다 높음, overpriced)
            score = max(-0.8, min(0.8, -z * 0.3))
            return CoinExpertSignal(
                self.name, score, self.weight,
                f"KP={kp:.2f}% (z={z:+.1f})",
            )

        # 단순 임계 — 평균 없으면 0.5% 기준
        if kp <= 0.5:
            return CoinExpertSignal(self.name, +0.3, self.weight, f"KP낮음{kp:.2f}%")
        return CoinExpertSignal(self.name, 0.0, self.weight, f"KP={kp:.2f}%")


# ============================================================
# 앙상블
# ============================================================

@dataclass
class EnsembleResult:
    final_score: float
    threshold: float
    is_buy: bool
    expert_signals: list[CoinExpertSignal]


def ensemble_evaluate(
    df: pd.DataFrame,
    ctx: dict,
    experts: list[CoinExpert],
    threshold: float = 0.5,
) -> EnsembleResult:
    """expert 각자 평가 → 가중 평균 → threshold 비교.

    final_score = sum(score × weight) / sum(weight)
    """
    signals = [e.evaluate(df, ctx) for e in experts]
    if not signals:
        return EnsembleResult(0.0, threshold, False, [])

    total_w = sum(s.weight for s in signals) or 1.0
    weighted = sum(s.score * s.weight for s in signals) / total_w
    return EnsembleResult(
        final_score=weighted,
        threshold=threshold,
        is_buy=weighted >= threshold,
        expert_signals=signals,
    )
