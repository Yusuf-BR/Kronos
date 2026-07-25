import json
import hashlib
import logging
import time
from difflib import SequenceMatcher
from pathlib import Path

from utils.atomic_json import atomic_write_json

logger = logging.getLogger(__name__)

DOMAIN_MEMORY_FILE = "domain_memory.json"
DOMAIN_SIMILARITY_THRESHOLD = 0.85
MIN_DOMAIN_DOCUMENTS = 3  # Below this = niche domain, flagged for review

CLASSIFY_PROMPT = """You are a domain classification engine for a knowledge graph.
Given a document's title and a sample of its text, identify the single broad
subject-matter domain it belongs to (e.g. "Machine Learning", "Medical",
"Law", "Finance", "Electrical Engineering", "History" — do not limit yourself
to this list, propose whatever domain genuinely fits).

Respond ONLY with valid JSON:
{"domain": "Domain Name", "confidence": 0.0-1.0}

Use a short, canonical, human-readable domain name (2-3 words max), consistent
in style so the same domain is named the same way across different documents.
"""

PARENT_PROMPT = """You are a taxonomy expert. A knowledge graph system has
discovered a new subject-matter domain: "{new_domain}".

Existing domains in the system: {existing_domains}

Is "{new_domain}" a subfield, specialization, or child of any existing domain?
If yes, respond with the exact name of the parent domain from the list above.
If no, or if it is a top-level field, respond with null.

Respond ONLY with valid JSON: {"parent": "Exact Domain Name" | null}
"""


class DomainClassifier:
    """
    Self-aware domain taxonomy for KRONOS.

    Guarantees:
    1. Same document content → same domain forever (survives renaming)
    2. Similar domain names → collapsed to canonical (Data Science = DataScience)
    3. Nested domains → parent/child hierarchy learned automatically
    4. Niche domains → flagged, never silently lost or auto-promoted
    5. Full introspection → the system can explain its own taxonomy

    Persistence shape (domain_memory.json):
    {
      "domains": {
        "machinelearning": {
          "canonical": "Machine Learning",
          "times_seen": 50,
          "parent": "Computer Science",
          "first_seen": "2026-07-24T19:00:00",
          "last_seen": "2026-07-24T19:00:00"
        }
      },
      "doc_cache": {
        "a1b2c3...": "Machine Learning"
      }
    }
    """

    def __init__(self, mistral_client, model: str = "mistral-small-2506",
                 memory_file: str = DOMAIN_MEMORY_FILE):
        self.mistral_client = mistral_client
        self.model = model
        self.memory_file = Path(memory_file)
        self.domains: dict = {}      # domain_key -> metadata
        self.doc_cache: dict = {}    # content_hash -> canonical domain name
        self._load()
        logger.info(
            f"DomainClassifier initialized — {len(self.domains)} domains, "
            f"{len(self.doc_cache)} cached documents, "
            f"{self._count_niche_domains()} niche domains flagged"
        )

    # ── Persistence ──

    def _load(self):
        if not self.memory_file.exists():
            self.domains = {}
            self.doc_cache = {}
            return
        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Migrate legacy flat format {key: {...}} → new structured format
            if isinstance(data, dict) and "domains" not in data:
                logger.info("  Migrating legacy domain_memory.json to structured format")
                self.domains = data
                self.doc_cache = {}
            else:
                self.domains = data.get("domains", {})
                self.doc_cache = data.get("doc_cache", {})
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load domain memory, starting fresh: {e}")
            self.domains = {}
            self.doc_cache = {}

    def _save(self):
        atomic_write_json(str(self.memory_file), {
            "domains": self.domains,
            "doc_cache": self.doc_cache
        })

    # ── Core Classification ──

    def classify(self, doc_title: str, sample_text: str) -> dict:
        """
        Returns {
            "domain": str,
            "confidence": float,
            "status": "cache" | "fuzzy" | "llm" | "fallback",
            "is_new_domain": bool
        }
        """
        doc_hash = self._doc_hash(doc_title, sample_text)

        # 1. Exact document seen before? (renamed PDF, forced reprocess)
        cached = self.doc_cache.get(doc_hash)
        if cached and cached.lower() in self.domains:
            domain_key = cached.lower()
            self.domains[domain_key]["times_seen"] += 1
            self.domains[domain_key]["last_seen"] = self._now()
            self._save()
            return {
                "domain": self.domains[domain_key]["canonical"],
                "confidence": 0.95,
                "status": "cache",
                "is_new_domain": False
            }

        # 2. Ask the LLM
        try:
            llm_domain, llm_confidence = self._llm_classify(doc_title, sample_text)
        except Exception as e:
            logger.warning(f"  Domain LLM failed for '{doc_title}': {e}")
            return {
                "domain": "General",
                "confidence": 0.3,
                "status": "fallback",
                "is_new_domain": False
            }

        normalized = self._normalize_domain_name(llm_domain)

        # 3. Fuzzy match against existing domains? (Data Science ≈ DataScience)
        existing_key = self._find_similar_domain(normalized)
        if existing_key:
            self.domains[existing_key]["times_seen"] += 1
            self.domains[existing_key]["last_seen"] = self._now()
            self.doc_cache[doc_hash] = self.domains[existing_key]["canonical"]
            self._save()
            return {
                "domain": self.domains[existing_key]["canonical"],
                "confidence": max(llm_confidence, 0.85),
                "status": "fuzzy",
                "is_new_domain": False
            }

        # 4. Genuinely new domain
        domain_key = normalized.lower()
        is_new = domain_key not in self.domains

        if is_new:
            parent = self._infer_parent(normalized)
            self.domains[domain_key] = {
                "canonical": normalized,
                "times_seen": 1,
                "parent": parent,
                "first_seen": self._now(),
                "last_seen": self._now()
            }
            logger.info(
                f"  Discovered new domain '{normalized}' "
                f"(parent={parent or 'top-level'})"
            )
        else:
            self.domains[domain_key]["times_seen"] += 1
            self.domains[domain_key]["last_seen"] = self._now()

        self.doc_cache[doc_hash] = normalized
        self._save()

        return {
            "domain": normalized,
            "confidence": llm_confidence,
            "status": "llm",
            "is_new_domain": is_new
        }

    # ── Helpers ──

    @staticmethod
    def _doc_hash(title: str, sample_text: str) -> str:
        """Stable identity hash — survives file renaming."""
        content = f"{title.strip().lower()}::{sample_text[:1200].strip().lower()}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _normalize_domain_name(raw: str) -> str:
        """Title-case, collapse whitespace, strip punctuation."""
        cleaned = " ".join(raw.strip().split())
        # Force title case: "machine learning" → "Machine Learning"
        return cleaned.title()

    def _find_similar_domain(self, normalized: str) -> str | None:
        """SequenceMatcher against existing canonical names."""
        for key, meta in self.domains.items():
            ratio = SequenceMatcher(None, normalized.lower(), meta["canonical"].lower()).ratio()
            if ratio >= DOMAIN_SIMILARITY_THRESHOLD:
                return key
        return None

    def _llm_classify(self, doc_title: str, sample_text: str) -> tuple[str, float]:
        response = self.mistral_client.chat.complete(
            model=self.model,
            messages=[
                {"role": "system", "content": CLASSIFY_PROMPT},
                {"role": "user", "content": f"Title: {doc_title}\n\nSample text:\n{sample_text[:1500]}"}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        result = json.loads(response.choices[0].message.content)
        domain = result.get("domain", "General").strip()
        confidence = result.get("confidence", 0.7)
        return domain, confidence

    def _infer_parent(self, new_domain: str) -> str | None:
        """One cheap LLM call per genuinely new domain to place it in hierarchy."""
        if len(self.domains) < 2:
            return None  # Too early to build hierarchy

        existing = [m["canonical"] for m in self.domains.values()]
        existing_str = ", ".join(existing[:30])  # Cap context length

        try:
            response = self.mistral_client.chat.complete(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a taxonomy expert."},
                    {"role": "user", "content": PARENT_PROMPT.format(
                        new_domain=new_domain,
                        existing_domains=existing_str
                    )}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            result = json.loads(response.choices[0].message.content)
            parent = result.get("parent")
            if parent and parent.lower() in self.domains:
                return self.domains[parent.lower()]["canonical"]
            return None
        except Exception as e:
            logger.warning(f"  Parent inference failed for '{new_domain}': {e}")
            return None

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    # ── Introspection / Self-Awareness ──

    def get_all_domains(self) -> dict:
        return dict(self.domains)

    def get_domain_lineage(self, domain_name: str) -> list[str]:
        """Return [child, parent, grandparent, ...] up to root."""
        lineage = []
        current = domain_name.lower()
        visited = set()
        while current and current not in visited:
            visited.add(current)
            meta = self.domains.get(current)
            if not meta:
                break
            lineage.append(meta["canonical"])
            parent = meta.get("parent")
            current = parent.lower() if parent else None
        return lineage

    def get_sibling_domains(self, domain_name: str) -> list[str]:
        """Return domains with the same parent."""
        key = domain_name.lower()
        meta = self.domains.get(key)
        if not meta or not meta.get("parent"):
            return []
        parent = meta["parent"].lower()
        return [
            m["canonical"] for k, m in self.domains.items()
            if m.get("parent", "").lower() == parent and k != key
        ]

    def get_niche_domains(self) -> list[dict]:
        """Domains with < MIN_DOMAIN_DOCUMENTS documents — flagged for review."""
        return [
            {"canonical": m["canonical"], "times_seen": m["times_seen"], "parent": m.get("parent")}
            for m in self.domains.values()
            if m["times_seen"] < MIN_DOMAIN_DOCUMENTS
        ]

    def _count_niche_domains(self) -> int:
        return sum(1 for m in self.domains.values() if m["times_seen"] < MIN_DOMAIN_DOCUMENTS)

    def get_taxonomy_report(self) -> dict:
        """Full self-description of what the system knows about its own domains."""
        return {
            "total_domains": len(self.domains),
            "total_documents_classified": sum(m["times_seen"] for m in self.domains.values()),
            "top_level_domains": [
                m["canonical"] for m in self.domains.values() if not m.get("parent")
            ],
            "niche_domains_flagged": self.get_niche_domains(),
            "most_popular": sorted(
                self.domains.values(),
                key=lambda x: x["times_seen"],
                reverse=True
            )[:10]
        }

    def explain_domain(self, domain_name: str) -> dict | None:
        """Why does this domain exist? What is its lineage? How niche is it?"""
        key = domain_name.lower()
        meta = self.domains.get(key)
        if not meta:
            return None
        return {
            "name": meta["canonical"],
            "documents_seen": meta["times_seen"],
            "is_niche": meta["times_seen"] < MIN_DOMAIN_DOCUMENTS,
            "lineage": self.get_domain_lineage(domain_name),
            "siblings": self.get_sibling_domains(domain_name),
            "first_seen": meta.get("first_seen"),
            "last_seen": meta.get("last_seen")
        }