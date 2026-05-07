"""Env 로더 및 런타임 설정."""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class TradeMode(str, Enum):
    PAPER = "paper"
    KIS_MOCK = "kis_mock"
    LIVE = "live"


def _env(key: str, default: str = "", required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"환경변수 {key} 가 비어있음")
    return val


TRADE_MODE = TradeMode(_env("TRADE_MODE", "paper"))

# 활성 시드. 절대 상한(코드 상수 1,000,000) 초과 시 기동 거부.
SEED_KRW = int(_env("SEED_KRW", "1000000"))
_SEED_CAP_ABS = 1_000_000
_SEED_CAP_MOCK = 10_000_000  # 모의투자 한정 상한 (1,000만원)

_effective_cap = _SEED_CAP_MOCK if TRADE_MODE == TradeMode.KIS_MOCK else _SEED_CAP_ABS
if SEED_KRW <= 0 or SEED_KRW > _effective_cap:
    raise RuntimeError(
        f"SEED_KRW 는 0보다 크고 {_effective_cap:,} 이하여야 합니다. 현재값: {SEED_KRW:,}"
    )

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_INVITE_CODE = _env("TELEGRAM_INVITE_CODE")
TELEGRAM_ADMIN_CHAT_ID = int(_env("TELEGRAM_ADMIN_CHAT_ID", "0"))

KIS_MOCK_APP_KEY = _env("KIS_MOCK_APP_KEY")
KIS_MOCK_APP_SECRET = _env("KIS_MOCK_APP_SECRET")
KIS_MOCK_ACCOUNT_NO = _env("KIS_MOCK_ACCOUNT_NO")

KIS_LIVE_APP_KEY = _env("KIS_LIVE_APP_KEY")
KIS_LIVE_APP_SECRET = _env("KIS_LIVE_APP_SECRET")
KIS_LIVE_ACCOUNT_NO = _env("KIS_LIVE_ACCOUNT_NO")

DB_PATH = Path(_env("DB_PATH", "./data/bunting.db"))
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
LOG_PATH = Path(_env("LOG_PATH", "./logs/bunting.log"))

# 자동 추천 (월 08:30 morning_kr_recommend) on/off. false 면 스케줄러가 잡 자체를 등록 안 함.
# 사용자는 텔레그램에서 /추천 으로 수동 호출은 그대로 가능.
AUTO_RECOMMEND_ENABLED = _env("AUTO_RECOMMEND_ENABLED", "true").lower() in ("1", "true", "yes")

# 잔고 정기 푸시 — 장중(09:10~15:30) 매 N분. 0 이면 비활성.
BALANCE_PERIODIC_MINUTES = int(_env("BALANCE_PERIODIC_MINUTES", "0"))

# 추천 최소 앙상블 점수 (0~100). 기본 60 — 5/7 임계 컷으로 picks 부족 (62 시 4건) → 60 으로 완화.
RECOMMEND_MIN_SCORE = float(_env("RECOMMEND_MIN_SCORE", "60"))

# 전일 코스피 등락률 하한선 (%). 이 값 미만이면 당일 추천 보류.
# 0 으로 설정 시 필터 비활성화.
MARKET_DOWN_THRESHOLD_PCT = float(_env("MARKET_DOWN_THRESHOLD_PCT", "-1.5"))


def require_live_keys() -> None:
    """live 모드 진입 시 호출 — 키 누락이면 예외."""
    missing = [k for k, v in {
        "KIS_LIVE_APP_KEY": KIS_LIVE_APP_KEY,
        "KIS_LIVE_APP_SECRET": KIS_LIVE_APP_SECRET,
        "KIS_LIVE_ACCOUNT_NO": KIS_LIVE_ACCOUNT_NO,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"live 모드에는 다음 환경변수가 필요: {missing}")
