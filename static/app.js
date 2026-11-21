/* Deep Research frontend: config -> form -> SSE timeline -> report */

const $ = (id) => document.getElementById(id);
const EXAMPLES = [
  "Most popular AI agent frameworks in 2026",
  "State of PCIe 7.0 adoption in data centers",
];

const FOOT_LINE = "The searching is parallel. The waiting, sadly, is not.";

const state = {
  config: null,
  model: null,
  searches: 5,
  stageStart: {},
  trace: null,
  markdown: "",
  query: "",
  running: false,
  clarify: true,
  qa: [],
  runToken: 0,
  pendingAnswer: null,
};

/* ── Boot ─────────────────────────────────────────────── */
async function boot() {
  const res = await fetch("/api/config");
  state.config = await res.json();
  $("pill-version").textContent = "v" + state.config.version;
  $("foot-line").textContent = FOOT_LINE;

  renderExamples();
  renderModels();
  selectModel(state.config.default_model);
  initSearches();

  const email = state.config.email;
  $("email-to").placeholder = email.default_to || "you@example.com";
  if (!email.configured) {
    $("email-enabled").checked = false;
    $("email-enabled").disabled = true;
    $("delivery-note").textContent = "Email is not configured on the server; the report is shown here only.";
  }
  syncEmail();

  $("email-enabled").addEventListener("change", syncEmail);
  state.clarify = $("clarify-enabled").checked;
  $("clarify-enabled").addEventListener("change", (e) => { state.clarify = e.target.checked; });
  $("clarify-form").addEventListener("submit", onClarifyContinue);
  $("clarify-skip").addEventListener("click", () => answer({ action: "skip" }));
  $("form").addEventListener("submit", onSubmit);
  $("new-run").addEventListener("click", resetToCompose);
  $("copy-md").addEventListener("click", copyMarkdown);
  $("download-md").addEventListener("click", downloadMarkdown);
  $("query").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") $("form").requestSubmit();
  });
}

function renderExamples() {
  const wrap = $("examples");
  EXAMPLES.forEach((text) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.textContent = text;
    b.addEventListener("click", () => { $("query").value = text; $("query").focus(); });
    wrap.appendChild(b);
  });
}

function renderModels() {
  const wrap = $("models");
  wrap.innerHTML = "";
  state.config.models.forEach((m) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "model";
    card.role = "radio";
    card.dataset.id = m.id;
    card.setAttribute("aria-checked", "false");
    card.innerHTML = `
      <span class="tier tier-${m.tier}">${m.tier}</span>
      <span class="model-name">${m.label}</span>
      <span class="model-cost">≈ ${fmtCost(m.est_cost)} / run</span>
      <span class="model-blurb">${m.blurb}</span>`;
    card.title = `$${m.input} in · $${m.output} out per 1M tokens`;
    card.addEventListener("click", () => selectModel(m.id));
    wrap.appendChild(card);
  });
}

function selectModel(id) {
  state.model = state.config.models.find((m) => m.id === id) || state.config.models[0];
  document.querySelectorAll(".model").forEach((c) => c.setAttribute("aria-checked", String(c.dataset.id === state.model.id)));
  $("pill-model").textContent = state.model.id;
  updateCosts();
}

/* Cost estimate: fixed part + per-search part + web-search tool fee */
function estimate(model, searches) {
  const c = state.config.cost_model;
  const tin = c.fixed_input + c.per_search_input * searches;
  const tout = c.fixed_output + c.per_search_output * searches;
  return (model.input * tin + model.output * tout) / 1e6 + c.web_search_call_usd * searches;
}

function updateCosts() {
  const n = state.searches;
  $("go-cost").textContent = "≈ " + fmtCost(estimate(state.model, n));
  document.querySelectorAll(".model").forEach((card) => {
    const m = state.config.models.find((x) => x.id === card.dataset.id);
    card.querySelector(".model-cost").textContent = `≈ ${fmtCost(estimate(m, n))} / run`;
  });
}

function initSearches() {
  const s = state.config.searches;
  const el = $("searches");
  el.min = s.min; el.max = s.max; el.value = s.default;
  state.searches = s.default;
  const paint = () => {
    state.searches = Number(el.value);
    $("searches-value").textContent = el.value;
    el.style.setProperty("--pct", ((el.value - el.min) / (el.max - el.min) * 100) + "%");
    updateCosts();
  };
  el.addEventListener("input", paint);
  paint();
}

function syncEmail() {
  const on = $("email-enabled").checked;
  $("email-to").disabled = !on;
  $("deliver-title").textContent = on ? "Send email" : "Email off — shown on screen";
}

function fmtCost(v) { return v < 0.01 ? "<$0.01" : "$" + v.toFixed(2); }

/* ── Run ──────────────────────────────────────────────── */
async function onSubmit(e) {
  e.preventDefault();
  if (state.running) return;
  const query = $("query").value.trim();
  if (query.length < 3) return;

  const body = {
    query,
    model: state.model.id,
    num_searches: state.searches,
    email_enabled: $("email-enabled").checked,
    email_to: $("email-to").value.trim() || null,
  };

  const token = ++state.runToken;
  startRunView(query);
  state.running = true;
  $("go").disabled = true;

  try {
    let qa = [];
    if (state.clarify) {
      qa = await runClarify(query, token);
      if (qa === null) return; // user left for a new research mid-clarify
    } else {
      setStage("clarify", "skipped");
      $("clarify-note").textContent = "skipped";
    }
    state.qa = qa;
    const res = await fetch("/api/research", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, qa }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(errorText(err, res.status));
    }
    await readSSE(res.body, handleEvent);
  } catch (err) {
    showError(err.message || String(err));
  } finally {
    if (token === state.runToken) {
      state.running = false;
      $("go").disabled = false;
    }
  }
}

/* ── Clarify: up to max_rounds of questions, driven here, not by the model ── */
async function runClarify(query, token) {
  const max = state.config.clarify.max_rounds;
  const note = $("clarify-note");
  let qa = [];
  let skipped = false;
  let failed = false;
  setStage("clarify", "running");
  for (let round = 1; round <= max; round++) {
    note.textContent = "thinking…";
    let res;
    try {
      res = await postClarify(query, qa);
    } catch (err) {
      if (token !== state.runToken) return null;
      failed = true;
      break;
    }
    if (token !== state.runToken) return null;
    if (!res.needs_clarify) break;

    note.textContent = max > 1 ? `round ${round} / ${max}` : "";
    const reply = await askUser(res);
    if (token !== state.runToken || reply.action === "cancel") return null;
    if (reply.action === "skip") { skipped = true; break; }
    qa = qa.concat(res.questions.map((q, i) => ({ q, a: reply.answers[i] || "no preference" })));
  }
  $("clarify-form").classList.add("hidden");
  setStage("clarify", "done");
  if (qa.length) note.textContent = `${qa.length} answer${qa.length === 1 ? "" : "s"}`;
  else if (skipped) note.textContent = "skipped";
  else if (failed) note.textContent = "questions unavailable, researching as is";
  else note.textContent = "Question is clear";
  return qa;
}

async function postClarify(query, qa) {
  const res = await fetch("/api/clarify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, qa, model: state.model.id }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(errorText(err, res.status));
  }
  return res.json();
}

/* FastAPI sends a string detail for HTTPException and a list of {msg, ...} for 422. */
function errorText(err, status) {
  const d = err && err.detail;
  if (Array.isArray(d) && d.length) return d[0].msg || `HTTP ${status}`;
  if (typeof d === "string" && d) return d;
  return `HTTP ${status}`;
}

/* Show one round of questions; resolves when the user continues, skips or leaves. */
function askUser(res) {
  const fields = $("clarify-fields");
  fields.innerHTML = "";
  res.questions.forEach((q, i) => {
    const id = `clarify-a-${i}`;
    const wrap = document.createElement("div");
    wrap.className = "clarify-field";
    wrap.innerHTML = `<label for="${id}">${esc(q)}</label>
      <input type="text" id="${id}" class="clarify-input" maxlength="500" placeholder="no preference" />`;
    fields.appendChild(wrap);
  });
  $("clarify-brief").textContent = res.clarification || "";
  $("clarify-form").classList.remove("hidden");
  const first = fields.querySelector("input");
  if (first) first.focus({ preventScroll: true });
  return new Promise((resolve) => { state.pendingAnswer = resolve; });
}

function answer(reply) {
  const resolve = state.pendingAnswer;
  if (!resolve) return;
  state.pendingAnswer = null;
  $("clarify-form").classList.add("hidden");
  resolve(reply);
}

function onClarifyContinue(e) {
  e.preventDefault();
  const answers = [...document.querySelectorAll(".clarify-input")].map((el) => el.value.trim());
  answer({ action: "continue", answers });
}

async function readSSE(stream, onEvent) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)));
    }
  }
}

function startRunView(query) {
  state.query = query;
  state.markdown = "";
  state.stageStart = {};
  $("compose").classList.add("hidden");
  $("run").classList.remove("hidden");
  $("run-query").textContent = query;
  $("report").classList.add("hidden");
  $("error").classList.add("hidden");
  $("plan-list").innerHTML = "";
  $("search-counter").textContent = "";
  $("clarify-note").textContent = "";
  $("clarify-fields").innerHTML = "";
  $("clarify-brief").textContent = "";
  $("clarify-form").classList.add("hidden");
  state.qa = [];
  document.querySelectorAll(".step").forEach((s) => { s.dataset.status = ""; });
  document.querySelectorAll(".step-time").forEach((t) => { t.textContent = ""; });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function resetToCompose() {
  // Leaving mid-clarify abandons that run; a research stream keeps going as before.
  if (state.pendingAnswer || document.querySelector('.step[data-stage="clarify"][data-status="running"]')) {
    state.runToken++;
    answer({ action: "cancel" });
    state.running = false;
    $("go").disabled = false;
  }
  $("run").classList.add("hidden");
  $("compose").classList.remove("hidden");
  $("query").focus();
}

/* ── Events ───────────────────────────────────────────── */
function handleEvent(ev) {
  switch (ev.type) {
    case "trace":
      state.trace = ev.url;
      $("trace-link").href = ev.url;
      break;
    case "stage":
      setStage(ev.stage, ev.status);
      if (ev.stage === "plan" && ev.status === "done") renderPlan(ev.searches);
      if (ev.stage === "search" && ev.status === "running") $("search-counter").textContent = `0 / ${ev.total}`;
      if (ev.stage === "deliver") {
        if (ev.status === "done") $("deliver-title").textContent = `Emailed to ${ev.to}`;
        if (ev.status === "failed") $("deliver-title").textContent = `Email failed: ${ev.error}`;
        if (ev.status === "skipped") $("deliver-title").textContent = ev.reason ? `Email skipped (${ev.reason}) — shown on screen` : "Email off — shown on screen";
      }
      break;
    case "search_done":
      markSearch(ev.index, "done", ev.summary);
      break;
    case "search_failed":
      markSearch(ev.index, "failed", "Failed: " + ev.error);
      break;
    case "report":
      renderReport(ev);
      break;
    case "error":
      showError(ev.message);
      break;
  }
}

function setStage(stage, status) {
  const step = document.querySelector(`.step[data-stage="${stage}"]`);
  if (!step) return;
  step.dataset.status = status;
  const t = step.querySelector(".step-time");
  if (status === "running") state.stageStart[stage] = performance.now();
  else if (state.stageStart[stage]) t.textContent = ((performance.now() - state.stageStart[stage]) / 1000).toFixed(1) + "s";
}

function renderPlan(searches) {
  const list = $("plan-list");
  list.innerHTML = "";
  searches.forEach((s, i) => {
    const li = document.createElement("li");
    li.className = "search";
    li.dataset.index = i;
    li.dataset.state = "running";
    li.style.animationDelay = `${i * 60}ms`;
    li.innerHTML = `
      <span class="search-state"></span>
      <details>
        <summary>
          <div class="search-q">${esc(s.query)}</div>
          <div class="search-why">${esc(s.reason)}</div>
        </summary>
        <div class="search-sum"></div>
      </details>`;
    list.appendChild(li);
  });
}

function markSearch(index, stateName, text) {
  const li = document.querySelector(`.search[data-index="${index}"]`);
  if (!li) return;
  li.dataset.state = stateName;
  li.querySelector(".search-sum").textContent = text;
  const total = document.querySelectorAll(".search").length;
  const done = document.querySelectorAll('.search[data-state="done"], .search[data-state="failed"]').length;
  $("search-counter").textContent = `${done} / ${total}`;
}

function renderReport(ev) {
  state.markdown = ev.markdown;
  $("summary").textContent = ev.summary;
  const n = state.qa.length;
  const clar = n ? ` · ${n} clarification${n === 1 ? "" : "s"}` : "";
  $("report-meta").textContent = `· ${state.model.id} · ${state.searches} searches${clar} · ${ev.elapsed}s`;
  const html = DOMPurify.sanitize(marked.parse(ev.markdown, { gfm: true }));
  $("prose").innerHTML = html;

  const fu = $("followups");
  fu.innerHTML = "";
  if (ev.follow_ups && ev.follow_ups.length) {
    const label = document.createElement("span");
    label.className = "eyebrow";
    label.textContent = "Dig deeper";
    fu.appendChild(label);
    ev.follow_ups.forEach((q) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "chip"; b.textContent = q;
      b.addEventListener("click", () => { resetToCompose(); $("query").value = q; });
      fu.appendChild(b);
    });
  }
  $("report").classList.remove("hidden");
  $("report").scrollIntoView({ behavior: "smooth", block: "start" });
}

function showError(msg) {
  const el = $("error");
  el.textContent = msg;
  el.classList.remove("hidden");
  document.querySelectorAll('.step[data-status="running"]').forEach((s) => { s.dataset.status = "failed"; });
}

/* ── Report actions ───────────────────────────────────── */
async function copyMarkdown() {
  await navigator.clipboard.writeText(state.markdown);
  flash($("copy-md"), "Copied");
}
function downloadMarkdown() {
  const blob = new Blob([state.markdown], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = slug(state.query) + ".md";
  a.click();
  URL.revokeObjectURL(a.href);
}
function flash(btn, text) {
  const old = btn.textContent; btn.textContent = text;
  setTimeout(() => { btn.textContent = old; }, 1200);
}
function slug(s) { return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "").slice(0, 60) || "report"; }
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

boot();
