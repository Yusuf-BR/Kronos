import logging
import json
from groq import Groq
from db.neo4j_client import Neo4jClient
from db.qdrant_client import KronosQdrantClient
from core.config import config

logger = logging.getLogger(__name__)

ANALYST_SYSTEM_PROMPT = """You are KRONOS, an intelligent knowledge analyst.
You answer questions based ONLY on the provided context from a knowledge graph and document chunks.

You have access to:
1. Vector search results: relevant text passages and claims from documents
2. Graph context: entities and relationships from the knowledge graph

Rules:
- Answer only from the provided context, never from your own knowledge
- Always cite your sources (document name and page)
- If two sources contradict each other, explicitly surface the conflict
- If confidence is low, say so
- If the answer is not in the context, say "I don't have enough information about this in the knowledge base"

Format your answer as:
ANSWER: [your answer]
SOURCES: [list of source documents and pages]
CONFIDENCE: [HIGH/MEDIUM/LOW]
CONFLICTS: [any contradictions found, or "None"]
"""

class AnalystAgent:
    def __init__(self):
        self.groq_client = Groq(api_key=config.GROQ_API_KEY)
        self.neo4j = Neo4jClient()
        self.qdrant = KronosQdrantClient()
        logger.info("AnalystAgent initialized")

    def query(self, question: str) -> dict:
        logger.info(f"Query: {question}")

        # Step 1 — Vector search for relevant chunks and claims
        chunks = self.qdrant.search(question, top_k=5)
        claims = self.qdrant.search(question, top_k=5, type_filter="claim")

        # Step 2 — Graph context: find relevant entities
        graph_context = self._graph_search(question)

        # Step 3 — Check for conflicts in graph
        conflicts = self._get_conflicts()

        # Step 4 — Build context for LLM
        context = self._build_context(chunks, claims, graph_context, conflicts)

        # Step 5 — Ask Groq to synthesize answer
        answer = self._synthesize(question, context)

        return {
            "question": question,
            "answer": answer,
            "chunks_used": len(chunks),
            "claims_used": len(claims),
            "graph_entities": len(graph_context),
            "conflicts_surfaced": len(conflicts)
        }

    def _graph_search(self, question: str) -> list:
        """Find entities in Neo4j that might be relevant to the question."""
        words = [w for w in question.lower().split() if len(w) > 4]
        results = []
        with self.neo4j.driver.session() as session:
            for word in words[:5]:
                result = session.run("""
                    MATCH (e:Entity)
                    WHERE toLower(e.name) CONTAINS $word
                    OPTIONAL MATCH (e)-[r]->(related:Entity)
                    RETURN e.name AS name, e.type AS type,
                           e.source_doc AS source, e.confidence AS confidence,
                           collect(type(r) + ' -> ' + related.name)[..3] AS relationships
                    LIMIT 5
                """, word=word)
                for record in result:
                    results.append(dict(record))
        return results

    def _get_conflicts(self) -> list:
        """Retrieve all CONFLICTS_WITH edges from Neo4j."""
        with self.neo4j.driver.session() as session:
            result = session.run("""
                MATCH (a:Entity)-[r:CONFLICTS_WITH]->(b:Entity)
                RETURN a.name AS entity, a.source_doc AS doc1,
                       b.source_doc AS doc2, a.properties AS desc1,
                       b.properties AS desc2
            """)
            return [dict(record) for record in result]

    def _build_context(self, chunks: list, claims: list,
                       graph_context: list, conflicts: list) -> str:
        parts = []

        if claims:
            parts.append("=== KEY CLAIMS FROM DOCUMENTS ===")
            for c in claims:
                parts.append(f"[{c['source_doc']} p.{c['page']} | score:{c['score']:.2f}] {c['text']}")

        if chunks:
            parts.append("\n=== RELEVANT TEXT PASSAGES ===")
            for c in chunks:
                parts.append(f"[{c['source_doc']} p.{c['page']} | score:{c['score']:.2f}] {c['text'][:300]}")

        if graph_context:
            parts.append("\n=== KNOWLEDGE GRAPH ENTITIES ===")
            for e in graph_context:
                rels = ", ".join(e.get("relationships") or [])
                parts.append(f"[{e['type']}] {e['name']} (from {e['source']}, confidence:{e['confidence']:.1f}) → {rels}")

        if conflicts:
            parts.append("\n=== ⚠️ KNOWN CONFLICTS IN KNOWLEDGE BASE ===")
            for c in conflicts:
                parts.append(f"CONFLICT: '{c['entity']}' is described differently in {c['doc1']} vs {c['doc2']}")

        return "\n".join(parts)

    def _synthesize(self, question: str, context: str) -> str:
        from utils.quota import groq_quota
        groq_quota.acquire(agent_name="Analyst")
        response = self.groq_client.chat.completions.create(
            model=config.RECONCILER_MODEL,
            messages=[
                {"role": "system", "content": ANALYST_SYSTEM_PROMPT},
                {"role": "user", "content": f"CONTEXT:\n{context}\n\nQUESTION: {question}"}
            ],
            temperature=0.2
        )
        return response.choices[0].message.content

    def close(self):
        self.neo4j.close()