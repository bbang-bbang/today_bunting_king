"""/strategy 통합 조회 — 전략/보유/조기익절 조합 별 effective TP/SL.

3 regime: swing 기본 / swing+early / day. 사용자가 헷갈리지 않게 명시.
"""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_bunting.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SEED_KRW", "1000000")
    monkeypatch.setenv("TRADE_MODE", "paper")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TELEGRAM_INVITE_CODE", "dummy")
    import src.config
    importlib.reload(src.config)
    import src.db.connection
    importlib.reload(src.db.connection)
    from src.db.connection import init_schema
    init_schema()
    yield


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


@dataclass
class FakeChat:
    id: int


@dataclass
class FakeUpdate:
    effective_chat: FakeChat
    message: FakeMessage = field(default_factory=FakeMessage)

    @property
    def effective_user(self):
        return self.effective_chat


@dataclass
class FakeCtx:
    args: list = field(default_factory=list)
    user_data: dict = field(default_factory=dict)


def _mk(chat_id):
    return FakeUpdate(effective_chat=FakeChat(id=chat_id))


# ============================================================
# _effective_tp_sl 단위 테스트
# ============================================================

def test_effective_tp_sl_swing_default():
    from src.bot.telegram_bot import _effective_tp_sl
    tp, sl, regime = _effective_tp_sl("bunt", "swing_week", False)
    assert tp == 7 and sl == 4
    assert "스윙 기본" in regime


def test_effective_tp_sl_swing_with_early():
    from src.bot.telegram_bot import _effective_tp_sl
    tp, sl, regime = _effective_tp_sl("bunt", "swing_week", True)
    assert tp == 3   # day TP (좁음)
    assert sl == 4   # swing SL (넓음)
    assert "조기익절" in regime


def test_effective_tp_sl_day_mode():
    from src.bot.telegram_bot import _effective_tp_sl
    tp, sl, regime = _effective_tp_sl("squeeze", "day", False)
    assert tp == 5 and sl == 3
    assert "당일" in regime


def test_effective_tp_sl_day_with_early_no_change():
    """day 모드면 early 효과 없음 — 같은 TP/SL."""
    from src.bot.telegram_bot import _effective_tp_sl
    tp1, sl1, _ = _effective_tp_sl("bunt", "day", False)
    tp2, sl2, _ = _effective_tp_sl("bunt", "day", True)
    assert (tp1, sl1) == (tp2, sl2)


# ============================================================
# /strategy 명령
# ============================================================

def test_strategy_cmd_shows_three_regimes_and_current():
    from src.services import user_service
    from src.bot.telegram_bot import cmd_strategy

    user_service.register_user(100)   # default: bunt / swing / early=OFF
    upd = _mk(100)
    asyncio.run(cmd_strategy(upd, FakeCtx()))

    text = upd.message.replies[0]
    assert "내 전략 설정" in text
    assert "bunt" in text
    assert "주간 스윙" in text
    assert "OFF" in text
    # 3 regime 비교 표
    assert "스윙 기본" in text
    assert "스윙 + /early on" in text
    assert "/holding day" in text
    # 현재 effective
    assert "+7%" in text   # swing default bunt
    assert "-4%" in text


def test_strategy_cmd_shows_early_on_regime():
    from src.services import user_service
    from src.bot.telegram_bot import cmd_strategy

    user_service.register_user(200)
    user_service.update_early_take_profit(200, True)

    upd = _mk(200)
    asyncio.run(cmd_strategy(upd, FakeCtx()))
    text = upd.message.replies[0]
    assert "ON" in text
    assert "+3%" in text   # early TP
    assert "-4%" in text   # swing SL


def test_strategy_cmd_shows_day_regime():
    from src.services import user_service
    from src.bot.telegram_bot import cmd_strategy

    user_service.register_user(300)
    user_service.update_holding_mode(300, "day")
    user_service.update_strategy_mode(300, "squeeze")

    upd = _mk(300)
    asyncio.run(cmd_strategy(upd, FakeCtx()))
    text = upd.message.replies[0]
    assert "당일매매" in text
    assert "+5%" in text and "-3%" in text   # squeeze day
