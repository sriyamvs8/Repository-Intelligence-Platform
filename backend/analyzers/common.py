"""Shared analysis context passed through all analyzers and resolvers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..kg.model import Entity, EntityType, GraphBuilder, RelType, Status, api_id, normalize_route

HTTP_VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


@dataclass
class HttpCall:
    caller_id: str
    method: str            # GET/POST/.../ANY
    path: str
    file: str
    line: int | None
    language: str
    is_test: bool
    dynamic: bool
    kind: str
    evidence: str


@dataclass
class AnalysisContext:
    gb: GraphBuilder
    repo_key: str = ""
    http_calls: list[HttpCall] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=lambda: {"unresolved_calls": 0, "resolved_calls": 0,
                                                            "candidate_calls": 0})
    test_files: set[str] = field(default_factory=set)
    spec_ops: list[dict] = field(default_factory=list)
    named_sql: dict = field(default_factory=dict)          # aiosql-style named queries: name -> [sql ids]
    migrations: list[dict] = field(default_factory=list)   # Alembic op.create_table / add_column      # OpenAPI operations (for generator/connexion linking)
    unresolved_samples: int = 0

    def unresolved(self, file: str, line: int | None, what: str) -> None:
        self.stats["unresolved_calls"] += 1
        if self.unresolved_samples < 300:
            self.unresolved_samples += 1
            self.gb.diag("INFO", "UNRESOLVED_CALL", what, file, line)


def add_route(ctx: AnalysisContext, *, method: str, path: str, handler_id: str | None, file: str,
              line: int | None, language: str, kind: str, evidence: str,
              status: Status = Status.VERIFIED, reason: str = "") -> str:
    method = (method or "ANY").upper()
    norm = normalize_route(path)
    aid = api_id(method, path)
    display = path if path.startswith("/") else "/" + path
    ent = ctx.gb.add_entity(Entity(
        id=aid, type=EntityType.REST_API.value, name=f"{method} {norm if '{}' in norm else display}",
        qualified_name=f"{method} {norm}", file=file, line=line, language=language,
        props={"http_method": method, "path": display, "normalized_path": norm, "sources": [kind],
               "handler_languages": [language] if handler_id else [], "documented": False}))
    if kind not in ent.props.setdefault("sources", []):
        ent.props["sources"].append(kind)
    if handler_id:
        if language not in ent.props.setdefault("handler_languages", []):
            ent.props["handler_languages"].append(language)
        ctx.gb.rel(aid, handler_id, RelType.HANDLED_BY, kind=kind, evidence=evidence, file=file, line=line,
                   status=status, reason=reason or "route declared on the handler in source")
    return aid


def match_routes(ctx: AnalysisContext) -> None:
    """Link HTTP client calls / test client calls to REST endpoints declared in this repository."""
    gb = ctx.gb
    by_norm: dict[str, list[Entity]] = {}
    for e in gb.entities.values():
        if e.type == EntityType.REST_API.value:
            by_norm.setdefault(e.props.get("normalized_path", ""), []).append(e)
    for call in ctx.http_calls:
        norm = normalize_route(call.path)
        cands = by_norm.get(norm, [])
        if not cands:
            # try suffix match for absolute URLs with context path, e.g. /petclinic/api/owners
            cands = [e for k, lst in by_norm.items() if len(k) > 1 and norm.endswith(k) for e in lst]
        exact = [e for e in cands if e.props.get("http_method") in (call.method, "ANY") or call.method == "ANY"]
        if not exact:
            continue
        for api in exact[:3]:
            status = Status.CANDIDATE if (call.dynamic or len(exact) > 1) else Status.VERIFIED
            reason = ("URL built dynamically; static parts match" if call.dynamic else
                      "HTTP method and path match a declared endpoint")
            if call.is_test:
                gb.rel(call.caller_id, api.id, RelType.TESTS, kind=call.kind, evidence=call.evidence,
                       file=call.file, line=call.line, status=status, reason="test sends request: " + reason)
                continue
            langs = set(api.props.get("handler_languages") or [])
            rtype = RelType.CROSS_LANGUAGE_MAP if langs and call.language not in langs else RelType.HTTP_CALLS
            gb.rel(call.caller_id, api.id, rtype, kind=call.kind, evidence=call.evidence, file=call.file,
                   line=call.line, status=status, reason=reason)
