"""Graph storage abstraction + in-memory backend (persisted to gzip JSON so restarts are safe).

All query logic (backend/graph/queries.py) is written against this small interface, so the
in-memory and Neo4j backends return identical results for the same graph (parity)."""
from __future__ import annotations

import gzip
import json
import os
import re
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from pathlib import Path
from typing import Iterable

GID_RE = re.compile(r"^g[0-9a-f]{11}$")


def rank_match(node: dict, q: str) -> int:
    """Lower is better. Exact id/qualified/name matches rank above substrings."""
    ql = q.lower()
    name = (node.get("name") or "").lower()
    qual = (node.get("qualified_name") or "").lower()
    nid = node.get("id", "").lower()
    if nid == ql:
        return 0
    if qual == ql:
        return 1
    if node.get("name") == q:
        return 2
    if name == ql:
        return 3
    qnp, lnp = qual.split("(")[0], ql.split("(")[0]
    if qnp == lnp:
        return 1
    if qual.endswith("." + ql) or qual.endswith("/" + ql) or qnp.endswith("." + lnp) or qnp.endswith("/" + lnp):
        return 4
    if name.split("(")[0] == ql.split("(")[0]:
        return 5
    if name.startswith(ql):
        return 6
    if ql in name:
        return 7
    if ql in qual:
        return 8
    if ql in (node.get("file") or "").lower():
        return 9
    return 99


class GraphStore(ABC):
    backend_name = "abstract"

    @abstractmethod
    def save_graph(self, gid: str, meta: dict, report: dict, entities: list[dict], relationships: list[dict],
                   diagnostics: list[dict], rejected: list[dict]) -> None: ...

    @abstractmethod
    def list_graphs(self) -> list[dict]: ...

    @abstractmethod
    def get_meta(self, gid: str) -> dict | None: ...

    @abstractmethod
    def delete_graph(self, gid: str) -> bool: ...

    @abstractmethod
    def get_node(self, gid: str, nid: str) -> dict | None: ...

    @abstractmethod
    def get_nodes(self, gid: str, ids: Iterable[str]) -> list[dict]: ...

    @abstractmethod
    def search(self, gid: str, q: str, types: list[str] | None = None, limit: int = 20) -> list[dict]: ...

    @abstractmethod
    def neighbors(self, gid: str, nid: str, direction: str = "both", rel_types: list[str] | None = None,
                  statuses: list[str] | None = None, limit: int = 200) -> list[tuple[dict, dict, str]]:
        """Return [(edge, other_node, 'out'|'in')]."""

    @abstractmethod
    def nodes_by_type(self, gid: str, types: list[str], limit: int = 500) -> list[dict]: ...

    @abstractmethod
    def rels_by_type(self, gid: str, types: list[str], statuses: list[str] | None = None, limit: int = 2000) -> list[dict]: ...

    @abstractmethod
    def degree(self, gid: str, nid: str) -> int: ...

    @abstractmethod
    def diagnostics(self, gid: str) -> dict: ...

    @abstractmethod
    def export(self, gid: str) -> dict: ...

    def health(self) -> dict:
        return {"backend": self.backend_name, "ok": True}

    def close(self) -> None:
        pass


class _MemGraph:
    __slots__ = ("nodes", "rels", "out", "inc", "by_type", "rels_by_type", "diagnostics", "rejected", "meta")

    def __init__(self, data: dict):
        self.meta = data["meta"]
        self.nodes: dict[str, dict] = {n["id"]: n for n in data["entities"]}
        self.rels: dict[str, dict] = {r["id"]: r for r in data["relationships"]}
        self.out: dict[str, list[str]] = {}
        self.inc: dict[str, list[str]] = {}
        self.by_type: dict[str, list[str]] = {}
        self.rels_by_type: dict[str, list[str]] = {}
        for r in self.rels.values():
            self.out.setdefault(r["source"], []).append(r["id"])
            self.inc.setdefault(r["target"], []).append(r["id"])
            self.rels_by_type.setdefault(r["type"], []).append(r["id"])
        for n in self.nodes.values():
            self.by_type.setdefault(n["type"], []).append(n["id"])
        self.diagnostics = data.get("diagnostics", [])
        self.rejected = data.get("rejected", [])


class InMemoryStore(GraphStore):
    backend_name = "in_memory"

    def __init__(self, data_dir: Path, max_loaded: int = 6) -> None:
        self.dir = Path(data_dir) / "graphs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.lock = threading.RLock()
        self.loaded: OrderedDict[str, _MemGraph] = OrderedDict()
        self.max_loaded = max_loaded
        self.index: dict[str, dict] = {}
        if self.index_path.exists():
            try:
                self.index = json.loads(self.index_path.read_text("utf-8"))
            except Exception:
                self.index = {}

    def _path(self, gid: str) -> Path:
        if not GID_RE.match(gid):
            raise KeyError(gid)
        return self.dir / f"{gid}.json.gz"

    def _write_index(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index, indent=1), "utf-8")
        os.replace(tmp, self.index_path)

    def _graph(self, gid: str) -> _MemGraph | None:
        with self.lock:
            g = self.loaded.get(gid)
            if g is not None:
                self.loaded.move_to_end(gid)
                return g
            try:
                p = self._path(gid)
            except KeyError:
                return None
            if not p.exists():
                return None
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                g = _MemGraph(json.load(fh))
            self.loaded[gid] = g
            while len(self.loaded) > self.max_loaded:
                self.loaded.popitem(last=False)
            return g

    def save_graph(self, gid, meta, report, entities, relationships, diagnostics, rejected) -> None:
        data = {"meta": {**meta, "report": report}, "entities": entities, "relationships": relationships,
                "diagnostics": diagnostics, "rejected": rejected}
        p = self._path(gid)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, p)
        with self.lock:
            self.loaded.pop(gid, None)
            self.index[gid] = {**meta, "report": report}
            self._write_index()

    def list_graphs(self) -> list[dict]:
        with self.lock:
            return sorted(self.index.values(), key=lambda m: m.get("analyzed_at", ""), reverse=True)

    def get_meta(self, gid: str) -> dict | None:
        return self.index.get(gid)

    def delete_graph(self, gid: str) -> bool:
        with self.lock:
            existed = gid in self.index
            self.index.pop(gid, None)
            self.loaded.pop(gid, None)
            self._write_index()
            try:
                self._path(gid).unlink(missing_ok=True)
            except KeyError:
                pass
            return existed

    def get_node(self, gid, nid):
        g = self._graph(gid)
        return g.nodes.get(nid) if g else None

    def get_nodes(self, gid, ids):
        g = self._graph(gid)
        return [g.nodes[i] for i in ids if i in g.nodes] if g else []

    def search(self, gid, q, types=None, limit=20):
        g = self._graph(gid)
        if not g or not q:
            return []
        ts = set(types or [])
        scored = []
        for n in g.nodes.values():
            if ts and n["type"] not in ts:
                continue
            r = rank_match(n, q)
            if r < 99:
                scored.append((r, len(n.get("qualified_name") or ""), n["id"], n))
        scored.sort(key=lambda x: x[:3])
        return [s[3] for s in scored[:limit]]

    def neighbors(self, gid, nid, direction="both", rel_types=None, statuses=None, limit=200):
        g = self._graph(gid)
        if not g:
            return []
        rts, sts = set(rel_types or []), set(statuses or [])
        out = []
        if direction in ("out", "both"):
            for rid in g.out.get(nid, []):
                r = g.rels[rid]
                if (not rts or r["type"] in rts) and (not sts or r["status"] in sts):
                    out.append((r, g.nodes[r["target"]], "out"))
        if direction in ("in", "both"):
            for rid in g.inc.get(nid, []):
                r = g.rels[rid]
                if (not rts or r["type"] in rts) and (not sts or r["status"] in sts):
                    out.append((r, g.nodes[r["source"]], "in"))
        out.sort(key=lambda x: (x[0]["type"], x[1].get("name") or "", x[1]["id"]))
        return out[:limit]

    def nodes_by_type(self, gid, types, limit=500):
        g = self._graph(gid)
        if not g:
            return []
        res = []
        for t in types:
            res.extend(g.nodes[i] for i in g.by_type.get(t, []))
        res.sort(key=lambda n: (n["type"], n.get("qualified_name") or n["name"]))
        return res[:limit]

    def rels_by_type(self, gid, types, statuses=None, limit=2000):
        g = self._graph(gid)
        if not g:
            return []
        sts = set(statuses or [])
        res = []
        for t in types:
            res.extend(g.rels[i] for i in g.rels_by_type.get(t, []) if not sts or g.rels[i]["status"] in sts)
        res.sort(key=lambda r: (r["type"], r["source"], r["target"]))
        return res[:limit]

    def degree(self, gid, nid):
        g = self._graph(gid)
        return (len(g.out.get(nid, [])) + len(g.inc.get(nid, []))) if g else 0

    def diagnostics(self, gid):
        g = self._graph(gid)
        return {"diagnostics": g.diagnostics, "rejected": g.rejected} if g else {}

    def export(self, gid):
        g = self._graph(gid)
        if not g:
            return {}
        return {"meta": g.meta, "entities": list(g.nodes.values()), "relationships": list(g.rels.values())}

    def health(self):
        return {"backend": self.backend_name, "ok": True, "graphs": len(self.index), "data_dir": str(self.dir)}


_store: GraphStore | None = None
_store_lock = threading.Lock()


def get_store() -> GraphStore:
    global _store
    with _store_lock:
        if _store is None:
            from ..config import settings
            if settings.graph_backend.lower() == "neo4j":
                from .neo4j_store import Neo4jStore
                _store = Neo4jStore(settings)
            else:
                _store = InMemoryStore(settings.data_dir)
        return _store


def set_store(store: GraphStore) -> None:
    global _store
    _store = store
