# Accuracy evaluation — RepoGraph (12 Oct 2026)

Everything below can be re-run from the repo (commands in §6). Numbers come from real runs, not estimates.

## 1. Results at a glance

| What was measured | How | Result |
|---|---|---|
| Extraction on the labelled fixture (Java + Python + SQL + OpenAPI) | `scripts/evaluate.py` against `tests/fixtures/shop_expected.py` (70 expected edges, 2 must-not-exist edges) | **P = 1.00, R = 1.00, F1 = 1.00** for all 10 relationship types; 8/8 APIs; 0 forbidden edges |
| Extraction precision on real repos | 119 randomly sampled edges (seed 7) from 3 public repos, each checked by hand against the code | **114/119 = 95.8 % before the fix → 119/119 after** (the 5 errors were all one bug, fixed below) |
| Fresh re-check after the fix | 24 new random TESTS edges (seed 99) from the two Java repos | **24/24 correct** |
| REST endpoint → handler coverage | APIs found vs. APIs with a `HANDLED_BY` edge | **100 % of implemented endpoints** across 5 repos (see §3) |
| Natural-language questions: dev set | 50 labelled questions (fixture repo) | intent 50/50, answer 50/50 (baseline before this work: 33/50) |
| Natural-language questions: held-out set | 26 questions written before tuning, run once | first blind run 22/26 → 26/26 after fixes (2 were real bugs, 2 were wrong labels) |
| Natural-language questions: frozen blind set | 20 questions on spring-petclinic, file hash-locked (`petclinic_blind.sha256`) before the first run | first run intent 20/20, answer **19/20**; 20/20 after the JPA-cascade fix |
| Unit and API tests | `pytest` | 102 passed, 2 skipped (the skipped ones need a live Neo4j) |

## 2. Real-repository precision (manual review)

The sample files `docs/eval/sample_*.json` hold every reviewed edge with its file, line, snippet, `label` and `note`.

| Relationship | Reviewed | Correct (before fix) | Correct (after fix) |
|---|---|---|---|
| HANDLED_BY (API → handler) | 15 | 15 | 15 |
| CALLS | 15 | 15 | 15 |
| EXECUTES (code → SQL) | 10 | 10 | 10 |
| READS (→ table) | 15 | 15 | 15 |
| WRITES (→ table) | 14 | 14 | 14 |
| TESTS | 10 | **5** | 10 |
| MAPS_TO (class → table) | 10 | 10 | 10 |
| FOREIGN_KEY | 9 | 9 | 9 |
| OVERRIDES | 11 | 11 | 11 |
| INSTANTIATES | 10 | 10 | 10 |
| **Total** | **119** | **114 (95.8 %)** | **119 (100 %)** |

Repos sampled: spring-petclinic-rest (50 edges), spring-boot-realworld (40), django-realworld (29).

> Honest caveat: "100 % after" is measured on the sample that revealed the bug. That is why a **fresh** sample of
> 24 TESTS edges was drawn after the fix (24/24 correct). The 95 % confidence interval for the overall precision
> on 119 samples is roughly 97–100 %, not "perfect".

## 3. Coverage and performance on real repositories

| Repo | Languages | Files | Nodes | Relationships | APIs | Verified edges | Analysis time* |
|---|---|---|---|---|---|---|---|
| spring-petclinic | Java, SQL | 57 | 465 | 918 | 17 | 96 % | 0.3 s |
| spring-petclinic-rest | Java, SQL, OpenAPI | 118 | 1,086 | 2,487 | 38 | 96 % | 0.6 s |
| spring-boot-realworld | Java, SQL (MyBatis) | 128 | 725 | 1,722 | 19 | 99 % | 0.3 s |
| django-realworld | Python | 44 | 214 | 303 | 20 | 98 % | 0.1 s |
| fastapi-realworld | Python, SQL (aiosql) | 100 | 442 | 1,157 | 19 | 100 % | 0.2 s |
| microblog (Flask) | Python | 34 | 261 | 461 | 35 | 95 % | 0.1 s |
| mall-swarm (microservices) | Java, SQL | 638 | 18,032 | 32,087 | 238 | 98 % | 3.8 s |
| commons-lang (library) | Java | 629 | 12,751 | 34,776 | 0 | 74 % | 6.2 s |

\*Parsing time on an already-cloned repo; cloning from GitHub adds network time.

"Verified" means the edge was resolved without ambiguity. The rest are CANDIDATE edges: they are shown dashed in the
UI, always carry a `reason`, and count at 0.6× in impact scores. commons-lang is low (74 %) because a library has many
overloaded methods with the same name, and the analyzer does not guess between overloads (see §4, L-2).

Endpoints without a handler, checked one by one:
* petclinic-rest `GET /oops`: documented in openapi.yml, but no Java method implements it → correctly **no** handler edge.
* django-realworld `ANY /admin`: Django's built-in admin site (library code, not in the repo) → correctly no handler.

## 4. Loopholes: fixed now, improved now, left for later

### A. Fixed now (a real defect, fixed and covered by a test)

| # | Loophole | Effect before | Fix | Proof |
|---|---|---|---|---|
| F-1 | Tests that only **stub a mock** (`given(mockService.find())`, `when(...)`, `verify(...)`) were counted as testing the real method | Impact said "covered by test X" when the method is never run by that test (5/119 sampled edges wrong) | A call whose receiver is a field annotated `@Mock`, `@MockBean` or `@MockitoBean`, or assigned `mock(X.class)`, makes no TESTS edge. This includes fields inherited from a test base class. `@Spy` still counts, because spies run real code. | New fixture test `stubsMockedService` plus a must-not-exist rule in `shop_expected.py`; the rule fails without the fix. All 5 wrong edges are gone, no correct edge was lost, and 24/24 fresh edges are correct. |
| F-2 | JPA **cascade** writes missed (`ownerRepository.save(owner)` also writes `pets`/`visits`) | Blind question "what is impacted if the visits table changes" missed the save path | CANDIDATE `WRITES` edges of kind `JPA_CASCADE` follow `cascade = ALL/PERSIST/MERGE` (depth ≤ 4); `@OneToMany @JoinColumn` creates the FOREIGN_KEY | Blind set 19/20 → 20/20 |
| F-3 | FOREIGN_KEY ignored by impact | Changing a parent table did not flag child tables | FOREIGN_KEY added to the dependency edges | held-out question now passes |
| F-4 | HTTP verb in a question ("who handles DELETE /x") was read as "delete data" | Wrong intent | Route text is removed before the intent rules run | dev/held-out sets |
| F-5 | Interface + implementation both matched a name → "choose one" | Needless clarification question | Interface/impl pairs linked by OVERRIDES are grouped as one answer | dev set |
| F-6 | Diagnostics flooded by statically-imported helper calls (`assertThat`, `given`) | Quality page looked broken on test-heavy repos | Implicit-receiver misses are counted, not logged | Quality page |
| F-7 | Repo summary listed test frameworks first, called DTOs "services" and an error DTO a "controller" | Misleading overview | Framework ranking and stricter layer rules; "Mapper" counts as data access only if it runs SQL | Overview on 6 repos checked by eye |

### B. Improved now (mitigated; a full fix needs deeper analysis)

| # | Loophole | Why it can't be fully fixed now | What is in place |
|---|---|---|---|
| I-1 | Overloaded methods with the same argument count (`append(int)` vs `append(String)`) | Needs expression type inference for every argument | Up to 3 candidate targets, status CANDIDATE, reason "ambiguous overloads"; dashed in the UI; 0.6× weight in impact |
| I-2 | Python dynamic calls (`obj.method()` where `obj`'s type is unknown) | Python has no static types; executing code is forbidden | If exactly one method in the repo has that (uncommon) name, a CANDIDATE edge with confidence 0.4 and an explicit reason; common names (`get`, `save`, …) are never linked |
| I-3 | Misspelled or partial names in questions | Open-ended | Fuzzy match (≥ 0.8 similarity) with a visible note "interpreted X as Y"; otherwise suggestions |
| I-4 | Gemini down, rate-limited (free tier) or writes unsafe Cypher | External service | Validator → one self-repair try → reviewed template; the answer says which path was used. Impact, flow and paths always use reviewed traversals. |
| I-5 | Very large repos clutter the graph | Thousands of nodes | Overview mode above 40 APIs (module-level aggregation), node caps, focus/expand on click |
| I-6 | "Potential" impact (shared tables, same-name methods) is not proven by code | It is a heuristic by nature | Shown in a **separate** table labelled *potential*, never mixed with evidence-backed impact |

### C. Left for later (with reasons)

| # | Gap | Why later |
|---|---|---|
| L-1 | Live run on **Neo4j Desktop** | You asked to take this up later. The Neo4j store and the Cypher validator are implemented and unit-tested; the 2 skipped tests are exactly the live-Neo4j ones. |
| L-2 | Overload resolution by argument types (raises commons-lang from 74 % verified) | Needs a type checker per language; about 2–3 days of work with a high regression risk right before the deadline. Ambiguity is already flagged honestly. |
| L-3 | Reflection, Spring AOP, event listeners, message queues (Kafka/RabbitMQ), scheduled jobs | Not visible as direct calls; each needs its own framework rule. They are listed as known blind spots in the manual. |
| L-4 | SQL built by string concatenation across several methods | Only constant and same-method strings are resolved; tracking values across methods needs data-flow analysis |
| L-5 | Front-end code (JS/TS) in the request flow | Outside the 4 target languages; the flow starts at the REST endpoint. Cross-language HTTP calls from Java/Python clients *are* linked. |
| L-6 | Python test doubles (`unittest.mock.patch`) | No sampled repo had such tests; the Java rule F-1 is the pattern to copy once a real case appears |
| L-7 | Bigger blind NLP set written by other developers | Our question sets were written by the same team that built the parser; a third-party set would give a fairer score |

## 5. How to read "accuracy" honestly
* Fixture scores (1.0) show the analyzer does what it was designed to do; they do **not** prove real-world accuracy.
* Real-world precision comes from hand-checked random samples (§2). Recall on real repos is only measured for
  endpoints (§3); measuring recall for calls would need a full manual call graph.
* NLP numbers: the blind set was frozen by hash **before** the first run, and its first-run score (19/20) is the
  official one. Later fixes are reported separately.

## 6. Re-run everything
```bash
python -m pytest -q                                   # 102 passed, 2 skipped
python scripts/evaluate.py                            # fixture P/R/F1
python scripts/eval_nlp.py all                        # dev + held-out NLP sets
python scripts/eval_nlp.py petclinic_blind <path-to-spring-petclinic>
python scripts/sample_edges.py <repo path> 5 out.json # draw a new random sample to review
```
