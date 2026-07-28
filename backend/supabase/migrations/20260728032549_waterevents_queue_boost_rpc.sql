-- queue_boost RPC — hand-steer which companies the full BFS scans NEXT, without stopping the fleet.
--
-- 用一句话讲完: 一个 SECURITY DEFINER 函数,把「事件数 < 阈值 且 full 从未扫过」的公司的 full 队列行 priority 压到
-- 5(压过 incremental 的 10)、due_at 设为 now(),于是 worker 下一轮 claim 就先抢它们;只改 status='queued' 的行,
-- 所以正在跑的单元一个都不碰 —— 这就是"不打断线上"的保证来源。
--
-- WHY it can write at all without a new database credential: SECURITY DEFINER runs the body as the function OWNER
-- (postgres), so an `anon` caller reaching it through PostgREST gets exactly this one bounded operation and nothing
-- else. Verified, not assumed: calling a probe as anon returned session_user=authenticator, current_user=postgres,
-- has_table_privilege(work_queue,UPDATE)=true, while a direct PATCH on work_queue as anon still returns 42501.
-- {USER 2026-07-28 "方案 1（PostgREST RPC，无新密码"} {MEASURED 2026-07-28 probe via /rest/v1/rpc}
-- [CONFIDENCE: CONFIRMED 100% — both the elevation and the direct-write denial were probed against the live API].
--
-- WHY that makes a shared secret MANDATORY: the anon key ships inside the browser bundle, so "granted to anon" means
-- "callable by anyone on the internet". Steering a crawler is not something the public may do, so the body refuses to
-- act unless the caller passes a token that matches a row in a table anon cannot read. Everything below the token
-- check is ALSO bounded, so even a leaked token cannot do arbitrary damage:
--   • only type='full' AND status='queued'  → an in-flight unit is never touched (the "don't break current runs" rule)
--   • row set is chosen SERVER-SIDE by the low-event predicate — the caller cannot name arbitrary companies
--   • p_limit is clamped, so incremental cannot be starved by boosting the whole 2302-row backlog at once
--   • priority is clamped to a sane band; 'reset' restores the default
--
-- Upstream trigger: api_service POST /queue/boost. Downstream: work_queue.priority/due_at → queue.claim_work's
-- `ORDER BY priority ASC, due_at ASC` picks the boosted rows first on the next claim.

-- The secret store. NOT granted to anon/authenticated — PostgREST cannot read or even see it; only the SECURITY
-- DEFINER body (running as postgres) can. Seed it with:  INSERT INTO waterevents.api_tokens VALUES ('queue_boost','<secret>');
CREATE TABLE IF NOT EXISTS waterevents.api_tokens (
    name       text PRIMARY KEY,
    token      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON waterevents.api_tokens FROM PUBLIC, anon, authenticated;

-- Audit trail: every accepted call is recorded, so "who reordered the queue and when" is answerable after the fact.
CREATE TABLE IF NOT EXISTS waterevents.queue_boost_log (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    action     text NOT NULL,
    max_events int,
    n_rows     int NOT NULL,
    note       text
);
REVOKE ALL ON waterevents.queue_boost_log FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION waterevents.queue_boost(
    p_token      text,
    p_action     text DEFAULT 'boost',      -- 'boost' | 'reset' | 'status'
    p_max_events int  DEFAULT 10,           -- target = companies with fewer than this many events
    p_limit      int  DEFAULT 25            -- how many full units to move (clamped to 200)
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = waterevents, pg_catalog
AS $$
DECLARE
    v_ok    boolean;
    v_lim   int := LEAST(GREATEST(COALESCE(p_limit, 25), 1), 200);   -- never let one call move the whole backlog
    v_maxev int := LEAST(GREATEST(COALESCE(p_max_events, 10), 0), 1000);
    v_rows  int := 0;
BEGIN
    SELECT EXISTS (SELECT 1 FROM waterevents.api_tokens
                   WHERE name = 'queue_boost' AND token = p_token) INTO v_ok;
    IF NOT v_ok THEN
        -- Deliberately vague: a public caller learns only that it was refused, never whether the token store exists.
        RAISE EXCEPTION 'unauthorized' USING ERRCODE = '28000';
    END IF;

    IF p_action = 'status' THEN
        RETURN (SELECT jsonb_build_object(
                    'boosted_now',  count(*) FILTER (WHERE priority = 5),
                    'full_queued',  count(*) FILTER (WHERE status = 'queued'),
                    'full_running', count(*) FILTER (WHERE status = 'running'),
                    'full_unscanned', count(*) FILTER (WHERE last_scanned_at IS NULL))
                FROM waterevents.work_queue WHERE type = 'full');

    ELSIF p_action = 'reset' THEN
        -- Undo: back to the default full priority. Only rows carrying OUR marker, and only while still queued.
        -- CONSEQUENCE worth knowing: a boosted unit a worker has already claimed is status='running', so reset
        -- reports rows_changed=0 for it and it still gets scanned. That is deliberate — reset withdraws a QUEUED
        -- intention, it does not abort work in progress. {MEASURED 2026-07-28 'reset right after a boost returned
        -- rows_changed=0 / boosted_total=16 because all 16 had been claimed within seconds'}
        -- [CONFIDENCE: CONFIRMED 100% — observed on the live queue].
        UPDATE waterevents.work_queue
           SET priority = 100, updated_at = now()
         WHERE type = 'full' AND status = 'queued' AND priority = 5;   -- ONLY our own marker, see _BOOST_PRIORITY note
        GET DIAGNOSTICS v_rows = ROW_COUNT;

    ELSIF p_action = 'boost' THEN
        -- priority 5 beats BOTH incremental (10) and normal full (100). Anything >= 10 would still lose to every due
        -- incremental, and the incremental pool is never empty, so a half-measure here is indistinguishable from
        -- doing nothing. due_at=now() makes the row immediately claimable.
        --
        -- WHY 5 and not 0, which would be the obvious choice: 5 is a MARKER that identifies rows this function set, so
        -- 'reset' can restore exactly those and nothing else. A row already sitting at priority 0 was found on the live
        -- queue during this migration's own testing — enqueue() only ever writes 100 (full) or 10 (incremental) and the
        -- audit log was empty, so it had been set by hand outside this code path. Resetting on `priority < 10` would
        -- have silently reverted that someone-else's change too.
        -- {DB 2026-07-28 "1 full row at priority=0, queue_boost_log empty, QUEUE.PY enqueue writes only 100/10"}
        -- [CONFIDENCE: CONFIRMED 100% — the stray row and the empty audit log were both read off the live database].
        -- {QUEUE.PY claim_work "ORDER BY priority ASC, due_at ASC"} {DB 2026-07-28 "incremental priority 10 / full 100"}
        WITH ev AS (SELECT company_id, count(*) n FROM waterevents.events GROUP BY 1),
        target AS (
            SELECT w.id FROM waterevents.work_queue w
            LEFT JOIN ev ON ev.company_id = w.company_id
            WHERE w.type = 'full'
              AND w.status = 'queued'                 -- in-flight rows are untouchable by construction
              AND w.last_scanned_at IS NULL           -- never scanned = the real coverage gap
              AND COALESCE(ev.n, 0) < v_maxev
              AND w.priority >= 10                    -- skip rows already boosted, so repeated calls
                                                      -- ADVANCE to the next batch instead of rewriting the same rows
            ORDER BY COALESCE(ev.n, 0) ASC, w.created_at ASC   -- worst-covered companies first
            LIMIT v_lim)
        UPDATE waterevents.work_queue w
           SET priority = 5, due_at = now(), updated_at = now()
          FROM target t WHERE w.id = t.id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
    ELSE
        RAISE EXCEPTION 'unknown action %', p_action USING ERRCODE = '22023';
    END IF;

    INSERT INTO waterevents.queue_boost_log(action, max_events, n_rows, note)
    VALUES (p_action, v_maxev, v_rows, format('limit=%s', v_lim));

    RETURN jsonb_build_object('action', p_action, 'rows_changed', v_rows,
                              'max_events', v_maxev, 'limit', v_lim,
                              'boosted_total', (SELECT count(*) FROM waterevents.work_queue
                                                WHERE type = 'full' AND priority = 5));
END;
$$;

-- EXECUTE only. Without a valid token the body raises before touching anything, so exposing it to anon grants the
-- public nothing but a refusal.
GRANT EXECUTE ON FUNCTION waterevents.queue_boost(text, text, int, int) TO anon, authenticated, service_role;

COMMENT ON FUNCTION waterevents.queue_boost(text, text, int, int) IS
  'Token-gated: move never-scanned full units for low-event companies to the front of the claim order. Touches only status=queued rows, so a running scan is never disturbed.';
