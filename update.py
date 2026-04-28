"""
HOWL STREET — site updater
Pulls live market data + headlines, rebuilds index.html from template.
Runs on GitHub Actions on a schedule.
"""

import os
import sys
import html
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf
import feedparser
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent
TEMPLATE_PATH = REPO_ROOT / "template.html"
OUTPUT_PATH = REPO_ROOT / "index.html"

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")

# Tickers we pull. Format: (display_name, yfinance_symbol, format_type)
# format_type: "price" (2dp), "yield" (3dp%), "bp" (basis points)
US_EQUITIES = [
    ("S&P 500",     "^GSPC", "price"),
    ("Nasdaq 100",  "^NDX",  "price"),
    ("Dow Jones",   "^DJI",  "price"),
    ("Russell 2000","^RUT",  "price"),
    ("VIX",         "^VIX",  "price"),
]

GLOBAL_INDICES = [
    ("FTSE 100",   "^FTSE",   "price"),
    ("DAX",        "^GDAXI",  "price"),
    ("CAC 40",     "^FCHI",   "price"),
    ("Nikkei 225", "^N225",   "price"),
    ("Hang Seng",  "^HSI",    "price"),
    ("Shanghai",   "000001.SS","price"),
]

TREASURIES = [
    ("US 2Y",  "^IRX",  "yield"),  # 13-week as proxy until we wire FRED
    ("US 5Y",  "^FVX",  "yield"),
    ("US 10Y", "^TNX",  "yield"),
    ("US 30Y", "^TYX",  "yield"),
]

FX_PAIRS = [
    ("DXY",      "DX-Y.NYB", "price"),
    ("EUR/USD",  "EURUSD=X", "fx"),
    ("GBP/USD",  "GBPUSD=X", "fx"),
    ("USD/JPY",  "JPY=X",    "fx2"),
    ("USD/CNH",  "CNH=X",    "fx"),
    ("AUD/USD",  "AUDUSD=X", "fx"),
]

COMMODITIES = [
    ("WTI Crude", "CL=F", "price"),
    ("Brent",     "BZ=F", "price"),
    ("Nat Gas",   "NG=F", "price"),
    ("Gold",      "GC=F", "price"),
    ("Silver",    "SI=F", "price"),
    ("Copper",    "HG=F", "price"),
]

CRYPTO = [
    ("Bitcoin",  "BTC-USD", "crypto"),
    ("Ethereum", "ETH-USD", "price"),
    ("Solana",   "SOL-USD", "price"),
    ("XRP",      "XRP-USD", "price"),
]

# All ticker-bar instruments (subset shown in scrolling header)
TICKER_BAR = [
    ("SPX", "^GSPC"), ("NDX", "^NDX"), ("DJIA", "^DJI"), ("RUT", "^RUT"),
    ("VIX", "^VIX"), ("US10Y", "^TNX"), ("US2Y", "^IRX"),
    ("DXY", "DX-Y.NYB"), ("EURUSD", "EURUSD=X"), ("USDJPY", "JPY=X"),
    ("BTC", "BTC-USD"), ("ETH", "ETH-USD"),
    ("WTI", "CL=F"), ("BRENT", "BZ=F"), ("GOLD", "GC=F"),
    ("SILVER", "SI=F"), ("COPPER", "HG=F"), ("NAT GAS", "NG=F"),
]

# RSS feeds for headlines
RSS_FEEDS = [
    ("REUTERS",   "https://feeds.reuters.com/reuters/businessNews"),
    ("CNBC",      "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("MARKETWATCH","https://www.marketwatch.com/feeds/topstories"),
    ("FED",       "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("TREASURY",  "https://home.treasury.gov/news/press-releases/feed"),
]

# ----------------------------------------------------------------------------
# DATA FETCH
# ----------------------------------------------------------------------------

def fetch_quote(symbol):
    """Return (last, change, pct_change) or (None, None, None) on failure."""
    try:
        t = yf.Ticker(symbol)
        # fast_info is faster and lighter than .info
        last = t.fast_info.get("last_price")
        prev = t.fast_info.get("previous_close")
        if last is None or prev is None or prev == 0:
            # fallback to history
            hist = t.history(period="2d")
            if len(hist) >= 2:
                last = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2])
            elif len(hist) == 1:
                last = float(hist["Close"].iloc[-1])
                prev = float(hist["Open"].iloc[-1])
            else:
                return None, None, None
        chg = last - prev
        pct = (chg / prev) * 100 if prev else 0
        return float(last), float(chg), float(pct)
    except Exception as e:
        print(f"  ! {symbol}: {e}", file=sys.stderr)
        return None, None, None


def fmt_price(v, kind="price"):
    if v is None:
        return "—"
    if kind == "yield":
        return f"{v:.3f}%"  # yfinance returns ^TNX/^FVX/^IRX/^TYX as percent already
    if kind == "fx":
        return f"{v:.4f}"
    if kind == "fx2":
        return f"{v:.2f}"
    if kind == "crypto":
        return f"{v:,.0f}"
    if v >= 1000:
        return f"{v:,.2f}"
    return f"{v:.2f}"


def fmt_pct(p):
    if p is None:
        return ("—", "")
    cls = "up" if p >= 0 else "down"
    sign = "+" if p >= 0 else ""
    return (f"{sign}{p:.2f}%", cls)


def fmt_chg(c, kind="price"):
    if c is None:
        return ("—", "")
    cls = "up" if c >= 0 else "down"
    sign = "+" if c >= 0 else ""
    if kind == "yield":
        # 1 percentage point = 100 basis points
        return (f"{sign}{c*100:.1f}bp", cls)
    if abs(c) < 1:
        return (f"{sign}{c:.3f}", cls)
    return (f"{sign}{c:,.2f}", cls)


# ----------------------------------------------------------------------------
# HTML BUILDERS
# ----------------------------------------------------------------------------

def build_table_row(name, last, chg, pct, kind):
    """Standard 4-col table row: Name, Last, Chg, %"""
    last_s = fmt_price(last, kind)
    chg_s, chg_cls = fmt_chg(chg, kind)
    pct_s, pct_cls = fmt_pct(pct)
    pill = f'<span class="pct-pill {pct_cls}">{pct_s}</span>' if pct_cls else pct_s
    return (
        f'<tr><td class="sym-cell">{html.escape(name)}</td>'
        f'<td class="r">{last_s}</td>'
        f'<td class="r {chg_cls}">{chg_s}</td>'
        f'<td class="r">{pill}</td></tr>'
    )


def build_table_row_3col(name, last, pct, kind):
    """3-col: Name, Last, %"""
    last_s = fmt_price(last, kind)
    pct_s, pct_cls = fmt_pct(pct)
    pill = f'<span class="pct-pill {pct_cls}">{pct_s}</span>' if pct_cls else pct_s
    return (
        f'<tr><td class="sym-cell">{html.escape(name)}</td>'
        f'<td class="r">{last_s}</td>'
        f'<td class="r">{pill}</td></tr>'
    )


def build_yield_row(name, last, chg):
    """Yield row: tenor, yield, bp change"""
    if last is None:
        return f'<tr><td class="sym-cell">{html.escape(name)}</td><td class="r">—</td><td class="r">—</td></tr>'
    yield_pct = last
    chg_bp_val = (chg or 0) * 100
    chg_cls = "up" if chg_bp_val >= 0 else "down"
    chg_sign = "+" if chg_bp_val >= 0 else ""
    return (
        f'<tr><td class="sym-cell">{html.escape(name)}</td>'
        f'<td class="r">{yield_pct:.3f}%</td>'
        f'<td class="r {chg_cls}">{chg_sign}{chg_bp_val:.1f}</td></tr>'
    )


def build_ticker_item(label, symbol):
    last, chg, pct = fetch_quote(symbol)
    if last is None:
        return ""
    if symbol in ("^TNX", "^FVX", "^IRX", "^TYX"):
        # show as yield
        last_str = f"{last:.3f}%"
        chg_bp = (chg or 0) * 100
        cls = "up" if chg_bp >= 0 else "down"
        sign = "+" if chg_bp >= 0 else ""
        chg_str = f"{sign}{chg_bp:.1f}bp"
    else:
        last_str = fmt_price(last, "crypto" if "BTC" in symbol or "ETH" in symbol else "price")
        pct_str, cls = fmt_pct(pct)
        chg_str = pct_str
    return (
        f'<span class="ticker-item">'
        f'<span class="sym">{html.escape(label)}</span>'
        f'<span class="px">{last_str}</span>'
        f'<span class="{cls}">{chg_str}</span>'
        f'</span>'
    )


def build_market_sessions():
    """Compute open/closed for NYSE, LSE, TSE based on each exchange's local hours.
    Holidays are not handled — weekend-aware only."""
    now_utc = datetime.now(timezone.utc)

    def is_open(tz, open_hm, close_hm):
        local = now_utc.astimezone(tz)
        if local.weekday() >= 5:  # Sat/Sun
            return False
        hm = (local.hour, local.minute)
        return open_hm <= hm < close_hm

    sessions = [
        ("NYSE", is_open(NY,     (9, 30), (16, 0))),
        ("LSE",  is_open(LONDON, (8, 0),  (16, 30))),
        ("TSE",  is_open(TOKYO,  (9, 0),  (15, 0))),
    ]
    parts = []
    for name, open_now in sessions:
        cls = "up" if open_now else "down"
        label = "OPEN" if open_now else "CLOSED"
        parts.append(f'<span>{name}: <span class="{cls}">{label}</span></span>')
    return "\n      ".join(parts)


def build_headlines():
    items = []
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:2]:  # top 2 from each source
                title = entry.get("title", "").strip()
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    ts = datetime(*published[:6])
                    time_str = ts.strftime("%b %d %H:%M")
                else:
                    time_str = ""
                if title:
                    items.append((ts if published else datetime.min, source, time_str, title, entry.get("link", "#")))
        except Exception as e:
            print(f"  ! RSS {source}: {e}", file=sys.stderr)

    # sort by datetime descending, take top 8
    items.sort(key=lambda x: x[0], reverse=True)
    items = items[:8]

    html_parts = []
    for _, source, time_str, title, link in items:
        html_parts.append(
            f'<a href="{html.escape(link)}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">'
            f'<div class="headline">'
            f'<div class="headline-meta"><span class="source-tag">{html.escape(source)}</span><span>{html.escape(time_str)}</span></div>'
            f'<div class="headline-text">{html.escape(title)}</div>'
            f'</div></a>'
        )
    return "\n".join(html_parts) if html_parts else '<div class="headline"><div class="headline-text" style="color:var(--text-dim)">Headlines unavailable.</div></div>'


# ----------------------------------------------------------------------------
# MAIN BUILD
# ----------------------------------------------------------------------------

def build_section(items, builder, kind_override=None):
    rows = []
    for entry in items:
        name, sym, kind = entry
        last, chg, pct = fetch_quote(sym)
        rows.append(builder(name, last, chg, pct, kind_override or kind))
    return "\n".join(rows)


def main():
    print("HOWL STREET updater — fetching data...")

    # Fetch all sections
    print("  US equities...")
    us_rows = []
    for name, sym, kind in US_EQUITIES:
        last, chg, pct = fetch_quote(sym)
        us_rows.append(build_table_row(name, last, chg, pct, kind))

    print("  Global indices...")
    global_rows = []
    for name, sym, kind in GLOBAL_INDICES:
        last, chg, pct = fetch_quote(sym)
        global_rows.append(build_table_row_3col(name, last, pct, kind))

    print("  Treasuries...")
    treas_rows = []
    for name, sym, _ in TREASURIES:
        last, chg, _ = fetch_quote(sym)
        treas_rows.append(build_yield_row(name, last, chg))

    print("  FX...")
    fx_rows = []
    for name, sym, kind in FX_PAIRS:
        last, chg, pct = fetch_quote(sym)
        fx_rows.append(build_table_row_3col(name, last, pct, kind))

    print("  Commodities...")
    cmdty_rows = []
    for name, sym, kind in COMMODITIES:
        last, chg, pct = fetch_quote(sym)
        cmdty_rows.append(build_table_row_3col(name, last, pct, kind))

    print("  Crypto...")
    crypto_rows = []
    for name, sym, kind in CRYPTO:
        last, chg, pct = fetch_quote(sym)
        crypto_rows.append(build_table_row_3col(name, last, pct, kind))

    print("  Ticker bar...")
    ticker_items = [build_ticker_item(lbl, sym) for lbl, sym in TICKER_BAR]
    ticker_html = "\n".join(t for t in ticker_items if t)

    print("  Headlines...")
    headlines_html = build_headlines()

    print("  Market sessions...")
    sessions_html = build_market_sessions()

    # Timestamp — use actual America/New_York tz so DST is handled (EST in winter, EDT in summer)
    now_ny = datetime.now(NY)
    tz_label = now_ny.tzname()  # "EST" or "EDT"
    ts_str = now_ny.strftime("%b %d %H:%M ") + tz_label
    ts_short = now_ny.strftime("%H:%M ") + tz_label

    # Load template, fill placeholders
    print("  Building HTML...")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    output = (
        template
        .replace("{{TICKER_BAR}}", ticker_html)
        .replace("{{US_EQUITIES}}", "\n".join(us_rows))
        .replace("{{GLOBAL_INDICES}}", "\n".join(global_rows))
        .replace("{{TREASURIES}}", "\n".join(treas_rows))
        .replace("{{FX}}", "\n".join(fx_rows))
        .replace("{{COMMODITIES}}", "\n".join(cmdty_rows))
        .replace("{{CRYPTO}}", "\n".join(crypto_rows))
        .replace("{{HEADLINES}}", headlines_html)
        .replace("{{MARKET_SESSIONS}}", sessions_html)
        .replace("{{TIMESTAMP}}", ts_str)
        .replace("{{TIMESTAMP_SHORT}}", ts_short)
    )

    OUTPUT_PATH.write_text(output, encoding="utf-8")
    print(f"  Wrote {OUTPUT_PATH} ({len(output):,} bytes)")
    print(f"  Updated at {ts_str}")


if __name__ == "__main__":
    main()
