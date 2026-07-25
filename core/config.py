import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # API Keys
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

    # Neo4j
    NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "kronos_password")

    # Qdrant
    QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
    QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))

    # Local model
    LOCAL_MODEL_URL = os.getenv("LOCAL_MODEL_URL", "http://localhost:1234/v1")
    LOCAL_MODEL_NAME = os.getenv("LOCAL_MODEL_NAME", "qwen2.5-coder-14b-instruct")

    # Paths
    INBOX_PATH = os.getenv("INBOX_PATH", "data/inbox")
    PROCESSED_PATH = os.getenv("PROCESSED_PATH", "data/processed")

    # ── Extractor: multi-backend smart routing ──
    EXTRACTOR_BACKEND = os.getenv("EXTRACTOR_BACKEND", "groq")

    # Gemini — has a hard 1,500 req/day cap on the free tier (confirmed
    # exhausted in practice). NOT used for automatic large-doc routing
    # anymore. Left configured only as a manual/opt-in backend.
    EXTRACTOR_GEMINI_MODEL = os.getenv("EXTRACTOR_GEMINI_MODEL", "gemini-2.0-flash")
    EXTRACTOR_MODEL = EXTRACTOR_GEMINI_MODEL

    # Groq (small/medium docs — best quality, tight daily cap)
    EXTRACTOR_GROQ_MODEL = "llama-3.3-70b-versatile"
    EXTRACTOR_GROQ_MODEL_FAST = "llama-3.1-8b-instant"

    # Mistral — NO daily cap (2.25M tokens/min, 5 req/sec). This is now
    # the primary route for large documents AND the fallback whenever
    # Groq's daily quota runs out mid-run.
    EXTRACTOR_FALLBACK_MODEL = os.getenv("EXTRACTOR_FALLBACK_MODEL", "mistral-small-2506")
    EXTRACTOR_LARGE_DOC_BACKEND = os.getenv("EXTRACTOR_LARGE_DOC_BACKEND", "mistral")
    DOMAIN_MODEL = os.getenv("DOMAIN_MODEL", "mistral-small-2506")
    ONTOLOGY_MODEL = os.getenv("ONTOLOGY_MODEL", "mistral-small-2506")
    REFEREE_MODEL = os.getenv("REFEREE_MODEL", "mistral-small-2506")

    # Reconciler
    RECONCILER_BACKEND = os.getenv("RECONCILER_BACKEND", "mistral")
    RECONCILER_MODEL = "llama-3.3-70b-versatile"
    RECONCILER_MISTRAL_MODEL = os.getenv("RECONCILER_MISTRAL_MODEL", "mistral-small-2506")

    # Librarian / Curator / Analyst
    LIBRARIAN_MODEL = "llama-3.1-8b-instant"
    CURATOR_MODEL = "mistral-large-latest"
    ANALYST_MODEL = "gemini-2.0-flash"  # NOTE: also blocked while Gemini's daily cap is exhausted

    # ── Smart Routing Thresholds ──
    GROQ_MAX_CHUNKS = int(os.getenv("GROQ_MAX_CHUNKS", "250"))
    EXTRACTOR_BATCH_SIZE = int(os.getenv("EXTRACTOR_BATCH_SIZE", "3"))
    ESTIMATED_TOKENS_PER_CHUNK = 1300

    # ── Rate Limiting (seconds between API calls) ──
    GEMINI_RATE_LIMIT_DELAY = float(os.getenv("GEMINI_RATE_LIMIT_DELAY", "1.0"))
    MISTRAL_RATE_LIMIT_DELAY = float(os.getenv("MISTRAL_RATE_LIMIT_DELAY", "0.2"))
    GROQ_RATE_LIMIT_DELAY = float(os.getenv("GROQ_RATE_LIMIT_DELAY", "0.0"))

    # Groq rate limits
    GROQ_RPM = 30
    GROQ_RPM_LARGE = 10
    GROQ_RPD = 14400

    # Embedding
    EMBEDDING_MODEL = "all-MiniLM-L6-v2"
    EMBEDDING_DIM = 384

    # Graph
    CONFIDENCE_DECAY_RATE = 0.05
    MIN_CONFIDENCE_THRESHOLD = 0.3

config = Config()