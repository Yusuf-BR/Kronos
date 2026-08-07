import logging
logging.basicConfig(level=logging.INFO)

from agents.extractor import ExtractorAgent
import os

inbox = "data/inbox"
pdfs = [f for f in os.listdir(inbox) if f.endswith(".pdf")]
if not pdfs:
    print("Drop a PDF into data/inbox/ first")
    exit()

filepath = os.path.join(inbox, pdfs[0])
print(f"Testing claims with: {filepath}\n")

extractor = ExtractorAgent()
result = extractor.extract_knowledge(filepath)

print(f"=== SUMMARY ===")
print(result['summary'])

print(f"\n=== CLAIMS EXTRACTED ({len(result['claims'])}) ===")
for claim in result['claims']:
    print(f"\n[Page {claim['page']}] {claim['text']}")

print(f"\n=== STATS ===")
print(f"Entities: {len(result['entities'])}")
print(f"Relationships: {len(result['relationships'])}")
print(f"Claims: {len(result['claims'])}")
print(f"Chunks: {len(result['chunks'])}")