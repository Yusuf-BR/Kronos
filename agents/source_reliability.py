import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_RELIABILITY = 0.7
RELIABILITY_STEP = 0.03
MIN_RELIABILITY = 0.2
MAX_RELIABILITY = 0.99


class SourceReliability:
    """
    Component 13. Every source document earns a reliability score over
    time. Seeded partly from extraction quality (a scanned, OCR-heavy
    PDF starts slightly lower than a clean digital one), then nudged by
    the Reconciler every time a real contradiction gets resolved in
    favor of one source over another — repeated wins raise trust,
    repeated losses lower it.
    """
    def __init__(self, memory_file: str = "source_reliability.json"):
        self.memory_file = Path(memory_file)
        self.scores = {}
        self._load()
        logger.info(f"SourceReliability initialized with {len(self.scores)} tracked sources")

    def _load(self):
        if self.memory_file.exists():
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    self.scores = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load source reliability, starting fresh: {e}")
                self.scores = {}

    def _save(self):
        with open(self.memory_file, "w", encoding="utf-8") as f:
            json.dump(self.scores, f, indent=2)

    def get(self, source_doc: str) -> float:
        return self.scores.get(source_doc, {}).get("reliability", DEFAULT_RELIABILITY)

    def register_source(self, source_doc: str, quality_score: float = 1.0):
        """Call once per new document at ingestion time."""
        if source_doc in self.scores:
            return
        seeded = round(DEFAULT_RELIABILITY * (0.7 + 0.3 * quality_score), 4)
        self.scores[source_doc] = {
            "reliability": min(MAX_RELIABILITY, max(MIN_RELIABILITY, seeded)),
            "wins": 0,
            "losses": 0
        }
        self._save()

    def record_resolution(self, winner_doc: str, loser_doc: str):
        """Called by the Reconciler whenever a genuine contradiction is
        resolved in favor of one source over another."""
        for doc in (winner_doc, loser_doc):
            if doc not in self.scores:
                self.register_source(doc, quality_score=1.0)

        winner = self.scores[winner_doc]
        loser = self.scores[loser_doc]

        winner["wins"] += 1
        winner["reliability"] = round(min(MAX_RELIABILITY, winner["reliability"] + RELIABILITY_STEP), 4)

        loser["losses"] += 1
        loser["reliability"] = round(max(MIN_RELIABILITY, loser["reliability"] - RELIABILITY_STEP), 4)

        self._save()
        logger.info(
            f"  SourceReliability updated: '{winner_doc}' -> {winner['reliability']} (won), "
            f"'{loser_doc}' -> {loser['reliability']} (lost)"
        )

    def get_all(self) -> dict:
        return self.scores