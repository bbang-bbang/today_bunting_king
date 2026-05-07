"""KIS Developers 어댑터 (kis_mock / live 공용).

OAuth 토큰 발급/캐시 → 잔고·주문·조회·취소 API.
모의투자(kis_mock)와 실거래(live)는 base_url 과 TR-ID 만 달라진다.

주요 엔드포인트:
  POST /oauth2/tokenP                                   — 토큰 발급
  POST /uapi/hashkey                                    — 주문 해시키 (POST 요청 서명)
  GET  /uapi/domestic-stock/v1/trading/inquire-balance  — 주식 잔고
  POST /uapi/domestic-stock/v1/trading/order-cash       — 주식 현금 주문
  GET  /uapi/domestic-stock/v1/trading/inquire-daily-ccld — 주문체결 조회
  POST /uapi/domestic-stock/v1/trading/order-rvsecncl   — 정정·취소
  GET  /uapi/domestic-stock/v1/quotations/inquire-price — 현재가 (거래가능 판정용)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from src import config
from src.adapters.broker_base import BrokerAdapter, OrderRequest, OrderResponse

log = logging.getLogger("bunting.adapter.kis_broker")

KIS_MOCK_BASE = "https://openapivts.koreainvestment.com:29443"
KIS_LIVE_BASE = "https://openapi.koreainvestment.com:9443"

# 토큰 디스크 캐시 위치. 봇 재시작 시 KIS "분당 1회 발급 제한"(EGW00133) 회피용.
# data/ 폴더는 .gitignore 로 보호됨.
_TOKEN_CACHE_DIR = Path("./data")


# TR-ID 표 (공식 KIS OpenAPI 기준)
# 주문
_TR_ORDER_BUY  = {"kis_mock": "VTTC0802U", "live": "TTTC0802U"}
_TR_ORDER_SELL = {"kis_mock": "VTTC0801U", "live": "TTTC0801U"}
_TR_ORDER_CANCEL = {"kis_mock": "VTTC0803U", "live": "TTTC0803U"}
# 조회
_TR_BALANCE = {"kis_mock": "VTTC8434R", "live": "TTTC8434R"}
_TR_DAILY_CCLD = {"kis_mock": "VTTC8001R", "live": "TTTC8001R"}


@dataclass
class _Token:
    access_token: str
    expires_at: float  # wall-clock epoch seconds (time.time())


def _token_cache_path(mode_key: str) -> Path:
    return _TOKEN_CACHE_DIR / f".kis_token_{mode_key}.json"


def _load_token_from_disk(mode_key: str) -> _Token | None:
    p = _token_cache_path(mode_key)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        tok = _Token(access_token=d["access_token"], expires_at=float(d["expires_at"]))
        if tok.expires_at <= time.time() + 60:
            return None  # 만료 임박은 무시
        return tok
    except Exception as e:
        log.warning("토큰 캐시 로드 실패 (%s): %s", p, e)
        return None


_TOKEN_ERR_HINT = {
    "EGW00133": "분당 1회 토큰 발급 제한. 1~2분 후 재시도 (또는 디스크 캐시 활용).",
    "EGW00121": "appkey/appsecret 오류. KIS 개발자센터의 모의투자/실전 키를 다시 확인.",
    "EGW00122": "appkey/appsecret 오류. KIS 개발자센터의 모의투자/실전 키를 다시 확인.",
    "EGW00205": "등록되지 않은 IP. KIS 개발자센터에서 호출 IP를 등록 필요.",
}


def _format_token_error(res: httpx.Response, mode_key: str) -> str:
    """KIS OAuth 실패 응답을 사람-친화 문자열로. 본문(JSON)에 error_code/description 있음."""
    code = ""
    desc = ""
    try:
        body = res.json()
        code = str(body.get("error_code") or body.get("rt_cd") or "")
        desc = str(body.get("error_description") or body.get("msg1") or body.get("msg") or "")
    except Exception:
        desc = (res.text or "")[:200]
    hint = _TOKEN_ERR_HINT.get(code, "")
    parts = [f"KIS 토큰 발급 실패 ({mode_key}) HTTP {res.status_code}"]
    if code:
        parts.append(f"code={code}")
    if desc:
        parts.append(f"msg={desc}")
    if hint:
        parts.append(f"→ {hint}")
    return " | ".join(parts)


def _save_token_to_disk(mode_key: str, tok: _Token) -> None:
    p = _token_cache_path(mode_key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"access_token": tok.access_token, "expires_at": tok.expires_at}),
            encoding="utf-8",
        )
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass  # Windows에선 무시
    except Exception as e:
        log.warning("토큰 캐시 저장 실패 (%s): %s", p, e)


class KISBrokerAdapter(BrokerAdapter):
    def __init__(self, mode: config.TradeMode) -> None:
        if mode == config.TradeMode.KIS_MOCK:
            self.base_url = KIS_MOCK_BASE
            self.app_key = config.KIS_MOCK_APP_KEY
            self.app_secret = config.KIS_MOCK_APP_SECRET
            self.account_no = config.KIS_MOCK_ACCOUNT_NO
            self._mode_key = "kis_mock"
        elif mode == config.TradeMode.LIVE:
            config.require_live_keys()
            self.base_url = KIS_LIVE_BASE
            self.app_key = config.KIS_LIVE_APP_KEY
            self.app_secret = config.KIS_LIVE_APP_SECRET
            self.account_no = config.KIS_LIVE_ACCOUNT_NO
            self._mode_key = "live"
        else:
            raise ValueError(f"KISBrokerAdapter 는 kis_mock/live 전용, 전달값: {mode}")

        if not self.app_key or not self.app_secret or not self.account_no:
            raise RuntimeError(f"KIS 키 누락 (mode={self._mode_key})")

        # CANO(8) - ACNT_PRDT_CD(2)
        if "-" not in self.account_no:
            raise RuntimeError(
                f"KIS 계좌번호 형식 오류 ('12345678-01' 형태여야 함): {self.account_no}"
            )
        cano, prdt = self.account_no.split("-", 1)
        self.cano = cano
        self.acnt_prdt_cd = prdt

        self._token: _Token | None = None
        self._token_lock = asyncio.Lock()

    # ============================================================
    # OAuth
    # ============================================================

    async def _ensure_token(self) -> str:
        async with self._token_lock:
            now = time.time()
            if self._token and self._token.expires_at > now + 30:  # 30초 여유
                return self._token.access_token

            disk = _load_token_from_disk(self._mode_key)
            if disk is not None:
                self._token = disk
                return disk.access_token

            async with httpx.AsyncClient(timeout=10) as cli:
                res = await cli.post(
                    f"{self.base_url}/oauth2/tokenP",
                    json={
                        "grant_type": "client_credentials",
                        "appkey": self.app_key,
                        "appsecret": self.app_secret,
                    },
                )
                if res.status_code >= 400:
                    raise RuntimeError(_format_token_error(res, self._mode_key))
                body = res.json()

            if "access_token" not in body:
                # 200이지만 본문에 토큰 없음 (KIS는 가끔 200 + error_description으로 응답)
                raise RuntimeError(
                    f"KIS 토큰 발급 응답에 access_token 없음 ({self._mode_key}): {body}"
                )
            access_token = body["access_token"]
            expires_in = int(body.get("expires_in", 86400))
            self._token = _Token(
                access_token=access_token,
                expires_at=time.time() + expires_in,
            )
            _save_token_to_disk(self._mode_key, self._token)
            log.info("KIS OAuth 토큰 발급 (%s, ttl=%ss)", self._mode_key, expires_in)
            return access_token

    async def _headers(
        self,
        tr_id: str,
        *,
        hashkey: str | None = None,
    ) -> dict[str, str]:
        h = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {await self._ensure_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if hashkey:
            h["hashkey"] = hashkey
        return h

    async def _hashkey(self, body: dict) -> str:
        """주문 POST 요청 바디를 서명한 hashkey 반환."""
        async with httpx.AsyncClient(timeout=10) as cli:
            res = await cli.post(
                f"{self.base_url}/uapi/hashkey",
                headers={
                    "content-type": "application/json; charset=utf-8",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                },
                json=body,
            )
            res.raise_for_status()
            return res.json()["HASH"]

    # ============================================================
    # 잔고
    # ============================================================

    async def get_balance(self) -> dict:
        """주식 잔고 조회.

        반환 스키마:
          {
            "cash_available": int,        # 주문가능현금 (D+2)
            "total_evaluation": int,      # 총평가금액
            "total_purchase": int,        # 총매입금액
            "total_pnl": int,             # 총손익
            "total_pnl_pct": float,       # 총손익률(%)
            "positions": [
              {
                "code": str,              # 종목코드
                "name": str,              # 종목명
                "quantity": int,          # 보유수량
                "avg_price": int,         # 평균매입가
                "current_price": int,     # 현재가
                "evaluation": int,        # 평가금액
                "pnl": int,               # 평가손익
                "pnl_pct": float,         # 평가수익률(%)
              },
              ...
            ],
          }
        """
        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "AFHR_FLPR_YN": "N",          # 시간외단일가
            "OFL_YN": "",                  # 오프라인여부
            "INQR_DVSN": "02",             # 조회구분(02: 종목별)
            "UNPR_DVSN": "01",             # 단가구분
            "FUND_STTL_ICLD_YN": "N",      # 펀드결제분포함
            "FNCG_AMT_AUTO_RDPT_YN": "N",  # 융자금액자동상환
            "PRCS_DVSN": "00",             # 처리구분(00: 전일매매포함)
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        # 백오프 retry — submit_order 와 동일한 패턴 (5xx + EGW00123 토큰 만료 대응).
        # KIS 모의투자가 inquire-balance 에 500 단발 발사하는 경우 다수.
        last_error: Exception | None = None
        data: dict | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10) as cli:
                    res = await cli.get(
                        f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance",
                        headers=await self._headers(_TR_BALANCE[self._mode_key]),
                        params=params,
                    )
                body_data: dict | None = None
                try:
                    body_data = res.json()
                except Exception:
                    pass

                # 토큰 만료 감지 → 캐시 무효화 후 즉시 재시도
                if body_data and body_data.get("msg_cd") == "EGW00123" and attempt < 2:
                    log.warning("[balance] 토큰 만료 감지 — 캐시 무효화 후 재발급 (%d/3)", attempt + 2)
                    self._token = None
                    try:
                        _token_cache_path(self._mode_key).unlink(missing_ok=True)
                    except Exception:
                        pass
                    last_error = RuntimeError(f"토큰 만료 EGW00123 (재발급 시도 {attempt+1})")
                    continue

                if res.status_code >= 500 and attempt < 2:
                    log.warning(
                        "[balance] 잔고조회 5xx (%d) — %ds 후 재시도 (%d/3)",
                        res.status_code, 2 ** attempt, attempt + 2,
                    )
                    last_error = RuntimeError(f"KIS Server error {res.status_code}")
                    await asyncio.sleep(2 ** attempt)
                    continue
                res.raise_for_status()
                data = body_data if body_data is not None else res.json()
                break
            except httpx.HTTPError as e:
                last_error = e
                if attempt < 2:
                    log.warning(
                        "[balance] 잔고조회 HTTP — %ds 후 재시도 (%d/3): %s",
                        2 ** attempt, attempt + 2, e,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

        if data is None:
            # 운영 메트릭 — 3회 retry 끝에 5xx 로 실패한 잔고조회 기록
            try:
                from src.services.audit_service import log_event
                log_event(None, "kis_5xx", {
                    "endpoint": "inquire-balance", "status": 500,
                    "retries_exhausted": True,
                })
            except Exception:
                pass
            raise RuntimeError(f"KIS 잔고조회 3회 재시도 실패: {last_error}")

        if data.get("rt_cd") != "0":
            raise RuntimeError(
                f"KIS 잔고조회 실패: rt_cd={data.get('rt_cd')} msg={data.get('msg1')}"
            )

        positions = []
        for row in data.get("output1", []) or []:
            qty = int(row.get("hldg_qty", 0) or 0)
            if qty <= 0:
                continue
            positions.append({
                "code": row.get("pdno", ""),
                "name": row.get("prdt_name", ""),
                "quantity": qty,
                "avg_price": int(float(row.get("pchs_avg_pric", 0) or 0)),
                "current_price": int(row.get("prpr", 0) or 0),
                "evaluation": int(row.get("evlu_amt", 0) or 0),
                "pnl": int(row.get("evlu_pfls_amt", 0) or 0),
                "pnl_pct": float(row.get("evlu_pfls_rt", 0) or 0),
            })

        out2 = (data.get("output2") or [{}])[0]
        total_eval = int(out2.get("tot_evlu_amt", 0) or 0)
        total_purchase = int(out2.get("pchs_amt_smtl_amt", 0) or 0)
        total_pnl = int(out2.get("evlu_pfls_smtl_amt", 0) or 0)
        total_pnl_pct = (total_pnl / total_purchase * 100) if total_purchase > 0 else 0.0

        # 주문가능현금: dnca_tot_amt(예수금) 또는 nxdy_excc_amt(익일정산) 또는 prvs_rcdl_excc_amt(D+2)
        # 우선 prvs_rcdl_excc_amt(D+2 예수금) 사용
        cash_available = int(
            out2.get("prvs_rcdl_excc_amt",
                     out2.get("nxdy_excc_amt",
                              out2.get("dnca_tot_amt", 0))) or 0
        )

        return {
            "cash_available": cash_available,
            "total_evaluation": total_eval,
            "total_purchase": total_purchase,
            "total_pnl": total_pnl,
            "total_pnl_pct": round(total_pnl_pct, 2),
            "positions": positions,
        }

    # ============================================================
    # 주문
    # ============================================================

    async def _get_position_quantity(self, code: str) -> int | None:
        """체결 확인 fallback 용 — 종목 보유량 조회. 실패 시 None."""
        try:
            bal = await self.get_balance()
            pos = next((p for p in bal.get("positions", []) if p.get("code") == code), None)
            return int(pos["quantity"]) if pos else 0
        except Exception as e:
            log.warning("[balance %s] 조회 실패 (체결 fallback): %s", code, e)
            return None

    async def submit_order(self, req: OrderRequest) -> OrderResponse:
        if req.price is None:
            # 시장가 (ORD_DVSN=01). ORD_UNPR은 0.
            ord_dvsn = "01"
            ord_unpr = "0"
        else:
            # 방어적 호가단위 정렬 — KIS 는 비호가 가격을 40030000 으로 거부함.
            # 매수는 up, 매도는 down (보수적 — 약간 비싸게 매수 / 약간 싸게 매도, 즉시 체결 우선).
            from src.risk.guard import align_to_tick
            aligned = align_to_tick(req.price, "up" if req.side == "buy" else "down")
            if aligned != req.price:
                log.info("[%s %s] 호가단위 정렬: %s → %s", req.side, req.code, req.price, aligned)
            ord_dvsn = "00"  # 지정가
            ord_unpr = str(aligned)

        # 매수 직전 보유량 캐시 — 체결 확인 fallback 용. 실패해도 진행.
        pre_qty: int | None = None
        if req.side == "buy":
            pre_qty = await self._get_position_quantity(req.code)

        body = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "PDNO": req.code,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(req.quantity),
            "ORD_UNPR": ord_unpr,
        }

        tr_map = _TR_ORDER_BUY if req.side == "buy" else _TR_ORDER_SELL
        tr_id = tr_map[self._mode_key]

        # KIS 모의투자 서버는 5xx 산발 발생 — 1s/2s/4s 백오프 재시도.
        # 4xx (클라이언트 에러) 는 재시도해도 동일 결과라 즉시 실패.
        last_error: Exception | None = None
        data: dict | None = None
        for attempt in range(3):
            try:
                hashkey = await self._hashkey(body)
                async with httpx.AsyncClient(timeout=10) as cli:
                    res = await cli.post(
                        f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash",
                        headers=await self._headers(tr_id, hashkey=hashkey),
                        json=body,
                    )
                    # KIS 가 토큰 만료를 500 + body{rt_cd:1, msg_cd:EGW00123} 로 보내는 황당 케이스 대응:
                    # body 를 먼저 파싱해서 토큰 에러면 캐시 무효화 후 재시도, 진짜 5xx 면 백오프 재시도.
                    body_data: dict | None = None
                    try:
                        body_data = res.json()
                    except Exception:
                        pass

                    if body_data and body_data.get("msg_cd") == "EGW00123" and attempt < 2:
                        log.warning(
                            "[%s %s] 토큰 만료 감지 — 캐시 무효화 후 재발급 (%d/3)",
                            req.side, req.code, attempt + 2,
                        )
                        self._token = None
                        try:
                            _token_cache_path(self._mode_key).unlink(missing_ok=True)
                        except Exception:
                            pass
                        last_error = RuntimeError(f"토큰 만료 EGW00123 (재발급 시도 {attempt+1})")
                        continue  # 즉시 재시도 (sleep 없이 — 토큰 재발급은 즉시 가능)

                    if res.status_code >= 500 and attempt < 2:
                        log.warning(
                            "[%s %s] 주문 5xx (%d) — %ds 후 재시도 (%d/3)",
                            req.side, req.code, res.status_code, 2 ** attempt, attempt + 2,
                        )
                        last_error = RuntimeError(f"KIS Server error {res.status_code}")
                        await asyncio.sleep(2 ** attempt)
                        continue
                    res.raise_for_status()
                    data = body_data if body_data is not None else res.json()
                    break
            except httpx.HTTPError as e:
                last_error = e
                if attempt < 2 and isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500:
                    log.warning(
                        "[%s %s] 주문 HTTP %s — %ds 후 재시도 (%d/3)",
                        req.side, req.code, e.response.status_code, 2 ** attempt, attempt + 2,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                if attempt < 2 and not isinstance(e, httpx.HTTPStatusError):
                    # 네트워크 timeout 등 — 5xx와 동일 처리
                    log.warning(
                        "[%s %s] 주문 네트워크 — %ds 후 재시도 (%d/3): %s",
                        req.side, req.code, 2 ** attempt, attempt + 2, e,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                log.warning("[%s %s] 주문 HTTP 실패: %s", req.side, req.code, e)
                return OrderResponse(
                    broker_order_id="",
                    status="failed",
                    error=f"HTTP 오류: {e}",
                )
            except RuntimeError as e:
                # _ensure_token / _format_token_error 등에서 KIS 본문 메시지 노출
                log.warning("[%s %s] 주문 사전단계 실패: %s", req.side, req.code, e)
                return OrderResponse(
                    broker_order_id="",
                    status="failed",
                    error=str(e),
                )

        if data is None:
            log.warning("[%s %s] 주문 3회 재시도 모두 실패: %s", req.side, req.code, last_error)
            return OrderResponse(
                broker_order_id="",
                status="failed",
                error=f"HTTP 오류 (3회 재시도 실패): {last_error}",
            )

        if data.get("rt_cd") != "0":
            return OrderResponse(
                broker_order_id="",
                status="failed",
                error=f"{data.get('msg_cd','')}:{data.get('msg1','')}",
                raw=data,
            )

        out = data.get("output", {}) or {}
        broker_order_id = f"{out.get('KRX_FWDG_ORD_ORGNO','')}-{out.get('ODNO','')}"

        if broker_order_id and "-" in broker_order_id:
            # 체결 확인 — 1s/2s/3s/4s/5s 후 5회 재조회 (총 15초).
            # KIS 모의투자 inquire-daily-ccld 가 5-10초 지연되는 정황 다수 → retry 길이 보강.
            # 그래도 안 잡히면 잔고 조회 fallback (매수 한정), 마지막에 명시적 failed.
            for retry in range(5):
                await asyncio.sleep(retry + 1)  # 1, 2, 3, 4, 5
                try:
                    status_resp = await self.get_order_status(broker_order_id)
                    if status_resp.status in ("filled", "partial"):
                        # 매수의 경우 잔고로 한 번 더 검증 — daily-ccld 가 잘못된 filled
                        # 응답을 주는 KIS 모의투자 사고 (2026-05-04 033100) 회귀 방지.
                        if req.side == "buy" and pre_qty is not None:
                            post_qty = await self._get_position_quantity(req.code)
                            if post_qty is not None and post_qty <= pre_qty:
                                log.warning(
                                    "[buy %s] daily-ccld %s 응답이지만 잔고 미증가 "
                                    "(pre=%d, post=%s, ODNO=%s) → pending 으로 강등",
                                    req.code, status_resp.status, pre_qty, post_qty,
                                    broker_order_id,
                                )
                                if retry < 4:
                                    continue
                                # 마지막 retry 까지 잔고 안 늘면 아래 pending fallback 으로
                                break
                        return status_resp
                    if status_resp.status == "pending" and retry < 4:
                        continue
                except Exception as e:
                    log.warning(
                        "[%s %s] 체결 재조회 (%d/5) 실패: %s",
                        req.side, req.code, retry + 1, e,
                    )
                    if retry < 4:
                        continue

            # Fallback — 매수면 잔고 변화로 체결 확인. KIS daily-ccld 지연되어도 잔고는 즉시 반영되는 케이스 대응.
            if req.side == "buy" and pre_qty is not None:
                post_qty = await self._get_position_quantity(req.code)
                if post_qty is not None and post_qty > pre_qty:
                    delta = post_qty - pre_qty
                    fill_price = req.price if req.price else 0
                    log.info(
                        "[buy %s] 잔고 fallback 체결 확인: +%d주 (pre=%d → post=%d, ODNO=%s)",
                        req.code, delta, pre_qty, post_qty, broker_order_id,
                    )
                    notional = fill_price * delta if fill_price else 0
                    return OrderResponse(
                        broker_order_id=broker_order_id,
                        status="filled",
                        filled_quantity=delta,
                        filled_avg_price=fill_price,
                        commission=notional * 15 // 100_000,
                        tax=0,
                        raw=data,
                    )

            # 5회 retry + 잔고 fallback 모두 안 잡힘 → 'pending' (주문은 등록됐지만 미체결 대기 가능성).
            # KIS 가 ODNO 를 발급했다는 건 주문 자체는 접수된 것 — 가용현금에서 차감되어 묶이는 케이스 多.
            # 체결 안 된 진짜 실패와 구분해서 caller 가 적절한 안내 표시하도록.
            log.warning(
                "[%s %s] 체결 미확인 (5회 retry + 잔고 fallback, ODNO=%s) — pending 처리 (미체결 대기 가능성)",
                req.side, req.code, broker_order_id,
            )
            return OrderResponse(
                broker_order_id=broker_order_id,
                status="pending",
                filled_quantity=0,
                filled_avg_price=0,
                commission=0,
                tax=0,
                error=(
                    f"체결 미확인 (ODNO={broker_order_id}). "
                    f"KIS 앱 미체결 주문 확인 또는 /reconcile."
                ),
                raw=data,
            )

        # broker_order_id 자체가 비어있는 비정상 케이스 — KIS 응답 형식 오류
        return OrderResponse(
            broker_order_id=broker_order_id,
            status="failed",
            error=f"KIS 응답에 ODNO 없음 (rt_cd={data.get('rt_cd')}, msg={data.get('msg1')})",
            raw=data,
        )

    async def get_order_status(self, broker_order_id: str) -> OrderResponse:
        """당일 주문체결 내역에서 broker_order_id(=KRX_FWDG_ORD_ORGNO-ODNO) 조회."""
        if "-" not in broker_order_id:
            return OrderResponse(
                broker_order_id=broker_order_id,
                status="failed",
                error=f"broker_order_id 형식 오류: {broker_order_id}",
            )
        _orgno, odno = broker_order_id.split("-", 1)

        from datetime import date as _date
        today = _date.today().strftime("%Y%m%d")
        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",   # 전체
            "INQR_DVSN": "00",          # 역순
            "PDNO": "",
            "CCLD_DVSN": "00",          # 전체(체결+미체결)
            "ORD_GNO_BRNO": "",
            "ODNO": odno,
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        async with httpx.AsyncClient(timeout=10) as cli:
            res = await cli.get(
                f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                headers=await self._headers(_TR_DAILY_CCLD[self._mode_key]),
                params=params,
            )
            res.raise_for_status()
            data = res.json()

        if data.get("rt_cd") != "0":
            return OrderResponse(
                broker_order_id=broker_order_id,
                status="failed",
                error=f"{data.get('msg_cd','')}:{data.get('msg1','')}",
                raw=data,
            )

        rows = data.get("output1", []) or []
        target = next((r for r in rows if r.get("odno") == odno), None)
        if not target:
            return OrderResponse(
                broker_order_id=broker_order_id,
                status="pending",
                raw=data,
            )

        ord_qty = int(target.get("ord_qty", 0) or 0)
        tot_ccld = int(target.get("tot_ccld_qty", 0) or 0)
        avg_price = int(float(target.get("avg_prvs", 0) or 0))
        status = (
            "filled" if tot_ccld >= ord_qty and ord_qty > 0
            else "partial" if tot_ccld > 0
            else "cancelled" if target.get("cncl_yn") == "Y"
            else "pending"
        )
        side = "sell" if target.get("sll_buy_dvsn_cd") == "01" else "buy"
        notional = avg_price * tot_ccld
        commission = notional * 15 // 100_000
        tax = notional * 20 // 100_000 if side == "sell" else 0

        return OrderResponse(
            broker_order_id=broker_order_id,
            status=status,
            filled_quantity=tot_ccld,
            filled_avg_price=avg_price,
            commission=commission,
            tax=tax,
            raw=data,
        )

    # KIS 좀비 응답 코드 — 봇 DB 만 정리해야 하는 케이스.
    # 모의투자에서 inquire-daily-ccld 가 잘못된 pending 응답을 주는 케이스.
    KIS_NO_QUANTITY_TO_CANCEL = "40330000"   # "수량 없음"
    KIS_ORDER_NOT_EXISTS = "40320000"        # "원주문번호 없음"
    KIS_ZOMBIE_CODES = {KIS_NO_QUANTITY_TO_CANCEL, KIS_ORDER_NOT_EXISTS}

    async def cancel_order_detail(self, broker_order_id: str) -> dict:
        """취소 시도 + 결과 상세 반환. zombie 판정에 사용.

        반환 dict:
          ok        : bool — 취소 성공 여부
          msg_cd    : str  — KIS 응답 메시지 코드 (실패 시)
          msg       : str  — KIS 응답 메시지 본문
          is_zombie : bool — msg_cd == 40330000 (큐에 사실상 없음)
        """
        if "-" not in broker_order_id:
            log.warning("cancel_order: broker_order_id 형식 오류 %s", broker_order_id)
            return {"ok": False, "msg_cd": "", "msg": "format_error", "is_zombie": False}
        orgno, odno = broker_order_id.split("-", 1)

        body = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": orgno,
            "ORGN_ODNO": odno,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 취소
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",       # 잔량 전부 취소
        }
        try:
            hashkey = await self._hashkey(body)
            async with httpx.AsyncClient(timeout=10) as cli:
                res = await cli.post(
                    f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl",
                    headers=await self._headers(_TR_ORDER_CANCEL[self._mode_key],
                                                hashkey=hashkey),
                    json=body,
                )
                res.raise_for_status()
                data = res.json()
        except httpx.HTTPError as e:
            log.warning("cancel_order HTTP 실패 (%s): %s", broker_order_id, e)
            return {"ok": False, "msg_cd": "", "msg": str(e), "is_zombie": False}

        ok = data.get("rt_cd") == "0"
        msg_cd = data.get("msg_cd", "") or ""
        msg = data.get("msg1", "") or ""
        is_zombie = msg_cd in self.KIS_ZOMBIE_CODES
        if not ok:
            log.warning("cancel_order 실패: %s %s", msg_cd, msg)
        return {"ok": ok, "msg_cd": msg_cd, "msg": msg, "is_zombie": is_zombie}

    async def cancel_order(self, broker_order_id: str) -> bool:
        """기존 호환 — bool 만 반환."""
        return (await self.cancel_order_detail(broker_order_id))["ok"]

    # ============================================================
    # 거래가능 여부
    # ============================================================

    async def is_tradable(self, code: str) -> bool:
        """현재가 조회로 거래정지·상하한가 여부 판단.

        - rt_cd != "0" 또는 output 없음  → False
        - iscd_stat_cls_code 가 "51"(거래정지) / "52"(관리) → False
        - 그 외 정상 → True
        """
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                res = await cli.get(
                    f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
                    headers=await self._headers("FHKST01010100"),
                    params=params,
                )
                res.raise_for_status()
                data = res.json()
        except httpx.HTTPError as e:
            log.warning("[%s] is_tradable 조회 실패: %s", code, e)
            return False

        if data.get("rt_cd") != "0":
            return False
        out = data.get("output") or {}
        stat = out.get("iscd_stat_cls_code", "")
        if stat in ("51", "52", "53", "54", "58", "59"):  # 정지·관리·투자주의·위험
            return False
        return True
