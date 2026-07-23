# Bell Centre Tracker

Daily-updated, subscribable calendar of Bell Centre events, powered by the
Ticketmaster Discovery API and GitHub Actions. No server required.

## Setup (once, ~5 minutes)

1. **Create a new GitHub repo** (e.g. `bell-centre-tracker`) and push these files.
   Public repo = free unlimited Actions minutes and a public Pages URL.

2. **Add your API key as a secret** — do NOT commit it:
   Repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `TM_API_KEY`
   - Value: your Ticketmaster consumer key

3. **Enable GitHub Pages**:
   Repo → Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, folder `/docs`.

4. **First run**: Repo → Actions → "Update Bell Centre calendar" → Run workflow.
   Check the log — it should print the resolved venue name and event count.

5. **Subscribe in Google Calendar**:
   Google Calendar → Settings → Add calendar → From URL →
   `https://gleepo.github.io/bell-centre-tracker/bell-centre.ics`
   (Or add it to phone calendar apps the same way.)

## Dashboard

`https://gleepo.github.io/bell-centre-tracker/` — a single static page
(`docs/index.html`, no build step, no frameworks) showing the next event with a
live countdown, the next 14 days of events, and road closures near the venue.
It fetches `events.json` and `closures.json` over relative paths, so it works
from any subpath, and it renders the events half normally when the closures
feed is missing or empty.

## Road closures

`fetch_closures.py` writes `docs/closures.json` from the City of Montreal's
"Entraves et travaux en cours" open data (CC-BY 4.0). It keeps permits that are
active now or start within 14 days **and** lie within 600 m of the Bell Centre
(45.4960, -73.5693).

- Two CSVs make up the dataset. The main one carries the permit and, usefully, a
  `longitude`/`latitude` pair — so closures are located by haversine distance,
  not by street-name matching. The companion "impacts" CSV carries the affected
  street segments and joins on `id_request` → `id`.
- **The feed's timestamps apply the UTC offset with the wrong sign.** Every
  start is stamped `20:00:00Z` in EDT months and `19:00:00Z` in EST months (ends
  `19:59:59Z` / `18:59:59Z`), so *adding* the offset lands on local midnight and
  gives whole-day permits; subtracting would report every closure a day early.
  See `city_local_date()`. Timestamps outside that fingerprint are converted
  normally and counted separately in the log, so a fix upstream won't break it.
- Roughly 9% of permits have no rows in the impacts CSV; those show their
  `occupancy_name` zone instead of street names.
- `currentstatus` is `Permis émis` for every row today, so it isn't a useful
  filter — it's carried into the JSON anyway in case that changes.
- The portal returns `503 no healthy upstream` during its daily reload, so
  downloads retry with backoff.

## Notes

- The two fetchers are separate workflow steps: a closures failure can't break
  the calendar build, the commit step commits whatever succeeded, and a final
  step still fails the job so a broken fetcher shows up red.
- Google refreshes subscribed calendars on its own schedule (typically every
  8–24 h; you can't force it). The feed itself updates daily at 6 AM.
- Events with no announced start time appear as all-day entries marked "(time TBA)".
- Event end times aren't provided by Ticketmaster; entries use a 3-hour estimate.
- `docs/events.json` is there for a future dashboard page (events + road
  closures in one view).
- Ticketmaster lists hospitality add-ons (dinner packages, lounge access, venue
  tours, meet-and-greets) as separate events; these are filtered out by the
  name patterns in `PACKAGE_PATTERNS`. The filter is a denylist, so a segment
  that hasn't appeared yet — comedy, Cirque, a new sport — still comes through.
  Any `Miscellaneous` event that matches no known package pattern is kept and
  listed in the Actions log; if a real event shows up there, nothing was lost,
  but if a new package type does, add its pattern.
- The output files are only rewritten when an event actually changes, so the
  daily run commits nothing on a quiet day.
- If the venue lookup ever fails, the Actions log lists the candidate venues it
  found — put the right ID in `venue_id.txt` manually and it'll use that.
