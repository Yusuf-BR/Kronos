import json
import logging
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from groq import Groq
from core.config import config
from utils.pdf_parser import extract_pdf, chunk_text

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """You are a knowledge extraction engine.
Given text from a document, extract:
1. Entities: people, organizations, concepts, technologies, locations, events
2. Relationships between those entities
3. Key claims: the most meaningful factual statements made in the text
4. A 3-sentence summary of the text

Respond ONLY with valid JSON in this exact format:
{
  "entities": [
    {"name": "entity name", "type": "PERSON|ORG|CONCEPT|TECHNOLOGY|LOCATION|EVENT", "description": "brief description"}
  ],
  "relationships": [
    {"from": "entity name", "from_type": "type", "to": "entity name", "to_type": "type", "relation": "SUPPORTS|CONTRADICTS|USES|BELONGS_TO|CREATED_BY|RELATED_TO|AUTHORED", "description": "brief description"}
  ],
  "claims": [
    "A precise factual statement extracted verbatim or near-verbatim from the text",
    "Another key claim or conclusion from the text"
  ],
  "summary": "3 sentence summary of the text"
}

Rules:
- Extract only what is explicitly stated, never infer
- Entity names must be consistent and canonical
- Minimum 2 entities, maximum 20 per chunk
- Claims must be precise, self-contained factual statements (3-8 per chunk)
- Claims should answer questions like: what happened, what was measured, what was concluded
- Return ONLY the JSON, no preamble, no markdown, no explanation before or after
"""


class ExtractorAgent:
    def __init__(self):
        self.backend = getattr(config, "EXTRACTOR_BACKEND", "groq").lower()

        if self.backend == "groq":
            self.groq_client = Groq(api_key=config.GROQ_API_KEY)
            self.groq_model = config.EXTRACTOR_GROQ_MODEL
            logger.info("ExtractorAgent initialized (backend=groq)")

        elif self.backend == "local":
            self.llm = ChatOpenAI(
                model=config.LOCAL_MODEL_NAME,
                base_url=config.LOCAL_MODEL_URL,
                api_key="not-needed",
                temperature=0.1,
            )
            logger.info(f"ExtractorAgent initialized (backend=local, model={config.LOCAL_MODEL_NAME})")

        else:
            self.llm = ChatGoogleGenerativeAI(
                model=config.EXTRACTOR_MODEL,
                google_api_key=config.GEMINI_API_KEY,
                temperature=0.1,
            )
            logger.info("ExtractorAgent initialized (backend=gemini)")

    def extract_knowledge(self, filepath: str) -> dict:
        logger.info(f"Extracting: {filepath}")
        parsed = extract_pdf(filepath)
        chunks = chunk_text(parsed["pages"])
        logger.info(f"  {len(parsed['pages'])} pages, {len(chunks)} chunks")

        all_entities = []
        all_relationships = []
        all_summaries = []
        all_claims = []

        for i, chunk in enumerate(chunks):
            logger.info(f"  Processing chunk {i+1}/{len(chunks)}")
            try:
                result = self._extract_chunk(
                    chunk["text"],
                    parsed["metadata"]["title"],
                    chunk["page"]
                )
                all_entities.extend(result.get("entities", []))
                all_relationships.extend(result.get("relationships", []))
                all_claims.extend([
                    {"text": claim, "page": chunk["page"], "source_doc": parsed["filename"]}
                    for claim in result.get("claims", [])
                ])
                if result.get("summary"):
                    all_summaries.append(result["summary"])
            except Exception as e:
                logger.warning(f"  Chunk {i+1} failed: {e}")
                continue

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
            "chunks": [
                {"text": c["text"], "page": c["page"], "source_doc": parsed["filename"]}
                for c in chunks
            ],
            "summary": " ".join(all_summaries[:3]),
            "tables": parsed["tables"]
        }

    def _extract_chunk(self, text: str, doc_title: str, page: int) -> dict:
        from utils.retry import call_with_retry
        from utils.quota import groq_quota

        if self.backend == "groq":
            def _call():
                groq_quota.acquire(agent_name="Extractor")
                response = self.groq_client.chat.completions.create(
                    model=self.groq_model,
                    messages=[
                        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                        {"role": "user", "content": f"Document: {doc_title} (page {page})\n\n{text}"}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1
                )
                return json.loads(response.choices[0].message.content)

            return call_with_retry(_call, max_attempts=5, base_delay=2.0)
        else:
            messages = [
                SystemMessage(content=EXTRACTION_SYSTEM_PROMPT),
                HumanMessage(content=f"Document: {doc_title} (page {page})\n\n{text}")
            ]
            response = self.llm.invoke(messages)
            return self._parse_json_response(response.content.strip())

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