import logging
from db.neo4j_client import Neo4jClient
from db.qdrant_client import KronosQdrantClient

logger = logging.getLogger(__name__)

class CodexAgent:
    def __init__(self):
        self.neo4j = Neo4jClient()
        self.qdrant = KronosQdrantClient()
        logger.info("CodexAgent initialized")

    def ingest(self, extracted: dict) -> dict:
        filename = extracted["filename"]
        logger.info(f"Ingesting: {filename}")

        # Clean up existing vectors before re-ingesting
        if self.qdrant.source_exists(filename):
            logger.info(f"  Existing vectors found for {filename} — cleaning up")
            self.qdrant.delete_by_source(filename)

        entity_count = 0
        relationship_count = 0
        chunk_count = 0
        claims_count = 0

        # Write entities to Neo4j with fuzzy deduplication
        for entity in extracted["entities"]:
            try:
                similar = self.neo4j.find_similar_entity(
                    entity["name"], entity["type"]
                )
                if similar and similar["name"] != entity["name"]:
                    logger.info(f"  Fuzzy match: '{entity['name']}' ~ '{similar['name']}' — using canonical")
                    entity["name"] = similar["name"]

                self.neo4j.create_or_update_entity(
                    name=entity["name"],
                    type=entity["type"],
                    source_doc=filename,
                    confidence=1.0,
                    properties={"description": entity.get("description", "")}
                )
                entity_count += 1
            except Exception as e:
                logger.warning(f"  Entity failed [{entity['name']}]: {e}")

        # Write relationships to Neo4j
        for rel in extracted["relationships"]:
            try:
                self.neo4j.create_relationship(
                    from_name=rel["from"],
                    from_type=rel["from_type"],
                    to_name=rel["to"],
                    to_type=rel["to_type"],
                    rel_type=rel["relation"],
                    source_doc=filename,
                    confidence=1.0
                )
                relationship_count += 1
            except Exception as e:
                logger.warning(f"  Relationship failed [{rel['from']} -> {rel['to']}]: {e}")

        # Build all vectors atomically
        all_vectors = []

        if extracted.get("chunks"):
            all_vectors.extend([
                {
                    "text": c["text"],
                    "source_doc": c["source_doc"],
                    "page": c["page"],
                    "confidence": 1.0,
                    "type": "chunk"
                }
                for c in extracted["chunks"]
            ])
            chunk_count = len(extracted["chunks"])

        if extracted.get("claims"):
            all_vectors.extend([
                {
                    "text": c["text"],
                    "source_doc": c["source_doc"],
                    "page": c["page"],
                    "confidence": 1.0,
                    "type": "claim"
                }
                for c in extracted["claims"]
            ])
            claims_count = len(extracted["claims"])

        # Single atomic write to Qdrant
        if all_vectors:
            try:
                self.qdrant.add_chunks(all_vectors)
            except Exception as e:
                logger.error(f"  Qdrant write failed for {filename}: {e}")
                logger.error(f"  DESYNC WARNING: {filename} written to Neo4j but NOT to Qdrant")
                raise

        result = {
            "filename": filename,
            "entities_written": entity_count,
            "relationships_written": relationship_count,
            "chunks_written": chunk_count,
            "claims_written": claims_count
        }
        logger.info(f"  Done: {result}")
        return result

    def close(self):
        self.neo4j.close()