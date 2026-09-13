#!/usr/bin/env python3
"""Backfill the collector's database from the Wayback Machine's captures of
a feed, for the years GDELT lost. Stdlib only; the parser and the insert
are scripts/collect.py's.

  scripts/backfill.py list URL [--from 2020 --to 2026]      # capture times, one an hour at most
  scripts/backfill.py run --domain nytimes.com [--from 2022] [--to 2024] [--kind rss] [--limit N]
  scripts/backfill.py run --feed URL --domain D --kind rss --section world

The Wayback Machine captured the section feeds of the big papers far more
often than their sitemaps (NYT World RSS: 315, 325 and 273 capture days in
2022 to 2024; the Post's world feed 361 days in 2020). Each capture is the
feed as it was that hour: the top of the section, ten to fifty stories.
Walking every capture of a feed, oldest first, and storing every (URL,
kind, title) not yet stored gives the section's headlines for those years
with the first hour each was seen, and its later titles where the feed
showed a change. The CDX index (web.archive.org/cdx/search/cdx) lists the
captures; each is fetched raw (the `id_` flag: the bytes as archived, no
rewriting) and parsed exactly as a live feed would be, with `seen` set to
the capture time, so versions and edits mean the same as in the hourly
collector. Rows land in collect.headlines with feed = the original feed
URL; nothing marks them as backfilled but their `seen` dates.

Resumable: collect.backfill records each capture done (feed, capture, the
counts); a run skips those. Polite: one request a second to
web.archive.org, and a pause of a minute on a 429 or a 5xx.
"""

import argparse
import datetime as dt
import json
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import collect  # noqa: E402

CDX = "https://web.archive.org/cdx/search/cdx"
PAUSE = 1.0


def captures(url, year_from, year_to):
    """Capture timestamps (YYYYMMDDHHMMSS) of a URL with status 200, at most
    one an hour, oldest first."""
    q = urllib.parse.urlencode({"url": url, "output": "json", "fl": "timestamp,statuscode",
                                "filter": "statuscode:200", "collapse": "timestamp:10",
                                "from": str(year_from), "to": str(year_to)})
    for attempt in range(5):
        f = collect.fetch(f"{CDX}?{q}", timeout=120)
        if f.status == 200:
            try:
                rows = json.loads(f.body or b"[]")
            except ValueError:
                rows = []
            return [r[0] for r in rows[1:]]
        time.sleep(30 * (attempt + 1))
    raise RuntimeError(f"cdx failed for {url}: {f.status} {f.error}")


def done_captures(feed):
    q = f"SELECT capture FROM collect.backfill FINAL WHERE feed = {{f:String}} FORMAT TSV"
    out = collect.ch(q, settings={"param_f": feed})
    return {line.strip() for line in out.splitlines() if line.strip()}


def fetch_capture(ts, url):
    """The archived bytes of a capture; None after retries."""
    wb = f"https://web.archive.org/web/{ts}id_/{url}"
    for attempt in range(4):
        f = collect.fetch(wb, timeout=90)
        if f.status == 200:
            return f
        if f.status in (404, 410):
            return f
        time.sleep(60 if f.status in (429, 0) or f.status >= 500 else 5)
    return f


def run_feed(feed, year_from, year_to, limit=None, batch=24):
    url, domain, kind, section = feed["url"], feed["domain"], feed["kind"], feed.get("section", "")
    todo = [c for c in captures(url, year_from, year_to) if c not in done_captures(url)]
    if limit:
        todo = todo[:limit]
    print(f"{domain} {section or kind}: {len(todo)} captures to do ({url})", flush=True)
    rows, log, n_done = [], [], 0
    keyed = set()   # (url, title) already staged in this batch: a headline in twenty captures is one row

    def flush():
        nonlocal rows, log
        keyed.clear()
        if rows:
            collect.ch("TRUNCATE TABLE collect.staging_backfill")
            collect.ch("INSERT INTO collect.staging_backfill FORMAT JSONEachRow",
                       "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode())
            collect.ch(collect.insert_new_sql("collect.staging_backfill"))
            collect.ch("TRUNCATE TABLE collect.staging_backfill")
        if log:
            collect.ch("INSERT INTO collect.backfill FORMAT JSONEachRow",
                       "\n".join(json.dumps(r, ensure_ascii=False) for r in log).encode())
        rows, log = [], []

    for ts in todo:
        seen = dt.datetime.strptime(ts, "%Y%m%d%H%M%S")
        f = fetch_capture(ts, url)
        label, fmt, items = "error", "", []
        if f.status == 200:
            root = collect.parse_xml(f.body)
            if root is not None:
                fmt, items = collect.parse_items(root)
                items = [i for i in items if i["url"] and i["title"]]
                label = "ok" if fmt in ("rss", "atom", "sitemap") else "parse"
            else:
                label = "parse"
        elif f.status in (404, 410):
            label = f"http {f.status}"
        stored_kind = "sitemap" if fmt == "sitemap" else "feed"
        for it in items:
            key = (it["url"], stored_kind, it["title"])
            if key in keyed:
                continue
            keyed.add(key)
            rows.append({"ts": (it["ts"] or seen).strftime("%Y-%m-%d %H:%M:%S"), "domain": domain, "url": it["url"],
                         "title": it["title"], "seen": seen.strftime("%Y-%m-%d %H:%M:%S"), "feed": url,
                         "kind": stored_kind, "section": section, "guid": it["guid"]})
        log.append({"feed": url, "capture": ts, "label": label, "items": len(items), "http": f.status,
                    "done": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
        n_done += 1
        if len(log) >= batch:
            flush()
            print(f"  {ts[:10]} {n_done}/{len(todo)}", flush=True)
        time.sleep(PAUSE)
    flush()
    print(f"{domain} {section or kind}: done {n_done}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("url")
    p.add_argument("--from", dest="year_from", type=int, default=2019)
    p.add_argument("--to", dest="year_to", type=int, default=dt.date.today().year)
    p = sub.add_parser("run")
    p.add_argument("--feed", help="one feed URL (with --domain, --kind, --section)")
    p.add_argument("--domain", required=True)
    p.add_argument("--kind", default=None, help="with --domain: only feeds of this kind (rss or sitemap)")
    p.add_argument("--section", default="")
    p.add_argument("--from", dest="year_from", type=int, default=2019)
    p.add_argument("--to", dest="year_to", type=int, default=dt.date.today().year)
    p.add_argument("--limit", type=int, help="captures per feed, for a trial")
    p.add_argument("--feeds", default=collect.FEEDS_PATH)
    a = ap.parse_args()
    if a.cmd == "list":
        for ts in captures(a.url, a.year_from, a.year_to):
            print(ts)
        return
    if a.feed:
        feeds = [{"url": a.feed, "domain": a.domain, "kind": a.kind or "rss", "section": a.section}]
    else:
        feeds = [f for f in collect.load_feeds(a.feeds) if f["domain"] == a.domain and (not a.kind or f["kind"] == a.kind)]
    if not feeds:
        sys.exit("no feeds")
    for feed in feeds:
        run_feed(feed, a.year_from, a.year_to, a.limit)


if __name__ == "__main__":
    main()
