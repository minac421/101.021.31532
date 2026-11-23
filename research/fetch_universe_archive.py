"""Download the whole USD-M USDT-perpetual universe (delisted symbols included) from data.binance.vision.

Needed for point-in-time universe tests (H-004): a universe picked today only contains survivors.
The symbol list and each symbol's available months come from the archive's public S3 listing, so
no request is made for files that do not exist. Every file is verified against its SHA256
CHECKSUM and written atomically (wt_data.binance.archive). Only months inside --start/--end are
fetched; the lockbox (2026-08 onwards) is refused outright.

Usage:
    uv run --group research python research/fetch_universe_archive.py --end 2025-06
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import urllib.parse
from collections import Counter
from pathlib import Path

import httpx

from wt_data.binance.archive import ArchiveFile, FetchStatus, fetch_all

LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
LOCKBOX_MONTH = "2026-08"
# fiat-pegged bases: a trend rule on a pegged asset is meaningless (fixed before any result was seen)
PEGGED_BASES = {"USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD", "USDE", "PYUSD", "EUR", "GBP", "AEUR",
                "EURI", "USD1", "RLUSD", "XUSD", "BFUSD", "UST"}  # fmt: skip


def is_candidate(symbol: str) -> bool:
    """USDT-margined perpetuals only: no delivery contracts (they carry '_'), no pegged bases."""
    return symbol.endswith("USDT") and "_" not in symbol and symbol[: -len("USDT")] not in PEGGED_BASES


async def _list(client: httpx.AsyncClient, prefix: str, delimiter: bool) -> list[str]:
    """All keys (or common prefixes when delimiter=True) under prefix, following pagination."""
    out: list[str] = []
    marker = ""
    while True:
        query = {"prefix": prefix}
        if delimiter:
            query["delimiter"] = "/"
        if marker:
            query["marker"] = marker
        resp = await client.get(f"{LISTING_URL}?{urllib.parse.urlencode(query)}")
        resp.raise_for_status()
        xml = resp.text
        tag = "Prefix" if delimiter else "Key"
        found = re.findall(rf"<{tag}>([^<]+)</{tag}>", xml)
        if delimiter:
            found = [p for p in found if p != prefix]
        out += found
        if "<IsTruncated>true</IsTruncated>" not in xml or not found:
            return out
        next_marker = re.findall(r"<NextMarker>([^<]+)</NextMarker>", xml)
        marker = next_marker[0] if next_marker else found[-1]


async def plan(
    dataset: str, interval: str | None, start: str, end: str, concurrency: int
) -> list[ArchiveFile]:
    base = f"data/futures/um/monthly/{dataset}/"
    async with httpx.AsyncClient(timeout=60.0) as client:
        listed = (p.rstrip("/").split("/")[-1] for p in await _list(client, base, True))
        symbols = sorted(s for s in listed if is_candidate(s))
        semaphore = asyncio.Semaphore(concurrency)

        async def months_of(symbol: str) -> list[str]:
            prefix = f"{base}{symbol}/{interval}/" if interval else f"{base}{symbol}/"
            async with semaphore:
                keys = await _list(client, prefix, False)
            return sorted(m for k in keys if (m := _month(k)) and start <= m <= end)

        per_symbol = await asyncio.gather(*(months_of(s) for s in symbols))
    return [
        ArchiveFile(dataset, s, m, interval=interval)
        for s, ms in zip(symbols, per_symbol, strict=True)
        for m in ms
    ]


def _month(key: str) -> str | None:
    m = re.search(r"-(\d{4}-\d{2})\.zip$", key)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    ap.add_argument("--start", default="2019-09")
    ap.add_argument("--end", required=True, help="last month to fetch, e.g. 2025-06")
    ap.add_argument("--concurrency", type=int, default=16)
    args = ap.parse_args()
    if args.end >= LOCKBOX_MONTH:
        raise SystemExit(f"refusing to fetch lockbox data ({LOCKBOX_MONTH} onwards)")
    summary: dict[str, object] = {}
    for dataset, interval in (("klines", "1d"), ("fundingRate", None)):
        files = asyncio.run(plan(dataset, interval, args.start, args.end, args.concurrency))
        results = asyncio.run(fetch_all(files, args.archive, concurrency=args.concurrency))
        counts = Counter(r.status.value for r in results)
        bad = [f"{r.file.path}: {r.status.value} {r.detail}" for r in results
               if r.status not in (FetchStatus.DOWNLOADED, FetchStatus.EXISTS)]  # fmt: skip
        summary[f"{dataset}/{interval or '-'}"] = {
            "symbols": len({f.symbol for f in files}),
            "files": len(files),
            "status": dict(counts),
            "problems": bad[:20],
        }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
