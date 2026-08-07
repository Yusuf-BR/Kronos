"""
core/synthesis.py

Takes the output of Retriever.retrieve()/ask() — graph_facts and chunk_hits,
each already carrying provenance — and produces an answer where every claim
is tagged back to the specific fact or chunk that supports it. The model is
constrained to the provided context on purpose: if graph_facts and chunk_hits
don't actually contain an answer, it should say so rather than fill the gap
from its own training data. That's the difference between "connects dots in
your documents" and "makes up plausible-sounding connections."

Uses config.CURATOR_MODEL (mistral-large) rather than the fast tier —
synthesis needs to reason across multiple sources and refuse ungrounded
claims, which is a harder task than the single-shot extraction/linking work
elsewhere in the pipeline.
"""
import json
import logging
import time
from mistralai import Mistral
from core.config import config
from utils.retry import call_with_retry

logger = logging.getLogger(__name__)

SYNTHESIS_PROMPT = """You are answering a question using ONLY the numbered facts and passages below. Do not use any outside knowledge.

Rules:
- Every claim in your answer must end with a citation tag like [G1] or [C3], referencing the specific fact/passage it comes from.
- If the facts and passages below don't contain enough to answer the question, say so plainly instead of guessing.
- Do not combine two facts into a claim neither one actually supports — a citation should genuinely back the sentence it's attached to.
- Keep the answer concise. Do not restate every fact — synthesize.
- If any fact is marked [CONFLICT SIGNAL], your sources disagree — explicitly say so in the answer (e.g. "sources disagree on X: [G2] states A, while [G5] states B") rather than silently picking one side.

GRAPH FACTS (structural relationships from the knowledge graph):
{graph_facts_block}

PASSAGES (text excerpts from source documents):
{chunk_hits_block}

QUESTION: {question}

ANSWER:"""


def _format_graph_facts(graph_facts: list[dict]) -> str:
    if not graph_facts:
        return "(none)"
    lines = []
    for i, f in enumerate(graph_facts, 1):
        chain = " -> ".join(f["relation_chain"])
        flag = " [CONFLICT SIGNAL]" if "CONTRADICTS" in f["relation_chain"] else ""
        lines.append(f"[G{i}]{flag} {f['anchor']} --{chain}--> {f['related_entity']} ({f['related_type']}, confidence {f['score']:.2f})")
    return "\n".join(lines)


def _format_chunk_hits(chunk_hits: list[dict]) -> str:
    if not chunk_hits:
        return "(none)"
    lines = []
    for i, c in enumerate(chunk_hits, 1):
        lines.append(f"[C{i}] ({c['source_doc']}, p.{c['page']}): {c['text']}")
    return "\n".join(lines)


class Synthesizer:
    def __init__(self):
        self.client = Mistral(api_key=config.MISTRAL_API_KEY, timeout_ms=45000)
        self.model = getattr(config, "CURATOR_MODEL", "mistral-large-latest")

    def synthesize(self, question: str, retrieval_result: dict) -> dict:
        """
        retrieval_result: the dict returned by Retriever.retrieve()/ask()
        (must have graph_facts and chunk_hits keys).

        Returns {"answer": str, "graph_facts": [...], "chunk_hits": [...]}
        — the original fact/chunk lists are passed through so the caller
        can resolve [G1]/[C3] tags back to full provenance (source doc,
        page, confidence) for display, without re-parsing the answer text.
        """
        graph_facts = retrieval_result.get("graph_facts", [])
        chunk_hits = retrieval_result.get("chunk_hits", [])

        if not graph_facts and not chunk_hits:
            return {
                "answer": "I don't have any information about that in the knowledge base.",
                "graph_facts": [],
                "chunk_hits": [],
            }

        prompt = SYNTHESIS_PROMPT.format(
            graph_facts_block=_format_graph_facts(graph_facts),
            chunk_hits_block=_format_chunk_hits(chunk_hits),
            question=question,
        )

        def _call():
            time.sleep(getattr(config, "MISTRAL_RATE_LIMIT_DELAY", 0.2))
            response = self.client.chat.complete(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            return response.choices[0].message.content.strip(), response.usage

        try:
            t0 = time.perf_counter()
            answer_text, usage = call_with_retry(_call, max_attempts=2, base_delay=2.0)
            synthesis_s = round(time.perf_counter() - t0, 3)
            answer = answer_text
            token_usage = {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            }
        except Exception as e:
            logger.error(f"Synthesis failed for question {question!r}: {e}")
            answer = "Something went wrong generating an answer — the underlying facts were retrieved, but synthesis failed."
            synthesis_s = None
            token_usage = None

        return {
            "answer": answer,
            "graph_facts": graph_facts,
            "chunk_hits": chunk_hits,
            "synthesis_s": synthesis_s,
            "tokens": token_usage,
        }