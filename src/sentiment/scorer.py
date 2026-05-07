"""주식 도메인 한국어 감성 분석 (키워드 사전 기반).

설계:
  - 호재/악재 키워드 리스트 + 강도 가중 (strong=2.0, normal=1.0)
  - 제목은 본문의 2배 가중
  - 부정 어휘("아니", "않", "없", "무산") 가 키워드 근처 10자 내면 매치 무효
  - 최종 점수 = 정규화 후 [-1, +1]
  - 라벨: > +0.15 positive / < -0.15 negative / else neutral

1차 버전은 규칙 기반. LLM 감성분석은 Phase B.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# 강한 호재 (weight 2.0) — 실적·모멘텀 직접 영향
_STRONG_POSITIVE = [
    "어닝서프라이즈", "어닝 서프라이즈", "흑자전환", "흑자 전환",
    "목표가 상향", "목표주가 상향", "투자의견 상향",
    "신고가", "사상 최대", "역대 최대", "최대 실적",
    "급등", "상한가", "폭등",
    "대형 수주", "대규모 수주", "최대 규모 계약",
]

# 일반 호재 (weight 1.0)
_POSITIVE = [
    "호재", "상승", "강세", "반등", "회복",
    "수주", "계약 체결", "낙찰", "선정",
    "수출", "수출 증가", "점유율 확대",
    "배당", "자사주 매입", "자사주매입",
    "승인", "허가", "통과",
    "출시", "런칭", "개시",
    "혁신", "신기술", "특허",
    "확대", "증가", "성장", "호실적", "호조",
    "매수", "매입",
    "수혜", "유망", "긍정",
    "합병", "인수", "파트너십", "협력",
    "흥행", "개봉",
]

# 강한 악재 (weight 2.0)
_STRONG_NEGATIVE = [
    "어닝쇼크", "어닝 쇼크", "적자전환", "적자 전환",
    "상장폐지", "상장 폐지", "관리종목", "감자", "감사의견 거절",
    "파산", "법정관리", "회생 신청",
    "목표가 하향", "투자의견 하향", "투자유의",
    "급락", "폭락", "하한가",
    "유상증자", "전환사채",  # 단타 관점에선 일반적으로 단기 악재
    "횡령", "배임", "분식회계",
]

# 일반 악재 (weight 1.0)
_NEGATIVE = [
    "악재", "하락", "약세", "조정", "부진",
    "손실", "적자", "감소",
    "리콜", "결함", "사고",
    "소송", "고소", "조사", "수사", "압수수색",
    "제재", "과징금", "벌금",
    "경고", "우려", "불안", "위기",
    "취소", "해지", "무산", "중단", "지연",
    "해임", "사퇴", "사임",
    "매도", "매각",
    "축소", "감원", "구조조정",
    "공급 과잉", "경쟁 심화",
]

_NEGATION_WINDOW = 10   # 키워드 주변 글자 수
_NEGATORS = ["아니", "않", "없", "무산", "번복", "취소됐", "철회"]


@dataclass
class SentimentResult:
    score: float                          # -1.0 ~ +1.0
    label: str                            # "positive" / "negative" / "neutral"
    positive_hits: list[str] = field(default_factory=list)
    negative_hits: list[str] = field(default_factory=list)


def _count_weighted(text: str, keywords_strong: list[str], keywords_normal: list[str]) -> tuple[float, list[str]]:
    """텍스트에서 키워드 가중 카운트. 부정어 근접 시 매치 무효."""
    hits: list[str] = []
    total = 0.0
    for kw in keywords_strong:
        for m in re.finditer(re.escape(kw), text):
            if _is_negated(text, m.start(), m.end()):
                continue
            hits.append(kw)
            total += 2.0
    for kw in keywords_normal:
        for m in re.finditer(re.escape(kw), text):
            if _is_negated(text, m.start(), m.end()):
                continue
            hits.append(kw)
            total += 1.0
    return total, hits


def _is_negated(text: str, start: int, end: int) -> bool:
    """키워드 직후 N글자 내에 부정어가 있으면 True."""
    tail = text[end : end + _NEGATION_WINDOW]
    return any(neg in tail for neg in _NEGATORS)


def analyze(title: str, body: str = "") -> SentimentResult:
    """제목 + 본문 감성 분석.

    제목은 2배 가중. 원시 점수를 [-1, +1] 로 정규화 (tanh 아닌 clip + scale).
    """
    title = title or ""
    body = body or ""

    pos_t, pos_t_hits = _count_weighted(title, _STRONG_POSITIVE, _POSITIVE)
    neg_t, neg_t_hits = _count_weighted(title, _STRONG_NEGATIVE, _NEGATIVE)
    pos_b, pos_b_hits = _count_weighted(body, _STRONG_POSITIVE, _POSITIVE)
    neg_b, neg_b_hits = _count_weighted(body, _STRONG_NEGATIVE, _NEGATIVE)

    raw = (pos_t - neg_t) * 2.0 + (pos_b - neg_b)

    # 정규화: ±6 이상이면 ±1.0 로 clip (제목 호재 1개 + 본문 호재 1개 ≈ 3, 강호재 제목 ≈ 4)
    score = max(-1.0, min(1.0, raw / 6.0))

    if score >= 0.15:
        label = "positive"
    elif score <= -0.15:
        label = "negative"
    else:
        label = "neutral"

    return SentimentResult(
        score=round(score, 3),
        label=label,
        positive_hits=pos_t_hits + pos_b_hits,
        negative_hits=neg_t_hits + neg_b_hits,
    )
