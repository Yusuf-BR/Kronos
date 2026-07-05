from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue, FilterSelector
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
        if self.COLLECTION_NAME not in existing:
            self.client.create_collection(
                collection_name=self.COLLECTION_NAME,
                vectors_config=VectorParams(
                    size=config.EMBEDDING_DIM,
                    distance=Distance.COSINE
                )
            )
            logger.info(f"Created Qdrant collection: {self.COLLECTION_NAME}")

    def add_chunks(self, chunks: list[dict]):
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
                    "type": chunk.get("type", "chunk")
                }
            ))
        self.client.upsert(
            collection_name=self.COLLECTION_NAME,
            points=points
        )

    def delete_by_source(self, source_doc: str):
        """Remove all vectors from a specific document before re-ingesting."""
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
        """Check if a document already has vectors in Qdrant."""
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

    def search(self, query: str, top_k: int = 5, source_doc: str = None, type_filter: str = None):
        vector = self.encoder.encode(query).tolist()

        conditions = []
        if source_doc:
            conditions.append(FieldCondition(key="source_doc", match=MatchValue(value=source_doc)))
        if type_filter:
            conditions.append(FieldCondition(key="type", match=MatchValue(value=type_filter)))

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
                "type": r.payload.get("type", "chunk")
            }
            for r in results
        ]

    def get_collection_stats(self):
        info = self.client.get_collection(self.COLLECTION_NAME)
        return {"total_chunks": info.points_count}