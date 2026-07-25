from db.qdrant_client import KronosQdrantClient

qdrant = KronosQdrantClient()

print("=== Claims for 'Dr. Ahmed Khalil' from doc_gamma.pdf.pdf ===")
results = qdrant.search(
    query="Dr. Ahmed Khalil",
    top_k=10,
    source_doc="doc_gamma.pdf.pdf",
    type_filter="claim"
)
for r in results:
    print(f"  - {r['text']}")

print("\n=== Same, without type_filter (chunks too) ===")
results2 = qdrant.search(
    query="Dr. Ahmed Khalil",
    top_k=10,
    source_doc="doc_gamma.pdf.pdf"
)
for r in results2:
    print(f"  [{r['type']}] {r['text']}")