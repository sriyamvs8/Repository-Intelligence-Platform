# Step-by-step guide (Windows + VS Code + Claude Code) — deadline Wed 14 Oct 2026

## 0. What is already done and verified
| Item | Status |
|---|---|
| Java / Python / SQL / OpenAPI / MyBatis analyzers, resolver, validator | done, tested |
| In-memory graph store (persisted) + named queries, impact, API→DB, paths | done, tested |
| Neo4j store (parameterised templates) | written; **run the parity test on your machine (step 5)** |
| NLP `/ask` (23 intents, plain-English + developer answer, Cypher, 5-step breakdown, fuzzy names) | done; dev 50/50, held-out 26/26, frozen blind 20/20 |
| Gemini → Cypher (validator, self-repair, read-only execution, fallback) | done; 40 validator/routing tests pass with mocked Gemini — **verify live with your key (step 5)** |
| Large-repo overview (handler class → table, grouped by module) | done, tested on 238-endpoint repo |
| Website: navbar with Overview · Explore graph · Ask · Request flow · Impact · Quality, light/dark, mobile | done, browser-tested, 0 console errors |
| Repo summary (4 lines) + architecture lanes | done (Overview page) |
| Node and relationship evidence (file, line, highlighted code, GitHub link, "why?") | done |
| Request flow (client → API → controller → service → data access → SQL → table) | done |
| Impact risk model (blast radius, risk levels, tests to run, heuristic side effects) | done |
| Accuracy report with fixed / improved / later loopholes | done — `docs/EVALUATION.md` |
| Tests: 102 pass · fixture P/R/F1 = 1.0 · real-repo sampled precision 95.8 % → 100 % after fix | verified |

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
python -m pytest -q                     # expect: 102 passed, 2 skipped
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

## 4. Neo4j (needed for Gemini → Cypher) — pick ONE
**A. Neo4j AuraDB Free (no Docker, easiest):** https://console.neo4j.io → New instance → Free → download the
credentials file. In `.env`:
```
GRAPH_BACKEND=neo4j
NEO4J_URI=neo4j+s://<id>.databases.neo4j.io
NEO4J_USER=neo4j
NEO4J_PASSWORD=<from credentials file>
```
**B. Docker Desktop:** `docker compose up -d neo4j`, then `NEO4J_URI=bolt://localhost:7687`, password `change-me-please`.

Verify (must pass):
```powershell
$env:NEO4J_TEST_URI="<your NEO4J_URI>"; $env:NEO4J_TEST_PASSWORD="<password>"
python -m pytest tests\test_store_parity.py -q
```
Restart uvicorn and re-analyze your repos (graphs are stored per backend).

## 5. Gemini key → natural language to Cypher
1. Get a free key: https://aistudio.google.com/apikey → `.env`: `GEMINI_API_KEY=...` and `NLP_MODE=hybrid`.
2. Restart uvicorn. The top-right badge must show **"Gemini → Cypher (gemini-2.5-flash)"**.
3. Ask: "Which endpoints use POST?", "Who calls findById?", "How many methods does each controller have?".
   Open the **Cypher** panel under the answer: it shows the Gemini-generated query that was validated and executed.
4. Ask "What is impacted if I change the owners table?": impact uses the reviewed traversal, and the panel shows its
   equivalent Cypher. Paste any shown query into Neo4j Browser (replace `$gid` with the graph id) to demonstrate it live.
5. Free-tier limits: if you see "Gemini unavailable" in the answer notes, you hit the per-minute quota; wait a minute
   (the system keeps answering from templates meanwhile). `GEMINI_MODEL=gemini-2.5-flash-lite` gives more free requests.

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
**Mon evening:** steps 1–3; analyze spring-petclinic-rest, spring-boot-realworld and django-realworld; read `docs/EVALUATION.md`.
**Tue:** step 4 (Neo4j Desktop), step 5 (Gemini key); rehearse the demo; copy the Cypher tab of 3 answers into Neo4j Browser.
**Wed (before review):** analyze the demo repos BEFORE the review (graphs persist in `data/`); keep `docs/screenshots` as backup.

## 8. Demo script (8 minutes) — repo: spring-petclinic-rest
1. **Overview**: read the 4-line summary; point at the architecture lanes (API → Controller → Service → Data access → Database).
2. **Explore graph**: search `saveOwner` → click it → file, line, highlighted code, GitHub link. Click an arrow → CONFIRMED vs PROBABLE + reason.
3. **Ask**: "Which tables are touched by POST /owners?" → show the Plain English, Developer, Cypher and *How it was answered* tabs.
4. Ask "What is impacted if I change the owners table?", then "Which endpoints have no tests?".
5. Unanswerable: "Who handles POST /refunds?" → the system says NOT FOUND instead of inventing an answer.
6. **Request flow**: POST /owners → lanes + 15 numbered steps with evidence.
7. **Impact**: `ClinicServiceImpl.saveOwner` → blast radius, risk, tests to run; open "How the risk is calculated".
8. **Quality**: what was parsed, what was not; then show `docs/EVALUATION.md` (95.8 % → 100 %, honest loopholes).

## 9. Viva answers (short)
- **Why a graph?** Relationships are explicit and traversable; every edge has evidence and status.
- **Why an LLM if the graph exists?** To understand free-form questions and phrase facts. If it writes Cypher, that Cypher must pass the allowlist validator (read-only, scoped, bounded) and runs in a READ transaction; impact/flow/paths always use reviewed traversals.
- **Missing edge?** System says "insufficient evidence"; we add a parser rule + fixture test, never let the LLM invent it.
- **How accurate?** Fixture P/R/F1 = 1.0; 119 hand-checked real edges: 95.8 % before the mock fix, 100 % after, 24/24 on a fresh sample; NLP blind set 19/20 on first run (see docs/EVALUATION.md).
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
