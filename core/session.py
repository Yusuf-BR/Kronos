"""
core/session.py

Lightweight multi-turn conversation state on top of Retriever/Synthesizer.
In-memory, process-local — lost on restart. Fine for a single-process
demo; would need a real store (redis, sqlite, or LangGraph's own
checkpointer) before this runs behind a multi-worker API.
"""
import logging
from collections import deque
from core.retrieval import Retriever
from core.synthesis import Synthesizer

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 5  # how many prior turns feed into context for coreference


class ConversationSession:
    def __init__(self, retriever: Retriever, synthesizer: Synthesizer, session_id: str):
        self.retriever = retriever
        self.synthesizer = synthesizer
        self.session_id = session_id
        self.turns = deque(maxlen=MAX_HISTORY_TURNS)

    def _build_context_block(self) -> str:
        """
        Questions only, not answers — feeding the full prior answer text
        into mention extraction caused it to over-extract every entity
        name mentioned anywhere in that answer, not just the one the
        current question's pronoun actually refers to. Trades away
        resolving references to things only named in a prior *answer*
        (not asked about directly) for much tighter, predictable
        extraction on the common case.

        No manual slicing needed — self.turns is a deque(maxlen=
        MAX_HISTORY_TURNS), so it already never holds more than that many
        turns; iterating the whole thing is already bounded.
        """
        if not self.turns:
            return ""
        return "\n".join(f"Q: {t['question']}" for t in self.turns)

    def _last_linked_entity_names(self) -> list[str]:
        if not self.turns:
            return []
        return [e["name"] for e in self.turns[-1]["linked_entities"]]

    def ask(self, question: str, domain: str | None = None, **kwargs) -> dict:
        context_block = self._build_context_block()
        mentions = self.retriever.mention_extractor.extract(question, context=context_block)

        # Referential follow-up with nothing new extracted ("what about its
        # limitations?") — reuse whatever was linked last turn rather than
        # losing the thread with an empty mentions list.
        if not mentions and self.turns:
            mentions = self._last_linked_entity_names()
            logger.info(f"  No new mentions extracted — reusing prior turn's entities: {mentions}")

        result = self.retriever.retrieve(question, mentions, domain=domain, **kwargs)
        answer = self.synthesizer.synthesize(question, result)

        self.turns.append({
            "question": question,
            "mentions": mentions,
            "linked_entities": result["linked_entities"],
            "answer": answer["answer"],
        })
        return answer