"""전문가 공통 타입.

추천 엔진은 여러 전문가(기술/재무/흐름)의 ExpertOpinion 을 앙상블한다.
각 전문가는 evaluate(code, ctx) → ExpertOpinion 을 반환한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Signal:
    """전문가가 감지한 개별 시그널."""
    name: str           # "RSI 과매도 근접"
    score: float        # 점수 가산분
    detail: str = ""    # 사용자 노출용 수치 설명


@dataclass
class ExpertOpinion:
    """전문가 1명의 평가 결과."""
    code: str
    expert: str                                # "technical" / "fundamental" / "flow"
    score: float                                # 0~100
    signals: list[Signal] = field(default_factory=list)
    mode_fit: dict[str, float] = field(default_factory=dict)   # {"bunt": 0~1, "squeeze": 0~1}
    reason_summary: str = ""                    # 사용자에게 보여줄 한 줄 요약
    error: str = ""                             # 평가 불가 시 사유 (예: 데이터 부족)

    @property
    def is_valid(self) -> bool:
        return not self.error
