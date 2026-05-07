"""BTC/ETH 90일 분봉 백테스트 1회 실행 — Phase 1 검증."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# unbuffered stdout (Windows 환경에서 print 가 buffered 되어 결과 안 보이는 사고 방지)
sys.stdout.reconfigure(line_buffering=True)

from src.coin.upbit_data import fetch_candles, save_to_csv, load_from_csv
from src.coin.backtest.simulator import grid_search
from src.coin.signals import momentum_signal, precompute_signals


CACHE_DIR = Path("data/upbit")
DAYS = 90
UNIT_MIN = 60   # 1시간봉 — 24*90 = 2160 캔들

TP_GRID = [1.0, 1.5, 2.0, 2.5]
SL_GRID = [0.7, 1.0, 1.3]


def _ensure_data(market: str):
    cache = CACHE_DIR / f"{market}_{UNIT_MIN}min.csv"
    if cache.exists():
        print(f"  [캐시] {cache}")
        return load_from_csv(cache)
    print(f"  [다운로드] {market} {DAYS}일치 다운로드 중...")
    df = fetch_candles(market, unit_min=UNIT_MIN, count=DAYS * 24)
    save_to_csv(df, cache)
    return df


def main():
    for market in ["KRW-BTC", "KRW-ETH"]:
        print(f"\n========== {market} ==========")
        df = _ensure_data(market)
        if df.empty:
            print("  데이터 없음 — 스킵")
            continue
        print(f"  데이터: {len(df)} 캔들  ({df.index[0]} ~ {df.index[-1]})")

        # 시그널 1회 사전 계산 → 그리드 모든 조합에서 reuse (O(N) → O(1) per cell)
        sig_series = precompute_signals(df)
        n_buy_signals = int(sig_series.sum())
        print(f"  [시그널] 매수 시그널 발동: {n_buy_signals}회")

        results = grid_search(
            df, signal_fn=momentum_signal, market=market,
            tp_grid=TP_GRID, sl_grid=SL_GRID, seed_krw=300_000,
            precomputed_signals=sig_series,
        )

        # top 5 (ASCII only — Windows cp949 호환)
        print(f"\n  TOP 5 (return%)")
        for i, r in enumerate(results[:5], 1):
            c = r.config
            print(
                f"  {i}. TP+{c.tp_pct}% SL-{c.sl_pct}%  "
                f"trades={r.n_trades:3d}  win%={r.win_rate*100:5.1f}  "
                f"avg={r.avg_return_pct:+.3f}%  total={r.total_return_pct:+.2f}%"
            )
        passing = [r for r in results if r.win_rate >= 0.55 and r.total_return_pct > 0]
        print(f"\n  PASS (win>=55% AND total>0): {len(passing)}/{len(results)}")
        if passing:
            best = passing[0]
            c = best.config
            print(
                f"     best: TP+{c.tp_pct}% SL-{c.sl_pct}%  "
                f"win%={best.win_rate*100:.1f}  total={best.total_return_pct:+.2f}%"
            )


if __name__ == "__main__":
    main()
