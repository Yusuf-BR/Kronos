"""
scripts/merge_duplicate_entities.py

One-time cleanup for entities that were split apart before the acronym-
matching fix existed (e.g. "ML" / "Machine Learning" / "Machine Learning
(ML)" as three separate nodes). Finds them, shows you the proposed merge,
and only acts after you confirm — this touches the graph permanently,
so nothing here runs silently.

Usage:
    python scripts/merge_duplicate_entities.py            # interactive, asks per merge
    python scripts/merge_duplicate_entities.py --dry-run  # shows candidates only, changes nothing
"""

import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.neo4j_client import Neo4jClient
from utils.acronym import is_acronym_match, extract_parenthetical

ALIAS_MEMORY_FILE = "alias_memory.json"
ENTITY_MEMORY_FILE = "entity_memory.json"


def fetch_all_entities(neo4j):
    with neo4j.driver.session() as session:
        result = session.run("""
            MATCH (e:Entity)
            RETURN id(e) AS node_id, e.name AS name, e.type AS type,
                   e.domain AS domain, e.confidence AS confidence
        """)
        return [dict(r) for r in result]


def find_duplicate_groups(entities: list[dict]) -> list[list[dict]]:
    """Groups entities of the same type (and same domain, if both set)
    that are acronym/abbreviation variants of each other."""
    by_type = {}
    for e in entities:
        by_type.setdefault(e["type"], []).append(e)

    groups = []
    for type_name, group_entities in by_type.items():
        visited = set()
        for i, a in enumerate(group_entities):
            if a["node_id"] in visited:
                continue
            cluster = [a]
            visited.add(a["node_id"])
            for b in group_entities[i + 1:]:
                if b["node_id"] in visited:
                    continue
                if a["domain"] and b["domain"] and a["domain"].strip().lower() != b["domain"].strip().lower():
                    continue
                if is_acronym_match(a["name"], b["name"]):
                    cluster.append(b)
                    visited.add(b["node_id"])
            if len(cluster) > 1:
                groups.append(cluster)
    return groups


def pick_canonical(cluster: list[dict]) -> dict:
    """Prefer the longest, most descriptive name as canonical — e.g.
    'Machine Learning' over 'ML' or 'Machine Learning (ML)'."""
    def base_len(e):
        base, _ = extract_parenthetical(e["name"])
        return len(base)

    best = max(cluster, key=lambda e: (base_len(e), e.get("confidence") or 0))
    base_name, _ = extract_parenthetical(best["name"])
    return {**best, "canonical_display": base_name}


def merge_cluster(neo4j, cluster: list[dict], canonical_name: str) -> bool:
    node_ids = [e["node_id"] for e in cluster]
    try:
        with neo4j.driver.session() as session:
            session.run("""
                MATCH (n) WHERE id(n) IN $ids
                WITH collect(n) AS nodes
                CALL apoc.refactor.mergeNodes(nodes, {
                    properties: 'combine',
                    mergeRels: true
                })
                YIELD node
                SET node.name = $canonical_name
                RETURN node
            """, ids=node_ids, canonical_name=canonical_name)
        return True
    except Exception as e:
        print(f"    ✗ Merge failed: {e}")
        return False


def update_alias_memory(cluster: list[dict], canonical_name: str):
    path = Path(ALIAS_MEMORY_FILE)
    memory = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    for e in cluster:
        if e["name"].lower() == canonical_name.lower():
            continue
        key = e["name"].lower()
        memory[key] = {
            "canonical": canonical_name,
            "confidence": 1.0,
            "times_seen": memory.get(key, {}).get("times_seen", 1),
            "source": "manual_merge_cleanup"
        }
        base, abbrev = extract_parenthetical(e["name"])
        if abbrev:
            memory[abbrev.lower()] = {
                "canonical": canonical_name,
                "confidence": 1.0,
                "times_seen": 1,
                "source": "manual_merge_cleanup"
            }

    path.write_text(json.dumps(memory, indent=2), encoding="utf-8")


def update_entity_memory(cluster: list[dict], canonical_name: str):
    path = Path(ENTITY_MEMORY_FILE)
    if not path.exists():
        return
    memory = json.loads(path.read_text(encoding="utf-8"))

    for e in cluster:
        if e["name"].lower() == canonical_name.lower():
            continue
        key = f"{e['type']}::{e['name'].strip().lower()}"
        memory.pop(key, None)

    path.write_text(json.dumps(memory), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show candidates without changing anything")
    args = parser.parse_args()

    neo4j = Neo4jClient()
    entities = fetch_all_entities(neo4j)
    print(f"Scanned {len(entities)} entities.\n")

    groups = find_duplicate_groups(entities)

    if not groups:
        print("No acronym/abbreviation duplicates found.")
        neo4j.close()
        return

    print(f"Found {len(groups)} duplicate cluster(s):\n")

    merged_count = 0
    approve_all = False

    for cluster in groups:
        canonical = pick_canonical(cluster)
        canonical_name = canonical["canonical_display"]
        names = [e["name"] for e in cluster]

        print("=" * 60)
        print(f"Cluster: {', '.join(names)}")
        print(f"Proposed canonical: '{canonical_name}'")

        if args.dry_run:
            continue

        if not approve_all:
            choice = input("Merge? [y]es / [n]o / [a]ll remaining / [q]uit: ").strip().lower()
            if choice == "q":
                break
            if choice == "a":
                approve_all = True
            elif choice != "y":
                print("  Skipped.")
                continue

        if merge_cluster(neo4j, cluster, canonical_name):
            update_alias_memory(cluster, canonical_name)
            update_entity_memory(cluster, canonical_name)
            print(f"  ✓ Merged into '{canonical_name}'")
            merged_count += 1

    neo4j.close()

    if args.dry_run:
        print(f"\nDry run — {len(groups)} cluster(s) found, nothing changed.")
    else:
        print(f"\nDone. {merged_count}/{len(groups)} cluster(s) merged.")
        print("Restart KRONOS (Watcher + API) to pick up the cleaned-up graph.")


if __name__ == "__main__":
    main()