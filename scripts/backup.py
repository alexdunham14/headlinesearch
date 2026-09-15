#!/usr/bin/env python3
"""Back up the collectors' data to R2: the tables nothing else can rebuild.

GDELT's headlines can be rebuilt from the extracts in R2 (server/rebuild.sql),
and `labels`, `sources` and the copy flag from them. The news collector's and
the blog collector's rows cannot: feeds and news sitemaps keep a day or two,
so what was not stored is gone. So once a day this writes a ClickHouse
BACKUP of every table in `collect` and `blogs` that holds data (not the
Memory staging tables) to R2:

  backups/daily/YYYY-MM-DD     every day; a lifecycle rule on the bucket
                               deletes these after 35 days
  backups/monthly/YYYY-MM      the first run of each month, kept

A BACKUP is ClickHouse's own format: the data parts as they are on disk
plus each table's definition, so a restore needs no schema and no parsing.
The server writes to R2 itself (the S3 backup engine); the keys go into the
statement, and ClickHouse writes them to its query log as [HIDDEN].

Restore with the admin client, on a fresh box after server/setup.sh (whose
schema files leave the tables there and empty) and with the collect and
blogs timers stopped:

  RESTORE TABLE collect.headlines, TABLE collect.fetches, TABLE collect.backfill,
          TABLE blogs.posts, TABLE blogs.sites, TABLE blogs.files, TABLE blogs.history,
          TABLE blogs.fetches
  FROM S3('https://ACCOUNT_ID.r2.cloudflarestorage.com/gdelt-gkg/backups/daily/2026-09-15',
          'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY')

An empty table is restored into as it is; a table with rows is refused
unless SETTINGS allow_non_empty_tables = true, which appends (so a second
restore doubles the rows). Tested 2026-09-15: collect.headlines and
blogs.posts restored into a scratch database with the live counts
(RESTORE TABLE collect.headlines AS scratch.headlines ... is how to check a
backup without touching the real tables).

  scripts/backup.py            what the timer runs (server/backup.timer)
  scripts/backup.py --name X   write backups/X instead (a test)

Environment: CH_URL, CH_USER, CH_PASSWORD (the ingest user), R2_ACCOUNT_ID,
R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET. Stdlib only.
"""

import argparse
import datetime as dt
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TABLES = ["collect.headlines", "collect.fetches", "collect.backfill",
          "blogs.posts", "blogs.sites", "blogs.files", "blogs.history", "blogs.fetches"]

CH_URL = os.environ.get("CH_URL", "http://127.0.0.1:8123")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASSWORD = os.environ.get("CH_PASSWORD", "")


def ch(query):
    req = urllib.request.Request(CH_URL + "/?" + urllib.parse.urlencode({"query": query}), data=b"", method="POST",
                                 headers={"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASSWORD})
    try:
        with urllib.request.urlopen(req, timeout=7200) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        # Never echo the statement: it carries the R2 keys.
        body = e.read().decode()[:800]
        for secret in (os.environ.get("R2_SECRET_ACCESS_KEY"), os.environ.get("R2_ACCESS_KEY_ID")):
            if secret:
                body = body.replace(secret, "[HIDDEN]")
        raise RuntimeError(f"clickhouse {e.code}: {body}") from None


def sql_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def backup(name):
    """BACKUP the tables to backups/<name>; returns (files, bytes)."""
    acct, key, secret = os.environ["R2_ACCOUNT_ID"], os.environ["R2_ACCESS_KEY_ID"], os.environ["R2_SECRET_ACCESS_KEY"]
    bucket = os.environ.get("R2_BUCKET", "gdelt-gkg")
    dest = f"https://{acct}.r2.cloudflarestorage.com/{bucket}/backups/{name}"
    tables = ", ".join(f"TABLE {t}" for t in TABLES)
    t = time.time()
    out = ch(f"BACKUP {tables} TO S3({sql_str(dest)}, {sql_str(key)}, {sql_str(secret)}) FORMAT TSV").split()
    backup_id, status = out[0], out[1]
    if status != "BACKUP_CREATED":
        raise RuntimeError(f"backups/{name}: {status}")
    row = ch(f"SELECT num_files, total_size FROM system.backups WHERE id = {sql_str(backup_id)} FORMAT TSV").split()
    files, size = (int(row[0]), int(row[1])) if len(row) == 2 else (0, 0)
    print(f"backups/{name}: {files} files, {size / 1e6:.1f} MB, {time.time() - t:.0f}s", flush=True)
    return files, size


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="write backups/NAME instead of the daily and monthly names")
    args = ap.parse_args()
    if args.name:
        backup(args.name)
        return 0
    today = dt.datetime.now(dt.timezone.utc).date()
    backup(f"daily/{today:%Y-%m-%d}")
    # The month's first successful run also leaves a copy that is kept. A
    # BACKUP refuses a destination that already holds one, which is the test.
    try:
        backup(f"monthly/{today:%Y-%m}")
    except RuntimeError as e:
        if "BACKUP_ALREADY_EXISTS" not in str(e) and "already exists" not in str(e):
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
