#!/usr/bin/env python3
"""Collect post titles from the blogging platforms that publish their own
lists, into a database of their own. Stdlib only; the fetching, robots and
insert code is scripts/collect.py's.

Why: ideas/creator-agenda.md and sessions/2026-09-13-* in the root repo.
Much of the deep work people do ends up on blogs, and for two platforms no
writer list has to be maintained because the platform publishes one:

- Substack. substack.com/sitemap.xml (listed in its robots.txt) indexes
  every publication on a substack.com subdomain, about 240,000, each with
  the time of its last post; 500 of them post in any hour, 15,000 in a day.
  Each publication's public archive API (/api/v1/archive?sort=new, the JSON
  its own archive page uses; not disallowed by robots.txt) gives the title,
  subtitle, time, type, audience (free or paid), language, author, word
  count and reactions of its posts, 23 on the first page and 50 a page
  after, back to the first post. Publications on their own domain (the
  biggest ones: Slow Boring, Astral Codex Ten, The Bulwark) are not in the
  index; their subdomain redirects. Substack's category leaderboards
  (/api/v1/category/public/ID/all, 33 categories, about 22 pages of 25)
  list them with their domain, so a weekly sweep of the leaderboards adds
  them, and they are polled through the archive API's ETag (a 304 when
  nothing changed) instead of the index.
- Medium. medium.com/sitemap/sitemap.xml indexes one file per day of every
  post, back to 2012, about 10,000 a day; the file for a day appears the
  next morning. The only title in it is the URL slug (lower case, no
  punctuation, non-Latin scripts percent-encoded), so Medium titles are
  stored degraded, marked kind = slug, and the post's time is the day.
- WordPress.com. The public REST API's tag streams (read/tags/TAG/posts),
  40 a page, paged back by date: slices, not a firehose, and the host's
  robots.txt turns every bot away from everything although the API's own
  terms invite apps. Off until Alex decides (scripts/blogs.json).

What it keeps is separate from the news collector's data (`collect`) and
the site's (`default`): database `blogs`, tables in scripts/blogs_schema.sql,
the same ClickHouse user as the news collector, the same hourly timer
shape (server/blogs.timer). Nothing on the site reads it. Every row
carries the platform, the platform's name for the publication, the
endpoint it came from and the time it was first seen, so the rows can be
told from GDELT's and the news collector's forever.

Manners, as in collect.py: a truthful User-Agent naming the site; robots.txt
read with it and obeyed (the platform serves one file for every Substack
subdomain, so it is read once a run from substack.com and applied to all
of them; a custom domain's own file is read and its answer kept for a day);
conditional requests wherever the platform answers 304; a global rate
limit per platform (scripts/blogs.json, half a request a second for
Substack, which answers 429 to about one request in twenty-five at one a
second; twenty 429s within one batch of 200 end the run, the rest of the
queue waiting for next hour)
with a few requests in flight; no retries inside a run; nothing that gets
round a wall: an invitation-only publication answers 403 and is marked
blocked and left alone.

Titles change, and the archive API and the sitemap show the current title
in place, so the unit stored is (platform, site, url, kind, title) with a
version, exactly as in collect.headlines: version 1 is the title as first
seen, later versions are the edits, timed by seen.

  scripts/blogs.py run [--platform substack|medium|wordpress] [--dry-run] [--limit N]
  scripts/blogs.py site SUBDOMAIN|URL           # one Substack publication's first archive page, printed
  scripts/blogs.py leaderboards [--dry-run]      # sweep Substack's category leaderboards into blogs.sites
  scripts/blogs.py history substack [--since 2025-09-13] [--from 2018-01-01] [--limit N] [--unfetched] [--rate R]
  scripts/blogs.py history medium --from 2019-01-01 [--to 2026-09-01] [--limit N]

`run` is what the timer calls: Substack's index every run, the leaderboard
sweep when a week has passed, Medium's index when its interval has passed,
WordPress if enabled. The history walks are run by hand under nohup on
the box and are resumable (blogs.history, blogs.files).

Environment: CH_URL (default http://127.0.0.1:8123), CH_USER, CH_PASSWORD.
"""

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402
from collect import ch, fetch, classify, clean_title, norm_url, parse_date, utcnow  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "blogs.json")
ARCHIVE_PAGE = 50            # what the archive API allows; the first page answers 23 whatever is asked
MAX_HISTORY_PAGES = 400      # 20,000 posts: a guard, not a target
LEADERBOARD_MAX_PAGES = 60


# ---------------------------------------------------------------- utilities

RECOVER_AFTER = 300     # seconds without a 429 before the pace comes back a step
SLOW_WINDOW = 10        # 429s within this many seconds of the last slow-down count as the same one
MAX_INTERVAL = 8.0      # seconds between requests at the slowest


class Limiter:
    """A global pace for one platform: at most `per_second` requests a
    second across every thread, evenly spaced. A 429 halves the pace
    (several within ten seconds count once, since four workers can meet
    the same limit together); every five minutes without another it comes
    back a step, to the configured pace at most. Changes are printed, so
    the journal shows them."""

    def __init__(self, per_second, name=""):
        self.base = 1.0 / max(per_second, 0.01)
        self.interval = self.base
        self.lock = threading.Lock()
        self.next = 0.0
        self.slowed_at = 0.0
        self.name = name

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.interval > self.base and now - self.slowed_at > RECOVER_AFTER:
                self.interval = max(self.base, self.interval / 2)
                self.slowed_at = now
                print(f"  {self.name} pace back to one request per {self.interval:.3g} s", flush=True)
            t = max(now, self.next)
            self.next = t + self.interval
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def slow(self):
        """Halve the pace (a 429 was seen)."""
        with self.lock:
            now = time.monotonic()
            if now - self.slowed_at < SLOW_WINDOW:
                return
            self.interval = min(self.interval * 2, MAX_INTERVAL)
            self.slowed_at = now
            print(f"  {self.name} 429: pace down to one request per {self.interval:.3g} s", flush=True)


def fmt(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")


def fmt_ms(d):
    return d.strftime("%Y-%m-%d %H:%M:%S.") + f"{d.microsecond // 1000:03d}"


def parse_iso(s):
    """ISO 8601 with fractional seconds and Z, to a naive UTC datetime with
    the milliseconds kept, or None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return parse_date(s)
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def load_config(path=CONFIG_PATH):
    with open(path) as fh:
        return json.load(fh)


def host_of(url):
    return urllib.parse.urlsplit(url).netloc.lower()


def json_body(f):
    try:
        return json.loads(f.body.decode("utf-8", "replace"))
    except ValueError:
        return None


EN_WORDS = set("""the a an of to in is are and for on with how why what your you my this that it its not from
by at be as or we i can do will new about vs into than more most all when who one our their has have was
were up out no get make just over after before first best top guide review tips things ways should could
would every don't dont without between through""".split())
NOT_EN = set("""yang dan dengan untuk tidak ini itu dari adalah kita saya que con una las los del como para
uma não você dos das der und ist mit für nicht eine les des est une pour dans che per non gli sono della
och att för är med ile bir için ve bu och""".split())


def guess_lang(title):
    """'en' if the title carries English function words and no marker of
    another language, else ''. A hint for Medium, whose sitemap says
    nothing about language; Substack states it."""
    words = set(re.findall(r"[a-zà-ÿ']+", title.lower()))
    if words & NOT_EN:
        return ""
    return "en" if words & EN_WORDS else ""


# ----------------------------------------------------------------- database

def rows_to_json(rows):
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode()


def insert_new_sql(staging="blogs.staging"):
    """The insert of a staged batch: every (platform, site, url, kind, title)
    not yet in posts, numbered after the versions already there."""
    cols = ("ts, platform, site, domain, url, title, seen, version, kind, source, subtitle, type, audience, "
            "lang, author, wordcount, reactions, comments, restacks, post_id, tags")
    return f"""
INSERT INTO blogs.posts ({cols})
SELECT s.ts, s.platform, s.site, s.domain, s.url, s.title, s.seen,
       toUInt16(e.n + row_number() OVER (PARTITION BY s.platform, s.site, s.url, s.kind ORDER BY s.seen, s.ts, s.title)) AS version,
       s.kind, s.source, s.subtitle, s.type, s.audience, s.lang, s.author, s.wordcount, s.reactions, s.comments,
       s.restacks, s.post_id, s.tags
FROM {staging} AS s
LEFT JOIN (
  SELECT platform, site, url, kind, count() AS n, groupArray(title) AS titles
  FROM blogs.posts
  WHERE (platform, site, url) IN (SELECT platform, site, url FROM {staging})
  GROUP BY platform, site, url, kind
) AS e ON s.platform = e.platform AND s.site = e.site AND s.url = e.url AND s.kind = e.kind
WHERE NOT has(e.titles, s.title)
"""


def store_posts(rows, staging="blogs.staging"):
    """Stage and insert what is new. Returns nothing; counts are read back
    by `seen` afterwards."""
    if not rows:
        return
    ch(f"TRUNCATE TABLE {staging}")
    ch(f"INSERT INTO {staging} FORMAT JSONEachRow", rows_to_json(rows))
    ch(insert_new_sql(staging))
    ch(f"TRUNCATE TABLE {staging}")


def sql_str(v):
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def counts_by_site(platform, seen, sites=None):
    """New rows and edits stored with this run's `seen`, per site; `sites`
    narrows it to a batch."""
    new, changed = defaultdict(int), defaultdict(int)
    where = f" AND site IN ({', '.join(sql_str(x) for x in sites)})" if sites else ""
    q = (f"SELECT site, count(), countIf(version > 1) FROM blogs.posts "
         f"WHERE platform = '{platform}' AND seen = '{fmt(seen)}'{where} GROUP BY site FORMAT TSV")
    for line in ch(q).splitlines():
        site, n, c = line.split("\t")
        new[site], changed[site] = int(n), int(c)
    return new, changed


def conditional_headers(platform):
    """The ETag and Last-Modified each index or file answered with last
    time, so that an unchanged one costs a 304."""
    out = {}
    q = (f"SELECT target, argMaxIf(etag, ts, etag != ''), argMaxIf(last_modified, ts, last_modified != '') "
         f"FROM blogs.fetches WHERE platform = '{platform}' AND ts > now() - INTERVAL 30 DAY AND label IN ('ok', '304') "
         f"GROUP BY target FORMAT TSV")
    for line in ch(q).splitlines():
        target, etag, lm = (line.split("\t") + ["", ""])[:3]
        out[target] = (etag, lm)
    return out


def last_fetch(platform, kind):
    """When a fetch of this kind last succeeded, or None."""
    q = (f"SELECT max(ts) FROM blogs.fetches WHERE platform = '{platform}' AND kind = '{kind}' "
         f"AND label IN ('ok', '304') FORMAT TSV")
    s = ch(q).strip()
    return parse_date(s) if s and not s.startswith("1970") else None


def log_fetches(rows):
    if rows:
        ch("INSERT INTO blogs.fetches FORMAT JSONEachRow", rows_to_json(rows))


SITE_COLS = ["platform", "site", "domain", "base_url", "lastmod", "seen", "listed", "category", "name", "language",
             "subscribers", "first_post", "etag", "status", "fetched", "robots", "robots_checked"]


class Site:
    """What the run needs to know about every listed publication, kept
    small because Substack lists 240,000 of them."""
    __slots__ = ("lastmod", "domain", "base_url", "status", "fetched")

    def __init__(self, lastmod, domain, base_url, status, fetched):
        self.lastmod, self.domain, self.base_url, self.status, self.fetched = lastmod, domain, base_url, status, fetched


def load_sites(platform):
    """blogs.sites for a platform, latest row per site, slim: {site: Site}."""
    q = (f"SELECT site, lastmod, domain, base_url, status, fetched FROM blogs.sites FINAL "
         f"WHERE platform = '{platform}' FORMAT TSVRaw")
    out = {}
    for line in ch(q).splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        site, lastmod, domain, base_url, status, fetched = parts
        out[site] = Site(parse_iso(lastmod) or dt.datetime(1970, 1, 1), domain, base_url, status,
                         parse_date(fetched) or dt.datetime(1970, 1, 1))
    return out


def load_site_rows(platform, sites):
    """The full blogs.sites rows of the named sites, as dicts, through the
    queue table (a list of thousands does not fit a query URL)."""
    out = {}
    sites = list(sites)
    if not sites:
        return out
    ch("TRUNCATE TABLE blogs.queue")
    ch("INSERT INTO blogs.queue FORMAT JSONEachRow", rows_to_json([{"site": s} for s in sites]))
    q = (f"SELECT {', '.join(SITE_COLS)} FROM blogs.sites FINAL WHERE platform = '{platform}' "
         f"AND site IN (SELECT site FROM blogs.queue) FORMAT JSONEachRow")
    for line in ch(q).splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        row["lastmod"] = parse_iso(row["lastmod"]) or dt.datetime(1970, 1, 1)
        row["fetched"] = parse_date(row["fetched"]) or dt.datetime(1970, 1, 1)
        row["robots_checked"] = parse_date(row["robots_checked"]) or dt.datetime(1970, 1, 1)
        row["subscribers"] = int(row["subscribers"] or 0)
        out[row["site"]] = row
    ch("TRUNCATE TABLE blogs.queue")
    return out


def site_row(platform, site, **kw):
    """A complete blogs.sites row with defaults, for a ReplacingMergeTree
    that keeps whole rows."""
    row = {"platform": platform, "site": site, "domain": "", "base_url": "", "lastmod": "1970-01-01 00:00:00.000",
           "seen": fmt(utcnow()), "listed": "", "category": "", "name": "", "language": "", "subscribers": 0,
           "first_post": "1970-01-01", "etag": "", "status": "", "fetched": "1970-01-01 00:00:00", "robots": "",
           "robots_checked": "1970-01-01 00:00:00"}
    row.update(kw)
    for k in ("lastmod",):
        if isinstance(row[k], dt.datetime):
            row[k] = fmt_ms(row[k])
    for k in ("fetched", "robots_checked", "seen"):
        if isinstance(row[k], dt.datetime):
            row[k] = fmt(row[k])
    if isinstance(row["first_post"], dt.datetime):
        row["first_post"] = row["first_post"].strftime("%Y-%m-%d") if row["first_post"].year >= 1970 else "1970-01-01"
    if not re.match(r"^\d{4}-\d\d-\d\d$", str(row["first_post"])):
        row["first_post"] = "1970-01-01"
    return row


def store_sites(rows):
    if rows:
        ch("INSERT INTO blogs.sites FORMAT JSONEachRow", rows_to_json(rows))


# ------------------------------------------------------------------- robots

class PlatformRobots(collect.Robots):
    """collect.Robots, with two additions: every *.substack.com host is
    answered by substack.com's file, read once a run (the platform serves
    the same file on every subdomain), and a host's answer can be seeded
    from blogs.sites and kept for a day."""

    def __init__(self, shared=None):
        super().__init__()
        self.shared = shared or {}       # suffix -> host whose file answers for it
        self.seeded = {}                 # host -> (allowed, checked)
        self.checked = {}                # host -> datetime of a fresh check this run

    def allowed(self, url):
        host = host_of(url)
        for suffix, canon in self.shared.items():
            if host.endswith(suffix) and host != canon:
                return self._via(canon, url)
        seed = self.seeded.get(host)
        if seed and host not in self.cache and (utcnow() - seed[1]) < dt.timedelta(hours=24):
            return seed[0]
        if host not in self.cache:
            self.checked[host] = utcnow()
        return super().allowed(url)

    def _via(self, canon, url):
        """Judge `url` by `canon`'s robots.txt."""
        if canon not in self.cache:
            self.cache[canon] = self._load(canon, "https://" + canon + "/")
        rp = self.cache[canon]
        if rp is None:
            return False
        if rp is True:
            return True
        return rp.can_fetch(collect.UA_TOKEN, url)


# ----------------------------------------------------------------- substack

def substack_index(cfg, cond, log, seen):
    """The platform's list: {subdomain: lastmod datetime}. None if every
    index file answered 304 (nothing changed) or the index could not be
    read; the fetch log gets a row per file either way."""
    f = fetch(cfg["index"])
    label = classify(f)
    log.append(fetch_row("substack", cfg["index"], "index", label, f, 0, seen))
    if label != "ok":
        return None
    files = re.findall(r"<loc>(https://substack\.com/sitemap-tt-index[^<]*)</loc>", f.body.decode("utf-8", "replace"))
    if not files:
        return None
    sites, all_304 = {}, True
    for url in files:
        etag, lm = cond.get(url, ("", ""))
        g = fetch(url, etag, lm, timeout=120)
        label = classify(g)
        n = 0
        if label == "ok":
            all_304 = False
            body = g.body.decode("utf-8", "replace")
            for m in re.finditer(r"<loc>https://([a-z0-9-]+)\.substack\.com/sitemap\.xml</loc>(?:<lastmod>([^<]*)</lastmod>)?", body):
                d = parse_iso(m.group(2))
                if d:
                    sites[m.group(1)] = d
                    n += 1
        elif label != "304":
            all_304 = False
        log.append(fetch_row("substack", url, "index", label, g, n, seen))
    if all_304:
        return None
    return sites


def fetch_row(platform, target, kind, label, f, items, seen, new=0, changed=0):
    return {"ts": fmt(seen), "platform": platform, "target": target[:500], "kind": kind, "label": label,
            "http": f.status, "bytes": len(f.body), "items": items, "new": new, "changed": changed, "ms": f.ms,
            "etag": (f.etag or "")[:200], "last_modified": (f.last_modified or "")[:100], "error": (f.error or "")[:300]}


def archive_url(base_url, offset=0, limit=ARCHIVE_PAGE):
    return f"{base_url.rstrip('/')}/api/v1/archive?sort=new&offset={offset}&limit={limit}"


def archive_posts(base_url, posts, site, seen, source):
    """Rows for blogs.staging from one archive API page."""
    rows = []
    for p in posts or []:
        if not isinstance(p, dict):
            continue
        url = norm_url(p.get("canonical_url") or "")
        title = clean_title(p.get("title") or "")
        ts = parse_iso(p.get("post_date")) or seen
        if not url or not title:
            continue
        reactions = p.get("reactions") or {}
        bylines = p.get("publishedBylines") or []
        rows.append({
            "ts": fmt(ts), "platform": "substack", "site": site, "domain": host_of(url), "url": url, "title": title,
            "seen": fmt(seen), "kind": "archive", "source": source,
            "subtitle": clean_title(p.get("subtitle") or "")[:500], "type": (p.get("type") or "")[:40],
            "audience": (p.get("audience") or "")[:40], "lang": (p.get("language") or "")[:10],
            "author": clean_title((bylines[0].get("name") if bylines and isinstance(bylines[0], dict) else "") or "")[:200],
            # The columns are unsigned; Substack has answered a negative
            # reactions total (2026-09-14, one post in 700,000, a lost batch).
            "wordcount": max(0, int(p.get("wordcount") or 0)),
            "reactions": max(0, int(sum(v for v in reactions.values() if isinstance(v, int)))) if isinstance(reactions, dict) else 0,
            "comments": max(0, int(p.get("comment_count") or 0)), "restacks": max(0, int(p.get("restacks") or 0)),
            "post_id": str(p.get("id") or ""), "tags": [t.get("name", "") for t in (p.get("postTags") or []) if isinstance(t, dict)][:20],
        })
    return rows


def read_archive(base_url, offset, limiter, robots, etag=""):
    """One archive page: (label, posts, fetched). robots is checked on the
    base URL's host; a redirect to a custom domain is followed as it is."""
    url = archive_url(base_url, offset)
    if robots is not None and not robots.allowed(url):
        return ("robots", [], collect.Fetched(url=url))
    limiter.wait()
    f = fetch(url, etag=etag)
    label = classify(f)
    if label == "ok":
        d = json_body(f)
        if d is None or not isinstance(d, list):
            return ("parse", [], f)
        return ("ok", d, f)
    if f.status == 403 and b"invited" in f.body[:300]:
        return ("blocked", [], f)
    return (label, [], f)


def poll_site(site, row, since, limiter, robots, seen, pages_per_site, mode):
    """Fetch one publication's newest posts from its archive API. mode is
    'index' (the platform's index said it changed) or 'custom' (a
    publication on its own domain, which the index does not track: the
    first page is asked for with the ETag stored last time, and a 304
    means nothing new). Returns (label, rows, log_rows, updates for the
    site row)."""
    base = row.get("base_url") or f"https://{site}.substack.com"
    log, updates = [], {}
    rows, offset, pages, newest = [], 0, 0, row.get("lastmod") or dt.datetime(1970, 1, 1)
    source = archive_url(base, 0, 0).split("?")[0]
    while pages < pages_per_site:
        etag = row.get("etag", "") if mode == "custom" and offset == 0 else ""
        label, posts, f = read_archive(base, offset, limiter, robots, etag=etag)
        log.append(fetch_row("substack", site, "archive", label, f, len(posts), seen))
        if label == "304":
            return ("304", [], log, {"status": "304"})
        if label != "ok":
            return (label, rows, log, {"status": label})
        if offset == 0:
            if mode == "custom":
                updates["etag"] = f.etag or ""
            if f.url and host_of(f.url) != host_of(base):
                # The subdomain redirected: the publication moved to its own domain.
                updates["domain"] = host_of(f.url)
                updates["base_url"] = f"https://{host_of(f.url)}"
        page = archive_posts(base, posts, site, seen, source)
        rows.extend(page)
        pages += 1
        if not posts:
            break
        dates = [parse_iso(p.get("post_date")) for p in posts if isinstance(p, dict)]
        dates = [d for d in dates if d]
        if dates:
            newest = max(newest, max(dates))
        oldest = min(dates) if dates else None
        # Page on only when the whole page is newer than what we had.
        if oldest is None or oldest <= since or len(posts) < 10:
            break
        offset += len(posts)
    updates.update({"status": "ok", "lastmod": newest, "fetched": seen})
    return ("ok", rows, log, updates)


def run_substack(cfg, dry_run=False, limit=None):
    seen = utcnow()
    state = {} if dry_run else load_sites("substack")
    cond = {} if dry_run else conditional_headers("substack")
    log = []
    index = substack_index(cfg, cond, log, seen)
    horizon = seen - dt.timedelta(days=cfg.get("first_run_days", 1))
    queue, new_rows = [], []
    if index is not None:
        for site, lastmod in index.items():
            st = state.get(site)
            if st is None:
                if lastmod > horizon:
                    queue.append((site, "index", lastmod))
                else:
                    new_rows.append(site_row("substack", site, lastmod=lastmod, listed="index", status="listed"))
            elif st.domain:
                continue    # polled through its own archive below
            elif lastmod > st.lastmod:
                queue.append((site, "index", lastmod))
    # Custom-domain publications: every few hours, through their archive's ETag.
    due = seen - dt.timedelta(hours=cfg.get("custom_every_hours", 3))
    for site, st in state.items():
        if st.domain and st.fetched < due and st.status not in ("blocked", "http 404", "http 410"):
            queue.append((site, "custom", None))
    if limit:
        queue = queue[:limit]
    full = {} if dry_run else load_site_rows("substack", [q[0] for q in queue])
    queue = [(site, full.get(site, {"lastmod": dt.datetime(1970, 1, 1), "listed": "index"}), mode, lm) for site, mode, lm in queue]
    robots = PlatformRobots(shared={".substack.com": "substack.com"})
    for site, row, mode, _ in queue:
        if row.get("robots") and row.get("domain"):
            robots.seeded[row["domain"]] = (row["robots"] == "allow", row["robots_checked"])
    limiter = Limiter(cfg.get("rate", 2), "substack")
    pages_per_site = cfg.get("pages_per_site", 4)
    too_many = threading.Event()
    n429 = [0]
    # The publications the index lists but this run will not fetch are
    # recorded now, so a run that dies later has still kept the list.
    if not dry_run:
        store_sites(new_rows)

    def one(item):
        site, row, mode, index_lastmod = item
        if too_many.is_set():
            return site, row, "skipped", [], [], None
        # Page back only to what we had, or to the first-run horizon for a
        # publication never fetched: the history walk does the rest.
        since = max(row.get("lastmod") or dt.datetime(1970, 1, 1), horizon)
        try:
            label, rows, site_log, updates = poll_site(site, row, since, limiter, robots, seen, pages_per_site, mode)
        except Exception as e:  # one publication must not stop the run
            label, rows, site_log, updates = "error", [], [], {"status": "error"}
            site_log.append(fetch_row("substack", site, mode, "error", collect.Fetched(error=type(e).__name__ + ": " + str(e)[:200]), 0, seen))
        if label == "http 429":
            # The platform asked for less: slow down, leave the row as it was
            # (so the publication is queued again next hour), and after
            # twenty of them within one batch (a tenth of it) give up on the
            # rest of the queue for this run.
            limiter.slow()
            n429[0] += 1
            if n429[0] >= 20:
                too_many.set()
            return site, row, label, [], site_log, None
        if mode == "index" and index_lastmod and label == "ok":
            updates["lastmod"] = max(updates.get("lastmod") or dt.datetime(1970, 1, 1), index_lastmod)
        elif mode == "index" and index_lastmod and label != "ok":
            updates["lastmod"] = index_lastmod   # do not retry it every hour for ever
        return site, row, label, rows, site_log, updates

    counts = defaultdict(int)
    total = {"items": 0, "new": 0, "changed": 0, "done": 0}
    index_log, log = log, []   # logged when the run completes, see below

    def flush(batch):
        """Store one batch of fetched publications: their posts, their
        fetch log rows with the new-row counts, their site rows. A run
        that dies loses at most one batch."""
        rows, keyed, blog, site_rows = [], set(), [], []
        for site, row, label, site_rows_, site_log, updates in batch:
            counts[label] += 1
            blog.extend(site_log)
            for r in site_rows_:
                key = (r["site"], r["url"], r["kind"], r["title"])
                if key not in keyed:
                    keyed.add(key)
                    rows.append(r)
            if updates is None:
                continue
            merged = dict(row)
            merged.update(updates)
            merged.setdefault("listed", "index")
            merged["seen"] = seen
            merged["fetched"] = seen if label in ("ok", "304") else row.get("fetched", dt.datetime(1970, 1, 1))
            if merged.get("domain") and merged["domain"] in robots.checked:
                merged["robots"] = "allow" if label != "robots" else "deny"
                merged["robots_checked"] = robots.checked[merged["domain"]]
            site_rows.append(site_row("substack", site, **{k: v for k, v in merged.items() if k in SITE_COLS and k not in ("platform", "site")}))
        new_by, changed_by = defaultdict(int), defaultdict(int)
        if not dry_run:
            store_posts(rows)
            if rows:
                new_by, changed_by = counts_by_site("substack", seen, [b[0] for b in batch])
            for r in blog:
                if r["kind"] == "archive":
                    r["new"], r["changed"] = new_by[r["target"]], changed_by[r["target"]]
            log_fetches(blog)
            store_sites(site_rows)
        total["items"] += len(rows)
        total["new"] += sum(new_by.values())
        total["changed"] += sum(changed_by.values())
        total["done"] += len(batch)
        n429[0] = 0     # the twenty-a-batch rule starts over
        if total["done"] < len(queue):
            print(f"  {total['done']}/{len(queue)} publications, items {total['items']}, new {total['new']}, "
                  f"changed {total['changed']}, pace {1 / limiter.interval:.2g}/s", flush=True)

    batch, every = [], cfg.get("flush_every", 200)
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.get("workers", 4)) as ex:
        for r in ex.map(one, queue):
            batch.append(r)
            if len(batch) >= every:
                flush(batch)
                batch = []
    if batch:
        flush(batch)
    if not dry_run:
        # The index fetch rows carry the ETags the next run sends back. They
        # are written only now, when every queued publication has been
        # handled, so that a run that dies re-reads the index in full and
        # queues again what it never reached.
        log_fetches(index_log)
        if counts.get("http 429"):
            log_fetches([fetch_row("substack", "run", "throttled", "http 429", collect.Fetched(status=429), counts["http 429"], seen)])
    print(f"{fmt(seen)[:16]} substack index {'changed' if index is not None else 'unchanged'} "
          f"{len(index or {})} listed, queued {len(queue)} (new sites {len(new_rows)} recorded), items {total['items']}, "
          f"new {total['new']} changed {total['changed']} "
          + " ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
    return counts


# ------------------------------------------------------- substack leaderboards

def run_leaderboards(cfg, dry_run=False):
    """Every top-level category's leaderboard, into blogs.sites: the
    subdomain, the custom domain, the name, language and size. Publications
    on their own domain are the reason: the index does not list them."""
    seen = utcnow()
    limiter = Limiter(cfg.get("leaderboard_rate", 0.5), "leaderboards")
    robots = PlatformRobots(shared={".substack.com": "substack.com"})
    log = []
    cats_url = "https://substack.com/api/v1/categories"
    if not robots.allowed(cats_url):
        print("leaderboards: substack.com robots.txt disallows the API; nothing done")
        return
    limiter.wait()
    f = fetch(cats_url)
    cats = json_body(f) if classify(f) == "ok" else None
    log.append(fetch_row("substack", cats_url, "leaderboard", classify(f), f, len(cats or []), seen))
    if not isinstance(cats, list):
        print(f"leaderboards: categories {classify(f)} {f.error}")
        if not dry_run:
            log_fetches(log)
        return
    found = {}     # subdomain -> dict
    complete = True
    for c in cats:
        if not isinstance(c, dict) or not c.get("leaderboard_enabled") or c.get("parent_id"):
            continue
        if not complete:
            break
        cid, cname = c.get("id"), c.get("name") or str(c.get("id"))
        for page in range(LEADERBOARD_MAX_PAGES):
            url = f"https://substack.com/api/v1/category/public/{cid}/all?page={page}"
            for attempt in range(4):
                limiter.wait()
                g = fetch(url, timeout=90)
                label = classify(g)
                if g.status != 429:
                    break
                time.sleep(30 * (attempt + 1))   # the platform asked for less
            d = json_body(g) if label == "ok" else None
            pubs = d.get("publications") if isinstance(d, dict) else None
            log.append(fetch_row("substack", url, "leaderboard", label if (label != "ok" or pubs is not None) else "parse", g, len(pubs or []), seen))
            if g.status == 429:
                complete = False   # the rest next hour; what was found is kept
                break
            if not pubs:
                break
            for p in pubs:
                sub = (p.get("subdomain") or "").lower()
                if not sub:
                    continue
                e = found.setdefault(sub, {"domain": (p.get("custom_domain") or "").lower(), "base_url": p.get("base_url") or "",
                                           "name": p.get("name") or "", "language": p.get("language") or "",
                                           "subscribers": 0, "first_post": p.get("first_post_date") or "", "categories": []})
                e["categories"].append(cname)
                try:
                    e["subscribers"] = max(e["subscribers"], int(str(p.get("freeSubscriberCount") or "0").replace(",", "")))
                except ValueError:
                    pass
            if not d.get("more"):
                break
        print(f"  leaderboard {cname}: {page + 1} pages, {len(found)} publications so far", flush=True)
    state = {} if dry_run else load_site_rows("substack", found.keys())
    rows = []
    for sub, e in found.items():
        row = state.get(sub, {})
        listed = "both" if row.get("listed") in ("index", "both") else "leaderboard"
        first = parse_iso(e["first_post"])
        rows.append(site_row("substack", sub, domain=e["domain"], base_url=e["base_url"] or row.get("base_url", ""),
                             lastmod=row.get("lastmod") or dt.datetime(1970, 1, 1), seen=seen, listed=listed,
                             category=", ".join(dict.fromkeys(e["categories"]))[:500], name=clean_title(e["name"])[:200],
                             language=e["language"][:10], subscribers=e["subscribers"],
                             first_post=first.strftime("%Y-%m-%d") if first else "1970-01-01",
                             etag=row.get("etag", ""), status=row.get("status") or "listed",
                             fetched=row.get("fetched") or dt.datetime(1970, 1, 1),
                             robots=row.get("robots", ""), robots_checked=row.get("robots_checked") or dt.datetime(1970, 1, 1)))
    custom = sum(1 for e in found.values() if e["domain"])
    if complete:
        log.append(fetch_row("substack", "leaderboards", "leaderboards-done", "ok", collect.Fetched(status=200), len(found), seen))
    if not dry_run:
        store_sites(rows)
        log_fetches(log)
    print(f"{fmt(seen)[:16]} leaderboards{'' if complete else ' (incomplete: 429, the rest next hour)'}: {len(found)} publications, "
          f"{custom} on their own domain, {sum(1 for s in found if s not in state)} not in blogs.sites before, {len(log)} requests", flush=True)


# ------------------------------------------------------------------- medium

MEDIUM_FILE_RE = re.compile(r"<loc>(https://medium\.com/sitemap/posts/(\d{4})/posts-(\d{4}-\d{2}-\d{2})\.xml)</loc>(?:\s*<lastmod>([^<]*)</lastmod>)?")


def medium_index(cfg, cond, log, seen):
    """{file url: (day, index lastmod)} for every daily post file, or None."""
    f = fetch(cfg["index"], *cond.get(cfg["index"], ("", "")), timeout=120)
    label = classify(f)
    files = {}
    if label == "ok":
        for m in MEDIUM_FILE_RE.finditer(f.body.decode("utf-8", "replace")):
            files[m.group(1)] = (m.group(3), m.group(4) or "")
    log.append(fetch_row("medium", cfg["index"], "index", label, f, len(files), seen))
    return files if label == "ok" else None


def slug_title(url):
    """Medium's slug as a title: the trailing id dropped, hyphens to spaces,
    percent-encoding undone. Lower case and unpunctuated, as the slug is."""
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    slug = re.sub(r"-[0-9a-f]{8,16}$", "", slug)
    return clean_title(" ".join(urllib.parse.unquote(slug).split("-")))


def medium_site(url):
    p = urllib.parse.urlsplit(url)
    segs = [s for s in p.path.split("/") if s]
    if p.netloc.lower() == "medium.com" and len(segs) >= 2:
        return segs[0]
    return p.netloc.lower()


def medium_post_id(url):
    m = re.search(r"-([0-9a-f]{8,16})$", urllib.parse.urlsplit(url).path.rstrip("/"))
    return m.group(1) if m else ""


def read_medium_file(url, day, etag, lm, limiter, robots, seen):
    """(label, rows, fetched) for one daily post sitemap."""
    if robots is not None and not robots.allowed(url):
        return ("robots", [], collect.Fetched(url=url))
    limiter.wait()
    f = fetch(url, etag, lm, timeout=120)
    label = classify(f)
    if label != "ok":
        return (label, [], f)
    root = collect.parse_xml(f.body)
    if root is None:
        return ("parse", [], f)
    rows = []
    for u in root:
        if collect.local(u.tag) != "url":
            continue
        loc = ""
        for c in u:
            if collect.local(c.tag) == "loc":
                loc = collect.text_of(c)
        loc = norm_url(loc)
        title = slug_title(loc) if loc else ""
        if not loc or not title:
            continue
        rows.append({"ts": day + " 00:00:00", "platform": "medium", "site": medium_site(loc), "domain": host_of(loc),
                     "url": loc, "title": title, "seen": fmt(seen), "kind": "slug", "source": url, "subtitle": "",
                     "type": "", "audience": "", "lang": guess_lang(title), "author": "", "wordcount": 0,
                     "reactions": 0, "comments": 0, "restacks": 0, "post_id": medium_post_id(loc), "tags": []})
    return ("ok", rows, f)


def load_files(platform):
    out = {}
    q = f"SELECT file, lastmod, etag, status FROM blogs.files FINAL WHERE platform = '{platform}' FORMAT TSV"
    for line in ch(q).splitlines():
        parts = (line.split("\t") + ["", "", ""])[:4]
        out[parts[0]] = {"lastmod": parts[1], "etag": parts[2], "status": parts[3]}
    return out


def process_medium_files(cfg, todo, files_state, seen, dry_run, staging="blogs.staging", label_kind="file"):
    """Fetch and store a list of (file url, day, index lastmod). Returns
    (rows stored count, per-file labels)."""
    limiter = Limiter(cfg.get("rate", 1), "medium")
    robots = None if cfg.get("robots") == "ignore" else PlatformRobots()
    log, file_rows, labels, total = [], [], defaultdict(int), 0
    batch = []

    def flush():
        nonlocal batch
        if batch and not dry_run:
            store_posts(batch, staging)
        batch = []

    for url, day, lastmod in todo:
        st = files_state.get(url, {})
        label, rows, f = read_medium_file(url, day, st.get("etag", ""), "", limiter, robots, seen)
        labels[label] += 1
        log.append(fetch_row("medium", url, label_kind, label, f, len(rows), seen))
        if label == "ok":
            batch.extend(rows)
            total += len(rows)
            if len(batch) >= 20000:
                flush()
        if label in ("ok", "304"):
            file_rows.append({"platform": "medium", "file": url, "lastmod": lastmod, "etag": (f.etag or st.get("etag", ""))[:200],
                              "seen": fmt(seen), "items": len(rows), "status": label})
        else:
            file_rows.append({"platform": "medium", "file": url, "lastmod": st.get("lastmod", ""), "etag": st.get("etag", ""),
                              "seen": fmt(seen), "items": 0, "status": label})
    flush()
    if not dry_run:
        log_fetches(log)
        if file_rows:
            ch("INSERT INTO blogs.files FORMAT JSONEachRow", rows_to_json(file_rows))
    return total, labels


def run_medium(cfg, dry_run=False, limit=None, force=False):
    seen = utcnow()
    if not dry_run and not force:
        last = last_fetch("medium", "index")
        if last and seen - last < dt.timedelta(hours=cfg.get("every_hours", 6)):
            print(f"{fmt(seen)[:16]} medium: index read {int((seen - last).total_seconds() // 60)} min ago, not due")
            return
    cond = {} if dry_run else conditional_headers("medium")
    log = []
    files = medium_index(cfg, cond, log, seen)
    if files is None:
        if not dry_run:
            log_fetches(log)
        print(f"{fmt(seen)[:16]} medium: index {log[-1]['label']} {log[-1]['error']}")
        return
    if not dry_run:
        log_fetches(log)
    state = {} if dry_run else load_files("medium")
    horizon = (seen - dt.timedelta(days=cfg.get("days", 7))).strftime("%Y-%m-%d")
    todo = []
    for url, (day, lastmod) in sorted(files.items(), key=lambda kv: kv[1][0]):
        if day < horizon or day > seen.strftime("%Y-%m-%d"):
            continue
        st = state.get(url)
        if st and st["status"] in ("ok", "304") and st["lastmod"] == lastmod:
            continue
        todo.append((url, day, lastmod))
    if limit:
        todo = todo[:limit]
    total, labels = process_medium_files(cfg, todo, state, seen, dry_run)
    new = changed = 0
    if not dry_run and total:
        n, c = counts_by_site("medium", seen)
        new, changed = sum(n.values()), sum(c.values())
    print(f"{fmt(seen)[:16]} medium: {len(files)} daily files listed, {len(todo)} read, items {total}, new {new} changed {changed} "
          + " ".join(f"{k}={v}" for k, v in sorted(labels.items())), flush=True)


def history_medium(cfg, day_from, day_to, limit=None, dry_run=False):
    """Every daily file from day_from to day_to not yet read, oldest first,
    at the platform's pace; resumable through blogs.files."""
    seen = utcnow()
    # No conditional headers: a 304 (the hourly run read the index since it
    # last changed) would leave the walk without the list of files.
    log = []
    files = medium_index(cfg, {}, log, seen)
    if files is None:
        sys.exit(f"medium index: {log[-1]['label']} {log[-1]['error']}")
    state = {} if dry_run else load_files("medium")
    todo = []
    for url, (day, lastmod) in sorted(files.items(), key=lambda kv: kv[1][0]):
        if day < day_from or day > day_to:
            continue
        st = state.get(url)
        if st and st["status"] in ("ok", "304"):
            continue
        todo.append((url, day, lastmod))
    if limit:
        todo = todo[:limit]
    print(f"medium history {day_from}..{day_to}: {len(todo)} files to read", flush=True)
    done = 0
    for i in range(0, len(todo), 30):
        chunk = todo[i:i + 30]
        total, labels = process_medium_files(cfg, chunk, state, seen, dry_run, staging="blogs.staging_history", label_kind="history")
        done += len(chunk)
        print(f"  {chunk[-1][1]} {done}/{len(todo)} items {total} " + " ".join(f"{k}={v}" for k, v in sorted(labels.items())), flush=True)
    print(f"medium history: done {done} files", flush=True)


# ------------------------------------------------------------ substack history

def history_substack(cfg, since, date_from, limit=None, dry_run=False, unfetched=False, rate=None):
    """Walk publications' archives back to date_from, most recently active
    first, resumable through blogs.history. since limits which publications:
    those the platform's list shows posting on or after it.

    unfetched: only publications the hourly run has never fetched (it
    fetches a publication only once its last post moves, so one that posted
    on 2026-09-05 and not since has none of its posts stored). This is the
    catch-up that makes the blogs complete from a start date: with --since
    and --from both that date, every publication that posted after it and
    was never read is read back to it. A walk that stops at date_from is
    not marked done, so a later, deeper walk resumes from the offset
    reached instead of skipping the publication."""
    seen = utcnow()
    state = load_sites("substack")
    q = "SELECT site, offset, done, oldest, pages FROM blogs.history FINAL WHERE platform = 'substack' FORMAT TSV"
    marks = {}
    for line in ch(q).splitlines():
        site, offset, done, oldest, pages = line.split("\t")
        marks[site] = {"offset": int(offset), "done": int(done), "oldest": parse_date(oldest), "pages": int(pages)}
    todo = [s for s, r in sorted(state.items(), key=lambda kv: kv[1].lastmod, reverse=True)
            if r.lastmod >= since and r.status not in ("blocked", "robots", "http 404", "http 410")
            and not marks.get(s, {}).get("done")
            and not (unfetched and r.fetched.year > 2000)]
    if limit:
        todo = todo[:limit]
    print(f"substack history: {len(todo)} publications to walk back to {date_from:%Y-%m-%d}", flush=True)
    limiter = Limiter(rate or cfg.get("rate", 2), "substack history")
    robots = PlatformRobots(shared={".substack.com": "substack.com"})
    rows, log, marks_out, n_done = [], [], [], 0

    def flush():
        nonlocal rows, log, marks_out
        if not dry_run:
            store_posts(rows, "blogs.staging_history")
            log_fetches(log)
            if marks_out:
                ch("INSERT INTO blogs.history FORMAT JSONEachRow", rows_to_json(marks_out))
        rows, log, marks_out = [], [], []

    for site in todo:
        base = state[site].base_url or f"https://{site}.substack.com"
        source = archive_url(base, 0, 0).split("?")[0]
        mark = marks.get(site, {"offset": 0, "done": 0, "oldest": None, "pages": 0})
        offset, pages, oldest, done, keyed = mark["offset"], mark["pages"], mark["oldest"], 0, set()
        while pages < MAX_HISTORY_PAGES:
            label, posts, f = read_archive(base, offset, limiter, robots)
            log.append(fetch_row("substack", site, "history", label, f, len(posts), seen))
            if label != "ok":
                if label in ("blocked", "http 404", "http 410", "robots"):
                    done = 1
                if label == "http 429":
                    limiter.slow()
                break
            pages += 1
            if not posts:
                done = 1
                break
            for r in archive_posts(base, posts, site, seen, source):
                key = (r["url"], r["title"])
                if key not in keyed:
                    keyed.add(key)
                    rows.append(r)
            dates = [d for d in (parse_iso(p.get("post_date")) for p in posts if isinstance(p, dict)) if d]
            if dates:
                oldest = min(oldest or dates[0], min(dates))
            offset += len(posts)
            if dates and min(dates) < date_from:
                done = 0 if unfetched else 1
                break
        marks_out.append({"platform": "substack", "site": site, "offset": offset, "done": done,
                          "oldest": fmt(oldest) if oldest else "1970-01-01 00:00:00", "pages": pages, "updated": fmt(utcnow())})
        n_done += 1
        if len(marks_out) >= 25:
            flush()
            print(f"  {n_done}/{len(todo)} {site} offset {offset} {'done' if done else 'more'}", flush=True)
    flush()
    print(f"substack history: walked {n_done} publications", flush=True)


# ---------------------------------------------------------------- wordpress

def run_wordpress(cfg, dry_run=False, limit=None):
    """Tag streams from WordPress.com's public API, paged back by date to
    the newest post already stored (or pages_per_tag pages on a first run)."""
    seen = utcnow()
    robots = None if cfg.get("robots") == "ignore" else PlatformRobots()
    limiter = Limiter(cfg.get("rate", 1), "wordpress")
    api = cfg.get("api", "https://public-api.wordpress.com/rest/v1.1").rstrip("/")
    tags = cfg.get("tags", [])
    if limit:
        tags = tags[:limit]
    newest = {}
    if not dry_run:
        q = "SELECT source, max(ts) FROM blogs.posts WHERE platform = 'wordpress' GROUP BY source FORMAT TSV"
        for line in ch(q).splitlines():
            src, ts = line.split("\t")
            newest[src] = parse_date(ts)
    rows, log, keyed, labels = [], [], set(), defaultdict(int)
    for tag in tags:
        source = f"{api}/read/tags/{urllib.parse.quote(tag)}/posts"
        floor = newest.get(source) or (seen - dt.timedelta(days=cfg.get("days", 7)))
        before, pages = "", 0
        while pages < cfg.get("pages_per_tag", 5):
            url = source + "?number=40" + (f"&before={urllib.parse.quote(before)}" if before else "")
            if robots is not None and not robots.allowed(url):
                labels["robots"] += 1
                log.append(fetch_row("wordpress", tag, "api", "robots", collect.Fetched(url=url), 0, seen))
                break
            limiter.wait()
            f = fetch(url, timeout=60)
            label = classify(f)
            d = json_body(f) if label == "ok" else None
            posts = d.get("posts") if isinstance(d, dict) else None
            if posts is None and label == "ok":
                label = "parse"
            labels[label] += 1
            log.append(fetch_row("wordpress", tag, "api", label, f, len(posts or []), seen))
            if not posts:
                break
            pages += 1
            oldest = None
            for p in posts:
                url_ = norm_url(p.get("URL") or "")
                title = clean_title(html.unescape(p.get("title") or ""))
                ts = parse_iso(p.get("date")) or seen
                if not url_ or not title:
                    continue
                oldest = ts if oldest is None or ts < oldest else oldest
                key = (url_, title)
                if key in keyed:
                    continue
                keyed.add(key)
                author = p.get("author") or {}
                rows.append({"ts": fmt(ts), "platform": "wordpress", "site": host_of(p.get("site_URL") or url_), "domain": host_of(url_),
                             "url": url_, "title": title, "seen": fmt(seen), "kind": "api", "source": source, "subtitle": "",
                             "type": "", "audience": "", "lang": "", "author": clean_title(author.get("name") or "")[:200] if isinstance(author, dict) else "",
                             "wordcount": 0, "reactions": max(0, int(p.get("like_count") or 0)), "comments": max(0, int(p.get("comment_count") or 0)),
                             "restacks": 0, "post_id": str(p.get("ID") or ""), "tags": list((p.get("tags") or {}).keys())[:20]})
            if oldest is None or oldest <= floor:
                break
            before = (d.get("date_range") or {}).get("before") or (oldest - dt.timedelta(seconds=1)).isoformat() + "Z"
    new = changed = 0
    if not dry_run:
        store_posts(rows)
        if rows:
            n, c = counts_by_site("wordpress", seen)
            new, changed = sum(n.values()), sum(c.values())
        log_fetches(log)
    print(f"{fmt(seen)[:16]} wordpress: {len(tags)} tags, items {len(rows)}, new {new} changed {changed} "
          + " ".join(f"{k}={v}" for k, v in sorted(labels.items())), flush=True)


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="what the timer runs: every enabled platform")
    p.add_argument("--platform", choices=["substack", "medium", "wordpress"])
    p.add_argument("--dry-run", action="store_true", help="fetch and parse, write nothing")
    p.add_argument("--limit", type=int, help="at most N publications, files or tags, for a trial")
    p.add_argument("--force", action="store_true", help="read Medium's index even if not due")
    p = sub.add_parser("site", help="one Substack publication's first archive page, printed")
    p.add_argument("site", help="a subdomain, or a base URL")
    p = sub.add_parser("leaderboards", help="sweep Substack's category leaderboards into blogs.sites")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("history", help="walk a platform's history; resumable")
    p.add_argument("platform", choices=["substack", "medium"])
    p.add_argument("--since", default=None, help="substack: publications active on or after this day (default 365 days ago)")
    p.add_argument("--from", dest="date_from", default="2018-01-01", help="walk back to this day")
    p.add_argument("--to", dest="date_to", default=None, help="medium: last day to read (default yesterday)")
    p.add_argument("--limit", type=int)
    p.add_argument("--unfetched", action="store_true", help="substack: only publications the hourly run never fetched (the catch-up to a start date)")
    p.add_argument("--rate", type=float, help="substack: requests a second, below the configured rate when the hourly run is going too")
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = load_config()

    if a.cmd == "site":
        base = a.site if a.site.startswith("http") else f"https://{a.site}.substack.com"
        label, posts, f = read_archive(base, 0, Limiter(2), PlatformRobots(shared={".substack.com": "substack.com"}))
        print(f"{label} http={f.status} bytes={len(f.body)} posts={len(posts)} final={f.url} {f.error}")
        site = a.site if not a.site.startswith("http") else host_of(a.site)
        for r in archive_posts(base, posts, site, utcnow(), base):
            print(f"{r['ts']}\t{r['type']}\t{r['audience']}\t{r['lang']}\t{r['url']}\t{r['title']}")
        return
    if a.cmd == "leaderboards":
        run_leaderboards(cfg["substack"], dry_run=a.dry_run)
        return
    if a.cmd == "history":
        if a.platform == "substack":
            since = parse_date(a.since) if a.since else utcnow() - dt.timedelta(days=365)
            history_substack(cfg["substack"], since, parse_date(a.date_from), limit=a.limit, dry_run=a.dry_run,
                             unfetched=a.unfetched, rate=a.rate)
        else:
            to = a.date_to or (utcnow() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
            history_medium(cfg["medium"], a.date_from, to, limit=a.limit, dry_run=a.dry_run)
        return

    platforms = [a.platform] if a.platform else [p for p in ("substack", "medium", "wordpress") if cfg.get(p, {}).get("enabled")]
    for name in platforms:
        c = cfg.get(name, {})
        if name == "substack":
            # The weekly leaderboard sweep (about 700 requests at half a
            # request a second, 25 minutes) runs first when due; a trial
            # (--limit) or a dry run skips it.
            if not a.dry_run and not a.limit and c.get("leaderboards_every_days"):
                last = last_fetch("substack", "leaderboards-done")
                if last is None or utcnow() - last > dt.timedelta(days=c["leaderboards_every_days"]):
                    run_leaderboards(c)
            run_substack(c, dry_run=a.dry_run, limit=a.limit)
        elif name == "medium":
            run_medium(c, dry_run=a.dry_run, limit=a.limit, force=a.force)
        elif name == "wordpress":
            run_wordpress(c, dry_run=a.dry_run, limit=a.limit)


if __name__ == "__main__":
    main()
