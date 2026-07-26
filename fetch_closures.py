#!/usr/bin/env python3
"""
Road closures near the Bell Centre.

Pulls the City of Montreal's "Entraves et travaux en cours" open data and writes:
  docs/closures.json   - closures active now or starting within 14 days,
                         within RADIUS_M of the Bell Centre, each with the
                         geometry of the blocks it affects
  docs/streets.json    - nearby street centrelines, the map's basemap

Three city datasets are involved. The main "entraves" CSV carries the permit
(dates, reason, borough) and - usefully - a longitude/latitude pair, so closures
are located by haversine distance, not by street-name matching. The companion
"impacts" CSV carries the affected street segments and joins on
id_request -> id. Geobase supplies the road centrelines that turn a closure from
a dot into the actual stretch of street that is blocked.

Data: Ville de Montreal, CC-BY 4.0.
Stdlib only - no pip installs needed. Never touches the calendar/.ics logic.
"""

import collections
import csv
import io
import json
import math
import os
import re
import sys
import time
import unicodedata
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

GEOBASE_URL = (
    "https://donnees.montreal.ca/dataset/984f7a68-ab34-4092-9204-4bdfcca767c5/resource/"
    "9d3d60d8-4e7f-493e-8d6a-dcd040319d8d/download/geobase.json"
)

BELL_LAT, BELL_LON = 45.4960, -73.5693
RADIUS_M = 1000
HORIZON_DAYS = 14
# Draw a little past the radius so the map doesn't end in mid-street at its edge.
MAP_PAD_M = 250
OUT_DIR = "docs"
OUT_FILE = "closures.json"
STREETS_FILE = "streets.json"

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


# --------------------------------------------------------------------------
# Street geometry
#
# The closure feed gives one point per permit, which on a map is a dot that says
# nothing about which road is shut. Geobase - the city's own road centreline
# network - supplies the lines. A closure names a street and two cross streets
# ("Sainte-Catherine entre Drummond et Crescent"), so the block is recovered by
# locating both intersections and keeping the centreline between them.
# --------------------------------------------------------------------------

# Street "types" and articles carry no identity: the impacts CSV says
# "rue Sainte-Catherine Ouest" where Geobase's NOM_VOIE is "Sainte-Catherine".
# Stripping both sides down to the distinctive part is what makes them join.
_TYPES = (
    r"rue|avenue|av|boulevard|boul|bd|chemin|ch|place|pl|route|voie|ruelle|"
    r"impasse|montee|cote|square|terrasse|croissant|allee|autoroute"
)
_DIRS = r"est|ouest|nord|sud|e|o|n|s"
_ARTICLES = r"de|du|des|la|le|les|l|d"


def street_key(s):
    """Fold a street name to a comparison key (accents, type, direction gone)."""
    s = (s or "").lower().strip()
    s = "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(%s)\b" % _TYPES, " ", s)
    s = re.sub(r"\b(%s)\b" % _DIRS, " ", s)
    s = re.sub(r"\b(%s)\b" % _ARTICLES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Local flat projection. Over a 1 km radius the error is centimetres, and it
# keeps the geometry maths in plain metres.
MPD_LAT = 111132.0
MPD_LON = 111320.0 * math.cos(math.radians(BELL_LAT))


def to_xy(lon, lat):
    return ((lon - BELL_LON) * MPD_LON, (lat - BELL_LAT) * MPD_LAT)


def simplify(points, tol=4.0):
    """Douglas-Peucker, in metres. Downtown blocks are near-straight, so this
    typically drops most vertices without a visible change."""
    if len(points) < 3:
        return points
    xy = [to_xy(p[0], p[1]) for p in points]

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = xy[i]
        bx, by = xy[j]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        best, bi = -1.0, None
        for k in range(i + 1, j):
            px, py = xy[k]
            if L < 1e-9:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs((px - ax) * dy - (py - ay) * dx) / L
            if d > best:
                best, bi = d, k
        if best > tol and bi is not None:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [p for p, k in zip(points, keep) if k]


def round_line(points, nd=5):
    """~1 m precision. Full float coordinates would triple the file for nothing."""
    return [[round(p[0], nd), round(p[1], nd)] for p in points]


def fetch_geobase():
    """Nearby road centrelines, indexed by folded street name.

    Returns (index, segments) or (None, None) - the caller treats a failure as
    "no map this run" rather than a failed build.
    """
    print("Downloading Géobase road network (~43 MB)...")
    last = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                GEOBASE_URL, headers={"User-Agent": "bell-centre-tracker/1.0"}
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            print(f"  geobase: attempt {attempt} failed ({e})", file=sys.stderr)
            if attempt < 3:
                time.sleep(15 * attempt)
    else:
        print(f"WARNING: could not download Géobase ({last}); the dashboard will "
              f"fall back to a list-only view.", file=sys.stderr)
        return None, None

    gj = json.loads(data.decode("utf-8-sig", "replace"))
    feats = gj.get("features") or []
    reach = RADIUS_M + MAP_PAD_M
    # A degree of latitude is ~111 km, so this box comfortably contains the
    # circle and rejects 97% of the city before any haversine runs.
    dlat = reach / MPD_LAT * 1.5
    dlon = reach / MPD_LON * 1.5

    segs = []
    for f in feats:
        g = f.get("geometry") or {}
        if g.get("type") != "LineString":
            continue
        co = g.get("coordinates") or []
        if not co:
            continue
        mid = co[len(co) // 2]
        if abs(mid[1] - BELL_LAT) > dlat or abs(mid[0] - BELL_LON) > dlon:
            continue
        if min(haversine_m(BELL_LAT, BELL_LON, c[1], c[0]) for c in co) > reach:
            continue
        pr = f.get("properties") or {}
        name = clean(pr.get("ODONYME")) or clean(pr.get("NOM_VOIE"))
        segs.append(
            {
                "key": street_key(pr.get("NOM_VOIE")),
                "name": name,
                "coords": co,
            }
        )

    index = collections.defaultdict(list)
    for s in segs:
        index[s["key"]].append(s)
    print(f"  geobase: {len(feats)} segments citywide, {len(segs)} within "
          f"{reach} m ({len(index)} streets)")
    return index, segs


def intersection_xy(index, key_a, key_b, limit=40.0):
    """Approximate point where two streets meet: their closest vertex pair.

    Geobase splits streets at intersections, so the shared corner is a vertex on
    both - no true line-line intersection needed.
    """
    best, point = limit * limit, None
    for a in index.get(key_a, []):
        for b in index.get(key_b, []):
            for ca in a["coords"]:
                pa = to_xy(ca[0], ca[1])
                for cb in b["coords"]:
                    pb = to_xy(cb[0], cb[1])
                    d2 = (pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2
                    if d2 < best:
                        best, point = d2, pa
    return point


def block_geometry(index, street, from_street, to_street, lat, lon):
    """Centrelines for the stretch of `street` between the two cross streets.

    Falls back to the segments nearest the permit's own coordinates when the
    cross streets can't be resolved - which happens when the feed repeats the
    street as its own bound ("Saint-Antoine entre Saint-Antoine et Montagne")
    or names a limit that isn't a street ("136 entre 720 et 720").
    """
    key = street_key(street)
    cand = index.get(key, [])
    if not cand:
        return [], "no-street"

    ka, kb = street_key(from_street), street_key(to_street)
    if ka and kb and ka != kb and ka != key and kb != key:
        p = intersection_xy(index, key, ka)
        q = intersection_xy(index, key, kb)
        if p and q:
            vx, vy = q[0] - p[0], q[1] - p[1]
            L2 = vx * vx + vy * vy
            if L2 > 1.0:
                L = math.sqrt(L2)
                out = []
                for s in cand:
                    m = to_xy(*s["coords"][len(s["coords"]) // 2][:2])
                    # Position along the block (0 = one corner, 1 = the other),
                    # with a little slack for segments that overhang a corner.
                    t = ((m[0] - p[0]) * vx + (m[1] - p[1]) * vy) / L2
                    perp = abs((m[0] - p[0]) * vy - (m[1] - p[1]) * vx) / L
                    if -0.12 <= t <= 1.12 and perp < 45:
                        out.append(s)
                if out:
                    return out, "between"

    near = [
        s
        for s in cand
        if min(haversine_m(lat, lon, c[1], c[0]) for c in s["coords"]) < 180
    ]
    return (near, "near-point") if near else ([], "unlocated")


def segments_for(impact_rows, index, geo_stats, lat, lon):
    segs = []
    for r in impact_rows:
        street = clean(r.get("name")) or clean(r.get("streetid"))
        if not street:
            continue
        seg = {
            "street": street,
            "from": clean(r.get("fromshortname")) or None,
            "to": clean(r.get("toshortname")) or None,
            "impact": clean(r.get("streetimpacttype")) or None,
            "sidewalk": clean(r.get("sidewalk_blockedtype")) or None,
            "bike_path": clean(r.get("bikepath_blockedtype")) or None,
        }
        if index is not None:
            lines, how = block_geometry(index, street, seg["from"], seg["to"], lat, lon)
            geo_stats[how] += 1
            if lines:
                seg["lines"] = [
                    round_line(simplify(s["coords"])) for s in lines
                ]
                seg["located"] = how
        segs.append(seg)
    return segs


def build(entraves, impacts, today, index=None):
    by_request = {}
    for r in impacts:
        by_request.setdefault(clean(r.get("id_request")), []).append(r)

    horizon = today + timedelta(days=HORIZON_DAYS)
    stats = {"shifted": 0, "direct": 0}
    geo_stats = collections.Counter()
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

        segs = segments_for(
            by_request.get(clean(r.get("id")), []), index, geo_stats, lat, lon
        )
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

    if index is not None:
        drawn = sum(
            1 for c in out if any("lines" in s for s in c["segments"])
        )
        print(
            f"  geometry: {drawn}/{len(out)} closures mapped "
            f"({dict(geo_stats)})."
        )

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


# Matches the "generated" field whether the file is indented or compact, so
# both closures.json and the single-line streets.json compare correctly.
GENERATED = re.compile(r'"generated"\s*:\s*"[^"]*"')


def write_if_changed(path, new_text):
    """Skip the write when only the per-run "generated" timestamp differs.

    Mirrors fetch_events.py: without this the daily run would dirty the files
    every day and commit a timestamp change with no real closure change. The
    basemap in particular is ~200 KB and changes only when the city edits the
    road network.
    """
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
        if GENERATED.sub("", old) == GENERATED.sub("", new_text):
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

    # The map is an enhancement, not a requirement: if Géobase is unreachable or
    # malformed, the closure list still gets built and the page drops to a
    # list-only view rather than the whole step failing.
    try:
        index, segments = fetch_geobase()
    except Exception as e:  # noqa: BLE001 - any geobase problem is non-fatal
        print(f"WARNING: Géobase processing failed ({e}); continuing without a map.",
              file=sys.stderr)
        index, segments = None, None

    today = (datetime.now(timezone.utc) - timedelta(hours=montreal_offset_hours(
        datetime.now(timezone.utc)))).date()
    payload = build(entraves, impacts, today, index)

    os.makedirs(OUT_DIR, exist_ok=True)
    written = []
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if write_if_changed(os.path.join(OUT_DIR, OUT_FILE), text):
        written.append(OUT_FILE)

    if segments:
        basemap = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "attribution": "Données: Ville de Montréal",
            "source": "Ville de Montréal — Géobase (réseau routier)",
            "source_url": "https://donnees.montreal.ca/dataset/geobase",
            "license": "CC-BY 4.0",
            "center": {"lat": BELL_LAT, "lon": BELL_LON},
            "radius_m": RADIUS_M,
            "streets": [
                {"name": s["name"], "line": round_line(simplify(s["coords"]))}
                for s in segments
            ],
        }
        btext = json.dumps(basemap, ensure_ascii=False, separators=(",", ":")) + "\n"
        if write_if_changed(os.path.join(OUT_DIR, STREETS_FILE), btext):
            written.append(STREETS_FILE)
        print(f"  basemap: {len(basemap['streets'])} street segments, "
              f"{len(btext) // 1024} KB")

    if written:
        print(f"Wrote {', '.join(OUT_DIR + '/' + n for n in written)}")
    else:
        print("No closure changes; output files left untouched.")


if __name__ == "__main__":
    main()
