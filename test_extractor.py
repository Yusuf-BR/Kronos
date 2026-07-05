import logging
logging.basicConfig(level=logging.INFO)

from agents.extractor import ExtractorAgent
import json
import os

# Find first PDF in inbox
inbox = "data/inbox"
pdfs = [f for f in os.listdir(inbox) if f.endswith(".pdf")]
if not pdfs:
    print("Drop a PDF into data/inbox/ first")
    exit()

filepath = os.path.join(inbox, pdfs[0])
print(f"Testing with: {filepath}\n")

agent = ExtractorAgent()
result = agent.extract_knowledge(filepath)

print(f"Title: {result['metadata']['title']}")
print(f"Pages: {result['metadata']['page_count']}")
print(f"Entities found: {len(result['entities'])}")
print(f"Relationships found: {len(result['relationships'])}")
print(f"Chunks: {len(result['chunks'])}")
print(f"\nSummary: {result['summary'][:300]}...")
print(f"\nFirst 5 entities:")
for e in result['entities'][:5]:
    print(f"  [{e['type']}] {e['name']} — {e['description']}")
print(f"\nFirst 3 relationships:")
for r in result['relationships'][:3]:
    print(f"  {r['from']} --{r['relation']}--> {r['to']}")