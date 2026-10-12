"""Python analyzer built on the standard-library `ast` module.

Extracts modules, classes, functions/methods, imports, calls (resolved through imports, self/cls,
local variable types, constructor assignments and class attributes), Flask/FastAPI/Django/DRF
routes, raw SQL (DB-API, SQLAlchemy text(), pandas), ORM table access (SQLAlchemy, Flask-SQLAlchemy,
Django ORM), HTTP client calls and pytest/unittest tests. Code is parsed, never executed."""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from ..kg.model import Entity, EntityType, RelType, Status, make_id, snake_case
from .common import AnalysisContext, HttpCall, add_route
from .sql_analyzer import add_sql, ensure_column, ensure_table, looks_like_sql, looks_like_sql_strict

PY_SQL_EXEC = {"execute", "executemany", "executescript", "read_sql", "read_sql_query", "raw", "mogrify",
               "exec_driver_sql", "fetch", "fetchrow", "fetchval", "fetch_all", "fetch_one", "execute_many",
               "copy_expert", "run_sql", "sql"}
HTTP_VERBS_PY = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH",
                 "head": "HEAD", "options": "OPTIONS"}
ROUTER_FACTORIES = {"Flask", "Blueprint", "FastAPI", "APIRouter", "Sanic", "APIBlueprint", "Quart", "Starlette"}
DJANGO_READ = {"filter", "get", "all", "exclude", "values", "values_list", "count", "exists", "first", "last",
               "aggregate", "annotate", "order_by", "select_related", "prefetch_related", "get_queryset", "only",
               "defer", "distinct", "in_bulk", "iterator", "latest", "earliest", "raw", "none"}
DJANGO_WRITE = {"create", "update", "delete", "bulk_create", "bulk_update", "get_or_create", "update_or_create"}
SQLA_READ_FUNCS = {"query", "select", "get", "scalars", "get_or_404", "first_or_404", "one_or_404"}
SQLA_WRITE_FUNCS = {"insert", "update", "delete"}
SESSION_WRITE = {"add", "add_all", "delete", "merge", "bulk_save_objects", "bulk_insert_mappings"}
import builtins as _builtins
COMMON_METHOD_NAMES = set("""get set save delete update create filter all first last add remove append extend items keys
values pop run start stop close open read write send execute commit rollback query validate process handle load dump
dumps loads format join split strip replace copy clear count index sort to_dict dict json init setup teardown refresh
flush merge exists find fetch list post put patch head options call apply build make parse render serialize
deserialize clean is_valid get_object get_queryset perform_create perform_update to_representation emit log info
debug warning error exception connect disconnect reset next iter encode decode lower upper scalar scalars one
one_or_none fetchone fetchall fetchmany cursor""".split())
BUILTINS = set(dir(_builtins))


def module_name_for(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    parts = p.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _expr(node: ast.AST | None) -> tuple | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return ("name", node.id)
    if isinstance(node, ast.Attribute):
        return ("attr", _expr(node.value), node.attr)
    if isinstance(node, ast.Call):
        return ("call", _expr(node.func))
    if isinstance(node, ast.Subscript):
        return _expr(node.value)
    if isinstance(node, ast.Await):
        return _expr(node.value)
    return None


def _dotted(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        d = _dotted(node.value)
        return f"{d}.{node.attr}" if d else None
    if isinstance(node, ast.Subscript):   # Optional[User] -> User ; List[User] -> User
        inner = node.slice
        if isinstance(inner, ast.Tuple) and inner.elts:
            inner = inner.elts[0]
        return _dotted(inner)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip("'\"")
    return None


@dataclass
class PyFunc:
    id: str
    name: str
    qual: str
    module: str
    cls: str | None
    file: str
    line: int
    node: ast.AST
    kind: str
    decorators: list = field(default_factory=list)


@dataclass
class PyClass:
    id: str
    name: str
    qual: str
    module: str
    file: str
    line: int
    bases: list = field(default_factory=list)       # dotted base names
    methods: dict = field(default_factory=dict)
    attr_types: dict = field(default_factory=dict)  # attribute -> dotted type
    table: str | None = None
    columns: list = field(default_factory=list)     # (name, type, line, fk_target)
    orm: str | None = None
    is_test_class: bool = False
    raw_table: str | None = None
    model_hint: bool = False
    django: bool = False
    abstract: bool = False
    sqlmodel: bool = False
    table_kw: bool = False


@dataclass
class PyModule:
    name: str
    file: str
    is_test: bool
    imports: dict = field(default_factory=dict)     # alias -> (target dotted, level, is_from, symbol)
    functions: dict = field(default_factory=dict)
    classes: dict = field(default_factory=dict)
    consts: dict = field(default_factory=dict)
    var_types: dict = field(default_factory=dict)   # module-level var -> ("Factory", call node) or dotted type
    routers: dict = field(default_factory=dict)     # var -> {"kind":..,"prefix":..}


class PythonAnalyzer:
    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.gb = ctx.gb
        self.modules: dict[str, PyModule] = {}
        self.funcs: dict[str, PyFunc] = {}
        self.classes: dict[str, PyClass] = {}
        self.pending_routes: list[dict] = []
        self.registrations: list[dict] = []         # register_blueprint / include_router
        self.django_urls: list[dict] = []
        self.drf_routes: list[dict] = []
        self.calls: list[dict] = []
        self.orm_ops: list[dict] = []
        self.sql: list[dict] = []
        self.http: list[dict] = []
        self.local_types: dict[str, dict] = {}
        self.views: dict[str, list] = {}          # function id -> rendered templates
        self.redirects: dict[str, list] = {}      # function id -> [("path"|"endpoint", value)]
        self.route_names: dict[str, set] = {}     # route name / endpoint name -> api ids
        self.named_http: list = []                # (func, verb, route name, line, evidence) e.g. url_path_for("users:login")
        self._mod_cache: dict = {}

    # ================================================================ Phase A: declare
    def declare_file(self, path: str, text: str, is_test: bool) -> None:
        try:
            try:
                tree = ast.parse(text, filename=path)
            except SyntaxError as first:
                # PEP 758 (Python 3.14): `except A, B:` without parentheses -> rewrite and retry
                if "parenthesized" not in str(first.msg):
                    raise
                fixed = re.sub(r"^(\s*except\*?\s+)([^():\n]+,[^():\n]+?)(\s+as\s+\w+)?\s*:", r"\1(\2)\3:", text, flags=re.M)
                tree = ast.parse(fixed, filename=path)
                self.gb.diag("INFO", "SYNTAX_COMPAT", "parsed after Python 3.14 except-clause compatibility rewrite", path, first.lineno)
        except (SyntaxError, ValueError) as exc:
            self.gb.diag("ERROR", "PARSE_ERROR", f"Python syntax error (Python 2 or invalid code?): {exc.msg if hasattr(exc, 'msg') else exc}",
                         path, getattr(exc, "lineno", None))
            ent = self.gb.get(make_id("file", path))
            if ent:
                ent.props["parse_status"] = "FAILED"
            return
        mname = module_name_for(path)
        mod = PyModule(name=mname, file=path, is_test=is_test)
        self.modules[mname] = mod
        fid = make_id("file", path)
        self.gb.get(fid).props["module"] = mname
        for node in tree.body:
            self._declare_stmt(node, mod, None)
        # module-level calls (registrations, django urlpatterns, DRF routers)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                self._module_call(node, mod)

    def _declare_stmt(self, node: ast.AST, mod: PyModule, cls: PyClass | None) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and cls is None:
            for a in node.names:
                if isinstance(node, ast.Import):
                    alias = a.asname or a.name.split(".")[0]
                    target = a.name if a.asname else a.name.split(".")[0]
                    mod.imports[alias] = (target, 0, False, None, node.lineno, a.name)
                else:
                    if a.name == "*":
                        mod.imports.setdefault("*", [])
                        continue
                    mod.imports[a.asname or a.name] = (node.module or "", node.level, True, a.name, node.lineno, None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._declare_func(node, mod, cls)
        elif isinstance(node, ast.ClassDef):
            self._declare_class(node, mod)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and cls is None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            val = node.value
            for t in targets:
                if not isinstance(t, ast.Name) or val is None:
                    continue
                s, dyn = self.static_str(val, {}, mod)
                if s is not None and not dyn:
                    mod.consts[t.id] = s
                if isinstance(val, ast.Call):
                    fname = _dotted(val.func) or ""
                    base = fname.split(".")[-1]
                    if base in ROUTER_FACTORIES:
                        prefix = ""
                        for kw in val.keywords:
                            if kw.arg in ("url_prefix", "prefix"):
                                prefix = self.static_str(kw.value, {}, mod)[0] or ""
                        mod.routers[t.id] = {"kind": base, "prefix": prefix, "extra": []}
                    mod.var_types[t.id] = fname
                if isinstance(node, ast.AnnAssign) and node.annotation is not None:
                    mod.var_types.setdefault(t.id, _dotted(node.annotation) or "")
        elif isinstance(node, (ast.If, ast.Try)) and cls is None:
            for sub in getattr(node, "body", []) + getattr(node, "orelse", []) + getattr(node, "finalbody", []):
                self._declare_stmt(sub, mod, None)
            for h in getattr(node, "handlers", []):
                for sub in h.body:
                    self._declare_stmt(sub, mod, None)

    def _declare_class(self, node: ast.ClassDef, mod: PyModule, outer: str | None = None) -> None:
        qual = f"{outer or mod.name}.{node.name}"
        cid = make_id("class", qual)
        bases = [b for b in (_dotted(x) for x in node.bases) if b]
        pc = PyClass(id=cid, name=node.name, qual=qual, module=mod.name, file=mod.file, line=node.lineno, bases=bases)
        pc.is_test_class = mod.is_test and (node.name.startswith("Test") or any(b.endswith("TestCase") for b in bases))
        self.classes[qual] = pc
        mod.classes[node.name] = pc
        self.gb.add_entity(Entity(id=cid, type=EntityType.CLASS.value, name=node.name, qualified_name=qual,
                                  file=mod.file, line=node.lineno, end_line=getattr(node, "end_lineno", None),
                                  language="python",
                                  props={"bases": bases, "decorators": [_dotted(d if not isinstance(d, ast.Call) else d.func) for d in node.decorator_list],
                                         "is_test_code": mod.is_test, "docstring": (ast.get_docstring(node) or "")[:300]}))
        self.gb.rel(make_id("file", mod.file) if outer is None else make_id("class", outer), cid, RelType.DEFINES,
                    kind="PYTHON_AST", evidence=f"class {node.name}", file=mod.file, line=node.lineno, reason="class declaration")
        is_model = any(b.split(".")[-1] in ("Model", "Base", "DeclarativeBase", "SQLModel", "Document") for b in bases)
        tablename = None
        for st in node.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._declare_func(st, mod, pc)
            elif isinstance(st, ast.ClassDef):
                if st.name == "Meta":
                    for m in st.body:
                        if isinstance(m, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "db_table" for t in m.targets):
                            tablename = self.static_str(m.value, {}, mod)[0]
                        if isinstance(m, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "abstract" for t in m.targets):
                            if isinstance(m.value, ast.Constant) and m.value.value is True:
                                pc.orm = "abstract"
                else:
                    self._declare_class(st, mod, outer=qual)
            elif isinstance(st, (ast.Assign, ast.AnnAssign)):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                for t in targets:
                    if not isinstance(t, ast.Name):
                        continue
                    if t.id == "__tablename__" and st.value is not None:
                        tablename = self.static_str(st.value, {}, mod)[0]
                        continue
                    val = st.value
                    if isinstance(val, ast.Call):
                        fn = (_dotted(val.func) or "").split(".")[-1]
                        if fn in ("Column", "mapped_column") or fn.endswith("Field") or fn in ("ForeignKey", "OneToOneField"):
                            ctype = ""
                            fk = None
                            for a in val.args:
                                d = _dotted(a.func if isinstance(a, ast.Call) else a)
                                if isinstance(a, ast.Call) and (d or "").split(".")[-1] == "ForeignKey" and a.args:
                                    fk = self.static_str(a.args[0], {}, mod)[0] or _dotted(a.args[0])
                                elif d and not ctype:
                                    ctype = d.split(".")[-1]
                            if fn in ("ForeignKey", "OneToOneField") and val.args:
                                fk = "model:" + (_dotted(val.args[0]) or self.static_str(val.args[0], {}, mod)[0] or "")
                            colname = t.id
                            for kw in val.keywords:
                                if kw.arg == "foreign_key":
                                    fk = self.static_str(kw.value, {}, mod)[0] or fk
                                if kw.arg in ("name", "db_column"):
                                    colname = self.static_str(kw.value, {}, mod)[0] or colname
                            if fn in ("ForeignKey", "OneToOneField") and not any(kw.arg == "db_column" for kw in val.keywords):
                                colname = f"{t.id}_id"
                            if fn in ("ManyToManyField",):
                                continue
                            pc.columns.append((colname, ctype or fn, st.lineno, fk,
                                               any(kw.arg == "primary_key" and isinstance(kw.value, ast.Constant) and kw.value.value for kw in val.keywords)))
                    elif isinstance(st, ast.AnnAssign) and is_model and st.annotation is not None and (_dotted(st.annotation) or "").startswith(("Mapped", "Optional", "int", "str")):
                        pass
        pc.raw_table = tablename
        pc.sqlmodel = any(b.split(".")[-1] == "SQLModel" for b in bases)
        pc.table_kw = any(kw.arg == "table" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                          for kw in node.keywords)
        pc.model_hint = is_model or bool(tablename)
        pc.django = any(b.endswith("models.Model") or b == "Model" and "django" in str(mod.imports) for b in bases)
        if pc.orm == "abstract":
            pc.abstract = True

    def _declare_func(self, node, mod: PyModule, cls: PyClass | None) -> None:
        owner = cls.qual if cls else mod.name
        qual = f"{owner}.{node.name}"
        is_test = (mod.is_test and node.name.startswith("test") and (cls is None or cls.is_test_class or cls.name.startswith("Test")))
        if is_test:
            kind, fid = EntityType.TEST.value, make_id("test", qual)
        else:
            kind, fid = (EntityType.METHOD.value if cls else EntityType.FUNCTION.value), make_id("function", qual)
        decos = [_dotted(d.func if isinstance(d, ast.Call) else d) or "" for d in node.decorator_list]
        pf = PyFunc(id=fid, name=node.name, qual=qual, module=mod.name, cls=cls.qual if cls else None,
                    file=mod.file, line=node.lineno, node=node, kind=kind, decorators=decos)
        self.funcs[qual] = pf
        if cls:
            cls.methods[node.name] = pf
        else:
            mod.functions[node.name] = pf
        args = [a.arg for a in node.args.args]
        self.gb.add_entity(Entity(id=fid, type=kind, name=node.name, qualified_name=qual, file=mod.file,
                                  line=node.lineno, end_line=getattr(node, "end_lineno", None), language="python",
                                  props={"class": cls.qual if cls else None, "parameters": args, "decorators": decos,
                                         "async": isinstance(node, ast.AsyncFunctionDef), "is_test_code": mod.is_test,
                                         "signature": f"def {node.name}({', '.join(args)})",
                                         "docstring": (ast.get_docstring(node) or "")[:300]}))
        self.gb.rel(cls.id if cls else make_id("file", mod.file), fid, RelType.DEFINES, kind="PYTHON_AST",
                    evidence=f"def {node.name}", file=mod.file, line=node.lineno, reason="function/method declaration")
        # attribute types assigned in __init__ / any method: self.x = Cls(...) or self.x = param (annotated)
        if cls:
            ann = {a.arg: _dotted(a.annotation) for a in node.args.args + node.args.kwonlyargs if a.annotation is not None}
            for st in ast.walk(node):
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                    for t in targets:
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                            v = st.value
                            if isinstance(st, ast.AnnAssign) and st.annotation is not None:
                                cls.attr_types.setdefault(t.attr, _dotted(st.annotation))
                            elif isinstance(v, ast.Call) and _dotted(v.func):
                                cls.attr_types.setdefault(t.attr, _dotted(v.func))
                            elif isinstance(v, ast.Name) and v.id in ann and ann[v.id]:
                                cls.attr_types.setdefault(t.attr, ann[v.id])
        # routes from decorators
        for d in node.decorator_list:
            if not isinstance(d, ast.Call) or not isinstance(d.func, ast.Attribute):
                continue
            obj = _dotted(d.func.value)
            attr = d.func.attr
            if not obj:
                continue
            verbs = None
            if attr == "route" or attr == "api_route" or attr == "websocket":
                verbs = ["GET"] if attr == "route" else ["ANY"]
                for kw in d.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
                        verbs = [self.static_str(e, {}, mod)[0] or "ANY" for e in kw.value.elts]
            elif attr in HTTP_VERBS_PY:
                verbs = [HTTP_VERBS_PY[attr]]
            if verbs is None:
                continue
            path = None
            if d.args:
                path = self.static_str(d.args[0], {}, mod)[0]
            for kw in d.keywords:
                if kw.arg in ("path", "rule"):
                    path = self.static_str(kw.value, {}, mod)[0]
            if path is None:
                path = "" if attr != "route" else None
            if path is None:
                continue
            rname = next((self.static_str(kw.value, {}, mod)[0] for kw in d.keywords if kw.arg in ("name", "endpoint")), None)
            self.pending_routes.append(dict(func=pf, obj=obj, verbs=[v.upper() for v in verbs], path=path,
                                            line=d.lineno, module=mod.name, route_name=rname,
                                            evidence=f"@{obj}.{attr}({path!r}) on {node.name}"))

    def _module_call(self, call: ast.Call, mod: PyModule) -> None:
        fn = call.func
        # ---- Alembic migrations: op.create_table("users", sa.Column("id", sa.Integer, primary_key=True), ...)
        if isinstance(fn, ast.Attribute) and fn.attr in ("create_table", "add_column") and isinstance(fn.value, ast.Name) \
                and fn.value.id == "op" and call.args:
            tname = self.static_str(call.args[0], {}, mod)[0]
            if tname:
                cols, fks = [], []
                for a in call.args[1:]:
                    if not isinstance(a, ast.Call):
                        continue
                    cname = (_dotted(a.func) or "").split(".")[-1]
                    if cname == "Column" and a.args:
                        col = self.static_str(a.args[0], {}, mod)[0]
                        if not col:
                            continue
                        ctype, fk = "", None
                        for x in a.args[1:]:
                            d = _dotted(x.func if isinstance(x, ast.Call) else x) or ""
                            if d.split(".")[-1] == "ForeignKey" and isinstance(x, ast.Call) and x.args:
                                fk = self.static_str(x.args[0], {}, mod)[0]
                            elif d and not ctype:
                                ctype = d.split(".")[-1]
                        pk = any(kw.arg == "primary_key" and isinstance(kw.value, ast.Constant) and kw.value.value for kw in a.keywords)
                        nullable = not any(kw.arg == "nullable" and isinstance(kw.value, ast.Constant) and kw.value.value is False for kw in a.keywords)
                        cols.append({"name": col, "type": ctype, "pk": bool(pk), "nullable": nullable})
                        if fk:
                            fks.append((col, fk))
                    elif cname == "ForeignKeyConstraint" and len(a.args) >= 2 and isinstance(a.args[0], (ast.List, ast.Tuple)) \
                            and isinstance(a.args[1], (ast.List, ast.Tuple)):
                        for c, ref in zip(a.args[0].elts, a.args[1].elts):
                            cs, rs = self.static_str(c, {}, mod)[0], self.static_str(ref, {}, mod)[0]
                            if cs and rs:
                                fks.append((cs, rs))
                self.ctx.migrations.append({"table": tname, "columns": cols, "fks": fks, "file": mod.file,
                                            "line": call.lineno, "op": fn.attr})
        if isinstance(fn, ast.Attribute) and fn.attr in ("register_blueprint", "include_router", "mount") and call.args:
            prefix = ""
            for kw in call.keywords:
                if kw.arg in ("url_prefix", "prefix"):
                    prefix = self.static_str(kw.value, {}, mod)[0] or ""
            target = call.args[0]
            if fn.attr == "mount" and len(call.args) >= 2:
                prefix = self.static_str(call.args[0], {}, mod)[0] or ""
                target = call.args[1]
            self.registrations.append(dict(module=mod.name, parent=_dotted(fn.value), target=_dotted(target), prefix=prefix))
        name = (_dotted(fn) or "").split(".")[-1]
        if name in ("path", "re_path", "url") and len(call.args) >= 2 and mod.file.endswith("urls.py"):
            route = self.static_str(call.args[0], {}, mod)[0]
            if route is None:
                return
            view = call.args[1]
            if isinstance(view, ast.Call) and (_dotted(view.func) or "").endswith("include") and view.args:
                inc = view.args[0]
                if isinstance(inc, ast.Tuple) and inc.elts:
                    inc = inc.elts[0]
                inc_s = self.static_str(inc, {}, mod)[0]
                self.django_urls.append(dict(module=mod.name, route=route, include=inc_s or _dotted(inc), view=None,
                                             line=call.lineno, regex=name != "path"))
            else:
                v = view.func.value if (isinstance(view, ast.Call) and isinstance(view.func, ast.Attribute) and view.func.attr == "as_view") else view
                self.django_urls.append(dict(module=mod.name, route=route, include=None, view=_dotted(v),
                                             line=call.lineno, regex=name != "path",
                                             name=next((self.static_str(kw.value, {}, mod)[0] for kw in call.keywords if kw.arg == "name"), None)))
        if isinstance(fn, ast.Attribute) and fn.attr == "register" and len(call.args) >= 2:
            prefix = self.static_str(call.args[0], {}, mod)[0]
            if prefix is not None:
                self.drf_routes.append(dict(module=mod.name, router=_dotted(fn.value), prefix=prefix,
                                            view=_dotted(call.args[1]), line=call.lineno))

    # ================================================================ strings
    def static_str(self, node: ast.AST | None, local: dict, mod: PyModule | None) -> tuple[str | None, bool]:
        if node is None:
            return None, False
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, False
        if isinstance(node, ast.JoinedStr):
            parts, dyn = [], False
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                else:
                    inner = self.static_str(v.value, local, mod)[0] if isinstance(v, ast.FormattedValue) else None
                    if inner is not None:
                        parts.append(inner)
                    else:
                        parts.append("?")
                        dyn = True
            return "".join(parts), dyn
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            l, dl = self.static_str(node.left, local, mod)
            r, dr = self.static_str(node.right, local, mod)
            if l is None and r is None:
                return None, True
            return (l if l is not None else "?") + (r if r is not None else "?"), dl or dr or l is None or r is None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            l, _ = self.static_str(node.left, local, mod)
            return (re.sub(r"%\(?\w*\)?[sdfr]", "?", l), True) if l is not None else (None, True)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            l, _ = self.static_str(node.func.value, local, mod)
            return (re.sub(r"\{[^}]*\}", "?", l), True) if l is not None else (None, True)
        if isinstance(node, ast.Call) and (_dotted(node.func) or "").split(".")[-1] in ("text", "SQL", "literal_column") and node.args:
            return self.static_str(node.args[0], local, mod)
        if isinstance(node, ast.Name):
            if node.id in local:
                return local[node.id], False
            if mod is not None and node.id in mod.consts:
                return mod.consts[node.id], False
            if mod is not None and node.id in mod.imports:
                tgt = self._import_target(mod, node.id)
                if isinstance(tgt, tuple) and tgt[0] == "const":
                    return tgt[1], False
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and mod is not None:
            m = self._resolve_module_alias(mod, node.value.id)
            if m and node.attr in m.consts:
                return m.consts[node.attr], False
        return None, False

    # ================================================================ Phase B: bodies
    def walk_bodies(self) -> None:
        for pf in list(self.funcs.values()):
            mod = self.modules[pf.module]
            local_types: dict[str, str] = {}
            local_str: dict[str, str] = {}
            node = pf.node
            for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                if a.annotation is not None:
                    t = _dotted(a.annotation)
                    if t:
                        local_types[a.arg] = t
            # pass 1: assignments (order-insensitive approximation)
            for st in ast.walk(node):
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and st is not node:
                    continue
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                    for t in targets:
                        if isinstance(t, ast.Name):
                            if isinstance(st, ast.AnnAssign) and st.annotation is not None:
                                local_types[t.id] = _dotted(st.annotation) or ""
                            elif isinstance(st.value, ast.Call) and _dotted(st.value.func):
                                local_types.setdefault(t.id, _dotted(st.value.func))
                            s, dyn = self.static_str(st.value, local_str, mod) if st.value is not None else (None, False)
                            if s is not None:
                                local_str[t.id] = s
                elif isinstance(st, ast.withitem) and isinstance(st.optional_vars, ast.Name) and isinstance(st.context_expr, ast.Call):
                    d = _dotted(st.context_expr.func)
                    if d:
                        local_types[st.optional_vars.id] = d
            self.local_types[pf.qual] = local_types
            for st in ast.walk(node):
                if isinstance(st, ast.Call):
                    self._on_call(st, pf, mod, local_types, local_str)

    def _on_call(self, call: ast.Call, pf: PyFunc, mod: PyModule, lt: dict, ls: dict) -> None:
        func = call.func
        line = call.lineno
        try:
            evidence = ast.unparse(call)[:160]
        except Exception:
            evidence = ""
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
        if name is None:
            return
        recv = _expr(func.value) if isinstance(func, ast.Attribute) else None
        self.calls.append(dict(func=pf, recv=recv, name=name, line=line, evidence=evidence, argc=len(call.args)))
        # ---- SQL
        if call.args:
            s, dyn = self.static_str(call.args[0], ls, mod)
            if s is not None:
                if name in PY_SQL_EXEC and looks_like_sql(s):
                    self.sql.append(dict(executor=pf.id, sql=s, file=pf.file, line=line, kind="PYTHON_SQL_CALL",
                                         status=Status.VERIFIED, dynamic=dyn, evidence=evidence))
                elif name not in ("text",) and looks_like_sql_strict(s):
                    self.sql.append(dict(executor=pf.id, sql=s, file=pf.file, line=line, kind="PYTHON_SQL_STRING",
                                         status=Status.CANDIDATE, dynamic=dyn, evidence=evidence,
                                         reason=f"SQL-shaped string passed to {name}() (execution API not recognised)"))
        # ---- ORM
        chain = recv
        root_attrs = []
        while chain and chain[0] in ("attr", "call"):
            if chain[0] == "attr":
                root_attrs.append(chain[2])
            chain = chain[1]
        root = chain[1] if chain and chain[0] == "name" else None
        if root is not None and recv is not None:
            if "objects" in root_attrs:
                self.orm_ops.append(dict(func=pf, model=root, op="WRITES" if name in DJANGO_WRITE else ("READS" if name in DJANGO_READ else "READS"),
                                         line=line, evidence=evidence, kind="DJANGO_ORM", via="name"))
            elif "query" in root_attrs and root_attrs[-1] == "query":
                self.orm_ops.append(dict(func=pf, model=root, op="WRITES" if name in ("delete", "update") else "READS",
                                         line=line, evidence=evidence, kind="FLASK_SQLALCHEMY", via="name"))
        if name in SQLA_READ_FUNCS | SQLA_WRITE_FUNCS and call.args:
            op = "WRITES" if name in SQLA_WRITE_FUNCS else "READS"
            for a in call.args[:3]:
                d = _dotted(a)
                if d and d[:1].isupper() or (d and "." in d and d.split(".")[-1][:1].isupper()):
                    self.orm_ops.append(dict(func=pf, model=d, op=op, line=line, evidence=evidence, kind="SQLALCHEMY_ORM",
                                             via="name", weak=name == "get"))
        if isinstance(func, ast.Attribute) and name in SESSION_WRITE and call.args and isinstance(call.args[0], ast.Name):
            v = call.args[0].id
            if v in lt:
                self.orm_ops.append(dict(func=pf, model=lt[v], op="WRITES", line=line, evidence=evidence, kind="ORM_SESSION", via="type"))
        if isinstance(func, ast.Attribute) and name in ("save", "delete") and isinstance(func.value, ast.Name) and func.value.id in lt:
            self.orm_ops.append(dict(func=pf, model=lt[func.value.id], op="WRITES", line=line, evidence=evidence,
                                     kind="DJANGO_ORM", via="type"))
        # ---- server-side views and redirects (navigation flow)
        if name in ("render_template", "render", "TemplateResponse", "render_to_response") and call.args:
            idx = 1 if name in ("render", "TemplateResponse") and len(call.args) > 1 else 0
            v = self.static_str(call.args[idx], ls, mod)[0]
            if v:
                self.views.setdefault(pf.id, []).append(v)
        if name in ("redirect", "RedirectResponse", "HttpResponseRedirect"):
            target = call.args[0] if call.args else next((kw.value for kw in call.keywords if kw.arg == "url"), None)
            if isinstance(target, ast.Call) and (_dotted(target.func) or "").split(".")[-1] in ("url_for", "reverse") and target.args:
                ep = self.static_str(target.args[0], ls, mod)[0]
                if ep:
                    self.redirects.setdefault(pf.id, []).append(("endpoint", ep))
            elif target is not None:
                v, _ = self.static_str(target, ls, mod)
                if v:
                    self.redirects.setdefault(pf.id, []).append(("path" if v.startswith("/") else "endpoint", v))
        # ---- HTTP client / test client
        verb = HTTP_VERBS_PY.get(name)
        if name == "request" and len(call.args) >= 2:
            verb = (self.static_str(call.args[0], ls, mod)[0] or "").upper() or None
        if verb and call.args and isinstance(func, ast.Attribute):
            url_node = call.args[1] if name == "request" else call.args[0]
            s, dyn = self.static_str(url_node, ls, mod)
            if isinstance(url_node, ast.Call) and (_dotted(url_node.func) or "").split(".")[-1] in ("url_path_for", "url_for", "reverse") \
                    and url_node.args:
                rn = self.static_str(url_node.args[0], ls, mod)[0]
                if rn:
                    self.named_http.append((pf, verb, rn, line, evidence))
            if s:
                if s.startswith("?"):
                    s2 = re.sub(r"^\?+", "", s)
                    s, dyn = (s2, True) if s2.startswith("/") else (None, dyn)
                if s and (s.startswith("/") or s.startswith("http")):
                    is_test = pf.kind == EntityType.TEST.value
                    self.ctx.http_calls.append(HttpCall(pf.id, verb, s, pf.file, line, "python", is_test, dyn,
                                                        "PYTHON_TEST_CLIENT" if is_test else "PYTHON_HTTP_CLIENT", evidence))

    # ================================================================ resolution helpers
    def _resolve_module(self, dotted: str, from_mod: PyModule | None = None, level: int = 0) -> PyModule | None:
        key = (dotted, from_mod.name if from_mod else None, level)
        if key in self._mod_cache:
            return self._mod_cache[key]
        res = None
        if level and from_mod is not None:
            pkg_parts = from_mod.name.split(".")
            is_pkg = from_mod.file.endswith("__init__.py")
            base = pkg_parts if is_pkg else pkg_parts[:-1]
            if level > 1:
                base = base[: len(base) - (level - 1)]
            full = ".".join(base + ([dotted] if dotted else []))
            res = self.modules.get(full)
        else:
            res = self.modules.get(dotted)
            if res is None and dotted:
                cands = [m for n, m in self.modules.items() if n.endswith("." + dotted)]
                if len(cands) == 1:
                    res = cands[0]
                elif cands and from_mod is not None:
                    def common(a: str, b: str) -> int:
                        n = 0
                        for x, y in zip(a.split("."), b.split(".")):
                            if x != y:
                                break
                            n += 1
                        return n
                    res = max(cands, key=lambda m: common(m.name, from_mod.name))
        self._mod_cache[key] = res
        return res

    def _resolve_module_alias(self, mod: PyModule, alias: str) -> PyModule | None:
        imp = mod.imports.get(alias)
        if not imp or isinstance(imp, list):
            return None
        target, level, is_from, symbol, _, _ = imp
        if is_from:
            return self._resolve_module(f"{target}.{symbol}" if target else symbol, mod, level)
        return self._resolve_module(target, mod, 0)

    def _import_target(self, mod: PyModule, alias: str, depth: int = 0):
        """Resolve an imported alias to ('module', PyModule) | ('class', PyClass) | ('func', PyFunc) | ('const', str)."""
        if depth > 5:
            return None
        imp = mod.imports.get(alias)
        if not imp or isinstance(imp, list):
            return None
        target, level, is_from, symbol, _, _ = imp
        if not is_from:
            m = self._resolve_module(target, mod, 0)
            return ("module", m) if m else None
        m = self._resolve_module(target, mod, level) if (target or level) else None
        if m is not None:
            r = self._symbol_in(m, symbol, depth + 1)
            if r:
                return r
        sub = self._resolve_module(f"{target}.{symbol}" if target else symbol, mod, level)
        return ("module", sub) if sub else None

    def _symbol_in(self, m: PyModule, name: str, depth: int = 0):
        if name in m.classes:
            return ("class", m.classes[name])
        if name in m.functions:
            return ("func", m.functions[name])
        if name in m.consts:
            return ("const", m.consts[name])
        if name in m.routers:
            return ("router", (m, name))
        if name in m.var_types:
            return ("var", (m, name))
        if name in m.imports:
            return self._import_target(m, name, depth + 1)
        return None

    def resolve_name(self, mod: PyModule, name: str):
        if name in mod.classes:
            return ("class", mod.classes[name])
        if name in mod.functions:
            return ("func", mod.functions[name])
        if name in mod.imports:
            return self._import_target(mod, name)
        if name in mod.var_types:
            return ("var", (mod, name))
        return None

    def resolve_dotted_class(self, mod: PyModule, dotted: str | None) -> PyClass | None:
        if not dotted:
            return None
        dotted = dotted.replace("typing.", "")
        parts = dotted.split(".")
        r = self.resolve_name(mod, parts[0])
        for p in parts[1:]:
            if r is None:
                return None
            if r[0] == "module":
                r = self._symbol_in(r[1], p)
            elif r[0] == "class":
                inner = self.classes.get(f"{r[1].qual}.{p}")
                r = ("class", inner) if inner else None
            else:
                return None
        return r[1] if r and r[0] == "class" else None

    def class_mro(self, pc: PyClass) -> list[PyClass]:
        out, queue, seen = [pc], [pc], {pc.qual}
        while queue:
            c = queue.pop(0)
            mod = self.modules[c.module]
            for b in c.bases:
                bc = self.resolve_dotted_class(mod, b)
                if bc and bc.qual not in seen:
                    seen.add(bc.qual)
                    out.append(bc)
                    queue.append(bc)
        return out

    def find_method(self, pc: PyClass, name: str) -> PyFunc | None:
        for c in self.class_mro(pc):
            if name in c.methods:
                return c.methods[name]
        return None

    def _eval_type(self, expr: tuple | None, pf: PyFunc, mod: PyModule, depth: int = 0) -> PyClass | None:
        """Evaluate an expression to the class of the object it denotes (instance)."""
        if expr is None or depth > 5:
            return None
        lt = self.local_types.get(pf.qual, {})
        cls = self.classes.get(pf.cls) if pf.cls else None
        k = expr[0]
        if k == "name":
            n = expr[1]
            if n in ("self",) and cls:
                return cls
            if n in lt:
                return self.resolve_dotted_class(mod, lt[n])
            r = self.resolve_name(mod, n)
            if r and r[0] == "var":
                m, v = r[1]
                return self.resolve_dotted_class(m, m.var_types.get(v))
            return None
        if k == "attr":
            base = expr[1]
            if base and base[0] == "name" and base[1] == "self" and cls:
                for c in self.class_mro(cls):
                    if expr[2] in c.attr_types:
                        return self.resolve_dotted_class(self.modules[c.module], c.attr_types[expr[2]])
                return None
            bc = self._eval_type(base, pf, mod, depth + 1)
            if bc:
                for c in self.class_mro(bc):
                    if expr[2] in c.attr_types:
                        return self.resolve_dotted_class(self.modules[c.module], c.attr_types[expr[2]])
            return None
        if k == "call":
            inner = expr[1]
            if inner and inner[0] == "name":
                if inner[1] == "super" and cls:
                    mro = self.class_mro(cls)
                    return mro[1] if len(mro) > 1 else None
                r = self.resolve_name(mod, inner[1])
                if r and r[0] == "class":
                    return r[1]
            if inner and inner[0] == "attr":
                # Module.Class() or obj.factory() (unknown)
                dotted = self._expr_dotted(inner)
                c = self.resolve_dotted_class(mod, dotted) if dotted else None
                return c
        return None

    @staticmethod
    def _expr_dotted(expr: tuple | None) -> str | None:
        if expr is None:
            return None
        if expr[0] == "name":
            return expr[1]
        if expr[0] == "attr":
            b = PythonAnalyzer._expr_dotted(expr[1])
            return f"{b}.{expr[2]}" if b else None
        return None

    # ================================================================ Phase C: resolve
    def resolve(self) -> None:
        gb = self.gb
        # ---- decide which classes are ORM models (needs inheritance, so done here)
        for pc in self.classes.values():
            mro = self.class_mro(pc)
            if pc.abstract:
                continue
            is_django = any(c.django for c in mro)
            is_sqlmodel = any(c.sqlmodel for c in mro)
            is_model = pc.model_hint or is_django or any(c.model_hint for c in mro[1:])
            if is_sqlmodel and not (pc.table_kw or pc.raw_table):
                continue            # SQLModel classes without table=True are schemas, not tables
            if not is_model:
                continue
            cols = list(pc.columns)
            for anc in mro[1:]:
                if anc.abstract or (is_django and not anc.raw_table) or (is_sqlmodel and not anc.table_kw):
                    cols += [c for c in anc.columns if c[0] not in {x[0] for x in cols}]
            if not (pc.raw_table or cols or is_django):
                continue
            pc.columns = cols
            pc.orm = "django" if is_django else "sqlalchemy"
            if pc.raw_table:
                pc.table = pc.raw_table
            elif is_sqlmodel:
                pc.table = pc.name.lower()
            elif is_django:
                parts = pc.file.split("/")
                app = parts[-2] if len(parts) > 1 else pc.module
                if app in ("models",) and len(parts) > 2:
                    app = parts[-3]
                pc.table = f"{app}_{pc.name.lower()}"
            else:
                pc.table = snake_case(pc.name)
        # ---- tables for ORM models
        model_by_qual: dict[str, PyClass] = {}
        for pc in self.classes.values():
            if not pc.table:
                continue
            model_by_qual[pc.qual] = pc
            tid = ensure_table(gb, pc.table, source=pc.id)
            gb.rel(pc.id, tid, RelType.MAPS_TO, kind=f"{(pc.orm or 'orm').upper()}_MODEL",
                   evidence=f"{pc.name} -> {pc.table}", file=pc.file, line=pc.line,
                   reason="ORM model declaration (__tablename__/Meta.db_table or framework default naming)")
            for colname, ctype, cline, fk, pk in pc.columns:
                ensure_column(gb, pc.table, {"name": colname, "type": ctype, "pk": bool(pk), "nullable": True},
                              file=pc.file, line=cline, kind="ORM_COLUMN")
                if fk:
                    target_table = None
                    if fk.startswith("model:"):
                        ref = fk[6:]
                        rc = self.resolve_dotted_class(self.modules[pc.module], ref) or next(
                            (c for c in self.classes.values() if c.name == ref.split(".")[-1] and c.table), None)
                        target_table = rc.table if rc and rc.table else None
                    else:
                        target_table = fk.split(".")[0] if "." in fk else None
                    if target_table:
                        gb.rel(ensure_table(gb, pc.table), ensure_table(gb, target_table), RelType.FOREIGN_KEY,
                               kind="ORM_COLUMN", evidence=f"{colname} -> {fk}", file=pc.file, line=cline,
                               reason="ForeignKey declared on model field", column=colname)
        # ---- imports
        for mod in self.modules.values():
            fid = make_id("file", mod.file)
            for alias, imp in mod.imports.items():
                if isinstance(imp, list):
                    continue
                target, level, is_from, symbol, line, _ = imp
                r = self._import_target(mod, alias)
                if not r:
                    continue
                kind, obj = r
                tgt = None
                if kind == "module" and obj:
                    tgt = make_id("file", obj.file)
                elif kind == "class":
                    tgt = obj.id
                elif kind == "func":
                    tgt = obj.id if obj.kind != EntityType.TEST.value else None
                elif kind in ("const", "router", "var"):
                    m = self._resolve_module(target, mod, level) if is_from else None
                    tgt = make_id("file", m.file) if m else None
                if tgt and tgt != fid:
                    stmt = f"from {'.' * level}{target} import {symbol}" if is_from else f"import {target}"
                    gb.rel(fid, tgt, RelType.IMPORTS, kind="PYTHON_IMPORT", evidence=stmt, file=mod.file, line=line,
                           reason="import statement resolved to repository module/symbol")
        # ---- inheritance + overrides
        for pc in self.classes.values():
            mod = self.modules[pc.module]
            for b in pc.bases:
                bc = self.resolve_dotted_class(mod, b)
                if bc:
                    gb.rel(pc.id, bc.id, RelType.EXTENDS, kind="PYTHON_AST", evidence=f"class {pc.name}({b})",
                           file=pc.file, line=pc.line, reason="base class resolved through module scope/imports")
            mro = self.class_mro(pc)[1:]
            for mname, pf in pc.methods.items():
                if mname.startswith("__") or pf.kind == EntityType.TEST.value:
                    continue
                for base in mro:
                    if mname in base.methods:
                        gb.rel(pf.id, base.methods[mname].id, RelType.OVERRIDES, kind="PYTHON_AST",
                               evidence=f"{pc.name}.{mname} overrides {base.name}.{mname}", file=pf.file, line=pf.line,
                               reason="same method name in resolved base class")
                        break
        # ---- routes
        self._resolve_routes()
        # ---- requests built from route names: client.post(app.url_path_for("users:login"))
        for pf, verb, rn, line, evidence in self.named_http:
            for aid in sorted(self.route_names.get(rn, set()) or self.route_names.get(rn.split(":")[-1].split(".")[-1], set())):
                api = gb.get(aid)
                if not api or api.props.get("http_method") not in (verb, "ANY"):
                    continue
                if pf.kind == EntityType.TEST.value:
                    gb.rel(pf.id, aid, RelType.TESTS, kind="PYTHON_TEST_CLIENT", evidence=evidence, file=pf.file, line=line,
                           reason=f"test sends {verb} to route named '{rn}'")
                else:
                    gb.rel(pf.id, aid, RelType.HTTP_CALLS, kind="PYTHON_HTTP_CLIENT", evidence=evidence, file=pf.file,
                           line=line, reason=f"request to route named '{rn}'")
        # ---- calls
        for c in self.calls:
            self._resolve_call(c)
        # ---- ORM ops
        for op in self.orm_ops:
            pf: PyFunc = op["func"]
            mod = self.modules[pf.module]
            pc = self.resolve_dotted_class(mod, op["model"])
            if not pc or not pc.table:
                continue
            if pf.kind == EntityType.TEST.value:
                continue
            rtype = RelType.WRITES if op["op"] == "WRITES" else RelType.READS
            gb.rel(pf.id, make_id("table", pc.table.lower()), rtype, kind=op["kind"], evidence=op["evidence"],
                   file=pf.file, line=op["line"], status=Status.CANDIDATE if op.get("weak") else Status.VERIFIED,
                   reason=f"ORM operation on model {pc.name}")
        # ---- SQL
        # ---- Alembic migrations -> tables / columns / foreign keys
        for mig in self.ctx.migrations:
            tid = ensure_table(gb, mig["table"], file=mig["file"], line=mig["line"], defined=mig["op"] == "create_table",
                               source=make_id("file", mig["file"]))
            for col in mig["columns"]:
                ensure_column(gb, mig["table"], col, file=mig["file"], line=mig["line"], kind="ALEMBIC_MIGRATION")
            for col, ref in mig["fks"]:
                ref_table = ref.split(".")[0] if "." in ref else ref
                gb.rel(tid, ensure_table(gb, ref_table), RelType.FOREIGN_KEY, kind="ALEMBIC_MIGRATION",
                       evidence=f"{col} -> {ref}", file=mig["file"], line=mig["line"],
                       reason="ForeignKey declared in migration", column=col)
        # ---- views / redirects as navigation metadata on handler functions
        for fid, vs in self.views.items():
            e = gb.get(fid)
            if e:
                e.props["renders_views"] = sorted(set(vs))[:10]
        func_by_name: dict[str, list] = {}
        for pf in self.funcs.values():
            func_by_name.setdefault(pf.name, []).append(pf)
        for fid, rs in self.redirects.items():
            e = gb.get(fid)
            if not e:
                continue
            out = []
            for kind, val in rs:
                if kind == "path":
                    out.append({"path": val})
                else:
                    fname = val.split(".")[-1].split(":")[-1]
                    cands = func_by_name.get(fname, [])
                    out.append({"endpoint": val, "handler": cands[0].id if len(cands) == 1 else None})
            e.props["redirects_to"] = out[:10]
        model_tables = {pc.name: pc.table for pc in model_by_qual.values()}
        for s in self.sql:
            add_sql(gb, s["sql"], file=s["file"], line=s["line"], language="python", executor_id=s["executor"],
                    exec_kind=s["kind"], exec_evidence=s["evidence"], exec_status=s["status"],
                    exec_reason=s.get("reason", ""), dynamic=s["dynamic"])
        for pf in self.funcs.values():
            pf.node = None   # free memory

    def _method_index(self) -> dict:
        if getattr(self, "_midx", None) is None:
            idx: dict[str, list] = {}
            for pc in self.classes.values():
                if pc.is_test_class or self.modules[pc.module].is_test:
                    continue
                for mname, pf in pc.methods.items():
                    if pf.kind != EntityType.TEST.value:
                        idx.setdefault(mname, []).append(pf)
            self._midx = idx
        return self._midx

    def _resolve_call(self, c: dict) -> None:
        gb = self.gb
        pf: PyFunc = c["func"]
        mod = self.modules[pf.module]
        recv, name = c["recv"], c["name"]
        target = None
        inst = None
        candidate = False
        if recv is None:
            r = self.resolve_name(mod, name)
            if r is None and pf.cls:
                # nested call to sibling function? (python requires self.) -> unresolved
                pass
            if r:
                if r[0] == "func":
                    target = r[1]
                elif r[0] == "class":
                    inst = r[1]
                    init = self.find_method(inst, "__init__")
                    target = init
        else:
            if recv[0] == "name" and recv[1] in ("self", "cls") and pf.cls:
                pc = self.classes.get(pf.cls)
                target = self.find_method(pc, name) if pc else None
            else:
                pc = self._eval_type(recv, pf, mod)
                if pc:
                    target = self.find_method(pc, name)
                elif recv[0] == "name":
                    r = self.resolve_name(mod, recv[1])
                    if r and r[0] == "module" and r[1]:
                        r2 = self._symbol_in(r[1], name)
                        if r2 and r2[0] == "func":
                            target = r2[1]
                        elif r2 and r2[0] == "class":
                            inst = r2[1]
                            target = self.find_method(inst, "__init__")
                    elif r and r[0] == "class":
                        target = self.find_method(r[1], name)
                elif recv[0] == "attr":
                    dotted = self._expr_dotted(recv)
                    if dotted:
                        parts = dotted.split(".")
                        r = self.resolve_name(mod, parts[0])
                        for p in parts[1:]:
                            if r and r[0] == "module" and r[1]:
                                r = self._symbol_in(r[1], p)
                            else:
                                r = None
                                break
                        if r and r[0] == "module" and r[1]:
                            r2 = self._symbol_in(r[1], name)
                            if r2 and r2[0] == "func":
                                target = r2[1]
                            elif r2 and r2[0] == "class":
                                inst = r2[1]
        is_test = pf.kind == EntityType.TEST.value
        if inst is not None:
            if is_test:
                if not self.modules[inst.module].is_test:
                    gb.rel(pf.id, inst.id, RelType.TESTS, kind="PYTHON_TEST_CALL", evidence=c["evidence"], file=pf.file,
                           line=c["line"], reason="test instantiates class")
            else:
                gb.rel(pf.id, inst.id, RelType.INSTANTIATES, kind="PYTHON_CALL", evidence=c["evidence"], file=pf.file,
                       line=c["line"], reason="constructor call resolved through scope/imports")
        if target is None and inst is None and recv is not None and recv[0] == "name":
            # aiosql / PugSQL: queries = aiosql.from_path(...); queries.get_user_by_email(conn, ...)
            r = self.resolve_name(mod, recv[1])
            if r and r[0] == "var":
                m2, v = r[1]
                if any(k in (m2.var_types.get(v) or "") for k in ("from_path", "from_str", "pugsql.module")):
                    for sid in self.ctx.named_sql.get(name, []):
                        gb.rel(pf.id, sid, RelType.EXECUTES, kind="AIOSQL", evidence=c["evidence"], file=pf.file,
                               line=c["line"], reason=f"named query '{name}' loaded with {m2.var_types.get(v)}")
                    if self.ctx.named_sql.get(name):
                        self.ctx.stats["resolved_calls"] += 1
                        return
        if target is None and inst is None and recv is not None and name not in COMMON_METHOD_NAMES \
                and len(name) > 3 and not name.startswith("__"):
            # unknown receiver type: link only when exactly ONE non-test class in the repo defines this method
            owners = self._method_index().get(name, [])
            if len(owners) == 1 and pf.kind != EntityType.TEST.value:
                t = owners[0]
                gb.rel(pf.id, t.id, RelType.CALLS, kind="PYTHON_CALL_BY_UNIQUE_NAME", evidence=c["evidence"],
                       file=pf.file, line=c["line"], status=Status.CANDIDATE, confidence=0.4,
                       reason=f"receiver type unknown; '{name}' is defined by exactly one repository class")
                self.ctx.stats["candidate_calls"] += 1
                return
        if target is None:
            if inst is None and name not in BUILTINS:
                self.ctx.stats["unresolved_calls"] += 1
            return
        if target.kind == EntityType.TEST.value:
            return
        self.ctx.stats["resolved_calls"] += 1
        reason = "call resolved through " + ("self/cls" if recv and recv[0] == "name" and recv[1] in ("self", "cls")
                                             else "receiver type" if recv else "module scope/imports")
        if is_test:
            if not self.modules[target.module].is_test:
                gb.rel(pf.id, target.id, RelType.TESTS, kind="PYTHON_TEST_CALL", evidence=c["evidence"], file=pf.file,
                       line=c["line"], reason="test invokes " + reason)
            return
        gb.rel(pf.id, target.id, RelType.CALLS, kind="PYTHON_CALL", evidence=c["evidence"], file=pf.file,
               line=c["line"], status=Status.CANDIDATE if candidate else Status.VERIFIED, reason=reason)

    def _router_key(self, mod: PyModule, name: str) -> tuple[str, str] | None:
        if name in mod.routers:
            return (mod.name, name)
        r = self._import_target(mod, name)
        if r and r[0] == "router":
            m, v = r[1]
            return (m.name, v)
        if "." in name:
            head, _, tail = name.partition(".")
            m = self._resolve_module_alias(mod, head)
            if m and tail in m.routers:
                return (m.name, tail)
        return None

    def _resolve_routes(self) -> None:
        # prefixes added by register_blueprint/include_router (one level + chained parents)
        extra: dict[tuple[str, str], list[str]] = {}
        parents: dict[tuple[str, str], tuple[tuple[str, str] | None, str]] = {}
        for reg in self.registrations:
            mod = self.modules.get(reg["module"])
            if not mod or not reg["target"]:
                continue
            key = self._router_key(mod, reg["target"])
            if key is None:
                continue
            pkey = self._router_key(mod, reg["parent"]) if reg["parent"] else None
            parents[key] = (pkey, reg["prefix"] or "")

        def full_prefix(key: tuple[str, str], depth: int = 0) -> str:
            m = self.modules[key[0]]
            own = m.routers.get(key[1], {}).get("prefix", "")
            if depth > 5 or key not in parents:
                return own
            pkey, pre = parents[key]
            parent_pre = full_prefix(pkey, depth + 1) if pkey else ""
            return "/".join(x.strip("/") for x in (parent_pre, pre, own) if x and x.strip("/"))

        for r in self.pending_routes:
            mod = self.modules[r["module"]]
            key = self._router_key(mod, r["obj"])
            if key is None and r["obj"].split(".")[-1] not in ("app", "bp", "router", "api", "blueprint", "blue_print", "routes"):
                continue
            prefix = full_prefix(key) if key else ""
            path = "/" + "/".join(x.strip("/") for x in (prefix, r["path"]) if x and x.strip("/"))
            if r["path"].endswith("/") and len(path) > 1:
                path += "/"
            pf: PyFunc = r["func"]
            kind = (self.modules[key[0]].routers[key[1]]["kind"].upper() + "_ROUTE") if key else "PYTHON_ROUTE_DECORATOR"
            for v in r["verbs"]:
                aid = add_route(self.ctx, method=v, path=path, handler_id=pf.id, file=pf.file, line=r["line"], language="python",
                                kind=kind, evidence=r["evidence"], reason="route decorator on handler function")
                for nm in {r.get("route_name"), pf.name} - {None}:
                    self.route_names.setdefault(nm, set()).add(aid)
        # ---- Django urls with include() prefixes
        prefix_of: dict[str, list[str]] = {}
        for _ in range(4):
            for u in self.django_urls:
                if u["include"]:
                    inc = u["include"]
                    if inc.endswith(".urls") or "." in inc:
                        m = self._resolve_module(inc, self.modules.get(u["module"]))
                        if m:
                            parents_pre = prefix_of.get(u["module"], [""])
                            prefix_of[m.name] = sorted({"/".join(x for x in (pp.strip("/"), self._django_route(u["route"], u["regex"]).strip("/")) if x) for pp in parents_pre})
                    elif inc.endswith("urls") and "." in inc:
                        pass
        router_prefix: dict[tuple[str, str], list[str]] = {}
        for u in self.django_urls:
            if u["include"] and u["include"].endswith(".urls") and u["include"].split(".")[0] in self.modules[u["module"]].var_types:
                rv = u["include"].split(".")[0]
                router_prefix[(u["module"], rv)] = [self._django_route(u["route"], u["regex"])]
        for u in self.django_urls:
            if u["include"] or not u["view"]:
                continue
            mod = self.modules[u["module"]]
            route = self._django_route(u["route"], u["regex"])
            for pre in prefix_of.get(mod.name, [""]):
                path = "/" + "/".join(x for x in (pre.strip("/"), route.strip("/")) if x)
                self._django_view_route(mod, u["view"], path, u["line"], f"path({u['route']!r}, {u['view']})")
        for d in self.drf_routes:
            mod = self.modules[d["module"]]
            pres = router_prefix.get((mod.name, d["router"]), [""])
            for pre in pres:
                for mpre in prefix_of.get(mod.name, [""]):
                    base = "/" + "/".join(x for x in (mpre.strip("/"), pre.strip("/"), d["prefix"].strip("/")) if x)
                    for p in (base, base + "/{pk}"):
                        self._django_view_route(mod, d["view"], p, d["line"], f"router.register({d['prefix']!r}, {d['view']})",
                                                kind="DRF_ROUTER")

    @staticmethod
    def _django_route(route: str, regex: bool) -> str:
        if not regex:
            return route
        r = route.lstrip("^").rstrip("$")
        r = re.sub(r"\(\?P<(\w+)>[^)]*\)", r"{\1}", r)
        return r

    def _django_view_route(self, mod: PyModule, view: str, path: str, line: int, evidence: str, kind: str = "DJANGO_URLCONF") -> None:
        parts = view.split(".")
        r = self.resolve_name(mod, parts[0])
        for p in parts[1:]:
            if r and r[0] == "module" and r[1]:
                r = self._symbol_in(r[1], p)
            elif r and r[0] == "class":
                c = self.classes.get(f"{r[1].qual}.{p}")
                r = ("class", c) if c else None
            else:
                r = None
        if r and r[0] == "func":
            add_route(self.ctx, method="ANY", path=path, handler_id=r[1].id, file=mod.file, line=line, language="python",
                      kind=kind, evidence=evidence, reason="Django URLconf maps path to view function")
        elif r and r[0] == "class":
            pc: PyClass = r[1]
            verbs = {m: HTTP_VERBS_PY[m] for m in HTTP_VERBS_PY if self.find_method(pc, m)}
            drf = {"list": "GET", "create": "POST", "retrieve": "GET", "update": "PUT", "partial_update": "PATCH", "destroy": "DELETE"}
            for mname, verb in drf.items():
                if mname in pc.methods:
                    verbs[mname] = verb
            if verbs:
                for mname, verb in verbs.items():
                    add_route(self.ctx, method=verb, path=path, handler_id=self.find_method(pc, mname).id, file=mod.file,
                              line=line, language="python", kind=kind, evidence=evidence,
                              reason=f"class-based view method {mname}()")
            else:
                add_route(self.ctx, method="ANY", path=path, handler_id=pc.id, file=mod.file, line=line, language="python",
                          kind=kind, evidence=evidence, reason="Django URLconf maps path to class-based view")
        else:
            add_route(self.ctx, method="ANY", path=path, handler_id=None, file=mod.file, line=line, language="python",
                      kind=kind, evidence=evidence)
            self.gb.diag("INFO", "UNRESOLVED_VIEW", f"Django view {view} not resolved", mod.file, line)
