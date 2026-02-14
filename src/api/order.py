import requests
import json
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from src.config.config_manager import config_manager
from src.api.auth import kis_auth
from src.utils.logger import logger, get_mode_logger, log_engine_api
from src.api.exchange import normalize_order_exchange
from src.api.quote import kis_quote


def _log_engine_api_if_needed(caller: str | None, mode: str, payload: dict):
    if caller and str(caller).strip().upper() == "ENGINE":
        log_engine_api(mode, payload)

def _is_expired_token_response(payload: dict) -> bool:
    try:
        msg_cd = str(payload.get("msg_cd") or "").strip()
        msg1 = str(payload.get("msg1") or "")
        if msg_cd == "EGW00123":
            return True
        if "기간이 만료된 token" in msg1:
            return True
    except Exception:
        pass
    return False

def _retry_on_expired_token(payload: dict, mode: str, attempt: int, max_attempts: int) -> bool:
    if _is_expired_token_response(payload) and attempt < (max_attempts - 1):
        try:
            kis_auth.invalidate_token(mode)
        except Exception:
            pass
        time.sleep(0.3 * (attempt + 1))
        return True
    return False

class KisOrder:
    def __init__(self):
        pass

    def _format_ovrs_ord_unpr(self, price) -> str:
        """
        KIS 해외주식 단가(OVRS_ORD_UNPR) 포맷터.
        - 스펙: (23.8) => 소수점 최대 8자리
        - float -> str 변환 시 0.124999999999 / 1e-06 같은 형태가 나와
          INVALID INPUT_FILED_SIZE(OPSQ2002)를 유발할 수 있어 Decimal 기반으로 강제 포맷한다.
        """
        try:
            if price is None:
                return "0.00000000"
            # float을 직접 Decimal로 만들면 부동소수 오차가 들어가므로 str()을 거친다.
            d = Decimal(str(price))
            # 음수 가격은 비정상 -> 0으로 처리(방어)
            if d < 0:
                d = Decimal("0")

            # 소수 8자리로 절사(ROUND_DOWN) 후 불필요한 0 제거
            q = Decimal("0.00000001")
            d = d.quantize(q, rounding=ROUND_DOWN)
            
            # (23.8) 포맷이지만, API에 따라 불필요한 0이 길어지면 OPSQ2002 오류가 발생함 (특히 v1_014)
            # 따라서 59.82000000 -> 59.82 형태로 변환한다.
            s = format(d, "f")
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s if s else "0"
        except (InvalidOperation, ValueError, TypeError):
            return "0.00000000"

    def _format_order_price(self, price) -> str:
        """
        실전 주문 가격 포맷:
        - $1 이상: 소수점 2자리
        - $1 미만: 소수점 4자리
        """
        try:
            if price is None:
                return "0"
            d = Decimal(str(price))
            if d < 0:
                d = Decimal("0")
            if d == 0:
                return "0"
            if d >= Decimal("1"):
                q = Decimal("0.01")
            else:
                q = Decimal("0.0001")
            d = d.quantize(q, rounding=ROUND_DOWN)
            return format(d, "f")
        except (InvalidOperation, ValueError, TypeError):
            return "0"

    def _get_order_tr_id(self, exchange: str, side: str, mode: str) -> str:
        """
        해외주식 주문 TR_ID 결정 (v1_해외주식-001)
        - exchange: OVRS_EXCG_CD 기준 (NASD/NYSE/AMEX/SEHK/SHAA/SZAA/TKSE/HASE/VNSE)
        - side: buy/sell
        """
        ex = normalize_order_exchange(exchange)
        is_mock = (mode == "mock")

        # 미국(나스닥/뉴욕/아멕스 포함)
        if ex in ("NASD", "NYSE", "AMEX"):
            if side == "buy":
                return "VTTT1002U" if is_mock else "TTTT1002U"
            return "VTTT1001U" if is_mock else "TTTT1006U"

        # 홍콩
        if ex == "SEHK":
            if side == "buy":
                return "VTTS1002U" if is_mock else "TTTS1002U"
            return "VTTS1001U" if is_mock else "TTTS1001U"

        # 일본
        if ex == "TKSE":
            if side == "buy":
                return "VTTS0308U" if is_mock else "TTTS0308U"
            return "VTTS0307U" if is_mock else "TTTS0307U"

        # 중국 상해
        if ex == "SHAA":
            if side == "buy":
                return "VTTS0202U" if is_mock else "TTTS0202U"
            return "VTTS1005U" if is_mock else "TTTS1005U"

        # 중국 심천
        if ex == "SZAA":
            if side == "buy":
                return "VTTS0305U" if is_mock else "TTTS0305U"
            return "VTTS0304U" if is_mock else "TTTS0304U"

        # 베트남 (하노이/호치민)
        if ex in ("HASE", "VNSE"):
            if side == "buy":
                return "VTTS0311U" if is_mock else "TTTS0311U"
            return "VTTS0310U" if is_mock else "TTTS0310U"

        # 기본은 미국으로 처리(안전하게)
        if side == "buy":
            return "VTTT1002U" if is_mock else "TTTT1002U"
        return "VTTT1001U" if is_mock else "TTTT1006U"

    def _get_rvsecncl_tr_id(self, exchange: str, mode: str) -> str:
        """
        해외주식 정정취소 TR_ID 결정 (v1_해외주식-003)
        """
        ex = normalize_order_exchange(exchange)
        is_mock = (mode == "mock")

        # 미국(나스닥/뉴욕/아멕스 포함)
        if ex in ("NASD", "NYSE", "AMEX"):
            return "VTTT1004U" if is_mock else "TTTT1004U"

        # 홍콩
        if ex == "SEHK":
            return "VTTS1003U" if is_mock else "TTTS1003U"

        # 일본
        if ex == "TKSE":
            return "VTTS0309U" if is_mock else "TTTS0309U"

        # 중국 상해 (취소)
        if ex == "SHAA":
            return "VTTS0302U" if is_mock else "TTTS0302U"

        # 중국 심천 (취소)
        if ex == "SZAA":
            return "VTTS0306U" if is_mock else "TTTS0306U"

        # 베트남 (취소)
        if ex in ("HASE", "VNSE"):
            return "VTTS0312U" if is_mock else "TTTS0312U"

        return "VTTT1004U" if is_mock else "TTTT1004U"

    def _get_account_info(self, mode):
        """계좌번호 정보 반환"""
        cano = config_manager.get(f'{mode}.account_no_prefix')
        acnt_prdt_cd = config_manager.get(f'{mode}.account_no_suffix')
        return cano, acnt_prdt_cd

    def get_balance(self, exchange='NASD', currency='USD', mode=None, caller: str | None = None):
        """
        해외주식 잔고 조회 (v1_해외주식-006)
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)
        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')
        
        cano, acnt_prdt_cd = self._get_account_info(mode)
        token = kis_auth.get_token(mode)
        
        if not token or not cano:
            log.error("[Order] 토큰 또는 계좌번호가 설정되지 않았습니다.")
            return None

        url = f"{url_base}/uapi/overseas-stock/v1/trading/inquire-balance"

        # TR_ID 설정
        tr_id = "VTTS3012R" if mode == 'mock' else "TTTS3012R"

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id
        }

        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": normalize_order_exchange(exchange),
            "TR_CRCY_CD": currency,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": ""
        }

        # 초당 제한(EGW00201) 대응: 짧게 재시도
        for attempt in range(3):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 3):
                        continue
                    if data.get('rt_cd') == '0':
                        # 성공 로그는 DEBUG 레벨로 (너무 많은 로그 방지)
                        output1_count = len(data.get('output1', [])) if isinstance(data.get('output1'), list) else 0
                        log.debug(f"[Order] 잔고 조회 성공: {output1_count}개 종목 [debug: CANO={cano} (len={len(str(cano))}), ACNT_PRDT_CD={acnt_prdt_cd} (len={len(str(acnt_prdt_cd))}), mode={mode}, tr_id={tr_id}]")
                        _log_engine_api_if_needed(caller, mode, {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "api": "v1_006",
                            "method": "GET",
                            "url": url,
                            "headers": headers,
                            "params": params,
                            "status": res.status_code,
                            "response": data,
                        })
                        return data
                    if data.get("msg_cd") == "EGW00201":
                        time.sleep(0.3 * (attempt + 1))
                        continue
                    # OPSQ2000(계좌번호 검증 실패) - 모의투자 서버 불안정으로 간헐적 발생 가능, 재시도
                    if data.get("msg_cd") == "OPSQ2000":
                        if attempt < 2:  # 최대 2회 재시도 (총 3회 시도)
                            wait_sec = 0.5 * (attempt + 1)  # 0.5초, 1초 대기
                            # 재시도 중에는 로그 없음 (최종 실패 시에만 로그)
                            time.sleep(wait_sec)
                            continue
                        # 모든 재시도 실패 시에만 ERROR 로그
                        log.error(f"[Order] 잔고 조회 실패 (재시도 3회 모두 실패): {data.get('msg1')} ({data.get('msg_cd')}) [debug: CANO={cano} (len={len(str(cano))}), ACNT_PRDT_CD={acnt_prdt_cd} (len={len(str(acnt_prdt_cd))}), mode={mode}, tr_id={tr_id}]")
                    else:
                        log.error(f"[Order] 잔고 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "api": "v1_006",
                        "method": "GET",
                        "url": url,
                        "headers": headers,
                        "params": params,
                        "status": res.status_code,
                        "response": data,
                    })
                    return None

                if res.status_code == 500:
                    # 초당 제한은 500으로 내려오는 케이스가 있음
                    try:
                        data = res.json() or {}
                        if _retry_on_expired_token(data, mode, attempt, 3):
                            continue
                        if data.get("msg_cd") == "EGW00201":
                            time.sleep(0.3 * (attempt + 1))
                            continue
                    except Exception:
                        pass

                log.error(f"[Order] API 호출 오류: {res.status_code} - {res.text}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_006",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "status": res.status_code,
                    "response": res.text,
                })
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Order] 잔고 조회 중 예외 발생: {e}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_006",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "error": str(e),
                })
                return None

    def get_present_balance(
        self,
        natn_cd: str = "840",
        tr_mket_cd: str = "00",
        inqr_dvsn_cd: str = "00",
        wcrc_frcr_dvsn_cd: str = "02",
        caller: str | None = None,
        mode=None,
    ):
        """
        해외주식 체결기준현재잔고 조회 (v1_해외주식-008)
        - URL: /uapi/overseas-stock/v1/trading/inquire-present-balance
        - output3.frcr_use_psbl_amt(외화사용가능금액) 활용 가능
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode, source=f"API:{caller}" if caller else "API")

        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')
        cano, acnt_prdt_cd = self._get_account_info(mode)
        token = kis_auth.get_token(mode)

        if not token or not cano:
            log.error("[Order] 토큰 또는 계좌번호가 설정되지 않았습니다.")
            return None

        url = f"{url_base}/uapi/overseas-stock/v1/trading/inquire-present-balance"
        tr_id = "VTRP6504R" if mode == "mock" else "CTRP6504R"

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id
        }

        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "WCRC_FRCR_DVSN_CD": wcrc_frcr_dvsn_cd,  # 01:원화, 02:외화
            "NATN_CD": natn_cd,                      # 840:미국, 000:전체
            "TR_MKET_CD": tr_mket_cd,                # 00:전체 (미국일 때 NASD/NYSE 등)
            "INQR_DVSN_CD": inqr_dvsn_cd,            # 00:전체
        }

        # 초당 제한(EGW00201) 대응: 짧게 재시도
        for attempt in range(3):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 3):
                        continue
                    if data.get('rt_cd') == '0':
                        # 성공 로그는 DEBUG 레벨로 (너무 많은 로그 방지)
                        output3 = data.get('output3', {}) if isinstance(data.get('output3'), dict) else {}
                        total_asset = output3.get('tot_evlu_amt', 'N/A')
                        log.debug(f"[Order] 체결기준현재잔고 조회 성공: 총자산={total_asset} [debug: CANO={cano} (len={len(str(cano))}), ACNT_PRDT_CD={acnt_prdt_cd} (len={len(str(acnt_prdt_cd))}), mode={mode}, tr_id={tr_id}]")
                        _log_engine_api_if_needed(caller, mode, {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "api": "v1_008",
                            "method": "GET",
                            "url": url,
                            "headers": headers,
                            "params": params,
                            "status": res.status_code,
                            "response": data,
                        })
                        return data
                    if data.get("msg_cd") == "EGW00201":
                        time.sleep(0.3 * (attempt + 1))
                        continue
                    # OPSQ2000(계좌번호 검증 실패) - 모의투자 서버 불안정으로 간헐적 발생 가능, 재시도
                    if data.get("msg_cd") == "OPSQ2000":
                        if attempt < 2:  # 최대 2회 재시도 (총 3회 시도)
                            wait_sec = 0.5 * (attempt + 1)  # 0.5초, 1초 대기
                            # 재시도 중에는 로그 없음 (최종 실패 시에만 로그)
                            time.sleep(wait_sec)
                            continue
                        # 모든 재시도 실패 시에만 ERROR 로그
                        log.error(f"[Order] 체결기준현재잔고 조회 실패 (재시도 3회 모두 실패): {data.get('msg1')} ({data.get('msg_cd')}) [debug: CANO={cano} (len={len(str(cano))}), ACNT_PRDT_CD={acnt_prdt_cd} (len={len(str(acnt_prdt_cd))}), mode={mode}, tr_id={tr_id}]")
                    else:
                        log.error(f"[Order] 체결기준현재잔고 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "api": "v1_008",
                        "method": "GET",
                        "url": url,
                        "headers": headers,
                        "params": params,
                        "status": res.status_code,
                        "response": data,
                    })
                    return None

                if res.status_code == 500:
                    try:
                        data = res.json() or {}
                        if _retry_on_expired_token(data, mode, attempt, 3):
                            continue
                        if data.get("msg_cd") == "EGW00201":
                            time.sleep(0.3 * (attempt + 1))
                            continue
                    except Exception:
                        pass

                log.error(f"[Order] API 호출 오류: {res.status_code} - {res.text}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_008",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "status": res.status_code,
                    "response": res.text,
                })
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Order] 체결기준현재잔고 조회 중 예외 발생: {e}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_008",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "error": str(e),
                })
                return None

    def get_foreign_margin(self, mode=None, caller: str | None = None):
        """
        해외증거금 통화별조회 (해외주식-035) - 실전 전용

        - URL: /uapi/overseas-stock/v1/trading/foreign-margin
        - output[].itgr_ord_psbl_amt: 통합주문가능금액 (통합증거금 포함 주문가능금액)
        - output[].frcr_gnrl_ord_psbl_amt: 외화일반주문가능금액

        가이드에 따르면 모의투자는 미지원.
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode, source=f"API:{caller}" if caller else "API")

        if mode == "mock":
            log.warning("[Order] 해외증거금 통화별조회(해외주식-035)는 모의투자를 지원하지 않습니다.")
            return None

        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')
        cano, acnt_prdt_cd = self._get_account_info(mode)
        token = kis_auth.get_token(mode)

        if not token or not cano:
            log.error("[Order] 토큰 또는 계좌번호가 설정되지 않았습니다.")
            return None

        url = f"{url_base}/uapi/overseas-stock/v1/trading/foreign-margin"
        tr_id = "TTTC2101R"

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id,
            # 가이드상 필수. 개인계좌 기준 P 사용.
            "custtype": "P",
        }
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
        }

        for attempt in range(5):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 5):
                        continue
                    if data.get("rt_cd") == "0":
                        _log_engine_api_if_needed(caller, mode, {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "api": "v1_035",
                            "method": "GET",
                            "url": url,
                            "headers": headers,
                            "params": params,
                            "status": res.status_code,
                            "response": data,
                        })
                        return data
                    if data.get("msg_cd") == "EGW00201":
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    if data.get("msg_cd") == "APBK1350":
                        time.sleep(0.7 * (attempt + 1))
                        continue
                    log.error(f"[Order] 해외증거금 통화별조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "api": "v1_035",
                        "method": "GET",
                        "url": url,
                        "headers": headers,
                        "params": params,
                        "status": res.status_code,
                        "response": data,
                    })
                    return None

                if res.status_code == 500:
                    try:
                        data = res.json() or {}
                        if _retry_on_expired_token(data, mode, attempt, 5):
                            continue
                        if data.get("msg_cd") == "EGW00201":
                            time.sleep(0.5 * (attempt + 1))
                            continue
                        if data.get("msg_cd") == "APBK1350":
                            time.sleep(0.7 * (attempt + 1))
                            continue
                    except Exception:
                        pass

                log.error(f"[Order] 해외증거금 통화별조회 API 호출 오류: {res.status_code} - {res.text}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_035",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "status": res.status_code,
                    "response": res.text,
                })
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Order] 해외증거금 통화별조회 중 예외 발생: {e}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_035",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "error": str(e),
                })
                return None

    def get_unfilled_orders(self, exchange='NASD', mode=None, caller: str | None = None):
        """
        해외주식 미체결 내역 조회 (v1_해외주식-005)
        모의투자는 지원하지 않음 (대신 체결내역 API 사용 권장되나 여기선 스킵)
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)
        
        if mode == 'mock':
            log.warning("[Order] 미체결내역 API는 모의투자를 지원하지 않습니다.")
            return None

        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')
        
        cano, acnt_prdt_cd = self._get_account_info(mode)
        token = kis_auth.get_token(mode)

        url = f"{url_base}/uapi/overseas-stock/v1/trading/inquire-nccs"
        tr_id = "TTTS3018R"

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id
        }
        
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": normalize_order_exchange(exchange),
            "SORT_SQN": "DS",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": ""
        }
        
        for attempt in range(2):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 2):
                        continue
                    if data['rt_cd'] == '0':
                        _log_engine_api_if_needed(caller, mode, {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "api": "v1_005",
                            "method": "GET",
                            "url": url,
                            "headers": headers,
                            "params": params,
                            "status": res.status_code,
                            "response": data,
                        })
                        return data['output']
                    log.error(f"[Order] 미체결 조회 실패: {data['msg1']} ({data['msg_cd']})")
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "api": "v1_005",
                        "method": "GET",
                        "url": url,
                        "headers": headers,
                        "params": params,
                        "status": res.status_code,
                        "response": data,
                    })
                    return None
                if res.status_code == 500:
                    try:
                        data = res.json() or {}
                        if _retry_on_expired_token(data, mode, attempt, 2):
                            continue
                    except Exception:
                        pass
                log.error(f"[Order] API 호출 오류: {res.status_code} - {res.text}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_005",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "status": res.status_code,
                    "response": res.text,
                })
                return None
            except Exception as e:
                if attempt < 1:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Order] 미체결 조회 중 예외 발생: {e}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_005",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "error": str(e),
                })
                return None

    def order(self, symbol, quantity, price, side='buy', exchange='NASD', order_type='00', mode=None, caller: str | None = None):
        """
        해외주식 주문 (v1_해외주식-001)
        :param symbol: 종목코드 (티커)
        :param quantity: 수량
        :param price: 가격 (시장가는 0)
        :param side: 'buy' (매수) or 'sell' (매도)
        :param exchange: 거래소 (NASD, NYSE, AMEX)
        :param order_type: 주문구분 (00:지정가, 32:LOO 등. 시장가 매수는 실전/모의 다름 주의)
                           모의투자는 지정가(00)만 지원
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)
        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')
        
        cano, acnt_prdt_cd = self._get_account_info(mode)
        token = kis_auth.get_token(mode)
        
        if not token or not cano:
            log.error("[Order] 토큰 또는 계좌번호가 설정되지 않았습니다.")
            return None

        # TR_ID 결정 (거래소/국가별)
        tr_id = self._get_order_tr_id(exchange=exchange, side=side, mode=mode)

        # 모의투자는 지정가만 가능(가이드)
        if mode == "mock" and order_type != "00":
            log.warning("[Order] 모의투자는 지정가(00) 주문만 가능합니다. 강제 변경합니다.")
            order_type = "00"

        url = f"{url_base}/uapi/overseas-stock/v1/trading/order"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id
        }

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": normalize_order_exchange(exchange),
            "PDNO": symbol,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": self._format_order_price(price),
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": order_type
        }

        # 초당 제한(EGW00201) 발생 시 짧게 재시도(사용자 경험/체결 안정성 개선)
        for attempt in range(3):
            try:
                log.info(f"[Order] 주문 요청: {side} {symbol} {quantity}주 @ {price} ({order_type})")
                res = requests.post(url, headers=headers, data=json.dumps(body), timeout=20)

                if res.status_code == 200:
                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 3):
                        continue
                    if data.get('rt_cd') == '0':
                        log.info(f"[Order] 주문 성공: {data.get('msg1')} (주문번호: {data.get('output', {}).get('ODNO')})")
                        _log_engine_api_if_needed(caller, mode, {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "api": "v1_001",
                            "method": "POST",
                            "url": url,
                            "headers": headers,
                            "body": body,
                            "status": res.status_code,
                            "response": data,
                        })
                        return data.get('output')
                    log.error(f"[Order] 주문 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "api": "v1_001",
                        "method": "POST",
                        "url": url,
                        "headers": headers,
                        "body": body,
                        "status": res.status_code,
                        "response": data,
                    })
                    return None

                if res.status_code == 500:
                    try:
                        data = res.json() or {}
                        if _retry_on_expired_token(data, mode, attempt, 3):
                            continue
                        if data.get("msg_cd") == "EGW00201":
                            time.sleep(0.3 * (attempt + 1))
                            continue
                    except Exception:
                        pass

                log.error(f"[Order] API 호출 오류: {res.status_code} - {res.text}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_001",
                    "method": "POST",
                    "url": url,
                    "headers": headers,
                    "body": body,
                    "status": res.status_code,
                    "response": res.text,
                })
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Order] 주문 요청 중 예외 발생: {e}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_001",
                    "method": "POST",
                    "url": url,
                    "headers": headers,
                    "body": body,
                    "error": str(e),
                })
                return None

    def revise_cancel_order(
        self,
        exchange: str,
        symbol: str,
        origin_order_no: str,
        qty: int,
        price: float,
        action: str = "cancel",
        mode: str | None = None,
        caller: str | None = None,
    ):
        """
        해외주식 정정취소주문 (v1_해외주식-003)

        - action: "revise"(정정) / "cancel"(취소)
        - 취소는 price=0 입력
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)

        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')

        cano, acnt_prdt_cd = self._get_account_info(mode)
        token = kis_auth.get_token(mode)

        if not token or not cano:
            log.error("[Order] 토큰 또는 계좌번호가 설정되지 않았습니다.")
            return None

        # 국가/거래소별 TR_ID
        tr_id = self._get_rvsecncl_tr_id(exchange=exchange, mode=mode)

        url = f"{url_base}/uapi/overseas-stock/v1/trading/order-rvsecncl"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id,
            # 가이드상 선택이지만, VTS/모의에서 누락 시 오류가 날 수 있어 개인계좌 기준 P 고정
            "custtype": "P",
        }

        rvse_cncl = "01" if action == "revise" else "02"

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": normalize_order_exchange(exchange),
            "PDNO": symbol,
            "ORGN_ODNO": str(origin_order_no),
            "RVSE_CNCL_DVSN_CD": rvse_cncl,
            "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": self._format_order_price(price),
            "ORD_SVR_DVSN_CD": "0",
        }

        for attempt in range(2):
            try:
                res = requests.post(url, headers=headers, data=json.dumps(body), timeout=20)
                if res.status_code != 200:
                    if res.status_code == 500:
                        try:
                            data = res.json() or {}
                            if _retry_on_expired_token(data, mode, attempt, 2):
                                continue
                        except Exception:
                            pass
                    log.error(f"[Order] 정정/취소 API 호출 오류: {res.status_code} - {res.text}")
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "api": "v1_003",
                        "method": "POST",
                        "url": url,
                        "headers": headers,
                        "body": body,
                        "status": res.status_code,
                        "response": res.text,
                    })
                    return None

                data = res.json()
                if _retry_on_expired_token(data, mode, attempt, 2):
                    continue
                if data.get("rt_cd") == "0":
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "api": "v1_003",
                        "method": "POST",
                        "url": url,
                        "headers": headers,
                        "body": body,
                        "status": res.status_code,
                        "response": data,
                    })
                    return data.get("output")
                log.error(f"[Order] 정정/취소 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_003",
                    "method": "POST",
                    "url": url,
                    "headers": headers,
                    "body": body,
                    "status": res.status_code,
                    "response": data,
                })
                return None
            except Exception as e:
                if attempt < 1:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Order] 정정/취소 중 예외 발생: {e}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_003",
                    "method": "POST",
                    "url": url,
                    "headers": headers,
                    "body": body,
                    "error": str(e),
                })
                return None

    def get_buyable_amount(
        self,
        exchange: str,
        symbol: str,
        price: float,
        mode: str | None = None,
        debug: bool = False,
        caller: str | None = None,
    ):
        """
        해외주식 매수가능금액조회 (v1_해외주식-014)
        - exchange: OVRS_EXCG_CD (또는 NAS/NYS 등도 입력 가능; 내부에서 변환)
        - price: OVRS_ORD_UNPR (해외주문단가)
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)

        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')

        cano, acnt_prdt_cd = self._get_account_info(mode)
        token = kis_auth.get_token(mode)

        if not token or not cano:
            log.error("[Order] 토큰 또는 계좌번호가 설정되지 않았습니다.")
            return None

        tr_id = "VTTS3007R" if mode == "mock" else "TTTS3007R"
        url = f"{url_base}/uapi/overseas-stock/v1/trading/inquire-psamount"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id,
            # 가이드상 선택이지만, VTS/모의에서 누락 시 오류가 날 수 있어 개인계좌 기준 P 고정
            "custtype": "P",
        }

        item_cd_source = None

        def _build_debug_request(params_override=None):
            # params_override가 없으면 실제 요청 params를 사용
            params_view = (params_override if params_override is not None else params).copy()
            return {
                "url": url,
                "headers": {
                    "content-type": headers.get("content-type"),
                    "authorization": "Bearer **redacted**" if headers.get("authorization") else None,
                    "appkey": headers.get("appkey"),
                    "appsecret": "****" if headers.get("appsecret") else None,
                    "tr_id": headers.get("tr_id"),
                    "custtype": headers.get("custtype"),
                },
                "params": params_view,
                "param_lens": {k: len(str(v)) for k, v in params_view.items()},
                "input_symbol": symbol,
                "item_cd_source": item_cd_source,
            }

        def _debug_error(msg_cd=None, msg1=None, http_status=None, detail=None, params_override=None):
            if not debug:
                return None
            err = {
                "msg_cd": msg_cd,
                "msg1": msg1,
                "http_status": http_status,
                "request": _build_debug_request(params_override=params_override),
            }
            if detail:
                err["detail"] = detail
            return {"_error": err}

        # v1_014(매수가능금액조회) ITEM_CD는 Ticker(Symbol)를 입력해야 함.
        # 가이드상 Length 12로 되어있으나, 예제(00011) 등은 Ticker임.
        # ISIN(US...)을 넣으면 OPSQ2002(Input Field Size) 오류가 발생할 수 있음.
        item_cd_source = "direct"
        item_cd = str(symbol).strip().upper()
        
        if not item_cd:
            log.error(f"[Order] ITEM_CD 매핑 실패: symbol={symbol}, exchange={exchange}")
            return _debug_error(
                msg_cd="ITEM_CD_NOT_FOUND",
                msg1="ITEM_CD mapping failed",
                detail={"symbol": symbol, "exchange": exchange, "url": url},
                params_override={
                    "CANO": cano,
                    "ACNT_PRDT_CD": acnt_prdt_cd,
                    "OVRS_EXCG_CD": normalize_order_exchange(exchange),
                    "OVRS_ORD_UNPR": self._format_order_price(price),  # v1_014는 주문 API와 동일한 포맷 사용
                    "ITEM_CD": symbol,
                },
            ) or None

        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": normalize_order_exchange(exchange),
            "OVRS_ORD_UNPR": self._format_order_price(price),  # v1_014는 주문 API와 동일한 포맷 사용 (모의투자 서버 제한)
            "ITEM_CD": item_cd,
        }

        # 초당 제한(EGW00201) 대응: 짧게 재시도
        # - 엔진에서 "2회 재시도 후 스킵" 정책을 쓰므로, 여기서도 2회까지만 재시도한다.
        for attempt in range(2):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 2):
                        continue
                    if data.get("rt_cd") == "0":
                        _log_engine_api_if_needed(caller, mode, {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "api": "v1_014",
                            "method": "GET",
                            "url": url,
                            "headers": headers,
                            "params": params,
                            "status": res.status_code,
                            "response": data,
                        })
                        return data.get("output")
                    if data.get("msg_cd") == "EGW00201":
                        time.sleep(0.3 * (attempt + 1))
                        continue
                    # OPSQ2002 등 필드 규격 오류는 원인 파악을 돕기 위해 '민감정보 제외'한 요청 파라미터를 함께 기록한다.
                    try:
                        if str(data.get("msg_cd") or "").strip().upper() in ("OPSQ2002",):
                            px = params.get("OVRS_ORD_UNPR")
                            log.error(
                                f"[Order] 매수가능금액조회 실패: {data.get('msg1')} ({data.get('msg_cd')}) "
                                f"[debug: OVRS_EXCG_CD={params.get('OVRS_EXCG_CD')}, ITEM_CD={params.get('ITEM_CD')}, "
                                f"OVRS_ORD_UNPR={px} (len={len(str(px))})]"
                            )
                        else:
                            log.error(f"[Order] 매수가능금액조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    except Exception:
                        log.error(f"[Order] 매수가능금액조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "api": "v1_014",
                        "method": "GET",
                        "url": url,
                        "headers": headers,
                        "params": params,
                        "status": res.status_code,
                        "response": data,
                    })
                    return _debug_error(msg_cd=data.get("msg_cd"), msg1=data.get("msg1"), http_status=200) or None

                if res.status_code == 500:
                    try:
                        data = res.json() or {}
                        if _retry_on_expired_token(data, mode, attempt, 2):
                            continue
                        if data.get("msg_cd") == "EGW00201":
                            time.sleep(0.3 * (attempt + 1))
                            continue
                        return _debug_error(msg_cd=data.get("msg_cd"), msg1=data.get("msg1"), http_status=500) or None
                    except Exception:
                        pass

                log.error(f"[Order] 매수가능금액조회 API 호출 오류: {res.status_code} - {res.text}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_014",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "status": res.status_code,
                    "response": res.text,
                })
                return _debug_error(http_status=res.status_code, detail=res.text) or None
            except Exception as e:
                if attempt < 1:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Order] 매수가능금액조회 중 예외 발생: {e}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "api": "v1_014",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "error": str(e),
                })
                return _debug_error(detail=str(e)) or None

    def get_order_history(
        self,
        start_date: str,
        end_date: str,
        pdno: str | None = None,
        sll_buy_dvsn: str = "00",
        ccld_nccs_dvsn: str = "00",
        ovrs_excg_cd: str | None = None,
        sort_sqn: str = "DS",
        mode: str | None = None,
        ctx_area_nk200: str = "",
        ctx_area_fk200: str = "",
        caller: str | None = None,
    ):
        """
        해외주식 주문체결내역 (v1_해외주식-007)

        - 모의투자는 제약이 많음(가이드): PDNO/OVRS_EXCG_CD/SLL_BUY_DVSN/CCLD_NCCS_DVSN 등이 전체조회만 가능.
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)

        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')

        cano, acnt_prdt_cd = self._get_account_info(mode)
        token = kis_auth.get_token(mode)

        if not token or not cano:
            log.error("[Order] 토큰 또는 계좌번호가 설정되지 않았습니다.")
            return None

        tr_id = "VTTS3035R" if mode == "mock" else "TTTS3035R"
        url = f"{url_base}/uapi/overseas-stock/v1/trading/inquire-ccnl"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id,
        }

        if mode == "mock":
            # 가이드 제약 준수
            pdno = ""  # 전체조회만 가능
            sll_buy_dvsn = "00"
            ccld_nccs_dvsn = "00"
            ovrs_excg_cd = ""  # 전체조회만 가능
            sort_sqn = "DS"
        else:
            if pdno is None:
                pdno = "%"
            if ovrs_excg_cd is None:
                ovrs_excg_cd = "%"

        all_rows: list[dict] = []
        last_output2 = None
        ctx_area_nk200 = (ctx_area_nk200 or "").strip()
        ctx_area_fk200 = (ctx_area_fk200 or "").strip()

        # 연속조회 루프
        tr_cont = ""
        for _ in range(20):
            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "PDNO": pdno,
                "ORD_STRT_DT": start_date,
                "ORD_END_DT": end_date,
                "SLL_BUY_DVSN": sll_buy_dvsn,
                "CCLD_NCCS_DVSN": ccld_nccs_dvsn,
                "OVRS_EXCG_CD": normalize_order_exchange(ovrs_excg_cd) if ovrs_excg_cd else ovrs_excg_cd,
                "SORT_SQN": sort_sqn,
                "ORD_DT": "",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "CTX_AREA_NK200": ctx_area_nk200,
                "CTX_AREA_FK200": ctx_area_fk200,
            }

            try:
                if tr_cont:
                    headers["tr_cont"] = tr_cont
                elif "tr_cont" in headers:
                    headers.pop("tr_cont", None)
                # 초당 제한(EGW00201) 발생 시 짧게 재시도
                data = None
                for attempt in range(3):
                    res = requests.get(url, headers=headers, params=params, timeout=20)
                    if res.status_code == 500:
                        # 초당 제한은 500으로 내려오는 케이스가 많음
                        try:
                            jd = res.json() or {}
                            if _retry_on_expired_token(jd, mode, attempt, 3):
                                continue
                            if jd.get("msg_cd") == "EGW00201":
                                time.sleep(0.3 * (attempt + 1))
                                continue
                        except Exception:
                            pass
                        log.error(f"[Order] 주문체결내역 API 호출 오류: {res.status_code} - {res.text}")
                        return None

                    if res.status_code != 200:
                        log.error(f"[Order] 주문체결내역 API 호출 오류: {res.status_code} - {res.text}")
                        return None

                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 3):
                        continue
                    if data.get("rt_cd") == "0":
                        _log_engine_api_if_needed(caller, mode, {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "api": "v1_007",
                            "method": "GET",
                            "url": url,
                            "headers": headers,
                            "params": params,
                            "status": res.status_code,
                            "response": data,
                            "tr_cont": res.headers.get("tr_cont"),
                        })
                        break

                    # vts/실전에서 초당 제한이 걸리면 재시도
                    if data.get("msg_cd") == "EGW00201":
                        time.sleep(0.3 * (attempt + 1))
                        continue

                    log.error(f"[Order] 주문체결내역 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "api": "v1_007",
                        "method": "GET",
                        "url": url,
                        "headers": headers,
                        "params": params,
                        "status": res.status_code,
                        "response": data,
                    })
                    return None

                if not data or data.get("rt_cd") != "0":
                    return None

                rows = data.get("output") or data.get("output1") or data.get("Output1") or []
                rows = rows if isinstance(rows, list) else [rows]
                all_rows.extend([r for r in rows if isinstance(r, dict)])

                last_output2 = data.get("output2") or data.get("Output2") or last_output2
                ctx_area_fk200 = (data.get("ctx_area_fk200") or "").strip()
                ctx_area_nk200 = (data.get("ctx_area_nk200") or "").strip()

                tr_cont = res.headers.get("tr_cont")  # F/M: next, D/E: last
                if tr_cont in ("D", "E") or (not ctx_area_fk200 and not ctx_area_nk200):
                    break

                continue
            except Exception as e:
                log.error(f"[Order] 주문체결내역 조회 중 예외 발생: {e}")
                return None

        # 하위 호환: 기존 호출부가 'output'을 참조하는 경우가 있어 함께 제공
        return {
            "output": all_rows,
            "output1": all_rows,
            "output2": last_output2,
            "ctx_area_fk200": ctx_area_fk200,
            "ctx_area_nk200": ctx_area_nk200,
        }

    def get_period_profit(
        self,
        start_date: str,
        end_date: str,
        exchange: str = "",
        currency_div: str = "01",
        mode: str | None = None,
    ):
        """
        해외주식 기간손익 (v1_해외주식-032) - 실전 전용

        - exchange: ""(전체) or NASD/SEHK/SHAA/TKSE/HASE 등 (가이드)
        - currency_div: 01(외화) / 02(원화)
        """
        if mode is None:
            mode = config_manager.get("common.mode", "mock")
        log = get_mode_logger(mode)

        if mode == "mock":
            log.warning("[Order] 기간손익 API는 모의투자를 지원하지 않습니다.")
            return None

        url_base = config_manager.get(f"{mode}.url_base")
        app_key = config_manager.get(f"{mode}.app_key")
        app_secret = config_manager.get(f"{mode}.app_secret")

        cano, acnt_prdt_cd = self._get_account_info(mode)
        token = kis_auth.get_token(mode)

        if not token or not cano:
            log.error("[Order] 토큰 또는 계좌번호가 설정되지 않았습니다.")
            return None

        url = f"{url_base}/uapi/overseas-stock/v1/trading/inquire-period-profit"
        tr_id = "TTTS3039R"

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id,
        }

        all_rows: list[dict] = []
        output2 = None
        ctx_area_nk200 = ""
        ctx_area_fk200 = ""

        # 연속조회 루프
        for _ in range(20):
            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "OVRS_EXCG_CD": normalize_order_exchange(exchange) if exchange else "",
                "NATN_CD": "",
                "CRCY_CD": "",
                "PDNO": "",
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "WCRC_FRCR_DVSN_CD": currency_div,
                "CTX_AREA_FK200": ctx_area_fk200,
                "CTX_AREA_NK200": ctx_area_nk200,
            }

            try:
                res = requests.get(url, headers=headers, params=params, timeout=20)
                if res.status_code != 200:
                    log.error(f"[Order] 기간손익 API 호출 오류: {res.status_code} - {res.text}")
                    return None

                data = res.json()
                if data.get("rt_cd") != "0":
                    log.error(f"[Order] 기간손익 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    return None

                rows = data.get("output1") or data.get("Output1") or []
                rows = rows if isinstance(rows, list) else [rows]
                all_rows.extend([r for r in rows if isinstance(r, dict) and r.get("trad_day")])

                output2 = data.get("output2") or data.get("Output2") or output2
                ctx_area_fk200 = (data.get("ctx_area_fk200") or "").strip()
                ctx_area_nk200 = (data.get("ctx_area_nk200") or "").strip()

                tr_cont = res.headers.get("tr_cont")  # F/M: next, D/E: last
                if tr_cont in ("D", "E") or (not ctx_area_fk200 and not ctx_area_nk200):
                    break

                # 다음 조회
                continue
            except Exception as e:
                log.error(f"[Order] 기간손익 조회 중 예외 발생: {e}")
                return None

        return {"output1": all_rows, "output2": output2}

kis_order = KisOrder()

