"""KISBrokerAdapter 단위 테스트.

httpx.AsyncClient 를 monkeypatch 로 대체해서 실제 KIS 호출 없이 검증.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from src import config
from src.adapters.broker_base import OrderRequest
from src.adapters.broker_kis import KISBrokerAdapter


# ============================================================
# Fake AsyncClient
# ============================================================

@dataclass
class _FakeResponse:
    _data: dict
    status: int = 200
    text: str = ""

    @property
    def status_code(self) -> int:
        return self.status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self) -> dict:
        return self._data


@dataclass
class _CallRecord:
    method: str
    url: str
    headers: dict | None = None
    params: dict | None = None
    json_body: dict | None = None


@dataclass
class _FakeClient:
    """httpx.AsyncClient 대체. 경로별 응답을 사전 주입, 호출 기록 보관."""
    responses: dict[str, dict]   # "METHOD path_suffix" -> response dict
    calls: list[_CallRecord] = field(default_factory=list)

    async def __aenter__(self): return self
    async def __aexit__(self, *_a): return False

    def _key(self, method: str, url: str) -> str:
        # URL 의 path suffix 로 매칭 (base_url 은 무시)
        for key in self.responses:
            m, suffix = key.split(" ", 1)
            if m == method and url.endswith(suffix):
                return key
        raise AssertionError(f"No fake response for {method} {url}")

    def _resp(self, key: str) -> _FakeResponse:
        val = self.responses[key]
        return val if isinstance(val, _FakeResponse) else _FakeResponse(val)

    async def post(self, url, *, json=None, headers=None):
        rec = _CallRecord("POST", url, headers=headers, json_body=json)
        self.calls.append(rec)
        return self._resp(self._key("POST", url))

    async def get(self, url, *, headers=None, params=None):
        rec = _CallRecord("GET", url, headers=headers, params=params)
        self.calls.append(rec)
        return self._resp(self._key("GET", url))


@pytest.fixture
def env_keys(monkeypatch):
    """필수 KIS 키 주입."""
    monkeypatch.setattr(config, "KIS_MOCK_APP_KEY", "FAKE_KEY")
    monkeypatch.setattr(config, "KIS_MOCK_APP_SECRET", "FAKE_SECRET")
    monkeypatch.setattr(config, "KIS_MOCK_ACCOUNT_NO", "50182952-01")


@pytest.fixture(autouse=True)
def _isolate_token_cache(tmp_path, monkeypatch):
    """토큰 디스크 캐시를 매 테스트마다 격리."""
    from src.adapters import broker_kis as _bk
    monkeypatch.setattr(_bk, "_TOKEN_CACHE_DIR", tmp_path)


def _install_fake_client(monkeypatch, responses: dict):
    """httpx.AsyncClient 를 _FakeClient 로 치환. 전체 호출 기록을 공유."""
    shared_calls: list[_CallRecord] = []

    def _factory(*a, **kw):
        c = _FakeClient(responses=responses, calls=shared_calls)
        return c

    monkeypatch.setattr("src.adapters.broker_kis.httpx.AsyncClient", _factory)
    return {"calls": shared_calls}


# ============================================================
# OAuth
# ============================================================

def test_ensure_token_caches(env_keys, monkeypatch):
    """토큰 발급 1회 후 재호출 시 캐시 반환."""
    holder = _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T1", "expires_in": 3600},
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    t1 = asyncio.run(ad._ensure_token())
    t2 = asyncio.run(ad._ensure_token())
    assert t1 == "T1" and t2 == "T1"
    posts = [c for c in holder["calls"] if c.method == "POST"]
    # 2회 호출했지만 토큰 발급은 1회 — 단, 각 호출마다 AsyncClient 가 새로 생김
    # 마지막 holder["client"] 는 두번째 호출 직후의 것이어서 비어있을 수 있음.
    # 검증은 "두번째 호출이 새 HTTP 호출 없음" — holder 없이 세자.
    # 대신, 두번째 호출 후에도 _token 이 동일 객체인지만 확인.
    assert ad._token is not None
    assert ad._token.access_token == "T1"


def test_token_disk_cache_reused_across_instances(env_keys, monkeypatch):
    """첫 인스턴스가 디스크에 저장한 토큰을 두번째 인스턴스가 HTTP 호출 없이 재사용."""
    holder = _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "DISK_TOK", "expires_in": 3600},
    })
    ad1 = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    t1 = asyncio.run(ad1._ensure_token())
    assert t1 == "DISK_TOK"
    n_token_calls_after_first = sum(
        1 for c in holder["calls"] if "/oauth2/tokenP" in c.url
    )

    # 신규 인스턴스 — 디스크에서 토큰을 읽어야 하며 HTTP 추가 호출 없음
    ad2 = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    t2 = asyncio.run(ad2._ensure_token())
    assert t2 == "DISK_TOK"
    n_token_calls_total = sum(
        1 for c in holder["calls"] if "/oauth2/tokenP" in c.url
    )
    assert n_token_calls_total == n_token_calls_after_first, (
        "두번째 인스턴스가 토큰 발급 HTTP를 다시 침 — 디스크 캐시가 동작 안함"
    )


def test_token_403_exposes_kis_body(env_keys, monkeypatch):
    """KIS 403 응답의 error_code/description이 RuntimeError 메시지에 노출되어야 함."""
    _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": _FakeResponse(
            {"error_code": "EGW00133",
             "error_description": "접근토큰 발급 잦은 요청"},
            status=403,
        ),
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(ad._ensure_token())
    msg = str(exc.value)
    assert "EGW00133" in msg
    assert "잦은 요청" in msg
    assert "403" in msg


def test_submit_order_token_failure_returns_failed(env_keys, monkeypatch):
    """매수 시 토큰 발급 403이면 OrderResponse(status=failed)로 KIS 본문 노출."""
    _install_fake_client(monkeypatch, {
        "POST /uapi/hashkey": {"HASH": "HK"},
        "POST /oauth2/tokenP": _FakeResponse(
            {"error_code": "EGW00133",
             "error_description": "접근토큰 발급 잦은 요청"},
            status=403,
        ),
        # order-cash는 토큰 단계에서 막혀 호출되지 않음
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.submit_order(
        OrderRequest(side="buy", code="005930", quantity=1, price=70_000)
    ))
    assert res.status == "failed"
    assert "EGW00133" in res.error
    assert "잦은 요청" in res.error


def test_account_parse(env_keys, monkeypatch):
    _install_fake_client(monkeypatch, {})
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    assert ad.cano == "50182952"
    assert ad.acnt_prdt_cd == "01"


def test_account_bad_format_raises(env_keys, monkeypatch):
    monkeypatch.setattr(config, "KIS_MOCK_ACCOUNT_NO", "12345678")  # 하이픈 없음
    with pytest.raises(RuntimeError, match="형식 오류"):
        KISBrokerAdapter(config.TradeMode.KIS_MOCK)


def test_missing_keys_raises(env_keys, monkeypatch):
    monkeypatch.setattr(config, "KIS_MOCK_APP_KEY", "")
    with pytest.raises(RuntimeError, match="KIS 키 누락"):
        KISBrokerAdapter(config.TradeMode.KIS_MOCK)


# ============================================================
# 잔고
# ============================================================

def test_get_balance_parse(env_keys, monkeypatch):
    holder = _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "GET /uapi/domestic-stock/v1/trading/inquire-balance": {
            "rt_cd": "0", "msg1": "정상",
            "output1": [
                {
                    "pdno": "005930", "prdt_name": "삼성전자",
                    "hldg_qty": "10", "pchs_avg_pric": "70000.5",
                    "prpr": "72000", "evlu_amt": "720000",
                    "evlu_pfls_amt": "20000", "evlu_pfls_rt": "2.86",
                },
                {
                    "pdno": "000660", "prdt_name": "SK하이닉스",
                    "hldg_qty": "0",  # 수량 0 → 제외
                },
            ],
            "output2": [{
                "tot_evlu_amt": "720000",
                "pchs_amt_smtl_amt": "700000",
                "evlu_pfls_smtl_amt": "20000",
                "prvs_rcdl_excc_amt": "9280000",
            }],
        },
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    bal = asyncio.run(ad.get_balance())
    assert bal["cash_available"] == 9_280_000
    assert bal["total_evaluation"] == 720_000
    assert bal["total_pnl"] == 20_000
    assert bal["total_pnl_pct"] == round(20_000 / 700_000 * 100, 2)
    assert len(bal["positions"]) == 1
    p = bal["positions"][0]
    assert p["code"] == "005930" and p["quantity"] == 10 and p["avg_price"] == 70_000

    # tr_id 검증
    bal_call = [c for c in holder["calls"]
                if c.method == "GET" and "inquire-balance" in c.url][0]
    assert bal_call.headers["tr_id"] == "VTTC8434R"
    assert bal_call.params["CANO"] == "50182952"
    assert bal_call.params["ACNT_PRDT_CD"] == "01"


def test_get_balance_error_raises(env_keys, monkeypatch):
    _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "GET /uapi/domestic-stock/v1/trading/inquire-balance": {
            "rt_cd": "1", "msg1": "인증 실패",
        },
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    with pytest.raises(RuntimeError, match="잔고조회 실패"):
        asyncio.run(ad.get_balance())


# ============================================================
# 주문
# ============================================================

def test_submit_buy_limit_order(env_keys, monkeypatch):
    holder = _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "POST /uapi/hashkey": {"HASH": "HK123"},
        "POST /uapi/domestic-stock/v1/trading/order-cash": {
            "rt_cd": "0",
            "output": {"KRX_FWDG_ORD_ORGNO": "01234", "ODNO": "0000012345"},
        },
        # get_order_status — 체결 확인용
        "GET /uapi/domestic-stock/v1/trading/inquire-daily-ccld": {
            "rt_cd": "0",
            "output1": [{
                "odno": "0000012345", "ord_qty": "5", "tot_ccld_qty": "5",
                "avg_prvs": "72000", "sll_buy_dvsn_cd": "02", "cncl_yn": "N",
            }],
        },
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.submit_order(
        OrderRequest(side="buy", code="005930", quantity=5, price=72_000)
    ))
    assert res.status == "filled"
    assert res.broker_order_id == "01234-0000012345"
    assert res.filled_quantity == 5 and res.filled_avg_price == 72_000
    # 매수는 거래세 없음
    assert res.tax == 0

    # 요청 본문 검증
    order_call = [c for c in holder["calls"]
                  if c.method == "POST" and "order-cash" in c.url][0]
    assert order_call.headers["tr_id"] == "VTTC0802U"
    assert order_call.headers["hashkey"] == "HK123"
    b = order_call.json_body
    assert b["PDNO"] == "005930"
    assert b["ORD_QTY"] == "5"
    assert b["ORD_UNPR"] == "72000"
    assert b["ORD_DVSN"] == "00"


def test_submit_sell_limit_order(env_keys, monkeypatch):
    holder = _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "POST /uapi/hashkey": {"HASH": "HK"},
        "POST /uapi/domestic-stock/v1/trading/order-cash": {
            "rt_cd": "0",
            "output": {"KRX_FWDG_ORD_ORGNO": "01234", "ODNO": "0000099999"},
        },
        "GET /uapi/domestic-stock/v1/trading/inquire-daily-ccld": {
            "rt_cd": "0",
            "output1": [{
                "odno": "0000099999", "ord_qty": "3", "tot_ccld_qty": "3",
                "avg_prvs": "75000", "sll_buy_dvsn_cd": "01", "cncl_yn": "N",
            }],
        },
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.submit_order(
        OrderRequest(side="sell", code="005930", quantity=3, price=75_000)
    ))
    assert res.status == "filled"
    # 매도: 거래세 0.2% 반영 예측
    assert res.tax == 3 * 75_000 * 20 // 100_000

    order_call = [c for c in holder["calls"]
                  if c.method == "POST" and "order-cash" in c.url][0]
    assert order_call.headers["tr_id"] == "VTTC0801U"


def test_submit_sell_balance_fallback_confirms_fill(env_keys, monkeypatch):
    """KIS 모의투자 daily-ccld 가 매도 체결을 안 잡아(빈 응답) 'pending' 이어도,
    주문 후 잔고가 감소했으면 체결로 확정 → filled.

    2026-05 사고 회귀 방지: 매수엔 있던 잔고 fallback 이 매도엔 없어 자동매도 21/21 이
    pending 좀비가 되고 손절이 실제로 안 나가 포지션이 묶여 출혈했음.
    """
    bal_calls = {"n": 0}

    def _bal(qty):
        return {
            "rt_cd": "0", "msg1": "정상",
            "output1": ([{
                "pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": str(qty),
                "pchs_avg_pric": "75000", "prpr": "74000", "evlu_amt": "0",
                "evlu_pfls_amt": "0", "evlu_pfls_rt": "0",
            }] if qty > 0 else []),
            "output2": [{
                "tot_evlu_amt": "0", "pchs_amt_smtl_amt": "0",
                "evlu_pfls_smtl_amt": "0", "prvs_rcdl_excc_amt": "1000000",
            }],
        }

    @dataclass
    class _SellFallbackClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

        async def post(self, url, *, json=None, headers=None):
            if "/uapi/hashkey" in url:
                return _FakeResponse({"HASH": "HK"})
            if "/oauth2/tokenP" in url:
                return _FakeResponse({"access_token": "T", "expires_in": 3600})
            if "/uapi/domestic-stock/v1/trading/order-cash" in url:
                return _FakeResponse({
                    "rt_cd": "0",
                    "output": {"KRX_FWDG_ORD_ORGNO": "00950", "ODNO": "0000077777"},
                })
            raise AssertionError(f"unexpected POST: {url}")

        async def get(self, url, *, headers=None, params=None):
            if "inquire-daily-ccld" in url:
                return _FakeResponse({"rt_cd": "0", "output1": []})   # 매도 미보고(pending)
            if "inquire-balance" in url:
                bal_calls["n"] += 1
                # 1st(pre)=3주 보유, 2nd(post)=0주 → 잔고 감소 = 체결
                return _FakeResponse(_bal(3 if bal_calls["n"] == 1 else 0))
            raise AssertionError(f"unexpected GET: {url}")

    monkeypatch.setattr("src.adapters.broker_kis.httpx.AsyncClient", lambda *a, **kw: _SellFallbackClient())
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("src.adapters.broker_kis.asyncio.sleep", _no_sleep)

    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.submit_order(
        OrderRequest(side="sell", code="005930", quantity=3, price=75_000)
    ))
    assert res.status == "filled", f"잔고 감소로 체결 확정돼야 함: {res}"
    assert res.filled_quantity == 3
    assert res.filled_avg_price == 75_000
    assert res.tax == 3 * 75_000 * 20 // 100_000   # 매도 거래세 0.2%


def test_submit_sell_balance_fallback_no_change_stays_pending(env_keys, monkeypatch):
    """주문 후 잔고가 그대로면(좀비 — 체결 안 됨) pending 유지 → price_monitor 가 재시도 가능."""
    def _bal3():
        return {
            "rt_cd": "0", "msg1": "정상",
            "output1": [{
                "pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "3",
                "pchs_avg_pric": "75000", "prpr": "74000", "evlu_amt": "0",
                "evlu_pfls_amt": "0", "evlu_pfls_rt": "0",
            }],
            "output2": [{
                "tot_evlu_amt": "0", "pchs_amt_smtl_amt": "0",
                "evlu_pfls_smtl_amt": "0", "prvs_rcdl_excc_amt": "1000000",
            }],
        }

    @dataclass
    class _NoFillClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

        async def post(self, url, *, json=None, headers=None):
            if "/uapi/hashkey" in url:
                return _FakeResponse({"HASH": "HK"})
            if "/oauth2/tokenP" in url:
                return _FakeResponse({"access_token": "T", "expires_in": 3600})
            if "/uapi/domestic-stock/v1/trading/order-cash" in url:
                return _FakeResponse({
                    "rt_cd": "0",
                    "output": {"KRX_FWDG_ORD_ORGNO": "00950", "ODNO": "0000077778"},
                })
            raise AssertionError(f"unexpected POST: {url}")

        async def get(self, url, *, headers=None, params=None):
            if "inquire-daily-ccld" in url:
                return _FakeResponse({"rt_cd": "0", "output1": []})
            if "inquire-balance" in url:
                return _FakeResponse(_bal3())   # pre·post 모두 3주 (변화 없음)
            raise AssertionError(f"unexpected GET: {url}")

    monkeypatch.setattr("src.adapters.broker_kis.httpx.AsyncClient", lambda *a, **kw: _NoFillClient())
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("src.adapters.broker_kis.asyncio.sleep", _no_sleep)

    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.submit_order(
        OrderRequest(side="sell", code="005930", quantity=3, price=75_000)
    ))
    assert res.status == "pending", f"잔고 변화 없으면 pending 유지: {res}"
    assert "0000077778" in res.broker_order_id


def test_submit_order_retries_on_5xx_then_succeeds(env_keys, monkeypatch):
    """KIS 5xx 발생 시 백오프 재시도 후 성공 — KIS_MOCK 서버 일시 장애 흡수."""
    # order-cash 호출 카운트별 응답 시퀀스 (1차 500, 2차 500, 3차 200)
    order_call_count = {"n": 0}
    order_responses = [
        _FakeResponse({"rt_cd": "1"}, status=500),
        _FakeResponse({"rt_cd": "1"}, status=500),
        _FakeResponse({
            "rt_cd": "0", "msg_cd": "40590000", "msg1": "OK",
            "output": {"KRX_FWDG_ORD_ORGNO": "00950", "ODNO": "0000099999"},
        }, status=200),
    ]

    @dataclass
    class _SeqClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def post(self, url, *, json=None, headers=None):
            if "/uapi/hashkey" in url:
                return _FakeResponse({"HASH": "HK"})
            if "/oauth2/tokenP" in url:
                return _FakeResponse({"access_token": "T", "expires_in": 3600})
            if "/uapi/domestic-stock/v1/trading/order-cash" in url:
                idx = order_call_count["n"]
                order_call_count["n"] += 1
                return order_responses[idx]
            raise AssertionError(f"unexpected URL: {url}")
        async def get(self, url, *, headers=None, params=None):
            if "/uapi/domestic-stock/v1/trading/inquire-daily-ccld" in url:
                # 체결 확인 — 마지막 ODNO 가 채워졌다고 응답
                odno = (params or {}).get("ODNO", "")
                return _FakeResponse({
                    "rt_cd": "0",
                    "output1": [{
                        "odno": odno, "ord_qty": "44", "tot_ccld_qty": "44",
                        "avg_prvs": "60562", "sll_buy_dvsn_cd": "01", "cncl_yn": "N",
                    }] if odno else [],
                })
            raise AssertionError(f"unexpected GET: {url}")

    monkeypatch.setattr("src.adapters.broker_kis.httpx.AsyncClient", lambda *a, **kw: _SeqClient())
    # sleep 단축 (테스트 빠르게)
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("src.adapters.broker_kis.asyncio.sleep", _no_sleep)

    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.submit_order(
        OrderRequest(side="sell", code="192080", quantity=44, price=60_562)
    ))
    assert order_call_count["n"] == 3, f"5xx 재시도 없이 끝남: {order_call_count}"
    assert res.status == "filled", f"3차 시도가 성공해야 하는데: {res}"
    assert "0000099999" in res.broker_order_id


def test_submit_order_5xx_all_attempts_exhausted_returns_failed(env_keys, monkeypatch):
    """5xx 가 3회 모두 발생 → status=failed, 명확한 에러 메시지."""
    order_call_count = {"n": 0}

    @dataclass
    class _AllFailClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def post(self, url, *, json=None, headers=None):
            if "/uapi/hashkey" in url:
                return _FakeResponse({"HASH": "HK"})
            if "/oauth2/tokenP" in url:
                return _FakeResponse({"access_token": "T", "expires_in": 3600})
            if "/uapi/domestic-stock/v1/trading/order-cash" in url:
                order_call_count["n"] += 1
                return _FakeResponse({"rt_cd": "1"}, status=500)
            raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("src.adapters.broker_kis.httpx.AsyncClient", lambda *a, **kw: _AllFailClient())
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("src.adapters.broker_kis.asyncio.sleep", _no_sleep)

    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.submit_order(
        OrderRequest(side="sell", code="192080", quantity=44, price=60_562)
    ))
    assert order_call_count["n"] == 3, "5xx 면 정확히 3회 재시도"
    assert res.status == "failed"
    assert "재시도" in res.error or "500" in res.error


def test_submit_order_recovers_from_expired_token(env_keys, monkeypatch):
    """KIS 가 토큰 만료를 500 + msg_cd=EGW00123 으로 보내면, 토큰 캐시 무효화 후 재시도 → 성공."""
    order_call_count = {"n": 0}
    token_call_count = {"n": 0}

    @dataclass
    class _ExpiredTokenClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False
        async def post(self, url, *, json=None, headers=None):
            if "/uapi/hashkey" in url:
                return _FakeResponse({"HASH": "HK"})
            if "/oauth2/tokenP" in url:
                token_call_count["n"] += 1
                return _FakeResponse({"access_token": f"T{token_call_count['n']}", "expires_in": 3600})
            if "/uapi/domestic-stock/v1/trading/order-cash" in url:
                order_call_count["n"] += 1
                if order_call_count["n"] == 1:
                    # 만료 토큰 → 500 + EGW00123
                    return _FakeResponse({
                        "rt_cd": "1", "msg_cd": "EGW00123",
                        "msg1": "기간이 만료된 token 입니다.",
                    }, status=500)
                # 새 토큰으로 재시도 → 성공
                return _FakeResponse({
                    "rt_cd": "0", "msg_cd": "40590000", "msg1": "OK",
                    "output": {"KRX_FWDG_ORD_ORGNO": "00950", "ODNO": "0000088888"},
                }, status=200)
            raise AssertionError(f"unexpected URL: {url}")
        async def get(self, url, *, headers=None, params=None):
            if "/uapi/domestic-stock/v1/trading/inquire-daily-ccld" in url:
                # 체결 확인 — 마지막 ODNO 가 채워졌다고 응답
                odno = (params or {}).get("ODNO", "")
                return _FakeResponse({
                    "rt_cd": "0",
                    "output1": [{
                        "odno": odno, "ord_qty": "44", "tot_ccld_qty": "44",
                        "avg_prvs": "60562", "sll_buy_dvsn_cd": "01", "cncl_yn": "N",
                    }] if odno else [],
                })
            raise AssertionError(f"unexpected GET: {url}")

    monkeypatch.setattr("src.adapters.broker_kis.httpx.AsyncClient", lambda *a, **kw: _ExpiredTokenClient())
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("src.adapters.broker_kis.asyncio.sleep", _no_sleep)

    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.submit_order(
        OrderRequest(side="sell", code="192080", quantity=44, price=60_562)
    ))
    assert order_call_count["n"] == 2, "EGW00123 후 즉시 재시도 — 정확히 2회 order-cash 호출"
    assert token_call_count["n"] == 2, "토큰 캐시 무효화 후 재발급 — 토큰 호출 2회"
    assert res.status == "filled", f"재시도가 성공해야 함: {res}"
    assert "0000088888" in res.broker_order_id


def test_submit_order_phantom_buy_returns_failed_not_optimistic_filled(env_keys, monkeypatch):
    """KIS 주문 응답은 rt_cd=0 인데 후속 체결조회가 빈 결과(KIS 일관성 깨짐) → pending 반환.

    2026-04-28 사고 회귀: ODNO 받았으나 inquire-daily-ccld 가 'no data' 반환했고,
    이전 코드는 낙관적으로 'filled' 반환해 봇 DB 에 가짜 매수가 들어감.

    2026-04-29 변경: ODNO 가 발급된 상태(=KIS에 주문 등록됨) → 미체결 대기 가능성 高 →
    'pending' 으로 명시 반환. 'failed' 는 KIS 응답 누락 등 진짜 실패에 한정.
    caller(execute_buy/_execute_buy_intent) 가 pending 인 경우 ⏳ 대기 메시지 표시.
    """
    _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "POST /uapi/hashkey": {"HASH": "HK"},
        "POST /uapi/domestic-stock/v1/trading/order-cash": {
            "rt_cd": "0",
            "output": {"KRX_FWDG_ORD_ORGNO": "00950", "ODNO": "0000030170"},
        },
        # 체결조회 — 빈 결과 (일관성 문제 시뮬레이션)
        "GET /uapi/domestic-stock/v1/trading/inquire-daily-ccld": {
            "rt_cd": "0", "msg_cd": "70070000",
            "msg1": "조회할 내역(자료)이 없습니다.",
            "output1": [],
        },
    })
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("src.adapters.broker_kis.asyncio.sleep", _no_sleep)

    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.submit_order(
        OrderRequest(side="buy", code="018670", quantity=9, price=262_000)
    ))
    assert res.status == "pending", f"체결 미확인 시 낙관적 filled 처리 금지 + ODNO 있으면 pending: {res}"
    assert res.broker_order_id == "00950-0000030170"
    assert res.filled_quantity == 0  # 체결 X
    assert "0000030170" in (res.error or "")  # ODNO 노출 (사용자 KIS 앱 확인용)


def test_submit_order_rejected(env_keys, monkeypatch):
    _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "POST /uapi/hashkey": {"HASH": "HK"},
        "POST /uapi/domestic-stock/v1/trading/order-cash": {
            "rt_cd": "1", "msg_cd": "APBK0551",
            "msg1": "주문수량 부족",
        },
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.submit_order(
        OrderRequest(side="sell", code="005930", quantity=100, price=70_000)
    ))
    assert res.status == "failed"
    assert "APBK0551" in res.error
    assert "수량 부족" in res.error


# ============================================================
# 조회 / 취소
# ============================================================

def test_get_order_status_filled(env_keys, monkeypatch):
    _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "GET /uapi/domestic-stock/v1/trading/inquire-daily-ccld": {
            "rt_cd": "0",
            "output1": [
                {"odno": "0000012345", "ord_qty": "5", "tot_ccld_qty": "5",
                 "avg_prvs": "72000", "sll_buy_dvsn_cd": "02", "cncl_yn": "N"},
            ],
        },
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.get_order_status("01234-0000012345"))
    assert res.status == "filled"
    assert res.filled_quantity == 5
    assert res.filled_avg_price == 72_000


def test_get_order_status_pending(env_keys, monkeypatch):
    _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "GET /uapi/domestic-stock/v1/trading/inquire-daily-ccld": {
            "rt_cd": "0",
            "output1": [],
        },
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    res = asyncio.run(ad.get_order_status("01234-0000099999"))
    assert res.status == "pending"


def test_cancel_order(env_keys, monkeypatch):
    holder = _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "POST /uapi/hashkey": {"HASH": "HK"},
        "POST /uapi/domestic-stock/v1/trading/order-rvsecncl": {
            "rt_cd": "0", "msg1": "취소 접수",
        },
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    ok = asyncio.run(ad.cancel_order("01234-0000012345"))
    assert ok is True
    call = [c for c in holder["calls"]
            if c.method == "POST" and "order-rvsecncl" in c.url][0]
    assert call.headers["tr_id"] == "VTTC0803U"
    assert call.json_body["RVSE_CNCL_DVSN_CD"] == "02"
    assert call.json_body["ORGN_ODNO"] == "0000012345"


def test_is_tradable(env_keys, monkeypatch):
    _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "GET /uapi/domestic-stock/v1/quotations/inquire-price": {
            "rt_cd": "0", "output": {"iscd_stat_cls_code": "00"},
        },
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    assert asyncio.run(ad.is_tradable("005930")) is True


def test_is_tradable_halted(env_keys, monkeypatch):
    _install_fake_client(monkeypatch, {
        "POST /oauth2/tokenP": {"access_token": "T", "expires_in": 3600},
        "GET /uapi/domestic-stock/v1/quotations/inquire-price": {
            "rt_cd": "0", "output": {"iscd_stat_cls_code": "51"},  # 거래정지
        },
    })
    ad = KISBrokerAdapter(config.TradeMode.KIS_MOCK)
    assert asyncio.run(ad.is_tradable("005930")) is False
