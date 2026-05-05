"""
YORO event scraper.

Runs on GitHub Actions twice daily. Reads manually-curated events from
events_curated.json, fetches more from external sources, writes the
combined feed to events.json.

To add a new source:
  1. Write fetch_yoursource() returning a list of dicts matching the schema.
  2. Add it to the SOURCES list at the bottom.

Schema (per event, * required):
  id*, title_ja*, title_zh, category*, kanji,
  date_start*, date_end*, prefecture*, city, venue,
  description_ja, description_zh,
  url, image_url, featured, source
"""

import hashlib
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ============== Helpers ==============

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


# Category + kanji guesser based on Japanese keywords.
# Order matters — earlier wins.
CATEGORY_KEYWORDS = [
    (("祭", "まつり", "Festival"), "祭り", "祭"),
    (("花火",), "祭り", "火"),
    (("展覧", "展示", "美術", "アート", "ギャラリー", "ミュージアム"), "展覧会", "展"),
    (("コンサート", "ライブ", "音楽", "フェス"), "音楽", "音"),
    (("桜", "さくら", "紅葉", "紫陽花", "あじさい", "梅", "藤",
      "牡丹", "薔薇", "バラ", "ライラック", "コスモス",
      "イルミネーション", "庭園", "公園", "ガーデン"), "自然", "花"),
    (("マーケット", "市場", "蚤の市", "骨董", "フリマ"), "マーケット", "市"),
    (("グルメ", "ビアガーデン", "ビール", "ワイン", "食"), "グルメ", "食"),
]


def guess_cat_kanji(name, desc=""):
    text = (name or "") + " " + (desc or "")
    for keywords, cat, kanji in CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return cat, kanji
    return "祭り", "事"


def stable_id(prefix, *seeds):
    seed = "|".join(s or "" for s in seeds)
    return f"{prefix}-{hashlib.md5(seed.encode('utf-8')).hexdigest()[:10]}"


def filter_future(events):
    today = date.today().isoformat()
    return [
        e for e in events
        if e.get("date_start")
        and (not e.get("date_end") or e["date_end"] >= today)
    ]


# ============== Translation (MyMemory, no API key) ==============

TRANSLATIONS_CACHE_FILE = DATA / "translations_cache.json"
_TRANS_CACHE = None
_TRANS_SESSION = None
_TRANS_FAILED = False


def _load_trans():
    global _TRANS_CACHE
    if _TRANS_CACHE is None:
        if TRANSLATIONS_CACHE_FILE.exists():
            try:
                _TRANS_CACHE = json.loads(
                    TRANSLATIONS_CACHE_FILE.read_text(encoding="utf-8")
                )
            except Exception:
                _TRANS_CACHE = {}
        else:
            _TRANS_CACHE = {}
    return _TRANS_CACHE


def _save_trans():
    if _TRANS_CACHE is None:
        return
    TRANSLATIONS_CACHE_FILE.write_text(
        json.dumps(_TRANS_CACHE, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


_BAD_TRANS_MARKERS = (
    "QUERY LENGTH LIMIT",
    "PLEASE SELECT TWO DISTINCT",
    "MYMEMORY WARNING",
    "INVALID LANGUAGE PAIR",
    "INVALID EMAIL",
)


def translate_ja_to_zh(text):
    """Japanese → Simplified Chinese via MyMemory free tier.

    Caches results to data/translations_cache.json. Returns original text
    on any failure (rate limit, network, junk response). Set MYMEMORY_EMAIL
    env var to bump free quota from 1k → 100k chars/day.
    """
    import os
    global _TRANS_SESSION, _TRANS_FAILED

    if not text or not text.strip():
        return text
    cache = _load_trans()
    if text in cache:
        return cache[text]
    if _TRANS_FAILED:
        return text  # don't keep hammering after rate limit

    if _TRANS_SESSION is None:
        _TRANS_SESSION = requests.Session()

    email = os.environ.get("MYMEMORY_EMAIL", "").strip()
    params = {"q": text[:500], "langpair": "ja|zh-CN"}
    if email:
        params["de"] = email

    try:
        r = _TRANS_SESSION.get(
            "https://api.mymemory.translated.net/get",
            params=params,
            timeout=8,
            headers={"User-Agent": "YoroBot/0.1 (+https://github.com/QA-Ray/yoro)"},
        )
        if r.status_code == 429 or r.status_code == 403:
            _TRANS_FAILED = True
            print(f"  [translate] rate limited (HTTP {r.status_code}); "
                  f"remaining translations skipped this run", file=sys.stderr)
            return text
        r.raise_for_status()
        data = r.json()
        status = str(data.get("responseStatus", ""))
        translated = (data.get("responseData", {}).get("translatedText") or "").strip()

        if status == "200" and translated:
            up = translated.upper()
            if any(m in up for m in _BAD_TRANS_MARKERS):
                return text
            if translated.upper() == text.upper():
                # No actual translation; cache as-is to avoid retrying
                cache[text] = text
                return text
            cache[text] = translated
            return translated
    except Exception as e:
        print(f"  [translate] {type(e).__name__}: {e}", file=sys.stderr)

    return text


# ============== Sources ==============

def fetch_connpass():
    """Connpass · 日本最大のITコミュニティイベント検索サイト.

    Public JSON API — likely requires API key as of 2024.
    If 403, set CONNPASS_API_KEY env var.
    """
    import os
    url = "https://connpass.com/api/v1/event/"
    params = {"count": 50, "order": 2}
    headers = {"User-Agent": "YoroBot/0.1 (+https://github.com/QA-Ray/yoro)"}
    api_key = os.environ.get("CONNPASS_API_KEY")
    if api_key:
        params["api_key"] = api_key

    r = requests.get(url, params=params, headers=headers, timeout=15)
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
        title = (item.get("title") or "").strip()
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


# Walkerplus prefecture codes (only the major ones; expand later)
WALKERPLUS_PREFS = [
    ("ar0313", "東京"),
    ("ar0727", "大阪"),
    ("ar0726", "京都"),
    ("ar0314", "神奈川"),
]


def fetch_walkerplus():
    """Walkerplus event listings (HTML page + Schema.org JSON-LD).

    Robots.txt explicitly allows /event_list/. Polite: 1.5s delay between
    prefecture pages. ~10 events per prefecture page.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    }

    out = []
    for code, pref_name in WALKERPLUS_PREFS:
        url = f"https://www.walkerplus.com/event_list/{code}/"
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"  [walkerplus {pref_name}] {type(e).__name__}: {e}",
                  file=sys.stderr)
            time.sleep(1.5)
            continue

        blocks = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            r.text, re.DOTALL,
        )

        added = 0
        for block in blocks:
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict) or item.get("@type") != "Event":
                    continue
                ev = parse_walkerplus_event(item, pref_name)
                if ev:
                    out.append(ev)
                    added += 1
        print(f"  [walkerplus {pref_name}] {added} events")
        time.sleep(1.5)

    return out


def parse_walkerplus_event(d, prefecture):
    name = (d.get("name") or "").strip()
    if not name:
        return None
    start = (d.get("startDate") or "")[:10]
    end = (d.get("endDate") or start)[:10]
    if not start:
        return None

    loc = d.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    venue = loc.get("name", "") if isinstance(loc, dict) else ""
    addr = loc.get("address", {}) if isinstance(loc, dict) else {}
    city = addr.get("addressLocality", "") if isinstance(addr, dict) else ""

    desc = (d.get("description") or "").strip()
    desc_short = desc[:140] + ("…" if len(desc) > 140 else "")

    event_url = d.get("url") or ""
    image = d.get("image") or ""
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url") or ""

    cat, kanji = guess_cat_kanji(name, desc)

    title_zh = translate_ja_to_zh(name)
    desc_zh = translate_ja_to_zh(desc_short)

    return {
        "id": stable_id("wp", event_url, name, start),
        "title_ja": name,
        "title_zh": title_zh,
        "category": cat,
        "kanji": kanji,
        "date_start": start,
        "date_end": end,
        "prefecture": prefecture,
        "city": city,
        "venue": venue,
        "description_ja": desc_short,
        "description_zh": desc_zh,
        "url": event_url,
        "image_url": image,
        "featured": False,
        "source": "walkerplus",
    }


# Add new sources here. Each is (name, fetch_function).
SOURCES = [
    ("connpass", fetch_connpass),
    ("walkerplus", fetch_walkerplus),
]


# ============== Pipeline ==============

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
            print(f"  ✓ {name}: {len(events)} events total")
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
    _save_trans()
    cache_size = len(_TRANS_CACHE) if _TRANS_CACHE else 0
    print(
        f"\n→ {out_path.relative_to(ROOT)}: {len(final)} events "
        f"({len(curated_events)} curated + {len(deduped)} scraped)"
    )
    print(f"→ translations cache: {cache_size} entries")


if __name__ == "__main__":
    run()
