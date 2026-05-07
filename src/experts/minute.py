"""분봉 전문가 — 당일 분봉 기반 VWAP · 거래량 급증 · EMA 정배열 · 눌림목 분석.

앙상블 5번째 전문가 (가중치: 번트 10% / 스퀴즈 15%).
분봉 데이터 없으면 fallback (다른 전문가 가중치 재정규화).

점수 배분 (총 100점):
  VWAP 위 안착        20점  (현재가/VWAP 비율 기반)
  거래량 급증          20점  (최근 5봉 vs 일중 평균)
  EMA 정배열           25점  (5분 > 10분 > 20분, 기울기 보너스)
  눌림목 패턴          20점  (고점 후 저거래량 눌림 → 반등)
  모드 적합도          15점
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from src.db.connection import get_connection
from src.experts.base import ExpertOpinion, Signal


@dataclass(frozen=True)
class MinuteBar:
    datetime: datetime
    open: int
    high: int
    low: int
    close: int
    volume: int


class MinuteExpert:
    name = "minute"

    # ----------------------------------------------------------
    # 앙상블 인터페이스
    # ----------------------------------------------------------

    def evaluate(self, code: str, as_of: date | None = None) -> ExpertOpinion:
        if as_of is None:
            as_of = date.today()
        bars = self._load_bars(code, as_of)
        if len(bars) < 10:
            return ExpertOpinion(
                code=code, expert=self.name, score=0,
                error="분봉 데이터 부족 (최소 10봉)",
            )
        return self.evaluate_bars(code, bars)

    # 순수 함수 (테스트용)
    def evaluate_bars(self, code: str, bars: list[MinuteBar]) -> ExpertOpinion:
        signals: list[Signal] = []
        signals += self._score_vwap(bars)
        signals += self._score_volume_surge(bars)
        signals += self._score_ema_alignment(bars)
        signals += self._score_pullback(bars)

        bunt_fit, squeeze_fit = self._compute_mode_fit(bars)
        signals.append(Signal(
            "모드 적합도",
            max(bunt_fit, squeeze_fit) * 15,
            f"번트 {bunt_fit:.2f} / 스퀴즈 {squeeze_fit:.2f}",
        ))

        total = min(100.0, max(0.0, sum(s.score for s in signals)))
        reason = " · ".join(
            f"{s.name}(+{s.score:.0f})" for s in signals if s.score >= 3
        )[:200]

        return ExpertOpinion(
            code=code, expert=self.name, score=total,
            signals=signals,
            mode_fit={"bunt": bunt_fit, "squeeze": squeeze_fit},
            reason_summary=reason,
        )

    # ----------------------------------------------------------
    # 세부 스코어러
    # ----------------------------------------------------------

    def _score_vwap(self, bars: list[MinuteBar]) -> list[Signal]:
        total_vol = sum(b.volume for b in bars)
        if total_vol == 0:
            return []
        vwap = sum(b.close * b.volume for b in bars) / total_vol
        if vwap <= 0:
            return []
        ratio = bars[-1].close / vwap
        if ratio >= 1.02:
            score = 20
        elif ratio >= 1.01:
            score = 14
        elif ratio >= 1.0:
            score = 8
        else:
            score = 0
        return [Signal("VWAP 위 안착", score, f"현재가/VWAP={ratio:.3f}")]

    def _score_volume_surge(self, bars: list[MinuteBar]) -> list[Signal]:
        if len(bars) < 10:
            return []
        avg_vol = sum(b.volume for b in bars) / len(bars)
        if avg_vol <= 0:
            return []
        recent_avg = sum(b.volume for b in bars[-5:]) / 5
        ratio = recent_avg / avg_vol
        if ratio >= 3.0:
            score = 20
        elif ratio >= 2.0:
            score = 14
        elif ratio >= 1.5:
            score = 8
        elif ratio >= 1.2:
            score = 4
        else:
            score = 0
        return [Signal("거래량 급증", score, f"최근5분/{len(bars)}분 평균={ratio:.1f}x")]

    def _score_ema_alignment(self, bars: list[MinuteBar]) -> list[Signal]:
        closes = [b.close for b in bars]
        if len(closes) < 20:
            return []
        ema5  = self._ema(closes, 5)
        ema10 = self._ema(closes, 10)
        ema20 = self._ema(closes, 20)
        score = 0
        if ema5 > ema10:
            score += 10
        if ema10 > ema20:
            score += 10
        if ema5 > ema10 > ema20 and len(closes) > 5:
            prev_ema5 = self._ema(closes[:-5], 5)
            if ema5 > prev_ema5:
                score += 5
        if score == 0:
            return []
        return [Signal("EMA 정배열", score,
                       f"EMA5={ema5:.0f} EMA10={ema10:.0f} EMA20={ema20:.0f}")]

    def _score_pullback(self, bars: list[MinuteBar]) -> list[Signal]:
        """눌림목: 전반 고점 → 후반 저거래량 눌림 → 현재 반등."""
        if len(bars) < 30:
            return []
        window = bars[-30:]
        first, second = window[:15], window[15:]

        peak_price    = max(b.high for b in first)
        pullback_low  = min(b.close for b in second)
        current_close = bars[-1].close

        avg_vol_first  = sum(b.volume for b in first) / 15
        avg_vol_second = sum(b.volume for b in second) / 15

        vol_contracted  = avg_vol_second < avg_vol_first * 0.7
        price_recovered = current_close > pullback_low * 1.005
        near_peak       = current_close > peak_price * 0.97

        if vol_contracted and price_recovered and near_peak:
            return [Signal("눌림목 패턴", 20,
                           f"고점{peak_price:,}→눌림→반등{current_close:,} 거래량수축")]
        if vol_contracted and price_recovered:
            return [Signal("눌림목 패턴", 10,
                           f"눌림 후 반등 (고점대비 {current_close/peak_price*100-100:.1f}%)")]
        return [Signal("눌림목 패턴", 0, "눌림목 미형성")]

    # ----------------------------------------------------------

    def _compute_mode_fit(self, bars: list[MinuteBar]) -> tuple[float, float]:
        bunt, squeeze = 0.5, 0.5
        if len(bars) < 10:
            return bunt, squeeze

        avg_vol    = sum(b.volume for b in bars) / len(bars)
        recent_avg = sum(b.volume for b in bars[-5:]) / 5
        if avg_vol > 0 and recent_avg / avg_vol >= 2.0:
            squeeze += 0.2

        closes = [b.close for b in bars]
        if len(closes) >= 20:
            ema5  = self._ema(closes, 5)
            ema20 = self._ema(closes, 20)
            pct = (ema5 - ema20) / ema20 * 100 if ema20 > 0 else 0
            if pct >= 2:
                squeeze += 0.15
            elif 0 < pct < 1:
                bunt += 0.15

        return max(0.0, min(1.0, bunt)), max(0.0, min(1.0, squeeze))

    # ----------------------------------------------------------
    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        if len(values) < period:
            return float(values[-1]) if values else 0.0
        k = 2.0 / (period + 1)
        result = sum(values[:period]) / period
        for v in values[period:]:
            result = v * k + result * (1 - k)
        return result

    # ----------------------------------------------------------
    def _load_bars(self, code: str, as_of: date) -> list[MinuteBar]:
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT datetime, open, high, low, close, volume
                   FROM ohlcv_minute
                   WHERE code = ? AND datetime >= ? AND datetime <= ?
                   ORDER BY datetime ASC""",
                (code, f"{as_of.isoformat()} 09:00", f"{as_of.isoformat()} 15:30"),
            ).fetchall()
        finally:
            conn.close()

        out: list[MinuteBar] = []
        for r in rows:
            try:
                dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                continue
            out.append(MinuteBar(
                datetime=dt,
                open=r["open"], high=r["high"], low=r["low"],
                close=r["close"], volume=r["volume"],
            ))
        return out
