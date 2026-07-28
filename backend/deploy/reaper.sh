#!/usr/bin/env bash
# reaper — return abandoned work_queue rows to the pool, on a timer, instead of waiting for someone to notice.
#
# 用一句话讲完: 每 2 分钟把「status='running' 但 lease 已经过期超过宽限期」的行退回 'queued' —— 这些是 worker 被
# SIGKILL / OOM / 运维重启掐掉时留下的,进程已经不在了但行还占着 running。claim_work 本身有惰性回收(它会捡 lease
# 过期的 running 行),但那要等优先级排到它才捡,期间这些行会污染每一个基于 running 计数的判断。
#
# WHY this is a timer and not "claim_work already handles it": lazy reclaim fixes the row only when a worker happens to
# reach it. Until then the row reads as in-flight. On 2026-07-28 that produced "82 full running" on a fleet with six
# workers, and that number was used to reason about GPU load — the reasoning was wrong because the number was wrong.
# An orphan that lingers is not just idle capacity, it is a lie in the monitoring surface.
# {MEASURED 2026-07-28 "work_queue running=99 with only 6 workers; 53 of them orphaned by fleet restarts"}
# [CONFIDENCE: CONFIRMED 100% — counted on the live table; every orphan's lease_owner named a PID that no longer existed].
#
# SAFETY, in the order it matters:
#   1. GRACE PERIOD — only rows whose lease died more than GRACE ago are touched, so a worker mid-renewal is never
#      raced. Verified before first run: of the 75 rows that qualified, ZERO had been updated in the previous 2 minutes.
#   2. LIVE LEASES ARE UNTOUCHED — the WHERE clause cannot match a row whose lease_until is still in the future.
#      Verified: 18 healthy running rows existed and none of them qualified.
#   3. IDEMPOTENT — re-running changes nothing, because the rows it flips no longer match the predicate.
#   4. DRY RUN IS THE DEFAULT — it prints the counts and exits unless --apply is passed, matching the house style of
#      backend/scripts/ops/queue_boost.py, so the first thing anyone runs is harmless.
#
# NOT DONE HERE, deliberately: companies.status is left alone. It looks like 1,667 rows stuck in 'discovering' for three
# days, but nothing reads that column and nothing has updated it since the company-level lease machinery was deleted on
# 2026-07-27 — the only writer left is scan.py's INSERT. "Converging" it would rewrite 1,667 rows every 2 minutes, and
# it would never converge: INSERT writes 'discovering', nothing advances it, a reaper flips it to 'queued', nothing
# advances that either. It is a dead column, and the fix for a dead column is to drop it, not to groom it.
# {MIGRATION 20260727095145 "虽然没有任何地方再读它们"} [CONFIDENCE: CONFIRMED 100% — no UPDATE of companies.status
#  anywhere in backend/, and none of the four frontend endpoints selects it].
#
# Upstream trigger: waterevents-reaper.timer (every 2 min). Downstream: rows become claimable again on the next claim_work.
set -uo pipefail

GRACE="${WATEREVENTS_REAP_GRACE:-2 minutes}"     # how long a lease must be dead before we reclaim it
DSN="${WATEREVENTS_DB_DSN:-}"
PSQL="${PSQL_BIN:-psql}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

if [ -z "$DSN" ]; then
  echo "reaper: WATEREVENTS_DB_DSN unset — refusing to run" >&2
  exit 78                                        # EX_CONFIG — fail loud, never silently pass
fi

WHERE="status='running' AND lease_until < now() - interval '$GRACE'"

n=$("$PSQL" "$DSN" -t -A -c "SELECT count(*) FROM waterevents.work_queue WHERE $WHERE" 2>/dev/null)
if [ -z "$n" ]; then
  echo "reaper: database unreachable — taking no action" >&2
  exit 0                                         # our own blindness is not grounds to mutate anything
fi

if [ "$APPLY" != "1" ]; then
  echo "reaper: DRY RUN — $n work_queue row(s) would be returned to 'queued' (grace: $GRACE). Pass --apply to do it."
  exit 0
fi

# STAGE 1 — work_queue. Guarded rather than early-returned: an earlier version exited here when the count was zero,
# which meant the stage-2 reclaim below only ever ran on the rounds that happened to also have work_queue orphans.
# Two independent cleanups must not be able to gate each other.
# attempt is NOT reset here. A row abandoned by a crash has genuinely been attempted, and complete_work already zeroes
# the counter on the next success, so the retry budget stays meaningful without this needing an opinion about it.
out=0
# COUNT IN SQL, never with `wc -l`. psql -t -A prints a trailing empty line even when the UPDATE matched nothing, so
# piping RETURNING through wc -l reports 1 for a no-op — an off-by-one that inflates every report by exactly one and is
# invisible precisely when nothing happened. A CTE + count(*) makes the database do the counting.
# {MEASURED 2026-07-28 "a 0-match UPDATE ... RETURNING 1 | wc -l -> 1, while SELECT 1 WHERE false | wc -l -> 0"}
# [CONFIDENCE: CONFIRMED 100% — reproduced against the live database; the first run of this script reported 77
#  reclaimed when the dry run had counted 76 candidates, which is exactly this artifact].
[ "$n" != "0" ] && out=$("$PSQL" "$DSN" -t -A -c \
  "WITH u AS (UPDATE waterevents.work_queue
                 SET status='queued', lease_owner=NULL, lease_until=NULL, updated_at=now()
               WHERE $WHERE RETURNING 1)
   SELECT count(*) FROM u" 2>/dev/null | tr -d ' ')

[ "$out" != "0" ] && echo "reaper: reclaimed $out abandoned unit(s) (lease dead > $GRACE)" || true

# STAGE-2 LEASES TOO. events.reconcile_events() exists in the codebase and does exactly this, but nothing calls it —
# `grep -rn reconcile_events` returns the definition and one comment claiming a reconcile runs, which it does not. The
# enrichment worker claims an event by flipping it to 'rendering' with a lease; if that worker dies the row stays
# 'rendering' forever and no later claim can pick it up, because the claim predicate only matches 'discovered'.
# It is the same orphan shape as the work_queue one, on the other half of the pipeline, so it belongs on the same timer
# rather than in a second mechanism with its own cadence.
# {EVENTS.PY:232 "RECLAIM EVENTS STUCK IN `RENDERING` PAST THEIR LEASE (CRASHED ENRICHMENT WORKER) → BACK TO `DISCOVERED`"}
# [CONFIDENCE: CONFIRMED 100% — zero call sites at the time of writing; currently 0 rows are stuck, so this lands
#  before it is needed rather than after].
ev=$("$PSQL" "$DSN" -t -A -c \
  "WITH u AS (UPDATE waterevents.events SET status='discovered', claim_token=NULL
               WHERE status='rendering' AND lease_until < now() RETURNING 1)
   SELECT count(*) FROM u" 2>/dev/null | tr -d ' ')
[ "${ev:-0}" != "0" ] && echo "reaper: reclaimed $ev stalled event lease(s)" || true
[ "$out" = "0" ] && [ "${ev:-0}" = "0" ] && echo "reaper: nothing to reclaim" || true
