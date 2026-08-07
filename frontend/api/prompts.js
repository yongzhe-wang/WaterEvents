// GET /api/prompts — the pipeline's REAL system prompts, read out of the python source at request time.
//
// 用一句话讲完: 直接打开 backend/agent/media_agent/extract/prompts.py,把里面的三元引号字符串常量抠出来返回 —— 不抄、
// 不缓存、不做构建期快照,所以 playground 里选的 SYSTEM_ROUTE 永远就是管线这一刻真正在用的那一份。
//
// WHY parse the source instead of vendoring a copy: a copy is a second source of truth, and the FIRST time someone
// tunes a prompt in python without remembering this file, the playground starts testing a prompt that no longer runs.
// That failure is silent and it invalidates every conclusion drawn from the tool — which is the one thing a testing
// surface must not do. Reading the file makes drift impossible rather than unlikely.
//
// The parse is deliberately narrow: top-level `NAME = """..."""` only. It is not a python parser and does not try to
// be one; anything it cannot read is simply absent from the list, which is visible, rather than wrong, which is not.
//
// Upstream: PlaygroundView's system-prompt picker. Downstream: the python source file, read-only.
import { readFile } from "node:fs/promises";
import { join } from "node:path";

// Candidate locations, in order. The deployed web app runs on ir-media-8 out of ~/WaterEvents/frontend with the
// backend checked out as its sibling, so the relative hop is the normal case; the env var covers any other layout.
// {IR-RENDER-16 2026-08-07 "PYTHONPATH=/home/thebigsun/WaterEvents/backend" — the same sibling layout on the fleet.}
const ROOTS = [
  process.env.WATEREVENTS_BACKEND_DIR,
  join(process.cwd(), "..", "backend"),
  join(process.cwd(), "backend"),
].filter(Boolean);

const FILES = [
  // The three-task ROUTE prompt the html handler actually sends, plus the legacy JS-shell fallback beside it.
  "agent/media_agent/extract/prompts.py",
];

// `NAME = """ ... """` at column 0. Backslash-newline continuations inside the string are joined the way python joins
// them, because the source wraps long lines that way and leaving the backslashes in would show the user a prompt that
// differs from the one the model receives.
// {PROMPTS.PY "SYSTEM_ROUTE = \"\"\"YOU ARE GIVEN ONE WEB PAGE FROM A COMPANY'S INVESTOR-RELATIONS SITE, PLUS WHAT OUR
//  CRAWLER BELIEVES IT TO BE. ...\"} — the literal spans ~35 wrapped lines joined by trailing backslashes.
// [CONFIDENCE: CONFIRMED 100% — read from the file; the continuation style is used throughout both constants.]
const CONST_RE = /^([A-Z][A-Z0-9_]*)\s*=\s*"""([\s\S]*?)"""/gm;

export default async function handler(_req, res) {
  const out = [];
  const tried = [];
  for (const root of ROOTS) {
    for (const rel of FILES) {
      const p = join(root, rel);
      tried.push(p);
      let src;
      try { src = await readFile(p, "utf8"); } catch { continue; }
      for (const m of src.matchAll(CONST_RE)) {
        const [, name, raw] = m;
        out.push({
          name,
          source: rel,
          // Join python's line continuations, then trim the leading newline the opening `"""` leaves behind.
          text: raw.replace(/\\\n\s*/g, "").replace(/^\n/, ""),
        });
      }
      if (out.length) break;
    }
    if (out.length) break;
  }
  res.setHeader("Cache-Control", "no-store");            // the file is the truth; never serve a stale read
  if (!out.length) {
    // Name the paths that were checked. "No prompts" with no explanation would read as "the pipeline has no prompts",
    // which is a very different and much more alarming statement than "this box has no backend checkout".
    return res.status(200).json({ prompts: [], error: "prompts.py not found", tried });
  }
  return res.status(200).json({ prompts: out });
}
