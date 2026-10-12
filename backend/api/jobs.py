"""Background analysis jobs (thread pool) with progress reporting and de-duplication."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from ..config import settings
from ..graph.store import get_store
from ..kg.acquire import AcquisitionError, acquire, cleanup
from ..kg.pipeline import analyze_source, graph_id_for

log = logging.getLogger("rkg.jobs")


class JobManager:
    def __init__(self, workers: int = 2) -> None:
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="analysis")
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def submit(self, repo_url: str, ref: str | None, force: bool) -> dict:
        key = f"{repo_url.strip().lower()}|{ref or ''}"
        with self.lock:
            for j in self.jobs.values():
                if j["key"] == key and j["status"] in ("queued", "running"):
                    return j
            jid = uuid.uuid4().hex[:16]
            job = {"job_id": jid, "key": key, "repo_url": repo_url, "ref": ref, "status": "queued", "progress": 0.0,
                   "message": "Queued", "graph_id": None, "error": None, "created_at": time.time(), "finished_at": None,
                   "cached": False}
            self.jobs[jid] = job
            # bound memory: forget old finished jobs
            if len(self.jobs) > 500:
                for old in sorted(self.jobs.values(), key=lambda x: x["created_at"])[:100]:
                    if old["status"] in ("done", "failed"):
                        self.jobs.pop(old["job_id"], None)
        self.pool.submit(self._run, jid, repo_url, ref, force)
        return job

    def get(self, jid: str) -> dict | None:
        return self.jobs.get(jid)

    def _update(self, jid: str, **kw) -> None:
        with self.lock:
            if jid in self.jobs:
                self.jobs[jid].update(kw)

    def _run(self, jid: str, repo_url: str, ref: str | None, force: bool) -> None:
        self._update(jid, status="running", message="Cloning repository", progress=0.02)
        src = None
        try:
            src = acquire(repo_url, ref)
            store = get_store()
            gid = graph_id_for(src.url, src.ref, src.commit)
            if not force and src.commit and store.get_meta(gid):
                self._update(jid, status="done", progress=1.0, message="Already analyzed at this commit (cached)",
                             graph_id=gid, cached=True, finished_at=time.time())
                return

            def progress(msg: str, pct: float) -> None:
                self._update(jid, message=msg, progress=round(0.05 + 0.85 * pct, 3))

            res = analyze_source(src, progress)
            self._update(jid, message="Saving graph", progress=0.93)
            store.save_graph(res.graph_id, res.meta, res.report, res.entities, res.relationships, res.diagnostics,
                             res.rejected)
            self._update(jid, status="done", progress=1.0, message="Analysis complete", graph_id=res.graph_id,
                         finished_at=time.time(), report=res.report)
        except AcquisitionError as exc:
            self._update(jid, status="failed", error=str(exc), message="Repository could not be acquired",
                         finished_at=time.time())
        except Exception as exc:
            log.exception("analysis job %s failed", jid)
            self._update(jid, status="failed", error=f"{type(exc).__name__}: {exc}", message="Analysis failed",
                         finished_at=time.time())
        finally:
            if src is not None:
                cleanup(src)


_jobs: JobManager | None = None


def get_jobs() -> JobManager:
    global _jobs
    if _jobs is None:
        _jobs = JobManager(settings.analysis_workers)
    return _jobs
