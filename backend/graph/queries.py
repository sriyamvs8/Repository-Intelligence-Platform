"""Deterministic, bounded graph queries and projections (shared by the REST API, the UI and
the NLP layer). Every traversal has an explicit edge policy, direction, depth and size bound."""
from __future__ import annotations

from collections import deque

from ..kg.model import RelType
from .store import GraphStore

R = RelType
STRUCTURAL = [R.CONTAINS.value, R.DEFINES.value]
SEMANTIC = [r.value for r in RelType if r not in (R.CONTAINS, R.DEFINES)]
# Edge types along which "X depends on Y" (edge X -> Y). Used reversed for impact analysis.
DEPENDENCY_EDGES = [R.CALLS.value, R.IMPORTS.value, R.EXTENDS.value, R.IMPLEMENTS.value, R.INSTANTIATES.value,
                    R.EXECUTES.value, R.READS.value, R.WRITES.value, R.HANDLED_BY.value, R.TESTS.value,
                    R.MAPS_TO.value, R.HTTP_CALLS.value, R.CROSS_LANGUAGE_MAP.value, R.OVERRIDES.value]
CALLABLE_TYPES = ["Method", "Function", "Constructor"]
VALID_STATUSES = ["VERIFIED", "CANDIDATE"]

NODE_FIELDS = ("module", "api_count", "apis", "id", "type", "name", "qualified_name", "file", "line", "end_line", "language", "http_method",
               "path", "operation", "data_type", "primary_key", "signature", "sql", "documented", "is_test_code",
               "parse_status", "summary", "spring_data_repository")
EDGE_FIELDS = ("id", "source", "target", "type", "status", "source_kind", "evidence", "file", "line", "reason",
               "confidence", "occurrences", "aggregated", "operations", "count")


def make_owner_id(h: dict) -> str:
    """Group key for a handler: its class (Java/Python methods) or its module file (Python functions)."""
    if h.get("class"):
        return "class:" + h["class"]
    if h["type"] in ("Class", "Interface"):
        return h["id"]
    return "file:" + (h.get("file") or "")


def slim_node(n: dict) -> dict:
    return {k: n.get(k) for k in NODE_FIELDS if n.get(k) not in (None, "", [], False)} | {"id": n["id"], "type": n["type"], "name": n.get("name")}


def slim_edge(e: dict) -> dict:
    return {k: e.get(k) for k in EDGE_FIELDS if e.get(k) is not None}


def _statuses(include_candidates: bool) -> list[str]:
    return VALID_STATUSES if include_candidates else ["VERIFIED"]


class Subgraph:
    def __init__(self, max_nodes: int) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.max_nodes = max_nodes
        self.truncated = False

    def add_node(self, n: dict) -> bool:
        if n["id"] in self.nodes:
            return True
        if len(self.nodes) >= self.max_nodes:
            self.truncated = True
            return False
        self.nodes[n["id"]] = slim_node(n)
        return True

    def add_edge(self, e: dict) -> None:
        if e["source"] in self.nodes and e["target"] in self.nodes:
            self.edges[e["id"]] = slim_edge(e)

    def to_dict(self, **extra) -> dict:
        return {"nodes": list(self.nodes.values()), "edges": list(self.edges.values()),
                "truncated": self.truncated, "node_count": len(self.nodes), "edge_count": len(self.edges), **extra}


class GraphQueries:
    def __init__(self, store: GraphStore, gid: str) -> None:
        self.s = store
        self.gid = gid

    # ------------------------------------------------------------------ basics
    def node(self, nid: str) -> dict | None:
        return self.s.get_node(self.gid, nid)

    def search(self, q: str, types: list[str] | None = None, limit: int = 20) -> list[dict]:
        return [slim_node(n) for n in self.s.search(self.gid, q, types, limit)]

    def node_details(self, nid: str) -> dict | None:
        n = self.node(nid)
        if not n:
            return None
        nb = self.s.neighbors(self.gid, nid, "both", limit=400)
        groups: dict[str, list] = {}
        for e, other, d in nb:
            key = f"{e['type']}:{d}"
            groups.setdefault(key, []).append({"edge": slim_edge(e), "node": slim_node(other)})
        return {"node": n, "relationships": groups, "degree": self.s.degree(self.gid, nid)}

    # ------------------------------------------------------------------ projections
    def view_ecosystem(self, max_nodes: int = 150) -> dict:
        sg = Subgraph(max_nodes)
        roots = self.s.nodes_by_type(self.gid, ["Repository"], 1)
        if not roots:
            return sg.to_dict(mode="ecosystem")
        q = deque([roots[0]])
        sg.add_node(roots[0])
        while q:
            cur = q.popleft()
            kids = self.s.neighbors(self.gid, cur["id"], "out", [R.CONTAINS.value], limit=400)
            kids.sort(key=lambda x: (x[1]["type"] != "Directory", x[1]["name"]))
            for e, child, _ in kids:
                if not sg.add_node(child):
                    break
                sg.add_edge(e)
                if child["type"] == "Directory":
                    q.append(child)
        return sg.to_dict(mode="ecosystem")

    def view_architecture(self, max_nodes: int = 150, include_candidates: bool = True, detail_threshold: int = 40) -> dict:
        """APIs -> handlers -> (calls) -> data access -> tables. Small repos: every path in detail.
        Large repos: an aggregated overview (handler class -> table) so the canvas stays readable."""
        apis = self.s.nodes_by_type(self.gid, ["RestApi"], 5000)
        if len(apis) > detail_threshold:
            return self.view_architecture_overview(apis, max_nodes, include_candidates)
        sg = Subgraph(max_nodes)
        sts = _statuses(include_candidates)
        res = self.api_to_database([a["id"] for a in apis], include_candidates=include_candidates,
                                   max_nodes=max_nodes, sg=sg)
        for api in apis:                       # endpoints without data access, if room is left
            sg.add_node(api)
            for e, h, _ in self.s.neighbors(self.gid, api["id"], "out", [R.HANDLED_BY.value], sts):
                if sg.add_node(h):
                    sg.add_edge(e)
        for t in self.s.nodes_by_type(self.gid, ["Table"], 200):
            sg.add_node(t)
        for e in self.s.rels_by_type(self.gid, [R.FOREIGN_KEY.value, R.CROSS_LANGUAGE_MAP.value, R.HTTP_CALLS.value], sts):
            sg.add_edge(e)
        return sg.to_dict(mode="architecture", paths=res.get("paths", [])[:50])

    def view_architecture_overview(self, apis: list[dict], max_nodes: int = 150, include_candidates: bool = True) -> dict:
        """Aggregated projection: one node per handler class/module (with its endpoint count) and one
        derived DATA_ACCESS edge per (class, table) summarising the underlying evidence-backed paths.
        Aggregated edges are a view only; they are never stored in the graph."""
        sts = _statuses(include_candidates)
        groups: dict[str, dict] = {}
        for api in apis:
            for e, h, _ in self.s.neighbors(self.gid, api["id"], "out", [R.HANDLED_BY.value], sts):
                owner_id = make_owner_id(h)
                g = groups.setdefault(owner_id, {"apis": set(), "handlers": set(), "candidate": True})
                g["apis"].add(api["name"])
                g["handlers"].add(h["id"])
                if e["status"] == "VERIFIED":
                    g["candidate"] = False
        fwd = [R.CALLS.value, R.EXECUTES.value, R.READS.value, R.WRITES.value, R.HTTP_CALLS.value, R.CROSS_LANGUAGE_MAP.value]
        access: dict[tuple[str, str], dict] = {}
        api_owner = {}
        for owner_id, g in groups.items():
            dist = {h: 0 for h in g["handlers"]}
            q = deque(g["handlers"])
            done: set[str] = set()
            while q:
                cur = q.popleft()
                if cur in done:
                    continue
                done.add(cur)
                for e, o, _ in (self.s.neighbors(self.gid, cur, "out", fwd, sts, limit=300) +
                                self.s.neighbors(self.gid, cur, "in", [R.OVERRIDES.value], sts, limit=50)):
                    if o["type"] == "Table":
                        a = access.setdefault((owner_id, o["id"]), {"READS": 0, "WRITES": 0, "candidate": True})
                        a[e["type"]] = a.get(e["type"], 0) + 1
                        if e["status"] == "VERIFIED":
                            a["candidate"] = False
                        continue
                    if o["type"] == "RestApi":       # service-to-service call
                        api_owner.setdefault((owner_id, o["id"]), e)
                        continue
                    c = 0 if e["type"] in (R.EXECUTES.value, R.OVERRIDES.value, R.READS.value, R.WRITES.value) else 1
                    nd = dist[cur] + c
                    if nd > 6 or (o["id"] in dist and dist[o["id"]] <= nd):
                        continue
                    dist[o["id"]] = nd
                    (q.appendleft if c == 0 else q.append)(o["id"])
        sg = Subgraph(max_nodes)
        ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]["apis"]), kv[0]))
        tables_used = {t for (_, t) in access}
        budget_groups = max(5, max_nodes - min(len(tables_used), max_nodes // 2))
        shown = 0
        for owner_id, g in ranked:
            if shown >= budget_groups:
                sg.truncated = True
                break
            n = self.node(owner_id)
            if not n:
                continue
            n = dict(n)
            n["name"] = f"{n['name']} · {len(g['apis'])} API{'s' if len(g['apis']) != 1 else ''}"
            n["api_count"] = len(g["apis"])
            if sg.add_node(n):
                f = n.get("file") or ""
                mod = f.split("/")[0] if "/" in f else ""
                if mod and mod not in ("src", "app", "backend"):
                    sg.nodes[owner_id]["module"] = mod
                sg.nodes[owner_id]["api_count"] = len(g["apis"])
                sg.nodes[owner_id]["apis"] = sorted(g["apis"])[:30]
                shown += 1
        for (owner_id, tid), a in sorted(access.items()):
            if owner_id not in sg.nodes:
                continue
            t = self.node(tid)
            if not t or not sg.add_node(t):
                continue
            ops = [op for op in ("READS", "WRITES") if a.get(op)]
            eid = "agg:" + owner_id + "->" + tid
            sg.edges[eid] = {"id": eid, "source": owner_id, "target": tid, "type": "DATA_ACCESS",
                             "status": "CANDIDATE" if a["candidate"] else "VERIFIED", "aggregated": True,
                             "operations": ops, "count": a.get("READS", 0) + a.get("WRITES", 0),
                             "evidence": f"{a.get('READS', 0)} read path(s), {a.get('WRITES', 0)} write path(s)",
                             "reason": "derived view: summarises CALLS/EXECUTES/READS/WRITES paths from this class's handlers"}
        for (owner_id, api_id), e in api_owner.items():
            for e2, h, _ in self.s.neighbors(self.gid, api_id, "out", [R.HANDLED_BY.value], sts):
                tgt = make_owner_id(h)
                if owner_id in sg.nodes and tgt in sg.nodes and tgt != owner_id:
                    eid = "agg:" + owner_id + "=>" + tgt
                    sg.edges[eid] = {"id": eid, "source": owner_id, "target": tgt, "type": e["type"], "status": e["status"],
                                     "aggregated": True, "evidence": e.get("evidence"), "reason": "derived view: HTTP call between services"}
        for e in self.s.rels_by_type(self.gid, [R.FOREIGN_KEY.value], sts):
            sg.add_edge(e)
        return sg.to_dict(mode="architecture-overview", groups=len(groups), groups_shown=shown,
                          note="Aggregated overview (handler class → table). Click a class and use Focus for its endpoints.")

    def view_neighborhood(self, root: str, hops: int = 1, max_nodes: int = 150, rel_types: list[str] | None = None,
                          include_structure: bool = True, include_candidates: bool = True,
                          per_node_limit: int = 40) -> dict:
        sg = Subgraph(max_nodes)
        start = self.node(root)
        if not start:
            return sg.to_dict(mode="hops", error="root not found")
        sg.add_node(start)
        types = rel_types or (None if include_structure else SEMANTIC)
        sts = _statuses(include_candidates)
        frontier = [start["id"]]
        hidden: dict[str, int] = {}
        for _ in range(max(0, min(hops, 3))):
            nxt = []
            for nid in frontier:
                nb = self.s.neighbors(self.gid, nid, "both", types, sts, limit=2000)
                if len(nb) > per_node_limit:
                    hidden[nid] = len(nb) - per_node_limit
                    # keep semantic edges first, structure last
                    nb.sort(key=lambda x: (x[0]["type"] in STRUCTURAL, x[0]["type"]))
                    nb = nb[:per_node_limit]
                for e, other, _ in nb:
                    new = other["id"] not in sg.nodes
                    if not sg.add_node(other):
                        break
                    sg.add_edge(e)
                    if new:
                        nxt.append(other["id"])
            frontier = nxt
            if sg.truncated:
                break
        # add edges between already-included nodes
        for nid in list(sg.nodes):
            for e, other, _ in self.s.neighbors(self.gid, nid, "out", types, sts, limit=300):
                sg.add_edge(e)
        return sg.to_dict(mode="hops", root=root, hidden_neighbors=hidden)

    def view_cross_language(self, max_nodes: int = 150) -> dict:
        sg = Subgraph(max_nodes)
        for e in self.s.rels_by_type(self.gid, [R.CROSS_LANGUAGE_MAP.value, R.HTTP_CALLS.value], VALID_STATUSES):
            for nid in (e["source"], e["target"]):
                n = self.node(nid)
                if n:
                    sg.add_node(n)
            sg.add_edge(e)
            for h, other, _ in self.s.neighbors(self.gid, e["target"], "out", [R.HANDLED_BY.value]):
                sg.add_node(other)
                sg.add_edge(h)
        return sg.to_dict(mode="cross-language")

    # ------------------------------------------------------------------ traversals
    def traverse(self, root: str, direction: str, rel_types: list[str], max_depth: int = 3,
                 include_candidates: bool = True, max_nodes: int = 300) -> dict:
        """BFS returning reached nodes with distance and the edge used (for path reconstruction)."""
        sts = _statuses(include_candidates)
        dist = {root: 0}
        parent: dict[str, tuple[str, dict]] = {}
        q = deque([root])
        while q and len(dist) < max_nodes:
            cur = q.popleft()
            if dist[cur] >= max_depth:
                continue
            for e, other, _ in self.s.neighbors(self.gid, cur, direction, rel_types, sts, limit=500):
                if other["id"] not in dist:
                    dist[other["id"]] = dist[cur] + 1
                    parent[other["id"]] = (cur, e)
                    q.append(other["id"])
        return {"dist": dist, "parent": parent}

    def _path_to(self, parent: dict, nid: str) -> tuple[list[str], list[dict]]:
        nodes, edges = [nid], []
        while nid in parent:
            prev, e = parent[nid]
            edges.append(e)
            nodes.append(prev)
            nid = prev
        return list(reversed(nodes)), list(reversed(edges))

    def callers(self, root: str, depth: int = 1, include_candidates: bool = True) -> dict:
        return self._directional(root, "in", [R.CALLS.value, R.HTTP_CALLS.value, R.CROSS_LANGUAGE_MAP.value,
                                               R.HANDLED_BY.value, R.TESTS.value], depth, include_candidates)

    def callees(self, root: str, depth: int = 1, include_candidates: bool = True) -> dict:
        return self._directional(root, "out", [R.CALLS.value, R.EXECUTES.value, R.READS.value, R.WRITES.value,
                                                R.HTTP_CALLS.value, R.CROSS_LANGUAGE_MAP.value, R.INSTANTIATES.value,
                                                R.HANDLED_BY.value], depth, include_candidates)

    def dependencies_of(self, root: str, depth: int = 2, include_candidates: bool = True) -> dict:
        types = DEPENDENCY_EDGES
        if (self.node(root) or {}).get("type") in ("Class", "Interface", "Enum"):
            types = DEPENDENCY_EDGES + [R.DEFINES.value]     # a type depends on what its members depend on
        res = self._directional(root, "out", types, depth + (1 if types is not DEPENDENCY_EDGES else 0), include_candidates)
        res["rows"] = [r for r in res["rows"] if r["via"]["type"] != R.DEFINES.value]
        return res

    def _directional(self, root: str, direction: str, types: list[str], depth: int, inc: bool,
                     max_nodes: int = 200) -> dict:
        t = self.traverse(root, direction, types, max(1, min(depth, 4)), inc, max_nodes)
        sg = Subgraph(max_nodes + 1)
        rows = []
        for nid, d in sorted(t["dist"].items(), key=lambda x: x[1]):
            n = self.node(nid)
            if not n:
                continue
            sg.add_node(n)
            if nid in t["parent"]:
                prev, e = t["parent"][nid]
                sg.add_edge(e)
                rows.append({"node": slim_node(n), "distance": d, "via": slim_edge(e)})
        return sg.to_dict(rows=rows, root=root)

    @staticmethod
    def _cost(edge: dict, other: dict) -> int:
        """0-1 BFS weights: only real call/dependency hops count towards depth. Plumbing hops
        (SQL statement nodes, interface->implementation, endpoint->handler, class->member) are free."""
        if edge["type"] in (R.OVERRIDES.value, R.EXECUTES.value, R.HANDLED_BY.value, R.DEFINES.value, R.MAPS_TO.value):
            return 0
        if other.get("type") == "SqlQuery":
            return 0
        return 1

    def impact_of(self, root: str, depth: int = 3, include_candidates: bool = True, max_nodes: int = 250) -> dict:
        """Who may be affected if `root` changes: reverse dependency traversal under an explicit edge
        policy. Overriding methods propagate to the method they override (dynamic dispatch)."""
        sts = _statuses(include_candidates)
        dist = {root: 0}
        parent: dict[str, tuple[str, dict]] = {}
        q = deque([root])
        done: set[str] = set()
        while q and len(dist) < max_nodes:
            cur = q.popleft()
            if cur in done:
                continue
            done.add(cur)
            cur_node = self.node(cur) or {}
            nbrs = self.s.neighbors(self.gid, cur, "in", DEPENDENCY_EDGES, sts, limit=500)
            # an implementation change affects callers of the interface/super method it overrides
            nbrs += [(e, o, d) for e, o, d in self.s.neighbors(self.gid, cur, "out", [R.OVERRIDES.value], sts)]
            # a change to the ROOT class affects its members' dependents (not classes reached transitively,
            # e.g. an ORM class reached from its table: that would pull in every getter's caller)
            if cur == root and cur_node.get("type") in ("Class", "Interface", "Enum"):
                nbrs += [(e, o, d) for e, o, d in self.s.neighbors(self.gid, cur, "out", [R.DEFINES.value], sts)
                         if o["type"] in CALLABLE_TYPES]
            for e, other, _ in nbrs:
                if other["type"] in ("File", "Directory", "Repository"):
                    continue
                c = self._cost(e, other)
                nd = dist[cur] + c
                if nd > depth or (other["id"] in dist and dist[other["id"]] <= nd):
                    continue
                dist[other["id"]] = nd
                parent[other["id"]] = (cur, e)
                (q.appendleft if c == 0 else q.append)(other["id"])
        sg = Subgraph(max_nodes + 1)
        groups: dict[str, list] = {}
        for nid, d in sorted(dist.items(), key=lambda x: x[1]):
            n = self.node(nid)
            if not n:
                continue
            sg.add_node(n)
            if nid == root:
                continue
            prev, e = parent[nid]
            sg.add_edge(e)
            groups.setdefault(n["type"], []).append({"node": slim_node(n), "distance": d, "via": slim_edge(e)})
        return sg.to_dict(root=root, affected=groups, total_affected=len(dist) - 1,
                          policy={"edges": DEPENDENCY_EDGES, "direction": "incoming (dependents)", "max_depth": depth,
                                  "note": "Connected under static rules; a review aid, not a proof of breakage. "
                                          "Depth counts call/dependency hops; SQL-statement, interface and handler hops are free."})

    def api_to_database(self, api_ids: list[str], include_candidates: bool = True, max_depth: int = 8,
                        max_nodes: int = 250, sg: Subgraph | None = None) -> dict:
        """Follow API -> HANDLED_BY -> CALLS* (+ implementations of called interface methods, + HTTP calls
        into other services of the repo) -> EXECUTES -> SqlQuery -> READS/WRITES -> Table (or ORM access).
        max_depth bounds the number of CALLS/HTTP hops (0-1 BFS)."""
        sts = _statuses(include_candidates)
        sg = sg or Subgraph(max_nodes)
        tables: dict[str, dict] = {}
        paths = []
        fwd = [R.HANDLED_BY.value, R.CALLS.value, R.EXECUTES.value, R.READS.value, R.WRITES.value,
               R.HTTP_CALLS.value, R.CROSS_LANGUAGE_MAP.value]
        for aid in api_ids:
            api = self.node(aid)
            if not api:
                continue
            sg.add_node(api)
            dist = {aid: 0}
            parent: dict[str, tuple[str, dict]] = {}
            q = deque([aid])
            done: set[str] = set()
            seen_tables: set[str] = set()
            while q:
                cur = q.popleft()
                if cur in done:
                    continue
                done.add(cur)
                cur_node = self.node(cur) or {}
                if cur_node.get("type") == "Table":
                    continue
                nbrs = self.s.neighbors(self.gid, cur, "out", fwd, sts, limit=300)
                nbrs += self.s.neighbors(self.gid, cur, "in", [R.OVERRIDES.value], sts, limit=50)
                for e, other, _ in nbrs:
                    c = 0 if e["type"] in (R.HANDLED_BY.value, R.EXECUTES.value, R.OVERRIDES.value, R.READS.value,
                                           R.WRITES.value) else 1
                    nd = dist[cur] + c
                    if nd > max_depth or (other["id"] in dist and dist[other["id"]] <= nd):
                        continue
                    dist[other["id"]] = nd
                    parent[other["id"]] = (cur, e)
                    (q.appendleft if c == 0 else q.append)(other["id"])
            for tid, d in dist.items():
                tn = self.node(tid)
                if not tn or tn["type"] != "Table" or tid in seen_tables:
                    continue
                seen_tables.add(tid)
                pn, pe = self._path_to(parent, tid)
                op = pe[-1]["type"] if pe else "READS"
                t = tables.setdefault(tid, {"table": slim_node(tn), "operations": set(), "apis": set()})
                t["operations"].add(op)
                t["apis"].add(api["name"])
                paths.append({"api": api["name"], "table": tn["name"], "operation": op, "nodes": pn,
                              "edges": [x["id"] for x in pe],
                              "status": "CANDIDATE" if any(x["status"] == "CANDIDATE" for x in pe) else "VERIFIED"})
                for n in self.s.get_nodes(self.gid, pn):
                    sg.add_node(n)
                for x in pe:
                    sg.add_edge(x)
        rows = [{"table": v["table"], "operations": sorted(v["operations"]), "apis": sorted(v["apis"])}
                for v in sorted(tables.values(), key=lambda v: v["table"]["name"])]
        return sg.to_dict(rows=rows, paths=paths)

    def path_between(self, a: str, b: str, max_depth: int = 6, include_candidates: bool = True) -> dict:
        sts = _statuses(include_candidates)
        for direction in ("out", "both"):
            dist = {a: 0}
            parent: dict[str, tuple[str, dict]] = {}
            q = deque([a])
            while q:
                cur = q.popleft()
                if cur == b or dist[cur] >= max_depth:
                    continue
                cur_type = (self.node(cur) or {}).get("type")
                for e, other, _ in self.s.neighbors(self.gid, cur, direction, SEMANTIC + [R.DEFINES.value], sts, limit=400):
                    # DEFINES is only traversed from a type to its members (never through files)
                    if e["type"] == R.DEFINES.value and not (e["source"] == cur and cur_type in ("Class", "Interface", "Enum")):
                        continue
                    if other["id"] not in dist:
                        dist[other["id"]] = dist[cur] + 1
                        parent[other["id"]] = (cur, e)
                        q.append(other["id"])
            if b in dist:
                pn, pe = self._path_to(parent, b)
                sg = Subgraph(50)
                for n in self.s.get_nodes(self.gid, pn):
                    sg.add_node(n)
                for e in pe:
                    sg.add_edge(e)
                return sg.to_dict(found=True, directed=direction == "out", path=pn, path_edges=[slim_edge(e) for e in pe])
        return Subgraph(1).to_dict(found=False)

    # ------------------------------------------------------------------ named queries
    def _pairs(self, rel_types: list[str], include_candidates: bool, limit: int, max_nodes: int = 300,
               source_filter: str | None = None) -> dict:
        sg = Subgraph(max_nodes)
        rows = []
        for e in self.s.rels_by_type(self.gid, rel_types, _statuses(include_candidates), limit=limit):
            if source_filter and e["source"] != source_filter and e["target"] != source_filter:
                continue
            pair = self.s.get_nodes(self.gid, [e["source"], e["target"]])
            if len(pair) != 2:
                continue
            ok = sg.add_node(pair[0]) and sg.add_node(pair[1])
            if ok:
                sg.add_edge(e)
            rows.append({"source": slim_node(pair[0]), "relationship": e["type"], "target": slim_node(pair[1]),
                         "status": e["status"], "evidence": e.get("evidence"), "file": e.get("file"), "line": e.get("line")})
        return sg.to_dict(rows=rows)

    def named(self, name: str, root: str | None = None, depth: int = 2, include_candidates: bool = True,
              max_nodes: int = 200) -> dict:
        if name == "apis_and_handlers":
            res = self._pairs([R.HANDLED_BY.value], include_candidates, 2000, max_nodes)
            handled = {r["source"]["id"] for r in res["rows"]}
            for api in self.s.nodes_by_type(self.gid, ["RestApi"], 2000):
                if api["id"] not in handled:
                    res["rows"].append({"source": slim_node(api), "relationship": "HANDLED_BY", "target": None,
                                        "status": "NO_HANDLER", "evidence": "documented in spec only / handler not found"})
            return res
        if name == "classes_and_methods":
            if root:
                return self._pairs([R.DEFINES.value], include_candidates, 5000, max_nodes, source_filter=root)
            sg = Subgraph(max_nodes)
            rows = []
            for c in self.s.nodes_by_type(self.gid, ["Class", "Interface", "Enum"], 400):
                ms = [o for e, o, _ in self.s.neighbors(self.gid, c["id"], "out", [R.DEFINES.value]) if o["type"] in CALLABLE_TYPES + ["Test"]]
                rows.append({"class": slim_node(c), "methods": [slim_node(m) for m in ms]})
                if sg.add_node(c):
                    for e, o, _ in self.s.neighbors(self.gid, c["id"], "out", [R.DEFINES.value]):
                        if sg.add_node(o):
                            sg.add_edge(e)
            return sg.to_dict(rows=rows)
        if name == "call_graph":
            if root:
                return self._directional(root, "both", [R.CALLS.value], depth, include_candidates)
            return self._pairs([R.CALLS.value], include_candidates, 3000, max_nodes)
        if name == "sql_and_tables":
            return self._pairs([R.EXECUTES.value, R.READS.value, R.WRITES.value], include_candidates, 4000, max_nodes)
        if name == "tables_and_columns":
            return self._pairs([R.HAS_COLUMN.value, R.FOREIGN_KEY.value, R.MAPS_TO.value], include_candidates, 4000, max_nodes)
        if name == "tests_and_targets":
            return self._pairs([R.TESTS.value], include_candidates, 4000, max_nodes)
        if name == "inheritance":
            return self._pairs([R.EXTENDS.value, R.IMPLEMENTS.value, R.OVERRIDES.value], include_candidates, 3000, max_nodes)
        if name == "dependencies_of":
            if not root:
                raise ValueError("root is required")
            return self.dependencies_of(root, depth, include_candidates)
        if name == "impact_of":
            if not root:
                raise ValueError("root is required")
            return self.impact_of(root, depth, include_candidates)
        if name == "api_to_database_path":
            ids = [root] if root else [a["id"] for a in self.s.nodes_by_type(self.gid, ["RestApi"], 400)]
            return self.api_to_database(ids, include_candidates, max_nodes=max_nodes)
        if name == "cross_language":
            return self.view_cross_language(max_nodes)
        raise ValueError(f"unknown query '{name}'")

    def schema(self, table_id: str) -> dict:
        cols = [slim_node(o) for e, o, _ in self.s.neighbors(self.gid, table_id, "out", [R.HAS_COLUMN.value])]
        fks_out = [{"edge": slim_edge(e), "table": slim_node(o)} for e, o, _ in self.s.neighbors(self.gid, table_id, "out", [R.FOREIGN_KEY.value])]
        fks_in = [{"edge": slim_edge(e), "table": slim_node(o)} for e, o, _ in self.s.neighbors(self.gid, table_id, "in", [R.FOREIGN_KEY.value])]
        models = [slim_node(o) for e, o, _ in self.s.neighbors(self.gid, table_id, "in", [R.MAPS_TO.value])]
        return {"columns": cols, "references": fks_out, "referenced_by": fks_in, "mapped_by": models}

    def summary(self) -> dict:
        meta = self.s.get_meta(self.gid) or {}
        hubs = []
        calls = self.s.rels_by_type(self.gid, [R.CALLS.value], limit=100000)
        indeg: dict[str, int] = {}
        for e in calls:
            indeg[e["target"]] = indeg.get(e["target"], 0) + 1
        for nid, d in sorted(indeg.items(), key=lambda x: -x[1])[:10]:
            n = self.node(nid)
            if n:
                hubs.append({"node": slim_node(n), "callers": d})
        return {"meta": meta, "report": meta.get("report", {}),
                "apis": [slim_node(n) for n in self.s.nodes_by_type(self.gid, ["RestApi"], 50)],
                "tables": [slim_node(n) for n in self.s.nodes_by_type(self.gid, ["Table"], 50)],
                "most_called": hubs}

    def schema_vocabulary(self) -> dict:
        from ..kg.model import ALLOWED_ENDPOINTS, RELATIONSHIP_MEANINGS
        rep = (self.s.get_meta(self.gid) or {}).get("report", {})
        return {"entity_types": rep.get("entity_counts", {}),
                "relationship_types": {k: {"meaning": RELATIONSHIP_MEANINGS.get(k.value, ""),
                                           "from": sorted(x.value for x in v[0]), "to": sorted(x.value for x in v[1]),
                                           "count": rep.get("relationship_counts", {}).get(k.value, {})}
                                       for k, v in ALLOWED_ENDPOINTS.items()},
                "statuses": ["VERIFIED", "CANDIDATE"],
                "named_queries": NAMED_QUERIES}


NAMED_QUERIES = ["apis_and_handlers", "classes_and_methods", "call_graph", "sql_and_tables", "tables_and_columns",
                 "tests_and_targets", "inheritance", "dependencies_of", "impact_of", "api_to_database_path",
                 "cross_language"]
