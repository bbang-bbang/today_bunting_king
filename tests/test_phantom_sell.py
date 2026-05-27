"""매도 phantom 탈출 (_escape_phantom_sell) 테스트.

KIS 모의투자가 marketable 매도도 영영 pending 으로 응답하는 phantom 대응.
잔고(=진실) 기준 강제 해소: 잔고0→청산, 잔고보유→취소+시장가 재집행(상한).
2026-05 005940(-13%) 2주 방치 사고 회귀 방지.
"""
from __future__ import annotations

import asyncio

import pytest

from src.adapters.broker_base import OrderRequest, OrderResponse
from src.bot import scheduler
from src.db.connection import get_connection, init_schema


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import src.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    import src.db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", db_path)
    init_schema()
    yield db_path


class _Bot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _Ctx:
    def __init__(self):
        self.bot = _Bot()


class _Adapter:
    def __init__(self, positions, submit=None, raise_balance=False):
        self._positions = positions
        self._submit = submit
        self.raise_balance = raise_balance
        self.cancelled: list[str] = []
        self.submitted: list[OrderRequest] = []

    async def get_balance(self):
        if self.raise_balance:
            raise RuntimeError("balance boom")
        return {"positions": self._positions}

    async def cancel_order(self, odno):
        self.cancelled.append(odno)
        return True

    async def submit_order(self, req):
        self.submitted.append(req)
        return self._submit


_ODNO = "00950-0000044804"


def _seed(code="005940", qty=70, buy=37050):
    from src.services import audit_service
    conn = get_connection()
    try:
        conn.execute("INSERT INTO bot_users(chat_id,status,registered_at) VALUES(999,'approved','t')")
        conn.commit()
    finally:
        conn.close()
    aid = audit_service.log_event(999, "order_sell_pending", {"code": code})
    conn = get_connection()
    try:
        # 매수 체결 주문 (positions.buy_order_id NOT NULL)
        cur = conn.execute(
            "INSERT INTO broker_orders(audit_id,trade_mode,side,code,quantity,price,broker_order_id,"
            "status,filled_quantity,filled_avg_price,commission,tax,created_at,updated_at) "
            "VALUES(?,?,'buy',?,?,?,?,'filled',?,?,0,0,?,?)",
            (aid, "kis_mock", code, qty, buy, "00950-0000003841", qty, buy,
             "2026-05-11T09:09:51", "2026-05-11T09:09:51"),
        )
        buy_bo_id = cur.lastrowid
        # 매도 pending(phantom) 주문
        cur = conn.execute(
            "INSERT INTO broker_orders(audit_id,trade_mode,side,code,quantity,price,broker_order_id,"
            "status,filled_quantity,filled_avg_price,commission,tax,created_at,updated_at) "
            "VALUES(?,?,'sell',?,?,?,?,'pending',0,0,0,0,?,?)",
            (aid, "kis_mock", code, qty, 33750, _ODNO,
             "2026-05-12T14:41:28", "2026-05-12T14:41:28"),
        )
        bo_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO positions(chat_id,code,strategy_mode,buy_order_id,buy_price,quantity,"
            "target_price,stop_price,status,sell_order_id,opened_at) "
            "VALUES(999,?,'bunt',?,?,?,0,0,'open',?,'2026-05-11T09:09:51')",
            (code, buy_bo_id, buy, qty, bo_id),
        )
        pid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return bo_id, pid


def _pos(pid):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT status,pnl,sell_order_id FROM positions WHERE id=?", (pid,)
        ).fetchone()
    finally:
        conn.close()


def test_phantom_closes_when_balance_empty(temp_db):
    """KIS 잔고 0 → 이미 빠짐 → 포지션 청산(pnl NULL), sell_order_id 해제."""
    bo_id, pid = _seed()
    ad = _Adapter(positions=[])
    ctx = _Ctx()
    ok = asyncio.run(scheduler._escape_phantom_sell(
        ctx, ad, bo_id=bo_id, code="005940", qty=70, broker_order_id=_ODNO,
        chat_id=999, position_id=pid, buy_price=37050,
    ))
    assert ok is True
    st = _pos(pid)
    assert st[0] == "closed"
    assert st[1] is None       # pnl NULL (체결가 미상)
    assert st[2] is None       # sell_order_id 해제


def test_phantom_market_resubmit_fills_and_closes(temp_db):
    """잔고 보유 → phantom → 기존 주문 취소 + 시장가 재발주 → 체결 → 청산."""
    bo_id, pid = _seed()
    filled = OrderResponse(
        broker_order_id="00950-0000055555", status="filled",
        filled_quantity=70, filled_avg_price=0, commission=0, tax=0,
    )
    ad = _Adapter(
        positions=[{"code": "005940", "quantity": 70, "current_price": 33000}],
        submit=filled,
    )
    ctx = _Ctx()
    ok = asyncio.run(scheduler._escape_phantom_sell(
        ctx, ad, bo_id=bo_id, code="005940", qty=70, broker_order_id=_ODNO,
        chat_id=999, position_id=pid, buy_price=37050,
    ))
    assert ok is True
    assert ad.cancelled == [_ODNO]                  # 기존 phantom 취소
    assert len(ad.submitted) == 1
    assert ad.submitted[0].price is None            # 시장가 재발주
    st = _pos(pid)
    assert st[0] == "closed"
    # 시장가 체결가 미보고(0) → 현재가 33,000 추정으로 pnl 계산
    assert st[1] == (33000 - 37050) * 70


def test_phantom_giveup_after_max_retry(temp_db, monkeypatch):
    """재집행 한도 초과 → 시장가 재발주 안 함 + 관리자 알림."""
    bo_id, pid = _seed()
    from src.services import audit_service
    for _ in range(scheduler._SELL_PHANTOM_MAX_RETRY):
        audit_service.log_event(999, "sell_phantom_escalated", {"code": "005940"})
    monkeypatch.setattr(scheduler.config, "TELEGRAM_ADMIN_CHAT_ID", 12345)
    ad = _Adapter(positions=[{"code": "005940", "quantity": 70, "current_price": 33000}])
    ctx = _Ctx()
    ok = asyncio.run(scheduler._escape_phantom_sell(
        ctx, ad, bo_id=bo_id, code="005940", qty=70, broker_order_id=_ODNO,
        chat_id=999, position_id=pid, buy_price=37050,
    ))
    assert ok is True
    assert ad.submitted == []                        # 재집행 중단(churn 방지)
    assert any(c == 12345 and "한도" in t for c, t in ctx.bot.sent)   # 관리자 알림
    assert _pos(pid)[0] == "open"                     # 자동 청산 안 함(수동 대기)


def test_phantom_returns_false_on_balance_error(temp_db):
    """잔고 조회 실패 → 미처리(False) → 폴링이 기존 분기로 fallback."""
    bo_id, pid = _seed()
    ad = _Adapter(positions=[], raise_balance=True)
    ctx = _Ctx()
    ok = asyncio.run(scheduler._escape_phantom_sell(
        ctx, ad, bo_id=bo_id, code="005940", qty=70, broker_order_id=_ODNO,
        chat_id=999, position_id=pid, buy_price=37050,
    ))
    assert ok is False
    assert _pos(pid)[0] == "open"                     # 손 안 댐
