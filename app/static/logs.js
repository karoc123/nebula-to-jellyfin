const pre = document.getElementById("log");
let stick = true;
pre.addEventListener("scroll", () => { stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40; });
async function tick() {
  try {
    const response = await fetch("/api/logs?lines=400", {cache: "no-store"});
    if (!response.ok) return;
    pre.textContent = await response.text();
    if (stick) pre.scrollTop = pre.scrollHeight;
  } catch { /* Retry at the next poll. */ }
}
tick(); setInterval(tick, 2000);
