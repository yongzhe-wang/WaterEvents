"""chunk_partial_vs_error — pin the rule that a page whose chunks PARTLY failed keeps the events that did come back.

用一句话讲完: 造一个「3 个 block 里 2 个 ReadTimeout、1 个正常返回事件」的抽取结果 → 断言它被标成 `_partial`
(engine 会保留事件、同时仍计 failed_extract) 而不是 `_error`(engine 会整页丢弃);再造一个「全部 block 都失败」
的结果 → 断言它仍然是 `_error`。全程不碰 GPU / 网络: QwenClient.send_many 被替换成返回预设 block 结果。

WHY this test exists: production lost 17 real dated events off Sony's earnings archive because two of its three
chunks timed out against a saturated vLLM and the transport error was reported as `_error`.
{TRACE 2026-07-28 .../presen/er/archive.html result.json "_error": "ReadTimeout: ; ReadTimeout: ", "events": 17 items
 — while the same run's summary.json says "n_events": 0}
[CONFIDENCE: CONFIRMED 100% — read off the production trace].

Run (no GPU, no DB):  PYTHONPATH=backend python3 -m tests.event.extract.chunk_partial_vs_error
"""
from __future__ import annotations

import asyncio
import sys

from agent.event_agent.crawl import extract as EX
from agent.event_agent.crawl import prompts

# A block of page text in the shape the crawl actually produces: reading-order text with inline [anchor](url) links.
_BLOCK = ("Earnings Announcements\n"
          "[FY2025 Q4 results webcast](https://investors.example.com/vod/20260508/q4.html) May 8, 2026\n"
          "[FY2025 Q3 results webcast](https://investors.example.com/vod/20260205/q3.html) February 5, 2026\n")


def _model_reply_with_events():
    """What a HEALTHY block returns: events whose urls are Lnn ids from THIS block's own map, and whose evidence is
    literally present in the block (both are required by _normalize_events, so the ids are derived, never guessed)."""
    _tagged, tag_map = prompts.tag_links(_BLOCK)
    ids = list(tag_map.keys())
    return {"events": [
        {"title": "FY2025 Q4 results webcast", "date": "2026-05-08", "type": "earnings",
         "urls": [ids[0]], "evidence": "May 8, 2026"},
        {"title": "FY2025 Q3 results webcast", "date": "2026-02-05", "type": "earnings",
         "urls": [ids[1]], "evidence": "February 5, 2026"},
    ]}


class _FakeClient:
    """Stands in for QwenClient. Returns one canned reply per job, in job order (send_many's documented contract)."""
    def __init__(self, replies): self._replies = replies
    async def send_many(self, jobs): return [self._replies[i % len(self._replies)] for i in range(len(jobs))]


async def case(name, replies, *, expect_events, expect_flag):
    # _extract_events_chunked splits by _CHUNK_TARGET_CHARS; feed it enough text to force >=2 blocks either way.
    text = _BLOCK * max(2, (EX._CHUNK_TARGET_CHARS // max(len(_BLOCK), 1)) + 1)
    out = await EX._extract_events_chunked(text, "https://investors.example.com/archive", _FakeClient(replies), False)
    n = len(out.get("events") or [])
    flag = "_error" if out.get("_error") else ("_partial" if out.get("_partial") else "none")
    ok = (n > 0) == expect_events and flag == expect_flag
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: events={n} flag={flag} "
          f"(expected events{'>0' if expect_events else '==0'}, flag={expect_flag})")
    return ok


async def main():
    timeout_block = {"events": [], "_error": "ReadTimeout: "}
    good_block = _model_reply_with_events()
    results = [
        # the Sony case: transport failure on some blocks, real events from another → keep them, flag partial
        await case("some blocks time out, one delivers", [timeout_block, good_block],
                   expect_events=True, expect_flag="_partial"),
        # nothing came back from anywhere → still a hard error, still fail-loud
        await case("every block fails", [timeout_block], expect_events=False, expect_flag="_error"),
        # clean run → no flag at all
        await case("all blocks succeed", [good_block], expect_events=True, expect_flag="none"),
    ]
    print(f"\n[chunk_partial_vs_error] {'ALL PASS ✅' if all(results) else 'FAIL ❌'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
