#!/usr/bin/env python3
"""Feed GDELT GKG page titles into ClickHouse and R2.

For every GKG file the database has not seen (per the `files` table):
download it (or read it from --from-dir), check its MD5 against the master
list, keep date / source domain / URL / page title from each row, write the
rows to R2 as headlines/YYYY/MM/DD/<ts>.tsv.gz, insert them into ClickHouse,
and record the file. Newest files first, so an interrupted backfill leaves
the most useful part of the corpus in place.

Idempotent: a file recorded in `files` is skipped; an insert whose block is
identical to a recent one is dropped by ClickHouse anyway. Resumable: stop it
whenever, run it again.

Environment: CH_URL (default http://127.0.0.1:8123), CH_USER, CH_PASSWORD,
R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET (default
gdelt-gkg). Stdlib only.
"""

import argparse
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import hmac
import html
import io
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

MASTER_URL = "https://data.gdeltproject.org/gdeltv2/masterfilelist.txt"
FIRST_TS = "20191001000000"  # PAGE_TITLE appears in GKG from late September 2019
GKG_RE = re.compile(r"/(\d{14})\.gkg\.csv\.zip$")
TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
WS_RE = re.compile(r"\s+")

CH_URL = os.environ.get("CH_URL", "http://127.0.0.1:8123")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASSWORD = os.environ.get("CH_PASSWORD", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "gdelt-gkg")


# ---------------------------------------------------------------- ClickHouse

def ch(query, data=None, settings=None):
    """Run one query over ClickHouse's HTTP interface. Returns the body as text."""
    params = {"query": query}
    if settings:
        params.update(settings)
    req = urllib.request.Request(
        CH_URL + "/?" + urllib.parse.urlencode(params),
        data=data,
        method="POST",
        headers={"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASSWORD},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"clickhouse {e.code}: {e.read().decode()[:500]}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)


def done_files():
    out = {}
    for line in ch("SELECT ts, status FROM files FINAL FORMAT TSV").splitlines():
        ts, status = line.split("\t")
        out[ts.replace("-", "").replace(":", "").replace(" ", "")] = status
    return out


# ------------------------------------------------------------------------ R2

class R2:
    """Just enough S3 SigV4 to PUT and HEAD objects in one bucket."""

    def __init__(self):
        acct = os.environ["R2_ACCOUNT_ID"]
        self.key = os.environ["R2_ACCESS_KEY_ID"]
        self.secret = os.environ["R2_SECRET_ACCESS_KEY"]
        self.host = f"{acct}.r2.cloudflarestorage.com"
        self.bucket = R2_BUCKET

    def _request(self, method, key, payload=b"", content_type=None):
        now = dt.datetime.now(dt.timezone.utc)
        amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        path = urllib.parse.quote(f"/{self.bucket}/{key}")
        payload_hash = hashlib.sha256(payload).hexdigest()
        headers = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz}
        if content_type:
            headers["content-type"] = content_type
        signed = ";".join(sorted(headers))
        canonical = "\n".join([method, path, "", *(f"{k}:{headers[k]}" for k in sorted(headers)), "", signed, payload_hash])
        scope = f"{day}/auto/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        k = ("AWS4" + self.secret).encode()
        for part in (day, "auto", "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={self.key}/{scope}, SignedHeaders={signed}, Signature={sig}"
        del headers["host"]
        return urllib.request.Request(f"https://{self.host}{path}", data=payload if method == "PUT" else None, headers=headers, method=method)

    def put(self, key, payload, content_type="application/gzip"):
        for attempt in range(5):
            try:
                with urllib.request.urlopen(self._request("PUT", key, payload, content_type), timeout=120):
                    return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)


# ------------------------------------------------------------------ parsing

def master_list():
    """{ts: (size, md5, url)} for every English GKG file since FIRST_TS."""
    with urllib.request.urlopen(MASTER_URL, timeout=300) as r:
        text = r.read().decode("utf-8", "replace")
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        m = GKG_RE.search(parts[2])
        if m and m.group(1) >= FIRST_TS:
            out[m.group(1)] = (int(parts[0]), parts[1], parts[2])
    return out


def tsv_escape(s):
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "")


def extract(zip_bytes):
    """GKG zip -> TSV text (ts, domain, url, title), one line per titled row."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if len(names) != 1:
            raise ValueError(f"zip has {len(names)} members")
        text = zf.read(names[0]).decode("utf-8", "replace")
    lines = []
    for line in text.split("\n"):
        f = line.split("\t")
        if len(f) < 27:
            continue
        m = TITLE_RE.search(f[26])
        if not m:
            continue
        title = WS_RE.sub(" ", html.unescape(m.group(1))).strip()
        d, domain, url = f[1].strip(), f[3].strip(), f[4].strip()
        if not (title and domain and url and len(d) == 14 and d.isdigit()):
            continue
        ts = f"{d[0:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:{d[10:12]}:{d[12:14]}"
        lines.append(f"{ts}\t{tsv_escape(domain)}\t{tsv_escape(url)}\t{tsv_escape(title)}\n")
    return "".join(lines)


def download(url):
    """Bytes of the zip, or None on a 404."""
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 3:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(3 * 2 ** attempt)


# -------------------------------------------------------------------- worker

def process(ts, meta, from_dir, use_r2):
    """Download, verify, extract, archive. Runs in a worker process.
    Returns (ts, status, tsv, size); the parent does the ClickHouse insert."""
    size, md5, url = meta
    if from_dir:
        path = os.path.join(from_dir, f"{ts}.gkg.csv.zip")
        with open(path, "rb") as fh:
            data = fh.read()
    else:
        data = download(url)
        if data is None:
            return ts, "missing", "", 0
    if md5 and hashlib.md5(data).hexdigest() != md5:
        raise ValueError("md5 mismatch")
    tsv = extract(data)
    if tsv and use_r2:
        gz = gzip.compress(tsv.encode(), compresslevel=6, mtime=0)
        R2().put(f"headlines/{ts[0:4]}/{ts[4:6]}/{ts[6:8]}/{ts}.tsv.gz", gz)
    return ts, "ok", tsv, len(data)


BATCH_FILES = 24  # one INSERT per six hours of files, so parts stay few and large


class Batch:
    """Insert files in fixed groups of BATCH_FILES consecutive queue entries,
    then record them. Groups are the same on every run, so if an insert fails
    half way (one part per month written, the next not) the retried block is
    byte-identical and ClickHouse's deduplication window drops the repeat."""

    def __init__(self, order):
        self.order = order  # file timestamps in queue order
        self.results = {}
        self.next = 0  # index into order of the first file not yet flushed
        self.errors = 0

    def add(self, ts, status, tsv, size):
        self.results[ts] = (ts, status, tsv, size)
        while self.next < len(self.order):
            group = self.order[self.next:self.next + BATCH_FILES]
            if not all(t in self.results for t in group):
                return
            self.flush(group)

    def finish(self):
        """Flush whatever is complete; skip over files that failed."""
        while self.next < len(self.order):
            group = [t for t in self.order[self.next:self.next + BATCH_FILES] if t in self.results]
            if group:
                self.flush(group)
            else:
                self.next += BATCH_FILES

    def flush(self, group):
        self.next += BATCH_FILES
        items = [self.results.pop(t) for t in group]
        tsv = "".join(item[2] for item in items)
        try:
            self._insert(items, tsv)
        except Exception as e:
            self.errors += len(items)
            print(f"batch at {group[0]} ERROR {type(e).__name__}: {str(e)[:300]}", file=sys.stderr, flush=True)

    def _insert(self, items, tsv):
        if tsv:
            ch("INSERT INTO headlines (ts, domain, url, title) FORMAT TSV", tsv.encode(), {"async_insert": 0})
        lines = []
        for ts, status, tsv, size in items:
            t = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
            lines.append(f"{t}\t{status}\t{tsv.count(chr(10))}\t{size}\n")
        ch("INSERT INTO files (ts, status, rows, size) FORMAT TSV", "".join(lines).encode(), {"async_insert": 0})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-dir", help="read <ts>.gkg.csv.zip files from this directory instead of downloading")
    ap.add_argument("--max-files", type=int, default=0, help="stop after this many files (0 = all)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--oldest-first", action="store_true")
    ap.add_argument("--no-r2", action="store_true", help="do not write extracts to R2")
    ap.add_argument("--retry-missing", action="store_true", help="try files previously recorded as missing")
    ap.add_argument("--since", default=FIRST_TS, help="ignore files before this YYYYMMDDHHMMSS")
    ap.add_argument("--until", default="99999999999999", help="ignore files after this YYYYMMDDHHMMSS")
    args = ap.parse_args()
    use_r2 = not args.no_r2
    if use_r2:
        R2()  # fail early if the environment is incomplete

    with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as fh:
        for statement in fh.read().split(";"):
            statement = "\n".join(l for l in statement.splitlines() if not l.lstrip().startswith("--"))
            if statement.strip():
                ch(statement)

    if args.from_dir:
        available = {m.group(1) for n in os.listdir(args.from_dir) for m in [re.match(r"(\d{14})\.gkg\.csv\.zip$", n)] if m}
        try:
            master = master_list()
        except Exception as e:
            print(f"master list unavailable ({e}); skipping MD5 checks", file=sys.stderr)
            master = {}
        todo = {ts: master.get(ts, (0, "", "")) for ts in available}
    else:
        todo = master_list()

    done = done_files()
    skip = {ts for ts, status in done.items() if status == "ok" or not args.retry_missing}
    queue = sorted((ts for ts in todo if ts not in skip and args.since <= ts <= args.until), reverse=not args.oldest_first)
    if args.max_files:
        queue = queue[: args.max_files]
    print(f"{len(todo)} files known, {len(done)} done, {len(queue)} to do", flush=True)
    if not queue:
        return 0

    counts = {"ok": 0, "missing": 0, "error": 0}
    total_rows = 0
    started = time.time()
    batch = Batch(queue)
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        # Submit in bounded chunks so a long backfill does not pin every result in memory.
        for start in range(0, len(queue), 240):
            chunk = queue[start:start + 240]
            futures = {pool.submit(process, ts, todo[ts], args.from_dir, use_r2): ts for ts in chunk}
            for fut in concurrent.futures.as_completed(futures):
                ts = futures[fut]
                try:
                    ts, status, tsv, size = fut.result()
                except Exception as e:
                    counts["error"] += 1
                    print(f"{ts} ERROR {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                    continue
                batch.add(ts, status, tsv, size)
                counts[status] += 1
                total_rows += tsv.count("\n")
            n = min(start + 240, len(queue))
            rate = n / (time.time() - started)
            print(f"{n}/{len(queue)} {ts} {counts} rows={total_rows} {rate:.2f} files/s", flush=True)
        batch.finish()
    counts["error"] += batch.errors
    print(f"done in {time.time() - started:.0f}s: {counts}, {total_rows} rows")
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
