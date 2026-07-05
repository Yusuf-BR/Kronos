import logging
logging.basicConfig(level=logging.INFO)

from agents.extractor import ExtractorAgent
from agents.codex import CodexAgent
from db.neo4j_client import Neo4jClient
import os

inbox = "data/inbox"
pdfs = [f for f in os.listdir(inbox) if f.endswith(".pdf")]
if not pdfs:
    print("Drop a PDF into data/inbox/ first")
    exit()

filepath = os.path.join(inbox, pdfs[0])
print(f"Testing full pipeline with: {filepath}\n")

# Step 1 - Extract
extractor = ExtractorAgent()
extracted = extractor.extract_knowledge(filepath)
print(f"Extracted: {len(extracted['entities'])} entities, {len(extracted['relationships'])} relationships")

# Step 2 - Ingest
codex = CodexAgent()
result = codex.ingest(extracted)
print(f"Written to DB: {result}")

# Step 3 - Verify in Neo4j
neo4j = Neo4jClient()
stats = neo4j.get_graph_stats()
print(f"\nNeo4j graph stats: {stats}")
codex.close()
neo4j.close()