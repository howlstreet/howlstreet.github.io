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


RECENT_TRADE_WINDOW_DAYS = 14


def fetch_insider_trades():
    """Pull cluster-buys + big-sales pages in parallel, dedupe by
    (ticker, trade_date, type), return at most 12 highest-signal trades
    from the last RECENT_TRADE_WINDOW_DAYS.

    Ranking inside the window: dollar_value desc, then num_insiders desc.
    Trades older than the window are dropped — stale Form 4s aren't news."""
    out = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        for chunk in ex.map(_fetch_one_openinsider, _OPENINSIDER_SOURCES):
            out.extend(chunk)
    cutoff = (datetime.utcnow() - timedelta(days=RECENT_TRADE_WINDOW_DAYS)).date()
    seen = set()
    deduped = []
    for tr in out:
        key = (tr["ticker"], tr["trade_date"], tr["type"])
        if key in seen:
            continue
        seen.add(key)
        try:
            td = datetime.strptime(tr["trade_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if td < cutoff:
            continue
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
    """Clean Form 4 share chart: tight scale, single mark, side legend."""
    import post_graphics as gfx

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

    is_buy = trade["type"] == "P"
    verb = "BOUGHT" if is_buy else "SOLD"
    pct_since = 0.0
    if trade.get("price"):
        pct_since = (values[-1] - trade["price"]) / trade["price"] * 100
    badge = f"{'+' if pct_since >= 0 else ''}{pct_since:.1f}%"
    dollar = trade.get("dollar_value") or 0
    if dollar >= 1_000_000:
        dollar_str = f"${dollar/1_000_000:.1f}M"
    else:
        dollar_str = f"${dollar:,.0f}"
    try:
        nice = trade_dt.strftime("%b %d")
    except Exception:
        nice = trade["trade_date"]
    n = trade.get("num_insiders", 1) or 1
    who = f"{n} insiders" if n > 1 else "1 insider"

    marks = [{
        "date": trade_dt,
        "label": f"{verb}",
        "detail": f"{nice} · {dollar_str} · {who}",
    }]
    title = f"{ticker} · {trade.get('company') or ''}".rstrip(" ·")

    return gfx.render_price_chart_with_marks(
        ticker=ticker,
        title=title,
        dates=dates,
        values=values,
        marks=marks,
        out_path=out_path,
        badge_text=badge,
        badge_positive=pct_since >= 0,
        desk_label="INSIDER WIRE · SEC FORM 4",
        source_label="Source: openinsider.com · SEC Form 4",
        accent=gfx.GREEN if is_buy else gfx.RED,
    )


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
        if ts <= cutoff:
            continue
        # Keep text posts even if chart is missing; drop only expired ones.
        chart = post.get("chart_path")
        if chart and not (REPO_ROOT / chart).exists():
            post = dict(post)
            post["chart_path"] = None
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
    # Cap new charts per cron so Actions stays inside the timeout.
    MAX_NEW_CHARTS = 5
    charts_made = 0
    # Prefer largest dollar trades for charting first.
    pending = [tr for tr in trades
               if f"{tr['ticker']}_{tr['type']}_{tr['trade_date']}" not in recent]
    pending.sort(key=lambda t: t.get("dollar_value", 0) or 0, reverse=True)

    for tr in pending:
        post_id = f"{tr['ticker']}_{tr['type']}_{tr['trade_date']}"
        chart_path = None
        if charts_made < MAX_NEW_CHARTS:
            try:
                chart_path = render_trade_chart(tr)
                if chart_path:
                    charts_made += 1
            except Exception as e:
                print(f"  ! insider chart {tr.get('ticker')}: {e}", file=sys.stderr)
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
