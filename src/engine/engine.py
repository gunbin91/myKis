import requests
import time as time_module
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from src.config.config_manager import config_manager
from src.api.order import kis_order
from src.api.quote import kis_quote
from src.utils.logger import logger, get_mode_logger
from src.utils.fx_rate import get_usd_krw_rate
from src.engine.position_store import PositionStore
from src.engine.run_state_store import RunStateStore

class TradingEngine:
    def __init__(self):
        self.is_running = False
        self.last_run_at = None
        self.last_error = None
        self.last_stop_watch_at = None
        self.last_stop_watch_error = None
        self._stop_loss_cooldown = {}  # symbol -> datetime
        self._last_scheduled_run_day = {}  # mode -> YYYYMMDD
        self._run_state_store = {}  # mode -> RunStateStore

    def _get_run_state_store(self, mode: str) -> RunStateStore:
        if mode not in self._run_state_store:
            self._run_state_store[mode] = RunStateStore(mode=mode)
        return self._run_state_store[mode]

    def _get_last_scheduled_run_day(self, mode: str) -> str | None:
        # 메모리 캐시 우선, 없으면 파일에서 로드
        if mode in self._last_scheduled_run_day:
            return self._last_scheduled_run_day.get(mode)
        day = self._get_run_state_store(mode).get_last_scheduled_run_day()
        if day:
            self._last_scheduled_run_day[mode] = day
        return day

    def _mark_scheduled_run_day(self, mode: str, day: str) -> None:
        # 프로세스 재시작에도 중복 실행 방지되도록 파일에도 저장
        self._last_scheduled_run_day[mode] = day
        self._get_run_state_store(mode).set_last_scheduled_run_day(day)

    def is_market_open(self):
        """
        미국 주식 시장 거래 시간 체크 (한국 시간 기준)

        - 정규장(대략): (겨울) 23:30 ~ 06:00 / (서머타임) 22:30 ~ 05:00
        - 핵심: 금요일 장이 한국 토요일 새벽까지 이어지므로,
          "한국시간 weekday 기준 주말 차단"을 그대로 쓰면 토요일 새벽을 잘못 close로 표기한다.
        - 따라서 자정(00:00)~마감(end) 구간은 '전날'의 weekday로 판단한다.

        ※ 휴장일(미국 공휴일)까지는 반영하지 않음.
        """
        now_utc = datetime.now(timezone.utc)
        now_kst = now_utc.astimezone(ZoneInfo("Asia/Seoul"))
        now_ny = now_utc.astimezone(ZoneInfo("America/New_York"))

        # 서머타임 여부만 반영(휴장일은 반영 X)
        is_dst = bool(now_ny.dst()) and (now_ny.dst() != timedelta(0))
        start_time = time(22, 30) if is_dst else time(23, 30)
        end_time = time(5, 0) if is_dst else time(6, 0)

        t = now_kst.time()
        in_session = (t >= start_time) or (t <= end_time)
        if not in_session:
            return False

        # "토요일 새벽"을 금요일 장으로 판단하기 위한 기준일(effective_day)
        effective_day = now_kst.date()
        if t <= end_time:
            effective_day = (now_kst - timedelta(days=1)).date()

        # 0=월 ... 5=토 6=일 (effective_day 기준)
        if effective_day.weekday() >= 5:
            return False

        return True

    def get_analysis_data(self):
        """
        분석 서버에서 매수/매도 리스트 가져오기
        - 요구사항: 분석서버의 '실시간 분석 실행'을 호출한 뒤 결과를 받는다.
          (분석서버 구현체에 따라 /api/start_analysis 또는 /v1/analysis/run)
        - 결과 폴링: /v1/analysis 또는 /v1/analysis/result (서버별 상이) 지원
        """
        host = config_manager.get("common.analysis_host", "localhost")
        port = config_manager.get("common.analysis_port", 5500)
        base_url = f"http://{host}:{int(port)}" if (host and port) else None
        # 서버별 엔드포인트 호환:
        # - start: /api/start_analysis(비동기 시작) 또는 /v1/analysis/run(동기 실행일 수 있음)
        # - result: /v1/analysis 또는 /v1/analysis/result
        start_urls = []
        result_urls = []
        if base_url:
            # 우선 /api/start_analysis(비동기 시작)를 사용한다.
            # /v1/analysis/run 은 동기 실행(수분~수십분)일 수 있어 불필요한 timeout/warning을 유발하므로,
            # /api/start_analysis 가 없는 환경에서만 폴백으로 사용한다.
            start_urls = [f"{base_url}/api/start_analysis"]
            # 사용자 환경에서 /v1/analysis 로 바뀐 경우가 있어 우선순위로 둠
            result_urls = [f"{base_url}/v1/analysis", f"{base_url}/v1/analysis/result"]
        health_url = f"{base_url}/health" if base_url else None
        legacy_url = config_manager.get("common.analysis_url")
        mock_enabled = bool(config_manager.get("common.analysis_mock_enabled", False))

        if mock_enabled:
            # 설정파일에 없더라도 코드에서 안전하게 토글 가능
            return {
                "buy": [{"code": "TSLA", "exchange": "NAS"}],
                "sell": []
            }

        def _normalize_payload_to_buy_sell(payload: dict):
            # kiwoomDeepLearning 포맷:
            #  - {success: bool, data: {analysis_date,total_stocks,top_stocks,analysis_result}, ...}
            if isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("data"), dict):
                data = payload.get("data") or {}
                rows = data.get("top_stocks") or data.get("analysis_result") or []
                if not isinstance(rows, list):
                    rows = [rows]

                buy = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    code = (
                        r.get("종목코드")
                        or r.get("ticker")
                        or r.get("code")
                        or r.get("symbol")
                    )
                    if not code:
                        continue
                    code = str(code).strip().upper()
                    if not code:
                        continue
                    # 해외주식 자동매매(myKis) 기준 exchange는 NAS로 고정
                    buy.append({
                        "code": code,
                        "exchange": "NAS",
                        # UI/미리보기용 메타 (엔진 로직은 code/exchange만 사용)
                        "name": r.get("종목명") or r.get("name") or r.get("stock_name"),
                        "price": r.get("현재가") or r.get("price") or r.get("current_price"),
                        "score": r.get("최종점수") or r.get("score") or r.get("final_score"),
                        "prob": r.get("상승확률") or r.get("prob") or r.get("up_prob") or r.get("prob_up"),
                    })

                # UI/미리보기에서 분석 정보를 표시할 수 있도록 meta를 함께 반환
                return {
                    "buy": buy,
                    "sell": [],
                    "meta": {
                        "analysis_date": data.get("analysis_date"),
                        "total_stocks": data.get("total_stocks"),
                        "top_stocks": rows[:20] if isinstance(rows, list) else rows,
                        "raw": payload,
                    },
                }

            # 기존/사용자 커스텀 분석서버 포맷: {"buy":[...],"sell":[...]}
            if isinstance(payload, dict) and ("buy" in payload or "sell" in payload):
                return payload

            return None

        def _to_bool(v) -> bool:
            if v is True:
                return True
            if v is False or v is None:
                return False
            try:
                if isinstance(v, (int, float)):
                    return bool(int(v))
            except Exception:
                pass
            s = str(v).strip().lower()
            return s in ("true", "1", "y", "yes", "t")

        def _normalize_date(v: str | None) -> str | None:
            """
            - "YYYY-MM-DD" -> "YYYY-MM-DD"
            - "YYYYMMDD" -> "YYYY-MM-DD"
            - "YYYY년 MM월 DD일" -> "YYYY-MM-DD"
            """
            if not v:
                return None
            s = str(v).strip()
            if not s:
                return None
            # 1) YYYYMMDD
            digits = "".join([ch for ch in s if ch.isdigit()])
            if len(digits) == 8:
                return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
            # 2) YYYY-MM-DD (또는 YYYY.MM.DD 등)
            if len(s) >= 8:
                # 숫자/구분자만 남겨서 파싱 시도
                parts = []
                cur = ""
                for ch in s:
                    if ch.isdigit():
                        cur += ch
                    else:
                        if cur:
                            parts.append(cur)
                            cur = ""
                if cur:
                    parts.append(cur)
                if len(parts) >= 3 and len(parts[0]) == 4:
                    y = parts[0]
                    m = parts[1].zfill(2)
                    d = parts[2].zfill(2)
                    if len(y) == 4 and len(m) == 2 and len(d) == 2:
                        return f"{y}-{m}-{d}"
            return None

        try:
            analysis_date = datetime.now().strftime("%Y-%m-%d")

            # 실제로 존재하는 result 엔드포인트를 먼저 선택한다.
            # (사용자 환경에서 /v1/analysis 는 없고 /v1/analysis/result 만 있는 케이스가 흔함)
            chosen_result_url = None
            if base_url:
                for ru in [f"{base_url}/v1/analysis/result", f"{base_url}/v1/analysis"]:
                    try:
                        rr0 = requests.get(ru, timeout=2)
                        if rr0.status_code in (200, 404):
                            chosen_result_url = ru
                            break
                    except Exception:
                        continue

            # base_url이 없으면 legacy_url만 사용
            if not chosen_result_url and legacy_url:
                chosen_result_url = legacy_url

            # baseline(시작 전 결과) 확보: "완료"를 날짜가 아니라 running 플래그의 전후 변화로 판단하기 위함
            baseline = {"analysis_date": None, "buy_sell": None}
            if chosen_result_url:
                try:
                    brr = requests.get(chosen_result_url, timeout=3)
                    if brr.status_code == 200:
                        bp = brr.json() or {}
                        # baseline date
                        try:
                            got = None
                            if isinstance(bp, dict) and isinstance(bp.get("data"), dict):
                                got = bp["data"].get("analysis_date")
                            if got is None and isinstance(bp, dict):
                                got = bp.get("analysis_date")
                            baseline["analysis_date"] = _normalize_date(got)
                        except Exception:
                            pass
                        baseline["buy_sell"] = _normalize_payload_to_buy_sell(bp)
                except Exception:
                    pass

            # 1) 실시간 분석 시작
            start_ok = False
            start_not_found = False
            for su in start_urls:
                try:
                    # /v1/analysis/run 은 동기 수행일 수 있어 타임아웃이 정상일 수 있음 -> 무시하고 폴링로 진행
                    sr = requests.post(su, json={"analysis_date": analysis_date}, timeout=3)
                    if sr.status_code in (200, 202, 409):
                        start_ok = True
                        # /v1/analysis/run 이 즉시 결과를 반환하는 구현이면 여기서 바로 처리
                        if sr.status_code == 200:
                            try:
                                payload = sr.json() or {}
                                out = _normalize_payload_to_buy_sell(payload)
                                if out is not None:
                                    return out
                            except Exception:
                                pass
                    elif sr.status_code == 400:
                        # 일부 분석서버 구현은 "이미 분석 실행 중"을 400으로 반환한다.
                        # (예: {"error":"이미 분석이 실행 중입니다..."})
                        try:
                            j = sr.json() or {}
                            msg = str(j.get("error") or j.get("message") or "")
                            if ("이미" in msg) and ("실행" in msg):
                                start_ok = True
                            else:
                                logger.warning(f"[Engine] 분석 start 400: {su} -> {msg}")
                        except Exception:
                            logger.warning(f"[Engine] 분석 start 400: {su}")
                    else:
                        if sr.status_code == 404:
                            start_not_found = True
                        logger.warning(f"[Engine] 분석 start 응답 오류: {su} -> {sr.status_code}")
                except Exception as e:
                    logger.warning(f"[Engine] 분석 start 호출 실패(무시하고 폴링): {su} -> {e}")

            # /api/start_analysis 가 없다면(404) 동기 실행 엔드포인트로 폴백 시도
            if (not start_ok) and start_not_found and base_url:
                su = f"{base_url}/v1/analysis/run"
                try:
                    sr = requests.post(su, json={"analysis_date": analysis_date}, timeout=3)
                    if sr.status_code in (200, 202, 409):
                        start_ok = True
                        if sr.status_code == 200:
                            try:
                                payload = sr.json() or {}
                                out = _normalize_payload_to_buy_sell(payload)
                                if out is not None:
                                    return out
                            except Exception:
                                pass
                    elif sr.status_code == 400:
                        try:
                            j = sr.json() or {}
                            msg = str(j.get("error") or j.get("message") or "")
                            if ("이미" in msg) and ("실행" in msg):
                                start_ok = True
                        except Exception:
                            pass
                except Exception:
                    pass

            # 2) 결과 폴링: 핵심은 analysis_running이 true였다가 false로 떨어지는 순간을 "완료"로 본다.
            if not chosen_result_url:
                return {"buy": [], "sell": []}

            seen_running = False
            stable_success_cnt = 0
            for _ in range(600):  # 20분(2초 * 600)
                try:
                    rr = requests.get(chosen_result_url, timeout=3)
                    if rr.status_code != 200:
                        time_module.sleep(2)
                        continue

                    payload = rr.json() or {}

                    # running 판단: /health가 있으면 그것을 우선 사용(서버별 result 응답에 running이 없거나 부정확할 수 있음)
                    running = False
                    try:
                        if health_url:
                            hr = requests.get(health_url, timeout=2)
                            if hr.status_code == 200:
                                hj = hr.json() or {}
                                running = _to_bool(hj.get("analysis_running"))
                        if (not running) and isinstance(payload, dict):
                            running = _to_bool(payload.get("analysis_running"))
                    except Exception:
                        running = False

                    if running:
                        seen_running = True
                        time_module.sleep(2)
                        continue

                    out = _normalize_payload_to_buy_sell(payload)
                    if out is None:
                        # 포맷이 예상과 다르면 잠시 대기 후 재시도
                        time_module.sleep(2)
                        continue

                    # 날짜가 서버/데이터 특성상 "최근 거래일"로 내려오는 경우가 있어,
                    # 날짜 불일치만으로 무한 대기하지 않도록 한다.
                    got_date_norm = None
                    try:
                        got_raw = None
                        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                            got_raw = payload["data"].get("analysis_date")
                        if got_raw is None and isinstance(payload, dict):
                            got_raw = payload.get("analysis_date")
                        got_date_norm = _normalize_date(got_raw)
                    except Exception:
                        got_date_norm = None

                    baseline_date = baseline.get("analysis_date")
                    date_changed = bool(got_date_norm and baseline_date and got_date_norm != baseline_date)

                    # 완료 인정 조건:
                    # - 분석이 실제로 실행중이었다가 끝났거나(seen_running)
                    # - 또는 baseline 대비 결과가 바뀐 것으로 판단되거나(date_changed)
                    # - 또는 start_ok인데 결과를 2회 연속 성공적으로 읽었으면(실행이 빠르게 끝난 케이스)
                    if seen_running or date_changed:
                        return out

                    stable_success_cnt += 1
                    if start_ok and stable_success_cnt >= 2:
                        return out

                except Exception:
                    pass

                time_module.sleep(2)

            return {"buy": [], "sell": []}
        except Exception as e:
            logger.warning(f"[Engine] 분석 서버 연결 실패: {e}")
            return {"buy": [], "sell": []}

    def _run_core(self, mode: str, analysis_data: dict | None, ignore_auto_enabled: bool):
        """주기적으로 실행될 메인 로직"""
        if self.is_running:
            logger.warning("[Engine] 이전 작업이 아직 진행 중입니다.")
            return

        log = get_mode_logger(mode)
        strategy = config_manager.get(f'{mode}.strategy', {})
        auto_enabled = config_manager.get(f'{mode}.auto_trading_enabled', False)
        schedule_time = config_manager.get(f"{mode}.schedule_time", "00:00") or "00:00"

        # 자동매매 OFF면 실행 자체를 하지 않음 (last_run_at도 갱신하지 않음)
        if (not ignore_auto_enabled) and (not auto_enabled):
            return

        # 실행시간 스케줄(1일 1회): 자동매매 ON이면 항상 지정 시각에만 실행
        if (not ignore_auto_enabled):
            now = datetime.now()
            try:
                hh, mm = str(schedule_time).split(":")
                hh = int(hh); mm = int(mm)
            except Exception:
                hh, mm = 0, 0

            # 1분 주기 체크는 프로세스/네트워크 상황에 따라 약간 지연될 수 있어
            # 지정 시각 ±1분 범위를 허용한다(키움 샘플과 동일한 안정성 의도).
            try:
                target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                start = target - timedelta(minutes=1)
                end = target + timedelta(minutes=1)
                if not (start <= now <= end):
                    return
            except Exception:
                # 파싱/계산 실패 시 기존 보수적 동작 유지(정확히 같은 분에만 실행)
                if not (now.hour == hh and now.minute == mm):
                    return

            today = now.strftime("%Y%m%d")
            # 서버 재시작에도 중복 실행 방지: 파일 기반 상태 우선
            if self._get_last_scheduled_run_day(mode) == today:
                return

        # 여기부터는 "실제 1회 실행"에 해당 (상태 갱신)
        self.is_running = True
        self.last_run_at = datetime.now()
        self.last_error = None
        try:
            # 오늘 실행으로 마킹(실패해도 1일 1회 원칙 유지)
            if (not ignore_auto_enabled):
                self._mark_scheduled_run_day(mode, datetime.now().strftime("%Y%m%d"))
            
            # 전략 파라미터 로드
            try:
                top_n = int(strategy.get("top_n", 5) or 5)
            except Exception:
                top_n = 5
            if top_n <= 0:
                top_n = 5
            # 손절/익절은 프론트 입력값(양/음수) 그대로 사용한다.
            # 예) take_profit_pct=+5 => +5% 익절, stop_loss_pct=-3 => -3% 손절
            take_profit_pct = float(strategy.get("take_profit_pct", 5.0) or 0.0)
            stop_loss_pct = float(strategy.get("stop_loss_pct", -3.0) or 0.0)
            # 하위호환(구버전 설정): stop_loss_pct를 양수로 저장해둔 경우가 있어,
            # "손절은 -X%" 관례에 맞게 음수로 보정한다.
            if stop_loss_pct > 0:
                log.warning(f"[Engine] stop_loss_pct가 양수로 설정되어 있어 하위호환 보정합니다: {stop_loss_pct}% -> {-abs(stop_loss_pct)}%")
                stop_loss_pct = -abs(stop_loss_pct)
            # reserve_cash_krw: UI에서는 원화로 입력(사용자 혼동 최소화)
            # reserve_cash: 구버전(USD) 하위호환
            reserve_cash_krw = float(strategy.get("reserve_cash_krw", 0) or 0.0)
            reserve_cash_usd_legacy = float(strategy.get("reserve_cash", 0) or 0.0)
            # USD/KRW 환율(원/달러): 사용자 입력/설정값은 사용하지 않고, KIS → FinanceDataReader 순으로 자동 조회
            fx = get_usd_krw_rate(mode=mode)
            usd_krw_rate = fx.rate or 0.0
            usd_krw_rate_source = fx.source
            reserve_cash = (reserve_cash_krw / usd_krw_rate) if reserve_cash_krw > 0 else reserve_cash_usd_legacy
            max_hold_days = int(strategy.get("max_hold_days", 0) or 0)
            # 시장가에 가깝게 체결시키기 위한 슬리피지(%) - 지정가만 사용하는 구조에서 체결률을 높이기 위함
            slippage_pct = float(strategy.get("slippage_pct", 0.5) or 0.5)
            
            log.info(f"=== 자동매매 엔진 실행 시작 ({mode} 모드) ===")
            log.info(f"전략: top_n={top_n}, reserve_cash_usd≈${reserve_cash:.2f} (reserve_cash_krw={reserve_cash_krw:.0f}, usd_krw_rate={usd_krw_rate} [{usd_krw_rate_source}]), 익절 {take_profit_pct}%, 손절 {stop_loss_pct}%")

            # 환율 자동조회가 실패하면 "매수는 취소, 매도만 실행" 정책 적용
            allow_buy = True
            if usd_krw_rate <= 0:
                allow_buy = False
                log.warning("[Engine] USD/KRW 환율 자동조회 실패(KIS+FinanceDataReader). 매수는 취소(스킵)하고 매도 조건만 실행합니다.")

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
            held_symbols = []
            for stock in output1:
                if not stock['ovrs_pdno']: continue
                
                symbol = stock['ovrs_pdno']
                qty = int(stock['ovrs_cblc_qty'])
                profit_rate = float(stock['evlu_pfls_rt'])
                exch = stock.get("ovrs_excg_cd") or "NASD"
                
                if qty > 0:
                    held_symbols.append(symbol)
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

            # 보유기간 보정: 가능하면 v1_007(주문체결내역)로 "최초 매수 체결일"을 동기화한다.
            # (과도한 호출 방지: 1일 1회, PositionStore meta에 기록)
            try:
                today = datetime.now().strftime("%Y%m%d")
                # api_sync_day가 오늘이어도, open_date가 detect(임시값)로 남아있으면 다시 동기화한다.
                needs_sync = False
                if held_symbols and (store.get_api_sync_day() != today):
                    needs_sync = True
                if held_symbols and (not needs_sync):
                    for sym in held_symbols:
                        try:
                            if (store.get_open_date_source(sym) or "detect") != "api":
                                needs_sync = True
                                break
                        except Exception:
                            continue

                if held_symbols and needs_sync:
                    end = today
                    lookback_days = 30 if mode == "mock" else 365
                    start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
                    hist = kis_order.get_order_history(start_date=start, end_date=end, mode=mode) or {}
                    rows = hist.get("output") or hist.get("output1") or []
                    rows = rows if isinstance(rows, list) else [rows]

                    def _as_yyyymmdd(v: str | None) -> str | None:
                        if not v:
                            return None
                        vv = str(v).strip().replace("-", "").replace(".", "")
                        return vv if len(vv) == 8 and vv.isdigit() else None

                    def _is_buy(row: dict) -> bool:
                        v = str(
                            row.get("sll_buy_dvsn")
                            or row.get("sll_buy_dvsn_cd")
                            or row.get("sll_buy_dvsn_name")
                            or row.get("SLL_BUY_DVSN")
                            or ""
                        ).strip().lower()
                        if ("buy" in v) or ("매수" in v):
                            return True
                        if v in ("02", "2", "buy"):
                            return True
                        return False

                    def _filled_qty(row: dict) -> float:
                        for k in (
                            "ft_ccld_qty",
                            "ccld_qty",
                            "CCLD_QTY",
                            "ccld_qty1",
                            "ccld_qty2",
                            "tot_ccld_qty",
                            "tot_ccld_qty1",
                            "ft_ord_qty",
                        ):
                            if k in row and row.get(k) is not None:
                                try:
                                    return float(str(row.get(k)).replace(",", ""))
                                except Exception:
                                    pass
                        return 0.0

                    last_buy_date: dict[str, str] = {}
                    held_set = set(held_symbols)
                    for r in rows:
                        if not isinstance(r, dict):
                            continue
                        sym = (r.get("pdno") or r.get("PDNO") or r.get("ovrs_pdno") or "").strip().upper()
                        if not sym or sym not in held_set:
                            continue
                        if _filled_qty(r) <= 0:
                            continue
                        if not _is_buy(r):
                            continue
                        d = _as_yyyymmdd(
                            r.get("trad_day")
                            or r.get("TRAD_DAY")
                            or r.get("ord_dt")
                            or r.get("ORD_DT")
                            or r.get("ccld_dt")
                            or r.get("CCLD_DT")
                        )
                        if not d:
                            continue
                        cur = last_buy_date.get(sym)
                        if (cur is None) or (d > cur):
                            last_buy_date[sym] = d

                    updated_any = False
                    for sym, d in last_buy_date.items():
                        store.set_open_date(symbol=sym, open_date=d, source="api")
                        updated_any = True
                    # 동기화가 실제로 성공(업데이트 발생)했을 때만 api_sync_day 갱신
                    if updated_any:
                        store.set_api_sync_day(today)
            except Exception:
                pass

            # 3. 분석 데이터 수신 (즉시실행/미리보기에서 전달되면 그것을 사용)
            if analysis_data is None:
                analysis_data = self.get_analysis_data()
            buy_list = analysis_data.get('buy', [])
            sell_list = analysis_data.get('sell', [])
            
            log.info(f"[Engine] 분석 데이터 - Buy: {len(buy_list)}, Sell: {len(sell_list)}")

            # 4. 매도 실행 (전략 매도 + 분석 매도)
            # 4-1. 익절/손절 감시
            sell_orders_sent = 0
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
                        sell_orders_sent += 1
                    # 매도했으므로 my_stocks에서 제거해야 중복 매도 방지되나, API 호출 텀이 있으므로 생략
                    continue
                    
                # 손절 조건: 입력값 그대로 비교 (stop_loss_pct는 보통 음수)
                if profit_rate <= stop_loss_pct:
                    log.info(f"[Engine] 손절 조건 만족: {symbol} ({profit_rate}% <= {stop_loss_pct}%)")
                    px = kis_quote.get_current_price(info.get("exchange","NASD"), symbol, mode=mode) or {}
                    sell_price = float(px.get("last", 0) or 0)
                    if sell_price <= 0:
                        log.warning(f"[Engine] {symbol} 매도가 산출 실패(현재가 0)로 손절 매도 스킵")
                    else:
                        sell_price = sell_price * (1.0 - (slippage_pct / 100.0))
                        kis_order.order(symbol, qty, sell_price, 'sell', exchange=info.get("exchange","NASD"), order_type='00', mode=mode)
                        sell_orders_sent += 1
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
                                    sell_orders_sent += 1
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
                        sell_orders_sent += 1

            # 키움 패턴처럼: 매도가 있었다면 잠깐 대기 후(체결/예수금 반영) 매수 예산 산정 시 최신 잔고를 쓰도록 한다.
            present_after_sell = None
            if sell_orders_sent > 0:
                time_module.sleep(2.0)
                try:
                    present_after_sell = kis_order.get_present_balance(
                        natn_cd="000", tr_mket_cd="00", inqr_dvsn_cd="00", wcrc_frcr_dvsn_cd="02", mode=mode
                    )
                except Exception:
                    present_after_sell = None

            # 5. 매수 실행
            if not buy_list:
                logger.info("[Engine] 매수 대상 종목이 없습니다.")
            else:
                if not allow_buy:
                    log.info("[Engine] 매수 스킵: 환율 자동조회 실패로 매수 로직을 실행하지 않습니다.")
                    buy_list = []
                    # 매도는 이미 실행되었으므로 매수만 건너뛰고 종료
                    log.info("=== 자동매매 엔진 실행 완료 (sell-only: fx_rate_unavailable) ===")
                    return

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

                # 매수 대상 수를 top_n으로 제한
                # 중요: 이미 보유중인 종목이 섞여 있으면, 먼저 제외한 뒤 top_n을 다시 뽑아야
                # 실제 매수 종목 수가 top_n에 가깝게 나온다.
                candidates = [x for x in normalized_buy if x.get("code") and x.get("code") not in my_stocks][:top_n]

                # autokiwoomstock처럼 "1회 예산 입력"은 사용하지 않고
                # 계좌의 '총 주문가능금액(USD)' - reserve_cash(USD 환산) 를 이번 실행 예산으로 사용
                #
                # - 실전: 해외주식-035(해외증거금 통화별조회) USD의 itgr_ord_psbl_amt(통합주문가능금액) 우선
                # - 모의: 해외주식-035 미지원 -> v1_008 output3.frcr_use_psbl_amt(외화사용가능금액)로 대체
                orderable_cash = 0.0
                try:
                    if mode == "real":
                        fm = kis_order.get_foreign_margin(mode=mode) or {}
                        rows = fm.get("output") or []
                        rows = rows if isinstance(rows, list) else [rows]
                        usd = None
                        for r in rows:
                            if isinstance(r, dict) and (str(r.get("crcy_cd") or "").strip().upper() == "USD"):
                                usd = r
                                break
                        if usd and usd.get("itgr_ord_psbl_amt") is not None:
                            orderable_cash = float(str(usd.get("itgr_ord_psbl_amt") or 0).replace(",", ""))

                    if orderable_cash <= 0:
                        ps = (present_after_sell or kis_order.get_present_balance(
                            natn_cd="000", tr_mket_cd="00", inqr_dvsn_cd="00", wcrc_frcr_dvsn_cd="02", mode=mode
                        ) or {})
                        out2 = ps.get("output2") or []
                        out2 = out2 if isinstance(out2, list) else [out2]
                        usd_row = None
                        for r in out2:
                            if isinstance(r, dict) and (str(r.get("crcy_cd") or "").strip().upper() == "USD"):
                                usd_row = r
                                break
                        if usd_row:
                            v = usd_row.get("frcr_drwg_psbl_amt_1") or usd_row.get("frcr_dncl_amt_2") or 0
                            orderable_cash = float(str(v or 0).replace(",", ""))
                        if orderable_cash <= 0:
                            out3 = ps.get("output3") or {}
                            orderable_cash = float(str(out3.get("frcr_use_psbl_amt") or 0).replace(",", ""))
                        # mock 마지막 fallback: 총자산(원화)/환율로 "통합증거금 느낌"의 총가용 USD 추정
                        if mode == "mock" and orderable_cash <= 0:
                            try:
                                # 이미 산출한 자동 환율(없으면 설정값 fallback)을 사용
                                usd_krw_rate = float(str(usd_krw_rate or 1350.0).replace(",", ""))
                                tot_asst_krw = float(str((ps.get("output3") or {}).get("tot_asst_amt") or 0).replace(",", ""))
                                if usd_krw_rate > 0 and tot_asst_krw > 0:
                                    orderable_cash = tot_asst_krw / usd_krw_rate
                            except Exception:
                                pass
                except Exception:
                    orderable_cash = 0.0

                total_budget = max(0.0, orderable_cash - reserve_cash)
                per_stock_budget = total_budget / len(candidates) if candidates else 0.0
                if not candidates:
                    logger.info("[Engine] 매수 대상 종목이 없습니다. (보유종목 제외 후)")
                    return

                if per_stock_budget <= 0:
                    log.warning(f"[Engine] 매수 예산 부족: orderable_cash={orderable_cash}, reserve_cash_usd≈{reserve_cash:.2f}")
                    return

                for item in candidates:
                    symbol = item["code"]
                    exchange = item["exchange"]

                    # 이미 보유중이면 패스(이론상 위에서 걸러지지만 방어적으로 유지)
                    if symbol in my_stocks:
                        log.info(f"[Engine] 이미 보유중인 종목입니다: {symbol}")
                        continue

                    # KIS 레이트리밋 방지용 기본 스로틀(키움식 운영 가드)
                    time_module.sleep(0.25)
                        
                    # 현재가 조회 (거래소 정보 포함)
                    price_info = kis_quote.get_current_price(exchange, symbol, mode=mode)
                    if not price_info:
                        log.warning(f"[Engine] {symbol} 시세 조회 실패")
                        continue
                        
                    current_price = float(price_info['last'])
                    if current_price <= 0:
                        log.warning(f"[Engine] {symbol} 현재가 0원")
                        continue

                    # 연속 API 호출 간 간격 확보(EGW00201 완화)
                    time_module.sleep(0.25)
                        
                    # 1) 종목당 예산 기준 수량
                    qty_by_budget = int(per_stock_budget // current_price)
                    if qty_by_budget <= 0:
                        log.info(f"[Engine] 예산 부족으로 매수 불가: {symbol} (필요: {current_price}, 예산: {per_stock_budget})")
                        continue

                    # 2) KIS 매수가능금액조회 기준 최대수량
                    ps = kis_order.get_buyable_amount(exchange=exchange, symbol=symbol, price=current_price, mode=mode)
                    if ps is None:
                        # 정책: v1_014가 최종 실패하면 해당 종목 매수는 스킵 (부분체결/로그-잔고 불일치 방지)
                        log.warning(f"[Engine] 매수가능금액조회(v1_014) 최종 실패(EGW00201 등). {symbol} 매수 스킵")
                        continue
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
                    
                    # 매수 방식(키움 참고):
                    # - mock: 호가 API 미지원이므로 기존처럼 (현재가 + 슬리피지) 지정가
                    # - real: 매도 1호가부터 단계적으로 지정가 매수(미체결이면 다음 호가), 가드(최대 허용 프리미엄%) 적용
                    buy_order_method = (strategy.get("buy_order_method") or ("limit_ask_ladder" if mode == "real" else "limit_slippage")).strip()
                    limit_buy_max_premium_pct = float(strategy.get("limit_buy_max_premium_pct", 1.0) or 1.0)
                    limit_buy_max_levels = int(strategy.get("limit_buy_max_levels", 5) or 5)
                    limit_buy_step_wait_sec = float(strategy.get("limit_buy_step_wait_sec", 1.0) or 1.0)

                    def _find_unfilled_qty_by_odno(odno: str) -> int:
                        try:
                            if not odno:
                                return 0
                            rows = kis_order.get_unfilled_orders(exchange=exchange, mode=mode) or []
                            rows = rows if isinstance(rows, list) else [rows]
                            for r in rows:
                                if not isinstance(r, dict):
                                    continue
                                if str(r.get("odno") or "").strip() == str(odno).strip():
                                    try:
                                        return int(float(str(r.get("nccs_qty") or 0).replace(",", "")))
                                    except Exception:
                                        return 0
                            return 0
                        except Exception:
                            return 0

                    def _extract_asks(ob: dict) -> list[float]:
                        # 해외주식-033 output2는 array로 내려오지만 실질 payload는 1개 dict인 케이스가 흔함.
                        out2 = ob.get("output2") if isinstance(ob, dict) else None
                        d = None
                        if isinstance(out2, list) and out2:
                            d = out2[0] if isinstance(out2[0], dict) else None
                        elif isinstance(out2, dict):
                            d = out2
                        if not d:
                            return []
                        asks = []
                        for i in range(1, 11):
                            k = f"pask{i}"
                            v = d.get(k)
                            try:
                                p = float(str(v or 0).replace(",", ""))
                            except Exception:
                                p = 0.0
                            if p and p > 0:
                                asks.append(p)
                        return asks

                    def _buy_with_ask_ladder() -> tuple[bool, str]:
                        # 1) 호가 조회
                        ob = kis_quote.get_asking_price(exchange, symbol, mode=mode)
                        if not ob:
                            log.warning(f"[Engine] {symbol} 호가 조회 실패 → 슬리피지 지정가로 폴백")
                            return False, "ask_api_failed"
                        asks = _extract_asks(ob)
                        if not asks:
                            log.warning(f"[Engine] {symbol} 호가 데이터 없음 → 슬리피지 지정가로 폴백")
                            return False, "asks_empty"

                        max_price = current_price * (1.0 + (limit_buy_max_premium_pct / 100.0)) if current_price > 0 else 0.0
                        remaining = int(qty)
                        used_levels = max(1, min(int(limit_buy_max_levels), len(asks)))

                        for level_idx in range(used_levels):
                            ask_price = asks[level_idx]
                            if max_price > 0 and ask_price > max_price:
                                log.warning(
                                    f"[Engine] 매수 가드 발동: {symbol} 매도{level_idx+1}호가 {ask_price:.4f} > max {max_price:.4f} "
                                    f"(허용 +{limit_buy_max_premium_pct:.2f}%) → 매수 스킵"
                                )
                                return False, "guard_triggered"

                            # 가격이 바뀌면 매수가능수량도 바뀔 수 있어 재조회
                            ps2 = kis_order.get_buyable_amount(exchange=exchange, symbol=symbol, price=ask_price, mode=mode)
                            max_ps_qty2 = None
                            try:
                                if ps2 and ps2.get("max_ord_psbl_qty"):
                                    max_ps_qty2 = int(float(ps2["max_ord_psbl_qty"]))
                                elif ps2 and ps2.get("ord_psbl_qty"):
                                    max_ps_qty2 = int(float(ps2["ord_psbl_qty"]))
                            except Exception:
                                max_ps_qty2 = None
                            if max_ps_qty2 is not None:
                                remaining = min(remaining, max_ps_qty2)

                            if remaining <= 0:
                                log.info(f"[Engine] 매수가능수량 부족으로 매수 불가: {symbol} (매수가능={max_ps_qty2})")
                                return False

                            log.info(f"[Engine] 지정가 매수 시도: {symbol}({exchange}) {remaining}주 @매도{level_idx+1}호가({ask_price})")
                            out = kis_order.order(symbol, remaining, ask_price, 'buy', exchange=exchange, order_type='00', mode=mode)
                            odno = (out or {}).get("ODNO") or (out or {}).get("odno")
                            if not odno:
                                log.warning(f"[Engine] {symbol} 매수 주문 실패(주문번호 없음)")
                                # 안전상 추가 주문을 진행하지 않는다(중복/과매수 방지).
                                return False, "order_no_missing"

                            # 짧게 대기 후 미체결 잔량 확인
                            time_module.sleep(max(0.2, limit_buy_step_wait_sec))
                            unfilled_qty = _find_unfilled_qty_by_odno(odno)
                            if unfilled_qty <= 0:
                                log.info(f"[Engine] {symbol} 매수 체결(또는 미체결 목록에서 제거됨): odno={odno}")
                                return True, "filled_or_removed"

                            # 잔량 취소 후 다음 호가로 재시도(중복 미체결 방지)
                            log.info(f"[Engine] {symbol} 미체결 잔량 {unfilled_qty}주 → 취소 후 다음 호가로 재시도 (odno={odno})")
                            cncl = kis_order.revise_cancel_order(
                                exchange=exchange,
                                symbol=symbol,
                                origin_order_no=str(odno),
                                qty=int(unfilled_qty),
                                price=0,
                                action="cancel",
                                mode=mode,
                            )
                            if not cncl:
                                log.warning(f"[Engine] {symbol} 잔량 취소 실패 → 중복 주문 방지 위해 재시도 중단 (odno={odno})")
                                # 취소 실패 시 중복 주문 위험이 크므로 폴백 포함 추가 주문 금지
                                return False, "cancel_failed"

                            remaining = int(unfilled_qty)
                            # 다음 단계로 넘어가기 전 과도한 호출 방지
                            time_module.sleep(0.4)

                        # 여기까지 왔으면 최대 레벨까지 시도했으나 잔량이 남은 케이스
                        log.warning(f"[Engine] {symbol} 지정가 호가 상향 시도 후에도 미체결 잔량이 남아 매수 완료 실패(remaining={remaining})")
                        # 부분체결 가능성이 있으므로 폴백 포함 추가 주문 금지
                        return False, "unfilled_remaining"

                    if qty > 0:
                        if mode == "real" and buy_order_method == "limit_ask_ladder":
                            ok, reason = _buy_with_ask_ladder()
                            if (not ok) and (reason in ("ask_api_failed", "asks_empty")):
                                # 실전에서 "호가 조회 자체"가 불가한 환경이면 ladder를 시작할 수 없다.
                                # 이 경우에만(=ladder 주문을 넣기 전) 기존 방식으로 1회 폴백한다.
                                buy_price = current_price * (1.0 + (slippage_pct / 100.0))
                                log.info(f"[Engine] ladder 불가({reason}) → 슬리피지 지정가 1회 폴백: {symbol} {qty}주 (@{buy_price})")
                                kis_order.order(symbol, qty, buy_price, 'buy', exchange=exchange, order_type='00', mode=mode)
                            elif not ok:
                                # 가드 발동/부분체결/취소 실패 등 "중복/과매수 위험" 케이스에서는 추가 주문을 금지한다.
                                log.warning(f"[Engine] ladder 실패({reason}) → 안전상 추가 폴백 주문을 생략합니다.")
                        else:
                            buy_price = current_price * (1.0 + (slippage_pct / 100.0))
                            log.info(f"[Engine] 매수 주문 실행: {symbol}({exchange}) {qty}주 (@{buy_price})")
                            kis_order.order(symbol, qty, buy_price, 'buy', exchange=exchange, order_type='00', mode=mode)
                    else:
                        log.info(f"[Engine] 매수가능수량 부족으로 매수 불가: {symbol} (예산수량={qty_by_budget}, 매수가능={max_ps_qty})")

            log.info("=== 자동매매 엔진 실행 완료 ===")

            # (스케줄 실행 기록은 위에서 선 마킹 처리)

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

    def run_once(self, mode: str | None = None):
        """
        수동 1회 실행(스케줄/auto_trading_enabled 무시):
        - myKiwoom-main의 manual_execution과 동일한 의도
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        return self._run_core(mode=mode, analysis_data=None, ignore_auto_enabled=True)

    def run_once_with_analysis(self, analysis_data: dict, mode: str | None = None):
        """
        즉시 실행(미리보기 후 실행) 전용:
        - 분석 결과(analysis_data)를 그대로 사용
        - auto_trading_enabled가 OFF여도 1회 실행은 허용
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        return self._run_core(mode=mode, analysis_data=analysis_data, ignore_auto_enabled=True)

    def get_next_scheduled_run_at(self, mode: str | None = None):
        """
        UI용: 다음 '실제 스케줄 실행 시각' 계산 (APScheduler의 interval next_run_time과 별개)
        - auto_trading_enabled가 OFF면 None
        - 오늘 이미 실행했으면 내일 schedule_time
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        auto_enabled = bool(config_manager.get(f"{mode}.auto_trading_enabled", False))
        if not auto_enabled:
            return None

        schedule_time = config_manager.get(f"{mode}.schedule_time", "00:00") or "00:00"
        try:
            hh, mm = str(schedule_time).split(":")
            hh = int(hh); mm = int(mm)
        except Exception:
            hh, mm = 0, 0

        now = datetime.now()
        today = now.strftime("%Y%m%d")
        last_day = self._get_last_scheduled_run_day(mode)

        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if last_day == today:
            target = target + timedelta(days=1)
        else:
            if now >= target:
                # 오늘 시간이 지났는데 아직 실행 안 했으면 '다음날'로 표시
                target = target + timedelta(days=1)
        return target

    def stop_loss_watch(self):
        """
        장중 손절 감시 (1분 주기)
        - myKiwoom-main의 장중 감시 UX를 단순화하여 1분마다 수행
        - 자동매매와 별개로 intraday_stop_loss.enabled가 ON일 때만 동작
        """
        mode = config_manager.get('common.mode', 'mock')
        log = get_mode_logger(mode)
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

            # 여기부터는 "실제 감시 로직 수행" (상태 갱신)
            self.last_stop_watch_at = datetime.now()
            self.last_stop_watch_error = None

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
