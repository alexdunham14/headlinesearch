# Headline Search

Live at https://newsheadlinesearch.com (headlinesearch.alexdunham14.workers.dev also serves it).

Every news headline GDELT has seen since October 2019, searchable by word or by
substring, with a date range and a source-domain filter. About half a billion
rows from the GDELT Global Knowledge Graph (GKG), which has carried page titles
since September 2019. For the person who wants to know when a phrase first
turned up in the news, or what a particular outlet headlined that week.

## Definition of done

- A page with one search box, a word/substring toggle, a newest/oldest-first
  toggle, a date range, an optional domain, and a plain list of results: date,
  headline, domain, link, with the matched words marked. Identical headlines
  within a result page collapse into one line with a count. A source or a
  month in the count can be clicked to narrow the search. The page says what
  is loaded (first and last day, row count). The URL carries the query so a
  search can be linked to.
- Search covers the whole corpus (English-language GKG files, 2019-10-01 to
  yesterday). Word search matches whole words, case-insensitive, and answers
  within its 20-second limit for any word over the whole range, with or
  without a source: about a second for a common word, a few seconds for a
  rare one, up to about fifteen when the server's caches are cold. Substring
  search matches any run of characters and is as quick unless the run is
  made of common trigrams, in which case the page says so and offers
  whole-word search or the last twelve months instead. Counting matches by
  month runs in six-month windows and draws the chart as they arrive:
  seconds for a rare word, a few minutes for a common one.
- The database is fed by a scheduled ingest that reads GDELT's master file
  list, downloads new GKG files, keeps only date, source, URL and title, and
  inserts them. It is idempotent and resumable: re-running never duplicates rows
  and never re-downloads a file it has already processed.
- The extracted rows are also written to R2 as gzipped TSV, one object per GKG
  file. That is the archive. The database can be rebuilt from it with one SQL
  statement, and it is the fallback if GDELT goes away.
- Users cannot run arbitrary SQL. The Worker builds every query from a fixed
  template with bound parameters, the database user is read-only with a query
  timeout and a row cap, and each visitor is rate limited.

Out of scope: the translated (non-English) GKG files, GDELT's themes,
locations, tone and other columns, article bodies, ranking by relevance.
If GDELT stops publishing, the site keeps serving what it has and this
project is finished.

## How it works

Three pieces, in three places.

1. **`scripts/ingest.py`** (stdlib Python) runs on the database server from a
   systemd timer. It fetches `masterfilelist.txt`, works out which GKG files
   the database has not seen, and for each one: downloads the zip, checks the
   MD5 from the master list, pulls the four fields out of the 27-column TSV,
   writes the rows to R2 (`headlines/YYYY/MM/DD/<ts>.tsv.gz`) and inserts them
   into ClickHouse. The `files` table records what has been done.
   `--from-dir` reads zips already on disk instead of downloading.
2. **ClickHouse** on a small VPS (`server/`). One table, `headlines`, ordered by
   time with three bloom-filter skip indexes: whole words and trigrams of the
   lowercased titles, and the source domain (`scripts/schema.sql` explains
   them). A read-only `search` user with a quota, reachable only
   from the Worker. `server/setup.sh` turns a fresh Debian box into this.
3. **The site** on Cloudflare Workers: `index.html`, `styles.css`, `app.js`
   as static assets, and `worker.js` for `/api/search`, `/api/count` and
   `/api/stats`, which validate the parameters, build a parameterised
   ClickHouse query, cache the answer at the edge (an hour for searches and
   stats, a day for counts) and rate-limit by IP (30 a minute).

Search returns 100 matches, newest first or oldest first (`sort=oldest`),
paged with `page=`, up to 50 pages. Either direction reads from its end of
the table and stops at the limit, so they cost the same. Stats is the first
and last timestamp and the row count, which the page shows so nobody
searches for 2020 while only 2025 is loaded.
Count returns matches per month for a date range, with a 20 second budget,
and says so when it ran out. The page asks for six-month windows, newest
first, and draws the chart as they arrive, so a common word over the whole
archive takes a few minutes and a rare word seconds; each window is cached
at the edge for a day. Both modes lower-case the query.

Word mode requires every word to be present as a whole token (split on
anything that is not a letter or digit). Substring mode requires the exact
character sequence. Per granule (8192 rows, about an hour of news) there is a
bloom filter over the lowercased titles' whole words, one over their trigrams,
and one over the source domains, so ClickHouse skips every hour that cannot
contain the word, the trigrams or the site. Common terms are found in the
first few granules anyway because reads go newest-first and stop at the
limit. What the filters cannot do is prune on a combination, so a source
filter is served by a projection instead: a second copy of the rows ordered
by (domain, ts), which ClickHouse picks whenever the query names a domain,
so "hurricane" on irishtimes.com reads that site's rows and nothing else.
Its price is the disk, about as much again as the table.

Two shapes are still slow. A substring made of common trigrams ("nenagh" as
a substring; as a word it is instant) scans until it finds a hundred and can
hit the 20-second limit over the whole archive; the page then offers the
last twelve months, or whole words. And a word with fewer than a hundred
matches in the whole archive has nothing to stop the read early, so every
granule's token filter is decompressed: 1.8 GB, about four seconds when it
is in memory and fifteen when it is not. Word mode tells ClickHouse to
ignore the trigram filter, which the LIKE would otherwise drag in for
another gigabyte and no extra pruning.

Measured 2026-09-11 with the full corpus, 325M rows and 27 GB, on the same
box: a common word over the whole range 0.4 to 1.5 s from cold; a rare word
("nenagh", 1,883 matches) 6 s cold and 3 warm; a word with no matches
anywhere 14.5 s cold, 4 warm; a six-month count window 2 to 9 s; every
source-filtered search over the whole archive timed out before the
projection and takes 0.2 to 0.4 s with it, from cold. The three skip
indexes come to 2.9 GB on disk, more than the box can keep in memory next
to the data, which is the cold-versus-warm gap.

Measured 2026-09-10 on the Lightsail box (2 vCPU, 4 GB) with 63M rows loaded
and the backfill inserting: a full scan of the titles ran at 4.5M rows a
second, so a full-corpus scan is about 100 seconds, which is why nothing on
the page depends on one. The token index cut a rare word ("nenagh", 88
matches) from a full scan to 1.3% of granules. Earlier, on 30 days: 54 bytes
a row on disk including the trigram index, so about 26 GB of data for the
corpus; the token index adds about 8 bytes a row.

## Running it locally

```
scripts/dev-db &                  # ClickHouse in ~/.local/share/headlinesearch (downloads the binary once)
. ~/.local/share/headlinesearch/env
scripts/ingest.py --no-r2 --max-files 50          # or --from-dir DIR for zips already on disk
printf 'CH_URL=http://127.0.0.1:8123\nCH_PASSWORD=%s\n' "$CH_SEARCH_PASSWORD" > .dev.vars
wrangler dev --persist-to ~/.local/share/headlinesearch/wrangler-state
```

`--persist-to` keeps wrangler's state out of the repo; with the assets
directory being the repo root, wrangler would otherwise reload on its own
writes. `scripts/tunnel` points the deployed Worker at the local database
through a Cloudflare quick tunnel, for demos before there is a server.

## Costs

- VPS: whatever runs ClickHouse comfortably. 4 GB of RAM works for a single
  user; 8 GB is comfortable. Around 40 GB of disk for the database and its
  merges.
- R2: about 24 GB of extracts, under $1 a month. No egress charges, so the
  server can be rebuilt from R2 for free.
- Cloudflare Workers: free tier.

## Hosting

Cloudflare Workers static assets plus a fetch handler (`wrangler.jsonc`).
Deploy by hand with `wrangler deploy` from a checkout. The GitHub Actions deploy was removed on 2026-09-06 because the `CLOUDFLARE_API_TOKEN` secret is not set and every push failed; put it back (cloudflare/wrangler-action with the token and `CLOUDFLARE_ACCOUNT_ID`) once the token exists.
The Worker needs two secrets of its
own: `CH_URL` (the ClickHouse HTTP endpoint) and `CH_PASSWORD` (the `search`
user's password), set with `wrangler secret put`. `CH_URL` must use a
hostname, not a bare IP address: a Worker's `fetch()` to an IP literal is
refused at Cloudflare's edge (error 1003) and never reaches the server. Any
port works.

The database runs on an AWS Lightsail instance (`headlinesearch-db`, 2 vCPU,
4 GB, 80 GB, Debian 12, us-east-1) in Alex's AWS account; `ssh headlinesearch-db`
reaches it from Alex's machine, and the root repo's session docs hold the
particulars. `CH_URL` points at it by hostname (see above). Until
`db.newsheadlinesearch.com` exists, that hostname is a wildcard-DNS name for
the box's static IP.

## Setting up the server

On a fresh Debian or Ubuntu box, as root:

```
git clone https://github.com/alexdunham14/headlinesearch /opt/headlinesearch
cd /opt/headlinesearch
CH_SEARCH_PASSWORD=... CH_INGEST_PASSWORD=... R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... ./server/setup.sh
```

That installs ClickHouse, the config in `server/`, a firewall that admits
port 8123 from Cloudflare's published IP ranges only, and a systemd timer
that runs the ingest every six hours. To start from the R2 archive rather
than re-downloading GDELT, run `server/rebuild.sql` (with the account id and
keys filled in) through `clickhouse-client` first. Traffic between Cloudflare
and the server is plain HTTP with a password; once a domain is on Cloudflare,
a Tunnel can replace the open port.
