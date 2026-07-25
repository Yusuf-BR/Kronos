import json
import logging
from pathlib import Path

from utils.atomic_json import atomic_write_json

logger = logging.getLogger(__name__)

REUSE_REINFORCEMENT = 0.01
MAX_CONFIDENCE = 1.0

# Minimum confidence required to WRITE an alias into permanent memory.
# This is deliberately higher than the threshold used to accept a match
# for a single entity right now — a shaky match affecting one document is
# recoverable, but a shaky match written to alias memory gets reused
# automatically, silently, forever, on every future document. The two
# decisions ("use this match now" vs "trust this match permanently") must
# not share the same bar.
ALIAS_COMMIT_THRESHOLD = 0.93


class AliasMemory:
    """
    Continuously learned alias table. Starts empty ({}), and grows as the
    system resolves entity names — via embedding match, fuzzy match, typo
    correction, or LLM referee — building up a persisted map of
    alias -> canonical name.

    Entry shape:
    {
      "canonical": "Graphics Processing Unit",
      "confidence": 0.99,
      "times_seen": 72,
      "source": "embedding"
    }
    """

    def __init__(self, memory_file: str = "alias_memory.json"):
        self.memory_file = Path(memory_file)
        self.memory = {}
        self._load()
        logger.info(f"AliasMemory initialized with {len(self.memory)} entries")

    def _load(self):
        if self.memory_file.exists():
            with open(self.memory_file, "r", encoding="utf-8") as f:
                self.memory = json.load(f)
        else:
            self.memory = {}
            self._save()

    def _save(self):
        atomic_write_json(str(self.memory_file), self.memory)

    def get(self, name: str) -> tuple[str | None, float]:
        """
        Look up an alias. On a hit, this counts as reinforcement.

        Returns:
            tuple: (canonical_name, confidence) or (None, 0.0) if unknown
        """
        key = name.lower()
        entry = self.memory.get(key)
        if not entry:
            return None, 0.0

        entry["times_seen"] = entry.get("times_seen", 1) + 1
        entry["confidence"] = round(min(MAX_CONFIDENCE, entry.get("confidence", 0.7) + REUSE_REINFORCEMENT), 4)
        self._save()

        return entry["canonical"], entry["confidence"]

    def record(self, name: str, canonical: str, confidence: float, source: str = "unknown"):
        """
        Record (or reinforce) an alias — but ONLY if confidence clears
        ALIAS_COMMIT_THRESHOLD. A match that's good enough to use for one
        entity right now is NOT automatically good enough to trust forever
        for every future document. Weaker matches are simply not written;
        the caller still uses them for the current entity, they just don't
        get cached, so a wrong single-instance guess can't compound.
        """
        key = name.lower()
        confidence = round(max(0.0, min(MAX_CONFIDENCE, confidence)), 4)

        if confidence < ALIAS_COMMIT_THRESHOLD:
            logger.info(
                f"  Alias NOT committed ({source}): '{name}' -> '{canonical}' "
                f"(confidence {confidence:.2f} below commit threshold {ALIAS_COMMIT_THRESHOLD}) — "
                f"used for this entity only, not cached"
            )
            return

        existing = self.memory.get(key)

        if not existing:
            self.memory[key] = {
                "canonical": canonical,
                "confidence": confidence,
                "times_seen": 1,
                "source": source,
            }
            logger.info(f"  Learned new alias ({source}): '{name}' -> '{canonical}' (confidence: {confidence:.2f})")
            self._save()
            return

        if existing["canonical"] == canonical:
            existing["times_seen"] = existing.get("times_seen", 1) + 1
            existing["confidence"] = round(max(existing.get("confidence", 0.0), confidence), 4)
            self._save()
            return

        # Conflict: same alias, different canonical than what's on record.
        if confidence > existing.get("confidence", 0.0):
            logger.warning(
                f"  Alias conflict ({source}): '{name}' was -> '{existing['canonical']}' "
                f"({existing.get('confidence', 0):.2f}, seen {existing.get('times_seen', 1)}x), "
                f"now -> '{canonical}' ({confidence:.2f}) — overwriting, new confidence higher"
            )
            self.memory[key] = {
                "canonical": canonical,
                "confidence": confidence,
                "times_seen": 1,
                "source": source,
            }
            self._save()
        else:
            logger.info(
                f"  Alias conflict ({source}): '{name}' -> '{canonical}' ({confidence:.2f}) ignored, "
                f"existing mapping '{existing['canonical']}' ({existing.get('confidence', 0):.2f}) is stronger"
            )

    def flag_bad_entry(self, name: str, reason: str = "manually flagged"):
        """
        Remove a bad alias entry — used by the audit tool or manual review
        to correct a mistake before it corrupts more documents.
        """
        key = name.lower()
        if key in self.memory:
            removed = self.memory.pop(key)
            self._save()
            logger.warning(f"  Removed bad alias '{name}' -> '{removed.get('canonical')}' ({reason})")
            return True
        return False