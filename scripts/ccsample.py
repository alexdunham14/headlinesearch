#!/usr/bin/env python3
"""A look inside Common Crawl's news crawl (CC-NEWS) without ingesting it:
stream a few of its WARC files from random days, keep only the URL, the
crawl time and the page's <title>, and summarise what is there. Stdlib only.

  scripts/ccsample.py list 2026/08                       # the month's files
  scripts/ccsample.py sample --months 2024/03,2025/03,2026/08 --per-month 2 --out cc.tsv
  scripts/ccsample.py summary cc.tsv [--lost lost_outlets.tsv]

CC-NEWS is one WARC file (about a gigabyte, gzipped) per crawler batch,
some sixteen a day, listed in crawl-data/CC-NEWS/YYYY/MM/warc.paths.gz.
There is no index, so the only way to see what a day holds is to read its
files. `sample` streams each chosen file record by record and never holds
more than one page in memory; a file costs its download and a minute of
CPU. The TSV it writes has file, crawl time, URL and title.

`summary` reports records, distinct sites, the biggest sites, and how many
of the sites in a lost-outlets list (scripts/collect.py's reason for being)
appear at all, with their counts.
"""

import argparse
import collections
import gzip
import html
import random
import re
import sys
import time
import urllib.error
import urllib.request
import zlib

BASE = "https://data.commoncrawl.org/"
UA = "headlinesearch/0.1 (+https://newsheadlinesearch.com)"
TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_RE = re.compile(rb"""<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']*)["']""", re.I)
TWO_LEVEL = {"co.uk", "com.au", "co.nz", "co.za", "com.ph", "co.in", "org.uk", "gov.uk", "ac.uk", "net.au", "org.au",
             "com.br", "com.mx", "co.jp", "com.sg", "com.hk", "com.pk", "com.bd", "com.ng", "co.ke", "com.my", "com.tr"}


def month_paths(month):
    with urllib.request.urlopen(urllib.request.Request(f"{BASE}crawl-data/CC-NEWS/{month}/warc.paths.gz", headers={"User-Agent": UA}), timeout=60) as r:
        return gzip.decompress(r.read()).decode().split()


def site_of(url):
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def records(stream):
    """Yield (warc headers dict, block bytes) from a WARC file's gzip stream.
    Each record is one gzip member; GzipFile reads the members in turn."""
    g = gzip.GzipFile(fileobj=stream)
    while True:
        line = g.readline()
        if not line:
            return
        if not line.startswith(b"WARC/"):
            continue
        headers = {}
        while True:
            line = g.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", "0"))
        block = g.read(n)
        g.readline(); g.readline()   # the record's trailing CRLF CRLF
        yield headers, block


def title_of(block):
    """The <title> (or og:title) of an HTTP response block: the HTTP headers,
    then the body, which may itself be gzip or deflate encoded."""
    head, _, body = block.partition(b"\r\n\r\n")
    enc = re.search(rb"(?im)^content-encoding:\s*(\S+)", head)
    if enc:
        try:
            if enc.group(1).lower() == b"gzip":
                body = gzip.decompress(body)
            elif enc.group(1).lower() == b"deflate":
                body = zlib.decompress(body, -zlib.MAX_WBITS)
        except Exception:
            return ""
    body = body[:200_000]
    m = OG_RE.search(body) or TITLE_RE.search(body)
    if not m:
        return ""
    t = html.unescape(m.group(1).decode("utf-8", "replace"))
    return re.sub(r"\s+", " ", t).strip()[:300]


def sample_file(path, out):
    url = BASE + path
    name = path.rsplit("/", 1)[-1]
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120) as r:
                n = 0
                t0 = time.monotonic()
                for headers, block in records(r):
                    if headers.get("warc-type") != "response":
                        continue
                    target = headers.get("warc-target-uri", "")
                    when = headers.get("warc-date", "")[:19].replace("T", " ")
                    title = title_of(block).replace("\t", " ")
                    out.write(f"{name}\t{when}\t{target}\t{title}\n")
                    n += 1
                    if n % 5000 == 0:
                        print(f"  {name}: {n} pages, {int(time.monotonic() - t0)}s", file=sys.stderr)
                print(f"{name}: {n} pages in {int(time.monotonic() - t0)}s", file=sys.stderr)
                return n
        except (urllib.error.HTTPError, urllib.error.URLError, ConnectionError, TimeoutError, EOFError, OSError) as e:
            wait = 20 * (attempt + 1)
            print(f"{name}: {e}; retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    print(f"{name}: gave up", file=sys.stderr)
    return 0


def cmd_sample(a):
    rnd = random.Random(a.seed)
    chosen = []
    for month in a.months.split(","):
        paths = month_paths(month.strip())
        by_day = collections.defaultdict(list)
        for p in paths:
            m = re.search(r"CC-NEWS-(\d{8})", p)
            if m:
                by_day[m.group(1)].append(p)
        days = rnd.sample(sorted(by_day), min(a.days_per_month, len(by_day)))
        for d in days:
            chosen.extend(rnd.sample(by_day[d], min(a.per_day, len(by_day[d]))))
    print(f"{len(chosen)} files:\n  " + "\n  ".join(chosen), file=sys.stderr)
    with open(a.out, "a") as out:
        for p in chosen:
            sample_file(p, out)


def cmd_list(a):
    for p in month_paths(a.month):
        print(p)


def cmd_summary(a):
    lost = {}
    if a.lost:
        with open(a.lost) as fh:
            for i, line in enumerate(fh):
                cols = line.rstrip("\n").split("\t")
                if i == 0 and cols[0] == "domain":
                    continue
                lost[cols[0]] = cols[1] if len(cols) > 1 else ""
    n = titled = 0
    sites = collections.Counter()
    files = collections.Counter()
    days = collections.Counter()
    with open(a.tsv) as fh:
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 4:
                continue
            n += 1
            files[cols[0]] += 1
            days[cols[1][:10]] += 1
            sites[site_of(cols[2])] += 1
            if cols[3]:
                titled += 1
    print(f"pages {n}  with a title {titled}  files {len(files)}  sites {len(sites)}")
    print(f"pages a file: {n // max(1, len(files))}; at ~16 files a day that is ~{16 * n // max(1, len(files))} a day, "
          f"~{16 * len(sites) // max(1, len(files))}+ sites a day")
    print("days: " + ", ".join(f"{d} {c}" for d, c in sorted(days.items())))
    top = sites.most_common(40)
    print(f"top sites ({sum(c for _, c in top) * 100 // max(1, n)}% of pages):")
    for s, c in top:
        print(f"  {c:7} {s}")
    if lost:
        present = [(s, sites[s]) for s in lost if sites.get(s)]
        present.sort(key=lambda x: -x[1])
        print(f"lost outlets present: {len(present)} of {len(lost)}")
        for s, c in present[:80]:
            print(f"  {c:7} {s}  (was {float(lost[s]):.0f} a year in GDELT)")
        absent = [s for s in list(lost)[:60] if not sites.get(s)]
        print("absent, of the sixty biggest: " + ", ".join(absent))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("month"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("sample")
    p.add_argument("--months", required=True, help="YYYY/MM, comma separated")
    p.add_argument("--days-per-month", type=int, default=1)
    p.add_argument("--per-day", type=int, default=2, help="files per chosen day")
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_sample)
    p = sub.add_parser("summary"); p.add_argument("tsv"); p.add_argument("--lost"); p.set_defaults(fn=cmd_summary)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
