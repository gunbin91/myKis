from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from src.utils.logger import logger


@dataclass
class FxRateResult:
    rate: Optional[float]
    source: str
    fetched_at: Optional[str] = None
    error: Optional[str] = None


# 프로세스 메모리 캐시(서버 재시작 시 초기화)
_CACHE: dict[str, Tuple[FxRateResult, datetime]] = {}
_CACHE_TTL_SEC = 600  # 10분


def _cache_get(key: str) -> Optional[FxRateResult]:
    item = _CACHE.get(key)
    if not item:
        return None
    val, expires_at = item
    if datetime.now() >= expires_at:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: FxRateResult) -> None:
    _CACHE[key] = (val, datetime.now() + timedelta(seconds=_CACHE_TTL_SEC))


def _to_float(v) -> float:
    try:
        if v is None:
            return 0.0
        s = str(v).replace(",", "").strip()
        if s == "":
            return 0.0
        return float(s)
    except Exception:
        return 0.0


def _extract_usd_krw_from_present(present: dict) -> FxRateResult:
    """
    KIS v1_008 응답에서 USD/KRW(원/달러) 환율 추출.
    - 우선: output3.frst_bltn_exrt (최초고시환율)
    - 차선: output1[].bass_exrt (기준환율; 첫 유효값)
    """
    try:
        out3 = (present or {}).get("output3") or {}
        r = _to_float(out3.get("frst_bltn_exrt"))
        if r > 0:
            return FxRateResult(rate=r, source="kis_v1_008_out3_frst_bltn_exrt", fetched_at=datetime.now().isoformat())
    except Exception as e:
        return FxRateResult(rate=None, source="kis_v1_008_error", fetched_at=datetime.now().isoformat(), error=str(e))

    try:
        out1 = (present or {}).get("output1") or []
        rows = out1 if isinstance(out1, list) else [out1]
        for row in rows:
            if not isinstance(row, dict):
                continue
            r = _to_float(row.get("bass_exrt"))
            if r > 0:
                return FxRateResult(rate=r, source="kis_v1_008_out1_bass_exrt", fetched_at=datetime.now().isoformat())
    except Exception as e:
        return FxRateResult(rate=None, source="kis_v1_008_error", fetched_at=datetime.now().isoformat(), error=str(e))

    return FxRateResult(rate=None, source="kis_v1_008_unavailable", fetched_at=datetime.now().isoformat())


def _fetch_usd_krw_from_fdr() -> FxRateResult:
    """
    FinanceDataReader로 USD/KRW 환율(당일/최근)을 조회.
    - 키움증권 자동매매 분석 프로젝트에서 쓰는 방식과 동일: fdr.DataReader('USD/KRW', start, end)['Close'].
    """
    try:
        import FinanceDataReader as fdr

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        df = fdr.DataReader("USD/KRW", start, end)
        if df is None or getattr(df, "empty", True):
            return FxRateResult(rate=None, source="fdr_usdkrw_empty", fetched_at=datetime.now().isoformat())
        # 최근 종가(또는 마지막 행의 Close) 사용
        try:
            last = float(df["Close"].iloc[-1])
        except Exception:
            # 컬럼명이 다르면 마지막 값 폴백
            last = float(df.iloc[-1].values[-1])
        if last > 0:
            return FxRateResult(rate=last, source="fdr_usdkrw", fetched_at=datetime.now().isoformat())
        return FxRateResult(rate=None, source="fdr_usdkrw_invalid", fetched_at=datetime.now().isoformat())
    except Exception as e:
        return FxRateResult(rate=None, source="fdr_error", fetched_at=datetime.now().isoformat(), error=str(e))


def get_usd_krw_rate(
    *,
    mode: str,
    kis_present: Optional[dict] = None,
    cache_key: str = "usd_krw",
) -> FxRateResult:
    """
    USD/KRW 환율 자동 조회:
    - 1순위: KIS(v1_008)에서 추출
    - 2순위: FinanceDataReader 'USD/KRW'
    - 둘 다 실패하면 rate=None (호출부에서 '매수 스킵' 등 정책 처리)
    """
    cached = _cache_get(cache_key)
    if cached:
        return cached

    # 1) KIS
    try:
        if kis_present is None:
            from src.api.order import kis_order

            kis_present = kis_order.get_present_balance(
                natn_cd="000",
                tr_mket_cd="00",
                inqr_dvsn_cd="00",
                wcrc_frcr_dvsn_cd="02",
                mode=mode,
            ) or {}
        r1 = _extract_usd_krw_from_present(kis_present)
        if r1.rate and r1.rate > 0:
            _cache_set(cache_key, r1)
            return r1
    except Exception as e:
        logger.warning(f"[FX] KIS 환율 추출 실패(무시하고 FDR 폴백): {e}")

    # 2) FinanceDataReader
    r2 = _fetch_usd_krw_from_fdr()
    if r2.rate and r2.rate > 0:
        _cache_set(cache_key, r2)
        return r2

    # 실패: 캐시하지 않고 그대로 반환(잠시 후 재시도 가능)
    return FxRateResult(rate=None, source=f"{r1.source if 'r1' in locals() else 'kis_unknown'}+{r2.source}", fetched_at=datetime.now().isoformat(), error=(r2.error or (r1.error if 'r1' in locals() else None)))


def get_cached_usd_krw_rate(cache_key: str = "usd_krw") -> Optional[FxRateResult]:
    """
    캐시된 USD/KRW 환율만 반환 (네트워크/KIS 호출 없음).
    - 없으면 None
    """
    return _cache_get(cache_key)

