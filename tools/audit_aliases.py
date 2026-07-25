"""
Audit script for alias_memory.json — lists learned aliases sorted by
confidence so bad merges (e.g. antonyms getting matched together) can be
spotted and removed before they corrupt more documents.

Usage:
    python tools/audit_aliases.py                  # list everything, lowest confidence first
    python tools/audit_aliases.py --below 0.9       # only show entries below a confidence cutoff
    python tools/audit_aliases.py --remove "unsupervised learning"   # delete a specific bad entry
"""
import argparse
import json
from pathlib import Path


def load_memory(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"No file found at {path}")
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_memory(path: str, memory: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Audit and clean alias_memory.json")
    parser.add_argument("--file", default="alias_memory.json")
    parser.add_argument("--below", type=float, default=None, help="Only show entries with confidence below this value")
    parser.add_argument("--source", default=None, help="Only show entries from a specific source (embedding, fuzzy, typo, referee)")
    parser.add_argument("--remove", default=None, help="Remove a specific alias key and exit")
    args = parser.parse_args()

    memory = load_memory(args.file)

    if args.remove:
        key = args.remove.lower()
        if key in memory:
            removed = memory.pop(key)
            save_memory(args.file, memory)
            print(f"Removed: '{args.remove}' -> '{removed.get('canonical')}'")
        else:
            print(f"No entry found for '{args.remove}'")
        return

    entries = [(k, v) for k, v in memory.items()]

    if args.below is not None:
        entries = [(k, v) for k, v in entries if v.get("confidence", 1.0) < args.below]

    if args.source:
        entries = [(k, v) for k, v in entries if v.get("source") == args.source]

    entries.sort(key=lambda kv: kv[1].get("confidence", 1.0))

    if not entries:
        print("No matching entries.")
        return

    print(f"{'alias':<40} {'canonical':<40} {'conf':>6} {'seen':>5} {'source':<10}")
    print("-" * 105)
    for key, entry in entries:
        print(
            f"{key[:39]:<40} {str(entry.get('canonical', ''))[:39]:<40} "
            f"{entry.get('confidence', 0):>6.3f} {entry.get('times_seen', 1):>5} "
            f"{entry.get('source', '?'):<10}"
        )

    print(f"\n{len(entries)} entries shown out of {len(memory)} total.")
    print("To remove a bad entry: python tools/audit_aliases.py --remove \"exact alias key\"")


if __name__ == "__main__":
    main()