"""코인 paper broker 단위 테스트."""
from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture(autouse=True)
def temp_coin_db(tmp_path, monkeypatch):
    db_path = tmp_path / "coin.db"
    monkeypatch.setenv("COIN_DB_PATH", str(db_path))
    monkeypatch.setenv("COIN_SEED_KRW", "1000000")
    monkeypatch.setenv("COIN_TRADE_MODE", "paper")
    import src.coin.db
    importlib.reload(src.coin.db)
    src.coin.db.init_coin_schema()
    yield


@pytest.fixture
def fake_ticker(monkeypatch):
    """fetch_current_price → 고정 ticker."""
    from src.coin.broker import CurrentTicker
    from datetime import datetime, timezone

    def _set(market: str, price: float):
        from src.coin import broker as br
        def fake(m):
            return CurrentTicker(market=m, price=price, high_24h=price * 1.05,
                                  low_24h=price * 0.95,
                                  timestamp=datetime.now(timezone.utc))
        monkeypatch.setattr(br, "fetch_current_price", fake)
    return _set


# ============================================================
# 매수 — 정상 / 부족 / paused
# ============================================================

def test_paper_buy_succeeds(fake_ticker):
    from src.coin.broker import execute_paper_buy, get_account_state
    fake_ticker("KRW-BTC", 100_000_000)   # 1억원/BTC
    res = execute_paper_buy("KRW-BTC", 0.001, 5.0, 1.5, reason="test")
    assert res["success"]
    assert res["market"] == "KRW-BTC"
    assert 100_000_000 < res["buy_price"] < 100_500_000   # slippage 가산

    state = get_account_state()
    assert len(state["positions"]) == 1
    assert state["cash_krw"] < 1_000_000   # 매수 후 감소


def test_paper_buy_fails_when_insufficient_cash(fake_ticker):
    from src.coin.broker import execute_paper_buy
    fake_ticker("KRW-BTC", 100_000_000)
    res = execute_paper_buy("KRW-BTC", 1.0, 5.0, 1.5)   # 1 BTC = 1억원, 시드 100만원
    assert not res["success"]
    assert "부족" in res["reason"]


def test_paper_buy_blocked_when_paused(fake_ticker):
    from src.coin.broker import execute_paper_buy, set_paused
    fake_ticker("KRW-BTC", 100_000_000)
    set_paused(True)
    res = execute_paper_buy("KRW-BTC", 0.001, 5.0, 1.5)
    assert not res["success"]
    assert "paused" in res["reason"].lower()


# ============================================================
# 매도 — TP, SL, 미보유
# ============================================================

def test_paper_sell_closes_position_and_returns_pnl(fake_ticker):
    from src.coin.broker import execute_paper_buy, execute_paper_sell, get_account_state

    # 매수
    fake_ticker("KRW-BTC", 100_000_000)
    buy = execute_paper_buy("KRW-BTC", 0.001, 5.0, 1.5)
    assert buy["success"]
    pos_id = buy["position_id"]

    # 매도 가격 +5% 가정
    fake_ticker("KRW-BTC", 105_000_000)
    sell = execute_paper_sell(pos_id, reason="tp")
    assert sell["success"]
    assert sell["pnl"] > 0
    assert sell["return_pct"] > 0

    state = get_account_state()
    assert len(state["positions"]) == 0   # closed


def test_paper_sell_loss_path(fake_ticker):
    from src.coin.broker import execute_paper_buy, execute_paper_sell

    fake_ticker("KRW-BTC", 100_000_000)
    buy = execute_paper_buy("KRW-BTC", 0.001, 5.0, 1.5)
    fake_ticker("KRW-BTC", 98_000_000)   # -2%
    sell = execute_paper_sell(buy["position_id"], reason="sl")
    assert sell["success"]
    assert sell["pnl"] < 0


def test_paper_sell_returns_failure_for_unknown_position(fake_ticker):
    from src.coin.broker import execute_paper_sell
    fake_ticker("KRW-BTC", 100_000_000)
    res = execute_paper_sell(99999, reason="tp")
    assert not res["success"]


# ============================================================
# 계좌 상태
# ============================================================

def test_initial_account_seed_from_env():
    from src.coin.broker import get_account_state
    state = get_account_state()
    assert state["cash_krw"] == 1_000_000   # COIN_SEED_KRW=1000000
    assert state["trade_mode"] == "paper"
    assert state["paused"] is False
    assert state["positions"] == []
