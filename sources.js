// The sources page: a row of letters, each listing every site whose name
// starts with it (from /api/sources?letter=), and a find box that lists the
// sites whose name contains the text (/api/sources?q=, the biggest twelve).
// The letter is in the URL (?l=b) so a list can be linked to. The Include
// boxes choose the collections (?src=gdelt,blogs; none is all three here,
// unlike the search page, since this is the list of everything), and each
// site says which of them it is in. A letter longer than one answer (10,000
// sites) continues from its last name (?after=).
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDay = s => { const [y, m, d] = s.slice(0, 10).split("-").map(Number); return `${d} ${MONTHS[m - 1]} ${y}`; };
  const fmt = n => n.toLocaleString();
  const LETTERS = ["0", ..."abcdefghijklmnopqrstuvwxyz"];
  const PAGE = 10000;
  const SRCS = ["gdelt", "feeds", "blogs"];
  const KIND_NAMES = { gdelt: "GDELT", feeds: "news feed", substack: "Substack", medium: "Medium" };
  const parseSrcs = s => { const g = (s || "").toLowerCase().split(","); const out = SRCS.filter(x => g.includes(x)); return out.length ? out : [...SRCS]; };
  let srcs = parseSrcs(new URLSearchParams(location.search).get("src"));
  const srcParam = () => srcs.length === SRCS.length ? "" : srcs.join(",");
  const renderSrcs = () => document.querySelectorAll("input[name=src]").forEach(i => { i.checked = srcs.includes(i.value); });
  let seq = 0;

  $("letters").innerHTML = LETTERS.map(l => `<a href="?l=${l}" data-l="${l}">${l === "0" ? "0-9" : l.toUpperCase()}</a>`).join("");

  // A site's link opens the search on the collections it is in, of those
  // chosen (a blog on the blogs, reuters.com on GDELT and the feeds).
  const searchLink = (d, kinds) => {
    const p = new URLSearchParams({ domain: d });
    const in_ = SRCS.filter(x => kinds.some(k => (k === "substack" || k === "medium" ? "blogs" : k) === x));
    if (in_.length && !(in_.length === 1 && in_[0] === "gdelt")) p.set("src", in_.join(","));
    return "./?" + p;
  };
  function table(rows) {
    if (!rows.length) { $("list").innerHTML = ""; return; }
    // With GDELT alone the Worker gives no collections (every site is GDELT's).
    $("list").innerHTML = `<tr><th>Source</th><th class="r">Articles</th><th>From</th><th>To</th><th>In</th></tr>` + rows.map(([d, n, a, b, k]) => {
      const kinds = (k || "gdelt").split(",");
      return `<tr><td class="d"><a href="${esc(searchLink(d, kinds))}">${esc(d)}</a></td><td class="r">${fmt(n)}</td><td>${fmtDay(a)}</td><td>${fmtDay(b)}</td><td>${kinds.map(x => KIND_NAMES[x] || x).join(", ")}</td></tr>`;
    }).join("");
  }
  async function get(query) {
    let r;
    const q = new URLSearchParams(query);
    q.set("src", srcs.join(","));
    try { r = await fetch("/api/sources?" + q).then(res => res.json()); } catch (e) { r = { error: "could not reach the list; try again" }; }
    return r;
  }
  // The page's own address: the letter, where in it, the collections.
  const pageUrl = (l, after) => { const p = new URLSearchParams({ l }); if (after) p.set("after", after); if (srcParam()) p.set("src", srcParam()); return "?" + p; };

  async function letter(l, push, after = "") {
    if (!LETTERS.includes(l)) l = "a";
    const my = ++seq;
    if (push) history.pushState(null, "", pageUrl(l, after));
    document.title = `Sources ${l === "0" ? "0-9" : l.toUpperCase()} - News Headline Search`;
    for (const a of $("letters").children) a.classList.toggle("on", a.dataset.l === l);
    $("status").textContent = "loading…";
    $("more").innerHTML = "";
    const r = await get(`letter=${l}${after ? `&after=${encodeURIComponent(after)}` : ""}`);
    if (my !== seq) return;
    if (r.error) { $("status").textContent = r.error; table([]); return; }
    const n = r.sources.length, what = l === "0" ? "a digit" : l.toUpperCase();
    $("status").textContent = `${fmt(n)} source${n === 1 ? "" : "s"} beginning with ${what}${after ? `, after ${after}` : n >= PAGE ? " (the first 10,000)" : ""}, in name order.`;
    table(r.sources);
    const links = [];
    if (after) links.push(`<a href="${esc(pageUrl(l))}" data-page="">&larr; back to the start of ${what}</a>`);
    if (n >= PAGE) links.push(`<a href="${esc(pageUrl(l, r.sources[n - 1][0]))}" data-page="${esc(r.sources[n - 1][0])}">the next ${fmt(PAGE)} &rarr;</a>`);
    $("more").innerHTML = links.join(" · ");
  }
  $("more").addEventListener("click", ev => {
    const a = ev.target.closest("a[data-page]");
    if (!a) return;
    ev.preventDefault();
    letter(current(), true, a.dataset.page);
    scrollTo(0, 0);
  });

  // The find box replaces the list with the matches as you type; emptying
  // it brings the letter back.
  let timer;
  async function find() {
    const text = $("find").value.trim().toLowerCase().replace(/[^a-z0-9.-]/g, "");
    if (!text) { letter(current(), false); return; }
    const my = ++seq;
    for (const a of $("letters").children) a.classList.remove("on");
    $("more").innerHTML = "";
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
  const currentAfter = () => new URLSearchParams(location.search).get("after") || "";
  $("letters").addEventListener("click", ev => {
    const a = ev.target.closest("a[data-l]");
    if (!a) return;
    ev.preventDefault();
    $("find").value = "";
    letter(a.dataset.l, true);
  });
  // The letters' links carry the collections chosen.
  const relink = () => { for (const a of $("letters").children) a.href = pageUrl(a.dataset.l); };
  $("srcs").addEventListener("change", ev => {
    const on = [...document.querySelectorAll("input[name=src]:checked")].map(i => i.value);
    if (!on.length) { ev.target.checked = true; return; } // at least one collection
    srcs = SRCS.filter(x => on.includes(x));
    relink();
    if ($("find").value.trim()) { history.replaceState(null, "", pageUrl(current(), currentAfter())); find(); }
    else letter(current(), true); // a different set of sites: back to the start of the letter
  });
  window.onpopstate = () => { $("find").value = ""; srcs = parseSrcs(new URLSearchParams(location.search).get("src")); renderSrcs(); relink(); letter(current(), false, currentAfter()); };

  fetch("/api/stats").then(res => res.json()).then(s => {
    if (s.sources) $("n").textContent = fmt(s.sources);
    const u = s.unified?.src;
    if (u) $("n-more").textContent = `${u.feeds ? `, ${fmt(u.feeds.sites)} whose news feeds this site has collected since ${fmtDay(u.feeds.first)}` : ""}${u.blogs ? `, and ${fmt(u.blogs.sites)} blogs on Substack and Medium since ${fmtDay(u.blogs.first)}` : ""}`;
  }).catch(() => {});
  renderSrcs();
  relink();
  letter(current(), false, currentAfter());
})();
