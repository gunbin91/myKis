import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class SchedulerStateStore:
    """
    멀티프로세스 스케줄러(모드별) 상태/하트비트 저장소.
    - data/scheduler_state_{mode}.json
    - 웹(UI) 프로세스가 자식 프로세스 상태를 확인할 수 있도록 파일로 기록한다.
    """

    mode: str

    def __post_init__(self):
        project_root = Path(__file__).resolve().parents[2]
        self._data_dir = project_root / "data"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / f"scheduler_state_{self.mode}.json"

    def read(self) -> dict[str, Any]:
        try:
            if not self._path.exists():
                return {}
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def write(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(self._path)  # Windows 포함 원자적 교체
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def heartbeat(
        self,
        *,
        pid: int,
        is_running: bool,
        is_executing: bool,
        last_error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        payload: dict[str, Any] = {
            "mode": self.mode,
            "pid": int(pid),
            "last_check_at": now,
            "is_running": bool(is_running),
            "is_executing": bool(is_executing),
            "last_error": (str(last_error) if last_error else None),
        }
        if extra and isinstance(extra, dict):
            payload.update(extra)
        self.write(payload)

