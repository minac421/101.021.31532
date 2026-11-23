"""Download the whole USD-M USDT-perpetual universe (delisted symbols included) from data.binance.vision.

Needed for point-in-time universe tests (H-004): a universe picked today only contains survivors.
The symbol list and each symbol's available months come from the archive's public S3 listing, so
no request is made for files that do not exist. Every file is verified against its SHA256
CHECKSUM and written atomically (wt_data.binance.archive). Only months inside --start/--end are
fetched; the lockbox (2026-08 onwards) is refused outright.

Some monthly files are truncated (e.g. 49 symbols lack 2022-02-26..28 and 2022-04-01..02 although
they traded): the "daily-gaps" step fills every internal gap, and any days after a symbol's last
monthly bar, from the official daily files when they exist. Days missing from both are left missing.

Usage:
    uv run --group research python research/fetch_universe_archive.py --end 2025-06
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import re
import urllib.parse
import zipfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import httpx

from wt_data.binance.archive import (
    ARCHIVE_BASE_URL,
    ArchiveFile,
    FetchResult,
    FetchStatus,
    fetch_all,
    fetch_file,
)

LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
LOCKBOX_MONTH = "2026-08"
# fiat-pegged bases: a trend rule on a pegged asset is meaningless (fixed before any result was seen)
PEGGED_BASES = {"USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD", "USDE", "PYUSD", "EUR", "GBP", "AEUR",
                "EURI", "USD1", "RLUSD", "XUSD", "BFUSD", "UST"}  # fmt: skip


def is_candidate(symbol: str) -> bool:
    """USDT-margined perpetuals only: no delivery contracts (they carry '_'), no pegged bases."""
    return symbol.endswith("USDT") and "_" not in symbol and symbol[: -len("USDT")] not in PEGGED_BASES


async def _get_with_retry(client: httpx.AsyncClient, url: str, attempts: int = 5) -> httpx.Response:
    """GET with exponential backoff on transport errors and 5xx (listing calls are idempotent)."""
    for attempt in range(attempts):
        try:
            resp = await client.get(url)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
        await asyncio.sleep(2**attempt)
    raise RuntimeError(f"listing failed after {attempts} attempts: {url}")


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
        resp = await _get_with_retry(client, f"{LISTING_URL}?{urllib.parse.urlencode(query)}")
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
    dataset: str,
    interval: str | None,
    start: str,
    end: str,
    concurrency: int,
    only: set[str] | None = None,
) -> list[ArchiveFile]:
    base = f"data/futures/um/monthly/{dataset}/"
    async with httpx.AsyncClient(timeout=60.0) as client:
        listed = (p.rstrip("/").split("/")[-1] for p in await _list(client, base, True))
        symbols = sorted(s for s in listed if is_candidate(s) and (only is None or s in only))
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


def monthly_days(root: Path, symbol: str, interval: str = "1d") -> set[date]:
    """Days that have at least one bar in the local monthly kline files of symbol."""
    out: set[date] = set()
    for path in sorted((root / f"data/futures/um/monthly/klines/{symbol}/{interval}").glob("*.zip")):
        with zipfile.ZipFile(path) as z:
            text = z.read(z.namelist()[0]).decode()
        for rec in csv.reader(io.StringIO(text)):
            if rec and rec[0].isdigit():
                t = int(rec[0])
                out.add(date(1970, 1, 1) + timedelta(milliseconds=t // 1000 if t > 10**14 else t))
    return out


async def fill_daily_gaps(
    root: Path,
    start: str,
    end: str,
    concurrency: int,
    interval: str = "1d",
    only: set[str] | None = None,
) -> list[FetchResult]:
    """Fetch daily kline files for internal gaps and for days after the last monthly bar (until a 404)."""
    first_day = date.fromisoformat(f"{start}-01")
    year, month = (int(x) for x in end.split("-"))
    stop = date(year + month // 12, month % 12 + 1, 1)  # first day after the last fetched month
    base = root / "data/futures/um/monthly/klines"
    semaphore = asyncio.Semaphore(concurrency)
    results: list[FetchResult] = []
    async with httpx.AsyncClient(base_url=ARCHIVE_BASE_URL, timeout=60.0) as client:

        async def one(symbol: str) -> None:
            have = {d for d in monthly_days(root, symbol, interval) if first_day <= d < stop}
            if not have:
                return
            lo, hi = min(have), max(have)
            missing = [lo + timedelta(days=k) for k in range((hi - lo).days + 1)]
            for day in (d for d in missing if d not in have):
                async with semaphore:
                    results.append(await fetch_file(client, _daily(symbol, day, interval), root))
            day = hi + timedelta(days=1)
            while day < stop:  # a truncated last month looks like a delisting: probe until the first 404
                async with semaphore:
                    result = await fetch_file(client, _daily(symbol, day, interval), root)
                results.append(result)
                if result.status not in (FetchStatus.DOWNLOADED, FetchStatus.EXISTS):
                    break
                day += timedelta(days=1)

        symbols = sorted(
            p.name
            for p in base.iterdir()
            if is_candidate(p.name) and (p / interval).is_dir() and (only is None or p.name in only)
        )
        await asyncio.gather(*(one(s) for s in symbols))
    return results


def _daily(symbol: str, day: date, interval: str) -> ArchiveFile:
    return ArchiveFile("klines", symbol, day.isoformat(), frequency="daily", interval=interval)


def _summary(results: list[FetchResult]) -> dict[str, object]:
    bad = [f"{r.file.path}: {r.status.value} {r.detail}" for r in results
           if r.status not in (FetchStatus.DOWNLOADED, FetchStatus.EXISTS, FetchStatus.MISSING)]  # fmt: skip
    return {
        "symbols": len({r.file.symbol for r in results}),
        "files": len(results),
        "status": dict(Counter(r.status.value for r in results)),
        "problems": bad[:20],
    }


STEPS = ("monthly-klines", "daily-gaps", "funding")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    ap.add_argument("--start", default="2019-09")
    ap.add_argument("--end", required=True, help="last month to fetch, e.g. 2025-06")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma-separated subset of {STEPS}")
    ap.add_argument("--interval", default="1d", help="kline interval for the kline steps")
    ap.add_argument("--symbols-file", type=Path, help="restrict to these symbols (one per line)")
    args = ap.parse_args()
    only = set(args.symbols_file.read_text().split()) if args.symbols_file else None
    if args.end >= LOCKBOX_MONTH:
        raise SystemExit(f"refusing to fetch lockbox data ({LOCKBOX_MONTH} onwards)")
    steps = args.steps.split(",")
    if unknown := set(steps) - set(STEPS):
        raise SystemExit(f"unknown steps: {sorted(unknown)}")
    summary: dict[str, object] = {}
    for step in STEPS:
        if step not in steps:
            continue
        if step == "daily-gaps":
            results = asyncio.run(
                fill_daily_gaps(args.archive, args.start, args.end, args.concurrency, args.interval, only)
            )
        else:
            dataset, interval = (
                ("klines", args.interval) if step == "monthly-klines" else ("fundingRate", None)
            )
            files = asyncio.run(plan(dataset, interval, args.start, args.end, args.concurrency, only))
            results = asyncio.run(fetch_all(files, args.archive, concurrency=args.concurrency))
        summary[step] = _summary(results)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
