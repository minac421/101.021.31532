from datetime import UTC, datetime

from wt_core.clock import NS_PER_S, ManualClock, hour_floor_s, utc_from_ns
from wt_core.health import HealthState, worst


def test_manual_clock_advances_both_clocks() -> None:
    clock = ManualClock(wall_ns=10 * NS_PER_S, mono_ns=5)
    clock.advance(1.5)
    now = clock.now()
    assert now.wall_ns == 11_500_000_000
    assert now.mono_ns == 1_500_000_005


def test_hour_floor_and_utc_conversion() -> None:
    ns = int(datetime(2026, 11, 23, 14, 37, 12, tzinfo=UTC).timestamp()) * NS_PER_S
    assert utc_from_ns(hour_floor_s(ns) * NS_PER_S) == datetime(2026, 11, 23, 14, tzinfo=UTC)


def test_worst_health_state() -> None:
    assert worst() is HealthState.OK
    assert worst(HealthState.OK, HealthState.DEGRADED) is HealthState.DEGRADED
    assert worst(HealthState.DEGRADED, HealthState.FAILED, HealthState.OK) is HealthState.FAILED
