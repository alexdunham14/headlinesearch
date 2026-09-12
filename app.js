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
  const fmt = n => n.toLocaleString();
  // 1.2M, 340k, 800: for the source suggestions.
  const fmtShort = n => n >= 1e6 ? `${+(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${+(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k` : String(n);
  // Paging: the Worker collapses identical headlines and returns where the
  // page stopped (a timestamp and how many rows at it were shown); the next
  // page asks from there. `page` is only a counter.
  let page = 1;
  let cursor = null;
  const reset = () => { page = 1; cursor = null; };
  let wantCount = false; // ?count=1 in the URL: show the chart too, so it can be linked to
  let chartOnly = false; // ?view=chart: the chart on its own, first on the page, with a link to the full search
  // What the database holds, from /api/stats; the chart's span and the date pickers' bounds.
  let loaded = { first: "2019-10-01", last: day(new Date()) };
  // The chosen sources, shown as chips under the source box; a search may
  // name several (the Worker takes them comma-separated) or none.
  const MAX_SOURCES = 10;
  let sources = [];
  // The month chart for the current term: per "YYYY-MM" a triple [stories,
  // outlets, articles] (see renderChart), filled in window by window; the
  // bars show the measure chosen under the chart.
  let chart = null;
  const MEASURES = ["stories", "outlets", "articles"];
  let measure = "stories";
  const setMeasure = m => { measure = MEASURES.includes(m) ? m : "stories"; document.querySelector(`input[name=measure][value=${measure}]`).checked = true; };

  // A search needs a term of two characters or more, or a source with an
  // empty box (everything from that site); one character is neither.
  const term = () => $("q").value.trim();
  const ready = () => term().length >= 2 || (term().length === 0 && sources.length > 0);

  function params() {
    const p = new URLSearchParams();
    if (term()) p.set("q", term());
    p.set("mode", form.mode.value);
    if (form.sort.value === "oldest") p.set("sort", "oldest");
    if ($("from").value) p.set("from", $("from").value);
    if ($("to").value) p.set("to", $("to").value);
    if (sources.length) p.set("domain", sources.join(","));
    if (cursor) { for (const [k, v] of Object.entries(cursor)) p.set(k, v); p.set("page", page); }
    return p;
  }

  function fromUrl() {
    const p = new URLSearchParams(location.search);
    sources = parseSources(p.get("domain"));
    renderChips();
    if (!p.get("q") && !sources.length) return false;
    $("q").value = p.get("q") || "";
    form.mode.value = p.get("mode") === "substring" ? "substring" : "word";
    form.sort.value = p.get("sort") === "oldest" ? "oldest" : "newest";
    $("from").value = p.get("from") || "";
    $("to").value = p.get("to") || "";
    const at = form.sort.value === "oldest" ? "after" : "before";
    cursor = p.get(at) ? { [at]: p.get(at), skip: p.get("skip") || 0 } : null;
    page = cursor ? Math.max(2, parseInt(p.get("page") || "2", 10) || 2) : 1;
    wantCount = p.get("count") === "1";
    chartOnly = p.get("view") === "chart";
    document.body.classList.toggle("chart-only", chartOnly);
    setMeasure(p.get("measure"));
    linkDates();
    return true;
  }

  // The chart belongs to a term, a mode and a set of sources; dates and order only narrow the list.
  const chartKey = p => [p.get("q") || "", p.get("mode"), p.get("domain") || ""].join("\n");
  const title = p => `${p.get("q") || sources.join(", ")} - News Headline Search`;
  // The page's own URL for a search: the query, count=1 while the chart is up,
  // the measure when not the default, and view=chart for the chart on its own
  // (which implies the chart, and carries no paging cursor).
  const pageUrl = (p, view = chartOnly) => {
    const u = new URLSearchParams(p);
    for (const k of ["count", "measure", "view"]) u.delete(k);
    if (view) for (const k of ["before", "after", "skip", "page"]) u.delete(k);
    if (chart || wantCount || view) {
      if (!view) u.set("count", "1");
      if (measure !== "stories") u.set("measure", measure);
    }
    if (view) u.set("view", "chart");
    return "?" + u;
  };
  // The chart's heading when it stands alone: the term, its mode, its sources.
  const describe = p => {
    const q = p.get("q"), d = sources.join(", ");
    return `${q ? `“${q}”` : "Everything"}${p.get("mode") === "substring" ? " as a substring" : ""}${d ? ` ${q ? "on" : "from"} ${d}` : ""}, by month`;
  };

  async function search(push) {
    const p = params();
    if (!ready()) return;
    if (chart && chart.key !== chartKey(p)) { chart = null; $("months").hidden = true; $("list-h").hidden = true; }
    if (push) history.pushState(null, "", pageUrl(p));
    document.title = title(p);
    $("examples").hidden = true;
    $("out").hidden = false;
    if (chartOnly) { // the chart on its own: no list, so no search; the count straight away
      if (chart) renderChart(p); else count(p);
      return;
    }
    $("status").textContent = "searching…";
    $("actions").hidden = true;
    $("results").innerHTML = "";
    $("more").innerHTML = "";
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

  // Wrap each match in <mark>: whole words in word mode (of every OR
  // alternative: "congo OR drc" marks either), the exact run in substring
  // mode. Matching runs on the folded title, and each folded character
  // remembers where in the original it came from, so the mark lands on the
  // original text ("Niño", not "nino"). No term (a source on its own) marks nothing.
  function highlighter(p) {
    const re = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const q = p.get("q") || "";
    if (!q) return esc;
    const words = [...new Set(q.split(/(?<=^|\s)OR(?=\s|$)/).map(fold).join(" ").split(/[^\p{L}\p{N}]+/u).filter(Boolean))];
    const pat = p.get("mode") === "substring"
      ? re(fold(q))
      : words.map(w => `(?<![\\p{L}\\p{N}])${re(w)}(?![\\p{L}\\p{N}])`).join("|");
    if (!pat) return esc;
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

  // The other sites that carried a headline (its copies grouped by site,
  // the first site left out), each with the first URL seen there and how
  // many copies that site had.
  function otherSites(g) {
    const m = new Map();
    for (const [d, u] of g.copies || []) {
      if (d === g.domain) continue;
      const o = m.get(d);
      if (o) o.k++; else m.set(d, { u, k: 1 });
    }
    return [...m];
  }

  function render(r, p) {
    const rows = r.rows; // one per distinct headline, with n copies on `sites` sites
    const oldest = p.get("sort") === "oldest";
    const hl = highlighter(p);
    if (!rows.length) {
      $("status").textContent = page > 1 ? "No more results." : "Nothing found.";
    } else {
      // "Displaying 100 headlines from the latest 316 articles": the
      // rows behind the page, once copies collapsed.
      const n = rows.length;
      $("status").textContent = `Displaying ${fmt(n)} headline${n === 1 ? "" : "s"} from the ${oldest ? "earliest" : "latest"} ${fmt(r.used)} article${r.used === 1 ? "" : "s"}.`;
    }
    $("actions").hidden = !(rows.length || chart);
    $("count").hidden = !!chart;
    $("compare").hidden = p.get("mode") === "substring";
    $("compare").href = compareUrl(p);
    $("list-h").hidden = !chart;
    $("list-h").textContent = `Headlines, ${oldest ? "oldest" : "newest"} first`;
    $("results").innerHTML = rows.map(g => {
      const others = otherSites(g);
      const more = others.length ? ` <a href="#" class="ex" aria-expanded="false">+${others.length} more</a><span class="copies" hidden>: ${others.map(([d, o]) => `<a href="${esc(o.u)}" rel="nofollow noopener">${esc(d)}</a>${o.k > 1 ? `<span class="n">&times;${o.k}</span>` : ""}`).join(", ")}</span>` : "";
      return `<li>
      ${hl(g.title)}${g.n > 1 ? ` <span class="n" title="${g.n} copies of this headline${g.sites > 1 ? ` on ${g.sites} sites` : ""}">×${g.n}</span>` : ""}
      <span class="m"><span title="${esc(g.ts)} UTC">${fmtDay(g.ts)}</span> · <a href="${esc(g.url)}" rel="nofollow noopener">${esc(g.domain)}</a>${more}</span>
    </li>`;
    }).join("");
    if (r.next) {
      $("more").innerHTML = `<a id="next">${oldest ? "newer" : "older"} results →</a>`;
      $("next").onclick = () => { cursor = r.next; page++; search(true); $("out").scrollIntoView(); };
    }
    if (chart) renderChart(p);
    else if (wantCount && rows.length) { wantCount = false; count(p); }
  }

  // The compare page with this search's term, sources, measure and dates.
  function compareUrl(p) {
    const s = [p.get("q") || "", sources.length ? `site:${sources.join(",")}` : ""].join(" ").trim();
    const cp = new URLSearchParams({ s });
    if (measure !== "stories") cp.set("measure", measure);
    if (p.get("from")) cp.set("from", p.get("from"));
    if (p.get("to")) cp.set("to", p.get("to"));
    return "/compare?" + cp;
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
    $("count").hidden = true;
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
      const q = new URLSearchParams({ mode: p.get("mode"), from: w[0] + "-01", to: monthEnd(w[w.length - 1]) });
      if (p.get("q")) q.set("q", p.get("q"));
      if (p.get("domain")) q.set("domain", p.get("domain"));
      let r;
      try { r = await fetch("/api/count?" + q).then(res => res.json()); } catch (e) { r = { error: "count failed" }; }
      if (chart.key !== key || r.error) return r;
      for (const m of w) chart.counts.set(m, [0, 0, 0]);
      for (const [m, s, o, a] of r.months) chart.counts.set(m.slice(0, 7), [s, o, a]);
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

  // Three counts a month, from the copy flag the database keeps per row:
  // stories (a headline's first appearance anywhere in a week), outlets (its
  // first appearance on each site) and articles (every page). The bars show
  // one; the note, the bar titles and the table give all three.
  function renderChart(p) {
    const months = monthList();
    const c = chart.counts;
    const k = MEASURES.indexOf(measure);
    const from = p.get("from") || loaded.first, to = p.get("to") || loaded.last;
    const narrowed = p.get("from") || p.get("to");
    const total = [0, 0, 0];
    let max = 1, peak = null, first = null, last = null;
    for (const m of months) {
      const v = c.get(m);
      if (v == null) continue;
      for (let i = 0; i < 3; i++) total[i] += v[i];
      if (v[k] > max) { max = v[k]; peak = m; }
      if (v[2] && !first) first = m;
      if (v[2]) last = m;
    }
    const triple = v => `${fmt(v[0])} stories, ${fmt(v[1])} outlets, ${fmt(v[2])} articles`;
    let note;
    if (chart.running && chart.wait) note = `${fmt(total[k])} ${measure} so far; too many requests from here in a minute, so the rest wait ${chart.wait} s…`;
    else if (chart.running) note = `${fmt(total[k])} ${measure} so far; counting ${chart.now ? `${fmtMonth(chart.now[0])} to ${fmtMonth(chart.now[chart.now.length - 1])}` : ""}…`;
    else if (!total[2]) note = chart.failed ? "The count could not be completed." : "No matching articles in any month.";
    else note = `${triple(total)}, ${fmtMonth(first)} to ${fmtMonth(last)}${peak ? `, most ${measure} in ${fmtMonth(peak)} (${fmt(max)})` : ""}.`;
    if (!chart.running && chart.partial.size) note += " Months marked ~ hit the time limit, so their counts are low.";
    if (!chart.running && chart.failed) note += ` ${chart.failed} window${chart.failed === 1 ? "" : "s"} could not be counted${chart.limited ? " (too many searches from here in a minute; search again in a minute to fill them in)" : ""}.`;
    if (!chart.running && total[2]) note += " Click a month or a year to narrow the search to it.";
    $("months-note").textContent = note;
    $("compare").href = compareUrl(p);
    $("months-h").textContent = chartOnly ? describe(p) : "Matches by month";
    $("chart-link").href = pageUrl(p, !chartOnly);
    $("chart-link").textContent = chartOnly ? "See the headlines and the full search" : "Linkable chart";
    $("chart").innerHTML = months.map(m => {
      const v = c.get(m);
      const sel = narrowed && from <= m + "-01" && to >= monthEnd(m);
      const label = v == null ? "not counted yet" : `${triple(v)}${chart.partial.has(m) ? " (partial)" : ""}`;
      return `<a href="#" class="${sel ? "sel" : ""}" data-month="${m}" title="${fmtMonth(m)}: ${label}"><i style="height:${v && v[k] ? Math.max(1.5, v[k] / max * 100).toFixed(1) : 0}%"></i></a>`;
    }).join("");
    $("years").innerHTML = months.map(m => `<span>${m.endsWith("-01") ? `<a href="#" data-year="${m.slice(0, 4)}">${m.slice(0, 4)}</a>` : ""}</span>`).join("");
    $("months-table").innerHTML = `<tr><th></th>${MEASURES.map(x => `<th>${x}</th>`).join("")}</tr>` + months.filter(m => c.get(m)?.[2]).map(m =>
      `<tr><td><a href="#" data-month="${m}">${fmtMonth(m)}</a></td>${[0, 1, 2].map(i => `<td>${chart.partial.has(m) ? "~" : ""}${fmt(c.get(m)[i])}</td>`).join("")}</tr>`).join("");
  }

  // A month or a year in the chart narrows the search to it; "+3 more" on a
  // headline opens the other sites that carried it.
  document.addEventListener("click", ev => {
    const ex = ev.target.closest("a.ex");
    if (ex) {
      ev.preventDefault();
      const open = ex.getAttribute("aria-expanded") !== "true";
      ex.setAttribute("aria-expanded", String(open));
      ex.nextElementSibling.hidden = !open;
      ex.textContent = open ? "fewer" : `+${ex.nextElementSibling.querySelectorAll("a").length} more`;
      return;
    }
    const a = ev.target.closest("a[data-month], a[data-year]");
    if (!a) return;
    ev.preventDefault();
    if (a.dataset.month) { $("from").value = a.dataset.month + "-01"; $("to").value = monthEnd(a.dataset.month); }
    else { $("from").value = a.dataset.year + "-01-01"; $("to").value = a.dataset.year + "-12-31"; }
    linkDates();
    if (chartOnly) { location.href = pageUrl(params(), false); return; } // from the chart alone, a month opens the full search narrowed to it
    reset();
    search(true);
  });
  const chartUrl = p => history.replaceState(null, "", pageUrl(p));
  $("count").onclick = () => {
    const p = params();
    count(p); // sets `chart` before its first await, so the URL below carries count=1
    chartUrl(p);
  };
  $("measures").addEventListener("change", ev => {
    setMeasure(ev.target.value);
    const p = params();
    if (chart) { chartUrl(p); renderChart(p); }
  });

  // ------------------------------------------------------------------ sources
  // The source box suggests sites as you type (from /api/sources: the sites
  // whose name contains the text, those starting with it first, then the
  // biggest); a chosen site becomes a chip under the box, with an × to
  // remove it. Enter on a typed name that is not in the list adds it as
  // typed, cleaned to a bare site name.
  function parseSources(s) {
    return [...new Set((s || "").toLowerCase().split(",").map(cleanSource).filter(Boolean))].slice(0, MAX_SOURCES);
  }
  function cleanSource(s) {
    return s.trim().toLowerCase().replace(/^[a-z]+:\/\//, "").replace(/^www\./, "").replace(/[/?#].*$/, "").replace(/[^a-z0-9.-]/g, "");
  }
  function renderChips() {
    $("chosen").innerHTML = sources.map(d => `<li>${esc(d)}<button type="button" class="x" data-remove="${esc(d)}" aria-label="Remove ${esc(d)}" title="Remove">&times;</button></li>`).join("");
    $("chosen").hidden = !sources.length;
  }
  function addSource(d) {
    d = cleanSource(d);
    if (!d || sources.includes(d) || sources.length >= MAX_SOURCES) return false;
    sources.push(d);
    renderChips();
    return true;
  }
  $("chosen").addEventListener("click", ev => {
    const b = ev.target.closest("button[data-remove]");
    if (!b) return;
    sources = sources.filter(d => d !== b.dataset.remove);
    renderChips();
    if (ready()) go(); else home(true);
  });

  const box = $("domain"), list = $("suggest");
  const found = new Map(); // text typed -> the sites suggested, so retyping costs nothing
  let sugg = [], active = -1, seq = 0, timer;
  const closeList = () => { list.hidden = true; list.innerHTML = ""; sugg = []; active = -1; box.setAttribute("aria-expanded", "false"); };
  async function suggest() {
    const text = cleanSource(box.value);
    if (!text) { closeList(); return; }
    const my = ++seq;
    let r = found.get(text);
    if (!r) {
      try { r = await fetch("/api/sources?q=" + encodeURIComponent(text)).then(res => res.json()); } catch (e) { r = {}; }
      if (r.sources) found.set(text, r);
    }
    if (my !== seq || cleanSource(box.value) !== text) return;
    sugg = (r.sources || []).filter(s => !sources.includes(s[0]));
    if (!sugg.length) { closeList(); return; }
    active = -1;
    list.innerHTML = sugg.map((s, i) => `<li role="option" id="sug-${i}" data-i="${i}">${esc(s[0])} <span class="n">${fmtShort(s[1])} articles</span></li>`).join("");
    list.hidden = false;
    box.setAttribute("aria-expanded", "true");
  }
  const mark = () => { [...list.children].forEach((li, i) => li.classList.toggle("on", i === active)); box.setAttribute("aria-activedescendant", active >= 0 ? `sug-${active}` : ""); };
  const choose = d => { if (addSource(d)) { box.value = ""; closeList(); go(); } else { box.value = ""; closeList(); } };
  box.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(suggest, 120); });
  box.addEventListener("focus", () => { if (box.value) suggest(); });
  box.addEventListener("blur", () => setTimeout(closeList, 150));
  box.addEventListener("keydown", ev => {
    if (ev.key === "ArrowDown" && sugg.length) { ev.preventDefault(); active = Math.min(active + 1, sugg.length - 1); mark(); }
    else if (ev.key === "ArrowUp" && sugg.length) { ev.preventDefault(); active = Math.max(active - 1, -1); mark(); }
    else if (ev.key === "Escape") { closeList(); }
    else if (ev.key === "Enter") {
      if (active >= 0) { ev.preventDefault(); choose(sugg[active][0]); }
      else if (box.value.trim()) { ev.preventDefault(); choose(box.value); }
      // an empty box: Enter submits the form, as in any other field
    }
  });
  list.addEventListener("pointerdown", ev => {
    const li = ev.target.closest("li[data-i]");
    if (!li) return;
    ev.preventDefault(); // keep the focus, so the blur does not close the list first
    choose(sugg[li.dataset.i][0]);
  });

  // What is loaded: the number in the lede, the line under "More
  // information", the chart's span and the date pickers' bounds.
  const statsReady = fetch("/api/stats").then(res => res.json()).then(s => {
    if (s.error) throw new Error(s.error);
    loaded = { first: s.first.slice(0, 10), last: s.last.slice(0, 10) };
    $("n").textContent = s.rows >= 1e6 ? `${Math.round(s.rows / 1e6)} million` : fmt(s.rows);
    $("stats").textContent = `${fmt(s.rows)} headlines from ${s.sources ? `${fmt(s.sources)} sources, ` : ""}${fmtDay(loaded.first)} to ${fmtDay(loaded.last)}.`
      + (loaded.first > "2019-10-02" ? " Earlier years are still being loaded." : "");
    $("from").min = $("to").min = loaded.first; $("from").max = $("to").max = loaded.last; linkDates();
  }).catch(() => { $("stats").textContent = "Could not reach the database just now."; });

  // Dates: "to" cannot precede "from" (fixing one side moves the other), both bounded to what
  // is loaded once /api/stats answers; clear, and shifting the range by its own length.
  function linkDates(changed) {
    const f = $("from"), t = $("to");
    if (f.value && t.value && t.value < f.value) { if (changed === "from") t.value = f.value; else f.value = t.value; }
    t.min = f.value || f.min; f.max = t.value || t.max || "";
  }
  const go = () => { linkDates(); if (ready()) { reset(); search(true); } };
  $("from").addEventListener("change", () => { linkDates("from"); go(); });
  $("to").addEventListener("change", () => { linkDates("to"); go(); });
  $("clear-dates").onclick = () => { $("from").value = ""; $("to").value = ""; go(); };
  const shift = dir => {
    const f = $("from").value || loaded.first, t = $("to").value || loaded.last;
    const len = Math.round((new Date(t) - new Date(f)) / 864e5) + 1;
    $("from").value = plus(f, dir * len); $("to").value = plus(t, dir * len); go();
  };
  $("earlier").onclick = () => shift(-1);
  $("later").onclick = () => shift(1);
  // Every filter back to its default; the term stays. With a term the
  // search runs again; without one there is nothing left to search.
  $("clear-all").onclick = () => {
    form.mode.value = "word"; form.sort.value = "newest";
    $("from").value = ""; $("to").value = ""; linkDates();
    sources = []; renderChips(); box.value = ""; closeList();
    if (ready()) go(); else home(true);
  };
  form.addEventListener("change", ev => { if (ev.target.name === "mode" || ev.target.name === "sort") go(); });

  // The front page: no results, the examples back.
  function home(push) {
    if (push) history.pushState(null, "", location.pathname);
    $("examples").hidden = false; $("out").hidden = true; chart = null; $("months").hidden = true;
    document.title = "News Headline Search";
  }
  form.onsubmit = e => {
    e.preventDefault();
    if (!ready()) {
      $("q").setCustomValidity(term().length === 1 ? "Type at least two characters." : "Type a word, or choose a source.");
      $("q").reportValidity();
      return;
    }
    reset(); search(true);
  };
  $("q").addEventListener("input", () => $("q").setCustomValidity(""));
  window.onpopstate = () => { if (fromUrl()) search(false); else home(false); };
  if (fromUrl()) search(false);
})();
