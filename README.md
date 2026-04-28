# Howl Street

Auto-updating global financial news terminal. Live at [howlstreet.github.io](https://howlstreet.github.io).

## How it works

- `template.html` — the page layout with `{{PLACEHOLDERS}}` for live data
- `update.py` — fetches data from yfinance + RSS feeds, fills the template, writes `index.html`
- `.github/workflows/update.yml` — runs `update.py` on a schedule via GitHub Actions
- GitHub Pages serves `index.html` as the live site

## Schedule

- Every 30 min during US market hours (M-F, 9am-5pm ET)
- Hourly outside market hours so global markets + headlines stay fresh

## Manual update

Go to the Actions tab → "Update Howl Street" → "Run workflow" to force an update.
