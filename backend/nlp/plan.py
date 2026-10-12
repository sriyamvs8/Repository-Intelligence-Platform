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
    COUNT = "COUNT"
    LIST_TYPE = "LIST_TYPE"
    FLOW = "FLOW"
    EXPLAIN = "EXPLAIN"
    UNUSED = "UNUSED"
    HOTSPOTS = "HOTSPOTS"
    UNTESTED = "UNTESTED"
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
    subject: Optional[Literal["RestApi", "Table", "Test", "Class", "Interface", "Method", "SqlQuery", "Column", "File",
                              "Controller", "Service", "Data access", "Model", "All"]] = Field(
        None, description="For COUNT / LIST_TYPE: which kind of thing to count or list")


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
    "SUMMARY": "Overview of the repository and its architecture",
    "COUNT": "How many endpoints/tables/classes/methods/tests are there?",
    "LIST_TYPE": "List all controllers/services/DAOs/models/classes/tests",
    "FLOW": "What happens when endpoint METHOD /path is called (step-by-step request flow)?",
    "EXPLAIN": "What does X do? Explain/describe X (location, role, callers, callees, data, tests)",
    "UNUSED": "Which methods are never called (possible dead code)?",
    "HOTSPOTS": "Which methods/classes are used the most (critical components)?",
    "UNTESTED": "Which endpoints have no tests?",
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
module modules service services controller does location source see want tell give anything live parts refactor blast
radius rely relies number codebase walk through request flow never most without covering exist enumerate controllers
dao daos happens happen describe summarize reach reaches gets much many app application exposes expose invoked invokes
lives would rest classes mean means critical important unused dead hotspot hotspots exists all any count total
components component role purpose work works processed lifecycle sequence trace end safe drop alter rename""".split())


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


TYPE_WORDS = [
    (r"\b(end ?points?|apis?|routes?|urls?)\b", "RestApi"), (r"\btables?\b", "Table"), (r"\btests?\b", "Test"),
    (r"\bcontrollers?\b|\bhandlers?\b|\bviews?\b", "Controller"), (r"\bservices?\b", "Service"),
    (r"\b(daos?|repositor(y|ies) classes|mappers?|data[- ]access)\b", "Data access"),
    (r"\b(models?|entit(y|ies))\b", "Model"), (r"\binterfaces?\b", "Interface"), (r"\bclass(es)?\b", "Class"),
    (r"\b(methods?|functions?)\b", "Method"), (r"\b(sql|quer(y|ies)|statements?)\b", "SqlQuery"),
    (r"\bcolumns?\b", "Column"), (r"\bfiles?\b", "File"),
]


def _subject(ql: str) -> str | None:
    for pat, sub in TYPE_WORDS:
        if re.search(pat, ql):
            return sub
    return None


def rule_plan(question: str) -> tuple[QueryPlan, list[str], list[str]]:
    """Deterministic intent + slot extraction. Returns (plan, entity candidates, target candidates).
    Rule order matters: specific phrasings are checked before generic ones."""
    q = question.strip()
    ql = f" {q.lower()} "
    verb, route = _route(q)
    terms = candidate_terms(q if not route else q.replace(route, " "))
    target_terms: list[str] = []
    if route:   # the HTTP verb + path must not be read as English words ("DELETE" is not "deletes data")
        ql = " " + re.sub(rf"(?:\b{verb}\s+)?{re.escape(route)}" if verb else re.escape(route), " ROUTE ", q, flags=re.I).lower() + " "

    def has(*pats: str) -> bool:
        return any(re.search(p, ql) for p in pats)

    intent = None
    subject = None
    m = re.search(r"(?:path|route|connect\w*|link\w*|relat\w*)\s+(?:from|between)\s+(.+?)\s+(?:to|and)\s+(.+?)[?.!]*$", q, re.I)
    mm = re.search(r"how (?:is|are|does) (.+?) (?:connected|related|linked) (?:to|with) (.+?)[?.!]*$", q, re.I)
    if m or mm:
        intent = Intent.PATH
        left, right = (m.group(1), m.group(2)) if m else (mm.group(1), mm.group(2))
        lv, lr = _route(left)
        rv, rr = _route(right)
        terms = ([lr] if lr else []) + candidate_terms(left)
        target_terms = ([rr] if rr else []) + candidate_terms(right)
        route = None
    elif has(r"\bhow many\b", r"\bnumber of\b", r"\bcount (the|of|all)?\b", r"\btotal (number )?of\b") and not has(r"\bcall(s|ers)?\b .+\b\w+\(\)"):
        intent, subject = Intent.COUNT, _subject(ql) or "All"
    elif has(r"\bno tests?\b", r"\bwithout (any )?tests?\b", r"\bwithout test coverage\b", r"\buntested\b",
             r"\bnot (been )?tested\b", r"\bmissing tests?\b", r"\black(s|ing)? tests?\b", r"\bnot covered\b"):
        intent = Intent.UNTESTED
    elif has(r"\bunused\b", r"\bdead code\b", r"\bnever (called|used|invoked)\b", r"\bnot (called|used|referenced) (anywhere|at all|by anything)?",
             r"\borphan", r"\bno callers\b"):
        intent = Intent.UNUSED
    elif has(r"\bmost (used|called|referenced|important|critical|central|depended)", r"\bhot ?spots?\b",
             r"\bcalled the most\b", r"\bused the most\b", r"\bcritical (components?|code|classes|methods)\b",
             r"\bcore components?\b", r"\bmost connected\b"):
        intent = Intent.HOTSPOTS
    elif has(r"\bimpact", r"\baffect", r"\bbreak", r"\bif (i|we|you) (change|modify|edit|delete|remove|refactor|rename|drop|alter)",
             r"\bripple", r"blast radius", r"\bwhat (would|will|might|could) (change|be affected|break)", r"\bsafe to (change|delete|remove|modify)"):
        intent = Intent.IMPACT
    elif has(r"\bwhat happens when\b", r"\brequest flow\b", r"\bflow (of|for)\b", r"\bwalk me through\b", r"\btrace\b",
             r"\bsequence\b", r"\blifecycle\b", r"\bend[- ]to[- ]end\b", r"\bhow does .+ (work|flow|reach|get processed)"):
        intent = Intent.FLOW if route else Intent.EXPLAIN
    elif has(r"\btest", r"\bcoverage\b", r"\bcovered\b", r"\bcovering\b"):
        intent = Intent.TEST_COVERAGE
    elif route:
        if has(r"\btables?\b", r"\bdatabase\b", r"\bdb\b", r"\bsql\b", r"\bdata\b", r"\bstored?\b", r"\bpersist",
               r"\btouch", r"\bquer", r"\breads?\b", r"\bwrites?\b", r"\bupdated?\b", r"\binsert", r"\bdelete[sd]?\b"):
            intent = Intent.API_TO_DATA
        elif has(r"\bwhat does .+ call\b", r"\bcalls made\b"):
            intent = Intent.CALLEES
        else:
            intent = Intent.API_HANDLER
    elif has(r"\btables?\b") and has(r"\b(use|uses|used|using|read|reads|write|writes|written|update|updates|insert|inserts|delete|deletes|access|accesses|modif\w*|touch\w*)\b") \
            and not has(r"\bwhich tables\b", r"\bwhat tables\b"):
        intent = Intent.TABLE_USAGE
    elif has(r"\b(who|what|which code|which parts of the code|which methods|which functions) (writes?|reads?|updates?|inserts?|deletes?|modifies) (to |from |into )?\w"):
        intent = Intent.TABLE_USAGE
    elif has(r"\bwhere is .+ (called|used|invoked|referenced)", r"\bwho (calls|uses|invokes|references)\b",
             r"\bwhat (calls|uses|invokes|references)\b", r"\bcallers?\b", r"\banything (calling|using)\b",
             r"\busages?\b", r"\breferences to\b", r"\bwho depends on\b", r"\bwhat depends on\b", r"\bcalled from where\b"):
        intent = Intent.CALLERS
    elif has(r"\bwhat does .+ (depend|rely) on\b", r"\bdepend", r"\brel(y|ies) on\b", r"\brequires?\b"):
        intent = Intent.DEPENDENCIES
    elif has(r"\bwhat does .+ do\b", r"\bexplain\b(?! the (architecture|repo|project|system|codebase))", r"\bdescribe\b(?! table)",
             r"\btell me about\b", r"\bpurpose of\b", r"\bwhat is the role of\b") and not has(r"\barchitecture\b"):
        intent = Intent.EXPLAIN
    elif has(r"\bwhat does .+ (call|invoke|use)\b", r"\bcalls made by\b", r"\bcallees?\b", r"\b(invoked|called|used) by\b",
             r"\b(gets?|is|are) (called|invoked) from\b", r"\bwhat .+ calls\b"):
        intent = Intent.CALLEES
    elif has(r"\bcolumns?\b", r"\bschema\b", r"\bfields?\b", r"\bstructure of\b", r"\bforeign keys?\b",
             r"\bprimary key\b", r"\bdescribe table\b", r"\bdescribe the .+ table\b"):
        intent = Intent.SCHEMA
    elif has(r"\b(which|what) tables?\b", r"\btables? (does|do|is|are)\b"):
        intent = Intent.SQL_TABLES
    elif has(r"\b(methods?|functions?|members?|operations?) (are |exist |defined |declared )?(of|in|inside|defined in|does)\b", r"\bwhat (methods|functions) does\b",
             r"\b(live|defined|declared) (inside|in)\b", r"\bwhat does .+ (contain|define)\b", r"\bmembers of\b"):
        intent = Intent.LIST_METHODS
    elif has(r"\b(list|show|all|which|what|give|enumerate|display)\b.*\b(end ?points|apis|routes|urls)\b",
             r"\brest api\b", r"\bexposed?\b"):
        intent = Intent.LIST_APIS
    elif has(r"\b(list|show|all|which|what|give|enumerate|display)\b.*\btables\b"):
        intent = Intent.LIST_TABLES
    elif has(r"\b(list|show|all|which|what are the|enumerate|display|give)\b.*\b(controllers|services|daos?|repositories|mappers|models|entities|classes|interfaces|tests)\b",
             r"\bwhich classes exist\b"):
        intent, subject = Intent.LIST_TYPE, _subject(ql) or "Class"
    elif has(r"\bsummar", r"\boverview\b", r"\barchitecture\b", r"\bwhat is this\b", r"\babout this\b",
             r"\bexplain the (repo|project|system|codebase|app)", r"\bwhat does this (repo|project|app|code)"):
        intent = Intent.SUMMARY
    elif has(r"\bwhere\b", r"\bfind\b", r"\blocate\b", r"\blocation\b", r"\bsource (code )?of\b", r"\bdefin",
             r"\bimplement", r"\bshow\b", r"\bsee\b", r"\bopen\b"):
        intent = Intent.LOCATE_SYMBOL
    elif has(r"\bwhat is\b"):
        intent = Intent.EXPLAIN
    if intent is None:
        intent = Intent.EXPLAIN if (terms or route) else Intent.UNSUPPORTED
    if route and intent not in (Intent.PATH,):
        terms = [route] + terms
    kind = None
    if route:
        kind = "RestApi"
    elif intent in (Intent.SCHEMA, Intent.TABLE_USAGE):
        kind = "Table"
    ent = EntityRef(text=terms[0], kind=kind, http_method=verb) if terms and intent not in (
        Intent.COUNT, Intent.LIST_TYPE, Intent.UNUSED, Intent.HOTSPOTS, Intent.UNTESTED, Intent.LIST_APIS,
        Intent.LIST_TABLES, Intent.SUMMARY) else None
    tgt = EntityRef(text=target_terms[0]) if target_terms else None
    hops = 3
    mh = re.search(r"\b(\d)\s*(?:hops?|levels?|steps?)\b", ql)
    if mh:
        hops = max(1, min(6, int(mh.group(1))))
    plan = QueryPlan(intent=intent, entity=ent, target=tgt, max_hops=hops, subject=subject,
                     include_candidates=not has(r"\bverified only\b", r"\bonly verified\b"))
    return plan, terms, target_terms
