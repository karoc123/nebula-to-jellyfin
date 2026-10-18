function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
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
function button(text, url) {
  const node = el("button", text);
  node.type = "button";
  node.addEventListener("click", async () => {
    node.disabled = true;
    try { await action(url); await refresh(); }
    catch (error) { feedback(error.message); }
    finally { node.disabled = false; }
  });
  return node;
}
function errorDetails(message) {
  const node = el("details");
  node.append(el("summary", "Error details"), el("p", message));
  return node;
}
let offset = 0;
async function refresh() {
  if (!document.getElementById("videos") && !document.getElementById("updates")) return;
  try {
    const response = await fetch(`/api/status?offset=${offset}`, {cache: "no-store"});
    if (!response.ok) throw new Error("Could not load status");
    const data = await response.json();
    const channels = document.getElementById("channels");
    if (channels) {
      channels.replaceChildren(...data.channels.map(channel => {
        const node = el("div", undefined, "channel");
        const link = el("a", channel.name); link.href = `/channels/${encodeURIComponent(channel.id)}`;
        node.append(link, el("span", `${channel.done}/${channel.total} completed`, "hint"),
          el("span", channel.subscribed ? "Subscribed" : "Not subscribed", "hint"),
          el("span", `Last checked: ${channel.last_checked_at || "Never"}`, "hint"));
        if (channel.last_error) node.append(errorDetails(channel.last_error));
        return node;
      }));
      const nextCheck = new Date(data.next_check).toLocaleString("en-GB", {timeZone: data.schedule.timezone});
      document.getElementById("schedule").textContent = `Next check: ${nextCheck} (${data.schedule.timezone})`;
      if (!data.channels.length) channels.append(el("p", "No channels imported yet.", "hint"));
      const videos = document.getElementById("videos");
      // Preserve open error details between status polls.
      const open = new Set([...videos.querySelectorAll("tr")].filter(row => row.querySelector("details[open]")).map(row => row.dataset.id));
      videos.replaceChildren(...data.videos.map(video => {
        const row = el("tr"); row.dataset.id = video.id;
        const status = el("td"); status.append(el("span", `${video.status} ${video.progress}%`, `badge ${video.status}`));
        if (video.error) { const detail = errorDetails(video.error); detail.open = open.has(video.id); status.append(detail); }
        const title = el("td"); title.append(el("div", video.title), el("small", video.published_at || "Metadata pending"));
        const actions = el("td");
        if (video.status === "error") actions.append(button("Retry", `/api/videos/${encodeURIComponent(video.id)}/retry`));
        row.append(status, el("td", video.channel_name), title, actions); return row;
      }));
      if (!data.videos.length) { const row = el("tr"); const cell = el("td", "No downloads on this page.", "hint"); cell.colSpan = 4; row.append(cell); videos.append(row); }
      document.getElementById("page").textContent = data.videos.length ? `Downloads ${offset + 1}–${offset + data.videos.length}` : "No downloads on this page";
      document.getElementById("previous").disabled = offset === 0;
      document.getElementById("next").disabled = data.videos.length < 100;
      const jobs = document.getElementById("jobs");
      const openJobs = new Set([...jobs.children].filter(node => node.querySelector("details[open]")).map(node => node.dataset.id));
      jobs.replaceChildren(...data.jobs.map(job => {
        const node = el("div", undefined, "job"); node.dataset.id = String(job.id);
        const payload = JSON.parse(job.payload);
        node.append(el("p", `#${job.id} ${job.kind}: ${payload.url || "Saved cookies"} — ${job.status}`));
        if (job.result) node.append(el("p", job.result, "hint"));
        if (job.error) {
          const details = errorDetails(job.error); details.open = openJobs.has(String(job.id));
          node.append(details, button("Retry import", `/api/jobs/${job.id}/retry`));
        }
        return node;
      }));
    }
    if (document.getElementById("jobs") && !data.jobs.length) document.getElementById("jobs").append(el("p", "No background work yet.", "hint"));
    const updates = document.getElementById("updates");
    if (updates) updates.replaceChildren(...data.updates.map(item => {
      const node = el("p", `${item.component}: installed ${item.installed}; latest ${item.latest || "unknown"}; ${item.status}. Last successful check: ${item.succeeded_at || "Never"}.`);
      if (item.error) node.append(errorDetails(item.error)); return node;
    }));
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
          result.textContent = state.error || state.result; cookieButton.disabled = false;
        } else setTimeout(poll, 1500);
      } catch (error) { result.textContent = error.message; cookieButton.disabled = false; }
    }; setTimeout(poll, 1000);
  } catch (error) { result.textContent = error.message; cookieButton.disabled = false; }
});
if (document.getElementById("previous")) {
  document.getElementById("previous").onclick = () => { offset = Math.max(0, offset - 100); refresh(); };
  document.getElementById("next").onclick = () => { offset += 100; refresh(); };
}
refresh();
setInterval(refresh, 2500);
