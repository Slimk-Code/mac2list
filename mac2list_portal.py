#!/usr/bin/env python3
"""
mac2list v1.2
State-machine filesystem: each step gets its own JSON file.
Sessions are fully isolated under data/cache/<session_id>/.
Consolidated resume file lives in data/session/.
"""
import requests
import json
import re
import os
import math
import time
import sys
import subprocess
import threading
import glob
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# PATH CONSTANTS
# ============================================================
DATA_DIR    = "data"
SESSION_DIR = "data/session"
CACHE_DIR   = "data/cache"
OUTPUT_DIR  = "data/output"


def make_session_id(base_url, mac):
    """Create a filesystem-safe session identifier from portal URL + MAC."""
    clean = base_url.rstrip("/").replace("https://", "").replace("http://", "")
    safe_portal = re.sub(r"[^a-zA-Z0-9.\-]", "_", clean)
    safe_mac = mac.upper().replace(":", "")
    return f"{safe_portal}_{safe_mac}"

# ============================================================
# SECTIONS & STEPS DEFINITION
# ============================================================
SECTIONS = {
    "Auth": {
        "title": "Auth, Profile & Account",
        "items": [
            ("A1", "type=stb&action=handshake", "Auth token", False),
            ("A2", "type=stb&action=get_profile", "STB profile (mac,sn)", True),
            ("B1", "type=account_info&action=get_main_info", "Phone,status,connections", True),
            ("B2", "type=account_info&action=get_info", "Full account details", True),
            ("B3", "type=account_info&action=get_tariff_plans", "Subscription plans", True),
        ]
    },
        "Live Channels": {
        "title": "Live Channels",
        "items": [
            ("C2", "type=itv&action=get_genres", "Channel categories", True),
            ("C5", "type=itv&action=get_ordered_list", "Channels by genre (all pages)", False),
            ("C4", "type=itv&action=create_link", "Resolve live stream URL", False),
        ]
    },
    "VOD Movies": {
        "title": "VOD Movies",
        "items": [
            ("D1", "type=vod&action=get_categories", "VOD categories", True),
            ("D4", "type=vod&action=get_ordered_list", "VOD by category (all pages)", False),
            ("D3", "type=vod&action=create_link", "Resolve VOD stream URL", False),
        ]
    },
    "Series": {
        "title": "Series",
        "items": [
            ("E1", "type=series&action=get_categories", "Series categories", True),
            ("E5", "type=series&action=get_ordered_list", "Series by category (all pages)", False),
            ("E3", "type=series&action=get_ordered_list&movie_id=...", "Episodes list", False),
            ("E4", "type=vod&action=create_link&series=N", "Resolve episode stream URL", False),
        ]
    },
    "Settings": {
        "title": "Settings & Unlock",
        "items": [
            ("F1", "type=settings&action=get", "Portal settings", True),
            ("F2", "type=settings&action=get_parental_lock", "Parental lock status", True),
            ("F3", "type=itv&action=set_parental_lock", "Unlock adult (tests 0000,1234,3333)", False),
        ]
    },
    "Convert": {
        "title": "Convert / Status",
        "items": [
            ("G1", "generate_m3u", "Generate M3U playlists", False),
        ]
    },
}

# ============================================================
# HUB FLOW CONSTANTS
# ============================================================
SCRAPE_SECTION_KEYS = ["Live Channels", "VOD Movies", "Series"]
SETTINGS_SECTION_KEYS = ["Settings"]

SETTINGS_STEP_CODES = []
for sec_key in SETTINGS_SECTION_KEYS:
    for code, _, _, _ in SECTIONS[sec_key]["items"]:
        SETTINGS_STEP_CODES.append(code)


STEP_PARAMS = {
    "A2": {"type": "stb", "action": "get_profile", "JsHttpRequest": "1-xml"},
    "B1": {"type": "account_info", "action": "get_main_info", "JsHttpRequest": "1-xml"},
    "B2": {"type": "account_info", "action": "get_info", "JsHttpRequest": "1-xml"},
    "B3": {"type": "account_info", "action": "get_tariff_plans", "JsHttpRequest": "1-xml"},
    "C2": {"type": "itv", "action": "get_genres", "JsHttpRequest": "1-xml"},
    "D1": {"type": "vod", "action": "get_categories", "JsHttpRequest": "1-xml"},
    "E1": {"type": "series", "action": "get_categories", "JsHttpRequest": "1-xml"},
    "F1": {"type": "settings", "action": "get", "JsHttpRequest": "1-xml"},
    "F2": {"type": "settings", "action": "get_parental_lock", "JsHttpRequest": "1-xml"},
}

FLAT_STEPS = []
for sec_key, sec in SECTIONS.items():
    for code, desc, info, is_auto in sec["items"]:
        FLAT_STEPS.append((sec_key, code, desc, info, is_auto))

# ============================================================
# STEP → FILE MAP  (relative to cache_root)
# ============================================================
STEP_FILE_MAP = {
    "A1": "01_auth/01_handshake.json",
    "A2": "01_auth/02_profile.json",
    "B1": "01_auth/03_account_main.json",
    "B2": "01_auth/04_account_full.json",
    "B3": "01_auth/05_tariff.json",
    "C2": "02_live/01_categories.json",
    "C5": "02_live/02_channels.json",
    "C4": "02_live/03_resolve.json",
    "D1": "03_vod/01_categories.json",
    "D4": "03_vod/02_movies.json",
    "D3": "03_vod/03_resolve.json",
    "E1": "04_series/01_categories.json",
    "E5": "04_series/02_items.json",
    "E3": "04_series/03_episodes.json",
    "E4": "04_series/04_resolve.json",
    "F1": "05_settings/01_portal_settings.json",
    "F2": "05_settings/02_parental_lock.json",
    "F3": "05_settings/03_unlock.json",
    "G1": "06_convert/01_generate.json",
}

class Mac2ListPortal:
    def __init__(self, base_url, mac_address):
        self.base_url = base_url.rstrip("/")
        self.mac = mac_address.upper().strip()
        self.session = requests.Session()
        self.token = None
        self.locked_url = f"{self.base_url}/portal.php"
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 4 rev: 1812 Safari/533.3",
            "X-User-Agent": "Model: MAG250; Link: Ethernet",
            "Referer": f"{self.base_url}/c/",
            "Cookie": f"mac={self.mac}; stb_lang=en; timezone=Europe%2FLondon",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _get(self, params):
        url = self.locked_url
        try:
            resp = self.session.get(url, params=params, timeout=15, allow_redirects=True)
            status = resp.status_code
            text = resp.text
            try:
                data = resp.json()
                return {"_data": data, "_status": status, "_url": url, "_text": "", "_error": None, "_lockedpath": None}
            except json.JSONDecodeError:
                pass
            try:
                if text.strip().startswith("{"):
                    data = json.loads(text)
                    return {"_data": data, "_status": status, "_url": url, "_text": "", "_error": None, "_lockedpath": None}
                elif "js" in text:
                    start = text.find("{")
                    end = text.rfind("}") + 1
                    if start >= 0 and end > start:
                        data = json.loads(text[start:end])
                        return {"_data": data, "_status": status, "_url": url, "_text": "", "_error": None, "_lockedpath": None}
            except:
                pass
            return {
                "_data": None, "_status": status, "_url": url,
                "_text": text[:200], "_error": None,
                "_lockedpath": [
                    {"url": url, "status": status,
                     "result": "HTTP OK but no parseable JSON",
                     "preview": text[:150]}
                ]
            }
        except requests.exceptions.RequestException as e:
            return {
                "_data": None, "_status": None, "_url": url,
                "_text": "", "_error": str(e),
                "_lockedpath": [
                    {"url": url, "status": None,
                     "result": f"Connection failed: {str(e)[:100]}"}
                ]
            }

    def handshake(self):
        params = {"type": "stb", "action": "handshake", "JsHttpRequest": "1-xml"}
        result = self._get(params)
        data = result.get("_data")
        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                token = js.get("token")
                if token:
                    self.token = token
                    self.session.headers["Authorization"] = f"Bearer {token}"
        return result

    def fetch(self, params):
        return self._get(params)

    def _fetch_single_page(self, p, params_template, delay, pages, all_data, fetched_items, done_count, failed_pages, lock):
        p_params = dict(params_template)
        p_params["p"] = str(p)
        for attempt in range(3):
            page_result = self._get(p_params)
            page_data = page_result.get("_data", {})
            if page_data and isinstance(page_data, dict):
                items = page_data.get("js", {}).get("data", [])
                if items:
                    with lock:
                        all_data.extend(items)
                        done_count[0] += 1
                        fetched_items[0] += len(items)
                    return True
            if attempt < 2:
                time.sleep(delay * 2)
        with lock:
            done_count[0] += 1
            failed_pages.append(p)
        return False

    def fetch_all_pages(self, params_template, delay=0.5):
        result = self._get(params_template)
        data = result.get("_data")
        if not data or not isinstance(data, dict):
            return result
        js = data.get("js", {})
        if not isinstance(js, dict):
            return result
        total = int(js.get("total_items") or 0)
        per_page = int(js.get("max_page_items") or (len(js.get("data", [])) or 1))
        pages = math.ceil(total / per_page) if per_page else 1
        if pages <= 1:
            return result
        all_data = js.get("data", [])
        failed_pages = []
        done_count = [0]
        fetched_items = [len(all_data)]
        lock = threading.Lock()

        def _fetch_page_thread(p):
            success = self._fetch_single_page(p, params_template, delay, pages, all_data, fetched_items, done_count, failed_pages, lock)

        # Failures are collected silently; retry progress shown separately

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_fetch_page_thread, p) for p in range(2, pages + 1)]
            for future in as_completed(futures):
                future.result()

        recovered_pages = []
        still_failed = failed_pages  # Retry deferred to caller

        merged = dict(data)
        merged_js = dict(js)
        merged_js["data"] = all_data
        merged_js["_fetched_pages"] = pages
        merged_js["_total_items_expected"] = total
        merged_js["_total_items_fetched"] = len(all_data)
        missing = total - len(all_data)
        merged_js["_missing_items"] = missing if missing > 0 else 0
        merged_js["_failed_pages"] = failed_pages
        merged_js["_failed_pages_count"] = len(failed_pages)
        merged_js["_recovered_pages"] = recovered_pages
        merged_js["_recovered_pages_count"] = len(recovered_pages)
        if total > 0:
            rate = (len(all_data) / total) * 100
        else:
            rate = 100.0
        merged_js["_success_rate"] = "{:.1f}%".format(rate)
        if failed_pages:
            merged_js["_status"] = "INCOMPLETE — some pages failed even after retry"
            if recovered_pages:
                merged_js["_note"] = "Expected {} items but only got {}. {} items missing across {} failed pages ({} recovered on retry).".format(total, len(all_data), missing, len(failed_pages), len(recovered_pages))
            else:
                merged_js["_note"] = "Expected {} items but only got {}. {} items missing across {} failed pages (none recovered).".format(total, len(all_data), missing, len(failed_pages))
        else:
            merged_js["_status"] = "COMPLETE"
            if recovered_pages:
                merged_js["_note"] = "All {} items fetched successfully across {} pages ({} recovered on retry).".format(total, pages, len(recovered_pages))
            else:
                merged_js["_note"] = "All {} items fetched successfully across {} pages.".format(total, pages)
        merged["js"] = merged_js
        return {
            "_data": merged, "_status": result.get("_status"),
            "_url": result.get("_url"), "_text": "",
            "_error": None, "_lockedpath": None
        }

# ============================================================
# TRIMMING HELPERS
# ============================================================
def trim_channel(item):
    return {
        "id": item.get("id", ""),
        "name": item.get("name", item.get("title", "")),
        "cmd": item.get("cmd", ""),
        "logo": item.get("logo", ""),
    }

def trim_movie(item):
    return {
        "id": item.get("id", ""),
        "name": item.get("name", item.get("title", "")),
        "cmd": item.get("cmd", ""),
        "logo": item.get("logo", ""),
    }

def trim_series_item(item):
    return {
        "id": item.get("id", ""),
        "name": item.get("name", item.get("title", "")),
        "censored": item.get("censored", 0),
        "series": item.get("series", []),
        "cmd": item.get("cmd", ""),
    }

# ============================================================
# CACHE MANAGER  —  state-machine filesystem
# ============================================================
class CacheManager:
    """Manages per-session state-machine directory tree.

    Layout:
        data/
        ├── session/<session_id>.json          ← consolidated resume file
        └── cache/<session_id>/
            ├── 01_auth/
            │   ├── 01_handshake.json          ← {"_status": "pending"} initially
            │   └── ...
            ├── 02_live/
            ├── 03_vod/
            ├── 04_series/
            ├── 05_settings/
            ├── 06_convert/
            └── errors/
    """

    # Subdirectories to create under cache_root
    _SUBDIRS = [
        "01_auth",
        "02_live",
        "03_vod",
        "04_series",
        "05_settings",
        "06_convert",
        "errors",
    ]

    def __init__(self, session_id):
        self.session_id = session_id
        self.cache_root = os.path.join(CACHE_DIR, session_id)
        self.session_file = os.path.join(SESSION_DIR, f"{session_id}.json")

    # ----------------------------------------------------------
    # Scaffold
    # ----------------------------------------------------------
    def scaffold(self):
        """Create all directories and initialise missing step files to pending."""
        os.makedirs(SESSION_DIR, exist_ok=True)
        os.makedirs(self.cache_root, exist_ok=True)
        for sub in self._SUBDIRS:
            os.makedirs(os.path.join(self.cache_root, sub), exist_ok=True)
        # Initialise every step file if not present
        for code, rel_path in STEP_FILE_MAP.items():
            full = os.path.join(self.cache_root, rel_path)
            if not os.path.exists(full):
                with open(full, "w", encoding="utf-8") as f:
                    json.dump({"_status": "pending"}, f, indent=2)

    # ----------------------------------------------------------
    # Step paths
    # ----------------------------------------------------------
    def step_path(self, code):
        """Absolute path to the step file for *code*."""
        rel = STEP_FILE_MAP.get(code)
        if not rel:
            return None
        return os.path.join(self.cache_root, rel)

    def error_path(self, code, label):
        """Absolute path for an error file inside errors/."""
        return os.path.join(self.cache_root, "errors", f"{code}_{label}_ERROR.json")

    # ----------------------------------------------------------
    # Read / write individual step files
    # ----------------------------------------------------------
    def load_step(self, code):
        """Return the parsed JSON dict for *code*, or {} if missing."""
        path = self.step_path(code)
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def write_step(self, code, data):
        """Write *data* dict to the step file.  Sets _status if not already present."""
        path = self.step_path(code)
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def write_error(self, code, label, data):
        """Write an error dict to errors/<code>_<label>_ERROR.json."""
        path = self.error_path(code, label)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return path

    # ----------------------------------------------------------
    # Step status helpers
    # ----------------------------------------------------------
    def step_status(self, code):
        """Return _status string from the step file ('pending', 'done', 'error', 'ignored')."""
        return self.load_step(code).get("_status", "pending")

    def is_done(self, code):
        return self.step_status(code) == "done"

    def is_ignored(self, code):
        return self.step_status(code) == "ignored"

    def mark_done(self, code):
        data = self.load_step(code)
        data["_status"] = "done"
        self.write_step(code, data)

    def mark_ignored(self, code):
        data = self.load_step(code)
        data["_status"] = "ignored"
        self.write_step(code, data)

    # ----------------------------------------------------------
    # Consolidated session file
    # ----------------------------------------------------------
    def save_session(self, data):
        """Write the consolidated JSON to data/session/<session_id>.json."""
        os.makedirs(SESSION_DIR, exist_ok=True)
        with open(self.session_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def load_session(self):
        """Return the consolidated session dict, or None if not found."""
        if os.path.exists(self.session_file):
            try:
                with open(self.session_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    # ----------------------------------------------------------
    # Merge all step files into the consolidated structure
    # ----------------------------------------------------------
    def merge_all(self):
        """Build and return the consolidated data dict from all step files."""
        session = self.load_session()
        if session:
            # Backfill scraped_at for old sessions
            meta = session.get("_meta", {})
            cat_codes = ["C2", "D1", "E1"]
            if not meta.get("scraped_at") and all(c in meta.get("done_steps", []) for c in cat_codes):
                meta["scraped_at"] = meta.get("created", "")
                session["_meta"] = meta
            return session
        # Build skeleton from scratch
        return {
            "_meta": {
                "created": datetime.now().isoformat(),
                "portal": "",
                "mac": "",
                "last_step": "",
                "scraped_at": "",
                "ignored_steps": [],
                "done_steps": []
            },
            "profile": {},
            "account": {},
            "live": {"total_channels": 0, "grand_total": 0, "categories": []},
            "movies": {"total_items": 0, "grand_total": 0, "categories": []},
            "series": {"total_items": 0, "grand_total": 0, "categories": []}
        }


# ============================================================
# JSON MANAGER  (delegates file I/O to CacheManager)
# ============================================================
class JSONManager:
    def __init__(self, base_url, mac):
        session_id = make_session_id(base_url, mac)
        self.cache = CacheManager(session_id)
        self.cache.scaffold()
        # session file path exposed for Convert step display
        self.filename = self.cache.session_file
        self.data = self.cache.merge_all()
        self._ensure_tracking()

    # _load() removed — CacheManager.merge_all() handles initial data load

    def _ensure_tracking(self):
        for section, key in [
            ("live", "_remaining_genres"),
            ("live", "_fetched_genres"),
            ("live", "_failed_genres"),
            ("live", "_probed_genres"),
            ("movies", "_remaining_categories"),
            ("movies", "_fetched_categories"),
            ("movies", "_failed_categories"),
            ("movies", "_probed_categories"),
            ("series", "_remaining_categories"),
            ("series", "_fetched_categories"),
            ("series", "_failed_categories"),
            ("series", "_probed_categories"),
        ]:
            if key not in self.data[section]:
                self.data[section][key] = []

    def save(self):
        """Persist consolidated data to data/session/<session_id>.json."""
        self.cache.save_session(self.data)
        return self.filename

    def set_meta(self, portal, mac):
        self.data["_meta"]["portal"] = portal
        self.data["_meta"]["mac"] = mac
        self.save()

    def update_last_step(self, step_code):
        self.data["_meta"]["last_step"] = step_code
        self.save()

    def mark_done(self, step_code):
        done = self.data["_meta"].get("done_steps", [])
        if step_code not in done:
            done.append(step_code)
            self.data["_meta"]["done_steps"] = done
            self.update_last_step(step_code)
            self.save()
            # Set scraped_at only when a category step completes and all 3 are now done
            cat_codes = ["C2", "D1", "E1"]
            if step_code in cat_codes and all(c in done for c in cat_codes):
                self.data["_meta"]["scraped_at"] = datetime.now().isoformat()
                self.save()
        # Mirror status into the individual step file
        self.cache.mark_done(step_code)

    def mark_ignored(self, step_code):
        ignored = self.data["_meta"].get("ignored_steps", [])
        if step_code not in ignored:
            ignored.append(step_code)
        self.data["_meta"]["ignored_steps"] = ignored
        self.update_last_step(step_code)
        # Mirror status into the individual step file
        self.cache.mark_ignored(step_code)

    def is_done(self, step_code):
        return step_code in self.data["_meta"].get("done_steps", [])

    def is_ignored(self, step_code):
        return step_code in self.data["_meta"].get("ignored_steps", [])

    def get_resume_index(self):
        last = self.data["_meta"].get("last_step", "")
        if not last:
            return 0
        for i, (_, code, _, _, _) in enumerate(FLAT_STEPS):
            if code == last:
                return i + 1
        return 0

    def update_profile(self, profile_data):
        self.data["profile"] = profile_data
        self.save()

    def update_account(self, account_data):
        self.data["account"] = account_data
        self.save()

    def update_live_categories(self, categories):
        existing = {str(c.get("id")): c for c in self.data["live"].get("categories", [])}
        merged = []
        for cat in categories:
            cat_id = str(cat.get("id"))
            if cat_id in existing and "channels" in existing[cat_id]:
                merged_cat = dict(cat)
                merged_cat["channels"] = existing[cat_id]["channels"]
                if existing[cat_id].get("total_items", 0) > 0:
                    merged_cat["total_items"] = existing[cat_id]["total_items"]
                merged.append(merged_cat)
            else:
                merged.append(dict(cat))
        self.data["live"]["categories"] = merged
        all_ids = [str(c.get("id")) for c in merged if str(c.get("id")) != "*"]
        fetched = set(self.data["live"].get("_fetched_genres", []))
        failed = set(self.data["live"].get("_failed_genres", []))
        self.data["live"]["_remaining_genres"] = [cid for cid in all_ids if cid not in fetched and cid not in failed]
        self.save()

    def update_live_channels(self, genre_id, channels, total_items):
        trimmed = [trim_channel(ch) for ch in channels]
        cats = self.data["live"]["categories"]
        found = False
        for cat in cats:
            if str(cat.get("id")) == str(genre_id):
                cat["channels"] = trimmed
                cat["total_items"] = total_items
                found = True
                break
        if not found:
            cats.append({"id": str(genre_id), "title": "Unknown", "censored": 0, "total_items": total_items, "channels": trimmed})
        self.data["live"]["total_channels"] = sum(c.get("total_items", 0) for c in cats)
        gid = str(genre_id)
        if gid in self.data["live"].get("_remaining_genres", []):
            self.data["live"]["_remaining_genres"].remove(gid)
        if gid not in self.data["live"].get("_fetched_genres", []):
            self.data["live"].setdefault("_fetched_genres", []).append(gid)
        self.save()

    def set_live_grand_total(self, total):
        self.data["live"]["grand_total"] = total
        self.save()

    def mark_live_genre_probed(self, genre_id):
        gid = str(genre_id)
        if gid not in self.data["live"].get("_probed_genres", []):
            self.data["live"].setdefault("_probed_genres", []).append(gid)
        self.save()

    def mark_live_genre_failed(self, genre_id):
        gid = str(genre_id)
        if gid in self.data["live"].get("_remaining_genres", []):
            self.data["live"]["_remaining_genres"].remove(gid)
        if gid not in self.data["live"].get("_failed_genres", []):
            self.data["live"].setdefault("_failed_genres", []).append(gid)
        self.save()

    def update_movie_categories(self, categories):
        existing = {str(c.get("id")): c for c in self.data["movies"].get("categories", [])}
        merged = []
        for cat in categories:
            cat_id = str(cat.get("id"))
            if cat_id in existing and "items" in existing[cat_id]:
                merged_cat = dict(cat)
                merged_cat["items"] = existing[cat_id]["items"]
                if existing[cat_id].get("total_items", 0) > 0:
                    merged_cat["total_items"] = existing[cat_id]["total_items"]
                merged.append(merged_cat)
            else:
                merged.append(dict(cat))
        self.data["movies"]["categories"] = merged
        all_ids = [str(c.get("id")) for c in merged if str(c.get("id")) != "*"]
        fetched = set(self.data["movies"].get("_fetched_categories", []))
        failed = set(self.data["movies"].get("_failed_categories", []))
        self.data["movies"]["_remaining_categories"] = [cid for cid in all_ids if cid not in fetched and cid not in failed]
        self.save()

    def update_movie_items(self, category_id, items, total_items):
        trimmed = [trim_movie(m) for m in items]
        cats = self.data["movies"]["categories"]
        found = False
        for cat in cats:
            if str(cat.get("id")) == str(category_id):
                cat["items"] = trimmed
                cat["total_items"] = total_items
                found = True
                break
        if not found:
            cats.append({"id": str(category_id), "title": "Unknown", "censored": 0, "total_items": total_items, "items": trimmed})
        self.data["movies"]["total_items"] = sum(c.get("total_items", 0) for c in cats)
        cid = str(category_id)
        if cid in self.data["movies"].get("_remaining_categories", []):
            self.data["movies"]["_remaining_categories"].remove(cid)
        if cid not in self.data["movies"].get("_fetched_categories", []):
            self.data["movies"].setdefault("_fetched_categories", []).append(cid)
        self.save()

    def set_movie_grand_total(self, total):
        self.data["movies"]["grand_total"] = total
        self.save()

    def mark_movie_category_probed(self, category_id):
        cid = str(category_id)
        if cid not in self.data["movies"].get("_probed_categories", []):
            self.data["movies"].setdefault("_probed_categories", []).append(cid)
        self.save()

    def mark_movie_category_failed(self, category_id):
        cid = str(category_id)
        if cid in self.data["movies"].get("_remaining_categories", []):
            self.data["movies"]["_remaining_categories"].remove(cid)
        if cid not in self.data["movies"].get("_failed_categories", []):
            self.data["movies"].setdefault("_failed_categories", []).append(cid)
        self.save()

    def update_series_categories(self, categories):
        existing = {str(c.get("id")): c for c in self.data["series"].get("categories", [])}
        merged = []
        for cat in categories:
            cat_id = str(cat.get("id"))
            if cat_id in existing and "items" in existing[cat_id]:
                merged_cat = dict(cat)
                merged_cat["items"] = existing[cat_id]["items"]
                if existing[cat_id].get("total_items", 0) > 0:
                    merged_cat["total_items"] = existing[cat_id]["total_items"]
                merged.append(merged_cat)
            else:
                merged.append(dict(cat))
        self.data["series"]["categories"] = merged
        all_ids = [str(c.get("id")) for c in merged if str(c.get("id")) != "*"]
        fetched = set(self.data["series"].get("_fetched_categories", []))
        failed = set(self.data["series"].get("_failed_categories", []))
        self.data["series"]["_remaining_categories"] = [cid for cid in all_ids if cid not in fetched and cid not in failed]
        self.save()

    def update_series_items(self, category_id, items, total_items):
        trimmed = [trim_series_item(s) for s in items]
        cats = self.data["series"]["categories"]
        found = False
        for cat in cats:
            if str(cat.get("id")) == str(category_id):
                cat["items"] = trimmed
                cat["total_items"] = total_items
                found = True
                break
        if not found:
            cats.append({"id": str(category_id), "title": "Unknown", "censored": 0, "total_items": total_items, "items": trimmed})
        self.data["series"]["total_items"] = sum(c.get("total_items", 0) for c in cats)
        cid = str(category_id)
        if cid in self.data["series"].get("_remaining_categories", []):
            self.data["series"]["_remaining_categories"].remove(cid)
        if cid not in self.data["series"].get("_fetched_categories", []):
            self.data["series"].setdefault("_fetched_categories", []).append(cid)
        self.save()

    def set_series_grand_total(self, total):
        self.data["series"]["grand_total"] = total
        self.save()

    def mark_series_category_probed(self, category_id):
        cid = str(category_id)
        if cid not in self.data["series"].get("_probed_categories", []):
            self.data["series"].setdefault("_probed_categories", []).append(cid)
        self.save()

    def mark_series_category_failed(self, category_id):
        cid = str(category_id)
        if cid in self.data["series"].get("_remaining_categories", []):
            self.data["series"]["_remaining_categories"].remove(cid)
        if cid not in self.data["series"].get("_failed_categories", []):
            self.data["series"].setdefault("_failed_categories", []).append(cid)
        self.save()

    def update_series_episodes(self, series_id, episodes_data):
        cats = self.data["series"]["categories"]
        for cat in cats:
            for item in cat.get("items", []):
                if str(item.get("id")) == str(series_id):
                    item["seasons"] = episodes_data
                    self.save()
                    return True
        return False

    def get_live_remaining(self):
        return self.data["live"].get("_remaining_genres", [])

    def get_live_fetched(self):
        return self.data["live"].get("_fetched_genres", [])

    def get_live_failed(self):
        return self.data["live"].get("_failed_genres", [])

    def get_movie_remaining(self):
        return self.data["movies"].get("_remaining_categories", [])

    def get_movie_fetched(self):
        return self.data["movies"].get("_fetched_categories", [])

    def get_movie_failed(self):
        return self.data["movies"].get("_failed_categories", [])

    def get_series_remaining(self):
        return self.data["series"].get("_remaining_categories", [])

    def get_series_fetched(self):
        return self.data["series"].get("_fetched_categories", [])

    def get_series_failed(self):
        return self.data["series"].get("_failed_categories", [])

# ============================================================
# UTILITY FUNCTIONS
# ============================================================
def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def _time_ago(iso_str):
    if not iso_str:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(iso_str)
        delta = datetime.now() - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return "just now"
        mins = secs // 60
        if mins < 60:
            return "{} min ago".format(mins)
        hours = mins // 60
        if hours < 24:
            return "{} hours ago".format(hours)
        days = hours // 24
        return "{} days ago".format(days)
    except Exception:
        return "Unknown"

def _cooldown(seconds=5):
    for i in range(seconds, 0, -1):
        sys.stdout.write("\r  Continuing in {}s...  ".format(i))
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r" + " " * 40 + "\r")
    sys.stdout.flush()

def progress_bar(current, total, prefix="", width=30):
    if total <= 0:
        pct = 100.0
        filled = width
    else:
        pct = (current / total) * 100
        filled = int(width * current / total)
    bar = "=" * filled + "-" * (width - filled)
    line = "{}[{}] {:5.1f}% ({}/{})".format(prefix, bar, pct, current, total)
    sys.stdout.write(chr(13) + line.ljust(80))
    sys.stdout.flush()
    if current >= total:
        print()

def save_json(data, code, action_name, cache=None):
    """Write raw step data. If cache is provided, writes to the step file path;
    otherwise falls back to data/<code>_<action_name>.json."""
    if not data:
        return None
    if cache:
        path = cache.step_path(code)
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Merge _status:done into stored data
            stored = dict(data)
            stored.setdefault("_status", "done")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(stored, f, indent=2, ensure_ascii=False)
            return path
    # Fallback: write to data/ root
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = os.path.join(DATA_DIR, f"{code}_{action_name}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return filename

def save_error_json(code, action_name, status, url, error_msg, lockedpath, cache=None):
    if lockedpath:
        reason = "Locked path returned empty/malformed data. See _lockedpath for details."
        error_data = {"_status": "error", "_error": True, "_timestamp": datetime.now().isoformat(), "_action": action_name, "_reason": reason, "_lockedpath": lockedpath}
    elif status == 200:
        reason = "HTTP 200 OK from {} — portal connected but returned empty/malformed data.".format(url)
        error_data = {"_status": "error", "_error": True, "_timestamp": datetime.now().isoformat(), "_action": action_name, "_reason": reason, "_url": url}
    elif status is not None:
        reason = "HTTP {} from {} — request failed. Portal rejected the call.".format(status, url)
        error_data = {"_status": "error", "_error": True, "_timestamp": datetime.now().isoformat(), "_action": action_name, "_reason": reason, "_url": url}
    else:
        reason = "Connection failed — could not reach endpoint. Error: {}.".format(error_msg)
        error_data = {"_status": "error", "_error": True, "_timestamp": datetime.now().isoformat(), "_action": action_name, "_reason": reason, "_url": url}
    if cache:
        # Write the error into errors/ dir AND mark the step file as error
        err_path = cache.write_error(code, action_name, error_data)
        step_path = cache.step_path(code)
        if step_path:
            with open(step_path, "w", encoding="utf-8") as f:
                json.dump({"_status": "error", "_error_file": os.path.basename(err_path)}, f, indent=2)
        return err_path
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = os.path.join(DATA_DIR, f"{code}_{action_name}_ERROR.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(error_data, f, indent=2, ensure_ascii=False)
    return filename

def handle_fetch_result(result, code, safe_name, cache=None):
    data = result.get("_data")
    status = result.get("_status")
    url = result.get("_url")
    error_msg = result.get("_error")
    lockedpath = result.get("_lockedpath")
    if data:
        fname = save_json(data, code, safe_name, cache=cache)
        return fname, "ok", False, False
    else:
        fname = save_error_json(code, safe_name, status, url, error_msg, lockedpath, cache=cache)
        is_200 = (status == 200) or (lockedpath and lockedpath[0].get("status") == 200)
        return fname, "error", True, is_200

# ============================================================
# PROBING
# ============================================================
def probe_categories(client, json_mgr, section):
    if section == "live":
        cats = json_mgr.data["live"].get("categories", [])
        action_type = "itv"
        action = "get_ordered_list"
        id_key = "genre"
        mark_fn = json_mgr.mark_live_genre_probed
        grand_fn = json_mgr.set_live_grand_total
    elif section == "movies":
        cats = json_mgr.data["movies"].get("categories", [])
        action_type = "vod"
        action = "get_ordered_list"
        id_key = "category"
        mark_fn = json_mgr.mark_movie_category_probed
        grand_fn = json_mgr.set_movie_grand_total
    elif section == "series":
        cats = json_mgr.data["series"].get("categories", [])
        action_type = "series"
        action = "get_ordered_list"
        id_key = "category"
        mark_fn = json_mgr.mark_series_category_probed
        grand_fn = json_mgr.set_series_grand_total
    else:
        return

    total = len(cats)
    if total == 0:
        return

    wildcard = None
    for cat in cats:
        if str(cat.get("id")) == "*":
            wildcard = cat
            break
    if wildcard:
        params = {"type": action_type, "action": action, id_key: "*", "p": "1", "JsHttpRequest": "1-xml"}
        result = client.fetch(params)
        data = result.get("_data")
        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                grand_total = js.get("total_items") or 0
                grand_fn(grand_total)
                print("  -> total Channels: {} items".format(grand_total))

    probed = 0
    for cat in cats:
        cat_id = str(cat.get("id", ""))
        if not cat_id or cat_id == "*":
            continue
        params = {"type": action_type, "action": action, id_key: cat_id, "p": "1", "JsHttpRequest": "1-xml"}
        result = client.fetch(params)
        data = result.get("_data")
        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                total_items = js.get("total_items") or 0
                cat["total_items"] = total_items
                mark_fn(cat_id)
                probed += 1
                progress_bar(probed, total - 1, prefix="  Loading Categories: ")
                time.sleep(0.3)
    print()
    json_mgr.save()
    print("  -> [OK] All {} categories probed. Saved to JSON.".format(total))

# ============================================================
# CATEGORY VIEWER (20 per page) with multi-number fetch
# ============================================================
def view_categories(json_mgr, section, client=None):
    """Paginated category viewer showing ONLY pending categories. 20 per page.
    Returns list of category IDs to fetch, or None if back to menu."""
    if section == "live":
        all_cats = json_mgr.data["live"].get("categories", [])
        fetched = set(json_mgr.get_live_fetched())
        failed = set(json_mgr.get_live_failed())
        title = "Fetch Channels"
    elif section == "movies":
        all_cats = json_mgr.data["movies"].get("categories", [])
        fetched = set(json_mgr.get_movie_fetched())
        failed = set(json_mgr.get_movie_failed())
        title = "VOD Categories"
    elif section == "series":
        all_cats = json_mgr.data["series"].get("categories", [])
        fetched = set(json_mgr.get_series_fetched())
        failed = set(json_mgr.get_series_failed())
        title = "Series Categories"
    else:
        return []

    # Build display list: pending first, then fetched, then failed
    pending = [c for c in all_cats if str(c.get("id")) != "*" and str(c.get("id")) not in fetched and str(c.get("id")) not in failed]
    fetched_list = [c for c in all_cats if str(c.get("id")) != "*" and str(c.get("id")) in fetched]
    failed_list = [c for c in all_cats if str(c.get("id")) != "*" and str(c.get("id")) in failed]
    cats = pending + fetched_list + failed_list
    total_pending = len(pending)
    total = len(cats)
    if total == 0:
        print("  No categories available.")
        return []
    

    page_size = 20
    page = 0
    max_page = (total - 1) // page_size
    to_fetch_ids = []

    while True:
        clear_screen()
        print("=" * 60)
        print("   {} — Page {}/{} — {} of {} pending".format(title, page + 1, max_page + 1, total_pending, len(all_cats)))
        print("=" * 60)
        print()

        print("  {:<4} {:<8} {:<40} {:<10}".format("#", "Status", "Name", "Items"))
        print("  " + "-" * 64)
        start = page * page_size
        end = min(start + page_size, total)
        for i in range(start, end):
            cat = cats[i]
            name = cat.get("title", cat.get("name", "Unknown"))[:38]
            total_items = cat.get("total_items", 0)
            if i < total_pending:
                status = "[]"
            elif i < total_pending + len(fetched_list):
                status = "[x]"
            else:
                status = "[!]"
            print("  {:<4} {:<8} {:<40} {:<10}".format(i + 1, status, name, total_items))
        print()
        if total_pending == 0:
            print("  -> [OK] All categories fetched. {} done, {} failed.".format(len(fetched), len(failed)))
            print()
            print("  [Enter] Next page  |  [B] Back")
        else:
            print("  [Enter] Next page  |  [A] Fetch ALL  |  [1-{}] Select #  |  [B] Back".format(end - start))
        choice = input("  > ").strip().upper()
        if choice == "A":
            if total_pending > 0:
                to_fetch_ids = [str(c.get("id")) for c in pending]
                break
            # else: ignore when nothing pending
        elif choice == "B":
            return "done" if total_pending == 0 else None
        elif choice == "":
            if page < max_page:
                page += 1
            else:
                page = 0
        else:
            # Parse multiple numbers
            nums = []
            for part in re.split(r"[,\s]+", choice):
                part = part.strip()
                if part.isdigit():
                    n = int(part)
                    if 1 <= n <= total:
                        nums.append(n)
            if nums and total_pending > 0:
                seen = set()
                to_fetch_ids = []
                for n in nums:
                    cat = cats[n - 1]
                    cid = str(cat.get("id", ""))
                    if cid and cid not in seen:
                        to_fetch_ids.append(cid)
                        seen.add(cid)
                break
    return to_fetch_ids

# ============================================================
# BATCH FETCH — stays pending until all done or ignored
# Single updating counter, viewer integrated
# ============================================================
def _set_batch_counter(done, failed, total, extra=""):
    remaining = total - done - failed
    line = "  [{}/{}] done | {} failed | {} remaining".format(done, total, failed, remaining)
    if extra:
        line += " | " + extra
    sys.stdout.write(chr(13) + line.ljust(80))
    sys.stdout.flush()

def _clear_batch_counter():
    sys.stdout.write(chr(13) + " " * 80 + chr(13))
    sys.stdout.flush()

def _fetch_single_category(client, json_mgr, section, cat_id, action_type, action, id_key):
    """Fetch one category and update json_mgr. Returns (success, items_count, failed_pages)."""
    params = {"type": action_type, "action": action, id_key: cat_id, "p": "1", "JsHttpRequest": "1-xml"}
    if action_type == "vod" or action_type == "series":
        params["fav"] = "0"
        params["sortby"] = "added"
        params["hd"] = "0"
    result = client.fetch_all_pages(params)
    data = result.get("_data")
    failed_pages = []
    if data and isinstance(data, dict):
        # Save raw page data into the section's step file via CacheManager
        cache = getattr(json_mgr, "cache", None)
        if cache:
            step_code = {"live": "C5", "movies": "D4", "series": "E5"}.get(section)
            if step_code:
                step_path = cache.step_path(step_code)
                if step_path:
                    os.makedirs(os.path.dirname(step_path), exist_ok=True)
                    with open(step_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)

        js = data.get("js", {})
        if isinstance(js, dict):
            items = js.get("data", [])
            total_items = js.get("total_items") or len(items)
            failed_pages = js.get("_failed_pages", [])
            if section == "live":
                json_mgr.update_live_channels(cat_id, items, total_items)
            elif section == "movies":
                json_mgr.update_movie_items(cat_id, items, total_items)
            elif section == "series":
                json_mgr.update_series_items(cat_id, items, total_items)
            return True, total_items, failed_pages, params
    if section == "live":
        json_mgr.mark_live_genre_failed(cat_id)
    elif section == "movies":
        json_mgr.mark_movie_category_failed(cat_id)
    elif section == "series":
        json_mgr.mark_series_category_failed(cat_id)
    return False, 0, [], params

# ============================================================
# ITEMS SUB-MENU
# ============================================================
# ============================================================
# ITEMS HANDLER - opens viewer directly, no sub-menu
# ============================================================
def run_episodes_step(client, json_mgr):
    """Fetch episodes for selected series. Loops until user presses Back."""
    cache = getattr(json_mgr, "cache", None)
    existing_responses = []
    if cache:
        step_path = cache.step_path("E3")
        if step_path and os.path.exists(step_path):
            try:
                with open(step_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    existing_responses = existing.get("responses", [])
            except Exception:
                pass

    while True:
        # Collect all series items
        series_items = []
        for cat in json_mgr.data["series"].get("categories", []):
            for item in cat.get("items", []):
                series_items.append(item)
        if not series_items:
            print("  No series available. Fetch series first.")
            _cooldown()
            return False

        page_size = 20
        page = 0
        to_fetch = []

        while True:
            # Recalculate pending/fetched each time
            pending = [it for it in series_items if not it.get("seasons")]
            fetched = [it for it in series_items if it.get("seasons")]
            series_items = pending + fetched
            total_pending = len(pending)
            total = len(series_items)
            max_page = (total - 1) // page_size
            if total == 0:
                print("  No series available.")
                _cooldown()
                return False

            clear_screen()
            print("=" * 60)
            print("   Available Series — Page {}/{} — {} of {} pending".format(page + 1, max_page + 1, total_pending, total))
            print("=" * 60)
            print()
            print("  {:<4} {:<8} {:<40} {:<10}".format("#", "Status", "Name", "ID"))
            print("  " + "-" * 64)
            start = page * page_size
            end = min(start + page_size, total)
            displayed_fetched_header = False
            for i in range(start, end):
                item = series_items[i]
                name = item.get("name", item.get("title", "Unknown"))[:38]
                sid = str(item.get("id", ""))[:8]
                if i < total_pending:
                    status = "[]"
                else:
                    if not displayed_fetched_header:
                        print("\n  --- Already fetched ({}) ---\n".format(len(fetched)))
                        displayed_fetched_header = True
                    status = "[x]"
                print("  {:<4} {:<8} {:<40} {:<10}".format(i + 1, status, name, sid))
            print()
            if total_pending == 0:
                print("  -> [OK] All series fetched. {}/{} items.".format(len(fetched), total))
                print()
                print("  [Enter] Next page  |  [B] Back")
            else:
                print("  [A] Fetch ALL  |  [Enter] Next page  |  [1-{}] Select #  |  [B] Back".format(end - start))
            choice = input("  > ").strip().upper()
            if choice == "A":
                if total_pending > 0:
                    to_fetch = pending[:]
                else:
                    continue
            elif choice == "B":
                return "done" if total_pending == 0 else None
            elif choice == "":
                if page < max_page:
                    page += 1
                else:
                    page = 0
                continue
            else:
                nums = []
                for part in re.split(r"[,\s]+", choice):
                    part = part.strip()
                    if part.isdigit():
                        n = int(part)
                        if 1 <= n <= total:
                            nums.append(n)
                if nums and total_pending > 0:
                    seen = set()
                    to_fetch = []
                    for n in nums:
                        item = series_items[n - 1]
                        if item["id"] not in seen:
                            to_fetch.append(item)
                            seen.add(item["id"])
                else:
                    continue

            if not to_fetch:
                continue

            # Fetch episodes with progress bar
            print()
            fail_count = 0
            for i, item in enumerate(to_fetch):
                sid = item.get("id", "")
                name = item.get("name", item.get("title", "Unknown"))
                params = {
                    "type": "series", "action": "get_ordered_list",
                    "movie_id": sid, "season_id": "0", "episode_id": "0",
                    "row": "0", "JsHttpRequest": "1-xml"
                }
                result = client.fetch(params)
                data = result.get("_data")
                if data:
                    js = data.get("js", {}) if isinstance(data, dict) else {}
                    items = js.get("data", []) if isinstance(js, dict) else []
                    seasons = []
                    for season_item in items:
                        seasons.append({
                            "season_id": season_item.get("id", ""),
                            "name": season_item.get("name", "Unknown"),
                            "episodes": season_item.get("series", []),
                            "cmd": season_item.get("cmd", "")
                        })
                    json_mgr.update_series_episodes(sid, seasons)
                    existing_responses.append({
                        "id": sid,
                        "name": name,
                        "raw_response": data
                    })
                else:
                    fail_count += 1
                # Progress bar
                pct = ((i + 1) / len(to_fetch)) * 100
                filled = int(30 * (i + 1) / len(to_fetch))
                bar = "=" * filled + "-" * (30 - filled)
                line = "  Fetching: [{}] {:5.1f}% ({}/{})".format(bar, pct, i + 1, len(to_fetch))
                if fail_count:
                    line += "  |  {} failed".format(fail_count)
                sys.stdout.write(chr(13) + line.ljust(80))
                sys.stdout.flush()
                time.sleep(0.3)

            print()
            if fail_count:
                print("  -> [OK] Fetched {}/{} series. {} failed.".format(len(to_fetch) - fail_count, len(to_fetch), fail_count))
            else:
                print("  -> [OK] Fetched {}/{} series.".format(len(to_fetch), len(to_fetch)))
            if cache:
                step_path = cache.step_path("E3")
                if step_path:
                    step_data = {
                        "_status": "done",
                        "_total_fetched": len(existing_responses),
                        "_total_failed": fail_count,
                        "responses": existing_responses
                    }
                    os.makedirs(os.path.dirname(step_path), exist_ok=True)
                    with open(step_path, "w", encoding="utf-8") as f:
                        json.dump(step_data, f, indent=2, ensure_ascii=False)
            time.sleep(0.5)
            to_fetch = []  # clear for next loop

def batch_fetch_section(client, json_mgr, section):
    """Main batch fetch loop for a section. Auto-shows viewer. Blocks until all done or ignored."""
    if section == "live":
        all_cats = json_mgr.data["live"].get("categories", [])
        action_type = "itv"
        action = "get_ordered_list"
        id_key = "genre"
        get_fetched = json_mgr.get_live_fetched
        get_failed = json_mgr.get_live_failed
        section_name = "Live"
    elif section == "movies":
        all_cats = json_mgr.data["movies"].get("categories", [])
        action_type = "vod"
        action = "get_ordered_list"
        id_key = "category"
        get_fetched = json_mgr.get_movie_fetched
        get_failed = json_mgr.get_movie_failed
        section_name = "Movie"
    elif section == "series":
        all_cats = json_mgr.data["series"].get("categories", [])
        action_type = "series"
        action = "get_ordered_list"
        id_key = "category"
        get_fetched = json_mgr.get_series_fetched
        get_failed = json_mgr.get_series_failed
        section_name = "Series"
    else:
        return True

    all_ids = [str(c.get("id")) for c in all_cats if str(c.get("id")) != "*"]
    total = len(all_ids)

    while True:
        fetched = set(get_fetched())
        failed = set(get_failed())
        remaining = [cid for cid in all_ids if cid not in fetched and cid not in failed]

        # Auto-show viewer — even when all done, so user can see final state
        to_fetch = view_categories(json_mgr, section, client)
        if to_fetch == "done":
            return "done"
        if to_fetch is None:
            return False
        if not to_fetch:
            return True

        # Fetch selected categories
        print()
        done_count = len(fetched)
        fail_count = len(failed)
        total_failed_pages = 0
        retry_queue = []  # [(params_template, page_numbers, cat_id, section)]
        for i, cid in enumerate(to_fetch):
            ok, _, failed_pages, p_template = _fetch_single_category(client, json_mgr, section, cid, action_type, action, id_key)
            if ok:
                done_count += 1
                if failed_pages:
                    total_failed_pages += len(failed_pages)
                    retry_queue.append((p_template, failed_pages, cid, section))
            else:
                fail_count += 1
            # Update progress bar with failed page count
            line = "  Fetching: [{}/{}] done".format(i + 1, len(to_fetch))
            if total_failed_pages:
                line += "  |  {} pages failed".format(total_failed_pages)
            sys.stdout.write(chr(13) + line.ljust(80))
            sys.stdout.flush()
            time.sleep(0.1)
        _clear_batch_counter()
        print("  -> [OK] {} fetched. {} pages failed across {} categories.".format(len(to_fetch), total_failed_pages, len(retry_queue)))

        # Phase 2: Deferred retry of all failed pages
        if retry_queue:
            all_retry_pages = sum(len(pages) for _, pages, _, _ in retry_queue)
            retry_done = 0
            retry_ok = 0
            retry_still_failed = 0
            for p_template, page_numbers, cat_id, sec in retry_queue:
                for p in page_numbers:
                    p_params = dict(p_template)
                    p_params["p"] = str(p)
                    recovered = False
                    for attempt in range(3):
                        page_result = client._get(p_params)
                        page_data = page_result.get("_data", {})
                        if page_data and isinstance(page_data, dict):
                            items = page_data.get("js", {}).get("data", [])
                            if items:
                                # Append to existing category in JSON manager
                                if sec == "live":
                                    existing = json_mgr.data["live"]["categories"]
                                elif sec == "movies":
                                    existing = json_mgr.data["movies"]["categories"]
                                elif sec == "series":
                                    existing = json_mgr.data["series"]["categories"]
                                for cat in existing:
                                    if str(cat.get("id")) == str(cat_id):
                                        if sec == "live":
                                            cat["channels"].extend([trim_channel(ch) for ch in items])
                                        elif sec == "movies":
                                            cat["items"].extend([trim_movie(m) for m in items])
                                        elif sec == "series":
                                            cat["items"].extend([trim_series_item(s) for s in items])
                                        break
                                json_mgr.save()
                                retry_ok += 1
                                recovered = True
                                break
                        if attempt < 2:
                            time.sleep(0.5 * 2)
                    retry_done += 1
                    if not recovered:
                        retry_still_failed += 1
                    progress_bar(retry_done, all_retry_pages, prefix="  Retrying: ")
            print("  |  {} OK, {} still failed".format(retry_ok, retry_still_failed))

        # Loop back — auto-refresh viewer with updated pending list

# ============================================================
# DISPLAY — collapsed sections except current, NO step codes
# ============================================================
def print_section_status(current_step_idx, json_mgr):
    clear_screen()
    print("=" * 60)
    print("   mac2list v1.2 — Linear Flow")
    print("=" * 60)
    print()
    flat_idx = 0
    sec_num = 0
    for sec_key, sec in SECTIONS.items():
        sec_num += 1
        section_steps = [flat_idx + j for j in range(len(sec["items"]))]
        is_current_section = current_step_idx in section_steps
        done_count = sum(1 for code, _, _, _ in sec["items"] if json_mgr.is_done(code))
        ignored_count = sum(1 for code, _, _, _ in sec["items"] if json_mgr.is_ignored(code))
        total_count = len(sec["items"])

        if is_current_section:
            print("  {}. {}  ({}/{} done)".format(sec_num, sec["title"], done_count, total_count))
            for j, (code, desc, info, _) in enumerate(sec["items"]):
                if json_mgr.is_done(code):
                    mark = "[x]"
                elif json_mgr.is_ignored(code):
                    mark = "[I]"
                elif flat_idx + j == current_step_idx:
                    mark = "[>]"
                else:
                    mark = "[ ]"
                print("    {} {:<50} {}".format(mark, desc, info))
            flat_idx += len(sec["items"])
        else:

            status = ""
            if done_count == total_count:
                status = " [complete]"
            elif done_count > 0 or ignored_count > 0:
                status = " [{}/{} done]".format(done_count, total_count)
            print("  {}. {}{}".format(sec_num, sec["title"], status))
            flat_idx += len(sec["items"])
    print()
    print("-" * 60)

# ============================================================
# STEP EXECUTORS
# ============================================================
def run_auto_fetch_step(client, json_mgr, step_code, step_desc):
    params = STEP_PARAMS.get(step_code)
    if not params:
        return False, ""
    result = client.fetch(params)
    safe_name = step_desc.replace("=", "_").replace("&", "_").replace(" ", "_")[:40]
    cache = getattr(json_mgr, "cache", None)
    fname, status_str, is_error, is_200 = handle_fetch_result(result, step_code, safe_name, cache=cache)
    if is_error:
        return False, "  -> [!] Failed — saved error to {}".format(fname)
    msg = "  -> [OK] Saved to {}".format(fname)
    data = result.get("_data")
    if data and isinstance(data, dict):
        js = data.get("js", {})
        if step_code == "A2":
            json_mgr.update_profile(js)
        elif step_code == "B1":
            json_mgr.update_account(js)
        elif step_code == "C2":
            cats = js if isinstance(js, list) else (js.get("data", []) if isinstance(js, dict) else [])
            json_mgr.update_live_categories(cats)
            probe_categories(client, json_mgr, "live")
        elif step_code == "D1":
            cats = js if isinstance(js, list) else (js.get("data", []) if isinstance(js, dict) else [])
            json_mgr.update_movie_categories(cats)
            probe_categories(client, json_mgr, "movies")
        elif step_code == "E1":
            cats = js if isinstance(js, list) else (js.get("data", []) if isinstance(js, dict) else [])
            json_mgr.update_series_categories(cats)
            probe_categories(client, json_mgr, "series")
    return True, msg

def run_batch_step(client, json_mgr, step_code):
    if step_code == "C5":
        return batch_fetch_section(client, json_mgr, "live")
    elif step_code == "D4":
        return batch_fetch_section(client, json_mgr, "movies")
    elif step_code == "E5":
        return batch_fetch_section(client, json_mgr, "series")
    return True

# ============================================================
# RESOLVE HANDLER - shows list view, user picks by number
# ============================================================
def _resolve_items_list(client, json_mgr, step_code, items, title, action_type):
    """Shared resolver for C4 and D3. Loops until user presses Back."""
    if not items:
        print("  No items available. Fetch items first.")
        _cooldown()
        return False

    page_size = 20
    page = 0
    to_resolve = []
    cache = getattr(json_mgr, "cache", None)
    existing_responses = []
    if cache:
        step_path = cache.step_path(step_code)
        if step_path and os.path.exists(step_path):
            try:
                with open(step_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    existing_responses = existing.get("responses", [])
            except Exception:
                pass

    while True:
        # Recalculate pending/resolved each time we redraw
        pending = [it for it in items if not it.get("resolved_url")]
        resolved = [it for it in items if it.get("resolved_url")]
        items = pending + resolved
        total_pending = len(pending)
        total = len(items)
        max_page = (total - 1) // page_size
        if total == 0:
            print("  No items available.")
            _cooldown()
            return True

        clear_screen()
        print("=" * 60)
        print("   {} — Page {}/{} — {} of {} pending".format("Resolve Link", page + 1, max_page + 1, total_pending, total))
        print("=" * 60)
        print()
        print("  {:<4} {:<8} {:<40} {:<10}".format("#", "Status", "Name", "ID"))
        print("  " + "-" * 64)
        start = page * page_size
        end = min(start + page_size, total)
        displayed_resolved_header = False
        for i in range(start, end):
            item = items[i]
            name = item.get("name", item.get("title", "Unknown"))[:38]
            cid = str(item.get("id", ""))[:8]
            if i < total_pending:
                status = "[]"
            else:
                if not displayed_resolved_header:
                    print("\n  --- Already resolved ({}) ---\n".format(len(resolved)))
                    displayed_resolved_header = True
                status = "[x]"
            print("  {:<4} {:<8} {:<40} {:<10}".format(i + 1, status, name, cid))
        print()
        if total_pending == 0:
            print("  -> [OK] All items resolved. {}/{} items.".format(len(resolved), total))
            print()
            print("  [Enter] Next page  |  [B] Back")
        else:
            print("  [A] Resolve ALL  |  [Enter] Next page  |  [1-{}] Select #  |  [B] Back".format(end - start))
        choice = input("  > ").strip().upper()
        if choice == "A":
            if total_pending > 0:
                to_resolve = pending[:]
        elif choice == "B":
            return "done" if total_pending == 0 else None
        elif choice == "":
            if page < max_page:
                page += 1
            else:
                page = 0
            continue
        else:
            nums = []
            for part in re.split(r"[,\s]+", choice):
                part = part.strip()
                if part.isdigit():
                    n = int(part)
                    if 1 <= n <= total:
                        nums.append(n)
            if nums and total_pending > 0:
                seen = set()
                to_resolve = []
                for n in nums:
                    item = items[n - 1]
                    if item["id"] not in seen:
                        to_resolve.append(item)
                        seen.add(item["id"])
            else:
                continue

        if not to_resolve:
            continue

        print()
        fail_count = 0
        for i, item in enumerate(to_resolve):
            name = item.get("name", item.get("title", "Unknown"))
            cmd = item.get("cmd", "")
            if not cmd:
                fail_count += 1
                continue
            params = {"type": action_type, "action": "create_link", "cmd": cmd, "series": "",
                      "forced_storage": "undefined", "disable_ad": "0", "download": "0", "JsHttpRequest": "1-xml"}
            result = client.fetch(params)
            data = result.get("_data")
            if action_type == "itv":
                server_cmd = None
                if data and isinstance(data, dict):
                    js_val = data.get("js")
                    if isinstance(js_val, str):
                        server_cmd = js_val
                    elif isinstance(js_val, dict):
                        server_cmd = js_val.get("cmd")
                resolved_url = server_cmd or ""
                m_res = re.search(r"[?&]stream=([^&]*)", resolved_url)
                if m_res and m_res.group(1) == "":
                    resolved_url = re.sub(r"stream=[^&]*",
                                          "stream=" + str(item.get("id", "")),
                                          resolved_url, count=1)
                resolved_url = resolved_url[7:] if resolved_url.startswith("ffmpeg ") else resolved_url
                item["resolved_url"] = resolved_url
                json_mgr.save()
                existing_responses.append({
                    "id": item.get("id", ""),
                    "name": item.get("name", item.get("title", "")),
                    "raw_response": data if data else cmd
                })
            else:
                if data and isinstance(data, dict):
                    js_val = data.get("js")
                    if isinstance(js_val, str):
                        resolved_url = js_val
                    elif isinstance(js_val, dict):
                        resolved_url = js_val.get("cmd")
                    else:
                        resolved_url = None
                    if resolved_url:
                        resolved_url = resolved_url[7:] if resolved_url.startswith("ffmpeg ") else resolved_url
                        item["resolved_url"] = resolved_url
                        json_mgr.save()
                        existing_responses.append({
                            "id": item.get("id", ""),
                            "name": item.get("name", item.get("title", "")),
                            "raw_response": data
                        })
                    else:
                        fail_count += 1
                else:
                    fail_count += 1
            # Inline progress bar with failed count
            pct = ((i + 1) / len(to_resolve)) * 100
            filled = int(30 * (i + 1) / len(to_resolve))
            bar = "=" * filled + "-" * (30 - filled)
            line = "  Resolving: [{}] {:5.1f}% ({}/{})".format(bar, pct, i + 1, len(to_resolve))
            if fail_count:
                line += "  |  {} failed".format(fail_count)
            sys.stdout.write(chr(13) + line.ljust(80))
            sys.stdout.flush()
            time.sleep(0.3)

        print()
        if fail_count:
            print("  -> [OK] Resolved {}/{} items. {} failed.".format(len(to_resolve) - fail_count, len(to_resolve), fail_count))
        else:
            print("  -> [OK] Resolved {}/{} items.".format(len(to_resolve), len(to_resolve)))
        if cache:
            step_path = cache.step_path(step_code)
            if step_path:
                step_data = {
                    "_status": "done",
                    "_total_resolved": len(existing_responses),
                    "_total_failed": fail_count,
                    "responses": existing_responses
                }
                os.makedirs(os.path.dirname(step_path), exist_ok=True)
                with open(step_path, "w", encoding="utf-8") as f:
                    json.dump(step_data, f, indent=2, ensure_ascii=False)
        time.sleep(0.5)


def _resolve_episodes(client, json_mgr, step_code):
    """E4: Series -> Episodes -> Resolve. Loops until user presses Back."""
    cache = getattr(json_mgr, "cache", None)
    existing_responses = []
    if cache:
        step_path = cache.step_path(step_code)
        if step_path and os.path.exists(step_path):
            try:
                with open(step_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    existing_responses = existing.get("responses", [])
            except Exception:
                pass

    while True:
        # Collect series that have episodes fetched (from Step E3)
        series_items = []
        for cat in json_mgr.data["series"].get("categories", []):
            for s in cat.get("items", []):
                seasons = s.get("seasons", [])
                if seasons and any(season.get("episodes") for season in seasons):
                    series_items.append(s)

        if not series_items:
            print("  No series available. Fetch series first.")
            _cooldown()
            return False

        page_size = 20
        page = 0
        selected_series = None

        while True:
            # Recalculate pending/resolved each time
            # A series is "resolved" if it has any resolved_ep_* keys in any season
            def _is_series_resolved(s):
                for season in s.get("seasons", []):
                    for key in season:
                        if key.startswith("resolved_ep_"):
                            return True
                return False

            pending = [s for s in series_items if not _is_series_resolved(s)]
            resolved = [s for s in series_items if _is_series_resolved(s)]
            series_items = pending + resolved
            total_pending = len(pending)
            total = len(series_items)
            max_page = (total - 1) // page_size
            if total == 0:
                print("  No series available.")
                _cooldown()
                return False

            clear_screen()
            print("=" * 60)
            print("   Select Series — Page {}/{} — {} of {} pending".format(page + 1, max_page + 1, total_pending, total))
            print("=" * 60)
            print()
            print("  {:<4} {:<8} {:<40} {:<10}".format("#", "Status", "Name", "Episodes"))
            print("  " + "-" * 64)
            start = page * page_size
            end = min(start + page_size, total)
            displayed_resolved_header = False
            for i in range(start, end):
                item = series_items[i]
                name = item.get("name", item.get("title", "Unknown"))[:38]
                total_eps = sum(len(s.get("episodes", [])) for s in item.get("seasons", [])) if "seasons" in item else 0
                if i < total_pending:
                    status = "[]"
                else:
                    if not displayed_resolved_header:
                        print("\n  --- Episodes resolved ({}) ---\n".format(len(resolved)))
                        displayed_resolved_header = True
                    status = "[x]"
                print("  {:<4} {:<8} {:<40} {:<10}".format(i + 1, status, name, total_eps))
            print()
            if total_pending == 0:
                print("  -> [OK] All series episodes resolved. {}/{} items.".format(len(resolved), total))
                print()
                print("  [Enter] Next page  |  [B] Back")
            else:
                print("  [A] Resolve ALL  |  [Enter] Next page  |  [1-{}] Select #  |  [B] Back".format(end - start))
            choice = input("  > ").strip().upper()
            if choice == "A":
                if total_pending > 0:
                    # Resolve ALL episodes for ALL pending series
                    selected_series = "ALL"
                    break
            elif choice == "B":
                return "done" if total_pending == 0 else None
            elif choice == "":
                page = (page + 1) if page < max_page else 0
                continue
            else:
                nums = []
                for part in re.split(r"[,\s]+", choice):
                    part = part.strip()
                    if part.isdigit():
                        n = int(part)
                        if 1 <= n <= total:
                            nums.append(n)
                if nums and total_pending > 0:
                    selected_series = series_items[nums[0] - 1]
                    break
                else:
                    continue

        if not selected_series:
            continue

        # Handle [A] Resolve ALL
        if selected_series == "ALL":
            all_episodes = []
            for s in pending:
                for season in s.get("seasons", []):
                    s_name = season.get("name", "Unknown")
                    s_cmd = season.get("cmd", "")
                    for ep_num in season.get("episodes", []):
                        all_episodes.append({
                            "season_name": s_name,
                            "episode_num": ep_num,
                            "cmd": s_cmd,
                            "series_name": s.get("name", "Unknown"),
                            "series_obj": s
                        })
            if not all_episodes:
                print("  No episodes found.")
                time.sleep(0.5)
                continue
            # Resolve all episodes across all pending series
            print()
            fail_count = 0
            for i, ep_dict in enumerate(all_episodes):
                ep_num = ep_dict["episode_num"]
                s_name = ep_dict["season_name"]
                cmd = ep_dict["cmd"]
                s_series = ep_dict["series_obj"]
                params = {
                    "type": "vod", "action": "create_link", "cmd": cmd,
                    "series": str(ep_num),
                    "forced_storage": "undefined", "disable_ad": "0",
                    "download": "0", "JsHttpRequest": "1-xml"
                }
                result = client.fetch(params)
                data = result.get("_data")
                if data and isinstance(data, dict):
                    js_val = data.get("js")
                    if isinstance(js_val, str):
                        resolved_url = js_val
                    elif isinstance(js_val, dict):
                        resolved_url = js_val.get("cmd", "")
                    else:
                        resolved_url = ""
                    if resolved_url:
                        resolved_url = resolved_url[7:] if resolved_url.startswith("ffmpeg ") else resolved_url
                        for season in s_series.get("seasons", []):
                            if season.get("name") == s_name:
                                season["resolved_ep_{}".format(ep_num)] = resolved_url
                                break
                        json_mgr.save()
                        existing_responses.append({
                            "series_name": ep_dict.get("series_name", ""),
                            "season_name": s_name,
                            "episode_num": ep_num,
                            "raw_response": data
                        })
                    else:
                        fail_count += 1
                else:
                    fail_count += 1
                pct = ((i + 1) / len(all_episodes)) * 100
                filled = int(30 * (i + 1) / len(all_episodes))
                bar = "=" * filled + "-" * (30 - filled)
                line = "  Resolving: [{}] {:5.1f}% ({}/{})".format(bar, pct, i + 1, len(all_episodes))
                if fail_count:
                    line += "  |  {} failed".format(fail_count)
                sys.stdout.write(chr(13) + line.ljust(80))
                sys.stdout.flush()
                time.sleep(0.3)
            print()
            if fail_count:
                print("  -> [OK] Resolved {}/{} episodes. {} failed.".format(len(all_episodes) - fail_count, len(all_episodes), fail_count))
            else:
                print("  -> [OK] Resolved {}/{} episodes.".format(len(all_episodes), len(all_episodes)))
            if cache:
                step_path = cache.step_path(step_code)
                if step_path:
                    step_data = {
                        "_status": "done",
                        "_total_resolved": len(existing_responses),
                        "_total_failed": fail_count,
                        "responses": existing_responses
                    }
                    os.makedirs(os.path.dirname(step_path), exist_ok=True)
                    with open(step_path, "w", encoding="utf-8") as f:
                        json.dump(step_data, f, indent=2, ensure_ascii=False)
            time.sleep(0.5)
            selected_series = None
            continue

        # Get episodes from the selected series
        seasons = selected_series.get("seasons", [])
        if not seasons:
            print("  No seasons available for this series.")
            time.sleep(0.5)
            continue

        episodes = []
        for season in seasons:
            season_name = season.get("name", "Unknown")
            season_cmd = season.get("cmd", "")
            for ep_num in season.get("episodes", []):
                episodes.append({
                    "season_name": season_name,
                    "episode_num": ep_num,
                    "cmd": season_cmd
                })

        if not episodes:
            print("  No episodes found.")
            time.sleep(0.5)
            continue

        # Resolve episodes with progress bar
        print()
        fail_count = 0
        for i, ep_dict in enumerate(episodes):
            ep_num = ep_dict["episode_num"]
            s_name = ep_dict["season_name"]
            cmd = ep_dict["cmd"]
            params = {
                "type": "vod", "action": "create_link", "cmd": cmd,
                "series": str(ep_num),
                "forced_storage": "undefined", "disable_ad": "0",
                "download": "0", "JsHttpRequest": "1-xml"
            }
            result = client.fetch(params)
            data = result.get("_data")
            if data and isinstance(data, dict):
                js_val = data.get("js")
                if isinstance(js_val, str):
                    resolved_url = js_val
                elif isinstance(js_val, dict):
                    resolved_url = js_val.get("cmd", "")
                else:
                    resolved_url = ""
                if resolved_url:
                    resolved_url = resolved_url[7:] if resolved_url.startswith("ffmpeg ") else resolved_url
                    for season in selected_series.get("seasons", []):
                        if season.get("name") == s_name:
                            season["resolved_ep_{}".format(ep_num)] = resolved_url
                            break
                    json_mgr.save()
                    existing_responses.append({
                        "series_name": selected_series.get("name", ""),
                        "season_name": s_name,
                        "episode_num": ep_num,
                        "raw_response": data
                    })
                else:
                    fail_count += 1
            # Progress bar
            pct = ((i + 1) / len(episodes)) * 100
            filled = int(30 * (i + 1) / len(episodes))
            bar = "=" * filled + "-" * (30 - filled)
            line = "  Resolving: [{}] {:5.1f}% ({}/{})".format(bar, pct, i + 1, len(episodes))
            if fail_count:
                line += "  |  {} failed".format(fail_count)
            sys.stdout.write(chr(13) + line.ljust(80))
            sys.stdout.flush()
            time.sleep(0.3)

        print()
        if fail_count:
            print("  -> [OK] Resolved {}/{} episodes. {} failed.".format(len(episodes) - fail_count, len(episodes), fail_count))
        else:
            print("  -> [OK] Resolved {}/{} episodes.".format(len(episodes), len(episodes)))
        if cache:
            step_path = cache.step_path(step_code)
            if step_path:
                step_data = {
                    "_status": "done",
                    "_total_resolved": len(existing_responses),
                    "_total_failed": fail_count,
                    "responses": existing_responses
                }
                os.makedirs(os.path.dirname(step_path), exist_ok=True)
                with open(step_path, "w", encoding="utf-8") as f:
                    json.dump(step_data, f, indent=2, ensure_ascii=False)
        time.sleep(0.5)
        selected_series = None  # clear for next loop

def run_resolve_step_auto(client, json_mgr, step_code):
    """Resolve links by picking from list view. No manual cmd entry."""
    if step_code == "C4":
        items = []
        for cat in json_mgr.data["live"].get("categories", []):
            for ch in cat.get("channels", []):
                items.append(ch)
        return _resolve_items_list(client, json_mgr, step_code, items, "Live Channels", "itv")
    elif step_code == "D3":
        items = []
        for cat in json_mgr.data["movies"].get("categories", []):
            for m in cat.get("items", []):
                items.append(m)
        return _resolve_items_list(client, json_mgr, step_code, items, "VOD Movies", "vod")
    elif step_code == "E4":
        return _resolve_episodes(client, json_mgr, step_code)
    else:
        return False

def run_f3_step(client, json_mgr):
    cache = getattr(json_mgr, "cache", None)
    pins = ["0000", "1234", "3333"]
    unlocked = False
    msg = ""
    for pin in pins:
        msg += "\nTrying PIN {} ...".format(pin)
        params = {"type": "itv", "action": "set_parental_lock", "password": pin, "JsHttpRequest": "1-xml"}
        result = client.fetch(params)
        data = result.get("_data")
        if data:
            js = data.get("js", {}) if isinstance(data, dict) else {}
            if js is True or (isinstance(js, dict) and js.get("result") in (True, "true", 1)):
                msg += "\n  -> [OK] Unlocked with PIN {}!".format(pin)
                unlocked = True
                break
    if unlocked:
        safe_name = "type_itv_action_set_parental_lock_UNLOCKED"
        fname = save_json(data, "F3", safe_name, cache=cache)
        msg += "\n  -> Saved to {}".format(fname)
        return True, msg
    else:
        msg += "\n  -> [-] All PINs failed (0000, 1234, 3333)"
        error_data = {
            "_status": "error",
            "_error": True,
            "_timestamp": datetime.now().isoformat(),
            "_action": "type=itv&action=set_parental_lock",
            "_reason": "All PIN combinations failed (0000, 1234, 3333). Portal may require a different PIN or parental lock is already disabled.",
            "_tried_pins": pins,
            "_lockedpath": result.get("_lockedpath", [])
        }
        if cache:
            filename = cache.write_error("F3", "type_itv_action_set_parental_lock", error_data)
        else:
            os.makedirs(DATA_DIR, exist_ok=True)
            filename = os.path.join(DATA_DIR, "F3_type_itv_action_set_parental_lock_ERROR.json")
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(error_data, f, indent=2, ensure_ascii=False)
        msg += "\n  -> Saved error to {}".format(filename)
        return True, msg

# ============================================================
# RESUME / STARTUP
# ============================================================
def scan_existing_sessions():
    """Scan data/session/ for existing consolidated session files."""
    os.makedirs(SESSION_DIR, exist_ok=True)
    files = glob.glob(os.path.join(SESSION_DIR, "*.json"))
    sessions = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as file:
                data = json.load(file)
                meta = data.get("_meta", {})
                portal = meta.get("portal", "unknown")
                mac = meta.get("mac", "unknown")
                sessions.append({
                    "file": f,
                    "portal": portal,
                    "mac": mac,
                    "phone": (data.get("account") or {}).get("phone", "")
                })
        except:
            pass
    return sessions


def _expiry_label(phone):
    if not phone:
        return ""
    try:
        dt = datetime.strptime(str(phone).strip(), "%B %d, %Y, %I:%M %p")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return ""


def show_resume_menu(sessions, reveal_new=False, portal="", mac=""):
    """Display landing page. If reveal_new=True, show Portal/MAC inputs inline."""
    clear_screen()
    print("=" * 60)
    print("   mac2list v1.2")
    print("=" * 60)
    print()

    # Restore sessions
    for i, s in enumerate(sessions, 1):
        print("  [{}] Restore session".format(i))
        print("      {}  |  {}".format(s["portal"], s["mac"]))
        expiry = _expiry_label(s.get("phone", ""))
        print("      Expiry: {}".format(expiry if expiry else "—"))
        print()

    # New session
    next_num = len(sessions) + 1
    print("  [{}] New session".format(next_num))

    # Reveal inputs if requested
    if reveal_new:
        print()
        if portal:
            print("      Portal URL: {}".format(portal))
        else:
            portal = input("      Portal URL: ").strip()
        if mac:
            print("      MAC Address: {}".format(mac))
        else:
            mac = input("      MAC Address: ").strip()
        # Auto-return if both inputs are filled — no extra prompt
        if portal and mac:
            return "", portal, mac

    print()
    print("  [Q] Quit")
    print()

    return input("  > ").strip().upper(), portal, mac

# ============================================================
# HUB HELPERS
# ============================================================
def get_step_info(code):
    """Return (index, sec_key, desc, info, is_auto) for a step code."""
    for i, (sec_key, c, desc, info, is_auto) in enumerate(FLAT_STEPS):
        if c == code:
            return i, sec_key, desc, info, is_auto
    return None


def get_next_pending_step(json_mgr, step_codes):
    """Return first step code not done and not ignored."""
    for code in step_codes:
        if not json_mgr.is_done(code) and not json_mgr.is_ignored(code):
            return code
    return None


# ============================================================
# PAGE 1 — RESUME / NEW
# ============================================================
def run_resume_or_new():
    """Page 1: Clean landing with reveal inputs. Returns (portal, mac, json_mgr, is_restored)."""
    sessions = scan_existing_sessions()
    portal = None
    mac = None
    json_mgr = None
    is_restored = False

    while True:
        # Phase 1: Show menu, get choice
        choice, _, _ = show_resume_menu(sessions)
        new_session_num = str(len(sessions) + 1)

        if choice == "Q":
            print("  Quitting...")
            sys.exit(0)

        elif choice == new_session_num:
            # Phase 2: Reveal inputs on same screen
            choice2, portal, mac = show_resume_menu(sessions, reveal_new=True)
            if choice2 == "Q":
                print("  Quitting...")
                sys.exit(0)
            # Validate
            if not portal or not mac:
                print("  [!] Both portal URL and MAC address are required.")
                input("  Press Enter to retry...")
                continue
            if not re.match(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$", mac):
                print("  [!] Invalid MAC address format. Use format: 00:1A:79:XX:XX:XX")
                input("  Press Enter to retry...")
                continue
            os.makedirs(DATA_DIR, exist_ok=True)
            os.makedirs(SESSION_DIR, exist_ok=True)
            os.makedirs(CACHE_DIR, exist_ok=True)
            json_mgr = JSONManager(portal, mac)
            json_mgr.set_meta(portal, mac)
            break

        else:
            # Restore session — JSONManager loads from data/session/ automatically
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(sessions):
                    session = sessions[idx]
                    portal = session["portal"]
                    mac = session["mac"]
                    json_mgr = JSONManager(portal, mac)
                    is_restored = True
                    break
            except:
                pass

    return portal, mac, json_mgr, is_restored


# ============================================================
# PAGE 2 — MAIN HUB
# ============================================================
def _section_status(json_mgr, section_key, item_label):
    sec = json_mgr.data.get(section_key, {})
    cats = sec.get("categories", [])
    if section_key == "live":
        resolved = sum(1 for c in cats for ch in c.get("channels", []) if ch.get("resolved_url"))
    elif section_key == "movies":
        resolved = sum(1 for c in cats for m in c.get("items", []) if m.get("resolved_url"))
    else:
        resolved = sum(1 for c in cats for s in c.get("items", []) if any(se.get("resolved_ep_{}".format(ep)) for se in s.get("seasons", []) for ep in se.get("episodes", [])))
    grand_total = sec.get("grand_total", 0)
    if grand_total == 0:
        return "Not fetched"
    return "{}/{} {}".format(resolved, grand_total, item_label)

def show_hub_header(json_mgr):
    """Print hub header/menu without input prompt."""
    clear_screen()
    print("=" * 60)
    print("   mac2list v1.2 — Main Hub")
    print("=" * 60)
    print()

    cat_codes = ["C2", "D1", "E1"]
    cat_done = sum(1 for code in cat_codes if json_mgr.is_done(code))
    if cat_done == 0:
        cat_status = "Not scraped"
    elif cat_done < len(cat_codes):
        cat_status = "{}/{} scraped".format(cat_done, len(cat_codes))
    else:
        cat_status = "Updated " + _time_ago(json_mgr.data["_meta"].get("scraped_at", ""))

    live_count = sum(1 for c in json_mgr.data["live"].get("categories", []) for ch in c.get("channels", []) if ch.get("resolved_url"))
    movie_count = sum(1 for c in json_mgr.data["movies"].get("categories", []) for m in c.get("items", []) if m.get("resolved_url"))
    series_count = sum(1 for c in json_mgr.data["series"].get("categories", []) for s in c.get("items", []) if any(se.get("resolved_ep_{}".format(ep)) for se in s.get("seasons", []) for ep in se.get("episodes", [])))

    convert_status = "Exported" if json_mgr.is_done("G1") else "Ready"

    settings_done = sum(1 for code in SETTINGS_STEP_CODES if json_mgr.is_done(code))
    settings_total = len(SETTINGS_STEP_CODES)

    auth_section = SECTIONS["Auth"]
    auth_done = sum(1 for code, _, _, _ in auth_section["items"] if json_mgr.is_done(code))
    auth_total = len(auth_section["items"])

    print("  [1] Scrape Categories  —  {}".format(cat_status))
    print()
    print("  [2] Live Channels      —  {}".format(_section_status(json_mgr, "live", "ch")))
    print("  [3] VOD Movies         —  {}".format(_section_status(json_mgr, "movies", "movies")))
    print("  [4] Series             —  {}".format(_section_status(json_mgr, "series", "series")))
    print()
    print("  [5] Watch              —  {} ch, {} movies, {} series".format(live_count, movie_count, series_count))
    print("  [6] Convert            —  {}".format(convert_status))
    print()
    print("  [7] Settings           —  {}/{} done".format(settings_done, settings_total))
    print("  [8] Auth               —  {}/{} done".format(auth_done, auth_total))
    print()
    print("  [B] Back")
    print()


def show_hub(json_mgr):
    """Display Main Hub. Returns user choice string."""
    clear_screen()
    print("=" * 60)
    print("   mac2list v1.2 — Main Hub")
    print("=" * 60)
    print()

    # Scrape categories status
    cat_codes = ["C2", "D1", "E1"]
    cat_done = sum(1 for code in cat_codes if json_mgr.is_done(code))
    if cat_done == 0:
        cat_status = "Not scraped"
    elif cat_done < len(cat_codes):
        cat_status = "{}/{} scraped".format(cat_done, len(cat_codes))
    else:
        cat_status = "Updated " + _time_ago(json_mgr.data["_meta"].get("scraped_at", ""))

    # Per-section status
    # Watch counts
    live_count = sum(1 for c in json_mgr.data["live"].get("categories", []) for ch in c.get("channels", []) if ch.get("resolved_url"))
    movie_count = sum(1 for c in json_mgr.data["movies"].get("categories", []) for m in c.get("items", []) if m.get("resolved_url"))
    series_count = sum(1 for c in json_mgr.data["series"].get("categories", []) for s in c.get("items", []) if any(se.get("resolved_ep_{}".format(ep)) for se in s.get("seasons", []) for ep in se.get("episodes", [])))

    # Convert
    convert_status = "Exported" if json_mgr.is_done("G1") else "Ready"

    # Settings
    settings_done = sum(1 for code in SETTINGS_STEP_CODES if json_mgr.is_done(code))
    settings_total = len(SETTINGS_STEP_CODES)

    # Auth
    auth_section = SECTIONS["Auth"]
    auth_done = sum(1 for code, _, _, _ in auth_section["items"] if json_mgr.is_done(code))
    auth_total = len(auth_section["items"])

    print("  [1] Scrape Categories  —  {}".format(cat_status))
    print()
    print("  [2] Live Channels      —  {}".format(_section_status(json_mgr, "live", "ch")))
    print("  [3] VOD Movies         —  {}".format(_section_status(json_mgr, "movies", "movies")))
    print("  [4] Series             —  {}".format(_section_status(json_mgr, "series", "series")))
    print()
    print("  [5] Watch              —  {} ch, {} movies, {} series".format(live_count, movie_count, series_count))
    print("  [6] Convert            —  {}".format(convert_status))
    print()
    print("  [7] Settings           —  {}/{} done".format(settings_done, settings_total))
    print("  [8] Auth               —  {}/{} done".format(auth_done, auth_total))
    print()
    print("  [B] Back")
    print()

    return input("  > ").strip().upper()


def hub_loop(client, json_mgr, is_restored):
    """Main Hub loop."""
    if is_restored:
        print("  -> Session restored")
        time.sleep(0.3)
    else:
        print("  -> New session started")
        time.sleep(0.3)

    while True:
        choice = show_hub(json_mgr)
        if choice == "B":
            break
        elif choice == "1":
            cat_codes = ["C2", "D1", "E1"]
            all_done = all(json_mgr.is_done(c) for c in cat_codes)
            if all_done:
                show_hub_header(json_mgr)
                print("  Already scraped.")
                ans = input("  Re-scrape? [Y/N] > ").strip().upper()
                if ans != "Y":
                    continue
                # Reset steps so they re-run
                for c in cat_codes:
                    if json_mgr.is_done(c):
                        done = json_mgr.data["_meta"].get("done_steps", [])
                        if c in done:
                            done.remove(c)
                            json_mgr.data["_meta"]["done_steps"] = done
                json_mgr.data["_meta"]["scraped_at"] = ""
                json_mgr.save()
            while True:
                next_code = get_next_pending_step(json_mgr, cat_codes)
                if next_code is None:
                    break
                show_hub_header(json_mgr)
                idx, _, desc, info, is_auto = get_step_info(next_code)
                run_single_step(client, json_mgr, next_code, desc, info, is_auto)
        elif choice == "2":
            run_section_submenu(client, json_mgr, "Live Channels", skip=["C2"])
        elif choice == "3":
            run_section_submenu(client, json_mgr, "VOD Movies", skip=["D1"])
        elif choice == "4":
            run_section_submenu(client, json_mgr, "Series", skip=["E1"])
        elif choice == "5":
            run_watch_submenu(json_mgr)
        elif choice == "6":
            run_convert_submenu(json_mgr)
        elif choice == "7":
            run_settings_submenu(client, json_mgr)
        elif choice == "8":
            run_section_submenu(client, json_mgr, "Auth")
        else:
            print("  Invalid choice.")
            time.sleep(0.5)


def _step_progress(json_mgr, code):
    """Return progress string for a step code."""
    if code == "C5":
        cats = json_mgr.data["live"].get("categories", [])
        total = len([c for c in cats if str(c.get("id")) != "*"])
        fetched = len(json_mgr.get_live_fetched())
        return "{}/{} categories".format(fetched, total)
    elif code == "C4":
        total = sum(1 for c in json_mgr.data["live"].get("categories", []) for ch in c.get("channels", []))
        resolved = sum(1 for c in json_mgr.data["live"].get("categories", []) for ch in c.get("channels", []) if ch.get("resolved_url"))
        return "{}/{} channels".format(resolved, total)
    elif code == "D4":
        cats = json_mgr.data["movies"].get("categories", [])
        total = len([c for c in cats if str(c.get("id")) != "*"])
        fetched = len(json_mgr.get_movie_fetched())
        return "{}/{} categories".format(fetched, total)
    elif code == "D3":
        total = sum(1 for c in json_mgr.data["movies"].get("categories", []) for m in c.get("items", []))
        resolved = sum(1 for c in json_mgr.data["movies"].get("categories", []) for m in c.get("items", []) if m.get("resolved_url"))
        return "{}/{} movies".format(resolved, total)
    elif code == "E5":
        cats = json_mgr.data["series"].get("categories", [])
        total = len([c for c in cats if str(c.get("id")) != "*"])
        fetched = len(json_mgr.get_series_fetched())
        return "{}/{} categories".format(fetched, total)
    elif code == "E3":
        all_series = [s for c in json_mgr.data["series"].get("categories", []) for s in c.get("items", [])]
        total = len(all_series)
        fetched = sum(1 for s in all_series if s.get("seasons"))
        return "{}/{} series".format(fetched, total)
    elif code == "E4":
        total_eps = 0
        resolved_eps = 0
        for c in json_mgr.data["series"].get("categories", []):
            for s in c.get("items", []):
                for se in s.get("seasons", []):
                    for ep in se.get("episodes", []):
                        total_eps += 1
                        if se.get("resolved_ep_{}".format(ep)):
                            resolved_eps += 1
        return "{}/{} episodes".format(resolved_eps, total_eps)
    elif json_mgr.is_done(code):
        return "complete"
    return "pending"


def run_section_submenu(client, json_mgr, sec_key, skip=None):
    """Independent step picker for one section."""
    if skip is None:
        skip = []
    sec = SECTIONS[sec_key]
    visible_items = [(c, d, i, a) for c, d, i, a in sec["items"] if c not in skip]

    while True:
        clear_screen()
        print("=" * 60)
        print("   {}".format(sec["title"]))
        print("=" * 60)
        print()

        for j, (code, desc, info, _) in enumerate(visible_items):
            progress = _step_progress(json_mgr, code)
            print("  [{}] {:<45} {}".format(j + 1, desc, progress))

        print()
        if len(visible_items) > 1:
            print("  [1-{}] Pick step  |  [B] Back".format(len(visible_items)))
        else:
            print("  [1] Pick step  |  [B] Back")

        choice = input("  > ").strip().upper()
        if choice == "B":
            break
        elif choice.isdigit() and 1 <= int(choice) <= len(visible_items):
            code = visible_items[int(choice) - 1][0]
            idx, _, desc, info, is_auto = get_step_info(code)
            run_single_step(client, json_mgr, code, desc, info, is_auto)
        else:
            print("  Invalid choice.")
            time.sleep(0.5)
            
# ============================================================
# PAGE 3 — SETTINGS SUB-MENU
# ============================================================
def print_settings_submenu(json_mgr):
    """Display Settings sub-menu."""
    clear_screen()
    print("=" * 60)

    done_count = sum(1 for code in SETTINGS_STEP_CODES if json_mgr.is_done(code))
    total = len(SETTINGS_STEP_CODES)
    print("   Settings — {}/{} done".format(done_count, total))
    print("=" * 60)
    print()

    sec = SECTIONS["Settings"]
    for j, (code, desc, info, _) in enumerate(sec["items"]):
        if json_mgr.is_done(code):
            mark = "[x]"
        elif json_mgr.is_ignored(code):
            mark = "[I]"
        else:
            mark = "[>]"
        print("  {} {:<50} {}".format(mark, desc, info))

    print()
    print("  [Enter] Continue next pending  |  [B] Back")


def run_settings_submenu(client, json_mgr):
    """Settings sub-menu loop."""
    while True:
        print_settings_submenu(json_mgr)
        choice = input("  > ").strip().upper()
        if choice == "B":
            break
        elif choice == "":
            next_code = get_next_pending_step(json_mgr, SETTINGS_STEP_CODES)
            if next_code is None:
                print("  -> [OK] All settings steps complete.")
                _cooldown()
            else:
                idx, sec_key, desc, info, is_auto = get_step_info(next_code)
                run_single_step(client, json_mgr, next_code, desc, info, is_auto)
        else:
            print("  Invalid choice.")
            time.sleep(0.5)


# ============================================================
# M3U GENERATOR
# ============================================================
def generate_m3u(json_mgr):
    session_id = json_mgr.cache.session_id
    out_dir = os.path.join(OUTPUT_DIR, session_id)
    os.makedirs(out_dir, exist_ok=True)

    files = {}

    # LIVE
    lines = ["#EXTM3U"]
    for cat in json_mgr.data["live"].get("categories", []):
        group = cat.get("title", "General")
        for ch in cat.get("channels", []):
            url = ch.get("resolved_url", "")
            if not url:
                continue
            name = ch.get("name", "Unknown")
            logo = ch.get("logo", "")
            lines.append('#EXTINF:-1 tvg-id="{}" tvg-name="{}" tvg-logo="{}" group-title="{}",{}'.format(
                ch.get("id", ""), name, logo, group, name))
            lines.append(url)
    live_path = os.path.join(out_dir, "{}_LIVE.m3u".format(session_id))
    with open(live_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files["live"] = live_path

    # MOVIES
    lines = ["#EXTM3U"]
    for cat in json_mgr.data["movies"].get("categories", []):
        group = cat.get("title", "Movies")
        for m in cat.get("items", []):
            url = m.get("resolved_url", "")
            if not url:
                continue
            name = m.get("name", "Unknown")
            logo = m.get("logo", "")
            lines.append('#EXTINF:-1 tvg-id="{}" tvg-name="{}" tvg-logo="{}" group-title="{}",{}'.format(
                m.get("id", ""), name, logo, group, name))
            lines.append(url)
    movie_path = os.path.join(out_dir, "{}_MOVIE.m3u".format(session_id))
    with open(movie_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files["movies"] = movie_path

    # SERIES
    lines = ["#EXTM3U"]
    for cat in json_mgr.data["series"].get("categories", []):
        for item in cat.get("items", []):
            series_name = item.get("name", "Unknown")
            for season in item.get("seasons", []):
                season_name = season.get("name", "Unknown")
                for ep in season.get("episodes", []):
                    url = season.get("resolved_ep_{}".format(ep), "")
                    if not url:
                        continue
                    title = "{} - {} E{:02d}".format(series_name, season_name, ep)
                    lines.append('#EXTINF:-1 tvg-name="{}" group-title="Series",{}'.format(
                        series_name, title))
                    lines.append(url)
    series_path = os.path.join(out_dir, "{}_SERIE.m3u".format(session_id))
    with open(series_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files["series"] = series_path

    return files


# ============================================================
# PAGE 3 — CONVERT
# ============================================================
def run_convert_submenu(json_mgr):
    """Convert action."""
    clear_screen()
    print("=" * 60)
    print("   Convert")
    print("=" * 60)
    print()

    files = generate_m3u(json_mgr)
    json_mgr.mark_done("G1")

    print("  -> [OK] M3U files generated:")
    print("     Live:   {}".format(files.get("live", "")))
    print("     Movies: {}".format(files.get("movies", "")))
    print("     Series: {}".format(files.get("series", "")))
    print()
    print("  [R] Regenerate  |  [B] Back")
    choice = input("  > ").strip().upper()
    if choice == "R":
        files = generate_m3u(json_mgr)
        print("  -> [OK] Regenerated:")
        print("     Live:   {}".format(files.get("live", "")))
        print("     Movies: {}".format(files.get("movies", "")))
        print("     Series: {}".format(files.get("series", "")))
        _cooldown()


# ============================================================
# PAGE 3 — WATCH SUB-MENU
# ============================================================
def show_watch_submenu(json_mgr):
    """Display Watch sub-menu."""
    clear_screen()
    print("=" * 60)
    print("   Watch — Browse fetched content")
    print("=" * 60)
    print()

    live_count = sum(1 for c in json_mgr.data["live"].get("categories", []) for ch in c.get("channels", []) if ch.get("resolved_url"))
    movie_count = sum(1 for c in json_mgr.data["movies"].get("categories", []) for m in c.get("items", []) if m.get("resolved_url"))
    series_count = sum(1 for c in json_mgr.data["series"].get("categories", []) for s in c.get("items", []) if any(se.get("resolved_ep_{}".format(ep)) for se in s.get("seasons", []) for ep in se.get("episodes", [])))

    print("  [1] Live Channels     —  {} channels".format(live_count))
    print("  [2] VOD Movies        —  {} movies".format(movie_count))
    print("  [3] Series            —  {} series".format(series_count))
    print()
    print("  [B] Back")


def run_watch_submenu(json_mgr):
    """Watch sub-menu loop."""
    get_vlc_path(json_mgr)
    while True:
        show_watch_submenu(json_mgr)
        choice = input("  > ").strip().upper()
        if choice == "B":
            break
        elif choice == "1":
            watch_live(json_mgr)
        elif choice == "2":
            watch_movies(json_mgr)
        elif choice == "3":
            watch_series(json_mgr)
        else:
            print("  Invalid choice.")
            time.sleep(0.5)


# ============================================================
# SINGLE STEP EXECUTOR (no prompts, returns to caller)
# ============================================================
def run_single_step(client, json_mgr, code, desc, info, is_auto):
    """Execute a single step. Returns to caller when done."""
    print()
    print("  Executing: {} — {}".format(code, desc))
    print()

    success = False
    step_msg = ""

    if is_auto:
        success, step_msg = run_auto_fetch_step(client, json_mgr, code, desc)
    elif code in ("C4", "D3", "E4"):
        success = run_resolve_step_auto(client, json_mgr, code)
    elif code in ("C5", "D4", "E5"):
        if code == "C5":
            success = batch_fetch_section(client, json_mgr, "live")
        elif code == "D4":
            success = batch_fetch_section(client, json_mgr, "movies")
        elif code == "E5":
            success = batch_fetch_section(client, json_mgr, "series")
    elif code == "E3":
        success = run_episodes_step(client, json_mgr)
    elif code == "F3":
        success, step_msg = run_f3_step(client, json_mgr)
    elif code == "G1":
        files = generate_m3u(json_mgr)
        step_msg = "  -> [OK] M3U files saved to {}".format(os.path.join(OUTPUT_DIR, json_mgr.cache.session_id))
        success = True

    if success:
        json_mgr.mark_done(code)
        if step_msg:
            print(step_msg)
        print("  -> [OK] {} complete.".format(desc))
    else:
        if step_msg:
            print(step_msg)
        print("  -> [..] {} — not complete yet.".format(desc))

    if success or step_msg:
        print()
        _cooldown()
    return success


# ============================================================
# WATCH VIEWERS (read-only, paginated)
# ============================================================
def get_vlc_path(json_mgr):
    """Ask user for VLC path once, store in session _meta."""
    meta = json_mgr.data.setdefault("_meta", {})
    if meta.get("vlc_path"):
        p = meta["vlc_path"]
        if os.path.isfile(p):
            return p
    print("  VLC not configured. Enter path to vlc.exe")
    print("  Example: C:\\Program Files\\VideoLAN\\VLC\\vlc.exe")
    path = input("  > ").strip().strip('"')
    if not path or not os.path.isfile(path):
        print("  Invalid path, VLC playback disabled.")
        return None
    meta["vlc_path"] = path
    json_mgr.save()
    return path


def play_in_vlc(vlc_path, urls, names=None):
    """Play URLs in VLC. Single URL -> direct. Multiple -> temp M3U (overwritten each time)."""
    if not vlc_path or not urls:
        return
    if len(urls) == 1:
        subprocess.Popen([vlc_path, urls[0]])
    else:
        lines = ["#EXTM3U"]
        for i, url in enumerate(urls):
            label = names[i] if names and i < len(names) else "Track {}".format(i + 1)
            lines.append("#EXTINF:-1,{}".format(label))
            lines.append(url)
        tmp = os.path.join(OUTPUT_DIR, "temp_series_vlc.m3u")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        subprocess.Popen([vlc_path, tmp])


def paginated_browse(items, title, headers, row_fmt_fn, page_size=20, get_urls_fn=None, vlc_path=None):
    """Generic paginated read-only browser."""
    total = len(items)
    if total == 0:
        print("  No items available.")
        _cooldown()
        return

    page = 0
    max_page = (total - 1) // page_size

    while True:
        clear_screen()
        print("=" * 60)
        print("   {} — Page {}/{} — {} total".format(title, page + 1, max_page + 1, total))
        print("=" * 60)
        print()

        header_line = "  " + "  ".join(headers)
        print(header_line)
        print("  " + "-" * 64)

        start = page * page_size
        end = min(start + page_size, total)
        for i in range(start, end):
            print(row_fmt_fn(items[i], i + 1))

        print()
        if get_urls_fn:
            if max_page > 0:
                if page < max_page:
                    print("  [Enter] Next page  |  [1-{}] Play  |  [B] Back".format(end - start))
                else:
                    print("  [Enter] First page  |  [1-{}] Play  |  [B] Back".format(end - start))
            else:
                print("  [1-{}] Play  |  [B] Back".format(end - start))
        else:
            if max_page > 0:
                if page < max_page:
                    print("  [Enter] Next page  |  [B] Back")
                else:
                    print("  [Enter] First page  |  [B] Back")
            else:
                print("  [B] Back")

        choice = input("  > ").strip().upper()
        if choice == "B":
            break
        elif choice == "" and max_page > 0:
            page = (page + 1) % (max_page + 1)
        elif get_urls_fn and choice.isdigit():
            num = int(choice)
            if 1 <= num <= end - start:
                item = items[start + num - 1]
                result = get_urls_fn(item)
                if result:
                    if isinstance(result[0], tuple):
                        names = [r[0] for r in result]
                        urls  = [r[1] for r in result]
                    else:
                        names = None
                        urls  = result
                    print("  Playing...")
                    play_in_vlc(vlc_path, urls, names=names)
                    time.sleep(1)


def watch_live(json_mgr):
    """Read-only live channel viewer."""
    items = []
    for cat in json_mgr.data["live"].get("categories", []):
        for ch in cat.get("channels", []):
            if ch.get("resolved_url"):
                items.append(ch)

    if not items:
        print("  No channels available. Fetch channels first (Scrape → Live).")
        _cooldown()
        return

    def fmt(item, idx):
        name = item.get("name", "Unknown")[:50]
        return "  {:<4} {}".format(idx, name)

    def get_urls(item):
        url = item.get("resolved_url", "")
        return [(item.get("name", ""), url)] if url else []

    paginated_browse(items, "Live Channels", ["#", "Name"], fmt,
                     get_urls_fn=get_urls, vlc_path=json_mgr.data.get("_meta", {}).get("vlc_path"))


def watch_movies(json_mgr):
    """Read-only movie viewer."""
    items = []
    for cat in json_mgr.data["movies"].get("categories", []):
        for m in cat.get("items", []):
            if m.get("resolved_url"):
                items.append(m)

    if not items:
        print("  No movies available. Fetch movies first (Scrape → VOD).")
        _cooldown()
        return

    def fmt(item, idx):
        name = item.get("name", "Unknown")[:50]
        return "  {:<4} {}".format(idx, name)

    def get_urls(item):
        url = item.get("resolved_url", "")
        return [(item.get("name", ""), url)] if url else []

    paginated_browse(items, "VOD Movies", ["#", "Name"], fmt,
                     get_urls_fn=get_urls, vlc_path=json_mgr.data.get("_meta", {}).get("vlc_path"))


def watch_series(json_mgr):
    """Read-only series viewer."""
    items = []
    for cat in json_mgr.data["series"].get("categories", []):
        for s in cat.get("items", []):
            seasons = s.get("seasons", [])
            has_resolved = any(
                se.get("resolved_ep_{}".format(ep))
                for se in seasons
                for ep in se.get("episodes", [])
            )
            if has_resolved:
                items.append(s)

    if not items:
        print("  No series available. Fetch series first (Scrape → Series).")
        _cooldown()
        return

    def fmt(item, idx):
        name = item.get("name", "Unknown")[:50]
        seasons = item.get("seasons", [])
        total_eps = sum(len(se.get("episodes", [])) for se in seasons)
        resolved = 0
        for se in seasons:
            for ep in se.get("episodes", []):
                if se.get("resolved_ep_{}".format(ep)):
                    resolved += 1
        return "  {:<4} {} ({}/{})".format(idx, name, resolved, total_eps)

    def get_urls(item):
        urls = []
        series_name = item.get("name", "")
        for se in item.get("seasons", []):
            season_num = se.get("season", "")
            for ep in se.get("episodes", []):
                url = se.get("resolved_ep_{}".format(ep))
                if url:
                    label = "{} S{}E{}".format(series_name, season_num, ep)
                    urls.append((label, url))
        return urls

    paginated_browse(items, "Series", ["#", "Name"], fmt,
                     get_urls_fn=get_urls, vlc_path=json_mgr.data.get("_meta", {}).get("vlc_path"))


# ============================================================
# MAIN
# ============================================================
def main():
    while True:
        portal, mac, json_mgr, is_restored = run_resume_or_new()

        if not portal or not mac or not json_mgr:
            return

        client = Mac2ListPortal(portal, mac)

        # Handshake always runs (silent in background)
        handshake_result = client.handshake()
        handshake_data = handshake_result.get("_data")
        status = handshake_result.get("_status")
        url = handshake_result.get("_url")
        error_msg = handshake_result.get("_error")
        lockedpath = handshake_result.get("_lockedpath")
        if not client.token:
            print("[!] Handshake failed — no token received.")
            cache = getattr(json_mgr, "cache", None)
            fname = save_error_json("A1", "handshake", status, url, error_msg, lockedpath, cache=cache)
            print("  -> Saved error to {}".format(fname))
            return

        cache = getattr(json_mgr, "cache", None)
        save_json(handshake_data, "A1", "handshake", cache=cache)
        json_mgr.mark_done("A1")

        # On first launch (new session), fetch profile + account info once (no cooldown)
        if not is_restored:
            for code in ("A2", "B1"):
                _, _, desc, info, is_auto = get_step_info(code)
                ok, msg = run_auto_fetch_step(client, json_mgr, code, desc)
                if ok:
                    json_mgr.mark_done(code)
            print("  -> [OK] Profile & account info saved.")

        # Enter Hub (returns on [B] Back → session selection)
        hub_loop(client, json_mgr, is_restored)


if __name__ == "__main__":
    main()