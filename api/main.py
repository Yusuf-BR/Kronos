"""
api/main.py

FastAPI service exposing the retrieval + synthesis + session pipeline over
HTTP. Replaces the test-only FastAPI file mentioned early in this project.

Retriever/Synthesizer are expensive to construct (SentenceTransformer
model load, DB connections) — built ONCE at startup via lifespan, not
per-request. Sessions are in-memory, process-local (same limitation as
ConversationSession itself) — fine for a single-process demo, not for
production or multi-worker deployment.

Run: uvicorn api.main:app --reload --port 8000
"""
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from core.retrieval import Retriever
from core.synthesis import Synthesizer
from core.session import ConversationSession

logger = logging.getLogger(__name__)

# ── Global state, set up once at startup ────────────────────────────────
_state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up: connecting to Neo4j/Qdrant, loading embedding model...")
    _state["retriever"] = Retriever()
    _state["synthesizer"] = Synthesizer()
    _state["sessions"]: dict[str, ConversationSession] = {}
    logger.info("Startup complete.")
    yield
    logger.info("Shutting down: closing Neo4j connection...")
    _state["retriever"].close()


app = FastAPI(title="Kronos Chat API", lifespan=lifespan)


# ── Request/response models ──────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str = Field(..., description="Client-chosen ID; reused across turns for conversation memory")
    question: str
    domain: Optional[str] = None
    max_hops: int = Field(default=2, ge=1, le=3, description="Clamped server-side regardless of client input")
    max_graph_facts: int = Field(default=20, ge=1, le=50)


class GraphFact(BaseModel):
    anchor: str
    related_entity: str
    related_type: str
    relation_chain: list[str]
    hops: int
    score: float


class ChunkHit(BaseModel):
    text: str
    source_doc: str
    page: int
    score: float
    type: str
    domain: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    graph_facts: list[GraphFact]
    chunk_hits: list[ChunkHit]


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Confirms live DB connections, not just that the process is up."""
    try:
        neo4j_ok = _state["retriever"].neo4j.verify_connection()
    except Exception as e:
        neo4j_ok = False
        logger.error(f"Neo4j health check failed: {e}")
    return {"status": "ok" if neo4j_ok else "degraded", "neo4j": neo4j_ok}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    sessions = _state["sessions"]
    if req.session_id not in sessions:
        sessions[req.session_id] = ConversationSession(
            _state["retriever"], _state["synthesizer"], req.session_id
        )
    session = sessions[req.session_id]

    try:
        result = session.ask(
            req.question,
            domain=req.domain,
            max_hops=req.max_hops,
            max_graph_facts=req.max_graph_facts,
        )
    except Exception as e:
        logger.error(f"Chat request failed for session {req.session_id}: {e}")
        raise HTTPException(status_code=500, detail="Something went wrong processing that question.")

    return ChatResponse(
        session_id=req.session_id,
        answer=result["answer"],
        graph_facts=result["graph_facts"],
        chunk_hits=result["chunk_hits"],
    )


@app.delete("/sessions/{session_id}")
def clear_session(session_id: str):
    """Drop conversation history for a session — e.g. a 'new chat' button."""
    removed = _state["sessions"].pop(session_id, None)
    return {"cleared": removed is not None}
