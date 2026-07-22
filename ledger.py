"""
Builds the derived keeper/draft-pick ledger from raw Sleeper data.

Key rule assumptions (per league rules doc), specific to this being the
league's first keeper-eligible offseason:
  - A player's "keeper value" round comes from the most recent season in
    which they were drafted fresh (is_keeper is not True). Consecutive
    seasons after that where is_keeper is True count as years already kept.
  - A player never found in any season's draft picks is a UDFA pickup.
    UDFA keeper value is fixed at round 10 (per rules), regardless of when
    picked up.
  - Retention rule: a player who ended a season on ANY roster keeps their
    draft value and years-kept count, no matter how many times they were
    traded/dropped/re-added along the way. A player who ended the season
    unclaimed resets and simply won't appear on any current roster, so no
    special handling is needed for that case.
  - Standard tenure caps: 3 total seasons on a roster for a drafted player
    (draft season + up to 2 kept seasons), 2 total seasons for a UDFA
    (pickup season + up to 1 kept season). Franchise tags extend this but
    are tracked manually via overrides.yaml since Sleeper has no concept
    of them.

Known limitation: if a UDFA sits on a roster across multiple seasons
without ever being run through a draft as a keeper, their acquisition
season can't be recovered from Sleeper data alone (no transaction history
is consulted). We fall back to "most recently completed season," which is
correct today (only one prior season exists) but should be re-checked once
the league has multiple years of history. Such rows are flagged via
acquisition_season_estimated.
"""

from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests
import yaml
from PIL import Image

import sleeper_api

OVERRIDES_PATH = Path(__file__).parent / "overrides.yaml"


def load_overrides(path=OVERRIDES_PATH):
    path = Path(path)
    if not path.exists():
        return {"players": {}, "picks": []}
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("players", {})
    data.setdefault("picks", [])
    return data


def load_all_data(current_league_id):
    league_chain = sleeper_api.get_league_chain(current_league_id)

    picks_by_season = {}
    drafts_by_season = {}
    for league in league_chain:
        draft_id = league.get("draft_id")
        if not draft_id:
            continue
        picks = sleeper_api.get_draft_picks(draft_id)
        if picks:
            picks_by_season[league["season"]] = picks
        drafts_by_season[league["season"]] = sleeper_api.get_draft(draft_id)

    current_league = league_chain[-1]
    rosters = sleeper_api.get_rosters(current_league["league_id"])
    users = sleeper_api.get_users(current_league["league_id"])
    traded_picks = sleeper_api.get_traded_picks(current_league["league_id"])
    players = sleeper_api.get_all_players()
    current_draft = drafts_by_season.get(current_league["season"])

    # Transactions serve two purposes: explaining how a player landed on their
    # current roster, and (critically) recovering real draft-pick trades that a
    # manual snake reassignment overwrote in traded_picks. A future-season pick
    # can be traded seasons in advance, so scan every league in the chain rather
    # than just the last couple - otherwise an old pick trade would silently
    # drop out of the snake-zone reconstruction once it aged past the window.
    # Results are cached, and older adds never override newer ones in the
    # player-acquisition logic, so the wider scan is safe there too.
    transactions = []
    for league in league_chain:
        txns = sleeper_api.get_all_transactions(league["league_id"])
        for t in txns:
            t["season"] = league["season"]
        transactions.extend(txns)

    # Previous season's performance, for the player ledger / player detail
    # view. One bulk call covers every NFL player instead of one call each.
    prev_season_stats = {}
    prev_season = None
    prev_season_flavor = "half_ppr"
    completed_leagues = league_chain[:-1]
    if completed_leagues:
        prev_league = completed_leagues[-1]
        prev_season = prev_league["season"]
        rec_pts = (prev_league.get("scoring_settings") or {}).get("rec", 0)
        prev_season_flavor = "ppr" if rec_pts >= 1 else "half_ppr" if rec_pts > 0 else "std"
        for entry in sleeper_api.get_all_season_stats(prev_season):
            pid = entry.get("player_id")
            if pid:
                prev_season_stats[pid] = entry.get("stats") or {}

    return {
        "league_chain": league_chain,
        "picks_by_season": picks_by_season,
        "drafts_by_season": drafts_by_season,
        "rosters": rosters,
        "users": users,
        "traded_picks": traded_picks,
        "current_draft": current_draft,
        "players": players,
        "transactions": transactions,
        "prev_season": prev_season,
        "prev_season_flavor": prev_season_flavor,
        "prev_season_stats": prev_season_stats,
    }


def _team_labels(rosters, users):
    user_by_id = {u["user_id"]: u for u in users}
    labels = {}
    for r in rosters:
        owner = user_by_id.get(r.get("owner_id"), {})
        team_name = (owner.get("metadata") or {}).get("team_name")
        labels[r["roster_id"]] = team_name or owner.get("display_name") or f"Roster {r['roster_id']}"
    return labels


def _pick_history_by_player(picks_by_season):
    history = {}
    for season in sorted(picks_by_season, key=int):
        for pick in picks_by_season[season]:
            pid = pick.get("player_id")
            if not pid:
                continue
            history.setdefault(pid, []).append(
                {
                    "season": season,
                    "round": pick["round"],
                    "pick_no": pick.get("pick_no"),
                    "is_keeper": pick.get("is_keeper"),
                    "roster_id": pick.get("roster_id"),
                }
            )
    return history


_TXN_LABELS = {
    "trade": "Trade",
    "waiver": "Waiver",
    "free_agent": "Waiver",  # same bucket as waiver claims - both are "picked up off the wire"
    "commissioner": "Commissioner Move",
}


def _latest_add_by_player_roster(transactions):
    """(player_id, roster_id) -> {'method', 'created'} for the most recent add landing them there."""
    latest = {}
    for t in transactions:
        adds = t.get("adds") or {}
        created = t.get("created") or 0
        for pid, rid in adds.items():
            key = (pid, rid)
            if key not in latest or created > latest[key]["created"]:
                latest[key] = {"method": _TXN_LABELS.get(t.get("type"), t.get("type")), "created": created}
    return latest


def _origin_and_years_kept(entries):
    """entries sorted by season ascending. Returns (origin_entry, years_kept_after_origin)."""
    origin_idx = 0
    for i in range(len(entries) - 1, -1, -1):
        if entries[i]["is_keeper"] is not True:
            origin_idx = i
            break
    origin = entries[origin_idx]
    years_kept_after = len(entries) - 1 - origin_idx
    return origin, years_kept_after


def _season_team_count(data):
    return {lg["season"]: lg.get("total_rosters") or len(data["rosters"]) for lg in data["league_chain"]}


def _format_pick(season, round_, pick_no, season_team_count):
    """'3.11' style round.pick-within-round, using overall pick_no since that's
    always sequential (unlike draft_slot, which flips on snake-draft rounds)."""
    if not pick_no:
        return f"Rd {round_}"
    num_teams = season_team_count.get(season) or 10
    pick_in_round = ((pick_no - 1) % num_teams) + 1
    return f"{round_}.{pick_in_round:02d}"


# Per-season snake conversion. Sleeper can't run a linear-then-snake draft, so
# the commissioner sets the draft to "linear" and manually reassigns pick
# custody to fake a snake after a given round (the last pick of that round picks
# again first in the next round). Map each affected season to its last linear
# round. A season not listed here is treated exactly as Sleeper reports it (no
# conversion, position == slot every round). This is intentionally hardcoded
# per season: if a future year changes the format, add or edit its entry here -
# the convention is never assumed to carry forward on its own. Sleeper's
# draft_type flag always says "linear" and never reflects the manual snake, so
# pick placement must apply this rule itself rather than trusting draft_type.
SNAKE_AFTER_ROUND_BY_SEASON = {
    "2026": 6,
}


def _snake_after_round(season):
    return SNAKE_AFTER_ROUND_BY_SEASON.get(str(season))


def _effective_pick_in_round(slot, round_, num_teams, snake_after_round):
    """Physical pick position of a draft slot in a given round under this
    league's snake convention. snake_after_round is None for seasons with no
    conversion (pure linear - position always equals slot)."""
    if snake_after_round is None or round_ <= snake_after_round:
        return slot
    rounds_after_pivot = round_ - snake_after_round
    if rounds_after_pivot % 2 == 1:
        return num_teams - slot + 1
    return slot


def build_player_ledger(data, overrides=None):
    overrides = overrides or {"players": {}}
    players_meta = data["players"]
    rosters = data["rosters"]
    team_labels = _team_labels(rosters, data["users"])
    pick_history = _pick_history_by_player(data["picks_by_season"])
    latest_add = _latest_add_by_player_roster(data.get("transactions", []))

    completed_seasons = [lg["season"] for lg in data["league_chain"][:-1]]
    most_recent_completed_season = completed_seasons[-1] if completed_seasons else None
    season_team_count = {lg["season"]: lg.get("total_rosters") or len(rosters) for lg in data["league_chain"]}

    prev_season = data.get("prev_season")
    prev_season_flavor = data.get("prev_season_flavor", "half_ppr")
    prev_season_stats = data.get("prev_season_stats", {})

    rows = []
    for roster in rosters:
        roster_id = roster["roster_id"]
        team_label = team_labels[roster_id]
        for pid in roster.get("players") or []:
            meta = players_meta.get(pid, {})
            name = meta.get("full_name") or f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip() or pid
            position = meta.get("position")
            nfl_team = meta.get("team")
            age = meta.get("age")

            pstats = prev_season_stats.get(pid) or {}
            prev_gp = pstats.get("gp")
            prev_pts = pstats.get(f"pts_{prev_season_flavor}")
            prev_ppg = round(prev_pts / prev_gp, 1) if prev_pts is not None and prev_gp else None
            prev_finish = pstats.get(f"rank_{prev_season_flavor}")
            prev_pos_rank = pstats.get(f"pos_rank_{prev_season_flavor}")
            prev_position_slot = f"{position}{int(prev_pos_rank)}" if position and prev_pos_rank else None

            entries = pick_history.get(pid)
            if entries:
                origin, years_kept_after = _origin_and_years_kept(entries)
                draft_status = "drafted"
                keeper_clock_start_season = origin["season"]
                keeper_clock_start_estimated = False
                keeper_value_round = origin["round"]
                origin_pick_display = _format_pick(origin["season"], origin["round"], origin.get("pick_no"), season_team_count)
                origin_team = team_labels.get(origin.get("roster_id"), origin.get("roster_id"))
                keeper_status_summary = f"Drafted in {keeper_clock_start_season} ({origin_pick_display} by {origin_team})"
            else:
                origin = None
                draft_status = "udfa"
                keeper_clock_start_season = most_recent_completed_season
                keeper_clock_start_estimated = True
                keeper_value_round = 10
                years_kept_after = 0
                keeper_status_summary = f"UDFA in {keeper_clock_start_season}" if keeper_clock_start_season else "UDFA"

            seasons_rostered = 1 + years_kept_after
            max_tenure_years = 2 if draft_status == "udfa" else 3
            years_remaining_keepable = max(max_tenure_years - seasons_rostered, 0)
            franchise_tag_eligible = draft_status == "drafted" and years_remaining_keepable == 0

            override_entry = overrides.get("players", {}).get(name, {})
            franchise_tag_years_used = int(override_entry.get("franchise_tag_years_used", 0))

            if franchise_tag_years_used > 0:
                # A franchise tag is a manual house-rule designation Sleeper has
                # no record of, so it overrides whatever the raw draft/transaction
                # history would otherwise say - the tag is why they're rostered.
                acquisition_type = "Tag"
            else:
                txn = latest_add.get((pid, roster_id))
                if txn:
                    acquisition_type = txn["method"]
                elif entries and entries[-1].get("roster_id") == roster_id:
                    # Last time they appeared on a draft board it was under this
                    # roster, and nothing has moved them since. Whether that
                    # counts as "Kept" depends on whether that draft-board slot
                    # was an actual keeper pick, or just a fresh draft pick.
                    acquisition_type = "Kept" if entries[-1]["is_keeper"] is True else "Drafted"
                else:
                    acquisition_type = "Unknown"

            # Tag cost is R-1 (year 1) then R-2 (year 2) of the player's draft
            # round, so a round-1 player has no valid year-1 round to pay
            # (round 0 doesn't exist) and gets 0 tag-years; a round-2 player
            # can only afford year 1 (round 1), not year 2 (round 0), so gets
            # 1; round 3+ gets the full 2. UDFAs are never tag-eligible.
            max_tag_years = 0 if draft_status == "udfa" else min(max(keeper_value_round - 1, 0), 2)
            tags_remaining = max(max_tag_years - franchise_tag_years_used, 0)

            chain = get_player_acquisition_chain(data, pid, franchise_tag_years_used)
            acquisition_history = ", ".join(chain) if chain else "Unknown"

            rows.append(
                {
                    "player_id": pid,
                    "name": name,
                    "position": position,
                    "nfl_team": nfl_team,
                    "age": age,
                    "prev_season": prev_season,
                    "prev_season_points": prev_pts,
                    "prev_season_games_played": prev_gp,
                    "prev_season_ppg": prev_ppg,
                    "prev_season_finish": prev_finish,
                    "prev_season_position_rank": int(prev_pos_rank) if prev_pos_rank else None,
                    "prev_season_position_slot": prev_position_slot,
                    "fantasy_team": team_label,
                    "roster_id": roster_id,
                    "acquisition_type": acquisition_type,
                    "acquisition_history": acquisition_history,
                    "draft_status": draft_status,
                    "keeper_status_summary": keeper_status_summary,
                    "keeper_clock_start_season": keeper_clock_start_season,
                    "keeper_clock_start_estimated": keeper_clock_start_estimated,
                    "keeper_value_round": keeper_value_round,
                    "years_kept_so_far": years_kept_after,
                    "seasons_rostered": seasons_rostered,
                    "max_tenure_years": max_tenure_years,
                    "years_remaining_keepable": years_remaining_keepable,
                    "franchise_tag_eligible": franchise_tag_eligible,
                    "tags_remaining": tags_remaining,
                    "total_potential_keeper_years": years_remaining_keepable + tags_remaining,
                }
            )

    df = pd.DataFrame(rows)
    matched_names = set(df["name"]) if not df.empty else set()
    unmatched_overrides = sorted(set(overrides.get("players", {})) - matched_names)
    return df, unmatched_overrides


def build_draft_pick_ledger(data, overrides=None, rounds_per_season=14, future_seasons=2):
    overrides = overrides or {"picks": []}
    rosters = data["rosters"]
    team_labels = _team_labels(rosters, data["users"])
    label_to_roster_id = {label: rid for rid, label in team_labels.items()}
    roster_ids = [r["roster_id"] for r in rosters]
    current_season = int(data["league_chain"][-1]["season"])

    # The current season's draft slot order is set before the draft happens
    # (draft_order/slot_to_roster_id), so we can show real pick numbers for
    # it even pre-draft. Future seasons beyond that have no order yet.
    current_draft = data.get("current_draft") or {}
    slot_to_roster = {int(slot): rid for slot, rid in (current_draft.get("slot_to_roster_id") or {}).items()}
    roster_to_slot = {rid: slot for slot, rid in slot_to_roster.items()}
    num_teams = len(roster_ids) or 10
    ordered_season = str(current_season) if roster_to_slot else None

    seasons_to_show = [str(current_season + i) for i in range(future_seasons + 1)]

    picks = {}
    for season in seasons_to_show:
        for rid in roster_ids:
            for rnd in range(1, rounds_per_season + 1):
                picks[(season, rnd, rid)] = {
                    "season": season,
                    "round": rnd,
                    "original_roster_id": rid,
                    "current_roster_id": rid,
                }

    # For the current season, rounds after the snake pivot are a special
    # case: this league manually reassigns pick custody round-by-round so a
    # structurally "linear" Sleeper draft behaves like a snake at the table.
    # That custody field is being used for live-draft turn coordination, not
    # keeper accounting - it is NOT a reliable source of "who should be
    # credited with this pick." So for that zone, traded_picks is ignored
    # entirely, and ownership is instead computed purely from identity (the
    # pick stays with its own team) modified only by real trades pulled
    # straight from the permanent transaction log, which never gets
    # overwritten by reassignments the way traded_picks does. Rounds at or
    # before the pivot, and every other season, still use traded_picks
    # directly - there's no conflict there.
    def _in_snake_zone(season, round_):
        sar = _snake_after_round(season)
        return season == ordered_season and sar is not None and round_ > sar

    for tp in data["traded_picks"]:
        if _in_snake_zone(tp["season"], tp["round"]):
            continue
        key = (tp["season"], tp["round"], tp["roster_id"])
        if key in picks:
            picks[key]["current_roster_id"] = tp["owner_id"]
        else:
            picks[key] = {
                "season": tp["season"],
                "round": tp["round"],
                "original_roster_id": tp["roster_id"],
                "current_roster_id": tp["owner_id"],
            }

    real_trades = sorted(
        (t for t in data.get("transactions", []) if t.get("type") == "trade"),
        key=lambda t: t.get("created") or 0,
    )
    for t in real_trades:
        for dp in t.get("draft_picks") or []:
            if not _in_snake_zone(dp["season"], dp["round"]):
                continue
            key = (dp["season"], dp["round"], dp["roster_id"])
            if key in picks:
                picks[key]["current_roster_id"] = dp["owner_id"]
            else:
                picks[key] = {
                    "season": dp["season"],
                    "round": dp["round"],
                    "original_roster_id": dp["roster_id"],
                    "current_roster_id": dp["owner_id"],
                }

    # Manual overrides remain available as a last-resort escape hatch for
    # anything the automatic logic above doesn't anticipate, but shouldn't be
    # needed for the ordinary snake-zone trade case anymore.
    unmatched_pick_overrides = []
    for override in overrides.get("picks", []):
        original_rid = label_to_roster_id.get(override.get("original_team"))
        owner_rid = label_to_roster_id.get(override.get("owner_team"))
        if original_rid is None or owner_rid is None:
            unmatched_pick_overrides.append(override)
            continue
        key = (str(override["season"]), int(override["round"]), original_rid)
        if key in picks:
            picks[key]["current_roster_id"] = owner_rid
        else:
            picks[key] = {
                "season": key[0],
                "round": key[1],
                "original_roster_id": original_rid,
                "current_roster_id": owner_rid,
            }

    rows = []
    for p in picks.values():
        pick_in_round = None
        if p["season"] == ordered_season and p["original_roster_id"] in roster_to_slot:
            pick_in_round = _effective_pick_in_round(
                roster_to_slot[p["original_roster_id"]], p["round"], num_teams,
                _snake_after_round(p["season"]),
            )

        rows.append(
            {
                "season": p["season"],
                "round": p["round"],
                "pick_in_round": pick_in_round,
                "pick_display": f"{p['round']}.{pick_in_round:02d}" if pick_in_round else None,
                "original_team": team_labels.get(p["original_roster_id"], p["original_roster_id"]),
                "current_owner_team": team_labels.get(p["current_roster_id"], p["current_roster_id"]),
                "traded": p["original_roster_id"] != p["current_roster_id"],
            }
        )

    df = (
        pd.DataFrame(rows)
        .sort_values(["season", "round", "pick_in_round", "current_owner_team"])
        .reset_index(drop=True)
    )
    return df, unmatched_pick_overrides


def reconcile_board_against_sleeper(data, pick_df):
    """Live guardrail: independently derive Sleeper's real draft-board owner for
    every cell of the current (ordered) season straight from traded_picks +
    slot_to_roster_id, and compare it to our reconstructed board. Returns a list
    of human-readable mismatch strings - empty means the reconstruction exactly
    matches what Sleeper will actually draft.

    The reconstruction (identity + snake display + trade-log recovery) and this
    check (Sleeper's own live custody) come from fully independent sources, so a
    match is strong evidence both are right. A mismatch means the board drifted
    from reality - e.g. a pick reassigned outside a normal Sleeper trade, or a
    pick trade that fell outside the loaded transaction history - and should be
    investigated before the board is trusted, rather than shown silently wrong.

    Sleeper runs a structurally linear draft, so a pick physically sits in its
    original roster's slot column; traded_picks (which folds in both the manual
    snake reassignment and any trades the commissioner mirrored back into it)
    gives that cell's true owner. Our board places the same pick at its snake
    position, but the owner reading down column c must still agree cell-for-cell,
    which is exactly what this compares."""
    team_labels = _team_labels(data["rosters"], data["users"])
    draft = data.get("current_draft") or {}
    slot_to_roster = {int(s): rid for s, rid in (draft.get("slot_to_roster_id") or {}).items()}
    if not slot_to_roster:
        return []
    roster_to_slot = {rid: s for s, rid in slot_to_roster.items()}
    season = str(data["league_chain"][-1]["season"])
    num_teams = len(data["rosters"]) or 10

    season_picks = pick_df[pick_df["season"] == season]
    rounds = sorted({int(r) for r in season_picks["round"].unique()})

    sleeper_owner = {}
    for rnd in rounds:
        for col in range(1, num_teams + 1):
            sleeper_owner[(rnd, col)] = slot_to_roster.get(col)
    for tp in data["traded_picks"]:
        if str(tp["season"]) != season:
            continue
        col = roster_to_slot.get(tp["roster_id"])
        if col is not None:
            sleeper_owner[(tp["round"], col)] = tp["owner_id"]

    label_to_roster = {label: rid for rid, label in team_labels.items()}
    mismatches = []
    for _, row in season_picks.iterrows():
        if pd.isna(row["pick_in_round"]):
            continue
        rnd, col = int(row["round"]), int(row["pick_in_round"])
        ours = label_to_roster.get(row["current_owner_team"])
        theirs = sleeper_owner.get((rnd, col))
        if ours != theirs:
            mismatches.append(
                f"R{rnd} Pick {col}: dashboard shows {team_labels.get(ours, ours)}, "
                f"Sleeper shows {team_labels.get(theirs, theirs)}"
            )
    return sorted(mismatches)


def get_pick_transaction_log(data):
    """Every draft-pick trade, most recent first. Sourced from actual 'trade'
    transactions' draft_picks field (not the traded_picks endpoint, which only
    reports the current net owner) - so a pick traded twice shows both hops,
    each keyed off previous_owner_id -> owner_id for that specific trade.

    Only type == 'trade' is read here, so a commissioner manually reassigning
    picks (e.g. converting the draft to a snake after some round) won't show
    up as a fake trade - that kind of edit isn't a roster transaction at all.

    TODO once real keeper picks exist: append a synthetic entry per is_keeper
    pick reading "Forfeited (kept <player>)" for that round/roster.
    """
    team_labels = _team_labels(data["rosters"], data["users"])
    events = []
    for t in data.get("transactions", []):
        if t.get("type") != "trade":
            continue
        created = t.get("created") or 0
        for dp in t.get("draft_picks") or []:
            events.append(
                {
                    "created": created,
                    "season": dp["season"],
                    "round": dp["round"],
                    "original_team": team_labels.get(dp.get("roster_id"), dp.get("roster_id")),
                    "from_team": team_labels.get(dp.get("previous_owner_id"), dp.get("previous_owner_id")),
                    "to_team": team_labels.get(dp.get("owner_id"), dp.get("owner_id")),
                }
            )

    events.sort(key=lambda e: e["created"], reverse=True)
    for e in events:
        created = e.pop("created")
        e["date"] = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d") if created else None
    return pd.DataFrame(events)


def get_pick_chain(pick_txn_df, season, round_, original_team):
    """['Team A'] if a pick was never traded, else ['Team A', 'Team B', ...]
    in chronological custody order - built by walking the pick transaction
    log's hops for this exact (season, round, original_team) pick."""
    if pick_txn_df.empty:
        return [original_team]
    matches = pick_txn_df[
        (pick_txn_df["season"] == season)
        & (pick_txn_df["round"] == round_)
        & (pick_txn_df["original_team"] == original_team)
    ].sort_values("date")
    if matches.empty:
        return [original_team]
    return [original_team] + list(matches["to_team"])




def get_team_visual_info(data):
    """{team_label: {'avatar_url': str|None, 'color': '#rrggbb'}} - color is
    the average pixel color of the manager's Sleeper avatar, used as a
    consistent, low-key accent (not a designed brand palette)."""
    team_labels = _team_labels(data["rosters"], data["users"])
    user_by_id = {u["user_id"]: u for u in data["users"]}
    roster_owner = {r["roster_id"]: r.get("owner_id") for r in data["rosters"]}

    visuals = {}
    for roster_id, label in team_labels.items():
        user = user_by_id.get(roster_owner.get(roster_id), {})
        avatar = user.get("avatar")
        avatar_url = f"https://sleepercdn.com/avatars/thumbs/{avatar}" if avatar else None
        color = "#888888"
        if avatar_url:
            try:
                resp = requests.get(avatar_url, timeout=5)
                img = Image.open(BytesIO(resp.content)).convert("RGB").resize((1, 1))
                r, g, b = img.getpixel((0, 0))
                color = f"#{r:02x}{g:02x}{b:02x}"
            except Exception:
                pass
        visuals[label] = {"avatar_url": avatar_url, "color": color}
    return visuals


def get_player_draft_history(data, player_id):
    """Every draft-board appearance for a player, oldest first - the evidence
    behind their keeper_value_round / years_kept_so_far computation."""
    team_labels = _team_labels(data["rosters"], data["users"])
    history = []
    for season in sorted(data["picks_by_season"], key=int):
        for pick in data["picks_by_season"][season]:
            if pick.get("player_id") != player_id:
                continue
            history.append(
                {
                    "season": season,
                    "round": pick["round"],
                    "pick_no": pick.get("pick_no"),
                    "is_keeper": bool(pick.get("is_keeper")),
                    "team": team_labels.get(pick.get("roster_id"), pick.get("roster_id")),
                }
            )
    return history


def get_player_transaction_history(data, player_id):
    """Every trade/waiver/free-agent/keeper event involving a player, most recent first."""
    team_labels = _team_labels(data["rosters"], data["users"])
    season_team_count = _season_team_count(data)
    events = []
    for t in data.get("transactions", []):
        created = t.get("created") or 0
        label = _TXN_LABELS.get(t.get("type"), t.get("type"))
        for pid, rid in (t.get("adds") or {}).items():
            if pid == player_id:
                events.append({"created": created, "action": "Added", "type": label, "team": team_labels.get(rid, rid)})
        for pid, rid in (t.get("drops") or {}).items():
            if pid == player_id:
                events.append({"created": created, "action": "Dropped", "type": label, "team": team_labels.get(rid, rid)})

    # Keeper picks aren't "transactions" in Sleeper's terms, but they're just
    # as much a player-custody event as a trade or waiver claim, so they
    # belong in the same log. None exist yet in this league.
    for season, picks in data.get("picks_by_season", {}).items():
        draft = (data.get("drafts_by_season") or {}).get(season) or {}
        created = draft.get("start_time") or 0
        for pick in picks:
            if pick.get("player_id") == player_id and pick.get("is_keeper") is True:
                pick_label = _format_pick(season, pick["round"], pick.get("pick_no"), season_team_count)
                events.append(
                    {
                        "created": created,
                        "action": f"Kept ({pick_label})",
                        "type": "Keeper",
                        "team": team_labels.get(pick.get("roster_id"), pick.get("roster_id")),
                    }
                )

    # The history should read as a complete story starting from how the
    # player entered the player pool at all - a UDFA's earliest event is
    # already their pickup transaction above, but a drafted player's origin
    # pick isn't a "transaction" in Sleeper's terms, so it has to be added
    # separately. Skipped if that pick was itself a keeper pick (already
    # captured by the loop above) - not possible yet since no keepers exist.
    draft_history = get_player_draft_history(data, player_id)
    if draft_history and not draft_history[0]["is_keeper"]:
        origin = draft_history[0]
        origin_draft = (data.get("drafts_by_season") or {}).get(origin["season"]) or {}
        pick_label = _format_pick(origin["season"], origin["round"], origin.get("pick_no"), season_team_count)
        events.append(
            {
                "created": origin_draft.get("start_time") or 0,
                "action": f"Drafted ({pick_label})",
                "type": "Draft",
                "team": origin["team"],
            }
        )

    events.sort(key=lambda e: e["created"], reverse=True)
    for e in events:
        created = e.pop("created")
        e["date"] = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d") if created else None
    return events


def get_player_acquisition_chain(data, player_id, franchise_tag_years_used=0):
    """One entry per season describing how the player's status changed that
    season - e.g. ["Drafted 3.11 (2025)", "Keeper 3.11 (2026)"]. Within a
    season, a trade/waiver move is treated as the season's outcome (it
    necessarily happens after that season's preseason draft)."""
    team_labels = _team_labels(data["rosters"], data["users"])
    season_team_count = _season_team_count(data)

    draft_by_season = {}
    for season, picks in data["picks_by_season"].items():
        for pick in picks:
            if pick.get("player_id") == player_id:
                draft_by_season[season] = pick

    txn_by_season = {}
    for t in data.get("transactions", []):
        season = t.get("season")
        adds = t.get("adds") or {}
        if player_id not in adds:
            continue
        created = t.get("created") or 0
        if season not in txn_by_season or created > txn_by_season[season]["created"]:
            txn_by_season[season] = {"created": created, "txn": t}

    chain = []
    for season in sorted(set(draft_by_season) | set(txn_by_season), key=int):
        txn_entry = txn_by_season.get(season)
        if txn_entry:
            t = txn_entry["txn"]
            label = _TXN_LABELS.get(t.get("type"), t.get("type"))
            if t.get("type") == "trade":
                from_roster = (t.get("drops") or {}).get(player_id)
                from_team = team_labels.get(from_roster, from_roster)
                chain.append(f"Traded (from {from_team}) ({season})" if from_team else f"Traded ({season})")
            else:
                chain.append(f"{label} ({season})")
        elif season in draft_by_season:
            pick = draft_by_season[season]
            pick_display = _format_pick(season, pick["round"], pick.get("pick_no"), season_team_count)
            label = "Keeper" if pick.get("is_keeper") is True else "Drafted"
            chain.append(f"{label} {pick_display} ({season})")

    if franchise_tag_years_used > 0:
        current_season = data["league_chain"][-1]["season"]
        chain.append(f"Tag ({current_season})")

    return chain
