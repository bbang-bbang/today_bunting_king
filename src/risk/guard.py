"""RiskGuard — 모든 주문이 통과해야 하는 유일 안전 통로.

불변조건 (HARD RULES, 절대 위반 금지):
  1. 시드머니 100만원 절대 상한 (SEED_CAP_KRW, 불변)
     - 환경변수로 조정되는 활성 시드(active_seed_krw)는 절대 SEED_CAP_KRW 초과 금지
     - 실전 테스트·단계적 승격 시 활성 시드를 낮추는 건 허용 (예: 10만원 소액 테스트)
  2. 국내 주식: 주간 스윙 (월 매수 → 금 청산, 주중 TP 도달 시 즉시 매도)
     크립토: 당일매매 (매일 진입 → 매일 청산)
  3. 번트/스퀴즈 리스크 파라미터 분리 (번트 < 스퀴즈)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# ---- 절대 불변 (코드 상수. 환경변수로도 넘을 수 없음) ----
SEED_CAP_KRW = 1_000_000                   # 시드 절대 상한 (실전, 불변)
SEED_CAP_MOCK_KRW = 10_000_000             # 모의투자 한정 상한 (1,000만원)

# ---- 비율 파라미터 ----
PER_POSITION_CAP_PCT = 50                  # 종목당 활성시드 대비 최대 비중 (%)
DAILY_LOSS_CUT_PCT = 3                     # 일일 손실 컷 (활성시드 대비 %)
LOSING_STREAK_CUT_DAYS = 3                 # 연속 손실 컷 (일)
PIN_REQUIRED_PCT = 30                      # PIN 2차 확인 임계치 (활성시드 대비 %)

MARKET_OPEN_HHMM = (9, 0)
MARKET_CLOSE_HHMM = (15, 30)
FORCE_LIQUIDATE_HHMM = (15, 20)            # 이 시각 이후 신규 매수 금지


class StrategyMode(str, Enum):
    BUNT = "bunt"
    SQUEEZE = "squeeze"


# 모드별 익절/손절 (단위: %) — 당일매매용
MODE_PARAMS = {
    StrategyMode.BUNT:    {"tp_pct": 3, "sl_pct": 2},   # 번트: +3% / -2%
    StrategyMode.SQUEEZE: {"tp_pct": 5, "sl_pct": 3},   # 스퀴즈: +5% / -3%
}

# 주간 스윙용 익절/손절 (5일 보유 → 더 넓은 밴드)
SWING_MODE_PARAMS = {
    StrategyMode.BUNT:    {"tp_pct": 7, "sl_pct": 4},   # 번트 스윙: +7% / -4%
    StrategyMode.SQUEEZE: {"tp_pct": 12, "sl_pct": 5},  # 스퀴즈 스윙: +12% / -5%
}


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class OrderIntent:
    chat_id: int
    side: Side
    code: str
    quantity: int
    price: int                     # 원 단위. 시장가면 기준가(예: 현재가) 투입.
    strategy_mode: StrategyMode
    pin_provided: bool = False


@dataclass
class GuardContext:
    """주문 시점의 계좌 상태 스냅샷. 호출자가 DB에서 조회해서 채운다."""
    cash_balance: int              # 현재 가용 현금 (원)
    position_value: int            # 보유 종목 평가액 합 (원)
    open_position_codes: set[str]  # 현재 보유 중인 종목 코드들
    daily_realized_pnl: int        # 오늘 실현 손익 (원, 손실이면 음수)
    losing_days_streak: int        # 연속 손실일 수


@dataclass
class GuardResult:
    approved: bool
    reason: str = ""               # 기각 사유 (사용자에게 노출 가능)
    requires_pin: bool = False     # True 면 클라이언트에서 PIN 요청


class RiskGuard:
    """모든 매수/매도 주문은 반드시 check() 를 통과해야 한다.

    active_seed_krw: 현재 세션의 활성 시드. 환경변수 SEED_KRW 로 주입.
                    항상 0 < active_seed_krw <= SEED_CAP_KRW 보장.
                    소액 실전 테스트(예: 10만원) 시 이 값만 낮추면 모든
                    비율 체크가 자동으로 그 금액 기준으로 조정된다.
    """

    def __init__(self, active_seed_krw: int, is_mock: bool = False) -> None:
        if active_seed_krw <= 0:
            raise ValueError(f"active_seed_krw must be > 0, got {active_seed_krw}")
        cap = SEED_CAP_MOCK_KRW if is_mock else SEED_CAP_KRW
        if active_seed_krw > cap:
            raise ValueError(
                f"active_seed_krw({active_seed_krw:,}) > 상한({cap:,}). "
                f"{'모의투자' if is_mock else '실전'} 모드 시드 상한 초과."
            )
        self.active_seed_krw = active_seed_krw
        self.is_mock = is_mock

    def check(
        self,
        intent: OrderIntent,
        ctx: GuardContext,
        now: datetime | None = None,
    ) -> GuardResult:
        now = (now or datetime.now(KST)).astimezone(KST)
        hm = (now.hour, now.minute)

        # 1. 정규장 시간 체크 (mock 모드는 24시간 허용)
        if not self.is_mock:
            if hm < MARKET_OPEN_HHMM or hm >= MARKET_CLOSE_HHMM:
                return GuardResult(False, "정규장 시간 외 주문 불가 (09:00~15:30)")

        # 2. 일일 손실 컷 / 연속 손실 컷 (mock 모드는 면제)
        if not self.is_mock:
            daily_cut = -(self.active_seed_krw * DAILY_LOSS_CUT_PCT // 100)
            if ctx.daily_realized_pnl <= daily_cut:
                return GuardResult(False, f"일일 손실 컷 -{DAILY_LOSS_CUT_PCT}% 도달, 오늘 거래 중지")
            if ctx.losing_days_streak >= LOSING_STREAK_CUT_DAYS:
                return GuardResult(False, f"{LOSING_STREAK_CUT_DAYS}일 연속 손실, 오늘 거래 중지")

        if intent.side == Side.BUY:
            # 3. 강제 청산 시각 이후 매수 금지 (mock 모드는 24시간 허용)
            if not self.is_mock and hm >= FORCE_LIQUIDATE_HHMM:
                return GuardResult(False, "강제청산 시각(15:20) 이후로 매수 불가")
            return self._check_buy(intent, ctx)

        return self._check_sell(intent, ctx)

    def _check_buy(self, intent: OrderIntent, ctx: GuardContext) -> GuardResult:
        # 같은 종목 당일 중복 매수 금지 (번트 원칙) — mock 모드는 면제
        if not self.is_mock and intent.code in ctx.open_position_codes:
            return GuardResult(False, "같은 종목은 당일 1회만 매수 (번트 원칙)")

        order_value = intent.price * intent.quantity

        # 활성 시드 상한
        if ctx.position_value + order_value > self.active_seed_krw:
            return GuardResult(
                False,
                f"활성 시드 상한({self.active_seed_krw:,}원) 초과",
            )

        # 절대 상한 (이중 방어 — 환경변수 조작 등 대비)
        abs_cap = SEED_CAP_MOCK_KRW if self.is_mock else SEED_CAP_KRW
        if ctx.position_value + order_value > abs_cap:
            return GuardResult(False, f"절대 시드 상한({abs_cap:,}원) 초과")

        # 가용 현금 체크
        if order_value > ctx.cash_balance:
            return GuardResult(
                False,
                f"주문 대금({order_value:,}원)이 가용 현금({ctx.cash_balance:,}원) 초과",
            )

        # 종목당 비중 상한 (활성 시드 기준) — mock 모드는 면제
        if not self.is_mock:
            per_cap = self.active_seed_krw * PER_POSITION_CAP_PCT // 100
            if order_value > per_cap:
                return GuardResult(False, f"종목당 한도({per_cap:,}원) 초과")

        # PIN 2차 확인 (활성 시드 기준) — mock/paper 모드는 면제
        if not self.is_mock:
            pin_threshold = self.active_seed_krw * PIN_REQUIRED_PCT // 100
            if order_value > pin_threshold and not intent.pin_provided:
                return GuardResult(
                    False,
                    f"PIN 인증 필요 (활성 시드 {PIN_REQUIRED_PCT}% 초과 주문)",
                    requires_pin=True,
                )

        return GuardResult(True)

    def _check_sell(self, intent: OrderIntent, ctx: GuardContext) -> GuardResult:
        if intent.code not in ctx.open_position_codes:
            return GuardResult(False, "보유하지 않은 종목 매도 불가")
        return GuardResult(True)

    @staticmethod
    def compute_target_stop(
        entry_price: int,
        mode: StrategyMode,
        holding_mode: str = "day",
    ) -> tuple[int, int]:
        """진입가 기준 익절가·손절가 계산.

        holding_mode="swing_week" 일 때 SWING_MODE_PARAMS(주간 스윙)를 사용하고,
        그 외에는 MODE_PARAMS(당일매매)를 사용한다.

        결과는 한국증시 호가단위(tick size)로 정렬 — KIS 가 비호가 가격을 거부함(40030000).
        매도(TP) 는 floor (보수적: 약간 낮춰 빠른 체결), 손절(SL) 은 floor (보수적: 약간 낮춰 보호 강화).
        """
        params = SWING_MODE_PARAMS[mode] if holding_mode == "swing_week" else MODE_PARAMS[mode]
        tp = entry_price * (100 + params["tp_pct"]) // 100
        sl = entry_price * (100 - params["sl_pct"]) // 100
        return align_to_tick(tp, "down"), align_to_tick(sl, "down")


# ============================================================
# 호가단위 (tick size) 정렬
# 한국증시 (2023-01-25 개정 기준).
# KIS 는 비호가 가격을 40030000 으로 거부하므로 모든 limit 주문 가격을 정렬해야 함.
# ============================================================
def _tick_size(price: int) -> int:
    if price < 1_000: return 1
    if price < 5_000: return 5
    if price < 10_000: return 10
    if price < 50_000: return 50
    if price < 100_000: return 100
    if price < 500_000: return 500
    return 1_000


def align_to_tick(price: int, direction: str = "nearest") -> int:
    """price 를 한국증시 호가단위로 정렬.
    direction: 'nearest' (반올림) | 'down' (내림) | 'up' (올림)
    """
    if price <= 0:
        return price
    tick = _tick_size(price)
    if direction == "down":
        return (price // tick) * tick
    if direction == "up":
        return ((price + tick - 1) // tick) * tick
    # nearest
    q, r = divmod(price, tick)
    return (q + (1 if r * 2 >= tick else 0)) * tick
