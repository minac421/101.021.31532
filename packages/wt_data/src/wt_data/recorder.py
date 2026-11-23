"""Append-only raw recorder: zstd-compressed JSON Lines, partitioned by kind and UTC hour.

Layout (docs/04-databases.md §6)::

    {root}/{source}/{kind}/date=YYYY-MM-DD/hour=HH/{instance}-{part:03d}.jsonl.zst

Each line is an envelope around the *exact* bytes received::

    {"recv_ns": <wall ns>, "mono_ns": <monotonic ns>, "origin": "<who/where>", "payload": <raw JSON>}

Design choices:

* the producer side (the asyncio event loop) never blocks: records go into a
  bounded queue and a dedicated writer thread does compression and disk I/O;
  if the queue is full the record is dropped and counted (and the data-quality
  monitor turns that into DEGRADED) — blocking the WebSocket reader instead would
  make the exchange disconnect us and lose far more;
* every flush ends a zstd frame, so a crash loses at most ``flush_interval_s``
  of data and every file remains readable up to its last complete frame;
* files are never rewritten; a file name is unique per process instance, so a
  restart can never overwrite earlier data.
"""

from __future__ import annotations

import io
import logging
import os
import queue
import re
import shutil
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec
import zstandard

from wt_core.clock import NS_PER_S, hour_floor_s, utc_from_ns

log = logging.getLogger(__name__)

_ORIGIN_RE = re.compile(r"^[A-Za-z0-9_.:@!/#?=&%,+-]{1,512}$")  # JSON-safe without escaping
_PATH_PART_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def envelope_line(*, recv_ns: int, mono_ns: int, origin: str, payload: bytes) -> bytes:
    """Build one JSON line around a raw JSON payload without re-serialising it."""
    if not _ORIGIN_RE.match(origin):
        raise ValueError(f"origin contains characters that are not JSON-safe: {origin!r}")
    body = payload.strip()
    if b"\n" in body or b"\r" in body:
        body = msgspec.json.encode(msgspec.json.decode(body))  # rare: multi-line JSON -> compact
    return b'{"recv_ns":%d,"mono_ns":%d,"origin":"%s","payload":%s}\n' % (
        recv_ns,
        mono_ns,
        origin.encode(),
        body,
    )


@dataclass(frozen=True, slots=True)
class _Item:
    kind: str
    hour_s: int
    line: bytes


@dataclass(slots=True)
class RecorderStats:
    # producer-thread fields
    enqueued: int = 0
    dropped: int = 0
    # writer-thread fields
    written_lines: int = 0
    written_bytes: int = 0
    files_opened: int = 0
    write_errors: int = 0


class _PartitionWriter:
    def __init__(self, path: Path, level: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = open(path, "xb")  # noqa: SIM115 — lifetime managed explicitly; "x" never overwrites
        self._zw = zstandard.ZstdCompressor(level=level).stream_writer(self._fh, closefd=False)
        self.dirty = False
        self.last_write_mono = time.monotonic()

    def write(self, line: bytes) -> None:
        self._zw.write(line)
        self.dirty = True
        self.last_write_mono = time.monotonic()

    def flush(self) -> None:
        if self.dirty:
            self._zw.flush(zstandard.FLUSH_FRAME)
            self._fh.flush()
            self.dirty = False

    def size(self) -> int:
        return self._fh.tell()

    def close(self) -> None:
        self._zw.close()  # type: ignore[no-untyped-call]
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()


class RawRecorder:
    def __init__(
        self,
        root: Path,
        *,
        source: str,
        zstd_level: int = 3,
        flush_interval_s: float = 2.0,
        queue_max: int = 200_000,
        close_grace_s: float = 300.0,
        instance_id: str | None = None,
    ) -> None:
        if not _PATH_PART_RE.match(source):
            raise ValueError(f"invalid source name {source!r}")
        self.root = root
        self.source = source
        self.stats = RecorderStats()
        raw_instance = instance_id or f"{socket.gethostname()}-{os.getpid()}-{time.time_ns()}"
        self.instance_id = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_instance)[:100]
        self._level = zstd_level
        self._flush_interval_s = flush_interval_s
        self._close_grace_s = close_grace_s
        self._queue: queue.Queue[_Item] = queue.Queue(maxsize=queue_max)
        self._writers: dict[tuple[str, int], _PartitionWriter] = {}
        self._parts: dict[tuple[str, int], int] = {}
        self._closed_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ producer API
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("recorder already started")
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="raw-recorder", daemon=True)
        self._thread.start()

    def record(self, kind: str, *, recv_ns: int, mono_ns: int, origin: str, payload: bytes) -> bool:
        """Queue one raw record. Never blocks; returns False if the record had to be dropped."""
        if not _PATH_PART_RE.match(kind):
            raise ValueError(f"invalid record kind {kind!r}")
        item = _Item(
            kind,
            hour_floor_s(recv_ns),
            envelope_line(recv_ns=recv_ns, mono_ns=mono_ns, origin=origin, payload=payload),
        )
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.stats.dropped += 1
            return False
        self.stats.enqueued += 1
        return True

    def record_event(
        self, kind: str, *, recv_ns: int, mono_ns: int, origin: str, event: Mapping[str, Any]
    ) -> bool:
        """Record a control/quality event generated locally (encoded as compact JSON)."""
        return self.record(
            kind, recv_ns=recv_ns, mono_ns=mono_ns, origin=origin, payload=msgspec.json.encode(event)
        )

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def disk_free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def close(self, timeout_s: float = 30.0) -> None:
        """Flush everything that was queued and close all files."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                log.error(
                    "raw recorder did not finish within %.0fs; %d records still queued",
                    timeout_s,
                    self.queue_depth,
                )

    # ------------------------------------------------------------------ writer thread
    def _run(self) -> None:
        last_flush = time.monotonic()
        while True:
            try:
                item: _Item | None = self._queue.get(timeout=0.25)
            except queue.Empty:
                item = None
            if item is not None:
                self._write(item)
                for _ in range(10_000):  # drain in batches without re-waiting
                    try:
                        self._write(self._queue.get_nowait())
                    except queue.Empty:
                        break
            now = time.monotonic()
            if now - last_flush >= self._flush_interval_s:
                self._flush_all()
                self._close_finished_partitions()
                last_flush = now
            if self._stop.is_set() and self._queue.empty():
                break
        for key in list(self._writers):
            self._close_partition(key)

    def _write(self, item: _Item) -> None:
        key = (item.kind, item.hour_s)
        try:
            writer = self._writers.get(key)
            if writer is None:
                writer = self._open_partition(key)
            writer.write(item.line)
            self.stats.written_lines += 1
        except OSError:
            self.stats.write_errors += 1
            log.exception("raw recorder write failed for %s", key)

    def _open_partition(self, key: tuple[str, int]) -> _PartitionWriter:
        kind, hour_s = key
        part = self._parts.get(key, -1) + 1
        self._parts[key] = part
        hour = utc_from_ns(hour_s * NS_PER_S)
        path = (
            self.root
            / self.source
            / kind
            / f"date={hour:%Y-%m-%d}"
            / f"hour={hour:%H}"
            / f"{self.instance_id}-{part:03d}.jsonl.zst"
        )
        writer = _PartitionWriter(path, self._level)
        self._writers[key] = writer
        self.stats.files_opened += 1
        return writer

    def _flush_all(self) -> None:
        for key, writer in list(self._writers.items()):
            try:
                writer.flush()
            except OSError:
                self.stats.write_errors += 1
                log.exception("raw recorder flush failed for %s", key)
        self.stats.written_bytes = self._closed_bytes + sum(w.size() for w in self._writers.values())

    def _close_finished_partitions(self) -> None:
        current_hour = hour_floor_s(time.time_ns())
        now = time.monotonic()
        for key, writer in list(self._writers.items()):
            if key[1] < current_hour and now - writer.last_write_mono > self._close_grace_s:
                self._close_partition(key)

    def _close_partition(self, key: tuple[str, int]) -> None:
        writer = self._writers.pop(key)
        try:
            writer.flush()
            self._closed_bytes += writer.size()
            writer.close()
        except OSError:
            self.stats.write_errors += 1
            log.exception("raw recorder close failed for %s", key)


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Read a recorder file, tolerating a truncated final line (e.g. after a crash)."""
    with open(path, "rb") as fh:
        reader = zstandard.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
        text = io.TextIOWrapper(reader, encoding="utf-8")
        try:
            for line in text:
                if not line.endswith("\n"):
                    return  # incomplete trailing record
                yield msgspec.json.decode(line)
        except zstandard.ZstdError:
            return  # truncated final frame
