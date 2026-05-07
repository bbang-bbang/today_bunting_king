"""백테스트 데이터 타입."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class TradeOutcome(str, Enum):
    TAKE_PROFIT = "take_profit"    # 익절가 도달
    STOP_LOSS = "stop_loss"        # 손절가 도달
    TIME_CLOSE = "time_close"      # 당일 종가 청산 (당일매매 모드)
    WEEK_CLOSE = "week_close"      # 금요일 종가 청산 (스윙 모드)


@dataclass
class Trade:
    code: str
    entry_date: date
    entry_price: int
    exit_price: int
    quantity: int
    strategy_mode: str
    outcome: TradeOutcome
    gross_pnl: int           # 세전 손익
    net_pnl: int             # 수수료·세금 반영
    return_pct: float        # net_pnl / order_value * 100
    expert_score: float
    expert_reason: str
    exit_date: date | None = None   # 스윙: 실제 청산일 (당일매매는 entry_date와 동일)


class HoldingMode(str, Enum):
    DAY = "day"              # 당일매매: 매수일 종가 강제 청산
    SWING_WEEK = "swing_week"  # 주간 스윙: 월 매수 → 금 청산, 주중 TP/SL 즉시 매도


@dataclass
class BacktestConfig:
    start: date
    end: date
    active_seed_krw: int = 1_000_000
    strategy_mode: str = "bunt"        # "bunt" | "squeeze"
    holding_mode: str = "day"          # "day" | "swing_week"
    early_take_profit: bool = False    # True 면 swing 모드에서 day-TP 도달 시 즉시 익절
    min_score: float = 50.0
    max_holdings: int = 1              # 동시 보유 종목 수 (MVP 1)
    commission_bps: int = 15           # 0.015% = 15 basis points (한 방향)
    sell_tax_bps: int = 20             # 매도 거래세 0.2%


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[Trade] = field(default_factory=list)
    daily_equity: list[tuple[date, int]] = field(default_factory=list)

    # 집계 메트릭 (Backtester.run 이 채움)
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_net_pnl: int = 0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    final_equity: int = 0

    @property
    def total_return_pct(self) -> float:
        if self.config.active_seed_krw <= 0:
            return 0.0
        return (self.final_equity - self.config.active_seed_krw) / self.config.active_seed_krw * 100

    def summary(self) -> str:
        cfg = self.config
        hold_label = "당일매매" if cfg.holding_mode == "day" else "주간스윙"
        return (
            f"[백테스트] {cfg.start} ~ {cfg.end}  {cfg.strategy_mode}/{hold_label}  시드={cfg.active_seed_krw:,}원\n"
            f"  거래: {self.total_trades}건 (승 {self.wins} / 패 {self.losses}, 승률 {self.win_rate*100:.1f}%)\n"
            f"  평균 수익률:    {self.avg_return_pct:+.2f}%\n"
            f"  총 손익:        {self.total_net_pnl:+,}원 (수수료·세금 반영)\n"
            f"  최종 자산:      {self.final_equity:,}원 (총수익률 {self.total_return_pct:+.2f}%)\n"
            f"  최대낙폭 MDD:   {self.max_drawdown_pct:.2f}%\n"
            f"  샤프 비율:      {self.sharpe:.2f}"
        )
