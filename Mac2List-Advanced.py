#!/usr/bin/env python3
"""
mac2list v1.2 — CLI interface.

This file is the ONLY user-facing entry point. All reusable logic lives in the
silent core/ package (no print/input/screen code). This file contains every
screen, menu, progress renderer and the main() entry point.
"""
import json
import os
import re
import shutil
import sys
import time

import requests

from core.config import (
    CACHE_DIR,
    DATABASE_FILE,
    DATA_DIR,
    OUTPUT_DIR,
    SECTIONS,
    SESSION_DIR,
    SETTINGS_STEP_CODES,
    STEP_PARAMS,
)
from core.convert import generate_m3u
from core.engine import (
    get_next_pending_step,
    get_step_info,
    resolved_counts,
    run_auto_fetch_step,
    section_status as _section_status,
    step_progress as _step_progress,
    unlock,
)
from core.fetch import (
    category_status as _category_status,
    fetch_episodes,
    fetch_single_category,
    retry_category_pages,
)
from core.portal import Mac2ListPortal
from core.resolve import (
    resolve_episode,
    resolve_live,
    resolve_vod,
)
from core.sessions import (
    cleanup_orphans as _cleanup_orphans,
    database_sessions as _database_sessions,
    make_session_id,
    register_session as _register_session,
)
from core.storage import (
    JSONManager,
    handle_fetch_result,
    save_error_json,
    save_json,
)
from core.utils import (
    domain_of as _domain_of,
    expiry_label as _expiry_label,
    is_valid_mac,
    time_ago as _time_ago,
)
from core.watch import (
    play_in_vlc,
    resolved_channels as _resolved_channels,
    resolved_movies as _resolved_movies,
    resolved_series as _resolved_series,
    series_episode_counts as _series_episode_counts,
)

# ============================================================
# TERMINAL HELPERS  (interface only)
# ============================================================
def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def _cooldown(seconds=3):
    time.sleep(seconds)


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


def _clear_batch_counter():
    sys.stdout.write(chr(13) + " " * 80 + chr(13))
    sys.stdout.flush()


# ============================================================
# LIVE FIRST PAGE ONLY (interface only, core untouched)
# ============================================================
def channel_name_ok(name):
    """Keep HD/SD names only, drop 4K/8K/UHD/HEVC/FHD names."""
    low = str(name or "").lower()
    for bad in ("4k", "8k", "uhd", "hevc", "fhd"):
        if bad in low:
            return False
    return ("hd" in low) or ("sd" in low)


def channel_name_clean(name):
    """Clean names: no bad tags and no hd/sd, used only to fill the gap."""
    low = str(name or "").lower()
    for bad in ("4k", "8k", "uhd", "hevc", "fhd"):
        if bad in low:
            return False
    return not channel_name_ok(name)


def _save_step_outcome(json_mgr, key, ok, reason=""):
    """Persist pass or failed plus reason for a hub row status."""
    meta = json_mgr.data.setdefault("_meta", {})
    meta[key + "_status"] = "pass" if ok else "failed"
    if reason:
        meta[key + "_reason"] = reason
    else:
        meta.pop(key + "_reason", None)
    json_mgr.save()


def fetch_all_live_no_viewer(client, json_mgr):
    """Fetch 1st page of every pending live category first,
    then filter the entire set at once and keep the first 100."""
    _, pending, _, _ = _category_status(json_mgr, "live")
    if not pending:
        return True
    print()
    # Phase 1: fetch everything into memory, no filtering yet
    collected = []
    fetched_ids = []
    first_error = None
    for i, cat in enumerate(pending):
        cid = str(cat.get("id"))
        params = {"type": "itv", "action": "get_ordered_list", "genre": cid, "p": "1", "JsHttpRequest": "1-xml"}
        result = client.fetch(params)
        data = result.get("_data")
        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                collected.append((cid, js.get("data", [])))
                fetched_ids.append(cid)
            else:
                if first_error is None:
                    first_error = result
                json_mgr.mark_live_genre_failed(cid)
        else:
            if first_error is None:
                first_error = result
            json_mgr.mark_live_genre_failed(cid)
        line = "  -> Fetching: [{}/{}] done".format(i + 1, len(pending))
        sys.stdout.write(chr(13) + line.ljust(80))
        sys.stdout.flush()
        time.sleep(0.1)
    _clear_batch_counter()
    if not fetched_ids:
        _save_step_outcome(json_mgr, "fetch_live",
                           False, fetch_reason(first_error) if first_error is not None else "unknown")
        return False
    # Phase 2: filter the entire set at once, keep first 100
    kept = [(cid, it) for cid, items in collected for it in items
            if channel_name_ok(it.get("name", it.get("title", "")))]
    kept = kept[:100]
    if len(kept) < 100:
        kept_ids = set(id(it) for _, it in kept)
        clean = [(cid, it) for cid, items in collected for it in items
                 if channel_name_clean(it.get("name", it.get("title", "")))
                 and id(it) not in kept_ids]
        kept = kept + clean[:100 - len(kept)]
    # Phase 3: save grouped by category
    by_cat = {}
    for cid, it in kept:
        by_cat.setdefault(cid, []).append(it)
    for cid in fetched_ids:
        subset = by_cat.get(cid, [])
        json_mgr.update_live_channels(cid, subset, len(subset))
    if not kept:
        source_total = sum(len(items) for _, items in collected)
        if source_total == 0:
            _save_step_outcome(json_mgr, "fetch_live", False, "no channel")
        else:
            _save_step_outcome(json_mgr, "fetch_live", False, "empty")
    else:
        _save_step_outcome(json_mgr, "fetch_live", True)
    return True


def movie_title_ok(title):
    """Keep 2010 to 2026 titles only."""
    text = str(title or "")
    for year in range(2010, 2027):
        if str(year) in text:
            return True
    return False


def fetch_all_movies_no_viewer(client, json_mgr):
    """Fetch 1st page of every pending VOD category first,
    then filter the entire set at once and keep the first 100."""
    _, pending, _, _ = _category_status(json_mgr, "movies")
    if not pending:
        return True
    print()
    # Phase 1: fetch everything into memory, no filtering yet
    collected = []
    fetched_ids = []
    first_error = None
    for i, cat in enumerate(pending):
        cid = str(cat.get("id"))
        params = {"type": "vod", "action": "get_ordered_list", "category": cid, "p": "1",
                  "fav": "0", "sortby": "added", "hd": "0", "JsHttpRequest": "1-xml"}
        result = client.fetch(params)
        data = result.get("_data")
        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                collected.append((cid, js.get("data", [])))
                fetched_ids.append(cid)
            else:
                if first_error is None:
                    first_error = result
                json_mgr.mark_movie_category_failed(cid)
        else:
            if first_error is None:
                first_error = result
            json_mgr.mark_movie_category_failed(cid)
        line = "  -> Fetching: [{}/{}] done".format(i + 1, len(pending))
        sys.stdout.write(chr(13) + line.ljust(80))
        sys.stdout.flush()
        time.sleep(0.1)
    _clear_batch_counter()
    if not fetched_ids:
        _save_step_outcome(json_mgr, "fetch_movies",
                           False, fetch_reason(first_error) if first_error is not None else "unknown")
        return False
    # Phase 2: filter the entire set at once, keep first 100
    kept = [(cid, it) for cid, items in collected for it in items
            if movie_title_ok(it.get("name", it.get("title", "")))]
    kept = kept[:100]
    if len(kept) < 100:
        kept_ids = set(id(it) for _, it in kept)
        clean = [(cid, it) for cid, items in collected for it in items
                 if not movie_title_ok(it.get("name", it.get("title", "")))
                 and id(it) not in kept_ids]
        kept = kept + clean[:100 - len(kept)]
    # Phase 3: save grouped by category
    by_cat = {}
    for cid, it in kept:
        by_cat.setdefault(cid, []).append(it)
    for cid in fetched_ids:
        subset = by_cat.get(cid, [])
        json_mgr.update_movie_items(cid, subset, len(subset))
    if not kept:
        source_total = sum(len(items) for _, items in collected)
        if source_total == 0:
            _save_step_outcome(json_mgr, "fetch_movies", False, "no channel")
        else:
            _save_step_outcome(json_mgr, "fetch_movies", False, "empty")
    else:
        _save_step_outcome(json_mgr, "fetch_movies", True)
    return True


# ============================================================
# RESOLVE ALL + SAVE M3U (interface only, core untouched)
# ============================================================
def save_section_m3u(json_mgr, section, folder):
    """Write one section m3u straight into its folder. No other files created."""
    session_id = json_mgr.cache.session_id
    out_dir = os.path.join(OUTPUT_DIR, "dead", session_id, folder)
    os.makedirs(out_dir, exist_ok=True)
    lines = ["#EXTM3U"]
    if section == "live":
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
        path = os.path.join(out_dir, "{}_LIVE.m3u".format(session_id))
    else:
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
        path = os.path.join(out_dir, "{}_MOVIE.m3u".format(session_id))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def resolve_all_no_viewer(client, json_mgr, step_code, section, bucket, action_type, folder):
    """Resolve every pending item at once, no picker, then save section m3u to its folder."""
    items = []
    for cat in json_mgr.data[section].get("categories", []):
        for it in cat.get(bucket, []):
            items.append(it)
    if not items:
        print("  No items available. Fetch items first.")
        _cooldown()
        return False

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

    pending = [it for it in items if not it.get("resolved_url")]
    print()
    total = len(pending)
    fail_count = 0
    nocmd_count = 0
    for i, item in enumerate(pending):
        if not item.get("cmd"):
            nocmd_count += 1
            fail_count += 1
        elif action_type == "itv":
            resolved_url, raw = resolve_live(client, json_mgr, item)
            existing_responses.append({
                "id": item.get("id", ""),
                "name": item.get("name", item.get("title", "")),
                "raw_response": raw if raw else item.get("cmd", "")
            })
            if not resolved_url:
                fail_count += 1
        else:
            resolved_url, raw = resolve_vod(client, json_mgr, item)
            if resolved_url:
                existing_responses.append({
                    "id": item.get("id", ""),
                    "name": item.get("name", item.get("title", "")),
                    "raw_response": raw
                })
            else:
                fail_count += 1
        line = "  -> Resolving: [{}/{}]".format(i + 1, total)
        if fail_count:
            line += "  |  {} failed".format(fail_count)
        sys.stdout.write(chr(13) + line.ljust(80))
        sys.stdout.flush()
        time.sleep(0.3)
    _clear_batch_counter()
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

    save_section_m3u(json_mgr, section, folder)
    rkey = "resolve_live" if section == "live" else "resolve_movies"
    dead = fail_count - nocmd_count
    live = total - fail_count
    if total == 0:
        _save_step_outcome(json_mgr, rkey, True)
    elif live > 0 and dead > 0:
        _save_step_outcome(json_mgr, rkey, True, "{}%".format(int(live * 100 / total)))
    elif dead > 0:
        _save_step_outcome(json_mgr, rkey, False, "{} dead".format(dead))
    elif nocmd_count > 0:
        _save_step_outcome(json_mgr, rkey, False, "no cmd")
    else:
        _save_step_outcome(json_mgr, rkey, True)
    return True


# ============================================================
# PAGE 1 — RESTORE SESSION VIEWER (paged, pick one)
# ============================================================
def _select_paginated(items, title, header_line, row_fmt_fn, page_size=20):
    """Paged browse where choosing a number returns that item, or None on Back.
    Rows are numbered with their real position in the list (like the resolver)."""
    total = len(items)
    page = 0
    max_page = (total - 1) // page_size

    while True:
        clear_screen()
        print("=" * 60)
        print("   {} — Page {}/{} — {} saved".format(title, page + 1, max_page + 1, total))
        print("=" * 60)
        print()

        print(header_line)
        print("  " + "-" * 64)

        start = page * page_size
        end = min(start + page_size, total)
        for i in range(start, end):
            print(row_fmt_fn(items[i], i + 1))

        print()
        if max_page > 0:
            print("  [Enter] Next page  |  [1-{}] Single Scan  |  [F] Full Scan  |  [B] Back".format(total))
        else:
            print("  [1-{}] Single Scan  |  [F] Full Scan  |  [B] Back".format(total))

        choice = input("  > ").strip().upper()
        if choice == "B":
            return None
        elif choice == "F":
            return "FULL"
        elif choice == "" and max_page > 0:
            page = (page + 1) % (max_page + 1)
        elif choice.isdigit():
            num = int(choice)
            if 1 <= num <= total:
                picked = items[num - 1]
                return picked


def _session_data(session):
    """Full saved session dict for a portal entry, {} when missing."""
    try:
        session_id = make_session_id(session.get("portal", ""), session.get("mac", ""))
        path = os.path.join(SESSION_DIR, session_id + ".json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _session_meta(session):
    """Saved session meta dict for a portal entry, {} when missing."""
    meta = _session_data(session).get("_meta", {})
    return meta if isinstance(meta, dict) else {}


def _portal_status(session):
    """HTTP first, checker share next, softer failures after, fresh states last."""
    data = _session_data(session)
    meta = data.get("_meta", {})
    if not isinstance(meta, dict):
        meta = {}
    if meta.get("handshake_status") == "failed":
        return "Handshake - {}".format(meta.get("handshake_reason", "unknown"))

    if meta.get("scrape_status") == "failed" and meta.get("scrape_reason") == "no category":
        return "No category"

    reasons = []
    for key in ("scrape_live", "fetch_live", "resolve_live",
                "scrape_movies", "fetch_movies", "resolve_movies",
                "check"):
        if meta.get(key + "_status") == "failed":
            reasons.append(meta.get(key + "_reason", "unknown") or "unknown")
    for reason in reasons:
        if reason.startswith("HTTP"):
            return reason

    try:
        check_alive = int(meta.get("check_alive", 0) or 0)
    except (TypeError, ValueError):
        check_alive = 0
    try:
        check_total = int(meta.get("check_total", 0) or 0)
    except (TypeError, ValueError):
        check_total = 0
    try:
        check_live_alive = int(meta.get("check_live_alive", 0) or 0)
    except (TypeError, ValueError):
        check_live_alive = 0
    try:
        check_vod_alive = int(meta.get("check_vod_alive", 0) or 0)
    except (TypeError, ValueError):
        check_vod_alive = 0
    if check_live_alive >= 50 and check_vod_alive >= 50:
        return "health score - 100%"
    if check_alive > 0 and check_total > 0:
        return "health score - {}%".format(int(check_alive * 100 / check_total))
    if "timeout" in reasons:
        return "timeout"
    if "no channel" in reasons:
        return "No channel"
    if reasons:
        return "Broken data"
    ran = [meta.get("handshake_status"), meta.get("scrape_status"),
           meta.get("fetch_live_status"), meta.get("fetch_movies_status"),
           meta.get("resolve_live_status"), meta.get("resolve_movies_status"),
           meta.get("check_status")]
    if any(r in ("pass", "failed") for r in ran):
        return "No Data"
    return "-"


def _portal_rank(session):
    """Sort key follows the displayed status: fresh, health 100,
    health percent desc, HTTP asc, timeout, no channel, No category,
    Broken data, No Data, handshake errors last. All MACs equal."""
    status = _portal_status(session)
    if status == "-":
        return (0, 0, 0)
    if status == "success" or status == "health score - 100%":
        return (1, 0, 0)
    if status.startswith("health score - "):
        try:
            pct = int(status.rsplit("-", 1)[1].strip().rstrip("%"))
        except (IndexError, ValueError):
            pct = 0
        return (2, -pct, 0)
    if status.startswith("HTTP"):
        try:
            code = int(status.split()[1])
        except (IndexError, ValueError):
            code = 999
        return (3, code, 0)
    if status == "timeout":
        return (4, 0, 0)
    if status == "No channel":
        return (5, 0, 0)
    if status == "No category":
        return (6, 0, 0)
    if status == "Broken data":
        return (7, 0, 0)
    if status == "No Data":
        return (8, 0, 0)
    return (9, 0, 0)


def _select_restore_session(sessions):
    """Show all saved sessions in a paged list. Returns chosen session dict or None."""
    if not sessions:
        print("  No saved sessions.")
        _cooldown()
        return None

    header_line = "  {:<4} {:<24} {:<19} {}".format("#", "Portal", "MAC", "Status")

    def row_fmt(s, idx):
        portal = _domain_of(s.get("portal", ""))[:24]
        mac = str(s.get("mac", ""))[:19]
        status = _portal_status(s)
        return "  {:<4} {:<24} {:<19} {}".format(idx, portal, mac, status)

    sessions = sorted(sessions, key=_portal_rank)
    picked = _select_paginated(sessions, "mac2list Scanner v1.2", header_line, row_fmt)
    return picked, sessions


# ============================================================
# TOP MENU — New session first, Restore second
# ============================================================
def _paint_top_menu(sessions):
    """Top screen paint without input."""
    clear_screen()
    print("=" * 60)
    print("   mac2list v1.2")
    print("=" * 60)
    print()
    print("  [1] New session")
    print()
    if sessions:
        print("  [2] Restore session")
        print("      {} saved".format(len(sessions)))
        print()
    print("  [Q] Quit")
    print()


def show_top_menu(sessions):
    """Top screen mirroring the portal landing. Returns 1, 2 or Q."""
    _paint_top_menu(sessions)
    return input("  > ").strip().upper()


def _register_new_portal(portal, mac):
    """Register a fresh portal with its first MAC active.

    register_session files newcomers under pending_macs, matching the
    conversion rule that only the first MAC is active. A group created
    by this very call therefore gets its MAC moved to active_mac, so a
    brand-new portal never shows up locked."""
    try:
        with open(DATABASE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        known = isinstance(data, dict) and any(
            isinstance(g, dict) and g.get("portal") == portal
            for g in data.get("portals", []) or [])
    except Exception:
        known = False
    _register_session(portal, mac)
    if known:
        return
    try:
        with open(DATABASE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        for group in data.get("portals", []) or []:
            if isinstance(group, dict) and group.get("portal") == portal:
                if not group.get("active_mac"):
                    pending = group.get("pending_macs") or []
                    if mac in pending:
                        pending.remove(mac)
                    group["active_mac"] = mac
                    group["pending_macs"] = pending
                break
        with open(DATABASE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def run_resume_or_new():
    """Scanner viewer loop. Returns (mode, payload). ONE: (portal, mac, idx,
    total). FULL: sorted sessions list. BACK: viewer quit. EMPTY: None."""
    while True:
        sessions = _database_sessions()
        _cleanup_orphans(sessions)
        if not sessions:
            clear_screen()
            print("=" * 60)
            print("   mac2list Scanner v1.2")
            print("=" * 60)
            print()
            print("  No saved sessions.")
            _cooldown()
            return "EMPTY", None

        picked, sessions = _select_restore_session(sessions)
        if picked is None:
            return "BACK", None
        if picked == "FULL":
            return "FULL", sessions

        idx = sessions.index(picked) + 1
        return "ONE", (picked["portal"], picked["mac"], idx, len(sessions))


# ============================================================
# PAGE 2 — MAIN HUB
# ============================================================
# Sequential hub order: one Enter runs the full order automatically.
_ORDER = ["A1", "LIVE", "VOD", "CHECK"]
_hub_pos = 0


def run_hub_handshake(client, json_mgr):
    """Three screens: work, fresh menu plus pause, menu plus saved line."""
    _, _, desc, _, _ = get_step_info("A1")
    print()
    print("  Executing: A1 — {}".format(desc))
    print()
    success, _ = run_handshake_step(client, json_mgr)
    if success:
        json_mgr.mark_done("A1")
    time.sleep(3)
    print()
    print("  -> session saved")
    time.sleep(3)
    return success


_CHECK_TIMEOUT = 10


def _check_link(url):
    """Open one collected link, read the first chunk only.
    Returns (alive_bool, reason)."""
    try:
        resp = requests.get(url, timeout=_CHECK_TIMEOUT, stream=True,
                            allow_redirects=True)
    except Exception as e:
        msg = str(e).lower()
        if "timeout" in msg or "timed out" in msg:
            return False, "timeout"
        return False, "connection"
    try:
        if resp.status_code != 200:
            return False, "HTTP {}".format(resp.status_code)
        try:
            chunk = next(resp.iter_content(chunk_size=32768), b"")
        except Exception:
            return False, "no-data"
        if chunk:
            return True, ""
        return False, "no-data"
    finally:
        try:
            resp.close()
        except Exception:
            pass


def run_check_links(client, json_mgr):
    """Check m3u links per side up to 50 alive each, persist alive shares."""
    session_id = json_mgr.cache.session_id
    sides = {}
    for folder in ("live", "vod"):
        folder_path = os.path.join(OUTPUT_DIR, "dead", session_id, folder)
        folder_urls = []
        if os.path.isdir(folder_path):
            for name in sorted(os.listdir(folder_path)):
                if not name.endswith(".m3u"):
                    continue
                with open(os.path.join(folder_path, name),
                          encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            folder_urls.append(line)
        sides[folder] = folder_urls
    total = len(sides["live"]) + len(sides["vod"])
    meta = json_mgr.data.setdefault("_meta", {})
    if total == 0:
        meta["check_alive"] = 0
        meta["check_total"] = 0
        _save_step_outcome(json_mgr, "check", False, "empty")
        return False
    print()
    alive = 0
    fail_count = 0
    first_err = ""
    side_alive = {"live": 0, "vod": 0}
    opened = 0
    for folder in ("live", "vod"):
        for url in sides[folder]:
            if side_alive[folder] >= 50:
                continue
            opened += 1
            ok, err = _check_link(url)
            if ok:
                alive += 1
                side_alive[folder] += 1
            else:
                fail_count += 1
                if not first_err:
                    first_err = err or "unknown"
            line = "  -> Checking: [{}/{}]".format(opened, total)
            if fail_count:
                line += "  |  {} failed".format(fail_count)
            sys.stdout.write(chr(13) + line.ljust(80))
            sys.stdout.flush()
            time.sleep(0.5)
    _clear_batch_counter()
    meta["check_alive"] = alive
    meta["check_total"] = total
    meta["check_live_alive"] = side_alive["live"]
    meta["check_vod_alive"] = side_alive["vod"]
    if alive == 0:
        _save_step_outcome(json_mgr, "check", False, first_err)
    else:
        _save_step_outcome(json_mgr, "check", True)
        src_root = os.path.join(OUTPUT_DIR, "dead", session_id)
        dst_root = os.path.join(OUTPUT_DIR, "success", session_id)
        if os.path.isdir(dst_root):
            shutil.rmtree(dst_root)
        if os.path.isdir(src_root):
            shutil.move(src_root, dst_root)
    return alive > 0


_ROW_KEYS = {"C5": "fetch_live", "C4": "resolve_live",
             "D4": "fetch_movies", "D3": "resolve_movies",
             "CHK": "check"}


def _row_counts(json_mgr, code):
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


def _row_status(json_mgr, code):
    """success plus reason, failed - xxx, or - from the saved step outcome."""
    meta = json_mgr.data.get("_meta", {})
    key = _ROW_KEYS.get(code, "")
    if meta.get(key + "_status") == "pass":
        reason = meta.get(key + "_reason", "")
        if reason:
            return "success - {}".format(reason)
        return "success"
    if meta.get(key + "_status") == "failed":
        return "failed - {}".format(meta.get(key + "_reason", "unknown"))
    return "-"


def _side_status(json_mgr, scrape_key, fetch_code, resolve_code):
    """success, failed - xxx, or - combined across one side, scrape first."""
    meta = json_mgr.data.get("_meta", {})
    parts = [
        (meta.get(scrape_key + "_status"), meta.get(scrape_key + "_reason", "unknown")),
        (meta.get(_ROW_KEYS[fetch_code] + "_status"),
         meta.get(_ROW_KEYS[fetch_code] + "_reason", "unknown")),
        (meta.get(_ROW_KEYS[resolve_code] + "_status"),
         meta.get(_ROW_KEYS[resolve_code] + "_reason", "unknown")),
    ]
    for status, reason in parts:
        if status == "failed":
            return "failed - {}".format(reason or "unknown")
    if all(status == "pass" for status, _ in parts):
        return "success"
    return "-"


def _side_label(name, json_mgr, fetch_code, resolve_code):
    fetch_done, fetch_total = _row_counts(json_mgr, fetch_code)
    resolve_done, resolve_total = _row_counts(json_mgr, resolve_code)
    return "{} [ {}/{} Cat - {}/{} Ch ]".format(name, fetch_done, fetch_total,
                                               resolve_done, resolve_total)


def show_hub_header(json_mgr):
    """Print hub header/menu without input prompt."""
    clear_screen()
    print("=" * 60)
    meta = json_mgr.data.get("_meta", {})
    print("   mac2list Scanner v1.2 — {} — {}".format(
        _domain_of(meta.get("portal", "")) or "Main Hub", meta.get("mac", "")))
    print("=" * 60)
    print()

    _cred = json_mgr.data.get("_meta", {})
    _cred_text = "{} — {}".format(_domain_of(_cred.get("portal", "")) or "-",
                                  _cred.get("mac", "") or "-")
    print("  {} {:<45} {}".format("  ", "Credential", _cred_text))
    print()
    print("  {} {:<45} {}".format(">>" if _hub_pos == 0 else "  ", "handshake", handshake_status(json_mgr)))
    print()
    print("  {} {:<45} {}".format(">>" if _hub_pos == 1 else "  ", _side_label("Channels", json_mgr, "C5", "C4"), _side_status(json_mgr, "scrape_live", "C5", "C4")))
    print()
    print("  {} {:<45} {}".format(">>" if _hub_pos == 2 else "  ", _side_label("Vod", json_mgr, "D4", "D3"), _side_status(json_mgr, "scrape_movies", "D4", "D3")))
    print()
    print("  {} {:<45} {}".format(">>" if _hub_pos == 3 else "  ", "Health Checker ({}/{})".format(*_row_counts(json_mgr, "CHK")), _row_status(json_mgr, "CHK")))
    print()
    print("-" * 60)
    if _hub_full:
        print("  {} out of {}".format(_hub_title_idx, _hub_title_total))
    else:
        print("  [Enter] Start | [B] Back")


def show_hub(json_mgr):
    """Display Main Hub. Returns user choice string."""
    show_hub_header(json_mgr)
    print()
    return input("  > ").strip().upper()


def hub_loop(client, json_mgr, is_restored):
    """Main Hub loop: one Enter runs the full order automatically."""
    global _hub_pos, _scrape_fail_reason
    _hub_pos = 0
    if not _hub_full:
        while True:
            choice = show_hub(json_mgr)
            if choice == "B":
                return
            if choice != "":
                continue
            break
    for pos, code in enumerate(_ORDER):
        _hub_pos = pos
        ok = True
        if code == "LIVE":
            cat_codes = ["C2", "D1"]
            # Always reset so it scrapes again at once
            for c in cat_codes:
                if json_mgr.is_done(c):
                    done = json_mgr.data["_meta"].get("done_steps", [])
                    if c in done:
                        done.remove(c)
                        json_mgr.data["_meta"]["done_steps"] = done
            json_mgr.data["_meta"]["scraped_at"] = ""
            json_mgr.save()
            _scrape_fail_reason = ""
            for next_code in ("C2", "C5", "C4"):
                show_hub_header(json_mgr)
                print()
                print("  > ")
                idx, _, desc, info, is_auto = get_step_info(next_code)
                run_single_step(client, json_mgr, next_code, desc, info, is_auto)
        elif code == "VOD":
            show_hub_header(json_mgr)
            print()
            print("  > ")
            idx, _, desc, info, is_auto = get_step_info("D1")
            run_single_step(client, json_mgr, "D1", desc, info, is_auto)
            meta = json_mgr.data.setdefault("_meta", {})
            if _scrape_fail_reason:
                meta["scrape_status"] = "failed"
                meta["scrape_reason"] = _scrape_fail_reason
            elif (len(json_mgr.data.get("live", {}).get("categories", [])) == 0
                    and len(json_mgr.data.get("movies", {}).get("categories", [])) == 0):
                meta["scrape_status"] = "failed"
                meta["scrape_reason"] = "no category"
            else:
                meta["scrape_status"] = "pass"
                meta.pop("scrape_reason", None)
            json_mgr.save()
            both_zero = (len(json_mgr.data.get("live", {}).get("categories", [])) == 0
                         and len(json_mgr.data.get("movies", {}).get("categories", [])) == 0)
            if both_zero:
                ok = False
            else:
                show_hub_header(json_mgr)
                print()
                print("  > ")
                idx, _, desc, info, is_auto = get_step_info("D4")
                run_single_step(client, json_mgr, "D4", desc, info, is_auto)
                show_hub_header(json_mgr)
                print()
                print("  > ")
                idx, _, desc, info, is_auto = get_step_info("D3")
                run_single_step(client, json_mgr, "D3", desc, info, is_auto)
        elif code == "CHECK":
            show_hub_header(json_mgr)
            print()
            print("  > ")
            ok = run_single_step(client, json_mgr, "CHK", "Health Checker", "", False)
        else:
            show_hub_header(json_mgr)
            print()
            print("  > ")
            idx, _, desc, info, is_auto = get_step_info(code)
            ok = run_single_step(client, json_mgr, code, desc, info, is_auto)
        if not ok:
            time.sleep(3)
            break


# ============================================================
# HANDSHAKE FROM HUB (interface only)
# ============================================================
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
    """Run portal handshake again from the hub."""
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


# ============================================================
# CATEGORY SCRAPE WITHOUT FIRST PAGE (interface only)
# ============================================================
# Transient scrape failure reason for the current click.
# Persisted once, after both C2 and D1 finish.
_scrape_fail_reason = ""


def run_category_scrape_no_probe(client, json_mgr, code, desc):
    """Fetch category list only, no per-category first-page check."""
    global _scrape_fail_reason
    params = STEP_PARAMS.get(code)
    if not params:
        return False, ""
    result = client.fetch(params)
    safe_name = desc.replace("=", "_").replace("&", "_").replace(" ", "_")[:40]
    cache = getattr(json_mgr, "cache", None)
    fname, status_str, is_error, is_200 = handle_fetch_result(result, code, safe_name, cache=cache)
    if is_error:
        _scrape_fail_reason = handshake_reason(result)
        _save_step_outcome(json_mgr,
                           "scrape_live" if code == "C2" else "scrape_movies",
                           False, _scrape_fail_reason)
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
    _save_step_outcome(json_mgr,
                       "scrape_live" if code == "C2" else "scrape_movies",
                       True)
    return True, msg


# ============================================================
# SINGLE STEP EXECUTOR (no prompts, returns to caller)
# ============================================================
def run_single_step(client, json_mgr, code, desc, info, is_auto):
    """Execute a single step. Returns to caller when done."""
    if code == "A1":
        return run_hub_handshake(client, json_mgr)
    print()
    print("  Executing: {} — {}".format(code, desc))
    print()

    success = False
    step_msg = ""

    if is_auto:
        if code in ("C2", "D1"):
            success, step_msg = run_category_scrape_no_probe(client, json_mgr, code, desc)
        else:
            def _probe_bar(cur, total):
                progress_bar(cur, total, prefix="  Loading Categories: ")
            success, step_msg = run_auto_fetch_step(client, json_mgr, code, desc, probe_progress=_probe_bar)
    elif code in ("C4", "D3"):
        if code == "C4":
            success = resolve_all_no_viewer(client, json_mgr, "C4", "live", "channels", "itv", "live")
        else:
            success = resolve_all_no_viewer(client, json_mgr, "D3", "movies", "items", "vod", "vod")
    elif code in ("C5", "D4"):
        if code == "C5":
            success = fetch_all_live_no_viewer(client, json_mgr)
        elif code == "D4":
            success = fetch_all_movies_no_viewer(client, json_mgr)
    elif code == "CHK":
        success = run_check_links(client, json_mgr)

    time.sleep(3)
    print()
    if success:
        json_mgr.mark_done(code)
    print("  -> session saved")
    print()
    _cooldown()
    return success


# ============================================================
# PORTAL SCREENS (inlined from mac2list-portal.py, prefixed)
# ============================================================
def portal_clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def portal__cooldown(seconds=5):
    for i in range(seconds, 0, -1):
        sys.stdout.write('\r  Continuing in {}s...  '.format(i))
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write('\r' + ' ' * 40 + '\r')
    sys.stdout.flush()

def portal_progress_bar(current, total, prefix='', width=30):
    if total <= 0:
        pct = 100.0
        filled = width
    else:
        pct = current / total * 100
        filled = int(width * current / total)
    bar = '=' * filled + '-' * (width - filled)
    line = '{}[{}] {:5.1f}% ({}/{})'.format(prefix, bar, pct, current, total)
    sys.stdout.write(chr(13) + line.ljust(80))
    sys.stdout.flush()
    if current >= total:
        print()

def portal__clear_batch_counter():
    sys.stdout.write(chr(13) + ' ' * 80 + chr(13))
    sys.stdout.flush()

def portal__parse_numbers(choice, total):
    """Parse a comma/space separated list of row numbers (1..total)."""
    nums = []
    for part in re.split('[,\\s]+', choice):
        part = part.strip()
        if part.isdigit():
            n = int(part)
            if 1 <= n <= total:
                nums.append(n)
    return nums

def portal__paged_picker(title, prepare, name_fn, right_fn, right_label, all_done_line, action_word, divider_label=None, with_failed=False, id_fn=None, page_size=20, start_page=0, header_total_fn=None):
    """Interactive paginated pending-first picker, shared by menus 1-4.

    prepare() -> (items, pending_count, fetched_count); items must already be
    ordered pending-first. Returns (result, end_page) where result is
    "all", "done", None (back), or the list of selected items."""
    if id_fn is None:
        id_fn = lambda it: str(it.get('id', ''))
    page = start_page
    while True:
        items, total_pending, fetched_count = prepare()
        total = len(items)
        max_page = (total - 1) // page_size
        portal_clear_screen()
        print('=' * 60)
        hdr_total = header_total_fn() if header_total_fn else total
        print('   {} — Page {}/{} — {} of {} pending'.format(title, page + 1, max_page + 1, total_pending, hdr_total))
        print('=' * 60)
        print()
        print('  {:<4} {:<8} {:<40} {:<10}'.format('#', 'Status', 'Name', right_label))
        print('  ' + '-' * 64)
        start = page * page_size
        end = min(start + page_size, total)
        shown_divider = False
        for i in range(start, end):
            item = items[i]
            name = name_fn(item)[:38]
            if i < total_pending:
                status = '[]'
            elif i < total_pending + fetched_count:
                if with_failed:
                    status = '[x]'
                else:
                    if not shown_divider and divider_label:
                        print('\n  --- {} ({}) ---\n'.format(divider_label, fetched_count))
                        shown_divider = True
                    status = '[x]'
            else:
                status = '[!]'
            print('  {:<4} {:<8} {:<40} {:<10}'.format(i + 1, status, name, right_fn(item)))
        print()
        if total_pending == 0:
            print('  -> [OK] ' + all_done_line(total, fetched_count))
            print()
            print('  [Enter] Next page  |  [B] Back')
        else:
            if with_failed:
                help_line = '  [Enter] Next page  |  [A] {} ALL  |  [1-{}] Select #  |  [B] Back'.format(action_word, end - start)
            else:
                help_line = '  [A] {} ALL  |  [Enter] Next page  |  [1-{}] Select #  |  [B] Back'.format(action_word, end - start)
            print(help_line)
        choice = input('  > ').strip().upper()
        if choice == 'A':
            if total_pending > 0:
                return ('all', page)
        elif choice == 'B':
            return ('done' if total_pending == 0 else None, page)
        elif choice == '':
            page = page + 1 if page < max_page else 0
        else:
            nums = portal__parse_numbers(choice, total)
            if nums and total_pending > 0:
                seen = set()
                selection = []
                for n in nums:
                    item = items[n - 1]
                    key = id_fn(item)
                    if key not in seen:
                        selection.append(item)
                        seen.add(key)
                return (selection, page)

def portal_view_categories(json_mgr, section, client=None):
    """Paginated category viewer showing ONLY pending categories. 20 per page.
    Returns list of category IDs to fetch, "done", or None if back to menu."""
    if section not in ('live', 'movies', 'series'):
        return []
    title = {'live': 'Fetch Channels', 'movies': 'VOD Categories', 'series': 'Series Categories'}[section]
    all_cats, pending, fetched_list, failed_list = _category_status(json_mgr, section)
    cats = pending + fetched_list + failed_list
    if not cats:
        print('  No categories available.')
        return []

    def prepare():
        return (cats, len(pending), len(fetched_list))
    page = 0
    while True:
        result, page = portal__paged_picker(title, prepare, lambda c: c.get('title', c.get('name', 'Unknown')), lambda c: c.get('total_items', 0), 'Items', lambda total, fcnt: 'All categories fetched. {} done, {} failed.'.format(len(fetched_list), len(failed_list)), 'Fetch', with_failed=True, id_fn=lambda c: str(c.get('id', '')), start_page=page, header_total_fn=lambda: len(all_cats))
        if result == 'done':
            return 'done'
        if result is None:
            return None
        if result == 'all':
            return [str(c.get('id')) for c in pending]
        return [str(c.get('id')) for c in result]

def portal_batch_fetch_section(client, json_mgr, section):
    """Main batch fetch loop for a section. Auto-shows viewer. Blocks until all done or ignored."""
    if section not in ('live', 'movies', 'series'):
        return True
    while True:
        _, _, fetched_list, failed_list = _category_status(json_mgr, section)
        to_fetch = portal_view_categories(json_mgr, section, client)
        if to_fetch == 'done':
            return 'done'
        if to_fetch is None:
            return False
        if not to_fetch:
            return True
        print()
        done_count = len(fetched_list)
        fail_count = len(failed_list)
        total_failed_pages = 0
        retry_queue = []
        for i, cid in enumerate(to_fetch):
            ok, _, failed_pages, p_template = fetch_single_category(client, json_mgr, section, cid)
            if ok:
                done_count += 1
                if failed_pages:
                    total_failed_pages += len(failed_pages)
                    retry_queue.append((p_template, failed_pages, cid, section))
            else:
                fail_count += 1
            line = '  Fetching: [{}/{}] done'.format(i + 1, len(to_fetch))
            if total_failed_pages:
                line += '  |  {} pages failed'.format(total_failed_pages)
            sys.stdout.write(chr(13) + line.ljust(80))
            sys.stdout.flush()
            time.sleep(0.1)
        portal__clear_batch_counter()
        print('  -> [OK] {} fetched. {} pages failed across {} categories.'.format(len(to_fetch), total_failed_pages, len(retry_queue)))
        if retry_queue:
            retry_ok, retry_still_failed = retry_category_pages(client, json_mgr, retry_queue, progress=portal_progress_bar)
            print('  |  {} OK, {} still failed'.format(retry_ok, retry_still_failed))

def portal_run_episodes_step(client, json_mgr):
    """Fetch episodes for selected series. Loops until user presses Back."""
    cache = getattr(json_mgr, 'cache', None)
    existing_responses = []
    if cache:
        step_path = cache.step_path('E3')
        if step_path and os.path.exists(step_path):
            try:
                with open(step_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    existing_responses = existing.get('responses', [])
            except Exception:
                pass
    page = 0
    while True:
        series_items = []
        for cat in json_mgr.data['series'].get('categories', []):
            for item in cat.get('items', []):
                series_items.append(item)
        if not series_items:
            print('  No series available. Fetch series first.')
            portal__cooldown()
            return False
        pending = [it for it in series_items if not it.get('seasons')]
        fetched = [it for it in series_items if it.get('seasons')]
        items = pending + fetched
        if not items:
            print('  No series available.')
            portal__cooldown()
            return False

        def prepare():
            return (pending, len(pending), len(fetched))
        result, page = portal__paged_picker('Available Series', prepare, lambda it: it.get('name', it.get('title', 'Unknown')), lambda it: str(it.get('id', ''))[:8], 'ID', lambda total, fcnt: 'All series fetched. {}/{} items.'.format(len(fetched), total), 'Fetch', divider_label='Already fetched', id_fn=lambda it: it.get('id', ''), start_page=page)
        if result == 'done':
            return 'done'
        if result is None:
            return None
        to_fetch = pending[:] if result == 'all' else result
        if not to_fetch:
            continue
        print()

        def _ep_progress(cur, ecnt, fails):
            pct = cur / ecnt * 100 if ecnt else 100
            filled = int(30 * cur / ecnt) if ecnt else 30
            bar = '=' * filled + '-' * (30 - filled)
            line = '  Fetching: [{}] {:5.1f}% ({}/{})'.format(bar, pct, cur, ecnt)
            if fails:
                line += '  |  {} failed'.format(fails)
            sys.stdout.write(chr(13) + line.ljust(80))
            sys.stdout.flush()
        ok_count, fail_count = fetch_episodes(client, json_mgr, to_fetch, existing_responses, progress=_ep_progress)
        print()
        if fail_count:
            print('  -> [OK] Fetched {}/{} series. {} failed.'.format(ok_count, len(to_fetch), fail_count))
        else:
            print('  -> [OK] Fetched {}/{} series.'.format(ok_count, len(to_fetch)))
        time.sleep(0.5)

def portal__resolve_items_list(client, json_mgr, step_code, items, title, action_type):
    """Shared resolver for C4 and D3. Loops until user presses Back."""
    if not items:
        print('  No items available. Fetch items first.')
        portal__cooldown()
        return False
    cache = getattr(json_mgr, 'cache', None)
    existing_responses = []
    if cache:
        step_path = cache.step_path(step_code)
        if step_path and os.path.exists(step_path):
            try:
                with open(step_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    existing_responses = existing.get('responses', [])
            except Exception:
                pass
    page = 0
    while True:
        pending = [it for it in items if not it.get('resolved_url')]
        resolved = [it for it in items if it.get('resolved_url')]
        items = pending + resolved
        if not items:
            print('  No items available.')
            portal__cooldown()
            return True

        def prepare():
            return (pending, len(pending), len(resolved))
        result, page = portal__paged_picker('Resolve Link', prepare, lambda it: it.get('name', it.get('title', 'Unknown')), lambda it: str(it.get('id', ''))[:8], 'ID', lambda total, fcnt: 'All items resolved. {}/{} items.'.format(len(resolved), total), 'Resolve', divider_label='Already resolved', id_fn=lambda it: it.get('id', ''), start_page=page)
        if result == 'done':
            return 'done'
        if result is None:
            return None
        to_resolve = pending[:] if result == 'all' else result
        if not to_resolve:
            continue
        print()
        fail_count = 0
        for i, item in enumerate(to_resolve):
            if action_type == 'itv':
                resolved_url, raw = resolve_live(client, json_mgr, item)
                existing_responses.append({'id': item.get('id', ''), 'name': item.get('name', item.get('title', '')), 'raw_response': raw if raw else item.get('cmd', '')})
            else:
                resolved_url, raw = resolve_vod(client, json_mgr, item)
                if resolved_url:
                    existing_responses.append({'id': item.get('id', ''), 'name': item.get('name', item.get('title', '')), 'raw_response': raw})
                else:
                    fail_count += 1
            pct = (i + 1) / len(to_resolve) * 100
            filled = int(30 * (i + 1) / len(to_resolve))
            bar = '=' * filled + '-' * (30 - filled)
            line = '  Resolving: [{}] {:5.1f}% ({}/{})'.format(bar, pct, i + 1, len(to_resolve))
            if fail_count:
                line += '  |  {} failed'.format(fail_count)
            sys.stdout.write(chr(13) + line.ljust(80))
            sys.stdout.flush()
            time.sleep(0.3)
        print()
        if fail_count:
            print('  -> [OK] Resolved {}/{} items. {} failed.'.format(len(to_resolve) - fail_count, len(to_resolve), fail_count))
        else:
            print('  -> [OK] Resolved {}/{} items.'.format(len(to_resolve), len(to_resolve)))
        if cache:
            step_path = cache.step_path(step_code)
            if step_path:
                step_data = {'_status': 'done', '_total_resolved': len(existing_responses), '_total_failed': fail_count, 'responses': existing_responses}
                os.makedirs(os.path.dirname(step_path), exist_ok=True)
                with open(step_path, 'w', encoding='utf-8') as f:
                    json.dump(step_data, f, indent=2, ensure_ascii=False)
        time.sleep(0.5)

def portal__resolve_episodes(client, json_mgr, step_code):
    """E4: Series -> Episodes -> Resolve. Loops until user presses Back."""
    cache = getattr(json_mgr, 'cache', None)
    existing_responses = []
    if cache:
        step_path = cache.step_path(step_code)
        if step_path and os.path.exists(step_path):
            try:
                with open(step_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    existing_responses = existing.get('responses', [])
            except Exception:
                pass

    def _is_series_resolved(s):
        for season in s.get('seasons', []):
            for key in season:
                if key.startswith('resolved_ep_'):
                    return True
        return False
    while True:
        page = 0
        series_items = []
        for cat in json_mgr.data['series'].get('categories', []):
            for s in cat.get('items', []):
                seasons = s.get('seasons', [])
                if seasons and any((season.get('episodes') for season in seasons)):
                    series_items.append(s)
        if not series_items:
            print('  No series available. Fetch series first.')
            portal__cooldown()
            return False
        pending = [s for s in series_items if not _is_series_resolved(s)]
        resolved = [s for s in series_items if _is_series_resolved(s)]
        series_items = pending + resolved
        if not series_items:
            print('  No series available.')
            portal__cooldown()
            return False

        def prepare():
            return (pending, len(pending), len(resolved))

        def right_fn(s):
            return sum((len(se.get('episodes', [])) for se in s.get('seasons', []))) if 'seasons' in s else 0
        result, _ = portal__paged_picker('Select Series', prepare, lambda it: it.get('name', it.get('title', 'Unknown')), right_fn, 'Episodes', lambda total, fcnt: 'All series episodes resolved. {}/{} items.'.format(len(resolved), total), 'Resolve', divider_label='Episodes resolved', id_fn=lambda s: s.get('id', ''), start_page=page)
        if result == 'done':
            return 'done'
        if result is None:
            return None
        if result == 'all':
            selected_series = 'ALL'
        else:
            selected_series = result[0] if result else None
        if selected_series is None:
            continue
        if selected_series == 'ALL':
            all_episodes = []
            for s in pending:
                for season in s.get('seasons', []):
                    s_name = season.get('name', 'Unknown')
                    s_cmd = season.get('cmd', '')
                    for ep_num in season.get('episodes', []):
                        all_episodes.append({'season_name': s_name, 'episode_num': ep_num, 'cmd': s_cmd, 'series_name': s.get('name', 'Unknown'), 'series_obj': s})
            if not all_episodes:
                print('  No episodes found.')
                time.sleep(0.5)
                continue
            print()
            fail_count = 0

            def _all_progress(cur, ecnt, fails):
                pct = cur / ecnt * 100 if ecnt else 100
                filled = int(30 * cur / ecnt) if ecnt else 30
                bar = '=' * filled + '-' * (30 - filled)
                line = '  Resolving: [{}] {:5.1f}% ({}/{})'.format(bar, pct, cur, ecnt)
                sys.stdout.write(chr(13) + line.ljust(80))
                sys.stdout.flush()
            for i, ep_dict in enumerate(all_episodes):
                ep_num = ep_dict['episode_num']
                s_name = ep_dict['season_name']
                cmd = ep_dict['cmd']
                s_series = ep_dict['series_obj']
                resolved_url, raw = resolve_episode(client, json_mgr, s_series, s_name, ep_num, cmd)
                if resolved_url:
                    existing_responses.append({'series_name': ep_dict.get('series_name', ''), 'season_name': s_name, 'episode_num': ep_num, 'raw_response': raw})
                else:
                    fail_count += 1
                _all_progress(i + 1, len(all_episodes), fail_count)
                time.sleep(0.3)
            print()
            if fail_count:
                print('  -> [OK] Resolved {}/{} episodes. {} failed.'.format(len(all_episodes) - fail_count, len(all_episodes), fail_count))
            else:
                print('  -> [OK] Resolved {}/{} episodes.'.format(len(all_episodes), len(all_episodes)))
            if cache:
                step_path = cache.step_path(step_code)
                if step_path:
                    step_data = {'_status': 'done', '_total_resolved': len(existing_responses), '_total_failed': fail_count, 'responses': existing_responses}
                    os.makedirs(os.path.dirname(step_path), exist_ok=True)
                    with open(step_path, 'w', encoding='utf-8') as f:
                        json.dump(step_data, f, indent=2, ensure_ascii=False)
            time.sleep(0.5)
            selected_series = None
            continue
        seasons = selected_series.get('seasons', [])
        if not seasons:
            print('  No seasons available for this series.')
            time.sleep(0.5)
            continue
        episodes = []
        for season in seasons:
            season_name = season.get('name', 'Unknown')
            season_cmd = season.get('cmd', '')
            for ep_num in season.get('episodes', []):
                episodes.append({'season_name': season_name, 'episode_num': ep_num, 'cmd': season_cmd})
        if not episodes:
            print('  No episodes found.')
            time.sleep(0.5)
            continue
        print()
        fail_count = 0
        for i, ep_dict in enumerate(episodes):
            ep_num = ep_dict['episode_num']
            s_name = ep_dict['season_name']
            cmd = ep_dict['cmd']
            resolved_url, raw = resolve_episode(client, json_mgr, selected_series, s_name, ep_num, cmd)
            if resolved_url:
                existing_responses.append({'series_name': selected_series.get('name', ''), 'season_name': s_name, 'episode_num': ep_num, 'raw_response': raw})
            else:
                fail_count += 1
            pct = (i + 1) / len(episodes) * 100
            filled = int(30 * (i + 1) / len(episodes))
            bar = '=' * filled + '-' * (30 - filled)
            line = '  Resolving: [{}] {:5.1f}% ({}/{})'.format(bar, pct, i + 1, len(episodes))
            if fail_count:
                line += '  |  {} failed'.format(fail_count)
            sys.stdout.write(chr(13) + line.ljust(80))
            sys.stdout.flush()
            time.sleep(0.3)
        print()
        if fail_count:
            print('  -> [OK] Resolved {}/{} episodes. {} failed.'.format(len(episodes) - fail_count, len(episodes), fail_count))
        else:
            print('  -> [OK] Resolved {}/{} episodes.'.format(len(episodes), len(episodes)))
        if cache:
            step_path = cache.step_path(step_code)
            if step_path:
                step_data = {'_status': 'done', '_total_resolved': len(existing_responses), '_total_failed': fail_count, 'responses': existing_responses}
                os.makedirs(os.path.dirname(step_path), exist_ok=True)
                with open(step_path, 'w', encoding='utf-8') as f:
                    json.dump(step_data, f, indent=2, ensure_ascii=False)
        time.sleep(0.5)
        selected_series = None

def portal_run_resolve_step_auto(client, json_mgr, step_code):
    """Resolve links by picking from list view. No manual cmd entry."""
    if step_code == 'C4':
        items = []
        for cat in json_mgr.data['live'].get('categories', []):
            for ch in cat.get('channels', []):
                items.append(ch)
        return portal__resolve_items_list(client, json_mgr, step_code, items, 'Live Channels', 'itv')
    elif step_code == 'D3':
        items = []
        for cat in json_mgr.data['movies'].get('categories', []):
            for m in cat.get('items', []):
                items.append(m)
        return portal__resolve_items_list(client, json_mgr, step_code, items, 'VOD Movies', 'vod')
    elif step_code == 'E4':
        return portal__resolve_episodes(client, json_mgr, step_code)
    else:
        return False

def portal_show_resume_menu(sessions, reveal_new=False, portal='', mac=''):
    """Display landing page. If reveal_new=True, show Portal/MAC inputs inline."""
    portal_clear_screen()
    print('=' * 60)
    print('   mac2list v1.2')
    print('=' * 60)
    print()
    if sessions:
        print('  [1] Restore session')
        print('      {} saved'.format(len(sessions)))
        print()
    next_num = 2 if sessions else 1
    print('  [{}] New session'.format(next_num))
    if reveal_new:
        print()
        if portal:
            print('      Portal URL: {}'.format(portal))
        else:
            portal = input('      Portal URL: ').strip()
        if mac:
            print('      MAC Address: {}'.format(mac))
        else:
            mac = input('      MAC Address: ').strip()
        if portal and mac:
            return ('', portal, mac)
    print()
    print('  [Q] Quit')
    print()
    return (input('  > ').strip().upper(), portal, mac)

def portal__select_paginated(items, title, header_line, row_fmt_fn, page_size=20):
    """Paged browse where choosing a number returns that item, or None on Back.
    Rows are numbered with their real position in the list (like the resolver)."""
    total = len(items)
    page = 0
    max_page = (total - 1) // page_size
    while True:
        portal_clear_screen()
        print('=' * 60)
        print('   {} — Page {}/{} — {} saved'.format(title, page + 1, max_page + 1, total))
        print('=' * 60)
        print()
        print(header_line)
        print('  ' + '-' * 64)
        start = page * page_size
        end = min(start + page_size, total)
        for i in range(start, end):
            print(row_fmt_fn(items[i], i + 1))
        print()
        if max_page > 0:
            print('  [Enter] Next page  |  [1-{}] Restore  |  [B] Back'.format(total))
        else:
            print('  [1-{}] Restore  |  [B] Back'.format(total))
        choice = input('  > ').strip().upper()
        if choice == 'B':
            return None
        elif choice == '' and max_page > 0:
            page = (page + 1) % (max_page + 1)
        elif choice.isdigit():
            num = int(choice)
            if 1 <= num <= total:
                return items[num - 1]

def portal__select_restore_session(sessions):
    """Show all saved sessions in a paged list. Returns chosen session dict or None."""
    if not sessions:
        print('  No saved sessions.')
        portal__cooldown()
        return None
    header_line = '  {:<4} {:<24} {:<19} {}'.format('#', 'Portal', 'MAC', 'Expiry')

    def row_fmt(s, idx):
        portal = _domain_of(s.get('portal', ''))[:24]
        mac = str(s.get('mac', ''))[:19]
        expiry = _expiry_label(s.get('phone', ''))
        return '  {:<4} {:<24} {:<19} {}'.format(idx, portal, mac, expiry if expiry else '—')
    return portal__select_paginated(sessions, 'Restore Session', header_line, row_fmt)

def portal_run_resume_or_new():
    """Page 1: Clean landing; Restore opens the saved-sessions viewer.
    Returns (portal, mac, json_mgr, is_restored)."""
    sessions = _database_sessions()
    _cleanup_orphans(sessions)
    portal = None
    mac = None
    json_mgr = None
    is_restored = False
    while True:
        choice, _, _ = portal_show_resume_menu(sessions)
        if choice == 'Q':
            print('  Quitting...')
            sys.exit(0)
        if sessions and choice == '1':
            session = portal__select_restore_session(sessions)
            if session is None:
                continue
            portal = session['portal']
            mac = session['mac']
            json_mgr = JSONManager(portal, mac)
            is_restored = True
            break
        if choice == ('2' if sessions else '1'):
            choice2, portal, mac = portal_show_resume_menu(sessions, reveal_new=True)
            if choice2 == 'Q':
                print('  Quitting...')
                sys.exit(0)
            if not portal or not mac:
                print('  [!] Both portal URL and MAC address are required.')
                input('  Press Enter to retry...')
                continue
            if not is_valid_mac(mac):
                print('  [!] Invalid MAC address format. Use format: 00:1A:79:XX:XX:XX')
                input('  Press Enter to retry...')
                continue
            os.makedirs(DATA_DIR, exist_ok=True)
            os.makedirs(SESSION_DIR, exist_ok=True)
            os.makedirs(CACHE_DIR, exist_ok=True)
            json_mgr = JSONManager(portal, mac)
            json_mgr.set_meta(portal, mac)
            _register_session(portal, mac)
            break
        print('  Invalid choice.')
        time.sleep(0.5)
    return (portal, mac, json_mgr, is_restored)

def portal_show_hub_header(json_mgr):
    """Print hub header/menu without input prompt."""
    portal_clear_screen()
    print('=' * 60)
    print('   mac2list v1.2 — Main Hub')
    print('=' * 60)
    print()
    _meta = json_mgr.data.get('_meta', {})
    try:
        _alive = int(_meta.get('check_alive', 0) or 0)
    except (TypeError, ValueError):
        _alive = 0
    try:
        _total = int(_meta.get('check_total', 0) or 0)
    except (TypeError, ValueError):
        _total = 0
    try:
        _live = int(_meta.get('check_live_alive', 0) or 0)
    except (TypeError, ValueError):
        _live = 0
    try:
        _vod = int(_meta.get('check_vod_alive', 0) or 0)
    except (TypeError, ValueError):
        _vod = 0
    if _live >= 50 and _vod >= 50:
        print('  {:<23} —  100%'.format('Health Score'))
    elif _total > 0 and _alive > 0:
        print('  {:<23} —  {}%'.format('Health Score', int(_alive * 100 / _total)))
    else:
        print('  {:<23} —  -'.format('Health Score'))
    print()
    cat_codes = ['C2', 'D1', 'E1']
    cat_done = sum((1 for code in cat_codes if json_mgr.is_done(code)))
    if cat_done == 0:
        cat_status = '0/3 scraped'
    elif cat_done < len(cat_codes):
        cat_status = '{}/{} scraped'.format(cat_done, len(cat_codes))
    else:
        cat_status = 'Updated ' + _time_ago(json_mgr.data['_meta'].get('scraped_at', ''))
    live_count, movie_count, series_count = resolved_counts(json_mgr)
    convert_status = 'Exported' if json_mgr.is_done('G1') else 'Ready'
    settings_done = sum((1 for code in SETTINGS_STEP_CODES if json_mgr.is_done(code)))
    settings_total = len(SETTINGS_STEP_CODES)
    auth_section = SECTIONS['Auth']
    auth_done = sum((1 for code, _, _, _ in auth_section['items'] if json_mgr.is_done(code)))
    auth_total = len(auth_section['items'])
    print('  [1] Scrape Categories  —  {}'.format(cat_status))
    print()
    print('  [2] Live Channels      —  {}'.format(_section_status(json_mgr, 'live', 'ch')))
    print('  [3] VOD Movies         —  {}'.format(_section_status(json_mgr, 'movies', 'movies')))
    print('  [4] Series             —  {}'.format(_section_status(json_mgr, 'series', 'series')))
    print()
    print('  [5] Watch              —  {} ch, {} movies, {} series'.format(live_count, movie_count, series_count))
    print('  [6] Convert            —  {}'.format(convert_status))
    print()
    print('  [7] Settings           —  {}/{} done'.format(settings_done, settings_total))
    print('  [8] Auth               —  {}/{} done'.format(auth_done, auth_total))
    print()
    print('-' * 60)
    print('  [B] Back')
    print()

def portal_show_hub(json_mgr):
    """Display Main Hub. Returns user choice string."""
    portal_show_hub_header(json_mgr)
    return input('  > ').strip().upper()

def portal_hub_loop(client, json_mgr, is_restored):
    """Main Hub loop."""
    if is_restored:
        print('  -> Session restored')
        time.sleep(0.3)
    else:
        print('  -> New session started')
        time.sleep(0.3)
    while True:
        choice = portal_show_hub(json_mgr)
        if choice == 'B':
            break
        elif choice == '1':
            cat_codes = ['C2', 'D1', 'E1']
            all_done = all((json_mgr.is_done(c) for c in cat_codes))
            if all_done:
                portal_show_hub_header(json_mgr)
                print('  Already scraped.')
                ans = input('  Re-scrape? [Y/N] > ').strip().upper()
                if ans != 'Y':
                    continue
                for c in cat_codes:
                    if json_mgr.is_done(c):
                        done = json_mgr.data['_meta'].get('done_steps', [])
                        if c in done:
                            done.remove(c)
                            json_mgr.data['_meta']['done_steps'] = done
                json_mgr.data['_meta']['scraped_at'] = ''
                json_mgr.save()
            while True:
                next_code = get_next_pending_step(json_mgr, cat_codes)
                if next_code is None:
                    break
                portal_show_hub_header(json_mgr)
                idx, _, desc, info, is_auto = get_step_info(next_code)
                portal_run_single_step(client, json_mgr, next_code, desc, info, is_auto)
        elif choice == '2':
            portal_run_section_submenu(client, json_mgr, 'Live Channels', skip=['C2'])
        elif choice == '3':
            portal_run_section_submenu(client, json_mgr, 'VOD Movies', skip=['D1'])
        elif choice == '4':
            portal_run_section_submenu(client, json_mgr, 'Series', skip=['E1'])
        elif choice == '5':
            portal_run_watch_submenu(json_mgr)
        elif choice == '6':
            portal_run_convert_submenu(json_mgr)
        elif choice == '7':
            portal_run_settings_submenu(client, json_mgr)
        elif choice == '8':
            portal_run_section_submenu(client, json_mgr, 'Auth')
        else:
            print('  Invalid choice.')
            time.sleep(0.5)

def portal_run_section_submenu(client, json_mgr, sec_key, skip=None):
    """Independent step picker for one section."""
    if skip is None:
        skip = []
    sec = SECTIONS[sec_key]
    visible_items = [(c, d, i, a) for c, d, i, a in sec['items'] if c not in skip]
    while True:
        portal_clear_screen()
        print('=' * 60)
        print('   {}'.format(sec['title']))
        print('=' * 60)
        print()
        for j, (code, desc, info, _) in enumerate(visible_items):
            progress = _step_progress(json_mgr, code)
            print('  [{}] {:<45} {}'.format(j + 1, desc, progress))
        print()
        if len(visible_items) > 1:
            print('  [1-{}] Pick step  |  [B] Back'.format(len(visible_items)))
        else:
            print('  [1] Pick step  |  [B] Back')
        choice = input('  > ').strip().upper()
        if choice == 'B':
            break
        elif choice.isdigit() and 1 <= int(choice) <= len(visible_items):
            code = visible_items[int(choice) - 1][0]
            idx, _, desc, info, is_auto = get_step_info(code)
            portal_run_single_step(client, json_mgr, code, desc, info, is_auto)
        else:
            print('  Invalid choice.')
            time.sleep(0.5)

def portal_print_settings_submenu(json_mgr):
    """Display Settings sub-menu."""
    portal_clear_screen()
    print('=' * 60)
    done_count = sum((1 for code in SETTINGS_STEP_CODES if json_mgr.is_done(code)))
    total = len(SETTINGS_STEP_CODES)
    print('   Settings — {}/{} done'.format(done_count, total))
    print('=' * 60)
    print()
    sec = SECTIONS['Settings']
    for j, (code, desc, info, _) in enumerate(sec['items']):
        if json_mgr.is_done(code):
            mark = '[x]'
        elif json_mgr.is_ignored(code):
            mark = '[I]'
        else:
            mark = '[>]'
        print('  {} {:<50} {}'.format(mark, desc, info))
    print()
    print('  [Enter] Continue next pending  |  [B] Back')

def portal_run_settings_submenu(client, json_mgr):
    """Settings sub-menu loop."""
    while True:
        portal_print_settings_submenu(json_mgr)
        choice = input('  > ').strip().upper()
        if choice == 'B':
            break
        elif choice == '':
            next_code = get_next_pending_step(json_mgr, SETTINGS_STEP_CODES)
            if next_code is None:
                print('  -> [OK] All settings steps complete.')
                portal__cooldown()
            else:
                idx, sec_key, desc, info, is_auto = get_step_info(next_code)
                portal_run_single_step(client, json_mgr, next_code, desc, info, is_auto)
        else:
            print('  Invalid choice.')
            time.sleep(0.5)

def portal_run_convert_submenu(json_mgr):
    """Convert action."""
    portal_clear_screen()
    print('=' * 60)
    print('   Convert')
    print('=' * 60)
    print()
    files = generate_m3u(json_mgr)
    json_mgr.mark_done('G1')
    print('  -> [OK] M3U files generated:')
    print('     Live:   {}'.format(files.get('live', '')))
    print('     Movies: {}'.format(files.get('movies', '')))
    print('     Series: {}'.format(files.get('series', '')))
    print()
    print('  [R] Regenerate  |  [B] Back')
    choice = input('  > ').strip().upper()
    if choice == 'R':
        files = generate_m3u(json_mgr)
        print('  -> [OK] Regenerated:')
        print('     Live:   {}'.format(files.get('live', '')))
        print('     Movies: {}'.format(files.get('movies', '')))
        print('     Series: {}'.format(files.get('series', '')))
        portal__cooldown()

def portal_show_watch_submenu(json_mgr):
    """Display Watch sub-menu."""
    portal_clear_screen()
    print('=' * 60)
    print('   Watch — Browse fetched content')
    print('=' * 60)
    print()
    live_count, movie_count, series_count = resolved_counts(json_mgr)
    print('  [1] Live Channels     —  {} channels'.format(live_count))
    print('  [2] VOD Movies        —  {} movies'.format(movie_count))
    print('  [3] Series            —  {} series'.format(series_count))
    print()
    print('  [B] Back')

def portal_run_watch_submenu(json_mgr):
    """Watch sub-menu loop."""
    portal_get_vlc_path(json_mgr)
    while True:
        portal_show_watch_submenu(json_mgr)
        choice = input('  > ').strip().upper()
        if choice == 'B':
            break
        elif choice == '1':
            portal_watch_live(json_mgr)
        elif choice == '2':
            portal_watch_movies(json_mgr)
        elif choice == '3':
            portal_watch_series(json_mgr)
        else:
            print('  Invalid choice.')
            time.sleep(0.5)

def portal_run_single_step(client, json_mgr, code, desc, info, is_auto):
    """Execute a single step. Returns to caller when done."""
    print()
    print('  Executing: {} — {}'.format(code, desc))
    print()
    success = False
    step_msg = ''
    if is_auto:

        def _probe_bar(cur, total):
            portal_progress_bar(cur, total, prefix='  Loading Categories: ')
        success, step_msg = run_auto_fetch_step(client, json_mgr, code, desc, probe_progress=_probe_bar)
    elif code in ('C4', 'D3', 'E4'):
        success = portal_run_resolve_step_auto(client, json_mgr, code)
    elif code in ('C5', 'D4', 'E5'):
        if code == 'C5':
            success = portal_batch_fetch_section(client, json_mgr, 'live')
        elif code == 'D4':
            success = portal_batch_fetch_section(client, json_mgr, 'movies')
        elif code == 'E5':
            success = portal_batch_fetch_section(client, json_mgr, 'series')
    elif code == 'E3':
        success = portal_run_episodes_step(client, json_mgr)
    elif code == 'F3':
        cache = getattr(json_mgr, 'cache', None)
        success, step_msg = unlock(client, cache)
    elif code == 'G1':
        files = generate_m3u(json_mgr)
        step_msg = '  -> [OK] M3U files saved to {}'.format(os.path.join(OUTPUT_DIR, json_mgr.cache.session_id))
        success = True
    if success:
        json_mgr.mark_done(code)
        if step_msg:
            print(step_msg)
        print('  -> [OK] {} complete.'.format(desc))
    else:
        if step_msg:
            print(step_msg)
        print('  -> [..] {} — not complete yet.'.format(desc))
    if success or step_msg:
        print()
        portal__cooldown()
    return success

def portal_get_vlc_path(json_mgr):
    """Ask user for VLC path once, store in session _meta."""
    meta = json_mgr.data.setdefault('_meta', {})
    if meta.get('vlc_path'):
        p = meta['vlc_path']
        if os.path.isfile(p):
            return p
    print('  VLC not configured. Enter path to vlc.exe')
    print('  Example: C:\\Program Files\\VideoLAN\\VLC\\vlc.exe')
    path = input('  > ').strip().strip('"')
    if not path or not os.path.isfile(path):
        print('  Invalid path, VLC playback disabled.')
        return None
    meta['vlc_path'] = path
    json_mgr.save()
    return path

def portal_paginated_browse(items, title, headers, row_fmt_fn, page_size=20, get_urls_fn=None, vlc_path=None):
    """Generic paginated read-only browser."""
    total = len(items)
    if total == 0:
        print('  No items available.')
        portal__cooldown()
        return
    page = 0
    max_page = (total - 1) // page_size
    while True:
        portal_clear_screen()
        print('=' * 60)
        print('   {} — Page {}/{} — {} total'.format(title, page + 1, max_page + 1, total))
        print('=' * 60)
        print()
        header_line = '  ' + '  '.join(headers)
        print(header_line)
        print('  ' + '-' * 64)
        start = page * page_size
        end = min(start + page_size, total)
        for i in range(start, end):
            print(row_fmt_fn(items[i], i + 1))
        print()
        if get_urls_fn:
            if max_page > 0:
                if page < max_page:
                    print('  [Enter] Next page  |  [1-{}] Play  |  [B] Back'.format(end - start))
                else:
                    print('  [Enter] First page  |  [1-{}] Play  |  [B] Back'.format(end - start))
            else:
                print('  [1-{}] Play  |  [B] Back'.format(end - start))
        elif max_page > 0:
            if page < max_page:
                print('  [Enter] Next page  |  [B] Back')
            else:
                print('  [Enter] First page  |  [B] Back')
        else:
            print('  [B] Back')
        choice = input('  > ').strip().upper()
        if choice == 'B':
            break
        elif choice == '' and max_page > 0:
            page = (page + 1) % (max_page + 1)
        elif get_urls_fn and choice.isdigit():
            num = int(choice)
            if 1 <= num <= end - start:
                item = items[start + num - 1]
                result = get_urls_fn(item)
                if result:
                    if isinstance(result[0], tuple):
                        names = [r[0] for r in result]
                        urls = [r[1] for r in result]
                    else:
                        names = None
                        urls = result
                    print('  Playing...')
                    play_in_vlc(vlc_path, urls, names=names)
                    time.sleep(1)

def portal_watch_live(json_mgr):
    """Read-only live channel viewer."""
    items = _resolved_channels(json_mgr)
    if not items:
        print('  No channels available. Fetch channels first (Scrape → Live).')
        portal__cooldown()
        return

    def fmt(item, idx):
        name = item.get('name', 'Unknown')[:50]
        return '  {:<4} {}'.format(idx, name)

    def get_urls(item):
        url = item.get('resolved_url', '')
        return [(item.get('name', ''), url)] if url else []
    portal_paginated_browse(items, 'Live Channels', ['#', 'Name'], fmt, get_urls_fn=get_urls, vlc_path=json_mgr.data.get('_meta', {}).get('vlc_path'))

def portal_watch_movies(json_mgr):
    """Read-only movie viewer."""
    items = _resolved_movies(json_mgr)
    if not items:
        print('  No movies available. Fetch movies first (Scrape → VOD).')
        portal__cooldown()
        return

    def fmt(item, idx):
        name = item.get('name', 'Unknown')[:50]
        return '  {:<4} {}'.format(idx, name)

    def get_urls(item):
        url = item.get('resolved_url', '')
        return [(item.get('name', ''), url)] if url else []
    portal_paginated_browse(items, 'VOD Movies', ['#', 'Name'], fmt, get_urls_fn=get_urls, vlc_path=json_mgr.data.get('_meta', {}).get('vlc_path'))

def portal_watch_series(json_mgr):
    """Read-only series viewer."""
    items = _resolved_series(json_mgr)
    if not items:
        print('  No series available. Fetch series first (Scrape → Series).')
        portal__cooldown()
        return

    def fmt(item, idx):
        name = item.get('name', 'Unknown')[:50]
        resolved, total_eps = _series_episode_counts(item)
        return '  {:<4} {} ({}/{})'.format(idx, name, resolved, total_eps)

    def get_urls(item):
        urls = []
        series_name = item.get('name', '')
        for se in item.get('seasons', []):
            season_num = se.get('season', '')
            for ep in se.get('episodes', []):
                url = se.get('resolved_ep_{}'.format(ep))
                if url:
                    label = '{} S{}E{}'.format(series_name, season_num, ep)
                    urls.append((label, url))
        return urls
    portal_paginated_browse(items, 'Series', ['#', 'Name'], fmt, get_urls_fn=get_urls, vlc_path=json_mgr.data.get('_meta', {}).get('vlc_path'))


# ============================================================
# MAIN
# ============================================================
# Position info traveling with the portal into the hub title.
_hub_title_idx = 0
_hub_title_total = 0

# Full Scan mode: counter row on, start prompt off.
_hub_full = False


_CONTENT_CODES = {"C2", "D1", "E1", "C5", "D4", "E5", "C4", "D3", "E4", "E3"}

def _clean_hub_memory(json_mgr):
    """Health-only fresh: clear live/vod/series content + content done markers,
    keep every other _meta value (health score, handshake, history)."""
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
            meta["done_steps"] = [c for c in done if c not in _CONTENT_CODES]
        ignored = meta.get("ignored_steps", [])
        if isinstance(ignored, list):
            meta["ignored_steps"] = [c for c in ignored if c not in _CONTENT_CODES]
        if meta.get("last_step") in _CONTENT_CODES:
            meta.pop("last_step", None)
        meta.pop("scraped_at", None)


def _fresh_session_file(portal, mac):
    """Health-only clean of saved file: clear live/vod/series + content done
    markers, keep _meta health/handshake/history and cache untouched."""
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
                meta["done_steps"] = [c for c in done if c not in _CONTENT_CODES]
            ignored = meta.get("ignored_steps", [])
            if isinstance(ignored, list):
                meta["ignored_steps"] = [c for c in ignored if c not in _CONTENT_CODES]
            if meta.get("last_step") in _CONTENT_CODES:
                meta.pop("last_step", None)
            meta.pop("scraped_at", None)
        with open(spath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _run_one(portal, mac, idx, total):
    """Open one portal and run its hub. Fail and done both return here."""
    global _hub_title_idx, _hub_title_total
    json_mgr = JSONManager(portal, mac)
    meta = json_mgr.data.setdefault("_meta", {})
    if not meta.get("portal") or not meta.get("mac"):
        json_mgr.set_meta(portal, mac)
    # Clean open: drop step state from memory only (no save), so the hub
    # starts pending while the file keeps history for the portal list.
    # The run re-saves fresh outcomes, content and markers as it goes.
    _clean_hub_memory(json_mgr)
    _hub_title_idx = idx
    _hub_title_total = total
    client = Mac2ListPortal(portal, mac)

    # No handshake here — first screen just moves to the hub.
    # Handshake runs only on hub row [1] click.
    # Enter Hub (returns on [B] Back → session selection)
    hub_loop(client, json_mgr, True)


def _run_one_portal(portal, mac):
    """Open one portal in the portal hub: fresh window, handshake below it."""
    json_mgr = JSONManager(portal, mac)
    meta = json_mgr.data.setdefault("_meta", {})
    if not meta.get("portal") or not meta.get("mac"):
        json_mgr.set_meta(portal, mac)
    _clean_hub_memory(json_mgr)
    client = Mac2ListPortal(portal, mac)
    portal_show_hub_header(json_mgr)
    print()
    print("  > ")
    idx, _, desc, info, is_auto = get_step_info("A1")
    if not run_single_step(client, json_mgr, "A1", desc, info, is_auto):
        return
    portal_hub_loop(client, json_mgr, True)


def _scan_again(session):
    """Second-plus runs: everything reruns except dead handshakes.

    Skipped only when the saved handshake failed with an HTTP 4xx code
    or a connection failure. Timeouts, 5xx, no-token, half-done,
    fully passed and never-scanned portals all run full again."""
    meta = _session_meta(session)
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


def main():
    global _hub_full, _hub_pos
    while True:
        sessions = _database_sessions()
        _cleanup_orphans(sessions)
        choice = show_top_menu(sessions)
        if choice == "Q":
            return
        if choice == "1":
            clear_screen()
            _paint_top_menu(sessions)
            print()
            print("  > ")
            print()
            portal = input("  Portal : ").strip()
            if not portal:
                continue
            mac = input("  MAC : ").strip()
            if not mac:
                continue
            if not is_valid_mac(mac):
                print("  [!] Invalid MAC address format. Use format: 00:1A:79:XX:XX:XX")
                _cooldown()
                continue
            _fresh_session_file(portal, mac)
            _register_new_portal(portal, mac)
            _hub_full = False
            _run_one(portal, mac, 1, 1)
        elif choice == "2" and sessions:
            while True:
                mode, payload = run_resume_or_new()
                if mode in ("BACK", "EMPTY"):
                    break
                if mode == "FULL":
                    todo = [s for s in payload if _scan_again(s)]
                    for i, session in enumerate(todo):
                        _hub_full = True
                        _run_one(session["portal"], session["mac"], i + 1, len(todo))
                    _hub_full = False
                else:
                    _hub_full = False
                    portal, mac, idx, total = payload
                    _run_one_portal(portal, mac)


if __name__ == "__main__":
    main()