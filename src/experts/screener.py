"""종목 스크리너 — 여러 종목 평가 후 상위 후보 반환.

시드 제약(활성 시드 × per_position_cap 비율 ≤ 1주 매수 가능) 필터 포함.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.experts.base import ExpertOpinion
from src.experts.technical import TechnicalExpert
from src.indicators import compute_all, load_ohlcv
from src.risk.guard import PER_POSITION_CAP_PCT


@dataclass
class ScreenResult:
    opinion: ExpertOpinion
    last_close: int
    estimated_quantity: int     # 종목당 한도로 살 수 있는 주수


def screen(
    codes: list[str],
    active_seed_krw: int,
    top_n: int = 5,
    min_score: float = 50.0,
) -> list[ScreenResult]:
    """여러 종목 스코어링 후 상위 top_n 반환.

    시드 제약:
      - 1주 가격 > 종목당 한도 → 탈락 (1주도 못 삼)
      - min_score 미달 → 탈락
    """
    per_position_cap = active_seed_krw * PER_POSITION_CAP_PCT // 100
    expert = TechnicalExpert()
    results: list[ScreenResult] = []

    for code in codes:
        df = load_ohlcv(code)
        if df.empty or len(df) < 60:
            continue

        enriched = compute_all(df)
        opinion = expert.evaluate(code, enriched)

        if not opinion.is_valid or opinion.score < min_score:
            continue

        last_close = int(df["close"].iloc[-1])
        if last_close <= 0 or last_close > per_position_cap:
            # 1주도 못 사는 종목은 제외
            continue

        qty = per_position_cap // last_close
        if qty < 1:
            continue

        results.append(ScreenResult(
            opinion=opinion,
            last_close=last_close,
            estimated_quantity=qty,
        ))

    results.sort(key=lambda r: r.opinion.score, reverse=True)
    return results[:top_n]
