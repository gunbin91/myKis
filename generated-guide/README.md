# 한투 오픈API 가이드 (xlsx → md 자동생성)

> 이 문서는 현재 폴더의 `.xlsx` 파일(선별된 API)로부터 자동 생성되었습니다.

## [해외주식] 기본시세

| API 명 | API ID | Method | URL | 문서 |
|---|---|---|---|---|
| 해외주식 현재가상세 | `v1_해외주식-029` | `GET` | `/uapi/overseas-price/v1/quotations/price-detail` | [link](overseas-stock/basic-price/v1_해외주식-029_해외주식 현재가상세.md) |
| 해외주식 현재가 호가 | `해외주식-033` | `GET` | `/uapi/overseas-price/v1/quotations/inquire-asking-price` | [link](overseas-stock/basic-price/해외주식-033_해외주식 현재가 호가.md) |
| 해외주식 현재체결가 | `v1_해외주식-009` | `GET` | `/uapi/overseas-price/v1/quotations/price` | [link](overseas-stock/basic-price/v1_해외주식-009_해외주식 현재체결가.md) |
| 해외주식 체결추이 | `해외주식-037` | `GET` | `/uapi/overseas-price/v1/quotations/inquire-ccnl` | [link](overseas-stock/basic-price/해외주식-037_해외주식 체결추이.md) |
| 해외주식분봉조회 | `v1_해외주식-030` | `GET` | `/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice` | [link](overseas-stock/basic-price/v1_해외주식-030_해외주식분봉조회.md) |
| 해외지수분봉조회 | `v1_해외주식-031` | `GET` | `/uapi/overseas-price/v1/quotations/inquire-time-indexchartprice` | [link](overseas-stock/basic-price/v1_해외주식-031_해외지수분봉조회.md) |
| 해외주식 기간별시세 | `v1_해외주식-010` | `GET` | `/uapi/overseas-price/v1/quotations/dailyprice` | [link](overseas-stock/basic-price/v1_해외주식-010_해외주식 기간별시세.md) |
| 해외주식 종목/지수/환율기간별시세(일/주/월/년) | `v1_해외주식-012` | `GET` | `/uapi/overseas-price/v1/quotations/inquire-daily-chartprice` | [link](overseas-stock/basic-price/v1_해외주식-012_해외주식 종목_지수_환율기간별시세(일_주_월_년).md) |
| 해외주식조건검색 | `v1_해외주식-015` | `GET` | `/uapi/overseas-price/v1/quotations/inquire-search` | [link](overseas-stock/basic-price/v1_해외주식-015_해외주식조건검색.md) |
| 해외결제일자조회 | `해외주식-017` | `GET` | `/uapi/overseas-stock/v1/quotations/countries-holiday` | [link](overseas-stock/basic-price/해외주식-017_해외결제일자조회.md) |
| 해외주식 상품기본정보 | `v1_해외주식-034` | `GET` | `/uapi/overseas-price/v1/quotations/search-info` | [link](overseas-stock/basic-price/v1_해외주식-034_해외주식 상품기본정보.md) |
| 해외주식 업종별시세 | `해외주식-048` | `GET` | `/uapi/overseas-price/v1/quotations/industry-theme` | [link](overseas-stock/basic-price/해외주식-048_해외주식 업종별시세.md) |
| 해외주식 업종별코드조회 | `해외주식-049` | `GET` | `/uapi/overseas-price/v1/quotations/industry-price` | [link](overseas-stock/basic-price/해외주식-049_해외주식 업종별코드조회.md) |

## [해외주식] 시세분석

| API 명 | API ID | Method | URL | 문서 |
|---|---|---|---|---|
| 해외주식 가격급등락 | `해외주식-038` | `GET` | `/uapi/overseas-stock/v1/ranking/price-fluct` | [link](overseas-stock/analysis/해외주식-038_해외주식 가격급등락.md) |
| 해외주식 거래량급증 | `해외주식-039` | `GET` | `/uapi/overseas-stock/v1/ranking/volume-surge` | [link](overseas-stock/analysis/해외주식-039_해외주식 거래량급증.md) |
| 해외주식 매수체결강도상위 | `해외주식-040` | `GET` | `/uapi/overseas-stock/v1/ranking/volume-power` | [link](overseas-stock/analysis/해외주식-040_해외주식 매수체결강도상위.md) |
| 해외주식 상승율/하락율 | `해외주식-041` | `GET` | `/uapi/overseas-stock/v1/ranking/updown-rate` | [link](overseas-stock/analysis/해외주식-041_해외주식 상승율_하락율.md) |
| 해외주식 신고/신저가 | `해외주식-042` | `GET` | `/uapi/overseas-stock/v1/ranking/new-highlow` | [link](overseas-stock/analysis/해외주식-042_해외주식 신고_신저가.md) |
| 해외주식 거래량순위 | `해외주식-043` | `GET` | `/uapi/overseas-stock/v1/ranking/trade-vol` | [link](overseas-stock/analysis/해외주식-043_해외주식 거래량순위.md) |
| 해외주식 거래대금순위 | `해외주식-044` | `GET` | `/uapi/overseas-stock/v1/ranking/trade-pbmn` | [link](overseas-stock/analysis/해외주식-044_해외주식 거래대금순위.md) |
| 해외주식 거래증가율순위 | `해외주식-045` | `GET` | `/uapi/overseas-stock/v1/ranking/trade-growth` | [link](overseas-stock/analysis/해외주식-045_해외주식 거래증가율순위.md) |
| 해외주식 거래회전율순위 | `해외주식-046` | `GET` | `/uapi/overseas-stock/v1/ranking/trade-turnover` | [link](overseas-stock/analysis/해외주식-046_해외주식 거래회전율순위.md) |
| 해외주식 시가총액순위 | `해외주식-047` | `GET` | `/uapi/overseas-stock/v1/ranking/market-cap` | [link](overseas-stock/analysis/해외주식-047_해외주식 시가총액순위.md) |
| 해외주식 기간별권리조회 | `해외주식-052` | `GET` | `/uapi/overseas-price/v1/quotations/period-rights` | [link](overseas-stock/analysis/해외주식-052_해외주식 기간별권리조회.md) |
| 해외뉴스종합(제목) | `해외주식-053` | `GET` | `/uapi/overseas-price/v1/quotations/news-title` | [link](overseas-stock/analysis/해외주식-053_해외뉴스종합(제목).md) |
| 해외주식 권리종합 | `해외주식-050` | `GET` | `/uapi/overseas-price/v1/quotations/rights-by-ice` | [link](overseas-stock/analysis/해외주식-050_해외주식 권리종합.md) |
| 당사 해외주식담보대출 가능 종목 | `해외주식-051` | `GET` | `/uapi/overseas-price/v1/quotations/colable-by-company` | [link](overseas-stock/analysis/해외주식-051_당사 해외주식담보대출 가능 종목.md) |
| 해외속보(제목) | `해외주식-055` | `GET` | `/uapi/overseas-price/v1/quotations/brknews-title` | [link](overseas-stock/analysis/해외주식-055_해외속보(제목).md) |

## [해외주식] 실시간시세

| API 명 | API ID | Method | URL | 문서 |
|---|---|---|---|---|
| 해외주식 실시간호가 | `실시간-021` | `POST` | `/tryitout/HDFSASP0` | [link](overseas-stock/realtime/실시간-021_해외주식 실시간호가.md) |
| 해외주식 지연호가(아시아) | `실시간-008` | `POST` | `/tryitout/HDFSASP1` | [link](overseas-stock/realtime/실시간-008_해외주식 지연호가(아시아).md) |
| 해외주식 실시간지연체결가 | `실시간-007` | `POST` | `/tryitout/HDFSCNT0` | [link](overseas-stock/realtime/실시간-007_해외주식 실시간지연체결가.md) |
| 해외주식 실시간체결통보 | `실시간-009` | `POST` | `/tryitout/H0GSCNI0` | [link](overseas-stock/realtime/실시간-009_해외주식 실시간체결통보.md) |

## [해외주식] 주문_계좌

| API 명 | API ID | Method | URL | 문서 |
|---|---|---|---|---|
| 해외주식 주문 | `v1_해외주식-001` | `POST` | `/uapi/overseas-stock/v1/trading/order` | [link](overseas-stock/trading-account/v1_해외주식-001_해외주식 주문.md) |
| 해외주식 정정취소주문 | `v1_해외주식-003` | `POST` | `/uapi/overseas-stock/v1/trading/order-rvsecncl` | [link](overseas-stock/trading-account/v1_해외주식-003_해외주식 정정취소주문.md) |
| 해외주식 예약주문접수 | `v1_해외주식-002` | `POST` | `/uapi/overseas-stock/v1/trading/order-resv` | [link](overseas-stock/trading-account/v1_해외주식-002_해외주식 예약주문접수.md) |
| 해외주식 예약주문접수취소 | `v1_해외주식-004` | `POST` | `/uapi/overseas-stock/v1/trading/order-resv-ccnl` | [link](overseas-stock/trading-account/v1_해외주식-004_해외주식 예약주문접수취소.md) |
| 해외주식 매수가능금액조회 | `v1_해외주식-014` | `GET` | `/uapi/overseas-stock/v1/trading/inquire-psamount` | [link](overseas-stock/trading-account/v1_해외주식-014_해외주식 매수가능금액조회.md) |
| 해외주식 미체결내역 | `v1_해외주식-005` | `GET` | `/uapi/overseas-stock/v1/trading/inquire-nccs` | [link](overseas-stock/trading-account/v1_해외주식-005_해외주식 미체결내역.md) |
| 해외주식 잔고 | `v1_해외주식-006` | `GET` | `/uapi/overseas-stock/v1/trading/inquire-balance` | [link](overseas-stock/trading-account/v1_해외주식-006_해외주식 잔고.md) |
| 해외주식 주문체결내역 | `v1_해외주식-007` | `GET` | `/uapi/overseas-stock/v1/trading/inquire-ccnl` | [link](overseas-stock/trading-account/v1_해외주식-007_해외주식 주문체결내역.md) |
| 해외주식 체결기준현재잔고 | `v1_해외주식-008` | `GET` | `/uapi/overseas-stock/v1/trading/inquire-present-balance` | [link](overseas-stock/trading-account/v1_해외주식-008_해외주식 체결기준현재잔고.md) |
| 해외주식 예약주문조회 | `v1_해외주식-013` | `GET` | `/uapi/overseas-stock/v1/trading/order-resv-list` | [link](overseas-stock/trading-account/v1_해외주식-013_해외주식 예약주문조회.md) |
| 해외주식 결제기준잔고 | `해외주식-064` | `GET` | `/uapi/overseas-stock/v1/trading/inquire-paymt-stdr-balance` | [link](overseas-stock/trading-account/해외주식-064_해외주식 결제기준잔고.md) |
| 해외주식 일별거래내역 | `해외주식-063` | `GET` | `/uapi/overseas-stock/v1/trading/inquire-period-trans` | [link](overseas-stock/trading-account/해외주식-063_해외주식 일별거래내역.md) |
| 해외주식 기간손익 | `v1_해외주식-032` | `GET` | `/uapi/overseas-stock/v1/trading/inquire-period-profit` | [link](overseas-stock/trading-account/v1_해외주식-032_해외주식 기간손익.md) |
| 해외증거금 통화별조회 | `해외주식-035` | `GET` | `/uapi/overseas-stock/v1/trading/foreign-margin` | [link](overseas-stock/trading-account/해외주식-035_해외증거금 통화별조회.md) |
| 해외주식 미국주간주문 | `v1_해외주식-026` | `POST` | `/uapi/overseas-stock/v1/trading/daytime-order` | [link](overseas-stock/trading-account/v1_해외주식-026_해외주식 미국주간주문.md) |
| 해외주식 미국주간정정취소 | `v1_해외주식-027` | `POST` | `/uapi/overseas-stock/v1/trading/daytime-order-rvsecncl` | [link](overseas-stock/trading-account/v1_해외주식-027_해외주식 미국주간정정취소.md) |
| 해외주식 지정가주문번호조회 | `해외주식-071` | `GET` | `/uapi/overseas-stock/v1/trading/algo-ordno` | [link](overseas-stock/trading-account/해외주식-071_해외주식 지정가주문번호조회.md) |
| 해외주식 지정가체결내역조회 | `해외주식-070` | `GET` | `/uapi/overseas-stock/v1/trading/inquire-algo-ccnl` | [link](overseas-stock/trading-account/해외주식-070_해외주식 지정가체결내역조회.md) |

## OAuth인증

| API 명 | API ID | Method | URL | 문서 |
|---|---|---|---|---|
| 접근토큰발급(P) | `인증-001` | `POST` | `/oauth2/tokenP` | [link](oauth/인증-001_접근토큰발급(P).md) |
| 접근토큰폐기(P) | `인증-002` | `POST` | `/oauth2/revokeP` | [link](oauth/인증-002_접근토큰폐기(P).md) |
| Hashkey | `Hashkey` | `POST` | `/uapi/hashkey` | [link](oauth/Hashkey_Hashkey.md) |
| 실시간 (웹소켓) 접속키 발급 | `실시간-000` | `POST` | `/oauth2/Approval` | [link](oauth/실시간-000_실시간 (웹소켓) 접속키 발급.md) |

