"""코인 백테스트 시뮬레이터.

KR 봇의 src/backtest/simulator.py 패턴 포팅. 차이:
  - 24/7 시장 — "거래일" 개념 X. 모든 분봉 처리.
  - narrow TP/SL — 기본 +1.5% / -1.0% (그리드 서치 가능).
  - 수수료: 업비트 0.05% × 매수+매도 = 0.1% (한국 주식보다 낮음).
  - 슬리피지: 0.05% (지정가) ~ 0.1% (시장가) — 가산 비용.
  - 종목 단위: KRW-BTC, KRW-ETH 등 market 코드.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import pandas as pd

log = logging.getLogger("bunting.coin.backtest")


# ============================================================
# Config / Result
# ============================================================

@dataclass
class CoinBacktestConfig:
    market: str                       # "KRW-BTC"
    start: pd.Timestamp               # UTC
    end: pd.Timestamp                 # UTC
    seed_krw: int = 300_000           # 30만원 파일럿
    tp_pct: float = 1.5               # +1.5% 익절
    sl_pct: float = 1.0               # -1.0% 손절
    commission_pct: float = 0.05      # 한 방향 (업비트)
    slippage_pct: float = 0.05        # 지정가 가정
    cooldown_min: int = 0             # 청산 후 N분 재진입 금지 (whipsaw 방어)
    max_holdings: int = 1             # 동시 보유 N종목 (현재 1)


@dataclass
class CoinTrade:
    market: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: float
    outcome: str                       # "tp" | "sl" | "end"
    gross_pnl: float                   # KRW
    net_pnl: float                     # KRW (수수료·슬리피지 차감)
    return_pct: float                  # net 기준


@dataclass
class CoinBacktestResult:
    config: CoinBacktestConfig
    trades: list[CoinTrade] = field(default_factory=list)
    final_equity: float = 0.0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl <= 0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_trades if self.n_trades else 0.0

    @property
    def total_return_pct(self) -> float:
        if self.config.seed_krw <= 0:
            return 0.0
        return (self.final_equity - self.config.seed_krw) / self.config.seed_krw * 100

    @property
    def avg_return_pct(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.return_pct for t in self.trades) / len(self.trades)

    def summary(self) -> str:
        c = self.config
        return (
            f"[코인백테] {c.market} {c.start.date()} ~ {c.end.date()}  "
            f"TP+{c.tp_pct}% SL-{c.sl_pct}%  시드={c.seed_krw:,}\n"
            f"  거래: {self.n_trades}건  (승 {self.wins} / 패 {self.losses}, 승률 {self.win_rate*100:.1f}%)\n"
            f"  평균 수익률: {self.avg_return_pct:+.3f}%\n"
            f"  최종 자산:   {self.final_equity:,.0f}원  (총 {self.total_return_pct:+.2f}%)"
        )


# ============================================================
# 시뮬레이터
# ============================================================

# 시그널 타입: (df_so_far) → "buy" | None
SignalFn = Callable[[pd.DataFrame], Optional[str]]


class CoinBacktester:
    """단일 종목·단일 모드 백테스트.

    매 캔들마다:
      1) 보유 중이면 high >= TP 또는 low <= SL 검사 → 청산
      2) 미보유면 시그널 평가 → buy
    수수료/슬리피지 차감 후 net_pnl 계산.
    """

    def __init__(self, signal_fn: SignalFn) -> None:
        self.signal_fn = signal_fn

    def run(
        self,
        df: pd.DataFrame,
        config: CoinBacktestConfig,
        precomputed_signals: Optional[pd.Series] = None,
    ) -> CoinBacktestResult:
        """백테스트 실행.

        precomputed_signals: 미리 계산된 boolean Series (df 와 같은 인덱스).
            제공되면 self.signal_fn 무시하고 lookup 으로 진행 (O(N) → 빠름).
            None 이면 매 캔들 self.signal_fn(sub) 호출 (O(N²)).
        """
        if df.empty:
            return CoinBacktestResult(config=config, final_equity=config.seed_krw)

        df = df[(df.index >= config.start) & (df.index <= config.end)].copy()
        if df.empty:
            return CoinBacktestResult(config=config, final_equity=config.seed_krw)

        # 시그널 사전 계산 — 같은 df 면 한 번만 계산 후 reuse
        if precomputed_signals is None and self.signal_fn is None:
            sig_series = pd.Series([False] * len(df), index=df.index)
        else:
            sig_series = precomputed_signals

        result = CoinBacktestResult(config=config)
        cash = float(config.seed_krw)
        position = None
        cooldown_until: pd.Timestamp | None = None

        # numpy 배열로 변환 (iterrows 보다 빠름)
        opens = df["open"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        index = df.index
        n = len(df)

        for i in range(n):
            ts = index[i]
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]

            # 1) 보유 중 → 청산 검사
            if position is not None:
                if l <= position["sl"]:
                    exit_price = position["sl"] * (1 - config.slippage_pct / 100)
                    self._close_position(result, position, exit_price, ts, "sl", config)
                    cash += position["quantity"] * exit_price * (1 - config.commission_pct / 100)
                    position = None
                    cooldown_until = ts + pd.Timedelta(minutes=config.cooldown_min)
                    continue
                if h >= position["tp"]:
                    exit_price = position["tp"] * (1 - config.slippage_pct / 100)
                    self._close_position(result, position, exit_price, ts, "tp", config)
                    cash += position["quantity"] * exit_price * (1 - config.commission_pct / 100)
                    position = None
                    cooldown_until = ts + pd.Timedelta(minutes=config.cooldown_min)
                    continue
                continue

            # 2) 미보유 + cooldown 외 → 시그널 평가
            if cooldown_until is not None and ts < cooldown_until:
                continue
            if i < 30:
                continue

            # 사전 계산된 시그널 lookup (있으면) 또는 fn 호출 (느림, fallback)
            if sig_series is not None:
                buy = bool(sig_series.iloc[i])
            else:
                sub = df.iloc[: i + 1]
                buy = self.signal_fn(sub) == "buy"
            if not buy:
                continue

            # 매수 — 다음 캔들 시가에 진입
            if i + 1 >= n:
                break
            entry_price = float(opens[i + 1]) * (1 + config.slippage_pct / 100)
            qty = (cash * 0.99) / entry_price
            if qty <= 0:
                continue
            cost = qty * entry_price
            commission = cost * config.commission_pct / 100
            cash -= (cost + commission)

            position = {
                "entry_price": entry_price,
                "quantity": qty,
                "entry_ts": index[i + 1],
                "tp": entry_price * (1 + config.tp_pct / 100),
                "sl": entry_price * (1 - config.sl_pct / 100),
            }

        # 백테스트 종료 시 보유분은 마지막 종가로 청산
        if position is not None:
            exit_price = float(df.iloc[-1]["close"])
            self._close_position(result, position, exit_price, df.index[-1], "end", config)
            cash += position["quantity"] * exit_price * (1 - config.commission_pct / 100)
            position = None

        result.final_equity = cash
        return result

    @staticmethod
    def _close_position(
        result: CoinBacktestResult, position: dict,
        exit_price: float, exit_ts: pd.Timestamp, outcome: str,
        config: CoinBacktestConfig,
    ) -> None:
        qty = position["quantity"]
        entry = position["entry_price"]
        gross = (exit_price - entry) * qty
        # 매수·매도 수수료 합산
        commission_total = (entry + exit_price) * qty * config.commission_pct / 100
        net = gross - commission_total
        return_pct = net / (entry * qty) * 100 if entry * qty > 0 else 0.0
        result.trades.append(CoinTrade(
            market=config.market,
            entry_ts=position["entry_ts"], exit_ts=exit_ts,
            entry_price=entry, exit_price=exit_price,
            quantity=qty, outcome=outcome,
            gross_pnl=gross, net_pnl=net, return_pct=return_pct,
        ))


# ============================================================
# 그리드 서치 — TP/SL 조합별 결과 비교
# ============================================================

def grid_search(
    df: pd.DataFrame,
    signal_fn: SignalFn,
    market: str,
    tp_grid: list[float],
    sl_grid: list[float],
    seed_krw: int = 300_000,
    precomputed_signals: Optional[pd.Series] = None,
) -> list[CoinBacktestResult]:
    """TP × SL 조합 모두 백테스트 후 결과 list 반환 (수익률 내림차순).

    precomputed_signals 전달 시 signal_fn 무시하고 lookup 으로 빠르게 N×M 회 실행.
    """
    bt = CoinBacktester(signal_fn=signal_fn)
    out: list[CoinBacktestResult] = []
    for tp in tp_grid:
        for sl in sl_grid:
            cfg = CoinBacktestConfig(
                market=market,
                start=df.index[0],
                end=df.index[-1],
                seed_krw=seed_krw,
                tp_pct=tp, sl_pct=sl,
            )
            r = bt.run(df, cfg, precomputed_signals=precomputed_signals)
            out.append(r)
    out.sort(key=lambda r: r.total_return_pct, reverse=True)
    return out
