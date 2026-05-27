"""종목 추천 엔진 — 앙상블 기반 상위 N 선정.

Telegram 봇의 /추천 명령과 연결될 진입점.
시드 제약 필터 + 앙상블 점수 + 모드 적합도 정렬.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.ensemble.scorer import EnsembleOpinion, EnsembleScorer
from src.indicators import compute_all, load_ohlcv
from src.risk.guard import PER_POSITION_CAP_PCT


@dataclass
class Recommendation:
    opinion: EnsembleOpinion
    last_close: int
    estimated_quantity: int
    order_value: int
    target_price: int
    stop_price: int

    def summary_line(self) -> str:
        op = self.opinion
        return (
            f"[{op.code}] 점수 {op.ensemble_score:.1f} · "
            f"{self.last_close:,}원 × {self.estimated_quantity}주 = {self.order_value:,}원 · "
            f"목표 {self.target_price:,} / 손절 {self.stop_price:,} · "
            f"{op.reason_summary}"
        )


def recommend(
    codes: list[str],
    active_seed_krw: int,
    mode: str = "bunt",
    top_n: int = 5,
    min_score: float = 50.0,
    as_of: date | None = None,
    min_market_cap: int = 100_000_000_000,  # 최소 시총 (기본 1,000억원, 소형주 제외)
) -> list[Recommendation]:
    from src.db.connection import get_connection
    from src.risk.guard import SWING_MODE_PARAMS, StrategyMode

    # 입력 코드 중복 제거 (순서 보존). fundamentals_snapshot 시계열로 같은 종목이
    # 여러 번 주입되면 top_n 이 동일 종목 복제본으로 채워지는 사고 방지(2026-05).
    codes = list(dict.fromkeys(codes))

    mode_enum = StrategyMode(mode)
    params = SWING_MODE_PARAMS[mode_enum]   # 국내 주식 = 주간스윙 파라미터
    per_cap = active_seed_krw * PER_POSITION_CAP_PCT // 100

    # 시총 필터: 소형주 제외 (유동성 부족·변동성 과다 방지)
    if min_market_cap > 0 and codes:
        conn = get_connection()
        try:
            placeholders = ",".join("?" for _ in codes)
            rows = conn.execute(
                f"""SELECT DISTINCT code FROM fundamentals_snapshot
                    WHERE code IN ({placeholders})
                      AND market_cap >= ?""",
                (*codes, min_market_cap),
            ).fetchall()
            codes = [r[0] for r in rows]
        finally:
            conn.close()

    scorer = EnsembleScorer(mode=mode)
    results: list[Recommendation] = []

    for code in codes:
        df = load_ohlcv(code, end=as_of) if as_of else load_ohlcv(code)
        if df.empty or len(df) < 60:
            continue

        enriched = compute_all(df)
        op = scorer.evaluate(code, enriched, as_of=as_of)

        if op.filtered or op.ensemble_score < min_score:
            continue

        last_close = int(df["close"].iloc[-1])
        if last_close <= 0 or last_close > per_cap:
            continue

        qty = per_cap // last_close
        if qty < 1:
            continue

        order_value = last_close * qty
        # 호가단위 정렬 — KIS 가 비호가 가격을 거부함 (40030000)
        from src.risk.guard import align_to_tick
        tp = align_to_tick(last_close * (100 + params["tp_pct"]) // 100, "down")
        sl = align_to_tick(last_close * (100 - params["sl_pct"]) // 100, "down")

        results.append(Recommendation(
            opinion=op,
            last_close=last_close,
            estimated_quantity=qty,
            order_value=order_value,
            target_price=tp,
            stop_price=sl,
        ))

    results.sort(key=lambda r: r.opinion.ensemble_score, reverse=True)
    return results[:top_n]
