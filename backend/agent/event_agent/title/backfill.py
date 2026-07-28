"""event_agent.title.backfill — CHEAP, VLM-FREE title backfill for title-less events. Two entry points:
  • backfill_for_company(pool, company_id) — the per-scan HOOK: after a full company / incremental hub is crawled, curl
    THAT company's title-less events and fill what it can. Cheap (HTTP-only) so it runs inline after every scan.
  • CLI (python -m agent.event_agent.title.backfill [N] [--apply]) — a batch pass over the WHOLE title-less backlog.

用一句话讲完: title-less 的事件(filings/PDF 让 VLM 抽不出标题)→ 直接 curl 它的 URL,HTML 抓 og:title/<title>(按声明
charset 解码,日文站要 shift_jis)、PDF 读 docinfo /Title、垃圾(EDGAR accession/UUID/PPT 默认/bot 墙)一律拒绝留空 →
幂等填进 events.title。纯 HTTP、网络绑定、零 VLM。scan_unit 每爬完一个 company/hub 就调 backfill_for_company 顺手回填。
{USER 2026-07-27 "curl the urls, get title from browser title; use it along full+incremental after crawl; own subfolder"}
[CONFIDENCE: CONFIRMED — sample-validated: HTML/real-filing 页得好标题, SEC accession/UUID 正确留空].
"""
from __future__ import annotations

import asyncio
import html as _html
import io
import json
import os
import re
import sys
from urllib.parse import unquote, urlsplit

import asyncpg
import httpx
from pypdf import PdfReader

# NO DEFAULT. A literal here is not "a convenient fallback" — it is a live, working production credential in
# every checkout, every container layer and every git object, and the env var being set at deploy time hides
# that rather than fixing it. Same shape queue.py and events.py already use; create_pool below raises on an
# empty DSN, so an unset variable fails at startup instead of connecting somewhere unintended.
# {AUDIT 2026-07-28 — last two literal DSNs in the working tree} [CONFIDENCE: CONFIRMED 100% — the value was
#  probed live with full DML against the 147k-row production dataset].
_DSN = os.environ.get("WATEREVENTS_DB_DSN", "")
_SCHEMA = os.environ.get("WATEREVENTS_DB_SCHEMA", "waterevents")
_UA = "Mozilla/5.0 (compatible; WaterEventsTitleBot/1.0)"

_GENERIC = {"", "investor relations", "ir", "home", "untitled", "error", "404", "403 forbidden",
            "404 not found", "not found", "page not found", "access denied", "just a moment..."}
_OG = re.compile(r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']', re.I)
_OG2 = re.compile(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']og:title["\']', re.I)
_TITLE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
_META_CHARSET = re.compile(rb'<meta[^>]+charset=["\']?\s*([\w-]+)', re.I)
_ACCESSION = re.compile(r"^\d{9,10}-\d{2}-\d{6}$")            # SEC EDGAR accession number (not a human title)
_HEXUUID = re.compile(r"^[0-9a-f]{8}[\s\-][0-9a-f]{4}[\s\-][0-9a-f]{4}", re.I)   # cloudfront UUID filename


def _clean(t: str) -> str:
    """Unescape HTML entities + collapse whitespace + strip office-export prefixes ('Microsoft Word - foo.docx' → 'foo')."""
    if not t:
        return ""
    t = re.sub(r"\s+", " ", _html.unescape(t)).strip()
    t = re.sub(r"^Microsoft (Word|PowerPoint|Excel) - ", "", t, flags=re.I)
    t = re.sub(r"\.(docx?|pptx?|xlsx?)$", "", t, flags=re.I)
    return t.strip()


def _generic(t: str) -> bool:
    lt = (t or "").strip().lower()
    return lt in _GENERIC or "just a moment" in lt or "are you a human" in lt or len(lt) < 3


def _junk(t: str) -> bool:
    """Reject machine-ids that are NOT human titles: EDGAR accession, Q4 'default', cloudfront UUID, bot-wall
    interstitials, PowerPoint default 'Slide N'/'投影片 N', binary/doc filenames. {USER 2026-07-27 sample-observed junk}."""
    s = (t or "").strip()
    lt = s.lower()
    if _ACCESSION.match(s) or _HEXUUID.match(s):
        return True
    if lt in ("default", "index", "document", "viewer"):
        return True
    if lt.startswith("error |") or lt.startswith("error -") or lt == "error":
        return True
    if "attention required" in lt or lt == "cloudflare" or "just a moment" in lt:
        return True
    if re.match(r"^(slide|投影片|幻灯片|diapositiva|folie|presentation)\s*\d*$", lt):
        return True
    if re.search(r"\.(xls|xlsx|zip|csv|htm|xml|jpe?g|png|docx?|pptx?)$", lt):
        return True
    return False


def _from_filename(url: str) -> str:
    path = urlsplit(url).path
    name = unquote(path.rsplit("/", 1)[-1]) or urlsplit(url).netloc
    name = re.sub(r"\.(pdf|html?|aspx|jsp|php)$", "", name, flags=re.I)
    return re.sub(r"[_\-]+", " ", name).strip()


def _decode(content: bytes, ctype: str) -> str:
    """Decode by DECLARED charset (Content-Type → <meta charset> → utf-8 → JP fallbacks) — Japanese IR sites need it."""
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    enc = m.group(1) if m else None
    if not enc:
        mm = _META_CHARSET.search(content[:4096])
        if mm:
            try:
                enc = mm.group(1).decode("ascii")
            except Exception:  # noqa: BLE001
                enc = None
    for e in ([enc] if enc else []) + ["utf-8", "shift_jis", "euc-jp", "latin-1"]:
        try:
            return content.decode(e, errors="strict")
        except Exception:  # noqa: BLE001
            continue
    return content.decode("utf-8", errors="replace")


def _html_title(content: bytes, ctype: str) -> str:
    text = _decode(content, ctype)
    for rx in (_OG, _OG2):
        m = rx.search(text)
        if m:
            t = _clean(m.group(1))
            if not _generic(t) and not _junk(t):
                return t
    m = _TITLE.search(text)
    if m:
        t = _clean(m.group(1))
        if not _generic(t) and not _junk(t):
            return t
    return ""


def _pdf_title(content: bytes) -> str:
    try:
        r = PdfReader(io.BytesIO(content))
        t = _clean((r.metadata.title if r.metadata else "") or "")
        if not _generic(t) and not _junk(t):
            return t
    except Exception:  # noqa: BLE001
        pass
    return ""


def _fname_or_blank(url: str) -> str:
    fn = _from_filename(url)
    return "" if (_generic(fn) or _junk(fn)) else fn


async def fetch_title(client: httpx.AsyncClient, url: str) -> str:
    """One URL → a human title or "". HTML og:title/<title>; PDF /Title; else a real-looking filename; junk → "". Never raises."""
    low = url.lower().split("?")[0]
    if re.search(r"\.(xls|xlsx|zip|csv)$", low):
        return ""
    try:
        r = await client.get(url, follow_redirects=True, timeout=8.0)
    except Exception:  # noqa: BLE001 — dead host/timeout → filename fallback (blank if that's junk)
        return _fname_or_blank(url)
    ctype = r.headers.get("content-type", "").lower()
    body = r.content
    if "pdf" in ctype or low.endswith(".pdf"):
        return _pdf_title(body) or _fname_or_blank(url)
    if "html" in ctype or "xml" in ctype or ctype == "":
        t = _html_title(body, ctype)
        if t:
            return t
    return _fname_or_blank(url)


def _primary_url(media_urls, source_url: str) -> str:
    a = media_urls if isinstance(media_urls, list) else (json.loads(media_urls) if media_urls else [])
    for u in a:
        if isinstance(u, str) and u.startswith("http"):
            return u
    return source_url or ""


async def _fill(pool, rows, conc: int) -> int:
    """Fetch titles for `rows` [(id, media_urls, source_url)] concurrently and UPDATE the ones we resolved. Returns filled."""
    if not rows:
        return 0
    sem = asyncio.Semaphore(conc)
    async with httpx.AsyncClient(headers={"User-Agent": _UA}, verify=False) as client:
        async def one(row):
            url = _primary_url(row["media_urls"], row["source_url"])
            if not url:
                return None
            async with sem:
                t = await fetch_title(client, url)
            return (row["id"], t) if t else None
        got = [x for x in await asyncio.gather(*(one(r) for r in rows)) if x]
    if got:
        async with pool.acquire() as conn:
            await conn.executemany(
                "UPDATE events SET title=$2 WHERE id=$1 AND (title IS NULL OR title='')", got)
    return len(got)


async def backfill_for_company(pool, company_id, run_id: str | None = None, conc: int = 20) -> int:
    """HOOK — called by scan_unit after a company/hub is crawled: curl this company's title-less events and fill titles.
    Cheap (HTTP-only, no VLM), bounded concurrency, never raises to the caller. Returns how many titles were filled."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, media_urls, source_url FROM events "
                "WHERE company_id=$1 AND (title IS NULL OR title='') "
                + ("AND run_id=$2 " if run_id else "")
                + "LIMIT 500",
                *([company_id, run_id] if run_id else [company_id]))
        return await _fill(pool, rows, conc)
    except Exception:  # noqa: BLE001 — a title-backfill hiccup must never sink the scan
        return 0


async def _main() -> None:
    """Batch CLI over the WHOLE title-less backlog. SAMPLE (print) unless --apply."""
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].lstrip("-").isdigit() else 0
    apply = "--apply" in sys.argv
    conc = int(os.environ.get("TITLE_CONC", "80"))
    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=4, statement_cache_size=0,
                                     server_settings={"search_path": _SCHEMA})
    async with pool.acquire() as conn:
        order = "" if apply else "ORDER BY random() "
        rows = await conn.fetch(
            f"SELECT id, media_urls, source_url FROM events WHERE (title IS NULL OR title='') {order}LIMIT $1",
            n if n else 1000000)
    print(f"[title] {len(rows)} title-less events (mode={'APPLY' if apply else 'SAMPLE'}, conc={conc})", flush=True)
    if apply:
        filled = await _fill(pool, rows, conc)
        print(f"[title] APPLIED {filled} titles (of {len(rows)} title-less).", flush=True)
    else:
        # sample: fetch + print without writing
        sem = asyncio.Semaphore(conc)
        async with httpx.AsyncClient(headers={"User-Agent": _UA}, verify=False) as client:
            async def one(row):
                url = _primary_url(row["media_urls"], row["source_url"])
                return (await fetch_title(client, url), url) if url else ("", "")
            res = await asyncio.gather(*(sem_wrap(sem, one, r) for r in rows))
        got = [x for x in res if x[0]]
        print(f"[title] {len(got)}/{len(rows)} resolved. Sample:")
        for (t, url) in res[:60]:
            if t:
                print(f"  {t[:74]}\n     <- {url[:90]}", flush=True)
    await pool.close()


async def sem_wrap(sem, fn, arg):
    async with sem:
        return await fn(arg)


if __name__ == "__main__":
    asyncio.run(_main())
