"""NLP regression: intent + answer accuracy on the labelled fixture questions, and the answer structure."""
from tests.conftest import FIXTURE  # noqa: F401
from scripts.eval_nlp import run


def test_nlp_accuracy_dev_and_heldout():
    r = run("all", verbose=False)
    assert r["dev"]["answer_accuracy"] >= 0.95, r["dev"]["failures"]
    assert r["heldout"]["answer_accuracy"] >= 0.9, r["heldout"]["failures"]


def test_answer_structure():
    import tempfile
    from pathlib import Path
    from backend.graph.store import InMemoryStore
    from backend.kg.pipeline import analyze_repository
    from backend.nlp.engine import AskEngine
    st = InMemoryStore(Path(tempfile.mkdtemp()))
    res = analyze_repository(FIXTURE)
    st.save_graph(res.graph_id, res.meta, res.report, res.entities, res.relationships, res.diagnostics, res.rejected)
    for q in ("who calls validate", "what is impacted if I change the orders table", "how many tables",
              "what happens when I call POST /api/orders", "summarize this codebase"):
        r = AskEngine(st, res.graph_id).ask(q)
        assert r["answer_nl"] and r["answer_dev"], q
        assert [b["step"] for b in r["breakdown"]][0] == "Understood the question"
        assert r["cypher"] and r["cypher"]["query"].startswith(("MATCH", "OPTIONAL")), q
