"""pytest conftest — put backend/ on sys.path so `import agent.*` / `providers.*` / `tools.*` work from anywhere.

WHY backend/ and not the repo root: the 2026-07-28 restructure collapsed the top level to frontend/ backend/ tests/,
moving every python package (agent, providers, tools) one level down into backend/. tests/ deliberately stayed at the
root, so it has to reach INTO backend/ for its imports. Adding backend/ here is the test-side mirror of what
backend/deploy/launch_fleet.sh does with PYTHONPATH="$CODE_DIR" — one import root, declared in exactly two places.
{RESTRUCTURE 2026-07-28 "agent|providers|deploy|scripts|supabase|tools → backend/"}
[CONFIDENCE: CONFIRMED 100% — with backend/ on the path every existing `import agent.*` statement is unchanged].
"""
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
