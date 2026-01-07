from flask import Flask, render_template, jsonify, request, redirect
from apscheduler.schedulers.background import BackgroundScheduler
import os
import glob
import yaml
from pathlib import Path
from datetime import datetime, timedelta
from uuid import uuid4
from src.engine.engine import trading_engine
from src.config.config_manager import config_manager
from src.api.order import kis_order
from src.api.quote import kis_quote
from src.utils.logger import logger

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"

app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(STATIC_DIR),
)

# 스케줄러 설정
scheduler = BackgroundScheduler()
# 1분마다 자동매매 엔진 실행
scheduler.add_job(func=trading_engine.run, trigger="interval", seconds=60, id="auto_trading")
# 1분마다 장중 손절 감시
scheduler.add_job(func=trading_engine.stop_loss_watch, trigger="interval", seconds=60, id="stop_loss_watch")
scheduler.start()

# 즉시 실행 미리보기(서버 메모리 임시 저장)
_TRADE_PREVIEWS: dict[str, dict] = {}
_TRADE_PREVIEW_TTL_SEC = 300

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
    # 간단한 상태 정보
    job = scheduler.get_job("auto_trading")
    job_watch = scheduler.get_job("stop_loss_watch")
    next_run = None
    next_run_watch = None
    try:
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()
        if job_watch and job_watch.next_run_time:
            next_run_watch = job_watch.next_run_time.isoformat()
    except Exception:
        next_run = None
        next_run_watch = None

    status = {
        "market_open": trading_engine.is_market_open(),
        "is_running": trading_engine.is_running,
        "mode": config_manager.get("common.mode", "mock"),
        "engine_last_run_at": trading_engine.last_run_at.isoformat() if trading_engine.last_run_at else None,
        "engine_last_error": trading_engine.last_error,
        "engine_next_run_at": next_run,
        "stop_watch_last_run_at": trading_engine.last_stop_watch_at.isoformat() if trading_engine.last_stop_watch_at else None,
        "stop_watch_last_error": trading_engine.last_stop_watch_error,
        "stop_watch_next_run_at": next_run_watch,
    }
    
    # 잔고 조회 (실시간성을 위해 API 호출)
    mode = config_manager.get("common.mode", "mock")
    balance_info = kis_order.get_balance(mode=mode)
    balance = {}
    
    if balance_info:
        output2 = balance_info.get('output2', {})
        balance = {
            "total_asset": output2.get('tot_evlu_pfls_amt', '0'),
            "total_profit": output2.get('ovrs_tot_pfls', '0'),
            "profit_rate": output2.get('tot_pftrt', '0'),
            "stocks": balance_info.get('output1', [])
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
        # YAML 파일 저장
        config_path = config_manager.CONFIG_FILE
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(new_config, f, allow_unicode=True, default_flow_style=False)
        
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

        config_path = config_manager.CONFIG_FILE
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_manager._config, f, allow_unicode=True, default_flow_style=False)
        config_manager.load_config()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/auto-trading/strategy', methods=['GET'])
def api_auto_trading_get_strategy():
    """자동매매 전략/분석서버 설정 조회 (보안상 계정/API 키는 제외)"""
    try:
        mode = request.args.get("mode") or config_manager.get("common.mode", "mock")
        strategy = config_manager.get(f"{mode}.strategy", {}) or {}
        intraday = config_manager.get(f"{mode}.intraday_stop_loss", {}) or {}
        schedule = {
            "schedule_time": config_manager.get(f"{mode}.schedule_time", "22:30"),
        }
        common = {
            "analysis_mock_enabled": bool(config_manager.get("common.analysis_mock_enabled", False)),
            "analysis_host": config_manager.get("common.analysis_host", "localhost"),
            "analysis_port": int(config_manager.get("common.analysis_port", 5000) or 5000),
        }
        return jsonify({"success": True, "mode": mode, "strategy": strategy, "common": common, "intraday_stop_loss": intraday, "schedule": schedule})
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

        # 3) strategy (mode별)
        st_in = payload.get("strategy") or {}
        config_manager._config[mode].setdefault("strategy", {})
        for k in ("max_buy_amount","reserve_cash","take_profit_pct","stop_loss_pct","max_hold_days","slippage_pct"):
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
        config_path = config_manager.CONFIG_FILE
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_manager._config, f, allow_unicode=True, default_flow_style=False)
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
        config_path = config_manager.CONFIG_FILE
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_manager._config, f, allow_unicode=True, default_flow_style=False)
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
        config_path = config_manager.CONFIG_FILE
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_manager._config, f, allow_unicode=True, default_flow_style=False)
            
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
        t = threading.Thread(target=trading_engine.run)
        t.start()
        return jsonify({"result": "started", "message": "자동매매 로직이 시작되었습니다."})
    else:
        return jsonify({"result": "running", "message": "이미 실행 중입니다."})

@app.route('/api/trade/preview', methods=['POST'])
def api_trade_preview():
    """
    myKiwoom-main UX:
    - 즉시 실행 전에 분석서버 응답을 먼저 보여주기 위한 미리보기
    """
    try:
        mode = config_manager.get("common.mode", "mock")
        analysis = trading_engine.get_analysis_data()
        preview_id = str(uuid4())
        now = datetime.now()
        _TRADE_PREVIEWS[preview_id] = {
            "mode": mode,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=_TRADE_PREVIEW_TTL_SEC)).isoformat(),
            "analysis": analysis,
        }
        return jsonify({
            "success": True,
            "preview_id": preview_id,
            "mode": mode,
            "analysis": analysis,
            "created_at": _TRADE_PREVIEWS[preview_id]["created_at"],
            "expires_at": _TRADE_PREVIEWS[preview_id]["expires_at"],
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

        analysis = item.get("analysis") or {"buy": [], "sell": []}

        import threading
        t = threading.Thread(target=trading_engine.run_once_with_analysis, args=(analysis, mode))
        t.start()

        return jsonify({"success": True, "message": "실행을 시작했습니다."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

if __name__ == '__main__':
    # 테스트용 단독 실행 (기본 포트 7500)
    app.run(host='0.0.0.0', port=7500, debug=False)
