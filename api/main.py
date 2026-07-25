from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import logging
import os
import glob

from agents.analyst import AnalystAgent
from agents.ontology_evolution import OntologyEvolution
from agents.source_reliability import SourceReliability
from agents.self_evaluation import SelfEvaluator
from db.neo4j_client import Neo4jClient
from core.config import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="KRONOS API", description="Self-evolving knowledge infrastructure", version="0.1")

analyst = None
neo4j = None
ontology_evolution = None
source_reliability_tracker = None
self_evaluator = None


@app.on_event("startup")
def startup():
    global analyst, neo4j, ontology_evolution, source_reliability_tracker, self_evaluator
    analyst = AnalystAgent()
    neo4j = Neo4jClient()
    ontology_evolution = OntologyEvolution(mistral_client=None, model="unused")
    source_reliability_tracker = SourceReliability()
    self_evaluator = SelfEvaluator()
    logger.info("KRONOS API started")


@app.on_event("shutdown")
def shutdown():
    if analyst:
        analyst.close()
    if neo4j:
        neo4j.close()


class QueryRequest(BaseModel):
    question: str


@app.post("/query")
def query(request: QueryRequest):
    """Ask KRONOS a question — hybrid graph + vector answer with sources and conflicts."""
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="question cannot be empty")
    result = analyst.query(request.question)
    return result


@app.get("/status")
def status():
    """Current graph health snapshot."""
    stats = neo4j.get_graph_stats()
    return {
        "total_entities": stats.get("total_entities", 0),
        "total_relationships": stats.get("total_relationships", 0),
        "avg_confidence": round(stats.get("avg_confidence", 0), 3)
    }


@app.get("/digest/latest")
def latest_digest():
    """Returns the most recent Curator digest."""
    files = sorted(glob.glob("logs/digest_*.md"), reverse=True)
    if not files:
        raise HTTPException(status_code=404, detail="no digests generated yet")
    with open(files[0], "r", encoding="utf-8") as f:
        content = f.read()
    return {"filename": os.path.basename(files[0]), "content": content}


@app.get("/ontology/pending")
def pending_ontology():
    """Relation types awaiting human approval — see scripts/review_ontology.py for the CLI review flow."""
    return ontology_evolution.get_pending_review()


class ApprovalRequest(BaseModel):
    key: str
    canonical: str | None = None
    category: str | None = None


@app.post("/ontology/approve")
def approve_ontology(request: ApprovalRequest):
    success = ontology_evolution.approve(request.key, canonical=request.canonical, category=request.category)
    if not success:
        raise HTTPException(status_code=404, detail=f"no pending proposal found for key '{request.key}'")
    return {"status": "approved", "key": request.key}


@app.post("/ontology/reject")
def reject_ontology(request: ApprovalRequest):
    success = ontology_evolution.reject(request.key)
    if not success:
        raise HTTPException(status_code=404, detail=f"no pending proposal found for key '{request.key}'")
    return {"status": "rejected", "key": request.key}


@app.get("/conflicts")
def conflicts():
    """All active CONFLICTS_WITH edges in the graph."""
    with neo4j.driver.session() as session:
        result = session.run("""
            MATCH (a:Entity)-[r:CONFLICTS_WITH]->(b:Entity)
            RETURN a.name AS entity, a.type AS type,
                   a.source_doc AS doc1, b.source_doc AS doc2
        """)
        return [dict(record) for record in result]


@app.get("/sources/reliability")
def source_reliability():
    """Reliability score per source document, based on how often it wins vs loses contradictions."""
    return source_reliability_tracker.get_all()


@app.get("/self-evaluation/recent")
def self_evaluation_recent(limit: int = 10):
    """KRONOS's own quality self-assessment for the most recently processed documents."""
    return self_evaluator.get_recent(limit)