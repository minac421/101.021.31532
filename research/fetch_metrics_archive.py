"""Download the archive's 5-minute open-interest snapshots ("metrics") for the point-in-time universe.

Only the symbol-days where the symbol belonged to the H-004 universe (top 20 by trailing 30-day
quote volume at the previous day's close) are fetched: that is all H-005 can use, and it keeps the
download at ~20 files per day instead of ~500. Files are SHA256-verified and written atomically.
Also writes the list of symbols ever in the universe (input for the hourly-kline download).

Usage:
    uv run --group research python research/fetch_metrics_archive.py --start 2020-09-01 --end 2025-06-30 \\
        --write-symbols data/archive/pit_universe_symbols.txt
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType

from wt_data.binance.archive import ArchiveFile, FetchStatus, fetch_all


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    ap.add_argument("--start", required=True, help="first day, e.g. 2020-09-01")
    ap.add_argument("--end", required=True, help="last day, e.g. 2025-06-30")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--write-symbols", type=Path)
    args = ap.parse_args()
    h4 = _load("h004_tsmom_pit")
    if date.fromisoformat(args.end) >= date(2025, 7, 1):
        raise SystemExit("refusing to fetch beyond 2025-06-30 (validation year and lockbox)")
    symbols, days, a = h4.build_panel(args.archive)
    universe = h4.pit_universe(a["close"], a["qvol"])
    epoch = date(1970, 1, 1)
    first, last = date.fromisoformat(args.start), date.fromisoformat(args.end)
    files = []
    for i in range(1, len(days)):
        day = epoch + timedelta(days=int(days[i]))
        if first <= day <= last:
            for j in universe[i - 1].nonzero()[0]:  # universe known at the previous close
                files.append(ArchiveFile("metrics", symbols[j], day.isoformat(), frequency="daily"))
    results = asyncio.run(fetch_all(files, args.archive, concurrency=args.concurrency))
    ever = sorted({symbols[j] for j in universe.any(axis=0).nonzero()[0]})
    if args.write_symbols:
        args.write_symbols.parent.mkdir(parents=True, exist_ok=True)
        args.write_symbols.write_text("\n".join(ever) + "\n")
    ok = (FetchStatus.DOWNLOADED, FetchStatus.EXISTS, FetchStatus.MISSING)
    problems = [f"{r.file.path}: {r.status.value} {r.detail}" for r in results if r.status not in ok]
    summary = {
        "files": len(files),
        "status": dict(Counter(r.status.value for r in results)),
        "symbols_ever_in_universe": len(ever),
        "problems": problems[:20],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
