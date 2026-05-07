"""뉴스 전문가 — 최근 7일 종목 뉴스 수집·감성분석·앙상블 점수 생성.

앙상블 4번째 전문가 (가중치 10%).
  - 용도 1: evaluate(code) → ExpertOpinion  (앙상블 참여)
  - 용도 2: analyze(code)  → NewsReport     (상세 리포트 / /news 명령)
  - 용도 3: 앙상블 추천 결과에 short_summary 1줄 자동 첨부

점수 배분 (총 100점 상한):
  감성 비율        40점  (호재-악재 비율 기반)
  강한 호재 가산   20점  (sentiment_score ≥ 0.4 기사당 +10, 최대 20)
  악재 안전도      20점  (강한 악재 없으면 20, 1건당 -15)
  뉴스 활성도      10점  (3건 이상이면 만점)
  모드 적합도      10점

설계:
  - DB (news_article) 에서 최근 days 일 기사 조회
  - 감성 집계 → 호재/악재 카테고리 + 룰 기반 점수/조언 생성
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from src.db.connection import get_connection
from src.experts.base import ExpertOpinion, Signal


@dataclass(frozen=True)
class NewsArticle:
    title: str
    url: str
    press: str
    published_at: datetime
    sentiment_score: float
    sentiment_label: str                 # positive/negative/neutral


@dataclass
class NewsReport:
    code: str
    as_of: date
    period_days: int
    total_count: int = 0
    positive: list[NewsArticle] = field(default_factory=list)
    negative: list[NewsArticle] = field(default_factory=list)
    neutral: list[NewsArticle] = field(default_factory=list)
    short_summary: str = ""              # 1줄 — 추천 결과 자동 첨부용
    full_report: str = ""                # 여러 줄 — /news 명령용
    advice: str = ""                     # 종합 조언 (템플릿)
    error: str = ""

    @property
    def is_valid(self) -> bool:
        return not self.error and self.total_count > 0


class NewsExpert:
    name = "news"

    TOP_N_PER_CATEGORY = 5

    # ------------------------------------------------------------
    # 앙상블 인터페이스
    # ------------------------------------------------------------

    def evaluate(self, code: str, as_of: date | None = None) -> ExpertOpinion:
        """앙상블 참여용 — 최근 7일 뉴스를 ExpertOpinion 으로 변환."""
        report = self.analyze(code, as_of=as_of, days=7)
        if not report.is_valid:
            return ExpertOpinion(
                code=code, expert=self.name, score=0,
                error=report.error or "뉴스 데이터 없음",
            )
        return self._to_opinion(code, report)

    def _to_opinion(self, code: str, report: NewsReport) -> ExpertOpinion:
        total      = report.total_count
        pos        = len(report.positive)
        neg        = len(report.negative)
        strong_pos = sum(1 for a in report.positive if a.sentiment_score >= 0.4)
        strong_neg = sum(1 for a in report.negative if a.sentiment_score <= -0.4)

        signals: list[Signal] = []

        # 1. 감성 비율 (최대 40점)
        ratio_score = max(0.0, (pos - neg) / total) * 40
        signals.append(Signal("감성 비율", ratio_score,
                              f"호재 {pos} · 악재 {neg} / 전체 {total}건"))

        # 2. 강한 호재 가산 (최대 20점, 1건당 +10)
        if strong_pos > 0:
            signals.append(Signal("강한 호재", min(strong_pos * 10, 20),
                                  f"{strong_pos}건 (감성≥0.4)"))

        # 3. 악재 안전도 (강한 악재 없으면 20점, 1건당 -15점)
        safety = max(0.0, 20.0 - strong_neg * 15)
        signals.append(Signal("악재 안전도", safety, f"강한 악재 {strong_neg}건"))

        # 4. 뉴스 활성도 (3건 이상 = 만점 10점)
        signals.append(Signal("뉴스 활성도", min(total / 3, 1.0) * 10, f"{total}건"))

        # 5. 모드 적합도 (최대 10점)
        bunt_fit, squeeze_fit = self._compute_mode_fit(report)
        signals.append(Signal("모드 적합도", max(bunt_fit, squeeze_fit) * 10,
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

    def _compute_mode_fit(self, report: NewsReport) -> tuple[float, float]:
        bunt, squeeze = 0.5, 0.5
        pos        = len(report.positive)
        neg        = len(report.negative)
        total      = report.total_count
        strong_pos = sum(1 for a in report.positive if a.sentiment_score >= 0.4)

        # 강한 호재 집중 + 뉴스 폭증 → 스퀴즈
        if strong_pos >= 2:
            squeeze += 0.2
        if total >= 10 and pos > neg * 2:
            squeeze += 0.15

        # 꾸준한 호재, 악재 없음, 뉴스 과열 아님 → 번트
        if pos > 0 and neg == 0 and total < 10:
            bunt += 0.2

        return max(0.0, min(1.0, bunt)), max(0.0, min(1.0, squeeze))

    # ------------------------------------------------------------
    # 상세 리포트 인터페이스
    # ------------------------------------------------------------

    def analyze(self, code: str, as_of: date | None = None, days: int = 7) -> NewsReport:
        if as_of is None:
            as_of = date.today()
        articles = self._load_articles(code, as_of, days)
        if not articles:
            return NewsReport(
                code=code, as_of=as_of, period_days=days,
                error="수집된 뉴스 없음",
                short_summary="📰 최근 뉴스 없음",
                full_report=f"📰 {code} 최근 {days}일 뉴스 없음 — `python -m src.crawlers.fetch_news --code {code}` 로 수집 필요",
                advice="뉴스 데이터 부족 — 판단 보류.",
            )

        positive = [a for a in articles if a.sentiment_label == "positive"]
        negative = [a for a in articles if a.sentiment_label == "negative"]
        neutral = [a for a in articles if a.sentiment_label == "neutral"]

        positive.sort(key=lambda a: (a.sentiment_score, a.published_at), reverse=True)
        negative.sort(key=lambda a: (a.sentiment_score, a.published_at))
        neutral.sort(key=lambda a: a.published_at, reverse=True)

        strong_pos = [a for a in positive if a.sentiment_score >= 0.4]
        strong_neg = [a for a in negative if a.sentiment_score <= -0.4]

        short = self._build_short_summary(
            total=len(articles), pos=len(positive), neg=len(negative),
            strong_pos=len(strong_pos), strong_neg=len(strong_neg),
        )
        advice = self._build_advice(
            total=len(articles), pos=len(positive), neg=len(negative),
            strong_pos=len(strong_pos), strong_neg=len(strong_neg),
            days=days,
        )
        full = self._build_full_report(
            code=code, as_of=as_of, days=days,
            positive=positive, negative=negative, neutral=neutral, advice=advice,
        )

        return NewsReport(
            code=code, as_of=as_of, period_days=days,
            total_count=len(articles),
            positive=positive, negative=negative, neutral=neutral,
            short_summary=short, full_report=full, advice=advice,
        )

    # ------------------------------------------------------------
    # 자연어 조언 생성 (룰 기반)
    # ------------------------------------------------------------

    def _build_short_summary(self, total: int, pos: int, neg: int,
                             strong_pos: int, strong_neg: int) -> str:
        if strong_neg >= 1:
            return f"📰 최근 뉴스 {total}건 중 강한 악재 {strong_neg}건 — 주의"
        if strong_pos >= 2 and neg == 0:
            return f"📰 최근 뉴스 {total}건, 강한 호재 {strong_pos}건 집중"
        if pos > neg and pos >= 2:
            return f"📰 최근 뉴스 {total}건 (호재 {pos} · 악재 {neg}) — 우호적"
        if neg > pos:
            return f"📰 최근 뉴스 {total}건 (호재 {pos} · 악재 {neg}) — 부정적"
        if total >= 15:
            return f"📰 최근 뉴스 {total}건 — 과열 주의 (테마·작전 확인 필요)"
        return f"📰 최근 뉴스 {total}건 (호재 {pos} · 악재 {neg} · 중립 {total - pos - neg})"

    def _build_advice(self, total: int, pos: int, neg: int,
                      strong_pos: int, strong_neg: int, days: int) -> str:
        lines = []
        # 악재 우선
        if strong_neg >= 1:
            lines.append(
                f"강한 악재 {strong_neg}건 감지 — 추격매수 지양, 진입 시 손절가를 평소보다 타이트하게."
            )
            if pos >= 2:
                lines.append("호재도 공존하나 악재 영향이 우선. 관망 권장.")
            return " ".join(lines)

        # 호재 집중
        if strong_pos >= 2 and neg == 0:
            lines.append("강한 호재 집중 — 모멘텀 구간. 스퀴즈 모드 적합도 상승.")
            lines.append("다만 이미 가격에 반영됐을 수 있으니 단기 추격은 진입가 엄격히.")
            return " ".join(lines)

        # 일반 호재 우세
        if pos > neg and pos >= 2:
            lines.append(f"호재 {pos}건이 악재 {neg}건을 상회 — 우호적 흐름.")
            lines.append("번트·스퀴즈 모두 가능. 기술·수급 전문가 판단 따를 것.")
            return " ".join(lines)

        # 악재 우세 (약)
        if neg > pos:
            lines.append(f"악재 {neg}건이 호재 {pos}건보다 우세 — 신중한 접근.")
            lines.append("번트 모드라도 진입 시 평소보다 포지션 크기를 줄이는 편이 안전.")
            return " ".join(lines)

        # 과열 경고
        if total >= 15:
            lines.append(f"최근 {days}일 뉴스 {total}건으로 과열 — 테마주·작전 가능성 점검.")
            lines.append("명확한 호재 없이 건수만 폭증하면 스퀴즈 진입은 피할 것.")
            return " ".join(lines)

        # 중립/약한 시그널
        return "특별한 이슈 없음. 뉴스 외 지표(기술·수급)에 무게를 두고 판단."

    def _build_full_report(self, code: str, as_of: date, days: int,
                           positive: list[NewsArticle],
                           negative: list[NewsArticle],
                           neutral: list[NewsArticle],
                           advice: str) -> str:
        total = len(positive) + len(negative) + len(neutral)
        lines = [f"📰 {code} 최근 {days}일 뉴스 ({total}건) — 기준일 {as_of}"]

        if positive:
            lines.append(f"\n🟢 호재 ({len(positive)}건)")
            for a in positive[: self.TOP_N_PER_CATEGORY]:
                lines.append(self._fmt_article(a))

        if negative:
            lines.append(f"\n🔴 악재 ({len(negative)}건)")
            for a in negative[: self.TOP_N_PER_CATEGORY]:
                lines.append(self._fmt_article(a))

        if neutral and not positive and not negative:
            lines.append(f"\n⚪ 중립 ({len(neutral)}건)")
            for a in neutral[: self.TOP_N_PER_CATEGORY]:
                lines.append(self._fmt_article(a))

        lines.append(f"\n💬 종합 조언\n  {advice}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_article(a: NewsArticle) -> str:
        d = a.published_at.strftime("%m/%d")
        press = (a.press or "").strip()[:8]
        title = a.title[:60]
        return f"  · {d} [{press}] {title}"

    # ------------------------------------------------------------
    # DB 조회
    # ------------------------------------------------------------

    def _load_articles(self, code: str, as_of: date, days: int) -> list[NewsArticle]:
        cutoff = (datetime.combine(as_of, datetime.min.time()) - timedelta(days=days))
        end = datetime.combine(as_of, datetime.max.time())
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT title, url, press, published_at, sentiment_score, sentiment_label
                   FROM news_article
                   WHERE code = ? AND published_at >= ? AND published_at <= ?
                   ORDER BY published_at DESC""",
                (code, cutoff.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")),
            ).fetchall()
        finally:
            conn.close()

        out: list[NewsArticle] = []
        for r in rows:
            try:
                dt = datetime.strptime(r["published_at"], "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                continue
            out.append(NewsArticle(
                title=r["title"],
                url=r["url"],
                press=r["press"] or "",
                published_at=dt,
                sentiment_score=r["sentiment_score"] or 0.0,
                sentiment_label=r["sentiment_label"] or "neutral",
            ))
        return out
