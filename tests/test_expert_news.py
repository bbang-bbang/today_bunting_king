"""뉴스 전문가 테스트.

DB 직접 주입해서 NewsExpert.analyze() 가 기대한 리포트를 만드는지 검증.
크롤러 파서는 별도 (test_crawler_news_parse) 에서.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.crawlers.fetch_news import RawArticle, parse_news_list, store_articles
from src.db.connection import get_connection, init_schema
from src.experts.news import NewsExpert


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """임시 SQLite 파일로 DB 격리."""
    from src import config
    db_path = tmp_path / "news_test.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    # src.db.connection 이 import 시 DB_PATH 를 참조하므로 monkeypatch 로 덮어씀
    from src.db import connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", db_path)
    init_schema()
    yield db_path


def _raw(code: str, title: str, days_ago: int, press: str = "한경", summary: str = "") -> RawArticle:
    return RawArticle(
        code=code, title=title, url=f"https://n.news/{title[:10]}/{days_ago}",
        press=press, published_at=datetime.now() - timedelta(days=days_ago),
        summary=summary,
    )


def test_no_articles_returns_error_report(fresh_db):
    rpt = NewsExpert().analyze("005930", days=7)
    assert not rpt.is_valid
    assert "없음" in rpt.error


def test_positive_cluster_report(fresh_db):
    arts = [
        _raw("005930", "삼성전자 어닝서프라이즈 발표, 영업이익 9조원", 0),
        _raw("005930", "HBM4 대형 수주 계약 체결", 1),
        _raw("005930", "목표주가 상향, 강한 성장 전망", 2),
    ]
    store_articles(arts)
    rpt = NewsExpert().analyze("005930", days=7)
    assert rpt.is_valid
    assert rpt.total_count == 3
    assert len(rpt.positive) >= 2
    assert len(rpt.negative) == 0
    assert "호재" in rpt.short_summary or "우호" in rpt.short_summary
    assert "모멘텀" in rpt.advice or "호재" in rpt.advice


def test_strong_negative_triggers_caution(fresh_db):
    arts = [
        _raw("005930", "어닝쇼크, 1분기 적자전환", 0),
        _raw("005930", "대규모 횡령 사건 적발", 1),
    ]
    store_articles(arts)
    rpt = NewsExpert().analyze("005930", days=7)
    assert rpt.is_valid
    assert len(rpt.negative) >= 1
    assert "악재" in rpt.short_summary or "주의" in rpt.short_summary
    assert "관망" in rpt.advice or "손절" in rpt.advice or "추격" in rpt.advice


def test_mixed_signals_report(fresh_db):
    arts = [
        _raw("005930", "수주 계약 체결", 0),
        _raw("005930", "수출 증가", 1),
        _raw("005930", "소송 제기", 2),
    ]
    store_articles(arts)
    rpt = NewsExpert().analyze("005930", days=7)
    assert rpt.is_valid
    assert len(rpt.positive) >= 1
    assert len(rpt.negative) >= 1


def test_only_neutral_articles(fresh_db):
    arts = [
        _raw("005930", "삼성전자 주주총회 개최 예정", 0),
        _raw("005930", "본사 조직개편 발표", 1),
    ]
    store_articles(arts)
    rpt = NewsExpert().analyze("005930", days=7)
    assert rpt.is_valid
    # neutral 만 있으면 advice 는 "뉴스 외 지표" 류
    assert "이슈 없음" in rpt.advice or "지표" in rpt.advice


def test_period_filter_excludes_old(fresh_db):
    arts = [
        _raw("005930", "수주 호재 최근", 1),
        _raw("005930", "과거 악재 소송", 30),   # 30일 전 → 7일 창 밖
    ]
    store_articles(arts)
    rpt = NewsExpert().analyze("005930", days=7)
    assert rpt.total_count == 1
    # 과거 악재는 제외돼야 함
    assert all("최근" in a.title or "수주" in a.title for a in rpt.positive)


def test_different_codes_isolated(fresh_db):
    store_articles([_raw("005930", "삼성 호재 수주", 0)])
    store_articles([_raw("000660", "SK 악재 소송", 0)])
    r_s = NewsExpert().analyze("005930", days=7)
    r_h = NewsExpert().analyze("000660", days=7)
    assert r_s.total_count == 1
    assert r_h.total_count == 1
    assert len(r_s.positive) == 1 and len(r_h.negative) == 1


def test_short_summary_contains_counts(fresh_db):
    arts = [_raw("005930", f"호재 수주 계약 #{i}", i) for i in range(3)]
    store_articles(arts)
    rpt = NewsExpert().analyze("005930", days=7)
    assert "3" in rpt.short_summary   # 총 건수 표기


def test_full_report_has_sections(fresh_db):
    arts = [
        _raw("005930", "어닝서프라이즈 호재", 0),
        _raw("005930", "소송 제기 악재", 1),
    ]
    store_articles(arts)
    rpt = NewsExpert().analyze("005930", days=7)
    assert "호재" in rpt.full_report
    assert "악재" in rpt.full_report
    assert "종합 조언" in rpt.full_report


# ------------------------------------------------------------
# 파서 단독 테스트
# ------------------------------------------------------------

_SAMPLE_HTML = """
<html><body>
<table class="type5">
  <tr>
    <td class="title"><a href="/item/news_read.naver?article_id=1">삼성전자, 어닝서프라이즈 발표</a></td>
    <td class="info">한국경제</td>
    <td class="date">2026.04.15 10:30</td>
  </tr>
  <tr>
    <td class="title"><a href="/item/news_read.naver?article_id=2">대형 수주 계약 체결</a></td>
    <td class="info">매일경제</td>
    <td class="date">2026.04.14 14:20</td>
  </tr>
</table>
</body></html>
"""


def test_parser_extracts_articles():
    arts = parse_news_list(_SAMPLE_HTML, "005930")
    assert len(arts) == 2
    assert arts[0].title == "삼성전자, 어닝서프라이즈 발표"
    assert arts[0].press == "한국경제"
    assert arts[0].published_at.year == 2026
    assert arts[0].url.startswith("https://finance.naver.com/")


def test_parser_handles_empty_html():
    assert parse_news_list("", "005930") == []
    assert parse_news_list("<html></html>", "005930") == []


def test_parser_skips_rows_without_date():
    html = """<table class="type5"><tr><td class="title"><a href="/x">t</a></td></tr></table>"""
    assert parse_news_list(html, "005930") == []
