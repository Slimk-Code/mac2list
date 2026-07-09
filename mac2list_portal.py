#!/usr/bin/env python3
"""
IPTV Portal JSON Extractor v15
FIXED: C5/D4/E5 batch fetch now derives work list from consolidated categories.
FIXED: C2/D1/E1 merge with existing data instead of overwriting.
FIXED: Handle js as list (direct categories) or dict (wrapped in data key).
"""

import requests
import json
import re
import os
import math
import time
import sys
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed


class IPTVPortal:
    def __init__(self, base_url, mac_address):
        self.base_url = base_url.rstrip('/')
        self.mac = mac_address.upper().strip()
        self.session = requests.Session()
        self.token = None
        self.locked_url = f"{self.base_url}/portal.php"

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 4 rev: 1812 Safari/533.3',
            'X-User-Agent': 'Model: MAG250; Link: Ethernet',
            'Referer': f'{self.base_url}/c/',
            'Cookie': f'mac={self.mac}; stb_lang=en; timezone=Europe%2FLondon',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
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
                if text.strip().startswith('{'):
                    data = json.loads(text)
                    return {"_data": data, "_status": status, "_url": url, "_text": "", "_error": None, "_lockedpath": None}
                elif 'js' in text:
                    start = text.find('{')
                    end = text.rfind('}') + 1
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
        params = {'type': 'stb', 'action': 'handshake', 'JsHttpRequest': '1-xml'}
        result = self._get(params)
        data = result.get("_data")

        if data and isinstance(data, dict):
            js = data.get('js', {})
            if isinstance(js, dict):
                token = js.get('token')
                if token:
                    self.token = token
                    self.session.headers['Authorization'] = f'Bearer {token}'

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

        total = int(js.get("total_items", 0))
        per_page = int(js.get("max_page_items", len(js.get("data", [])) or 1))
        pages = math.ceil(total / per_page) if per_page else 1

        if pages <= 1:
            return result

        all_data = js.get("data", [])
        failed_pages = []
        done_count = [0]
        fetched_items = [len(all_data)]
        lock = threading.Lock()

        def _update_counter():
            with lock:
                counter = "[{}/{}] pages — [{}/{}] items".format(done_count[0], pages, fetched_items[0], total)
                sys.stdout.write("\r" + counter.ljust(80))
                sys.stdout.flush()

        def _fetch_page_thread(p):
            success = self._fetch_single_page(p, params_template, delay, pages, all_data, fetched_items, done_count, failed_pages, lock)
            if success:
                _update_counter()
            else:
                with lock:
                    fail_msg = "    Page {}/{} FAILED after 3 retries".format(p, pages)
                    sys.stdout.write("\r" + " " * 80 + "\r")
                    sys.stdout.write(fail_msg + "\n")
                    sys.stdout.write("[{}/{}] pages — [{}/{}] items".format(done_count[0], pages, fetched_items[0], total).ljust(80))
                    sys.stdout.flush()

        print("\nFetching {} pages with 10 workers...".format(pages))
        sys.stdout.write("[0/{}] pages — [{}/{}] items".format(pages, fetched_items[0], total).ljust(80))
        sys.stdout.flush()

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_fetch_page_thread, p) for p in range(2, pages + 1)]
            for future in as_completed(futures):
                future.result()

        print()

        recovered_pages = []
        still_failed = []

        if failed_pages:
            print("\nRetrying {} failed page(s)...".format(len(failed_pages)))
            for p in failed_pages:
                p_params = dict(params_template)
                p_params["p"] = str(p)
                recovered = False
                for attempt in range(3):
                    page_result = self._get(p_params)
                    page_data = page_result.get("_data", {})
                    if page_data and isinstance(page_data, dict):
                        items = page_data.get("js", {}).get("data", [])
                        if items:
                            all_data.extend(items)
                            fetched_items[0] += len(items)
                            recovered_pages.append(p)
                            print("    Page {}/{} retry OK ({} items)".format(p, pages, len(items)))
                            recovered = True
                            break
                    if attempt < 2:
                        time.sleep(delay * 2)
                if not recovered:
                    still_failed.append(p)
                    print("    Page {}/{} retry FAILED".format(p, pages))

            failed_pages = still_failed
            print()

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
    }


# ============================================================
# AUTO-APPEND JSON MANAGER — FIXED MERGE + BATCH DERIVE
# ============================================================

class JSONManager:
    """Manages <portal>_<mac>.json with auto-append/update sections.
    FIXED: Category updates merge with existing channels/items.
    FIXED: Batch fetchers derive remaining list from categories array.
    """

    def __init__(self, base_url, mac):
        safe_portal = re.sub(r'[^a-zA-Z0-9]', '_', base_url.rstrip('/').replace('http://', '').replace('https://', ''))
        safe_mac = mac.upper().replace(':', '_')
        self.filename = f"temp/{safe_portal}_{safe_mac}.json"
        self.data = self._load()
        self._ensure_tracking()

    def _load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {
            "_meta": {
                "created": datetime.now().isoformat(),
                "portal": "",
                "mac": ""
            },
            "profile": {},
            "account": {},
            "live": {"total_channels": 0, "grand_total": 0, "categories": []},
            "movies": {"total_items": 0, "grand_total": 0, "categories": []},
            "series": {"total_items": 0, "grand_total": 0, "categories": []}
        }

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
        with open(self.filename, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        return self.filename

    def set_meta(self, portal, mac):
        self.data["_meta"]["portal"] = portal
        self.data["_meta"]["mac"] = mac
        self.save()

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
            cats.append({
                "id": str(genre_id),
                "title": "Unknown",
                "censored": 0,
                "total_items": total_items,
                "channels": trimmed
            })
        self.data["live"]["total_channels"] = sum(
            c.get("total_items", 0) for c in cats
        )
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
            cats.append({
                "id": str(category_id),
                "title": "Unknown",
                "censored": 0,
                "total_items": total_items,
                "items": trimmed
            })
        self.data["movies"]["total_items"] = sum(
            c.get("total_items", 0) for c in cats
        )
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
            cats.append({
                "id": str(category_id),
                "title": "Unknown",
                "censored": 0,
                "total_items": total_items,
                "items": trimmed
            })
        self.data["series"]["total_items"] = sum(
            c.get("total_items", 0) for c in cats
        )
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
# MENU DEFINITIONS
# ============================================================

MENU = {
    "A": {
        "title": "Auth, Profile & Account",
        "items": [
            ("A1", "type=stb&action=handshake", {"type": "stb", "action": "handshake", "JsHttpRequest": "1-xml"}, "Get auth token"),
            ("A2", "type=stb&action=get_profile", {"type": "stb", "action": "get_profile", "JsHttpRequest": "1-xml"}, "STB profile (mac,sn)"),
            ("B1", "type=account_info&action=get_main_info", {"type": "account_info", "action": "get_main_info", "JsHttpRequest": "1-xml"}, "Phone,status,connections"),
            ("B2", "type=account_info&action=get_info", {"type": "account_info", "action": "get_info", "JsHttpRequest": "1-xml"}, "Full account details"),
            ("B3", "type=account_info&action=get_tariff_plans", {"type": "account_info", "action": "get_tariff_plans", "JsHttpRequest": "1-xml"}, "Subscription plans"),
        ]
    },
    "C": {
        "title": "Categories",
        "items": [
            ("C2", "type=itv&action=get_genres", {"type": "itv", "action": "get_genres", "JsHttpRequest": "1-xml"}, "Channel categories"),
            ("D1", "type=vod&action=get_categories", {"type": "vod", "action": "get_categories", "JsHttpRequest": "1-xml"}, "VOD categories"),
            ("E1", "type=series&action=get_categories", {"type": "series", "action": "get_categories", "JsHttpRequest": "1-xml"}, "Series categories"),
        ]
    },
    "I": {
        "title": "Items",
        "items": [
            ("C3", "type=itv&action=get_ordered_list", None, "Channels by genre (manual)"),
            ("D2", "type=vod&action=get_ordered_list", None, "VOD by category (manual)"),
            ("E2", "type=series&action=get_ordered_list", None, "Series by category (manual)"),
            ("C5", "batch_fetch_live", None, "BATCH: Fetch remaining live genres"),
            ("D4", "batch_fetch_movies", None, "BATCH: Fetch remaining movie categories"),
            ("E5", "batch_fetch_series", None, "BATCH: Fetch remaining series categories"),
            ("C6", "view_live_categories", None, "VIEW: Live category list"),
            ("D5", "view_movie_categories", None, "VIEW: Movie category list"),
            ("E6", "view_series_categories", None, "VIEW: Series category list"),
        ]
    },
    "R": {
        "title": "Resolve Link",
        "items": [
            ("C4", "type=itv&action=create_link (needs cmd)", None, "Resolve live stream URL"),
            ("D3", "type=vod&action=create_link (needs cmd)", None, "Resolve VOD stream URL"),
            ("E3", "type=series&action=get_ordered_list&movie_id=...", None, "Episodes list"),
            ("E4", "type=vod&action=create_link&series=N (needs cmd)", None, "Resolve episode stream URL"),
        ]
    },
    "F": {
        "title": "Settings & Unlock",
        "items": [
            ("F1", "type=settings&action=get", {"type": "settings", "action": "get", "JsHttpRequest": "1-xml"}, "Portal settings"),
            ("F2", "type=settings&action=get_parental_lock", {"type": "settings", "action": "get_parental_lock", "JsHttpRequest": "1-xml"}, "Parental lock status"),
            ("F3", "type=itv&action=set_parental_lock", None, "Unlock adult (tests 0000,1234,3333)"),
        ]
    },
    "G": {
        "title": "Convert / Status",
        "items": [
            ("G1", "generate_json", None, "Generate/Regenerate consolidated JSON"),
        ]
    },
}

G_CHECKLIST = [
    ("A2", "Account profile"),
    ("B1", "Billing info"),
    ("C2", "Live categories"),
    ("C3", "Live channels"),
    ("D1", "Movie categories"),
    ("D2", "Movie items"),
    ("E1", "Series categories"),
    ("E2", "Series items"),
    ("E3", "Episodes"),
]

AUTO_FETCH = {
    "A": ["A2", "B1"],
}


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_menu(done_map, fail_200_map, fail_other_map, expanded_cat=None):
    clear_screen()
    print("=" * 60)
    print("   API ACTION MENU")
    print("   Type letter to expand, code to fetch, 'done' to finish")
    print("=" * 60)
    for cat_key, cat in MENU.items():
        is_expanded = (cat_key == expanded_cat)
        print("\n  {} — {}".format(cat_key, cat['title']))
        if is_expanded:
            for code, desc, params, info in cat["items"]:
                if fail_200_map.get(code, False):
                    mark = "[-]"
                elif fail_other_map.get(code, False):
                    mark = "[!]"
                elif done_map.get(code, False):
                    mark = "[x]"
                else:
                    mark = "[ ]"
                if code[0] != cat_key:
                    num = code
                else:
                    num = code[1:]
                print("    {} {:<5}  {}  → {}".format(mark, num, desc, info))
    print("\n" + "-" * 60)


def print_g_status(done_map, json_mgr):
    clear_screen()
    print("=" * 60)
    print("   G — CONVERT / STATUS")
    print("=" * 60)
    print("\n  Checklist of required items:")
    print()
    all_done = True
    for code, label in G_CHECKLIST:
        if done_map.get(code, False):
            mark = "[x]"
        else:
            mark = "[ ]"
            all_done = False
        print("    {}  {}  {}".format(mark, code, label))
    print()
    print("-" * 60)
    if all_done:
        print("\n  [OK] All required items present!")
        print("  Type G1 to generate the consolidated JSON file.")
    else:
        print("\n  [!] Missing items — fetch all above before generating.")
    print()
    return all_done


def save_json(data, code, action_name):
    if not data:
        return None
    filename = f"temp/{code}_{action_name}.json"
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return filename


def save_error_json(code, action_name, status, url, error_msg, lockedpath):
    if lockedpath:
        reason = "Locked path returned empty/malformed data. See _lockedpath for details."
        error_data = {
            "_error": True,
            "_timestamp": datetime.now().isoformat(),
            "_action": action_name,
            "_reason": reason,
            "_lockedpath": lockedpath
        }
    elif status == 200:
        reason = "HTTP 200 OK from {} — portal connected but returned empty/malformed data.".format(url)
        error_data = {
            "_error": True,
            "_timestamp": datetime.now().isoformat(),
            "_action": action_name,
            "_reason": reason,
            "_url": url
        }
    elif status is not None:
        reason = "HTTP {} from {} — request failed. Portal rejected the call.".format(status, url)
        error_data = {
            "_error": True,
            "_timestamp": datetime.now().isoformat(),
            "_action": action_name,
            "_reason": reason,
            "_url": url
        }
    else:
        reason = "Connection failed — could not reach endpoint. Error: {}.".format(error_msg)
        error_data = {
            "_error": True,
            "_timestamp": datetime.now().isoformat(),
            "_action": action_name,
            "_reason": reason,
            "_url": url
        }

    filename = f"temp/{code}_{action_name}_ERROR.json"
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(error_data, f, indent=2, ensure_ascii=False)
    return filename


def handle_fetch_result(result, code, safe_name):
    data = result.get("_data")
    status = result.get("_status")
    url = result.get("_url")
    error_msg = result.get("_error")
    lockedpath = result.get("_lockedpath")

    if data:
        fname = save_json(data, code, safe_name)
        return fname, "ok", False, False
    else:
        fname = save_error_json(code, safe_name, status, url, error_msg, lockedpath)
        is_200 = (status == 200) or (lockedpath and lockedpath[0].get("status") == 200)
        return fname, "error", True, is_200


def do_auto_fetch(client, code, flat_items, done_map, fail_200_map, fail_other_map, json_mgr):
    desc, params, info = flat_items[code]
    if params is None:
        return False

    result = client.fetch(params)
    safe_name = desc.replace("=", "_").replace("&", "_").replace(" ", "_")[:40]
    fname, status_str, is_error, is_200 = handle_fetch_result(result, code, safe_name)

    if is_error:
        if is_200:
            fail_200_map[code] = True
        else:
            fail_other_map[code] = True
        return False

    data = result.get("_data")
    if data and isinstance(data, dict):
        js = data.get("js", {})

        if code == "A2":
            json_mgr.update_profile(js)
        elif code == "B1":
            json_mgr.update_account(js)
        elif code == "C2":
            if isinstance(js, list):
                cats = js
            elif isinstance(js, dict):
                cats = js.get("data", [])
            else:
                cats = []
            json_mgr.update_live_categories(cats)
        elif code == "D1":
            if isinstance(js, list):
                cats = js
            elif isinstance(js, dict):
                cats = js.get("data", [])
            else:
                cats = []
            json_mgr.update_movie_categories(cats)
        elif code == "E1":
            if isinstance(js, list):
                cats = js
            elif isinstance(js, dict):
                cats = js.get("data", [])
            else:
                cats = []
            json_mgr.update_series_categories(cats)

    done_map[code] = True
    return True


# ============================================================
# CATEGORY PROBING
# ============================================================

def probe_categories(client, json_mgr, section, done_map, fail_200_map, fail_other_map):
    if section == "live":
        code = "C2"
        cats = json_mgr.data["live"].get("categories", [])
        action_type = "itv"
        action = "get_ordered_list"
        id_key = "genre"
        mark_fn = json_mgr.mark_live_genre_probed
        grand_fn = json_mgr.set_live_grand_total
    elif section == "movies":
        code = "D1"
        cats = json_mgr.data["movies"].get("categories", [])
        action_type = "vod"
        action = "get_ordered_list"
        id_key = "category"
        mark_fn = json_mgr.mark_movie_category_probed
        grand_fn = json_mgr.set_movie_grand_total
    elif section == "series":
        code = "E1"
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
        print("\n  Probing wildcard '*' for grand total...")
        params = {
            "type": action_type,
            "action": action,
            id_key: "*",
            "p": "1",
            "JsHttpRequest": "1-xml"
        }
        result = client.fetch(params)
        data = result.get("_data")
        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                grand_total = js.get("total_items", 0)
                grand_fn(grand_total)
                print("  -> Grand total: {} items".format(grand_total))

    print("\n  Probing page 1 of {} {} categories...".format(total, section))
    probed = 0

    for cat in cats:
        cat_id = str(cat.get("id", ""))
        if not cat_id or cat_id == "*":
            continue

        params = {
            "type": action_type,
            "action": action,
            id_key: cat_id,
            "p": "1",
            "JsHttpRequest": "1-xml"
        }
        result = client.fetch(params)
        data = result.get("_data")

        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                total_items = js.get("total_items", 0)
                cat["total_items"] = total_items
                mark_fn(cat_id)

        probed += 1
        sys.stdout.write("\r  [{}/{}] categories probed".format(probed, total))
        sys.stdout.flush()
        time.sleep(0.3)

    print()
    json_mgr.save()
    print("  -> [OK] All {} categories probed. Saved to JSON.".format(total))


# ============================================================
# PAGINATED CATEGORY VIEWER
# ============================================================

def paginated_viewer(client, json_mgr, section, done_map, fail_200_map, fail_other_map):
    if section == "live":
        cats = json_mgr.data["live"].get("categories", [])
        fetched_list = json_mgr.get_live_fetched()
        failed_list = json_mgr.get_live_failed()
        action_type = "itv"
        action = "get_ordered_list"
        id_key = "genre"
        update_fn = json_mgr.update_live_channels
        fail_fn = json_mgr.mark_live_genre_failed
        code = "C3"
    elif section == "movies":
        cats = json_mgr.data["movies"].get("categories", [])
        fetched_list = json_mgr.get_movie_fetched()
        failed_list = json_mgr.get_movie_failed()
        action_type = "vod"
        action = "get_ordered_list"
        id_key = "category"
        update_fn = json_mgr.update_movie_items
        fail_fn = json_mgr.mark_movie_category_failed
        code = "D2"
    elif section == "series":
        cats = json_mgr.data["series"].get("categories", [])
        fetched_list = json_mgr.get_series_fetched()
        failed_list = json_mgr.get_series_failed()
        action_type = "series"
        action = "get_ordered_list"
        id_key = "category"
        update_fn = json_mgr.update_series_items
        fail_fn = json_mgr.mark_series_category_failed
        code = "E2"
    else:
        return

    sorted_cats = []
    wildcard = None
    normal_cats = []
    for cat in cats:
        if str(cat.get("id")) == "*":
            wildcard = cat
        else:
            normal_cats.append(cat)

    normal_cats.sort(key=lambda x: x.get("total_items", 0), reverse=True)
    if wildcard:
        sorted_cats = [wildcard] + normal_cats
    else:
        sorted_cats = normal_cats

    page_size = 50
    total_cats = len(sorted_cats)
    current_page = 1
    total_pages = math.ceil(total_cats / page_size) if total_cats else 1
    filter_mode = "all"

    while True:
        if filter_mode == "remaining":
            display_cats = [c for c in sorted_cats if str(c.get("id")) not in fetched_list and str(c.get("id")) not in failed_list]
        elif filter_mode == "fetched":
            display_cats = [c for c in sorted_cats if str(c.get("id")) in fetched_list]
        elif filter_mode == "failed":
            display_cats = [c for c in sorted_cats if str(c.get("id")) in failed_list]
        else:
            display_cats = sorted_cats

        total_display = len(display_cats)
        total_pages = math.ceil(total_display / page_size) if total_display else 1
        if current_page > total_pages:
            current_page = total_pages if total_pages > 0 else 1

        start_idx = (current_page - 1) * page_size
        end_idx = min(start_idx + page_size, total_display)
        page_cats = display_cats[start_idx:end_idx]

        clear_screen()
        title_map = {"live": "LIVE TV", "movies": "MOVIES", "series": "SERIES"}
        print("=" * 70)
        print("   {} CATEGORIES (Page {}/{}) — Filter: {}".format(title_map.get(section, section.upper()), current_page, total_pages, filter_mode))
        print("=" * 70)
        print("  [#]  ID      TITLE                  ITEMS   STATUS")
        print("  " + "-" * 64)

        for i, cat in enumerate(page_cats, 1):
            cat_id = str(cat.get("id", ""))
            title = cat.get("title", "Unknown")[:22]
            items = cat.get("total_items", 0)
            if cat_id == "*":
                status = "-"
            elif cat_id in fetched_list:
                status = "[x]"
            elif cat_id in failed_list:
                status = "[!]"
            else:
                status = "[ ]"
            print("  [{:<2}] {:<6} {:<22} {:<6} {}".format(i, cat_id, title, items, status))

        print("  " + "-" * 64)
        print("  Total: {} | Page {} of {} | {} shown".format(total_display, current_page, total_pages, len(page_cats)))
        print("  n=next  p=prev  j=jump  f=filter  b=back")
        print("  Or type row number [1-{}] to fetch that category".format(len(page_cats)))

        choice = input("\n  > ").strip().lower()

        if choice == "b":
            break
        elif choice == "n":
            if current_page < total_pages:
                current_page += 1
        elif choice == "p":
            if current_page > 1:
                current_page -= 1
        elif choice == "j":
            pg = input("  Jump to page: ").strip()
            try:
                pg_num = int(pg)
                if 1 <= pg_num <= total_pages:
                    current_page = pg_num
            except:
                pass
        elif choice == "f":
            print("  Filter: [a]ll  [r]emaining  [f]etched  [e]rror/failed")
            fchoice = input("  > ").strip().lower()
            if fchoice == "a":
                filter_mode = "all"
            elif fchoice == "r":
                filter_mode = "remaining"
            elif fchoice == "f":
                filter_mode = "fetched"
            elif fchoice == "e":
                filter_mode = "failed"
            current_page = 1
        else:
            try:
                row_num = int(choice)
                if 1 <= row_num <= len(page_cats):
                    selected = page_cats[row_num - 1]
                    cat_id = str(selected.get("id", ""))
                    cat_title = selected.get("title", "Unknown")

                    if cat_id == "*":
                        print("\n  [!] Cannot fetch wildcard '*' category individually.")
                        input("  Press Enter to continue...")
                        continue

                    if cat_id in fetched_list:
                        print("\n  [!] Category {} already fetched.".format(cat_id))
                        input("  Press Enter to continue...")
                        continue

                    print("\n  Fetching category {} ({}) — all pages...".format(cat_id, cat_title))
                    params = {
                        "type": action_type,
                        "action": action,
                        id_key: cat_id,
                        "p": "1",
                        "JsHttpRequest": "1-xml"
                    }
                    result = client.fetch_all_pages(params)
                    data = result.get("_data")

                    if data and isinstance(data, dict):
                        js = data.get("js", {})
                        if isinstance(js, dict):
                            items = js.get("data", [])
                            total_items = js.get("total_items", len(items))
                            update_fn(cat_id, items, total_items)
                            print("  -> [OK] Fetched {} items.".format(total_items))
                            done_map[code] = True
                        else:
                            fail_fn(cat_id)
                            print("  -> [!] Failed to fetch.")
                    else:
                        fail_fn(cat_id)
                        print("  -> [!] Failed to fetch.")

                    input("  Press Enter to continue...")
            except ValueError:
                pass


# ============================================================
# BATCH FETCH — FIXED: derive remaining from categories array
# ============================================================

def batch_fetch_live(client, json_mgr, done_map, fail_200_map, fail_other_map):
    cats = json_mgr.data["live"].get("categories", [])
    fetched = set(json_mgr.get_live_fetched())
    failed = set(json_mgr.get_live_failed())

    all_ids = [str(c.get("id")) for c in cats if str(c.get("id")) != "*"]
    remaining = [cid for cid in all_ids if cid not in fetched and cid not in failed]

    if not remaining:
        print("\n  [OK] Nothing left to fetch. {} done, {} failed.".format(len(fetched), len(failed)))
        return

    print("\n  Live Batch Fetch")
    print("  Total genres: {} | Remaining: {} | Fetched: {} | Failed: {}".format(len(all_ids), len(remaining), len(fetched), len(failed)))
    count = input("  How many to fetch? (or 'all'): ").strip().lower()

    if count == "all":
        to_fetch = remaining[:]
    else:
        try:
            n = int(count)
            to_fetch = remaining[:n]
        except:
            print("  [!] Invalid input.")
            return

    if not to_fetch:
        return

    print("\n  Fetching {} genres (all pages each)...".format(len(to_fetch)))
    done_count = 0
    fail_count = 0

    for gid in to_fetch:
        params = {"type": "itv", "action": "get_ordered_list", "genre": gid, "p": "1", "JsHttpRequest": "1-xml"}
        result = client.fetch_all_pages(params)
        data = result.get("_data")

        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                items = js.get("data", [])
                total_items = js.get("total_items", len(items))
                json_mgr.update_live_channels(gid, items, total_items)
                done_count += 1
            else:
                json_mgr.mark_live_genre_failed(gid)
                fail_count += 1
        else:
            json_mgr.mark_live_genre_failed(gid)
            fail_count += 1

        sys.stdout.write("\r  [{}/{}] done | {} failed | {} remaining".format(
            done_count, len(to_fetch), fail_count, len(to_fetch) - done_count - fail_count))
        sys.stdout.flush()

    print()
    print("  -> [OK] Batch complete. {} fetched, {} failed.".format(done_count, fail_count))
    done_map["C3"] = True


def batch_fetch_movies(client, json_mgr, done_map, fail_200_map, fail_other_map):
    cats = json_mgr.data["movies"].get("categories", [])
    fetched = set(json_mgr.get_movie_fetched())
    failed = set(json_mgr.get_movie_failed())

    all_ids = [str(c.get("id")) for c in cats if str(c.get("id")) != "*"]
    remaining = [cid for cid in all_ids if cid not in fetched and cid not in failed]

    if not remaining:
        print("\n  [OK] Nothing left to fetch. {} done, {} failed.".format(len(fetched), len(failed)))
        return

    print("\n  Movie Batch Fetch")
    print("  Total categories: {} | Remaining: {} | Fetched: {} | Failed: {}".format(len(all_ids), len(remaining), len(fetched), len(failed)))
    count = input("  How many to fetch? (or 'all'): ").strip().lower()

    if count == "all":
        to_fetch = remaining[:]
    else:
        try:
            n = int(count)
            to_fetch = remaining[:n]
        except:
            print("  [!] Invalid input.")
            return

    if not to_fetch:
        return

    print("\n  Fetching {} categories (all pages each)...".format(len(to_fetch)))
    done_count = 0
    fail_count = 0

    for cid in to_fetch:
        params = {"type": "vod", "action": "get_ordered_list", "category": cid, "fav": "0", "sortby": "added", "hd": "0", "p": "1", "JsHttpRequest": "1-xml"}
        result = client.fetch_all_pages(params)
        data = result.get("_data")

        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                items = js.get("data", [])
                total_items = js.get("total_items", len(items))
                json_mgr.update_movie_items(cid, items, total_items)
                done_count += 1
            else:
                json_mgr.mark_movie_category_failed(cid)
                fail_count += 1
        else:
            json_mgr.mark_movie_category_failed(cid)
            fail_count += 1

        sys.stdout.write("\r  [{}/{}] done | {} failed | {} remaining".format(
            done_count, len(to_fetch), fail_count, len(to_fetch) - done_count - fail_count))
        sys.stdout.flush()

    print()
    print("  -> [OK] Batch complete. {} fetched, {} failed.".format(done_count, fail_count))
    done_map["D2"] = True


def batch_fetch_series(client, json_mgr, done_map, fail_200_map, fail_other_map):
    cats = json_mgr.data["series"].get("categories", [])
    fetched = set(json_mgr.get_series_fetched())
    failed = set(json_mgr.get_series_failed())

    all_ids = [str(c.get("id")) for c in cats if str(c.get("id")) != "*"]
    remaining = [cid for cid in all_ids if cid not in fetched and cid not in failed]

    if not remaining:
        print("\n  [OK] Nothing left to fetch. {} done, {} failed.".format(len(fetched), len(failed)))
        return

    print("\n  Series Batch Fetch")
    print("  Total categories: {} | Remaining: {} | Fetched: {} | Failed: {}".format(len(all_ids), len(remaining), len(fetched), len(failed)))
    count = input("  How many to fetch? (or 'all'): ").strip().lower()

    if count == "all":
        to_fetch = remaining[:]
    else:
        try:
            n = int(count)
            to_fetch = remaining[:n]
        except:
            print("  [!] Invalid input.")
            return

    if not to_fetch:
        return

    print("\n  Fetching {} categories (all pages each)...".format(len(to_fetch)))
    done_count = 0
    fail_count = 0

    for cid in to_fetch:
        params = {"type": "series", "action": "get_ordered_list", "category": cid, "fav": "0", "sortby": "added", "hd": "0", "p": "1", "JsHttpRequest": "1-xml"}
        result = client.fetch_all_pages(params)
        data = result.get("_data")

        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                items = js.get("data", [])
                total_items = js.get("total_items", len(items))
                json_mgr.update_series_items(cid, items, total_items)
                done_count += 1
            else:
                json_mgr.mark_series_category_failed(cid)
                fail_count += 1
        else:
            json_mgr.mark_series_category_failed(cid)
            fail_count += 1

        sys.stdout.write("\r  [{}/{}] done | {} failed | {} remaining".format(
            done_count, len(to_fetch), fail_count, len(to_fetch) - done_count - fail_count))
        sys.stdout.flush()

    print()
    print("  -> [OK] Batch complete. {} fetched, {} failed.".format(done_count, fail_count))
    done_map["E2"] = True


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("   IPTV Portal JSON Extractor v15 — portal.php")
    print("=" * 60)

    portal = input("\nPortal URL (e.g., http://example.com or http://ip:port): ").strip()
    mac = input("MAC Address (e.g., 00:1A:79:XX:XX:XX): ").strip()

    if not portal or not mac:
        print("[!] Both portal URL and MAC address are required.")
        return

    if not re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', mac):
        print("[!] Invalid MAC address format. Use format: 00:1A:79:XX:XX:XX")
        return

    os.makedirs("temp", exist_ok=True)

    client = IPTVPortal(portal, mac)
    json_mgr = JSONManager(portal, mac)
    json_mgr.set_meta(portal, mac)

    print("\n[Handshake] Using: {}".format(client.locked_url))
    handshake_result = client.handshake()
    handshake_data = handshake_result.get("_data")

    status = handshake_result.get("_status")
    url = handshake_result.get("_url")
    error_msg = handshake_result.get("_error")
    lockedpath = handshake_result.get("_lockedpath")

    print("  Status: {}, URL: {}".format(status, url))

    if not client.token:
        print("[!] Handshake failed — no token received.")
        fname = save_error_json("A1", "handshake", status, url, error_msg, lockedpath)
        print("  -> Saved error to {}".format(fname))
        return

    print("  [OK] Token received.")

    save_json(handshake_data, "A1", "handshake")
    print("  -> Saved to temp/A1_handshake.json")

    done_map = {"A1": True}
    fail_200_map = {}
    fail_other_map = {}
    expanded_cat = "A"

    flat_items = {}
    for cat_key, cat in MENU.items():
        for code, desc, params, info in cat["items"]:
            flat_items[code] = (desc, params, info)

    print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)

    while True:
        choice = input("\nEnter letter/code (or 'done' / 'list'): ").strip().upper()

        if choice in ("DONE", "EXIT", "QUIT"):
            break

        if choice == "LIST":
            print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)
            continue

        if choice == "G":
            print_g_status(done_map, json_mgr)
            continue

        if choice == "G1":
            all_done = print_g_status(done_map, json_mgr)
            if all_done:
                fname = json_mgr.save()
                print("\n  [OK] Consolidated JSON generated: {}".format(fname))
            else:
                print("\n  [!] Cannot generate — missing required items.")
            continue

        if choice in MENU and len(choice) == 1:
            auto_codes = AUTO_FETCH.get(choice, [])
            if isinstance(auto_codes, str):
                auto_codes = [auto_codes]

            for auto_code in auto_codes:
                if auto_code and not done_map.get(auto_code, False) and not fail_200_map.get(auto_code, False) and not fail_other_map.get(auto_code, False):
                    print("\n  [Auto-fetch] {} ...".format(auto_code))
                    ok = do_auto_fetch(client, auto_code, flat_items, done_map, fail_200_map, fail_other_map, json_mgr)
                    if ok:
                        print("  -> [OK] {} fetched and appended.".format(auto_code))
                    else:
                        print("  -> [!] {} auto-fetch failed.".format(auto_code))

            for auto_code in auto_codes:
                if done_map.get(auto_code, False):
                    if auto_code == "C2":
                        probe_categories(client, json_mgr, "live", done_map, fail_200_map, fail_other_map)
                    elif auto_code == "D1":
                        probe_categories(client, json_mgr, "movies", done_map, fail_200_map, fail_other_map)
                    elif auto_code == "E1":
                        probe_categories(client, json_mgr, "series", done_map, fail_200_map, fail_other_map)

            expanded_cat = choice
            print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)
            continue

        if choice.isdigit():
            full_code = expanded_cat + choice
            if full_code not in flat_items:
                print("[!] {} does not exist in {}.".format(full_code, expanded_cat))
                continue
            choice = full_code
        elif choice not in flat_items:
            print("[!] Unknown: {}. Type a letter (A,C,I,R,F,G) to expand, or a code.".format(choice))
            continue

        if done_map.get(choice, False) or fail_200_map.get(choice, False) or fail_other_map.get(choice, False):
            print("[!] {} already tried.".format(choice))
            continue

        desc, params, info = flat_items[choice]
        use_all_pages = False

        if choice == "C6":
            paginated_viewer(client, json_mgr, "live", done_map, fail_200_map, fail_other_map)
            print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)
            continue

        if choice == "D5":
            paginated_viewer(client, json_mgr, "movies", done_map, fail_200_map, fail_other_map)
            print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)
            continue

        if choice == "E6":
            paginated_viewer(client, json_mgr, "series", done_map, fail_200_map, fail_other_map)
            print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)
            continue

        if choice == "C5":
            batch_fetch_live(client, json_mgr, done_map, fail_200_map, fail_other_map)
            print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)
            continue

        if choice == "D4":
            batch_fetch_movies(client, json_mgr, done_map, fail_200_map, fail_other_map)
            print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)
            continue

        if choice == "E5":
            batch_fetch_series(client, json_mgr, done_map, fail_200_map, fail_other_map)
            print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)
            continue

        if choice == "C3":
            gid = input("  Enter genre_id for get_ordered_list: ").strip()
            if not gid:
                print("  Skipped — no genre_id provided.")
                continue
            mode = input("  Fetch 1 page or ALL pages? [1/all]: ").strip().lower()
            params = {"type": "itv", "action": "get_ordered_list", "genre": gid, "p": "1", "JsHttpRequest": "1-xml"}
            if mode == "all":
                use_all_pages = True

        elif choice == "C4":
            cmd = input("  Enter cmd value for create_link: ").strip()
            if not cmd:
                print("  Skipped — no cmd provided.")
                continue
            params = {"type": "itv", "action": "create_link", "cmd": cmd, "series": "", "forced_storage": "undefined", "disable_ad": "0", "download": "0", "JsHttpRequest": "1-xml"}

        elif choice == "D2":
            cid = input("  Enter category_id for VOD ordered_list: ").strip()
            if not cid:
                print("  Skipped — no category_id provided.")
                continue
            mode = input("  Fetch 1 page or ALL pages? [1/all]: ").strip().lower()
            params = {"type": "vod", "action": "get_ordered_list", "category": cid, "fav": "0", "sortby": "added", "hd": "0", "p": "1", "JsHttpRequest": "1-xml"}
            if mode == "all":
                use_all_pages = True

        elif choice == "D3":
            cmd = input("  Enter cmd value for VOD create_link: ").strip()
            if not cmd:
                print("  Skipped — no cmd provided.")
                continue
            params = {"type": "vod", "action": "create_link", "cmd": cmd, "series": "", "forced_storage": "undefined", "disable_ad": "0", "download": "0", "JsHttpRequest": "1-xml"}

        elif choice == "E2":
            cid = input("  Enter category_id for Series ordered_list: ").strip()
            if not cid:
                print("  Skipped — no category_id provided.")
                continue
            mode = input("  Fetch 1 page or ALL pages? [1/all]: ").strip().lower()
            params = {"type": "series", "action": "get_ordered_list", "category": cid, "fav": "0", "sortby": "added", "hd": "0", "p": "1", "JsHttpRequest": "1-xml"}
            if mode == "all":
                use_all_pages = True

        elif choice == "E3":
            sid = input("  Enter series_id (movie_id) for episode list: ").strip()
            if not sid:
                print("  Skipped — no series_id provided.")
                continue
            params = {"type": "series", "action": "get_ordered_list", "movie_id": sid, "season_id": "0", "episode_id": "0", "row": "0", "JsHttpRequest": "1-xml"}

        elif choice == "E4":
            cmd = input("  Enter cmd value for episode create_link: ").strip()
            ep_num = input("  Enter episode number: ").strip() or "1"
            if not cmd:
                print("  Skipped — no cmd provided.")
                continue
            params = {"type": "vod", "action": "create_link", "cmd": cmd, "series": ep_num, "forced_storage": "undefined", "disable_ad": "0", "download": "0", "JsHttpRequest": "1-xml"}

        elif choice == "F3":
            pins = ["0000", "1234", "3333"]
            unlocked = False
            for pin in pins:
                print("\n  Trying PIN {} ...".format(pin))
                params = {"type": "itv", "action": "set_parental_lock", "password": pin, "JsHttpRequest": "1-xml"}
                result = client.fetch(params)
                data = result.get("_data")
                if data:
                    js = data.get('js', {}) if isinstance(data, dict) else {}
                    if js is True or (isinstance(js, dict) and js.get('result') in (True, 'true', 1)):
                        print("  -> [OK] Unlocked with PIN {}!".format(pin))
                        unlocked = True
                        break

            if unlocked:
                safe_name = "type_itv_action_set_parental_lock_UNLOCKED"
                fname = save_json(data, choice, safe_name)
                print("  -> Saved to {}".format(fname))
                done_map[choice] = True
            else:
                print("  -> [-] All PINs failed (0000, 1234, 3333)")
                error_data = {
                    "_error": True,
                    "_timestamp": datetime.now().isoformat(),
                    "_action": "type=itv&action=set_parental_lock",
                    "_reason": "All PIN combinations failed (0000, 1234, 3333). Portal may require a different PIN or parental lock is already disabled.",
                    "_tried_pins": pins,
                    "_lockedpath": result.get("_lockedpath", [])
                }
                safe_name = "type_itv_action_set_parental_lock_ERROR"
                filename = "temp/{}_{}.json".format(choice, safe_name)
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(error_data, f, indent=2, ensure_ascii=False)
                print("  -> Saved error to {}".format(filename))
                fail_200_map[choice] = True

            print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)
            continue

        if params is None:
            print("[!] {} requires manual parameters.".format(choice))
            continue

        print("\n  Fetching {} — {} ...".format(choice, desc))

        if use_all_pages:
            result = client.fetch_all_pages(params)
        else:
            result = client.fetch(params)

        safe_name = desc.replace("=", "_").replace("&", "_").replace(" ", "_")[:40]
        fname, status_str, is_error, is_200 = handle_fetch_result(result, choice, safe_name)

        if is_error:
            if is_200:
                print("  -> [-] Saved error to {}".format(fname))
                fail_200_map[choice] = True
            else:
                print("  -> [!] Saved error to {}".format(fname))
                fail_other_map[choice] = True
        else:
            print("  -> [OK] Saved to {}".format(fname))
            done_map[choice] = True

            data = result.get("_data")
            if data and isinstance(data, dict):
                js = data.get("js", {})

                if choice == "A2":
                    json_mgr.update_profile(js)
                elif choice == "B1":
                    json_mgr.update_account(js)
                elif choice == "C2":
                    if isinstance(js, list):
                        cats = js
                    elif isinstance(js, dict):
                        cats = js.get("data", [])
                    else:
                        cats = []
                    json_mgr.update_live_categories(cats)
                    probe_categories(client, json_mgr, "live", done_map, fail_200_map, fail_other_map)
                elif choice == "C3":
                    gid = params.get("genre", "")
                    if isinstance(js, list):
                        items = js
                    elif isinstance(js, dict):
                        items = js.get("data", [])
                    else:
                        items = []
                    total = js.get("total_items", len(items)) if isinstance(js, dict) else len(items)
                    json_mgr.update_live_channels(gid, items, total)
                elif choice == "D1":
                    if isinstance(js, list):
                        cats = js
                    elif isinstance(js, dict):
                        cats = js.get("data", [])
                    else:
                        cats = []
                    json_mgr.update_movie_categories(cats)
                    probe_categories(client, json_mgr, "movies", done_map, fail_200_map, fail_other_map)
                elif choice == "D2":
                    cid = params.get("category", "")
                    if isinstance(js, list):
                        items = js
                    elif isinstance(js, dict):
                        items = js.get("data", [])
                    else:
                        items = []
                    total = js.get("total_items", len(items)) if isinstance(js, dict) else len(items)
                    json_mgr.update_movie_items(cid, items, total)
                elif choice == "E1":
                    if isinstance(js, list):
                        cats = js
                    elif isinstance(js, dict):
                        cats = js.get("data", [])
                    else:
                        cats = []
                    json_mgr.update_series_categories(cats)
                    probe_categories(client, json_mgr, "series", done_map, fail_200_map, fail_other_map)
                elif choice == "E2":
                    cid = params.get("category", "")
                    if isinstance(js, list):
                        items = js
                    elif isinstance(js, dict):
                        items = js.get("data", [])
                    else:
                        items = []
                    total = js.get("total_items", len(items)) if isinstance(js, dict) else len(items)
                    json_mgr.update_series_items(cid, items, total)
                elif choice == "E3":
                    sid = params.get("movie_id", "")
                    if isinstance(js, list):
                        items = js
                    elif isinstance(js, dict):
                        items = js.get("data", [])
                    else:
                        items = []
                    seasons_map = {}
                    for ep in items:
                        season_id = ep.get("season_id", "0")
                        if season_id not in seasons_map:
                            seasons_map[season_id] = {
                                "season_id": season_id,
                                "name": ep.get("season_name", "Season " + str(season_id)),
                                "episodes": [],
                                "cmd": ep.get("cmd", "")
                            }
                        seasons_map[season_id]["episodes"].append(ep.get("episode_num", ep.get("number", 0)))
                    seasons = list(seasons_map.values())
                    json_mgr.update_series_episodes(sid, seasons)

        print_menu(done_map, fail_200_map, fail_other_map, expanded_cat)

    print("\n" + "=" * 60)
    print("   Done! All fetched JSON files saved to temp/.")
    print("=" * 60)


if __name__ == '__main__':
    main()
