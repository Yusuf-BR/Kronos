import re
import logging
import json
import ast
import time
from difflib import SequenceMatcher
from collections import defaultdict
from db.neo4j_client import Neo4jClient
from db.qdrant_client import KronosQdrantClient
from core.config import config
from agents.alias_memory import AliasMemory
from agents.ontology_resolver import OntologyResolver
from agents.embedding_cache import EmbeddingCache
from agents.entity_memory import EntityMemory
from agents.learning_engine import LearningEngine
from agents.source_reliability import SourceReliability
from agents.self_evaluation import SelfEvaluator
from mistralai import Mistral
from agents.evidence_engine import build_evidence_record
from utils.acronym import extract_parenthetical

logger = logging.getLogger(__name__)

MIN_FUZZY_MATCH_LENGTH = 4
FUZZY_MATCH_THRESHOLD = 0.87
NEW_ENTITY_RESOLUTION_CONFIDENCE = 1.0
REJECTED_REFEREE_RESOLUTION_CONFIDENCE = 0.75
ACRONYM_MATCH_CONFIDENCE = 0.95

TYPO_MAX_EDIT_DISTANCE = 2
NEGATION_PREFIXES = ("un", "non", "in", "im", "ir", "il", "dis", "anti", "de", "mis")

REQUIRED_ENTITY_KEYS = ("name", "type")
REQUIRED_REL_KEYS = ("from", "from_type", "to", "to_type", "relation")

_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")


def normalize_entity_name(name: str) -> tuple[str, str]:
    display_name = name.strip()
    normalized = name.lower().strip()
    normalized = re.sub(r'\s+', ' ', normalized)
    normalized = re.sub(r'[^\w\s]', '', normalized)
    return normalized, display_name


def fuzzy_match_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


def extract_numbers(name: str) -> set[str]:
    return set(_NUMBER_PATTERN.findall(name))


def has_numeric_conflict(a: str, b: str) -> bool:
    nums_a = extract_numbers(a)
    nums_b = extract_numbers(b)
    if not nums_a or not nums_b:
        return False
    return nums_a != nums_b


def strip_known_negation_prefix(word: str) -> str | None:
    for prefix in NEGATION_PREFIXES:
        if word.startswith(prefix) and len(word) > len(prefix) + 2:
            return word[len(prefix):]
    return None


def is_negation_pair(a: str, b: str) -> bool:
    a_clean = re.sub(r'[^a-z]', '', a.lower())
    b_clean = re.sub(r'[^a-z]', '', b.lower())
    stripped_a = strip_known_negation_prefix(a_clean)
    if stripped_a and stripped_a == b_clean:
        return True
    stripped_b = strip_known_negation_prefix(b_clean)
    if stripped_b and stripped_b == a_clean:
        return True
    return False


def is_cross_domain_mismatch(domain_a: str | None, domain_b: str | None) -> bool:
    if not domain_a or not domain_b:
        return False
    return domain_a.strip().lower() != domain_b.strip().lower()


def is_disqualified_match(a: str, b: str, domain_a: str | None = None, domain_b: str | None = None) -> str | None:
    if is_negation_pair(a, b):
        return "negation pair (opposite meaning)"
    if has_numeric_conflict(a, b):
        return "differing numeric reference"
    if is_cross_domain_mismatch(domain_a, domain_b):
        return f"different domains ('{domain_a}' vs '{domain_b}')"
    return None


def is_safe_fuzzy_match(new_name: str, candidate_name: str) -> bool:
    a, b = new_name.strip(), candidate_name.strip()
    if len(a) < MIN_FUZZY_MATCH_LENGTH or len(b) < MIN_FUZZY_MATCH_LENGTH:
        return a.lower() == b.lower()
    return fuzzy_match_ratio(a, b) >= FUZZY_MATCH_THRESHOLD


def is_typo_candidate(new_name: str, candidate_name: str) -> bool:
    a, b = new_name.strip(), candidate_name.strip()
    if len(a) < MIN_FUZZY_MATCH_LENGTH or len(b) < MIN_FUZZY_MATCH_LENGTH:
        return False
    if a.lower() == b.lower():
        return False
    return levenshtein_distance(a, b) <= TYPO_MAX_EDIT_DISTANCE


def levenshtein_distance(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    if a == b:
        return 0
    if len(a) == 0:
        return len(b)
    if len(b) == 0:
        return len(a)
    previous_row = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        current_row = [i + 1]
        for j, cb in enumerate(b):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (ca != cb)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def combine_confidence(*scores: float) -> float:
    result = 1.0
    for s in scores:
        result *= max(0.0, min(1.0, s if s is not None else 0.0))
    return round(result, 4)


def validate_entity(entity: dict) -> str | None:
    for key in REQUIRED_ENTITY_KEYS:
        if key not in entity or not entity[key]:
            return f"missing or empty '{key}'"
    return None


def validate_relationship(rel: dict) -> str | None:
    for key in REQUIRED_REL_KEYS:
        if key not in rel or not rel[key]:
            return f"missing or empty '{key}'"
    return None


class CodexAgent:
    def __init__(self):
        self.neo4j = Neo4jClient()
        self.qdrant = KronosQdrantClient()
        self.alias_memory = AliasMemory()
        self.embedding_model = self.qdrant.encoder
        self.embedding_cache = EmbeddingCache(
            self.embedding_model,
            model_name=getattr(config, "EMBEDDING_MODEL", "unknown-model")
        )
        self.entity_memory = EntityMemory(neo4j_client=self.neo4j)
        self.learning_engine = LearningEngine()
        self.source_reliability = SourceReliability()
        self.self_evaluator = SelfEvaluator()
        self.mistral_client = Mistral(api_key=config.MISTRAL_API_KEY)
        self.ontology = OntologyResolver(
            self.mistral_client,
            model=getattr(config, "ONTOLOGY_MODEL", "mistral-small-2506")
        )
        self.referee_model = getattr(config, "REFEREE_MODEL", "mistral-small-2506")
        self._pending_cross_domain_links: list[dict] = []
        logger.info("CodexAgent initialized")

    @staticmethod
    def _safe_get_description(record: dict | None) -> str:
        if not record:
            return ""
        props = record.get("properties", {})
        if isinstance(props, str):
            try:
                props = ast.literal_eval(props)
            except (ValueError, SyntaxError):
                return ""
        if isinstance(props, dict):
            return props.get("description", "")
        return ""

    def ingest(self, extracted: dict) -> dict:
        filename = extracted["filename"]
        logger.info(f"Ingesting: {filename}")
        self._pending_cross_domain_links.clear()

        _start_time = time.time()
        self.source_reliability.register_source(filename, extracted.get("quality_score", 1.0))
        _aliases_before = len(self.alias_memory.memory)
        _ontology_before = len(self.ontology.memory)

        if self.qdrant.source_exists(filename):
            logger.info(f"  Existing vectors found for {filename} — cleaning up")
            self.qdrant.delete_by_source(filename)

        entity_count = 0
        relationship_count = 0
        chunk_count = 0
        claims_count = 0
        relationships_rejected = 0
        entities_skipped = 0

        chunk_groups: dict[str, list[dict]] = defaultdict(list)

        for entity in extracted["entities"]:
            problem = validate_entity(entity)
            if problem:
                entities_skipped += 1
                logger.warning(f"  Entity skipped (malformed: {problem}): {entity}")
                continue

            try:
                normalized, display_name = normalize_entity_name(entity["name"])
                entity_type = entity["type"]
                entity_description = entity.get("description", "")
                entity_domain = entity.get("_domain")
                extraction_confidence = entity.get("_extraction_confidence", 1.0)

                # ── Declared-alias pre-normalization ──
                # "Machine Learning (ML)" is the source text itself stating
                # that "ML" abbreviates "Machine Learning" — ground truth,
                # not an inference. Commit it immediately at full confidence
                # rather than waiting on embedding/fuzzy/referee matching,
                # which cannot reliably catch acronym-vs-full-phrase pairs
                # (see utils/acronym.py for why). This also means the base
                # name — not the parenthetical form — is what actually flows
                # into the rest of the pipeline as this entity's name.
                base_name, declared_abbrev = extract_parenthetical(display_name)
                if declared_abbrev:
                    display_name = base_name
                    normalized, _ = normalize_entity_name(base_name)
                    entity["name"] = base_name
                    self.alias_memory.record(declared_abbrev, base_name, 1.0, source="declared_parenthetical")
                    logger.info(f"  Declared alias: '{declared_abbrev}' -> '{base_name}' (from parenthetical form)")

                entity_resolution_confidence = NEW_ENTITY_RESOLUTION_CONFIDENCE

                canonical, alias_confidence = self.alias_memory.get(normalized)
                if canonical:
                    logger.info(f"  Alias match: '{entity['name']}' -> '{canonical}' — using canonical")
                    entity["name"] = canonical
                    entity_resolution_confidence = alias_confidence
                else:
                    entity_embedding_vec = self.embedding_cache.get_or_compute(display_name)
                    resolved = False

                    similar = self.entity_memory.find_similar(entity_embedding_vec, entity_type, threshold=0.85, domain=entity_domain)
                    if not similar:
                        similar = self.qdrant.find_similar_entity_by_embedding(
                            entity_embedding_vec, entity_type, domain=entity_domain, threshold=0.85
                        )

                    # ── Acronym/abbreviation check ──
                    # Runs independently of embedding similarity, which is
                    # unreliable for short-acronym-vs-full-phrase pairs by
                    # construction (e.g. "ML" vs "Machine Learning" — low
                    # cosine similarity AND high edit distance despite
                    # meaning the same thing). Catches standalone "ML"
                    # mentions that never had a "(ML)" form declared anywhere.
                    via_acronym = False
                    if not similar:
                        acronym_match = self.entity_memory.find_acronym_match(display_name, entity_type, domain=entity_domain)
                        if acronym_match:
                            similar = {**acronym_match, "similarity": ACRONYM_MATCH_CONFIDENCE}
                            via_acronym = True

                    if similar:
                        disqualified = is_disqualified_match(
                            display_name, similar["name"],
                            domain_a=entity_domain, domain_b=similar.get("domain")
                        )
                        if disqualified:
                            logger.info(
                                f"  {'Acronym' if via_acronym else 'Embedding'} match REJECTED (structurally disqualified: {disqualified}): "
                                f"'{display_name}' ~ '{similar['name']}' — treating as distinct entity"
                            )
                            if "different domains" in disqualified:
                                self._pending_cross_domain_links.append({
                                    "new_name": display_name,
                                    "new_type": entity_type,
                                    "new_desc": entity_description,
                                    "existing_name": similar["name"],
                                    "existing_type": similar.get("type", entity_type),
                                    "existing_desc": self._safe_get_description(similar),
                                    "new_domain": entity_domain,
                                    "existing_domain": similar.get("domain"),
                                    "source_doc": filename
                                })
                        else:
                            logger.info(f"  {'Acronym' if via_acronym else 'Embedding'} match: '{display_name}' ~ '{similar['name']}' — using canonical")
                            entity["name"] = similar["name"]
                            entity_resolution_confidence = similar.get("similarity", 0.85)
                            self.alias_memory.record(
                                display_name, similar["name"], entity_resolution_confidence,
                                source="acronym" if via_acronym else "embedding"
                            )
                            resolved = True

                    if not resolved:
                        nearest = self.qdrant.find_nearest_entity_candidate(entity_embedding_vec, entity_type, domain=entity_domain)
                        if nearest and is_typo_candidate(display_name, nearest["name"]):
                            disqualified = is_disqualified_match(
                                display_name, nearest["name"],
                                domain_a=entity_domain, domain_b=nearest.get("domain")
                            )
                            if disqualified:
                                logger.info(
                                    f"  Typo candidate REJECTED (structurally disqualified: {disqualified}): "
                                    f"'{display_name}' ~ '{nearest['name']}'"
                                )
                                if "different domains" in disqualified:
                                    self._pending_cross_domain_links.append({
                                        "new_name": display_name,
                                        "new_type": entity_type,
                                        "new_desc": entity_description,
                                        "existing_name": nearest["name"],
                                        "existing_type": nearest.get("type", entity_type),
                                        "existing_desc": self._safe_get_description(nearest),
                                        "new_domain": entity_domain,
                                        "existing_domain": nearest.get("domain"),
                                        "source_doc": filename
                                    })
                            else:
                                referee_result = self._llm_referee(
                                    display_name,
                                    entity_description,
                                    nearest["name"],
                                    self._safe_get_description(nearest)
                                )
                                if referee_result and referee_result.get("confidence", 0) > 0.8 and referee_result.get("canonical_name"):
                                    logger.info(f"  Typo match (referee-confirmed): '{display_name}' ~ '{nearest['name']}' — using canonical")
                                    entity["name"] = referee_result["canonical_name"]
                                    entity_resolution_confidence = referee_result["confidence"]
                                    self.alias_memory.record(
                                        display_name,
                                        referee_result["canonical_name"],
                                        referee_result["confidence"],
                                        source="typo_referee"
                                    )
                                    resolved = True
                                else:
                                    logger.info(f"  Rejected typo candidate: '{display_name}' ~ '{nearest['name']}' — referee not confident, treating as distinct")

                        if not resolved:
                            similar = self.neo4j.find_similar_entity(display_name, entity_type)
                            if similar:
                                disqualified = is_disqualified_match(
                                    display_name, similar["name"],
                                    domain_a=entity_domain, domain_b=similar.get("domain")
                                )
                                if disqualified:
                                    logger.info(
                                        f"  Neo4j match REJECTED (structurally disqualified: {disqualified}): "
                                        f"'{display_name}' ~ '{similar['name']}'"
                                    )
                                    if "different domains" in disqualified:
                                        self._pending_cross_domain_links.append({
                                            "new_name": display_name,
                                            "new_type": entity_type,
                                            "new_desc": entity_description,
                                            "existing_name": similar["name"],
                                            "existing_type": similar.get("type", entity_type),
                                            "existing_desc": self._safe_get_description(similar),
                                            "new_domain": entity_domain,
                                            "existing_domain": similar.get("domain"),
                                            "source_doc": filename
                                        })
                                elif similar["name"] != display_name:
                                    ratio = fuzzy_match_ratio(display_name, similar["name"])
                                    if is_safe_fuzzy_match(display_name, similar["name"]):
                                        logger.info(f"  Fuzzy match: '{display_name}' ~ '{similar['name']}' — using canonical")
                                        entity["name"] = similar["name"]
                                        entity_resolution_confidence = ratio
                                        self.alias_memory.record(
                                            display_name, similar["name"], ratio, source="fuzzy"
                                        )
                                    else:
                                        referee_result = self._llm_referee(
                                            display_name,
                                            entity_description,
                                            similar["name"],
                                            self._safe_get_description(similar)
                                        )
                                        if referee_result and referee_result.get("confidence", 0) > 0.8 and referee_result.get("canonical_name"):
                                            logger.info(f"  LLM Referee match: '{display_name}' ~ '{similar['name']}' — using canonical")
                                            entity["name"] = referee_result["canonical_name"]
                                            entity_resolution_confidence = referee_result["confidence"]
                                            self.alias_memory.record(
                                                display_name,
                                                referee_result["canonical_name"],
                                                referee_result["confidence"],
                                                source="referee"
                                            )
                                        else:
                                            logger.info(f"  Rejected LLM Referee match: '{display_name}' ~ '{similar['name']}' — not confident enough")
                                            entity_resolution_confidence = REJECTED_REFEREE_RESOLUTION_CONFIDENCE
                                else:
                                    logger.info(f"  Exact Neo4j match (same domain): '{display_name}' — using canonical")
                                    entity_resolution_confidence = 1.0

                final_confidence = combine_confidence(extraction_confidence, entity_resolution_confidence)

                evidence = build_evidence_record(
                    source_doc=entity.get("_source_doc", filename),
                    page=entity.get("_page", 0),
                    chunk_id=entity.get("_chunk_id", "unknown"),
                    excerpt=entity.get("_excerpt", ""),
                    confidence=final_confidence
                )

                self.neo4j.create_or_update_entity(
                    name=entity["name"],
                    type=entity_type,
                    source_doc=filename,
                    confidence=final_confidence,
                    properties={
                        "description": entity_description,
                        "extraction_confidence": extraction_confidence,
                        "entity_resolution_confidence": round(entity_resolution_confidence, 4),
                    },
                    evidence=evidence,
                    domain=entity_domain
                )

                entity_embedding = self.embedding_cache.get_or_compute(entity["name"])
                self.qdrant.upsert_entity_embedding(entity["name"], entity_type, entity_embedding, domain=entity_domain)
                self.entity_memory.upsert(
                    canonical_name=entity["name"],
                    entity_type=entity_type,
                    embedding=entity_embedding,
                    description=entity_description,
                    alias=display_name,
                    confidence=final_confidence,
                    domain=entity_domain,
                )

                chunk_id = entity.get("_chunk_id", "unknown")
                chunk_groups[chunk_id].append({"name": entity["name"], "type": entity_type})

                entity_count += 1
            except Exception as e:
                logger.warning(f"  Entity failed [{entity.get('name', '?')}]: {e}")

        for chunk_id, chunk_entities in chunk_groups.items():
            if len(chunk_entities) >= 2:
                self.learning_engine.record_chunk_entities(chunk_entities, filename)

        for rel in extracted["relationships"]:
            problem = validate_relationship(rel)
            if problem:
                relationships_rejected += 1
                logger.warning(f"  Relationship skipped (malformed: {problem}): {rel}")
                continue

            resolution = self.ontology.resolve(
                rel["relation"],
                context={"from": rel["from"], "to": rel["to"], "description": rel.get("description", "")}
            )
            safe_rel_type, status, ontology_confidence = (
                resolution["canonical"], resolution["status"], resolution.get("confidence", 0.5)
            )

            if status == "invalid":
                relationships_rejected += 1
                logger.warning(
                    f"  Relationship rejected [{rel['from']} -> {rel['to']}]: "
                    f"empty/unusable relation type '{rel['relation']}'"
                )
                continue

            if status == "llm":
                logger.info(f"  Relationship type '{rel['relation']}' newly classified as '{safe_rel_type}' ({resolution['category']})")
            elif status == "fallback":
                logger.info(
                    f"  Relationship type '{rel['relation']}' unresolved even by LLM — "
                    f"writing as RELATED_TO [{rel['from']} -> {rel['to']}]"
                )
            elif status == "memory":
                logger.info(f"  Relationship type '{rel['relation']}' resolved from ontology memory -> '{safe_rel_type}'")
            elif status == "synonym":
                logger.info(f"  Relationship type '{rel['relation']}' mapped to '{safe_rel_type}'")

            extraction_confidence = rel.get("_extraction_confidence", 1.0)
            final_confidence = combine_confidence(extraction_confidence, ontology_confidence)

            evidence = build_evidence_record(
                source_doc=rel.get("_source_doc", filename),
                page=rel.get("_page", 0),
                chunk_id=rel.get("_chunk_id", "unknown"),
                excerpt=rel.get("_excerpt", ""),
                confidence=final_confidence
            )
            try:
                self.neo4j.create_relationship(
                    from_name=rel["from"],
                    from_type=rel["from_type"],
                    to_name=rel["to"],
                    to_type=rel["to_type"],
                    rel_type=safe_rel_type,
                    source_doc=filename,
                    confidence=final_confidence,
                    evidence=evidence
                )
                relationship_count += 1
                self.entity_memory.record_relationship(rel["from"], rel["from_type"])
                self.entity_memory.record_relationship(rel["to"], rel["to_type"])
            except Exception as e:
                logger.warning(f"  Relationship failed [{rel['from']} -> {rel['to']}]: {e}")

        for link in self._pending_cross_domain_links:
            cross_link_confidence = 0.4
            evidence = build_evidence_record(
                source_doc=link["source_doc"],
                page=0,
                chunk_id="cross_domain_link",
                excerpt=f"Cross-domain link: '{link['new_name']}' in '{link['new_domain']}' vs '{link['existing_name']}' in '{link['existing_domain']}'",
                confidence=cross_link_confidence
            )
            try:
                self.neo4j.create_relationship(
                    from_name=link["new_name"],
                    from_type=link["new_type"],
                    to_name=link["existing_name"],
                    to_type=link["existing_type"],
                    rel_type="RELATED_TERM_DIFFERENT_DOMAIN",
                    source_doc=link["source_doc"],
                    confidence=cross_link_confidence,
                    evidence=evidence,
                    from_domain=link.get("new_domain"),
                    to_domain=link.get("existing_domain")
                )
                logger.info(f"  Cross-domain link created: '{link['new_name']}' ({link['new_domain']}) ~ '{link['existing_name']}' ({link['existing_domain']})")
            except Exception as e:
                logger.warning(f"  Failed to create cross-domain link: {e}")

        all_vectors = []

        if extracted.get("chunks"):
            all_vectors.extend([
                {
                    "text": c["text"],
                    "source_doc": c["source_doc"],
                    "page": c["page"],
                    "confidence": extracted.get("extraction_confidence", 1.0),
                    "type": "chunk",
                    "domain": extracted.get("domain")
                }
                for c in extracted["chunks"]
            ])
            chunk_count = len(extracted["chunks"])

        if extracted.get("claims"):
            all_vectors.extend([
                {
                    "text": c["text"],
                    "source_doc": c["source_doc"],
                    "page": c["page"],
                    "confidence": extracted.get("extraction_confidence", 1.0),
                    "type": "claim",
                    "domain": extracted.get("domain")
                }
                for c in extracted["claims"]
            ])
            claims_count = len(extracted["claims"])

        all_vectors = [
            v for v in all_vectors
            if isinstance(v.get("text"), str) and v["text"].strip()
        ]
        if all_vectors:
            try:
                self.qdrant.add_chunks(all_vectors)
            except Exception as e:
                logger.error(f"  Qdrant write failed for {filename}: {e}")
                logger.error(f"  DESYNC WARNING: {filename} written to Neo4j but NOT to Qdrant")
                raise

        self.embedding_cache.flush()
        self.entity_memory.flush()
        self.learning_engine.flush()

        result = {
            "filename": filename,
            "entities_written": entity_count,
            "entities_skipped": entities_skipped,
            "relationships_written": relationship_count,
            "relationships_rejected": relationships_rejected,
            "chunks_written": chunk_count,
            "claims_written": claims_count,
            "domain": extracted.get("domain")
        }

        _processing_seconds = time.time() - _start_time
        self.self_evaluator.evaluate(
            filename=filename,
            extracted=extracted,
            ingest_result=result,
            aliases_before=_aliases_before,
            aliases_after=len(self.alias_memory.memory),
            ontology_before=_ontology_before,
            ontology_after=len(self.ontology.memory),
            processing_seconds=_processing_seconds
        )

        logger.info(f"  Done: {result}")
        return result

    def _llm_referee(self, name1: str, desc1: str, name2: str, desc2: str) -> dict | None:
        REFEREE_PROMPT = """
        You are a knowledge referee. Determine if two entity names refer to the same real-world entity.
        Respond ONLY with valid JSON in this exact format:
        {
          "canonical_name": "the most appropriate canonical name",
          "aliases": ["name1", "name2"],
          "confidence": 0.0-1.0,
          "reason": "llm_referee"
        }
        If you cannot confidently determine they are the same entity, respond with:
        {"canonical_name": null, "confidence": 0.0, "reason": "uncertain"}
        """

        max_attempts = 3
        base_delay = 0.25

        for attempt in range(max_attempts):
            time.sleep(base_delay)
            try:
                response = self.mistral_client.chat.complete(
                    model=self.referee_model,
                    messages=[
                        {"role": "system", "content": REFEREE_PROMPT},
                        {"role": "user", "content": f"Name 1: {name1}\nDescription 1: {desc1}\nName 2: {name2}\nDescription 2: {desc2}\nAre these the same real-world entity?"}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1
                )
                return json.loads(response.choices[0].message.content)
            except Exception as e:
                is_rate_limit = "429" in str(e) or "rate_limited" in str(e).lower()
                if is_rate_limit and attempt < max_attempts - 1:
                    backoff = base_delay * (2 ** (attempt + 1))
                    logger.info(f"  Referee rate-limited, retrying in {backoff:.1f}s (attempt {attempt + 1}/{max_attempts})")
                    time.sleep(backoff)
                    continue
                logger.warning(f"LLM referee failed: {e}")
                return None

        return None

    def close(self):
        self.embedding_cache.flush()
        self.entity_memory.flush()
        self.learning_engine.close()
        self.neo4j.close()