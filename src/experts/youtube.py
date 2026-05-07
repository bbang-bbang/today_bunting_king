"""유튜브 전문가 — 최근 7일 종목 관련 유튜브 영상 감성·관심도 분석.

앙상블 7번째 전문가 (가중치 7~8%).

점수 배분 (총 100점):
  영상 관심도     25점  (7일 영상 수 기반, 과열 제외)
  감성 비율       35점  (긍정-부정 비율)
  조회수 가중     25점  (고조회수 영상의 감성 반영)
  모드 적합도     15점

설계:
  - DB (youtube_video) 에서 최근 days 일 영상 조회
  - 감성 집계 → 관심도·조회수 가중 룰 기반 점수/조언 생성
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from src.db.connection import get_connection
from src.experts.base import ExpertOpinion, Signal


@dataclass(frozen=True)
class YoutubeVideo:
    upload_date: date
    title: str
    view_count: int
    like_count: int
    sentiment_score: float
    sentiment_label: str    # positive / negative / neutral


class YoutubeExpert:
    name = "youtube"

    # ------------------------------------------------------------
    # 앙상블 인터페이스
    # ------------------------------------------------------------

    def evaluate(self, code: str, as_of: date | None = None) -> ExpertOpinion:
        """앙상블 참여용 — 최근 7일 유튜브 영상을 ExpertOpinion 으로 변환."""
        if as_of is None:
            as_of = date.today()
        videos = self._load_videos(code, as_of, days=7)
        if not videos:
            return ExpertOpinion(
                code=code, expert=self.name, score=0,
                error="유튜브 데이터 없음",
            )
        return self._to_opinion(code, as_of, videos)

    def _to_opinion(
        self, code: str, as_of: date, videos: list[YoutubeVideo]
    ) -> ExpertOpinion:
        total = len(videos)
        pos   = sum(1 for v in videos if v.sentiment_label == "positive")
        neg   = sum(1 for v in videos if v.sentiment_label == "negative")

        signals: list[Signal] = []

        # 1. 영상 관심도 (최대 25점)
        interest_score = self._interest_score(total)
        signals.append(Signal("영상 관심도", interest_score,
                              f"7일 영상 {total}건"))

        # 2. 감성 비율 (최대 35점)
        sentiment_score = max(0.0, (pos - neg) / total * 35) if total > 0 else 0.0
        signals.append(Signal("감성 비율", sentiment_score,
                              f"긍정 {pos} · 부정 {neg} / 전체 {total}건"))

        # 3. 조회수 가중 감성 (최대 25점)
        # 조회수 내림차순 정렬은 DB 조회 시 이미 처리됨 (ORDER BY view_count DESC)
        top3 = videos[:3]
        if top3:
            avg_top3 = sum(v.sentiment_score for v in top3) / len(top3)
        else:
            avg_top3 = 0.0
        view_weighted_score = max(0.0, avg_top3) * 25
        top3_detail = (
            f"상위 3개 평균 sentiment {avg_top3:.3f}"
            if top3 else "상위 영상 없음"
        )
        signals.append(Signal("조회수 가중 감성", view_weighted_score, top3_detail))

        # 4. 모드 적합도 (최대 15점)
        bunt_fit, squeeze_fit = self._compute_mode_fit(total, pos, neg, avg_top3)
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
        """영상 수 기반 관심도 점수 (최대 25점)."""
        if total == 0:
            return 0.0    # 정보 없음
        if total <= 3:
            return 15.0   # 소수
        if total <= 10:
            return 25.0   # 만점 (적정 관심)
        if total <= 20:
            return 18.0   # 다소 과열
        return 8.0        # 과열

    def _compute_mode_fit(
        self, total: int, pos: int, neg: int, avg_top3_sentiment: float
    ) -> tuple[float, float]:
        """번트·스퀴즈 모드 적합도 계산 (0.0~1.0)."""
        bunt, squeeze = 0.5, 0.5

        # 영상 급증 + 강한 긍정 → 스퀴즈 적합도 상승
        if total >= 11 and pos > neg * 2 and avg_top3_sentiment > 0.3:
            squeeze += 0.3
        elif total >= 11 and pos > neg:
            squeeze += 0.1

        # 소수 긍정 영상 (1~3건, 긍정 우세) → 번트 적합도 상승
        if 1 <= total <= 3 and pos > 0 and neg == 0:
            bunt += 0.2
        elif 4 <= total <= 10 and pos > neg:
            bunt += 0.1

        return max(0.0, min(1.0, bunt)), max(0.0, min(1.0, squeeze))

    # ------------------------------------------------------------
    # DB 조회
    # ------------------------------------------------------------

    def _load_videos(
        self, code: str, as_of: date, days: int = 7
    ) -> list[YoutubeVideo]:
        """youtube_video 테이블에서 최근 days일치 영상 로드.

        조회수 내림차순 정렬 (조회수 가중 계산에 활용).
        """
        start = (as_of - timedelta(days=days)).strftime("%Y-%m-%d")
        end   = as_of.strftime("%Y-%m-%d")
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT upload_date, title, view_count, like_count,
                          sentiment_score, sentiment_label
                   FROM youtube_video
                   WHERE code = ? AND upload_date >= ? AND upload_date <= ?
                   ORDER BY view_count DESC""",
                (code, start, end),
            ).fetchall()
        except Exception:
            return []
        finally:
            conn.close()

        out: list[YoutubeVideo] = []
        for r in rows:
            try:
                from datetime import datetime as _dt
                ud = _dt.strptime(r["upload_date"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            out.append(YoutubeVideo(
                upload_date=ud,
                title=r["title"],
                view_count=r["view_count"] or 0,
                like_count=r["like_count"] or 0,
                sentiment_score=r["sentiment_score"] or 0.0,
                sentiment_label=r["sentiment_label"] or "neutral",
            ))
        return out
