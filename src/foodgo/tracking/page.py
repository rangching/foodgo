"""追蹤頁：一頁 HTML，每秒查一次 GET /orders/{id}/tracking，畫出四格步驟條。不用任何前端框架。

顏色和影片一樣：點餐 #6fb3f2、付款 #5fd38d、廚房 #ef7b6b、外送 #c89cf2。
查到 404（投影還沒讀到）就顯示「更新中…」，一秒後再查；之前查到過的資料先留在畫面上。
"""

PAGE = """<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ORDER_ID}} 訂單追蹤｜好食快送 FoodGo</title>
<style>
  :root {
    --bg: #111418; --card: #1a1f26; --line: #2a313b; --text: #e8ecf1; --muted: #8a8f98;
    --ordering: #6fb3f2; --payment: #5fd38d; --kitchen: #ef7b6b; --delivery: #c89cf2;
    --error: #ff6b5e; --accent: #f2a541;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 18px/1.5 "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", system-ui, sans-serif; }
  main { max-width: 960px; margin: 0 auto; padding: 40px 24px 32px; }
  header { display: flex; justify-content: space-between; align-items: baseline; color: var(--muted); }
  header b { color: var(--text); font-size: 20px; letter-spacing: .02em; }
  h1 { margin: 18px 0 6px; font-size: 44px; display: flex; gap: 18px; align-items: center; flex-wrap: wrap; }
  .pill { font-size: 22px; padding: 4px 16px; border-radius: 999px; color: #111418; font-weight: 700; }
  .headline { font-size: 22px; color: var(--text); min-height: 33px; }
  .banner { margin-top: 14px; padding: 10px 16px; border-radius: 10px; background: #24303d; color: var(--ordering);
            display: none; align-items: center; gap: 10px; }
  .banner.show { display: flex; }
  .spinner { width: 16px; height: 16px; border-radius: 50%; border: 3px solid currentColor; border-right-color: transparent;
             animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .steps { display: grid; grid-template-columns: repeat(4, 1fr); margin: 36px 0 28px; position: relative; }
  .step { text-align: center; position: relative; padding: 0 8px; }
  .step::before { content: ""; position: absolute; top: 34px; left: -50%; width: 100%; height: 6px;
                  background: var(--line); z-index: 0; }
  .step:first-child::before { display: none; }
  .step.reached::before { background: var(--c); }
  .dot { width: 74px; height: 74px; margin: 0 auto; border-radius: 50%; position: relative; z-index: 1;
         display: flex; align-items: center; justify-content: center; font-size: 34px; font-weight: 800;
         background: var(--bg); border: 6px solid var(--line); color: var(--muted); }
  .done .dot { background: var(--c); border-color: var(--c); color: #111418; }
  .active .dot { border-color: var(--c); color: var(--c); animation: pulse 1.4s ease-in-out infinite; }
  .failed .dot { background: var(--error); border-color: var(--error); color: #111418; }
  .undone .dot { border-color: var(--c); color: var(--c); border-style: dashed; }
  .skipped .dot { border-style: dashed; }
  @keyframes pulse { 50% { box-shadow: 0 0 0 12px color-mix(in srgb, var(--c) 25%, transparent); } }
  .step h2 { margin: 14px 0 2px; font-size: 24px; color: var(--c); }
  .todo h2, .skipped h2 { color: var(--muted); }
  .step p { margin: 0; font-size: 17px; color: var(--text); min-height: 52px; }
  .failed p { color: var(--error); }
  .undone p, .skipped p { color: var(--muted); }
  .undone p { text-decoration: line-through; text-decoration-color: color-mix(in srgb, var(--muted) 60%, transparent); }
  .undone p::after { content: "（已倒回）"; text-decoration: none; display: inline-block; margin-left: 4px; }

  .card { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 20px 26px;
          display: grid; grid-template-columns: 110px 1fr; row-gap: 10px; }
  .card:empty { display: none; }
  .card dt { color: var(--muted); }
  .card dd { margin: 0; }
  footer { margin-top: 18px; color: var(--muted); font-size: 16px; display: flex; gap: 18px; flex-wrap: wrap; }
  footer code { color: var(--text); font-family: "SF Mono", Menlo, monospace; }
</style>
</head>
<body>
<main>
  <header><b>好食快送 FoodGo・訂單追蹤</b><span>讀模型 <code>order_tracking</code></span></header>
  <h1><span id="order">{{ORDER_ID}}</span><span class="pill" id="pill" style="background: var(--muted)">查詢中</span></h1>
  <div class="headline" id="headline"></div>
  <div class="banner" id="banner"><span class="spinner"></span><span id="banner-text">更新中…</span></div>
  <section class="steps" id="steps"></section>
  <dl class="card" id="card"></dl>
  <footer><span>資料時間 <code id="updated">—</code></span><span>as_of_position <code id="position">—</code></span>
          <span>每秒更新・上次查詢 <code id="polled">—</code></span></footer>
</main>
<script>
const ORDER = decodeURIComponent(location.pathname.split("/").pop());
const COLORS = { placed: "--ordering", paid: "--payment", cooking: "--kitchen", delivering: "--delivery",
                 cancelling: "--accent", cancelled: "--muted", needs_attention: "--error" };
const MARKS = { done: "✓", active: "…", failed: "✕", undone: "↺", todo: "", skipped: "—" };
const EMPTY = ["ordering", "payment", "kitchen", "delivery"].map((key, i) =>
  ({ key, title: ["點餐", "付款", "廚房", "外送"][i], state: i ? "todo" : "active", detail: i ? "" : "等資料…" }));
let seen = false, misses = 0;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clock = (iso) => new Date(iso).toLocaleTimeString("zh-TW", { hour12: false, timeZone: "Asia/Taipei" });

function drawSteps(steps) {
  let reached = true;
  $("steps").innerHTML = steps.map((s) => {
    const here = reached && s.state !== "todo" && s.state !== "skipped";  // 連線只畫到真的走到的那一格
    reached = here && s.state !== "active";
    return `<div class="step ${s.state} ${here ? "reached" : ""}" style="--c: var(--${s.key})">
      <div class="dot">${MARKS[s.state]}</div><h2>${esc(s.title)}</h2><p>${esc(s.detail)}</p></div>`;
  }).join("");
}

function draw(t) {
  $("pill").textContent = t.status_label;
  $("pill").style.background = `var(${COLORS[t.status] || "--muted"})`;
  $("headline").textContent = t.headline;
  drawSteps(t.steps);
  const items = t.items.length ? t.items.map((i) => `${esc(i.name)} ×${i.quantity}`).join("、") : "（品項還在路上）";
  $("card").innerHTML = `<dt>品項</dt><dd>${items}</dd>
    <dt>備註</dt><dd>${esc(t.note || "無")}</dd>
    <dt>合計</dt><dd>${t.amount == null ? "—" : t.amount + " 元"}</dd>
    <dt>送到</dt><dd>${esc(t.delivery_address || "—")}</dd>
    <dt>餐廳</dt><dd>${esc(t.restaurant_id || "—")}</dd>`;
  $("updated").textContent = clock(t.updated_at);
  $("position").textContent = t.as_of_position;
}

function waiting(body) {
  misses += 1;
  $("position").textContent = body.as_of_position;
  $("banner").classList.add("show");
  $("banner-text").textContent = seen
    ? "更新中…（讀模型暫時查不到這張單，例如正在重建；先顯示上次的資料）"
    : misses > 15 ? `查不到 ${ORDER}：訂單編號對嗎？（還是會繼續查）` : "更新中…剛下單的話，追蹤資料還在路上";
  if (!seen) { drawSteps(EMPTY); $("pill").textContent = "更新中"; $("headline").textContent = ""; }
}

async function poll() {
  try {
    const response = await fetch(`/orders/${encodeURIComponent(ORDER)}/tracking`, { cache: "no-store" });
    const body = await response.json();
    if (response.ok) { seen = true; misses = 0; $("banner").classList.remove("show"); draw(body); }
    else if (response.status === 404) { waiting(body); }
  } catch (error) {
    $("banner").classList.add("show");
    $("banner-text").textContent = "連不到伺服器，一秒後再試…";
  }
  $("polled").textContent = new Date().toLocaleTimeString("zh-TW", { hour12: false });
  setTimeout(poll, 1000);
}
drawSteps(EMPTY);
poll();
</script>
</body>
</html>
"""
