"""백테스트 엔진.

과거 OHLCV 로 기술 전문가 룰의 번트/스퀴즈 성과를 시뮬레이션.
Look-ahead bias 방지: 날짜 D 추천 시 D-1 까지의 데이터만 사용.
"""
from src.backtest.simulator import Backtester
from src.backtest.types import (
    BacktestConfig,
    BacktestResult,
    Trade,
    TradeOutcome,
)

__all__ = ["Backtester", "BacktestConfig", "BacktestResult", "Trade", "TradeOutcome"]
