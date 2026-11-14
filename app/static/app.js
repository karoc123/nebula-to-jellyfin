function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
// Absolute local time, e.g. 2026-10-19 07:57.
const timeFormat = new Intl.DateTimeFormat("sv-SE", {dateStyle: "short", timeStyle: "short"});
const dateFormat = new Intl.DateTimeFormat("sv-SE", {dateStyle: "short"});
function formatTime(value, format = timeFormat) {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime()) ? format.format(date) : "";
}
function timeNode(value, format = timeFormat) {
  const node = el("time", formatTime(value, format));
  node.dateTime = value || "";
  node.title = value || "";
  return node;
}
function formatTimes(root = document) {
  root.querySelectorAll("time[data-time], time[data-date]").forEach(node => {
    let format = node.hasAttribute("data-date") ? dateFormat : timeFormat;
    // Schedule times are shown in the configured timezone, not the browser's.
    if (node.dataset.tz) {
      try { format = new Intl.DateTimeFormat("sv-SE", {dateStyle: "short", timeStyle: "short", timeZone: node.dataset.tz}); }
      catch (error) { /* Keep the browser timezone for an unknown zone. */ }
    }
    const text = formatTime(node.dateTime, format);
    if (text) { node.textContent = text; node.title = node.dateTime; }
  });
}
function resultText(value) {
  if (!value) return "";
  try {
    const data = JSON.parse(value);
    if (typeof data === "string") return data;
    if (data && typeof data === "object") {
      return Object.entries(data)
        .filter(([, item]) => item !== null && item !== undefined)
        .map(([key, item]) => `${key}: ${item}`).join("; ");
    }
  } catch (error) { return value; }
  return String(value);
}
function feedback(message) { document.getElementById("feedback").textContent = message; }
function formFeedback(node, message, state = "neutral") {
  node.dataset.state = state;
  node.textContent = message;
}
async function action(url, body) {
  const response = await fetch(url, {method: "POST", body});
  if (!response.ok) {
    const data = await response.json();
    throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  }
  return response;
}
function button(text, url, className = "") {
  const node = el("button", text, className);
  node.type = "button";
  node.addEventListener("click", async () => {
    node.disabled = true;
    try { await action(url); await refresh(); }
    catch (error) { feedback(error.message); }
    finally { node.disabled = false; }
  });
  return node;
}
// The first line of an error is visible; the full text opens on demand.
function errorDetails(message) {
  const node = el("details", undefined, "err");
  node.append(el("summary", message.split("\n").find(line => line.trim()) || "Error"), el("pre", message));
  return node;
}
const STATES = {
  queued: ["wait", "Queued"], downloading: ["run", "Downloading"], publishing: ["run", "Publishing"],
  running: ["run", "Running"], done: ["ok", "Done"], error: ["err", "Failed"],
};
// Status is shown as a coloured dot and a word.
function stateNode(status, text) {
  const [tone, label] = STATES[status] || ["wait", status];
  const node = el("span", undefined, `state ${tone}`);
  node.append(el("i"), text ?? label);
  return node;
}
function duration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  const s = Math.round(seconds), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`;
}
function thumbNode(video) {
  const node = el("span", undefined, "thumb");
  if (video.thumb_url) {
    const img = el("img"); img.src = video.thumb_url; img.alt = ""; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
    img.addEventListener("error", () => img.remove());
    node.append(img);
  }
  const length = duration(video.duration);
  if (length) node.append(el("span", length));
  return node;
}
// Keep error details open across polls; rows are rebuilt on every refresh.
function openDetails(container) {
  return new Set([...container.querySelectorAll("[data-id]")]
    .filter(node => node.querySelector("details[open]")).map(node => node.dataset.id));
}
function replaceRows(container, rows) {
  const focused = document.activeElement;
  const row = container.contains(focused) ? focused.closest("[data-id], [data-empty]") : null;
  const label = focused?.textContent;
  container.replaceChildren(...rows);
  if (!row) return;
  const replacement = [...container.children].find(node => node.dataset.id === row.dataset.id && node.dataset.empty === row.dataset.empty);
  const control = replacement && [...replacement.querySelectorAll("button, summary, a")]
    .find(node => node.textContent === label);
  if (control) control.focus({preventScroll: true});
  else { container.tabIndex = -1; container.focus({preventScroll: true}); }
}
function showPause(paused) {
  const banner = document.getElementById("pause-banner");
  banner.hidden = !paused;
  const pauseButton = document.getElementById("pause-button");
  if (pauseButton) pauseButton.hidden = Boolean(paused);
  if (!paused) return;
  document.getElementById("pause-reason").textContent = paused.reason;
  const time = document.getElementById("pause-time");
  time.dateTime = paused.at; time.textContent = formatTime(paused.at); time.title = paused.at;
}

const PAGE = 100;
const JOB_PAGE = 50;
const params = new URLSearchParams(window.location.search);
const filters = {status: params.get("status") || "", channel: params.get("channel") || "", q: params.get("q") || ""};
let offset = 0;
let jobOffset = 0;
let counts = {};
let generation = 0;
let refreshing = false;
let refreshPending = false;

function filterQuery() {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value);
  return query;
}
const REMOVE_ICON = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M3 3l8 8M11 3l-8 8"/></svg>';
function videoRow(video, open) {
  const row = el("li"); row.dataset.id = video.id;
  const what = el("div", undefined, "what");
  const meta = el("p", `${video.channel_name} · `, "meta");
  meta.append(video.published_at ? timeNode(video.published_at, dateFormat) : "publication date pending");
  what.append(el("p", video.title, "title"), meta);
  if (video.error) { const detail = errorDetails(video.error); detail.open = open.has(video.id); what.append(detail); }
  const foot = el("div", undefined, "foot");
  const progress = video.status === "downloading" && Number.isFinite(video.progress);
  const state = stateNode(video.status, progress ? "Downloading " : undefined);
  if (progress) state.append(el("b", `${video.progress}%`));
  const updated = timeNode(video.updated_at); updated.className = "when";
  foot.append(state, updated);
  const actions = el("div", undefined, "row-acts");
  const id = encodeURIComponent(video.id);
  if (video.status === "error") actions.append(button("Retry", `/api/videos/${id}/retry`));
  if (video.status === "error" || video.status === "done") {
    const remove = button("", `/api/videos/${id}/remove`, "quiet sq");
    remove.innerHTML = REMOVE_ICON;
    remove.setAttribute("aria-label", "Remove from list");
    remove.title = video.status === "done" ? "Remove from list. Library files are kept." : "Remove from list. The video returns to the channel catalog.";
    actions.append(remove);
  }
  row.append(thumbNode(video), what, foot, actions);
  return row;
}
function currentWork(video) {
  const item = el("div", undefined, "now-item");
  const body = el("div");
  const eyebrow = el("p", undefined, "eyebrow");
  eyebrow.append(stateNode(video.status, ""), video.status === "publishing" ? "Publishing now" : "Downloading now");
  const meta = el("p", `${video.channel_name}`, "meta");
  if (video.published_at) meta.append(" · ", timeNode(video.published_at, dateFormat));
  body.append(eyebrow, el("h2", video.title), meta);
  if (video.status === "downloading" && Number.isFinite(video.progress)) {
    const bar = el("progress"); bar.max = 100; bar.value = video.progress;
    bar.setAttribute("aria-label", `Download progress for ${video.title}`);
    const foot = el("p", undefined, "now-foot");
    foot.append(el("b", `${video.progress}%`), el("span", queuedText()));
    body.append(bar, foot);
  }
  item.append(thumbNode(video), body);
  return item;
}
function queuedText() {
  const queued = counts.queued || 0;
  return queued ? `${queued} queued behind this` : "Nothing queued behind this";
}
async function refreshDownloads(version) {
  const query = filterQuery(); query.set("offset", offset);
  const [response, currentResponse] = await Promise.all([
    fetch(`/api/downloads?${query}`, {cache: "no-store"}),
    fetch("/api/downloads?status=active&limit=1", {cache: "no-store"}),
  ]);
  if (!response.ok) throw new Error("Could not load downloads");
  if (!currentResponse.ok) throw new Error("Could not load current work");
  const [data, current] = await Promise.all([response.json(), currentResponse.json()]);
  if (version !== generation) return;
  showPause(data.paused);
  counts = data.counts;
  const activeWork = current.videos.filter(video => video.status === "downloading" || video.status === "publishing");
  document.getElementById("active-download").replaceChildren(...(activeWork.length
    ? activeWork.map(currentWork) : [el("p", "Nothing is downloading right now.", "hint")]));
  const active = (counts.queued || 0) + (counts.downloading || 0) + (counts.publishing || 0);
  const total = Object.values(counts).reduce((sum, n) => sum + n, 0);
  const shown = {"": total, active, done: counts.done || 0, error: counts.error || 0, queued: counts.queued || 0};
  document.querySelectorAll("[data-count]").forEach(node => { node.textContent = shown[node.dataset.count]; });
  const summary = [[counts.downloading, "downloading"], [counts.publishing, "publishing"], [counts.queued, "queued"], [counts.error, "failed"]]
    .filter(([n]) => n).map(([n, label]) => `${n} ${label}`);
  document.getElementById("summary").textContent = summary.length ? summary.join(", ") : (total ? "Nothing in progress" : "");
  document.querySelectorAll("[data-status]").forEach(node => node.setAttribute("aria-pressed", String(node.dataset.status === filters.status)));
  document.querySelectorAll("[data-bulk]").forEach(node => {
    const key = node.dataset.bulk === "remove-done" ? "done" : (node.dataset.bulk === "remove-queued" ? "queued" : "error");
    node.querySelector("span").textContent = shown[key];
    node.disabled = !shown[key];
  });
  const videos = document.getElementById("videos");
  const open = openDetails(videos);
  const rows = data.videos.map(video => videoRow(video, open));
  if (!data.videos.length) {
    const row = el("li", undefined, "empty"); const cell = row;
    row.dataset.empty = "downloads";
    if (filters.status || filters.channel || filters.q) {
      cell.append("No downloads match these filters. ");
      const reset = el("button", "Reset filters"); reset.type = "button";
      reset.addEventListener("click", () => { filters.status = ""; filters.channel = ""; filters.q = ""; document.getElementById("search").value = ""; document.getElementById("channel-filter").value = ""; applyFilters(); });
      cell.append(reset);
    } else { cell.append("The download queue is empty. "); const link = el("a", "Add downloads"); link.href = "#urls"; link.addEventListener("click", () => openImport("video")); cell.append(link); }
    rows.push(row);
  }
  replaceRows(videos, rows);
  document.getElementById("page").textContent = data.total ? `${offset + 1}–${offset + data.videos.length} of ${data.total}` : "";
  document.getElementById("previous").disabled = offset === 0;
  document.getElementById("next").disabled = offset + data.videos.length >= data.total;
}

const JOB_KINDS = {video: "Video import", channel: "Channel import", check: "Channel check", cookie_test: "Cookie test"};
async function refreshJobs() {
  const response = await fetch(`/api/jobs?offset=${jobOffset}`, {cache: "no-store"});
  if (!response.ok) throw new Error("Could not load activity");
  const data = await response.json();
  const jobs = document.getElementById("jobs");
  const open = openDetails(jobs);
  replaceRows(jobs, data.jobs.map(job => {
    const row = el("li"); row.dataset.id = String(job.id);
    const payload = JSON.parse(job.payload);
    const task = el("div", undefined, "what");
    task.append(el("p", `${JOB_KINDS[job.kind] || job.kind} #${job.id}`, "title"), el("p", payload.url || "Saved cookies", "meta mono"));
    if (job.result) task.append(el("p", resultText(job.result), "meta"));
    if (job.error) { const detail = errorDetails(job.error); detail.open = open.has(String(job.id)); task.append(detail); }
    const updated = timeNode(job.updated_at); updated.className = "when";
    const actions = el("div", undefined, "row-acts");
    if (job.status === "error") actions.append(button("Retry", `/api/jobs/${job.id}/retry`));
    row.append(stateNode(job.status), task, updated, actions);
    return row;
  }));
  if (!data.jobs.length) {
    jobs.append(el("li", "No activity on this page.", "empty"));
  }
  document.getElementById("jobs-page").textContent = data.jobs.length ? `${jobOffset + 1}–${jobOffset + data.jobs.length}` : "";
  document.getElementById("jobs-previous").disabled = jobOffset === 0;
  document.getElementById("jobs-next").disabled = data.jobs.length < JOB_PAGE;
}

async function refreshUpdates() {
  const response = await fetch("/api/status", {cache: "no-store"});
  if (!response.ok) throw new Error("Could not load status");
  const data = await response.json();
  showPause(data.paused);
  const updates = document.getElementById("updates");
  if (updates.contains(document.activeElement)) return;
  const open = new Set([...updates.querySelectorAll("[data-component]")].filter(node => node.querySelector("details[open]")).map(node => node.dataset.component));
  const UPDATE = {current: ["ok", "Current"], available: ["warn", "Update available"], unknown: ["wait", "Unknown"]};
  const table = el("table"), head = el("tr"), body = el("tbody");
  for (const label of ["Component", "Installed", "Latest", "Status", "Last check"]) head.append(el("th", label));
  table.append(el("thead"), body); table.tHead.append(head);
  for (const item of data.updates) {
    const row = el("tr"); row.dataset.component = item.component;
    const [tone, label] = UPDATE[item.status] || ["wait", item.status];
    const state = el("td"); const dot = el("span", undefined, `state ${tone}`); dot.append(el("i"), label); state.append(dot);
    if (item.error) { const detail = errorDetails(item.error); detail.open = open.has(item.component); state.append(detail); }
    const checked = el("td", undefined, "mono"); checked.append(item.succeeded_at ? timeNode(item.succeeded_at) : "never");
    row.append(el("td", item.component), el("td", item.installed, "mono"), el("td", item.latest || "unknown", "mono"), state, checked);
    body.append(row);
  }
  updates.replaceChildren(table);
}

async function refresh() {
  if (refreshing) { refreshPending = true; return; }
  refreshing = true;
  const version = generation;
  try {
    if (document.getElementById("videos")) await refreshDownloads(version);
    if (document.getElementById("jobs")) await refreshJobs();
    if (document.getElementById("updates")) await refreshUpdates();
  } catch (error) { feedback(error.message); }
  finally {
    refreshing = false;
    if (refreshPending) { refreshPending = false; refresh(); }
  }
}

document.querySelectorAll("[data-form]").forEach(form => form.addEventListener("submit", async event => {
  event.preventDefault();
  const submit = form.querySelector('button:not([type="button"])'); submit.disabled = true;
  const target = form.querySelector(".form-feedback");
  if (target) formFeedback(target, "");
  try { const response = await action(form.action, new FormData(form)); window.location.assign(response.url); }
  catch (error) { if (target) formFeedback(target, error.message, "error"); else feedback(error.message); submit.disabled = false; }
}));
document.querySelectorAll("[data-action]").forEach(node => node.addEventListener("click", async () => {
  const data = new FormData(); if (node.dataset.enabled) data.set("enabled", node.dataset.enabled);
  node.disabled = true;
  try { await action(node.dataset.action, data); feedback("Work queued."); window.location.reload(); }
  catch (error) { feedback(error.message); node.disabled = false; }
}));
document.querySelectorAll("[data-queue]").forEach(node => node.addEventListener("click", async () => {
  node.disabled = true;
  try { await action(`/api/queue/${node.dataset.queue}`); window.location.reload(); }
  catch (error) { feedback(error.message); node.disabled = false; }
}));
const BULK = {
  "retry-errors": n => `Retry ${n} failed downloads?`,
  "remove-errors": n => `Remove ${n} failed downloads from this list? They return to the channel catalog and can be selected again.`,
  "remove-done": n => `Remove ${n} finished downloads from this list? Files in the library are kept and the videos stay marked as downloaded.`,
  "remove-queued": n => `Remove ${n} queued downloads from this list? They return to the channel catalog and can be selected again.`,
};
document.querySelectorAll("[data-bulk]").forEach(node => node.addEventListener("click", async () => {
  const n = node.dataset.bulk === "remove-done" ? counts.done : (node.dataset.bulk === "remove-queued" ? counts.queued : counts.error);
  if (!window.confirm(BULK[node.dataset.bulk](n || 0))) return;
  const body = new FormData(); body.set("channel", filters.channel); body.set("q", filters.q);
  node.disabled = true;
  try {
    const response = await action(`/api/downloads/${node.dataset.bulk}`, body);
    formFeedback(document.getElementById("bulk-feedback"), `${(await response.json()).count} downloads updated.`, "success");
    offset = 0; await refresh();
  } catch (error) { formFeedback(document.getElementById("bulk-feedback"), error.message, "error"); node.disabled = false; }
}));
document.querySelectorAll("[data-clear]").forEach(node => node.addEventListener("click", async () => {
  const label = node.dataset.clear === "done" ? "finished" : "failed";
  if (!window.confirm(`Delete all ${label} entries from Activity?`)) return;
  const body = new FormData(); body.set("status", node.dataset.clear);
  node.disabled = true;
  try { await action("/api/jobs/clear", body); jobOffset = 0; await refresh(); }
  catch (error) { feedback(error.message); }
  finally { node.disabled = false; }
}));
const cookieButton = document.getElementById("test-cookie");
if (cookieButton) cookieButton.addEventListener("click", async () => {
  const result = document.getElementById("cookie-result"); cookieButton.disabled = true;
  formFeedback(result, "Starting cookie test…");
  try {
    const response = await action("/api/settings/test-cookie"); const job = await response.json();
    formFeedback(result, `Cookie test queued (#${job.job_id}).`);
    const poll = async () => {
      try {
        const response = await fetch(`/api/jobs/${job.job_id}`); const state = await response.json();
        if (state.status === "done" || state.status === "error") {
          formFeedback(result, state.error || resultText(state.result), state.status === "error" ? "error" : "success"); cookieButton.disabled = false;
        } else setTimeout(poll, 1500);
      } catch (error) { formFeedback(result, error.message, "error"); cookieButton.disabled = false; }
    }; setTimeout(poll, 1000);
  } catch (error) { formFeedback(result, error.message, "error"); cookieButton.disabled = false; }
});
const selectAll = document.getElementById("select-all");
if (selectAll) {
  const boxes = [...document.querySelectorAll('input[name="videos"]:not(:disabled)')];
  const updateSelection = () => {
    const count = boxes.filter(box => box.checked).length;
    selectAll.checked = boxes.length > 0 && count === boxes.length;
    selectAll.indeterminate = count > 0 && count < boxes.length;
    selectAll.disabled = boxes.length === 0;
    document.getElementById("selection-summary").textContent = `${count} selected`;
    document.getElementById("download-selected").disabled = count === 0;
  };
  selectAll.addEventListener("change", () => { boxes.forEach(box => { box.checked = selectAll.checked; }); updateSelection(); });
  boxes.forEach(box => box.addEventListener("change", updateSelection));
  updateSelection();
}
const kind = document.getElementById("import-kind");
function openImport(value) {
  const panel = document.getElementById("import");
  if (!panel) return;
  kind.querySelector(`[value="${value}"]`).checked = true;
  kind.dispatchEvent(new Event("change"));
  panel.hidden = false;
  document.getElementById("urls").focus();
}
if (kind) {
  const mode = document.getElementById("import-mode");
  const updateImport = () => {
    const channel = kind.querySelector(":checked").value === "channel";
    if (channel && mode.value === "video") mode.value = "future";
    if (!channel) mode.value = "video";
    mode.querySelector('[value="video"]').hidden = channel;
    mode.hidden = !channel;
    document.getElementById("import-hint").hidden = !channel;
    document.getElementById("import-subscription").hidden = !channel;
    document.getElementById("import-subscription").querySelector("input").disabled = !channel;
    const latest = document.getElementById("import-latest");
    latest.hidden = !channel || mode.value !== "latest";
    latest.querySelector("input").disabled = latest.hidden;
  };
  kind.addEventListener("change", updateImport); mode.addEventListener("change", updateImport); updateImport();
  document.querySelectorAll("[data-import-open]").forEach(node => node.addEventListener("click", () => openImport(node.dataset.importOpen)));
  document.querySelector("[data-import-close]").addEventListener("click", () => { document.getElementById("import").hidden = true; });
}
// Close an open menu when clicking elsewhere.
if (document.querySelectorAll("details.menu").length) document.addEventListener("click", event => {
  document.querySelectorAll("details.menu[open]").forEach(menu => { if (!menu.contains(event.target)) menu.open = false; });
});
const channelSearch = document.getElementById("channel-search");
if (channelSearch) {
  const tabs = [...document.querySelectorAll("#channel-subscription [data-value]")], errors = document.getElementById("channel-errors");
  let subscription = "";
  const cards = [...document.querySelectorAll(".channel-card")];
  const filterChannels = () => {
    tabs.forEach(tab => tab.setAttribute("aria-pressed", String(tab.dataset.value === subscription)));
    cards.forEach(card => { card.hidden = !card.dataset.name.toLocaleLowerCase().includes(channelSearch.value.trim().toLocaleLowerCase()) || (subscription && card.dataset.subscribed !== subscription) || (errors.value && card.dataset.errors !== errors.value); });
    document.getElementById("channel-empty").hidden = !cards.length || cards.some(card => !card.hidden);
  };
  tabs.forEach(tab => tab.addEventListener("click", () => { subscription = tab.dataset.value; filterChannels(); }));
  [channelSearch, errors].forEach(node => node.addEventListener("input", filterChannels));
  document.getElementById("channel-reset").addEventListener("click", () => { channelSearch.value = errors.value = subscription = ""; filterChannels(); });
}
let applyFilters;
if (document.getElementById("videos")) {
  // Filters live in the URL so a reload or shared link keeps them.
  applyFilters = () => {
    generation++;
    document.querySelectorAll("[data-bulk]").forEach(node => { node.disabled = true; });
    offset = 0;
    const query = filterQuery().toString();
    window.history.replaceState(null, "", query ? `?${query}` : window.location.pathname);
    refresh();
  };
  document.querySelectorAll("[data-status]").forEach(node => node.addEventListener("click", () => { filters.status = node.dataset.status; applyFilters(); }));
  const channel = document.getElementById("channel-filter");
  channel.value = filters.channel;
  channel.addEventListener("change", () => { filters.channel = channel.value; applyFilters(); });
  const search = document.getElementById("search");
  search.value = filters.q;
  let typing;
  search.addEventListener("input", () => { generation++; clearTimeout(typing); typing = setTimeout(() => { filters.q = search.value.trim(); applyFilters(); }, 300); });
  document.getElementById("previous").onclick = () => { offset = Math.max(0, offset - PAGE); generation++; refresh(); };
  document.getElementById("next").onclick = () => { offset += PAGE; generation++; refresh(); };
}
if (document.getElementById("jobs")) {
  document.getElementById("jobs-previous").onclick = () => { jobOffset = Math.max(0, jobOffset - JOB_PAGE); refresh(); };
  document.getElementById("jobs-next").onclick = () => { jobOffset += JOB_PAGE; refresh(); };
}
formatTimes();
if (document.getElementById("videos") || document.getElementById("jobs") || document.getElementById("updates")) {
  refresh();
  setInterval(refresh, 2500);
}
