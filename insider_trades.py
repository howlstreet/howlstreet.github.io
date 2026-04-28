"""
HOWL STREET — corporate insider trades pipeline.

Each cron tick we scrape openinsider.com (free public mirror of SEC Form 4
filings) for the highest-signal corporate insider activity:
  - Cluster buys (multiple insiders purchasing the same stock at once →
    typically a strong informational signal)
  - Big insider sales ($1M+, where execs dump shares — often before bad
    news, the corruption-watch angle)

Each surfaced trade becomes a queue card with:
  - "Pack spotted: $TICKER insider bought/sold X at $P" lede
  - Bulleted briefing with company, dollar value, trade date, and the
    return-since-trade vs SPY benchmark
  - A custom matplotlib chart showing 1Y price with the line color-split
    at the trade date and an annotation arrow
  - Branded HOWL STREET watermark

Data note: Capitol Trades (Congressional) is locked behind CloudFront,
and the public Senate/House Stock Watcher S3 dumps are 403. When a
working Congressional data source surfaces, plug it into the same
collect_insider_posts() output schema and the queue / site will render
it identically.
"""

import os
import re
import sys
import json
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import yfinance as yf

REPO_ROOT = Path(__file__).parent
INSIDER_CHARTS_DIR = REPO_ROOT / "charts" / "insider"
INSIDER_POSTS_PATH = REPO_ROOT / "insider_posts.json"
NY = ZoneInfo("America/New_York")

# Brand palette (matches signals.py)
BRAND_GREEN = "#00ff88"
BRAND_RED = "#ff4d4d"
BRAND_BG = "#0a0a0a"
BRAND_FG = "#cccccc"
BRAND_DIM = "#666666"
BRAND_GRAY = "#888888"

INSIDER_POST_TTL_HOURS = 48  # how long a fired insider trade stays in queue.html

# openinsider sources we scrape (URL → category label)
_OPENINSIDER_SOURCES = (
    ("CLUSTER_BUY", "http://openinsider.com/latest-cluster-buys"),
    ("BIG_SALE",    "http://openinsider.com/latest-insider-sales-1m"),
)


# ----------------------------------------------------------------------------
# SCRAPER
# ----------------------------------------------------------------------------

def _strip_tags(s):
    """Strip HTML tags + decode entities + collapse whitespace."""
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&amp;", "&").replace("&nbsp;", " ").replace("&#39;", "'")
    return re.sub(r"\s+", " ", s).strip()


def _parse_money(s):
    """Convert '$1,791,696' or '+$1,791,696' to a float."""
    cleaned = re.sub(r"[^\d.\-]", "", s or "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_int(s):
    cleaned = re.sub(r"[^\d\-]", "", s or "")
    try:
        return int(cleaned)
    except ValueError:
        return 0


def _fetch_one_openinsider(category_url):
    """Worker: fetch one openinsider page, parse the tinytable, return list
    of trade dicts. Defensive — bad data / HTTP error returns []."""
    category, url = category_url
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; HowlStreet/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            page = resp.read(800_000).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ! openinsider {category}: {e}", file=sys.stderr)
        return []

    table_match = re.search(
        r'<table[^>]*class="tinytable"[^>]*>(.*?)</table>',
        page, re.DOTALL,
    )
    if not table_match:
        return []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_match.group(1), re.DOTALL)

    out = []
    for row in rows[1:]:  # skip header
        cells_raw = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells_raw) < 13:
            continue
        # Extract ticker — sometimes wrapped in a JS tooltip span; the
        # actual symbol shows up after the messy ". Take the trailing
        # capitalized run.
        ticker_raw = _strip_tags(cells_raw[3])
        m = re.search(r"\b([A-Z]{1,5})\s*$", ticker_raw)
        ticker = m.group(1) if m else ""
        if not ticker:
            continue

        company = _strip_tags(cells_raw[4])
        industry = _strip_tags(cells_raw[5])
        try:
            num_insiders = int(_strip_tags(cells_raw[6]) or 0)
        except ValueError:
            num_insiders = 0
        type_str = _strip_tags(cells_raw[7]).upper()
        if "PURCHASE" in type_str:
            ttype = "P"
        elif "SALE" in type_str:
            ttype = "S"
        else:
            continue
        price = _parse_money(_strip_tags(cells_raw[8]))
        qty = _parse_int(_strip_tags(cells_raw[9]))
        dollar_value = _parse_money(_strip_tags(cells_raw[12]))
        trade_date_raw = _strip_tags(cells_raw[2])
        try:
            trade_date = datetime.strptime(trade_date_raw, "%Y-%m-%d").date()
        except ValueError:
            continue

        out.append({
            "ticker": ticker,
            "company": company,
            "industry": industry,
            "num_insiders": num_insiders,
            "type": ttype,
            "price": price,
            "qty": qty,
            "dollar_value": dollar_value,
            "trade_date": trade_date.strftime("%Y-%m-%d"),
            "category": category,
            "link": f"http://openinsider.com/screener?s={ticker}",
        })
    return out


def fetch_insider_trades():
    """Pull cluster-buys + big-sales pages in parallel, dedupe by
    (ticker, trade_date, type), return at most 12 highest-signal trades.

    Ranking: dollar_value desc, then num_insiders desc."""
    out = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        for chunk in ex.map(_fetch_one_openinsider, _OPENINSIDER_SOURCES):
            out.extend(chunk)
    seen = set()
    deduped = []
    for tr in out:
        key = (tr["ticker"], tr["trade_date"], tr["type"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(tr)
    deduped.sort(key=lambda t: (t["dollar_value"], t["num_insiders"]), reverse=True)
    return deduped[:12]


# ----------------------------------------------------------------------------
# CHART RENDERER
# ----------------------------------------------------------------------------

def _fetch_price_history(ticker, years=1):
    try:
        hist = yf.Ticker(ticker).history(period=f"{years}y", interval="1d", auto_adjust=False)
    except Exception as e:
        print(f"  ! yfinance {ticker}: {e}", file=sys.stderr)
        return [], []
    if hist is None or hist.empty:
        return [], []
    closes = hist["Close"].dropna()
    dates = [idx.to_pydatetime().replace(tzinfo=None) for idx in closes.index]
    values = [float(v) for v in closes.values]
    return dates, values


def render_trade_chart(trade):
    """Render a 1Y price chart with the line color-split at the trade date.
    Gray before; green-after for buys, red-after for sells. Returns a
    repo-relative chart path or None on failure."""
    INSIDER_CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    ticker = trade["ticker"]
    safe_ticker = re.sub(r"[^A-Za-z0-9]", "_", ticker)
    date_safe = trade["trade_date"].replace("-", "")
    out_path = INSIDER_CHARTS_DIR / f"{safe_ticker}_{trade['type']}_{date_safe}.png"

    dates, values = _fetch_price_history(ticker, years=1)
    if not dates:
        return None

    try:
        trade_dt = datetime.strptime(trade["trade_date"], "%Y-%m-%d")
    except ValueError:
        return None

    before_d = [d for d in dates if d < trade_dt]
    before_v = [v for d, v in zip(dates, values) if d < trade_dt]
    after_d = [d for d in dates if d >= trade_dt]
    after_v = [v for d, v in zip(dates, values) if d >= trade_dt]

    if not before_d or not after_d:
        return None  # trade falls outside our 1Y window

    is_buy = trade["type"] == "P"
    after_color = BRAND_GREEN if is_buy else BRAND_RED

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)
    fig.patch.set_facecolor(BRAND_BG)
    ax.set_facecolor(BRAND_BG)

    ax.plot(before_d, before_v, color=BRAND_GRAY, linewidth=2.0, label="Before trade")
    ax.plot(after_d, after_v, color=after_color, linewidth=2.5, label="After trade")

    # Trade date annotation
    pivot_v = after_v[0] if after_v else (before_v[-1] if before_v else 0)
    ax.scatter([trade_dt], [pivot_v], color=after_color, s=140, zorder=5,
               edgecolors=BRAND_BG, linewidths=2.5)
    ax.annotate(f"{'BOUGHT' if is_buy else 'SOLD'} HERE",
                xy=(trade_dt, pivot_v),
                xytext=(15, 15), textcoords="offset points",
                color=after_color, fontsize=13, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=after_color, lw=2))

    # Current value marker
    cur_v = values[-1]
    ax.scatter([dates[-1]], [cur_v], color=after_color, s=120, zorder=5,
               edgecolors=BRAND_BG, linewidths=2.5)
    ax.annotate(f"${cur_v:,.2f}",
                xy=(dates[-1], cur_v),
                xytext=(10, 0), textcoords="offset points",
                color=after_color, fontsize=14, fontweight="bold",
                va="center")

    # Style
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color(BRAND_DIM)
    ax.spines["left"].set_color(BRAND_DIM)
    ax.tick_params(colors=BRAND_DIM, labelsize=10)
    ax.grid(True, color=BRAND_DIM, alpha=0.15, linestyle="-", linewidth=0.5)
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))

    # Title
    ax.set_title(f"{trade['ticker']} · {trade['company']}",
                 color=BRAND_FG, fontsize=20, fontweight="bold",
                 loc="left", pad=24)

    # Big % since-trade badge top-right
    pct_since = (cur_v - trade["price"]) / trade["price"] * 100 if trade["price"] else 0
    badge_color = BRAND_GREEN if pct_since > 0 else BRAND_RED
    badge = f"{'+' if pct_since > 0 else ''}{pct_since:.1f}% since"
    fig.text(0.985, 0.93, badge,
             ha="right", va="top",
             color="#000", fontsize=22, fontweight="bold",
             family="monospace",
             bbox=dict(boxstyle="round,pad=0.5", facecolor=badge_color, edgecolor="none"))

    # HOWL STREET watermark
    fig.text(0.01, 0.965, "HOWL STREET",
             ha="left", va="top", color=BRAND_GREEN,
             fontsize=15, fontweight="bold", family="monospace")
    fig.text(0.01, 0.93, "@HowlStreet · Your Wolf of Wall Street",
             ha="left", va="top", color=BRAND_DIM, fontsize=10,
             family="monospace")

    # Source attribution + URL
    fig.text(0.99, 0.02, "Source: openinsider.com · SEC Form 4",
             ha="right", va="bottom", color=BRAND_DIM, fontsize=10,
             family="monospace")
    fig.text(0.01, 0.02, "howlstreet.github.io",
             ha="left", va="bottom", color=BRAND_DIM, fontsize=10,
             family="monospace")

    plt.tight_layout(rect=(0.01, 0.05, 0.99, 0.91))
    plt.savefig(out_path, facecolor=BRAND_BG, dpi=100, bbox_inches="tight")
    plt.close(fig)
    return str(out_path.relative_to(REPO_ROOT))


# ----------------------------------------------------------------------------
# PERSISTENCE (24/48h TTL so trades stay visible after firing)
# ----------------------------------------------------------------------------

def _load_recent_posts():
    if not INSIDER_POSTS_PATH.exists():
        return {}
    try:
        data = json.loads(INSIDER_POSTS_PATH.read_text())
    except Exception:
        return {}
    cutoff = datetime.utcnow() - timedelta(hours=INSIDER_POST_TTL_HOURS)
    out = {}
    for post_id, post in data.items():
        try:
            ts = datetime.fromisoformat(post.get("fired_at", ""))
        except (TypeError, ValueError):
            continue
        if ts > cutoff and post.get("chart_path"):
            if (REPO_ROOT / post["chart_path"]).exists():
                out[post_id] = post
    return out


def _save_recent_posts(posts_by_id):
    try:
        INSIDER_POSTS_PATH.write_text(json.dumps(posts_by_id, indent=2))
    except Exception as e:
        print(f"  ! insider posts save failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------------------

def collect_insider_posts():
    """Top-level call from update.py. Fetches recent trades, renders charts
    for new ones, persists to a TTL'd JSON, returns sorted list of post
    dicts ready for queue / site rendering."""
    print("  insider trades: fetching...")
    trades = fetch_insider_trades()
    print(f"    {len(trades)} candidate trades")

    recent = _load_recent_posts()
    print(f"    {len(recent)} carryover trades still in TTL window")

    now_iso = datetime.utcnow().isoformat()
    for tr in trades:
        post_id = f"{tr['ticker']}_{tr['type']}_{tr['trade_date']}"
        if post_id in recent:
            continue  # already rendered
        try:
            chart_path = render_trade_chart(tr)
        except Exception as e:
            print(f"  ! chart render {post_id}: {e}", file=sys.stderr)
            continue
        if not chart_path:
            continue
        # Calculate pct since the trade (from chart's last close vs trade price)
        try:
            ticker_obj = yf.Ticker(tr["ticker"])
            cur = float(ticker_obj.history(period="5d")["Close"].dropna().iloc[-1])
            pct_since = (cur - tr["price"]) / tr["price"] * 100 if tr["price"] else 0
        except Exception:
            pct_since = 0.0
        recent[post_id] = {
            **tr,
            "post_id": post_id,
            "chart_path": chart_path,
            "pct_since": pct_since,
            "fired_at": now_iso,
        }

    _save_recent_posts(recent)
    posts = list(recent.values())
    posts.sort(key=lambda p: p.get("fired_at", ""), reverse=True)
    return posts
