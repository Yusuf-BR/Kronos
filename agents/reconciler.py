import logging
import json
from groq import Groq
from db.neo4j_client import Neo4jClient
from core.config import config
from utils.quota import groq_quota

logger = logging.getLogger(__name__)

RECONCILER_PROMPT = """You are a knowledge reconciliation engine with precise judgment.

Your job is to determine if two descriptions of the same entity represent a REAL CONTRADICTION or are simply COMPLEMENTARY information.

REAL CONTRADICTION = two sources assert incompatible values for the SAME specific attribute.
Examples of real contradictions:
- "has 42 papers" vs "has 31 papers" → same attribute (paper count), different values
- "works at University of Tunis" vs "works at University of Carthage" → same attribute (employer), different values  
- "founded in 2019" vs "founded in 2021" → same attribute (founding year), different values
- "h-index is 18" vs "h-index is 24" → same attribute (h-index), different values

NOT A CONTRADICTION = different aspects, roles, or contexts of the same entity.
Examples of complementary information:
- "certificate recipient" vs "creator of the system" → different roles, both can be true
- "author of the document" vs "student" → different contexts, both can be true
- "researcher" vs "professor" → different titles, can both apply
- "born in Tunisia" vs "works in France" → different attributes entirely

Respond ONLY with valid JSON:
{
  "is_contradiction": true or false,
  "conflict_type": "CONTRADICTION|UPDATE|COMPLEMENTARY|DUPLICATE",
  "contradicting_attribute": "the specific attribute that conflicts, or null if complementary",
  "confidence": 0.0-1.0,
  "resolution": "KEEP_DOC1|KEEP_DOC2|KEEP_BOTH|FLAG_FOR_REVIEW|MERGE",
  "reason": "precise explanation of why this is or isn't a contradiction"
}

Conflict types:
- CONTRADICTION: directly incompatible values for same attribute
- UPDATE: newer document likely supersedes older (same attribute, plausible change over time)
- COMPLEMENTARY: different aspects of same entity, both true simultaneously
- DUPLICATE: same information expressed differently

Resolution options:
- KEEP_DOC1: first document is more credible for this attribute
- KEEP_DOC2: second document is more credible for this attribute
- KEEP_BOTH: both are valid (use for COMPLEMENTARY and DUPLICATE)
- FLAG_FOR_REVIEW: genuine conflict requiring human judgment
- MERGE: combine both descriptions as they are additive
"""


class ReconcilerAgent:
    def __init__(self):
        self.groq_client = Groq(api_key=config.GROQ_API_KEY)
        self.neo4j = Neo4jClient()
        logger.info("ReconcilerAgent initialized")

    def reconcile(self, extracted: dict) -> dict:
        filename = extracted["filename"]
        logger.info(f"Reconciling: {filename}")

        conflicts_found = []
        conflicts_resolved = []
        conflicts_flagged = []

        for entity in extracted["entities"]:
            existing = self.neo4j.find_conflicting_entities(
                name=entity["name"],
                type=entity["type"]
            )

            # Filter out same document
            existing = [e for e in existing if e["e.source_doc"] != filename]

            if not existing:
                continue

            for existing_entity in existing:
                conflict = self._detect_conflict(entity, existing_entity, filename)
                if not conflict:
                    continue

                resolution = self._resolve_conflict(conflict)

                # Only act on real contradictions
                if not resolution.get("is_contradiction", False):
                    logger.info(f"  Complementary info for [{entity['name']}] — skipping conflict edge")
                    continue

                conflicts_found.append(conflict)

                if resolution["resolution"] in ("KEEP_BOTH", "FLAG_FOR_REVIEW", "MERGE"):
                    self._create_conflict_edge(entity, existing_entity, resolution)
                    conflicts_flagged.append({**conflict, "resolution": resolution})
                    logger.warning(f"  Real conflict flagged: [{entity['name']}] — {resolution.get('contradicting_attribute', 'unknown attribute')}")
                elif resolution["resolution"] == "KEEP_DOC2":
                    self._apply_resolution(entity, existing_entity, resolution, filename)
                    conflicts_resolved.append({**conflict, "resolution": resolution})
                    logger.info(f"  Resolved: [{entity['name']}] — new doc wins on {resolution.get('contradicting_attribute')}")
                elif resolution["resolution"] == "KEEP_DOC1":
                    logger.info(f"  Resolved: [{entity['name']}] — existing doc wins on {resolution.get('contradicting_attribute')}")
                    conflicts_resolved.append({**conflict, "resolution": resolution})

        result = {
            "filename": filename,
            "conflicts_found": len(conflicts_found),
            "conflicts_resolved": len(conflicts_resolved),
            "conflicts_flagged": len(conflicts_flagged),
            "details": conflicts_found
        }
        logger.info(f"  Reconciliation done: {result}")
        return result

    def _detect_conflict(self, new_entity: dict, existing_entity: dict, new_doc: str) -> dict | None:
        new_desc = new_entity.get("description", "").lower().strip()
        existing_desc = str(existing_entity.get("e.properties", "")).lower().strip()

        if "description" in existing_desc:
            import re
            match = re.search(r"'description':\s*'([^']*)'", existing_desc)
            if match:
                existing_desc = match.group(1).lower().strip()

        if not new_desc or not existing_desc:
            return None
        if new_desc == existing_desc:
            return None

        return {
            "entity_name": new_entity["name"],
            "entity_type": new_entity["type"],
            "new_doc": new_doc,
            "new_description": new_entity.get("description", ""),
            "existing_doc": existing_entity.get("e.source_doc", ""),
            "existing_description": existing_desc
        }

    def _get_claims_for_entity(self, entity_name: str, source_doc: str) -> str:
        """Pull relevant claims from Qdrant for this entity in this document."""
        try:
            from db.qdrant_client import KronosQdrantClient
            if not hasattr(self, '_qdrant'):
                self._qdrant = KronosQdrantClient()
            results = self._qdrant.search(
                query=entity_name,
                top_k=5,
                source_doc=source_doc,
                type_filter="claim"
            )
            if not results:
                results = self._qdrant.search(
                    query=entity_name,
                    top_k=3,
                    source_doc=source_doc
                )
            return "\n".join([f"- {r['text']}" for r in results]) or "No claims found"
        except Exception as e:
            logger.warning(f"Could not fetch claims for {entity_name}: {e}")
            return "Claims unavailable"

    def _resolve_conflict(self, conflict: dict) -> dict:
        """Use Groq with full claim context to reason about contradictions."""
        # Fetch actual claims from both documents
        claims_doc1 = self._get_claims_for_entity(
            conflict["entity_name"], conflict["existing_doc"]
        )
        claims_doc2 = self._get_claims_for_entity(
            conflict["entity_name"], conflict["new_doc"]
        )

        prompt = f"""
Entity: {conflict['entity_name']} (type: {conflict['entity_type']})

Source 1 — {conflict['existing_doc']}:
Description: "{conflict['existing_description']}"
Relevant claims:
{claims_doc1}

Source 2 — {conflict['new_doc']}:
Description: "{conflict['new_description']}"
Relevant claims:
{claims_doc2}

Compare ALL the claims above. Are any specific facts directly contradictory between the two sources?
"""
        try:
            groq_quota.acquire(agent_name="Reconciler")
            response = self.groq_client.chat.completions.create(
                model=config.RECONCILER_MODEL,
                messages=[
                    {"role": "system", "content": RECONCILER_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            result = json.loads(response.choices[0].message.content)
            return result
        except Exception as e:
            logger.warning(f"  Groq reconciliation failed: {e}")
            return {
                "is_contradiction": False,
                "conflict_type": "AMBIGUOUS",
                "resolution": "FLAG_FOR_REVIEW",
                "reason": "Auto-resolution failed",
                "confidence": 0.5,
                "contradicting_attribute": None
            }

    def _create_conflict_edge(self, new_entity: dict, existing_entity: dict, resolution: dict):
        try:
            self.neo4j.create_relationship(
                from_name=new_entity["name"],
                from_type=new_entity["type"],
                to_name=existing_entity["e.name"],
                to_type=existing_entity["e.type"],
                rel_type="CONFLICTS_WITH",
                source_doc=f"{new_entity.get('source_doc', '')} vs {existing_entity.get('e.source_doc', '')}",
                confidence=0.5
            )
            attr = resolution.get("contradicting_attribute", "unknown")
            logger.info(f"  Created CONFLICTS_WITH edge for [{new_entity['name']}] on attribute: {attr}")
        except Exception as e:
            logger.warning(f"  Failed to create conflict edge: {e}")

    def _flag_entity(self, name: str, type: str):
        with self.neo4j.driver.session() as session:
            session.run("""
                MATCH (e:Entity {name: $name, type: $type})
                SET e.flagged = true, e.confidence = e.confidence * 0.7
            """, name=name, type=type)
        logger.info(f"  Flagged entity [{name}] for review")

    def _apply_resolution(self, new_entity: dict, existing_entity: dict,
                          resolution: dict, filename: str):
        if resolution["resolution"] == "KEEP_DOC2":
            with self.neo4j.driver.session() as session:
                session.run("""
                    MATCH (e:Entity {name: $name, type: $type, source_doc: $old_doc})
                    SET e.confidence = e.confidence * 0.6,
                        e.superseded_by = $new_doc
                """, name=new_entity["name"], type=new_entity["type"],
                    old_doc=existing_entity["e.source_doc"], new_doc=filename)
        elif resolution["resolution"] == "KEEP_DOC1":
            logger.info(f"  Existing entity [{new_entity['name']}] is more credible — keeping original")

    def close(self):
        self.neo4j.close()