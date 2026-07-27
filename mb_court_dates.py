#!/usr/bin/env python3
"""
Manitoba Court Registry - available court dates scraper (Winnipeg-KB).
Version 6.

New in v6: individual broken links are now noticed.

Before this, a run was only flagged if more than a fifth of categories
broke at once. Eight silent failures out of forty passed as healthy, so
one hearing type could stop working for weeks without anyone knowing.

Now every category's failures are counted across runs in
data/health.json. A single timeout on a slow government server stays
quiet, which is right, because that is ordinary noise. But a category
that fails CONSECUTIVE_FAIL_LIMIT times in a row is named in the log and
fails the run, so the scheduler emails you.

latest.json also carries an "unreadable" list now, so the website can
say "we could not check this one" instead of the false and more damaging
"no dates available".

What it writes, all inside a "data" folder:

  data/runs/<timestamp>.json.gz   every run, forever, compressed
  data/raw/<timestamp>.json.gz    the raw HTML of every page, forever
  data/history.csv                one row per category per run, for trends
  data/latest.json                current state, for the website to read
  data/catalogue.json             the list of categories we know about
  data/health.json                consecutive failure count per category

Other safety rules, unchanged:

  A run that looks broken overall is still saved but is NOT promoted to
  latest.json, so the website keeps showing the last good data.

  The catalogue is compared every run. If the registry adds, removes or
  renames a hearing type, you find out the day it happens.

Usage:
    pip install -r requirements.txt
    python mb_court_dates.py
"""

import csv
import gzip
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- settings

# CHANGE THIS to a real address before you run it.
CONTACT_EMAIL = "tom@courtready.ca"

BASE = "https://web43.gov.mb.ca"
REGISTRY_HOME = "https://web43.gov.mb.ca/Registry/"
LOCATION_DESC = "Winnipeg-KB"
LOCATION_URL = (
    "https://web43.gov.mb.ca/Registry/AvailableCourtDates/HearingTypeSelect"
    "?LocationCode=01%20%20&LocationDesc=Winnipeg-KB"
)

DELAY_SECONDS = 2.0
TIMEOUT_SECONDS = 45
MAX_ATTEMPTS = 3

# If another run finished less than this many minutes ago, skip.
SKIP_IF_RECENT_MINUTES = 20

# How many runs in a row a single category must fail before we shout.
# At five runs a day, three in a row is roughly half a day.
CONSECUTIVE_FAIL_LIMIT = 3

WINNIPEG = ZoneInfo("America/Winnipeg")
DATA = "data"

# A run must clear all of these to be published at all.
MIN_CATEGORIES = 30       # we currently expect 40
MIN_OK_FRACTION = 0.50    # at least half must return real data
MAX_BAD_FRACTION = 0.20   # at most a fifth may fail or mismatch

HEADERS = {
    "User-Agent": (
        f"CourtreadyBot/1.0 (+https://courtready.ca; contact: {CONTACT_EMAIL}) "
        "public court availability monitoring"
    )
}

BROKEN = ("FETCH_FAILED", "MISMATCH")


# ---------------------------------------------------------------- helpers


def norm(s):
    """Flatten a label for comparison: collapse spaces, trim, uppercase."""
    return " ".join(s.split()).upper()


def code_from_url(url):
    """Pull the hearing type code out of a link, spaces and all."""
    q = parse_qs(urlparse(url).query, keep_blank_values=True)
    return q.get("HearingTypeCode", [""])[0]


def forced():
    return os.environ.get("FORCE_RUN", "").strip().lower() in ("1", "true", "yes")


def minutes_since_last_run(now):
    """How long ago the previous run finished, in minutes. None if never."""
    folder = os.path.join(DATA, "runs")
    if not os.path.isdir(folder):
        return None

    stamps = []
    for fn in os.listdir(folder):
        if not fn.endswith(".json.gz"):
            continue
        try:
            t = datetime.strptime(fn[:-8], "%Y-%m-%dT%H%M")
        except ValueError:
            continue
        stamps.append(t.replace(tzinfo=WINNIPEG))

    if not stamps:
        return None
    return (now - max(stamps)).total_seconds() / 60.0


def get(session, url):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            print(f"    attempt {attempt}/{MAX_ATTEMPTS} failed: {e}", file=sys.stderr)
            if attempt < MAX_ATTEMPTS:
                time.sleep(DELAY_SECONDS * attempt)
    return None


def read_registry_timestamp(session):
    """
    The Registry home page prints "Database last updated on <date time>".
    That is when the court refreshed its data, which is not the same as
    when we looked. Returns (raw_string, iso_string_or_None, html).
    """
    r = get(session, REGISTRY_HOME)
    if r is None:
        return None, None, ""

    text = " ".join(BeautifulSoup(r.text, "html.parser").get_text(" ").split())

    m = re.search(
        r"Database last updated on\s+"
        r"([A-Za-z]{3,9}\s+\d{1,2}\s+\d{4}\s+\d{1,2}:\d{2}:\d{2})",
        text,
    )
    if not m:
        return None, None, r.text

    raw = m.group(1)
    iso = None
    for fmt in ("%b %d %Y %H:%M:%S", "%B %d %Y %H:%M:%S"):
        try:
            iso = datetime.strptime(raw, fmt).replace(tzinfo=WINNIPEG).isoformat()
            break
        except ValueError:
            continue

    return raw, iso, r.text


def collect_links(session):
    r = get(session, LOCATION_URL)
    if r is None:
        raise SystemExit("Could not load the hearing type list. Aborting.")

    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        if "ShowCourtDates" in a["href"]:
            links.append((a.get_text().strip(), urljoin(BASE, a["href"])))
    return links, r.text


def parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    hearing_on_page = ""
    for i, ln in enumerate(lines):
        if ln == LOCATION_DESC and i + 1 < len(lines):
            hearing_on_page = lines[i + 1]
            break

    found = "not found" not in text.lower()

    rows = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [" ".join(td.get_text().split()) for td in tr.find_all("td")]
            if len(cells) >= 2 and cells[0] and cells[0].lower() != "date":
                rows.append((cells[0], cells[1]))

    return hearing_on_page, rows, found


def days_until(date_str, today):
    try:
        d = datetime.strptime(date_str, "%d-%b-%Y").date()
        return (d - today).days
    except ValueError:
        return None


def write_gz(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f)


def check_catalogue(results):
    """Compare this run's categories to the ones we already knew about."""
    path = os.path.join(DATA, "catalogue.json")
    current = sorted({(r["code"], norm(r["hearing_type"])) for r in results})
    current_list = [{"code": c, "name": n} for c, n in current]

    if not os.path.exists(path):
        os.makedirs(DATA, exist_ok=True)
        with open(path, "w") as f:
            json.dump(current_list, f, indent=2)
        return []

    with open(path) as f:
        previous = {(d["code"], d["name"]) for d in json.load(f)}

    changes = []
    for c, n in sorted(set(current) - previous):
        changes.append(f"NEW category: {c} {n}")
    for c, n in sorted(previous - set(current)):
        changes.append(f"GONE category: {c} {n}")

    if changes:
        with open(path, "w") as f:
            json.dump(current_list, f, indent=2)

    return changes


def update_health(results, now):
    """
    Count consecutive failures per category.

    Returns (streaks, persistent) where streaks maps code to how many runs
    in a row it has failed, and persistent lists the ones that have hit
    the limit and deserve an email.
    """
    path = os.path.join(DATA, "health.json")

    previous = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                previous = json.load(f)
        except (ValueError, OSError):
            previous = {}

    streaks = {}
    persistent = []

    for r in results:
        code = r["code"]
        was = previous.get(code, {})
        if r["status"] in BROKEN:
            count = int(was.get("consecutive_failures", 0)) + 1
            streaks[code] = {
                "consecutive_failures": count,
                "hearing_type": r["hearing_type"],
                "last_status": r["status"],
                "first_failed_at": was.get("first_failed_at") or now.isoformat(),
                "last_failed_at": now.isoformat(),
            }
            if count >= CONSECUTIVE_FAIL_LIMIT:
                persistent.append(
                    f"{r['hearing_type']} ({code.strip()}) has failed "
                    f"{count} runs in a row, {r['status']}"
                )
        else:
            streaks[code] = {
                "consecutive_failures": 0,
                "hearing_type": r["hearing_type"],
                "last_status": r["status"],
                "last_ok_at": now.isoformat(),
            }

    os.makedirs(DATA, exist_ok=True)
    with open(path, "w") as f:
        json.dump(streaks, f, indent=2)

    return streaks, persistent


# ---------------------------------------------------------------- main


def main():
    now = datetime.now(WINNIPEG)
    today = now.date()
    tag = now.strftime("%Y-%m-%dT%H%M")

    # Skip guard. The backup trigger runs behind the primary one, so most
    # of the time it has nothing to do.
    if not forced():
        gap = minutes_since_last_run(now)
        if gap is not None and gap >= 0 and gap < SKIP_IF_RECENT_MINUTES:
            print(f"A scrape finished {gap:.0f} minutes ago. Nothing to do.")
            print("Set FORCE_RUN=1 to override.")
            return

    session = requests.Session()
    print(f"Run {tag} ({LOCATION_DESC})")

    reg_raw, reg_iso, reg_html = read_registry_timestamp(session)
    if reg_raw:
        print(f"Registry says its database was last updated: {reg_raw}")
    else:
        print("Could not read the registry's own timestamp.")

    print("Reading the hearing type list...")
    links, list_html = collect_links(session)
    print(f"Found {len(links)} hearing types.\n")

    raw_pages = {"__home__": reg_html, "__list__": list_html}
    results = []

    for idx, (name, url) in enumerate(links, 1):
        clean_name = " ".join(name.split())
        code = code_from_url(url)
        print(f"[{idx}/{len(links)}] {clean_name}")
        time.sleep(DELAY_SECONDS)

        base_row = {
            "code": code,
            "hearing_type": clean_name,
            "url": url,
            "earliest_date": None,
            "days_out": None,
            "slots_total": None,
            "dates": [],
        }

        r = get(session, url)
        if r is None:
            results.append({**base_row, "status": "FETCH_FAILED"})
            print("    -> could not fetch")
            continue

        raw_pages[code] = r.text
        on_page, rows, found = parse_page(r.text)

        if norm(on_page) != norm(name):
            results.append({**base_row, "status": "MISMATCH",
                            "note": f"page showed '{on_page}'"})
            print(f"    -> MISMATCH: page showed '{on_page}'. Discarded.")
            continue

        if not found or not rows:
            results.append({**base_row, "status": "NO_DATES", "slots_total": 0})
            print("    -> no dates available")
            continue

        earliest = rows[0][0]
        results.append({**base_row,
                        "status": "OK",
                        "earliest_date": earliest,
                        "days_out": days_until(earliest, today),
                        "slots_total": len(rows),
                        "dates": [{"date": d, "time": t} for d, t in rows]})
        print(f"    -> {len(rows)} dates, earliest {earliest}")

    # ------------------------------------------------------ health checks

    total = len(results)
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    ok = counts.get("OK", 0)
    bad = counts.get("FETCH_FAILED", 0) + counts.get("MISMATCH", 0)

    # Whole-run problems. These stop the data being published at all.
    problems = []
    if total < MIN_CATEGORIES:
        problems.append(f"only {total} categories found, expected at least {MIN_CATEGORIES}")
    if total and ok / total < MIN_OK_FRACTION:
        problems.append(f"only {ok} of {total} returned data")
    if total and bad / total > MAX_BAD_FRACTION:
        problems.append(f"{bad} of {total} failed or mismatched")

    catalogue_changes = check_catalogue(results)
    streaks, persistent = update_health(results, now)
    healthy = not problems

    unreadable = [
        {"code": r["code"], "hearing_type": r["hearing_type"], "status": r["status"]}
        for r in results if r["status"] in BROKEN
    ]

    run_record = {
        "run_id": tag,
        "scraped_at": now.isoformat(),
        "registry_last_updated_raw": reg_raw,
        "registry_last_updated": reg_iso,
        "location": LOCATION_DESC,
        "healthy": healthy,
        "problems": problems,
        "catalogue_changes": catalogue_changes,
        "persistent_failures": persistent,
        "counts": counts,
        "results": results,
    }

    # ------------------------------------------------------ write files

    write_gz(os.path.join(DATA, "runs", f"{tag}.json.gz"), run_record)
    write_gz(os.path.join(DATA, "raw", f"{tag}.json.gz"), raw_pages)

    hist_path = os.path.join(DATA, "history.csv")
    new_file = not os.path.exists(hist_path)
    os.makedirs(DATA, exist_ok=True)
    with open(hist_path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["run_id", "scraped_at", "registry_last_updated",
                        "location", "code", "hearing_type", "status",
                        "earliest_date", "days_out", "slots_total", "healthy"])
        for r in results:
            w.writerow([tag, now.isoformat(), reg_raw, LOCATION_DESC,
                        r["code"], r["hearing_type"], r["status"],
                        r["earliest_date"], r["days_out"], r["slots_total"],
                        healthy])

    if healthy:
        summary = {
            "run_id": tag,
            "scraped_at": now.isoformat(),
            "registry_last_updated_raw": reg_raw,
            "registry_last_updated": reg_iso,
            "location": LOCATION_DESC,
            "counts": counts,
            "unreadable": unreadable,
            "results": results,
        }
        with open(os.path.join(DATA, "latest.json"), "w") as f:
            json.dump(summary, f, indent=2)

    # ------------------------------------------------------ report

    print("\n--- Summary ---")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")

    if unreadable:
        print(f"\n{len(unreadable)} could not be read this run:")
        for u in unreadable:
            streak = streaks.get(u["code"], {}).get("consecutive_failures", 1)
            word = "run" if streak == 1 else "runs in a row"
            print(f"  {u['hearing_type']} ({u['status']}), {streak} {word}")

    if catalogue_changes:
        print("\n*** THE REGISTRY CHANGED ITS CATEGORY LIST ***")
        for c in catalogue_changes:
            print(f"  {c}")

    if persistent:
        print("\n*** THESE CATEGORIES LOOK GENUINELY BROKEN ***")
        for p in persistent:
            print(f"  {p}")
        print("  Open the link on the registry by hand and see what it does.")

    if healthy:
        print(f"\nRun looks good. Published to {DATA}/latest.json")
    else:
        print("\n*** RUN QUARANTINED, latest.json NOT updated ***")
        for p in problems:
            print(f"  {p}")

    # A non-zero exit makes the scheduler send you an email.
    if problems or catalogue_changes or persistent:
        sys.exit(1)


if __name__ == "__main__":
    main()
