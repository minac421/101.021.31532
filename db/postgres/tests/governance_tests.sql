-- =============================================================================
-- Governance tests for db/postgres/001_core_schema.sql
-- -----------------------------------------------------------------------------
-- Proves that capital-protection governance is enforced by the database itself:
-- every "expect_fail" must be blocked, every "expect_ok" must pass.
-- Run against a THROWAWAY database right after applying the schema, e.g.:
--   psql -d wt_test -v ON_ERROR_STOP=1 -f db/postgres/001_core_schema.sql
--   psql -d wt_test -f db/postgres/tests/governance_tests.sql 2>&1 | grep -E 'PASS|FAIL'
-- Expected: 29 PASS, 0 FAIL.
-- =============================================================================
\set ON_ERROR_STOP 1
SET client_min_messages = notice;

CREATE FUNCTION public.expect_fail(label text, r text, stmt text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE format('SET LOCAL ROLE %I', r);
  BEGIN
    EXECUTE stmt;
    RAISE WARNING 'FAIL (statement succeeded): %', label;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'PASS (blocked): % -> %', label, SQLERRM;
  END;
  RESET ROLE;
END $$;

CREATE FUNCTION public.expect_ok(label text, r text, stmt text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE format('SET LOCAL ROLE %I', r);
  BEGIN
    EXECUTE stmt;
    RAISE NOTICE 'PASS (allowed): %', label;
  EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'FAIL (unexpected error): % -> %', label, SQLERRM;
  END;
  RESET ROLE;
END $$;

-- fixtures (as owner)
INSERT INTO trade.decision_cycles (cycle_id, mode, release_id, started_at, trigger, risk_state, data_health, universe_size, candidates)
VALUES ('00000000-0000-0000-0000-000000000001','PAPER','0.1.0', now(), 'BAR_CLOSE_5M', 'NORMAL', '{}', 300, 12);
INSERT INTO trade.theses (thesis_id, cycle_id, created_at, symbol, side, setup_family, config_version, horizon_minutes,
  entry_plan, invalidation, targets, evidence, counter_evidence, intel_snapshot_id, feature_set_version, model_versions)
VALUES ('00000000-0000-0000-0000-0000000000a1','00000000-0000-0000-0000-000000000001', now(), 'SOLUSDT','LONG','sweep_reclaim','sweep_reclaim@1.0.0', 240,
  '{}','{"stop_price":182.1}','[]','[]','[]', gen_random_uuid(), 'fs-1', '{}');
INSERT INTO trade.decisions (decision_id, cycle_id, decided_at, mode, symbol, action, chosen_thesis_id, alternatives, rationale, explanation, release_id, risk_policy_sha256)
VALUES ('00000000-0000-0000-0000-0000000000d1','00000000-0000-0000-0000-000000000001', now(), 'PAPER','SOLUSDT','OPEN_LONG','00000000-0000-0000-0000-0000000000a1','[]','test','{}','0.1.0','\x00');
INSERT INTO trade.positions (position_id, mode, symbol, side, thesis_id, release_id, status, opened_at, initial_stop, current_stop, initial_risk_usd)
VALUES ('00000000-0000-0000-0000-0000000000b1','PAPER','SOLUSDT','LONG','00000000-0000-0000-0000-0000000000a1','0.1.0','OPEN', now(), 182.1, 182.1, 25);

BEGIN;
SELECT expect_fail('optimizer cannot smuggle risk keys into strategy params', 'wt_learning',
  $s$INSERT INTO cfg.strategy_configs (config_version, setup_family, params, params_sha256, created_by)
     VALUES ('x@1','sweep_reclaim','{"risk_pct_max":2.0,"entry_buffer_atr":0.2}','\x01','OPTIMIZER')$s$);
SELECT expect_ok('optimizer may propose plain strategy params', 'wt_learning',
  $s$INSERT INTO cfg.strategy_configs (config_version, setup_family, params, params_sha256, created_by)
     VALUES ('sweep_reclaim@1.1.0','sweep_reclaim','{"entry_buffer_atr":0.2}','\x02','OPTIMIZER')$s$);
SELECT expect_fail('learning cannot write risk policies', 'wt_learning',
  $s$INSERT INTO cfg.risk_policies (policy_version, policy_yaml, policy_sha256, signature, signed_by, change_summary, loosens_limits, effective_from)
     VALUES (99,'x','\x03','sig','me','x',false, now())$s$);
SELECT expect_fail('learning cannot even read risk policy table', 'wt_learning',
  $s$SELECT count(*) FROM cfg.risk_policies$s$);
SELECT expect_ok('learning may register a release candidate (stage forced to CANDIDATE)', 'wt_learning',
  $s$INSERT INTO cfg.release_bundles (release_id, git_sha, kernel_version, feature_set_version, strategy_configs, model_versions, risk_policy_sha256, stage)
     VALUES ('0.2.0','abc','2.0.0','fs-1','{sweep_reclaim@1.1.0}','{}','\x00','CHAMPION')$s$);
COMMIT;
SELECT release_id, stage FROM cfg.release_bundles;

BEGIN;
SELECT expect_ok('learning may advance through validation gates', 'wt_learning',
  $s$INSERT INTO cfg.release_stage_transitions (release_id, from_stage, to_stage, actor_kind, actor, evidence, reason)
     VALUES ('0.2.0','CANDIDATE','BACKTEST_PASSED','SYSTEM','validation-farm','{}','gates passed')$s$);
SELECT expect_fail('learning cannot skip gates', 'wt_learning',
  $s$INSERT INTO cfg.release_stage_transitions (release_id, from_stage, to_stage, actor_kind, actor, evidence, reason)
     VALUES ('0.2.0','BACKTEST_PASSED','PAPER','SYSTEM','validation-farm','{}','skip')$s$);
SELECT expect_fail('learning cannot promote to capital even if it claims to be HUMAN', 'wt_learning',
  $s$INSERT INTO cfg.release_stage_transitions (release_id, from_stage, to_stage, actor_kind, actor, evidence, reason)
     VALUES ('0.2.0','BACKTEST_PASSED','CANARY','HUMAN','fake','{}','x')$s$);
SELECT expect_fail('learning cannot activate a LIVE release', 'wt_learning',
  $s$INSERT INTO cfg.active_releases (mode, role, release_id, activated_by) VALUES ('LIVE','CHAMPION','0.2.0','optimizer')$s$);
SELECT expect_ok('learning may run a PAPER challenger', 'wt_learning',
  $s$INSERT INTO cfg.active_releases (mode, role, release_id, activated_by) VALUES ('PAPER','CHALLENGER','0.2.0','optimizer')$s$);
SELECT expect_fail('learning cannot edit a release bundle', 'wt_learning',
  $s$UPDATE cfg.release_bundles SET model_versions = '{"x":1}' WHERE release_id = '0.2.0'$s$);
SELECT expect_fail('even a human cannot skip from BACKTEST_PASSED to CHAMPION', 'wt_release_admin',
  $s$INSERT INTO cfg.release_stage_transitions (release_id, from_stage, to_stage, actor_kind, actor, evidence, reason)
     VALUES ('0.2.0','BACKTEST_PASSED','CHAMPION','HUMAN','operator','{}','x')$s$);
COMMIT;

BEGIN;
SELECT expect_fail('risk policy loosening without 24h cooldown is rejected', 'wt_risk_admin',
  $s$INSERT INTO cfg.risk_policies (policy_version, policy_yaml, policy_sha256, signature, signed_by, change_summary, loosens_limits, effective_from)
     VALUES (2,'y','\x05','sig','owner','raise daily limit', true, now() + interval '1 hour')$s$);
SELECT expect_ok('risk policy loosening with cooldown is accepted (takes effect later)', 'wt_risk_admin',
  $s$INSERT INTO cfg.risk_policies (policy_version, policy_yaml, policy_sha256, signature, signed_by, change_summary, loosens_limits, effective_from)
     VALUES (3,'z','\x06','sig','owner','raise daily limit', true, now() + interval '25 hours')$s$);
SELECT expect_ok('risk policy tightening is effective immediately', 'wt_risk_admin',
  $s$INSERT INTO cfg.risk_policies (policy_version, policy_yaml, policy_sha256, signature, signed_by, change_summary, loosens_limits, effective_from)
     VALUES (4,'w','\x07','sig','owner','lower risk per trade', false, now())$s$);
COMMIT;

BEGIN;
SELECT expect_ok('trader may escalate risk state', 'wt_trader',
  $s$INSERT INTO risk.state_transitions (mode, scope, from_state, to_state, trigger_rule, details, actor_kind, actor)
     VALUES ('PAPER','GLOBAL','NORMAL','HALTED','daily_loss_flatten','{}','SYSTEM','risk-guardian')$s$);
SELECT expect_ok('trader may auto-recover from CAUTION', 'wt_trader',
  $s$INSERT INTO risk.state_transitions (mode, scope, from_state, to_state, trigger_rule, details, actor_kind, actor)
     VALUES ('PAPER','GLOBAL','CAUTION','NORMAL','cleared','{}','SYSTEM','risk-guardian')$s$);
SELECT expect_fail('trader cannot auto-leave HALTED', 'wt_trader',
  $s$INSERT INTO risk.state_transitions (mode, scope, from_state, to_state, trigger_rule, details, actor_kind, actor)
     VALUES ('PAPER','GLOBAL','HALTED','NORMAL','auto','{}','SYSTEM','risk-guardian')$s$);
SELECT expect_fail('trader cannot impersonate a human to leave HALTED', 'wt_trader',
  $s$INSERT INTO risk.state_transitions (mode, scope, from_state, to_state, trigger_rule, details, actor_kind, actor)
     VALUES ('PAPER','GLOBAL','HALTED','NORMAL','auto','{}','HUMAN','fake')$s$);
SELECT expect_fail('watchdog may not de-escalate', 'wt_watchdog',
  $s$INSERT INTO risk.state_transitions (mode, scope, from_state, to_state, trigger_rule, details, actor_kind, actor)
     VALUES ('PAPER','GLOBAL','REDUCE_ONLY','NORMAL','x','{}','WATCHDOG','watchdog')$s$);
SELECT expect_ok('human may reset HALTED after review', 'wt_risk_admin',
  $s$INSERT INTO risk.state_transitions (mode, scope, from_state, to_state, trigger_rule, details, actor_kind, actor)
     VALUES ('PAPER','GLOBAL','HALTED','REDUCE_ONLY','incident-review-42','{}','HUMAN','operator')$s$);
COMMIT;

BEGIN;
SELECT expect_fail('trader cannot rewrite the decision journal', 'wt_trader',
  $s$UPDATE trade.decisions SET rationale = 'rewritten' WHERE decision_id = '00000000-0000-0000-0000-0000000000d1'$s$);
SELECT expect_fail('stop may not be widened on a LONG', 'wt_trader',
  $s$UPDATE trade.positions SET current_stop = 180.0 WHERE position_id = '00000000-0000-0000-0000-0000000000b1'$s$);
SELECT expect_ok('stop may be tightened on a LONG', 'wt_trader',
  $s$UPDATE trade.positions SET current_stop = 183.0 WHERE position_id = '00000000-0000-0000-0000-0000000000b1'$s$);
SELECT expect_fail('entry order requires a decision', 'wt_trader',
  $s$INSERT INTO trade.orders (client_order_id, mode, symbol, purpose, side, order_type, status, created_at, updated_at)
     VALUES ('c1','PAPER','SOLUSDT','ENTRY','BUY','LIMIT','NEW', now(), now())$s$);
SELECT expect_fail('protective order must be reduce-only/close-position', 'wt_trader',
  $s$INSERT INTO trade.orders (client_order_id, mode, symbol, purpose, side, order_type, status, created_at, updated_at)
     VALUES ('c2','PAPER','SOLUSDT','STOP_LOSS','SELL','STOP_MARKET','NEW', now(), now())$s$);
SELECT expect_ok('protective close-position stop is accepted', 'wt_trader',
  $s$INSERT INTO trade.orders (client_order_id, mode, symbol, purpose, side, order_type, close_position, working_type, trigger_price, status, created_at, updated_at, position_id)
     VALUES ('c3','PAPER','SOLUSDT','STOP_LOSS','SELL','STOP_MARKET', true, 'MARK_PRICE', 182.1, 'NEW', now(), now(), '00000000-0000-0000-0000-0000000000b1')$s$);
COMMIT;

-- owner/superuser path: triggers still block mutation of journal rows
DO $$ BEGIN
  BEGIN
    DELETE FROM trade.decisions;
    RAISE WARNING 'FAIL: owner could delete journal';
  EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'PASS (blocked): owner delete on journal -> %', SQLERRM; END;
  BEGIN
    TRUNCATE risk.state_transitions;
    RAISE WARNING 'FAIL: owner could truncate';
  EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'PASS (blocked): owner truncate -> %', SQLERRM; END;
END $$;

SELECT policy_version, loosens_limits, effective_from > now() AS pending FROM cfg.risk_policies ORDER BY 1;
