from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter, FieldCondition,
    MatchValue, FilterSelector, PayloadSchemaType
)
from sentence_transformers import SentenceTransformer
from core.config import config
import logging
import uuid

logger = logging.getLogger(__name__)


class KronosQdrantClient:
    COLLECTION_NAME = "kronos_chunks"

    def __init__(self):
        self.client = QdrantClient(
            host=config.QDRANT_HOST,
            port=config.QDRANT_PORT
        )
        self.encoder = SentenceTransformer(config.EMBEDDING_MODEL)
        self._setup_collection()

    def _setup_collection(self):
        existing = [c.name for c in self.client.get_collections().collections]

        # ── kronos_chunks: stores chunk + claim vectors ──────────────────────
        if self.COLLECTION_NAME not in existing:
            self.client.create_collection(
                collection_name=self.COLLECTION_NAME,
                vectors_config=VectorParams(
                    size=config.EMBEDDING_DIM,
                    distance=Distance.COSINE
                )
            )
            logger.info(f"Created Qdrant collection: {self.COLLECTION_NAME}")

        # Index payload fields we filter on in production
        for field in ("source_doc", "type", "domain"):
            try:
                self.client.create_payload_index(
                    collection_name=self.COLLECTION_NAME,
                    field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD
                )
            except Exception:
                pass  # Index may already exist

        # ── kronos_entities: stores entity name embeddings ─────────────────
        if "kronos_entities" not in existing:
            self.client.create_collection(
                collection_name="kronos_entities",
                vectors_config=VectorParams(
                    size=config.EMBEDDING_DIM,
                    distance=Distance.COSINE
                )
            )
            logger.info("Created Qdrant collection: kronos_entities")

        # Domain index for entity similarity searches scoped by domain
        for field in ("type", "domain"):
            try:
                self.client.create_payload_index(
                    collection_name="kronos_entities",
                    field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD
                )
            except Exception:
                pass

    # ── Chunk / Claim writes ───────────────────────────────────────────────

    def add_chunks(self, chunks: list[dict]):
        """
        Upsert chunks/claims into kronos_chunks.
        Each chunk dict must have: text, source_doc, page, confidence, type, domain.
        """
        points = []
        for chunk in chunks:
            vector = self.encoder.encode(chunk["text"]).tolist()
            points.append(PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "text": chunk["text"],
                    "source_doc": chunk["source_doc"],
                    "page": chunk.get("page", 0),
                    "confidence": chunk.get("confidence", 1.0),
                    "type": chunk.get("type", "chunk"),
                    "domain": chunk.get("domain")  # None if not provided — no "General" fallback
                }
            ))
        self.client.upsert(
            collection_name=self.COLLECTION_NAME,
            points=points
        )

    def delete_by_source(self, source_doc: str):
        self.client.delete(
            collection_name=self.COLLECTION_NAME,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(
                        key="source_doc",
                        match=MatchValue(value=source_doc)
                    )]
                )
            )
        )
        logger.info(f"Deleted existing vectors for: {source_doc}")

    def source_exists(self, source_doc: str) -> bool:
        results = self.client.query_points(
            collection_name=self.COLLECTION_NAME,
            query=[0.0] * config.EMBEDDING_DIM,
            limit=1,
            query_filter=Filter(
                must=[FieldCondition(
                    key="source_doc",
                    match=MatchValue(value=source_doc)
                )]
            )
        ).points
        return len(results) > 0

    # ── Semantic search (with optional domain filter) ──────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        source_doc: str = None,
        type_filter: str = None,
        domain_filter: str = None
    ):
        """
        Semantic search over kronos_chunks with optional filtering.
        domain_filter: if provided, only return chunks/claims from that domain.
        """
        vector = self.encoder.encode(query).tolist()

        conditions = []
        if source_doc:
            conditions.append(
                FieldCondition(key="source_doc", match=MatchValue(value=source_doc))
            )
        if type_filter:
            conditions.append(
                FieldCondition(key="type", match=MatchValue(value=type_filter))
            )
        if domain_filter:
            conditions.append(
                FieldCondition(key="domain", match=MatchValue(value=domain_filter))
            )

        filter_ = Filter(must=conditions) if conditions else None

        results = self.client.query_points(
            collection_name=self.COLLECTION_NAME,
            query=vector,
            limit=top_k,
            query_filter=filter_
        ).points

        return [
            {
                "text": r.payload["text"],
                "source_doc": r.payload["source_doc"],
                "page": r.payload["page"],
                "score": r.score,
                "type": r.payload.get("type", "chunk"),
                "domain": r.payload.get("domain")  # None if untagged
            }
            for r in results
        ]

    def get_collection_stats(self):
        info = self.client.get_collection(self.COLLECTION_NAME)
        return {"total_chunks": info.points_count}

    # ── Entity embedding storage ───────────────────────────────────────────

    def upsert_entity_embedding(
        self,
        name: str,
        entity_type: str,
        embedding: list[float],
        domain: str | None = None
    ):
        """
        Store or update an entity's embedding in kronos_entities.
        Includes domain in payload for domain-scoped similarity search.
        """
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, name.lower()))
        self.client.upsert(
            collection_name="kronos_entities",
            points=[
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={
                        "name": name,
                        "type": entity_type,
                        "domain": domain  # None if untagged
                    }
                )
            ]
        )

    # ── Domain-aware entity similarity search ──────────────────────────────

    def find_similar_entity_by_embedding(
        self,
        embedding: list[float],
        entity_type: str,
        domain: str | None = None,
        threshold: float = 0.85
    ) -> dict | None:
        """
        Find the most similar entity of the same type in Qdrant.
        If `domain` is provided, restricts search to entities from that domain.
        Returns the entity if similarity > threshold, otherwise None.
        """
        conditions = [
            FieldCondition(
                key="type",
                match=MatchValue(value=entity_type)
            )
        ]

        if domain:
            conditions.append(
                FieldCondition(
                    key="domain",
                    match=MatchValue(value=domain)
                )
            )

        results = self.client.query_points(
            collection_name="kronos_entities",
            query=embedding,
            query_filter=Filter(must=conditions),
            limit=1
        ).points

        if results and results[0].score > threshold:
            return {
                "name": results[0].payload["name"],
                "type": results[0].payload["type"],
                "domain": results[0].payload.get("domain"),  # None if untagged
                "similarity": results[0].score
            }
        return None
    def search_entities(self, embedding: list[float], top_k: int = 5, domain: str | None = None):
            """
            Like find_similar_entity_by_embedding, but no type filter and no
            threshold cutoff — for query-time entity linking, where you don't yet
            know what type the mention resolves to and want to see all candidates
            to pick from.
            """
            conditions = []
            if domain:
                conditions.append(FieldCondition(key="domain", match=MatchValue(value=domain)))
            filter_ = Filter(must=conditions) if conditions else None

            results = self.client.query_points(
                collection_name="kronos_entities",
                query=embedding,
                query_filter=filter_,
                limit=top_k
            ).points

            return [
                {
                    "name": r.payload["name"],
                    "type": r.payload["type"],
                    "domain": r.payload.get("domain"),
                    "score": r.score
                }
                for r in results
            ]
    def find_nearest_entity_candidate(
        self,
        embedding: list[float],
        entity_type: str,
        domain: str | None = None
    ) -> dict | None:
        """
        Returns the single nearest entity of the same type, REGARDLESS of
        similarity threshold — used for typo detection.
        If `domain` is provided, restricts to that domain.
        """
        conditions = [
            FieldCondition(
                key="type",
                match=MatchValue(value=entity_type)
            )
        ]

        if domain:
            conditions.append(
                FieldCondition(
                    key="domain",
                    match=MatchValue(value=domain)
                )
            )

        results = self.client.query_points(
            collection_name="kronos_entities",
            query=embedding,
            query_filter=Filter(must=conditions),
            limit=1
        ).points

        if results:
            return {
                "name": results[0].payload["name"],
                "type": results[0].payload["type"],
                "domain": results[0].payload.get("domain"),  # None if untagged
                "similarity": results[0].score
            }
        return None

    # ── Domain-scoped search convenience ───────────────────────────────────

    def search_by_domain(
        self,
        query: str,
        domain: str,
        top_k: int = 5,
        type_filter: str = None
    ):
        """
        Convenience wrapper: semantic search restricted to a single domain.
        """
        return self.search(
            query=query,
            top_k=top_k,
            type_filter=type_filter,
            domain_filter=domain
        )
# Add this method to the KronosQdrantClient class in db/qdrant_client.py

def search_entities(self, embedding: list[float], top_k: int = 5, domain: str | None = None):
    """
    Like find_similar_entity_by_embedding, but no type filter and no
    threshold cutoff — for query-time entity linking, where you don't yet
    know what type the mention resolves to and want to see all candidates
    to pick from (unlike ingestion, which needs a single confident match).
    """
    conditions = []
    if domain:
        conditions.append(FieldCondition(key="domain", match=MatchValue(value=domain)))
    filter_ = Filter(must=conditions) if conditions else None

    results = self.client.query_points(
        collection_name="kronos_entities",
        query=embedding,
        query_filter=filter_,
        limit=top_k
    ).points

    return [
        {
            "name": r.payload["name"],
            "type": r.payload["type"],
            "domain": r.payload.get("domain"),
            "score": r.score
        }
        for r in results
    ]