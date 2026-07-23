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

## Notes

- Google refreshes subscribed calendars on its own schedule (typically every
  8–24 h; you can't force it). The feed itself updates daily at 6 AM.
- Events with no announced start time appear as all-day entries marked "(time TBA)".
- Event end times aren't provided by Ticketmaster; entries use a 3-hour estimate.
- `docs/events.json` is there for a future dashboard page (events + road
  closures in one view).
- If the venue lookup ever fails, the Actions log lists the candidate venues it
  found — put the right ID in `venue_id.txt` manually and it'll use that.
