"""watercrawl.capacity — the hard ceiling that stops the render lane from taking the whole machine down.

用一句话讲完: 开新页之前问一句"这台机器现在还扛得住吗" —— 读 MemAvailable、chrome 总 RSS、iowait 三个信号,
任何一条越线就让调用方等,而不是继续开页。三个信号都是全机器的,所以 6 个 worker 进程读同一份 /proc 会得到
同一个答案 —— 不需要任何共享文件,顶天然就是全 fleet 的。

WHY A CEILING AND NOT A SMALLER STATIC LIMIT. The static limits are what we have today and they are set by hand from
the last thing that broke: IR_WATERCRAWL_BROWSERS=2 and IR_WATERCRAWL_MAX_PAGES=8 were both cut after the 2026-07-27
livelock. That is a number chosen for the worst moment and paid for at every other moment — measured right now, the
box is 83% idle with 24.5GB of 32GB available while chrome holds 8.5GB. A ceiling lets the fleet use what is actually
free and still refuses the request that would take it over.

WHAT THIS BOX ACTUALLY FAILS FROM — and why "free memory" alone would not have caught it. On 2026-07-27 22:11 six
workers × 3 browsers × 24 pages tipped the VM into a page-cache thrash livelock: disk reads pinned at the 3600 IOPS
instance ceiling for 4.5 hours with writes starved to ~0, journald and DHCP renewal blocked, the box alive at ~17% CPU
of pure iowait and able to do nothing. The OOM killer never fired, because with no swap the kernel could always
"successfully" reclaim more page cache. So a memory-only gate would have watched that happen and seen nothing wrong.
That is why iowait is a first-class signal here and not a nice-to-have.
{LAUNCH_FLEET.SH:70-82 "TIPPED THE VM INTO A PAGE-CACHE THRASH LIVELOCK: DISK READS PINNED AT THE 3600 IOPS ... THE
 OOM KILLER NEVER FIRED BECAUSE WITH NO SWAP THE KERNEL COULD ALWAYS 'SUCCESSFULLY' RECLAIM MORE PAGE CACHE"}
{MEASURED 2026-08-01 ir-media-8 "cores=8 mem_total=32093MB mem_avail=24548MB swap=8191MB / chromium 进程: 140 /
 chrome 总 RSS: 8.5 GB / load1=1.69 wa=3 id=83"}
[CONFIDENCE: CONFIRMED 100% — the incident is recorded in the launcher's own comments; the headroom was read off the
 host today.]

WHY NOT load average — the mistake this module exists to not repeat. load1 counts D-state tasks, and D state is what a
chromium waiting on a remote IR site sits in. A sibling service measured load1=7.59 (95% of 8 cores) while real CPU was
58%, and its gate shed 62% of traffic at that reading. This box currently runs 140 chromium processes, nearly all of
them waiting on someone else's server, so the same distortion applies and is larger. Every CPU-ish signal here is a
/proc/stat delta; load average appears nowhere.
[CONFIDENCE: CONFIRMED 100% — the divergence was measured on a comparable service under real network load.]

Upstream trigger: render.py, immediately before it acquires the page/shot semaphores. Downstream: a caller that waits
instead of opening a page, and a counter that says so out loud.
"""
from __future__ import annotations

import os
import threading
import time

# Thresholds. Deliberately generous against today's 8.5GB / 24.5GB-free reading — a ceiling that fires in normal
# operation is a static limit wearing a costume, and the point is to only refuse the request that would tip the box.
_CHROME_RSS_MAX_GB = float(os.environ.get("WATERCRAWL_CHROME_RSS_MAX_GB", "18"))
_MEM_AVAIL_MIN_GB = float(os.environ.get("WATERCRAWL_MEM_AVAIL_MIN_GB", "6"))
# 40%: the livelock ran at ~17% CPU that was *entirely* iowait for four and a half hours, so the number that matters is
# not "is iowait high" but "is the machine spending its time waiting on disk instead of working". Normal reading today
# is 3%. [CONFIDENCE: INFERRED 70% — 40% is a judgement between today's 3% and the incident's sustained pathology; it
# has not yet been observed firing, which is the point of shipping the ceiling before the growth controller.]
_IOWAIT_MAX_PCT = float(os.environ.get("WATERCRAWL_IOWAIT_MAX_PCT", "40"))
_ENABLED = os.environ.get("WATERCRAWL_CAPACITY_GATE", "1") not in ("0", "false", "no")
# Sampling cost is real: totalling chrome RSS means reading ~140 /proc entries. At 48 concurrent page-opens that is
# 6,700 file reads a second if uncached, so one sample is shared for this long.
_SAMPLE_TTL_S = float(os.environ.get("WATERCRAWL_CAPACITY_TTL_S", "2.0"))

_lock = threading.Lock()
_last_sample: dict = {}                                   # the cached reading, refreshed at most every _SAMPLE_TTL_S
_last_cpu: tuple[float, float] | None = None              # (total_jiffies, iowait_jiffies) for the iowait delta
blocked_count = 0                                         # how many times the gate made a caller wait, this process


def _mem_available_gb() -> float:
    """MemAvailable, not MemFree: free memory excludes the page cache the kernel would hand back on demand, so gating
    on it would refuse work while 20GB of reclaimable cache sits there."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1048576.0     # kB → GiB
    except OSError:
        pass
    return float("inf")                                   # unreadable → do not block work on a broken probe


def _chrome_rss_gb() -> float:
    """Total RSS of every chromium/camoufox process on the box, summed across ALL worker processes.

    Walking /proc rather than asking Playwright, because the number that matters is what the OS sees: one 'browser'
    is a tree of renderer, GPU, zygote and utility processes, and today 12 launched browsers are 140 OS processes
    holding 8.5GB. Anything counting browsers or tabs undercounts by an order of magnitude — config.py's own estimate
    of ~60MB per page is 3x low against the 180MB/slot measured on this host.
    {CONFIG.PY:12 "PEAK MEM ≈ MAX_PAGES×~60MB"} {MEASURED 2026-08-01 "8.5 GB / 48 SLOTS = 180MB/SLOT, 140 PROCESSES"}
    [CONFIDENCE: CONFIRMED 100% — process count and RSS total read off the host in one command.]
    """
    total_kb = 0
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/comm") as fh:
                    comm = fh.read().strip()
                if "chrome" not in comm and "camoufox" not in comm and "firefox" not in comm:
                    continue
                with open(f"/proc/{pid}/statm") as fh:
                    # statm field 2 is resident set in pages; page size is 4096 on every platform this runs on.
                    total_kb += int(fh.read().split()[1]) * 4
            except (OSError, ValueError, IndexError):
                continue                                  # process exited mid-walk — normal, skip it
    except OSError:
        return 0.0
    return total_kb / 1048576.0


def _iowait_pct() -> float:
    """Percentage of jiffies spent in iowait SINCE THE LAST CALL — a delta, never a cumulative total.

    /proc/stat's counters are monotonic since boot, so reading them once tells you the machine's lifetime average and
    nothing about now. The first call has no previous sample and returns 0.0, which fails open."""
    global _last_cpu
    try:
        with open("/proc/stat") as fh:
            parts = fh.readline().split()
        vals = [float(x) for x in parts[1:]]
        total, iowait = sum(vals), vals[4]
    except (OSError, ValueError, IndexError):
        return 0.0
    prev, _last_cpu = _last_cpu, (total, iowait)
    if prev is None:
        return 0.0
    dt, dw = total - prev[0], iowait - prev[1]
    return 0.0 if dt <= 0 else max(0.0, 100.0 * dw / dt)


def sample(force: bool = False) -> dict:
    """The three signals, cached for _SAMPLE_TTL_S. One reading is shared by every caller in this process."""
    global _last_sample
    with _lock:
        now = time.monotonic()
        if not force and _last_sample and now - _last_sample.get("_t", 0.0) < _SAMPLE_TTL_S:
            return _last_sample
        _last_sample = {"_t": now, "mem_avail_gb": _mem_available_gb(),
                        "chrome_rss_gb": _chrome_rss_gb(), "iowait_pct": _iowait_pct()}
        return _last_sample


def check() -> tuple[bool, str]:
    """(ok, reason) — may this machine open another render page right now?

    Reason is empty when ok. It names the tripped signal and its value when not, because a gate that refuses without
    saying which line it hit is the same class of thing as a janitor that reports success while deleting nothing."""
    if not _ENABLED:
        return True, ""
    s = sample()
    if s["chrome_rss_gb"] > _CHROME_RSS_MAX_GB:
        return False, f"chrome RSS {s['chrome_rss_gb']:.1f}GB > {_CHROME_RSS_MAX_GB:.0f}GB"
    if s["mem_avail_gb"] < _MEM_AVAIL_MIN_GB:
        return False, f"MemAvailable {s['mem_avail_gb']:.1f}GB < {_MEM_AVAIL_MIN_GB:.0f}GB"
    if s["iowait_pct"] > _IOWAIT_MAX_PCT:
        return False, f"iowait {s['iowait_pct']:.0f}% > {_IOWAIT_MAX_PCT:.0f}%"
    return True, ""


def wait_for_capacity(budget_s: float = 30.0, poll_s: float = 1.0) -> tuple[bool, str]:
    """Block until the machine can take another page, or until budget_s elapses.

    WAITING, NOT FAILING, is the contract. A unit that waits keeps its place and renders a second later; a unit that
    fails burns an attempt and, at four attempts, parks in status='failed' — which this session watched happen to 34
    live, event-producing companies for reasons that had nothing to do with them. Returning False after the budget is
    the honest outcome and the caller counts it as a render failure, which scan_log.failed_render now records.
    [CONFIDENCE: CONFIRMED 100% — the 'failed' population and its cause were read from production on 2026-08-01.]
    """
    global blocked_count
    ok, why = check()
    if ok:
        return True, ""
    blocked_count += 1
    print(f"[capacity] ⏸ holding off a render — {why} (waiting up to {budget_s:.0f}s)", flush=True)
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        ok, why = check()
        if ok:
            return True, ""
    return False, why


async def wait_for_capacity_async(budget_s: float = 30.0, poll_s: float = 1.0) -> tuple[bool, str]:
    """The coroutine form, for callers running ON the Playwright loop thread.

    THE SYNC VERSION MUST NOT BE CALLED FROM THE LOOP. time.sleep() there stops every in-flight render, not just this
    one, and sample() walks ~140 /proc entries, which is itself too much to do inline on the loop. Both are offloaded:
    asyncio.sleep for the wait, asyncio.to_thread for the reading. This is the same mistake politeness.py made earlier
    today — a synchronous gate on the loop thread froze it for 3,042ms, and moving the work off-loop took that to
    10.6ms. Writing the async twin at the same time as the sync one is the cheap way to not repeat it.
    {MEASURED 2026-08-01 politeness gate "loop freeze 3042ms → 10.6ms" after offloading via asyncio.to_thread}
    [CONFIDENCE: CONFIRMED 100% — measured on this codebase, this session.]
    """
    global blocked_count
    import asyncio
    ok, why = await asyncio.to_thread(check)
    if ok:
        return True, ""
    blocked_count += 1
    print(f"[capacity] ⏸ holding off a render — {why} (waiting up to {budget_s:.0f}s)", flush=True)
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_s)
        ok, why = await asyncio.to_thread(check)
        if ok:
            return True, ""
    return False, why
