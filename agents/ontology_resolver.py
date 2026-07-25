import json
import re
import time
import logging
from pathlib import Path

from utils.atomic_json import atomic_write_json

from agents.ontology_evolution import OntologyEvolution, LOW_CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)

RELATION_HIERARCHY = {
    "STRUCTURAL": {"IS_A", "SUBSET_OF", "PART_OF", "BELONGS_TO", "CONTAINS"},
    "AUTHORSHIP": {"CREATED_BY", "DEVELOPED_BY", "AUTHORED_BY", "EMPLOYED_BY",
                   "WORKED_AT", "CONTRIBUTED_TO", "COLLABORATED_WITH", "MENTORED"},
    "FUNCTIONAL": {"USES", "USED_FOR", "USED_IN", "USED_BY", "USED_WITH",
                   "APPLIES_TO", "APPLICABLE_TO", "ENABLES", "MEASURES",
                   "EVALUATES", "REQUIRES", "CREATES", "GENERATES", "ADDRESSES"},
    "REFERENTIAL": {"REFERENCES", "PRECEDES", "FOLLOWS", "AFFECTS",
                     "INFLUENCES", "TARGETS", "COMPATIBLE_WITH"},
    "SEMANTIC": {"SUPPORTS", "CONTRADICTS", "RELATED_TO", "SIMILAR_TO",
                  "SAME_AS", "DIFFERENT_FROM"},
}

VALID_RELATION_TYPES = set().union(*RELATION_HIERARCHY.values())
RELATION_TO_CATEGORY = {
    rel: category
    for category, rels in RELATION_HIERARCHY.items()
    for rel in rels
}

RELATION_SYNONYMS = {
    "SUBFIELD_OF": "SUBSET_OF",
    "IS_RELATED_TO": "RELATED_TO",
    "ARE_RELATED_TO": "RELATED_TO",
    "PROVIDES_ACCESS_TO": "ENABLES",
    "USED_TO_CONVERT": "USED_FOR",
    "USED_TO_EVALUATE": "USED_FOR",
    "SHOWS_CHANGE": "REFERENCES",
    "SHOWS": "REFERENCES",
    "SUMMARIZES": "REFERENCES",
    "ADDRESS": "ADDRESSES",
    "DOES_NOT_SUPPORT": "CONTRADICTS",
    "DEVELOPED": "DEVELOPED_BY",
    "SELECTED": "USED_FOR",
    "PROVIDES_INFORMATION": "REFERENCES",
    "FORCES": "REQUIRES",
    "RELATES_TO": "RELATED_TO",
    "WRAPPED": "USES",
    "BEHAVES_AS": "SIMILAR_TO",
    "IMPLEMENTED_IN": "USED_IN",
    "DIFFERS_FROM": "DIFFERENT_FROM",
    "INTEGRATES": "USES",
    "INTEGRATES_WITH": "USES",
    "EXPORTS_TO": "USED_FOR",
    "RUNS": "USES",
    "ENHANCES": "AFFECTS",
    "COMMISSIONED": "CREATED_BY",
    "IS_ABOUT": "RELATED_TO",
    "IS_SIMILAR_TO": "SIMILAR_TO",
    "AUTHORED": "AUTHORED_BY",
    "SOLVED_BY": "USED_FOR",
    "ENHANCED_BY": "AFFECTS",
    "OBJECTIVE": "TARGETS",
    "HAS_PHASE": "PART_OF",
    "COMPARISON": "SIMILAR_TO",
    "TOPIC": "RELATED_TO",
    "OPERATES_IN": "USED_IN",
    "IS_PART_OF": "PART_OF",
    "IS_USED_BY": "USED_BY",
    "IS_CHARACTERISTIC_OF": "BELONGS_TO",
    "MEASURED_BY": "MEASURES",
    "HAS": "CONTAINS",
    "MARKS": "RELATED_TO",
    "HANDLED": "USES",
    "DEALS_WITH": "RELATED_TO",
    "COMBINED_WITH": "RELATED_TO",
    "REPRESENTS": "RELATED_TO",
    "INPUT_FOR": "USED_IN",
    "OUTPUT_OF": "GENERATES",
    "INVOLVES": "CONTAINS",
    "WORKED_WITH": "COLLABORATED_WITH",
    "PURSUING": "TARGETS",
    "ASSOCIATED_WITH": "RELATED_TO",
    "OCCURRED_IN": "RELATED_TO",
    "DEDICATED_TO": "RELATED_TO",
    "PRAYED_TO": "RELATED_TO",
}

EXACT_MATCH_CONFIDENCE = 1.0
SYNONYM_MATCH_CONFIDENCE = 0.95
FALLBACK_CONFIDENCE = 0.3
INVALID_CONFIDENCE = 0.0


class OntologyResolver:
    """
    Resolves an arbitrary relation string into a canonical relation type.

    Raw relation -> normalize -> ontology memory (learned, persisted)
                 -> static synonym map -> LLM classifier -> fallback

    On top of this, an OntologyEvolution tracker silently watches for
    relations that keep landing on 'fallback' or low-confidence 'llm'
    classifications. Once a pattern repeats enough, it proposes a
    genuinely new canonical type — pending human approval via
    scripts/review_ontology.py. Approved types are loaded from
    ontology_extensions.json at startup and become first-class 'exact'
    matches from then on. None of this changes resolve()'s behavior for
    unapproved patterns — they keep landing exactly where they did before.
    """

    def __init__(self, mistral_client, memory_file: str = "ontology_memory.json",
                 model: str = "mistral-small-2506",
                 extensions_file: str = "ontology_extensions.json",
                 pending_file: str = "pending_ontology.json"):
        self.mistral_client = mistral_client
        self.memory_file = Path(memory_file)
        self.model = model
        self.memory = {}
        self._load()

        # Instance-level copies so extension loading never mutates the
        # module-level constants shared across instances/processes.
        self.valid_types = set(VALID_RELATION_TYPES)
        self.hierarchy = {k: set(v) for k, v in RELATION_HIERARCHY.items()}
        self.category_map = dict(RELATION_TO_CATEGORY)
        self.synonyms = dict(RELATION_SYNONYMS)

        self.evolution = OntologyEvolution(
            mistral_client, model,
            pending_file=pending_file,
            extensions_file=extensions_file
        )
        self._apply_extensions()

        logger.info(
            f"OntologyResolver initialized with {len(self.memory)} learned mappings, "
            f"{len(self.valid_types) - len(VALID_RELATION_TYPES)} approved extensions (model={self.model})"
        )

    def _apply_extensions(self):
        extensions = self.evolution.load_extensions()
        for canonical, category in extensions.get("new_types", {}).items():
            self.valid_types.add(canonical)
            self.hierarchy.setdefault(category, set()).add(canonical)
            self.category_map[canonical] = category
        for raw, canonical in extensions.get("approved_synonyms", {}).items():
            self.synonyms[raw] = canonical
        if extensions.get("new_types"):
            logger.info(f"  Loaded {len(extensions['new_types'])} approved ontology extension(s): {list(extensions['new_types'].keys())}")

    def _load(self):
        if self.memory_file.exists():
            with open(self.memory_file, "r", encoding="utf-8") as f:
                self.memory = json.load(f)
        else:
            self.memory = {}
            self._save()

    def _save(self):
        atomic_write_json(str(self.memory_file), self.memory)

    @staticmethod
    def _normalize(raw: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw.strip()).upper()
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")
        return cleaned

    def resolve(self, raw: str, context: dict | None = None) -> dict:
        """
        context (optional): {"from": ..., "to": ..., "description": ..., "source_doc": ...}
        Returns: {"canonical": str, "category": str, "status": str, "confidence": float}
        status is one of: "exact", "synonym", "memory", "llm", "fallback", "invalid"
        """
        context = context or {}

        if not raw:
            return {"canonical": None, "category": None, "status": "invalid", "confidence": INVALID_CONFIDENCE}

        cleaned = self._normalize(raw)
        if not cleaned:
            return {"canonical": None, "category": None, "status": "invalid", "confidence": INVALID_CONFIDENCE}

        # 1. Already canonical (includes approved extensions)
        if cleaned in self.valid_types:
            return {
                "canonical": cleaned,
                "category": self.category_map[cleaned],
                "status": "exact",
                "confidence": EXACT_MATCH_CONFIDENCE,
            }

        # 2. Synonym map (includes approved synonyms from prior human review)
        if cleaned in self.synonyms:
            canonical = self.synonyms[cleaned]
            return {
                "canonical": canonical,
                "category": self.category_map.get(canonical, "SEMANTIC"),
                "status": "synonym",
                "confidence": SYNONYM_MATCH_CONFIDENCE,
            }

        # 3. Learned mapping from a previous LLM call
        memory_key = cleaned.lower()
        if memory_key in self.memory:
            entry = self.memory[memory_key]
            return {
                "canonical": entry["canonical"],
                "category": entry["category"],
                "status": "memory",
                "confidence": entry.get("confidence", 0.7),
            }

        # 4. Never seen before — ask the LLM to classify it into the existing set
        llm_result = self._llm_classify(raw, context)
        if llm_result:
            self.memory[memory_key] = {
                "canonical": llm_result["canonical"],
                "category": llm_result["category"],
                "confidence": llm_result.get("confidence", 0.7),
            }
            self._save()
            logger.info(f"  Ontology LLM classified '{raw}' -> '{llm_result['canonical']}' ({llm_result['category']}) — cached for future use")

            result = {
                "canonical": llm_result["canonical"],
                "category": llm_result["category"],
                "status": "llm",
                "confidence": llm_result.get("confidence", 0.7),
            }
            if result["confidence"] < LOW_CONFIDENCE_THRESHOLD:
                self.evolution.record_friction(raw, cleaned, result, context)
            return result

        # 5. LLM couldn't classify confidently — never lose the edge
        result = {
            "canonical": "RELATED_TO",
            "category": "SEMANTIC",
            "status": "fallback",
            "confidence": FALLBACK_CONFIDENCE,
        }
        self.evolution.record_friction(raw, cleaned, result, context)
        return result

    def _llm_classify(self, raw: str, context: dict) -> dict | None:
        categories_desc = "\n".join(
            f"- {cat}: {', '.join(sorted(rels))}" for cat, rels in self.hierarchy.items()
        )

        prompt = f"""A knowledge graph relation type could not be matched automatically: "{raw}"

From entity: {context.get('from', 'unknown')}
To entity: {context.get('to', 'unknown')}
Description: {context.get('description', 'none')}

Classify this relation into EXACTLY ONE of the following canonical relation types, grouped by category:
{categories_desc}

Respond ONLY with valid JSON:
{{"canonical": "ONE_OF_THE_TYPES_ABOVE", "category": "ITS_CATEGORY", "confidence": 0.0-1.0}}"""

        max_attempts = 2
        for attempt in range(max_attempts):
            time.sleep(0.25)
            try:
                response = self.mistral_client.chat.complete(
                   model=self.model,
                    messages=[
                        {"role": "system", "content": "You are an ontology classification engine for a knowledge graph. Always respond with a type from the given list, never invent a new one."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1
                )
                result = json.loads(response.choices[0].message.content)
                canonical = result.get("canonical", "").strip().upper()
                if canonical in self.valid_types:
                    return {
                        "canonical": canonical,
                        "category": self.category_map[canonical],
                        "confidence": result.get("confidence", 0.7)
                    }
                logger.info(f"  Ontology LLM proposed an out-of-set type for '{raw}', discarding")
                return None
            except Exception as e:
                is_rate_limit = "429" in str(e) or "rate_limited" in str(e).lower()
                if is_rate_limit and attempt < max_attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                logger.warning(f"  Ontology LLM classification failed for '{raw}': {e}")
                return None
        return None

    def close(self):
        self.evolution.close()