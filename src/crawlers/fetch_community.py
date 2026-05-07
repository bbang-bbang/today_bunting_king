"""네이버 종목토론방 + StockPlus 커뮤니티 게시글 스크래퍼.

수집 대상:
  - 네이버 종목토론방: https://finance.naver.com/item/board.naver?code={code}&page={n}
  - StockPlus: https://stockplus.com/m/stocks/{code}/board

최근 7일분 수집. 오래된 게시글 만나면 조기 종료.
DB: community_post 테이블 UPSERT (감성분석 포함).

사용:
  python -m src.crawlers.fetch_community --code 005930
  python -m src.crawlers.fetch_community --code 005930 --days 7

매너:
  - User-Agent 설정
  - 페이지 요청 간 1초 sleep
  - 실패 시 우아하게 빈 리스트 반환 (절대 예외 전파 안 함)
"""
from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from src.db.connection import get_connection, init_schema
from src.sentiment import analyze

log = logging.getLogger("bunting.crawler.community")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _USER_AGENT, "Referer": "https://finance.naver.com/"}
_PAGE_SLEEP = 1.0
_MAX_PAGES = 10

# 네이버 종목토론방
_NAVER_BOARD_URL = "https://finance.naver.com/item/board.naver"
# StockPlus 게시판
_STOCKPLUS_URL = "https://stockplus.com/m/stocks/{code}/board"

# 날짜 포맷 패턴
_DATE_PATTERNS = [
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y.%m.%d",
    "%Y-%m-%d",
]


@dataclass
class RawPost:
    code: str
    source: str                  # 'naver' | 'stockplus'
    title: str
    posted_at: datetime
    view_count: int = 0
    comment_count: int = 0


# ------------------------------------------------------------
# 공통 파싱 유틸
# ------------------------------------------------------------

def _parse_datetime(s: str) -> datetime | None:
    """날짜 문자열 파싱. 실패 시 None."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _safe_int(s: str | None) -> int:
    """숫자 문자열을 int로 변환. 실패 시 0."""
    try:
        if not s:
            return 0
        # 쉼표·공백 제거 후 변환
        cleaned = s.strip().replace(",", "")
        return int(cleaned)
    except (ValueError, TypeError):
        return 0


# ------------------------------------------------------------
# 네이버 종목토론방 파싱
# ------------------------------------------------------------

def parse_naver_board(html: str, code: str) -> list[RawPost]:
    """네이버 종목토론방 HTML 파싱. 실패 시 빈 리스트.

    2026-04 기준 네이버 구조: table.type2 > tr > td 6개
      [0] 날짜  [1] 제목  [2] 작성자  [3] 조회수  [4] 댓글수  [5] 기타
    또는 구 구조: td.date + td a + td.r (호환 유지)
    """
    if not html:
        return []
    posts: list[RawPost] = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.type2 tr")
        for tr in rows:
            try:
                tds = tr.select("td")
                if len(tds) < 5:
                    continue

                # 새 구조: 6개 td 순서대로
                date_text = tds[0].get_text(strip=True)
                dt = _parse_datetime(date_text)
                if dt is None:
                    continue

                title_a = tds[1].select_one("a") if len(tds) > 1 else None
                if not title_a:
                    # fallback: 아무 td의 a 태그
                    title_a = tr.select_one("td a")
                if not title_a:
                    continue
                title = title_a.get_text(strip=True)
                if not title:
                    continue

                view_count = _safe_int(tds[3].get_text(strip=True)) if len(tds) > 3 else 0
                comment_count = _safe_int(tds[4].get_text(strip=True)) if len(tds) > 4 else 0

                posts.append(RawPost(
                    code=code,
                    source="naver",
                    title=title,
                    posted_at=dt,
                    view_count=view_count,
                    comment_count=comment_count,
                ))
            except Exception as row_err:
                log.debug("네이버 토론방 행 파싱 스킵: %s", row_err)
                continue
    except Exception as e:
        log.warning("네이버 토론방 HTML 파싱 실패: %s", e)
    return posts


# ------------------------------------------------------------
# 네이버 종목토론방 수집
# ------------------------------------------------------------

def fetch_naver_board_page(
    code: str, page: int, client: httpx.Client | None = None
) -> list[RawPost]:
    """네이버 종목토론방 한 페이지 요청 + 파싱. 실패 시 빈 리스트."""
    own = client is None
    if own:
        client = httpx.Client(headers=_HEADERS, timeout=10.0)
    try:
        r = client.get(_NAVER_BOARD_URL, params={"code": code, "page": page})
        if r.status_code != 200:
            log.warning("네이버 토론방 [%s p%d] status=%s", code, page, r.status_code)
            return []
        # 네이버 금융은 EUC-KR
        r.encoding = r.encoding or "euc-kr"
        return parse_naver_board(r.text, code)
    except Exception as e:
        log.warning("네이버 토론방 요청 실패 [%s p%d]: %s", code, page, e)
        return []
    finally:
        if own:
            client.close()


def fetch_naver_recent_posts(
    code: str, days: int = 7, max_pages: int = _MAX_PAGES
) -> list[RawPost]:
    """최근 days일 네이버 종목토론방 게시글 수집. 오래된 게시글 만나면 조기 종료."""
    cutoff = datetime.now() - timedelta(days=days)
    collected: list[RawPost] = []
    seen: set[tuple] = set()      # (title, posted_at) 중복 방지

    with httpx.Client(headers=_HEADERS, timeout=10.0) as client:
        for page in range(1, max_pages + 1):
            posts = fetch_naver_board_page(code, page, client=client)
            if not posts:
                break
            page_all_old = True
            for p in posts:
                key = (p.title, p.posted_at)
                if key in seen:
                    continue
                if p.posted_at >= cutoff:
                    collected.append(p)
                    seen.add(key)
                    page_all_old = False
            if page_all_old:
                break
            time.sleep(_PAGE_SLEEP)

    collected.sort(key=lambda p: p.posted_at, reverse=True)
    return collected


# ------------------------------------------------------------
# StockPlus 파싱
# ------------------------------------------------------------

def parse_stockplus_board(html: str, code: str) -> list[RawPost]:
    """StockPlus 게시판 HTML 파싱. 구조 변경 등 실패 시 빈 리스트 (graceful degradation)."""
    if not html:
        return []
    posts: list[RawPost] = []
    try:
        soup = BeautifulSoup(html, "lxml")

        # ul.board-list li 구조 시도
        items = soup.select("ul.board-list li") or soup.select("div.post-item")
        for item in items:
            try:
                # 제목
                title_el = (
                    item.select_one("a.title")
                    or item.select_one("span.title")
                    or item.select_one("p.title")
                    or item.select_one("a")
                )
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title:
                    continue

                # 날짜
                date_el = (
                    item.select_one("span.date")
                    or item.select_one("time")
                    or item.select_one("span.time")
                )
                date_str = ""
                if date_el:
                    date_str = date_el.get("datetime", "") or date_el.get_text(strip=True)
                dt = _parse_datetime(date_str)
                if dt is None:
                    continue

                # 조회수 (있으면)
                view_el = item.select_one("span.view") or item.select_one("span.hit")
                view_count = _safe_int(view_el.get_text(strip=True)) if view_el else 0

                # 댓글수 (있으면)
                cmt_el = item.select_one("span.comment") or item.select_one("span.reply")
                comment_count = _safe_int(cmt_el.get_text(strip=True)) if cmt_el else 0

                posts.append(RawPost(
                    code=code,
                    source="stockplus",
                    title=title,
                    posted_at=dt,
                    view_count=view_count,
                    comment_count=comment_count,
                ))
            except Exception as row_err:
                log.debug("StockPlus 행 파싱 스킵: %s", row_err)
                continue
    except Exception as e:
        log.warning("StockPlus HTML 파싱 실패: %s", e)
    return posts


# ------------------------------------------------------------
# StockPlus 수집
# ------------------------------------------------------------

def fetch_stockplus_posts(code: str, days: int = 7) -> list[RawPost]:
    """StockPlus 게시글 수집. 실패 시 빈 리스트."""
    url = _STOCKPLUS_URL.format(code=code)
    try:
        with httpx.Client(headers=_HEADERS, timeout=10.0, follow_redirects=True) as client:
            r = client.get(url)
            if r.status_code != 200:
                log.warning("StockPlus [%s] status=%s", code, r.status_code)
                return []
            posts = parse_stockplus_board(r.text, code)
    except Exception as e:
        log.warning("StockPlus 요청 실패 [%s]: %s", code, e)
        return []

    # 날짜 필터: 최근 days일분만
    cutoff = datetime.now() - timedelta(days=days)
    posts = [p for p in posts if p.posted_at >= cutoff]
    posts.sort(key=lambda p: p.posted_at, reverse=True)
    return posts


# ------------------------------------------------------------
# DB 저장
# ------------------------------------------------------------

def store_posts(posts: list[RawPost]) -> int:
    """게시글 리스트 → community_post UPSERT. 감성분석 포함. 저장 건수 반환."""
    if not posts:
        return 0
    init_schema()
    conn = get_connection()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for p in posts:
            try:
                sent = analyze(p.title, "")
            except Exception:
                # 감성분석 실패 시 neutral 기본값
                from src.sentiment.scorer import SentimentResult
                sent = SentimentResult(score=0.0, label="neutral")
            rows.append((
                p.code,
                p.source,
                p.posted_at.strftime("%Y-%m-%d %H:%M"),
                p.title,
                p.view_count,
                p.comment_count,
                sent.score,
                sent.label,
                now,
            ))

        # FK 방어: instruments에 없는 code는 자동 INSERT OR IGNORE
        if rows:
            conn.execute(
                """INSERT OR IGNORE INTO instruments (code, name, market, is_tradable, updated_at)
                   VALUES (?, ?, 'KOSPI', 1, ?)""",
                (posts[0].code, posts[0].code, now),
            )

        conn.executemany(
            """INSERT INTO community_post
               (code, source, posted_at, title, view_count, comment_count,
                sentiment_score, sentiment_label, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(code, source, title, posted_at) DO UPDATE SET
                 view_count      = excluded.view_count,
                 comment_count   = excluded.comment_count,
                 sentiment_score = excluded.sentiment_score,
                 sentiment_label = excluded.sentiment_label,
                 fetched_at      = excluded.fetched_at""",
            rows,
        )
        return len(rows)
    finally:
        conn.close()


# ------------------------------------------------------------
# 메인 실행 진입점
# ------------------------------------------------------------

def run(code: str, days: int = 7) -> int:
    """네이버 종목토론방 + StockPlus 수집 후 DB 저장. 저장 건수 반환."""
    naver_posts = fetch_naver_recent_posts(code, days=days)
    log.info("[%s] 네이버 토론방 %d 건 수집", code, len(naver_posts))

    stockplus_posts = fetch_stockplus_posts(code, days=days)
    log.info("[%s] StockPlus %d 건 수집", code, len(stockplus_posts))

    all_posts = naver_posts + stockplus_posts
    n = store_posts(all_posts)
    log.info("[%s] 커뮤니티 %d 건 저장 (최근 %d일)", code, n, days)
    return n


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--code", required=True, type=str, help="종목코드 (예: 005930)")
    p.add_argument("--days", type=int, default=7, help="최근 N일 (기본 7)")
    args = p.parse_args()

    n = run(args.code, days=args.days)
    print(f"{args.code} 최근 {args.days}일 커뮤니티 게시글 {n} 건 저장")


if __name__ == "__main__":
    main()
