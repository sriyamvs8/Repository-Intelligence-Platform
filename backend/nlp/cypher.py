"""Natural language -> Cypher (Gemini) with a strict safety validator, plus the reviewed Cypher
templates that correspond to every deterministic intent.

Flow:  question --Gemini--> Cypher text --validate/rewrite--> read-only Neo4j transaction --> rows
Safety (the prompt is NOT a security boundary; this module is):
  * single statement, read clauses only (MATCH/OPTIONAL MATCH/WHERE/WITH/UNWIND/RETURN/ORDER BY/SKIP/LIMIT)
  * forbidden: CREATE MERGE SET DELETE REMOVE DROP CALL LOAD CSV FOREACH USE apoc/db/dbms procedures ...
  * labels, relationship types, property keys and functions must be on allowlists derived from the schema
  * the query must be scoped to the active graph ($gid); results are post-filtered by graph_id as well
  * variable-length patterns are rewritten to at most *..MAX_HOPS; LIMIT is enforced (<= MAX_ROWS)
  * executed in a READ access-mode managed transaction with a server-side timeout (Neo4jStore.run_cypher)
"""
from __future__ import annotations

import json
import logging
import re

from ..config import settings
from ..kg.model import EntityType, RelType

log = logging.getLogger("rkg.cypher")

MAX_HOPS = 6
MAX_ROWS = 200
DEFAULT_LIMIT = 100

ENTITY_LABELS = {e.value for e in EntityType} | {"Entity"}
REL_TYPES = {r.value for r in RelType}
NODE_PROPS = {"id", "graph_id", "type", "name", "qualified_name", "file", "line", "end_line", "language",
              "http_method", "path", "normalized_path", "operation", "sql", "data_type", "primary_key",
              "nullable", "signature", "class", "is_test_code", "documented", "return_type", "table",
              "parse_status", "defined_in_schema", "kind", "spring_data_repository", "operation_id", "summary"}
REL_PROPS = {"id", "type", "status", "source_kind", "evidence", "file", "line", "reason", "confidence", "occurrences",
             "column", "ref_column"}
ALLOWED_PROPS = NODE_PROPS | REL_PROPS
ALLOWED_FUNCS = {"tolower", "toupper", "coalesce", "size", "length", "nodes", "relationships", "labels", "type",
                 "count", "collect", "min", "max", "avg", "sum", "startnode", "endnode", "properties", "keys", "head",
                 "last", "tail", "any", "all", "none", "single", "exists", "range", "split", "substring", "trim",
                 "ltrim", "rtrim", "replace", "tostring", "tointeger", "tofloat", "toboolean", "reverse", "left",
                 "right", "shortestpath", "allshortestpaths", "elementid", "distinct", "abs", "round", "isempty"}
FORBIDDEN = {"CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "LOAD", "CSV", "CALL", "FOREACH",
             "USE", "GRANT", "DENY", "REVOKE", "ALTER", "SHOW", "START", "STOP", "TERMINATE", "PERIODIC", "COMMIT",
             "INDEX", "CONSTRAINT", "IMPORT", "APOC", "DBMS", "FINISH", "INSERT", "UNION", "YIELD", "RENAME",
             "ENABLE", "DISABLE", "SUSPEND", "IMPERSONATE", "TRANSACTIONS", "DATABASE", "USER", "ROLE"}
START_CLAUSES = ("MATCH", "OPTIONAL", "WITH", "UNWIND")

ENTITY_ID_RE = re.compile(r"^(method|function|class|api|table|column|sql|test|file|dir|repo):")


class CypherRejected(ValueError):
    pass


def _strip_strings_and_comments(q: str) -> str:
    q = re.sub(r"'(?:[^'\\]|\\.)*'", "''", q)          # strings first, so '//' inside a string is not a comment
    q = re.sub(r'"(?:[^"\\]|\\.)*"', "''", q)
    q = re.sub(r"//[^\n]*", " ", q)
    q = re.sub(r"/\*.*?\*/", " ", q, flags=re.S)
    return q


def clean_llm_output(text: str) -> str:
    t = (text or "").strip()
    m = re.search(r"```(?:cypher)?\s*(.*?)```", t, re.S | re.I)
    if m:
        t = m.group(1).strip()
    return t.strip().rstrip(";").strip()


def validate_cypher(query: str) -> tuple[str, list[str]]:
    """Return (safe query, notes). Raise CypherRejected with a reason the LLM can use to repair."""
    q = clean_llm_output(query)
    notes: list[str] = []
    if not q:
        raise CypherRejected("empty query")
    if q.upper() == "UNSUPPORTED":
        raise CypherRejected("UNSUPPORTED")
    if len(q) > 3000:
        raise CypherRejected("query too long")
    no_strings = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", q)
    if "//" in no_strings or "/*" in no_strings:
        raise CypherRejected("comments are not allowed")
    bare = _strip_strings_and_comments(q)
    if ";" in bare:
        raise CypherRejected("only a single statement is allowed (no ';')")
    words = {w.upper() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", bare)}
    bad = sorted(words & FORBIDDEN)
    if bad:
        raise CypherRejected(f"forbidden keyword(s): {', '.join(bad)} (read-only queries only)")
    first = re.match(r"\s*([A-Za-z]+)", bare)
    if not first or first.group(1).upper() not in START_CLAUSES:
        raise CypherRejected("query must start with MATCH, OPTIONAL MATCH, WITH or UNWIND")
    if not re.search(r"\bRETURN\b", bare, re.I):
        raise CypherRejected("query must RETURN results")
    if "$gid" not in bare:
        raise CypherRejected("query must be scoped to the active graph with {graph_id: $gid}")
    # functions
    for fn in re.findall(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(", bare):
        if "." in fn:
            raise CypherRejected(f"namespaced function/procedure '{fn}' is not allowed")
        if fn.lower() not in ALLOWED_FUNCS and fn.upper() not in ("IN", "AND", "OR", "NOT", "WHERE", "EXISTS"):
            # a bare '(' after a keyword like MATCH or a variable is a pattern, not a function
            if fn.upper() in ("MATCH", "OPTIONAL", "WITH", "RETURN", "WHERE", "AND", "OR", "NOT", "UNWIND", "AS",
                              "DISTINCT", "BY", "XOR", "WHEN", "THEN", "ELSE", "CASE", "IS", "NULL", "CONTAINS"):
                continue
            raise CypherRejected(f"function '{fn}' is not allowed")
    # property keys (n.prop) and map keys ({prop: ...})
    for prop in re.findall(r"\.\s*([A-Za-z_][A-Za-z0-9_]*)", bare):
        if prop not in ALLOWED_PROPS:
            raise CypherRejected(f"unknown property '{prop}'. Allowed: {', '.join(sorted(ALLOWED_PROPS))}")
    for body in re.findall(r"\{([^{}]*)\}", bare):
        for key in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", body):
            if key not in ALLOWED_PROPS:
                raise CypherRejected(f"unknown property '{key}' in map")
    # labels / relationship types: every ':Name' outside map literals
    no_maps = re.sub(r"\{[^{}]*\}", "{}", bare)
    label_lists = re.findall(r":\s*!?`?[A-Za-z_][A-Za-z0-9_]*`?(?:\s*[|&]\s*:?\s*!?`?[A-Za-z_][A-Za-z0-9_]*`?)*", no_maps)
    for lst in label_lists:
        for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", lst):
            if name not in ENTITY_LABELS and name not in REL_TYPES:
                raise CypherRejected(f"unknown label or relationship type '{name}'")
    # bound variable-length relationships
    def bound(m: re.Match) -> str:
        lo, hi = m.group(1), m.group(3)
        lo_i = int(lo) if lo else 1
        hi_i = int(hi) if hi else (lo_i if (lo and not m.group(2)) else MAX_HOPS)
        hi_i = min(hi_i, MAX_HOPS)
        if lo_i > hi_i:
            lo_i = hi_i
        if not hi or int(hi) > MAX_HOPS:
            notes.append(f"variable-length pattern bounded to *{lo_i}..{hi_i}")
        return f"*{lo_i}..{hi_i}"
    q = re.sub(r"\*\s*(\d*)\s*(\.\.)?\s*(\d*)(?=\s*[\]\{])", bound, q)
    # enforce LIMIT
    m = re.search(r"\bLIMIT\s+(\d+)\s*$", q, re.I)
    if m:
        if int(m.group(1)) > MAX_ROWS:
            q = q[:m.start(1)] + str(MAX_ROWS)
            notes.append(f"LIMIT capped at {MAX_ROWS}")
    elif re.search(r"\bLIMIT\s+\$?\w+\s*$", q, re.I):
        q = re.sub(r"\bLIMIT\s+\$?\w+\s*$", f"LIMIT {DEFAULT_LIMIT}", q, flags=re.I)
    else:
        q = f"{q}\nLIMIT {DEFAULT_LIMIT}"
        notes.append(f"LIMIT {DEFAULT_LIMIT} added")
    return q, notes


# ----------------------------------------------------------------------------------------- prompt
SCHEMA_TEXT = """Neo4j graph of ONE analyzed source-code repository (many repositories share the database;
ALWAYS constrain nodes with {graph_id: $gid}).
Every node has label :Entity plus exactly one type label:
  Repository, Directory, File, Class, Interface, Enum, Method, Constructor, Function, RestApi, SqlQuery, Table, Column, Test
Node properties:
  id (unique, e.g. 'method:com.x.OwnerController.showOwner(int)', 'api:GET /owners/{}', 'table:owners'),
  graph_id, type, name (short name; RestApi name is like 'GET /owners/{}'), qualified_name, file, line, language,
  http_method + path + normalized_path (RestApi; path params are '{}' in normalized_path),
  operation + sql (SqlQuery), data_type + primary_key (Column), signature + class (Method), is_test_code
Relationships (direction is always source -> target):
  (Repository|Directory)-[:CONTAINS]->(Directory|File)
  (File|Class|Interface|SqlQuery)-[:DEFINES]->(Class|Method|Function|Test|SqlQuery|RestApi|Table)
  (File)-[:IMPORTS]->(File|Class|Function)
  (Method|Function|Constructor)-[:CALLS]->(Method|Function|Constructor)
  (Method|Function|Test)-[:INSTANTIATES]->(Class)
  (Class|Interface)-[:EXTENDS]->(Class|Interface)      (Class)-[:IMPLEMENTS]->(Interface)
  (Method)-[:OVERRIDES]->(Method)       implementation method -> interface/super method
  (RestApi)-[:HANDLED_BY]->(Method|Function)
  (Method|Function|Test)-[:EXECUTES]->(SqlQuery)
  (SqlQuery|Method|Function)-[:READS]->(Table)        (SqlQuery|Method|Function)-[:WRITES]->(Table)
  (Table)-[:HAS_COLUMN]->(Column)       (Table)-[:FOREIGN_KEY]->(Table)       (Class)-[:MAPS_TO]->(Table)
  (Test)-[:TESTS]->(Method|Function|Class|RestApi)
  (Method|Function)-[:HTTP_CALLS|CROSS_LANGUAGE_MAP]->(RestApi)
Relationship properties: status ('VERIFIED' or 'CANDIDATE'), evidence, file, line, reason, source_kind"""

RULES_TEXT = """Rules:
- Output ONLY one read-only Cypher query, no explanation, no markdown fences.
- Constrain at least one node with {graph_id: $gid}. Use only the labels, relationship types and properties above.
- Never use CREATE, MERGE, SET, DELETE, REMOVE, CALL, LOAD CSV, FOREACH, UNION, apoc.* or db.* procedures.
- Variable-length relationships must be bounded, e.g. [:CALLS*1..4]; never more than *1..6.
- Match names case-insensitively: toLower(n.name) = toLower('x') or toLower(n.name) CONTAINS toLower('x').
- When "Resolved entities" are given, match them by exact id: n.id = '<id>' (more reliable than names).
- Return scalar columns with clear aliases. For EVERY entity you return also return its id as <alias>_id,
  plus its file and line, e.g.  RETURN c.name AS caller, c.id AS caller_id, c.file AS file, c.line AS line
- Include relationship status when returning relationships: r.status AS status.
- End with LIMIT 100 or less.
- If the question cannot be answered with this schema, output exactly: UNSUPPORTED"""

EXAMPLES = [
    ("Who calls the method findById?",
     "MATCH (c:Entity {graph_id: $gid})-[r:CALLS]->(m:Method {graph_id: $gid})\n"
     "WHERE toLower(m.name) = toLower('findById')\n"
     "RETURN c.name AS caller, c.id AS caller_id, c.file AS file, r.line AS line, m.qualified_name AS callee, "
     "m.id AS callee_id, r.status AS status\nORDER BY caller LIMIT 100"),
    ("Which tables does GET /owners/{id} read or write?",
     "MATCH (a:RestApi {graph_id: $gid}) WHERE a.normalized_path = '/owners/{}' AND a.http_method IN ['GET','ANY']\n"
     "MATCH p = (a)-[:HANDLED_BY]->(h)-[:CALLS|EXECUTES*0..5]->(x)-[rw:READS|WRITES]->(t:Table)\n"
     "RETURN DISTINCT a.name AS api, a.id AS api_id, h.qualified_name AS handler, h.id AS handler_id, "
     "type(rw) AS operation, t.name AS table, t.id AS table_id, rw.file AS file, rw.line AS line LIMIT 100"),
    ("Which columns does the users table have?",
     "MATCH (t:Table {graph_id: $gid})-[:HAS_COLUMN]->(c:Column)\nWHERE toLower(t.name) = 'users'\n"
     "RETURN t.name AS table, t.id AS table_id, c.name AS column, c.id AS column_id, c.data_type AS data_type, "
     "c.primary_key AS primary_key, c.file AS file, c.line AS line LIMIT 100"),
    ("How many classes are in each file of the service package?",
     "MATCH (f:File {graph_id: $gid})-[:DEFINES]->(c:Class)\nWHERE toLower(f.file) CONTAINS 'service'\n"
     "RETURN f.file AS file, f.id AS file_id, count(c) AS classes ORDER BY classes DESC LIMIT 100"),
]


def build_prompt(question: str, resolved: list[dict], context: dict | None) -> str:
    ex = "\n\n".join(f"Question: {q}\nCypher:\n{c}" for q, c in EXAMPLES)
    res = "\n".join(f"  - {n.get('type')}: name={n.get('name')!r} id={n.get('id')!r}" for n in resolved[:8]) or "  (none)"
    ctx = f"\nConversation context entity: {context.get('type')} {context.get('name')!r} id={context.get('id')!r}" if context else ""
    return (f"{SCHEMA_TEXT}\n\n{RULES_TEXT}\n\nExamples:\n{ex}\n\n"
            f"Resolved entities from the question (exact ids in this graph):\n{res}{ctx}\n\n"
            f"Question: {question[:800]}\nCypher:")


def generate_cypher(question: str, resolved: list[dict], context: dict | None,
                    previous: str | None = None, error: str | None = None) -> str | None:
    """Ask Gemini for a Cypher query. Returns raw text or None if Gemini is unavailable."""
    from . import gemini
    if not gemini._available():
        return None
    try:
        from google.genai import types
        prompt = build_prompt(question, resolved, context)
        if previous and error:
            prompt += (f"\n\nYour previous query was rejected or failed:\n{previous}\nReason: {error}\n"
                       f"Write a corrected query that follows all rules.\nCypher:")
        resp = gemini._get_client().models.generate_content(
            model=settings.gemini_model, contents=prompt,
            config=types.GenerateContentConfig(temperature=0, max_output_tokens=700))
        return (resp.text or "").strip()
    except Exception as exc:
        gemini._trip(exc)
        return None


def entity_ids_in_rows(rows: list[dict]) -> list[str]:
    ids: list[str] = []
    for row in rows:
        for v in row.values():
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                if isinstance(x, str) and ENTITY_ID_RE.match(x) and x not in ids:
                    ids.append(x)
                elif isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"] not in ids:
                    ids.append(x["id"])
    return ids


def scope_rows(rows: list[dict], gid: str) -> list[dict]:
    """Drop any row that leaks a node of another graph (defence in depth on top of $gid scoping)."""
    out = []
    for row in rows:
        leak = False
        for v in row.values():
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, dict) and x.get("graph_id") not in (None, gid):
                    leak = True
        if not leak:
            out.append({k: _jsonable(v) for k, v in row.items()})
    return out


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items() if k not in ("props_json", "uid", "graph_id")}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def rows_to_text(rows: list[dict], limit: int = 25) -> str:
    if not rows:
        return "The query returned no rows."
    lines = []
    for r in rows[:limit]:
        parts = []
        for k, v in r.items():
            if k.endswith("_id") or v in (None, "", []):
                continue
            if isinstance(v, (dict, list)):
                v = json.dumps(v, default=str)[:120]
            parts.append(f"{k}: `{v}`" if k not in ("line", "count", "distance", "classes") else f"{k}: {v}")
        lines.append("- " + ", ".join(parts))
    more = f"\n…{len(rows) - limit} more rows" if len(rows) > limit else ""
    return f"{len(rows)} row(s):\n" + "\n".join(lines) + more


# ----------------------------------------------------------------------------------------- templates
# Reviewed, parameterised Cypher equivalents of the deterministic intents (shown with every answer and
# executed against a real Neo4j in CI). Params: $gid, $ids (resolved entity ids), $statuses.
_DEP = "CALLS|HANDLED_BY|TESTS|EXECUTES|READS|WRITES|IMPORTS|EXTENDS|IMPLEMENTS|INSTANTIATES|MAPS_TO|HTTP_CALLS|CROSS_LANGUAGE_MAP"
TEMPLATES: dict[str, str] = {
    "LOCATE_SYMBOL": "MATCH (n:Entity {graph_id: $gid}) WHERE n.id IN $ids\n"
                     "OPTIONAL MATCH (o:Entity)-[:DEFINES]->(n)\n"
                     "RETURN n.type AS type, n.name AS name, n.id AS name_id, n.file AS file, n.line AS line, o.name AS defined_in LIMIT 50",
    "LIST_METHODS": "MATCH (c:Entity {graph_id: $gid})-[:DEFINES]->(m:Entity) WHERE c.id IN $ids\n"
                    "RETURN m.type AS type, m.name AS member, m.id AS member_id, m.signature AS signature, m.line AS line ORDER BY line LIMIT 200",
    "CALLERS": "MATCH (c:Entity {graph_id: $gid})-[r:CALLS|HANDLED_BY|TESTS|HTTP_CALLS|CROSS_LANGUAGE_MAP|INSTANTIATES]->(t:Entity)\n"
               "WHERE t.id IN $ids AND r.status IN $statuses\n"
               "RETURN c.name AS caller, c.id AS caller_id, type(r) AS relationship, r.file AS file, r.line AS line, r.status AS status LIMIT 100",
    "CALLEES": "MATCH (s:Entity {graph_id: $gid})-[r:CALLS|EXECUTES|READS|WRITES|HTTP_CALLS|CROSS_LANGUAGE_MAP|INSTANTIATES]->(t:Entity)\n"
               "WHERE s.id IN $ids AND r.status IN $statuses\n"
               "RETURN type(r) AS relationship, t.name AS target, t.id AS target_id, r.file AS file, r.line AS line, r.status AS status LIMIT 100",
    "API_HANDLER": "MATCH (a:RestApi {graph_id: $gid})-[r:HANDLED_BY]->(h:Entity) WHERE a.id IN $ids\n"
                   "RETURN a.name AS api, a.id AS api_id, h.qualified_name AS handler, h.id AS handler_id, h.file AS file, h.line AS line, "
                   "r.evidence AS evidence, r.status AS status LIMIT 50",
    "API_TO_DATA": "MATCH (a:RestApi {graph_id: $gid}) WHERE a.id IN $ids\n"
                   "MATCH p = (a)-[:HANDLED_BY]->(:Entity)-[:CALLS|HTTP_CALLS|CROSS_LANGUAGE_MAP|HANDLED_BY|EXECUTES*0..6]->(x:Entity)-[rw:READS|WRITES]->(t:Table)\n"
                   "WHERE all(r IN relationships(p) WHERE r.status IN $statuses)\n"
                   "RETURN DISTINCT t.name AS table, t.id AS table_id, type(rw) AS operation, x.name AS via, x.id AS via_id, "
                   "length(p) AS hops ORDER BY hops LIMIT 100",
    "SQL_TABLES": "MATCH (m:Entity {graph_id: $gid}) WHERE m.id IN $ids\n"
                  "MATCH p = (m)-[:CALLS|EXECUTES*0..5]->(x:Entity)-[rw:READS|WRITES]->(t:Table)\n"
                  "RETURN DISTINCT t.name AS table, t.id AS table_id, type(rw) AS operation, x.name AS via, x.id AS via_id LIMIT 100",
    "SCHEMA": "MATCH (t:Table {graph_id: $gid}) WHERE t.id IN $ids\n"
              "OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column)\n"
              "RETURN t.name AS table, t.id AS table_id, c.name AS column, c.id AS column_id, c.data_type AS data_type, "
              "c.primary_key AS primary_key LIMIT 200",
    "TABLE_USAGE": "MATCH (t:Table {graph_id: $gid}) WHERE t.id IN $ids\n"
                   "MATCH (x:Entity)-[rw:READS|WRITES]->(t)\nOPTIONAL MATCH (e:Entity)-[:EXECUTES]->(x)\n"
                   "RETURN coalesce(e.name, x.name) AS code, coalesce(e.id, x.id) AS code_id, type(rw) AS operation, "
                   "rw.file AS file, rw.line AS line, rw.status AS status LIMIT 200",
    "DEPENDENCIES": "MATCH (s:Entity {graph_id: $gid}) WHERE s.id IN $ids\n"
                    f"MATCH p = (s)-[:DEFINES|{_DEP}*1..3]->(d:Entity)\n"
                    "WHERE all(r IN relationships(p) WHERE r.status IN $statuses)\n"
                    "RETURN d.type AS type, d.name AS dependency, d.id AS dependency_id, min(length(p)) AS distance ORDER BY distance LIMIT 200",
    "IMPACT": "MATCH (t:Entity {graph_id: $gid}) WHERE t.id IN $ids\n"
              f"MATCH p = (d:Entity)-[:{_DEP}|OVERRIDES*1..4]->(t)\n"
              "WHERE all(r IN relationships(p) WHERE r.status IN $statuses)\n"
              "RETURN d.type AS type, d.name AS affected, d.id AS affected_id, d.file AS file, d.line AS line, "
              "min(length(p)) AS distance ORDER BY distance LIMIT 200",
    "TEST_COVERAGE": "MATCH (t:Entity {graph_id: $gid}) WHERE t.id IN $ids\n"
                     "MATCH (x:Test)-[r:TESTS]->(u:Entity)\n"
                     "WHERE u = t OR (u)-[:HANDLED_BY|CALLS]->(t)\n"
                     "RETURN DISTINCT x.name AS test, x.id AS test_id, u.name AS via, x.file AS file, x.line AS line LIMIT 200",
    "PATH": "MATCH (a:Entity {graph_id: $gid}), (b:Entity {graph_id: $gid}) WHERE a.id = $ids[0] AND b.id = $ids[1]\n"
            "MATCH p = shortestPath((a)-[*1..6]-(b))\n"
            "WHERE none(r IN relationships(p) WHERE type(r) = 'CONTAINS')\n"
            "RETURN [n IN nodes(p) | n.name] AS names, [n IN nodes(p) | n.id] AS path_ids, [r IN relationships(p) | type(r)] AS relationships LIMIT 1",
    "LIST_APIS": "MATCH (a:RestApi {graph_id: $gid})\nOPTIONAL MATCH (a)-[:HANDLED_BY]->(h:Entity)\n"
                 "RETURN a.name AS api, a.id AS api_id, h.qualified_name AS handler, h.id AS handler_id ORDER BY api LIMIT 200",
    "LIST_TABLES": "MATCH (t:Table {graph_id: $gid})\nOPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column)\n"
                   "RETURN t.name AS table, t.id AS table_id, count(c) AS columns ORDER BY table LIMIT 200",
    "SUMMARY": "MATCH (n:Entity {graph_id: $gid})\nRETURN n.type AS type, count(*) AS count ORDER BY count DESC LIMIT 50",
    "COUNT": "MATCH (n:Entity {graph_id: $gid})\nRETURN n.type AS type, count(*) AS count ORDER BY count DESC LIMIT 50",
    "LIST_TYPE": "MATCH (n:Entity {graph_id: $gid}) WHERE n.type IN ['Class', 'Interface', 'Function']\n"
                 "AND coalesce(n.is_test_code, false) = false\n"
                 "RETURN n.type AS type, n.name AS name, n.id AS name_id, n.file AS file, n.line AS line ORDER BY name LIMIT 200",
    "FLOW": "MATCH (a:RestApi {graph_id: $gid}) WHERE a.id IN $ids\n"
            "MATCH p = (a)-[:HANDLED_BY|CALLS|EXECUTES|READS|WRITES*1..8]->(x:Entity)\n"
            "WHERE all(r IN relationships(p) WHERE r.status IN $statuses)\n"
            "RETURN [n IN nodes(p) | n.name] AS steps, [r IN relationships(p) | type(r)] AS hops_via, length(p) AS hops "
            "ORDER BY hops LIMIT 100",
    "EXPLAIN": "MATCH (n:Entity {graph_id: $gid}) WHERE n.id IN $ids\n"
               "OPTIONAL MATCH (n)-[:CALLS|EXECUTES|READS|WRITES|HANDLED_BY|DEFINES]->(o:Entity)\n"
               "OPTIONAL MATCH (i:Entity)-[:CALLS|HANDLED_BY|TESTS|INSTANTIATES]->(n)\n"
               "RETURN n.name AS name, n.id AS name_id, n.type AS type, n.file AS file, n.line AS line, "
               "collect(DISTINCT o.name) AS uses, collect(DISTINCT i.name) AS used_by LIMIT 10",
    "UNUSED": "MATCH (m:Entity {graph_id: $gid}) WHERE m.type IN ['Method', 'Function'] AND coalesce(m.is_test_code, false) = false\n"
              "AND NOT EXISTS { (m)<-[:CALLS|HANDLED_BY|TESTS|HTTP_CALLS|CROSS_LANGUAGE_MAP]-(:Entity) }\n"
              "AND NOT EXISTS { (m)-[:OVERRIDES]->(:Entity) }\n"
              "RETURN m.qualified_name AS method, m.id AS method_id, m.file AS file, m.line AS line LIMIT 200",
    "HOTSPOTS": "MATCH (c:Entity {graph_id: $gid})-[r:CALLS]->(m:Entity)\n"
                "RETURN m.qualified_name AS method, m.id AS method_id, count(r) AS callers ORDER BY callers DESC LIMIT 25",
    "UNTESTED": "MATCH (a:RestApi {graph_id: $gid})\nWHERE NOT EXISTS { (:Test)-[:TESTS]->(a) }\n"
                "AND NOT EXISTS { (a)-[:HANDLED_BY]->(:Entity)<-[:TESTS]-(:Test) }\n"
                "OPTIONAL MATCH (a)-[:HANDLED_BY]->(h:Entity)\n"
                "RETURN a.name AS api, a.id AS api_id, h.qualified_name AS handler, h.id AS handler_id ORDER BY api LIMIT 200",
}

TEMPLATE_NOTES = {
    "API_TO_DATA": "The engine additionally follows interface → implementation (reverse OVERRIDES) hops and "
                   "counts only CALLS/HTTP hops towards the depth limit.",
    "IMPACT": "The engine uses a weighted traversal (SQL-statement, interface and handler hops are free) and "
              "expands class members only for the root.",
    "TEST_COVERAGE": "The engine also adds members of a class and tests of endpoints handled by it.",
}


def template_for(intent: str, ids: list[str], include_candidates: bool) -> dict | None:
    q = TEMPLATES.get(intent)
    if not q:
        return None
    return {"query": q, "params": {"gid": "<active graph>", "ids": ids[:10],
                                   "statuses": ["VERIFIED", "CANDIDATE"] if include_candidates else ["VERIFIED"]},
            "source": "reviewed template", "note": TEMPLATE_NOTES.get(intent, "")}
