"""
core/mention_extractor.py

Turns a raw user question into a list of candidate entity mention strings
for EntityLinker/Retriever to resolve against the graph. Uses the fast
Groq tier (config.EXTRACTOR_GROQ_MODEL_FAST) rather than the 70B model —
pulling mentions out of one short question is a much lighter task than
full-document extraction and doesn't need that quality bar.
"""
import json
import logging
from groq import Groq
from core.config import config
from utils.retry import call_with_retry

logger = logging.getLogger(__name__)

MENTION_EXTRACTION_PROMPT = """Extract the key entities, concepts, technologies, people, or organizations mentioned or implied in this question. Return ONLY a JSON array of short strings, nothing else — no explanation, no markdown fences.

Question: {question}

Example output: ["gradient descent", "overfitting", "neural networks"]

JSON array:"""


class MentionExtractor:
    def __init__(self):
        self.client = Groq(api_key=config.GROQ_API_KEY)
        self.model = config.EXTRACTOR_GROQ_MODEL_FAST

    def extract(self, question: str) -> list[str]:
        def _call():
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": MENTION_EXTRACTION_PROMPT.format(question=question)}],
                temperature=0.0,
                max_tokens=200,
            )
            raw = response.choices[0].message.content.strip()
            # Model sometimes wraps output in ```json fences despite instructions
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            return json.loads(raw)

        try:
            mentions = call_with_retry(_call, max_attempts=2, base_delay=1.0)
            if not isinstance(mentions, list):
                logger.warning(f"Mention extraction returned non-list: {mentions!r}")
                return []
            mentions = [m.strip() for m in mentions if isinstance(m, str) and m.strip()]
            logger.info(f"  Extracted mentions: {mentions}")
            return mentions
        except Exception as e:
            logger.error(f"Mention extraction failed for question {question!r}: {e}")
            return []