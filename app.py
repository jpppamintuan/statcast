"""
MLB Statcast Total Run Value Leaderboard
-----------------------------------------
Live-fetches Batting, Pitching, Fielding, and Baserunning Run Value from
Baseball Savant on every page load, joins them by player (MLBAM ID), and
ranks players by the sum of all four.

Deploy target: Streamlit Community Cloud (free), source repo on GitHub.
No local machine or scheduled job required -- every visit re-pulls fresh data.
"""

import requests
import pandas as pd
import streamlit as st
from io import StringIO

st.set_page_config(page_title="Statcast Total Run Value Leaderboard", layout="wide")

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
# Baseball Savant leaderboard pages generally export a CSV by appending
# "csv=true" to the page's query string. Savant occasionally tweaks these
# URLs/params -- if a fetch below returns 0 rows or unexpected columns, open
# the leaderboard page in a browser, use its CSV/export link, and copy the
# resulting URL here.
SOURCES = {
    "batting": "https://baseballsavant.mlb.com/leaderboard/statcast?type=batter&year={year}&position=&team=&min=1&csv=true",
    "pitching": "https://baseballsavant.mlb.com/leaderboard/statcast?type=pitcher&year={year}&position=&team=&min=1&csv=true",
    "fielding": "https://baseballsavant.mlb.com/leaderboard/fielding-run-value?year={year}&csv=true",
    "baserunning": "https://baseballsavant.mlb.com/leaderboard/baserunning-run-value?season_start={year}&season_end={year}&csv=true",
}

# Candidate column names Savant has used for the run-value figure and player
# identity on each leaderboard. The first match found in the fetched CSV wins.
RUN_VALUE_CANDIDATES = {
    "batting": ["run_value_batting", "runs_batting", "run_value", "batting_run_value"],
    "pitching": ["run_value_pitching", "runs_pitching", "run_value", "pitching_run_value"],
    "fielding": ["run_value", "fielding_run_value", "frv", "runs"],
    "baserunning": ["run_value", "baserunning_run_value", "runner_runs_tot", "runs"],
}
PLAYER_ID_CANDIDATES = ["player_id", "batter", "pitcher", "fielder_id", "runner_id", "mlbam_id"]
PLAYER_NAME_CANDIDATES = ["player_name", "last_name, first_name", "name", "full_name"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StatcastLeaderboardApp/1.0)"}


def pick_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_csv(url: str) -> pd.DataFrame:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return pd.read_csv(StringIO(resp.text))


def normalize(df: pd.DataFrame, category: str) -> tuple[pd.DataFrame, dict]:
    """Reduce a raw leaderboard CSV down to [player_id, player_name, <category>_rv]."""
    debug = {"columns_found": list(df.columns), "rows": len(df)}

    id_col = pick_col(df, PLAYER_ID_CANDIDATES)
    name_col = pick_col(df, PLAYER_NAME_CANDIDATES)
    rv_col = pick_col(df, RUN_VALUE_CANDIDATES[category])

    debug.update({"id_col_used": id_col, "name_col_used": name_col, "run_value_col_used": rv_col})

    if id_col is None or rv_col is None:
        return pd.DataFrame(columns=["player_id", "player_name", f"{category}_rv"]), debug

    out = pd.DataFrame({
        "player_id": df[id_col],
        "player_name": df[name_col] if name_col else "",
        f"{category}_rv": pd.to_numeric(df[rv_col], errors="coerce"),
    })
    return out.dropna(subset=["player_id"]), debug


def build_leaderboard(year: int):
    frames = {}
    debug_info = {}
    for category, url_template in SOURCES.items():
        url = url_template.format(year=year)
        try:
            raw = fetch_csv(url)
            norm, debug = normalize(raw, category)
            frames[category] = norm
            debug_info[category] = {"status": "ok", "url": url, **debug}
        except Exception as e:
            frames[category] = pd.DataFrame(columns=["player_id", "player_name", f"{category}_rv"])
            debug_info[category] = {"status": f"error: {e}", "url": url}

    combined = None
    for category, df in frames.items():
        if combined is None:
            combined = df
        else:
            combined = combined.merge(df, on="player_id", how="outer", suffixes=("", f"_{category}"))

    # Consolidate player_name across sources (first non-null wins)
    name_cols = [c for c in combined.columns if c.startswith("player_name")]
    combined["player_name"] = combined[name_cols].bfill(axis=1).iloc[:, 0]
    combined = combined.drop(columns=[c for c in name_cols if c != "player_name"])

    rv_cols = [f"{c}_rv" for c in SOURCES.keys()]
    for c in rv_cols:
        if c not in combined.columns:
            combined[c] = 0.0
    combined[rv_cols] = combined[rv_cols].fillna(0.0)
    combined["total_run_value"] = combined[rv_cols].sum(axis=1)

    combined = combined.sort_values("total_run_value", ascending=False).reset_index(drop=True)
    combined.index += 1
    return combined, debug_info


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.title("⚾ MLB Statcast Total Run Value Leaderboard")
st.caption(
    "Live from Baseball Savant. Total Run Value = Batting + Pitching + Fielding + Baserunning "
    "run value, summed per player."
)

col1, col2 = st.columns([1, 3])
with col1:
    year = st.number_input("Season", min_value=2016, max_value=2026, value=2026, step=1)
    if st.button("🔄 Force refresh now"):
        st.cache_data.clear()

with st.spinner("Pulling live data from Baseball Savant..."):
    leaderboard, debug_info = build_leaderboard(int(year))

display_cols = ["player_name", "batting_rv", "pitching_rv", "fielding_rv", "baserunning_rv", "total_run_value"]
display_cols = [c for c in display_cols if c in leaderboard.columns]
pretty_names = {
    "player_name": "Player",
    "batting_rv": "Batting RV",
    "pitching_rv": "Pitching RV",
    "fielding_rv": "Fielding RV",
    "baserunning_rv": "Baserunning RV",
    "total_run_value": "Total Run Value",
}

st.dataframe(
    leaderboard[display_cols].rename(columns=pretty_names),
    use_container_width=True,
    height=700,
)

with st.expander("🔧 Debug: raw source status & column mapping (check this if numbers look off)"):
    for category, info in debug_info.items():
        st.markdown(f"**{category}**")
        st.json(info)
