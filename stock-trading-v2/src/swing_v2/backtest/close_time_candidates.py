"""Close-time, no-lookahead assessment of universe entry candidates.

``Candidate`` requires positive Decimal score fields.  For an ineligible
assessment, its embedded Candidate consequently receives ``NEUTRAL_SCORE``
when no positive, rankable score is available.  The assessment's optional
score fields retain the real score (including zero/negative) or ``None`` when
history is insufficient or invalid.
"""

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from swing_v2.contracts import DailyBar
from swing_v2.signals import is_momentum_breakout, is_risk_on, passes_liquidity_filter

from .candidates import Candidate


NEUTRAL_SCORE = Decimal("0.0000000001")


@dataclass(frozen=True)
class CandidateAssessment:
    """One frozen close-time assessment, including its rankable Candidate."""

    symbol: str
    asset_type: str | None
    candidate: Candidate
    risk_on: bool
    liquidity: bool
    momentum: bool
    rejection_reasons: tuple[str, ...]
    breakout_strength: Decimal | None
    momentum_60: Decimal | None


def assess_close_time_candidates(
    *,
    signal_date: date,
    market_closes: Sequence[Decimal],
    asset_types: object,
    asset_histories: object,
    universe_symbols: Set[str],
) -> tuple[CandidateAssessment, ...]:
    """Assess each universe symbol from complete bars available at close.

    The result is ordered by ascending symbol.  A malformed, missing, or
    incomplete asset history produces a data-quality rejection rather than an
    exception.  A risk-on result is calculated once from the market sequence
    and copied unchanged into every assessment.
    """
    if type(signal_date) is not date:
        raise ValueError("signal_date must be a plain date")

    risk_on = _safe_risk_on(market_closes)
    asset_type_mapping = asset_types if isinstance(asset_types, Mapping) else {}
    history_mapping = asset_histories if isinstance(asset_histories, Mapping) else {}
    assessments: list[CandidateAssessment] = []

    for symbol in sorted(universe_symbols):
        mapped_asset_type = asset_type_mapping.get(symbol)
        bars = _validated_history(
            symbol=symbol,
            asset_type=mapped_asset_type,
            history=history_mapping.get(symbol),
            signal_date=signal_date,
        )
        if bars is None:
            reasons = ("DATA_QUALITY_REJECT",)
            if not risk_on:
                reasons = ("RISK_OFF",) + reasons
            assessments.append(
                _make_assessment(
                    symbol=symbol,
                    asset_type=mapped_asset_type if isinstance(mapped_asset_type, str) else None,
                    risk_on=risk_on,
                    liquidity=False,
                    momentum=False,
                    rejection_reasons=reasons,
                    breakout_strength=None,
                    momentum_60=None,
                )
            )
            continue

        liquidity = passes_liquidity_filter(bars)
        momentum = is_momentum_breakout([bar.close for bar in bars])
        breakout_strength, momentum_60 = _scores(bars)
        rejection_reasons = _rejection_reasons(
            risk_on=risk_on,
            liquidity=liquidity,
            momentum=momentum,
            breakout_strength=breakout_strength,
            momentum_60=momentum_60,
        )
        assessments.append(
            _make_assessment(
                symbol=symbol,
                asset_type=mapped_asset_type,
                risk_on=risk_on,
                liquidity=liquidity,
                momentum=momentum,
                rejection_reasons=rejection_reasons,
                breakout_strength=breakout_strength,
                momentum_60=momentum_60,
            )
        )

    return tuple(assessments)


def _make_assessment(
    *,
    symbol: str,
    asset_type: str | None,
    risk_on: bool,
    liquidity: bool,
    momentum: bool,
    rejection_reasons: tuple[str, ...],
    breakout_strength: Decimal | None,
    momentum_60: Decimal | None,
) -> CandidateAssessment:
    has_positive_scores = (
        breakout_strength is not None
        and momentum_60 is not None
        and breakout_strength > 0
        and momentum_60 > 0
    )
    eligible = risk_on and liquidity and momentum and has_positive_scores
    candidate_breakout_strength = NEUTRAL_SCORE
    candidate_momentum_60 = NEUTRAL_SCORE
    if eligible:
        assert breakout_strength is not None and momentum_60 is not None
        candidate_breakout_strength = breakout_strength
        candidate_momentum_60 = momentum_60
    candidate = Candidate(
        symbol=symbol,
        eligible=eligible,
        breakout_strength=candidate_breakout_strength,
        momentum_60=candidate_momentum_60,
    )
    return CandidateAssessment(
        symbol=symbol,
        asset_type=asset_type,
        candidate=candidate,
        risk_on=risk_on,
        liquidity=liquidity,
        momentum=momentum,
        rejection_reasons=rejection_reasons,
        breakout_strength=breakout_strength,
        momentum_60=momentum_60,
    )


def _validated_history(
    *, symbol: str, asset_type: object, history: object, signal_date: date
) -> tuple[DailyBar, ...] | None:
    if not isinstance(asset_type, str) or not asset_type:
        return None
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        return None

    bars = tuple(history)
    if not bars:
        return None

    previous_date: date | None = None
    for bar in bars:
        if not isinstance(bar, DailyBar):
            return None
        if bar.symbol != symbol or bar.asset_type != asset_type:
            return None
        if previous_date is not None and bar.trade_date <= previous_date:
            return None
        previous_date = bar.trade_date

    return bars if previous_date == signal_date else None


def _safe_risk_on(closes: Sequence[Decimal]) -> bool:
    try:
        return is_risk_on(closes)
    except (ArithmeticError, TypeError, ValueError):
        return False


def _scores(bars: Sequence[DailyBar]) -> tuple[Decimal | None, Decimal | None]:
    if len(bars) < 61:
        return None, None
    closes = [bar.close for bar in bars]
    return closes[-1] / max(closes[-21:-1]) - 1, closes[-1] / closes[-61] - 1


def _rejection_reasons(
    *,
    risk_on: bool,
    liquidity: bool,
    momentum: bool,
    breakout_strength: Decimal | None,
    momentum_60: Decimal | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not risk_on:
        reasons.append("RISK_OFF")
    if not liquidity:
        reasons.append("LIQUIDITY_REJECT")
    if not momentum:
        reasons.append("MOMENTUM_REJECT")
    if breakout_strength is None or momentum_60 is None:
        reasons.append("INSUFFICIENT_HISTORY")
    elif breakout_strength <= 0 or momentum_60 <= 0:
        reasons.append("NON_POSITIVE_SCORE")
    return tuple(reasons)
