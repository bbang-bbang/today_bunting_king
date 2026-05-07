"""yt-dlp 를 이용한 유튜브 종목 영상 수집기.

검색 쿼리: "{종목명} 주가"  (예: "삼성전자 주가")
최근 20개 검색 결과(ytsearch20) 중 upload_date 기준 최근 7일 이내만 필터링.
title 감성분석 후 youtube_video 테이블에 UPSERT.

사용:
  python -m src.crawlers.fetch_youtube --code 005930
  python -m src.crawlers.fetch_youtube --code 005930 --days 7

실패 시 우아하게 빈 리스트 반환 (절대 예외 전파 안 함)
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

try:
    import yt_dlp
    _YT_DLP_AVAILABLE = True
except ImportError:
    yt_dlp = None  # type: ignore[assignment]
    _YT_DLP_AVAILABLE = False

from src.db.connection import get_connection, init_schema
from src.sentiment import analyze

log = logging.getLogger("bunting.crawler.youtube")


@dataclass
class RawVideo:
    code: str
    video_id: str
    title: str
    channel: str
    upload_date: date       # 'YYYY-MM-DD' 로 변환된 값
    view_count: int
    like_count: int
    duration: int           # 초


# ------------------------------------------------------------
# instruments 테이블에서 종목명 조회
# ------------------------------------------------------------

def _get_company_name(code: str) -> str | None:
    """instruments 테이블에서 code → name(회사명) 반환. 없으면 None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT name FROM instruments WHERE code = ?", (code,)
        ).fetchone()
        return row["name"] if row else None
    except Exception as e:
        log.warning("종목명 조회 실패 [%s]: %s", code, e)
        return None
    finally:
        conn.close()


# ------------------------------------------------------------
# yt-dlp 수집
# ------------------------------------------------------------

def _parse_upload_date(s: str) -> date | None:
    """'YYYYMMDD' 형식 → date 객체. 실패 시 None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def fetch_videos(code: str, name: str, days: int = 7) -> list[RawVideo]:
    """yt-dlp 로 최근 days일 영상 수집. 실패 시 빈 리스트."""
    if not _YT_DLP_AVAILABLE:
        log.warning("yt-dlp 를 사용할 수 없습니다. pip install yt-dlp")
        return []

    cutoff = date.today() - timedelta(days=days)
    query = f"ytsearch20:{name} 주가"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }

    videos: list[RawVideo] = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            for entry in info.get("entries", []):
                video_id = entry.get("id")
                title = entry.get("title", "")
                if not video_id or not title:
                    continue

                upload_date_str = entry.get("upload_date", "")
                upload_date = _parse_upload_date(upload_date_str)
                # yt-dlp 검색 모드에서는 upload_date 가 None 일 수 있음
                # → 검색 결과 자체가 최근 영상 위주이므로 None 이면 오늘로 간주
                if upload_date is None:
                    upload_date = date.today()
                if upload_date < cutoff:
                    continue

                channel = entry.get("channel", "") or entry.get("uploader", "") or ""
                view_count = int(entry.get("view_count") or 0)
                like_count = int(entry.get("like_count") or 0)
                duration = int(entry.get("duration") or 0)

                videos.append(RawVideo(
                    code=code,
                    video_id=video_id,
                    title=title,
                    channel=channel,
                    upload_date=upload_date,
                    view_count=view_count,
                    like_count=like_count,
                    duration=duration,
                ))
    except Exception as e:
        log.warning("유튜브 수집 실패 [%s / %s]: %s", code, name, e)
        return []

    log.info("[%s] yt-dlp 검색 완료 → %d건 (최근 %d일)", code, len(videos), days)
    return videos


# ------------------------------------------------------------
# DB 저장
# ------------------------------------------------------------

def store_videos(videos: list[RawVideo]) -> int:
    """영상 리스트 → youtube_video UPSERT. 감성분석 함께 수행. 저장 건수 반환."""
    if not videos:
        return 0
    init_schema()
    conn = get_connection()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        code = videos[0].code

        # FK 방어: instruments 에 없으면 더미 삽입
        conn.execute(
            """INSERT OR IGNORE INTO instruments (code, name, market, is_tradable, updated_at)
               VALUES (?, ?, 'KOSPI', 1, ?)""",
            (code, code, now),
        )

        rows = []
        for v in videos:
            sent = analyze(v.title, "")
            rows.append((
                v.code,
                v.video_id,
                v.title,
                v.channel,
                v.upload_date.strftime("%Y-%m-%d"),
                v.view_count,
                v.like_count,
                v.duration,
                sent.score,
                sent.label,
                now,
            ))

        conn.executemany(
            """INSERT INTO youtube_video
               (code, video_id, title, channel, upload_date,
                view_count, like_count, duration,
                sentiment_score, sentiment_label, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(code, video_id) DO UPDATE SET
                 title           = excluded.title,
                 channel         = excluded.channel,
                 upload_date     = excluded.upload_date,
                 view_count      = excluded.view_count,
                 like_count      = excluded.like_count,
                 duration        = excluded.duration,
                 sentiment_score = excluded.sentiment_score,
                 sentiment_label = excluded.sentiment_label,
                 fetched_at      = excluded.fetched_at""",
            rows,
        )
        return len(rows)
    except Exception as e:
        log.error("youtube_video 저장 실패 [%s]: %s", videos[0].code, e)
        return 0
    finally:
        conn.close()


def run(code: str, days: int = 7) -> int:
    name = _get_company_name(code)
    if not name:
        log.warning("[%s] instruments 테이블에 종목명 없음 — 수집 건너뜀", code)
        return 0
    videos = fetch_videos(code, name, days=days)
    n = store_videos(videos)
    log.info("[%s] 유튜브 영상 %d 건 수집 (최근 %d일)", code, n, days)
    return n


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--code", required=True, type=str, help="종목코드 (예: 005930)")
    p.add_argument("--days", type=int, default=7, help="최근 N일 (기본 7)")
    args = p.parse_args()

    n = run(args.code, days=args.days)
    print(f"{args.code} 최근 {args.days}일 유튜브 영상 {n} 건 저장")


if __name__ == "__main__":
    main()
