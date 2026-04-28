"""
HOWL STREET — image card renderer for queue posts.

Each queue post (HOWL OF THE DAY, EARNINGS HOWL, FRESH HOWL, BREAKING,
CORRUPTION, WIRE) gets a custom-rendered 1200x675 PNG card with HOWL
STREET branding so the X post is image-first (à la Polymarket Money,
StockMKTNewz, Bull Theory) instead of a bare link card.

The card is the centerpiece of each tweet: brief tweet text + image
attached. No external link required.

This file lives separately from update.py to keep the rendering logic
contained — adding new card templates here doesn't bloat the main build
script.
"""

import os
import re
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO_ROOT = Path(__file__).parent
CARDS_DIR = REPO_ROOT / "cards"

# Brand palette (matches signals.py / insider_trades.py)
BRAND_BG = "#0a0a0a"
BRAND_FG = "#ffffff"
BRAND_DIM = "#888888"
BRAND_GREEN = "#00ff88"
BRAND_RED = "#ff4d4d"
BRAND_BLUE = "#00bfff"
BRAND_PURPLE = "#b042ff"
BRAND_ORANGE = "#ffaa00"
BRAND_GRAY = "#1a1a1a"

# Category → (badge bg color, badge text)
_CATEGORY_STYLES = {
    "HOWL_OF_THE_DAY": (BRAND_GREEN, "HOWL OF THE DAY"),
    "EARNINGS":        (BRAND_BLUE,   "EARNINGS HOWL"),
    "BREAKING":        (BRAND_RED,    "BREAKING"),
    "JUST_IN":         (BRAND_RED,    "FRESH HOWL"),
    "CORRUPTION":      (BRAND_PURPLE, "CORRUPTION"),
    "WIRE":            (BRAND_GREEN,  "WIRE"),
}


def _safe_id(s):
    return re.sub(r"[^A-Za-z0-9_-]", "_", s or "")[:80]


def _wrap(text, width):
    """Wrap text into lines for matplotlib rendering. Returns list of lines."""
    if not text:
        return []
    return textwrap.wrap(text, width=width, break_long_words=False)


def render_card(*, category, headline, briefing="", ticker="", source="",
                ts_label="", post_id=None):
    """Render a 1200x675 PNG card. Returns repo-relative chart path or None.

    Args:
      category: one of HOWL_OF_THE_DAY / EARNINGS / BREAKING / JUST_IN /
                CORRUPTION / WIRE — drives badge color and label.
      headline: the lede sentence (gets the biggest treatment).
      briefing: optional body text. If it contains "\\n- " bullet lines,
                they're rendered as bullets; otherwise wrapped as prose.
      ticker:   optional cashtag string ("$KO") rendered top-right.
      source:   "Reuters" / "Bloomberg" etc., rendered bottom-left.
      ts_label: e.g. "Apr 28 · 14:30 EDT" — rendered next to source.
      post_id:  unique id used as the filename. Required for caching.
    """
    if not post_id:
        return None
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CARDS_DIR / f"{_safe_id(post_id)}.png"

    badge_color, badge_text = _CATEGORY_STYLES.get(
        category, (BRAND_GREEN, "WIRE"))

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)
    fig.patch.set_facecolor(BRAND_BG)
    ax.set_facecolor(BRAND_BG)
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    # ── Top bar: HOWL STREET handle + category badge + ticker ──
    fig.text(0.04, 0.93, "HOWL STREET",
             color=BRAND_GREEN, fontsize=20, fontweight="bold",
             family="monospace", va="top")
    fig.text(0.04, 0.885, "@HowlStreet · Your Wolf of Wall Street",
             color=BRAND_DIM, fontsize=11, family="monospace", va="top")

    # Category badge (top-right or right of handle)
    fig.text(0.96, 0.93, badge_text,
             ha="right", va="top",
             color="#000", fontsize=14, fontweight="bold",
             family="monospace",
             bbox=dict(boxstyle="round,pad=0.5", facecolor=badge_color,
                       edgecolor="none"))

    # Ticker badge below the category badge
    if ticker:
        fig.text(0.96, 0.855, ticker.upper(),
                 ha="right", va="top",
                 color=BRAND_GREEN, fontsize=18, fontweight="bold",
                 family="monospace")

    # ── Headline (big bold) ──
    headline_lines = _wrap(headline, 38)
    # Cap to 4 lines so we don't overflow
    if len(headline_lines) > 4:
        joined = " ".join(headline_lines[:4])
        # cut at last word boundary, no ellipsis
        headline_lines = _wrap(joined.rsplit(" ", 1)[0], 38)[:4]
    headline_y = 0.78
    for i, line in enumerate(headline_lines):
        fig.text(0.04, headline_y - i * 0.085, line,
                 color=BRAND_FG, fontsize=30, fontweight="bold",
                 family="sans-serif", va="top")

    # ── Briefing (bullets or prose) ──
    briefing_y_start = headline_y - len(headline_lines) * 0.085 - 0.04
    if briefing:
        # If we got the bullet-formatted briefing (with "- " line starts),
        # split on lines and render each as a bullet.
        raw_lines = [ln.strip() for ln in briefing.split("\n") if ln.strip()]
        rendered_lines = []
        for ln in raw_lines:
            if ln.startswith("- "):
                # Wrap the bullet content
                content = ln[2:]
                wrapped = _wrap(content, 75)
                if wrapped:
                    rendered_lines.append(("bullet", wrapped[0]))
                    for cont in wrapped[1:]:
                        rendered_lines.append(("cont", cont))
            else:
                wrapped = _wrap(ln, 75)
                for w in wrapped:
                    rendered_lines.append(("prose", w))
        # Cap at ~6 lines so we don't overflow
        rendered_lines = rendered_lines[:6]
        y = briefing_y_start
        for kind, text in rendered_lines:
            if kind == "bullet":
                fig.text(0.04, y, "—", color=BRAND_GREEN, fontsize=15,
                         fontweight="bold", family="sans-serif", va="top")
                fig.text(0.075, y, text,
                         color=BRAND_FG, fontsize=15,
                         family="sans-serif", va="top")
            elif kind == "cont":
                fig.text(0.075, y, text,
                         color=BRAND_FG, fontsize=15,
                         family="sans-serif", va="top")
            else:
                fig.text(0.04, y, text,
                         color=BRAND_FG, fontsize=15,
                         family="sans-serif", va="top")
            y -= 0.058

    # ── Bottom bar: source + timestamp + URL ──
    bottom_meta_parts = []
    if source:
        bottom_meta_parts.append(source.upper())
    if ts_label:
        bottom_meta_parts.append(ts_label)
    bottom_meta = "  ·  ".join(bottom_meta_parts)
    if bottom_meta:
        fig.text(0.04, 0.04, bottom_meta,
                 color=BRAND_DIM, fontsize=11, family="monospace",
                 va="bottom")
    fig.text(0.96, 0.04, "howlstreet.github.io",
             ha="right", va="bottom",
             color=BRAND_DIM, fontsize=11, family="monospace")

    # Subtle bottom border line
    ax.plot([0.04, 0.96], [0.085, 0.085],
            color=BRAND_GRAY, linewidth=1, transform=fig.transFigure,
            clip_on=False)
    # Subtle top border line
    ax.plot([0.04, 0.96], [0.85, 0.85],
            color=BRAND_GRAY, linewidth=1, transform=fig.transFigure,
            clip_on=False)

    try:
        plt.savefig(out_path, facecolor=BRAND_BG, dpi=100, bbox_inches="tight")
    except Exception as e:
        plt.close(fig)
        print(f"  ! card render {post_id}: {e}", file=sys.stderr)
        return None
    plt.close(fig)
    return str(out_path.relative_to(REPO_ROOT))
