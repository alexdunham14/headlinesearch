(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const form = $("f");
  let page = 1;

  function params() {
    const p = new URLSearchParams();
    p.set("q", $("q").value.trim());
    p.set("mode", form.mode.value);
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

  function render(r, p) {
    const rows = r.rows;
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
      $("status").innerHTML = `${rows.length === 100 ? "newest 100" : rows.length} result${rows.length === 1 ? "" : "s"}${page > 1 ? ` (page ${page})` : ""}, ${r.elapsed.toFixed(1)}s` +
        (r.timedOut ? " — search hit its time limit; narrow the dates" : "") +
        (page === 1 ? ` · <a id="count" href="#">count by month</a>` : "");
      if (page === 1) $("count").onclick = e => { e.preventDefault(); count(p); };
    }
    $("results").innerHTML = groups.map(g => `<tr>
      <td class="when">${esc(g.ts.slice(0, 10))}</td>
      <td class="title"><a href="${esc(g.url)}" rel="nofollow noopener">${esc(g.title)}</a>${g.n > 1 ? ` <span class="n">×${g.n}</span>` : ""}</td>
      <td class="src">${esc(g.n > 1 && g.domains.size > 1 ? `${g.domain} +${g.domains.size - 1}` : g.domain)}</td>
    </tr>`).join("");
    if (rows.length === 100 && page < 50) {
      $("more").innerHTML = `<a id="next">older results →</a>`;
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
    $("months").innerHTML = `<p class="none">${total.toLocaleString()} matching headlines${r.partial ? " counted before the time limit; totals are incomplete" : ""} (${r.elapsed.toFixed(1)}s)</p>` +
      `<table>${r.months.map(m => `<tr><td class="when">${esc(m[0].slice(0, 7))}</td><td class="bar"><span style="width:${(m[1] / max * 20).toFixed(1)}rem"></span> ${m[1].toLocaleString()}</td></tr>`).join("")}</table>`;
  }

  form.onsubmit = e => { e.preventDefault(); page = 1; search(true); };
  window.onpopstate = () => { if (fromUrl()) search(false); };
  if (fromUrl()) search(false);
})();
