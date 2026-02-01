import requests
import json
import time
from src.config.config_manager import config_manager
from src.api.auth import kis_auth
from src.utils.logger import logger, get_mode_logger, log_engine_api
from src.api.exchange import normalize_quote_exchange, normalize_order_exchange


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

class KisQuote:
    def __init__(self):
        self._product_info_cache = {}
        self._product_info_ttl_sec = 24 * 60 * 60

    def _get_product_type_code(self, exchange: str) -> str | None:
        # order exchange 기준 매핑
        ex = normalize_order_exchange(exchange)
        return {
            "NASD": "512",
            "NYSE": "513",
            "AMEX": "529",
            "TKSE": "515",
            "SEHK": "501",
            "HASE": "507",
            "VNSE": "508",
            "SHAA": "551",
            "SZAA": "552",
        }.get(ex)

    def get_current_price(self, exchange, symbol, mode=None, caller: str | None = None):
        """
        해외주식 현재체결가 (v1_해외주식-009)
        :param exchange: 거래소코드 (NAS:나스닥, NYS:뉴욕, AMS:아멕스)
        :param symbol: 종목코드
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)
        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')
        
        token = kis_auth.get_token(mode)
        if not token:
            return None

        url = f"{url_base}/uapi/overseas-price/v1/quotations/price"
        
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": "HHDFS00000300"  # 실전/모의 동일
        }
        
        params = {
            "AUTH": "",
            "EXCD": normalize_quote_exchange(exchange),
            "SYMB": symbol
        }
        
        # 초당 제한(EGW00201) 발생 시 짧게 재시도(사용자 경험 개선)
        for attempt in range(3):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 3):
                        continue
                    if data.get('rt_cd') == '0':
                        _log_engine_api_if_needed(caller, mode, {
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "api": "v1_009",
                            "method": "GET",
                            "url": url,
                            "headers": headers,
                            "params": params,
                            "status": res.status_code,
                            "response": data,
                        })
                        return data.get('output')
                    log.error(f"[Quote] 현재가 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "api": "v1_009",
                        "method": "GET",
                        "url": url,
                        "headers": headers,
                        "params": params,
                        "status": res.status_code,
                        "response": data,
                    })
                    return None

                # 500이면서 초당 제한이면 backoff 후 재시도
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

                log.error(f"[Quote] API 호출 오류: {res.status_code} - {res.text}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "api": "v1_009",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "status": res.status_code,
                    "response": res.text,
                })
                return None
            except Exception as e:
                # 네트워크 순간 오류는 짧게 재시도
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Quote] 현재가 조회 중 예외 발생: {e}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "api": "v1_009",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "error": str(e),
                })
                return None

    def get_price_detail(self, exchange, symbol, mode=None):
        """
        해외주식 현재가상세 (v1_해외주식-029)
        :param exchange: 거래소코드 (NAS:나스닥, NYS:뉴욕, AMS:아멕스)
        :param symbol: 종목코드
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)
        
        # 모의투자는 미지원
        if mode == 'mock':
            log.warning("[Quote] 현재가상세 API는 모의투자를 지원하지 않습니다.")
            return None

        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')
        
        token = kis_auth.get_token(mode)
        if not token:
            return None

        url = f"{url_base}/uapi/overseas-price/v1/quotations/price-detail"
        
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": "HHDFS76200200",
        }
        
        params = {
            "AUTH": "",
            "EXCD": normalize_quote_exchange(exchange),
            "SYMB": symbol
        }
        
        # 초당 제한(EGW00201) 발생 시 짧게 재시도
        for attempt in range(3):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 3):
                        continue
                    if data.get('rt_cd') == '0':
                        return data.get('output')
                    if data.get("msg_cd") == "EGW00201":
                        time.sleep(0.3 * (attempt + 1))
                        continue
                    log.error(f"[Quote] 상세 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
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

                log.error(f"[Quote] API 호출 오류: {res.status_code} - {res.text}")
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Quote] 상세 조회 중 예외 발생: {e}")
                return None

    def get_asking_price(self, exchange, symbol, mode=None, caller: str | None = None):
        """
        해외주식 현재가 호가 (해외주식-033)

        - URL: /uapi/overseas-price/v1/quotations/inquire-asking-price
        - 실전 전용(가이드: 모의 미지원)
        - output2에 pask1..pask10 / vask1..vask10 등이 포함(미국은 10호가)
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)

        if mode == 'mock':
            log.warning("[Quote] 현재가 호가 API(해외주식-033)는 모의투자를 지원하지 않습니다.")
            return None

        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')

        token = kis_auth.get_token(mode)
        if not token:
            return None

        url = f"{url_base}/uapi/overseas-price/v1/quotations/inquire-asking-price"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": "HHDFS76200100",
            # 가이드상 필수. 개인계좌 기준 P 사용.
            "custtype": "P",
        }
        params = {
            "AUTH": "",
            "EXCD": normalize_quote_exchange(exchange),
            "SYMB": symbol,
        }

        # 초당 제한(EGW00201) 발생 시 짧게 재시도
        for attempt in range(3):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 3):
                        continue
                    if data.get("rt_cd") == "0":
                        _log_engine_api_if_needed(caller, mode, {
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "api": "v1_033",
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
                    log.error(f"[Quote] 호가 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    _log_engine_api_if_needed(caller, mode, {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "api": "v1_033",
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

                log.error(f"[Quote] API 호출 오류: {res.status_code} - {res.text}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "api": "v1_033",
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
                log.error(f"[Quote] 호가 조회 중 예외 발생: {e}")
                _log_engine_api_if_needed(caller, mode, {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "api": "v1_033",
                    "method": "GET",
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "error": str(e),
                })
                return None

    def get_product_info(self, exchange, symbol, mode=None):
        """
        해외주식 상품기본정보 (v1_해외주식-034) - 실전 전용
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)

        if mode == 'mock':
            log.warning("[Quote] 상품기본정보 API(해외주식-034)는 모의투자를 지원하지 않습니다.")
            return None

        prdt_type_cd = self._get_product_type_code(exchange)
        if not prdt_type_cd:
            log.error(f"[Quote] 상품유형코드 매핑 실패: exchange={exchange}")
            return None

        url_base = config_manager.get(f'{mode}.url_base')
        app_key = config_manager.get(f'{mode}.app_key')
        app_secret = config_manager.get(f'{mode}.app_secret')
        token = kis_auth.get_token(mode)
        if not token:
            return None

        url = f"{url_base}/uapi/overseas-price/v1/quotations/search-info"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": token,
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": "CTPF1702R",
            "custtype": "P",
        }
        params = {
            "PRDT_TYPE_CD": prdt_type_cd,
            "PDNO": symbol,
        }

        for attempt in range(3):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    if _retry_on_expired_token(data, mode, attempt, 3):
                        continue
                    if data.get("rt_cd") == "0":
                        return data.get("output")
                    if data.get("msg_cd") == "EGW00201":
                        time.sleep(0.3 * (attempt + 1))
                        continue
                    log.error(f"[Quote] 상품기본정보 실패: {data.get('msg1')} ({data.get('msg_cd')})")
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

                log.error(f"[Quote] 상품기본정보 API 호출 오류: {res.status_code} - {res.text}")
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Quote] 상품기본정보 조회 중 예외 발생: {e}")
                return None

    def get_std_pdno(self, exchange, symbol, mode=None):
        """
        상품기본정보에서 std_pdno(12자리) 조회 (실전 전용)
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        if mode == "mock":
            return None

        key = (mode, normalize_order_exchange(exchange), str(symbol).strip().upper())
        cached = self._product_info_cache.get(key)
        if cached and (time.time() - cached.get("ts", 0)) < self._product_info_ttl_sec:
            return cached.get("std_pdno")

        info = self.get_product_info(exchange, symbol, mode=mode)
        if not info:
            return None
        std_pdno = (info.get("std_pdno") or "").strip()
        if std_pdno:
            self._product_info_cache[key] = {"std_pdno": std_pdno, "ts": time.time()}
        return std_pdno

kis_quote = KisQuote()

