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
        self.data = {"positions": {}}
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
                self.data = loaded
        except Exception:
            # 손상 파일이면 초기화
            self.data = {"positions": {}}
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
                self._save()
            return

        if symbol not in positions:
            positions[symbol] = {
                "open_date": self._today_yyyymmdd(),
                "qty": int(qty),
                "exchange": exchange,
            }
            self._save()
            return

        # 수량 증가(추가매수) 시 보유 시작일을 오늘로 갱신(보수적)
        prev_qty = int(positions[symbol].get("qty", 0) or 0)
        if int(qty) > prev_qty:
            positions[symbol]["open_date"] = self._today_yyyymmdd()

        positions[symbol]["qty"] = int(qty)
        if exchange:
            positions[symbol]["exchange"] = exchange
        self._save()

    def get_open_date(self, symbol: str) -> str | None:
        symbol = (symbol or "").strip().upper()
        return (self.data.get("positions", {}).get(symbol) or {}).get("open_date")

    def get_exchange(self, symbol: str) -> str | None:
        symbol = (symbol or "").strip().upper()
        return (self.data.get("positions", {}).get(symbol) or {}).get("exchange")

    def all_symbols(self):
        return list((self.data.get("positions", {}) or {}).keys())


