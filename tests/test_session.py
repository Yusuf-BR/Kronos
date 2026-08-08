"""
tests/test_session.py

Two different kinds of test here, deliberately split:

- test_context_block_* : pure logic, no live services needed. Runs in
  the default (mocked) pytest mode like everything except
  test_retrieval_pipeline.py/test_diagnostic.py.
- test_coreference_*   : needs real Neo4j/Qdrant/LLM clients end to end,
  gated the same way as the rest of the integration suite.
"""
import os
import pytest
from core.session import ConversationSession, MAX_HISTORY_TURNS


# ── Fast: context-block formatting, no live services needed ────────────

def _fake_session():
    # _build_context_block only touches self.turns — retriever/synthesizer
    # are never called by these tests, so None is safe here.
    return ConversationSession(retriever=None, synthesizer=None, session_id="test")


def test_context_block_empty_with_no_turns():
    session = _fake_session()
    assert session._build_context_block() == ""


def test_context_block_includes_questions_only_not_answers():
    """Regression test for the over-extraction bug: feeding full prior
    answers into mention extraction caused it to pull in every entity
    named anywhere in the answer, not just what the pronoun referred to.
    Context block must carry questions only."""
    session = _fake_session()
    session.turns.append({
        "question": "What is overfitting?",
        "mentions": ["overfitting"],
        "linked_entities": [{"name": "Overfitting", "type": "CONCEPT"}],
        "answer": "Overfitting involves Rule Post-Pruning, Stopping Criterion, "
                  "Decision Tree Learning, and Artificial Neural Networks.",
    })
    block = session._build_context_block()
    assert "What is overfitting?" in block
    # The whole point of the fix — none of the answer's entity names
    # should leak into the context block the extractor sees.
    assert "Rule Post-Pruning" not in block
    assert "Artificial Neural Networks" not in block


def test_context_block_respects_history_limit():
    session = _fake_session()
    for i in range(MAX_HISTORY_TURNS + 3):
        session.turns.append({
            "question": f"Question number {i}",
            "mentions": [], "linked_entities": [], "answer": "",
        })
    block = session._build_context_block()
    # Oldest turns should have been dropped, most recent should remain
    assert "Question number 0" not in block
    assert f"Question number {MAX_HISTORY_TURNS + 2}" in block
    
def test_turns_storage_is_bounded():
    """self.turns must not grow unboundedly — a deque(maxlen=...) should
    evict the oldest turn once the cap is reached, not just cap what's
    fed into the prompt while still holding everything in memory."""
    session = _fake_session()
    for i in range(MAX_HISTORY_TURNS + 5):
        session.turns.append({
            "question": f"q{i}", "mentions": [], "linked_entities": [], "answer": "",
        })
    assert len(session.turns) == MAX_HISTORY_TURNS
    assert session.turns[0]["question"] == f"q{5}"  # oldest 5 evicted

# ── Integration: real end-to-end coreference resolution ─────────────────

pytestmark_integration = pytest.mark.skipif(
    not os.getenv("KRONOS_INTEGRATION_TEST"),
    reason="Needs real Neo4j/Qdrant/LLM clients — run with $env:KRONOS_INTEGRATION_TEST=1"
)


@pytestmark_integration
def test_coreference_resolves_pronoun_to_prior_entity():
    """The actual regression test for today's fix: a vague follow-up
    ('it') should resolve to the single entity from the prior turn,
    not sweep in every entity mentioned in the prior answer's text."""
    from core.retrieval import Retriever
    from core.synthesis import Synthesizer

    r = Retriever()
    synth = Synthesizer()
    session = ConversationSession(r, synth, session_id="pytest-coref")

    session.ask("What is overfitting?")
    session.ask("What are some ways to address it?")

    mentions_used = session.turns[1]["mentions"]
    assert "overfitting" in [m.lower() for m in mentions_used]
    # The over-extraction bug pulled in 8 unrelated entities from the
    # prior answer's text — a tight resolution should stay small.
    assert len(mentions_used) <= 3

    r.close()
