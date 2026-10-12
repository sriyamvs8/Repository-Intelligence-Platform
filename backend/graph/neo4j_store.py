"""Neo4j backend. Every query is a reviewed, PARAMETERISED template (no string-built user input).
Dynamic labels / relationship types are taken only from the canonical enums (allowlist).

Schema:
  (:RKGGraph {graph_id, meta_json, diagnostics_json, rejected_json})
  (:Entity:<Type> {uid, graph_id, id, type, name, qualified_name, file, line, end_line, language, props_json})
  (a)-[:<REL_TYPE> {uid, graph_id, id, type, status, source_kind, evidence, file, line, reason,
                    confidence, occurrences, props_json}]->(b)
"""
from __future__ import annotations

import json
from typing import Iterable

from ..kg.model import EntityType, RelType
from .store import GraphStore, rank_match

NODE_BASE = ("id", "type", "name", "qualified_name", "file", "line", "end_line", "language")
REL_BASE = ("id", "type", "source", "target", "status", "source_kind", "evidence", "file", "line", "reason",
            "confidence", "occurrences")
ENTITY_LABELS = {e.value for e in EntityType}
REL_TYPES = {r.value for r in RelType}
BATCH = 2000


# extra properties promoted to real Neo4j properties so that (LLM-generated) Cypher can filter on them
PROMOTED = ("http_method", "path", "normalized_path", "operation", "sql", "data_type", "primary_key", "nullable",
            "signature", "class", "is_test_code", "documented", "return_type", "table", "parse_status",
            "defined_in_schema", "kind", "spring_data_repository", "operation_id", "summary")


def _node_row(gid: str, n: dict) -> dict:
    base = {k: n.get(k) for k in NODE_BASE}
    extra = {k: v for k, v in n.items() if k not in NODE_BASE}
    promoted = {k: (v[:2000] if isinstance(v, str) else v) for k, v in extra.items()
                if k in PROMOTED and isinstance(v, (str, int, float, bool))}
    return {"uid": f"{gid}|{n['id']}", "graph_id": gid, **base, **promoted, "props_json": json.dumps(extra, default=str)}


def _rel_row(gid: str, r: dict) -> dict:
    base = {k: r.get(k) for k in REL_BASE if k not in ("source", "target")}
    extra = {k: v for k, v in r.items() if k not in REL_BASE}
    return {"uid": f"{gid}|{r['id']}", "s": f"{gid}|{r['source']}", "t": f"{gid}|{r['target']}",
            "props": {**base, "graph_id": gid, "uid": f"{gid}|{r['id']}", "props_json": json.dumps(extra, default=str)}}


def _node_out(rec) -> dict:
    d = dict(rec)
    extra = json.loads(d.pop("props_json", None) or "{}")
    for k in ("uid", "graph_id") + PROMOTED:
        d.pop(k, None)
    return {**{k: d.get(k) for k in NODE_BASE}, **extra}


def _rel_out(rec, source: str, target: str) -> dict:
    d = dict(rec)
    extra = json.loads(d.pop("props_json", None) or "{}")
    for k in ("uid", "graph_id"):
        d.pop(k, None)
    return {**{k: d.get(k) for k in REL_BASE if k not in ("source", "target")}, "source": source, "target": target, **extra}


class Neo4jStore(GraphStore):
    backend_name = "neo4j"
    supports_cypher = True

    def __init__(self, settings) -> None:
        from neo4j import GraphDatabase
        self.db = settings.neo4j_database
        self.driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password),
                                           max_connection_pool_size=20, connection_timeout=10)
        self.driver.verify_connectivity()
        with self.driver.session(database=self.db) as s:
            s.run("CREATE CONSTRAINT rkg_entity_uid IF NOT EXISTS FOR (n:Entity) REQUIRE n.uid IS UNIQUE").consume()
            s.run("CREATE CONSTRAINT rkg_graph_id IF NOT EXISTS FOR (g:RKGGraph) REQUIRE g.graph_id IS UNIQUE").consume()
            s.run("CREATE INDEX rkg_entity_graph IF NOT EXISTS FOR (n:Entity) ON (n.graph_id, n.type)").consume()
            s.run("CREATE INDEX rkg_entity_name IF NOT EXISTS FOR (n:Entity) ON (n.graph_id, n.name)").consume()

    # ------------------------------------------------------------------ helpers
    def _read(self, query: str, **params) -> list[dict]:
        """Read-only managed transaction with a server-side timeout."""
        from neo4j import unit_of_work

        @unit_of_work(timeout=20)
        def work(tx):
            return tx.run(query, params).data()

        with self.driver.session(database=self.db, default_access_mode="READ") as s:
            return s.execute_read(work)

    def _write(self, query: str, **params) -> None:
        with self.driver.session(database=self.db) as s:
            s.execute_write(lambda tx: tx.run(query, params).consume())

    def run_cypher(self, query: str, params: dict) -> list[dict]:
        """Execute an already-validated read-only query: READ access mode, managed read transaction,
        10 s server-side timeout, at most 200 rows."""
        from neo4j import unit_of_work

        @unit_of_work(timeout=10)
        def work(tx):
            res = tx.run(query, params)
            out = []
            for rec in res:
                out.append(rec.data())
                if len(out) >= 200:
                    break
            return out

        with self.driver.session(database=self.db, default_access_mode="READ") as s:
            return s.execute_read(work)

    # ------------------------------------------------------------------ writes
    def save_graph(self, gid, meta, report, entities, relationships, diagnostics, rejected) -> None:
        self.delete_graph(gid)
        for i in range(0, len(entities), BATCH):
            rows = [_node_row(gid, n) for n in entities[i:i + BATCH]]
            self._write("UNWIND $rows AS row CREATE (n:Entity) SET n = row", rows=rows)
        for label in sorted({n["type"] for n in entities} & ENTITY_LABELS):   # allowlisted labels only
            self._write(f"MATCH (n:Entity {{graph_id: $g, type: $t}}) SET n:`{label}`", g=gid, t=label)
        by_type: dict[str, list] = {}
        for r in relationships:
            if r["type"] in REL_TYPES:
                by_type.setdefault(r["type"], []).append(_rel_row(gid, r))
        for rtype, rows in by_type.items():   # allowlisted relationship types only
            for i in range(0, len(rows), BATCH):
                self._write(f"UNWIND $rows AS row MATCH (a:Entity {{uid: row.s}}) MATCH (b:Entity {{uid: row.t}}) "
                            f"CREATE (a)-[r:`{rtype}`]->(b) SET r = row.props", rows=rows[i:i + BATCH])
        self._write("MERGE (g:RKGGraph {graph_id: $g}) SET g.meta_json = $meta, g.diagnostics_json = $d, "
                    "g.rejected_json = $rj, g.analyzed_at = $at",
                    g=gid, meta=json.dumps({**meta, "report": report}, default=str),
                    d=json.dumps(diagnostics[:5000], default=str), rj=json.dumps(rejected[:5000], default=str),
                    at=meta.get("analyzed_at", ""))

    def delete_graph(self, gid: str) -> bool:
        existed = bool(self._read("MATCH (g:RKGGraph {graph_id: $g}) RETURN g.graph_id AS id", g=gid))
        while True:
            with self.driver.session(database=self.db) as s:
                n = s.execute_write(lambda tx: tx.run(
                    "MATCH (n:Entity {graph_id: $g}) WITH n LIMIT 5000 DETACH DELETE n RETURN count(*) AS c",
                    g=gid).single()["c"])
            if n == 0:
                break
        self._write("MATCH (g:RKGGraph {graph_id: $g}) DELETE g", g=gid)
        return existed

    # ------------------------------------------------------------------ reads
    def list_graphs(self) -> list[dict]:
        rows = self._read("MATCH (g:RKGGraph) RETURN g.meta_json AS m ORDER BY g.analyzed_at DESC")
        return [json.loads(r["m"]) for r in rows]

    def get_meta(self, gid):
        rows = self._read("MATCH (g:RKGGraph {graph_id: $g}) RETURN g.meta_json AS m", g=gid)
        return json.loads(rows[0]["m"]) if rows else None

    def get_node(self, gid, nid):
        rows = self._read("MATCH (n:Entity {uid: $u}) RETURN properties(n) AS n", u=f"{gid}|{nid}")
        return _node_out(rows[0]["n"]) if rows else None

    def get_nodes(self, gid, ids):
        uids = [f"{gid}|{i}" for i in ids]
        rows = self._read("MATCH (n:Entity) WHERE n.uid IN $u RETURN properties(n) AS n", u=uids)
        found = {r["n"]["id"]: _node_out(r["n"]) for r in rows}
        return [found[i] for i in ids if i in found]

    def search(self, gid, q, types=None, limit=20):
        if not q:
            return []
        rows = self._read(
            "MATCH (n:Entity {graph_id: $g}) WHERE ($types IS NULL OR n.type IN $types) AND "
            "(n.id = $raw OR toLower(n.name) CONTAINS $q OR toLower(coalesce(n.qualified_name,'')) CONTAINS $q "
            " OR toLower(coalesce(n.file,'')) CONTAINS $q) RETURN properties(n) AS n LIMIT 2000",
            g=gid, q=q.lower(), raw=q, types=types or None)
        nodes = [_node_out(r["n"]) for r in rows]
        scored = sorted(((rank_match(n, q), len(n.get("qualified_name") or ""), n["id"], n) for n in nodes),
                        key=lambda x: x[:3])
        return [s[3] for s in scored if s[0] < 99][:limit]

    def neighbors(self, gid, nid, direction="both", rel_types=None, statuses=None, limit=200):
        out = []
        params = dict(u=f"{gid}|{nid}", types=rel_types or None, st=statuses or None, lim=5000)
        if direction in ("out", "both"):
            for r in self._read("MATCH (n:Entity {uid: $u})-[r]->(m:Entity) WHERE ($types IS NULL OR r.type IN $types) "
                                "AND ($st IS NULL OR r.status IN $st) RETURN properties(r) AS r, properties(m) AS m LIMIT $lim", **params):
                m = _node_out(r["m"])
                out.append((_rel_out(r["r"], nid, m["id"]), m, "out"))
        if direction in ("in", "both"):
            for r in self._read("MATCH (n:Entity {uid: $u})<-[r]-(m:Entity) WHERE ($types IS NULL OR r.type IN $types) "
                                "AND ($st IS NULL OR r.status IN $st) RETURN properties(r) AS r, properties(m) AS m LIMIT $lim", **params):
                m = _node_out(r["m"])
                out.append((_rel_out(r["r"], m["id"], nid), m, "in"))
        out.sort(key=lambda x: (x[0]["type"], x[1].get("name") or "", x[1]["id"]))
        return out[:limit]

    def nodes_by_type(self, gid, types, limit=500):
        rows = self._read("MATCH (n:Entity {graph_id: $g}) WHERE n.type IN $types RETURN properties(n) AS n",
                          g=gid, types=list(types))
        res = sorted((_node_out(r["n"]) for r in rows), key=lambda n: (n["type"], n.get("qualified_name") or n["name"]))
        return res[:limit]

    def rels_by_type(self, gid, types, statuses=None, limit=2000):
        rows = self._read("MATCH (a:Entity {graph_id: $g})-[r]->(b:Entity) WHERE r.type IN $types AND "
                          "($st IS NULL OR r.status IN $st) RETURN properties(r) AS r, a.id AS s, b.id AS t",
                          g=gid, types=list(types), st=statuses or None)
        res = sorted((_rel_out(r["r"], r["s"], r["t"]) for r in rows), key=lambda r: (r["type"], r["source"], r["target"]))
        return res[:limit]

    def degree(self, gid, nid):
        rows = self._read("MATCH (n:Entity {uid: $u}) RETURN COUNT { (n)--() } AS d", u=f"{gid}|{nid}")
        return rows[0]["d"] if rows else 0

    def diagnostics(self, gid):
        rows = self._read("MATCH (g:RKGGraph {graph_id: $g}) RETURN g.diagnostics_json AS d, g.rejected_json AS r", g=gid)
        if not rows:
            return {}
        return {"diagnostics": json.loads(rows[0]["d"] or "[]"), "rejected": json.loads(rows[0]["r"] or "[]")}

    def export(self, gid):
        meta = self.get_meta(gid)
        if meta is None:
            return {}
        nodes = [_node_out(r["n"]) for r in self._read("MATCH (n:Entity {graph_id: $g}) RETURN properties(n) AS n", g=gid)]
        rels = [_rel_out(r["r"], r["s"], r["t"]) for r in self._read(
            "MATCH (a:Entity {graph_id: $g})-[r]->(b:Entity) RETURN properties(r) AS r, a.id AS s, b.id AS t", g=gid)]
        return {"meta": meta, "entities": nodes, "relationships": rels}

    def health(self):
        try:
            self.driver.verify_connectivity()
            return {"backend": "neo4j", "ok": True, "database": self.db}
        except Exception as exc:
            return {"backend": "neo4j", "ok": False, "error": str(exc)[:200]}

    def close(self):
        self.driver.close()
