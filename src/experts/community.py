"""커뮤니티 전문가 — 최근 7일 종목토론방·StockPlus 게시글 감성·관심도 분석.

앙상블 6번째 전문가 (가중치 7%).

점수 배분 (총 100점):
  관심도 적정성   25점  (7일 게시글 수: 과열도, 침묵도 아닌 적정 관심)
  긍정 감성       35점  (긍정 비율 기반)
  과열 역신호     25점  (과열 없으면 25, 게시글 급증+감성 혼재 시 감점)
  모드 적합도     15점

설계:
  - DB (community_post) 에서 최근 days 일 게시글 조회
  - 감성 집계 → 관심도·과열 룰 기반 점수/조언 생성
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from src.db.connection import get_connection
from src.experts.base import ExpertOpinion, Signal

# 과열 탐지용 키워드
_OVERHEAT_KEYWORDS = ["급등", "상한가", "따상", "따따상", "폭등", "대박", "천장"]


@dataclass(frozen=True)
class CommunityPost:
    posted_at: datetime
    title: str
    view_count: int
    comment_count: int
    sentiment_score: float
    sentiment_label: str          # positive / negative / neutral


class CommunityExpert:
    name = "community"

    # ------------------------------------------------------------
    # 앙상블 인터페이스
    # ------------------------------------------------------------

    def evaluate(self, code: str, as_of: date | None = None) -> ExpertOpinion:
        """앙상블 참여용 — 최근 7일 커뮤니티 게시글을 ExpertOpinion 으로 변환."""
        if as_of is None:
            as_of = date.today()
        posts = self._load_posts(code, as_of, days=7)
        if not posts:
            return ExpertOpinion(
                code=code, expert=self.name, score=0,
                error="커뮤니티 데이터 없음",
            )
        return self._to_opinion(code, as_of, posts)

    def _to_opinion(
        self, code: str, as_of: date, posts: list[CommunityPost]
    ) -> ExpertOpinion:
        total = len(posts)
        pos   = sum(1 for p in posts if p.sentiment_label == "positive")
        neg   = sum(1 for p in posts if p.sentiment_label == "negative")

        # 과열 키워드 포함 게시글 수
        overheat_count = sum(
            1 for p in posts
            if any(kw in p.title for kw in _OVERHEAT_KEYWORDS)
        )

        signals: list[Signal] = []

        # 1. 관심도 적정성 (최대 25점)
        interest_score = self._interest_score(total)
        signals.append(Signal("관심도 적정성", interest_score,
                              f"7일 게시글 {total}건"))

        # 2. 긍정 감성 (최대 35점)
        sentiment_score = max(0.0, (pos - neg) / total * 35) if total > 0 else 0.0
        signals.append(Signal("긍정 감성", sentiment_score,
                              f"긍정 {pos} · 부정 {neg} / 전체 {total}건"))

        # 3. 과열 역신호 안전도 (기본 25점)
        overheat_penalty = 0.0
        overheat_detail = "이상 없음"
        if total >= 31:
            if neg >= pos:
                overheat_penalty = 15.0
                overheat_detail = f"과열({total}건) + 부정 혼재"
            elif pos > neg * 2:
                overheat_penalty = 5.0
                overheat_detail = f"과열({total}건), 긍정 우세 — 경미 감점"
        if overheat_count >= 3:
            overheat_penalty += 10.0
            overheat_detail += f" / 과열 키워드 {overheat_count}건"
        safety_score = max(0.0, 25.0 - overheat_penalty)
        signals.append(Signal("과열 역신호 안전도", safety_score, overheat_detail))

        # 4. 모드 적합도 (최대 15점)
        bunt_fit, squeeze_fit = self._compute_mode_fit(total, pos, neg)
        fit_score = max(bunt_fit, squeeze_fit) * 15
        signals.append(Signal("모드 적합도", fit_score,
                              f"번트 {bunt_fit:.2f} / 스퀴즈 {squeeze_fit:.2f}"))

        score = min(100.0, max(0.0, sum(s.score for s in signals)))
        reason = " · ".join(
            f"{s.name}(+{s.score:.0f})" for s in signals if s.score >= 3
        )[:200]

        return ExpertOpinion(
            code=code, expert=self.name, score=score,
            signals=signals,
            mode_fit={"bunt": bunt_fit, "squeeze": squeeze_fit},
            reason_summary=reason,
        )

    # ------------------------------------------------------------
    # 점수 계산 헬퍼
    # ------------------------------------------------------------

    @staticmethod
    def _interest_score(total: int) -> float:
        """게시글 수 기반 관심도 적정성 점수 (최대 25점)."""
        if total <= 2:
            return 5.0    # 무관심
        if total <= 10:
            return 20.0   # 적정
        if total <= 20:
            return 25.0   # 활발, 만점
        if total <= 30:
            return 15.0   # 다소 과열
        return 5.0        # 과열 주의

    def _compute_mode_fit(
        self, total: int, pos: int, neg: int
    ) -> tuple[float, float]:
        """번트·스퀴즈 모드 적합도 계산 (0.0~1.0)."""
        bunt, squeeze = 0.5, 0.5

        # 활발한 긍정 커뮤니티 + 게시글 증가 → 스퀴즈 적합도 상승
        if total >= 11 and pos > neg * 2:
            squeeze += 0.2
        if total >= 21 and pos > neg:
            squeeze += 0.1

        # 조용한 긍정 (10건 이하, 부정 없음) → 번트 적합도 상승
        if total <= 10 and neg == 0 and pos > 0:
            bunt += 0.2

        return max(0.0, min(1.0, bunt)), max(0.0, min(1.0, squeeze))

    # ------------------------------------------------------------
    # DB 조회
    # ------------------------------------------------------------

    def _load_posts(
        self, code: str, as_of: date, days: int = 7
    ) -> list[CommunityPost]:
        """community_post 테이블에서 최근 days일치 게시글 로드."""
        cutoff = datetime.combine(as_of, datetime.min.time()) - timedelta(days=days)
        end    = datetime.combine(as_of, datetime.max.time())
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT posted_at, title, view_count, comment_count,
                          sentiment_score, sentiment_label
                   FROM community_post
                   WHERE code = ? AND posted_at >= ? AND posted_at <= ?
                   ORDER BY posted_at DESC""",
                (
                    code,
                    cutoff.strftime("%Y-%m-%d %H:%M"),
                    end.strftime("%Y-%m-%d %H:%M"),
                ),
            ).fetchall()
        finally:
            conn.close()

        out: list[CommunityPost] = []
        for r in rows:
            try:
                dt = datetime.strptime(r["posted_at"], "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                continue
            out.append(CommunityPost(
                posted_at=dt,
                title=r["title"],
                view_count=r["view_count"] or 0,
                comment_count=r["comment_count"] or 0,
                sentiment_score=r["sentiment_score"] or 0.0,
                sentiment_label=r["sentiment_label"] or "neutral",
            ))
        return out
