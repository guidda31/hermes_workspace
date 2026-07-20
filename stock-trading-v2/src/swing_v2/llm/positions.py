"""Derive the signal-implied portfolio from accumulated forward-observation records.

Forward observation records decisions (BUY/SELL/HOLD), not a live position ledger.
The set of symbols you would be holding, if you had followed every recorded signal, is
just the running net of admitted BUYs minus admitted SELLs in signal-date order. The
daily risk watch uses this to auto-scope itself to "the names I actually hold" instead
of the whole universe. Point-in-time honest: a record dated after `as_of` is ignored.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def held_symbols_from_records(records_dir, *, as_of: date | None = None) -> tuple[str, ...]:
    """Net held symbols from forward records (admitted BUY − admitted SELL), date-ordered."""
    if as_of is not None and type(as_of) is not date:
        raise ValueError("as_of must be a plain date or None")
    directory = Path(records_dir)
    if not directory.is_dir():
        return ()
    records = []
    for path in directory.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        signal_date = date.fromisoformat(record["signal_date"])
        if as_of is not None and signal_date > as_of:
            continue
        records.append((signal_date, record))
    records.sort(key=lambda pair: pair[0])

    held: dict[str, None] = {}  # insertion-ordered set
    for _, record in records:
        admitted = set(record.get("admitted_symbols", ()))
        for decision in record.get("decisions", ()):
            symbol = decision.get("symbol")
            if symbol not in admitted:
                continue
            action = decision.get("action")
            if action == "BUY":
                held[symbol] = None
            elif action == "SELL":
                held.pop(symbol, None)
    return tuple(held)
