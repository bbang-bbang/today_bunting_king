"""KIS 실계좌 ↔ 봇 DB positions 정합성 점검 + ghost 정리.

dry-run (default): 차이 출력만
--apply           : DB-only 'open' 포지션을 status='closed', pnl=0 으로 정리

ghost = 봇 DB 는 open 인데 KIS 잔고에 없는 포지션 (주문 거절·외부청산·체결 누락 등)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from src import config
from src.db.connection import get_connection
from src.services import audit_service, portfolio_service, user_service


async def reconcile_chat(chat_id: int, apply: bool, grace_minutes: int = 15) -> dict:
    conn = get_connection()
    open_db = [
        dict(r)
        for r in conn.execute(
            "SELECT id, code, quantity, buy_price, strategy_mode, opened_at, sell_order_id FROM positions "
            "WHERE chat_id=? AND status='open' ORDER BY id",
            (chat_id,),
        )
    ]

    bal = await portfolio_service.get_broker_balance(config.TRADE_MODE.value)
    if bal is None or "error" in bal:
        return {"chat_id": chat_id, "error": (bal or {}).get("error", "조회 실패")}

    kis_by_code: dict[str, dict] = {p["code"]: p for p in bal["positions"]}
    db_codes_open: set[str] = {d["code"] for d in open_db}

    matched: list[tuple[dict, dict]] = []
    mismatched: list[tuple[dict, dict]] = []
    ghosts: list[dict] = []
    grace_protected: list[dict] = []  # 최근 매수 → KIS 동기화 지연 가능, ghost 판정 보류
    pending_sell: list[dict] = []  # sell_order_id 마킹 → 매도 주문 진행 중, ghost 판정 보류
    orphans: list[dict] = []  # KIS 보유인데 봇 DB open 에 없음 (매수 체결 누락 케이스)
    now = datetime.now()
    for d in open_db:
        kp = kis_by_code.get(d["code"])
        if kp is None:
            # 매도 주문 진행 중 (sell_order_id 마킹) — KIS 가 미체결 매도분을 잔고에서 차감해서 안 보일 수 있음
            if d.get("sell_order_id"):
                pending_sell.append(d)
                continue
            # KIS 일시적 잔고 누락 보호: opened_at 이 grace_minutes 이내면 ghost 처리 X
            try:
                opened_at = datetime.fromisoformat(d["opened_at"])
                if (now - opened_at).total_seconds() < grace_minutes * 60:
                    grace_protected.append(d)
                    continue
            except Exception:
                pass
            ghosts.append(d)
        else:
            # 같은 code 다중 lot 합산 비교
            kis_qty = kp["quantity"]
            kis_avg = kp["avg_price"]
            db_lots_same_code = [x for x in open_db if x["code"] == d["code"]]
            db_qty = sum(x["quantity"] for x in db_lots_same_code)
            if kis_qty == db_qty and kis_avg == d["buy_price"]:
                matched.append((d, kp))
            else:
                mismatched.append((d, kp))

    # KIS → DB orphan 검출 (체결 누락 케이스)
    for code, kp in kis_by_code.items():
        if code not in db_codes_open and int(kp.get("quantity", 0)) > 0:
            orphans.append(kp)

    print(f"\n=== chat_id={chat_id} ===")
    print(f"DB open: {len(open_db)}, KIS held: {len(kis_by_code)}, grace={grace_minutes}분")
    print(f"  ✓ 정상 매칭: {len(matched)}")
    print(f"  ⚠ 수량/가격 차이: {len(set(m[0]['code'] for m in mismatched))}건")
    for d, kp in mismatched:
        print(
            f"     [{d['code']}] DB id={d['id']} qty={d['quantity']}@{d['buy_price']:,} "
            f"vs KIS qty={kp['quantity']}@{kp['avg_price']:,}"
        )
    print(f"  ⏳ grace 보호 (최근 매수, ghost 보류): {len(grace_protected)}건")
    for d in grace_protected:
        print(
            f"     id={d['id']} [{d['code']}] qty={d['quantity']}@{d['buy_price']:,} "
            f"opened_at={d['opened_at']}"
        )
    print(f"  📤 매도 주문 진행중 (sell_order_id 마킹, ghost 보류): {len(pending_sell)}건")
    for d in pending_sell:
        print(
            f"     id={d['id']} [{d['code']}] qty={d['quantity']}@{d['buy_price']:,} "
            f"sell_order_id={d['sell_order_id']}"
        )
    print(f"  👻 ghost (DB만 open, grace 외): {len(ghosts)}건")
    for d in ghosts:
        print(
            f"     id={d['id']} [{d['code']}] qty={d['quantity']}@{d['buy_price']:,} "
            f"mode={d['strategy_mode']}"
        )
    print(f"  🆕 orphan (KIS 보유, DB open 없음): {len(orphans)}건")
    for kp in orphans:
        print(
            f"     [{kp['code']}] {kp.get('name','')} "
            f"qty={kp['quantity']}@{kp['avg_price']:,}"
        )

    closed = 0
    adopted = 0
    if apply:
        now_iso = datetime.now().isoformat()
        for d in ghosts:
            # pnl=NULL: 매도 체결가 미확인 ghost 는 손익 미집계.
            # pnl=0 으로 저장하면 통계(승률·평균손익)가 오염된다.
            conn.execute(
                "UPDATE positions SET status='closed', pnl=NULL, closed_at=? "
                "WHERE id=? AND status='open'",
                (now_iso, d["id"]),
            )
            audit_service.log_event(
                chat_id,
                "reconcile_ghost_close",
                {
                    "position_id": d["id"],
                    "code": d["code"],
                    "qty": d["quantity"],
                    "buy_price": d["buy_price"],
                    "reason": "KIS 잔고에 없음 — 미체결/외부청산 추정 (pnl=NULL)",
                },
            )
            closed += 1

        if orphans:
            # orphan INSERT — strategy_mode 는 사용자의 현재 모드. TP/SL 은 RiskGuard 로 계산.
            # positions.buy_order_id NOT NULL 이라 synthetic broker_orders row 먼저 INSERT 후
            # 그 id 를 buy_order_id 로 사용 (이전 버그: NULL 시도 → IntegrityError, 2026-05-04).
            from src.risk.guard import RiskGuard, StrategyMode
            from src import config as _cfg
            user = user_service.get_user(chat_id)
            default_mode = (user.strategy_mode if user else "bunt") or "bunt"
            holding = (user.holding_mode if user else "swing_week") or "swing_week"
            for kp in orphans:
                try:
                    mode_enum = StrategyMode(default_mode)
                    tp, sl = RiskGuard.compute_target_stop(
                        int(kp["avg_price"]), mode_enum, holding_mode=holding,
                    )
                except Exception:
                    tp, sl = 0, 0

                # synthetic audit_log + broker_orders (입양 마커)
                cur = conn.execute(
                    "INSERT INTO audit_log(chat_id, event_type, payload_json, ts) "
                    "VALUES (?, 'reconcile_orphan_adopt', '{}', ?)",
                    (chat_id, now_iso),
                )
                audit_id = cur.lastrowid
                cur = conn.execute(
                    """INSERT INTO broker_orders
                       (audit_id, trade_mode, side, code, quantity, price, status,
                        filled_quantity, filled_avg_price, broker_order_id,
                        commission, tax, created_at, updated_at)
                       VALUES (?, ?, 'buy', ?, ?, ?, 'filled', ?, ?, 'ORPHAN-ADOPT', 0, 0, ?, ?)""",
                    (audit_id, _cfg.TRADE_MODE.value, kp["code"],
                     int(kp["quantity"]), int(kp["avg_price"]),
                     int(kp["quantity"]), int(kp["avg_price"]),
                     now_iso, now_iso),
                )
                synthetic_buy_order_id = cur.lastrowid

                conn.execute(
                    """INSERT INTO positions
                       (chat_id, code, strategy_mode, buy_order_id, buy_price, quantity,
                        target_price, stop_price, status, opened_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
                    (chat_id, kp["code"], default_mode, synthetic_buy_order_id,
                     int(kp["avg_price"]), int(kp["quantity"]), tp, sl, now_iso),
                )
                audit_service.log_event(
                    chat_id,
                    "reconcile_orphan_adopt",
                    {
                        "code": kp["code"],
                        "qty": int(kp["quantity"]),
                        "avg_price": int(kp["avg_price"]),
                        "strategy_mode": default_mode,
                        "holding_mode": holding,
                        "synthetic_buy_order_id": synthetic_buy_order_id,
                        "reason": "KIS 잔고에 있는데 봇 DB open 없음 — 체결 누락 추정",
                    },
                )
                adopted += 1

        conn.commit()
        if closed:
            print(f"  → ghost {closed}건 status='closed', pnl=0 으로 정리")
        if adopted:
            print(f"  → orphan {adopted}건 봇 DB 에 INSERT (strategy_mode={default_mode})")

    return {
        "chat_id": chat_id,
        "db_open": len(open_db),
        "kis_held": len(kis_by_code),
        "matched": len(matched),
        "mismatched": len(mismatched),
        "ghosts": len(ghosts),
        "grace_protected": len(grace_protected),
        "pending_sell": len(pending_sell),
        "orphans": len(orphans),
        "closed": closed,
        "adopted": adopted,
    }


async def main_async(apply: bool, only_chat_id: int | None, grace_minutes: int) -> int:
    conn = get_connection()
    if only_chat_id is not None:
        chat_ids = [only_chat_id]
    else:
        chat_ids = [
            r["chat_id"]
            for r in conn.execute(
                "SELECT chat_id FROM bot_users WHERE status='approved' ORDER BY chat_id"
            )
        ]
    print(f"trade_mode: {config.TRADE_MODE.value} | apply={apply} | users={len(chat_ids)}")

    results = []
    for cid in chat_ids:
        r = await reconcile_chat(cid, apply=apply, grace_minutes=grace_minutes)
        results.append(r)

    print()
    print("=== 요약 ===")
    for r in results:
        if "error" in r:
            print(f"  chat_id={r['chat_id']} ERROR {r['error']}")
        else:
            print(
                f"  chat_id={r['chat_id']} "
                f"db_open={r['db_open']} kis={r['kis_held']} "
                f"matched={r['matched']} mismatch={r['mismatched']} "
                f"ghost={r['ghosts']} closed={r['closed']}"
            )
    if not apply and any(r.get("ghosts") for r in results):
        print("\n--apply 로 ghost 포지션 정리 가능")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="ghost 포지션 status='closed', pnl=0 으로 갱신")
    p.add_argument("--chat-id", type=int, default=None, help="단일 사용자만 (생략 시 승인된 전원)")
    p.add_argument("--grace-minutes", type=int, default=15,
                   help="이 분 이내 매수된 포지션은 ghost 판정 보류 (KIS 동기화 지연 보호). 기본 15분")
    args = p.parse_args()
    return asyncio.run(main_async(apply=args.apply, only_chat_id=args.chat_id, grace_minutes=args.grace_minutes))


if __name__ == "__main__":
    sys.exit(main())
