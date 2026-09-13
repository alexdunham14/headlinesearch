#!/usr/bin/env python3
"""Which outlets have gone quiet in GDELT this week? Stdlib only. Run weekly
by .github/workflows/quiet.yml, which commits the result and opens a GitHub
issue when there is something new; that issue is the alert.

  scripts/quiet.py snapshot            # today's sites from the public API into data/quiet/snapshots/
  scripts/quiet.py report              # compare the snapshots, write data/quiet.json and data/quiet/report.md
  scripts/quiet.py seed weekly.tsv current.tsv   # synthetic weekly snapshots from the database, once

Why: sessions/2026-09-12-gdelt-source-coverage.md in the root repo. GDELT
loses outlets to bot walls, whole publisher groups on one day, and the
losses are only found by looking. The site's /api/sources gives every
site's row count and last day, so a weekly snapshot of the sites with
5,000 rows or more (a few thousand sites) makes two things visible:

- dark: a site whose last headline is two weeks old or more, though it had
  been giving a hundred rows a week or more (the baseline: the mean of its
  weekly increments over the eight weeks before the last two);
- thinning: a site whose last two weeks ran under a fifth of a baseline of
  two hundred a week or more, though it is not dark.

A site is reported once, the week it first meets a test, with its baseline,
its recent rate and its last day; data/quiet.json keeps every site
currently flagged with the week it was first flagged, and a site that
recovers drops out. Sites that stopped on the same day are grouped in the
report, since a group stopping together is a publisher's wall going up.

The snapshots are TSV (domain, rows, last day), one file a week, the
sixteen newest kept in the tree (git has the rest). The API lists sites by
first letter, 10,000 a letter, alphabetically; every letter is under that
(the biggest, "t", has 7,200 sites of all sizes), so nothing is cut.
"""

import collections
import datetime as dt
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SNAPDIR = os.path.join(ROOT, "data", "quiet", "snapshots")
QUIET = os.path.join(ROOT, "data", "quiet.json")
REPORT = os.path.join(ROOT, "data", "quiet", "report.md")
API = "https://newsheadlinesearch.com/api/sources?letter="
MIN_ROWS = 5000
KEEP = 16
DARK_DAYS = 14
DARK_BASELINE = 100
THIN_BASELINE = 200
THIN_RATIO = 0.2


def today():
    return dt.date.today()


def snapshot():
    rows = {}
    for letter in "abcdefghijklmnopqrstuvwxyz0":
        req = urllib.request.Request(API + letter, headers={"User-Agent": "headlinesearch quiet.py (+https://newsheadlinesearch.com)"})
        with urllib.request.urlopen(req, timeout=120) as r:
            body = json.load(r)
        for domain, n, first, last in body["sources"]:
            if n >= MIN_ROWS:
                rows[domain] = (n, last)
    os.makedirs(SNAPDIR, exist_ok=True)
    path = os.path.join(SNAPDIR, f"{today():%Y-%m-%d}.tsv")
    with open(path, "w") as fh:
        for domain in sorted(rows):
            n, last = rows[domain]
            fh.write(f"{domain}\t{n}\t{last}\n")
    print(f"{path}: {len(rows)} sites with {MIN_ROWS}+ rows")
    prune()


def prune():
    files = sorted(os.listdir(SNAPDIR))
    for f in files[:-KEEP]:
        os.remove(os.path.join(SNAPDIR, f))


def load_snapshots():
    out = []
    for f in sorted(os.listdir(SNAPDIR)):
        if not f.endswith(".tsv"):
            continue
        day = dt.date.fromisoformat(f[:-4])
        rows = {}
        with open(os.path.join(SNAPDIR, f)) as fh:
            for line in fh:
                domain, n, last = line.rstrip("\n").split("\t")
                rows[domain] = (int(n), last)
        out.append((day, rows))
    return out


def report():
    snaps = load_snapshots()
    if len(snaps) < 3:
        sys.exit(f"{len(snaps)} snapshots; three are needed")
    day, now = snaps[-1]
    # Weekly increments per site between consecutive snapshots, scaled to seven days.
    inc = collections.defaultdict(list)
    for (d0, r0), (d1, r1) in zip(snaps, snaps[1:]):
        days = max(1, (d1 - d0).days)
        for domain, (n1, _) in r1.items():
            if domain in r0:
                inc[domain].append((r1[domain][0] - r0[domain][0]) * 7 / days)
    flags = {}
    for domain, (n, last) in now.items():
        series = inc.get(domain, [])
        if len(series) < 3:
            continue
        recent = series[-2:]
        base = series[-10:-2] or series[:-2]
        baseline = sum(base) / len(base)
        recent_rate = sum(recent) / len(recent)
        last_day = dt.date.fromisoformat(last)
        age = (day - last_day).days
        if age >= DARK_DAYS and baseline >= DARK_BASELINE:
            flags[domain] = {"why": "dark", "baseline": round(baseline), "recent": round(recent_rate), "last": last, "rows": n}
        elif baseline >= THIN_BASELINE and recent_rate < THIN_RATIO * baseline:
            flags[domain] = {"why": "thinning", "baseline": round(baseline), "recent": round(recent_rate), "last": last, "rows": n}
    prev = {}
    if os.path.exists(QUIET):
        prev = json.load(open(QUIET)).get("sites", {})
    out = {}
    for domain, f in flags.items():
        f["since"] = prev.get(domain, {}).get("since", f"{day:%Y-%m-%d}")
        out[domain] = f
    new = sorted((d for d in out if d not in prev), key=lambda d: -out[d]["baseline"])
    recovered = sorted(d for d in prev if d not in out)
    json.dump({"day": f"{day:%Y-%m-%d}", "sites": dict(sorted(out.items()))}, open(QUIET, "w"), indent=1)
    lines = [f"Week of {day:%Y-%m-%d}: {len(new)} newly quiet, {len(out)} quiet in all, {len(recovered)} recovered.", ""]
    if new:
        lines += ["| site | why | was a week | last two weeks | last day | rows |", "|---|---|---|---|---|---|"]
        for d in new:
            f = out[d]
            lines.append(f"| {d} | {f['why']} | {f['baseline']} | {f['recent']} | {f['last']} | {f['rows']} |")
        by_day = collections.defaultdict(list)
        for d in new:
            if out[d]["why"] == "dark":
                by_day[out[d]["last"]].append(d)
        groups = [(k, v) for k, v in by_day.items() if len(v) >= 3]
        if groups:
            lines += ["", "Stopped on the same day (a publisher's wall, usually):"]
            for k, v in sorted(groups):
                lines.append(f"- {k}: " + ", ".join(sorted(v)))
    if recovered:
        lines += ["", "Recovered: " + ", ".join(recovered)]
    lines += ["", "From the weekly snapshots of newsheadlinesearch.com/api/sources; scripts/quiet.py explains the tests."]
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    open(REPORT, "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    # For the workflow: the count of new flags on the last line of stdout.
    print(f"NEW={len(new)}")


def seed(weekly_path, current_path):
    """Synthetic weekly snapshots from the database, for the weeks before the
    first real one. weekly.tsv: domain, Monday, rows that week, last day
    that week; current.tsv: domain, rows in all, last day."""
    current = {}
    with open(current_path) as fh:
        for line in fh:
            domain, n, last = line.rstrip("\n").split("\t")
            current[domain] = (int(n), last)
    weeks = collections.defaultdict(dict)   # domain -> monday -> (rows, last)
    mondays = set()
    with open(weekly_path) as fh:
        for line in fh:
            domain, monday, c, last = line.rstrip("\n").split("\t")
            weeks[domain][monday] = (int(c), last)
            mondays.add(monday)
    mondays = sorted(mondays)
    # A snapshot "as of" the Sunday ending each week but the current one:
    # rows to date = rows now minus the rows of every later week; last day =
    # the latest week to that point with rows.
    os.makedirs(SNAPDIR, exist_ok=True)
    for i, monday in enumerate(mondays[:-1]):
        asof = dt.date.fromisoformat(monday) + dt.timedelta(days=6)
        later = mondays[i + 1:]
        path = os.path.join(SNAPDIR, f"{asof:%Y-%m-%d}.tsv")
        n_written = 0
        with open(path, "w") as fh:
            for domain in sorted(current):
                n_now, last_now = current[domain]
                w = weeks.get(domain, {})
                n = n_now - sum(w[m][0] for m in later if m in w)
                past = [w[m][1] for m in mondays[:i + 1] if m in w]
                last = past[-1] if past else (last_now if last_now <= f"{asof}" else "")
                if not last:
                    continue
                if n >= MIN_ROWS:
                    fh.write(f"{domain}\t{n}\t{last}\n")
                    n_written += 1
        print(f"{path}: {n_written} sites")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "snapshot":
        snapshot()
    elif cmd == "report":
        report()
    elif cmd == "seed" and len(sys.argv) == 4:
        seed(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
