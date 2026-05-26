"""통합 데이터 수집 파이프라인.

전 단계를 한 번에 실행 — 첫 수집(백필) 또는 일일 증분 모드.

사용:
  # 첫 수집 (수 시간 소요 — 3년 일봉 + 전 종목 per-code 데이터)
  python -m src.crawlers.collect_all --first-time

  # 일일 증분 (운영 모드, cron 권장)
  python -m src.crawlers.collect_all --daily

  # 테스트용: 상위 10개 종목만
  python -m src.crawlers.collect_all --daily --codes-limit 10

  # per-code 크롤러(뉴스·커뮤·유튜브) 건너뛰기
  python -m src.crawlers.collect_all --daily --skip-per-code

수집 단계:
  1. 종목 마스터 (instruments)
  2. 일봉 (ohlcv_daily)       — 증분 or 3년 백필
  3. 재무 스냅샷 (fundamentals_snapshot)
  4. 투자자 수급 (investor_flow)
  5. 뉴스 (news_article)        — per-code
  6. 커뮤니티 (community_post)  — per-code
  7. 유튜브 (youtube_video)     — per-code
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

from src.db.connection import get_connection, init_schema

log = logging.getLogger("bunting.collect_all")


def _detect_latest_krx_date() -> str | None:
    """KRX 실제 최신 영업일을 탐지. 시스템 시계가 미래로 설정된 환경 대비.

    리턴: 'YYYY-MM-DD' 또는 None(탐지 실패)
    """
    from datetime import date, timedelta
    try:
        from pykrx import stock
    except ImportError:
        return None
    end = date.today()
    start = end - timedelta(days=60)
    try:
        df = stock.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "005930",
        )
        if df is None or df.empty:
            return None
        latest = df.index[-1]
        return latest.strftime("%Y-%m-%d")
    except Exception as e:
        log.warning("KRX 최신일 탐지 실패: %s", e)
        return None


@dataclass
class StepResult:
    label: str
    ok: bool
    elapsed: float
    detail: str = ""


# ============================================================
# 단발 크롤러 — subprocess 호출 (에러 격리)
# ============================================================

def _run_subprocess(module: str, args: list[str]) -> tuple[bool, str]:
    cmd = [sys.executable, "-m", module] + args
    try:
        subprocess.run(cmd, check=True)
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, f"exit_code={e.returncode}"


def _seed_instruments_via_fdr() -> tuple[int, str]:
    """FDR StockListing 으로 KOSPI+KOSDAQ 종목 마스터 채우기.

    pykrx 의 시장 전체 API 가 깨진 환경 대응.
    """
    import FinanceDataReader as fdr
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")

    frames = []
    for market in ("KOSPI", "KOSDAQ"):
        df = fdr.StockListing(market)
        if df is None or df.empty:
            continue
        df = df[["Code", "Name", "Market"]].copy()
        df["Market"] = market   # FDR 의 Market 컬럼 표기 통일
        frames.append(df)

    if not frames:
        return 0, "FDR 응답 비어있음"

    conn = get_connection()
    try:
        total = 0
        for df in frames:
            rows = [
                (row["Code"], row["Name"], row["Market"], 1, now)
                for _, row in df.iterrows()
            ]
            conn.executemany(
                """INSERT INTO instruments (code, name, market, is_tradable, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                     name=excluded.name, market=excluded.market,
                     is_tradable=excluded.is_tradable, updated_at=excluded.updated_at""",
                rows,
            )
            total += len(rows)
        return total, f"{total}종목 upsert"
    finally:
        conn.close()


def step_instruments(as_of: str | None = None) -> StepResult:
    t = time.time()
    # 1순위: FDR (이 환경에서 안정적)
    try:
        n, detail = _seed_instruments_via_fdr()
        if n > 0:
            return StepResult("종목 마스터 (FDR)", True, time.time() - t, detail)
    except Exception as e:
        log.warning("FDR 기반 종목 마스터 실패, pykrx 로 fallback: %s", e)
    # fallback: pykrx (느리고 환경 의존)
    args = ["--as-of", as_of] if as_of else []
    ok, detail = _run_subprocess("src.crawlers.fetch_instruments", args)
    return StepResult("종목 마스터 (pykrx)", ok, time.time() - t, detail)


def step_ohlcv(
    first_time: bool, years: int, end: str | None = None,
    universe_only: bool = False,
) -> StepResult:
    """일봉 수집.

    universe_only=True (daily 권장): analysis_universe 종목만 증분 → 18분 → 3~4분.
    universe 가 비면 fetch_ohlcv 가 자동 fallback.
    first_time(백필) 모드에서는 universe 가 아직 없으므로 무시.
    """
    t = time.time()
    if first_time:
        args = ["--backfill", "--years", str(years)]
        if end:
            args += ["--end", end]
    else:
        args = ["--incremental"]
        if universe_only:
            args += ["--universe-only"]
    ok, detail = _run_subprocess("src.crawlers.fetch_ohlcv", args)
    suffix = " (universe)" if universe_only and not first_time else ""
    label = f"일봉 ({'백필 ' + str(years) + '년' if first_time else '증분'}{suffix})"
    return StepResult(label, ok, time.time() - t, detail)


def step_fundamentals(
    as_of: str | None = None,
    universe_only: bool = False,
) -> StepResult:
    """재무 스냅샷 — pykrx 1순위, 실패 시 naver fallback (KRX 인증 X 환경 대응).

    universe_only=True (daily 권장): naver fallback 시 analysis_universe 만 처리.
    """
    t = time.time()
    args = ["--as-of", as_of] if as_of else []
    ok, detail = _run_subprocess("src.crawlers.fetch_fundamentals", args)
    if ok:
        return StepResult("재무 스냅샷 (pykrx)", True, time.time() - t, detail)
    log.warning("재무 스냅샷 pykrx 실패 — naver fallback")
    fallback_args = ["--all", "--concurrency", "5"]
    if universe_only:
        fallback_args += ["--universe-only"]
    ok2, detail2 = _run_subprocess("src.crawlers.fetch_fundamentals_naver", fallback_args)
    suffix = " universe" if universe_only else ""
    label = f"재무 스냅샷 (naver{suffix} fallback)"
    return StepResult(label, ok2, time.time() - t, detail2 or detail)


def step_investor_flow(first_time: bool, end: str | None = None) -> StepResult:
    t = time.time()
    # 1순위: naver 스크래핑. pykrx 투자자 매매 API 가 KRX 빈 응답으로 죽어(2026-04~)
    # 시장전체·per-ticker 양쪽 다 0건 → naver frgn 페이지(순매매량→대금 환산)로 대체.
    try:
        from src.crawlers.fetch_investor_flow_naver import run_batch as _naver_batch
        codes = _get_universe_codes()
        if codes:
            # page 1(≈20영업일)이면 FlowExpert 5일 lookback 충족. 동시 6스레드.
            n = _naver_batch(codes, pages=1, concurrency=6)
            if n > 0:
                return StepResult(
                    "투자자 수급 (naver)", True, time.time() - t,
                    f"{n:,}건 / {len(codes)}종목",
                )
            log.warning("naver 수급 0건 — pykrx 로 fallback")
    except Exception as e:
        log.warning("naver 수급 수집 실패, pykrx 로 fallback: %s", e)

    # fallback: pykrx (현재 환경에선 0건 예상이나 환경 복구 대비 유지)
    from datetime import date, timedelta
    ref = date.fromisoformat(end) if end else date.today()
    if first_time:
        args = [
            "--from", (ref - timedelta(days=365)).isoformat(),
            "--to", ref.isoformat(),
        ]
    else:
        args = ["--as-of", ref.isoformat()]
    ok, detail = _run_subprocess("src.crawlers.fetch_investor_flow", args)
    return StepResult("투자자 수급 (pykrx)", ok, time.time() - t, detail)


# ============================================================
# per-code 크롤러 — 직접 import + 순차 호출 (subprocess 오버헤드 회피)
# ============================================================

def _get_universe_codes(limit: int | None = None) -> list[str]:
    """분석 유니버스(analysis_universe) 기준 코드 반환.

    유니버스가 비어있으면 fallback 으로 전 종목 반환 (+ 경고 로그).
    2026-04-22 B위원회 결정에 따라 per-code 수집은 유니버스만 대상으로 한다.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT code FROM analysis_universe ORDER BY rank ASC"
        ).fetchall()
        codes = [r[0] for r in rows]
    finally:
        conn.close()

    if not codes:
        log.warning(
            "analysis_universe 가 비어있음 — 유니버스 빌드 먼저 필요 "
            "(python -m src.universe.builder). 일단 전 종목 fallback."
        )
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT code FROM instruments WHERE is_tradable = 1 ORDER BY code"
            ).fetchall()
            codes = [r[0] for r in rows]
        finally:
            conn.close()

    return codes[:limit] if limit else codes


def step_rebuild_universe(as_of: str | None = None) -> StepResult:
    """분석 유니버스 재빌드 (ohlcv·재무 수집 직후 실행)."""
    t = time.time()
    from datetime import date as _date
    from src.universe.builder import rebuild_universe
    as_of_dt = _date.fromisoformat(as_of) if as_of else None
    try:
        n = rebuild_universe(as_of=as_of_dt)
        detail = f"{n} 종목 선정 (top 500 & ADV 10억+ & 60일+)"
        return StepResult("분석 유니버스", n > 0, time.time() - t, detail)
    except Exception as e:
        return StepResult("분석 유니버스", False, time.time() - t, str(e))


def _run_per_code(
    label: str,
    runner: Callable[[str, int], int],
    codes: list[str],
    days: int,
) -> StepResult:
    t = time.time()
    total = 0
    errors = 0
    for i, code in enumerate(codes, 1):
        try:
            total += runner(code, days)
        except Exception as e:
            errors += 1
            log.warning("[%s] %s 실패: %s", label, code, e)
        if i % 50 == 0:
            log.info("[%s] 진행 %d/%d (누적 %d건)", label, i, len(codes), total)
    detail = f"{total}건 수집, {errors}건 실패"
    return StepResult(label, errors == 0, time.time() - t, detail)


def step_news(codes: list[str], days: int) -> StepResult:
    from src.crawlers.fetch_news import run
    return _run_per_code("뉴스", run, codes, days)


def step_community(codes: list[str], days: int) -> StepResult:
    from src.crawlers.fetch_community import run
    return _run_per_code("커뮤니티", run, codes, days)


def step_youtube(codes: list[str], days: int) -> StepResult:
    from src.crawlers.fetch_youtube import run
    return _run_per_code("유튜브", run, codes, days)


# ============================================================
# 파이프라인
# ============================================================

def run_pipeline(
    *,
    first_time: bool,
    years: int,
    codes_limit: int | None,
    skip_per_code: bool,
    per_code_days: int,
    continue_on_error: bool,
    as_of_override: str | None = None,
    skip_fundamentals: bool = False,
    skip_investor_flow: bool = False,
    on_step_done: Callable[[StepResult], None] | None = None,
) -> list[StepResult]:
    init_schema()
    results: list[StepResult] = []

    def _record(r: StepResult) -> bool:
        results.append(r)
        status = "✅" if r.ok else "❌"
        print(f"{status} {r.label:20}  {r.elapsed:6.1f}s  {r.detail}")
        if on_step_done is not None:
            try:
                on_step_done(r)
            except Exception:
                log.exception("on_step_done 콜백 실패 (무시)")
        return r.ok or continue_on_error

    # KRX 실제 최신 영업일 탐지 (시스템 시계가 미래로 설정된 환경 대비)
    effective_date = as_of_override
    if effective_date is None:
        print("🔎 KRX 최신 영업일 탐지 중...")
        effective_date = _detect_latest_krx_date()
        if effective_date:
            print(f"   → 탐지: {effective_date}")
        else:
            print("   → 탐지 실패, 시스템 date.today() 사용")

    # 1) 단발 수집 — instruments 는 다음 단계 FK 전제이므로 실패 시 중단
    if not _record(step_instruments(as_of=effective_date)):
        return results

    # 2) 일봉 (pykrx 개별 쿼리, 이 환경에서 동작)
    # daily 증분은 분석 유니버스만 갱신 (전 종목은 추천에 사용 안 됨, 18분→3~4분)
    if not _record(step_ohlcv(
        first_time, years, end=effective_date,
        universe_only=not first_time,
    )):
        if not continue_on_error:
            return results

    # 3) 재무 — daily 일 때 naver fallback 도 universe-only
    if skip_fundamentals:
        print("· 재무 스냅샷 건너뜀 (--skip-fundamentals)")
    else:
        if not _record(step_fundamentals(
            as_of=effective_date, universe_only=not first_time,
        )):
            if not continue_on_error:
                return results

    # 4) 투자자 수급
    if skip_investor_flow:
        print("· 투자자 수급 건너뜀 (--skip-investor-flow)")
    else:
        if not _record(step_investor_flow(first_time, end=effective_date)):
            if not continue_on_error:
                return results

    # 4.5) 분석 유니버스 재빌드 (per-code 직전에 최신화)
    if not _record(step_rebuild_universe(as_of=effective_date)):
        if not continue_on_error:
            return results

    if skip_per_code:
        print("· per-code 단계 건너뜀 (--skip-per-code)")
        return results

    # 5~7) per-code 단계 — 분석 유니버스만 대상
    codes = _get_universe_codes(codes_limit)
    print(f"\n📦 per-code 대상 종목 {len(codes)}개 (days={per_code_days})")
    for step_fn in (
        lambda: step_news(codes, per_code_days),
        lambda: step_community(codes, per_code_days),
        lambda: step_youtube(codes, per_code_days),
    ):
        if not _record(step_fn()):
            if not continue_on_error:
                return results

    return results


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser()
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--first-time", action="store_true",
                      help="첫 수집: 3년 백필 + 1년 수급 + 전 종목 per-code")
    mode.add_argument("--daily", action="store_true",
                      help="일일 증분: 신규 일봉 + 오늘 수급 + 최근 1일 per-code")
    p.add_argument("--years", type=int, default=3,
                   help="백필 연수 (--first-time 전용, 기본 3)")
    p.add_argument("--codes-limit", type=int, default=None,
                   help="per-code 대상 종목 상한 (테스트용)")
    p.add_argument("--skip-per-code", action="store_true",
                   help="뉴스·커뮤·유튜브 건너뛰기 (빠른 일일 갱신)")
    p.add_argument("--skip-fundamentals", action="store_true",
                   help="재무 스냅샷 건너뛰기 (pykrx market API 미가용 환경)")
    p.add_argument("--skip-investor-flow", action="store_true",
                   help="투자자 수급 건너뛰기 (pykrx market API 미가용 환경)")
    p.add_argument("--per-code-days", type=int, default=None,
                   help="per-code 조회 일수. 미지정 시 first-time=30, daily=1")
    p.add_argument("--continue-on-error", action="store_true",
                   help="단계 실패해도 다음 단계 계속")
    p.add_argument("--as-of", type=str, default=None,
                   help="기준일 수동 지정 (YYYY-MM-DD). 미지정 시 KRX 최신일 자동 탐지.")
    args = p.parse_args()

    per_code_days = args.per_code_days
    if per_code_days is None:
        per_code_days = 30 if args.first_time else 1

    print(f"🚀 {'첫 수집' if args.first_time else '일일 증분'} 모드 시작")
    t0 = time.time()

    results = run_pipeline(
        first_time=args.first_time,
        years=args.years,
        codes_limit=args.codes_limit,
        skip_per_code=args.skip_per_code,
        per_code_days=per_code_days,
        continue_on_error=args.continue_on_error,
        as_of_override=args.as_of,
        skip_fundamentals=args.skip_fundamentals,
        skip_investor_flow=args.skip_investor_flow,
    )

    elapsed = time.time() - t0
    n_fail = sum(1 for r in results if not r.ok)
    print(f"\n{'='*60}")
    print(f"총 소요 {elapsed:.1f}s · 성공 {len(results)-n_fail}/{len(results)}")
    if n_fail:
        print("❌ 일부 단계 실패 — 로그 확인 후 해당 크롤러 직접 재실행 권장")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
