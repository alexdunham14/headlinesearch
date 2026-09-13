-- The blog collector's own database (scripts/blogs.py): post titles from
-- the blogging platforms that publish their own lists. Separate from
-- `collect` (the news collector) and from `default` (the site's data):
-- nothing on the site reads these tables. The `collect` ClickHouse user
-- (server/users.xml) can reach `collect` and `blogs` and nothing else.
-- Applied by server/setup.sh with the admin client; safe to re-run.

CREATE DATABASE IF NOT EXISTS blogs;

-- One row per (platform, site, url, kind, title) seen: a post title as the
-- platform showed it, with the platform's time for the post (ts) and the
-- time we first saw it (seen). version is 1 for the first title seen for a
-- URL under that kind, 2 for the first change, and so on, exactly as in
-- collect.headlines. platform is substack, medium or wordpress; site is the
-- platform's own name for the publication (a Substack subdomain, a Medium
-- @user or publication slug, a WordPress.com site host); domain is the host
-- of the URL, which for a Substack on its own domain differs from the site.
-- kind says where the title came from: archive (Substack's public archive
-- API, the real title), slug (Medium's daily post sitemap, where the only
-- title is the URL slug: lower case, punctuation gone, so a degraded
-- title), api (WordPress.com's public API). source is the endpoint fetched.
-- The rest is what the platform said about the post at the time it was
-- first seen: subtitle, type (newsletter, podcast, ...), audience (everyone
-- or only_paid), lang (Substack's own field; for Medium a guess from the
-- title's function words, en or empty), author, wordcount, reactions,
-- comments, restacks (all Substack), post_id, tags. Ordered by site and URL
-- so the collector's lookup of what it already has is a key read.
CREATE TABLE IF NOT EXISTS blogs.posts (
  ts        DateTime('UTC') CODEC(DoubleDelta, ZSTD(1)),
  platform  LowCardinality(String),
  site      String CODEC(ZSTD(3)),
  domain    String CODEC(ZSTD(3)),
  url       String CODEC(ZSTD(3)),
  title     String CODEC(ZSTD(3)),
  seen      DateTime('UTC') CODEC(DoubleDelta, ZSTD(1)),
  version   UInt16,
  kind      LowCardinality(String),
  source    String CODEC(ZSTD(3)),
  subtitle  String CODEC(ZSTD(3)),
  type      LowCardinality(String),
  audience  LowCardinality(String),
  lang      LowCardinality(String),
  author    String CODEC(ZSTD(3)),
  wordcount UInt32,
  reactions UInt32,
  comments  UInt32,
  restacks  UInt32,
  post_id   String CODEC(ZSTD(3)),
  tags      Array(String) CODEC(ZSTD(3))
)
ENGINE = MergeTree
PARTITION BY (platform, toYear(ts))
ORDER BY (platform, site, url, kind, seen);

-- The staging table for one run: every post every fetch returned, from
-- which the rows not yet in posts are inserted with their version numbers.
-- Truncated around every run. staging_history is the same for the history
-- walkers, which run beside the hourly collector.
CREATE TABLE IF NOT EXISTS blogs.staging (
  ts        DateTime('UTC'),
  platform  String,
  site      String,
  domain    String,
  url       String,
  title     String,
  seen      DateTime('UTC'),
  kind      String,
  source    String,
  subtitle  String,
  type      String,
  audience  String,
  lang      String,
  author    String,
  wordcount UInt32,
  reactions UInt32,
  comments  UInt32,
  restacks  UInt32,
  post_id   String,
  tags      Array(String)
)
ENGINE = Memory;

CREATE TABLE IF NOT EXISTS blogs.staging_history AS blogs.staging ENGINE = Memory;

-- The sites a run is about to fetch, so their full rows can be read with a
-- join rather than a list of thousands in a query. Truncated around use.
CREATE TABLE IF NOT EXISTS blogs.queue (site String) ENGINE = Memory;

-- One row per publication the platforms list, latest state wins (seen).
-- For Substack: site is the subdomain, domain the custom domain if the
-- publication has one (then base_url is on that domain and the platform's
-- sitemap index no longer tracks it, so it is polled through its archive
-- API's ETag), lastmod the platform's last-post time from the index
-- or the newest post seen, listed where it came from (index, leaderboard,
-- or both), category the leaderboard categories, name, language,
-- subscribers and first_post from the leaderboard. etag is the ETag of
-- the publication's archive API answered with (custom-domain polling). status is the
-- last fetch's label (ok, 304, blocked, http NNN, error, robots, listed
-- for a row never fetched); fetched when. robots and robots_checked cache
-- the host's robots.txt answer for a day (RFC 9309 allows 24 hours).
CREATE TABLE IF NOT EXISTS blogs.sites (
  platform       LowCardinality(String),
  site           String,
  domain         String,
  base_url       String,
  lastmod        DateTime64(3, 'UTC'),
  seen           DateTime('UTC'),
  listed         LowCardinality(String),
  category       String,
  name           String,
  language       LowCardinality(String),
  subscribers    UInt32,
  first_post     Date,
  etag           String,
  status         LowCardinality(String),
  fetched        DateTime('UTC'),
  robots         LowCardinality(String),
  robots_checked DateTime('UTC')
)
ENGINE = ReplacingMergeTree(seen)
ORDER BY (platform, site);

-- One row per platform file processed (Medium's daily post sitemaps), so
-- that a changed file is re-read and an unchanged one is not, and so that
-- the history walk can be stopped and resumed. lastmod is the index's
-- lastmod for the file as a string, etag what the file answered with.
CREATE TABLE IF NOT EXISTS blogs.files (
  platform LowCardinality(String),
  file     String,
  lastmod  String,
  etag     String,
  seen     DateTime('UTC'),
  items    UInt32,
  status   LowCardinality(String)
)
ENGINE = ReplacingMergeTree(seen)
ORDER BY (platform, file);

-- The Substack history walk's bookmark per publication: the archive
-- offset reached, whether the walk hit the end, the oldest post date seen.
CREATE TABLE IF NOT EXISTS blogs.history (
  platform LowCardinality(String),
  site     String,
  offset   UInt32,
  done     UInt8,
  oldest   DateTime('UTC'),
  pages    UInt32,
  updated  DateTime('UTC')
)
ENGINE = ReplacingMergeTree(updated)
ORDER BY (platform, site);

-- One row per fetch per run: what happened. target is the site, file, tag
-- or index URL. label is ok, 304, blocked, robots, error, parse, empty,
-- html, or http NNN. etag and last_modified are what the next run sends
-- back as conditional headers for index and file fetches.
CREATE TABLE IF NOT EXISTS blogs.fetches (
  ts            DateTime('UTC'),
  platform      LowCardinality(String),
  target        String,
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
ORDER BY (platform, target, ts)
TTL ts + INTERVAL 90 DAY;
