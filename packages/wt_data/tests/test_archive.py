import hashlib
from pathlib import Path

import httpx
import pytest

from wt_data.binance.archive import ArchiveFile, FetchStatus, days, fetch_all, months, verify_local


def test_paths_mirror_the_archive_layout() -> None:
    assert (
        ArchiveFile("klines", "BTCUSDT", "2026-10", interval="15m").path
        == "data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2026-10.zip"
    )
    assert (
        ArchiveFile("metrics", "BTCUSDT", "2026-11-20", frequency="daily").path
        == "data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-11-20.zip"
    )
    assert (
        ArchiveFile("fundingRate", "ETHUSDT", "2025-01").path
        == "data/futures/um/monthly/fundingRate/ETHUSDT/ETHUSDT-fundingRate-2025-01.zip"
    )
    with pytest.raises(ValueError, match="interval"):
        ArchiveFile("klines", "BTCUSDT", "2026-10")


def test_ranges() -> None:
    assert list(months("2025-11", "2026-02")) == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert list(days("2024-02-28", "2024-03-01")) == ["2024-02-28", "2024-02-29", "2024-03-01"]


def _transport(files: dict[str, bytes], *, corrupt: set[str] | None = None) -> httpx.MockTransport:
    corrupt = corrupt or set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        if path.endswith(".CHECKSUM"):
            data = files.get(path.removesuffix(".CHECKSUM"))
            if data is None:
                return httpx.Response(404)
            name = path.rsplit("/", 1)[-1].removesuffix(".CHECKSUM")
            return httpx.Response(200, text=f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        if path not in files:
            return httpx.Response(404)
        return httpx.Response(200, content=b"tampered" if path in corrupt else files[path])

    return httpx.MockTransport(handler)


async def test_download_verify_skip_and_missing(tmp_path: Path) -> None:
    good = ArchiveFile("klines", "BTCUSDT", "2026-09", interval="15m")
    absent = ArchiveFile("klines", "NEWUSDT", "2026-09", interval="15m")
    files = {good.path: b"zip-bytes-1"}
    results = await fetch_all([good, absent], tmp_path, transport=_transport(files))
    assert [r.status for r in results] == [FetchStatus.DOWNLOADED, FetchStatus.MISSING]
    assert (tmp_path / good.path).read_bytes() == b"zip-bytes-1"
    assert verify_local(tmp_path / good.path)
    again = await fetch_all([good], tmp_path, transport=_transport(files))
    assert again[0].status is FetchStatus.EXISTS


async def test_checksum_mismatch_is_never_written(tmp_path: Path) -> None:
    file = ArchiveFile("fundingRate", "ETHUSDT", "2026-09")
    results = await fetch_all(
        [file], tmp_path, transport=_transport({file.path: b"real"}, corrupt={file.path})
    )
    assert results[0].status is FetchStatus.CHECKSUM_MISMATCH
    assert not (tmp_path / file.path).exists()
