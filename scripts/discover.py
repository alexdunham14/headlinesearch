#!/usr/bin/env python3
"""Find the feeds and news sitemaps of a list of sites, for scripts/feeds.json.
Stdlib only; shares its fetching and parsing with scripts/collect.py.

  scripts/discover.py nytimes.com politico.com > found.jsonl
  scripts/discover.py --tsv lost_outlets.tsv --workers 12 --summary summary.tsv > found.jsonl

For each site, with the same User-Agent as the collector: robots.txt (its
Sitemap: lines, and whether it turns unnamed bots away from the whole site),
the home page (its <link rel="alternate"> feeds and any link that looks like
a feed), and the paths feeds and news sitemaps usually live at. Every
candidate is then fetched once and classified as the collector would: ok
(with the format, the item count, the newest date and a sample title),
blocked, html, robots, no-titles, parse, error or an HTTP status. Sites
whose robots.txt disallows everything for bots it does not name are not
probed beyond robots.txt and the home page; their Sitemap: lines are
reported so the decision can be made by hand.

Nothing here fetches an article page. The home page is the one page it
loads, which is what any feed reader does.

Output: one JSON object per candidate on stdout; with --summary, one line
per site: domain, home label, robots verdict, best sitemap and its item
count, number of working feeds, and what was blocked.
"""

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import re
import sys
import urllib.parse
import urllib.robotparser

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from collect import Robots, UA_TOKEN, classify, fetch, read_feed  # noqa: E402

SITEMAP_PATHS = [
    "/news-sitemap.xml", "/sitemap-news.xml", "/sitemap_news.xml", "/sitemaps/news.xml", "/sitemap/news.xml",
    "/news/sitemap.xml", "/googlenews.xml", "/google-news-sitemap.xml", "/news_sitemap.xml", "/sitemap-news-index.xml",
    "/arc/outboundfeeds/news-sitemap/?outputType=xml", "/arc/outboundfeeds/news-sitemap-index/?outputType=xml",
    "/sitemap.xml",
]
FEED_PATHS = [
    "/rss", "/feed", "/rss.xml", "/feed.xml", "/feeds/rss", "/rss/index.xml", "/index.rss", "/atom.xml", "/rss/",
    "/feed/", "/arc/outboundfeeds/rss/?outputType=xml", "/?format=rss", "/news/feed/", "/rss/news", "/rss/headlines",
    "/feeds/news.rss", "/rss/news.xml", "/feeds/latest.rss", "/feeds/posts/default",
    "/search/?f=rss&t=article&l=50&s=start_time&sd=desc",   # BLOX (TownNews), most Lee and CNHI dailies
]
LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""([a-zA-Z:-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


def attrs(tag):
    return {m.group(1).lower(): html.unescape(m.group(3) or m.group(4) or m.group(5) or "") for m in ATTR_RE.finditer(tag)}


def home_feeds(base, body):
    """Feed URLs advertised by a page: <link rel=alternate type=rss/atom>, then
    anchors whose href looks like a feed."""
    page = body[:600_000].decode("utf-8", "replace")
    out = []
    for tag in LINK_RE.findall(page):
        a = attrs(tag)
        if "alternate" in a.get("rel", "").lower() and ("rss" in a.get("type", "") or "atom" in a.get("type", "")) and a.get("href"):
            out.append(urllib.parse.urljoin(base, a["href"]))
    for href in HREF_RE.findall(page):
        href = html.unescape(href)
        h = href.lower()
        if ("rss" in h or "/feed" in h or ".xml" in h) and not h.endswith((".css", ".js", ".png", ".svg")) and "sitemap" not in h:
            u = urllib.parse.urljoin(base, href)
            if u not in out:
                out.append(u)
        if len(out) > 25:
            break
    return out


def probe_site(domain):
    """Everything found for one site: a dict with home, robots, and candidates."""
    site = {"domain": domain, "home": "", "robots_all": "", "robots_sitemaps": [], "candidates": []}
    base = None
    for host in (f"www.{domain}", domain):
        f = fetch(f"https://{host}/robots.txt", timeout=15)
        if f.status and f.status not in (0,) and not (f.status == 404 and host.startswith("www.") is False):
            base = f"https://{host}"
            break
        if f.status == 0:
            continue
        base = f"https://{host}"
        break
    if base is None:
        site["home"] = "unreachable"
        return site
    robots = Robots()
    rf = fetch(base + "/robots.txt", timeout=15)
    if rf.status == 200 and not classify(rf) == "blocked" and b"<html" not in rf.body[:500].lower():
        text = rf.body.decode("utf-8", "replace")
        site["robots_sitemaps"] = [m.strip() for m in re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", text)]
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines())
        site["robots_all"] = "disallow-all" if not rp.can_fetch(UA_TOKEN, base + "/") else "ok"
    elif rf.status in (401, 403) or classify(rf) == "blocked":
        site["robots_all"] = f"blocked ({rf.status})"
    elif rf.status == 0 or rf.status >= 500:
        site["robots_all"] = f"unreachable ({rf.status})"
    else:
        site["robots_all"] = f"none ({rf.status})"

    hf = fetch(base + "/", timeout=20)
    site["home"] = classify(hf)
    if hf.url and urllib.parse.urlsplit(hf.url).netloc:
        base = f"{urllib.parse.urlsplit(hf.url).scheme}://{urllib.parse.urlsplit(hf.url).netloc}"
    if site["home"] == "html" and hf.status == 200:
        site["home"] = "ok"
    if site["robots_all"].startswith(("disallow-all", "unreachable")):
        return site

    cands = []
    for u in site["robots_sitemaps"]:
        if re.search(r"news|google", u, re.I):
            cands.append(("robots-sitemap", u))
    if not any(k == "robots-sitemap" for k, _ in cands):
        for u in site["robots_sitemaps"][:3]:
            cands.append(("robots-sitemap", u))
    for p in SITEMAP_PATHS:
        cands.append(("path-sitemap", base + p))
    if site["home"] == "ok":
        for u in home_feeds(base, hf.body):
            cands.append(("home-link", u))
    for p in FEED_PATHS:
        cands.append(("path-feed", base + p))
    seen = set()
    sitemaps_ok = feeds_ok = 0
    for how, u in cands:
        if u in seen:
            continue
        seen.add(u)
        # Enough: one working news sitemap, and three working feeds beyond the
        # ones the home page advertises (those are all tried).
        if how in ("robots-sitemap", "path-sitemap") and sitemaps_ok >= 1:
            continue
        if how == "path-feed" and feeds_ok >= 3:
            continue
        label, fmt, items, f = read_feed(u, robots=robots)
        c = {"domain": domain, "how": how, "url": u, "label": label, "format": fmt, "http": f.status,
             "items": len(items), "final_url": f.url if f.url != u else ""}
        if items:
            dated = [i["ts"] for i in items if i["ts"]]
            c["newest"] = max(dated).strftime("%Y-%m-%d %H:%M") if dated else ""
            c["oldest"] = min(dated).strftime("%Y-%m-%d %H:%M") if dated else ""
            c["sample"] = items[0]["title"][:120]
            c["sample_url"] = items[0]["url"]
        site["candidates"].append(c)
        if label == "ok" and fmt == "sitemap":
            sitemaps_ok += 1
        elif label == "ok" and fmt in ("rss", "atom") and items:
            feeds_ok += 1
    return site


def summarise(site):
    ok = [c for c in site["candidates"] if c["label"] == "ok"]
    sitemaps = sorted([c for c in ok if c["format"] == "sitemap"], key=lambda c: -c["items"])
    feeds = [c for c in ok if c["format"] in ("rss", "atom")]
    blocked = sorted({c["label"] for c in site["candidates"] if c["label"] in ("blocked", "robots")})
    best = f"{sitemaps[0]['url']} ({sitemaps[0]['items']}, newest {sitemaps[0].get('newest', '')})" if sitemaps else ""
    return "\t".join([site["domain"], site["home"], site["robots_all"], best, str(len(feeds)),
                      ",".join(blocked), " ".join(site["robots_sitemaps"][:4])])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("domains", nargs="*")
    ap.add_argument("--tsv", help="a TSV whose first column is the domain (a header line is skipped)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--summary", help="write one line per site here")
    a = ap.parse_args()
    domains = list(a.domains)
    if a.tsv:
        with open(a.tsv) as fh:
            for i, line in enumerate(fh):
                d = line.split("\t")[0].strip()
                if d and not (i == 0 and d == "domain"):
                    domains.append(d)
    if not domains:
        sys.exit("no domains")
    summary = open(a.summary, "w") if a.summary else None
    if summary:
        summary.write("domain\thome\trobots\tbest_sitemap\tfeeds\tblocked\trobots_sitemaps\n")
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for site in ex.map(probe_site, domains):
            done += 1
            for c in site["candidates"]:
                print(json.dumps(c, ensure_ascii=False))
            sys.stdout.flush()
            line = summarise(site)
            if summary:
                summary.write(line + "\n")
                summary.flush()
            print(f"[{done}/{len(domains)}] {line}", file=sys.stderr)
    if summary:
        summary.close()


if __name__ == "__main__":
    main()
