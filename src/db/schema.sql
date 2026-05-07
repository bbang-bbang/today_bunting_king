-- ============================================================
-- 내일은 번트왕 DB 스키마 (SQLite)
-- 2026-04-15 | B2 DB 엔지니어
-- 원칙:
--   * audit_log 는 append-only (트리거로 UPDATE/DELETE 차단)
--   * 모든 시계열 테이블은 (식별자, 날짜) 복합 PK
--   * 금액/수량은 정수(원/주/개). Decimal 은 Python 측에서 처리.
-- ============================================================

PRAGMA foreign_keys = ON;

-- ============================================================
-- 종목 마스터
-- ============================================================
CREATE TABLE IF NOT EXISTS instruments (
  code         TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  market       TEXT NOT NULL CHECK(market IN ('KOSPI','KOSDAQ')),
  sector       TEXT,
  is_tradable  INTEGER NOT NULL DEFAULT 1,
  updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_instruments_market ON instruments(market);

-- ============================================================
-- 일봉 OHLCV (수집 대상: 3년 백필)
-- ============================================================
CREATE TABLE IF NOT EXISTS ohlcv_daily (
  code           TEXT NOT NULL,
  date           TEXT NOT NULL,         -- 'YYYY-MM-DD'
  open           INTEGER NOT NULL,
  high           INTEGER NOT NULL,
  low            INTEGER NOT NULL,
  close          INTEGER NOT NULL,
  volume         INTEGER NOT NULL,
  value          INTEGER,               -- 거래대금 (일부 소스 미제공)
  change_pct     REAL,                  -- 전일 대비 등락률 (%)
  PRIMARY KEY (code, date),
  FOREIGN KEY (code) REFERENCES instruments(code)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON ohlcv_daily(date);

-- ============================================================
-- 재무 스냅샷
-- ============================================================
CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
  code           TEXT NOT NULL,
  snapshot_date  TEXT NOT NULL,
  market_cap     INTEGER,
  per            REAL,
  pbr            REAL,
  roe            REAL,
  debt_ratio     REAL,
  is_warning     INTEGER DEFAULT 0,     -- 관리종목 여부
  is_watch       INTEGER DEFAULT 0,     -- 투자주의 여부
  source         TEXT,
  PRIMARY KEY (code, snapshot_date),
  FOREIGN KEY (code) REFERENCES instruments(code)
);

-- ============================================================
-- 투자자별 순매수 (흐름 전문가용)
-- ============================================================
CREATE TABLE IF NOT EXISTS investor_flow (
  date            TEXT NOT NULL,
  code            TEXT NOT NULL,
  foreign_net     INTEGER,            -- 외국인 순매수대금 (원)
  institution_net INTEGER,            -- 기관합계 순매수대금 (원)
  individual_net  INTEGER,            -- 개인 순매수대금 (원)
  PRIMARY KEY (date, code),
  FOREIGN KEY (code) REFERENCES instruments(code)
);

CREATE INDEX IF NOT EXISTS idx_flow_code_date ON investor_flow(code, date);

-- ============================================================
-- 뉴스 기사 (뉴스 전문가용, 네이버 금융 뉴스 스크래핑)
-- ============================================================
CREATE TABLE IF NOT EXISTS news_article (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  code              TEXT NOT NULL,
  published_at      TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM'
  title             TEXT NOT NULL,
  summary           TEXT,
  url               TEXT NOT NULL,
  press             TEXT,
  sentiment_score   REAL,                   -- -1.0 ~ +1.0
  sentiment_label   TEXT CHECK(sentiment_label IN ('positive','negative','neutral')),
  fetched_at        TEXT NOT NULL,
  UNIQUE(code, url),
  FOREIGN KEY (code) REFERENCES instruments(code)
);

CREATE INDEX IF NOT EXISTS idx_news_code_date ON news_article(code, published_at);

-- ============================================================
-- 수집 로그 (증분 cursor + 재현성 해시)
-- ============================================================
CREATE TABLE IF NOT EXISTS ingest_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  source         TEXT NOT NULL,         -- 'kis_daily_ohlcv','kis_fundamentals' ...
  last_date      TEXT,
  row_count      INTEGER,
  snapshot_hash  TEXT,                  -- 수집 배치 완료 시점 해시
  status         TEXT NOT NULL CHECK(status IN ('success','fail','running')),
  error          TEXT,
  started_at     TEXT NOT NULL,
  finished_at    TEXT
);

-- ============================================================
-- 봇 사용자 (화이트리스트)
-- ============================================================
CREATE TABLE IF NOT EXISTS bot_users (
  chat_id        INTEGER PRIMARY KEY,
  status         TEXT NOT NULL CHECK(status IN ('pending','approved','blocked')),
  pin_hash       TEXT,
  trade_mode     TEXT NOT NULL DEFAULT 'paper'
                 CHECK(trade_mode IN ('paper','kis_mock','live')),
  strategy_mode  TEXT NOT NULL DEFAULT 'bunt'
                 CHECK(strategy_mode IN ('bunt','squeeze')),
  holding_mode   TEXT NOT NULL DEFAULT 'swing_week'
                 CHECK(holding_mode IN ('day','swing_week')),
  early_take_profit INTEGER NOT NULL DEFAULT 0,
  registered_at  TEXT NOT NULL,
  approved_at    TEXT
);

-- ============================================================
-- 주문 확인 대기 (10분 TTL + idempotency UUID)
-- ============================================================
CREATE TABLE IF NOT EXISTS pending_confirmations (
  uuid         TEXT PRIMARY KEY,
  chat_id      INTEGER NOT NULL,
  intent_json  TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  consumed     INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (chat_id) REFERENCES bot_users(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_expires ON pending_confirmations(expires_at);

-- ============================================================
-- 감사 로그 (append-only)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id       INTEGER,
  event_type    TEXT NOT NULL,         -- 'command','button','order_req','order_res','guard_block',...
  payload_json  TEXT NOT NULL,
  ts            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_chat_ts ON audit_log(chat_id, ts);

-- audit_log 는 append-only
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only');
END;

-- ============================================================
-- 브로커 주문 (KIS 요청/응답 페어)
-- ============================================================
CREATE TABLE IF NOT EXISTS broker_orders (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  audit_id          INTEGER NOT NULL,
  trade_mode        TEXT NOT NULL,
  side              TEXT NOT NULL CHECK(side IN ('buy','sell')),
  code              TEXT NOT NULL,
  quantity          INTEGER NOT NULL,
  price             INTEGER,              -- NULL = 시장가
  kis_req_json      TEXT,
  kis_res_json      TEXT,
  broker_order_id   TEXT,
  status            TEXT NOT NULL CHECK(status IN ('pending','filled','partial','failed','cancelled')),
  filled_quantity   INTEGER DEFAULT 0,
  filled_avg_price  INTEGER,
  commission        INTEGER DEFAULT 0,
  tax               INTEGER DEFAULT 0,
  error             TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  FOREIGN KEY (audit_id) REFERENCES audit_log(id)
);

CREATE INDEX IF NOT EXISTS idx_broker_orders_code   ON broker_orders(code);
CREATE INDEX IF NOT EXISTS idx_broker_orders_status ON broker_orders(status);

-- ============================================================
-- 포지션 (당일 매수·매도 페어, 번트/스퀴즈 태깅)
-- ============================================================
CREATE TABLE IF NOT EXISTS positions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id         INTEGER NOT NULL,
  code            TEXT NOT NULL,
  strategy_mode   TEXT NOT NULL CHECK(strategy_mode IN ('bunt','squeeze')),
  buy_order_id    INTEGER NOT NULL,
  sell_order_id   INTEGER,
  buy_price       INTEGER NOT NULL,
  quantity        INTEGER NOT NULL,
  target_price    INTEGER NOT NULL,     -- 익절가
  stop_price      INTEGER NOT NULL,     -- 손절가
  status          TEXT NOT NULL CHECK(status IN ('open','closed')),
  pnl             INTEGER,
  opened_at       TEXT NOT NULL,
  closed_at       TEXT,
  FOREIGN KEY (chat_id)       REFERENCES bot_users(chat_id),
  FOREIGN KEY (buy_order_id)  REFERENCES broker_orders(id),
  FOREIGN KEY (sell_order_id) REFERENCES broker_orders(id)
);

CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(status, chat_id);

-- ============================================================
-- 분봉 OHLCV (분봉 전문가용, 당일 장중 수집)
-- ============================================================
CREATE TABLE IF NOT EXISTS ohlcv_minute (
  code      TEXT NOT NULL,
  datetime  TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM'
  open      INTEGER NOT NULL,
  high      INTEGER NOT NULL,
  low       INTEGER NOT NULL,
  close     INTEGER NOT NULL,
  volume    INTEGER NOT NULL,
  PRIMARY KEY (code, datetime),
  FOREIGN KEY (code) REFERENCES instruments(code)
);

CREATE INDEX IF NOT EXISTS idx_minute_code_dt ON ohlcv_minute(code, datetime);

-- ============================================================
-- 커뮤니티 게시글 (커뮤니티 전문가용)
-- ============================================================
CREATE TABLE IF NOT EXISTS community_post (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  code            TEXT NOT NULL,
  source          TEXT NOT NULL CHECK(source IN ('naver','stockplus')),
  posted_at       TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM'
  title           TEXT NOT NULL,
  view_count      INTEGER,
  comment_count   INTEGER,
  sentiment_score REAL,
  sentiment_label TEXT CHECK(sentiment_label IN ('positive','negative','neutral')),
  fetched_at      TEXT NOT NULL,
  UNIQUE(code, source, title, posted_at),
  FOREIGN KEY (code) REFERENCES instruments(code)
);

CREATE INDEX IF NOT EXISTS idx_community_code_dt ON community_post(code, posted_at);

-- ============================================================
-- 유튜브 영상 (유튜브 전문가용)
-- ============================================================
CREATE TABLE IF NOT EXISTS youtube_video (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  code            TEXT NOT NULL,
  video_id        TEXT NOT NULL,
  title           TEXT NOT NULL,
  channel         TEXT,
  upload_date     TEXT NOT NULL,   -- 'YYYY-MM-DD'
  view_count      INTEGER,
  like_count      INTEGER,
  duration        INTEGER,         -- 초
  sentiment_score REAL,
  sentiment_label TEXT CHECK(sentiment_label IN ('positive','negative','neutral')),
  fetched_at      TEXT NOT NULL,
  UNIQUE(code, video_id),
  FOREIGN KEY (code) REFERENCES instruments(code)
);

CREATE INDEX IF NOT EXISTS idx_youtube_code_date ON youtube_video(code, upload_date);

-- ============================================================
-- 추천 로그 (PM 산출물 기록)
-- rec_id 규칙: {MARKET}-{YYYYMMDD}-{NN}  (예: KR-20260416-01)
-- 행위 로그(recommendation_actions)와 rec_id로 조인 → 회고·승률 집계 기반
-- ============================================================
CREATE TABLE IF NOT EXISTS recommendations (
  rec_id              TEXT PRIMARY KEY,
  chat_id             INTEGER NOT NULL,
  session_date        TEXT NOT NULL,            -- 'YYYY-MM-DD' (발송 세션 기준)
  market              TEXT NOT NULL CHECK(market IN ('KR','US','CR')),
  code                TEXT NOT NULL,
  name                TEXT NOT NULL,
  strategy_mode       TEXT NOT NULL CHECK(strategy_mode IN ('bunt','squeeze')),
  entry_price         INTEGER NOT NULL,
  target_price        INTEGER NOT NULL,
  stop_price          INTEGER NOT NULL,
  expected_return_pct REAL NOT NULL,
  ensemble_score      REAL,
  reason_summary      TEXT NOT NULL,            -- 5인 관점 종합 (메시지 본문에 노출)
  reason_json         TEXT,                     -- 전문가별 개별 점수/근거 직렬화
  sent_at             TEXT NOT NULL,            -- 텔레그램 발송 시각
  FOREIGN KEY (chat_id) REFERENCES bot_users(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_recommendations_session ON recommendations(session_date, chat_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_code    ON recommendations(code);

-- ============================================================
-- 추천 회신 행위 로그 (매수함 / 건너뜀 / 매도)
-- 사유 태그 (action_type별 허용값은 애플리케이션 레이어에서 검증):
--   bought  : trust_ensemble | intuition | news | other
--   skipped : low_trust | no_cash | missed_timing | other
--   sold    : target_hit | stop_hit | eod_forced | impulsive | news_change | other
-- ============================================================
CREATE TABLE IF NOT EXISTS recommendation_actions (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  rec_id              TEXT NOT NULL,
  chat_id             INTEGER NOT NULL,
  action_type         TEXT NOT NULL CHECK(action_type IN ('bought','skipped','sold')),
  price               INTEGER,                  -- 매수·매도 시. 건너뜀이면 NULL
  quantity            INTEGER,
  reason_tag          TEXT NOT NULL,
  reason_text         TEXT,
  realized_pnl        INTEGER,                  -- 매도 시점에만
  realized_return_pct REAL,                     -- 매도 시점에만
  acted_at            TEXT NOT NULL,
  FOREIGN KEY (rec_id)  REFERENCES recommendations(rec_id),
  FOREIGN KEY (chat_id) REFERENCES bot_users(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_rec_actions_rec   ON recommendation_actions(rec_id);
CREATE INDEX IF NOT EXISTS idx_rec_actions_type  ON recommendation_actions(action_type, acted_at);
CREATE INDEX IF NOT EXISTS idx_rec_actions_chat  ON recommendation_actions(chat_id, acted_at);

-- ============================================================
-- 분석 유니버스 (2026-04-22 B위원회 결정)
-- 크롤러·스케줄러·추천 엔진이 공통 참조하는 분석 대상 종목 집합.
-- 기준: 시총 상위 500 AND 20일 평균거래대금 10억+ AND ohlcv 60일+
-- 갱신: 일요일 23:00 KST 주 1회 (job_rebuild_universe)
-- ============================================================
CREATE TABLE IF NOT EXISTS analysis_universe (
  code        TEXT PRIMARY KEY,
  market_cap  INTEGER NOT NULL,           -- 빌드 시점 시총 (원)
  adv_20d     INTEGER,                    -- 20일 평균 거래대금 (원)
  rank        INTEGER NOT NULL,           -- 시총 순위 (1=최대)
  added_at    TEXT NOT NULL,              -- ISO timestamp (빌드 시점)
  FOREIGN KEY (code) REFERENCES instruments(code)
);

CREATE INDEX IF NOT EXISTS idx_universe_cap ON analysis_universe(market_cap DESC);
CREATE INDEX IF NOT EXISTS idx_universe_rank ON analysis_universe(rank);
