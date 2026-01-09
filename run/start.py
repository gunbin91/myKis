import sys
import os
import socket
import webbrowser
from threading import Timer

# 프로젝트 루트 경로를 sys.path에 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from src.web.app import app, start_scheduler
from src.utils.logger import logger

def find_available_port(start_port=7500, max_port=7600):
    """사용 가능한 포트 찾기"""
    for port in range(start_port, max_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('localhost', port)) != 0:
                return port
    return start_port

def open_browser(port):
    """브라우저 실행"""
    webbrowser.open_new(f"http://localhost:{port}")

if __name__ == "__main__":
    try:
        # 가용 포트 탐색
        port = find_available_port()
        logger.info(f"=== myKis 시스템 시작 (Port: {port}) ===")
        
        # 스케줄러는 서버 실행 시점에만 시작 (중복 시작 방지)
        start_scheduler()

        # 서버 시작 1.5초 후 브라우저 자동 실행
        Timer(1.5, open_browser, args=[port]).start()
        
        # Flask 앱 실행
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        
    except Exception as e:
        logger.error(f"서버 실행 중 오류 발생: {e}")
        input("엔터를 누르면 종료합니다...")
