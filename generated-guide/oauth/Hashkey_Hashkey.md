## Hashkey

- **Source**: `OAuth인증.xlsx` / sheet `Hashkey`
- **API ID**: `Hashkey`
- **통신방식**: **REST**
- **HTTP Method**: `POST`
- **URL**: `/uapi/hashkey`
- **실전 Domain**: `https://openapi.koreainvestment.com:9443`
- **모의 Domain**: `https://openapivts.koreainvestment.com:29443`
- **메뉴 위치**: OAuth인증

### 개요

해쉬키(Hashkey)는 보안을 위한 요소로 사용자가 보낸 요청 값을 중간에 탈취하여 변조하지 못하도록 하는데 사용됩니다. 해쉬키를 사용하면 POST로 보내는 요청(주로 주문/정정/취소 API 해당)의 body 값을 사전에 암호화시킬 수 있습니다. 해쉬키는 비필수값으로 사용하지 않아도 POST API 호출은 가능합니다.

### 요청/응답 스펙 (Layout)

| 구분 | Element | 한글명 | Type | Required | Length | Description |
|---|---|---|---|---|---:|---|
| Request Header | content-type | 컨텐츠타입 | string | N | 40 | application/json; charset=utf-8 |
|  | appkey | 앱키 | string | Y | 36 | 한국투자증권 홈페이지에서 발급받은 appkey (절대 노출되지 않도록 주의해주세요.) |
|  | appsecret | 앱시크릿키 | string | Y | 180 | 한국투자증권 홈페이지에서 발급받은 appsecret (절대 노출되지 않도록 주의해주세요.) |
| Request Body | JsonBody | 요청값 | object | Y |  | POST로 보낼 body값  ex) datas = {     "CANO": '00000000',     "ACNT_PRDT_CD": "01",     "OVRS_EXCG_CD": "SHAA"  } |
| Response Header |  |  |  |  |  |  |
| Response Body | JsonBody | 요청값 | object | Y |  | 요청한 JsonBody |
|  | HASH | 해쉬키 | string | Y | 256 | [POST API 대상] Client가 요청하는 Request Body를 hashkey api로 생성한 Hash값 * API문서 > hashkey 참조 |
| Example |  |  |  |  |  |  |
| Request Example (Python) | { 	"ORD_PRCS_DVSN_CD": "02", 	"CANO": "계좌번호", 	"ACNT_PRDT_CD": "03", 	"SLL_BUY_DVSN_CD": "02", 	"SHTN_PDNO": "101S06", 	"ORD_QTY": "1", 	"UNIT_PRICE": "370", 	"NMPR_TYPE_CD": "", 	"KRX_NMPR_CNDT_CD": "", 	"CTAC_TLNO": "", 	"FUOP_ITEM_DVSN_CD": "", 	"ORD_DVSN_CD": "02" } |  |  |  |  |  |
| Response Example | {   "BODY": {     "ORD_PRCS_DVSN_CD": "02",     "CANO": "계좌번호",     "ACNT_PRDT_CD": "03",     "SLL_BUY_DVSN_CD": "02",     "SHTN_PDNO": "101S06",     "ORD_QTY": "1",     "UNIT_PRICE": "370",     "NMPR_TYPE_CD": "",     "KRX_NMPR_CNDT_CD": "",     "CTAC_TLNO": "",     "FUOP_ITEM_DVSN_CD": "",     "ORD_DVSN_CD": "02"   },   "HASH": "8b84068222a49302f7ef58226d90403f62e216828f8103465f900de0e7be2f0f" } |  |  |  |  |  |

