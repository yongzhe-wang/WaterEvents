"""WaterEvents tools — single-responsibility, self-contained tools. First tool: pdf_extract.

Each tool is a package under tools/ with a clean public API and lazy heavy deps (import inside functions), so the
package always imports and a missing optional dep degrades to a no-op instead of an ImportError at load.
"""
