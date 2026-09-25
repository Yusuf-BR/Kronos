from core.retrieval import Retriever

r = Retriever()
result = r.retrieve(
    query="how does cross-validation relate to overfitting?",
    mentions=["Cross-Validation", "Overfitting"]
)
self_loops = [f for f in result["graph_facts"] if f["anchor"] == f["related_entity"]]
print(f"Self-loops found: {len(self_loops)}")
print(f"Total graph_facts: {len(result['graph_facts'])}")
r.close()