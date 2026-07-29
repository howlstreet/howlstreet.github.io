"""
HOWL STREET — Congressional STOCK Act disclosures (Type D).

Pulls recent House + Senate trades from CongressInvests (public free
tier over official PTR filings), clusters same-ticker buys by multiple
members, and renders Howl-branded price charts with buy marks.

Site home: Capitol Wire panel under Predator Desk.
Drafts: CONGRESS_WATCH format for manual X posting.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import yfinance as yf

REPO_ROOT = Path(__file__).parent
CONGRESS_CHARTS_DIR = REPO_ROOT / "charts" / "congress"
CONGRESS_POSTS_PATH = REPO_ROOT / "congress_posts.json"
PARTY_CACHE_PATH = REPO_ROOT / "congress_party_cache.json"
NY = ZoneInfo("America/New_York")

BRAND_GREEN = "#00ff88"
BRAND_RED = "#ff4d4d"
BRAND_BG = "#0a0a0a"
BRAND_FG = "#cccccc"
BRAND_DIM = "#666666"
BRAND_GRAY = "#888888"
BRAND_GOLD = "#ffd966"

CONGRESS_POST_TTL_HOURS = 72
MIN_CLUSTER_MEMBERS = 3          # Uber-style: multiple politicians, same ticker
LOOKBACK_DAYS = 365              # YTD-ish window for clustering
MAX_NEW_CHARTS_PER_RUN = 3
API_PAGE_SIZE = 100
API_MAX_PAGES = 8                # 800 trades — enough to find clusters
API_BASE = "https://congressinvests.com/trades"
LEGISLATORS_URL = (
    "https://unitedstates.github.io/congress-legislators/legislators-current.json"
)


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": "HowlStreet/1.0 (+https://howlstreet.github.io)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _parse_amount_band(raw):
    """Return (label, approx_midpoint) from STOCK Act bands like
    '$1,001 - $15,000' or 'Up to $5,000,000'."""
    if not raw:
        return "", 0.0
    label = re.sub(r"\s+", " ", str(raw)).strip()
    nums = [float(x.replace(",", "")) for x in re.findall(r"[\d,]+(?:\.\d+)?", label)]
    if not nums:
        return label, 0.0
    if len(nums) == 1:
        return label, nums[0]
    return label, (nums[0] + nums[1]) / 2.0


def _is_options(asset, amount_label=""):
    blob = f"{asset or ''} {amount_label or ''}".lower()
    return bool(re.search(r"\b(call|put|option|expir)", blob))


# ----------------------------------------------------------------------------
# PARTY LOOKUP
# ----------------------------------------------------------------------------

def _load_party_map():
    """Map lowercase official_full / first last → D/R/I. Cached on disk
    for a week so cron isn't hammering GitHub every 30 minutes."""
    if PARTY_CACHE_PATH.exists():
        try:
            cached = json.loads(PARTY_CACHE_PATH.read_text())
            ts = datetime.fromisoformat(cached.get("fetched_at", ""))
            if datetime.utcnow() - ts < timedelta(days=7) and cached.get("map"):
                return cached["map"]
        except Exception:
            pass

    try:
        legs = _get_json(LEGISLATORS_URL, timeout=40)
    except Exception as e:
        print(f"  ! congress party map: {e}", file=sys.stderr)
        if PARTY_CACHE_PATH.exists():
            try:
                return json.loads(PARTY_CACHE_PATH.read_text()).get("map", {})
            except Exception:
                return {}
        return {}

    party_map = {}
    for leg in legs:
        name = leg.get("name") or {}
        terms = leg.get("terms") or []
        if not terms:
            continue
        party = (terms[-1].get("party") or "").strip()
        letter = {"Democrat": "D", "Republican": "R", "Independent": "I"}.get(party, "")
        if not letter:
            continue
        keys = []
        if name.get("official_full"):
            keys.append(name["official_full"])
        first = name.get("first") or ""
        last = name.get("last") or ""
        if first and last:
            keys.append(f"{first} {last}")
            keys.append(last)
        for k in keys:
            party_map[k.strip().lower()] = letter

    try:
        PARTY_CACHE_PATH.write_text(json.dumps({
            "fetched_at": datetime.utcnow().isoformat(),
            "map": party_map,
        }))
    except Exception:
        pass
    return party_map


def _party_for(member, party_map):
    if not member:
        return ""
    key = member.strip().lower()
    if key in party_map:
        return party_map[key]
    # Soft match on last token
    last = key.split()[-1]
    hits = [v for k, v in party_map.items() if k.endswith(" " + last) or k == last]
    if len(set(hits)) == 1:
        return hits[0]
    return ""


# ----------------------------------------------------------------------------
# FETCH + NORMALIZE
# ----------------------------------------------------------------------------

def fetch_congress_trades(max_pages=API_MAX_PAGES):
    """Paginate CongressInvests free feed. Returns normalized trade dicts."""
    out = []
    for page in range(max_pages):
        offset = page * API_PAGE_SIZE
        url = f"{API_BASE}?limit={API_PAGE_SIZE}&offset={offset}"
        try:
            data = _get_json(url)
        except Exception as e:
            print(f"  ! congress page {page}: {e}", file=sys.stderr)
            break
        chunk = data.get("trades") or []
        if not chunk:
            break
        for row in chunk:
            tick = (row.get("ticker") or "").strip().upper()
            if not tick or tick in ("--", "N/A", "NONE", "NAN"):
                continue
            member = (row.get("member") or "").strip()
            if not member:
                continue
            ttype = (row.get("trade_type") or "").strip().lower()
            if "purchase" in ttype or ttype == "buy":
                side = "buy"
            elif "sale" in ttype or ttype == "sell":
                side = "sell"
            else:
                continue
            tx_raw = (row.get("tx_date") or "")[:10]
            try:
                tx = date.fromisoformat(tx_raw)
            except ValueError:
                continue
            # Drop absurd future dates (bad OCR / parse artifacts)
            if tx > date.today() + timedelta(days=1):
                continue
            amount_label, amount_mid = _parse_amount_band(row.get("amount"))
            asset = row.get("asset") or ""
            out.append({
                "member": member,
                "chamber": row.get("chamber") or "",
                "side": side,
                "ticker": tick,
                "asset": asset,
                "amount_label": amount_label,
                "amount_mid": amount_mid,
                "is_options": _is_options(asset, amount_label),
                "tx_date": tx_raw,
                "disclosed": (row.get("disclosed") or "")[:10],
                "link": row.get("link") or "",
            })
        if not data.get("has_more"):
            break
    return out


def build_buy_clusters(trades, min_members=MIN_CLUSTER_MEMBERS,
                       lookback_days=LOOKBACK_DAYS):
    """Group YTD-ish buys by ticker where ≥min_members distinct politicians
    bought and have no matching sale in-window (best-effort)."""
    cutoff = date.today() - timedelta(days=lookback_days)
    buys_by_ticker = defaultdict(list)
    sells_by_key = set()

    for t in trades:
        try:
            tx = date.fromisoformat(t["tx_date"])
        except ValueError:
            continue
        if tx < cutoff:
            continue
        if t["side"] == "sell":
            sells_by_key.add((t["member"].lower(), t["ticker"]))
            continue
        if t["side"] != "buy":
            continue
        buys_by_ticker[t["ticker"]].append(t)

    clusters = []
    for ticker, buys in buys_by_ticker.items():
        # Keep only members who haven't filed a sell in-window
        held = [b for b in buys if (b["member"].lower(), ticker) not in sells_by_key]
        members = {}
        for b in held:
            # Keep largest disclosed band per member
            prev = members.get(b["member"])
            if not prev or b["amount_mid"] > prev["amount_mid"]:
                members[b["member"]] = b
        if len(members) < min_members:
            continue
        member_trades = sorted(
            members.values(),
            key=lambda x: (-x["amount_mid"], x["tx_date"]),
        )
        first_buy = min(member_trades, key=lambda x: x["tx_date"])
        biggest = member_trades[0]
        clusters.append({
            "ticker": ticker,
            "members": member_trades,
            "member_count": len(member_trades),
            "first_buy_date": first_buy["tx_date"],
            "biggest": biggest,
            "all_buys": held,
        })

    clusters.sort(key=lambda c: (-c["member_count"], -c["biggest"]["amount_mid"]))
    return clusters


# ----------------------------------------------------------------------------
# CHART
# ----------------------------------------------------------------------------

def _fetch_price_history(ticker, years=1):
    try:
        hist = yf.Ticker(ticker).history(period=f"{years}y", interval="1d",
                                         auto_adjust=False)
    except Exception as e:
        print(f"  ! yfinance {ticker}: {e}", file=sys.stderr)
        return [], []
    if hist is None or hist.empty:
        return [], []
    closes = hist["Close"].dropna()
    dates = [idx.to_pydatetime().replace(tzinfo=None) for idx in closes.index]
    values = [float(v) for v in closes.values]
    return dates, values


def render_congress_cluster_chart(cluster, party_map):
    """Uber-style chart: YTD-ish price path with politician buy marks."""
    CONGRESS_CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    ticker = cluster["ticker"]
    safe = re.sub(r"[^A-Za-z0-9]", "_", ticker)
    out_path = CONGRESS_CHARTS_DIR / f"{safe}_cluster.png"

    dates, values = _fetch_price_history(ticker, years=1)
    if len(dates) < 10:
        return None

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)
    fig.patch.set_facecolor(BRAND_BG)
    ax.set_facecolor(BRAND_BG)

    # Color the line by YTD direction vs first point of calendar year
    ytd_start = datetime(date.today().year, 1, 1)
    ytd_vals = [v for d, v in zip(dates, values) if d >= ytd_start]
    ytd_pct = 0.0
    if ytd_vals and values:
        ytd_pct = (values[-1] - ytd_vals[0]) / ytd_vals[0] * 100
    line_color = BRAND_RED if ytd_pct < 0 else BRAND_GREEN

    ax.plot(dates, values, color=line_color, linewidth=2.2)
    ax.fill_between(dates, values, color=line_color, alpha=0.12)

    # Annotate each member buy
    for i, tr in enumerate(cluster["members"][:8]):
        try:
            td = datetime.strptime(tr["tx_date"], "%Y-%m-%d")
        except ValueError:
            continue
        # Nearest price on/after trade date
        pivot = None
        for d, v in zip(dates, values):
            if d >= td:
                pivot = (d, v)
                break
        if not pivot:
            continue
        last = tr["member"].split()[-1]
        party = _party_for(tr["member"], party_map)
        label = f"{last}" + (f" ({party})" if party else "")
        # Stagger vertical offsets so labels don't stack
        y_off = 18 + (i % 4) * 14
        x_off = 8 if i % 2 == 0 else -8
        ax.scatter([pivot[0]], [pivot[1]], color="#ffffff", s=70, zorder=5,
                   edgecolors=BRAND_BG, linewidths=1.5)
        ax.annotate(
            label,
            xy=pivot,
            xytext=(x_off, y_off),
            textcoords="offset points",
            color="#ffffff",
            fontsize=10,
            fontweight="bold",
            arrowprops=dict(arrowstyle="->", color="#ffffff", lw=1.2),
            ha="left" if x_off >= 0 else "right",
        )

    cur_v = values[-1]
    ax.scatter([dates[-1]], [cur_v], color=line_color, s=110, zorder=6,
               edgecolors=BRAND_BG, linewidths=2)
    ax.annotate(f"${cur_v:,.2f}",
                xy=(dates[-1], cur_v),
                xytext=(10, 0), textcoords="offset points",
                color=line_color, fontsize=14, fontweight="bold", va="center")

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color(BRAND_DIM)
    ax.spines["left"].set_color(BRAND_DIM)
    ax.tick_params(colors=BRAND_DIM, labelsize=10)
    ax.grid(True, color=BRAND_DIM, alpha=0.15, linewidth=0.5)
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))

    company = ""
    try:
        info = yf.Ticker(ticker).info or {}
        company = info.get("shortName") or info.get("longName") or ""
    except Exception:
        company = ""
    title = f"{company} · ${ticker}" if company else f"${ticker}"
    ax.set_title(title, color=BRAND_FG, fontsize=18, fontweight="bold",
                 loc="left", pad=22)

    badge_color = BRAND_GREEN if ytd_pct >= 0 else BRAND_RED
    badge = f"{'+' if ytd_pct >= 0 else ''}{ytd_pct:.1f}% YTD"
    fig.text(0.985, 0.93, badge, ha="right", va="top", color="#000",
             fontsize=20, fontweight="bold", family="monospace",
             bbox=dict(boxstyle="round,pad=0.45", facecolor=badge_color,
                       edgecolor="none"))

    fig.text(0.01, 0.965, "HOWL STREET", ha="left", va="top",
             color=BRAND_GREEN, fontsize=15, fontweight="bold",
             family="monospace")
    fig.text(0.01, 0.93, "CAPITOL WIRE · STOCK Act disclosures",
             ha="left", va="top", color=BRAND_DIM, fontsize=10,
             family="monospace")
    fig.text(0.99, 0.02, "Source: STOCK Act PTR filings",
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
# PERSISTENCE + ENTRY
# ----------------------------------------------------------------------------

def _load_recent():
    if not CONGRESS_POSTS_PATH.exists():
        return {}
    try:
        data = json.loads(CONGRESS_POSTS_PATH.read_text())
    except Exception:
        return {}
    cutoff = datetime.utcnow() - timedelta(hours=CONGRESS_POST_TTL_HOURS)
    out = {}
    for pid, post in data.items():
        try:
            ts = datetime.fromisoformat(post.get("fired_at", ""))
        except (TypeError, ValueError):
            continue
        if ts > cutoff:
            out[pid] = post
    return out


def _save_recent(posts):
    try:
        CONGRESS_POSTS_PATH.write_text(json.dumps(posts, indent=2))
    except Exception as e:
        print(f"  ! congress posts save failed: {e}", file=sys.stderr)


def _pct_since(ticker, since_date):
    try:
        hist = yf.Ticker(ticker).history(period="1y", interval="1d",
                                         auto_adjust=False)
        if hist is None or hist.empty:
            return 0.0
        closes = hist["Close"].dropna()
        # Normalize tz so we can compare to naive since_date
        idx = closes.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
            closes = closes.copy()
            closes.index = idx
        since = datetime.strptime(since_date, "%Y-%m-%d")
        after = closes[closes.index >= since]
        if after.empty:
            after = closes
        base = float(after.iloc[0])
        cur = float(closes.iloc[-1])
        return (cur - base) / base * 100 if base else 0.0
    except Exception:
        return 0.0


def collect_congress_posts():
    """Fetch, cluster, chart top stories, return list of post packages."""
    print("  congress trades: fetching...")
    trades = fetch_congress_trades()
    print(f"    {len(trades)} normalized trades")
    party_map = _load_party_map()
    clusters = build_buy_clusters(trades)
    print(f"    {len(clusters)} buy clusters (≥{MIN_CLUSTER_MEMBERS} members)")

    recent = _load_recent()
    now_iso = datetime.utcnow().isoformat()
    charts_made = 0

    for cluster in clusters[:12]:
        ticker = cluster["ticker"]
        post_id = f"congress_{ticker}_{cluster['first_buy_date']}_{cluster['member_count']}"
        if post_id in recent:
            continue

        members_out = []
        for tr in cluster["members"]:
            party = _party_for(tr["member"], party_map)
            members_out.append({
                "name": tr["member"],
                "party": party,
                "chamber": tr["chamber"],
                "amount_label": tr["amount_label"],
                "amount_mid": tr["amount_mid"],
                "tx_date": tr["tx_date"],
                "is_options": tr["is_options"],
                "link": tr["link"],
            })

        chart_path = None
        if charts_made < MAX_NEW_CHARTS_PER_RUN:
            try:
                chart_path = render_congress_cluster_chart(cluster, party_map)
                if chart_path:
                    charts_made += 1
            except Exception as e:
                print(f"  ! congress chart {ticker}: {e}", file=sys.stderr)

        pct = _pct_since(ticker, cluster["first_buy_date"])
        biggest = cluster["biggest"]
        recent[post_id] = {
            "post_id": post_id,
            "ticker": ticker,
            "member_count": cluster["member_count"],
            "members": members_out,
            "first_buy_date": cluster["first_buy_date"],
            "biggest_name": biggest["member"],
            "biggest_amount": biggest["amount_label"],
            "biggest_date": biggest["tx_date"],
            "biggest_is_options": biggest["is_options"],
            "pct_since_first": pct,
            "chart_path": chart_path,
            "source_url": biggest.get("link") or (
                f"https://congressinvests.com/trades/{urllib.parse.quote(ticker)}"
            ),
            "fired_at": now_iso,
        }

    # Refresh carryover chart paths that vanished
    _save_recent(recent)
    posts = list(recent.values())
    posts.sort(key=lambda p: (p.get("member_count", 0), p.get("fired_at", "")),
               reverse=True)
    return posts
