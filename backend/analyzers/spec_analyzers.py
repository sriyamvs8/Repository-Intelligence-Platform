"""OpenAPI/Swagger specification analyzer and MyBatis mapper-XML analyzer."""
from __future__ import annotations

import json
import re

import yaml

try:
    from defusedxml import ElementTree as SafeET   # protects against XXE / entity expansion
except Exception:  # pragma: no cover
    SafeET = None

from ..kg.model import EntityType, RelType, Status, make_id, normalize_route
from .common import AnalysisContext, add_route
from .sql_analyzer import add_sql, looks_like_sql

HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")


def _load_spec(path: str, text: str):
    if path.endswith(".json"):
        if '"openapi"' not in text[:4000] and '"swagger"' not in text[:4000]:
            return None
        return json.loads(text)
    if not re.search(r"^(openapi|swagger)\s*:", text[:4000], re.M):
        return None
    return yaml.safe_load(text)


def analyze_openapi(ctx: AnalysisContext, path: str, text: str) -> int:
    """Register spec operations as RestApi contract nodes. A spec NEVER creates HANDLED_BY."""
    try:
        spec = _load_spec(path, text)
    except Exception as exc:
        ctx.gb.diag("WARNING", "SPEC_PARSE_ERROR", f"invalid OpenAPI document: {exc}"[:300], path)
        return 0
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        return 0
    base = spec.get("basePath") or ""
    servers = spec.get("servers") or []
    server_prefix = ""
    if servers and isinstance(servers[0], dict):
        m = re.match(r"^(?:[a-z]+://[^/]+)?(/[^?#]*)", str(servers[0].get("url", "")))
        server_prefix = m.group(1) if m else ""
    fid = make_id("file", path)
    ctx.gb.get(fid).props["artifact"] = "openapi"
    lines = text.splitlines()
    n = 0
    existing = {e.props.get("normalized_path") for e in ctx.gb.entities.values() if e.type == EntityType.REST_API.value}
    for p, ops in spec["paths"].items():
        if not isinstance(ops, dict):
            continue
        line = next((i + 1 for i, l in enumerate(lines) if l.strip().startswith((f"{p}:", f'"{p}"', f"'{p}'"))), None)
        for method, op in ops.items():
            if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            full = (base.rstrip("/") + p) if base else p
            if server_prefix and server_prefix != "/":
                prefixed = server_prefix.rstrip("/") + p
                if normalize_route(prefixed) in existing:
                    full = prefixed
            aid = add_route(ctx, method=method.upper(), path=full, handler_id=None, file=path, line=line,
                            language="openapi", kind="OPENAPI_SPEC", evidence=f"{method.upper()} {p}")
            ent = ctx.gb.get(aid)
            ent.props["documented"] = True
            ent.props["operation_id"] = op.get("operationId")
            ent.props["summary"] = (op.get("summary") or op.get("description") or "")[:300]
            ent.props["tags"] = op.get("tags") or []
            if op.get("operationId"):
                ctx.spec_ops.append({"api": aid, "operation_id": str(op["operationId"]), "tags": op.get("tags") or [],
                                     "file": path, "line": line, "controller": op.get("x-openapi-router-controller")
                                     or ops.get("x-openapi-router-controller") or spec.get("x-openapi-router-controller")})
            ctx.gb.rel(fid, aid, RelType.DEFINES, kind="OPENAPI_SPEC", evidence=f"{method.upper()} {p}", file=path,
                       line=line, reason="operation declared in API specification (contract, not implementation)")
            n += 1
    return n


def analyze_mybatis(ctx: AnalysisContext, path: str, text: str, java) -> int:
    """MyBatis mapper XML: <mapper namespace="a.b.UserMapper"><select id="findById">SQL</select>"""
    if "<mapper" not in text or "namespace" not in text:
        return 0
    if SafeET is None:
        ctx.gb.diag("WARNING", "XML_SKIPPED", "defusedxml not installed; mapper XML skipped", path)
        return 0
    try:
        cleaned = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.S)
        root = SafeET.fromstring(cleaned)
    except Exception as exc:
        ctx.gb.diag("WARNING", "XML_PARSE_ERROR", f"{exc}"[:300], path)
        return 0
    if root.tag != "mapper":
        return 0
    ns = root.get("namespace", "")
    ctx.gb.get(make_id("file", path)).props["artifact"] = "mybatis-mapper"
    lines = text.splitlines()
    n = 0
    for el in root:
        if el.tag not in ("select", "insert", "update", "delete"):
            continue
        sid = el.get("id", "")
        sql = re.sub(r"\s+", " ", " ".join(el.itertext())).strip()
        sql = re.sub(r"#\{[^}]*\}", "?", sql)
        dynamic = "${" in sql or any(True for _ in el.iter() if _.tag in ("if", "choose", "foreach", "where", "trim"))
        if not sql or not looks_like_sql(sql):
            continue
        line = next((i + 1 for i, l in enumerate(lines) if f'id="{sid}"' in l), None)
        executor = None
        if java is not None and ns in java.idx.types:
            ms = java.method_by_qualified(ns, sid)
            if ms:
                executor = ms[0].id
        add_sql(ctx.gb, sql, file=path, line=line, language="xml", executor_id=executor, exec_kind="MYBATIS_XML",
                exec_evidence=f'<{el.tag} id="{sid}"> in mapper {ns}', exec_status=Status.VERIFIED,
                exec_reason="MyBatis mapper namespace + statement id match the Java mapper method", dynamic=dynamic)
        if executor is None:
            ctx.gb.diag("INFO", "MYBATIS_UNBOUND", f"mapper statement {ns}.{sid} has no matching Java method", path, line)
        n += 1
    return n


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def link_spec_handlers(ctx: AnalysisContext, java, py) -> int:
    """Link spec operations to handlers ONLY through framework conventions that leave source evidence:
    * openapi-generator (Spring, interface-only): a @RestController method named exactly like the
      operationId that @Overrides an *external* generated interface named <Tag>Api  -> CANDIDATE
    * Connexion (Python): operationId is an explicit dotted 'module.function' reference -> VERIFIED
    An operationId alone never establishes HANDLED_BY."""
    gb = ctx.gb
    handled = {r.source for r in gb.relationships.values() if r.type == RelType.HANDLED_BY.value}
    n = 0
    java_ctrl = []
    if java is not None:
        for jt in java.idx.types.values():
            if not ({"RestController", "Controller"} & set(jt.annotations)):
                continue
            external_apis = [s for s in jt.implements + jt.extends
                             if s.endswith("Api") and not (java.resolve_type(s, jt) or ("", False))[0] in java.idx.types]
            if external_apis:
                java_ctrl.append((jt, external_apis))
    for op in ctx.spec_ops:
        if op["api"] in handled:
            continue
        oid = op["operation_id"]
        # ---- Connexion style
        if py is not None and "." in oid:
            mod_name, _, fn = oid.rpartition(".")
            if op.get("controller"):
                mod_name, fn = op["controller"], oid.rsplit(".", 1)[-1]
            m = py._resolve_module(mod_name)
            if m and fn in m.functions:
                f = m.functions[fn]
                gb.rel(op["api"], f.id, RelType.HANDLED_BY, kind="CONNEXION_OPERATION_ID", evidence=f"operationId: {oid}",
                       file=op["file"], line=op["line"], status=Status.VERIFIED,
                       reason="Connexion routes the operation to the dotted operationId function")
                n += 1
                continue
        # ---- openapi-generator interface-only convention
        cands = []
        for jt, apis in java_ctrl:
            for m in jt.methods:
                if m.name == oid and "Override" in m.annotations:
                    tag_ok = any(_norm(a)[:-3] in {_norm(t) for t in op["tags"]} for a in apis) if op["tags"] else False
                    cands.append((tag_ok, jt, m, apis))
        if not cands:
            continue
        tagged = [c for c in cands if c[0]]
        chosen = tagged or cands
        for tag_ok, jt, m, apis in chosen[:3]:
            gb.rel(op["api"], m.id, RelType.HANDLED_BY, kind="OPENAPI_GENERATOR_CONVENTION",
                   evidence=f"operationId {oid} == {jt.simple}.{m.name}() @Override of generated {apis[0]}",
                   file=m.file, line=m.line, status=Status.CANDIDATE, confidence=0.8 if (tag_ok and len(chosen) == 1) else 0.5,
                   reason="openapi-generator interface-only pattern: controller overrides the generated "
                          f"{apis[0]} method named after the operationId" + (" (tag matches)" if tag_ok else "")
                          + (f"; {len(chosen)} possible controllers" if len(chosen) > 1 else ""))
            ent = gb.get(op["api"])
            if ent and "java" not in ent.props.setdefault("handler_languages", []):
                ent.props["handler_languages"].append("java")
            n += 1
    return n
