#!/usr/bin/env python3
"""Build `unified.headlines`: GDELT, the news collector and the blog
collector in one table, with copy flags that are exact for every choice of
sources. The proof of concept of 2026-09-15 (root repo:
sessions/2026-09-15-unifying-the-sources.md); the table and its flags are
described in scripts/unified_schema.sql.

Sources and their hard start dates (a source contributes nothing earlier):
  gdelt  from START, every row of default.headlines with its copy flag
  feeds  from FEEDS_START: collect.headlines, one row per URL (the news
         sitemap's title before the feed's), dated by the item's own time
         but never later than first seen, and left out when first seen
         more than FEED_MAX_LAG_DAYS after it (a feed added later showing
         old items, the NYT Wayback backfill); English only (BBC's World
         Service languages by URL section, then any title whose letters are
         mostly outside A-Z)
  blogs  from BLOGS_START: blogs.posts, one row per URL, English only
         (Substack's own language field; Medium's guess); the site without
         a leading www., as GDELT names sites and the search page's source
         box cleans what is typed (www.slowboring.com is slowboring.com)

A run works out which days need building and builds them oldest first, each
through a shadow table and REPLACE PARTITION:
  - every day whose row count per source differs from the table's (new
    rows in GDELT, the feeds or the blogs, including a catch-up filling old
    days), and the seven days after each, whose flags a new row can change;
  - the last SETTLE_DAYS days and today, always: a feed row's "GDELT has
    this URL" mark looks seven days ahead, and GDELT's files arrive up to a
    day late.
A day's flags come from that day's rows, windowed as scripts/copyflag.py's
ingest_select does, and the latest sighting of each story in the seven days
before, from the table itself (and from default.headlines before START).

  scripts/unify.py run                 what the timer runs (server/unify.timer)
  scripts/unify.py build --from DAY    rebuild every day from DAY (e.g. after
                                       changing a filter or the labels)
  scripts/unify.py check               the tests: GDELT's copy_news against its
                                       copy before FEEDS_START, flags recomputed
                                       in one query against the stored ones

A run does nothing while the GDELT ingest is running (both are heavy on a
4 GB box) and holds a lock so two builds never overlap.

Environment: CH_URL, CH_USER, CH_PASSWORD (the ingest user). Stdlib only.
"""

import argparse
import datetime as dt
import fcntl
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import copyflag  # noqa: E402

START = dt.date(2026, 9, 1)
FEEDS_START = dt.date(2026, 9, 12)
BLOGS_START = dt.date(2026, 9, 1)
FEED_MAX_LAG_DAYS = 3
SETTLE_DAYS = 3
WINDOW = copyflag.WINDOW_DAYS
LOCK = os.environ.get("UNIFY_LOCK", "/tmp/headlinesearch-unify.lock")

# The BBC sections GDELT itself records (bbc.com and bbc.co.uk, August and
# September 2026); every other first path segment of a BBC URL is a World
# Service language (tamil, mundo, cymrufyw, ...).
BBC_ENGLISH = ["news", "sport", "sounds", "iplayer", "weather", "newsround", "culture", "future", "programmes",
               "food", "bitesize", "mediacentre", "travel", "articles", "worklife", "reel", "innovation",
               "business", "arts", "audio", "videos", "live"]

CH_URL = os.environ.get("CH_URL", "http://127.0.0.1:8123")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASSWORD = os.environ.get("CH_PASSWORD", "")

# The flag build's settings: copyflag's for heavy statements on this box.
HEAVY = {**copyflag.HEAVY, "optimize_use_projections": 0}


def ch(query, settings=None):
    params = {"query": query}
    if settings:
        params.update({k: str(v) for k, v in settings.items()})
    req = urllib.request.Request(CH_URL + "/?" + urllib.parse.urlencode(params), data=b"", method="POST",
                                 headers={"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASSWORD})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=7200) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"clickhouse {e.code}: {e.read().decode()[:800]}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def lit(d):
    """A day as a ClickHouse DateTime literal (midnight UTC)."""
    return f"toDateTime('{d:%Y-%m-%d} 00:00:00', 'UTC')"


def schema():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "unified_schema.sql")) as fh:
        sql = "\n".join(l for l in fh.read().splitlines() if not l.lstrip().startswith("--"))
    for statement in sql.split(";"):
        if statement.strip():
            ch(statement)


# ------------------------------------------------------------------- staging

NURL = "replaceRegexpOne(replaceRegexpOne(lower({u}), '^https?://(www\\\\.)?', ''), '/+$', '')"


def stage():
    """Every feed and blog row from its start date into unified.stage, one
    per URL, filtered. The two tables are small (the feeds a few hundred
    thousand rows, the blogs a few million), so this is the whole set each
    run rather than an increment."""
    ch("TRUNCATE TABLE unified.stage")
    sections = ", ".join(f"'{s}'" for s in BBC_ENGLISH)
    ch(f"""
INSERT INTO unified.stage (ts, src, platform, domain, url, title)
SELECT t, 'feeds', '', domain, url, tt FROM (
  SELECT domain, url,
         argMin(least(ts, seen), (kind != 'sitemap', seen)) AS t,
         argMin(seen, (kind != 'sitemap', seen)) AS first_seen,
         argMin(title, (kind != 'sitemap', seen)) AS tt
  FROM collect.headlines
  WHERE version = 1 AND seen >= {lit(FEEDS_START)}
  GROUP BY domain, url)
WHERE t >= {lit(FEEDS_START)} AND t >= first_seen - INTERVAL {FEED_MAX_LAG_DAYS} DAY
  AND tt != ''
  AND countMatches(tt, '[A-Za-z]') * 2 >= countMatches(tt, '\\\\p{{L}}')
  AND NOT (domain IN ('bbc.com', 'bbc.co.uk') AND extract(url, '^https?://[^/]+/([^/]+)/') NOT IN ({sections}))""", HEAVY)
    ch(f"""
INSERT INTO unified.stage (ts, src, platform, domain, url, title)
SELECT t, 'blogs', platform, replaceRegexpOne(dm, '^www\\.', ''), url, tt FROM (
  SELECT platform, url,
         least(argMin(ts, seen), min(seen)) AS t,
         argMin(domain, seen) AS dm,
         argMin(title, seen) AS tt,
         argMin(lang, seen) AS lg
  FROM blogs.posts
  WHERE version = 1 AND ts >= {lit(BLOGS_START)}
  GROUP BY platform, url)
WHERE t >= {lit(BLOGS_START)} AND lg = 'en' AND tt != ''""", HEAVY)


# ------------------------------------------------------------------ one day

def build_day(d):
    """Rebuild one day's partition of unified.headlines; returns its rows."""
    lo, hi = lit(d), lit(d + dt.timedelta(days=1))
    part = f"{d:%Y%m%d}"
    ch("TRUNCATE TABLE unified.day")
    ch(f"""
INSERT INTO unified.day (ts, src, platform, domain, url, title, key, gcopy, in_gdelt)
SELECT ts, 'gdelt', '', domain, url, title, story_key(domain, title), copy, 0
FROM default.headlines WHERE ts >= {lo} AND ts < {hi}""", HEAVY)
    if d >= BLOGS_START:
        ch(f"""
INSERT INTO unified.day (ts, src, platform, domain, url, title, key, gcopy, in_gdelt)
SELECT ts, src, platform, domain, url, title, story_key(domain, title), 0, 0
FROM unified.stage WHERE src = 'blogs' AND ts >= {lo} AND ts < {hi}""", HEAVY)
    if d >= FEEDS_START:
        ch(f"""
INSERT INTO unified.day (ts, src, platform, domain, url, title, key, gcopy, in_gdelt)
SELECT ts, src, platform, domain, url, title, story_key(domain, title), 0,
       {NURL.format(u='url')} IN (SELECT {NURL.format(u='url')} FROM default.headlines
                                  WHERE ts >= {lo} - INTERVAL {WINDOW} DAY AND ts < {hi} + INTERVAL {WINDOW} DAY)
FROM unified.stage WHERE src = 'feeds' AND ts >= {lo} AND ts < {hi}""", HEAVY)
    n = int(ch("SELECT count() FROM unified.day").strip())

    # The seven days before: this table's rows, and default.headlines' for
    # any part of the window before START.
    ch("TRUNCATE TABLE unified.lookback")
    ch(f"""
INSERT INTO unified.lookback (ts, src, domain, key, excl)
SELECT ts, src, domain, key, src = 'feeds' AND copy_news = 255
FROM unified.headlines WHERE ts >= {lo} - INTERVAL {WINDOW} DAY AND ts < {lo}""", HEAVY)
    if d - dt.timedelta(days=WINDOW) < START:
        ch(f"""
INSERT INTO unified.lookback (ts, src, domain, key, excl)
SELECT ts, 'gdelt', domain, story_key(domain, title), 0
FROM default.headlines WHERE ts >= {lo} - INTERVAL {WINDOW} DAY AND ts < {lit(START)}""", HEAVY)

    lag = "lagInFrame(ts, 1, toDateTime(0, 'UTC')) OVER (PARTITION BY {p} ORDER BY ts, url ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)"
    flag = "multiIf({a} < ts - INTERVAL %d DAY, 0, {d} < ts - INTERVAL %d DAY, 1, 2)" % (WINDOW, WINDOW)
    own = flag.format(a="own_any", d="own_dom")
    news = flag.format(a="news_any", d="news_dom")
    # news: the row takes part in the GDELT-and-feeds window. A blog row or a
    # feed row GDELT has (in_gdelt) is windowed apart and its news flag unused.
    select = f"""
SELECT ts, src, platform, domain, url, title, key,
       if(src = 'gdelt', gcopy, {own}) AS copy,
       multiIf(src = 'blogs', {own}, in_gdelt = 1, 255, {news}) AS copy_news
FROM (
  SELECT ts, src, platform, domain, url, title, key, gcopy, in_gdelt,
         greatest({lag.format(p='src, key')}, oa_last) AS own_any,
         greatest({lag.format(p='src, key, domain')}, od_last) AS own_dom,
         greatest({lag.format(p='news, key')}, if(news, na_last, toDateTime(0, 'UTC'))) AS news_any,
         greatest({lag.format(p='news, key, domain')}, if(news, nd_last, toDateTime(0, 'UTC'))) AS news_dom
  FROM (SELECT *, src != 'blogs' AND in_gdelt = 0 AS news FROM unified.day) AS i
  LEFT JOIN (SELECT src AS oa_src, key AS oa_key, max(ts) AS oa_last FROM unified.lookback GROUP BY src, key) AS oa
    ON oa_src = i.src AND oa_key = i.key
  LEFT JOIN (SELECT src AS od_src, key AS od_key, domain AS od_domain, max(ts) AS od_last FROM unified.lookback GROUP BY src, key, domain) AS od
    ON od_src = i.src AND od_key = i.key AND od_domain = i.domain
  LEFT JOIN (SELECT key AS na_key, max(ts) AS na_last FROM unified.lookback WHERE src != 'blogs' AND excl = 0 GROUP BY key) AS na
    ON na_key = i.key
  LEFT JOIN (SELECT key AS nd_key, domain AS nd_domain, max(ts) AS nd_last FROM unified.lookback WHERE src != 'blogs' AND excl = 0 GROUP BY key, domain) AS nd
    ON nd_key = i.key AND nd_domain = i.domain
)"""
    ch(f"ALTER TABLE unified.headlines_new DROP PARTITION {part}")
    if n:
        ch(f"INSERT INTO unified.headlines_new (ts, src, platform, domain, url, title, key, copy, copy_news) {select}",
           {**HEAVY, "max_insert_threads": 1, "join_use_nulls": 0})
    m = int(ch(f"SELECT count() FROM unified.headlines_new WHERE toYYYYMMDD(ts) = {part}").strip())
    if m != n:
        raise RuntimeError(f"{d}: {m} rows built, {n} expected; stopping")
    if n:
        ch(f"ALTER TABLE unified.headlines REPLACE PARTITION {part} FROM unified.headlines_new")
    else:
        ch(f"ALTER TABLE unified.headlines DROP PARTITION {part}")
    ch(f"ALTER TABLE unified.headlines_new DROP PARTITION {part}")
    return n


# --------------------------------------------------------------- which days

def counts(sql):
    out = {}
    for line in ch(sql + " FORMAT TSV", HEAVY).splitlines():
        d, src, n = line.split("\t")
        out[(d, src)] = int(n)
    return out


def dirty_days(today):
    want = counts(f"SELECT toDate(ts) AS d, 'gdelt', count() FROM default.headlines WHERE ts >= {lit(START)} GROUP BY d")
    want.update(counts("SELECT toDate(ts) AS d, src, count() FROM unified.stage GROUP BY d, src"))
    have = counts("SELECT toDate(ts) AS d, src, count() FROM unified.headlines GROUP BY d, src")
    changed = {dt.date.fromisoformat(d) for (d, s) in set(want) | set(have) if want.get((d, s), 0) != have.get((d, s), 0)}
    days = set()
    for d in changed:
        days.update(d + dt.timedelta(days=k) for k in range(WINDOW + 1))
    days.update(today - dt.timedelta(days=k) for k in range(SETTLE_DAYS + 1))
    return sorted(x for x in days if START <= x <= today), sorted(changed)


def sources():
    ch("DROP TABLE IF EXISTS unified.sources_new")
    ch("CREATE TABLE unified.sources_new AS unified.sources")
    ch("INSERT INTO unified.sources_new (src, platform, domain, n, first, last) "
       "SELECT src, platform, domain, count(), min(ts), max(ts) FROM unified.headlines GROUP BY src, platform, domain", HEAVY)
    ch("EXCHANGE TABLES unified.sources AND unified.sources_new")
    ch("DROP TABLE unified.sources_new")


def build(days, why):
    if not days:
        log("nothing to build")
        return
    ch("CREATE TABLE IF NOT EXISTS unified.headlines_new AS unified.headlines")
    ch("ALTER TABLE unified.headlines_new MODIFY SETTING text_index_max_memory_usage_before_flush = 268435456")
    started = time.time()
    total = 0
    log(f"building {len(days)} days, {days[0]} to {days[-1]} ({why})")
    for d in days:
        t = time.time()
        n = build_day(d)
        total += n
        log(f"{d} {n} rows {time.time() - t:.0f}s")
    sources()
    log(f"done: {len(days)} days, {total} rows, {(time.time() - started) / 60:.1f} min")


# -------------------------------------------------------------------- check

def check():
    """The flags against what they should be."""
    # 1. Before the feeds start, GDELT's news flag must be GDELT's own.
    r = ch(f"SELECT count(), countIf(copy_news != copy) FROM unified.headlines WHERE src = 'gdelt' AND ts < {lit(FEEDS_START)} FORMAT TSV").split()
    print(f"gdelt rows before {FEEDS_START}: {r[0]}, copy_news differing from copy: {r[1]}")
    # 2. Recompute every flag in one query over the whole table (no day
    # boundaries) and compare, for the days whose seven-day look-back lies
    # inside the table: GDELT's own copy is not recomputed (it is carried).
    lo = lit(START + dt.timedelta(days=WINDOW))
    lag = "lagInFrame(ts, 1, toDateTime(0, 'UTC')) OVER (PARTITION BY {p} ORDER BY ts, url ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)"
    flag = "multiIf({a} < ts - INTERVAL %d DAY, 0, {d} < ts - INTERVAL %d DAY, 1, 2)" % (WINDOW, WINDOW)
    r = ch(f"""
SELECT count(), countIf(src != 'gdelt' AND copy != own), countIf(src = 'blogs' AND copy_news != own),
       countIf(src != 'blogs' AND copy_news != 255 AND copy_news != nws)
FROM (
  SELECT ts, src, copy, copy_news, {flag.format(a='oa', d='od')} AS own, {flag.format(a='na', d='nd')} AS nws
  FROM (
    SELECT ts, src, copy, copy_news,
           {lag.format(p='src, key')} AS oa, {lag.format(p='src, key, domain')} AS od,
           {lag.format(p='news, key')} AS na, {lag.format(p='news, key, domain')} AS nd
    FROM (SELECT *, src != 'blogs' AND copy_news != 255 AS news FROM unified.headlines)))
WHERE ts >= {lo} FORMAT TSV""", HEAVY).split()
    print(f"rows from {START + dt.timedelta(days=WINDOW)}: {r[0]}; own copy differing (feeds, blogs): {r[1]}; "
          f"blog copy_news differing: {r[2]}; news copy_news differing: {r[3]}")


# --------------------------------------------------------------------- main

def ingest_running():
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "ingest.service"]).returncode == 0
    except FileNotFoundError:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    p = sub.add_parser("build")
    p.add_argument("--from", dest="day_from", required=True, help="YYYY-MM-DD")
    sub.add_parser("check")
    a = ap.parse_args()

    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another unify run holds the lock; nothing to do")
        return 0
    if a.cmd == "check":
        check()
        return 0
    if ingest_running():
        log("the GDELT ingest is running; next time")
        return 0
    schema()
    today = dt.datetime.now(dt.timezone.utc).date()
    t = time.time()
    stage()
    log("staged: " + ch("SELECT src, count() FROM unified.stage GROUP BY src ORDER BY src FORMAT TSV").replace("\t", " ").replace("\n", ", ").rstrip(", ")
        + f" ({time.time() - t:.0f}s)")
    if a.cmd == "build":
        first = max(dt.date.fromisoformat(a.day_from), START)
        build([first + dt.timedelta(days=k) for k in range((today - first).days + 1)], f"from {first}")
    else:
        days, changed = dirty_days(today)
        build(days, f"changed: {', '.join(str(d) for d in changed) or 'none'}; and the last {SETTLE_DAYS} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
