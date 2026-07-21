import html as html_lib
import re

import pandas as pd
import streamlit as st

import ledger
from config import CURRENT_LEAGUE_ID

st.set_page_config(page_title="Bag n Butthole - Keeper Dashboard", layout="wide", menu_items={})

# Design tokens: a validated light/dark categorical palette + ink/surface
# roles, so every custom component (table, grid, cards) reads consistently
# instead of ad-hoc grays. Categorical hues are assigned to fixed roles
# (never cycled) so the same acquisition type always reads as the same color.
TABLE_STYLE = """
<style>
:root {
  --sd-ink: #0b0b0b;
  --sd-ink-2: #52514e;
  --sd-ink-muted: #898781;
  --sd-border: rgba(11,11,11,0.10);
  --sd-hairline: #e1e0d9;
  --sd-hover: rgba(11,11,11,0.05);
  --sd-header-bg: rgba(11,11,11,0.035);
  --sd-blue: #2a78d6;
  --sd-aqua: #1baf7a;
  --sd-yellow: #eda100;
  --sd-violet: #4a3aa7;
  --sd-magenta: #e87ba4;
  --sd-amber-wash: rgba(237,161,0,0.12);
}
@media (prefers-color-scheme: dark) {
  :root {
    --sd-ink: #ffffff;
    --sd-ink-2: #c3c2b7;
    --sd-ink-muted: #898781;
    --sd-border: rgba(255,255,255,0.14);
    --sd-hairline: #2c2c2a;
    --sd-hover: rgba(255,255,255,0.07);
    --sd-header-bg: rgba(255,255,255,0.05);
    --sd-blue: #3987e5;
    --sd-aqua: #199e70;
    --sd-yellow: #c98500;
    --sd-violet: #9085e9;
    --sd-magenta: #d55181;
    --sd-amber-wash: rgba(201,133,0,0.18);
  }
}
.sd-table, .sd-card, .db-grid { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.sd-table-wrap { overflow-x:auto; border:1px solid var(--sd-border); border-radius:10px; }
.sd-table { width:100%; border-collapse:collapse; font-size:0.88rem; }
.sd-table th { text-align:left; padding:10px 12px; background:var(--sd-header-bg); border-bottom:1px solid var(--sd-border); white-space:nowrap; font-weight:600; font-size:0.74rem; text-transform:uppercase; letter-spacing:0.03em; color:var(--sd-ink-2); }
.sd-table td { padding:8px 12px; border-bottom:1px solid var(--sd-hairline); vertical-align:middle; white-space:nowrap; color:var(--sd-ink); }
.sd-table tbody tr:hover td { background:var(--sd-hover); }
.sd-table tbody tr:last-child td { border-bottom:none; }
.sd-name-link { text-decoration:none; font-weight:600; color:var(--sd-ink); }
.sd-name-link:hover { text-decoration:underline; }
.sd-headshot { width:32px; height:32px; border-radius:50%; object-fit:cover; display:block; border:1px solid var(--sd-border); }
.sd-dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; vertical-align:middle; flex-shrink:0; }
.sd-num { text-align:center; font-variant-numeric:tabular-nums; }
.sd-team-avatar { width:20px; height:20px; border-radius:50%; object-fit:cover; vertical-align:middle; margin-right:6px; border:1px solid var(--sd-border); }
.sd-truncate { max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sd-card { border:1px solid var(--sd-border); border-radius:10px; padding:14px 16px; margin:10px 0 18px 0; background:var(--sd-header-bg); }
.sd-card-title { font-size:0.74rem; font-weight:700; text-transform:uppercase; letter-spacing:0.03em; color:var(--sd-ink-2); margin-bottom:8px; }
.sd-card-row { display:flex; justify-content:space-between; align-items:center; padding:5px 0; font-size:0.9rem; border-bottom:1px solid var(--sd-hairline); gap:12px; }
.sd-card-row:last-child { border-bottom:none; }
.sd-card-label { color:var(--sd-ink-2); }
.sd-card-value { color:var(--sd-ink); font-weight:600; font-variant-numeric:tabular-nums; text-align:right; }
</style>
"""

# Fixed categorical color per acquisition type - never reassigned/cycled, so
# "Drafted" is always blue, "Trade" always violet, etc. across the whole app.
_ACQUISITION_COLORS = {
    "Drafted": "var(--sd-blue)",
    "Kept": "var(--sd-aqua)",
    "Waiver": "var(--sd-yellow)",
    "Trade": "var(--sd-violet)",
    "Tag": "var(--sd-magenta)",
}


def _acquisition_dot(acquisition_type):
    color = _ACQUISITION_COLORS.get(acquisition_type, "var(--sd-ink-muted)")
    return f'<span class="sd-dot" style="background:{color};"></span>'


def _flatten_html(s):
    """st.markdown treats 4+ space indented lines as a literal code block
    (CommonMark), so any multi-line HTML built with normal Python indentation
    silently renders as raw text unless collapsed to single lines first."""
    return re.sub(r"\n\s*", "", s)


def render_info_card(title, rows):
    rows_html = "".join(
        f'<div class="sd-card-row"><span class="sd-card-label">{html_lib.escape(label)}</span>'
        f'<span class="sd-card-value">{value}</span></div>'
        for label, value in rows
    )
    card_html = f"""
    {TABLE_STYLE}
    <div class="sd-card">
      <div class="sd-card-title">{html_lib.escape(title)}</div>
      {rows_html}
    </div>
    """
    st.markdown(_flatten_html(card_html), unsafe_allow_html=True)


def player_sort_columns(prev_season):
    """(header label, df column, default-first-click-ascending, help text) for
    every column, in table order. None as the df column means that header
    isn't sortable (headshot image, or long free-text columns)."""
    season_label = prev_season or "Last season"
    return [
        ("", None, None, ""),
        ("Name", "name", True, "Click a player's name to open their full profile."),
        ("Pos", "position", True, "The position Sleeper lists for this player."),
        ("Team", "nfl_team", True, "The player's real NFL team ('FA' if currently unsigned)."),
        ("Age", "age", True, "Player's current age."),
        ("Fantasy Team", "fantasy_team", True, "Which manager currently rosters this player."),
        ("Acquired", None, None, "The most recent event the current manager used to acquire or retain this player - e.g. drafted, kept, traded for, or picked up off waivers. Open the player's profile for the full transaction history."),
        ("Keeper Status", None, None, "Where this player's keeper value originated: the season/round they were drafted (or 'UDFA'), and who drafted them."),
        ("Keeper Cost", "keeper_value_round", True, "The draft round it costs to keep this player next season."),
        ("Keeper Years Remaining", "years_remaining_keepable", True, "How many more seasons this player can still be kept under the standard tenure rule (3 total seasons for drafted players, 2 for UDFAs) before they reset to the draft pool."),
        ("Tags Remaining", "tags_remaining", True, "Franchise-tag years still available for this player. Depends on their draft round (tag costs R-1 then R-2, so round-1 players get 0, round-2 get 1, round 3+ get 2) - UDFAs are never tag-eligible."),
        ("Total Potential Years", "total_potential_keeper_years", True, "Keeper Years Remaining + Tags Remaining combined - the max total seasons this player could still be retained by any means."),
        (f"{season_label} GP", "prev_season_games_played", False, f"Games played in the {season_label} season."),
        (f"{season_label} PPG", "prev_season_ppg", False, f"Points per game in {season_label}, scored under this league's actual scoring format."),
        (f"{season_label} Finish", "prev_season_position_rank", True, f"{season_label} finish among all players at their position (e.g. 'RB2' = 2nd-best RB)."),
    ]


def _plain_header_html(label, df_key, help_text, current_sort_col, current_sort_ascending):
    title_attr = f' title="{html_lib.escape(help_text)}"' if help_text else ""
    if df_key is None or df_key != current_sort_col:
        return f"<th{title_attr}>{html_lib.escape(label)}</th>"
    arrow = " ▲" if current_sort_ascending else " ▼"
    return f'<th{title_attr} style="color:var(--sd-ink);">{html_lib.escape(label)}{arrow}</th>'


@st.cache_data(ttl=300)
def load_data(league_id):
    return ledger.load_all_data(league_id)


@st.cache_data(ttl=300)
def load_team_visuals(league_id):
    return ledger.get_team_visual_info(load_data(league_id))


def _team_chip(team, team_visuals):
    v = team_visuals.get(team, {})
    avatar = v.get("avatar_url")
    img = f'<img class="sd-team-avatar" src="{avatar}">' if avatar else ""
    return f'{img}<span class="sd-dot" style="background:{v.get("color", "#888888")};"></span>'


def render_player_table(df, team_visuals, prev_season, current_sort_col, current_sort_ascending):
    rows_html = []
    for _, row in df.iterrows():
        headshot = f"https://sleepercdn.com/content/nfl/players/{row['player_id']}.jpg"
        name = html_lib.escape(str(row["name"]))
        team_name = html_lib.escape(str(row["fantasy_team"]))
        tags_remaining = "" if pd.isna(row["tags_remaining"]) else int(row["tags_remaining"])
        gp = "" if pd.isna(row["prev_season_games_played"]) else int(row["prev_season_games_played"])
        ppg = "" if pd.isna(row["prev_season_ppg"]) else f"{row['prev_season_ppg']:.1f}"
        pos_slot = row["prev_season_position_slot"] if pd.notna(row["prev_season_position_slot"]) else ""
        nfl_team = row["nfl_team"] if pd.notna(row["nfl_team"]) else "FA"
        position = row["position"] if pd.notna(row["position"]) else ""
        acquired_full = html_lib.escape(str(row["acquisition_history"]))
        keeper_status_full = html_lib.escape(str(row["keeper_status_summary"]))
        acq_dot = _acquisition_dot(row["acquisition_type"])
        rows_html.append(
            f"""<tr>
            <td><img class="sd-headshot" src="{headshot}" onerror="this.style.visibility='hidden'"></td>
            <td><a class="sd-name-link" href="?player_id={row['player_id']}" target="_self">{name}</a></td>
            <td>{position}</td>
            <td>{nfl_team}</td>
            <td class="sd-num">{row['age'] if pd.notna(row['age']) else ''}</td>
            <td class="sd-truncate" title="{team_name}">{_team_chip(row['fantasy_team'], team_visuals)}{team_name}</td>
            <td class="sd-truncate" title="{acquired_full}">{acq_dot}{acquired_full}</td>
            <td class="sd-truncate" title="{keeper_status_full}">{keeper_status_full}</td>
            <td class="sd-num">R{row['keeper_value_round']}</td>
            <td class="sd-num">{row['years_remaining_keepable']}</td>
            <td class="sd-num">{tags_remaining}</td>
            <td class="sd-num">{row['total_potential_keeper_years']}</td>
            <td class="sd-num">{gp}</td>
            <td class="sd-num">{ppg}</td>
            <td class="sd-num">{pos_slot}</td>
            </tr>"""
        )

    header_cells = "".join(
        _plain_header_html(label, df_key, help_text, current_sort_col, current_sort_ascending)
        for label, df_key, _, help_text in player_sort_columns(prev_season)
    )

    table_html = f"""
    {TABLE_STYLE}
    <div class="sd-table-wrap">
    <table class="sd-table">
      <thead><tr>{header_cells}</tr></thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    </div>
    """
    st.markdown(_flatten_html(table_html), unsafe_allow_html=True)


def render_pick_board_table(df, team_visuals):
    """Fallback flat list, used only when a season has no set draft order yet."""
    rows_html = []
    for _, row in df.sort_values(["round", "owned_by"]).iterrows():
        pick_label = row["pick_display"] if pd.notna(row["pick_display"]) else "Undetermined"
        team_name = html_lib.escape(str(row["owned_by"]))
        rows_html.append(
            f"""<tr>
            <td>{pick_label}</td>
            <td>{row['round']}</td>
            <td>{_team_chip(row['owned_by'], team_visuals)}{team_name}</td>
            <td>{html_lib.escape(str(row['source']))}</td>
            </tr>"""
        )

    table_html = f"""
    {TABLE_STYLE}
    <div class="sd-table-wrap">
    <table class="sd-table">
      <thead><tr><th>Pick</th><th>Round</th><th>Owned By</th><th>Source</th></tr></thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    </div>
    """
    st.markdown(_flatten_html(table_html), unsafe_allow_html=True)


def render_draft_board_grid(board_df, pick_txn_df, team_visuals, highlight_team=None):
    """A squared, N-column draft board. Columns are plain pick-position labels
    (not team identities) since a manual mid-draft snake conversion would make
    a fixed column->team header wrong for later rounds. Each cell shows the
    current owner's avatar and, if traded, the full custody chain. When
    highlight_team is set, every cell not currently owned by that team is
    blacked out so only their picks stand out."""
    if board_df["pick_display"].isna().all():
        st.caption("No draft order set yet for this season — showing a simple list instead.")
        render_pick_board_table(board_df, team_visuals)
        return

    season = str(board_df["season"].iloc[0])
    first_round = board_df["round"].min()
    columns = board_df[board_df["round"] == first_round].sort_values("pick_in_round")["original_team"].tolist()
    rounds = sorted(board_df["round"].unique())

    def team_chip(team, avatar_size=22):
        v = team_visuals.get(team, {})
        avatar = v.get("avatar_url")
        img = f'<img src="{avatar}" style="width:{avatar_size}px;height:{avatar_size}px;border-radius:50%;object-fit:cover;margin-right:6px;border:1px solid var(--sd-border);">' if avatar else ""
        return f'<div style="display:flex;align-items:center;">{img}<span style="font-size:0.72rem;color:var(--sd-ink);">{html_lib.escape(str(team))}</span></div>'

    header_cells = ['<div class="db-cell db-corner"></div>']
    for i in range(1, len(columns) + 1):
        header_cells.append(f'<div class="db-cell db-header">Pick {i}</div>')

    body_cells = []
    for rnd in rounds:
        body_cells.append(f'<div class="db-cell db-round-label">R{rnd}</div>')
        for team in columns:
            cell_rows = board_df[(board_df["round"] == rnd) & (board_df["original_team"] == team)]
            if cell_rows.empty:
                body_cells.append('<div class="db-cell db-empty"></div>')
                continue
            r = cell_rows.iloc[0]

            if highlight_team and r["owned_by"] != highlight_team:
                body_cells.append('<div class="db-cell db-blackout"></div>')
                continue

            chain = ledger.get_pick_chain(pick_txn_df, season, int(rnd), team)
            chain_html = ""
            if len(chain) > 1:
                arrow_chain = " → ".join(html_lib.escape(str(c)) for c in chain)
                chain_html = f'<div class="db-chain">{arrow_chain}</div>'
            cell_class = "db-cell db-traded" if r["traded"] else "db-cell"
            body_cells.append(f'<div class="{cell_class}">{team_chip(r["owned_by"])}{chain_html}</div>')

    grid_html = f"""
    {TABLE_STYLE}
    <style>
    .db-grid {{ display:grid; grid-template-columns: 46px repeat({len(columns)}, minmax(96px,1fr)); gap:6px; margin-top:10px; }}
    .db-cell {{ border:1px solid var(--sd-border); border-radius:8px; padding:7px 8px;
      min-height:52px; display:flex; flex-direction:column; justify-content:center; background:var(--sd-header-bg); }}
    .db-header {{ font-weight:700; font-size:0.72rem; justify-content:center; align-items:center;
      color:var(--sd-ink-muted); text-transform:uppercase; letter-spacing:0.03em; background:transparent; border:none; }}
    .db-round-label {{ border:none; background:transparent; font-weight:700; align-items:center; font-size:0.8rem; color:var(--sd-ink-2); }}
    .db-corner {{ border:none; background:transparent; }}
    .db-traded {{ background:var(--sd-amber-wash); }}
    .db-chain {{ font-size:0.65rem; opacity:0.8; margin-top:3px; color:var(--sd-ink-2); }}
    .db-empty {{ opacity:0.25; background:transparent; }}
    .db-blackout {{ background:rgba(20,20,20,0.92); border-color:rgba(20,20,20,0.92); }}
    </style>
    <div class="sd-table-wrap">
    <div class="db-grid">{''.join(header_cells)}{''.join(body_cells)}</div>
    </div>
    """
    st.markdown(_flatten_html(grid_html), unsafe_allow_html=True)


def render_player_detail(data, row):
    headshot_url = f"https://sleepercdn.com/content/nfl/players/{row['player_id']}.jpg"
    nfl_team = row["nfl_team"] if pd.notna(row["nfl_team"]) else None
    logo_badge = ""
    if nfl_team:
        logo_url = f"https://sleepercdn.com/images/team_logos/nfl/{nfl_team.lower()}.png"
        logo_badge = (
            f'<img src="{logo_url}" style="position:absolute;bottom:-6px;right:-6px;'
            f'width:58px;height:58px;border-radius:50%;border:4px solid white;background:white;">'
        )

    col_img, col_info = st.columns([1, 3])
    with col_img:
        composite_html = f"""<div style="position:relative;width:180px;height:180px;">
        <img src="{headshot_url}" style="width:180px;height:180px;border-radius:50%;object-fit:cover;border:4px solid rgba(128,128,128,0.25);">
        {logo_badge}
        </div>"""
        st.markdown(_flatten_html(composite_html), unsafe_allow_html=True)

    with col_info:
        st.subheader(f"{row['name']} — {row['position']} ({nfl_team or 'FA'})")
        st.write(f"**Age:** {row['age']}  |  **Fantasy team:** {row['fantasy_team']}")
        acq_dot = _acquisition_dot(row["acquisition_type"])
        st.markdown(
            _flatten_html(f"{TABLE_STYLE}<div>{acq_dot}<strong>Acquired:</strong> {html_lib.escape(str(row['acquisition_history']))}</div>"),
            unsafe_allow_html=True,
        )

    render_info_card(
        "Keeper Metrics",
        [
            ("Status", html_lib.escape(str(row["keeper_status_summary"]))),
            ("Keeper Years Remaining", row["years_remaining_keepable"]),
            ("Tags Remaining", row["tags_remaining"]),
            ("Total Potential Years", row["total_potential_keeper_years"]),
        ],
    )

    if pd.notna(row["prev_season_points"]):
        gp_val = int(row["prev_season_games_played"]) if pd.notna(row["prev_season_games_played"]) else "—"
        ppg_val = f"{row['prev_season_ppg']:.1f}" if pd.notna(row["prev_season_ppg"]) else "—"
        finish_val = f"#{int(row['prev_season_finish'])}" if pd.notna(row["prev_season_finish"]) else "—"
        pos_val = f"#{int(row['prev_season_position_rank'])}" if pd.notna(row["prev_season_position_rank"]) else "—"
        render_info_card(
            f"{row['prev_season']} Player Performance",
            [
                ("Points", f"{row['prev_season_points']:.1f}"),
                ("PPG", ppg_val),
                ("Games Played", gp_val),
                ("Overall Rank", finish_val),
                ("Position Rank", pos_val),
            ],
        )

    st.markdown("**Transaction history** _(includes keeper picks once any exist, most recent first)_")
    txn_hist = ledger.get_player_transaction_history(data, row["player_id"])
    if txn_hist:
        st.dataframe(pd.DataFrame(txn_hist)[["date", "type", "action", "team"]], hide_index=True, width="stretch")
    else:
        st.caption("No trade/waiver/free-agent/keeper moves on record.")


def _clear_player_query():
    if "player_id" in st.query_params:
        del st.query_params["player_id"]


@st.dialog("Player Detail", width="large", on_dismiss=_clear_player_query)
def show_player_dialog(data, row):
    render_player_detail(data, row)


def main():
    st.markdown(
        """<style>
        [data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none; }
        #MainMenu, footer { visibility: hidden; }
        </style>""",
        unsafe_allow_html=True,
    )

    title_col, refresh_col = st.columns([5, 1])
    with title_col:
        st.title("🏈 Bag n Butthole — Keeper Dashboard")
    with refresh_col:
        st.write("")
        if st.button("Refresh from Sleeper"):
            load_data.clear()
            load_team_visuals.clear()

    data = load_data(CURRENT_LEAGUE_ID)
    overrides = ledger.load_overrides()
    player_df, unmatched_overrides = ledger.build_player_ledger(data, overrides)
    pick_df = ledger.build_draft_pick_ledger(data)
    pick_txn_df = ledger.get_pick_transaction_log(data)
    team_visuals = load_team_visuals(CURRENT_LEAGUE_ID)

    current_league = data["league_chain"][-1]
    st.caption(
        f"**League:** {current_league['name']}  |  "
        f"**Season:** {current_league['season']} ({current_league['status']})  |  "
        f"**Seasons of history loaded:** {len(data['league_chain'])}"
    )

    if unmatched_overrides:
        st.warning(
            "overrides.yaml has names that don't match any current player:\n"
            + "\n".join(f"- {n}" for n in unmatched_overrides)
        )

    tab_players, tab_picks, tab_rules = st.tabs(["Player / Keeper Ledger", "Draft Picks", "League Rules"])

    with tab_players:
        st.subheader("Player / Keeper Ledger")

        with st.expander("What do these columns mean?"):
            for label, _, _, help_text in player_sort_columns(data.get("prev_season")):
                if label and help_text:
                    st.markdown(f"**{label}** — {help_text}")

        f1, f2, f3, f4, f5, f6, f7 = st.columns(7)
        with f1:
            team_filter = st.multiselect("Team", sorted(player_df["fantasy_team"].unique()))
        with f2:
            pos_filter = st.multiselect("Position", sorted(player_df["position"].dropna().unique()))
        with f3:
            nfl_filter = st.multiselect("NFL Team", sorted(player_df["nfl_team"].dropna().unique()))
        with f4:
            keeper_cost_filter = st.multiselect("Keeper Cost (Round)", sorted(player_df["keeper_value_round"].unique()))
        with f5:
            years_remaining_filter = st.multiselect(
                "Keeper Years Remaining", sorted(player_df["years_remaining_keepable"].unique())
            )
        with f6:
            tags_remaining_filter = st.multiselect(
                "Tags Remaining", sorted(player_df["tags_remaining"].unique())
            )
        with f7:
            total_potential_filter = st.multiselect(
                "Total Potential Years", sorted(player_df["total_potential_keeper_years"].unique())
            )

        filtered = player_df.copy()
        if team_filter:
            filtered = filtered[filtered["fantasy_team"].isin(team_filter)]
        if pos_filter:
            filtered = filtered[filtered["position"].isin(pos_filter)]
        if nfl_filter:
            filtered = filtered[filtered["nfl_team"].isin(nfl_filter)]
        if keeper_cost_filter:
            filtered = filtered[filtered["keeper_value_round"].isin(keeper_cost_filter)]
        if years_remaining_filter:
            filtered = filtered[filtered["years_remaining_keepable"].isin(years_remaining_filter)]
        if tags_remaining_filter:
            filtered = filtered[filtered["tags_remaining"].isin(tags_remaining_filter)]
        if total_potential_filter:
            filtered = filtered[filtered["total_potential_keeper_years"].isin(total_potential_filter)]

        sort_lookup = {label: (df_key, default_asc) for label, df_key, default_asc, _ in player_sort_columns(data.get("prev_season")) if df_key}
        sort_choice = st.pills("Sort by", list(sort_lookup.keys()), selection_mode="single")
        reverse = st.checkbox("Reverse")

        if sort_choice:
            sort_key, default_asc = sort_lookup[sort_choice]
            sort_ascending = (not default_asc) if reverse else default_asc
            display_df = filtered.sort_values(sort_key, ascending=sort_ascending, na_position="last").reset_index(drop=True)
        else:
            sort_key, sort_ascending = None, True
            display_df = filtered.sort_values(["fantasy_team", "keeper_value_round"]).reset_index(drop=True)

        st.caption("Click a player's name to see their headshot, keeper metrics, and transaction history.")
        render_player_table(display_df, team_visuals, data.get("prev_season"), sort_key, sort_ascending)

        pid = st.query_params.get("player_id")
        if pid:
            matches = player_df[player_df["player_id"] == pid]
            if not matches.empty:
                show_player_dialog(data, matches.iloc[0])

    with tab_picks:
        st.subheader("Draft Pick Inventory")

        season_filter = st.selectbox("Season", sorted(pick_df["season"].unique()))
        season_picks = pick_df[pick_df["season"] == season_filter]

        all_pick_teams = sorted(season_picks["current_owner_team"].unique())
        highlight_choice = st.selectbox("Highlight team (blacks out everyone else's picks)", ["(All teams)"] + all_pick_teams)
        highlight_team = None if highlight_choice == "(All teams)" else highlight_choice

        st.markdown("**Full pick board (draft order)**")
        board = season_picks.copy()
        board["source"] = board.apply(
            lambda r: "Own pick" if not r["traded"] else f"Via trade, from {r['original_team']}", axis=1
        )
        board = board.rename(columns={"current_owner_team": "owned_by"})
        render_draft_board_grid(board, pick_txn_df, team_visuals, highlight_team)

        st.markdown("**Pick transactions** _(trade history, most recent first)_")

        txn_filtered = pick_txn_df[pick_txn_df["season"] == season_filter] if not pick_txn_df.empty else pick_txn_df
        if highlight_team and not txn_filtered.empty:
            txn_filtered = txn_filtered[
                txn_filtered["from_team"].eq(highlight_team) | txn_filtered["to_team"].eq(highlight_team)
            ]

        if txn_filtered.empty:
            st.caption("No pick trades on record for this season.")
        else:
            st.dataframe(
                txn_filtered[["date", "season", "round", "from_team", "to_team", "original_team"]],
                width="stretch",
                hide_index=True,
            )

    with tab_rules:
        st.subheader("Quick Reference")
        st.markdown(
            """
- **Max keepers:** 5 players per team annually
- **Standard tenure:** drafted players — 2 years after being drafted (3 total years)
- **UDFA tenure:** 2 total years on roster (1 additional year beyond pickup), keeper value = round 10
- **Standard keeper penalty:** forfeit a pick equal to the round they were last drafted in
- **Two same-round-value keepers:** 1st player = standard penalty; 2nd player = forfeit R+1 AND R+2 (max 1 player/team eligible for this)
- **Franchise tag:** eligible after 2 kept years; Year 1 cost = R-1, Year 2 cost = R-2 of current draft value; max 2 tags per team; not available for UDFA
- **Trade deadline:** Week 11
- **Waiver processing:** Wednesdays 12:00 AM PDT, FAAB $1000/team, tradeable
- **Keeper declaration deadline:** 1 month before NFL season opener
            """
        )


if __name__ == "__main__":
    main()
