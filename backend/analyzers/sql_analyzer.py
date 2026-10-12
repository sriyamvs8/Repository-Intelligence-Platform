"""SQL analysis: statement splitting (sqlparse) + a conservative token-level extractor for
table reads/writes, CTE exclusion, DDL (CREATE/ALTER) columns, primary and foreign keys.

Used for .sql files AND for SQL literals found in Java/Python/MyBatis code."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlparse

from ..kg.model import (Entity, EntityType, GraphBuilder, RelType, Status, make_id, normalize_table_name,
                        short_hash, table_id)

SQL_START = re.compile(
    r"^\s*(\(\s*)?(select|insert|update|delete|with|merge|replace|create|alter|drop|truncate|upsert)\b", re.I)
SQL_SHAPE = re.compile(
    r"\bselect\b[\s\S]+\bfrom\b|\binsert\s+(ignore\s+)?into\b|\bupdate\b[\s\S]+\bset\b|\bdelete\s+from\b|"
    r"\bmerge\s+into\b|\bcreate\s+(or\s+replace\s+)?(temp(orary)?\s+)?(table|view|index)\b|\balter\s+table\b|"
    r"\bdrop\s+table\b|\btruncate\b|\bwith\b[\s\S]+\bas\s*\(", re.I)

KEYWORDS = {
    "select", "from", "where", "join", "inner", "left", "right", "full", "outer", "cross", "on", "as", "and",
    "or", "not", "in", "is", "null", "group", "by", "order", "having", "limit", "offset", "union", "all",
    "distinct", "insert", "into", "values", "update", "set", "delete", "with", "recursive", "merge", "using",
    "when", "then", "matched", "case", "else", "end", "exists", "lateral", "natural", "fetch", "first", "next",
    "rows", "only", "returning", "table", "create", "alter", "drop", "if", "replace", "ignore", "window",
    "partition", "over", "asc", "desc", "for", "share", "nowait", "skip", "locked", "straight_join", "dual",
    "default", "conflict", "do", "nothing", "duplicate", "key", "primary", "foreign", "references", "unique",
    "constraint", "check", "index", "truncate", "upsert", "except", "intersect", "minus", "use", "force",
    "apply", "outer", "tablesample", "unnest", "like", "between", "any", "some", "escape", "collate",
}
FUNC_WITH_FROM = {"extract", "substring", "trim", "overlay", "position", "substr"}

_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$#]*)'
_TOKEN_RE = re.compile(
    rf"{_IDENT}(?:\s*\.\s*(?:{_IDENT}|\*))*|#\{{[^}}]*\}}|\$\{{[^}}]*\}}|:[A-Za-z_]\w*|\?\d*|%s|\d+(?:\.\d+)?|[(),;=*<>!+\-/|.]")


@dataclass
class TableDef:
    name: str
    columns: list[dict] = field(default_factory=list)       # {name, type, pk, nullable}
    fks: list[tuple[str, str, str]] = field(default_factory=list)  # (col, ref_table, ref_col)
    kind: str = "table"


@dataclass
class SqlFacts:
    op: str
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    defines: list[TableDef] = field(default_factory=list)
    alter_fks: list[tuple[str, str, str, str]] = field(default_factory=list)  # table, col, ref_table, ref_col
    alter_columns: list[tuple[str, dict]] = field(default_factory=list)
    fingerprint: str = ""


def looks_like_sql(text: str | None) -> bool:
    if not text or len(text) < 10:
        return False
    t = text.strip()
    return bool(SQL_START.match(t) and SQL_SHAPE.search(t))


def looks_like_sql_strict(text: str | None) -> bool:
    """Stricter check for SQL-looking strings that are NOT passed to a known execution API."""
    if not looks_like_sql(text):
        return False
    t = text.strip()
    upper_kw = bool(re.match(r"^\s*\(?\s*(SELECT|INSERT|UPDATE|DELETE|WITH|MERGE|CREATE|ALTER)\b", t))
    return upper_kw or bool(re.search(r"[*=,?]|\bwhere\b|\bjoin\b|\bvalues\b|:\w", t, re.I))


def strip_comments_and_strings(sql: str) -> str:
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "#" and (i == 0 or sql[i - 1] in "\n\r") :
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            seg = sql[i:(n if j < 0 else j + 2)]
            out.append("\n" * seg.count("\n") or " ")
            i = n if j < 0 else j + 2
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            out.append("?")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _clean_ident(tok: str) -> str:
    parts = [p.strip().strip('`"[]') for p in re.split(r"\s*\.\s*", tok)]
    return ".".join(p for p in parts if p)


def _is_ident(tok: str) -> bool:
    return bool(re.match(rf"^{_IDENT}", tok)) and not tok[0].isdigit()


def tokenize(sql: str) -> list[str]:
    return _TOKEN_RE.findall(strip_comments_and_strings(sql))


def fingerprint(sql: str) -> str:
    s = strip_comments_and_strings(sql)
    s = re.sub(r"\b\d+(\.\d+)?\b", "?", s)
    s = re.sub(r"\(\s*\?(\s*,\s*\?)*\s*\)", "(?)", s)
    s = re.sub(r"(values\s*\(\?\))(\s*,\s*\(\?\))+", r"\1", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip().rstrip(";").lower()
    return s


def _split_top_level(body: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _matching_paren(s: str, start: int) -> int:
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(s)


_CREATE_RE = re.compile(
    rf"^\s*create\s+(?:or\s+replace\s+)?(?:(?:global|local)\s+)?(?:temp(?:orary)?\s+)?(?:unlogged\s+)?"
    rf"(?P<kind>table|view)\s+(?:if\s+not\s+exists\s+)?(?P<name>{_IDENT}(?:\s*\.\s*{_IDENT})*)", re.I)
_FK_RE = re.compile(
    rf"foreign\s+key\s*\((?P<cols>[^)]*)\)\s*references\s+(?P<ref>{_IDENT}(?:\s*\.\s*{_IDENT})*)\s*(?:\((?P<rcols>[^)]*)\))?", re.I)
_INLINE_REF_RE = re.compile(rf"references\s+(?P<ref>{_IDENT}(?:\s*\.\s*{_IDENT})*)\s*(?:\((?P<rcol>[^)]*)\))?", re.I)
_ALTER_RE = re.compile(rf"^\s*alter\s+table\s+(?:only\s+)?(?:if\s+exists\s+)?(?P<name>{_IDENT}(?:\s*\.\s*{_IDENT})*)", re.I)
_ADD_COL_RE = re.compile(rf"\badd\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?(?P<col>{_IDENT})\s+(?P<type>[A-Za-z][\w ]*(?:\([^)]*\))?)", re.I)
_CONSTRAINT_WORDS = ("constraint", "primary", "foreign", "unique", "key", "index", "check", "fulltext", "spatial", "exclude", "period")


def _parse_create(stmt: str) -> TableDef | None:
    m = _CREATE_RE.match(stmt)
    if not m:
        return None
    td = TableDef(name=_clean_ident(m.group("name")), kind=m.group("kind").lower())
    if td.kind == "view":
        return td
    rest = stmt[m.end():]
    p = rest.find("(")
    if p < 0 or re.match(r"^\s*as\b", rest, re.I):
        return td
    body = rest[p + 1:_matching_paren(rest, p)]
    pk_cols: set[str] = set()
    for part in _split_top_level(body):
        low = part.lower().lstrip()
        fm = re.match(r"[a-z_]+", low)
        first = fm.group(0) if fm else ""
        if first in _CONSTRAINT_WORDS:
            mpk = re.search(r"primary\s+key\s*\(([^)]*)\)", part, re.I)
            if mpk:
                pk_cols |= {_clean_ident(c).lower() for c in mpk.group(1).split(",")}
            for fk in _FK_RE.finditer(part):
                cols = [_clean_ident(c) for c in fk.group("cols").split(",")]
                rcols = [_clean_ident(c) for c in (fk.group("rcols") or "").split(",")] if fk.group("rcols") else [""]
                for i, c in enumerate(cols):
                    td.fks.append((c, _clean_ident(fk.group("ref")), rcols[min(i, len(rcols) - 1)]))
            continue
        mcol = re.match(rf"^\s*({_IDENT})\s*(.*)$", part, re.S)
        if not mcol:
            continue
        cname = _clean_ident(mcol.group(1))
        rest_def = mcol.group(2)
        ctype = re.match(r"^([A-Za-z][\w]*(?:\s+(?:varying|precision|unsigned|zone|with|time|without))*\s*(?:\([^)]*\))?)", rest_def.strip())
        col = {"name": cname, "type": (ctype.group(1).strip() if ctype else ""),
               "pk": bool(re.search(r"\bprimary\s+key\b", rest_def, re.I)),
               "nullable": not re.search(r"\bnot\s+null\b", rest_def, re.I)}
        td.columns.append(col)
        ref = _INLINE_REF_RE.search(rest_def)
        if ref:
            td.fks.append((cname, _clean_ident(ref.group("ref")), _clean_ident(ref.group("rcol") or "")))
    for c in td.columns:
        if c["name"].lower() in pk_cols:
            c["pk"] = True
            c["nullable"] = False
    return td


def analyze_statement(stmt: str, entity_tables: dict[str, str] | None = None) -> SqlFacts:
    """Extract facts from ONE statement. entity_tables maps JPQL entity names -> table names."""
    cleaned = strip_comments_and_strings(stmt)
    toks = _TOKEN_RE.findall(cleaned)
    low = [t.lower() for t in toks]
    first = next((t for t in low if t not in ("(",)), "")
    facts = SqlFacts(op=first.upper() or "OTHER", fingerprint=fingerprint(stmt))

    # --- DDL ------------------------------------------------------------------------
    if first == "create":
        td = _parse_create(cleaned)
        if td:
            facts.op = "CREATE_VIEW" if td.kind == "view" else "CREATE_TABLE"
            facts.defines.append(td)
        else:
            mi = re.search(rf"\bindex\b.*?\bon\s+({_IDENT}(?:\s*\.\s*{_IDENT})*)", cleaned, re.I | re.S)
            if mi:
                facts.op = "CREATE_INDEX"
                facts.writes.append(_clean_ident(mi.group(1)))
            return facts
        if td.kind == "table" and not re.search(r"\bas\s*\(?\s*select\b", cleaned, re.I):
            return facts
    if first == "alter":
        m = _ALTER_RE.match(cleaned)
        if m:
            t = _clean_ident(m.group("name"))
            facts.op = "ALTER_TABLE"
            facts.writes.append(t)
            for fk in _FK_RE.finditer(cleaned):
                cols = [_clean_ident(c) for c in fk.group("cols").split(",")]
                rcols = [_clean_ident(c) for c in (fk.group("rcols") or "").split(",")] if fk.group("rcols") else [""]
                for i, c in enumerate(cols):
                    facts.alter_fks.append((t, c, _clean_ident(fk.group("ref")), rcols[min(i, len(rcols) - 1)]))
            for ac in _ADD_COL_RE.finditer(cleaned):
                name = _clean_ident(ac.group("col"))
                if name.lower() not in _CONSTRAINT_WORDS:
                    facts.alter_columns.append((t, {"name": name, "type": ac.group("type").strip(), "pk": False, "nullable": True}))
        return facts
    if first in ("drop", "truncate"):
        m = re.search(rf"\b(?:table|truncate)\s+(?:if\s+exists\s+)?(?:table\s+)?({_IDENT}(?:\s*\.\s*{_IDENT})*)", cleaned, re.I)
        facts.op = "DROP_TABLE" if first == "drop" else "TRUNCATE"
        if m and m.group(1).lower() not in ("table", "if", "index", "view"):
            facts.writes.append(_clean_ident(m.group(1)))
        return facts

    # --- DML ------------------------------------------------------------------------
    ctes: set[str] = set()
    if "with" in low:
        for m in re.finditer(rf"({_IDENT})\s*(?:\([^()]*\))?\s+as\s*(?:not\s+)?(?:materialized\s+)?\(", cleaned, re.I):
            ctes.add(_clean_ident(m.group(1)).lower())
    owners: list[str] = []     # paren owner stack (function names)
    i = 0
    n = len(toks)

    def take_ident(j: int, allow_paren: bool = False) -> tuple[str | None, int]:
        while j < n and low[j] in ("only", "lateral", "ignore", "table"):
            j += 1
        if j < n and _is_ident(toks[j]) and low[j] not in KEYWORDS:
            if j + 1 < n and toks[j + 1] == "(" and not allow_paren:        # function call e.g. FROM generate_series(...)
                return None, j + 1
            return _clean_ident(toks[j]), j + 1
        return None, j

    def skip_alias(j: int) -> int:
        if j < n and low[j] == "as":
            j += 1
        if j < n and _is_ident(toks[j]) and low[j] not in KEYWORDS:
            j += 1
        return j

    while i < n:
        t, lt = toks[i], low[i]
        if t == "(":
            owners.append(low[i - 1] if i > 0 else "")
            i += 1
            continue
        if t == ")":
            if owners:
                owners.pop()
            i += 1
            continue
        if lt == "from":
            if owners and owners[-1] in FUNC_WITH_FROM:
                i += 1
                continue
            prev = low[i - 1] if i > 0 else ""
            if prev == "delete":
                name, j = take_ident(i + 1)
                if name:
                    facts.writes.append(name)
                i = j
                continue
            if prev == "distinct" or (i > 1 and low[i - 1] == "distinct" and low[i - 2] == "is"):
                i += 1
                continue
            j = i + 1
            while True:
                name, j = take_ident(j)
                if name:
                    facts.reads.append(name)
                    j = skip_alias(j)
                if j < n and toks[j] == ",":
                    j += 1
                    continue
                break
            i = max(j, i + 1)
            continue
        if lt == "join" or lt == "straight_join":
            name, j = take_ident(i + 1)
            if name:
                facts.reads.append(name)
            i = max(j, i + 1)
            continue
        if lt == "into":
            prev = low[i - 1] if i > 0 else ""
            if prev in ("insert", "replace", "merge", "ignore", "upsert"):
                name, j = take_ident(i + 1, allow_paren=True)
                if name:
                    facts.writes.append(name)
                i = max(j, i + 1)
                continue
        if lt == "update":
            prev = low[i - 1] if i > 0 else ""
            if prev not in ("on", "for", "key", "do", "matched", "then", "no") :
                name, j = take_ident(i + 1)
                if name:
                    facts.writes.append(name)
                    j = skip_alias(j)
                i = max(j, i + 1)
                continue
        if lt == "using" and "merge" in low[:i]:
            name, j = take_ident(i + 1)
            if name:
                facts.reads.append(name)
            i = max(j, i + 1)
            continue
        i += 1

    def keep(name: str) -> str | None:
        base = name.split(".")[-1]
        if not base or base == "*" or base.lower() in ctes or base.lower() in KEYWORDS:
            return None
        if base.startswith((":", "?", "#", "$")):
            return None
        if entity_tables and base in entity_tables:
            return entity_tables[base]
        if entity_tables and base.lower() in {k.lower(): v for k, v in entity_tables.items()}:
            return {k.lower(): v for k, v in entity_tables.items()}[base.lower()]
        return name

    facts.reads = list(dict.fromkeys(x for x in (keep(r) for r in facts.reads) if x))
    facts.writes = list(dict.fromkeys(x for x in (keep(w) for w in facts.writes) if x))
    if facts.op in ("WITH", "(") :
        facts.op = "SELECT" if not facts.writes else "WRITE"
    return facts


def split_statements(text: str) -> list[tuple[str, int]]:
    """Split a SQL script into statements, returning (statement, 1-based start line)."""
    out: list[tuple[str, int]] = []
    pos = 0
    for stmt in sqlparse.split(text):
        if not stmt.strip():
            continue
        body = stmt.strip()
        # skip leading comments so the line points at the statement itself
        lead = re.match(r"^(?:\s*(?:--[^\n]*\n|/\*.*?\*/))*\s*", body, re.S)
        core = body[lead.end():] if lead else body
        if not core:
            continue
        idx = text.find(core[:40], pos)
        if idx < 0:
            idx = pos
        line = text.count("\n", 0, idx) + 1
        pos = idx + len(core[:40])
        out.append((core, line))
    return out


# ---------------------------------------------------------------------------------------
# Graph emission
# ---------------------------------------------------------------------------------------

def ensure_table(gb: GraphBuilder, name: str, *, file: str | None = None, line: int | None = None,
                 defined: bool = False, kind: str = "table", source: str = "") -> str:
    tid = table_id(name)
    schema = name.split(".")[0] if "." in name else None
    ent = Entity(id=tid, type=EntityType.TABLE.value, name=normalize_table_name(name),
                 qualified_name=name.strip('`"[]'), file=file if defined else None,
                 line=line if defined else None, language="sql",
                 props={"schema": schema, "kind": kind, "defined_in_schema": defined, "referenced_by": [source] if source else []})
    e = gb.add_entity(ent)
    if defined:
        e.props["defined_in_schema"] = True
        if not e.file:
            e.file, e.line = file, line
    return tid


def ensure_column(gb: GraphBuilder, table: str, col: dict, *, file: str | None, line: int | None,
                  kind: str = "SQL_DDL") -> str:
    tname = normalize_table_name(table)
    cid = make_id("column", f"{tname}.{col['name'].lower()}")
    gb.add_entity(Entity(id=cid, type=EntityType.COLUMN.value, name=col["name"], qualified_name=f"{tname}.{col['name']}",
                         file=file, line=line, language="sql",
                         props={"data_type": col.get("type", ""), "primary_key": col.get("pk", False),
                                "nullable": col.get("nullable", True), "table": tname}))
    gb.rel(table_id(table), cid, RelType.HAS_COLUMN, kind=kind, evidence=f"{col['name']} {col.get('type', '')}".strip(),
           file=file, line=line, reason="column declared in table definition")
    return cid


def add_sql(gb: GraphBuilder, sql: str, *, file: str, line: int | None, language: str,
            executor_id: str | None = None, exec_kind: str = "", exec_evidence: str = "",
            exec_status: Status = Status.VERIFIED, exec_reason: str = "", dynamic: bool = False,
            entity_tables: dict[str, str] | None = None) -> str | None:
    """Create a SqlQuery entity for `sql` and its READS/WRITES/DEFINES/HAS_COLUMN/FOREIGN_KEY edges.
    Returns the SqlQuery id (or None if nothing could be extracted)."""
    try:
        facts = analyze_statement(sql, entity_tables)
    except Exception as exc:  # never let one statement break the whole repository
        gb.diag("WARNING", "SQL_PARSE_ERROR", f"{type(exc).__name__}: {exc}", file, line)
        return None
    if not (facts.reads or facts.writes or facts.defines or facts.alter_fks):
        if executor_id:
            gb.diag("INFO", "SQL_NO_TABLES", f"SQL statement without resolvable tables: {sql[:80]}", file, line)
        return None
    fp = facts.fingerprint
    sid = make_id("sql", file, short_hash(fp))
    main = (facts.defines[0].name if facts.defines else (facts.writes or facts.reads or [""])[0])
    name = f"{facts.op} {normalize_table_name(main)}".strip()
    ent = gb.add_entity(Entity(id=sid, type=EntityType.SQL_QUERY.value, name=name, qualified_name=fp[:200],
                               file=file, line=line, language=language,
                               props={"sql": sql.strip()[:2000], "operation": facts.op, "dynamic": dynamic,
                                      "lines": [line] if line else []}))
    if line and line not in ent.props.setdefault("lines", []) and len(ent.props["lines"]) < 25:
        ent.props["lines"].append(line)
    gb.rel(make_id("file", file), sid, RelType.DEFINES, kind="SQL_PARSE", evidence=sql.strip()[:120], file=file, line=line,
           reason="SQL statement located in this file")
    st = Status.CANDIDATE if dynamic else Status.VERIFIED
    why = "dynamic SQL: only static parts parsed" if dynamic else ""
    for td in facts.defines:
        tid = ensure_table(gb, td.name, file=file, line=line, defined=True, kind=td.kind, source=sid)
        gb.rel(sid, tid, RelType.DEFINES, kind="SQL_DDL", evidence=sql.strip()[:160], file=file, line=line,
               reason=f"CREATE {td.kind.upper()} statement")
        for col in td.columns:
            ensure_column(gb, td.name, col, file=file, line=line)
        for col, ref, rcol in td.fks:
            rt = ensure_table(gb, ref, source=sid)
            gb.rel(tid, rt, RelType.FOREIGN_KEY, kind="SQL_DDL", evidence=f"{col} REFERENCES {ref}({rcol})",
                   file=file, line=line, reason="foreign key constraint", column=col, ref_column=rcol)
    for t, col in facts.alter_columns:
        ensure_table(gb, t, source=sid)
        ensure_column(gb, t, col, file=file, line=line, kind="SQL_DDL_ALTER")
    for t, col, ref, rcol in facts.alter_fks:
        a, b = ensure_table(gb, t, source=sid), ensure_table(gb, ref, source=sid)
        gb.rel(a, b, RelType.FOREIGN_KEY, kind="SQL_DDL_ALTER", evidence=f"{col} REFERENCES {ref}({rcol})",
               file=file, line=line, reason="foreign key added by ALTER TABLE", column=col, ref_column=rcol)
    for r in facts.reads:
        tid = ensure_table(gb, r, source=sid)
        gb.rel(sid, tid, RelType.READS, kind="SQL_PARSE", evidence=sql.strip()[:160], file=file, line=line,
               status=st, reason=why or "table appears in FROM/JOIN/USING clause")
    for w in facts.writes:
        tid = ensure_table(gb, w, source=sid)
        ddl = facts.op in ("ALTER_TABLE", "DROP_TABLE", "TRUNCATE", "CREATE_INDEX")
        gb.rel(sid, tid, RelType.WRITES, kind="SQL_PARSE", evidence=sql.strip()[:160], file=file, line=line,
               status=st, reason=why or (f"DDL {facts.op}" if ddl else f"target of {facts.op}"))
    if executor_id:
        gb.rel(executor_id, sid, RelType.EXECUTES, kind=exec_kind, evidence=exec_evidence or sql.strip()[:160],
               file=file, line=line, status=exec_status, reason=exec_reason or "SQL passed to a recognised execution API")
    return sid


_NAME_RE = re.compile(r"^\s*--\s*name\s*:\s*([A-Za-z_][\w\-]*)", re.M)


def analyze_sql_file(gb: GraphBuilder, rel_path: str, text: str, named: dict | None = None) -> int:
    """Analyze a SQL script. Named statements (aiosql / PugSQL / yesql `-- name: get-user`) are recorded in
    `named` so Python/Java code calling them by name can be linked with EXECUTES."""
    names = [(text.count("\n", 0, m.start()) + 1, m.group(1).replace("-", "_")) for m in _NAME_RE.finditer(text)]
    count = 0
    seen_lines: list[int] = []
    for stmt, line in split_statements(text):
        seen_lines.append(line)
        sid = add_sql(gb, stmt, file=rel_path, line=line, language="sql")
        if not sid:
            continue
        count += 1
        prior = [n for ln, n in names if ln <= line]
        if prior and named is not None:
            qname = prior[-1]
            # only if no other statement started between the name comment and this one
            name_line = max(ln for ln, n in names if ln <= line)
            if not any(name_line < l2 < line for l2 in seen_lines[:-1]):
                named.setdefault(qname, []).append(sid)
                ent = gb.get(sid)
                ent.props["query_name"] = qname
                ent.name = f"{qname} ({ent.props.get('operation', '')})"
    return count
