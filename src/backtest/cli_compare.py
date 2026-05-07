"""전문가별 백테스트 비교 CLI.

사용법:
  # 삼성전자·SK하이닉스 기술 vs 재무 vs 조합 비교
  python -m src.backtest.cli_compare --codes 005930,000660 --start 2025-10-01 --end 2026-04-14

  # 번트 모드, 개별 거래 내역까지 출력
  python -m src.backtest.cli_compare --codes 005930 --start 2025-10-01 --end 2026-04-14 --show-trades
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from src.backtest.simulator import (
    Backtester,
    DbExpertAdapter,
    TechFundComboExpert,
)
from src.backtest.types import BacktestConfig, BacktestResult
from src.experts.fundamental import FundamentalExpert
from src.experts.technical import TechnicalExpert


def _build_experts() -> list[tuple[str, object]]:
    """비교할 전문가 목록 반환: (label, expert_instance)."""
    return [
        ("기술적 분석", TechnicalExpert()),
        ("재무제표",    DbExpertAdapter(FundamentalExpert(), ignore_as_of=True)),
        ("기술+재무",   TechFundComboExpert()),
    ]


def _print_comparison(results: list[tuple[str, BacktestResult]]) -> None:
    """전문가별 결과를 나란히 비교 출력."""
    cfg = results[0][1].config
    print()
    print("=" * 78)
    print(f"  전문가별 백테스트 비교  |  {cfg.start} ~ {cfg.end}  |  모드={cfg.strategy_mode}  시드={cfg.active_seed_krw:,}원")
    print("=" * 78)
    print()

    # 헤더
    header = f"{'지표':<18}"
    for label, _ in results:
        header += f"  {label:>14}"
    print(header)
    print("-" * (18 + 16 * len(results)))

    # 각 행
    rows = [
        ("총 거래",       lambda r: f"{r.total_trades}건"),
        ("승리",          lambda r: f"{r.wins}건"),
        ("패배",          lambda r: f"{r.losses}건"),
        ("승률",          lambda r: f"{r.win_rate * 100:.1f}%"),
        ("평균 수익률",    lambda r: f"{r.avg_return_pct:+.2f}%"),
        ("총 손익",        lambda r: f"{r.total_net_pnl:+,}원"),
        ("최종 자산",      lambda r: f"{r.final_equity:,}원"),
        ("총 수익률",      lambda r: f"{r.total_return_pct:+.2f}%"),
        ("MDD",           lambda r: f"{r.max_drawdown_pct:.2f}%"),
        ("샤프 비율",      lambda r: f"{r.sharpe:.2f}"),
    ]

    for label, fn in rows:
        line = f"{label:<18}"
        for _, result in results:
            line += f"  {fn(result):>14}"
        print(line)

    print()

    # 승자 판정
    best_idx = max(range(len(results)), key=lambda i: results[i][1].total_net_pnl)
    best_label = results[best_idx][0]
    best_pnl = results[best_idx][1].total_net_pnl
    print(f"  >>> 총 손익 기준 최고: [{best_label}] {best_pnl:+,}원")

    best_sharpe_idx = max(range(len(results)), key=lambda i: results[i][1].sharpe)
    print(f"  >>> 샤프 기준 최고:    [{results[best_sharpe_idx][0]}] {results[best_sharpe_idx][1].sharpe:.2f}")
    print()


def _print_trades(label: str, result: BacktestResult) -> None:
    if not result.trades:
        return
    print(f"--- [{label}] 개별 거래 ({len(result.trades)}건) ---")
    for t in result.trades:
        sign = "+" if t.net_pnl >= 0 else ""
        exit_info = f"→{t.exit_date}" if t.exit_date and t.exit_date != t.entry_date else ""
        print(
            f"  {t.entry_date}{exit_info}  {t.code}  {t.entry_price:,}→{t.exit_price:,}  "
            f"{t.quantity}주  [{t.outcome.value}]  {sign}{t.net_pnl:,}원 ({t.return_pct:+.2f}%)  "
            f"score={t.expert_score:.1f}"
        )
    print()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="전문가별 백테스트 비교")
    p.add_argument("--codes", type=str, required=True, help="쉼표 구분 종목코드")
    p.add_argument("--start", type=str, required=True, help="YYYY-MM-DD")
    p.add_argument("--end", type=str, required=True, help="YYYY-MM-DD")
    p.add_argument("--seed", type=int, default=1_000_000)
    p.add_argument("--mode", type=str, default="bunt", choices=["bunt", "squeeze"])
    p.add_argument("--min-score", type=float, default=50.0)
    p.add_argument("--max-holdings", type=int, default=1)
    p.add_argument("--holding", type=str, default="day", choices=["day", "swing_week"],
                   help="day=당일매매, swing_week=주간스윙(월→금)")
    p.add_argument("--early-tp", action="store_true",
                   help="swing 모드에서 day-TP(+3% bunt / +5% squeeze) 도달 시 즉시 익절")
    p.add_argument("--compare-early", action="store_true",
                   help="--early-tp ON/OFF 두 결과를 함께 출력 (swing 모드에서만 의미)")
    p.add_argument("--show-trades", action="store_true")
    args = p.parse_args()

    codes = [c.strip() for c in args.codes.split(",")]
    config = BacktestConfig(
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        active_seed_krw=args.seed,
        strategy_mode=args.mode,
        holding_mode=args.holding,
        early_take_profit=args.early_tp,
        min_score=args.min_score,
        max_holdings=args.max_holdings,
    )

    experts = _build_experts()
    results: list[tuple[str, BacktestResult]] = []

    # --compare-early: same expert·config 로 early ON/OFF 두 번 돌려서 나란히 비교
    if args.compare_early and args.holding == "swing_week":
        from dataclasses import replace
        for label, expert in experts:
            for early in (False, True):
                cfg2 = replace(config, early_take_profit=early)
                tag = "early-OFF" if not early else "early-ON"
                print(f"  [{label} · {tag}] 백테스트 실행 중...")
                bt = Backtester(expert=expert)
                result = bt.run(cfg2, codes)
                results.append((f"{label} · {tag}", result))
    else:
        for label, expert in experts:
            print(f"  [{label}] 백테스트 실행 중...")
            bt = Backtester(expert=expert)
            result = bt.run(config, codes)
            results.append((label, result))

    _print_comparison(results)

    if args.show_trades:
        for label, result in results:
            _print_trades(label, result)


if __name__ == "__main__":
    main()
