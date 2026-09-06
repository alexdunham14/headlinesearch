// /api/search and /api/count: validate the query string, build one of two
// fixed ClickHouse queries with bound parameters, run it as the read-only
// `search` user, cache the answer at the edge. Everything else is a static
// asset. Nothing a visitor sends ever reaches ClickHouse as SQL.
//
// Secrets: CH_URL (e.g. http://1.2.3.4:8123), CH_PASSWORD. Binding:
// SEARCH_LIMIT (rate limit per IP, see wrangler.jsonc).

const PAGE = 100;
const MAX_OFFSET = 5000;
const FIRST_DAY = "2019-10-01";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname !== "/api/search" && url.pathname !== "/api/count") return env.ASSETS.fetch(request);
    if (request.method !== "GET") return json({ error: "GET only" }, 405);

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
      return json({ error: e.message }, 502);
    }
    const res = json(body, 200, `public, max-age=${count ? 86400 : 3600}`);
    ctx.waitUntil(cache.put(key, res.clone()));
    return res;
  },
};

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
  return { q, mode, words, from, to, domain, page };
}

function day(s, fallback) {
  if (!s) return fallback;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s) || isNaN(Date.parse(s))) throw new Error("dates are YYYY-MM-DD");
  return s;
}

function canonical(q) {
  return new URLSearchParams({ q: q.q, mode: q.mode, from: q.from, to: q.to, domain: q.domain, page: q.page }).toString();
}

// ----------------------------------------------------------------- queries

// The WHERE clause. Each mode pairs a fast case-insensitive test with an
// equivalent LIKE on lowerUTF8(title): the LIKE is what the trigram bloom
// index understands, and ClickHouse's short-circuit AND only evaluates the
// slower lowerUTF8 on rows the fast test already matched.
function where(q, params) {
  const conds = ["ts >= {from:Date}", "ts < {to:Date} + INTERVAL 1 DAY"];
  params.from = q.from;
  params.to = q.to;
  if (q.domain) {
    conds.push("domain = {domain:String}");
    params.domain = q.domain;
  }
  if (q.mode === "substring") {
    params.q = q.q;
    params.pat = "%" + q.q.toLowerCase().replace(/[\\%_]/g, "\\$&") + "%";
    conds.push("positionCaseInsensitiveUTF8(title, {q:String}) > 0", "lowerUTF8(title) LIKE {pat:String}");
  } else {
    q.words.forEach((w, i) => {
      const lw = w.toLowerCase();
      params["w" + i] = lw;
      params["p" + i] = "%" + lw.replace(/[\\%_]/g, "\\$&") + "%";
      // hasTokenCaseInsensitive folds ASCII only; go through lowerUTF8 for anything else.
      conds.push(/^[\x00-\x7f]*$/.test(w) ? `hasTokenCaseInsensitive(title, {w${i}:String})` : `hasToken(lowerUTF8(title), {w${i}:String})`);
      conds.push(`lowerUTF8(title) LIKE {p${i}:String}`);
    });
  }
  return conds.join(" AND ");
}

async function runSearch(env, q) {
  const params = { limit: PAGE, offset: (q.page - 1) * PAGE };
  const sql = `SELECT ts, domain, url, title FROM headlines WHERE ${where(q, params)}
    ORDER BY ts DESC LIMIT {limit:UInt32} OFFSET {offset:UInt32} FORMAT JSON`;
  const r = await clickhouse(env, sql, params, { max_execution_time: 10 });
  return { rows: r.data, elapsed: r.statistics.elapsed, timedOut: r.statistics.elapsed >= 10 };
}

async function runCount(env, q) {
  const params = {};
  const sql = `SELECT toStartOfMonth(ts) AS month, count() AS n FROM headlines WHERE ${where(q, params)}
    GROUP BY month ORDER BY month FORMAT JSON`;
  const settings = { max_execution_time: 20, timeout_overflow_mode: "break" };
  const r = await clickhouse(env, sql, params, settings);
  const partial = r.statistics.elapsed >= 20;
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
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text.includes("TIMEOUT_EXCEEDED") ? "search took too long; narrow the dates" : "database error");
  }
  return res.json();
}

function json(body, status = 200, cacheControl = "no-store") {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": cacheControl },
  });
}
