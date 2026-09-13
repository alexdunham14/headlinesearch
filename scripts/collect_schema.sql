-- The collector's own database (scripts/collect.py). Separate from `default`,
-- which the site reads: nothing on the site reads these tables, and the
-- `collect` user (server/users.xml) can reach nothing else. Applied by
-- server/setup.sh with the admin client; safe to re-run.

CREATE DATABASE IF NOT EXISTS collect;

-- One row per (domain, url, kind, title) the collector has seen: a headline
-- as a feed or a sitemap showed it, with the time the feed gave (ts) and the
-- time we first saw it (seen). version is 1 for the first title seen for a
-- URL under that kind, 2 for the first change, and so on; the rows with
-- version > 1 are headline edits, timed by seen. kind is 'feed' (RSS or
-- Atom) or 'sitemap' (a Google News sitemap, whose news:title is nearer the
-- page's <title>, which is what GDELT stored). The same story usually has a
-- row of each kind, often with different wording. Ordered by (domain, url)
-- so the collector's lookup of what it already has is a key read.
CREATE TABLE IF NOT EXISTS collect.headlines (
  ts      DateTime('UTC') CODEC(DoubleDelta, ZSTD(1)),
  domain  LowCardinality(String),
  url     String CODEC(ZSTD(3)),
  title   String CODEC(ZSTD(3)),
  seen    DateTime('UTC') CODEC(DoubleDelta, ZSTD(1)),
  version UInt16,
  feed    LowCardinality(String),
  kind    LowCardinality(String),
  section LowCardinality(String),
  guid    String CODEC(ZSTD(3))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(seen)
ORDER BY (domain, url, kind, seen);

-- One row per feed per run: what happened. label is ok, 304, blocked (a
-- challenge page, a 401 or a 403), html (a page where a feed should be),
-- robots (disallowed, or robots.txt unreachable), error (network), parse,
-- no-titles (a sitemap without news:title), empty, or http NNN. etag and
-- last_modified are what the next run sends back as conditional headers.
CREATE TABLE IF NOT EXISTS collect.fetches (
  ts            DateTime('UTC'),
  feed          LowCardinality(String),
  domain        LowCardinality(String),
  kind          LowCardinality(String),
  label         LowCardinality(String),
  http          UInt16,
  bytes         UInt32,
  items         UInt32,
  new           UInt32,
  changed       UInt32,
  ms            UInt32,
  etag          String,
  last_modified String,
  error         String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (feed, ts)
TTL ts + INTERVAL 180 DAY;

-- The staging table for one run: every item every feed returned, from which
-- the rows not yet in headlines are inserted with their version numbers.
-- Truncated around every run.
CREATE TABLE IF NOT EXISTS collect.staging (
  ts      DateTime('UTC'),
  domain  String,
  url     String,
  title   String,
  seen    DateTime('UTC'),
  feed    String,
  kind    String,
  section String,
  guid    String
)
ENGINE = Memory;
