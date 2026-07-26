from db.neo4j_client import Neo4jClient
from db.qdrant_client import KronosQdrantClient

print("Testing Neo4j...")
neo4j = Neo4jClient()
assert neo4j.verify_connection(), "Neo4j connection failed"
neo4j.setup_schema()
print("Neo4j OK")

print("Testing Qdrant...")
qdrant = KronosQdrantClient()
stats = qdrant.get_collection_stats()
print(f"Qdrant OK — {stats}")

neo4j.close()
print("\nAll database connections healthy")