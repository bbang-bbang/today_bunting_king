"""네이버 금융 종목별 재무지표 크롤러.

URL: https://finance.naver.com/item/main.naver?code={code}

추출 지표:
  - PER  (em#_per)     — 현재가 기준 주가수익비율
  - PBR  (em#_pbr)     — 현재가 기준 주가순자산비율
  - 시가총액 (em#_market_sum, "NNN조 NNN(억)" 두 스팬)
  - ROE       (재무 IFRS 테이블, 최근 연간 실적)
  - 부채비율  (재무 IFRS 테이블, 최근 연간 실적)

단일 종목 사용:
  python -m src.crawlers.fetch_fundamentals_naver --code 005930

전 종목 배치:
  python -m src.crawlers.fetch_fundamentals_naver --all
  python -m src.crawlers.fetch_fundamentals_naver --all --limit 350 --market-cap-min 100000000000
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

from src.db.connection import get_connection, init_schema

log = logging.getLogger("bunting.crawler.fund_naver")

_URL = "https://finance.naver.com/item/main.naver"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
}
_PAGE_SLEEP = 0.3


@dataclass(frozen=True)
class NaverFundamentals:
    code: str
    per: float | None
    pbr: float | None
    roe: float | None
    debt_ratio: float | None
    market_cap: int | None
    is_warning: bool   # 관리종목
    is_watch: bool     # 투자주의/경고/위험

    @property
    def has_any(self) -> bool:
        return any(
            v is not None for v in
            (self.per, self.pbr, self.roe, self.debt_ratio, self.market_cap)
        )


# ------------------------------------------------------------
# 파싱 (순수 함수 — HTML → NaverFundamentals)
# ------------------------------------------------------------

def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace(",", "").replace("%", "").strip()
    if not s or s in ("-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_market_cap(em) -> int | None:
    """em#_market_sum 파싱.

    네이버는 "1,271조 5,656(억)" 형태.
    EUC-KR → UTF-8 디코딩 과정에서 "조" 문자가 깨질 수 있으므로
    텍스트에서 숫자 토큰만 순서대로 추출해서 조·억 매핑.
    """
    if em is None:
        return None
    text = em.get_text(" ", strip=True)
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", text) if n]
    if not nums:
        return None
    if len(nums) == 1:
        # 억 단위만 (시총 1조 미만)
        return nums[0] * 10**8
    # 첫 토큰=조, 둘째 토큰=억
    return nums[0] * 10**12 + nums[1] * 10**8


def _parse_warning_flags(soup: BeautifulSoup) -> tuple[bool, bool]:
    """관리종목·투자주의 감지. 네이버는 종목명 옆에 span.ico_warning 등으로 표시.

    간단 휴리스틱: 페이지 상단 영역 텍스트에 키워드가 들어있는지.
    """
    is_warning = False
    is_watch = False
    area = soup.select_one(".wrap_company") or soup.select_one(".h_company")
    if area is not None:
        t = area.get_text(" ", strip=True)
        if "관리종목" in t:
            is_warning = True
        if any(k in t for k in ("투자주의", "투자경고", "투자위험")):
            is_watch = True
    return is_warning, is_watch


def _parse_annual_row(tbl, label_prefix: str) -> float | None:
    """IFRS 재무 테이블에서 특정 행의 '최근 연간 실적' 값을 뽑는다.

    tds[2] = 3번째 연간(가장 최근 실적). 2026.12(E) 는 추정치라 제외.
    """
    if tbl is None:
        return None
    for tr in tbl.select("tbody tr"):
        th = tr.select_one("th")
        if not th:
            continue
        label = th.get_text(strip=True)
        if label.startswith(label_prefix):
            tds = [td.get_text(strip=True) for td in tr.select("td")]
            # 4개 연간 + 4개 분기 = 8 tds. 최근 연간 실적은 index 2.
            if len(tds) >= 3:
                return _to_float(tds[2])
    return None


def parse_html(html: str, code: str) -> NaverFundamentals:
    soup = BeautifulSoup(html, "lxml")

    per = _to_float(soup.select_one("em#_per").get_text(strip=True)) \
        if soup.select_one("em#_per") else None
    pbr = _to_float(soup.select_one("em#_pbr").get_text(strip=True)) \
        if soup.select_one("em#_pbr") else None

    market_cap = _parse_market_cap(soup.select_one("em#_market_sum"))

    tbl_fund = soup.select_one("table.tb_type1_ifrs")
    roe = _parse_annual_row(tbl_fund, "ROE")
    debt_ratio = _parse_annual_row(tbl_fund, "부채비율")

    is_warning, is_watch = _parse_warning_flags(soup)

    return NaverFundamentals(
        code=code, per=per, pbr=pbr, roe=roe, debt_ratio=debt_ratio,
        market_cap=market_cap, is_warning=is_warning, is_watch=is_watch,
    )


# ------------------------------------------------------------
# 네트워크
# ------------------------------------------------------------

def fetch(code: str, client: httpx.Client | None = None) -> NaverFundamentals | None:
    own = client is None
    if own:
        client = httpx.Client(headers=_HEADERS, timeout=10.0)
    try:
        r = client.get(_URL, params={"code": code})
        if r.status_code != 200:
            log.warning("[%s] HTTP %s", code, r.status_code)
            return None
        r.encoding = r.encoding or "euc-kr"
        return parse_html(r.text, code)
    except Exception as e:
        log.warning("[%s] fetch 실패: %s", code, e)
        return None
    finally:
        if own:
            client.close()


# ------------------------------------------------------------
# DB 저장
# ------------------------------------------------------------

def store(f: NaverFundamentals, snapshot_date: str | None = None) -> bool:
    if not f.has_any and not (f.is_warning or f.is_watch):
        return False
    init_schema()
    sd = snapshot_date or date.today().isoformat()
    conn = get_connection()
    try:
        # FK 방어 — instruments 에 없으면 스킵
        row = conn.execute(
            "SELECT 1 FROM instruments WHERE code = ?", (f.code,)
        ).fetchone()
        if not row:
            log.warning("[%s] instruments 에 없음 — 저장 스킵", f.code)
            return False
        conn.execute(
            """INSERT INTO fundamentals_snapshot
               (code, snapshot_date, market_cap, per, pbr, roe, debt_ratio,
                is_warning, is_watch, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'naver')
               ON CONFLICT(code, snapshot_date) DO UPDATE SET
                 market_cap=excluded.market_cap,
                 per=excluded.per, pbr=excluded.pbr, roe=excluded.roe,
                 debt_ratio=excluded.debt_ratio,
                 is_warning=excluded.is_warning, is_watch=excluded.is_watch,
                 source=excluded.source""",
            (f.code, sd, f.market_cap, f.per, f.pbr, f.roe, f.debt_ratio,
             int(f.is_warning), int(f.is_watch)),
        )
        return True
    finally:
        conn.close()


def run(code: str) -> bool:
    f = fetch(code)
    if f is None:
        return False
    ok = store(f)
    if ok:
        log.info(
            "[%s] PER=%s PBR=%s ROE=%s 부채=%s 시총=%s%s%s",
            code, f.per, f.pbr, f.roe, f.debt_ratio,
            f.market_cap,
            " [관리]" if f.is_warning else "",
            " [주의]" if f.is_watch else "",
        )
    return ok


# ------------------------------------------------------------
# 배치
# ------------------------------------------------------------

def _list_target_codes(limit: int | None = None, universe_only: bool = False) -> list[str]:
    """타깃 종목 코드 목록.

    universe_only=True 면 analysis_universe (top 500) 만, 비면 instruments 전체로 fallback.
    daily 운영에서 universe 만 갱신하면 2,771 → 500 으로 줄어 5배 빠름.
    """
    conn = get_connection()
    try:
        if universe_only:
            rows = conn.execute(
                "SELECT code FROM analysis_universe ORDER BY rank ASC"
            ).fetchall()
            codes = [r[0] for r in rows]
            if not codes:
                log.warning(
                    "--universe-only 지정됐으나 analysis_universe 비어있음 — instruments 전체로 fallback"
                )
                rows = conn.execute(
                    "SELECT code FROM instruments WHERE is_tradable = 1 ORDER BY code"
                ).fetchall()
                codes = [r[0] for r in rows]
        else:
            rows = conn.execute(
                "SELECT code FROM instruments WHERE is_tradable = 1 ORDER BY code"
            ).fetchall()
            codes = [r[0] for r in rows]
    finally:
        conn.close()
    return codes[:limit] if limit else codes


def run_batch(codes: list[str]) -> dict:
    total = len(codes)
    ok_cnt = 0
    fail_cnt = 0
    warn_cnt = 0

    with httpx.Client(headers=_HEADERS, timeout=10.0) as client:
        for i, code in enumerate(codes, 1):
            f = fetch(code, client=client)
            if f is None:
                fail_cnt += 1
            else:
                stored = store(f)
                if stored:
                    ok_cnt += 1
                else:
                    fail_cnt += 1
                if f.is_warning or f.is_watch:
                    warn_cnt += 1
            if i % 50 == 0:
                log.info("진행 %d/%d  (성공 %d, 실패 %d, 경고 %d)",
                         i, total, ok_cnt, fail_cnt, warn_cnt)
            time.sleep(_PAGE_SLEEP)

    log.info("배치 완료 — 성공 %d / 실패 %d / 총 %d (관리·주의 %d)",
             ok_cnt, fail_cnt, total, warn_cnt)
    return {"ok": ok_cnt, "fail": fail_cnt, "total": total, "warnings": warn_cnt}


# ------------------------------------------------------------
# 비동기 배치 (concurrency 제한)
# ------------------------------------------------------------

async def _fetch_async(
    client: httpx.AsyncClient,
    code: str,
    sem: asyncio.Semaphore,
) -> NaverFundamentals | None:
    async with sem:
        try:
            r = await client.get(_URL, params={"code": code})
            if r.status_code != 200:
                log.warning("[%s] HTTP %s", code, r.status_code)
                return None
            text = r.content.decode(r.encoding or "euc-kr", errors="replace")
            return parse_html(text, code)
        except Exception as e:
            log.warning("[%s] async fetch 실패: %s", code, e)
            return None


async def _run_batch_async(codes: list[str], concurrency: int = 5) -> dict:
    total = len(codes)
    ok_cnt = 0
    fail_cnt = 0
    warn_cnt = 0
    sem = asyncio.Semaphore(concurrency)
    done = 0
    lock = asyncio.Lock()
    t0 = time.time()

    async with httpx.AsyncClient(
        headers=_HEADERS, timeout=10.0,
        limits=httpx.Limits(max_connections=concurrency * 2),
    ) as client:
        async def _one(code: str):
            nonlocal ok_cnt, fail_cnt, warn_cnt, done
            f = await _fetch_async(client, code, sem)
            async with lock:
                done += 1
                if f is None:
                    fail_cnt += 1
                else:
                    stored = store(f)
                    if stored:
                        ok_cnt += 1
                    else:
                        fail_cnt += 1
                    if f.is_warning or f.is_watch:
                        warn_cnt += 1
                if done % 100 == 0:
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done) / rate if rate > 0 else 0
                    log.info("진행 %d/%d (%.1f/s) ETA %.0fs  성공%d 실패%d 경고%d",
                             done, total, rate, eta, ok_cnt, fail_cnt, warn_cnt)

        await asyncio.gather(*(_one(c) for c in codes))

    log.info("비동기 배치 완료 — 성공 %d / 실패 %d / 총 %d (관리·주의 %d) · %.1fs",
             ok_cnt, fail_cnt, total, warn_cnt, time.time() - t0)
    return {"ok": ok_cnt, "fail": fail_cnt, "total": total, "warnings": warn_cnt}


def run_batch_async(codes: list[str], concurrency: int = 5) -> dict:
    return asyncio.run(_run_batch_async(codes, concurrency=concurrency))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--code", type=str, help="단일 종목 (예: 005930)")
    g.add_argument("--all", action="store_true", help="DB instruments 전체")
    p.add_argument("--limit", type=int, default=None,
                   help="--all 시 상한 (테스트용)")
    p.add_argument("--concurrency", type=int, default=5,
                   help="--all 비동기 동시 요청 수 (기본 5, 네이버 rate limit 주의)")
    p.add_argument("--sync", action="store_true",
                   help="--all 동기 모드 (느림, 디버깅용)")
    p.add_argument("--universe-only", action="store_true",
                   help="analysis_universe (top 500) 만 — daily 운영 권장")
    args = p.parse_args()

    if args.code:
        ok = run(args.code)
        print("OK" if ok else "FAIL")
    else:
        codes = _list_target_codes(limit=args.limit, universe_only=args.universe_only)
        print(f"대상 {len(codes)}종목 · concurrency={args.concurrency}")
        if args.sync:
            stats = run_batch(codes)
        else:
            stats = run_batch_async(codes, concurrency=args.concurrency)
        print(f"완료 — 성공 {stats['ok']} / 실패 {stats['fail']} / 경고 {stats['warnings']}")


if __name__ == "__main__":
    main()
