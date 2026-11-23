"""Binance public data archive (data.binance.vision): verified, idempotent downloads.

Every archive file ``X.zip`` has a sibling ``X.zip.CHECKSUM`` holding its SHA256.
A file is only written to disk if its SHA256 matches; writes are atomic (temp file
+ rename), so an interrupted backfill never leaves a corrupt file that a later run
would trust. Missing files (HTTP 404 — e.g. a symbol not yet listed that month)
are reported, not treated as errors.

Paths mirror the archive, e.g.
``data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2026-10.zip``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

import httpx

ARCHIVE_BASE_URL = "https://data.binance.vision"
KLINE_LIKE = {"klines", "markPriceKlines", "indexPriceKlines", "premiumIndexKlines"}


class FetchStatus(StrEnum):
    DOWNLOADED = "downloaded"
    EXISTS = "exists"  # already on disk and verified
    MISSING = "missing"  # 404: not published (e.g. symbol not listed yet)
    CHECKSUM_MISMATCH = "checksum_mismatch"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    dataset: str  # klines, fundingRate, metrics, aggTrades, bookDepth, ...
    symbol: str
    period: str  # "2026-10" (monthly) or "2026-10-31" (daily)
    frequency: str = "monthly"  # monthly | daily
    interval: str | None = None  # kline-like datasets only, e.g. "15m"
    market: str = "futures/um"

    def __post_init__(self) -> None:
        if self.frequency not in ("monthly", "daily"):
            raise ValueError("frequency must be 'monthly' or 'daily'")
        if (self.dataset in KLINE_LIKE) != (self.interval is not None):
            raise ValueError(
                f"{self.dataset}: interval is required for kline-like datasets and only for them"
            )

    @property
    def path(self) -> str:
        middle = f"{self.symbol}/{self.interval}" if self.interval else self.symbol
        name = f"{self.symbol}-{self.interval or self.dataset}-{self.period}.zip"
        return f"data/{self.market}/{self.frequency}/{self.dataset}/{middle}/{name}"


@dataclass(frozen=True, slots=True)
class FetchResult:
    file: ArchiveFile
    status: FetchStatus
    detail: str = ""


def months(start: str, end: str) -> Iterator[str]:
    """Inclusive month range: months("2025-11", "2026-01") -> 2025-11, 2025-12, 2026-01."""
    year, month = (int(x) for x in start.split("-"))
    end_year, end_month = (int(x) for x in end.split("-"))
    while (year, month) <= (end_year, end_month):
        yield f"{year:04d}-{month:02d}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def days(start: str, end: str) -> Iterator[str]:
    """Inclusive day range in ISO format."""
    current, last = date.fromisoformat(start), date.fromisoformat(end)
    while current <= last:
        yield current.isoformat()
        current += timedelta(days=1)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def verify_local(dest: Path) -> bool:
    checksum = dest.with_name(dest.name + ".CHECKSUM")
    if not dest.exists() or not checksum.exists():
        return False
    expected = checksum.read_text().split()[0].strip().lower()
    return _sha256(dest.read_bytes()) == expected


async def fetch_file(client: httpx.AsyncClient, file: ArchiveFile, root: Path) -> FetchResult:
    dest = root / file.path
    if verify_local(dest):
        return FetchResult(file, FetchStatus.EXISTS)
    try:
        checksum_resp = await client.get(f"/{file.path}.CHECKSUM")
        if checksum_resp.status_code == 404:
            return FetchResult(file, FetchStatus.MISSING)
        checksum_resp.raise_for_status()
        expected = checksum_resp.text.split()[0].strip().lower()
        data_resp = await client.get(f"/{file.path}")
        if data_resp.status_code == 404:
            return FetchResult(file, FetchStatus.MISSING)
        data_resp.raise_for_status()
    except httpx.HTTPError as exc:
        return FetchResult(file, FetchStatus.ERROR, repr(exc)[:200])
    actual = _sha256(data_resp.content)
    if actual != expected:
        return FetchResult(file, FetchStatus.CHECKSUM_MISMATCH, f"expected {expected}, got {actual}")
    _write_atomic(dest, data_resp.content)
    _write_atomic(dest.with_name(dest.name + ".CHECKSUM"), checksum_resp.content)
    return FetchResult(file, FetchStatus.DOWNLOADED)


async def fetch_all(
    files: Iterable[ArchiveFile],
    root: Path,
    *,
    concurrency: int = 8,
    base_url: str = ARCHIVE_BASE_URL,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = 60.0,
) -> list[FetchResult]:
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout_s) as client:

        async def one(file: ArchiveFile) -> FetchResult:
            async with semaphore:
                return await fetch_file(client, file, root)

        return await asyncio.gather(*(one(f) for f in files))
