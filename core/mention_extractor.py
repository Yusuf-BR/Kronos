"""
core/mention_extractor.py

Turns a raw user question into a list of candidate entity mention strings
for EntityLinker/Retriever to resolve against the graph. Uses the fast
Groq tier (config.EXTRACTOR_GROQ_MODEL_FAST) rather than the 70B model —
pulling mentions out of one short question is a much lighter task than
full-document extraction and doesn't need that quality bar.

Goes through the same shared groq_quota tracker as ExtractorAgent, since
both compete for the same daily/per-minute Groq limits — without this,
frequent question-answering could silently eat into the quota ingestion
depends on, in a way the tracker wouldn't even see coming.
"""
import json
import logging
from groq import Groq
from core.config import config
from utils.retry import call_with_retry
from utils.quota import groq_quota, QuotaExhaustedError

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
            # EXTRACTOR_GROQ_MODEL_FAST is the 8B tier, so large_model=False
            # — matches ExtractorAgent's single-chunk fast-path call.
            groq_quota.acquire(agent_name="MentionExtractor", large_model=False)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": MENTION_EXTRACTION_PROMPT.format(question=question)}],
                temperature=0.0,
                max_tokens=200,
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            return json.loads(raw)

        try:
            mentions = call_with_retry(_call, max_attempts=2, base_delay=1.0)
        except QuotaExhaustedError:
            # Not confirmed to be translated into DailyQuotaExceeded by
            # call_with_retry the way extractor.py's Groq path expects —
            # caught explicitly so a daily cap doesn't surface as a generic
            # extraction failure.
            logger.warning("Groq daily quota exhausted — mention extraction unavailable until reset")
            return []
        except Exception as e:
            logger.error(f"Mention extraction failed for question {question!r}: {e}")
            return []

        if not isinstance(mentions, list):
            logger.warning(f"Mention extraction returned non-list: {mentions!r}")
            return []
        mentions = [m.strip() for m in mentions if isinstance(m, str) and m.strip()]
        logger.info(f"  Extracted mentions: {mentions}")
        return mentions