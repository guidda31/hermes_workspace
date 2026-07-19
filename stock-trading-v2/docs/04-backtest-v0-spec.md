# 자동매매 V2 — 백테스트 v0 최소·재현 가능한 명세

> 범위: KRX 개별 보통주와 일반 지수·섹터 ETF의 **롱 전용** 일봉 스윙(10~20 거래일) 전략을 검증한다. 이는 연구용 시뮬레이터이며 주문 API·실거래·장중 체결 모델은 포함하지 않는다.
>
> **검증 전 수치 가설 표기:** 이 문서의 `가설(H)` 값은 아직 실데이터 검증·브로커 비용 대사를 거치지 않은 초기값이다. 코드에는 반드시 설정값으로만 넣고, 결과 보고서에 사용값을 함께 남긴다. `확정`이라고 표시한 시간·상태 전이 규칙은 v0 재현성 계약이다.

## 1. 실행 입력과 재현성 고정물

한 번의 실행(`run_id`)은 아래 입력을 불변 스냅샷으로 저장한다. 같은 스냅샷·설정·코드 버전이면 같은 결과가 나와야 한다.

- `strategy_id`, 코드 Git commit, Python/package lock hash
- 시작/종료 거래일, 초기 현금(`initial_cash`), 기준 시장지수 심볼, 종목 유니버스의 **각 거래일 유효 이력**
- 정규화된 일봉 스냅샷의 파일 hash와 `data_timestamp`; 가격은 조정 전 원시 OHLCV, `trading_value=close×volume` 프록시임을 명시
- 신호 파라미터, 보유·청산 파라미터, 비용 모델 파라미터, 거래 가능 판정 규칙
- 정렬 규칙(날짜 오름차순, 같은 날짜의 심볼 오름차순)과 난수 seed(현재 난수 미사용이어도 기록)

입력은 `03-backtest-data-contract.md`의 `DailyBar` 계약을 통과해야 한다. 일봉의 날짜 중복, 역순, 심볼 불일치, `low <= open/close <= high` 위반, 0 이하 가격, 비유한 값은 **유효하지 않은 바**다.

## 2. 확정 시간 모델: `t` 종가 신호 → `t+1` 시가 체결

### 2.1 관측 가능 정보

**확정:** 거래일 `t`의 의사결정은 `t` 장 마감 뒤 확정된 데이터만 사용한다. 자산 신호에는 해당 자산의 `t`까지의 바, 시장 필터에는 기준지수의 `t`까지의 종가만 사용할 수 있다. `t+1`의 OHLCV·상태·종가는 신호, 순위, 수량 결정에 사용할 수 없다.

**확정:** `t`에 생성한 주문 의도(intent)는 오직 해당 심볼의 다음 실제 거래일 `t+1`에 한 번만 체결을 시도한다. `t` 종가 체결, `t+2` 이후 이월 체결, 종가·고가·저가를 이용한 당일 체결은 v0에서 금지한다.

### 2.2 일자별 상태 전이와 순서

각 캘린더 거래일 `d`에서 다음 순서를 고정한다.

1. `d` 시가 전에, 전일 `d-1` 종가에 생성된 대기 주문만 처리한다. 청산 주문을 먼저 처리하고, 그 다음 진입 주문을 처리한다.
2. 유효하고 거래 가능한 `d` 바의 시가로 체결 가능한 주문을 비용 모델에 전달한다. 현금·포지션·체결 원장을 갱신한다.
3. `d` 종가에서 보유 포지션을 종가로 평가하여 NAV를 산출한다. 단, 유효한 종가가 없으면 마지막 유효 종가로 평가하되 `stale_mark=true`를 남긴다.
4. `d` 종가 확정 후 신호·청산 조건·일 손실 차단을 평가하고, 다음 거래일 `d+1`용 주문 의도를 만든다.

따라서 체결된 진입의 손익은 진입일 시가 이후의 움직임부터 반영되며, 그 날 종가 신호가 진입 체결에 영향을 주지 않는다.

## 3. 신호, 후보 순위와 포지션 제약

### 3.1 현재 엔진을 v0 입력으로 고정

`d=t` 종가 기준 아래 모두 참인 경우만 신규 후보로 한다.

- 시장 risk-on: 기준지수 `close_t > SMA200_t` 이고 `SMA50_t > SMA200_t`.
- 유동성: 최근 20개 바가 모두 거래 가능이고, 최신 종가가 1,000원 이상이며, `mean(close×volume, 최근 20일) >= 1,000,000,000원`.
- 모멘텀·돌파: `close_t > SMA20_t > SMA60_t`, `close_t > max(close[t-20:t-1])`, `close_t > close[t-60]`.

위의 20·60·50·200일 창과 1,000원·10억원 기준은 **가설(H)** 이다. 단, 구현된 신호 엔진과의 최초 비교를 위해 v0 기본값으로 기록한다. 바가 필요한 길이보다 부족하면 해당 조건은 거짓이다.

후보는 `breakout_strength = close_t / max(close[t-20:t-1]) - 1` 내림차순, 이어서 `momentum_60 = close_t / close[t-60] - 1` 내림차순, 마지막으로 `symbol` 오름차순으로 정렬한다. 같은 심볼의 기존 포지션·대기 진입 주문이 있으면 후보에서 제외한다.

### 3.2 포지션·현금 제약

- **확정:** 롱 포지션만 허용, 심볼당 최대 1개, 동시 보유 최대 5개, 신용·공매도·분할매수·부분청산은 v0 범위 밖이다.
- **가설(H):** 신규 포지션의 위험 예산은 주문 직전 NAV의 1% (`risk_per_position=0.01`), 포지션 명목가치 상한은 NAV의 20% (`max_position_notional_pct=0.20`)로 둔다.
- **확정:** 수량은 `floor(min(위험예산/(체결가-초기손절가), 명목상한/체결가, 가용현금/(체결가+매수단위비용)))`이며 1주 미만이면 주문을 취소한다. 매수 단위비용은 비용 모델이 산출한다. `t` 종가로 만든 `expected_fill_price`와 `expected_cash_cost`는 수량·현금 예약을 위한 추정치일 뿐이다. 실제 `t+1` 시가 체결가와 비용은 공통 체결 모델이 권위 있게 산출하며, 실제 현금 차감액이 실행 가능 현금 안이면 추정치와 달라도 체결한다. 초과하면 `CANCELED_UNFILLED`/`CASH_UNAVAILABLE`으로 한 번만 취소한다.
- **가설(H):** 초기손절가는 진입 체결가의 95% (`initial_stop_pct=0.05`)로 고정한다. 이로써 1% 위험 수량을 계산할 수 있으나, 갭·비체결 때문에 실제 손실은 1%를 초과할 수 있다.

### 3.3 일 손실 차단

**가설(H):** `daily_loss_limit=3%`. `daily_return_t = NAV_close_t / NAV_close_{t-1} - 1`가 `-3%` 이하이면 `t+1`의 **신규 매수 의도**를 만들지 않는다. 이미 대기 중인 전일 매수도 `t` 종가에 차단 상태가 확인되면 `t+1` 시가 전에 취소한다. 청산은 항상 허용한다. 전일 NAV 또는 당일 유효한 평가가격이 없어 일 수익률을 계산할 수 없으면 보수적으로 신규 매수를 차단하고 사유를 기록한다.

## 4. 보유·청산 규칙의 평가 가능한 고정

모든 청산은 장중 가격이 아니라 `t` 종가에 판정하고 `t+1` 시가에만 시도한다. 진입일을 포함하여 보유 포지션의 유효 일봉 수를 `age_sessions`로 센다(진입일 시가 체결 후 그 날 종가가 유효하면 1).

다음 중 하나가 참이면 `EXIT` 의도를 하나 만든다. 같은 날 복수 조건이면 아래 우선순위의 단일 `exit_reason`만 기록한다.

1. `STOP_CLOSE`: `close_t <= initial_stop_price`.
2. `MAX_HOLD`: `age_sessions >= 20`.
3. `TREND_BREAK`: `age_sessions >= 10` 이고 `close_t < SMA20_t`.

`initial_stop_price`는 진입 때 결정한 절대 가격으로 보유 중 변경하지 않는다. 트레일링 스톱, 장중 저가 스톱, 이익실현 목표, risk-off 일괄 청산은 v0에 넣지 않는다. 이 선택은 **가설(H)** 이며, 특히 ‘10~20일 스윙’이라는 전략 의도를 검사하기 위한 최소 규칙이다.

신규 진입은 3절의 risk-on을 요구하지만, 이미 보유한 포지션은 위 청산 규칙으로만 청산한다. 시장 risk-off는 신규 진입 금지의 결과일 뿐 그 자체로 청산 사유가 아니다.

## 5. 거래 가능성, 결측과 미체결

### 5.1 실행일의 거래 가능 판정

주문 시도일 `t+1` 바가 다음을 모두 만족할 때만 체결 가능(`fillable`)하다.

- 바가 존재하고 `is_tradable=true`.
- OHLCV가 데이터 품질 계약을 통과하고 `open > 0`.
- `volume > 0` 및 `trading_value > 0`.

거래정지, 주문 제한, 상·하한가 고착을 공급원이 명시적으로 제공하면 `is_tradable=false`로 전달한다. 공급원이 상태 자체를 제공하지 않는 FDR 일봉에서는 상태 부재를 임의로 추론하지 않고, 그 한계를 `tradability_source_limitations`에 기록한다. 다만 상태가 `restricted`로 들어왔는데 매수·매도 가능 여부를 판정할 정보가 부족하면 보수적으로 거래 불가로 처리한다. `volume=0`은 v0에서 거래 불가로 처리한다.

### 5.2 한 번만 시도하는 주문

**확정:** v0 주문은 단일 거래일 IOC 성격이다. `t+1`에 결측·거래 불가·0거래량·유효하지 않은 시가이면 주문은 `CANCELED_UNFILLED`로 끝나며 이후 날짜로 이월하지 않는다.

- 진입 미체결: 현금·포지션 변화 없음. `unfilled_reason`을 원장에 남긴다.
- 청산 미체결: 기존 포지션을 유지한다. 이후 유효한 종가에 청산 조건이 다시 참이면 그 다음 거래일에 새 청산 의도를 만든다.
- 공급원이 `restricted` 상태만 주고 가격제한폭·호가단위 등으로 방향별 주문 가능성을 판정할 수 없으면 `UNVERIFIABLE_TRADABILITY`로 취소한다. 상태가 아예 제공되지 않은 FDR 일봉은 이 사유가 아니라 위의 데이터 공급 한계로 보고한다.

신호 산출에 필요한 과거 바가 결측·무효이면 해당 심볼·그 날짜의 신규 후보를 제외하고 `DATA_QUALITY_REJECT`를 기록한다. 기존 포지션의 결측 종가는 마지막 유효 종가로 평가만 하며, 결측 데이터를 이용해 새 신호·청산 신호를 만들지 않는다.

**v0 제한:** 부분 체결과 거래대금 참여율은 지원하지 않는다. 주문은 전량 체결 또는 0주 체결이다. 향후 버전에서 참여율 한도와 부분 체결을 넣기 전에는 대형 주문의 현실성을 주장할 수 없다.

## 6. 비용·슬리피지의 주입 계약

전략 코드가 비용을 상수로 갖지 않게 한다. 실행마다 직렬화 가능한 `ExecutionCostConfig`를 주입한다.

```text
ExecutionCostConfig(
  buy_slippage_bps: Decimal,
  sell_slippage_bps: Decimal,
  buy_commission_bps: Decimal,
  sell_commission_bps: Decimal,
  sell_tax_bps_by_asset_type: Mapping[str, Decimal],
  fixed_fee_per_order: Decimal,
  tick_rounder: Callable[[Decimal, Side], Decimal],
)
```

체결 가능 주문의 기준가는 `open_{t+1}`이고 다음을 순서대로 적용한다.

```text
raw_buy  = open * (1 + buy_slippage_bps / 10_000)
raw_sell = open * (1 - sell_slippage_bps / 10_000)
fill_price = tick_rounder(raw_buy, BUY)   # 매수는 올림
fill_price = tick_rounder(raw_sell, SELL) # 매도는 내림
notional = fill_price * quantity
commission = notional * side_commission_bps / 10_000
sell_tax = notional * sell_tax_bps_by_asset_type[asset_type] / 10_000  # SELL만
cash_debit(BUY) = notional + commission + fixed_fee_per_order
cash_credit(SELL) = notional - commission - sell_tax - fixed_fee_per_order
```

기본 시나리오는 아래 **가설(H)** 값으로 시작한다. 실제 KRX 수수료·매도세·ETF 과세와 호가단위는 상품·시점·브로커별로 대사 전까지 확정값이 아니다.

| 파라미터 | v0 기본 가설(H) |
|---|---:|
| 매수/매도 슬리피지 | 각각 10 bps |
| 매수/매도 수수료 | 각각 1.5 bps |
| 매도세 | 주식 20 bps, ETF 0 bps |
| 주문 고정비 | 0원 |
| 호가 반올림 | 1원 단위, 매수 올림·매도 내림 |

보고서는 최소한 `base`, `stress` 두 설정을 실행한다. `stress`는 base보다 각 방향 슬리피지와 수수료를 작지 않게 두며, 정확한 배수는 **가설(H)** 로 설정 파일에 기록한다. 설정값·버전·각 비용 구성요소를 거래 원장에 보존한다.

## 7. 산출물 계약

### 7.1 실행 요약과 일별 equity curve

`run_summary.json`은 입력 스냅샷 hash, 모든 설정, 시작/종료일, 종목 수, 처리/거절 바 수, 주문·체결·취소 수를 포함한다. `equity_curve.csv`는 거래일별 아래 컬럼을 가진다.

```text
trade_date, cash, market_value, nav_close, daily_return, cumulative_return,
peak_nav, drawdown, gross_exposure, position_count, stale_mark_count,
new_entry_blocked, new_entry_block_reason
```

필수 지표(모두 비용 후 NAV 기준):

- 시작/종료 NAV, 총수익률, 연환산 수익률(CAGR; 기간이 1년 미만이면 `N/A`), 일별 변동성, 연환산 Sharpe(무위험수익률 0이라는 **가설(H)**), 최대낙폭과 그 구간
- 거래 수, 체결률, 미체결/데이터 거절 수와 사유별 건수, 승률, 평균·중앙 보유일, 평균 승/패, profit factor
- 평균·최대 동시 포지션 수, 평균·최대 gross exposure, 총 수수료·세금·슬리피지 비용
- 신호일별·체결일별 월간 수익률과 월별 거래 수

연환산에 쓰는 거래일 수는 252라는 **가설(H)** 이며 `annualization_days`로 설정·기록한다. 무거래일·stale mark 일수도 함께 보고하여 지표 해석을 제한한다.

### 7.2 주문·체결·포지션 원장 스키마

식별자는 실행 내 유일하고 연결 가능해야 한다. CSV 또는 Parquet에서 아래 최소 컬럼을 보장한다.

**`orders` (주문 의도/시도):**

```text
run_id, order_id, signal_id, position_id, symbol, asset_type, side,
signal_date, scheduled_trade_date, submitted_at_phase, status,
intent_reason, candidate_rank, risk_on, breakout_strength, momentum_60,
risk_budget, requested_quantity, filled_quantity, unfilled_quantity,
unfilled_reason, execution_config_id, data_snapshot_hash
```

상태는 `PENDING`, `FILLED`, `CANCELED_UNFILLED`, `CANCELED_RISK_BLOCK`, `CANCELED_CASH`만 허용한다.

**`fills` (체결당 1행; v0에서는 주문당 최대 1행):**

```text
run_id, fill_id, order_id, position_id, trade_date, symbol, side, quantity,
reference_open, raw_slippage_price, fill_price, notional, slippage_bps,
slippage_cost, commission, sell_tax, fixed_fee, total_cost, cash_delta,
tick_rounding_rule, asset_type
```

**`positions` (종료 포지션당 1행, 열린 포지션도 실행 종료 시 1행):**

```text
run_id, position_id, symbol, asset_type, entry_order_id, entry_fill_id,
entry_signal_date, entry_trade_date, entry_price, initial_stop_price,
entry_quantity, exit_order_id, exit_fill_id, exit_signal_date,
exit_trade_date, exit_price, exit_reason, age_sessions, status,
gross_pnl, total_costs, net_realized_pnl, net_return, last_mark_date,
last_mark_price, unrealized_pnl
```

`signals` 부속 원장에는 모든 평가 심볼을 남긴다: `signal_id, signal_date, symbol, eligible, rejection_reason, risk_on, liquidity_pass, momentum_pass, candidate_rank, breakout_strength, momentum_60, scheduled_trade_date`. 후보가 아니었던 이유까지 남겨야 선택편향·결측을 감사할 수 있다.

## 8. 첫 구현: 작은 TDD 수직 슬라이스

첫 구현은 전체 전략·데이터 로더를 만들지 말고, 메모리의 두 거래일·한 심볼·현금만으로 다음 관통 경로를 증명한다. 예상 패키지 경로는 `src/swing_v2/backtest/`, 테스트는 `tests/test_backtest_v0.py`다.

1. **RED:** `test_signal_at_t_is_filled_only_at_t_plus_1_open_with_buy_costs`를 작성한다. `t` 종가에서 이미 참인 신호와 `t+1`의 다른 시가를 주고, `t`에는 fill이 없고 `t+1`에만 `fill_price=open×(1+slippage)` 및 정확한 수수료·현금 차감이 발생한다고 단언한다. 아직 `run_two_day_backtest`가 없으므로 의도된 import/attribute failure를 확인한다.
2. **GREEN:** `DailyBar`와 명시적 `ExecutionCostConfig`만 받는 최소 순수 함수 `run_two_day_backtest(...)`를 구현한다. 신호 생성, 다음 날 주문 의도, 시가 체결, 비용 차감, 하나의 fill 원장 행을 만든다. 외부 데이터·시간·난수·전략 전역상수는 사용하지 않는다. 테스트가 통과함을 확인한다.
3. **RED:** `test_untradable_or_missing_t_plus_1_cancels_instead_of_delaying_fill`를 추가한다. `t+1`이 `is_tradable=false` 또는 없으면 `CANCELED_UNFILLED`이고 `t+2` 바가 있어도 fill이 없음을 단언한다. 실패를 확인한다.
4. **GREEN:** 한 번만 시도하는 주문 상태 전이와 `unfilled_reason`을 최소 구현해 통과시킨다.
5. **RED:** `test_close_stop_signal_exits_next_open_not_same_close`를 추가한다. 진입 후 `t` 종가가 stop 이하이면 그 종가로 매도하지 않고 `t+1` 시가에 매도하며 `STOP_CLOSE`가 기록됨을 단언한다. 실패를 확인한다.
6. **GREEN:** 단일 포지션과 `STOP_CLOSE`의 종가 판정·다음 시가 청산만 구현해 통과시킨다.
7. 모든 단위 테스트를 실행한 뒤에만 중복을 정리한다. 그 다음 순서로 10/20일 보유 규칙, 랭킹·5개 한도, 일 손실 차단, 데이터 어댑터를 각각 별도 RED→GREEN 주기로 확장한다.

수직 슬라이스의 완료 기준은 (a) 어떤 테스트도 동일일 종가 체결을 허용하지 않고, (b) 비용은 주입값이 바뀌면 원장과 현금에 반영되며, (c) 미체결이 미래 날짜에 유령 체결되지 않고, (d) 각 원장 행이 `signal_id → order_id → fill_id/position_id`로 추적되는 것이다.

## 9. v0 비목표와 검증 게이트

v0 결과는 가설 탐색용이다. 생존자 편향 없는 과거 유니버스, 상장폐지 종목, 기업행사 조정, 실제 가격제한폭·호가단위, 공식 거래대금, 브로커 수수료·세금, 거래대금 참여율, 부분 체결을 검증하기 전에는 실거래 기대수익률로 해석하지 않는다.

다음 단계로 넘어가기 전에는 최소한 (1) 원시 데이터 스냅샷 재실행 결과의 hash/NAV/원장이 동일한지, (2) base와 stress 비용 하에서 성과가 어떻게 변하는지, (3) 미체결·결측·risk block 사유별 건수가 무엇인지, (4) 표본 외 기간에서도 같은 고정 파라미터가 유지되는지를 보고한다.
