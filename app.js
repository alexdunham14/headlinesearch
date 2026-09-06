(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const form = $("f");
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDay = s => { const [y, m, d] = s.slice(0, 10).split("-").map(Number); return `${d} ${MONTHS[m - 1]} ${y}`; };
  let page = 1;

  function params() {
    const p = new URLSearchParams();
    p.set("q", $("q").value.trim());
    p.set("mode", form.mode.value);
    if (form.sort.value === "oldest") p.set("sort", "oldest");
    if ($("from").value) p.set("from", $("from").value);
    if ($("to").value) p.set("to", $("to").value);
    if ($("domain").value.trim()) p.set("domain", $("domain").value.trim());
    return p;
  }

  function fromUrl() {
    const p = new URLSearchParams(location.search);
    if (!p.get("q")) return false;
    $("q").value = p.get("q");
    form.mode.value = p.get("mode") === "substring" ? "substring" : "word";
    form.sort.value = p.get("sort") === "oldest" ? "oldest" : "newest";
    $("from").value = p.get("from") || "";
    $("to").value = p.get("to") || "";
    $("domain").value = p.get("domain") || "";
    page = Math.max(1, parseInt(p.get("page") || "1", 10) || 1);
    return true;
  }

  async function search(push) {
    const p = params();
    if (p.get("q").length < 2) return;
    if (page > 1) p.set("page", page);
    if (push) history.pushState(null, "", "?" + p);
    document.title = `${p.get("q")} · Headline Search`;
    $("examples").hidden = true;
    $("status").textContent = "searching…";
    $("results").innerHTML = "";
    $("more").innerHTML = "";
    $("months").innerHTML = "";
    let r;
    try {
      r = await fetch("/api/search?" + p).then(res => res.json());
    } catch (e) {
      $("status").textContent = "search failed; try again";
      return;
    }
    if (r.error) { $("status").textContent = r.error; return; }
    render(r, p);
  }

  // Wrap each match in <mark>: whole words in word mode, the exact run in substring mode.
  function highlighter(p) {
    const re = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const q = p.get("q");
    const pat = p.get("mode") === "substring"
      ? re(q)
      : q.split(/[^\p{L}\p{N}]+/u).filter(Boolean).map(w => `(?<![\\p{L}\\p{N}])${re(w)}(?![\\p{L}\\p{N}])`).join("|");
    let rx;
    try { rx = new RegExp(pat, "giu"); } catch (e) { return esc; }
    return title => {
      let out = "", last = 0;
      for (const m of title.matchAll(rx)) {
        if (!m[0]) continue;
        out += esc(title.slice(last, m.index)) + "<mark>" + esc(m[0]) + "</mark>";
        last = m.index + m[0].length;
      }
      return out + esc(title.slice(last));
    };
  }

  function render(r, p) {
    const rows = r.rows;
    const oldest = p.get("sort") === "oldest";
    const hl = highlighter(p);
    // Collapse runs of identical headlines within the page, keeping the first link and a count.
    const groups = [];
    for (const row of rows) {
      const last = groups[groups.length - 1];
      if (last && last.title === row.title) { last.n++; last.domains.add(row.domain); }
      else groups.push({ ...row, n: 1, domains: new Set([row.domain]) });
    }
    if (!rows.length) {
      $("status").textContent = page > 1 ? "no more results" : "nothing found";
    } else {
      const span = rows.length > 1 ? `, ${fmtDay(rows[0].ts)} ${oldest ? "forward" : "back"} to ${fmtDay(rows[rows.length - 1].ts)}` : `, ${fmtDay(rows[0].ts)}`;
      $("status").innerHTML = `${rows.length === 100 ? (oldest ? "oldest 100" : "newest 100") : rows.length} result${rows.length === 1 ? "" : "s"}${page > 1 ? ` (page ${page})` : ""}${span}, ${r.elapsed.toFixed(1)}s` +
        (r.timedOut ? " — search hit its time limit; narrow the dates" : "") +
        (page === 1 ? ` · <a id="count" href="#">count by month</a>` : "");
      if (page === 1) $("count").onclick = e => { e.preventDefault(); count(p); };
    }
    $("results").innerHTML = groups.map(g => `<tr>
      <td class="when" title="${esc(g.ts)} UTC">${esc(g.ts.slice(0, 10))}</td>
      <td class="title"><a href="${esc(g.url)}" rel="nofollow noopener">${hl(g.title)}</a>${g.n > 1 ? ` <span class="n">×${g.n}</span>` : ""}</td>
      <td class="src"><a href="#" data-domain="${esc(g.domain)}" title="only ${esc(g.domain)}">${esc(g.domain)}</a>${g.n > 1 && g.domains.size > 1 ? ` +${g.domains.size - 1}` : ""}</td>
    </tr>`).join("");
    if (rows.length === 100 && page < 50) {
      $("more").innerHTML = `<a id="next">${oldest ? "newer" : "older"} results →</a>`;
      $("next").onclick = () => { page++; search(true); window.scrollTo(0, 0); };
    }
  }

  async function count(p) {
    $("months").textContent = "counting…";
    let r;
    try {
      r = await fetch("/api/count?" + p).then(res => res.json());
    } catch (e) { $("months").textContent = "count failed"; return; }
    if (r.error) { $("months").textContent = r.error; return; }
    const total = r.months.reduce((a, m) => a + m[1], 0);
    const max = Math.max(1, ...r.months.map(m => m[1]));
    $("months").innerHTML = `<p class="none">${total.toLocaleString()} matching headlines${r.partial ? " counted before the time limit; totals are incomplete" : ""} (${r.elapsed.toFixed(1)}s). Click a month to narrow to it.</p>` +
      `<table>${r.months.map(m => `<tr><td class="when"><a href="#" data-month="${esc(m[0].slice(0, 7))}">${esc(m[0].slice(0, 7))}</a></td><td class="bar"><span style="width:${(m[1] / max * 20).toFixed(1)}rem"></span> ${m[1].toLocaleString()}</td></tr>`).join("")}</table>`;
  }

  // Facets: a source in the results, or a month in the count, narrows the search.
  document.addEventListener("click", ev => {
    const a = ev.target.closest("a[data-domain], a[data-month]");
    if (!a) return;
    ev.preventDefault();
    if (a.dataset.domain) $("domain").value = a.dataset.domain;
    else {
      const [y, m] = a.dataset.month.split("-").map(Number);
      $("from").value = `${y}-${String(m).padStart(2, "0")}-01`;
      $("to").value = `${y}-${String(m).padStart(2, "0")}-${String(new Date(y, m, 0).getDate()).padStart(2, "0")}`;
    }
    page = 1; search(true); window.scrollTo(0, 0);
  });

  // What is loaded: the intro sentence and the date pickers' bounds.
  fetch("/api/stats").then(res => res.json()).then(s => {
    if (s.error) throw new Error(s.error);
    const first = s.first.slice(0, 10), last = s.last.slice(0, 10);
    const n = s.rows >= 1e9 ? `${(s.rows / 1e9).toFixed(1)} billion` : s.rows >= 1e6 ? `${(s.rows / 1e6).toFixed(1)} million` : s.rows.toLocaleString();
    const early = first > "2019-10-02" ? "; earlier years are still being loaded" : "";
    $("stats").textContent = `Loaded so far: ${n} headlines, ${fmtDay(first)} to ${fmtDay(last)}${early}.`;
    $("from").min = $("to").min = first; $("from").max = $("to").max = last;
  }).catch(() => { $("stats").textContent = "Could not reach the database just now."; });

  form.onsubmit = e => { e.preventDefault(); page = 1; search(true); };
  window.onpopstate = () => { if (fromUrl()) search(false); else { $("examples").hidden = false; $("status").textContent = ""; $("results").innerHTML = ""; $("more").innerHTML = ""; $("months").innerHTML = ""; document.title = "Headline Search"; } };
  if (fromUrl()) search(false);
})();
