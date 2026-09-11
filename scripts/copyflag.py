#!/usr/bin/env python3
"""Site labels and the copy flag: what makes "stories" and "outlets" countable.

A headline reaches the archive many times over: a wire story on three hundred
local sites, one company's story on each of its seven hundred station pages,
a group's story under each of its mastheads. Counting rows counts all of
that; counting distinct titles misses the site label some sites append
("... | Sunny 102.3 FM", "... - Jamaica Observer"), so one story on a hundred
station pages is a hundred titles. Two things fix this, both kept in the
database so that every query, the ingest and the backfill agree:

1. `labels`: (domain, tail) pairs where `tail` is what follows the last
   " | ", " - ", " – " or " — " in a title and that domain has used it on
   at least LABEL_MIN of its titles over the archive. The SQL functions
   `strip_label` and `story_key` (scripts/schema.sql) cut such a tail off,
   twice over for "headline | section | site", and leave every other title
   alone. A label is a site's or a section's name, never a headline's own
   second half, because a headline's second half does not recur twenty times
   on one site. `title` itself stays as GDELT recorded it.

2. `copy`: one byte per row, from the row's story key and the seven days
   before it: 0 = first sighting of the key anywhere (a story), 1 = first
   sighting on this domain, seen elsewhere before (an outlet's copy),
   2 = a repeat on the same domain (a company's other pages). So
   countIf(copy = 0) is stories, countIf(copy <= 1) outlets, count()
   articles, all answered from the text index plus one small column.
   "Before" means the previous sighting is within seven days; a title that
   comes back after a quiet week is a story again. Ties at one timestamp
   (GDELT's are fifteen-minute batches) are ordered by URL.

The ingest (scripts/ingest.py) sets the flag for each batch it inserts, from
the batch itself and the seven days already in the table; ingest_select()
below is that query. This script builds the labels and, once, the flag:

  copyflag.py labels     rebuild `labels` from the whole archive (about ten
                         minutes; rerun now and then, no backfill needed
                         afterwards since a label only changes new keys)
  copyflag.py backfill   rebuild every partition of `headlines` with the
                         flag, newest first, one partition at a time through
                         a shadow table and REPLACE PARTITION (hours; the
                         site stays up but is slow, and until a partition is
                         rebuilt a source-filtered search over it is slow,
                         because REPLACE PARTITION needs both tables to
                         declare the same projection, so by_domain is
                         redefined with the column first and each partition
                         gets it back as it is rebuilt). Resumable: a
                         partition with any flagged row is skipped.
  copyflag.py status     which partitions carry the flag

Environment: CH_URL, CH_USER, CH_PASSWORD, as for ingest.py. Stdlib only.
"""

import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LABEL_MIN = 20
WINDOW_DAYS = 7
MIN_FREE_GB = 4  # pause the backfill under this much free disk on the data volume
DATA_DIR = "/var/lib/clickhouse"

# ClickHouse string literals: \\| is re2's \|, \\x{2013} the en dash, \\x{2014}
# the em dash (written as escapes so that no tool along the way can turn
# them into hyphens). Group 1 is the head, 2 the separator, 3 the tail.
SEP = r"'^(.*) (\\||-|\\x{2013}|\\x{2014}) (.*)$'"

# The window part shared by the backfill and the ingest: the previous
# sighting of the key anywhere and on this domain, both within the rows of
# `inner`, which must provide ts, domain, url, title, key (and, for the
# ingest, seen_any / seen_dom from the table: the latest sighting before
# the batch, or 1970 when there was none).
FLAG = "multiIf(prev_any < ts - INTERVAL %d DAY, 0, prev_dom < ts - INTERVAL %d DAY, 1, 2)" % (WINDOW_DAYS, WINDOW_DAYS)
LAG = "lagInFrame(ts, 1, toDateTime(0, 'UTC')) OVER (PARTITION BY %s ORDER BY ts, url ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)"


def backfill_select(start, end):
    """SELECT ts, domain, url, title, copy for the rows in [start, end), with
    the previous WINDOW_DAYS days read for the look-back."""
    return f"""
SELECT ts, domain, url, title, {FLAG} AS copy
FROM (
  SELECT ts, domain, url, title, {LAG % 'key'} AS prev_any, {LAG % 'key, domain'} AS prev_dom
  FROM (SELECT ts, domain, url, title, story_key(domain, title) AS key FROM headlines
        WHERE ts >= toDateTime('{start}', 'UTC') - INTERVAL {WINDOW_DAYS} DAY AND ts < toDateTime('{end}', 'UTC'))
) WHERE ts >= toDateTime('{start}', 'UTC')"""


def ingest_select(first):
    """SELECT ts, domain, url, title, copy for the rows of `incoming`, whose
    earliest timestamp is `first` ('YYYY-MM-DD HH:MM:SS'): the look-back is
    the batch itself plus the table's rows in the WINDOW_DAYS days before
    `first`. Ordered, so a retried batch is the same bytes and ClickHouse's
    deduplication window drops it."""
    lo = f"toDateTime('{first}', 'UTC') - INTERVAL {WINDOW_DAYS} DAY"
    hi = f"toDateTime('{first}', 'UTC')"
    return f"""
SELECT ts, domain, url, title, {FLAG} AS copy
FROM (
  SELECT ts, domain, url, title, greatest({LAG % 'key'}, seen_any) AS prev_any, greatest({LAG % 'key, domain'}, seen_dom) AS prev_dom
  FROM (
    SELECT i.ts AS ts, i.domain AS domain, i.url AS url, i.title AS title, i.key AS key, a.last AS seen_any, d.last AS seen_dom
    FROM (SELECT ts, domain, url, title, story_key(domain, title) AS key FROM incoming) AS i
    LEFT JOIN (SELECT key, max(ts) AS last FROM (SELECT story_key(domain, title) AS key, ts FROM headlines WHERE ts >= {lo} AND ts < {hi}) GROUP BY key) AS a ON a.key = i.key
    LEFT JOIN (SELECT key, domain, max(ts) AS last FROM (SELECT story_key(domain, title) AS key, domain, ts FROM headlines WHERE ts >= {lo} AND ts < {hi}) GROUP BY key, domain) AS d ON d.key = i.key AND d.domain = i.domain
  )
) ORDER BY ts, domain, url"""


# The settings the heavy statements run with on a 4 GB box.
HEAVY = {"max_threads": 2, "max_memory_usage": 2800000000, "max_bytes_before_external_sort": 600000000,
         "max_bytes_before_external_group_by": 600000000, "max_execution_time": 3600}


# ---------------------------------------------------------------- ClickHouse

CH_URL = os.environ.get("CH_URL", "http://127.0.0.1:8123")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASSWORD = os.environ.get("CH_PASSWORD", "")


def ch(query, settings=None):
    params = {"query": query}
    if settings:
        params.update({k: str(v) for k, v in settings.items()})
    req = urllib.request.Request(CH_URL + "/?" + urllib.parse.urlencode(params), data=b"", method="POST",
                                 headers={"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASSWORD})
    try:
        with urllib.request.urlopen(req, timeout=7200) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"clickhouse {e.code}: {e.read().decode()[:800]}") from None


def partitions():
    return [l for l in ch("SELECT DISTINCT partition FROM system.parts WHERE database = currentDatabase() AND table = 'headlines' AND active ORDER BY partition DESC FORMAT TSV").split()]


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# -------------------------------------------------------------------- labels

def labels():
    ch("CREATE TABLE IF NOT EXISTS labels_raw (domain String, tail String, n UInt64) ENGINE = MergeTree ORDER BY (domain, tail)")
    ch("TRUNCATE TABLE labels_raw")
    parts = partitions()
    for i, p in enumerate(parts):
        t = time.time()
        ch(f"""
INSERT INTO labels_raw SELECT domain, g[3] AS tail, count() AS n
FROM (SELECT domain, extractGroups(title, {SEP}) AS g FROM headlines
      WHERE toYYYYMM(ts) = {p} AND (position(title, ' | ') > 0 OR position(title, ' - ') > 0 OR match(title, ' (\\\\x{{2013}}|\\\\x{{2014}}) ')))
WHERE length(g) = 3 AND length(tail) BETWEEN 1 AND 160
GROUP BY domain, tail""", HEAVY)
        log(f"labels_raw {p} ({i + 1}/{len(parts)}) {time.time() - t:.0f}s")
    ch("DROP TABLE IF EXISTS labels_new")
    ch("CREATE TABLE labels_new AS labels")
    ch(f"INSERT INTO labels_new SELECT domain, tail, sum(n) FROM labels_raw GROUP BY domain, tail HAVING sum(n) >= {LABEL_MIN} SETTINGS optimize_aggregation_in_order = 1", HEAVY)
    ch("EXCHANGE TABLES labels AND labels_new")
    ch("DROP TABLE labels_new")
    ch("DROP TABLE labels_raw")
    log("labels: " + ch("SELECT count() AS pairs, uniqExact(domain) AS domains, sum(n) AS rows FROM labels FORMAT TSVWithNames").replace("\n", " "))


# ------------------------------------------------------------------ backfill

def free_gb():
    return shutil.disk_usage(DATA_DIR).free / 1e9


def flagged(p):
    return int(ch(f"SELECT countIf(copy > 0) FROM headlines WHERE toYYYYMM(ts) = {p}").strip())


def bounds(p):
    y, m = int(p[:4]), int(p[4:6])
    start = f"{y:04d}-{m:02d}-01 00:00:00"
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    return start, f"{y2:04d}-{m2:02d}-01 00:00:00"


def backfill():
    ch("ALTER TABLE headlines ADD COLUMN IF NOT EXISTS copy UInt8 DEFAULT 0")
    proj = ch("SELECT query FROM system.projections WHERE database = currentDatabase() AND table = 'headlines' AND name = 'by_domain'")
    if "copy" not in proj:
        log("redefining by_domain with the copy column; source-filtered searches are slow until each partition is rebuilt")
        ch("ALTER TABLE headlines DROP PROJECTION IF EXISTS by_domain")
        ch("ALTER TABLE headlines ADD PROJECTION by_domain (SELECT ts, domain, url, title, copy ORDER BY (domain, ts))")
    ch("CREATE TABLE IF NOT EXISTS headlines_new AS headlines")
    ch("ALTER TABLE headlines MODIFY SETTING old_parts_lifetime = 60")
    parts = partitions()
    started = time.time()
    done = 0
    try:
        for i, p in enumerate(parts):
            if flagged(p):
                log(f"{p} already flagged")
                continue
            while free_gb() < MIN_FREE_GB:
                log(f"{free_gb():.1f} GB free; waiting")
                time.sleep(30)
            t = time.time()
            ch(f"ALTER TABLE headlines_new DROP PARTITION {p}")
            start, end = bounds(p)
            ch(f"INSERT INTO headlines_new (ts, domain, url, title, copy) {backfill_select(start, end)}",
               {**HEAVY, "max_insert_threads": 1, "min_insert_block_size_rows": 2500000, "min_insert_block_size_bytes": 1500000000})
            n = ch(f"SELECT count() FROM headlines_new WHERE toYYYYMM(ts) = {p}").strip()
            m = ch(f"SELECT count() FROM headlines WHERE toYYYYMM(ts) = {p}").strip()
            if n != m:
                raise RuntimeError(f"{p}: {n} rows rebuilt, {m} in the table; stopping")
            ch(f"ALTER TABLE headlines REPLACE PARTITION {p} FROM headlines_new")
            ch(f"ALTER TABLE headlines_new DROP PARTITION {p}")
            done += 1
            log(f"{p} ({i + 1}/{len(parts)}) {n} rows, {time.time() - t:.0f}s, {free_gb():.1f} GB free")
    finally:
        ch("ALTER TABLE headlines RESET SETTING old_parts_lifetime")
    ch("DROP TABLE headlines_new")
    log(f"backfill done: {done} partitions in {(time.time() - started) / 60:.0f} min")


def status():
    print(ch("SELECT toYYYYMM(ts) AS partition, count() AS rows, countIf(copy = 0) AS stories, countIf(copy = 1) AS outlets, countIf(copy = 2) AS repeats FROM headlines GROUP BY partition ORDER BY partition FORMAT PrettyCompactMonoBlock"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "labels":
        labels()
    elif cmd == "backfill":
        backfill()
    elif cmd == "status":
        status()
    else:
        sys.exit(__doc__)
