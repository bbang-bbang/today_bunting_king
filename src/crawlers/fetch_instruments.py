"""종목 마스터 수집기 (KOSPI + KOSDAQ 전종목).

사용법:
  python -m src.crawlers.fetch_instruments
  python -m src.crawlers.fetch_instruments --as-of 2026-04-14
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime

from src.adapters.market_data_base import Instrument, MarketDataSource
from src.adapters.market_data_pykrx import PyKRXMarketDataSource
from src.db.connection import get_connection, init_schema

log = logging.getLogger("bunting.crawler.instruments")


def _log_ingest_start(conn, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_log (source, status, started_at) VALUES (?, 'running', ?)",
        (source, datetime.now().isoformat(timespec="seconds")),
    )
    return cur.lastrowid


def _log_ingest_finish(conn, log_id: int, row_count: int, status: str, error: str | None = None):
    conn.execute(
        "UPDATE ingest_log SET row_count=?, status=?, error=?, finished_at=? WHERE id=?",
        (row_count, status, error, datetime.now().isoformat(timespec="seconds"), log_id),
    )


def upsert_instruments(conn, instruments: list[Instrument]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    rows = [
        (ins.code, ins.name, ins.market, ins.sector, 1, now)
        for ins in instruments
    ]
    conn.executemany(
        """
        INSERT INTO instruments (code, name, market, sector, is_tradable, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
          name = excluded.name,
          market = excluded.market,
          sector = excluded.sector,
          updated_at = excluded.updated_at
        """,
        rows,
    )
    return len(rows)


def run(source: MarketDataSource, as_of: date) -> int:
    init_schema()
    conn = get_connection()
    log_id = _log_ingest_start(conn, "instruments_master")
    total = 0
    try:
        for market in ("KOSPI", "KOSDAQ"):
            items = source.list_instruments(as_of=as_of, market=market)
            log.info("[%s] %d 종목 수집", market, len(items))
            total += upsert_instruments(conn, items)
        _log_ingest_finish(conn, log_id, total, "success")
        return total
    except Exception as e:
        _log_ingest_finish(conn, log_id, total, "fail", error=str(e))
        raise
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD (기본: 오늘)")
    args = p.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    source = PyKRXMarketDataSource()
    n = run(source, as_of)
    print(f"종목 마스터 {n} 건 upsert 완료 (as_of={as_of})")


if __name__ == "__main__":
    main()
