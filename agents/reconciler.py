import logging
import json
import re
import numpy as np
from groq import Groq
from mistralai import Mistral
from db.neo4j_client import Neo4jClient
from core.config import config
from utils.quota import groq_quota
from agents.source_reliability import SourceReliability

logger = logging.getLogger(__name__)

CLAIMS_TOP_K = 10
CLAIMS_FALLBACK_TOP_K = 8

RECONCILER_PROMPT = """You are a knowledge reconciliation engine with precise judgment.

Your job is to determine if two sources describing the same entity represent a REAL
CONTRADICTION or are simply COMPLEMENTARY information. This applies across ANY domain
KRONOS may encounter — people, organizations, products, specifications, financial figures,
legal terms, measurements, policies, historical events, or anything else. Do not assume
a particular domain; reason generally from the evidence given.

BASE YOUR JUDGMENT ON THE CLAIMS PROVIDED, NOT JUST THE DESCRIPTION WORDING.
Two entity descriptions can sound generic or similar ("researcher", "product", "policy")
while the underlying claims genuinely conflict on a specific attribute. Always check the
claims list from both sources for concrete conflicting facts before concluding the
sources are merely complementary. If either claims list contains specific numbers, dates,
or values for the same kind of attribute, and those numbers/dates/values differ, that IS
a contradiction — do not downgrade it to complementary just because the general topic
or role described sounds similar.

REAL CONTRADICTION = two sources assert incompatible values for the SAME specific attribute.
Examples (illustrative only — the same pattern applies to any subject matter):
- "has 42 papers" vs "has 31 papers" → same attribute (count), different values
- "based in City A" vs "based in City B" → same attribute (location), different values
- "founded in 2019" vs "founded in 2021" → same attribute (date), different values
- "costs $50" vs "costs $75" → same attribute (price), different values
- "h-index is 24" vs "h-index is 20" → same attribute (metric), different values

EXPLICIT REVISION / CORRECTION SIGNAL — treat as a strong indicator of a real contradiction
whenever a source explicitly states it corrects, updates, revises, supersedes, retracts,
or amends an earlier figure or fact, REGARDLESS OF DOMAIN. When such language is present
and a specific value is being changed, resolve toward the source containing the correction —
KEEP_DOC2 if the newer document contains the correction, KEEP_DOC1 if the older one does —
even if the two descriptions otherwise read as generally compatible. Only mark attributes
that are ACTUALLY being changed as contradicting — an unrelated attribute that stays the
same across both sources is not part of the contradiction.

NOT A CONTRADICTION = different aspects, roles, or contexts of the same entity, where no
specific attribute is actually asserted differently.
Examples:
- "certificate recipient" vs "creator of the system" → different roles, both can be true
- "author of the document" vs "student" → different contexts, both can be true
- "available in Region A" vs "available in Region B" → can both be true simultaneously

Respond ONLY with valid JSON:
{
  "is_contradiction": true or false,
  "conflict_type": "CONTRADICTION|UPDATE|COMPLEMENTARY|DUPLICATE",
  "contradicting_attribute": "the specific attribute that conflicts, or null if complementary",
  "confidence": 0.0-1.0,
  "resolution": "KEEP_DOC1|KEEP_DOC2|KEEP_BOTH|FLAG_FOR_REVIEW|MERGE",
  "reason": "precise explanation citing the SPECIFIC conflicting values found, or why none exist"
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
        self.backend = getattr(config, "RECONCILER_BACKEND", "groq").lower()

        if self.backend == "mistral":
            self.mistral_client = Mistral(api_key=config.MISTRAL_API_KEY)
            logger.info(f"ReconcilerAgent initialized (backend=mistral, model={config.RECONCILER_MISTRAL_MODEL})")
        else:
            self.groq_client = Groq(api_key=config.GROQ_API_KEY)
            logger.info(f"ReconcilerAgent initialized (backend=groq, model={config.RECONCILER_MODEL})")

        self.neo4j = Neo4jClient()
        self.source_reliability = SourceReliability()

    def reconcile(self, extracted: dict) -> dict:
        filename = extracted["filename"]
        logger.info(f"Reconciling: {filename}")

        # The document currently being ingested hasn't been written to
        # Qdrant yet (Codex runs after this) — so its claims can't be
        # queried there. Encode them ONCE here, up front, so ranking
        # against each entity later is just a cosine lookup instead of
        # re-embedding hundreds of claims per entity on large documents.
        new_doc_claims = extracted.get("claims", [])
        new_doc_claim_vectors = self._encode_claims(new_doc_claims)

        conflicts_found = []
        conflicts_resolved = []
        conflicts_flagged = []

        for entity in extracted["entities"]:
            existing = self.neo4j.find_conflicting_entities(
                name=entity["name"],
                type=entity["type"]
            )

            existing = [e for e in existing if e["e.source_doc"] != filename]

            if not existing:
                continue

            for existing_entity in existing:
                conflict = self._detect_conflict(entity, existing_entity, filename)
                if not conflict:
                    continue

                resolution = self._resolve_conflict(
                    conflict, new_doc_claims, new_doc_claim_vectors
                )

                logger.info(
                    f"  Reconciler reasoning for [{entity['name']}]: {resolution.get('reason', 'n/a')} "
                    f"(is_contradiction={resolution.get('is_contradiction')}, "
                    f"confidence={resolution.get('confidence')}, "
                    f"resolution={resolution.get('resolution')})"
                )

                if not resolution.get("is_contradiction", False):
                    if resolution.get("_status") == "failed":
                        logger.warning(f"  UNVERIFIED — could not check [{entity['name']}], flagging for later re-check")
                        conflicts_flagged.append({**conflict, "resolution": resolution, "unverified": True})
                        self._create_conflict_edge(entity, existing_entity, resolution)
                    else:
                        logger.info(f"  Complementary info for [{entity['name']}] — skipping conflict edge")
                    continue

                conflicts_found.append(conflict)

                if resolution["resolution"] in ("KEEP_BOTH", "FLAG_FOR_REVIEW", "MERGE"):
                    self._create_conflict_edge(entity, existing_entity, resolution)
                    conflicts_flagged.append({**conflict, "resolution": resolution})
                    logger.warning(f"  Real conflict flagged: [{entity['name']}] — {resolution.get('contradicting_attribute', 'unknown attribute')}")
                elif resolution["resolution"] == "KEEP_DOC2":
                    self._apply_resolution(entity, existing_entity, resolution, filename)
                    self.source_reliability.record_resolution(
                        winner_doc=filename, loser_doc=existing_entity["e.source_doc"]
                    )
                    conflicts_resolved.append({**conflict, "resolution": resolution})
                    logger.info(f"  Resolved: [{entity['name']}] — new doc wins on {resolution.get('contradicting_attribute')}")
                elif resolution["resolution"] == "KEEP_DOC1":
                    self.source_reliability.record_resolution(
                        winner_doc=existing_entity["e.source_doc"], loser_doc=filename
                    )
                    logger.info(f"  Resolved: [{entity['name']}] — existing doc wins on {resolution.get('contradicting_attribute')}")
                    conflicts_resolved.append({**conflict, "resolution": resolution})

        result = {
            "filename": filename,
            "conflicts_found": len(conflicts_found),
            "conflicts_resolved": len(conflicts_resolved),
            "conflicts_flagged": len(conflicts_flagged),
            "unverified_count": sum(1 for c in conflicts_flagged if c.get("unverified")),
            "details": conflicts_found
        }
        logger.info(f"  Reconciliation done: {result}")
        return result

    def _get_encoder(self):
        if not hasattr(self, '_qdrant'):
            from db.qdrant_client import KronosQdrantClient
            self._qdrant = KronosQdrantClient()
        return self._qdrant.encoder

    def _encode_claims(self, claims: list[dict]):
        """Batch-encode all of a document's claims once. Returns None if
        there are no claims, so downstream ranking can skip cleanly."""
        if not claims:
            return None
        encoder = self._get_encoder()
        texts = [c["text"] for c in claims]
        return encoder.encode(texts)

    def _rank_new_doc_claims(self, entity_name: str, claims: list[dict],
                              vectors, top_k: int = CLAIMS_TOP_K) -> str:
        """
        Ranks the NEW document's claims (already encoded once for the
        whole document in reconcile()) by similarity to entity_name.
        Mirrors what a Qdrant query would return once persisted, without
        needing this document to be written to Qdrant first — and stays
        cheap even on a 90-page Tier 3 document with hundreds of claims,
        since encoding happened once, not once per entity comparison.
        """
        if not claims or vectors is None:
            return "No claims found"

        encoder = self._get_encoder()
        query_vec = encoder.encode(entity_name)

        def cosine(a, b):
            return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))

        scored = sorted(
            zip(claims, vectors),
            key=lambda cv: cosine(query_vec, cv[1]),
            reverse=True
        )
        top = [c["text"] for c, _ in scored[:top_k]]
        return "\n".join([f"- {t}" for t in top]) or "No claims found"

    def _detect_conflict(self, new_entity: dict, existing_entity: dict, new_doc: str) -> dict | None:
        """
        Builds a comparison candidate whenever the same entity appears
        across two different documents. Does NOT gate on whether the two
        descriptions look similar — a generic, near-identical description
        can still hide a real contradiction in the underlying claims. The
        contradiction-vs-complementary judgment is left entirely to the
        LLM in _resolve_conflict, which sees the full claims for both
        sources. Only skipped when neither side has ANY description at
        all — nothing meaningful to compare or show the model.
        """
        new_desc = new_entity.get("description", "").strip()
        existing_desc = str(existing_entity.get("e.properties", "")).strip()

        if "description" in existing_desc.lower():
            match = re.search(r"'description':\s*'([^']*)'", existing_desc)
            if match:
                existing_desc = match.group(1).strip()

        if not new_desc and not existing_desc:
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
        """
        Pull relevant claims/chunks from Qdrant for the EXISTING (already
        ingested in a previous run) side of a comparison only. The NEW
        document being reconciled right now is handled separately by
        _rank_new_doc_claims, since it isn't in Qdrant yet.
        """
        try:
            encoder = self._get_encoder()
            results = self._qdrant.search(
                query=entity_name,
                top_k=CLAIMS_TOP_K,
                source_doc=source_doc,
                type_filter="claim"
            )
            if not results:
                results = self._qdrant.search(
                    query=entity_name,
                    top_k=CLAIMS_FALLBACK_TOP_K,
                    source_doc=source_doc
                )
            return "\n".join([f"- {r['text']}" for r in results]) or "No claims found"
        except Exception as e:
            logger.warning(f"Could not fetch claims for {entity_name}: {e}")
            return "Claims unavailable"

    def _build_reconcile_prompt(self, conflict: dict, claims_doc1: str, claims_doc2: str) -> str:
        return f"""
Entity: {conflict['entity_name']} (type: {conflict['entity_type']})

Source 1 — {conflict['existing_doc']}:
Description: "{conflict['existing_description']}"
Relevant claims:
{claims_doc1}

Source 2 — {conflict['new_doc']}:
Description: "{conflict['new_description']}"
Relevant claims:
{claims_doc2}

Compare ALL the claims above carefully, attribute by attribute — not just the two
description lines. List out, mentally, every specific number/date/value each source
states, and check whether the SAME kind of attribute has a DIFFERENT value across the
two sources. Pay particular attention to any claim that explicitly states it corrects,
updates, revises, or supersedes an earlier figure or statement.
"""

    def _resolve_conflict(self, conflict: dict, new_doc_claims: list[dict], new_doc_claim_vectors) -> dict:
        claims_doc1 = self._get_claims_for_entity(
            conflict["entity_name"], conflict["existing_doc"]
        )
        claims_doc2 = self._rank_new_doc_claims(
            conflict["entity_name"], new_doc_claims, new_doc_claim_vectors
        )
        prompt = self._build_reconcile_prompt(conflict, claims_doc1, claims_doc2)

        try:
            if self.backend == "mistral":
                raw = self._call_mistral(prompt)
            else:
                raw = self._call_groq(prompt)

            result = json.loads(raw)
            result["_status"] = "checked"
            return result

        except Exception as e:
            logger.warning(f"  Reconciliation call failed for [{conflict['entity_name']}]: {e}")
            return {
                "is_contradiction": False,
                "conflict_type": "UNVERIFIED",
                "resolution": "FLAG_FOR_REVIEW",
                "reason": f"LLM call failed, could not check for contradiction: {e}",
                "confidence": 0.0,
                "contradicting_attribute": None,
                "_status": "failed"
            }

    def _call_groq(self, prompt: str) -> str:
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
        return response.choices[0].message.content

    def _call_mistral(self, prompt: str) -> str:
        response = self.mistral_client.chat.complete(
            model=config.RECONCILER_MISTRAL_MODEL,
            messages=[
                {"role": "system", "content": RECONCILER_PROMPT},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        return response.choices[0].message.content

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