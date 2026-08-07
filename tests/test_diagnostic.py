# tests/test_diagnostic.py
import os
from turtle import clear
from core.config import config
from core.retrieval import Retriever


def test_diagnose_environment():
    print("\ncwd:", os.getcwd())
    print("GROQ_API_KEY set:", bool(config.GROQ_API_KEY))
    print("MISTRAL_API_KEY set:", bool(config.MISTRAL_API_KEY))

    r = Retriever()
    print("Groq client type:", type(r.mention_extractor.client))
    print("Neo4j driver type:", type(r.neo4j.driver))
    print("Qdrant client type:", type(r.qdrant.client))

    # Real call, bypassing mention extraction entirely
    result = r.retrieve("what is machine learning?", mentions=["Machine learning"])
    print("linked_entities:", result["linked_entities"])
    r.close()