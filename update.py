"""
HOWL STREET — site updater
Pulls live market data + headlines, rebuilds index.html from template.
Runs on GitHub Actions on a schedule.
"""

import os
import socket
import sys
import html
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, date
from pathlib import Path

import yfinance as yf
import feedparser
from zoneinfo import ZoneInfo

# Cap per-feed network wait so one slow source can't stall the build
socket.setdefaulttimeout(15)

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent
TEMPLATE_PATH = REPO_ROOT / "template.html"
OUTPUT_PATH = REPO_ROOT / "index.html"
HERO_PATH = REPO_ROOT / "hero.md"
SITEMAP_PATH = REPO_ROOT / "sitemap.xml"

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")
BERLIN = ZoneInfo("Europe/Berlin")
PARIS = ZoneInfo("Europe/Paris")
HONG_KONG = ZoneInfo("Asia/Hong_Kong")
SHANGHAI = ZoneInfo("Asia/Shanghai")

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()

# NYSE holidays. Hardcoded list — extend as years go on.
NYSE_HOLIDAYS = {
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),
    date(2027, 6, 18),   # Juneteenth observed
    date(2027, 7, 5),    # Independence Day observed
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),  # Christmas Day observed
}

# Tickers we pull. Format: (display_name, yfinance_symbol, format_type)
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

# Treasuries: (display_name, yfinance_fallback_symbol, fred_series_id)
TREASURIES = [
    ("US 2Y",  "^IRX",  "DGS2"),
    ("US 5Y",  "^FVX",  "DGS5"),
    ("US 10Y", "^TNX",  "DGS10"),
    ("US 30Y", "^TYX",  "DGS30"),
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

# S&P sector ETFs — quick read on where money is moving today
SECTORS = [
    ("Technology",     "XLK",  "price"),
    ("Financials",     "XLF",  "price"),
    ("Energy",         "XLE",  "price"),
    ("Health Care",    "XLV",  "price"),
    ("Industrials",    "XLI",  "price"),
    ("Materials",      "XLB",  "price"),
    ("Utilities",      "XLU",  "price"),
    ("Staples",        "XLP",  "price"),
    ("Discretionary",  "XLY",  "price"),
    ("Real Estate",    "XLRE", "price"),
    ("Comm Services",  "XLC",  "price"),
]

# The names that drive the index
MEGACAPS = [
    ("Apple",        "AAPL",  "price"),
    ("Microsoft",    "MSFT",  "price"),
    ("Nvidia",       "NVDA",  "price"),
    ("Alphabet",     "GOOGL", "price"),
    ("Amazon",       "AMZN",  "price"),
    ("Meta",         "META",  "price"),
    ("Tesla",        "TSLA",  "price"),
    ("Berkshire B",  "BRK-B", "price"),
    ("JPMorgan",     "JPM",   "price"),
    ("UnitedHealth", "UNH",   "price"),
]

TICKER_BAR = [
    ("SPX", "^GSPC"), ("NDX", "^NDX"), ("DJIA", "^DJI"), ("RUT", "^RUT"),
    ("VIX", "^VIX"), ("US10Y", "^TNX"), ("US2Y", "^IRX"),
    ("DXY", "DX-Y.NYB"), ("EURUSD", "EURUSD=X"), ("USDJPY", "JPY=X"),
    ("BTC", "BTC-USD"), ("ETH", "ETH-USD"),
    ("WTI", "CL=F"), ("BRENT", "BZ=F"), ("GOLD", "GC=F"),
    ("SILVER", "SI=F"), ("COPPER", "HG=F"), ("NAT GAS", "NG=F"),
]

RSS_FEEDS = [
    # Official institutions — highest signal, primary sources
    ("FED",            "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("TREASURY",       "https://home.treasury.gov/news/press-releases/feed"),
    ("BIS",            "https://www.bis.org/rss/home.rss"),
    ("IMF",            "https://www.imf.org/en/News/RSS?Language=ENG"),

    # Major global wires
    ("REUTERS",        "https://news.google.com/rss/search?q=site%3Areuters.com+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("AP",             "https://news.google.com/rss/search?q=site%3Aapnews.com+business+OR+economy+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("BLOOMBERG",      "https://news.google.com/rss/search?q=site%3Abloomberg.com+markets+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("WSJ",            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),

    # UK / Europe
    ("BBC",            "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("GUARDIAN",       "https://www.theguardian.com/business/rss"),
    ("TELEGRAPH",      "https://www.telegraph.co.uk/business/rss.xml"),
    ("DW",             "https://rss.dw.com/rdf/rss-en-bus"),
    ("EURONEWS",       "https://www.euronews.com/rss?level=vertical&name=business"),

    # Asia / Pacific
    ("NIKKEI",         "https://news.google.com/rss/search?q=site%3Aasia.nikkei.com+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("SCMP",           "https://news.google.com/rss/search?q=site%3Ascmp.com+business+OR+economy+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("CAIXIN",         "https://www.caixinglobal.com/rss/"),
    ("ASIA TIMES",     "https://asiatimes.com/feed/"),
    ("KOREA HERALD",   "https://news.google.com/rss/search?q=site%3Akoreaherald.com+business+OR+economy+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("ECON TIMES IN",  "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms"),
    ("TIMES INDIA",    "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms"),
    ("ABC AUSTRALIA",  "https://www.abc.net.au/news/feed/51892/rss.xml"),
    ("SMH",            "https://www.smh.com.au/rss/business.xml"),

    # Middle East
    ("AL JAZEERA",     "https://www.aljazeera.com/xml/rss/all.xml"),
    ("TIMES ISRAEL",   "https://www.timesofisrael.com/feed/"),

    # Russia (independent, not state propaganda)
    ("MOSCOW TIMES",   "https://www.themoscowtimes.com/rss/news"),

    # Americas (non-US)
    ("GLOBE & MAIL",   "https://www.theglobeandmail.com/business/rss/"),

    # More US — broader business coverage
    ("YAHOO FINANCE",  "https://finance.yahoo.com/news/rssindex"),
    ("FORBES",         "https://www.forbes.com/business/feed/"),
    ("BUSINESS INSIDER","https://www.businessinsider.com/rss"),
    ("FORTUNE",        "https://fortune.com/feed/"),
    ("AXIOS",          "https://api.axios.com/feed/markets"),
    ("THE HILL",       "https://thehill.com/policy/finance/feed/"),
    ("POLITICO US",    "https://www.politico.com/rss/economy.xml"),

    # More Europe
    ("FRANCE 24",      "https://www.france24.com/en/business-economic/rss"),
    ("SKY NEWS",       "https://feeds.skynews.com/feeds/rss/business.xml"),
    ("POLITICO EU",    "https://www.politico.eu/feed/"),
    ("SPIEGEL",        "https://www.spiegel.de/international/index.rss"),

    # More Asia
    ("CHANNEL NEWS",   "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6936"),
    ("JAPAN TIMES",    "https://www.japantimes.co.jp/category/business/feed/"),
    ("STRAITS TIMES",  "https://www.straitstimes.com/news/business/rss.xml"),
    ("MINT INDIA",     "https://www.livemint.com/rss/markets"),
    ("BIZ STD INDIA",  "https://www.business-standard.com/rss/markets-106.rss"),
    ("HINDU BIZLINE",  "https://www.thehindubusinessline.com/economy/feeder/default.rss"),

    # More Middle East
    ("ARAB NEWS",      "https://www.arabnews.com/rss.xml"),
    ("GULF NEWS",      "https://gulfnews.com/business/rss-feeds"),
    ("THE NATIONAL",   "https://www.thenationalnews.com/business/rss/"),

    # Africa
    ("ALL AFRICA",     "https://allafrica.com/tools/headlines/rdf/business/headlines.rdf"),
    ("BIZNEWS SA",     "https://www.biznews.com/feed"),

    # Latin America
    ("MERCOPRESS",     "https://en.mercopress.com/rss"),

    # More Canada
    ("CBC BUSINESS",   "https://rss.cbc.ca/lineup/business.xml"),
    ("FINANCIAL POST", "https://financialpost.com/feed"),

    # Crypto-specific (relevant for digital currency / CBDC themes)
    ("COINDESK",       "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("COINTELEGRAPH",  "https://cointelegraph.com/rss"),
    ("THE BLOCK",      "https://www.theblock.co/rss.xml"),

    # Specialized markets / commodities / FX
    ("INVESTING",      "https://www.investing.com/rss/news.rss"),
    ("MINING",         "https://www.mining.com/feed/"),
    ("FOREXLIVE",      "https://www.forexlive.com/feed/news"),

    # Macro / geopolitics specialty
    ("FOREIGN POLICY", "https://foreignpolicy.com/feed/"),
    ("PROJECT SYND",   "https://www.project-syndicate.org/rss"),

    # US right-leaning / contrarian
    ("FOX BUSINESS",   "https://moxie.foxbusiness.com/google-publisher/markets.xml"),
    ("NY POST",        "https://nypost.com/business/feed/"),
    ("ZEROHEDGE",      "https://www.zerohedge.com/fullrss.xml"),
    ("EPOCH TIMES",    "https://www.theepochtimes.com/c-business/feed"),
    ("FREE PRESS",     "https://www.thefp.com/feed"),

    # Specialized — gold / energy / FX matter for macro themes
    ("KITCO",          "https://www.kitco.com/rss/KitcoNews.xml"),
    ("OILPRICE",       "https://oilprice.com/rss/main"),

    # US center / left
    ("NPR",            "https://feeds.npr.org/1006/rss.xml"),

    # US retail
    ("CNBC",           "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("MARKETWATCH",    "https://feeds.content.dowjones.io/public/rss/mw_topstories"),

    # ── Round 2 expansion: more US national ──
    ("USA TODAY",      "https://rssfeeds.usatoday.com/usatoday-newstopstories"),
    ("LA TIMES",       "https://www.latimes.com/business/rss2.0.xml"),
    ("WAPO",           "https://feeds.washingtonpost.com/rss/business"),
    ("WASH TIMES",     "https://www.washingtontimes.com/rss/headlines/business/"),
    ("DAILY CALLER",   "https://dailycaller.com/section/business/feed/"),
    ("REALCLEAR",      "https://www.realclearmarkets.com/index.xml"),
    ("BENZINGA",       "https://www.benzinga.com/feed"),

    # Think tanks / policy shops (broad spectrum)
    ("AEI",            "https://www.aei.org/feed/"),
    ("CATO",           "https://www.cato.org/rss/recent-content"),
    ("ATLANTIC CNCL",  "https://www.atlanticcouncil.org/feed/"),
    ("CSIS",           "https://www.csis.org/analysis/rss.xml"),

    # Europe additions
    ("LE MONDE",       "https://www.lemonde.fr/en/rss/une.xml"),
    ("SWISSINFO",      "https://www.swissinfo.ch/eng/rss-news"),
    ("ANSA ITALY",     "https://www.ansa.it/english/news/business_economy/business_economy_rss.xml"),

    # Asia additions
    ("BANGKOK POST",   "https://www.bangkokpost.com/rss/data/business.xml"),
    ("INQUIRER PH",    "https://business.inquirer.net/feed/"),
    ("MAINICHI JP",    "https://mainichi.jp/english/rss/etc/etc-business.rss"),
    ("YONHAP",         "https://en.yna.co.kr/RSS/economy.xml"),

    # Middle East additions
    ("HAARETZ",        "https://www.haaretz.com/cmlink/1.4605045"),
    ("AL ARABIYA",     "https://english.alarabiya.net/.mrss/en.xml"),

    # Africa additions
    ("DAILY MAVERICK", "https://www.dailymaverick.co.za/dmrss/"),
    ("EAST AFRICAN",   "https://www.theeastafrican.co.ke/rss/business/index.xml"),
    ("PREMIUM TIMES",  "https://www.premiumtimesng.com/feed"),

    # Latin America additions
    ("BA TIMES",       "https://www.batimes.com.ar/rss/news.xml"),
    ("RIO TIMES",      "https://www.riotimesonline.com/feed/"),

    # Specialized markets / FX / shipping (highly relevant to macro themes)
    ("TRADING ECON",   "https://tradingeconomics.com/calendar/rss"),
    ("DAILYFX",        "https://www.dailyfx.com/feeds/market-news"),
    ("FXSTREET",       "https://www.fxstreet.com/rss/news"),
    ("HELLENIC SHIP",  "https://www.hellenicshippingnews.com/feed/"),

    # More crypto (CBDC + digital currency themes)
    ("BITCOIN MAG",    "https://bitcoinmagazine.com/feed"),
    ("DECRYPT",        "https://decrypt.co/feed"),
]

# Used by the auto Loudest Howl picker. Weighted by signal quality (institutional
# credibility + reporting depth), NOT by political slant. Diverse perspectives are
# included on purpose — the picker rewards substance, the wire panel shows breadth.
SOURCE_WEIGHT = {
    # Official primary sources
    "FED":          6, "TREASURY":     6, "BIS":          6, "IMF":          6,
    # Major global wires
    "REUTERS":      5, "AP":           5, "BLOOMBERG":    5, "WSJ":          5,
    # UK / Europe
    "BBC":          4, "GUARDIAN":     4, "TELEGRAPH":    3, "DW":           3,
    "EURONEWS":     3, "FRANCE 24":    3, "SKY NEWS":     3, "POLITICO EU":  3, "SPIEGEL":     3,
    # Asia / Pacific
    "NIKKEI":       4, "SCMP":         3, "CAIXIN":       4, "ASIA TIMES":   3,
    "KOREA HERALD": 3, "ECON TIMES IN":4, "TIMES INDIA":  2, "ABC AUSTRALIA":4, "SMH":          3,
    "CHANNEL NEWS": 4, "JAPAN TIMES":  3, "STRAITS TIMES":3, "MINT INDIA":   3,
    "BIZ STD INDIA":3, "HINDU BIZLINE":3,
    # Middle East
    "AL JAZEERA":   4, "TIMES ISRAEL": 3, "ARAB NEWS":    3, "GULF NEWS":    3, "THE NATIONAL": 3,
    # Russia (independent)
    "MOSCOW TIMES": 3,
    # Africa
    "ALL AFRICA":   3, "BIZNEWS SA":   2,
    # Latin America
    "MERCOPRESS":   2,
    # Americas (non-US)
    "GLOBE & MAIL": 4, "CBC BUSINESS": 4, "FINANCIAL POST":3,
    # Crypto / digital currency
    "COINDESK":     3, "COINTELEGRAPH":2, "THE BLOCK":    3,
    # Specialized markets / commodities / FX
    "INVESTING":    2, "MINING":       3, "FOREXLIVE":    3,
    # Macro / geopolitics specialty
    "FOREIGN POLICY":4, "PROJECT SYND":3,
    # US right-leaning / contrarian
    "FOX BUSINESS": 3, "NY POST":      2, "ZEROHEDGE":    2, "EPOCH TIMES":  2, "FREE PRESS":   3,
    # Specialized commodity / energy
    "KITCO":        3, "OILPRICE":     3,
    # US center / left
    "NPR":          3,
    # US broader business
    "YAHOO FINANCE":2, "FORBES":       2, "BUSINESS INSIDER":2, "FORTUNE":   2,
    "AXIOS":        3, "THE HILL":     2, "POLITICO US":  3,
    # US retail (kept for variety; rarely wins Loudest Howl)
    "CNBC":         1, "MARKETWATCH":  1,
    # Round 2 expansion
    "USA TODAY":    2, "LA TIMES":     3, "WAPO":         4, "WASH TIMES":   2,
    "DAILY CALLER": 1, "REALCLEAR":    2, "BENZINGA":     1,
    "AEI":          3, "CATO":         3, "ATLANTIC CNCL":4, "CSIS":         4,
    "LE MONDE":     4, "SWISSINFO":    3, "ANSA ITALY":   3,
    "BANGKOK POST": 3, "INQUIRER PH":  3, "MAINICHI JP":  3, "YONHAP":       3,
    "HAARETZ":      3, "AL ARABIYA":   3,
    "DAILY MAVERICK":3, "EAST AFRICAN":3, "PREMIUM TIMES":3,
    "BA TIMES":     2, "RIO TIMES":    2,
    "TRADING ECON": 4, "DAILYFX":      3, "FXSTREET":     3, "HELLENIC SHIP":3,
    "BITCOIN MAG":  2, "DECRYPT":      2,
}

# Keyword score boosts (lowercase, substring match against title).
# Heavily weights hard-data releases, central bank action, real-world events with
# financial impact, and macro/monetary-system shifts.
KEYWORD_BOOSTS = {
    # Central banks / rate decisions
    "fomc": 5, "fed ": 3, "powell": 3, "ecb": 4, "lagarde": 3, "boj": 4, "ueda": 3,
    "boe": 3, "pboc": 4, "rba": 3, "snb": 2, "rate cut": 4, "rate hike": 4,
    "rate decision": 4, "interest rate": 3, "monetary policy": 3, "qe": 2, "qt": 2,
    # Hard-data releases
    "cpi": 4, "ppi": 3, "pce": 4, "core inflation": 3, "inflation": 2,
    "gdp": 3, "jobs report": 4, "nonfarm": 3, "payrolls": 4, "unemployment": 3,
    "retail sales": 2, "ism": 2, "pmi": 2, "consumer confidence": 2,
    # Trade / sanctions / geopolitics with market impact
    "tariff": 4, "sanction": 3, "embargo": 3, "trade war": 4, "export control": 3,
    "war": 3, "invasion": 3, "ceasefire": 2, "missile": 2, "strike": 2,
    # Specific shipping / energy chokepoints
    "strait of hormuz": 5, "hormuz": 4, "red sea": 4, "houthi": 4, "suez": 3,
    "panama canal": 3, "shipping": 1,
    # Energy / commodities
    "opec": 4, "opec+": 4, "saudi": 2, "crude": 2, "natural gas": 2, "lng": 2,
    "oil price": 3, "gas price": 2,
    # Crisis / systemic
    "crisis": 3, "default": 4, "bailout": 4, "downgrade": 3, "credit rating": 3,
    "bank failure": 4, "liquidity": 2, "contagion": 3, "systemic": 3,
    # Monetary-system shifts (CBDCs, de-dollarization, reserve regime)
    "cbdc": 5, "central bank digital": 5, "digital dollar": 5, "digital euro": 4,
    "digital yuan": 4, "e-cny": 4, "digital pound": 3,
    "brics": 4, "de-dollarization": 5, "dedollarization": 5, "petroyuan": 4,
    "reserve currency": 4, "dollar hegemony": 4, "dollar dominance": 3,
    "imf": 3, "sdr": 4, "special drawing rights": 5, "world bank": 2, "bis ": 3,
    "mbridge": 5, "swift alternative": 4, "cross-border payment": 4,
    "wef": 3, "davos": 3, "g7": 2, "g20": 2,
    # Digital-ID / surveillance-finance intersection
    "digital id": 4, "digital identity": 4, "biometric": 2, "social credit": 3,
    # Corporate / M&A
    "earnings": 1, "guidance": 1, "ipo": 2, "merger": 2, "acquisition": 2,
    "buyout": 2, "lbo": 2, "spinoff": 2, "bankruptcy": 3,
    # Regions
    "china": 1, "russia": 2, "ukraine": 2, "iran": 3, "israel": 2, "gaza": 2,
    "taiwan": 2, "north korea": 2, "venezuela": 1, "argentina": 1,
    # Magnitude / movement
    "trillion": 2, "billion": 1, "record high": 2, "record low": 2, "all-time": 2,
    "selloff": 2, "crash": 3, "rout": 2,
}

# Penalize click-bait + pundit content + speculation framing
# Specific phrases only — broad ones like "here's what" caught legitimate
# earnings-preview reporting ("Here's what Wall Street expects").
KEYWORD_PENALTIES = {
    "stocks to buy": -5, "stocks to watch": -4, "watchlist": -4, "best stocks": -4,
    "what to know": -4, "things to know": -4, "what to watch": -3,
    "wall street loves": -4, "10 things": -4, "5 things": -4, "3 things": -3,
    "cramer": -3, "jim cramer": -3,
    "should you": -3, "is it time": -3,
}

# Sources whose feed is finance/markets-focused — items always pass the
# relevance gate even without explicit keyword matches.
FINANCIAL_SOURCES = {
    # Institutional
    "FED", "TREASURY", "BIS", "IMF",
    # Markets-only / business-section-only feeds
    "BLOOMBERG", "WSJ", "MARKETWATCH",
    "AP",
    "BBC", "GUARDIAN", "TELEGRAPH", "DW", "EURONEWS", "FRANCE 24", "SKY NEWS",
    "SMH", "ECON TIMES IN", "MINT INDIA", "BIZ STD INDIA", "HINDU BIZLINE",
    "JAPAN TIMES", "STRAITS TIMES", "CHANNEL NEWS",
    "GLOBE & MAIL", "CBC BUSINESS", "FINANCIAL POST",
    "GULF NEWS", "THE NATIONAL",
    "ALL AFRICA", "BIZNEWS SA",
    "CAIXIN", "FOX BUSINESS", "NY POST", "EPOCH TIMES",
    "YAHOO FINANCE", "FORBES", "BUSINESS INSIDER", "FORTUNE",
    "INVESTING", "MINING", "FOREXLIVE",
    "COINDESK", "COINTELEGRAPH", "THE BLOCK",
    "AXIOS", "THE HILL", "POLITICO US", "POLITICO EU", "FREE PRESS",
    # Round 2: business-section feeds
    "LA TIMES", "WAPO", "WASH TIMES", "DAILY CALLER", "REALCLEAR", "BENZINGA",
    "ANSA ITALY", "BANGKOK POST", "INQUIRER PH", "MAINICHI JP", "YONHAP",
    "HAARETZ", "EAST AFRICAN", "TRADING ECON", "DAILYFX", "FXSTREET",
    "HELLENIC SHIP", "BITCOIN MAG", "DECRYPT",
    # Commodities/macro focused
    "KITCO", "OILPRICE", "ZEROHEDGE",
}

# Region classification for the per-region wire cap and regional desk panels.
SOURCE_REGION = {
    # United States
    "FED": "US", "TREASURY": "US", "IMF": "US",
    "BLOOMBERG": "US", "WSJ": "US", "AP": "US", "MARKETWATCH": "US",
    "CNBC": "US", "NPR": "US", "NY POST": "US", "FOX BUSINESS": "US",
    "ZEROHEDGE": "US", "EPOCH TIMES": "US", "KITCO": "US", "OILPRICE": "US",
    "FREE PRESS": "US", "YAHOO FINANCE": "US", "FORBES": "US",
    "BUSINESS INSIDER": "US", "FORTUNE": "US", "AXIOS": "US",
    "THE HILL": "US", "POLITICO US": "US", "USA TODAY": "US",
    "LA TIMES": "US", "WAPO": "US", "WASH TIMES": "US", "DAILY CALLER": "US",
    "REALCLEAR": "US", "BENZINGA": "US", "AEI": "US", "CATO": "US",
    "ATLANTIC CNCL": "US", "CSIS": "US", "INVESTING": "US",
    "BITCOIN MAG": "US", "DECRYPT": "US", "COINDESK": "US",
    "COINTELEGRAPH": "US", "THE BLOCK": "US", "FOREXLIVE": "US",
    "FOREIGN POLICY": "US", "DAILYFX": "US",
    # Europe (incl. UK + international institutions HQ'd there)
    "BIS": "EU", "REUTERS": "EU", "BBC": "EU", "GUARDIAN": "EU",
    "TELEGRAPH": "EU", "DW": "EU", "EURONEWS": "EU", "FRANCE 24": "EU",
    "SKY NEWS": "EU", "POLITICO EU": "EU", "SPIEGEL": "EU", "LE MONDE": "EU",
    "SWISSINFO": "EU", "ANSA ITALY": "EU", "MOSCOW TIMES": "EU",
    "PROJECT SYND": "EU", "TRADING ECON": "EU", "FXSTREET": "EU",
    "HELLENIC SHIP": "EU",
    # Asia / Pacific
    "NIKKEI": "ASIA", "SCMP": "ASIA", "CAIXIN": "ASIA", "ASIA TIMES": "ASIA",
    "KOREA HERALD": "ASIA", "ECON TIMES IN": "ASIA", "TIMES INDIA": "ASIA",
    "ABC AUSTRALIA": "ASIA", "SMH": "ASIA", "CHANNEL NEWS": "ASIA",
    "JAPAN TIMES": "ASIA", "STRAITS TIMES": "ASIA", "MINT INDIA": "ASIA",
    "BIZ STD INDIA": "ASIA", "HINDU BIZLINE": "ASIA", "BANGKOK POST": "ASIA",
    "INQUIRER PH": "ASIA", "MAINICHI JP": "ASIA", "YONHAP": "ASIA",
    # Middle East
    "AL JAZEERA": "ME", "TIMES ISRAEL": "ME", "ARAB NEWS": "ME",
    "GULF NEWS": "ME", "THE NATIONAL": "ME", "HAARETZ": "ME", "AL ARABIYA": "ME",
    # Africa
    "ALL AFRICA": "AF", "BIZNEWS SA": "AF", "DAILY MAVERICK": "AF",
    "EAST AFRICAN": "AF", "PREMIUM TIMES": "AF",
    # Americas (non-US: Canada + Latin America)
    "GLOBE & MAIL": "AMERICAS", "CBC BUSINESS": "AMERICAS",
    "FINANCIAL POST": "AMERICAS", "MERCOPRESS": "AMERICAS",
    "BA TIMES": "AMERICAS", "RIO TIMES": "AMERICAS",
}

REGION_LABEL = {
    "US": "United States",
    "EU": "Europe",
    "ASIA": "Asia / Pacific",
    "ME": "Middle East",
    "AF": "Africa",
    "AMERICAS": "Americas (Non-US)",
}

# Mixed-content sources. Items must have at least one financial-keyword hit
# (in title or summary) to make it into the wire panel — this filters out
# general-news drift (cartel violence, lifestyle, sports, etc.).
MIXED_CONTENT_SOURCES = {
    "REUTERS",
    "AL JAZEERA", "ARAB NEWS", "TIMES ISRAEL", "MOSCOW TIMES", "AL ARABIYA",
    "NIKKEI", "SCMP", "ASIA TIMES", "KOREA HERALD", "TIMES INDIA",
    "ABC AUSTRALIA",
    "MERCOPRESS", "BA TIMES", "RIO TIMES",
    "SPIEGEL", "LE MONDE", "SWISSINFO",
    "FOREIGN POLICY", "PROJECT SYND",
    "AEI", "CATO", "ATLANTIC CNCL", "CSIS",
    "DAILY MAVERICK", "PREMIUM TIMES",
    "USA TODAY",
    "NPR", "CNBC",
}

# ----------------------------------------------------------------------------
# DATA FETCH
# ----------------------------------------------------------------------------

def fetch_quote(symbol, retries=1):
    """Return (last, change, pct_change) or (None, None, None) on failure. Retries once on transient errors."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(symbol)
            last = t.fast_info.get("last_price")
            prev = t.fast_info.get("previous_close")
            if last is None or prev is None or prev == 0:
                hist = t.history(period="2d")
                if len(hist) >= 2:
                    last = float(hist["Close"].iloc[-1])
                    prev = float(hist["Close"].iloc[-2])
                elif len(hist) == 1:
                    last = float(hist["Close"].iloc[-1])
                    prev = float(hist["Open"].iloc[-1])
                else:
                    raise RuntimeError("no history")
            chg = last - prev
            pct = (chg / prev) * 100 if prev else 0
            return float(last), float(chg), float(pct)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1)
    print(f"  ! {symbol}: {last_err}", file=sys.stderr)
    return None, None, None


def fetch_article_summary(url, timeout=8):
    """Fetch the article URL and pull og:description / meta description.
    Used to populate the Loudest Howl body when the RSS summary is empty
    or just repeats the title (common with Google News-sourced items).
    Returns the description string or None on failure."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; HowlStreet/1.0; +https://howlstreet.github.io)",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            page = resp.read(400_000).decode(charset, errors="replace")
    except Exception as e:
        print(f"    ! summary fetch failed: {e}", file=sys.stderr)
        return None

    patterns = (
        r'<meta[^>]+property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:description["\']',
        r'<meta[^>]+name=["\']twitter:description["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']twitter:description["\']',
        r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']description["\']',
    )
    for pattern in patterns:
        m = re.search(pattern, page, re.IGNORECASE)
        if m:
            desc = html.unescape(m.group(1)).strip()
            if desc and len(desc) > 30:
                return desc
    return None


def fetch_treasury_fred(series_id):
    """Fetch latest two observations from FRED. Returns (yield_pct, change_pct) or (None, None).
    Requires FRED_API_KEY env var; returns (None, None) silently if not set."""
    if not FRED_API_KEY:
        return None, None
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={FRED_API_KEY}"
        f"&file_type=json&sort_order=desc&limit=10"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        valid = []
        for obs in data.get("observations", []):
            v = obs.get("value", ".")
            if v and v != ".":
                try:
                    valid.append(float(v))
                except ValueError:
                    pass
            if len(valid) >= 2:
                break
        if not valid:
            return None, None
        if len(valid) < 2:
            return valid[0], 0.0
        last, prev = valid[0], valid[1]
        return last, last - prev
    except Exception as e:
        print(f"  ! FRED {series_id}: {e}", file=sys.stderr)
        return None, None


def fmt_price(v, kind="price"):
    if v is None:
        return "—"
    if kind == "yield":
        return f"{v:.3f}%"
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
        return (f"{sign}{c*100:.1f}bp", cls)
    if abs(c) < 1:
        return (f"{sign}{c:.3f}", cls)
    return (f"{sign}{c:,.2f}", cls)


# ----------------------------------------------------------------------------
# HTML BUILDERS
# ----------------------------------------------------------------------------

def build_table_row(name, last, chg, pct, kind):
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
    last_s = fmt_price(last, kind)
    pct_s, pct_cls = fmt_pct(pct)
    pill = f'<span class="pct-pill {pct_cls}">{pct_s}</span>' if pct_cls else pct_s
    return (
        f'<tr><td class="sym-cell">{html.escape(name)}</td>'
        f'<td class="r">{last_s}</td>'
        f'<td class="r">{pill}</td></tr>'
    )


def build_yield_row(name, last, chg):
    if last is None:
        return f'<tr><td class="sym-cell">{html.escape(name)}</td><td class="r">—</td><td class="r">—</td></tr>'
    chg_bp_val = (chg or 0) * 100
    chg_cls = "up" if chg_bp_val >= 0 else "down"
    chg_sign = "+" if chg_bp_val >= 0 else ""
    return (
        f'<tr><td class="sym-cell">{html.escape(name)}</td>'
        f'<td class="r">{last:.3f}%</td>'
        f'<td class="r {chg_cls}">{chg_sign}{chg_bp_val:.1f}</td></tr>'
    )


def build_ticker_item(label, symbol):
    last, chg, pct = fetch_quote(symbol)
    if last is None:
        return ""
    if symbol in ("^TNX", "^FVX", "^IRX", "^TYX"):
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


def _exchange_open(tz, open_hm, close_hm, lunch=None, holidays=None):
    """True if the given exchange is currently within its regular session."""
    local = datetime.now(timezone.utc).astimezone(tz)
    if local.weekday() >= 5:
        return False
    if holidays and local.date() in holidays:
        return False
    hm = (local.hour, local.minute)
    if not (open_hm <= hm < close_hm):
        return False
    if lunch and lunch[0] <= hm < lunch[1]:
        return False
    return True


def global_indices_status_label():
    """Compose a label naming which non-US regions are currently trading.
    Examples: 'CLOSED', 'EU LIVE', 'JP·HK·CN LIVE'.
    Holiday lists not maintained for non-US exchanges — weekend-aware only."""
    eu_open = (
        _exchange_open(LONDON, (8, 0), (16, 30))                                # LSE / FTSE
        or _exchange_open(BERLIN, (9, 0), (17, 30))                             # XETRA / DAX
        or _exchange_open(PARIS,  (9, 0), (17, 30))                             # Euronext / CAC
    )
    jp_open = _exchange_open(TOKYO, (9, 0), (15, 0), lunch=((11, 30), (12, 30)))  # TSE / Nikkei
    cn_hk_open = (
        _exchange_open(HONG_KONG, (9, 30), (16, 0), lunch=((12, 0), (13, 0)))   # HKEX / Hang Seng
        or _exchange_open(SHANGHAI, (9, 30), (15, 0), lunch=((11, 30), (13, 0)))  # SSE / Shanghai
    )

    open_regions = []
    if eu_open:
        open_regions.append("EU")
    if jp_open:
        open_regions.append("JP")
    if cn_hk_open:
        open_regions.append("HK·CN")

    if not open_regions:
        return "CLOSED"
    return "·".join(open_regions) + " LIVE"


def is_us_treasury_open():
    """US Treasury cash market — SIFMA recommended hours: 8am-5pm ET, Mon-Fri.
    Honors NYSE holidays as a proxy (Treasury usually follows NYSE closings)."""
    return _exchange_open(NY, (8, 0), (17, 0), holidays=NYSE_HOLIDAYS)


def is_any_major_market_open():
    """True if any of NYSE / LSE / TSE is currently in regular session.
    Drives the header LIVE/STANDBY indicator."""
    return (
        _exchange_open(NY,     (9, 30), (16, 0), holidays=NYSE_HOLIDAYS)
        or _exchange_open(LONDON, (8, 0),  (16, 30))
        or _exchange_open(TOKYO,  (9, 0),  (15, 0), lunch=((11, 30), (12, 30)))
    )


def build_live_indicator():
    """LIVE pulse when a major market is open, STANDBY (dim, no animation) otherwise."""
    if is_any_major_market_open():
        return '<span class="live-dot">LIVE</span>'
    return '<span class="live-dot standby">STANDBY</span>'


def build_market_sessions():
    """NYSE / LSE / TSE open-or-closed indicator for the header."""
    sessions = [
        ("NYSE", _exchange_open(NY,     (9, 30), (16, 0), holidays=NYSE_HOLIDAYS)),
        ("LSE",  _exchange_open(LONDON, (8, 0),  (16, 30))),
        ("TSE",  _exchange_open(TOKYO,  (9, 0),  (15, 0), lunch=((11, 30), (12, 30)))),
    ]
    parts = []
    for name, open_now in sessions:
        cls = "up" if open_now else "down"
        label = "OPEN" if open_now else "CLOSED"
        parts.append(f'<span>{name}: <span class="{cls}">{label}</span></span>')
    return "\n      ".join(parts)


def _clean_title(title, source):
    """Strip ' - SourceName' / ' | SourceName' / ' — SourceName' suffixes that
    Google News appends, and decode HTML entities."""
    title = html.unescape(title).strip()
    # Common suffix patterns
    for sep in (" - ", " — ", " | "):
        idx = title.rfind(sep)
        if idx > 0 and idx >= len(title) - 60:
            tail = title[idx + len(sep):].strip()
            # If the tail looks like a publisher/byline (short-ish, mostly letters), strip it
            if 0 < len(tail) <= 40 and re.match(r"^[A-Za-z0-9 .&'\-]+$", tail):
                title = title[:idx].strip()
                break
    return title


def _clean_summary(raw):
    """Decode entities, strip HTML tags, collapse whitespace."""
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _fetch_one_feed(source_url):
    """Worker for ThreadPoolExecutor. Returns a list of item dicts."""
    source, url = source_url
    epoch_min = datetime(2000, 1, 1, tzinfo=NY)
    out = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:6]:
            title = _clean_title(entry.get("title") or "", source)
            if not title:
                continue
            summary = _clean_summary(entry.get("summary") or "")
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                ts = datetime(*published[:6], tzinfo=timezone.utc).astimezone(NY)
            else:
                ts = epoch_min
            out.append({
                "source": source,
                "title": title,
                "summary": summary,
                "link": entry.get("link", "#"),
                "ts": ts,
            })
    except Exception as e:
        print(f"  ! RSS {source}: {e}", file=sys.stderr)
    return out


def fetch_all_headlines():
    """Fetch every RSS feed in parallel and return a flat list of items.
    Each item: {source, title, summary, link, ts (NY tz)}."""
    items = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        for chunk in ex.map(_fetch_one_feed, RSS_FEEDS):
            items.extend(chunk)
    return items


def _kw_match(text, keyword):
    """Word-boundary match so 'war' doesn't hit 'ward'.
    Multi-word keywords ('strait of hormuz') still match — \b handles each end."""
    return re.search(r'\b' + re.escape(keyword) + r'\b', text) is not None


def is_financially_relevant(item):
    """Gate for the wire panel. Always-finance sources pass automatically; mixed
    sources must have at least one financial keyword in title or summary."""
    if item["source"] in FINANCIAL_SOURCES:
        return True
    text = (item["title"] + " " + item["summary"]).lower()
    return any(_kw_match(text, kw) for kw in KEYWORD_BOOSTS)


def score_item(item):
    """Score a wire item for Loudest Howl candidacy. Higher = more newsworthy."""
    score = SOURCE_WEIGHT.get(item["source"], 1)

    # Recency — lose 1 point per hour, capped at -12
    now = datetime.now(NY)
    age_hours = max(0, (now - item["ts"]).total_seconds() / 3600)
    score -= min(age_hours, 12)

    title_lower = item["title"].lower()
    for kw, bonus in KEYWORD_BOOSTS.items():
        if _kw_match(title_lower, kw):
            score += bonus
    for phrase, penalty in KEYWORD_PENALTIES.items():
        if _kw_match(title_lower, phrase):
            score += penalty

    return score


# Minimum score for an auto-picked Loudest Howl. Below this, hero stays hidden.
HERO_MIN_SCORE = 4.0


def build_hero_auto(items):
    """Pick the highest-scoring recent item and render it as hero.
    Returns empty string if nothing clears the quality threshold."""
    if not items:
        return ""

    now = datetime.now(NY)
    # Limit to last 24h; if nothing recent, don't promote anything
    recent = [i for i in items if (now - i["ts"]).total_seconds() < 24 * 3600]
    if not recent:
        return ""

    scored = sorted(((score_item(i), i) for i in recent), key=lambda x: x[0], reverse=True)
    top_score, top = scored[0]
    if top_score < HERO_MIN_SCORE:
        return ""

    # Summary already cleaned in fetch_all_headlines. Strip the source name
    # if Google News duplicated it at the end.
    summary_text = top["summary"]
    src_lower = top["source"].lower()
    while summary_text.lower().endswith(src_lower):
        summary_text = summary_text[: -len(src_lower)].rstrip(" ,.;:|—-")
    # If the summary is just the title repeated (Google News pattern), discard it.
    if summary_text.strip().lower() == top["title"].strip().lower():
        summary_text = ""
    # If still nothing useful, follow the article URL and grab og:description.
    if not summary_text:
        fetched = fetch_article_summary(top["link"])
        if fetched and fetched.strip().lower() != top["title"].strip().lower():
            summary_text = _clean_summary(fetched)
    # Trim to first sentence — terminal-feel, scannable.
    if summary_text:
        m = re.match(r"^(.{30,}?[.!?])(?:\s|$)", summary_text)
        if m:
            summary_text = m.group(1)
        elif len(summary_text) > 200:
            summary_text = summary_text[:197].rstrip(" ,.;:") + "…"

    label = (
        "LOUDEST HOWL · "
        + top["source"]
        + " · "
        + top["ts"].strftime("%b %d %H:%M ")
        + top["ts"].tzname()
    )

    body_tag = f'  <p class="hero-body">{html.escape(summary_text)}</p>\n' if summary_text else ""

    return (
        f'<a class="hero-link" href="{html.escape(top["link"])}" target="_blank" rel="noopener">\n'
        '<section class="hero">\n'
        f'  <div class="hero-label">▸ {html.escape(label)} <span class="hero-arrow">↗</span></div>\n'
        f'  <h2 class="hero-headline">{html.escape(top["title"])}</h2>\n'
        f'{body_tag}'
        '</section>\n'
        '</a>'
    )


def build_hero_from_md():
    """Read hero.md and render the hero <section>. Empty string if missing or has no headline.

    Format:
        LABEL: LOUDEST HOWL          (optional — auto-generated if missing)
        LINK:  https://example.com   (optional — wraps hero in clickable link)
        # Headline goes here

        Body paragraph goes here. Markdown *emphasis* becomes <em>.
    """
    if not HERO_PATH.exists():
        return ""
    text = HERO_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return ""

    label = None
    link = None
    headline = None
    body_lines = []
    in_body = False
    for line in text.splitlines():
        if not in_body:
            if line.startswith("LABEL:"):
                label = line[len("LABEL:"):].strip()
                continue
            if line.startswith("LINK:"):
                link = line[len("LINK:"):].strip()
                continue
            if line.lstrip().startswith("#"):
                headline = line.lstrip("# ").strip()
                in_body = True
                continue
        else:
            body_lines.append(line)

    if not headline:
        return ""

    body = "\n".join(body_lines).strip()
    body_html = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", html.escape(body))
    headline_html = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", html.escape(headline))

    if not label:
        now_ny = datetime.now(NY)
        label = "LOUDEST HOWL · " + now_ny.strftime("%b %d %H:%M ") + now_ny.tzname()

    label_html = f'<div class="hero-label">▸ {html.escape(label)}'
    if link:
        label_html += ' <span class="hero-arrow">↗</span>'
    label_html += '</div>'

    inner = (
        f'  {label_html}\n'
        f'  <h2 class="hero-headline">{headline_html}</h2>\n'
        f'  <p class="hero-body">{body_html}</p>\n'
    )

    if link:
        return (
            f'<a class="hero-link" href="{html.escape(link)}" target="_blank" rel="noopener">\n'
            '<section class="hero">\n'
            f'{inner}'
            '</section>\n'
            '</a>'
        )
    return (
        '<section class="hero">\n'
        f'{inner}'
        '</section>'
    )


def build_headlines_from_items(items, exclude_link=None, exclude_sources=None,
                                max_per_source=2, max_per_region=3, total=10):
    """Render the wire panel from already-fetched items, sorted recency-first.
    Filters to financially-relevant items; caps both per-source and per-region
    so the panel stays globally diverse instead of US-dominated."""
    pool = [i for i in items if is_financially_relevant(i)]
    if exclude_sources:
        pool = [i for i in pool if i["source"] not in exclude_sources]
    if exclude_link:
        pool = [i for i in pool if i["link"] != exclude_link]
    pool.sort(key=lambda x: x["ts"], reverse=True)

    selected = []
    per_source_count = {}
    per_region_count = {}
    for item in pool:
        if per_source_count.get(item["source"], 0) >= max_per_source:
            continue
        region = SOURCE_REGION.get(item["source"], "OTHER")
        if per_region_count.get(region, 0) >= max_per_region:
            continue
        selected.append(item)
        per_source_count[item["source"]] = per_source_count.get(item["source"], 0) + 1
        per_region_count[region] = per_region_count.get(region, 0) + 1
        if len(selected) >= total:
            break

    html_parts = []
    for item in selected:
        time_str = item["ts"].strftime("%b %d %H:%M") if item["ts"].year > 2001 else ""
        html_parts.append(
            f'<a href="{html.escape(item["link"])}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">'
            f'<div class="headline">'
            f'<div class="headline-meta"><span class="source-tag">{html.escape(item["source"])}</span><span>{html.escape(time_str)}</span></div>'
            f'<div class="headline-text">{html.escape(item["title"])}</div>'
            f'</div></a>'
        )
    return "\n".join(html_parts) if html_parts else '<div class="headline"><div class="headline-text" style="color:var(--text-dim)">Headlines unavailable.</div></div>'


def build_regional_panels(items, exclude_link=None):
    """Per-continent wire panels for the regional desk.
    Returns dict: region_code → rendered HTML for that panel's body."""
    panels = {}
    for region in REGION_LABEL:
        region_items = [
            i for i in items
            if SOURCE_REGION.get(i["source"]) == region
            and is_financially_relevant(i)
            and i["source"] != "TRADING ECON"  # calendar handled separately
            and (not exclude_link or i["link"] != exclude_link)
        ]
        region_items.sort(key=lambda x: x["ts"], reverse=True)

        # 1 per source for diversity in small panels; cap at 4 items
        selected = []
        seen_sources = set()
        for item in region_items:
            if item["source"] in seen_sources:
                continue
            selected.append(item)
            seen_sources.add(item["source"])
            if len(selected) >= 4:
                break

        if not selected:
            panels[region] = (
                '<div class="headline"><div class="headline-text" '
                'style="color:var(--text-dim)">No recent items.</div></div>'
            )
            continue

        html_parts = []
        for item in selected:
            time_str = item["ts"].strftime("%b %d %H:%M") if item["ts"].year > 2001 else ""
            html_parts.append(
                f'<a href="{html.escape(item["link"])}" target="_blank" rel="noopener" '
                f'style="text-decoration:none;color:inherit;">'
                f'<div class="headline">'
                f'<div class="headline-meta">'
                f'<span class="source-tag">{html.escape(item["source"])}</span>'
                f'<span>{html.escape(time_str)}</span>'
                f'</div>'
                f'<div class="headline-text">{html.escape(item["title"])}</div>'
                f'</div></a>'
            )
        panels[region] = "\n".join(html_parts)
    return panels


def build_economic_calendar(items):
    """Render Trading Economics RSS items as a calendar list.
    Most recent calendar entries first; max 12 rows."""
    cal_items = [i for i in items if i["source"] == "TRADING ECON"]
    cal_items.sort(key=lambda x: x["ts"], reverse=True)
    cal_items = cal_items[:12]

    if not cal_items:
        return (
            '<div class="headline"><div class="headline-text" '
            'style="color:var(--text-dim)">Calendar feed unavailable.</div></div>'
        )

    rows = []
    for item in cal_items:
        time_str = item["ts"].strftime("%b %d %H:%M") if item["ts"].year > 2001 else "—"
        # Trading Economics often packs the full event description in title;
        # if there's a useful summary, append it.
        desc = item["summary"]
        desc_html = (
            f'<div style="color:var(--text-dim);font-size:0.85em;margin-top:2px;">'
            f'{html.escape(desc[:140])}</div>' if desc and desc.lower() != item["title"].lower() else ""
        )
        rows.append(
            f'<a href="{html.escape(item["link"])}" target="_blank" rel="noopener" '
            f'style="text-decoration:none;color:inherit;">'
            f'<div class="headline">'
            f'<div class="headline-meta">'
            f'<span class="source-tag" style="color:var(--green);">{html.escape(time_str)}</span>'
            f'</div>'
            f'<div class="headline-text">{html.escape(item["title"])}</div>'
            f'{desc_html}'
            f'</div></a>'
        )
    return "\n".join(rows)


def write_sitemap():
    today = datetime.now(NY).strftime("%Y-%m-%d")
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '  <url>\n'
        '    <loc>https://howlstreet.github.io/</loc>\n'
        f'    <lastmod>{today}</lastmod>\n'
        '    <changefreq>hourly</changefreq>\n'
        '    <priority>1.0</priority>\n'
        '  </url>\n'
        '</urlset>\n'
    )
    SITEMAP_PATH.write_text(content, encoding="utf-8")


# ----------------------------------------------------------------------------
# MAIN BUILD
# ----------------------------------------------------------------------------

def main():
    print("HOWL STREET updater — fetching data...")

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
    used_fred = False
    for name, yf_sym, fred_sym in TREASURIES:
        last, chg = fetch_treasury_fred(fred_sym)
        if last is not None:
            used_fred = True
        else:
            last, chg, _ = fetch_quote(yf_sym)
        treas_rows.append(build_yield_row(name, last, chg))
    if used_fred:
        print("    (FRED)")
    elif FRED_API_KEY:
        print("    (FRED key set but request failed; fell back to yfinance)")
    else:
        print("    (yfinance — set FRED_API_KEY env to use FRED)")

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

    print("  Sectors...")
    sector_rows = []
    for name, sym, kind in SECTORS:
        last, chg, pct = fetch_quote(sym)
        sector_rows.append(build_table_row(name, last, chg, pct, kind))

    print("  Mega-caps...")
    megacap_rows = []
    for name, sym, kind in MEGACAPS:
        last, chg, pct = fetch_quote(sym)
        megacap_rows.append(build_table_row(name, last, chg, pct, kind))

    print("  Ticker bar...")
    ticker_items = [build_ticker_item(lbl, sym) for lbl, sym in TICKER_BAR]
    ticker_html = "\n".join(t for t in ticker_items if t)

    print("  Wires (all feeds)...")
    all_items = fetch_all_headlines()
    print(f"    fetched {len(all_items)} items from {len(RSS_FEEDS)} sources")

    print("  Market sessions...")
    sessions_html = build_market_sessions()
    global_indices_status = global_indices_status_label()
    treasury_status = "LIVE" if is_us_treasury_open() else "CLOSED"
    live_indicator_html = build_live_indicator()

    print("  Hero (Loudest Howl)...")
    hero_html = build_hero_from_md()
    hero_link = None
    if hero_html:
        print("    (manual override from hero.md)")
    else:
        hero_html = build_hero_auto(all_items)
        if hero_html:
            # extract link to dedupe from wire panel
            m = re.search(r'class="hero-link"\s+href="([^"]+)"', hero_html)
            if m:
                hero_link = html.unescape(m.group(1))
            print(f"    (auto-picked from wires)")
        else:
            print("    (nothing cleared the quality threshold — hero hidden)")

    print("  Wire panel...")
    headlines_html = build_headlines_from_items(
        all_items, exclude_link=hero_link, exclude_sources={"TRADING ECON"},
    )

    print("  Regional desk...")
    regional = build_regional_panels(all_items, exclude_link=hero_link)

    print("  Economic calendar...")
    calendar_html = build_economic_calendar(all_items)

    # Timestamp — actual NY tz so DST is handled (EST winter / EDT summer)
    now_ny = datetime.now(NY)
    tz_label = now_ny.tzname()
    ts_str = now_ny.strftime("%b %d %H:%M ") + tz_label
    ts_short = now_ny.strftime("%H:%M ") + tz_label

    print("  Building HTML...")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    output = (
        template
        .replace("{{TICKER_BAR}}", ticker_html)
        .replace("{{HERO_SECTION}}", hero_html)
        .replace("{{US_EQUITIES}}", "\n".join(us_rows))
        .replace("{{GLOBAL_INDICES}}", "\n".join(global_rows))
        .replace("{{TREASURIES}}", "\n".join(treas_rows))
        .replace("{{FX}}", "\n".join(fx_rows))
        .replace("{{COMMODITIES}}", "\n".join(cmdty_rows))
        .replace("{{CRYPTO}}", "\n".join(crypto_rows))
        .replace("{{HEADLINES}}", headlines_html)
        .replace("{{SECTORS}}", "\n".join(sector_rows))
        .replace("{{MEGACAPS}}", "\n".join(megacap_rows))
        .replace("{{REGIONAL_US}}", regional.get("US", ""))
        .replace("{{REGIONAL_EU}}", regional.get("EU", ""))
        .replace("{{REGIONAL_ASIA}}", regional.get("ASIA", ""))
        .replace("{{REGIONAL_ME}}", regional.get("ME", ""))
        .replace("{{REGIONAL_AF}}", regional.get("AF", ""))
        .replace("{{REGIONAL_AMERICAS}}", regional.get("AMERICAS", ""))
        .replace("{{ECONOMIC_CALENDAR}}", calendar_html)
        .replace("{{MARKET_SESSIONS}}", sessions_html)
        .replace("{{LIVE_INDICATOR}}", live_indicator_html)
        .replace("{{GLOBAL_INDICES_STATUS}}", global_indices_status)
        .replace("{{TREASURY_STATUS}}", treasury_status)
        .replace("{{TIMESTAMP}}", ts_str)
        .replace("{{TIMESTAMP_SHORT}}", ts_short)
    )

    OUTPUT_PATH.write_text(output, encoding="utf-8")
    write_sitemap()
    print(f"  Wrote {OUTPUT_PATH} ({len(output):,} bytes)")
    print(f"  Updated at {ts_str}")


if __name__ == "__main__":
    main()
