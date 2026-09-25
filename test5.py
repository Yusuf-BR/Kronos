from core.retrieval import Retriever
from core.synthesis import Synthesizer

r = Retriever()
synth = Synthesizer()

result = r.ask("How does cross-validation help prevent overfitting?")
print(result["timings"])

answer = synth.synthesize("How does cross-validation help prevent overfitting?", result)
print(f"synthesis: {answer['synthesis_s']}s, tokens: {answer['tokens']}")