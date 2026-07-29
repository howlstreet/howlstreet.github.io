# Howl Street

Auto-updating global financial news terminal. Live at [howlstreet.github.io](https://howlstreet.github.io).

## How it works

- `template.html` — the page layout with `{{PLACEHOLDERS}}` for live data
- `update.py` — fetches markets + RSS, fills the template, writes `index.html`
- `insider_trades.py` — SEC Form 4 cluster/big trades → Insider Wire + charts
- `congress_trades.py` — STOCK Act multi-member buy clusters → Capitol Wire + charts
- `drafter.py` — short truth-forward post drafts in `drafts.json` (manual X for now)
- `.github/workflows/update.yml` — runs on a schedule via GitHub Actions

## Predator Desk (site)

- **The Hunt** — corruption / fraud headlines
- **Insider Wire** — corporate Form 4 buys/sells
- **Capitol Wire** — Congress STOCK Act clusters (Type D)

## Schedule

- Every 30 min during US market hours (M-F, 9am-5pm ET)
- Hourly outside market hours so global markets + headlines stay fresh

## Manual update

Go to the Actions tab → "Update Howl Street" → "Run workflow" to force an update.
