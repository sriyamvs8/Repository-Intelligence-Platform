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
from ..graph.insights import Insights
from ..graph.store import GraphStore
from ..kg.model import normalize_route
from .cypher import (CypherRejected, entity_ids_in_rows, generate_cypher, rows_to_text, scope_rows, template_for,
                     validate_cypher)
from .gemini import explain_with_gemini, grounded, plan_with_gemini
from .plan import INTENT_GUIDE, EntityRef, Intent, QueryPlan, rule_plan

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
    Intent.FLOW: ["RestApi"],
    Intent.EXPLAIN: CALLABLE_TYPES + ["Class", "Interface", "Enum", "Table", "RestApi", "SqlQuery", "Test"],
}
# complex analyses answered by reviewed, bounded traversals even when Gemini->Cypher is available
TEMPLATE_FIRST = {Intent.IMPACT, Intent.API_TO_DATA, Intent.TEST_COVERAGE, Intent.PATH, Intent.TABLE_USAGE,
                  Intent.DEPENDENCIES, Intent.SUMMARY, Intent.LIST_APIS, Intent.LIST_TABLES, Intent.FLOW,
                  Intent.UNUSED, Intent.HOTSPOTS, Intent.UNTESTED}
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
        self._notes: list[str] = []
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
            # an interface method and its implementation(s) are one concept: answer for the interface method
            ids = {n["id"] for n in same}
            for n in same:
                impls = {e["source"] for e, o, _ in self.store.neighbors(self.gid, n["id"], "in", ["OVERRIDES"], limit=50)}
                if impls and (ids - {n["id"]}) <= impls:
                    self._notes.append(f"'{term}' matches an interface method and its implementation(s); "
                                       f"answering for {_label(n)} (callers of implementations are included).")
                    return [n] + [x for x in same if x["id"] != n["id"]], []
            # prefer non-test code when ambiguous between test and production code
            prod = [n for n in same if not n.get("is_test_code")]
            groups = {(n["type"], (n.get("qualified_name") or n["name"]).split("(")[0]) for n in prod}
            if prod and len(groups) == 1:
                return prod[:10], []
            return [], same[:8]
        if best_cands:
            return [], best_cands
        # typo tolerance: closest names among entities of the preferred types
        import difflib
        pool = self.store.nodes_by_type(self.gid, preferred or ["Class", "Interface", "Method", "Function", "Table",
                                                                "RestApi", "Test", "Enum"], 50000)
        names: dict[str, list[dict]] = {}
        for n in pool:
            names.setdefault((n.get("name") or "").lower(), []).append(n)
            q = (n.get("qualified_name") or "").split("(")[0].split(".")
            if len(q) >= 2:
                names.setdefault(".".join(q[-2:]).lower(), []).append(n)
        for term in terms[:4]:
            if term.startswith("/"):
                continue
            close = difflib.get_close_matches(term.lower(), list(names), n=3, cutoff=0.8)
            if close:
                hits = names[close[0]]
                self._notes.append(f"Interpreted '{term}' as '{hits[0]['name']}' (closest name in the repository).")
                prod = [h for h in hits if not h.get("is_test_code")] or hits
                return prod[:10], []
        return [], []

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
        self._notes: list[str] = []
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
        if self._notes:
            resp["limitations"] = self._notes + resp["limitations"]
        resp["answer_dev"] = resp["answer"]
        if not resp.get("answer_nl"):
            resp["answer_nl"] = resp.get("explanation") or (_compose_nl(plan.intent.value, resp) if resp["status"] == "ANSWERED" else None) \
                or _plain(resp["answer"], resp["status"])
        it = plan.intent.value
        ents = resp.get("resolved_entities") or []
        status_meaning = {"ANSWERED": "answer built only from facts found in the code graph",
                          "INSUFFICIENT_EVIDENCE": "the entity exists but the graph has no evidence for what was asked",
                          "NOT_FOUND": "no entity with that name exists in the analyzed repository",
                          "CLARIFY": "several entities match; you must choose one",
                          "UNSUPPORTED": "the question is outside what the code graph can answer"}
        cy = resp.get("cypher") or {}
        resp["breakdown"] = [
            {"step": "Understood the question", "detail": f"{INTENT_GUIDE.get(it, it)}  →  intent {it} (planner: {planner})"},
            {"step": "Identified entities", "detail": ", ".join(f"{e.get('type')} {e.get('name')}" for e in ents[:6])
             or ("not needed for this question" if it in ("SUMMARY", "COUNT", "LIST_TYPE", "LIST_APIS", "LIST_TABLES",
                                                         "UNUSED", "HOTSPOTS", "UNTESTED") else "none matched"),
             "notes": list(self._notes)},
            {"step": "Built the graph query", "detail": (cy.get("source") or "reviewed traversal") +
             (" — executed on Neo4j" if cy.get("executed") else " — equivalent Cypher shown")},
            {"step": "Retrieved evidence", "detail": f"{len(resp['facts'])} fact(s) with file/line evidence"},
            {"step": "Composed the answer", "detail": f"{resp['status']}: {status_meaning.get(resp['status'], '')}"},
        ]
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
                limitations=None, candidates=None, resolved=None, nl: str | None = None) -> dict:
        return {"status": status, "answer": answer, "answer_nl": nl, "facts": facts or [], "entities": entities or [],
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
        if it == Intent.COUNT:
            return self._count(plan)
        if it == Intent.LIST_TYPE:
            return self._list_type(plan)
        if it == Intent.UNUSED:
            return self._unused(plan)
        if it == Intent.HOTSPOTS:
            return self._hotspots(plan)
        if it == Intent.UNTESTED:
            return self._untested(plan)
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
        rep = self._ins().impact_report(nodes[0]["id"], depth=max(plan.max_hops, 3), include_candidates=plan.include_candidates)
        items, pot = rep["items"], rep["potential"]
        if not items and not pot:
            return self._result("INSUFFICIENT_EVIDENCE", f"No dependents of `{_label(nodes[0])}` were found in the graph.",
                                resolved=resolved, graph=self._graph_of(nodes, []),
                                nl=f"Nothing in the repository depends on {_label(nodes[0])} according to the code graph. "
                                   "It may still be used via reflection, configuration or from outside the repository.")
        dev = [f"Risk: **{rep['risk_level']}** · blast radius {rep['blast_radius']}/100 · "
               f"{rep['counts']['apis']} API(s), {rep['counts']['code']} code item(s), {rep['counts']['tests']} test(s), "
               f"{rep['counts']['potential']} potential"]
        order = ["RestApi", "Method", "Function", "Constructor", "Class", "Interface", "SqlQuery", "Table", "Test"]
        by_type: dict[str, list] = {}
        for i in items:
            by_type.setdefault(i["node"]["type"], []).append(i)
        for typ in order + [t for t in by_type if t not in order]:
            grp = by_type.get(typ)
            if grp:
                dev.append(f"- {typ} ({len(grp)}): " + ", ".join(f"`{_label(i['node'])}` [{i['risk']}]" for i in grp[:10])
                           + (" …" if len(grp) > 10 else ""))
        if pot:
            dev.append(f"- Potential (heuristic, {len(pot)}): " + ", ".join(f"`{_label(p['node'])}`" for p in pot[:6]))
        facts = []
        nodemap = {n["id"]: n for n in rep["graph"]["nodes"]}
        for i in items:
            e = i["via"]
            if e["source"] in nodemap and e["target"] in nodemap:
                f = _fact(e, nodemap[e["source"]], nodemap[e["target"]])
                f["risk"] = i["risk"]
                facts.append(f)
        nl = rep["summary"] + " " + " ".join(rep["recommendations"])
        res = self._result("ANSWERED", "\n".join(dev), facts, resolved, graph=rep["graph"],
                           limitations=[rep["model"]["disclaimer"]], resolved=resolved, nl=nl)
        res["impact"] = {k: rep[k] for k in ("risk_level", "blast_radius", "counts", "recommendations", "untested_apis",
                                             "tables_touched", "model")}
        return res

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
        prof = self._ins().repo_profile()
        arch = prof["architecture"]
        dev = ["**About**"] + [f"- {l}" for l in prof["summary_lines"]] + ["", "**Architecture**"] + \
              [f"- {l['layer']} ({l['count']}): " + ", ".join(f"`{e}`" for e in l["examples"][:4]) for l in arch["layers"]] + \
              ["", "Technology: " + ", ".join(arch["frameworks"]), "Modules: " + (", ".join(arch["modules"]) or "single module")]
        facts = [{"subject": l["layer"], "relationship": "LAYER_COUNT", "object": str(l["count"]),
                  "evidence": ", ".join(l["examples"])} for l in arch["layers"]]
        res = self._result("ANSWERED", "\n".join(dev), facts, nl=" ".join(prof["summary_lines"]))
        res["profile"] = prof
        return res

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


    # ------------------------------------------------------------------ new intents (no entity needed)
    def _ins(self) -> Insights:
        return Insights(self.store, self.gid)

    def _count(self, plan):
        rep = (self.store.get_meta(self.gid) or {}).get("report", {})
        ec = rep.get("entity_counts", {})
        sub = plan.subject or "All"
        if sub in ("Controller", "Service", "Data access", "Model"):
            prof = self._ins().repo_profile()
            n = next((l["count"] for l in prof["architecture"]["layers"] if l["layer"] == sub), 0)
            nl = f"There are {n} {sub.lower()} component(s) in this repository."
            return self._result("ANSWERED", f"{sub} components: **{n}**", [{"subject": sub, "relationship": "COUNT", "object": str(n)}], nl=nl)
        if sub == "Method":
            n = ec.get("Method", 0) + ec.get("Function", 0) + ec.get("Constructor", 0)
            return self._result("ANSWERED", f"Methods/functions: **{n}** (Method {ec.get('Method', 0)}, Function {ec.get('Function', 0)}, Constructor {ec.get('Constructor', 0)})",
                                [{"subject": "Method", "relationship": "COUNT", "object": str(n)}],
                                nl=f"The code has {n} methods and functions.")
        if sub == "All" or sub not in ec:
            lines = [f"- {k}: {v}" for k, v in sorted(ec.items(), key=lambda x: -x[1]) if k not in ("Directory",)]
            return self._result("ANSWERED", "Entity counts:\n" + "\n".join(lines),
                                [{"subject": k, "relationship": "COUNT", "object": str(v)} for k, v in ec.items()],
                                nl=f"The graph holds {rep.get('total_entities', 0)} code entities and {rep.get('total_relationships', 0)} relationships; "
                                   f"for example {ec.get('RestApi', 0)} endpoints, {ec.get('Table', 0)} tables and {ec.get('Test', 0)} tests.")
        n = ec.get(sub, 0)
        word = {"RestApi": "REST endpoints", "Table": "database tables", "Test": "tests", "Class": "classes",
                "Interface": "interfaces", "SqlQuery": "SQL statements", "Column": "table columns", "File": "files"}.get(sub, sub)
        return self._result("ANSWERED", f"{word}: **{n}**", [{"subject": sub, "relationship": "COUNT", "object": str(n)}],
                            nl=f"There are {n} {word} in this repository.")

    def _list_type(self, plan):
        sub = plan.subject or "Class"
        ins = self._ins()
        if sub in ("Controller", "Service", "Data access", "Model"):
            pool = [c for c in self.store.nodes_by_type(self.gid, ["Class", "Interface"], 20000) if not c.get("is_test_code")]
            items = [c for c in pool if ins.layer(c) == sub]
            if sub == "Controller":
                for h in self.store.get_nodes(self.gid, list(ins.handler_ids())[:2000]):
                    if h["type"] == "Function" and not h.get("class") and h not in items:
                        items.append(h)
        elif sub == "Method":
            items = self.store.nodes_by_type(self.gid, ["Method", "Function"], 5000)
        else:
            items = self.store.nodes_by_type(self.gid, [sub], 5000)
        if not items:
            return self._result("INSUFFICIENT_EVIDENCE", f"No {sub} components were found.", nl=f"I could not find any {sub.lower()} components.")
        lines = [f"- `{_label(n)}` — {_loc(n)}" for n in items[:max(plan.limit, 40)]]
        more = f"\n…and {len(items) - len(lines)} more" if len(items) > len(lines) else ""
        facts = [{"subject": _label(n), "subject_id": n["id"], "relationship": "IS_A", "object": sub,
                  "source_file": n.get("file"), "line": n.get("line")} for n in items[:200]]
        return self._result("ANSWERED", f"{len(items)} {sub} component(s):\n" + "\n".join(lines) + more, facts,
                            graph=self._graph_of(items[:80], []),
                            nl=f"The repository has {len(items)} {sub.lower()} component(s), for example "
                               f"{', '.join(_label(n) for n in items[:4])}.")

    def _call_index(self):
        incoming: dict[str, int] = {}
        for t in ("CALLS", "HANDLED_BY", "TESTS", "HTTP_CALLS", "CROSS_LANGUAGE_MAP"):
            for e in self.store.rels_by_type(self.gid, [t], limit=500000):
                incoming[e["target"]] = incoming.get(e["target"], 0) + (1 if t == "CALLS" else 0) or incoming.get(e["target"], 0) or 0
                if t != "CALLS":
                    incoming.setdefault(e["target"], 0)
        return incoming

    def _unused(self, plan):
        used = set()
        for t in ("CALLS", "HANDLED_BY", "TESTS", "HTTP_CALLS", "CROSS_LANGUAGE_MAP", "INSTANTIATES"):
            used |= {e["target"] for e in self.store.rels_by_type(self.gid, [t], limit=500000)}
        overriding = {e["source"] for e in self.store.rels_by_type(self.gid, ["OVERRIDES"], limit=500000)}
        overridden = {e["target"] for e in self.store.rels_by_type(self.gid, ["OVERRIDES"], limit=500000)}
        cands = []
        for m in self.store.nodes_by_type(self.gid, ["Method", "Function"], 100000):
            if m.get("is_test_code") or m["id"] in used or m["id"] in overriding:
                continue
            if m["id"] in overridden and any(o in used for o in ()):
                continue
            nm = m.get("name") or ""
            if nm.startswith("__") or nm in ("main", "toString", "hashCode", "equals", "run", "setUp", "configure"):
                continue
            cands.append(m)
        if not cands:
            return self._result("ANSWERED", "No uncalled methods found.", nl="Every method is called from somewhere in the repository.")
        lines = [f"- `{_label(m)}` — {_loc(m)}" for m in cands[:max(plan.limit, 40)]]
        facts = [{"subject": _label(m), "subject_id": m["id"], "relationship": "NO_INCOMING_CALLS", "object": "",
                  "source_file": m.get("file"), "line": m.get("line")} for m in cands[:200]]
        return self._result("ANSWERED", f"{len(cands)} method(s) with no callers, handlers or tests in the repository:\n" + "\n".join(lines)
                            + (f"\n…and {len(cands) - len(lines)} more" if len(cands) > len(lines) else ""), facts,
                            graph=self._graph_of(cands[:60], []),
                            limitations=["'Unused' = no static reference inside this repository. Framework callbacks, reflection, "
                                         "getters used by serializers/templates and public library APIs can still be used."],
                            nl=f"{len(cands)} methods are never called from other code in this repository (possible dead code), "
                               f"e.g. {', '.join(_label(m) for m in cands[:3])}. Check framework/reflection use before deleting them.")

    def _hotspots(self, plan):
        indeg: dict[str, int] = {}
        for e in self.store.rels_by_type(self.gid, ["CALLS"], limit=500000):
            indeg[e["target"]] = indeg.get(e["target"], 0) + 1
        top = sorted(indeg.items(), key=lambda x: -x[1])[:max(10, min(plan.limit, 25))]
        nodes = self.store.get_nodes(self.gid, [t for t, _ in top])
        nm = {n["id"]: n for n in nodes}
        lines, facts = [], []
        for tid, c in top:
            n = nm.get(tid)
            if not n:
                continue
            lines.append(f"- `{_label(n)}` — called from {c} place(s) — {_loc(n)}")
            facts.append({"subject": _label(n), "subject_id": tid, "relationship": "CALLED_BY_COUNT", "object": str(c),
                          "source_file": n.get("file"), "line": n.get("line")})
        if not lines:
            return self._result("INSUFFICIENT_EVIDENCE", "No resolved calls in this repository.", nl="No call relationships were found.")
        return self._result("ANSWERED", "Most-called methods (hotspots):\n" + "\n".join(lines), facts,
                            graph=self._graph_of(nodes, []),
                            nl=f"The most heavily used code is {facts[0]['subject']} (called from {facts[0]['object']} places)"
                               + (f", followed by {facts[1]['subject']} and {facts[2]['subject']}" if len(facts) > 2 else "")
                               + ". Changes there have the widest effect — test them carefully.")

    def _untested(self, plan):
        apis = self.store.nodes_by_type(self.gid, ["RestApi"], 5000)
        tested = {e["target"] for e in self.store.rels_by_type(self.gid, ["TESTS"], limit=500000)}
        handlers = {}
        for e in self.store.rels_by_type(self.gid, ["HANDLED_BY"], limit=500000):
            handlers.setdefault(e["source"], []).append(e["target"])
        untested = [a for a in apis if a["id"] not in tested and not any(h in tested for h in handlers.get(a["id"], []))]
        if not apis:
            return self._result("INSUFFICIENT_EVIDENCE", "No endpoints in this repository.", nl="This repository exposes no REST endpoints.")
        lines = [f"- `{a['name']}` → `{_label(self.store.get_node(self.gid, handlers[a['id']][0]))}`" if handlers.get(a["id"]) else f"- `{a['name']}`"
                 for a in untested[:max(plan.limit, 50)]]
        facts = [{"subject": a["name"], "subject_id": a["id"], "relationship": "NO_TESTS", "object": "",
                  "source_file": a.get("file"), "line": a.get("line")} for a in untested]
        return self._result("ANSWERED", f"{len(untested)} of {len(apis)} endpoint(s) have no test (directly or through their handler):\n"
                            + "\n".join(lines), facts, graph=self._graph_of(untested[:80], []),
                            nl=f"{len(untested)} of {len(apis)} endpoints have no automated test that calls them or their handler"
                               + (f", e.g. {', '.join(a['name'] for a in untested[:3])}." if untested else "."))

    # ------------------------------------------------------------------ flow & explain
    def _i_flow(self, nodes, plan, resolved):
        api = next((n for n in nodes if n["type"] == "RestApi"), None)
        if not api:
            return self._i_explain(nodes, plan, resolved)
        fl = self._ins().request_flow(api["id"], plan.include_candidates)
        lines = [f"{s['n']}. [{s['layer']}] {s['text']}" for s in fl["steps"][:30]]
        facts = [{"subject": s["text"], "relationship": s["layer"], "object": "", "evidence": s.get("dev"),
                  "validation_status": s.get("status", "VERIFIED"), "subject_id": s.get("node")} for s in fl["steps"]]
        tables = ", ".join(fl["tables"]) or "no database table"
        byid = {n["id"]: n for n in fl["nodes"]}
        chain = []
        for layer in ("Controller", "Service", "Data access"):
            lane = next((l for l in fl["lanes"] if l["layer"] == layer and l["nodes"]), None)
            if lane:
                n = byid.get(lane["nodes"][0]) if isinstance(lane["nodes"][0], str) else lane["nodes"][0]
                if n:
                    chain.append(f"{layer.lower()} {_short(n)}")
        route = f" through the {', then the '.join(chain)}" if chain else ""
        nl = (f"When a client calls {api['name']}, the request goes{route}, and finally reaches {tables}. "
              f"{len(fl['steps'])} evidence-backed steps are listed below.")
        res = self._result("ANSWERED", "\n".join(lines), facts, resolved, graph={"nodes": fl["nodes"], "edges": fl["edges"]},
                           resolved=resolved, nl=nl)
        res["flow"] = fl
        return res

    def _i_explain(self, nodes, plan, resolved):
        n = nodes[0]
        ins = self._ins()
        layer = ins.layer(n)
        sts = ["VERIFIED", "CANDIDATE"] if plan.include_candidates else ["VERIFIED"]
        nb = self.store.neighbors(self.gid, n["id"], "both", None, sts, limit=400)
        incoming = [(e, o) for e, o, d in nb if d == "in" and e["type"] in ("CALLS", "HANDLED_BY", "HTTP_CALLS", "CROSS_LANGUAGE_MAP", "INSTANTIATES")]
        outgoing = [(e, o) for e, o, d in nb if d == "out" and e["type"] in ("CALLS", "EXECUTES", "READS", "WRITES", "HTTP_CALLS", "INSTANTIATES", "HANDLED_BY")]
        members = [(e, o) for e, o, d in nb if d == "out" and e["type"] == "DEFINES"]
        tests = [(e, o) for e, o, d in nb if d == "in" and e["type"] == "TESTS"]
        owner = next((o for e, o, d in nb if d == "in" and e["type"] == "DEFINES"), None)
        data = self.q.traverse(n["id"], "out", ["CALLS", "EXECUTES", "READS", "WRITES", "DEFINES"] if n["type"] in ("Class", "Interface") else
                               ["CALLS", "EXECUTES", "READS", "WRITES"], 4, plan.include_candidates, 300)
        tables = sorted({self.store.get_node(self.gid, nid)["name"] for nid, (p, e) in data["parent"].items() if e["type"] in ("READS", "WRITES")})
        imp = self.q.impact_of(n["id"], 3, plan.include_candidates, max_nodes=200)
        apis = [r["node"]["name"] for r in imp["affected"].get("RestApi", [])]
        kind = {"Method": "method", "Function": "function", "Constructor": "constructor", "Class": "class", "Interface": "interface",
                "Table": "database table", "RestApi": "REST endpoint", "SqlQuery": "SQL statement", "Test": "test", "Enum": "enum"}.get(n["type"], n["type"])
        parts = [f"{_label(n)} is a {kind}" + (f" in the {layer.lower()} layer" if layer not in ("Other", "API", "Database", "SQL", "Test") else "")
                 + (f", defined in {_label(owner)}" if owner and owner["type"] != "File" else "") + f" ({_loc(n)})."]
        if n.get("docstring") or n.get("summary"):
            parts.append(f"Its documentation says: “{(n.get('docstring') or n.get('summary'))[:200]}”.")
        if members:
            parts.append(f"It contains {len(members)} member(s) such as {', '.join(o['name'] for e, o in members[:5])}.")
        if incoming:
            parts.append(f"It is used by {len(incoming)} place(s), e.g. {', '.join(_label(o) for e, o in incoming[:3])}.")
        elif n["type"] in CALLABLE_TYPES:
            parts.append("Nothing in the repository calls it directly (it may be invoked by a framework).")
        if outgoing:
            parts.append(f"It uses {', '.join(_label(o) for e, o in outgoing[:4])}{' and more' if len(outgoing) > 4 else ''}.")
        if tables:
            parts.append(f"Directly or indirectly it works with the table(s) {', '.join(tables[:6])}.")
        if apis:
            parts.append(f"It is reachable from {len(apis)} endpoint(s), e.g. {', '.join(apis[:3])}.")
        parts.append(f"It is covered by {len(tests)} test(s)." if tests else "No test targets it directly.")
        dev = [f"**{_label(n)}** · {n['type']} · layer: {layer} · {_loc(n)}", f"id: `{n['id']}`"]
        if n.get("signature"):
            dev.append(f"signature: `{n['signature']}`")
        for title, rows in (("used by", incoming), ("uses", outgoing), ("members", members), ("tests", tests)):
            if rows:
                dev.append(f"{title} ({len(rows)}): " + ", ".join(f"`{_label(o)}`" for e, o in rows[:12]))
        if tables:
            dev.append("tables reached: " + ", ".join(f"`{t}`" for t in tables))
        if apis:
            dev.append("endpoints reaching it: " + ", ".join(f"`{a}`" for a in apis[:10]))
        facts = [_fact(e, o, n) for e, o in incoming[:30]] + [_fact(e, n, o) for e, o in outgoing[:30]] + \
                [_fact(e, n, o) for e, o in members[:30]] + [_fact(e, o, n) for e, o in tests[:20]]
        facts.insert(0, {"subject": _label(n), "subject_id": n["id"], "relationship": "LOCATED_AT", "object": _loc(n),
                         "source_file": n.get("file"), "line": n.get("line"), "validation_status": "VERIFIED"})
        g_nodes = [n] + [o for e, o in incoming[:20] + outgoing[:20] + members[:20] + tests[:10]]
        g_edges = [e for e, o in incoming[:20] + outgoing[:20] + members[:20] + tests[:10]]
        return self._result("ANSWERED", "\n".join(dev), facts, resolved, graph=self._graph_of(g_nodes, g_edges),
                            resolved=resolved, nl=" ".join(parts))


def _plain(answer: str, status: str) -> str:
    """Fallback plain-English rendering of a developer answer."""
    text = re.sub(r"`([^`]+)`", r"\1", answer or "")
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    lines = [l.strip(" -•") for l in text.splitlines() if l.strip()]
    if not lines:
        return ""
    head = lines[0].rstrip(":")
    rest = [l for l in lines[1:] if l]
    if rest:
        shown = "; ".join(rest[:4])
        return f"{head}: {shown}" + (f" (and {len(rest) - 4} more)." if len(rest) > 4 else ".")
    return head


def _names(xs, n=4) -> str:
    xs = list(dict.fromkeys(x for x in xs if x))
    if not xs:
        return ""
    if len(xs) > n:
        return ", ".join(xs[:n]) + f" and {len(xs) - n} more"
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def _compose_nl(intent: str, r: dict) -> str | None:
    """Turn the retrieved facts into plain English sentences (no new information is added)."""
    facts = [f for f in r.get("facts", []) if isinstance(f, dict)]
    ents = r.get("resolved_entities") or []
    who = _label(ents[0]) if ents else "it"
    if intent == "API_HANDLER":
        hs = [f for f in facts if f.get("relationship") == "HANDLED_BY"]
        if hs:
            return " ".join(f"{f['subject']} is handled by {f['object']} ({f.get('source_file')}:{f.get('line')})." for f in hs[:3])
    if intent in ("API_TO_DATA", "SQL_TABLES"):
        paths = r.get("paths") or []
        g = {n["id"]: n for n in (r.get("graph") or {}).get("nodes", [])}
        writes = sorted({p["table"] for p in paths if p.get("operation") == "WRITES"})
        reads = sorted({p["table"] for p in paths if p.get("operation") == "READS"})
        if not paths:
            writes = sorted({f["object"] for f in facts if f.get("relationship") == "WRITES"})
            reads = sorted({f["object"] for f in facts if f.get("relationship") == "READS"})
        parts = []
        if writes:
            parts.append(f"writes to {_names(writes)}")
        if reads:
            parts.append(f"reads from {_names(reads)}")
        if not parts:
            return None
        s = f"{who} {' and '.join(parts)}."
        if paths:
            mids = [_label(g[n]) for n in paths[0]["nodes"][1:-1] if n in g and g[n]["type"] != "SqlQuery"]
            if mids:
                s += f" The request goes through {' → '.join(mids[:6])}{' …' if len(mids) > 6 else ''}."
        cand = any(p.get("status") == "CANDIDATE" for p in paths)
        if cand:
            s += " Some links on the way are probable rather than confirmed."
        return s
    if intent == "CALLERS":
        callers = [f["subject"] for f in facts if f.get("relationship") in ("CALLS", "HANDLED_BY", "HTTP_CALLS", "CROSS_LANGUAGE_MAP", "INSTANTIATES")]
        tests = [f["subject"] for f in facts if f.get("relationship") == "TESTS"]
        apis = [f["subject"] for f in facts if f.get("relationship") == "HANDLED_BY"]
        s = f"{who} is used by {len(set(callers))} place(s): {_names(callers)}." if callers else f"No code calls {who} directly."
        if apis:
            s += f" It handles the endpoint(s) {_names(apis)}."
        if tests:
            s += f" {len(set(tests))} test(s) call it, e.g. {_names(tests, 2)}."
        return s
    if intent == "CALLEES":
        calls = [f["object"] for f in facts if f.get("relationship") == "CALLS"]
        data = [f"{'writes' if f['relationship'] == 'WRITES' else 'reads'} {f['object']}" for f in facts if f.get("relationship") in ("READS", "WRITES")]
        sql = [f["object"] for f in facts if f.get("relationship") == "EXECUTES"]
        s = f"{who} calls {_names(calls)}." if calls else f"{who} does not call other code in the repository."
        if sql:
            s += f" It runs SQL ({_names(sql, 3)})."
        if data:
            s += f" It {_names(data, 3)}."
        return s
    if intent == "SCHEMA":
        cols = [f["object"] for f in facts if f.get("relationship") == "HAS_COLUMN"]
        fks = [f["object"] for f in facts if f.get("relationship") == "FOREIGN_KEY" and f.get("subject") == who]
        cls = [f["subject"] for f in facts if f.get("relationship") == "MAPS_TO"]
        s = f"Table {who} has {len(cols)} column(s): {_names(cols, 8)}." if cols else f"Table {who} has no extracted columns."
        if fks:
            s += f" It references {_names(fks)}."
        if cls:
            s += f" In code it is represented by the class {_names(cls)}."
        return s
    if intent == "TABLE_USAGE":
        w = [f["subject"] for f in facts if f.get("relationship") == "WRITES" and f.get("subject_type") != "SqlQuery"]
        rd = [f["subject"] for f in facts if f.get("relationship") == "READS" and f.get("subject_type") != "SqlQuery"]
        ex = [f["subject"] for f in facts if f.get("relationship") == "EXECUTES"]
        parts = []
        if w or rd or ex:
            parts.append(f"Table {who} is used by {_names(ex + w + rd)}.")
        else:
            parts.append(f"Only SQL scripts touch table {who}; no application code accesses it.")
        return " ".join(parts)
    if intent == "TEST_COVERAGE":
        direct = [f["subject"] for f in facts if f.get("relationship") == "TESTS" and not f.get("indirect_via")]
        ind = [f["subject"] for f in facts if f.get("indirect_via")]
        s = f"{who} is tested directly by {len(set(direct))} test(s){': ' + _names(direct, 3) if direct else ''}."
        if ind:
            s += f" {len(set(ind))} more test(s) exercise it indirectly through its callers or endpoints."
        return s
    if intent == "DEPENDENCIES":
        deps = [f["object"] for f in facts]
        return f"{who} depends on {_names(deps, 6)}." if deps else None
    if intent == "PATH":
        chain = [facts[0]["subject"]] + [f["object"] for f in facts] if facts else []
        return f"They are connected in {len(facts)} step(s): {' → '.join(chain)}." if chain else None
    if intent == "LOCATE_SYMBOL":
        locs = [f for f in facts if f.get("relationship") == "LOCATED_AT"]
        if locs:
            return " ".join(f"{f['subject']} is in {f['object']}." for f in locs[:3])
    if intent == "LIST_METHODS":
        ms = [f["object"] for f in facts]
        return f"{who} contains {len(ms)} member(s): {_names(ms, 8)}." if ms else None
    if intent == "LIST_APIS":
        return f"The application exposes {len(facts)} REST endpoint(s), for example {_names([f['subject'] for f in facts], 4)}."
    if intent == "LIST_TABLES":
        return f"The repository works with {len(facts)} database table(s): {_names([f['subject'] for f in facts], 8)}."
    return None


def _short(n: dict) -> str:
    q = n.get("qualified_name") or n.get("name") or "?"
    if n.get("type") in ("Method", "Function", "Constructor"):
        parts = q.split("(")[0].split(".")
        return ".".join(parts[-2:]) + "()"
    return n.get("name") or q
