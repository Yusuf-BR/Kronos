import json
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


class SelfEvaluator:
    """
    Component 17. After every ingestion, KRONOS scores its own
    performance on that document — not just how many things got written,
    but the ratio of what it kept vs. discarded, how much it learned,
    and how long it took. Appended to a running log so quality trends
    are visible over time, not just per-document.
    """
    def __init__(self, log_file: str = "self_evaluation.jsonl"):
        self.log_file = Path(log_file)

    def evaluate(self, filename: str, extracted: dict, ingest_result: dict,
                 aliases_before: int, aliases_after: int,
                 ontology_before: int, ontology_after: int,
                 processing_seconds: float) -> dict:
        entities_total = len(extracted.get("entities", []))
        relationships_total = len(extracted.get("relationships", []))

        entities_written = ingest_result.get("entities_written", 0)
        entities_skipped = ingest_result.get("entities_skipped", 0)
        relationships_written = ingest_result.get("relationships_written", 0)
        relationships_rejected = ingest_result.get("relationships_rejected", 0)

        duplicate_rate = round(1 - (entities_written / entities_total), 4) if entities_total else 0.0

        evaluation = {
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
            "tier": extracted.get("tier", 1),
            "quality_score": extracted.get("quality_score", 1.0),
            "entities_extracted": entities_total,
            "entities_written": entities_written,
            "entities_skipped": entities_skipped,
            "relationships_extracted": relationships_total,
            "relationships_written": relationships_written,
            "relationships_rejected": relationships_rejected,
            "new_aliases_learned": max(0, aliases_after - aliases_before),
            "new_ontology_mappings_learned": max(0, ontology_after - ontology_before),
            "duplicate_rate": duplicate_rate,
            "processing_seconds": round(processing_seconds, 2),
        }

        self._append(evaluation)
        logger.info(
            f"  Self-evaluation: {entities_written}/{entities_total} entities kept, "
            f"{relationships_written}/{relationships_total} relationships kept, "
            f"{evaluation['new_aliases_learned']} new aliases, "
            f"{evaluation['new_ontology_mappings_learned']} new ontology mappings, "
            f"{processing_seconds:.1f}s"
        )
        return evaluation

    def _append(self, record: dict):
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def get_recent(self, limit: int = 10) -> list[dict]:
        if not self.log_file.exists():
            return []
        with open(self.log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return list(reversed([json.loads(l) for l in lines[-limit:]]))