"""
core/retrieval.py

Given a question and the entity mentions found in it, this is the actual
"connects dots" step: link each mention to a graph node, walk N hops out
from each one to surface structurally related facts, run vector search
over chunks/claims in parallel for direct supporting passages, and return
both, ranked, with provenance intact — ready for a synthesis LLM call that
can cite exactly which edge or chunk it used.
"""
import logging
from core.entity_linker import EntityLinker
from core.mention_extractor import MentionExtractor
from db.neo4j_client import Neo4jClient
from db.qdrant_client import KronosQdrantClient
from agents.alias_memory import AliasMemory

logger = logging.getLogger(__name__)

DEFAULT_MAX_HOPS = 2
DEFAULT_TOP_K_CHUNKS = 8
DEFAULT_MIN_EDGE_CONFIDENCE = 0.4  # matches MIN_CONFIDENCE_THRESHOLD's neighborhood in config


class Retriever:
    def __init__(self):
        self.neo4j = Neo4jClient()
        self.qdrant = KronosQdrantClient()
        self.alias_memory = AliasMemory()
        self.linker = EntityLinker(self.neo4j, self.qdrant, self.alias_memory)
        self.mention_extractor = MentionExtractor()
        try:
            # AUTHORSHIP (AUTHORED_BY, COLLABORATED_WITH...) and REFERENTIAL
            # (REFERENCES, DISCUSSED_IN...) edges are citation-network noise
            # for multi-hop conceptual traversal — see get_neighbors docstring.
            # Adjust the path in get_relation_types_by_category if
            # ontology_memory.json lives somewhere else in your layout.
            self.noisy_relation_types = self.neo4j.get_relation_types_by_category(["REFERENTIAL", "AUTHORSHIP"])
        except FileNotFoundError:
            logger.warning("ontology_memory.json not found — no relation-type filtering applied")
            self.noisy_relation_types = []

    def retrieve(
        self,
        query: str,
        mentions: list[str],
        domain: str | None = None,
        max_hops: int = DEFAULT_MAX_HOPS,
        top_k_chunks: int = DEFAULT_TOP_K_CHUNKS,
        max_graph_facts: int = 20
    ) -> dict:
        max_hops = max(1, min(max_hops, 3))

        linked_entities = self.linker.link_all(mentions, domain=domain)

        graph_facts_by_key = {}
        for entity in linked_entities:
            neighbors = self.neo4j.get_neighbors(
                entity["name"], type=entity["type"],
                max_hops=max_hops, min_confidence=DEFAULT_MIN_EDGE_CONFIDENCE,
                exclude_relation_types=self.noisy_relation_types
            )
            for n in neighbors:
                key = (entity["name"], n["name"])
                fact = {
                    "anchor": entity["name"],
                    "related_entity": n["name"],
                    "related_type": n["type"],
                    "relation_chain": n["relation_chain"],
                    "hops": n["hops"],
                    "score": n["path_confidence"],
                }
                if key not in graph_facts_by_key or fact["score"] > graph_facts_by_key[key]["score"]:
                    graph_facts_by_key[key] = fact
        graph_facts = list(graph_facts_by_key.values())

        chunk_hits = self.qdrant.search(query, top_k=top_k_chunks, domain_filter=domain)

        graph_facts.sort(key=lambda f: f["score"], reverse=True)
        graph_facts = graph_facts[:max_graph_facts]
        chunk_hits.sort(key=lambda c: c["score"], reverse=True)

        if not linked_entities:
            logger.info("  No entities linked — falling back to pure vector search")

        return {
            "linked_entities": linked_entities,
            "graph_facts": graph_facts,
            "chunk_hits": chunk_hits,
        }

    def ask(self, question: str, domain: str | None = None, **kwargs) -> dict:
        """
        End-to-end: extract mentions from the raw question, then retrieve.
        Use this once you trust mention extraction; use retrieve() directly
        when you want to pass a hand-picked mention list (as in testing).
        """
        mentions = self.mention_extractor.extract(question)
        return self.retrieve(question, mentions, domain=domain, **kwargs)

    def close(self):
        self.neo4j.close()