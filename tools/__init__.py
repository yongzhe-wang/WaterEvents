"""WaterEvents tools — single-responsibility, self-contained tools.

  officeall      — pdf / pptx / xlsx / docx / html → structured content (Docling engine). Replaced the old
                   pdf_extract + pptx_extract (pypdf/pdfplumber/python-pptx) with one engine: clean markdown +
                   TableFormer structured tables + built-in OCR.
  audio_extract  — audio / video → transcript (faster-whisper, local on the H100). Separate: not an office doc.

Each tool is a package with a clean public API and lazy heavy deps (import inside functions), so the package always
imports and a missing optional dep degrades to a no-op / ok=False instead of an ImportError at load.
"""
