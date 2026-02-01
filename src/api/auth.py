import time
import os
import requests
import json
from datetime import datetime, timedelta
import threading
from pathlib import Path
from src.config.config_manager import config_manager
from src.utils.logger import logger, get_mode_logger

class KisAuth:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(KisAuth, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """초기화"""
        self.access_tokens = {
            'mock': {'token': None, 'expired_at': None},
            'real': {'token': None, 'expired_at': None}
        }
        # 토큰 발급은 분당 1회 제한이 있을 수 있어 동시 발급을 막는다(모드별 락).
        self._token_locks = {
            'mock': threading.Lock(),
            'real': threading.Lock(),
        }
        # 프로세스 간 토큰 발급 경쟁(EGW00133) 방지:
        # - 토큰 자체를 공유하지 않더라도 "발급 시도"는 모드별로 조율해야 한다.
        project_root = Path(__file__).resolve().parents[2]
        self._data_dir = project_root / "data"
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def get_token(self, mode=None):
        """
        유효한 접근 토큰 반환
        mode: 'mock' 또는 'real'. None이면 config의 기본 모드 사용
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')

        # 모드별 로그 분리
        self._log = get_mode_logger(mode)

        # 현재 토큰 상태 확인
        current_token_info = self.access_tokens.get(mode)
        if self._is_token_valid(current_token_info):
            return current_token_info['token']

        # 프로세스 간 발급 쿨다운(예: EGW00133) 체크
        if self._is_in_issue_cooldown(mode):
            return None

        # 토큰이 없거나 만료되었으면 재발급(동시 발급 방지)
        lock = self._token_locks.get(mode) or threading.Lock()
        with lock:
            current_token_info = self.access_tokens.get(mode)
            if self._is_token_valid(current_token_info):
                return current_token_info['token']
            # 쿨다운/동시발급 재확인
            if self._is_in_issue_cooldown(mode):
                return None
            return self._issue_token(mode)

    def _is_token_valid(self, token_info):
        """토큰 유효성 검사 (만료 1분 전까지 유효한 것으로 간주)"""
        if not token_info or not token_info['token'] or not token_info['expired_at']:
            return False
            
        now = datetime.now()
        # 만료 시간보다 60초 여유를 두고 체크
        if now < (token_info['expired_at'] - timedelta(seconds=60)):
            return True
            
        return False

    def _issue_token(self, mode):
        """접근 토큰 발급 요청"""
        log = get_mode_logger(mode)
        # 프로세스 간 동시 발급 방지(모드별 락 파일)
        lock_path = self._acquire_process_issue_lock(mode)
        if lock_path is None:
            # 다른 프로세스가 이미 발급 시도 중이면, 과도한 재시도/스팸 로그를 막기 위해 짧게 쿨다운을 둔다.
            # (요청: 토큰 재시도 텀을 10초로)
            self._set_issue_cooldown(mode, seconds=10, reason="issue_in_progress")
            return None
        config_key = mode  # 'mock' or 'real'
        app_key = config_manager.get(f'{config_key}.app_key')
        app_secret = config_manager.get(f'{config_key}.app_secret')
        url_base = config_manager.get(f'{config_key}.url_base')

        if not app_key or not app_secret:
            log.error("APP Key 또는 Secret이 설정되지 않았습니다.")
            return None

        url = f"{url_base}/oauth2/tokenP"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": app_key,
            "appsecret": app_secret
        }

        try:
            log.info("토큰 발급 요청 중...")
            res = requests.post(url, headers=headers, data=json.dumps(body), timeout=20)
            
            if res.status_code == 200:
                data = res.json()
                access_token = data.get('access_token')
                expires_in = data.get('expires_in', 86400) # 기본 24시간
                
                # 토큰 저장
                self.access_tokens[mode]['token'] = f"Bearer {access_token}"
                self.access_tokens[mode]['expired_at'] = datetime.now() + timedelta(seconds=expires_in)
                
                log.info(f"토큰 발급 성공 (만료: {self.access_tokens[mode]['expired_at']})")
                # 성공 시 쿨다운 해제(즉시 다음 발급 가능하진 않아도 되지만, 불필요한 차단은 제거)
                self._set_issue_cooldown(mode, seconds=0, reason="issued")
                return self.access_tokens[mode]['token']
            else:
                log.error(f"토큰 발급 실패: {res.text}")
                # EGW00133: 1분당 1회 제한. 프로세스 간 쿨다운을 모드별로 기록해 재시도 폭주를 막는다.
                cool_sec = 10
                try:
                    payload = res.json() or {}
                    if payload.get("error_code") == "EGW00133" or payload.get("msg_cd") == "EGW00133":
                        cool_sec = 65
                except Exception:
                    pass
                self._set_issue_cooldown(mode, seconds=cool_sec, reason="issue_failed")
                return None

        except Exception as e:
            log.error(f"토큰 발급 중 오류 발생: {e}")
            # 순간 네트워크 오류 등은 짧은 쿨다운만
            self._set_issue_cooldown(mode, seconds=5, reason="exception")
            return None
        finally:
            self._release_process_issue_lock(lock_path)

    def invalidate_token(self, mode: str | None = None) -> None:
        """
        토큰 강제 무효화:
        - 서버가 만료 토큰(EGW00123)을 반환하는 경우 재발급 유도용
        """
        if mode is None:
            mode = config_manager.get('common.mode', 'mock')
        try:
            if mode in self.access_tokens:
                self.access_tokens[mode]['token'] = None
                self.access_tokens[mode]['expired_at'] = None
        except Exception:
            pass

    # ---- process-wide (cross-process) helpers ----

    def _token_meta_path(self, mode: str) -> Path:
        return self._data_dir / f"token_meta_{mode}.json"

    def _issue_lock_path(self, mode: str) -> Path:
        return self._data_dir / f"token_issue_{mode}.lock"

    def _read_token_meta(self, mode: str) -> dict:
        path = self._token_meta_path(mode)
        try:
            if not path.exists():
                return {}
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _write_token_meta(self, mode: str, meta: dict) -> None:
        path = self._token_meta_path(mode)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta or {}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def _is_in_issue_cooldown(self, mode: str) -> bool:
        meta = self._read_token_meta(mode)
        next_at = meta.get("next_issue_at")
        if not next_at:
            return False
        try:
            dt = datetime.fromisoformat(str(next_at))
            return datetime.now() < dt
        except Exception:
            return False

    def _set_issue_cooldown(self, mode: str, seconds: int, reason: str) -> None:
        try:
            seconds = int(seconds or 0)
        except Exception:
            seconds = 0
        meta = self._read_token_meta(mode)
        meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
        meta["reason"] = reason
        if seconds <= 0:
            meta.pop("next_issue_at", None)
        else:
            meta["next_issue_at"] = (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        self._write_token_meta(mode, meta)

    def _acquire_process_issue_lock(self, mode: str):
        """
        Windows에서도 동작하는 간단한 프로세스 간 락:
        - O_EXCL로 lock 파일을 선점한 프로세스만 발급 진행
        """
        p = self._issue_lock_path(mode)
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("utf-8"))
            finally:
                try:
                    os.close(fd)
                except Exception:
                    pass
            return p
        except FileExistsError:
            # 비정상 종료로 락 파일이 남을 수 있어, 충분히 오래된 경우는 정리한다.
            try:
                age_sec = time.time() - os.path.getmtime(str(p))
                if age_sec > 120:
                    os.remove(str(p))
                    # 1회 재시도
                    fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    try:
                        os.write(fd, str(os.getpid()).encode("utf-8"))
                    finally:
                        try:
                            os.close(fd)
                        except Exception:
                            pass
                    return p
            except Exception:
                pass
            return None
        except Exception:
            return None

    def _release_process_issue_lock(self, lock_path: Path | None) -> None:
        try:
            if lock_path is None:
                return
            try:
                if lock_path.exists():
                    lock_path.unlink()
            except Exception:
                pass
        except Exception:
            pass

# 전역 인스턴스
kis_auth = KisAuth()

