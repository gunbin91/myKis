import requests
import json
import time
from src.config.config_manager import config_manager
from src.api.auth import kis_auth
from src.utils.logger import logger, get_mode_logger
from src.api.exchange import normalize_quote_exchange

class KisQuote:
    def __init__(self):
        pass

    def get_current_price(self, exchange, symbol, mode=None):
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
                res = requests.get(url, headers=headers, params=params)
                if res.status_code == 200:
                    data = res.json()
                    if data.get('rt_cd') == '0':
                        return data.get('output')
                    log.error(f"[Quote] 현재가 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    return None

                # 500이면서 초당 제한이면 backoff 후 재시도
                if res.status_code == 500:
                    try:
                        data = res.json() or {}
                        if data.get("msg_cd") == "EGW00201":
                            time.sleep(0.3 * (attempt + 1))
                            continue
                    except Exception:
                        pass

                log.error(f"[Quote] API 호출 오류: {res.status_code} - {res.text}")
                return None
            except Exception as e:
                # 네트워크 순간 오류는 짧게 재시도
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                log.error(f"[Quote] 현재가 조회 중 예외 발생: {e}")
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

    def get_asking_price(self, exchange, symbol, mode=None):
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
                res = requests.get(url, headers=headers, params=params, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    if data.get("rt_cd") == "0":
                        return data
                    if data.get("msg_cd") == "EGW00201":
                        time.sleep(0.3 * (attempt + 1))
                        continue
                    log.error(f"[Quote] 호가 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                    return None

                if res.status_code == 500:
                    try:
                        data = res.json() or {}
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
                log.error(f"[Quote] 호가 조회 중 예외 발생: {e}")
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
            "tr_id": "HHDFS76200200"
        }
        
        params = {
            "AUTH": "",
            "EXCD": normalize_quote_exchange(exchange),
            "SYMB": symbol
        }
        
        try:
            res = requests.get(url, headers=headers, params=params)
            if res.status_code == 200:
                data = res.json()
                if data['rt_cd'] == '0':
                    return data['output']
                else:
                    log.error(f"[Quote] 상세 조회 실패: {data['msg1']} ({data['msg_cd']})")
                    return None
            else:
                log.error(f"[Quote] API 호출 오류: {res.status_code} - {res.text}")
                return None
        except Exception as e:
            log.error(f"[Quote] 상세 조회 중 예외 발생: {e}")
            return None

kis_quote = KisQuote()

