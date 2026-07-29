# Howl Street

Auto-updating global financial news terminal. Live at [howlstreet.github.io](https://howlstreet.github.io).

## How it works

- `template.html` — the page layout with `{{PLACEHOLDERS}}` for live data
- `update.py` — fetches markets + RSS, fills the template, writes `index.html`
- `insider_trades.py` — SEC Form 4 → Insider Wire + charts
- `congress_trades.py` — STOCK Act clusters → Capitol Wire + charts
- `make_queue.py` — builds local copy/paste post packages (caption + image)
- `.github/workflows/update.yml` — runs on a schedule via GitHub Actions

## How to post

```bash
python make_queue.py
```

Then either:

1. Open **`ready/`** in Finder — each numbered folder has `caption.txt` + `image.png`
2. Or open **`queue.html`** in a browser — Copy caption + Download image

`queue.html` and `ready/` are local-only (gitignored). Not on the public site.

Priority: Capitol → Insider → Loudest Howl → Hunt → Move Wire.

## Predator Desk (site)

| The Hunt | Insider Wire |
| Capitol Wire | Move Wire |

## Schedule

- Every 30 min during US market hours (M-F, 9am-5pm ET)
- Hourly outside market hours

## Manual update

Actions tab → "Update Howl Street" → "Run workflow".
