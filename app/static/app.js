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
async function action(url, body) {
  const response = await fetch(url, {method: "POST", body});
  if (!response.ok) {
    const data = await response.json();
    throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  }
  return response;
}
function button(text, url, className = "secondary") {
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
function errorDetails(message, summary = "Error details") {
  const node = el("details");
  node.append(el("summary", summary), el("p", message));
  return node;
}
function badge(status, text = status) { return el("span", text, `badge ${status}`); }
// Keep error details open across polls; rows are rebuilt on every refresh.
function openDetails(container) {
  return new Set([...container.querySelectorAll("[data-id]")]
    .filter(node => node.querySelector("details[open]")).map(node => node.dataset.id));
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

function filterQuery() {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value);
  return query;
}
function videoRow(video, open) {
  const row = el("tr"); row.dataset.id = video.id;
  const status = el("td");
  status.append(badge(video.status));
  if (video.status === "downloading") {
    const bar = el("progress"); bar.max = 100; bar.value = video.progress;
    bar.title = `${video.progress}%`; status.append(bar);
  }
  const title = el("td", undefined, "video");
  const meta = el("small", `${video.channel_name} · `);
  meta.append(video.published_at ? timeNode(video.published_at, dateFormat) : "publication date pending");
  title.append(el("div", video.title), meta);
  if (video.error) { const detail = errorDetails(video.error); detail.open = open.has(video.id); title.append(detail); }
  const updated = el("td", undefined, "nowrap"); updated.append(timeNode(video.updated_at));
  const actions = el("td", undefined, "row-actions");
  const id = encodeURIComponent(video.id);
  if (video.status === "error") actions.append(button("Retry", `/api/videos/${id}/retry`));
  if (video.status === "error" || video.status === "done") actions.append(button("Remove", `/api/videos/${id}/remove`));
  row.append(status, title, updated, actions);
  return row;
}
async function refreshDownloads() {
  const query = filterQuery(); query.set("offset", offset);
  const response = await fetch(`/api/downloads?${query}`, {cache: "no-store"});
  if (!response.ok) throw new Error("Could not load downloads");
  const data = await response.json();
  showPause(data.paused);
  counts = data.counts;
  const active = (counts.queued || 0) + (counts.downloading || 0) + (counts.publishing || 0);
  const total = Object.values(counts).reduce((sum, n) => sum + n, 0);
  const shown = {"": total, active, done: counts.done || 0, error: counts.error || 0};
  document.querySelectorAll("[data-count]").forEach(node => { node.textContent = shown[node.dataset.count]; });
  document.querySelectorAll("[data-status]").forEach(node => node.setAttribute("aria-pressed", String(node.dataset.status === filters.status)));
  document.querySelector('[data-bulk="retry-errors"]').textContent = `Retry failed (${shown.error})`;
  document.querySelector('[data-bulk="remove-errors"]').textContent = `Remove failed (${shown.error})`;
  document.querySelector('[data-bulk="remove-done"]').textContent = `Remove done (${shown.done})`;
  document.querySelectorAll("[data-bulk]").forEach(node => { node.disabled = !shown[node.dataset.bulk === "remove-done" ? "done" : "error"]; });
  const videos = document.getElementById("videos");
  const open = openDetails(videos);
  videos.replaceChildren(...data.videos.map(video => videoRow(video, open)));
  if (!data.videos.length) {
    const row = el("tr"); const cell = el("td", "No downloads match these filters.", "hint"); cell.colSpan = 4;
    row.append(cell); videos.append(row);
  }
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
  jobs.replaceChildren(...data.jobs.map(job => {
    const row = el("tr"); row.dataset.id = String(job.id);
    const payload = JSON.parse(job.payload);
    const status = el("td"); status.append(badge(job.status));
    const task = el("td");
    task.append(el("div", `${JOB_KINDS[job.kind] || job.kind} #${job.id}`), el("small", payload.url || "Saved cookies"));
    if (job.result) task.append(el("small", resultText(job.result)));
    if (job.error) { const detail = errorDetails(job.error); detail.open = open.has(String(job.id)); task.append(detail); }
    const updated = el("td", undefined, "nowrap"); updated.append(timeNode(job.updated_at));
    const actions = el("td", undefined, "row-actions");
    if (job.status === "error") actions.append(button("Retry", `/api/jobs/${job.id}/retry`));
    row.append(status, task, updated, actions);
    return row;
  }));
  if (!data.jobs.length) {
    const row = el("tr"); const cell = el("td", "No activity on this page.", "hint"); cell.colSpan = 4;
    row.append(cell); jobs.append(row);
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
  document.getElementById("updates").replaceChildren(...data.updates.map(item => {
    const node = el("p", `${item.component}: installed ${item.installed}; latest ${item.latest || "unknown"}; ${item.status}. Last successful check: `, `update ${item.status}`);
    node.append(item.succeeded_at ? timeNode(item.succeeded_at) : "never", ".");
    if (item.error) node.append(errorDetails(item.error)); return node;
  }));
}

async function refresh() {
  try {
    if (document.getElementById("videos")) await refreshDownloads();
    if (document.getElementById("jobs")) await refreshJobs();
    if (document.getElementById("updates")) await refreshUpdates();
  } catch (error) { feedback(error.message); }
}

document.querySelectorAll("[data-form]").forEach(form => form.addEventListener("submit", async event => {
  event.preventDefault();
  const submit = form.querySelector("button"); submit.disabled = true;
  try { const response = await action(form.action, new FormData(form)); window.location.assign(response.url); }
  catch (error) { feedback(error.message); submit.disabled = false; }
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
};
document.querySelectorAll("[data-bulk]").forEach(node => node.addEventListener("click", async () => {
  const n = node.dataset.bulk === "remove-done" ? counts.done : counts.error;
  if (!window.confirm(BULK[node.dataset.bulk](n))) return;
  const body = new FormData(); body.set("channel", filters.channel); body.set("q", filters.q);
  node.disabled = true;
  try {
    const response = await action(`/api/downloads/${node.dataset.bulk}`, body);
    feedback(`${(await response.json()).count} downloads updated.`);
    offset = 0; await refresh();
  } catch (error) { feedback(error.message); node.disabled = false; }
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
  try {
    const response = await action("/api/settings/test-cookie"); const job = await response.json();
    result.textContent = `Cookie test queued (#${job.job_id}).`;
    const poll = async () => {
      try {
        const response = await fetch(`/api/jobs/${job.job_id}`); const state = await response.json();
        if (state.status === "done" || state.status === "error") {
          result.textContent = state.error || resultText(state.result); cookieButton.disabled = false;
        } else setTimeout(poll, 1500);
      } catch (error) { result.textContent = error.message; cookieButton.disabled = false; }
    }; setTimeout(poll, 1000);
  } catch (error) { result.textContent = error.message; cookieButton.disabled = false; }
});
const selectAll = document.getElementById("select-all");
if (selectAll) selectAll.addEventListener("change", () => {
  document.querySelectorAll('input[name="videos"]:not(:disabled)').forEach(box => { box.checked = selectAll.checked; });
});
if (document.getElementById("videos")) {
  // Filters live in the URL so a reload or shared link keeps them.
  const applyFilters = () => {
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
  search.addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => { filters.q = search.value.trim(); applyFilters(); }, 300); });
  document.getElementById("previous").onclick = () => { offset = Math.max(0, offset - PAGE); refresh(); };
  document.getElementById("next").onclick = () => { offset += PAGE; refresh(); };
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
