# Hermes 자율 Forward-Observation 운용 Runbook (신호 전용, 주문 없음)

> 상태: **운영 규약 + 루틴 준비**. Hermes가 매 거래일 스스로 실행한다. 실주문·성과주장 없음.
> Hermes가 두뇌, 저장소 도구가 손발. 이 문서가 실제 forward 운용의 단일 실행 기준이다.

## 왜 forward observation인가 (핵심)
backtest의 성과는 **시장 베타 + 생존편향**이 대부분이고, 규칙 momentum의 **종목선별 알파는
사실상 0**임이 `forward_eval`로 확인됐다(모든 horizon에서 엣지 ≈ 0). 따라서 이 프로젝트의
진짜 질문은 **"Hermes가 뉴스·공시를 해석해 지수 대비 엣지를 만드는가"** 이고, 그 답은
**생존편향-free한 forward observation 누적**으로만 나온다. 이 runbook이 그 누적 루프다.

## 안전 불변식
- **주문 0.** 이 루프는 신호를 기록하고 채점할 뿐, KIS 주문·잔고·계좌변경 API를 호출하지 않는다.
- **PIT.** `t` 종가까지의 정보만. 공시·뉴스는 발행시각 ≤ `t`. 미래 데이터 금지.
- 어떤 단계든 도구가 오류를 내면 **중단·보고**(추정 진행 금지).

## 매 거래일 절차

### 0. 세션 확인 & 데이터 수집 (읽기 전용)
실제 KRX 거래일에만 실행. `signal_date` = 방금 마감한 세션.
```bash
cd stock-trading-v2 && export PYTHONPATH=src
# 유니버스 30종목 + KOSPI의 signal_date까지 일봉을 수집(재개 가능, 주문 아님)
.venv/bin/python -m swing_v2.kis_snapshot_collector --symbol 005930:STOCK ... \
  --start <시작> --end <signal_date> --output data/kis-live/<...> --delay-seconds 0.25
# 수집분으로 signal_date 기준 스냅샷 재조립 (KOSPI 워밍업 200+세션 포함)
#   → data/snapshots/live-<signal_date>.json  (조립 스크립트는 universe30 빌드와 동일 방식)
```

### 1. 브리프 렌더 (저장소 → Hermes)
```bash
.venv/bin/python -m swing_v2.llm.forward_cli render \
  --snapshot <SNAP> --signal-date <signal_date> --symbols <30종목> [--held <보유>]
```
출력 프롬프트를 Hermes가 읽는다. (DART/뉴스는 `OPENDART_API_KEY` 설정 시 `llm/providers.py`로 연결.)

### 2. 판단 (Hermes 두뇌)
프롬프트 규칙대로 **JSON 배열만** 산출: 제공된 종목·evidence_id만, BUY는 conviction·target_weight>0,
브리프 밖 정보 가정 금지(PIT). 조치 불필요 시 `[]`.

### 3. 신호 기록 (불변 감사)
```bash
.venv/bin/python -m swing_v2.llm.forward_cli record \
  --snapshot <SNAP> --signal-date <signal_date> --symbols <30종목> \
  --eligible <자격종목> --model-id hermes/openai-oauth \
  --reply-file <hermes.json> --output data/forward-records/signal-<signal_date>.json
```
→ 스키마·환각·유니버스·가드레일 강제 후 통과분만 **write-once** 기록. 주문 필드 0.

### 4. (주기적) 채점 — 누적된 신호가 실제로 맞았나
며칠~몇 주 뒤 forward 결과가 쌓이면:
```bash
.venv/bin/python -m swing_v2.llm.forward_cli score \
  --records-dir data/forward-records --snapshot <최신 SNAP> --forward-sessions 20
# 출력: scored/signal, hit_rate, pick_return, market_return, edge
```
**edge = 픽수익 − 시장수익.** forward 창이 안 지난 기록은 자동 스킵되므로 반복 실행 가능.

## Hermes가 지켜야 할 가드레일
- 비거래일엔 실행 안 함. KIS 주문·잔고 엔드포인트 호출 금지(수집은 읽기전용만).
- 감사기록 덮어쓰기 금지(write-once). 같은 날 재실행은 새 경로.
- 실거래 활성화는 이 루프 범위 밖 — 별도 명시 승인 필요.

## 루틴/스케줄 등록 (준비됨 — **활성화는 승인 후**)
현재 자동 cron **비활성**. 매일 자동 실행하려면 아래를 Hermes/OpenClaw 루틴으로 등록한다:
- **트리거**: KRX 장 마감 후(예: 평일 KST 16:00), 거래일에만.
- **동작**: 위 0→1→2→3 순서. Hermes 에이전트가 2단계(판단)를 담당.
- **주간**: 매 금요일 4단계(score) 실행 → 누적 edge 리포트.
- **가드**: 실패 시 중단·알림. 실주문 절대 없음.
등록 자체(cron/routine 생성)는 **자율 반복 실행을 시작**하는 행위이므로 사용자 명시 승인 뒤에만 한다.

## 성공 기준 (무엇을 보면 되나)
- 수개월 누적 후 `score`의 **edge가 유의하게 양(+)** 이면 → Hermes가 규칙이 못 만든 **선별 알파**를
  더한다는 첫 증거(생존편향-free). edge ≈ 0이면 → LLM도 베타만 탄다는 정직한 결론.
- 이 판정이 **paper → 소액 실거래**로 갈지 말지의 근거가 된다. edge 없이 실거래로 가지 않는다.
