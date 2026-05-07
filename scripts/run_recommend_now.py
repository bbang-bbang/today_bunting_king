"""오늘 기준 추천 한 번 출력 (DB 저장 X)."""
from __future__ import annotations
import sys
from datetime import date

from src.db.connection import get_connection
from src.ensemble.recommender import recommend
from src import config


def main() -> int:
    conn = get_connection()
    codes = [r[0] for r in conn.execute("SELECT code FROM analysis_universe ORDER BY rank")]
    print(f"universe size: {len(codes)}")
    print(f"seed: {config.SEED_KRW:,} KRW")
    print(f"as_of: {date.today()}")
    print()

    for mode in ("bunt", "squeeze"):
        picks = recommend(
            codes=codes,
            active_seed_krw=config.SEED_KRW,
            mode=mode,
            top_n=5,
            min_score=config.RECOMMEND_MIN_SCORE,
        )
        print(f"=== {mode.upper()} mode — {len(picks)} pick(s) ===")
        for i, r in enumerate(picks, 1):
            op = r.opinion
            name = conn.execute(
                "SELECT name FROM instruments WHERE code=?", (op.code,)
            ).fetchone()
            nm = name[0] if name else op.code
            print(
                f"  {i}. [{op.code}] {nm} | 점수 {op.ensemble_score:.1f} | "
                f"진입 {r.last_close:,} × {r.estimated_quantity}주 = {r.order_value:,} | "
                f"목표 {r.target_price:,} / 손절 {r.stop_price:,}"
            )
            print(f"     {op.reason_summary}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
