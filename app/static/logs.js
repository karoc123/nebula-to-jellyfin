const pre = document.getElementById("log");
const status = document.getElementById("log-status");
let stick = true;
function showStatus(text, state) {
  const dot = document.createElement("i");
  status.className = `state ${state}`;
  status.replaceChildren(dot, text);
}
pre.addEventListener("scroll", () => { stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40; });
async function tick() {
  try {
    const response = await fetch("/api/logs?lines=400", {cache: "no-store"});
    if (!response.ok) throw new Error("Could not load logs");
    pre.textContent = await response.text();
    if (stick) pre.scrollTop = pre.scrollHeight;
    showStatus("Live", "run");
  } catch (error) { showStatus("Could not load logs. Retrying automatically.", "err"); }
  finally { setTimeout(tick, 2000); }
}
tick();
