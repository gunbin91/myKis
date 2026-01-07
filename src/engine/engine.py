import requests
from datetime import datetime, time
from datetime import timedelta
from src.config.config_manager import config_manager
from src.api.order import kis_order
from src.api.quote import kis_quote
from src.utils.logger import logger, get_mode_logger
from src.engine.position_store import PositionStore

class TradingEngine:
    def __init__(self):
        self.is_running = False
        self.last_run_at = None
        self.last_error = None
        self.last_stop_watch_at = None
        self.last_stop_watch_error = None
        self._stop_loss_cooldown = {}  # symbol -> datetime
        self._last_scheduled_run_day = {}  # mode -> YYYYMMDD

    def is_market_open(self):
        """
        미국 주식 시장 거래 시간 체크 (한국 시간 기준)
        정규장: 23:30 ~ 06:00 (썸머타임 22:30 ~ 05:00)
        간단하게 22:00 ~ 07:00 범위를 허용하여 프리/애프터 마켓 일부 포함 및 오차 허용
        """
        now = datetime.now()
        current_time = now.time()

        # 주말 차단 (한국시간 기준)
        # 0=월 ... 5=토 6=일
        if now.weekday() >= 5:
            return False
        
        # 오후 10시(22:00) ~ 오전 7시(07:00)
        start_time = time(22, 0)
        end_time = time(7, 0)
        
        # 자정을 넘기는 시간대 체크 로직
        if start_time <= current_time or current_time <= end_time:
            return True
        
        return False

    def get_analysis_data(self):
        """분석 서버에서 매수/매도 리스트 가져오기"""
        # host/port를 분리 저장하고, 경로는 고정(/analysis)
        host = config_manager.get("common.analysis_host")
        port = config_manager.get("common.analysis_port")
        path = "/analysis"
        url = None
        if host and port:
            url = f"http://{host}:{int(port)}{path}"
        else:
            # legacy
            url = config_manager.get('common.analysis_url')
        mock_enabled = bool(config_manager.get("common.analysis_mock_enabled", False))

        if mock_enabled:
            # 설정파일에 없더라도 코드에서 안전하게 토글 가능
            return {
                "buy": [{"code": "TSLA", "exchange": "NAS"}],
                "sell": []
            }
        if not url:
            logger.warning("[Engine] 분석 서버 URL이 설정되지 않았습니다.")
            return {"buy": [], "sell": []}

        try:
            # 간단 재시도(네트워크 순간 장애 방어)
            for i in range(3):
                res = requests.get(url, timeout=3)
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"[Engine] 분석 서버 응답 오류: {res.status_code}")
        except Exception as e:
            logger.warning(f"[Engine] 분석 서버 연결 실패: {e}")

        return {"buy": [], "sell": []}

    def _run_core(self, mode: str, analysis_data: dict | None, ignore_auto_enabled: bool):
        """주기적으로 실행될 메인 로직"""
        if self.is_running:
            logger.warning("[Engine] 이전 작업이 아직 진행 중입니다.")
            return

        self.is_running = True
        self.last_run_at = datetime.now()
        self.last_error = None
        try:
            log = get_mode_logger(mode)
            strategy = config_manager.get(f'{mode}.strategy', {})
            auto_enabled = config_manager.get(f'{mode}.auto_trading_enabled', False)
            schedule_time = config_manager.get(f"{mode}.schedule_time", "00:00") or "00:00"

            if (not ignore_auto_enabled) and (not auto_enabled):
                log.info(f"[Engine] 자동매매 OFF 상태입니다. (mode={mode})")
                return

            # 실행시간 스케줄(1일 1회): 자동매매 ON이면 항상 지정 시각에만 실행
            if (not ignore_auto_enabled):
                now = datetime.now()
                try:
                    hh, mm = str(schedule_time).split(":")
                    hh = int(hh); mm = int(mm)
                except Exception:
                    hh, mm = 0, 0

                if not (now.hour == hh and now.minute == mm):
                    return

                today = now.strftime("%Y%m%d")
                if self._last_scheduled_run_day.get(mode) == today:
                    return
            
            # 전략 파라미터 로드
            max_buy_amount = float(strategy.get('max_buy_amount', 1000))  # 총 매수 예산(USD)
            take_profit_pct = strategy.get('take_profit_pct', 5.0)
            stop_loss_pct = strategy.get('stop_loss_pct', 3.0)
            reserve_cash = float(strategy.get('reserve_cash', 0))  # reserved (USD)
            max_hold_days = int(strategy.get("max_hold_days", 0) or 0)
            # 시장가에 가깝게 체결시키기 위한 슬리피지(%) - 지정가만 사용하는 구조에서 체결률을 높이기 위함
            slippage_pct = float(strategy.get("slippage_pct", 0.5) or 0.5)
            
            log.info(f"=== 자동매매 엔진 실행 시작 ({mode} 모드) ===")
            log.info(f"전략: 1회매수 ${max_buy_amount}, 익절 {take_profit_pct}%, 손절 {stop_loss_pct}%")

            # 1. 거래 가능 시간 체크
            if not self.is_market_open():
                log.info("[Engine] 현재 거래 가능 시간이 아닙니다. (22:00 ~ 07:00)")
                return

            # 2. 잔고 조회
            balance_info = kis_order.get_balance(mode=mode)
            if not balance_info:
                log.error("[Engine] 잔고 조회 실패로 중단")
                self.is_running = False
                return

            output1 = balance_info.get('output1', [])
            # 보유 종목 정보 파싱
            my_stocks = {}
            store = PositionStore(mode)
            for stock in output1:
                if not stock['ovrs_pdno']: continue
                
                symbol = stock['ovrs_pdno']
                qty = int(stock['ovrs_cblc_qty'])
                profit_rate = float(stock['evlu_pfls_rt'])
                exch = stock.get("ovrs_excg_cd") or "NASD"
                
                if qty > 0:
                    my_stocks[symbol] = {
                        'qty': qty,
                        'profit_rate': profit_rate,
                        'name': stock['ovrs_item_name'],
                        'exchange': exch,
                    }

                # 보유기간 추적(최초 감지일/추가매수 시점 기록)
                store.upsert(symbol=symbol, qty=qty, exchange=exch)

            # 잔고에 없는 종목은 store에서도 정리
            for sym in store.all_symbols():
                if sym not in my_stocks:
                    store.upsert(symbol=sym, qty=0)

            # 3. 분석 데이터 수신 (즉시실행/미리보기에서 전달되면 그것을 사용)
            if analysis_data is None:
                analysis_data = self.get_analysis_data()
            buy_list = analysis_data.get('buy', [])
            sell_list = analysis_data.get('sell', [])
            
            log.info(f"[Engine] 분석 데이터 - Buy: {len(buy_list)}, Sell: {len(sell_list)}")

            # 4. 매도 실행 (전략 매도 + 분석 매도)
            # 4-1. 익절/손절 감시
            for symbol, info in my_stocks.items():
                profit_rate = info['profit_rate']
                qty = info['qty']
                
                # 익절 조건
                if profit_rate >= take_profit_pct:
                    log.info(f"[Engine] 익절 조건 만족: {symbol} ({profit_rate}% >= {take_profit_pct}%)")
                    # 지정가(현재가 근사)로 매도 (price=0은 지정가에서 실패 가능)
                    px = kis_quote.get_current_price(info.get("exchange","NASD"), symbol, mode=mode) or {}
                    sell_price = float(px.get("last", 0) or 0)
                    if sell_price <= 0:
                        log.warning(f"[Engine] {symbol} 매도가 산출 실패(현재가 0)로 익절 매도 스킵")
                    else:
                        # 매도는 체결 우선 -> 현재가 대비 소폭 낮게
                        sell_price = sell_price * (1.0 - (slippage_pct / 100.0))
                        kis_order.order(symbol, qty, sell_price, 'sell', exchange=info.get("exchange","NASD"), order_type='00', mode=mode)
                    # 매도했으므로 my_stocks에서 제거해야 중복 매도 방지되나, API 호출 텀이 있으므로 생략
                    continue
                    
                # 손절 조건 (음수 비교 주의)
                if profit_rate <= -stop_loss_pct:
                    log.info(f"[Engine] 손절 조건 만족: {symbol} ({profit_rate}% <= -{stop_loss_pct}%)")
                    px = kis_quote.get_current_price(info.get("exchange","NASD"), symbol, mode=mode) or {}
                    sell_price = float(px.get("last", 0) or 0)
                    if sell_price <= 0:
                        log.warning(f"[Engine] {symbol} 매도가 산출 실패(현재가 0)로 손절 매도 스킵")
                    else:
                        sell_price = sell_price * (1.0 - (slippage_pct / 100.0))
                        kis_order.order(symbol, qty, sell_price, 'sell', exchange=info.get("exchange","NASD"), order_type='00', mode=mode)
                    continue

                # 보유기간 초과 강제매도 (로컬 추적 기반)
                if max_hold_days > 0:
                    open_date = store.get_open_date(symbol)
                    if open_date and len(open_date) == 8:
                        try:
                            od = datetime.strptime(open_date, "%Y%m%d").date()
                            days_held = (datetime.now().date() - od).days
                            if days_held >= max_hold_days:
                                log.info(f"[Engine] 보유기간 초과 매도: {symbol} ({days_held}d >= {max_hold_days}d)")
                                px = kis_quote.get_current_price(info.get("exchange","NASD"), symbol, mode=mode) or {}
                                sell_price = float(px.get("last", 0) or 0)
                                if sell_price <= 0:
                                    log.warning(f"[Engine] {symbol} 매도가 산출 실패(현재가 0)로 보유기간 매도 스킵")
                                else:
                                    sell_price = sell_price * (1.0 - (slippage_pct / 100.0))
                                    kis_order.order(symbol, qty, sell_price, 'sell', exchange=info.get("exchange","NASD"), order_type='00', mode=mode)
                        except Exception:
                            pass

            # 4-2. 분석 리스트 매도 (여기서는 exchange 정보가 없어도 보유종목 매도라 상관없음)
            for item in sell_list:
                # item: "TSLA" 또는 {"code":"TSLA","exchange":"NAS"}
                if isinstance(item, dict):
                    symbol = (item.get('code') or '').strip().upper()
                    exchange = (item.get('exchange') or 'NASD').strip().upper()
                else:
                    symbol = (str(item) or '').strip().upper()
                    exchange = 'NASD'

                if symbol in my_stocks:
                    qty = my_stocks[symbol]['qty']
                    exchange = my_stocks[symbol].get("exchange") or exchange
                    log.info(f"[Engine] 분석 리스트 매도: {symbol} {qty}주 ({exchange})")
                    px = kis_quote.get_current_price(exchange, symbol, mode=mode) or {}
                    sell_price = float(px.get("last", 0) or 0)
                    if sell_price <= 0:
                        log.warning(f"[Engine] {symbol} 매도가 산출 실패(현재가 0)로 분석 매도 스킵")
                    else:
                        sell_price = sell_price * (1.0 - (slippage_pct / 100.0))
                        kis_order.order(symbol, qty, sell_price, 'sell', exchange=exchange, order_type='00', mode=mode)

            # 5. 매수 실행
            if not buy_list:
                logger.info("[Engine] 매수 대상 종목이 없습니다.")
            else:
                # 분석 서버 포맷 지원:
                # 1) ["TSLA","AAPL"]
                # 2) [{"code":"TSLA","exchange":"NAS"}, ...]
                normalized_buy = []
                for item in buy_list:
                    if isinstance(item, dict):
                        code = (item.get('code') or '').strip().upper()
                        exchange = (item.get('exchange') or 'NAS').strip().upper()
                    else:
                        code = (str(item) or '').strip().upper()
                        exchange = 'NAS'
                    if code:
                        normalized_buy.append({"code": code, "exchange": exchange})

                if not normalized_buy:
                    logger.info("[Engine] 매수 대상 종목이 없습니다.")
                    return

                # 총 매수 예산을 종목 수만큼 N분할
                per_stock_budget = max(0.0, max_buy_amount - reserve_cash) / len(normalized_buy)
                if per_stock_budget <= 0:
                    log.warning(f"[Engine] 매수 예산 부족: max_buy_amount={max_buy_amount}, reserve_cash={reserve_cash}")
                    return

                for item in normalized_buy:
                    symbol = item["code"]
                    exchange = item["exchange"]

                    # 이미 보유중이면 패스
                    if symbol in my_stocks:
                        log.info(f"[Engine] 이미 보유중인 종목입니다: {symbol}")
                        continue
                        
                    # 현재가 조회 (거래소 정보 포함)
                    price_info = kis_quote.get_current_price(exchange, symbol, mode=mode)
                    if not price_info:
                        log.warning(f"[Engine] {symbol} 시세 조회 실패")
                        continue
                        
                    current_price = float(price_info['last'])
                    if current_price <= 0:
                        log.warning(f"[Engine] {symbol} 현재가 0원")
                        continue
                        
                    # 1) 종목당 예산 기준 수량
                    qty_by_budget = int(per_stock_budget // current_price)
                    if qty_by_budget <= 0:
                        log.info(f"[Engine] 예산 부족으로 매수 불가: {symbol} (필요: {current_price}, 예산: {per_stock_budget})")
                        continue

                    # 2) KIS 매수가능금액조회 기준 최대수량
                    ps = kis_order.get_buyable_amount(exchange=exchange, symbol=symbol, price=current_price, mode=mode)
                    max_ps_qty = None
                    try:
                        if ps and ps.get("max_ord_psbl_qty"):
                            max_ps_qty = int(float(ps["max_ord_psbl_qty"]))
                        elif ps and ps.get("ord_psbl_qty"):
                            max_ps_qty = int(float(ps["ord_psbl_qty"]))
                    except Exception:
                        max_ps_qty = None

                    qty = qty_by_budget
                    if max_ps_qty is not None:
                        qty = min(qty, max_ps_qty)
                    
                    if qty > 0:
                        buy_price = current_price * (1.0 + (slippage_pct / 100.0))
                        log.info(f"[Engine] 매수 주문 실행: {symbol}({exchange}) {qty}주 (@{buy_price})")
                        kis_order.order(symbol, qty, buy_price, 'buy', exchange=exchange, order_type='00', mode=mode)
                    else:
                        log.info(f"[Engine] 매수가능수량 부족으로 매수 불가: {symbol} (예산수량={qty_by_budget}, 매수가능={max_ps_qty})")

            log.info("=== 자동매매 엔진 실행 완료 ===")

            # 스케줄 실행 기록 (성공/실패와 무관하게 중복 실행 방지 목적)
            if (not ignore_auto_enabled):
                self._last_scheduled_run_day[mode] = datetime.now().strftime("%Y%m%d")

        except Exception as e:
            log = get_mode_logger(config_manager.get('common.mode', 'mock'))
            log.error(f"[Engine] 실행 중 오류 발생: {e}")
            self.last_error = str(e)
            import traceback
            log.error(traceback.format_exc())
        finally:
            self.is_running = False

    def run(self):
        """스케줄러/자동 실행 (설정의 auto_trading_enabled를 따름)"""
        mode = config_manager.get('common.mode', 'mock')
        return self._run_core(mode=mode, analysis_data=None, ignore_auto_enabled=False)

    def run_once_with_analysis(self, analysis_data: dict, mode: str | None = None):
        """
        즉시 실행(미리보기 후 실행) 전용:
        - 분석 결과(analysis_data)를 그대로 사용
        - auto_trading_enabled가 OFF여도 1회 실행은 허용
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        return self._run_core(mode=mode, analysis_data=analysis_data, ignore_auto_enabled=True)

    def stop_loss_watch(self):
        """
        장중 손절 감시 (1분 주기)
        - myKiwoom-main의 장중 감시 UX를 단순화하여 1분마다 수행
        - 자동매매와 별개로 intraday_stop_loss.enabled가 ON일 때만 동작
        """
        mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)

        self.last_stop_watch_at = datetime.now()
        self.last_stop_watch_error = None

        try:
            intraday_cfg = config_manager.get(f"{mode}.intraday_stop_loss", {}) or {}
            intraday_enabled = bool(intraday_cfg.get("enabled", False))
            if not intraday_enabled:
                return

            if not self.is_market_open():
                return

            # 엔진이 돌고 있으면 중복 주문/충돌 방지
            if self.is_running:
                return

            strategy = config_manager.get(f'{mode}.strategy', {}) or {}
            # myKiwoom-main 호환: threshold_pct 사용. 기존 stop_loss_pct가 있으면 -abs로 마이그레이션 처리
            threshold_pct = intraday_cfg.get("threshold_pct", None)
            if threshold_pct is None and intraday_cfg.get("stop_loss_pct") is not None:
                try:
                    threshold_pct = -abs(float(intraday_cfg.get("stop_loss_pct")))
                except Exception:
                    threshold_pct = -7.0
            try:
                threshold_pct = float(threshold_pct)
            except Exception:
                threshold_pct = -7.0
            slippage_pct = float(strategy.get("slippage_pct", 0.5) or 0.5)

            balance_info = kis_order.get_balance(mode=mode)
            if not balance_info:
                return

            output1 = balance_info.get('output1', []) or []
            now = datetime.now()
            cooldown_td = timedelta(minutes=5)  # 같은 종목 반복 주문 방지

            for stock in output1:
                symbol = (stock.get('ovrs_pdno') or '').strip().upper()
                if not symbol:
                    continue

                try:
                    qty = int(float(stock.get('ovrs_cblc_qty') or 0))
                except Exception:
                    qty = 0
                if qty <= 0:
                    continue

                try:
                    profit_rate = float(stock.get('evlu_pfls_rt') or 0)
                except Exception:
                    profit_rate = 0.0

                # 장중 감시 조건: threshold_pct가 음수면 손절, 양수면 익절(둘 다 매도)
                if threshold_pct < 0:
                    if profit_rate > threshold_pct:
                        continue
                elif threshold_pct > 0:
                    if profit_rate < threshold_pct:
                        continue
                else:
                    continue

                # 쿨다운 체크
                last_sell = self._stop_loss_cooldown.get(symbol)
                if last_sell and (now - last_sell) < cooldown_td:
                    continue

                exchange = stock.get("ovrs_excg_cd") or "NASD"

                # 현재가 기반 지정가(체결 우선: -슬리피지)
                px = kis_quote.get_current_price(exchange, symbol, mode=mode) or {}
                sell_price = float(px.get("last", 0) or 0)
                if sell_price <= 0:
                    continue
                sell_price = sell_price * (1.0 - (slippage_pct / 100.0))

                log.info(f"[StopWatch] 장중 감시 매도: {symbol} qty={qty}, rate={profit_rate}%, threshold={threshold_pct}%, price={sell_price}")
                out = kis_order.order(symbol, qty, sell_price, 'sell', exchange=exchange, order_type='00', mode=mode)
                if out:
                    self._stop_loss_cooldown[symbol] = now

        except Exception as e:
            self.last_stop_watch_error = str(e)
            log.error(f"[StopWatch] 오류: {e}")

# 전역 인스턴스
trading_engine = TradingEngine()
