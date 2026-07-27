"""media_agent — stage-2 EVENT enrichment (media fork): a known event's detail page → enriched record (basic_info +
media). Subpackages: extract/ (html→md + classify) · pipeline/ (enrich · handlers · worker) · storage/ (db_media).
Facade re-exports the enrichment endpoint. {2026-07-27 repo restructure}."""
from .pipeline.enrich import enrich_page, enrich_pages  # noqa: F401

__all__ = ["enrich_page", "enrich_pages"]
