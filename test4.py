from core.retrieval import Retriever
from core.synthesis import Synthesizer

r = Retriever()
synth = Synthesizer()

question = "Can artificial neural networks solve the XOR problem?"
result = r.retrieve(question, mentions=["XOR", "ANNs"])

# Confirm the CONTRADICTS edge actually made it into graph_facts before
# even checking synthesis — if it got filtered or capped out, the
# downstream test is meaningless
contradictions = [f for f in result["graph_facts"] if "CONTRADICTS" in f["relation_chain"]]
print(f"CONTRADICTS facts present: {len(contradictions)}")
for c in contradictions:
    print(f"  {c['anchor']} --{c['relation_chain']}--> {c['related_entity']}")

answer = synth.synthesize(question, result)
print(f"\nA: {answer['answer']}")

r.close()