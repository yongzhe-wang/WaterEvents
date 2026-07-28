"""api_service — WaterEvents' public read-only HTTP surface.

Deliberately holds NO database connection: every query goes through PostgREST, whose pool is separate from the
Supavisor pool the crawl fleet depends on. See main.py for the reasoning and the measurements behind it.
"""
