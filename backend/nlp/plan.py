"""Structured QueryPlan (the ONLY thing an LLM may produce) + deterministic rule-based planner.

The LLM never writes Cypher. It fills this schema; the backend validates every field against enums
and limits, then maps the plan to reviewed, read-only query functions."""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Intent(str, Enum):
    LOCATE_SYMBOL = "LOCATE_SYMBOL"
    LIST_METHODS = "LIST_METHODS"
    CALLERS = "CALLERS"
    CALLEES = "CALLEES"
    API_HANDLER = "API_HANDLER"
    API_TO_DATA = "API_TO_DATA"
    SQL_TABLES = "SQL_TABLES"
    SCHEMA = "SCHEMA"
    TABLE_USAGE = "TABLE_USAGE"
    DEPENDENCIES = "DEPENDENCIES"
    IMPACT = "IMPACT"
    TEST_COVERAGE = "TEST_COVERAGE"
    PATH = "PATH"
    LIST_APIS = "LIST_APIS"
    LIST_TABLES = "LIST_TABLES"
    SUMMARY = "SUMMARY"
    UNSUPPORTED = "UNSUPPORTED"


EntityKind = Literal["Class", "Interface", "Enum", "Method", "Function", "Constructor", "RestApi", "SqlQuery",
                     "Table", "Column", "Test", "File"]
HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "ANY"]


class EntityRef(BaseModel):
    text: str = Field(..., min_length=1, max_length=200, description="Entity name, qualified name, route path or file path exactly as mentioned")
    kind: Optional[EntityKind] = Field(None, description="Entity type if clear from the question")
    http_method: Optional[HttpMethod] = Field(None, description="HTTP method if the entity is a REST endpoint")

    @field_validator("text")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip().strip("`'\"")


class QueryPlan(BaseModel):
    intent: Intent
    entity: Optional[EntityRef] = None
    target: Optional[EntityRef] = Field(None, description="Second entity, only for PATH")
    max_hops: int = Field(3, ge=1, le=6)
    include_candidates: bool = True
    limit: int = Field(25, ge=1, le=100)


INTENT_GUIDE = {
    "LOCATE_SYMBOL": "Where is X defined/implemented?",
    "LIST_METHODS": "What methods/functions does class/module X contain?",
    "CALLERS": "Who/what calls or uses callable X?",
    "CALLEES": "What does callable X call?",
    "API_HANDLER": "Which method handles endpoint METHOD /path?",
    "API_TO_DATA": "Which tables does endpoint /path read or write (API -> code -> SQL -> table)?",
    "SQL_TABLES": "Which tables does method/query X read or write?",
    "SCHEMA": "Which columns/keys does table X have?",
    "TABLE_USAGE": "Which code/APIs read or write table X?",
    "DEPENDENCIES": "What does X depend on?",
    "IMPACT": "What may be affected if X changes?",
    "TEST_COVERAGE": "Which tests exercise X?",
    "PATH": "How is X connected to Y?",
    "LIST_APIS": "List the REST endpoints",
    "LIST_TABLES": "List the database tables",
    "SUMMARY": "Overview of the repository",
    "UNSUPPORTED": "Question cannot be answered from the repository graph",
}

ROUTE_RE = re.compile(r"(?:\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+)?(/[A-Za-z0-9_\-./{}:<>*]*)", re.I)
BACKTICK_RE = re.compile(r"`([^`]+)`")
STOP = set("""a an the of in on for to from by with and or is are was were be been it its this that these those what which who
whom whose where when why how does do did can could would should will shall may might must i me my we our you your they them
their there here all any each every some show list find tell give explain describe me please about code repo repository
project method methods function functions class classes api apis endpoint endpoints route routes table tables database db sql
query queries call calls called caller callers calling invoke invokes used use uses using usage usages handle handles
handled handler handlers implemented implement implementation defined define definition located locate test tests tested
testing coverage depend depends dependency dependencies dependent impact impacts affected affect affects break breaks change
changes changed modify modifies edit delete remove refactor if column columns schema field fields structure path connected
connection between read reads write writes written touch touches access accesses insert update select data stored store
saves save get post put patch members member contain contains inside overview summary architecture what's whats file files
module modules service services controller does""".split())


def _route(q: str) -> tuple[str | None, str | None]:
    for m in ROUTE_RE.finditer(q):
        path = m.group(2)
        if not path or path.startswith("//"):
            continue
        if path == "/" and not m.group(1):
            continue
        start = m.start(2)
        if start > 0 and (q[start - 1].isalnum() or q[start - 1] in "._"):
            continue          # part of a file path like src/main/App.java, not a route
        return (m.group(1) or "").upper() or None, path.rstrip(".?,!")
    return None, None


def candidate_terms(q: str) -> list[str]:
    """Identifier-like terms ordered by specificity (code-looking first)."""
    terms: list[str] = []
    terms += BACKTICK_RE.findall(q)
    cleaned = BACKTICK_RE.sub(" ", q)
    tokens = re.findall(r"[A-Za-z_][\w.$#]*(?:\(\))?", cleaned)
    code_like, plain = [], []
    for t in tokens:
        t2 = t.rstrip("()").strip(".")
        if not t2 or t2.lower() in STOP:
            continue
        if t.endswith("()") or "_" in t2 or "." in t2 or re.search(r"[a-z][A-Z]", t2) or re.match(r"^[A-Z][a-z]+[A-Z]", t2) or t2[0].isupper():
            code_like.append(t2)
        else:
            plain.append(t2)
    for t in code_like + plain:
        if t not in terms:
            terms.append(t)
    return terms


def rule_plan(question: str) -> tuple[QueryPlan, list[str], list[str]]:
    """Return (plan, entity candidates, target candidates). Entity text is resolved by the engine."""
    q = question.strip()
    ql = f" {q.lower()} "
    verb, route = _route(q)
    terms = candidate_terms(q if not route else q.replace(route, " "))
    target_terms: list[str] = []

    def has(*pats: str) -> bool:
        return any(re.search(p, ql) for p in pats)

    intent = None
    m = re.search(r"(?:path|route|connect\w*|link\w*|relat\w*)\s+(?:from|between)\s+(.+?)\s+(?:to|and)\s+(.+?)[?.!]*$", q, re.I)
    if m or has(r"\bhow (is|are|does) .+ (connected|related|linked) to\b"):
        intent = Intent.PATH
        if m:
            left, right = m.group(1), m.group(2)
        else:
            mm = re.search(r"how (?:is|are|does) (.+?) (?:connected|related|linked) to (.+?)[?.!]*$", q, re.I)
            left, right = (mm.group(1), mm.group(2)) if mm else ("", "")
        lv, lr = _route(left)
        rv, rr = _route(right)
        terms = ([lr] if lr else []) + candidate_terms(left)
        target_terms = ([rr] if rr else []) + candidate_terms(right)
        route = None
    elif has(r"\bimpact", r"\baffect", r"\bbreak", r"\bif (i|we) (change|modify|edit|delete|remove|refactor|rename)",
             r"\bripple", r"blast radius", r"\bdepends on it\b", r"\bwhat (would|will|might) (change|be affected)"):
        intent = Intent.IMPACT
    elif has(r"\btest", r"\bcoverage\b", r"\bcovered\b"):
        intent = Intent.TEST_COVERAGE
    elif route:
        intent = Intent.API_TO_DATA if has(r"\btables?\b", r"\bdatabase\b", r"\bdb\b", r"\bsql\b", r"\bdata\b",
                                           r"\bstored?\b", r"\bpersist", r"\btouch", r"\bquer", r"\breads?\b", r"\bwrites?\b") \
            else (Intent.CALLEES if has(r"\bcalls?\b(?! by)", r"\binvok") else Intent.API_HANDLER)
    elif has(r"\bwho (calls|uses|invokes)", r"\bwhat (calls|uses|invokes)", r"\bcallers?\b", r"\bcalled (by|from)\b",
             r"\bwhere is .+ (called|used|invoked)", r"\busages?\b", r"\breferences to\b", r"\bwho depends on\b",
             r"\bwhat depends on\b"):
        intent = Intent.CALLERS
    elif has(r"\bwhat does .+ call\b", r"\bcalls made by\b", r"\bcallees?\b", r"\bwhat .+ invokes?\b", r"\bwhat .+ calls\b"):
        intent = Intent.CALLEES
    elif has(r"\bcolumns?\b", r"\bschema\b", r"\bfields? (of|in)\b", r"\bstructure of\b", r"\bforeign keys?\b", r"\bprimary key\b"):
        intent = Intent.SCHEMA
    elif has(r"\b(which|what|who) (methods?|functions?|apis?|endpoints?|code|classes?|services?|queries) .*\b(read|write|use|access|query|queries|update|insert|delete|modif|touch)",
             r"\bwho (reads|writes|updates|modifies|accesses)\b", r"\bused by\b.*\btable", r"\btable\b.*\b(used|accessed|written|read|updated) by\b"):
        intent = Intent.TABLE_USAGE
    elif has(r"\b(which|what) tables?\b", r"\btables? (does|do|is|are)\b"):
        intent = Intent.SQL_TABLES
    elif has(r"\bdepend", r"\bdependenc"):
        intent = Intent.DEPENDENCIES
    elif has(r"\b(methods?|functions?|members?|operations?) (of|in|inside|defined in|does)\b", r"\bwhat (methods|functions) does\b",
             r"\bwhat does .+ (contain|define)\b"):
        intent = Intent.LIST_METHODS
    elif has(r"\b(list|show|all|which|what)\b.*\b(apis|endpoints|routes)\b", r"\brest api\b.*\b(list|exposed)", r"\bexposed\b"):
        intent = Intent.LIST_APIS
    elif has(r"\b(list|show|all|which|what)\b.*\btables\b"):
        intent = Intent.LIST_TABLES
    elif has(r"\bsummar", r"\boverview\b", r"\barchitecture\b", r"\bwhat is this (repo|project|code)", r"\babout this (repo|project)"):
        intent = Intent.SUMMARY
    elif has(r"\bwhere\b", r"\bfind\b", r"\blocate\b", r"\bdefin", r"\bimplement", r"\bwhat is\b", r"\bshow\b", r"\bexplain\b"):
        intent = Intent.LOCATE_SYMBOL
    if intent is None:
        intent = Intent.LOCATE_SYMBOL if (terms or route) else Intent.UNSUPPORTED
    if route and intent not in (Intent.PATH,):
        terms = [route] + terms
    kind = None
    if route:
        kind = "RestApi"
    elif intent in (Intent.SCHEMA,) or has(r"\btable\b"):
        kind = "Table" if intent in (Intent.SCHEMA, Intent.TABLE_USAGE) else None
    ent = EntityRef(text=terms[0], kind=kind, http_method=verb) if terms else None
    tgt = EntityRef(text=target_terms[0]) if target_terms else None
    hops = 3
    mh = re.search(r"\b(\d)\s*(?:hops?|levels?|steps?)\b", ql)
    if mh:
        hops = max(1, min(6, int(mh.group(1))))
    plan = QueryPlan(intent=intent, entity=ent, target=tgt, max_hops=hops,
                     include_candidates=not has(r"\bverified only\b", r"\bonly verified\b"))
    return plan, terms, target_terms
