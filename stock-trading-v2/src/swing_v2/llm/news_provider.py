"""Source-agnostic point-in-time news evidence provider.

Turns generic news records into ``EvidenceItem`` values for the brief layer so
Hermes can factor news into decisions. The concrete news source (e.g. the Naver
News API) and its key are INJECTED as a ``fetch`` callable: this module reads no
``.env``, constructs no default network client, and imports no HTTP library. The
brief builder — not this provider — enforces the PIT cutoff, so this module's one
hard duty is to produce a correct timezone-aware ``published_at``. A record whose
publication timestamp is missing, naive, or unparseable is rejected fail-closed:
an unprovable timestamp is unusable and is never treated as "assume now".

Record shape (PROVISIONAL — reconcile with the real chosen news API later):
``title`` (summary text), ``published_at`` or ``pubDate`` (ISO-8601 tz-aware or
RFC-1123, e.g. Naver's ``"Mon, 13 Jul 2026 09:00:00 +0900"``), ``url`` or
``link`` (identity), and an optional ``id`` (preferred stable identity).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Callable

from .brief import EvidenceItem, EvidenceProvider

# Candidate record keys, tried in order. A single accepted key per concern keeps
# this strict; the small alias lists absorb the two shapes we already know about
# (a canonical snake_case shape and Naver's camelCase ``pubDate``/``link``).
_TITLE_KEYS = ("title",)
_TIME_KEYS = ("published_at", "pubDate")
_URL_KEYS = ("url", "link")
_ID_KEYS = ("id",)

# RFC-1123 as emitted by Naver News (``pubDate``). ``%z`` parses the ``+0900``
# offset into a tz-aware datetime; the C-locale ``%a``/``%b`` tokens are English.
_RFC_1123 = "%a, %d %b %Y %H:%M:%S %z"

_DEFAULT_WINDOW_DAYS = 14

Fetch = Callable[[str, date, date], Sequence[Mapping]]


def make_news_provider(
    *,
    fetch: Fetch,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    kind: str = "news",
) -> EvidenceProvider:
    """Build an ``EvidenceProvider`` closure over an injected news source query."""
    if not callable(fetch):
        raise ValueError("fetch must be callable")
    if type(window_days) is not int or window_days <= 0:
        raise ValueError("window_days must be a positive int")
    if kind != "news":
        raise ValueError("kind must be 'news'")

    def provider(symbol: str, signal_date: date) -> tuple[EvidenceItem, ...]:
        if type(symbol) is not str or not symbol:
            raise ValueError("symbol must be a nonempty plain str")
        if type(signal_date) is not date:
            raise ValueError("signal_date must be a plain date")
        begin_date = signal_date - timedelta(days=window_days)
        records = fetch(symbol, begin_date, signal_date)
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise ValueError("fetch must return a sequence of records")
        return tuple(_to_item(record, symbol, kind) for record in records)

    return provider


def _to_item(record: object, symbol: str, kind: str) -> EvidenceItem:
    """Map one raw news record to an ``EvidenceItem``, fail-closed."""
    if not isinstance(record, Mapping):
        raise ValueError("news records must be mappings")
    summary = _required(record, _TITLE_KEYS, "title")
    published_at = _parse_published_at(_required(record, _TIME_KEYS, "publication time"))
    url_or_title = _optional(record, _URL_KEYS) or summary
    return EvidenceItem(
        evidence_id=_evidence_id(record, symbol, published_at, url_or_title),
        kind=kind,
        symbol=symbol,
        published_at=published_at,
        summary=summary,
    )


def _evidence_id(record: Mapping, symbol: str, published_at: datetime, url_or_title: str) -> str:
    """Prefer an injected record id; else a stable sha256 over time + url/title."""
    injected = _optional(record, _ID_KEYS)
    if injected:
        return f"news:{symbol}:{injected}"
    digest = hashlib.sha256(f"{published_at.isoformat()}{url_or_title}".encode()).hexdigest()
    return f"news:{symbol}:{digest[:16]}"


def _parse_published_at(value: str) -> datetime:
    """Parse an ISO-8601 tz-aware or RFC-1123 string to a tz-aware datetime."""
    parsed = _try_iso(value)
    if parsed is None:
        parsed = _try_rfc_1123(value)
    if parsed is None or parsed.tzinfo is None:
        raise ValueError("news publication time must be a timezone-aware timestamp")
    return parsed


def _try_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _try_rfc_1123(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, _RFC_1123)
    except ValueError:
        return None


def _required(record: Mapping, keys: tuple[str, ...], label: str) -> str:
    value = _optional(record, keys)
    if value is None:
        raise ValueError(f"news record is missing {label}")
    return value


def _optional(record: Mapping, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = record.get(key)
        if type(value) is str and value.strip():
            return value
    return None


if __name__ == "__main__":
    raise SystemExit("news_provider is a library module; import make_news_provider")
