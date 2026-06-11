"""브로커 잔고 TTL 캐시 (get_broker_balance) 테스트.

KIS inquire-balance 가 느린 날 여러 잡/버튼의 중복·동시 호출을 단일 네트워크 콜로
합쳐 /잔고 체감 지연을 줄인다. 매수/매도 후엔 무효화되어 항상 최신.
"""
from __future__ import annotations

import asyncio

import pytest

import src.config as config
import src.services.portfolio_service as ps


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    ps.invalidate_balance_cache()
    monkeypatch.setattr(config, "BALANCE_CACHE_TTL_SEC", 8.0)
    yield
    ps.invalidate_balance_cache()


def _install_broker(calls: dict, delay: float = 0.05, payload: dict | None = None):
    payload = payload if payload is not None else {"positions": [], "total_evaluation": 1_000_000}

    class _Fake:
        async def get_balance(self):
            calls["n"] = calls.get("n", 0) + 1
            await asyncio.sleep(delay)
            return dict(payload)

    ps.get_broker = lambda mode: _Fake()  # type: ignore[assignment]


def test_concurrent_calls_collapse_to_one():
    """동시 5건 → 락으로 단일 KIS 콜."""
    calls: dict = {}
    _install_broker(calls, delay=0.2)

    async def run():
        return await asyncio.gather(*[ps.get_broker_balance("kis_mock") for _ in range(5)])

    results = asyncio.run(run())
    assert calls["n"] == 1
    assert all(r["total_evaluation"] == 1_000_000 for r in results)


def test_ttl_hit_serves_cache():
    calls: dict = {}
    _install_broker(calls)

    async def run():
        await ps.get_broker_balance("kis_mock")
        await ps.get_broker_balance("kis_mock")  # TTL 내 → 캐시

    asyncio.run(run())
    assert calls["n"] == 1


def test_invalidate_forces_refetch():
    calls: dict = {}
    _install_broker(calls)

    async def run():
        await ps.get_broker_balance("kis_mock")
        ps.invalidate_balance_cache("kis_mock")
        await ps.get_broker_balance("kis_mock")

    asyncio.run(run())
    assert calls["n"] == 2


def test_error_response_not_cached():
    """KIS 실패 응답은 캐시하지 않아 다음 호출에서 즉시 재시도."""
    calls: dict = {}

    class _Boom:
        async def get_balance(self):
            calls["n"] = calls.get("n", 0) + 1
            raise RuntimeError("KIS 다운")

    ps.get_broker = lambda mode: _Boom()  # type: ignore[assignment]

    async def run():
        r1 = await ps.get_broker_balance("kis_mock")
        r2 = await ps.get_broker_balance("kis_mock")
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert "error" in r1 and "error" in r2
    assert calls["n"] == 2  # 캐시 안 했으므로 매번 시도


def test_ttl_zero_disables_cache(monkeypatch):
    monkeypatch.setattr(config, "BALANCE_CACHE_TTL_SEC", 0.0)
    calls: dict = {}
    _install_broker(calls)

    async def run():
        await ps.get_broker_balance("kis_mock")
        await ps.get_broker_balance("kis_mock")

    asyncio.run(run())
    assert calls["n"] == 2


def test_paper_mode_returns_none():
    calls: dict = {}
    _install_broker(calls)
    assert asyncio.run(ps.get_broker_balance("paper")) is None
    assert calls.get("n", 0) == 0
