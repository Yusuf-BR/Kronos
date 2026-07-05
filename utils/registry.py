import json
import os
import hashlib
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

REGISTRY_PATH = "logs/registry.json"

def _load() -> dict:
    if not os.path.exists(REGISTRY_PATH):
        return {"processed": {}, "failed": {}, "fingerprints": {}}
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def _save(registry: dict):
    os.makedirs("logs", exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)

def fingerprint(filepath: str) -> str:
    """MD5 hash of file content — detects duplicates regardless of filename."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def is_duplicate(filepath: str) -> bool:
    """Returns True if this exact file content was already processed."""
    fp = fingerprint(filepath)
    registry = _load()
    return fp in registry["fingerprints"]

def mark_processed(filepath: str, result: dict):
    """Record successful processing."""
    fp = fingerprint(filepath)
    filename = Path(filepath).name
    registry = _load()
    registry["processed"][filename] = {
        "fingerprint": fp,
        "processed_at": datetime.now().isoformat(),
        "entities": result.get("entities_written", 0),
        "relationships": result.get("relationships_written", 0),
        "chunks": result.get("chunks_written", 0),
        "claims": result.get("claims_written", 0)
    }
    registry["fingerprints"][fp] = filename
    _save(registry)
    logger.info(f"Registry: marked {filename} as processed")

def mark_failed(filepath: str, error: str, stage: str):
    """Record failed processing with error details."""
    filename = Path(filepath).name
    registry = _load()
    registry["failed"][filename] = {
        "failed_at": datetime.now().isoformat(),
        "stage": stage,
        "error": str(error)[:500]
    }
    _save(registry)
    logger.error(f"Registry: marked {filename} as FAILED at {stage}: {error}")

def get_failed() -> dict:
    return _load()["failed"]

def get_processed() -> dict:
    return _load()["processed"]

def get_stats() -> dict:
    registry = _load()
    return {
        "total_processed": len(registry["processed"]),
        "total_failed": len(registry["failed"]),
        "total_fingerprints": len(registry["fingerprints"])
    }

def remove_from_failed(filename: str):
    """Remove from failed list so it can be retried."""
    registry = _load()
    if filename in registry["failed"]:
        del registry["failed"][filename]
        _save(registry)
        logger.info(f"Registry: {filename} removed from failed, ready for retry")