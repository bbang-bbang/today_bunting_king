"""분석 유니버스 — 크롤러·스케줄러·추천이 공유하는 대상 종목 집합.

2026-04-22 B위원회 결정:
  - 시총 상위 500
  - 20일 평균 거래대금 ≥ 10억원
  - ohlcv 60일 이상
  - 주 1회 (일요일 23:00) 재빌드

공개 API:
  - rebuild_universe(as_of=None) -> int (저장 개수)
  - get_universe_codes() -> list[str]
  - universe_size() -> int
"""
from src.universe.builder import (
    get_universe_codes,
    rebuild_universe,
    universe_size,
)

__all__ = ["rebuild_universe", "get_universe_codes", "universe_size"]
