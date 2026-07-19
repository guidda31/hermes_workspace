"""Point-in-time (PIT) brief handed to Hermes for one signal date.

The brief is the sole context the agent sees when deciding. Its defining invariant:
nothing observed or published after ``signal_date`` may appear in it. Price bars are
bounded by the existing no-lookahead snapshot query; disclosures and news arrive via
injected providers and are filtered by publication time. An evidence item without a
timezone-aware publication time is rejected fail-closed — an unprovable timestamp is
treated as unusable, never as "assume it existed then" (see
``docs/krx-historical-metadata-source-recon.md`` for the same principle).

This module calls no LLM API and submits no orders. It only assembles a brief and
exposes ``known_symbols`` / ``known_evidence_ids`` so the decision parser can reject
any agent citation that was not actually in the brief.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from ..backtest_data import SnapshotBacktestData
from ..signals import is_risk_on, passes_liquidity_filter

# KRX trades in KST; publication instants are compared on the KST calendar day so a
# same-day after-close disclosure (known when we decide after the close) is included,
# while anything dated after the signal date is excluded.
_KST = timezone(timedelta(hours=9))

# Evidence provider: given a symbol and signal date, return candidate items. The
# builder — not the provider — enforces the PIT cutoff, so a provider cannot widen it.
EvidenceProvider = Callable[[str, date], Sequence["EvidenceItem"]]

# Must exceed the longest indicator lookback: the 60-day return needs 61 closes,
# so a 60-bar window would leave return_60 permanently None. 120 gives headroom.
_DEFAULT_WINDOW = 120


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    kind: str
    symbol: str
    published_at: datetime
    summary: str

    def __post_init__(self) -> None:
        if type(self.evidence_id) is not str or not self.evidence_id.strip():
            raise ValueError("evidence_id must be a nonempty plain str")
        if self.kind not in {"disclosure", "news"}:
            raise ValueError("evidence kind must be 'disclosure' or 'news'")
        if type(self.symbol) is not str or not self.symbol.strip():
            raise ValueError("evidence symbol must be a nonempty plain str")
        if type(self.published_at) is not datetime or self.published_at.tzinfo is None:
            raise ValueError("evidence published_at must be a timezone-aware datetime")
        if type(self.summary) is not str or not self.summary.strip():
            raise ValueError("evidence summary must be a nonempty plain str")

    def published_kst_date(self) -> date:
        return self.published_at.astimezone(_KST).date()


@dataclass(frozen=True)
class SymbolBrief:
    symbol: str
    asset_type: str
    latest_trade_date: date
    latest_close: Decimal
    latest_trading_value: Decimal
    moving_average_20: Optional[Decimal]
    moving_average_60: Optional[Decimal]
    return_20: Optional[Decimal]
    return_60: Optional[Decimal]
    liquidity_pass: bool
    price_evidence_id: str
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class MarketBrief:
    symbol: str
    latest_trade_date: date
    latest_close: Decimal
    is_risk_on: bool
    price_evidence_id: str


@dataclass(frozen=True)
class Brief:
    signal_date: date
    market: MarketBrief
    symbols: tuple[SymbolBrief, ...]
    known_symbols: frozenset[str] = field(init=False)
    known_evidence_ids: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "known_symbols", frozenset(s.symbol for s in self.symbols))
        evidence_ids = {self.market.price_evidence_id}
        for symbol_brief in self.symbols:
            evidence_ids.add(symbol_brief.price_evidence_id)
            evidence_ids.update(item.evidence_id for item in symbol_brief.evidence)
        object.__setattr__(self, "known_evidence_ids", frozenset(evidence_ids))


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values) / Decimal(len(values))


def _return_over(closes: Sequence[Decimal], span: int) -> Optional[Decimal]:
    if len(closes) < span + 1 or closes[-1 - span] <= 0:
        return None
    return closes[-1] / closes[-1 - span] - 1


def _collect_pit_evidence(
    provider: Optional[EvidenceProvider], symbol: str, signal_date: date
) -> tuple[EvidenceItem, ...]:
    if provider is None:
        return ()
    items = provider(symbol, signal_date)
    kept: list[EvidenceItem] = []
    for item in items:
        if not isinstance(item, EvidenceItem):
            raise ValueError("evidence provider must yield EvidenceItem values")
        if item.symbol != symbol:
            raise ValueError("evidence item symbol does not match the requested symbol")
        # PIT cutoff: exclude anything published after the signal date's KST day.
        if item.published_kst_date() <= signal_date:
            kept.append(item)
    return tuple(kept)


def build_brief(
    data: SnapshotBacktestData,
    *,
    signal_date: date,
    symbols: Sequence[str],
    disclosure_provider: Optional[EvidenceProvider] = None,
    news_provider: Optional[EvidenceProvider] = None,
    window: int = _DEFAULT_WINDOW,
) -> Brief:
    """Assemble the PIT brief for ``signal_date`` from a local snapshot only."""
    if not isinstance(data, SnapshotBacktestData):
        raise ValueError("data must be a SnapshotBacktestData")
    if type(signal_date) is not date:
        raise ValueError("signal_date must be a plain date")
    symbol_list = tuple(symbols)
    if not symbol_list or len(symbol_list) != len(set(symbol_list)):
        raise ValueError("symbols must be a nonempty sequence of unique symbols")
    if type(window) is not int or window <= 0:
        raise ValueError("window must be a positive int")

    market_bar = data.get_market_index_bar(signal_date)
    if market_bar is None:
        raise ValueError("signal_date is not a session with a market-index bar")

    market_closes = data.get_historical_closes(market_bar.symbol, signal_date, max(window, 200))
    market = MarketBrief(
        symbol=market_bar.symbol,
        latest_trade_date=market_bar.trade_date,
        latest_close=market_bar.close,
        is_risk_on=is_risk_on(market_closes),
        price_evidence_id=f"px:{market_bar.symbol}:{signal_date.isoformat()}",
    )

    symbol_briefs: list[SymbolBrief] = []
    for symbol in symbol_list:
        bars = data.get_historical_bars(symbol, signal_date, window)
        if not bars or bars[-1].trade_date != signal_date:
            raise ValueError(f"no confirmed {symbol} bar at signal_date {signal_date.isoformat()}")
        closes = tuple(bar.close for bar in bars)
        latest = bars[-1]
        disclosures = _collect_pit_evidence(disclosure_provider, symbol, signal_date)
        news = _collect_pit_evidence(news_provider, symbol, signal_date)
        symbol_briefs.append(SymbolBrief(
            symbol=symbol,
            asset_type=data.get_asset_type(symbol),
            latest_trade_date=latest.trade_date,
            latest_close=latest.close,
            latest_trading_value=latest.trading_value,
            moving_average_20=_mean(closes[-20:]) if len(closes) >= 20 else None,
            moving_average_60=_mean(closes[-60:]) if len(closes) >= 60 else None,
            return_20=_return_over(closes, 20),
            return_60=_return_over(closes, 60),
            liquidity_pass=passes_liquidity_filter(bars),
            price_evidence_id=f"px:{symbol}:{signal_date.isoformat()}",
            evidence=disclosures + news,
        ))

    return Brief(signal_date=signal_date, market=market, symbols=tuple(symbol_briefs))
