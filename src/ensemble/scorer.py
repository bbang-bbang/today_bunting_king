"""앙상블 스코어러 — 기술 + 재무 + 흐름 + 뉴스 + 분봉 + 커뮤니티 + 유튜브 7-expert 가중합.

번트: tech 0.28, fund 0.21, flow 0.13, news 0.09, minute 0.09, community 0.10, youtube 0.10
스퀴즈: tech 0.31, fund 0.12, flow 0.13, news 0.09, minute 0.13, community 0.11, youtube 0.11

가중치 v3 (2026-06-11 보수적 재조정): 성숙 신호 152건(down레짐 치우침)의
전문가점수↔5일수익 상관 측정 결과 — 기술 -0.08(최대가중인데 무상관)·흐름 -0.36(역상관)
이 과대평가, 재무 +0.21·커뮤 +0.41·유튜브 +0.30 이 과소평가로 확인됨. 표본이 단일
레짐이라 과적합 위험 → 방향만 반영해 기술·흐름 소폭↓, 재무·커뮤·유튜브 소폭↑ (기술은
여전히 최대 비중 유지). 측정 루프가 효과를 추적, 데이터 누적 후 재검토.

하드 필터:
  - 재무의 is_acceptable()=False → 즉시 탈락
  - 기술 error → 앙상블 실패
  - 재무/흐름/뉴스/분봉/커뮤니티/유튜브 중 일부 error → 유효한 전문가 가중치로 재정규화 (fallback)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from src.experts.base import ExpertOpinion
from src.experts.community import CommunityExpert
from src.experts.flow import FlowExpert
from src.experts.fundamental import FundamentalExpert
from src.experts.minute import MinuteExpert
from src.experts.news import NewsExpert
from src.experts.technical import TechnicalExpert
from src.experts.youtube import YoutubeExpert


@dataclass(frozen=True)
class EnsembleWeights:
    technical: float
    fundamental: float
    flow: float = 0.0
    news: float = 0.0
    minute: float = 0.0
    community: float = 0.0
    youtube: float = 0.0

    def __post_init__(self):
        total = (
            self.technical + self.fundamental + self.flow
            + self.news + self.minute + self.community + self.youtube
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"가중치 합이 1.0 이 아님: {total}")


# 모드별 기본 가중치 (v3 — 2026-06-11 보수적 재조정, 상관 측정 기반. docstring 참조)
# 번트:   0.28+0.21+0.13+0.09+0.09+0.10+0.10 = 1.00
# 스퀴즈: 0.31+0.12+0.13+0.09+0.13+0.11+0.11 = 1.00
BUNT_WEIGHTS    = EnsembleWeights(technical=0.28, fundamental=0.21, flow=0.13, news=0.09, minute=0.09, community=0.10, youtube=0.10)
SQUEEZE_WEIGHTS = EnsembleWeights(technical=0.31, fundamental=0.12, flow=0.13, news=0.09, minute=0.13, community=0.11, youtube=0.11)


@dataclass
class EnsembleOpinion:
    code: str
    ensemble_score: float
    opinions: dict[str, ExpertOpinion] = field(default_factory=dict)
    mode_fit: dict[str, float] = field(default_factory=dict)
    reason_summary: str = ""
    filtered: bool = False
    filter_reason: str = ""
    fallback_used: bool = False

    @property
    def is_recommended(self) -> bool:
        return not self.filtered and self.ensemble_score > 0


class EnsembleScorer:
    def __init__(
        self,
        mode: str = "bunt",
        technical: TechnicalExpert | None = None,
        fundamental: FundamentalExpert | None = None,
        flow: FlowExpert | None = None,
        news: NewsExpert | None = None,
        minute: MinuteExpert | None = None,
        community: CommunityExpert | None = None,
        youtube: YoutubeExpert | None = None,
        weights: EnsembleWeights | None = None,
    ):
        if mode not in ("bunt", "squeeze"):
            raise ValueError(f"mode must be 'bunt' or 'squeeze', got {mode}")
        self.mode = mode
        self.technical = technical or TechnicalExpert()
        self.fundamental = fundamental or FundamentalExpert()
        self.flow = flow or FlowExpert()
        self.news = news or NewsExpert()
        self.minute = minute or MinuteExpert()
        self.community = community or CommunityExpert()
        self.youtube = youtube or YoutubeExpert()
        self.weights = weights or (
            BUNT_WEIGHTS if mode == "bunt" else SQUEEZE_WEIGHTS
        )

    def evaluate(
        self,
        code: str,
        enriched: pd.DataFrame,
        as_of: date | None = None,
    ) -> EnsembleOpinion:
        if as_of is None and enriched is not None and not enriched.empty:
            try:
                as_of = enriched.index[-1].date()
            except Exception:
                as_of = None

        # 1. 재무 하드 필터
        ok, reason = self.fundamental.is_acceptable(code, as_of=as_of)
        if not ok:
            return EnsembleOpinion(
                code=code, ensemble_score=0.0,
                filtered=True, filter_reason=reason,
                reason_summary=f"필터 탈락: {reason}",
            )

        # 2. 기술 전문가 (필수)
        tech_op = self.technical.evaluate(code, enriched, as_of=as_of)
        if not tech_op.is_valid:
            return EnsembleOpinion(
                code=code, ensemble_score=0.0,
                opinions={"technical": tech_op},
                filtered=True,
                filter_reason=tech_op.error or "기술 평가 실패",
                reason_summary=f"기술 평가 불가: {tech_op.error}",
            )

        # 3. 재무·흐름·뉴스·분봉·커뮤니티·유튜브 전문가 평가
        fund_op      = self.fundamental.evaluate(code, as_of=as_of)
        flow_op      = self.flow.evaluate(code, as_of=as_of)
        news_op      = self.news.evaluate(code, as_of=as_of)
        minute_op    = self.minute.evaluate(code, as_of=as_of)
        community_op = self.community.evaluate(code, as_of=as_of)
        youtube_op   = self.youtube.evaluate(code, as_of=as_of)

        # 4. 유효한 전문가 점수만 가중합, 가중치 재정규화
        valid: list[tuple[str, float, float, ExpertOpinion]] = [
            ("technical", tech_op.score, self.weights.technical, tech_op),
        ]
        if fund_op.is_valid:
            valid.append(("fundamental", fund_op.score, self.weights.fundamental, fund_op))
        if flow_op.is_valid:
            valid.append(("flow", flow_op.score, self.weights.flow, flow_op))
        if news_op.is_valid:
            valid.append(("news", news_op.score, self.weights.news, news_op))
        if minute_op.is_valid:
            valid.append(("minute", minute_op.score, self.weights.minute, minute_op))
        if community_op.is_valid:
            valid.append(("community", community_op.score, self.weights.community, community_op))
        if youtube_op.is_valid:
            valid.append(("youtube", youtube_op.score, self.weights.youtube, youtube_op))

        total_w = sum(w for _, _, w, _ in valid)
        ensemble_score = sum(s * w for _, s, w, _ in valid) / total_w
        fallback = len(valid) < 7   # 7명 전원 유효 아니면 fallback

        # 5. 모드 적합도 가중평균 (유효 전문가만)
        def _fit(key: str) -> float:
            num = 0.0
            for _, _, w, op in valid:
                num += op.mode_fit.get(key, 0.5) * w
            return num / total_w

        mode_fit = {"bunt": _fit("bunt"), "squeeze": _fit("squeeze")}

        parts = [f"기술 {tech_op.score:.0f}"]
        if fund_op.is_valid:
            parts.append(f"재무 {fund_op.score:.0f}")
        else:
            parts.append("재무 ✗")
        if flow_op.is_valid:
            parts.append(f"흐름 {flow_op.score:.0f}")
        else:
            parts.append("흐름 ✗")
        if news_op.is_valid:
            parts.append(f"뉴스 {news_op.score:.0f}")
        else:
            parts.append("뉴스 ✗")
        if minute_op.is_valid:
            parts.append(f"분봉 {minute_op.score:.0f}")
        else:
            parts.append("분봉 ✗")
        if community_op.is_valid:
            parts.append(f"커뮤 {community_op.score:.0f}")
        else:
            parts.append("커뮤 ✗")
        if youtube_op.is_valid:
            parts.append(f"유튜브 {youtube_op.score:.0f}")
        else:
            parts.append("유튜브 ✗")
        parts.append(f"[{self.mode}] 앙상블 {ensemble_score:.1f}")
        reason = " · ".join(parts)

        return EnsembleOpinion(
            code=code,
            ensemble_score=min(100.0, max(0.0, ensemble_score)),
            opinions={
                "technical": tech_op, "fundamental": fund_op,
                "flow": flow_op, "news": news_op,
                "minute": minute_op, "community": community_op,
                "youtube": youtube_op,
            },
            mode_fit=mode_fit,
            reason_summary=reason,
            fallback_used=fallback,
        )
