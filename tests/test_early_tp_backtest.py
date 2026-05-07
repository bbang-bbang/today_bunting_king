"""백테스트 early_take_profit 모드 — swing 시뮬에서 day-TP 우선 청산.

2026-05-04 도입: /early 토글 효과를 데이터로 측정 가능하게.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.backtest.simulator import Backtester
from src.backtest.types import BacktestConfig, TradeOutcome


# ============================================================
# Fakes — backtest 의 _FixedScoreExpert 패턴 재사용
# ============================================================

class _FixedScoreExpert:
    name = "fake"

    def __init__(self, score: float = 80.0):
        self.score = score

    def evaluate(self, code, enriched):
        from src.experts.base import ExpertOpinion
        if enriched is None or enriched.empty or len(enriched) < 60:
            return ExpertOpinion(code=code, expert="fake", score=0, error="부족")
        return ExpertOpinion(
            code=code, expert="fake", score=self.score,
            mode_fit={"bunt": 0.7, "squeeze": 0.3},
            reason_summary="fake bull",
        )


def _make_swing_test_ohlcv(
    test_week_high_first_day: int,
    test_week_close_friday: int,
    code_count: int = 1,
) -> dict:
    """60일 baseline + 1주(월~금) 테스트 윈도우 OHLCV 생성.

    매수일(월) open=100,000.
    화요일 high=test_week_high_first_day (이게 핵심 — TP 도달 여부 결정).
    금요일 close=test_week_close_friday.
    """
    base_dates = pd.date_range("2026-02-01", periods=60, freq="B")
    test_week = pd.date_range("2026-05-04", periods=5, freq="B")  # 월~금
    all_dates = list(base_dates) + list(test_week)

    # baseline: 100,000 +/- 약간
    base_prices = [100_000 + 50 * (i % 7 - 3) for i in range(60)]

    # 테스트 주: 월=100,000 진입, 화=test_week_high (high), 수~금=평탄, 금 close=지정
    week_data = [
        (100_000, 100_500, 99_800, 100_200),   # 월: open=100,000
        (100_500, test_week_high_first_day, 100_300, max(102_000, test_week_high_first_day - 1_500)),  # 화
        (102_000, 102_500, 101_500, 102_000),
        (102_000, 102_300, 101_500, 101_800),
        (101_800, 102_000, 101_000, test_week_close_friday),
    ]

    rows_open, rows_high, rows_low, rows_close = [], [], [], []
    for p in base_prices:
        rows_open.append(p)
        rows_high.append(p + 200)
        rows_low.append(p - 200)
        rows_close.append(p)
    for o, h, l, c in week_data:
        rows_open.append(o)
        rows_high.append(h)
        rows_low.append(l)
        rows_close.append(c)

    df = pd.DataFrame({
        "open": rows_open,
        "high": rows_high,
        "low": rows_low,
        "close": rows_close,
        "volume": [100_000.0] * len(all_dates),
    }, index=all_dates)
    df.index.name = "date"
    return df


@pytest.fixture
def fake_data(monkeypatch):
    """load_ohlcv / compute_all monkeypatch 헬퍼 — caller 가 df 를 주면 1종목으로 사용."""
    from src.indicators.compute import compute_all as real_compute_all

    def _setup(df):
        def fake_load(code, start=None, end=None):
            return df
        def fake_compute(d):
            return real_compute_all(d)
        monkeypatch.setattr("src.backtest.simulator.load_ohlcv", fake_load)
        monkeypatch.setattr("src.backtest.simulator.compute_all", fake_compute)
    return _setup


def _run(cfg, df, fake_data) -> list:
    fake_data(df)
    bt = Backtester(expert=_FixedScoreExpert())
    return bt.run(cfg, ["TEST"]).trades


def _base_cfg(early: bool = False) -> BacktestConfig:
    return BacktestConfig(
        start=date(2026, 5, 4), end=date(2026, 5, 8),
        active_seed_krw=10_000_000,
        strategy_mode="bunt", holding_mode="swing_week",
        early_take_profit=early, max_holdings=1, min_score=50,
    )


# ============================================================
# 시나리오 1: +3% 만 도달 (swing TP +7% 미달)
# OFF: 금 종가 청산 / ON: 화요일 day-TP 즉시 익절
# ============================================================

def test_early_off_takes_no_profit_when_only_day_tp_hit(fake_data):
    """OFF: +3% 만 도달, swing TP +7% 안 가면 → 금 종가 청산."""
    df = _make_swing_test_ohlcv(
        test_week_high_first_day=103_500,   # +3.5% 도달
        test_week_close_friday=101_500,
    )
    trades = _run(_base_cfg(early=False), df, fake_data)
    assert len(trades) == 1
    assert trades[0].outcome == TradeOutcome.WEEK_CLOSE
    assert trades[0].exit_price == 101_500


def test_early_on_takes_profit_at_day_tp(fake_data):
    """ON: 같은 상황에서 day-TP 도달 즉시 익절."""
    df = _make_swing_test_ohlcv(
        test_week_high_first_day=103_500,
        test_week_close_friday=101_500,
    )
    trades = _run(_base_cfg(early=True), df, fake_data)
    assert len(trades) == 1
    assert trades[0].outcome == TradeOutcome.TAKE_PROFIT
    # +3% (호가 정렬) 부근에서 익절
    assert 102_500 <= trades[0].exit_price <= 103_500
    assert trades[0].net_pnl > 0


# ============================================================
# 시나리오 2: +10% 도달 — OFF 는 swing TP, ON 은 day-TP (적게 벌음)
# ============================================================

def test_early_on_misses_bigger_swing_gains(fake_data):
    """ON 의 trade-off: +7% 갈 종목을 +3% 에 끊어서 작게 벎."""
    df = _make_swing_test_ohlcv(
        test_week_high_first_day=110_000,   # 화요일 +10% 단번에
        test_week_close_friday=109_000,
    )

    trades_off = _run(_base_cfg(early=False), df, fake_data)
    df2 = _make_swing_test_ohlcv(
        test_week_high_first_day=110_000,
        test_week_close_friday=109_000,
    )
    trades_on = _run(_base_cfg(early=True), df2, fake_data)

    off, on = trades_off[0], trades_on[0]
    assert off.outcome == TradeOutcome.TAKE_PROFIT
    assert on.outcome == TradeOutcome.TAKE_PROFIT
    # OFF 가 더 비싼 가격(swing TP +7%)에 익절. ON 은 day TP +3%.
    assert off.exit_price > on.exit_price
    assert off.net_pnl > on.net_pnl


# ============================================================
# 시나리오 3: SL 은 그대로 swing 기준
# ============================================================

def test_early_on_keeps_swing_sl(fake_data):
    """ON: -3% (day-SL 이라면 도달) 가도 swing-SL(-4%) 미만이면 청산 X."""
    df = _make_swing_test_ohlcv(
        test_week_high_first_day=100_500,   # +0.5% — TP 미도달
        test_week_close_friday=98_000,       # 금 종가 -2%
    )
    # 화요일 low 가 97,000 근처지만 swing SL 96,000 안 깨도록 조정
    df.loc[pd.Timestamp("2026-05-05"), "low"] = 97_000
    trades = _run(_base_cfg(early=True), df, fake_data)
    assert len(trades) == 1
    # SL_HIT 아니어야
    assert trades[0].outcome != TradeOutcome.STOP_LOSS
    # 종가 청산
    assert trades[0].outcome == TradeOutcome.WEEK_CLOSE
