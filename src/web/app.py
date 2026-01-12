from flask import Flask, render_template, jsonify, request, redirect
import threading
import os
import glob
import yaml
from pathlib import Path
from datetime import datetime, timedelta
from uuid import uuid4
from src.engine.engine import trading_engine
from src.engine.multi_process_scheduler import multi_process_scheduler
from src.engine.scheduler_state_store import SchedulerStateStore
from src.config.config_manager import config_manager
from src.api.order import kis_order
from src.api.quote import kis_quote
from src.utils.logger import logger
from src.engine.position_store import PositionStore
from src.utils.fx_rate import get_usd_krw_rate

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"

app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(STATIC_DIR),
)

# 멀티프로세스 스케줄러(모의/실전 동시 실행)
_scheduler_started = False
_scheduler_lock = threading.Lock()

def start_scheduler():
    """
    스케줄러 시작은 import 시점이 아니라 '서버 실행 시점'에만 수행.
    - 모의/실전은 서로 간섭 없이 동시 실행(키움 샘플과 동일한 운영 패턴)
    - 멀티프로세스이므로 웹 접속 여부와 무관하게 서버 프로세스가 살아있으면 돌아간다.
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return

        multi_process_scheduler.start()
        _scheduler_started = True


@app.route("/api/scheduler/<action>", methods=["POST"])
def api_scheduler_control(action: str):
    """
    스케줄러 운영 제어 (키움 샘플 수준의 운영 편의)
    - action: start|stop|restart
    - body: {"mode":"mock"|"real"|"all"}
    """
    try:
        payload = request.json or {}
        mode = (payload.get("mode") or "all").strip().lower()
        action = (action or "").strip().lower()
        if action not in ("start", "stop", "restart"):
            return jsonify({"success": False, "message": "invalid_action"})
        if mode not in ("mock", "real", "all"):
            return jsonify({"success": False, "message": "invalid_mode"})

        if action == "start":
            if mode == "all":
                multi_process_scheduler.start()
            elif mode == "mock":
                multi_process_scheduler.mock.start()
            else:
                multi_process_scheduler.real.start()
        elif action == "stop":
            if mode == "all":
                multi_process_scheduler.stop()
            elif mode == "mock":
                multi_process_scheduler.mock.stop()
            else:
                multi_process_scheduler.real.stop()
        else:  # restart
            if mode == "all":
                multi_process_scheduler.stop()
                multi_process_scheduler.start()
            elif mode == "mock":
                multi_process_scheduler.mock.stop()
                multi_process_scheduler.mock.start()
            else:
                multi_process_scheduler.real.stop()
                multi_process_scheduler.real.start()

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

# 즉시 실행 미리보기(서버 메모리 임시 저장)
_TRADE_PREVIEWS: dict[str, dict] = {}
# 분석서버 실시간 분석은 수분~수십분까지 걸릴 수 있어 TTL을 넉넉히 잡는다.
_TRADE_PREVIEW_TTL_SEC = 1800  # 30분

@app.route('/')
def dashboard():
    """대시보드 메인 페이지"""
    return render_template('dashboard.html')

@app.route('/server-selection')
def server_selection():
    return render_template('server_selection.html')

@app.route('/portfolio')
def portfolio_page():
    return render_template('portfolio.html')

@app.route('/orders')
def orders_page():
    return render_template('orders.html')

@app.route('/auto-trading')
def auto_trading_page():
    return render_template('auto_trading.html')

@app.route('/api-test')
def api_test_page():
    return render_template('api_test.html')


# ===== API Test helpers (KIS 가이드/프로젝트 사용 API를 UI에서 쉽게 호출하기 위한 전용 엔드포인트) =====
def _api_test_mode() -> str:
    return request.args.get("mode") or (request.json.get("mode") if request.is_json and request.json else None) or config_manager.get("common.mode", "mock")


@app.route('/api/test/balance')
def api_test_balance():
    """KIS v1_해외주식-006 (inquire-balance) RAW 응답"""
    try:
        mode = _api_test_mode()
        exchange = request.args.get("exchange", "NASD")
        currency = request.args.get("currency", "USD")
        data = kis_order.get_balance(exchange=exchange, currency=currency, mode=mode)
        return jsonify({"success": bool(data), "data": data, "mode": mode})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/test/present-balance')
def api_test_present_balance():
    """KIS v1_해외주식-008 (inquire-present-balance) RAW 응답"""
    try:
        mode = _api_test_mode()
        natn_cd = request.args.get("natn_cd", "000")
        tr_mket_cd = request.args.get("tr_mket_cd", "00")
        inqr_dvsn_cd = request.args.get("inqr_dvsn_cd", "00")
        wcrc_frcr_dvsn_cd = request.args.get("wcrc_frcr_dvsn_cd", "02")
        data = kis_order.get_present_balance(
            natn_cd=natn_cd,
            tr_mket_cd=tr_mket_cd,
            inqr_dvsn_cd=inqr_dvsn_cd,
            wcrc_frcr_dvsn_cd=wcrc_frcr_dvsn_cd,
            mode=mode,
        )
        return jsonify({"success": bool(data), "data": data, "mode": mode})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/test/foreign-margin')
def api_test_foreign_margin():
    """KIS 해외주식-035 (foreign-margin) RAW 응답 (실전 전용)"""
    try:
        mode = _api_test_mode()
        if mode == "mock":
            return jsonify({"success": False, "message": "real_only", "mode": mode})
        data = kis_order.get_foreign_margin(mode=mode)
        return jsonify({"success": bool(data), "data": data, "mode": mode})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/test/quote/asking')
def api_test_quote_asking():
    """KIS 해외주식-033 (현재가 호가) RAW 응답 (실전 전용)"""
    try:
        mode = _api_test_mode()
        exchange = request.args.get("exchange", "NAS")
        symbol = request.args.get("symbol")
        if not symbol:
            return jsonify({"success": False, "message": "missing_symbol", "mode": mode})
        if mode == "mock":
            return jsonify({"success": False, "message": "real_only", "mode": mode})
        data = kis_quote.get_asking_price(exchange=exchange, symbol=symbol, mode=mode)
        return jsonify({"success": bool(data), "data": data, "mode": mode})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/test/period-profit/raw')
def api_test_period_profit_raw():
    """KIS v1_해외주식-032 (기간손익) RAW 응답 (실전 전용)"""
    try:
        mode = _api_test_mode()
        start_date = (request.args.get("start_date") or "").replace("-", "")
        end_date = (request.args.get("end_date") or "").replace("-", "")
        exchange = request.args.get("exchange") or ""
        currency_div = request.args.get("currency_div") or "01"
        if not (start_date and end_date) or len(start_date) != 8 or len(end_date) != 8:
            return jsonify({"success": False, "message": "invalid_date", "mode": mode})
        if mode == "mock":
            return jsonify({"success": False, "message": "real_only", "mode": mode})
        data = kis_order.get_period_profit(
            start_date=start_date,
            end_date=end_date,
            exchange=exchange,
            currency_div=currency_div,
            mode=mode,
        )
        return jsonify({"success": bool(data), "data": data, "mode": mode})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


def _api_test_require_order_confirm(payload: dict, action_label: str) -> tuple[bool, str]:
    """
    API 테스트 페이지에서만 쓰는 주문 안전장치.
    - 프론트 체크박스/입력값을 백엔드에서도 재검증한다.
    """
    ack = bool(payload.get("ack"))
    text = (payload.get("confirm_text") or "").strip()
    if not ack:
        return False, f"missing_ack_for_{action_label}"
    if text != "동의합니다":
        return False, f"missing_confirm_text_for_{action_label}"
    return True, ""


@app.route('/api/test/orders/cancel', methods=['POST'])
def api_test_orders_cancel():
    """정정/취소 테스트(취소) - 안전장치 포함"""
    try:
        payload = request.json or {}
        ok, msg = _api_test_require_order_confirm(payload, "cancel")
        if not ok:
            return jsonify({"success": False, "message": msg})

        mode = payload.get("mode") or config_manager.get("common.mode", "mock")
        exchange = payload.get("exchange") or "NASD"
        symbol = payload.get("symbol") or ""
        origin_order_no = payload.get("origin_order_no") or payload.get("origin_order_no".upper()) or ""
        qty = int(payload.get("qty") or 0)
        if not (symbol and origin_order_no and qty > 0):
            return jsonify({"success": False, "message": "missing_params"})

        out = kis_order.revise_cancel_order(
            exchange=exchange,
            symbol=symbol,
            origin_order_no=origin_order_no,
            qty=qty,
            price=0.0,
            action="cancel",
            mode=mode,
        )
        return jsonify({"success": bool(out), "data": out, "mode": mode})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/test/orders/revise', methods=['POST'])
def api_test_orders_revise():
    """정정/취소 테스트(정정) - 안전장치 포함"""
    try:
        payload = request.json or {}
        ok, msg = _api_test_require_order_confirm(payload, "revise")
        if not ok:
            return jsonify({"success": False, "message": msg})

        mode = payload.get("mode") or config_manager.get("common.mode", "mock")
        exchange = payload.get("exchange") or "NASD"
        symbol = payload.get("symbol") or ""
        origin_order_no = payload.get("origin_order_no") or ""
        qty = int(payload.get("qty") or 0)
        price = float(payload.get("price") or 0)
        if not (symbol and origin_order_no and qty > 0 and price > 0):
            return jsonify({"success": False, "message": "missing_params"})

        out = kis_order.revise_cancel_order(
            exchange=exchange,
            symbol=symbol,
            origin_order_no=origin_order_no,
            qty=qty,
            price=price,
            action="revise",
            mode=mode,
        )
        return jsonify({"success": bool(out), "data": out, "mode": mode})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/test/orders/place', methods=['POST'])
def api_test_orders_place():
    """
    해외주식 주문(v1_해외주식-001) 테스트 - 안전상 모의(mock)만 허용
    (실전 주문은 이 화면에서 지원하지 않음)
    """
    try:
        payload = request.json or {}
        ok, msg = _api_test_require_order_confirm(payload, "place")
        if not ok:
            return jsonify({"success": False, "message": msg})

        mode = payload.get("mode") or config_manager.get("common.mode", "mock")
        if mode != "mock":
            return jsonify({"success": False, "message": "mock_only_for_safety", "mode": mode})

        exchange = payload.get("exchange") or "NASD"
        symbol = (payload.get("symbol") or "").strip()
        side = (payload.get("side") or "buy").strip()
        order_type = (payload.get("order_type") or "00").strip()
        qty = int(payload.get("qty") or 0)
        price = float(payload.get("price") or 0)
        if not (symbol and qty > 0):
            return jsonify({"success": False, "message": "missing_params"})

        out = kis_order.order(
            symbol=symbol,
            quantity=qty,
            price=price,
            side=side,
            exchange=exchange,
            order_type=order_type,
            mode=mode,
        )
        return jsonify({"success": bool(out), "data": out, "mode": mode})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/trading-diary')
def trading_diary_page():
    return render_template('trading_diary.html')

@app.route('/api/orders/history')
def api_orders_history():
    """
    주문체결내역(v1_해외주식-007) 조회 (UI용)
    - 기본: 최근 7일, 전체조회
    - 실전: 필터/연속조회 지원
    """
    try:
        mode = request.args.get("mode") or config_manager.get("common.mode", "mock")

        # 기간
        days = int(request.args.get("days", "7"))
        end = request.args.get("end_date") or datetime.now().strftime("%Y%m%d")
        start = request.args.get("start_date") or (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

        # 필터(실전에서만 의미 있음. 모의는 내부에서 제약에 맞춰 무시됨)
        pdno = request.args.get("symbol")
        sll_buy_dvsn = request.args.get("sll_buy_dvsn", "00")  # 00/01/02
        ccld_nccs_dvsn = request.args.get("ccld_nccs_dvsn", "00")  # 00/01/02
        ovrs_excg_cd = request.args.get("exchange")  # NASD/NYSE/...
        sort_sqn = request.args.get("sort_sqn", "DS")
        ctx_area_nk200 = request.args.get("ctx_area_nk200", "")
        ctx_area_fk200 = request.args.get("ctx_area_fk200", "")

        data = kis_order.get_order_history(
            start_date=start,
            end_date=end,
            pdno=pdno,
            sll_buy_dvsn=sll_buy_dvsn,
            ccld_nccs_dvsn=ccld_nccs_dvsn,
            ovrs_excg_cd=ovrs_excg_cd,
            sort_sqn=sort_sqn,
            ctx_area_nk200=ctx_area_nk200,
            ctx_area_fk200=ctx_area_fk200,
            mode=mode,
        )
        if not data:
            return jsonify({"success": False, "message": "no_data"})
        return jsonify({"success": True, "data": data, "mode": mode})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/settings')
def settings_page():
    """설정 페이지"""
    config = config_manager._config
    return render_template('settings.html', config=config)

@app.route('/api/status')
def get_status():
    """현재 상태 및 잔고 조회 (AJAX)"""
    # 멀티프로세스 스케줄러 상태(모드별 하트비트 파일) 기반으로 표시
    def _read_scheduler(mode: str) -> dict:
        try:
            return SchedulerStateStore(mode=mode).read() or {}
        except Exception:
            return {}

    sch_mock = _read_scheduler("mock")
    sch_real = _read_scheduler("real")

    def _as_iso(v):
        try:
            return str(v) if v else None
        except Exception:
            return None

    # 모드별 다음 실행시각(UI용, 스케줄러 프로세스의 loop 지연과 무관한 "논리적 다음 실행")
    def _next_run_for(m: str):
        try:
            dt = trading_engine.get_next_scheduled_run_at(mode=m)
            return dt.isoformat() if dt else None
        except Exception:
            return None

    next_run_mock = _next_run_for("mock")
    next_run_real = _next_run_for("real")

    # 현재 모드의 엔진 상태는 '해당 모드 스케줄러 프로세스'가 기록한 값으로 제공
    mode = config_manager.get("common.mode", "mock")
    sch_cur = sch_mock if mode == "mock" else sch_real

    status = {
        "market_open": trading_engine.is_market_open(),
        "is_running": bool(sch_cur.get("is_executing", False)),
        "mode": mode,
        "engine_last_run_at": _as_iso(sch_cur.get("engine_last_run_at")),
        "engine_last_error": sch_cur.get("engine_last_error"),
        "engine_next_run_at": next_run_mock if mode == "mock" else next_run_real,
        "stop_watch_last_run_at": _as_iso(sch_cur.get("stop_watch_last_run_at")),
        "stop_watch_last_error": sch_cur.get("stop_watch_last_error"),
        "stop_watch_next_run_at": None,
        # 확장: 모드별 스케줄러 상태(디버깅/운영 확인용)
        "schedulers": {
            "mock": {**sch_mock, "engine_next_run_at": next_run_mock},
            "real": {**sch_real, "engine_next_run_at": next_run_real},
        },
    }
    
    # 잔고 조회 (실시간성을 위해 API 호출)
    # - v1_006(해외주식 잔고): 보유 종목/평가손익(종목별) 위주
    # - v1_008(체결기준현재잔고) output3: 총자산/예수금/외화사용가능/총평가손익/평가수익률(가이드 기준)
    mode = config_manager.get("common.mode", "mock")
    balance_info = kis_order.get_balance(mode=mode) or {}
    # NATN_CD=000(전체)로 조회해야 통화별/전체 잔고 요약(output3)이 안정적으로 내려오는 편이다.
    # (미국 840로 고정하면 계좌/상황에 따라 0으로 내려오는 케이스가 있었다)
    present_info = kis_order.get_present_balance(natn_cd="000", tr_mket_cd="00", inqr_dvsn_cd="00", wcrc_frcr_dvsn_cd="02", mode=mode) or {}

    out3 = present_info.get("output3") or {}
    out2_raw = present_info.get("output2")
    # KIS 응답은 output2가 dict 또는 list로 내려올 수 있어 방어적으로 처리
    out2 = {}
    if isinstance(out2_raw, dict):
        out2 = out2_raw
    elif isinstance(out2_raw, list) and out2_raw:
        out2 = out2_raw[0] if isinstance(out2_raw[0], dict) else {}

    def _to_float(v, default=0.0):
        """
        숫자/문자열/None 모두 안전 변환.
        default=None 인 경우 float(None)로 죽지 않도록 None 그대로 반환한다.
        """
        try:
            if v is None:
                return default if default is None else float(default)
            s = str(v).replace(",", "").strip()
            if s == "":
                return default if default is None else float(default)
            return float(s)
        except Exception:
            return default if default is None else float(default)

    # v1_008 output3의 evlu_erng_rt1(평가수익율1)이 모의에서 0으로 내려오는 경우가 있어,
    # pchs_amt_smtl(매입금액합계)와 evlu_pfls_amt_smtl(평가손익금액합계)로 수익률을 계산해 보완한다.
    pchs_amt_smtl = _to_float(out3.get("pchs_amt_smtl"))
    evlu_pfls_amt_smtl = _to_float(out3.get("evlu_pfls_amt_smtl"))
    computed_profit_rate = (evlu_pfls_amt_smtl / pchs_amt_smtl * 100.0) if pchs_amt_smtl != 0 else 0.0

    raw_profit_rate = _to_float(out3.get("evlu_erng_rt1"), default=None)
    # raw_profit_rate가 0인데 평가손익이 존재하면 계산값을 사용(가이드 기반 fallback)
    profit_rate_krw = raw_profit_rate
    if profit_rate_krw is None:
        profit_rate_krw = computed_profit_rate
    elif abs(profit_rate_krw) < 1e-12 and abs(evlu_pfls_amt_smtl) > 0:
        profit_rate_krw = computed_profit_rate

    # v1_006: 보유 종목 리스트 (대시보드/포트폴리오 표에 사용)
    stocks = (balance_info.get("output1") or [])

    # 자동 환율(원/달러): 사용자 입력/설정값은 사용하지 않고, KIS → FinanceDataReader 순으로 자동 조회
    fx = get_usd_krw_rate(mode=mode, kis_present=present_info)
    usd_krw_rate_effective = fx.rate or 0.0
    usd_krw_rate_src = fx.source

    # 보유기간(일) 계산: 가능하면 v1_007 주문체결내역으로 "최초 매수 체결일"을 추정/확정한다.
    # - 과도한 API 호출 방지: 1일 1회만 동기화(파일 기반 PositionStore meta)
    try:
        store = PositionStore(mode=mode)
        today = datetime.now().strftime("%Y%m%d")
        held_symbols = []
        for s in stocks:
            try:
                sym = (s.get("ovrs_pdno") or "").strip().upper()
                qty = int(float(s.get("ovrs_cblc_qty", 0) or 0))
                exch = (s.get("ovrs_excg_cd") or "").strip().upper() or "NASD"
                if sym and qty > 0:
                    held_symbols.append(sym)
                    store.upsert(symbol=sym, qty=qty, exchange=exch)
            except Exception:
                continue

        # 잔고에 없는 종목은 store에서도 정리
        for sym in store.all_symbols():
            if sym not in held_symbols:
                store.upsert(symbol=sym, qty=0)

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
            # 주문체결내역 조회는 호출 제한/조회제약이 있을 수 있어, 최근 N일만 조회한다.
            # - 모의: 범위를 너무 크게 잡으면 실패/빈값이 나오는 경우가 있어 보수적으로 짧게
            # - 실전: 필요 시 늘릴 수 있음
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
                # 휴리스틱: 'buy/매수' 포함 또는 코드값이 2 계열이면 매수로 간주
                if ("buy" in v) or ("매수" in v):
                    return True
                if v in ("02", "2", "buy"):
                    return True
                return False

            def _filled_qty(row: dict) -> float:
                # v1_007(주문체결내역)에서 모의/실전 필드명이 다를 수 있어 폭넓게 대응
                for k in (
                    "ft_ccld_qty",  # 모의: 해외체결수량
                    "ccld_qty",
                    "CCLD_QTY",
                    "ccld_qty1",
                    "ccld_qty2",
                    "tot_ccld_qty",
                    "tot_ccld_qty1",
                    "ft_ord_qty",  # 최악의 폴백(주문수량)
                ):
                    if k in row and row.get(k) is not None:
                        try:
                            return float(str(row.get(k)).replace(",", ""))
                        except Exception:
                            pass
                return 0.0

            last_buy_date: dict[str, str] = {}
            for r in rows:
                if not isinstance(r, dict):
                    continue
                sym = (r.get("pdno") or r.get("PDNO") or r.get("ovrs_pdno") or "").strip().upper()
                if not sym or sym not in held_symbols:
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

        # stocks에 보유기간 필드 주입
        for s in stocks:
            try:
                sym = (s.get("ovrs_pdno") or "").strip().upper()
                if not sym:
                    continue
                od = store.get_open_date(sym)
                s["open_date"] = od
                if od and len(od) == 8:
                    days = (datetime.now().date() - datetime.strptime(od, "%Y%m%d").date()).days
                    s["holding_days"] = int(days)
                else:
                    s["holding_days"] = None
            except Exception:
                s["holding_days"] = None
    except Exception:
        # 보유기간은 보조정보이므로 실패해도 status는 반환
        pass

    # 주문가능금액(USD) 산정:
    # - 실전: 해외주식-035(해외증거금 통화별조회) USD의 itgr_ord_psbl_amt(통합주문가능금액)을 최우선 사용
    # - 모의: 해외주식-035 미지원.
    #   v1_008 output3.frcr_use_psbl_amt가 0으로 내려오는 케이스가 있어,
    #   output2(통화별)에서 USD의 외화출금가능금액(frcr_drwg_psbl_amt_1) 또는 외화예수금(frcr_dncl_amt_2)을 우선 사용한다.
    #   그래도 0이면(=모의에서 USD 예수금이 안 내려오는 케이스), 통합증거금 효과를 "총자산(원화) / 환율"로 추정해
    #   자동매매 예산 산정이 0으로 막히지 않게 한다(모의 모드에서만).
    fx_orderable_amt = None
    fx_orderable_source = None
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
            if usd:
                fx_orderable_amt = _to_float(usd.get("itgr_ord_psbl_amt"), default=None)
                fx_orderable_source = "035_itgr"
        if fx_orderable_amt is None:
            # mock(또는 실전 035 실패) fallback
            usd_row = None
            try:
                if isinstance(out2_raw, list):
                    for r in out2_raw:
                        if isinstance(r, dict) and (str(r.get("crcy_cd") or "").strip().upper() == "USD"):
                            usd_row = r
                            break
            except Exception:
                usd_row = None

            if usd_row:
                # 출금가능 외화금액(우선) -> 예수금
                fx_orderable_amt = _to_float(usd_row.get("frcr_drwg_psbl_amt_1"), default=None)
                if fx_orderable_amt is None or fx_orderable_amt <= 0:
                    fx_orderable_amt = _to_float(usd_row.get("frcr_dncl_amt_2"), default=None)
                fx_orderable_source = "008_out2_usd"

            if fx_orderable_amt is None:
                fx_orderable_amt = _to_float(out3.get("frcr_use_psbl_amt"), default=0.0)
                fx_orderable_source = "008_frcr_use"

            # mock 마지막 fallback: 총자산(원화) 기반 추정(통합증거금/자동환전 느낌의 "총가용"을 흉내)
            if mode == "mock" and (fx_orderable_amt is None or fx_orderable_amt <= 0):
                tot_asst_krw = _to_float(out3.get("tot_asst_amt"), default=0.0)
                if usd_krw_rate_effective > 0 and tot_asst_krw > 0:
                    fx_orderable_amt = tot_asst_krw / usd_krw_rate_effective
                    fx_orderable_source = "mock_est_tot_asset"
    except Exception:
        fx_orderable_amt = _to_float(out3.get("frcr_use_psbl_amt"), default=0.0)
        fx_orderable_source = "008_frcr_use"

    balance = {
        "stocks": stocks,

        # v1_008(output3): 자산 요약 (원화 기준이 대부분)
        "total_asset_krw": out3.get("tot_asst_amt", "0"),          # 총자산금액
        "eval_amount_krw": out3.get("evlu_amt_smtl") or out3.get("evlu_amt_smtl_amt", "0"),  # 평가금액합계(원화)
        "deposit_krw": out3.get("tot_dncl_amt") or out3.get("dncl_amt", "0"),  # (총)예수금액
        "withdrawable_krw": out3.get("wdrw_psbl_tot_amt", "0"),    # 인출가능총금액
        "fx_use_psbl_amt": out3.get("frcr_use_psbl_amt", "0"),     # 외화사용가능금액(통화는 계좌/시장 상황에 따름)
        # 주문가능금액(USD) - "총예산 ÷ 매수종목수" 산정에 사용
        "fx_orderable_amt": str(fx_orderable_amt),
        "fx_orderable_source": fx_orderable_source,
        # USD/KRW 환율(원/달러) - 자동(008) 우선, 설정값은 fallback
        "usd_krw_rate": str(usd_krw_rate_effective) if usd_krw_rate_effective > 0 else "0",
        "usd_krw_rate_source": usd_krw_rate_src or "unavailable",
        # 평가손익/수익률은 가이드상 원화환산 합계가 존재하므로 우선 사용
        "total_profit_krw": out3.get("evlu_pfls_amt_smtl") or out3.get("tot_evlu_pfls_amt", "0"),  # 평가손익금액합계(우선) / 총평가손익금액(대체)
        "profit_rate_krw": str(profit_rate_krw),                    # 평가수익율1(우선) / 계산값 fallback

        # 하위 호환(기존 UI/코드가 참조하던 키). 이제 '총자산/손익/수익률'은 v1_008 기준으로 맞춘다.
        "total_asset": out3.get("tot_asst_amt", "0"),
        "total_profit": out3.get("evlu_pfls_amt_smtl") or out3.get("tot_evlu_pfls_amt", "0"),
        "profit_rate": str(profit_rate_krw),
    }
    
    return jsonify({"status": status, "balance": balance})

@app.route('/api/orders/unfilled')
def api_orders_unfilled():
    """
    - 실전: v1_해외주식-005 미체결내역
    - 모의: 미지원 -> v1_007 주문체결내역에서 nccs_qty > 0 기반으로 대체(근사)
    """
    try:
        mode = request.args.get("mode") or config_manager.get("common.mode", "mock")

        if mode == "real":
            data = kis_order.get_unfilled_orders(mode=mode)
            return jsonify({"success": True, "mode": mode, "source": "v1_005", "data": data or []})

        # mock fallback
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
        hist = kis_order.get_order_history(start_date=start, end_date=end, mode=mode)
        rows = (hist or {}).get("output") or []
        rows = rows if isinstance(rows, list) else [rows]
        unfilled = []
        for r in rows:
            try:
                if int(float(r.get("nccs_qty", 0) or 0)) > 0:
                    unfilled.append(r)
            except Exception:
                continue
        return jsonify({"success": True, "mode": mode, "source": "v1_007_fallback", "data": unfilled})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/orders/cancel', methods=['POST'])
def api_orders_cancel():
    """정정취소(v1_003) - 취소"""
    try:
        payload = request.json or {}
        mode = payload.get("mode") or config_manager.get("common.mode", "mock")
        exchange = payload.get("exchange")
        symbol = payload.get("symbol")
        origin_order_no = payload.get("origin_order_no")
        qty = int(payload.get("qty", 0))

        if not (exchange and symbol and origin_order_no and qty > 0):
            return jsonify({"success": False, "message": "missing_params"})

        out = kis_order.revise_cancel_order(
            exchange=exchange,
            symbol=symbol,
            origin_order_no=origin_order_no,
            qty=qty,
            price=0,
            action="cancel",
            mode=mode,
        )
        if out:
            return jsonify({"success": True, "data": out})
        return jsonify({"success": False, "message": "cancel_failed"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/orders/revise', methods=['POST'])
def api_orders_revise():
    """정정취소(v1_003) - 정정(가격/수량)"""
    try:
        payload = request.json or {}
        mode = payload.get("mode") or config_manager.get("common.mode", "mock")
        exchange = payload.get("exchange")
        symbol = payload.get("symbol")
        origin_order_no = payload.get("origin_order_no")
        qty = payload.get("qty")
        price = payload.get("price")

        if not (exchange and symbol and origin_order_no and qty is not None and price is not None):
            return jsonify({"success": False, "message": "missing_params"})

        out = kis_order.revise_cancel_order(
            exchange=exchange,
            symbol=symbol,
            origin_order_no=origin_order_no,
            qty=int(qty),
            price=float(price),
            action="revise",
            mode=mode,
        )
        if out:
            return jsonify({"success": True, "data": out})
        return jsonify({"success": False, "message": "revise_failed"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/quote/current')
def api_quote_current():
    try:
        mode = request.args.get("mode") or config_manager.get("common.mode", "mock")
        exchange = request.args.get("exchange", "NAS")
        symbol = request.args.get("symbol")
        if not symbol:
            return jsonify({"success": False, "message": "missing_symbol"})
        out = kis_quote.get_current_price(exchange, symbol, mode=mode)
        return jsonify({"success": True, "data": out})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/quote/detail')
def api_quote_detail():
    try:
        mode = request.args.get("mode") or config_manager.get("common.mode", "mock")
        exchange = request.args.get("exchange", "NAS")
        symbol = request.args.get("symbol")
        if not symbol:
            return jsonify({"success": False, "message": "missing_symbol"})
        out = kis_quote.get_price_detail(exchange, symbol, mode=mode)
        return jsonify({"success": True, "data": out})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/account/buyable')
def api_account_buyable():
    try:
        mode = request.args.get("mode") or config_manager.get("common.mode", "mock")
        exchange = request.args.get("exchange", "NASD")
        symbol = request.args.get("symbol")
        price = request.args.get("price")
        if not symbol or not price:
            return jsonify({"success": False, "message": "missing_params"})
        out = kis_order.get_buyable_amount(exchange=exchange, symbol=symbol, price=float(price), mode=mode)
        return jsonify({"success": True, "data": out})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/trading-diary/period-profit')
def api_trading_diary_period_profit():
    """
    매매일지(기간손익)
    - 실전: v1_해외주식-032 기간손익 API 사용 (정확도 우선)
    - 모의: 미지원 -> v1_007 기반 참고용 집계(간단)
    """
    try:
        mode = request.args.get("mode") or config_manager.get("common.mode", "mock")
        from_date = (request.args.get("from") or "").replace("-", "")
        to_date = (request.args.get("to") or "").replace("-", "")
        exchange = request.args.get("exchange") or ""

        if not (from_date and to_date) or len(from_date) != 8 or len(to_date) != 8:
            return jsonify({"success": False, "message": "invalid_date"})

        if mode == "real":
            data = kis_order.get_period_profit(
                start_date=from_date,
                end_date=to_date,
                exchange=exchange,
                currency_div="01",
                mode=mode,
            )
            if not data:
                return jsonify({"success": False, "message": "no_data"})

            rows = data.get("output1") or []
            # 일별 집계(동일 날짜 여러 종목 합산)
            daily = {}
            total_trades = 0
            for r in rows:
                d = r.get("trad_day")
                if not d:
                    continue
                key = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
                daily.setdefault(key, {"date": key, "sell_qty": 0, "buy_amt": 0.0, "sell_amt": 0.0, "profit": 0.0})
                try:
                    daily[key]["sell_qty"] += int(float(r.get("slcl_qty") or 0))
                except Exception:
                    pass
                for k_src, k_dst in [
                    ("frcr_pchs_amt1", "buy_amt"),
                    ("frcr_sll_amt_smtl1", "sell_amt"),
                    ("ovrs_rlzt_pfls_amt", "profit"),
                ]:
                    try:
                        daily[key][k_dst] += float(str(r.get(k_src) or 0).replace(",", ""))
                    except Exception:
                        pass
                total_trades += 1

            daily_rows = sorted(daily.values(), key=lambda x: x["date"])
            for dr in daily_rows:
                buy = float(dr["buy_amt"] or 0)
                prof = float(dr["profit"] or 0)
                dr["rate"] = round((prof / buy) * 100, 4) if buy != 0 else 0.0

            out2 = data.get("output2") or {}
            total_profit = float(str(out2.get("ovrs_rlzt_pfls_tot_amt") or 0).replace(",", ""))
            total_buy = float(str(out2.get("stck_buy_amt_smtl") or 0).replace(",", ""))
            total_rate = round((total_profit / total_buy) * 100, 4) if total_buy != 0 else 0.0

            return jsonify({
                "success": True,
                "source": "KIS v1_해외주식-032(실전)",
                "note": "가이드 기준: HTS 해외 기간손익과 동일 로직(참고용)",
                "rows": daily_rows,
                "summary": {
                    "total_trades": total_trades,
                    "total_profit": total_profit,
                    "total_rate": total_rate,
                }
            })

        # mock fallback (간단 집계: 주문체결내역 기반)
        hist = kis_order.get_order_history(
            start_date=from_date,
            end_date=to_date,
            mode=mode,
        ) or {}
        orders = hist.get("output") or []
        orders = orders if isinstance(orders, list) else [orders]

        daily = {}
        total_trades = 0
        for o in orders:
            d = o.get("ord_dt")
            if not d:
                continue
            key = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
            daily.setdefault(key, {"date": key, "sell_qty": 0, "buy_amt": 0.0, "sell_amt": 0.0, "profit": 0.0})
            # 모의는 참고용: 체결금액/체결수량만 집계 (손익은 0 처리)
            try:
                qty = int(float(o.get("ft_ccld_qty") or 0))
            except Exception:
                qty = 0
            side = o.get("sll_buy_dvsn_cd") or ""
            try:
                amt = float(str(o.get("ft_ccld_amt3") or 0).replace(",", ""))
            except Exception:
                amt = 0.0

            if side == "02":  # 매수
                daily[key]["buy_amt"] += amt
            elif side == "01":  # 매도
                daily[key]["sell_amt"] += amt
                daily[key]["sell_qty"] += qty

            total_trades += 1

        daily_rows = sorted(daily.values(), key=lambda x: x["date"])
        for dr in daily_rows:
            dr["rate"] = 0.0

        total_profit = 0.0
        total_buy = sum([float(r["buy_amt"] or 0) for r in daily_rows])
        total_rate = 0.0

        return jsonify({
            "success": True,
            "source": "KIS v1_007(모의 fallback)",
            "note": "모의는 기간손익 API 미지원이라 참고용 집계(손익/수익률은 0)입니다.",
            "rows": daily_rows,
            "summary": {
                "total_trades": total_trades,
                "total_profit": total_profit,
                "total_rate": total_rate,
            }
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/logs')
def get_logs():
    """최신 로그 데이터 반환"""
    mode = request.args.get("mode") or config_manager.get("common.mode", "mock")

    # mock/real 로그 분리 디렉토리 우선 사용
    mode_dir = str(PROJECT_ROOT / "logs" / mode)
    list_of_files = glob.glob(f'{mode_dir}/*.log')

    # fallback: 기존 단일 로그
    if not list_of_files:
        log_dir = str(PROJECT_ROOT / "logs")
        list_of_files = glob.glob(f'{log_dir}/*.log')
    
    if not list_of_files:
        return jsonify({"logs": "로그 파일이 없습니다."})
        
    latest_file = max(list_of_files, key=os.path.getctime)
    
    try:
        with open(latest_file, 'r', encoding='utf-8') as f:
            # 마지막 100줄만 읽기
            lines = f.readlines()
            last_logs = "".join(lines[-100:])
            return jsonify({"logs": last_logs})
    except Exception as e:
        return jsonify({"logs": f"로그 읽기 실패: {e}"})

@app.route('/api/settings', methods=['POST'])
def update_settings():
    """설정 업데이트"""
    try:
        new_config = request.json
        # YAML 파일 저장(원자적 교체)
        config_manager._config = new_config
        config_manager.save_config()
        
        # 메모리 로드
        config_manager.load_config()
        return jsonify({"result": "success"})
    except Exception as e:
        return jsonify({"result": "fail", "message": str(e)})

@app.route('/api/auto-trading/config', methods=['GET'])
def api_auto_trading_get_config():
    mode = request.args.get("mode") or config_manager.get("common.mode", "mock")
    enabled = bool(config_manager.get(f"{mode}.auto_trading_enabled", False))
    return jsonify({"auto_trading_enabled": enabled, "mode": mode})

@app.route('/api/auto-trading/config', methods=['POST'])
def api_auto_trading_set_config():
    try:
        payload = request.json or {}
        mode = payload.get("mode") or config_manager.get("common.mode", "mock")
        enabled = bool(payload.get("auto_trading_enabled", False))

        config_manager._config.setdefault(mode, {})
        config_manager._config[mode]["auto_trading_enabled"] = enabled

        config_manager.save_config()
        config_manager.load_config()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/auto-trading/strategy', methods=['GET'])
def api_auto_trading_get_strategy():
    """자동매매 전략/분석서버 설정 조회 (보안상 계정/API 키는 제외)"""
    try:
        mode = request.args.get("mode") or config_manager.get("common.mode", "mock")
        enabled = bool(config_manager.get(f"{mode}.auto_trading_enabled", False))
        strategy = config_manager.get(f"{mode}.strategy", {}) or {}
        # autokiwoomstock 스타일: 투자금액 입력 없음. (기존 max_buy_amount/investment_amount는 무시)
        if isinstance(strategy, dict):
            strategy.pop("investment_amount", None)
            strategy.pop("max_buy_amount", None)
            # reserve_cash는 구버전(USD). UI는 reserve_cash_krw(원화)로 보여준다.
            if ("reserve_cash_krw" not in strategy) or (strategy.get("reserve_cash_krw") is None):
                usd_krw = float(config_manager.get("common.usd_krw_rate", 1350.0) or 1350.0)
                try:
                    legacy_usd = float(strategy.get("reserve_cash", 0) or 0.0)
                except Exception:
                    legacy_usd = 0.0
                if legacy_usd > 0 and usd_krw > 0:
                    strategy["reserve_cash_krw"] = legacy_usd * usd_krw
                else:
                    strategy["reserve_cash_krw"] = 0
            # reserve_cash는 더 이상 UI/저장에서 사용하지 않음
            strategy.pop("reserve_cash", None)
            if ("top_n" not in strategy) or (strategy.get("top_n") is None):
                strategy["top_n"] = 5
            if ("max_hold_days" not in strategy) and (strategy.get("max_hold_period") is not None):
                strategy["max_hold_days"] = strategy.get("max_hold_period")
            # 매수 주문 방식/가드 기본값
            if ("buy_order_method" not in strategy) or (strategy.get("buy_order_method") is None):
                strategy["buy_order_method"] = "limit_ask_ladder" if mode == "real" else "limit_slippage"
            if ("limit_buy_max_premium_pct" not in strategy) or (strategy.get("limit_buy_max_premium_pct") is None):
                strategy["limit_buy_max_premium_pct"] = 1.0
            if ("limit_buy_max_levels" not in strategy) or (strategy.get("limit_buy_max_levels") is None):
                strategy["limit_buy_max_levels"] = 5
            if ("limit_buy_step_wait_sec" not in strategy) or (strategy.get("limit_buy_step_wait_sec") is None):
                strategy["limit_buy_step_wait_sec"] = 1.0
        intraday = config_manager.get(f"{mode}.intraday_stop_loss", {}) or {}
        schedule = {
            "schedule_time": config_manager.get(f"{mode}.schedule_time", "22:30"),
        }
        common = {
            "analysis_mock_enabled": bool(config_manager.get("common.analysis_mock_enabled", False)),
            "analysis_host": config_manager.get("common.analysis_host", "localhost"),
            "analysis_port": int(config_manager.get("common.analysis_port", 5500) or 5500),
        }
        return jsonify({
            "success": True,
            "mode": mode,
            "auto_trading_enabled": enabled,
            "strategy": strategy,
            "common": common,
            "intraday_stop_loss": intraday,
            "schedule": schedule
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/auto-trading/strategy', methods=['POST'])
def api_auto_trading_set_strategy():
    """자동매매 전략/분석서버 설정 저장 (화이트리스트 저장, 비밀키/계정정보는 건드리지 않음)"""
    try:
        payload = request.json or {}
        mode = payload.get("mode") or config_manager.get("common.mode", "mock")
        if mode not in ("mock", "real"):
            return jsonify({"success": False, "message": "invalid_mode"})

        # 1) auto_trading_enabled
        enabled = bool(payload.get("auto_trading_enabled", False))
        config_manager._config.setdefault(mode, {})
        config_manager._config[mode]["auto_trading_enabled"] = enabled

        # 2) common (analysis host/port + mock toggle)
        common_in = payload.get("common") or {}
        config_manager._config.setdefault("common", {})
        if "analysis_mock_enabled" in common_in:
            config_manager._config["common"]["analysis_mock_enabled"] = bool(common_in.get("analysis_mock_enabled"))
        if "analysis_host" in common_in and common_in.get("analysis_host"):
            config_manager._config["common"]["analysis_host"] = str(common_in.get("analysis_host")).strip()
        if "analysis_port" in common_in and common_in.get("analysis_port"):
            config_manager._config["common"]["analysis_port"] = int(common_in.get("analysis_port"))
        # usd_krw_rate: 사용자 입력은 받지 않음(안전상). 자동 환율(KIS→FDR)만 사용.

        # 3) strategy (mode별)
        st_in = payload.get("strategy") or {}
        config_manager._config[mode].setdefault("strategy", {})
        if ("max_hold_days" not in st_in) and (st_in.get("max_hold_period") is not None):
            st_in["max_hold_days"] = st_in.get("max_hold_period")

        # autokiwoomstock 스타일: investment_amount/max_buy_amount 저장하지 않음
        st_in.pop("investment_amount", None)
        st_in.pop("max_buy_amount", None)

        for k in (
            "top_n",
            "reserve_cash_krw",
            "take_profit_pct",
            "stop_loss_pct",
            "max_hold_days",
            "slippage_pct",
            # 매수 방식/가드
            "buy_order_method",
            "limit_buy_max_premium_pct",
            "limit_buy_max_levels",
            "limit_buy_step_wait_sec",
        ):
            if k in st_in and st_in.get(k) is not None:
                config_manager._config[mode]["strategy"][k] = st_in.get(k)

        # 4) intraday_stop_loss (mode별, 자동매매와 별개)
        intraday_in = payload.get("intraday_stop_loss") or {}
        config_manager._config[mode].setdefault("intraday_stop_loss", {})
        if "enabled" in intraday_in:
            config_manager._config[mode]["intraday_stop_loss"]["enabled"] = bool(intraday_in.get("enabled"))
        # threshold_pct: 부호 그대로 저장(+면 익절 감시, -면 손절 감시)
        if "threshold_pct" in intraday_in and intraday_in.get("threshold_pct") is not None:
            config_manager._config[mode]["intraday_stop_loss"]["threshold_pct"] = float(intraday_in.get("threshold_pct"))
        # backward compat
        if "stop_loss_pct" in intraday_in and intraday_in.get("stop_loss_pct") is not None:
            config_manager._config[mode]["intraday_stop_loss"]["threshold_pct"] = -abs(float(intraday_in.get("stop_loss_pct")))

        # 5) schedule (mode별) - 자동매매 ON이면 항상 schedule_time 적용
        schedule_in = payload.get("schedule") or {}
        if "schedule_time" in schedule_in and schedule_in.get("schedule_time"):
            config_manager._config[mode]["schedule_time"] = str(schedule_in.get("schedule_time")).strip()

        # 저장
        config_manager.save_config()
        config_manager.load_config()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/server/select', methods=['POST'])
def api_server_select():
    """myKiwoom-main과 동일한 UX를 위해 server selection API 제공"""
    try:
        data = request.json or {}
        server_type = data.get("server_type")
        if server_type not in ("mock", "real"):
            return jsonify({"success": False, "message": "invalid server_type"})

        config_manager._config["common"]["mode"] = server_type
        config_manager.save_config()
        config_manager.load_config()
        return jsonify({"success": True, "message": "ok"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/settings/mode', methods=['POST'])
def change_mode():
    """실행 모드 전환 (mock <-> real)"""
    try:
        data = request.json
        new_mode = data.get('mode')
        
        if new_mode not in ['mock', 'real']:
            return jsonify({"result": "fail", "message": "유효하지 않은 모드입니다."})
            
        # config 메모리 및 파일 업데이트
        config_manager._config['common']['mode'] = new_mode
        
        # YAML 파일 저장
        config_manager.save_config()
            
        return jsonify({"result": "success", "mode": new_mode})
        
    except Exception as e:
        return jsonify({"result": "fail", "message": str(e)})

@app.route('/api/trade/start', methods=['POST'])
def start_trade():
    """수동으로 즉시 1회 실행"""
    if not trading_engine.is_running:
        # 비동기로 실행하지 않고 즉시 실행 (응답 대기) 또는 스레드로 실행
        # 여기서는 즉시 실행 시킴
        import threading
        t = threading.Thread(target=trading_engine.run_once)
        t.start()
        return jsonify({"result": "started", "message": "자동매매 로직이 시작되었습니다."})
    else:
        return jsonify({"result": "running", "message": "이미 실행 중입니다."})

def _build_trade_preview_view(analysis: dict | None, mode: str) -> dict:
    """
    autokiwoomstock UX처럼 "즉시실행 미리보기"에서 바로 이해할 수 있는 데이터 생성.
    - 매도 대상: 손절/익절/최대보유 + (가능하면) 분석 SELL
    - 매수 대상: 분석 BUY 상위 top_n (보유종목 제외), 예산 기반 예상수량
    """
    analysis = analysis or {}

    def _to_float(v, default=0.0) -> float:
        try:
            if v is None:
                return float(default)
            s = str(v).replace(",", "").strip()
            if s == "":
                return float(default)
            return float(s)
        except Exception:
            return float(default)

    def _to_int(v, default=0) -> int:
        try:
            if v is None:
                return int(default)
            return int(float(str(v).replace(",", "").strip() or default))
        except Exception:
            return int(default)

    # 전략 파라미터
    top_n = _to_int(config_manager.get(f"{mode}.strategy.top_n", 5), 5)
    reserve_cash_krw = _to_float(config_manager.get(f"{mode}.strategy.reserve_cash_krw", 0), 0.0)
    stop_loss_pct = _to_float(config_manager.get(f"{mode}.strategy.stop_loss_pct", -3.0), -3.0)
    take_profit_pct = _to_float(config_manager.get(f"{mode}.strategy.take_profit_pct", 5.0), 5.0)
    max_hold_days = _to_int(config_manager.get(f"{mode}.strategy.max_hold_days", 15), 15)

    # 주문가능(USD) 계산: 035(실전) -> 008(output2 USD) -> 008(output3 frcr_use) -> mock 추정(총자산/환율)
    present = kis_order.get_present_balance(
        natn_cd="000", tr_mket_cd="00", inqr_dvsn_cd="00", wcrc_frcr_dvsn_cd="02", mode=mode
    ) or {}
    out3 = present.get("output3") or {}
    # 자동 환율(원/달러): v1_008 output3의 frst_bltn_exrt(최초고시환율)을 우선 사용하고 실패 시 설정값 fallback
    # 자동 환율(원/달러): 사용자 입력/설정값은 사용하지 않고, KIS → FinanceDataReader 순으로 자동 조회
    fx = get_usd_krw_rate(mode=mode, kis_present=present)
    usd_krw_rate = fx.rate or 0.0
    usd_krw_rate_source = fx.source

    reserve_cash_usd = (reserve_cash_krw / usd_krw_rate) if usd_krw_rate > 0 else 0.0

    out2_raw = present.get("output2") or []
    out2_list = out2_raw if isinstance(out2_raw, list) else [out2_raw]

    orderable_usd = 0.0
    orderable_source = None
    if mode == "real":
        try:
            fm = kis_order.get_foreign_margin(mode=mode) or {}
            rows = fm.get("output") or []
            rows = rows if isinstance(rows, list) else [rows]
            for r in rows:
                if isinstance(r, dict) and (str(r.get("crcy_cd") or "").strip().upper() == "USD"):
                    orderable_usd = _to_float(r.get("itgr_ord_psbl_amt"), 0.0)
                    orderable_source = "035_itgr"
                    break
        except Exception:
            pass

    if orderable_usd <= 0:
        usd_row = None
        for r in out2_list:
            if isinstance(r, dict) and (str(r.get("crcy_cd") or "").strip().upper() == "USD"):
                usd_row = r
                break
        if usd_row:
            orderable_usd = _to_float(usd_row.get("frcr_drwg_psbl_amt_1"), 0.0)
            if orderable_usd <= 0:
                orderable_usd = _to_float(usd_row.get("frcr_dncl_amt_2"), 0.0)
            orderable_source = "008_out2_usd"

    if orderable_usd <= 0:
        orderable_usd = _to_float(out3.get("frcr_use_psbl_amt"), 0.0)
        orderable_source = "008_frcr_use"

    if mode == "mock" and orderable_usd <= 0:
        tot_asst_krw = _to_float(out3.get("tot_asst_amt"), 0.0)
        if usd_krw_rate > 0 and tot_asst_krw > 0:
            orderable_usd = tot_asst_krw / usd_krw_rate
            orderable_source = "mock_est_tot_asset"

    total_budget_usd = max(0.0, orderable_usd - reserve_cash_usd)

    # 분석 데이터 정규화 (buy)
    raw_buy = analysis.get("buy") or []
    raw_sell = analysis.get("sell") or []
    buy_items = []
    for item in (raw_buy if isinstance(raw_buy, list) else [raw_buy]):
        if isinstance(item, dict):
            code = (item.get("code") or item.get("ticker") or item.get("symbol") or "").strip().upper()
            exc = (item.get("exchange") or "NAS").strip().upper()
            if code:
                buy_items.append({
                    "code": code,
                    "exchange": exc,
                    "name": item.get("name"),
                    "price": item.get("price"),
                    "score": item.get("score"),
                    "prob": item.get("prob"),
                })
        else:
            code = (str(item) or "").strip().upper()
            if code:
                buy_items.append({"code": code, "exchange": "NAS"})

    sell_codes = set()
    for item in (raw_sell if isinstance(raw_sell, list) else [raw_sell]):
        if isinstance(item, dict):
            c = (item.get("code") or item.get("ticker") or item.get("symbol") or "").strip().upper()
        else:
            c = (str(item) or "").strip().upper()
        if c:
            sell_codes.add(c)

    # 보유종목 가져오기
    bal = kis_order.get_balance(mode=mode) or {}
    holdings = bal.get("output1") or []
    holdings = holdings if isinstance(holdings, list) else [holdings]
    held_map: dict[str, dict] = {}
    for h in holdings:
        if not isinstance(h, dict):
            continue
        sym = (h.get("ovrs_pdno") or "").strip().upper()
        if sym:
            held_map[sym] = h

    # 보유기간(일)
    holding_days_map: dict[str, int | None] = {}
    try:
        from src.engine.position_store import PositionStore
        store = PositionStore(mode=mode)
        for sym in held_map.keys():
            od = store.get_open_date(sym)
            if od and len(od) == 8:
                try:
                    holding_days_map[sym] = int((datetime.now().date() - datetime.strptime(od, "%Y%m%d").date()).days)
                except Exception:
                    holding_days_map[sym] = None
            else:
                holding_days_map[sym] = None
    except Exception:
        pass

    # 매도 후보 생성 (엔진 로직과 최대한 유사하게)
    sell_candidates = []
    for sym, h in held_map.items():
        qty = _to_float(h.get("ovrs_cblc_qty"), 0.0)
        if qty <= 0:
            continue
        pr = _to_float(h.get("evlu_pfls_rt"), 0.0)
        reasons = []
        if sym in sell_codes:
            reasons.append("분석 SELL")
        if pr <= stop_loss_pct:
            reasons.append(f"손절({pr:.2f}% ≤ {stop_loss_pct:.2f}%)")
        if pr >= take_profit_pct:
            reasons.append(f"익절({pr:.2f}% ≥ {take_profit_pct:.2f}%)")
        hd = holding_days_map.get(sym)
        if (hd is not None) and (max_hold_days > 0) and (hd >= max_hold_days):
            reasons.append(f"최대보유({hd}d ≥ {max_hold_days}d)")
        if not reasons:
            continue
        cur = _to_float(h.get("now_pric2"), 0.0)
        avg = _to_float(h.get("pchs_avg_pric"), 0.0)
        sell_candidates.append({
            "code": sym,
            "name": h.get("ovrs_item_name") or sym,
            "qty": qty,
            "avg_price": avg,
            "current_price": cur,
            "profit_rate": pr,
            "holding_days": hd,
            "est_amount": (qty * cur) if cur > 0 else None,
            "reasons": reasons,
        })

    # 매수 후보 생성 (상위 top_n, 보유종목 제외)
    candidates = [x for x in buy_items if x.get("code") and x.get("code") not in held_map][:max(0, top_n)]
    per_stock_budget = (total_budget_usd / len(candidates)) if candidates else 0.0

    buy_candidates = []
    for idx, c in enumerate(candidates, start=1):
        sym = c.get("code")
        exc = c.get("exchange") or "NAS"
        name = c.get("name") or sym
        price = _to_float(c.get("price"), 0.0)
        if price <= 0:
            try:
                p = kis_quote.get_current_price(exc, sym, mode=mode) or {}
                price = _to_float(p.get("last"), 0.0)
            except Exception:
                price = 0.0
        qty = int(per_stock_budget // price) if price > 0 else 0
        buy_candidates.append({
            "rank": idx,
            "code": sym,
            "name": name,
            "exchange": exc,
            "current_price": price if price > 0 else None,
            "score": c.get("score"),
            "prob": c.get("prob"),
            "per_stock_budget": per_stock_budget,
            "est_qty": qty,
            "est_amount": (qty * price) if (qty > 0 and price > 0) else None,
            "reason": "분석 상위 추천",
        })

    meta = analysis.get("meta") or {}
    analysis_date = meta.get("analysis_date")
    total_stocks = meta.get("total_stocks")

    return {
        "mode": mode,
        "analysis": {
            "analysis_date": analysis_date,
            "total_stocks": total_stocks,
        },
        "strategy": {
            "top_n": top_n,
            "reserve_cash_krw": reserve_cash_krw,
            "usd_krw_rate": usd_krw_rate,
            "usd_krw_rate_source": usd_krw_rate_source,
            "reserve_cash_usd": reserve_cash_usd,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "max_hold_days": max_hold_days,
        },
        "budget": {
            "orderable_usd": orderable_usd,
            "orderable_source": orderable_source,
            "usd_krw_rate": usd_krw_rate,
            "usd_krw_rate_source": usd_krw_rate_source,
            "total_budget_usd": total_budget_usd,
            "per_stock_budget_usd": per_stock_budget,
        },
        "sell_candidates": sell_candidates,
        "buy_candidates": buy_candidates,
    }

@app.route('/api/trade/preview', methods=['POST'])
def api_trade_preview():
    """
    myKiwoom-main UX:
    - 즉시 실행 전에 분석서버 응답을 먼저 보여주기 위한 미리보기
    """
    try:
        mode = config_manager.get("common.mode", "mock")
        preview_id = str(uuid4())
        now = datetime.now()
        _TRADE_PREVIEWS[preview_id] = {
            "mode": mode,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=_TRADE_PREVIEW_TTL_SEC)).isoformat(),
            "status": "running",  # running|ready|error
            "analysis": None,
            "error": None,
        }

        # 실시간 분석 실행은 오래 걸릴 수 있으므로 백그라운드에서 수행
        def _run_analysis_for_preview(pid: str):
            try:
                item = _TRADE_PREVIEWS.get(pid)
                if not item:
                    return
                # 만료되었으면 중단
                try:
                    exp = item.get("expires_at")
                    if exp and datetime.fromisoformat(exp) < datetime.now():
                        _TRADE_PREVIEWS.pop(pid, None)
                        return
                except Exception:
                    pass

                analysis = trading_engine.get_analysis_data()  # 실시간 분석(폴링)

                # autokiwoomstock UX처럼: 미리보기에서 바로 이해할 수 있는 "뷰 데이터"를 생성
                try:
                    view = _build_trade_preview_view(analysis=analysis, mode=item.get("mode") or config_manager.get("common.mode", "mock"))
                except Exception as ve:
                    view = {"error": f"preview_view_build_failed: {ve}"}

                item["analysis"] = analysis
                item["view"] = view
                item["status"] = "ready"
            except Exception as e:
                item = _TRADE_PREVIEWS.get(pid)
                if item:
                    item["status"] = "error"
                    item["error"] = str(e)

        t = threading.Thread(target=_run_analysis_for_preview, args=(preview_id,), daemon=True)
        t.start()

        return jsonify({
            "success": True,
            "preview_id": preview_id,
            "mode": mode,
            "status": _TRADE_PREVIEWS[preview_id]["status"],
            "created_at": _TRADE_PREVIEWS[preview_id]["created_at"],
            "expires_at": _TRADE_PREVIEWS[preview_id]["expires_at"],
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/trade/preview/<preview_id>', methods=['GET'])
def api_trade_preview_status(preview_id):
    """미리보기 진행 상태/결과 조회 (폴링용)"""
    try:
        item = _TRADE_PREVIEWS.get(preview_id)
        if not item:
            return jsonify({"success": False, "message": "preview_not_found"})

        # 만료 체크
        expires_at = item.get("expires_at")
        try:
            if expires_at and datetime.fromisoformat(expires_at) < datetime.now():
                _TRADE_PREVIEWS.pop(preview_id, None)
                return jsonify({"success": False, "message": "preview_expired"})
        except Exception:
            pass

        return jsonify({
            "success": True,
            "preview_id": preview_id,
            "mode": item.get("mode"),
            "status": item.get("status", "running"),
            "analysis": item.get("analysis"),
            "view": item.get("view"),
            "error": item.get("error"),
            "created_at": item.get("created_at"),
            "expires_at": item.get("expires_at"),
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/trade/execute', methods=['POST'])
def api_trade_execute():
    """
    미리보기 후 실행:
    - preview_id에 저장된 분석결과로 1회 실행
    """
    try:
        payload = request.json or {}
        preview_id = payload.get("preview_id")
        if not preview_id:
            return jsonify({"success": False, "message": "missing_preview_id"})

        item = _TRADE_PREVIEWS.get(preview_id)
        if not item:
            return jsonify({"success": False, "message": "preview_not_found"})

        # 만료 체크
        expires_at = item.get("expires_at")
        try:
            if expires_at and datetime.fromisoformat(expires_at) < datetime.now():
                _TRADE_PREVIEWS.pop(preview_id, None)
                return jsonify({"success": False, "message": "preview_expired"})
        except Exception:
            pass

        mode = config_manager.get("common.mode", "mock")
        if item.get("mode") != mode:
            return jsonify({"success": False, "message": "mode_changed"})

        if trading_engine.is_running:
            return jsonify({"success": False, "message": "engine_running"})

        if item.get("status") != "ready" or item.get("analysis") is None:
            return jsonify({"success": False, "message": "preview_not_ready"})

        analysis = item.get("analysis") or {"buy": [], "sell": []}

        import threading
        t = threading.Thread(target=trading_engine.run_once_with_analysis, args=(analysis, mode))
        t.start()

        return jsonify({"success": True, "message": "실행을 시작했습니다."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

if __name__ == '__main__':
    # 테스트용 단독 실행 (기본 포트 7500)
    start_scheduler()
    app.run(host='0.0.0.0', port=7500, debug=False)
