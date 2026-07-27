"""event_agent.crawl — the crawl engine (render→extract→route BFS). Facade re-exports so external callers keep using
`from agent.event_agent.crawl import crawl_company` after the flat→subpackage split. {2026-07-27 repo restructure}."""
from .engine import crawl_company                             # the BFS close-loop
from .extract import extract_page, extract_pages             # the VLM extraction endpoint

__all__ = ["crawl_company", "extract_page", "extract_pages"]
