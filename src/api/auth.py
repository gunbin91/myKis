import time
import requests
import json
from datetime import datetime, timedelta
import threading
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

        # 토큰이 없거나 만료되었으면 재발급(동시 발급 방지)
        lock = self._token_locks.get(mode) or threading.Lock()
        with lock:
            current_token_info = self.access_tokens.get(mode)
            if self._is_token_valid(current_token_info):
                return current_token_info['token']
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
            res = requests.post(url, headers=headers, data=json.dumps(body))
            
            if res.status_code == 200:
                data = res.json()
                access_token = data.get('access_token')
                expires_in = data.get('expires_in', 86400) # 기본 24시간
                
                # 토큰 저장
                self.access_tokens[mode]['token'] = f"Bearer {access_token}"
                self.access_tokens[mode]['expired_at'] = datetime.now() + timedelta(seconds=expires_in)
                
                log.info(f"토큰 발급 성공 (만료: {self.access_tokens[mode]['expired_at']})")
                return self.access_tokens[mode]['token']
            else:
                log.error(f"토큰 발급 실패: {res.text}")
                return None

        except Exception as e:
            log.error(f"토큰 발급 중 오류 발생: {e}")
            return None

# 전역 인스턴스
kis_auth = KisAuth()

