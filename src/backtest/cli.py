"""백테스트 CLI.

사용법:
  # 삼성·SK하이닉스·카카오 번트 백테스트
  python -m src.backtest.cli --codes 005930,000660,035720 --start 2025-04-15 --end 2026-04-14

  # 스퀴즈 모드, 시드 10만원
  python -m src.backtest.cli --codes 005930 --mode squeeze --seed 100000

  # DB에 있는 모든 종목 대상
  python -m src.backtest.cli --all --start 2025-10-01 --end 2026-04-14
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from src.backtest.simulator import Backtester
from src.backtest.types import BacktestConfig
from src.db.connection import get_connection


def _list_all_codes() -> list[str]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT DISTINCT code FROM ohlcv_daily ORDER BY code").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--codes", type=str, help="쉼표 구분 종목코드 (예: 005930,000660)")
    g.add_argument("--all", action="store_true", help="DB의 모든 종목")

    p.add_argument("--start", type=str, required=True, help="YYYY-MM-DD")
    p.add_argument("--end", type=str, required=True, help="YYYY-MM-DD")
    p.add_argument("--seed", type=int, default=1_000_000, help="활성 시드 (기본 100만)")
    p.add_argument("--mode", type=str, default="bunt", choices=["bunt", "squeeze"])
    p.add_argument("--min-score", type=float, default=50.0)
    p.add_argument("--max-holdings", type=int, default=1)
    p.add_argument("--show-trades", action="store_true", help="개별 거래 내역 출력")
    args = p.parse_args()

    codes = _list_all_codes() if args.all else [c.strip() for c in args.codes.split(",")]

    config = BacktestConfig(
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        active_seed_krw=args.seed,
        strategy_mode=args.mode,
        min_score=args.min_score,
        max_holdings=args.max_holdings,
    )

    result = Backtester().run(config, codes)
    print()
    print(result.summary())

    if args.show_trades and result.trades:
        print()
        print("개별 거래:")
        for t in result.trades:
            sign = "+" if t.net_pnl >= 0 else ""
            print(
                f"  {t.entry_date}  {t.code}  {t.entry_price:,}→{t.exit_price:,}  "
                f"{t.quantity}주  [{t.outcome.value}]  {sign}{t.net_pnl:,}원 ({t.return_pct:+.2f}%)  "
                f"score={t.expert_score:.1f}"
            )


if __name__ == "__main__":
    main()
