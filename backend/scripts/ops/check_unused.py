"""check_unused — fail the build on a module-level definition that nothing ever calls.

用一句话讲完: 扫所有 tracked 的 .py, 找出「定义了但全仓零引用」的顶层函数/类, 有一个就 exit 1 —— 因为这个仓库
今天一天里出现了 5 个「修复写完了但没接线」的函数, 每一个都让人以为问题已经解决, 而实际上那段代码从未执行。

WHY this specific check. CI already catches the mirror case: ruff's F821 flags a name that is USED but never DEFINED.
The failure mode this repo actually produces is the opposite one — a name that is DEFINED but never used — and no
standard linter reports it, because at module scope an unused public function is normally a legitimate API. Here it is
not: on 2026-07-28 `url_allowed` (an SSRF + robots guard), `_close_ctx` (a teardown timeout), `log_slots_if_low` (a
semaphore-exhaustion warning) and `reconcile_work` (the lease reaper) were all written, reviewed and believed shipped,
while every one of them had zero call sites. The engine's `status="incomplete"` had the same shape one layer up: it was
computed correctly and then dropped by its caller, which is what let a 11h52m total outage look healthy.
{AUDIT 2026-07-28 "git grep <fn> → only the definition line, for 5 separate freshly-written fixes"}
[CONFIDENCE: CONFIRMED 100% — each was verified by grepping the whole tree for call sites and finding only the def.]

WAIVERS. A definition may opt out with a `# ci: allow-unused` comment within the 16 lines above it, and the convention
is that the waiver carries the reason. That keeps deliberate not-yet-wired code (e.g. a fix that needs a data backfill
before it can be switched on) visible as NAMED debt in review, instead of pushing the whole check into a config file
nobody reads.

Upstream trigger: .github/workflows/ci.yml. Downstream: a non-zero exit fails the build with one ::error:: per finding.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys

# How far above a definition the waiver comment may sit. 16 lines is generous enough for this repo's long evidence
# comments (which routinely run 8-12 lines) without letting a waiver drift onto an unrelated definition.
_WAIVER_LOOKBACK = 16
_WAIVER = "ci: allow-unused"


def tracked_python() -> list[str]:
    """Every .py git knows about. `git ls-files` rather than a filesystem walk so build artifacts, virtualenvs and
    untracked scratch files can never influence the verdict."""
    out = subprocess.run(["git", "ls-files", "*.py"], capture_output=True, text=True).stdout
    return out.split()


def referenced_names(src: dict[str, str]) -> set[str]:
    """Every identifier the corpus actually READS — `ast.Name` loads, `ast.Attribute` accesses, and the names in
    `from x import y`. A definition's own `def foo` does NOT appear here, because FunctionDef stores its name as a
    plain string attribute rather than as a Name node, so membership in this set means "something uses it".

    WHY AST rather than a text search, which is what the first version of this check did: a text search counts
    OCCURRENCES IN COMMENTS. `reconcile_work` was mentioned by name in two explanatory comments while having zero call
    sites, so a textual count saw three references and stayed silent — the check passed on exactly the class of bug it
    was written to catch. Verified by unwiring the one real call and re-running: the text version reported clean, this
    version names the file and line.
    [CONFIDENCE: CONFIRMED 100% — both versions were run against the same deliberately-unwired tree.]"""
    used: set[str] = set()
    for text in src.values():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)                    # q.reconcile_work(...) → "reconcile_work"
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    used.add(a.asname or a.name)       # a re-export in __init__.py counts as a use
    return used


def find_unused(src: dict[str, str]) -> list[str]:
    """Module-level defs that nothing in the corpus references."""
    used = referenced_names(src)
    dead: list[str] = []
    for path, text in src.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue                                   # the parse check owns syntax; don't double-report it here
        lines = text.splitlines()
        # A script with a __main__ guard may legitimately keep private helpers that only it calls; those still show up
        # as one textual occurrence if the caller is inside a nested scope, so private names in entrypoints are exempt.
        entrypoint = "__main__" in text
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = node.name
            if name.startswith("__") or name == "main":
                continue
            if entrypoint and name.startswith("_"):
                continue
            head = "\n".join(lines[max(0, node.lineno - _WAIVER_LOOKBACK):node.lineno])
            if _WAIVER in head:
                continue
            if name not in used:
                dead.append(f"{path}:{node.lineno}  {name}")
    return sorted(dead)


def load_baseline() -> set[str]:
    """The known-unused backlog, one bare NAME per line, '#' comments ignored.

    WHY a ratchet instead of shipping this check green or shipping it as a warning. Turning the check on found 13
    genuinely-unused definitions already in the tree — an entire never-wired circuit breaker in host_health, the
    stage-2 lease reclaimer, and a sync pacing helper added the same day. Failing on all of them means the check is
    red the day it lands, and a check that is red on day one gets switched off rather than fixed; that is the same
    reasoning the credential/parse checks in this workflow were built around. Making it a warning is worse: today's
    entire post-mortem is about signals that were emitted and never acted on.
    A baseline keeps the check BLOCKING for anything new — the exact failure mode being prevented — while the existing
    backlog is written down by name, in git, where it can only shrink. Names are matched WITHOUT file/line so that
    moving or reformatting code does not silently re-baseline it.
    [CONFIDENCE: CONFIRMED 100% — the 13 were produced by running this check against the tree at the time it landed.]"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unused_baseline.txt")
    if not os.path.exists(path):
        return set()
    out = set()
    for line in open(path, encoding="utf8"):
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def main() -> int:
    src = {}
    for f in tracked_python():
        try:
            src[f] = open(f, encoding="utf8").read()
        except OSError:
            continue
    dead = find_unused(src)
    baseline = load_baseline()
    dead_names = {d.split()[-1] for d in dead}
    new = [d for d in dead if d.split()[-1] not in baseline]
    # A baseline entry that is no longer unused must be REMOVED, not left to rot. Without this the file only ever grows
    # and slowly becomes a permanent exemption list — which is how a ratchet turns back into a disabled check.
    stale = sorted(baseline - dead_names)

    for d in new:
        print(f"::error::defined but never used — {d}")
    for s in stale:
        print(f"::error::{s} is in unused_baseline.txt but is now used — delete that line (the backlog only shrinks)")
    print(f"unused: {len(dead)} total, {len(baseline)} baselined, {len(new)} new, {len(stale)} stale-baseline")
    return 1 if (new or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
