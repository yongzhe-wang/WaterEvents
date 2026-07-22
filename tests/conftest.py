"""pytest conftest — make WaterEvents root importable so `import tools.*` works from anywhere.

用一句话讲完: 把 WaterEvents 根目录塞进 sys.path,这样 tests/ 里 `from tools.pdf_extract import ...` 能直接跑,
不用装成 package。live 集成测试(需真实依赖 curl_cffi/pypdf/whisper/python-pptx + 网络)用 @needs_deps 跳过,
纯逻辑单测(detect/magic/Result)无依赖,任何机器都能跑。
"""
import sys
from pathlib import Path

import pytest

# WaterEvents root = tests/ 的上一级 → 加进 sys.path 顶部,让 `import tools.*` 生效。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _has(mod: str) -> bool:
    """True if an optional heavy dep is importable — used to SKIP live tests on a machine without it."""
    import importlib.util
    return importlib.util.find_spec(mod) is not None


# Skip markers for the deps each live path needs (so `pytest` on the Mac runs the pure-logic tests + skips live).
needs_curl = pytest.mark.skipif(not _has("curl_cffi"), reason="curl_cffi not installed (live fetch)")
needs_pypdf = pytest.mark.skipif(not _has("pypdf"), reason="pypdf not installed (pdf text)")
needs_pptx = pytest.mark.skipif(not _has("pptx"), reason="python-pptx not installed (slides)")
needs_whisper = pytest.mark.skipif(not _has("faster_whisper"), reason="faster-whisper not installed (transcribe)")
