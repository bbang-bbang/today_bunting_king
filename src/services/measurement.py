"""신호 성과 측정 루프 (2026-06-04).

추천 점수(ensemble_score)의 *실제* 예측력을 레짐·점수구간별로 계측한다.
스코어러/추천 로직은 건드리지 않는다 — 옆에 계측기만 단다.

설계 원칙:
  - 외부 HTTP 0: 레짐 라벨도 자체 OHLCV(20일선 breadth)로 산출.
  - 룩어헤드 차단: forward-return 은 진입일 *이후*, 레짐은 진입일 *이하* 데이터로만.
  - 멱등: backfill/forward_fill 재실행 안전 (UPSERT).
  - 신호 식별 = (session_date, code, strategy_mode). 다중 사용자로 중복 집계 안 함.

CLI:
  python -m src.services.measurement backfill   # 과거 추천 전체 소급 기록 + 레짐
  python -m src.services.measurement fill        # 성숙한 신호의 forward-return 채움 (일배치)
  python -m src.services.measurement report       # 캘리브레이션 리포트 출력
"""
from __future__ import annotations

import statistics as st
from datetime import datetime, timezone

from src.db.connection import get_connection

# 점수 구간 (추천은 min_score=60 이상만 존재 → 60+ 구간이 의미)
_BUCKETS = [(60, 63, "60-63"), (63, 66, "63-66"), (66, 1e9, "66+")]
# 레짐 임계 (breadth %: 종가>20일선 종목 비율). 초기값 — 튜닝 말고 기록만.
_REGIME_UP, _REGIME_DOWN = 60.0, 40.0
_FWD_DAYS = (5, 10)  # 측정 지평 (거래일)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bucket(score: float | None) -> str:
    if score is None:
        return "unknown"
    for lo, hi, label in _BUCKETS:
        if lo <= score < hi:
            return label
    return "<60" if score < 60 else "66+"


# ---------------------------------------------------------------------------
# 레짐 라벨 (자체 OHLCV breadth)
# ---------------------------------------------------------------------------
def _latest_trading_date(conn, date: str) -> str | None:
    r = conn.execute(
        "SELECT MAX(date) AS d FROM ohlcv_daily WHERE date <= ?", (date,)
    ).fetchone()
    return r["d"] if r and r["d"] else None


def compute_regime(conn, date: str) -> dict | None:
    """date 종가 기준, 전 종목 중 '종가 > 20일 SMA' 비율로 레짐 산출.

    룩어헤드 없음 — date 이하 데이터만 사용. 20봉 미만 종목은 제외.
    비거래일이 들어오면 직전 거래일 종가 기준으로 산출.
    """
    anchor = _latest_trading_date(conn, date)
    if anchor is None:
        return None
    date = anchor
    rows = conn.execute(
        """
        WITH win AS (
          SELECT code, date, close,
            AVG(close) OVER (PARTITION BY code ORDER BY date
                             ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS sma20,
            COUNT(*)   OVER (PARTITION BY code ORDER BY date
                             ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS n
          FROM ohlcv_daily
          WHERE date <= ? AND date > date(?, '-60 days')
        )
        SELECT close, sma20 FROM win WHERE date = ? AND n >= 20
        """,
        (date, date, date),
    ).fetchall()
    if not rows:
        return None
    above = sum(1 for r in rows if r["close"] > r["sma20"])
    total = len(rows)
    pct = 100.0 * above / total
    regime = "up" if pct >= _REGIME_UP else ("down" if pct < _REGIME_DOWN else "side")
    return {"breadth_pct": round(pct, 2), "n": total, "regime": regime}


def upsert_regime(conn, date: str) -> str | None:
    """date 의 레짐을 계산·저장하고 regime 문자열 반환 (이미 있으면 재사용)."""
    cached = conn.execute(
        "SELECT regime FROM regime_daily WHERE date = ?", (date,)
    ).fetchone()
    if cached:
        return cached["regime"]
    r = compute_regime(conn, date)
    if r is None:
        return None
    conn.execute(
        """INSERT OR REPLACE INTO regime_daily(date, breadth_pct, n_codes, regime, computed_at)
           VALUES (?,?,?,?,?)""",
        (date, r["breadth_pct"], r["n"], r["regime"], _now()),
    )
    return r["regime"]


# ---------------------------------------------------------------------------
# 가격 헬퍼
# ---------------------------------------------------------------------------
def _ref_close(conn, code: str, date: str) -> int | None:
    r = conn.execute(
        "SELECT close FROM ohlcv_daily WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
        (code, date),
    ).fetchone()
    return r["close"] if r else None


def _closes_after(conn, code: str, date: str, n: int) -> list[int]:
    rows = conn.execute(
        "SELECT close FROM ohlcv_daily WHERE code=? AND date>? ORDER BY date LIMIT ?",
        (code, date, n),
    ).fetchall()
    return [r["close"] for r in rows]


def _fwd_ret(conn, code: str, date: str, ndays: int, ref: int | None = None) -> float | None:
    """진입일(date) 종가 → +ndays 거래일 종가 수익률(%). 데이터 부족 시 None."""
    if ref is None:
        ref = _ref_close(conn, code, date)
    if not ref:
        return None
    cl = _closes_after(conn, code, date, ndays)
    if len(cl) < ndays:
        return None
    return round((cl[ndays - 1] - ref) / ref * 100, 3)


def _bench_5d(conn, date: str, universe: list[str]) -> float | None:
    """date 기준 유니버스 평균 +5거래일 수익률 — 상대성과 벤치마크."""
    vals = []
    for code in universe:
        v = _fwd_ret(conn, code, date, 5)
        if v is not None:
            vals.append(v)
    return round(st.mean(vals), 3) if vals else None


def _universe_codes(conn) -> list[str]:
    return [r["code"] for r in conn.execute("SELECT code FROM analysis_universe")]


# ---------------------------------------------------------------------------
# 백필 / 포워드필
# ---------------------------------------------------------------------------
def backfill(conn) -> dict:
    """과거 recommendations 전체를 signal_outcomes 로 소급 기록.

    신호 식별 = (session_date, code, strategy_mode) DISTINCT → 다중 사용자 중복 제거.
    """
    mkt = dict(conn.execute("SELECT code, market FROM instruments").fetchall())
    universe = _universe_codes(conn)
    sigs = conn.execute(
        """SELECT session_date, code, strategy_mode,
                  MIN(rec_id) AS rec_id, AVG(ensemble_score) AS score
           FROM recommendations
           GROUP BY session_date, code, strategy_mode"""
    ).fetchall()

    bench_cache: dict[str, float | None] = {}
    n_ins = 0
    for s in sigs:
        sd, code, mode = s["session_date"], s["code"], s["strategy_mode"]
        regime = upsert_regime(conn, sd)
        ref = _ref_close(conn, code, sd)
        r5 = _fwd_ret(conn, code, sd, 5, ref)
        r10 = _fwd_ret(conn, code, sd, 10, ref)
        if sd not in bench_cache:
            bench_cache[sd] = _bench_5d(conn, sd, universe)
        bench = bench_cache[sd]
        excess = round(r5 - bench, 3) if (r5 is not None and bench is not None) else None
        conn.execute(
            """INSERT INTO signal_outcomes(
                 session_date, code, strategy_mode, market, rec_id, ensemble_score,
                 score_bucket, ref_price, regime_at_entry, fwd_ret_5d, fwd_ret_10d,
                 bench_ret_5d, excess_ret_5d, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_date, code, strategy_mode) DO UPDATE SET
                 market=excluded.market, ensemble_score=excluded.ensemble_score,
                 score_bucket=excluded.score_bucket, ref_price=excluded.ref_price,
                 regime_at_entry=excluded.regime_at_entry, fwd_ret_5d=excluded.fwd_ret_5d,
                 fwd_ret_10d=excluded.fwd_ret_10d, bench_ret_5d=excluded.bench_ret_5d,
                 excess_ret_5d=excluded.excess_ret_5d, updated_at=excluded.updated_at""",
            (sd, code, mode, mkt.get(code), s["rec_id"], s["score"],
             _bucket(s["score"]), ref, regime, r5, r10, bench, excess, _now(), _now()),
        )
        n_ins += 1
    return {"signals": n_ins, "session_dates": len(bench_cache)}


def forward_fill(conn) -> int:
    """forward-return 이 비어있던 신호 중 이제 성숙한 것을 채움 (일배치용)."""
    universe = _universe_codes(conn)
    rows = conn.execute(
        """SELECT session_date, code, strategy_mode, ref_price
           FROM signal_outcomes
           WHERE fwd_ret_5d IS NULL OR fwd_ret_10d IS NULL OR bench_ret_5d IS NULL"""
    ).fetchall()
    bench_cache: dict[str, float | None] = {}
    n = 0
    for s in rows:
        sd, code = s["session_date"], s["code"]
        ref = s["ref_price"] or _ref_close(conn, code, sd)
        r5 = _fwd_ret(conn, code, sd, 5, ref)
        r10 = _fwd_ret(conn, code, sd, 10, ref)
        if sd not in bench_cache:
            bench_cache[sd] = _bench_5d(conn, sd, universe)
        bench = bench_cache[sd]
        excess = round(r5 - bench, 3) if (r5 is not None and bench is not None) else None
        conn.execute(
            """UPDATE signal_outcomes SET ref_price=?, fwd_ret_5d=?, fwd_ret_10d=?,
                 bench_ret_5d=?, excess_ret_5d=?, updated_at=?
               WHERE session_date=? AND code=? AND strategy_mode=?""",
            (ref, r5, r10, bench, excess, _now(), sd, code, s["strategy_mode"]),
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# 캘리브레이션 리포트
# ---------------------------------------------------------------------------
def calibration(conn, by_regime: bool = True) -> list[dict]:
    """score_bucket (× regime) 별 forward-return 집계. 유효N(고유종목수) 포함."""
    rows = conn.execute(
        "SELECT * FROM signal_outcomes WHERE fwd_ret_5d IS NOT NULL"
    ).fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        key = (r["score_bucket"], r["regime_at_entry"] if by_regime else "ALL")
        groups.setdefault(key, []).append(r)

    out = []
    for (bucket, regime), rs in sorted(groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        r5 = [x["fwd_ret_5d"] for x in rs]
        ex = [x["excess_ret_5d"] for x in rs if x["excess_ret_5d"] is not None]
        uniq = len({x["code"] for x in rs})
        out.append({
            "bucket": bucket, "regime": regime,
            "n": len(rs), "uniq_codes": uniq,
            "avg_ret_5d": round(st.mean(r5), 2),
            "median_ret_5d": round(st.median(r5), 2),
            "win_pct": round(100 * sum(1 for x in r5 if x > 0) / len(r5), 1),
            "avg_excess_5d": round(st.mean(ex), 2) if ex else None,
        })
    return out


def format_report(conn) -> str:
    lines = ["📊 신호 캘리브레이션 (5거래일 forward-return)", ""]
    total = conn.execute(
        "SELECT COUNT(*) n, SUM(fwd_ret_5d IS NOT NULL) mat FROM signal_outcomes"
    ).fetchone()
    lines.append(f"신호 {total['n']}건 (성숙 {total['mat']}건) · 레짐 {dict(conn.execute('SELECT regime,COUNT(*) FROM regime_daily GROUP BY regime').fetchall())}")
    lines.append("")
    lines.append(f"{'구간':>6} {'레짐':>5} {'N':>4} {'고유':>4} {'평균%':>7} {'중앙%':>7} {'승률%':>6} {'초과%':>7}")
    for row in calibration(conn, by_regime=True):
        ex = f"{row['avg_excess_5d']:+.2f}" if row["avg_excess_5d"] is not None else "  -  "
        flag = " ⚠집중" if row["uniq_codes"] * 2 < row["n"] else ""
        lines.append(
            f"{row['bucket']:>6} {row['regime']:>5} {row['n']:>4} {row['uniq_codes']:>4} "
            f"{row['avg_ret_5d']:>+7.2f} {row['median_ret_5d']:>+7.2f} {row['win_pct']:>6.1f} {ex:>7}{flag}"
        )
    lines.append("")
    lines.append("⚠집중 = 고유종목수 ≪ N (반복추천 → 유효표본 작음, 해석 주의)")
    return "\n".join(lines)


def _main():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    conn = get_connection()
    try:
        if cmd == "backfill":
            print("backfill:", backfill(conn))
            print("\n" + format_report(conn))
        elif cmd == "fill":
            print("forward_fill: 갱신", forward_fill(conn), "건")
        elif cmd == "report":
            print(format_report(conn))
        else:
            print(f"unknown cmd: {cmd} (backfill|fill|report)")
    finally:
        conn.close()


if __name__ == "__main__":
    _main()
