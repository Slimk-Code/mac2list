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
import sys
import time

from core.config import (
    CACHE_DIR,
    DATA_DIR,
    OUTPUT_DIR,
    SECTIONS,
    SESSION_DIR,
    SETTINGS_STEP_CODES,
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
    register_session as _register_session,
)
from core.storage import (
    JSONManager,
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


def _clear_batch_counter():
    sys.stdout.write(chr(13) + " " * 80 + chr(13))
    sys.stdout.flush()


def _parse_numbers(choice, total):
    """Parse a comma/space separated list of row numbers (1..total)."""
    nums = []
    for part in re.split(r"[,\s]+", choice):
        part = part.strip()
        if part.isdigit():
            n = int(part)
            if 1 <= n <= total:
                nums.append(n)
    return nums


def _paged_picker(title, prepare, name_fn, right_fn, right_label, all_done_line,
                  action_word, divider_label=None, with_failed=False, id_fn=None,
                  page_size=20, start_page=0, header_total_fn=None):
    """Interactive paginated pending-first picker, shared by menus 1-4.

    prepare() -> (items, pending_count, fetched_count); items must already be
    ordered pending-first. Returns (result, end_page) where result is
    "all", "done", None (back), or the list of selected items."""
    if id_fn is None:
        id_fn = lambda it: str(it.get("id", ""))
    page = start_page
    while True:
        items, total_pending, fetched_count = prepare()
        total = len(items)
        max_page = (total - 1) // page_size

        clear_screen()
        print("=" * 60)
        hdr_total = header_total_fn() if header_total_fn else total
        print("   {} — Page {}/{} — {} of {} pending".format(title, page + 1, max_page + 1, total_pending, hdr_total))
        print("=" * 60)
        print()
        print("  {:<4} {:<8} {:<40} {:<10}".format("#", "Status", "Name", right_label))
        print("  " + "-" * 64)
        start = page * page_size
        end = min(start + page_size, total)
        shown_divider = False
        for i in range(start, end):
            item = items[i]
            name = name_fn(item)[:38]
            if i < total_pending:
                status = "[]"
            elif i < total_pending + fetched_count:
                if with_failed:
                    status = "[x]"
                else:
                    if not shown_divider and divider_label:
                        print("\n  --- {} ({}) ---\n".format(divider_label, fetched_count))
                        shown_divider = True
                    status = "[x]"
            else:
                status = "[!]"
            print("  {:<4} {:<8} {:<40} {:<10}".format(i + 1, status, name, right_fn(item)))
        print()
        if total_pending == 0:
            print("  -> [OK] " + all_done_line(total, fetched_count))
            print()
            print("  [Enter] Next page  |  [B] Back")
        else:
            if with_failed:
                help_line = "  [Enter] Next page  |  [A] {} ALL  |  [1-{}] Select #  |  [B] Back".format(action_word, end - start)
            else:
                help_line = "  [A] {} ALL  |  [Enter] Next page  |  [1-{}] Select #  |  [B] Back".format(action_word, end - start)
            print(help_line)
        choice = input("  > ").strip().upper()
        if choice == "A":
            if total_pending > 0:
                return "all", page
        elif choice == "B":
            return ("done" if total_pending == 0 else None), page
        elif choice == "":
            page = (page + 1) if page < max_page else 0
        else:
            nums = _parse_numbers(choice, total)
            if nums and total_pending > 0:
                seen = set()
                selection = []
                for n in nums:
                    item = items[n - 1]
                    key = id_fn(item)
                    if key not in seen:
                        selection.append(item)
                        seen.add(key)
                return selection, page


# ============================================================
# CATEGORY VIEWER (20 per page) with multi-number fetch
# ============================================================
def view_categories(json_mgr, section, client=None):
    """Paginated category viewer showing ONLY pending categories. 20 per page.
    Returns list of category IDs to fetch, "done", or None if back to menu."""
    if section not in ("live", "movies", "series"):
        return []
    title = {"live": "Fetch Channels", "movies": "VOD Categories", "series": "Series Categories"}[section]

    all_cats, pending, fetched_list, failed_list = _category_status(json_mgr, section)

    # Build display list: pending first, then fetched, then failed
    cats = pending + fetched_list + failed_list
    if not cats:
        print("  No categories available.")
        return []

    def prepare():
        return cats, len(pending), len(fetched_list)

    page = 0
    while True:
        result, page = _paged_picker(
            title, prepare,
            lambda c: c.get("title", c.get("name", "Unknown")),
            lambda c: c.get("total_items", 0), "Items",
            lambda total, fcnt: "All categories fetched. {} done, {} failed.".format(len(fetched_list), len(failed_list)),
            "Fetch",
            with_failed=True,
            id_fn=lambda c: str(c.get("id", "")),
            start_page=page,
            header_total_fn=lambda: len(all_cats),
        )
        if result == "done":
            return "done"
        if result is None:
            return None
        if result == "all":
            return [str(c.get("id")) for c in pending]
        return [str(c.get("id")) for c in result]


# ============================================================
# BATCH FETCH — stays pending until all done or ignored
# ============================================================
def batch_fetch_section(client, json_mgr, section):
    """Main batch fetch loop for a section. Auto-shows viewer. Blocks until all done or ignored."""
    if section not in ("live", "movies", "series"):
        return True

    while True:
        _, _, fetched_list, failed_list = _category_status(json_mgr, section)

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
        done_count = len(fetched_list)
        fail_count = len(failed_list)
        total_failed_pages = 0
        retry_queue = []  # [(params_template, page_numbers, cat_id, section)]
        for i, cid in enumerate(to_fetch):
            ok, _, failed_pages, p_template = fetch_single_category(client, json_mgr, section, cid)
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
            retry_ok, retry_still_failed = retry_category_pages(client, json_mgr, retry_queue, progress=progress_bar)
            print("  |  {} OK, {} still failed".format(retry_ok, retry_still_failed))

        # Loop back — auto-refresh viewer with updated pending list


# ============================================================
# ITEMS HANDLER — opens viewer directly, no sub-menu
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

    page = 0
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

        # Recalculate pending/fetched each time we redraw
        pending = [it for it in series_items if not it.get("seasons")]
        fetched = [it for it in series_items if it.get("seasons")]
        items = pending + fetched
        if not items:
            print("  No series available.")
            _cooldown()
            return False

        def prepare():
            return pending, len(pending), len(fetched)

        result, page = _paged_picker(
            "Available Series", prepare,
            lambda it: it.get("name", it.get("title", "Unknown")),
            lambda it: str(it.get("id", ""))[:8], "ID",
            lambda total, fcnt: "All series fetched. {}/{} items.".format(len(fetched), total),
            "Fetch",
            divider_label="Already fetched",
            id_fn=lambda it: it.get("id", ""),
            start_page=page,
        )
        if result == "done":
            return "done"
        if result is None:
            return None
        to_fetch = pending[:] if result == "all" else result
        if not to_fetch:
            continue

        # Fetch episodes with progress bar
        print()

        def _ep_progress(cur, ecnt, fails):
            pct = (cur / ecnt) * 100 if ecnt else 100
            filled = int(30 * cur / ecnt) if ecnt else 30
            bar = "=" * filled + "-" * (30 - filled)
            line = "  Fetching: [{}] {:5.1f}% ({}/{})".format(bar, pct, cur, ecnt)
            if fails:
                line += "  |  {} failed".format(fails)
            sys.stdout.write(chr(13) + line.ljust(80))
            sys.stdout.flush()

        ok_count, fail_count = fetch_episodes(client, json_mgr, to_fetch, existing_responses, progress=_ep_progress)

        print()
        if fail_count:
            print("  -> [OK] Fetched {}/{} series. {} failed.".format(ok_count, len(to_fetch), fail_count))
        else:
            print("  -> [OK] Fetched {}/{} series.".format(ok_count, len(to_fetch)))
        time.sleep(0.5)


# ============================================================
# RESOLVE HANDLER - shows list view, user picks by number
# ============================================================
def _resolve_items_list(client, json_mgr, step_code, items, title, action_type):
    """Shared resolver for C4 and D3. Loops until user presses Back."""
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

    page = 0
    while True:
        # Recalculate pending/resolved each time we redraw
        pending = [it for it in items if not it.get("resolved_url")]
        resolved = [it for it in items if it.get("resolved_url")]
        items = pending + resolved
        if not items:
            print("  No items available.")
            _cooldown()
            return True

        def prepare():
            return pending, len(pending), len(resolved)

        result, page = _paged_picker(
            "Resolve Link", prepare,
            lambda it: it.get("name", it.get("title", "Unknown")),
            lambda it: str(it.get("id", ""))[:8], "ID",
            lambda total, fcnt: "All items resolved. {}/{} items.".format(len(resolved), total),
            "Resolve",
            divider_label="Already resolved",
            id_fn=lambda it: it.get("id", ""),
            start_page=page,
        )
        if result == "done":
            return "done"
        if result is None:
            return None
        to_resolve = pending[:] if result == "all" else result
        if not to_resolve:
            continue

        print()
        fail_count = 0
        for i, item in enumerate(to_resolve):
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

    def _is_series_resolved(s):
        for season in s.get("seasons", []):
            for key in season:
                if key.startswith("resolved_ep_"):
                    return True
        return False

    while True:
        page = 0
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

        # Recalculate pending/resolved each time we redraw
        # A series is "resolved" if it has any resolved_ep_* keys in any season
        pending = [s for s in series_items if not _is_series_resolved(s)]
        resolved = [s for s in series_items if _is_series_resolved(s)]
        series_items = pending + resolved
        if not series_items:
            print("  No series available.")
            _cooldown()
            return False

        def prepare():
            return pending, len(pending), len(resolved)

        def right_fn(s):
            return sum(len(se.get("episodes", [])) for se in s.get("seasons", [])) if "seasons" in s else 0

        result, _ = _paged_picker(
            "Select Series", prepare,
            lambda it: it.get("name", it.get("title", "Unknown")),
            right_fn, "Episodes",
            lambda total, fcnt: "All series episodes resolved. {}/{} items.".format(len(resolved), total),
            "Resolve",
            divider_label="Episodes resolved",
            id_fn=lambda s: s.get("id", ""),
            start_page=page,
        )
        if result == "done":
            return "done"
        if result is None:
            return None
        if result == "all":
            selected_series = "ALL"
        else:
            selected_series = result[0] if result else None

        if selected_series is None:
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

            def _all_progress(cur, ecnt, fails):
                pct = (cur / ecnt) * 100 if ecnt else 100
                filled = int(30 * cur / ecnt) if ecnt else 30
                bar = "=" * filled + "-" * (30 - filled)
                line = "  Resolving: [{}] {:5.1f}% ({}/{})".format(bar, pct, cur, ecnt)
                sys.stdout.write(chr(13) + line.ljust(80))
                sys.stdout.flush()

            for i, ep_dict in enumerate(all_episodes):
                ep_num = ep_dict["episode_num"]
                s_name = ep_dict["season_name"]
                cmd = ep_dict["cmd"]
                s_series = ep_dict["series_obj"]
                resolved_url, raw = resolve_episode(client, json_mgr, s_series, s_name, ep_num, cmd)
                if resolved_url:
                    existing_responses.append({
                        "series_name": ep_dict.get("series_name", ""),
                        "season_name": s_name,
                        "episode_num": ep_num,
                        "raw_response": raw
                    })
                else:
                    fail_count += 1
                _all_progress(i + 1, len(all_episodes), fail_count)
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
            resolved_url, raw = resolve_episode(client, json_mgr, selected_series, s_name, ep_num, cmd)
            if resolved_url:
                existing_responses.append({
                    "series_name": selected_series.get("name", ""),
                    "season_name": s_name,
                    "episode_num": ep_num,
                    "raw_response": raw
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


# ============================================================
# PAGE 1 — RESUME / NEW
# ============================================================
def show_resume_menu(sessions, reveal_new=False, portal="", mac=""):
    """Display landing page. If reveal_new=True, show Portal/MAC inputs inline."""
    clear_screen()
    print("=" * 60)
    print("   mac2list v1.2")
    print("=" * 60)
    print()

    # Shared restore entry — the viewer opens on demand
    if sessions:
        print("  [1] Restore session")
        print("      {} saved".format(len(sessions)))
        print()

    # New session
    next_num = 2 if sessions else 1
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
# PAGE 1b — RESTORE SESSION VIEWER (paged, pick one)
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


def _select_restore_session(sessions):
    """Show all saved sessions in a paged list. Returns chosen session dict or None."""
    if not sessions:
        print("  No saved sessions.")
        _cooldown()
        return None

    header_line = "  {:<4} {:<24} {:<19} {}".format("#", "Portal", "MAC", "Expiry")

    def row_fmt(s, idx):
        portal = _domain_of(s.get("portal", ""))[:24]
        mac = str(s.get("mac", ""))[:19]
        expiry = _expiry_label(s.get("phone", ""))
        return "  {:<4} {:<24} {:<19} {}".format(idx, portal, mac, expiry if expiry else "—")

    return _select_paginated(sessions, "Restore Session", header_line, row_fmt)


def run_resume_or_new():
    """Page 1: Clean landing; Restore opens the saved-sessions viewer.
    Returns (portal, mac, json_mgr, is_restored)."""
    sessions = _database_sessions()
    _cleanup_orphans(sessions)
    portal = None
    mac = None
    json_mgr = None
    is_restored = False

    while True:
        # Phase 1: Show menu, get choice
        choice, _, _ = show_resume_menu(sessions)

        if choice == "Q":
            print("  Quitting...")
            sys.exit(0)

        # Restore entry opens the viewer
        if sessions and choice == "1":
            session = _select_restore_session(sessions)
            if session is None:
                continue
            portal = session["portal"]
            mac = session["mac"]
            json_mgr = JSONManager(portal, mac)
            is_restored = True
            break

        # New session entry
        if choice == ("2" if sessions else "1"):
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
            if not is_valid_mac(mac):
                print("  [!] Invalid MAC address format. Use format: 00:1A:79:XX:XX:XX")
                input("  Press Enter to retry...")
                continue
            os.makedirs(DATA_DIR, exist_ok=True)
            os.makedirs(SESSION_DIR, exist_ok=True)
            os.makedirs(CACHE_DIR, exist_ok=True)
            json_mgr = JSONManager(portal, mac)
            json_mgr.set_meta(portal, mac)
            _register_session(portal, mac)
            break

        print("  Invalid choice.")
        time.sleep(0.5)

    return portal, mac, json_mgr, is_restored


# ============================================================
# PAGE 2 — MAIN HUB
# ============================================================
def show_hub_header(json_mgr):
    """Print hub header/menu without input prompt."""
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

    # Watch counts
    live_count, movie_count, series_count = resolved_counts(json_mgr)

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


def show_hub(json_mgr):
    """Display Main Hub. Returns user choice string."""
    show_hub_header(json_mgr)
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

    live_count, movie_count, series_count = resolved_counts(json_mgr)

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
        def _probe_bar(cur, total):
            progress_bar(cur, total, prefix="  Loading Categories: ")
        success, step_msg = run_auto_fetch_step(client, json_mgr, code, desc, probe_progress=_probe_bar)
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
        cache = getattr(json_mgr, "cache", None)
        success, step_msg = unlock(client, cache)
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
    items = _resolved_channels(json_mgr)

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
    items = _resolved_movies(json_mgr)

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
    items = _resolved_series(json_mgr)

    if not items:
        print("  No series available. Fetch series first (Scrape → Series).")
        _cooldown()
        return

    def fmt(item, idx):
        name = item.get("name", "Unknown")[:50]
        resolved, total_eps = _series_episode_counts(item)
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
            print("  -> Returning to session selection...")
            time.sleep(1)
            continue

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