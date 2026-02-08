import json
import os
from datetime import datetime


class PositionStore:
    """
    보유기간(max_hold_days) 적용을 위한 로컬 상태 저장소.
    - KIS 잔고 API(v1_006)에 매수일자가 없어서, '최초 감지일'을 보유 시작일로 기록합니다.
    - 추후 주문체결내역(v1_007) 기반으로 정확화 가능.
    """

    def __init__(self, mode: str):
        self.mode = (mode or "mock").strip().lower()
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.data_dir = os.path.join(project_root, "data")
        self.path = os.path.join(self.data_dir, f"positions_{self.mode}.json")
        # 구조:
        # {
        #   "meta": {"api_sync_day": "YYYYMMDD"},
        #   "positions": { "TSLA": {"open_date": "YYYYMMDD", "open_date_source": "detect|api", "qty": 1, "exchange": "NASD"} }
        # }
        self.data = {"meta": {}, "positions": {}}
        self._load()

    def _load(self):
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.path):
            self._save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f) or {}
            if isinstance(loaded, dict) and "positions" in loaded:
                # meta는 옵션 (하위호환)
                meta = loaded.get("meta") or {}
                meta.setdefault("missing_counts", {})
                self.data = {"meta": meta, "positions": loaded.get("positions") or {}}
        except Exception:
            # 손상 파일이면 초기화
            self.data = {"meta": {}, "positions": {}}
            self._save()

    def _save(self):
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _today_yyyymmdd() -> str:
        return datetime.now().strftime("%Y%m%d")

    def upsert(self, symbol: str, qty: int, exchange: str | None = None):
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return

        positions = self.data.setdefault("positions", {})

        if qty <= 0:
            if symbol in positions:
                del positions[symbol]
            self.clear_missing(symbol)
            self._save()
            return

        if symbol not in positions:
            positions[symbol] = {
                "open_date": self._today_yyyymmdd(),
                "open_date_source": "detect",
                "qty": int(qty),
                "exchange": exchange,
            }
            self.clear_missing(symbol)
            self._save()
            return

        # 수량 증가(추가매수) 시 보유 시작일 갱신:
        # - 기존 방식: 오늘로 리셋(보수적)
        # - 단, API로 확정된 open_date(api)는 유지한다(최초 매수일 기반 보유기간 표시/강제매도 목적)
        prev_qty = int(positions[symbol].get("qty", 0) or 0)
        if int(qty) > prev_qty:
            if (positions[symbol].get("open_date_source") or "detect") != "api":
                positions[symbol]["open_date"] = self._today_yyyymmdd()
                positions[symbol]["open_date_source"] = "detect"

        positions[symbol]["qty"] = int(qty)
        if exchange:
            positions[symbol]["exchange"] = exchange
        self.clear_missing(symbol)
        self._save()

    def get_open_date(self, symbol: str) -> str | None:
        symbol = (symbol or "").strip().upper()
        return (self.data.get("positions", {}).get(symbol) or {}).get("open_date")

    def get_open_date_source(self, symbol: str) -> str | None:
        symbol = (symbol or "").strip().upper()
        return (self.data.get("positions", {}).get(symbol) or {}).get("open_date_source")

    def set_open_date(self, symbol: str, open_date: str, source: str = "api") -> None:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return
        if not open_date or len(str(open_date)) != 8:
            return
        positions = self.data.setdefault("positions", {})
        pos = positions.setdefault(symbol, {})
        cur_source = str(pos.get("open_date_source") or "detect").strip().lower()
        new_source = str(source or "api").strip().lower()

        # api가 제공하는 open_date는 신뢰도가 높지만,
        # 이미 더 최신 날짜가 기록된 경우(전량 매도 후 재매수 등)는 과거로 되돌리지 않는다.
        if new_source == "api" and cur_source != "api":
            new_date = str(open_date)
            cur_date = str(pos.get("open_date") or "")
            if (not cur_date) or (len(cur_date) != 8) or (new_date >= cur_date):
                pos["open_date"] = new_date
                pos["open_date_source"] = "api"
                self._save()
            return

        # "가장 최근 매수일" 기준: 더 최신 날짜일 때만 갱신(과거로 되돌아가는 것 방지)
        new_date = str(open_date)
        cur_date = str(pos.get("open_date") or "")
        if (not cur_date) or (len(cur_date) != 8) or (new_date >= cur_date):
            pos["open_date"] = new_date
            pos["open_date_source"] = new_source
        self._save()

    def _missing_counts(self) -> dict:
        meta = self.data.setdefault("meta", {})
        mc = meta.setdefault("missing_counts", {})
        if not isinstance(mc, dict):
            meta["missing_counts"] = {}
            mc = meta["missing_counts"]
        return mc

    def mark_missing(self, symbol: str) -> int:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return 0
        mc = self._missing_counts()
        mc[symbol] = int(mc.get(symbol, 0) or 0) + 1
        self._save()
        return int(mc[symbol] or 0)

    def clear_missing(self, symbol: str) -> None:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return
        mc = self._missing_counts()
        if symbol in mc:
            del mc[symbol]
            self._save()

    def get_missing_count(self, symbol: str) -> int:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return 0
        mc = self._missing_counts()
        return int(mc.get(symbol, 0) or 0)

    def get_api_sync_day(self) -> str | None:
        return (self.data.get("meta") or {}).get("api_sync_day")

    def set_api_sync_day(self, day: str) -> None:
        self.data.setdefault("meta", {})
        self.data["meta"]["api_sync_day"] = day
        self._save()

    def get_api_retry_at(self) -> str | None:
        return (self.data.get("meta") or {}).get("api_retry_at")

    def set_api_retry_at(self, dt_iso: str) -> None:
        self.data.setdefault("meta", {})
        self.data["meta"]["api_retry_at"] = dt_iso
        self._save()

    def clear_api_retry(self) -> None:
        meta = self.data.setdefault("meta", {})
        if "api_retry_at" in meta:
            del meta["api_retry_at"]
        if "api_last_error" in meta:
            del meta["api_last_error"]
        self._save()

    def set_api_last_error(self, msg: str | None) -> None:
        self.data.setdefault("meta", {})
        if msg:
            self.data["meta"]["api_last_error"] = msg
        else:
            self.data["meta"].pop("api_last_error", None)
        self._save()

    def get_exchange(self, symbol: str) -> str | None:
        symbol = (symbol or "").strip().upper()
        return (self.data.get("positions", {}).get(symbol) or {}).get("exchange")

    def all_symbols(self):
        return list((self.data.get("positions", {}) or {}).keys())


