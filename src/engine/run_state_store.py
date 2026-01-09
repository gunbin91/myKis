import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class RunStateStore:
    """
    자동매매 '하루 1회 실행'의 재시작 내성 확보용 상태 저장소.
    - myKiwoom-main의 is_today_executed()처럼, 프로세스 재시작 후에도 중복 실행을 막는다.
    - data/run_state_{mode}.json 에 저장
    """

    mode: str

    def __post_init__(self):
        project_root = Path(__file__).resolve().parents[2]
        self._data_dir = project_root / "data"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / f"run_state_{self.mode}.json"

    def get_last_scheduled_run_day(self) -> Optional[str]:
        """YYYYMMDD 문자열 반환 (없으면 None)"""
        try:
            if not self._path.exists():
                return None
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            day = data.get("last_scheduled_run_day")
            if isinstance(day, str) and len(day) == 8 and day.isdigit():
                return day
            return None
        except Exception:
            return None

    def set_last_scheduled_run_day(self, day: str) -> None:
        """YYYYMMDD 문자열 저장"""
        if not (isinstance(day, str) and len(day) == 8 and day.isdigit()):
            return
        payload = {"last_scheduled_run_day": day}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            # Windows 포함 원자적 교체
            tmp.replace(self._path)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


