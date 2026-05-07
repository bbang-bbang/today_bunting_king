"""일봉 OHLCV 수집기.

사용법:
  # 3년 백필 (전 종목)
  python -m src.crawlers.fetch_ohlcv --backfill --years 3

  # 단일 종목 테스트
  python -m src.crawlers.fetch_ohlcv --code 005930 --start 2023-01-01 --end 2026-04-14

  # 증분 업데이트 (ingest_log 의 last_date 이후만)
  python -m src.crawlers.fetch_ohlcv --incremental
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import time
from datetime import date, datetime, timedelta

from src.adapters.market_data_base import MarketDataSource, OHLCV
from src.adapters.market_data_pykrx import PyKRXMarketDataSource
from src.db.connection import get_connection, init_schema

log = logging.getLogger("bunting.crawler.ohlcv")

# pykrx 서버 배려용. 너무 빠르면 일시 차단 가능.
SLEEP_BETWEEN_CODES_SEC = 0.1


def _log_ingest_start(conn, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_log (source, status, started_at) VALUES (?, 'running', ?)",
        (source, datetime.now().isoformat(timespec="seconds")),
    )
    return cur.lastrowid


def _log_ingest_finish(conn, log_id: int, row_count: int, last_date: date | None, snapshot_hash: str, status: str, error: str | None = None):
    conn.execute(
        """UPDATE ingest_log SET row_count=?, last_date=?, snapshot_hash=?, status=?, error=?, finished_at=?
           WHERE id=?""",
        (
            row_count,
            last_date.isoformat() if last_date else None,
            snapshot_hash,
            status,
            error,
            datetime.now().isoformat(timespec="seconds"),
            log_id,
        ),
    )


def upsert_ohlcv(conn, bars: list[OHLCV]) -> int:
    if not bars:
        return 0
    rows = [
        (b.code, b.date.isoformat(), b.open, b.high, b.low, b.close, b.volume, b.value, b.change_pct)
        for b in bars
    ]
    conn.executemany(
        """
        INSERT INTO ohlcv_daily (code, date, open, high, low, close, volume, value, change_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, date) DO UPDATE SET
          open = excluded.open,
          high = excluded.high,
          low = excluded.low,
          close = excluded.close,
          volume = excluded.volume,
          value = excluded.value,
          change_pct = excluded.change_pct
        """,
        rows,
    )
    return len(rows)


def list_codes(conn, only_tradable: bool = True) -> list[str]:
    q = "SELECT code FROM instruments"
    if only_tradable:
        q += " WHERE is_tradable = 1"
    q += " ORDER BY code"
    return [r[0] for r in conn.execute(q).fetchall()]


def list_universe_codes(conn) -> list[str]:
    """analysis_universe 우선 — 비면 빈 리스트."""
    rows = conn.execute(
        "SELECT code FROM analysis_universe ORDER BY rank ASC"
    ).fetchall()
    return [r[0] for r in rows]


def get_last_collected_date(conn, code: str) -> date | None:
    r = conn.execute(
        "SELECT MAX(date) FROM ohlcv_daily WHERE code = ?", (code,)
    ).fetchone()
    if r and r[0]:
        return date.fromisoformat(r[0])
    return None


def run(
    source: MarketDataSource,
    start: date,
    end: date,
    codes: list[str] | None = None,
    incremental: bool = False,
) -> tuple[int, str]:
    """수집 실행. (수집 행 수, snapshot_hash) 반환."""
    init_schema()
    conn = get_connection()
    ingest_source = "ohlcv_daily_incr" if incremental else "ohlcv_daily_backfill"
    log_id = _log_ingest_start(conn, ingest_source)

    total_rows = 0
    last_date_seen: date | None = None
    hasher = hashlib.sha256()

    try:
        target_codes = codes if codes else list_codes(conn)
        if not target_codes:
            raise RuntimeError("instruments 테이블이 비어있음. fetch_instruments 먼저 실행하세요.")

        log.info("대상 %d 종목, %s ~ %s", len(target_codes), start, end)

        for i, code in enumerate(target_codes, 1):
            s = start
            if incremental:
                last = get_last_collected_date(conn, code)
                if last:
                    s = last + timedelta(days=1)
                    if s > end:
                        continue

            try:
                bars = source.fetch_ohlcv(code, s, end)
            except Exception as e:
                log.warning("[%s] 조회 실패: %s — 건너뜀", code, e)
                continue

            n = upsert_ohlcv(conn, bars)
            total_rows += n
            if bars:
                last_date_seen = max(last_date_seen or bars[-1].date, bars[-1].date)
                for b in bars:
                    hasher.update(f"{b.code}|{b.date}|{b.close}|{b.volume}".encode())

            if i % 100 == 0:
                log.info("[%d/%d] 누적 %d 행 수집", i, len(target_codes), total_rows)

            time.sleep(SLEEP_BETWEEN_CODES_SEC)

        snapshot_hash = hasher.hexdigest()
        _log_ingest_finish(conn, log_id, total_rows, last_date_seen, snapshot_hash, "success")
        return total_rows, snapshot_hash

    except Exception as e:
        _log_ingest_finish(conn, log_id, total_rows, last_date_seen, hasher.hexdigest(), "fail", error=str(e))
        raise
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backfill", action="store_true", help="과거 N년치 백필")
    mode.add_argument("--incremental", action="store_true", help="각 종목의 마지막 수집일 이후만")
    mode.add_argument("--code", type=str, help="단일 종목 수집 (테스트)")

    p.add_argument("--years", type=int, default=3, help="backfill 연수 (기본 3)")
    p.add_argument("--start", type=str, help="YYYY-MM-DD (단일 종목 모드)")
    p.add_argument("--end", type=str, help="YYYY-MM-DD (기본: 오늘)")
    p.add_argument(
        "--universe-only", action="store_true",
        help="incremental 모드에서 analysis_universe 종목만 갱신 (기본 instruments 전체)",
    )
    args = p.parse_args()

    today = date.today()
    end = date.fromisoformat(args.end) if args.end else today

    source = PyKRXMarketDataSource()

    if args.code:
        start = date.fromisoformat(args.start) if args.start else today - timedelta(days=30)
        n, h = run(source, start, end, codes=[args.code])
    elif args.backfill:
        start = end - timedelta(days=365 * args.years)
        n, h = run(source, start, end)
    else:  # incremental
        # 각 종목별로 개별 시작일을 run() 내부에서 계산하므로 start 는 과거로 여유있게
        start = end - timedelta(days=365 * 5)
        codes = None
        if args.universe_only:
            conn = get_connection()
            try:
                codes = list_universe_codes(conn)
            finally:
                conn.close()
            if not codes:
                log.warning(
                    "--universe-only 지정됐으나 analysis_universe 비어있음 — "
                    "instruments 전체로 fallback (universe 빌드 후 재시도 권장)"
                )
                codes = None
            else:
                log.info("--universe-only 활성: %d 종목만 증분", len(codes))
        n, h = run(source, start, end, codes=codes, incremental=True)

    print(f"수집 완료: {n:,} 행, snapshot_hash={h[:16]}...")


if __name__ == "__main__":
    main()
