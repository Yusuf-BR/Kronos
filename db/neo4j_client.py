from neo4j import GraphDatabase
from core.config import config
import logging

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
            session.run("""
                CREATE CONSTRAINT entity_unique IF NOT EXISTS
                FOR (e:Entity) REQUIRE (e.name, e.type) IS UNIQUE
            """)
            session.run("""
                CREATE INDEX entity_confidence IF NOT EXISTS
                FOR (e:Entity) ON (e.confidence)
            """)
            session.run("""
                CREATE INDEX entity_timestamp IF NOT EXISTS
                FOR (e:Entity) ON (e.last_updated)
            """)
            logger.info("Neo4j schema ready")

    def create_or_update_entity(self, name: str, type: str,
                                 source_doc: str, confidence: float,
                                 properties: dict = {}):
        with self.driver.session() as session:
            session.run("""
                MERGE (e:Entity {name: $name, type: $type})
                SET e.confidence = $confidence,
                    e.source_doc = $source_doc,
                    e.last_updated = timestamp(),
                    e.properties = $properties
            """, name=name, type=type, confidence=confidence,
                source_doc=source_doc, properties=str(properties))

    def create_relationship(self, from_name: str, from_type: str,
                             to_name: str, to_type: str,
                             rel_type: str, source_doc: str,
                             confidence: float):
        with self.driver.session() as session:
            session.run(f"""
                MATCH (a:Entity {{name: $from_name, type: $from_type}})
                MATCH (b:Entity {{name: $to_name, type: $to_type}})
                MERGE (a)-[r:{rel_type}]->(b)
                SET r.source_doc = $source_doc,
                    r.confidence = $confidence,
                    r.created_at = timestamp()
            """, from_name=from_name, from_type=from_type,
                to_name=to_name, to_type=to_type,
                source_doc=source_doc, confidence=confidence)

    def find_conflicting_entities(self, name: str, type: str):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity {name: $name, type: $type})
                RETURN e.name, e.type, e.source_doc,
                       e.confidence, e.properties
            """, name=name, type=type)
            return [dict(record) for record in result]

    def find_similar_entity(self, name: str, type: str) -> dict | None:
        """Find existing entity with similar name — catches aliases and prefixes."""
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
                       e.confidence AS confidence
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