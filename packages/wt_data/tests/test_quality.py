from wt_core.clock import NS_PER_S, Stamp
from wt_data.binance.um.messages import event_time_ms
from wt_data.quality import QualityMonitor, SeqResult, SequenceTracker


def test_contiguous_ids_are_ok_and_duplicates_counted() -> None:
    t = SequenceTracker(window_s=1)
    assert [t.observe(i, 0) for i in (1, 2, 3)] == [SeqResult.OK] * 3
    assert t.observe(2, 0) is SeqResult.DUPLICATE
    assert t.duplicates == 1


def test_late_fill_within_window_is_not_a_gap() -> None:
    t = SequenceTracker(window_s=1)
    t.observe(10, 0)
    assert t.observe(13, 0) is SeqResult.GAP_OPENED
    assert t.open_missing == 2
    assert t.observe(11, 100) is SeqResult.LATE_FILL
    assert t.observe(12, 200) is SeqResult.LATE_FILL
    assert t.expire(5 * NS_PER_S) == 0
    assert t.confirmed_missing == 0


def test_missing_ids_confirmed_after_window() -> None:
    t = SequenceTracker(window_s=1)
    t.observe(10, 0)
    t.observe(15, 0)
    assert t.expire(NS_PER_S // 2) == 0
    assert t.expire(NS_PER_S) == 4
    assert t.confirmed_missing == 4
    assert t.open_missing == 0


def test_huge_gaps_are_confirmed_in_bulk() -> None:
    t = SequenceTracker(window_s=1, max_tracked_missing=100)
    t.observe(1, 0)
    t.observe(1_000_000, 0)
    assert t.confirmed_missing == 999_998
    assert t.open_missing == 0


def test_monitor_lag_and_summary() -> None:
    q = QualityMonitor(reorder_window_s=1)
    stamp = Stamp(wall_ns=1_000_250 * 1_000_000, mono_ns=5 * NS_PER_S)
    assert q.on_message("aggTrade", 1_000_000, stamp) == 250.0
    assert q.on_message("markPrice", None, stamp) is None
    q.on_agg_trade("BTCUSDT", 1, 0)
    q.on_agg_trade("BTCUSDT", 4, 0)
    assert q.expire(2 * NS_PER_S) == {"BTCUSDT": 2}
    assert q.expire(3 * NS_PER_S) == {}
    summary = q.summary_and_reset(Stamp(stamp.wall_ns, 6 * NS_PER_S))
    assert summary["kinds"]["aggTrade"]["lag_ms_p50"] == 250.0
    assert summary["kinds"]["aggTrade"]["seconds_since_last"] == 1.0
    assert summary["agg_trade_continuity"]["BTCUSDT"]["confirmed_missing"] == 2
    assert q.summary_and_reset(stamp)["kinds"]["aggTrade"]["messages"] == 0


def test_event_time_extraction() -> None:
    assert event_time_ms(b'{"e":"aggTrade","E":123}') == 123
    assert event_time_ms(b'[{"E":5},{"E":9}]') == 9
    assert event_time_ms(b'{"no":"time"}') is None
    assert event_time_ms(b"not json") is None
