# BitUnix Grid Plan Bot

Menu-driven plan bot for BTCUSDT on BitUnix futures.

## What it does
- Define a wide weekly plan (e.g., 50 levels: low buy → high buy → high sell).
- Each day it auto-places only the BUYs allowed by BitUnix’s price-band cap.
- When a long exists, it places queued SELLs reduce-only from the plan.
- Logs each run to CSV and supports exporting a plan snapshot.

## Files in this repo
- `bitunix_grid_bot.py` – main menu and CLI (`--plan-tick` for cron)
- `README.md` – this file
- `.gitignore` – excludes venv and local cruft

## Where runtime data lives (not in git)
- Config, secrets, plan, logs: `~/.bitunix_grid_bot/`
  - `config.yaml` (defaults)
  - `secrets.json` (API keys)
  - `plan.yaml` (your plan)
  - `logs/ticks.csv`, `logs/plan_snapshot.csv`

## Quick start
```bash
python3 -m venv venv
source venv/bin/activate
pip install requests pyyaml rich
python bitunix_grid_bot.py

eof
