"""투자자별 매매동향 수집기 (외인·기관·개인 일별 순매수대금).

사용:
  python -m src.crawlers.fetch_investor_flow                 # 오늘
  python -m src.crawlers.fetch_investor_flow --as-of 2026-04-14
  python -m src.crawlers.fetch_investor_flow --from 2026-04-01 --to 2026-04-14
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta

from pykrx import stock

from src.db.connection import get_connection, init_schema

log = logging.getLogger("bunting.crawler.investor_flow")

_INVESTOR_MAP = {
    "외국인": "foreign_net",
    "기관합계": "institution_net",
    "개인": "individual_net",
}
_MARKETS = ("KOSPI", "KOSDAQ")


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _log_start(conn, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_log (source, status, started_at) VALUES (?, 'running', ?)",
        (source, datetime.now().isoformat(timespec="seconds")),
    )
    return cur.lastrowid


def _log_finish(conn, log_id: int, n: int, last_date: date | None, status: str, error: str | None = None):
    conn.execute(
        """UPDATE ingest_log SET row_count=?, last_date=?, status=?, error=?, finished_at=?
           WHERE id=?""",
        (
            n,
            last_date.isoformat() if last_date else None,
            status,
            error,
            datetime.now().isoformat(timespec="seconds"),
            log_id,
        ),
    )


def fetch_one_day(as_of: date) -> dict[str, dict]:
    """하루치 전 종목 투자자별 순매수 수집 → {code: {foreign_net, institution_net, individual_net}}."""
    d_str = _yyyymmdd(as_of)
    merged: dict[str, dict] = {}

    for market in _MARKETS:
        for investor_ko, col in _INVESTOR_MAP.items():
            try:
                df = stock.get_market_net_purchases_of_equities_by_ticker(
                    d_str, d_str, market, investor_ko,
                )
            except Exception as e:
                log.warning("[%s/%s/%s] 수집 실패: %s", as_of, market, investor_ko, e)
                continue
            if df is None or df.empty:
                continue

            # 컬럼명이 KRX 변경에 따라 다를 수 있으므로 '순매수거래대금' 또는 '순매수대금' 후보 확인
            amount_col = None
            for cand in ("순매수거래대금", "순매수대금"):
                if cand in df.columns:
                    amount_col = cand
                    break
            if amount_col is None:
                log.warning("[%s/%s] 순매수대금 컬럼 없음 — 컬럼: %s",
                            market, investor_ko, list(df.columns))
                continue

            for ticker, row in df.iterrows():
                if ticker not in merged:
                    merged[ticker] = {"foreign_net": 0, "institution_net": 0, "individual_net": 0}
                try:
                    merged[ticker][col] = int(row[amount_col] or 0)
                except (TypeError, ValueError):
                    merged[ticker][col] = 0

    return merged


def run_one_day(as_of: date) -> int:
    init_schema()
    conn = get_connection()
    log_id = _log_start(conn, "investor_flow")
    try:
        data = fetch_one_day(as_of)
        rows = [
            (as_of.isoformat(), code, d["foreign_net"], d["institution_net"], d["individual_net"])
            for code, d in data.items()
        ]
        if rows:
            conn.executemany(
                """INSERT INTO investor_flow (date, code, foreign_net, institution_net, individual_net)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(date, code) DO UPDATE SET
                     foreign_net = excluded.foreign_net,
                     institution_net = excluded.institution_net,
                     individual_net = excluded.individual_net""",
                rows,
            )
            # FK 방어: instruments 레코드 없으면 최소 생성
            conn.execute(
                """INSERT OR IGNORE INTO instruments (code, name, market, is_tradable, updated_at)
                   SELECT DISTINCT code, code, 'KOSPI', 1, ?
                   FROM investor_flow WHERE code NOT IN (SELECT code FROM instruments)""",
                (datetime.now().isoformat(timespec="seconds"),),
            )
        _log_finish(conn, log_id, len(rows), as_of, "success")
        return len(rows)
    except Exception as e:
        _log_finish(conn, log_id, 0, None, "fail", error=str(e))
        raise
    finally:
        conn.close()


def run_range(start: date, end: date) -> int:
    total = 0
    d = start
    while d <= end:
        if d.weekday() < 5:   # 주말 스킵
            try:
                n = run_one_day(d)
                log.info("[%s] %d 건", d, n)
                total += n
            except Exception as e:
                log.warning("[%s] 실패: %s — 스킵", d, e)
            time.sleep(0.3)   # KRX 배려
        d += timedelta(days=1)
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD (하루 수집)")
    p.add_argument("--from", dest="from_date", type=str, default=None, help="YYYY-MM-DD (범위 시작)")
    p.add_argument("--to", dest="to_date", type=str, default=None, help="YYYY-MM-DD (범위 끝)")
    args = p.parse_args()

    if args.from_date and args.to_date:
        s = date.fromisoformat(args.from_date)
        e = date.fromisoformat(args.to_date)
        total = run_range(s, e)
        print(f"범위 수집 완료: {total:,} 건")
    else:
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        n = run_one_day(as_of)
        print(f"{as_of} 투자자 수급 {n:,} 건 수집")


if __name__ == "__main__":
    main()
