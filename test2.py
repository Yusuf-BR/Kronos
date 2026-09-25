from core.retrieval import Retriever
from core.synthesis import Synthesizer

r = Retriever()
synth = Synthesizer()
result = r.ask("What does the report say about transformer architectures and attention mechanisms?")
for i, c in enumerate(result["chunk_hits"], 1):
    print(f"[C{i}] ({c['source_doc']}, p.{c['page']}, score {c['score']:.3f}): {c['text'][:200]}")


r.close()