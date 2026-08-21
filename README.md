# MLB Statcast Total Run Value Leaderboard

A Streamlit app that live-fetches Batting, Pitching, Fielding, and Baserunning
Run Value from Baseball Savant on every page load, and ranks players by the
sum of all four ("Total Run Value"). Runs entirely in the cloud — no local
machine, no scheduled job. Every visit pulls fresh data.

## How it works

- `app.py` fetches four CSV exports from Baseball Savant's leaderboards
  (batting run value, pitching run value, fielding run value, baserunning
  run value), normalizes each to `[player_id, player_name, run_value]`, and
  outer-joins them on `player_id` (MLBAM ID).
- Results are cached for 5 minutes per season (`st.cache_data(ttl=300)`) so a
  burst of page refreshes doesn't hammer Savant — click **"Force refresh
  now"** in the app to bypass the cache immediately.
- A **Debug** panel at the bottom of the app shows exactly which columns were
  found and used from each source. Check this first if a number looks wrong.

## Deploy (no local machine required)

1. **Create a GitHub repo** and add these three files (`app.py`,
   `requirements.txt`, `README.md`) directly through GitHub's web UI
   ("Add file" → "Upload files" or "Create new file") — no git client needed.
2. Go to **[share.streamlit.io](https://share.streamlit.io)**, sign in with
   GitHub, click **"New app"**, and point it at the repo, branch (`main`),
   and file path (`app.py`).
3. Click **Deploy**. Streamlit Cloud builds and hosts the app for free; you
   get a public URL like `https://your-app-name.streamlit.app`.
4. Every time someone opens that URL, `app.py` re-runs and pulls live data.
   No cron jobs, no infrastructure to maintain.

## If a leaderboard returns 0 rows or a column looks wrong

Baseball Savant occasionally changes its leaderboard URLs or column names.
To fix:

1. Open the relevant leaderboard on baseballsavant.mlb.com in a browser
   (e.g. the Fielding Run Value leaderboard).
2. Set your filters (season, etc.), then look for the page's CSV/export
   link — copy that exact URL.
3. Paste it into the `SOURCES` dict at the top of `app.py`, keeping the
   `{year}` placeholder if the URL includes a season parameter.
4. Check the **Debug** panel in the running app — it lists every column
   name it found in the fetched CSV. Add any new column name to the
   matching list in `RUN_VALUE_CANDIDATES`, `PLAYER_ID_CANDIDATES`, or
   `PLAYER_NAME_CANDIDATES` at the top of `app.py`.
5. Commit the change on GitHub — Streamlit Cloud auto-redeploys.

## Notes on the numbers

- Batting/Pitching Run Value: Savant offers both **context-neutral** and
  **leverage-based** versions; `app.py` pulls whichever is the default
  export from the leaderboard URL configured (context-neutral, as of this
  writing). Adjust the URL params if you want leverage-based instead.
- Fielding Run Value combines range (Outs Above Average), throwing,
  catcher blocking, catcher framing, and catcher throwing onto one run
  scale.
- Baserunning Run Value combines basestealing and extra-bases-taken value.
- A player who doesn't appear on one of the four leaderboards (e.g. a
  pure reliever has no baserunning value) is treated as `0` for that
  category, not excluded from the total.
