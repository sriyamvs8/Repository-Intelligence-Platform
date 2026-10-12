/* RepoGraph UI — vanilla JS + Cytoscape. Renders bounded projections from the API; never mutates the graph. */
(() => {
  "use strict";
  // ================================================================ utilities
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const md = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  const qs = (o) => Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  const store = {
    get(k, d) { try { const v = localStorage.getItem("rkg." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("rkg." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  function toast(msg, err = false) {
    const t = document.createElement("div"); t.className = "toast" + (err ? " err" : ""); t.textContent = msg;
    document.body.appendChild(t); setTimeout(() => t.remove(), err ? 6000 : 2800);
  }
  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    const key = store.get("apiKey", "");
    if (key) headers["X-API-Key"] = key;
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401) {
      const k = prompt("This server requires an API key (X-API-Key):");
      if (k) { store.set("apiKey", k); return api(path, opts); }
    }
    let body = null; try { body = await res.json(); } catch { body = null; }
    if (!res.ok) throw new Error((body && (body.detail?.[0]?.msg || body.detail)) || `${res.status} ${res.statusText}`);
    return body;
  }

  // ================================================================ vocabulary (plain English)
  const TYPE_COLORS = { Repository: "#f4b860", Directory: "#9aa7b6", File: "#b3c0cf", Class: "#5b9dff", Interface: "#8fb8ff", Enum: "#6fc3df",
    Method: "#9d8cff", Constructor: "#b7a9ff", Function: "#c58cff", RestApi: "#ff8a5b", SqlQuery: "#f2c94c", Table: "#3fcf8e",
    Column: "#86e3b5", Test: "#ff6b9a", Client: "#9aa7b6", Module: "#9aa7b6" };
  const TYPE_SHAPES = { Repository: "star", Directory: "tag", File: "rectangle", Class: "round-rectangle", Interface: "round-rectangle",
    Enum: "round-rectangle", Method: "ellipse", Constructor: "ellipse", Function: "ellipse", RestApi: "round-hexagon", SqlQuery: "diamond",
    Table: "barrel", Column: "ellipse", Test: "round-triangle", Client: "round-tag" };
  const TYPE_TEXT = { Repository: "Repository", Directory: "Folder", File: "Source file", Class: "Class", Interface: "Interface", Enum: "Enum",
    Method: "Method", Constructor: "Constructor", Function: "Function", RestApi: "REST endpoint", SqlQuery: "SQL statement", Table: "Database table",
    Column: "Table column", Test: "Test", Client: "Client" };
  const REL_COLORS = { CONTAINS: "#8a96a6", DEFINES: "#8a96a6", IMPORTS: "#8794a8", CALLS: "#7f9cc7", INSTANTIATES: "#8aa0c8",
    EXTENDS: "#6f9fe8", IMPLEMENTS: "#6f9fe8", OVERRIDES: "#a493f5", HANDLED_BY: "#ff8a5b", EXECUTES: "#e0b030", READS: "#2fbf7f",
    WRITES: "#ff9f43", HAS_COLUMN: "#6fd3a4", FOREIGN_KEY: "#2bb3a3", MAPS_TO: "#4fd1c5", TESTS: "#ff6b9a", HTTP_CALLS: "#ff5d5d",
    CROSS_LANGUAGE_MAP: "#ff3b3b", DATA_ACCESS: "#2fbf7f" };
  const REL_FWD = { CONTAINS: "contains", DEFINES: "defines", IMPORTS: "imports", CALLS: "calls", INSTANTIATES: "creates an instance of",
    EXTENDS: "extends", IMPLEMENTS: "implements", OVERRIDES: "implements / overrides", HANDLED_BY: "is handled by", EXECUTES: "runs SQL",
    READS: "reads table", WRITES: "writes table", HAS_COLUMN: "has column", FOREIGN_KEY: "references (foreign key)", MAPS_TO: "is stored in table",
    TESTS: "tests", HTTP_CALLS: "calls endpoint", CROSS_LANGUAGE_MAP: "calls endpoint in another language", DATA_ACCESS: "reads / writes" };
  const REL_REV = { CONTAINS: "is inside", DEFINES: "is defined in", IMPORTS: "is imported by", CALLS: "is called by", INSTANTIATES: "is created by",
    EXTENDS: "is extended by", IMPLEMENTS: "is implemented by", OVERRIDES: "is implemented / overridden by", HANDLED_BY: "handles endpoint",
    EXECUTES: "is run by", READS: "is read by", WRITES: "is written by", HAS_COLUMN: "is a column of", FOREIGN_KEY: "is referenced by",
    MAPS_TO: "is the table of class", TESTS: "is tested by", HTTP_CALLS: "is called over HTTP by", CROSS_LANGUAGE_MAP: "is called (other language) by",
    DATA_ACCESS: "is accessed by" };
  const KIND_TEXT = { JAVA_CALL: "Java method call, target resolved from the receiver's declared type", JAVA_AST: "Java declaration (parsed syntax tree)",
    JAVA_IMPORT: "Java import statement", JAVA_NEW: "Java `new` expression", JAVA_SQL_CALL: "SQL string passed to a JDBC/JPA execution method",
    JAVA_SQL_STRING: "SQL-looking string passed to an unrecognised method", JAVA_TEST_CALL: "call made inside a JUnit test",
    JAVA_TEST_CLIENT: "HTTP request sent by a test (MockMvc/RestAssured/TestRestTemplate)", JAVA_HTTP_CLIENT: "HTTP call via RestTemplate/WebClient",
    SPRING_ANNOTATION: "Spring @GetMapping / @RequestMapping annotation on the handler", JAXRS_ANNOTATION: "JAX-RS @Path / @GET annotation",
    SPRING_DATA_DERIVED: "Spring Data query method naming convention (findBy…, deleteBy…)", SPRING_DATA_INHERITED: "inherited Spring Data CRUD method (save, findAll…)",
    SPRING_DATA_QUERY: "@Query annotation", SPRING_DATA_CONVENTION: "Spring Data repository entity type (default naming)",
    JPA_MAPPING: "JPA @Entity / @Table / @Column annotations", MYBATIS_XML: "MyBatis mapper XML (namespace + statement id)", MYBATIS_ANNOTATION: "MyBatis @Select/@Insert annotation",
    FEIGN_CLIENT: "Feign client declaration", OPENAPI_SPEC: "OpenAPI / Swagger specification", OPENAPI_GENERATOR_CONVENTION: "openapi-generator convention (method named after operationId, @Override of generated interface)",
    CONNEXION_OPERATION_ID: "Connexion operationId reference", PYTHON_CALL: "Python call resolved through imports / self / declared types", PYTHON_AST: "Python declaration (parsed syntax tree)",
    PYTHON_IMPORT: "Python import statement", PYTHON_SQL_CALL: "SQL passed to execute()/text()/read_sql()", PYTHON_SQL_STRING: "SQL-looking string passed to an unrecognised function",
    PYTHON_TEST_CALL: "call made inside a pytest/unittest test", PYTHON_TEST_CLIENT: "HTTP request sent by a test client", PYTHON_HTTP_CLIENT: "HTTP call via requests/httpx",
    SQLALCHEMY_ORM: "SQLAlchemy ORM query (select/query/insert on a model)", FLASK_SQLALCHEMY: "Flask-SQLAlchemy Model.query", DJANGO_ORM: "Django ORM (Model.objects…, save/delete)",
    ORM_SESSION: "ORM session.add/delete of a model instance", ORM_COLUMN: "ORM model field declaration", SQLALCHEMY_MODEL: "SQLAlchemy model (__tablename__)", DJANGO_MODEL: "Django model",
    FLASK_ROUTE: "Flask @app.route decorator", BLUEPRINT_ROUTE: "Flask blueprint route", FASTAPI_ROUTE: "FastAPI route decorator", APIROUTER_ROUTE: "FastAPI APIRouter route",
    DJANGO_URLCONF: "Django urls.py path()", DRF_ROUTER: "Django REST framework router.register", PYTHON_ROUTE_DECORATOR: "route decorator",
    SQL_PARSE: "parsed from the SQL statement (FROM/JOIN/INSERT/UPDATE…)", SQL_DDL: "CREATE TABLE definition", SQL_DDL_ALTER: "ALTER TABLE statement", FILESYSTEM: "folder structure" };
  const STATUS_TEXT = { VERIFIED: "Confirmed — the code itself proves this link.", CANDIDATE: "Probable — strong evidence, but not 100% proof (e.g. naming convention or ambiguous overload)." };
  const ST_LABEL = { ANSWERED: "ANSWERED", INSUFFICIENT_EVIDENCE: "NOT ENOUGH EVIDENCE", CLARIFY: "CHOOSE ONE", NOT_FOUND: "NOT FOUND", UNSUPPORTED: "OUT OF SCOPE", ERROR: "ERROR" };
  const LAYER_COLORS = { Client: "#9aa7b6", API: "#ff8a5b", Controller: "#5b9dff", Service: "#9d8cff", "Data access": "#2fbf7f", Model: "#6fc3df", SQL: "#f2c94c", Database: "#3fcf8e", Test: "#ff6b9a", Other: "#9aa7b6" };

  function label(n) {
    if (!n) return "?";
    const t = n.type;
    if (["Method", "Function", "Constructor", "Test"].includes(t)) {
      const q = (n.qualified_name || n.name || "").split("(")[0].split(".");
      return q.slice(-2).join(".") + "()";
    }
    return n.name || n.id;
  }
  const tchip = (t) => `<span class="tchip" style="background:${TYPE_COLORS[t] || "#ccc"}">${esc(TYPE_TEXT[t] || t)}</span>`;
  function ghLink(file, line) {
    const m = state.meta || {};
    const url = (m.repo_url || "").replace(/\.git$/, "");
    if (!file || !/^https:\/\/(github\.com|gitlab\.com)\//.test(url)) return "";
    const ref = m.commit || m.ref || "HEAD";
    const path = url.includes("gitlab.com") ? `${url}/-/blob/${ref}/${file}` : `${url}/blob/${ref}/${file}`;
    return `<a href="${esc(path)}${line ? "#L" + line : ""}" target="_blank" rel="noopener">open on ${url.includes("gitlab") ? "GitLab" : "GitHub"} ↗</a>`;
  }
  function codeBlock(snippet, start, highlight) {
    if (!snippet) return "";
    const rows = snippet.split("\n").map((l, i) => {
      const ln = (start || 1) + i;
      return `<tr class="${highlight && ln === highlight ? "hl" : ""}"><td class="ln">${ln}</td><td>${esc(l)}</td></tr>`;
    }).join("");
    return `<div class="code"><table>${rows}</table></div>`;
  }

  // ================================================================ state
  const state = { gid: null, meta: null, profile: null, page: "welcome", selected: null, mode: "architecture",
    hidden: new Set(), hiddenRels: new Set(), ctxEntity: null, lastGraph: null, apis: null, health: null };

  // ================================================================ cytoscape
  if (window.cytoscapeDagre) cytoscape.use(window.cytoscapeDagre);
  if (window.cytoscapeFcose) cytoscape.use(window.cytoscapeFcose);
  function sizeOf(n) {
    const t = n.data("type");
    if (n.data("api_count")) return Math.min(24 + 4 * Math.sqrt(n.data("api_count")), 60);
    return { Repository: 34, Table: 30, RestApi: 30, Class: 26, Interface: 26, Column: 12, Directory: 16, File: 16, Client: 30 }[t] || 18;
  }
  function cyStyle() {
    const text = css("--text") || "#16202c", bg = css("--canvas") || "#fbfcfd";
    return [
      { selector: "node", style: { "background-color": (n) => TYPE_COLORS[n.data("type")] || "#999", shape: (n) => TYPE_SHAPES[n.data("type")] || "ellipse",
        label: "data(label)", color: text, "font-size": 10, "text-valign": "bottom", "text-margin-y": 4, "text-wrap": "ellipsis", "text-max-width": 150,
        width: sizeOf, height: (n) => sizeOf(n) * (n.data("type") === "RestApi" ? 0.8 : 1), "border-width": 1.5, "border-color": bg,
        "text-outline-color": bg, "text-outline-width": 2, "min-zoomed-font-size": 6 } },
      { selector: 'node[type = "Interface"]', style: { "border-style": "dashed", "border-color": "#6f9fe8", "border-width": 2 } },
      { selector: "node[?is_test_code]", style: { "background-opacity": 0.75 } },
      { selector: "node[?isModule]", style: { shape: "round-rectangle", "background-color": css("--surface2"), "background-opacity": 0.6, "border-color": css("--line"),
        "border-width": 1, label: "data(label)", "text-valign": "top", "font-size": 28, "font-weight": 700, color: css("--muted"), padding: 24, "text-max-width": 600 } },
      { selector: "node.flown", style: { "font-size": 13, "text-max-width": 200, "min-zoomed-font-size": 4 } },
      { selector: "node.ov", style: { "font-size": 16, "min-zoomed-font-size": 4, "text-max-width": 220 } },
      { selector: "node:selected", style: { "border-width": 4, "border-color": css("--accent") } },
      { selector: "node.root", style: { "border-width": 4, "border-color": "#ffbf3c" } },
      { selector: "edge", style: { width: 1.5, "line-color": (e) => REL_COLORS[e.data("type")] || "#888", "target-arrow-color": (e) => REL_COLORS[e.data("type")] || "#888",
        "target-arrow-shape": "triangle", "arrow-scale": 0.85, "curve-style": "bezier", opacity: 0.85, "font-size": 9, color: text,
        "text-rotation": "autorotate", "text-outline-color": bg, "text-outline-width": 2 } },
      { selector: 'edge[status = "CANDIDATE"]', style: { "line-style": "dashed", opacity: 0.7 } },
      { selector: 'edge[type = "CONTAINS"], edge[type = "DEFINES"]', style: { width: 1, opacity: 0.45, "target-arrow-shape": "none" } },
      { selector: 'edge[type = "CROSS_LANGUAGE_MAP"], edge[type = "HTTP_CALLS"]', style: { width: 3, "line-style": "dotted" } },
      { selector: "edge[?aggregated]", style: { width: (e) => Math.min(1.5 + Math.log2(1 + (e.data("count") || 1)), 6),
        "line-color": (e) => ((e.data("operations") || []).includes("WRITES") ? "#ff9f43" : "#2fbf7f"),
        "target-arrow-color": (e) => ((e.data("operations") || []).includes("WRITES") ? "#ff9f43" : "#2fbf7f"), opacity: 0.7 } },
      { selector: "edge.labelled", style: { label: (e) => REL_FWD[e.data("type")] || e.data("type") } },
      { selector: "edge:selected", style: { width: 4, opacity: 1, label: (e) => REL_FWD[e.data("type")] || e.data("type"), "z-index": 99 } },
      { selector: ".faded", style: { opacity: 0.1 } },
      { selector: ".hl", style: { opacity: 1, "z-index": 9 } },
      { selector: "node.lane", style: { shape: "round-rectangle", "background-color": (n) => LAYER_COLORS[n.data("layer")] || "#999", "background-opacity": 0.08,
        "border-width": 1, "border-color": (n) => LAYER_COLORS[n.data("layer")] || "#999", "border-opacity": 0.5, label: "data(label)", "text-valign": "top",
        "text-halign": "center", "font-size": 12, "font-weight": 700, color: css("--muted"), events: "no" } },
    ];
  }
  function toElements(graph, extra = {}) {
    const nodes = (graph.nodes || []).map((n) => ({ group: "nodes", data: { ...n, label: n.type === "Client" ? n.name : label(n) } }));
    const mods = [...new Set((graph.nodes || []).filter((n) => n.module).map((n) => n.module))];
    if (mods.length > 1) {
      nodes.forEach((n) => { if (n.data.module) n.data.parent = "mod:" + n.data.module; });
      mods.forEach((m) => nodes.unshift({ group: "nodes", data: { id: "mod:" + m, label: m, type: "Module", isModule: true } }));
    }
    const ids = new Set(nodes.map((n) => n.data.id));
    const edges = (graph.edges || []).filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({ group: "edges", data: { ...e } }));
    return [...nodes, ...edges];
  }
  function layoutOpts(name, mode) {
    if (name === "dagre") return { name: "dagre", rankDir: mode === "ecosystem" ? "TB" : "LR", nodeSep: 26, rankSep: mode === "ecosystem" ? 60 : 140, edgeSep: 10, animate: false };
    if (name === "fcose") return { name: "fcose", quality: "proof", nodeRepulsion: 9000, idealEdgeLength: 90, nestingFactor: 0.6, gravity: 0.3, packComponents: true, animate: false, randomize: true, nodeDimensionsIncludeLabels: true };
    if (name === "breadthfirst") return { name: "breadthfirst", directed: true, spacingFactor: 1.1, animate: false };
    if (name === "concentric") return { name: "concentric", minNodeSpacing: 14, animate: false, concentric: (n) => n.degree() };
    return { name: "grid", animate: false };
  }
  function makeCy(container, onTapNode, onTapEdge) {
    const cy = cytoscape({ container, wheelSensitivity: 0.25, minZoom: 0.05, maxZoom: 3, style: cyStyle() });
    cy.on("tap", "node", (e) => { if (!e.target.data("isModule") && !e.target.hasClass("lane") && e.target.data("type") !== "Client") onTapNode(e.target.id()); });
    cy.on("tap", "edge", (e) => onTapEdge(e.target.data()));
    cy.on("mouseover", "node", (e) => { const nb = e.target.closedNeighborhood(); cy.elements().not(nb).addClass("faded"); nb.addClass("hl"); });
    cy.on("mouseout", "node", () => cy.elements().removeClass("faded hl"));
    return cy;
  }
  function draw(cy, graph, { layout, mode, root } = {}) {
    cy.elements().remove();
    cy.add(toElements(graph));
    if (root) cy.$id(root).addClass("root");
    if (graph.mode === "architecture-overview") cy.nodes().not("[?isModule]").addClass("ov");
    const lay = layout || (graph.mode === "architecture-overview" ? "fcose" : "dagre");
    try { cy.elements().layout(layoutOpts(lay, mode)).run(); } catch (e) { cy.layout({ name: "cose", animate: false }).run(); }
    cy.fit(undefined, 30);
    if (cy.zoom() > 1.3) { cy.zoom(1.3); cy.center(); }
  }
  let cyMain = null, cyAsk = null, cyFlow = null, cyImpact = null;

  // ================================================================ routing
  const PAGES = ["welcome", "overview", "explore", "ask", "flow", "impact", "quality"];
  function route() {
    let p = (location.hash || "#overview").slice(1).split("?")[0];
    if (!PAGES.includes(p)) p = "overview";
    if (!state.gid && p !== "welcome") p = "welcome";
    showPage(p);
  }
  function showPage(p) {
    state.page = p;
    $$(".page").forEach((s) => s.classList.toggle("on", s.id === "page-" + p));
    $$("#nav a").forEach((a) => { a.classList.toggle("on", a.dataset.page === p); a.classList.toggle("disabled", !state.gid); });
    closeSideInspector();
    if (!state.gid) return;
    if (p === "explore") enterExplore();
    if (p === "ask") enterAsk();
    if (p === "flow") enterFlow();
    if (p === "impact") enterImpact();
    if (p === "quality") enterQuality();
  }
  window.addEventListener("hashchange", route);

  // ================================================================ repositories
  async function loadGraphs(selectGid) {
    let gs = [];
    try { gs = await api("/api/graphs"); } catch (e) { toast("Server not reachable: " + e.message, true); }
    $("#graphSelect").innerHTML = (gs.length ? "" : `<option value="">No repository yet</option>`) +
      gs.map((g) => `<option value="${g.graph_id}">${esc(g.repo_key)}${g.ref ? " @" + esc(g.ref) : ""}</option>`).join("");
    $("#recentRepos").innerHTML = gs.length ? `<h3>Already analyzed</h3><div class="row">${gs.map((g) => `<button class="chip" data-gid="${g.graph_id}">${esc(g.repo_key)} <em>${g.entities} nodes</em></button>`).join("")}</div>` : "";
    $$("#recentRepos [data-gid]").forEach((b) => b.onclick = () => { openGraph(b.dataset.gid); location.hash = "#overview"; });
    const want = selectGid || store.get("gid", null);
    const pick = gs.find((g) => g.graph_id === want) || gs[0];
    if (pick) { $("#graphSelect").value = pick.graph_id; await openGraph(pick.graph_id); }
    route();
  }
  async function openGraph(gid) {
    state.gid = gid; store.set("gid", gid);
    Object.assign(state, { selected: null, ctxEntity: null, apis: null, profile: null, lastGraph: null });
    state.exploreLoaded = false; state.askInit = false;
    try {
      const [sum, prof] = await Promise.all([api(`/api/graph/${gid}/summary`), api(`/api/graph/${gid}/profile`)]);
      state.meta = sum.meta; state.summary = sum; state.profile = prof;
      renderOverview();
    } catch (e) { toast(e.message, true); }
    $("#chat").innerHTML = "";
    renderSuggestions();
    $$("#nav a").forEach((a) => a.classList.remove("disabled"));
  }
  $("#graphSelect").onchange = async (e) => { if (e.target.value) { await openGraph(e.target.value); route(); } };

  // ================================================================ OVERVIEW (task 3)
  function renderOverview() {
    const p = state.profile, m = state.meta || {};
    if (!p) return;
    const repoUrl = (m.repo_url || "").replace(/\.git$/, "");
    $("#ovTitle").textContent = m.repo_key || p.name;
    $("#ovSub").innerHTML = `${esc(m.ref || "default branch")} · commit <code>${esc((m.commit || "").slice(0, 10))}</code> · analyzed ${esc((m.analyzed_at || "").replace("T", " ").slice(0, 16))}` +
      (/^https:/.test(repoUrl) ? ` · <a href="${esc(repoUrl)}" target="_blank" rel="noopener">repository ↗</a>` : "");
    $("#aboutLines").innerHTML = p.summary_lines.map((l) => `<li>${esc(l)}</li>`).join("");
    const a = p.architecture;
    $("#techChips").innerHTML = a.frameworks.map((f) => `<span>${esc(f)}</span>`).join("") +
      Object.entries(a.languages).map(([k, v]) => `<span>${esc(k)} · ${v} files</span>`).join("");
    const s = p.stats;
    const tiles = [["endpoints", "REST endpoints", "#flow"], ["tables", "Database tables", "#explore?tables"], ["classes", "Classes & interfaces", "#explore"],
      ["methods", "Methods & functions", "#explore"], ["tests", "Automated tests", "#quality"], ["relationships", "Relationships", "#quality"]];
    $("#tiles").innerHTML = tiles.map(([k, t, href]) => `<button class="tile" data-href="${href}"><b>${s[k] ?? 0}</b><span>${t}</span></button>`).join("");
    $$("#tiles .tile").forEach((b) => b.onclick = () => { location.hash = b.dataset.href.split("?")[0]; });
    const lanesOrder = ["API", "Controller", "Service", "Data access", "Model", "Database"];
    const lanes = lanesOrder.map((l) => a.layers.find((x) => x.layer === l)).filter(Boolean);
    $("#archLanes").innerHTML = (lanes.length ? lanes : a.layers).map((l) => `
      <div class="lane"><div class="lane-box" style="border-top:3px solid ${LAYER_COLORS[l.layer] || "#999"}">
        <div class="lt">${esc(l.layer)} <span>${l.count}</span></div><p>${esc(l.meaning)}</p>
        <ul>${l.examples.slice(0, 4).map((e) => `<li data-q="${esc(e)}" title="Find ${esc(e)} in the graph">${esc(e)}</li>`).join("")}</ul></div></div>`).join("");
    $$("#archLanes li").forEach((li) => li.onclick = () => findAndFocus(li.dataset.q));
    $("#archNotes").innerHTML = a.explanation.map((x) => `<li>${esc(x)}</li>`).join("");
    const dm = a.data_model;
    $("#dataModel").innerHTML = dm.table_count ? `<p class="muted">${dm.table_count} tables${dm.foreign_keys ? `, ${dm.foreign_keys} foreign keys` : ""}. Click a table to see who uses it.</p>
      <div class="tbl-chips">${dm.tables.map((t) => `<button class="chip" data-t="${esc(t)}">${esc(t)}</button>`).join("")}</div>` : `<p class="muted">No database tables were found.</p>`;
    $$("#dataModel [data-t]").forEach((b) => b.onclick = () => { location.hash = "#ask"; setTimeout(() => askQuestion(`Which code reads or writes the ${b.dataset.t} table?`), 50); });
    $("#hotList").innerHTML = a.central_components.length ? a.central_components.map((c) => `<li data-id="${esc(c.id)}">${esc(c.name)}() <span>— called from ${c.callers} places</span></li>`).join("")
      : `<li class="muted">No call relationships.</li>`;
    $$("#hotList [data-id]").forEach((li) => li.onclick = () => openSideInspector(li.dataset.id));
  }
  async function findAndFocus(q) {
    try {
      const res = await api(`/api/graph/${state.gid}/search?${qs({ q, limit: 1 })}`);
      if (!res.length) return toast(`“${q}” not found`);
      state.selected = res[0].id; state.mode = "hops"; location.hash = "#explore";
      setTimeout(() => { setMode("hops"); loadView(); inspectNode(res[0].id, $("#inspector")); }, 30);
    } catch (e) { toast(e.message, true); }
  }

  // ================================================================ EXPLORE
  const MODE_HINT = { architecture: "Endpoints, the code that handles them and the tables they use. Big repositories show one box per controller class.",
    hops: "Only the selected item and its neighbours. Use “Spread” to go further.", ecosystem: "Folders and source files of the repository.",
    "cross-language": "HTTP calls between services or languages inside this repository." };
  function enterExplore() {
    if (!cyMain) cyMain = makeCy($("#cy"), (id) => { state.selected = id; inspectNode(id, $("#inspector")); }, (d) => inspectEdge(d, $("#inspector")));
    setTimeout(() => cyMain.resize(), 0);
    if (!state.exploreLoaded) { state.exploreLoaded = true; setMode(state.selected ? "hops" : "architecture"); loadView(); }
  }
  function setMode(m) {
    state.mode = m;
    $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
    $("#modeHint").textContent = MODE_HINT[m] || "";
  }
  async function loadView() {
    if (!state.gid) return;
    const p = { mode: state.mode, max_nodes: $("#maxNodes").value, include_candidates: $("#incCand").checked, include_structure: false };
    if (state.mode === "hops") {
      if (!state.selected) { toast("Select something first: use Find or click a node."); setMode("architecture"); return loadView(); }
      p.root_id = state.selected; p.hops = $("#hops").value;
    }
    try {
      const g = await api(`/api/graph/${state.gid}/view?${qs(p)}`);
      const titles = { architecture: g.mode === "architecture-overview" ? `Architecture overview · ${g.groups_shown} handler classes → tables (orange = writes)` : "Architecture · endpoint → code → database",
        ecosystem: "Files and folders", hops: `Focus · ${p.hops} step${p.hops > 1 ? "s" : ""} around the selection`, "cross-language": "Calls between services" };
      renderExplore(g, titles[state.mode], p.root_id);
    } catch (e) { toast(e.message, true); }
  }
  function renderExplore(g, title, root) {
    state.lastGraph = g;
    draw(cyMain, g, { layout: $("#layoutSel").dataset.touched ? $("#layoutSel").value : undefined, mode: state.mode, root });
    applyFilters();
    $("#viewTitle").textContent = `${title} · ${g.nodes.length} nodes · ${g.edges.length} links`;
    const hidden = g.hidden_neighbors ? Object.values(g.hidden_neighbors).reduce((a, b) => a + b, 0) : 0;
    const tn = $("#truncNote");
    if (!g.nodes.length) { tn.textContent = "Nothing to show in this view. Try another view or use Find."; tn.classList.remove("hidden"); }
    else if (g.truncated || hidden) { tn.textContent = g.truncated ? `Showing the first ${g.nodes.length} nodes — use Find, Focus or filters to narrow down.` : `${hidden} more neighbours of busy nodes are hidden — select a node and press ⊕.`; tn.classList.remove("hidden"); }
    else tn.classList.add("hidden");
    buildFilters(); buildLegend();
  }
  function buildFilters() {
    const types = [...new Set(cyMain.nodes().filter((n) => !n.data("isModule")).map((n) => n.data("type")))].sort();
    const rels = [...new Set(cyMain.edges().map((e) => e.data("type")))].sort();
    $("#typeFilters").innerHTML = types.map((t) => `<span class="fchip ${state.hidden.has(t) ? "off" : ""}" data-t="${t}" title="Click to hide/show"><span class="dot" style="background:${TYPE_COLORS[t]}"></span>${esc(TYPE_TEXT[t] || t)} <small class="muted">${cyMain.nodes(`[type = "${t}"]`).length}</small></span>`).join("");
    $("#relFilters").innerHTML = rels.map((r) => `<span class="fchip ${state.hiddenRels.has(r) ? "off" : ""}" data-r="${r}" title="${esc(REL_FWD[r] || r)}"><span class="dot" style="background:${REL_COLORS[r] || "#888"}"></span>${esc(REL_FWD[r] || r)}</span>`).join("");
  }
  function applyFilters() {
    cyMain.batch(() => {
      cyMain.elements().style("display", "element");
      cyMain.nodes().forEach((n) => { if (state.hidden.has(n.data("type"))) n.style("display", "none"); });
      cyMain.edges().forEach((e) => { if (state.hiddenRels.has(e.data("type"))) e.style("display", "none"); });
      cyMain.edges().toggleClass("labelled", $("#edgeLabels").checked);
    });
  }
  function legendHTML() {
    const nodes = ["RestApi", "Class", "Interface", "Method", "Function", "SqlQuery", "Table", "Column", "Test", "File"];
    const rels = ["HANDLED_BY", "CALLS", "READS", "WRITES", "EXECUTES", "TESTS", "HTTP_CALLS", "FOREIGN_KEY"];
    return `<div class="legend-grid">${nodes.map((t) => `<span class="dot" style="background:${TYPE_COLORS[t]};width:14px;height:14px;border-radius:${["Method", "Function"].includes(t) ? "50%" : "4px"}"></span><span>${TYPE_TEXT[t]}</span>`).join("")}</div>
      <hr style="border:0;border-top:1px solid var(--line);margin:10px 0"><div class="legend-grid">${rels.map((r) => `<span class="legend-line" style="border-color:${REL_COLORS[r]}"></span><span>${REL_FWD[r]}</span>`).join("")}
      <span class="legend-line dash" style="border-color:var(--faint)"></span><span>dashed = probable link</span></div>`;
  }
  function buildLegend() { $("#legend").innerHTML = `<b>What you see</b><p class="muted" style="margin:4px 0 8px">Shapes are parts of the code; arrows point from the user to what it uses.</p>${legendHTML()}`; }
  $("#legendBtn").onclick = () => $("#legend").classList.toggle("hidden");
  $$("#modeSeg button").forEach((b) => b.onclick = () => { setMode(b.dataset.mode); loadView(); });
  $("#hops").oninput = () => { $("#hopsVal").textContent = $("#hops").value; };
  $("#hops").onchange = () => state.mode === "hops" && loadView();
  ["#incCand", "#maxNodes"].forEach((s) => $(s).onchange = loadView);
  $("#edgeLabels").onchange = applyFilters;
  $("#layoutSel").onchange = () => { $("#layoutSel").dataset.touched = "1"; cyMain.elements().layout(layoutOpts($("#layoutSel").value, state.mode)).run(); cyMain.fit(undefined, 30); };
  $("#fitBtn").onclick = () => cyMain.fit(undefined, 30);
  $("#zoomIn").onclick = () => cyMain.zoom({ level: cyMain.zoom() * 1.25, renderedPosition: { x: cyMain.width() / 2, y: cyMain.height() / 2 } });
  $("#zoomOut").onclick = () => cyMain.zoom({ level: cyMain.zoom() / 1.25, renderedPosition: { x: cyMain.width() / 2, y: cyMain.height() / 2 } });
  $("#pngBtn").onclick = () => { const a = document.createElement("a"); a.href = cyMain.png({ full: true, scale: 2, bg: css("--canvas") }); a.download = "repograph.png"; a.click(); };
  $("#expandBtn").onclick = async () => {
    const sel = cyMain.$("node:selected"); if (!sel.length) return toast("Select a node first, then press ⊕.");
    const id = sel[0].id();
    const g = await api(`/api/graph/${state.gid}/view?${qs({ mode: "hops", root_id: id, hops: 1, max_nodes: 80, include_candidates: $("#incCand").checked })}`);
    const merged = { nodes: [...state.lastGraph.nodes], edges: [...state.lastGraph.edges] };
    const hn = new Set(merged.nodes.map((n) => n.id)), he = new Set(merged.edges.map((e) => e.id));
    g.nodes.forEach((n) => !hn.has(n.id) && merged.nodes.push(n)); g.edges.forEach((e) => !he.has(e.id) && merged.edges.push(e));
    renderExplore(merged, "Expanded view", id);
  };
  $("#runQuery").onclick = async () => {
    try {
      const r = await api(`/api/graph/${state.gid}/query/${$("#querySel").value}?${qs({ include_candidates: $("#incCand").checked, max_nodes: $("#maxNodes").value })}`);
      renderExplore(r, $("#querySel").selectedOptions[0].textContent);
    } catch (e) { toast(e.message, true); }
  };
  $("#typeFilters").onclick = (e) => { const c = e.target.closest(".fchip"); if (!c) return; const t = c.dataset.t; state.hidden.has(t) ? state.hidden.delete(t) : state.hidden.add(t); applyFilters(); buildFilters(); };
  $("#relFilters").onclick = (e) => { const c = e.target.closest(".fchip"); if (!c) return; const r = c.dataset.r; state.hiddenRels.has(r) ? state.hiddenRels.delete(r) : state.hiddenRels.add(r); applyFilters(); buildFilters(); };
  $("#resetFilters").onclick = () => { state.hidden.clear(); state.hiddenRels.clear(); applyFilters(); buildFilters(); };
  let searchTimer = null;
  $("#searchBox").addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = $("#searchBox").value.trim();
    if (!q) { $("#searchResults").innerHTML = ""; return; }
    searchTimer = setTimeout(async () => {
      const res = await api(`/api/graph/${state.gid}/search?${qs({ q, limit: 25 })}`);
      $("#searchResults").innerHTML = res.length ? res.map((n) => `<li data-id="${esc(n.id)}">${tchip(n.type)}<span><span class="nm">${esc(label(n))}</span><span class="fl">${esc(n.file || n.qualified_name || "")}${n.line ? ":" + n.line : ""}</span></span></li>`).join("") : `<li class="muted">No match for “${esc(q)}”</li>`;
      $$("#searchResults li[data-id]").forEach((li) => li.onclick = () => { state.selected = li.dataset.id; setMode("hops"); loadView(); inspectNode(li.dataset.id, $("#inspector")); });
    }, 220);
  });

  // ================================================================ INSPECTOR (task 7)
  async function inspectNode(id, target) {
    if (!state.gid) return;
    state.ctxEntity = id;
    target.classList.remove("empty");
    target.innerHTML = `<div class="insp muted">Loading…</div>`;
    try {
      const d = await api(`/api/graph/${state.gid}/node?${qs({ entity_id: id })}`);
      const n = d.node;
      const what = describeNode(n);
      const props = [["Qualified name", n.qualified_name], ["Signature", n.signature], ["HTTP", n.http_method ? `${n.http_method} ${n.path}` : ""],
        ["Operation", n.operation], ["Data type", n.data_type], ["Primary key", n.primary_key ? "yes" : ""], ["Language", n.language],
        ["Parameters", (n.parameters || []).join(", ")], ["Returns", n.return_type], ["Annotations", (n.annotations || []).map((a) => "@" + a).join(" ")],
        ["Decorators", (n.decorators || []).filter(Boolean).join(", ")], ["Defined in schema", n.defined_in_schema ? "yes" : ""],
        ["Documented in spec", n.documented ? "yes (OpenAPI)" : ""], ["Parse status", n.parse_status && n.parse_status !== "OK" ? n.parse_status : ""],
        ["ID", n.id]].filter(([, v]) => v);
      const groups = Object.entries(d.relationships).sort((a, b) => (a[0].endsWith(":in") ? 1 : 0) - (b[0].endsWith(":in") ? 1 : 0));
      const relHTML = groups.map(([k, items]) => {
        const [rel, dir] = k.split(":");
        const verb = dir === "out" ? REL_FWD[rel] || rel : REL_REV[rel] || rel;
        return `<div class="relgroup"><h5>${esc(cap(verb))} <span class="muted">(${items.length})</span></h5>${items.slice(0, 30).map((it) => `
          <div class="rel" data-id="${esc(it.node.id)}" data-eid="${esc(it.edge.id)}">${tchip(it.node.type)}<span class="t"><b>${esc(label(it.node))}</b>
          ${it.edge.status === "CANDIDATE" ? '<span class="badge CANDIDATE">probable</span>' : ""}
          <small>${esc(it.edge.file || "")}${it.edge.line ? ":" + it.edge.line : ""}</small></span><button class="link why" data-eid="${esc(it.edge.id)}" title="Show the evidence for this relationship">why?</button></div>`).join("")}
          ${items.length > 30 ? `<div class="muted small">…${items.length - 30} more</div>` : ""}</div>`;
      }).join("");
      target.innerHTML = `<div class="insp">
        <div class="insp-head">${tchip(n.type)}<h2>${esc(label(n))}</h2></div>
        <p class="insp-what">${what}</p>
        ${n.file ? `<div class="insp-sec"><h4>Where in the code</h4><div class="loc"><span>📄 ${esc(n.file)}${n.line ? ":" + n.line : ""}${n.end_line && n.end_line !== n.line ? "–" + n.end_line : ""}</span>${ghLink(n.file, n.line)}</div>
          ${codeBlock(n.snippet, n.snippet_start, n.line)}</div>` : ""}
        ${n.sql ? `<div class="insp-sec"><h4>SQL</h4><pre class="cypher">${esc(n.sql)}</pre></div>` : ""}
        <div class="actions">
          <button class="btn sm" data-act="focus">⬡ Show neighbours</button>
          <button class="btn sm" data-act="impact">◬ Impact if changed</button>
          ${n.type === "RestApi" ? '<button class="btn sm" data-act="flow">⇢ Request flow</button>' : ""}
          <button class="btn sm" data-act="ask">✦ Explain</button>
        </div>
        <div class="insp-sec"><h4>Details</h4><dl class="kv">${props.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}<dt>Connections</dt><dd>${d.degree}</dd></dl></div>
        <div class="insp-sec"><h4>Relationships <span class="muted">click to open</span></h4>${relHTML || '<p class="muted">No relationships.</p>'}</div>
      </div>`;
      $$(".why", target).forEach((b) => b.onclick = (ev) => { ev.stopPropagation(); inspectEdgeById(b.dataset.eid, target); });
      $$(".rel", target).forEach((el) => el.onclick = (ev) => {
        if (ev.altKey) return inspectEdgeById(el.dataset.eid, target);
        if (cyMain && state.page === "explore") selectIn(cyMain, el.dataset.id);
        inspectNode(el.dataset.id, target);
      });
      $$("[data-act]", target).forEach((b) => b.onclick = () => nodeAction(b.dataset.act, n));
    } catch (e) { target.innerHTML = `<div class="insp">${esc(e.message)}</div>`; }
  }
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  function describeNode(n) {
    const t = TYPE_TEXT[n.type] || n.type;
    if (n.type === "RestApi") return `A <b>${esc(n.http_method)}</b> endpoint at <code>${esc(n.path)}</code> that clients can call${n.documented ? "; it is also documented in the API specification" : ""}.`;
    if (n.type === "Table") return `A database table${n.defined_in_schema ? " defined in the SQL schema" : " referenced by queries or ORM mappings"}.`;
    if (n.type === "SqlQuery") return `A ${esc(n.operation || "")} SQL statement found in <code>${esc(n.file)}</code>.`;
    if (["Method", "Function", "Constructor"].includes(n.type)) return `A ${t.toLowerCase()}${n.class ? ` of class <code>${esc(n.class.split(".").pop())}</code>` : ""}${n.is_test_code ? " in test code" : ""}.`;
    if (n.type === "Test") return "An automated test.";
    if (n.type === "Column") return `A column of table <code>${esc(n.table || "")}</code>.`;
    return `A ${t.toLowerCase()}${n.package ? ` in package <code>${esc(n.package)}</code>` : ""}.`;
  }
  async function inspectEdgeById(eid, target) {
    try { const d = await api(`/api/graph/${state.gid}/edge?${qs({ edge_id: eid })}`); renderEdge(d.edge, d.source, d.target, target); }
    catch (e) { toast(e.message, true); }
  }
  async function inspectEdge(data, target) {
    if (data.aggregated) {
      target.classList.remove("empty");
      target.innerHTML = `<div class="insp"><div class="insp-head"><span class="tchip" style="background:${REL_COLORS.DATA_ACCESS}">Summary link</span><h2>${esc(data.evidence || "")}</h2></div>
        <p class="insp-what">This arrow summarises many evidence-backed paths from the handlers of this class to the table (${esc((data.operations || []).join(" + ").toLowerCase())}). Select the class and use <b>Show neighbours</b> to see the individual paths.</p></div>`;
      return;
    }
    return inspectEdgeById(data.id, target);
  }
  function renderEdge(e, s, t, target) {
    target.classList.remove("empty");
    const verb = REL_FWD[e.type] || e.type;
    target.innerHTML = `<div class="insp">
      <div class="insp-head"><span class="tchip" style="background:${REL_COLORS[e.type] || "#999"}">Relationship</span><h2>${esc(e.type)}</h2></div>
      <p class="sentence"><b data-id="${esc(s.id)}" class="lnk">${esc(label(s))}</b> ${esc(verb)} <b data-id="${esc(t.id)}" class="lnk">${esc(label(t))}</b>.</p>
      <div class="insp-sec"><h4>How sure is this?</h4><span class="badge ${e.status}">${e.status === "VERIFIED" ? "CONFIRMED" : "PROBABLE"}</span> <span class="muted">${esc(STATUS_TEXT[e.status] || "")}</span></div>
      ${e.file ? `<div class="insp-sec"><h4>Evidence in the code</h4><div class="loc"><span>📄 ${esc(e.file)}${e.line ? ":" + e.line : ""}</span>${ghLink(e.file, e.line)}</div>
        ${codeBlock(e.snippet, e.snippet_start, e.line) || (e.evidence ? `<pre class="cypher">${esc(e.evidence)}</pre>` : "")}</div>` : (e.evidence ? `<div class="insp-sec"><h4>Evidence</h4><pre class="cypher">${esc(e.evidence)}</pre></div>` : "")}
      <div class="insp-sec"><h4>Details</h4><dl class="kv">
        <dt>Found by</dt><dd>${esc(KIND_TEXT[e.source_kind] || (e.source_kind || "").replace(/_/g, " ").toLowerCase())}</dd>
        <dt>Why</dt><dd>${esc(e.reason || "")}</dd>
        <dt>Occurrences</dt><dd>${e.occurrences || 1}</dd>
        <dt>From</dt><dd>${tchip(s.type)} ${esc(s.qualified_name || s.name)}</dd>
        <dt>To</dt><dd>${tchip(t.type)} ${esc(t.qualified_name || t.name)}</dd>
        <dt>ID</dt><dd><code>${esc(e.id)}</code></dd></dl></div></div>`;
    $$(".lnk", target).forEach((b) => { b.style.cursor = "pointer"; b.onclick = () => inspectNode(b.dataset.id, target); });
  }
  function selectIn(cy, id) { cy.$(":selected").unselect(); const el = cy.$id(id); if (el.length) { el.select(); cy.animate({ center: { eles: el }, duration: 250 }); } }
  async function nodeAction(act, n) {
    if (act === "focus") { state.selected = n.id; state.exploreLoaded = true; location.hash = "#explore"; setTimeout(() => { setMode("hops"); loadView(); }, 30); }
    if (act === "impact") { location.hash = "#impact"; setTimeout(() => runImpact(n.id), 30); }
    if (act === "flow") { location.hash = "#flow"; setTimeout(() => runFlow(n.id), 30); }
    if (act === "ask") { location.hash = "#ask"; setTimeout(() => askQuestion(`Explain ${n.type === "RestApi" ? n.name : label(n).replace("()", "")}`, n.id), 30); }
  }
  // side inspector (used on Ask / Flow / Impact / Overview pages)
  function openSideInspector(id, edge) {
    let d = $("#sideInsp");
    if (!d) { d = document.createElement("aside"); d.id = "sideInsp"; d.className = "side-drawer"; document.body.appendChild(d); }
    d.innerHTML = `<button class="btn icon close" aria-label="Close">✕</button><div class="body"></div>`;
    $(".close", d).onclick = closeSideInspector;
    const body = $(".body", d);
    if (edge) inspectEdge(edge, body); else if (id) inspectNode(id, body);
  }
  function closeSideInspector() { $("#sideInsp")?.remove(); }

  // ================================================================ ASK (task 2)
  function enterAsk() {
    if (!cyAsk) cyAsk = makeCy($("#cyAsk"), (id) => openSideInspector(id), (d) => openSideInspector(null, d));
    setTimeout(() => cyAsk.resize(), 0);
    const h = state.health?.nlp;
    $("#nlpMode").innerHTML = h ? `<span class="pill">Understanding: ${esc(h.label)}</span><span class="pill">${h.text_to_cypher ? "Cypher generated by Gemini, validated, run read-only on Neo4j" : "Cypher: reviewed templates (shown with each answer)"}</span>` : "";
    if (!$("#chat").children.length) renderSuggestions();
  }
  function renderSuggestions() {
    const s = state.summary || {}, p = state.profile;
    const apis = s.apis || [];
    const api0 = apis.find((a) => a.http_method === "POST") || apis.find((a) => (a.path || "/").length > 1) || apis[0];
    const tbl = (s.tables || [])[0];
    const hub = p?.architecture?.central_components?.[0];
    const ctrl = p?.architecture?.layers?.find((l) => l.layer === "Controller")?.examples?.[0];
    const name = (x) => (x ? x.name.replace(/\{\}/g, "{id}") : "");
    const groups = [
      ["Understand", ["Summarize this repository", ctrl && `Explain ${ctrl}`, "How many endpoints are there?"]],
      ["Trace", [api0 && `What happens when I call ${name(api0)}?`, api0 && `Which tables are touched by ${name(api0)}?`, hub && `Who calls ${hub.name.split(".").pop()}?`]],
      ["Change safely", [tbl && `What is impacted if I change the ${tbl.name} table?`, "Which endpoints have no tests?", "Which methods are never called?"]],
    ];
    $("#suggest").innerHTML = groups.map(([g, qs2]) => `<div class="grp"><span>${g}</span>${qs2.filter(Boolean).map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>`).join("");
    $$("#suggest [data-q]").forEach((b) => b.onclick = () => askQuestion(b.dataset.q));
    if (!$("#chat").children.length && state.gid) $("#chat").innerHTML = `<div class="empty-state"><b>Ask anything about ${esc(state.meta?.repo_key || "this repository")}</b>Pick a suggestion below or type your own question.</div>`;
  }
  async function askQuestion(q, ctx) {
    if (!state.gid) return;
    if (state.page !== "ask") location.hash = "#ask";
    const chat = $("#chat");
    chat.querySelector(".empty-state")?.remove();
    chat.insertAdjacentHTML("beforeend", `<div class="msg-q">${esc(q)}</div>`);
    const holder = document.createElement("div"); holder.className = "msg-a"; holder.innerHTML = `<div class="top"><span class="muted">Analysing the code graph…</span></div>`;
    chat.appendChild(holder); chat.scrollTop = chat.scrollHeight;
    $("#askBtn").disabled = true;
    try {
      const r = await api(`/api/graph/${state.gid}/ask`, { method: "POST", body: JSON.stringify({ question: q, context_entity_id: ctx || state.ctxEntity, include_candidates: true }) });
      if (r.resolved_entities?.length) state.ctxEntity = r.resolved_entities[0].id;
      renderAnswer(holder, r, q);
      if (r.graph?.nodes?.length && cyAsk) { state.askGraph = r.graph; draw(cyAsk, r.graph, { root: r.resolved_entities?.[0]?.id }); }
    } catch (e) { holder.innerHTML = `<div class="top"><span class="st ERROR">ERROR</span> ${esc(e.message)}</div>`; }
    finally { $("#askBtn").disabled = false; chat.scrollTop = chat.scrollHeight; }
  }
  function renderAnswer(holder, r, q) {
    const facts = (r.facts || []).slice(0, 80).map((f) => `<div class="fact"><b>${esc(f.subject)}</b> ${esc((REL_FWD[f.relationship] || f.relationship || "").toLowerCase())} <b>${esc(f.object ?? "")}</b>
      ${f.validation_status ? `<span class="badge ${f.validation_status}">${f.validation_status === "VERIFIED" ? "confirmed" : f.validation_status === "CANDIDATE" ? "probable" : esc(f.validation_status)}</span>` : ""}
      ${f.source_file ? `<span class="ev">${esc(f.source_file)}:${f.line ?? ""}${f.evidence ? " — " + esc(f.evidence) : ""}</span>` : ""}</div>`).join("");
    const cy = r.cypher;
    const tabs = [["nl", "Plain English"], ["dev", "Developer"], ["cypher", "Cypher"], ["how", "How it was answered"], ["ev", `Evidence (${(r.facts || []).length})`]];
    holder.innerHTML = `
      <div class="top"><span class="st ${r.status}">${ST_LABEL[r.status] || r.status}</span><span class="pill">${esc((r.query_plan.intent || "").replace(/_/g, " ").toLowerCase())}</span>
        <span class="meta">${esc(r.planner)} · ${r.elapsed_ms} ms</span></div>
      <div class="atabs">${tabs.map(([k, t], i) => `<button data-t="${k}" class="${i === 0 ? "on" : ""}">${t}</button>`).join("")}</div>
      <div class="apanel on" data-p="nl"><div class="nl">${md(r.answer_nl || r.answer)}</div>${r.explanation && r.explanation !== r.answer_nl ? `<p class="muted small" style="margin-top:8px">AI wording checked against the facts.</p>` : ""}</div>
      <div class="apanel" data-p="dev"><div class="dev">${md(r.answer_dev || r.answer)}</div></div>
      <div class="apanel" data-p="cypher">${cy ? `<p class="muted small">${esc(cy.source)}${cy.executed ? " — this exact query was executed" : " — the answer was computed by the equivalent reviewed traversal"}</p>
        <pre class="cypher">${esc(cy.query)}</pre>${cy.params ? `<p class="muted small">Parameters: <code>${esc(JSON.stringify(cy.params))}</code></p>` : ""}
        ${(cy.notes || []).map((n) => `<p class="muted small">• ${esc(n)}</p>`).join("")}${cy.note ? `<p class="muted small">${esc(cy.note)}</p>` : ""}
        ${(cy.repaired_from || []).length ? `<p class="muted small">Self-repaired after: ${esc(cy.repaired_from.map((a) => a.error).join(" | "))}</p>` : ""}
        <button class="btn sm" data-copy>Copy Cypher</button>` : `<p class="muted">No Cypher for this answer.</p>`}</div>
      <div class="apanel" data-p="how"><ol class="bd">${(r.breakdown || []).map((b) => `<li><b>${esc(b.step)}</b><span class="muted">${esc(b.detail)}</span>${(b.notes || []).map((n) => `<div class="small">↳ ${esc(n)}</div>`).join("")}</li>`).join("")}</ol></div>
      <div class="apanel" data-p="ev">${facts || '<p class="muted">No facts.</p>'}</div>
      ${r.candidates?.length ? `<div class="note">Did you mean:</div><div class="cands">${r.candidates.map((c) => `<button class="chip" data-cid="${esc(c.id)}" title="${esc(c.qualified_name || c.id)}">${esc(TYPE_TEXT[c.type] || c.type)}: ${esc(label(c))}</button>`).join("")}</div>` : ""}
      ${(r.limitations || []).map((l) => `<div class="note">ⓘ ${esc(l)}</div>`).join("")}
      <div class="fb"><button class="btn sm" data-h="1">👍 Helpful</button><button class="btn sm" data-h="0">👎 Not helpful</button></div>`;
    $$(".atabs button", holder).forEach((b) => b.onclick = () => {
      $$(".atabs button", holder).forEach((x) => x.classList.toggle("on", x === b));
      $$(".apanel", holder).forEach((p) => p.classList.toggle("on", p.dataset.p === b.dataset.t));
    });
    $("[data-copy]", holder)?.addEventListener("click", () => { try { navigator.clipboard.writeText(cy.query); toast("Cypher copied"); } catch { /* clipboard unavailable */ } });
    $$("[data-cid]", holder).forEach((b) => b.onclick = () => askQuestion(q, b.dataset.cid));
    $$("[data-h]", holder).forEach((b) => b.onclick = async () => {
      await api(`/api/graph/${state.gid}/ask/feedback`, { method: "POST", body: JSON.stringify({ question_id: r.question_id, question: q, helpful: b.dataset.h === "1" }) });
      b.parentElement.innerHTML = '<span class="muted small">Thanks — feedback saved.</span>';
    });
  }
  $("#askForm").onsubmit = (e) => { e.preventDefault(); const q = $("#question").value.trim(); if (q) { $("#question").value = ""; askQuestion(q); } };
  $("#question").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#askForm").requestSubmit(); } });
  $("#askToExplore").onclick = () => { if (!state.askGraph) return toast("Ask a question first."); state.exploreLoaded = true; location.hash = "#explore"; setTimeout(() => renderExplore(state.askGraph, "From your question"), 40); };

  // ================================================================ FLOW (task 6)
  async function enterFlow() {
    if (!cyFlow) cyFlow = makeCy($("#cyFlow"), (id) => openSideInspector(id), (d) => openSideInspector(null, d));
    setTimeout(() => cyFlow.resize(), 0);
    if (!state.apis) {
      const r = await api(`/api/graph/${state.gid}/query/apis_and_handlers?max_nodes=600`);
      state.apis = r.rows.map((x) => x.source).filter((v, i, a) => a.findIndex((y) => y.id === v.id) === i)
        .sort((a, b) => (a.path || "").localeCompare(b.path || "") || (a.http_method || "").localeCompare(b.http_method || ""));
    }
    renderApiList();
  }
  function renderApiList() {
    const f = ($("#apiFilter").value || "").toLowerCase();
    const list = state.apis.filter((a) => !f || a.name.toLowerCase().includes(f));
    $("#apiList").innerHTML = list.length ? list.map((a) => `<li data-id="${esc(a.id)}" class="${state.flowApi === a.id ? "on" : ""}"><span class="verb ${esc(a.http_method)}">${esc(a.http_method)}</span>${esc(a.path || a.name)}</li>`).join("")
      : `<li class="muted">${state.apis.length ? "No match" : "This repository exposes no REST endpoints."}</li>`;
    $$("#apiList li[data-id]").forEach((li) => li.onclick = () => runFlow(li.dataset.id));
  }
  $("#apiFilter").oninput = renderApiList;
  async function runFlow(apiId) {
    if (!cyFlow) await enterFlow();
    state.flowApi = apiId; renderApiList();
    try {
      const fl = await api(`/api/graph/${state.gid}/flow?${qs({ api_id: apiId })}`);
      const lanes = fl.lanes;
      $("#flowHead").innerHTML = `<b>${esc(fl.api.name)}</b> — ${lanes.filter((l) => l.layer !== "Client").map((l) => `<span style="color:${LAYER_COLORS[l.layer]}">${esc(l.layer)}</span>`).join(" → ")}
        ${fl.tables.length ? ` · tables: <code>${esc(fl.tables.join(", "))}</code>` : " · no database access found"}`;
      // lane layout: one column per layer
      const els = [], colW = 250;
      const byId = Object.fromEntries(fl.nodes.map((n) => [n.id, n]));
      lanes.forEach((l, ci) => {
        els.push({ group: "nodes", data: { id: "lane:" + l.layer, label: l.layer, layer: l.layer }, classes: "lane", grabbable: false, selectable: false });
        l.nodes.slice(0, 12).forEach((id, ri) => { const n = byId[id]; if (n) els.push({ group: "nodes", classes: "flown", data: { ...n, label: n.type === "Client" ? n.name : label(n), parent: "lane:" + l.layer }, position: { x: ci * colW, y: 60 + ri * 78 } }); });
      });
      const ids = new Set(els.map((e) => e.data.id));
      fl.edges.forEach((e) => { if (ids.has(e.source) && ids.has(e.target)) els.push({ group: "edges", data: e }); });
      if (ids.has("client:http")) els.push({ group: "edges", data: { id: "edge:client", source: "client:http", target: fl.api.id, type: "HTTP_CALLS", status: "VERIFIED" } });
      cyFlow.elements().remove(); cyFlow.add(els); cyFlow.layout({ name: "preset", fit: true, padding: 30 }).run();
      if (cyFlow.zoom() > 1.2) { cyFlow.zoom(1.2); cyFlow.center(); }
      if (cyFlow.zoom() < 0.8) {          // keep labels readable: zoom in and let the user pan sideways
        const bb = cyFlow.elements().boundingBox();
        cyFlow.zoom(0.8); cyFlow.pan({ x: -bb.x1 * 0.8 + 20, y: -bb.y1 * 0.8 + 20 });
        $("#flowHead").insertAdjacentHTML("beforeend", ' <span class="muted small">· drag the diagram to see all lanes →</span>');
      }
      $("#flowSteps").innerHTML = fl.steps.map((s) => `<li data-node="${esc(s.node || "")}" data-edge="${esc(s.edge || "")}"><span class="n">${s.n}</span><span class="ly" style="color:${LAYER_COLORS[s.layer] || "inherit"}">${esc(s.layer)}</span>
        <span class="tx">${esc(s.text)}${s.status === "CANDIDATE" ? ' <span class="badge CANDIDATE">probable</span>' : ""}<small>${esc(s.dev || "")}</small></span></li>`).join("");
      $$("#flowSteps li").forEach((li) => li.onclick = () => { if (li.dataset.edge) inspectEdgeById(li.dataset.edge, sideBody()); else if (li.dataset.node) openSideInspector(li.dataset.node); });
    } catch (e) { toast(e.message, true); }
  }
  function sideBody() { openSideInspector(null, null); const b = $("#sideInsp .body"); b.innerHTML = ""; return b; }

  // ================================================================ IMPACT (task 6)
  function enterImpact() {
    if (!$("#impactOut").children.length) $("#impactOut").innerHTML = `<div class="card empty-state"><b>Pick what you plan to change</b>Search above for a method, class, table, endpoint or SQL statement.</div>`;
  }
  let impTimer = null;
  $("#impactSearch").addEventListener("input", () => {
    clearTimeout(impTimer);
    const q = $("#impactSearch").value.trim();
    if (!q) { $("#impactResults").innerHTML = ""; return; }
    impTimer = setTimeout(async () => {
      const res = await api(`/api/graph/${state.gid}/search?${qs({ q, limit: 12, types: "Method,Function,Class,Interface,Table,RestApi,SqlQuery,Column,Constructor" })}`);
      $("#impactResults").innerHTML = res.map((n) => `<li data-id="${esc(n.id)}">${tchip(n.type)}<span><span class="nm">${esc(label(n))}</span><span class="fl">${esc(n.file || n.qualified_name || "")}</span></span></li>`).join("") || `<li class="muted">No match</li>`;
      $$("#impactResults li[data-id]").forEach((li) => li.onclick = () => { $("#impactResults").innerHTML = ""; runImpact(li.dataset.id); });
    }, 220);
  });
  // Keep the graph readable: the top-N items by risk plus the nodes that connect each of them to the root.
  function trimImpactGraph(r, topN) {
    const all = r.graph;
    if (all.nodes.length <= topN + 10) return { ...all, trimmed: false };
    const adj = {};
    all.edges.forEach((e) => { (adj[e.source] ||= []).push(e.target); (adj[e.target] ||= []).push(e.source); });
    const parent = { [r.root.id]: null }, queue = [r.root.id];
    while (queue.length) { const u = queue.shift(); (adj[u] || []).forEach((v) => { if (!(v in parent)) { parent[v] = u; queue.push(v); } }); }
    const keep = new Set([r.root.id]);
    r.items.slice(0, topN).forEach((i) => { let v = i.node.id; while (v != null && !keep.has(v) && keep.size < topN * 2) { keep.add(v); v = parent[v]; } });
    const nodes = all.nodes.filter((n) => keep.has(n.id));
    const edges = all.edges.filter((e) => keep.has(e.source) && keep.has(e.target));
    return { nodes, edges, trimmed: true, shown: nodes.length - 1 };
  }
  async function runImpact(id) {
    $("#impactOut").innerHTML = `<div class="card muted">Computing impact…</div>`;
    try {
      const r = await api(`/api/graph/${state.gid}/impact-report?${qs({ root_id: id, depth: 4 })}`);
      const col = { High: "var(--bad)", Medium: "var(--warn)", Low: "var(--ok)" }[r.risk_level];
      const rows = r.items.map((i) => `<tr class="click" data-id="${esc(i.node.id)}"><td><span class="risk-level risk-${i.risk}">${i.risk === "Run" ? "run" : i.risk}</span></td><td>${tchip(i.node.type)}</td>
        <td class="wrap"><b>${esc(label(i.node))}</b><div class="muted small mono">${esc((i.node.file || "").split("/").slice(-2).join("/"))}${i.node.line ? ":" + i.node.line : ""}</div></td><td>${esc(i.layer)}</td><td class="nowrap" title="links between it and what you change (SQL/table mapping hops are not counted)">${i.distance <= 1 ? "direct" : i.distance + " links"}</td>
        <td class="small">${esc(REL_REV[i.via.type] ? `via ${REL_FWD[i.via.type]}` : i.via.type)}${i.via.status === "CANDIDATE" ? " (probable)" : ""}</td></tr>`).join("");
      const pot = r.potential.map((p) => `<tr class="click" data-id="${esc(p.node.id)}"><td><span class="risk-level risk-${p.risk}">${p.risk}</span></td><td>${tchip(p.node.type)}</td><td><b>${esc(label(p.node))}</b></td><td colspan="3" class="small">${esc(p.reason)}</td></tr>`).join("");
      $("#impactOut").innerHTML = `
        <div class="card"><div class="risk-head">
          <div class="gauge" style="--g:${r.blast_radius};--g-col:${col}"><span>${r.blast_radius}<small>BLAST RADIUS</small></span></div>
          <div><h2>${tchip(r.root.type)} ${esc(label(r.root))} <span class="risk-level risk-${r.risk_level}">${r.risk_level.toUpperCase()} RISK</span></h2>
            <p class="nl" style="margin:0 0 8px">${esc(r.summary)}</p>
            <div class="count-row"><div><b>${r.counts.apis}</b><span>endpoints affected</span></div><div><b>${r.counts.code}</b><span>code items</span></div>
              <div><b>${r.counts.tables}</b><span>tables involved</span></div><div><b>${r.counts.tests}</b><span>tests to run</span></div><div><b>${r.counts.potential}</b><span>possible side effects</span></div></div></div></div></div>
        <div class="card"><h2>What to do before changing it</h2><ol class="recs">${r.recommendations.map((x) => `<li>${esc(x)}</li>`).join("")}</ol></div>
        <div class="impact-grid">
          <div class="card"><h2>Affected by evidence <span class="muted small">(sorted by risk)</span></h2><div class="scroll"><table class="dt"><tr><th>Risk</th><th>Type</th><th>Item</th><th>Layer</th><th>Distance</th><th>Connected by</th></tr>${rows || '<tr><td colspan="6" class="muted">Nothing depends on it in the graph.</td></tr>'}</table></div></div>
          <div class="card"><h2>Graph <span class="muted small" id="impactGraphNote"></span></h2><div id="cyImpact" class="impact-cy"></div></div>
        </div>
        <div class="card"><h2>Possible side effects <span class="tag">heuristic, not proven</span></h2><div class="scroll"><table class="dt"><tr><th>Risk</th><th>Type</th><th>Item</th><th colspan="3">Why it might be affected</th></tr>${pot || '<tr><td colspan="6" class="muted">None detected.</td></tr>'}</table></div></div>
        <details class="card model-box"><summary><b>How the risk is calculated</b></summary>
          <p>Score of each item = <b>type weight × distance decay × certainty</b> (probable links count 0.6). High ≥ 0.6, Medium ≥ 0.3, otherwise Low. Tests are listed as “run”.</p>
          <p>Type weights: ${esc(Object.entries(r.model.type_weights).map(([k, v]) => `${k} ${v}`).join(", "))}. Distance decay per step: ${esc(r.model.distance_decay.join(", "))}.</p>
          <p>Possible side effects: ${esc(r.model.potential_rules.join("; "))}.</p><p>${esc(r.model.disclaimer)}</p></details>`;
      cyImpact = makeCy($("#cyImpact"), (nid) => openSideInspector(nid), (d) => openSideInspector(null, d));
      const g = trimImpactGraph(r, 45);
      $("#impactGraphNote").textContent = g.trimmed ? `— showing the ${g.shown} highest-risk items and how they connect (full list in the table)` : "";
      cyImpact.add(toElements(g)); cyImpact.$id(r.root.id).addClass("root");
      // dependency arrows point at what they depend on, so a left-to-right layout ends at the item being changed
      const small = g.nodes.length < 30;
      cyImpact.layout({ name: "dagre", rankDir: "LR", nodeSep: small ? 30 : 6, rankSep: small ? 170 : 90, edgeSep: 6, animate: false }).run();
      cyImpact.fit(undefined, 20);
      $$("#impactOut tr.click").forEach((tr) => tr.onclick = () => openSideInspector(tr.dataset.id));
    } catch (e) { $("#impactOut").innerHTML = `<div class="card">${esc(e.message)}</div>`; }
  }

  // ================================================================ QUALITY
  async function enterQuality() {
    try {
      const d = await api(`/api/graph/${state.gid}/diagnostics?limit=300`);
      const r = d.report;
      const langs = Object.entries(r.files_by_language || {}).map(([k, v]) => {
        const okp = v.files ? Math.round(100 * (v.OK || 0) / v.files) : 0;
        return `<div class="bar-row"><span>${esc(k)}</span><div class="bar"><i style="width:${okp}%"></i></div><span>${v.OK || 0}/${v.files} ok</span></div>`;
      }).join("");
      const rel = Object.entries(r.relationship_counts || {}).filter(([k]) => !["CONTAINS", "DEFINES"].includes(k)).map(([k, v]) => {
        const tot = (v.VERIFIED || 0) + (v.CANDIDATE || 0) || 1;
        return `<div class="bar-row"><span title="${esc(k)}">${esc(cap(REL_FWD[k] || k))}</span><div class="bar"><i style="width:${100 * (v.VERIFIED || 0) / tot}%"></i><i class="c" style="width:${100 * (v.CANDIDATE || 0) / tot}%"></i></div><span>${v.VERIFIED || 0} + ${v.CANDIDATE || 0}</span></div>`;
      }).join("");
      const res = r.calls_resolved || 0, unres = r.calls_unresolved_or_external || 0;
      const rows = d.diagnostics.slice(0, 200).map((x) => `<tr><td>${esc(x.level)}</td><td>${esc(x.code)}</td><td class="mono small">${esc(x.file || "")}${x.line ? ":" + x.line : ""}</td><td>${esc(x.message)}</td></tr>`).join("");
      $("#qualityOut").innerHTML = `
        <div class="card"><h2>Endpoint coverage</h2><div class="big-num">${r.apis_with_handler}/${r.apis_total}</div><p class="muted">endpoints linked to the code that handles them${r.apis_documented_only ? `; ${r.apis_documented_only} exist only in the API spec` : ""}.</p></div>
        <div class="card"><h2>Method calls</h2><div class="big-num">${res}</div><p class="muted">calls resolved to code inside the repository. ${unres} other calls go to libraries (e.g. String, List) or could not be resolved — that is normal.</p></div>
        <div class="card"><h2>Files analysed</h2>${langs}<p class="muted small">${r.files_analyzed} files read · ${r.files_skipped} skipped · ${r.elapsed_seconds}s</p></div>
        <div class="card"><h2>Relationships <span class="muted small">green = confirmed, amber = probable</span></h2>${rel}</div>
        <div class="card wide"><h2>Trustworthiness</h2><ul class="arch-notes">
          <li>Every link carries the file and line it came from — click any arrow in Explore to check it.</li>
          <li>${r.rejected_relationships} proposed links were rejected by the validator (wrong types or missing evidence) and are not shown.</li>
          <li>Static analysis cannot see reflection, runtime-registered routes or code generated at build time; such places appear as “not enough evidence”.</li>
          <li>Measured accuracy of the analyzers and the question answering is documented in <code>docs/EVALUATION.md</code>.</li></ul></div>
        <div class="card wide"><h2>Warnings and notes <span class="muted small">(first 200 of ${d.diagnostics_total})</span></h2><div class="scroll"><table class="dt"><tr><th>Level</th><th>Kind</th><th>Where</th><th>Message</th></tr>${rows || '<tr><td colspan="4" class="muted">None</td></tr>'}</table></div></div>`;
    } catch (e) { $("#qualityOut").innerHTML = `<div class="card">${esc(e.message)}</div>`; }
  }

  // ================================================================ analyze dialog
  const dlg = $("#analyzeDlg");
  function openAnalyze(url) { $("#repoUrl").value = url || ""; $("#progressBox").classList.add("hidden"); $("#analyzeBtn").disabled = false; dlg.showModal(); if (url) $("#analyzeForm").requestSubmit(); }
  $("#openAnalyze").onclick = () => openAnalyze("");
  $("#heroForm").onsubmit = (e) => { e.preventDefault(); openAnalyze($("#heroUrl").value.trim()); };
  $$(".examples [data-url]").forEach((b) => b.onclick = () => openAnalyze(b.dataset.url));
  $("#cancelAnalyze").onclick = () => dlg.close();
  $("#analyzeForm").onsubmit = async (e) => {
    e.preventDefault();
    $("#analyzeBtn").disabled = true;
    try {
      const job = await api("/api/analyze", { method: "POST", body: JSON.stringify({ repo_url: $("#repoUrl").value.trim(), ref: $("#repoRef").value.trim() || null, force: $("#force").checked }) });
      $("#progressBox").classList.remove("hidden");
      const poll = async () => {
        const j = await api(`/api/jobs/${job.job_id}`);
        $("#progressBar").style.width = Math.round((j.progress || 0) * 100) + "%";
        $("#progressMsg").textContent = j.message + (j.cached ? " — loaded from cache" : "");
        if (j.status === "done") { toast("Analysis complete"); dlg.close(); await loadGraphs(j.graph_id); location.hash = "#overview"; }
        else if (j.status === "failed") { $("#progressMsg").textContent = "Failed: " + j.error; $("#analyzeBtn").disabled = false; }
        else setTimeout(poll, 700);
      };
      poll();
    } catch (err) { $("#progressBox").classList.remove("hidden"); $("#progressMsg").textContent = err.message; $("#analyzeBtn").disabled = false; }
  };

  // ================================================================ help, theme, boot
  function openHelp(open) { $("#help").classList.toggle("open", open); $("#drawerShade").classList.toggle("hidden", !open); $("#help").setAttribute("aria-hidden", String(!open)); }
  $("#helpBtn").onclick = () => openHelp(true); $("#helpClose").onclick = () => openHelp(false); $("#drawerShade").onclick = () => openHelp(false);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { openHelp(false); closeSideInspector(); } });
  $("#helpLegend").innerHTML = legendHTML();
  function applyTheme(t) {
    if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
    store.set("theme", t || "");
    [cyMain, cyAsk, cyFlow, cyImpact].forEach((c) => c && c.style(cyStyle()));
  }
  $("#themeBtn").onclick = () => {
    const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    applyTheme(cur === "dark" ? "light" : "dark");
  };
  applyTheme(store.get("theme", "") || undefined);
  (async () => {
    try {
      const h = await api("/api/health"); state.health = h;
      $("#statusDot").className = "status-dot " + (h.graph_backend.ok ? "ok" : "bad");
      $("#statusDot").title = `Server OK · storage: ${h.graph_backend.backend} · questions: ${h.nlp.label}`;
    } catch (e) { $("#statusDot").className = "status-dot bad"; $("#statusDot").title = "Server not reachable"; }
    await loadGraphs();
  })();
})();
