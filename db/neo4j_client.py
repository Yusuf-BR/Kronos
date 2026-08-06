from neo4j import GraphDatabase
from core.config import config
import logging
import re

logger = logging.getLogger(__name__)

class Neo4jClient:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
        )

    def close(self):
        self.driver.close()

    def verify_connection(self):
        with self.driver.session() as session:
            result = session.run("RETURN 1 AS ok")
            return result.single()["ok"] == 1

    def setup_schema(self):
        with self.driver.session() as session:
            session.run("DROP CONSTRAINT entity_unique IF EXISTS")
            session.run("""
                CREATE INDEX entity_confidence IF NOT EXISTS
                FOR (e:Entity) ON (e.confidence)
            """)
            session.run("""
                CREATE INDEX entity_timestamp IF NOT EXISTS
                FOR (e:Entity) ON (e.last_updated)
            """)
            session.run("""
                CREATE INDEX entity_domain IF NOT EXISTS
                FOR (e:Entity) ON (e.domain)
            """)
            logger.info("Neo4j schema ready")

    def get_all_entities(self, limit: int = 10000) -> list[dict]:
        """
        Return all entities with properties needed to hydrate EntityMemory.
        Called on startup when entity_memory.json is missing or corrupted.
        """
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity)
                RETURN e.name AS canonical, e.type AS type, e.domain AS domain,
                       e.confidence AS confidence, e.properties AS properties,
                       e.evidence AS evidence
                LIMIT $limit
            """, limit=limit)
            return [dict(record) for record in result]

    def create_or_update_entity(self, name: str, type: str,
                                 source_doc: str, confidence: float,
                                 properties: dict = {}, evidence: dict = None,
                                 domain: str | None = None):
        with self.driver.session() as session:
            if domain:
                existing = session.run("""
                    MATCH (e:Entity {name: $name, type: $type, domain: $domain})
                    RETURN e.confidence AS confidence, e.evidence AS evidence
                """, name=name, type=type, domain=domain).single()
            else:
                existing = session.run("""
                    MATCH (e:Entity {name: $name, type: $type})
                    WHERE e.domain IS NULL
                    RETURN e.confidence AS confidence, e.evidence AS evidence
                """, name=name, type=type).single()

            final_confidence = confidence
            merged_evidence_json = None

            if evidence:
                from agents.evidence_engine import merge_evidence, apply_reinforcement
                if existing:
                    existing_conf = existing["confidence"] if existing["confidence"] is not None else confidence
                    merged_evidence_json, is_new, bump = merge_evidence(existing["evidence"], evidence)
                    final_confidence = apply_reinforcement(existing_conf, bump) if is_new else existing_conf
                else:
                    merged_evidence_json, _, _ = merge_evidence(None, evidence)

            if domain:
                label = self._safe_label(domain)
                session.run(f"""
                    MERGE (e:Entity {{name: $name, type: $type, domain: $domain}})
                    SET e.confidence = $confidence,
                        e.source_doc = $source_doc,
                        e.last_updated = timestamp(),
                        e.properties = $properties,
                        e.evidence = $evidence
                    SET e:`{label}`
                """, name=name, type=type, domain=domain, confidence=final_confidence,
                    source_doc=source_doc, properties=str(properties),
                    evidence=merged_evidence_json)
            else:
                session.run("""
                    MERGE (e:Entity {name: $name, type: $type})
                    WHERE e.domain IS NULL
                    SET e.confidence = $confidence,
                        e.source_doc = $source_doc,
                        e.last_updated = timestamp(),
                        e.properties = $properties,
                        e.evidence = $evidence
                """, name=name, type=type, confidence=final_confidence,
                    source_doc=source_doc, properties=str(properties),
                    evidence=merged_evidence_json)

    def create_relationship(self, from_name: str, from_type: str,
                             to_name: str, to_type: str,
                             rel_type: str, source_doc: str,
                             confidence: float, evidence: dict = None,
                             from_domain: str | None = None,
                             to_domain: str | None = None):
        with self.driver.session() as session:
            from_match = f"a:Entity {{name: $from_name, type: $from_type{', domain: $from_domain' if from_domain else ''}}}"
            to_match = f"b:Entity {{name: $to_name, type: $to_type{', domain: $to_domain' if to_domain else ''}}}"

            existing = session.run(f"""
                MATCH ({from_match})
                MATCH ({to_match})
                MERGE (a)-[r:{rel_type}]->(b)
                RETURN r.confidence AS confidence, r.evidence AS evidence
            """, from_name=from_name, from_type=from_type, from_domain=from_domain,
                to_name=to_name, to_type=to_type, to_domain=to_domain).single()

            final_confidence = confidence
            merged_evidence_json = None

            if evidence:
                from agents.evidence_engine import merge_evidence, apply_reinforcement
                if existing:
                    existing_conf = existing["confidence"] if existing["confidence"] is not None else confidence
                    merged_evidence_json, is_new, bump = merge_evidence(existing["evidence"], evidence)
                    final_confidence = apply_reinforcement(existing_conf, bump) if is_new else existing_conf
                    if is_new:
                        logger.info(f"  Relationship reinforced [{from_name} -[{rel_type}]-> {to_name}]: {existing_conf} -> {final_confidence}")
                else:
                    merged_evidence_json, _, _ = merge_evidence(None, evidence)

            session.run(f"""
                MATCH ({from_match})
                MATCH ({to_match})
                MERGE (a)-[r:{rel_type}]->(b)
                SET r.source_doc = $source_doc,
                    r.confidence = $confidence,
                    r.evidence = $evidence,
                    r.last_updated = timestamp(),
                    r.created_at = coalesce(r.created_at, timestamp())
            """, from_name=from_name, from_type=from_type, from_domain=from_domain,
                to_name=to_name, to_type=to_type, to_domain=to_domain,
                source_doc=source_doc, confidence=final_confidence,
                evidence=merged_evidence_json)

    def find_conflicting_entities(self, name: str, type: str):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity {name: $name, type: $type})
                RETURN e.name, e.type, e.source_doc,
                       e.confidence, e.properties
            """, name=name, type=type)
            return [dict(record) for record in result]

    def find_similar_entity(self, name: str, type: str) -> dict | None:
        clean_name = name.lower()
        for prefix in ["dr.", "dr ", "prof.", "prof ", "mr.", "mr ", "ms.", "ms ", "mrs.", "mrs "]:
            clean_name = clean_name.replace(prefix, "").strip()

        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity {type: $type})
                WHERE toLower(e.name) CONTAINS $clean_name
                   OR $clean_name CONTAINS toLower(e.name)
                RETURN e.name AS name, e.type AS type,
                       e.source_doc AS source_doc,
                       e.confidence AS confidence,
                       e.domain AS domain,
                       e.properties AS properties
                LIMIT 1
            """, type=type, clean_name=clean_name)
        record = result.single()
        return dict(record) if record else None

    def get_stale_entities(self, min_confidence: float):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity)
                WHERE e.confidence < $min_confidence
                RETURN e.name, e.type, e.source_doc, e.confidence
                ORDER BY e.confidence ASC
            """, min_confidence=min_confidence)
            return [dict(record) for record in result]

    def get_graph_stats(self):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity)
                WITH count(e) AS total_entities,
                     avg(e.confidence) AS avg_confidence
                MATCH ()-[r]->()
                RETURN total_entities, avg_confidence, count(r) AS total_relationships
            """)
            return dict(result.single())

    @staticmethod
    def _safe_label(domain: str | None) -> str:
        if not domain:
            return "General"
        return re.sub(r'[^A-Za-z0-9]', '', domain.title()) or "General"

    def tag_entity_domain(self, name: str, type: str, domain: str):
        label = self._safe_label(domain)
        with self.driver.session() as session:
            session.run(f"""
                MATCH (e:Entity {{name: $name, type: $type}})
                SET e.domain = $domain
                SET e:`{label}`
            """, name=name, type=type, domain=domain)

    def get_domain_stats(self):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity)
                WHERE e.domain IS NOT NULL
                RETURN e.domain AS domain, count(e) AS entity_count
                ORDER BY entity_count DESC
            """)
            return [dict(record) for record in result]

    def get_entity_by_name(self, name: str, type: str | None = None) -> dict | None:
        """Exact lookup — used by the entity linker once a canonical name is known."""
        with self.driver.session() as session:
            if type:
                result = session.run("""
                    MATCH (e:Entity {name: $name, type: $type})
                    RETURN e.name AS name, e.type AS type, e.domain AS domain,
                           e.confidence AS confidence, e.properties AS properties
                    LIMIT 1
                """, name=name, type=type)
            else:
                result = session.run("""
                    MATCH (e:Entity {name: $name})
                    RETURN e.name AS name, e.type AS type, e.domain AS domain,
                           e.confidence AS confidence, e.properties AS properties
                    LIMIT 1
                """, name=name)
            record = result.single()
            return dict(record) if record else None

    def get_neighbors(self, name: str, type: str | None = None, max_hops: int = 2,
                       min_confidence: float = 0.0, limit: int = 50,
                       exclude_relation_types: list[str] | None = None) -> list[dict]:
        """
        Walk up to max_hops from a named entity, in either direction, along
        edges whose confidence clears min_confidence. Returns each reachable
        entity with the relation chain that connects it and a path_confidence
        (product of edge confidences — decays fast on purpose).

        exclude_relation_types: canonical relation types to skip entirely
        (e.g. REFERENTIAL-category ones) — see get_relation_types_by_category.
        """
        type_clause = ", type: $type" if type else ""
        exclude = exclude_relation_types or []
        with self.driver.session() as session:
            result = session.run(f"""
                MATCH (start:Entity {{name: $name{type_clause}}})
                MATCH path = (start)-[rels*1..{max_hops}]-(end:Entity)
                WHERE start <> end
                  AND ALL(r IN rels WHERE r.confidence >= $min_confidence
                                     AND NOT type(r) IN $exclude)
                WITH end, rels,
                     reduce(c = 1.0, r IN rels | c * r.confidence) AS path_confidence
                RETURN DISTINCT end.name AS name, end.type AS type, end.domain AS domain,
                       [r IN rels | type(r)] AS relation_chain,
                       path_confidence,
                       size(rels) AS hops
                ORDER BY path_confidence DESC
                LIMIT $limit
            """, name=name, type=type, min_confidence=min_confidence, limit=limit, exclude=exclude)
            return [dict(record) for record in result]

    def get_relation_types_by_category(self, exclude_categories: list[str]) -> list[str]:
        """
        Reads ontology_memory.json and returns every canonical relation type
        whose category is in exclude_categories.
        """
        import json
        with open("ontology_memory.json") as f:  # adjust path to wherever it actually lives
            ontology = json.load(f)
        return sorted({
            entry["canonical"] for entry in ontology.values()
            if entry.get("category") in exclude_categories
        })