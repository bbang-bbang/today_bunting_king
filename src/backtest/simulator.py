"""백테스트 시뮬레이터 — 번트/스퀴즈 룰 충실 재현.

규칙:
  - 날짜 D 추천 시 enriched[:D-1] 만 사용 (look-ahead bias 방지)
  - 매수가: D일 시가
  - 청산:
      저가 ≤ 손절가  → 손절가에서 체결
      고가 ≥ 익절가  → 익절가에서 체결
      둘 다 아니면    → 종가 청산 (당일매매 원칙)
  - 익절·손절 동시 도달 케이스는 손절 우선 (보수적)
  - 수수료·세금: 매수 0.015%, 매도 0.015% + 거래세 0.2%
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from src.backtest.metrics import compute_metrics
from src.backtest.types import (
    BacktestConfig,
    BacktestResult,
    Trade,
    TradeOutcome,
)
from src.experts.technical import TechnicalExpert
from src.indicators.compute import compute_all
from src.indicators.loader import load_ohlcv
from src.risk.guard import MODE_PARAMS, PER_POSITION_CAP_PCT, SWING_MODE_PARAMS, StrategyMode

log = logging.getLogger("bunting.backtest")


class DbExpertAdapter:
    """DB 기반 전문가(fundamental, flow, news 등)를 Backtester에 맞게 래핑.

    Backtester는 expert.evaluate(code, enriched) 를 호출하는데,
    DB 전문가들은 evaluate(code, as_of) 시그니처를 쓴다.
    enriched 의 마지막 날짜를 as_of 로 변환해서 전달한다.

    ignore_as_of=True 이면 as_of=None 으로 최신 스냅샷 사용 (백테스트에서
    스냅샷이 1개뿐일 때 유용).
    """

    def __init__(self, expert, ignore_as_of: bool = False) -> None:
        self.expert = expert
        self.name = getattr(expert, "name", "adapted")
        self.ignore_as_of = ignore_as_of

    def evaluate(self, code, enriched, **_kw):
        if self.ignore_as_of:
            return self.expert.evaluate(code, as_of=None)
        as_of = None
        if enriched is not None and not enriched.empty:
            try:
                as_of = enriched.index[-1].date()
            except Exception:
                pass
        return self.expert.evaluate(code, as_of=as_of)


class TechFundComboExpert:
    """기술 + 재무 2-전문가 조합. 가중 평균 (tech 0.65, fund 0.35)."""

    name = "tech+fund"

    def __init__(self, tech_w: float = 0.65, fund_w: float = 0.35) -> None:
        from src.experts.fundamental import FundamentalExpert
        self.technical = TechnicalExpert()
        self.fundamental = FundamentalExpert()
        self.tech_w = tech_w
        self.fund_w = fund_w

    def evaluate(self, code, enriched, **_kw):
        from src.experts.base import ExpertOpinion
        tech_op = self.technical.evaluate(code, enriched)
        if not tech_op.is_valid:
            return tech_op

        # 스냅샷이 1개뿐인 환경에서도 동작하도록 as_of=None (최신 스냅샷 사용)
        fund_op = self.fundamental.evaluate(code, as_of=None)

        if fund_op.is_valid:
            score = tech_op.score * self.tech_w + fund_op.score * self.fund_w
        else:
            score = tech_op.score  # 재무 데이터 없으면 기술만

        return ExpertOpinion(
            code=code,
            expert=self.name,
            score=min(100.0, score),
            signals=tech_op.signals,
            mode_fit=tech_op.mode_fit,
            reason_summary=f"기술 {tech_op.score:.0f} · 재무 {fund_op.score:.0f} → 조합 {score:.0f}",
        )


class Backtester:
    def __init__(self, expert=None) -> None:
        self.expert = expert or TechnicalExpert()

    # ----------------------------------------------------------
    # 공통: 데이터 준비 & 후보 선정
    # ----------------------------------------------------------

    def _prepare_data(self, codes: list[str]) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
        data: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
        for code in codes:
            df = load_ohlcv(code)
            if df.empty or len(df) < 60:
                continue
            data[code] = (df, compute_all(df))
        return data

    def _evaluate_candidates(
        self, data, d_ts, per_cap, min_score,
    ) -> list[tuple[str, object, float, pd.Series]]:
        candidates = []
        for code, (ohlcv, enriched) in data.items():
            sub = enriched[enriched.index < d_ts]
            if len(sub) < 60:
                continue
            opinion = self.expert.evaluate(code, sub)
            op_score = getattr(opinion, "ensemble_score", None)
            if op_score is None:
                op_score = opinion.score
            op_valid = not getattr(opinion, "filtered", False)
            if hasattr(opinion, "is_valid"):
                op_valid = op_valid and opinion.is_valid
            if not op_valid or op_score < min_score:
                continue
            d_rows = ohlcv[ohlcv.index == d_ts]
            if d_rows.empty:
                continue
            last_close_prev = int(sub["close"].iloc[-1])
            if last_close_prev <= 0 or last_close_prev > per_cap:
                continue
            candidates.append((code, opinion, op_score, d_rows.iloc[0]))
        candidates.sort(key=lambda x: x[2], reverse=True)
        return candidates

    @staticmethod
    def _calc_pnl(entry_price, exit_price, qty, config):
        order_value = entry_price * qty
        gross_pnl = (exit_price - entry_price) * qty
        commission = (entry_price + exit_price) * qty * config.commission_bps // 100_000
        tax = exit_price * qty * config.sell_tax_bps // 100_000
        net_pnl = gross_pnl - commission - tax
        return_pct = net_pnl / order_value * 100 if order_value else 0.0
        return gross_pnl, net_pnl, return_pct

    # ----------------------------------------------------------
    # 메인 진입점
    # ----------------------------------------------------------

    def run(self, config: BacktestConfig, codes: list[str]) -> BacktestResult:
        if config.holding_mode == "swing_week":
            return self._run_swing(config, codes)
        return self._run_day(config, codes)

    # ----------------------------------------------------------
    # 당일매매 시뮬레이션 (기존)
    # ----------------------------------------------------------

    def _run_day(self, config: BacktestConfig, codes: list[str]) -> BacktestResult:
        mode = StrategyMode(config.strategy_mode)
        tp_pct = MODE_PARAMS[mode]["tp_pct"]
        sl_pct = MODE_PARAMS[mode]["sl_pct"]
        per_cap = config.active_seed_krw * PER_POSITION_CAP_PCT // 100

        data = self._prepare_data(codes)
        if not data:
            log.warning("백테스트 대상 종목 데이터 없음")
            return BacktestResult(config=config)

        all_dates = sorted({d for df, _ in data.values() for d in df.index.date})
        date_range = [d for d in all_dates if config.start <= d <= config.end]

        cash = config.active_seed_krw
        trades: list[Trade] = []
        daily_equity: list[tuple[date, int]] = []

        for D in date_range:
            d_ts = pd.Timestamp(D)
            candidates = self._evaluate_candidates(data, d_ts, per_cap, config.min_score)
            selected = candidates[: config.max_holdings]

            for code, opinion, op_score, d_bar in selected:
                entry_price = int(d_bar["open"])
                qty = per_cap // entry_price
                if qty < 1:
                    continue
                if entry_price * qty > cash:
                    continue

                tp_price = entry_price * (100 + tp_pct) // 100
                sl_price = entry_price * (100 - sl_pct) // 100
                high, low, close = int(d_bar["high"]), int(d_bar["low"]), int(d_bar["close"])

                if low <= sl_price:
                    exit_price, outcome = sl_price, TradeOutcome.STOP_LOSS
                elif high >= tp_price:
                    exit_price, outcome = tp_price, TradeOutcome.TAKE_PROFIT
                else:
                    exit_price, outcome = close, TradeOutcome.TIME_CLOSE

                gross_pnl, net_pnl, return_pct = self._calc_pnl(entry_price, exit_price, qty, config)
                trades.append(Trade(
                    code=code, entry_date=D, entry_price=entry_price, exit_price=exit_price,
                    quantity=qty, strategy_mode=config.strategy_mode, outcome=outcome,
                    gross_pnl=gross_pnl, net_pnl=net_pnl, return_pct=return_pct,
                    expert_score=op_score, expert_reason=(opinion.reason_summary or "")[:120],
                    exit_date=D,
                ))
                cash += net_pnl

            daily_equity.append((D, cash))

        metrics = compute_metrics(trades, daily_equity, config.active_seed_krw)
        return BacktestResult(config=config, trades=trades, daily_equity=daily_equity, **metrics)

    # ----------------------------------------------------------
    # 주간 스윙 시뮬레이션 (월 매수 → 주중 TP/SL → 금 종가 강제 청산)
    # ----------------------------------------------------------

    def _run_swing(self, config: BacktestConfig, codes: list[str]) -> BacktestResult:
        mode = StrategyMode(config.strategy_mode)
        tp_pct = SWING_MODE_PARAMS[mode]["tp_pct"]
        sl_pct = SWING_MODE_PARAMS[mode]["sl_pct"]
        # early_take_profit ON 이면 day-TP 도 함께 평가 (더 빠른 익절 시뮬)
        early_tp_pct = MODE_PARAMS[mode]["tp_pct"] if config.early_take_profit else None
        per_cap = config.active_seed_krw * PER_POSITION_CAP_PCT // 100

        data = self._prepare_data(codes)
        if not data:
            log.warning("백테스트 대상 종목 데이터 없음")
            return BacktestResult(config=config)

        all_dates = sorted({d for df, _ in data.values() for d in df.index.date})
        date_range = [d for d in all_dates if config.start <= d <= config.end]

        # 주 단위 그룹핑 (ISO week)
        weeks: dict[tuple[int, int], list[date]] = {}
        for d in date_range:
            key = (d.isocalendar()[0], d.isocalendar()[1])
            weeks.setdefault(key, []).append(d)

        cash = config.active_seed_krw
        trades: list[Trade] = []
        daily_equity: list[tuple[date, int]] = []

        for _week_key, week_dates in sorted(weeks.items()):
            if len(week_dates) < 2:
                # 거래일 1일뿐인 주(공휴일 등)는 스킵
                for d in week_dates:
                    daily_equity.append((d, cash))
                continue

            entry_day = week_dates[0]   # 월요일 (또는 해당 주 첫 거래일)
            d_ts = pd.Timestamp(entry_day)

            candidates = self._evaluate_candidates(data, d_ts, per_cap, config.min_score)
            selected = candidates[: config.max_holdings]

            # 이번 주 포지션 없으면 equity만 기록
            if not selected:
                for d in week_dates:
                    daily_equity.append((d, cash))
                continue

            for code, opinion, op_score, entry_bar in selected:
                entry_price = int(entry_bar["open"])
                qty = per_cap // entry_price
                if qty < 1:
                    continue
                if entry_price * qty > cash:
                    continue

                tp_price = entry_price * (100 + tp_pct) // 100
                sl_price = entry_price * (100 - sl_pct) // 100
                # early-TP 활성 시 day-TP 가격 (swing TP 보다 낮음 → 먼저 도달)
                early_tp_price = (
                    entry_price * (100 + early_tp_pct) // 100
                    if early_tp_pct is not None else None
                )

                ohlcv = data[code][0]
                exit_price = None
                exit_day = None
                outcome = None

                # 매수일부터 주말 전까지 매일 체크
                for i, check_day in enumerate(week_dates):
                    check_ts = pd.Timestamp(check_day)
                    check_rows = ohlcv[ohlcv.index == check_ts]
                    if check_rows.empty:
                        continue

                    bar = check_rows.iloc[0]
                    high, low, close = int(bar["high"]), int(bar["low"]), int(bar["close"])
                    is_last_day = (i == len(week_dates) - 1)

                    # 매수일은 시가 이후만 체크 (시가=진입가이므로 고/저/종으로 판단)
                    if low <= sl_price:
                        exit_price, outcome, exit_day = sl_price, TradeOutcome.STOP_LOSS, check_day
                        break
                    elif early_tp_price is not None and high >= early_tp_price:
                        # day-TP 우선: swing TP 까지 안 가도 먼저 익절
                        exit_price, outcome, exit_day = early_tp_price, TradeOutcome.TAKE_PROFIT, check_day
                        break
                    elif high >= tp_price:
                        exit_price, outcome, exit_day = tp_price, TradeOutcome.TAKE_PROFIT, check_day
                        break
                    elif is_last_day:
                        exit_price, outcome, exit_day = close, TradeOutcome.WEEK_CLOSE, check_day
                        break

                if exit_price is None:
                    # 데이터 없어서 청산 못 함 → 마지막 가용 종가로 강제 청산
                    exit_price = entry_price
                    outcome = TradeOutcome.WEEK_CLOSE
                    exit_day = entry_day

                gross_pnl, net_pnl, return_pct = self._calc_pnl(entry_price, exit_price, qty, config)
                trades.append(Trade(
                    code=code, entry_date=entry_day, entry_price=entry_price,
                    exit_price=exit_price, quantity=qty, strategy_mode=config.strategy_mode,
                    outcome=outcome, gross_pnl=gross_pnl, net_pnl=net_pnl,
                    return_pct=return_pct, expert_score=op_score,
                    expert_reason=(opinion.reason_summary or "")[:120],
                    exit_date=exit_day,
                ))
                cash += net_pnl

            # 주간 equity: 각 거래일별 기록
            for d in week_dates:
                daily_equity.append((d, cash))

        metrics = compute_metrics(trades, daily_equity, config.active_seed_krw)
        return BacktestResult(config=config, trades=trades, daily_equity=daily_equity, **metrics)
