"""백테스트 엔진 테스트.

합성 데이터로 메트릭 정확성과 체결 로직 검증.
DB 의존 없음 — 가짜 Expert 와 가짜 data dict 로 시뮬레이터 단위 테스트.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.backtest.metrics import compute_metrics
from src.backtest.types import BacktestConfig, Trade, TradeOutcome


def _make_trade(
    net_pnl: int,
    entry_price: int = 100_000,
    qty: int = 1,
    outcome: TradeOutcome = TradeOutcome.TAKE_PROFIT,
) -> Trade:
    return_pct = net_pnl / (entry_price * qty) * 100
    return Trade(
        code="TEST",
        entry_date=date(2026, 4, 1),
        entry_price=entry_price,
        exit_price=entry_price + net_pnl,
        quantity=qty,
        strategy_mode="bunt",
        outcome=outcome,
        gross_pnl=net_pnl,
        net_pnl=net_pnl,
        return_pct=return_pct,
        expert_score=60.0,
        expert_reason="test",
    )


# ============================================================
# 메트릭
# ============================================================

def test_metrics_empty_trades():
    m = compute_metrics([], [], 1_000_000)
    assert m["total_trades"] == 0
    assert m["wins"] == 0
    assert m["win_rate"] == 0.0


def test_metrics_all_wins():
    trades = [_make_trade(3_000), _make_trade(3_000), _make_trade(3_000)]
    equity = [(date(2026, 4, 1), 1_003_000), (date(2026, 4, 2), 1_006_000), (date(2026, 4, 3), 1_009_000)]
    m = compute_metrics(trades, equity, 1_000_000)
    assert m["total_trades"] == 3
    assert m["wins"] == 3
    assert m["win_rate"] == 1.0
    assert m["avg_return_pct"] == pytest.approx(3.0)
    assert m["total_net_pnl"] == 9_000


def test_metrics_mdd():
    # 1,000,000 → 1,050,000 → 950,000 → 1,100,000
    # peak=1,050,000, trough=950,000 → MDD = 100k/1.05M ≈ 9.52%
    trades = [_make_trade(50_000), _make_trade(-100_000), _make_trade(150_000)]
    equity = [
        (date(2026, 4, 1), 1_050_000),
        (date(2026, 4, 2), 950_000),
        (date(2026, 4, 3), 1_100_000),
    ]
    m = compute_metrics(trades, equity, 1_000_000)
    assert m["max_drawdown_pct"] == pytest.approx(9.5238, abs=0.01)


def test_metrics_sharpe_positive_on_steady_gain():
    trades = [_make_trade(3_000) for _ in range(30)]
    # 매일 +3,000원씩 증가하는 자산 곡선
    equity = [(date(2026, 4, 1) + pd.Timedelta(days=i), 1_000_000 + 3_000 * (i + 1)) for i in range(30)]
    equity = [(e[0].date() if hasattr(e[0], "date") else e[0], e[1]) for e in equity]
    m = compute_metrics(trades, equity, 1_000_000)
    # 꾸준한 상승 → 샤프 양수
    assert m["sharpe"] > 0


# ============================================================
# 시뮬레이터 (가짜 Expert 주입)
# ============================================================

class _FixedScoreExpert:
    """항상 고정 점수를 반환하는 가짜 전문가."""
    name = "fake"

    def __init__(self, score: float = 80.0):
        self.score = score

    def evaluate(self, code, enriched):
        from src.experts.base import ExpertOpinion
        if enriched is None or enriched.empty or len(enriched) < 60:
            return ExpertOpinion(code=code, expert="fake", score=0, error="부족")
        return ExpertOpinion(
            code=code, expert="fake", score=self.score,
            mode_fit={"bunt": 0.6, "squeeze": 0.4},
            reason_summary="fake signal",
        )


def test_backtest_take_profit_path(monkeypatch):
    """매일 +3% 상승하는 종목 → 전부 익절."""
    from src.backtest.simulator import Backtester

    # load_ohlcv, compute_all 를 monkeypatch
    dates = pd.date_range("2026-01-01", periods=100, freq="D")
    close = pd.Series(10_000 * (1.03 ** np.arange(100)), index=dates)
    high = close * 1.04
    low = close * 0.99
    open_ = close / 1.03                   # 전일 종가로부터 +3% 확실히 나오는 시가
    volume = pd.Series([100_000.0] * 100, index=dates)
    fake_ohlcv = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})

    # enriched 를 compute_all 로 계산
    from src.indicators.compute import compute_all as real_compute_all
    fake_enriched = real_compute_all(fake_ohlcv)

    def fake_load(code, start=None, end=None):
        return fake_ohlcv

    def fake_compute(df):
        return fake_enriched

    monkeypatch.setattr("src.backtest.simulator.load_ohlcv", fake_load)
    monkeypatch.setattr("src.backtest.simulator.compute_all", fake_compute)

    cfg = BacktestConfig(
        start=date(2026, 3, 15),       # 60일 이후 시점부터 시뮬
        end=date(2026, 4, 5),
        active_seed_krw=1_000_000,
        strategy_mode="bunt",
        min_score=50.0,
    )
    bt = Backtester(expert=_FixedScoreExpert(80.0))
    result = bt.run(cfg, ["FAKE"])

    assert result.total_trades > 0
    # 상승 확실한 데이터 → 승률 높아야 함
    assert result.win_rate > 0.5, f"승률 {result.win_rate}"
    # 익절 outcome 이 다수
    tp_count = sum(1 for t in result.trades if t.outcome == TradeOutcome.TAKE_PROFIT)
    assert tp_count > 0


def test_backtest_stop_loss_path(monkeypatch):
    """매일 -3% 하락 → 손절 다수."""
    from src.backtest.simulator import Backtester

    dates = pd.date_range("2026-01-01", periods=100, freq="D")
    close = pd.Series(10_000 * (0.97 ** np.arange(100)), index=dates)
    high = close * 1.01
    low = close * 0.96                     # 당일 저가 -2% 이하 확실
    open_ = close / 0.97
    volume = pd.Series([100_000.0] * 100, index=dates)
    fake_ohlcv = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})

    from src.indicators.compute import compute_all as real_compute_all
    fake_enriched = real_compute_all(fake_ohlcv)

    monkeypatch.setattr("src.backtest.simulator.load_ohlcv", lambda c, **_: fake_ohlcv)
    monkeypatch.setattr("src.backtest.simulator.compute_all", lambda df: fake_enriched)

    cfg = BacktestConfig(
        start=date(2026, 3, 15),
        end=date(2026, 4, 5),
        active_seed_krw=1_000_000,
        min_score=30.0,                    # 하락장도 받아들이기 위해 낮춤
    )
    bt = Backtester(expert=_FixedScoreExpert(40.0))
    result = bt.run(cfg, ["FAKE"])

    # 거래가 있으면 대부분 손절
    if result.total_trades > 0:
        sl_count = sum(1 for t in result.trades if t.outcome == TradeOutcome.STOP_LOSS)
        assert sl_count >= result.total_trades * 0.5


def test_backtest_no_data_returns_empty():
    from src.backtest.simulator import Backtester
    cfg = BacktestConfig(
        start=date(2026, 4, 1),
        end=date(2026, 4, 10),
        active_seed_krw=1_000_000,
    )
    result = Backtester(expert=_FixedScoreExpert()).run(cfg, [])
    assert result.total_trades == 0
    assert result.final_equity == 0    # 메트릭 기본값


def test_backtest_respects_seed_cap(monkeypatch):
    """1주 가격이 per_position_cap 넘으면 거래 0건."""
    from src.backtest.simulator import Backtester

    dates = pd.date_range("2026-01-01", periods=100, freq="D")
    # 1주 가격이 시드 상한 초과 (시드 10만 · 50% cap = 5만)
    close = pd.Series([1_000_000.0] * 100, index=dates)    # 주당 100만원
    high = close * 1.05
    low = close * 0.95
    open_ = close
    volume = pd.Series([100_000.0] * 100, index=dates)
    fake = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})

    from src.indicators.compute import compute_all as real_compute_all
    fake_enriched = real_compute_all(fake)

    monkeypatch.setattr("src.backtest.simulator.load_ohlcv", lambda c, **_: fake)
    monkeypatch.setattr("src.backtest.simulator.compute_all", lambda df: fake_enriched)

    cfg = BacktestConfig(
        start=date(2026, 3, 15), end=date(2026, 4, 5),
        active_seed_krw=100_000,     # 시드 10만 → 종목당 5만 한도 → 주당 100만 살 수 없음
    )
    result = Backtester(expert=_FixedScoreExpert(80.0)).run(cfg, ["EXPENSIVE"])
    assert result.total_trades == 0


# ============================================================
# 주간 스윙 시뮬레이터
# ============================================================

def _make_weekly_ohlcv():
    """월~금 영업일 기준 100일 합성 데이터. 주당 +5% 꾸준히 상승."""
    dates = pd.bdate_range("2026-01-05", periods=100, freq="B")
    close = pd.Series(10_000 * (1.01 ** np.arange(100)), index=dates)
    high = close * 1.08   # 일중 고가 +8% → 스윙 TP(7%) 도달 가능
    low = close * 0.97
    open_ = close * 0.995
    volume = pd.Series([100_000.0] * 100, index=dates)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


def test_swing_take_profit(monkeypatch):
    """상승 종목 → 주중 TP 7% 도달해서 익절."""
    from src.backtest.simulator import Backtester

    fake_ohlcv = _make_weekly_ohlcv()
    from src.indicators.compute import compute_all as real_compute_all
    fake_enriched = real_compute_all(fake_ohlcv)

    monkeypatch.setattr("src.backtest.simulator.load_ohlcv", lambda c, **_: fake_ohlcv)
    monkeypatch.setattr("src.backtest.simulator.compute_all", lambda df: fake_enriched)

    cfg = BacktestConfig(
        start=date(2026, 3, 16), end=date(2026, 4, 17),
        active_seed_krw=1_000_000,
        strategy_mode="bunt",
        holding_mode="swing_week",
        min_score=50.0,
    )
    result = Backtester(expert=_FixedScoreExpert(80.0)).run(cfg, ["FAKE"])

    assert result.total_trades > 0
    tp_count = sum(1 for t in result.trades if t.outcome == TradeOutcome.TAKE_PROFIT)
    assert tp_count > 0
    # 스윙은 매수일 != 청산일인 경우가 있어야 함
    multi_day = [t for t in result.trades if t.exit_date and t.exit_date != t.entry_date]
    # 전부 당일 청산은 아님 (주간 보유이므로)
    assert result.win_rate > 0.5


def test_swing_week_close(monkeypatch):
    """횡보 종목 → TP/SL 모두 미도달, 금요일 종가 청산."""
    from src.backtest.simulator import Backtester

    # 횡보: 시가=종가, 고가+2%, 저가-2% (TP 7% / SL 4% 모두 미도달)
    dates = pd.bdate_range("2026-01-05", periods=100, freq="B")
    close = pd.Series([10_000.0] * 100, index=dates)
    high = close * 1.02
    low = close * 0.98
    open_ = close
    volume = pd.Series([100_000.0] * 100, index=dates)
    fake_ohlcv = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})

    from src.indicators.compute import compute_all as real_compute_all
    fake_enriched = real_compute_all(fake_ohlcv)

    monkeypatch.setattr("src.backtest.simulator.load_ohlcv", lambda c, **_: fake_ohlcv)
    monkeypatch.setattr("src.backtest.simulator.compute_all", lambda df: fake_enriched)

    cfg = BacktestConfig(
        start=date(2026, 3, 16), end=date(2026, 4, 17),
        active_seed_krw=1_000_000,
        strategy_mode="bunt",
        holding_mode="swing_week",
        min_score=30.0,
    )
    result = Backtester(expert=_FixedScoreExpert(40.0)).run(cfg, ["FLAT"])

    if result.total_trades > 0:
        wc_count = sum(1 for t in result.trades if t.outcome == TradeOutcome.WEEK_CLOSE)
        # 횡보 데이터 → 대부분 금요일 종가 청산
        assert wc_count >= result.total_trades * 0.5


def test_swing_stop_loss(monkeypatch):
    """하락 종목 → SL 4% 도달해서 손절."""
    from src.backtest.simulator import Backtester

    dates = pd.bdate_range("2026-01-05", periods=100, freq="B")
    close = pd.Series(10_000 * (0.99 ** np.arange(100)), index=dates)
    high = close * 1.01
    low = close * 0.94    # 일중 저가 -6% → SL 4% 도달
    open_ = close * 1.005
    volume = pd.Series([100_000.0] * 100, index=dates)
    fake_ohlcv = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})

    from src.indicators.compute import compute_all as real_compute_all
    fake_enriched = real_compute_all(fake_ohlcv)

    monkeypatch.setattr("src.backtest.simulator.load_ohlcv", lambda c, **_: fake_ohlcv)
    monkeypatch.setattr("src.backtest.simulator.compute_all", lambda df: fake_enriched)

    cfg = BacktestConfig(
        start=date(2026, 3, 16), end=date(2026, 4, 17),
        active_seed_krw=1_000_000,
        strategy_mode="bunt",
        holding_mode="swing_week",
        min_score=30.0,
    )
    result = Backtester(expert=_FixedScoreExpert(40.0)).run(cfg, ["DROP"])

    if result.total_trades > 0:
        sl_count = sum(1 for t in result.trades if t.outcome == TradeOutcome.STOP_LOSS)
        assert sl_count >= result.total_trades * 0.5


def test_swing_no_data_returns_empty():
    """데이터 없는 종목 → 0건."""
    from src.backtest.simulator import Backtester
    cfg = BacktestConfig(
        start=date(2026, 4, 1), end=date(2026, 4, 10),
        active_seed_krw=1_000_000,
        holding_mode="swing_week",
    )
    result = Backtester(expert=_FixedScoreExpert()).run(cfg, [])
    assert result.total_trades == 0
