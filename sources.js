// The sources page: a row of letters, each listing every site whose name
// starts with it (from /api/sources?letter=), and a find box that lists the
// sites whose name contains the text (/api/sources?q=, the biggest twelve).
// The letter is in the URL (?l=b) so a list can be linked to.
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDay = s => { const [y, m, d] = s.slice(0, 10).split("-").map(Number); return `${d} ${MONTHS[m - 1]} ${y}`; };
  const fmt = n => n.toLocaleString();
  const LETTERS = ["0", ..."abcdefghijklmnopqrstuvwxyz"];
  let seq = 0;

  $("letters").innerHTML = LETTERS.map(l => `<a href="?l=${l}" data-l="${l}">${l === "0" ? "0-9" : l.toUpperCase()}</a>`).join("");

  function table(rows) {
    if (!rows.length) { $("list").innerHTML = ""; return; }
    $("list").innerHTML = `<tr><th>Source</th><th class="r">Articles</th><th>From</th><th>To</th></tr>` + rows.map(([d, n, a, b]) =>
      `<tr><td class="d"><a href="./?domain=${encodeURIComponent(d)}">${esc(d)}</a></td><td class="r">${fmt(n)}</td><td>${fmtDay(a)}</td><td>${fmtDay(b)}</td></tr>`).join("");
  }
  async function get(query) {
    let r;
    try { r = await fetch("/api/sources?" + query).then(res => res.json()); } catch (e) { r = { error: "could not reach the list; try again" }; }
    return r;
  }

  async function letter(l, push) {
    if (!LETTERS.includes(l)) l = "a";
    const my = ++seq;
    if (push) history.pushState(null, "", "?l=" + l);
    document.title = `Sources ${l === "0" ? "0-9" : l.toUpperCase()} - News Headline Search`;
    for (const a of $("letters").children) a.classList.toggle("on", a.dataset.l === l);
    $("status").textContent = "loading…";
    const r = await get("letter=" + l);
    if (my !== seq) return;
    if (r.error) { $("status").textContent = r.error; table([]); return; }
    const n = r.sources.length;
    $("status").textContent = `${fmt(n)} source${n === 1 ? "" : "s"} beginning with ${l === "0" ? "a digit" : l.toUpperCase()}${n >= 10000 ? " (the first 10,000)" : ""}, in name order.`;
    table(r.sources);
  }

  // The find box replaces the list with the matches as you type; emptying
  // it brings the letter back.
  let timer;
  async function find() {
    const text = $("find").value.trim().toLowerCase().replace(/[^a-z0-9.-]/g, "");
    if (!text) { letter(current(), false); return; }
    const my = ++seq;
    for (const a of $("letters").children) a.classList.remove("on");
    const r = await get("q=" + encodeURIComponent(text));
    if (my !== seq) return;
    if (r.error) { $("status").textContent = r.error; table([]); return; }
    const n = r.sources.length;
    $("status").textContent = n ? `${n === 12 ? "The 12 biggest sources" : `${n} source${n === 1 ? "" : "s"}`} with "${text}" in the name, those starting with it first.` : `No source has "${text}" in its name.`;
    table(r.sources);
  }
  $("find").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(find, 150); });
  $("find").addEventListener("keydown", ev => { if (ev.key === "Enter") { ev.preventDefault(); clearTimeout(timer); find(); } });

  const current = () => (new URLSearchParams(location.search).get("l") || "a").toLowerCase();
  $("letters").addEventListener("click", ev => {
    const a = ev.target.closest("a[data-l]");
    if (!a) return;
    ev.preventDefault();
    $("find").value = "";
    letter(a.dataset.l, true);
  });
  window.onpopstate = () => { $("find").value = ""; letter(current(), false); };

  fetch("/api/stats").then(res => res.json()).then(s => { if (s.sources) $("n").textContent = fmt(s.sources); }).catch(() => {});
  letter(current(), false);
})();
