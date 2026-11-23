-- =============================================================================
-- AI Whale Trader — ClickHouse schema: market data & intelligence (DRAFT v0.1)
-- -----------------------------------------------------------------------------
-- Conventions
--   * every row carries exchange time and, where it matters, `available_ts`:
--     the first moment the value could have been used in a decision (point-in-time);
--   * history is appended, never updated in place (state = argMax over history);
--   * prices/quantities are Float64 for analytics; the exact Decimal values used for
--     orders live in PostgreSQL (trade.*), never here;
--   * live ingestion goes Redpanda -> Kafka engine table -> materialized view -> table
--     (pattern at the bottom). Backfills only fill gaps, so materialized views that
--     aggregate on insert never see duplicates; a daily job rebuilds and verifies bars.
-- =============================================================================

CREATE DATABASE IF NOT EXISTS md;      -- market data
CREATE DATABASE IF NOT EXISTS intel;   -- derived intelligence

-- -----------------------------------------------------------------------------
-- Aggregated trades — all eligible symbols (<symbol>@aggTrade on /market + archive)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS md.agg_trades
(
    symbol          LowCardinality(String),
    agg_trade_id    UInt64,
    trade_ts        DateTime64(3, 'UTC')  CODEC(DoubleDelta, ZSTD(3)),
    event_ts        DateTime64(3, 'UTC')  CODEC(DoubleDelta, ZSTD(3)),
    recv_ts         DateTime64(6, 'UTC')  CODEC(DoubleDelta, ZSTD(3)),   -- epoch 0 for archive rows
    price           Float64               CODEC(Gorilla, ZSTD(3)),
    qty             Float64               CODEC(Gorilla, ZSTD(3)),
    quote_qty       Float64 MATERIALIZED price * qty,
    is_buyer_maker  Bool,                                               -- true => the aggressor SOLD
    first_trade_id  UInt64                CODEC(Delta, ZSTD(3)),
    last_trade_id   UInt64                CODEC(Delta, ZSTD(3)),
    source          LowCardinality(String)                              -- 'ws' | 'rest' | 'archive'
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(trade_ts)
ORDER BY (symbol, trade_ts, agg_trade_id);

-- 1m bars built locally from trades (also gives aggressive buy/sell volume => CVD)
CREATE TABLE IF NOT EXISTS md.bars_1m
(
    symbol           LowCardinality(String),
    bar_ts           DateTime('UTC'),
    open             AggregateFunction(argMin, Float64, DateTime64(3, 'UTC')),
    high             SimpleAggregateFunction(max, Float64),
    low              SimpleAggregateFunction(min, Float64),
    close            AggregateFunction(argMax, Float64, DateTime64(3, 'UTC')),
    volume           SimpleAggregateFunction(sum, Float64),
    quote_volume     SimpleAggregateFunction(sum, Float64),
    buy_volume       SimpleAggregateFunction(sum, Float64),   -- aggressive buys
    sell_volume      SimpleAggregateFunction(sum, Float64),   -- aggressive sells
    trades           SimpleAggregateFunction(sum, UInt64),
    max_trade_quote  SimpleAggregateFunction(max, Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(bar_ts)
ORDER BY (symbol, bar_ts);

CREATE MATERIALIZED VIEW IF NOT EXISTS md.bars_1m_mv TO md.bars_1m AS
SELECT
    symbol,
    toStartOfMinute(toDateTime(trade_ts))  AS bar_ts,
    argMinState(price, trade_ts)            AS open,
    max(price)                              AS high,
    min(price)                              AS low,
    argMaxState(price, trade_ts)            AS close,
    sum(qty)                                AS volume,
    sum(price * qty)                        AS quote_volume,
    sumIf(qty, NOT is_buyer_maker)          AS buy_volume,
    sumIf(qty, is_buyer_maker)              AS sell_volume,
    sum(last_trade_id - first_trade_id + 1) AS trades,
    max(price * qty)                        AS max_trade_quote
FROM md.agg_trades
GROUP BY symbol, bar_ts;

-- Official klines (cross-check of local bars) for USDM and Spot; also mark/index/premium klines.
CREATE TABLE IF NOT EXISTS md.klines_1m
(
    market           LowCardinality(String),   -- 'um' | 'spot'
    symbol           LowCardinality(String),
    kind             LowCardinality(String),   -- 'trade' | 'mark' | 'index' | 'premium'
    open_ts          DateTime('UTC'),
    open             Float64,
    high             Float64,
    low              Float64,
    close            Float64,
    volume           Float64,
    quote_volume     Float64,
    trades           UInt32,
    taker_buy_base   Float64,                  -- spot taker-buy volume => spot CVD approximation
    taker_buy_quote  Float64,
    source           LowCardinality(String)
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(open_ts)
ORDER BY (market, symbol, kind, open_ts);

-- -----------------------------------------------------------------------------
-- Order book
-- -----------------------------------------------------------------------------
-- Top-N snapshots every second for focus symbols (raw diffs are archived in object storage).
CREATE TABLE IF NOT EXISTS md.book_snapshots_1s
(
    symbol          LowCardinality(String),
    ts              DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(3)),
    last_update_id  UInt64,
    bid_px          Array(Float64) CODEC(ZSTD(3)),   -- best first
    bid_qty         Array(Float64) CODEC(ZSTD(3)),
    ask_px          Array(Float64) CODEC(ZSTD(3)),
    ask_qty         Array(Float64) CODEC(ZSTD(3)),
    book_valid      Bool                             -- false while resyncing
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (symbol, ts)
TTL toDateTime(ts) + INTERVAL 180 DAY DELETE;

-- Depth bands for ALL eligible symbols (from the 500ms local books), every 5 seconds.
CREATE TABLE IF NOT EXISTS md.book_depth_bands
(
    symbol          LowCardinality(String),
    ts              DateTime('UTC'),
    mid             Float64,
    spread_bps      Float32,
    bid_depth_usd   Array(Float32),   -- cumulative notional within [0.1, 0.25, 0.5, 1, 2, 5] % of mid
    ask_depth_usd   Array(Float32),
    imbalance_0_5   Float32,          -- (bid - ask) / (bid + ask) within 0.5 %
    book_valid      Bool
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (symbol, ts);

-- -----------------------------------------------------------------------------
-- Derivatives: mark price & funding, open interest, 5m statistics, liquidations
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS md.mark_price
(
    symbol            LowCardinality(String),
    event_ts          DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD(3)),
    mark_price        Float64 CODEC(Gorilla, ZSTD(3)),
    index_price       Float64 CODEC(Gorilla, ZSTD(3)),
    est_settle_price  Float64 CODEC(Gorilla, ZSTD(3)),
    funding_rate      Float64 CODEC(Gorilla, ZSTD(3)),    -- current (indicative) rate
    next_funding_ts   DateTime('UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_ts)
ORDER BY (symbol, event_ts)
TTL toDateTime(event_ts) + INTERVAL 120 DAY DELETE;      -- 1m rollup below is kept

CREATE TABLE IF NOT EXISTS md.mark_price_1m
(
    symbol        LowCardinality(String),
    bar_ts        DateTime('UTC'),
    mark_close    AggregateFunction(argMax, Float64, DateTime64(3, 'UTC')),
    index_close   AggregateFunction(argMax, Float64, DateTime64(3, 'UTC')),
    funding_last  AggregateFunction(argMax, Float64, DateTime64(3, 'UTC')),
    premium_max   SimpleAggregateFunction(max, Float64),
    premium_min   SimpleAggregateFunction(min, Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(bar_ts)
ORDER BY (symbol, bar_ts);

CREATE MATERIALIZED VIEW IF NOT EXISTS md.mark_price_1m_mv TO md.mark_price_1m AS
SELECT
    symbol,
    toStartOfMinute(toDateTime(event_ts))  AS bar_ts,
    argMaxState(mark_price, event_ts)       AS mark_close,
    argMaxState(index_price, event_ts)      AS index_close,
    argMaxState(funding_rate, event_ts)     AS funding_last,
    max(if(index_price > 0, (mark_price - index_price) / index_price, 0)) AS premium_max,
    min(if(index_price > 0, (mark_price - index_price) / index_price, 0)) AS premium_min
FROM md.mark_price
GROUP BY symbol, bar_ts;

CREATE TABLE IF NOT EXISTS md.funding_settlements
(
    symbol          LowCardinality(String),
    funding_ts      DateTime('UTC'),
    funding_rate    Float64,
    mark_price      Float64,
    interval_hours  UInt8               -- funding intervals differ between symbols
)
ENGINE = ReplacingMergeTree
ORDER BY (symbol, funding_ts);

CREATE TABLE IF NOT EXISTS md.open_interest
(
    symbol        LowCardinality(String),
    exchange_ts   DateTime64(3, 'UTC'),
    available_ts  DateTime64(3, 'UTC'),    -- when we received it
    oi_contracts  Float64,
    oi_value_usd  Float64                  -- oi_contracts * mark price at exchange_ts
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(exchange_ts)
ORDER BY (symbol, exchange_ts);

-- /futures/data/* (5m) and the data.binance.vision `metrics` archive.
-- Both sources are kept side by side (source is part of the key) so that
-- point-in-time queries can use the earliest truly available value.
CREATE TABLE IF NOT EXISTS md.futures_stats_5m
(
    symbol                        LowCardinality(String),
    period_ts                     DateTime('UTC'),
    available_ts                  DateTime64(3, 'UTC'),   -- archive rows: conservative estimate
    sum_open_interest             Nullable(Float64),
    sum_open_interest_value       Nullable(Float64),
    top_trader_account_ls_ratio   Nullable(Float64),
    top_trader_position_ls_ratio  Nullable(Float64),
    global_account_ls_ratio       Nullable(Float64),
    taker_buy_sell_vol_ratio      Nullable(Float64),
    taker_buy_vol                 Nullable(Float64),
    taker_sell_vol                Nullable(Float64),
    basis                         Nullable(Float64),
    source                        LowCardinality(String)  -- 'rest' | 'archive_metrics'
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(period_ts)
ORDER BY (symbol, period_ts, source);

-- NOTE: Binance pushes at most ONE (the largest) liquidation per symbol per 1000 ms.
-- This table is therefore a sample and a lower bound, never a complete total.
CREATE TABLE IF NOT EXISTS md.liquidations
(
    symbol           LowCardinality(String),
    event_ts         DateTime64(3, 'UTC'),
    trade_ts         DateTime64(3, 'UTC'),
    side             Enum8('BUY' = 1, 'SELL' = 2),   -- SELL => a long position was liquidated
    order_type       LowCardinality(String),
    time_in_force    LowCardinality(String),
    price            Float64,
    avg_price        Float64,
    orig_qty         Float64,
    last_filled_qty  Float64,
    filled_acc_qty   Float64,
    order_status     LowCardinality(String),
    notional_usd     Float64 MATERIALIZED avg_price * filled_acc_qty,
    recv_ts          DateTime64(6, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_ts)
ORDER BY (symbol, event_ts);

-- =============================================================================
-- intel: derived intelligence (append-only history, point-in-time)
-- =============================================================================
CREATE TABLE IF NOT EXISTS intel.feature_vectors
(
    symbol               LowCardinality(String),
    ts                   DateTime64(3, 'UTC'),     -- feature time (bar close / event)
    available_ts         DateTime64(3, 'UTC'),     -- when computed and usable
    feature_set_version  LowCardinality(String),
    mode                 LowCardinality(String),   -- 'live' | 'replay' (parity checks compare both)
    values               Map(LowCardinality(String), Float64),
    quality_score        Float32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (feature_set_version, symbol, ts, mode);

CREATE TABLE IF NOT EXISTS intel.footprint_events
(
    event_id          UUID,
    symbol            LowCardinality(String),
    ts                DateTime64(3, 'UTC'),
    available_ts      DateTime64(3, 'UTC'),
    kind              LowCardinality(String),  -- LARGE_PRINT, SWEEP, ABSORPTION, ICEBERG_SUSPECT, WALL_ADDED,
                                               -- WALL_PULLED, OI_SURGE, OI_FLUSH, LIQ_CLUSTER, FUNDING_EXTREME,
                                               -- SPOT_PERP_DIVERGENCE, TOP_TRADER_SHIFT
    side              Enum8('NONE' = 0, 'BUY' = 1, 'SELL' = 2),
    price             Float64,
    notional_usd      Float64,
    strength          Float32,                 -- standardized vs the symbol's own history
    confidence        Float32,                 -- [0, 1]
    alternatives      Array(LowCardinality(String)),   -- competing explanations: spoof, mm_inventory, hedge, ...
    detector_version  LowCardinality(String),
    details           String                   -- JSON
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (symbol, ts, kind);

-- Every observation of a liquidity level is appended; current state = argMax(status, observed_ts).
CREATE TABLE IF NOT EXISTS intel.liquidity_levels
(
    symbol            LowCardinality(String),
    level_id          UUID,
    observed_ts       DateTime64(3, 'UTC'),
    price             Float64,
    price_lo          Float64,
    price_hi          Float64,
    kind              LowCardinality(String),  -- SWING_HIGH, SWING_LOW, EQUAL_HIGHS, EQUAL_LOWS, PDH, PDL, PWH, PWL,
                                               -- SESSION_HIGH, SESSION_LOW, ROUND_NUMBER, HVN, LVN, POC, VWAP_BAND,
                                               -- BOOK_WALL, EST_LIQUIDATION_CLUSTER
    timeframe         LowCardinality(String),
    strength          Float32,
    touches           UInt16,
    status            LowCardinality(String),  -- ACTIVE, SWEPT, BROKEN, ACCEPTED, EXPIRED
    is_estimate       Bool,                    -- true for modeled levels (e.g. liquidation clusters)
    detector_version  LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(observed_ts)
ORDER BY (symbol, observed_ts, level_id);

CREATE TABLE IF NOT EXISTS intel.regimes
(
    scope          LowCardinality(String),     -- 'GLOBAL' or a symbol
    ts             DateTime('UTC'),
    available_ts   DateTime64(3, 'UTC'),
    regime         LowCardinality(String),     -- TREND_UP, TREND_DOWN, RANGE, VOL_EXPANSION, COMPRESSION, STRESS, UNKNOWN
    probabilities  Map(LowCardinality(String), Float32),
    model_version  LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (scope, ts);

CREATE TABLE IF NOT EXISTS intel.data_quality_1m
(
    source      LowCardinality(String),
    symbol      LowCardinality(String),
    minute      DateTime('UTC'),
    messages    UInt32,
    expected    Float32,
    gaps        UInt16,
    resyncs     UInt16,
    max_lag_ms  UInt32,
    p99_lag_ms  UInt32,
    score       Float32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (source, symbol, minute);

-- Exact inputs of every decision (for audit, replay and live-vs-replay parity tests).
CREATE TABLE IF NOT EXISTS intel.decision_feature_snapshots
(
    decision_id          UUID,
    thesis_id            UUID,
    symbol               LowCardinality(String),
    ts                   DateTime64(3, 'UTC'),
    feature_set_version  LowCardinality(String),
    values               Map(LowCardinality(String), Float64),
    intel_snapshot       String                   -- full JSON snapshot used by the decision
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (symbol, ts, decision_id);

CREATE TABLE IF NOT EXISTS intel.news_events
(
    event_id            UUID,
    published_ts        DateTime64(3, 'UTC'),
    received_ts         DateTime64(3, 'UTC'),     -- point-in-time key for news
    source              LowCardinality(String),
    url                 String,
    title               String,
    assets              Array(LowCardinality(String)),
    event_type          LowCardinality(String),   -- LISTING, DELISTING, HACK, REGULATION, MACRO, UNLOCK, ...
    direction_hint      Enum8('NEGATIVE' = -1, 'UNCLEAR' = 0, 'POSITIVE' = 1),
    severity            UInt8,                    -- 1..5
    confidence          Float32,
    classifier_version  LowCardinality(String),
    raw                 String
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(received_ts)
ORDER BY (received_ts, event_id);

-- =============================================================================
-- Live ingestion pattern (requires a Redpanda broker; shown for md.agg_trades)
-- =============================================================================
-- CREATE TABLE md.agg_trades_queue
-- (
--     symbol String, agg_trade_id UInt64, trade_ts DateTime64(3, 'UTC'), event_ts DateTime64(3, 'UTC'),
--     recv_ts DateTime64(6, 'UTC'), price Float64, qty Float64, is_buyer_maker Bool,
--     first_trade_id UInt64, last_trade_id UInt64, source String
-- )
-- ENGINE = Kafka
-- SETTINGS kafka_broker_list = 'redpanda:9092',
--          kafka_topic_list  = 'md.norm.agg_trades',
--          kafka_group_name  = 'clickhouse-agg-trades',
--          kafka_format      = 'JSONEachRow';
--
-- CREATE MATERIALIZED VIEW md.agg_trades_ingest TO md.agg_trades AS
-- SELECT * FROM md.agg_trades_queue;
