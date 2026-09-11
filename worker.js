// /api/search, /api/count and /api/stats: validate the query string, build
// one of three fixed ClickHouse queries with bound parameters, run it as the
// read-only `search` user, cache the answer at the edge. Everything else is a
// static asset. Nothing a visitor sends ever reaches ClickHouse as SQL.
//
// Secrets: CH_URL (e.g. http://db.example.com:8123; a hostname, not a bare
// IP, which Cloudflare refuses with error 1003), CH_PASSWORD. Binding:
// SEARCH_LIMIT (rate limit per IP, see wrangler.jsonc).

const PAGE = 100;
const MAX_OFFSET = 5000;
// The search user's profile caps queries at 20 s. Since the text index
// (2026-09-11) a word search over the whole archive is answered in a second
// or two; the limit is the backstop for the substring patterns the index
// cannot prune (see README).
const TIME_LIMIT = 20;
const FIRST_DAY = "2019-10-01";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!["/api/search", "/api/count", "/api/stats"].includes(url.pathname)) return env.ASSETS.fetch(request);
    if (request.method !== "GET") return json({ error: "GET only" }, 405);
    if (url.pathname === "/api/stats") return stats(env, ctx, url);

    let q;
    try {
      q = parse(url.searchParams);
    } catch (e) {
      return json({ error: e.message }, 400);
    }

    // Same parameters, same cache entry, whatever order or junk the URL had.
    const key = new Request(`${url.origin}${url.pathname}?${canonical(q)}`);
    const cache = caches.default;
    const hit = await cache.match(key);
    if (hit) return hit;

    const ip = request.headers.get("cf-connecting-ip") || "0";
    const { success } = await env.SEARCH_LIMIT.limit({ key: ip });
    if (!success) return json({ error: "Too many searches; wait a minute." }, 429);

    const count = url.pathname === "/api/count";
    let body;
    try {
      body = count ? await runCount(env, q) : await runSearch(env, q);
    } catch (e) {
      return json({ error: e.message, timeout: !!e.timeout }, 502);
    }
    const res = json(body, 200, `public, max-age=${count ? 86400 : 3600}`);
    ctx.waitUntil(cache.put(key, res.clone()));
    return res;
  },
};

// What is loaded: first and last timestamp and the row count. Takes no
// parameters, so one cache entry for everyone, an hour at a time.
async function stats(env, ctx, url) {
  const key = new Request(`${url.origin}/api/stats`);
  const cache = caches.default;
  const hit = await cache.match(key);
  if (hit) return hit;
  let body;
  try {
    const r = await clickhouse(env, "SELECT min(ts) AS first, max(ts) AS last, count() AS rows FROM headlines FORMAT JSON", {}, { max_execution_time: 10 });
    const x = r.data[0];
    body = { first: x.first, last: x.last, rows: Number(x.rows) };
  } catch (e) {
    return json({ error: e.message }, 502);
  }
  const res = json(body, 200, "public, max-age=3600");
  ctx.waitUntil(cache.put(key, res.clone()));
  return res;
}

// ---------------------------------------------------------------- parsing

function parse(p) {
  const q = (p.get("q") || "").trim();
  if (q.length < 2) throw new Error("query must be at least two characters");
  if (q.length > 200) throw new Error("query too long");
  const mode = p.get("mode") === "substring" ? "substring" : "word";
  const words = q.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  if (mode === "word" && words.length === 0) throw new Error("no words to search for");
  if (words.length > 8) throw new Error("at most eight words");
  const from = day(p.get("from"), FIRST_DAY);
  const to = day(p.get("to"), "2100-01-01");
  if (from > to) throw new Error("from is after to");
  const domain = (p.get("domain") || "").trim().toLowerCase();
  if (domain.length > 100 || /[^a-z0-9.-]/.test(domain)) throw new Error("domain looks wrong");
  const page = Math.min(Math.max(parseInt(p.get("page") || "1", 10) || 1, 1), MAX_OFFSET / PAGE);
  const sort = p.get("sort") === "oldest" ? "oldest" : "newest";
  return { q, mode, words, from, to, domain, page, sort };
}

function day(s, fallback) {
  if (!s) return fallback;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s) || isNaN(Date.parse(s))) throw new Error("dates are YYYY-MM-DD");
  return s;
}

function canonical(q) {
  return new URLSearchParams({ q: q.q, mode: q.mode, from: q.from, to: q.to, domain: q.domain, page: q.page, sort: q.sort }).toString();
}

// ----------------------------------------------------------------- queries

// The text index is built on this expression of title (scripts/schema.sql):
// lowercased, accents stripped (NFD, then the combining marks removed), the
// letters that do not decompose mapped by hand (ł ø đ ħ ŧ ı, ß æ œ), invalid
// UTF-8 repaired first because normalizeUTF8NFD throws on it. fold() below
// is the same thing in JavaScript for what a visitor types, and app.js has a
// copy for highlighting; the three must agree or a word is not found.
const FOLD = String.raw`replaceAll(replaceAll(replaceAll(translateUTF8(replaceRegexpAll(normalizeUTF8NFD(lowerUTF8(toValidUTF8(title))), '\\p{Mn}', ''), 'łøđħŧı', 'lodhti'), 'ß', 'ss'), 'æ', 'ae'), 'œ', 'oe')`;

function fold(s) {
  return s.toLowerCase().normalize("NFD").replace(/\p{Mn}/gu, "")
    .replace(/[łøđħŧı]/g, (c) => "lodhti"["łøđħŧı".indexOf(c)])
    .replace(/ß/g, "ss").replace(/æ/g, "ae").replace(/œ/g, "oe");
}

// The WHERE clause. Word mode is one hasAllTokens over the folded title,
// which the text index `tx` answers from its posting lists without reading
// the title column: the same tokenizer as the index, the folded words as a
// constant array of bound parameters, so "el nino" and "el niño" both find
// "El Niño" and "El Nino". Nothing else is tested per row on purpose:
// hasTokenCaseInsensitive is never index-aware, and an extra LIKE turns the
// index into a mere hint (measured 2026-09-11 on a one-year copy: 0.5 s for
// the hinted form, 0.03 s for this one).
//
// Substring mode keeps its exact, accent-sensitive meaning: a fast
// case-insensitive test, then the equivalent LIKE on lowerUTF8(title), which
// ClickHouse's short-circuit AND only evaluates on rows the fast test
// matched. Between them goes a LIKE on the folded title per folded fragment
// of four or more letters or digits: the text index answers a
// single-fragment LIKE by scanning its dictionary for tokens containing the
// fragment (text_index_like_min_pattern_length is 4), so "%raleigh%" and
// "%charter%" narrow the read to the granules holding both kinds of token
// before the exact pattern is checked. A title that contains the fragment
// contains its folded form once folded, so each fragment LIKE is implied by
// the full one and results are unchanged. A pattern with no such fragment
// gets no pruning and scans until the limit.
function where(q, params) {
  const conds = ["ts >= {from:Date}", "ts < {to:Date} + INTERVAL 1 DAY"];
  params.from = q.from;
  params.to = q.to;
  if (q.domain) {
    conds.push("domain = {domain:String}");
    params.domain = q.domain;
  }
  if (q.mode === "substring") {
    const lq = q.q.toLowerCase();
    params.q = q.q;
    params.pat = "%" + like(lq) + "%";
    conds.push("positionCaseInsensitiveUTF8(title, {q:String}) > 0");
    lq.split(/[^\p{L}\p{N}]+/u).map(fold).filter((f) => f.length >= 4).forEach((f, i) => {
      params["f" + i] = "%" + like(f) + "%";
      conds.push(`${FOLD} LIKE {f${i}:String}`);
    });
    conds.push("lowerUTF8(title) LIKE {pat:String}");
  } else {
    const words = q.words.map(fold).filter(Boolean);
    if (words.length === 0) throw new Error("no words to search for");
    words.forEach((w, i) => (params["w" + i] = w));
    conds.push(`hasAllTokens(${FOLD}, [${words.map((_, i) => `{w${i}:String}`).join(", ")}])`);
  }
  return conds.join(" AND ");
}

function like(s) {
  return s.replace(/[\\%_]/g, "\\$&");
}

// A source filter is served by the by_domain projection, and projections are
// switched on only then: left to itself ClickHouse picks by_domain for a
// whole-archive search too, because without a usable index the table looks
// like a full scan and the projection has slightly fewer marks, and then it
// cannot read newest-first and scans until the limit (measured 2026-09-11:
// "cricket" went from 1 s to a timeout). The reverse trap exists too: with
// the text index usable, a source query looks cheap on the table (the index
// names the granules with the word) and ClickHouse skips the projection,
// then reads the domain column of every such granule ("hurricane" on
// irishtimes.com: 179M rows, timeout). So a source query also tells
// ClickHouse to ignore the text index; on the projection hasAllTokens runs
// as a plain function over that site's rows, about two seconds.
function settings(q, extra) {
  const s = { ...extra, optimize_use_projections: q.domain ? 1 : 0 };
  if (q.domain) s.ignore_data_skipping_indices = "tx";
  return s;
}

async function runSearch(env, q) {
  const params = { limit: PAGE, offset: (q.page - 1) * PAGE };
  // Oldest-first reads the earliest granules first and stops at the limit just
  // as newest-first does, so it costs the same; it is how you find when a
  // phrase first turned up.
  const sql = `SELECT ts, domain, url, title FROM headlines WHERE ${where(q, params)}
    ORDER BY ts ${q.sort === "oldest" ? "ASC" : "DESC"} LIMIT {limit:UInt32} OFFSET {offset:UInt32} FORMAT JSON`;
  const r = await clickhouse(env, sql, params, settings(q, { max_execution_time: TIME_LIMIT }));
  return { rows: r.data, elapsed: r.statistics.elapsed, timedOut: r.statistics.elapsed >= TIME_LIMIT };
}

async function runCount(env, q) {
  const params = {};
  const sql = `SELECT toStartOfMonth(ts) AS month, count() AS n FROM headlines WHERE ${where(q, params)}
    GROUP BY month ORDER BY month FORMAT JSON`;
  const r = await clickhouse(env, sql, params, settings(q, { max_execution_time: TIME_LIMIT, timeout_overflow_mode: "break" }));
  const partial = r.statistics.elapsed >= TIME_LIMIT;
  return { months: r.data.map((x) => [x.month, Number(x.n)]), elapsed: r.statistics.elapsed, partial };
}

async function clickhouse(env, sql, params, settings) {
  const u = new URL(env.CH_URL);
  for (const [k, v] of Object.entries(params)) u.searchParams.set("param_" + k, String(v));
  for (const [k, v] of Object.entries(settings)) u.searchParams.set(k, String(v));
  const res = await fetch(u, {
    method: "POST",
    body: sql,
    headers: { "X-ClickHouse-User": "search", "X-ClickHouse-Key": env.CH_PASSWORD },
    signal: AbortSignal.timeout(30000),
  });
  const text = await res.text();
  // A count that runs out of its budget before it has produced anything comes
  // back as a 200 with an empty body (timeout_overflow_mode=break, seen at the
  // 20 s cap on a cold server), and an error after the headers are sent is
  // appended to the body. Neither is JSON; both are failures.
  if (res.ok) {
    try {
      return JSON.parse(text);
    } catch (e) {}
  }
  console.error("clickhouse", res.status, text.length, text.slice(0, 300));
  const timeout = !text.trim() || text.includes("TIMEOUT_EXCEEDED");
  const e = new Error(timeout ? `This search would take more than ${TIME_LIMIT} seconds over the whole archive.` : "database error");
  e.timeout = timeout;
  throw e;
}

function json(body, status = 200, cacheControl = "no-store") {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": cacheControl },
  });
}
