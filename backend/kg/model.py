"""Canonical knowledge-graph model: entity/relationship types, IDs, evidence and the
ontology rules (allowed endpoint types) used by the validator.

Every analyzer emits records in THIS format, so Java/Python/SQL/OpenAPI parsers never
invent their own incompatible node/edge shapes.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1.0.0"
ANALYZER_VERSION = "1.0.0"


class EntityType(str, Enum):
    REPOSITORY = "Repository"
    DIRECTORY = "Directory"
    FILE = "File"
    CLASS = "Class"
    INTERFACE = "Interface"
    ENUM = "Enum"
    METHOD = "Method"
    CONSTRUCTOR = "Constructor"
    FUNCTION = "Function"
    REST_API = "RestApi"
    SQL_QUERY = "SqlQuery"
    TABLE = "Table"
    COLUMN = "Column"
    TEST = "Test"


class RelType(str, Enum):
    CONTAINS = "CONTAINS"            # Repository/Directory -> Directory/File
    DEFINES = "DEFINES"              # File/Type -> declaration
    IMPORTS = "IMPORTS"              # File -> File/Type/Function
    CALLS = "CALLS"                  # callable -> callable
    INSTANTIATES = "INSTANTIATES"    # callable -> Class
    EXTENDS = "EXTENDS"              # type -> type
    IMPLEMENTS = "IMPLEMENTS"        # class -> interface
    OVERRIDES = "OVERRIDES"          # method -> method it overrides/implements
    HANDLED_BY = "HANDLED_BY"        # RestApi -> handler callable
    EXECUTES = "EXECUTES"            # callable -> SqlQuery
    READS = "READS"                  # SqlQuery/callable -> Table
    WRITES = "WRITES"                # SqlQuery/callable -> Table
    HAS_COLUMN = "HAS_COLUMN"        # Table -> Column
    FOREIGN_KEY = "FOREIGN_KEY"      # Table -> Table
    MAPS_TO = "MAPS_TO"              # ORM class -> Table
    TESTS = "TESTS"                  # Test -> target
    HTTP_CALLS = "HTTP_CALLS"        # callable -> RestApi (same language client call)
    CROSS_LANGUAGE_MAP = "CROSS_LANGUAGE_MAP"  # callable -> RestApi in another language


class Status(str, Enum):
    VERIFIED = "VERIFIED"
    CANDIDATE = "CANDIDATE"
    REJECTED = "REJECTED"


E = EntityType
TYPES = {E.CLASS, E.INTERFACE, E.ENUM}
CALLABLES = {E.METHOD, E.CONSTRUCTOR, E.FUNCTION}
CODE_DECLS = TYPES | CALLABLES | {E.TEST}

# Ontology: which endpoint types each relationship may connect (validator enforces this).
ALLOWED_ENDPOINTS: dict[RelType, tuple[set, set]] = {
    RelType.CONTAINS: ({E.REPOSITORY, E.DIRECTORY}, {E.DIRECTORY, E.FILE}),
    RelType.DEFINES: ({E.FILE} | TYPES | {E.SQL_QUERY}, CODE_DECLS | {E.SQL_QUERY, E.REST_API, E.TABLE}),
    RelType.IMPORTS: ({E.FILE}, {E.FILE} | TYPES | {E.FUNCTION}),
    RelType.CALLS: (CALLABLES, CALLABLES),
    RelType.INSTANTIATES: (CALLABLES | {E.TEST}, TYPES),
    RelType.EXTENDS: (TYPES, TYPES),
    RelType.IMPLEMENTS: ({E.CLASS, E.ENUM}, {E.INTERFACE, E.CLASS}),
    RelType.OVERRIDES: (CALLABLES, CALLABLES),
    RelType.HANDLED_BY: ({E.REST_API}, CALLABLES | TYPES),
    RelType.EXECUTES: (CALLABLES | {E.TEST}, {E.SQL_QUERY}),
    RelType.READS: ({E.SQL_QUERY} | CALLABLES, {E.TABLE}),
    RelType.WRITES: ({E.SQL_QUERY} | CALLABLES, {E.TABLE}),
    RelType.HAS_COLUMN: ({E.TABLE}, {E.COLUMN}),
    RelType.FOREIGN_KEY: ({E.TABLE}, {E.TABLE}),
    RelType.MAPS_TO: (TYPES, {E.TABLE}),
    RelType.TESTS: ({E.TEST}, CALLABLES | TYPES | {E.REST_API}),
    RelType.HTTP_CALLS: (CALLABLES | {E.TEST}, {E.REST_API}),
    RelType.CROSS_LANGUAGE_MAP: (CALLABLES, {E.REST_API}),
}

STRUCTURAL_RELS = {RelType.CONTAINS, RelType.DEFINES}

RELATIONSHIP_MEANINGS = {
    "CONTAINS": "parent directory/repository contains child",
    "DEFINES": "file/type/sql statement declares the target",
    "IMPORTS": "file imports a module/type",
    "CALLS": "caller invokes callee (static resolution)",
    "INSTANTIATES": "callable creates an instance of a class",
    "EXTENDS": "type inherits from type",
    "IMPLEMENTS": "class implements interface",
    "OVERRIDES": "method overrides/implements a supertype method",
    "HANDLED_BY": "REST endpoint is implemented by handler",
    "EXECUTES": "callable executes SQL statement",
    "READS": "SQL statement / ORM call reads table",
    "WRITES": "SQL statement / ORM call modifies table",
    "HAS_COLUMN": "table has column",
    "FOREIGN_KEY": "table references another table",
    "MAPS_TO": "ORM entity class maps to table",
    "TESTS": "test exercises target",
    "HTTP_CALLS": "code calls a REST endpoint of this repository over HTTP",
    "CROSS_LANGUAGE_MAP": "code in one language calls a REST endpoint implemented in another",
}


def make_id(kind: str, *parts: str) -> str:
    """Single, centralised ID builder. IDs are deterministic so re-analysis is idempotent."""
    return kind.lower() + ":" + "::".join(p for p in parts if p is not None)


def short_hash(text: str, n: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:n]


_PARAM_RE = re.compile(r"\{[^}]*\}|<[^>]*>|:[A-Za-z_][A-Za-z0-9_]*|\*\*?")


def normalize_route(path: str) -> str:
    """Normalise route templates so /users/{id}, /users/<int:id>, /users/:id compare equal."""
    if not path:
        return "/"
    p = path.strip().split("?", 1)[0]
    p = re.sub(r"^[a-z]+://[^/]+", "", p)          # strip scheme+host
    p = p.replace("^", "").replace("$", "")
    p = _PARAM_RE.sub("{}", p)
    p = re.sub(r"\(\?P<[^>]+>[^)]*\)", "{}", p)      # django regex groups
    p = re.sub(r"/+", "/", "/" + p)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def api_id(method: str, path: str) -> str:
    return make_id("api", f"{method.upper()} {normalize_route(path)}")


def table_id(name: str) -> str:
    return make_id("table", normalize_table_name(name))


def normalize_table_name(name: str) -> str:
    n = name.strip().strip('`"[]').split(".")[-1].strip('`"[]')
    return n.lower()


def snake_case(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


@dataclass
class Entity:
    id: str
    type: str
    name: str
    qualified_name: str = ""
    file: str | None = None
    line: int | None = None
    end_line: int | None = None
    language: str | None = None
    props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(d.pop("props") or {})
        return d


@dataclass
class Relationship:
    source: str
    target: str
    type: str
    source_kind: str = ""
    evidence: str = ""
    file: str | None = None
    line: int | None = None
    status: str = Status.VERIFIED.value
    reason: str = ""
    confidence: float = 1.0
    occurrences: int = 1
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return "rel:" + short_hash(f"{self.type}|{self.source}|{self.target}", 16)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(d.pop("props") or {})
        d["id"] = self.id
        return d


@dataclass
class Diagnostic:
    level: str          # INFO | WARNING | ERROR
    code: str           # PARSE_ERROR, SKIPPED_FILE, UNRESOLVED_CALL, DYNAMIC_SQL ...
    message: str
    file: str | None = None
    line: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_STATUS_RANK = {Status.VERIFIED.value: 2, Status.CANDIDATE.value: 1, Status.REJECTED.value: 0}


class GraphBuilder:
    """Accumulates entities/relationships with deduplication and evidence merging."""

    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self.relationships: dict[str, Relationship] = {}
        self.diagnostics: list[Diagnostic] = []
        self.rejected: list[dict] = []

    # -- entities -------------------------------------------------------------------
    def add_entity(self, ent: Entity) -> Entity:
        existing = self.entities.get(ent.id)
        if existing is None:
            self.entities[ent.id] = ent
            return ent
        # merge: keep first location, fill blanks, merge props (lists are unioned)
        for attr in ("qualified_name", "file", "line", "end_line", "language"):
            if not getattr(existing, attr) and getattr(ent, attr):
                setattr(existing, attr, getattr(ent, attr))
        for k, v in ent.props.items():
            if k not in existing.props or existing.props[k] in (None, "", [], {}):
                existing.props[k] = v
            elif isinstance(existing.props[k], list) and isinstance(v, list):
                for item in v:
                    if item not in existing.props[k]:
                        existing.props[k].append(item)
        return existing

    def get(self, eid: str) -> Entity | None:
        return self.entities.get(eid)

    # -- relationships --------------------------------------------------------------
    def add_rel(self, rel: Relationship) -> Relationship | None:
        if rel.source == rel.target and rel.type not in (RelType.CALLS.value,):
            return None
        rid = rel.id
        existing = self.relationships.get(rid)
        if existing is None:
            self.relationships[rid] = rel
            return rel
        existing.occurrences += 1
        # keep the strongest status/evidence
        if _STATUS_RANK.get(rel.status, 0) > _STATUS_RANK.get(existing.status, 0):
            existing.status, existing.reason = rel.status, rel.reason
            existing.evidence, existing.file, existing.line = rel.evidence, rel.file, rel.line
            existing.source_kind, existing.confidence = rel.source_kind, rel.confidence
        return existing

    def rel(self, source: str, target: str, rtype: RelType, *, kind: str, evidence: str = "",
            file: str | None = None, line: int | None = None, status: Status = Status.VERIFIED,
            reason: str = "", confidence: float | None = None, **props) -> Relationship | None:
        if confidence is None:
            confidence = 1.0 if status == Status.VERIFIED else 0.5
        return self.add_rel(Relationship(
            source=source, target=target, type=rtype.value, source_kind=kind,
            evidence=(evidence or "")[:300], file=file, line=line, status=status.value,
            reason=reason, confidence=confidence, props=props))

    def diag(self, level: str, code: str, message: str, file: str | None = None,
             line: int | None = None) -> None:
        if len(self.diagnostics) < 20000:
            self.diagnostics.append(Diagnostic(level, code, message[:400], file, line))
