"""분석 유니버스 빌더.

기준 (2026-04-22 결정):
  1. 시총 상위 500 (fundamentals_snapshot.market_cap DESC)
  2. 20일 평균 거래대금 ≥ 1,000,000,000원 (ohlcv_daily 최근 20거래일 avg(close*volume))
  3. ohlcv_daily 60거래일 이상 보유

CLI:
  python -m src.universe.builder                  # 오늘 기준 재빌드
  python -m src.universe.builder --as-of 2026-04-22
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime

from src.db.connection import get_connection

log = logging.getLogger("bunting.universe")

# 기본 파라미터
TOP_N = 500
MIN_ADV_20D = 1_000_000_000     # 10억원
MIN_OHLCV_DAYS = 60
ADV_WINDOW_DAYS = 20


def rebuild_universe(
    as_of: date | None = None,
    top_n: int = TOP_N,
    min_adv: int = MIN_ADV_20D,
    min_days: int = MIN_OHLCV_DAYS,
) -> int:
    """분석 유니버스 테이블 재빌드. 저장된 종목 수 반환.

    - 기존 레코드 전부 DELETE 후 INSERT (단일 트랜잭션, 원자적 교체)
    - 빌드 기준일(as_of) 미지정 시 오늘
    """
    if as_of is None:
        as_of = date.today()
    now = datetime.now().isoformat(timespec="seconds")

    conn = get_connection()
    try:
        # 60일+ 데이터 보유 종목
        rows_60 = conn.execute(
            """SELECT code FROM ohlcv_daily
               WHERE date <= ? GROUP BY code HAVING COUNT(*) >= ?""",
            (as_of.isoformat(), min_days),
        ).fetchall()
        codes_60 = {r[0] for r in rows_60}
        if not codes_60:
            log.warning("ohlcv_daily 에 60일+ 종목 없음 (as_of=%s)", as_of)
            return 0

        # 20일 평균 거래대금 (close*volume)
        placeholders = ",".join("?" for _ in codes_60)
        adv_rows = conn.execute(
            f"""SELECT code, AVG(close * volume) AS adv FROM (
                   SELECT code, close, volume FROM ohlcv_daily
                   WHERE code IN ({placeholders}) AND date <= ?
                   ORDER BY code, date DESC
               )
               GROUP BY code""",
            (*codes_60, as_of.isoformat()),
        ).fetchall()
        # 주의: 위 쿼리는 종목별 전체 평균이 됨 — 최근 20일만으로 재계산 필요.
        # 아래에서 per-code 로 최근 20일 avg 다시 뽑음 (정확성 우선)

        adv_by_code: dict[str, float] = {}
        for code in codes_60:
            rows = conn.execute(
                """SELECT close, volume FROM ohlcv_daily
                   WHERE code = ? AND date <= ?
                   ORDER BY date DESC LIMIT ?""",
                (code, as_of.isoformat(), ADV_WINDOW_DAYS),
            ).fetchall()
            if len(rows) < ADV_WINDOW_DAYS:
                continue
            adv = sum(r[0] * r[1] for r in rows) / ADV_WINDOW_DAYS
            if adv >= min_adv:
                adv_by_code[code] = adv

        if not adv_by_code:
            log.warning("거래대금 %s+ 종목 없음", f"{min_adv:,}")
            return 0

        # 시총 순위로 top_n 압축 (fundamentals_snapshot.market_cap)
        # fundamentals_snapshot 은 (code, snapshot_date) 키 — 종목당 여러 스냅샷.
        # GROUP BY code + MAX 로 dedup 안 하면 같은 종목이 여러 번 잡혀 INSERT 시 UNIQUE 충돌.
        codes_filtered = list(adv_by_code.keys())
        placeholders = ",".join("?" for _ in codes_filtered)
        cap_rows = conn.execute(
            f"""SELECT code, MAX(market_cap) AS market_cap FROM fundamentals_snapshot
                WHERE code IN ({placeholders}) AND market_cap > 0
                GROUP BY code
                ORDER BY market_cap DESC LIMIT ?""",
            (*codes_filtered, top_n),
        ).fetchall()

        if not cap_rows:
            log.warning("fundamentals_snapshot 매칭 0건 — 재무 수집 먼저 필요")
            return 0

        # 원자적 교체
        conn.execute("DELETE FROM analysis_universe")
        payload = [
            (code, int(cap), int(adv_by_code[code]), rank + 1, now)
            for rank, (code, cap) in enumerate(cap_rows)
        ]
        conn.executemany(
            """INSERT INTO analysis_universe
               (code, market_cap, adv_20d, rank, added_at)
               VALUES (?, ?, ?, ?, ?)""",
            payload,
        )
        conn.commit()

        log.info(
            "analysis_universe 재빌드 완료: %d 종목 (top_n=%d, min_adv=%s, min_days=%d)",
            len(payload), top_n, f"{min_adv:,}", min_days,
        )
        return len(payload)
    finally:
        conn.close()


def get_universe_codes(order_by: str = "rank") -> list[str]:
    """현재 저장된 유니버스 종목 코드 리스트 반환.

    order_by: 'rank' (시총 내림차순) | 'code' (사전식)
    """
    col = "rank ASC" if order_by == "rank" else "code ASC"
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT code FROM analysis_universe ORDER BY {col}"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def universe_size() -> int:
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) FROM analysis_universe").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _cli() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(description="분석 유니버스 재빌드")
    p.add_argument("--as-of", help="기준일 YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--top-n", type=int, default=TOP_N)
    p.add_argument("--min-adv", type=int, default=MIN_ADV_20D)
    p.add_argument("--min-days", type=int, default=MIN_OHLCV_DAYS)
    args = p.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    n = rebuild_universe(
        as_of=as_of, top_n=args.top_n, min_adv=args.min_adv, min_days=args.min_days
    )
    print(f"✓ analysis_universe: {n} 종목 저장")


if __name__ == "__main__":
    _cli()
