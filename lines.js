// The line chart: the compare page's terms, and the search page's chart when
// it shows a share. The SVG is drawn here with its hover layer; each page
// writes its own words around it. Loaded before compare.js and app.js, which
// find it as the global `Lines`.
const Lines = (function () {
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // Six line colours on white and six on the dark background (the image is
  // always drawn on white); text, gridline and band colours follow styles.css.
  const PALETTE = {
    light: ["#4d6d9a", "#c8553d", "#3f8f5f", "#8a5ea8", "#b8860b", "#2a9d8f"],
    dark: ["#7aa2d8", "#f08a70", "#6fc590", "#c39ae0", "#e0b544", "#5fd1c1"],
  };
  const dark = matchMedia("(prefers-color-scheme: dark)");
  const colours = () => PALETTE[dark.matches ? "dark" : "light"];
  const theme = () => { const cs = getComputedStyle(document.documentElement), v = n => cs.getPropertyValue(n).trim(); return { ink: v("--ink"), muted: v("--muted"), line: v("--line"), band: v("--band") }; };

  // Axis ticks: a step of 1, 2, 2.5 or 5 times a power of ten, four or five of them.
  function ticks(top) {
    if (!(top > 0)) return [0, 1];
    const raw = top / 4, p = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * p).find(v => v >= raw);
    const out = [];
    for (let v = 0; v < top + step - 1e-9; v += step) out.push(+v.toPrecision(12));
    return out;
  }

  // The chart as SVG markup, `w` by `h` pixels: gridlines with tick labels
  // (written by `fmtTick`), year (or month) labels along the bottom, one path
  // per series from its `vals` (one per month, with gaps where a month is not
  // counted yet), an optional shaded `band` over the months from index
  // band[0] to band[1], and, for the page (`live`), a hover layer (a rule and
  // a dot per series) over a capture rectangle. Returns the markup and the
  // geometry the hover needs.
  function svg(w, h, months, series, { cols, th, live, fmtTick, band }) {
    const top = Math.max(0, ...series.flatMap(ser => ser.vals || []).filter(v => v != null));
    const tk = ticks(top), ymax = tk[tk.length - 1];
    const labels = tk.map(fmtTick);
    const padL = 10 + Math.max(...labels.map(l => l.length)) * 7.5, padR = 12, padT = 10, padB = 22;
    const iw = w - padL - padR, ih = h - padT - padB, n = months.length;
    const x = i => padL + (n > 1 ? i / (n - 1) * iw : iw / 2);
    const y = v => padT + ih - (ymax ? v / ymax * ih : 0);
    let out = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" font-size="11" role="img" aria-label="Line chart">`;
    if (band) {
      const half = n > 1 ? iw / (n - 1) / 2 : iw / 2;
      const x0 = Math.max(padL, x(band[0]) - half), x1 = Math.min(padL + iw, x(band[1]) + half);
      out += `<rect x="${x0.toFixed(1)}" y="${padT}" width="${(x1 - x0).toFixed(1)}" height="${ih}" fill="${th.band}"/>`;
    }
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

  // The index of the month nearest a pointer, from the drawn chart's geometry.
  const at = (el, g, n, clientX) => Math.max(0, Math.min(n - 1, Math.round((clientX - el.getBoundingClientRect().left - g.padL) / g.iw * (n - 1))));

  // Hovering (or touching) the chart picks the nearest month: a rule and a
  // dot per series; `pick` is told the month's index, or null when a mouse
  // leaves. A lifted finger also fires pointerleave, and until 2026-09-15 that
  // cleared a tapped month as soon as it was shown; now it stays until the
  // next tap.
  function hover(el, g, months, series, pick) {
    const hv = el.querySelector(".hv"), rule = hv.querySelector("line"), dots = hv.querySelectorAll("circle");
    const show = ev => {
      if (!months.length) return;
      const i = at(el, g, months.length, ev.clientX);
      hv.setAttribute("visibility", "visible");
      rule.setAttribute("x1", g.x(i)); rule.setAttribute("x2", g.x(i));
      series.forEach((ser, j) => {
        const v = ser.vals?.[i];
        if (v == null) dots[j].setAttribute("r", 0);
        else { dots[j].setAttribute("r", 3); dots[j].setAttribute("cx", g.x(i)); dots[j].setAttribute("cy", g.y(v)); }
      });
      pick(i);
    };
    el.addEventListener("pointermove", show);
    el.addEventListener("pointerdown", show);
    el.addEventListener("pointerleave", ev => { if (ev.pointerType !== "mouse") return; hv.setAttribute("visibility", "hidden"); pick(null); });
  }

  return { PALETTE, dark, colours, theme, svg, at, hover };
})();
