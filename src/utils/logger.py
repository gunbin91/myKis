import json
import logging
import os
import sys
from datetime import datetime

_LOGGER_CACHE = {}
_ENGINE_API_LOGGING_ENABLED = {}

def _ensure_dir(path: str):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def setup_logger(name=None, log_file: str | None = None):
    """로거 설정 및 반환

    - log_file 지정 시 해당 파일로 기록
    - 미지정 시 logs/system_YYYYMMDD.log
    """
    # 프로젝트 루트 경로 찾기 (현재 파일 기준 상위 3단계)
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    LOG_DIR = os.path.join(PROJECT_ROOT, 'logs')
    
    _ensure_dir(LOG_DIR)
        
    # 로그 파일명 (날짜별)
    today = datetime.now().strftime("%Y%m%d")
    if log_file is None:
        log_file = os.path.join(LOG_DIR, f"system_{today}.log")
    
    # 로거 생성
    logger = logging.getLogger(name if name else 'myKis')
    logger.setLevel(logging.INFO)
    # 로거 이름이 계층(myKis.mock 등)인 경우, 상위 로거로 propagate 되면서 중복 출력될 수 있음
    logger.propagate = False
    
    # 중복 핸들러 방지
    if logger.handlers:
        return logger
        
    # 포맷 설정
    formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 파일 핸들러
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # 콘솔 핸들러
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

class _PrefixAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        prefix = self.extra.get("prefix")
        if prefix:
            msg = f"{prefix} {msg}"
        return msg, kwargs


def get_mode_logger(mode: str, source: str | None = None):
    """
    mock/real 로그를 파일로 분리하기 위한 로거
    - logs/mock/system_YYYYMMDD.log
    - logs/real/system_YYYYMMDD.log
    """
    mode = (mode or "unknown").strip().lower()
    cache_key = f"myKis.{mode}"
    if cache_key in _LOGGER_CACHE:
        base = _LOGGER_CACHE[cache_key]
        if source:
            return _PrefixAdapter(base, {"prefix": f"[{mode.upper()}][{source.upper()}]"})
        return base

    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    mode_dir = os.path.join(PROJECT_ROOT, "logs", mode)
    _ensure_dir(mode_dir)
    today = datetime.now().strftime("%Y%m%d")
    log_file = os.path.join(mode_dir, f"system_{today}.log")

    lg = setup_logger(name=cache_key, log_file=log_file)
    _LOGGER_CACHE[cache_key] = lg
    if source:
        return _PrefixAdapter(lg, {"prefix": f"[{mode.upper()}][{source.upper()}]"})
    return lg

# 기본 로거 인스턴스
logger = setup_logger()


def log_engine_api(mode: str, payload: dict):
    """
    자동매매(ENGINE) API 요청/응답 로깅.
    - logs/api/{mode}/engine_YYYYMMDD.jsonl
    """
    try:
        mode = (mode or "unknown").strip().lower()
        if not _ENGINE_API_LOGGING_ENABLED.get(mode, False):
            return
        PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        log_dir = os.path.join(PROJECT_ROOT, "logs", "api", mode)
        _ensure_dir(log_dir)
        today = datetime.now().strftime("%Y%m%d")
        log_file = os.path.join(log_dir, f"engine_{today}.jsonl")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload or {}, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # 로깅 실패는 매매 실패로 간주하지 않음
        pass

def set_engine_api_logging(mode: str, enabled: bool) -> None:
    """
    엔진 API 로깅 on/off.
    - 실제 자동매매 실행 구간에서만 기록하도록 제어
    """
    try:
        m = (mode or "unknown").strip().lower()
        _ENGINE_API_LOGGING_ENABLED[m] = bool(enabled)
    except Exception:
        pass

