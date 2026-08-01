-- scan_log.failed_render — persist the render-lane failure count, the one signal that was inferable but never stored.
--
-- 用一句话讲完: engine 已经在数 failed_render, worker 已经拿它决定一个 unit 是 fail 还是 complete, 但 scan_log 里没有
-- 这一列, 所以事后查库时"渲染失败"只能从 vlm_calls=0 且 extract_errors=0 且 events=0 反推 —— 加上它, 这件事变成可查的。
--
-- WHAT MADE THIS NECESSARY. 40 work_queue rows sit permanently in status='failed' at attempt=4, and every one of them
-- has produced real events before: SAP (17), Nutrien (18), Centrica (16), Bureau Veritas (20), POSCO (10),
-- Raymond James (9), Argan (36). Diagnosing why took reading their scan_log rows and recognising a shape:
--
--     render_pages=1  vlm_calls=0  vlm_skipped=0  events=0  extract_errors=0
--
-- One page visited, the VLM never called, and not a hash-gate skip either. That is a render that failed — the page was
-- never obtained, so there was no text to send. But nothing in the row SAYS so; it has to be deduced from three zeros.
-- The engine knows the number, worker.py's fail/complete predicate consumes it (`lost = extract_errors + failed_render`),
-- and it is dropped on the way to the table.
--
-- This is the same argument the extract_errors column was added on, in the same table, six days earlier: without it
-- every consumer sees one row for "found nothing" and for "could not look", and each invents its own guess. That
-- indistinguishability was the measured root cause of three separate health checks reporting healthy through an
-- 11h52m outage. The render lane deserves the column the extraction lane already got.
--
-- {DB 2026-08-01 "SELECT ... FROM work_queue WHERE status='failed'" -> 40 rows, ALL attempt=4, ALL with prior events}
-- {DB 2026-08-01 scan_log for those urls -> "1 | 0 | 0 | 0 | 0" (pages|vlm|skip|events|err) on every row}
-- {EVENTS.PY:156 "EXTRACT_ERRORS IS WHAT LETS A QUERY TELL 'THIS SCAN FOUND NOTHING' APART FROM 'THIS SCAN COULD NOT
--  LOOK'." — the identical argument, already accepted for the other lane}
-- [CONFIDENCE: CONFIRMED 100% — the fingerprint was read off the production database for all 40 parked units.]
--
-- Upstream: storage/events.py log_scan() writes it from the stats dict scan.py already carries.
-- Downstream: makes "the render lane is failing" answerable in SQL; fleet_health() may later use it as a second
-- predicate alongside the VLM one, which today is the only thing it can see.

ALTER TABLE waterevents.scan_log
  ADD COLUMN IF NOT EXISTS failed_render integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN waterevents.scan_log.failed_render IS
  'Pages whose render returned nothing on this scan. Distinguishes "the page had no events" from "the page was never '
  'obtained": with vlm_calls=0 and extract_errors=0 and events=0, a nonzero value here is the render lane failing, '
  'which is otherwise only inferable from three zeros. Consumed by worker.py''s fail/complete predicate.';

-- Partial index on the failure shape only. The table is append-only and grows by ~1,400 rows/hour, but the rows worth
-- querying are the rare ones; indexing only failed_render > 0 keeps the index a small fraction of the table while
-- still serving "which hosts are failing to render, and since when".
CREATE INDEX IF NOT EXISTS scan_log_failed_render_idx
  ON waterevents.scan_log (ts DESC, url)
  WHERE failed_render > 0;
