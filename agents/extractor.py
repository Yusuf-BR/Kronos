import json
import logging
import time
from collections import defaultdict, Counter
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from groq import Groq
from mistralai import Mistral
from core.config import config
from utils.pdf_parser import extract_pdf, chunk_text
from agents.evidence_engine import make_chunk_id
from agents.domain_classifier import DomainClassifier
from utils.retry import call_with_retry, DailyQuotaExceeded
from utils.quota import groq_quota

logger = logging.getLogger(__name__)

# ── Smart Routing Constants ──
GROQ_MAX_CHUNKS = 250          # Above this, route away from Groq preemptively
BATCH_SIZE = 3
ESTIMATED_TOKENS_PER_CHUNK = 1300

EXTRACTION_SYSTEM_PROMPT = """You are a knowledge extraction engine.
You will receive {batch_size} text segment(s) from a document. For EACH segment, extract:
1. Entities: people, organizations, concepts, technologies, locations, events
2. Relationships between those entities
3. Key claims: the most meaningful factual statements
4. A 1-sentence summary

Respond ONLY with valid JSON in this exact format:
{{
  "results": [
    {{
      "chunk_index": 0,
      "entities": [
        {{"name": "entity name", "type": "PERSON|ORG|CONCEPT|TECHNOLOGY|LOCATION|EVENT", "description": "brief description"}}
      ],
      "relationships": [
        {{"from": "entity name", "from_type": "type", "to": "entity name", "to_type": "type", "relation": "RELATION_TYPE", "description": "brief description"}}
      ],
      "claims": [
        "A precise factual statement"
      ],
      "summary": "1 sentence summary"
    }}
  ]
}}

The "relation" field MUST be one of:
SUPPORTS, CONTRADICTS, RELATED_TO, SIMILAR_TO, SAME_AS, DIFFERENT_FROM,
IS_A, SUBSET_OF, PART_OF, BELONGS_TO, CONTAINS,
CREATED_BY, DEVELOPED_BY, AUTHORED_BY, EMPLOYED_BY, WORKED_AT, CONTRIBUTED_TO, COLLABORATED_WITH, MENTORED,
USES, USED_FOR, USED_IN, USED_BY, USED_WITH, APPLIES_TO, APPLICABLE_TO,
ENABLES, MEASURES, EVALUATES, REQUIRES, CREATES, GENERATES, ADDRESSES,
REFERENCES, PRECEDES, FOLLOWS, AFFECTS, INFLUENCES, TARGETS, COMPATIBLE_WITH

Rules:
- Extract only what is explicitly stated, never infer
- Entity names must be consistent and canonical across ALL segments
- Minimum 2 entities, maximum 20 per segment
- Claims must be precise, self-contained factual statements (3-8 per segment)
- Return ONLY the JSON, no preamble, no markdown, no explanation before or after
"""

EXTRACTION_CONFIDENCE_BY_TIER = {
    1: 0.95,
    2: 0.85,
    3: 0.70,
}
DEFAULT_TIER_CONFIDENCE = 0.75
FALLBACK_CONFIDENCE_PENALTY = 0.9


def compute_extraction_confidence(tier: int, quality_score: float, used_fallback: bool = False) -> float:
    tier_confidence = EXTRACTION_CONFIDENCE_BY_TIER.get(tier, DEFAULT_TIER_CONFIDENCE)
    quality_score = max(0.0, min(1.0, quality_score if quality_score is not None else 1.0))
    result = tier_confidence * quality_score
    if used_fallback:
        result *= FALLBACK_CONFIDENCE_PENALTY
    return round(result, 4)


class ExtractorAgent:
    def __init__(self):
        self.backend_preference = getattr(config, "EXTRACTOR_BACKEND", "groq").lower()
        self.large_doc_backend = getattr(config, "EXTRACTOR_LARGE_DOC_BACKEND", "mistral").lower()
        self.current_tier = 1
        self._active_backend = self.backend_preference

        # ── Initialize ALL available clients (routing picks at runtime) ──
        self.groq_client = None
        self.groq_model = None
        self.groq_model_fast = None
        if getattr(config, "GROQ_API_KEY", None):
            self.groq_client = Groq(api_key=config.GROQ_API_KEY)
            self.groq_model = getattr(config, "EXTRACTOR_GROQ_MODEL", "llama-3.3-70b-versatile")
            self.groq_model_fast = getattr(config, "EXTRACTOR_GROQ_MODEL_FAST", "llama-3.1-8b-instant")

        # Gemini kept available as a manual/opt-in backend only. It is
        # NOT auto-selected for large documents anymore — its free tier
        # has a hard 1,500 req/day cap that has already been exhausted
        # in practice, unlike Mistral which has no daily cap.
        self.gemini_llm = None
        if getattr(config, "GEMINI_API_KEY", None):
            self.gemini_llm = ChatGoogleGenerativeAI(
                model=getattr(config, "EXTRACTOR_MODEL", "gemini-1.5-flash"),
                google_api_key=config.GEMINI_API_KEY,
                temperature=0.1,
            )

        self.local_llm = None
        if getattr(config, "LOCAL_MODEL_URL", None):
            self.local_llm = ChatOpenAI(
                model=config.LOCAL_MODEL_NAME,
                base_url=config.LOCAL_MODEL_URL,
                api_key="not-needed",
                temperature=0.1,
            )

        # Mistral is always available — no daily cap, primary route for
        # large documents and the universal fallback when Groq's daily
        # quota runs out mid-run.
        self.mistral_client = Mistral(api_key=config.MISTRAL_API_KEY, timeout_ms=45000)
        self.fallback_model = getattr(config, "EXTRACTOR_FALLBACK_MODEL", "mistral-small-2506")

        self._domain_mistral = Mistral(api_key=config.MISTRAL_API_KEY, timeout_ms=30000)
        self.domain_classifier = DomainClassifier(
            self._domain_mistral,
            model=getattr(config, "DOMAIN_MODEL", "mistral-small-2506")
        )

        logger.info(
            f"ExtractorAgent initialized (preference={self.backend_preference}, "
            f"large_doc_backend={self.large_doc_backend}, "
            f"groq={'yes' if self.groq_client else 'no'}, "
            f"gemini={'yes' if self.gemini_llm else 'no (manual only)'}, "
            f"local={'yes' if self.local_llm else 'no'})"
        )

    def _select_backend(self, chunk_count: int) -> str:
        """
        Smart routing: don't burn Groq's daily quota (or hit its tighter
        rate limits) on large documents. Tier 3 documents route to
        Mistral unconditionally — Groq's free-tier limits have proven
        too tight for sustained Tier-3 batch processing even under our
        own quota tracker, causing long 429 retry chains.
        """
        if self.current_tier == 3:
            logger.info(f"  Tier 3 document — routing directly to {self.large_doc_backend} (avoids Groq rate-limit chains)")
            return self.large_doc_backend

        if self.backend_preference == "groq":
            estimated_tokens = chunk_count * ESTIMATED_TOKENS_PER_CHUNK
            if chunk_count > GROQ_MAX_CHUNKS:
                logger.info(
                    f"  Large document ({chunk_count} chunks, ~{estimated_tokens:,} tokens) — "
                    f"routing to {self.large_doc_backend} (no daily cap) to preserve Groq quota"
                )
                return self.large_doc_backend
        return self.backend_preference

    def extract_knowledge(self, filepath: str) -> dict:
        logger.info(f"Extracting: {filepath}")
        parsed = extract_pdf(filepath)
        tier = parsed.get("tier", 1)
        self.current_tier = tier
        quality_score = parsed.get("quality_score", 1.0)

        sample_text = " ".join(p["text"] for p in parsed["pages"][:2])[:1500]
        domain_result = self.domain_classifier.classify(parsed["metadata"]["title"], sample_text)
        domain = domain_result.get("domain")
        logger.info(f"  Classified domain: '{domain}' (confidence {domain_result.get('confidence', 0)}, status={domain_result.get('status', 'unknown')})")

        chunks = chunk_text(parsed["pages"], tier=tier)

        if tier == 3:
            from utils.pdf_parser import get_priority_chunks
            chunks = get_priority_chunks(chunks, tier=3)
            logger.info(f"  Tier 3: processing {len(chunks)} priority chunks")

        # ── Smart backend selection for THIS document ──
        self._active_backend = self._select_backend(len(chunks))
        logger.info(f"  {len(parsed['pages'])} pages, {len(chunks)} chunks, tier {tier}, backend={self._active_backend}")

        all_entities = []
        all_relationships = []
        all_summaries = []
        all_claims = []

        # ── Batch processing loop ──
        batch_size = 1 if self._active_backend == "local" else BATCH_SIZE

        for batch_start in range(0, len(chunks), batch_size):
            batch = chunks[batch_start:batch_start + batch_size]
            batch_indices = list(range(batch_start, min(batch_start + batch_size, len(chunks))))

            logger.info(f"  Processing batch {batch_start//batch_size + 1}/{(len(chunks)-1)//batch_size + 1} "
                       f"(chunks {batch_indices[0]+1}-{batch_indices[-1]+1}) [{batch[0].get('section', 'body')}]")

            try:
                results, used_fallback = self._extract_batch(batch, parsed["metadata"]["title"])
            except Exception as e:
                logger.warning(f"  Batch failed, trying single-chunk fallback: {e}")
                results = []
                used_fallback = self._active_backend != self.backend_preference
                for idx, chunk in zip(batch_indices, batch):
                    try:
                        r, uf = self._extract_chunk(chunk["text"], parsed["metadata"]["title"], chunk["page"])
                        results.append(r)
                        used_fallback = used_fallback or uf
                    except Exception as e2:
                        logger.warning(f"  Chunk {idx+1} failed: {e2}")
                        results.append(None)

            extraction_confidence = compute_extraction_confidence(tier, quality_score, used_fallback)

            for chunk_idx, result in zip(batch_indices, results):
                if result is None:
                    continue
                chunk = chunks[chunk_idx]
                chunk_id = make_chunk_id(parsed["filename"], chunk["page"], chunk_idx, chunk["text"])

                entities = result.get("entities", [])
                relationships = result.get("relationships", [])

                for e in entities:
                    e["_source_doc"] = parsed["filename"]
                    e["_page"] = chunk["page"]
                    e["_chunk_id"] = chunk_id
                    e["_excerpt"] = chunk["text"]
                    e["_extraction_confidence"] = extraction_confidence
                    e["_domain"] = domain

                for r in relationships:
                    r["_source_doc"] = parsed["filename"]
                    r["_page"] = chunk["page"]
                    r["_chunk_id"] = chunk_id
                    r["_excerpt"] = chunk["text"]
                    r["_extraction_confidence"] = extraction_confidence
                    r["_domain"] = domain

                all_entities.extend(entities)
                all_relationships.extend(relationships)
                raw_claims = result.get("claims", [])
                valid_claims = []
                for claim in raw_claims:
                    if isinstance(claim, str) and claim.strip():
                        valid_claims.append(claim)
                    else:
                        logger.warning(f"  Discarding malformed claim (not a string): {claim!r}")
                all_claims.extend([
                    {"text": claim, "page": chunk["page"], "source_doc": parsed["filename"]}
                    for claim in valid_claims
                ])
                if result.get("summary"):
                    all_summaries.append(result["summary"])

        # ── Type Reconciliation ──
        # Defensive: an entity/relationship dict missing required keys
        # (malformed LLM output — same failure class as the claims-list
        # bug) must never crash the whole document. A single bad entity
        # here used to trigger a full extract_knowledge() retry via the
        # Watcher's call_with_retry wrapper — re-parsing, re-chunking,
        # and re-running all 31 Groq batches from scratch. Now it's just
        # dropped and logged.
        valid_all_entities = []
        for e in all_entities:
            if not isinstance(e, dict) or not e.get("name") or not e.get("type"):
                logger.warning(f"  Discarding malformed entity (missing name/type): {e!r}")
                continue
            valid_all_entities.append(e)
        all_entities = valid_all_entities

        name_type_votes = defaultdict(Counter)
        for e in all_entities:
            name_type_votes[e["name"].lower()][e["type"]] += 1

        canonical_type_for_name = {
            name: votes.most_common(1)[0][0]
            for name, votes in name_type_votes.items()
        }

        for e in all_entities:
            e["type"] = canonical_type_for_name[e["name"].lower()]

        valid_all_relationships = []
        for r in all_relationships:
            if not isinstance(r, dict) or not all(r.get(k) for k in ("from", "to", "from_type", "to_type", "relation")):
                logger.warning(f"  Discarding malformed relationship: {r!r}")
                continue
            valid_all_relationships.append(r)
        all_relationships = valid_all_relationships

        for r in all_relationships:
            r["from_type"] = canonical_type_for_name.get(r["from"].lower(), r["from_type"])
            r["to_type"] = canonical_type_for_name.get(r["to"].lower(), r["to_type"])

        seen = set()
        unique_entities = []
        for e in all_entities:
            key = (e["name"].lower(), e["type"])
            if key not in seen:
                seen.add(key)
                unique_entities.append(e)

        return {
            "filename": parsed["filename"],
            "filepath": parsed["filepath"],
            "metadata": parsed["metadata"],
            "entities": unique_entities,
            "relationships": all_relationships,
            "claims": all_claims,
            "chunks": [{"text": c["text"], "page": c["page"], "source_doc": parsed["filename"], "section": c.get("section", "body")} for c in chunks],
            "summary": " ".join(all_summaries[:3]),
            "tables": parsed["tables"],
            "tier": tier,
            "quality_score": quality_score,
            "ocr_pages": parsed.get("ocr_pages", []),
            "domain": domain
        }

    def _extract_batch(self, batch: list[dict], doc_title: str) -> tuple[list[dict], bool]:
        """
        Extract multiple chunks in one API call. Returns (list_of_results, used_fallback).
        """
        batch_text = "\n\n".join(
            f"Segment {j+1} (page {chunk['page']}):\n{chunk['text']}"
            for j, chunk in enumerate(batch)
        )

        if self._active_backend == "groq":
            return self._call_groq_batch(batch, batch_text, doc_title)
        elif self._active_backend == "gemini":
            return self._call_gemini_batch(batch, batch_text, doc_title)
        elif self._active_backend == "local":
            return self._call_local_batch(batch, batch_text, doc_title)
        else:
            # Mistral direct — the default large-doc route and the
            # universal fallback for everything else
            return self._call_mistral_batch(batch, batch_text, doc_title)

    def _call_groq_batch(self, batch: list[dict], batch_text: str, doc_title: str) -> tuple[list[dict], bool]:
        is_large_model = self.current_tier < 3
        model = self.groq_model if is_large_model else self.groq_model_fast

        def _call():
            groq_quota.acquire(agent_name="Extractor", large_model=is_large_model)
            response = self.groq_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT.format(batch_size=len(batch))},
                    {"role": "user", "content": f"Document: {doc_title}\n\n{batch_text}"}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            return json.loads(response.choices[0].message.content)

        try:
            raw = call_with_retry(_call, max_attempts=5, base_delay=2.0)
            if self.current_tier == 3:
                time.sleep(2.5)
            return self._parse_batch_response(raw, len(batch)), False

        except DailyQuotaExceeded:
            logger.warning(f"  Groq daily quota exhausted on batch — switching entire document to Mistral (no daily cap)")
            self._active_backend = "mistral"
            return self._call_mistral_batch(batch, batch_text, doc_title)

    def _call_gemini_batch(self, batch: list[dict], batch_text: str, doc_title: str) -> tuple[list[dict], bool]:
        """
        Manual/opt-in path only — not auto-selected by _select_backend
        anymore. If Gemini's daily cap is exhausted this will fail
        immediately and fall back to Mistral, same as any other failure.
        """
        messages = [
            SystemMessage(content=EXTRACTION_SYSTEM_PROMPT.format(batch_size=len(batch))),
            HumanMessage(content=f"Document: {doc_title}\n\n{batch_text}")
        ]
        try:
            response = self.gemini_llm.invoke(messages)
            raw = self._parse_json_response(response.content.strip())
            return self._parse_batch_response(raw, len(batch)), False
        except Exception as e:
            logger.warning(f"  Gemini batch failed ({e}) — falling back to Mistral")
            return self._call_mistral_batch(batch, batch_text, doc_title)

    def _call_mistral_batch(self, batch: list[dict], batch_text: str, doc_title: str) -> tuple[list[dict], bool]:
        def _call():
            time.sleep(getattr(config, "MISTRAL_RATE_LIMIT_DELAY", 0.2))
            response = self.mistral_client.chat.complete(
                model=self.fallback_model,
                messages=[
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT.format(batch_size=len(batch))},
                    {"role": "user", "content": f"Document: {doc_title}\n\n{batch_text}"}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            return json.loads(response.choices[0].message.content)

        raw = call_with_retry(_call, max_attempts=3, base_delay=3.0)
        return self._parse_batch_response(raw, len(batch)), True

    def _call_local_batch(self, batch: list[dict], batch_text: str, doc_title: str) -> tuple[list[dict], bool]:
        messages = [
            SystemMessage(content=EXTRACTION_SYSTEM_PROMPT.format(batch_size=len(batch))),
            HumanMessage(content=f"Document: {doc_title}\n\n{batch_text}")
        ]
        response = self.local_llm.invoke(messages)
        raw = self._parse_json_response(response.content.strip())
        return self._parse_batch_response(raw, len(batch)), False

    def _extract_chunk(self, text: str, doc_title: str, page: int) -> tuple[dict, bool]:
        """
        Legacy single-chunk path. Used only when batch fails and falls back.
        """
        if self._active_backend == "groq":
            is_large_model = self.current_tier < 3
            model = self.groq_model if is_large_model else self.groq_model_fast

            def _call_groq():
                groq_quota.acquire(agent_name="Extractor", large_model=is_large_model)
                response = self.groq_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT.format(batch_size=1)},
                        {"role": "user", "content": f"Document: {doc_title} (page {page})\n\n{text}"}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1
                )
                return json.loads(response.choices[0].message.content)

            try:
                result = call_with_retry(_call_groq, max_attempts=5, base_delay=2.0)
                if self.current_tier == 3:
                    time.sleep(2.5)
                return result, False
            except DailyQuotaExceeded:
                logger.warning(f"  Falling back to Mistral for this chunk")
                return self._call_mistral_single(text, doc_title, page), True

        elif self._active_backend == "gemini":
            messages = [
                SystemMessage(content=EXTRACTION_SYSTEM_PROMPT.format(batch_size=1)),
                HumanMessage(content=f"Document: {doc_title} (page {page})\n\n{text}")
            ]
            try:
                response = self.gemini_llm.invoke(messages)
                return self._parse_json_response(response.content.strip()), False
            except Exception as e:
                logger.warning(f"  Gemini single-chunk failed ({e}) — falling back to Mistral")
                return self._call_mistral_single(text, doc_title, page), True

        elif self._active_backend == "local":
            messages = [
                SystemMessage(content=EXTRACTION_SYSTEM_PROMPT.format(batch_size=1)),
                HumanMessage(content=f"Document: {doc_title} (page {page})\n\n{text}")
            ]
            response = self.local_llm.invoke(messages)
            return self._parse_json_response(response.content.strip()), False

        else:
            return self._call_mistral_single(text, doc_title, page), True

    def _call_mistral_single(self, text: str, doc_title: str, page: int) -> dict:
        def _call():
            time.sleep(getattr(config, "MISTRAL_RATE_LIMIT_DELAY", 0.2))
            response = self.mistral_client.chat.complete(
                model=self.fallback_model,
                messages=[
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT.format(batch_size=1)},
                    {"role": "user", "content": f"Document: {doc_title} (page {page})\n\n{text}"}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            return json.loads(response.choices[0].message.content)

        return call_with_retry(_call, max_attempts=3, base_delay=3.0)

    @staticmethod
    def _parse_batch_response(raw: dict, expected_count: int) -> list[dict]:
        """
        Extract individual chunk results from a batch response.
        Handles: {"results": [...]} or {"chunk_index": 0, ...} (single) or [...] (array).
        """
        if raw is None:
            return [None] * expected_count

        if isinstance(raw, dict) and "results" in raw:
            results = raw["results"]
            if isinstance(results, list) and len(results) == expected_count:
                return results
            while len(results) < expected_count:
                results.append({"entities": [], "relationships": [], "claims": [], "summary": ""})
            return results[:expected_count]

        if isinstance(raw, dict) and "entities" in raw:
            return [raw] + [None] * (expected_count - 1)

        if isinstance(raw, list):
            while len(raw) < expected_count:
                raw.append({"entities": [], "relationships": [], "claims": [], "summary": ""})
            return raw[:expected_count]

        return [None] * expected_count

    @staticmethod
    def _parse_json_response(raw: str) -> dict:
        text = raw.strip()
        if "```" in text:
            parts = text.split("```")
            candidate = parts[1] if len(parts) > 1 else text
            if candidate.lstrip().startswith("json"):
                candidate = candidate.lstrip()[4:]
            text = candidate.strip()
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                text = text[start:end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"  Failed to parse model output as JSON: {e}")
            logger.debug(f"  Raw output was: {raw[:500]}")
            raise