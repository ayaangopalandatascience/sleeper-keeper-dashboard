import json
import time
from pathlib import Path

import requests

BASE_URL = "https://api.sleeper.app/v1"
STATS_BASE_URL = "https://api.sleeper.app/stats/nfl"
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
PLAYERS_CACHE_FILE = CACHE_DIR / "players.json"
PLAYERS_CACHE_MAX_AGE = 60 * 60 * 24  # 1 day, per Sleeper's guidance on /players/nfl
STATS_CACHE_MAX_AGE = 60 * 60 * 24  # 1 day - a completed season's totals don't change often


def _get(path):
    resp = requests.get(f"{BASE_URL}{path}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_league(league_id):
    return _get(f"/league/{league_id}")


def get_league_chain(latest_league_id):
    """Walk previous_league_id backwards. Returns leagues oldest -> newest."""
    chain = []
    league_id = latest_league_id
    while league_id:
        league = get_league(league_id)
        chain.append(league)
        league_id = league.get("previous_league_id")
    chain.reverse()
    return chain


def get_rosters(league_id):
    return _get(f"/league/{league_id}/rosters")


def get_users(league_id):
    return _get(f"/league/{league_id}/users")


def get_draft(draft_id):
    return _get(f"/draft/{draft_id}")


def get_draft_picks(draft_id):
    return _get(f"/draft/{draft_id}/picks")


def get_traded_picks(league_id):
    return _get(f"/league/{league_id}/traded_picks")


def get_transactions(league_id, week):
    return _get(f"/league/{league_id}/transactions/{week}")


def get_all_transactions(league_id, max_week=18):
    """Sleeper has no single 'all transactions' endpoint - it's paged by week."""
    all_txns = []
    for week in range(1, max_week + 1):
        txns = get_transactions(league_id, week) or []
        all_txns.extend(t for t in txns if t.get("status") == "complete")
    return all_txns


def get_all_season_stats(season, season_type="regular", force_refresh=False):
    """Undocumented but stable Sleeper stats endpoint - one call returns every
    NFL player's season totals (points under every scoring format, ranks,
    games played), instead of needing one request per player."""
    cache_file = CACHE_DIR / f"stats_{season}_{season_type}.json"
    if not force_refresh and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < STATS_CACHE_MAX_AGE:
            return json.loads(cache_file.read_text())

    resp = requests.get(f"{STATS_BASE_URL}/{season}", params={"season_type": season_type}, timeout=30)
    resp.raise_for_status()
    stats = resp.json()
    cache_file.write_text(json.dumps(stats))
    return stats


def get_all_players(force_refresh=False):
    if not force_refresh and PLAYERS_CACHE_FILE.exists():
        age = time.time() - PLAYERS_CACHE_FILE.stat().st_mtime
        if age < PLAYERS_CACHE_MAX_AGE:
            return json.loads(PLAYERS_CACHE_FILE.read_text())
    players = _get("/players/nfl")
    PLAYERS_CACHE_FILE.write_text(json.dumps(players))
    return players
