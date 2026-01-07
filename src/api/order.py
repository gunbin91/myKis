import requests
import json
from datetime import datetime
from src.config.config_manager import config_manager
from src.api.auth import kis_auth
from src.utils.logger import logger, get_mode_logger
from src.api.exchange import normalize_order_exchange

class KisOrder:
    def __init__(self):
        pass

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

    def get_balance(self, exchange='NASD', currency='USD', mode=None):
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
        
        try:
            res = requests.get(url, headers=headers, params=params)
            if res.status_code == 200:
                data = res.json()
                if data['rt_cd'] == '0':
                    return data
                else:
                    log.error(f"[Order] 잔고 조회 실패: {data['msg1']} ({data['msg_cd']})")
                    return None
            else:
                log.error(f"[Order] API 호출 오류: {res.status_code} - {res.text}")
                return None
        except Exception as e:
            log.error(f"[Order] 잔고 조회 중 예외 발생: {e}")
            return None

    def get_unfilled_orders(self, exchange='NASD', mode=None):
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
        
        try:
            res = requests.get(url, headers=headers, params=params)
            if res.status_code == 200:
                data = res.json()
                if data['rt_cd'] == '0':
                    return data['output']
                else:
                    log.error(f"[Order] 미체결 조회 실패: {data['msg1']} ({data['msg_cd']})")
                    return None
            else:
                log.error(f"[Order] API 호출 오류: {res.status_code} - {res.text}")
                return None
        except Exception as e:
            log.error(f"[Order] 미체결 조회 중 예외 발생: {e}")
            return None

    def order(self, symbol, quantity, price, side='buy', exchange='NASD', order_type='00', mode=None):
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
            "OVRS_ORD_UNPR": str(price),
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": order_type
        }

        try:
            log.info(f"[Order] 주문 요청: {side} {symbol} {quantity}주 @ {price} ({order_type})")
            res = requests.post(url, headers=headers, data=json.dumps(body))

            if res.status_code == 200:
                data = res.json()
                if data.get('rt_cd') == '0':
                    log.info(f"[Order] 주문 성공: {data.get('msg1')} (주문번호: {data.get('output', {}).get('ODNO')})")
                    return data.get('output')
                log.error(f"[Order] 주문 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                return None

            log.error(f"[Order] API 호출 오류: {res.status_code} - {res.text}")
            return None
        except Exception as e:
            log.error(f"[Order] 주문 요청 중 예외 발생: {e}")
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
            "OVRS_ORD_UNPR": str(price),
            "ORD_SVR_DVSN_CD": "0",
        }

        try:
            res = requests.post(url, headers=headers, data=json.dumps(body))
            if res.status_code != 200:
                log.error(f"[Order] 정정/취소 API 호출 오류: {res.status_code} - {res.text}")
                return None

            data = res.json()
            if data.get("rt_cd") == "0":
                return data.get("output")
            log.error(f"[Order] 정정/취소 실패: {data.get('msg1')} ({data.get('msg_cd')})")
            return None
        except Exception as e:
            log.error(f"[Order] 정정/취소 중 예외 발생: {e}")
            return None

    def get_buyable_amount(
        self,
        exchange: str,
        symbol: str,
        price: float,
        mode: str | None = None,
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
        }

        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": normalize_order_exchange(exchange),
            "OVRS_ORD_UNPR": str(price),
            "ITEM_CD": symbol,
        }

        try:
            res = requests.get(url, headers=headers, params=params)
            if res.status_code != 200:
                log.error(f"[Order] 매수가능금액조회 API 호출 오류: {res.status_code} - {res.text}")
                return None

            data = res.json()
            if data.get("rt_cd") == "0":
                return data.get("output")
            log.error(f"[Order] 매수가능금액조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
            return None
        except Exception as e:
            log.error(f"[Order] 매수가능금액조회 중 예외 발생: {e}")
            return None

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
                res = requests.get(url, headers=headers, params=params)
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
            res = requests.get(url, headers=headers, params=params)
            if res.status_code != 200:
                log.error(f"[Order] 주문체결내역 API 호출 오류: {res.status_code} - {res.text}")
                return None

            data = res.json()
            if data.get("rt_cd") == "0":
                # 연속조회 여부는 응답 헤더에 존재
                data["_tr_cont"] = res.headers.get("tr_cont")
                return data
            log.error(f"[Order] 주문체결내역 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
            return None
        except Exception as e:
            log.error(f"[Order] 주문체결내역 조회 중 예외 발생: {e}")
            return None

        # TR_ID 결정 (미국 주식 기준)
        # (잘못 삽입된 주문 로직 블록 제거됨)

kis_order = KisOrder()

