import logging
logging.basicConfig(level=logging.WARNING)

from agents.analyst import AnalystAgent

analyst = AnalystAgent()

questions = [
    "Who is Dr. Ahmed Khalil and where does he work?",
    "How many papers has Dr. Ahmed Khalil published?",
    "What is Dr. Ahmed Khalil's h-index?",
    "When was the AI Research Lab founded?",
    "What is Dr. Ahmed Khalil's research focus?",
]

for question in questions:
    print(f"\n{'='*60}")
    print(f"Q: {question}")
    print(f"{'='*60}")
    result = analyst.query(question)
    print(result["answer"])
    print(f"\n[Used: {result['chunks_used']} chunks, {result['claims_used']} claims, "
          f"{result['graph_entities']} graph entities, {result['conflicts_surfaced']} conflicts]")

analyst.close()