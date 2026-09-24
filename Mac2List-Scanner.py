#!/usr/bin/env python3
"""
mac2list v1.2 — CLI interface.

This file is the ONLY user-facing entry point. All reusable logic lives in the
silent core/ package (no print/input/screen code). This file contains every
screen, menu, progress renderer and the main() entry point.
"""
import json
import os
import shutil
import sys
import time

from core.config import (
    OUTPUT_DIR,
    SECTIONS,
    SESSION_DIR,
    STEP_PARAMS,
)
from core.convert import generate_m3u
from core.engine import (
    get_next_pending_step,
    get_step_info,
    run_auto_fetch_step,
    step_progress as _step_progress,
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
    time_ago as _time_ago,
)

# ============================================================
# TERMINAL HELPERS  (interface only)
# ============================================================
def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def _cooldown(seconds=1):
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


def fetch_all_live_no_viewer(client, json_mgr):
    """Fetch 1st page of every pending live category first,
    then filter the entire set at once and keep the first 100."""
    _, pending, _, _ = _category_status(json_mgr, "live")
    if not pending:
        return True
    show_hub_header(json_mgr)
    print()
    # Phase 1: fetch everything into memory, no filtering yet
    collected = []
    fetched_ids = []
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
                json_mgr.mark_live_genre_failed(cid)
        else:
            json_mgr.mark_live_genre_failed(cid)
        line = "  Fetching: [{}/{}] done".format(i + 1, len(pending))
        sys.stdout.write(chr(13) + line.ljust(80))
        sys.stdout.flush()
        time.sleep(0.1)
    _clear_batch_counter()
    if not fetched_ids:
        print("  -> [OK] 0 fetched.")
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
    print("  -> [OK] {} channels kept.".format(len(kept)))
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
    show_hub_header(json_mgr)
    print()
    # Phase 1: fetch everything into memory, no filtering yet
    collected = []
    fetched_ids = []
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
                json_mgr.mark_movie_category_failed(cid)
        else:
            json_mgr.mark_movie_category_failed(cid)
        line = "  Fetching: [{}/{}] done".format(i + 1, len(pending))
        sys.stdout.write(chr(13) + line.ljust(80))
        sys.stdout.flush()
        time.sleep(0.1)
    _clear_batch_counter()
    if not fetched_ids:
        print("  -> [OK] 0 fetched.")
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
    print("  -> [OK] {} movies kept.".format(len(kept)))
    return True


# ============================================================
# RESOLVE ALL + SAVE M3U (interface only, core untouched)
# ============================================================
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
    show_hub_header(json_mgr)
    print()
    total = len(pending)
    fail_count = 0
    for i, item in enumerate(pending):
        if action_type == "itv":
            resolved_url, raw = resolve_live(client, json_mgr, item)
            existing_responses.append({
                "id": item.get("id", ""),
                "name": item.get("name", item.get("title", "")),
                "raw_response": raw if raw else item.get("cmd", "")
            })
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
        line = "  Resolving: [{}/{}]".format(i + 1, total)
        if fail_count:
            line += "  |  {} failed".format(fail_count)
        sys.stdout.write(chr(13) + line.ljust(80))
        sys.stdout.flush()
        time.sleep(0.3)
    _clear_batch_counter()
    done = total - fail_count
    if fail_count:
        print("  -> [OK] Resolved {}/{} items. {} failed.".format(done, total, fail_count))
    else:
        print("  -> [OK] Resolved {}/{} items.".format(done, total))
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

    files = generate_m3u(json_mgr)
    key = "live" if section == "live" else "movies"
    src = files.get(key, "")
    out_dir = os.path.join(OUTPUT_DIR, json_mgr.cache.session_id, folder)
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, os.path.basename(src))
    shutil.move(src, dst)
    print("  -> [OK] M3U saved to {}".format(dst))
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
            print("  [Enter] Next page  |  [1-{}] Restore  |  [B] Back".format(total))
        else:
            print("  [1-{}] Restore  |  [B] Back".format(total))

        choice = input("  > ").strip().upper()
        if choice == "B":
            return None
        elif choice == "" and max_page > 0:
            page = (page + 1) % (max_page + 1)
        elif choice.isdigit():
            num = int(choice)
            if 1 <= num <= total:
                return items[num - 1]


def _portal_status(session):
    """Pass, failed - xxx, or pending read from the saved session file."""
    try:
        session_id = make_session_id(session.get("portal", ""), session.get("mac", ""))
        path = os.path.join(SESSION_DIR, session_id + ".json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("_meta", {}) if isinstance(data, dict) else {}
        if meta.get("handshake_status") == "pass":
            return "pass"
        if meta.get("handshake_status") == "failed":
            return "failed - {}".format(meta.get("handshake_reason", "unknown"))
    except Exception:
        pass
    return "pending"


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
        return "  {:<4} {:<24} {:<19} {}".format(idx, portal, mac, _portal_status(s))

    return _select_paginated(sessions, "Restore Session", header_line, row_fmt)


def run_resume_or_new():
    """Page 1: Open straight on Restore Session viewer.
    Returns (portal, mac, json_mgr, is_restored)."""
    while True:
        sessions = _database_sessions()
        _cleanup_orphans(sessions)
        if not sessions:
            clear_screen()
            print("=" * 60)
            print("   Restore Session")
            print("=" * 60)
            print()
            print("  No saved sessions.")
            _cooldown()
            return None, None, None, False

        session = _select_restore_session(sessions)
        if session is None:
            print("  Quitting...")
            sys.exit(0)

        portal = session["portal"]
        mac = session["mac"]
        json_mgr = JSONManager(portal, mac)
        meta = json_mgr.data.setdefault("_meta", {})
        if not meta.get("portal") or not meta.get("mac"):
            json_mgr.set_meta(portal, mac)
        is_restored = True
        break

    return portal, mac, json_mgr, is_restored


# ============================================================
# PAGE 2 — MAIN HUB
# ============================================================
# Next hub draw prints this under the menu, then clears it.
_hub_notice = []

# Sequential hub order: one step per Enter press.
_ORDER = ["A1", "SCRAPE", "C5", "C4", "D4", "D3", "G1"]
_hub_pos = 0


def run_hub_handshake(client, json_mgr):
    """Clear, run handshake, then redraw menu with message under it."""
    global _hub_notice
    print("  -> Handshake running...")
    success, _ = run_handshake_step(client, json_mgr)
    if success:
        json_mgr.mark_done("A1")
        result_line = "  -> pass"
    else:
        reason = json_mgr.data.get("_meta", {}).get("handshake_reason", "unknown")
        result_line = "  -> failed - {}".format(reason)
    _hub_notice = [result_line, "  -> saved"]
    return success


def show_hub_header(json_mgr):
    """Print hub header/menu without input prompt."""
    clear_screen()
    print("=" * 60)
    meta = json_mgr.data.get("_meta", {})
    print("   mac2list Scanner v1.2 — {} — {}".format(
        _domain_of(meta.get("portal", "")) or "Main Hub", meta.get("mac", "")))
    print("=" * 60)
    print()

    # Scrape categories status
    cat_codes = ["C2", "D1"]
    cat_done = sum(1 for code in cat_codes if json_mgr.is_done(code))
    if cat_done == 0:
        cat_status = "Not scraped"
    elif cat_done < len(cat_codes):
        cat_status = "{}/{} scraped".format(cat_done, len(cat_codes))
    else:
        cat_status = "Updated " + _time_ago(json_mgr.data["_meta"].get("scraped_at", ""))

    # Convert
    convert_status = "Exported" if json_mgr.is_done("G1") else "Ready"

    # Step rows moved from sub-menus (descs match old submenu rows)
    _descs = {}
    for _sec in SECTIONS.values():
        for _code, _desc, _info, _auto in _sec["items"]:
            _descs[_code] = _desc

    print("  {} {:<45} {}".format(">>" if _hub_pos == 0 else "  ", _descs.get("A1", "A1"), handshake_status(json_mgr)))
    print()
    print("  {} {:<45} —  {}".format(">>" if _hub_pos == 1 else "  ", "Scrape Categories", cat_status))
    print()
    print("  {} {:<45} {}".format(">>" if _hub_pos == 2 else "  ", _descs.get("C5", "C5"), _step_progress(json_mgr, "C5")))
    print("  {} {:<45} {}".format(">>" if _hub_pos == 3 else "  ", _descs.get("C4", "C4"), _step_progress(json_mgr, "C4")))
    print()
    print("  {} {:<45} {}".format(">>" if _hub_pos == 4 else "  ", _descs.get("D4", "D4"), _step_progress(json_mgr, "D4")))
    print("  {} {:<45} {}".format(">>" if _hub_pos == 5 else "  ", _descs.get("D3", "D3"), _step_progress(json_mgr, "D3")))
    print()
    print("  {} {:<45} —  {}".format(">>" if _hub_pos == 6 else "  ", "Convert", convert_status))
    print()
    print("  [Enter] Next step | [B] Back")
    print()
    print("-" * 60)


def show_hub(json_mgr):
    """Display Main Hub. Returns user choice string."""
    global _hub_notice
    show_hub_header(json_mgr)
    if _hub_notice:
        for line in _hub_notice:
            print(line)
        _hub_notice = []
    print()
    return input("  > ").strip().upper()


def hub_loop(client, json_mgr, is_restored):
    """Main Hub loop: one step per Enter press, in row order."""
    global _hub_notice, _hub_pos
    _hub_notice = ["  -> press Enter to start with handshake"]
    _hub_pos = 0
    while True:
        choice = show_hub(json_mgr)
        if choice == "B":
            break
        elif choice == "":
            code = _ORDER[_hub_pos]
            ok = True
            if code == "SCRAPE":
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
                while True:
                    next_code = get_next_pending_step(json_mgr, cat_codes)
                    if next_code is None:
                        break
                    show_hub_header(json_mgr)
                    idx, _, desc, info, is_auto = get_step_info(next_code)
                    if not run_single_step(client, json_mgr, next_code, desc, info, is_auto):
                        ok = False
                        break
            elif code == "G1":
                run_convert_submenu(json_mgr)
            else:
                idx, _, desc, info, is_auto = get_step_info(code)
                ok = run_single_step(client, json_mgr, code, desc, info, is_auto)
            if not ok:
                break
            _hub_pos = (_hub_pos + 1) % len(_ORDER)
        else:
            print("  Press Enter for next step or [B] Back.")
            time.sleep(0.5)


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
    print()
    print("  [R] Regenerate  |  [B] Back")
    choice = input("  > ").strip().upper()
    if choice == "R":
        files = generate_m3u(json_mgr)
        print("  -> [OK] Regenerated:")
        print("     Live:   {}".format(files.get("live", "")))
        print("     Movies: {}".format(files.get("movies", "")))
        _cooldown()


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


def handshake_status(json_mgr):
    """Hub row status: pass, failed - xxx, or pending when never run."""
    meta = json_mgr.data.get("_meta", {})
    outcome = meta.get("handshake_status", "")
    if outcome == "pass":
        return "pass"
    if outcome == "failed":
        return "failed - {}".format(meta.get("handshake_reason", "unknown"))
    return "pending"


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
def run_category_scrape_no_probe(client, json_mgr, code, desc):
    """Fetch category list only, no per-category first-page check."""
    params = STEP_PARAMS.get(code)
    if not params:
        return False, ""
    result = client.fetch(params)
    safe_name = desc.replace("=", "_").replace("&", "_").replace(" ", "_")[:40]
    cache = getattr(json_mgr, "cache", None)
    fname, status_str, is_error, is_200 = handle_fetch_result(result, code, safe_name, cache=cache)
    if is_error:
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
    elif code == "A1":
        success, step_msg = run_handshake_step(client, json_mgr)
    elif code in ("C5", "D4"):
        if code == "C5":
            success = fetch_all_live_no_viewer(client, json_mgr)
        elif code == "D4":
            success = fetch_all_movies_no_viewer(client, json_mgr)
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
# MAIN
# ============================================================
def main():
    while True:
        portal, mac, json_mgr, is_restored = run_resume_or_new()

        if not portal or not mac or not json_mgr:
            return

        client = Mac2ListPortal(portal, mac)

        # No handshake here — first screen just moves to the hub.
        # Handshake runs only on hub row [1] click.
        # Enter Hub (returns on [B] Back → session selection)
        hub_loop(client, json_mgr, is_restored)


if __name__ == "__main__":
    main()