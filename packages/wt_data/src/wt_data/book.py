"""Local order book kept in sync from a REST snapshot plus the diff stream.

Implements the official Binance USD-M procedure (docs/03-data-sources.md §7):

1. buffer diff events from the stream;
2. take a REST snapshot (``lastUpdateId`` = L);
3. drop buffered events with ``u < L``;
4. the first processed event must satisfy ``U <= L <= u``;
5. afterwards every event must satisfy ``pu == previous u``, otherwise resync.

Two additions make it robust for multi-connection feeds:

* **duplicates** (``u <= last applied u``) are ignored — they occur when two
  sockets carry the same stream while a connection is being rotated;
* **reorder window**: an event that arrives *ahead* of a missing one is held
  for a short window, in case the other socket delivers the missing event.
  Only if the window expires is it declared a gap (and a resync is requested).
  While anything is held the book reports ``is_valid = False``.

Prices and quantities are kept as ``Decimal`` exactly as sent by the exchange.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from sortedcontainers import SortedDict

from wt_data.binance.um.messages import DepthSnapshot, DepthUpdate, PriceLevel


class BookState(StrEnum):
    BUFFERING = "BUFFERING"  # waiting for a snapshot (initially, or after a gap)
    SYNCING = "SYNCING"  # snapshot applied, waiting for the bridging event (U <= L <= u)
    LIVE = "LIVE"


class DiffResult(StrEnum):
    BUFFERED = "BUFFERED"
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"  # older than the snapshot
    PENDING = "PENDING"  # held in the reorder window
    GAP = "GAP"  # continuity broken -> snapshot requested


class SnapshotResult(StrEnum):
    LIVE = "LIVE"
    SYNCING = "SYNCING"
    TOO_OLD = "TOO_OLD"  # buffered events start after the snapshot: a newer snapshot is needed
    IGNORED = "IGNORED"  # the book was not waiting for a snapshot


@dataclass(slots=True)
class BookStats:
    applied: int = 0
    duplicates: int = 0
    stale: int = 0
    gaps: int = 0
    snapshots: int = 0
    resyncs: int = 0
    buffer_overflows: int = 0
    crossed: int = 0
    reordered: int = 0


Level = tuple[Decimal, Decimal]


class OrderBook:
    def __init__(self, symbol: str, *, max_buffer: int = 5000, reorder_window_s: float = 1.5) -> None:
        self.symbol = symbol
        self.state = BookState.BUFFERING
        self.stats = BookStats()
        self.last_update_id = 0
        self.needs_snapshot = True
        self._snapshot_id = 0
        self._bids = SortedDict()
        self._asks = SortedDict()
        self._buffer: deque[DepthUpdate] = deque()
        self._max_buffer = max_buffer
        self._pending: dict[int, tuple[DepthUpdate, int]] = {}  # keyed by pu -> (event, arrival mono ns)
        self._reorder_window_ns = round(reorder_window_s * 1_000_000_000)

    # ------------------------------------------------------------------ status
    @property
    def is_valid(self) -> bool:
        """True only when the book is live and nothing is waiting in the reorder window."""
        return self.state is BookState.LIVE and not self._pending

    @property
    def has_buffered_events(self) -> bool:
        return bool(self._buffer)

    # ------------------------------------------------------------------ input
    def on_diff(self, event: DepthUpdate, mono_ns: int) -> DiffResult:
        if self.state is BookState.BUFFERING:
            self._buffer_event(event)
            return DiffResult.BUFFERED
        if self.state is BookState.SYNCING:
            return self._on_syncing_diff(event)
        return self._on_live_diff(event, mono_ns)

    def apply_snapshot(self, snapshot: DepthSnapshot) -> SnapshotResult:
        if self.state is not BookState.BUFFERING:
            return SnapshotResult.IGNORED
        last_id = snapshot.lastUpdateId
        self._load_levels(snapshot.bids, snapshot.asks)
        self._snapshot_id = last_id
        self.last_update_id = last_id
        self.stats.snapshots += 1
        if self.stats.snapshots > 1:
            self.stats.resyncs += 1
        self.needs_snapshot = False

        events = sorted(self._buffer, key=lambda e: e.u)
        self._buffer.clear()
        fresh = [e for e in events if e.u >= last_id]
        self.stats.stale += len(events) - len(fresh)
        if not fresh:
            self.state = BookState.SYNCING
            return SnapshotResult.SYNCING
        if last_id < fresh[0].U:
            self._back_to_buffering(fresh, count_gap=False)
            return SnapshotResult.TOO_OLD
        self._apply(fresh[0])
        for i, event in enumerate(fresh[1:], start=1):
            if event.u <= self.last_update_id:
                self.stats.duplicates += 1
                continue
            if event.pu != self.last_update_id:
                self._back_to_buffering(fresh[i:], count_gap=True)
                return SnapshotResult.TOO_OLD
            self._apply(event)
        self.state = BookState.LIVE
        if self._is_crossed():
            self._crossed()
            return SnapshotResult.TOO_OLD
        return SnapshotResult.LIVE

    def check_timeouts(self, mono_ns: int) -> bool:
        """Declare a gap if an event has waited in the reorder window for too long."""
        if not self._pending:
            return False
        oldest = min(arrived for _, arrived in self._pending.values())
        if mono_ns - oldest < self._reorder_window_ns:
            return False
        held = [event for event, _ in self._pending.values()]
        self._pending.clear()
        self._back_to_buffering(sorted(held, key=lambda e: e.u), count_gap=True)
        return True

    # ------------------------------------------------------------------ queries
    def best_bid(self) -> Level | None:
        return self._bids.peekitem(-1) if self._bids else None

    def best_ask(self) -> Level | None:
        return self._asks.peekitem(0) if self._asks else None

    def mid(self) -> Decimal | None:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid[0] + ask[0]) / 2

    def spread_bps(self) -> float | None:
        bid, ask = self.best_bid(), self.best_ask()
        mid = self.mid()
        if bid is None or ask is None or not mid:
            return None
        return float((ask[0] - bid[0]) / mid * 10_000)

    def top_levels(self, depth: int) -> tuple[list[Level], list[Level]]:
        bids = [(p, self._bids[p]) for p in self._bids.irange(reverse=True)][:depth]
        asks = [(p, self._asks[p]) for p in self._asks.irange()][:depth]
        return bids, asks

    def depth_notional(self, pct: Decimal) -> tuple[Decimal, Decimal]:
        """Cumulative quote notional within ``pct`` (e.g. Decimal("0.005") = 0.5 %) of mid."""
        mid = self.mid()
        if mid is None:
            return Decimal(0), Decimal(0)
        lo, hi = mid * (1 - pct), mid * (1 + pct)
        bid_notional = sum((p * self._bids[p] for p in self._bids.irange(minimum=lo)), Decimal(0))
        ask_notional = sum((p * self._asks[p] for p in self._asks.irange(maximum=hi)), Decimal(0))
        return bid_notional, ask_notional

    def level_count(self) -> tuple[int, int]:
        return len(self._bids), len(self._asks)

    # ------------------------------------------------------------------ internals
    def _on_syncing_diff(self, event: DepthUpdate) -> DiffResult:
        last_id = self._snapshot_id
        if event.u < last_id:
            self.stats.stale += 1
            return DiffResult.STALE
        if event.U <= last_id <= event.u:
            self._apply(event)
            self.state = BookState.LIVE
            if self._is_crossed():
                self._crossed()
                return DiffResult.GAP
            return DiffResult.APPLIED
        self._back_to_buffering([event], count_gap=True)  # the bridging event was missed
        return DiffResult.GAP

    def _on_live_diff(self, event: DepthUpdate, mono_ns: int) -> DiffResult:
        if event.u <= self.last_update_id:
            self.stats.duplicates += 1
            return DiffResult.DUPLICATE
        if event.pu == self.last_update_id:
            self._apply(event)
            self._drain_pending()
            if self._is_crossed():
                self._crossed()
                return DiffResult.GAP
            return DiffResult.APPLIED
        if event.pu > self.last_update_id:
            self._pending.setdefault(event.pu, (event, mono_ns))
            if self.check_timeouts(mono_ns):
                return DiffResult.GAP
            return DiffResult.PENDING
        # pu < last < u: overlapping ranges can only mean an inconsistent stream
        self._back_to_buffering([event], count_gap=True)
        return DiffResult.GAP

    def _drain_pending(self) -> None:
        while self._pending:
            for pu in [pu for pu, (ev, _) in self._pending.items() if ev.u <= self.last_update_id]:
                del self._pending[pu]  # became duplicates
                self.stats.duplicates += 1
            held = self._pending.pop(self.last_update_id, None)
            if held is None:
                return
            self._apply(held[0])
            self.stats.reordered += 1

    def _apply(self, event: DepthUpdate) -> None:
        _apply_side(self._bids, event.b)
        _apply_side(self._asks, event.a)
        self.last_update_id = event.u
        self.stats.applied += 1

    def _load_levels(self, bids: list[PriceLevel], asks: list[PriceLevel]) -> None:
        self._bids.clear()
        self._asks.clear()
        _apply_side(self._bids, bids)
        _apply_side(self._asks, asks)

    def _buffer_event(self, event: DepthUpdate) -> None:
        if len(self._buffer) >= self._max_buffer:
            self._buffer.popleft()
            self.stats.buffer_overflows += 1
        self._buffer.append(event)

    def _back_to_buffering(self, events: list[DepthUpdate], *, count_gap: bool) -> None:
        if count_gap:
            self.stats.gaps += 1
        self.state = BookState.BUFFERING
        self.needs_snapshot = True
        self._pending.clear()
        self._buffer.clear()
        for event in events:
            self._buffer_event(event)

    def _is_crossed(self) -> bool:
        bid, ask = self.best_bid(), self.best_ask()
        return bid is not None and ask is not None and bid[0] >= ask[0]

    def _crossed(self) -> None:
        self.stats.crossed += 1
        self._back_to_buffering([], count_gap=True)


def _apply_side(side: SortedDict, levels: list[PriceLevel]) -> None:
    for price_s, qty_s in levels:
        price, qty = Decimal(price_s), Decimal(qty_s)
        if qty == 0:
            side.pop(price, None)  # removing an unknown level is normal (docs, step 9)
        else:
            side[price] = qty
