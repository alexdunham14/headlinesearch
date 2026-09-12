# Headline Search

Live at https://newsheadlinesearch.com (headlinesearch.alexdunham14.workers.dev also serves it).

Every news headline GDELT has seen since October 2019, searchable by word or by
substring, with a date range and a source-domain filter. About 325 million
rows (September 2026, growing by four million a month) from the GDELT Global
Knowledge Graph (GKG), which has carried page titles since September 2019. For the person who wants to know when a phrase first
turned up in the news, or what a particular outlet headlined that week.

## Definition of done

- A page with one search box, a word/substring toggle, a newest/oldest-first
  toggle, a date range, a source box and a plain list of results: the
  headline as text (so it can be copied), the date, and the source as a
  link to the article, with the matched words marked. A page is a hundred
  distinct stories: copies of a headline (a story on many sites, or on one
  site's many local editions, with or without the site's own label after a
  pipe or a dash) collapse into one line with a count and "+3 more", which
  opens the other sites that carried it, each linked to its own copy. The
  source box suggests sites as you type (from a table of every site with
  its article count), a chosen site becomes a chip with an × to remove it,
  up to ten can be chosen and are searched together, and a source with an
  empty search box lists everything from that site; "Clear all filters"
  puts every filter back. A month in the count can be clicked to narrow
  the search. A sources page lists every site by first letter, with its
  article count and the first and last day it covers, and finds sites by
  part of a name. The page says what is loaded (first and last day, row
  count, number of sources). The URL carries the query so a search can be
  linked to.
- Search covers the whole corpus (English-language GKG files, 2019-10-01 to
  yesterday). Word search matches whole words, ignoring case and accents
  ("el nino" finds "El Niño" and "El Nino"; the corpus spells it both ways
  about equally), and answers
  in a second or two for any word or combination of words over the whole
  range, with or without a source, from an inverted index rather than a
  scan. Substring search matches any run of characters and is usually as
  quick when the pattern contains four or more letters or digits in a row;
  otherwise it scans and can hit the 20-second limit over the whole archive,
  in which case the page says so and offers whole-word search or the last
  twelve months instead. Counting matches by month runs in twelve-month
  windows and draws the chart as they arrive, about a second a window, and
  gives three counts a month: stories (a headline's first appearance
  anywhere in a week), outlets (its first appearance on each site) and
  articles (every page), so that a story copied to a hundred pages of one
  radio group is one story and one outlet, and a wire story on three
  hundred local sites is one story and three hundred outlets. The three
  mean the same whatever the source filter (on one site, stories are the
  headlines that site had first). The bars show one measure, chosen under
  the chart; the note and the table give all three.
- Compare mode (`/compare`): up to six terms on one chart, month by month.
  Each term is a whole-word search, with OR between alternatives (`congo OR
  drc`) and, optionally, sources (`gaza site:bbc.com`, `gaza
  site:bbc.com,nytimes.com`, or `site:bbc.com` alone for everything from
  it). The lines show stories, outlets or articles, as counts or as a
  share of all the headlines GDELT collected that month (for a term with
  sources, of those sources' headlines), over a date range; a hover gives
  the month's numbers; a table gives the same; the URL carries all of it;
  and the chart can be saved as an image with the URL on it. The same OR
  works in the search box, so a series can be clicked through to its
  headlines. The search page's "Compare to other terms" button carries its
  term, sources, measure and dates across.
- The database is fed by a scheduled ingest that reads GDELT's master file
  list, downloads new GKG files, keeps only date, source, URL and title,
  flags each row as a story, an outlet's copy or a repeat, and inserts
  them. It is idempotent and resumable: re-running never duplicates rows
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
   time, with a text index (ClickHouse's inverted index) over the lowercased
   titles, a bloom-filter skip index over the source domain, and a projection
   ordered by source (`scripts/schema.sql` explains them); beside it
   `sources`, a row per site with its article count and first and last
   headline, kept by a materialized view on every insert (86,737 sites on
   2026-09-12). A read-only `search` user with a quota, reachable only
   from the Worker. `server/setup.sh` turns a fresh Debian box into this.
3. **The site** on Cloudflare Workers: `index.html`, `styles.css`, `app.js`
   as static assets, `compare.html` and `compare.js` for compare mode,
   `sources.html` and `sources.js` for the sources page, and `worker.js`
   for `/api/search`, `/api/count`, `/api/totals`, `/api/sources` and
   `/api/stats`, which validate the parameters, build a parameterised
   ClickHouse query, cache the answer at the edge (an hour for searches and
   stats, a day for counts and source lists) and rate-limit by IP (120 a
   minute; a search with its chart is eight requests, and the page retries
   windows that were refused once the minute has turned). The pages are
   in the browser's own font with the least CSS that lays them out for a
   phone first.

Search returns a page of 100 distinct headlines, newest first or oldest
first (`sort=oldest`). The database returns rows, one per article URL, and
the Worker collapses identical titles: it reads a hundred rows and, when
fewer than half of them were distinct (a story copied to a hundred local
radio-station pages, say), up to a thousand, then keeps the first hundred
distinct titles with a count of copies and of sites for each. The collapse
is in the Worker rather than the query because `LIMIT 1 BY title` makes
ClickHouse read the title of every candidate row instead of the final
hundred, five to twenty times slower on a common word (measured
2026-09-11). The next page continues from where this one stopped, given as
`before=20260911183000&skip=7` (oldest-first: `after=`): the timestamp of
the last row used and how many rows at that timestamp were already shown,
since GDELT's timestamps are fifteen-minute batches shared by many rows.
Unlike a row offset, page fifty costs the same as page one, and rows
inserted between two page loads do not shift the pages. Either direction
reads from its end of the table and stops at the limit, so they cost the
same. Stats is the first and last timestamp, the row count and the number of
sources, which the page shows so nobody searches for 2020 while only 2025
is loaded. Sources (`/api/sources?q=bb`, `?letter=b`) is the typeahead and
the sources page: the sites whose name contains the text, those starting
with it first and then the biggest, twelve of them; or every site starting
with a letter in name order (7,200 for the biggest letter), both
milliseconds from the `sources` table.
Count returns matches per month for a date range, with a 20 second budget,
and says so when it ran out. The page asks for twelve-month windows, newest
first, and draws the chart as they arrive, about a second a window since
the text index (a common word over the whole archive took minutes before
it); each window is cached at the edge for a day. Both modes
lower-case the query.

Word mode requires every word to be present as a whole token (split on
anything that is not a letter or digit), compared after folding: lowercased,
accents stripped, ł ø đ ß æ œ and the like mapped to ASCII, as Postgres's
unaccent would. Substring mode requires the exact character sequence, case
aside. Both are answered by a text index: per data part, a dictionary of
every token in the folded titles and, for each token, a
compressed bitmap of the rows that contain it, the same structure as a
Postgres GIN index. A word search intersects the bitmaps of its words and
reads only the rows that survive, so "raleigh charter" (41 matches among 325
million rows, both words common on their own) costs its two posting lists
rather than a scan. Common terms are cheap for the other reason too: reads go
newest-first (or oldest-first) and stop at the limit. A substring search
adds, for each run of four or more letters or digits in the pattern, a lookup
of the dictionary for the tokens containing that run, and the bitmaps of
those tokens narrow the read before the exact pattern is checked.

OR between alternatives (`congo OR drc`, `el nino OR la nina`; upper case,
on its own) makes a word search match headlines with any of them. Single
words become one `hasAnyTokens` over the folded title; phrases become
`hasAllTokens` per alternative joined by OR, which the text index also
answers from its posting lists (measured 2026-09-12 over one year: "el nino
OR la nina" read 1,038 of 5,142 granules, 0.3 s; "congo OR drc" 0.8 s).
Substring mode takes OR literally. At most eight alternatives of eight
words.

**Compare mode** is the month chart for several terms at once, and asks the
database for nothing new: each series is the same `/api/count` in the same
twelve-month windows as the single chart, so the two share the edge cache,
and the denominator for a share is `/api/totals`, the three counts a month
with no term (about a second a window over the whole crawl, 0.04 s for one
source through the projection), cached a day like the counts. The page
fetches three windows at a time, newest first, and draws the lines as they
arrive. The share is of the same measure: stories against all stories that
month, an outlet's articles against that outlet's articles. Its caveat is
on the page: GDELT's set of sources is not constant (15 million rows in
2019, 53 million in 2020, 42 million in 2025), so a share across years is
against a changing base, and a term's share of one source's headlines is
the steadier comparison. The image is the chart's SVG drawn again on white
with the series, their totals and the page's URL, rasterised in the
browser.

Until 2026-09-11 the same job was done by two bloom-filter skip indexes (one
per granule of 8192 rows, about an hour of news, over the words and one over
the trigrams). Those can only say that an hour certainly lacks a word, and
can only intersect two words at the level of an hour, so a rare combination
of common words ("raleigh charter", "kenny felder") left most of the archive
to scan and timed out. The text index replaced them; the session doc of that
day in the root repo has the measurements.

**Stories, outlets, articles.** A headline reaches the archive many times
over: a wire story on three hundred local sites, a radio group's story on
each of its seven hundred station pages, a newspaper group's story under
each masthead. Every row carries a one-byte `copy` flag, set by the
ingest from the row's story key and the seven days before it: 0 for the
first sighting of the key anywhere (a story), 1 for the first sighting on
that domain of a key seen elsewhere before (an outlet's copy), 2 for a
repeat on the same domain. The key is the title with a known site label
cut off, where a label is what follows the last " | ", " - ", " – " or
" — " when that domain has used the same tail on twenty or more titles
("| Sunny 102.3 FM", "- Jamaica Observer", "| Opinion"); a headline's own
second half does not recur twenty times on one site, so it is left alone,
and `title` itself stays as GDELT recorded it. `story_key()` is a SQL
function in the database (scripts/schema.sql), so the ingest, the
one-off backfill and the Worker's searches agree; `scripts/copyflag.py`
explains the rule, builds the label table and rebuilt the archive on
2026-09-12 (over the whole archive 57% of rows are stories, 32% outlets'
copies and 11% same-site repeats: 184.7M, 103.4M and 37.3M of 325.4M; in
August 2026 iHeart's 218,853 rows were 7,972 stories).
The counts are then `countIf(copy = 0)`, `countIf(copy <= 1)` and
`count()`, all answered from the text index plus the flag column (a month
of "trump" in 0.14 s), and the same three with a source filter, so the
definitions on the page hold everywhere: on bbc.com "stories" are the
headlines bbc.com had before anyone else and "outlets" its own first
sightings of any headline (until 2026-09-12 a sourced count used the
latter for both and hid outlets). Two limits: outlets rewrite wire headlines, so
"stories" overcounts by the rewrites, a consistent overcount a trend can
live with; and the week is a choice, so a title that comes back after a
quiet week is a story again.

What no title index can do is prune on a source, so a source filter is
served by a projection instead: a second copy of the rows ordered by
(domain, ts), which ClickHouse picks whenever the query names a domain (or
several: `domain IN` over a bound array picks it too), so "hurricane" on
irishtimes.com reads that site's rows and nothing else. Its price is the
disk, about as much again as the table. A search with a source and no
term at all is the cheapest kind, the tail of that site's rows (measured
2026-09-12 as the search user: bbc.com newest-first 0.14 s, iheart.com
with its 15 million rows 0.9 s, three sites together 0.35 s, the three
biggest 1.6 s; a no-term count of three sites over the whole archive
0.33 s). A term over several sources costs a pass over each site's rows,
about two seconds a site over the whole archive ("hurricane" on three
sites 5.8 s), which is why the Worker takes at most ten. The Worker turns
projections on only for a query with a source: left to itself ClickHouse
also picked the projection for every whole-archive search, since in its lazy
skip-index mode the table looks like a full scan and the projection has
slightly fewer marks, and then it could not read newest-first and scanned
until the limit.

Still slow: a substring pattern with no run of four letters or digits, or
whose runs occur inside more than fifty distinct tokens while the pattern
itself is rare, scans until it finds a hundred and can hit the 20-second
limit over the whole archive; the page then offers the last twelve months,
or whole words.

Measured 2026-09-11 with the text index, after a merge of the table to one
part per month (84 parts), live through the Worker on the same box (2 vCPU,
4 GB): "raleigh charter" (41 matches, both words common) 0.6 s oldest-first
over the whole archive, 0.9 s with the server's caches dropped and 0.2 s
warm; a word with no matches 0.5 s cold; "cricket" newest-first 0.6 s cold;
"nenagh" 0.8 to 1.2 s; page 50 of "cricket" 1.3 to 1.8 s; a six-month count
window 0.2 to 0.6 s ("election" in 2024, 233k matches, 0.6 s); "hurricane"
on irishtimes.com 2.7 to 3.9 s, and any source filter about 2 s at least,
which is the projection's floor of one read per part plus folding that
site's titles; substring "nenagh" 1.0 s (1.8 cold), substring "raleigh
charter" 2.8 s (3.6 cold), "zelensk", "qatar" and "ovid" 0.2 to 0.4 s; "el
nino" and "el niño" the same hundred rows in 0.3 s. On disk the table is
27 GB, the projection 28 GB and the text index 7.6 GB, with 14 GB of the
80 GB free. The merge took an hour and the index rebuild twenty minutes,
with the site slow throughout; the per-partition merge needed the retention
of replaced parts shortened (`old_parts_lifetime`) and a pause under 6 GB
free, since replaced parts are only cleaned up every five minutes.

Measured 2026-09-11 before the text index, with the full corpus, 325M rows
and 27 GB, on the same box: a common word over the whole range 0.4 to 1.5 s from cold; a rare word
("nenagh", 1,883 matches) 6 s cold and 3 warm; a word with no matches
anywhere 12 to 15 s, or 4 when the token index happens to be in memory,
which on this box it rarely is; a six-month count window 2 to 9 s; page 50
of a common word 13 to 15 s. Every source-filtered search over the whole
archive timed out before the projection and takes 2 to 5 s with it, cold or
warm, most of that a floor of one granule per part (575 parts) rather than
the site's rows; a count with a source 0.2 s. The three skip indexes come
to 2.9 GB on disk, more than the box can keep in memory next to the data,
which is the cold-versus-warm gap. The projection took 17 minutes to build
and made the site slow for that long: the build saturated the disk.

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
  user; 8 GB is comfortable. Around 70 GB of disk: the table, its projection
  (as much again), the text index (about a gigabyte per year of rows) and
  room for merges.
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
`db.newsheadlinesearch.com` is a proxied record, which the deployed Worker
reaches all the same because a subrequest to a hostname in the Worker's
own zone goes straight to the origin; a `wrangler versions upload` preview
on workers.dev is not in that zone, goes through the proxy, which does not
serve port 8123, and gets "database error" for everything (2026-09-12).
So the API can only be tried in production, or locally against
`scripts/dev-db`.

The database runs on an AWS Lightsail instance (`headlinesearch-db`, 2 vCPU,
4 GB, 80 GB, Debian 12, us-east-1) in Alex's AWS account; `ssh headlinesearch-db`
reaches it from Alex's machine, and the root repo's session docs hold the
particulars. `CH_URL` points at it as `db.newsheadlinesearch.com` (see above).

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
