"""Analysis orchestration: acquire -> scan -> parse -> resolve -> validate -> report."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..analyzers.common import AnalysisContext, match_routes
from ..analyzers.java_analyzer import JavaAnalyzer
from ..analyzers.python_analyzer import PythonAnalyzer
from ..analyzers.spec_analyzers import analyze_mybatis, analyze_openapi, link_spec_handlers
from ..analyzers.sql_analyzer import analyze_sql_file
from ..config import settings
from .acquire import RepoSource, acquire, cleanup
from .model import (ALLOWED_ENDPOINTS, ANALYZER_VERSION, SCHEMA_VERSION, Entity, EntityType, GraphBuilder, RelType,
                    Status, make_id, short_hash)
from .scanner import read_text, scan

log = logging.getLogger("rkg.pipeline")
Progress = Callable[[str, float], None]


@dataclass
class AnalysisResult:
    graph_id: str
    meta: dict
    entities: list[dict]
    relationships: list[dict]
    diagnostics: list[dict]
    rejected: list[dict]
    report: dict = field(default_factory=dict)


def graph_id_for(url: str, ref: str | None, commit: str | None) -> str:
    return "g" + short_hash(f"{url}|{ref or ''}|{commit or ''}|{SCHEMA_VERSION}", 11)


def analyze_repository(url: str, ref: str | None = None, progress: Progress | None = None) -> AnalysisResult:
    progress = progress or (lambda msg, pct: None)
    progress("Cloning repository", 0.03)
    src = acquire(url, ref)
    try:
        return analyze_source(src, progress)
    finally:
        cleanup(src)


def analyze_source(src: RepoSource, progress: Progress | None = None) -> AnalysisResult:
    progress = progress or (lambda msg, pct: None)
    t0 = time.time()
    gb = GraphBuilder()
    ctx = AnalysisContext(gb=gb, repo_key=src.key)
    progress("Scanning files", 0.12)
    sr = scan(src.path, settings.max_file_bytes, settings.max_files)

    # ---- structure: Repository -> Directory -> File
    repo_id = make_id("repo", src.key)
    gb.add_entity(Entity(id=repo_id, type=EntityType.REPOSITORY.value, name=src.key, qualified_name=src.url,
                         props={"url": src.url, "ref": src.ref, "commit": src.commit}))
    for d in sr.directories:
        gb.add_entity(Entity(id=make_id("dir", d), type=EntityType.DIRECTORY.value, name=d.rsplit("/", 1)[-1],
                             qualified_name=d, file=d))
    files_by_kind: dict[str, list] = {}
    for f in sr.files:
        lang = {"java": "java", "python": "python", "sql": "sql"}.get(f.kind, f.ext.lstrip("."))
        gb.add_entity(Entity(id=make_id("file", f.path), type=EntityType.FILE.value, name=f.path.rsplit("/", 1)[-1],
                             qualified_name=f.path, file=f.path, line=1, language=lang,
                             props={"extension": f.ext, "size": f.size, "is_test_code": f.is_test, "parse_status": "OK"}))
        files_by_kind.setdefault(f.kind, []).append(f)
        if f.is_test:
            ctx.test_files.add(f.path)
    for d in sr.directories:
        parent = d.rsplit("/", 1)[0] if "/" in d else None
        gb.rel(make_id("dir", parent) if parent else repo_id, make_id("dir", d), RelType.CONTAINS, kind="FILESYSTEM",
               evidence=d, reason="directory structure")
    for f in sr.files:
        parent = f.path.rsplit("/", 1)[0] if "/" in f.path else None
        gb.rel(make_id("dir", parent) if parent else repo_id, make_id("file", f.path), RelType.CONTAINS,
               kind="FILESYSTEM", evidence=f.path, reason="file location")
    for path, reason in sr.skipped:
        gb.diag("INFO", "SKIPPED_FILE", reason, path)

    texts: dict[str, str] = {}

    def text_of(f) -> str | None:
        if f.path in texts:
            return texts[f.path]
        try:
            texts[f.path] = read_text(f.abs_path)
        except Exception as exc:
            gb.diag("WARNING", "READ_ERROR", str(exc), f.path)
            gb.get(make_id("file", f.path)).props["parse_status"] = "FAILED"
            texts[f.path] = None
        return texts[f.path]

    def safe(fn, f, *a):
        try:
            return fn(*a)
        except RecursionError:
            gb.diag("WARNING", "DEPTH_LIMIT", "file too deeply nested to analyse fully", f.path)
            gb.get(make_id("file", f.path)).props["parse_status"] = "PARTIAL"
        except Exception as exc:  # one bad file must never abort the repository
            log.exception("analyzer failure on %s", f.path)
            gb.diag("ERROR", "ANALYZER_ERROR", f"{type(exc).__name__}: {exc}", f.path)
            gb.get(make_id("file", f.path)).props["parse_status"] = "FAILED"

    # ---- Phase A: declarations
    java = JavaAnalyzer(ctx)
    py = PythonAnalyzer(ctx)
    jfiles, pfiles = files_by_kind.get("java", []), files_by_kind.get("python", [])
    total = max(1, len(jfiles) + len(pfiles))
    for i, f in enumerate(jfiles):
        if i % 50 == 0:
            progress(f"Parsing Java ({i}/{len(jfiles)})", 0.15 + 0.35 * i / total)
        t = text_of(f)
        if t is not None:
            safe(java.declare_file, f, f.path, t, f.is_test)
    for i, f in enumerate(pfiles):
        if i % 50 == 0:
            progress(f"Parsing Python ({i}/{len(pfiles)})", 0.15 + 0.35 * (len(jfiles) + i) / total)
        t = text_of(f)
        if t is not None:
            safe(py.declare_file, f, f.path, t, f.is_test)
    # ---- Phase B: bodies
    progress("Extracting calls and SQL", 0.55)
    try:
        java.walk_bodies()
    except Exception as exc:
        log.exception("java body walk failed")
        gb.diag("ERROR", "ANALYZER_ERROR", f"java body analysis: {exc}")
    try:
        py.walk_bodies()
    except Exception as exc:
        log.exception("python body walk failed")
        gb.diag("ERROR", "ANALYZER_ERROR", f"python body analysis: {exc}")
    # ---- SQL files
    progress("Analyzing SQL", 0.65)
    for f in files_by_kind.get("sql", []):
        t = text_of(f)
        if t is not None:
            safe(analyze_sql_file, f, gb, f.path, t)
    # ---- Phase C: resolution
    progress("Resolving symbols and relationships", 0.72)
    for name, an in (("java", java), ("python", py)):
        try:
            an.resolve()
        except Exception as exc:
            log.exception("%s resolution failed", name)
            gb.diag("ERROR", "RESOLVER_ERROR", f"{name}: {type(exc).__name__}: {exc}")
    # ---- specs & XML
    for f in files_by_kind.get("openapi?", []):
        t = text_of(f)
        if t is not None:
            safe(analyze_openapi, f, ctx, f.path, t)
    for f in files_by_kind.get("xml?", []):
        if f.size < 500_000:
            t = text_of(f)
            if t is not None and "<mapper" in t:
                safe(analyze_mybatis, f, ctx, f.path, t, java)
    try:
        link_spec_handlers(ctx, java, py)
    except Exception as exc:
        log.exception("spec handler linking failed")
        gb.diag("ERROR", "RESOLVER_ERROR", f"spec linking: {exc}")
    progress("Matching HTTP calls to endpoints", 0.85)
    match_routes(ctx)

    # ---- drop artifact File nodes that turned out to be irrelevant (json/yaml/xml without meaning)
    for f in sr.files:
        if f.kind in ("openapi?", "xml?"):
            fid = make_id("file", f.path)
            ent = gb.get(fid)
            if ent and not ent.props.get("artifact"):
                gb.entities.pop(fid, None)
                parent = f.path.rsplit("/", 1)[0] if "/" in f.path else None
                pid = make_id("dir", parent) if parent else repo_id
                gb.relationships.pop(next((rid for rid, r in gb.relationships.items()
                                           if r.target == fid and r.source == pid), ""), None)
            elif ent:
                ent.language = ent.props.get("artifact")

    progress("Validating graph", 0.9)
    validate(gb, {f.path for f in sr.files})
    report = quality_report(gb, sr, ctx, time.time() - t0)
    meta = {
        "repo_url": src.url, "repo_key": src.key, "ref": src.ref, "commit": src.commit,
        "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION, "analyzer_version": ANALYZER_VERSION,
    }
    gid = graph_id_for(src.url, src.ref, src.commit)
    meta["graph_id"] = gid
    progress("Done", 1.0)
    return AnalysisResult(
        graph_id=gid, meta=meta,
        entities=[e.to_dict() for e in gb.entities.values()],
        relationships=[r.to_dict() for r in gb.relationships.values() if r.status != Status.REJECTED.value],
        diagnostics=[d.to_dict() for d in gb.diagnostics],
        rejected=gb.rejected, report=report)


def validate(gb: GraphBuilder, scanned_files: set[str]) -> None:
    """Enforce the ontology: endpoints exist, endpoint types are allowed, evidence files exist,
    OpenAPI never implies HANDLED_BY, cross-language edges really cross languages."""
    for rid, r in list(gb.relationships.items()):
        problems = []
        s, t = gb.entities.get(r.source), gb.entities.get(r.target)
        if s is None or t is None:
            problems.append("endpoint missing")
        else:
            try:
                allowed = ALLOWED_ENDPOINTS[RelType(r.type)]
                if s.type not in {x.value for x in allowed[0]} or t.type not in {x.value for x in allowed[1]}:
                    problems.append(f"{s.type}->{t.type} not allowed for {r.type}")
            except ValueError:
                problems.append(f"unknown relationship type {r.type}")
        if r.file and r.file not in scanned_files and r.source_kind != "FILESYSTEM":
            problems.append("evidence file not in scanned set")
        if r.type == RelType.HANDLED_BY.value and r.source_kind == "OPENAPI_SPEC":
            problems.append("OpenAPI contract cannot establish HANDLED_BY")
        if r.type == RelType.CROSS_LANGUAGE_MAP.value and s is not None and t is not None:
            if s.language in (t.props.get("handler_languages") or []):
                problems.append("not cross-language")
        if problems:
            r.status = Status.REJECTED.value
            gb.rejected.append({**r.to_dict(), "rejection": "; ".join(problems)})
            del gb.relationships[rid]
    # node-level: mark callable entities without location as warnings
    for e in gb.entities.values():
        if e.type in (EntityType.METHOD.value, EntityType.FUNCTION.value) and not e.file:
            gb.diag("WARNING", "NO_LOCATION", f"{e.id} has no source location")


def quality_report(gb: GraphBuilder, sr, ctx: AnalysisContext, elapsed: float) -> dict:
    ent_counts: dict[str, int] = {}
    for e in gb.entities.values():
        ent_counts[e.type] = ent_counts.get(e.type, 0) + 1
    rel_counts: dict[str, dict[str, int]] = {}
    for r in gb.relationships.values():
        rel_counts.setdefault(r.type, {"VERIFIED": 0, "CANDIDATE": 0})
        rel_counts[r.type][r.status] = rel_counts[r.type].get(r.status, 0) + 1
    by_lang: dict[str, dict[str, int]] = {}
    for e in gb.entities.values():
        if e.type == EntityType.FILE.value:
            st = e.props.get("parse_status", "OK")
            by_lang.setdefault(e.language or "other", {"files": 0, "OK": 0, "PARTIAL": 0, "FAILED": 0})
            by_lang[e.language or "other"]["files"] += 1
            by_lang[e.language or "other"][st] = by_lang[e.language or "other"].get(st, 0) + 1
    diag_counts: dict[str, int] = {}
    for d in gb.diagnostics:
        diag_counts[d.code] = diag_counts.get(d.code, 0) + 1
    apis = [e for e in gb.entities.values() if e.type == EntityType.REST_API.value]
    handled = {r.source for r in gb.relationships.values() if r.type == RelType.HANDLED_BY.value}
    resolved, unresolved = ctx.stats["resolved_calls"], ctx.stats["unresolved_calls"]
    return {
        "files_discovered": sr.total_files_seen,
        "files_analyzed": len(sr.files),
        "files_skipped": len(sr.skipped),
        "files_by_language": by_lang,
        "entity_counts": dict(sorted(ent_counts.items())),
        "relationship_counts": dict(sorted(rel_counts.items())),
        "total_entities": len(gb.entities),
        "total_relationships": len(gb.relationships),
        "rejected_relationships": len(gb.rejected),
        "calls_resolved": resolved,
        "calls_unresolved_or_external": unresolved,
        "candidate_calls": ctx.stats["candidate_calls"],
        "apis_total": len(apis),
        "apis_with_handler": len(handled),
        "apis_documented_only": len([a for a in apis if a.id not in handled]),
        "diagnostic_counts": diag_counts,
        "elapsed_seconds": round(elapsed, 2),
    }
