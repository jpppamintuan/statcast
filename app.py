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
    "fielding": "https://baseballsavant.mlb.com/leaderboard/fielding-run-value?gameType=Regular&seasonStart={year}&seasonEnd={year}&type=fielder&position={position}&minInnings=q&minResults=1&csv=true",
    "baserunning": "https://baseballsavant.mlb.com/leaderboard/baserunning-run-value?season_start={year}&season_end={year}&csv=true",
}

# Position/role filter for the Total RV table. Each entry says (a) what
# "position" value to request from Savant's fielding-run-value leaderboard
# (only meaningful for actual fielding positions) and (b) which fetched
# source's player list to use as the row filter.
#
# C/1B/2B/3B/SS match the standard baseball scorekeeping numbers (2-6),
# confirmed live against Savant's own filter panel; LF/CF/RF (7/8/9) follow
# the same convention but weren't independently confirmed as raw values (the
# live page only showed label text, not the underlying codes) -- worth a
# quick check against the debug panel's row counts once deployed.
#
# "Pitcher" is NOT a fielding-position code on Savant (there isn't one), so
# it's handled differently: filtered by presence on the PITCHING source
# instead of the fielding one. This is deliberately an inclusive filter
# ("has a pitching RV entry") rather than an exclusionary one ("has no
# batting/fielding/baserunning entry") specifically so two-way players still
# show up under "Pitcher" -- excluding them just because they also hit would
# be wrong. The pitching source's own min=q (qualified) threshold already
# screens out token/mop-up position-player-pitching appearances, so this
# doesn't need extra filtering to avoid false positives from that.
#
# There's still no way to isolate a pure DH this way -- DH has no fielding
# code and no pitching record, so nothing distinguishes it from "didn't play."
POSITION_OPTIONS = {
    "All": {"fielding_position": 0, "filter_source": None},
    "C": {"fielding_position": 2, "filter_source": "fielding"},
    "1B": {"fielding_position": 3, "filter_source": "fielding"},
    "2B": {"fielding_position": 4, "filter_source": "fielding"},
    "3B": {"fielding_position": 5, "filter_source": "fielding"},
    "SS": {"fielding_position": 6, "filter_source": "fielding"},
    "LF": {"fielding_position": 7, "filter_source": "fielding"},
    "CF": {"fielding_position": 8, "filter_source": "fielding"},
    "RF": {"fielding_position": 9, "filter_source": "fielding"},
    "Pitcher": {"fielding_position": 0, "filter_source": "pitching"},
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

# Streamlit's default theme font is "Source Sans" (branded "Source Sans Pro"
# in most tooling, renamed "Source Sans 3" in its current Google Fonts
# release) -- match it so the chart's text doesn't look like a different app.
# This has to be set explicitly rather than inherited: the chart is embedded
# via components.html, which renders in its own iframe and does NOT pick up
# the parent page's theme CSS (font included) -- a known Streamlit limitation
# (streamlit/streamlit#10660), not something fixable from our side via CSS.
# The <link> tags in render_scrollable_chart() load the actual font file
# inside that iframe; this family stack is what Plotly is told to use.
FONT_FAMILY = '"Source Sans Pro", "Source Sans 3", sans-serif'

# --- Team Performance feature config --------------------------------------
# wRC+ and FIP- aren't available from a site whose terms permit automated
# access (FanGraphs and Baseball-Reference both explicitly prohibit
# scraping/API use for building tools). MLB's own Stats API is free, keyless,
# and official, so team component stats are pulled from there instead and
# wRC+ / FIP- are computed ourselves from the public formulas -- WITHOUT a
# park-factor adjustment (that's the deliberate simplification agreed on;
# FanGraphs' published wRC+/FIP- do include park adjustments, so our numbers
# will be close but not identical, especially for teams in extreme parks).
MLB_STATSAPI_BASE = "https://statsapi.mlb.com/api/v1"
TEAM_HITTING_SEASON_URL = MLB_STATSAPI_BASE + "/teams/stats?stats=season&group=hitting&season={year}&sportId=1"
TEAM_PITCHING_SEASON_URL = MLB_STATSAPI_BASE + "/teams/stats?stats=season&group=pitching&season={year}&sportId=1"
TEAM_HITTING_SABERMETRICS_URL = MLB_STATSAPI_BASE + "/teams/stats?stats=sabermetrics&group=hitting&season={year}&sportId=1"
# Fallback if the team-level sabermetrics stat type above doesn't exist:
# pull it per player (confirmed to work) and aggregate wRAA by team ourselves.
PLAYER_HITTING_SABERMETRICS_URL = MLB_STATSAPI_BASE + "/stats?stats=sabermetrics&group=hitting&season={year}&sportId=1&playerPool=all&limit=2000"
# Player-level pitching component stats, for the rotation-vs-bullpen FIP-
# scatter: this single source gives both the FIP inputs (HR/BB/HBP/K/IP) AND
# gamesStarted for role classification, so there's no need to cross-reference
# Savant's GS source and risk a player_id mismatch between the two systems.
PLAYER_PITCHING_SEASON_URL = MLB_STATSAPI_BASE + "/stats?stats=season&group=pitching&season={year}&sportId=1&playerPool=all&limit=3000"

# Candidate JSON field names per stat -- MLB Stats API field names are fairly
# stable, but this hasn't been live-tested from this environment (no network
# access here), so candidates are kept broad the same way CSV columns are
# elsewhere in this file. Check the debug panel's "sample_stat_keys" entries
# if any of these come back empty.
STATSAPI_FIELD_CANDIDATES = {
    "hr_allowed": ["homeRuns"],
    "bb": ["baseOnBalls", "walks"],
    "hbp": ["hitBatsmen", "hitByPitch"],
    "so": ["strikeOuts", "strikeouts"],
    "ip": ["inningsPitched"],
    "earned_runs": ["earnedRuns"],
    "runs": ["runs"],
    "pa": ["plateAppearances"],
    "wraa": ["wRaa", "wRAA", "wraa"],
    # Team-level wins/losses -- confirmed present on the team pitching "season"
    # stat endpoint (TEAM_PITCHING_SEASON_URL), which is the team's actual
    # season record, NOT the sum of individual pitchers' win/loss decisions.
    "wins": ["wins"],
    "losses": ["losses"],
    "games_started": ["gamesStarted"],
}

# Team fielding run value -- from Savant's own CSV export, same mechanism as
# every other Savant source in this file (not MLB Stats API, which doesn't
# have a fielding-run-value equivalent).
TEAM_FIELDING_RV_URL = "https://baseballsavant.mlb.com/leaderboard/fielding-run-value?gameType=Regular&seasonStart={year}&seasonEnd={year}&type=fielding-team&position=0&minInnings=q&minResults=1&csv=true"
TEAM_FIELDING_ID_CANDIDATES = ["id", "team_id"]
TEAM_FIELDING_NAME_CANDIDATES = ["name", "team_name"]
TEAM_FIELDING_RV_CANDIDATES = ["total_runs", "run_value", "fielding_run_value"]

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

# Reverse lookup (abbreviation -> team_id) and a team_id -> league ("AL"/"NL")
# map, both derived from TEAM_ID_TO_ABBR/DIVISIONS above rather than
# maintained separately, for the Total RV table's team/league filters.
ABBR_TO_TEAM_ID = {abbr: team_id for team_id, abbr in TEAM_ID_TO_ABBR.items()}
TEAM_ID_TO_LEAGUE = {
    ABBR_TO_TEAM_ID[abbr]: ("AL" if division.startswith("AL") else "NL")
    for division, abbrs in DIVISIONS.items()
    for abbr in abbrs
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StatcastLeaderboardApp/1.0)"}

# Columns that get the Statcast-style diverging color scale + integer rounding.
RV_KEYS = ["batting_rv", "pitching_rv", "fielding_rv", "baserunning_rv", "total_run_value"]


def pick_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def pick_stat(stat: dict, candidates: list[str]):
    """Same idea as pick_col, but for a JSON stat dict's keys instead of a
    DataFrame's columns (used for the MLB Stats API responses)."""
    for c in candidates:
        if c in stat:
            return stat[c]
    return None


def parse_innings_pitched(value) -> float:
    """MLB's innings-pitched notation uses .0/.1/.2 for outs (thirds), not
    real tenths (e.g. "162.1" means 162 innings and 1 out = 162 + 1/3). This
    matters here because FIP needs an actual sum/divide, unlike the
    threshold-only IP comparison used elsewhere in this file."""
    if value is None:
        return 0.0
    s = str(value)
    whole_str, sep, frac_str = s.partition(".")
    try:
        whole = float(whole_str)
    except ValueError:
        return 0.0
    outs = 0
    if sep and frac_str:
        try:
            outs = int(frac_str[0])
        except ValueError:
            outs = 0
    return whole + outs / 3.0


@st.cache_data(ttl=None, show_spinner=False)
def fetch_json(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def extract_team_splits(payload: dict) -> list[dict]:
    """MLB Stats API team-stats responses nest results under
    stats[0].splits, each split holding a 'team' dict and a 'stat' dict."""
    try:
        return payload["stats"][0]["splits"]
    except (KeyError, IndexError, TypeError):
        return []


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


def normalize_team_fielding(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Reduce the team fielding-run-value leaderboard CSV down to
    [team_id, fielding_rv]."""
    debug = {"columns_found": list(df.columns), "rows": len(df)}
    id_col = pick_col(df, TEAM_FIELDING_ID_CANDIDATES)
    rv_col = pick_col(df, TEAM_FIELDING_RV_CANDIDATES)
    debug.update({"id_col_used": id_col, "rv_col_used": rv_col})

    if id_col is None or rv_col is None:
        return pd.DataFrame(columns=["team_id", "fielding_rv"]), debug

    out = pd.DataFrame({
        "team_id": pd.to_numeric(df[id_col], errors="coerce"),
        "fielding_rv": pd.to_numeric(df[rv_col], errors="coerce"),
    })
    return out.dropna(subset=["team_id"]), debug


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
def build_leaderboard(year: int, position_filter: str = "All"):
    """position_filter is a row FILTER, not a recalculation: Batting/Pitching/
    Fielding/Baserunning RV columns stay each player's season totals as
    always. For an actual fielding position, the table is restricted to
    players who show up on Savant's fielding-run-value leaderboard at that
    position. For "Pitcher", it's restricted to players present on the
    pitching RV source instead (see POSITION_OPTIONS for why this is
    deliberately inclusive rather than exclusionary). "All" applies no filter.
    Pure DH can't be isolated either way -- no fielding code, no pitching
    record, nothing to filter on."""
    position_config = POSITION_OPTIONS.get(position_filter, POSITION_OPTIONS["All"])
    fielding_position = position_config["fielding_position"]
    filter_source = position_config["filter_source"]

    frames = {}
    debug_info = {}
    source_player_ids = {}
    for category, url_template in SOURCES.items():
        url = url_template.format(year=year, position=fielding_position)
        try:
            raw = fetch_csv(url)
            norm, debug = normalize(raw, category)
            frames[category] = norm
            debug_info[category] = {"status": "ok", "url": url, **debug}
            source_player_ids[category] = set(norm["player_id"].dropna())
        except Exception as e:
            frames[category] = pd.DataFrame(columns=["player_id", "player_name", "team_id", f"{category}_rv"])
            debug_info[category] = {"status": f"error: {e}", "url": url}
            source_player_ids[category] = set()

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

    # The actual position/role-filter step: everyone outside the matching
    # source's player list is excluded entirely, not just blanked out -- a
    # real row filter, not a recalculation.
    if filter_source is not None:
        combined = combined[combined["player_id"].isin(source_player_ids.get(filter_source, set()))]

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


@st.cache_data(ttl=None, show_spinner=False)
def build_team_performance_data(year: int):
    """Team-level wRC+ and FIP-, computed ourselves (no park adjustment) from
    MLB Stats API component totals -- see the config comment above for why."""
    debug_info = {}

    # --- Team pitching component totals (drives FIP-) ---
    pitching_rows = []
    try:
        payload = fetch_json(TEAM_PITCHING_SEASON_URL.format(year=year))
        splits = extract_team_splits(payload)
        for split in splits:
            team = split.get("team", {})
            stat = split.get("stat", {})
            pitching_rows.append({
                "team_id": team.get("id"),
                "hr_allowed": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["hr_allowed"]),
                "bb": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["bb"]),
                "hbp": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["hbp"]),
                "so": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["so"]),
                "ip": parse_innings_pitched(pick_stat(stat, STATSAPI_FIELD_CANDIDATES["ip"])),
                "earned_runs": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["earned_runs"]),
                "wins": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["wins"]),
                "losses": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["losses"]),
            })
        debug_info["team_pitching_season"] = {
            "status": "ok", "url": TEAM_PITCHING_SEASON_URL.format(year=year),
            "teams_found": len(splits),
            "sample_stat_keys": list(splits[0]["stat"].keys()) if splits else [],
        }
    except Exception as e:
        debug_info["team_pitching_season"] = {"status": f"error: {e}", "url": TEAM_PITCHING_SEASON_URL.format(year=year)}
    pitching_df = pd.DataFrame(pitching_rows)

    # --- Team hitting totals (team PA, plus league runs/PA) ---
    hitting_rows = []
    try:
        payload = fetch_json(TEAM_HITTING_SEASON_URL.format(year=year))
        splits = extract_team_splits(payload)
        for split in splits:
            team = split.get("team", {})
            stat = split.get("stat", {})
            hitting_rows.append({
                "team_id": team.get("id"),
                "runs": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["runs"]),
                "pa": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["pa"]),
            })
        debug_info["team_hitting_season"] = {
            "status": "ok", "url": TEAM_HITTING_SEASON_URL.format(year=year),
            "teams_found": len(splits),
            "sample_stat_keys": list(splits[0]["stat"].keys()) if splits else [],
        }
    except Exception as e:
        debug_info["team_hitting_season"] = {"status": f"error: {e}", "url": TEAM_HITTING_SEASON_URL.format(year=year)}
    hitting_df = pd.DataFrame(hitting_rows)

    # --- Team wRAA (drives wRC+) -- try team-level sabermetrics first, fall
    # back to aggregating player-level sabermetrics by team if that stat type
    # isn't available at the team level. ---
    wraa_rows = []
    try:
        payload = fetch_json(TEAM_HITTING_SABERMETRICS_URL.format(year=year))
        splits = extract_team_splits(payload)
        for split in splits:
            team = split.get("team", {})
            stat = split.get("stat", {})
            wraa_val = pick_stat(stat, STATSAPI_FIELD_CANDIDATES["wraa"])
            if wraa_val is not None:
                wraa_rows.append({"team_id": team.get("id"), "wraa": wraa_val})
        debug_info["team_hitting_sabermetrics"] = {
            "status": "ok" if wraa_rows else "fetched but no wRAA field found",
            "url": TEAM_HITTING_SABERMETRICS_URL.format(year=year),
            "teams_found": len(splits),
            "sample_stat_keys": list(splits[0]["stat"].keys()) if splits else [],
        }
    except Exception as e:
        debug_info["team_hitting_sabermetrics"] = {"status": f"error: {e}", "url": TEAM_HITTING_SABERMETRICS_URL.format(year=year)}

    if not wraa_rows:
        try:
            payload = fetch_json(PLAYER_HITTING_SABERMETRICS_URL.format(year=year))
            splits = extract_team_splits(payload)
            player_rows = []
            for split in splits:
                team = split.get("team", {})
                stat = split.get("stat", {})
                wraa_val = pick_stat(stat, STATSAPI_FIELD_CANDIDATES["wraa"])
                if team.get("id") is not None and wraa_val is not None:
                    player_rows.append({"team_id": team.get("id"), "wraa": wraa_val})
            player_df = pd.DataFrame(player_rows)
            if not player_df.empty:
                agg = player_df.groupby("team_id", as_index=False)["wraa"].sum()
                wraa_rows = agg.to_dict("records")
            debug_info["player_hitting_sabermetrics_fallback"] = {
                "status": "ok" if wraa_rows else "fetched but no wRAA field found",
                "url": PLAYER_HITTING_SABERMETRICS_URL.format(year=year),
                "players_found": len(splits),
                "sample_stat_keys": list(splits[0]["stat"].keys()) if splits else [],
            }
        except Exception as e:
            debug_info["player_hitting_sabermetrics_fallback"] = {"status": f"error: {e}", "url": PLAYER_HITTING_SABERMETRICS_URL.format(year=year)}
    wraa_df = pd.DataFrame(wraa_rows) if wraa_rows else pd.DataFrame(columns=["team_id", "wraa"])

    # --- Team fielding run value (third dimension, from Savant directly) ---
    try:
        fielding_raw = fetch_csv(TEAM_FIELDING_RV_URL.format(year=year))
        fielding_df, fielding_debug = normalize_team_fielding(fielding_raw)
        debug_info["team_fielding_rv"] = {"status": "ok", "url": TEAM_FIELDING_RV_URL.format(year=year), **fielding_debug}
    except Exception as e:
        fielding_df = pd.DataFrame(columns=["team_id", "fielding_rv"])
        debug_info["team_fielding_rv"] = {"status": f"error: {e}", "url": TEAM_FIELDING_RV_URL.format(year=year)}

    if pitching_df.empty or hitting_df.empty:
        return pd.DataFrame(), debug_info

    combined = pitching_df.merge(hitting_df, on="team_id", how="outer")
    combined = combined.merge(wraa_df, on="team_id", how="left")
    combined = combined.merge(fielding_df, on="team_id", how="left")
    numeric_cols = ["hr_allowed", "bb", "hbp", "so", "ip", "earned_runs", "runs", "pa", "wraa", "fielding_rv", "wins", "losses"]
    for c in numeric_cols:
        combined[c] = pd.to_numeric(combined[c], errors="coerce")
    combined = combined.dropna(subset=["team_id"])

    # --- League totals (straight sums across all teams, not an average of
    # per-team rates, so partial/extra-inning games don't skew things) ---
    league_hr = combined["hr_allowed"].sum()
    league_bb = combined["bb"].sum()
    league_hbp = combined["hbp"].sum()
    league_so = combined["so"].sum()
    league_ip = combined["ip"].sum()
    league_er = combined["earned_runs"].sum()
    league_era = 9 * league_er / league_ip if league_ip else None
    league_runs = combined["runs"].sum()
    league_pa = combined["pa"].sum()
    league_r_pa = league_runs / league_pa if league_pa else None

    fip_constant = None
    if league_era is not None and league_ip:
        fip_constant = league_era - (13 * league_hr + 3 * (league_bb + league_hbp) - 2 * league_so) / league_ip

    debug_info["league_totals"] = {
        "league_era": league_era, "fip_constant": fip_constant,
        "league_r_pa": league_r_pa, "league_ip": league_ip,
    }

    def compute_row(row):
        fip = fip_minus = wrc_plus = None
        if row["ip"] and fip_constant is not None:
            fip = (13 * row["hr_allowed"] + 3 * (row["bb"] + row["hbp"]) - 2 * row["so"]) / row["ip"] + fip_constant
            if league_era:
                fip_minus = (fip / league_era) * 100
        if pd.notna(row.get("wraa")) and row.get("pa") and league_r_pa:
            wrc_plus = ((row["wraa"] / row["pa"] + league_r_pa) / league_r_pa) * 100
        return pd.Series({"fip": fip, "fip_minus": fip_minus, "wrc_plus": wrc_plus})

    combined = pd.concat([combined, combined.apply(compute_row, axis=1)], axis=1)
    combined["team_abbr"] = combined["team_id"].apply(
        lambda t: TEAM_ID_TO_ABBR.get(int(t), str(int(t))) if pd.notna(t) else None
    )
    return combined, debug_info


@st.cache_data(ttl=None, show_spinner=False)
def build_rotation_bullpen_fip_data(year: int):
    """Per-team FIP- for starters vs. relievers -- a different computation
    from build_team_performance_data's team-level FIP-: here, pitchers are
    first classified Starter/Reliever individually, then each team+role
    group's RAW FIP COMPONENTS (HR/BB/HBP/K/IP) are SUMMED before computing
    one FIP- for the group -- not an unweighted average of each pitcher's own
    FIP-. This matters: summing components first is mathematically identical
    to weighting each pitcher's FIP- by his innings pitched (since a weighted
    mean of (numerator_i/IP_i) by IP_i reduces to sum(numerator_i)/sum(IP_i)),
    so a 16-inning reliever's one bad outing can't swing the group average as
    much as a 180-inning workhorse's full season. The league FIP constant/ERA
    used to index each group's FIP- is recomputed independently here from
    team totals (small amount of duplicated arithmetic vs.
    build_team_performance_data, not a duplicated network call, since the
    underlying fetch is itself cached) rather than threading a shared value
    between the two functions, to keep this feature self-contained and avoid
    touching the already-working first chart."""
    debug_info = {}

    # League FIP constant + league ERA (same formula as build_team_performance_data).
    try:
        payload = fetch_json(TEAM_PITCHING_SEASON_URL.format(year=year))
        splits = extract_team_splits(payload)
        rows = []
        for split in splits:
            stat = split.get("stat", {})
            rows.append({
                "hr_allowed": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["hr_allowed"]),
                "bb": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["bb"]),
                "hbp": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["hbp"]),
                "so": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["so"]),
                "ip": parse_innings_pitched(pick_stat(stat, STATSAPI_FIELD_CANDIDATES["ip"])),
                "earned_runs": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["earned_runs"]),
            })
        league_df = pd.DataFrame(rows)
        for c in ["hr_allowed", "bb", "hbp", "so", "ip", "earned_runs"]:
            league_df[c] = pd.to_numeric(league_df[c], errors="coerce")
        league_hr, league_bb = league_df["hr_allowed"].sum(), league_df["bb"].sum()
        league_hbp, league_so = league_df["hbp"].sum(), league_df["so"].sum()
        league_ip, league_er = league_df["ip"].sum(), league_df["earned_runs"].sum()
        league_era = 9 * league_er / league_ip if league_ip else None
        fip_constant = (
            league_era - (13 * league_hr + 3 * (league_bb + league_hbp) - 2 * league_so) / league_ip
            if (league_era is not None and league_ip) else None
        )
        debug_info["league_pitching_totals"] = {
            "status": "ok", "league_era": league_era, "fip_constant": fip_constant,
        }
    except Exception as e:
        league_era = fip_constant = None
        debug_info["league_pitching_totals"] = {"status": f"error: {e}"}

    if league_era is None or fip_constant is None:
        return pd.DataFrame(), debug_info

    # Per-pitcher component stats + gamesStarted, all from one source.
    player_rows = []
    try:
        payload = fetch_json(PLAYER_PITCHING_SEASON_URL.format(year=year))
        splits = extract_team_splits(payload)
        for split in splits:
            player = split.get("player", {})
            team = split.get("team", {})
            stat = split.get("stat", {})
            player_rows.append({
                "player_id": player.get("id"),
                "team_id": team.get("id"),
                "hr_allowed": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["hr_allowed"]),
                "bb": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["bb"]),
                "hbp": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["hbp"]),
                "so": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["so"]),
                "ip": parse_innings_pitched(pick_stat(stat, STATSAPI_FIELD_CANDIDATES["ip"])),
                "gs": pick_stat(stat, STATSAPI_FIELD_CANDIDATES["games_started"]),
            })
        debug_info["player_pitching_season"] = {
            "status": "ok", "url": PLAYER_PITCHING_SEASON_URL.format(year=year),
            "players_found": len(splits),
            "sample_stat_keys": list(splits[0]["stat"].keys()) if splits else [],
        }
    except Exception as e:
        debug_info["player_pitching_season"] = {"status": f"error: {e}", "url": PLAYER_PITCHING_SEASON_URL.format(year=year)}

    player_df = pd.DataFrame(player_rows)
    if player_df.empty:
        return pd.DataFrame(), debug_info
    for c in ["hr_allowed", "bb", "hbp", "so", "ip", "gs"]:
        player_df[c] = pd.to_numeric(player_df[c], errors="coerce")
    player_df = player_df.dropna(subset=["team_id", "ip"])

    # Role classification -- same thresholds as the Pitchers tab.
    league_max_gs = player_df["gs"].max()
    if pd.isna(league_max_gs) or league_max_gs <= 0:
        return pd.DataFrame(), debug_info
    min_gs_threshold = math.ceil(MIN_GS_PCT_OF_MAX * league_max_gs)

    def classify(row):
        if row["gs"] >= min_gs_threshold:
            return "starter"
        if row["ip"] >= MIN_RELIEVER_IP:
            return "reliever"
        return None

    player_df["role"] = player_df.apply(classify, axis=1)
    player_df = player_df.dropna(subset=["role"])

    # Sum raw FIP components within each team+role group FIRST (IP-weighted
    # result -- see function docstring), then compute one FIP-/group from
    # those sums, rather than averaging each pitcher's individually-computed
    # FIP-.
    grouped = player_df.groupby(["team_id", "role"]).agg(
        hr_allowed=("hr_allowed", "sum"),
        bb=("bb", "sum"),
        hbp=("hbp", "sum"),
        so=("so", "sum"),
        ip=("ip", "sum"),
        pitcher_count=("player_id", "count"),
    ).reset_index()

    def group_fip_minus(row):
        if not row["ip"]:
            return None
        fip = (13 * row["hr_allowed"] + 3 * (row["bb"] + row["hbp"]) - 2 * row["so"]) / row["ip"] + fip_constant
        return (fip / league_era) * 100

    grouped["fip_minus"] = grouped.apply(group_fip_minus, axis=1)
    grouped = grouped.dropna(subset=["fip_minus"])

    fip_pivot = grouped.pivot(index="team_id", columns="role", values="fip_minus")
    count_pivot = grouped.pivot(index="team_id", columns="role", values="pitcher_count")
    fip_pivot = fip_pivot.rename(columns={"starter": "starter_fip_minus", "reliever": "reliever_fip_minus"})
    count_pivot = count_pivot.rename(columns={"starter": "starter_count", "reliever": "reliever_count"})
    result = fip_pivot.join(count_pivot).reset_index()
    result["team_abbr"] = result["team_id"].apply(
        lambda t: TEAM_ID_TO_ABBR.get(int(t), str(int(t))) if pd.notna(t) else None
    )
    return result, debug_info


def build_team_performance_chart(df: pd.DataFrame) -> go.Figure | None:
    """One point per team: wRC+ on x (higher = better hitting), FIP- on y.
    The y-axis is reversed so "up" always means "better" in both dimensions
    (lower FIP- is actually better pitching, same convention as ERA-) --
    without the reversal, the top-right corner would misleadingly mix a good
    result (high wRC+) with a bad one (high FIP-). Axis ranges are forced
    symmetric around 100 on each axis, so the league-average point (100, 100)
    always sits at the visual center of the plot rather than wherever the
    data's own min/max happen to place it.

    Fielding run value is the third dimension, shown as marker FILL color on
    a red (positive) / gray (zero) / blue (negative) diverging scale, scaled
    symmetrically around 0 using the largest absolute value across all 30
    teams -- so a team's shade reflects how it compares leaguewide, not just
    its own raw number. This uses Plotly's native numeric color + colorscale
    (rather than pre-computed rgba strings) specifically so a real colorbar
    legend can be attached to it. Marker SIZE is a fourth dimension: team
    wins (from the team pitching stat endpoint's own win total -- the
    team's actual season record, not a sum of individual pitchers' win/loss
    decisions). Team identity is the black label text next to each point."""
    df = df.dropna(subset=["wrc_plus", "fip_minus", "team_id"]).copy()
    if df.empty:
        return None

    # Symmetric ranges: how far the data strays from 100 in the worst
    # direction on each axis becomes the half-width/half-height on both
    # sides, so 100 lands exactly in the middle either way.
    x_half = max((df["wrc_plus"] - 100).abs().max(), 1) * 1.15
    y_half = max((df["fip_minus"] - 100).abs().max(), 1) * 1.15
    x_range = [100 - x_half, 100 + x_half]
    y_range = [100 - y_half, 100 + y_half]  # smaller FIP- (better) at the low end

    DIVERGING_COLORSCALE = [
        [0.0, "rgb(31,119,180)"],   # most negative -- blue
        [0.5, "rgb(225,225,225)"],  # zero -- neutral gray
        [1.0, "rgb(214,39,40)"],    # most positive -- red
    ]

    # Marker size <- wins. Chosen purely for legibility: big enough that the
    # worst team's dot is still clearly a dot, small enough that the best
    # team's dot doesn't swallow its neighbors' labels.
    MIN_MARKER_SIZE, MAX_MARKER_SIZE = 12, 34
    has_wins = "wins" in df.columns and df["wins"].notna().any()
    if has_wins:
        win_min, win_max = df["wins"].min(), df["wins"].max()

        def size_for_wins(w):
            if pd.isna(w) or win_max == win_min:
                return (MIN_MARKER_SIZE + MAX_MARKER_SIZE) / 2
            frac = (w - win_min) / (win_max - win_min)
            return MIN_MARKER_SIZE + frac * (MAX_MARKER_SIZE - MIN_MARKER_SIZE)

        # Sort biggest-wins first: traces draw in array order (later = on
        # top), so putting the biggest circles first and smallest last means
        # a smaller point never gets buried under a bigger overlapping one.
        df = df.sort_values("wins", ascending=False).reset_index(drop=True)
        marker_sizes = df["wins"].apply(size_for_wins)
    else:
        marker_sizes = pd.Series(MAX_MARKER_SIZE / 2, index=df.index)

    # fielding_vals is computed AFTER the sort above (not before) -- it must
    # read from the same row order as x/y/text/customdata below, or Plotly
    # aligns color-to-point purely by array position and every team ends up
    # wearing some other team's fielding color. That was the actual bug.
    fielding_vals = df["fielding_rv"].fillna(0) if "fielding_rv" in df.columns else pd.Series(0, index=df.index)
    fielding_max_abs = fielding_vals.abs().max()
    if pd.isna(fielding_max_abs) or not fielding_max_abs:
        fielding_max_abs = 1

    fig = go.Figure()

    # Subtle quadrant labels, drawn behind the markers. go.layout.Annotation
    # has no "layer" property (that's a shapes-only property, not valid on
    # annotations -- this is the fix for the ValueError), so instead this is
    # a text-only scatter trace added before the team marker traces below --
    # Plotly draws traces in the order they're added, so this one ends up
    # underneath. "Good pitching" is the low-FIP- half, which renders at the
    # TOP once the y-axis is reversed below -- labels are placed by data
    # value, not by screen position, so this still comes out visually correct.
    quadrant_labels = [
        (100 - x_half / 2, 100 - y_half / 2, "Bad Hitting, Good Pitching"),
        (100 + x_half / 2, 100 - y_half / 2, "Good Hitting, Good Pitching"),
        (100 - x_half / 2, 100 + y_half / 2, "Bad Hitting, Bad Pitching"),
        (100 + x_half / 2, 100 + y_half / 2, "Good Hitting, Bad Pitching"),
    ]
    fig.add_trace(go.Scatter(
        x=[q[0] for q in quadrant_labels],
        y=[q[1] for q in quadrant_labels],
        mode="text",
        text=[q[2] for q in quadrant_labels],
        textfont=dict(size=13, color="rgba(150,150,150,0.45)", family=FONT_FAMILY),
        hoverinfo="skip",
        showlegend=False,
    ))

    # All 30 teams as one trace (not one trace per team) -- needed so a
    # single colorbar legend renders once, rather than once per team.
    if "wins" not in df.columns:
        df["wins"] = None
    if "losses" not in df.columns:
        df["losses"] = None
    customdata = df[["team_abbr", "wrc_plus", "fip_minus", "fielding_rv", "wins", "losses"]].values
    fig.add_trace(go.Scatter(
        x=df["wrc_plus"], y=df["fip_minus"],
        mode="markers+text",
        text=df["team_abbr"],
        textposition="top center",
        textfont=dict(size=11, family=FONT_FAMILY, color="black"),
        marker=dict(
            size=marker_sizes,
            color=fielding_vals,
            colorscale=DIVERGING_COLORSCALE,
            cmin=-fielding_max_abs, cmax=fielding_max_abs,
            line=dict(color="white", width=1),
            colorbar=dict(
                orientation="h",
                thickness=5,
                len=0.25,
                x=0.22, xanchor="center",
                y=-0.16, yanchor="top",
                tickmode="array",
                tickvals=[-fielding_max_abs, 0, fielding_max_abs],
                ticktext=["◀ Worse fielding", "Avg", "Better fielding ▶"],
                outlinewidth=0,
                tickfont=dict(size=9, family=FONT_FAMILY),
            ),
        ),
        customdata=customdata,
        hovertemplate=(
            "%{customdata[0]}<br>wRC+: %{customdata[1]:.0f}<br>FIP-: %{customdata[2]:.0f}"
            "<br>Fielding RV: %{customdata[3]:.0f}<br>Record: %{customdata[4]:.0f}-%{customdata[5]:.0f}<extra></extra>"
        ),
        showlegend=False,
    ))

    # Text-based legend for wins, beside the colorbar. The earlier version
    # used reference circles of different sizes (the standard Plotly
    # workaround for a "size legend," since there's no native one the way
    # there's a native colorbar for color) -- dropped because two moderately
    # different win totals (e.g. 80 vs. 103) just don't read as distinguishable
    # circle sizes at a glance. Plain numbers are unambiguous instead.
    if has_wins:
        fig.add_annotation(
            xref="paper", yref="paper",
            x=0.98, y=-0.16, xanchor="right", yanchor="top",
            text=f"Point size = Wins (range: {int(win_min)}\u2013{int(win_max)})",
            showarrow=False,
            font=dict(size=10, color="rgba(100,100,100,0.9)", family=FONT_FAMILY),
        )

    fig.add_vline(x=100, line_dash="dash", line_color="rgba(150,150,150,0.5)")
    fig.add_hline(y=100, line_dash="dash", line_color="rgba(150,150,150,0.5)")

    fig.update_layout(
        height=650,
        margin=dict(l=70, r=40, t=30, b=110),
        font=dict(family=FONT_FAMILY),
        xaxis=dict(
            title="wRC+ (100 = league average)<br>→ better hitting",
            range=x_range, fixedrange=True,
        ),
        yaxis=dict(
            title="FIP- (100 = league average)<br>↑ better pitching",
            range=y_range[::-1], fixedrange=True,
        ),
        dragmode=False,
        hoverlabel=dict(font=dict(family=FONT_FAMILY)),
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def build_rotation_bullpen_chart(df: pd.DataFrame) -> go.Figure | None:
    """x = a team's starters' average FIP-, y = its relievers' average FIP-
    (each individually computed per pitcher, then averaged within role --
    not the team-level FIP- from the chart above). Both axes are reversed
    here, not just one: lower FIP- is better for BOTH roles (unlike the
    wRC+/FIP- chart, where one axis points up and the other down), so
    reversing both keeps "up and to the right = good team" consistent with
    the first chart. Centered at (100, 100) the same way, for the same
    reason -- so league-average sits in the middle regardless of the data's
    own spread."""
    df = df.dropna(subset=["starter_fip_minus", "reliever_fip_minus", "team_id"]).copy()
    if df.empty:
        return None

    x_half = max((df["starter_fip_minus"] - 100).abs().max(), 1) * 1.15
    y_half = max((df["reliever_fip_minus"] - 100).abs().max(), 1) * 1.15
    x_range = [100 - x_half, 100 + x_half]
    y_range = [100 - y_half, 100 + y_half]

    fig = go.Figure()

    # Subtle quadrant labels behind the markers -- same technique as the
    # chart above (text-only trace added first, so later traces draw over
    # it; go.layout.Annotation has no "layer" property to do this directly).
    quadrant_labels = [
        (100 - x_half / 2, 100 - y_half / 2, "Good Rotation, Good Bullpen"),
        (100 + x_half / 2, 100 - y_half / 2, "Bad Rotation, Good Bullpen"),
        (100 - x_half / 2, 100 + y_half / 2, "Good Rotation, Bad Bullpen"),
        (100 + x_half / 2, 100 + y_half / 2, "Bad Rotation, Bad Bullpen"),
    ]
    fig.add_trace(go.Scatter(
        x=[q[0] for q in quadrant_labels],
        y=[q[1] for q in quadrant_labels],
        mode="text",
        text=[q[2] for q in quadrant_labels],
        textfont=dict(size=13, color="rgba(150,150,150,0.45)", family=FONT_FAMILY),
        hoverinfo="skip",
        showlegend=False,
    ))

    if "starter_count" not in df.columns:
        df["starter_count"] = None
    if "reliever_count" not in df.columns:
        df["reliever_count"] = None

    marker_colors = df["team_id"].apply(lambda t: TEAM_ID_TO_COLOR.get(int(t), "#1f77b4"))
    customdata = df[["team_abbr", "starter_fip_minus", "reliever_fip_minus", "starter_count", "reliever_count"]].values
    fig.add_trace(go.Scatter(
        x=df["starter_fip_minus"], y=df["reliever_fip_minus"],
        mode="markers+text",
        text=df["team_abbr"],
        textposition="top center",
        textfont=dict(size=11, family=FONT_FAMILY, color="black"),
        marker=dict(size=16, color=marker_colors, line=dict(color="white", width=1)),
        customdata=customdata,
        hovertemplate=(
            "%{customdata[0]}<br>Starters' avg FIP-: %{customdata[1]:.0f} (n=%{customdata[3]:.0f})"
            "<br>Relievers' avg FIP-: %{customdata[2]:.0f} (n=%{customdata[4]:.0f})<extra></extra>"
        ),
        showlegend=False,
    ))

    fig.add_vline(x=100, line_dash="dash", line_color="rgba(150,150,150,0.5)")
    fig.add_hline(y=100, line_dash="dash", line_color="rgba(150,150,150,0.5)")

    fig.update_layout(
        height=650,
        margin=dict(l=70, r=40, t=30, b=70),
        font=dict(family=FONT_FAMILY),
        xaxis=dict(
            title="Starters' avg FIP- (100 = league average)<br>→ better rotation",
            range=x_range[::-1], fixedrange=True,
        ),
        yaxis=dict(
            title="Relievers' avg FIP- (100 = league average)<br>↑ better bullpen",
            range=y_range[::-1], fixedrange=True,
        ),
        dragmode=False,
        hoverlabel=dict(font=dict(family=FONT_FAMILY)),
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


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
                    textfont=dict(size=10, family=FONT_FAMILY),
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
            font=dict(size=12, color="gray", family=FONT_FAMILY),
        )

    ROW_PX = 60
    fig.update_layout(
        height=max(400, ROW_PX * len(y_categories) + 120),
        margin=dict(l=170, r=30, t=30, b=40),
        font=dict(family=FONT_FAMILY),
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
        hoverlabel=dict(font=dict(family=FONT_FAMILY)),
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
    # This iframe is a separate document from the main Streamlit page, so it
    # doesn't have Streamlit's "Source Sans" font file loaded -- fig.font
    # naming the right family isn't enough on its own, the actual font has
    # to be fetched here too, or it silently falls back to a generic
    # sans-serif that merely resembles it.
    font_link_tags = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:ital,wght@0,300..900;1,300..900&display=swap" rel="stylesheet">'
        '<link href="https://fonts.googleapis.com/css2?family=Source+Sans+Pro:ital,wght@0,300;0,400;0,600;0,700;0,900;1,400&display=swap" rel="stylesheet">'
    )
    wrapped_html = f"""
    {font_link_tags}
    <div style="overflow-x:auto; -webkit-overflow-scrolling:touch; font-family:{FONT_FAMILY};">
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

if "view" not in st.session_state:
    st.session_state.view = "total_rv"

btn_col1, btn_col2, btn_col3, btn_col4, _ = st.columns([1, 1, 1, 1, 2])
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
with btn_col3:
    if st.button(
        "Team Performance", use_container_width=True,
        type="primary" if st.session_state.view == "team_performance" else "secondary",
    ):
        st.session_state.view = "team_performance"
        st.rerun()
with btn_col4:
    if st.button(
        "Team Pitching", use_container_width=True,
        type="primary" if st.session_state.view == "team_pitching" else "secondary",
    ):
        st.session_state.view = "team_pitching"
        st.rerun()

if st.session_state.view == "total_rv":
    pos_col, team_col, league_col, _ = st.columns([1, 1, 1, 2])
    with pos_col:
        position_label = st.selectbox("Filter by position", options=list(POSITION_OPTIONS.keys()), index=0)
    with team_col:
        team_options = ["All"] + sorted(ABBR_TO_TEAM_ID.keys())
        team_label = st.selectbox("Filter by team", options=team_options, index=0)
    with league_col:
        league_label = st.selectbox("Filter by league", options=["All", "AL", "NL"], index=0)

    with st.spinner("Pulling live data from Baseball Savant..."):
        leaderboard, debug_info = build_leaderboard(int(year), position_label)

    # Team/league are plain filters on the already-fetched table -- unlike
    # the position filter, neither changes what's requested from Savant, so
    # there's no need to involve build_leaderboard's caching for these.
    if team_label != "All":
        leaderboard = leaderboard[leaderboard["team_id"] == ABBR_TO_TEAM_ID[team_label]]
    if league_label != "All":
        leaderboard = leaderboard[leaderboard["team_id"].map(TEAM_ID_TO_LEAGUE) == league_label]
    # Re-rank 1..N for whatever subset is actually being shown, rather than
    # leaving gaps from the pre-filter rank (e.g. 3, 7, 15...).
    leaderboard = leaderboard.sort_values("total_run_value", ascending=False).reset_index(drop=True)
    leaderboard.index += 1

    st.caption(
        "Live from Baseball Savant. Total Run Value = Batting + Pitching + Fielding + "
        "Baserunning run value, summed per player. Position/team/league are row filters "
        "on *which players* appear -- they don't recompute the RV columns, which stay "
        "each player's full-season totals. \"Pitcher\" includes two-way players (anyone "
        "with a pitching RV entry, not just players with no batting/fielding/baserunning "
        "entry) so it doesn't wrongly exclude them. Pure DH can't be isolated by any of "
        "these filters, since there's no fielding code or pitching record to catch it on."
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

elif st.session_state.view == "starting_pitchers":
    with st.spinner("Pulling live pitcher data from Baseball Savant..."):
        pitchers_data, pitchers_debug = build_pitchers_data(int(year))

    # st.segmented_control renders as one connected pill (rounded outer
    # corners, square divider in the middle) rather than two separate
    # buttons -- visually reads as "an option within Pitchers" instead of
    # a third top-level tab, unlike the Total RV / Pitchers buttons above.
    sub_view = st.segmented_control(
        "Pitcher type",
        options=["Starters", "Relievers"],
        default="Starters",
        label_visibility="collapsed",
        key="pitcher_subview",
    )
    if sub_view is None:  # clicking the active segment again can deselect it
        sub_view = "Starters"

    league_max_gs = pitchers_data["neutral"]["gs"].max()
    min_gs_threshold = math.ceil(MIN_GS_PCT_OF_MAX * league_max_gs) if pd.notna(league_max_gs) and league_max_gs > 0 else 0

    if sub_view == "Starters":
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
        show_gs_in_tooltip=(sub_view == "Starters"),
    )
    if pitcher_chart is not None:
        st.caption("↔ Scroll horizontally to see all teams on smaller screens.")
        render_scrollable_chart(pitcher_chart)
    else:
        st.info("No pitcher data available for this season/selection yet.")

    debug_to_show = pitchers_debug

elif st.session_state.view == "team_performance":
    with st.spinner("Pulling live team data from MLB Stats API..."):
        team_perf_df, team_perf_debug = build_team_performance_data(int(year))

    st.caption(
        "Team-level wRC+ (x-axis) and FIP- (y-axis), computed from MLB's official "
        "Stats API (FanGraphs and Baseball-Reference both prohibit automated/scraped "
        "access to their own versions of these stats). **No park-factor adjustment** "
        "-- FanGraphs' published wRC+/FIP- do adjust for park, so these numbers will "
        "be close but not identical, especially for teams in extreme parks. Fielding "
        "run value (from Baseball Savant) is a third dimension, shown as marker color: "
        "red = above-average fielding, blue = below-average, gray = league average, "
        "scaled relative to the other 29 teams (see the colorbar below the chart). "
        "Wins are a fourth dimension, shown as marker size -- the team's actual season "
        "win total, not a sum of individual pitchers' decisions (see the text legend "
        "next to the colorbar for the size-to-wins range). Full win-loss record is in "
        "the tooltip."
    )
    team_perf_chart = build_team_performance_chart(team_perf_df)
    if team_perf_chart is not None:
        render_scrollable_chart(team_perf_chart)
    else:
        st.info("No team performance data available for this season yet.")

    debug_to_show = team_perf_debug

else:
    st.subheader("Rotation vs. Bullpen")
    with st.spinner("Pulling live per-pitcher data from MLB Stats API..."):
        rotation_bullpen_df, rotation_bullpen_debug = build_rotation_bullpen_fip_data(int(year))

    st.caption(
        "Each team's starters' FIP- (x-axis) vs. its relievers' FIP- (y-axis) -- a "
        "different number from the team-level FIP- on the Team Performance tab. "
        "Pitchers are classified Starter/Reliever first (same rule as the Pitchers "
        "tab: GS at least 10% of the league's highest GS = starter; below that with "
        "16.2+ IP = reliever), then each team+role group's raw FIP components are "
        "summed before computing one FIP- for the group -- mathematically the same "
        "as weighting each pitcher's FIP- by his innings pitched, so a reliever with "
        "just a handful of innings can't swing the number as much as a workhorse's "
        "full season. Pitcher counts behind each number are in the tooltip -- worth "
        "a glance for teams where a role only has a couple of qualifying arms."
    )
    rotation_bullpen_chart = build_rotation_bullpen_chart(rotation_bullpen_df)
    if rotation_bullpen_chart is not None:
        render_scrollable_chart(rotation_bullpen_chart)
    else:
        st.info("No rotation/bullpen data available for this season yet.")

    debug_to_show = rotation_bullpen_debug

with st.expander("🔧 Debug: raw source status & column mapping (check this if numbers look off)"):
    for category, info in debug_to_show.items():
        st.markdown(f"**{category}**")
        st.json(info)
