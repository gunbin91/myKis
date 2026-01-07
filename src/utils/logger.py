import logging
import os
import sys
from datetime import datetime

_LOGGER_CACHE = {}

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

def get_mode_logger(mode: str):
    """
    mock/real 로그를 파일로 분리하기 위한 로거
    - logs/mock/system_YYYYMMDD.log
    - logs/real/system_YYYYMMDD.log
    """
    mode = (mode or "unknown").strip().lower()
    cache_key = f"myKis.{mode}"
    if cache_key in _LOGGER_CACHE:
        return _LOGGER_CACHE[cache_key]

    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    mode_dir = os.path.join(PROJECT_ROOT, "logs", mode)
    _ensure_dir(mode_dir)
    today = datetime.now().strftime("%Y%m%d")
    log_file = os.path.join(mode_dir, f"system_{today}.log")

    lg = setup_logger(name=cache_key, log_file=log_file)
    _LOGGER_CACHE[cache_key] = lg
    return lg

# 기본 로거 인스턴스
logger = setup_logger()

