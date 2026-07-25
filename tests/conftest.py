import sys
from pathlib import Path
from unittest.mock import MagicMock

# ── 1. Add project root to PYTHONPATH ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── 2. Stub heavy external dependencies BEFORE any test imports them ──
# These are imported at module level by codex.py, extractor.py, etc.
# but our unit tests only need the pure Python logic inside them.
_STUBS = [
    "neo4j",
    "qdrant_client",
    "qdrant_client.models",
    "sentence_transformers",
    "groq",
    "mistralai",
    "langchain_google_genai",
    "langchain_openai",
    "langchain_core",
    "langchain_core.messages",
    "pdf2image",
    "pytesseract",
]

for mod in _STUBS:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Some modules have nested attributes that code expects
sys.modules["neo4j"].GraphDatabase = MagicMock()
sys.modules["qdrant_client"].QdrantClient = MagicMock()
sys.modules["sentence_transformers"].SentenceTransformer = MagicMock()