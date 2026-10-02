import json
import os
from datetime import datetime

from .config import FLAT_STEPS, STEP_PARAMS
from .scrape import probe_categories
from .storage import (
    handle_fetch_result,
    save_json,
)


# ============================================================
# STEP QUERY HELPERS  (pure logic, silent)
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


def section_status(json_mgr, section_key, item_label):
    """Return 'Not fetched' or '{resolved}/{grand_total} {item_label}'."""
    sec = json_mgr.data.get(section_key, {})
    cats = sec.get("categories", [])
    if section_key == "live":
        resolved = sum(1 for c in cats for ch in c.get("channels", []) if ch.get("resolved_url"))
    elif section_key == "movies":
        resolved = sum(1 for c in cats for m in c.get("items", []) if m.get("resolved_url"))
    else:
        resolved = sum(1 for c in cats for s in c.get("items", [])
                       if any(se.get("resolved_ep_{}".format(ep)) for se in s.get("seasons", []) for ep in se.get("episodes", [])))
    grand_total = sec.get("grand_total", 0)
    if grand_total == 0:
        return "Not fetched"
    return "{}/{} {}".format(resolved, grand_total, item_label)


def resolved_counts(json_mgr):
    """Return (live_channels, movies, series) with resolved URLs."""
    live = sum(1 for c in json_mgr.data["live"].get("categories", [])
               for ch in c.get("channels", []) if ch.get("resolved_url"))
    movies = sum(1 for c in json_mgr.data["movies"].get("categories", [])
                 for m in c.get("items", []) if m.get("resolved_url"))
    series = sum(1 for c in json_mgr.data["series"].get("categories", [])
                 for s in c.get("items", [])
                 if any(se.get("resolved_ep_{}".format(ep)) for se in s.get("seasons", []) for ep in se.get("episodes", [])))
    return live, movies, series


def step_progress(json_mgr, code):
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


# ============================================================
# AUTO FETCH STEP  (silent, returns (success, msg))
# ============================================================
def run_auto_fetch_step(client, json_mgr, step_code, step_desc, probe_progress=None):
    """Run a single auto step (STEP_PARAMS). Returns (success: bool, msg: str)."""
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
            probe_categories(client, json_mgr, "live", progress=probe_progress)
        elif step_code == "D1":
            cats = js if isinstance(js, list) else (js.get("data", []) if isinstance(js, dict) else [])
            json_mgr.update_movie_categories(cats)
            probe_categories(client, json_mgr, "movies", progress=probe_progress)
        elif step_code == "E1":
            cats = js if isinstance(js, list) else (js.get("data", []) if isinstance(js, dict) else [])
            json_mgr.update_series_categories(cats)
            probe_categories(client, json_mgr, "series", progress=probe_progress)
    return True, msg


# ============================================================
# UNLOCK  (F3)
# ============================================================
def unlock(client, cache):
    """Try PINs 0000/1234/3333. Returns (True, msg)."""
    pins = ["0000", "1234", "3333"]
    unlocked = False
    msg = ""
    data = None
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
            from .config import DATA_DIR
            os.makedirs(DATA_DIR, exist_ok=True)
            filename = os.path.join(DATA_DIR, "F3_type_itv_action_set_parental_lock_ERROR.json")
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(error_data, f, indent=2, ensure_ascii=False)
        msg += "\n  -> Saved error to {}".format(filename)
        return True, msg