"""Graph-grounded question answering:
question -> QueryPlan (Gemini or rules) -> validation -> entity resolution -> allowlisted read-only
query -> evidence packaging -> deterministic answer (+ optional grounded LLM explanation)."""
from __future__ import annotations

import logging
import re
import time
import uuid

from ..config import settings
from ..graph.queries import CALLABLE_TYPES, GraphQueries, slim_edge, slim_node
from ..graph.store import GraphStore
from ..kg.model import normalize_route
from .cypher import (CypherRejected, entity_ids_in_rows, generate_cypher, rows_to_text, scope_rows, template_for,
                     validate_cypher)
from .gemini import explain_with_gemini, grounded, plan_with_gemini
from .plan import EntityRef, Intent, QueryPlan, candidate_terms, rule_plan

log = logging.getLogger("rkg.nlp")

PREFERRED_TYPES = {
    Intent.CALLERS: CALLABLE_TYPES + ["Class", "Interface", "RestApi", "Table"],
    Intent.CALLEES: CALLABLE_TYPES + ["RestApi", "Test"],
    Intent.LIST_METHODS: ["Class", "Interface", "Enum", "File"],
    Intent.API_HANDLER: ["RestApi"],
    Intent.API_TO_DATA: ["RestApi"],
    Intent.SQL_TABLES: CALLABLE_TYPES + ["SqlQuery", "RestApi", "Class", "Interface"],
    Intent.SCHEMA: ["Table"],
    Intent.TABLE_USAGE: ["Table"],
    Intent.DEPENDENCIES: CALLABLE_TYPES + ["Class", "Interface", "File", "RestApi"],
    Intent.IMPACT: CALLABLE_TYPES + ["Class", "Interface", "Table", "SqlQuery", "RestApi", "Column"],
    Intent.TEST_COVERAGE: CALLABLE_TYPES + ["Class", "Interface", "RestApi"],
    Intent.LOCATE_SYMBOL: None,
    Intent.PATH: None,
}
# complex analyses answered by reviewed, bounded traversals even when Gemini->Cypher is available
TEMPLATE_FIRST = {Intent.IMPACT, Intent.API_TO_DATA, Intent.TEST_COVERAGE, Intent.PATH, Intent.TABLE_USAGE,
                  Intent.DEPENDENCIES, Intent.SUMMARY, Intent.LIST_APIS, Intent.LIST_TABLES}
PRONOUN_RE = re.compile(r"\b(it|its|this|that|them|those|these|same)\b", re.I)


def _loc(n: dict) -> str:
    if n.get("file"):
        return f"{n['file']}:{n['line']}" if n.get("line") else n["file"]
    return ""


def _label(n: dict) -> str:
    if n["type"] in ("Method", "Function", "Constructor", "Test"):
        q = n.get("qualified_name") or n["name"]
        short = q.split("(")[0].split(".")
        return ".".join(short[-2:]) + "()"
    return n.get("name") or n["id"]


def _fact(edge: dict, s: dict, t: dict) -> dict:
    return {"subject": _label(s), "subject_id": s["id"], "subject_type": s["type"], "relationship": edge["type"],
            "object": _label(t), "object_id": t["id"], "object_type": t["type"], "source_file": edge.get("file"),
            "line": edge.get("line"), "evidence": edge.get("evidence"), "validation_status": edge.get("status"),
            "reason": edge.get("reason")}


class AskEngine:
    def __init__(self, store: GraphStore, gid: str) -> None:
        self.store = store
        self.gid = gid
        self.q = GraphQueries(store, gid)

    # ------------------------------------------------------------------ entity resolution
    def resolve(self, ref: EntityRef | None, preferred: list[str] | None, extra_terms: list[str] | None = None
                ) -> tuple[list[dict], list[dict]]:
        """Return (resolved nodes, ambiguous candidates)."""
        terms = ([ref.text] if ref else []) + [t for t in (extra_terms or []) if not ref or t != ref.text]
        http_method = ref.http_method if ref else None
        best_cands: list[dict] = []
        for term in terms[:8]:
            if term.startswith("/") or (ref and ref.kind == "RestApi" and term == ref.text):
                apis = self._resolve_route(term, http_method)
                if apis:
                    return apis, []
                continue
            types = [ref.kind] if (ref and ref.kind and term == ref.text) else preferred
            res = self.store.search(self.gid, term, types, 30)
            if not res and types:
                res = self.store.search(self.gid, term, None, 30)
            if not res:
                continue
            from ..graph.store import rank_match
            ranked = sorted(res, key=lambda n: rank_match(n, term))
            top = rank_match(ranked[0], term)
            if top > 6:          # only substring matches: weak; try next term but remember
                best_cands = best_cands or ranked[:8]
                continue
            same = [n for n in ranked if rank_match(n, term) == top]
            # a class and its constructor share a name: the type wins
            type_names = {n.get("qualified_name") for n in same if n["type"] in ("Class", "Interface", "Enum")}
            same = [n for n in same if not (n["type"] == "Constructor" and n.get("class") in type_names)]
            # a source file named after the type (Foo.java / foo.py) is the same concept
            if any(n["type"] != "File" for n in same):
                same = [n for n in same if n["type"] != "File"] or same
            if preferred:
                pref = [n for n in same if n["type"] in preferred]
                same = pref or same
            # overloads of one method / one API path with several verbs count as one entity group
            groups = {(n["type"], (n.get("qualified_name") or n["name"]).split("(")[0]) for n in same}
            if len(groups) == 1 or len(same) == 1:
                return same[:10], []
            # prefer non-test code when ambiguous between test and production code
            prod = [n for n in same if not n.get("is_test_code")]
            groups = {(n["type"], (n.get("qualified_name") or n["name"]).split("(")[0]) for n in prod}
            if prod and len(groups) == 1:
                return prod[:10], []
            return [], same[:8]
        return [], best_cands

    def _resolve_route(self, path: str, method: str | None) -> list[dict]:
        norm = normalize_route(path)
        apis = self.store.nodes_by_type(self.gid, ["RestApi"], 5000)
        exact = [a for a in apis if a.get("normalized_path") == norm]
        if not exact:
            exact = [a for a in apis if a.get("normalized_path", "").endswith(norm) and norm != "/"]
        if method:
            m = [a for a in exact if a.get("http_method") in (method, "ANY")]
            exact = m or exact
        return exact[:10]

    # ------------------------------------------------------------------ main entry
    def ask(self, question: str, context_entity_id: str | None = None, include_candidates: bool | None = None,
            max_hops: int | None = None) -> dict:
        t0 = time.time()
        qid = uuid.uuid4().hex[:12]
        question = (question or "").strip()[:1000]
        rplan, terms, tterms = rule_plan(question)
        if include_candidates is not None:
            rplan.include_candidates = include_candidates
        plan, planner = rplan, "rules"
        cypher_note = None
        # ---- 1. Gemini text-to-Cypher on Neo4j (validated, read-only), when configured
        mode = settings.nlp_mode
        if (settings.gemini_enabled and self.store.supports_cypher and mode in ("hybrid", "cypher")
                and (mode == "cypher" or rplan.intent not in TEMPLATE_FIRST)):
            cres = self._cypher_path(question, rplan, terms, context_entity_id)
            if cres and cres.get("status") == "ANSWERED":
                return self._finish(cres, question, rplan, "gemini → cypher", t0, qid, explain=True)
            cypher_note = (cres or {}).get("note")
        # ---- 2. structured plan (Gemini planner on in-memory backend, else rules) + reviewed traversals
        elif settings.gemini_enabled:
            g = plan_with_gemini(question, self.q.schema_vocabulary(), context_entity_id)
            if g is not None:
                plan, planner = g, "gemini"
        if include_candidates is not None:
            plan.include_candidates = include_candidates
        if max_hops is not None:
            plan.max_hops = max(1, min(settings.nlp_max_hops, int(max_hops)))
        plan.max_hops = min(plan.max_hops, settings.nlp_max_hops)
        resp = self._execute(question, plan, terms, tterms, context_entity_id)
        # Fallback: if Gemini's plan found nothing but the rule plan differs, try rules.
        if planner == "gemini" and resp["status"] in ("NOT_FOUND", "UNSUPPORTED") and rplan.intent != plan.intent:
            alt = self._execute(question, rplan, terms, tterms, context_entity_id)
            if alt["status"] == "ANSWERED":
                resp, plan, planner = alt, rplan, "rules (gemini fallback)"
        if cypher_note:
            resp["limitations"].append(cypher_note)
            planner = planner + " (cypher fallback)"
        ids = [e["id"] for e in resp.get("resolved_entities", [])]
        resp["cypher"] = template_for(plan.intent.value, ids, plan.include_candidates)
        if resp["cypher"]:
            resp["cypher"]["executed"] = False
            resp["cypher"]["note"] = (resp["cypher"]["note"] + " " if resp["cypher"]["note"] else "") + (
                "Answer computed by the equivalent reviewed traversal." )
        return self._finish(resp, question, plan, planner, t0, qid, explain=True)

    def _finish(self, resp: dict, question: str, plan: QueryPlan, planner: str, t0: float, qid: str,
                explain: bool) -> dict:
        resp["answer_source"] = "graph"
        if explain and resp["status"] == "ANSWERED" and resp["facts"]:
            llm = explain_with_gemini(question, resp["facts"], resp["answer"])
            if llm and grounded(llm, resp["facts"]):
                resp["explanation"] = llm
                resp["answer_source"] = "graph+llm"
            elif llm:
                resp["limitations"].append("LLM explanation discarded: it mentioned entities not present in the graph results.")
        resp.setdefault("cypher", None)
        resp.update({"question_id": qid, "question": question, "planner": planner,
                     "query_plan": plan.model_dump(mode="json"), "elapsed_ms": int((time.time() - t0) * 1000)})
        log.info("ask qid=%s intent=%s status=%s planner=%s facts=%d ms=%d", qid, plan.intent.value, resp["status"],
                 planner, len(resp["facts"]), resp["elapsed_ms"])
        return resp

    # ------------------------------------------------------------------ Gemini text-to-Cypher
    def _cypher_path(self, question: str, plan: QueryPlan, terms: list[str], ctx_id: str | None) -> dict | None:
        hints, cands = self.resolve(plan.entity, PREFERRED_TYPES.get(plan.intent), terms) if (plan.entity or terms) else ([], [])
        hints = [slim_node(n) for n in (hints or cands)[:8]]
        ctx = slim_node(self.store.get_node(self.gid, ctx_id)) if ctx_id and self.store.get_node(self.gid, ctx_id) else None
        raw = generate_cypher(question, hints, ctx)
        if raw is None:
            return {"note": "Gemini unavailable; answered with reviewed traversal."}
        attempts = []
        for attempt in range(2):
            try:
                q, notes = validate_cypher(raw)
            except CypherRejected as exc:
                attempts.append({"query": raw, "error": str(exc)})
                if str(exc) == "UNSUPPORTED":
                    return {"note": "Gemini could not express this as Cypher; answered with reviewed traversal."}
                raw = generate_cypher(question, hints, ctx, previous=raw, error=str(exc)) if attempt == 0 else None
                if raw is None:
                    break
                continue
            try:
                rows = scope_rows(self.store.run_cypher(q, {"gid": self.gid}), self.gid)
            except Exception as exc:     # syntax/semantic error reported by Neo4j -> one repair attempt
                attempts.append({"query": q, "error": str(exc)[:300]})
                raw = generate_cypher(question, hints, ctx, previous=q, error=str(exc)[:300]) if attempt == 0 else None
                if raw is None:
                    break
                continue
            if not rows:
                return {"note": "Gemini-generated Cypher returned no rows; answered with reviewed traversal instead.",
                        "cypher": {"query": q, "notes": notes}}
            ids = entity_ids_in_rows(rows)
            nodes = self.store.get_nodes(self.gid, ids[:80])
            idset = {n["id"] for n in nodes}
            edges = []
            for n in nodes:
                for e, o, _ in self.store.neighbors(self.gid, n["id"], "out", None, None, limit=200):
                    if o["id"] in idset and e["type"] not in ("CONTAINS",):
                        edges.append(e)
            facts = []
            for r in rows:
                desc = ", ".join(f"{k}={v}" for k, v in r.items() if not k.endswith("_id") and v not in (None, "", [])
                                 and k not in ("file", "line", "status"))
                facts.append({**r, "subject": desc[:240], "relationship": "", "object": "",
                              "source_file": r.get("file"), "line": r.get("line"), "validation_status": r.get("status")})
            res = self._result("ANSWERED", rows_to_text(rows), facts, graph=self._graph_of(nodes, edges),
                               resolved=hints, limitations=["Answer produced by Gemini-generated Cypher, validated "
                                                            "and executed read-only on Neo4j; facts are the raw rows."])
            res["cypher"] = {"query": q, "params": {"gid": self.gid}, "source": "Gemini-generated · validated · executed read-only",
                             "notes": notes, "repaired_from": attempts, "executed": True}
            return res
        return {"note": "Gemini-generated Cypher was rejected by the safety validator or failed in Neo4j "
                        f"({attempts[-1]['error'][:120] if attempts else 'unknown'}); answered with reviewed traversal."}

    def _result(self, status: str, answer: str, facts=None, entities=None, paths=None, graph=None,
                limitations=None, candidates=None, resolved=None) -> dict:
        return {"status": status, "answer": answer, "facts": facts or [], "entities": entities or [],
                "paths": paths or [], "graph": graph or {"nodes": [], "edges": []}, "limitations": limitations or [],
                "candidates": candidates or [], "resolved_entities": resolved or []}

    def _execute(self, question: str, plan: QueryPlan, terms: list[str], tterms: list[str],
                 ctx_id: str | None) -> dict:
        self._question = question
        it = plan.intent
        inc = plan.include_candidates
        if it == Intent.UNSUPPORTED:
            return self._result("UNSUPPORTED", "This question cannot be mapped to a repository-graph query. "
                                "Try asking where something is defined, who calls a method, which tables an endpoint "
                                "touches, what is impacted by a change, or which tests cover something.")
        if it == Intent.SUMMARY:
            return self._summary()
        if it == Intent.LIST_APIS:
            return self._list_apis(plan.limit)
        if it == Intent.LIST_TABLES:
            return self._list_tables(plan.limit)
        preferred = PREFERRED_TYPES.get(it)
        nodes, cands = self.resolve(plan.entity, preferred, terms)
        if not nodes and ctx_id and (PRONOUN_RE.search(question) or not plan.entity):
            n = self.store.get_node(self.gid, ctx_id)
            if n:
                nodes = [n]
        if not nodes:
            if cands:
                return self._result("CLARIFY", "More than one entity matches. Which one do you mean?",
                                    candidates=[slim_node(c) for c in cands])
            what = plan.entity.text if plan.entity else "the entity"
            return self._result("NOT_FOUND", f"No entity matching '{what}' exists in the analyzed graph. "
                                "It may be spelled differently, live in an unsupported language/framework, or be outside the repository.")
        resolved = [slim_node(n) for n in nodes]
        handler = getattr(self, f"_i_{it.value.lower()}", None)
        if it == Intent.PATH:
            tnodes, tc = self.resolve(plan.target, None, tterms)
            if not tnodes:
                return self._result("NOT_FOUND" if not tc else "CLARIFY",
                                    "The second entity of the path question was not found." if not tc else
                                    "The second entity is ambiguous.", candidates=[slim_node(c) for c in tc])
            return self._i_path(nodes[0], tnodes[0], plan, resolved)
        if handler is None:
            return self._i_locate_symbol(nodes, plan, resolved)
        return handler(nodes, plan, resolved)

    # ------------------------------------------------------------------ intent handlers
    def _graph_of(self, nodes: list[dict], edges: list[dict]) -> dict:
        return {"nodes": [slim_node(n) for n in {n["id"]: n for n in nodes}.values()],
                "edges": [slim_edge(e) for e in {e["id"]: e for e in edges}.values()]}

    def _collect(self, root_nodes: list[dict], direction: str, types: list[str], inc: bool,
                 other_filter=None) -> tuple[list[dict], list[dict], list[dict]]:
        facts, nodes, edges = [], list(root_nodes), []
        sts = ["VERIFIED", "CANDIDATE"] if inc else ["VERIFIED"]
        for rn in root_nodes:
            for e, o, d in self.store.neighbors(self.gid, rn["id"], direction, types, sts, limit=300):
                if other_filter and not other_filter(o):
                    continue
                s, t = (rn, o) if d == "out" else (o, rn)
                facts.append(_fact(e, s, t))
                nodes.append(o)
                edges.append(e)
        return facts, nodes, edges

    def _i_locate_symbol(self, nodes, plan, resolved):
        lines = []
        facts = []
        for n in nodes[:10]:
            parent = self.store.neighbors(self.gid, n["id"], "in", ["DEFINES"], limit=3)
            owner = f" (defined in {_label(parent[0][1])})" if parent else ""
            lines.append(f"- `{_label(n)}` — {n['type']} at {_loc(n) or 'unknown location'}{owner}")
            facts.append({"subject": _label(n), "subject_id": n["id"], "subject_type": n["type"],
                          "relationship": "LOCATED_AT", "object": _loc(n), "source_file": n.get("file"),
                          "line": n.get("line"), "validation_status": "VERIFIED"})
            if n["type"] == "RestApi":
                hs = self.store.neighbors(self.gid, n["id"], "out", ["HANDLED_BY"])
                for e, h, _ in hs:
                    lines.append(f"  - handled by `{_label(h)}` at {_loc(h)}")
                    facts.append(_fact(e, n, h))
        return self._result("ANSWERED", "Found:\n" + "\n".join(lines), facts, resolved, graph=self._graph_of(nodes, []),
                            resolved=resolved)

    def _i_list_methods(self, nodes, plan, resolved):
        facts, ns, es = self._collect(nodes[:1], "out", ["DEFINES"], True,
                                      lambda o: o["type"] in CALLABLE_TYPES + ["Test", "Class", "Interface", "Enum"])
        if not facts:
            return self._result("INSUFFICIENT_EVIDENCE", f"`{_label(nodes[0])}` has no extracted members.", resolved=resolved)
        lines = [f"- `{f['object']}` ({f['object_type']}) line {f['line'] or '?'}" for f in facts[:plan.limit]]
        return self._result("ANSWERED", f"`{_label(nodes[0])}` defines {len(facts)} member(s):\n" + "\n".join(lines),
                            facts, resolved, graph=self._graph_of(ns, es), resolved=resolved)

    def _i_callers(self, nodes, plan, resolved):
        types = ["CALLS", "HANDLED_BY", "TESTS", "HTTP_CALLS", "CROSS_LANGUAGE_MAP", "INSTANTIATES", "EXECUTES"]
        if nodes[0]["type"] == "Table":
            return self._i_table_usage(nodes, plan, resolved)
        # include callers of overridden/interface methods (dynamic dispatch)
        roots = list(nodes)
        for n in nodes:
            for e, o, _ in self.store.neighbors(self.gid, n["id"], "out", ["OVERRIDES"]):
                roots.append(o)
        facts, ns, es = self._collect(roots, "in", types, plan.include_candidates)
        if not facts:
            return self._result("INSUFFICIENT_EVIDENCE",
                                f"The graph contains no callers of `{_label(nodes[0])}`. It may be unused, called via "
                                "reflection/framework wiring, or called from code the analyzers could not resolve.",
                                resolved=resolved, graph=self._graph_of(nodes, []))
        lines = [f"- `{f['subject']}` {f['relationship']} `{f['object']}` ({f['source_file']}:{f['line']}) [{f['validation_status']}]"
                 for f in facts[:plan.limit]]
        more = f"\n…and {len(facts) - plan.limit} more." if len(facts) > plan.limit else ""
        return self._result("ANSWERED", f"{len(facts)} incoming relationship(s) to `{_label(nodes[0])}`:\n" + "\n".join(lines) + more,
                            facts, resolved, graph=self._graph_of(ns, es), resolved=resolved)

    def _i_callees(self, nodes, plan, resolved):
        types = ["CALLS", "EXECUTES", "READS", "WRITES", "HTTP_CALLS", "CROSS_LANGUAGE_MAP", "INSTANTIATES", "HANDLED_BY"]
        facts, ns, es = self._collect(nodes, "out", types, plan.include_candidates)
        if not facts:
            return self._result("INSUFFICIENT_EVIDENCE", f"No outgoing calls from `{_label(nodes[0])}` were resolved to repository code.",
                                resolved=resolved, graph=self._graph_of(nodes, []))
        lines = [f"- {f['relationship']} `{f['object']}` ({f['source_file']}:{f['line']}) [{f['validation_status']}]" for f in facts[:plan.limit]]
        return self._result("ANSWERED", f"`{_label(nodes[0])}` →\n" + "\n".join(lines), facts, resolved,
                            graph=self._graph_of(ns, es), resolved=resolved)

    def _i_api_handler(self, nodes, plan, resolved):
        facts, ns, es = self._collect(nodes, "out", ["HANDLED_BY"], plan.include_candidates)
        if not facts:
            doc = any(n.get("documented") for n in nodes)
            return self._result("INSUFFICIENT_EVIDENCE",
                                f"`{nodes[0]['name']}` exists{' in the API specification' if doc else ''}, but no handler "
                                "was found in source (the route may be registered dynamically or in an unsupported framework).",
                                resolved=resolved, graph=self._graph_of(nodes, []))
        lines = [f"- `{f['subject']}` is handled by `{f['object']}` at {f['source_file']}:{f['line']} — evidence: {f['evidence']}"
                 for f in facts]
        return self._result("ANSWERED", "\n".join(lines), facts, resolved, graph=self._graph_of(ns, es), resolved=resolved)

    def _paths_result(self, res: dict, nodes, resolved, what: str, plan) -> dict:
        if not res["rows"]:
            return self._result("INSUFFICIENT_EVIDENCE",
                                f"No data access was found along {what}. The handler chain may use an unresolved call, "
                                "a framework/ORM pattern not yet supported, or no database at all.",
                                resolved=resolved, graph={"nodes": res["nodes"], "edges": res["edges"]},
                                limitations=["Static analysis: dynamic dispatch, reflection and runtime wiring are not followed."])
        edges = {e["id"]: e for e in res["edges"]}
        nodemap = {n["id"]: n for n in res["nodes"]}
        facts = []
        for p in res["paths"][:plan.limit]:
            for eid in p["edges"]:
                e = edges.get(eid)
                if e and e["source"] in nodemap and e["target"] in nodemap:
                    f = _fact(e, nodemap[e["source"]], nodemap[e["target"]])
                    if f not in facts:
                        facts.append(f)
        lines = []
        for r in res["rows"]:
            lines.append(f"- table `{r['table']['name']}`: {', '.join(r['operations'])} (via {', '.join(r['apis'][:4])})")
        chains = []
        for p in res["paths"][:6]:
            chain = " → ".join(_label(nodemap[n]) for n in p["nodes"] if n in nodemap)
            chains.append(f"  • {chain} [{p['status']}]")
        return self._result("ANSWERED", "Tables reached:\n" + "\n".join(lines) + "\nPaths:\n" + "\n".join(chains),
                            facts, resolved, paths=res["paths"], graph={"nodes": res["nodes"], "edges": res["edges"]},
                            resolved=resolved)

    def _i_api_to_data(self, nodes, plan, resolved):
        apis = [n for n in nodes if n["type"] == "RestApi"]
        if not apis:
            return self._i_sql_tables(nodes, plan, resolved)
        res = self.q.api_to_database([a["id"] for a in apis], plan.include_candidates, max_depth=max(plan.max_hops + 5, 8))
        return self._paths_result(res, nodes, resolved, f"the handler chain of {apis[0]['name']}", plan)

    def _i_sql_tables(self, nodes, plan, resolved):
        if nodes[0]["type"] == "RestApi":
            return self._i_api_to_data(nodes, plan, resolved)
        if nodes[0]["type"] == "SqlQuery":
            facts, ns, es = self._collect(nodes, "out", ["READS", "WRITES", "DEFINES"], plan.include_candidates)
            if not facts:
                return self._result("INSUFFICIENT_EVIDENCE", "No tables extracted for this SQL statement.", resolved=resolved)
            lines = [f"- {f['relationship']} `{f['object']}`" for f in facts]
            return self._result("ANSWERED", "\n".join(lines), facts, resolved, graph=self._graph_of(ns, es), resolved=resolved)
        # callable/class: treat it like an entry point and follow calls to data access
        roots = list(nodes)
        if nodes[0]["type"] in ("Class", "Interface"):
            roots = [o for e, o, _ in self.store.neighbors(self.gid, nodes[0]["id"], "out", ["DEFINES"]) if o["type"] in CALLABLE_TYPES]
        sg_nodes, sg_edges, facts, rows = [], [], [], {}
        for r in roots[:30]:
            t = self.q.traverse(r["id"], "out", ["CALLS", "EXECUTES", "READS", "WRITES"], max(plan.max_hops, 4), plan.include_candidates)
            for nid, (prev, e) in t["parent"].items():
                if e["type"] in ("READS", "WRITES"):
                    pn, pe = self.q._path_to(t["parent"], nid)
                    for x in pe:
                        s, o = self.store.get_node(self.gid, x["source"]), self.store.get_node(self.gid, x["target"])
                        f = _fact(x, s, o)
                        if f not in facts:
                            facts.append(f)
                        sg_nodes += [s, o]
                        sg_edges.append(x)
                    tn = self.store.get_node(self.gid, nid)
                    rows.setdefault(tn["name"], set()).add(e["type"])
        if not rows:
            return self._result("INSUFFICIENT_EVIDENCE", f"No table access reachable from `{_label(nodes[0])}` was found.",
                                resolved=resolved, graph=self._graph_of(nodes, []))
        lines = [f"- `{t}`: {', '.join(sorted(ops))}" for t, ops in sorted(rows.items())]
        return self._result("ANSWERED", f"Tables reached from `{_label(nodes[0])}`:\n" + "\n".join(lines), facts, resolved,
                            graph=self._graph_of(sg_nodes or nodes, sg_edges), resolved=resolved)

    def _i_schema(self, nodes, plan, resolved):
        t = nodes[0]
        if t["type"] != "Table":
            tables = [o for e, o, _ in self.store.neighbors(self.gid, t["id"], "out", ["MAPS_TO"])]
            if not tables:
                return self._result("NOT_FOUND", f"`{_label(t)}` is not a table (and is not mapped to one).", resolved=resolved)
            t = tables[0]
        sc = self.q.schema(t["id"])
        facts, ns, es = self._collect([t], "both", ["HAS_COLUMN", "FOREIGN_KEY", "MAPS_TO"], True)
        if not sc["columns"] and not facts:
            return self._result("INSUFFICIENT_EVIDENCE", f"Table `{t['name']}` is referenced, but its definition (DDL/ORM model) "
                                "was not found in the repository.", resolved=resolved)
        cols = [f"`{c['name']}` {c.get('data_type') or ''}{' PK' if c.get('primary_key') else ''}".strip() for c in sc["columns"]]
        lines = [f"Table `{t['name']}` columns: " + (", ".join(cols) or "none extracted")]
        for r in sc["references"]:
            lines.append(f"- references `{r['table']['name']}` ({r['edge'].get('evidence', '')})")
        for r in sc["referenced_by"]:
            lines.append(f"- referenced by `{r['table']['name']}`")
        for m in sc["mapped_by"]:
            lines.append(f"- mapped by ORM class `{m['name']}`")
        return self._result("ANSWERED", "\n".join(lines), facts, resolved, graph=self._graph_of(ns, es), resolved=resolved)

    def _i_table_usage(self, nodes, plan, resolved):
        t = nodes[0]
        if t["type"] != "Table":
            return self._i_callers(nodes, plan, resolved)
        ql = getattr(self, "_question", "").lower()
        want = set()
        if re.search(r"\b(writ|insert|updat|delet|modif|chang|sav|persist|creat)", ql):
            want.add("WRITES")
        if re.search(r"\b(read|select|query|queries|fetch|load|get)", ql):
            want.add("READS")
        want = want or {"READS", "WRITES"}
        sts = ["VERIFIED", "CANDIDATE"] if plan.include_candidates else ["VERIFIED"]
        facts, gnodes, gedges, code_lines, script_lines = [], [t], [], [], []
        for e, src, _ in self.store.neighbors(self.gid, t["id"], "in", ["READS", "WRITES"], sts, limit=500):
            if e["type"] not in want:
                continue
            if src["type"] == "SqlQuery":
                execs = self.store.neighbors(self.gid, src["id"], "in", ["EXECUTES"], sts, limit=50)
                if not execs:
                    script_lines.append(f"- SQL script `{src['name']}` {e['type']} `{t['name']}` ({_loc(src)})")
                    facts.append(_fact(e, src, t))
                    continue
                for xe, ex, _ in execs:
                    code_lines.append(f"- `{_label(ex)}` {e['type']} `{t['name']}` via SQL `{src['name']}` ({xe.get('file')}:{xe.get('line')}) [{xe['status']}]")
                    facts += [_fact(xe, ex, src), _fact(e, src, t)]
                    gnodes += [ex, src]
                    gedges += [xe, e]
            else:
                code_lines.append(f"- `{_label(src)}` {e['type']} `{t['name']}` ({e.get('file')}:{e.get('line')}) [{e['status']}] — {e.get('source_kind')}")
                facts.append(_fact(e, src, t))
                gnodes.append(src)
                gedges.append(e)
        if not facts:
            return self._result("INSUFFICIENT_EVIDENCE", f"No code that {' or '.join(sorted(w.lower()[:-1] + 's' for w in want))} "
                                f"`{t['name']}` was found.", resolved=resolved, graph=self._graph_of(nodes, []))
        lines = [f"Code accessing table `{t['name']}` ({'/'.join(sorted(want))}):"] + (code_lines[:plan.limit] or ["- none in application code"])
        if script_lines:
            lines += [f"SQL scripts ({len(script_lines)}):"] + script_lines[:8]
        imp = self.q.impact_of(t["id"], depth=max(plan.max_hops, 4), include_candidates=plan.include_candidates)
        apis = [r["node"]["name"] for r in imp["affected"].get("RestApi", [])]
        if apis:
            lines.append("Endpoints that reach this table: " + ", ".join(f"`{a}`" for a in apis[:15]))
        return self._result("ANSWERED", "\n".join(lines), facts, resolved, graph=self._graph_of(gnodes, gedges), resolved=resolved)

    def _i_dependencies(self, nodes, plan, resolved):
        res = self.q.dependencies_of(nodes[0]["id"], min(plan.max_hops, 3), plan.include_candidates)
        nodemap = {n["id"]: n for n in res["nodes"]}
        facts = [_fact(r["via"], nodemap[r["via"]["source"]], nodemap[r["via"]["target"]]) for r in res["rows"]
                 if r["via"]["source"] in nodemap and r["via"]["target"] in nodemap]
        if not facts:
            return self._result("INSUFFICIENT_EVIDENCE", f"No resolved dependencies of `{_label(nodes[0])}`.", resolved=resolved)
        lines = [f"- [{r['distance']}] {r['via']['type']} `{_label(r['node'])}`" for r in res["rows"][:plan.limit]]
        return self._result("ANSWERED", f"Dependencies of `{_label(nodes[0])}` (up to {min(plan.max_hops, 3)} hops):\n" + "\n".join(lines),
                            facts, resolved, graph={"nodes": res["nodes"], "edges": res["edges"]}, resolved=resolved)

    def _i_impact(self, nodes, plan, resolved):
        res = self.q.impact_of(nodes[0]["id"], depth=plan.max_hops, include_candidates=plan.include_candidates)
        nodemap = {n["id"]: n for n in res["nodes"]}
        facts = []
        for grp in res["affected"].values():
            for row in grp:
                e = row["via"]
                if e["source"] in nodemap and e["target"] in nodemap:
                    facts.append(_fact(e, nodemap[e["source"]], nodemap[e["target"]]))
        if not res["total_affected"]:
            return self._result("INSUFFICIENT_EVIDENCE", f"No dependents of `{_label(nodes[0])}` were found in the graph "
                                f"(within {plan.max_hops} hops).", resolved=resolved, graph=self._graph_of(nodes, []))
        order = ["RestApi", "Method", "Function", "Constructor", "Class", "Interface", "SqlQuery", "Table", "Test"]
        lines = [f"If `{_label(nodes[0])}` changes, {res['total_affected']} connected entities may need review:"]
        for typ in order + [t for t in res["affected"] if t not in order]:
            grp = res["affected"].get(typ)
            if not grp:
                continue
            names = ", ".join(f"`{_label(r['node'])}`" for r in grp[:10])
            lines.append(f"- {typ} ({len(grp)}): {names}{' …' if len(grp) > 10 else ''}")
        return self._result("ANSWERED", "\n".join(lines), facts, resolved, graph={"nodes": res["nodes"], "edges": res["edges"]},
                            limitations=["Impact = connected under static dependency rules (callers, handlers, tests, data access). "
                                         "It is a review aid, not a guarantee that these will break."], resolved=resolved)

    def _i_test_coverage(self, nodes, plan, resolved):
        if nodes[0]["type"] in ("Class", "Interface", "Enum"):
            members = [o for e, o, _ in self.store.neighbors(self.gid, nodes[0]["id"], "out", ["DEFINES"])
                       if o["type"] in CALLABLE_TYPES]
            nodes = nodes + members
        direct, ns, es = self._collect(nodes, "in", ["TESTS"], plan.include_candidates)
        # indirect: tests of the callers (1 level), and of APIs handled by this method
        indirect = []
        seen_tests = {f["subject_id"] for f in direct}
        for n in nodes:
            ups = self.store.neighbors(self.gid, n["id"], "in", ["CALLS", "HANDLED_BY"], limit=100)
            for e, up, _ in ups:
                f2, n2, e2 = self._collect([up], "in", ["TESTS"], plan.include_candidates)
                f2 = [f for f in f2 if f["subject_id"] not in seen_tests]
                for f in f2:
                    f["indirect_via"] = _label(up)
                    seen_tests.add(f["subject_id"])
                indirect += f2
                ns += n2 + [up]
                es += e2 + [e]
        if not direct and not indirect:
            return self._result("INSUFFICIENT_EVIDENCE", f"No tests exercising `{_label(nodes[0])}` were found "
                                "(directly or through its callers).", resolved=resolved, graph=self._graph_of(nodes, []))
        lines = [f"Direct tests ({len(direct)}):"] + [f"- `{f['subject']}` ({f['source_file']}:{f['line']})" for f in direct[:plan.limit]]
        if indirect:
            lines += [f"Indirect tests via callers/endpoints ({len(indirect)}):"] + \
                     [f"- `{f['subject']}` via `{f['indirect_via']}`" for f in indirect[:plan.limit]]
        return self._result("ANSWERED", "\n".join(lines), direct + indirect, resolved, graph=self._graph_of(ns, es), resolved=resolved)

    def _i_path(self, a, b, plan, resolved):
        res = self.q.path_between(a["id"], b["id"], max_depth=max(plan.max_hops, 4) + 2, include_candidates=plan.include_candidates)
        if not res.get("found"):
            return self._result("INSUFFICIENT_EVIDENCE", f"No connection between `{_label(a)}` and `{_label(b)}` was found "
                                "within the traversal bound.", resolved=resolved)
        nodemap = {n["id"]: n for n in res["nodes"]}
        facts = [_fact(e, nodemap[e["source"]], nodemap[e["target"]]) for e in res["path_edges"]]
        chain = []
        for e in res["path_edges"]:
            chain.append(f"`{_label(nodemap[e['source']])}` —{e['type']}→ `{_label(nodemap[e['target']])}`")
        return self._result("ANSWERED", ("Directed path" if res["directed"] else "Connection (mixed directions)") + ":\n- " + "\n- ".join(chain),
                            facts, resolved, paths=[{"nodes": res["path"], "edges": [e["id"] for e in res["path_edges"]]}],
                            graph={"nodes": res["nodes"], "edges": res["edges"]}, resolved=resolved)

    def _summary(self):
        s = self.q.summary()
        rep = s["report"]
        m = s["meta"]
        lines = [f"Repository `{m.get('repo_key')}` @ {(m.get('commit') or '')[:10]} — {rep.get('total_entities', 0)} entities, "
                 f"{rep.get('total_relationships', 0)} relationships.",
                 "Entities: " + ", ".join(f"{k} {v}" for k, v in rep.get("entity_counts", {}).items() if k not in ("Directory",)),
                 f"REST endpoints: {rep.get('apis_total', 0)} ({rep.get('apis_with_handler', 0)} with a resolved handler)."]
        if s["tables"]:
            lines.append("Tables: " + ", ".join(f"`{t['name']}`" for t in s["tables"][:20]))
        if s["most_called"]:
            lines.append("Most-called code: " + ", ".join(f"`{_label(h['node'])}` ({h['callers']})" for h in s["most_called"][:5]))
        facts = [{"subject": m.get("repo_key"), "relationship": "SUMMARY", "object": k, "value": v} for k, v in rep.get("entity_counts", {}).items()]
        return self._result("ANSWERED", "\n".join(lines), facts)

    def _list_apis(self, limit):
        apis = self.store.nodes_by_type(self.gid, ["RestApi"], 1000)
        if not apis:
            return self._result("INSUFFICIENT_EVIDENCE", "No REST endpoints were extracted from this repository.")
        facts, lines = [], []
        for a in apis[:max(limit, 50)]:
            hs = self.store.neighbors(self.gid, a["id"], "out", ["HANDLED_BY"])
            h = _label(hs[0][1]) if hs else "(no handler in source)"
            lines.append(f"- `{a['name']}` → `{h}`" if hs else f"- `{a['name']}` {h}")
            facts.append({"subject": a["name"], "subject_id": a["id"], "relationship": "HANDLED_BY", "object": h,
                          "source_file": a.get("file"), "line": a.get("line")})
        return self._result("ANSWERED", f"{len(apis)} endpoint(s):\n" + "\n".join(lines), facts,
                            graph=self._graph_of(apis[:100], []))

    def _list_tables(self, limit):
        ts = self.store.nodes_by_type(self.gid, ["Table"], 1000)
        if not ts:
            return self._result("INSUFFICIENT_EVIDENCE", "No database tables were found in SQL files, SQL strings or ORM models.")
        lines = [f"- `{t['name']}`{' (schema defined)' if t.get('defined_in_schema') else ''}" for t in ts[:max(limit, 50)]]
        facts = [{"subject": t["name"], "subject_id": t["id"], "relationship": "IS_TABLE", "source_file": t.get("file")} for t in ts]
        return self._result("ANSWERED", f"{len(ts)} table(s):\n" + "\n".join(lines), facts, graph=self._graph_of(ts[:100], []))
