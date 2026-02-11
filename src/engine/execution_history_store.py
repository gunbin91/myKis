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
        self._index_path = self._data_dir / f"auto_trading_history_index_{self.mode}.json"
        self._detail_dir = self._data_dir / f"auto_trading_history_{self.mode}"
        self._detail_dir.mkdir(parents=True, exist_ok=True)

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

    def _read_index(self) -> list[dict[str, Any]]:
        try:
            if not self._index_path.exists():
                return []
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f) or []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _write_index(self, rows: list[dict[str, Any]]) -> None:
        tmp = self._index_path.with_suffix(self._index_path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
            tmp.replace(self._index_path)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def _detail_path(self, run_id: str) -> Path:
        rid = (run_id or "").strip() or "unknown"
        return self._detail_dir / f"{rid}.json"

    def _write_detail(self, item: dict[str, Any]) -> None:
        try:
            rid = str(item.get("run_id") or "").strip()
            if not rid:
                return
            path = self._detail_path(rid)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(item, f, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except Exception:
            pass

    def _build_slim(self, item: dict[str, Any]) -> dict[str, Any]:
        def _cnt(v) -> int:
            try:
                if isinstance(v, list):
                    return len(v)
                return int(v or 0)
            except Exception:
                return 0

        return {
            "run_id": item.get("run_id"),
            "mode": item.get("mode"),
            "run_type": item.get("run_type"),
            "started_at": item.get("started_at"),
            "finished_at": item.get("finished_at"),
            "status": item.get("status"),
            "message": item.get("message"),
            "buy_attempts_count": _cnt(item.get("buy_attempts")),
            "sell_attempts_count": _cnt(item.get("sell_attempts")),
            "skips_count": _cnt(item.get("skips")),
            "errors_count": _cnt(item.get("errors")),
        }

    def append(self, item: dict[str, Any]) -> None:
        if not isinstance(item, dict):
            return
        # 상세 파일 저장 (run_id 기준)
        self._write_detail(item)

        # 목록 인덱스 저장 (요약)
        try:
            index_rows = self._read_index()
            index_rows.insert(0, self._build_slim(item))
            if len(index_rows) > int(self.max_entries):
                index_rows = index_rows[: int(self.max_entries)]
            self._write_index(index_rows)
        except Exception:
            pass

        # 하위 호환: 기존 통합 파일도 유지
        rows = self._read_all()
        rows.insert(0, item)  # 최신이 위
        if len(rows) > int(self.max_entries):
            rows = rows[: int(self.max_entries)]
        self._write_all(rows)

    def list(self, days: int = 7) -> list[dict[str, Any]]:
        rows = self._read_index()
        if not rows:
            rows = self._read_all()
            if rows:
                # 인덱스가 없으면 1회 생성 (키움 방식: 목록 요약만 유지)
                try:
                    slim_rows = [self._build_slim(r) for r in rows if isinstance(r, dict)]
                    self._write_index(slim_rows[: int(self.max_entries)])
                    rows = slim_rows
                except Exception:
                    pass
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
        try:
            path = self._detail_path(rid)
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass

        for r in self._read_all():
            try:
                if str(r.get("run_id") or "") == rid:
                    # 상세 파일이 없으면 생성해서 이후 조회를 빠르게 함
                    self._write_detail(r)
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

