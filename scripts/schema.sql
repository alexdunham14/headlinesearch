-- One row per GKG record that carried a page title. Ordered by time so a
-- "newest first" query reads in key order and stops after LIMIT rows. Within
-- one 15-minute bucket rows sort by domain so URLs compress well.
--
-- title is LZ4 rather than ZSTD because scanning titles is the whole
-- workload and LZ4 decompresses about twice as fast. url is never scanned.
--
-- tx is a text index, ClickHouse's inverted index (26.8): per part, a
-- front-coded dictionary of the tokens of the folded titles (split on
-- non-alphanumeric ASCII, the same tokenizer as hasToken) and a roaring-bitmap
-- posting list per token. Folded: lowercased, then accents stripped (NFD and
-- the combining marks removed), then the letters that do not decompose mapped
-- by hand (ł ø đ ħ ŧ ı, ß æ œ), so "nino" and "niño" are one token; the
-- corpus spells El Niño both ways about equally. toValidUTF8 comes first
-- because normalizeUTF8NFD throws on invalid UTF-8. The Worker folds the
-- typed words the same way (fold() in worker.js and app.js) and queries
-- hasAllTokens(<the same expression>, [...]), which is answered
-- from the posting lists without reading the title column ("direct read",
-- query_plan_direct_read_from_text_index, on by default), so a word or a
-- combination of words with few matches costs the posting lists, not a scan.
-- It replaced two bloom-filter skip indexes on 2026-09-11 (tokenbf_v1 over
-- the words, ngrambf_v1 over the trigrams: per-granule filters, which could
-- only intersect two words at the level of an hour of news; "raleigh charter",
-- 41 matches from two common words, left 137M rows to scan and timed out).
-- Substring searches use it too: a LIKE whose pattern has a run of four or
-- more letters or digits is answered by scanning the dictionary for tokens
-- containing the run (use_text_index_like_evaluation_by_dictionary_scan).
-- About a gigabyte per year of rows; inserts write it for their own part and
-- merges rebuild it for the merged part. Not `hasTokenCaseInsensitive`, which
-- is never index-aware: lowercase both sides instead, as here.
--
-- domain_bf lets a source filter skip the granules (hours) in which that site
-- published nothing, which for all but the biggest sites is most of them.
--
-- by_domain is a projection: a second copy of the rows inside each part,
-- ordered by (domain, ts). ClickHouse reads it instead of the table whenever
-- the query names a domain, so a source filter reads that site's rows and
-- nothing else. The bloom filters cannot do that: a big site is in nearly
-- every granule, and on 2026-09-11 every source-filtered search over the
-- whole archive timed out. It costs as much disk as the table (about 27 GB
-- at 325M rows) and every insert and merge writes the rows twice. One trap:
-- ClickHouse also picks it for a search with no domain at all, since in its
-- lazy skip-index mode the table looks like a full scan and the projection
-- has slightly fewer marks; it then cannot read in time order and scans
-- until the limit. The Worker sends optimize_use_projections=0 unless the
-- query names a domain. Do the same in any query run by hand.
--
-- non_replicated_deduplication_window: an INSERT whose block is identical to
-- one of the last 1000 inserted blocks is silently dropped, a second guard
-- against duplicates after the files table.

CREATE TABLE IF NOT EXISTS headlines (
  ts     DateTime('UTC') CODEC(DoubleDelta, ZSTD(1)),
  domain String CODEC(ZSTD(3)),
  url    String CODEC(ZSTD(3)),
  title  String CODEC(LZ4),
  INDEX tx replaceAll(replaceAll(replaceAll(translateUTF8(replaceRegexpAll(normalizeUTF8NFD(lowerUTF8(toValidUTF8(title))), '\\p{Mn}', ''), 'łøđħŧı', 'lodhti'), 'ß', 'ss'), 'æ', 'ae'), 'œ', 'oe') TYPE text(tokenizer = 'splitByNonAlpha'),
  INDEX domain_bf domain TYPE bloom_filter(0.01) GRANULARITY 1,
  PROJECTION by_domain (SELECT ts, domain, url, title ORDER BY (domain, ts))
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
