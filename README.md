# Headline Search

Every news headline GDELT has seen since October 2019, searchable by word or by
substring, with a date range and a source-domain filter. About half a billion
rows from the GDELT Global Knowledge Graph (GKG), which has carried page titles
since September 2019. For the person who wants to know when a phrase first
turned up in the news, or what a particular outlet headlined that week.

## Definition of done

- A page with one search box, a word/substring toggle, a date range, an
  optional domain, and a plain list of results: date, headline, domain, link.
  Identical headlines within a result page collapse into one line with a count.
  The URL carries the query so a search can be linked to.
- Search covers the whole corpus (English-language GKG files, 2019-10-01 to
  yesterday) and answers in a few seconds at worst. Word search matches whole
  words, case-insensitive; substring search matches any run of characters.
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
   time with a text index over lowercased titles that serves both word and
   substring queries. A read-only `search` user with a quota, reachable only
   from the Worker. `server/setup.sh` turns a fresh Debian box into this.
3. **The site** on Cloudflare Workers: `index.html`, `styles.css`, `app.js`
   as static assets, and `worker.js` for `/api/search` and `/api/count`, which
   validate the parameters, build a parameterised ClickHouse query, cache the
   answer at the edge (an hour for searches, a day for counts) and rate-limit
   by IP (30 a minute).

Search returns the newest 100 matches, paged with `page=`, up to 50 pages.
Count returns matches per month and is a full scan for anything but rare
terms: it runs with a 20 second budget and says so when it ran out. Both
modes lower-case the query.

Word mode requires every word to be present as a whole token (split on
anything that is not a letter or digit). Substring mode requires the exact
character sequence. A bloom filter per granule over lowercased trigrams
(`scripts/schema.sql` explains the sizing) lets ClickHouse skip the hour-long
granules that cannot contain a rare term; common terms are found in the first
few granules anyway because reads go newest-first and stop at the limit.

Sizes measured on 30 days (3.5M rows): 54 bytes a row on disk including the
index, so about 26 GB for the corpus. A full scan of the title column runs at
tens of millions of rows a second per core.

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
Pushes to main deploy via GitHub Actions using the `CLOUDFLARE_API_TOKEN` and
`CLOUDFLARE_ACCOUNT_ID` repository secrets. The Worker needs two secrets of its
own: `CH_URL` (the ClickHouse HTTP endpoint) and `CH_PASSWORD` (the `search`
user's password), set with `wrangler secret put`.

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
