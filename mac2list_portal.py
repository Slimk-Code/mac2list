#!/usr/bin/env python3
"""
IPTV Portal JSON Extractor v16
Section-based linear flow: only current section expanded.
Failed steps treated as done — flow continues.
Auto-resume from temp/ folder.
"""

import requests
import json
import re
import os
import math
import time
import sys
import threading
import glob
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# SECTIONS & STEPS DEFINITION
# ============================================================

SECTIONS = {
    "A": {
        "title": "Auth, Profile & Account",
        "items": [
            ("A1", "type=stb&action=handshake", "Auth token", False),
            ("A2", "type=stb&action=get_profile", "STB profile (mac,sn)", True),
            ("B1", "type=account_info&action=get_main_info", "Phone,status,connections", True),
            ("B2", "type=account_info&action=get_info", "Full account details", True),
            ("B3", "type=account_info&action=get_tariff_plans", "Subscription plans", True),
        ]
    },
    "C": {
        "title": "Categories",
        "items": [
            ("C2", "type=itv&action=get_genres", "Channel categories", True),
            ("D1", "type=vod&action=get_categories", "VOD categories", True),
            ("E1", "type=series&action=get_categories", "Series categories", True),
        ]
    },
    "I": {
        "title": "Items",
        "items": [
            ("C5", "type=itv&action=get_ordered_list", "Channels by genre (all pages)", False),
            ("D4", "type=vod&action=get_ordered_list", "VOD by category (all pages)", False),
            ("E5", "type=series&action=get_ordered_list", "Series by category (all pages)", False),
        ]
    },
    "R": {
        "title": "Resolve Link",
        "items": [
            ("C4", "type=itv&action=create_link", "Resolve live stream URL", False),
            ("D3", "type=vod&action=create_link", "Resolve VOD stream URL", False),
            ("E3", "type=series&action=get_ordered_list&movie_id=...", "Episodes list", False),
            ("E4", "type=vod&action=create_link&series=N", "Resolve episode stream URL", False),
        ]
    },
    "F": {
        "title": "Settings & Unlock",
        "items": [
            ("F1", "type=settings&action=get", "Portal settings", True),
            ("F2", "type=settings&action=get_parental_lock", "Parental lock status", True),
            ("F3", "type=itv&action=set_parental_lock", "Unlock adult (tests 0000,1234,3333)", False),
        ]
    },
    "G": {
        "title": "Convert / Status",
        "items": [
            ("G1", "generate_json", "Generate/Regenerate consolidated JSON", False),
        ]
    },
}

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
# JSON MANAGER
# ============================================================

class JSONManager:
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
                "mac": "",
                "last_step": "",
                "ignored_steps": [],
                "done_steps": []
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

    def update_last_step(self, step_code):
        self.data["_meta"]["last_step"] = step_code
        self.save()

    def mark_done(self, step_code):
        done = self.data["_meta"].get("done_steps", [])
        if step_code not in done:
            done.append(step_code)
            self.data["_meta"]["done_steps"] = done
        self.update_last_step(step_code)

    def mark_ignored(self, step_code):
        ignored = self.data["_meta"].get("ignored_steps", [])
        if step_code not in ignored:
            ignored.append(step_code)
            self.data["_meta"]["ignored_steps"] = ignored
        self.update_last_step(step_code)

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
# UTILITY FUNCTIONS
# ============================================================

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


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
# CATEGORY VIEWER (20 per page)
# ============================================================

def view_categories(json_mgr, section):
    """Paginated category viewer. 20 per page. Returns when user quits."""
    if section == "live":
        cats = json_mgr.data["live"].get("categories", [])
        fetched = set(json_mgr.get_live_fetched())
        failed = set(json_mgr.get_live_failed())
        title = "Live Channel Categories"
    elif section == "movies":
        cats = json_mgr.data["movies"].get("categories", [])
        fetched = set(json_mgr.get_movie_fetched())
        failed = set(json_mgr.get_movie_failed())
        title = "VOD Categories"
    elif section == "series":
        cats = json_mgr.data["series"].get("categories", [])
        fetched = set(json_mgr.get_series_fetched())
        failed = set(json_mgr.get_series_failed())
        title = "Series Categories"
    else:
        return

    cats = [c for c in cats if str(c.get("id")) != "*"]
    total = len(cats)
    if total == 0:
        print("\n  No categories to view.")
        input("  Press Enter to return...")
        return

    page_size = 20
    page = 0
    max_page = (total - 1) // page_size

    while True:
        clear_screen()
        print("=" * 60)
        print("   {} — Page {}/{}".format(title, page + 1, max_page + 1))
        print("=" * 60)
        print()
        print("  {:<4} {:<6} {:<40} {:<10}".format("#", "Icon", "Name", "Status"))
        print("  " + "-" * 56)

        start = page * page_size
        end = min(start + page_size, total)
        for i in range(start, end):
            cat = cats[i]
            cid = str(cat.get("id", ""))
            name = cat.get("title", cat.get("name", "Unknown"))[:38]
            total_items = cat.get("total_items", 0)
            if cid in fetched:
                status = "Fetched"
                icon = "[x]"
            elif cid in failed:
                status = "Failed"
                icon = "[!]"
            else:
                status = "Pending"
                icon = "[ ]"
            print("  {:<4} {:<6} {:<40} {:<10} ({} items)".format(
                i + 1, icon, name, status, total_items))

        print()
        print("  [Enter] Next page  |  [P] Previous  |  [Q] Quit")
        choice = input("  > ").strip().upper()

        if choice == "Q":
            break
        elif choice == "P":
            if page > 0:
                page -= 1
        else:
            if page < max_page:
                page += 1
            else:
                page = 0


# ============================================================
# BATCH FETCH — stays pending until all done or ignored
# ============================================================

def _batch_prompt(section_name, fetched_count, failed_count, remaining_count, total_count):
    print()
    print("  {} Batch Fetch".format(section_name))
    print("  Total: {} | Fetched: {} | Failed: {} | Remaining: {}".format(
        total_count, fetched_count, failed_count, remaining_count))
    print("  Press Enter to fetch more, [A]ll to fetch rest, [V]iew, or [I]gnore...")
    return input("  > ").strip().upper()


def batch_fetch_live(client, json_mgr):
    cats = json_mgr.data["live"].get("categories", [])
    all_ids = [str(c.get("id")) for c in cats if str(c.get("id")) != "*"]
    total = len(all_ids)

    while True:
        fetched = set(json_mgr.get_live_fetched())
        failed = set(json_mgr.get_live_failed())
        remaining = [cid for cid in all_ids if cid not in fetched and cid not in failed]

        if not remaining:
            print("\n  [OK] All live genres fetched. {} done, {} failed.".format(len(fetched), len(failed)))
            return True

        choice = _batch_prompt("Live", len(fetched), len(failed), len(remaining), total)

        if choice == "I":
            print("  -> Ignored remaining {} genres.".format(len(remaining)))
            return True
        elif choice == "V":
            view_categories(json_mgr, "live")
            continue
        elif choice == "A":
            to_fetch = remaining[:]
        else:
            print("\n  How many to fetch? (or 'all'): ", end="")
            count = input().strip().lower()
            if count == "all":
                to_fetch = remaining[:]
            else:
                try:
                    n = int(count)
                    to_fetch = remaining[:n]
                except:
                    print("  [!] Invalid input.")
                    continue

        if not to_fetch:
            continue

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
                len(fetched) + done_count, total, len(failed) + fail_count, total - len(fetched) - done_count - len(failed) - fail_count))
            sys.stdout.flush()

        print()
        print("  -> [OK] Batch round complete. {} fetched, {} failed this round.".format(done_count, fail_count))
        # Loop back to prompt — stays pending


def batch_fetch_movies(client, json_mgr):
    cats = json_mgr.data["movies"].get("categories", [])
    all_ids = [str(c.get("id")) for c in cats if str(c.get("id")) != "*"]
    total = len(all_ids)

    while True:
        fetched = set(json_mgr.get_movie_fetched())
        failed = set(json_mgr.get_movie_failed())
        remaining = [cid for cid in all_ids if cid not in fetched and cid not in failed]

        if not remaining:
            print("\n  [OK] All movie categories fetched. {} done, {} failed.".format(len(fetched), len(failed)))
            return True

        choice = _batch_prompt("Movie", len(fetched), len(failed), len(remaining), total)

        if choice == "I":
            print("  -> Ignored remaining {} categories.".format(len(remaining)))
            return True
        elif choice == "V":
            view_categories(json_mgr, "movies")
            continue
        elif choice == "A":
            to_fetch = remaining[:]
        else:
            print("\n  How many to fetch? (or 'all'): ", end="")
            count = input().strip().lower()
            if count == "all":
                to_fetch = remaining[:]
            else:
                try:
                    n = int(count)
                    to_fetch = remaining[:n]
                except:
                    print("  [!] Invalid input.")
                    continue

        if not to_fetch:
            continue

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
                len(fetched) + done_count, total, len(failed) + fail_count, total - len(fetched) - done_count - len(failed) - fail_count))
            sys.stdout.flush()

        print()
        print("  -> [OK] Batch round complete. {} fetched, {} failed this round.".format(done_count, fail_count))


def batch_fetch_series(client, json_mgr):
    cats = json_mgr.data["series"].get("categories", [])
    all_ids = [str(c.get("id")) for c in cats if str(c.get("id")) != "*"]
    total = len(all_ids)

    while True:
        fetched = set(json_mgr.get_series_fetched())
        failed = set(json_mgr.get_series_failed())
        remaining = [cid for cid in all_ids if cid not in fetched and cid not in failed]

        if not remaining:
            print("\n  [OK] All series categories fetched. {} done, {} failed.".format(len(fetched), len(failed)))
            return True

        choice = _batch_prompt("Series", len(fetched), len(failed), len(remaining), total)

        if choice == "I":
            print("  -> Ignored remaining {} categories.".format(len(remaining)))
            return True
        elif choice == "V":
            view_categories(json_mgr, "series")
            continue
        elif choice == "A":
            to_fetch = remaining[:]
        else:
            print("\n  How many to fetch? (or 'all'): ", end="")
            count = input().strip().lower()
            if count == "all":
                to_fetch = remaining[:]
            else:
                try:
                    n = int(count)
                    to_fetch = remaining[:n]
                except:
                    print("  [!] Invalid input.")
                    continue

        if not to_fetch:
            continue

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
                len(fetched) + done_count, total, len(failed) + fail_count, total - len(fetched) - done_count - len(failed) - fail_count))
            sys.stdout.flush()

        print()
        print("  -> [OK] Batch round complete. {} fetched, {} failed this round.".format(done_count, fail_count))


# ============================================================
# DISPLAY — collapsed sections except current, NO step codes
# ============================================================

def print_section_status(current_step_idx, json_mgr):
    clear_screen()
    print("=" * 60)
    print("   IPTV Portal JSON Extractor v16 — Linear Flow")
    print("=" * 60)
    print()

    flat_idx = 0
    for sec_key, sec in SECTIONS.items():
        section_steps = [flat_idx + j for j in range(len(sec["items"]))]
        is_current_section = current_step_idx in section_steps

        done_count = sum(1 for code, _, _, _ in sec["items"] if json_mgr.is_done(code))
        ignored_count = sum(1 for code, _, _, _ in sec["items"] if json_mgr.is_ignored(code))
        total_count = len(sec["items"])

        if is_current_section:
            print("  ▼ {} — {}  ({}/{} done)".format(sec_key, sec['title'], done_count, total_count))
            for code, desc, info, _ in sec["items"]:
                if json_mgr.is_done(code):
                    mark = "[x]"
                elif json_mgr.is_ignored(code):
                    mark = "[I]"
                elif flat_idx == current_step_idx:
                    mark = "[→]"
                else:
                    mark = "[ ]"
                # NO step code shown — just desc and info
                print("    {} {:<50} {}".format(mark, desc, info))
                flat_idx += 1
        else:
            status = ""
            if done_count == total_count:
                status = " [all done]"
            elif done_count > 0 or ignored_count > 0:
                status = " [{}/{} done]".format(done_count, total_count)
            print("  ▶ {} — {}{}".format(sec_key, sec['title'], status))
            flat_idx += len(sec["items"])
        print()

    print("-" * 60)


def prompt_continue(step_code, step_info, step_desc):
    print("\n  [{}] {}".format(step_desc, step_info))
    print("  Press Enter to continue or [I]gnore to skip...")
    choice = input("  > ").strip().upper()
    return choice == "I"


# ============================================================
# STEP EXECUTORS
# ============================================================

def run_auto_fetch_step(client, json_mgr, step_code, step_desc):
    params = STEP_PARAMS.get(step_code)
    if not params:
        return False

    result = client.fetch(params)
    safe_name = step_desc.replace("=", "_").replace("&", "_").replace(" ", "_")[:40]
    fname, status_str, is_error, is_200 = handle_fetch_result(result, step_code, safe_name)

    if is_error:
        print("  -> [!] Failed — saved error to {}".format(fname))
        return False

    print("  -> [OK] Saved to {}".format(fname))

    data = result.get("_data")
    if data and isinstance(data, dict):
        js = data.get("js", {})

        if step_code == "A2":
            json_mgr.update_profile(js)
        elif step_code == "B1":
            json_mgr.update_account(js)
        elif step_code == "C2":
            if isinstance(js, list):
                cats = js
            elif isinstance(js, dict):
                cats = js.get("data", [])
            else:
                cats = []
            json_mgr.update_live_categories(cats)
            probe_categories(client, json_mgr, "live")
        elif step_code == "D1":
            if isinstance(js, list):
                cats = js
            elif isinstance(js, dict):
                cats = js.get("data", [])
            else:
                cats = []
            json_mgr.update_movie_categories(cats)
            probe_categories(client, json_mgr, "movies")
        elif step_code == "E1":
            if isinstance(js, list):
                cats = js
            elif isinstance(js, dict):
                cats = js.get("data", [])
            else:
                cats = []
            json_mgr.update_series_categories(cats)
            probe_categories(client, json_mgr, "series")
        elif step_code == "F1":
            pass
        elif step_code == "F2":
            pass

    return True


def run_batch_step(client, json_mgr, step_code):
    if step_code == "C5":
        return batch_fetch_live(client, json_mgr)
    elif step_code == "D4":
        return batch_fetch_movies(client, json_mgr)
    elif step_code == "E5":
        return batch_fetch_series(client, json_mgr)
    return True


def run_resolve_step(client, json_mgr, step_code, step_desc):
    if step_code == "C4":
        cmd = input("  Enter cmd value for live create_link: ").strip()
        if not cmd:
            print("  -> Skipped — no cmd provided.")
            return True
        params = {"type": "itv", "action": "create_link", "cmd": cmd, "series": "", "forced_storage": "undefined", "disable_ad": "0", "download": "0", "JsHttpRequest": "1-xml"}

    elif step_code == "D3":
        cmd = input("  Enter cmd value for VOD create_link: ").strip()
        if not cmd:
            print("  -> Skipped — no cmd provided.")
            return True
        params = {"type": "vod", "action": "create_link", "cmd": cmd, "series": "", "forced_storage": "undefined", "disable_ad": "0", "download": "0", "JsHttpRequest": "1-xml"}

    elif step_code == "E3":
        sid = input("  Enter series_id (movie_id) for episode list: ").strip()
        if not sid:
            print("  -> Skipped — no series_id provided.")
            return True
        params = {"type": "series", "action": "get_ordered_list", "movie_id": sid, "season_id": "0", "episode_id": "0", "row": "0", "JsHttpRequest": "1-xml"}

    elif step_code == "E4":
        cmd = input("  Enter cmd value for episode create_link: ").strip()
        ep_num = input("  Enter episode number: ").strip() or "1"
        if not cmd:
            print("  -> Skipped — no cmd provided.")
            return True
        params = {"type": "vod", "action": "create_link", "cmd": cmd, "series": ep_num, "forced_storage": "undefined", "disable_ad": "0", "download": "0", "JsHttpRequest": "1-xml"}

    else:
        return True

    result = client.fetch(params)
    safe_name = step_desc.replace("=", "_").replace("&", "_").replace(" ", "_")[:40]
    fname, status_str, is_error, is_200 = handle_fetch_result(result, step_code, safe_name)

    if is_error:
        print("  -> [!] Failed — saved error to {}".format(fname))
        return True

    print("  -> [OK] Saved to {}".format(fname))

    if step_code == "E3":
        data = result.get("_data")
        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, list):
                items = js
            elif isinstance(js, dict):
                items = js.get("data", [])
            else:
                items = []
            sid = params.get("movie_id", "")
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

    return True


def run_f3_step(client, json_mgr):
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
        fname = save_json(data, "F3", safe_name)
        print("  -> Saved to {}".format(fname))
        return True
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
        filename = "temp/F3_{}.json".format(safe_name)
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(error_data, f, indent=2, ensure_ascii=False)
        print("  -> Saved error to {}".format(filename))
        return True


# ============================================================
# RESUME / STARTUP
# ============================================================

def scan_existing_sessions():
    os.makedirs("temp", exist_ok=True)
    files = glob.glob("temp/*_*.json")
    sessions = []
    for f in files:
        basename = os.path.basename(f)
        if re.match(r'^[A-Z]\d+_', basename):
            continue
        try:
            with open(f, 'r', encoding='utf-8') as file:
                data = json.load(file)
            meta = data.get("_meta", {})
            portal = meta.get("portal", "unknown")
            mac = meta.get("mac", "unknown")
            last_step = meta.get("last_step", "")
            done = meta.get("done_steps", [])
            ignored = meta.get("ignored_steps", [])
            sessions.append({
                "file": f,
                "portal": portal,
                "mac": mac,
                "last_step": last_step,
                "done_count": len(done),
                "ignored_count": len(ignored)
            })
        except:
            pass
    return sessions


def show_resume_menu(sessions):
    clear_screen()
    print("=" * 60)
    print("   IPTV Portal JSON Extractor v16")
    print("=" * 60)
    print("\n  Existing sessions found:")
    print()
    for i, s in enumerate(sessions, 1):
        status = "{} done, {} ignored".format(s["done_count"], s["ignored_count"])
        if s["last_step"]:
            status += " | last: {}".format(s["last_step"])
        print("  [{}] {} | {}".format(i, s["portal"], s["mac"]))
        print("      {}".format(status))
    print()
    print("  [N] Start new session")
    print()
    choice = input("  Select: ").strip().upper()
    return choice


# ============================================================
# MAIN
# ============================================================

def main():
    sessions = scan_existing_sessions()

    portal = None
    mac = None
    json_mgr = None
    resume_idx = 0

    if sessions:
        choice = show_resume_menu(sessions)
        if choice == "N":
            pass
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(sessions):
                    session = sessions[idx]
                    with open(session["file"], 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    portal = data["_meta"].get("portal", "")
                    mac = data["_meta"].get("mac", "")
                    json_mgr = JSONManager(portal, mac)
                    json_mgr.data = data
                    resume_idx = json_mgr.get_resume_index()
                    print("\n  [OK] Resuming session: {} | {}".format(portal, mac))
                    print("  Last step: {}".format(data["_meta"].get("last_step", "none")))
                    input("  Press Enter to continue...")
            except:
                pass

    if not portal or not mac:
        clear_screen()
        print("=" * 60)
        print("   IPTV Portal JSON Extractor v16 — New Session")
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
        json_mgr = JSONManager(portal, mac)
        json_mgr.set_meta(portal, mac)

    client = IPTVPortal(portal, mac)

    # Handshake always runs
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
    json_mgr.mark_done("A1")

    # Linear flow through all sections
    for i, (sec_key, code, desc, info, is_auto) in enumerate(FLAT_STEPS):
        if i == 0:  # A1 already done above
            continue

        if i < resume_idx:
            continue

        print_section_status(i, json_mgr)

        if json_mgr.is_done(code) or json_mgr.is_ignored(code):
            continue

        ignored = prompt_continue(code, info, desc)

        if ignored:
            json_mgr.mark_ignored(code)
            print("  -> Ignored.")
            input("  Press Enter to continue to next step...")
            continue

        success = False

        if is_auto:
            success = run_auto_fetch_step(client, json_mgr, code, desc)
        elif code in ("C5", "D4", "E5"):
            success = run_batch_step(client, json_mgr, code)
        elif code in ("C4", "D3", "E3", "E4"):
            success = run_resolve_step(client, json_mgr, code, desc)
        elif code == "F3":
            success = run_f3_step(client, json_mgr)
        elif code == "G1":
            fname = json_mgr.save()
            print("  -> [OK] Consolidated JSON saved to {}".format(fname))
            success = True

        if success:
            json_mgr.mark_done(code)
            print("  -> [OK] Step complete.")
        else:
            print("  -> [!] Step failed. Marking as done, continuing...")
            json_mgr.mark_done(code)

        if code != "G1":
            input("  Press Enter to continue to next step...")

    print_section_status(len(FLAT_STEPS), json_mgr)
    print("\n  All steps complete!")
    print("\n" + "=" * 60)
    print("   Done! All fetched data saved.")
    print("=" * 60)


if __name__ == '__main__':
    main()