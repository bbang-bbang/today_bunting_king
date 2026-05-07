"""Paper 트레이딩 어댑터 — DB 내부 시뮬레이션. 외부 호출 없음.

개발/테스트/초기 검증 단계에서 사용한다.
체결은 지정가 = 시장가로 가정하고 즉시 체결 처리한다.
수수료 0.015% × 2회 + 거래세 0.2% (매도 시) 를 반영한다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.adapters.broker_base import BrokerAdapter, OrderRequest, OrderResponse

COMMISSION_BPS = 15     # 0.015% = 15 bps (한 방향)
SELL_TAX_BPS = 20       # 매도 거래세 0.2% = 20 bps


class PaperBrokerAdapter(BrokerAdapter):
    async def submit_order(self, req: OrderRequest) -> OrderResponse:
        if req.price is None:
            return OrderResponse(
                broker_order_id="",
                status="failed",
                error="paper 모드는 지정가 필수 (시장가 미지원)",
            )

        order_id = f"PAPER-{uuid.uuid4().hex[:10]}"
        notional = req.price * req.quantity
        commission = notional * COMMISSION_BPS // 100_000
        tax = notional * SELL_TAX_BPS // 100_000 if req.side == "sell" else 0

        return OrderResponse(
            broker_order_id=order_id,
            status="filled",
            filled_quantity=req.quantity,
            filled_avg_price=req.price,
            commission=commission,
            tax=tax,
            raw={"paper_simulated_at": datetime.now(timezone.utc).isoformat()},
        )

    async def get_order_status(self, broker_order_id: str) -> OrderResponse:
        # paper 모드는 즉시 체결 처리되므로 조회 의미 없음
        return OrderResponse(broker_order_id=broker_order_id, status="filled")

    async def cancel_order(self, broker_order_id: str) -> bool:
        return True

    async def get_balance(self) -> dict:
        # 실제 잔고는 broker_orders/positions 테이블에서 집계. 여기선 자리만.
        return {"source": "paper", "note": "잔고는 DB 집계로 계산"}

    async def is_tradable(self, code: str) -> bool:
        # paper 모드는 모든 종목 거래 가능으로 가정
        return True
