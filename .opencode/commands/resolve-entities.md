Read the current entity resolution logic in agents/codex.py (or wherever
the SequenceMatcher-based resolver lives). Cross-reference against
KRONOS_BLUEPRINT.md, Component 1 (Entity Resolver):

  Normalize -> Alias Memory lookup -> Embedding Similarity -> LLM Referee -> Canonical Entity

Implement or extend this pipeline as a modular class, where each stage
can be tested independently. Only escalate to the LLM Referee stage for
pairs that Normalize + Alias Memory + Embedding Similarity could not
confidently resolve, to keep API costs low. Output canonical entities
in this shape:

{
  "canonical_name": "...",
  "aliases": [],
  "confidence": 0.0,
  "reason": "normalize|alias_memory|embedding|llm_referee"
}
