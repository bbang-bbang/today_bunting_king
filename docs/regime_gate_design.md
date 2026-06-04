# 레짐 게이트 설계 (v1 스펙) — 2026-06-04

> 상태: **설계만. 미배포.** 측정 루프([[project_measurement_loop]])가 down 레짐 칸을 더 확인하고
> 상승장 표본이 쌓인 뒤 활성화 검토. 지금은 섀도 모드 배포까지가 최대치.

## 배경
점수(ensemble_score)는 강세확인 가중평균이라 **하락장에서 가장 강하게 오르던 종목을 고득점화 → 상투/휩쏘**.
실측: 오늘(06-04) 레짐 = **down (breadth 16%)**, 오늘 추천 전부가 과거 손실 칸(60-63/down -3.97%·승률25%,
63-66/down -6.89%·0%, 66+/down -2.51%·0%)에 위치. 유일한 +칸(60-63/**up** +1.03%)은 오늘 해당 없음.
→ 진짜 레버는 점수 산식 튜닝이 아니라 **"깊은 하락장엔 추천 자체를 보류"**.

## 0. 목표 / 원칙
- 깊은 하락 레짐일 때 추천 보류(또는 축소).
- 측정 루프와 **동일한 breadth 레짐 재사용** → 외부 HTTP 0, 신호 일관성.
- **섀도 우선**: 배포해도 처음엔 행동 불변, 결정만 로깅 → 게이트가 도움됐을지 검증 후 활성.
- 게이트 결정도 **측정 가능**해야 함(차단해도 would-be 추천을 기록 → 반사실 비교).
- 점수 산식·가중치는 **건드리지 않음**.

## 1. 레짐 소스 (기존 방식 대체)
- `src.services.measurement.compute_regime(conn, today)` 재사용 → `breadth_pct`, `regime(up/side/down)`.
- 기존 `_check_market_regime`의 **pykrx 일간 -1.5% + fail-open**(외부의존·grind-down 통과 문제)을 대체.
  - pykrx 의존 제거 = 메모리 `feedback_calendar_no_external_http` 원칙 부합.

## 2. 게이트 정책 (열린 결정 — 활성화 전 택1)
- **A. 하드 보류**: `regime==down`(또는 breadth < FLOOR)이면 추천 0건. ← *초기 권고(단순)*
- B. 스케일다운: down이면 `top_n` 축소(10→3) 또는 `min_score` 임시 상향.
- C. 연속값: breadth<X 보류, X~Y 축소, Y+ 통과.
- 초기 권고: **A + breadth 임계 1개**, 단 섀도로.

## 3. 통합 지점
- `job_morning_recommend` 시작부, 현 `_check_market_regime(ctx.bot)` 호출 자리.
- 판정 → 통과/보류. 섀도 모드면 **로그만** 남기고 기존대로 추천 진행.

## 4. config 플래그 (토글·A/B)
```
REGIME_GATE_MODE        = off | shadow | active   # 기본 shadow 로 배포
REGIME_GATE_BREADTH_FLOOR = 30.0                   # 이하 = 보류 대상 (초기값, 측정으로 조정)
```
- `MARKET_DOWN_THRESHOLD_PCT`(pykrx)는 deprecate 경로로.

## 5. 측정 훅 (게이트 평가 가능하게) — **핵심**
게이트가 차단한 날도, "통과시켰다면 추천했을 종목"을 그대로 스코어링해 `signal_outcomes`에 기록하되
**`gated` 플래그(0/1)** 를 남긴다. → 나중에 "게이트 ON vs OFF" forward-return을 반사실로 비교 가능.
- 신규 컬럼: `signal_outcomes.gated INTEGER DEFAULT 0`, `gate_breadth REAL`.
- 섀도 모드에선 모든 추천이 실제 발송되며 gated=1로 "막았을 것"만 표시.

## 6. 롤아웃 단계
- **S1 (섀도 배포)**: 매일 "오늘이면 보류/통과 + breadth" 로깅. **행동 불변.** ← 다음에 할 일
- **S2 (검증, 1~2주 + up 레짐 ≥1회)**: 섀도 로그로 "게이트가 막았을 날들이 실제로 나빴나" 확인.
- **S3 (활성)**: 데이터가 지지하면 `active` 전환. 작은 임계부터.

## 7. 열린 결정 (활성화 전)
- 정책 A/B/C
- breadth FLOOR 값 (초기값일 뿐, 측정으로 조정)
- 하드 보류 시 사용자 메시지 발송 여부 (현 `_check_market_regime`은 "오늘 추천 보류" 발송)

## 8. 리스크
- **순환 표본**: 현재 거의 down뿐 → "down은 나쁘다"가 순환적. active 전환은 **up 표본 확보 후**.
- breadth FLOOR 자체가 또 다른 미검증 손잡이 → 섀도로 측정하며 정함.
- 섀도조차 측정 훅(5절)이 없으면 평가 불가 → 측정 훅이 S1의 필수 동반.
