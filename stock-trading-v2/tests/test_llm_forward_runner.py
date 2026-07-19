"""Tests for the forward-observation SIGNAL runner.

The runner composes the existing llm tools into one signal-only cycle: it builds the
point-in-time brief, hands it to the INJECTED Hermes ``decide`` seam, validates and
guardrails the returned decisions, and emits an immutable signal audit. It never
produces an order, fill, quantity, or cash concept — only an audit observation.

These tests inject a fake ``decide`` in place of the real agent, so they touch no
network and no LLM API.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from swing_v2.backtest_data import (
    DailyBarSnapshot,
    SnapshotBacktestData,
    SnapshotMetadata,
)
from swing_v2.contracts import DailyBar
from swing_v2.llm.forward_runner import run_forward_signal
from swing_v2.llm.guardrail import GuardrailConfig, PortfolioContext
from swing_v2.llm.signal_audit import load_signal_audit


KST = timezone(timedelta(hours=9))
PILOT = ("005930", "000660")
DECIDED_AT = datetime(2026, 7, 16, 16, 0, tzinfo=KST)
MODEL_ID = "hermes-agent-gpt/2026-07"


def _bar(day, symbol, asset_type, close, *, tradable=True, volume=1_000_000):
    close = Decimal(close)
    return DailyBar(
        trade_date=day,
        symbol=symbol,
        asset_type=asset_type,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        trading_value=close * volume,
        is_tradable=tradable,
    )


def _make_data(last_day=date(2026, 7, 16), n=6):
    """Build a small in-memory snapshot of n ascending weekday sessions."""
    days = []
    cursor = last_day
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.sort()
    market = [_bar(d, "KS11", "INDEX", 2500 + i) for i, d in enumerate(days)]
    histories = {
        "005930": [_bar(d, "005930", "STOCK", 70000 + i * 100) for i, d in enumerate(days)],
        "000660": [_bar(d, "000660", "STOCK", 180000 + i * 100) for i, d in enumerate(days)],
    }
    metadata = SnapshotMetadata(
        source="TEST",
        retrieved_at="2026-07-17T00:00:00+09:00",
        data_as_of=days[-1].isoformat(),
        trading_value_is_close_times_volume_proxy=True,
    )
    snapshot = DailyBarSnapshot(
        metadata=metadata,
        market_symbol="KS11",
        asset_types={"005930": "STOCK", "000660": "STOCK"},
        trade_calendar=days,
        histories=histories,
        market_history=market,
    )
    return SnapshotBacktestData(snapshot), days


def _portfolio():
    return PortfolioContext(held_symbols=frozenset(), new_entries_blocked=False)


def _config(eligible=frozenset(PILOT)):
    return GuardrailConfig(eligible_symbols=eligible)


def _buy(symbol, *, evidence, conviction="0.8", weight="0.1"):
    return {
        "symbol": symbol,
        "action": "BUY",
        "conviction": conviction,
        "target_weight": weight,
        "rationale": f"momentum on {symbol}",
        "cited_evidence": list(evidence),
    }


def _run(data, days, decide, **overrides):
    kwargs = dict(
        signal_date=days[-1],
        symbols=PILOT,
        guardrail_config=_config(),
        portfolio=_portfolio(),
        model_id=MODEL_ID,
        decided_at=DECIDED_AT,
        decide=decide,
    )
    kwargs.update(overrides)
    return run_forward_signal(data, **kwargs)


def _deep_walk_keys(obj):
    """Yield every mapping key appearing anywhere in a nested structure."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _deep_walk_keys(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _deep_walk_keys(value)


class ForwardRunnerHappyPathTests(unittest.TestCase):
    def test_valid_buy_is_admitted_in_the_audit(self):
        data, days = _make_data()
        signal_date = days[-1]
        evidence_id = f"px:005930:{signal_date.isoformat()}"

        def decide(brief):
            self.assertEqual(brief.signal_date, signal_date)
            self.assertIn("005930", brief.known_symbols)
            return [_buy("005930", evidence=[evidence_id])]

        record = _run(data, days, decide)
        self.assertIn("005930", record["admitted_symbols"])
        self.assertEqual(record["model_id"], MODEL_ID)
        self.assertEqual(record["signal_date"], signal_date.isoformat())
        self.assertIn("integrity", record)

    def test_decide_receives_the_brief_object_not_a_dict(self):
        data, days = _make_data()
        seen = {}

        def decide(brief):
            seen["type"] = type(brief).__name__
            seen["has_known_symbols"] = hasattr(brief, "known_symbols")
            return []

        _run(data, days, decide)
        self.assertEqual(seen["type"], "Brief")
        self.assertTrue(seen["has_known_symbols"])


class ForwardRunnerRejectionTests(unittest.TestCase):
    def test_citing_evidence_not_in_brief_is_rejected_by_parse(self):
        data, days = _make_data()

        def decide(brief):
            return [_buy("005930", evidence=["news:005930:hallucinated"])]

        with self.assertRaises(ValueError):
            _run(data, days, decide)

    def test_ineligible_symbol_appears_in_rejected(self):
        data, days = _make_data()
        signal_date = days[-1]
        evidence_id = f"px:000660:{signal_date.isoformat()}"

        def decide(brief):
            return [_buy("000660", evidence=[evidence_id])]

        record = run_forward_signal(
            data,
            signal_date=signal_date,
            symbols=PILOT,
            guardrail_config=_config(eligible=frozenset({"005930"})),
            portfolio=_portfolio(),
            model_id=MODEL_ID,
            decided_at=DECIDED_AT,
            decide=decide,
        )
        rejected_symbols = [r["symbol"] for r in record["rejected"]]
        self.assertIn("000660", rejected_symbols)
        self.assertNotIn("000660", record["admitted_symbols"])


class ForwardRunnerFailClosedTests(unittest.TestCase):
    def test_decide_returning_a_mapping_not_a_list_is_rejected(self):
        data, days = _make_data()

        def decide(brief):
            return {"symbol": "005930"}  # a bare mapping, not a list of decisions

        with self.assertRaises(ValueError):
            _run(data, days, decide)

    def test_decide_returning_non_mapping_entry_is_rejected(self):
        data, days = _make_data()

        def decide(brief):
            return ["not-a-mapping"]

        with self.assertRaises(ValueError):
            _run(data, days, decide)

    def test_non_callable_decide_is_rejected(self):
        data, days = _make_data()
        with self.assertRaises(ValueError):
            _run(data, days, "not-callable")


class ForwardRunnerNoOrderConceptTests(unittest.TestCase):
    def test_record_contains_no_order_or_fill_or_quantity_or_cash_keys(self):
        data, days = _make_data()
        signal_date = days[-1]
        evidence_id = f"px:005930:{signal_date.isoformat()}"

        def decide(brief):
            return [_buy("005930", evidence=[evidence_id])]

        record = _run(data, days, decide)
        forbidden = {
            "order", "orders", "fill", "fills", "quantity", "qty", "shares",
            "cash", "position", "positions", "notional", "price", "cost",
        }
        keys = set(_deep_walk_keys(record))
        self.assertEqual(keys & forbidden, set())


class ForwardRunnerOutputTests(unittest.TestCase):
    def test_output_path_writes_an_immutable_reloadable_file(self):
        import tempfile
        from pathlib import Path

        data, days = _make_data()
        signal_date = days[-1]
        evidence_id = f"px:005930:{signal_date.isoformat()}"

        def decide(brief):
            return [_buy("005930", evidence=[evidence_id])]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal-audit.json"
            record = _run(data, days, decide, output_path=path)
            reloaded = load_signal_audit(path)
            self.assertEqual(reloaded["integrity"]["digest"], record["integrity"]["digest"])
            # write-once: a second write to the same path must fail
            with self.assertRaises(ValueError):
                _run(data, days, decide, output_path=path)


if __name__ == "__main__":
    unittest.main()
