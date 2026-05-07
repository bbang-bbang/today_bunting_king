"""네이버 금융 종목별 뉴스 스크래퍼.

URL: https://finance.naver.com/item/news_news.naver?code={code}&page={n}
페이지당 약 10건. 최근 7일분 수집 시 보통 1~3페이지면 충분.

사용:
  python -m src.crawlers.fetch_news --code 005930
  python -m src.crawlers.fetch_news --code 005930 --days 7

매너:
  - User-Agent 설정
  - 페이지 요청 간 1초 sleep
  - 실패 시 우아하게 빈 리스트 반환 (절대 예외 전파 안 함)
"""
from __future__ import annotations

import argparse
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from src.db.connection import get_connection, init_schema
from src.sentiment import analyze

log = logging.getLogger("bunting.crawler.news")

_BASE_URL = "https://finance.naver.com/item/news_news.naver"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _USER_AGENT, "Referer": "https://finance.naver.com/"}
_PAGE_SLEEP = 1.0
_MAX_PAGES = 10


@dataclass
class RawArticle:
    code: str
    title: str
    url: str
    press: str
    published_at: datetime
    summary: str = ""


# ------------------------------------------------------------
# 파싱 (순수 함수 — HTML → RawArticle 리스트)
# ------------------------------------------------------------

def parse_news_list(html: str, code: str) -> list[RawArticle]:
    """네이버 종목뉴스 목록 HTML 파싱. 실패 시 빈 리스트."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    articles: list[RawArticle] = []

    # 테이블의 type2 클래스 안에 뉴스 행들이 있음. 각 행은 td.title / td.info / td.date
    # 관련/묶음 뉴스는 td class에 'relation_tit' 들어감 → 제외
    rows = soup.select("table.type5 tr") or soup.select("table.type2 tr")
    for tr in rows:
        title_td = tr.select_one("td.title")
        date_td = tr.select_one("td.date")
        info_td = tr.select_one("td.info")
        if not (title_td and date_td):
            continue
        # 관련뉴스 링크는 div 또는 클래스로 구분 — 스킵
        if "relation" in " ".join(tr.get("class", [])):
            continue

        link = title_td.find("a")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        url = _normalize_url(href)
        press = info_td.get_text(strip=True) if info_td else ""
        dt = _parse_datetime(date_td.get_text(strip=True))
        if dt is None or not title or not url:
            continue

        articles.append(RawArticle(
            code=code, title=title, url=url, press=press, published_at=dt,
        ))

    return articles


def _normalize_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://finance.naver.com" + href
    return "https://finance.naver.com/" + href


_DATE_PATTERNS = [
    "%Y.%m.%d %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y.%m.%d",
]


def _parse_datetime(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    # "2026.04.15 10:30" / "2026-04-15 10:30" / "2026.04.15"
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------
# 네트워크 요청
# ------------------------------------------------------------

def fetch_page(code: str, page: int, client: httpx.Client | None = None) -> list[RawArticle]:
    """한 페이지 요청 + 파싱. 실패 시 빈 리스트."""
    own = client is None
    if own:
        client = httpx.Client(headers=_HEADERS, timeout=10.0)
    try:
        r = client.get(_BASE_URL, params={"code": code, "page": page})
        if r.status_code != 200:
            log.warning("네이버 뉴스 [%s p%d] status=%s", code, page, r.status_code)
            return []
        # 네이버 금융은 EUC-KR — apparent_encoding 로 자동 보정
        r.encoding = r.encoding or "euc-kr"
        return parse_news_list(r.text, code)
    except Exception as e:
        log.warning("네이버 뉴스 요청 실패 [%s p%d]: %s", code, page, e)
        return []
    finally:
        if own:
            client.close()


def fetch_recent_news(code: str, days: int = 7, max_pages: int = _MAX_PAGES) -> list[RawArticle]:
    """최근 days 일 기사 수집. 오래된 기사 만나면 조기 종료."""
    cutoff = datetime.now() - timedelta(days=days)
    collected: list[RawArticle] = []
    seen_urls: set[str] = set()

    with httpx.Client(headers=_HEADERS, timeout=10.0) as client:
        for page in range(1, max_pages + 1):
            arts = fetch_page(code, page, client=client)
            if not arts:
                break
            page_all_old = True
            for a in arts:
                if a.url in seen_urls:
                    continue
                if a.published_at >= cutoff:
                    collected.append(a)
                    seen_urls.add(a.url)
                    page_all_old = False
            if page_all_old:
                break
            time.sleep(_PAGE_SLEEP)

    # 최신 순 정렬
    collected.sort(key=lambda a: a.published_at, reverse=True)
    return collected


# ------------------------------------------------------------
# DB 저장
# ------------------------------------------------------------

def store_articles(articles: list[RawArticle]) -> int:
    """기사 리스트 → news_article UPSERT. 감성분석 함께 수행. 저장 건수 반환."""
    if not articles:
        return 0
    init_schema()
    conn = get_connection()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for a in articles:
            sent = analyze(a.title, a.summary)
            rows.append((
                a.code,
                a.published_at.strftime("%Y-%m-%d %H:%M"),
                a.title,
                a.summary,
                a.url,
                a.press,
                sent.score,
                sent.label,
                now,
            ))
        # FK 방어
        conn.execute(
            """INSERT OR IGNORE INTO instruments (code, name, market, is_tradable, updated_at)
               SELECT DISTINCT code, code, 'KOSPI', 1, ?
               FROM (SELECT ? AS code)
               WHERE code NOT IN (SELECT code FROM instruments)""",
            (now, articles[0].code),
        )
        conn.executemany(
            """INSERT INTO news_article
               (code, published_at, title, summary, url, press,
                sentiment_score, sentiment_label, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(code, url) DO UPDATE SET
                 title = excluded.title,
                 summary = excluded.summary,
                 published_at = excluded.published_at,
                 sentiment_score = excluded.sentiment_score,
                 sentiment_label = excluded.sentiment_label,
                 fetched_at = excluded.fetched_at""",
            rows,
        )
        return len(rows)
    finally:
        conn.close()


def run(code: str, days: int = 7) -> int:
    arts = fetch_recent_news(code, days=days)
    n = store_articles(arts)
    log.info("[%s] 뉴스 %d 건 수집 (최근 %d일)", code, n, days)
    return n


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--code", required=True, type=str, help="종목코드 (예: 005930)")
    p.add_argument("--days", type=int, default=7, help="최근 N일 (기본 7)")
    args = p.parse_args()

    n = run(args.code, days=args.days)
    print(f"{args.code} 최근 {args.days}일 뉴스 {n} 건 저장")


if __name__ == "__main__":
    main()
