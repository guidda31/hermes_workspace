"""Prepare a forward-observation snapshot for a signal date (collect + rebuild).

Encapsulates the deterministic daily steps so the Hermes/analyst only has to render,
decide, and record. READ-ONLY market data (KIS daily-price + KOSPI index); never any
order/balance/account endpoint. Run after the KRX close on a real trading day.

Usage (from stock-trading-v2/):
    PYTHONPATH=src .venv/bin/python deploy/forward_prepare.py --signal-date 2026-07-20
    # then: forward_cli render/record with the printed snapshot path.

    # dry-run against already-collected data (no KIS calls):
    PYTHONPATH=src .venv/bin/python deploy/forward_prepare.py --signal-date 2026-07-16 \
        --skip-collect --collection-dir data/kis-universe45-2023-07-17_2026-07-17
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from swing_v2.backtest_data import DailyBarSnapshot, SnapshotMetadata, save_snapshot
from swing_v2.contracts import DailyBar

_WARMUP_START = date(2023, 7, 17)  # ~3y so the 200-day KOSPI regime is warm at any recent date


def _bars(path: Path) -> list[DailyBar]:
    return [DailyBar.from_mapping(b) for b in json.loads(path.read_text(encoding="utf-8"))["bars"]]


def _collect(symbols: list[str], signal_date: date, collection_dir: Path) -> None:
    from swing_v2.kis import KisClient, KisCredentials
    from swing_v2.kis_market_index import collect_kospi_market_index_snapshot
    from swing_v2.kis_snapshot_collector import collect_daily_snapshot

    load_dotenv()
    app_key, app_secret = os.getenv("KIS_APP_KEY"), os.getenv("KIS_APP_SECRET")
    if not app_key or not app_secret:
        raise SystemExit("KIS_APP_KEY / KIS_APP_SECRET must be set in .env")
    client = KisClient(credentials=KisCredentials(app_key=app_key, app_secret=app_secret))
    cache = os.getenv("KIS_TOKEN_CACHE")
    token = client.get_access_token(cache_path=Path(cache) if cache else None)
    collect_daily_snapshot(
        client=client, access_token=token, symbols=tuple(symbols),
        asset_types={s: "STOCK" for s in symbols}, start=_WARMUP_START, end=signal_date,
        output_path=collection_dir, delay_seconds=0.25, max_symbols=len(symbols) + 1, max_requests=600,
    )
    if not (collection_dir / "KOSPI.json").exists():
        collect_kospi_market_index_snapshot(
            client=client, access_token=token, start=_WARMUP_START, end=signal_date,
            output_path=collection_dir, delay_seconds=0.25, max_requests=80,
        )


def _rebuild(symbols: list[str], collection_dir: Path, signal_date: date) -> Path:
    kospi = _bars(collection_dir / "KOSPI.json")
    calendar = [b.trade_date for b in kospi if b.trade_date <= signal_date]
    calset = set(calendar)
    as_of = calendar[-1]
    histories = {
        s: [b for b in _bars(collection_dir / f"{s}.json") if b.trade_date in calset]
        for s in symbols
    }
    meta = SnapshotMetadata(
        source="KIS domestic daily (forward universe, research)",
        retrieved_at=f"{as_of.isoformat()}T16:00:00+09:00",
        data_as_of=as_of.isoformat(), trading_value_is_close_times_volume_proxy=False,
    )
    snap = DailyBarSnapshot(
        metadata=meta, market_symbol="KOSPI", asset_types={s: "STOCK" for s in symbols},
        trade_calendar=[d for d in calendar], histories=histories,
        market_history=[b for b in kospi if b.trade_date <= signal_date],
    )
    out = Path("data/snapshots") / f"forward-{signal_date.isoformat()}.json"
    save_snapshot(snap, out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal-date", required=True, type=date.fromisoformat)
    parser.add_argument("--universe", default="data/universe-symbols.json")
    parser.add_argument("--collection-dir", default=None)
    parser.add_argument("--skip-collect", action="store_true")
    args = parser.parse_args()

    symbols = json.loads(Path(args.universe).read_text(encoding="utf-8"))
    collection_dir = Path(args.collection_dir) if args.collection_dir else Path("data/kis-live")
    if not args.skip_collect:
        print(f"수집: {len(symbols)}종목 + KOSPI, {_WARMUP_START}~{args.signal_date} → {collection_dir}")
        _collect(symbols, args.signal_date, collection_dir)
    snapshot_path = _rebuild(symbols, collection_dir, args.signal_date)
    csv = ",".join(symbols)
    print(f"\n스냅샷: {snapshot_path}")
    print("다음 단계:")
    print(f"  render: PYTHONPATH=src .venv/bin/python -m swing_v2.llm.forward_cli render \\")
    print(f"          --snapshot {snapshot_path} --signal-date {args.signal_date} --symbols {csv} --window 200")
    print(f"  record: ...forward_cli record --snapshot {snapshot_path} --signal-date {args.signal_date} \\")
    print(f"          --symbols {csv} --eligible {csv} --model-id <you> --reply-file <hermes.json> \\")
    print(f"          --output data/forward-records/signal-{args.signal_date}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
