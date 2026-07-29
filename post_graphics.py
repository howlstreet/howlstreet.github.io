"""
HOWL STREET — shared post graphics.

Clean, high-DPI share cards for X. Used by congress_trades, insider_trades,
and make_queue. One glance, readable on mobile, nothing clipped.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch, Rectangle

REPO_ROOT = Path(__file__).parent

BG = "#0b0b0b"
PANEL = "#141414"
FG = "#f2f2f2"
DIM = "#9a9a9a"
MUTED = "#5c5c5c"
GREEN = "#00ff88"
RED = "#ff4d4d"
GOLD = "#ffd966"
PINK = "#ff66c4"
CYAN = "#00bfff"
GRID = "#1e1e1e"

DPI = 180
FIG_W, FIG_H = 12.0, 6.75


def short_amount(label: str) -> str:
    """'$1,001 - $15,000' → '$1k–$15k'."""
    if not label:
        return ""
    s = label.replace(",", "").replace(" ", "")
    import re
    nums = re.findall(r"\$?([\d.]+)", label.replace(",", ""))
    def fmt(n):
        try:
            v = float(n)
        except ValueError:
            return n
        if v >= 1_000_000:
            return f"${v/1_000_000:.1f}M".replace(".0M", "M")
        if v >= 1000:
            return f"${v/1000:.0f}k"
        return f"${v:.0f}"
    if len(nums) >= 2:
        return f"{fmt(nums[0])}–{fmt(nums[1])}"
    if len(nums) == 1:
        return fmt(nums[0])
    return label[:18]


def _save(fig, out_path: Path):
    out_path = Path(out_path)
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor=BG, dpi=DPI)
    plt.close(fig)
    return str(out_path.relative_to(REPO_ROOT))


def render_caption_card(*, tag, headline, subline="", accent=GREEN,
                        out_path=None, kicker=""):
    """Editorial share card — full frame, no dead void."""
    if out_path is None:
        raise ValueError("out_path required")
    out_path = Path(out_path)

    fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=DPI, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor(BG)

    # Left accent
    ax.add_patch(Rectangle((0, 0), 0.014, 1, facecolor=accent, edgecolor="none"))

    # Right panel block — fills the void with desk identity
    ax.add_patch(Rectangle((0.62, 0), 0.38, 1, facecolor=PANEL, edgecolor="none"))
    ax.add_patch(Rectangle((0.62, 0), 0.014, 1, facecolor=accent, edgecolor="none", alpha=0.35))

    fig.text(0.045, 0.91, "HOWL STREET", color=accent, fontsize=16,
             fontweight="bold", fontfamily="monospace", ha="left", va="top")
    fig.text(0.045, 0.855, (tag or "").upper(), color=DIM, fontsize=12,
             fontfamily="monospace", ha="left", va="top", fontweight="bold")

    # Headline — wrap tightly, max ~4 lines, ellipsis if needed
    words = (headline or "").replace("investingLive", "investingLive ").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) > 28:
            if cur:
                lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    if len(lines) > 4:
        lines = lines[:4]
        if len(lines[-1]) > 25:
            lines[-1] = lines[-1][:25].rstrip() + "…"
        else:
            lines[-1] = lines[-1].rstrip() + "…"

    y = 0.72
    for line in lines or ["(no headline)"]:
        fig.text(0.045, y, line, color=FG, fontsize=30, fontweight="bold",
                 fontfamily="sans-serif", ha="left", va="top")
        y -= 0.10

    # Right rail content
    fig.text(0.68, 0.72, "FOR THE PACK", color=MUTED, fontsize=11,
             fontfamily="monospace", ha="left", va="top")
    fig.text(0.68, 0.58, (tag or "WIRE").upper().replace(" ", "\n"),
             color=accent, fontsize=26, fontweight="bold",
             fontfamily="monospace", ha="left", va="top", linespacing=1.25)
    if kicker:
        fig.text(0.68, 0.28, kicker[:40], color=DIM, fontsize=12,
                 fontfamily="monospace", ha="left", va="top")

    # Footer strip
    ax.add_patch(Rectangle((0, 0), 1, 0.11, facecolor="#080808", edgecolor="none"))
    fig.text(0.045, 0.055, (subline or "Public record")[:70], color=DIM,
             fontsize=12, fontfamily="monospace", ha="left", va="center")
    fig.text(0.97, 0.055, "howlstreet.github.io", color=MUTED, fontsize=11,
             fontfamily="monospace", ha="right", va="center")

    return _save(fig, out_path)


def render_price_chart_with_marks(
    *,
    ticker,
    title,
    dates,
    values,
    marks,
    out_path,
    badge_text,
    badge_positive,
    desk_label,
    source_label,
    accent=None,
):
    """Annotated price chart with numbered marks + readable side legend."""
    if len(dates) < 5 or len(values) < 5:
        return None

    accent = accent or (GREEN if badge_positive else RED)
    out_path = Path(out_path)

    vmin, vmax = min(values), max(values)
    pad = max((vmax - vmin) * 0.14, abs(vmax) * 0.025, 0.75)
    ymin, ymax = vmin - pad, vmax + pad

    fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=DPI, facecolor=BG)

    # Fixed figure coords — leave room so nothing clips
    ax = fig.add_axes([0.08, 0.13, 0.54, 0.66])
    ax.set_facecolor(BG)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color(MUTED)
        ax.spines[spine].set_linewidth(0.7)
    ax.tick_params(colors=DIM, labelsize=10, length=3, pad=4)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)

    ax.plot(dates, values, color=accent, linewidth=2.6, solid_capstyle="round")
    ax.fill_between(dates, values, ymin, color=accent, alpha=0.11)
    ax.set_ylim(ymin, ymax)
    # Leave headroom on the right so last price label fits inside axes
    x0, x1 = dates[0], dates[-1]
    span = (x1 - x0).total_seconds() if hasattr(x1 - x0, "total_seconds") else 1
    try:
        from datetime import timedelta
        ax.set_xlim(x0, x1 + timedelta(seconds=span * 0.06))
    except Exception:
        pass
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))

    legend_lines = []
    for i, m in enumerate(marks[:8], 1):
        td = m["date"]
        pivot = None
        for d, v in zip(dates, values):
            if d >= td:
                pivot = (d, v)
                break
        if not pivot:
            continue
        ax.scatter([pivot[0]], [pivot[1]], s=120, color=FG, zorder=5,
                   edgecolors=accent, linewidths=2.0)
        ax.text(pivot[0], pivot[1], str(i), color=BG, fontsize=8,
                fontweight="bold", ha="center", va="center", zorder=6)
        detail = m.get("detail") or ""
        legend_lines.append((str(i), m["label"], detail))

    # Current price — above the last point so it never clips the legend
    ax.scatter([dates[-1]], [values[-1]], s=70, color=accent, zorder=5,
               edgecolors=BG, linewidths=1.5)
    ax.annotate(
        f"${values[-1]:,.2f}",
        xy=(dates[-1], values[-1]),
        xytext=(0, 14),
        textcoords="offset points",
        color=accent,
        fontsize=12,
        fontweight="bold",
        ha="right",
        va="bottom",
    )

    # Header
    fig.text(0.08, 0.955, "HOWL STREET", color=GREEN, fontsize=13,
             fontweight="bold", fontfamily="monospace", ha="left", va="top")
    fig.text(0.08, 0.918, desk_label, color=DIM, fontsize=9,
             fontfamily="monospace", ha="left", va="top")
    fig.text(0.08, 0.865, title[:48], color=FG, fontsize=17, fontweight="bold",
             fontfamily="sans-serif", ha="left", va="top")

    # Badge fully inside
    badge_color = GREEN if badge_positive else RED
    fig.patches.append(FancyBboxPatch(
        (0.78, 0.905), 0.16, 0.055,
        boxstyle="square,pad=0",
        facecolor=badge_color, edgecolor="none",
        transform=fig.transFigure, clip_on=False,
    ))
    fig.text(0.86, 0.932, badge_text, color="#000", fontsize=13,
             fontweight="bold", fontfamily="monospace",
             ha="center", va="center")

    # Legend panel
    fig.patches.append(FancyBboxPatch(
        (0.66, 0.13), 0.30, 0.66,
        boxstyle="square,pad=0",
        facecolor=PANEL, edgecolor="#262626", linewidth=1,
        transform=fig.transFigure, clip_on=False,
    ))
    fig.text(0.68, 0.76, "MARKS", color=MUTED, fontsize=9,
             fontfamily="monospace", fontweight="bold", ha="left", va="top")

    y = 0.71
    for num, label, detail in legend_lines:
        fig.text(0.68, y, f"{num}  {label[:22]}", color=FG, fontsize=10,
                 fontfamily="monospace", ha="left", va="top", fontweight="bold")
        y -= 0.035
        if detail:
            fig.text(0.695, y, detail[:34], color=DIM, fontsize=9,
                     fontfamily="monospace", ha="left", va="top")
            y -= 0.045
        else:
            y -= 0.02
        if y < 0.16:
            break
    if not legend_lines:
        fig.text(0.68, 0.70, "No marks", color=MUTED, fontsize=10,
                 fontfamily="monospace", ha="left", va="top")

    fig.text(0.08, 0.04, "howlstreet.github.io", color=MUTED, fontsize=9,
             fontfamily="monospace", ha="left", va="bottom")
    fig.text(0.96, 0.04, source_label, color=MUTED, fontsize=9,
             fontfamily="monospace", ha="right", va="bottom")

    return _save(fig, out_path)
