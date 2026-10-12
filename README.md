# Repository Knowledge Graph (RKG)

Evidence-backed knowledge graph for **Java · Python · SQL · REST/OpenAPI** repositories, with bounded graph
exploration and **graph-grounded natural-language questions** (Gemini optional, deterministic fallback built in).

> Paste a public GitHub URL → the system clones it (never executes it), parses the code, links
> `REST endpoint → handler → calls → SQL/ORM → table`, stores the graph (in-memory or Neo4j), and answers questions such as
> *"Which tables are touched by POST /owners/new?"* or *"What is impacted if I change the orders table?"* — citing file:line evidence for every fact.

```
 GitHub URL ─► acquire (safe shallow clone) ─► scan ─► analyzers ─► resolver ─► validator ─► store ─► queries ─► UI / API / NLP
                                                  │  Java (tree-sitter)       symbol index    ontology     in-memory   bounded views
                                                  │  Python (ast)             receiver types  evidence     or Neo4j    impact, paths
                                                  │  SQL (sqlparse+tokens)    imports/MRO     REJECTED     (parity)    API→DB
                                                  │  OpenAPI / MyBatis XML    HTTP matching   report
 Question ─► QueryPlan (Gemini structured output OR rule planner) ─► validate ─► allowlisted read-only query ─► facts ─► answer (+grounding check)
```

## What is extracted

| Area | Supported patterns | Edges |
|---|---|---|
| **Java** | classes, interfaces, enums, records, methods, constructors, fields, imports, generics, text blocks (tree-sitter, error tolerant) | `DEFINES` `IMPORTS` `EXTENDS` `IMPLEMENTS` `OVERRIDES` `CALLS` `INSTANTIATES` |
| **Java REST** | Spring `@RestController/@RequestMapping/@GetMapping…`, JAX-RS `@Path/@GET`, Feign clients, openapi-generator interface-only controllers (CANDIDATE) | `HANDLED_BY` `HTTP_CALLS` |
| **Java data** | JDBC/JdbcTemplate/JPA `createQuery`/`createNativeQuery`, `@Query`, MyBatis XML + annotations, Spring Data derived & inherited CRUD methods, `@Entity/@Table/@Column/@JoinColumn` | `EXECUTES` `READS` `WRITES` `MAPS_TO` `HAS_COLUMN` `FOREIGN_KEY` |
| **Python** | modules, classes, functions, methods, imports (absolute/relative), MRO, `self.x` attribute types, annotations | `CALLS` `INSTANTIATES` `EXTENDS` `OVERRIDES` `IMPORTS` |
| **Python REST** | Flask (`route`, blueprints + `url_prefix`, `register_blueprint`), FastAPI (`APIRouter(prefix)`, `include_router`), Django `urls.py` + `include()`, class-based views, DRF routers, Connexion `operationId` | `HANDLED_BY` |
| **Python data** | DB-API `execute`, SQLAlchemy `text()`, `select/insert/update/delete(Model)`, `session.add`, Flask-SQLAlchemy `Model.query`, Django ORM `objects.*`, SQLModel `table=True`, f-string SQL (CANDIDATE) | `EXECUTES` `READS` `WRITES` `MAPS_TO` `HAS_COLUMN` `FOREIGN_KEY` |
| **SQL** | `.sql` scripts: CREATE TABLE/VIEW/INDEX, ALTER (columns, FKs), DML with joins, sub-queries, CTEs (excluded), MERGE, aliases | `DEFINES` `READS` `WRITES` `HAS_COLUMN` `FOREIGN_KEY` |
| **OpenAPI** | Swagger 2 / OpenAPI 3 (JSON/YAML), `basePath`/`servers` prefix — contract only, never implies a handler | `DEFINES` |
| **Tests** | JUnit `@Test`… , pytest/unittest, MockMvc/RestAssured/TestClient requests | `TESTS` |
| **Cross-language** | RestTemplate/WebClient/Feign/requests/httpx calls matched to endpoints of another language in the repo | `CROSS_LANGUAGE_MAP` |

Every edge has `status` (**VERIFIED** / **CANDIDATE**), `source_kind` (mechanism), `evidence`, `file`, `line`, `reason`.
Edges that violate the ontology are **REJECTED** and only shown in diagnostics.

## Quick start (Windows PowerShell)

```powershell
cd repo-knowledge-graph
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
copy .env.example .env          # GRAPH_BACKEND=in_memory works without any database
python -m pytest -q              # 53 passed, 1 skipped (Neo4j parity needs a running Neo4j)
python scripts\evaluate.py       # precision / recall on the labelled fixture
python -m uvicorn backend.app:app --reload
```
Open **http://127.0.0.1:8000** (UI) and **http://127.0.0.1:8000/docs** (Swagger UI of the API).
Linux/macOS: `python3 -m venv .venv && source .venv/bin/activate` and the same commands.

### With Neo4j
```bash
docker compose up -d neo4j        # browser at http://localhost:7474 (neo4j / change-me-please)
# .env: GRAPH_BACKEND=neo4j, NEO4J_PASSWORD=change-me-please
NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_PASSWORD=change-me-please python -m pytest tests/test_store_parity.py
```
Whole stack in Docker: `docker compose up --build` → http://localhost:8000.

### With Gemini (optional)
Put `GEMINI_API_KEY=...` in `.env`. Gemini is used **only** to (1) fill a schema-validated `QueryPlan` and (2) phrase an
explanation that must pass a grounding check (every backticked entity/file must exist in the returned facts).
On any error/quota problem the system falls back to the deterministic planner for 60 s (circuit breaker).

### CLI
```bash
python scripts/cli.py analyze https://github.com/spring-projects/spring-petclinic
python scripts/cli.py list
python scripts/cli.py ask <graph_id> "Which tables are touched by POST /owners/new?"
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | backend, NLP mode, auth |
| POST | `/api/analyze` `{repo_url, ref?, force?}` | start background analysis → `job_id` (cached per commit) |
| GET | `/api/jobs/{job_id}` | progress / `graph_id` / error |
| GET | `/api/graphs` | analyzed repositories |
| GET | `/api/graph/{gid}/summary` · `/export` · `/schema` · `/diagnostics` | report, full export, vocabulary, quality |
| GET | `/api/graph/{gid}/search?q=` | ranked entity search (exact qualified > name > substring) |
| GET | `/api/graph/{gid}/view?mode=architecture\|ecosystem\|hops\|cross-language` | bounded projections (`max_nodes`, `hops` ≤ 3) |
| GET | `/api/graph/{gid}/node?entity_id=` | entity + grouped relationships with evidence |
| GET | `/api/graph/{gid}/query/{name}` | `apis_and_handlers`, `api_to_database_path`, `sql_and_tables`, `tables_and_columns`, `call_graph`, `classes_and_methods`, `inheritance`, `tests_and_targets`, `dependencies_of`, `impact_of`, `cross_language` |
| GET | `/api/graph/{gid}/impact?root_id=` · `/path?from_id=&to_id=` | impact analysis, shortest path |
| POST | `/api/graph/{gid}/ask` `{question, context_entity_id?}` | graph-grounded answer: `status`, `answer`, `facts`, `paths`, `graph`, `query_plan`, `planner` |
| POST | `/api/graph/{gid}/ask/feedback` | helpful / not helpful (evaluation data) |

Answer `status`: `ANSWERED`, `INSUFFICIENT_EVIDENCE`, `NOT_FOUND`, `CLARIFY` (ambiguous → candidates), `UNSUPPORTED`.
Intents: `LOCATE_SYMBOL, LIST_METHODS, CALLERS, CALLEES, API_HANDLER, API_TO_DATA, SQL_TABLES, SCHEMA, TABLE_USAGE,
DEPENDENCIES, IMPACT, TEST_COVERAGE, PATH, LIST_APIS, LIST_TABLES, SUMMARY`.

## Measured results (12 Oct 2026)

**Labelled fixture** (`tests/fixtures/shop`: Spring + JDBC + MyBatis + OpenAPI, FastAPI + SQLAlchemy, SQL schema, cross-language HTTP):
precision = recall = F1 = **1.00** for HANDLED_BY, CALLS, OVERRIDES, IMPLEMENTS, data access, HAS_COLUMN, FOREIGN_KEY,
MAPS_TO, CROSS_LANGUAGE_MAP, TESTS; 20/20 labelled NL questions answered with the correct intent. The fixture covers
the supported patterns by design — it proves the rules work, not that every real repository is handled.

**Real public repositories** (analysis time, single thread):

| Repository | Files scanned | Entities | Endpoints with handler | Notes |
|---|---|---|---|---|
| spring-projects/spring-petclinic | 64 | 465 | 17/17 | JPA + Spring Data, MockMvc tests linked to endpoints — 0.2 s |
| spring-petclinic/spring-petclinic-rest | 123 | 1 086 | 37/38 | openapi-generator controllers linked as CANDIDATE — 0.4 s |
| miguelgrinberg/microblog | 34 | 257 | 35/35 | Flask blueprints + SQLAlchemy — 0.1 s |
| pallets/flask (examples + tests) | 88 | 1 098 | 35/35 | raw sqlite SQL in tutorial app — 0.5 s |
| fastapi/full-stack-fastapi-template | 52 | 294 | 23/23 | SQLModel `table=True` only — 0.1 s |
| gothinkster/django-realworld-example-app | 44 | 214 | 19/20 | Django ORM, abstract base models — 0.04 s |
| mybatis/jpetstore-6 | 71 | 724 | 22/22 | MyBatis XML mappers → tables — 0.3 s |
| macrozheng/mall-swarm | 696 | 18 032 | 238/238 | 104 MyBatis mappers, 685 WRITES — 3.3 s, 281 MB |
| apache/commons-lang | 662 | 12 751 | – | 6 680 calls, 11 561 test links — 5.0 s |

"Files scanned" counts Java/Python/SQL plus JSON/YAML/XML candidates (non-spec/non-mapper ones are dropped).
These are coverage/performance figures; precision on real repositories should be measured by manually labelling a
sample of edges (see `scripts/evaluate.py` for the method).

## Project layout
```
backend/
  app.py                 FastAPI app (security middleware, rate limits, endpoints, static UI)
  config.py              all settings from environment / .env
  kg/model.py            canonical entities, relationships, ontology (allowed endpoints), IDs
  kg/acquire.py          safe clone: https only, host allowlist, no hooks/symlinks/prompts, timeout, size cap
  kg/scanner.py          exclusions, .gitignore, symlink & path-traversal guards, size limits
  kg/pipeline.py         orchestration, validator, quality report
  analyzers/             java_analyzer.py, python_analyzer.py, sql_analyzer.py, spec_analyzers.py, common.py
  graph/store.py         store interface + in-memory backend (gzip JSON persistence)
  graph/neo4j_store.py   Neo4j backend (parameterised templates, allowlisted labels/types, read-only tx)
  graph/queries.py       bounded projections, impact (0-1 BFS), API→DB, paths, named queries
  nlp/plan.py            QueryPlan schema + deterministic rule planner
  nlp/gemini.py          Gemini structured output + grounding check + circuit breaker
  nlp/engine.py          entity resolution (CLARIFY on ambiguity), intent handlers, answer packaging
  api/jobs.py            background analysis jobs, de-duplication, commit cache
frontend/                index.html, app.js, styles.css, vendor/ (Cytoscape + dagre + fCoSE, MIT, served locally)
tests/                   pytest suite + labelled fixture + ground truth
scripts/evaluate.py      precision/recall/F1 per relationship type
scripts/cli.py           command line
```

## Security
Repository content is untrusted data: nothing is executed; clones are shallow, hook-free, symlink-free, time- and
size-limited and deleted after analysis; only `https://` URLs on `ALLOWED_GIT_HOSTS`; refs validated; XML parsed with
defusedxml; YAML with `safe_load`; graph IDs validated; all Neo4j queries parameterised with allowlisted labels;
the LLM never writes Cypher; optional `API_KEY` header auth; per-IP rate limits; security headers; secrets only in `.env`.

## Known limitations (state these honestly in reviews)
* Static analysis: reflection, dependency-injection by name, dynamic dispatch beyond declared types, runtime route
  registration and generated code that is not committed are not followed (generated-API controllers are CANDIDATE).
* Python call resolution needs a resolvable receiver (self, imports, constructor assignment, annotations); untyped
  parameters are counted as unresolved.
* SQL extraction is conservative token analysis, not a full dialect parser; dynamic SQL is CANDIDATE.
* "Impact" means *connected under static dependency rules* — a review aid, not proof that something breaks.
* The in-memory backend keeps graphs in `DATA_DIR` (gzip JSON); multi-user/multi-instance deployment should use Neo4j.
* Neo4j parity test must be run on a machine with Neo4j (CI workflow does this automatically).
