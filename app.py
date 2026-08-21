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
SOURCES = {
    "batting": "https://baseballsavant.mlb.com/leaderboard/swing-take?year={year}&team=&leverage=Neutral&group=Batter&type=All&sub_type=null&min=1&csv=true",
    "pitching": "https://baseballsavant.mlb.com/leaderboard/swing-take?year={year}&team=&leverage=Neutral&group=Pitcher&type=All&sub_type=null&min=1&csv=true",
    "fielding": "https://baseballsavant.mlb.com/leaderboard/fielding-run-value?gameType=Regular&seasonStart={year}&seasonEnd={year}&type=fielder&position=0&minInnings=q&minResults=1&csv=true",
    "baserunning": "https://baseballsavant.mlb.com/leaderboard/baserunning-run-value?season_start={year}&season_end={year}&csv=true",
}

# Candidate column names Savant has used for the run-value figure, player
# identity, and team on each leaderboard. The first match found wins.
RUN_VALUE_CANDIDATES = {
    "batting": ["run_value_batting", "runs_batting", "run_value", "batting_run_value", "runs_all"],
    "pitching": ["run_value_pitching", "runs_pitching", "run_value", "pitching_run_value", "runs_all"],
    "fielding": ["run_value", "fielding_run_value", "frv", "runs", "total_runs"],
    "baserunning": ["run_value", "baserunning_run_value", "runner_runs_tot", "runs"],
}
PLAYER_ID_CANDIDATES = ["player_id", "batter", "pitcher", "fielder_id", "runner_id", "mlbam_id", "id"]
PLAYER_NAME_CANDIDATES = ["player_name", "last_name, first_name", "name", "full_name", "entity_name"]
# team_id is expected on the batting/pitching (swing-take) sources.
TEAM_ID_CANDIDATES = ["team_id", "team", "teamId"]

TEAM_LOGO_URL = "https://www.mlbstatic.com/team-logos/{team_id}.svg"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StatcastLeaderboardApp/1.0)"}

# Columns that get the Statcast-style diverging color scale + integer rounding.
RV_KEYS = ["batting_rv", "pitching_rv", "fielding_rv", "baserunning_rv", "total_run_value"]


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
    """Reduce a raw leaderboard CSV down to [player_id, player_name, team_id, <category>_rv]."""
    debug = {"columns_found": list(df.columns), "rows": len(df)}

    id_col = pick_col(df, PLAYER_ID_CANDIDATES)
    name_col = pick_col(df, PLAYER_NAME_CANDIDATES)
    rv_col = pick_col(df, RUN_VALUE_CANDIDATES[category])
    team_col = pick_col(df, TEAM_ID_CANDIDATES)

    debug.update({
        "id_col_used": id_col,
        "name_col_used": name_col,
        "run_value_col_used": rv_col,
        "team_id_col_used": team_col,
    })

    if id_col is None or rv_col is None:
        empty = pd.DataFrame(columns=["player_id", "player_name", "team_id", f"{category}_rv"])
        return empty, debug

    out = pd.DataFrame({
        "player_id": df[id_col],
        "player_name": df[name_col] if name_col else "",
        "team_id": pd.to_numeric(df[team_col], errors="coerce") if team_col else pd.NA,
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
            frames[category] = pd.DataFrame(columns=["player_id", "player_name", "team_id", f"{category}_rv"])
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

    # Consolidate team_id across sources (batting/pitching are the ones that carry it)
    team_cols = [c for c in combined.columns if c.startswith("team_id")]
    combined["team_id"] = combined[team_cols].bfill(axis=1).iloc[:, 0]
    combined = combined.drop(columns=[c for c in team_cols if c != "team_id"])

    rv_cols = [f"{c}_rv" for c in SOURCES.keys()]
    for c in rv_cols:
        if c not in combined.columns:
            combined[c] = 0.0
    combined[rv_cols] = combined[rv_cols].fillna(0.0)
    combined["total_run_value"] = combined[rv_cols].sum(axis=1)

    combined["team_logo"] = combined["team_id"].apply(
        lambda t: TEAM_LOGO_URL.format(team_id=int(t)) if pd.notna(t) else None
    )

    combined = combined.sort_values("total_run_value", ascending=False).reset_index(drop=True)
    combined.index += 1
    return combined, debug_info


def statcast_diverging_style(row: pd.Series) -> list[str]:
    """Statcast-portal style coloring: red on the row's highest value, blue on the
    row's lowest, no fill at 0, with intermediate values shaded proportionally."""
    vals = row.astype(float)
    max_abs = vals.abs().max()
    if pd.isna(max_abs) or max_abs == 0:
        max_abs = 1
    styles = []
    for v in vals:
        if pd.isna(v) or v == 0:
            styles.append("background-color: transparent")
        elif v > 0:
            intensity = min(abs(v) / max_abs, 1)
            alpha = 0.15 + 0.65 * intensity
            styles.append(f"background-color: rgba(214,39,40,{alpha:.2f})")  # red
        else:
            intensity = min(abs(v) / max_abs, 1)
            alpha = 0.15 + 0.65 * intensity
            styles.append(f"background-color: rgba(31,119,180,{alpha:.2f})")  # blue
    return styles


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

display_cols = ["team_logo", "player_name", "batting_rv", "pitching_rv", "fielding_rv", "baserunning_rv", "total_run_value"]
display_cols = [c for c in display_cols if c in leaderboard.columns]
pretty_names = {
    "team_logo": "Team",
    "player_name": "Player",
    "batting_rv": "Batting RV",
    "pitching_rv": "Pitching RV",
    "fielding_rv": "Fielding RV",
    "baserunning_rv": "Baserunning RV",
    "total_run_value": "Total Run Value",
}

display_df = leaderboard[display_cols].rename(columns=pretty_names)
rv_display_cols = [pretty_names[c] for c in RV_KEYS if pretty_names[c] in display_df.columns]

styled = (
    display_df.style
    .apply(statcast_diverging_style, axis=1, subset=rv_display_cols)
    .format({c: "{:.0f}" for c in rv_display_cols})
)

st.dataframe(
    styled,
    use_container_width=True,
    height=700,
    column_config={
        "Team": st.column_config.ImageColumn("Team", width="small"),
    },
)

with st.expander("🔧 Debug: raw source status & column mapping (check this if numbers look off)"):
    for category, info in debug_info.items():
        st.markdown(f"**{category}**")
        st.json(info)
