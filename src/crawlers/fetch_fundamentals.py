"""재무지표 수집기 (pykrx `get_market_fundamental`).

지표: PER / PBR / EPS / BPS / DIV / DPS
ROE 는 EPS / BPS 로 근사 계산.

사용법:
  python -m src.crawlers.fetch_fundamentals                 # 오늘 기준
  python -m src.crawlers.fetch_fundamentals --as-of 2026-04-14
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta

from pykrx import stock

from src.db.connection import get_connection, init_schema

log = logging.getLogger("bunting.crawler.fundamentals")


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _log_start(conn, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_log (source, status, started_at) VALUES (?, 'running', ?)",
        (source, datetime.now().isoformat(timespec="seconds")),
    )
    return cur.lastrowid


def _log_finish(conn, log_id: int, row_count: int, last_date: date | None, status: str, error: str | None = None):
    conn.execute(
        """UPDATE ingest_log SET row_count=?, last_date=?, status=?, error=?, finished_at=?
           WHERE id=?""",
        (
            row_count,
            last_date.isoformat() if last_date else None,
            status,
            error,
            datetime.now().isoformat(timespec="seconds"),
            log_id,
        ),
    )


def _fetch_with_retry(as_of: date, max_back: int = 7):
    """휴장일이면 하루씩 뒤로 가며 최대 max_back 일 재시도."""
    d = as_of
    for _ in range(max_back):
        df = stock.get_market_fundamental(_yyyymmdd(d), market="ALL")
        if df is not None and not df.empty:
            return df, d
        d -= timedelta(days=1)
    return None, as_of


def run(as_of: date) -> tuple[int, date]:
    init_schema()
    conn = get_connection()
    log_id = _log_start(conn, "fundamentals_snapshot")

    try:
        df, effective_date = _fetch_with_retry(as_of)
        if df is None or df.empty:
            raise RuntimeError(f"재무 데이터 조회 실패 (as_of={as_of})")

        log.info("pykrx 재무지표 %d 종목 수신 (기준일 %s)", len(df), effective_date)

        rows = []
        snap_date_str = effective_date.isoformat()
        for ticker, row in df.iterrows():
            per = _safe_float(row.get("PER"))
            pbr = _safe_float(row.get("PBR"))
            eps = _safe_float(row.get("EPS"))
            bps = _safe_float(row.get("BPS"))
            div_pct = _safe_float(row.get("DIV"))         # 시가배당률 %

            # ROE ≈ EPS / BPS × 100
            roe = (eps / bps * 100) if (eps is not None and bps and bps > 0) else None

            rows.append((
                ticker,
                snap_date_str,
                None,                  # market_cap — 별도 수집 필요
                per,
                pbr,
                roe,
                None,                  # debt_ratio — DART API 필요
                0,                     # is_warning (관리종목)
                0,                     # is_watch (투자주의)
                "pykrx_fundamental",
            ))

        conn.executemany(
            """
            INSERT INTO fundamentals_snapshot
              (code, snapshot_date, market_cap, per, pbr, roe, debt_ratio, is_warning, is_watch, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, snapshot_date) DO UPDATE SET
              per = excluded.per,
              pbr = excluded.pbr,
              roe = excluded.roe,
              source = excluded.source
            """,
            rows,
        )

        # 종목마스터 없는 경우 최소 레코드 보장 (FK)
        conn.execute(
            """INSERT OR IGNORE INTO instruments (code, name, market, is_tradable, updated_at)
               SELECT DISTINCT code, code, 'KOSPI', 1, ? FROM fundamentals_snapshot
               WHERE code NOT IN (SELECT code FROM instruments)""",
            (datetime.now().isoformat(timespec="seconds"),),
        )

        _log_finish(conn, log_id, len(rows), effective_date, "success")
        return len(rows), effective_date

    except Exception as e:
        _log_finish(conn, log_id, 0, None, "fail", error=str(e))
        raise
    finally:
        conn.close()


def _safe_float(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:     # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD (기본: 오늘)")
    args = p.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    n, eff = run(as_of)
    print(f"재무지표 {n} 건 수집 완료 (기준일 {eff})")


if __name__ == "__main__":
    main()
