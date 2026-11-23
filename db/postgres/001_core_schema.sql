-- =============================================================================
-- AI Whale Trader — PostgreSQL core schema (DRAFT v0.1, for design review)
-- -----------------------------------------------------------------------------
-- System of record: point-in-time reference data, configs & releases, the
-- decision journal, orders/fills/positions, risk events, analytics & ML metadata,
-- and the audit log. High-volume market data lives in ClickHouse, not here.
--
-- Governance is enforced IN THE DATABASE, not only in application code:
--   * journal tables are append-only (triggers + no UPDATE/DELETE grants);
--   * risk policies can only be inserted by the human role wt_risk_admin;
--   * loosening a risk limit cannot take effect before a 24h cooldown;
--   * promotion to CANARY/CHAMPION requires a human actor (RLS + CHECK);
--   * automatic actors cannot leave FLATTEN/HALTED risk states.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS ref;        -- reference data (point-in-time)
CREATE SCHEMA IF NOT EXISTS cfg;        -- risk policies, strategy configs, releases
CREATE SCHEMA IF NOT EXISTS trade;      -- decisions, theses, orders, fills, positions
CREATE SCHEMA IF NOT EXISTS risk;       -- verdicts, state machine, risk events, watchdog
CREATE SCHEMA IF NOT EXISTS analytics;  -- autopsies, thesis outcomes (incl. rejected)
CREATE SCHEMA IF NOT EXISTS ml;         -- datasets, hypotheses, studies, validation reports
CREATE SCHEMA IF NOT EXISTS ops;        -- data health incidents, audit log

-- -----------------------------------------------------------------------------
-- Enumerations (enum declaration order matters: risk_state is ordered by severity)
-- -----------------------------------------------------------------------------
CREATE TYPE ops.actor_kind        AS ENUM ('SYSTEM', 'OPTIMIZER', 'WATCHDOG', 'HUMAN');
CREATE TYPE trade.run_mode        AS ENUM ('BACKTEST', 'PAPER', 'CANARY', 'LIVE');
CREATE TYPE trade.side            AS ENUM ('LONG', 'SHORT');
CREATE TYPE trade.decision_action AS ENUM ('NO_TRADE', 'OPEN_LONG', 'OPEN_SHORT', 'ADJUST', 'REDUCE', 'CLOSE');
CREATE TYPE trade.order_purpose   AS ENUM ('ENTRY', 'STOP_LOSS', 'TAKE_PROFIT', 'EXIT', 'EMERGENCY_EXIT', 'KILL_SWITCH');
CREATE TYPE trade.order_status    AS ENUM ('PENDING_NEW', 'NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED',
                                           'EXPIRED', 'REJECTED', 'TRIGGERED', 'FINISHED');
CREATE TYPE risk.risk_state       AS ENUM ('NORMAL', 'CAUTION', 'REDUCE_ONLY', 'FLATTEN', 'HALTED');
CREATE TYPE cfg.release_stage     AS ENUM ('CANDIDATE', 'BACKTEST_PASSED', 'WALKFORWARD_PASSED', 'STRESS_PASSED',
                                           'PAPER', 'CANARY', 'CHAMPION', 'RETIRED', 'REJECTED', 'ROLLED_BACK');

-- =============================================================================
-- ref: point-in-time reference data (SCD type 2 — never overwrite, close validity)
-- =============================================================================
CREATE TABLE ref.instruments (
    instrument_version_id BIGSERIAL PRIMARY KEY,
    symbol              TEXT        NOT NULL,
    pair                TEXT        NOT NULL,
    contract_type       TEXT        NOT NULL,          -- PERPETUAL, CURRENT_QUARTER, ...
    status              TEXT        NOT NULL,          -- TRADING, SETTLING, CLOSE, ...
    base_asset          TEXT        NOT NULL,
    quote_asset         TEXT        NOT NULL,
    margin_asset        TEXT        NOT NULL,
    underlying_type     TEXT,                          -- non-crypto underlyings are excluded by default
    onboard_date        TIMESTAMPTZ,
    delivery_date       TIMESTAMPTZ,
    tick_size           NUMERIC(38,18) NOT NULL,
    step_size           NUMERIC(38,18) NOT NULL,
    market_step_size    NUMERIC(38,18),
    min_qty             NUMERIC(38,18),
    min_notional        NUMERIC(38,18),
    filters             JSONB       NOT NULL,          -- raw filters exactly as returned by exchangeInfo
    raw_sha256          BYTEA       NOT NULL,          -- change detection
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_to            TIMESTAMPTZ,                   -- NULL = current version
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT instruments_validity CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE UNIQUE INDEX instruments_one_current ON ref.instruments (symbol) WHERE valid_to IS NULL;
CREATE INDEX instruments_symbol_time ON ref.instruments (symbol, valid_from);

CREATE TABLE ref.leverage_brackets (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT        NOT NULL,
    brackets     JSONB       NOT NULL,   -- [{bracket, initialLeverage, notionalCap, notionalFloor, maintMarginRatio, cum}]
    raw_sha256   BYTEA       NOT NULL,
    valid_from   TIMESTAMPTZ NOT NULL,
    valid_to     TIMESTAMPTZ
);
CREATE UNIQUE INDEX leverage_brackets_one_current ON ref.leverage_brackets (symbol) WHERE valid_to IS NULL;

CREATE TABLE ref.funding_config (
    id                      BIGSERIAL PRIMARY KEY,
    symbol                  TEXT        NOT NULL,
    funding_interval_hours  SMALLINT    NOT NULL CHECK (funding_interval_hours > 0),
    rate_cap                NUMERIC(18,10),
    rate_floor              NUMERIC(18,10),
    valid_from              TIMESTAMPTZ NOT NULL,
    valid_to                TIMESTAMPTZ
);
CREATE UNIQUE INDEX funding_config_one_current ON ref.funding_config (symbol) WHERE valid_to IS NULL;

-- Universe & tiers as they were at each snapshot (survivorship-free backtests).
CREATE TABLE ref.universe_snapshots (
    snapshot_ts  TIMESTAMPTZ NOT NULL,
    symbol       TEXT        NOT NULL,
    eligible     BOOLEAN     NOT NULL,
    tier         CHAR(1)     NOT NULL CHECK (tier IN ('A', 'B', 'C', 'X')),
    reasons      TEXT[]      NOT NULL DEFAULT '{}',      -- why excluded / downgraded
    metrics      JSONB       NOT NULL,                   -- volume, spread, depth, listing age, ...
    PRIMARY KEY (snapshot_ts, symbol)
);

CREATE TABLE ref.correlation_clusters (
    as_of         DATE NOT NULL,
    symbol        TEXT NOT NULL,
    cluster_id    INT  NOT NULL,
    beta_btc_30d  DOUBLE PRECISION,
    corr_btc_30d  DOUBLE PRECISION,
    sector        TEXT,
    PRIMARY KEY (as_of, symbol)
);

-- =============================================================================
-- cfg: risk policies (HUMAN-ONLY), strategy configs, release bundles
-- =============================================================================
CREATE TABLE cfg.risk_policies (
    policy_version   INT         PRIMARY KEY,
    policy_yaml      TEXT        NOT NULL,
    policy_sha256    BYTEA       NOT NULL UNIQUE,
    signature        TEXT        NOT NULL,          -- detached signature by the policy owner's key
    signed_by        TEXT        NOT NULL,          -- key id of the human owner
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_summary   TEXT        NOT NULL,
    loosens_limits   BOOLEAN     NOT NULL,          -- computed by the signing tool AND re-checked by the Risk Guardian
    effective_from   TIMESTAMPTZ NOT NULL,
    CONSTRAINT loosening_requires_cooldown
        CHECK (NOT loosens_limits OR effective_from >= created_at + INTERVAL '24 hours')
);

CREATE TABLE cfg.risk_policy_approvals (
    policy_version  INT         NOT NULL REFERENCES cfg.risk_policies (policy_version),
    approver        TEXT        NOT NULL,
    approved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    method          TEXT        NOT NULL,           -- e.g. 'signing-cli+totp'
    PRIMARY KEY (policy_version, approver)
);

-- The policy the Risk Guardian may load (it ALSO verifies the signature with the owner's public key).
CREATE VIEW cfg.effective_risk_policy AS
SELECT p.*
FROM cfg.risk_policies AS p
WHERE p.effective_from <= now()
  AND EXISTS (SELECT 1 FROM cfg.risk_policy_approvals AS a WHERE a.policy_version = p.policy_version)
ORDER BY p.policy_version DESC
LIMIT 1;

-- Strategy parameters the optimizer MAY propose. Risk keys are rejected here as a
-- second line of defense (primary validation is the config schema + a CI test).
CREATE TABLE cfg.strategy_configs (
    config_version  TEXT        PRIMARY KEY,         -- e.g. 'sweep_reclaim@1.4.0'
    setup_family    TEXT        NOT NULL,
    params          JSONB       NOT NULL,
    params_sha256   BYTEA       NOT NULL,
    parent_version  TEXT        REFERENCES cfg.strategy_configs (config_version),
    created_by      ops.actor_kind NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    study_id        TEXT,                            -- optimizer study that produced it, if any
    CONSTRAINT strategy_params_have_no_risk_keys CHECK (
        NOT (params ?| ARRAY['risk', 'risk_policy', 'per_trade', 'portfolio', 'loss_limits',
                             'risk_pct_max', 'max_leverage', 'daily_loss_limit', 'max_drawdown'])
    )
);

CREATE TABLE cfg.release_bundles (
    release_id           TEXT        PRIMARY KEY,    -- semver, e.g. '0.4.0'
    git_sha              TEXT        NOT NULL,
    image_digest         TEXT,
    kernel_version       TEXT        NOT NULL,       -- pinned trading-kernel version
    feature_set_version  TEXT        NOT NULL,
    strategy_configs     TEXT[]      NOT NULL,
    model_versions       JSONB       NOT NULL,       -- {"meta_sweep_reclaim": "models:/meta_sweep_reclaim/12", ...}
    risk_policy_sha256   BYTEA       NOT NULL,       -- policy the release was validated under
    training_datasets    TEXT[]      NOT NULL DEFAULT '{}',
    parent_release_id    TEXT        REFERENCES cfg.release_bundles (release_id),
    stage                cfg.release_stage NOT NULL DEFAULT 'CANDIDATE',  -- maintained ONLY by trigger below
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes                TEXT
);

-- Bundles are immutable: they are always created as CANDIDATE, and afterwards only
-- `stage` may change — exclusively through cfg.apply_stage_transition().
CREATE FUNCTION cfg.release_bundles_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.stage := 'CANDIDATE';
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'release bundle % is immutable and cannot be deleted', OLD.release_id;
    END IF;
    IF (to_jsonb(NEW) - 'stage') IS DISTINCT FROM (to_jsonb(OLD) - 'stage') THEN
        RAISE EXCEPTION 'release bundle % is immutable except for its stage', OLD.release_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER release_bundles_guard
    BEFORE INSERT OR UPDATE OR DELETE ON cfg.release_bundles
    FOR EACH ROW EXECUTE FUNCTION cfg.release_bundles_guard();

CREATE TABLE cfg.release_stage_transitions (
    id           BIGSERIAL   PRIMARY KEY,
    release_id   TEXT        NOT NULL REFERENCES cfg.release_bundles (release_id),
    from_stage   cfg.release_stage NOT NULL,
    to_stage     cfg.release_stage NOT NULL,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_kind   ops.actor_kind NOT NULL,
    actor        TEXT        NOT NULL,
    evidence     JSONB       NOT NULL,               -- validation report ids, gate results
    reason       TEXT        NOT NULL,
    CONSTRAINT human_required_for_capital
        CHECK (to_stage NOT IN ('CANARY', 'CHAMPION') OR actor_kind = 'HUMAN')
);

-- Keep release_bundles.stage consistent with the append-only transition log, and only
-- allow the promotion pipeline's legal moves (no skipping gates).
-- RETIRED -> CHAMPION is the human rollback path (re-instating a previous champion).
CREATE FUNCTION cfg.apply_stage_transition() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, cfg AS $$
DECLARE
    current_stage cfg.release_stage;
    move          TEXT := NEW.from_stage::text || '>' || NEW.to_stage::text;
BEGIN
    SELECT stage INTO current_stage FROM cfg.release_bundles WHERE release_id = NEW.release_id FOR UPDATE;
    IF current_stage IS DISTINCT FROM NEW.from_stage THEN
        RAISE EXCEPTION 'release % is in stage %, not %', NEW.release_id, current_stage, NEW.from_stage;
    END IF;
    IF NOT (
        move = ANY (ARRAY[
            'CANDIDATE>BACKTEST_PASSED', 'BACKTEST_PASSED>WALKFORWARD_PASSED',
            'WALKFORWARD_PASSED>STRESS_PASSED', 'STRESS_PASSED>PAPER', 'PAPER>CANARY',
            'CANARY>CHAMPION', 'CANARY>ROLLED_BACK', 'CHAMPION>RETIRED', 'CHAMPION>ROLLED_BACK',
            'RETIRED>CHAMPION'])
        OR (NEW.to_stage = 'REJECTED'
            AND NEW.from_stage IN ('CANDIDATE', 'BACKTEST_PASSED', 'WALKFORWARD_PASSED',
                                   'STRESS_PASSED', 'PAPER', 'CANARY'))
    ) THEN
        RAISE EXCEPTION 'illegal release stage transition % for release %', move, NEW.release_id;
    END IF;
    UPDATE cfg.release_bundles SET stage = NEW.to_stage WHERE release_id = NEW.release_id;
    RETURN NEW;
END;
$$;

CREATE TRIGGER release_stage_transition_apply
    AFTER INSERT ON cfg.release_stage_transitions
    FOR EACH ROW EXECUTE FUNCTION cfg.apply_stage_transition();

-- Which release runs in which mode/role (history kept; one active per mode+role).
CREATE TABLE cfg.active_releases (
    id              BIGSERIAL   PRIMARY KEY,
    mode            trade.run_mode NOT NULL,
    role            TEXT        NOT NULL CHECK (role IN ('CHAMPION', 'CHALLENGER', 'CANARY')),
    release_id      TEXT        NOT NULL REFERENCES cfg.release_bundles (release_id),
    activated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_by    TEXT        NOT NULL,
    deactivated_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX active_release_one_per_role
    ON cfg.active_releases (mode, role) WHERE deactivated_at IS NULL;

-- =============================================================================
-- trade: decision journal, theses, orders, fills, positions
-- =============================================================================
CREATE TABLE trade.decision_cycles (
    cycle_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    mode           trade.run_mode NOT NULL,
    release_id     TEXT        NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL,
    trigger        TEXT        NOT NULL,      -- BAR_CLOSE_5M, SWEEP_EVENT, NEWS, POSITION_REVIEW, ...
    risk_state     risk.risk_state NOT NULL,
    data_health    JSONB       NOT NULL,
    universe_size  INT         NOT NULL,
    candidates     INT         NOT NULL
);
CREATE INDEX decision_cycles_time ON trade.decision_cycles (started_at);

CREATE TABLE trade.theses (
    thesis_id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id             UUID        NOT NULL REFERENCES trade.decision_cycles (cycle_id),
    created_at           TIMESTAMPTZ NOT NULL,
    symbol               TEXT        NOT NULL,
    side                 trade.side  NOT NULL,
    setup_family         TEXT        NOT NULL,
    config_version       TEXT        NOT NULL,
    horizon_minutes      INT         NOT NULL CHECK (horizon_minutes > 0),
    entry_plan           JSONB       NOT NULL,   -- {type, zone:[lo,hi], expiry_s, tactic}
    invalidation         JSONB       NOT NULL,   -- {stop_price, structural:[...], time_stop_min}
    targets              JSONB       NOT NULL,   -- [{price, fraction, kind}]
    p_win                DOUBLE PRECISION CHECK (p_win BETWEEN 0 AND 1),
    p_win_lower          DOUBLE PRECISION CHECK (p_win_lower BETWEEN 0 AND 1),
    reward_r             DOUBLE PRECISION,
    loss_r               DOUBLE PRECISION,       -- >= 1: includes modeled stop slippage
    cost_r               DOUBLE PRECISION,       -- fees + entry slippage + expected funding, in R
    ev_r                 DOUBLE PRECISION,
    ev_r_lower           DOUBLE PRECISION,
    evidence             JSONB       NOT NULL,   -- supporting facts with sources & values
    counter_evidence     JSONB       NOT NULL,   -- reasons it may fail
    intel_snapshot_id    UUID        NOT NULL,
    feature_set_version  TEXT        NOT NULL,
    model_versions       JSONB       NOT NULL
);
CREATE INDEX theses_symbol_time ON trade.theses (symbol, created_at);

CREATE TABLE trade.decisions (
    decision_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id            UUID        NOT NULL REFERENCES trade.decision_cycles (cycle_id),
    decided_at          TIMESTAMPTZ NOT NULL,
    mode                trade.run_mode NOT NULL,
    symbol              TEXT        NOT NULL,
    action              trade.decision_action NOT NULL,
    chosen_thesis_id    UUID        REFERENCES trade.theses (thesis_id),
    alternatives        JSONB       NOT NULL,   -- [{thesis_id, side, ev_r_lower, rejected_because:[...]}]
    rationale           TEXT        NOT NULL,   -- human-readable, generated from structured data
    explanation         JSONB       NOT NULL,   -- top feature contributions, rule hits
    release_id          TEXT        NOT NULL,
    risk_policy_sha256  BYTEA       NOT NULL,
    CONSTRAINT open_requires_thesis
        CHECK (action NOT IN ('OPEN_LONG', 'OPEN_SHORT') OR chosen_thesis_id IS NOT NULL)
);
CREATE INDEX decisions_symbol_time ON trade.decisions (symbol, decided_at);

CREATE TABLE trade.positions (
    position_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    mode              trade.run_mode NOT NULL,
    symbol            TEXT        NOT NULL,
    side              trade.side  NOT NULL,
    thesis_id         UUID        REFERENCES trade.theses (thesis_id),   -- NULL only for adopted/unknown positions
    release_id        TEXT        NOT NULL,
    status            TEXT        NOT NULL CHECK (status IN ('OPENING', 'OPEN', 'CLOSING', 'CLOSED')),
    opened_at         TIMESTAMPTZ NOT NULL,
    closed_at         TIMESTAMPTZ,
    entry_avg_price   NUMERIC(38,18),
    exit_avg_price    NUMERIC(38,18),
    max_qty           NUMERIC(38,18) NOT NULL DEFAULT 0,
    initial_stop      NUMERIC(38,18) NOT NULL,
    current_stop      NUMERIC(38,18) NOT NULL,
    initial_risk_usd  NUMERIC(38,18) NOT NULL CHECK (initial_risk_usd > 0),
    pnl_gross_usd     NUMERIC(38,18),
    fees_usd          NUMERIC(38,18) NOT NULL DEFAULT 0,
    funding_usd       NUMERIC(38,18) NOT NULL DEFAULT 0,
    pnl_net_usd       NUMERIC(38,18),
    r_multiple        DOUBLE PRECISION,
    mae_r             DOUBLE PRECISION,
    mfe_r             DOUBLE PRECISION,
    exit_reason       TEXT        -- STOP, TARGET, TIME_STOP, INVALIDATION, RISK_STATE, KILL_SWITCH, WATCHDOG, MANUAL
);
CREATE INDEX positions_open ON trade.positions (symbol) WHERE status <> 'CLOSED';

-- Stops may only be tightened (never widened) once a position is open.
CREATE FUNCTION trade.forbid_stop_widening() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.current_stop IS DISTINCT FROM OLD.current_stop THEN
        IF (NEW.side = 'LONG'  AND NEW.current_stop < OLD.current_stop) OR
           (NEW.side = 'SHORT' AND NEW.current_stop > OLD.current_stop) THEN
            RAISE EXCEPTION 'stop for position % may only be tightened', NEW.position_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER positions_stop_only_tightens
    BEFORE UPDATE OF current_stop ON trade.positions
    FOR EACH ROW EXECUTE FUNCTION trade.forbid_stop_widening();

CREATE TABLE trade.orders (
    order_id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    client_order_id    TEXT        NOT NULL UNIQUE,  -- deterministic from decision/leg/attempt => idempotent retries
    exchange_order_id  BIGINT,
    algo_id            BIGINT,                       -- conditional orders placed via the Algo Order API
    mode               trade.run_mode NOT NULL,
    decision_id        UUID        REFERENCES trade.decisions (decision_id),
    position_id        UUID        REFERENCES trade.positions (position_id),
    symbol             TEXT        NOT NULL,
    purpose            trade.order_purpose NOT NULL,
    side               TEXT        NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type         TEXT        NOT NULL,         -- LIMIT, MARKET, STOP_MARKET, TAKE_PROFIT_MARKET, ...
    time_in_force      TEXT,                         -- GTC, IOC, FOK, GTX (post-only), GTD
    price              NUMERIC(38,18),
    trigger_price      NUMERIC(38,18),
    quantity           NUMERIC(38,18),
    reduce_only        BOOLEAN     NOT NULL DEFAULT FALSE,
    close_position     BOOLEAN     NOT NULL DEFAULT FALSE,
    working_type       TEXT,                         -- MARK_PRICE / CONTRACT_PRICE
    status             trade.order_status NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL,
    CONSTRAINT entry_requires_decision
        CHECK (purpose <> 'ENTRY' OR decision_id IS NOT NULL),
    CONSTRAINT protective_orders_cannot_increase_exposure
        CHECK (purpose = 'ENTRY' OR reduce_only OR close_position)
);
CREATE INDEX orders_position ON trade.orders (position_id);
CREATE INDEX orders_symbol_time ON trade.orders (symbol, created_at);

CREATE TABLE trade.order_events (
    event_id     BIGSERIAL   PRIMARY KEY,
    order_id     UUID        NOT NULL REFERENCES trade.orders (order_id),
    event_ts     TIMESTAMPTZ NOT NULL,     -- exchange event time
    recv_ts      TIMESTAMPTZ NOT NULL,
    event_type   TEXT        NOT NULL,     -- SUBMITTED, ACK, PARTIAL_FILL, FILL, CANCELED, REJECTED, TRIGGERED, EXPIRED
    status       trade.order_status NOT NULL,
    filled_qty   NUMERIC(38,18),
    avg_price    NUMERIC(38,18),
    reject_code  INT,
    reject_msg   TEXT,
    raw          JSONB       NOT NULL
);
CREATE INDEX order_events_order ON trade.order_events (order_id, event_ts);

CREATE TABLE trade.fills (
    fill_id                BIGSERIAL   PRIMARY KEY,
    order_id               UUID        NOT NULL REFERENCES trade.orders (order_id),
    exchange_trade_id      BIGINT,
    fill_ts                TIMESTAMPTZ NOT NULL,
    price                  NUMERIC(38,18) NOT NULL,
    qty                    NUMERIC(38,18) NOT NULL CHECK (qty > 0),
    fee                    NUMERIC(38,18) NOT NULL,
    fee_asset              TEXT        NOT NULL,
    is_maker               BOOLEAN     NOT NULL,
    realized_pnl           NUMERIC(38,18),
    arrival_mid            NUMERIC(38,18),          -- mid at decision time => implementation shortfall
    expected_slippage_bps  DOUBLE PRECISION,
    UNIQUE (order_id, exchange_trade_id)
);

CREATE TABLE trade.position_events (
    event_id     BIGSERIAL   PRIMARY KEY,
    position_id  UUID        NOT NULL REFERENCES trade.positions (position_id),
    event_ts     TIMESTAMPTZ NOT NULL,
    event_type   TEXT        NOT NULL,   -- OPENED, SCALED_IN, PARTIAL_TP, STOP_TIGHTENED, INVALIDATED, ADOPTED, CLOSED
    details      JSONB       NOT NULL
);

CREATE TABLE trade.funding_payments (
    id           BIGSERIAL   PRIMARY KEY,
    position_id  UUID        REFERENCES trade.positions (position_id),
    symbol       TEXT        NOT NULL,
    funding_ts   TIMESTAMPTZ NOT NULL,
    rate         NUMERIC(18,10) NOT NULL,
    amount_usd   NUMERIC(38,18) NOT NULL
);

CREATE TABLE trade.account_snapshots (
    snapshot_ts            TIMESTAMPTZ NOT NULL,
    mode                   trade.run_mode NOT NULL,
    source                 TEXT        NOT NULL CHECK (source IN ('EXCHANGE', 'INTERNAL', 'SIMULATED')),
    equity_usd             NUMERIC(38,18) NOT NULL,
    wallet_usd             NUMERIC(38,18) NOT NULL,
    unrealized_usd         NUMERIC(38,18) NOT NULL,
    margin_ratio           DOUBLE PRECISION,
    gross_exposure_usd     NUMERIC(38,18) NOT NULL,
    net_beta_exposure_usd  NUMERIC(38,18),
    open_risk_usd          NUMERIC(38,18) NOT NULL,
    PRIMARY KEY (mode, snapshot_ts, source)
);

CREATE TABLE trade.daily_pnl (
    trading_day          DATE        NOT NULL,       -- UTC day
    mode                 trade.run_mode NOT NULL,
    start_equity_usd     NUMERIC(38,18) NOT NULL,
    realized_usd         NUMERIC(38,18) NOT NULL DEFAULT 0,
    fees_usd             NUMERIC(38,18) NOT NULL DEFAULT 0,
    funding_usd          NUMERIC(38,18) NOT NULL DEFAULT 0,
    min_equity_usd       NUMERIC(38,18),
    max_intraday_dd_pct  DOUBLE PRECISION,
    trades               INT         NOT NULL DEFAULT 0,
    loss_limit_state     TEXT        NOT NULL DEFAULT 'OK',
    PRIMARY KEY (trading_day, mode)
);

-- =============================================================================
-- risk: verdicts, state machine, events, watchdog actions
-- =============================================================================
CREATE TABLE risk.verdicts (
    verdict_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id         UUID        NOT NULL REFERENCES trade.decisions (decision_id),
    evaluated_at        TIMESTAMPTZ NOT NULL,
    approved            BOOLEAN     NOT NULL,
    risk_state          risk.risk_state NOT NULL,
    checks              JSONB       NOT NULL,   -- [{rule, value, limit, passed}]
    sizing              JSONB,                  -- {equity, risk_pct, multipliers{}, qty, notional, leverage, liq_price}
    rejection_reasons   TEXT[]      NOT NULL DEFAULT '{}',
    risk_policy_sha256  BYTEA       NOT NULL,
    CONSTRAINT approved_requires_sizing CHECK (NOT approved OR sizing IS NOT NULL)
);
CREATE INDEX verdicts_decision ON risk.verdicts (decision_id);

CREATE TABLE risk.state_transitions (
    id            BIGSERIAL   PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode          trade.run_mode NOT NULL,
    scope         TEXT        NOT NULL,      -- 'GLOBAL' | 'SYMBOL:BTCUSDT' | 'SETUP:sweep_reclaim'
    from_state    risk.risk_state NOT NULL,
    to_state      risk.risk_state NOT NULL,
    trigger_rule  TEXT        NOT NULL,
    details       JSONB       NOT NULL,
    actor_kind    ops.actor_kind NOT NULL,
    actor         TEXT        NOT NULL,
    -- Automatic actors may escalate at any time (enum order = severity), but may only
    -- de-escalate out of the mild states. Leaving FLATTEN/HALTED requires a human.
    CONSTRAINT automatic_deescalation_limited CHECK (
        actor_kind = 'HUMAN'
        OR to_state >= from_state
        OR from_state IN ('CAUTION', 'REDUCE_ONLY')
    )
);
CREATE INDEX state_transitions_scope_time ON risk.state_transitions (scope, at);

CREATE TABLE risk.events (
    id            BIGSERIAL   PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode          trade.run_mode NOT NULL,
    severity      TEXT        NOT NULL CHECK (severity IN ('P1', 'P2', 'P3')),
    rule          TEXT        NOT NULL,
    scope         TEXT        NOT NULL,
    observed      JSONB       NOT NULL,      -- {value, limit, ...}
    action_taken  TEXT        NOT NULL
);

CREATE TABLE risk.watchdog_actions (
    id          BIGSERIAL   PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    check_name  TEXT        NOT NULL,
    finding     JSONB       NOT NULL,
    action      TEXT        NOT NULL CHECK (action IN ('ALERT', 'PLACE_EMERGENCY_STOP', 'CANCEL_ENTRIES',
                                                        'REDUCE', 'FLATTEN', 'FORCE_REDUCE_ONLY')),
    result      JSONB       NOT NULL
);

-- =============================================================================
-- analytics: autopsies and outcomes for ALL theses (taken and rejected)
-- =============================================================================
CREATE TABLE analytics.trade_autopsies (
    position_id       UUID        NOT NULL REFERENCES trade.positions (position_id),
    analysis_version  TEXT        NOT NULL,
    analyzed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome_class     TEXT        NOT NULL,   -- WIN_THESIS_CORRECT, WIN_LUCKY, LOSS_THESIS_WRONG, LOSS_TIMING,
                                              -- LOSS_EXECUTION, LOSS_REGIME_SHOCK, LOSS_DATA, LOSS_RISK_EXIT
    decision_quality  TEXT        NOT NULL CHECK (decision_quality IN ('GOOD', 'ACCEPTABLE', 'POOR', 'UNKNOWN')),
    primary_cause     TEXT        NOT NULL,
    secondary_causes  TEXT[]      NOT NULL DEFAULT '{}',
    metrics           JSONB       NOT NULL,   -- mae/mfe, slippage vs model, time-to-mfe, BTC move during trade, ...
    counterfactuals   JSONB       NOT NULL,   -- no-trade, opposite side, wider/tighter stop, hold-to-target, ...
    notes             TEXT,
    PRIMARY KEY (position_id, analysis_version)
);

-- Learning only from trades we took is selection-biased: label rejected theses too.
CREATE TABLE analytics.thesis_outcomes (
    thesis_id         UUID        NOT NULL REFERENCES trade.theses (thesis_id),
    labeling_version  TEXT        NOT NULL,
    labeled_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    taken             BOOLEAN     NOT NULL,
    barrier_hit       TEXT        NOT NULL CHECK (barrier_hit IN ('TARGET', 'STOP', 'TIME', 'ENTRY_NOT_REACHED', 'AMBIGUOUS')),
    realized_r        DOUBLE PRECISION,       -- hypothetical (after modeled costs) for rejected theses
    mae_r             DOUBLE PRECISION,
    mfe_r             DOUBLE PRECISION,
    minutes_to_outcome INT,
    PRIMARY KEY (thesis_id, labeling_version)
);

-- =============================================================================
-- ml: datasets, pre-registered hypotheses, optimizer studies, validation, lockbox
-- =============================================================================
CREATE TABLE ml.dataset_snapshots (
    dataset_id   TEXT        PRIMARY KEY,    -- content-addressed (sha256 of manifest)
    spec         JSONB       NOT NULL,       -- symbols, period, feature_set_version, labeling_version, as-of rules
    uri          TEXT        NOT NULL,
    row_count    BIGINT      NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ml.research_hypotheses (
    hypothesis_id    TEXT        PRIMARY KEY,
    registered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    statement        TEXT        NOT NULL,
    expected_effect  TEXT        NOT NULL,
    test_plan        TEXT        NOT NULL,
    status           TEXT        NOT NULL CHECK (status IN ('REGISTERED', 'TESTING', 'SUPPORTED', 'REJECTED', 'ABANDONED')),
    result_summary   TEXT
);

CREATE TABLE ml.optimizer_studies (
    study_id       TEXT        PRIMARY KEY,
    setup_family   TEXT        NOT NULL,
    search_space   JSONB       NOT NULL,
    objective      TEXT        NOT NULL,
    n_trials       INT         NOT NULL,     -- ALL trials, needed for the deflated Sharpe ratio
    best_trial     JSONB,
    dsr            DOUBLE PRECISION,
    pbo            DOUBLE PRECISION,
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ
);

CREATE TABLE ml.validation_reports (
    report_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id     TEXT        REFERENCES cfg.release_bundles (release_id),
    kind           TEXT        NOT NULL CHECK (kind IN ('BACKTEST', 'WALK_FORWARD', 'CPCV', 'STRESS', 'MONTE_CARLO',
                                                         'LOCKBOX', 'PAPER', 'CANARY', 'CHAMPION_COMPARISON')),
    period_start   TIMESTAMPTZ NOT NULL,
    period_end     TIMESTAMPTZ NOT NULL,
    metrics        JSONB       NOT NULL,
    gates          JSONB       NOT NULL,     -- [{gate, threshold, value, passed}]
    passed         BOOLEAN     NOT NULL,
    artifact_uri   TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (period_end > period_start)
);

-- The lockbox (final untouched holdout) may be used ONCE per release candidate.
CREATE TABLE ml.lockbox_access (
    id            BIGSERIAL   PRIMARY KEY,
    period_start  TIMESTAMPTZ NOT NULL,
    period_end    TIMESTAMPTZ NOT NULL,
    release_id    TEXT        NOT NULL REFERENCES cfg.release_bundles (release_id),
    accessed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    accessed_by   TEXT        NOT NULL,
    UNIQUE (period_start, period_end, release_id)
);

-- =============================================================================
-- ops: data health incidents & audit log
-- =============================================================================
CREATE TABLE ops.data_health_incidents (
    id          BIGSERIAL   PRIMARY KEY,
    source      TEXT        NOT NULL,      -- e.g. 'binance.um.public.depth'
    symbol      TEXT,
    kind        TEXT        NOT NULL,      -- STALE, GAP, RESYNC, LAG, CLOCK_DRIFT, OUTLIER, REST_ERRORS, ...
    severity    TEXT        NOT NULL CHECK (severity IN ('DEGRADED', 'FAILED')),
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    details     JSONB       NOT NULL
);

CREATE TABLE ops.audit_log (
    id            BIGSERIAL   PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_kind    ops.actor_kind NOT NULL,
    actor         TEXT        NOT NULL,
    action        TEXT        NOT NULL,
    target        TEXT        NOT NULL,
    before_state  JSONB,
    after_state   JSONB,
    source_ip     INET
);

-- =============================================================================
-- Append-only enforcement (defense in depth: grants below are the primary control)
-- =============================================================================
CREATE FUNCTION ops.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table %.% is append-only (% rejected)', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP;
END;
$$;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'cfg.risk_policies', 'cfg.risk_policy_approvals', 'cfg.release_stage_transitions', 'cfg.strategy_configs',
        'trade.decision_cycles', 'trade.theses', 'trade.decisions', 'trade.order_events', 'trade.fills',
        'trade.position_events', 'trade.funding_payments',
        'risk.verdicts', 'risk.state_transitions', 'risk.events', 'risk.watchdog_actions',
        'ml.lockbox_access', 'ops.audit_log']
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION ops.forbid_mutation()',
                       'append_only_' || replace(t, '.', '_'), t);
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION ops.forbid_mutation()',
                       'no_truncate_' || replace(t, '.', '_'), t);
    END LOOP;
END $$;

-- =============================================================================
-- Roles & least-privilege grants (group roles; per-service login users are members)
-- =============================================================================
DO $$
DECLARE
    r TEXT;
BEGIN
    FOREACH r IN ARRAY ARRAY['wt_collector', 'wt_trader', 'wt_watchdog', 'wt_learning',
                             'wt_risk_admin', 'wt_release_admin', 'wt_readonly']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END $$;

-- collectors: reference data + data health only
GRANT USAGE ON SCHEMA ref, ops TO wt_collector;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA ref TO wt_collector;   -- UPDATE only to close valid_to
GRANT INSERT ON ops.data_health_incidents TO wt_collector;
GRANT UPDATE (ended_at) ON ops.data_health_incidents TO wt_collector;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA ref, ops TO wt_collector;

-- trader: writes the journal; reads configs; can NEVER write risk policies or releases
GRANT USAGE ON SCHEMA ref, cfg, trade, risk, ops TO wt_trader;
GRANT SELECT ON ALL TABLES IN SCHEMA ref, cfg TO wt_trader;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA trade, risk TO wt_trader;
GRANT UPDATE ON trade.orders, trade.positions, trade.daily_pnl TO wt_trader;
GRANT INSERT ON ops.data_health_incidents, ops.audit_log TO wt_trader;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA trade, risk, ops TO wt_trader;

-- watchdog: reads state, records its own actions and risk escalations
GRANT USAGE ON SCHEMA trade, risk, cfg, ops TO wt_watchdog;
GRANT SELECT ON ALL TABLES IN SCHEMA trade, risk TO wt_watchdog;
GRANT SELECT ON cfg.effective_risk_policy, cfg.risk_policies TO wt_watchdog;
GRANT INSERT ON risk.watchdog_actions, risk.state_transitions, risk.events, ops.audit_log TO wt_watchdog;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA risk, ops TO wt_watchdog;

-- learning/optimizer: reads everything it learns from; may PROPOSE candidates; nothing on risk policies
GRANT USAGE ON SCHEMA ref, cfg, trade, risk, analytics, ml TO wt_learning;
GRANT SELECT ON ALL TABLES IN SCHEMA ref, trade, risk TO wt_learning;
GRANT SELECT ON cfg.strategy_configs, cfg.release_bundles, cfg.release_stage_transitions, cfg.active_releases TO wt_learning;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA analytics, ml TO wt_learning;
GRANT INSERT ON cfg.strategy_configs, cfg.release_bundles, cfg.release_stage_transitions TO wt_learning;
GRANT INSERT ON cfg.active_releases TO wt_learning;               -- restricted to BACKTEST/PAPER by RLS below
GRANT UPDATE (deactivated_at) ON cfg.active_releases TO wt_learning;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA cfg, ml TO wt_learning;
-- deliberately NO grant of any kind on cfg.risk_policies / cfg.risk_policy_approvals to wt_learning

-- humans
GRANT USAGE ON SCHEMA ref, cfg, trade, risk, analytics, ml, ops TO wt_risk_admin, wt_release_admin, wt_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA ref, cfg, trade, risk, analytics, ml, ops TO wt_risk_admin, wt_release_admin, wt_readonly;
GRANT INSERT ON cfg.risk_policies, cfg.risk_policy_approvals, ops.audit_log TO wt_risk_admin;
GRANT INSERT ON cfg.release_stage_transitions, cfg.active_releases, ops.audit_log TO wt_release_admin;
GRANT UPDATE (deactivated_at) ON cfg.active_releases TO wt_release_admin;
GRANT INSERT ON risk.state_transitions TO wt_risk_admin;          -- human reset out of FLATTEN/HALTED
GRANT USAGE ON ALL SEQUENCES IN SCHEMA cfg, risk, ops TO wt_risk_admin, wt_release_admin;

-- Row-level security: automated roles can never promote to capital stages or activate live releases.
ALTER TABLE cfg.release_stage_transitions ENABLE ROW LEVEL SECURITY;
CREATE POLICY rst_read_all       ON cfg.release_stage_transitions FOR SELECT USING (true);
CREATE POLICY rst_learning_insert ON cfg.release_stage_transitions FOR INSERT TO wt_learning
    WITH CHECK (to_stage NOT IN ('CANARY', 'CHAMPION') AND actor_kind IN ('SYSTEM', 'OPTIMIZER'));
CREATE POLICY rst_human_insert    ON cfg.release_stage_transitions FOR INSERT TO wt_release_admin
    WITH CHECK (actor_kind = 'HUMAN');

ALTER TABLE cfg.active_releases ENABLE ROW LEVEL SECURITY;
CREATE POLICY ar_read_all         ON cfg.active_releases FOR SELECT USING (true);
CREATE POLICY ar_learning_insert  ON cfg.active_releases FOR INSERT TO wt_learning
    WITH CHECK (mode IN ('BACKTEST', 'PAPER'));
CREATE POLICY ar_learning_update  ON cfg.active_releases FOR UPDATE TO wt_learning
    USING (mode IN ('BACKTEST', 'PAPER')) WITH CHECK (mode IN ('BACKTEST', 'PAPER'));
CREATE POLICY ar_human_insert     ON cfg.active_releases FOR INSERT TO wt_release_admin WITH CHECK (true);
CREATE POLICY ar_human_update     ON cfg.active_releases FOR UPDATE TO wt_release_admin USING (true) WITH CHECK (true);

ALTER TABLE risk.state_transitions ENABLE ROW LEVEL SECURITY;
CREATE POLICY st_read_all         ON risk.state_transitions FOR SELECT USING (true);
CREATE POLICY st_trader_insert    ON risk.state_transitions FOR INSERT TO wt_trader
    WITH CHECK (actor_kind = 'SYSTEM');
CREATE POLICY st_watchdog_insert  ON risk.state_transitions FOR INSERT TO wt_watchdog
    WITH CHECK (actor_kind = 'WATCHDOG' AND to_state >= from_state);
CREATE POLICY st_human_insert     ON risk.state_transitions FOR INSERT TO wt_risk_admin
    WITH CHECK (actor_kind = 'HUMAN');

COMMIT;
