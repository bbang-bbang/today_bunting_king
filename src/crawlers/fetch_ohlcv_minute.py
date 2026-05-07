"""당일 분봉 OHLCV 수집기 (KIS OpenAPI).

사용법:
  python -m src.crawlers.fetch_ohlcv_minute --today
  python -m src.crawlers.fetch_ohlcv_minute --code 005930
  python -m src.crawlers.fetch_ohlcv_minute --date 2026-04-15
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date

from src.adapters.market_data_kis import KISMarketDataSource
from src.adapters.market_data_base import MinuteBar
from src.db.connection import get_connection, init_schema

log = logging.getLogger("bunting.crawler.ohlcv_minute")

SLEEP_BETWEEN_CODES_SEC = 0.3


def upsert_minute_bars(conn, bars: list[MinuteBar]) -> int:
    if not bars:
        return 0
    rows = [
        (b.code, b.datetime.strftime("%Y-%m-%d %H:%M"),
         b.open, b.high, b.low, b.close, b.volume)
        for b in bars
    ]
    conn.executemany(
        """INSERT INTO ohlcv_minute (code, datetime, open, high, low, close, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(code, datetime) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low,
             close=excluded.close, volume=excluded.volume""",
        rows,
    )
    return len(rows)


def list_codes(conn, only_tradable: bool = True) -> list[str]:
    q = "SELECT code FROM instruments"
    if only_tradable:
        q += " WHERE is_tradable = 1"
    return [r[0] for r in conn.execute(q).fetchall()]


def run(as_of: date, codes: list[str] | None = None) -> int:
    init_schema()
    source = KISMarketDataSource()
    conn = get_connection()
    total = 0
    try:
        target = codes if codes else list_codes(conn)
        log.info("분봉 수집 시작: %d 종목, %s", len(target), as_of)
        for i, code in enumerate(target, 1):
            try:
                bars = source.fetch_minute_ohlcv(code, as_of)
                n = upsert_minute_bars(conn, bars)
                total += n
            except Exception as e:
                log.warning("[%s] 수집 실패: %s — 건너뜀", code, e)
            if i % 50 == 0:
                log.info("[%d/%d] 누적 %d 행", i, len(target), total)
            time.sleep(SLEEP_BETWEEN_CODES_SEC)
    finally:
        conn.close()
    log.info("분봉 수집 완료: %d 행", total)
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--today", action="store_true")
    grp.add_argument("--date", type=str, help="YYYY-MM-DD")
    grp.add_argument("--code", type=str, help="단일 종목 (오늘 날짜)")
    args = p.parse_args()

    if args.code:
        n = run(date.today(), codes=[args.code])
    elif args.date:
        n = run(date.fromisoformat(args.date))
    else:
        n = run(date.today())
    print(f"수집 완료: {n:,} 행")


if __name__ == "__main__":
    main()
