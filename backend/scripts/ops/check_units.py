#!/usr/bin/env python3
"""check_units — refuse systemd unit files whose ExecStart systemd will silently rewrite.

用一句话讲完: 扫 backend/deploy/systemd/*.service 的 ExecStart, 凡是内联 `bash -c '...'` 又在里面用了引号,
或者写了未转义的 `%`, 就报错 —— 因为 systemd 会在 shell 看到这行之前把它改掉, 而且改完照样报成功。

WHY THIS EXISTS. waterevents-janitor.service carried its retention logic inline:

    ExecStart=/bin/bash -c 'set -o pipefail; T=/…/traces; [ -d "$T" ] || exit 0; find "$T" … '

systemd parses ExecStart with its OWN quoting rules before any shell runs. The inner double quotes around $T
terminate its quoted-string parsing, so what it actually stored was:

    argv[]=/bin/bash -c set -o pipefail

— a shell that sets an option and exits 0. The unit ran daily, reported Finished in one second, and deleted
nothing, for long enough to accumulate 127,523 directories and 36GB of crawl traces on a 97GB disk. `%` is a
second instance of the same hazard: it is systemd's specifier prefix, so find's `-printf '%T@ %p'` is rewritten
too, and `\t` / `\0` go through systemd's escape processing before bash ever sees them.

None of this produces an error. systemd logs "Ignoring unknown escape sequences" at daemon-reload and carries on,
and the unit's exit status is whatever the truncated remnant returns — 0. That combination, a silent rewrite plus
a successful exit, is why a static check is worth more here than any amount of runtime monitoring.

{JOURNAL 2026-08-01 "systemd[1]: /etc/systemd/system/waterevents-janitor.service:42: Ignoring unknown escape
 sequences: \"set -o pipefail; T=...\""}
{SYSTEMCTL 2026-08-01 "systemctl show -p ExecStart --value waterevents-janitor.service" ->
 "argv[]=/bin/bash -c set -o pipefail"}
{MEASURED 2026-08-01 "127523 dirs / 36G" in traces while the timer reported success daily}
[CONFIDENCE: CONFIRMED 100% — the truncated argv was read back out of systemd on the production host.]

THE FIX THIS ENFORCES: put the logic in a script file and point ExecStart at it. deploy/janitor.sh,
deploy/reaper.sh and deploy/watchdog.sh are all files for exactly this reason. A file is executed by a shell,
so systemd's quoting and specifier rules never touch its contents.

Upstream trigger: CI (.github/workflows/ci.yml) and any manual run. Downstream: exits non-zero with the offending
file:line, blocking the commit.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
UNIT_DIR = ROOT / "backend" / "deploy" / "systemd"

# Only Exec* directives run through the parser that caused this; Environment= has its own (also quote-sensitive)
# rules, so it is checked for the same two hazards rather than being trusted.
DIRECTIVE = re.compile(r"^(ExecStart|ExecStartPre|ExecStartPost|ExecStop|ExecReload|Environment)\s*=\s*(.*)$")
INLINE_SHELL = re.compile(r"\b(?:/bin/|/usr/bin/|)(?:ba|da|z|k|)sh\s+-[a-z]*c\b")


def problems(path: Path) -> list[str]:
    """Return one message per hazardous directive in `path`. Empty list means the unit is safe to install."""
    out: list[str] = []
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if line.startswith("#") or line.startswith(";"):      # unit-file comments — not parsed as directives
            continue
        m = DIRECTIVE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)

        # HAZARD 1 — an inline shell whose script body contains a quote. systemd consumes the quote as its own
        # delimiter and truncates the command there. A quote-free `sh -c` body survives, so the check is on the
        # combination rather than on `sh -c` alone; that keeps a legitimately simple one-liner allowed.
        if INLINE_SHELL.search(val) and ('"' in val or "'" in val.strip("'\"")):
            out.append(f"{path.name}:{n}: {key} runs an inline shell whose body contains quotes — systemd will "
                       f"truncate it at the first quote and still exit 0. Move the logic into a script file "
                       f"(see deploy/janitor.sh) and point {key} at that file.")

        # HAZARD 2 — a bare `%`. systemd reads it as a specifier prefix; only `%%` reaches the process as a literal
        # percent. This is what makes find's -printf and any printf/date format unsafe to write in a unit.
        for pm in re.finditer(r"%(.)", val):
            if pm.group(1) != "%":
                out.append(f"{path.name}:{n}: {key} contains an unescaped %{pm.group(1)} — systemd expands % as a "
                           f"specifier. Write %% for a literal percent, or move the command into a script file.")
                break
    return out


def main() -> int:
    if not UNIT_DIR.is_dir():
        print(f"check_units: {UNIT_DIR} not found", file=sys.stderr)
        return 2
    units = sorted(UNIT_DIR.glob("*.service")) + sorted(UNIT_DIR.glob("*.timer")) + sorted(UNIT_DIR.glob("*.target"))
    found = [msg for u in units for msg in problems(u)]
    for msg in found:
        print(f"  {msg}", file=sys.stderr)
    print(f"check_units: {len(units)} unit files, {len(found)} problems")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
