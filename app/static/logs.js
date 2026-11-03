const pre = document.getElementById("log");
const status = document.getElementById("log-status");
let stick = true;
pre.addEventListener("scroll", () => { stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40; });
async function tick() {
  try {
    const response = await fetch("/api/logs?lines=400", {cache: "no-store"});
    if (!response.ok) throw new Error("Could not load logs");
    pre.textContent = await response.text();
    if (stick) pre.scrollTop = pre.scrollHeight;
    status.textContent = "Logs refresh automatically.";
  } catch (error) { status.textContent = `Could not load logs. Retrying automatically.`; }
  finally { setTimeout(tick, 2000); }
}
tick();
