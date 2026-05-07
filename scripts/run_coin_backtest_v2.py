"""코인 백테스트 v2 — 앙상블 (technical + sentiment + arbitrage).

데이터 소스:
  - Upbit OHLCV (이미 캐시)
  - alternative.me Fear & Greed Index
  - Binance USDT 가격 (김치프리미엄 계산)
  - USD/KRW 환율 (단순 평균값 사용 — R&D 단계 보강 필요)

비교: v1 (technical only) vs v2 (앙상블).
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

import pandas as pd

from src.coin.upbit_data import fetch_candles, load_from_csv, save_to_csv
from src.coin.fear_greed import fetch_fear_greed_history, fng_to_hourly_index
from src.coin.kimchi_premium import (
    fetch_binance_klines, fetch_usdkrw_proxy, kimchi_premium_series,
)
from src.coin.ensemble import precompute_ensemble_signals
from src.coin.backtest.simulator import grid_search


CACHE_DIR = Path("data/upbit")
TP_GRID = [2.0, 3.0, 4.0, 5.0, 7.0]
SL_GRID = [1.5, 2.0, 2.5, 3.0]


def _load_ohlcv(market: str) -> pd.DataFrame:
    cache = CACHE_DIR / f"{market}_60min.csv"
    if cache.exists():
        return load_from_csv(cache)
    df = fetch_candles(market, unit_min=60, count=2160)
    save_to_csv(df, cache)
    return df


def _load_binance_for_kp(symbol: str, count: int = 2160) -> pd.DataFrame:
    """Binance 시간봉 — 페이징 (limit=1000 max)."""
    cache = CACHE_DIR / f"binance_{symbol}_1h.csv"
    if cache.exists():
        df = pd.read_csv(cache, parse_dates=["timestamp"], index_col="timestamp")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df
    # 1000 캔들 받음 (Binance limit). 2160 풀로 받으려면 페이징인데 일단 1000 으로.
    df = fetch_binance_klines(symbol, interval="1h", limit=1000)
    if not df.empty:
        df.to_csv(cache, index=True, index_label="timestamp")
    return df


def main():
    fng_path = CACHE_DIR / "fng.csv"
    if fng_path.exists():
        fng_daily = pd.read_csv(fng_path, parse_dates=["date"], index_col="date")
        if fng_daily.index.tz is None:
            fng_daily.index = fng_daily.index.tz_localize("UTC")
        print(f"[FNG] cache  ({len(fng_daily)} days)")
    else:
        print("[FNG] downloading...")
        fng_daily = fetch_fear_greed_history(days=120)
        fng_daily.to_csv(fng_path, index=True, index_label="date")
        print(f"[FNG] saved ({len(fng_daily)} days)")

    usd_krw = fetch_usdkrw_proxy(days=120)
    print(f"[FX]  USD/KRW proxy ({len(usd_krw)} days, simple value)")

    for market in ["KRW-BTC", "KRW-ETH"]:
        print(f"\n========== {market} ==========")
        ohlcv = _load_ohlcv(market)
        if ohlcv.empty:
            print("  no data, skip")
            continue
        print(f"  ohlcv: {len(ohlcv)} candles  ({ohlcv.index[0]} ~ {ohlcv.index[-1]})")

        # FNG 시간봉 ffill
        fng_hourly = fng_to_hourly_index(fng_daily, ohlcv.index)
        print(f"  fng:   ffilled to {len(fng_hourly)} candles  (mean={fng_hourly.mean():.1f})")

        # 김프
        binance_symbol = "BTCUSDT" if market == "KRW-BTC" else "ETHUSDT"
        binance_df = _load_binance_for_kp(binance_symbol)
        if binance_df.empty:
            print(f"  binance {binance_symbol}: 다운로드 실패, KP 없이 진행")
            kp_hourly = None
        else:
            print(f"  binance {binance_symbol}: {len(binance_df)} candles")
            kp_full = kimchi_premium_series(ohlcv, binance_df, usd_krw)
            print(f"  kp_pct: {len(kp_full)} candles  (mean={kp_full.mean():.2f}%, std={kp_full.std():.2f})")
            # ohlcv 인덱스에 reindex (없는 시각은 ffill)
            kp_hourly = kp_full.reindex(ohlcv.index, method="ffill")

        # ===== v2 앙상블 시그널 =====
        # 김프 데이터 신뢰성 낮음 (환율 proxy 부정확) — weight 0 으로 일단 무력화
        # threshold 0.3 — 이번 백테스트로 시그널 발동 빈도 측정용
        sig_df = precompute_ensemble_signals(
            ohlcv, fng_hourly=fng_hourly, kp_hourly=None,
            threshold=0.6,
            weights={"technical": 1.0, "sentiment": 0.8, "arbitrage": 0.0},
        )
        n_buy = int(sig_df["buy"].sum())
        score_max = float(sig_df["total_score"].max())
        score_avg = float(sig_df["total_score"].mean())
        print(
            f"  [v2 앙상블] buy 시그널 {n_buy}회  "
            f"(threshold=0.6, score_avg={score_avg:+.2f}, max={score_max:+.2f})"
        )

        # 그리드 서치
        print(f"\n  [v2] TOP 5 (return%)")
        results = grid_search(
            ohlcv, signal_fn=None, market=market,
            tp_grid=TP_GRID, sl_grid=SL_GRID, seed_krw=300_000,
            precomputed_signals=sig_df["buy"],
        )
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
