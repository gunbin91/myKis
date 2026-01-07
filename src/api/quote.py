import requests
import json
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
        
        try:
            res = requests.get(url, headers=headers, params=params)
            if res.status_code == 200:
                data = res.json()
                if data['rt_cd'] == '0':
                    return data['output']
                else:
                    log.error(f"[Quote] 현재가 조회 실패: {data['msg1']} ({data['msg_cd']})")
                    return None
            else:
                log.error(f"[Quote] API 호출 오류: {res.status_code} - {res.text}")
                return None
        except Exception as e:
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

