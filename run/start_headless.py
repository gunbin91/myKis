import sys
import os
import multiprocessing

# 프로젝트 루트 경로를 sys.path에 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from src.web.app import start_scheduler  # 웹을 띄우지 않아도 스케줄러만 구동 가능
from src.utils.logger import logger


if __name__ == "__main__":
    try:
        multiprocessing.freeze_support()
        logger.info("=== myKis Headless Scheduler 시작 (mock/real 동시) ===")
        start_scheduler()
        logger.info("스케줄러가 백그라운드 프로세스로 실행 중입니다. 종료하려면 Ctrl+C")

        # 메인 프로세스는 살아있어야 자식(daemon) 프로세스도 유지됨
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("사용자 중지(Ctrl+C)로 종료합니다.")
    except Exception as e:
        logger.error(f"Headless Scheduler 실행 중 오류: {e}")
        raise

