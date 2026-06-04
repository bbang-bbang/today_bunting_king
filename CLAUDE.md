# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

내일은 번트왕 — 텔레그램 기반 국내주식 주간스윙 추천·실행 봇. 7-전문가 앙상블이 종목을 채점·추천하고, 사용자가 버튼으로 매수/매도하면 KIS API로 체결한다. 별도 코인 봇(`src/coin/`)이 같은 레포에 공존한다.

## 명령어

```bash
# 테스트
python -m pytest tests/ -q                          # 전체
python -m pytest tests/test_ensemble.py -q          # 단일 파일
python -m pytest tests/test_ensemble.py::test_xxx   # 단일 테스트
# (테스트는 async 를 @pytest.mark.asyncio 대신 대부분 asyncio.run() 으로 직접 감쌈)

# 실행 (모듈 형태로만 실행 — src 는 패키지)
python -m src.bot.telegram_bot          # KR 봇 기동 (스케줄러 포함)
python -m src.coin.telegram_bot         # 코인 봇 기동 (독립)
python -m src.universe.builder          # analysis_universe 재빌드 (--as-of YYYY-MM-DD)
python -m src.crawlers.collect_all --daily --continue-on-error   # 일일 증분 수집
python -m src.crawlers.collect_all --first-time                  # 3년 백필 (수 시간)
python -m src.db.connection             # 스키마 적용 (idempotent, 신규 테이블 생성)
python -m src.services.measurement report|backfill|fill          # 신호 성과 측정
python -m src.backtest.cli              # 백테스트
```

별도 lint/format 설정 파일은 없다(ruff 캐시만 gitignore). 의존성은 `requirements.txt` (Python 3.12 권장 — 운영 서버 기준).

## 아키텍처 (큰 그림)

**파이프라인 = 공유 유니버스 위의 단방향 흐름.** 크롤링·분석·추천이 **모두 같은 `analysis_universe` 테이블(시총 top 500)** 을 참조한다(불변 규칙). 흐름:

```
crawlers/ (OHLCV·재무·수급·뉴스·커뮤니티·유튜브)  →  ohlcv_daily 등 시계열 테이블
        ↓ (주 1회 일 23:00 재빌드)
universe/builder.py  →  analysis_universe (500종목)
        ↓
ensemble/recommender.py → ensemble/scorer.py (7전문가 가중합)  →  recommendations 테이블
        ↓
bot/scheduler.py (잡들)  →  telegram_bot.py (버튼)  →  adapters/ (KIS REST)  →  broker_orders / positions
```

**`src/bot/scheduler.py` 가 시스템의 척추다.** APScheduler(telegram JobQueue)로 등록된 잡들이 거의 모든 자동 동작을 담당한다: 아침 데이터 갱신(07:30)·추천(08:00)·파이프라인 헬스체크(08:05)·신호 측정(08:10)·장중 가격 모니터(3분)·매수/매도 폴링·EOD 청산 리마인더·주간 유니버스 재빌드. 새 주기적 동작을 추가하려면 여기에 잡 함수 + `jq.run_daily(...)` 등록을 더한다.

**앙상블 스코어러(`src/ensemble/scorer.py`)는 7개 전문가(기술·재무·흐름·뉴스·분봉·커뮤니티·유튜브) 점수의 가중평균을 0~100으로 정규화한 값이다.** 모드별 가중치가 다르다(번트=재무↑, 스퀴즈=기술/분봉↑). **중요: 이 점수는 미래 수익률에 캘리브레이션된 적이 없다 — "60점=수익확률 60%"가 아니다.** 일부 전문가가 데이터 결손으로 실패하면 유효 전문가만으로 가중치를 재정규화한다(fallback). 재무 `is_acceptable()=False`는 하드 필터(즉시 탈락).

**`recommend()` 는 무겁다**(500종목 ~2-3분). event loop 차단 방지를 위해 `asyncio.to_thread`로 번트/스퀴즈 병렬 실행하고, 결과를 `recommendations`에 저장해 같은 `session_date` 재요청 시 캐시 반환한다.

**신호 성과 측정 루프(`src/services/measurement.py`)** 는 스코어러를 건드리지 않는 계측기다. 추천 신호를 `signal_outcomes`(점수구간×레짐별 forward-return)에 기록한다. 레짐은 외부 API 없이 자체 OHLCV의 20일선 breadth로 산출. 신호 식별 = `(session_date, code, strategy_mode)`.

**DB:** SQLite (`data/bunting.db`). 스키마는 `src/db/schema.sql`(전부 `CREATE TABLE IF NOT EXISTS`) + `connection.py::_run_migrations`(컬럼 추가는 여기에 idempotent하게). `get_connection()`은 autocommit + WAL + FK on.

**TRADE_MODE** = `paper`(외부호출 없는 DB 시뮬) | `kis_mock`(KIS 모의 — 실제 KIS 서버 호출하나 체결 없음) | `live`(실거래). 현재 운영은 `kis_mock`.

## 깨지기 쉬운 외부 의존 (사고 이력 기반)

- **pykrx 는 KRX에서 빈 응답을 자주 반환** → `JSONDecodeError`/빈 DataFrame. 종목마스터는 FDR(`finance-datareader`), 재무·수급은 naver finance fallback으로 우회한다. 새 데이터 경로 추가 시 pykrx 단독 의존 금지.
- **거래일/공휴일 판정에 외부 HTTP 사용 금지** — `holidays` 라이브러리를 쓴다. (pykrx 빈 응답을 휴장으로 오판한 사고 다수.)
- **포지션 정합성의 진실은 "잔고"** — broker_orders pending/daily-ccld 가 부정확할 수 있다(좀비 pending 사고). 정합성 진단은 KIS 실잔고 기준.

## 불변 규칙 (USAGE.md §14 — 바꾸지 말 것)

1. **시드 상한 100만원** (모의 확장 상한 1,000만원). 코드 상수로 고정, 초과 입력은 시작 단계에서 거부.
2. **당일/주중 청산** — 국내 스윙은 월~금 내 청산, 오버주말 금지.
3. **번트 리스크 ≤ 스퀴즈** (TP/SL: 번트 +7%/-4%, 스퀴즈 +12%/-5%).
4. **실거래(`live`) 전환은 사용자 명시 허락 후에만.**
5. **테스트 주문 금지** — 분석 파이프라인이 뽑은 실제 종목만 체결.
6. **크롤=분석=추천이 동일 `analysis_universe`** 공유.

## 운영 / 배포

- 운영 서버: 가비아 `ssh -i today-project.pem rocky@1.201.126.200`, 봇 경로 `/home/rocky/bunting/`, systemd `bunting.service`(KR) / `coin-bunting.service`(코인). **DB는 서버에만 존재** — 분석/진단은 서버 DB에 직접 한다.
- 배포는 변경 파일을 scp 후 `sudo systemctl restart bunting.service`. 배포 전후 로컬↔서버 diff로 드리프트 확인 권장.
- `.env`·`today-project.pem`은 git에 없다(`.gitignore`). 멀티 PC 세팅은 README "다른 PC에서 작업하기" 참고.

## 테스트 주의

요일/시각 의존으로 내 변경과 무관하게 항상 실패하는 테스트가 있다(예: `test_daily_sell_report` 계열, `test_cb_button` 일부). 실패 시 시간 의존인지부터 확인할 것.
