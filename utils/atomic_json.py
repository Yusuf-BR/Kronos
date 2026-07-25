# utils/atomic_json.py
import json
import os
import tempfile
import logging

logger = logging.getLogger(__name__)

def atomic_write_json(path: str, data: dict, indent: int = 2) -> None:
    """
    Write JSON data to `path` atomically using a temp file + rename.
    
    Guarantees that `path` is never in a partially-written state.
    If the write fails, the original file (if any) remains untouched.
    """
    abs_path = os.path.abspath(path)
    dir_name = os.path.dirname(abs_path)
    
    # Ensure target directory exists
    os.makedirs(dir_name, exist_ok=True)
    
    # Write to temp file in same directory (same filesystem for atomic rename)
    fd, temp_path = tempfile.mkstemp(
        dir=dir_name,
        prefix='.' + os.path.basename(path) + '.tmp.',
        suffix=''
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())  # Force OS-level flush to disk
    except Exception:
        # Clean up temp file on any write failure
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    
    # Atomic replace: old file stays valid until the instant new file appears
    os.replace(temp_path, abs_path)
    logger.debug(f"Atomically wrote {abs_path}")