#!/usr/bin/env python3
"""
Road closures near the Bell Centre.

Pulls the City of Montreal's "Entraves et travaux en cours" open data and writes:
  docs/closures.json   - closures active now or starting within 14 days,
                         within RADIUS_M of the Bell Centre

Two CSVs make up the dataset. The main one carries the permit (dates, reason,
borough) and - usefully - a longitude/latitude pair, so closures are located by
haversine distance, not by street-name matching. The companion "impacts" CSV
carries the affected street segments and joins to the main one on
id_request -> id.

Data: Ville de Montreal, CC-BY 4.0.
Stdlib only - no pip installs needed. Never touches the calendar/.ics logic.
"""

import csv
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

DATASET = "667342f7-f667-4c3c-9837-65e81312cd8d"
ENTRAVES_URL = (
    f"https://donnees.montreal.ca/dataset/{DATASET}/resource/"
    "cc41b532-f12d-40fb-9f55-eb58c9a2b12b/download/entraves-travaux-en-cours.csv"
)
IMPACTS_URL = (
    f"https://donnees.montreal.ca/dataset/{DATASET}/resource/"
    "a2bc8014-488c-495d-941b-e7ae1999d1bd/download/impacts-entraves-travaux-en-cours.csv"
)

BELL_LAT, BELL_LON = 45.4960, -73.5693
RADIUS_M = 600
HORIZON_DAYS = 14
OUT_DIR = "docs"
OUT_FILE = "closures.json"

# The city's timestamps are day boundaries, so a permit's real extent is a range
# of local calendar days. See city_local_date() for the sign quirk.
SHIFTED_TIMES = {"19:00:00", "20:00:00", "18:59:59", "19:59:59"}

# "Rue barree" is the only impact type that closes a street outright; the others
# take a parking or travel lane. Worth surfacing separately on the dashboard.
STREET_CLOSED = "Rue barrée"
NO_IMPACT = "Aucun impact / non applicable"


def fetch_csv(url, label):
    """Download a CSV, retrying: the portal returns 503 during its daily reload."""
    last = None
    for attempt in range(1, 5):
        req = urllib.request.Request(
            url, headers={"User-Agent": "bell-centre-tracker/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            print(f"  {label}: attempt {attempt} failed ({e})", file=sys.stderr)
            if attempt < 4:
                time.sleep(15 * attempt)
    else:
        print(f"ERROR: could not download {label}: {last}", file=sys.stderr)
        raise SystemExit(1)

    # The files are served without a BOM today, but utf-8-sig strips one if it
    # ever appears - a BOM would otherwise corrupt the first column name.
    text = raw.decode("utf-8-sig", "replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    print(f"  {label}: {len(raw)} bytes, {len(rows)} rows")
    return rows


def nth_sunday(year, month, nth):
    d = date(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7 + 7 * (nth - 1))


def montreal_offset_hours(dt_utc):
    """Hours Montreal is behind UTC: 4 during EDT, 5 during EST.

    Hand-rolled because zoneinfo needs an OS tz database, which Windows lacks
    unless the tzdata package is installed - and this script takes no
    dependencies. The US/Canada rule (2nd Sunday of March to 1st Sunday of
    November, switching at 02:00 local) has been stable since 2007.
    """
    y = dt_utc.year
    dst_start = datetime.combine(
        nth_sunday(y, 3, 2), datetime.min.time(), timezone.utc
    ) + timedelta(hours=7)  # 02:00 EST
    dst_end = datetime.combine(
        nth_sunday(y, 11, 1), datetime.min.time(), timezone.utc
    ) + timedelta(hours=6)  # 02:00 EDT
    return 4 if dst_start <= dt_utc < dst_end else 5


def parse_utc(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None


def city_local_date(dt_utc, stats):
    """The Montreal calendar day a permit timestamp refers to.

    The feed applies the UTC offset with the wrong sign. Every start is stamped
    20:00:00Z in EDT months and 19:00:00Z in EST months, every end 19:59:59Z /
    18:59:59Z - so *adding* the offset (not subtracting it) lands exactly on
    local midnight and 23:59:59, giving whole-day permits. Subtracting instead
    would report every closure a day early and end it mid-afternoon.

    Timestamps outside that fingerprint are converted normally, so if the city
    ever fixes the feed this keeps working; the counts are printed either way.
    """
    off = montreal_offset_hours(dt_utc)
    if dt_utc.strftime("%H:%M:%S") in SHIFTED_TIMES:
        stats["shifted"] += 1
        return (dt_utc + timedelta(hours=off)).date()
    stats["direct"] += 1
    return (dt_utc - timedelta(hours=off)).date()


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8  # mean Earth radius, metres
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def clean(s):
    """Street names in the impacts CSV carry a trailing space."""
    return (s or "").strip()


def segments_for(impact_rows):
    segs = []
    for r in impact_rows:
        street = clean(r.get("name")) or clean(r.get("streetid"))
        if not street:
            continue
        segs.append(
            {
                "street": street,
                "from": clean(r.get("fromshortname")) or None,
                "to": clean(r.get("toshortname")) or None,
                "impact": clean(r.get("streetimpacttype")) or None,
                "sidewalk": clean(r.get("sidewalk_blockedtype")) or None,
                "bike_path": clean(r.get("bikepath_blockedtype")) or None,
            }
        )
    return segs


def build(entraves, impacts, today):
    by_request = {}
    for r in impacts:
        by_request.setdefault(clean(r.get("id_request")), []).append(r)

    horizon = today + timedelta(days=HORIZON_DAYS)
    stats = {"shifted": 0, "direct": 0}
    no_coords = no_dates = 0
    in_radius = 0
    out = []

    for r in entraves:
        try:
            lat = float(r["latitude"])
            lon = float(r["longitude"])
        except (TypeError, ValueError, KeyError):
            no_coords += 1
            continue

        dist = haversine_m(BELL_LAT, BELL_LON, lat, lon)
        if dist > RADIUS_M:
            continue
        in_radius += 1

        start_utc = parse_utc(r.get("duration_start_date"))
        end_utc = parse_utc(r.get("duration_end_date"))
        if not start_utc:
            no_dates += 1
            continue
        start = city_local_date(start_utc, stats)
        end = city_local_date(end_utc, stats) if end_utc else None

        # Active now, or starting inside the horizon. A permit with no end date
        # is treated as open-ended rather than dropped.
        if end and end < today:
            continue
        if start > horizon:
            continue

        segs = segments_for(by_request.get(clean(r.get("id")), []))
        streets = sorted({s["street"] for s in segs})
        out.append(
            {
                "id": clean(r.get("id")),
                "permit_id": clean(r.get("permit_permit_id")) or None,
                "reason": clean(r.get("reason_category")) or None,
                "zone": clean(r.get("occupancy_name")) or None,
                "borough": clean(r.get("boroughid")) or None,
                "organization": clean(r.get("organizationname")) or None,
                "streets": streets,
                "segments": segs,
                "start_date": start.isoformat(),
                "end_date": end.isoformat() if end else None,
                "status": clean(r.get("currentstatus")) or None,
                "state": "active" if start <= today else "upcoming",
                "street_closed": any(
                    (s["impact"] or "") == STREET_CLOSED for s in segs
                ),
                "distance_m": round(dist),
            }
        )

    # Nearest first; among equals, the ones already under way.
    out.sort(key=lambda c: (c["distance_m"], c["start_date"]))

    print(
        f"Scanned {len(entraves)} permits: {in_radius} within {RADIUS_M} m, "
        f"{len(out)} active now or starting within {HORIZON_DAYS} days."
    )
    print(
        f"  dates: {stats['shifted']} shifted-encoding, {stats['direct']} direct; "
        f"{no_coords} without coordinates, {no_dates} without a start date."
    )
    if stats["direct"] and stats["shifted"]:
        print(
            "NOTE: mixed timestamp encodings in the feed - the city may be "
            "changing the format. Check city_local_date()."
        )
    unlocated = sum(1 for c in out if not c["streets"])
    if unlocated:
        print(f"  {unlocated} closure(s) have no street segment rows in the impacts CSV.")

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source": "Ville de Montréal — Entraves et travaux en cours",
        "source_url": f"https://donnees.montreal.ca/dataset/{DATASET}",
        "attribution": "Données: Ville de Montréal",
        "license": "CC-BY 4.0",
        "reference_point": {"name": "Bell Centre", "lat": BELL_LAT, "lon": BELL_LON},
        "radius_m": RADIUS_M,
        "horizon_days": HORIZON_DAYS,
        "counts": {
            "scanned": len(entraves),
            "in_radius": in_radius,
            "reported": len(out),
            "active": sum(1 for c in out if c["state"] == "active"),
            "upcoming": sum(1 for c in out if c["state"] == "upcoming"),
        },
        "closures": out,
    }


def write_if_changed(path, new_text):
    """Skip the write when only the per-run "generated" line differs.

    Mirrors fetch_events.py: without this the daily run would dirty the file
    every day and commit a timestamp change with no real closure change.
    """
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
        strip = lambda t: "\n".join(
            l for l in t.splitlines() if not l.lstrip().startswith('"generated"')
        )
        if strip(old) == strip(new_text):
            return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return True


def main():
    print("Downloading Ville de Montréal entraves data...")
    entraves = fetch_csv(ENTRAVES_URL, "entraves")
    impacts = fetch_csv(IMPACTS_URL, "impacts")

    # A schema change upstream would otherwise show up as "0 closures found",
    # which looks like a quiet day rather than a broken script.
    required = {"id", "latitude", "longitude", "duration_start_date", "duration_end_date"}
    missing = required - set(entraves[0].keys() if entraves else [])
    if missing:
        print(
            f"ERROR: entraves CSV is missing expected column(s): {sorted(missing)}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    today = (datetime.now(timezone.utc) - timedelta(hours=montreal_offset_hours(
        datetime.now(timezone.utc)))).date()
    payload = build(entraves, impacts, today)

    os.makedirs(OUT_DIR, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path = os.path.join(OUT_DIR, OUT_FILE)
    if write_if_changed(path, text):
        print(f"Wrote {path}")
    else:
        print("No closure changes; output file left untouched.")


if __name__ == "__main__":
    main()
