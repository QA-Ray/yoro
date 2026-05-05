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
# Order matters — earlier wins. Specific patterns first, then broad.
CATEGORY_KEYWORDS = [
    # Festivals (specific to祭, not just food fest)
    (("祭", "まつり", "神輿", "山車", "屋台"), "祭り", "祭"),
    (("花火", "花火大会"), "祭り", "火"),
    # Exhibitions / Art (covers most "展" events including character/IP exhibits)
    (("展覧", "展示", "美術館", "美術", "ギャラリー", "ミュージアム",
      "博物館", "アート"), "展覧会", "展"),
    (("〜展", "展—", "の展", "展2", "展(", "展《"), "展覧会", "展"),  # title patterns ending in 展
    # Sports & motorsport — specific markers only (avoid broad "スポーツ" alone)
    (("F1", "MotoGP", "Formula 1", "フォーミュラ", "Grand Prix", "グランプリ",
      "サーキット", "ラリー",
      "野球", "サッカー", "Jリーグ", "Bリーグ",
      "バスケットボール", "ラグビー", "テニス", "ゴルフ", "卓球",
      "マラソン", "駅伝", "相撲", "競馬", "競輪", "競艇",
      "オリンピック", "パラリンピック", "選手権大会",
      "スポーツ大会", "スポーツフェス"), "スポーツ", "競"),
    # Live performance / concerts — specific live-event terms only
    (("コンサート", "ライブ", "オーケストラ",
      "ジャズ", "クラシック音楽", "ミュージカル",
      "演劇", "舞台公演", "アイドル", "K-POP", "シンフォニー",
      "歌劇", "オペラ", "リサイタル"), "音楽", "響"),
    # Nature: flowers, parks, scenic
    (("桜", "さくら", "サクラ", "紅葉", "もみじ", "紫陽花", "あじさい",
      "梅", "藤", "牡丹", "薔薇", "バラ", "ライラック", "コスモス",
      "ひまわり", "向日葵", "チューリップ", "菜の花",
      "イルミネーション", "庭園", "公園", "ガーデン",
      "動物園", "水族館", "植物園", "牧場",
      "狩り", "苺", "イチゴ", "ぶどう", "りんご", "みかん"), "自然", "花"),
    # Food / drink
    (("グルメ", "ビアガーデン", "ビール", "ワイン", "日本酒",
      "カフェ", "ラーメン", "焼肉", "寿司", "スイーツ"), "グルメ", "食"),
    # Markets
    (("マーケット", "市場", "蚤の市", "骨董", "フリマ", "朝市", "夜市"), "マーケット", "市"),
    # Catch generic event types as アート (cultural/experience)
    (("謎解き", "謎解", "イベント", "体験", "ワークショップ",
      "ショー", "パフォーマンス", "プロジェクションマッピング",
      "リアル脱出", "脱出ゲーム", "アトラクション"), "アート", "事"),
]


def guess_cat_kanji(name, desc=""):
    text = (name or "") + " " + (desc or "")
    for keywords, cat, kanji in CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return cat, kanji
    # Unmatched fallback: generic "other event" — surfaces in その他 section,
    # avoids mislabeling random events as 祭り.
    return "その他", "事"


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


# Walkerplus prefecture codes — pattern is ar{REGION}{JIS_PREF}
# Limited to popular tourist destinations to keep run time reasonable.
# To add more: see https://www.walkerplus.com/event_list/ar{XXYY}/
WALKERPLUS_PREFS = [
    ("ar0101", "北海道"),
    ("ar0313", "東京"),
    ("ar0314", "神奈川"),
    ("ar0622", "静岡"),
    ("ar0623", "愛知"),
    ("ar0726", "京都"),
    ("ar0727", "大阪"),
    ("ar0728", "兵庫"),
    ("ar0729", "奈良"),
    ("ar1040", "福岡"),
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


def parse_walkerplus_event(d, prefecture=None):
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

    if prefecture is None:
        region = addr.get("addressRegion", "") if isinstance(addr, dict) else ""
        prefecture = region.replace("都", "").replace("府", "").replace("県", "").strip()
        if prefecture not in PREF_KEYWORDS.values():
            prefecture = guess_prefecture(name + " " + venue) or "東京"

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


# Walkerplus genre feeds — national events filtered by category type.
# Each entry: (genre_code, display_name, forced_category, forced_kanji)
# Genre codes discovered at /event_list/eg0108/ etc.
WALKERPLUS_GENRES = [
    ("eg0108", "全国スポーツ",   "スポーツ", "競"),
    ("eg0109", "全国ライブ・音楽", "音楽",   "響"),
    ("eg0111", "全国舞台・演劇",  "音楽",   "舞"),
]


def fetch_walkerplus_genre():
    """Walkerplus genre feeds — sports, live music, theater.

    Forces category based on the source genre URL (we trust Walkerplus's
    own classification more than keyword guessing for these specific types).
    Yields ~10 events per genre, nationally curated.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    }

    out = []
    for code, name, forced_cat, forced_kanji in WALKERPLUS_GENRES:
        url = f"https://www.walkerplus.com/event_list/{code}/"
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"  [walkerplus genre {name}] {type(e).__name__}: {e}",
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
                ev = parse_walkerplus_event(item)  # auto-detect prefecture
                if not ev:
                    continue
                # Override category — trust Walkerplus's genre classification
                ev["category"] = forced_cat
                ev["kanji"] = forced_kanji
                ev["source"] = "walkerplus_genre"
                out.append(ev)
                added += 1
        print(f"  [walkerplus genre {name}] {added} events")
        time.sleep(1.5)

    return out


# Bandsintown — concert tracker by artist (per-artist API queries)
# English region → JP prefecture mapping for venues
BIT_PREF_MAP = {
    "Tokyo": "東京", "Osaka": "大阪", "Kyoto": "京都",
    "Kanagawa": "神奈川", "Hokkaido": "北海道",
    "Hyogo": "兵庫", "Aichi": "愛知", "Saitama": "埼玉",
    "Chiba": "千葉", "Fukuoka": "福岡", "Nara": "奈良",
    "Hiroshima": "広島", "Miyagi": "宮城", "Niigata": "新潟",
    "Shizuoka": "静岡", "Okinawa": "沖縄",
}


def fetch_bandsintown():
    """Bandsintown — concerts by tracked artist.

    Configured via:
      - data/bandsintown_artists.json  (list of artist names)
      - BANDSINTOWN_API_KEY env var    (your app_id from bandsintown.com/api)

    Filters to Japan-only events. Polite 0.5s delay per artist call.
    Returns empty if either config missing — fail-soft.
    """
    import os
    import urllib.parse

    api_key = os.environ.get("BANDSINTOWN_API_KEY", "").strip()
    if not api_key:
        print("  [bandsintown] BANDSINTOWN_API_KEY not set, skipping",
              file=sys.stderr)
        return []

    artists_path = DATA / "bandsintown_artists.json"
    if not artists_path.exists():
        print("  [bandsintown] data/bandsintown_artists.json missing, skipping",
              file=sys.stderr)
        return []

    try:
        cfg = json.loads(artists_path.read_text(encoding="utf-8"))
        artists = cfg.get("artists", []) or []
    except Exception as e:
        print(f"  [bandsintown] config parse error: {e}", file=sys.stderr)
        return []

    if not artists:
        return []

    headers = {
        "User-Agent": "YoroBot/0.1 (+https://github.com/QA-Ray/yoro)",
        "Accept": "application/json",
    }

    out = []
    for artist in artists:
        encoded = urllib.parse.quote(artist, safe="")
        url = (
            f"https://rest.bandsintown.com/artists/{encoded}/events"
            f"?app_id={urllib.parse.quote(api_key)}&date=upcoming"
        )
        try:
            r = requests.get(url, headers=headers, timeout=12)
        except Exception as e:
            print(f"  [bandsintown {artist}] network: {e}", file=sys.stderr)
            time.sleep(0.5)
            continue

        if r.status_code != 200:
            print(f"  [bandsintown {artist}] HTTP {r.status_code}",
                  file=sys.stderr)
            time.sleep(0.5)
            continue

        try:
            data = r.json()
        except Exception:
            time.sleep(0.5)
            continue

        if not isinstance(data, list):
            time.sleep(0.5)
            continue

        japan = 0
        for ev in data:
            venue = ev.get("venue", {}) or {}
            if (venue.get("country") or "").lower() != "japan":
                continue
            dt = (ev.get("datetime") or "")[:10]
            if not dt:
                continue

            artist_obj = ev.get("artist") or {}
            artist_name = artist_obj.get("name") or artist
            ev_title = ev.get("title") or ev.get("description") or \
                       f"{artist_name} @ {venue.get('name','')}"

            region = venue.get("region", "") or ""
            prefecture = BIT_PREF_MAP.get(region, region) or "東京"

            image = (artist_obj.get("image_url") or
                     artist_obj.get("thumb_url") or "")

            out.append({
                "id": f"bit-{ev.get('id') or stable_id('bit', artist_name, dt)}",
                "title_ja": ev_title,
                "title_zh": ev_title,
                "category": "音楽",
                "kanji": "響",
                "date_start": dt,
                "date_end": dt,
                "prefecture": prefecture,
                "city": venue.get("city", "") or "",
                "venue": venue.get("name", "") or "",
                "description_ja": (ev.get("description") or "")[:140] or
                                  f"{artist_name} live",
                "description_zh": (ev.get("description") or "")[:140] or
                                  f"{artist_name} live",
                "url": ev.get("url", "") or "",
                "image_url": image,
                "featured": False,
                "source": "bandsintown",
            })
            japan += 1
        print(f"  [bandsintown {artist}] {japan} Japan events")
        time.sleep(0.5)

    return out


def fetch_ticketmaster():
    """Ticketmaster Discovery API — fetches all JP events, buckets by segment.

    Uses CONSUMER_KEY as `apikey`. Free tier: 5000/day, 5/sec.
    Single call per run, classifies events on our side via segment name
    (Music / Sports / Arts & Theatre / etc).

    Note: Ticketmaster's JP coverage is limited — many domestic Japan
    events go through PIA/Eplus, not Ticketmaster. Empty result is OK,
    fail-soft pipeline continues.
    """
    import os
    import collections
    api_key = os.environ.get("CONSUMER_KEY", "").strip()
    if not api_key:
        print("  [ticketmaster] CONSUMER_KEY not set, skipping",
              file=sys.stderr)
        return []

    base = "https://app.ticketmaster.com/discovery/v2/events.json"
    params = {
        "apikey": api_key,
        "countryCode": "JP",
        "size": 200,
        "sort": "date,asc",
    }
    try:
        r = requests.get(
            base, params=params, timeout=15,
            headers={"User-Agent": "YoroBot/0.1 (+https://github.com/QA-Ray/yoro)"},
        )
        if r.status_code != 200:
            print(f"  [ticketmaster] HTTP {r.status_code}: {r.text[:140]}",
                  file=sys.stderr)
            return []
        data = r.json()
    except Exception as e:
        print(f"  [ticketmaster] {type(e).__name__}: {e}", file=sys.stderr)
        return []

    events = (data.get("_embedded") or {}).get("events", []) or []
    print(f"  [ticketmaster] {len(events)} JP events from API")

    if not events:
        return []

    SEGMENT_MAP = {
        "music":              ("音楽", "響"),
        "sports":             ("スポーツ", "競"),
        "arts & theatre":     ("音楽", "舞"),
        "miscellaneous":      ("その他", "事"),
        "film":               ("アート", "光"),
    }

    seg_counts = collections.Counter()
    skip_reasons = collections.Counter()
    out = []
    for i, ev in enumerate(events):
        classifications = ev.get("classifications") or []
        seg_name = ""
        if classifications:
            seg_name = (classifications[0].get("segment") or {}).get("name", "") or ""
        cat, kanji = SEGMENT_MAP.get(seg_name.lower(), ("音楽", "響"))

        mapped, reason = parse_ticketmaster_event(ev, cat, kanji)
        if mapped:
            out.append(mapped)
            seg_counts[seg_name or "unknown"] += 1
        else:
            skip_reasons[reason] += 1
            if i < 2:
                # Log first 2 skipped events with key fields for diagnosis
                ev_name = (ev.get("name") or "")[:40]
                dates = ev.get("dates") or {}
                start_d = (dates.get("start") or {})
                venues = (ev.get("_embedded") or {}).get("venues") or []
                v0 = venues[0] if venues else {}
                cc = ((v0.get("country") or {}).get("countryCode") or "")
                print(f"  [ticketmaster skip] reason={reason} name={ev_name!r} "
                      f"localDate={start_d.get('localDate')!r} "
                      f"dateTBD={start_d.get('dateTBD')} "
                      f"cc={cc!r}", file=sys.stderr)

    if seg_counts:
        breakdown = ", ".join(f"{s}={n}" for s, n in seg_counts.most_common())
        print(f"  [ticketmaster] segments: {breakdown}")
    if skip_reasons:
        skip_summary = ", ".join(f"{r}={n}" for r, n in skip_reasons.most_common())
        print(f"  [ticketmaster] skipped: {skip_summary}")

    return out


def parse_ticketmaster_event(ev, cat, kanji):
    """Returns (event_dict, None) on success, (None, reason) on rejection."""
    name = (ev.get("name") or "").strip()
    if not name:
        return None, "no_name"

    dates = ev.get("dates") or {}
    start_obj = dates.get("start") or {}
    start = (start_obj.get("localDate") or
             (start_obj.get("dateTime") or "")[:10] or "")
    if not start:
        return None, "no_start_date"
    end = (dates.get("end") or {}).get("localDate") or start

    venues = (ev.get("_embedded") or {}).get("venues") or []
    venue = venues[0] if venues else {}
    cc = ((venue.get("country") or {}).get("countryCode") or "").upper()
    if cc and cc != "JP":
        return None, "wrong_country"

    venue_name = venue.get("name", "") or ""
    city = (venue.get("city") or {}).get("name", "") or ""
    state = (venue.get("state") or {}).get("name", "") or ""
    prefecture = BIT_PREF_MAP.get(state, state) or guess_prefecture(state + " " + city) or "東京"

    # Best image: prefer largest by area, ratio 16_9 first
    images = ev.get("images") or []
    image = ""
    if images:
        sixteen_nine = [i for i in images if i.get("ratio") == "16_9"]
        pool = sixteen_nine or images
        best = max(pool, key=lambda i: (i.get("width") or 0) * (i.get("height") or 0))
        image = best.get("url") or ""

    desc = ((ev.get("info") or ev.get("pleaseNote") or "") or "").strip()[:140]
    if not desc:
        desc = f"{name} · live in Japan"

    return ({
        "id": f"tm-{ev.get('id') or stable_id('tm', name, start)}",
        "title_ja": name,
        "title_zh": name,
        "category": cat,
        "kanji": kanji,
        "date_start": start,
        "date_end": end,
        "prefecture": prefecture,
        "city": city,
        "venue": venue_name,
        "description_ja": desc,
        "description_zh": desc,
        "url": ev.get("url", "") or "",
        "image_url": image,
        "featured": False,
        "source": "ticketmaster",
    }, None)


# Add new sources here. Each is (name, fetch_function).
SOURCES = [
    ("connpass", fetch_connpass),
    ("walkerplus", fetch_walkerplus),
    ("walkerplus_genre", fetch_walkerplus_genre),
    ("bandsintown", fetch_bandsintown),
    ("ticketmaster", fetch_ticketmaster),
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
