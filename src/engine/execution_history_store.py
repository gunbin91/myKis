import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass
class ExecutionHistoryStore:
    """
    자동매매 실행 이력(모드별) 저장소.
    - data/auto_trading_history_{mode}.json

    저장 단위: run_id 1개 = 자동매매 1회 실행(스케줄/수동/미리보기 실행 포함)
    """

    mode: str
    max_entries: int = 2000  # 안전장치(파일 무한 증가 방지)

    def __post_init__(self):
        project_root = Path(__file__).resolve().parents[2]
        self._data_dir = project_root / "data"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / f"auto_trading_history_{self.mode}.json"

    def _read_all(self) -> list[dict[str, Any]]:
        try:
            if not self._path.exists():
                return []
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f) or []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _write_all(self, rows: list[dict[str, Any]]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
            tmp.replace(self._path)  # Windows 포함 원자적 교체
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def append(self, item: dict[str, Any]) -> None:
        if not isinstance(item, dict):
            return
        rows = self._read_all()
        rows.insert(0, item)  # 최신이 위
        if len(rows) > int(self.max_entries):
            rows = rows[: int(self.max_entries)]
        self._write_all(rows)

    def list(self, days: int = 7) -> list[dict[str, Any]]:
        rows = self._read_all()
        if not rows:
            return []
        try:
            cutoff = datetime.now() - timedelta(days=int(days))
        except Exception:
            cutoff = datetime.now() - timedelta(days=7)

        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                ts = r.get("started_at") or r.get("finished_at")
                if not ts:
                    out.append(r)
                    continue
                dt = datetime.fromisoformat(str(ts))
                if dt >= cutoff:
                    out.append(r)
            except Exception:
                out.append(r)
        return out

    def get(self, run_id: str) -> dict[str, Any] | None:
        rid = (run_id or "").strip()
        if not rid:
            return None
        for r in self._read_all():
            try:
                if str(r.get("run_id") or "") == rid:
                    return r
            except Exception:
                continue
        return None

    def get_last_buy_date(self, symbol: str, days: int | None = None) -> str | None:
        """
        자동매매 이력에서 종목별 '가장 최근 매수 성공일(YYYYMMDD)'을 반환.
        - rows는 최신이 위라서 첫 매칭을 반환한다.
        """
        sym = (symbol or "").strip().upper()
        if not sym:
            return None
        rows = self._read_all()
        if not rows:
            return None
        if days is not None:
            try:
                cutoff = datetime.now() - timedelta(days=int(days))
            except Exception:
                cutoff = None
        else:
            cutoff = None

        def _to_yyyymmdd(ts: str | None) -> str | None:
            if not ts:
                return None
            try:
                dt = datetime.fromisoformat(str(ts))
                return dt.strftime("%Y%m%d")
            except Exception:
                return None

        for r in rows:
            try:
                ts = r.get("started_at") or r.get("finished_at")
                if cutoff and ts:
                    try:
                        dt = datetime.fromisoformat(str(ts))
                        if dt < cutoff:
                            continue
                    except Exception:
                        pass
                for att in (r.get("buy_attempts") or []):
                    if not isinstance(att, dict):
                        continue
                    if not att.get("ok"):
                        continue
                    s = (att.get("symbol") or "").strip().upper()
                    if s == sym:
                        return _to_yyyymmdd(ts)
            except Exception:
                continue
        return None

