import os
import json
from datetime import datetime

from .config import DATA_DIR, SESSION_DIR, CACHE_DIR, STEP_FILE_MAP, FLAT_STEPS
from .sessions import make_session_id

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
    # Section config: live tracks genres/channels; movies & series track categories/items
    _SECTION = {
        "live": {"bucket": "channels", "kind": "genre", "total": "total_channels"},
        "movies": {"bucket": "items", "kind": "category", "total": "total_items"},
        "series": {"bucket": "items", "kind": "category", "total": "total_items"},
    }
    _TRIM = {
        "live": trim_channel,
        "movies": trim_movie,
        "series": trim_series_item,
    }

    def _sec(self, section):
        return self.data[section]

    def _track_key(self, section, field):
        return "_{}_{}".format(field, self._SECTION[section]["kind"] + "s")

    def __init__(self, base_url, mac):
        session_id = make_session_id(base_url, mac)
        self.cache = CacheManager(session_id)
        self.cache.scaffold()
        # session file path exposed for Convert step display
        self.filename = self.cache.session_file
        self.data = self.cache.merge_all()
        self._ensure_tracking()

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

    # ----------------------------------------------------------
    # Generic section helpers  (live / movies / series)
    # ----------------------------------------------------------
    def _update_categories(self, section, categories):
        tree = self._sec(section)
        existing = {str(c.get("id")): c for c in tree.get("categories", [])}
        bucket = self._SECTION[section]["bucket"]
        merged = []
        for cat in categories:
            cat_id = str(cat.get("id"))
            if cat_id in existing and bucket in existing[cat_id]:
                merged_cat = dict(cat)
                merged_cat[bucket] = existing[cat_id][bucket]
                if existing[cat_id].get("total_items", 0) > 0:
                    merged_cat["total_items"] = existing[cat_id]["total_items"]
                merged.append(merged_cat)
            else:
                merged.append(dict(cat))
        tree["categories"] = merged
        remaining_key = self._track_key(section, "remaining")
        fetched_key = self._track_key(section, "fetched")
        failed_key = self._track_key(section, "failed")
        all_ids = [str(c.get("id")) for c in merged if str(c.get("id")) != "*"]
        fetched = set(tree.get(fetched_key, []))
        failed = set(tree.get(failed_key, []))
        tree[remaining_key] = [cid for cid in all_ids if cid not in fetched and cid not in failed]
        self.save()

    def _update_items(self, section, cat_id, items, total_items):
        tree = self._sec(section)
        trimmed = [self._TRIM[section](it) for it in items]
        cats = tree["categories"]
        bucket = self._SECTION[section]["bucket"]
        found = False
        for cat in cats:
            if str(cat.get("id")) == str(cat_id):
                cat[bucket] = trimmed
                cat["total_items"] = total_items
                found = True
                break
        if not found:
            cats.append({"id": str(cat_id), "title": "Unknown", "censored": 0,
                         "total_items": total_items, bucket: trimmed})
        tree[self._SECTION[section]["total"]] = sum(c.get("total_items", 0) for c in cats)
        cid = str(cat_id)
        remaining_key = self._track_key(section, "remaining")
        fetched_key = self._track_key(section, "fetched")
        if cid in tree.get(remaining_key, []):
            tree[remaining_key].remove(cid)
        if cid not in tree.get(fetched_key, []):
            tree.setdefault(fetched_key, []).append(cid)
        self.save()

    def _set_grand_total(self, section, total):
        self._sec(section)["grand_total"] = total
        self.save()

    def _mark_probed(self, section, cat_id):
        tree = self._sec(section)
        key = self._track_key(section, "probed")
        cid = str(cat_id)
        if cid not in tree.get(key, []):
            tree.setdefault(key, []).append(cid)
        self.save()

    def _mark_failed(self, section, cat_id):
        tree = self._sec(section)
        remaining_key = self._track_key(section, "remaining")
        failed_key = self._track_key(section, "failed")
        cid = str(cat_id)
        if cid in tree.get(remaining_key, []):
            tree[remaining_key].remove(cid)
        if cid not in tree.get(failed_key, []):
            tree.setdefault(failed_key, []).append(cid)
        self.save()

    def _get_tracked(self, section, field):
        return self._sec(section).get(self._track_key(section, field), [])

    def update_live_categories(self, categories):
        self._update_categories("live", categories)

    def update_live_channels(self, genre_id, channels, total_items):
        self._update_items("live", genre_id, channels, total_items)

    def set_live_grand_total(self, total):
        self._set_grand_total("live", total)

    def mark_live_genre_probed(self, genre_id):
        self._mark_probed("live", genre_id)

    def mark_live_genre_failed(self, genre_id):
        self._mark_failed("live", genre_id)

    def update_movie_categories(self, categories):
        self._update_categories("movies", categories)

    def update_movie_items(self, category_id, items, total_items):
        self._update_items("movies", category_id, items, total_items)

    def set_movie_grand_total(self, total):
        self._set_grand_total("movies", total)

    def mark_movie_category_probed(self, category_id):
        self._mark_probed("movies", category_id)

    def mark_movie_category_failed(self, category_id):
        self._mark_failed("movies", category_id)

    def update_series_categories(self, categories):
        self._update_categories("series", categories)

    def update_series_items(self, category_id, items, total_items):
        self._update_items("series", category_id, items, total_items)

    def set_series_grand_total(self, total):
        self._set_grand_total("series", total)

    def mark_series_category_probed(self, category_id):
        self._mark_probed("series", category_id)

    def mark_series_category_failed(self, category_id):
        self._mark_failed("series", category_id)

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
        return self._get_tracked("live", "remaining")

    def get_live_fetched(self):
        return self._get_tracked("live", "fetched")

    def get_live_failed(self):
        return self._get_tracked("live", "failed")

    def get_movie_remaining(self):
        return self._get_tracked("movies", "remaining")

    def get_movie_fetched(self):
        return self._get_tracked("movies", "fetched")

    def get_movie_failed(self):
        return self._get_tracked("movies", "failed")

    def get_series_remaining(self):
        return self._get_tracked("series", "remaining")

    def get_series_fetched(self):
        return self._get_tracked("series", "fetched")

    def get_series_failed(self):
        return self._get_tracked("series", "failed")

# ============================================================
# RAW STEP SAVING
# ============================================================
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