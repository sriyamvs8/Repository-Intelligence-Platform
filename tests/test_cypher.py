"""Text-to-Cypher: safety validator + hybrid routing (Gemini and Neo4j mocked; real Neo4j in test_store_parity)."""
import pytest

from tests.conftest import FIXTURE
from backend.config import settings
from backend.graph.store import InMemoryStore
from backend.kg.pipeline import analyze_repository
from backend.nlp import engine as eng
from backend.nlp.cypher import EXAMPLES, TEMPLATES, CypherRejected, validate_cypher

ATTACKS = [
    "MATCH (n) DETACH DELETE n",
    "MATCH (n:Entity {graph_id:$gid}) SET n.name='x' RETURN n",
    "CALL db.labels()",
    "MATCH (n:Entity {graph_id:$gid}) RETURN n; MATCH (m) DELETE m",
    "MATCH (n:RKGGraph {graph_id:$gid}) RETURN n.meta_json",
    "MATCH (n:Entity) RETURN n LIMIT 5",                                   # not scoped to the graph
    "MATCH (n:Entity {graph_id:$gid}) RETURN apoc.text.join(['a'],',')",
    "LOAD CSV FROM 'http://x' AS r RETURN r",
    "MATCH (n:Entity {graph_id:$gid}) RETURN n.props_json",
    "MATCH (n:Entity {graph_id:$gid, password:'x'}) RETURN n",
    "MATCH (n:Entity {graph_id:$gid}) WITH n MERGE (n)-[:X]->(n) RETURN n",
    "MATCH (n:Entity {graph_id:$gid}) RETURN n UNION MATCH (m) RETURN m",
    "MATCH (n:Entity {graph_id:$gid}) FOREACH (x IN [1] | CREATE (:A)) RETURN n",
    "MATCH (n:Entity {graph_id:$gid}) RETURN n // ; DROP DATABASE neo4j",
    "RETURN 1",
]


@pytest.mark.parametrize("q", ATTACKS)
def test_unsafe_cypher_rejected(q):
    with pytest.raises(CypherRejected):
        validate_cypher(q)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_templates_pass_validator(name):
    validate_cypher(TEMPLATES[name])


def test_examples_pass_validator():
    for _, q in EXAMPLES:
        validate_cypher(q)


def test_rewrites_bounds_and_limits():
    q, notes = validate_cypher("```cypher\nMATCH p=(a:Method {graph_id:$gid})-[:CALLS*]->(b) RETURN [n IN nodes(p) | n.name] AS names LIMIT 999\n```")
    assert "*1..6" in q and q.endswith("LIMIT 200") and notes
    q, _ = validate_cypher("MATCH (a:Method {graph_id:$gid}) RETURN a.name")
    assert q.endswith("LIMIT 100")


class FakeNeo4j(InMemoryStore):
    """In-memory store that pretends to execute Cypher (returns canned rows for the mocked LLM queries)."""
    supports_cypher = True
    canned: dict = {}

    def run_cypher(self, query, params):
        for key, rows in self.canned.items():
            if key in query:
                if isinstance(rows, Exception):
                    raise rows
                return rows
        return []


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    st = FakeNeo4j(tmp_path_factory.mktemp("neo"))
    res = analyze_repository(FIXTURE)
    st.save_graph(res.graph_id, res.meta, res.report, res.entities, res.relationships, res.diagnostics, res.rejected)
    return st, res.graph_id


@pytest.fixture
def gemini_on(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "nlp_mode", "hybrid")
    monkeypatch.setattr(eng, "explain_with_gemini", lambda *a, **k: None)
    monkeypatch.setattr(eng, "plan_with_gemini", lambda *a, **k: None)
    calls = []

    def use(responses):
        it = iter(responses)

        def fake(question, resolved, context, previous=None, error=None):
            calls.append({"question": question, "resolved": resolved, "error": error})
            return next(it, None)
        monkeypatch.setattr(eng, "generate_cypher", fake)
        return calls
    return use


GOOD = ("MATCH (c:Entity {graph_id: $gid})-[r:CALLS]->(m:Method {graph_id: $gid}) WHERE m.id = "
        "'method:com.shop.repo.OrderDao.insert(Order)' RETURN c.name AS caller, c.id AS caller_id, r.file AS file, "
        "r.line AS line, r.status AS status")


def test_cypher_path_answers(setup, gemini_on):
    st, gid = setup
    FakeNeo4j.canned = {"OrderDao.insert": [{"caller": "place", "caller_id": "method:com.shop.service.OrderServiceImpl.place(Order)",
                                             "file": "x.java", "line": 3, "status": "VERIFIED"}]}
    calls = gemini_on([GOOD])
    r = eng.AskEngine(st, gid).ask("Who calls OrderDao.insert?")
    assert r["status"] == "ANSWERED" and r["planner"] == "gemini → cypher"
    assert r["cypher"]["executed"] and r["cypher"]["query"].endswith("LIMIT 100")
    assert any(n["id"].startswith("method:com.shop.service.OrderServiceImpl.place") for n in r["graph"]["nodes"])
    # resolved entity ids are passed to Gemini so it can match exact ids
    assert calls[0]["resolved"] and calls[0]["resolved"][0]["id"] == "method:com.shop.repo.OrderDao.insert(Order)"


def test_cypher_repair_after_rejection(setup, gemini_on):
    st, gid = setup
    FakeNeo4j.canned = {"OrderDao.insert": [{"caller": "place", "caller_id": "method:x", "status": "VERIFIED"}]}
    calls = gemini_on(["MATCH (n:Entity) DETACH DELETE n", GOOD])
    r = eng.AskEngine(st, gid).ask("Who calls OrderDao.insert?")
    assert r["status"] == "ANSWERED" and "forbidden" in r["cypher"]["repaired_from"][0]["error"]
    assert calls[1]["error"]


def test_unsafe_cypher_falls_back_to_templates(setup, gemini_on):
    st, gid = setup
    gemini_on(["MATCH (n) DELETE n", "MATCH (n) DELETE n"])
    r = eng.AskEngine(st, gid).ask("Who calls OrderDao.insert?")
    assert r["status"] == "ANSWERED" and "OrderServiceImpl.place" in r["answer"]
    assert "cypher fallback" in r["planner"] and r["cypher"]["source"] == "reviewed template"
    assert any("rejected" in l for l in r["limitations"])


def test_empty_rows_fall_back(setup, gemini_on):
    st, gid = setup
    FakeNeo4j.canned = {}
    gemini_on([GOOD])
    r = eng.AskEngine(st, gid).ask("Who calls OrderDao.insert?")
    assert r["status"] == "ANSWERED" and any("no rows" in l for l in r["limitations"])


def test_impact_uses_reviewed_traversal(setup, gemini_on):
    st, gid = setup
    calls = gemini_on([GOOD])
    r = eng.AskEngine(st, gid).ask("What is impacted if I change the orders table?")
    assert r["query_plan"]["intent"] == "IMPACT" and not calls
    assert "GET /api/orders" in r["answer"] and "IMPACT" not in (r["cypher"] or {}).get("source", "")
    assert r["cypher"]["query"].startswith("MATCH (t:Entity {graph_id: $gid})")


def test_neo4j_error_triggers_repair(setup, gemini_on):
    st, gid = setup
    FakeNeo4j.canned = {"BROKEN": RuntimeError("Neo.ClientError.Statement.SyntaxError: Invalid input"),
                        "OrderDao.insert": [{"caller": "place", "caller_id": "method:x"}]}
    gemini_on(["MATCH (n:Entity {graph_id: $gid}) WHERE n.name = 'BROKEN' RETURN n.name AS name", GOOD])
    r = eng.AskEngine(st, gid).ask("Who calls OrderDao.insert?")
    assert r["status"] == "ANSWERED" and "SyntaxError" in r["cypher"]["repaired_from"][0]["error"]


def test_strings_with_slashes_are_not_comments():
    q, _ = validate_cypher("MATCH (f:File {graph_id: $gid}) WHERE f.file CONTAINS 'http://x' RETURN f.name AS name")
    assert "RETURN f.name" in q
    with pytest.raises(CypherRejected):
        validate_cypher("MATCH (f:File {graph_id: $gid}) WHERE f.name = 'a' SET f.name = 'b // x' RETURN f")
