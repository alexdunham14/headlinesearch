// /api/search, /api/count, /api/totals and /api/stats: validate the query
// string, build one of three fixed ClickHouse queries with bound parameters
// (totals is the count query without its term), run it as the read-only
// `search` user, cache the answer at the edge. Everything else is a static
// asset. Nothing a visitor sends ever reaches ClickHouse as SQL.
//
// Secrets: CH_URL (e.g. http://db.example.com:8123; a hostname, not a bare
// IP, which Cloudflare refuses with error 1003), CH_PASSWORD (the `search`
// user, who reads `headlines` and `labels`). Binding:
// SEARCH_LIMIT (rate limit per IP, see wrangler.jsonc: 120 a minute, where a
// search with its chart is eight requests; the page retries what is refused).

// A page is PAGE distinct stories. The database returns rows (one per
// article URL), and a story syndicated to many sites, or to one site's many
// local editions, is the same headline over and over, sometimes with the
// site's own label after a pipe or a dash: the Worker collapses rows with
// the same story key (story_key() in scripts/schema.sql: the title with a
// known site label cut off) and, when more than half of a hundred rows
// collapsed, looks at up to WINDOW rows for that page instead. Pages
// continue from a timestamp cursor rather than a row offset (see runSearch).
const PAGE = 100;
const WINDOW = 1000;
const MAX_SKIP = 10000;
// The search user's profile caps queries at 20 s. Since the text index
// (2026-09-11) a word search over the whole archive is answered in a second
// or two; the limit is the backstop for the substring patterns the index
// cannot prune (see README).
const TIME_LIMIT = 20;
const FIRST_DAY = "2019-10-01";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!["/api/search", "/api/count", "/api/totals", "/api/stats"].includes(url.pathname)) return env.ASSETS.fetch(request);
    if (request.method !== "GET") return json({ error: "GET only" }, 405);
    if (url.pathname === "/api/stats") return stats(env, ctx, url);

    // Totals (compare mode's denominator) are the three counts a month with
    // no term: the count query, parsed and cached the same way, minus the term.
    const totals = url.pathname === "/api/totals";
    const count = url.pathname === "/api/count" || totals;
    let q;
    try {
      q = parse(url.searchParams, totals);
    } catch (e) {
      return json({ error: e.message }, 400);
    }
    if (count) { q.cursor = ""; q.skip = 0; } // a count is the whole range; no paging

    // Same parameters, same cache entry, whatever order or junk the URL had.
    const key = new Request(`${url.origin}${url.pathname}?${canonical(q)}`);
    const cache = caches.default;
    const hit = await cache.match(key);
    if (hit) return hit;

    const ip = request.headers.get("cf-connecting-ip") || "0";
    const { success } = await env.SEARCH_LIMIT.limit({ key: ip });
    if (!success) return json({ error: "Too many searches; wait a minute.", rateLimited: true }, 429);

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

// In word mode the query is alternatives separated by OR (upper case, on its
// own: "congo OR drc", "el nino OR la nina"), each a set of words that must
// all be present; `groups` holds them. Substring mode takes OR literally. A
// totals request has no query at all.
function parse(p, totals = false) {
  const q = totals ? "" : (p.get("q") || "").trim();
  if (!totals && q.length < 2) throw new Error("query must be at least two characters");
  if (q.length > 200) throw new Error("query too long");
  const mode = p.get("mode") === "substring" ? "substring" : "word";
  const split = (s) => s.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  const groups = mode === "word" ? q.split(/(?<=^|\s)OR(?=\s|$)/).map(split).filter((g) => g.length) : [split(q)];
  if (!totals && mode === "word" && groups.length === 0) throw new Error("no words to search for");
  if (groups.length > 8) throw new Error("at most eight alternatives");
  if (groups.some((g) => g.length > 8)) throw new Error("at most eight words");
  const from = day(p.get("from"), FIRST_DAY);
  const to = day(p.get("to"), "2100-01-01");
  if (from > to) throw new Error("from is after to");
  const domain = (p.get("domain") || "").trim().toLowerCase();
  if (domain.length > 100 || /[^a-z0-9.-]/.test(domain)) throw new Error("domain looks wrong");
  const sort = p.get("sort") === "oldest" ? "oldest" : "newest";
  // Where the previous page stopped: its last timestamp (before= going
  // newest-first, after= going oldest-first) and how many rows at that
  // timestamp it already showed.
  const cursor = stamp(p.get(sort === "oldest" ? "after" : "before"));
  const skip = cursor ? Math.min(Math.max(parseInt(p.get("skip") || "0", 10) || 0, 0), MAX_SKIP) : 0;
  return { q, mode, groups, from, to, domain, sort, cursor, skip, totals };
}

// A cursor timestamp is written compactly (20260911183000, GDELT's own
// form) and read back as ClickHouse's "2026-09-11 18:30:00".
function stamp(s) {
  if (!s) return "";
  const m = /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/.exec(s);
  if (!m || isNaN(Date.parse(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`))) throw new Error("before/after is a timestamp like 20260911183000");
  return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
}

function day(s, fallback) {
  if (!s) return fallback;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s) || isNaN(Date.parse(s))) throw new Error("dates are YYYY-MM-DD");
  return s;
}

// v is the response shape: bump it when the shape changes, so entries cached
// at the edge under the old shape (a day, for counts) are never served.
function canonical(q) {
  return new URLSearchParams({ v: 2, q: q.q, mode: q.mode, from: q.from, to: q.to, domain: q.domain, sort: q.sort, cursor: q.cursor, skip: q.skip }).toString();
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
//
// OR-groups: single-word alternatives are one hasAnyTokens, which the index
// answers as a union of posting lists; alternatives with more than one word
// are a hasAllTokens each, joined by OR, which the index also prunes on
// (measured 2026-09-12: "el nino OR la nina" over a year read a fifth of the
// granules in 0.3 s).
function where(q, params) {
  const conds = ["ts >= {from:Date}", "ts < {to:Date} + INTERVAL 1 DAY"];
  params.from = q.from;
  params.to = q.to;
  if (q.domain) {
    conds.push("domain = {domain:String}");
    params.domain = q.domain;
  }
  if (q.totals) {
    // no term: every headline in the range
  } else if (q.mode === "substring") {
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
    const groups = q.groups.map((g) => g.map(fold).filter(Boolean)).filter((g) => g.length);
    if (groups.length === 0) throw new Error("no words to search for");
    let n = 0;
    const bind = (w) => { params["w" + n] = w; return `{w${n++}:String}`; };
    const list = (g) => `[${g.map(bind).join(", ")}]`;
    if (groups.length === 1) conds.push(`hasAllTokens(${FOLD}, ${list(groups[0])})`);
    else if (groups.every((g) => g.length === 1)) conds.push(`hasAnyTokens(${FOLD}, ${list(groups.map((g) => g[0]))})`);
    else conds.push("(" + groups.map((g) => `hasAllTokens(${FOLD}, ${list(g)})`).join(" OR ") + ")");
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

// A page: rows newest-first (or oldest-first) from the cursor, rows with
// the same story key collapsed in order of first appearance, the first PAGE
// stories kept. Reading in key order and stopping at the limit is what makes
// a search cheap, so the collapse happens here rather than in the database:
// LIMIT 1 BY title would make ClickHouse read the title of every candidate
// row instead of the final hundred (measured 2026-09-11: five to twenty
// times slower on the common case). A hundred rows are fetched first; when
// fewer than half of them were distinct, up to WINDOW rows are fetched
// instead, so a story copied to a hundred local editions costs one more
// query rather than a page of one line.
//
// The next page starts where this one stopped: at the timestamp of the last
// row used, skipping the rows at that timestamp already shown (many rows
// share a timestamp; GDELT's are fifteen-minute batches). Unlike a row
// offset, that costs the same for page fifty as for page one, and rows
// inserted meanwhile do not shift the pages under the reader.
async function runSearch(env, q) {
  const params = { limit: PAGE, offset: q.skip };
  let conds = where(q, params);
  if (q.cursor) {
    conds += q.sort === "oldest" ? " AND ts >= {cursor:DateTime}" : " AND ts <= {cursor:DateTime}";
    params.cursor = q.cursor;
  }
  // The key is computed outside the limited query, for the page's rows only.
  const sql = `SELECT ts, domain, url, title, story_key(domain, title) AS key FROM (SELECT ts, domain, url, title FROM headlines WHERE ${conds}
    ORDER BY ts ${q.sort === "oldest" ? "ASC" : "DESC"} LIMIT {limit:UInt32} OFFSET {offset:UInt32}) FORMAT JSON`;
  let r = await clickhouse(env, sql, params, settings(q, { max_execution_time: TIME_LIMIT }));
  let elapsed = r.statistics.elapsed;
  let page = collapse(r.data, PAGE);
  if (r.data.length === PAGE && page.groups.length < PAGE / 2) {
    params.limit = WINDOW;
    r = await clickhouse(env, sql, params, settings(q, { max_execution_time: TIME_LIMIT }));
    elapsed += r.statistics.elapsed;
    page = collapse(r.data, PAGE);
  }
  let next = null;
  if (page.used && (page.used < r.data.length || r.data.length === params.limit)) {
    const last = r.data[page.used - 1].ts;
    let k = 0;
    for (let i = page.used - 1; i >= 0 && r.data[i].ts === last; i--) k++;
    if (last === q.cursor) k += q.skip;
    next = { [q.sort === "oldest" ? "after" : "before"]: last.replace(/\D/g, ""), skip: k };
  }
  return { rows: page.groups, used: page.used, next, elapsed, timedOut: r.statistics.elapsed >= TIME_LIMIT };
}

// Collapse rows with the same story key into one, in order of first
// appearance, up to `want` stories; `used` is how many rows that took, which
// is where the next page starts. The first copy's title is the one shown.
function collapse(rows, want) {
  const groups = [];
  const seen = new Map();
  let used = 0;
  for (const row of rows) {
    const key = row.key ?? row.title;
    let g = seen.get(key);
    if (!g) {
      if (groups.length === want) break;
      g = { ts: row.ts, domain: row.domain, url: row.url, title: row.title, n: 0, sites: new Set() };
      groups.push(g);
      seen.set(key, g);
    }
    g.n++;
    g.sites.add(row.domain);
    used++;
  }
  return { groups: groups.map((g) => ({ ...g, sites: g.sites.size })), used };
}

// Three counts a month from the copy flag (scripts/schema.sql): stories are
// first sightings (copy = 0), outlets first sightings per site (copy <= 1),
// articles every row. With a source, "stories" means that site's own first
// sightings (copy <= 1), since whether it was first anywhere is a different
// question; the page then does not offer outlets, which would be the same.
// A totals request (q.totals, compare mode's denominator) is the same query
// with no term: every headline in the range, by month.
async function runCount(env, q) {
  const params = {};
  const sql = `SELECT toStartOfMonth(ts) AS month, countIf(copy <= ${q.domain ? 1 : 0}) AS stories, countIf(copy <= 1) AS outlets, count() AS articles
    FROM headlines WHERE ${where(q, params)} GROUP BY month ORDER BY month FORMAT JSON`;
  const r = await clickhouse(env, sql, params, settings(q, { max_execution_time: TIME_LIMIT, timeout_overflow_mode: "break" }));
  const partial = r.statistics.elapsed >= TIME_LIMIT;
  return { months: r.data.map((x) => [x.month, Number(x.stories), Number(x.outlets), Number(x.articles)]), elapsed: r.statistics.elapsed, partial };
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
