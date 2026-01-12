"""
모의/실전 자동매매를 동시에(서로 간섭 없이) 돌리기 위한 멀티프로세스 스케줄러.

키움 샘플(kiwoomAutoProgram/src/auto_trading/scheduler.py)의 패턴을 간소화하여 이식:
- 모드별(mock/real)로 별도 프로세스를 띄워서 루프 수행
- 각 프로세스는 자신의 mode를 runtime common.mode로 주입(파일에는 쓰지 않음)
- 상태/하트비트는 파일로 기록하여 웹(UI) 프로세스에서 확인 가능
"""

import multiprocessing
import os
import signal
import threading
import time
from datetime import datetime
import atexit

from src.config.config_manager import config_manager
from src.engine.engine import trading_engine
from src.engine.scheduler_state_store import SchedulerStateStore
from src.utils.logger import get_mode_logger


class _ModeScheduler:
    def __init__(self, mode: str):
        self.mode = (mode or "mock").strip().lower()
        self._proc: multiprocessing.Process | None = None
        self._lock = threading.Lock()
        # watchdog/backoff
        self._restart_fail_count = 0
        self._next_restart_at = 0.0  # epoch seconds

    def start(self):
        with self._lock:
            if self._proc and self._proc.is_alive():
                return

            # 기존 프로세스가 남아있으면 정리
            if self._proc and (not self._proc.is_alive()):
                try:
                    self._proc.join(timeout=0.2)
                except Exception:
                    pass
                self._proc = None

            self._proc = multiprocessing.Process(
                target=_scheduler_loop,
                name=f"myKisScheduler-{self.mode}",
                args=(self.mode,),
                daemon=True,
            )
            self._proc.start()

    def stop(self):
        with self._lock:
            if self._proc and self._proc.is_alive():
                try:
                    self._proc.terminate()
                    self._proc.join(timeout=2)
                    if self._proc.is_alive():
                        self._proc.kill()
                except Exception:
                    pass
            self._proc = None
            self._restart_fail_count = 0
            self._next_restart_at = 0.0

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.is_alive())

    def pid(self) -> int | None:
        try:
            return int(self._proc.pid) if self._proc and self._proc.pid else None
        except Exception:
            return None

    def mark_restart_failure(self):
        """
        연속 실패에 대한 백오프를 설정한다.
        - 너무 빠르게 죽는 경우 무한 재시작 루프를 피한다.
        """
        with self._lock:
            self._restart_fail_count += 1
            # 1,2,4,8,16,30 sec ... (max 30)
            backoff = min(30, 2 ** max(0, self._restart_fail_count - 1))
            self._next_restart_at = time.time() + backoff

    def can_restart_now(self) -> bool:
        with self._lock:
            return time.time() >= (self._next_restart_at or 0.0)

    def reset_restart_backoff(self):
        with self._lock:
            self._restart_fail_count = 0
            self._next_restart_at = 0.0


def _scheduler_loop(mode: str):
    mode = (mode or "mock").strip().lower()
    log = get_mode_logger(mode)
    state = SchedulerStateStore(mode=mode)

    is_executing = False
    last_error = None

    def _sig_handler(signum, frame):
        nonlocal last_error
        last_error = f"signal:{signum}"
        try:
            state.heartbeat(pid=os.getpid(), is_running=False, is_executing=is_executing, last_error=last_error)
        except Exception:
            pass
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGINT, _sig_handler)
    except Exception:
        # Windows/환경에 따라 signal 설정이 제한될 수 있음
        pass

    started_at = datetime.now().isoformat(timespec="seconds")
    restart_count = 0
    log.info(f"[Scheduler] 프로세스 시작: pid={os.getpid()}, mode={mode}")
    state.heartbeat(
        pid=os.getpid(),
        is_running=True,
        is_executing=False,
        last_error=None,
        extra={
            "process_started_at": started_at,
            "restart_count": restart_count,
        },
    )

    while True:
        loop_started = datetime.now()
        try:
            # 1) 설정 reload (웹에서 저장해도 반영)
            config_manager.load_config()
            # 2) 런타임 모드 주입 (파일에는 쓰지 않음) - mode=None 기본 동작이 안전해짐
            try:
                config_manager._config.setdefault("common", {})
                config_manager._config["common"]["mode"] = mode
            except Exception:
                pass

            is_executing = False
            state.heartbeat(
                pid=os.getpid(),
                is_running=True,
                is_executing=False,
                last_error=last_error,
                extra={
                    "started_at": loop_started.isoformat(timespec="seconds"),
                    "engine_last_run_at": trading_engine.last_run_at.isoformat() if trading_engine.last_run_at else None,
                    "engine_last_error": trading_engine.last_error,
                    "stop_watch_last_run_at": trading_engine.last_stop_watch_at.isoformat() if trading_engine.last_stop_watch_at else None,
                    "stop_watch_last_error": trading_engine.last_stop_watch_error,
                    "process_started_at": started_at,
                    "restart_count": restart_count,
                },
            )

            # 3) 장중 손절 감시(모드별) - intraday_stop_loss.enabled 기반, 자동매매와 별개
            trading_engine.stop_loss_watch()

            # 4) 자동매매(모드별) - auto_trading_enabled + schedule_time + 하루 1회 조건은 엔진 내부에서 처리
            is_executing = True
            state.heartbeat(
                pid=os.getpid(),
                is_running=True,
                is_executing=True,
                last_error=last_error,
            )
            trading_engine.run()
            is_executing = False

            # 실행 후 상태 갱신
            state.heartbeat(
                pid=os.getpid(),
                is_running=True,
                is_executing=False,
                last_error=last_error,
                extra={
                    "engine_last_run_at": trading_engine.last_run_at.isoformat() if trading_engine.last_run_at else None,
                    "engine_last_error": trading_engine.last_error,
                    "stop_watch_last_run_at": trading_engine.last_stop_watch_at.isoformat() if trading_engine.last_stop_watch_at else None,
                    "stop_watch_last_error": trading_engine.last_stop_watch_error,
                    "process_started_at": started_at,
                    "restart_count": restart_count,
                },
            )

        except SystemExit:
            break
        except Exception as e:
            last_error = str(e)
            try:
                log.error(f"[Scheduler] 루프 오류: {e}")
            except Exception:
                pass
            try:
                state.heartbeat(pid=os.getpid(), is_running=True, is_executing=is_executing, last_error=last_error)
            except Exception:
                pass

        # 1분 주기(키움 샘플과 동일). 실행 시간이 길어지면 그만큼 다음 체크가 늦어질 수 있음.
        time.sleep(60)

    log.info(f"[Scheduler] 프로세스 종료: pid={os.getpid()}, mode={mode}")


class MultiProcessScheduler:
    """
    mock/real 프로세스를 함께 관리하는 상위 스케줄러.
    """

    def __init__(self):
        self.mock = _ModeScheduler("mock")
        self.real = _ModeScheduler("real")
        self._started = False
        self._lock = threading.Lock()
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()

    def start(self):
        with self._lock:
            if self._started:
                return
            self.mock.start()
            self.real.start()
            self._start_watchdog()
            self._started = True

    def stop(self):
        with self._lock:
            self._stop_watchdog()
            self.mock.stop()
            self.real.stop()
            self._started = False

    def started(self) -> bool:
        return self._started

    def _start_watchdog(self):
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()

        def _loop():
            # 운영 안정성: 죽으면 자동 재시작 + 백오프
            while not self._watchdog_stop.is_set():
                for ms in (self.mock, self.real):
                    try:
                        # 살아있으면 백오프 리셋
                        if ms.is_alive():
                            ms.reset_restart_backoff()
                            continue

                        # 너무 빠른 재시작 방지
                        if not ms.can_restart_now():
                            continue

                        # 상태 파일에 restart 시도 기록(웹에서 확인 가능)
                        st = SchedulerStateStore(mode=ms.mode)
                        prev = st.read() or {}
                        rc = int(prev.get("restart_count") or 0) + 1
                        st.heartbeat(
                            pid=0,
                            is_running=False,
                            is_executing=False,
                            last_error=str(prev.get("last_error") or "process_down"),
                            extra={
                                "restart_count": rc,
                                "restart_last_at": datetime.now().isoformat(timespec="seconds"),
                                "restart_reason": "watchdog",
                            },
                        )

                        ms.start()
                    except Exception:
                        ms.mark_restart_failure()
                time.sleep(2.0)

        self._watchdog_thread = threading.Thread(target=_loop, name="myKisSchedulerWatchdog", daemon=True)
        self._watchdog_thread.start()

    def _stop_watchdog(self):
        try:
            self._watchdog_stop.set()
            if self._watchdog_thread and self._watchdog_thread.is_alive():
                self._watchdog_thread.join(timeout=1.0)
        except Exception:
            pass
        self._watchdog_thread = None


def _atexit_cleanup():
    try:
        multi_process_scheduler.stop()
    except Exception:
        pass


atexit.register(_atexit_cleanup)


# 전역 인스턴스
multi_process_scheduler = MultiProcessScheduler()

