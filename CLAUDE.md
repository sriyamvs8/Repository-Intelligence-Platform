# Repository Knowledge Graph — instructions for Claude Code

## What this project is
FastAPI service + Cytoscape UI that builds an evidence-backed knowledge graph of Java / Python / SQL / REST(OpenAPI)
repositories and answers natural-language questions ONLY from graph facts. See README.md for architecture.

## Commands
- Install: `python -m pip install -r requirements.txt`
- Tests (must stay green): `python -m pytest -q`
- Accuracy on labelled fixture (must stay 1.0 for every type): `python scripts/evaluate.py`
- NLP accuracy: `python scripts/eval_nlp.py all` (dev ≥ 0.95, held-out ≥ 0.9; the petclinic blind set is frozen by sha256, never tune on it)
- Full accuracy report: docs/EVALUATION.md
- Run: `python -m uvicorn backend.app:app --reload` → http://127.0.0.1:8000 (UI), /docs (API)
- CLI: `python scripts/cli.py analyze <github-url>` / `python scripts/cli.py ask <gid> "<question>"`

## Non-negotiable rules (from the project handoff)
1. Parsers produce facts, the graph stores relationships, the LLM only interprets questions and explains facts.
2. Never create an edge from name equality alone. Ambiguous → `Status.CANDIDATE` with a `reason`; impossible → not emitted.
3. Every relationship needs `source_kind`, `evidence`, `file`, `line`, `reason`. Allowed endpoint types live in
   `backend/kg/model.py::ALLOWED_ENDPOINTS`; the validator in `backend/kg/pipeline.py` rejects anything else.
4. IDs are built only with `make_id`/`api_id`/`table_id` in `backend/kg/model.py`.
5. OpenAPI operations never imply `HANDLED_BY` (only the documented generator/Connexion conventions in `spec_analyzers.py`).
6. LLM-written Cypher is allowed ONLY through `backend/nlp/cypher.py::validate_cypher` (read-only clauses,
   allowlisted labels/types/properties/functions, `$gid` scoping, bounded `*..6`, LIMIT ≤ 200) and
   `Neo4jStore.run_cypher` (READ session, 10 s timeout). Never bypass or weaken the validator; add a test in
   tests/test_cypher.py for every change. Impact/API→DB/tests/paths stay on reviewed traversals (NLP_MODE=hybrid).
7. Repository code is never executed. Keep acquire.py / scanner.py safety checks intact.
8. Every projection/traversal must stay bounded (max_nodes, depth).

## How to add support for a new framework pattern
1. Add a small example to `tests/fixtures/shop/...` and the expected edges to `tests/fixtures/shop_expected.py`.
2. Implement it in the relevant analyzer (java_analyzer.py / python_analyzer.py / sql_analyzer.py / spec_analyzers.py).
3. Run `python scripts/evaluate.py` and `python -m pytest -q`; precision and recall must not drop.
4. If it is a new NL question type: add an `Intent`, a rule in `rule_plan`, a `_i_<intent>` handler in engine.py,
   and a labelled question in `tests/test_api_and_nlp.py::LABELLED`.

## Layout
backend/kg (model, acquire, scanner, pipeline) · backend/analyzers · backend/graph (store, neo4j_store, queries)
· backend/graph/insights.py (profile, flow, impact report) · backend/nlp (plan, gemini, cypher, engine) · backend/api/jobs.py · backend/app.py · frontend/ · tests/ · scripts/
