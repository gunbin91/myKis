import requests
import time as time_module
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from src.config.config_manager import config_manager
from src.api.order import kis_order
from src.api.quote import kis_quote
from src.api.auth import kis_auth
from src.api.exchange import normalize_analysis_exchange
from src.utils.logger import logger, get_mode_logger, set_engine_api_logging
from src.utils.fx_rate import get_usd_krw_rate
from src.engine.position_store import PositionStore
from src.engine.run_state_store import RunStateStore
from src.engine.execution_history_store import ExecutionHistoryStore
from uuid import uuid4

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

    def _wait_for_token(self, mode: str, timeout_sec: int = 70, poll_sec: int = 10) -> bool:
        """
        (B) 실행 시작 시 토큰을 확보할 때까지 제한시간 내 재시도.
        - kis_auth.get_token()은 EGW00133 대응(프로세스 간 쿨다운/락) 때문에 None을 반환할 수 있어,
          엔진 실행은 여기서 기다린 후 진행한다.
        """
        log = get_mode_logger(mode, "ENGINE")
        try:
            timeout_sec = int(timeout_sec or 0)
        except Exception:
            timeout_sec = 0
        if timeout_sec <= 0:
            timeout_sec = 70
        try:
            poll_sec = int(poll_sec or 0)
        except Exception:
            poll_sec = 0
        if poll_sec <= 0:
            poll_sec = 10

        deadline = datetime.now() + timedelta(seconds=timeout_sec)
        first = True
        while datetime.now() <= deadline:
            try:
                token = kis_auth.get_token(mode)
                if token:
                    return True
            except Exception:
                pass
            if first:
                log.warning(f"[Engine] 토큰 확보 대기 중... (최대 {timeout_sec}s)")
                first = False
            time_module.sleep(poll_sec)
        log.error(f"[Engine] 토큰 확보 실패(시간 초과): timeout={timeout_sec}s")
        return False

    def _wait_for_fx_rate(self, *, mode: str, timeout_sec: int = 60, poll_sec: int = 5):
        """
        환율(USD/KRW)을 확보할 때까지 제한시간 내 재시도.
        - 웹(/api/status)은 v1_008 present를 먼저 조회한 뒤 fx에 주입하므로 안정적인 편.
        - 자동매매 프로세스는 순간적인 토큰/네트워크 이슈로 v1_008이 실패할 수 있어, 여기서 기다린 후 진행한다.
        - 실패 시 FxRateResult(rate=None, ...) 형태를 반환할 수 있다.
        """
        log = get_mode_logger(mode, "ENGINE")
        try:
            timeout_sec = int(timeout_sec or 0)
        except Exception:
            timeout_sec = 0
        if timeout_sec <= 0:
            timeout_sec = 60
        try:
            poll_sec = int(poll_sec or 0)
        except Exception:
            poll_sec = 0
        if poll_sec <= 0:
            poll_sec = 5

        deadline = datetime.now() + timedelta(seconds=timeout_sec)
        first = True
        last_fx = None
        while datetime.now() <= deadline:
            try:
                present = kis_order.get_present_balance(
                    natn_cd="000",
                    tr_mket_cd="00",
                    inqr_dvsn_cd="00",
                    wcrc_frcr_dvsn_cd="02",
                    caller="ENGINE",
                    mode=mode,
                )
                fx = get_usd_krw_rate(mode=mode, kis_present=(present or {}))
                last_fx = fx
                if fx and fx.rate and fx.rate > 0:
                    return fx
            except Exception:
                pass
            if first:
                log.warning(f"[Engine] USD/KRW 환율 확보 대기 중... (최대 {timeout_sec}s)")
                first = False
            time_module.sleep(poll_sec)
        if last_fx is not None:
            return last_fx
        # 최후 폴백: 기존 동작 유지(내부에서 KIS→FDR 1회)
        return get_usd_krw_rate(mode=mode)

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

    def get_analysis_data(self, trace_cb=None):
        """
        분석 서버에서 매수/매도 리스트 가져오기
        - 요구사항: 분석서버의 '실시간 분석 실행'을 호출한 뒤 결과를 받는다.
          (분석서버 구현체에 따라 /api/start_analysis 또는 /v1/analysis/run)
        - 결과 폴링: /v1/analysis 또는 /v1/analysis/result (서버별 상이) 지원
        """
        def _trace(step: str, **meta):
            try:
                if callable(trace_cb):
                    trace_cb(step=step, **(meta or {}))
            except Exception:
                pass

        log = get_mode_logger(config_manager.get("common.mode", "mock"), "ENGINE")

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
            _trace("analysis.mock_enabled")
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
                    exchange_raw = (
                        r.get("exchange")
                        or r.get("시장구분")
                        or r.get("market")
                        or r.get("excd")
                    )
                    exchange = normalize_analysis_exchange(exchange_raw)
                    if not exchange:
                        log.warning(f"[Engine] 분석 exchange 파싱 실패 → 기본 NAS 사용: raw={exchange_raw}")
                        exchange = "NAS"
                    buy.append({
                        "code": code,
                        "exchange": exchange,
                        # UI/미리보기용 메타 (엔진 로직은 code/exchange만 사용)
                        "name": r.get("종목명") or r.get("name") or r.get("stock_name"),
                        "price": r.get("현재가") or r.get("price") or r.get("current_price"),
                        "score": r.get("최종점수") or r.get("score") or r.get("final_score"),
                        "prob": r.get("상승확률") or r.get("prob") or r.get("up_prob") or r.get("prob_up"),
                        "market_cap": r.get("시가총액") or r.get("market_cap") or r.get("marketCap") or r.get("mktcap"),
                    })

                # UI/미리보기에서 분석 정보를 표시할 수 있도록 meta를 함께 반환
                return {
                    "buy": buy,
                    "sell": [],
                    "meta": {
                        "analysis_date": data.get("analysis_date"),
                        "total_stocks": data.get("total_stocks"),
                        "top_stocks": rows[:20] if isinstance(rows, list) else rows,
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
            _trace("analysis.start", base_url=base_url)
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
                                log.warning(f"[Engine] 분석 start 400: {su} -> {msg}")
                        except Exception:
                            log.warning(f"[Engine] 분석 start 400: {su}")
                    else:
                        if sr.status_code == 404:
                            start_not_found = True
                        log.warning(f"[Engine] 분석 start 응답 오류: {su} -> {sr.status_code}")
                except Exception as e:
                    log.warning(f"[Engine] 분석 start 호출 실패(무시하고 폴링): {su} -> {e}")

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
                _trace("analysis.no_result_url")
                return {"buy": [], "sell": []}

            seen_running = False
            stable_success_cnt = 0
            _trace("analysis.poll.start", url=chosen_result_url)
            poll_started = datetime.now()
            last_trace_sec = -999999.0
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
                        # 너무 잦은 trace 방지(30초 단위)
                        try:
                            elapsed = (datetime.now() - poll_started).total_seconds()
                            if (elapsed - last_trace_sec) >= 30:
                                last_trace_sec = elapsed
                                _trace("analysis.poll.waiting", elapsed_sec=int(elapsed))
                        except Exception:
                            pass
                        time_module.sleep(2)
                        continue

                    out = _normalize_payload_to_buy_sell(payload)
                    if out is None:
                        # 포맷이 예상과 다르면 잠시 대기 후 재시도
                        try:
                            elapsed = (datetime.now() - poll_started).total_seconds()
                            if (elapsed - last_trace_sec) >= 30:
                                last_trace_sec = elapsed
                                _trace("analysis.poll.unexpected_format", elapsed_sec=int(elapsed))
                        except Exception:
                            pass
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
                        try:
                            elapsed = (datetime.now() - poll_started).total_seconds()
                        except Exception:
                            elapsed = None
                        _trace("analysis.poll.done", elapsed_sec=int(elapsed) if elapsed is not None else None, seen_running=seen_running, date_changed=date_changed)
                        return out

                    stable_success_cnt += 1
                    if start_ok and stable_success_cnt >= 2:
                        try:
                            elapsed = (datetime.now() - poll_started).total_seconds()
                        except Exception:
                            elapsed = None
                        _trace("analysis.poll.done", elapsed_sec=int(elapsed) if elapsed is not None else None, stable_success_cnt=stable_success_cnt)
                        return out

                except Exception:
                    pass

                time_module.sleep(2)

            _trace("analysis.poll.timeout")
            return {"buy": [], "sell": []}
        except Exception as e:
            log.warning(f"[Engine] 분석 서버 연결 실패: {e}")
            _trace("analysis.exception", error=str(e))
            return {"buy": [], "sell": []}

    def _run_core(self, mode: str, analysis_data: dict | None, ignore_auto_enabled: bool):
        """주기적으로 실행될 메인 로직"""
        log = get_mode_logger(mode, "ENGINE")
        if self.is_running:
            log.warning("[Engine] 이전 작업이 아직 진행 중입니다.")
            return

        # 실행 이력(상세) 수집: 실패/예외 포함해서 1회 실행 단위로 저장한다.
        run_id = str(uuid4())
        started_at = datetime.now().isoformat(timespec="seconds")
        history: dict = {
            "run_id": run_id,
            "mode": mode,
            "run_type": "manual" if ignore_auto_enabled else "scheduled",
            "started_at": started_at,
            "finished_at": None,
            "status": "unknown",  # success|partial|no_trade|error
            "message": None,
            "analysis": None,  # 원본(요약 포함)
            "analysis_buy": [],
            "analysis_sell": [],
            "sell_attempts": [],
            "buy_attempts": [],
            "skips": [],  # {"side":"buy/sell","symbol":..,"reason":..}
            "errors": [],
            # 사용자 친화적 표시(해외주식 운영): 실행 당시 스냅샷/단계 trace/제외사유
            "snapshot": {},
            "trace": [],
            "excluded": {"buy": [], "sell": []},
        }

        def _trace(step: str, **meta):
            try:
                history["trace"].append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "step": step,
                    **(meta or {}),
                })
            except Exception:
                pass
        strategy = config_manager.get(f'{mode}.strategy', {})
        last_step = "init"
        last_context: dict = {}

        def _set_step(step: str, **ctx):
            nonlocal last_step, last_context
            last_step = step
            last_context = ctx or {}

        def _log_issue(kind: str, message: str, **meta):
            payload = {
                "run_id": run_id,
                "mode": mode,
                "step": last_step,
                **(last_context or {}),
                **(meta or {}),
            }
            log.error(f"[Engine] {message} | {payload}")
            try:
                history["errors"].append({"kind": kind, **payload})
            except Exception:
                pass
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
        set_engine_api_logging(mode, True)
        try:
            # (B) 자동매매는 토큰 확보 후 진행 (토큰 발급 제한(EGW00133) 대응)
            if not self._wait_for_token(mode=mode, timeout_sec=70, poll_sec=10):
                self.last_error = "token_issue_timeout"
                history["status"] = "error"
                history["message"] = "토큰 확보 실패(시간 초과)"
                _trace("token.wait.timeout")
                return
            _trace("token.ready")
            
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
            # USD/KRW 환율(원/달러): 토큰처럼 "확보 후 진행"이 더 안전하다.
            # - reserve_cash_krw를 사용(>0)하는 경우에만 환율 확보를 기다린다.
            if reserve_cash_krw > 0:
                fx = self._wait_for_fx_rate(mode=mode, timeout_sec=60, poll_sec=5)
            else:
                fx = get_usd_krw_rate(mode=mode)
            usd_krw_rate = fx.rate or 0.0
            usd_krw_rate_source = fx.source
            usd_krw_rate_error = getattr(fx, "error", None)

            # 환율 자동조회가 실패하면 "매수는 취소, 매도만 실행" 정책 적용
            # (주의) 환율 실패(0/None) 상태에서 reserve_cash_krw/usd_krw_rate를 먼저 계산하면 0나누기 예외로 프로세스가 죽는다.
            allow_buy = True
            if usd_krw_rate <= 0:
                allow_buy = False
                history["errors"].append(f"fx_rate_unavailable: source={usd_krw_rate_source}, error={usd_krw_rate_error}")
                _trace("fx.unavailable", source=usd_krw_rate_source, error=usd_krw_rate_error)
            else:
                _trace("fx.ready", usd_krw_rate=usd_krw_rate, source=usd_krw_rate_source)

            # reserve_cash 계산(USD): 환율이 유효할 때만 KRW→USD 변환
            if reserve_cash_krw > 0:
                if usd_krw_rate > 0:
                    reserve_cash = (reserve_cash_krw / usd_krw_rate)
                else:
                    # 환율이 없으면 매수는 스킵되므로 reserve_cash는 0(또는 legacy)로 안전하게 둔다.
                    reserve_cash = reserve_cash_usd_legacy if reserve_cash_usd_legacy > 0 else 0.0
            else:
                reserve_cash = reserve_cash_usd_legacy
            max_hold_days = int(strategy.get("max_hold_days", 0) or 0)
            # 시장가에 가깝게 체결시키기 위한 슬리피지(%) - 지정가만 사용하는 구조에서 체결률을 높이기 위함
            slippage_pct = float(strategy.get("slippage_pct", 0.5) or 0.5)
            
            log.info(f"=== 자동매매 엔진 실행 시작 ({mode} 모드) ===")
            log.info(f"전략: top_n={top_n}, reserve_cash_usd≈${reserve_cash:.2f} (reserve_cash_krw={reserve_cash_krw:.0f}, usd_krw_rate={usd_krw_rate} [{usd_krw_rate_source}]), 익절 {take_profit_pct}%, 손절 {stop_loss_pct}%")
            if not allow_buy:
                log.warning(f"[Engine] USD/KRW 환율 자동조회 실패 → 매수는 취소(스킵)하고 매도 조건만 실행합니다. source={usd_krw_rate_source}, error={usd_krw_rate_error}")

            # 실행 스냅샷(해외주식 기준: USD 예산/환율/주문방식이 핵심)
            history["snapshot"] = {
                "mode": mode,
                "schedule_time": schedule_time,
                "strategy": {
                    "top_n": top_n,
                    "take_profit_pct": take_profit_pct,
                    "stop_loss_pct": stop_loss_pct,
                    "max_hold_days": max_hold_days,
                    "slippage_pct": slippage_pct,
                    "reserve_cash_krw": reserve_cash_krw,
                    "reserve_cash_usd": reserve_cash,
                    "buy_order_method": (strategy.get("buy_order_method") or ("limit_ask_ladder" if mode == "real" else "limit_slippage")).strip(),
                    "limit_buy_max_premium_pct": float(strategy.get("limit_buy_max_premium_pct", 1.0) or 1.0),
                    "limit_buy_max_levels": int(strategy.get("limit_buy_max_levels", 5) or 5),
                    "limit_buy_step_wait_sec": float(strategy.get("limit_buy_step_wait_sec", 1.0) or 1.0),
                },
                "fx": {
                    "usd_krw_rate": usd_krw_rate,
                    "source": usd_krw_rate_source,
                    "error": usd_krw_rate_error,
                    "allow_buy": allow_buy,
                },
            }

            # 1. 거래 가능 시간 체크
            if not self.is_market_open():
                log.info("[Engine] 현재 거래 가능 시간이 아닙니다. (22:00 ~ 07:00)")
                return

            # 2. 잔고 조회
            balance_info = kis_order.get_balance(mode=mode, caller="ENGINE")
            if not balance_info:
                log.error("[Engine] 잔고 조회 실패로 중단")
                self.is_running = False
                _trace("balance.failed")
                return
            _trace("balance.ok")

            output1 = balance_info.get('output1', [])
            # 보유 종목 정보 파싱
            my_stocks = {}
            store = PositionStore(mode)
            history_store = ExecutionHistoryStore(mode=mode)
            held_symbols = []
            for stock in output1:
                if not stock['ovrs_pdno']: continue
                
                symbol = stock['ovrs_pdno']
                try:
                    qty = int(float(stock.get('ovrs_cblc_qty') or 0))
                except Exception:
                    qty = 0
                try:
                    ord_psbl_qty = int(float(stock.get('ord_psbl_qty') or 0))
                except Exception:
                    ord_psbl_qty = 0
                profit_rate = float(stock['evlu_pfls_rt'])
                exch = stock.get("ovrs_excg_cd") or "NASD"
                
                if qty > 0:
                    held_symbols.append(symbol)
                    my_stocks[symbol] = {
                        'qty': qty,
                        'ord_psbl_qty': ord_psbl_qty,
                        'profit_rate': profit_rate,
                        'name': stock['ovrs_item_name'],
                        'exchange': exch,
                    }

                # 보유기간 추적(최초 감지일/추가매수 시점 기록)
                store.upsert(symbol=symbol, qty=qty, exchange=exch)

            # 잔고에 없는 종목은 store에서도 정리(일시 누락 유예)
            for sym in store.all_symbols():
                if sym not in my_stocks:
                    miss = store.mark_missing(sym)
                    if miss >= 2:
                        store.upsert(symbol=sym, qty=0)

            # 보유기간 보정: v1_007(주문체결내역) 동기화는 실전만 수행
            # - mock에서는 ExecutionHistoryStore로 보유기간을 계산하므로 불필요 호출을 생략한다.
            # - (과도한 호출 방지: 1일 1회, PositionStore meta에 기록)
            last_buy_date_map: dict[str, str] = {}
            def _pick_latest_date(*dates: str | None) -> str | None:
                candidates = []
                for d in dates:
                    if not d:
                        continue
                    s = str(d).strip()
                    if len(s) == 8 and s.isdigit():
                        candidates.append(s)
                return max(candidates) if candidates else None
            cache_path = Path(__file__).resolve().parents[2] / "data" / f"last_buy_cache_{mode}.json"
            def _read_last_buy_cache() -> dict[str, str]:
                try:
                    if not cache_path.exists():
                        return {}
                    import json
                    with open(cache_path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                    dates = data.get("dates") if isinstance(data, dict) else None
                    return dates if isinstance(dates, dict) else {}
                except Exception:
                    return {}
            def _write_last_buy_cache(dates: dict[str, str]) -> None:
                try:
                    import json
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    payload = {
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                        "dates": dates,
                    }
                    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=2)
                    tmp.replace(cache_path)
                except Exception:
                    pass
            cache_dates = _read_last_buy_cache()
            try:
                if mode != "mock":
                    today = datetime.now().strftime("%Y%m%d")
                    now_dt = datetime.now()
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

                    retry_at = store.get_api_retry_at()
                    retry_due = False
                    if retry_at:
                        try:
                            retry_due = now_dt >= datetime.fromisoformat(retry_at)
                        except Exception:
                            retry_due = True
                    if (not needs_sync) and held_symbols and retry_due:
                        needs_sync = True

                    if held_symbols and needs_sync:
                        hist = None
                        rows = []
                        for lookback_days in (60, 30, 14):
                            end = today
                            start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
                            hist = kis_order.get_order_history(
                                start_date=start,
                                end_date=end,
                                sll_buy_dvsn="02",
                                ccld_nccs_dvsn="01",
                                mode=mode,
                                caller="ENGINE",
                            )
                            if hist is not None:
                                rows = hist.get("output") or hist.get("output1") or []
                                rows = rows if isinstance(rows, list) else [rows]
                                break

                        def _as_yyyymmdd(v: str | None) -> str | None:
                            if not v:
                                return None
                            vv = str(v).strip().replace("-", "").replace(".", "")
                            return vv if len(vv) == 8 and vv.isdigit() else None

                        def _is_buy(row: dict) -> bool:
                            # 가이드: sll_buy_dvsn_cd = 02 (매수)
                            cd = str(
                                row.get("sll_buy_dvsn_cd")
                                or row.get("SLL_BUY_DVSN_CD")
                                or row.get("sll_buy_dvsn")
                                or row.get("SLL_BUY_DVSN")
                                or ""
                            ).strip()
                            if cd in ("02", "2"):
                                return True
                            v = str(
                                row.get("sll_buy_dvsn_name")
                                or row.get("sll_buy_dvsn_cd_name")
                                or row.get("sll_buy_dvsn_name")
                                or ""
                            ).strip().lower()
                            if ("buy" in v) or ("매수" in v):
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
                            # 가이드: ord_dt(주문일자)를 우선 사용
                            d = _as_yyyymmdd(
                                r.get("ccld_dt")
                                or r.get("CCLD_DT")
                                or r.get("ord_dt")
                                or r.get("ORD_DT")
                                or r.get("trad_day")
                                or r.get("TRAD_DAY")
                            )
                            if not d:
                                continue
                            cur = last_buy_date.get(sym)
                            if (cur is None) or (d > cur):
                                last_buy_date[sym] = d

                        if hist is not None:
                            updated_any = False
                            for sym, d in last_buy_date.items():
                                store.set_open_date(symbol=sym, open_date=d, source="api")
                                updated_any = True
                            last_buy_date_map = last_buy_date
                            if last_buy_date:
                                cache_dates.update(last_buy_date)
                                _write_last_buy_cache(cache_dates)
                            # 동기화가 실제로 성공(업데이트 발생)했을 때만 api_sync_day 갱신
                            if updated_any:
                                store.set_api_sync_day(today)
                            store.clear_api_retry()
                            # 일부 종목 누락 시 개별 조회로 보강 (페이지 제한/정렬 문제 대응)
                            missing = set(held_symbols) - set(last_buy_date.keys())
                            if missing:
                                for sym in sorted(missing):
                                    fetched = None
                                    for lookback_days in (60, 30, 14):
                                        end = today
                                        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
                                        h2 = kis_order.get_order_history(
                                            start_date=start,
                                            end_date=end,
                                            pdno=sym,
                                            sll_buy_dvsn="02",
                                            ccld_nccs_dvsn="01",
                                            mode=mode,
                                            caller="ENGINE",
                                        )
                                        if h2 is None:
                                            continue
                                        r2 = h2.get("output") or h2.get("output1") or []
                                        r2 = r2 if isinstance(r2, list) else [r2]
                                        for rr in r2:
                                            if not isinstance(rr, dict):
                                                continue
                                            d2 = _as_yyyymmdd(
                                                rr.get("ccld_dt")
                                                or rr.get("CCLD_DT")
                                                or rr.get("ord_dt")
                                                or rr.get("ORD_DT")
                                                or rr.get("trad_day")
                                                or rr.get("TRAD_DAY")
                                            )
                                            if d2 and ((fetched is None) or (d2 > fetched)):
                                                fetched = d2
                                        if fetched:
                                            break
                                    if fetched:
                                        store.set_open_date(symbol=sym, open_date=fetched, source="api")
                                        last_buy_date_map[sym] = fetched
                                        cache_dates[sym] = fetched
                                if missing:
                                    _write_last_buy_cache(cache_dates)
                        else:
                            # 실패: 다음 재시도 스케줄
                            store.set_api_last_error("v1_007_failed")
                            store.set_api_retry_at((now_dt + timedelta(minutes=20)).isoformat(timespec="seconds"))
            except Exception:
                pass
            if (mode != "mock") and (not last_buy_date_map) and cache_dates:
                last_buy_date_map = cache_dates

            # 3. 분석 데이터 수신 (즉시실행/미리보기에서 전달되면 그것을 사용)
            if analysis_data is None:
                _trace("analysis.fetch.start")
                analysis_data = self.get_analysis_data(trace_cb=_trace)
            history["analysis"] = analysis_data
            buy_list = analysis_data.get('buy', []) if isinstance(analysis_data, dict) else []
            # 정책(B안): 분석서버의 sell 리스트 기반 "자동 매도" 기능은 사용하지 않음(혼동/오주문 리스크).
            # - 분석은 매수 후보 생성에만 사용한다.
            # - sell이 내려오더라도 무시하며, UI/이력에서도 sell 리스트를 비워서 혼동을 막는다.
            sell_list = []
            history["analysis_buy"] = buy_list if isinstance(buy_list, list) else [buy_list]
            history["analysis_sell"] = []
            
            log.info(f"[Engine] 분석 데이터 - Buy: {len(buy_list)}, Sell: 0 (sell list ignored)")
            _trace("analysis.fetch.done", buy=len(buy_list), sell=0, sell_ignored=True)

            # (C) '오늘 실행 마킹'은 핵심 사전조건(시장 오픈 + 잔고 조회 + 분석 수신) 이후에 수행
            # - 토큰만 확보한 상태에서 실패하면 "오늘 실행됨"으로 기록되어 하루가 통째로 스킵될 수 있어 위험.
            if (not ignore_auto_enabled):
                self._mark_scheduled_run_day(mode, datetime.now().strftime("%Y%m%d"))
                _trace("run_day.marked")

            # run 내부 중복 방지용 상태
            sold_symbols: set[str] = set()
            # 실전에서만 사용: 미체결 선취소를 위한 exchange별 캐시(과도한 v1_005 호출 방지)
            _unfilled_cache: dict[str, list[dict]] = {}

            def _get_unfilled_cached(ex: str) -> list[dict]:
                ex2 = (ex or "NASD").strip().upper()
                if mode != "real":
                    return []
                if ex2 in _unfilled_cache:
                    return _unfilled_cache[ex2]
                rows = kis_order.get_unfilled_orders(exchange=ex2, mode=mode, caller="ENGINE") or []
                rows = rows if isinstance(rows, list) else [rows]
                _unfilled_cache[ex2] = rows
                return rows

            def _cancel_unfilled_for_symbol(ex: str, sym: str, *, side_filter: str | None = None) -> int:
                """
                실전: 특정 종목의 미체결 주문을 선취소하여 다음 주문(특히 매수)을 확보한다.
                - side_filter: "buy"|"sell"|None
                - 주의: 자동매매가 사용자 수동주문을 취소할 수 있으므로, 티커 단위로만 제한한다.
                """
                if mode != "real":
                    return 0
                ex2 = (ex or "NASD").strip().upper()
                sym2 = (sym or "").strip().upper()
                if not sym2:
                    return 0
                try:
                    rows = _get_unfilled_cached(ex2)
                except Exception:
                    return 0
                cancelled = 0
                for r in (rows or []):
                    if not isinstance(r, dict):
                        continue
                    rsym = (r.get("pdno") or r.get("PDNO") or r.get("ovrs_pdno") or "").strip().upper()
                    if rsym != sym2:
                        continue
                    side_cd = str(r.get("sll_buy_dvsn_cd") or r.get("SLL_BUY_DVSN_CD") or r.get("sll_buy_dvsn") or "").strip().lower()
                    if side_filter == "buy":
                        if side_cd and side_cd not in ("02", "buy"):
                            continue
                    if side_filter == "sell":
                        if side_cd and side_cd not in ("01", "sell"):
                            continue
                    odno = str(r.get("odno") or r.get("ODNO") or "").strip()
                    if not odno:
                        continue
                    try:
                        nccs = int(float(str(r.get("nccs_qty") or r.get("NCCS_QTY") or 0).replace(",", "")))
                    except Exception:
                        nccs = 0
                    if nccs <= 0:
                        continue

                    _trace("unfilled.cancel.try", exchange=ex2, symbol=sym2, order_no=odno, qty=nccs, side=side_cd)
                    cncl = kis_order.revise_cancel_order(
                        exchange=ex2,
                        symbol=sym2,
                        origin_order_no=odno,
                        qty=int(nccs),
                        price=0,
                        action="cancel",
                        mode=mode,
                        caller="ENGINE",
                    )
                    _trace("unfilled.cancel.done", exchange=ex2, symbol=sym2, order_no=odno, ok=bool(cncl))
                    if cncl:
                        cancelled += 1
                return cancelled

            def _wait_for_sell_execution(sell_orders: list[dict], max_wait_time: int = 30) -> bool:
                """매도 주문 체결 대기 및 확인 (키움 패턴: 상한선 후 종료, 미체결 취소 없음)"""
                if not sell_orders:
                    return True
                try:
                    max_wait_time = int(max_wait_time or 0)
                except Exception:
                    max_wait_time = 30
                if max_wait_time <= 0:
                    max_wait_time = 30

                log.info(f"[Engine] 매도 체결 확인 대기 시작: {len(sell_orders)}건 (max={max_wait_time}s)")
                start_time = datetime.now()
                deadline = start_time + timedelta(seconds=max_wait_time)

                while datetime.now() <= deadline:
                    try:
                        today = datetime.now().strftime("%Y%m%d")
                        hist = kis_order.get_order_history(
                            start_date=today,
                            end_date=today,
                            sll_buy_dvsn="01",
                            ccld_nccs_dvsn="00",
                            mode=mode,
                            caller="ENGINE",
                        ) or {}
                        rows = hist.get("output") or []
                        rows = rows if isinstance(rows, list) else [rows]

                        executed_count = 0
                        for so in sell_orders:
                            sym = (so.get("symbol") or "").strip().upper()
                            qty = int(so.get("qty") or 0)
                            if not sym or qty <= 0:
                                continue
                            for r in rows:
                                if not isinstance(r, dict):
                                    continue
                                rsym = (r.get("pdno") or "").strip().upper()
                                if rsym != sym:
                                    continue
                                try:
                                    ccld_qty = int(float(str(r.get("ft_ccld_qty") or 0).replace(",", "")))
                                except Exception:
                                    ccld_qty = 0
                                if ccld_qty >= qty:
                                    executed_count += 1
                                    break

                        if executed_count >= len(sell_orders):
                            log.info(f"[Engine] 매도 체결 확인 완료: {executed_count}/{len(sell_orders)}건")
                            return True
                        log.info(f"[Engine] 매도 체결 대기 중: {executed_count}/{len(sell_orders)}건")
                    except Exception as e:
                        log.warning(f"[Engine] 매도 체결 확인 중 오류: {e}")
                    time_module.sleep(3)

                log.warning(f"[Engine] 매도 체결 확인 시간 초과 ({max_wait_time}s), 계속 진행")
                return False

            def _submit_sell_market_first(symbol: str, qty: int, exchange: str, reason: str):
                """
                매도 주문: 시장가(가격=0) 우선 시도, 실패 시 지정가로 폴백.
                - 실전/모의 공통: price=0, order_type='00'
                """
                # 1) 시장가 시도(가이드: OVRS_ORD_UNPR=0)
                out = kis_order.order(symbol, qty, 0, 'sell', exchange=exchange, order_type='00', mode=mode, caller="ENGINE")
                if out:
                    return out, 0.0, "market_0"

                # 2) 폴백: 현재가 기반 지정가(체결 우선: -슬리피지)
                px = kis_quote.get_current_price(exchange, symbol, mode=mode, caller="ENGINE") or {}
                sell_price = float(px.get("last", 0) or 0)
                if sell_price <= 0:
                    return None, 0.0, "price_unavailable"
                sell_price = sell_price * (1.0 - (slippage_pct / 100.0))
                out = kis_order.order(symbol, qty, sell_price, 'sell', exchange=exchange, order_type='00', mode=mode, caller="ENGINE")
                return out, float(sell_price), "limit_fallback"

            # 4. 매도 실행 (전략 매도)
            # 4-1. 익절/손절 감시
            sell_orders_sent = 0
            sell_orders = []
            for symbol, info in my_stocks.items():
                profit_rate = info['profit_rate']
                qty = info.get('ord_psbl_qty', 0)
                exchange = info.get("exchange", "NASD")

                # 런 내부 중복 매도 방지 + 장중 손절 감시(StopWatch)와의 충돌 방지(공통 쿨다운)
                try:
                    symu = (symbol or "").strip().upper()
                    if symu in sold_symbols:
                        history["skips"].append({"side": "sell", "symbol": symu, "reason": "already_sold_in_run"})
                        continue
                    now_dt = datetime.now()
                    last_sw = self._stop_loss_cooldown.get(symu)
                    if last_sw and (now_dt - last_sw) < timedelta(minutes=5):
                        history["skips"].append({"side": "sell", "symbol": symu, "reason": "sell_cooldown_recent"})
                        continue
                except Exception:
                    pass

                # 주문가능수량이 0이면 매도 시도하지 않는다(모의/실전 공통 안전)
                if qty <= 0:
                    history["skips"].append({"side": "sell", "symbol": symu, "reason": "sell_qty_unavailable"})
                    continue
                
                # 익절 조건
                if profit_rate >= take_profit_pct:
                    log.info(f"[Engine] 익절 조건 만족: {symbol} ({profit_rate}% >= {take_profit_pct}%)")
                    try:
                        _cancel_unfilled_for_symbol(exchange, symbol)
                    except Exception:
                        pass
                    out, sell_price, method = _submit_sell_market_first(symbol, qty, exchange, "take_profit")
                    if not out and method == "price_unavailable":
                        log.warning(f"[Engine] {symbol} 매도가 산출 실패(현재가 0)로 익절 매도 스킵")
                        history["skips"].append({"side": "sell", "symbol": symbol, "reason": "take_profit_price_unavailable"})
                    else:
                        history["sell_attempts"].append({
                            "symbol": symbol,
                            "exchange": exchange,
                            "qty": qty,
                            "price": sell_price,
                            "reason": "take_profit",
                            "profit_rate": profit_rate,
                            "take_profit_pct": take_profit_pct,
                            "stop_loss_pct": stop_loss_pct,
                            "method": method,
                            "order_no": (out or {}).get("ODNO") or (out or {}).get("odno"),
                            "ok": bool(out),
                        })
                        if out:
                            sell_orders_sent += 1
                            sell_orders.append({"symbol": symbol, "qty": qty})
                            try:
                                sold_symbols.add((symbol or "").strip().upper())
                                self._stop_loss_cooldown[(symbol or "").strip().upper()] = datetime.now()
                            except Exception:
                                pass
                    # 매도했으므로 my_stocks에서 제거해야 중복 매도 방지되나, API 호출 텀이 있으므로 생략
                    continue
                    
                # 손절 조건: 입력값 그대로 비교 (stop_loss_pct는 보통 음수)
                if profit_rate <= stop_loss_pct:
                    log.info(f"[Engine] 손절 조건 만족: {symbol} ({profit_rate}% <= {stop_loss_pct}%)")
                    try:
                        _cancel_unfilled_for_symbol(exchange, symbol)
                    except Exception:
                        pass
                    out, sell_price, method = _submit_sell_market_first(symbol, qty, exchange, "stop_loss")
                    if not out and method == "price_unavailable":
                        log.warning(f"[Engine] {symbol} 매도가 산출 실패(현재가 0)로 손절 매도 스킵")
                        history["skips"].append({"side": "sell", "symbol": symbol, "reason": "stop_loss_price_unavailable"})
                    else:
                        history["sell_attempts"].append({
                            "symbol": symbol,
                            "exchange": exchange,
                            "qty": qty,
                            "price": sell_price,
                            "reason": "stop_loss",
                            "profit_rate": profit_rate,
                            "take_profit_pct": take_profit_pct,
                            "stop_loss_pct": stop_loss_pct,
                            "method": method,
                            "order_no": (out or {}).get("ODNO") or (out or {}).get("odno"),
                            "ok": bool(out),
                        })
                        if out:
                            sell_orders_sent += 1
                            sell_orders.append({"symbol": symbol, "qty": qty})
                            try:
                                sold_symbols.add((symbol or "").strip().upper())
                                self._stop_loss_cooldown[(symbol or "").strip().upper()] = datetime.now()
                            except Exception:
                                pass
                    continue

                # 보유기간 초과 강제매도
                if max_hold_days > 0:
                    open_date = None
                    if mode == "mock":
                        try:
                            open_date = ExecutionHistoryStore(mode=mode).get_last_buy_date(symbol)
                        except Exception:
                            open_date = None
                    if not open_date:
                        symu = (symbol or "").strip().upper()
                        if mode != "mock":
                            open_date = last_buy_date_map.get(symu)
                        else:
                            try:
                                open_date = history_store.get_last_buy_date(symu)
                            except Exception:
                                open_date = None
                    if (mode != "mock") and (not open_date):
                        # 실전: v1_007/캐시가 없으면 보유기간 매도 판단을 스킵
                        continue
                    if open_date and len(open_date) == 8:
                        try:
                            od = datetime.strptime(open_date, "%Y%m%d").date()
                            days_held = (datetime.now().date() - od).days
                            if days_held >= max_hold_days:
                                log.info(f"[Engine] 보유기간 초과 매도: {symbol} ({days_held}d >= {max_hold_days}d)")
                                try:
                                    _cancel_unfilled_for_symbol(exchange, symbol)
                                except Exception:
                                    pass
                                out, sell_price, method = _submit_sell_market_first(symbol, qty, exchange, "max_hold_days")
                                if not out and method == "price_unavailable":
                                    log.warning(f"[Engine] {symbol} 매도가 산출 실패(현재가 0)로 보유기간 매도 스킵")
                                    history["skips"].append({"side": "sell", "symbol": symbol, "reason": "max_hold_price_unavailable"})
                                else:
                                    history["sell_attempts"].append({
                                        "symbol": symbol,
                                        "exchange": exchange,
                                        "qty": qty,
                                        "price": sell_price,
                                        "reason": "max_hold_days",
                                        "holding_days": days_held,
                                        "max_hold_days": max_hold_days,
                                        "method": method,
                                        "order_no": (out or {}).get("ODNO") or (out or {}).get("odno"),
                                        "ok": bool(out),
                                    })
                                    if out:
                                        sell_orders_sent += 1
                                        sell_orders.append({"symbol": symbol, "qty": qty})
                                        try:
                                            sold_symbols.add((symbol or "").strip().upper())
                                            self._stop_loss_cooldown[(symbol or "").strip().upper()] = datetime.now()
                                        except Exception:
                                            pass
                        except Exception:
                            pass

            # (B안) 분석 리스트 기반 매도 기능 제거: sell_list는 무시한다.

            # 키움 패턴처럼: 매도가 있었다면 잠깐 대기 후(체결/예수금 반영) 매수 예산 산정 시 최신 잔고를 쓰도록 한다.
            present_after_sell = None
            if sell_orders_sent > 0:
                _wait_for_sell_execution(sell_orders, max_wait_time=30)
                time_module.sleep(2.0)
                try:
                    present_after_sell = kis_order.get_present_balance(
                        natn_cd="000",
                        tr_mket_cd="00",
                        inqr_dvsn_cd="00",
                        wcrc_frcr_dvsn_cd="02",
                        caller="ENGINE",
                        mode=mode
                    )
                except Exception:
                    present_after_sell = None

            # 5. 매수 실행
            if not buy_list:
                log.info("[Engine] 매수 대상 종목이 없습니다.")
            else:
                if not allow_buy:
                    log.info("[Engine] 매수 스킵: 환율 자동조회 실패로 매수 로직을 실행하지 않습니다.")
                    buy_list = []
                    # 매도는 이미 실행되었으므로 매수만 건너뛰고 종료
                    log.info("=== 자동매매 엔진 실행 완료 (sell-only: fx_rate_unavailable) ===")
                    history["status"] = "partial" if history["sell_attempts"] else "no_trade"
                    history["message"] = "buy_skipped_fx_rate_unavailable"
                    return

                # 분석 서버 포맷 지원:
                # 1) ["TSLA","AAPL"]
                # 2) [{"code":"TSLA","exchange":"NAS"}, ...]
                normalized_buy = []
                for item in buy_list:
                    if isinstance(item, dict):
                        code = (item.get('code') or '').strip().upper()
                        exchange_raw = (
                            item.get("exchange")
                            or item.get("시장구분")
                            or item.get("market")
                            or item.get("excd")
                        )
                        exchange = normalize_analysis_exchange(exchange_raw)
                        if not exchange:
                            log.warning(f"[Engine] 매수 exchange 파싱 실패 → 기본 NAS 사용: raw={exchange_raw} ({code})")
                            _trace("analysis.exchange.defaulted", symbol=code, raw=exchange_raw)
                            exchange = "NAS"
                    else:
                        code = (str(item) or '').strip().upper()
                        exchange = 'NAS'
                    if code:
                        normalized_buy.append({"code": code, "exchange": exchange})

                if not normalized_buy:
                    log.info("[Engine] 매수 대상 종목이 없습니다.")
                    history["status"] = "partial" if history["sell_attempts"] else "no_trade"
                    history["message"] = "no_buy_candidates"
                    return

                # 매수 대상 수를 top_n으로 제한
                # 중요: 이미 보유중인 종목이 섞여 있으면, 먼저 제외한 뒤 top_n을 다시 뽑아야
                # 실제 매수 종목 수가 top_n에 가깝게 나온다.
                # 키움과 동일하게: 이번 런에서 '매도 성공한 종목'은 재매수 허용
                candidates = [
                    x for x in normalized_buy
                    if x.get("code") and (x.get("code") not in my_stocks or x.get("code") in sold_symbols)
                ][:top_n]
                # 제외 사유 기록(사용자 친화 UI용)
                try:
                    for x in normalized_buy:
                        sym = x.get("code")
                        if not sym:
                            continue
                        if sym in my_stocks and sym not in sold_symbols:
                            history["excluded"]["buy"].append({"symbol": sym, "reason": "already_held"})
                    # top_n 밖으로 밀린 종목
                    kept = set([x.get("code") for x in candidates if x.get("code")])
                    for x in normalized_buy:
                        sym = x.get("code")
                        if not sym or (sym in my_stocks and sym not in sold_symbols):
                            continue
                        if sym not in kept:
                            history["excluded"]["buy"].append({"symbol": sym, "reason": "beyond_top_n"})
                except Exception:
                    pass

                # autokiwoomstock처럼 "1회 예산 입력"은 사용하지 않고
                # 계좌의 '총 주문가능금액(USD)' - reserve_cash(USD 환산) 를 이번 실행 예산으로 사용
                #
                # - 실전: 해외주식-035(해외증거금 통화별조회) USD의 itgr_ord_psbl_amt(통합주문가능금액) 우선
                # - 모의: 해외주식-035 미지원 -> v1_008 output3.frcr_use_psbl_amt(외화사용가능금액)로 대체
                orderable_cash = 0.0
                orderable_source = None
                try:
                    if mode == "real":
                        fm = kis_order.get_foreign_margin(mode=mode, caller="ENGINE") or {}
                        rows = fm.get("output") or []
                        rows = rows if isinstance(rows, list) else [rows]
                        usd = None
                        for r in rows:
                            if isinstance(r, dict) and (str(r.get("crcy_cd") or "").strip().upper() == "USD"):
                                usd = r
                                break
                        if usd and usd.get("itgr_ord_psbl_amt") is not None:
                            orderable_cash = float(str(usd.get("itgr_ord_psbl_amt") or 0).replace(",", ""))
                            if orderable_cash > 0:
                                orderable_source = "035_itgr"

                    if orderable_cash <= 0:
                        ps = (present_after_sell or kis_order.get_present_balance(
                            natn_cd="000",
                            tr_mket_cd="00",
                            inqr_dvsn_cd="00",
                            wcrc_frcr_dvsn_cd="02",
                            caller="ENGINE",
                            mode=mode
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
                            if orderable_cash > 0:
                                orderable_source = "008_out2_usd"
                        if orderable_cash <= 0:
                            out3 = ps.get("output3") or {}
                            orderable_cash = float(str(out3.get("frcr_use_psbl_amt") or 0).replace(",", ""))
                            if orderable_cash > 0:
                                orderable_source = "008_frcr_use"
                        # mock 마지막 fallback: 총자산(원화)/환율로 "통합증거금 느낌"의 총가용 USD 추정
                        if mode == "mock" and orderable_cash <= 0:
                            try:
                                # 이미 산출한 자동 환율(없으면 설정값 fallback)을 사용
                                usd_krw_rate = float(str(usd_krw_rate or 1350.0).replace(",", ""))
                                out3 = (ps.get("output3") or {}) if isinstance(ps, dict) else {}
                                tot_asst_krw = float(str(out3.get("tot_asst_amt") or 0).replace(",", ""))
                                evlu_krw = float(str(out3.get("evlu_amt_smtl") or out3.get("evlu_amt_smtl_amt") or 0).replace(",", ""))
                                cash_krw = max(0.0, tot_asst_krw - evlu_krw)
                                if usd_krw_rate > 0 and cash_krw > 0:
                                    orderable_cash = cash_krw / usd_krw_rate
                                    if orderable_cash > 0:
                                        orderable_source = "mock_est_cash_krw"
                                elif usd_krw_rate > 0 and tot_asst_krw > 0:
                                    # 최후 폴백(기존 동작): 현금성 추정이 0일 때만 총자산 기반
                                    orderable_cash = tot_asst_krw / usd_krw_rate
                                    if orderable_cash > 0:
                                        orderable_source = "mock_est_tot_asset"
                            except Exception:
                                pass
                except Exception:
                    orderable_cash = 0.0
                    orderable_source = None

                total_budget = max(0.0, orderable_cash - reserve_cash)
                per_stock_budget = total_budget / len(candidates) if candidates else 0.0
                try:
                    history["snapshot"].setdefault("budget", {})
                    history["snapshot"]["budget"] = {
                        "orderable_usd": float(orderable_cash),
                        "orderable_source": orderable_source,
                        "reserve_cash_usd": float(reserve_cash),
                        "total_budget_usd": float(total_budget),
                        "per_stock_budget_usd": float(per_stock_budget),
                        "candidate_count": int(len(candidates) if candidates else 0),
                    }
                except Exception:
                    pass
                _trace("budget.ready", orderable_usd=orderable_cash, source=orderable_source, reserve_cash_usd=reserve_cash, total_budget_usd=total_budget, per_stock_budget_usd=per_stock_budget)
                if not candidates:
                    log.info("[Engine] 매수 대상 종목이 없습니다. (보유종목 제외 후)")
                    history["status"] = "partial" if history["sell_attempts"] else "no_trade"
                    history["message"] = "no_buy_candidates_after_filter"
                    return

                if per_stock_budget <= 0:
                    log.warning(f"[Engine] 매수 예산 부족: orderable_cash={orderable_cash}, reserve_cash_usd≈{reserve_cash:.2f}")
                    history["status"] = "partial" if history["sell_attempts"] else "no_trade"
                    history["message"] = "buy_budget_insufficient"
                    return

                for item in candidates:
                    symbol = item["code"]
                    exchange = item["exchange"]

                    # 이미 보유중이면 패스(방어 로직).
                    # 단, 이번 런에서 매도 성공한 종목은 키움 패턴과 동일하게 재매수 허용.
                    symu = (symbol or "").strip().upper()
                    if (symu in my_stocks) and (symu not in sold_symbols):
                        log.info(f"[Engine] 이미 보유중인 종목입니다: {symbol}")
                        history["skips"].append({
                            "side": "buy",
                            "symbol": symbol,
                            "reason": "already_held_guard",
                            "detail": {"exchange": exchange}
                        })
                        continue

                    # KIS 레이트리밋 방지용 기본 스로틀(키움식 운영 가드)
                    time_module.sleep(0.25)
                        
                    # 현재가 조회 (거래소 정보 포함)
                    _set_step("buy.quote.current", symbol=symbol, exchange=exchange)
                    price_info = kis_quote.get_current_price(exchange, symbol, mode=mode, caller="ENGINE")
                    if not price_info:
                        log.warning(f"[Engine] {symbol} 시세 조회 실패")
                        history["skips"].append({"side": "buy", "symbol": symbol, "reason": "quote_failed", "detail": {"exchange": exchange}})
                        continue

                    last_raw = None
                    if isinstance(price_info, dict):
                        last_raw = price_info.get("last")
                    try:
                        current_price = float(str(last_raw).replace(",", "").strip())
                    except Exception:
                        _log_issue(
                            "quote_invalid_last",
                            f"[Engine] {symbol} 시세 last 파싱 실패",
                            response=price_info,
                            last_raw=last_raw,
                            exchange=exchange,
                        )
                        history["skips"].append({"side": "buy", "symbol": symbol, "reason": "quote_invalid_last", "detail": {"exchange": exchange}})
                        continue
                    if current_price <= 0:
                        log.warning(f"[Engine] {symbol} 현재가 0원")
                        history["skips"].append({"side": "buy", "symbol": symbol, "reason": "price_zero", "detail": {"exchange": exchange}})
                        continue

                    # 매수 방식(키움 참고):
                    # - mock: 호가 API 미지원이므로 기존처럼 (현재가 + 슬리피지) 지정가
                    # - real: 매도 1호가부터 단계적으로 지정가 매수(미체결이면 다음 호가), 가드(최대 허용 프리미엄%) 적용
                    buy_order_method = (strategy.get("buy_order_method") or ("limit_ask_ladder" if mode == "real" else "limit_slippage")).strip()
                    limit_buy_max_premium_pct = float(strategy.get("limit_buy_max_premium_pct", 1.0) or 1.0)
                    limit_buy_max_levels = int(strategy.get("limit_buy_max_levels", 5) or 5)
                    limit_buy_step_wait_sec = float(strategy.get("limit_buy_step_wait_sec", 1.0) or 1.0)

                    # 슬리피지 주문은 실제 주문 단가가 current_price보다 높아질 수 있어,
                    # v1_014(매수가능수량) 조회 및 예산 수량 산정도 "실제 주문가" 기준으로 보수화한다.
                    planned_buy_price = current_price
                    if not (mode == "real" and buy_order_method == "limit_ask_ladder"):
                        planned_buy_price = current_price * (1.0 + (slippage_pct / 100.0))

                    # 연속 API 호출 간 간격 확보(EGW00201 완화)
                    time_module.sleep(0.25)
                        
                    # 1) 종목당 예산 기준 수량
                    qty_by_budget = int(per_stock_budget // planned_buy_price) if planned_buy_price > 0 else 0
                    if qty_by_budget <= 0:
                        log.info(f"[Engine] 예산 부족으로 매수 불가: {symbol} (필요: {current_price}, 예산: {per_stock_budget})")
                        history["skips"].append({"side": "buy", "symbol": symbol, "reason": "budget_insufficient", "detail": {"exchange": exchange, "current_price": current_price, "per_stock_budget": per_stock_budget}})
                        continue

                    # 2) KIS 매수가능금액조회(v1_014) 기준 최대수량
                    # - 모의/실전 공통 적용: 모의에서도 주문가능수량을 먼저 반영해 과대주문 실패를 줄인다.
                    max_ps_qty = None
                    _set_step("buy.buyable", symbol=symbol, exchange=exchange, price=float(planned_buy_price))
                    ps = kis_order.get_buyable_amount(exchange=exchange, symbol=symbol, price=planned_buy_price, mode=mode, caller="ENGINE")
                    if ps is None:
                        # 정책: v1_014가 최종 실패하면 해당 종목 매수는 스킵 (부분체결/로그-잔고 불일치 방지)
                        log.warning(f"[Engine] 매수가능금액조회(v1_014) 최종 실패(EGW00201 등). {symbol} 매수 스킵")
                        history["skips"].append({"side": "buy", "symbol": symbol, "reason": "buyable_query_failed", "detail": {"exchange": exchange, "price": planned_buy_price}})
                        continue
                    try:
                        if ps and ps.get("ovrs_max_ord_psbl_qty"):
                            max_ps_qty = int(float(ps["ovrs_max_ord_psbl_qty"]))
                        elif ps and ps.get("max_ord_psbl_qty"):
                            max_ps_qty = int(float(ps["max_ord_psbl_qty"]))
                        elif ps and ps.get("ord_psbl_qty"):
                            max_ps_qty = int(float(ps["ord_psbl_qty"]))
                    except Exception:
                        max_ps_qty = None

                    qty = qty_by_budget
                    if max_ps_qty is not None:
                        qty = min(qty, max_ps_qty)

                    if qty <= 0:
                        log.info(f"[Engine] 매수가능수량 부족으로 매수 불가: {symbol} (예산수량={qty_by_budget}, 매수가능={max_ps_qty})")
                        history["skips"].append({"side": "buy", "symbol": symbol, "reason": "qty_insufficient", "detail": {"exchange": exchange, "qty_by_budget": qty_by_budget, "max_ps_qty": max_ps_qty}})
                        continue

                    def _find_unfilled_qty_by_odno(odno: str) -> int:
                        try:
                            if not odno:
                                return 0
                            rows = kis_order.get_unfilled_orders(exchange=exchange, mode=mode, caller="ENGINE") or []
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
                        ob = kis_quote.get_asking_price(exchange, symbol, mode=mode, caller="ENGINE")
                        if not ob:
                            log.warning(f"[Engine] {symbol} 호가 조회 실패 → 슬리피지 지정가로 폴백")
                            _trace("buy.ladder.hoga_failed", symbol=symbol, exchange=exchange)
                            return False, "ask_api_failed"
                        asks = _extract_asks(ob)
                        if not asks:
                            log.warning(f"[Engine] {symbol} 호가 데이터 없음 → 슬리피지 지정가로 폴백")
                            _trace("buy.ladder.hoga_empty", symbol=symbol, exchange=exchange)
                            return False, "asks_empty"

                        max_price = current_price * (1.0 + (limit_buy_max_premium_pct / 100.0)) if current_price > 0 else 0.0
                        remaining = int(qty)
                        used_levels = max(1, min(int(limit_buy_max_levels), len(asks)))
                        _trace(
                            "buy.ladder.start",
                            symbol=symbol,
                            exchange=exchange,
                            qty=int(qty),
                            current_price=float(current_price),
                            max_price=float(max_price),
                            max_premium_pct=float(limit_buy_max_premium_pct),
                            levels=int(used_levels),
                            step_wait_sec=float(limit_buy_step_wait_sec),
                        )

                        for level_idx in range(used_levels):
                            ask_price = asks[level_idx]
                            _trace(
                                "buy.ladder.level.begin",
                                symbol=symbol,
                                exchange=exchange,
                                level=int(level_idx + 1),
                                ask_price=float(ask_price),
                                remaining=int(remaining),
                                max_price=float(max_price),
                            )
                            if max_price > 0 and ask_price > max_price:
                                log.warning(
                                    f"[Engine] 매수 가드 발동: {symbol} 매도{level_idx+1}호가 {ask_price:.4f} > max {max_price:.4f} "
                                    f"(허용 +{limit_buy_max_premium_pct:.2f}%) → 매수 스킵"
                                )
                                _trace(
                                    "buy.ladder.guard_triggered",
                                    symbol=symbol,
                                    exchange=exchange,
                                    level=int(level_idx + 1),
                                    ask_price=float(ask_price),
                                    max_price=float(max_price),
                                )
                                return False, "guard_triggered"

                            # 가격이 바뀌면 매수가능수량도 바뀔 수 있어 재조회
                            _set_step("buy.buyable_ladder", symbol=symbol, exchange=exchange, price=float(ask_price))
                            ps2 = kis_order.get_buyable_amount(exchange=exchange, symbol=symbol, price=ask_price, mode=mode, caller="ENGINE")
                            max_ps_qty2 = None
                            try:
                                if ps2 and ps2.get("ovrs_max_ord_psbl_qty"):
                                    max_ps_qty2 = int(float(ps2["ovrs_max_ord_psbl_qty"]))
                                elif ps2 and ps2.get("max_ord_psbl_qty"):
                                    max_ps_qty2 = int(float(ps2["max_ord_psbl_qty"]))
                                elif ps2 and ps2.get("ord_psbl_qty"):
                                    max_ps_qty2 = int(float(ps2["ord_psbl_qty"]))
                            except Exception:
                                max_ps_qty2 = None
                            if max_ps_qty2 is not None:
                                remaining = min(remaining, max_ps_qty2)

                            if remaining <= 0:
                                log.info(f"[Engine] 매수가능수량 부족으로 매수 불가: {symbol} (매수가능={max_ps_qty2})")
                                _trace(
                                    "buy.ladder.qty_insufficient",
                                    symbol=symbol,
                                    exchange=exchange,
                                    level=int(level_idx + 1),
                                    ask_price=float(ask_price),
                                    max_ps_qty=int(max_ps_qty2) if max_ps_qty2 is not None else None,
                                )
                                return False, "qty_insufficient"

                            log.info(f"[Engine] 지정가 매수 시도: {symbol}({exchange}) {remaining}주 @매도{level_idx+1}호가({ask_price})")
                            _trace(
                                "buy.ladder.order.submit",
                                symbol=symbol,
                                exchange=exchange,
                                level=int(level_idx + 1),
                                qty=int(remaining),
                                price=float(ask_price),
                            )
                            out = kis_order.order(symbol, remaining, ask_price, 'buy', exchange=exchange, order_type='00', mode=mode, caller="ENGINE")
                            odno = (out or {}).get("ODNO") or (out or {}).get("odno")
                            # ladder 주문 시도도 이력에 기록(상세 UI용)
                            try:
                                history["buy_attempts"].append({
                                    "symbol": symbol,
                                    "exchange": exchange,
                                    "qty": int(remaining),
                                    "price": float(ask_price),
                                    "method": "ask_ladder",
                                    "level": int(level_idx + 1),
                                    "ok": bool(out),
                                    "order_no": odno,
                                })
                            except Exception:
                                pass
                            if not odno:
                                log.warning(f"[Engine] {symbol} 매수 주문 실패(주문번호 없음)")
                                # 안전상 추가 주문을 진행하지 않는다(중복/과매수 방지).
                                _trace(
                                    "buy.ladder.order.no_order_no",
                                    symbol=symbol,
                                    exchange=exchange,
                                    level=int(level_idx + 1),
                                )
                                return False, "order_no_missing"

                            # 짧게 대기 후 미체결 잔량 확인
                            time_module.sleep(max(0.2, limit_buy_step_wait_sec))
                            unfilled_qty = _find_unfilled_qty_by_odno(odno)
                            if unfilled_qty <= 0:
                                try:
                                    # 매수 체결(또는 미체결 목록에서 제거됨) 시점 기준으로 보유일 갱신
                                    store.set_open_date(symbol=symbol, open_date=datetime.now().strftime("%Y%m%d"), source="buy_exec")
                                except Exception:
                                    pass
                                log.info(f"[Engine] {symbol} 매수 체결(또는 미체결 목록에서 제거됨): odno={odno}")
                                _trace(
                                    "buy.ladder.filled_or_removed",
                                    symbol=symbol,
                                    exchange=exchange,
                                    level=int(level_idx + 1),
                                    order_no=str(odno),
                                )
                                return True, "filled_or_removed"

                            # 마지막 단계면 미체결을 남기고 종료(요청 정책)
                            if level_idx >= (used_levels - 1):
                                log.warning(
                                    f"[Engine] {symbol} 마지막 호가 단계 미체결 잔량 {unfilled_qty}주 → 취소하지 않고 종료 (odno={odno})"
                                )
                                _trace(
                                    "buy.ladder.unfilled_last_level_left",
                                    symbol=symbol,
                                    exchange=exchange,
                                    level=int(level_idx + 1),
                                    order_no=str(odno),
                                    unfilled_qty=int(unfilled_qty),
                                )
                                return False, "unfilled_left_last_level"

                            # 잔량 취소 후 다음 호가로 재시도(중복 미체결 방지)
                            log.info(f"[Engine] {symbol} 미체결 잔량 {unfilled_qty}주 → 취소 후 다음 호가로 재시도 (odno={odno})")
                            _trace(
                                "buy.ladder.unfilled",
                                symbol=symbol,
                                exchange=exchange,
                                level=int(level_idx + 1),
                                order_no=str(odno),
                                unfilled_qty=int(unfilled_qty),
                            )
                            cncl = kis_order.revise_cancel_order(
                                exchange=exchange,
                                symbol=symbol,
                                origin_order_no=str(odno),
                                qty=int(unfilled_qty),
                                price=0,
                                action="cancel",
                                mode=mode,
                                caller="ENGINE",
                            )
                            _trace("buy.ladder.cancel", symbol=symbol, exchange=exchange, order_no=str(odno), unfilled_qty=int(unfilled_qty), ok=bool(cncl))
                            if not cncl:
                                log.warning(f"[Engine] {symbol} 잔량 취소 실패 → 중복 주문 방지 위해 재시도 중단 (odno={odno})")
                                # 취소 실패 시 중복 주문 위험이 크므로 폴백 포함 추가 주문 금지
                                _trace(
                                    "buy.ladder.cancel_failed",
                                    symbol=symbol,
                                    exchange=exchange,
                                    level=int(level_idx + 1),
                                    order_no=str(odno),
                                    unfilled_qty=int(unfilled_qty),
                                )
                                return False, "cancel_failed"

                            remaining = int(unfilled_qty)
                            _trace(
                                "buy.ladder.level.next",
                                symbol=symbol,
                                exchange=exchange,
                                next_level=int(level_idx + 2),
                                remaining=int(remaining),
                            )
                            # 다음 단계로 넘어가기 전 과도한 호출 방지
                            time_module.sleep(0.4)

                        # 여기까지 왔으면 최대 레벨까지 시도했으나 잔량이 남은 케이스
                        log.warning(f"[Engine] {symbol} 지정가 호가 상향 시도 후에도 미체결 잔량이 남아 매수 완료 실패(remaining={remaining})")
                        # 부분체결 가능성이 있으므로 폴백 포함 추가 주문 금지
                        _trace(
                            "buy.ladder.unfilled_remaining",
                            symbol=symbol,
                            exchange=exchange,
                            remaining=int(remaining),
                        )
                        return False, "unfilled_remaining"

                    if qty > 0:
                        if mode == "real" and buy_order_method == "limit_ask_ladder":
                            # 매수 전 선취소: 동일 종목 미체결 주문이 있으면 취소하고 진행 (실전)
                            try:
                                _cancel_unfilled_for_symbol(exchange, symbol, side_filter="buy")
                            except Exception:
                                pass
                            ok, reason = _buy_with_ask_ladder()
                            if (not ok) and (reason in ("ask_api_failed", "asks_empty")):
                                # 실전에서 "호가 조회 자체"가 불가한 환경이면 ladder를 시작할 수 없다.
                                # 이 경우에만(=ladder 주문을 넣기 전) 기존 방식으로 1회 폴백한다.
                                buy_price = current_price * (1.0 + (slippage_pct / 100.0))
                                log.info(f"[Engine] ladder 불가({reason}) → 슬리피지 지정가 1회 폴백: {symbol} {qty}주 (@{buy_price})")
                                out = kis_order.order(symbol, qty, buy_price, 'buy', exchange=exchange, order_type='00', mode=mode, caller="ENGINE")
                                history["buy_attempts"].append({
                                    "symbol": symbol,
                                    "exchange": exchange,
                                    "qty": qty,
                                    "price": buy_price,
                                    "method": "slippage_fallback",
                                    "ok": bool(out),
                                    "order_no": (out or {}).get("ODNO") or (out or {}).get("odno"),
                                    "note": f"ladder_unavailable:{reason}",
                                })
                            elif not ok:
                                # 가드 발동/부분체결/취소 실패 등 "중복/과매수 위험" 케이스에서는 추가 주문을 금지한다.
                                log.warning(f"[Engine] ladder 실패({reason}) → 안전상 추가 폴백 주문을 생략합니다.")
                                history["skips"].append({"side": "buy", "symbol": symbol, "reason": f"ladder_failed_no_fallback:{reason}"})
                        else:
                            # 매수 전 선취소: 동일 종목 미체결 주문이 있으면 취소하고 진행 (실전)
                            try:
                                _cancel_unfilled_for_symbol(exchange, symbol, side_filter="buy")
                            except Exception:
                                pass
                            buy_price = planned_buy_price
                            log.info(f"[Engine] 매수 주문 실행: {symbol}({exchange}) {qty}주 (@{buy_price})")
                            out = kis_order.order(symbol, qty, buy_price, 'buy', exchange=exchange, order_type='00', mode=mode, caller="ENGINE")
                            history["buy_attempts"].append({
                                "symbol": symbol,
                                "exchange": exchange,
                                "qty": qty,
                                "price": buy_price,
                                "method": "slippage",
                                "ok": bool(out),
                                "order_no": (out or {}).get("ODNO") or (out or {}).get("odno"),
                            })
                    # qty<=0 케이스는 상단에서 스킵 처리(중복 기록 방지)

            log.info("=== 자동매매 엔진 실행 완료 ===")
            # status 요약
            buy_ok = any(bool(x.get("ok")) for x in (history.get("buy_attempts") or []))
            sell_ok = any(bool(x.get("ok")) for x in (history.get("sell_attempts") or []))
            if buy_ok or sell_ok:
                history["status"] = "success"
            elif (history.get("buy_attempts") or history.get("sell_attempts")) or (history.get("skips") or history.get("errors")):
                history["status"] = "partial"
            else:
                history["status"] = "no_trade"
            history["message"] = history.get("message") or "ok"

            # (스케줄 실행 기록은 위에서 선 마킹 처리)

        except Exception as e:
            log = get_mode_logger(config_manager.get('common.mode', 'mock'), "ENGINE")
            try:
                log.error(f"[Engine] 실행 중 오류 발생: {e} | step={last_step} | ctx={last_context}")
            except Exception:
                log.error(f"[Engine] 실행 중 오류 발생: {e}")
            self.last_error = str(e)
            import traceback
            log.error(traceback.format_exc())
            history["status"] = "error"
            try:
                history["errors"].append({"kind": "exception", "error": str(e), "step": last_step, "context": last_context})
            except Exception:
                history["errors"].append(str(e))
            history["message"] = f"exception:{e}"
        finally:
            try:
                history["finished_at"] = datetime.now().isoformat(timespec="seconds")
                ExecutionHistoryStore(mode=mode).append(history)
            except Exception:
                # 이력 저장 실패는 매매 실패로 간주하지 않는다.
                pass
            set_engine_api_logging(mode, False)
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

    def run_once_sell_only(self, mode: str | None = None):
        """
        매도 전용 1회 실행:
        - 분석서버 요청 없이 매수 로직을 제외하고 매도 로직만 수행
        - auto_trading_enabled가 OFF여도 1회 실행은 허용
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        analysis_data = {"buy": [], "sell": []}
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
        log = get_mode_logger(mode, "ENGINE")
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

            balance_info = kis_order.get_balance(mode=mode, caller="ENGINE")
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
                    qty = int(float(stock.get('ord_psbl_qty') or 0))
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

                # 시장가(가격=0) 우선 시도, 실패 시 지정가 폴백
                out = kis_order.order(symbol, qty, 0, 'sell', exchange=exchange, order_type='00', mode=mode, caller="ENGINE")
                sell_price = 0.0
                method = "market_0"
                if not out:
                    px = kis_quote.get_current_price(exchange, symbol, mode=mode, caller="ENGINE") or {}
                    sell_price = float(px.get("last", 0) or 0)
                    if sell_price <= 0:
                        continue
                    sell_price = sell_price * (1.0 - (slippage_pct / 100.0))
                    out = kis_order.order(symbol, qty, sell_price, 'sell', exchange=exchange, order_type='00', mode=mode, caller="ENGINE")
                    method = "limit_fallback"

                log.info(f"[StopWatch] 장중 감시 매도: {symbol} qty={qty}, rate={profit_rate}%, threshold={threshold_pct}%, price={sell_price}, method={method}")
                if out:
                    self._stop_loss_cooldown[symbol] = now

        except Exception as e:
            self.last_stop_watch_error = str(e)
            log.error(f"[StopWatch] 오류: {e}")

# 전역 인스턴스
trading_engine = TradingEngine()
