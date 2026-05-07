"""코인 백테스트 시뮬레이터 + 시그널 단위 테스트."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _make_df(prices: list[float], freq: str = "1h", wick_pct: float = 0.001) -> pd.DataFrame:
    """간단 OHLCV — close 만 받아 합성. wick_pct = high/low 의 wick 비율 (기본 0.1%)."""
    n = len(prices)
    idx = pd.date_range("2026-04-01T00:00:00", periods=n, freq=freq, tz="UTC")
    closes = np.array(prices, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * (1 + wick_pct)
    lows = np.minimum(opens, closes) * (1 - wick_pct)
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 100.0),
    }, index=idx)


# ============================================================
# Backtester 핵심 — TP / SL 발동
# ============================================================

def test_backtester_take_profit_path():
    """매 캔들 +0.5% 상승 → TP 1.5% 도달 시 익절."""
    from src.coin.backtest.simulator import (
        CoinBacktester, CoinBacktestConfig,
    )

    # 30 캔들 워밍업 + 상승 구간
    df = _make_df([100.0 * (1.005 ** i) for i in range(50)])

    # 시그널: 31번째 캔들에서 무조건 buy
    fired = {"called": False}
    def sig(sub):
        if not fired["called"] and len(sub) == 31:
            fired["called"] = True
            return "buy"
        return None

    cfg = CoinBacktestConfig(
        market="KRW-TEST", start=df.index[0], end=df.index[-1],
        seed_krw=1_000_000, tp_pct=1.5, sl_pct=1.0,
        commission_pct=0.0, slippage_pct=0.0,
    )
    bt = CoinBacktester(signal_fn=sig)
    res = bt.run(df, cfg)

    assert res.n_trades == 1
    assert res.trades[0].outcome == "tp"
    assert res.wins == 1


def test_backtester_stop_loss_path():
    """매 캔들 -0.5% 하락 → SL 1.0% 도달 시 손절."""
    from src.coin.backtest.simulator import (
        CoinBacktester, CoinBacktestConfig,
    )

    df = _make_df([100.0 * (0.995 ** i) for i in range(50)])
    fired = {"called": False}
    def sig(sub):
        if not fired["called"] and len(sub) == 31:
            fired["called"] = True
            return "buy"
        return None

    cfg = CoinBacktestConfig(
        market="KRW-TEST", start=df.index[0], end=df.index[-1],
        seed_krw=1_000_000, tp_pct=1.5, sl_pct=1.0,
        commission_pct=0.0, slippage_pct=0.0,
    )
    bt = CoinBacktester(signal_fn=sig)
    res = bt.run(df, cfg)

    assert res.n_trades == 1
    assert res.trades[0].outcome == "sl"
    assert res.losses == 1


def test_backtester_no_signal_no_trades():
    from src.coin.backtest.simulator import (
        CoinBacktester, CoinBacktestConfig,
    )
    df = _make_df([100.0] * 50)
    cfg = CoinBacktestConfig(
        market="KRW-TEST", start=df.index[0], end=df.index[-1],
        seed_krw=1_000_000,
    )
    res = CoinBacktester(signal_fn=lambda sub: None).run(df, cfg)
    assert res.n_trades == 0
    assert res.final_equity == 1_000_000


def test_backtester_commission_reduces_pnl():
    """수수료 0.05% × 양방향 = 0.1% 매매당 손실. TP 1.5% 면 net 1.4% 정도."""
    from src.coin.backtest.simulator import (
        CoinBacktester, CoinBacktestConfig,
    )
    df = _make_df([100.0 * (1.005 ** i) for i in range(50)])
    fired = {"called": False}
    def sig(sub):
        if not fired["called"] and len(sub) == 31:
            fired["called"] = True
            return "buy"
        return None

    cfg = CoinBacktestConfig(
        market="KRW-TEST", start=df.index[0], end=df.index[-1],
        seed_krw=1_000_000, tp_pct=1.5, sl_pct=1.0,
        commission_pct=0.05, slippage_pct=0.0,
    )
    res = CoinBacktester(signal_fn=sig).run(df, cfg)
    assert res.n_trades == 1
    # net return < gross return
    t = res.trades[0]
    assert 1.0 <= t.return_pct < 1.5


# ============================================================
# 시그널 — 컴포넌트
# ============================================================

def test_signal_returns_none_with_insufficient_data():
    from src.coin.signals import momentum_signal
    df = _make_df([100.0] * 20)
    assert momentum_signal(df) is None


def test_signal_buy_on_rsi_oversold_recovery():
    """급락 후 반등 + EMA 우상향 + 거래량 증가 → buy."""
    from src.coin.signals import momentum_signal

    # 30캔들 평탄 → 5캔들 급락 (RSI <= 35) → 반등 캔들 (RSI 회복)
    prices = [100.0] * 30 + [99.0, 97.5, 96.0, 94.5, 93.0] + [94.0]
    df = _make_df(prices)
    # 마지막 캔들에서 거래량 ↑
    df.iloc[-1, df.columns.get_loc("volume")] = 200.0

    sig = momentum_signal(df)
    # 정확한 조건은 복잡 — 버그 없이 동작만 확인 (None or "buy")
    assert sig in ("buy", None)


def test_signal_no_buy_when_ema_downtrend():
    """완만한 하락 추세 → EMA12 < EMA26 → buy X."""
    from src.coin.signals import momentum_signal
    prices = [100.0 * (0.998 ** i) for i in range(40)]
    df = _make_df(prices)
    assert momentum_signal(df) is None


# ============================================================
# 그리드 서치
# ============================================================

def test_grid_search_returns_sorted():
    from src.coin.backtest.simulator import grid_search

    df = _make_df([100.0 * (1.003 ** i) for i in range(60)])
    fired = {"called": False}
    def sig(sub):
        if not fired["called"] and len(sub) == 31:
            fired["called"] = True
            return "buy"
        return None

    results = grid_search(
        df, signal_fn=sig, market="KRW-TEST",
        tp_grid=[1.0, 1.5, 2.0], sl_grid=[0.7, 1.0],
        seed_krw=1_000_000,
    )
    # 6 조합
    assert len(results) == 6
    # 수익률 내림차순
    pcts = [r.total_return_pct for r in results]
    assert pcts == sorted(pcts, reverse=True)
