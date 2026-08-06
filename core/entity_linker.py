"""
core/entity_linker.py

Query-time entity linking: resolves a free-text mention from a user's
question to a canonical (name, type) pair already sitting in the graph.
Mirrors the resolution cascade in codex.py, but simplified — at query time
there's no new entity to *create*, only an existing one to *find*, so this
skips the referee/typo/negation machinery and just tries alias memory then
embedding similarity.
"""
import re
import logging
from db.neo4j_client import Neo4jClient
from db.qdrant_client import KronosQdrantClient
from agents.alias_memory import AliasMemory

logger = logging.getLogger(__name__)

# Looser than ingestion's 0.85 — query text is short and often just a
# fragment ("logistic loss" vs. a full description), so exact phrasing
# match matters less than at ingestion time.
ENTITY_LINK_THRESHOLD = 0.75


def _normalize(name: str) -> str:
    normalized = name.lower().strip()
    normalized = re.sub(r'\s+', ' ', normalized)
    normalized = re.sub(r'[^\w\s]', '', normalized)
    return normalized


class EntityLinker:
    def __init__(self, neo4j: Neo4jClient, qdrant: KronosQdrantClient, alias_memory: AliasMemory):
        self.neo4j = neo4j
        self.qdrant = qdrant
        self.alias_memory = alias_memory
        self.encoder = qdrant.encoder

    def link(self, mention: str, domain: str | None = None) -> dict | None:
        """
        Resolve one mention. Returns None rather than a low-confidence
        guess — an unlinked mention should fall through to plain vector
        search over chunks, not anchor a graph walk on a bad match.
        """
        normalized = _normalize(mention)

        canonical, confidence = self.alias_memory.get(normalized)
        if canonical:
            entity = self.neo4j.get_entity_by_name(canonical)
            if entity:
                logger.info(f"  Linked '{mention}' -> '{canonical}' (alias memory, {confidence:.2f})")
                return {**entity, "link_confidence": confidence, "link_source": "alias"}

        embedding = self.encoder.encode(mention).tolist()
        candidates = self.qdrant.search_entities(embedding, top_k=1, domain=domain)
        if candidates and candidates[0]["score"] >= ENTITY_LINK_THRESHOLD:
            top = candidates[0]
            logger.info(f"  Linked '{mention}' -> '{top['name']}' (embedding, {top['score']:.2f})")
            return {
                "name": top["name"], "type": top["type"], "domain": top.get("domain"),
                "link_confidence": top["score"], "link_source": "embedding"
            }

        logger.info(f"  No confident link found for '{mention}'")
        return None

    def link_all(self, mentions: list[str], domain: str | None = None) -> list[dict]:
        linked = [self.link(m, domain=domain) for m in mentions]
        return [l for l in linked if l is not None]