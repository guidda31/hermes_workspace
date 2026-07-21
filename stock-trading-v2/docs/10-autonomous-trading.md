# 10 — Autonomous (unattended) live trading

The full design for the loop running **with no human in the loop**. The deterministic
tools are all built (`swing_v2.live.*`, `swing_v2.llm.*`); the only step that needs an
LLM is the BUY/SELL/HOLD decision, and that is **Hermes** (GPT via OAuth) at the runtime.
So "autonomous" = a **Hermes-native cron** orchestrating the tools + an **auto-arm
authorization** that replaces the human `--arm` step with bounded, fail-closed automation.

> Status: PREPARED, not activated. Activation needs (a) Hermes GPT quota to return, and
> (b) the operator to write the autonomous authorization. Until then the same loop runs
> semi-automatically (a human triggers it; Claude or Hermes makes the decision).

## Daily sequence (what the cron does)

Each KRX trading day, after data is available and **during 09:00–15:30 KST**:

1. `python -m deploy.forward_prepare --signal-date <D> --collection-dir data/kis-live-<D>`
   — read-only KIS collect + rebuild the point-in-time snapshot.
2. `python -m swing_v2.llm.forward_cli render --snapshot … --signal-date <D> --held <held> …`
   — build the PIT brief (price + DART disclosures + news).
3. **Hermes reads the brief and decides** (the only LLM step) → writes the reply JSON.
4. `python -m swing_v2.llm.forward_cli record --reply-file <hermes.json> --output data/forward-records/signal-<D>.json`
   — guardrail + immutable audit of the AI's decision.
5. `python -m swing_v2.live.pilot_cli from-decision --autonomous [--symbol <one> --max-notional <≥1 share> --max-positions <n>]`
   — turn the AI's admitted BUY into a sized order and place it **auto-armed**.

Steps 1–2, 4–5 are deterministic tools. Step 3 is Hermes thinking. All-HOLD → step 5
orders nothing (correct).

## The safety stack (all fail-closed — replaces the human)

`from-decision --autonomous` places an order only if EVERY check passes:

1. **Kill switch** disengaged (`live/kill_switch.py`; a present/corrupt marker halts).
   Operator override anytime: `pilot_cli halt --reason "…"` / `resume`.
2. **KRX regular hours** (`is_krx_regular_session`, Mon–Fri 09:00–15:30 KST).
3. **Autonomous authorization** on file (`live/autonomous.py`), which is:
   - explicit (created once via `authorize-autonomous`, carries the exact phrase),
   - **expiring** (hard `--expires` date; auto-stops if not renewed),
   - **budgeted** — `--max-orders`/day and `--max-notional`/day, enforced across cron
     firings by the per-day order-budget ledger.
4. **Pretrade guardrails** (`live/risk.py`): max positions, per-position risk, notional
   cap, and the **realized daily-loss circuit breaker** (`live/daily_loss.py`).
5. **Tiny pilot caps** (`--max-notional`, `--max-positions`) + **write-once audit** +
   duplicate-order protection (`live/audit.py`).

If any fails: no order, non-zero exit, a logged reason. The autonomous path passes
through the **same** production submit gate as the manual path.

## Enable / disable (operator actions)

```bash
# ENABLE (bounded opt-in): e.g. expires in a week, ≤1 order & ≤150,000원/day
python -m swing_v2.live.pilot_cli authorize-autonomous \
    --expires 2026-07-28 --max-orders 1 --max-notional 150000 \
    --confirm KIS_AUTONOMOUS_TRADING_OPERATOR_CONFIRMED

# DISABLE immediately (overrides authorization):
python -m swing_v2.live.pilot_cli halt --reason "stop autonomous"
# or just let the --expires date pass (fail-safe: it stops itself).
```

## Hermes cron registration (activate when GPT quota returns — DO NOT run now)

```
hermes cron create "0 1 * * 1-5" \
  "KRX autonomous pilot. cd stock-trading-v2. Run forward_prepare for today, render the \
   brief, DECIDE BUY/SELL/HOLD from disclosures+news (defense-first), record it, then run \
   pilot_cli from-decision --autonomous. Respect all fail-closed gates; if anything \
   refuses, stop and report — never bypass." \
  --name krx-autonomous-pilot
```
`0 1 * * 1-5` = 10:00 KST weekdays (01:00 UTC). Cloud schedulers can NOT run this (no
local KIS creds / local data); only the Hermes-native cron can, because OpenClaw manages
the OAuth session. Register ONCE, only after quota returns and after a few armed manual
runs have been reviewed.

## Residual risks (be honest)

- **No human circuit breaker** — mitigated by the expiring, budgeted authorization + kill
  switch + daily-loss breaker + tiny caps. Keep budgets small; renew deliberately.
- **KRX holidays not modeled** — a holiday run reaches the market-hours gate as "open",
  submits, and KIS rejects it (`BrokerRejectedOrder`, handled). No order results.
- **~0 selection alpha** — the AI's stock-picking edge is unproven (see doc 09 / forward
  eval). This pilot is a plumbing + discipline test, not a validated money-maker. The
  defensive value (risk_cli) is the surer use.
- **OAuth quota** — if Hermes quota lapses mid-week the cron simply fails to fire; the
  authorization still expires on schedule, so nothing runs unsupervised indefinitely.
