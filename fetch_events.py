#!/usr/bin/env python3
"""
Bell Centre event fetcher.

Pulls upcoming events at the Bell Centre from the Ticketmaster Discovery API
and writes:
  docs/bell-centre.ics   - subscribable calendar feed
  docs/events.json       - raw-ish event list for any future UI

Requires env var TM_API_KEY (Ticketmaster consumer key).
Stdlib only - no pip installs needed.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://app.ticketmaster.com/discovery/v2"
RAW_KEY = os.environ.get("TM_API_KEY") or ""
# A key pasted from a web page can carry a trailing newline, and zero-width
# characters that survive .strip(). Both make a good key look "Invalid".
JUNK = r"[\s​‌‍⁠﻿‎‏]"
API_KEY = re.sub(JUNK, "", RAW_KEY)
VENUE_CACHE = "venue_id.txt"  # cached after first successful lookup
OUT_DIR = "docs"
# Lines that change on every run regardless of whether any event changed.
# A folded ICS continuation line always starts with a space, so ^DTSTAMP is
# safe against matching mid-value.
VOLATILE = re.compile(r'^(DTSTAMP:.*|\s*"generated":.*)$', re.M)

# Ticketmaster sells hospitality add-ons (dinner packages, lounge access,
# venue tours, meet-and-greets) as separate "events" at the venue. They all
# classify as Miscellaneous and follow a short list of name patterns. Match on
# the pattern rather than on the segment alone: a real event classified
# Miscellaneous is kept and reported, so it can never vanish silently.
PACKAGE_PATTERNS = [
    re.compile(r"^Centre Bell\s+-\s", re.I),
    re.compile(r"^Visites Guid[eé]es Centre Bell", re.I),
    re.compile(r"^Salon des Directeurs CIBC", re.I),
    re.compile(r"^TICKETLESS:", re.I),
]


def api_get(path, **params):
    params["apikey"] = API_KEY
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "bell-centre-tracker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # The body carries the real reason (e.g. "Invalid ApiKey"). Never print
        # the URL - it has the key in its query string.
        body = e.read().decode("utf-8", "replace")[:500]
        print(f"ERROR: {path} returned HTTP {e.code} {e.reason}", file=sys.stderr)
        print(f"       {body}", file=sys.stderr)
        raise SystemExit(1)


def resolve_venue_id():
    """Find the Bell Centre's Ticketmaster venue ID (cached after first run)."""
    if os.path.exists(VENUE_CACHE):
        with open(VENUE_CACHE) as f:
            cached = f.read().strip()
        if cached:
            return cached

    data = api_get("venues.json", keyword="Centre Bell", countryCode="CA", size=20)
    venues = data.get("_embedded", {}).get("venues", [])
    if not venues:
        # Try the English name too
        data = api_get("venues.json", keyword="Bell Centre", countryCode="CA", size=20)
        venues = data.get("_embedded", {}).get("venues", [])

    match = None
    for v in venues:
        city = (v.get("city", {}).get("name") or "").lower()
        name = (v.get("name") or "").lower()
        if "montr" in city and ("bell" in name and ("centre" in name or "center" in name)):
            match = v
            break

    if not match:
        print("ERROR: Could not resolve Bell Centre venue ID. Candidates were:", file=sys.stderr)
        for v in venues:
            print(f"  {v.get('id')}  {v.get('name')}  ({v.get('city', {}).get('name')})", file=sys.stderr)
        sys.exit(1)

    vid = match["id"]
    with open(VENUE_CACHE, "w") as f:
        f.write(vid)
    print(f"Resolved venue: {match.get('name')} -> {vid}")
    return vid


def fetch_events(venue_id):
    events, page = [], 0
    while True:
        data = api_get(
            "events.json",
            venueId=venue_id,
            sort="date,asc",
            size=100,
            page=page,
        )
        batch = data.get("_embedded", {}).get("events", [])
        events.extend(batch)
        page_info = data.get("page", {})
        page += 1
        if page >= page_info.get("totalPages", 1) or not batch:
            break
    return events


def segment_of(ev):
    """Segment name for an event, tolerating missing/null classification data."""
    cls = (ev.get("classifications") or [{}])[0] or {}
    return (cls.get("segment") or {}).get("name")


def drop_packages(events):
    """Remove hospitality add-ons, keeping anything that isn't a known package."""
    kept, dropped, unmatched = [], 0, []
    for ev in events:
        name = ev.get("name") or ""
        if segment_of(ev) == "Miscellaneous":
            if any(p.search(name) for p in PACKAGE_PATTERNS):
                dropped += 1
                continue
            unmatched.append(name or "(unnamed)")
        kept.append(ev)

    print(f"Filtered {dropped} hospitality package(s); {len(kept)} events remain.")
    if unmatched:
        # Not a known package, so it stays in the feed - but say so loudly, as
        # it means the patterns above may need updating.
        print(f"NOTE: kept {len(unmatched)} Miscellaneous event(s) matching no known package pattern:")
        for n in sorted(set(unmatched)):
            print(f"  - {n}")
    return kept


def ics_escape(text):
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line):
    """RFC 5545 line folding at 75 octets."""
    out = []
    while len(line.encode("utf-8")) > 75:
        # find a safe cut point
        cut = 75
        while len(line[:cut].encode("utf-8")) > 75:
            cut -= 1
        out.append(line[:cut])
        line = " " + line[cut:]
    out.append(line)
    return "\r\n".join(out)


def build_ics(events):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//bell-centre-tracker//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Bell Centre Events",
        "X-WR-TIMEZONE:America/Toronto",
    ]
    skipped = 0
    for ev in events:
        dates = ev.get("dates", {}).get("start", {})
        dt_utc = dates.get("dateTime")  # e.g. 2026-08-01T23:30:00Z
        local_date = dates.get("localDate")
        name = ev.get("name", "Bell Centre event")
        status = ev.get("dates", {}).get("status", {}).get("code", "")
        if status == "cancelled":
            continue

        lines.append("BEGIN:VEVENT")
        lines.append(fold(f"UID:{ev.get('id','unknown')}@bell-centre-tracker"))
        lines.append(f"DTSTAMP:{now}")
        if dt_utc:
            stamp = dt_utc.replace("-", "").replace(":", "")
            lines.append(f"DTSTART:{stamp}")
            lines.append("DURATION:PT3H")  # estimate; TM rarely provides end times
        elif local_date:
            # time TBA -> all-day entry
            lines.append(f"DTSTART;VALUE=DATE:{local_date.replace('-', '')}")
            name += " (time TBA)"
            skipped += 1
        else:
            lines.append("END:VEVENT")
            continue
        lines.append(fold(f"SUMMARY:{ics_escape(name)}"))
        lines.append(fold("LOCATION:Bell Centre\\, Montreal"))
        url = ev.get("url")
        if url:
            lines.append(fold(f"DESCRIPTION:{ics_escape(url)}"))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    print(f"ICS built: {len(events)} events fetched, {skipped} with time TBA.")
    return "\r\n".join(lines) + "\r\n"


def build_json(events):
    slim = []
    for ev in events:
        dates = ev.get("dates", {}).get("start", {})
        slim.append(
            {
                "id": ev.get("id"),
                "name": ev.get("name"),
                "localDate": dates.get("localDate"),
                "localTime": dates.get("localTime"),
                "dateTimeUTC": dates.get("dateTime"),
                "status": ev.get("dates", {}).get("status", {}).get("code"),
                "segment": segment_of(ev),
                "url": ev.get("url"),
            }
        )
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "venue": "Bell Centre",
        "events": slim,
    }


def write_if_changed(path, new_text):
    """Write only when the content differs ignoring per-run timestamps.

    DTSTAMP (ICS) and "generated" (JSON) are regenerated every run, so a plain
    write would dirty both files daily and produce a commit a day with no real
    event changes. Comparing with those lines blanked keeps the history honest.
    """
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            old = f.read()
        if VOLATILE.sub("", old) == VOLATILE.sub("", new_text):
            return False
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(new_text)
    return True


def main():
    if not API_KEY:
        print("ERROR: TM_API_KEY env var is not set.", file=sys.stderr)
        sys.exit(1)
    if API_KEY != RAW_KEY:
        removed = len(RAW_KEY) - len(API_KEY)
        print(f"NOTE: stripped {removed} whitespace/zero-width char(s) from TM_API_KEY.")
    # Shape check only - never prints the key itself. A Ticketmaster consumer
    # key is 32 alphanumeric characters.
    if not re.fullmatch(r"[A-Za-z0-9]{32}", API_KEY):
        n_alnum = sum(c.isalnum() and c.isascii() for c in API_KEY)
        print(
            f"WARNING: TM_API_KEY is not shaped like a Ticketmaster consumer key "
            f"(expected 32 alphanumeric chars; got {len(API_KEY)} chars, "
            f"{n_alnum} of them ASCII alphanumeric).",
            file=sys.stderr,
        )
    os.makedirs(OUT_DIR, exist_ok=True)
    venue_id = resolve_venue_id()
    events = drop_packages(fetch_events(venue_id))

    ics = build_ics(events)
    payload = json.dumps(build_json(events), indent=2, ensure_ascii=False) + "\n"
    written = [
        name
        for name, text in (("bell-centre.ics", ics), ("events.json", payload))
        if write_if_changed(os.path.join(OUT_DIR, name), text)
    ]
    if written:
        print(f"Wrote {', '.join(OUT_DIR + '/' + n for n in written)}")
    else:
        print("No event changes; output files left untouched.")


if __name__ == "__main__":
    main()
