import os
import yaml
from src.utils.logger import logger

class ConfigManager:
    _instance = None
    _config = None

    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    CONFIG_FILE = os.path.join(PROJECT_ROOT, 'config', 'settings.yaml')
    MOCK_TXT = os.path.join(PROJECT_ROOT, '모의투자.txt')
    REAL_TXT = os.path.join(PROJECT_ROOT, '실전투자.txt')

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """설정 초기화 및 로드"""
        if not os.path.exists(self.CONFIG_FILE):
            logger.info(f"[Config] settings.yaml 파일이 없습니다. 초기 설정을 생성합니다.")
            self._create_default_config()
        
        self.load_config()

    def _parse_txt_file(self, file_path):
        """모의/실전 투자 txt 파일 파싱"""
        data = {'APP_KEY': '', 'APP_SECRET': ''}
        if not os.path.exists(file_path):
            return data

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                current_key = None
                
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                        
                    if "APP Key" in line:
                        current_key = 'APP_KEY'
                        continue
                    elif "APP Secret" in line:
                        current_key = 'APP_SECRET'
                        continue
                    
                    if current_key and line:
                        # 키/시크릿 값 저장 (여러 줄일 수 있으나 보통 한 줄)
                        data[current_key] = line
                        current_key = None # 값 읽었으면 초기화
                        
        except Exception as e:
            logger.error(f"[Config] {file_path} 파싱 중 오류: {e}")
            
        return data

    def _create_default_config(self):
        """기본 설정 파일 생성"""
        # txt 파일에서 키 로드
        mock_data = self._parse_txt_file(self.MOCK_TXT)
        real_data = self._parse_txt_file(self.REAL_TXT)

        default_config = {
            'common': {
                'analysis_url': 'http://localhost:5000/analysis',
                'mode': 'mock',
                # 분석서버 대신 mock 데이터를 사용할지 여부 (테스트용)
                'analysis_mock_enabled': False,
            },
            'mock': {
                'account_no_prefix': '00000000',
                'account_no_suffix': '01',
                'app_key': mock_data['APP_KEY'],
                'app_secret': mock_data['APP_SECRET'],
                'auto_trading_enabled': False,
                # 장중 손절 감시(자동매매와 별개)
                'intraday_stop_loss': {
                    'enabled': False,
                    # 기준(%): -7이면 손절, +7이면 익절(장중 감시 매도)  (myKiwoom-main: threshold_pct)
                    'threshold_pct': -7.0,
                },
                # 자동매매 실행시간(1일 1회) - 1분 주기 체크로 해당 시각에만 실행
                'schedule_time': '22:30',
                'strategy': {
                    # 총 매수 예산(USD) - 종목 수만큼 N분할
                    'max_buy_amount': 1000,
                    'reserve_cash': 0,
                    'take_profit_pct': 5.0,
                    'stop_loss_pct': 3.0,
                    'max_hold_days': 15,
                    # 지정가 기반에서 체결 확률을 올리기 위한 슬리피지(%)
                    'slippage_pct': 0.5,
                },
                'url_base': "https://openapivts.koreainvestment.com:29443",
            },
            'real': {
                'account_no_prefix': '00000000',
                'account_no_suffix': '01',
                'app_key': real_data['APP_KEY'],
                'app_secret': real_data['APP_SECRET'],
                'auto_trading_enabled': False,
                'intraday_stop_loss': {
                    'enabled': False,
                    'threshold_pct': -7.0,
                },
                'schedule_time': '22:30',
                'strategy': {
                    'max_buy_amount': 500,
                    'reserve_cash': 1000,
                    'take_profit_pct': 3.0,
                    'stop_loss_pct': 2.0,
                    'max_hold_days': 10,
                    'slippage_pct': 0.5,
                },
                'url_base': "https://openapi.koreainvestment.com:9443",
            },
        }

        # config 디렉토리 확인
        os.makedirs(os.path.dirname(self.CONFIG_FILE), exist_ok=True)

        with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
            yaml.dump(default_config, f, allow_unicode=True, default_flow_style=False)
        
        logger.info(f"[Config] {self.CONFIG_FILE} 생성 완료.")

    def load_config(self):
        """설정 파일 로드"""
        try:
            with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)
        except Exception as e:
            logger.error(f"[Config] 설정 로드 실패: {e}")
            self._config = {}

    def get(self, key, default=None):
        """설정값 조회 (key1.key2 형식 지원)"""
        keys = key.split('.')
        value = self._config
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default

# 전역 인스턴스
config_manager = ConfigManager()

