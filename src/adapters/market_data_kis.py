"""KIS OpenAPI 분봉 수집 어댑터.

pykrx 는 분봉을 지원하지 않으므로 KIS OpenAPI 직접 호출.
kis_mock / live 공용 (base_url 만 달라짐).

주요 엔드포인트:
  POST /oauth2/tokenP                                          — 토큰 발급
  GET  /uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice
       TR-ID: FHKST03010200
       1회 최대 30봉, FID_INPUT_HOUR_1 역순 paginate로 전일치 수집
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime

import httpx

from src import config
from src.adapters.broker_kis import (
    _Token,
    _format_token_error,
    _load_token_from_disk,
    _save_token_to_disk,
)
from src.adapters.market_data_base import CurrentPrice, MinuteBar

log = logging.getLogger("bunting.adapter.kis_market")

KIS_MOCK_BASE = "https://openapivts.koreainvestment.com:29443"
KIS_LIVE_BASE = "https://openapi.koreainvestment.com:9443"

MARKET_OPEN  = "090000"
MARKET_CLOSE = "153000"


class KISMarketDataSource:
    """KIS OpenAPI 분봉 수집 전용 어댑터."""

    def __init__(self, mode: config.TradeMode | None = None) -> None:
        m = mode or config.TRADE_MODE
        if m == config.TradeMode.LIVE:
            self.base_url = KIS_LIVE_BASE
            self.app_key  = config.KIS_LIVE_APP_KEY
            self.app_secret = config.KIS_LIVE_APP_SECRET
            self._mode_key = "live"
        else:
            self.base_url = KIS_MOCK_BASE
            self.app_key  = config.KIS_MOCK_APP_KEY
            self.app_secret = config.KIS_MOCK_APP_SECRET
            self._mode_key = "kis_mock"
        # broker_kis 와 같은 디스크 캐시 파일을 공유 — 같은 KIS 키로 토큰 1회 발급, 두 어댑터가 재사용.
        self._token: _Token | None = None

    # ----------------------------------------------------------
    # 인증
    # ----------------------------------------------------------

    def _get_token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at > now + 30:
            return self._token.access_token

        disk = _load_token_from_disk(self._mode_key)
        if disk is not None:
            self._token = disk
            return disk.access_token

        res = httpx.post(
            f"{self.base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
            timeout=10,
        )
        if res.status_code >= 400:
            raise RuntimeError(_format_token_error(res, self._mode_key))
        body = res.json()
        if "access_token" not in body:
            raise RuntimeError(
                f"KIS 토큰 발급 응답에 access_token 없음 ({self._mode_key}): {body}"
            )
        expires_in = int(body.get("expires_in", 86400))
        self._token = _Token(
            access_token=body["access_token"],
            expires_at=time.time() + expires_in,
        )
        _save_token_to_disk(self._mode_key, self._token)
        log.info("KIS OAuth 토큰 발급 (%s, ttl=%ss)", self._mode_key, expires_in)
        return self._token.access_token

    def _headers(self, tr_id: str) -> dict:
        return {
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    # ----------------------------------------------------------
    # 현재가 조회
    # ----------------------------------------------------------

    def fetch_current_price(self, code: str) -> CurrentPrice | None:
        """종목 현재가 스냅샷 조회 (FHKST01010100).

        장중에는 실시간 현재가, 장 마감 후에는 종가를 반환.
        """
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        }
        try:
            res = httpx.get(
                f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=self._headers("FHKST01010100"),
                params=params,
                timeout=10,
            )
            res.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("[%s] 현재가 조회 실패: %s", code, e)
            # 5xx 만 audit_log 기록 — 운영 메트릭에 노출 (KIS 측 장애 집계용)
            try:
                status = getattr(getattr(e, "response", None), "status_code", 0)
                if status and status >= 500:
                    from src.services.audit_service import log_event
                    log_event(None, "kis_5xx", {
                        "endpoint": "inquire-price", "code": code, "status": status,
                    })
            except Exception:
                pass
            return None

        data = res.json()
        if data.get("rt_cd") != "0":
            log.warning("[%s] KIS 현재가 응답 오류: %s", code, data.get("msg1"))
            return None

        o = data.get("output", {})
        try:
            return CurrentPrice(
                code=code,
                price=int(o["stck_prpr"]),           # 현재가
                open=int(o["stck_oprc"]),             # 시가
                high=int(o["stck_hgpr"]),             # 고가
                low=int(o["stck_lwpr"]),              # 저가
                prev_close=int(o["stck_sdpr"]),       # 전일 종가
                volume=int(o["acml_vol"]),            # 누적 거래량
                change_pct=float(o.get("prdy_ctrt", 0)),  # 등락률
                fetched_at=datetime.now(),
            )
        except (KeyError, ValueError) as e:
            log.warning("[%s] 현재가 파싱 실패: %s", code, e)
            return None

    def fetch_current_prices(self, codes: list[str]) -> dict[str, CurrentPrice]:
        """여러 종목 현재가 일괄 조회. API 호출 제한(초당 20회) 준수."""
        result: dict[str, CurrentPrice] = {}
        for code in codes:
            cp = self.fetch_current_price(code)
            if cp:
                result[code] = cp
            time.sleep(0.05)  # 초당 20회 제한 준수
        return result

    # ----------------------------------------------------------
    # 분봉 수집
    # ----------------------------------------------------------

    def fetch_minute_ohlcv(self, code: str, as_of: date) -> list[MinuteBar]:
        """당일 분봉 전체 수집 (9:00 ~ 15:30).

        KIS API 는 1회 최대 30봉이므로 역순 cursor 방식으로 paginate.
        """
        all_bars: dict[str, MinuteBar] = {}
        cursor = MARKET_CLOSE

        for _ in range(15):   # 최대 15 × 30 = 450봉 (390봉으로 충분)
            batch = self._fetch_batch(code, as_of, cursor)
            if not batch:
                break
            for bar in batch:
                key = bar.datetime.strftime("%Y-%m-%d %H:%M")
                all_bars[key] = bar
            earliest = min(batch, key=lambda b: b.datetime)
            cursor = earliest.datetime.strftime("%H%M%S")
            if cursor <= MARKET_OPEN:
                break
            time.sleep(0.05)

        return sorted(all_bars.values(), key=lambda b: b.datetime)

    def _fetch_batch(self, code: str, as_of: date, hour: str) -> list[MinuteBar]:
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": hour,
            "FID_PW_DATA_INCU_YN": "Y",
        }
        try:
            res = httpx.get(
                f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                headers=self._headers("FHKST03010200"),
                params=params,
                timeout=10,
            )
            res.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("[%s] 분봉 조회 실패: %s", code, e)
            return []

        data = res.json()
        if data.get("rt_cd") != "0":
            log.warning("[%s] KIS 응답 오류: %s", code, data.get("msg1"))
            return []

        date_str = as_of.strftime("%Y%m%d")
        open_dt  = datetime.strptime(f"{date_str} {MARKET_OPEN}",  "%Y%m%d %H%M%S")
        close_dt = datetime.strptime(f"{date_str} {MARKET_CLOSE}", "%Y%m%d %H%M%S")

        out: list[MinuteBar] = []
        for row in data.get("output2", []):
            row_date = row.get("stck_bsop_date", date_str)
            row_time = row.get("stck_cntg_hour", "000000")
            try:
                dt = datetime.strptime(f"{row_date} {row_time}", "%Y%m%d %H%M%S")
            except ValueError:
                continue
            if not (open_dt <= dt <= close_dt):
                continue
            try:
                out.append(MinuteBar(
                    code=code,
                    datetime=dt,
                    open=int(row["stck_oprc"]),
                    high=int(row["stck_hgpr"]),
                    low=int(row["stck_lwpr"]),
                    close=int(row["stck_prpr"]),
                    volume=int(row["cntg_vol"]),
                ))
            except (KeyError, ValueError):
                continue
        return out
