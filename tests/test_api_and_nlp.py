import time

import pytest
from tests.conftest import FIXTURE
from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


@pytest.fixture(scope="module")
def gid():
    j = client.post("/api/analyze", json={"repo_url": FIXTURE}).json()
    for _ in range(300):
        s = client.get(f"/api/jobs/{j['job_id']}").json()
        if s["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert s["status"] == "done", s
    return s["graph_id"]


def ask(gid, q, **kw):
    r = client.post(f"/api/graph/{gid}/ask", json={"question": q, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_health():
    assert client.get("/api/health").json()["status"] == "ok"


def test_bad_inputs(gid):
    assert client.post("/api/analyze", json={"repo_url": "https://evil.example/a/b"}).status_code == 400
    assert client.get("/api/graph/../../etc/summary").status_code == 404
    assert client.get("/api/graph/gdeadbeef000/summary").status_code == 404
    assert client.get(f"/api/graph/{gid}/query/drop_everything").status_code == 400
    assert client.get(f"/api/graph/{gid}/view", params={"mode": "hops"}).status_code == 400


def test_views_are_bounded(gid):
    for mode in ("ecosystem", "architecture", "cross-language"):
        v = client.get(f"/api/graph/{gid}/view", params={"mode": mode, "max_nodes": 10}).json()
        assert v["node_count"] <= 10
    v = client.get(f"/api/graph/{gid}/view", params={"mode": "hops", "root_id": "api:GET /api/orders", "hops": 3,
                                                     "max_nodes": 25}).json()
    assert 1 < v["node_count"] <= 25


def test_named_queries(gid):
    for name in ("apis_and_handlers", "classes_and_methods", "call_graph", "sql_and_tables", "tables_and_columns",
                 "tests_and_targets", "inheritance", "api_to_database_path", "cross_language"):
        r = client.get(f"/api/graph/{gid}/query/{name}")
        assert r.status_code == 200, name
    r = client.get(f"/api/graph/{gid}/query/impact_of", params={"root_id": "table:orders"}).json()
    assert "RestApi" in r["affected"]


LABELLED = [
    ("Which method handles GET /api/orders/{id}?", "API_HANDLER", "OrderController.get"),
    ("Which tables are touched by POST /api/orders?", "API_TO_DATA", "audit_log"),
    ("what database tables does DELETE /api/orders/{id} write", "API_TO_DATA", "orders"),
    ("Who calls reserve_stock?", "CALLERS", "routes.reserve"),
    ("What calls OrderDao.insert?", "CALLERS", "OrderServiceImpl.place"),
    ("What does OrderServiceImpl.place call?", "CALLEES", "InventoryClient.reserve"),
    ("What is impacted if I change the orders table?", "IMPACT", "GET /api/orders"),
    ("What might break if we modify StockRepository.decrement?", "IMPACT", "POST /inventory/reserve"),
    ("Which tests cover reserve_stock?", "TEST_COVERAGE", "test_reserve_stock_rejects_missing"),
    ("What columns does the items table have?", "SCHEMA", "warehouse_id"),
    ("Which code writes the orders table?", "TABLE_USAGE", "OrderDao.insert"),
    ("Where is OrderServiceImpl defined?", "LOCATE_SYMBOL", "OrderServiceImpl.java"),
    ("list all endpoints", "LIST_APIS", "/inventory/low-stock"),
    ("show all tables", "LIST_TABLES", "stock_events"),
    ("What methods does OrderDao contain?", "LIST_METHODS", "updateStatus"),
    ("What does OrderServiceImpl depend on?", "DEPENDENCIES", "OrderDao"),
    ("How is OrderController connected to the customers table?", "PATH", "customers"),
    ("Which tables does StockRepository.low_stock read?", "SQL_TABLES", "items"),
    ("give me an overview of this repository", "SUMMARY", "endpoints"),
    ("Which tables are used by GET /inventory/items/{sku}?", "API_TO_DATA", "items"),
]


@pytest.mark.parametrize("q,intent,expect", LABELLED)
def test_labelled_questions(gid, q, intent, expect):
    r = ask(gid, q)
    assert r["query_plan"]["intent"] == intent, (q, r["query_plan"])
    assert r["status"] == "ANSWERED", (q, r["answer"])
    assert expect in r["answer"], (q, r["answer"])


def test_cross_language_path(gid):
    r = ask(gid, "Which tables are touched by POST /api/orders?")
    assert "items" in r["answer"]          # Java -> HTTP -> Python -> SQL -> items
    assert all(f["validation_status"] in ("VERIFIED", "CANDIDATE") for f in r["facts"])


def test_abstains_instead_of_guessing(gid):
    r = ask(gid, "Which method handles POST /api/refunds?")
    assert r["status"] == "INSUFFICIENT_EVIDENCE"         # documented in OpenAPI, no handler in source
    r = ask(gid, "Who calls launchRocketToMars?")
    assert r["status"] == "NOT_FOUND"
    r = ask(gid, "write me a poem about cats")
    assert r["status"] in ("UNSUPPORTED", "NOT_FOUND")


def test_ambiguous_name_asks_to_clarify(gid):
    r = ask(gid, "Which tables does low_stock read?")      # routes.low_stock and StockRepository.low_stock
    assert r["status"] == "CLARIFY" and len(r["candidates"]) == 2


def test_follow_up_pronoun(gid):
    first = ask(gid, "Where is OrderDao defined?")
    eid = first["resolved_entities"][0]["id"]
    r = ask(gid, "what methods does it contain?", context_entity_id=eid)
    assert r["status"] == "ANSWERED" and "findAll" in r["answer"]


def test_every_fact_is_in_graph(gid):
    export = client.get(f"/api/graph/{gid}/export").json()
    ids = {e["id"] for e in export["entities"]}
    for q, _, _ in LABELLED:
        r = ask(gid, q)
        for f in r["facts"]:
            if f.get("subject_id"):
                assert f["subject_id"] in ids
            if f.get("object_id"):
                assert f["object_id"] in ids


def test_feedback_and_diagnostics(gid):
    assert client.post(f"/api/graph/{gid}/ask/feedback", json={"question_id": "x", "helpful": True}).json()["saved"]
    d = client.get(f"/api/graph/{gid}/diagnostics").json()
    assert "report" in d and d["report"]["apis_total"] == 8
