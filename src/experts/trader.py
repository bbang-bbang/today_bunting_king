"""TraderExpert — 매매 가격 권고 모듈.

기존 experts/* 와 다름: 점수 매기는 expert 가 아니라, 매매 시점에
"이 가격에 사세요/파세요" 권고만 하는 헬퍼. 실제 결정은 사용자.

가격 권고 종류:
- aggressive  : 현재가 + 1틱 (즉시 체결 우선)
- passive     : 현재가 - 1% (더 싸게, 미체결 위험 감수)
- support_5d  : 최근 5일 저가 평균 (단기 지지선) — ohlcv 있을 때만
- ma_20d      : 20일 이동평균 (중기 추세선) — ohlcv 있을 때만
- bollinger_lower: 20일 볼린저 하단 (mean - 2*std) — ohlcv 있을 때만

향후 확장 (시즌 2): 호가창 imbalance, ATR, 거래량 가중 평균
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from src.risk.guard import _tick_size, align_to_tick


@dataclass
class OHLCVRow:
    """ohlcv_daily 한 행 — 시그널 계산 입력."""
    date: str
    open: int
    high: int
    low: int
    close: int
    volume: int


@dataclass
class PriceOption:
    price: int
    label: str       # 키보드 버튼 짧은 라벨
    reason: str      # 메시지 설명


@dataclass
class BuyPriceSuggestion:
    aggressive: PriceOption                  # 항상 있음
    passive: PriceOption                     # 항상 있음
    signals: list[PriceOption] = field(default_factory=list)  # ohlcv 있을 때 추가


@dataclass
class SellPriceSuggestion:
    aggressive: PriceOption
    passive: PriceOption


class TraderExpert:
    """매매 가격 권고. 단순 ±α + 5년치 OHLCV 시그널."""

    @staticmethod
    def suggest_buy_price(
        current_price: int,
        ohlcv: list[OHLCVRow] | None = None,
    ) -> BuyPriceSuggestion:
        if current_price <= 0:
            zero = PriceOption(0, "?", "현재가 미상")
            return BuyPriceSuggestion(aggressive=zero, passive=zero)

        tick = _tick_size(current_price)
        agg = align_to_tick(current_price + tick, "up")
        pas = align_to_tick(int(current_price * 0.99), "down")

        sug = BuyPriceSuggestion(
            aggressive=PriceOption(agg, "🎯 즉시체결",
                                    f"현재가+1틱({tick:,}) — 즉시 체결 우선"),
            passive=PriceOption(pas, "🐢 더 싸게",
                                 "현재가 -1% — 미체결 위험"),
        )

        if not ohlcv or len(ohlcv) < 5:
            return sug

        # 시그널 1: 최근 5일 저가 (단기 지지선)
        recent_5 = ohlcv[-5:]
        low_5d = min(r.low for r in recent_5)
        # 현재가의 ±5% 안일 때만 의미 있는 시그널 (너무 멀면 비활성)
        if abs(low_5d - current_price) / current_price <= 0.05:
            aligned = align_to_tick(low_5d, "down")
            sug.signals.append(PriceOption(
                aligned, "📉 5일저가",
                f"최근 5일 저가 — 단기 지지선",
            ))

        # 시그널 2: 20일 이동평균 (중기 추세선)
        if len(ohlcv) >= 20:
            recent_20 = ohlcv[-20:]
            ma_20 = int(sum(r.close for r in recent_20) / 20)
            if abs(ma_20 - current_price) / current_price <= 0.05:
                aligned = align_to_tick(ma_20, "down")
                sug.signals.append(PriceOption(
                    aligned, "📊 20일MA",
                    f"20일 이동평균 — 중기 추세선",
                ))

        # 시그널 3: 볼린저 밴드 하단 (mean - 2*std)
        if len(ohlcv) >= 20:
            closes = [r.close for r in ohlcv[-20:]]
            mean = sum(closes) / 20
            var = sum((c - mean) ** 2 for c in closes) / 20
            std = math.sqrt(var)
            bb_lower = int(mean - 2 * std)
            if bb_lower > 0 and abs(bb_lower - current_price) / current_price <= 0.07:
                aligned = align_to_tick(bb_lower, "down")
                # 5일저가와 비슷하면 중복 제외 (호가단위 차이 1-2틱은 같은 신호로 봄)
                if not sug.signals or abs(aligned - sug.signals[0].price) > tick * 3:
                    sug.signals.append(PriceOption(
                        aligned, "📏 볼린저하",
                        f"20일 볼린저 밴드 하단 — 통계적 저점",
                    ))

        # 가격 오름차순 정렬 (싼 것부터 표시 — 사용자가 비교하기 쉬움)
        sug.signals.sort(key=lambda p: p.price)
        return sug

    @staticmethod
    def suggest_sell_price(current_price: int) -> SellPriceSuggestion:
        if current_price <= 0:
            zero = PriceOption(0, "?", "현재가 미상")
            return SellPriceSuggestion(aggressive=zero, passive=zero)
        tick = _tick_size(current_price)
        agg = align_to_tick(current_price - tick, "down")
        pas = align_to_tick(int(current_price * 1.01), "up")
        return SellPriceSuggestion(
            aggressive=PriceOption(agg, "🎯 즉시체결",
                                    f"현재가-1틱({tick:,}) — 즉시 체결"),
            passive=PriceOption(pas, "🐢 더 비싸게",
                                 "현재가 +1% — 미체결 위험"),
        )
