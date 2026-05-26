"""투자자별 매매동향 수집기 — 네이버 금융 스크래핑.

pykrx 수급 API 동작 불가(2026-04) 시 대체용.
네이버 종목별 외국인/기관 순매매량을 일별로 수집.

사용:
  python -m src.crawlers.fetch_investor_flow_naver --codes 005930,000660
  python -m src.crawlers.fetch_investor_flow_naver --codes 005930 --pages 5
  python -m src.crawlers.fetch_investor_flow_naver --top 20   # 시총 상위 20
"""
from __future__ import annotations

import argparse
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

from src.db.connection import get_connection, init_schema

log = logging.getLogger("bunting.crawler.investor_flow_naver")

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _parse_int(text: str) -> int:
    """'+1,432,694' / '-505,391' / '1,234' → int."""
    cleaned = re.sub(r"[,\s+]", "", text)
    if not cleaned or cleaned == "-":
        return 0
    return int(cleaned)


def _row_from_shares(
    d: date, code: str, close: int,
    institution_shares: int, foreign_shares: int, foreign_pct: float,
) -> dict:
    """네이버 순매매'량'(주) → 순매수'대금'(원) 환산.

    네이버 frgn 페이지는 주식 수를 주지만, investor_flow.*_net 컬럼과
    FlowExpert 임계값(20억/5억/1억원)은 모두 '원' 기준이므로 종가로 환산한다.
    종가를 평균체결가 대용으로 쓰는 근사지만 자릿수·부호는 정합.
    """
    inst_amount = institution_shares * close
    foreign_amount = foreign_shares * close
    return {
        "date": d,
        "code": code,
        "close": close,
        "institution_net": inst_amount,
        "foreign_net": foreign_amount,
        "individual_net": -(inst_amount + foreign_amount),  # 잔차
        "institution_shares": institution_shares,
        "foreign_shares": foreign_shares,
        "foreign_holding_pct": foreign_pct,
    }


def fetch_flow_page(code: str, page: int = 1) -> list[dict]:
    """네이버 금융 외국인/기관 순매매 페이지 파싱.

    Returns:
        [{'date': date, 'code': str, 'close': int,
          'institution_net': int(원), 'foreign_net': int(원),
          'institution_shares': int, 'foreign_shares': int,
          'foreign_holding_pct': float}]
    """
    url = f"https://finance.naver.com/item/frgn.naver?code={code}&page={page}"
    try:
        res = httpx.get(url, headers=_HEADERS, timeout=10)
        res.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("[%s] 페이지 %d 요청 실패: %s", code, page, e)
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    tables = soup.select("table.type2")
    if len(tables) < 2:
        return []

    table = tables[1]
    rows_out: list[dict] = []

    for tr in table.select("tr"):
        tds = tr.select("td")
        if len(tds) < 9:
            continue
        date_text = tds[0].get_text(strip=True)
        if not re.match(r"\d{4}\.\d{2}\.\d{2}", date_text):
            continue

        try:
            d = date.fromisoformat(date_text.replace(".", "-"))
            close = _parse_int(tds[1].get_text(strip=True))
            institution_shares = _parse_int(tds[5].get_text(strip=True))
            foreign_shares = _parse_int(tds[6].get_text(strip=True))
            foreign_pct_text = tds[8].get_text(strip=True).replace("%", "")
            foreign_pct = float(foreign_pct_text) if foreign_pct_text else 0.0
        except (ValueError, IndexError):
            continue

        rows_out.append(_row_from_shares(
            d, code, close, institution_shares, foreign_shares, foreign_pct,
        ))

    return rows_out


def fetch_flow_multi_page(code: str, pages: int = 3) -> list[dict]:
    """여러 페이지 수집 후 병합."""
    all_rows: list[dict] = []
    seen_dates: set[date] = set()
    for p in range(1, pages + 1):
        rows = fetch_flow_page(code, p)
        if not rows:
            break
        for r in rows:
            if r["date"] not in seen_dates:
                seen_dates.add(r["date"])
                all_rows.append(r)
        time.sleep(0.3)
    return sorted(all_rows, key=lambda r: r["date"])


def save_flow(rows: list[dict]) -> int:
    """DB에 저장. UPSERT."""
    if not rows:
        return 0
    conn = get_connection()
    try:
        db_rows = [
            (r["date"].isoformat(), r["code"],
             r["foreign_net"], r["institution_net"], r["individual_net"])
            for r in rows
        ]
        conn.executemany(
            """INSERT INTO investor_flow (date, code, foreign_net, institution_net, individual_net)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(date, code) DO UPDATE SET
                 foreign_net = excluded.foreign_net,
                 institution_net = excluded.institution_net,
                 individual_net = excluded.individual_net""",
            db_rows,
        )
        return len(db_rows)
    finally:
        conn.close()


def run(code: str, pages: int = 3) -> int:
    """종목 하나의 수급 수집 + DB 저장. 저장 건수 반환."""
    init_schema()
    rows = fetch_flow_multi_page(code, pages)
    return save_flow(rows)


def _fetch_one(code: str, pages: int) -> list[dict]:
    # 동시 실행 시 초기 버스트를 분산해 네이버 차단 회피
    time.sleep(random.uniform(0, 0.4))
    return fetch_flow_multi_page(code, pages)


def run_batch(codes: list[str], pages: int = 3, concurrency: int = 1) -> int:
    """여러 종목 일괄 수집. concurrency>1 이면 동시 수집 후 일괄 저장(sqlite 쓰기 경합 회피)."""
    if concurrency <= 1:
        total = 0
        for code in codes:
            try:
                n = run(code, pages)
                log.info("[%s] 수급 %d건 저장", code, n)
                total += n
            except Exception as e:
                log.warning("[%s] 수급 수집 실패: %s", code, e)
            time.sleep(0.5)
        return total

    init_schema()
    all_rows: list[dict] = []
    ok = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_fetch_one, code, pages): code for code in codes}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                rows = fut.result()
                if rows:
                    all_rows.extend(rows)
                    ok += 1
            except Exception as e:
                log.warning("[%s] 수급 수집 실패: %s", code, e)
    saved = save_flow(all_rows)
    log.info("naver 수급 동시수집: %d/%d 종목 성공, %d건 저장", ok, len(codes), saved)
    return saved


def _get_top_codes(n: int) -> list[str]:
    """시총 상위 N개 종목코드."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT code FROM fundamentals_snapshot ORDER BY market_cap DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="네이버 금융 투자자별 매매동향 수집")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--codes", type=str, help="쉼표 구분 종목코드")
    g.add_argument("--top", type=int, help="시총 상위 N종목")
    p.add_argument("--pages", type=int, default=3, help="페이지 수 (1페이지 ≈ 10영업일)")
    p.add_argument("--concurrency", type=int, default=1, help="동시 수집 스레드 수")
    args = p.parse_args()

    if args.top:
        codes = _get_top_codes(args.top)
        print(f"시총 상위 {args.top}종목: {len(codes)}개")
    else:
        codes = [c.strip() for c in args.codes.split(",")]

    total = run_batch(codes, args.pages, concurrency=args.concurrency)
    print(f"수급 수집 완료: {total:,}건")


if __name__ == "__main__":
    main()
