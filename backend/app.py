"""FastAPI application: REST API + static UI.

Run:  python -m uvicorn backend.app:app --reload        (docs at http://127.0.0.1:8000/docs)"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .api.jobs import get_jobs
from .config import ROOT, settings
from .graph.queries import NAMED_QUERIES, GraphQueries
from .graph.store import GID_RE, get_store
from .kg.acquire import AcquisitionError, normalize_repo_url, validate_ref
from .kg.model import ANALYZER_VERSION, SCHEMA_VERSION
from .nlp.engine import AskEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rkg.api")

app = FastAPI(title="Repository Knowledge Graph API", version=ANALYZER_VERSION,
              description="Evidence-backed knowledge graph of Java / Python / SQL / REST repositories with "
                          "graph-grounded natural-language questions.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST", "DELETE"],
                   allow_headers=["*"])


# ---------------------------------------------------------------------------------- security
class RateLimiter:
    def __init__(self) -> None:
        self.hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, bucket: str, ip: str, per_min: int) -> None:
        if per_min <= 0:
            return
        now = time.time()
        with self.lock:
            dq = self.hits[(bucket, ip)]
            while dq and now - dq[0] > 60:
                dq.popleft()
            if len(dq) >= per_min:
                raise HTTPException(429, f"Rate limit exceeded ({per_min}/min for {bucket}). Try again shortly.")
            dq.append(now)


limiter = RateLimiter()


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if settings.api_key and path.startswith("/api/") and path != "/api/health":
        if request.headers.get("x-api-key") != settings.api_key:
            return JSONResponse({"detail": "Missing or invalid X-API-Key"}, status_code=401)
    t0 = time.time()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if path.startswith("/api/"):
        log.info("%s %s %s %dms", request.method, path, response.status_code, int((time.time() - t0) * 1000))
    return response


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def graph_or_404(gid: str) -> str:
    if not GID_RE.match(gid) or get_store().get_meta(gid) is None:
        raise HTTPException(404, f"Graph '{gid}' not found. Analyze a repository first (POST /api/analyze).")
    return gid


# ---------------------------------------------------------------------------------- models
class AnalyzeRequest(BaseModel):
    repo_url: str = Field(..., min_length=3, max_length=500, examples=["https://github.com/spring-projects/spring-petclinic"])
    ref: Optional[str] = Field(None, max_length=200, description="branch, tag or commit SHA")
    force: bool = Field(False, description="re-analyze even if this commit was already analyzed")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=1000)
    context_entity_id: Optional[str] = Field(None, max_length=500)
    include_candidates: Optional[bool] = None
    max_hops: Optional[int] = Field(None, ge=1, le=6)


class FeedbackRequest(BaseModel):
    question_id: str = Field(..., max_length=64)
    question: Optional[str] = Field(None, max_length=1000)
    helpful: bool
    grounded: Optional[bool] = None
    comment: Optional[str] = Field(None, max_length=1000)


# ---------------------------------------------------------------------------------- endpoints
@app.get("/api/health")
def health():
    st = get_store()
    return {"status": "ok", "schema_version": SCHEMA_VERSION, "analyzer_version": ANALYZER_VERSION,
            "graph_backend": st.health(), "nlp": {"enabled": settings.nlp_enabled, "gemini": settings.gemini_enabled,
                                                  "model": settings.gemini_model if settings.gemini_enabled else None,
                                                  "planner": "gemini+rules" if settings.gemini_enabled else "rules"},
            "auth_required": bool(settings.api_key)}


@app.post("/api/analyze", status_code=202)
def analyze(req: AnalyzeRequest, request: Request):
    limiter.check("analyze", client_ip(request), settings.rate_limit_analyze_per_min)
    local_ok = settings.allow_local_paths and Path(req.repo_url).expanduser().is_dir()
    if not local_ok:
        try:
            normalize_repo_url(req.repo_url)
            validate_ref(req.ref)
        except AcquisitionError as exc:
            raise HTTPException(400, str(exc))
    job = get_jobs().submit(req.repo_url, req.ref, req.force)
    return {k: v for k, v in job.items() if k != "key"}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = get_jobs().get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {k: v for k, v in job.items() if k != "key"}


@app.get("/api/graphs")
def list_graphs():
    return [{k: m.get(k) for k in ("graph_id", "repo_key", "repo_url", "ref", "commit", "analyzed_at")} |
            {"entities": m.get("report", {}).get("total_entities"), "relationships": m.get("report", {}).get("total_relationships")}
            for m in get_store().list_graphs()]


@app.get("/api/graph/{gid}/summary")
def summary(gid: str = Depends(graph_or_404)):
    return GraphQueries(get_store(), gid).summary()


@app.delete("/api/graph/{gid}")
def delete_graph(gid: str = Depends(graph_or_404)):
    return {"deleted": get_store().delete_graph(gid)}


@app.get("/api/graph/{gid}/export")
def export(gid: str = Depends(graph_or_404)):
    return get_store().export(gid)


@app.get("/api/graph/{gid}/schema")
def schema(gid: str = Depends(graph_or_404)):
    return GraphQueries(get_store(), gid).schema_vocabulary()


@app.get("/api/graph/{gid}/diagnostics")
def diagnostics(gid: str = Depends(graph_or_404), code: Optional[str] = None, limit: int = Query(500, ge=1, le=20000)):
    d = get_store().diagnostics(gid)
    diags = [x for x in d.get("diagnostics", []) if not code or x["code"] == code]
    report = (get_store().get_meta(gid) or {}).get("report", {})
    return {"report": report, "diagnostics": diags[:limit], "diagnostics_total": len(diags),
            "rejected": d.get("rejected", [])[:limit], "rejected_total": len(d.get("rejected", []))}


@app.get("/api/graph/{gid}/search")
def search(gid: str = Depends(graph_or_404), q: str = Query(..., min_length=1, max_length=200),
           types: Optional[str] = Query(None, description="comma-separated entity types"), limit: int = Query(20, ge=1, le=100)):
    return GraphQueries(get_store(), gid).search(q, [t for t in (types or "").split(",") if t] or None, limit)


@app.get("/api/graph/{gid}/view")
def view(gid: str = Depends(graph_or_404),
         mode: Literal["ecosystem", "architecture", "entity", "hops", "cross-language"] = "architecture",
         root_id: Optional[str] = None, hops: int = Query(1, ge=0, le=3),
         max_nodes: int = Query(None, ge=1), include_structure: bool = False, include_candidates: bool = True,
         rel_types: Optional[str] = None):
    max_nodes = min(max_nodes or settings.max_view_nodes, settings.hard_max_nodes)
    q = GraphQueries(get_store(), gid)
    if mode == "ecosystem":
        return q.view_ecosystem(max_nodes)
    if mode == "architecture":
        return q.view_architecture(max_nodes, include_candidates)
    if mode == "cross-language":
        return q.view_cross_language(max_nodes)
    if not root_id:
        raise HTTPException(400, "root_id is required for entity/hops mode")
    if not q.node(root_id):
        raise HTTPException(404, "root entity not found")
    return q.view_neighborhood(root_id, hops if mode == "hops" else max(hops, 1), max_nodes,
                               [r for r in (rel_types or "").split(",") if r] or None, include_structure, include_candidates)


@app.get("/api/graph/{gid}/node")
def node(gid: str = Depends(graph_or_404), entity_id: str = Query(..., max_length=500)):
    d = GraphQueries(get_store(), gid).node_details(entity_id)
    if not d:
        raise HTTPException(404, "entity not found")
    return d


@app.get("/api/graph/{gid}/node/{entity_id:path}")
def node_by_path(entity_id: str, gid: str = Depends(graph_or_404)):
    return node(gid, entity_id)


@app.get("/api/graph/{gid}/query/{name}")
def named_query(name: str, gid: str = Depends(graph_or_404), root_id: Optional[str] = None,
                depth: int = Query(2, ge=1, le=6), include_candidates: bool = True, max_nodes: int = Query(200, ge=1)):
    if name not in NAMED_QUERIES:
        raise HTTPException(400, f"unknown query; allowed: {', '.join(NAMED_QUERIES)}")
    q = GraphQueries(get_store(), gid)
    if root_id and not q.node(root_id):
        raise HTTPException(404, "root entity not found")
    try:
        return q.named(name, root_id, depth, include_candidates, min(max_nodes, settings.hard_max_nodes))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/graph/{gid}/impact")
def impact(gid: str = Depends(graph_or_404), root_id: str = Query(...), depth: int = Query(3, ge=1, le=6),
           include_candidates: bool = True):
    q = GraphQueries(get_store(), gid)
    if not q.node(root_id):
        raise HTTPException(404, "root entity not found")
    return q.impact_of(root_id, depth, include_candidates)


@app.get("/api/graph/{gid}/path")
def path(gid: str = Depends(graph_or_404), from_id: str = Query(...), to_id: str = Query(...),
         max_depth: int = Query(6, ge=1, le=10)):
    return GraphQueries(get_store(), gid).path_between(from_id, to_id, max_depth)


@app.post("/api/graph/{gid}/ask")
def ask(req: AskRequest, request: Request, gid: str = Depends(graph_or_404)):
    if not settings.nlp_enabled:
        raise HTTPException(503, "NLP is disabled (NLP_ENABLED=false)")
    limiter.check("ask", client_ip(request), settings.rate_limit_ask_per_min)
    return AskEngine(get_store(), gid).ask(req.question, req.context_entity_id, req.include_candidates, req.max_hops)


@app.post("/api/graph/{gid}/ask/feedback")
def ask_feedback(fb: FeedbackRequest, gid: str = Depends(graph_or_404)):
    p = settings.data_dir / "feedback.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"graph_id": gid, "ts": time.time(), **fb.model_dump()}) + "\n")
    return {"saved": True}


# ---------------------------------------------------------------------------------- UI
FRONTEND = ROOT / "frontend"
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(FRONTEND / "index.html")
