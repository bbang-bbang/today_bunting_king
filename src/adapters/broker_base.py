"""Broker adapter 추상 인터페이스.

모든 브로커 구현(paper/kis_mock/live)은 이 인터페이스를 따른다.
주문 호출 전 반드시 RiskGuard.check() 를 통과해야 한다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class OrderRequest:
    side: str                  # 'buy' | 'sell'
    code: str
    quantity: int
    price: int | None = None   # None = 시장가


@dataclass
class OrderResponse:
    broker_order_id: str
    status: str                # 'pending'|'filled'|'partial'|'failed'|'cancelled'
    filled_quantity: int = 0
    filled_avg_price: int = 0
    commission: int = 0
    tax: int = 0
    error: str = ""
    raw: dict | None = None    # 브로커 원응답 (audit_log 저장용)


class BrokerAdapter(ABC):
    """모든 어댑터는 async 인터페이스로 통일."""

    @abstractmethod
    async def submit_order(self, req: OrderRequest) -> OrderResponse: ...

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> OrderResponse: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool: ...

    @abstractmethod
    async def get_balance(self) -> dict: ...

    @abstractmethod
    async def is_tradable(self, code: str) -> bool:
        """거래 가능 여부 (거래정지/VI/상하한가 등 제외)."""
        ...
