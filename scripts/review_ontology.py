import json
import sys
from pathlib import Path

PENDING_FILE = Path("pending_ontology.json")


def load_pending() -> dict:
    if not PENDING_FILE.exists():
        print("No pending_ontology.json found — nothing has hit friction yet.")
        sys.exit(0)
    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    pending = load_pending()
    to_review = {k: v for k, v in pending.items() if v["review_status"] == "pending_review"}

    if not to_review:
        collecting = sum(1 for v in pending.values() if v["review_status"] == "collecting")
        print(f"No proposals awaiting review. {collecting} pattern(s) still accumulating evidence.")
        return

    print(f"\n{len(to_review)} ontology expansion proposal(s) awaiting your review.\n")

    # Need OntologyEvolution to persist approve/reject decisions correctly
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agents.ontology_evolution import OntologyEvolution

    # mistral_client not needed for approve/reject, only for new proposals
    evolution = OntologyEvolution(mistral_client=None, model="unused")

    for key, entry in to_review.items():
        print("=" * 70)
        print(f"Raw phrasings seen: {', '.join(entry['raw_examples'])}")
        print(f"Occurred {entry['count']} times — currently force-mapped to '{entry['current_landing']}' (avg confidence {entry['avg_confidence']})")
        print(f"\nSuggested NEW type: {entry['suggested_canonical']}  (category: {entry['suggested_category']})")
        print(f"LLM confidence in suggestion: {entry.get('suggestion_confidence')}")
        print(f"Justification: {entry.get('justification', 'n/a')}")
        print("\nEvidence:")
        for e in entry["evidence"]:
            print(f"  - {e['from']} --[{key}]--> {e['to']}  ({e['source_doc']}): {e['description']}")

        choice = input("\n[a]pprove / [r]eject / [s]kip for now > ").strip().lower()

        if choice == "a":
            custom = input(f"Press enter to accept '{entry['suggested_canonical']}', or type a different name: ").strip()
            canonical = custom.upper() if custom else None
            evolution.approve(key, canonical=canonical)
            print(f"  Approved. '{key}' patterns will resolve directly from now on.")
        elif choice == "r":
            evolution.reject(key)
            print(f"  Rejected. Will keep using existing fallback mapping.")
        else:
            print("  Skipped — still pending for next review.")

    print("\nDone. Restart KRONOS agents to pick up any newly approved ontology extensions.")


if __name__ == "__main__":
    main()