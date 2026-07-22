"""event_agent local verification — runs the FULL crawl_company close-loop with MOCKED watercrawl + LLM, so we prove
the orchestration + tracing are correct WITHOUT a running Qwen server or browser. Run: python3 -m agent.event_agent.verify_local

Fake site:  /news (hub) --go_deeper--> /news/q3-detail   plus /about (go_deeper=false) + twitter.com (off-site)
Proves: events collected + deduped · multi-url event kept · go_deeper followed · nav NOT followed · off-site blocked
        · every page traced to disk (content/screenshot/html/links/result/meta with the winning fetch method).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile

from providers import watercrawl
from agent.event_agent import crawl as crawl_mod

_SHOT = base64.b64encode(b"FAKE_JPEG_BYTES_FOR_TRACE_TEST").decode()   # valid b64 → trace writes screenshot.jpg


def _fake_render_shot(url: str, wait_ms: int = 3000) -> dict:
    if url.rstrip("/").endswith("/news"):
        return {"text": "Newsroom | About\nQ3 2025 Results (Oct 28 2025) [Press Release] [PDF]",
                "links": ["https://investors.acme.com/news/q3-detail", "https://investors.acme.com/about",
                          "https://twitter.com/acme"],
                "html": "<html>hub</html>", "shot_b64": _SHOT, "method": "render"}
    if "q3-detail" in url:
        return {"text": "Q4 2025 Guidance call ...", "links": [], "html": "<html>detail</html>",
                "shot_b64": _SHOT, "method": "residential"}
    return {"text": "", "links": [], "html": "", "shot_b64": "", "method": ""}


async def _fake_extract_pages(pages, client=None, use_image=False):
    out = []
    for p in pages:
        u = p["page_url"]
        if u.rstrip("/").endswith("/news"):
            out.append({"events": [{"title": "Q3 2025 Results", "date": "2025-10-28", "type": "earnings",
                                    "urls": ["https://investors.acme.com/news/q3",
                                             "https://investors.acme.com/files/q3-slides.pdf"]}],
                        "routes": [{"url": "https://investors.acme.com/news/q3-detail", "go_deeper": True},
                                   {"url": "https://investors.acme.com/about", "go_deeper": False},
                                   {"url": "https://twitter.com/acme", "go_deeper": True}]})   # off-site → must be blocked
        elif "q3-detail" in u:
            out.append({"events": [{"title": "Q4 2025 Guidance", "date": "", "type": "",
                                    "urls": ["https://investors.acme.com/news/q4-guidance"]}], "routes": []})
        else:
            out.append({"events": [], "routes": []})
    return out


async def main() -> None:
    watercrawl.render_shot = _fake_render_shot          # mock the browser
    crawl_mod.extract_pages = _fake_extract_pages       # mock the LLM
    crawl_mod._USE_IMAGE = True
    tmp = tempfile.mkdtemp(prefix="wm_verify_")

    class _Dummy:                                       # stand-in so crawl_company doesn't build a real QwenClient
        pass

    out = await crawl_mod.crawl_company("https://investors.acme.com/news", client=_Dummy(), trace_dir=tmp)
    events, pages, run_dir = out["events"], out["pages"], out["trace_dir"]

    titles = {e["title"] for e in events}
    q3 = next((e for e in events if e["title"] == "Q3 2025 Results"), None)
    page_dirs = sorted(os.listdir(os.path.join(run_dir, "pages")))
    methods = []
    go_deeper_logged = False
    shots = 0
    for pd in page_dirs:
        d = os.path.join(run_dir, "pages", pd)
        meta = json.load(open(os.path.join(d, "meta.json")))
        methods.append(meta["method"])
        if meta["go_deeper"]:
            go_deeper_logged = True
        if os.path.exists(os.path.join(d, "screenshot.jpg")):
            shots += 1
        for must in ("content.txt", "page.html", "links.txt", "result.json", "meta.json"):
            assert os.path.exists(os.path.join(d, must)), f"missing {must} in {pd}"

    checks = {
        "two_events": len(events) == 2,
        "both_titles": titles == {"Q3 2025 Results", "Q4 2025 Guidance"},
        "multi_url": q3 is not None and len(q3["urls"]) == 2 and any(".pdf" in u for u in q3["urls"]),
        "followed_deeper": pages == 2,                  # /news + /news/q3-detail (about + twitter NOT visited)
        "offsite_blocked": not any("twitter" in u for e in events for u in e["urls"]),   # never crawled twitter
        "two_pages_traced": len(page_dirs) == 2,
        "methods_recorded": set(methods) == {"render", "residential"},
        "go_deeper_recorded": go_deeper_logged,
        "screenshots_saved": shots == 2,
        "summary_exists": os.path.exists(os.path.join(run_dir, "summary.json")),
    }
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print(f"\n[verify] trace at {run_dir}")
    print("[verify]", "ALL PASS ✅" if all(checks.values()) else "SOME FAILED ❌")


if __name__ == "__main__":
    asyncio.run(main())
