# KRONOS Project — Agent Instructions

This project is building KRONOS v2, a self-improving knowledge intelligence
engine. Full architecture spec: **KRONOS_BLUEPRINT.md** (project root).

## Rules for every session

1. **Always read KRONOS_BLUEPRINT.md before implementing anything**, even if
   it was read earlier in the conversation — confirm the current component's
   spec (inputs, outputs, responsibilities) against it before writing code.

2. **Build components in this order** unless explicitly told otherwise:
   - v2: Entity Resolver (1) → Ontology Resolver (2) → Evidence Engine (3) →
     Confidence Engine (4) → Alias Memory (5) → Embedding Cache (6)
   - v3: Entity Memory (7) → Learning Engine (8) → Ontology Evolution (9) →
     Knowledge Statistics (10) → Provenance (11) → Contradiction Memory (12) →
     Source Reliability (13) → Relationship Confidence (14) →
     Knowledge Reinforcement (15) → Self-Evaluation (17)
   - v4: Cross-Document Reasoning (16) → Knowledge Feedback Loop (18)

   Don't skip ahead to a later component if an earlier dependency isn't
   built yet — e.g. Learning Engine (8) depends on Alias Memory (5) and
   Entity Memory (7) already existing.

3. **Match existing code style.** Before writing new code, check how the
   current v1 codebase is structured (naming conventions, module layout,
   error handling patterns) and follow it, rather than introducing a new
   style per component.

4. **Every component must be modular and independently testable.** Follow
   the blueprint's "Responsibilities" and "Output" sections literally —
   e.g. Component 1's output must match the exact JSON shape specified
   (`canonical_name`, `aliases`, `confidence`, `reason`).

5. **Never hardcode `confidence = 1.0`.** Per Component 4, confidence is
   always calculated as a product of upstream confidences, never assumed.

6. **After implementing a component, update its status.** Note in your
   response (or in a `PROGRESS.md` if one exists) which component was
   completed and what remains partial.

7. **Escalate to the `reasoning` subagent only for genuinely ambiguous
   judgment calls** (ontology mapping decisions, contradiction handling,
   confidence weighting) — not for routine code generation, to conserve
   OpenRouter's limited free quota.

## Execution style

**Work one step at a time, never a whole component in one shot.** Each
component in the blueprint is already broken into a pipeline (e.g.
Component 1 = Normalize → Alias Memory → Embedding Similarity → LLM
Referee). Implement exactly one stage, show the code, then STOP and wait
for explicit confirmation before moving to the next stage. Do not
pre-emptively write the next stage "for convenience." This keeps each
response small (avoids rate-limit issues) and lets the user review
incrementally.

If a request doesn't specify which stage/step to work on, ask which one
before writing code, rather than implementing the whole component.

## Current status

Run `/blueprint-status` at the start of any new work session to get a
fresh table of what's done, partial, or not started before proceeding.