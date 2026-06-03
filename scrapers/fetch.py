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
    "宮城": "宮城", "仙台": "宮城",
    "広島": "広島",
    "静岡": "静岡", "浜松": "静岡",
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
      "演劇", "舞台", "公演", "アイドル", "K-POP", "シンフォニー",
      "歌劇", "オペラ", "リサイタル", "朗読劇", "落語", "寄席",
      "バレエ", "能", "狂言", "歌舞伎"), "音楽", "響"),
    # Nature: flowers, parks, scenic
    (("桜", "さくら", "サクラ", "紅葉", "もみじ", "紫陽花", "あじさい",
      "梅", "藤", "牡丹", "薔薇", "バラ", "ライラック", "コスモス",
      "ひまわり", "向日葵", "チューリップ", "菜の花",
      "ツツジ", "つつじ", "サツキ", "シャクヤク", "シャクナゲ",
      "見ごろ", "見頃", "開花", "開園",
      "イルミネーション", "庭園", "公園", "ガーデン",
      "動物園", "水族館", "植物園", "牧場",
      "狩り", "苺", "イチゴ", "ぶどう", "りんご", "みかん"), "自然", "花"),
    # Food / drink
    (("グルメ", "ビアガーデン", "ビール", "ワイン", "日本酒",
      "カフェ", "ラーメン", "焼肉", "寿司", "スイーツ",
      "バーベキュー", "BBQ", "食フェス", "肉フェス",
      "ローズツアー"), "グルメ", "食"),
    # Markets
    (("マーケット", "マルシェ", "市場", "蚤の市", "骨董", "フリマ",
      "朝市", "夜市", "クラフト市", "手作り市"), "マーケット", "市"),
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


# ============== Cross-source de-duplication ==============

# Higher number = lower priority. Curated wins. Walkerplus (richest data) next.
_SOURCE_PRIORITY = {
    # Lower wins on dedup. Curated > authoritative > broad > niche.
    "manual":           0,
    "walkerplus":       1,
    "walkerplus_genre": 2,
    # Regional/official tourism boards — high editorial quality.
    "gotokyo":          3,
    "kyoto":            3,
    "osaka":            3,
    "okinawa":          3,
    # Domain-specific aggregators.
    "museum":           4,
    "tokyoartbeat":     5,
    "npb":              5,
    "tokyo_cheapo":     6,
    "ticketmaster":     7,
    "pia":              7,
    "confetti":         7,
    "bandsintown":      8,
    "tiget":            8,
    "connpass":         9,
}


def _normalize_title(s):
    """Strip year, edition markers, decoration symbols, whitespace, and
    case. Used as the cross-source dedup key — different sources spell
    the same event very differently."""
    if not s:
        return ""
    s = s.lower()
    # Year — \b doesn't fire on CJK boundaries, so match anywhere.
    s = re.sub(r'(?:^|[^0-9])(20\d\d|19\d\d)(?=[^0-9]|$)', '', s)
    # "第N回" / "第N届" edition markers
    s = re.sub(r'第\s*\d+\s*[回届]', '', s)
    # Roman volume markers
    s = re.sub(r'\bvol\.?\s*\d+\b', '', s)
    # Decoration / punctuation
    s = re.sub(
        r'[\s\-_/&·。、，,.()（）「」『』《》〈〉【】〔〕'
        r'！!?？:：;；~～〜=＝・※"\'★☆◇◆▼▽▲△○●◎◯♥♡|｜]+',
        '',
        s,
    )
    return s


def _normalize_venue(s):
    """Looser normalization for venue strings — strip whitespace and a few
    organizational suffix markers so 'Tokyo Dome' vs '東京ドーム' nuances
    don't matter; but mostly used as a presence-and-equality check."""
    if not s:
        return ""
    s = re.sub(r'\s+', '', s.lower())
    s = re.sub(r'[（）()「」『』【】〔〕,，、。.]+', '', s)
    return s


def _date_overlap(a, b):
    s1, e1 = a.get("date_start", ""), a.get("date_end", "") or a.get("date_start", "")
    s2, e2 = b.get("date_start", ""), b.get("date_end", "") or b.get("date_start", "")
    if not s1 or not s2:
        return False
    return s1 <= e2 and s2 <= e1


def _looks_same_event(a, b):
    """Two events are likely the same when: same prefecture, dates
    overlap, AND either (a) titles look alike, or (b) the venue matches
    and titles aren't wildly different. With 15+ sources, the same
    exhibition / festival shows up under varied wording — keep the union
    of evidence permissive but require multiple signals."""
    if a.get("prefecture") != b.get("prefecture"):
        return False
    if not _date_overlap(a, b):
        return False

    def title_set(ev):
        s = {
            _normalize_title(ev.get("title_ja", "")),
            _normalize_title(ev.get("title_zh", "")),
        }
        for al in (ev.get("aliases") or []):
            s.add(_normalize_title(al))
        return {t for t in s if len(t) >= 4}

    titles_a = title_set(a)
    titles_b = title_set(b)

    # Same venue + overlapping dates + prefecture match is already a
    # strong duplicate signal, even if titles diverge slightly.
    va = _normalize_venue(a.get("venue", ""))
    vb = _normalize_venue(b.get("venue", ""))
    venue_match = bool(va) and bool(vb) and (va == vb or va in vb or vb in va)

    from difflib import SequenceMatcher
    for ta in titles_a:
        for tb in titles_b:
            if ta == tb:
                return True
            if len(ta) >= 6 and len(tb) >= 6 and (ta in tb or tb in ta):
                return True
            # Fuzzy ratio: 0.80 generally requires near-identical content;
            # drop to 0.65 when venue also matches.
            threshold = 0.65 if venue_match else 0.80
            if max(len(ta), len(tb)) >= 6:
                if SequenceMatcher(None, ta, tb).ratio() >= threshold:
                    return True
    return False


def merge_dedup(events):
    """Merge events from multiple sources, dropping fuzzy duplicates.
    Higher-priority source wins. Returns deduped list."""
    sorted_events = sorted(
        events,
        key=lambda e: _SOURCE_PRIORITY.get(e.get("source", "manual"), 99)
    )
    kept = []
    dropped = 0
    for ev in sorted_events:
        if any(_looks_same_event(ev, k) for k in kept):
            dropped += 1
            continue
        kept.append(ev)
    if dropped:
        print(f"  ⤵ dedup: dropped {dropped} cross-source duplicates")
    return kept


# ============== Translation (MyMemory, no API key) ==============

TRANSLATIONS_CACHE_FILE = DATA / "translations_cache.json"
_TRANS_CACHE = None
_TRANS_SESSION = None
_TRANS_FAILED = False

# When True, translate_*() defers API calls instead of blocking each scraper.
# Texts are queued in _TRANS_QUEUE with their source language, then resolved
# in parallel via _resolve_translations() at the end of the pipeline.
_TRANS_LAZY = False
_TRANS_QUEUE = {}  # text -> src ("ja" | "en")


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
    return _translate(text, src="ja")


def translate_en_to_zh(text):
    return _translate(text, src="en")


def _translate(text, src="ja"):
    """Public entry. In lazy mode (set by run()), uncached text is queued
    and "" is returned; the post-scrape pass resolves the queue in parallel
    and run() backfills events from the populated cache. In eager mode
    (default — used when scrapers are called directly outside run()) the
    API is hit synchronously."""
    if not text or not text.strip():
        return text
    cache = _load_trans()
    if text in cache:
        return cache[text]
    if _TRANS_LAZY:
        _TRANS_QUEUE[text] = src
        return ""
    return _translate_sync(text, src)


def _translate_sync(text, src="ja"):
    """Synchronous MyMemory call. Returns translated text on success,
    original text on any failure. Does NOT write to the module-level
    cache — callers handle persistence (avoids races in the worker pool).
    """
    import os
    global _TRANS_SESSION, _TRANS_FAILED

    if _TRANS_FAILED:
        return text
    if _TRANS_SESSION is None:
        _TRANS_SESSION = requests.Session()

    email = os.environ.get("MYMEMORY_EMAIL", "").strip()
    params = {"q": text[:500], "langpair": f"{src}|zh-CN"}
    if email:
        params["de"] = email

    try:
        r = _TRANS_SESSION.get(
            "https://api.mymemory.translated.net/get",
            params=params,
            timeout=8,
            headers={"User-Agent": "YoroBot/0.1 (+https://github.com/QA-Ray/yoro)"},
        )
        if r.status_code in (429, 403):
            _TRANS_FAILED = True
            print(f"  [translate] rate limited (HTTP {r.status_code}); "
                  f"remaining skipped this run", file=sys.stderr)
            return text
        r.raise_for_status()
        data = r.json()
        if str(data.get("responseStatus", "")) != "200":
            return text
        translated = (data.get("responseData", {})
                          .get("translatedText") or "").strip()
        if not translated:
            return text
        up = translated.upper()
        if any(m in up for m in _BAD_TRANS_MARKERS):
            return text
        if up == text.upper():
            return text
        return translated
    except Exception as e:
        print(f"  [translate] {type(e).__name__}: {e}", file=sys.stderr)
        return text


def _resolve_translations(max_workers=8):
    """Drain _TRANS_QUEUE via a thread pool, writing results back into the
    on-disk cache. Cache hits and items added after _TRANS_FAILED flips are
    skipped. Returns the number of API calls made."""
    cache = _load_trans()
    todo = [(t, src) for t, src in _TRANS_QUEUE.items()
            if t not in cache and t.strip()]
    if not todo:
        _TRANS_QUEUE.clear()
        return 0

    from concurrent.futures import ThreadPoolExecutor

    print(f"  translating {len(todo)} strings (pool={max_workers})…")

    def work(item):
        text, src = item
        return text, _translate_sync(text, src)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for text, result in pool.map(work, todo):
            cache[text] = result
    _TRANS_QUEUE.clear()
    return len(todo)


def _backfill_translations(events):
    """After translations resolve, sweep events and pull title_zh /
    description_zh from cache when scrapers left them empty due to lazy
    mode."""
    cache = _load_trans()
    for ev in events:
        for ja, zh in (("title_ja", "title_zh"),
                       ("description_ja", "description_zh")):
            if not ev.get(zh) and ev.get(ja):
                hit = cache.get(ev[ja])
                if hit:
                    ev[zh] = hit


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

    r = requests.get(url, params=params, headers=headers, timeout=25)
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


# Walkerplus prefecture codes — pattern is ar{REGION}{JIS_PREF}.
# Full 47-prefecture coverage. Names normalized to match the rest of the
# dataset (no 都/府/県 suffix; 北海道 kept whole).
# To verify codes: https://www.walkerplus.com/event_list/ar{XXYY}/
WALKERPLUS_PREFS = [
    ("ar0101", "北海道"),
    ("ar0202", "青森"),
    ("ar0203", "岩手"),
    ("ar0204", "宮城"),
    ("ar0205", "秋田"),
    ("ar0206", "山形"),
    ("ar0207", "福島"),
    ("ar0308", "茨城"),
    ("ar0309", "栃木"),
    ("ar0310", "群馬"),
    ("ar0311", "埼玉"),
    ("ar0312", "千葉"),
    ("ar0313", "東京"),
    ("ar0314", "神奈川"),
    ("ar0415", "新潟"),
    ("ar0516", "富山"),
    ("ar0517", "石川"),
    ("ar0518", "福井"),
    ("ar0419", "山梨"),
    ("ar0420", "長野"),
    ("ar0621", "岐阜"),
    ("ar0622", "静岡"),
    ("ar0623", "愛知"),
    ("ar0624", "三重"),
    ("ar0725", "滋賀"),
    ("ar0726", "京都"),
    ("ar0727", "大阪"),
    ("ar0728", "兵庫"),
    ("ar0729", "奈良"),
    ("ar0730", "和歌山"),
    ("ar0831", "鳥取"),
    ("ar0832", "島根"),
    ("ar0833", "岡山"),
    ("ar0834", "広島"),
    ("ar0835", "山口"),
    ("ar0936", "徳島"),
    ("ar0937", "香川"),
    ("ar0938", "愛媛"),
    ("ar0939", "高知"),
    ("ar1040", "福岡"),
    ("ar1041", "佐賀"),
    ("ar1042", "長崎"),
    ("ar1043", "熊本"),
    ("ar1044", "大分"),
    ("ar1045", "宮崎"),
    ("ar1046", "鹿児島"),
    ("ar1047", "沖縄"),
]

# How many listing pages to fetch per prefecture (~10 events/page).
WALKERPLUS_PAGES = 3


def fetch_walkerplus():
    """Walkerplus event listings (HTML page + Schema.org JSON-LD).

    Robots.txt explicitly allows /event_list/. Polite: 1.5s delay between
    requests. ~10 events per listing page; paginated up to WALKERPLUS_PAGES
    pages per prefecture (page 2+ via /N.html), across all 47 prefectures.
    Stops early when a page yields no events.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    }

    out = []
    for code, pref_name in WALKERPLUS_PREFS:
        pref_added = 0
        for page in range(1, WALKERPLUS_PAGES + 1):
            base = f"https://www.walkerplus.com/event_list/{code}/"
            url = base if page == 1 else f"{base}{page}.html"
            try:
                r = requests.get(url, headers=headers, timeout=25)
                r.raise_for_status()
            except Exception as e:
                print(f"  [walkerplus {pref_name} p{page}] "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                time.sleep(1.5)
                break  # stop paginating this prefecture on error

            blocks = re.findall(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                r.text, re.DOTALL,
            )

            page_added = 0
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
                        page_added += 1
            pref_added += page_added
            time.sleep(1.5)
            if page_added == 0:
                break  # no more events for this prefecture
        print(f"  [walkerplus {pref_name}] {pref_added} events")

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
    ("eg0102", "全国花火",       "祭り",   "火"),
    ("eg0135", "全国祭り",       "祭り",   "祭"),
]


# Genre feeds are national (not per-prefecture), so seasonal types like
# 花火 / 祭り need more depth to surface July–August events that sit past
# the nearer-dated listings. Paginate deeper than the per-prefecture feeds.
WALKERPLUS_GENRE_PAGES = 5


def fetch_walkerplus_genre():
    """Walkerplus genre feeds — sports, live music, theater, fireworks, festivals.

    Forces category based on the source genre URL (we trust Walkerplus's
    own classification more than keyword guessing for these specific types).
    Nationally curated; paginated up to WALKERPLUS_GENRE_PAGES per genre
    (page 2+ via /N.html), stopping early when a page yields no new events.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    }

    out = []
    for code, name, forced_cat, forced_kanji in WALKERPLUS_GENRES:
        genre_added = 0
        for page in range(1, WALKERPLUS_GENRE_PAGES + 1):
            base = f"https://www.walkerplus.com/event_list/{code}/"
            url = base if page == 1 else f"{base}{page}.html"
            try:
                r = requests.get(url, headers=headers, timeout=25)
                r.raise_for_status()
            except Exception as e:
                print(f"  [walkerplus genre {name} p{page}] "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                time.sleep(1.5)
                break

            blocks = re.findall(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                r.text, re.DOTALL,
            )
            page_added = 0
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
                    page_added += 1
            genre_added += page_added
            time.sleep(1.5)
            if page_added == 0:
                break
        print(f"  [walkerplus genre {name}] {genre_added} events")

    return out


# Bandsintown / Ticketmaster — English region → JP prefecture mapping
BIT_PREF_MAP = {
    "Tokyo": "東京", "Osaka": "大阪", "Kyoto": "京都",
    "Kanagawa": "神奈川", "Hokkaido": "北海道",
    "Hyogo": "兵庫", "Aichi": "愛知", "Saitama": "埼玉",
    "Chiba": "千葉", "Fukuoka": "福岡", "Nara": "奈良",
    "Hiroshima": "広島", "Miyagi": "宮城", "Niigata": "新潟",
    "Shizuoka": "静岡", "Okinawa": "沖縄",
}

# City-name fallback (e.g. Ticketmaster sometimes only has the city, not the prefecture)
CITY_TO_PREF = {
    "Nagoya": "愛知", "Yokohama": "神奈川",
    "Sapporo": "北海道", "Hakodate": "北海道",
    "Kobe": "兵庫", "Kyoto": "京都", "Tokyo": "東京",
    "Osaka": "大阪", "Nara": "奈良", "Fukuoka": "福岡",
    "Hiroshima": "広島", "Sendai": "宮城", "Niigata": "新潟",
    "Shizuoka": "静岡", "Naha": "沖縄", "Gifu": "岐阜",
    "Saitama": "埼玉", "Chiba": "千葉",
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
        "size": 50,  # cap volume — TM JP coverage is sparse anyway
        "sort": "date,asc",
    }
    try:
        r = requests.get(
            base, params=params, timeout=25,
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

    SPORT_WORDS_EN = (
        "basketball", "football", "soccer", "tennis", "golf",
        "shooting", "skateboard", "squash", "boxing", "wrestl",
        "athletics", "swim", "track", "marathon", "rugby",
        "volleyball", "judo", "karate", "kendo", "hockey",
        "weightlifting", "triathlon", "fencing", "archery",
        "gymnastics", "sepak", "sailing", "rowing", "canoe",
        "cycling", "diving", "taekwondo", "baseball",
        "pentathlon", "cricket", "handball", "kabaddi", "wushu",
        "softball", "billiards", "bowling", "ju-jitsu",
        "esport", "polo", "lawn bowls",
    )

    # Asian Games / Olympic-style event code pattern: "Basketball - BKB01"
    GAMES_PATTERN = re.compile(r'^[A-Za-z\s\-/&]+ - [A-Z]{2,4}\d+$')

    def classify(seg_name, name, classifications):
        # 1. Direct segment match
        if seg_name and seg_name.lower() in SEGMENT_MAP:
            return SEGMENT_MAP[seg_name.lower()]
        # 2. Genre fallback
        if classifications:
            genre = ((classifications[0].get("genre") or {}).get("name") or "").lower()
            if genre and genre != "undefined" and genre in SEGMENT_MAP:
                return SEGMENT_MAP[genre]
        # 3. Asian Games / Olympic naming pattern
        if GAMES_PATTERN.match((name or "").strip()):
            return "スポーツ", "競"
        # 4. English sport keywords
        nl = (name or "").lower()
        if any(w in nl for w in SPORT_WORDS_EN):
            return "スポーツ", "競"
        # 5. Default to music
        return "音楽", "響"

    seg_counts = collections.Counter()
    skip_reasons = collections.Counter()
    out = []
    for i, ev in enumerate(events):
        classifications = ev.get("classifications") or []
        seg_name = ""
        if classifications:
            seg_name = (classifications[0].get("segment") or {}).get("name", "") or ""
        cat, kanji = classify(seg_name, ev.get("name", ""), classifications)

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

    venue_name = (venue.get("name") or "").strip()
    city = ((venue.get("city") or {}).get("name") or "").strip()
    state = ((venue.get("state") or {}).get("name") or "").strip()

    # Prefecture detection: check direct keys, then substring search in venue
    # name (Ticketmaster sometimes only encodes location in venue name like
    # "Aichi International Arena" with no state/city set)
    prefecture = BIT_PREF_MAP.get(state) or CITY_TO_PREF.get(city)
    if not prefecture:
        location_text = f"{state} {city} {venue_name}"
        for eng, jp in {**BIT_PREF_MAP, **CITY_TO_PREF}.items():
            if eng in location_text:
                prefecture = jp
                break
    if not prefecture:
        prefecture = guess_prefecture(state + " " + city + " " + venue_name) or "東京"

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


PIA_PAGES = [
    ("/zh-CHS/event/",  None),         # generic events — let keyword guesser decide cat
    ("/zh-CHS/sports/", "スポーツ"),    # sports landing page
    ("/zh-CHS/classic/", "音楽"),       # classical music
]


def fetch_pia():
    """PIA Tickets (チケットぴあ) Chinese landing pages.

    PIA's HTML uses WOVN.io for client-side JP→ZH swap; our scraper
    sees the underlying Japanese. Cards are <a><figure><figcaption>
    <h2>TITLE</h2><p>DESC</p></figcaption></figure></a>. Dates are
    not in structured form — extracted by regex on description text
    (looks for 20XX/M/D patterns). Skip events without parseable dates.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    }

    card_re = re.compile(
        r'<a[^>]+href="(https?://t\.pia\.jp/zh-CHS/pia/events/[^"]+/)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )

    out = []
    for path, cat_hint in PIA_PAGES:
        url = f"https://t.pia.jp{path}"
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"  [pia {path}] {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(1.5)
            continue

        seen = set()
        added = 0
        for ev_url, body in card_re.findall(r.text):
            if ev_url in seen:
                continue
            seen.add(ev_url)
            ev = parse_pia_card(ev_url, body, cat_hint)
            if ev:
                out.append(ev)
                added += 1
        print(f"  [pia {path.rstrip('/').split('/')[-1] or 'home'}] {added} events")
        time.sleep(1.5)

    return out


def parse_pia_card(url, body, cat_hint):
    title_m = re.search(r'<h2[^>]*>([^<]+)</h2>', body)
    if not title_m:
        return None
    title = title_m.group(1).strip()
    if not title:
        return None

    img_m = re.search(r'<img[^>]+src="([^"]+)"', body)
    image = (img_m.group(1) if img_m else "").strip()
    if image.startswith("//"):
        image = "https:" + image

    desc_m = re.search(r'<p[^>]*>([^<]+)</p>', body)
    desc = (desc_m.group(1).strip() if desc_m else "")[:200]

    # Extract dates from anywhere in card body — accept 2025-2027 only
    date_matches = re.findall(r'(20\d{2})[/.\-](\d{1,2})[/.\-](\d{1,2})', body)
    valid_dates = []
    for y, m, d in date_matches:
        y, m, d = int(y), int(m), int(d)
        if not (2025 <= y <= 2027):
            continue
        if not (1 <= m <= 12 and 1 <= d <= 31):
            continue
        valid_dates.append(f"{y}-{m:02d}-{d:02d}")
    valid_dates = sorted(set(valid_dates))
    if not valid_dates:
        return None  # skip events without parseable dates — pure title without date is noise

    date_start = valid_dates[0]
    date_end = valid_dates[-1] if len(valid_dates) > 1 else date_start

    # Category: keyword guess on title+desc, then fall back to genre hint
    cat, kanji = guess_cat_kanji(title, desc[:140])
    if cat == "その他" and cat_hint:
        cat = cat_hint
        kanji = {"スポーツ": "競", "音楽": "響"}.get(cat_hint, "事")

    prefecture = guess_prefecture(title + " " + desc) or "東京"

    title_zh = translate_ja_to_zh(title)
    desc_zh = translate_ja_to_zh(desc[:140])

    return {
        "id": stable_id("pia", url),
        "title_ja": title,
        "title_zh": title_zh,
        "category": cat,
        "kanji": kanji,
        "date_start": date_start,
        "date_end": date_end,
        "prefecture": prefecture,
        "city": "",
        "venue": "",
        "description_ja": desc[:140],
        "description_zh": desc_zh,
        "url": url,
        "image_url": image,
        "featured": False,
        "source": "pia",
    }


def guess_cat_kanji_en(title, desc=""):
    """English-keyword variant of guess_cat_kanji for non-JP sources."""
    text = (title + " " + desc).lower()
    EN_KEYWORDS = [
        (("matsuri", "festival"),                                 "祭り",   "祭"),
        (("fireworks", "hanabi"),                                 "祭り",   "火"),
        (("sumo", "marathon", "baseball", "soccer", "tennis",
          "olympic", "tournament", "rugby", "golf", "basketball"),"スポーツ", "競"),
        (("concert", "live", "musical", "opera", "jazz",
          "classical music", "philharmonic", "symphony"),         "音楽",   "響"),
        (("exhibition", "museum", "gallery", " art ", "art ",
          "paintings"),                                            "展覧会", "展"),
        (("cherry blossom", "sakura", "flower", "garden",
          "blossom", "leaves", "autumn", "winter illumination"),  "自然",   "花"),
        (("market", "flea", "fair", "fair ", "antique"),          "マーケット", "市"),
        (("food", "gourmet", "beer", "wine", "coffee",
          "oktoberfest", "ramen", "sushi"),                       "グルメ", "食"),
    ]
    for keywords, cat, kanji in EN_KEYWORDS:
        if any(kw in text for kw in keywords):
            return cat, kanji
    return "その他", "事"


_MONTHS_EN = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], start=1
)}


def parse_en_date(s, today=None):
    """Parse "Apr 29" → "2026-04-29". Year inferred (next year if past)."""
    if not today:
        today = date.today()
    s = (s or "").strip().lower()
    m = re.match(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})', s)
    if not m:
        return None
    month = _MONTHS_EN.get(m.group(1))
    day = int(m.group(2))
    if not month or not (1 <= day <= 31):
        return None
    year = today.year
    # If parsed month is more than 3 months behind today, roll to next year
    if month < today.month - 3:
        year += 1
    return f"{year}-{month:02d}-{day:02d}"


def fetch_tokyo_cheapo():
    """Tokyo Cheapo — English traveler-oriented Tokyo events.

    Cards are <article class="card--event">…</article> with date,
    title, excerpt, image, link inline. Yields 20+ events per fetch,
    all in English (no translation needed).
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
    }
    url = "https://tokyocheapo.com/events/"
    try:
        r = requests.get(url, headers=headers, timeout=25)
        r.raise_for_status()
    except Exception as e:
        print(f"  [tokyocheapo] {type(e).__name__}: {e}", file=sys.stderr)
        return []

    cards = re.findall(
        r'<article[^>]*class="[^"]*card--event[^"]*"[^>]*>(.*?)</article>',
        r.text, re.DOTALL,
    )
    out = []
    for c in cards:
        ev = parse_tokyo_cheapo_card(c)
        if ev:
            out.append(ev)
    print(f"  [tokyocheapo] {len(out)} events")
    return out


def parse_tokyo_cheapo_card(card_html):
    title_m = re.search(r'class="card__title">\s*<a[^>]+>([^<]+)</a>', card_html)
    if not title_m:
        return None
    title = title_m.group(1).strip()
    if not title:
        return None

    url_m = re.search(r'class="card__image"[^>]+href="([^"]+)"', card_html)
    url = (url_m.group(1) if url_m else "").strip()
    if url.startswith("/"):
        url = "https://tokyocheapo.com" + url

    excerpt_m = re.search(r'class="card__excerpt">([^<]+)</p>', card_html)
    excerpt = (excerpt_m.group(1).strip() if excerpt_m else "")[:160]

    img_m = re.search(r'<img[^>]+src="([^"]+)"', card_html)
    image = (img_m.group(1) if img_m else "").strip()

    raw_dates = re.findall(r'class="date">([^<]+)</div>', card_html)
    parsed = [parse_en_date(s) for s in raw_dates]
    parsed = [d for d in parsed if d]
    if not parsed:
        return None
    date_start, date_end = parsed[0], (parsed[1] if len(parsed) > 1 else parsed[0])

    cat, kanji = guess_cat_kanji_en(title, excerpt)
    # Default to Tokyo unless title hints otherwise (Yokohama / Kamakura / etc)
    prefecture = guess_prefecture(title) or "東京"

    title_zh = translate_en_to_zh(title)
    desc_zh = translate_en_to_zh(excerpt)

    return {
        "id": stable_id("tc", url or title),
        "title_ja": title,         # English original (shown as subtitle if different)
        "title_zh": title_zh,
        "category": cat,
        "kanji": kanji,
        "date_start": date_start,
        "date_end": date_end,
        "prefecture": prefecture,
        "city": "",
        "venue": "",
        "description_ja": excerpt,
        "description_zh": desc_zh,
        "url": url,
        "image_url": image,
        "featured": False,
        "source": "tokyo_cheapo",
    }


# NPB stadium-name keyword → prefecture. Order matters: longer / more
# specific keywords first to avoid false hits (e.g. "東京" matches both
# 東京ドーム and 神宮; the latter wins because it appears earlier).
NPB_STADIUM_PREF = [
    ("PayPay", "福岡"), ("みずほ", "福岡"),
    ("ZOZOマリン", "千葉"), ("マリン", "千葉"),
    ("楽天モバイル", "宮城"), ("Koboパーク", "宮城"),
    ("エスコン", "北海道"),
    ("ベルーナ", "埼玉"),
    ("京セラ", "大阪"), ("ほっと神戸", "兵庫"),
    ("東京ドーム", "東京"), ("神宮", "東京"),
    ("甲子園", "兵庫"),
    ("バンテリン", "愛知"), ("ナゴヤ", "愛知"),
    ("マツダ", "広島"),
    ("横浜", "神奈川"), ("ハマスタ", "神奈川"),
    # Common alternate / regional venues
    ("札幌ドーム", "北海道"),
    ("ほっともっと", "兵庫"),
    ("ZOZO", "千葉"),
    ("沖縄", "沖縄"),
    ("浜松", "静岡"),
]


def npb_stadium_to_prefecture(name):
    if not name:
        return None
    for kw, pref in NPB_STADIUM_PREF:
        if kw in name:
            return pref
    return guess_prefecture(name)


def fetch_npb():
    """NPB (npb.jp) — official Japanese pro baseball schedule.

    Fetches current + next month from /games/{year}/schedule_MM_detail.html
    (HTML table with id="dateMMDD" rows). ~70-80 games per month.
    Skips 予備日 (reserve / weather-backup days).
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
    }

    today = date.today()
    months = [(today.year, today.month)]
    nm = today.month + 1
    ny = today.year
    if nm > 12:
        nm = 1
        ny += 1
    months.append((ny, nm))

    today_iso = today.isoformat()
    out = []
    for year, month in months:
        url = f"https://npb.jp/games/{year}/schedule_{month:02d}_detail.html"
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"  [npb] {url} {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(1.5)
            continue

        # Server omits charset header; requests defaults to ISO-8859-1.
        # Body is actually UTF-8.
        r.encoding = "utf-8"
        rows = re.findall(
            r'<tr id="date(\d{4})"[^>]*>(.*?)</tr>',
            r.text, re.DOTALL,
        )
        added = 0
        for mmdd, body in rows:
            ev = parse_npb_row(year, mmdd, body, today_iso)
            if ev:
                out.append(ev)
                added += 1
        print(f"  [npb] {year}-{month:02d}: {added} games")
        time.sleep(1.5)
    return out


def parse_npb_row(year, mmdd, body, today_iso):
    # Skip reserve / canceled days
    if re.search(r'class="cancel"', body):
        return None
    t1 = re.search(r'class="team1">([^<]+)<', body)
    t2 = re.search(r'class="team2">([^<]+)<', body)
    if not t1 or not t2:
        return None
    team1 = t1.group(1).strip()
    team2 = t2.group(1).strip()
    if not team1 or not team2:
        return None

    mm, dd = int(mmdd[:2]), int(mmdd[2:])
    date_iso = f"{year:04d}-{mm:02d}-{dd:02d}"
    if date_iso < today_iso:
        return None

    place_m = re.search(r'class="place">([^<]+)<', body)
    venue = place_m.group(1).strip() if place_m else ""
    # NPB pads venue names with fullwidth spaces (e.g. "横　浜"); collapse
    # so stadium→prefecture keyword matches work.
    venue = venue.replace("　", "").replace(" ", "")
    time_m = re.search(r'class="time">([^<]+)<', body)
    time_str = time_m.group(1).strip() if time_m else ""

    prefecture = npb_stadium_to_prefecture(venue) or "東京"

    title = f"プロ野球: {team1} vs {team2}"
    title_zh = translate_ja_to_zh(title)
    desc = f"{venue} {time_str}".strip() if (venue or time_str) else ""

    return {
        "id": stable_id("npb", date_iso, team1, team2, venue),
        "title_ja": title,
        "title_zh": title_zh,
        "category": "スポーツ",
        "kanji": "競",
        "date_start": date_iso,
        "date_end": date_iso,
        "prefecture": prefecture,
        "city": "",
        "venue": venue,
        "description_ja": desc,
        "description_zh": "",
        "url": f"https://npb.jp/games/{year}/",
        "image_url": "",
        "featured": False,
        "source": "npb",
    }


TIGET_DURATION_RE = re.compile(
    r'(\d{4})年(\d{1,2})月(\d{1,2})日'
    r'(?:[^〜<]*〜\s*(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日)?'
)


def parse_tiget_duration(s):
    """Parse '開催：2026年06月27日(土) 〜 06月28日(日)' style strings.
    End is optional (single-day event); end-year/month inherit from start.
    """
    m = TIGET_DURATION_RE.search(s)
    if not m:
        return None, None
    y1, m1, d1, y2s, m2s, d2s = m.groups()
    y1, m1, d1 = int(y1), int(m1), int(d1)
    start = f"{y1:04d}-{m1:02d}-{d1:02d}"
    if not d2s:
        return start, start
    y2 = int(y2s) if y2s else y1
    if m2s:
        m2 = int(m2s)
        if not y2s and m2 < m1:
            y2 = y1 + 1
    else:
        m2 = m1
    return start, f"{y2:04d}-{m2:02d}-{int(d2s):02d}"


def fetch_tiget():
    """TIGET (tiget.net) — indie / underground live music ticketing.

    Fills the gap left by Bandsintown / Ticketmaster, which mostly cover
    big-name international tours. ~24 unique events per page; pull 3
    pages (≈72 events) with polite delays. All entries are 音楽.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
    }
    pages = ["https://tiget.net/events"] + [
        f"https://tiget.net/events?page={p}" for p in (2, 3)
    ]

    out = []
    seen_ids = set()
    for url in pages:
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"  [tiget] {url} {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(1.5)
            continue

        # Each card appears twice (desktop + mobile responsive markup);
        # dedup by event id in parse step.
        chunks = re.split(r'<div class="event-box[ "]', r.text)[1:]
        added = 0
        for c in chunks:
            ev = parse_tiget_card(c)
            if ev and ev["id"] not in seen_ids:
                seen_ids.add(ev["id"])
                out.append(ev)
                added += 1
        print(f"  [tiget] page {url.split('=')[-1] if '=' in url else 1}: {added} events")
        time.sleep(1.5)
    return out


def parse_tiget_card(html):
    id_m = re.search(r'href="/events/(\d+)"', html)
    if not id_m:
        return None
    event_id = id_m.group(1)

    title_m = re.search(
        r'class="event-title"[^>]*>\s*<a[^>]*>([^<]+)</a>',
        html,
    )
    if not title_m:
        return None
    title = title_m.group(1).strip()
    if not title:
        return None

    dur_m = re.search(r'class="play-date"[^>]*>([^<]+)<', html)
    if not dur_m:
        return None
    start, end = parse_tiget_duration(dur_m.group(1))
    if not start:
        return None

    area_m = re.search(r'class="event-area"[^>]*>([^<]+)<', html)
    prefecture = "東京"
    if area_m:
        raw = area_m.group(1).replace("場所：", "").strip()
        pref_clean = (raw.replace("都", "")
                         .replace("府", "")
                         .replace("県", "")
                         .strip())
        if pref_clean in PREF_KEYWORDS.values():
            prefecture = pref_clean
        else:
            prefecture = guess_prefecture(raw) or "東京"

    perf_m = re.search(r'class="performer"[^>]*>([^<]*)<', html)
    performer = perf_m.group(1).strip() if perf_m else ""

    img_m = re.search(r'data-src="(https?://[^"]+)"', html)
    image = img_m.group(1) if img_m else ""
    if "no-image" in image:
        image = ""

    event_url = f"https://tiget.net/events/{event_id}"
    title_zh = translate_ja_to_zh(title)
    desc_zh = translate_ja_to_zh(performer) if performer else ""

    return {
        "id": stable_id("tg", event_id),
        "title_ja": title,
        "title_zh": title_zh,
        "category": "音楽",
        "kanji": "音",
        "date_start": start,
        "date_end": end,
        "prefecture": prefecture,
        "city": "",
        "venue": "",
        "description_ja": performer,
        "description_zh": desc_zh,
        "url": event_url,
        "image_url": image,
        "featured": False,
        "source": "tiget",
    }


_CONFETTI_DATE_RE = re.compile(
    r'(\d{4})年(\d{1,2})月(\d{1,2})日'
    r'(?:[^〜]*〜\s*(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日)?'
)


def fetch_confetti():
    """Confetti (confetti-web.com) — theater / stage tickets.

    Cloudflare-protected, so this depends on Scrapling's StealthyFetcher
    (Playwright + anti-detection). If Scrapling isn't installed (e.g. CI
    without browser binaries), this source returns [] gracefully — the
    rest of the pipeline is unaffected.

    We scrape the homepage rather than /events: /events is dominated by
    archived streaming rentals (★配信チケット), while the homepage
    "popular-event" carousel surfaces actual upcoming physical shows
    with venue info.
    """
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError:
        print("  [confetti] scrapling not installed — skipping",
              file=sys.stderr)
        return []

    try:
        page = StealthyFetcher.fetch(
            "https://www.confetti-web.com/",
            headless=True,
            solve_cloudflare=True,
            timeout=60_000,
        )
    except Exception as e:
        print(f"  [confetti] {type(e).__name__}: {e}", file=sys.stderr)
        return []

    html = getattr(page, "html_content", "") or ""
    if not html:
        print("  [confetti] empty response", file=sys.stderr)
        return []

    # Homepage uses two card variants (popular-event carousel and a
    # ranked event list) — both share the same inner markup. /events
    # is dominated by streaming archives and is avoided.
    chunks = re.split(
        r'<a href="/events/(\d+)"[^>]*class="(?:event|popular-event)[ \"]',
        html,
    )[1:]
    out = []
    seen = set()
    for i in range(0, len(chunks), 2):
        event_id = chunks[i]
        body = chunks[i + 1] if i + 1 < len(chunks) else ""
        ev = parse_confetti_card(event_id, body)
        if ev and ev["id"] not in seen:
            seen.add(ev["id"])
            out.append(ev)
    print(f"  [confetti] {len(out)} events")
    return out


def parse_confetti_card(event_id, body):
    cut = body.find('<a href="/events/')
    if cut > 0:
        body = body[:cut]

    # Card layout:
    #   <p class="fs-2xs fw-bold ...">subtitle (organizer)</p>  [optional]
    #   <p class="fs-sm fw-bold ...">title</p>                  [required]
    #   <p class="fs-3xs ...">date range</p>                    [required]
    #   <p class="fs-2xs ... text-text-gray ...">venue</p>      [usually present]
    title_m = re.search(
        r'<p class="fs-sm fw-bold[^"]*"[^>]*>([^<]+)</p>',
        body,
    )
    if not title_m:
        return None
    title = title_m.group(1).strip()
    if not title:
        return None
    if "配信" in title or "レンタル動画" in title:
        return None

    date_m = re.search(r'<p class="fs-3xs[^"]*"[^>]*>([^<]+)</p>', body)
    if not date_m:
        return None
    dm = _CONFETTI_DATE_RE.search(date_m.group(1))
    if not dm:
        return None
    y1, m1, d1, y2s, m2s, d2s = dm.groups()
    start = f"{int(y1):04d}-{int(m1):02d}-{int(d1):02d}"
    if d2s:
        y2 = int(y2s) if y2s else int(y1)
        m2 = int(m2s) if m2s else int(m1)
        if not y2s and m2 < int(m1):
            y2 += 1
        end = f"{y2:04d}-{m2:02d}-{int(d2s):02d}"
    else:
        end = start

    venue_m = re.search(
        r'<p class="[^"]*text-text-gray[^"]*"[^>]*>([^<]+)</p>',
        body,
    )
    venue = venue_m.group(1).strip() if venue_m else ""

    img_m = re.search(r'<img\s+src="([^"]+)"', body)
    image = img_m.group(1) if img_m else ""

    prefecture = guess_prefecture(venue + " " + title) or "東京"
    title_zh = translate_ja_to_zh(title)

    return {
        "id": stable_id("cnf", event_id),
        "title_ja": title,
        "title_zh": title_zh,
        "category": "その他",
        "kanji": "舞",
        "date_start": start,
        "date_end": end,
        "prefecture": prefecture,
        "city": "",
        "venue": venue,
        "description_ja": "",
        "description_zh": "",
        "url": f"https://www.confetti-web.com/events/{event_id}",
        "image_url": image,
        "featured": False,
        "source": "confetti",
    }


_GOTOKYO_DATE_RE = re.compile(
    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})'
    r'(?:\s*-\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4}))?'
)


def parse_gotokyo_dates(s):
    """Parse 'Jun 10, 2026 - Jun 14, 2026' (or single 'Jun 10, 2026')."""
    if not s:
        return None, None
    m = _GOTOKYO_DATE_RE.search(s)
    if not m:
        return None, None
    mon1, d1, y1, mon2, d2, y2 = m.groups()
    m1 = _MONTHS_EN.get(mon1.lower())
    if not m1:
        return None, None
    start = f"{int(y1):04d}-{m1:02d}-{int(d1):02d}"
    if mon2 and d2 and y2:
        m2 = _MONTHS_EN.get(mon2.lower())
        end = f"{int(y2):04d}-{m2:02d}-{int(d2):02d}"
    else:
        end = start
    return start, end


def fetch_gotokyo():
    """GO TOKYO (gotokyo.org) — Tokyo official tourism board.

    Curated event listings via the travel-directory search. Querying with
    a wide date window (today → +90 days) returns ~30-50 future events.
    All entries are prefecture=東京.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
    }
    today = date.today()
    end_window = date.fromordinal(today.toordinal() + 90)
    url = (
        "https://www.gotokyo.org/ja/travel-directory/result/index/template/"
        "152,153,154,155,156,157,158,159,160,161,222,259/"
        f"event_date_st/{today.isoformat()}/event_date_ed/{end_window.isoformat()}"
    )
    out = []
    seen = set()
    # Walk up to 5 pages (their pagination is /page/N at URL end)
    for page in range(1, 6):
        page_url = url if page == 1 else f"{url}/page/{page}"
        try:
            r = requests.get(page_url, headers=headers, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"  [gotokyo] page {page} {type(e).__name__}: {e}",
                  file=sys.stderr)
            break

        items = re.findall(
            r'<li class="result_lists_li"[^>]*>(.*?)</li>',
            r.text, re.DOTALL,
        )
        if not items:
            break
        added = 0
        for raw in items:
            ev = parse_gotokyo_item(raw)
            if ev and ev["id"] not in seen:
                seen.add(ev["id"])
                out.append(ev)
                added += 1
        print(f"  [gotokyo] page {page}: {added} events")
        if added == 0:
            break
        time.sleep(1.5)
    return out


def parse_gotokyo_item(html):
    link_m = re.search(r'<a href="(/jp/spot/ev\d+/[^"]*)"', html)
    if not link_m:
        return None
    href = link_m.group(1)
    title_m = re.search(r'class="result_name"[^>]*>([^<]+)<', html)
    if not title_m:
        return None
    title = title_m.group(1).strip()
    if not title:
        return None

    date_m = re.search(r'class="result_date"[^>]*>([^<]+)<', html)
    start, end = parse_gotokyo_dates(date_m.group(1) if date_m else "")
    if not start:
        return None

    img_m = re.search(r'<img[^>]+src="([^"]+\.(?:webp|jpg|jpeg|png))"', html)
    image = img_m.group(1) if img_m else ""
    if image.startswith("/"):
        image = "https://www.gotokyo.org" + image

    cat, kanji = guess_cat_kanji(title)
    title_zh = translate_ja_to_zh(title)

    return {
        "id": stable_id("gtk", href),
        "title_ja": title,
        "title_zh": title_zh,
        "category": cat,
        "kanji": kanji,
        "date_start": start,
        "date_end": end,
        "prefecture": "東京",
        "city": "",
        "venue": "",
        "description_ja": "",
        "description_zh": "",
        "url": f"https://www.gotokyo.org{href}",
        "image_url": image,
        "featured": False,
        "source": "gotokyo",
    }


def fetch_kyoto():
    """Kyoto Travel (ja.kyoto.travel) — official Kyoto tourism.

    Iterates category_id=1..6. Each card carries data-begin_date /
    data-end_date attributes, so date parsing is trivial.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
    }
    out = []
    seen = set()
    for cat_id in range(1, 6):
        url = f"https://ja.kyoto.travel/event/search.php?category_id={cat_id}"
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"  [kyoto] cat {cat_id} {type(e).__name__}: {e}",
                  file=sys.stderr)
            time.sleep(1.5)
            continue

        # Split on the unique outer-card marker. Inner `<li class="item">`
        # appears in tag sub-lists, so a generic regex would over-match.
        chunks = re.split(r'<li class="item"\s+data-is_finished', r.text)[1:]
        added = 0
        for raw in chunks:
            ev = parse_kyoto_item(raw)
            if ev and ev["id"] not in seen:
                seen.add(ev["id"])
                out.append(ev)
                added += 1
        print(f"  [kyoto] cat {cat_id}: {added} events")
        time.sleep(1.5)
    return out


def parse_kyoto_item(html):
    begin_m = re.search(r'data-begin_date="(\d{4}-\d{2}-\d{2})', html)
    end_m = re.search(r'data-end_date="(\d{4}-\d{2}-\d{2})', html)
    if not begin_m:
        return None
    start = begin_m.group(1)
    end = end_m.group(1) if end_m else start

    id_m = re.search(r'event_id=(\d+)', html)
    if not id_m:
        return None
    event_id = id_m.group(1)

    title_m = re.search(
        r'class="tit"[^>]*>\s*<a[^>]*>([^<]+)</a>',
        html,
    )
    if not title_m:
        return None
    title = title_m.group(1).strip()
    if not title:
        return None

    img_m = re.search(r'<img[^>]+src="([^"]+)"[^>]+class="viewPc"', html)
    if not img_m:
        img_m = re.search(r'<img[^>]+src="([^"]+\.(?:jpg|jpeg|png|webp))"', html)
    image = img_m.group(1) if img_m else ""
    if "noimage" in image:
        image = ""
    if image.startswith("/"):
        image = "https://ja.kyoto.travel" + image

    cat_m = re.search(r'class="cat"[^>]*>\s*<a[^>]*>([^<]+)</a>', html)
    cat_text = cat_m.group(1).strip() if cat_m else ""
    cat, kanji = guess_cat_kanji(cat_text + " " + title)

    title_zh = translate_ja_to_zh(title)

    return {
        "id": stable_id("kyo", event_id),
        "title_ja": title,
        "title_zh": title_zh,
        "category": cat,
        "kanji": kanji,
        "date_start": start,
        "date_end": end,
        "prefecture": "京都",
        "city": "",
        "venue": "",
        "description_ja": "",
        "description_zh": "",
        "url": f"https://ja.kyoto.travel/event/single.php?event_id={event_id}",
        "image_url": image,
        "featured": False,
        "source": "kyoto",
    }


def fetch_osaka():
    """OSAKA-INFO (osaka-info.jp) — Osaka official tourism.

    Hits internal /api_/orden/get_event_list.php endpoint which returns
    HTML fragments. ~40 events spread across 6 pages.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://osaka-info.jp/event/",
    }
    out = []
    seen = set()
    for page in range(1, 8):
        url = f"https://osaka-info.jp/api_/orden/get_event_list.php?page={page}"
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"  [osaka] page {page} {type(e).__name__}: {e}",
                  file=sys.stderr)
            break

        items = re.findall(r'<li[^>]*>\s*<a href="([^"]+)"(.*?)</a>\s*</li>',
                           r.text, re.DOTALL)
        added = 0
        for href, body in items:
            ev = parse_osaka_item(href, body)
            if ev and ev["id"] not in seen:
                seen.add(ev["id"])
                out.append(ev)
                added += 1
        print(f"  [osaka] page {page}: {added} events")
        if added == 0:
            break
        time.sleep(1.5)
    return out


def parse_osaka_item(href, body):
    if "event_detail.html" not in href and "/event/" not in href:
        return None
    title_m = re.search(r'class="name"[^>]*>([^<]+)<', body)
    if not title_m:
        return None
    title = title_m.group(1).strip()
    if not title:
        return None

    date_m = re.search(
        r'class="date"[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})',
        body,
    )
    if not date_m:
        # Try single-date variant
        sd_m = re.search(r'class="date"[^>]*>\s*(\d{4}-\d{2}-\d{2})', body)
        if not sd_m:
            return None
        start = end = sd_m.group(1)
    else:
        start, end = date_m.group(1), date_m.group(2)

    img_m = re.search(r'<img[^>]+src="([^"]+)"', body)
    image = img_m.group(1) if img_m else ""
    if "ogp_image" in image:
        image = ""

    area_m = re.search(r'class="area"[^>]*>([^<]+)<', body)
    area = area_m.group(1).strip() if area_m else ""

    cat, kanji = guess_cat_kanji(title + " " + area)
    title_zh = translate_ja_to_zh(title)

    return {
        "id": stable_id("osk", href),
        "title_ja": title,
        "title_zh": title_zh,
        "category": cat,
        "kanji": kanji,
        "date_start": start,
        "date_end": end,
        "prefecture": "大阪",
        "city": "",
        "venue": area,
        "description_ja": "",
        "description_zh": "",
        "url": href,
        "image_url": image,
        "featured": False,
        "source": "osaka",
    }


def fetch_okinawa():
    """OkinawaStory (okinawastory.jp) — Okinawa official tourism.

    Card list at /event/. Single fetch returns the visible page;
    pagination follows /event/page/N/ pattern.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
    }
    out = []
    seen = set()
    try:
        r = requests.get("https://www.okinawastory.jp/event/",
                         headers=headers, timeout=25)
        r.raise_for_status()
    except Exception as e:
        print(f"  [okinawa] {type(e).__name__}: {e}", file=sys.stderr)
        return []

    articles = re.findall(
        r'<article class="os-c-list-cmn[^"]*"[^>]*>(.*?)</article>',
        r.text, re.DOTALL,
    )
    for raw in articles:
        ev = parse_okinawa_item(raw)
        if ev and ev["id"] not in seen:
            seen.add(ev["id"])
            out.append(ev)
    print(f"  [okinawa] {len(out)} events")
    return out


_OKINAWA_DATE_RE = re.compile(
    r'(\d{4})年(\d{1,2})月(\d{1,2})日'
    r'(?:[^〜]*〜\s*(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日)?'
)


def parse_okinawa_item(html):
    id_m = re.search(r'href="/event/(\d+)"', html)
    if not id_m:
        return None
    event_id = id_m.group(1)

    title_m = re.search(
        r'class="os-c-list-cmn__title[^"]*"[^>]*>\s*<a[^>]*>([^<]+)</a>',
        html,
    )
    if not title_m:
        return None
    title = title_m.group(1).strip()
    if not title:
        return None

    date_m = re.search(
        r'class="os-c-list-cmn__lead[^"]*"[^>]*>\s*([^<]+)\s*<',
        html,
    )
    start = end = None
    if date_m:
        dm = _OKINAWA_DATE_RE.search(date_m.group(1))
        if dm:
            y1, m1, d1, y2s, m2s, d2s = dm.groups()
            start = f"{int(y1):04d}-{int(m1):02d}-{int(d1):02d}"
            if d2s:
                y2 = int(y2s) if y2s else int(y1)
                m2 = int(m2s) if m2s else int(m1)
                if not y2s and m2 < int(m1):
                    y2 += 1
                end = f"{y2:04d}-{m2:02d}-{int(d2s):02d}"
            else:
                end = start
    if not start:
        return None

    venue_m = re.search(
        r'class="os-c-list-cmn__disc[^"]*"[^>]*>\s*([^<]+)\s*<',
        html,
    )
    venue = venue_m.group(1).strip() if venue_m else ""

    img_m = re.search(r'data-src="(https?://[^"]+)"', html)
    if not img_m:
        img_m = re.search(r'<img[^>]+src="([^"]+\.(?:jpg|jpeg|png|webp))"', html)
    image = img_m.group(1) if img_m else ""
    if "noimage" in image:
        image = ""

    cat, kanji = guess_cat_kanji(title + " " + venue)
    title_zh = translate_ja_to_zh(title)

    return {
        "id": stable_id("okn", event_id),
        "title_ja": title,
        "title_zh": title_zh,
        "category": cat,
        "kanji": kanji,
        "date_start": start,
        "date_end": end,
        "prefecture": "沖縄",
        "city": "",
        "venue": venue,
        "description_ja": "",
        "description_zh": "",
        "url": f"https://www.okinawastory.jp/event/{event_id}",
        "image_url": image,
        "featured": False,
        "source": "okinawa",
    }


MUSEUM_DURATION_RE = re.compile(
    r'(\d{4})年(\d{1,2})月(\d{1,2})日[^〜]*〜'
    r'(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日'
)


def parse_museum_duration(s):
    """Parse '2026年8月21日（Fr）〜9月27日（Su）' style strings.

    End-year / end-month may be omitted when same as start. If end-month
    is given and is less than start-month with no explicit year, assume
    end-year = start-year + 1 (wraps over new year).
    """
    m = MUSEUM_DURATION_RE.search(s)
    if not m:
        return None, None
    y1s, m1s, d1s, y2s, m2s, d2s = m.groups()
    y1, m1, d1 = int(y1s), int(m1s), int(d1s)
    d2 = int(d2s)
    if y2s:
        y2 = int(y2s)
    else:
        y2 = y1
    if m2s:
        m2 = int(m2s)
        if not y2s and m2 < m1:
            y2 = y1 + 1
    else:
        m2 = m1
    return f"{y1:04d}-{m1:02d}-{d1:02d}", f"{y2:04d}-{m2:02d}-{d2:02d}"


def fetch_museum():
    """Internet Museum (museum.or.jp) — Japan's most authoritative
    museum/exhibition database. Nuxt SSR; events live in HTML cards.

    First page yields ~40 cards; paginated pages yield ~20 each. We pull
    the first 3 pages (≈80 unique exhibitions) with polite 1.5s delays.
    All entries are 展覧会.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
    }
    pages = ["https://www.museum.or.jp/event"] + [
        f"https://www.museum.or.jp/event?page={p}" for p in (2, 3)
    ]

    out = []
    for url in pages:
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"  [museum] {url} {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(1.5)
            continue

        # Split on the card-wrapper marker. The first segment is page
        # chrome (header / filters) so we drop it.
        chunks = re.split(r'<div class="c-eventItem"[ >]', r.text)[1:]
        added = 0
        for c in chunks:
            ev = parse_museum_card(c)
            if ev:
                out.append(ev)
                added += 1
        print(f"  [museum] {url.rsplit('/', 1)[-1] or 'event'}: {added} events")
        time.sleep(1.5)
    return out


def parse_museum_card(html):
    title_m = re.search(
        r'class="c-eventItem_museum[^"]*"[^>]*>\s*<a href="(/event/\d+)"[^>]*>([^<]+)</a>',
        html,
    )
    if not title_m:
        return None
    path = title_m.group(1)
    title = title_m.group(2).strip()
    if not title:
        return None

    venue_m = re.search(r'class="c-eventItem_event"[^>]*>([^<]+)<', html)
    venue, prefecture = "", "東京"
    if venue_m:
        parts = [p.strip() for p in venue_m.group(1).split("|")]
        venue = parts[0] if parts else ""
        if len(parts) > 1:
            pref_raw = (parts[1]
                        .replace("都", "").replace("府", "").replace("県", "")
                        .strip())
            if pref_raw in PREF_KEYWORDS.values():
                prefecture = pref_raw
            else:
                prefecture = guess_prefecture(parts[1] + " " + venue) or "東京"

    dur_m = re.search(r'class="c-eventItem_duration"[^>]*>([^<]+)<', html)
    if not dur_m:
        return None
    start, end = parse_museum_duration(dur_m.group(1))
    if not start:
        return None

    img_m = re.search(r'<img[^>]+src="([^"]+)"', html)
    image = img_m.group(1) if img_m else ""

    event_url = f"https://www.museum.or.jp{path}"
    title_zh = translate_ja_to_zh(title)

    return {
        "id": stable_id("mu", path),
        "title_ja": title,
        "title_zh": title_zh,
        "category": "展覧会",
        "kanji": "展",
        "date_start": start,
        "date_end": end,
        "prefecture": prefecture,
        "city": "",
        "venue": venue,
        "description_ja": "",
        "description_zh": "",
        "url": event_url,
        "image_url": image,
        "featured": False,
        "source": "museum",
    }


def fetch_tokyoartbeat():
    """Tokyo Art Beat — JP exhibitions / art events database.

    Server-renders ~1000 events as JSON embedded in Next.js __NEXT_DATA__
    on /events. Single fetch, no API key. All entries are art exhibitions,
    so category is fixed to 展覧会.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
    }
    url = "https://www.tokyoartbeat.com/events"
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  [tokyoartbeat] {type(e).__name__}: {e}", file=sys.stderr)
        return []

    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r.text, re.DOTALL,
    )
    if not m:
        print("  [tokyoartbeat] __NEXT_DATA__ not found", file=sys.stderr)
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"  [tokyoartbeat] JSON parse: {e}", file=sys.stderr)
        return []

    fallback = (data.get("props", {})
                    .get("pageProps", {})
                    .get("fallback", {}))
    items = []
    for key, val in fallback.items():
        if (isinstance(val, dict)
                and "EventSearch" in key
                and isinstance(val.get("data"), list)):
            items = val["data"]
            break
    if not items:
        print("  [tokyoartbeat] EventSearch data not found", file=sys.stderr)
        return []

    out = []
    for item in items:
        ev = parse_tokyoartbeat_event(item)
        if ev:
            out.append(ev)
    print(f"  [tokyoartbeat] {len(out)} events")
    return out


def parse_tokyoartbeat_event(d):
    name = (d.get("eventName") or "").strip()
    start = (d.get("scheduleStartsOn") or "")[:10]
    end = (d.get("scheduleEndsOn") or start)[:10]
    if not name or not start:
        return None

    venue_fields = (d.get("venue") or {}).get("fields") or {}
    venue = (venue_fields.get("fullName") or "").strip()
    area = ((venue_fields.get("localArea") or {})
            .get("fields", {})
            .get("name", ""))
    prefecture = area.replace("都", "").replace("府", "").replace("県", "").strip()
    if prefecture not in PREF_KEYWORDS.values():
        prefecture = guess_prefecture(f"{area} {venue} {name}") or "東京"

    img_file = (((d.get("imageposter") or {})
                 .get("fields") or {})
                .get("file") or {})
    image = img_file.get("url") or ""
    if image.startswith("//"):
        image = "https:" + image

    slug = d.get("slug") or ""
    event_url = f"https://www.tokyoartbeat.com/events/{slug}" if slug else ""

    title_zh = translate_ja_to_zh(name)

    return {
        "id": stable_id("tab", d.get("id") or slug or name),
        "title_ja": name,
        "title_zh": title_zh,
        "category": "展覧会",
        "kanji": "展",
        "date_start": start,
        "date_end": end,
        "prefecture": prefecture,
        "city": "",
        "venue": venue,
        "description_ja": "",
        "description_zh": "",
        "url": event_url,
        "image_url": image,
        "featured": False,
        "source": "tokyoartbeat",
    }


# Add new sources here. Each is (name, fetch_function).
SOURCES = [
    ("connpass", fetch_connpass),
    ("walkerplus", fetch_walkerplus),
    ("walkerplus_genre", fetch_walkerplus_genre),
    ("bandsintown", fetch_bandsintown),
    ("ticketmaster", fetch_ticketmaster),
    ("pia", fetch_pia),
    ("tokyo_cheapo", fetch_tokyo_cheapo),
    ("tokyoartbeat", fetch_tokyoartbeat),
    ("museum", fetch_museum),
    ("tiget", fetch_tiget),
    ("npb", fetch_npb),
    ("gotokyo", fetch_gotokyo),
    ("kyoto", fetch_kyoto),
    ("osaka", fetch_osaka),
    ("okinawa", fetch_okinawa),
    ("confetti", fetch_confetti),
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

    global _TRANS_LAZY
    _TRANS_LAZY = True
    scraped = []
    for name, fn in SOURCES:
        try:
            events = fn()
            print(f"  ✓ {name}: {len(events)} events total")
            scraped.extend(events)
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}", file=sys.stderr)
    _TRANS_LAZY = False

    _resolve_translations()
    _backfill_translations(scraped)

    scraped = filter_future(scraped)
    seen, deduped = set(), []
    for ev in scraped:
        if ev["id"] in seen or ev["id"] in curated_ids:
            continue
        seen.add(ev["id"])
        deduped.append(ev)

    # Cross-source fuzzy dedup: same prefecture + date overlap + similar title
    final = merge_dedup(curated_events + deduped)
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
