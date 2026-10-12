"""Java analyzer built on tree-sitter (error tolerant; supports records, text blocks, switch
expressions, generics, annotations).

Phase A (declare):   types, fields, constants, methods, routes, annotations  -> index
Phase B (bodies):    calls, instantiations, SQL literals, HTTP client calls  -> pending records
Phase C (resolve):   type resolution through imports/package/nesting, call resolution through
                     receiver types + class hierarchy, Spring Data / JPA mapping, SQL emission.
Ambiguous resolutions become CANDIDATE edges; nothing is linked on name equality alone
unless the simple name is unique in the repository (and then it is CANDIDATE)."""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field

import tree_sitter_java as tsj
from tree_sitter import Language, Node, Parser

from ..kg.model import Entity, EntityType, RelType, Status, make_id, snake_case
from .common import AnalysisContext, HttpCall, add_route
from .sql_analyzer import add_sql, ensure_column, ensure_table, looks_like_sql, looks_like_sql_strict

JAVA_LANG = Language(tsj.language())
_parser = Parser(JAVA_LANG)

TYPE_DECLS = {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
              "annotation_type_declaration"}
SPRING_MAPPINGS = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE",
                   "PatchMapping": "PATCH", "RequestMapping": None}
JAXRS_VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
SPRING_DATA_BASES = {"Repository", "CrudRepository", "PagingAndSortingRepository", "JpaRepository",
                     "ListCrudRepository", "ListPagingAndSortingRepository", "MongoRepository",
                     "ReactiveCrudRepository", "ReactiveSortingRepository", "R2dbcRepository",
                     "CassandraRepository", "ElasticsearchRepository", "Neo4jRepository", "RevisionRepository"}
JAVA_SQL_EXEC = {"query", "queryForObject", "queryForList", "queryForMap", "queryForRowSet", "queryForStream",
                 "queryForLong", "queryForInt", "update", "batchUpdate", "execute", "executeQuery",
                 "executeUpdate", "executeLargeUpdate", "prepareStatement", "prepareCall", "createQuery",
                 "createNativeQuery", "createSQLQuery", "addBatch", "sql", "nativeQuery", "queryForSingleResult",
                 "fetch", "select", "insertInto", "createStatement"}
MYBATIS_ANN = {"Select", "Insert", "Update", "Delete", "SelectProvider"}
TEST_ANN = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate"}
LOMBOK_ACCESSORS = {"Data", "Getter", "Setter", "Value", "Builder", "SuperBuilder"}
REST_CLIENT_METHODS = {"getForObject": "GET", "getForEntity": "GET", "postForObject": "POST",
                       "postForEntity": "POST", "postForLocation": "POST", "put": "PUT", "delete": "DELETE",
                       "patchForObject": "PATCH"}
HTTP_VERB_CALLS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH",
                   "head": "HEAD", "options": "OPTIONS"}
READ_PREFIX = ("find", "get", "read", "query", "search", "stream", "count", "exists", "load", "fetch", "select", "list")
WRITE_PREFIX = ("save", "delete", "remove", "insert", "update", "persist", "merge", "flush", "add", "create")


def _t(node: Node | None, src: bytes) -> str:
    return "" if node is None else src[node.start_byte:node.end_byte].decode("utf-8", "ignore")


def _base_type(type_text: str) -> str:
    t = re.sub(r"@\w+(\([^)]*\))?\s*", "", type_text or "").strip()
    t = re.sub(r"<.*>", "", t, flags=re.S).replace("[]", "").replace("...", "").strip()
    return t


def _generic_args(type_text: str) -> list[str]:
    m = re.search(r"<(.*)>", type_text or "", re.S)
    if not m:
        return []
    depth, cur, out = 0, "", []
    for ch in m.group(1):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(_base_type(cur))
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(_base_type(cur))
    return out


@dataclass
class JMethod:
    id: str
    name: str
    cls: str
    file: str
    params: list[tuple[str, str]]
    ret: str | None
    line: int
    end_line: int
    kind: str                     # Method | Constructor | Test
    static: bool = False
    varargs: bool = False
    annotations: dict = field(default_factory=dict)
    body: Node | None = None


@dataclass
class JType:
    fqn: str
    simple: str
    kind: str
    file: str
    line: int
    extends: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)
    super_generics: dict = field(default_factory=dict)       # base simple -> [type args]
    fields: dict = field(default_factory=dict)               # name -> type text
    field_nodes: list = field(default_factory=list)          # (name, type, annotations, line, static)
    mock_fields: set = field(default_factory=set)            # fields annotated @Mock/@MockBean/... or = mock(X.class)
    consts: dict = field(default_factory=dict)               # name -> string value
    methods: list = field(default_factory=list)
    annotations: dict = field(default_factory=dict)
    outer: str | None = None
    id: str = ""


@dataclass
class JFile:
    path: str
    package: str
    src: bytes
    imports: dict = field(default_factory=dict)
    wildcards: list = field(default_factory=list)
    static_imports: dict = field(default_factory=dict)     # member -> class fqn
    static_wild: list = field(default_factory=list)
    types: list = field(default_factory=list)
    is_test: bool = False


MOCK_ANNOTATIONS = {"Mock", "MockBean", "MockitoBean"}   # @Spy/@SpyBean call real code, so they still count


@dataclass
class PendingCall:
    caller: JMethod
    recv: tuple | None
    name: str
    argc: int | None
    line: int
    evidence: str


class JavaIndex:
    def __init__(self) -> None:
        self.types: dict[str, JType] = {}
        self.by_simple: dict[str, list[str]] = {}
        self.files: dict[str, JFile] = {}
        self.methods: dict[str, JMethod] = {}
        self.calls: list[PendingCall] = []
        self.news: list[tuple[JMethod, str, int]] = []
        self.sql: list[dict] = []
        self.feign_routes: dict[str, list[tuple[str, str]]] = {}
        self.locals_cache: dict[str, dict] = {}
        self.returns: dict[str, list] = {}         # method id -> [(string, dynamic)] returned (views/redirects)


# =========================================================================================
# Annotation helpers
# =========================================================================================

def _annotations(mod_node: Node | None, src: bytes) -> dict[str, Node]:
    out: dict[str, Node] = {}
    if mod_node is None:
        return out
    for ch in mod_node.named_children:
        if ch.type in ("annotation", "marker_annotation"):
            name = _t(ch.child_by_field_name("name"), src).split(".")[-1]
            out[name] = ch
    return out


def _modifiers(node: Node) -> Node | None:
    for ch in node.named_children:
        if ch.type == "modifiers":
            return ch
    return None


def _decode_string(node: Node, src: bytes) -> str:
    if node.type != "string_literal":
        return ""
    raw = _t(node, src)
    if raw.startswith('"""'):
        body = raw[3:-3]
        if body.startswith("\n"):
            body = body[1:]
        return textwrap.dedent(body).replace("\\\n", "")
    parts = []
    for ch in node.named_children:
        if ch.type in ("string_fragment", "multiline_string_fragment"):
            parts.append(_t(ch, src))
        elif ch.type == "escape_sequence":
            esc = _t(ch, src)
            parts.append({"\\n": "\n", "\\t": "\t", '\\"': '"', "\\\\": "\\", "\\'": "'"}.get(esc, ""))
    if not node.named_children and len(raw) >= 2:
        return raw[1:-1]
    return "".join(parts)


class _StrCtx:
    def __init__(self, analyzer: "JavaAnalyzer", jtype: JType | None, local_str: dict | None = None):
        self.a, self.jtype, self.local_str = analyzer, jtype, local_str or {}


def _static_string(node: Node | None, src: bytes, sc: _StrCtx | None) -> tuple[str | None, bool]:
    """Best-effort compile-time string evaluation. Returns (value, dynamic)."""
    if node is None:
        return None, False
    tp = node.type
    if tp == "string_literal":
        return _decode_string(node, src), False
    if tp == "parenthesized_expression" and node.named_children:
        return _static_string(node.named_children[0], src, sc)
    if tp == "binary_expression" and _t(node.child_by_field_name("operator"), src) == "+":
        l, dl = _static_string(node.child_by_field_name("left"), src, sc)
        r, dr = _static_string(node.child_by_field_name("right"), src, sc)
        if l is None and r is None:
            return None, True
        return (l if l is not None else "?") + (r if r is not None else "?"), dl or dr or l is None or r is None
    if tp == "identifier" and sc is not None:
        name = _t(node, src)
        if name in sc.local_str:
            return sc.local_str[name], False
        val = sc.a.lookup_const(sc.jtype, name)
        return (val, False) if val is not None else (None, False)
    if tp == "field_access" and sc is not None:
        obj, fld = _t(node.child_by_field_name("object"), src), _t(node.child_by_field_name("field"), src)
        val = sc.a.lookup_const_in_type(sc.jtype, obj, fld)
        return (val, False) if val is not None else (None, False)
    if tp == "method_invocation":
        name = _t(node.child_by_field_name("name"), src)
        args = node.child_by_field_name("arguments")
        obj = node.child_by_field_name("object")
        if name in ("format", "formatted") :
            target = args.named_children[0] if (name == "format" and args and args.named_children) else obj
            v, _ = _static_string(target, src, sc)
            if v is not None:
                return re.sub(r"%[sdf]", "?", v), True
        if name in ("concat", "trim", "strip", "toUpperCase", "toLowerCase") and obj is not None:
            v, d = _static_string(obj, src, sc)
            if v is not None:
                return v, True
    return None, False


def _ann_values(ann: Node, src: bytes, sc: _StrCtx | None) -> dict[str, list]:
    """Return {key: [values]} for an annotation; positional value -> 'value'.
    Values are strings when statically evaluable, else the raw source text of the expression."""
    out: dict[str, list] = {}
    args = ann.child_by_field_name("arguments")
    if args is None:
        return out

    def vals(node: Node) -> list:
        if node.type == "element_value_array_initializer":
            res = []
            for ch in node.named_children:
                res.extend(vals(ch))
            return res
        s, _ = _static_string(node, src, sc)
        if s is not None:
            return [s]
        return [_t(node, src)]

    for ch in args.named_children:
        if ch.type == "element_value_pair":
            out.setdefault(_t(ch.child_by_field_name("key"), src), []).extend(vals(ch.child_by_field_name("value")))
        else:
            out.setdefault("value", []).extend(vals(ch))
    return out


def _join_paths(prefix: str, path: str) -> str:
    p = "/".join(x.strip("/") for x in (prefix, path) if x and x.strip("/"))
    return "/" + p


# =========================================================================================
# Analyzer
# =========================================================================================

class JavaAnalyzer:
    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.gb = ctx.gb
        self.idx = JavaIndex()

    # ------------------------------------------------------------------ Phase A: declare
    def declare_file(self, path: str, text: str, is_test: bool) -> None:
        src = text.encode("utf-8", "ignore")
        tree = _parser.parse(src)
        root = tree.root_node
        jf = JFile(path=path, package="", src=src, is_test=is_test)
        self.idx.files[path] = jf
        if root.has_error:
            self.gb.diag("WARNING", "PARSE_PARTIAL", "Java syntax errors; extraction may be incomplete", path, None)
            self.gb.get(make_id("file", path)).props["parse_status"] = "PARTIAL"
        for ch in root.named_children:
            if ch.type == "package_declaration":
                jf.package = _t(ch.named_children[0], src) if ch.named_children else ""
            elif ch.type == "import_declaration":
                txt = _t(ch, src)
                static = " static " in f" {txt} "
                wildcard = any(c.type == "asterisk" for c in ch.named_children)
                name = next((_t(c, src) for c in ch.named_children if c.type in ("scoped_identifier", "identifier")), "")
                if static:
                    if wildcard:
                        jf.static_wild.append(name)
                    else:
                        cls, _, member = name.rpartition(".")
                        jf.static_imports[member] = cls
                elif wildcard:
                    jf.wildcards.append(name)
                elif name:
                    jf.imports[name.rsplit(".", 1)[-1]] = name
        for ch in root.named_children:
            if ch.type in TYPE_DECLS:
                self._declare_type(ch, jf, outer=None)

    def _declare_type(self, node: Node, jf: JFile, outer: JType | None) -> None:
        src = jf.src
        name = _t(node.child_by_field_name("name"), src)
        if not name:
            return
        fqn = f"{outer.fqn}.{name}" if outer else (f"{jf.package}.{name}" if jf.package else name)
        kind = {"interface_declaration": EntityType.INTERFACE, "enum_declaration": EntityType.ENUM,
                "annotation_type_declaration": EntityType.INTERFACE}.get(node.type, EntityType.CLASS)
        line = node.start_point[0] + 1
        mods = _modifiers(node)
        anns = _annotations(mods, src)
        jt = JType(fqn=fqn, simple=name, kind=kind.value, file=jf.path, line=line, annotations=anns,
                   outer=outer.fqn if outer else None, id=make_id("class", fqn))
        # supertypes
        sc = node.child_by_field_name("superclass")
        if sc is not None:
            for tnode in sc.named_children:
                txt = _t(tnode, src)
                jt.extends.append(_base_type(txt))
                jt.super_generics[_base_type(txt)] = _generic_args(txt)
        for ch in node.named_children:
            if ch.type in ("super_interfaces", "extends_interfaces"):
                for tl in ch.named_children:
                    for tnode in (tl.named_children if tl.type == "type_list" else [tl]):
                        txt = _t(tnode, src)
                        b = _base_type(txt)
                        jt.super_generics[b] = _generic_args(txt)
                        (jt.extends if (ch.type == "extends_interfaces") else jt.implements).append(b)
        self.idx.types[fqn] = jt
        self.idx.by_simple.setdefault(name, []).append(fqn)
        jf.types.append(fqn)
        end = node.end_point[0] + 1
        props = {"annotations": sorted(anns), "package": jf.package, "is_test_code": jf.is_test}
        # same annotation name, different meaning: MapStruct @Mapper (DTO mapping) vs MyBatis @Mapper (SQL)
        if "Mapper" in anns:
            src_imp = jf.imports.get("Mapper", "") or next((w for w in jf.wildcards if "mapstruct" in w or "ibatis" in w), "")
            props["annotation_origin"] = "mapstruct" if "mapstruct" in src_imp else ("mybatis" if "ibatis" in src_imp else "")
        if node.type == "record_declaration":
            props["record"] = True
        self.gb.add_entity(Entity(id=jt.id, type=kind.value, name=name, qualified_name=fqn, file=jf.path,
                                  line=line, end_line=end, language="java", props=props))
        parent = outer.id if outer else make_id("file", jf.path)
        self.gb.rel(parent, jt.id, RelType.DEFINES, kind="JAVA_AST", evidence=f"{node.type.split('_')[0]} {name}",
                    file=jf.path, line=line, reason="type declaration")
        # record components behave as fields
        if node.type == "record_declaration":
            params = node.child_by_field_name("parameters")
            for p in (params.named_children if params else []):
                pn, ptype = _t(p.child_by_field_name("name"), src), _t(p.child_by_field_name("type"), src)
                if pn:
                    jt.fields[pn] = ptype
                    jt.field_nodes.append((pn, ptype, {}, p.start_point[0] + 1, False))
        body = node.child_by_field_name("body")
        if body is None:
            return
        members = list(body.named_children)
        for m in list(members):
            if m.type == "enum_body_declarations":
                members.extend(m.named_children)
        # fields first (constants needed when evaluating annotations)
        for m in members:
            if m.type in ("field_declaration", "constant_declaration"):
                self._declare_field(m, jt, jf)
        for m in members:
            if m.type in TYPE_DECLS:
                self._declare_type(m, jf, outer=jt)
            elif m.type in ("method_declaration", "constructor_declaration", "compact_constructor_declaration"):
                self._declare_method(m, jt, jf)

    def _declare_field(self, node: Node, jt: JType, jf: JFile) -> None:
        src = jf.src
        ftype = _t(node.child_by_field_name("type"), src)
        mods = _modifiers(node)
        mtext = _t(mods, src) if mods is not None else ""
        anns = _annotations(mods, src)
        static = " static " in f" {mtext} " or jt.kind == EntityType.INTERFACE.value
        for d in node.named_children:
            if d.type != "variable_declarator":
                continue
            fname = _t(d.child_by_field_name("name"), src)
            jt.fields[fname] = ftype
            jt.field_nodes.append((fname, ftype, anns, d.start_point[0] + 1, static))
            val = d.child_by_field_name("value")
            if MOCK_ANNOTATIONS & set(anns) or (val is not None and re.match(r"^(Mockito\.)?mock\s*\(", _t(val, src))):
                jt.mock_fields.add(fname)
            if val is not None and _base_type(ftype) == "String":
                s, dyn = _static_string(val, src, _StrCtx(self, jt))
                if s is not None:
                    jt.consts[fname] = s

    def _declare_method(self, node: Node, jt: JType, jf: JFile) -> None:
        src = jf.src
        is_ctor = node.type != "method_declaration"
        name = jt.simple if is_ctor else _t(node.child_by_field_name("name"), src)
        params: list[tuple[str, str]] = []
        varargs = False
        pnode = node.child_by_field_name("parameters")
        for p in (pnode.named_children if pnode else []):
            if p.type == "formal_parameter":
                params.append((_t(p.child_by_field_name("type"), src), _t(p.child_by_field_name("name"), src)))
            elif p.type == "spread_parameter":
                varargs = True
                tnode = next((c for c in p.named_children if c.type not in ("modifiers", "variable_declarator")), None)
                vd = next((c for c in p.named_children if c.type == "variable_declarator"), None)
                params.append((_t(tnode, src), _t(vd.child_by_field_name("name"), src) if vd else ""))
        if node.type == "compact_constructor_declaration":
            params = [(t, n) for n, t in jt.fields.items()]
        mods = _modifiers(node)
        anns = _annotations(mods, src)
        mtext = _t(mods, src) if mods is not None else ""
        sig = ",".join(_base_type(t) for t, _ in params)
        line, end = node.start_point[0] + 1, node.end_point[0] + 1
        is_test = bool(TEST_ANN & set(anns)) or (jf.is_test and name.startswith("test") and
                                                    "TestCase" in jt.extends)
        if is_test:
            kind, mid = EntityType.TEST.value, make_id("test", f"{jt.fqn}.{name}({sig})")
        elif is_ctor:
            kind, mid = EntityType.CONSTRUCTOR.value, make_id("method", f"{jt.fqn}.<init>({sig})")
        else:
            kind, mid = EntityType.METHOD.value, make_id("method", f"{jt.fqn}.{name}({sig})")
        ret = None if is_ctor else _t(node.child_by_field_name("type"), src)
        jm = JMethod(id=mid, name=name, cls=jt.fqn, file=jf.path, params=params, ret=ret, line=line, end_line=end,
                     kind=kind, static=" static " in f" {mtext} ", varargs=varargs, annotations=anns,
                     body=node.child_by_field_name("body"))
        jt.methods.append(jm)
        self.idx.methods[mid] = jm
        first_line = src[node.start_byte:node.end_byte].decode("utf-8", "ignore").split("{")[0]
        signature = re.sub(r"\s+", " ", re.sub(r"@\w+(\([^)]*\))?", "", first_line)).strip()[:200]
        self.gb.add_entity(Entity(id=mid, type=kind, name=name, qualified_name=f"{jt.fqn}.{name}({sig})",
                                  file=jf.path, line=line, end_line=end, language="java",
                                  props={"class": jt.fqn, "signature": signature, "annotations": sorted(anns),
                                         "parameters": [f"{_base_type(t)} {n}" for t, n in params],
                                         "return_type": _base_type(ret) if ret else None,
                                         "is_test_code": jf.is_test}))
        self.gb.rel(jt.id, mid, RelType.DEFINES, kind="JAVA_AST", evidence=signature, file=jf.path, line=line,
                    reason="member declaration")
        sc = _StrCtx(self, jt)
        self._routes_for_method(jm, jt, jf, anns, sc)
        # SQL annotations
        if "Query" in anns:
            vals = _ann_values(anns["Query"], src, sc)
            q = (vals.get("value") or [None])[0]
            native = any(v.lower() == "true" for v in vals.get("nativeQuery", []))
            if q and isinstance(q, str):
                self.idx.sql.append(dict(executor=mid, sql=q, file=jf.path, line=line, kind="SPRING_DATA_QUERY",
                                         status=Status.VERIFIED, dynamic=False, jpql=not native,
                                         evidence=f"@Query on {name}"))
        for a in MYBATIS_ANN & set(anns):
            vals = _ann_values(anns[a], src, sc)
            q = " ".join(v for v in vals.get("value", []) if isinstance(v, str))
            if q and looks_like_sql(q.strip()):
                self.idx.sql.append(dict(executor=mid, sql=q, file=jf.path, line=line, kind="MYBATIS_ANNOTATION",
                                         status=Status.VERIFIED, dynamic="${" in q, jpql=False,
                                         evidence=f"@{a} on {name}"))

    def _class_prefixes(self, jt: JType, jf: JFile) -> tuple[list[str], str]:
        sc = _StrCtx(self, jt)
        if "RequestMapping" in jt.annotations:
            vals = _ann_values(jt.annotations["RequestMapping"], jf.src, sc)
            paths = vals.get("value", []) + vals.get("path", [])
            return [p for p in paths if isinstance(p, str)] or [""], "spring"
        if "Path" in jt.annotations:
            vals = _ann_values(jt.annotations["Path"], jf.src, sc)
            return [p for p in vals.get("value", []) if isinstance(p, str)] or [""], "jaxrs"
        if "FeignClient" in jt.annotations:
            vals = _ann_values(jt.annotations["FeignClient"], jf.src, sc)
            return [p for p in vals.get("path", []) if isinstance(p, str)] or [""], "feign"
        return [""], "spring"

    def _routes_for_method(self, jm: JMethod, jt: JType, jf: JFile, anns: dict, sc: _StrCtx) -> None:
        if jm.kind == EntityType.TEST.value:
            return
        found: list[tuple[str, str, str]] = []   # (verb, path, kind)
        prefixes, style = self._class_prefixes(jt, jf)
        outer = self.idx.types.get(jt.outer) if jt.outer else None
        for ann, verb in SPRING_MAPPINGS.items():
            if ann not in anns:
                continue
            vals = _ann_values(anns[ann], jf.src, sc)
            paths = [p for p in vals.get("value", []) + vals.get("path", []) if isinstance(p, str)] or [""]
            if any(not isinstance(p, str) or p.startswith(("${", "#{")) for p in vals.get("value", []) + vals.get("path", [])):
                self.gb.diag("INFO", "DYNAMIC_ROUTE", f"non-literal route on {jm.name}", jf.path, jm.line)
            verbs = [verb] if verb else ([v.split(".")[-1].upper() for v in vals.get("method", [])] or ["ANY"])
            for v in verbs:
                for pre in prefixes:
                    for p in paths:
                        found.append((v, _join_paths(pre, p), "SPRING_ANNOTATION"))
        jax_verbs = JAXRS_VERBS & set(anns)
        if jax_verbs and ("Path" in jt.annotations or "Path" in anns):
            mpaths = [p for p in _ann_values(anns["Path"], jf.src, sc).get("value", []) if isinstance(p, str)] if "Path" in anns else [""]
            for v in jax_verbs:
                for pre in prefixes:
                    for p in mpaths or [""]:
                        found.append((v, _join_paths(pre, p), "JAXRS_ANNOTATION"))
        if not found:
            return
        if style == "feign" or "FeignClient" in jt.annotations:
            self.idx.feign_routes[jm.id] = [(v, p) for v, p, _ in found]
            return
        for v, p, kind in found:
            ann_name = next((a for a in anns if a in SPRING_MAPPINGS or a in JAXRS_VERBS), "")
            add_route(self.ctx, method=v, path=p, handler_id=jm.id, file=jf.path, line=jm.line, language="java",
                      kind=kind, evidence=f"@{ann_name} {p} on {jt.simple}.{jm.name}",
                      reason=f"@{ann_name} annotation on handler method")

    # ------------------------------------------------------------------ constants lookup
    def lookup_const(self, jt: JType | None, name: str) -> str | None:
        seen = set()
        while jt is not None and jt.fqn not in seen:
            seen.add(jt.fqn)
            if name in jt.consts:
                return jt.consts[name]
            nxt = None
            for s in jt.extends + jt.implements:
                r = self.resolve_type(s, jt)
                if r and r[0] in self.idx.types:
                    st = self.idx.types[r[0]]
                    if name in st.consts:
                        return st.consts[name]
                    nxt = nxt or st
            jt = nxt or (self.idx.types.get(jt.outer) if jt.outer else None)
        return None

    def lookup_const_in_type(self, jt: JType | None, type_name: str, fld: str) -> str | None:
        if jt is None:
            return None
        r = self.resolve_type(type_name, jt)
        if r and r[0] in self.idx.types:
            return self.lookup_const(self.idx.types[r[0]], fld)
        return None

    # ------------------------------------------------------------------ type resolution
    def resolve_type(self, name: str, jt: JType | None, jf: JFile | None = None) -> tuple[str, bool] | None:
        """Resolve a simple/qualified type name in the scope of jt. Returns (fqn, is_candidate)."""
        name = _base_type(name or "")
        if not name or name in ("var", "void", "int", "long", "boolean", "double", "float", "char", "byte", "short"):
            return None
        idx = self.idx
        if name in idx.types:
            return name, False
        if jf is None and jt is not None:
            jf = idx.files.get(jt.file)
        head, _, rest = name.partition(".")
        # nested types visible from current class chain
        cur = jt
        while cur is not None:
            cand = f"{cur.fqn}.{name}"
            if cand in idx.types:
                return cand, False
            for s in cur.extends + cur.implements:
                sfq = f"{(jf.package + '.') if jf and jf.package else ''}{s}"
                if f"{sfq}.{name}" in idx.types:
                    return f"{sfq}.{name}", False
            cur = idx.types.get(cur.outer) if cur.outer else None
        if jf is not None:
            if head in jf.imports:
                cand = jf.imports[head] + (("." + rest) if rest else "")
                return (cand, False) if cand in idx.types else (cand, False)
            if jf.package and f"{jf.package}.{name}" in idx.types:
                return f"{jf.package}.{name}", False
            for w in jf.wildcards:
                if f"{w}.{name}" in idx.types:
                    return f"{w}.{name}", False
        if name in ("String", "Object", "Integer", "Long", "Boolean", "List", "Map", "Set", "Optional",
                    "Collection", "Iterable", "Stream", "Exception", "RuntimeException"):
            return None
        simple = name.split(".")[-1]
        cands = idx.by_simple.get(simple, [])
        if len(cands) == 1:
            return cands[0], True
        return None

    def supertypes(self, fqn: str) -> list[str]:
        """All resolved supertypes (BFS, nearest first)."""
        out, queue, seen = [], [fqn], {fqn}
        while queue:
            cur = self.idx.types.get(queue.pop(0))
            if cur is None:
                continue
            for s in cur.extends + cur.implements:
                r = self.resolve_type(s, cur)
                if r and r[0] in self.idx.types and r[0] not in seen:
                    seen.add(r[0])
                    out.append(r[0])
                    queue.append(r[0])
        return out

    def is_mock_receiver(self, recv: tuple | None, jm: "JMethod") -> bool:
        """True when a call's receiver is a Mockito/Spring test double (calls on it never reach production code)."""
        if not recv:
            return False
        if recv[0] == "name":
            fname = recv[1]
        elif recv[0] == "field" and recv[1] and recv[1][0] == "this":
            fname = recv[2]
        else:
            return False
        cur = self.idx.types.get(jm.cls)
        while cur is not None:
            for t in [cur.fqn] + self.supertypes(cur.fqn):
                jt = self.idx.types.get(t)
                if jt and fname in jt.fields:
                    return fname in jt.mock_fields
            cur = self.idx.types.get(cur.outer) if cur.outer else None
        return False

    def field_type(self, fqn: str, fname: str) -> tuple[str, JType] | None:
        for t in [fqn] + self.supertypes(fqn):
            jt = self.idx.types.get(t)
            if jt and fname in jt.fields:
                return jt.fields[fname], jt
        return None

    def find_methods(self, fqn: str, name: str, argc: int | None) -> list[JMethod]:
        for t in [fqn] + self.supertypes(fqn):
            jt = self.idx.types.get(t)
            if not jt:
                continue
            ms = [m for m in jt.methods if m.name == name and m.kind != EntityType.CONSTRUCTOR.value and
                  (argc is None or len(m.params) == argc or (m.varargs and argc >= len(m.params) - 1))]
            if ms:
                return ms
        return []

    # ------------------------------------------------------------------ Phase B: bodies
    def walk_bodies(self) -> None:
        for jm in list(self.idx.methods.values()):
            if jm.body is None:
                continue
            jt = self.idx.types[jm.cls]
            jf = self.idx.files[jm.file]
            local_types = {n: t for t, n in jm.params}
            local_str: dict[str, str] = {}
            try:
                self._walk(jm.body, jm, jt, jf, local_types, local_str)
            except RecursionError:
                self.gb.diag("WARNING", "DEPTH_LIMIT", f"method body too deeply nested: {jm.name}", jm.file, jm.line)
            jm.body = None   # free tree memory

    def _expr(self, node: Node | None, src: bytes, local_types: dict) -> tuple | None:
        if node is None:
            return ("implicit",)
        tp = node.type
        if tp == "identifier":
            n = _t(node, src)
            return ("type", local_types[n]) if n in local_types and local_types[n] != "var" else ("name", n)
        if tp == "this":
            return ("this",)
        if tp == "super":
            return ("super",)
        if tp == "field_access":
            return ("field", self._expr(node.child_by_field_name("object"), src, local_types),
                    _t(node.child_by_field_name("field"), src))
        if tp == "method_invocation":
            args = node.child_by_field_name("arguments")
            return ("call", self._expr(node.child_by_field_name("object"), src, local_types),
                    _t(node.child_by_field_name("name"), src), len(args.named_children) if args else 0)
        if tp == "object_creation_expression":
            return ("type", _t(node.child_by_field_name("type"), src))
        if tp == "parenthesized_expression" and node.named_children:
            return self._expr(node.named_children[0], src, local_types)
        if tp == "cast_expression":
            return ("type", _t(node.child_by_field_name("type"), src))
        if tp in ("scoped_identifier", "type_identifier"):
            return ("type", _t(node, src))
        return None

    def _walk(self, node: Node, jm: JMethod, jt: JType, jf: JFile, lt: dict, ls: dict) -> None:
        src = jf.src
        stack = [node]
        while stack:
            n = stack.pop()
            tp = n.type
            if tp == "local_variable_declaration":
                vtype = _t(n.child_by_field_name("type"), src)
                for d in n.named_children:
                    if d.type == "variable_declarator":
                        vn = _t(d.child_by_field_name("name"), src)
                        val = d.child_by_field_name("value")
                        t = vtype
                        if vtype == "var" and val is not None and val.type == "object_creation_expression":
                            t = _t(val.child_by_field_name("type"), src)
                        lt[vn] = t
                        if val is not None and _base_type(vtype) in ("String", "var"):
                            s, _ = _static_string(val, src, _StrCtx(self, jt, ls))
                            if s is not None:
                                ls[vn] = s
            elif tp == "enhanced_for_statement":
                vn = _t(n.child_by_field_name("name"), src)
                if vn:
                    lt[vn] = _t(n.child_by_field_name("type"), src)
            elif tp in ("catch_formal_parameter",):
                pass
            elif tp == "lambda_expression":
                params = n.child_by_field_name("parameters")
                if params is not None:
                    for p in ([params] if params.type == "identifier" else params.named_children):
                        pn = _t(p.child_by_field_name("name") if p.type == "formal_parameter" else p, src)
                        lt.pop(pn, None)
                        if p.type == "formal_parameter":
                            lt[pn] = _t(p.child_by_field_name("type"), src)
            elif tp == "method_invocation":
                self._on_invocation(n, jm, jt, jf, lt, ls)
            elif tp == "object_creation_expression":
                tname = _t(n.child_by_field_name("type"), src)
                self.idx.news.append((jm, tname, n.start_point[0] + 1))
                if _base_type(tname) in ("ModelAndView", "RedirectView"):
                    args = n.child_by_field_name("arguments")
                    if args is not None and args.named_children:
                        v, dyn = _static_string(args.named_children[0], src, _StrCtx(self, jt, ls))
                        if v:
                            pre = "redirect:" if _base_type(tname) == "RedirectView" and not v.startswith("redirect:") else ""
                            self.idx.returns.setdefault(jm.id, []).append((pre + v, dyn))
            elif tp == "return_statement" and n.named_children:
                v, dyn = _static_string(n.named_children[0], src, _StrCtx(self, jt, ls))
                if v:
                    self.idx.returns.setdefault(jm.id, []).append((v, dyn))
            elif tp == "method_reference":
                kids = n.named_children
                if kids:
                    member = _t(kids[-1], src) if len(kids) > 1 else ""
                    if member and member != "new":
                        recv = self._expr(kids[0], src, lt)
                        self.idx.calls.append(PendingCall(jm, recv, member, None, n.start_point[0] + 1,
                                                          _t(n, src)[:120]))
            stack.extend(reversed(n.named_children))

    def _on_invocation(self, n: Node, jm: JMethod, jt: JType, jf: JFile, lt: dict, ls: dict) -> None:
        src = jf.src
        name = _t(n.child_by_field_name("name"), src)
        obj = n.child_by_field_name("object")
        args = n.child_by_field_name("arguments")
        argl = list(args.named_children) if args else []
        line = n.start_point[0] + 1
        recv = self._expr(obj, src, lt) if obj is not None else ("implicit",)
        evidence = re.sub(r"\s+", " ", _t(n, src))[:160]
        self.idx.calls.append(PendingCall(jm, recv, name, len(argl), line, evidence))
        sc = _StrCtx(self, jt, ls)
        # ---- SQL in arguments
        strings = [(_static_string(a, src, sc), a) for a in argl[:3]]
        for (s, dyn), a in strings:
            if s is None:
                continue
            if name in JAVA_SQL_EXEC and looks_like_sql(s):
                self.idx.sql.append(dict(executor=jm.id, sql=s, file=jf.path, line=line, kind="JAVA_SQL_CALL",
                                         status=Status.VERIFIED, dynamic=dyn, jpql=name in ("createQuery",),
                                         evidence=evidence))
                break
            if looks_like_sql_strict(s):
                self.idx.sql.append(dict(executor=jm.id, sql=s, file=jf.path, line=line, kind="JAVA_SQL_STRING",
                                         status=Status.CANDIDATE, dynamic=dyn, jpql=False, evidence=evidence,
                                         reason=f"SQL-shaped string passed to {name}() (execution API not recognised)"))
                break
        # ---- HTTP client / test client calls
        verb = None
        if name in REST_CLIENT_METHODS:
            verb = REST_CLIENT_METHODS[name]
        elif name in HTTP_VERB_CALLS:
            verb = HTTP_VERB_CALLS[name]
        elif name == "exchange" and len(argl) >= 2:
            verb = _t(argl[1], src).split(".")[-1].upper()
            verb = verb if verb in ("GET", "POST", "PUT", "DELETE", "PATCH") else None
        elif name == "uri" and recv and recv[0] == "call" and recv[2] in HTTP_VERB_CALLS:
            verb = HTTP_VERB_CALLS[recv[2]]
        if verb and argl:
            s, dyn = _static_string(argl[0], src, sc)
            if s and (s.startswith("/") or s.startswith("http")):
                self.ctx.http_calls.append(HttpCall(jm.id, verb, s, jf.path, line, "java",
                                                    jm.kind == EntityType.TEST.value, dyn,
                                                    "JAVA_HTTP_CLIENT" if jm.kind != EntityType.TEST.value else "JAVA_TEST_CLIENT",
                                                    evidence))

    # ------------------------------------------------------------------ Phase C: resolve
    def _eval(self, expr: tuple | None, jm: JMethod, depth: int = 0) -> tuple[str | None, bool]:
        """Evaluate a receiver expression to a class FQN. Returns (fqn|None, candidate)."""
        if expr is None or depth > 6:
            return None, False
        jt = self.idx.types.get(jm.cls)
        k = expr[0]
        if k in ("this", "implicit"):
            return jm.cls, False
        if k == "super":
            for s in jt.extends:
                r = self.resolve_type(s, jt)
                if r and r[0] in self.idx.types:
                    return r
            return None, False
        if k == "type":
            r = self.resolve_type(expr[1], jt)
            return r if r else (None, False)
        if k == "name":
            name = expr[1]
            cur = jt
            while cur is not None:
                ft = self.field_type(cur.fqn, name)
                if ft:
                    r = self.resolve_type(ft[0], ft[1])
                    return r if r else (None, False)
                cur = self.idx.types.get(cur.outer) if cur.outer else None
            r = self.resolve_type(name, jt)
            return r if r else (None, False)
        if k == "field":
            base, cand = self._eval(expr[1], jm, depth + 1)
            if base and base in self.idx.types:
                ft = self.field_type(base, expr[2])
                if ft:
                    r = self.resolve_type(ft[0], ft[1])
                    return (r[0], cand or r[1]) if r else (None, False)
            if expr[1] and expr[1][0] == "name":   # maybe a qualified type name a.b.C
                r = self.resolve_type(f"{expr[1][1]}.{expr[2]}", jt)
                return r if r else (None, False)
            return None, False
        if k == "call":
            base, cand = self._eval(expr[1], jm, depth + 1)
            if base and base in self.idx.types:
                ms = self.find_methods(base, expr[2], expr[3])
                if ms and ms[0].ret:
                    owner = self.idx.types.get(ms[0].cls)
                    r = self.resolve_type(ms[0].ret, owner)
                    return (r[0], cand or r[1] or len(ms) > 1) if r else (None, False)
            return None, False
        return None, False

    def _spring_data_entity(self, fqn: str) -> tuple[str | None, str | None]:
        """If fqn is a Spring Data repository interface, return (entity fqn, table id)."""
        for t in [fqn] + self.supertypes(fqn):
            jt = self.idx.types.get(t)
            if not jt:
                continue
            for base, args in jt.super_generics.items():
                if base.split(".")[-1] in SPRING_DATA_BASES and args:
                    r = self.resolve_type(args[0], jt)
                    if r and r[0] in self.idx.types:
                        ent = self.idx.types[r[0]]
                        tid = self._entity_table.get(r[0])
                        if not tid:   # entity without JPA/Mongo annotation in source: implied by naming convention
                            tid = ensure_table(self.gb, snake_case(ent.simple), source=ent.id, kind="implied")
                            self._entity_table[r[0]] = tid
                            self.gb.rel(ent.id, tid, RelType.MAPS_TO, kind="SPRING_DATA_CONVENTION",
                                        evidence=f"{jt.simple} extends {base}<{args[0]}, ...>", file=jt.file,
                                        line=jt.line, status=Status.CANDIDATE,
                                        reason="repository entity type; store name derived by default naming convention")
                        return r[0], tid
                    return None, None
        return None, None

    def resolve(self) -> None:
        gb, idx = self.gb, self.idx
        self._entity_table: dict[str, str] = {}
        entity_names: dict[str, str] = {}
        # ---- JPA entities -> tables/columns
        for jt in idx.types.values():
            if not ({"Entity", "Table", "Document"} & set(jt.annotations)):
                continue
            jf = idx.files[jt.file]
            sc = _StrCtx(self, jt)
            tname = None
            if "Table" in jt.annotations:
                tname = (_ann_values(jt.annotations["Table"], jf.src, sc).get("name") or [None])[0]
            ename = jt.simple
            if "Entity" in jt.annotations:
                ename = (_ann_values(jt.annotations["Entity"], jf.src, sc).get("name") or [jt.simple])[0]
            if "Document" in jt.annotations:
                tname = tname or (_ann_values(jt.annotations["Document"], jf.src, sc).get("collection") or
                                  _ann_values(jt.annotations["Document"], jf.src, sc).get("value") or [None])[0]
            table = tname if isinstance(tname, str) and tname else snake_case(ename)
            tid = ensure_table(gb, table, source=jt.id)
            self._entity_table[jt.fqn] = tid
            entity_names[ename] = table
            entity_names[jt.simple] = table
            gb.rel(jt.id, tid, RelType.MAPS_TO, kind="JPA_MAPPING", evidence=f"@Entity {jt.simple} -> {table}",
                   file=jt.file, line=jt.line, reason="@Entity/@Table mapping (Spring physical naming if implicit)")
        # columns (including inherited @MappedSuperclass fields)
        for fqn, tid in self._entity_table.items():
            jt = idx.types[fqn]
            chain = [jt] + [idx.types[s] for s in self.supertypes(fqn) if s in idx.types and
                            "MappedSuperclass" in idx.types[s].annotations]
            for owner in chain:
                jf = idx.files[owner.file]
                sc = _StrCtx(self, owner)
                for fname, ftype, anns, fline, static in owner.field_nodes:
                    if static or "Transient" in anns or {"OneToMany", "ManyToMany"} & set(anns):
                        continue
                    col = None
                    if "Column" in anns:
                        col = (_ann_values(anns["Column"], jf.src, sc).get("name") or [None])[0]
                    if "JoinColumn" in anns:
                        col = (_ann_values(anns["JoinColumn"], jf.src, sc).get("name") or [None])[0]
                    is_rel = bool({"ManyToOne", "OneToOne"} & set(anns))
                    if not isinstance(col, str) or not col:
                        col = snake_case(fname) + ("_id" if is_rel else "")
                    ensure_column(gb, tid.split(":", 1)[1], {"name": col, "type": _base_type(ftype),
                                                             "pk": "Id" in anns, "nullable": True},
                                  file=owner.file, line=fline, kind="JPA_MAPPING")
                    if is_rel:
                        r = self.resolve_type(ftype, owner)
                        if r and r[0] in self._entity_table:
                            gb.rel(tid, self._entity_table[r[0]], RelType.FOREIGN_KEY, kind="JPA_MAPPING",
                                   evidence=f"{fname}: {_base_type(ftype)}", file=owner.file, line=fline,
                                   reason="@ManyToOne/@OneToOne association", column=col)
        # ---- JPA associations: cascade graph + @OneToMany @JoinColumn foreign keys
        self._cascade: dict[str, list[tuple[str, str]]] = {}     # entity fqn -> [(target entity fqn, field)]
        for fqn, tid in self._entity_table.items():
            owner = idx.types[fqn]
            jf = idx.files[owner.file]
            sc = _StrCtx(self, owner)
            for fname, ftype, anns, fline, static in owner.field_nodes:
                assoc = {"OneToMany", "ManyToMany", "OneToOne", "ManyToOne"} & set(anns)
                if not assoc:
                    continue
                args = _generic_args(ftype)
                target_t = args[-1] if args else ftype
                r = self.resolve_type(target_t, owner)
                if not r or r[0] not in self._entity_table:
                    continue
                a = next(iter(assoc))
                text = _t(anns[a], jf.src)
                if re.search(r"cascade\s*=\s*[^)]*(ALL|PERSIST|MERGE)", text):
                    self._cascade.setdefault(fqn, []).append((r[0], fname))
                if a == "OneToMany" and "JoinColumn" in anns:
                    col = (_ann_values(anns["JoinColumn"], jf.src, sc).get("name") or [None])[0]
                    child_tid = self._entity_table[r[0]]
                    if isinstance(col, str) and col:
                        ensure_column(gb, child_tid.split(":", 1)[1], {"name": col, "type": "", "pk": False, "nullable": True},
                                      file=owner.file, line=fline, kind="JPA_MAPPING")
                    gb.rel(child_tid, tid, RelType.FOREIGN_KEY, kind="JPA_MAPPING", evidence=f"@OneToMany @JoinColumn({col}) {owner.simple}.{fname}",
                           file=owner.file, line=fline, reason="@OneToMany with @JoinColumn: child table holds the parent key", column=col)
        # ---- inheritance
        for jt in idx.types.values():
            for rel_t, names in ((RelType.EXTENDS, jt.extends), (RelType.IMPLEMENTS, jt.implements)):
                for s in names:
                    r = self.resolve_type(s, jt)
                    if r and r[0] in idx.types:
                        st = idx.types[r[0]]
                        rt = rel_t
                        if rel_t == RelType.IMPLEMENTS and st.kind != EntityType.INTERFACE.value:
                            rt = RelType.EXTENDS
                        if rel_t == RelType.EXTENDS and jt.kind == EntityType.CLASS.value and st.kind == EntityType.INTERFACE.value:
                            rt = RelType.IMPLEMENTS
                        gb.rel(jt.id, st.id, rt, kind="JAVA_AST", evidence=f"{jt.simple} {rt.value.lower()} {s}",
                               file=jt.file, line=jt.line, status=Status.CANDIDATE if r[1] else Status.VERIFIED,
                               reason="explicit supertype declaration" + (" (resolved by unique simple name)" if r[1] else ""))
                    else:
                        ent = gb.get(jt.id)
                        ent.props.setdefault("external_supertypes", []).append(s)
        # ---- overrides
        for jt in idx.types.values():
            sups = self.supertypes(jt.fqn)
            for m in jt.methods:
                if m.kind in (EntityType.CONSTRUCTOR.value, EntityType.TEST.value) or m.static:
                    continue
                for s in sups:
                    st = idx.types[s]
                    match = [sm for sm in st.methods if sm.name == m.name and len(sm.params) == len(m.params)
                             and sm.kind != EntityType.CONSTRUCTOR.value]
                    for sm in match:
                        gb.rel(m.id, sm.id, RelType.OVERRIDES, kind="JAVA_AST", evidence=f"{jt.simple}.{m.name} overrides {st.simple}.{sm.name}",
                               file=m.file, line=m.line, reason="same name and arity in resolved supertype")
                    if match:
                        break
        # ---- imports
        for jf in idx.files.values():
            fid = make_id("file", jf.path)
            for simple, fqn in jf.imports.items():
                if fqn in idx.types:
                    gb.rel(fid, idx.types[fqn].id, RelType.IMPORTS, kind="JAVA_IMPORT", evidence=f"import {fqn};",
                           file=jf.path, reason="import declaration resolved to repository type")
        # ---- Spring Data repositories
        repo_info: dict[str, str] = {}
        repo_entity: dict[str, str] = {}
        for jt in idx.types.values():
            if jt.kind != EntityType.INTERFACE.value:
                continue
            ent, tid = self._spring_data_entity(jt.fqn)
            if tid:
                repo_info[jt.fqn] = tid
                repo_entity[jt.fqn] = ent
                gb.get(jt.id).props["spring_data_repository"] = True
                gb.get(jt.id).props["entity"] = ent
                for m in jt.methods:
                    if "Query" in m.annotations:
                        continue
                    low = m.name.lower()
                    op = RelType.READS if low.startswith(READ_PREFIX) else (RelType.WRITES if low.startswith(WRITE_PREFIX) else None)
                    if op:
                        gb.rel(m.id, tid, op, kind="SPRING_DATA_DERIVED", evidence=f"{jt.simple}.{m.name}()",
                               file=m.file, line=m.line, reason="Spring Data derived query method on repository of this entity")
        entity_tables = {k: v for k, v in entity_names.items()}
        # ---- calls
        for pc in idx.calls:
            jm = pc.caller
            jt = idx.types.get(jm.cls)
            targets: list[JMethod] = []
            cand = False
            recv_fqn = None
            if pc.recv and pc.recv[0] == "implicit":
                cur = jt
                while cur is not None and not targets:
                    targets = self.find_methods(cur.fqn, pc.name, pc.argc)
                    recv_fqn = cur.fqn
                    cur = idx.types.get(cur.outer) if cur.outer else None
                if not targets:
                    jf = idx.files[jm.file]
                    cls = jf.static_imports.get(pc.name)
                    for c in ([cls] if cls else []) + jf.static_wild:
                        if c in idx.types:
                            targets = self.find_methods(c, pc.name, pc.argc)
                            if targets:
                                recv_fqn = c
                                break
            else:
                recv_fqn, cand = self._eval(pc.recv, jm)
                if recv_fqn and recv_fqn in idx.types:
                    targets = self.find_methods(recv_fqn, pc.name, pc.argc)
            if not targets:
                if recv_fqn in repo_info and jm.kind != EntityType.TEST.value:
                    low = pc.name.lower()
                    op = RelType.READS if low.startswith(READ_PREFIX) else (RelType.WRITES if low.startswith(WRITE_PREFIX) else None)
                    if op:
                        gb.rel(jm.id, repo_info[recv_fqn], op, kind="SPRING_DATA_INHERITED", evidence=pc.evidence,
                               file=jm.file, line=pc.line, status=Status.CANDIDATE if cand else Status.VERIFIED,
                               reason=f"inherited Spring Data CRUD method {pc.name}() on repository of this entity")
                        if op == RelType.WRITES:
                            for ctid, path in self.cascade_tables(repo_entity.get(recv_fqn)):
                                gb.rel(jm.id, ctid, RelType.WRITES, kind="JPA_CASCADE", evidence=pc.evidence, file=jm.file,
                                       line=pc.line, status=Status.CANDIDATE,
                                       reason=f"{pc.name}() cascades through JPA association {path} (cascade ALL/PERSIST/MERGE)")
                        self.ctx.stats["resolved_calls"] += 1
                        continue
                if recv_fqn in idx.types and pc.recv and pc.recv[0] == "implicit":
                    self.ctx.stats["unresolved_calls"] += 1     # most likely a statically imported library helper
                    continue
                if recv_fqn in idx.types:
                    rt = idx.types[recv_fqn]
                    if LOMBOK_ACCESSORS & set(rt.annotations) and re.match(r"^(get|set|is|with|builder|toBuilder)", pc.name):
                        continue
                    if rt.kind == EntityType.ENUM.value and pc.name in ("values", "valueOf", "name", "ordinal"):
                        continue
                    if rt.kind == EntityType.CLASS.value and "record" in (gb.get(rt.id).props if gb.get(rt.id) else {}) and pc.name in rt.fields:
                        continue
                    self.ctx.unresolved(jm.file, pc.line, f"{rt.simple}.{pc.name}() not found in repository type hierarchy")
                else:
                    self.ctx.stats["unresolved_calls"] += 1
                continue
            multi = len(targets) > 1
            status = Status.CANDIDATE if (cand or multi) else Status.VERIFIED
            reason = ("receiver type resolved; " if pc.recv and pc.recv[0] != "implicit" else "same-class/inherited call; ") + \
                     ("ambiguous overloads" if multi else "unique target") + (" (type resolved by unique simple name)" if cand else "")
            self.ctx.stats["resolved_calls"] += 1
            if status == Status.CANDIDATE:
                self.ctx.stats["candidate_calls"] += 1
            for tm in targets[:3]:
                if tm.id == jm.id and pc.recv and pc.recv[0] not in ("implicit", "this"):
                    pass
                if jm.kind == EntityType.TEST.value:
                    if self.is_mock_receiver(pc.recv, jm):
                        continue    # stubbing/verifying a mock (given/when/verify) does not exercise the real method
                    if not idx.files[tm.file].is_test:
                        gb.rel(jm.id, tm.id, RelType.TESTS, kind="JAVA_TEST_CALL", evidence=pc.evidence, file=jm.file,
                               line=pc.line, status=status, reason="test invokes " + reason)
                    continue
                if tm.kind == EntityType.TEST.value:
                    continue
                gb.rel(jm.id, tm.id, RelType.CALLS, kind="JAVA_CALL", evidence=pc.evidence, file=jm.file, line=pc.line,
                       status=status, reason=reason)
                if tm.id in idx.feign_routes:
                    for verb, path in idx.feign_routes[tm.id]:
                        self.ctx.http_calls.append(HttpCall(jm.id, verb, path, jm.file, pc.line, "java", False, False,
                                                            "FEIGN_CLIENT", pc.evidence))
        # ---- instantiations
        for jm, tname, line in idx.news:
            r = self.resolve_type(tname, idx.types.get(jm.cls))
            if r and r[0] in idx.types:
                gb.rel(jm.id, idx.types[r[0]].id, RelType.INSTANTIATES, kind="JAVA_NEW", evidence=f"new {tname}(...)",
                       file=jm.file, line=line, status=Status.CANDIDATE if r[1] else Status.VERIFIED,
                       reason="object creation expression")
        # ---- SQL
        for s in idx.sql:
            add_sql(gb, s["sql"], file=s["file"], line=s["line"], language="java", executor_id=s["executor"],
                    exec_kind=s["kind"], exec_evidence=s["evidence"], exec_status=s["status"],
                    exec_reason=s.get("reason", ""), dynamic=s["dynamic"],
                    entity_tables=entity_tables if s.get("jpql") else None)
        self.entity_tables = entity_tables
        # ---- Spring MVC navigation: view names and redirects returned by @Controller handlers
        for mid, vals in idx.returns.items():
            jm = idx.methods.get(mid)
            ent = gb.get(mid)
            if not jm or not ent:
                continue
            jt = idx.types[jm.cls]
            is_view_controller = "Controller" in jt.annotations and "RestController" not in jt.annotations \
                and "ResponseBody" not in jm.annotations
            views, redirects = [], []
            for v, dyn in vals:
                v = v.strip()
                if v.startswith(("redirect:", "forward:")):
                    path = v.split(":", 1)[1].replace("?", "{}")
                    redirects.append({"path": path, "dynamic": dyn})
                elif is_view_controller and re.fullmatch(r"[A-Za-z0-9_\-/.{}?]+", v) and len(v) < 120:
                    views.append(v.replace("?", "{}"))
            if views:
                ent.props["renders_views"] = sorted(set(views))[:10]
            if redirects:
                ent.props["redirects_to"] = redirects[:10]

    def cascade_tables(self, entity_fqn: str | None, depth: int = 4) -> list[tuple[str, str]]:
        """Tables written transitively when entity_fqn is saved (JPA cascade)."""
        out, seen = [], {entity_fqn}
        stack = [(entity_fqn, self.idx.types[entity_fqn].simple if entity_fqn in self.idx.types else "")] if entity_fqn else []
        while stack:
            cur, path = stack.pop()
            for tgt, field in getattr(self, "_cascade", {}).get(cur, []):
                if tgt in seen or len(seen) > depth + 1:
                    continue
                seen.add(tgt)
                p2 = f"{path}.{field}"
                out.append((self._entity_table[tgt], p2))
                stack.append((tgt, p2))
        return out

    def method_by_qualified(self, cls_fqn: str, name: str) -> list[JMethod]:
        jt = self.idx.types.get(cls_fqn)
        return [m for m in jt.methods if m.name == name] if jt else []
