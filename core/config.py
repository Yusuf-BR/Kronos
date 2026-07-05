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

    # Model routing
    EXTRACTOR_BACKEND = os.getenv("EXTRACTOR_BACKEND", "groq")  # "groq", "gemini", or "local"
    EXTRACTOR_MODEL = "gemini-2.0-flash"           # used only if backend=gemini
    EXTRACTOR_GROQ_MODEL = "llama-3.3-70b-versatile"
    LIBRARIAN_MODEL = "llama-3.1-8b-instant"       # Groq
    RECONCILER_MODEL = "llama-3.3-70b-versatile"   # Groq
    CURATOR_MODEL = "mistral-large-latest"          # Mistral, nightly
    ANALYST_MODEL = "gemini-2.0-flash"

    # Embedding
    EMBEDDING_MODEL = "all-MiniLM-L6-v2"
    EMBEDDING_DIM = 384

    # Graph
    CONFIDENCE_DECAY_RATE = 0.05
    MIN_CONFIDENCE_THRESHOLD = 0.3

config = Config()