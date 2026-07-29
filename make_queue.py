"""
HOWL STREET — local post kit (NOT published to GitHub Pages).

Builds Finder-friendly packages you can copy/paste from:
  ready/01-capitol-MSFT/caption.txt
  ready/01-capitol-MSFT/image.png

Plus a local queue.html with one-click Copy + image preview.

Usage:
  python make_queue.py
  open queue.html          # or open the ready/ folders in Finder

Never commit queue.html or ready/ — they are local-only.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO_ROOT = Path(__file__).parent
DRAFTS_PATH = REPO_ROOT / "drafts.json"
CONGRESS_PATH = REPO_ROOT / "congress_posts.json"
INSIDER_PATH = REPO_ROOT / "insider_posts.json"
READY_DIR = REPO_ROOT / "ready"
QUEUE_PATH = REPO_ROOT / "queue.html"
CARDS_DIR = REPO_ROOT / "charts" / "cards"

BRAND_GREEN = "#00ff88"
BRAND_BG = "#0a0a0a"
BRAND_FG = "#e8e8e8"
BRAND_DIM = "#777777"

# Public X priority — skip the rest for the post kit.
PRIORITY = [
    "CONGRESS_WATCH",
    "CORRUPTION_WATCH_INSIDER",
    "LOUD_HOWL",
    "CORRUPTION_WATCH",
    "MARKET_MOVE",
]
MAX_PACKS = 12


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _slug(s, n=40):
    s = re.sub(r"[^A-Za-z0-9]+", "-", (s or "").strip()).strip("-").lower()
    return (s or "post")[:n]


def render_text_card(*, tag, headline, subline=""):
    """Branded PNG for posts that don't already have a chart."""
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    safe = _slug(f"{tag}-{headline}", 48)
    out = CARDS_DIR / f"{safe}.png"

    fig = plt.figure(figsize=(12, 6.75), dpi=100, facecolor=BRAND_BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor(BRAND_BG)

    fig.text(0.05, 0.90, "HOWL STREET", color=BRAND_GREEN, fontsize=18,
             fontweight="bold", family="monospace", ha="left", va="top")
    fig.text(0.05, 0.84, tag.upper(), color=BRAND_DIM, fontsize=12,
             family="monospace", ha="left", va="top")

    # Word-wrap headline
    words = (headline or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) > 42:
            if cur:
                lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    lines = lines[:5] or ["(no headline)"]

    y = 0.62
    for line in lines:
        fig.text(0.05, y, line, color=BRAND_FG, fontsize=28, fontweight="bold",
                 family="sans-serif", ha="left", va="top")
        y -= 0.09

    if subline:
        fig.text(0.05, 0.12, subline[:90], color=BRAND_DIM, fontsize=13,
                 family="monospace", ha="left", va="bottom")
    fig.text(0.95, 0.08, "howlstreet.github.io", color=BRAND_DIM, fontsize=11,
             family="monospace", ha="right", va="bottom")

    fig.savefig(out, facecolor=BRAND_BG, dpi=100)
    plt.close(fig)
    return str(out.relative_to(REPO_ROOT))


def _resolve_image(path):
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return str(p.relative_to(REPO_ROOT)) if p.exists() else None


def packs_from_congress(posts_by_id):
    out = []
    for post in sorted(
        (posts_by_id or {}).values(),
        key=lambda p: (p.get("member_count", 0), p.get("fired_at", "")),
        reverse=True,
    ):
        ticker = post.get("ticker") or ""
        members = post.get("members") or []
        if not ticker or len(members) < 2:
            continue
        lines = []
        for m in members[:8]:
            party = m.get("party") or ""
            tag = f" ({party})" if party else ""
            lines.append(f"• {m.get('name', '')}{tag}")
        n = post.get("member_count") or len(members)
        biggest = post.get("biggest_amount") or ""
        biggest_name = post.get("biggest_name") or ""
        biggest_date = post.get("biggest_date") or ""
        try:
            nice = datetime.strptime(biggest_date, "%Y-%m-%d").strftime("%b %d")
        except (ValueError, TypeError):
            nice = biggest_date
        opt = " in options" if post.get("biggest_is_options") else ""
        pct = post.get("pct_since_first", 0) or 0
        sign = "+" if pct >= 0 else ""
        caption = (
            f"{n} members of Congress bought ${ticker} this year — "
            f"and these filings don't show them selling:\n\n"
            + "\n".join(lines)
            + "\n\n"
        )
        if biggest and biggest_name:
            caption += f"Largest disclosed here: {biggest_name}, {biggest}{opt} on {nice}.\n\n"
        caption += (
            f"Price since the first buy in this group: {sign}{pct:.1f}%.\n\n"
            f"STOCK Act disclosures. Public record."
        )
        img = _resolve_image(post.get("chart_path"))
        if not img:
            img = render_text_card(
                tag="Capitol Wire",
                headline=f"{n} in Congress bought ${ticker}",
                subline="STOCK Act · public record",
            )
        out.append({
            "format": "CONGRESS_WATCH",
            "label": f"Capitol · ${ticker}",
            "caption": caption,
            "image_path": img,
            "source_url": post.get("source_url") or "",
            "id": post.get("post_id") or f"congress-{ticker}",
        })
    return out


def packs_from_insider(posts_by_id):
    out = []
    for post in sorted(
        (posts_by_id or {}).values(),
        key=lambda p: (p.get("dollar_value", 0) or 0),
        reverse=True,
    ):
        dv = post.get("dollar_value", 0) or 0
        if dv < 250_000:
            continue
        ticker = post.get("ticker") or ""
        company = (post.get("company") or "").rstrip(".")
        ttype = post.get("type") or ""
        verb = "bought" if ttype == "P" else "sold"
        noun = "purchase" if ttype == "P" else "sale"
        qty = post.get("qty", 0) or 0
        price = post.get("price", 0) or 0
        pct = post.get("pct_since", 0) or 0
        sign = "+" if pct >= 0 else ""
        n = post.get("num_insiders", 1) or 1
        who = f"{n} company insiders" if n > 1 else "a company insider"
        dollar = f"${dv/1_000_000:.1f}M" if dv >= 1_000_000 else f"${dv:,.0f}"
        trade_date = post.get("trade_date") or ""
        try:
            nice = datetime.strptime(trade_date, "%Y-%m-%d").strftime("%b %d")
        except (ValueError, TypeError):
            nice = trade_date
        caption = (
            f"${ticker} — {who} {verb} {dollar} on {nice}.\n\n"
            f"{company}. {qty:,.0f} shares at ${price:,.2f}.\n\n"
            f"Stock is {sign}{pct:.1f}% since the {noun}. "
            f"This is a public SEC Form 4 filing."
        )
        img = _resolve_image(post.get("chart_path"))
        if not img:
            img = render_text_card(
                tag="Insider Wire",
                headline=f"${ticker} insider {verb} {dollar}",
                subline=f"{company} · Form 4",
            )
        out.append({
            "format": "CORRUPTION_WATCH_INSIDER",
            "label": f"Insider · ${ticker}",
            "caption": caption,
            "image_path": img,
            "source_url": post.get("link") or "",
            "id": post.get("post_id") or f"insider-{ticker}",
        })
    return out


def packs_from_drafts(drafts):
    out = []
    for d in drafts or []:
        fmt = d.get("format") or ""
        if fmt not in PRIORITY:
            continue
        # Prefer dedicated builders for congress/insider
        if fmt in ("CONGRESS_WATCH", "CORRUPTION_WATCH_INSIDER"):
            continue
        text = (d.get("draft_text") or "").strip()
        if not text:
            continue
        # Drop ancient marketing openers if any slipped through
        for bad in ("PACK FRONT-PAGE:", "TODAY'S LOUDEST HOWL:", "FOR THE WOLF"):
            if text.startswith(bad):
                text = text[len(bad):].strip()
        img = _resolve_image(d.get("image_path"))
        title = d.get("source_title") or d.get("primary_source") or fmt
        if not img:
            tag = {
                "LOUD_HOWL": "Loudest Howl",
                "CORRUPTION_WATCH": "The Hunt",
                "MARKET_MOVE": "Move Wire",
            }.get(fmt, fmt)
            img = render_text_card(
                tag=tag,
                headline=title[:80],
                subline=(d.get("primary_source") or "")[:60],
            )
        out.append({
            "format": fmt,
            "label": {
                "LOUD_HOWL": "Loudest Howl",
                "CORRUPTION_WATCH": "The Hunt",
                "MARKET_MOVE": "Move Wire",
            }.get(fmt, fmt),
            "caption": text,
            "image_path": img,
            "source_url": d.get("source_url") or "",
            "id": d.get("id") or _slug(title, 16),
        })
    return out


def collect_packs():
    congress = packs_from_congress(_load_json(CONGRESS_PATH, {}))
    insider = packs_from_insider(_load_json(INSIDER_PATH, {}))
    drafts = packs_from_drafts(_load_json(DRAFTS_PATH, []))

    # Dedupe by caption hash-ish; keep priority order
    seen = set()
    ordered = []
    for pack in congress + insider + drafts:
        key = re.sub(r"\W+", "", pack["caption"].lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        ordered.append(pack)
        if len(ordered) >= MAX_PACKS:
            break
    return ordered


def write_ready(packs):
    if READY_DIR.exists():
        shutil.rmtree(READY_DIR)
    READY_DIR.mkdir(parents=True)

    written = []
    for i, pack in enumerate(packs, 1):
        folder = READY_DIR / f"{i:02d}-{_slug(pack['label'])}-{_slug(pack['id'], 12)}"
        folder.mkdir(parents=True)
        (folder / "caption.txt").write_text(pack["caption"].rstrip() + "\n",
                                            encoding="utf-8")
        src = REPO_ROOT / pack["image_path"]
        dest = folder / "image.png"
        shutil.copy2(src, dest)
        meta = {
            "format": pack["format"],
            "label": pack["label"],
            "source_url": pack.get("source_url") or "",
            "image": "image.png",
            "caption": "caption.txt",
        }
        (folder / "meta.json").write_text(json.dumps(meta, indent=2),
                                          encoding="utf-8")
        # Tiny README for Finder users
        (folder / "HOW_TO_POST.txt").write_text(
            "1. Open caption.txt — select all — copy\n"
            "2. Attach image.png to the X post\n"
            "3. Paste caption and send\n",
            encoding="utf-8",
        )
        written.append((folder.name, pack))
    return written


def write_queue_html(packs):
    cards = []
    for i, pack in enumerate(packs, 1):
        # queue.html is at repo root; images are relative paths
        img = html_lib.escape(pack["image_path"])
        caption = html_lib.escape(pack["caption"])
        label = html_lib.escape(pack["label"])
        fmt = html_lib.escape(pack["format"])
        folder = f"ready/{i:02d}-{_slug(pack['label'])}-{_slug(pack['id'], 12)}"
        cards.append(f"""
<article class="card" data-i="{i}">
  <header>
    <span class="num">{i:02d}</span>
    <span class="badge">{label}</span>
    <span class="fmt">{fmt}</span>
  </header>
  <div class="body">
    <img src="{img}" alt="{label}" />
    <div class="side">
      <pre class="caption" id="cap-{i}">{caption}</pre>
      <div class="actions">
        <button type="button" onclick="copyCap({i})">Copy caption</button>
        <a class="btn" href="{img}" download="howl-{i:02d}.png">Download image</a>
        <a class="btn ghost" href="{html_lib.escape(folder)}/" target="_blank">Open folder</a>
      </div>
      <div class="status" id="st-{i}"></div>
    </div>
  </div>
</article>
""")

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Howl Street — Post Kit (local only)</title>
<style>
  :root {{ --bg:#050505; --fg:#e8e8e8; --dim:#777; --green:#00ff88; --card:#0d0d0d; --border:#222; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,sans-serif; }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 28px 20px 80px; }}
  h1 {{ color:var(--green); font-size:22px; letter-spacing:1px; margin:0 0 6px; font-family: ui-monospace, monospace; }}
  .sub {{ color:var(--dim); font-size:13px; margin-bottom: 28px; }}
  .card {{ background:var(--card); border:1px solid var(--border); margin-bottom:22px; padding:16px; }}
  header {{ display:flex; gap:10px; align-items:center; margin-bottom:14px; font-size:12px; }}
  .num {{ color:var(--green); font-family:ui-monospace,monospace; font-weight:bold; }}
  .badge {{ background:var(--green); color:#000; padding:3px 8px; font-weight:bold; font-size:11px; }}
  .fmt {{ color:var(--dim); margin-left:auto; font-family:ui-monospace,monospace; }}
  .body {{ display:grid; grid-template-columns: 1.1fr 1fr; gap:18px; }}
  @media (max-width: 800px) {{ .body {{ grid-template-columns: 1fr; }} }}
  img {{ width:100%; border:1px solid var(--border); background:#000; display:block; }}
  .caption {{ white-space:pre-wrap; background:#000; border:1px solid var(--border); padding:12px; margin:0; min-height:160px; font:13px/1.5 ui-monospace,monospace; color:var(--fg); }}
  .actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
  button, .btn {{ background:var(--green); color:#000; border:none; padding:9px 14px; font-weight:bold; font-size:12px; cursor:pointer; text-decoration:none; display:inline-block; }}
  .btn.ghost {{ background:transparent; color:var(--green); border:1px solid var(--green); }}
  .status {{ color:var(--green); font-size:12px; min-height:18px; margin-top:8px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>HOWL STREET — POST KIT</h1>
  <div class="sub">Local only — not on the public site. Copy caption → attach image → post.
  Same packages live in the <code>ready/</code> folder in Finder.</div>
  {''.join(cards) if cards else '<p class="sub">No packs yet. Run update.py then make_queue.py.</p>'}
</div>
<script>
function copyCap(i) {{
  const el = document.getElementById('cap-' + i);
  const text = el.innerText;
  navigator.clipboard.writeText(text).then(() => {{
    const st = document.getElementById('st-' + i);
    st.textContent = 'Caption copied. Attach the image and paste on X.';
    setTimeout(() => {{ st.textContent = ''; }}, 2500);
  }});
}}
</script>
</body>
</html>
"""
    QUEUE_PATH.write_text(page, encoding="utf-8")


def main():
    packs = collect_packs()
    written = write_ready(packs)
    write_queue_html(packs)
    print(f"  post kit: {len(written)} packages → ready/ + queue.html")
    for name, pack in written[:8]:
        print(f"    {name}  [{pack['format']}]  img={pack['image_path']}")
    if len(written) > 8:
        print(f"    … +{len(written) - 8} more")
    print(f"  open {QUEUE_PATH}")
    return packs


if __name__ == "__main__":
    main()
