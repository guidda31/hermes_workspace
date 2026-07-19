# 자동매매 V2 — 핸드오프 & 프로젝트 상태

> 목적: 다음 세션(Claude/Hermes/GPT) 또는 사용자가 이 프로젝트를 **이어받을 때** 필요한
> 전체 상태·구조·안전경계·다음 작업을 한 곳에 정리한다. 코드/커밋에서 자명한 건 줄이고,
> 자명하지 않은 판단·경계·블로커를 명시한다.
>
> 기준: 브랜치 `llm-decision-layer`, 커밋 15개(HEAD `a980c43`), **618 tests 통과**, 59개 테스트 파일.

---

## 0. 한 문단 요약

Hermes(OpenAI OAuth로 GPT에 인증된 에이전트)가 **두뇌**로서 KRX 스윙 매매를 판단하고, 이
저장소는 Hermes가 호출하는 **결정론적 도구**(브리프·가드레일·감사·백테스트·페이퍼)를 제공한다.
저장소 코드는 LLM API를 호출하지 않는다. **현재 실거래는 연결되어 있지 않다** — 시스템은
"신호를 내고 기록"하거나 "모의(페이퍼) 체결"까지만 하며, 실제 돈이 나가는 경로는 비활성·격리
상태다. 전략 로직은 백테스트로 건전함이 확인됐으나, 모든 성과 숫자는 **3개 생존 우량주**
기반이라 일반화 불가. 진짜 검증은 외부 입력(KRX PIT 데이터, DART/뉴스 키)을 기다린다.

---

## 1. 아키텍처 원칙 (반드시 인지)

- **Hermes = LLM (별도 API 없음).** 판단 지능은 Hermes 런타임에 있다. 저장소는 `openai`/
  OpenRouter 키·SDK를 두지 않는다. 역전 구조: **GPT(=Hermes)가 코드를 호출**한다.
- **계층별 안전 격리** (grep으로 검증됨):
  - `swing_v2/llm/` — 네트워크는 `dart_transport.py`(주입식)에만, 나머지 clean. 주문 없음.
  - `swing_v2/paper/` — 실주문·네트워크·kis **전무**(AST 테스트로 증명). 순수 시뮬레이션.
  - `swing_v2/live/` — 실주문 코드 존재하나 **비활성·미연결**(아래 §4 참조).
- **실주문 원칙**(`docs/00-clean-slate.md`): 백테스트·페이퍼 검증 통과 + 명시 승인 전까지 비활성.
- **no-lookahead / PIT**: `t` 종가 신호 → `t+1` 시가 체결. 공시·뉴스는 발행시각 ≤ 신호일만.
  provenance.as_of ≤ signal_date. 이 규율은 코드에 구조적으로 강제돼 있다.

---

## 2. 모듈 맵

### `swing_v2/llm/` — LLM 판단 계층 (Hermes가 호출)
| 파일 | 역할 |
|---|---|
| `brief.py` | PIT 브리프 빌더 (미래 가격·공시·뉴스 배제, 발행시각 검증) |
| `decision.py` | Hermes 결정 스키마 검증 (환각 인용·유니버스밖 거부) |
| `guardrail.py` | 하드 한도 강제 (5종목·단일비중·신규진입차단·deny-by-default) |
| `signal_audit.py` | 불변·변조감지 신호기록 (주문 필드 0) |
| `prompt.py` | Brief→프롬프트 렌더 + Hermes 응답 파싱 (code-fence 강건) |
| `forward_runner.py` / `forward_cli.py` | 신호 전용 사이클 오케스트레이션 + CLI |
| `dart_disclosures.py`·`dart_corp_codes.py`·`dart_transport.py` | DART 공시 provider (주입식 transport, 키 유출 방지) |
| `news_provider.py` | source-무관 뉴스 provider (발행시각 tz 필수) |
| `providers.py` | 실 provider 키 배선 (`OPENDART_API_KEY`→DART provider) |
| `eligibility.py` | PIT 유니버스 → 가드레일 화이트리스트 |
| `order_bridge.py` | **inert**: 승인 신호→리스크검증 intent까지만 (제출 없음, AST 검증) |

### `swing_v2/backtest/` — 연구 백테스트 + 결과 계층
- 엔진: `backtest_engine.py`(멀티세션 러너), `engine.py`(체결 원시), `portfolio_*`,
  `entry_*`/`exit_*`, `close_time_candidates.py`, `daily_loss_guard.py`, `position_sizing.py`
- 결과: `metrics.py`(Sharpe/MDD/PF/승률…), `ledgers.py`(CSV 원장+run_summary), `cli.py`(실행)
- 검증: `scenarios.py`(비용 stress + walk-forward), `sensitivity.py`(파라미터 스윕)

### `swing_v2/paper/` — 페이퍼 트레이딩 (순수 시뮬레이션)
`session.py`(모의 체결+대사) · `ledger.py`(write-once+재시작복구+중복방지) ·
`report.py`(성과) · `kill_switch.py`(수동정지, fail-closed) · `runner.py`(조합) · `cli.py`(자율 진입점)

### `swing_v2/live/` — 실거래 계약 (⚠️ 비활성)
`production_execution.py`에 실제 KIS 주문 POST 코드가 **존재**하나, ① 게이트
`live_trading_enabled` 기본 False ② 실 세션·자격증명 미주입 ③ 클라이언트 미생성
④ 아무도 `submit_cash_limit_order`를 호출 안 함 — **네 가지 다 없어 비활성**. `gate.py`,
`intent.py`, `risk.py`, `audit.py` 등은 계약/감사용. 상세: `docs/*-phase1.md`.

### 기타
`kis*.py`(읽기전용 시세 수집), `krx_xlsx_normalizer.py`, `universe_metadata.py`(PIT 로더),
`backtest_data.py`(불변 스냅샷), `signals.py`(규칙 baseline), `contracts.py`(DailyBar).

---

## 3. 실행 명령 (CLI)

```bash
cd stock-trading-v2 && export PYTHONPATH=src   # 모든 명령 공통

# 백테스트 (RESEARCH 비-PIT 메타데이터로 3종목 baseline)
.venv/bin/python -m swing_v2.backtest.cli --snapshot data/snapshots/krx-research-2024-01-02_2026-07-17.json \
  --initial-cash 10000000 --output-dir /tmp/bt   # metrics 출력 + CSV 원장

# 페이퍼 트레이딩 (Hermes 루틴이 매 거래일 호출)
.venv/bin/python -m swing_v2.paper.cli render  --snapshot <SNAP> --signal-date <t> --symbols A,B --session-dir <D> --kill-switch <K>
.venv/bin/python -m swing_v2.paper.cli run     --snapshot <SNAP> --signal-date <t> --execution-date <t+1> --symbols A,B --eligible A,B --reply-file <hermes.json> --session-dir <D> --kill-switch <K> --initial-cash 10000000
.venv/bin/python -m swing_v2.paper.cli report  --session-dir <D>
.venv/bin/python -m swing_v2.paper.cli halt|resume --kill-switch <K> [--reason ...]

# forward observation (신호만, 주문 없음)
.venv/bin/python -m swing_v2.llm.forward_cli render|record ...

# 전체 테스트
.venv/bin/python -m unittest discover -s tests
```

---

## 4. 검증 현황 (정직한 상태)

| 항목 | 상태 | 근거 |
|---|---|---|
| no-lookahead 시간모델 | ✅ 구조적 강제 | 엔진 히스토리 절단·검증 |
| 비용 견고성 | ✅ | stress(슬리피지 3배): +46.4% vs base +49.9% |
| 파라미터 견고성 | ✅ | 민감도 스윕: 절벽 없음, 단조 위험/수익 트레이드오프 |
| regime 200일 | ✅ 근거 확인 | 완화(SMA100/60) 시 모든 위험조정 지표 악화 |
| 시간 안정성 | ⚠️ **경고** | walk-forward: 수익 대부분 마지막 구간 집중 |
| **생존편향** | ❌ **미해결** | 3개 생존 우량주뿐. PIT 데이터 필요 |
| 실전(forward) 증거 | ❌ 미착수 | 실 데이터·키 필요 |

**⚠️ 모든 성과 숫자(+49.9% 등)를 실거래 기대수익으로 읽으면 안 된다** (doc-04 §9).
3-생존종목 + 최근 상승장 집중이라 일반화 불가.

---

## 5. 하드 블로커 (사용자 조치 필요)

1. **KRX Data Marketplace 계정/라이선스** → 과거 PIT 유니버스 데이터. 비로그인 차단이라
   코드로 우회 불가. 확보 시 `docs/07` 절차로 로더에 임포트 → 생존편향 제거. (소비측 코드 준비 완료)
2. **opendart API 키**(`OPENDART_API_KEY`) → 실 공시. `llm/providers.py`가 키만 있으면 바로 배선.
3. **뉴스 소스 + 키** 선정 → `news_provider`에 fetch 주입. (어댑터 준비 완료, 소스 미정)
4. **Hermes cron/루틴 등록** → 자율 매일 실행. 현재 비활성(로드맵 게이트, 승인 후).
5. **실거래 승인** → §4 검증 통과 + 명시 승인 전까지 금지.

---

## 6. 다음 작업 우선순위

- **지금 가능(외부 의존 없음)**: 위생(죽은 테스트 `tests/backtest/test_backtest_engine.py`
  구API·`__init__.py` 없어 discover가 스킵 — 정리), 재현성 체크(doc-04 §9-1), 유동성/참여율
  파라미터화(현재 `signals.passes_liquidity_filter`에 10억 하드코딩 — regime처럼 config화하면 스윕 가능).
- **외부 확보 후**: PIT 데이터 임포트 → 생존편향 없는 재검증 / DART·뉴스 키 → 실 forward 가동 →
  페이퍼 누적 → (승인) 소액 실거래.
- **판단**: 3-생존종목 데이터에서 짜낼 검증은 사실상 완료. **진짜 진전은 #1·#2 외부 입력에서 나온다.**

---

## 7. 개발 컨벤션 (코드 이어쓸 때)

- 스타일: `from __future__ import annotations`, frozen dataclass, 엄격 `type(x) is not T`
  (isinstance 아님 — bool-as-int 등 거부), Decimal(금액), canonical JSON(sort_keys·compact),
  fail-closed `raise ValueError`, 짧은 docstring.
- **TDD**: 테스트 먼저(RED) → 구현(GREEN). 커밋마다 전체 `unittest discover` 통과 유지.
- 신규 계층은 **안전 격리 AST 테스트**를 붙인다(주문/네트워크 import 없음 증명) — `paper/`·`order_bridge` 참고.
- **커밋 전 필수**: `unittest discover` 통과, `compileall` OK, `git diff --check`(whitespace),
  스테이징에 `.env`/시크릿/`data/` 없음 확인.
- **gitignore 주의**: 프로젝트 `.gitignore`가 `*.md`·`*.json`·`data/`·`.env` 제외 → **docs와
  스냅샷은 커밋 안 됨**(로컬 전용). 이 핸드오프 문서도 로컬에만 있음.

---

## 8. 이번 세션 커밋 이력 (요약)

`e66a4aa` 소스 baseline + LLM 계층 → `3c3a00b` decide-seam(prompt/CLI/corp_code) →
`8d1d1fe` DART transport·news·order-bridge → `9d90d7e` 백테스트 지뢰 2건(stale-mark·gap-up) →
`fa93ef2`~`ffa757c` 페이퍼 계층(session·runner·CLI) → `949735b` 백테스트 결과계층(metrics·ledgers·CLI) →
`13cb624` scenarios·providers → `40e64b4` regime 파라미터화 → `a980c43` 파라미터 민감도.

(브랜치 `llm-decision-layer`는 아직 `main`에 병합·푸시 안 됨. 원격 미설정.)
