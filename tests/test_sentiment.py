"""키워드 기반 감성 분석 테스트."""
from __future__ import annotations

from src.sentiment import analyze


def test_empty_text_neutral():
    r = analyze("", "")
    assert r.label == "neutral"
    assert r.score == 0.0


def test_strong_positive_title():
    r = analyze("삼성전자 1분기 어닝서프라이즈, 영업이익 9조원", "")
    assert r.label == "positive"
    assert r.score > 0.3
    assert any("어닝서프라이즈" in h for h in r.positive_hits)


def test_strong_negative_title():
    r = analyze("셀트리온 어닝쇼크, 영업이익 50% 급감", "")
    assert r.label == "negative"
    assert r.score < -0.3


def test_title_weighted_more_than_body():
    # 제목엔 호재, 본문엔 악재
    r1 = analyze("수주 계약 체결", "소송 우려")
    assert r1.label == "positive"
    # 반대 — 제목에 악재, 본문에 호재
    r2 = analyze("소송 제기", "수주 계약")
    assert r2.label == "negative"


def test_negation_cancels_positive():
    # "호재 아님" 이면 호재 점수 무효
    r_with = analyze("실적 호재 발표", "")
    r_neg = analyze("실적 호재 아니다", "")
    assert r_with.score > r_neg.score


def test_neutral_when_no_keywords():
    r = analyze("삼성전자 주주총회 개최 예정", "본사에서 열린다")
    assert r.label == "neutral"


def test_mixed_signals_can_offset():
    r = analyze("수주 계약 호재 상승", "적자 소송 우려 부진")
    # 제목 호재 3개 × 2 = 6  vs  본문 악재 4개 × 1 = 4 → 순호재
    assert r.label == "positive"


def test_score_bounded():
    # 키워드 많이 퍼부어도 [-1, 1]
    r_hot = analyze(
        "어닝서프라이즈 목표가상향 수주 호재 상승 급등 강세",
        "수혜 유망 신고가 흑자전환 성장 확대 혁신",
    )
    assert -1.0 <= r_hot.score <= 1.0


def test_positive_and_negative_hits_separately():
    r = analyze("수주 계약", "하락 우려")
    assert r.positive_hits
    assert r.negative_hits


def test_strong_keyword_weighted_2x():
    r_strong = analyze("어닝서프라이즈", "")
    r_normal = analyze("호재", "")
    assert r_strong.score > r_normal.score
