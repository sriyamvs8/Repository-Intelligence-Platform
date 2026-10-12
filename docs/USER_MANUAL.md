# Repository Knowledge Graph — User Manual

This tool reads a code repository and draws a **map of how the code is connected**:
which URL (API) runs which method, which method calls which, and which database table gets read or changed.
You can explore the map by clicking, or just **ask questions in plain English**.

Open it at **http://127.0.0.1:8000** after starting the server.

---

## 1. The screen at a glance

```
┌──────────────── Top bar: paste repo link · Analyze · pick a repo · status badges ───────────────┐
│ LEFT PANEL            │            CENTER: the graph (map)            │ RIGHT PANEL           │
│ Repository summary    │                                               │ Ask  · Details ·      │
│ Search                │   coloured shapes = code parts                │ Diagnostics           │
│ View buttons          │   arrows = connections                        │                       │
│ Named queries         │                                               │ (answers, info about  │
│ Filters               │                                               │  what you clicked)    │
└───────────────────────┴───────────────────────────────────────────────┴───────────────────────┘
```

---

## 2. Analyze a repository (first thing to do)

1. Paste a **public GitHub link** in the top box, e.g. `https://github.com/spring-petclinic/spring-petclinic-rest`
2. (Optional) type a branch name in the second box. Leave empty for the main branch.
3. Click **Analyze**. A progress bar shows the steps (cloning → parsing → linking). Small repos take a few seconds.
4. When finished, the repo appears in the **"Analyzed repositories"** dropdown and the map loads.

> Already analyzed before? It loads instantly from cache. Tick **force** to analyze again.
> The tool never runs the repository's code — it only reads it.

---

## 3. What the shapes and colours mean

| Shape / colour | What it is |
|---|---|
| 🟧 Orange hexagon | **API endpoint** (a URL like `GET /owners/{id}`) |
| 🟪 Purple circle | **Method / function** |
| 🟦 Blue rounded box | **Class** (dashed border = Interface) |
| 🟩 Green cylinder | **Database table** |
| 🟨 Yellow diamond | **SQL query** |
| 🩷 Pink triangle | **Test** |
| Grey shapes | Files and folders |

**Arrows** point from "the one who uses" → "the thing being used".

| Arrow colour | Meaning |
|---|---|
| Orange | API is **handled by** this method |
| Light blue | method **calls** another method |
| Green | **reads** a table |
| Dark orange | **writes / changes** a table |
| Pink | **tests** this |
| Red dotted | one service **calls another service's API** |

**Solid line = confirmed.  Dashed line = "probably" (CANDIDATE)** — the tool found good evidence but not 100% proof.
Click any arrow to see *why* it exists.

---

## 4. The three things you will use most

### A) Ask a question (easiest)
Right panel → **Ask** tab → type → press **Enter**.

Good questions to copy:

| You want to know… | Type this |
|---|---|
| Which code runs a URL | `Which method handles GET /owners/{id}?` |
| Which tables a URL touches | `Which tables are touched by POST /owners?` |
| Who uses a method | `Who calls findById?` |
| What a method uses | `What does processCreationForm call?` |
| **What breaks if I change something** | `What is impacted if I change the owners table?` |
| Tests for something | `Which tests cover OwnerController?` |
| Table columns | `What columns does the owners table have?` |
| Where something lives | `Where is OwnerService defined?` |
| How two things connect | `How is OwnerController connected to the owners table?` |
| Lists | `list all endpoints` · `show all tables` · `give me an overview` |

How to read the answer:
- **Green label ANSWERED** = found in the code map. **Yellow** = not enough evidence / please pick one. **Red** = not found.
- The **map in the center redraws** to show only the things in the answer.
- **Cypher** box = the database query used (useful for your report).
- **Supporting facts** = each fact with the file name and line number. Open that file to double-check.
- If it says *"More than one entity matches"*, click the right one from the buttons shown.
- Follow-up questions work: after asking about `OwnerDao`, you can ask `what methods does it contain?`

### B) Search and click
1. Left panel → **Search** → type a name (class, method, `/url`, table).
2. Click a result → the map shows **that item and its neighbours**.
3. Right panel → **Details** tab shows: file + line, and every connection grouped (who calls it, what it calls…).
4. Buttons in Details:
   - **Focus neighbourhood** – show only things around it
   - **Impact analysis** – what may be affected if you change it
   - **Callers** / **Dependencies** / **Tests** / **API → database**

### C) Impact analysis (before changing code)
Click the item → **Impact analysis**, or ask `What is impacted if I change X?`
You get a list grouped as **APIs, methods, tests, SQL, tables** that are connected to X.
Use it as a **review checklist** — it means "check these", not "these will definitely break".

---

## 5. View buttons (left panel)

| Button | Shows | Use it when |
|---|---|---|
| **Architecture** | API → method → … → table paths. Big repos show a **summary**: one box per controller class → tables | You want the big picture |
| **Structure** | folders and files | You want to know where files are |
| **Focus** | only the selected item and its neighbours (choose **Hops** 1–3 = how far to spread) | You are studying one thing |
| **X-lang** | calls from one service/language to another | Microservices / Java ↔ Python |

---

## 6. If the map looks crowded ("like an ocean")

Do these, in this order:
1. **Don't start from the full map.** Search one thing and use **Focus** with **Hops = 1**.
2. **Filters** (left panel, bottom): click a chip to hide that type, e.g. hide **Test** and **Column**. Click again to show. **reset** brings all back.
3. Untick **structure** so files/folders are hidden.
4. Lower **Max** (e.g. 60) = fewer nodes drawn.
5. Change **Layout**: *Layered* for flows (left → right), *Force* for clusters.
6. **Hover** a node = everything not connected fades out. **Double-click** = focus on it.
7. **Ask a question** instead of browsing — the map then shows only the answer.
8. Toolbar: **⤢** fit to screen, **+ / −** zoom, **⊕** expand the selected node, **⇩** save as PNG.

---

## 7. Diagnostics tab (is the map trustworthy?)

Shows how complete the analysis was:
- files read per language, and how many failed
- how many method calls were linked to repository code (others are library calls, e.g. `String.format`)
- counts of each connection type (confirmed vs probable)
- warnings with file and line

If something is missing in an answer, check here first.

---

## 8. Status badges (top right)

| Badge | Meaning |
|---|---|
| ● in_memory / ● neo4j | where the map is stored (green = OK) |
| rule-based NLP | questions understood by built-in rules |
| Gemini planner | Gemini helps understand questions |
| **Gemini → Cypher** | Gemini writes database queries (needs Neo4j + Gemini key) |

---

## 9. Common problems

| Problem | What to do |
|---|---|
| "Repository could not be acquired" | Link must be a **public** GitHub/GitLab/Bitbucket repo |
| Answer says NOT FOUND | Check spelling, or search the name first and click it, then ask "who calls it?" |
| Answer says INSUFFICIENT EVIDENCE | The code uses a pattern the tool can't see statically (e.g. reflection). It refuses to guess — that is intentional |
| An API has no handler | It's only in the API spec file, or created at build time |
| Gemini note "unavailable" | Free quota reached — wait one minute; answers still work |
| Map empty | Pick a repo from the dropdown, or switch view to Architecture |

---

## 10. Supported code

- **Java**: Spring Boot / JAX-RS controllers, JDBC, JPA, Spring Data, MyBatis
- **Python**: Flask, FastAPI, Django / DRF, SQLAlchemy, SQLModel, Django ORM
- **SQL**: `.sql` files and SQL written inside code
- **REST**: annotations/decorators and OpenAPI/Swagger files

Other languages (JavaScript, HTML, Go…) in a repo are ignored.
