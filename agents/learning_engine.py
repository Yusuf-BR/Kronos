import json
import logging
import math
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)

# How much extra confidence an alias earns as times_seen grows.
# Saturating curve — diminishing returns, capped well below 1.0 so a
# single LLM referee override can still always outrank pure repetition.
REINFORCEMENT_CAP = 0.97
REINFORCEMENT_RATE = 0.08  # higher = confidence climbs faster with repetition

# Minimum times two entities must co-occur in the same chunk before
# they're surfaced as a candidate — avoids flagging every incidental pairing.
COOCCURRENCE_MIN_COUNT = 4


def _pair_key(name_a: str, type_a: str, name_b: str, type_b: str) -> str:
    """Order-independent key so (A,B) and (B,A) are the same pair."""
    a = f"{type_a}::{name_a.strip().lower()}"
    b = f"{type_b}::{name_b.strip().lower()}"
    return " | ".join(sorted([a, b]))


class LearningEngine:
    """
    Consolidation layer that runs periodically (alongside the Curator)
    rather than on every document. Two responsibilities:

    1. Reinforce alias/entity confidence based on accumulated times_seen —
       something seen independently 50 times deserves more trust than
       something seen once, even if the original detection confidence
       was identical.
    2. Track entity co-occurrence within chunks — surfaces pairs that
       keep appearing together but have no relationship edge in Neo4j,
       which is either a missed relationship or a missed entity merge.

    This does NOT duplicate AliasMemory or EntityMemory's storage — it
    reads their existing data and Neo4j's graph, and only persists the
    co-occurrence counts, which nothing else tracks.
    """

    def __init__(self, cooccurrence_file: str = "cooccurrence_memory.json"):
        self.cooccurrence_file = Path(cooccurrence_file)
        self.pairs: dict[str, dict] = {}
        self._dirty = False
        self._load()
        logger.info(f"LearningEngine initialized with {len(self.pairs)} tracked entity pairs")

    def _load(self):
        if self.cooccurrence_file.exists():
            try:
                with open(self.cooccurrence_file, "r", encoding="utf-8") as f:
                    self.pairs = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load co-occurrence memory, starting fresh: {e}")
                self.pairs = {}

    def flush(self):
        if not self._dirty:
            return
        with open(self.cooccurrence_file, "w", encoding="utf-8") as f:
            json.dump(self.pairs, f, indent=2)
        self._dirty = False

    def record_chunk_entities(self, entities_in_chunk: list[dict], source_doc: str):
        """
        Call once per chunk during ingestion with the list of entities
        {"name", "type"} that appeared in that chunk. Records every
        pairwise co-occurrence within the chunk.
        """
        unique = {}
        for e in entities_in_chunk:
            key = f"{e['type']}::{e['name'].strip().lower()}"
            unique[key] = e
        items = list(unique.values())

        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                key = _pair_key(a["name"], a["type"], b["name"], b["type"])
                entry = self.pairs.get(key)
                if not entry:
                    self.pairs[key] = {
                        "entity_a": a["name"], "type_a": a["type"],
                        "entity_b": b["name"], "type_b": b["type"],
                        "count": 1,
                        "source_docs": [source_doc]
                    }
                else:
                    entry["count"] += 1
                    if source_doc not in entry["source_docs"]:
                        entry["source_docs"].append(source_doc)
                self._dirty = True

    def get_unlinked_candidates(self, neo4j_client, min_count: int = COOCCURRENCE_MIN_COUNT) -> list[dict]:
        """
        Returns entity pairs that co-occur frequently but have NO
        relationship edge between them in Neo4j yet. These are candidates
        for either a missed relationship or a missed entity merge —
        surfaced for the Curator digest, not auto-created.
        """
        candidates = []
        for entry in self.pairs.values():
            if entry["count"] < min_count:
                continue
            if self._has_any_relationship(neo4j_client, entry["entity_a"], entry["entity_b"]):
                continue
            candidates.append(entry)

        candidates.sort(key=lambda e: e["count"], reverse=True)
        return candidates

    @staticmethod
    def _has_any_relationship(neo4j_client, name_a: str, name_b: str) -> bool:
        with neo4j_client.driver.session() as session:
            result = session.run("""
                MATCH (a:Entity {name: $name_a})-[r]-(b:Entity {name: $name_b})
                RETURN count(r) AS rel_count
            """, name_a=name_a, name_b=name_b)
            record = result.single()
            return record and record["rel_count"] > 0

    def reinforce_alias_confidence(self, alias_memory) -> int:
        """
        Bump confidence on aliases based on accumulated times_seen.
        A single detection event gives you the base confidence — being
        independently re-confirmed across many documents should push
        that higher, with diminishing returns, capped below 1.0 so a
        strong LLM referee rejection can still always override pure
        repetition.

        Expects alias_memory to expose its raw entries dict as
        alias_memory.memory, each entry having at least
        {"confidence", "times_seen"}. AliasMemory saves immediately on
        every write (no separate flush step), so this calls its
        internal _save() directly when anything changes.
        """
        if not hasattr(alias_memory, "memory"):
            logger.warning("LearningEngine: alias_memory has no 'memory' attribute, skipping reinforcement")
            return 0

        adjusted = 0
        for key, entry in alias_memory.memory.items():
            times_seen = entry.get("times_seen", 1)
            base_confidence = entry.get("confidence", 0.7)

            reinforced = REINFORCEMENT_CAP - (REINFORCEMENT_CAP - base_confidence) * math.exp(
                -REINFORCEMENT_RATE * max(0, times_seen - 1)
            )
            reinforced = round(min(REINFORCEMENT_CAP, reinforced), 4)

            if reinforced > entry.get("confidence", 0.7):
                entry["confidence"] = reinforced
                adjusted += 1

        if adjusted:
            alias_memory._save()
            logger.info(f"LearningEngine: reinforced confidence on {adjusted} aliases based on repeated observation")

        return adjusted

    def get_entity_type_stats(self, entity_memory) -> dict:
        """
        Confidence statistics per entity type — surfaces if a specific
        type (e.g. ORG, EVENT) is systematically extracted with low
        confidence, which usually points to a prompt/extraction quality
        issue for that category rather than a one-off bad document.
        """
        stats = defaultdict(lambda: {"count": 0, "total_confidence": 0.0})
        for entry in entity_memory.entities.values():
            t = entry.get("type", "UNKNOWN")
            stats[t]["count"] += 1
            stats[t]["total_confidence"] += entry.get("confidence", 0.0)

        result = {}
        for t, data in stats.items():
            result[t] = {
                "count": data["count"],
                "avg_confidence": round(data["total_confidence"] / data["count"], 3) if data["count"] else 0.0
            }
        return result

    def run_consolidation(self, entity_memory, alias_memory, neo4j_client) -> dict:
        """
        Full learning pass — call this periodically (e.g. from the
        Curator's nightly cycle), not on every document.
        """
        logger.info("LearningEngine: running consolidation pass...")

        aliases_reinforced = self.reinforce_alias_confidence(alias_memory)
        unlinked = self.get_unlinked_candidates(neo4j_client)
        type_stats = self.get_entity_type_stats(entity_memory)

        report = {
            "aliases_reinforced": aliases_reinforced,
            "unlinked_candidate_pairs": unlinked[:20],
            "total_unlinked_candidates": len(unlinked),
            "entity_type_stats": type_stats,
            "tracked_cooccurrence_pairs": len(self.pairs)
        }

        logger.info(
            f"LearningEngine: consolidation done — {aliases_reinforced} aliases reinforced, "
            f"{len(unlinked)} unlinked candidate pairs found"
        )
        return report

    def close(self):
        self.flush()