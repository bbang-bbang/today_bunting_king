"""투자자 수급 naver fallback 테스트.

pykrx 투자자 매매 API 가 KRX 빈 응답으로 죽은 환경(2026-04~) 대응.
naver frgn 페이지는 순매매'량'(주)을 주므로 종가로 순매수'대금'(원) 환산해야
FlowExpert 의 원 단위 임계값(20억/5억/1억)과 정합한다.
"""
from __future__ import annotations

from datetime import date

from src.crawlers import collect_all, fetch_investor_flow_naver


# ============================================================
# 순매매량(주) → 순매수대금(원) 환산
# ============================================================

def test_row_from_shares_converts_shares_to_won_amount():
    close = 292_500
    foreign_shares = -3_419_695
    inst_shares = -307_631
    row = fetch_investor_flow_naver._row_from_shares(
        date(2026, 5, 22), "005930", close, inst_shares, foreign_shares, 49.5,
    )
    # *_net 은 원(대금) — 종가 환산
    assert row["foreign_net"] == foreign_shares * close
    assert row["institution_net"] == inst_shares * close
    # 개인 = 잔차 (외인+기관 반대)
    assert row["individual_net"] == -(row["foreign_net"] + row["institution_net"])
    # 원본 주식수도 디버그용 보존
    assert row["foreign_shares"] == foreign_shares
    assert row["institution_shares"] == inst_shares


def test_row_from_shares_amount_reaches_won_scale_thresholds():
    """주식수 그대로면 1억(1e8) 임계값 미달이지만, 환산하면 원 스케일 도달."""
    close = 80_000
    foreign_shares = 500_000   # 50만주: 그대로면 5e5 < 1e8 → 사실상 0점
    row = fetch_investor_flow_naver._row_from_shares(
        date(2026, 5, 22), "000660", close, 0, foreign_shares, 0.0,
    )
    # 환산하면 400억원 → FlowExpert 최고 등급(20억+) 도달
    assert row["foreign_net"] == 40_000_000_000
    assert row["foreign_net"] >= 2_000_000_000


# ============================================================
# collect_all.step_investor_flow — naver 1순위 / pykrx fallback
# ============================================================

def test_step_investor_flow_uses_naver_when_codes_available(monkeypatch):
    monkeypatch.setattr(collect_all, "_get_universe_codes", lambda: ["005930", "000660"])
    monkeypatch.setattr(
        fetch_investor_flow_naver, "run_batch",
        lambda codes, pages=1, concurrency=1: 40,
    )
    # pykrx fallback 이 호출되면 실패 처리 (호출되면 안 됨)
    monkeypatch.setattr(
        collect_all, "_run_subprocess",
        lambda m, a: (_ for _ in ()).throw(AssertionError("pykrx fallback 호출됨")),
    )
    r = collect_all.step_investor_flow(first_time=False, end="2026-05-22")
    assert r.ok
    assert "naver" in r.label


def test_step_investor_flow_falls_back_to_pykrx_when_naver_zero(monkeypatch):
    """naver 0건 → pykrx subprocess fallback."""
    calls: list[str] = []
    monkeypatch.setattr(collect_all, "_get_universe_codes", lambda: ["005930"])
    monkeypatch.setattr(
        fetch_investor_flow_naver, "run_batch",
        lambda codes, pages=1, concurrency=1: 0,
    )
    monkeypatch.setattr(
        collect_all, "_run_subprocess",
        lambda m, a: (calls.append(m), (True, "ok"))[1],
    )
    r = collect_all.step_investor_flow(first_time=False, end="2026-05-22")
    assert "pykrx" in r.label
    assert calls == ["src.crawlers.fetch_investor_flow"]


def test_step_investor_flow_falls_back_on_naver_exception(monkeypatch):
    """naver 예외 → pykrx fallback (전체 잡이 죽지 않음)."""
    calls: list[str] = []
    monkeypatch.setattr(collect_all, "_get_universe_codes", lambda: ["005930"])

    def boom(codes, pages=1, concurrency=1):
        raise RuntimeError("naver 차단")

    monkeypatch.setattr(fetch_investor_flow_naver, "run_batch", boom)
    monkeypatch.setattr(
        collect_all, "_run_subprocess",
        lambda m, a: (calls.append(m), (True, "ok"))[1],
    )
    r = collect_all.step_investor_flow(first_time=False, end="2026-05-22")
    assert "pykrx" in r.label
    assert calls == ["src.crawlers.fetch_investor_flow"]
