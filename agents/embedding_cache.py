import json
import logging
import unicodedata
from pathlib import Path

from utils.atomic_json import atomic_write_json

logger = logging.getLogger(__name__)

# Round stored embedding values to this many decimals. Cosine similarity
# is unaffected at this precision, but it meaningfully shrinks file size
# since JSON serializes floats as text (e.g. -0.0223869439214468 -> -0.022387).
EMBEDDING_PRECISION = 6

# Soft warning threshold — not an automatic eviction, just a signal that
# it may be time to consider a different storage backend (SQLite, or a
# dedicated Qdrant collection) as the corpus grows.
CACHE_SIZE_WARNING_THRESHOLD = 5000


class EmbeddingCache:
    """
    Persistent cache: normalized text -> embedding vector. Avoids
    recomputing the same embedding across entities/runs. Saves are
    batched (flush() explicitly, not per-entry) to avoid excessive I/O
    on a large document.
    """
    def __init__(self, encoder, cache_file: str = "embedding_cache.json", model_name: str | None = None):
        self.encoder = encoder
        self.cache_file = Path(cache_file)
        self.model_name = model_name or getattr(encoder, "model_name", getattr(encoder, "name", "unknown"))
        self.cache: dict[str, list[float]] = {}
        self._dirty = False
        self.hits = 0
        self.misses = 0
        self._load()
        logger.info(f"EmbeddingCache initialized with {len(self.cache)} entries (model={self.model_name})")

    def _load(self):
        if not self.cache_file.exists():
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load embedding cache, starting fresh: {e}")
            self.cache = {}
            return

        if not isinstance(data, dict):
            logger.warning(f"Embedding cache file has unexpected format ({type(data)}), starting fresh")
            self.cache = {}
            return

        # Versioned format: {"_model": "...", "entries": {...}}.
        # Older cache files are a flat {key: vector} dict with no model tag —
        # treat those as legacy and adopt them under the current model rather
        # than silently discarding a whole corpus of embeddings.
        if "_model" in data and "entries" in data:
            if data["_model"] != self.model_name:
                logger.warning(
                    f"Embedding model changed ('{data['_model']}' -> '{self.model_name}') — "
                    f"invalidating cache to avoid mixing incompatible vectors"
                )
                self.cache = {}
            else:
                self.cache = data["entries"]
        else:
            logger.info("Loaded legacy (unversioned) embedding cache — tagging with current model on next flush")
            self.cache = data

    def flush(self):
        if self._dirty:
            atomic_write_json(
                str(self.cache_file),
                {"_model": self.model_name, "entries": self.cache},
                indent=None
            )
            self._dirty = False

            if len(self.cache) >= CACHE_SIZE_WARNING_THRESHOLD:
                logger.warning(
                    f"EmbeddingCache has grown to {len(self.cache)} entries — "
                    f"flat JSON file is starting to carry real read/write cost. "
                    f"Worth considering a SQLite-backed cache if this keeps growing."
                )

        total = self.hits + self.misses
        ratio = f"{(self.hits / total * 100):.1f}%" if total else "n/a"
        logger.info(f"EmbeddingCache: {self.hits} hits, {self.misses} misses ({ratio} hit rate), {len(self.cache)} total entries")

    @staticmethod
    def _key(text: str) -> str:
        # Accent-insensitive key so French variants (e.g. "Développement" vs
        # "developpement") collide in cache. This affects ONLY the cache key —
        # entity resolution / merging logic in codex.py is untouched, so this
        # cannot cause two distinct entities to be merged; worst case is an
        # unnecessary re-embed, never a wrong match.
        normalized = unicodedata.normalize("NFKD", text.strip().lower())
        normalized = "".join(c for c in normalized if not unicodedata.combining(c))
        normalized = " ".join(normalized.split())  # collapse internal whitespace
        return normalized

    @staticmethod
    def _round_vector(vector: list[float]) -> list[float]:
        return [round(v, EMBEDDING_PRECISION) for v in vector]

    def get_or_compute(self, text: str) -> list[float]:
        key = self._key(text)
        cached = self.cache.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        vector = self._round_vector(self.encoder.encode(text).tolist())
        self.cache[key] = vector
        self._dirty = True
        return vector