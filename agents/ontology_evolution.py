import json
import time
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

# A relation only becomes an expansion candidate once it's shown up this
# many times without fitting cleanly into the existing ontology — one
# occurrence is noise, repeated occurrence is a pattern.
FRICTION_MIN_COUNT = 4

# "llm" classifications below this confidence count as friction too —
# the LLM technically picked something from the existing set, but wasn't
# confident, which usually means it was forced into the closest-available
# bucket rather than the correct one.
LOW_CONFIDENCE_THRESHOLD = 0.65

MAX_EVIDENCE_PER_ENTRY = 5
MAX_RAW_EXAMPLES = 5


class OntologyEvolution:
    """
    Tracks relation types that keep failing to fit the existing ontology
    cleanly. Once a pattern has enough repeated evidence, asks the LLM to
    propose a genuinely NEW canonical relation type (not just a mapping
    into an existing one) — then waits for human approval before that
    type becomes real. Nothing here ever blocks normal resolution;
    OntologyResolver keeps working exactly as before regardless of
    whether a proposal is pending, approved, or rejected.
    """

    def __init__(self, mistral_client, model: str,
                 pending_file: str = "pending_ontology.json",
                 extensions_file: str = "ontology_extensions.json"):
        self.mistral_client = mistral_client
        self.model = model
        self.pending_file = Path(pending_file)
        self.extensions_file = Path(extensions_file)
        self.pending: dict = {}
        self._load()
        logger.info(f"OntologyEvolution initialized — {len(self.pending)} tracked friction patterns")

    def _load(self):
        if self.pending_file.exists():
            try:
                with open(self.pending_file, "r", encoding="utf-8") as f:
                    self.pending = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load pending ontology file, starting fresh: {e}")
                self.pending = {}

    def _save(self):
        with open(self.pending_file, "w", encoding="utf-8") as f:
            json.dump(self.pending, f, indent=2)

    def load_extensions(self) -> dict:
        """Returns {'new_types': {canonical: category}, 'approved_synonyms': {raw: canonical}}."""
        if self.extensions_file.exists():
            try:
                with open(self.extensions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {
                        "new_types": data.get("new_types", {}),
                        "approved_synonyms": data.get("approved_synonyms", {})
                    }
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load ontology extensions, using none: {e}")
        return {"new_types": {}, "approved_synonyms": {}}

    def _save_extensions(self, extensions: dict):
        with open(self.extensions_file, "w", encoding="utf-8") as f:
            json.dump(extensions, f, indent=2)

    def record_friction(self, raw: str, cleaned: str, result: dict, context: dict):
        """
        Call this whenever resolve() lands on 'fallback', or on 'llm' with
        low confidence. Accumulates evidence; once the threshold is
        crossed and this pattern hasn't been reviewed yet, triggers an
        LLM proposal for a genuinely new relation type.
        """
        key = cleaned.lower()
        entry = self.pending.get(key)

        if not entry:
            entry = {
                "raw_examples": [raw],
                "count": 0,
                "current_landing": result.get("canonical"),
                "avg_confidence": 0.0,
                "evidence": [],
                "suggested_canonical": None,
                "suggested_category": None,
                "suggestion_confidence": None,
                "review_status": "collecting",
                "first_seen": datetime.now().isoformat(),
                "last_seen": datetime.now().isoformat(),
            }
            self.pending[key] = entry

        if raw not in entry["raw_examples"] and len(entry["raw_examples"]) < MAX_RAW_EXAMPLES:
            entry["raw_examples"].append(raw)

        # Running average confidence for this friction pattern
        prior_count = entry["count"]
        prior_avg = entry["avg_confidence"]
        new_conf = result.get("confidence", 0.0)
        entry["count"] = prior_count + 1
        entry["avg_confidence"] = round((prior_avg * prior_count + new_conf) / entry["count"], 4)
        entry["last_seen"] = datetime.now().isoformat()
        entry["current_landing"] = result.get("canonical")

        if len(entry["evidence"]) < MAX_EVIDENCE_PER_ENTRY:
            entry["evidence"].append({
                "from": context.get("from", ""),
                "to": context.get("to", ""),
                "description": context.get("description", "")[:200],
                "source_doc": context.get("source_doc", "unknown"),
            })

        # Only propose once, and only once there's enough repeated evidence
        if entry["review_status"] == "collecting" and entry["count"] >= FRICTION_MIN_COUNT:
            self._propose_expansion(key, entry)

        self._save()

    def _propose_expansion(self, key: str, entry: dict):
        """Ask the LLM to propose a genuinely new canonical type for a
        recurring pattern that keeps being force-fit into the existing set."""
        evidence_desc = "\n".join(
            f"- {e['from']} --[{key}]--> {e['to']}: {e['description']}"
            for e in entry["evidence"]
        )
        prompt = f"""A relation type keeps appearing in documents but doesn't fit cleanly
into our existing ontology — it's currently being force-mapped to '{entry['current_landing']}'
with low average confidence ({entry['avg_confidence']}).

Raw phrasings seen: {', '.join(entry['raw_examples'])}
Occurred {entry['count']} times across multiple documents.

Example evidence:
{evidence_desc}

Propose a NEW canonical relation type (SCREAMING_SNAKE_CASE) that would better represent
this relationship, along with which category it belongs to: STRUCTURAL, AUTHORSHIP,
FUNCTIONAL, REFERENTIAL, or SEMANTIC.

Respond ONLY with valid JSON:
{{"proposed_canonical": "NEW_TYPE_NAME", "category": "ONE_OF_THE_CATEGORIES", "confidence": 0.0-1.0, "justification": "one sentence"}}"""

        try:
            response = self.mistral_client.chat.complete(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are an ontology design assistant helping expand a knowledge graph's relation vocabulary thoughtfully. Propose new types only when genuinely warranted."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.2
            )
            result = json.loads(response.choices[0].message.content)
            entry["suggested_canonical"] = result.get("proposed_canonical", "").strip().upper()
            entry["suggested_category"] = result.get("category", "SEMANTIC").strip().upper()
            entry["suggestion_confidence"] = result.get("confidence", 0.5)
            entry["justification"] = result.get("justification", "")
            entry["review_status"] = "pending_review"
            logger.info(
                f"  Ontology expansion proposed: '{key}' -> new type '{entry['suggested_canonical']}' "
                f"({entry['suggested_category']}) — awaiting human approval"
            )
        except Exception as e:
            logger.warning(f"  Ontology expansion proposal failed for '{key}': {e}")

    def get_pending_review(self) -> list[dict]:
        return [
            {"key": k, **v} for k, v in self.pending.items()
            if v["review_status"] == "pending_review"
        ]

    def get_summary(self) -> dict:
        collecting = sum(1 for v in self.pending.values() if v["review_status"] == "collecting")
        pending_review = sum(1 for v in self.pending.values() if v["review_status"] == "pending_review")
        approved = sum(1 for v in self.pending.values() if v["review_status"] == "approved")
        rejected = sum(1 for v in self.pending.values() if v["review_status"] == "rejected")
        return {
            "collecting": collecting,
            "pending_review": pending_review,
            "approved": approved,
            "rejected": rejected,
            "top_pending": sorted(self.get_pending_review(), key=lambda e: e["count"], reverse=True)[:5]
        }

    def approve(self, key: str, canonical: str | None = None, category: str | None = None):
        """Called by the review script. Adds the new type permanently to
        ontology_extensions.json so OntologyResolver picks it up next run."""
        entry = self.pending.get(key)
        if not entry:
            return False

        final_canonical = (canonical or entry["suggested_canonical"]).strip().upper()
        final_category = (category or entry["suggested_category"]).strip().upper()

        extensions = self.load_extensions()
        extensions["new_types"][final_canonical] = final_category
        for raw in entry["raw_examples"]:
            raw_key = raw.strip().upper().replace(" ", "_")
            extensions["approved_synonyms"][raw_key] = final_canonical
        extensions["approved_synonyms"][key.upper()] = final_canonical
        self._save_extensions(extensions)

        entry["review_status"] = "approved"
        entry["final_canonical"] = final_canonical
        entry["final_category"] = final_category
        entry["approved_at"] = datetime.now().isoformat()
        self._save()
        logger.info(f"  Ontology expansion APPROVED: '{key}' -> '{final_canonical}' ({final_category})")
        return True

    def reject(self, key: str):
        entry = self.pending.get(key)
        if not entry:
            return False
        entry["review_status"] = "rejected"
        entry["rejected_at"] = datetime.now().isoformat()
        self._save()
        logger.info(f"  Ontology expansion REJECTED: '{key}' — will keep using existing fallback mapping")
        return True

    def close(self):
        self._save()