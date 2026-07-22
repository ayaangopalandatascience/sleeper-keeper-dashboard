# Bag n Butthole — Keeper Dashboard

A Streamlit dashboard for a 10-team [Sleeper](https://sleeper.com) keeper/dynasty-lite fantasy
football league. It pulls live data from the Sleeper API and computes everything the league's
keeper and franchise-tag rules require that Sleeper itself doesn't track — keeper eligibility,
tag cost, and years remaining — plus a real draft-pick trade ledger and prior-season performance,
all in one place.

## Features

**Player / Keeper Ledger**
- Every rostered player: position, NFL team, age, acquisition history, keeper status, keeper
  cost, years remaining, franchise tag years remaining, and total potential keeper years
- Previous season's points, PPG, games played, overall finish, and position rank (e.g. `RB2`)
- Click any column header to sort by it (click again to reverse, a third time to reset)
- Click a player's name for a detail modal: headshot + NFL team logo, full keeper audit trail,
  and complete transaction history (draft → trades/waivers → keeper picks), most recent first

**Draft Picks**
- A real draft-board grid in **true draft order**: rounds 1–6 run linear, rounds 7+ snake (the
  order reverses each round), exactly as the league runs it. Columns are the actual pick
  positions, so reading a row left-to-right is the real pick sequence and every cell's owner
  matches Sleeper's board.
- Trades are annotated inline with the full custody chain (`Team A → Team B → Team C`), showing
  each pick's snake-initial owner through to its current owner — a pick traded twice shows both hops
- "Highlight team" view blacks out every other team's cells so you can see exactly what one
  team's draft capital looks like
- A built-in **reconciliation guardrail** cross-checks the reconstructed board against Sleeper's
  real pick ownership on every load and warns loudly if they ever diverge
- Pick transaction log sourced from actual trade data, ready for multi-hop trades and (once the
  league has real keepers) forfeited picks

**League Rules** — quick reference for the actual keeper/tag/waiver rules, so the numbers above
aren't floating without context.

## Setup

```bash
git clone <this repo>
cd "Sleeper Dashboard"
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Set your league in `config.py`:

```python
CURRENT_LEAGUE_ID = "your_sleeper_league_id"  # from the league URL
```

Older seasons are discovered automatically by walking Sleeper's `previous_league_id` chain — no
need to list them manually.

Run it:

```bash
streamlit run dashboard.py
```

## Project structure

| File | What it does |
|---|---|
| `config.py` | The one thing you set per league: `CURRENT_LEAGUE_ID` |
| `sleeper_api.py` | Thin wrapper over Sleeper's public REST API and its (undocumented but stable) stats endpoint, with on-disk caching for the heavy calls |
| `ledger.py` | All the derived logic — keeper eligibility, tag cost, draft/trade history reconstruction, previous-season stats. This is where the actual rule engine lives |
| `dashboard.py` | The Streamlit UI — tables, filters, the draft board grid, the player detail modal |
| `overrides.yaml` | Manual overlay for what Sleeper can't represent: franchise tags (a permanent house rule) and, as an escape hatch, manual pick ownership when a pick moved outside a normal trade |
| `cache/` | Local cache for the player directory and season stats (gitignored, rebuilds automatically) |

## How the keeper logic works

Sleeper's API has no idea what a "keeper" or "franchise tag" is — it only knows draft picks,
rosters, and transactions. Everything keeper-related is reconstructed from that raw data:

- **Keeper value** comes from the most recent season a player was drafted *fresh* (not as a
  keeper). If they were never drafted at all, they're UDFA and their value is fixed at round 10
  (per league rules), regardless of when they were actually picked up.
- **Retention rule**: a player who ended a season on *any* roster keeps their draft value and
  years-kept count, no matter how many times they were traded, dropped, or re-added along the
  way. A player who ends a season unclaimed simply won't be on any roster next season — no
  special-case code needed for that, it falls out naturally.
- **Tenure caps**: 3 total seasons on a roster for a drafted player (draft season + up to 2 kept
  seasons), 2 total for a UDFA (pickup season + up to 1 kept season).
- **Franchise tag cost**: Year 1 costs R-1 of the player's draft round, Year 2 costs R-2. A
  round-1 player has no valid Year-1 round to pay (round 0 doesn't exist) and gets 0 tag-years; a
  round-2 player gets 1 (Year 1 only); round 3+ gets the full 2. UDFAs are never tag-eligible.
  Since Sleeper has no concept of a franchise tag, active tags are tracked manually in
  `overrides.yaml`.
- **Once real keeper picks exist** in a season's draft (Sleeper marks these with `is_keeper:
  true` on the pick), the whole system picks them up automatically — the manual overlay is only
  needed for franchise tags, permanently, since that's a pure house rule.

## How the draft board works

Sleeper can't run a linear-then-snake draft, so the commissioner sets the draft to `linear` and
manually reassigns picks to fake a snake after round 6 (the last pick of round 6 picks again first
in round 7). That manual reassignment has two side effects the dashboard has to work around: it
pollutes Sleeper's `traded_picks` data with dozens of fake "trades" that are really just the snake
conversion, and it *overwrites* any real pick trade sitting on those same picks. So the board is
reconstructed from clean sources rather than trusting Sleeper's live pick data directly:

- **Ownership** starts from identity (each team owns its own pick) and applies only real trades
  from the permanent transaction log (`type: trade`), which a manual reassignment can never
  overwrite. Sleeper's `traded_picks` endpoint is trusted only for the rounds the snake conversion
  doesn't touch.
- **The snake is display-only.** A pick's position within its round is computed from the league's
  own convention (`SNAKE_AFTER_ROUND_BY_SEASON` in `ledger.py`), not from Sleeper's stale `linear`
  flag. The convention is hardcoded per season, so a future year that changes format just adds an
  entry — nothing is assumed to carry forward.
- **A reconciliation check** (`reconcile_board_against_sleeper`) independently derives what Sleeper
  will actually draft, from `traded_picks` + the slot order, and compares it to the reconstructed
  board cell-for-cell. A match is strong evidence both are right; a mismatch means a pick changed
  hands outside a normal trade and shows up as a prominent warning instead of a silently wrong board.

## Known limitations

- If a UDFA sits on a roster across multiple seasons without ever being run through a draft as a
  keeper, their original acquisition season can't be recovered from Sleeper data alone. The
  dashboard falls back to "most recently completed season," which is correct today (the league
  has only one prior season) but should be re-checked as more seasons accumulate. Affected rows
  are flagged internally via `keeper_clock_start_estimated`.
- The linear-then-snake convention is hardcoded per season in `SNAKE_AFTER_ROUND_BY_SEASON`
  (`ledger.py`). If a future draft changes format (different pivot round, all-snake, all-linear),
  that entry must be updated — it's deliberately not auto-detected, since inferring it from the
  reassignment pattern would be fragile.
- Real pick trades are recovered from Sleeper's `trade` transactions, so a trade executed by
  manually reassigning a pick (instead of using Sleeper's trade tool) won't be picked up
  automatically — record those in `overrides.yaml`. The reconciliation guardrail flags the
  discrepancy if it ever happens, so it can't go unnoticed.

## Data source

Everything comes from [Sleeper's public API](https://docs.sleeper.com/) — no authentication
required. Previous-season performance uses an undocumented but stable Sleeper stats endpoint,
scored under whatever format the league actually uses (checked against `scoring_settings`, not
assumed).
