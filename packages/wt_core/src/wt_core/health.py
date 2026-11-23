"""Data-health states shared by the data plane and the risk state machine.

See docs/03-data-sources.md §9: health is aggregated per source and symbol into
OK / DEGRADED / FAILED, and the risk state machine escalates on anything but OK.
"""

from __future__ import annotations

from enum import StrEnum

import msgspec


class HealthState(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


_SEVERITY = {HealthState.OK: 0, HealthState.DEGRADED: 1, HealthState.FAILED: 2}


def worst(*states: HealthState) -> HealthState:
    """The most severe of the given states (OK when called with none)."""
    return max(states, key=lambda s: s.severity, default=HealthState.OK)


class DataHealth(msgspec.Struct, frozen=True, kw_only=True):
    """Health of one scope (e.g. ``binance-um/aggTrade/BTCUSDT``) at one instant."""

    scope: str
    state: HealthState
    score: float
    reasons: tuple[str, ...] = ()
    at_ns: int = 0
