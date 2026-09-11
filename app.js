(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const form = $("f");
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDay = s => { const [y, m, d] = s.slice(0, 10).split("-").map(Number); return `${d} ${MONTHS[m - 1]} ${y}`; };
  const fmtMonth = ym => { const [y, m] = ym.split("-").map(Number); return `${MONTHS[m - 1]} ${y}`; };
  const day = d => d.toISOString().slice(0, 10);
  const plus = (s, n) => { const d = new Date(s + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return day(d); };
  const monthEnd = ym => { const [y, m] = ym.split("-").map(Number); return `${ym}-${String(new Date(Date.UTC(y, m, 0)).getUTCDate()).padStart(2, "0")}`; };
  // Paging: the Worker collapses identical headlines and returns where the
  // page stopped (a timestamp and how many rows at it were shown); the next
  // page asks from there. `page` is only a counter for the status line.
  let page = 1;
  let cursor = null;
  const reset = () => { page = 1; cursor = null; };
  let wantCount = false; // ?count=1 in the URL: show the chart too, so it can be linked to
  // What the database holds, from /api/stats; the chart's span and the date pickers' bounds.
  let loaded = { first: "2019-10-01", last: day(new Date()) };
  // The month chart for the current term: counts per "YYYY-MM", filled in window by window.
  let chart = null;

  function params() {
    const p = new URLSearchParams();
    p.set("q", $("q").value.trim());
    p.set("mode", form.mode.value);
    if (form.sort.value === "oldest") p.set("sort", "oldest");
    if ($("from").value) p.set("from", $("from").value);
    if ($("to").value) p.set("to", $("to").value);
    if ($("domain").value.trim()) p.set("domain", $("domain").value.trim());
    if (cursor) { for (const [k, v] of Object.entries(cursor)) p.set(k, v); p.set("page", page); }
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
    const at = form.sort.value === "oldest" ? "after" : "before";
    cursor = p.get(at) ? { [at]: p.get(at), skip: p.get("skip") || 0 } : null;
    page = cursor ? Math.max(2, parseInt(p.get("page") || "2", 10) || 2) : 1;
    wantCount = p.get("count") === "1";
    linkDates();
    return true;
  }

  // The chart belongs to a term, a mode and a source; dates and order only narrow the list.
  const chartKey = p => [p.get("q"), p.get("mode"), p.get("domain") || ""].join("\n");

  async function search(push) {
    const p = params();
    if (p.get("q").length < 2) return;
    if (push) history.pushState(null, "", "?" + p);
    document.title = `${p.get("q")} - Headline Search`;
    $("examples").hidden = true;
    $("out").hidden = false;
    $("status").textContent = "searching…";
    $("count").hidden = true;
    $("results").innerHTML = "";
    $("more").innerHTML = "";
    if (chart && chart.key !== chartKey(p)) { chart = null; $("months").hidden = true; $("list-h").hidden = true; }
    let r;
    try {
      r = await fetch("/api/search?" + p).then(res => res.json());
    } catch (e) {
      $("status").textContent = "search failed; try again";
      return;
    }
    if (r.error) { failed(r, p); return; }
    render(r, p);
  }

  // A search that hit its time limit gets two ways out: the last twelve months, or
  // whole words instead of a substring, which the token index answers quickly.
  function failed(r, p) {
    let html = esc(r.error);
    if (r.timeout) {
      html += ` <button type="button" class="chip" id="try-year">search the last 12 months</button>`;
      if (p.get("mode") === "substring") html += ` <button type="button" class="chip" id="try-word">try whole words</button>`;
    }
    $("status").innerHTML = html;
    if (!r.timeout) return;
    $("try-year").onclick = () => { const last = $("to").value || loaded.last; $("from").value = plus(last, -364); $("to").value = last; go(); };
    if ($("try-word")) $("try-word").onclick = () => { form.mode.value = "word"; go(); };
  }

  // The database matches words on a folded title: lowercased, accents
  // stripped, ł ø đ ħ ŧ ı ß æ œ mapped to ASCII (the same fold() as worker.js;
  // keep them identical). So a search for "nino" returns "El Niño", and the
  // highlighter has to fold the same way to find what to mark.
  function fold(s) {
    return s.toLowerCase().normalize("NFD").replace(/\p{Mn}/gu, "")
      .replace(/[łøđħŧı]/g, c => "lodhti"["łøđħŧı".indexOf(c)])
      .replace(/ß/g, "ss").replace(/æ/g, "ae").replace(/œ/g, "oe");
  }

  // Wrap each match in <mark>: whole words in word mode, the exact run in
  // substring mode. Matching runs on the folded title, and each folded
  // character remembers where in the original it came from, so the mark
  // lands on the original text ("Niño", not "nino").
  function highlighter(p) {
    const re = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const q = fold(p.get("q"));
    const pat = p.get("mode") === "substring"
      ? re(q)
      : q.split(/[^\p{L}\p{N}]+/u).filter(Boolean).map(w => `(?<![\\p{L}\\p{N}])${re(w)}(?![\\p{L}\\p{N}])`).join("|");
    let rx;
    try { rx = new RegExp(pat, "giu"); } catch (e) { return esc; }
    return title => {
      let folded = "", starts = [], ends = [], i = 0;
      for (const ch of title) {
        const f = fold(ch);
        for (let k = 0; k < f.length; k++) { starts.push(i); ends.push(i + ch.length); }
        folded += f;
        i += ch.length;
      }
      let out = "", last = 0;
      for (const m of folded.matchAll(rx)) {
        if (!m[0]) continue;
        const a = starts[m.index], b = ends[m.index + m[0].length - 1];
        if (a < last) continue;
        out += esc(title.slice(last, a)) + "<mark>" + esc(title.slice(a, b)) + "</mark>";
        last = b;
      }
      return out + esc(title.slice(last));
    };
  }

  function render(r, p) {
    const rows = r.rows; // one per distinct headline, with n copies on `sites` sites
    const oldest = p.get("sort") === "oldest";
    const hl = highlighter(p);
    const range = p.get("from") || p.get("to") ? ` between ${fmtDay(p.get("from") || loaded.first)} and ${fmtDay(p.get("to") || loaded.last)}` : "";
    if (!rows.length) {
      $("status").textContent = page > 1 ? "No more results." : `Nothing found${range}.`;
    } else {
      const a = fmtDay(rows[0].ts), b = fmtDay(rows[rows.length - 1].ts);
      const span = a === b ? `on ${a}` : `${a} ${oldest ? "forward" : "back"} to ${b}`;
      const end = oldest ? "oldest" : "newest";
      const n = rows.length, used = r.used.toLocaleString();
      // "Newest 100 headlines from 316 matching articles": the rows behind
      // the page, when copies collapsed. A page that stayed short even after
      // the Worker looked further says how far it looked.
      const what = r.next && n === 100 ? `${oldest ? "Oldest" : "Newest"} 100 headlines${r.used > n ? ` from ${used} matching articles` : ""}`
        : r.next ? `${n} headlines from the ${end} ${used} matching articles`
        : `${n} headline${n === 1 ? "" : "s"}${r.used > n ? ` from ${used} matching articles` : ""}`;
      $("status").textContent = `${what}${page > 1 ? ` (page ${page})` : ""}${p.get("domain") ? ` on ${p.get("domain")}` : ""}, ${span}, ${r.elapsed.toFixed(1)}s.`;
    }
    $("count").hidden = !(rows.length || chart);
    $("count").classList.toggle("act", !chart);
    $("list-h").hidden = !chart;
    $("list-h").textContent = `Headlines, ${oldest ? "oldest" : "newest"} first`;
    $("results").innerHTML = rows.map(g => `<li>
      <a class="t" href="${esc(g.url)}" rel="nofollow noopener">${hl(g.title)}</a>${g.n > 1 ? ` <span class="n" title="${g.n} copies of this headline${g.sites > 1 ? ` on ${g.sites} sites` : ""}">×${g.n}</span>` : ""}
      <span class="m"><span title="${esc(g.ts)} UTC">${fmtDay(g.ts)}</span> · <a href="#" data-domain="${esc(g.domain)}" title="Only ${esc(g.domain)}">${esc(g.domain)}</a>${g.sites > 1 ? ` +${g.sites - 1} more` : ""}</span>
    </li>`).join("");
    if (r.next) {
      $("more").innerHTML = `<a id="next">${oldest ? "newer" : "older"} results →</a>`;
      $("next").onclick = () => { cursor = r.next; page++; search(true); $("out").scrollIntoView(); };
    }
    if (chart) renderChart(p);
    else if (wantCount && rows.length) { wantCount = false; count(p); }
  }

  // ------------------------------------------------------------- the month chart
  // Every month the database holds, oldest first.
  function monthList() {
    const out = [];
    let [y, m] = loaded.first.slice(0, 7).split("-").map(Number);
    const last = loaded.last.slice(0, 7);
    for (;;) {
      const ym = `${y}-${String(m).padStart(2, "0")}`;
      out.push(ym);
      if (ym >= last) return out;
      if (++m > 12) { m = 1; y++; }
    }
  }

  // The count runs in twelve-month windows, newest first, so the chart fills
  // in as they arrive (about a second a window since the text index); each
  // window is cached at the edge for a day. The Worker refuses requests when
  // an address has sent too many in a minute (a search with its chart is
  // eight); windows refused that way are asked for again after the minute.
  async function count(p) {
    const key = chartKey(p);
    chart = { key, counts: new Map(), partial: new Set(), running: true, failed: 0, limited: 0, wait: 0 };
    $("months").hidden = false;
    $("list-h").hidden = false;
    $("count").classList.remove("act");
    $("months-note").textContent = "counting…";
    await statsReady; // the chart spans what is loaded, so wait to know that
    if (chart.key !== key) return;
    const months = monthList();
    const windows = [];
    for (let i = months.length; i > 0; i -= 12) windows.push(months.slice(Math.max(0, i - 12), i));
    renderChart(p);
    const fetchWindow = async w => {
      chart.now = w;
      renderChart(p);
      const q = new URLSearchParams({ q: p.get("q"), mode: p.get("mode"), from: w[0] + "-01", to: monthEnd(w[w.length - 1]) });
      if (p.get("domain")) q.set("domain", p.get("domain"));
      let r;
      try { r = await fetch("/api/count?" + q).then(res => res.json()); } catch (e) { r = { error: "count failed" }; }
      if (chart.key !== key || r.error) return r;
      for (const m of w) chart.counts.set(m, 0);
      for (const [m, n] of r.months) chart.counts.set(m.slice(0, 7), n);
      if (r.partial) for (const m of w) chart.partial.add(m);
      return r;
    };
    const refused = [];
    for (const w of windows) {
      if (chart.key !== key) return;
      const r = await fetchWindow(w);
      if (chart.key !== key) return;
      if (r.error) { if (r.rateLimited) refused.push(w); else chart.failed++; }
    }
    if (refused.length) {
      chart.now = null;
      for (chart.wait = 30; chart.wait > 0; chart.wait--) {
        renderChart(p);
        await new Promise(res => setTimeout(res, 1000));
        if (chart.key !== key) return;
      }
      for (const w of refused) {
        const r = await fetchWindow(w);
        if (chart.key !== key) return;
        if (r.error) { chart.failed++; if (r.rateLimited) chart.limited++; }
      }
    }
    chart.running = false;
    chart.now = null;
    renderChart(p);
  }

  function renderChart(p) {
    const months = monthList();
    const c = chart.counts;
    const from = p.get("from") || loaded.first, to = p.get("to") || loaded.last;
    const narrowed = p.get("from") || p.get("to");
    let total = 0, max = 1, peak = null, first = null, last = null;
    for (const m of months) {
      const n = c.get(m);
      if (n == null) continue;
      total += n;
      if (n > max) { max = n; peak = m; }
      if (n && !first) first = m;
      if (n) last = m;
    }
    let note;
    if (chart.running && chart.wait) note = `${total.toLocaleString()} so far; too many requests from here in a minute, so the rest wait ${chart.wait} s…`;
    else if (chart.running) note = `${total.toLocaleString()} so far; counting ${chart.now ? `${fmtMonth(chart.now[0])} to ${fmtMonth(chart.now[chart.now.length - 1])}` : ""}…`;
    else if (!total) note = chart.failed ? "The count could not be completed." : "No matching articles in any month.";
    // Articles, not headlines: the count is of rows, one per article URL, copies included.
    else note = `${total.toLocaleString()} matching article${total === 1 ? "" : "s"}, ${fmtMonth(first)} to ${fmtMonth(last)}, most in ${fmtMonth(peak)} (${max.toLocaleString()}).`;
    if (!chart.running && chart.partial.size) note += " Months marked ~ hit the time limit, so their counts are low.";
    if (!chart.running && chart.failed) note += ` ${chart.failed} window${chart.failed === 1 ? "" : "s"} could not be counted${chart.limited ? " (too many searches from here in a minute; search again in a minute to fill them in)" : ""}.`;
    if (!chart.running && total) note += " Click a month or a year to narrow the search to it.";
    $("months-note").textContent = note;
    $("chart").innerHTML = months.map(m => {
      const n = c.get(m);
      const sel = narrowed && from <= m + "-01" && to >= monthEnd(m);
      const label = n == null ? "not counted yet" : `${n.toLocaleString()}${chart.partial.has(m) ? " (partial)" : ""}`;
      return `<a href="#" class="${sel ? "sel" : ""}" data-month="${m}" title="${fmtMonth(m)}: ${label}"><i style="height:${n ? Math.max(1.5, n / max * 100).toFixed(1) : 0}%"></i></a>`;
    }).join("");
    $("years").innerHTML = months.map(m => `<span>${m.endsWith("-01") ? `<a href="#" data-year="${m.slice(0, 4)}">${m.slice(0, 4)}</a>` : ""}</span>`).join("");
    $("months-table").innerHTML = months.filter(m => c.get(m)).map(m => `<tr><td><a href="#" data-month="${m}">${fmtMonth(m)}</a></td><td>${chart.partial.has(m) ? "~" : ""}${c.get(m).toLocaleString()}</td></tr>`).join("");
  }

  // Facets: a source in the list, a month or a year in the chart, each narrows the search.
  document.addEventListener("click", ev => {
    const a = ev.target.closest("a[data-domain], a[data-month], a[data-year]");
    if (!a) return;
    ev.preventDefault();
    if (a.dataset.domain) { $("domain").value = a.dataset.domain; }
    else if (a.dataset.month) { $("from").value = a.dataset.month + "-01"; $("to").value = monthEnd(a.dataset.month); }
    else { $("from").value = a.dataset.year + "-01-01"; $("to").value = a.dataset.year + "-12-31"; }
    linkDates();
    reset();
    search(true);
    if (a.dataset.domain) $("out").scrollIntoView();
  });
  $("count").onclick = () => {
    const p = params();
    history.replaceState(null, "", "?" + p + "&count=1");
    count(p);
  };

  // What is loaded: the intro sentence, the chart's span and the date pickers' bounds.
  const statsReady = fetch("/api/stats").then(res => res.json()).then(s => {
    if (s.error) throw new Error(s.error);
    loaded = { first: s.first.slice(0, 10), last: s.last.slice(0, 10) };
    const n = s.rows >= 1e9 ? `${(s.rows / 1e9).toFixed(1)} billion` : s.rows >= 1e6 ? `${(s.rows / 1e6).toFixed(1)} million` : s.rows.toLocaleString();
    $("stats").textContent = loaded.first > "2019-10-02"
      ? `Loaded so far: ${n} headlines, ${fmtDay(loaded.first)} to ${fmtDay(loaded.last)}; earlier years are still being loaded.`
      : `${n} headlines, ${fmtDay(loaded.first)} to ${fmtDay(loaded.last)}.`;
    $("from").min = $("to").min = loaded.first; $("from").max = $("to").max = loaded.last; linkDates();
  }).catch(() => { $("stats").textContent = "Could not reach the database just now."; });

  // Dates: "to" cannot precede "from" (fixing one side moves the other), both bounded to what
  // is loaded once /api/stats answers; clear, and shifting the range by its own length.
  function linkDates(changed) {
    const f = $("from"), t = $("to");
    if (f.value && t.value && t.value < f.value) { if (changed === "from") t.value = f.value; else f.value = t.value; }
    t.min = f.value || f.min; f.max = t.value || t.max || "";
  }
  const go = () => { linkDates(); if ($("q").value.trim().length >= 2) { reset(); search(true); } };
  $("from").addEventListener("change", () => { linkDates("from"); go(); });
  $("to").addEventListener("change", () => { linkDates("to"); go(); });
  $("clear-dates").onclick = () => { $("from").value = ""; $("to").value = ""; go(); };
  $("clear-domain").onclick = () => { $("domain").value = ""; go(); };
  const shift = dir => {
    const f = $("from").value || loaded.first, t = $("to").value || loaded.last;
    const len = Math.round((new Date(t) - new Date(f)) / 864e5) + 1;
    $("from").value = plus(f, dir * len); $("to").value = plus(t, dir * len); go();
  };
  $("earlier").onclick = () => shift(-1);
  $("later").onclick = () => shift(1);

  form.onsubmit = e => { e.preventDefault(); reset(); search(true); };
  window.onpopstate = () => {
    if (fromUrl()) search(false);
    else { $("examples").hidden = false; $("out").hidden = true; chart = null; $("months").hidden = true; document.title = "Headline Search"; }
  };
  if (fromUrl()) search(false);
})();
