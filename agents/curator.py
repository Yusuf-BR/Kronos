import logging
from groq import Groq
from mistralai import Mistral
from db.neo4j_client import Neo4jClient
from db.qdrant_client import KronosQdrantClient
from core.config import config
from datetime import datetime
from agents.learning_engine import LearningEngine
from agents.entity_memory import EntityMemory
from agents.alias_memory import AliasMemory

logger = logging.getLogger(__name__)

CURATOR_PROMPT = """You are KRONOS Curator, responsible for maintaining knowledge graph health.
You will receive a snapshot of the current knowledge base state and generate a Knowledge Digest.

The digest should include:
1. Overall health assessment
2. Key entities and their confidence levels
3. Active conflicts that need human review
4. Stale or low-confidence knowledge
5. Notable relationships discovered
6. Learning Engine findings — reinforced aliases and unlinked entity pairs worth reviewing
7. Recommendations for improving knowledge quality

Write in a clear, professional tone. Be concise but informative.
This digest will be read by the system administrator to understand the state of the knowledge base.
"""

class CuratorAgent:
    def __init__(self):
        self.groq_client = Groq(api_key=config.GROQ_API_KEY)
        self.mistral_client = Mistral(api_key=config.MISTRAL_API_KEY)
        self.neo4j = Neo4jClient()
        self.qdrant = KronosQdrantClient()
        self.learning_engine = LearningEngine()
        logger.info("CuratorAgent initialized")

    def run(self) -> dict:
        """Full curation cycle — runs nightly."""
        logger.info("Starting curation cycle...")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        decayed = self._decay_confidence()
        stale = self.neo4j.get_stale_entities(config.MIN_CONFIDENCE_THRESHOLD)
        stats = self.neo4j.get_graph_stats()
        conflicts = self._get_conflicts()
        top_entities = self._get_top_entities()
        sources = self._get_sources()

        # Learning Engine consolidation pass — reinforce aliases, find
        # unlinked co-occurring entity pairs. AliasMemory saves immediately
        # on every write (no flush() method), EntityMemory batches and
        # needs an explicit flush.
        entity_memory = EntityMemory()
        alias_memory = AliasMemory()
        learning_report = self.learning_engine.run_consolidation(
            entity_memory, alias_memory, self.neo4j
        )
        entity_memory.flush()

        digest = self._generate_digest(
            stats, conflicts, stale, top_entities, sources, learning_report, timestamp
        )

        digest_path = self._save_digest(digest, timestamp)

        result = {
            "timestamp": timestamp,
            "stats": stats,
            "decayed_nodes": decayed,
            "stale_entities": len(stale),
            "active_conflicts": len(conflicts),
            "aliases_reinforced": learning_report["aliases_reinforced"],
            "unlinked_candidates": learning_report["total_unlinked_candidates"],
            "digest_path": digest_path
        }

        logger.info(f"Curation complete: {result}")
        return result

    def _decay_confidence(self) -> int:
        with self.neo4j.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity)
                WHERE e.last_updated < timestamp() - 86400000
                AND e.confidence > $threshold
                SET e.confidence = e.confidence * (1 - $decay_rate)
                RETURN count(e) AS decayed
            """, threshold=config.MIN_CONFIDENCE_THRESHOLD,
                decay_rate=config.CONFIDENCE_DECAY_RATE)
            record = result.single()
            count = record["decayed"] if record else 0
            logger.info(f"  Decayed confidence on {count} nodes")
            return count

    def _get_conflicts(self) -> list:
        with self.neo4j.driver.session() as session:
            result = session.run("""
                MATCH (a:Entity)-[r:CONFLICTS_WITH]->(b:Entity)
                RETURN a.name AS entity, a.type AS type,
                       a.source_doc AS doc1, b.source_doc AS doc2,
                       a.confidence AS conf1, b.confidence AS conf2
            """)
            return [dict(record) for record in result]

    def _get_top_entities(self, limit: int = 10) -> list:
        with self.neo4j.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity)
                WHERE e.flagged IS NULL OR e.flagged = false
                RETURN e.name AS name, e.type AS type,
                       e.confidence AS confidence, e.source_doc AS source
                ORDER BY e.confidence DESC
                LIMIT $limit
            """, limit=limit)
            return [dict(record) for record in result]

    def _get_sources(self) -> list:
        with self.neo4j.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity)
                RETURN DISTINCT e.source_doc AS source,
                       count(e) AS entity_count
                ORDER BY entity_count DESC
            """)
            return [dict(record) for record in result]

    def _generate_digest(self, stats: dict, conflicts: list,
                          stale: list, top_entities: list,
                          sources: list, learning_report: dict, timestamp: str) -> str:
        unlinked = learning_report.get("unlinked_candidate_pairs", [])
        type_stats = learning_report.get("entity_type_stats", {})

        context = f"""
KRONOS Knowledge Base Snapshot — {timestamp}

=== GRAPH STATISTICS ===
Total Entities: {stats.get('total_entities', 0)}
Total Relationships: {stats.get('total_relationships', 0)}
Average Confidence: {stats.get('avg_confidence', 0):.2f}

=== SOURCE DOCUMENTS ===
{chr(10).join([f"- {s['source']} ({s['entity_count']} entities)" for s in sources])}

=== ACTIVE CONFLICTS ({len(conflicts)}) ===
{chr(10).join([f"- [{c['type']}] {c['entity']}: {c['doc1']} vs {c['doc2']}" for c in conflicts]) or "None"}

=== STALE/LOW-CONFIDENCE ENTITIES ({len(stale)}) ===
{chr(10).join([f"- {s['e.name']} ({s['e.type']}) confidence:{s['e.confidence']:.2f}" for s in stale]) or "None"}

=== TOP ENTITIES BY CONFIDENCE ===
{chr(10).join([f"- [{e['type']}] {e['name']} (confidence:{e['confidence']:.2f}, source:{e['source']})" for e in top_entities])}

=== LEARNING ENGINE ===
Aliases reinforced this cycle: {learning_report.get('aliases_reinforced', 0)}
Unlinked candidate pairs (co-occur often, no relationship edge yet): {learning_report.get('total_unlinked_candidates', 0)}
{chr(10).join([f"- {p['entity_a']} <-> {p['entity_b']} (seen together {p['count']}x across {len(p['source_docs'])} doc(s))" for p in unlinked[:5]]) or "None"}

=== ENTITY TYPE CONFIDENCE STATS ===
{chr(10).join([f"- {t}: {v['count']} entities, avg confidence {v['avg_confidence']}" for t, v in type_stats.items()]) or "None"}
"""
        try:
            response = self.mistral_client.chat.complete(
                model=config.CURATOR_MODEL,
                messages=[
                    {"role": "system", "content": CURATOR_PROMPT},
                    {"role": "user", "content": context}
                ]
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.warning(f"Mistral digest generation failed: {e}, using Groq fallback")
            from utils.quota import groq_quota
            groq_quota.acquire(agent_name="Curator")
            response = self.groq_client.chat.completions.create(
                model=config.RECONCILER_MODEL,
                messages=[
                    {"role": "system", "content": CURATOR_PROMPT},
                    {"role": "user", "content": context}
                ]
            )
            return response.choices[0].message.content

    def _save_digest(self, digest: str, timestamp: str) -> str:
        import os
        os.makedirs("logs", exist_ok=True)
        filename = f"logs/digest_{timestamp.replace(':', '-').replace(' ', '_')}.md"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# KRONOS Knowledge Digest\n")
            f.write(f"**Generated:** {timestamp}\n\n")
            f.write(digest)
        logger.info(f"  Digest saved to {filename}")
        return filename

    def close(self):
        self.learning_engine.close()
        self.neo4j.close()