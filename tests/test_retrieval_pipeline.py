"""
tests/test_retrieval_pipeline.py

Integration tests for the retrieval + synthesis pipeline (core/retrieval.py,
core/entity_linker.py, core/synthesis.py). These hit live Neo4j, Qdrant, and
LLM APIs — there's no mocked path, so this suite requires the same running
services main.py does, and the LLM-calling tests cost real tokens/time.
Not unit tests in the isolated sense; call it what it is if anyone asks.

Assumes the graph already contains MachineLearningTomMitchell.pdf and
Graduation_project_report.pdf (or equivalent) — several assertions depend
on specific entities/relations that exist in that corpus. If you re-run
this against a different corpus, the entity names below will need updating.

Run: pytest tests/test_retrieval_pipeline.py -v
"""
import pytest
from core.retrieval import Retriever
from core.synthesis import Synthesizer
import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("KRONOS_INTEGRATION_TEST"),
    reason="Needs real Neo4j/Qdrant/LLM clients — run with $env:KRONOS_INTEGRATION_TEST=1"
)

@pytest.fixture(scope="module")
def retriever():
    r = Retriever()
    yield r
    r.close()


@pytest.fixture(scope="module")
def synthesizer():
    return Synthesizer()


# ── Entity linking ──────────────────────────────────────────────────────

def test_known_entity_links_successfully(retriever):
    result = retriever.retrieve("what is machine learning?", mentions=["Machine learning"])
    assert len(result["linked_entities"]) == 1
    assert result["linked_entities"][0]["link_confidence"] > 0.9


def test_unknown_entity_falls_back_gracefully(retriever):
    """No match in the graph shouldn't crash — should degrade to vector-only."""
    result = retriever.retrieve(
        "what does the report say about quantum computing?",
        mentions=["quantum computing"]
    )
    assert result["linked_entities"] == []
    assert len(result["chunk_hits"]) > 0  # vector search still runs


# ── Graph traversal quality ─────────────────────────────────────────────

def test_no_self_referential_facts(retriever):
    """An entity should never show up as its own related_entity."""
    result = retriever.retrieve("what is machine learning?", mentions=["Machine learning"])
    self_loops = [f for f in result["graph_facts"] if f["anchor"] == f["related_entity"]]
    assert self_loops == []


def test_no_authorship_or_referential_noise(retriever):
    """AUTHORED_BY/COLLABORATED_WITH/REFERENCES etc. shouldn't reach graph_facts."""
    result = retriever.retrieve("what is machine learning?", mentions=["Machine learning"])
    noisy_types_present = {
        rel for f in result["graph_facts"] for rel in f["relation_chain"]
        if rel in retriever.noisy_relation_types
    }
    assert noisy_types_present == set()


def test_graph_facts_respects_cap(retriever):
    """A high-degree hub entity shouldn't blow past max_graph_facts regardless
    of how many paths actually exist."""
    result = retriever.retrieve(
        "what is machine learning?", mentions=["Machine learning"],
        max_hops=3, max_graph_facts=10
    )
    assert len(result["graph_facts"]) <= 10


def test_deduplicates_multiple_paths_to_same_entity(retriever):
    """Same (anchor, related_entity) pair reached via different paths should
    collapse to one fact, keeping the best-scoring path."""
    result = retriever.retrieve(
        "how does cross-validation relate to overfitting?",
        mentions=["Cross-Validation", "Overfitting"],
        max_hops=2
    )
    seen = [(f["anchor"], f["related_entity"]) for f in result["graph_facts"]]
    assert len(seen) == len(set(seen))


# ── Timing instrumentation ──────────────────────────────────────────────

def test_retrieve_reports_timings(retriever):
    result = retriever.retrieve("what is machine learning?", mentions=["Machine learning"])
    timings = result["timings"]
    for key in ("entity_linking_s", "graph_traversal_s", "vector_search_s", "total_s"):
        assert key in timings
        assert timings[key] >= 0


def test_ask_includes_mention_extraction_timing(retriever):
    result = retriever.ask("What is the ID3 algorithm used for?")
    assert "mention_extraction_s" in result["timings"]
    assert result["timings"]["total_s"] >= result["timings"]["mention_extraction_s"]


# ── Synthesis: grounding and refusal (the expensive, LLM-calling tests) ──

def test_synthesis_refuses_when_context_is_empty(synthesizer):
    """No graph_facts, no chunk_hits -> should say so, not fabricate."""
    empty_result = {"graph_facts": [], "chunk_hits": []}
    answer = synthesizer.synthesize("What does the report say about string theory?", empty_result)
    assert "don't have" in answer["answer"].lower() or "no information" in answer["answer"].lower()


def test_synthesis_cites_sources(retriever, synthesizer):
    """A question with real context should produce an answer with at least
    one citation tag — doesn't verify citation *accuracy* (that needs a
    human to read the answer against the sources), only that citations
    are present at all."""
    question = "How does cross-validation help prevent overfitting?"
    result = retriever.ask(question)
    answer = synthesizer.synthesize(question, result)
    assert "[G" in answer["answer"] or "[C" in answer["answer"]


def test_synthesis_flags_known_contradiction(retriever, synthesizer):
    """XOR/ANNs is a known CONTRADICTS pair in this corpus (single-layer
    perceptrons can't solve XOR; multilayer ANNs can) — the answer should
    surface the disagreement rather than silently picking a side."""
    question = "Can artificial neural networks solve the XOR problem?"
    result = retriever.retrieve(question, mentions=["XOR", "ANNs"])
    contradictions = [f for f in result["graph_facts"] if "CONTRADICTS" in f["relation_chain"]]
    assert len(contradictions) > 0, "Expected CONTRADICTS edge not found — corpus may have changed"

    answer = synthesizer.synthesize(question, result)
    assert "disagree" in answer["answer"].lower() or "contradict" in answer["answer"].lower()
