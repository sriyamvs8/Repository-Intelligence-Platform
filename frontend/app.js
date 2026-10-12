/* Repository Knowledge Graph – UI (vanilla JS + Cytoscape). The UI only renders bounded projections
   returned by the API; it never mutates the stored graph. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k, d) { try { const v = localStorage.getItem("rkg." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("rkg." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };

  const TYPE_COLORS = {
    Repository: "#f4b860", Directory: "#8b98a9", File: "#a3b1c2", Class: "#5b9dff", Interface: "#8fb8ff", Enum: "#6fc3df",
    Method: "#9d8cff", Constructor: "#b7a9ff", Function: "#c58cff", RestApi: "#ff8a5b", SqlQuery: "#f2c94c",
    Table: "#3fcf8e", Column: "#86e3b5", Test: "#ff6b9a",
  };
  const TYPE_SHAPES = {
    Repository: "star", Directory: "tag", File: "rectangle", Class: "round-rectangle", Interface: "round-rectangle",
    Enum: "round-rectangle", Method: "ellipse", Constructor: "ellipse", Function: "ellipse", RestApi: "round-hexagon",
    SqlQuery: "diamond", Table: "barrel", Column: "ellipse", Test: "round-triangle",
  };
  const REL_COLORS = {
    CONTAINS: "#5a6577", DEFINES: "#6b7689", IMPORTS: "#7d8aa0", CALLS: "#9db4d6", INSTANTIATES: "#8aa0c8",
    EXTENDS: "#8fb8ff", IMPLEMENTS: "#8fb8ff", OVERRIDES: "#b7a9ff", HANDLED_BY: "#ff8a5b", EXECUTES: "#f2c94c",
    READS: "#3fcf8e", WRITES: "#ff9f43", HAS_COLUMN: "#86e3b5", FOREIGN_KEY: "#2bb3a3", MAPS_TO: "#4fd1c5",
    TESTS: "#ff6b9a", HTTP_CALLS: "#ff5d5d", CROSS_LANGUAGE_MAP: "#ff3b3b",
  };

  const state = {
    gid: null, meta: null, mode: "architecture", selected: null, lastQuery: null, hidden: new Set(), hiddenRels: new Set(),
    ctxEntity: null, apiKey: store.get("apiKey", ""), polling: null,
  };

  // ------------------------------------------------------------------ API
  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    if (state.apiKey) headers["X-API-Key"] = state.apiKey;
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401) {
      const k = prompt("This server requires an API key (X-API-Key):");
      if (k) { state.apiKey = k; store.set("apiKey", k); return api(path, opts); }
    }
    let body = null;
    try { body = await res.json(); } catch { body = null; }
    if (!res.ok) throw new Error((body && (body.detail?.[0]?.msg || body.detail)) || `${res.status} ${res.statusText}`);
    return body;
  }
  const qs = (o) => Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

  function toast(msg, err = false) {
    const t = document.createElement("div");
    t.className = "toast" + (err ? " err" : "");
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), err ? 6000 : 3000);
  }

  // ------------------------------------------------------------------ Cytoscape
  if (window.cytoscapeDagre) cytoscape.use(window.cytoscapeDagre);
  if (window.cytoscapeFcose) cytoscape.use(window.cytoscapeFcose);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const cy = cytoscape({
    container: $("#cy"), wheelSensitivity: 0.25, minZoom: 0.05, maxZoom: 3,
    style: buildStyle(),
  });

  function buildStyle() {
    const text = css("--text") || "#e6edf3";
    const bg = css("--canvas") || "#0f141c";
    const s = [
      { selector: "node", style: {
        "background-color": (n) => TYPE_COLORS[n.data("type")] || "#999", shape: (n) => TYPE_SHAPES[n.data("type")] || "ellipse",
        label: "data(label)", color: text, "font-size": 10, "text-valign": "bottom", "text-margin-y": 4, "text-wrap": "ellipsis",
        "text-max-width": 140, width: (n) => sizeOf(n), height: (n) => sizeOf(n) * (n.data("type") === "RestApi" ? 0.8 : 1),
        "border-width": 1.5, "border-color": bg, "text-outline-color": bg, "text-outline-width": 2, "min-zoomed-font-size": 7,
      } },
      { selector: 'node[type = "Interface"]', style: { "border-style": "dashed", "border-color": "#8fb8ff", "border-width": 2 } },
      { selector: "node[?is_test_code]", style: { "background-opacity": 0.75 } },
      { selector: "node:selected", style: { "border-width": 4, "border-color": css("--accent") || "#5b9dff" } },
      { selector: "node.root", style: { "border-width": 4, "border-color": "#ffd166" } },
      { selector: "edge", style: {
        width: 1.4, "line-color": (e) => REL_COLORS[e.data("type")] || "#888", "target-arrow-color": (e) => REL_COLORS[e.data("type")] || "#888",
        "target-arrow-shape": "triangle", "arrow-scale": 0.8, "curve-style": "bezier", opacity: 0.85,
        "font-size": 8, color: text, "text-rotation": "autorotate", "text-outline-color": bg, "text-outline-width": 2,
      } },
      { selector: 'edge[status = "CANDIDATE"]', style: { "line-style": "dashed", opacity: 0.6 } },
      { selector: 'edge[type = "CONTAINS"], edge[type = "DEFINES"]', style: { width: 0.9, opacity: 0.45, "target-arrow-shape": "none" } },
      { selector: 'edge[type = "CROSS_LANGUAGE_MAP"], edge[type = "HTTP_CALLS"]', style: { width: 3, "line-style": "dotted" } },
      { selector: "edge.labelled", style: { label: "data(type)" } },
      { selector: "edge:selected", style: { width: 3.5, opacity: 1, label: "data(type)" } },
      { selector: ".faded", style: { opacity: 0.12 } },
      { selector: ".hl", style: { opacity: 1, "z-index": 9 } },
      { selector: "edge.hl", style: { width: 3.5 } },
    ];
    return s;
  }
  function sizeOf(n) {
    const t = n.data("type");
    if (t === "Repository") return 34; if (t === "Table") return 30; if (t === "RestApi") return 30;
    if (t === "Class" || t === "Interface") return 26; if (t === "Column") return 12; if (t === "Directory" || t === "File") return 16;
    return 18;
  }

  function nodeLabel(n) {
    const t = n.type;
    if (["Method", "Function", "Constructor", "Test"].includes(t)) {
      const q = (n.qualified_name || n.name).split("(")[0].split(".");
      return q.slice(-2).join(".") + "()";
    }
    if (t === "SqlQuery") return n.name;
    return n.name || n.id;
  }

  function render(graph, title, opts = {}) {
    state.lastGraph = graph;
    cy.elements().remove();
    const nodes = (graph.nodes || []).map((n) => ({ group: "nodes", data: { ...n, label: nodeLabel(n) } }));
    const ids = new Set(nodes.map((n) => n.data.id));
    const edges = (graph.edges || []).filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({ group: "edges", data: { ...e } }));
    cy.add(nodes); cy.add(edges);
    applyFilters();
    if (opts.root) cy.$id(opts.root).addClass("root");
    runLayout(opts.layout);
    $("#viewTitle").textContent = `${title} · ${nodes.length} nodes · ${edges.length} edges`;
    if (nodes.length) $("#emptyState").classList.add("hidden");
    else if (state.gid) {
      $("#emptyState").classList.remove("hidden");
      $("#emptyState .emptyinner").innerHTML = `<h2>Nothing to show in this view</h2><p>${esc(opts.emptyMsg || "Try another view, search for an entity, or ask a question.")}</p>`;
    }
    const tn = $("#truncNote");
    const hidden = graph.hidden_neighbors ? Object.values(graph.hidden_neighbors).reduce((a, b) => a + b, 0) : 0;
    if (graph.truncated || hidden) {
      tn.textContent = graph.truncated ? `View truncated at ${nodes.length} nodes — refine with search, filters or a smaller hop count.` :
        `${hidden} neighbours of high-degree nodes hidden — select a node and expand (⊕).`;
      tn.classList.remove("hidden");
    } else tn.classList.add("hidden");
    buildLegend();
    buildFilters();
  }

  function runLayout(name) {
    const sel = name || $("#layoutSel").value;
    const visible = cy.elements().not(".filtered");
    let opts;
    if (sel === "dagre") opts = { name: "dagre", rankDir: state.mode === "ecosystem" ? "TB" : "LR", nodeSep: 26, rankSep: state.mode === "ecosystem" ? 60 : 150, edgeSep: 10, animate: false };
    else if (sel === "fcose") opts = { name: "fcose", quality: "default", nodeRepulsion: 6500, idealEdgeLength: 70, animate: false, randomize: true };
    else if (sel === "breadthfirst") opts = { name: "breadthfirst", directed: true, spacingFactor: 1.1, animate: false };
    else if (sel === "concentric") opts = { name: "concentric", minNodeSpacing: 14, animate: false, concentric: (n) => n.degree() };
    else opts = { name: "grid", animate: false };
    try { visible.layout(opts).run(); } catch (e) { cy.layout({ name: "cose", animate: false }).run(); }
    cy.fit(visible, 30);
    if (cy.zoom() > 1.3) { cy.zoom(1.3); cy.center(visible); }
  }

  function buildLegend() {
    const types = new Set(cy.nodes().map((n) => n.data("type")));
    $("#legend").innerHTML = [...types].sort().map((t) => `<span class="item"><span class="dot" style="background:${TYPE_COLORS[t]}"></span>${t}</span>`).join("")
      + (cy.edges('[status = "CANDIDATE"]').length ? `<span class="item">┄ candidate edge</span>` : "");
  }

  function buildFilters() {
    const types = [...new Set(cy.nodes().map((n) => n.data("type")))].sort();
    const rels = [...new Set(cy.edges().map((e) => e.data("type")))].sort();
    $("#typeFilters").innerHTML = types.map((t) => `<span class="fchip ${state.hidden.has(t) ? "off" : ""}" data-t="${t}"><span class="dot" style="background:${TYPE_COLORS[t]}"></span>${t} <small class="muted">${cy.nodes(`[type = "${t}"]`).length}</small></span>`).join("");
    $("#relFilters").innerHTML = rels.map((r) => `<span class="fchip ${state.hiddenRels.has(r) ? "off" : ""}" data-r="${r}"><span class="dot" style="background:${REL_COLORS[r] || "#888"}"></span>${r}</span>`).join("");
  }

  function applyFilters() {
    cy.batch(() => {
      cy.elements().removeClass("filtered").style("display", "element");
      cy.nodes().forEach((n) => { if (state.hidden.has(n.data("type"))) n.addClass("filtered").style("display", "none"); });
      cy.edges().forEach((e) => { if (state.hiddenRels.has(e.data("type"))) e.addClass("filtered").style("display", "none"); });
      const labels = $("#edgeLabels").checked;
      cy.edges().toggleClass("labelled", labels);
    });
  }

  // ------------------------------------------------------------------ loading graphs / views
  async function loadGraphs(selectGid) {
    try {
      const gs = await api("/api/graphs");
      $("#graphSelect").innerHTML = `<option value="">Analyzed repositories (${gs.length})…</option>` +
        gs.map((g) => `<option value="${g.graph_id}">${esc(g.repo_key)}${g.ref ? "@" + esc(g.ref) : ""} · ${(g.commit || "").slice(0, 7)} · ${g.entities} nodes</option>`).join("");
      const want = selectGid || store.get("gid", null);
      if (want && gs.some((g) => g.graph_id === want)) { $("#graphSelect").value = want; await openGraph(want); }
    } catch (e) { toast("Could not list graphs: " + e.message, true); }
  }

  async function openGraph(gid) {
    state.gid = gid; store.set("gid", gid);
    state.selected = null; state.ctxEntity = null;
    const s = await api(`/api/graph/${gid}/summary`);
    state.meta = s.meta;
    renderSummary(s);
    $("#emptyState").classList.add("hidden");
    await loadView();
    loadDiagnostics();
    updateSuggestions(s);
  }

  function renderSummary(s) {
    const r = s.report || {}, m = s.meta || {};
    const ec = r.entity_counts || {};
    const rc = r.relationship_counts || {};
    const sumRel = (t) => (rc[t] ? (rc[t].VERIFIED || 0) + (rc[t].CANDIDATE || 0) : 0);
    const warn = Object.entries(r.diagnostic_counts || {}).filter(([k]) => /ERROR|PARTIAL/.test(k)).reduce((a, [, v]) => a + v, 0);
    $("#summary").innerHTML = `
      <div class="repo-title">${esc(m.repo_key)}</div>
      <div class="repo-sub">${esc(m.ref || "default branch")} · ${esc((m.commit || "").slice(0, 10))} · ${esc((m.analyzed_at || "").replace("T", " "))}</div>
      <div class="stats">
        <div class="stat"><b>${r.files_analyzed ?? "–"}</b><span>files analysed</span></div>
        <div class="stat"><b>${r.total_relationships ?? "–"}</b><span>relationships</span></div>
        <div class="stat"><b>${r.apis_with_handler ?? 0}/${r.apis_total ?? 0}</b><span>APIs with handler</span></div>
        <div class="stat"><b>${ec.Table || 0}</b><span>tables</span></div>
        <div class="stat"><b>${(ec.Method || 0) + (ec.Function || 0)}</b><span>methods/functions</span></div>
        <div class="stat"><b>${sumRel("CALLS")}</b><span>resolved calls</span></div>
      </div>
      <div class="muted" style="margin-top:6px">${warn ? `⚠ ${warn} parse warnings · ` : ""}${r.elapsed_seconds ?? "?"}s analysis · <a href="#" id="dlExport">export JSON</a></div>`;
    $("#dlExport").onclick = async (ev) => {
      ev.preventDefault();
      const data = await api(`/api/graph/${state.gid}/export`);
      const blob = new Blob([JSON.stringify(data, null, 1)], { type: "application/json" });
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `${(m.repo_key || "graph").replace("/", "_")}_kg.json`; a.click();
    };
  }

  function updateSuggestions(s) {
    const apis = s.apis || [];
    const api0 = apis.find((a) => a.http_method === "POST") || apis.find((a) => (a.path || "/").length > 1) || apis[0], tbl = (s.tables || [])[0], hub = (s.most_called || [])[0];
    const list = [];
    if (api0) list.push(`Which tables are touched by ${api0.name.replace("{}", "{id}")}?`, `Which method handles ${api0.name.replace("{}", "{id}")}?`);
    if (hub) list.push(`Who calls ${nodeLabel(hub.node).replace("()", "")}?`);
    if (tbl) list.push(`What is impacted if I change the ${tbl.name} table?`, `What columns does the ${tbl.name} table have?`);
    list.push("list all endpoints", "give me an overview of this repository");
    $("#chat").innerHTML = `<div class="hint">Ask about <b>${esc(s.meta.repo_key)}</b>, for example:<ul class="sugg">${list.map((q) => `<li>${esc(q)}</li>`).join("")}</ul></div>`;
  }

  async function loadView() {
    if (!state.gid) return;
    const p = { mode: state.mode, max_nodes: $("#maxNodes").value, include_candidates: $("#incCand").checked, include_structure: $("#incStruct").checked };
    if (state.mode === "hops") {
      if (!state.selected) { toast("Select an entity first (search or click a node)."); setMode("architecture"); return; }
      p.root_id = state.selected; p.hops = $("#hops").value;
    }
    try {
      const g = await api(`/api/graph/${state.gid}/view?${qs(p)}`);
      const titles = { architecture: "Architecture: API → code → data", ecosystem: "Repository structure", hops: `Focus (${p.hops} hop${p.hops > 1 ? "s" : ""})`, "cross-language": "Cross-language HTTP calls" };
      render(g, titles[state.mode], { root: p.root_id, emptyMsg: state.mode === "cross-language" ? "No HTTP calls between components were detected in this repository." : state.mode === "architecture" ? "No REST endpoints or tables were found. Try the Structure view or search." : undefined });
    } catch (e) { toast(e.message, true); }
  }

  function setMode(m) {
    state.mode = m;
    $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
  }

  // ------------------------------------------------------------------ details panel
  async function showNode(id, { focus = false } = {}) {
    if (!state.gid) return;
    state.selected = id; state.ctxEntity = id;
    try {
      const d = await api(`/api/graph/${state.gid}/node?${qs({ entity_id: id })}`);
      const n = d.node;
      const props = ["qualified_name", "file", "line", "language", "signature", "http_method", "path", "operation", "data_type", "return_type", "parse_status", "operation_id", "summary", "docstring"]
        .filter((k) => n[k] !== undefined && n[k] !== null && n[k] !== "")
        .map((k) => `<dt>${k.replace("_", " ")}</dt><dd>${k === "file" ? `<code>${esc(n.file)}${n.line ? ":" + n.line : ""}${n.end_line ? "–" + n.end_line : ""}</code>` : esc(String(n[k]))}</dd>`).join("");
      const groups = Object.entries(d.relationships).sort().map(([k, items]) => {
        const [rel, dir] = k.split(":");
        return `<div class="relgroup"><h4>${dir === "out" ? "→" : "←"} ${rel} (${items.length})</h4>${items.slice(0, 40).map((it) =>
          `<div class="rel" data-id="${esc(it.node.id)}"><span class="tchip" style="background:${TYPE_COLORS[it.node.type]}">${it.node.type}</span><span>${esc(nodeLabel(it.node))}${it.edge.status === "CANDIDATE" ? ' <span class="cand">candidate</span>' : ""}<br><span class="muted ev" title="${esc(it.edge.reason || "")}">${esc(it.edge.evidence || "")}${it.edge.file ? ` · ${esc(it.edge.file)}:${it.edge.line ?? ""}` : ""}</span></span></div>`).join("")}${items.length > 40 ? `<div class="muted">…${items.length - 40} more</div>` : ""}</div>`;
      }).join("");
      const sql = n.sql ? `<pre class="sql">${esc(n.sql)}</pre>` : "";
      $("#details").innerHTML = `
        <div class="pad">
          <div class="dh"><span class="tchip" style="background:${TYPE_COLORS[n.type]}">${n.type}</span><h2>${esc(nodeLabel(n))}</h2></div>
          <dl class="kv">${props}<dt>id</dt><dd><code>${esc(n.id)}</code></dd><dt>degree</dt><dd>${d.degree}</dd></dl>
          ${sql}
          <div class="actions">
            <button data-act="focus">Focus neighbourhood</button>
            <button data-act="impact">Impact analysis</button>
            <button data-act="callers">Callers</button>
            <button data-act="deps">Dependencies</button>
            ${n.type === "RestApi" ? '<button data-act="api2db">API → database</button>' : ""}
            <button data-act="tests">Tests</button>
          </div>
          ${groups || '<div class="muted">No relationships.</div>'}
        </div>`;
      $$("#details .rel").forEach((el) => el.onclick = () => { selectInGraph(el.dataset.id); showNode(el.dataset.id); });
      $$("#details [data-act]").forEach((b) => b.onclick = () => nodeAction(b.dataset.act, n));
      switchTab("details");
      if (focus) { setMode("hops"); await loadView(); }
    } catch (e) { toast(e.message, true); }
  }

  async function nodeAction(act, n) {
    const enc = encodeURIComponent;
    try {
      if (act === "focus") { setMode("hops"); return loadView(); }
      if (act === "impact") { const r = await api(`/api/graph/${state.gid}/impact?root_id=${enc(n.id)}&depth=3&include_candidates=${$("#incCand").checked}`); return render(r, `Impact of ${nodeLabel(n)} (${r.total_affected} affected)`, { root: n.id }); }
      if (act === "deps") { const r = await api(`/api/graph/${state.gid}/query/dependencies_of?root_id=${enc(n.id)}&depth=2`); return render(r, `Dependencies of ${nodeLabel(n)}`, { root: n.id }); }
      if (act === "api2db") { const r = await api(`/api/graph/${state.gid}/query/api_to_database_path?root_id=${enc(n.id)}`); return render(r, `${n.name} → database`, { root: n.id }); }
      if (act === "callers") return askQuestion(`Who calls ${n.type === "RestApi" ? n.name : (n.qualified_name || n.name).split("(")[0]}?`, n.id);
      if (act === "tests") return askQuestion(`Which tests cover ${n.type === "RestApi" ? n.name : (n.qualified_name || n.name).split("(")[0]}?`, n.id);
    } catch (e) { toast(e.message, true); }
  }

  function selectInGraph(id) {
    cy.$(":selected").unselect();
    const el = cy.$id(id);
    if (el.length) { el.select(); cy.animate({ center: { eles: el }, duration: 250 }); }
  }

  function showEdge(e) {
    const d = e.data();
    const s = cy.$id(d.source).data(), t = cy.$id(d.target).data();
    $("#details").innerHTML = `<div class="pad">
      <div class="dh"><span class="tchip" style="background:${REL_COLORS[d.type] || "#999"}">${d.type}</span><h2>${esc(nodeLabel(s))} → ${esc(nodeLabel(t))}</h2></div>
      <dl class="kv"><dt>status</dt><dd>${d.status}${d.status === "CANDIDATE" ? " (plausible, not proven)" : ""}</dd><dt>evidence</dt><dd><code>${esc(d.evidence || "")}</code></dd>
      <dt>location</dt><dd>${d.file ? `<code>${esc(d.file)}:${d.line ?? ""}</code>` : "–"}</dd><dt>mechanism</dt><dd>${esc(d.source_kind || "")}</dd>
      <dt>reason</dt><dd>${esc(d.reason || "")}</dd><dt>occurrences</dt><dd>${d.occurrences ?? 1}</dd></dl></div>`;
    switchTab("details");
  }

  // ------------------------------------------------------------------ ask
  async function askQuestion(q, ctx) {
    if (!state.gid) return toast("Analyze or select a repository first.", true);
    switchTab("ask");
    const chat = $("#chat");
    chat.querySelector(".hint")?.remove();
    chat.insertAdjacentHTML("beforeend", `<div class="msg q">${esc(q)}</div>`);
    const holder = document.createElement("div"); holder.className = "msg"; holder.innerHTML = '<span class="muted">Thinking over the graph…</span>';
    chat.appendChild(holder); chat.scrollTop = chat.scrollHeight;
    $("#askBtn").disabled = true;
    try {
      const r = await api(`/api/graph/${state.gid}/ask`, { method: "POST", body: JSON.stringify({ question: q, context_entity_id: ctx || state.ctxEntity, include_candidates: $("#incCand").checked }) });
      if (r.resolved_entities?.length) state.ctxEntity = r.resolved_entities[0].id;
      const md = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
      const facts = (r.facts || []).slice(0, 60).map((f) => `<div class="fact"><b>${esc(f.subject)}</b> ${esc(f.relationship)} <b>${esc(f.object ?? "")}</b> ${f.validation_status ? `<span class="${f.validation_status === "CANDIDATE" ? "cand" : "muted"}">[${f.validation_status}]</span>` : ""}
        ${f.source_file ? `<span class="ev">${esc(f.source_file)}:${f.line ?? ""}${f.evidence ? " — " + esc(f.evidence) : ""}</span>` : ""}</div>`).join("");
      holder.innerHTML = `<div class="meta"><span class="st ${r.status}">${r.status.replace("_", " ")}</span><span class="badge">${r.query_plan.intent}</span><span class="badge">planner: ${esc(r.planner)}</span><span class="badge">${r.elapsed_ms} ms</span></div>
        <div class="ans">${md(r.answer)}</div>
        ${r.explanation ? `<div class="expl"><span class="badge">LLM explanation · grounding-checked</span><div class="ans">${md(r.explanation)}</div></div>` : ""}
        ${r.candidates?.length ? `<div class="cands">${r.candidates.map((c) => `<button data-cid="${esc(c.id)}" title="${esc(c.qualified_name || c.id)}">${esc(c.type)}: ${esc(nodeLabel(c))}</button>`).join("")}</div>` : ""}
        ${(r.limitations || []).map((l) => `<div class="muted" style="margin-top:6px">ⓘ ${esc(l)}</div>`).join("")}
        ${facts ? `<details class="facts"><summary>${r.facts.length} supporting fact(s) from the graph</summary>${facts}</details>` : ""}
        <details class="facts"><summary>query plan</summary><pre class="sql">${esc(JSON.stringify(r.query_plan, null, 1))}</pre></details>
        <div class="fb"><button data-h="1">👍 helpful</button><button data-h="0">👎 not helpful</button></div>`;
      holder.querySelectorAll("[data-cid]").forEach((b) => b.onclick = () => askQuestion(q, b.dataset.cid));
      holder.querySelectorAll("[data-h]").forEach((b) => b.onclick = async () => {
        await api(`/api/graph/${state.gid}/ask/feedback`, { method: "POST", body: JSON.stringify({ question_id: r.question_id, question: q, helpful: b.dataset.h === "1" }) });
        b.parentElement.innerHTML = '<span class="muted">Thanks — feedback recorded.</span>';
      });
      if (r.graph?.nodes?.length) render(r.graph, `Answer: ${q.slice(0, 60)}`, { root: r.resolved_entities?.[0]?.id });
    } catch (e) {
      holder.innerHTML = `<span class="st NOT_FOUND">ERROR</span> ${esc(e.message)}`;
    } finally {
      $("#askBtn").disabled = false; chat.scrollTop = chat.scrollHeight;
    }
  }

  // ------------------------------------------------------------------ diagnostics
  async function loadDiagnostics() {
    try {
      const d = await api(`/api/graph/${state.gid}/diagnostics?limit=300`);
      const r = d.report;
      const rel = Object.entries(r.relationship_counts || {}).map(([k, v]) => `<tr><td>${k}</td><td>${v.VERIFIED || 0}</td><td>${v.CANDIDATE || 0}</td></tr>`).join("");
      const langs = Object.entries(r.files_by_language || {}).map(([k, v]) => `<tr><td>${k}</td><td>${v.files}</td><td>${v.OK || 0}</td><td>${(v.PARTIAL || 0) + (v.FAILED || 0)}</td></tr>`).join("");
      const codes = Object.entries(r.diagnostic_counts || {}).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");
      const rows = d.diagnostics.slice(0, 200).map((x) => `<tr><td>${x.level}</td><td>${x.code}</td><td><code>${esc(x.file || "")}${x.line ? ":" + x.line : ""}</code><br>${esc(x.message)}</td></tr>`).join("");
      $("#diag").innerHTML = `<div class="pad">
        <h3>Quality report</h3>
        <p class="muted">Calls resolved to repository code: <b>${r.calls_resolved}</b> · unresolved/external (library) calls: ${r.calls_unresolved_or_external} · rejected edges: ${r.rejected_relationships} · APIs documented only: ${r.apis_documented_only}</p>
        <table class="dt"><tr><th>Language</th><th>files</th><th>OK</th><th>partial/failed</th></tr>${langs}</table><br>
        <table class="dt"><tr><th>Relationship</th><th>verified</th><th>candidate</th></tr>${rel}</table><br>
        <table class="dt"><tr><th>Diagnostic</th><th>count</th></tr>${codes || "<tr><td colspan=2>none</td></tr>"}</table><br>
        <h3>Diagnostics (first 200 of ${d.diagnostics_total})</h3><table class="dt">${rows || "<tr><td>none</td></tr>"}</table></div>`;
    } catch (e) { $("#diag").textContent = e.message; }
  }

  // ------------------------------------------------------------------ analyze
  async function analyze(url, ref, force) {
    $("#analyzeBtn").disabled = true;
    try {
      const job = await api("/api/analyze", { method: "POST", body: JSON.stringify({ repo_url: url, ref: ref || null, force }) });
      $("#progress").classList.remove("hidden");
      const poll = async () => {
        const j = await api(`/api/jobs/${job.job_id}`);
        $("#progressBar").style.width = Math.round((j.progress || 0) * 100) + "%";
        $("#progressMsg").textContent = `${j.message}${j.cached ? " (cached)" : ""}`;
        if (j.status === "done") {
          $("#progress").classList.add("hidden"); $("#analyzeBtn").disabled = false;
          toast(j.cached ? "Already analyzed at this commit — loaded cached graph." : "Analysis complete.");
          await loadGraphs(j.graph_id);
        } else if (j.status === "failed") {
          $("#progress").classList.add("hidden"); $("#analyzeBtn").disabled = false;
          toast("Analysis failed: " + j.error, true);
        } else setTimeout(poll, 700);
      };
      poll();
    } catch (e) { $("#analyzeBtn").disabled = false; toast(e.message, true); }
  }

  // ------------------------------------------------------------------ search
  let searchTimer = null;
  $("#searchBox").addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = $("#searchBox").value.trim();
    if (!q || !state.gid) { $("#searchResults").innerHTML = ""; return; }
    searchTimer = setTimeout(async () => {
      try {
        const res = await api(`/api/graph/${state.gid}/search?${qs({ q, limit: 25 })}`);
        $("#searchResults").innerHTML = res.length ? res.map((n) => `<li data-id="${esc(n.id)}"><span class="tchip" style="background:${TYPE_COLORS[n.type]}">${n.type}</span><span><span class="nm">${esc(nodeLabel(n))}</span><span class="fl">${esc(n.file || n.qualified_name || "")}${n.line ? ":" + n.line : ""}</span></span></li>`).join("") : '<li class="muted">No matches</li>';
        $$("#searchResults li[data-id]").forEach((li) => li.onclick = () => showNode(li.dataset.id, { focus: true }));
      } catch (e) { toast(e.message, true); }
    }, 220);
  });

  // ------------------------------------------------------------------ wiring
  function switchTab(t) {
    $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    $$(".tab").forEach((s) => s.classList.toggle("on", s.id === "tab-" + t));
  }
  $$(".tabs button").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
  $("#analyzeForm").onsubmit = (e) => { e.preventDefault(); analyze($("#repoUrl").value.trim(), $("#repoRef").value.trim(), $("#force").checked); };
  $$(".examples button").forEach((b) => b.onclick = () => { $("#repoUrl").value = b.dataset.url; analyze(b.dataset.url, "", false); });
  $("#graphSelect").onchange = (e) => e.target.value && openGraph(e.target.value);
  $$("#modeSeg button").forEach((b) => b.onclick = () => { setMode(b.dataset.mode); loadView(); });
  $("#hops").oninput = () => { $("#hopsVal").textContent = $("#hops").value; };
  $("#hops").onchange = () => state.mode === "hops" && loadView();
  ["#incCand", "#incStruct", "#maxNodes"].forEach((s) => $(s).onchange = loadView);
  $("#edgeLabels").onchange = applyFilters;
  $("#layoutSel").onchange = () => runLayout();
  $("#fitBtn").onclick = () => cy.fit(cy.elements().not(".filtered"), 30);
  $("#zoomIn").onclick = () => cy.zoom({ level: cy.zoom() * 1.25, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  $("#zoomOut").onclick = () => cy.zoom({ level: cy.zoom() / 1.25, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  $("#pngBtn").onclick = () => { const a = document.createElement("a"); a.href = cy.png({ full: true, scale: 2, bg: css("--canvas") }); a.download = "knowledge-graph.png"; a.click(); };
  $("#expandBtn").onclick = async () => {
    const sel = cy.$("node:selected");
    if (!sel.length) return toast("Select a node to expand.");
    const id = sel[0].id();
    const g = await api(`/api/graph/${state.gid}/view?${qs({ mode: "hops", root_id: id, hops: 1, max_nodes: 80, include_candidates: $("#incCand").checked, include_structure: $("#incStruct").checked })}`);
    const merged = { nodes: [...(state.lastGraph?.nodes || [])], edges: [...(state.lastGraph?.edges || [])] };
    const have = new Set(merged.nodes.map((n) => n.id)), haveE = new Set(merged.edges.map((e) => e.id));
    g.nodes.forEach((n) => !have.has(n.id) && merged.nodes.push(n));
    g.edges.forEach((e) => !haveE.has(e.id) && merged.edges.push(e));
    render(merged, $("#viewTitle").textContent.split(" · ")[0] + " (expanded)", { root: id });
  };
  $("#runQuery").onclick = async () => {
    if (!state.gid) return;
    const name = $("#querySel").value;
    if (["impact_of", "dependencies_of"].includes(name) && !state.selected) return toast("Select an entity first.");
    try {
      const r = await api(`/api/graph/${state.gid}/query/${name}?${qs({ root_id: ["impact_of", "dependencies_of"].includes(name) ? state.selected : undefined, include_candidates: $("#incCand").checked, max_nodes: $("#maxNodes").value })}`);
      render(r, $("#querySel").selectedOptions[0].textContent + (r.rows ? ` (${r.rows.length} rows)` : ""), { root: state.selected });
    } catch (e) { toast(e.message, true); }
  };
  $("#typeFilters").onclick = (e) => { const c = e.target.closest(".fchip"); if (!c) return; const t = c.dataset.t; state.hidden.has(t) ? state.hidden.delete(t) : state.hidden.add(t); applyFilters(); buildFilters(); };
  $("#relFilters").onclick = (e) => { const c = e.target.closest(".fchip"); if (!c) return; const r = c.dataset.r; state.hiddenRels.has(r) ? state.hiddenRels.delete(r) : state.hiddenRels.add(r); applyFilters(); buildFilters(); };
  $("#resetFilters").onclick = () => { state.hidden.clear(); state.hiddenRels.clear(); applyFilters(); buildFilters(); };
  $("#askForm").onsubmit = (e) => { e.preventDefault(); const q = $("#question").value.trim(); if (q) { $("#question").value = ""; askQuestion(q); } };
  $("#question").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#askForm").requestSubmit(); } });
  $("#chat").addEventListener("click", (e) => { if (e.target.matches(".sugg li")) askQuestion(e.target.textContent); });

  cy.on("tap", "node", (e) => showNode(e.target.id()));
  cy.on("tap", "edge", (e) => showEdge(e.target));
  cy.on("dbltap", "node", (e) => showNode(e.target.id(), { focus: true }));
  cy.on("mouseover", "node", (e) => {
    const n = e.target; const nb = n.closedNeighborhood();
    cy.elements().not(nb).addClass("faded"); nb.addClass("hl");
  });
  cy.on("mouseout", "node", () => cy.elements().removeClass("faded hl"));

  // theme
  const applyTheme = (t) => { document.documentElement.dataset.theme = t; store.set("theme", t); cy.style(buildStyle()); };
  $("#themeBtn").onclick = () => applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  applyTheme(store.get("theme", window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));

  // boot
  (async () => {
    try {
      const h = await api("/api/health");
      $("#badges").innerHTML = `<span class="badge ${h.graph_backend.ok ? "ok" : "bad"}">● ${esc(h.graph_backend.backend)}</span><span class="badge">${h.nlp.gemini ? "Gemini " + esc(h.nlp.model) : "rule-based NLP"}</span>`;
    } catch (e) { $("#badges").innerHTML = '<span class="badge bad">● API offline</span>'; }
    loadGraphs();
  })();
})();
