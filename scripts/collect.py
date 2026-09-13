#!/usr/bin/env python3
"""Collect headlines from the RSS feeds and news sitemaps of outlets GDELT
has lost, into a database of their own. Stdlib only.

Why: sessions/2026-09-12-gdelt-source-coverage.md in the root repo. The
corpus behind the site lost the New York Times newsroom, the Washington
Post, Reuters, AP, the Wall Street Journal and some five hundred other
outlets between 2022 and 2026, mostly to bot walls in front of the article
pages. Their feeds and Google News sitemaps are another matter: those are
published for crawlers, carry the headline and the time, and are often
served from a host that has no wall. This script reads them and keeps
every headline it has not seen. It never fetches an article page.

What it keeps is separate from the site's data: database `collect`, not
`default`, its own ClickHouse user, its own timer (server/collect.timer,
hourly). Nothing on the site reads it yet. scripts/collect_schema.sql has
the tables; scripts/feeds.json has the feeds, one entry per feed URL with
the site as GDELT names it (`domain`), a section label, and `kind` (rss or
sitemap; Atom counts as rss). scripts/discover.py finds candidates.

Manners: a truthful User-Agent naming the site; robots.txt read with it
and obeyed (a 5xx or an unreachable robots.txt means no fetch this run,
per RFC 9309); conditional requests (ETag, Last-Modified) so an unchanged
feed costs a 304; one request in flight per host; no retries inside a run.
Nothing here gets round a bot wall: a challenge page, a 401 or a 403 is
logged as such and the feed is tried again next hour.

Headlines change. A feed and a sitemap both show the current title of a
URL, in place; neither is a changelog. So the unit stored is (domain, url,
kind, title): the first time a URL is seen under a kind its title is
version 1; a run that finds the same URL with a different title stores the
new title as version 2, with the time. A search over version 1 rows is the
headline as first published, the nearest thing to what GDELT would have
crawled; the rows with version > 1 are the edits, with the time of each.
The RSS title and the sitemap's news:title of one story are often
different strings (the sitemap's is nearer the page's <title>, which is
what GDELT keeps), so versions are counted per kind and both are kept.

  scripts/collect.py                # every enabled feed, into ClickHouse
  scripts/collect.py --dry-run      # fetch and parse, print counts, write nothing
  scripts/collect.py --feed URL     # one feed, print its items, write nothing
  scripts/collect.py --domain nytimes.com   # only that site's feeds

Environment: CH_URL (default http://127.0.0.1:8123), CH_USER, CH_PASSWORD.
"""

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import gzip
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import defaultdict

UA_TOKEN = "headlinesearch"
UA = "headlinesearch/0.1 (+https://newsheadlinesearch.com; RSS and news sitemaps only, no article pages)"
ACCEPT = "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5"
TIMEOUT = 30
MAX_BYTES = 20_000_000
MAX_CHILD_SITEMAPS = 6      # of a sitemap index, newest first
HERE = os.path.dirname(os.path.abspath(__file__))
FEEDS_PATH = os.path.join(HERE, "feeds.json")

CH_URL = os.environ.get("CH_URL", "http://127.0.0.1:8123")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASSWORD = os.environ.get("CH_PASSWORD", "")

WS_RE = re.compile(r"\s+")
# Query parameters that mark where a click came from, not which page it is.
TRACKING_RE = re.compile(r"^(utm_.*|fbclid|gclid|ocid|cmpid|cmp|ns_.*|mc_.*|ito|ftag|src|ref|sref|rss|output|s|ss|smid|smtyp|partner|icid|ICID|itm_.*|_gl|taid|guccounter|guce_referrer.*|syn-.*)$")
CHALLENGE_RE = re.compile(r"Just a moment|Please enable JS|challenge-platform|_cf_chl|cf-browser-verification|captcha-delivery|DataDome|Access Denied|Request unsuccessful|Incapsula|perimeterx|px-captcha", re.I)


# ------------------------------------------------------------------ fetching

class Fetched:
    """One HTTP fetch. status is the HTTP code, or 0 for a network error."""
    __slots__ = ("status", "body", "etag", "last_modified", "url", "error", "ms")

    def __init__(self, status=0, body=b"", etag="", last_modified="", url="", error="", ms=0):
        self.status, self.body, self.etag, self.last_modified = status, body, etag, last_modified
        self.url, self.error, self.ms = url, error, ms


def fetch(url, etag="", last_modified="", timeout=TIMEOUT):
    """GET a URL politely. Follows redirects, decodes gzip, caps the size."""
    headers = {"User-Agent": UA, "Accept": ACCEPT, "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return Fetched(r.status, b"", url=r.url, error="too large", ms=int((time.monotonic() - t0) * 1000))
            if r.headers.get("Content-Encoding", "").lower() == "gzip" or body[:2] == b"\x1f\x8b":
                try:
                    body = gzip.decompress(body)
                except (OSError, EOFError):
                    pass
            return Fetched(r.status, body, r.headers.get("ETag", ""), r.headers.get("Last-Modified", ""), r.url,
                           ms=int((time.monotonic() - t0) * 1000))
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(200_000)
        except Exception:
            pass
        return Fetched(e.code, body, url=e.url or url, error=f"http {e.code}", ms=int((time.monotonic() - t0) * 1000))
    except Exception as e:  # URLError, socket.timeout, ssl errors, RemoteDisconnected, ...
        return Fetched(0, b"", url=url, error=type(e).__name__ + ": " + str(e)[:120], ms=int((time.monotonic() - t0) * 1000))


def looks_like_challenge(body):
    head = body[:20000].decode("utf-8", "replace")
    return bool(CHALLENGE_RE.search(head))


# -------------------------------------------------------------------- robots

class Robots:
    """robots.txt per host, fetched once a run with our User-Agent.
    RFC 9309: 2xx parse; 4xx allow all; 5xx or unreachable, no crawling."""

    def __init__(self):
        self.cache = {}

    def allowed(self, url):
        host = urllib.parse.urlsplit(url).netloc.lower()
        if host not in self.cache:
            self.cache[host] = self._load(host, url)
        rp = self.cache[host]
        if rp is None:
            return False
        if rp is True:
            return True
        return rp.can_fetch(UA_TOKEN, url)

    def _load(self, host, url):
        scheme = urllib.parse.urlsplit(url).scheme or "https"
        f = fetch(f"{scheme}://{host}/robots.txt", timeout=15)
        if f.status == 0 or f.status >= 500:
            return None
        if f.status >= 400:
            return True
        if f.status == 304:
            return True
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(f.body.decode("utf-8", "replace").splitlines())
        except Exception:
            return True
        return rp


# -------------------------------------------------------------------- parsing

def local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def text_of(el):
    if el is None:
        return ""
    return html.unescape(WS_RE.sub(" ", "".join(el.itertext()))).strip()


def clean_title(s):
    s = html.unescape(WS_RE.sub(" ", s or "")).strip()
    return s.replace("\t", " ")


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def parse_date(s):
    """RFC 822 (RSS) or ISO 8601 (Atom, sitemaps) to a naive UTC datetime, or None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
            if not m:
                return None
            d = dt.datetime.fromisoformat(m.group(1))
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if d.year < 2000 or d > utcnow() + dt.timedelta(days=2):
        return None
    return d


def norm_url(u):
    """Lowercase scheme and host, drop the fragment and tracking parameters."""
    u = (u or "").strip()
    if not u.startswith(("http://", "https://")):
        return ""
    p = urllib.parse.urlsplit(u)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not TRACKING_RE.match(k)]
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urllib.parse.urlencode(q), ""))


def parse_xml(body):
    """ElementTree root, or None. Strips a BOM and leading junk before '<'."""
    body = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    i = body.find(b"<")
    if i < 0:
        return None
    body = body[i:]
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        # A few feeds carry control characters or bare ampersands.
        fixed = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", body)
        fixed = re.sub(rb"&(?!(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);)", b"&amp;", fixed)
        try:
            return ET.fromstring(fixed)
        except ET.ParseError:
            return None


def parse_items(root):
    """(format, items). format: rss, atom, sitemap, sitemapindex, plain-sitemap
    (a urlset with no news:title, useless to us), or ''. items: dicts with
    url, title, ts (datetime or None), guid. For a sitemapindex the items are
    the child sitemap URLs, with lastmod in ts."""
    kind = local(root.tag).lower()
    items = []
    if kind == "rss" or kind == "rdf":
        for it in root.iter():
            if local(it.tag) != "item":
                continue
            title = link = guid = date = ""
            for c in it:
                t = local(c.tag)
                if t == "title":
                    title = text_of(c)
                elif t == "link":
                    link = text_of(c) or c.get("href", "")
                elif t == "guid":
                    guid = text_of(c)
                elif t in ("pubDate", "date", "published", "updated") and not date:
                    date = text_of(c)
            if not link and guid.startswith("http"):
                link = guid
            items.append({"url": norm_url(link), "title": clean_title(title), "ts": parse_date(date), "guid": guid[:200]})
        return ("rss", items)
    if kind == "feed":
        for e in root:
            if local(e.tag) != "entry":
                continue
            title = link = guid = date = ""
            for c in e:
                t = local(c.tag)
                if t == "title":
                    title = text_of(c)
                elif t == "link":
                    rel = c.get("rel", "alternate")
                    if rel == "alternate" and (not link or c.get("type", "").startswith("text/html")):
                        link = c.get("href", "")
                elif t == "id":
                    guid = text_of(c)
                elif t == "published":
                    date = text_of(c)
                elif t == "updated" and not date:
                    date = text_of(c)
            if not link and guid.startswith("http"):
                link = guid
            items.append({"url": norm_url(link), "title": clean_title(title), "ts": parse_date(date), "guid": guid[:200]})
        return ("atom", items)
    if kind == "sitemapindex":
        for s in root:
            if local(s.tag) != "sitemap":
                continue
            loc = lastmod = ""
            for c in s:
                t = local(c.tag)
                if t == "loc":
                    loc = text_of(c)
                elif t == "lastmod":
                    lastmod = text_of(c)
            if loc:
                items.append({"url": loc, "title": "", "ts": parse_date(lastmod), "guid": ""})
        return ("sitemapindex", items)
    if kind == "urlset":
        with_titles = 0
        for u in root:
            if local(u.tag) != "url":
                continue
            loc = title = date = lastmod = ""
            for c in u:
                t = local(c.tag)
                if t == "loc":
                    loc = text_of(c)
                elif t == "lastmod":
                    lastmod = text_of(c)
                elif t == "news":
                    for n in c.iter():
                        nt = local(n.tag)
                        if nt == "title":
                            title = text_of(n)
                        elif nt == "publication_date":
                            date = text_of(n)
            if title:
                with_titles += 1
            items.append({"url": norm_url(loc), "title": clean_title(title), "ts": parse_date(date or lastmod), "guid": ""})
        if not with_titles:
            return ("plain-sitemap", [])
        return ("sitemap", [i for i in items if i["title"]])
    return ("", [])


def classify(f):
    """A label for a fetch: ok, 304, blocked (a challenge page or 401/403),
    html (a page where a feed should be), empty, http NNN, error, parse."""
    if f.status == 304:
        return "304"
    if f.status == 0:
        return "error"
    if f.status in (401, 403) or (f.status in (429, 503) and looks_like_challenge(f.body)):
        return "blocked"
    if f.status >= 400:
        return f"http {f.status}"
    if not f.body.strip():
        return "empty"
    head = f.body[:2000].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head[:300]:
        return "blocked" if looks_like_challenge(f.body) else "html"
    return "ok"


def read_feed(url, etag="", last_modified="", robots=None, depth=0):
    """Fetch one feed or sitemap and return (label, format, items, fetched).
    A sitemap index is followed into its newest children."""
    if robots is not None and not robots.allowed(url):
        return ("robots", "", [], Fetched(url=url))
    f = fetch(url, etag, last_modified)
    label = classify(f)
    if label != "ok":
        return (label, "", [], f)
    root = parse_xml(f.body)
    if root is None:
        return ("parse", "", [], f)
    fmt, items = parse_items(root)
    if fmt == "sitemapindex":
        if depth > 0:
            return ("nested-index", fmt, [], f)
        children = sorted(items, key=lambda i: i["ts"] or dt.datetime.min, reverse=True)
        # Prefer children that call themselves news; then the newest by lastmod.
        news_first = [c for c in children if "news" in c["url"].lower()] + [c for c in children if "news" not in c["url"].lower()]
        out = []
        for c in news_first[:MAX_CHILD_SITEMAPS]:
            lab, cf, ci, _ = read_feed(c["url"], robots=robots, depth=1)
            if cf == "sitemap":
                out.extend(ci)
        return ("ok" if out else "index-empty", "sitemap", out, f)
    if not fmt:
        return ("parse", "", [], f)
    if fmt == "plain-sitemap":
        return ("no-titles", fmt, [], f)
    return ("ok", fmt, [i for i in items if i["url"] and i["title"]], f)


# ----------------------------------------------------------------- clickhouse

def ch(query, data=None, settings=None):
    params = {"query": query}
    if settings:
        params.update(settings)
    req = urllib.request.Request(
        CH_URL + "/?" + urllib.parse.urlencode(params), data=data, method="POST",
        headers={"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASSWORD})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"clickhouse {e.code}: {e.read().decode()[:500]}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def conditional_headers():
    """The ETag and Last-Modified each feed answered with last time, so that
    an unchanged feed costs a 304."""
    out = {}
    q = ("SELECT feed, argMax(etag, ts), argMax(last_modified, ts) FROM collect.fetches "
         "WHERE ts > now() - INTERVAL 7 DAY AND label IN ('ok', '304') GROUP BY feed FORMAT TSV")
    for line in ch(q).splitlines():
        feed, etag, lm = (line.split("\t") + ["", ""])[:3]
        out[feed] = (etag, lm)
    return out


INSERT_NEW = """
INSERT INTO collect.headlines (ts, domain, url, title, seen, version, feed, kind, section, guid)
SELECT s.ts, s.domain, s.url, s.title, s.seen,
       toUInt16(e.n + row_number() OVER (PARTITION BY s.domain, s.url, s.kind ORDER BY s.ts, s.title)) AS version,
       s.feed, s.kind, s.section, s.guid
FROM collect.staging AS s
LEFT JOIN (
  SELECT domain, url, kind, count() AS n, groupArray(title) AS titles
  FROM collect.headlines
  WHERE (domain, url) IN (SELECT domain, url FROM collect.staging)
  GROUP BY domain, url, kind
) AS e ON s.domain = e.domain AND s.url = e.url AND s.kind = e.kind
WHERE NOT has(e.titles, s.title)
"""


# ------------------------------------------------------------------- the run

def load_feeds(path=FEEDS_PATH):
    with open(path) as fh:
        doc = json.load(fh)
    feeds = [f for f in doc["feeds"] if f.get("enabled", True)]
    for f in feeds:
        f.setdefault("section", "")
        f["kind"] = "sitemap" if f.get("kind") == "sitemap" else "rss"
    return feeds


def one_feed(feed, cond, robots):
    etag, lm = cond.get(feed["url"], ("", ""))
    label, fmt, items, f = read_feed(feed["url"], etag, lm, robots)
    kind = "sitemap" if fmt == "sitemap" else "feed"
    return feed, label, kind, items, f


def run(feeds, dry_run=False, workers=8):
    seen = utcnow()
    robots = Robots()
    cond = {} if dry_run else conditional_headers()
    # One host at a time: feeds grouped by host, hosts in parallel.
    by_host = defaultdict(list)
    for f in feeds:
        by_host[urllib.parse.urlsplit(f["url"]).netloc.lower()].append(f)

    def host_batch(fs):
        return [one_feed(f, cond, robots) for f in fs]

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for batch in ex.map(host_batch, by_host.values()):
            results.extend(batch)

    rows, keyed = [], {}
    for feed, label, kind, items, f in results:
        for it in items:
            key = (feed["domain"], it["url"], kind, it["title"])
            if key in keyed:
                continue
            keyed[key] = True
            rows.append({
                "ts": (it["ts"] or seen).strftime("%Y-%m-%d %H:%M:%S"),
                "domain": feed["domain"], "url": it["url"], "title": it["title"],
                "seen": seen.strftime("%Y-%m-%d %H:%M:%S"),
                "feed": feed["url"], "kind": kind, "section": feed["section"], "guid": it["guid"],
            })

    new_by_feed, changed_by_feed = defaultdict(int), defaultdict(int)
    if not dry_run:
        ch("TRUNCATE TABLE collect.staging")
        if rows:
            payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode()
            ch("INSERT INTO collect.staging FORMAT JSONEachRow", payload)
            ch(INSERT_NEW)
            ch("TRUNCATE TABLE collect.staging")
            q = (f"SELECT feed, count(), countIf(version > 1) FROM collect.headlines "
                 f"WHERE seen = '{seen:%Y-%m-%d %H:%M:%S}' GROUP BY feed FORMAT TSV")
            for line in ch(q).splitlines():
                feed, n, c = line.split("\t")
                new_by_feed[feed], changed_by_feed[feed] = int(n), int(c)
        log = []
        for feed, label, kind, items, f in results:
            log.append({
                "ts": seen.strftime("%Y-%m-%d %H:%M:%S"), "feed": feed["url"], "domain": feed["domain"], "kind": kind,
                "label": label, "http": f.status, "bytes": len(f.body), "items": len(items),
                "new": new_by_feed[feed["url"]], "changed": changed_by_feed[feed["url"]], "ms": f.ms,
                "etag": f.etag[:200], "last_modified": f.last_modified[:100], "error": f.error[:300],
            })
        ch("INSERT INTO collect.fetches FORMAT JSONEachRow", "\n".join(json.dumps(r, ensure_ascii=False) for r in log).encode())

    counts = defaultdict(int)
    for feed, label, kind, items, f in results:
        counts[label] += 1
    problems = [(feed["domain"], label, feed["url"]) for feed, label, kind, items, f in results if label not in ("ok", "304")]
    print(f"{seen:%Y-%m-%d %H:%M} feeds {len(results)} items {len(rows)} new {sum(new_by_feed.values())} "
          f"changed {sum(changed_by_feed.values())} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for d, label, url in sorted(problems):
        print(f"  {label:12} {d:24} {url}")
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and parse, write nothing")
    ap.add_argument("--feed", help="one feed URL: fetch it and print its items, write nothing")
    ap.add_argument("--domain", help="only this site's feeds")
    ap.add_argument("--feeds", default=FEEDS_PATH, help="feeds.json to use")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-robots", action="store_true", help="(for --feed only) skip the robots.txt check")
    a = ap.parse_args()
    if a.feed:
        label, fmt, items, f = read_feed(a.feed, robots=None if a.no_robots else Robots())
        print(f"{label} {fmt} http={f.status} bytes={len(f.body)} items={len(items)} {f.error}")
        for it in items[:400]:
            print(f"{str(it['ts'] or ''):19}\t{it['url']}\t{it['title']}")
        return
    feeds = load_feeds(a.feeds)
    if a.domain:
        feeds = [f for f in feeds if f["domain"] == a.domain]
    if not feeds:
        sys.exit("no feeds")
    run(feeds, dry_run=a.dry_run, workers=a.workers)


if __name__ == "__main__":
    main()
