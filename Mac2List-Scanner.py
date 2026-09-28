#!/usr/bin/env python3
"""
mac2list v1.2 — CLI interface.

This file is the ONLY user-facing entry point. All reusable logic lives in the
silent core/ package (no print/input/screen code). This file contains every
screen, menu, progress renderer and the main() entry point.
"""
import json
import os
import sys
import time

import requests

from core.config import (
    OUTPUT_DIR,
    SESSION_DIR,
    STEP_PARAMS,
)
from core.engine import (
    get_step_info,
    run_auto_fetch_step,
)
from core.fetch import (
    category_status as _category_status,
)
from core.portal import Mac2ListPortal
from core.resolve import (
    resolve_live,
    resolve_vod,
)
from core.sessions import (
    cleanup_orphans as _cleanup_orphans,
    database_sessions as _database_sessions,
    make_session_id,
)
from core.storage import (
    JSONManager,
    handle_fetch_result,
    save_error_json,
    save_json,
)
from core.utils import (
    domain_of as _domain_of,
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
    out_dir = os.path.join(OUTPUT_DIR, session_id, folder)
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
                if isinstance(picked, dict) and picked.get("kind") == "pending":
                    continue
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
    """Checker success line when links checked, else combined side causes."""
    data = _session_data(session)
    meta = data.get("_meta", {})
    if not isinstance(meta, dict):
        meta = {}
    if meta.get("handshake_status") == "failed":
        return "Handshake - {}".format(meta.get("handshake_reason", "unknown"))

    try:
        check_alive = int(meta.get("check_alive", 0) or 0)
    except (TypeError, ValueError):
        check_alive = 0
    try:
        check_total = int(meta.get("check_total", 0) or 0)
    except (TypeError, ValueError):
        check_total = 0
    if check_total > 0:
        if check_alive >= check_total:
            return "success"
        if check_alive > 0:
            return "success - {}%".format(int(check_alive * 100 / check_total))
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
    """Sort key: pending first, then least progress, fully passed last, locked very last."""
    if session.get("kind") == "pending":
        return (1, 0, 0, 0, "")
    meta = _session_meta(session)
    done = meta.get("done_steps", [])
    stages = [
        meta.get("handshake_status") == "pass",
        meta.get("scrape_status") == "pass" and "C2" in done and "D1" in done,
        meta.get("fetch_live_status") == "pass",
        meta.get("fetch_movies_status") == "pass",
        meta.get("resolve_live_status") == "pass",
        meta.get("resolve_movies_status") == "pass",
    ]
    ran = [meta.get("handshake_status"), meta.get("scrape_status"),
           meta.get("fetch_live_status"), meta.get("fetch_movies_status"),
           meta.get("resolve_live_status"), meta.get("resolve_movies_status")]
    if not any(r in ("pass", "failed") for r in ran):
        return (0, 0, 0, 0, "")
    passed = sum(1 for s in stages if s)
    first_bad = next((i for i, s in enumerate(stages) if not s), len(stages))
    reason_keys = ["handshake_reason", "scrape_reason",
                   "fetch_live_reason", "fetch_movies_reason",
                   "resolve_live_reason", "resolve_movies_reason"]
    reason = ""
    if first_bad < len(stages):
        reason = meta.get(reason_keys[first_bad], "") or ""
    return (0, 1, passed, first_bad, reason)


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
        status = "Locked" if s.get("kind") == "pending" else _portal_status(s)
        return "  {:<4} {:<24} {:<19} {}".format(idx, portal, mac, status)

    sessions = sorted(sessions, key=_portal_rank)
    picked = _select_paginated(sessions, "mac2list Scanner v1.2", header_line, row_fmt)
    return picked, sessions


def run_resume_or_new():
    """Page 1: Open straight on Restore Session viewer.
    Returns (mode, payload). ONE: (portal, mac, idx, total).
    FULL: sorted sessions list. EMPTY: None."""
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
            print("  Quitting...")
            sys.exit(0)
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
    """Check every collected m3u link, persist alive share."""
    session_id = json_mgr.cache.session_id
    urls = []
    for folder in ("live", "vod"):
        folder_path = os.path.join(OUTPUT_DIR, session_id, folder)
        if not os.path.isdir(folder_path):
            continue
        for name in sorted(os.listdir(folder_path)):
            if not name.endswith(".m3u"):
                continue
            with open(os.path.join(folder_path, name),
                      encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
    total = len(urls)
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
    for i, url in enumerate(urls):
        ok, err = _check_link(url)
        if ok:
            alive += 1
        else:
            fail_count += 1
            if not first_err:
                first_err = err or "unknown"
        line = "  -> Checking: [{}/{}]".format(i + 1, total)
        if fail_count:
            line += "  |  {} failed".format(fail_count)
        sys.stdout.write(chr(13) + line.ljust(80))
        sys.stdout.flush()
        time.sleep(0.1)
    _clear_batch_counter()
    meta["check_alive"] = alive
    meta["check_total"] = total
    if alive == 0:
        _save_step_outcome(json_mgr, "check", False, first_err)
    else:
        _save_step_outcome(json_mgr, "check", True)
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

    print("  {} {:<45} {}".format(">>" if _hub_pos == 0 else "  ", "handshake", handshake_status(json_mgr)))
    print()
    print("  {} {:<45} {}".format(">>" if _hub_pos == 1 else "  ", _side_label("Channels", json_mgr, "C5", "C4"), _side_status(json_mgr, "scrape_live", "C5", "C4")))
    print()
    print("  {} {:<45} {}".format(">>" if _hub_pos == 2 else "  ", _side_label("Vod", json_mgr, "D4", "D3"), _side_status(json_mgr, "scrape_movies", "D4", "D3")))
    print()
    print("  {} {:<45} {}".format(">>" if _hub_pos == 3 else "  ", "Link Checker ({}/{})".format(*_row_counts(json_mgr, "CHK")), _row_status(json_mgr, "CHK")))
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
            ok = run_single_step(client, json_mgr, "CHK", "Link Checker", "", False)
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
# MAIN
# ============================================================
# Position info traveling with the portal into the hub title.
_hub_title_idx = 0
_hub_title_total = 0

# Full Scan mode: counter row on, start prompt off.
_hub_full = False


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
    meta.pop("done_steps", None)
    meta.pop("last_step", None)
    meta.pop("ignored_steps", None)
    meta.pop("scraped_at", None)
    for key in ("handshake_status", "handshake_reason",
                "scrape_status", "scrape_reason",
                "scrape_live_status", "scrape_live_reason",
                "scrape_movies_status", "scrape_movies_reason",
                "fetch_live_status", "fetch_live_reason",
                "fetch_movies_status", "fetch_movies_reason",
                "resolve_live_status", "resolve_live_reason",
                "resolve_movies_status", "resolve_movies_reason",
                "check_status", "check_reason",
                "check_alive", "check_total"):
        meta.pop(key, None)
    for section in ("live", "movies"):
        sec = json_mgr.data.get(section)
        if isinstance(sec, dict):
            sec["categories"] = []
            for key in [k for k in sec
                        if k.startswith(("_remaining", "_fetched", "_failed", "_probed"))]:
                sec[key] = []
    _hub_title_idx = idx
    _hub_title_total = total
    client = Mac2ListPortal(portal, mac)

    # No handshake here — first screen just moves to the hub.
    # Handshake runs only on hub row [1] click.
    # Enter Hub (returns on [B] Back → session selection)
    hub_loop(client, json_mgr, True)


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
    global _hub_full
    while True:
        mode, payload = run_resume_or_new()

        if mode == "EMPTY":
            return

        if mode == "FULL":
            todo = [s for s in payload
                    if s.get("kind") != "pending" and _scan_again(s)]
            for i, session in enumerate(todo):
                _hub_full = True
                _run_one(session["portal"], session["mac"], i + 1, len(todo))
            _hub_full = False
        else:
            _hub_full = False
            portal, mac, idx, total = payload
            _run_one(portal, mac, idx, total)


if __name__ == "__main__":
    main()