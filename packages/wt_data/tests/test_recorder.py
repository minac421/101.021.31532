import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wt_core.clock import NS_PER_S
from wt_data.recorder import RawRecorder, envelope_line, read_records


def _ns(hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 11, 23, hour, minute, tzinfo=UTC).timestamp()) * NS_PER_S


def _files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.jsonl.zst"))


def test_envelope_keeps_payload_bytes_exactly() -> None:
    payload = b'{"stream":"btcusdt@aggTrade","data":{"p":"97000.10","q":"0.001"}}'
    line = envelope_line(recv_ns=1, mono_ns=2, origin="market-aggTrade-00/s1", payload=payload)
    assert line == b'{"recv_ns":1,"mono_ns":2,"origin":"market-aggTrade-00/s1","payload":' + payload + b"}\n"


def test_envelope_rejects_unsafe_origin_and_normalises_multiline_json() -> None:
    with pytest.raises(ValueError, match="JSON-safe"):
        envelope_line(recv_ns=1, mono_ns=2, origin='bad"origin', payload=b"{}")
    line = envelope_line(recv_ns=1, mono_ns=2, origin="x", payload=b'{\n  "a": 1\n}\n')
    assert line.endswith(b'"payload":{"a":1}}\n')


def test_records_round_trip_partitioned_by_kind_and_hour(tmp_path: Path) -> None:
    rec = RawRecorder(tmp_path, source="binance-um", flush_interval_s=0.05, instance_id="test")
    rec.start()
    for i in range(100):
        rec.record("ws.aggTrade", recv_ns=_ns(10) + i, mono_ns=i, origin="conn/s1", payload=b'{"i":%d}' % i)
    rec.record(
        "rest.depth", recv_ns=_ns(11, 5), mono_ns=0, origin="GET:/fapi/v1/depth?symbol=BTCUSDT", payload=b"{}"
    )
    rec.close()
    files = _files(tmp_path)
    rel = [str(f.relative_to(tmp_path)) for f in files]
    assert rel == [
        "binance-um/rest.depth/date=2026-11-23/hour=11/test-000.jsonl.zst",
        "binance-um/ws.aggTrade/date=2026-11-23/hour=10/test-000.jsonl.zst",
    ]
    records = list(read_records(files[1]))
    assert [r["payload"]["i"] for r in records] == list(range(100))
    assert records[0]["origin"] == "conn/s1"
    assert rec.stats.written_lines == 101
    assert rec.stats.dropped == 0


def test_data_is_readable_before_close(tmp_path: Path) -> None:
    """A crash after a flush must not lose flushed data: files are multi-frame zstd."""
    rec = RawRecorder(tmp_path, source="binance-um", flush_interval_s=0.05, instance_id="live")
    rec.start()
    try:
        rec.record("ws.depth", recv_ns=time.time_ns(), mono_ns=0, origin="c", payload=b'{"n":1}')
        deadline = time.monotonic() + 5
        records: list[dict[str, object]] = []
        while time.monotonic() < deadline and not records:
            time.sleep(0.05)
            files = _files(tmp_path)
            records = list(read_records(files[0])) if files else []
        assert records
        assert records[0]["payload"] == {"n": 1}
    finally:
        rec.close()


def test_full_queue_drops_instead_of_blocking(tmp_path: Path) -> None:
    rec = RawRecorder(tmp_path, source="binance-um", queue_max=1, instance_id="q")
    assert rec.record("ws.depth", recv_ns=1, mono_ns=1, origin="c", payload=b"{}")
    assert not rec.record("ws.depth", recv_ns=1, mono_ns=1, origin="c", payload=b"{}")
    assert rec.stats.dropped == 1


def test_files_are_never_overwritten(tmp_path: Path) -> None:
    for _ in range(2):
        rec = RawRecorder(tmp_path, source="binance-um", instance_id="same")
        rec.start()
        rec.record("control", recv_ns=_ns(9), mono_ns=0, origin="collector", payload=b'{"event":"x"}')
        rec.close()
    # the second instance with the same id cannot reuse the existing file name -> write error, not overwrite
    files = _files(tmp_path)
    assert len(files) == 1
    assert len(list(read_records(files[0]))) == 1


def test_invalid_kind_is_rejected(tmp_path: Path) -> None:
    rec = RawRecorder(tmp_path, source="binance-um")
    with pytest.raises(ValueError, match="invalid record kind"):
        rec.record("../escape", recv_ns=1, mono_ns=1, origin="c", payload=b"{}")
