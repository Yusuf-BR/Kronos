import ast
import json
import logging
import math
from pathlib import Path

from utils.atomic_json import atomic_write_json
from utils.acronym import is_acronym_match

logger = logging.getLogger(__name__)

CONFIDENCE_MERGE_WEIGHT = 0.3
MAX_DESCRIPTIONS = 5


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class EntityMemory:
    def __init__(self, memory_file: str = "entity_memory.json", neo4j_client=None):
        self.memory_file = Path(memory_file)
        self.neo4j_client = neo4j_client
        self.entities: dict[str, dict] = {}
        self._embeddings: dict[str, list[float]] = {}
        self._dirty = False
        self._load()
        if not self.entities and self.neo4j_client:
            self._hydrate_from_neo4j()
        logger.info(f"EntityMemory initialized with {len(self.entities)} entities (metadata only, no persisted vectors)")

    def _load(self):
        if self.memory_file.exists():
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    self.entities = json.load(f)
                for entry in self.entities.values():
                    entry.pop("embedding", None)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load entity memory, starting fresh: {e}")
                self.entities = {}

    def _hydrate_from_neo4j(self):
        try:
            records = self.neo4j_client.get_all_entities()
            for record in records:
                key = self._key(record["canonical"], record["type"])

                description = ""
                if record.get("properties"):
                    try:
                        props = ast.literal_eval(record["properties"])
                        if isinstance(props, dict):
                            description = props.get("description", "")
                    except (ValueError, SyntaxError):
                        pass

                occurrence_count = 1
                if record.get("evidence"):
                    try:
                        evidence_list = json.loads(record["evidence"])
                        if isinstance(evidence_list, list):
                            occurrence_count = max(1, len(evidence_list))
                    except (json.JSONDecodeError, TypeError):
                        pass

                self.entities[key] = {
                    "canonical": record["canonical"],
                    "type": record["type"],
                    "domain": record.get("domain"),
                    "aliases": [],
                    "descriptions": [description] if description else [],
                    "occurrence_count": occurrence_count,
                    "confidence": record.get("confidence", 0.7) or 0.7,
                    "relationship_count": 0,
                }
            logger.info(f"  Hydrated {len(self.entities)} entities from Neo4j")
        except Exception as e:
            logger.warning(f"  Failed to hydrate EntityMemory from Neo4j: {e}")

    def flush(self):
        if not self._dirty:
            return
        atomic_write_json(str(self.memory_file), self.entities, indent=None)
        self._dirty = False

    @staticmethod
    def _key(canonical_name: str, entity_type: str) -> str:
        return f"{entity_type}::{canonical_name.strip().lower()}"

    def find_similar(self, embedding: list[float], entity_type: str, threshold: float = 0.85, domain: str | None = None) -> dict | None:
        best_match, best_score = None, 0.0
        for key, entry in self.entities.items():
            if entry.get("type") != entity_type:
                continue
            if domain and entry.get("domain") and domain != entry.get("domain"):
                continue
            vec = self._embeddings.get(key)
            if not vec:
                continue
            score = _cosine(embedding, vec)
            if score > best_score:
                best_score, best_match = score, entry
        if best_match and best_score >= threshold:
            return {
                "name": best_match["canonical"],
                "type": best_match["type"],
                "domain": best_match.get("domain"),
                "similarity": best_score
            }
        return None

    def find_acronym_match(self, name: str, entity_type: str, domain: str | None = None) -> dict | None:
        """
        Independent of embedding similarity — scans known entities of the
        same type for a plausible acronym/abbreviation relationship (e.g.
        'ML' <-> 'Machine Learning'). Both embedding cosine similarity and
        edit-distance checks fail on this pattern by construction (short
        token vs long phrase, see utils/acronym.py), so this needs its
        own independent check rather than a tuned threshold on either.
        """
        for entry in self.entities.values():
            if entry.get("type") != entity_type:
                continue
            if domain and entry.get("domain") and domain != entry.get("domain"):
                continue
            candidate = entry["canonical"]
            if candidate.lower() == name.lower():
                continue
            if is_acronym_match(name, candidate):
                return {"name": candidate, "type": entry["type"], "domain": entry.get("domain")}
        return None

    def upsert(self, canonical_name: str, entity_type: str, embedding: list[float],
               description: str = "", alias: str | None = None, confidence: float = 1.0,
               domain: str | None = None):
        key = self._key(canonical_name, entity_type)
        entry = self.entities.get(key)

        self._embeddings[key] = embedding

        if not entry:
            self.entities[key] = {
                "canonical": canonical_name,
                "type": entity_type,
                "domain": domain,
                "aliases": [alias] if alias and alias.lower() != canonical_name.lower() else [],
                "descriptions": [description] if description else [],
                "occurrence_count": 1,
                "confidence": confidence,
                "relationship_count": 0,
            }
        else:
            entry["occurrence_count"] = entry.get("occurrence_count", 1) + 1
            if alias and alias.lower() != canonical_name.lower() and alias not in entry["aliases"]:
                entry["aliases"].append(alias)
            if description and description not in entry.get("descriptions", []):
                descs = entry.setdefault("descriptions", [])
                descs.append(description)
                entry["descriptions"] = descs[-MAX_DESCRIPTIONS:]
            old_conf = entry.get("confidence", confidence)
            entry["confidence"] = round(
                old_conf * (1 - CONFIDENCE_MERGE_WEIGHT) + confidence * CONFIDENCE_MERGE_WEIGHT, 4
            )
            if domain and not entry.get("domain"):
                entry["domain"] = domain

        self._dirty = True

    def record_relationship(self, canonical_name: str, entity_type: str):
        entry = self.entities.get(self._key(canonical_name, entity_type))
        if entry:
            entry["relationship_count"] = entry.get("relationship_count", 0) + 1
            self._dirty = True