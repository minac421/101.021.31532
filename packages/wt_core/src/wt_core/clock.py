"""Clocks and timestamps.

Every received message is stamped with BOTH a wall clock (nanoseconds since the
Unix epoch, UTC) and a monotonic clock. Wall time is what gets compared with
exchange timestamps; monotonic time is what timeouts and rate windows use, so a
wall-clock step (NTP correction) can never fire or suppress a timeout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

NS_PER_S = 1_000_000_000
NS_PER_MS = 1_000_000


@dataclass(frozen=True, slots=True)
class Stamp:
    """A pair of local timestamps taken at the same instant."""

    wall_ns: int
    mono_ns: int


class Clock(Protocol):
    def now(self) -> Stamp: ...


class SystemClock:
    """The real clock."""

    def now(self) -> Stamp:
        return Stamp(time.time_ns(), time.monotonic_ns())


class ManualClock:
    """A controllable clock for deterministic tests and replays."""

    def __init__(self, wall_ns: int = 1_700_000_000 * NS_PER_S, mono_ns: int = 0) -> None:
        self._wall_ns = wall_ns
        self._mono_ns = mono_ns

    def now(self) -> Stamp:
        return Stamp(self._wall_ns, self._mono_ns)

    def advance(self, seconds: float) -> None:
        delta = round(seconds * NS_PER_S)
        self._wall_ns += delta
        self._mono_ns += delta


def utc_from_ns(ns: int) -> datetime:
    """Convert epoch nanoseconds to an aware UTC datetime."""
    return datetime.fromtimestamp(ns / NS_PER_S, tz=UTC)


def hour_floor_s(wall_ns: int) -> int:
    """Start of the UTC hour containing ``wall_ns``, in epoch seconds."""
    seconds = wall_ns // NS_PER_S
    return seconds - seconds % 3600
