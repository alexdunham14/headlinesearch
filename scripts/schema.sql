-- One row per GKG record that carried a page title. Ordered by time so a
-- "newest first" query reads in key order and stops after LIMIT rows. Within
-- one 15-minute bucket rows sort by domain so URLs compress well.
--
-- title is LZ4 rather than ZSTD because scanning titles is the whole
-- workload and LZ4 decompresses about twice as fast. url is never scanned.
--
-- title_ngram is a bloom filter per granule (8192 rows, about an hour of
-- news) over the lowercased trigrams of the titles in it: 32 KB, two hashes,
-- about 13k distinct trigrams per granule, so under 1% false positives per
-- trigram. Queries against lowerUTF8(title) with LIKE or hasToken skip every
-- granule whose filter lacks one of the query's trigrams. That prunes rare
-- terms hard and common terms not at all, which is fine: common terms hit
-- LIMIT after a few granules when read newest-first.
--
-- title_tokens is the same idea over whole words (tokens between non-alphanumeric
-- ASCII characters), 64 KB and three hashes per granule against about 25k
-- distinct tokens, so well under 1% false positives. It exists because the
-- trigram filter cannot prune a rare word made of ordinary trigrams: on
-- 2026-09-10 "nenagh" (88 matches) scanned all 63M rows loaded, 13 seconds,
-- which at the full corpus is a hundred. hasToken(lowerUTF8(title), word)
-- skips every granule without the word.
--
-- domain_bf lets a source filter skip the granules (hours) in which that site
-- published nothing, which for all but the biggest sites is most of them.
--
-- non_replicated_deduplication_window: an INSERT whose block is identical to
-- one of the last 1000 inserted blocks is silently dropped, a second guard
-- against duplicates after the files table.

CREATE TABLE IF NOT EXISTS headlines (
  ts     DateTime('UTC') CODEC(DoubleDelta, ZSTD(1)),
  domain String CODEC(ZSTD(3)),
  url    String CODEC(ZSTD(3)),
  title  String CODEC(LZ4),
  INDEX title_ngram lowerUTF8(title) TYPE ngrambf_v1(3, 32768, 2, 0) GRANULARITY 1,
  INDEX title_tokens lowerUTF8(title) TYPE tokenbf_v1(65536, 3, 0) GRANULARITY 1,
  INDEX domain_bf domain TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (ts, domain)
SETTINGS non_replicated_deduplication_window = 1000;

-- One row per GKG file the ingest has dealt with. status is 'ok' (rows
-- inserted, possibly zero) or 'missing' (GDELT returns 404 for a file its
-- master list names, not retried). Other failures are not recorded and are
-- retried on the next run.
CREATE TABLE IF NOT EXISTS files (
  ts     DateTime('UTC'),
  status LowCardinality(String),
  rows   UInt32,
  size   UInt32,
  done   DateTime('UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree(done)
ORDER BY ts;
