"""
거래소 코드 매핑 유틸

- Quote(시세) API: EXCD (NAS/NYS/AMS/...)
- Order/Balance API: OVRS_EXCG_CD (NASD/NYSE/AMEX/...)
"""

from __future__ import annotations


QUOTE_TO_ORDER = {
    # US
    "NAS": "NASD",
    "NYS": "NYSE",
    "AMS": "AMEX",
    # US day session (quote)
    "BAQ": "NASD",
    "BAY": "NYSE",
    "BAA": "AMEX",
}

ORDER_TO_QUOTE = {
    "NASD": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
}

ANALYSIS_TO_QUOTE = {
    "NASDAQ": "NAS",
    "NASD": "NAS",
    "NAS": "NAS",
    "NYSE": "NYS",
    "NYS": "NYS",
    "AMEX": "AMS",
    "AMS": "AMS",
    "BAQ": "NAS",
    "BAY": "NYS",
    "BAA": "AMS",
    "나스닥": "NAS",
    "뉴욕": "NYS",
    "아멕스": "AMS",
}


def normalize_quote_exchange(excd: str | None) -> str:
    """Quote API용 EXCD로 정규화."""
    if not excd:
        return "NAS"
    excd = excd.strip().upper()
    if excd in ORDER_TO_QUOTE:
        return ORDER_TO_QUOTE[excd]
    return excd


def normalize_order_exchange(ovrs_excg_cd: str | None) -> str:
    """Order/Balance API용 OVRS_EXCG_CD로 정규화."""
    if not ovrs_excg_cd:
        return "NASD"
    ovrs_excg_cd = ovrs_excg_cd.strip().upper()
    if ovrs_excg_cd in QUOTE_TO_ORDER:
        return QUOTE_TO_ORDER[ovrs_excg_cd]
    return ovrs_excg_cd


def normalize_analysis_exchange(value: str | None) -> str | None:
    """
    분석 결과의 시장구분을 Quote용 EXCD(NAS/NYS/AMS)로 정규화.
    """
    if not value:
        return None
    v = str(value).strip().upper()
    if not v:
        return None
    if v in ANALYSIS_TO_QUOTE:
        return ANALYSIS_TO_QUOTE[v]
    if v in ORDER_TO_QUOTE:
        return ORDER_TO_QUOTE[v]
    if v in QUOTE_TO_ORDER:
        ovrs = QUOTE_TO_ORDER[v]
        return ORDER_TO_QUOTE.get(ovrs)
    return None


