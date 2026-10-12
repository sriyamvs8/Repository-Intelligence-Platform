"""Developer-facing insights computed from the graph:

* classify_layer   – API / Controller / Service / Data access / Model / SQL / Database / Test / Other
* repo_profile     – 4-line plain-English summary + architecture description (deterministic, evidence-based)
* request_flow     – step-by-step flow of one endpoint: client -> API -> controller -> service -> data -> SQL -> table
* impact_report    – scored impact assessment: evidence-based dependents with risk levels, plus clearly-labelled
                     *potential* (heuristic) impact such as shared-table coupling and same-name methods
"""
from __future__ import annotations

import re
from collections import Counter, deque

from ..kg.model import RelType
from .queries import CALLABLE_TYPES, DEPENDENCY_EDGES, GraphQueries, slim_edge, slim_node
from .store import GraphStore

R = RelType
LAYER_ORDER = ["Client", "API", "Controller", "Service", "Data access", "Model", "SQL", "Database", "Test", "Other"]
LAYER_HELP = {
    "Client": "whoever sends the HTTP request (browser/frontend, another service, a test)",
    "API": "REST endpoints (URL + HTTP method) the application exposes",
    "Controller": "classes/functions that receive HTTP requests and return responses",
    "Service": "business logic: rules, validation, orchestration",
    "Data access": "repositories / DAOs / mappers / CRUD helpers that talk to the database",
    "Model": "entity / model classes mapped to database tables",
    "SQL": "SQL statements found in code, mapper XML or .sql files",
    "Database": "database tables",
    "Test": "automated tests",
    "Other": "helpers, mappers/converters, utilities and configuration",
}
_CTRL = re.compile(r"(controller|resource|endpoint|views?|viewset|apiview|router|routes?)$", re.I)
_CTRL_PATH = re.compile(r"/(controllers?|routes?|views?|endpoints?)/|(routes|views|urls)\.py$", re.I)
_SVC = re.compile(r"(service|serviceimpl|manager|usecase|facade|interactor)$", re.I)
_SVC_PATH = re.compile(r"/(services?|usecases?)/", re.I)
_DATA = re.compile(r"(repository|repo|dao|mapper|crud|store|gateway|queries|db)$", re.I)
_DATA_PATH = re.compile(r"/(repository|repositories|repo|dao|mapper|mappers|crud|persistence|db|database|queries)/", re.I)
_MODEL_PATH = re.compile(r"/(models?|entity|entities|domain/model|schemas?)/", re.I)


def owner_name(n: dict) -> str:
    """Short owning class/module name of a callable (or the type name itself)."""
    if n.get("class"):
        return n["class"].split(".")[-1]
    q = (n.get("qualified_name") or n.get("name") or "").split("(")[0]
    parts = q.split(".")
    if n.get("type") in CALLABLE_TYPES + ["Test"] and len(parts) > 1:
        return parts[-2]
    return parts[-1]


def classify_layer(n: dict, handler_ids: set[str] | None = None, model_ids: set[str] | None = None) -> str:
    t = n.get("type")
    if t == "RestApi":
        return "API"
    if t == "Table":
        return "Database"
    if t == "SqlQuery":
        return "SQL"
    if t == "Test" or n.get("is_test_code"):
        return "Test"
    if t in ("File", "Directory", "Repository", "Column"):
        return "Other"
    path = "/" + (n.get("file") or "").lower()
    owner = owner_name(n)
    anns = set(n.get("annotations") or [])
    cls_fqn = n.get("class") or ""
    if handler_ids and n.get("id") in handler_ids:
        return "Controller"
    if model_ids and (n.get("id") in model_ids or ("class:" + cls_fqn) in model_ids):
        return "Model"
    if anns & {"RestController", "Controller", "Path"} or _CTRL.search(owner):
        return "Controller"
    if _CTRL_PATH.search(path) and t in ("Function",):
        return "Controller"
    if anns & {"Service"} or _SVC.search(owner) or _SVC_PATH.search(path):
        return "Service"
    if anns & {"Repository", "Mapper"} or n.get("spring_data_repository") or _DATA.search(owner) or _DATA_PATH.search(path):
        return "Data access"
    if anns & {"Entity", "Table", "Document"} or _MODEL_PATH.search(path) or owner.lower().endswith(("entity", "model")):
        return "Model"
    return "Other"


FRAMEWORKS = {
    "SPRING_ANNOTATION": "Spring Web (MVC)", "JAXRS_ANNOTATION": "JAX-RS", "FEIGN_CLIENT": "Spring Cloud OpenFeign",
    "OPENAPI_GENERATOR_CONVENTION": "OpenAPI Generator", "JPA_MAPPING": "JPA / Hibernate",
    "SPRING_DATA_DERIVED": "Spring Data", "SPRING_DATA_INHERITED": "Spring Data", "SPRING_DATA_QUERY": "Spring Data",
    "MYBATIS_XML": "MyBatis", "MYBATIS_ANNOTATION": "MyBatis", "JAVA_SQL_CALL": "JDBC",
    "FLASK_ROUTE": "Flask", "BLUEPRINT_ROUTE": "Flask", "FASTAPI_ROUTE": "FastAPI", "APIROUTER_ROUTE": "FastAPI",
    "DJANGO_URLCONF": "Django", "DRF_ROUTER": "Django REST framework", "CONNEXION_OPERATION_ID": "Connexion",
    "SQLALCHEMY_ORM": "SQLAlchemy", "SQLALCHEMY_MODEL": "SQLAlchemy", "FLASK_SQLALCHEMY": "Flask-SQLAlchemy",
    "DJANGO_ORM": "Django ORM", "DJANGO_MODEL": "Django ORM", "PYTHON_SQL_CALL": "DB-API / raw SQL",
    "OPENAPI_SPEC": "OpenAPI specification", "JAVA_TEST_CLIENT": "MockMvc / REST test client",
    "PYTHON_TEST_CLIENT": "HTTP test client", "JAVA_TEST_CALL": "JUnit", "PYTHON_TEST_CALL": "pytest / unittest",
    "PYTHON_HTTP_CLIENT": "requests/httpx client", "JAVA_HTTP_CLIENT": "RestTemplate/WebClient",
}


def _join(items: list[str], n: int = 4) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) > n:
        return ", ".join(items[:n]) + f" and {len(items) - n} more"
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


class Insights:
    def __init__(self, store: GraphStore, gid: str) -> None:
        self.s, self.gid = store, gid
        self.q = GraphQueries(store, gid)
        self._handlers: set[str] | None = None
        self._models: set[str] | None = None

    # ------------------------------------------------------------------ helpers
    def handler_ids(self) -> set[str]:
        if self._handlers is None:
            self._handlers = {e["target"] for e in self.s.rels_by_type(self.gid, [R.HANDLED_BY.value], limit=100000)}
        return self._handlers

    def model_ids(self) -> set[str]:
        if self._models is None:
            self._models = {e["source"] for e in self.s.rels_by_type(self.gid, [R.MAPS_TO.value], limit=100000)}
        return self._models

    def data_classes(self) -> set[str]:
        """Classes whose methods execute SQL or access tables (evidence for the data-access layer)."""
        if getattr(self, "_data", None) is None:
            ids = set()
            for t in ("EXECUTES", "READS", "WRITES"):
                ids |= {e["source"] for e in self.s.rels_by_type(self.gid, [t], limit=200000)}
            self._data = {n.get("class") for n in self.s.get_nodes(self.gid, list(ids)[:20000]) if n.get("class")}
        return self._data

    def layer(self, n: dict) -> str:
        l = classify_layer(n, self.handler_ids(), self.model_ids())
        if l == "Data access":
            owner_fqn = n.get("class") or (n.get("qualified_name") if n.get("type") in ("Class", "Interface") else "")
            name = owner_name(n).lower()
            # "...Mapper" is only data access when it really runs SQL (MyBatis); MapStruct/DTO mappers are not
            if name.endswith("mapper") and owner_fqn not in self.data_classes():
                return "Other"
        return l

    # ------------------------------------------------------------------ T3: repository profile
    def repo_profile(self) -> dict:
        meta = self.s.get_meta(self.gid) or {}
        rep = meta.get("report", {})
        ec = rep.get("entity_counts", {})
        name = (meta.get("repo_key") or "this repository").split("/")[-1]
        langs = {k: v["files"] for k, v in (rep.get("files_by_language") or {}).items()}
        jv, py = langs.get("java", 0), langs.get("python", 0)
        if jv and py and min(jv, py) >= 0.2 * max(jv, py):
            main_lang = "Java and Python"
        else:
            main_lang = "Java" if jv >= py and jv else ("Python" if py else "SQL")
        kinds = Counter()
        for rt in ("HANDLED_BY", "MAPS_TO", "READS", "WRITES", "EXECUTES", "TESTS", "HTTP_CALLS", "CROSS_LANGUAGE_MAP"):
            for e in self.s.rels_by_type(self.gid, [rt], limit=100000):
                kinds[e.get("source_kind", "")] += 1
        apis = self.s.nodes_by_type(self.gid, ["RestApi"], 5000)
        for a in apis:
            for k in a.get("sources") or []:
                kinds[k] += 1
        frameworks = []
        for k, _ in kinds.most_common():
            f = FRAMEWORKS.get(k)
            if f and f not in frameworks:
                frameworks.append(f)
        test_fw = {"JUnit", "pytest / unittest", "MockMvc / REST test client", "HTTP test client",
                   "requests/httpx client", "RestTemplate/WebClient", "OpenAPI specification"}
        web_fw = {"Spring Web (MVC)", "JAX-RS", "Flask", "FastAPI", "Django", "Django REST framework", "Connexion",
                  "Spring Cloud OpenFeign", "OpenAPI Generator"}
        frameworks.sort(key=lambda f: (0 if f in web_fw else (2 if f in test_fw else 1)))
        main_fw = [f for f in frameworks if f not in test_fw]
        tables = self.s.nodes_by_type(self.gid, ["Table"], 2000)
        # domain resources from API paths and tables
        seg = Counter()
        for a in apis:
            parts = [p for p in (a.get("path") or "").split("/") if p and "{" not in p and "<" not in p
                     and p.lower() not in ("api", "v1", "v2", "v3", "rest", "public", "admin", "internal")]
            if parts:
                seg[re.sub(r"\.\w+$", "", parts[0].lower())] += 1
        resources = []
        for r, _ in seg.most_common(10):
            key = r.rstrip("s")
            if r and not any(x.rstrip("s") == key for x in resources):
                resources.append(r)
        resources = resources[:6]
        if not resources:
            resources = [t["name"] for t in tables[:6]]
        # layers
        layer_nodes: dict[str, list[dict]] = {l: [] for l in LAYER_ORDER}
        classes = self.s.nodes_by_type(self.gid, ["Class", "Interface"], 20000)
        for c in classes:
            if c.get("is_test_code"):
                continue
            layer_nodes[self.layer(c)].append(c)
        for h in self.s.get_nodes(self.gid, list(self.handler_ids())[:3000]):
            if h["type"] == "Function" and not h.get("class"):
                layer_nodes["Controller"].append(h)
        for l in layer_nodes:
            seen, uniq = set(), []
            for n in layer_nodes[l]:
                key = owner_name(n) if n["type"] != "Function" else (n.get("file") or n["name"])
                if key not in seen:
                    seen.add(key)
                    uniq.append(n)
            if l in ("Controller", "Service", "Data access", "Model"):
                uniq.sort(key=lambda n: -self.s.degree(self.gid, n["id"]) if n["type"] != "Function" else 0)
            layer_nodes[l] = uniq
        layers = []
        for l in ["API", "Controller", "Service", "Data access", "Model", "SQL", "Database", "Test"]:
            if l == "API":
                cnt, ex = len(apis), [a["name"] for a in apis[:4]]
            elif l == "SQL":
                cnt, ex = ec.get("SqlQuery", 0), []
            elif l == "Database":
                cnt, ex = len(tables), [t["name"] for t in tables[:5]]
            elif l == "Test":
                cnt, ex = ec.get("Test", 0), []
            else:
                cnt = len(layer_nodes[l])
                ex = [owner_name(n) if n["type"] != "Function" else (n.get("file") or "").split("/")[-1] for n in layer_nodes[l][:4]]
            if cnt:
                layers.append({"layer": l, "count": cnt, "examples": ex, "meaning": LAYER_HELP[l]})
        modules = Counter()
        for d in self.s.nodes_by_type(self.gid, ["File"], 50000):
            f = d.get("file") or ""
            if d.get("language") in ("java", "python") and "/" in f:
                modules[f.split("/")[0]] += 1
        top_modules = [m for m, c in modules.most_common(8) if c >= 3]
        multi_service = len([m for m in top_modules if any((a.get("file") or "").startswith(m + "/") for a in apis)]) > 1
        hubs = self.q.summary().get("most_called", [])[:5]
        cross = len(self.s.rels_by_type(self.gid, [R.CROSS_LANGUAGE_MAP.value, R.HTTP_CALLS.value], limit=10000))
        handled = rep.get("apis_with_handler", 0)
        api_db = self.q.api_to_database([a["id"] for a in apis[:400]], max_nodes=2000)
        apis_with_data = len({p["api"] for p in api_db.get("paths", [])})
        fks = len(self.s.rels_by_type(self.gid, [R.FOREIGN_KEY.value], limit=10000))
        style = "a set of services (one per top-level module)" if multi_service else "a single application"
        present = [l["layer"] for l in layers if l["layer"] in ("Controller", "Service", "Data access")]
        layered = " → ".join(present) if len(present) >= 2 else ""

        # ---- the 4 lines
        fw = _join(main_fw, 3)
        line1 = (f"{name} is {'an' if main_lang[0] in 'AEIOU' else 'a'} {main_lang} application"
                 f"{' built with ' + fw if fw else ''} that exposes {len(apis)} REST endpoint{'s' if len(apis) != 1 else ''}"
                 f"{f' and works with {len(tables)} database table' + ('s' if len(tables) != 1 else '') if tables else ''}.")
        line2 = (f"It is mainly about {_join(resources, 5)}." if resources else
                 "Its business domain could not be inferred from endpoint paths or table names.")
        ex = lambda layer: _join([e for l in layers if l["layer"] == layer for e in l["examples"]], 2)
        has = {l["layer"] for l in layers}
        if layered:
            line3 = f"It follows a layered design ({layered}): requests arrive at {ex('Controller') or 'controllers'}"
            if "Service" in has:
                line3 += f", business logic sits in {ex('Service')}"
            elif "Data access" in has:
                line3 += ", which call the data-access layer directly (no separate service layer)"
            if "Data access" in has:
                line3 += f", and data is read/written through {ex('Data access')}"
            line3 += "."
        else:
            line3 = f"It is organised as {style}; request handling and data access are not split into clearly named layers."
        tests = ec.get("Test", 0)
        line4 = (f"{handled} of {len(apis)} endpoints are linked to their handler code and {apis_with_data} reach the database"
                 f"{f'; {tests} automated tests exercise the code' if tests else '; no automated tests were found'}.")
        arch = {
            "style": style, "layered_flow": layered, "layers": layers, "modules": top_modules,
            "frameworks": frameworks, "languages": langs, "central_components": [
                {"name": owner_name(h["node"]) + "." + h["node"]["name"], "id": h["node"]["id"], "callers": h["callers"]} for h in hubs],
            "data_model": {"tables": [t["name"] for t in tables[:40]], "table_count": len(tables), "foreign_keys": fks},
            "integration": {"cross_service_calls": cross},
            "explanation": self._arch_explanation(name, layers, style, layered, top_modules, frameworks, tables, fks, cross),
        }
        return {"name": name, "summary_lines": [line1, line2, line3, line4], "architecture": arch,
                "stats": {"files": rep.get("files_analyzed"), "endpoints": len(apis), "handled": handled,
                          "endpoints_reaching_db": apis_with_data, "tables": len(tables), "tests": tests,
                          "classes": ec.get("Class", 0) + ec.get("Interface", 0),
                          "methods": ec.get("Method", 0) + ec.get("Function", 0) + ec.get("Constructor", 0),
                          "relationships": rep.get("total_relationships")},
                "resources": resources, "generated_by": "deterministic (from the graph)"}

    @staticmethod
    def _arch_explanation(name, layers, style, layered, modules, frameworks, tables, fks, cross) -> list[str]:
        out = [f"{name} is organised as {style}."]
        if modules and len(modules) > 1:
            out.append(f"Top-level modules: {_join(modules, 6)}.")
        if layered:
            out.append(f"A typical request travels {layered.replace('→', '→')} and finally reaches the database.")
        for l in layers:
            if l["layer"] in ("API", "SQL", "Test"):
                continue
            ex = f" (e.g. {_join(l['examples'], 3)})" if l["examples"] else ""
            out.append(f"{l['layer']} layer — {l['count']} component(s){ex}: {l['meaning']}.")
        if tables:
            out.append(f"Data model: {len(tables)} tables{f' connected by {fks} foreign keys' if fks else ''}.")
        if cross:
            out.append(f"{cross} HTTP call(s) between components/services were detected.")
        if frameworks:
            out.append(f"Detected technology: {_join(frameworks, 8)}.")
        return out

    # ------------------------------------------------------------------ T6a: request flow
    def request_flow(self, api_id: str, include_candidates: bool = True, max_calls: int = 6) -> dict:
        api = self.s.get_node(self.gid, api_id)
        if not api:
            raise KeyError(api_id)
        sts = ["VERIFIED", "CANDIDATE"] if include_candidates else ["VERIFIED"]
        steps: list[dict] = []
        nodes: dict[str, dict] = {api["id"]: api}
        edges: list[dict] = []
        callers = [(e, o) for e, o, _ in self.s.neighbors(self.gid, api_id, "in",
                                                          [R.HTTP_CALLS.value, R.CROSS_LANGUAGE_MAP.value, R.TESTS.value], sts)]
        lanes: dict[str, list[str]] = {l: [] for l in LAYER_ORDER}
        lanes["Client"].append("client:http")
        nodes["client:http"] = {"id": "client:http", "type": "Client", "name": "HTTP client / frontend",
                                "qualified_name": f"sends {api['name']}"}
        lanes["API"].append(api_id)
        for e, o in callers[:8]:
            nodes[o["id"]] = o
            lanes["Client"].append(o["id"])
            edges.append(e)
        dist = {api_id: 0}
        parent: dict[str, tuple[str, dict]] = {}
        q = deque([api_id])
        done = set()
        fwd = [R.HANDLED_BY.value, R.CALLS.value, R.EXECUTES.value, R.READS.value, R.WRITES.value,
               R.HTTP_CALLS.value, R.CROSS_LANGUAGE_MAP.value]
        while q and len(nodes) < 120:
            cur = q.popleft()
            if cur in done:
                continue
            done.add(cur)
            cn = nodes.get(cur) or self.s.get_node(self.gid, cur) or {}
            if cn.get("type") in ("Table",):
                continue
            nb = self.s.neighbors(self.gid, cur, "out", fwd, sts, limit=200)
            nb += self.s.neighbors(self.gid, cur, "in", [R.OVERRIDES.value], sts, limit=20)
            for e, o, _ in nb:
                c = 0 if e["type"] in (R.HANDLED_BY.value, R.EXECUTES.value, R.OVERRIDES.value, R.READS.value, R.WRITES.value) else 1
                nd = dist[cur] + c
                if nd > max_calls or o["id"] in dist:
                    continue
                if o.get("is_test_code"):
                    continue
                dist[o["id"]] = nd
                parent[o["id"]] = (cur, e)
                nodes[o["id"]] = o
                edges.append(e)
                (q.appendleft if c == 0 else q.append)(o["id"])
        for nid in dist:
            if nid == api_id:
                continue
            n = nodes[nid]
            l = self.layer(n)
            if nid in self.handler_ids() and parent.get(nid, ("", {}))[1].get("type") == R.HANDLED_BY.value:
                l = "Controller"
            lanes.setdefault(l, []).append(nid)
        # narrative steps (ordered by BFS distance, then layer)
        order = sorted((nid for nid in dist if nid != api_id),
                       key=lambda x: (dist[x], LAYER_ORDER.index(next((l for l, ids in lanes.items() if x in ids), "Other"))))
        steps.append({"n": 1, "layer": "Client", "text": f"A client (frontend, another service or a test) sends {api['name']}.",
                      "dev": f"HTTP {api.get('http_method', '')} {api.get('path', '')}", "node": None})
        steps.append({"n": 2, "layer": "API", "text": f"The application exposes this endpoint{' (also documented in the API spec)' if api.get('documented') else ''}.",
                      "dev": f"{api['id']}  [{', '.join(api.get('sources') or [])}]", "node": api_id})
        k = 3
        seen_text = set()
        for nid in order[:60]:
            n = nodes[nid]
            prev, e = parent[nid]
            pn = nodes.get(prev) or {}
            l = next((l for l, ids in lanes.items() if nid in ids), "Other")
            verb = {"HANDLED_BY": "is handled by", "CALLS": "calls", "EXECUTES": "runs SQL", "READS": "reads table",
                    "WRITES": "writes table", "OVERRIDES": "is implemented by", "HTTP_CALLS": "calls endpoint",
                    "CROSS_LANGUAGE_MAP": "calls (other language) endpoint"}.get(e["type"], e["type"].lower())
            if e["type"] == R.OVERRIDES.value:
                text = f"{_lbl(pn)} is an interface/abstract method; the real work happens in {_lbl(n)}."
            elif e["type"] in (R.READS.value, R.WRITES.value):
                text = f"{_lbl(pn)} {'reads from' if e['type'] == 'READS' else 'writes to'} the {n['name']} table."
            elif e["type"] == R.EXECUTES.value:
                text = f"{_lbl(pn)} runs the SQL statement “{n['name']}”."
            else:
                text = f"{_lbl(pn)} {verb} {_lbl(n)}."
            if text in seen_text:
                continue
            seen_text.add(text)
            steps.append({"n": k, "layer": l, "text": text, "status": e["status"],
                          "dev": f"{e['type']}  {e.get('file') or ''}:{e.get('line') or ''}  {e.get('evidence') or ''}"[:220],
                          "node": nid, "edge": e["id"]})
            k += 1
        used_lanes = [l for l in LAYER_ORDER if lanes.get(l)]
        return {"api": slim_node(api), "lanes": [{"layer": l, "help": LAYER_HELP.get(l, ""), "nodes": lanes[l]} for l in used_lanes],
                "nodes": [slim_node(n) if n.get("type") != "Client" else n for n in nodes.values()],
                "edges": [slim_edge(e) for e in {e["id"]: e for e in edges}.values()],
                "steps": steps, "tables": [nodes[i]["name"] for i in lanes.get("Database", [])],
                "truncated": len(nodes) >= 120}

    # ------------------------------------------------------------------ T6b: impact risk model
    TYPE_WEIGHT = {"RestApi": 1.0, "Table": 0.8, "Method": 0.6, "Function": 0.6, "Constructor": 0.5, "Class": 0.6,
                   "Interface": 0.7, "SqlQuery": 0.5, "Test": 0.35, "Column": 0.4}
    DECAY = [1.0, 1.0, 0.7, 0.5, 0.35, 0.25, 0.2]

    def impact_report(self, root: str, depth: int = 4, include_candidates: bool = True) -> dict:
        rn = self.s.get_node(self.gid, root)
        if not rn:
            raise KeyError(root)
        base = self.q.impact_of(root, depth, include_candidates, max_nodes=400)
        items = []
        for typ, rows in base["affected"].items():
            for r in rows:
                n, d, via = r["node"], r["distance"], r["via"]
                w = self.TYPE_WEIGHT.get(n["type"], 0.4)
                score = w * self.DECAY[min(d, len(self.DECAY) - 1)] * (0.6 if via.get("status") == "CANDIDATE" else 1.0)
                if n["type"] == "Test":
                    risk = "Run"
                else:
                    risk = "High" if score >= 0.6 else ("Medium" if score >= 0.3 else "Low")
                items.append({"node": n, "distance": d, "via": via, "score": round(score, 2), "risk": risk,
                              "layer": self.layer(n), "kind": "evidence"})
        items.sort(key=lambda x: (-x["score"], x["distance"], x["node"]["name"]))
        affected_ids = {i["node"]["id"] for i in items} | {root}
        # ---- potential (heuristic) impact – clearly labelled, not evidence
        potential = []
        sts = ["VERIFIED", "CANDIDATE"] if include_candidates else ["VERIFIED"]
        # (1) shared-data coupling: tables written by the root's downstream code -> other readers of those tables
        down = self.q.traverse(root, "out", [R.CALLS.value, R.EXECUTES.value, R.WRITES.value, R.READS.value], 3,
                               include_candidates, 300)
        touched_tables = {}
        for nid, (prev, e) in down["parent"].items():
            if e["type"] in (R.WRITES.value, R.READS.value):
                touched_tables.setdefault(nid, set()).add(e["type"])
        if rn["type"] == "Table":
            touched_tables[root] = {"WRITES"}
        for tid, ops in list(touched_tables.items())[:10]:
            if "WRITES" not in ops:
                continue
            tn = self.s.get_node(self.gid, tid)
            for e, x, _ in self.s.neighbors(self.gid, tid, "in", [R.READS.value], sts, limit=200):
                execs = [o for _, o, _ in self.s.neighbors(self.gid, x["id"], "in", [R.EXECUTES.value], sts)] if x["type"] == "SqlQuery" else [x]
                for ex in execs:
                    if ex["id"] in affected_ids:
                        continue
                    affected_ids.add(ex["id"])
                    potential.append({"node": slim_node(ex), "risk": "Medium", "kind": "potential",
                                      "reason": f"also reads table {tn['name']}, which the changed code writes — data/format changes may affect it"})
        # (2) same-name methods not linked by static analysis (possible reflection / dynamic dispatch)
        if rn["type"] in CALLABLE_TYPES:
            for n in self.s.search(self.gid, rn["name"], CALLABLE_TYPES, 30):
                if n["name"] == rn["name"] and n["id"] not in affected_ids and not n.get("is_test_code"):
                    affected_ids.add(n["id"])
                    potential.append({"node": slim_node(n), "risk": "Low", "kind": "potential",
                                      "reason": "same method name in another class — only affected through dynamic dispatch/reflection or copy-paste"})
                    if len(potential) > 40:
                        break
        # (3) untested user-facing entry points
        apis = [i for i in items if i["node"]["type"] == "RestApi"]
        untested = []
        for a in apis:
            if not self.s.neighbors(self.gid, a["node"]["id"], "in", [R.TESTS.value], sts, limit=1):
                untested.append(a["node"]["name"])
        tests = [i for i in items if i["node"]["type"] == "Test"]
        high = [i for i in items if i["risk"] == "High"]
        med = [i for i in items if i["risk"] == "Medium"]
        blast = min(100, round(sum(i["score"] for i in items if i["risk"] != "Run") * 8 + len(potential) * 1.5))
        level = "High" if (len(apis) >= 3 or blast >= 60) else ("Medium" if (apis or blast >= 25) else "Low")
        recs = []
        if apis:
            recs.append(f"Re-test {len(apis)} endpoint(s): {_join([a['node']['name'] for a in apis], 5)}.")
        if tests:
            recs.append(f"Run {len(tests)} automated test(s) linked to the change (listed under 'Tests to run').")
        if untested:
            recs.append(f"{len(untested)} affected endpoint(s) have no test: {_join(untested, 4)} — add tests before changing.")
        if touched_tables:
            recs.append(f"Check data compatibility for table(s): {_join([self.s.get_node(self.gid, t)['name'] for t in touched_tables], 5)}.")
        if not items:
            recs.append("No dependents found in the graph. If this code is used via reflection, configuration or from "
                        "outside the repository, those usages are not visible to static analysis.")
        nodes_for_graph = base["nodes"] + [p["node"] for p in potential[:30]]
        return {
            "root": slim_node(rn), "risk_level": level, "blast_radius": blast,
            "counts": {"apis": len(apis), "code": len([i for i in items if i["node"]["type"] in CALLABLE_TYPES + ["Class", "Interface"]]),
                       "tables": len([i for i in items if i["node"]["type"] == "Table"]) + len(touched_tables),
                       "tests": len(tests), "potential": len(potential), "high": len(high), "medium": len(med)},
            "summary": (f"Changing {_lbl(rn)} has {level.upper()} estimated impact: {len(apis)} endpoint(s), "
                        f"{len([i for i in items if i['node']['type'] in CALLABLE_TYPES])} method(s) and {len(tests)} test(s) are "
                        f"connected to it by evidence; {len(potential)} more are potentially affected (heuristic)."),
            "items": items, "potential": potential, "tests_to_run": [t["node"] for t in tests],
            "untested_apis": untested, "tables_touched": [self.s.get_node(self.gid, t)["name"] for t in touched_tables],
            "recommendations": recs,
            "graph": {"nodes": nodes_for_graph, "edges": base["edges"]},
            "model": {"score": "type weight × distance decay × status factor",
                      "type_weights": self.TYPE_WEIGHT, "distance_decay": self.DECAY[1:], "candidate_factor": 0.6,
                      "risk_thresholds": {"High": 0.6, "Medium": 0.3},
                      "evidence_edges": DEPENDENCY_EDGES, "max_depth": depth,
                      "potential_rules": ["shared table written by the changed code → other readers (Medium)",
                                          "same-named methods elsewhere (Low)"],
                      "disclaimer": "Evidence items are connected by static analysis; 'potential' items are heuristics. "
                                    "Neither proves runtime breakage — use as a review/test checklist."},
        }


def _lbl(n: dict) -> str:
    if not n:
        return "?"
    if n.get("type") in CALLABLE_TYPES + ["Test"]:
        return f"{owner_name(n)}.{n['name']}()"
    if n.get("type") == "Table":
        return f"table {n['name']}"
    return n.get("name") or n.get("id", "?")
