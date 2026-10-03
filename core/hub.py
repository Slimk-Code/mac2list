# ============================================================
# HUB WORK  —  pure logic, no print/input/screen code.
# UI shells (Advanced, Simple) render progress through callbacks
# and own every screen, prompt and cooldown.
# ============================================================
import copy
import json
import os

from .config import SESSION_DIR
from .engine import get_step_info, run_auto_fetch_step
from .library import (
    fetch_all_movies_no_viewer,
    fetch_all_live_no_viewer,
    resolve_all_no_viewer,
)
from .sessions import make_session_id, register_session as _register_session
from .status import session_meta
from .storage import (
    handle_fetch_result,
    save_json,
    save_error_json,
    save_step_outcome,
)

ROW_KEYS = {"C5": "fetch_live", "C4": "resolve_live",
            "D4": "fetch_movies", "D3": "resolve_movies",
            "CHK": "check"}

CONTENT_CODES = {"C2", "D1", "E1", "C5", "D4", "E5", "C4", "D3", "E4", "E3"}

# Health keys persisted after a transient run. Everything else from the
# run stays in memory only, so the next transient open starts from 0 again.
HEALTH_KEYS = ("check_alive", "check_total", "check_live_alive",
               "check_vod_alive", "check_status", "check_reason",
               "handshake_status", "handshake_reason")

# Transient scrape failure reason for the current click.
# Reset once per hub pass, persisted after both C2 and D1 finish.
scrape_fail_reason = ""


def reset_scrape_fail_reason():
    """Clear the transient scrape failure reason for a new hub pass."""
    global scrape_fail_reason
    scrape_fail_reason = ""


def handshake_reason(result):
    """Short failure word for a handshake result: timeout, connection, HTTP code, or no token."""
    err = str(result.get("_error") or "").lower()
    if "timeout" in err or "timed out" in err:
        return "timeout"
    if not result.get("_data"):
        status = result.get("_status")
        if status is None:
            return "connection"
        return "HTTP {}".format(status)
    return "no token"


def fetch_reason(result):
    """Short failure word for a category fetch result."""
    err = str(result.get("_error") or "").lower()
    if "timeout" in err or "timed out" in err:
        return "timeout"
    if not result.get("_data"):
        status = result.get("_status")
        if status is None:
            return "connection"
        return "HTTP {}".format(status)
    return "no channel"


def handshake_status(json_mgr):
    """Hub row status: success, failed - xxx, or - when never run."""
    meta = json_mgr.data.get("_meta", {})
    outcome = meta.get("handshake_status", "")
    if outcome == "pass":
        return "success"
    if outcome == "failed":
        return "failed - {}".format(meta.get("handshake_reason", "unknown"))
    return "-"


def run_handshake_step(client, json_mgr):
    """Run portal handshake. Returns (success, message). No screen code."""
    result = client.handshake()
    cache = getattr(json_mgr, "cache", None)
    meta = json_mgr.data.setdefault("_meta", {})
    if not client.token:
        fname = save_error_json("A1", "handshake", result.get("_status"), result.get("_url"),
                                result.get("_error"), result.get("_lockedpath"), cache=cache)
        meta["handshake_status"] = "failed"
        meta["handshake_reason"] = handshake_reason(result)
        json_mgr.save()
        return False, "  -> [!] Handshake failed — no token. Saved error to {}".format(fname)
    save_json(result.get("_data"), "A1", "handshake", cache=cache)
    meta["handshake_status"] = "pass"
    meta.pop("handshake_reason", None)
    json_mgr.save()
    return True, "  -> [OK] Handshake OK — token received."


def run_category_scrape_no_probe(client, json_mgr, code, desc):
    """Fetch category list only, no per-category first-page check.
    Returns (success, message). No screen code."""
    from .config import STEP_PARAMS

    global scrape_fail_reason
    params = STEP_PARAMS.get(code)
    if not params:
        return False, ""
    result = client.fetch(params)
    safe_name = desc.replace("=", "_").replace("&", "_").replace(" ", "_")[:40]
    cache = getattr(json_mgr, "cache", None)
    fname, status_str, is_error, is_200 = handle_fetch_result(result, code, safe_name, cache=cache)
    if is_error:
        scrape_fail_reason = handshake_reason(result)
        save_step_outcome(json_mgr,
                           "scrape_live" if code == "C2" else "scrape_movies",
                           False, scrape_fail_reason)
        return False, "  -> [!] Failed — saved error to {}".format(fname)
    msg = "  -> [OK] Saved to {}".format(fname)
    data = result.get("_data")
    if data and isinstance(data, dict):
        js = data.get("js", {})
        if code == "C2":
            cats = js if isinstance(js, list) else (js.get("data", []) if isinstance(js, dict) else [])
            cats = [c for c in cats if str(c.get("id")) != "*"][:50]
            json_mgr.update_live_categories(cats)
        elif code == "D1":
            cats = js if isinstance(js, list) else (js.get("data", []) if isinstance(js, dict) else [])
            cats = [c for c in cats if str(c.get("id")) != "*"][:50]
            json_mgr.update_movie_categories(cats)
    save_step_outcome(json_mgr,
                       "scrape_live" if code == "C2" else "scrape_movies",
                       True)
    return True, msg


def run_step_work(client, json_mgr, code, desc,
                  probe_progress=None, fetch_progress=None,
                  resolve_progress=None, check_progress=None):
    """Execute one hub step with no screen code.

    Returns (success, step_msg, empty). Progress callbacks:
    probe_progress(cur, total), fetch_progress(done, total),
    resolve_progress(done, total, fails), check_progress(opened, total, fails).
    """
    from .health import run_check_links

    if code in ("C2", "D1"):
        success, step_msg = run_category_scrape_no_probe(client, json_mgr, code, desc)
        return success, step_msg, False

    if code in ("C4", "D3"):
        if code == "C4":
            success, empty = resolve_all_no_viewer(
                client, json_mgr, "C4", "live", "channels", "itv", "live",
                progress=resolve_progress)
        else:
            success, empty = resolve_all_no_viewer(
                client, json_mgr, "D3", "movies", "items", "vod", "vod",
                progress=resolve_progress)
        if empty:
            return False, "  No items available. Fetch items first.", True
        return success, "", False

    if code in ("C5", "D4"):
        if code == "C5":
            success = fetch_all_live_no_viewer(client, json_mgr, progress=fetch_progress)
        else:
            success = fetch_all_movies_no_viewer(client, json_mgr, progress=fetch_progress)
        return success, "", False

    if code == "CHK":
        success = run_check_links(client, json_mgr, progress=check_progress)
        return success, "", False

    _, _, _, _, is_auto = get_step_info(code)
    if is_auto:
        def _probe(cur, total):
            if probe_progress:
                probe_progress(cur, total)
        success, step_msg = run_auto_fetch_step(client, json_mgr, code, desc,
                                                probe_progress=_probe)
        return success, step_msg, False
    return False, "", False


def row_counts(json_mgr, code):
    """(done, total) numbers for a fetch/resolve hub row."""
    if code == "C5":
        cats = json_mgr.data["live"].get("categories", [])
        total = len([c for c in cats if str(c.get("id")) != "*"])
        return len(json_mgr.get_live_fetched()), total
    if code == "D4":
        cats = json_mgr.data["movies"].get("categories", [])
        total = len([c for c in cats if str(c.get("id")) != "*"])
        return len(json_mgr.get_movie_fetched()), total
    if code == "C4":
        cats = json_mgr.data["live"].get("categories", [])
        total = sum(1 for c in cats for _ in c.get("channels", []))
        done = sum(1 for c in cats for ch in c.get("channels", []) if ch.get("resolved_url"))
        return done, total
    if code == "D3":
        cats = json_mgr.data["movies"].get("categories", [])
        total = sum(1 for c in cats for _ in c.get("items", []))
        done = sum(1 for c in cats for m in c.get("items", []) if m.get("resolved_url"))
        return done, total
    if code == "CHK":
        meta = json_mgr.data.get("_meta", {})
        try:
            return int(meta.get("check_alive", 0) or 0), int(meta.get("check_total", 0) or 0)
        except (TypeError, ValueError):
            return 0, 0
    return 0, 0


def row_status(json_mgr, code):
    """success plus reason, failed - xxx, or - from the saved step outcome."""
    meta = json_mgr.data.get("_meta", {})
    key = ROW_KEYS.get(code, "")
    if meta.get(key + "_status") == "pass":
        reason = meta.get(key + "_reason", "")
        if reason:
            return "success - {}".format(reason)
        return "success"
    if meta.get(key + "_status") == "failed":
        return "failed - {}".format(meta.get(key + "_reason", "unknown"))
    return "-"


def side_status(json_mgr, scrape_key, fetch_code, resolve_code):
    """success, failed - xxx, or - combined across one side, scrape first."""
    meta = json_mgr.data.get("_meta", {})
    parts = [
        (meta.get(scrape_key + "_status"), meta.get(scrape_key + "_reason", "unknown")),
        (meta.get(ROW_KEYS[fetch_code] + "_status"),
         meta.get(ROW_KEYS[fetch_code] + "_reason", "unknown")),
        (meta.get(ROW_KEYS[resolve_code] + "_status"),
         meta.get(ROW_KEYS[resolve_code] + "_reason", "unknown")),
    ]
    for status, reason in parts:
        if status == "failed":
            return "failed - {}".format(reason or "unknown")
    if all(status == "pass" for status, _ in parts):
        return "success"
    return "-"


def side_label(name, json_mgr, fetch_code, resolve_code):
    """Hub row label with fetch/resolve counts."""
    fetch_done, fetch_total = row_counts(json_mgr, fetch_code)
    resolve_done, resolve_total = row_counts(json_mgr, resolve_code)
    return "{} [ {}/{} Cat - {}/{} Ch ]".format(name, fetch_done, fetch_total,
                                                resolve_done, resolve_total)


def register_new_portal(portal, mac):
    """Register a portal+MAC in the macs[] list."""
    _register_session(portal, mac)


def register_new_xtream(portal, username, password=""):
    """Register an Xtream login in the users[] list."""
    from .sessions import register_xtream as _register_xtream
    _register_xtream(portal, username, password)


def scan_again(session):
    """Second-plus runs: everything reruns except dead handshakes.

    Skipped only when the saved handshake failed with an HTTP 4xx code
    or a connection failure. Timeouts, 5xx, no-token, half-done,
    fully passed and never-scanned portals all run full again."""
    meta = session_meta(session)
    if meta.get("handshake_status") == "failed":
        reason = meta.get("handshake_reason", "") or ""
        if reason == "connection":
            return False
        if reason.startswith("HTTP"):
            try:
                code = int(reason.split()[1])
            except (IndexError, ValueError):
                code = 0
            if 400 <= code <= 499:
                return False
    return True


def clean_hub_memory(json_mgr):
    """Health-only fresh in memory: clear live/vod/series content plus
    content done markers, keep every other _meta value
    (health score, handshake, history). No file writes."""
    for section in ("live", "movies", "series"):
        sec = json_mgr.data.get(section)
        if isinstance(sec, dict):
            sec["categories"] = []
            for key in [k for k in sec
                        if k.startswith(("_remaining", "_fetched", "_failed", "_probed"))]:
                sec[key] = []
    meta = json_mgr.data.get("_meta", {})
    if isinstance(meta, dict):
        done = meta.get("done_steps", [])
        if isinstance(done, list):
            meta["done_steps"] = [c for c in done if c not in CONTENT_CODES]
        ignored = meta.get("ignored_steps", [])
        if isinstance(ignored, list):
            meta["ignored_steps"] = [c for c in ignored if c not in CONTENT_CODES]
        if meta.get("last_step") in CONTENT_CODES:
            meta.pop("last_step", None)
        meta.pop("scraped_at", None)


def fresh_session_file(portal, mac):
    """Health-only clean of the saved file: clear live/vod/series plus
    content done markers, keep _meta health/handshake/history and the
    cache untouched."""
    try:
        session_id = make_session_id(portal, mac)
        spath = os.path.join(SESSION_DIR, session_id + ".json")
        if not os.path.exists(spath):
            return
        with open(spath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        for section in ("live", "movies", "series"):
            sec = data.get(section)
            if isinstance(sec, dict):
                sec["categories"] = []
                for key in [k for k in sec
                            if k.startswith(("_remaining", "_fetched", "_failed", "_probed"))]:
                    sec[key] = []
        meta = data.get("_meta", {})
        if isinstance(meta, dict):
            done = meta.get("done_steps", [])
            if isinstance(done, list):
                meta["done_steps"] = [c for c in done if c not in CONTENT_CODES]
            ignored = meta.get("ignored_steps", [])
            if isinstance(ignored, list):
                meta["ignored_steps"] = [c for c in ignored if c not in CONTENT_CODES]
            if meta.get("last_step") in CONTENT_CODES:
                meta.pop("last_step", None)
            meta.pop("scraped_at", None)
        with open(spath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


OUTCOME_STATUS_KEYS = (
    "handshake_status",
    "scrape_status",
    "scrape_live_status",
    "scrape_movies_status",
    "fetch_live_status",
    "fetch_movies_status",
    "resolve_live_status",
    "resolve_movies_status",
    "check_status",
)

CHECK_COUNT_KEYS = (
    "check_alive",
    "check_total",
    "check_live_alive",
    "check_vod_alive",
)


def clear_outcomes_memory(json_mgr):
    """Drop every outcome status/reason plus check counts in memory only.
    Passes vanish; the next transient open starts blank. No file writes."""
    meta = json_mgr.data.get("_meta", {})
    if not isinstance(meta, dict):
        return
    for key in OUTCOME_STATUS_KEYS:
        meta.pop(key, None)
        meta.pop(key[:-len("_status")] + "_reason", None)
    for key in CHECK_COUNT_KEYS:
        meta.pop(key, None)


def persist_failures_only(portal, mac, post_meta):
    """Rewrite the saved file outcomes from a transient run.

    Strips old scrape/fetch/resolve outcomes, then applies every failed
    outcome (with reasons) plus the check result either way (pass with
    counts, or failed with counts). Handshake state is preserved as-is.
    Content passes are never written, so the next open starts empty while
    health score and errors used by the main list survive. Saved scrape
    content is untouched.
    """
    from .storage import JSONManager

    _STRIP = tuple(k for k in OUTCOME_STATUS_KEYS if not k.startswith("handshake"))
    real = JSONManager(portal, mac)
    rmeta = real.data.setdefault("_meta", {})
    for key in _STRIP:
        rmeta.pop(key, None)
        rmeta.pop(key[:-len("_status")] + "_reason", None)
    for key in CHECK_COUNT_KEYS:
        rmeta.pop(key, None)
    if not isinstance(post_meta, dict):
        real.save()
        return real
    for key in _STRIP:
        if post_meta.get(key) == "failed":
            rmeta[key] = "failed"
            reason = key[:-len("_status")] + "_reason"
            if reason in post_meta:
                rmeta[reason] = copy.deepcopy(post_meta[reason])
    if post_meta.get("check_status") == "pass":
        rmeta["check_status"] = "pass"
        for key in CHECK_COUNT_KEYS:
            if key in post_meta:
                rmeta[key] = copy.deepcopy(post_meta[key])
    elif post_meta.get("check_status") == "failed":
        rmeta["check_status"] = "failed"
        if "check_reason" in post_meta:
            rmeta["check_reason"] = copy.deepcopy(post_meta["check_reason"])
        for key in CHECK_COUNT_KEYS:
            if key in post_meta:
                rmeta[key] = copy.deepcopy(post_meta[key])
    real.save()
    return real


def sync_outer_meta(outer_meta, real_meta):
    """Refresh an open manager's _meta from persisted file meta after [0].

    Drops old content outcomes plus check state, then copies failures and
    the check result either way. Handshake state is preserved as-is.
    """
    _STRIP = tuple(k for k in OUTCOME_STATUS_KEYS if not k.startswith("handshake"))
    for key in _STRIP:
        outer_meta.pop(key, None)
        outer_meta.pop(key[:-len("_status")] + "_reason", None)
    for key in CHECK_COUNT_KEYS:
        outer_meta.pop(key, None)
    if not isinstance(real_meta, dict):
        return outer_meta
    for key in _STRIP:
        if real_meta.get(key) == "failed":
            outer_meta[key] = copy.deepcopy(real_meta[key])
            reason = key[:-len("_status")] + "_reason"
            if reason in real_meta:
                outer_meta[reason] = copy.deepcopy(real_meta[reason])
    if real_meta.get("check_status") in ("pass", "failed"):
        outer_meta["check_status"] = real_meta["check_status"]
        if "check_reason" in real_meta:
            outer_meta["check_reason"] = copy.deepcopy(real_meta["check_reason"])
        for key in CHECK_COUNT_KEYS:
            if key in real_meta:
                outer_meta[key] = copy.deepcopy(real_meta[key])
    return outer_meta


def extract_health(meta):
    """Copy of the health keys from a _meta dict for health-only persist."""
    return {k: copy.deepcopy(meta[k]) for k in HEALTH_KEYS if k in meta}


def apply_health(meta, health):
    """Write persisted health keys into a _meta dict."""
    for key in HEALTH_KEYS:
        if key in health:
            meta[key] = copy.deepcopy(health[key])
