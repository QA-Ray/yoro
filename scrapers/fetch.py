"""
YORO event scraper.

Runs daily on GitHub Actions. Reads manually-curated events from
events_curated.json, fetches more from external sources, writes
the combined feed to events.json.

To add a new source:
  1. Write fetch_yoursource() returning a list of dicts matching the schema.
  2. Add it to the SOURCES list at the bottom.

Schema (per event, * required):
  id*, title_ja*, title_zh, category*, kanji,
  date_start*, date_end*, prefecture*, city, venue,
  description_ja, description_zh,
  url, image_url, featured, source
"""

import json
import sys
from pathlib import Path
from datetime import date

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = "YoroBot/0.1 (+https://github.com/yoro/yoro)"


PREF_KEYWORDS = {
    "東京": "東京", "Tokyo": "東京",
    "京都": "京都", "Kyoto": "京都",
    "大阪": "大阪", "Osaka": "大阪",
    "神奈川": "神奈川", "横浜": "神奈川", "Yokohama": "神奈川",
    "兵庫": "兵庫", "神戸": "兵庫", "Kobe": "兵庫",
    "北海道": "北海道", "札幌": "北海道", "函館": "北海道",
    "愛知": "愛知", "名古屋": "愛知",
    "福岡": "福岡",
    "山梨": "山梨", "千葉": "千葉", "埼玉": "埼玉",
    "沖縄": "沖縄", "奈良": "奈良", "滋賀": "滋賀",
    "茨城": "茨城", "栃木": "栃木", "群馬": "群馬",
    "和歌山": "和歌山",
}


def guess_prefecture(text):
    if not text:
        return None
    for kw, pref in PREF_KEYWORDS.items():
        if kw in text:
            return pref
    return None


# ============== Sources ==============

def fetch_connpass():
    """Connpass · 日本最大のITコミュニティイベント検索サイト.

    Mostly tech/dev meetups, but proves the pipeline. Public JSON API.
    Note: as of 2024, Connpass is moving toward API keys for new clients.
    If this breaks, add ?key=xxx to params or remove from SOURCES.
    """
    url = "https://connpass.com/api/v1/event/"
    params = {"count": 50, "order": 2}

    r = requests.get(url, params=params,
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    data = r.json()

    out = []
    for item in data.get("events", []):
        place = (item.get("place") or "").strip()
        addr = (item.get("address") or "").strip()
        if "オンライン" in place or "オンライン" in addr or "Online" in place:
            continue
        if not (place or addr):
            continue

        prefecture = guess_prefecture(f"{place} {addr}")
        if not prefecture:
            continue

        title = item.get("title", "").strip()
        if not title:
            continue

        out.append({
            "id": f"connpass-{item['event_id']}",
            "title_ja": title,
            "title_zh": title,
            "category": "アート",
            "kanji": "集",
            "date_start": (item.get("started_at") or "")[:10],
            "date_end": (item.get("ended_at") or "")[:10],
            "prefecture": prefecture,
            "city": addr.split(" ")[1] if " " in addr else addr[:24],
            "venue": place or addr,
            "description_ja": (item.get("catch") or "")[:140],
            "description_zh": (item.get("catch") or "")[:140],
            "url": item.get("event_url", ""),
            "image_url": "",
            "featured": False,
            "source": "connpass",
        })
    return out


# Add new sources here. Each entry is (name, fetch_function).
SOURCES = [
    ("connpass", fetch_connpass),
]


# ============== Pipeline ==============

def filter_future(events):
    """Drop events whose end date is already past."""
    today = date.today().isoformat()
    return [
        e for e in events
        if e.get("date_start") and (not e.get("date_end") or e["date_end"] >= today)
    ]


def run():
    curated_path = DATA / "events_curated.json"
    if not curated_path.exists():
        print(f"ERROR: {curated_path} not found", file=sys.stderr)
        sys.exit(1)

    curated = json.loads(curated_path.read_text(encoding="utf-8"))
    curated_events = curated["events"]
    curated_ids = {e["id"] for e in curated_events}

    print(f"Loaded {len(curated_events)} curated events")
    print(f"Running {len(SOURCES)} scraper(s)...")

    scraped = []
    for name, fn in SOURCES:
        try:
            events = fn()
            print(f"  ✓ {name}: {len(events)} events")
            scraped.extend(events)
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}", file=sys.stderr)

    scraped = filter_future(scraped)
    seen, deduped = set(), []
    for ev in scraped:
        if ev["id"] in seen or ev["id"] in curated_ids:
            continue
        seen.add(ev["id"])
        deduped.append(ev)

    final = curated_events + deduped
    output = {
        "updated_at": date.today().isoformat(),
        "events": final,
    }

    out_path = DATA / "events.json"
    out_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n→ {out_path.relative_to(ROOT)}: "
          f"{len(final)} events ({len(curated_events)} curated + {len(deduped)} scraped)")


if __name__ == "__main__":
    run()
