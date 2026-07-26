# Quick Python check
from db.qdrant_client import KronosQdrantClient
qc = KronosQdrantClient()

# Check a chunk
chunks = qc.client.query_points(
    collection_name="kronos_chunks",
    query=[0.0] * 384,
    limit=3
).points
for p in chunks:
    print(p.payload.get("domain"), p.payload.get("type"), p.payload.get("source_doc")[:40])

# Check an entity
entities = qc.client.query_points(
    collection_name="kronos_entities",
    query=[0.0] * 384,
    limit=3
).points
for p in entities:
    print(p.payload.get("domain"), p.payload.get("name"))