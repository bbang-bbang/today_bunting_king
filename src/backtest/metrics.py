"""백테스트 성과 메트릭 계산."""
from __future__ import annotations

from datetime import date

import numpy as np

from src.backtest.types import Trade


def compute_metrics(
    trades: list[Trade],
    daily_equity: list[tuple[date, int]],
    initial_cash: int,
) -> dict:
    """백테스트 결과로부터 집계 메트릭 산출."""
    total = len(trades)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    losses = sum(1 for t in trades if t.net_pnl < 0)

    avg_return_pct = (
        sum(t.return_pct for t in trades) / total if total > 0 else 0.0
    )
    total_net_pnl = sum(t.net_pnl for t in trades)

    # 자산 곡선 (초기 현금 + 일별 잔고)
    equity_curve = [initial_cash] + [e for _, e in daily_equity]
    final_equity = equity_curve[-1]

    # MDD (Max Drawdown)
    peak = equity_curve[0]
    mdd = 0.0
    for e in equity_curve:
        if e > peak:
            peak = e
        dd = (e - peak) / peak if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd

    # Sharpe (일간 수익률, 연환산 √252)
    if len(equity_curve) > 1:
        prev = np.array(equity_curve[:-1], dtype=float)
        curr = np.array(equity_curve[1:], dtype=float)
        # prev = 0 인 구간(거의 없지만) 방어
        safe = prev > 0
        if safe.sum() > 1:
            daily_rets = curr[safe] / prev[safe] - 1
            std_r = daily_rets.std(ddof=1) if len(daily_rets) > 1 else 0.0
            mean_r = daily_rets.mean()
            sharpe = (mean_r / std_r * np.sqrt(252)) if std_r > 0 else 0.0
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total) if total > 0 else 0.0,
        "avg_return_pct": avg_return_pct,
        "total_net_pnl": total_net_pnl,
        "max_drawdown_pct": abs(mdd) * 100,
        "sharpe": float(sharpe),
        "final_equity": final_equity,
    }
