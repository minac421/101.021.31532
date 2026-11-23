"""Typed (partial) decoders for Binance USD-M messages.

Only the fields the data plane needs are declared; unknown fields are ignored,
so additive changes on the exchange side do not break decoding. The raw text is
always recorded separately, untouched.
"""

from __future__ import annotations

import msgspec

PriceLevel = tuple[str, str]  # (price, quantity) as decimal strings, exactly as sent


class StreamEnvelope(msgspec.Struct):
    """Combined-stream wrapper: ``{"stream": "...", "data": {...}}``."""

    stream: str
    data: msgspec.Raw


class EventTime(msgspec.Struct):
    E: int = 0


class DepthUpdate(msgspec.Struct):
    """``<symbol>@depth[@100ms|@500ms]`` diff event."""

    E: int
    T: int
    s: str
    U: int  # first update id in event
    u: int  # final update id in event
    pu: int  # final update id of the previous event
    b: list[PriceLevel]
    a: list[PriceLevel]


class AggTradeId(msgspec.Struct):
    """The fields of ``<symbol>@aggTrade`` needed for continuity checks."""

    E: int
    s: str
    a: int  # aggregate trade id — contiguous per symbol
    T: int = 0


class DepthSnapshot(msgspec.Struct):
    """``GET /fapi/v1/depth`` response."""

    lastUpdateId: int
    bids: list[PriceLevel]
    asks: list[PriceLevel]
    E: int = 0
    T: int = 0


class RateLimit(msgspec.Struct):
    rateLimitType: str
    interval: str
    intervalNum: int
    limit: int


class SymbolInfo(msgspec.Struct):
    symbol: str
    status: str
    contractType: str = ""
    quoteAsset: str = ""
    marginAsset: str = ""
    underlyingType: str = ""
    onboardDate: int = 0


class ExchangeInfo(msgspec.Struct):
    symbols: list[SymbolInfo]
    rateLimits: list[RateLimit] = msgspec.field(default_factory=list)
    serverTime: int = 0


class Ticker24h(msgspec.Struct):
    symbol: str
    quoteVolume: str = "0"


class ServerTime(msgspec.Struct):
    serverTime: int


envelope_decoder = msgspec.json.Decoder(StreamEnvelope)
event_time_decoder = msgspec.json.Decoder(EventTime)
event_time_list_decoder = msgspec.json.Decoder(list[EventTime])
depth_update_decoder = msgspec.json.Decoder(DepthUpdate)
agg_trade_decoder = msgspec.json.Decoder(AggTradeId)
depth_snapshot_decoder = msgspec.json.Decoder(DepthSnapshot)
exchange_info_decoder = msgspec.json.Decoder(ExchangeInfo)
tickers_decoder = msgspec.json.Decoder(list[Ticker24h])
server_time_decoder = msgspec.json.Decoder(ServerTime)


def event_time_ms(data: bytes | msgspec.Raw) -> int | None:
    """Latest exchange event time (ms) in a payload; handles array streams (``!...@arr``)."""
    raw = bytes(data)
    stripped = raw.lstrip()
    try:
        if stripped.startswith(b"["):
            items = event_time_list_decoder.decode(raw)
            return max((i.E for i in items), default=0) or None
        return event_time_decoder.decode(raw).E or None
    except msgspec.DecodeError:
        return None
