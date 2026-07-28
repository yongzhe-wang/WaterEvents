"""event_agent.title — cheap, VLM-free event-title backfill by HTTP-curling event URLs (HTML og:title/<title>, PDF
/Title, junk-filtered). Used both as a per-scan hook (backfill_for_company) and a batch CLI (backfill.py). Kept in its
OWN subpackage because it's a separate concern from crawl/extract. {USER 2026-07-27 "own subfolder for this"}."""
from .backfill import backfill_for_company, fetch_title  # noqa: F401
