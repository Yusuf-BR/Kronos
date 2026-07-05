import logging
logging.basicConfig(level=logging.INFO)

from agents.extractor import ExtractorAgent
from agents.codex import CodexAgent
from agents.reconciler import ReconcilerAgent
import os

inbox = "data/inbox"
pdfs = sorted([f for f in os.listdir(inbox) if f.endswith(".pdf")])

alpha = [f for f in pdfs if "alpha" in f.lower()]
beta = [f for f in pdfs if "beta" in f.lower()]

if not alpha or not beta:
    print("Need doc_alpha.pdf and doc_beta.pdf in data/inbox/")
    exit()

extractor = ExtractorAgent()
codex = CodexAgent()
reconciler = ReconcilerAgent()

# Ingest first document
print(f"\n=== INGESTING DOC 1: {alpha[0]} ===")
extracted1 = extractor.extract_knowledge(os.path.join(inbox, alpha[0]))
codex.ingest(extracted1)

# Reconcile second document against first
print(f"\n=== INGESTING DOC 2: {beta[0]} ===")
extracted2 = extractor.extract_knowledge(os.path.join(inbox, beta[0]))

print(f"\n=== RECONCILING ===")
result = reconciler.reconcile(extracted2)

print(f"\n=== RECONCILIATION REPORT ===")
print(f"Conflicts found:    {result['conflicts_found']}")
print(f"Conflicts resolved: {result['conflicts_resolved']}")
print(f"Conflicts flagged:  {result['conflicts_flagged']}")

if result['details']:
    print(f"\n=== CONFLICT DETAILS ===")
    for c in result['details']:
        print(f"\nEntity: [{c['entity_type']}] {c['entity_name']}")
        print(f"  Doc 1 ({c['existing_doc']}): {c['existing_description'][:100]}")
        print(f"  Doc 2 ({c['new_doc']}): {c['new_description'][:100]}")

codex.ingest(extracted2)
codex.close()
reconciler.close()