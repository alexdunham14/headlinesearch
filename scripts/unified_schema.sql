-- Searching GDELT, the news collector and the blog collector together (built
-- 2026-09-15 as a proof of concept and put on the site the same day; root
-- repo: sessions/2026-09-15-unifying-the-sources.md).
-- Its own database, rebuilt by scripts/unify.py from `default.headlines`,
-- `collect.headlines` and `blogs.posts`, none of which it changes; DROP
-- DATABASE unified undoes it. Applied by scripts/unify.py on every run;
-- safe to re-run.
--
-- The Worker reads `default.headlines` for anything before START (unify.py,
-- 2026-09-01) and this table from START on, so every GDELT row from START is
-- here too, beside the feeds (from 2026-09-12) and the blogs (from
-- 2026-09-01). The dates are hard starts: a source contributes nothing before
-- its own.

CREATE DATABASE IF NOT EXISTS unified;

-- One row per article. src is gdelt, feeds or blogs; platform is substack or
-- medium for a blog and empty otherwise. title is the page title (GDELT),
-- the news sitemap's title or else the feed's (feeds), the archive API's
-- title or the URL slug (blogs: Medium's titles are slugs). key is
-- story_key(domain, title) (scripts/schema.sql), kept so the look-back of
-- the next day's build and the Worker's collapsing do not recompute it.
--
-- Two copy flags, each 0 = the first sighting of the story in seven days, 1
-- = its first sighting on this domain, 2 = a repeat on the same domain, as
-- in default.headlines:
--   copy       within the row's own source. For a GDELT row it is the flag
--              default.headlines has, carried over.
--   copy_news  within GDELT and the feeds together, for a search that
--              chooses both. 255 on a feed row whose URL GDELT also has
--              (within seven days either side), which such a search leaves
--              out. A blog row carries its `copy` here too: a blog post
--              never counts as a copy of a news story (Alex, 2026-09-15).
-- So every choice of sources counts exactly: gdelt alone, feeds alone or
-- blogs alone use `copy`; gdelt and feeds use `copy_news` (without the 255
-- rows); blogs beside either use their own `copy`, which is also their
-- `copy_news`.
--
-- Partitioned by day, unlike default.headlines, because a row that arrives
-- late (Medium's daily files come a day or two after the posts, GDELT's
-- files up to a day after their time) changes the flags of later rows of
-- the same story: unify.py rebuilds whole days through a shadow table and
-- REPLACE PARTITION, as copyflag.py backfill does. The text index, the
-- by_domain projection and the settings the Worker sends are the same as
-- default.headlines' (see there).
CREATE TABLE IF NOT EXISTS unified.headlines (
  ts        DateTime('UTC') CODEC(DoubleDelta, ZSTD(1)),
  src       LowCardinality(String),
  platform  LowCardinality(String),
  domain    String CODEC(ZSTD(3)),
  url       String CODEC(ZSTD(3)),
  title     String CODEC(LZ4),
  key       String CODEC(ZSTD(3)),
  copy      UInt8,
  copy_news UInt8,
  INDEX tx replaceAll(replaceAll(replaceAll(translateUTF8(replaceRegexpAll(normalizeUTF8NFD(lowerUTF8(toValidUTF8(title))), '\\p{Mn}', ''), 'łøđħŧı', 'lodhti'), 'ß', 'ss'), 'æ', 'ae'), 'œ', 'oe') TYPE text(tokenizer = 'splitByNonAlpha'),
  INDEX domain_bf domain TYPE bloom_filter(0.01) GRANULARITY 1,
  PROJECTION by_domain (SELECT ts, src, platform, domain, url, title, key, copy, copy_news ORDER BY (domain, ts))
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (ts, domain);

-- A row per (source, platform, site) with its article count and first and
-- last article, rebuilt after every run; the Worker's source typeahead, the
-- sources page and /api/stats read it beside default.sources. platform is
-- substack or medium for a blog, so a site can be listed as either.
CREATE TABLE IF NOT EXISTS unified.sources (
  src      LowCardinality(String),
  platform LowCardinality(String),
  domain   String,
  n        UInt64,
  first    DateTime('UTC'),
  last     DateTime('UTC')
)
ENGINE = MergeTree
ORDER BY (domain, src);

ALTER TABLE unified.sources ADD COLUMN IF NOT EXISTS platform LowCardinality(String) AFTER src;

-- Staging, truncated around use: every feed and blog row from its start
-- date, one per URL, filtered (stage); one day's rows with their keys
-- (day); the seven days before it (lookback).
CREATE TABLE IF NOT EXISTS unified.stage (
  ts       DateTime('UTC'),
  src      String,
  platform String,
  domain   String,
  url      String,
  title    String
)
ENGINE = Memory;

CREATE TABLE IF NOT EXISTS unified.day (
  ts       DateTime('UTC'),
  src      String,
  platform String,
  domain   String,
  url      String,
  title    String,
  key      String,
  gcopy    UInt8,
  in_gdelt UInt8
)
ENGINE = Memory;

CREATE TABLE IF NOT EXISTS unified.lookback (
  ts     DateTime('UTC'),
  src    String,
  domain String,
  key    String,
  excl   UInt8
)
ENGINE = Memory;
