// psyneulink-ui frontend.
//
// Vanilla JS, no build step. Three responsibilities:
//   1. Open a UI session on page load (POST /api/sessions).
//   2. On user submit, stream chat events from the backend over SSE
//      (parsing the wire format ourselves because EventSource is GET-only
//      and we want POST + JSON body).
//   3. Poll /graph/revision every 1.5s; refetch /graph (and update the
//      <img>) only when the revision number has bumped.

"use strict";

const state = {
  sid: null,
  lastRevision: 0,
  lastComposition: null,
  pollTimer: null,
  inFlight: false,
  currentAssistantMsg: null,
  toolCardsById: new Map(),
  // Latest composition rendered into the code pane. Tracked separately
  // from ``lastComposition`` so a tab switch (Graph → Code) can decide
  // whether the visible code is still current without re-fetching.
  lastCodeRevision: -1,
  lastCodeComposition: null,
  activeRightTab: "graph",
};

const els = {
  status: document.getElementById("status"),
  backendLabel: document.getElementById("backend-label"),
  scrollback: document.getElementById("scrollback"),
  composer: document.getElementById("composer"),
  composerInput: document.getElementById("composer-input"),
  composerSend: document.getElementById("composer-send"),
  composerStop: document.getElementById("composer-stop"),
  resources: document.getElementById("resources"),
  uploadPdf: document.getElementById("upload-pdf"),
  uploadData: document.getElementById("upload-data"),
  uploadModel: document.getElementById("upload-model"),
  saveModelBtn: document.getElementById("save-model-btn"),
  graphImg: document.getElementById("graph-img"),
  graphEmpty: document.getElementById("graph-empty"),
  graphMeta: document.getElementById("graph-meta"),
  graphFormat: document.getElementById("graph-format"),
  graphRefresh: document.getElementById("graph-refresh"),
  tabGraph: document.getElementById("tab-graph"),
  tabCode: document.getElementById("tab-code"),
  panelGraph: document.getElementById("panel-graph"),
  panelCode: document.getElementById("panel-code"),
  codePre: document.querySelector(".code-pre"),
  codeText: document.getElementById("code-text"),
  codeEmpty: document.getElementById("code-empty"),
  codeMeta: document.getElementById("code-meta"),
  codeRefresh: document.getElementById("code-refresh"),
};

// ---------------------------------------------------------------------------
// status helpers
// ---------------------------------------------------------------------------

function setStatus(text, klass) {
  els.status.textContent = text;
  els.status.className = "status" + (klass ? " " + klass : "");
}

function setBackendLabel(kind) {
  // ``kind`` is "sdk" / "cli" / "unknown"; we render it verbatim so
  // the label stays a faithful echo of what the server picked.
  if (!els.backendLabel) return;
  const value = kind || "unknown";
  els.backendLabel.textContent = "backend: " + value;
  els.backendLabel.classList.toggle("backend-unknown", value === "unknown");
}

// ---------------------------------------------------------------------------
// session lifecycle
// ---------------------------------------------------------------------------

async function ensureSession() {
  setStatus("connecting…");
  const res = await fetch("/api/sessions", { method: "POST" });
  if (!res.ok) {
    setStatus("session failed", "err");
    throw new Error("could not create session");
  }
  const body = await res.json();
  state.sid = body.sid;
  setBackendLabel(body.backend_kind);
  setStatus(`ready · ${body.model}`, "ok");
  await refreshResources();
  startGraphPolling();
}

// Best-effort tear down on tab close.
window.addEventListener("beforeunload", () => {
  if (state.sid) {
    navigator.sendBeacon &&
      navigator.sendBeacon(`/api/sessions/${state.sid}`);
  }
});

// ---------------------------------------------------------------------------
// scrollback helpers
// ---------------------------------------------------------------------------

function appendUserMessage(text) {
  const msg = document.createElement("div");
  msg.className = "message user";
  msg.innerHTML = `<div class="role">you</div><div class="body"></div>`;
  msg.querySelector(".body").textContent = text;
  els.scrollback.appendChild(msg);
  scrollToBottom();
}

function startAssistantMessage() {
  const msg = document.createElement("div");
  msg.className = "message assistant";
  msg.innerHTML = `<div class="role">assistant</div><div class="body"></div>`;
  els.scrollback.appendChild(msg);
  state.currentAssistantMsg = msg;
  scrollToBottom();
}

function appendAssistantText(text) {
  if (state.currentAssistantMsg === null) startAssistantMessage();
  const body = state.currentAssistantMsg.querySelector(".body");
  body.appendChild(document.createTextNode(text));
  scrollToBottom();
}

function appendToolCard(toolUse) {
  if (state.currentAssistantMsg === null) startAssistantMessage();
  const card = document.createElement("details");
  card.className = "tool-card";
  card.open = false;
  const inputJson = JSON.stringify(toolUse.input || {}, null, 2);
  card.innerHTML = `
    <summary>
      <span class="tool-tag">tool</span>
      <span class="tool-name"></span>
      <span class="tool-status" style="color:var(--muted);font-size:11px;">running…</span>
    </summary>
    <pre class="tool-input"></pre>
    <pre class="tool-output" style="display:none"></pre>`;
  card.querySelector(".tool-name").textContent = toolUse.name || "(unnamed)";
  card.querySelector(".tool-input").textContent = inputJson;
  state.currentAssistantMsg.appendChild(card);
  if (toolUse.id) state.toolCardsById.set(toolUse.id, card);
  scrollToBottom();
}

function fillToolResult(result) {
  const card = result.id ? state.toolCardsById.get(result.id) : null;
  if (!card) return;
  const out = card.querySelector(".tool-output");
  out.textContent = result.content || "";
  out.style.display = "block";
  card.querySelector(".tool-status").textContent = result.is_error ? "error" : "done";
  if (result.is_error) card.classList.add("error");
}

function scrollToBottom() {
  els.scrollback.scrollTop = els.scrollback.scrollHeight;
}

// ---------------------------------------------------------------------------
// SSE chat stream
//
// We can't use EventSource because it's GET-only and we want POST with a
// JSON body. So we fetch() and parse the SSE wire format by hand. Each
// frame is separated by a blank line; a frame has zero or more
// `event: <type>` and `data: <json>` lines.
// ---------------------------------------------------------------------------

function parseSseFrame(raw) {
  let event = "message";
  const dataLines = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  let data;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch (_e) {
    data = { raw: dataLines.join("\n") };
  }
  return { event, data };
}

function setInFlight(flag) {
  state.inFlight = flag;
  els.composerSend.disabled = flag;
  // Stop button is only meaningful while the SSE stream is open.
  if (els.composerStop) {
    els.composerStop.hidden = !flag;
  }
}

function appendCancelMarker() {
  if (state.currentAssistantMsg === null) startAssistantMessage();
  const marker = document.createElement("div");
  marker.className = "cancelled-marker";
  marker.textContent = "Stopped by user.";
  state.currentAssistantMsg.appendChild(marker);
  scrollToBottom();
}

async function streamChat(message) {
  if (!state.sid) return;
  setInFlight(true);

  appendUserMessage(message);
  startAssistantMessage();

  let res;
  try {
    res = await fetch(`/api/sessions/${state.sid}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
  } catch (err) {
    appendAssistantText(`\n[network error: ${err}]`);
    setInFlight(false);
    return;
  }
  if (!res.ok || !res.body) {
    appendAssistantText(`\n[server error: HTTP ${res.status}]`);
    setInFlight(false);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (!raw.trim()) continue;
      const frame = parseSseFrame(raw);
      if (frame) handleEvent(frame);
    }
  }

  state.currentAssistantMsg = null;
  state.toolCardsById.clear();
  setInFlight(false);
  // A turn just finished (or was cancelled) — graph and code may have
  // moved. forceGraphRefresh refetches the graph; the code pane piggy-
  // backs on the same revision tick so it'll catch up on the next poll.
  forceGraphRefresh();
}

async function cancelCurrentTurn() {
  if (!state.sid || !state.inFlight) return;
  // Disable the button to prevent double-click; the SSE stream's
  // closure will flip it (and Send) back via setInFlight(false).
  if (els.composerStop) els.composerStop.disabled = true;
  try {
    await fetch(`/api/sessions/${state.sid}/cancel`, { method: "POST" });
  } catch (_e) {
    // Network failure → server already torn down or unreachable. The
    // SSE reader loop will eventually unblock; nothing to do here.
  } finally {
    if (els.composerStop) els.composerStop.disabled = false;
  }
}

function handleEvent({ event, data }) {
  switch (event) {
    case "text_chunk":
      appendAssistantText(data.text || "");
      break;
    case "tool_use":
      appendToolCard(data);
      break;
    case "tool_result":
      fillToolResult(data);
      break;
    case "turn_complete":
      // no-op; user can send the next message
      break;
    case "turn_cancelled":
      appendCancelMarker();
      break;
    case "error":
      appendAssistantText(`\n[${data.message || "error"}]`);
      break;
    case "end":
      // stream is over
      break;
    default:
      // forward-compat: drop unknown events into a comment-style line
      console.debug("unknown SSE event", event, data);
  }
}

// ---------------------------------------------------------------------------
// graph polling
// ---------------------------------------------------------------------------

function startGraphPolling() {
  if (state.pollTimer !== null) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(pollRevision, 1500);
}

async function pollRevision() {
  if (!state.sid) return;
  try {
    const r = await fetch(`/api/sessions/${state.sid}/graph/revision`);
    if (!r.ok) return;
    const body = await r.json();
    if (body.revision == null) return;
    if (
      body.revision !== state.lastRevision ||
      body.composition !== state.lastComposition
    ) {
      state.lastRevision = body.revision;
      state.lastComposition = body.composition;
      // Both the graph and the code pane are downstream of the same
      // revision counter — refresh them together so the two views
      // never disagree on what's currently in the composition.
      await Promise.all([fetchGraph(), fetchCode()]);
    }
  } catch (_e) {
    // network blip; the next tick will retry
  }
}

async function forceGraphRefresh() {
  state.lastRevision = -1; // force the next poll to refetch
  state.lastCodeRevision = -1;
  await pollRevision();
}

async function fetchGraph() {
  if (!state.sid) return;
  const fmt = els.graphFormat.value || "png";
  const url = `/api/sessions/${state.sid}/graph?fmt=${encodeURIComponent(fmt)}`;
  const r = await fetch(url);
  if (r.status === 204) {
    els.graphImg.classList.remove("loaded");
    els.graphImg.removeAttribute("src");
    els.graphMeta.textContent = "no composition yet";
    els.graphEmpty.style.display = "block";
    return;
  }
  if (!r.ok) {
    els.graphMeta.textContent = `render failed: HTTP ${r.status}`;
    return;
  }
  const body = await r.json();
  if (body.error) {
    els.graphMeta.textContent = `error: ${body.error}`;
    return;
  }
  if (body.data_url) {
    els.graphImg.src = body.data_url;
    els.graphImg.classList.add("loaded");
    els.graphEmpty.style.display = "none";
  }
  els.graphMeta.textContent =
    `${body.composition || "?"} · rev ${body.revision ?? "?"}` +
    (body.n_nodes != null ? ` · ${body.n_nodes} nodes` : "") +
    (body.n_projections != null ? ` · ${body.n_projections} projections` : "");
}

// ---------------------------------------------------------------------------
// code preview pane
//
// Driven by the same revision tick as the graph (no separate timer).
// Backend hits the MCP's ``export_python_script`` with ``dry_run=true``
// so polling doesn't spray ``.py`` files on disk every 1.5s.
// ---------------------------------------------------------------------------

async function fetchCode() {
  if (!state.sid) return;
  if (!els.codeText) return;
  let r;
  try {
    r = await fetch(`/api/sessions/${state.sid}/code`);
  } catch (_e) {
    return;
  }
  if (r.status === 204) {
    els.codeText.textContent = "";
    els.codePre.classList.remove("loaded");
    els.codeEmpty.style.display = "flex";
    els.codeMeta.textContent = "no composition yet";
    state.lastCodeComposition = null;
    state.lastCodeRevision = -1;
    return;
  }
  if (!r.ok) {
    els.codeMeta.textContent = `render failed: HTTP ${r.status}`;
    return;
  }
  const body = await r.json();
  if (body.error) {
    els.codeMeta.textContent = `error: ${body.error}`;
    return;
  }
  els.codeText.textContent = body.text || "";
  els.codePre.classList.add("loaded");
  els.codeEmpty.style.display = "none";
  state.lastCodeComposition = body.composition;
  state.lastCodeRevision = body.revision ?? state.lastCodeRevision;
  els.codeMeta.textContent =
    `${body.composition || "?"} · rev ${body.revision ?? "?"}` +
    (body.n_objects != null ? ` · ${body.n_objects} objects` : "") +
    (body.n_operations != null ? ` · ${body.n_operations} ops` : "");
}

function showRightTab(name) {
  state.activeRightTab = name;
  const isGraph = name === "graph";
  els.tabGraph.classList.toggle("active", isGraph);
  els.tabCode.classList.toggle("active", !isGraph);
  els.tabGraph.setAttribute("aria-selected", isGraph ? "true" : "false");
  els.tabCode.setAttribute("aria-selected", isGraph ? "false" : "true");
  els.panelGraph.hidden = !isGraph;
  els.panelCode.hidden = isGraph;
  // Switching to Code while a fresh revision is pending? Pull immediately
  // rather than waiting for the next 1.5s tick.
  if (!isGraph && state.lastCodeRevision !== state.lastRevision) {
    fetchCode();
  }
}

// ---------------------------------------------------------------------------
// resources
// ---------------------------------------------------------------------------

async function refreshResources() {
  if (!state.sid) return;
  const r = await fetch(`/api/sessions/${state.sid}/resources`);
  if (!r.ok) return;
  const list = await r.json();
  els.resources.innerHTML = "";
  if (list.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "no resources attached";
    els.resources.appendChild(li);
    return;
  }
  for (const r of list) {
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="kind"></span>
      <span class="label"></span>
      <button type="button" title="detach">×</button>`;
    li.querySelector(".kind").textContent = r.kind;
    li.querySelector(".label").textContent = r.label;
    li.querySelector("button").addEventListener("click", () => detachResource(r.index));
    els.resources.appendChild(li);
  }
}

async function uploadResource(kind, file) {
  if (!state.sid || !file) return;
  const fd = new FormData();
  fd.append("file", file);
  setStatus(`uploading ${file.name}…`);
  const r = await fetch(`/api/sessions/${state.sid}/resources/${kind}`, {
    method: "POST",
    body: fd,
  });
  if (!r.ok) {
    const detail = await r.text();
    setStatus(`upload failed: ${detail}`, "err");
    return;
  }
  setStatus("ready", "ok");
  await refreshResources();
}

async function detachResource(index) {
  if (!state.sid) return;
  await fetch(`/api/sessions/${state.sid}/resources/${index}`, {
    method: "DELETE",
  });
  await refreshResources();
}

// ---------------------------------------------------------------------------
// save model
// ---------------------------------------------------------------------------

async function saveModel() {
  if (!state.sid) return;
  const path = window.prompt("Save current model to .py path:", "model.py");
  if (!path) return;
  setStatus("saving model…");
  const r = await fetch(`/api/sessions/${state.sid}/model/save`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok || body.error) {
    setStatus(`save failed: ${body.error || r.status}`, "err");
    return;
  }
  setStatus(`saved · ${path}`, "ok");
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------

els.composer.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const message = els.composerInput.value.trim();
  if (!message || state.inFlight) return;
  els.composerInput.value = "";
  streamChat(message);
});

els.composerInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    els.composer.requestSubmit();
  }
});

els.uploadPdf.addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  ev.target.value = "";
  if (f) uploadResource("pdf", f);
});
els.uploadData.addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  ev.target.value = "";
  if (f) uploadResource("data", f);
});
els.uploadModel.addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  ev.target.value = "";
  if (f) uploadResource("model", f);
});

els.saveModelBtn.addEventListener("click", saveModel);
els.graphRefresh.addEventListener("click", forceGraphRefresh);
els.graphFormat.addEventListener("change", forceGraphRefresh);
if (els.composerStop) {
  els.composerStop.addEventListener("click", cancelCurrentTurn);
}
if (els.tabGraph) els.tabGraph.addEventListener("click", () => showRightTab("graph"));
if (els.tabCode) els.tabCode.addEventListener("click", () => showRightTab("code"));
if (els.codeRefresh) els.codeRefresh.addEventListener("click", () => fetchCode());

ensureSession().catch((err) => {
  console.error(err);
  setStatus(`startup failed: ${err}`, "err");
});
