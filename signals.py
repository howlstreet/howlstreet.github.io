"""
HOWL STREET — signal detector + chart engine.

Each cron tick we pull a curated list of macro / market series, look for
"newsworthy" levels (multi-year highs/lows, big short-term moves), and
emit branded chart-attached posts that go into queue.html alongside the
RSS-derived wire posts.

The point: turn @HowlStreet from an aggregator into a signal account
that produces Polymarket-Money / Hedgeye style original posts every day.

Each fired signal becomes one queue card with:
  - Declarative headline ("U.S. 30Y mortgage rate hits 7.92%, highest since 2000")
  - "Why it matters" framing (one templated line tying the level to markets)
  - A custom matplotlib chart with HOWL STREET branding saved to charts/
  - The same Open-on-X / Copy / Download buttons as wire posts
"""

import os
import re
import sys
import json
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")  # headless on GitHub Actions
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import yfinance as yf

REPO_ROOT = Path(__file__).parent
CHARTS_DIR = REPO_ROOT / "charts"
SIGNAL_STATE_PATH = REPO_ROOT / "signal_state.json"
SIGNAL_POSTS_PATH = REPO_ROOT / "signal_posts.json"
SIGNAL_POST_TTL_HOURS = 24  # how long a fired signal stays visible in queue.html
NY = ZoneInfo("America/New_York")

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()

# Brand colors (match the site)
BRAND_GREEN = "#00ff88"
BRAND_BG = "#0a0a0a"
BRAND_FG = "#cccccc"
BRAND_DIM = "#666666"
BRAND_RED = "#ff4d4d"

# How big a move qualifies as "newsworthy" for short-window signals.
BIG_MOVE_PCT = 5.0     # 5% move in the lookback window
BIG_MOVE_DAYS = 5      # over 5 trading days

# How recent a high/low has to be vs the historical window to fire.
HIGH_LOW_YEARS = 5     # qualifies as "highest in 5+ years"

# Per-signal cooldown so the same series doesn't post the same headline
# every cron tick. Once a signal fires we silence it for this many hours.
SIGNAL_COOLDOWN_HOURS = 18


# ----------------------------------------------------------------------------
# DATA STRUCTURES
# ----------------------------------------------------------------------------

@dataclass
class Series:
    """A monitored data series (FRED or yfinance)."""
    key: str              # internal id, e.g. "MORTGAGE30US"
    label: str            # user-facing, e.g. "U.S. 30Y Mortgage Rate"
    short_label: str      # punchy, used in headlines, e.g. "30Y mortgage"
    unit: str             # "%" or "$" or "" — appended to numbers in headlines
    source: str           # "FRED · MORTGAGE30US" or "Yahoo Finance · ^VIX"
    fetcher: str          # "fred" or "yahoo"
    matters_template: str # one-line "why this matters" copy
    hashtags: str         # signal-specific X tags, e.g. "#Oil #WTI #Commodities"
    decimals: int = 2     # display precision


@dataclass
class Signal:
    series: Series
    headline: str
    matters: str
    current: float
    history: list = field(default_factory=list)  # [(datetime, float)]
    chart_path: str = ""
    signal_id: str = ""   # unique id used for cooldown dedupe
    kind: str = ""        # "high" | "low" | "move_up" | "move_down"
    badge: str = ""       # short overlay text for chart, e.g. "+19.5%" or "8Y HIGH"
    extreme_years: float = 0.0  # for high/low: how many years since last match


# ----------------------------------------------------------------------------
# CURATED SERIES (~25 macro + market indicators)
# ----------------------------------------------------------------------------

SERIES_CATALOG = [
    # ── FRED macro ──
    Series("MORTGAGE30US", "U.S. 30-year mortgage rate", "30Y mortgage rate", "%", "FRED · MORTGAGE30US",
           "fred",
           "Housing affordability moves directly with this rate. Every 1% higher cuts buyer purchasing power by ~10%.",
           "#Housing #Mortgage #Rates", decimals=2),
    Series("DGS10", "U.S. 10-year Treasury yield", "10Y yield", "%", "FRED · DGS10",
           "fred",
           "The 10Y is the global benchmark for risk-free rates. Stocks, mortgages, and corporate debt are all priced off this.",
           "#Bonds #Yields #Treasury #Rates", decimals=2),
    Series("DGS2", "U.S. 2-year Treasury yield", "2Y yield", "%", "FRED · DGS2",
           "fred",
           "The 2Y tracks Fed expectations. When it inverts vs the 10Y, recession has followed within 18 months in every cycle since 1980.",
           "#Bonds #Yields #Fed", decimals=2),
    Series("DFF", "U.S. Federal funds rate", "Fed funds rate", "%", "FRED · DFF",
           "fred",
           "The Fed's policy rate sets the floor for every other interest rate in the economy.",
           "#Fed #FOMC #Rates", decimals=2),
    Series("UNRATE", "U.S. unemployment rate", "unemployment", "%", "FRED · UNRATE",
           "fred",
           "Sahm rule: when unemployment rises 0.5pp from cycle low, recession has started in every cycle since 1953.",
           "#Jobs #Recession #Macro", decimals=1),
    Series("ICSA", "U.S. weekly jobless claims", "jobless claims", "", "FRED · ICSA",
           "fred",
           "Initial claims are the highest-frequency labor market signal. Persistent moves above 250K historically mark cycle turns.",
           "#Jobs #Labor #Macro", decimals=0),
    Series("CPIAUCSL_YOY", "U.S. CPI year-over-year", "CPI YoY", "%", "FRED · CPIAUCSL",
           "fred",
           "Headline CPI is what the Fed targets at 2%. Anything well above forces tighter policy; anything below opens the door to cuts.",
           "#CPI #Inflation #Fed", decimals=1),
    Series("UMCSENT", "U.S. consumer sentiment", "consumer sentiment", "", "FRED · UMCSENT",
           "fred",
           "Sentiment leads consumer spending, which is 70% of U.S. GDP.",
           "#Consumer #Macro", decimals=1),
    Series("DCOILWTICO", "WTI crude oil price", "WTI crude", "$", "FRED · DCOILWTICO",
           "fred",
           "Oil flows through inflation, transport costs, and energy-stock earnings. A 10% move shifts every CPI forecast.",
           "#Oil #WTI #Energy #Commodities", decimals=2),

    # ── yfinance market ──
    Series("^VIX", "VIX (S&P 500 volatility)", "VIX", "", "CBOE · ^VIX",
           "yahoo",
           "The market's fear gauge. Above 30 signals stress, above 40 is panic, below 12 is complacency.",
           "#VIX #Volatility #Markets", decimals=2),
    Series("^GSPC", "S&P 500", "S&P 500", "", "Yahoo Finance · ^GSPC",
           "yahoo",
           "The benchmark of U.S. equities. Watched globally as the proxy for risk-on / risk-off.",
           "#SPX #SP500 #Stocks #Markets", decimals=2),
    Series("^IXIC", "Nasdaq Composite", "Nasdaq", "", "Yahoo Finance · ^IXIC",
           "yahoo",
           "Tech-heavy index. Leads risk appetite and is most sensitive to rate moves.",
           "#Nasdaq #Tech #Stocks", decimals=2),
    Series("^DJI", "Dow Jones Industrial Average", "Dow", "", "Yahoo Finance · ^DJI",
           "yahoo",
           "30 large U.S. industrials. Slower-moving than the S&P but the headline number retail readers track.",
           "#Dow #Stocks #Markets", decimals=2),
    Series("DX-Y.NYB", "U.S. dollar index (DXY)", "DXY", "", "ICE · DX-Y.NYB",
           "yahoo",
           "DXY measures the dollar against six major peers. Higher dollar pressures emerging markets and commodities.",
           "#DXY #Dollar #FX #Macro", decimals=2),
    Series("GC=F", "Gold futures", "Gold", "$", "COMEX · GC=F",
           "yahoo",
           "Gold tracks real yields and central bank demand. Multi-year highs flag debasement or geopolitical hedging flows.",
           "#Gold #PreciousMetals #Commodities", decimals=2),
    Series("BTC-USD", "Bitcoin", "Bitcoin", "$", "Yahoo Finance · BTC-USD",
           "yahoo",
           "Bitcoin is now treated as a macro asset alongside gold and tech. Its swings move alt-coins, public miners, and crypto-exposed stocks.",
           "#Bitcoin #BTC #Crypto", decimals=0),
    Series("CL=F", "WTI crude oil futures", "WTI crude", "$", "NYMEX · CL=F",
           "yahoo",
           "Front-month WTI is the live oil market. Inflation, energy stocks, and airline P&L all key off this.",
           "#Oil #WTI #Energy #Commodities", decimals=2),
]

SERIES_BY_KEY = {s.key: s for s in SERIES_CATALOG}


# ----------------------------------------------------------------------------
# DATA FETCHERS
# ----------------------------------------------------------------------------

def _fetch_fred(series_key, years=10):
    """Pull series observations from FRED. Returns list of (date, float)
    sorted oldest-first. Returns [] silently if no API key or HTTP fails."""
    if not FRED_API_KEY:
        return []
    real_key = "CPIAUCSL" if series_key == "CPIAUCSL_YOY" else series_key
    start = (datetime.utcnow() - timedelta(days=int(365 * years) + 30)).strftime("%Y-%m-%d")
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={real_key}&api_key={FRED_API_KEY}"
        f"&observation_start={start}&file_type=json"
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  ! FRED {series_key}: {e}", file=sys.stderr)
        return []
    out = []
    for obs in data.get("observations", []):
        v = obs.get("value", "")
        if v in (".", "", None):
            continue
        try:
            out.append((datetime.strptime(obs["date"], "%Y-%m-%d"), float(v)))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda x: x[0])

    if series_key == "CPIAUCSL_YOY":
        # Convert CPI level to YoY % change.
        by_date = {d: v for d, v in out}
        out2 = []
        for d, v in out:
            prior = d.replace(year=d.year - 1)
            # FRED publishes monthly; find closest prior-year observation
            cand = [x for x in by_date.items() if abs((x[0] - prior).days) <= 20]
            if not cand:
                continue
            _, prior_val = min(cand, key=lambda x: abs((x[0] - prior).days))
            if prior_val:
                out2.append((d, (v - prior_val) / prior_val * 100.0))
        return out2
    return out


def _fetch_yahoo(symbol, years=10):
    """Pull daily closes from yfinance. Returns list of (datetime, float)."""
    try:
        period = f"{years}y" if years <= 10 else "max"
        hist = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
    except Exception as e:
        print(f"  ! yfinance {symbol}: {e}", file=sys.stderr)
        return []
    if hist is None or hist.empty:
        return []
    closes = hist["Close"].dropna()
    return [(idx.to_pydatetime().replace(tzinfo=None), float(val)) for idx, val in closes.items()]


def fetch_series(series):
    """Dispatch to the right fetcher and return [(datetime, float), ...]."""
    if series.fetcher == "fred":
        return _fetch_fred(series.key, years=HIGH_LOW_YEARS + 5)
    return _fetch_yahoo(series.key, years=HIGH_LOW_YEARS + 5)


# ----------------------------------------------------------------------------
# SIGNAL DETECTION
# ----------------------------------------------------------------------------

def _format_value(value, series):
    if series.unit == "$":
        if abs(value) >= 1000:
            return f"${value:,.{series.decimals}f}"
        return f"${value:.{series.decimals}f}"
    if series.unit == "%":
        return f"{value:.{series.decimals}f}%"
    if series.decimals == 0:
        return f"{value:,.0f}"
    return f"{value:,.{series.decimals}f}"


def _years_since_match(history, current, comparator):
    """How many years back do we have to look before finding a value
    that matches comparator(current, val) (i.e., extending the streak)?
    Returns None if the entire window agrees with current being extreme."""
    if not history:
        return None
    today = history[-1][0]
    for d, v in reversed(history[:-1]):
        if comparator(v, current):
            yrs = (today - d).days / 365.25
            return yrs
    return (today - history[0][0]).days / 365.25


def _move_verb(pct):
    """Pick a punchier verb based on move magnitude. Wolf-vibe but reads
    like financial news, not parody."""
    a = abs(pct)
    if pct > 0:
        if a >= 15: return "rips"
        if a >= 10: return "surges"
        if a >= 7:  return "jumps"
        return "climbs"
    else:
        if a >= 15: return "craters"
        if a >= 10: return "tumbles"
        if a >= 7:  return "slides"
        return "drops"


def detect_signals_for_series(series, history):
    """Apply the multi-year high/low and big-move detectors. Returns a list
    of zero or more Signal candidates (caller dedupes with cooldown)."""
    if len(history) < 60:
        return []
    current_d, current = history[-1]
    cur_str = _format_value(current, series)
    signals = []

    # ── multi-year high ──
    yrs_since_higher = _years_since_match(history, current, lambda v, c: v > c)
    if yrs_since_higher is not None and yrs_since_higher >= HIGH_LOW_YEARS:
        yrs = int(yrs_since_higher)
        signals.append(Signal(
            series=series, current=current, history=history,
            kind="high",
            extreme_years=yrs_since_higher,
            badge=f"{yrs}Y HIGH",
            headline=f"{series.short_label.capitalize()} just hit {cur_str} — highest in {yrs}+ years",
            matters=series.matters_template,
            signal_id=f"{series.key}:high:{current_d.strftime('%Y-%m-%d')}",
        ))

    # ── multi-year low ──
    yrs_since_lower = _years_since_match(history, current, lambda v, c: v < c)
    if yrs_since_lower is not None and yrs_since_lower >= HIGH_LOW_YEARS:
        yrs = int(yrs_since_lower)
        signals.append(Signal(
            series=series, current=current, history=history,
            kind="low",
            extreme_years=yrs_since_lower,
            badge=f"{yrs}Y LOW",
            headline=f"{series.short_label.capitalize()} just dropped to {cur_str} — lowest in {yrs}+ years",
            matters=series.matters_template,
            signal_id=f"{series.key}:low:{current_d.strftime('%Y-%m-%d')}",
        ))

    # ── big short-window move ──
    cutoff = current_d - timedelta(days=BIG_MOVE_DAYS * 2)
    earlier = [(d, v) for d, v in history if d <= cutoff]
    if earlier:
        prior = earlier[-1][1]
        if prior:
            pct = (current - prior) / prior * 100.0
            if abs(pct) >= BIG_MOVE_PCT:
                verb = _move_verb(pct)
                sign = "+" if pct > 0 else ""
                signals.append(Signal(
                    series=series, current=current, history=history,
                    kind="move_up" if pct > 0 else "move_down",
                    badge=f"{sign}{pct:.1f}%",
                    headline=f"{series.short_label.capitalize()} {verb} {abs(pct):.1f}% in {BIG_MOVE_DAYS} sessions to {cur_str}",
                    matters=series.matters_template,
                    signal_id=f"{series.key}:move:{current_d.strftime('%Y-%m-%d')}",
                ))

    return signals


def detect_all_signals():
    """Walk the catalog, fetch each series, run detectors, return list of
    Signal objects ready to render (cooldown + chart applied by caller)."""
    fired = []
    for series in SERIES_CATALOG:
        history = fetch_series(series)
        if not history:
            continue
        for sig in detect_signals_for_series(series, history):
            fired.append(sig)
    return fired


# ----------------------------------------------------------------------------
# COOLDOWN
# ----------------------------------------------------------------------------

def _load_state():
    if not SIGNAL_STATE_PATH.exists():
        return {}
    try:
        return json.loads(SIGNAL_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state):
    try:
        SIGNAL_STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        print(f"  ! signal state save failed: {e}", file=sys.stderr)


def filter_with_cooldown(signals):
    """Drop signals that fired within the cooldown window. Records the
    firing time of any signal we let through so subsequent runs see it."""
    state = _load_state()
    now = datetime.utcnow()
    out = []
    for sig in signals:
        last_iso = state.get(sig.signal_id)
        if last_iso:
            try:
                last = datetime.fromisoformat(last_iso)
                if (now - last) < timedelta(hours=SIGNAL_COOLDOWN_HOURS):
                    continue
            except ValueError:
                pass
        state[sig.signal_id] = now.isoformat()
        out.append(sig)
    # Garbage-collect entries older than 14 days so the file doesn't grow.
    cutoff = now - timedelta(days=14)
    state = {k: v for k, v in state.items()
             if datetime.fromisoformat(v) > cutoff}
    _save_state(state)
    return out


# ----------------------------------------------------------------------------
# CHART RENDERER (matplotlib, HOWL STREET branded)
# ----------------------------------------------------------------------------

def render_chart(signal):
    """Render a 1200x675 PNG chart (X-card sized) for the given signal.
    Saves to charts/{signal_id}.png and returns the path."""
    CHARTS_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", signal.signal_id)
    out_path = CHARTS_DIR / f"{safe}.png"

    dates = [d for d, _ in signal.history]
    values = [v for _, v in signal.history]

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)
    fig.patch.set_facecolor(BRAND_BG)
    ax.set_facecolor(BRAND_BG)

    is_bearish = signal.kind in ("low", "move_down")
    line_color = BRAND_RED if is_bearish else BRAND_GREEN

    ax.plot(dates, values, color=line_color, linewidth=2.2)
    ax.fill_between(dates, values, min(values), color=line_color, alpha=0.10)

    # Highlight current value with a marker + label
    ax.scatter([dates[-1]], [values[-1]], color=line_color, s=120, zorder=5,
               edgecolors=BRAND_BG, linewidths=2.5)
    ax.annotate(_format_value(values[-1], signal.series),
                xy=(dates[-1], values[-1]),
                xytext=(10, 0), textcoords="offset points",
                color=line_color, fontsize=15, fontweight="bold",
                va="center")

    # Axes / grid styling
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color(BRAND_DIM)
    ax.spines["left"].set_color(BRAND_DIM)
    ax.tick_params(colors=BRAND_DIM, labelsize=10)
    ax.grid(True, color=BRAND_DIM, alpha=0.15, linestyle="-", linewidth=0.5)
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # Big title (full label) + small kicker (short label) for context
    ax.set_title(signal.series.label, color=BRAND_FG, fontsize=20,
                 fontweight="bold", loc="left", pad=24)

    # Big BADGE overlay top-right (e.g. "+19.5%" or "8Y HIGH")
    if signal.badge:
        badge_color = BRAND_RED if is_bearish else BRAND_GREEN
        fig.text(0.985, 0.93, signal.badge,
                 ha="right", va="top",
                 color="#000", fontsize=24, fontweight="bold",
                 family="monospace",
                 bbox=dict(boxstyle="round,pad=0.5", facecolor=badge_color,
                           edgecolor="none"))

    # HOWL STREET handle, top-left under title area, prominent
    fig.text(0.01, 0.965, "HOWL STREET",
             ha="left", va="top", color=BRAND_GREEN,
             fontsize=15, fontweight="bold", family="monospace")
    fig.text(0.01, 0.93, "@HowlStreet · Your Wolf of Wall Street",
             ha="left", va="top", color=BRAND_DIM, fontsize=10,
             family="monospace")

    # Source attribution + site URL bottom strip
    fig.text(0.99, 0.02, signal.series.source,
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
# ENTRY POINT
# ----------------------------------------------------------------------------

def _load_recent_posts():
    """Load the persisted signal posts and drop any older than TTL. Returns
    a dict keyed by signal_id."""
    if not SIGNAL_POSTS_PATH.exists():
        return {}
    try:
        data = json.loads(SIGNAL_POSTS_PATH.read_text())
    except Exception:
        return {}
    cutoff = datetime.utcnow() - timedelta(hours=SIGNAL_POST_TTL_HOURS)
    out = {}
    for sig_id, post in data.items():
        try:
            ts = datetime.fromisoformat(post.get("fired_at", ""))
        except (TypeError, ValueError):
            continue
        if ts > cutoff and post.get("chart_path"):
            # Verify chart file still exists on disk
            if (REPO_ROOT / post["chart_path"]).exists():
                out[sig_id] = post
    return out


def _save_recent_posts(posts_by_id):
    try:
        SIGNAL_POSTS_PATH.write_text(json.dumps(posts_by_id, indent=2))
    except Exception as e:
        print(f"  ! signal posts save failed: {e}", file=sys.stderr)


def collect_signal_posts():
    """Top-level call from update.py. Returns list of dicts ready to render
    as queue cards.

    Posts persist for SIGNAL_POST_TTL_HOURS (24h) — once a signal fires
    it stays visible in queue.html until the user gets a chance to post it,
    even though the cooldown logic prevents re-firing the same headline."""
    print("  signals: detecting...")
    fired = detect_all_signals()
    print(f"    {len(fired)} candidate signals before cooldown")
    fresh = filter_with_cooldown(fired)
    print(f"    {len(fresh)} after cooldown")

    # Load existing recent posts (still within TTL).
    recent = _load_recent_posts()
    print(f"    {len(recent)} carryover signals still in TTL window")

    now_iso = datetime.utcnow().isoformat()
    for sig in fresh:
        # Chart rendering disabled — user wants only real article photos in
        # tweets. Macro signal data still flows as text-only drafts; no PNG
        # gets written. Re-enable by setting sig.chart_path = render_chart(sig).
        sig.chart_path = None
        recent[sig.signal_id] = {
            "headline": sig.headline,
            "matters": sig.matters,
            "source": sig.series.source,
            "chart_path": None,
            "signal_id": sig.signal_id,
            "current_str": _format_value(sig.current, sig.series),
            "label": sig.series.label,
            "hashtags": sig.series.hashtags,
            "kind": sig.kind,
            "badge": sig.badge,
            "fired_at": now_iso,
        }

    _save_recent_posts(recent)

    # Most recent first
    posts = list(recent.values())
    posts.sort(key=lambda p: p.get("fired_at", ""), reverse=True)
    return posts
