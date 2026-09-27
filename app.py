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
import plotly.graph_objects as go
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
# ImageColumn only accepts "small" / "medium" / "large" for column *width* --
# there's no pixel-level width knob. The logo's actual rendered size is driven
# by ROW_HEIGHT below (the image scales to fill the row vertically, so a
# taller row = a bigger logo). Requires Streamlit >= 1.43.
LOGO_COLUMN_WIDTH = "small"
ROW_HEIGHT = 46  # default is 35px; raise this to make the logos bigger

# Zebra-striping base color for even-numbered rows (odd rows stay transparent).
ZEBRA_COLOR = "rgba(120,120,120,0.08)"

# --- Starting-pitcher chart config ---------------------------------------
# Games Started (GS) isn't on the pitching run-value leaderboard, so it's
# pulled separately from this custom leaderboard.
STARTS_URL = (
    "https://baseballsavant.mlb.com/leaderboard/custom?year={year}&type=pitcher"
    "&filter=&min=q&selections=p_starting_p&chart=false&x=&y=&r=no"
    "&chartType=beeswarm&sort=1&sortDir=asc&csv=true"
)
GS_CANDIDATES = ["p_starting_p", "gs", "games_started", "starting_p"]

# Standard MLBAM team IDs (same IDs used for the team-logo URLs) mapped to
# their abbreviation, for labeling the chart's per-team rows.
TEAM_ID_TO_ABBR = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KC", 119: "LAD",
    120: "WSH", 121: "NYM", 133: "OAK", 134: "PIT", 135: "SD", 136: "SEA",
    137: "SF", 138: "STL", 139: "TB", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StatcastLeaderboardApp/1.0)"}

# Columns that get the Statcast-style diverging color scale + integer rounding.
RV_KEYS = ["batting_rv", "pitching_rv", "fielding_rv", "baserunning_rv", "total_run_value"]


def pick_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


# ttl=None -> cache never expires on its own. The only way data gets re-fetched
# is (a) a brand new argument value (e.g. a season not seen yet) or (b) an
# explicit st.cache_data.clear() call, which the "Force refresh" button below
# triggers. This is what gives us "live on first load, static until the user
# asks for a refresh" instead of a background/timed refresh.
@st.cache_data(ttl=None, show_spinner=False)
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


def normalize_starts(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Reduce the custom GS leaderboard CSV down to [player_id, gs]."""
    debug = {"columns_found": list(df.columns), "rows": len(df)}
    id_col = pick_col(df, PLAYER_ID_CANDIDATES)
    gs_col = pick_col(df, GS_CANDIDATES)
    debug.update({"id_col_used": id_col, "gs_col_used": gs_col})

    if id_col is None or gs_col is None:
        return pd.DataFrame(columns=["player_id", "gs"]), debug

    out = pd.DataFrame({
        "player_id": df[id_col],
        "gs": pd.to_numeric(df[gs_col], errors="coerce"),
    })
    return out.dropna(subset=["player_id"]), debug


def get_last_name(full_name: str) -> str:
    """Savant player names usually come as 'Last, First'; fall back to the
    last whitespace-separated token for any other format."""
    if not isinstance(full_name, str) or not full_name.strip():
        return ""
    if "," in full_name:
        return full_name.split(",")[0].strip()
    parts = full_name.strip().split()
    return parts[-1] if parts else full_name


# Cached for the same reason as fetch_csv: the merge/aggregation work only
# reruns when the underlying CSVs actually change (i.e. after a cache clear),
# not on every Streamlit rerun (sorting, resizing, other widget interactions).
@st.cache_data(ttl=None, show_spinner=False)
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

    # Games Started -- separate source, only used by the starting-pitcher chart.
    try:
        starts_raw = fetch_csv(STARTS_URL.format(year=year))
        starts_norm, starts_debug = normalize_starts(starts_raw)
        debug_info["starts"] = {"status": "ok", "url": STARTS_URL.format(year=year), **starts_debug}
    except Exception as e:
        starts_norm = pd.DataFrame(columns=["player_id", "gs"])
        debug_info["starts"] = {"status": f"error: {e}", "url": STARTS_URL.format(year=year)}

    combined = combined.merge(starts_norm, on="player_id", how="left")
    combined["gs"] = combined["gs"].fillna(0)

    combined = combined.sort_values("total_run_value", ascending=False).reset_index(drop=True)
    combined.index += 1
    return combined, debug_info


def diverging_column_style(col: pd.Series) -> list[str]:
    """Statcast-portal style coloring, scaled per column (i.e. per run-value
    category) across all players: red = that column's highest value, blue =
    that column's lowest, no fill at 0, intermediate values shaded proportionally."""
    vals = col.astype(float)
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


def zebra_column_style(col: pd.Series) -> list[str]:
    """Zebra-stripe a non-run-value column by row position."""
    return [f"background-color: {ZEBRA_COLOR if i % 2 == 0 else 'transparent'}" for i in col.index]


def zebra_index_style(index_values) -> list[str]:
    """Stripe the rank/index column to match its row."""
    return [f"background-color: {ZEBRA_COLOR if i % 2 == 0 else 'transparent'}" for i in index_values]


def build_starting_pitcher_chart(leaderboard: pd.DataFrame) -> go.Figure | None:
    """One horizontal number line per team: x = pitching run value, dot size =
    Games Started, label above each dot = the pitcher's last name. Every
    team's line spans the same range (league-wide min/max pitching RV among
    starters), so the lines are directly comparable."""
    starters = leaderboard[(leaderboard["gs"] > 0) & leaderboard["team_id"].notna()].copy()
    if starters.empty:
        return None

    starters["last_name"] = starters["player_name"].apply(get_last_name)
    starters["team_abbr"] = starters["team_id"].apply(
        lambda t: TEAM_ID_TO_ABBR.get(int(t), str(int(t)))
    )

    league_min = starters["pitching_rv"].min()
    league_max = starters["pitching_rv"].max()
    pad = max((league_max - league_min) * 0.08, 1)
    x_range = [league_min - pad, league_max + pad]

    max_gs = max(starters["gs"].max(), 1)
    min_size, max_size = 10, 32

    team_order = sorted(starters["team_abbr"].unique())

    fig = go.Figure()
    for team in team_order:
        team_df = starters[starters["team_abbr"] == team].sort_values("pitching_rv")

        # The number line itself.
        fig.add_trace(go.Scatter(
            x=x_range, y=[team, team],
            mode="lines",
            line=dict(color="rgba(150,150,150,0.4)", width=2),
            hoverinfo="skip",
            showlegend=False,
        ))

        # The starters on that line.
        sizes = min_size + (team_df["gs"] / max_gs) * (max_size - min_size)
        fig.add_trace(go.Scatter(
            x=team_df["pitching_rv"], y=[team] * len(team_df),
            mode="markers+text",
            text=team_df["last_name"],
            textposition="top center",
            marker=dict(size=sizes, color="#1f77b4", line=dict(color="white", width=1)),
            customdata=team_df["gs"],
            hovertemplate="%{text}<br>Pitching RV: %{x:.0f}<br>GS: %{customdata:.0f}<extra></extra>",
            showlegend=False,
        ))

    fig.update_layout(
        height=max(320, 34 * len(team_order) + 100),
        margin=dict(l=60, r=30, t=30, b=40),
        xaxis=dict(title="Pitching Run Value", range=x_range, zeroline=True),
        yaxis=dict(title="", categoryorder="array", categoryarray=team_order[::-1]),
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


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

display_cols = ["player_name", "team_logo", "batting_rv", "pitching_rv", "fielding_rv", "baserunning_rv", "total_run_value"]
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

non_rv_cols = [c for c in display_df.columns if c not in rv_display_cols]

styled = (
    display_df.style
    .apply(zebra_column_style, axis=0, subset=non_rv_cols)
    .apply(diverging_column_style, axis=0, subset=rv_display_cols)
    .apply_index(zebra_index_style, axis=0)
    .format({c: "{:.0f}" for c in rv_display_cols})
)

# alignment="center"/"left" requires Streamlit >= 1.56. "Player" and the
# rank/index column are left out on purpose to keep their current alignment.
numeric_column_config = {
    name: st.column_config.NumberColumn(name, alignment="center")
    for name in rv_display_cols
}

st.dataframe(
    styled,
    use_container_width=True,
    height=700,
    row_height=ROW_HEIGHT,
    column_config={
        "Team": st.column_config.ImageColumn("Team", width=LOGO_COLUMN_WIDTH, alignment="center"),
        **numeric_column_config,
    },
)

with st.expander("🔧 Debug: raw source status & column mapping (check this if numbers look off)"):
    for category, info in debug_info.items():
        st.markdown(f"**{category}**")
        st.json(info)

st.divider()
st.header("Starting Pitchers — Pitching Run Value by Team")
st.caption(
    "Each row is one team's starting rotation. Dot position = pitching run value "
    "(lower to the left, higher to the right); dot size = Games Started; every "
    "team's line spans the same league-wide min-to-max range."
)
pitcher_chart = build_starting_pitcher_chart(leaderboard)
if pitcher_chart is not None:
    st.plotly_chart(pitcher_chart, use_container_width=True)
else:
    st.info("No starting-pitcher data available for this season yet.")
