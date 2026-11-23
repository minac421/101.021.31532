"""Order book synchronisation: the official procedure, A/B duplicates, reordering, gaps.

The property tests generate random diff streams whose update ids are linked by
``pu`` (and may skip ids between events, as USD-M streams can), take a snapshot
at a random point, deliver the remaining events in *any* order with duplicates,
and require the result to equal a straightforward reference model.
"""

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from wt_data.binance.um.messages import DepthSnapshot, DepthUpdate
from wt_data.book import BookState, DiffResult, OrderBook, SnapshotResult

BID_PRICES = [f"{p}.0" for p in range(90, 100)]
ASK_PRICES = [f"{p}.0" for p in range(101, 111)]
INITIAL_BIDS = [("95.0", "1"), ("96.0", "2")]
INITIAL_ASKS = [("104.0", "1"), ("105.0", "3")]


def ev(
    U: int, u: int, pu: int, b: list[tuple[str, str]] | None = None, a: list[tuple[str, str]] | None = None
) -> DepthUpdate:
    return DepthUpdate(E=0, T=0, s="TEST", U=U, u=u, pu=pu, b=b or [], a=a or [])


def snap(
    last_id: int, bids: list[tuple[str, str]] | None = None, asks: list[tuple[str, str]] | None = None
) -> DepthSnapshot:
    return DepthSnapshot(
        lastUpdateId=last_id, bids=bids or list(INITIAL_BIDS), asks=asks or list(INITIAL_ASKS)
    )


def live_book(last_id: int = 100) -> OrderBook:
    book = OrderBook("TEST", reorder_window_s=1.0)
    book.on_diff(ev(last_id - 4, last_id, last_id - 5), 0)
    assert book.apply_snapshot(snap(last_id)) is SnapshotResult.LIVE
    return book


# ------------------------------------------------------------------ official procedure
def test_buffered_events_bridge_the_snapshot() -> None:
    book = OrderBook("TEST")
    for e in (ev(90, 95, 89), ev(96, 100, 95, b=[("97.0", "4")]), ev(101, 105, 100, a=[("103.0", "2")])):
        assert book.on_diff(e, 0) is DiffResult.BUFFERED
    assert book.apply_snapshot(snap(98)) is SnapshotResult.LIVE
    assert book.is_valid
    assert book.last_update_id == 105
    assert book.best_bid() == (Decimal("97.0"), Decimal("4"))
    assert book.best_ask() == (Decimal("103.0"), Decimal("2"))
    assert book.stats.stale == 1  # the event with u < lastUpdateId was dropped


def test_snapshot_newer_than_buffer_waits_for_bridging_event() -> None:
    book = OrderBook("TEST")
    book.on_diff(ev(90, 95, 89), 0)
    assert book.apply_snapshot(snap(120)) is SnapshotResult.SYNCING
    assert not book.is_valid
    assert book.on_diff(ev(110, 115, 109), 0) is DiffResult.STALE
    assert book.on_diff(ev(116, 125, 115), 0) is DiffResult.APPLIED
    assert book.state is BookState.LIVE


def test_snapshot_older_than_buffer_requests_a_newer_one() -> None:
    book = OrderBook("TEST")
    book.on_diff(ev(200, 205, 199), 0)
    assert book.apply_snapshot(snap(150)) is SnapshotResult.TOO_OLD
    assert book.needs_snapshot
    assert book.state is BookState.BUFFERING
    assert book.apply_snapshot(snap(202)) is SnapshotResult.LIVE


def test_snapshot_ignored_when_not_waiting() -> None:
    book = live_book()
    assert book.apply_snapshot(snap(500)) is SnapshotResult.IGNORED
    assert book.last_update_id == 100


def test_syncing_book_detects_missed_bridging_event() -> None:
    book = OrderBook("TEST")
    book.on_diff(ev(10, 12, 9), 0)
    assert book.apply_snapshot(snap(50)) is SnapshotResult.SYNCING
    assert book.on_diff(ev(60, 65, 55), 0) is DiffResult.GAP
    assert book.needs_snapshot


def test_buffer_gap_after_snapshot_requests_resync() -> None:
    book = OrderBook("TEST")
    for e in (ev(96, 100, 95), ev(101, 105, 100), ev(111, 115, 110)):  # 106..110 missing
        book.on_diff(e, 0)
    assert book.apply_snapshot(snap(98)) is SnapshotResult.TOO_OLD
    assert book.stats.gaps == 1


# ------------------------------------------------------------------ live behaviour
def test_zero_quantity_removes_level_and_unknown_removal_is_harmless() -> None:
    book = live_book()
    assert book.on_diff(ev(101, 102, 100, b=[("96.0", "0"), ("91.0", "0")]), 0) is DiffResult.APPLIED
    assert book.best_bid() == (Decimal("95.0"), Decimal("1"))


def test_duplicates_are_ignored() -> None:
    book = live_book()
    e = ev(101, 103, 100, a=[("102.0", "5")])
    assert book.on_diff(e, 0) is DiffResult.APPLIED
    assert book.on_diff(e, 0) is DiffResult.DUPLICATE
    assert book.stats.duplicates == 1
    assert book.is_valid


def test_out_of_order_event_within_window_is_reordered() -> None:
    book = live_book()
    e1, e2 = ev(101, 103, 100, b=[("97.0", "1")]), ev(104, 106, 103, b=[("98.0", "1")])
    assert book.on_diff(e2, 0) is DiffResult.PENDING
    assert not book.is_valid
    assert book.on_diff(e1, 10) is DiffResult.APPLIED
    assert book.is_valid
    assert book.last_update_id == 106
    assert book.stats.reordered == 1


def test_missing_event_becomes_gap_after_window() -> None:
    book = live_book()
    assert book.on_diff(ev(104, 106, 103), 0) is DiffResult.PENDING
    assert not book.check_timeouts(500_000_000)
    assert book.check_timeouts(1_000_000_000)
    assert book.needs_snapshot
    assert book.state is BookState.BUFFERING
    assert book.stats.gaps == 1


def test_crossed_book_forces_resync() -> None:
    book = live_book()
    assert book.on_diff(ev(101, 101, 100, b=[("106.0", "1")]), 0) is DiffResult.GAP
    assert book.stats.crossed == 1
    assert book.needs_snapshot


def test_depth_queries() -> None:
    book = live_book()
    assert book.mid() == Decimal("100.0")
    assert round(book.spread_bps() or 0, 6) == 800.0
    bids, asks = book.top_levels(1)
    assert bids == [(Decimal("96.0"), Decimal("2"))]
    assert asks == [(Decimal("104.0"), Decimal("1"))]
    bid_notional, ask_notional = book.depth_notional(Decimal("0.045"))
    assert bid_notional == Decimal("96.0") * 2
    assert ask_notional == Decimal("104.0")


def test_buffer_is_bounded() -> None:
    book = OrderBook("TEST", max_buffer=3)
    for i in range(5):
        book.on_diff(ev(10 * i + 1, 10 * i + 5, 10 * i), 0)
    assert book.stats.buffer_overflows == 2


# ------------------------------------------------------------------ properties
change = st.tuples(st.booleans(), st.integers(0, 9), st.integers(0, 5))
batch = st.lists(change, min_size=1, max_size=4)


def build_events(batches: list[list[tuple[bool, int, int]]], skips: list[int]) -> list[DepthUpdate]:
    events, u = [], 1000
    for changes, skip in zip(batches, skips, strict=True):
        first = u + 1 + skip  # USD-M ids may skip between events; continuity is defined by pu
        last = first + len(changes) - 1
        bids = [(BID_PRICES[i], str(q)) for is_bid, i, q in changes if is_bid]
        asks = [(ASK_PRICES[i], str(q)) for is_bid, i, q in changes if not is_bid]
        events.append(ev(first, last, u, bids, asks))
        u = last
    return events


def reference(
    events: list[DepthUpdate],
) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
    bids = {Decimal(p): Decimal(q) for p, q in INITIAL_BIDS}
    asks = {Decimal(p): Decimal(q) for p, q in INITIAL_ASKS}
    for e in events:
        for side, levels in ((bids, e.b), (asks, e.a)):
            for p, q in levels:
                if Decimal(q) == 0:
                    side.pop(Decimal(p), None)
                else:
                    side[Decimal(p)] = Decimal(q)
    return sorted(bids.items(), reverse=True), sorted(asks.items())


def as_strings(levels: list[tuple[Decimal, Decimal]]) -> list[tuple[str, str]]:
    return [(str(p), str(q)) for p, q in levels]


@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_any_arrival_order_with_duplicates_converges_to_reference(data: st.DataObject) -> None:
    batches = data.draw(st.lists(batch, min_size=2, max_size=25))
    skips = data.draw(st.lists(st.integers(0, 3), min_size=len(batches), max_size=len(batches)))
    events = build_events(batches, skips)
    n = len(events)
    k = data.draw(st.integers(1, n), label="snapshot after k events")
    j = data.draw(st.integers(0, k - 1), label="buffering started at event j")
    m = data.draw(st.integers(k, n), label="events received before the snapshot was applied")

    book = OrderBook("TEST", reorder_window_s=1.0)
    buffered = events[j:m] + data.draw(st.lists(st.sampled_from(events[j:m]), max_size=5), label="dup")
    for e in data.draw(st.permutations(buffered), label="buffer order"):
        assert book.on_diff(e, 0) is DiffResult.BUFFERED
    snap_bids, snap_asks = reference(events[:k])
    result = book.apply_snapshot(DepthSnapshot(events[k - 1].u, as_strings(snap_bids), as_strings(snap_asks)))
    assert result is SnapshotResult.LIVE

    live = (
        events[m:] + data.draw(st.lists(st.sampled_from(events), max_size=5), label="dup")
        if events[m:]
        else []
    )
    for e in data.draw(st.permutations(live), label="live order"):
        book.on_diff(e, 0)

    assert book.is_valid
    assert book.stats.gaps == 0
    assert book.last_update_id == events[-1].u
    assert book.top_levels(100) == reference(events)


@settings(max_examples=200, deadline=None)
@given(data=st.data())
def test_a_lost_event_is_always_detected(data: st.DataObject) -> None:
    batches = data.draw(st.lists(batch, min_size=3, max_size=20))
    events = build_events(batches, [0] * len(batches))
    book = OrderBook("TEST", reorder_window_s=1.0)
    book.on_diff(events[0], 0)
    snap_bids, snap_asks = reference(events[:1])
    assert (
        book.apply_snapshot(DepthSnapshot(events[0].u, as_strings(snap_bids), as_strings(snap_asks)))
        is SnapshotResult.LIVE
    )
    lost = data.draw(st.integers(1, len(events) - 2), label="lost event")
    for e in events[1:lost] + events[lost + 1 :]:
        book.on_diff(e, 0)
    assert not book.is_valid  # something is held in the reorder window
    assert book.check_timeouts(1_000_000_000)
    assert book.needs_snapshot
    assert book.stats.gaps == 1
