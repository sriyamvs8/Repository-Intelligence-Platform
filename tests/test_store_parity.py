"""In-memory vs Neo4j parity. Runs only when NEO4J_TEST_URI is set (e.g. bolt://localhost:7687)."""
import os

import pytest
from tests.conftest import FIXTURE

from backend.config import Settings
from backend.graph.queries import GraphQueries
from backend.graph.store import InMemoryStore
from backend.kg.pipeline import analyze_repository

pytestmark = pytest.mark.skipif(not os.getenv("NEO4J_TEST_URI"), reason="set NEO4J_TEST_URI to run Neo4j parity tests")


def test_parity(tmp_path):
    from backend.graph.neo4j_store import Neo4jStore
    s = Settings()
    s.neo4j_uri = os.environ["NEO4J_TEST_URI"]
    s.neo4j_user = os.getenv("NEO4J_TEST_USER", "neo4j")
    s.neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", "")
    neo, mem = Neo4jStore(s), InMemoryStore(tmp_path)
    res = analyze_repository(FIXTURE)
    for st in (neo, mem):
        st.save_graph(res.graph_id, res.meta, res.report, res.entities, res.relationships, res.diagnostics, res.rejected)
    a, b = GraphQueries(mem, res.graph_id), GraphQueries(neo, res.graph_id)
    assert {e["id"] for e in mem.export(res.graph_id)["entities"]} == {e["id"] for e in neo.export(res.graph_id)["entities"]}
    assert {r["id"] for r in mem.export(res.graph_id)["relationships"]} == {r["id"] for r in neo.export(res.graph_id)["relationships"]}
    for q in ("OrderDao", "orders", "reserve"):
        assert [n["id"] for n in a.search(q)] == [n["id"] for n in b.search(q)]
    for root in ("table:orders", "api:POST /api/orders"):
        assert a.impact_of(root)["total_affected"] == b.impact_of(root)["total_affected"]
    assert a.named("api_to_database_path")["rows"] == b.named("api_to_database_path")["rows"]
    neo.delete_graph(res.graph_id)
    neo.close()


def test_templates_execute_on_real_neo4j():
    """Every reviewed template Cypher must be valid, read-only-executable Cypher on a real Neo4j."""
    from backend.graph.neo4j_store import Neo4jStore
    from backend.nlp.cypher import TEMPLATES, EXAMPLES, validate_cypher
    s = Settings()
    s.neo4j_uri = os.environ["NEO4J_TEST_URI"]
    s.neo4j_user = os.getenv("NEO4J_TEST_USER", "neo4j")
    s.neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", "")
    neo = Neo4jStore(s)
    res = analyze_repository(FIXTURE)
    neo.save_graph(res.graph_id, res.meta, res.report, res.entities, res.relationships, res.diagnostics, res.rejected)
    ids = {"IMPACT": ["table:orders"], "API_TO_DATA": ["api:POST /api/orders"], "PATH": ["api:GET /api/orders", "table:orders"],
           "SCHEMA": ["table:orders"], "TABLE_USAGE": ["table:orders"]}
    for name, q in TEMPLATES.items():
        q2, _ = validate_cypher(q)
        rows = neo.run_cypher(q2, {"gid": res.graph_id, "ids": ids.get(name, ["method:com.shop.repo.OrderDao.insert(Order)"]),
                                   "statuses": ["VERIFIED", "CANDIDATE"]})
        assert isinstance(rows, list), name
    rows = neo.run_cypher(validate_cypher(TEMPLATES["API_TO_DATA"])[0],
                          {"gid": res.graph_id, "ids": ["api:POST /api/orders"], "statuses": ["VERIFIED", "CANDIDATE"]})
    assert {"orders", "audit_log"} <= {r["table"] for r in rows}
    for _, q in EXAMPLES:
        neo.run_cypher(validate_cypher(q)[0], {"gid": res.graph_id})
    # read-only enforcement: Neo4j itself refuses writes in a READ session
    with pytest.raises(Exception):
        neo.run_cypher("CREATE (n:Hack) RETURN n", {})
    neo.delete_graph(res.graph_id)
    neo.close()
