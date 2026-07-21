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
- A real draft-order grid (not just a round-by-round list) built from the actual assigned draft
  slots, with team avatars per cell
- Trades are annotated inline with the full custody chain (`Team A → Team B → Team C`), not just
  current vs. original owner — a pick traded twice shows both hops
- "Highlight team" view blacks out every other team's cells so you can see exactly what one
  team's draft capital looks like
- Pick transaction log sourced from actual trade data, ready for multi-hop trades and (once the
  league has real keepers) forfeited-picks

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
| `overrides.yaml` | Manual overlay for the one thing Sleeper has no concept of at all: franchise tags |
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

## Known limitations

- If a UDFA sits on a roster across multiple seasons without ever being run through a draft as a
  keeper, their original acquisition season can't be recovered from Sleeper data alone. The
  dashboard falls back to "most recently completed season," which is correct today (the league
  has only one prior season) but should be re-checked as more seasons accumulate. Affected rows
  are flagged internally via `keeper_clock_start_estimated`.
- Draft board columns are labeled by pick position (`Pick 1`, `Pick 2`, ...), not by team,
  because a commissioner manually converting part of the draft to a snake format later wouldn't
  be reflected in Sleeper's draft-order data — a fixed team-per-column header would go stale.
- The pick-transaction log only reads actual `trade` transactions, so a commissioner manually
  reassigning pick slots (e.g. converting to a snake after some round) intentionally does **not**
  show up there — that's not a roster transaction.

## Data source

Everything comes from [Sleeper's public API](https://docs.sleeper.com/) — no authentication
required. Previous-season performance uses an undocumented but stable Sleeper stats endpoint,
scored under whatever format the league actually uses (checked against `scoring_settings`, not
assumed).
