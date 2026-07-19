# Hermes 자율 Forward-Observation 루틴 (신호 전용, 주문 없음)

> 상태: **운영 규약(runbook)**. 이 루틴은 매 거래일 장 마감 후 Hermes가 스스로 실행한다.
> 실주문·성과주장은 하지 않는다. Hermes가 두뇌, 저장소 도구가 손발이다(`docs/05` 참조).

## 목적
매 실제 거래일에 대해 PIT 브리프를 만들고, Hermes가 판단하고, 하드 가드레일을 거쳐 **불변 신호 감사기록**을 남긴다. 이 기록을 다음날 실제 결과와 대사해 LLM 판단 품질을 축적한다(정직한 검증 경로). 주문은 절대 내지 않는다.

## 선행 조건 (하나라도 불충족 시 중단·보고)
1. `signal_date`가 **실제 KRX 거래일**이어야 한다(주말·공휴일·비거래일이면 실행 안 함).
2. `signal_date`까지의 봉을 담은 **로컬 스냅샷**이 있어야 한다. 없으면 먼저 KIS 읽기 전용 수집기로 확보한다(주문·계좌 엔드포인트 금지).
3. eligibility는 **provenance.as_of ≤ signal_date** 인 KRX 메타데이터로만 판정한다. 과거 구간은 PIT 메타데이터 부재로 막혀 있다(현재 as_of=2026-07-18, forward-only).

## 매 거래일 절차 (Hermes가 수행)

### 1. 브리프 렌더
```bash
cd stock-trading-v2
PYTHONPATH=src .venv/bin/python -m swing_v2.llm.forward_cli render \
  --snapshot <스냅샷경로> --signal-date <YYYY-MM-DD> \
  --symbols <종목,종목,...> [--held <보유종목>] [--new-entries-blocked]
```
출력된 프롬프트를 읽는다.

### 2. 판단 (Hermes 두뇌)
프롬프트의 규칙을 지켜 **JSON 배열만** 산출한다:
- 제공된 종목·evidence_id만 사용(환각 인용 금지).
- BUY는 conviction>0·target_weight>0, SELL은 전량청산(target_weight=0), HOLD/SELL은 보유 종목만.
- 브리프 밖 정보를 가정하지 않는다(PIT). 조치가 불필요하면 `[]`.

### 3. 기록 (가드레일 + 감사)
Hermes의 JSON을 파일(또는 stdin)로 넘긴다:
```bash
PYTHONPATH=src .venv/bin/python -m swing_v2.llm.forward_cli record \
  --snapshot <스냅샷경로> --signal-date <YYYY-MM-DD> --symbols <...> \
  --eligible <자격종목> --model-id hermes/openai-oauth \
  --reply-file <hermes응답.json> --output <감사기록경로>.json
```
동일 스냅샷+날짜로 **동일 브리프가 재구성**되므로 브리프를 따로 저장할 필요가 없다. 도구가 스키마·환각·유니버스·5종목·단일비중·신규진입차단을 강제하고, 통과분만 불변 기록에 남긴다.

### 4. 보고
`admitted`/`rejected` 요약을 남긴다. **주문은 하지 않는다.**

## 안전 가드레일 (Hermes가 반드시 지킴)
- 비거래일에는 브리프·기록을 만들지 않는다(캘린더/스냅샷 없으면 중단).
- 어떤 단계든 도구가 오류를 내면 **중단하고 보고**한다(추정으로 진행 금지).
- KIS **주문·잔고·계좌변경** 엔드포인트를 호출하지 않는다(수집은 읽기 전용만).
- 감사기록을 덮어쓰지 않는다(write-once). 같은 날 재실행은 새 경로를 쓴다.
- 실거래 활성화는 이 루틴 범위 밖이며 별도 명시 승인이 필요하다.

## 스케줄링
- 현재 **자동 cron 비활성**. 매일 자동 실행은 별도 승인 후 Hermes 루틴/cron으로 등록한다(로드맵 Phase 5 게이트, `.hermes/plans/...forward-observation-and-pit-plan.md`).
- 그 전까지는 이 절차를 **수동 트리거**로 실행한다.

## 확장 훅 (붙으면 자동 반영)
- DART 공시: `make_dart_disclosure_provider`(transport·키·corp_code 맵 주입) → record 단계에 provider 연결.
- 뉴스: `make_news_provider`(source fetch 주입) → 동일.
- 두 provider 모두 발행시각 PIT 필터는 브리프 빌더가 강제한다.
