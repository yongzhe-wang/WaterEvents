"""Audit the render corpus for JS-SHELL pages — pages the HTTP-first (curl_cffi, no JS execution) path fetched as an
empty app skeleton (<div id=root>…</div>) and we WRONGLY counted as a successful render.

用一句话讲完: 遍历 hstress_out/trace 每页 → 读 meta.json 的 method + n_links + n_events/routes + content.txt 字符数 +
grep page.html 找 JS-app 标记(id=root/__next/__NEXT_DATA__/ng-app/id=app) → 按 method 分桶,标出「content 极薄 + 有 JS-app
标记」= curl 拿到的空壳 → 给出多少 render「成功」其实是没内容的壳。RAW file 分析,不发一个 VLM 请求。
{USER 2026-07-24 "suspicious about curl, many pages might be js shell, check for those"}.
"""
from __future__ import annotations

import glob
import json
import os
import re

TRACE = "/workspace/WaterEvents/tests/hstress_out/trace"
THIN_CHARS = 800          # content.txt below this ≈ no real rendered text (a shell)
THIN_LINKS = 8            # fewer real links than a normal IR nav ≈ shell
SHELL_RE = re.compile(rb'id=["\']root["\']|id=["\']app["\']|id=["\']__next["\']|__NEXT_DATA__|ng-app|data-reactroot|window\.__NUXT__', re.I)


def main() -> None:
    metas = glob.glob(os.path.join(TRACE, "**", "meta.json"), recursive=True)
    by_method: dict[str, int] = {}
    thin, shell_markers, thin_and_marked = 0, 0, 0
    size_buckets = {"<200": 0, "200-800": 0, "800-3k": 0, "3k-10k": 0, ">10k": 0}
    link_buckets = {"0": 0, "1-5": 0, "6-20": 0, "21-50": 0, "51-100": 0, ">100": 0}   # LINK count histogram
    links_by_method: dict[str, list] = {}               # method → [n_links,...] for per-method distribution
    text_full_link_poor = 0                             # chars>800 BUT links<10 → text shell w/ JS-missing links
    tfp_suspects: list[tuple[str, str, int, int]] = []  # that cohort's worst offenders
    suspects: list[tuple[str, str, int, int]] = []      # (url, method, chars, n_links) for the worst offenders
    n = 0
    for m in metas:
        d = os.path.dirname(m)
        html_p = os.path.join(d, "page.html")
        c_p = os.path.join(d, "content.txt")
        try:
            meta = json.load(open(m, encoding="utf-8")) or {}
        except Exception:                               # noqa: BLE001
            continue
        n += 1
        method = meta.get("method") or "?"
        by_method[method] = by_method.get(method, 0) + 1
        chars = os.path.getsize(c_p) if os.path.exists(c_p) else 0    # content.txt bytes ≈ rendered text volume
        nlinks = int(meta.get("n_links") or 0)
        # size histogram
        if chars < 200: size_buckets["<200"] += 1
        elif chars < 800: size_buckets["200-800"] += 1
        elif chars < 3000: size_buckets["800-3k"] += 1
        elif chars < 10000: size_buckets["3k-10k"] += 1
        else: size_buckets[">10k"] += 1
        # LINK histogram — few links on an IR page ≈ the event/detail/webcast links were JS-loaded and curl missed them
        if nlinks == 0: link_buckets["0"] += 1
        elif nlinks <= 5: link_buckets["1-5"] += 1
        elif nlinks <= 20: link_buckets["6-20"] += 1
        elif nlinks <= 50: link_buckets["21-50"] += 1
        elif nlinks <= 100: link_buckets["51-100"] += 1
        else: link_buckets[">100"] += 1
        links_by_method.setdefault(method, []).append(nlinks)     # per-method link distribution
        if chars >= 800 and nlinks < 10:                          # text-full BUT link-poor → the size-check blind spot
            text_full_link_poor += 1
            if len(tfp_suspects) < 40:
                tfp_suspects.append((meta.get("url", "")[:70], method, chars, nlinks))
        is_thin = chars < THIN_CHARS or nlinks < THIN_LINKS
        has_marker = False
        if os.path.exists(html_p):
            try:
                head = open(html_p, "rb").read()          # whole html — shell markers can be anywhere
                has_marker = bool(SHELL_RE.search(head))
            except Exception:                             # noqa: BLE001
                pass
        if is_thin: thin += 1
        if has_marker: shell_markers += 1
        if is_thin and has_marker:                        # THE smoking gun: JS-app skeleton + no rendered content
            thin_and_marked += 1
            if len(suspects) < 40:
                suspects.append((meta.get("url", "")[:70], method, chars, nlinks))

    def _stats(xs: list) -> str:                        # min / median / mean / max for a link list
        if not xs:
            return "n=0"
        s = sorted(xs)
        return f"n={len(s)} min={s[0]} p50={s[len(s)//2]} mean={sum(s)/len(s):.0f} max={s[-1]}"

    print("=== JS-SHELL AUDIT ===")
    print(f"pages analyzed          : {n}")
    print(f"by render method        : {dict(sorted(by_method.items(), key=lambda x: -x[1]))}")
    print(f"content.txt size buckets: {size_buckets}")
    print(f"LINK count buckets       : {link_buckets}")
    print("per-method LINK stats    :")
    for meth, xs in sorted(links_by_method.items(), key=lambda x: -len(x[1])):
        print(f"    [{meth}]  {_stats(xs)}")
    print(f"THIN (content<{THIN_CHARS}B OR links<{THIN_LINKS}): {thin}  ({100*thin/max(n,1):.1f}%)")
    print(f"has JS-app marker        : {shell_markers}  ({100*shell_markers/max(n,1):.1f}%)")
    print(f">>> THIN *AND* JS-app marker (curl-served empty shell): {thin_and_marked}  ({100*thin_and_marked/max(n,1):.1f}%)")
    print(f">>> TEXT-FULL (>800B) BUT LINK-POOR (<10 links) [size-check blind spot]: {text_full_link_poor}  ({100*text_full_link_poor/max(n,1):.1f}%)")
    print("\n=== text-full-but-link-poor suspects (content_bytes | n_links | method | url) ===")
    for u, meth, ch, nl in tfp_suspects:
        print(f"  {ch:6d}B  {nl:3d}L  [{meth}]  {u}")
    print("\n=== worst empty-shell suspects (content_bytes | n_links | method | url) ===")
    for u, meth, ch, nl in suspects:
        print(f"  {ch:6d}B  {nl:3d}L  [{meth}]  {u}")


if __name__ == "__main__":
    main()
