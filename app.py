"""
MLB Statcast Total Run Value Leaderboard
-----------------------------------------
Live-fetches Batting, Pitching, Fielding, and Baserunning Run Value from
Baseball Savant on every page load, joins them by player (MLBAM ID), and
ranks players by the sum of all four.

Deploy target: Streamlit Community Cloud (free), source repo on GitHub.
No local machine or scheduled job required -- every visit re-pulls fresh data.
"""

import math
import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import plotly.graph_objects as go
from io import StringIO

st.set_page_config(page_title="Statcast Leaderboard", layout="wide")

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
SOURCES = {
    "batting": "https://baseballsavant.mlb.com/leaderboard/swing-take?year={year}&team=&leverage=Neutral&group=Batter&type=All&sub_type=null&min=1&csv=true",
    "pitching": "https://baseballsavant.mlb.com/leaderboard/swing-take?year={year}&team=&group=Pitcher&type=All&sub_type=null&min=q&csv=true",
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

# The pitcher chart renders at this fixed pixel width regardless of viewport.
# On a wide desktop screen this is roughly full-bleed; on a phone-width
# screen it's wider than the viewport, so the chart scrolls horizontally
# inside its own container instead of squishing labels and points together.
CHART_FIXED_WIDTH = 1300

# --- Pitchers feature config ---------------------------------------------
# Games Started (GS) and Innings Pitched (IP) aren't on the pitching run-value
# leaderboard, so they're pulled separately from this custom leaderboard.
STARTS_URL = (
    "https://baseballsavant.mlb.com/leaderboard/custom?year={year}&type=pitcher"
    "&filter=&min=1&selections=p_formatted_ip%2Cp_starting_p&chart=false"
    "&x=p_starting_p&y=p_starting_p&r=no&chartType=beeswarm&sort=p_starting_p"
    "&sortDir=desc&csv=true"
)
GS_CANDIDATES = ["p_starting_p", "gs", "games_started", "starting_p"]
# Savant's "formatted IP" uses baseball's innings notation (X.0/X.1/X.2 for
# outs, not literal tenths -- e.g. 45.2 means 45 and 2/3 innings). That still
# sorts and thresholds correctly as a plain float compare (any X.0/X.1/X.2
# always falls between X and X+1), so no unit conversion is needed for a
# ">=" filter like the reliever IP minimum below.
IP_CANDIDATES = ["p_formatted_ip", "ip", "innings_pitched"]

# A pitcher needs at least this fraction of the league's highest GS to count
# as a starter (e.g. highest GS = 33 -> ceil(0.10 * 33) = 4 minimum). Below
# that, they're a reliever candidate -- and also need at least this many
# innings pitched to show up on the Relievers chart.
MIN_GS_PCT_OF_MAX = 0.10
MIN_RELIEVER_IP = 16.2

# Starters use context-neutral pitching run value; relievers use
# leverage-based (weighted by game situation), since a reliever's value is
# concentrated in high-leverage spots that context-neutral RV would flatten out.
PITCHERS_NEUTRAL_RV_URL = "https://baseballsavant.mlb.com/leaderboard/swing-take?year={year}&team=&leverage=Neutral&group=Pitcher&type=All&sub_type=null&min=10&csv=true"
PITCHERS_LEVERAGED_RV_URL = "https://baseballsavant.mlb.com/leaderboard/swing-take?year={year}&team=&leverage=Leveraged&group=Pitcher&type=All&sub_type=null&min=10&csv=true"

# Standard MLBAM team IDs (same IDs used for the team-logo URLs) mapped to
# their abbreviation, for labeling the chart's per-team rows.
TEAM_ID_TO_ABBR = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KC", 119: "LAD",
    120: "WSH", 121: "NYM", 133: "OAK", 134: "PIT", 135: "SD", 136: "SEA",
    137: "SF", 138: "STL", 139: "TB", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}

# Best-effort official primary brand colors per team. These are hardcoded
# rather than extracted from the logo SVGs at runtime -- pulling a "dominant
# color" out of an image is unreliable (logos mix several colors, plus
# transparency) and would need extra heavy dependencies (image rasterizing +
# color-quantization libraries). A static, curated map is simpler and matches
# each team's actual brand color more faithfully. Double-check any that look
# off to you -- these are compiled from public team style guides, not verified
# pixel-for-pixel against the current logo files.
TEAM_ID_TO_COLOR = {
    108: "#BA0021", 109: "#A71930", 110: "#DF4601", 111: "#BD3039", 112: "#0E3386",
    113: "#C6011F", 114: "#0C2340", 115: "#333366", 116: "#0C2340", 117: "#002D62",
    118: "#004687", 119: "#005A9C", 120: "#AB0003", 121: "#002D72", 133: "#003831",
    134: "#FDB827", 135: "#2F241D", 136: "#0C2C56", 137: "#FD5A1E", 138: "#C41E3A",
    139: "#092C5C", 140: "#003278", 141: "#134A8E", 142: "#002B5C", 143: "#E81828",
    144: "#CE1141", 145: "#27251F", 146: "#00A3E0", 147: "#132448", 158: "#12284B",
}

# Division groupings for the small-multiples layout. Order within each
# division is alphabetized in code (not by this list order).
DIVISION_ORDER = ["AL East", "AL Central", "AL West", "NL East", "NL Central", "NL West"]
DIVISIONS = {
    "AL East": ["BAL", "BOS", "NYY", "TB", "TOR"],
    "AL Central": ["CWS", "CLE", "DET", "KC", "MIN"],
    "AL West": ["HOU", "LAA", "OAK", "SEA", "TEX"],
    "NL East": ["ATL", "MIA", "NYM", "PHI", "WSH"],
    "NL Central": ["CHC", "CIN", "MIL", "PIT", "STL"],
    "NL West": ["ARI", "COL", "LAD", "SD", "SF"],
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
    """Reduce the custom GS/IP leaderboard CSV down to [player_id, gs, ip]."""
    debug = {"columns_found": list(df.columns), "rows": len(df)}
    id_col = pick_col(df, PLAYER_ID_CANDIDATES)
    gs_col = pick_col(df, GS_CANDIDATES)
    ip_col = pick_col(df, IP_CANDIDATES)
    debug.update({"id_col_used": id_col, "gs_col_used": gs_col, "ip_col_used": ip_col})

    if id_col is None or gs_col is None:
        return pd.DataFrame(columns=["player_id", "gs", "ip"]), debug

    out = pd.DataFrame({
        "player_id": df[id_col],
        "gs": pd.to_numeric(df[gs_col], errors="coerce"),
        "ip": pd.to_numeric(df[ip_col], errors="coerce") if ip_col else pd.NA,
    })
    return out.dropna(subset=["player_id"]), debug


def format_last_first(full_name: str) -> str:
    """Normalize any player-name format to 'Last Name, First Name'."""
    if not isinstance(full_name, str) or not full_name.strip():
        return ""
    name = full_name.strip()
    if "," in name:
        last, _, first = name.partition(",")
        return f"{last.strip()}, {first.strip()}" if first.strip() else last.strip()
    parts = name.split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def get_last_name(full_name: str) -> str:
    """Just the last name, for the fixed point label above each dot."""
    formatted = format_last_first(full_name)
    return formatted.split(",")[0].strip() if formatted else ""


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

    combined = combined.sort_values("total_run_value", ascending=False).reset_index(drop=True)
    combined.index += 1
    return combined, debug_info


# Cached the same way as build_leaderboard -- fetched once, held until a
# manual refresh. Returns {"neutral": df, "leveraged": df}, each with
# [player_id, player_name, team_id, pitching_rv, gs, ip]. The GS/IP source is
# shared between both; only the run-value source (and its leverage setting)
# differs.
@st.cache_data(ttl=None, show_spinner=False)
def build_pitchers_data(year: int):
    debug_info = {}

    try:
        starts_raw = fetch_csv(STARTS_URL.format(year=year))
        starts_norm, starts_debug = normalize_starts(starts_raw)
        debug_info["pitchers_gs_ip"] = {"status": "ok", "url": STARTS_URL.format(year=year), **starts_debug}
    except Exception as e:
        starts_norm = pd.DataFrame(columns=["player_id", "gs", "ip"])
        debug_info["pitchers_gs_ip"] = {"status": f"error: {e}", "url": STARTS_URL.format(year=year)}

    datasets = {}
    for mode, url_template in [("neutral", PITCHERS_NEUTRAL_RV_URL), ("leveraged", PITCHERS_LEVERAGED_RV_URL)]:
        url = url_template.format(year=year)
        try:
            raw = fetch_csv(url)
            norm, debug = normalize(raw, "pitching")
            debug_info[f"pitchers_rv_{mode}"] = {"status": "ok", "url": url, **debug}
        except Exception as e:
            norm = pd.DataFrame(columns=["player_id", "player_name", "team_id", "pitching_rv"])
            debug_info[f"pitchers_rv_{mode}"] = {"status": f"error: {e}", "url": url}

        merged = norm.merge(starts_norm, on="player_id", how="left")
        merged["gs"] = merged["gs"].fillna(0)
        merged["ip"] = merged["ip"].fillna(0)
        datasets[mode] = merged

    return datasets, debug_info


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


def build_pitcher_line_chart(subset: pd.DataFrame, x_axis_title: str, show_gs_in_tooltip: bool) -> go.Figure | None:
    """One full-width horizontal number line per team (stacked vertically, one
    row per team), grouped by division with extra blank spacing between
    divisions and teams alphabetized within each division. Every line shares
    the same x-axis range (min/max pitching RV across the given subset), so
    lengths stay directly comparable. Labels alternate above/below their
    point when consecutive points on the same line sit close enough to
    collide. `subset` is expected to already be filtered to the pitchers
    that should appear (Starters or Relievers) -- this function just draws it.
    Point size is driven by IP. The tooltip always shows IP; GS is added too
    only when show_gs_in_tooltip is True (starters), since it's ~0 and not
    meaningful for relievers."""
    subset = subset[subset["team_id"].notna()].copy()
    if subset.empty:
        return None

    subset["last_name"] = subset["player_name"].apply(get_last_name)
    subset["full_name"] = subset["player_name"].apply(format_last_first)
    subset["team_abbr"] = subset["team_id"].apply(
        lambda t: TEAM_ID_TO_ABBR.get(int(t), str(int(t)))
    )
    # Local max IP (within this subset) drives marker sizing, so starters and
    # relievers each get a meaningful size spread rather than one group
    # rendering tiny against the other group's scale.
    local_max_ip = max(subset["ip"].max(), 1)

    league_min = subset["pitching_rv"].min()
    league_max = subset["pitching_rv"].max()
    pad = max((league_max - league_min) * 0.08, 1)
    x_range = [league_min - pad, league_max + pad]

    min_size, max_size = 10, 32
    # Points within this fraction of the x-range are considered "close enough"
    # to collide; the second of the pair gets flipped to the other side.
    x_span = x_range[1] - x_range[0]
    collision_gap = x_span * 0.05

    # Build the y-axis category order: teams grouped by division (alphabetized
    # within each), with several hidden spacer categories between divisions
    # for extra vertical breathing room -- bigger gap than the one used
    # between teams within the same division (which comes from ROW_PX alone).
    GAPS_BETWEEN_DIVISIONS = 3
    y_categories, tick_text = [], []
    for i, division in enumerate(DIVISION_ORDER):
        for team in sorted(DIVISIONS[division]):
            y_categories.append(team)
            tick_text.append(team)
        if i < len(DIVISION_ORDER) - 1:
            for g in range(GAPS_BETWEEN_DIVISIONS):
                y_categories.append(f"__gap_{i}_{g}__")
                tick_text.append("")

    if show_gs_in_tooltip:
        customdata_cols = ["team_abbr", "full_name", "gs", "ip"]
        hovertemplate = (
            "%{customdata[0]}<br>%{customdata[1]}<br>"
            "Pitching RV: %{x:.0f}<br>GS: %{customdata[2]:.0f}<br>IP: %{customdata[3]:.1f}<extra></extra>"
        )
    else:
        customdata_cols = ["team_abbr", "full_name", "ip"]
        hovertemplate = (
            "%{customdata[0]}<br>%{customdata[1]}<br>"
            "Pitching RV: %{x:.0f}<br>IP: %{customdata[2]:.1f}<extra></extra>"
        )

    fig = go.Figure()
    for division in DIVISION_ORDER:
        teams = sorted(DIVISIONS[division])
        for team in teams:
            team_df = subset[subset["team_abbr"] == team].sort_values("pitching_rv")
            team_ids = subset.loc[subset["team_abbr"] == team, "team_id"]
            team_color = TEAM_ID_TO_COLOR.get(int(team_ids.iloc[0]), "#1f77b4") if not team_ids.empty else "#1f77b4"

            fig.add_trace(go.Scatter(
                x=x_range, y=[team, team],
                mode="lines",
                line=dict(color="rgba(150,150,150,0.4)", width=2),
                hoverinfo="skip",
                showlegend=False,
            ))

            if not team_df.empty:
                sizes = min_size + (team_df["ip"] / local_max_ip) * (max_size - min_size)
                customdata = team_df[customdata_cols].values

                # Alternate top/bottom placement whenever consecutive points
                # (sorted left to right) are close enough to collide.
                text_positions = []
                prev_x, last_pos = None, "top center"
                for x in team_df["pitching_rv"]:
                    if prev_x is not None and (x - prev_x) < collision_gap:
                        last_pos = "bottom center" if last_pos == "top center" else "top center"
                    else:
                        last_pos = "top center"
                    text_positions.append(last_pos)
                    prev_x = x

                fig.add_trace(go.Scatter(
                    x=team_df["pitching_rv"], y=[team] * len(team_df),
                    mode="markers+text",
                    text=team_df["last_name"],
                    textposition=text_positions,
                    textfont=dict(size=10),
                    marker=dict(size=sizes, color=team_color, line=dict(color="white", width=1)),
                    customdata=customdata,
                    hovertemplate=hovertemplate,
                    showlegend=False,
                ))

        # Division label, floating well clear of the y-axis tick labels at the
        # middle row of that division's block.
        mid_team = teams[len(teams) // 2]
        fig.add_annotation(
            xref="paper", x=-0.16, xanchor="right",
            yref="y", y=mid_team,
            text=f"<b>{division}</b>",
            showarrow=False,
            font=dict(size=12, color="gray"),
        )

    ROW_PX = 60
    fig.update_layout(
        height=max(400, ROW_PX * len(y_categories) + 120),
        margin=dict(l=170, r=30, t=30, b=40),
        xaxis=dict(title=x_axis_title, range=x_range, zeroline=True, fixedrange=True),
        yaxis=dict(
            title="",
            tickmode="array",
            tickvals=y_categories,
            ticktext=tick_text,
            categoryorder="array",
            categoryarray=y_categories[::-1],
            fixedrange=True,
        ),
        plot_bgcolor="rgba(0,0,0,0)",
        dragmode=False,
    )
    return fig


def render_scrollable_chart(fig: go.Figure) -> None:
    """Render a Plotly figure at a fixed pixel width inside a horizontally
    scrollable container. On screens narrower than CHART_FIXED_WIDTH (most
    phones), the chart keeps its full desktop layout and spacing -- the
    viewer scrolls sideways to see the rest of it, rather than everything
    getting squeezed to fit.

    st.plotly_chart can't be wrapped this way directly (it renders as its own
    isolated component, not nested inside surrounding st.markdown HTML), so
    this exports the figure to a raw HTML snippet and embeds that snippet,
    plus our own scroll wrapper, together in one components.html iframe."""
    chart_height = fig.layout.height or 600
    fig.update_layout(width=CHART_FIXED_WIDTH)
    chart_html = fig.to_html(
        include_plotlyjs="cdn",
        full_html=False,
        config={"displayModeBar": False, "scrollZoom": False, "doubleClick": False},
    )
    wrapped_html = f"""
    <div style="overflow-x:auto; -webkit-overflow-scrolling:touch;">
        <div style="width:{CHART_FIXED_WIDTH}px;">
            {chart_html}
        </div>
    </div>
    """
    components.html(wrapped_html, height=chart_height + 40, scrolling=False)


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.title("⚾ Statcast Leaderboard")

col1, col2 = st.columns([1, 3])
with col1:
    year = st.number_input("Season", min_value=2016, max_value=2026, value=2026, step=1)
    if st.button("🔄 Force refresh now"):
        st.cache_data.clear()

with st.spinner("Pulling live data from Baseball Savant..."):
    leaderboard, debug_info = build_leaderboard(int(year))

if "view" not in st.session_state:
    st.session_state.view = "total_rv"

btn_col1, btn_col2, _ = st.columns([1, 1, 4])
with btn_col1:
    if st.button(
        "Total RV", use_container_width=True,
        type="primary" if st.session_state.view == "total_rv" else "secondary",
    ):
        st.session_state.view = "total_rv"
        st.rerun()
with btn_col2:
    if st.button(
        "Pitchers", use_container_width=True,
        type="primary" if st.session_state.view == "starting_pitchers" else "secondary",
    ):
        st.session_state.view = "starting_pitchers"
        st.rerun()

if st.session_state.view == "total_rv":
    st.caption(
        "Live from Baseball Savant. Total Run Value = Batting + Pitching + Fielding + "
        "Baserunning run value, summed per player."
    )

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

    debug_to_show = debug_info

else:
    with st.spinner("Pulling live pitcher data from Baseball Savant..."):
        pitchers_data, pitchers_debug = build_pitchers_data(int(year))

    if "pitcher_subview" not in st.session_state:
        st.session_state.pitcher_subview = "starters"

    sub_col1, sub_col2, _ = st.columns([1, 1, 4])
    with sub_col1:
        if st.button(
            "Starters", use_container_width=True,
            type="primary" if st.session_state.pitcher_subview == "starters" else "secondary",
        ):
            st.session_state.pitcher_subview = "starters"
            st.rerun()
    with sub_col2:
        if st.button(
            "Relievers", use_container_width=True,
            type="primary" if st.session_state.pitcher_subview == "relievers" else "secondary",
        ):
            st.session_state.pitcher_subview = "relievers"
            st.rerun()

    league_max_gs = pitchers_data["neutral"]["gs"].max()
    min_gs_threshold = math.ceil(MIN_GS_PCT_OF_MAX * league_max_gs) if pd.notna(league_max_gs) and league_max_gs > 0 else 0

    if st.session_state.pitcher_subview == "starters":
        subset = pitchers_data["neutral"]
        subset = subset[subset["gs"] >= min_gs_threshold]
        x_axis_title = "Pitching Run Value (Context-Neutral)"
        st.caption(
            "Starters: pitchers with GS at least 10% of the league's highest GS "
            f"(currently {min_gs_threshold}+). Scored with **context-neutral** run value."
        )
    else:
        subset = pitchers_data["leveraged"]
        subset = subset[(subset["gs"] < min_gs_threshold) & (subset["ip"] >= MIN_RELIEVER_IP)]
        x_axis_title = "Pitching Run Value (Leverage-Based)"
        st.caption(
            f"Relievers: pitchers with GS below the starter threshold and at least "
            f"{MIN_RELIEVER_IP} IP. Scored with **leverage-based** run value "
            "(weighted by game situation), not context-neutral."
        )

    st.caption(
        "One full-width line per team, grouped by division (with extra spacing "
        "between divisions) and alphabetized within each division. Dot position = "
        "run value (lower to the left, higher to the right); dot size = Innings "
        "Pitched; dot color = team's primary color; every line spans the same "
        "min-to-max range. Labels flip above/below their point when two on the "
        "same line sit close together."
    )
    pitcher_chart = build_pitcher_line_chart(
        subset, x_axis_title,
        show_gs_in_tooltip=(st.session_state.pitcher_subview == "starters"),
    )
    if pitcher_chart is not None:
        st.caption("↔ Scroll horizontally to see all teams on smaller screens.")
        render_scrollable_chart(pitcher_chart)
    else:
        st.info("No pitcher data available for this season/selection yet.")

    debug_to_show = {**debug_info, **pitchers_debug}

with st.expander("🔧 Debug: raw source status & column mapping (check this if numbers look off)"):
    for category, info in debug_to_show.items():
        st.markdown(f"**{category}**")
        st.json(info)
