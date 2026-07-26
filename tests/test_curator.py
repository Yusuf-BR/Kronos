import logging
logging.basicConfig(level=logging.INFO)

from agents.curator import CuratorAgent

curator = CuratorAgent()
result = curator.run()

print(f"\n=== CURATION COMPLETE ===")
print(f"Timestamp:        {result['timestamp']}")
print(f"Decayed nodes:    {result['decayed_nodes']}")
print(f"Stale entities:   {result['stale_entities']}")
print(f"Active conflicts: {result['active_conflicts']}")
print(f"Digest saved to:  {result['digest_path']}")

print(f"\n=== KNOWLEDGE DIGEST PREVIEW ===")
with open(result['digest_path'], 'r', encoding='utf-8') as f:
    print(f.read())

curator.close()