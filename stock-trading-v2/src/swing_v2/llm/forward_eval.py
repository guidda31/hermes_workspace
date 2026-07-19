"""Score accumulated forward-observation signal records against realized outcomes.

Forward observation is the survivorship-free validation: signals recorded on real
future days, judged only by what actually happened next — no look-back, no LLM
hindsight. This harness joins each record's admitted BUY picks to realized bars
(entry at the next session's open, exit at the close ``forward_sessions`` later) and
reports hit rate, mean pick return, the market benchmark, the pick-minus-market edge,
and conviction calibration. Records whose forward window has not elapsed yet are
skipped, not counted — so this can run repeatedly as observations accumulate.

Pure over injected inputs (records + a bar lookup + the calendar); no network, no
orders. It measures signal quality; it never trades.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..contracts import DailyBar

BarLookup = Callable[[str, date], "DailyBar | None"]

_BUCKETS = (("high", Decimal("0.7"), Decimal("1")), ("mid", Decimal("0.4"), Decimal("0.7")), ("low", Decimal("0"), Decimal("0.4")))


@dataclass(frozen=True)
class SymbolForwardOutcome:
    symbol: str
    signal_date: date
    conviction: Decimal
    entry_price: Decimal
    exit_price: Decimal
    forward_return: Decimal
    market_return: Decimal | None
    hit: bool


@dataclass(frozen=True)
class ConvictionBucket:
    label: str
    count: int
    mean_return: Decimal
    hit_rate: Decimal


@dataclass(frozen=True)
class ForwardObservationReport:
    signal_count: int
    scored_count: int
    hit_rate: Decimal
    mean_pick_return: Decimal
    mean_market_return: Decimal
    edge: Decimal
    by_conviction: tuple[ConvictionBucket, ...]
    outcomes: tuple[SymbolForwardOutcome, ...]


def evaluate_forward_observations(
    records: Sequence[Mapping],
    *,
    bar_lookup: BarLookup,
    calendar: Sequence[date],
    market_symbol: str,
    forward_sessions: int = 5,
) -> ForwardObservationReport:
    """Score accumulated signal records against realized forward outcomes."""
    if not records:
        raise ValueError("records must be a nonempty sequence of signal records")
    if not callable(bar_lookup):
        raise ValueError("bar_lookup must be callable")
    if type(forward_sessions) is not int or forward_sessions < 1:
        raise ValueError("forward_sessions must be an int >= 1")
    index_of = {day: i for i, day in enumerate(calendar)}
    if len(index_of) != len(calendar):
        raise ValueError("calendar must be unique dates")

    signal_count = 0
    outcomes: list[SymbolForwardOutcome] = []
    for record in records:
        signal_date = date.fromisoformat(str(record["signal_date"]))
        picks = _admitted_buys(record)
        signal_count += len(picks)
        i = index_of.get(signal_date)
        if i is None or i + forward_sessions >= len(calendar):
            continue  # unobservable yet (or off-calendar): skip, do not count
        entry_day, exit_day = calendar[i + 1], calendar[i + forward_sessions]
        market_return = _window_return(bar_lookup, market_symbol, entry_day, exit_day)
        for symbol, conviction in picks:
            pick_return = _window_return(bar_lookup, symbol, entry_day, exit_day)
            if pick_return is None:
                continue  # missing forward bar: skip
            entry_bar = bar_lookup(symbol, entry_day)
            exit_bar = bar_lookup(symbol, exit_day)
            outcomes.append(SymbolForwardOutcome(
                symbol=symbol, signal_date=signal_date, conviction=conviction,
                entry_price=entry_bar.open, exit_price=exit_bar.close,
                forward_return=pick_return, market_return=market_return, hit=pick_return > 0,
            ))

    return _aggregate(signal_count, tuple(outcomes))


def _admitted_buys(record: Mapping) -> list[tuple[str, Decimal]]:
    admitted = set(record.get("admitted_symbols", []))
    result: list[tuple[str, Decimal]] = []
    for decision in record.get("decisions", []):
        if decision.get("action") == "BUY" and decision.get("symbol") in admitted:
            result.append((str(decision["symbol"]), Decimal(str(decision["conviction"]))))
    return result


def _window_return(bar_lookup: BarLookup, symbol: str, entry_day: date, exit_day: date) -> Decimal | None:
    entry_bar = bar_lookup(symbol, entry_day)
    exit_bar = bar_lookup(symbol, exit_day)
    if entry_bar is None or exit_bar is None:
        return None
    if not entry_bar.open.is_finite() or entry_bar.open <= 0 or not exit_bar.close.is_finite():
        return None
    return exit_bar.close / entry_bar.open - Decimal("1")


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else Decimal("0")


def _aggregate(signal_count: int, outcomes: tuple[SymbolForwardOutcome, ...]) -> ForwardObservationReport:
    scored = len(outcomes)
    pick_returns = [o.forward_return for o in outcomes]
    market_returns = [o.market_return for o in outcomes if o.market_return is not None]
    hit_rate = _mean([Decimal("1") if o.hit else Decimal("0") for o in outcomes])
    mean_pick = _mean(pick_returns)
    mean_market = _mean(market_returns)

    buckets: list[ConvictionBucket] = []
    for label, lo, hi in _BUCKETS:
        in_bucket = [o for o in outcomes if lo <= o.conviction < hi or (hi == Decimal("1") and o.conviction == Decimal("1"))]
        if in_bucket:
            buckets.append(ConvictionBucket(
                label=label, count=len(in_bucket),
                mean_return=_mean([o.forward_return for o in in_bucket]),
                hit_rate=_mean([Decimal("1") if o.hit else Decimal("0") for o in in_bucket]),
            ))

    return ForwardObservationReport(
        signal_count=signal_count, scored_count=scored, hit_rate=hit_rate,
        mean_pick_return=mean_pick, mean_market_return=mean_market,
        edge=mean_pick - mean_market, by_conviction=tuple(buckets), outcomes=outcomes,
    )
