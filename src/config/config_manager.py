import os
import yaml
from src.utils.logger import logger
from pathlib import Path

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
                'analysis_url': 'http://localhost:5500/v1/analysis/result',
                'analysis_host': 'localhost',
                'analysis_port': 5500,
                # reserve_cash_krw(원화)을 USD 예산으로 환산할 때 사용하는 기준 환율(사용자 입력/환경에 맞게 조정)
                'usd_krw_rate': 1350.0,
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
                    'top_n': 5,
                    # 매수 제외 금액(원화) - UI에서 입력
                    'reserve_cash_krw': 0,
                    'take_profit_pct': 5.0,
                    # 손절/익절은 부호 그대로 사용(프론트에서 ± 입력 가능)
                    # 예) -3 => -3% 손절
                    'stop_loss_pct': -3.0,
                    'max_hold_days': 15,
                    # 지정가 기반에서 체결 확률을 올리기 위한 슬리피지(%)
                    'slippage_pct': 0.5,
                    # 매수 주문 방식:
                    # - mock: 호가 API 미지원 → 현재가(+slippage) 지정가
                    # - real: 매도호가 기반 지정가(ask ladder) 권장
                    'buy_order_method': 'limit_slippage',
                    # ask ladder 매수 시, 현재가 대비 허용 프리미엄 상한(+%)
                    'limit_buy_max_premium_pct': 1.0,
                    # ask ladder 매수 시, 최대 시도 호가 레벨(미국: 최대 10)
                    'limit_buy_max_levels': 5,
                    # 호가 단계별 체결/미체결 확인 대기(초)
                    'limit_buy_step_wait_sec': 1.0,
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
                    'top_n': 3,
                    'reserve_cash_krw': 0,
                    'take_profit_pct': 3.0,
                    'stop_loss_pct': -2.0,
                    'max_hold_days': 10,
                    'slippage_pct': 0.5,
                    'buy_order_method': 'limit_ask_ladder',
                    'limit_buy_max_premium_pct': 1.0,
                    'limit_buy_max_levels': 5,
                    'limit_buy_step_wait_sec': 1.0,
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

    def save_config(self, config: dict | None = None) -> None:
        """
        설정 파일 저장(원자적 교체).

        멀티프로세스 환경에서 reader(스케줄러)와 writer(웹 저장)가 동시에 접근해도
        중간 상태(깨진 YAML)를 읽지 않도록 tmp 파일로 쓴 뒤 replace 한다.
        """
        data = config if config is not None else (self._config or {})
        try:
            path = Path(self.CONFIG_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
            tmp.replace(path)  # Windows 포함 원자적 교체
        except Exception as e:
            logger.error(f"[Config] 설정 저장 실패: {e}")
            raise

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

