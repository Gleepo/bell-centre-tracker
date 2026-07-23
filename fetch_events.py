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
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://app.ticketmaster.com/discovery/v2"
API_KEY = os.environ.get("TM_API_KEY")
VENUE_CACHE = "venue_id.txt"  # cached after first successful lookup
OUT_DIR = "docs"


def api_get(path, **params):
    params["apikey"] = API_KEY
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "bell-centre-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


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
                "segment": (ev.get("classifications") or [{}])[0]
                .get("segment", {})
                .get("name"),
                "url": ev.get("url"),
            }
        )
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "venue": "Bell Centre",
        "events": slim,
    }


def main():
    if not API_KEY:
        print("ERROR: TM_API_KEY env var is not set.", file=sys.stderr)
        sys.exit(1)
    os.makedirs(OUT_DIR, exist_ok=True)
    venue_id = resolve_venue_id()
    events = fetch_events(venue_id)
    with open(os.path.join(OUT_DIR, "bell-centre.ics"), "w", newline="") as f:
        f.write(build_ics(events))
    with open(os.path.join(OUT_DIR, "events.json"), "w") as f:
        json.dump(build_json(events), f, indent=2, ensure_ascii=False)
    print(f"Wrote {OUT_DIR}/bell-centre.ics and {OUT_DIR}/events.json")


if __name__ == "__main__":
    main()
