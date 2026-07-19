# KRX 시점별(PIT) 유니버스 획득·임포트 절차 (step 4)

> 상태: **하드 블로커 — 데이터 미확보**. 과거 시점별 상장/ETF분류/관리·정지 이력은 KRX
> Data Marketplace 비로그인 차단으로 받을 수 없다(`docs/krx-historical-metadata-source-recon.md`
> 참조). 이 문서는 **계정/라이선스 확보 후** 그 데이터를 기존 코드에 넣는 구체 절차다.
> 코드로 우회할 수 없는 단계이며, 현재 상태에서 과거 유니버스를 만들면 안 된다.

## 왜 코드가 아니라 데이터가 문제인가
- 저장소에는 이미 소비 측이 준비돼 있다: `universe_metadata.py`의 `select_eligible_universe`
  (deny-by-default, `provenance.as_of ≤ signal_date` 강제)와 `llm/eligibility.py`의
  `eligible_symbols_as_of(...)`가 가드레일의 `eligible_symbols`를 만든다.
- 빠진 건 **당시 공개됐음이 증명되는 과거 원본**뿐이다. "오늘 조회되니 그때도 있었다"는 금지.

## 획득 절차 (담당자 수행, 저장소 밖)
1. **KRX Data Marketplace 계정 + 마켓데이터 이용약관/라이선스**를 확보한다. 자격증명은
   저장소·문서·로그·`.env`에 넣지 않는다.
2. 승인된 세션에서 **단일 과거 일자·소수 종목**으로 각 화면의 실제 반환 컬럼·날짜 파라미터·
   다운로드 형식을 검증한다: 주식 전종목 기본정보, ETF 상세검색, 관리종목·매매거래정지 이력.
3. **발행일이 표시된** 원본 파일만 받는다. 원본 파일명, 원본 표시 기준일/공시일, 조회 UTC 시각,
   `sha256sum`, 라이선스 근거를 manifest에 남긴다. 날짜가 없으면 그 파일은 그 신호일의
   `provenance.as_of` 증거가 아니다.
4. 원본은 `data/research/krx/`(현재 `.gitignore`로 무시됨) 아래 **불변 보관**한다.

## 임포트 절차 (데이터 확보 후, 저장소 안)
1. 검증된 원본 컬럼만으로 각 종목을 `STOCK/ETF/ETN/PREFERRED/SPAC`, ETF 노출(지수/섹터/
   레버리지/인버스/해외지수), 관리·정지 **유효기간(effective_from/effective_to)**, 그리고
   원본이 증명한 `provenance.as_of`로 매핑한다. 분류가 원본에 명시 안 되면 `UNKNOWN`으로
   두고 유니버스에서 제외한다(휴리스틱 금지).
2. 매핑 결과를 기존 `UniverseMetadataSnapshot` 형식으로 만들고 `content_hash`·`source`·
   `version`(원본 파일명/조회 버전)·`as_of`를 채운다.
3. 원본→메타데이터 매퍼에 **커버리지/감사 테스트**를 붙인다: 메타데이터 공백이 있는 종목은
   그 신호일에 무조건 deny되는지, `provenance.as_of > signal_date`가 거부되는지, 구간 중첩·
   미래일 provenance가 없는지 검증한다.
4. `load_universe_metadata`와 전체 테스트를 돌린다. 이후 `llm/eligibility.py`가 이 스냅샷으로
   자동으로 forward/과거 eligibility를 판정한다 — 추가 배선 불필요.

## 그때까지의 안전한 기본값
- **Forward(미래) 관측**: 현재 KRX 메타데이터(`as_of=2026-07-18`, forward-only)로 판정 가능.
  즉 앞으로의 실거래일 신호는 지금도 정상 동작한다(정직한 검증 경로).
- **과거 구간**: `as_of > 과거 signal_date`이므로 전부 deny된다. 이는 버그가 아니라
  PIT 안전장치다. 광범위 과거 백테스트는 이 데이터가 감사를 통과하기 전까지 하지 않는다.

## 완료 기준
- [ ] 발행일이 검증된 KRX 원본이 라이선스 근거와 함께 불변 보관되고 sha256이 기록됨.
- [ ] 원본→`UniverseMetadataSnapshot` 매퍼와 커버리지/감사 테스트가 통과.
- [ ] `eligible_symbols_as_of`가 과거 신호일에 대해 생존편향 없이 자격 종목을 산출.
- [ ] 독립 검토로 생존편향·미래일 provenance·구간 오류가 없음을 확인.
