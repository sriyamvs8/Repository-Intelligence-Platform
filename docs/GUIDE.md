# Step-by-step guide (Windows + VS Code + Claude Code) — deadline Wed 14 Oct 2026

## 0. What is already done and verified
| Item | Status |
|---|---|
| Java / Python / SQL / OpenAPI / MyBatis analyzers, resolver, validator | done, tested |
| In-memory graph store (persisted) + named queries, impact, API→DB, paths | done, tested |
| Neo4j store (parameterised templates) | written; **run the parity test on your machine (step 5)** |
| NLP `/ask` (rule planner, Gemini optional, grounding check, CLARIFY/abstain) | done, 20 labelled questions pass |
| Cytoscape UI (architecture/structure/focus/x-lang, search, filters, evidence, ask, diagnostics) | done, browser-tested |
| Tests: 53 pass · fixture precision/recall/F1 = 1.0 · clean install on Python 3.11 | verified |

## 1. Install tools (once, ~20 min)
1. **Git for Windows** — https://git-scm.com/download/win (defaults are fine).
2. **Python 3.11** — https://www.python.org/downloads/ → tick **"Add python.exe to PATH"**.
3. **VS Code** — https://code.visualstudio.com/
4. **Claude Code extension** — in VS Code press `Ctrl+Shift+X`, search **"Claude Code"** (publisher Anthropic), Install.
   Click the Spark icon (top-right of an open file, or the Activity Bar) → **Sign in** with your Claude account.
5. Optional: **Docker Desktop** (only for Neo4j) — https://www.docker.com/products/docker-desktop/

## 2. Open and run the project (~10 min)
Unzip `repo-knowledge-graph.zip`, then in VS Code: File → Open Folder → `repo-knowledge-graph`.
Open a terminal (`` Ctrl+` ``) and run:
```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
copy .env.example .env
python -m pytest -q                     # expect: 53 passed, 1 skipped
python scripts\evaluate.py              # expect: overall precision 1.0 recall 1.0
python -m uvicorn backend.app:app --reload
```
Open http://127.0.0.1:8000 → click the **spring-petclinic** example button → wait ~5 s.
(Or press F5 and choose "Run API + UI (uvicorn)".)

## 3. Push to your GitHub (5 min)
Create an EMPTY repo on github.com (no README), then:
```powershell
git remote add origin https://github.com/<your-user>/repo-knowledge-graph.git
git push -u origin main
```
The included GitHub Actions workflow runs tests, Neo4j parity and evaluation on every push (Actions tab).

## 4. Gemini (optional, 5 min)
Get a key at https://aistudio.google.com/apikey → put `GEMINI_API_KEY=...` in `.env` → restart uvicorn.
The top-right badge changes from "rule-based NLP" to "Gemini …". Never commit `.env`.

## 5. Neo4j (Tuesday)
```powershell
docker compose up -d neo4j              # http://localhost:7474  user neo4j / change-me-please
$env:NEO4J_TEST_URI="bolt://localhost:7687"; $env:NEO4J_TEST_PASSWORD="change-me-please"
python -m pytest tests\test_store_parity.py -q     # must pass: same results as in-memory
```
Then set `GRAPH_BACKEND=neo4j` and `NEO4J_PASSWORD=change-me-please` in `.env`, restart, re-analyze.
In Neo4j Browser try: `MATCH (a:RestApi)-[:HANDLED_BY]->(m)-[:CALLS*1..4]->(x)-[:READS|WRITES]->(t:Table) RETURN a,m,x,t LIMIT 50`

## 6. Working fast with Claude Code in VS Code
`CLAUDE.md` in the project root tells Claude the architecture, commands and rules automatically.
Good prompts (paste into the Claude panel):
- "Run the tests and the evaluation script and explain any failure."
- "Explain how a POST endpoint is linked to a table, using backend/graph/queries.py::api_to_database."
- "Add support for Spring `@Repository` classes using `NamedParameterJdbcTemplate`. Add a fixture example and expected edges first, then implement, then run evaluate.py."
- "Analyze https://github.com/<repo> with scripts/cli.py and list endpoints without handlers; explain why each one is missing."
Use **Plan** mode (`/plan`) for anything big, review the diff before accepting, and always finish with
`python -m pytest -q` and `python scripts\evaluate.py`. Commit after each green step: `git add -A; git commit -m "..."`.

## 7. Two-day plan
**Mon evening:** steps 1–3; analyze 3 repos (petclinic, petclinic-rest, microblog); read README "Measured results".
**Tue:** step 5 (Neo4j), step 4 (Gemini); label ~30 real edges from petclinic by hand (open file:line evidence in
the Details panel, mark correct/incorrect) → real-repo precision for your report; take screenshots.
**Wed (before review):** rehearse the demo script below twice; keep `docs/screenshots` as backup if Wi-Fi fails
(graphs persist in `data/`, so analyze BEFORE the review).

## 8. Demo script (8 minutes)
1. `/api/health` → backend + NLP mode. 2. Analyze spring-petclinic → summary card (17/17 APIs, 7 tables).
3. Architecture view: API → handler → repository → table. 4. Click an edge → evidence, file:line, VERIFIED/CANDIDATE.
5. Ask: "Which tables are touched by POST /owners/new?" → path + facts. 6. "What is impacted if I change the owners table?"
7. "Which tests cover OwnerController?" 8. Unanswerable: "Which method handles POST /refunds?" → system abstains.
9. Diagnostics tab: quality report (resolved vs external calls, rejected edges). 10. petclinic-rest: CANDIDATE handlers
from openapi-generator convention — explain why they are not VERIFIED.

## 9. Viva answers (short)
- **Why a graph?** Relationships are explicit and traversable; every edge has evidence and status.
- **Why an LLM if the graph exists?** Only to understand the question (QueryPlan) and phrase facts; it never writes Cypher and its text must pass a grounding check.
- **Missing edge?** System says "insufficient evidence"; we add a parser rule + fixture test, never let the LLM invent it.
- **How accurate?** Fixture P/R/F1 = 1.0 per relationship type (designed coverage); real repos: coverage numbers in README + your hand-labelled sample.
- **Never claim:** perfect understanding of any repo, runtime proof of calls, that impact predicts breakage, that Gemini can safely run arbitrary Cypher.

## 10. Troubleshooting
| Problem | Fix |
|---|---|
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `git failed ... not found` | repo is private or URL wrong; only public https GitHub/GitLab/Bitbucket URLs |
| Port 8000 busy | `python -m uvicorn backend.app:app --port 8001` |
| Analyze a local folder | set `ALLOW_LOCAL_PATHS=true` in `.env`, paste the folder path |
| Neo4j auth error | password in `.env` must match `NEO4J_AUTH` in docker-compose |
| Gemini 403/429 | invalid key/quota → system auto-falls back to rules for 60 s |
