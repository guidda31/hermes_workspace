"""The concrete boundary of the ``decide`` seam: Brief -> prompt, and reply -> dicts.

The repository calls no LLM. A Hermes routine renders a brief into the prompt text
below, the agent reasons, and its textual reply is parsed here — fail-closed — into
raw decision mappings that feed the existing ``parse_decision_set`` / guardrail path.
Neither function performs any I/O, network, or order action.

Keeping this glue in the repo (rather than as free-form routine text) makes the agent
contract testable and stable: the prompt always states the exact JSON schema, the
citable evidence ids, and the hard constraints the guardrail will independently
enforce, so a well-behaved agent and the code agree on the same rules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
import json

from .brief import Brief, SymbolBrief
from .guardrail import PortfolioContext


def _fmt(value: Decimal | None) -> str:
    return "n/a" if value is None else format(value.normalize(), "f")


def _symbol_block(symbol_brief: SymbolBrief, held: bool) -> str:
    lines = [
        f"- {symbol_brief.symbol} ({symbol_brief.asset_type}){' [HELD]' if held else ''}",
        f"    close={_fmt(symbol_brief.latest_close)} "
        f"trading_value={_fmt(symbol_brief.latest_trading_value)} "
        f"liquidity_pass={symbol_brief.liquidity_pass}",
        f"    ma20={_fmt(symbol_brief.moving_average_20)} ma60={_fmt(symbol_brief.moving_average_60)} "
        f"ret20={_fmt(symbol_brief.return_20)} ret60={_fmt(symbol_brief.return_60)}",
        f"    price_evidence_id={symbol_brief.price_evidence_id}",
    ]
    for item in symbol_brief.evidence:
        lines.append(
            f"    evidence {item.evidence_id} [{item.kind} @ {item.published_at.isoformat()}]: {item.summary}"
        )
    return "\n".join(lines)


_SCHEMA_INSTRUCTIONS = """\
Respond with ONLY a JSON array (no prose). Each element:
  {"symbol": <one listed symbol>, "action": "BUY"|"SELL"|"HOLD",
   "conviction": <decimal 0..1>, "target_weight": <decimal 0..1>,
   "rationale": <short string>, "cited_evidence": [<evidence_id>, ...]}
Hard rules (the system will also enforce these and reject violations):
- Propose only symbols listed above; cite only the evidence_ids listed above.
- BUY requires conviction>0 and target_weight>0. SELL means a full exit: target_weight=0.
- HOLD/SELL only for a symbol currently HELD. One position per symbol.
- Do not assume any information beyond this brief; it is point-in-time as of the signal date.
- Return [] if no action is warranted."""


def render_brief_prompt(brief: Brief, *, portfolio: PortfolioContext) -> str:
    """Render the point-in-time brief into the prompt text the agent reasons over."""
    if not isinstance(brief, Brief):
        raise ValueError("brief must be a Brief")
    if not isinstance(portfolio, PortfolioContext):
        raise ValueError("portfolio must be a PortfolioContext")

    held = portfolio.held_symbols
    held_line = ", ".join(sorted(held)) if held else "(none)"
    entry_state = (
        "NEW ENTRIES BLOCKED (daily-loss guard / risk-off): propose no BUY; SELL/HOLD only."
        if portfolio.new_entries_blocked
        else "New entries allowed."
    )
    symbol_blocks = "\n".join(
        _symbol_block(s, s.symbol in held) for s in brief.symbols
    )
    return (
        "You are a KRX swing-trading analyst. Decide next-session actions from the "
        "point-in-time brief below. Signals act at the next session open; you never "
        "see future data.\n\n"
        f"Signal date (KST): {brief.signal_date.isoformat()}\n"
        f"Market {brief.market.symbol}: close={_fmt(brief.market.latest_close)}, "
        f"risk_on={brief.market.is_risk_on} (evidence {brief.market.price_evidence_id})\n"
        f"Held positions: {held_line}\n"
        f"Entry state: {entry_state}\n\n"
        "Candidate universe:\n"
        f"{symbol_blocks}\n\n"
        f"{_SCHEMA_INSTRUCTIONS}\n"
    )


def _extract_json_text(text: str) -> str:
    """Return the JSON substring from a raw agent reply, tolerating code fences/prose."""
    fence = "```"
    if fence in text:
        start = text.find(fence)
        after = text.find("\n", start)
        end = text.find(fence, after + 1)
        if after != -1 and end != -1:
            body = text[after + 1:end].strip()
            if body.lower().startswith("json"):
                body = body[4:].strip()
            return body
    stripped = text.strip()
    # Fall back to the outermost array or object in the reply.
    for opener, closer in (("[", "]"), ("{", "}")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            return stripped[start:end + 1]
    return stripped


def parse_agent_response(text: object) -> list[dict]:
    """Parse the agent's textual reply into a list of raw decision mappings, fail-closed."""
    if type(text) is not str or not text.strip():
        raise ValueError("agent response must be a nonempty str")
    try:
        parsed = json.loads(_extract_json_text(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("agent response did not contain parseable JSON") from exc

    if isinstance(parsed, Mapping):
        parsed = parsed.get("decisions")
        if parsed is None:
            raise ValueError("agent response object must carry a 'decisions' array")
    if isinstance(parsed, (str, bytes)) or not isinstance(parsed, Sequence):
        raise ValueError("agent response must resolve to a JSON array of decisions")
    decisions: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, Mapping):
            raise ValueError("each agent decision must be a JSON object")
        decisions.append(dict(entry))
    return decisions
