"""조기 익절 (early_take_profit) 토글 — 회귀 테스트.

2026-05-04 도입: ON 시 스윙 포지션이라도 day-TP 도달하면 즉시 매도.
"""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from datetime import datetime, timedelta

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


# ============================================================
# 마이그레이션
# ============================================================

def test_migration_adds_early_take_profit_column():
    from src.db.connection import get_connection, _run_migrations
    conn = get_connection()
    try:
        _run_migrations(conn)
        _run_migrations(conn)  # idempotent
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_users)").fetchall()}
        assert "early_take_profit" in cols
    finally:
        conn.close()


# ============================================================
# user_service
# ============================================================

def test_default_early_take_profit_is_false():
    from src.services import user_service
    user_service.register_user(100)
    u = user_service.get_user(100)
    assert u.early_take_profit is False


def test_update_early_take_profit_true():
    from src.services import user_service
    user_service.register_user(200)
    assert user_service.update_early_take_profit(200, True)
    u = user_service.get_user(200)
    assert u.early_take_profit is True


def test_update_early_take_profit_false():
    from src.services import user_service
    user_service.register_user(300)
    user_service.update_early_take_profit(300, True)
    user_service.update_early_take_profit(300, False)
    u = user_service.get_user(300)
    assert u.early_take_profit is False


# ============================================================
# /early 커맨드
# ============================================================

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
    message: FakeMessage

    @property
    def effective_user(self):
        return self.effective_chat


@dataclass
class FakeCtx:
    args: list
    user_data: dict


def _mk(chat_id):
    return FakeUpdate(effective_chat=FakeChat(id=chat_id), message=FakeMessage())


def test_early_cmd_no_args_shows_off_by_default():
    from src.services import user_service
    from src.bot.telegram_bot import cmd_early

    user_service.register_user(400)
    upd = _mk(400)
    asyncio.run(cmd_early(upd, FakeCtx(args=[], user_data={})))
    assert "OFF" in upd.message.replies[0]


def test_early_cmd_on_enables_flag():
    from src.services import user_service
    from src.bot.telegram_bot import cmd_early

    user_service.register_user(500)
    upd = _mk(500)
    asyncio.run(cmd_early(upd, FakeCtx(args=["on"], user_data={})))
    assert user_service.get_user(500).early_take_profit is True


def test_early_cmd_korean_arg():
    from src.services import user_service
    from src.bot.telegram_bot import cmd_early

    user_service.register_user(600)
    upd = _mk(600)
    asyncio.run(cmd_early(upd, FakeCtx(args=["켜"], user_data={})))
    assert user_service.get_user(600).early_take_profit is True

    upd2 = _mk(600)
    asyncio.run(cmd_early(upd2, FakeCtx(args=["꺼"], user_data={})))
    assert user_service.get_user(600).early_take_profit is False


# ============================================================
# price_monitor 핵심: day-TP 우선 평가
# ============================================================

def _seed_open_position(chat_id, code, buy_price, qty, target, stop, mode="bunt", opened_minutes_ago=60):
    """price_monitor 테스트용 — 봇 DB 에 open 포지션 1건 + audit/broker_orders 만들어줌."""
    from src.db.connection import get_connection
    opened_at = (datetime.now() - timedelta(minutes=opened_minutes_ago)).isoformat(timespec="seconds")
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO audit_log(chat_id, event_type, payload_json, ts) VALUES (?, 'order_buy', '{}', ?)",
            (chat_id, opened_at),
        )
        audit_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, status,
                filled_quantity, filled_avg_price, created_at, updated_at)
               VALUES (?, 'paper', 'buy', ?, ?, ?, 'filled', ?, ?, ?, ?)""",
            (audit_id, code, qty, buy_price, qty, buy_price, opened_at, opened_at),
        )
        bo_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO positions
               (chat_id, code, strategy_mode, buy_order_id, buy_price, quantity,
                target_price, stop_price, status, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (chat_id, code, mode, bo_id, buy_price, qty, target, stop, opened_at),
        )
        conn.execute(
            "INSERT OR IGNORE INTO instruments(code, name, market, updated_at) VALUES (?, '테스트', 'KOSPI', '2026-04-16')",
            (code,),
        )
        return cur.lastrowid
    finally:
        conn.close()


class FakeKIS:
    """KIS 어댑터 흉내 — fetch_current_prices 만 구현."""
    def __init__(self, prices_by_code):
        self.prices_by_code = prices_by_code  # {code: (price, high, low)}

    def fetch_current_prices(self, codes):
        from src.adapters.market_data_base import CurrentPrice
        out = {}
        for c in codes:
            v = self.prices_by_code.get(c)
            if v:
                p, h, l = v
                out[c] = CurrentPrice(
                    code=c, price=p, high=h, low=l,
                    open=p, prev_close=p, volume=0, change_pct=0.0,
                    fetched_at=datetime.now(),
                )
        return out


def test_early_tp_off_swing_position_does_NOT_fire_at_day_tp_level():
    """OFF 기본: bunt swing 포지션이 +3% 도달해도 swing TP(+7%)까지 안 오면 미발사."""
    from src.services import user_service, price_monitor

    chat_id = 700
    user_service.register_user(chat_id)
    # early_tp OFF (default)
    # buy 100,000, swing target 107,000, stop 96,000
    _seed_open_position(chat_id, "AAA", 100_000, 10, 107_000, 96_000, mode="bunt")

    # 현재가 103,000 (+3% — day TP 도달, swing TP 미도달)
    fake = FakeKIS({"AAA": (103_000, 103_500, 99_000)})
    monitor = price_monitor.PriceMonitor(kis=fake)
    alerts = monitor.check_positions(chat_id)

    # exit signal 없음 (NEAR_TP 등은 가능하지만 TP_HIT 은 아니어야)
    exit_alerts = [a for a in alerts if a.is_exit_signal]
    assert exit_alerts == []


def test_early_tp_on_swing_position_FIRES_at_day_tp_level():
    """ON: 같은 상황에서 day TP(+3%) 도달 → 즉시 TP_HIT."""
    from src.services import user_service, price_monitor

    chat_id = 800
    user_service.register_user(chat_id)
    user_service.update_early_take_profit(chat_id, True)
    _seed_open_position(chat_id, "BBB", 100_000, 10, 107_000, 96_000, mode="bunt")

    # 고가 103,500 → day TP (+3% = 103,000) 넘김
    fake = FakeKIS({"BBB": (103_200, 103_500, 99_000)})
    monitor = price_monitor.PriceMonitor(kis=fake)
    alerts = monitor.check_positions(chat_id)

    exit_alerts = [a for a in alerts if a.is_exit_signal]
    assert len(exit_alerts) == 1
    a = exit_alerts[0]
    assert a.alert_type.value == "tp_hit"
    # effective target 가 day-TP 로 낮춰져야
    assert a.target_price < 107_000
    # +3% 근처 (호가 정렬 때문에 ±수십원 가능)
    assert 102_500 <= a.target_price <= 103_500


def test_early_tp_on_squeeze_uses_5pct():
    """squeeze 모드에서 day TP = +5%."""
    from src.services import user_service, price_monitor

    chat_id = 900
    user_service.register_user(chat_id)
    user_service.update_early_take_profit(chat_id, True)
    _seed_open_position(chat_id, "CCC", 100_000, 10, 112_000, 95_000, mode="squeeze")

    # 고가 105,500 → squeeze day TP (+5% = 105,000) 넘김
    fake = FakeKIS({"CCC": (105_500, 106_000, 100_000)})
    monitor = price_monitor.PriceMonitor(kis=fake)
    alerts = monitor.check_positions(chat_id)

    exit_alerts = [a for a in alerts if a.is_exit_signal]
    assert len(exit_alerts) == 1
    a = exit_alerts[0]
    assert a.alert_type.value == "tp_hit"
    assert 104_500 <= a.target_price <= 106_000


def test_early_tp_on_does_not_change_stop_loss():
    """ON 이라도 SL 은 그대로 swing 기준."""
    from src.services import user_service, price_monitor

    chat_id = 1000
    user_service.register_user(chat_id)
    user_service.update_early_take_profit(chat_id, True)
    # bunt swing: SL=96,000 (-4%). day SL 이라면 -2% = 98,000.
    _seed_open_position(chat_id, "DDD", 100_000, 10, 107_000, 96_000, mode="bunt")

    # 저가 97,000 (-3% — day SL 이라면 도달했을 것이지만 swing SL 은 96,000 미달)
    fake = FakeKIS({"DDD": (97_500, 99_000, 97_000)})
    monitor = price_monitor.PriceMonitor(kis=fake)
    alerts = monitor.check_positions(chat_id)

    # SL_HIT 없어야 (swing -4% 까지 안 도달)
    exit_alerts = [a for a in alerts if a.alert_type.value == "sl_hit"]
    assert exit_alerts == []


def test_early_tp_on_swing_position_already_at_day_target_NOT_swing_target():
    """day_tp < swing target 일 때만 effective_target 가 낮춰짐. swing target 가 이미 더 낮으면 그대로."""
    from src.services import user_service, price_monitor

    chat_id = 1100
    user_service.register_user(chat_id)
    user_service.update_early_take_profit(chat_id, True)
    # 비정상: target_price 가 이미 day_tp 보다 낮은 케이스 (있을 수 없지만 방어)
    # bunt: day +3% = 103,000 / swing +7% = 107,000. target=105,000 (swing 미만이지만 day 초과)
    _seed_open_position(chat_id, "EEE", 100_000, 10, 105_000, 96_000, mode="bunt")

    # 현재가 103,500: day_tp(103,000) 도달 — early_tp 가 이걸 잡아서 발사해야
    fake = FakeKIS({"EEE": (103_500, 103_800, 99_000)})
    monitor = price_monitor.PriceMonitor(kis=fake)
    alerts = monitor.check_positions(chat_id)
    exit_alerts = [a for a in alerts if a.is_exit_signal]
    assert len(exit_alerts) == 1, "day_tp < swing target 이므로 day_tp 가 effective"
