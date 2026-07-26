from agents.codex import CodexAgent

codex = CodexAgent()

mock_ml = {
    "filename": "test_ml.pdf",
    "domain": "Data Science",
    "entities": [
        {
            "name": "Transformer",
            "type": "TECHNOLOGY",
            "description": "Neural network architecture using self-attention",
            "_domain": "Data Science",
            "_extraction_confidence": 0.95,
            "_source_doc": "test_ml.pdf",
            "_page": 1,
            "_chunk_id": "test:1:0",
            "_excerpt": "The Transformer architecture revolutionized NLP."
        }
    ],
    "relationships": [],
    "claims": [],
    "chunks": [],
    "quality_score": 1.0
}

mock_ee = {
    "filename": "test_ee.pdf",
    "domain": "Electrical Engineering",
    "entities": [
        {
            "name": "Transformer",
            "type": "TECHNOLOGY",
            "description": "Electrical device for changing voltage levels",
            "_domain": "Electrical Engineering",
            "_extraction_confidence": 0.95,
            "_source_doc": "test_ee.pdf",
            "_page": 1,
            "_chunk_id": "test:1:0",
            "_excerpt": "The transformer steps voltage up or down."
        }
    ],
    "relationships": [],
    "claims": [],
    "chunks": [],
    "quality_score": 1.0
}

print("Ingesting ML doc...")
codex.ingest(mock_ml)
print("Ingesting EE doc...")
codex.ingest(mock_ee)
codex.close()
print("Done.")