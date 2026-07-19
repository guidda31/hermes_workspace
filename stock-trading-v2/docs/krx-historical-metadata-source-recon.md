# KRX 시점별 유니버스 메타데이터 원본 조사

조사일: 2026-07-18 (HTTP 응답의 `Date`는 아래 표에 그대로 기록).  
범위: KRX 공식 원본만 대상으로, 과거 시점의 상장 유가증권/상품 구분과 ETF 분류, 관리·매매정지 상태를 신뢰성 있게 확보할 수 있는지를 확인했다. 현재 FDR 종목목록은 조사·대안 어느 쪽에도 사용하지 않았다.

## 결론

**공식 KRX 후보 서비스는 확인했지만, 비로그인 상태에서 과거 메타데이터 행 또는 날짜가 든 파일을 실제로 내려받지는 못했다.** KRX Data Marketplace의 공개 랜딩 페이지는 서비스와 관련 메뉴를 명시하지만, 아래의 실제 통계/이력 화면은 모두 로그인 또는 회원가입을 요구한다. 따라서 이 조사만으로는 과거 구간에 넣을 수 있는 `provenance.as_of`가 증명된 원본 파일도, 익명 접근 가능한 다운로드 API도 확인되지 않았다.

현 시점에서 안전한 상태는 **historical metadata dataset 없음**이다. 기존 `docs/krx-universe-metadata.md`와 구현의 `provenance.as_of <= signal_date` 제약을 우회하거나, 현재 목록을 과거로 역적용해서는 안 된다.

## 실측한 공식 KRX 증거

`Date`는 각 서버가 회신한 값이며, 상태 코드 200만으로 데이터 접근 가능을 의미하지 않는다.

|용도 / KRX가 노출한 화면명|정확한 URL|실제 HTTP 결과|확인된 내용과 한계|
|---|---|---|---|
|Data Marketplace 랜딩|<https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd>|`200`, `text/html; charset=utf-8`, 588,336 bytes, `Date: Fri, 17 Jul 2026 15:48:10 GMT`|페이지 제목은 `KRX | KRX Data Marketplace`. 메뉴 HTML에 주식 `전종목 기본정보` (`MDC0201020201`), ETF `전종목 기본정보` (`MDC0201030104`), `ETF 상세검색` (`MDC020103010901`), `매매거래정지 내역(개별종목)` (`MDC02020602`), `관리종목 지정 내역(개별종목)` (`MDC02020702`)이 존재한다. 랜딩 자체는 원본 행/파일이 아니다.|
|주식 전종목 기본정보 화면|<https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020201>|`200`, `text/html;charset=utf-8`, 407 bytes, `Date: Fri, 17 Jul 2026 15:48:32 GMT`|본문 JavaScript가 정확히 `로그인 또는 회원가입이 필요합니다.`라고 알리고 로그인 URL로 이동시킨다. 비로그인 데이터 응답 없음.|
|ETF 전종목 기본정보 화면|<https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201030104>|`200`, `text/html;charset=utf-8`, 407 bytes, `Date: Fri, 17 Jul 2026 15:48:32 GMT`|동일한 로그인 차단. ETF의 레버리지/인버스/해외지수 필드가 실제 출력에 존재하는지는 이 상태에서 검증하지 못했다.|
|ETF 상세검색 화면|<https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC020103010901>|`200`, `text/html;charset=utf-8`, 407 bytes, `Date: Fri, 17 Jul 2026 15:48:32 GMT`|동일한 로그인 차단. 메뉴 명칭만으로 세부 분류 필드나 과거 일자 파라미터를 추정하지 않는다.|
|매매거래정지 개별종목 이력 화면|<https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC02020602>|`200`, `text/html;charset=utf-8`, 407 bytes, `Date: Fri, 17 Jul 2026 15:48:32 GMT`|공식 메뉴 HTML이 `매매거래정지 내역(개별종목)`으로 명시하지만, 로그인 차단으로 이력의 시작/종료일·다운로드 형식은 미검증.|
|관리종목 지정 개별종목 이력 화면|<https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC02020702>|`200`, `text/html;charset=utf-8`, 407 bytes, `Date: Fri, 17 Jul 2026 15:48:32 GMT`|공식 메뉴 HTML이 `관리종목 지정 내역(개별종목)`으로 명시하지만, 로그인 차단으로 이력의 시작/해제일·다운로드 형식은 미검증.|
|로그인|<https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd?locale=ko_KR>|`200`, `text/html; charset=utf-8`, 20,436 bytes, `Date: Fri, 17 Jul 2026 15:48:44 GMT`|페이지 제목은 `로그인 - KRX | KRX Data Marketplace`. 실제 통계 화면의 접근 전제다.|
|공개 홈페이지 이용약관|<https://data.krx.co.kr/contents/MDC/INFO/informationController/MDCINFO003.cmd>|`200`, `text/html; charset=utf-8`, 444,386 bytes, `Date: Fri, 17 Jul 2026 15:48:52 GMT`|약관은 회원을 계정을 가진 서비스 이용자로 정의하고, 마켓데이터 구매/이용 시 별도 `마켓데이터 이용약관` 준수를 요구하며 미동의 시 구매·이용이 제한될 수 있다고 명시한다. 데이터 라이선스/구입 안내 화면은 비로그인에서 같은 407-byte 로그인 차단을 반환했다.|
|KIND 기업목록 초기 화면 (보조 확인)|<https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage>|`200`, `text/html; charset=UTF-8`, 89,994 bytes, `Date: Fri, 17 Jul 2026 15:48:08 GMT`|제목 `대한민국 대표 기업공시채널 KIND`의 공개 초기 HTML은 확인했지만, 이 요청에서 과거 스냅샷 또는 유니버스 분류 파일을 얻지 못했다. 이 조사는 이를 과거 유니버스 원본으로 승인하지 않는다.|

비로그인으로 추측한 AJAX 요청도 허용 원본이 아니었다. 예를 들어 `POST https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd`에 알려진 전종목 통계 BLD 후보와 과거 거래일을 보냈을 때 `400`, `text/html; charset=utf-8`, 6 bytes, 본문 `LOGOUT`이었다 (`Date: Fri, 17 Jul 2026 15:47:02 GMT`). 세션/권한 우회나 비공개 엔드포인트 추측은 진행하지 않는다.

## 요구 필드별 판정

|필요 정보|공식 후보|현재 판정|
|---|---|---|
|과거 상장 종목 스냅샷, 시장/상품 유형|Data Marketplace 주식·ETF `전종목 기본정보`|후보 화면은 확인, 과거 날짜 선택·반환 컬럼·익명 다운로드는 **미검증/차단**.|
|ETF / ETN 구분|상품별 기본정보 화면과 Data Marketplace 메뉴 구조|ETF 화면은 확인했으나 ETN 화면/반환 컬럼은 이번 권한에서 검증하지 못했다. **미검증**.|
|ETF 레버리지·인버스·해외지수|`ETF 상세검색`|후보 화면만 확인. 해당 분류가 명시 필드인지, 과거 시점에도 재현되는지는 **미검증**. 명칭/코드 휴리스틱으로 대체 금지.|
|관리종목 및 매매거래정지|각각 `개별종목` 이력 화면|공식 이력 메뉴는 확인. 실제 사건일, 상태 종료 처리, 일자 기준은 **미검증/차단**.|
|실제 공개/기준일 (`as_of`)|다운로드 파일/응답의 날짜·발행시각·변경 이력|익명으로 날짜가 든 데이터 파일을 받지 못했으므로 **증명 불가**. 단순히 요청한 `trdDd` 또는 내려받은 날을 과거 `as_of`로 쓰면 안 된다.|

## 최소 안전 획득 계획

1. **정식 접근을 확보한다.** 프로젝트용 KRX Data Marketplace 계정을 만들고, 데이터 상품/마켓데이터 이용약관·라이선스가 해당 저장 및 연구 사용을 허용하는지 담당자가 확인한다. 자격증명은 저장소·문서·로그에 넣지 않는다.
2. **먼저 작은 권한 검증을 한다.** 승인된 세션에서 한 날짜와 소수 종목만 대상으로 각 화면의 제공 범위·날짜 선택·CSV/XLS 다운로드·필드명을 확인한다. 원본 행, 응답 헤더, 메뉴 ID, 요청 파라미터, 조회/발행 시각을 보존한다. 데이터가 실제로 반환되고 날짜가 명시된 뒤에만 `data/research/krx/`(현재 `.gitignore`로 무시됨)에 작은 샘플을 저장하고 `sha256sum`을 계산한다.
3. **시점 증명 기준을 문서화한다.** 각 파일은 `source URL/menu ID`, 원본 파일명, 원본에 표시된 기준일 또는 공시/발행일, 조회 UTC 시각, SHA-256, 라이선스/권한 근거를 manifest에 기록한다. 단, 원본에 과거 당시 공개됐음을 보이는 날짜가 없으면 그 파일은 그 과거 신호일의 `provenance.as_of` 증거가 아니다.
4. **필드 매핑을 검증한다.** 원본 컬럼으로만 `STOCK/ETF/ETN/PREFERRED/SPAC`, ETF 노출 및 세 제외 플래그, 관리/정지 사건의 유효기간을 매핑한다. ETF 분류나 상태 해제 규칙이 원본에 명시되지 않으면 해당 플래그/노출은 `UNKNOWN`으로 남기고 유니버스에서 제외한다.
5. **수집 전략을 둘로 나눈다.** 앞으로의 연구는 매 거래일 장 마감 후(그리고 다음 신호 전에) 허용된 원본을 캡처해 자체 dated archive를 만든다. 이미 지난 구간은 KRX가 제공하는 **발행일이 검증 가능한** 과거 파일/이력만 사용한다. 단지 오늘 조회가 과거일 선택을 받는다는 사실은 당시 이용 가능성을 증명하지 못한다.
6. **정규화·검증 후에만 로드한다.** 검증된 원본별로 기존 10개 필드 포맷에 변환하여 `source`, `version`(원본 파일명/조회 버전), `content_hash`, 원본이 증명한 `as_of`를 넣는다. `load_universe_metadata`와 전체 테스트를 실행하고, `provenance.as_of > signal_date` 또는 필수 분류 결측은 거부 상태를 유지한다.

## 권고되는 다음 행동

**KRX 계정/라이선스 승인을 먼저 받고, 로그인 세션에서 단일 과거 일자에 대한 “주식 기본정보 + ETF 상세검색 + 관리/정지 이력”의 실제 파일과 날짜 컬럼을 검사하라.** 그 검증에서 날짜가 표시된 합법적 파일 하나라도 내려받을 수 있을 때에만 작은 무시 경로 샘플과 SHA-256을 추가한다. 그 전에는 실제 historical metadata fixture나 백테스트 유니버스를 만들지 않는다.
