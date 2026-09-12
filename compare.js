// Compare mode: up to six terms on one chart, month by month. Each term is a
// whole-word search (OR between alternatives, optional site:domain filters,
// or a site: on its own for everything from that source), and
// each is counted by the same /api/count in the same twelve-month windows
// as the search page's chart, so the two share the edge cache; a share is
// against /api/totals, the three counts a month with no term. The lines are
// SVG drawn here; "save as image" draws the same SVG again on white with the
// legend and the page's URL and rasterises it in the browser.
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDay = s => { const [y, m, d] = s.slice(0, 10).split("-").map(Number); return `${d} ${MONTHS[m - 1]} ${y}`; };
  const fmtMonth = ym => { const [y, m] = ym.split("-").map(Number); return `${MONTHS[m - 1]} ${y}`; };
  const day = d => d.toISOString().slice(0, 10);
  const monthEnd = ym => { const [y, m] = ym.split("-").map(Number); return `${ym}-${String(new Date(Date.UTC(y, m, 0)).getUTCDate()).padStart(2, "0")}`; };
  const fmt = n => Math.round(n).toLocaleString();
  // 1.2M, 340k, 12k, 800: for axis ticks and denominators.
  const fmtShort = n => n >= 1e6 ? `${+(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${+(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k` : String(Math.round(n));
  // A share as a percentage with two significant digits (0.042%, 3.1%, 12%).
  const fmtPct = v => v === 0 ? "0%" : v >= 10 ? `${Math.round(v)}%` : `${+v.toPrecision(2)}%`;
  const sleep = ms => new Promise(res => setTimeout(res, ms));

  const MAX = 6;
  const MEASURES = ["stories", "outlets", "articles"];
  const SCALES = ["count", "share"];
  // Six line colours on white and six on the dark background (the image is
  // always drawn on white); text and gridline colours follow styles.css.
  const PALETTE = {
    light: ["#4d6d9a", "#c8553d", "#3f8f5f", "#8a5ea8", "#b8860b", "#2a9d8f"],
    dark: ["#7aa2d8", "#f08a70", "#6fc590", "#c39ae0", "#e0b544", "#5fd1c1"],
  };
  const dark = matchMedia("(prefers-color-scheme: dark)");
  const colours = () => PALETTE[dark.matches ? "dark" : "light"];
  const theme = () => { const cs = getComputedStyle(document.documentElement); return { ink: cs.getPropertyValue("--ink").trim(), muted: cs.getPropertyValue("--muted").trim(), line: cs.getPropertyValue("--line").trim() }; };
  const PRINT = { ink: "#1a1a1a", muted: "#666", line: "#ddd" };

  let loaded = { first: "2019-10-01", last: day(new Date()) };
  let measure = "stories", scale = "count";
  let chartOnly = false; // ?view=chart: the chart on its own, first on the page, with a link to the full page
  const setMeasure = m => { measure = MEASURES.includes(m) ? m : "stories"; document.querySelector(`input[name=measure][value=${measure}]`).checked = true; };
  const setScale = s => { scale = SCALES.includes(s) ? s : "count"; document.querySelector(`input[name=scale][value=${scale}]`).checked = true; };
  // What is drawn: the series (text, q, domain, counts per "YYYY-MM" as
  // [stories, outlets, articles], which windows are fetched) and, per
  // distinct source, the totals; `gen` tells stale responses from live ones.
  let state = null;
  let gen = 0;

  // ------------------------------------------------------------------ the form
  const rows = () => [...$("series").querySelectorAll("input")];
  function addRow(value = "") {
    if (rows().length >= MAX) return;
    const li = document.createElement("li");
    li.innerHTML = `<input type="text" name="s" maxlength="200" aria-label="Term"><button type="button" class="chip del" title="Remove this term">&times;</button>`;
    const input = li.querySelector("input");
    input.value = value;
    input.placeholder = rows().length ? "another term" : "a term, e.g. ukraine";
    $("series").appendChild(li);
    $("add").hidden = rows().length >= MAX;
    return input;
  }
  // "gaza site:bbc.com" is the words "gaza" on the source bbc.com;
  // "site:bbc.com,nytimes.com" or two site: terms name several sources,
  // anywhere in the text; `domain` is the list, comma-joined and sorted, as
  // the Worker takes it. A term with a site: and no words is everything
  // from those sources.
  function parseSeries(text) {
    const ds = new Set();
    const q = text.replace(/(?:^|\s)site:([a-z0-9.,-]+)(?=\s|$)/gi, (m, d) => { d.toLowerCase().split(",").filter(Boolean).forEach(x => ds.add(x)); return " "; }).replace(/\s+/g, " ").trim();
    return { text: text.replace(/\s+/g, " ").trim(), q, domain: [...ds].sort().join(",") };
  }
  const read = () => rows().map(i => parseSeries(i.value)).filter(s => s.text);

  function toUrl(series, view = chartOnly) {
    const p = new URLSearchParams();
    series.forEach(s => p.append("s", s.text));
    if (measure !== "stories") p.set("measure", measure);
    if (scale !== "count") p.set("scale", scale);
    if ($("from").value) p.set("from", $("from").value);
    if ($("to").value) p.set("to", $("to").value);
    if (view) p.set("view", "chart");
    return "?" + p;
  }
  function fromUrl() {
    const p = new URLSearchParams(location.search);
    const ss = p.getAll("s").map(t => t.trim()).filter(Boolean).slice(0, MAX);
    $("series").innerHTML = "";
    $("add").hidden = false;
    ss.forEach(addRow);
    while (rows().length < 2) addRow();
    setMeasure(p.get("measure"));
    setScale(p.get("scale"));
    chartOnly = p.get("view") === "chart";
    document.body.classList.toggle("chart-only", chartOnly);
    $("from").value = /^\d{4}-\d{2}-\d{2}$/.test(p.get("from") || "") ? p.get("from") : "";
    $("to").value = /^\d{4}-\d{2}-\d{2}$/.test(p.get("to") || "") ? p.get("to") : "";
    linkDates();
    return ss.length > 0;
  }

  // ----------------------------------------------------------------- windows
  // Every month the database holds, oldest first; the months in the date
  // range; and the twelve-month windows, newest first, cut exactly as the
  // search page cuts them so both pages hit the same cache entries.
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
  function span() {
    const f = ($("from").value || loaded.first).slice(0, 7), t = ($("to").value || loaded.last).slice(0, 7);
    return monthList().filter(m => m >= f && m <= t);
  }
  function windows() {
    const months = monthList(), sp = span(), out = [];
    if (!sp.length) return out;
    for (let i = months.length; i > 0; i -= 12) {
      const w = months.slice(Math.max(0, i - 12), i);
      if (w[w.length - 1] >= sp[0] && w[0] <= sp[sp.length - 1]) out.push(w);
    }
    return out;
  }

  // ---------------------------------------------------------------- fetching
  // Three requests in flight at a time. The Worker refuses an address that
  // sent too many in a minute (120; six terms over seven windows with their
  // totals is under fifty); a refused request is asked for once more after
  // the minute has turned, and nothing else goes out while it waits.
  const queue = [];
  let inFlight = 0, holdUntil = 0;
  const LANES = 3;
  function enqueue(t) { queue.push(t); pump(); }
  function pump() {
    while (inFlight < LANES && queue.length) {
      inFlight++;
      run(queue.shift()).finally(() => { inFlight--; pump(); });
    }
  }
  async function run(t) {
    if (t.gen !== gen) return;
    if (t.skip()) { t.done({ skipped: true }); return; }
    while (holdUntil > Date.now()) { render(); await sleep(1000); if (t.gen !== gen) return; }
    let r;
    try { const res = await fetch(t.url); r = await res.json(); r.status = res.status; }
    catch (e) { r = { error: "request failed", status: 0 }; }
    if (t.gen !== gen) return;
    if (r.rateLimited && !t.retried) { t.retried = true; holdUntil = Date.now() + 31000; queue.push(t); return; }
    t.done(r);
  }

  function draw(push) {
    const series = read();
    if (!series.length) return;
    if (push) history.pushState(null, "", toUrl(series));
    document.title = `${series.map(s => s.text).join(", ")} - News Headline Search`;
    $("examples").hidden = true;
    $("out").hidden = false;
    const key = series.map(s => s.text).join("\n");
    if (state && state.key === key) { schedule(); render(); return; } // the same terms: keep what is counted
    gen++;
    state = {
      key, gen,
      series: series.map((s, i) => ({ ...s, i, counts: new Map(), partial: new Set(), have: new Set(), pending: 0, failed: 0, limited: 0, error: s.q.length < 2 && !(s.q.length === 0 && s.domain) ? "needs at least two characters" : "" })),
      totals: new Map(),
    };
    statsReady.finally(() => { if (state.gen !== gen) return; schedule(); render(); });
  }

  // Ask for every window of every series not yet asked for, newest first,
  // the series side by side so a window fills for all of them together; and,
  // for a share, the totals of each distinct source (or of everything).
  function schedule() {
    const s = state;
    const fetched = { counts: new Map(), partial: new Set(), have: new Set(), pending: 0, failed: 0, limited: 0 };
    for (const w of windows()) {
      const wk = w[0], from = w[0] + "-01", to = monthEnd(w[w.length - 1]);
      for (const ser of s.series) {
        if (ser.error || ser.have.has(wk)) continue;
        ser.have.add(wk);
        ser.pending++;
        const q = new URLSearchParams({ mode: "word", from, to });
        if (ser.q) q.set("q", ser.q);
        if (ser.domain) q.set("domain", ser.domain);
        enqueue({ gen: s.gen, url: "/api/count?" + q, skip: () => !!ser.error, done: r => { ser.pending--; take(ser, w, r); } });
      }
      if (scale !== "share") continue;
      for (const d of new Set(s.series.filter(x => !x.error).map(x => x.domain))) {
        let t = s.totals.get(d);
        if (!t) { t = { ...fetched, counts: new Map(), partial: new Set(), have: new Set() }; s.totals.set(d, t); }
        if (t.have.has(wk)) continue;
        t.have.add(wk);
        t.pending++;
        const q = new URLSearchParams({ from, to });
        if (d) q.set("domain", d);
        enqueue({ gen: s.gen, url: "/api/totals?" + q, skip: () => false, done: r => { t.pending--; take(t, w, r); } });
      }
    }
  }
  // A window's answer into a series (or a totals) record. A 400 is the term
  // itself being refused (too many alternatives, no words), so the series is
  // marked and its other windows are skipped; any other failure is one
  // window, asked for again on the next schedule().
  function take(x, w, r) {
    if (r.skipped) { x.have.delete(w[0]); return; }
    if (r.error) {
      x.have.delete(w[0]);
      if (r.status === 400) x.error = r.error;
      else { x.failed++; if (r.rateLimited) x.limited++; }
    } else {
      for (const m of w) x.counts.set(m, [0, 0, 0]);
      for (const [m, a, b, c] of r.months) x.counts.set(m.slice(0, 7), [a, b, c]);
      if (r.partial) for (const m of w) x.partial.add(m);
    }
    render();
  }

  // ----------------------------------------------------------------- drawing
  function render() {
    const s = state;
    if (!s) return;
    const months = span();
    const k = MEASURES.indexOf(measure);
    const cols = colours();
    const value = (ser, m) => {
      const v = ser.counts.get(m);
      if (!v) return null;
      if (scale === "count") return v[k];
      const t = s.totals.get(ser.domain)?.counts.get(m);
      return t ? (t[k] ? v[k] / t[k] * 100 : 0) : null;
    };
    for (const ser of s.series) {
      ser.total = [0, 0, 0]; ser.tot = [0, 0, 0]; ser.max = 0; ser.peakMonth = null;
      for (const m of months) {
        const v = ser.counts.get(m);
        if (!v) continue;
        const t = s.totals.get(ser.domain)?.counts.get(m);
        for (let i = 0; i < 3; i++) { ser.total[i] += v[i]; if (t) ser.tot[i] += t[i]; }
        if (v[k] > ser.max) { ser.max = v[k]; ser.peakMonth = m; }
      }
      ser.vals = months.map(m => value(ser, m));
    }

    // The status line: progress, then what the lines are.
    const recs = [...s.series.filter(x => !x.error), ...(scale === "share" ? s.totals.values() : [])];
    const pending = recs.reduce((n, x) => n + x.pending, 0), asked = recs.reduce((n, x) => n + x.have.size, 0);
    const failed = recs.reduce((n, x) => n + x.failed, 0), limited = recs.reduce((n, x) => n + x.limited, 0);
    const partial = recs.some(x => months.some(m => x.partial.has(m)));
    let status;
    if (!months.length) status = "No months in that range.";
    else if (holdUntil > Date.now()) status = `Too many requests from here in a minute; the rest wait ${Math.ceil((holdUntil - Date.now()) / 1000)} s…`;
    else if (pending) status = `Counting… ${asked - pending} of ${asked} windows so far.`;
    else {
      status = `${what()}, ${fmtMonth(months[0])} to ${fmtMonth(months[months.length - 1])}.`;
      if (partial) status += " Months marked ~ hit the time limit, so their counts are low.";
      if (failed) status += ` ${failed} window${failed === 1 ? "" : "s"} could not be counted${limited ? " (too many requests from here in a minute; draw again in a minute to fill them in)" : ""}.`;
    }
    $("status").textContent = status;

    // The legend: each term, linked to its headlines, with its total over the span.
    $("legend").innerHTML = s.series.map((ser, i) => {
      const sw = `<i style="background:${cols[i]}"></i>`;
      if (ser.error) return `<li>${sw}<span class="t">${esc(ser.text)}</span> <span class="note">${esc(ser.error)}</span></li>`;
      const lp = new URLSearchParams({ count: 1 });
      if (ser.q) lp.set("q", ser.q);
      if (ser.domain) lp.set("domain", ser.domain);
      if (measure !== "stories") lp.set("measure", measure);
      const share = scale === "share" && ser.tot[k] ? `, ${fmtPct(ser.total[k] / ser.tot[k] * 100)} of ${whose(ser)} ${measure}` : "";
      const peak = ser.peakMonth ? `, most in ${fmtMonth(ser.peakMonth)} (${fmt(ser.max)})` : "";
      return `<li>${sw}<a class="t" href="./?${lp}" title="The headlines">${esc(ser.text)}</a> <span class="note">${fmt(ser.total[k])} ${measure}${share}${peak}</span></li>`;
    }).join("");

    $("chart-link").href = toUrl(s.series, !chartOnly);
    $("chart-link").textContent = chartOnly ? "See the full compare page" : "Chart on its own";

    // The chart, then the hover layer wired to it.
    const box = $("chart");
    const w = Math.max(280, box.clientWidth), h = w < 560 ? 200 : 260;
    const g = chartSvg(w, h, months, s.series, cols, theme(), true);
    box.innerHTML = g.svg;
    hover(g, months, s.series, cols);
    readout(null);

    const cell = (ser, m) => {
      const v = ser.counts.get(m);
      if (!v) return "";
      const tilde = ser.partial.has(m) ? "~" : "";
      if (scale === "share") { const x = value(ser, m); return x == null ? tilde + fmt(v[k]) : `${tilde}${fmtPct(x)} <span class="n">${fmt(v[k])}</span>`; }
      return tilde + fmt(v[k]);
    };
    $("table").innerHTML = `<tr><th>month</th>${s.series.map((ser, i) => `<th><i style="background:${cols[i]}"></i>${esc(ser.text)}</th>`).join("")}</tr>` +
      months.filter(m => s.series.some(ser => ser.counts.get(m))).map(m => `<tr><td>${fmtMonth(m)}</td>${s.series.map(ser => `<td>${cell(ser, m)}</td>`).join("")}</tr>`).join("");
  }
  // "Results per month", or "Results per month as a share of all stories that
  // month": the status line and the image's title. `whose`: whose headlines a
  // share is of ("all", "bbc.com's", "the 3 sources'").
  const what = () => scale === "share" ? `Results per month as a share of all ${measure} that month` : "Results per month";
  const whose = ser => { const ds = ser.domain ? ser.domain.split(",") : []; return !ds.length ? "all" : ds.length === 1 ? `${esc(ds[0])}'s` : `the ${ds.length} sources'`; };

  // Axis ticks: a step of 1, 2, 2.5 or 5 times a power of ten, four or five of them.
  function ticks(top) {
    if (!(top > 0)) return [0, 1];
    const raw = top / 4, p = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * p).find(v => v >= raw);
    const out = [];
    for (let v = 0; v < top + step - 1e-9; v += step) out.push(+v.toPrecision(12));
    return out;
  }
  const fmtTick = v => scale === "share" ? `${+v.toPrecision(3)}%` : fmtShort(v);

  // The chart as SVG markup, `w` by `h` pixels: gridlines with tick labels,
  // year (or month) labels along the bottom, one path per series with gaps
  // where a month is not counted yet, and, for the page, a hover layer (a
  // rule and a dot per series) over a capture rectangle. Returns the markup
  // and the geometry the hover needs.
  function chartSvg(w, h, months, series, cols, th, live) {
    const top = Math.max(0, ...series.flatMap(ser => ser.vals || []).filter(v => v != null));
    const tk = ticks(top), ymax = tk[tk.length - 1];
    const labels = tk.map(fmtTick);
    const padL = 10 + Math.max(...labels.map(l => l.length)) * 7.5, padR = 12, padT = 10, padB = 22;
    const iw = w - padL - padR, ih = h - padT - padB, n = months.length;
    const x = i => padL + (n > 1 ? i / (n - 1) * iw : iw / 2);
    const y = v => padT + ih - (ymax ? v / ymax * ih : 0);
    let out = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" font-size="11" role="img" aria-label="Line chart">`;
    tk.forEach((t, i) => {
      out += `<line x1="${padL}" x2="${w - padR}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}" stroke="${th.line}"/>`;
      out += `<text x="${padL - 5}" y="${(y(t) + 3.5).toFixed(1)}" text-anchor="end" fill="${th.muted}">${labels[i]}</text>`;
    });
    // Years when the span is long; else every third month, or every month.
    const step = n > 36 ? 12 : n > 12 ? 3 : 1;
    months.forEach((m, i) => {
      if (step === 12 ? !m.endsWith("-01") : i % step) return;
      const [yy, mm] = m.split("-");
      const label = step === 12 ? yy : `${MONTHS[mm - 1]} ${yy.slice(2)}`;
      const anchor = n > 1 && i === n - 1 ? "end" : n > 1 && i === 0 ? "start" : "middle";
      out += `<line x1="${x(i).toFixed(1)}" x2="${x(i).toFixed(1)}" y1="${padT + ih}" y2="${padT + ih + 4}" stroke="${th.muted}"/>`;
      out += `<text x="${x(i).toFixed(1)}" y="${h - 6}" text-anchor="${anchor}" fill="${th.muted}"${step === 12 && live ? ` class="yr" data-year="${yy}"` : ""}>${label}</text>`;
    });
    series.forEach((ser, i) => {
      const vals = ser.vals || [];
      let d = "", pen = false;
      vals.forEach((v, j) => {
        if (v == null) { pen = false; return; }
        d += `${pen ? "L" : "M"}${x(j).toFixed(1)} ${y(v).toFixed(1)}`;
        pen = true;
        if (vals[j - 1] == null && vals[j + 1] == null) out += `<circle cx="${x(j).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2" fill="${cols[i]}"/>`;
      });
      if (d) out += `<path d="${d}" fill="none" stroke="${cols[i]}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>`;
    });
    if (live) {
      out += `<g class="hv" visibility="hidden"><line y1="${padT}" y2="${padT + ih}" stroke="${th.muted}" stroke-dasharray="2 3"/>${series.map((_, i) => `<circle r="3" fill="${cols[i]}"/>`).join("")}</g>`;
      out += `<rect class="cap" x="${padL}" y="${padT}" width="${iw}" height="${ih}" fill="transparent"/>`;
    }
    return { svg: out + "</svg>", x, y, padL, iw };
  }

  // Hovering (or touching) the chart picks the nearest month: a rule, a dot
  // per series, and the month's numbers in the line under the chart.
  function hover(g, months, series, cols) {
    const svg = $("chart").querySelector("svg");
    const hv = svg.querySelector(".hv"), rule = hv.querySelector("line"), dots = hv.querySelectorAll("circle");
    const show = ev => {
      if (!months.length) return;
      const px = ev.clientX - svg.getBoundingClientRect().left;
      const i = Math.max(0, Math.min(months.length - 1, Math.round((px - g.padL) / g.iw * (months.length - 1))));
      hv.setAttribute("visibility", "visible");
      rule.setAttribute("x1", g.x(i)); rule.setAttribute("x2", g.x(i));
      series.forEach((ser, j) => {
        const v = ser.vals?.[i];
        if (v == null) dots[j].setAttribute("r", 0);
        else { dots[j].setAttribute("r", 3); dots[j].setAttribute("cx", g.x(i)); dots[j].setAttribute("cy", g.y(v)); }
      });
      readout(i, months, series, cols);
    };
    svg.addEventListener("pointermove", show);
    svg.addEventListener("pointerdown", show);
    svg.addEventListener("pointerleave", () => { hv.setAttribute("visibility", "hidden"); readout(null); });
  }
  function readout(i, months, series, cols) {
    if (i == null) { $("readout").textContent = "Hover or tap the chart for a month's numbers; click a year to narrow to it."; return; }
    const k = MEASURES.indexOf(measure), m = months[i];
    $("readout").innerHTML = `<b>${fmtMonth(m)}</b>` + series.map((ser, j) => {
      const v = ser.counts.get(m);
      if (!v) return "";
      const t = state.totals.get(ser.domain)?.counts.get(m);
      const extra = scale === "share" ? (t ? ` (${fmtPct(t[k] ? v[k] / t[k] * 100 : 0)} of ${fmtShort(t[k])})` : "") : "";
      return ` <span class="rd"><i style="background:${cols[j]}"></i>${esc(ser.text)} ${ser.partial.has(m) ? "~" : ""}${fmt(v[k])}${extra}</span>`;
    }).join("");
  }

  // --------------------------------------------------------------- the image
  // The chart drawn again on white, at twice the pixels, with a title, the
  // legend, and the page's address, so the picture says where it came from.
  async function saveImage() {
    const s = state;
    if (!s) return;
    const months = span();
    const cols = PALETTE.light, k = MEASURES.indexOf(measure);
    const W = 1100, M = 28, CH = 380;
    const legend = s.series.filter(x => !x.error).map((ser, i) => {
      const share = scale === "share" && ser.tot[k] ? `, ${fmtPct(ser.total[k] / ser.tot[k] * 100)} of ${whose(ser).replace(/&#39;/g, "'")} ${measure}` : "";
      return { c: cols[s.series.indexOf(ser)], text: `${ser.text}: ${fmt(ser.total[k])} ${measure}${share}${ser.peakMonth ? `, most in ${fmtMonth(ser.peakMonth)} (${fmt(ser.max)})` : ""}` };
    });
    const url = location.origin + location.pathname + location.search;
    const urlLines = url.match(/.{1,130}/g) || [url];
    const H = M + 26 + legend.length * 19 + 14 + CH + 12 + urlLines.length * 15 + 18 + M;
    let y = M + 18;
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><rect width="100%" height="100%" fill="#fff"/>`;
    svg += `<text x="${M}" y="${y}" font-size="17" fill="${PRINT.ink}">${esc(what())}, ${fmtMonth(months[0])} to ${fmtMonth(months[months.length - 1])}</text>`;
    y += 12;
    for (const l of legend) {
      y += 19;
      svg += `<rect x="${M}" y="${y - 10}" width="11" height="11" rx="2" fill="${l.c}"/><text x="${M + 17}" y="${y}" font-size="13" fill="${PRINT.ink}">${esc(l.text)}</text>`;
    }
    y += 14;
    const g = chartSvg(W - 2 * M, CH, months, s.series, cols, PRINT, false);
    svg += `<svg x="${M}" y="${y}" width="${W - 2 * M}" height="${CH}" overflow="visible">${g.svg.slice(g.svg.indexOf(">") + 1)}`;
    y += CH + 12;
    for (const l of urlLines) { y += 15; svg += `<text x="${M}" y="${y}" font-size="12" fill="${PRINT.muted}">${esc(l)}</text>`; }
    y += 18;
    svg += `<text x="${M}" y="${y}" font-size="12" fill="${PRINT.muted}">newsheadlinesearch.com · headlines from GDELT · drawn ${fmtDay(day(new Date()))}</text></svg>`;
    const blobUrl = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }));
    try {
      const img = new Image();
      img.src = blobUrl;
      await img.decode();
      const c = document.createElement("canvas");
      c.width = W * 2; c.height = H * 2;
      const ctx = c.getContext("2d");
      ctx.scale(2, 2);
      ctx.drawImage(img, 0, 0);
      const png = await new Promise(res => c.toBlob(res, "image/png"));
      const a = document.createElement("a");
      a.href = URL.createObjectURL(png);
      a.download = `headlines-${s.series.map(x => x.text).join("-").replace(/[^\p{L}\p{N}]+/gu, "-").slice(0, 80)}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    } catch (e) {
      $("status").textContent = "The image could not be drawn in this browser.";
    } finally {
      URL.revokeObjectURL(blobUrl);
    }
  }

  // ------------------------------------------------------------------ wiring
  $("f").onsubmit = e => { e.preventDefault(); draw(true); };
  $("add").onclick = () => { const i = addRow(); if (i) i.focus(); };
  $("series").addEventListener("click", ev => {
    const b = ev.target.closest(".del");
    if (!b) return;
    if (rows().length > 1) b.parentNode.remove(); else rows()[0].value = "";
    $("add").hidden = rows().length >= MAX;
  });
  const replaceUrl = () => { if (state) history.replaceState(null, "", toUrl(state.series)); };
  $("measures").addEventListener("change", ev => { setMeasure(ev.target.value); replaceUrl(); render(); });
  $("scales").addEventListener("change", ev => { setScale(ev.target.value); replaceUrl(); if (state) { schedule(); render(); } });
  // Dates: "to" cannot precede "from" (fixing one side moves the other), both
  // bounded to what is loaded once /api/stats answers.
  function linkDates(changed) {
    const f = $("from"), t = $("to");
    if (f.value && t.value && t.value < f.value) { if (changed === "from") t.value = f.value; else f.value = t.value; }
    t.min = f.value || f.min; f.max = t.value || t.max || "";
  }
  const redate = changed => { linkDates(changed); if (state) { history.pushState(null, "", toUrl(state.series)); schedule(); render(); } };
  $("from").addEventListener("change", () => redate("from"));
  $("to").addEventListener("change", () => redate("to"));
  $("clear-dates").onclick = () => { $("from").value = ""; $("to").value = ""; redate(); };
  $("chart").addEventListener("click", ev => {
    const t = ev.target.closest("text.yr");
    if (!t) return;
    $("from").value = `${t.dataset.year}-01-01`; $("to").value = `${t.dataset.year}-12-31`;
    redate();
  });
  $("png").onclick = saveImage;
  let resizeTimer;
  addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 150); });
  dark.addEventListener("change", render);

  // What is loaded: the intro sentence, the chart's span and the date pickers' bounds.
  const statsReady = fetch("/api/stats").then(res => res.json()).then(s => {
    if (s.error) throw new Error(s.error);
    loaded = { first: s.first.slice(0, 10), last: s.last.slice(0, 10) };
    $("stats").textContent = `Headlines from ${fmtDay(loaded.first)} to ${fmtDay(loaded.last)}.`;
    $("from").min = $("to").min = loaded.first; $("from").max = $("to").max = loaded.last; linkDates();
  }).catch(() => { $("stats").textContent = "Could not reach the database just now."; });

  window.onpopstate = () => {
    if (fromUrl()) draw(false);
    else { $("examples").hidden = false; $("out").hidden = true; document.title = "Compare terms - News Headline Search"; }
  };
  if (fromUrl()) draw(false);
})();
