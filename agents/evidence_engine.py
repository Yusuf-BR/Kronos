import json
import time
import hashlib
import logging

logger = logging.getLogger(__name__)

# How much to bump confidence when a new, previously-unseen piece of
# evidence corroborates an already-known entity/relationship. Mirrors
# config.CONFIDENCE_DECAY_RATE so reinforcement and decay are symmetric.
REINFORCEMENT_STEP = 0.05
MAX_CONFIDENCE = 1.0


def make_chunk_id(source_doc: str, page: int, index: int, text: str) -> str:
    """
    Stable, deterministic chunk ID — same chunk always gets the same ID
    across re-runs, which is what makes evidence dedup possible.
    """
    basis = f"{source_doc}:{page}:{index}:{text[:80]}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def build_evidence_record(source_doc: str, page: int, chunk_id: str,
                           excerpt: str, confidence: float = 1.0) -> dict:
    """
    One evidence record: everything needed to trace a fact back to its origin.
    """
    return {
        "source_doc": source_doc,
        "page": page,
        "chunk_id": chunk_id,
        "excerpt": excerpt[:300],  # keep it bounded, this is a pointer not a copy of the whole chunk
        "confidence": confidence,
        "extracted_at": int(time.time()),
    }


def load_evidence_list(evidence_json: str | None) -> list[dict]:
    if not evidence_json:
        return []
    try:
        data = json.loads(evidence_json)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupt evidence_json found, resetting to empty list")
        return []


def merge_evidence(existing_json: str | None, new_record: dict) -> tuple[str, bool, float]:
    """
    Append new_record to the existing evidence list, deduped by
    (source_doc, chunk_id) so reprocessing the same document doesn't
    create duplicate evidence entries.

    Returns: (updated_json_string, is_new_evidence, confidence_bump)
    confidence_bump is REINFORCEMENT_STEP if this is genuinely new
    corroborating evidence, else 0.0.
    """
    existing = load_evidence_list(existing_json)

    dedup_key = (new_record["source_doc"], new_record["chunk_id"])
    already_present = any(
        (e.get("source_doc"), e.get("chunk_id")) == dedup_key
        for e in existing
    )

    if already_present:
        return json.dumps(existing), False, 0.0

    existing.append(new_record)
    # Cheap cap so this doesn't grow unbounded on documents reprocessed many times
    # with slightly different chunking — keep the most recent 50 pieces of evidence.
    if len(existing) > 50:
        existing = sorted(existing, key=lambda e: e.get("confidence", 0), reverse=True)[:50]

    return json.dumps(existing), True, REINFORCEMENT_STEP


def apply_reinforcement(current_confidence: float, bump: float) -> float:
    return min(MAX_CONFIDENCE, current_confidence + bump)