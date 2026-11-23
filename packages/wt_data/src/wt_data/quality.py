"""Data-quality tracking: per-stream message rates, exchange-to-receive lag, and
sequence continuity with a reorder window (tolerant of A/B socket overlap)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from wt_core.clock import NS_PER_MS, NS_PER_S, Stamp


class SeqResult(StrEnum):
    OK = "OK"
    DUPLICATE = "DUPLICATE"
    LATE_FILL = "LATE_FILL"  # an id previously seen as missing arrived (e.g. from the other socket)
    GAP_OPENED = "GAP_OPENED"


class SequenceTracker:
    """Checks that ids are contiguous; missing ids are confirmed only after a window.

    Very large gaps are not tracked id-by-id: they are confirmed immediately as
    ``bulk_missing`` (they cannot be a reordering artefact).
    """

    def __init__(self, *, window_s: float = 2.0, max_tracked_missing: int = 10_000) -> None:
        self.last: int | None = None
        self.duplicates = 0
        self.late_fills = 0
        self.confirmed_missing = 0
        self._missing: dict[int, int] = {}
        self._window_ns = round(window_s * NS_PER_S)
        self._max_tracked = max_tracked_missing

    def observe(self, seq: int, mono_ns: int) -> SeqResult:
        if self.last is None or seq == self.last + 1:
            self.last = seq
            return SeqResult.OK
        if seq <= self.last:
            if self._missing.pop(seq, None) is not None:
                self.late_fills += 1
                return SeqResult.LATE_FILL
            self.duplicates += 1
            return SeqResult.DUPLICATE
        gap = seq - self.last - 1
        if len(self._missing) + gap > self._max_tracked:
            self.confirmed_missing += gap
        else:
            for missing_id in range(self.last + 1, seq):
                self._missing[missing_id] = mono_ns
        self.last = seq
        return SeqResult.GAP_OPENED

    def expire(self, mono_ns: int) -> int:
        """Confirm ids that stayed missing for longer than the window; returns how many."""
        expired = [i for i, seen in self._missing.items() if mono_ns - seen >= self._window_ns]
        for missing_id in expired:
            del self._missing[missing_id]
        self.confirmed_missing += len(expired)
        return len(expired)

    @property
    def open_missing(self) -> int:
        return len(self._missing)


@dataclass(slots=True)
class _KindStats:
    messages: int = 0
    interval_messages: int = 0
    last_recv_wall_ns: int = 0
    last_recv_mono_ns: int = 0
    lags_ms: deque[float] = field(default_factory=lambda: deque(maxlen=2048))


class QualityMonitor:
    """Aggregates quality signals; the collector turns them into metrics and records."""

    def __init__(self, *, reorder_window_s: float = 2.0) -> None:
        self._kinds: dict[str, _KindStats] = {}
        self._agg: dict[str, SequenceTracker] = {}
        self._reported_missing: dict[str, int] = {}
        self._window_s = reorder_window_s

    def on_message(self, kind: str, event_ms: int | None, stamp: Stamp) -> float | None:
        """Account one message; returns the receive lag in ms (None without an event time)."""
        stats = self._kinds.get(kind)
        if stats is None:
            stats = self._kinds[kind] = _KindStats()
        stats.messages += 1
        stats.interval_messages += 1
        stats.last_recv_wall_ns = stamp.wall_ns
        stats.last_recv_mono_ns = stamp.mono_ns
        if not event_ms:
            return None
        lag = stamp.wall_ns / NS_PER_MS - event_ms
        stats.lags_ms.append(lag)
        return lag

    def on_agg_trade(self, symbol: str, agg_id: int, mono_ns: int) -> SeqResult:
        tracker = self._agg.get(symbol)
        if tracker is None:
            tracker = self._agg[symbol] = SequenceTracker(window_s=self._window_s)
        return tracker.observe(agg_id, mono_ns)

    def expire(self, mono_ns: int) -> dict[str, int]:
        """Confirm missing aggregate-trade ids; returns newly confirmed counts per symbol."""
        newly: dict[str, int] = {}
        for symbol, tracker in self._agg.items():
            tracker.expire(mono_ns)
            reported = self._reported_missing.get(symbol, 0)
            if tracker.confirmed_missing > reported:
                newly[symbol] = tracker.confirmed_missing - reported
                self._reported_missing[symbol] = tracker.confirmed_missing
        return newly

    def seconds_since_last(self, now: Stamp) -> dict[str, float]:
        return {k: (now.mono_ns - s.last_recv_mono_ns) / NS_PER_S for k, s in self._kinds.items()}

    def summary_and_reset(self, now: Stamp) -> dict[str, Any]:
        """A per-interval summary suitable for recording (kind ``quality.summary``)."""
        kinds: dict[str, Any] = {}
        for kind, stats in self._kinds.items():
            lags = sorted(stats.lags_ms)
            kinds[kind] = {
                "messages": stats.interval_messages,
                "total_messages": stats.messages,
                "seconds_since_last": round((now.mono_ns - stats.last_recv_mono_ns) / NS_PER_S, 3),
                "lag_ms_p50": round(lags[len(lags) // 2], 1) if lags else None,
                "lag_ms_p99": round(lags[min(len(lags) - 1, int(len(lags) * 0.99))], 1) if lags else None,
                "lag_ms_max": round(lags[-1], 1) if lags else None,
            }
            stats.interval_messages = 0
            stats.lags_ms.clear()
        agg = {
            symbol: {
                "confirmed_missing": t.confirmed_missing,
                "open_missing": t.open_missing,
                "duplicates": t.duplicates,
                "late_fills": t.late_fills,
            }
            for symbol, t in sorted(self._agg.items())
            if t.confirmed_missing or t.open_missing or t.duplicates or t.late_fills
        }
        return {"at_ns": now.wall_ns, "kinds": kinds, "agg_trade_continuity": agg}
