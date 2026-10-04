#!/usr/bin/env python3
"""
mac2list v1.2 — CLI interface (UI shell only).

All reusable logic lives in the silent core/ package (no print/input/screen
code). This file contains only screens, menus, progress renderers and the
main() entry point. It talks to core, never to the other UI file.
"""
import json
import os
import sys
import time

import core.hub as _core_hub
from core.config import (
    OUTPUT_DIR,
    SECTIONS,
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
    ordered_categories as _ordered_categories,
    retry_category_pages,
)
from core.hub import (
    clean_hub_memory as _clean_hub_memory,
    clear_outcomes_memory,
    persist_failures_only,
    fresh_session_file as _fresh_session_file,
    handshake_status,
    register_new_portal as _register_new_portal,
    reset_scrape_fail_reason,
    row_status as _row_status,
    run_handshake_step,
    run_step_work,
    scan_again as _scan_again,
    side_label as _side_label,
    side_status as _side_status,
    sync_outer_meta,
)
from core.library import (
    collect_live_items as _collect_live_items,
    collect_movie_items as _collect_movie_items,
    collect_series_items as _collect_series_items,
    collect_series_with_episodes as _collect_series_with_episodes,
    flatten_all_episodes as _flatten_all_episodes,
    flatten_series_episodes as _flatten_series_episodes,
    is_series_resolved as _is_series_resolved,
    resolve_items_batch,
    split_pending_resolved as _split_pending_resolved,
)
from core.portal import Mac2ListPortal
from core.ui_cli import (
    clear_batch_counter as _clear_batch_counter,
    clear_screen,
    cooldown as _cooldown,
    countdown as _countdown,
    paint_caption,
    paint_footer,
    paint_gap,
    paint_header,
    paint_prompt,
    paint_rows,
    progress_bar,
)
from core.resolve import (
    resolve_episode,
)
from core.sessions import (
    cleanup_orphans as _cleanup_orphans,
    database_sessions as _database_sessions,
    make_session_id,
)
from core.status import (
    health_status as _health_status,
    portal_rank as _portal_rank,
    portal_status as _portal_status,
    session_data as _session_data,
    session_meta as _session_meta,
)
from core.storage import (
    JSONManager,
)
from core.utils import (
    domain_of as _domain_of,
    expiry_label as _expiry_label,
    is_valid_mac,
    parse_numbers as _parse_numbers,
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
# TERMINAL HELPERS  (thin aliases to core/ui_cli.py)
# ============================================================


# ============================================================
# PAGE 1 — RESTORE SESSION VIEWER (paged, pick one)
# ============================================================
def _select_paginated(items, title, header_line, row_fmt_fn, page_size=20):
    """Paged browse where choosing a number returns that item, or None on Back.
    Rows are numbered with their real position in the list (like the resolver)."""
    total = len(items)
    page = 0
    max_page = (total - 1) // page_size

    def _paint_list():
        clear_screen()
        paint_header("{} — Page {}/{} — {} saved".format(title, page + 1, max_page + 1, total))
        print()

        paint_caption(header_line)

        start = page * page_size
        end = min(start + page_size, total)
        paint_rows([row_fmt_fn(items[i], i + 1) for i in range(start, end)])

        if max_page > 0:
            paint_footer("  [Enter] Next page  |  [1-{}] Scan #  |  [N] New session  |  [Q] Quit".format(total))
        else:
            paint_footer("  [1-{}] Scan #  |  [N] New session  |  [Q] Quit".format(total))

    def _inline_new_session():
        """Portal + MAC prompts below a fresh list repaint."""
        _paint_list()
        print()
        print()
        paint_prompt("Portal")
        try:
            portal = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not portal:
            return None
        paint_prompt("MAC")
        try:
            mac = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not mac:
            return None
        if not is_valid_mac(mac):
            print()
            print("  [!] Invalid MAC address format. Use format: 00:1A:79:XX:XX:XX")
            _cooldown()
            return None
        _fresh_session_file(portal, mac)
        _register_new_portal(portal, mac)
        return (portal, mac)

    while True:
        _paint_list()

        choice = input().strip().upper()
        if choice == "Q":
            return None
        elif choice == "N":
            res = _inline_new_session()
            if res:
                return ("DIRECT", res[0], res[1])
            return "NEW"
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

    header_line = "  {:<4}{:<8}{:<24}{:<20}{}".format("#", "Health", "Portal", "MAC", "Expiry")

    def row_fmt(s, idx):
        _h = _health_status(_session_meta(s))
        badge = _h if _h.endswith('%') else '-'
        portal = _domain_of(s.get("portal", ""))[:24]
        mac = str(s.get("mac", ""))[:19]
        expiry = _expiry_label(s.get("phone", ""))
        if expiry:
            status = expiry
        elif _portal_status(s) == "-":
            status = "-"
        else:
            status = "unknown"
        return "  {:<4}{:<8}{:<24}{:<20}{}".format(idx, badge, portal, mac, status)

    sessions = sorted(sessions, key=_portal_rank)
    picked = _select_paginated(sessions, "Mac2List Advanced v1.2", header_line, row_fmt)
    return picked, sessions


def _empty_list_shell():
    """List shell when the database is empty. Returns NEW, BACK or EMPTY."""
    def _paint_empty():
        clear_screen()
        paint_header("Mac2List Advanced v1.2 — Page 1/1 — 0 saved")
        paint_gap()
        paint_rows(["  No saved sessions."])
        paint_footer("  [N] New session  |  [Q] Quit")

    while True:
        _paint_empty()
        choice = input().strip().upper()
        if choice == "Q":
            return "BACK"
        if choice == "N":
            _paint_empty()
            print()
            print()
            paint_prompt("Portal")
            try:
                portal = input().strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if not portal:
                continue
            paint_prompt("MAC")
            try:
                mac = input().strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if not mac:
                continue
            if not is_valid_mac(mac):
                print()
                print("  [!] Invalid MAC address format. Use format: 00:1A:79:XX:XX:XX")
                _cooldown()
                continue
            _fresh_session_file(portal, mac)
            _register_new_portal(portal, mac)
            return ("DIRECT", portal, mac)


def run_resume_or_new():
    """Scanner viewer loop. Returns (mode, payload). ONE: (portal, mac, idx,
    total). FULL: sorted sessions list. BACK: viewer quit. NEW: session made."""
    while True:
        sessions = _database_sessions()
        _cleanup_orphans(sessions + _database_sessions("xtream"))
        if not sessions:
            outcome = _empty_list_shell()
            if outcome == "BACK":
                return "BACK", None
            if isinstance(outcome, tuple) and outcome[0] == "DIRECT":
                _, portal, mac = outcome
                sessions = _database_sessions()
                idx = next((i + 1 for i, s in enumerate(sessions) if s["portal"] == portal and s["mac"] == mac), len(sessions))
                return "DIRECT", (portal, mac, idx, len(sessions))
            continue

        picked, sessions = _select_restore_session(sessions)
        if picked is None:
            return "BACK", None
        if picked == "FULL":
            return "FULL", sessions
        if picked == "NEW":
            continue
        if isinstance(picked, tuple) and picked[0] == "DIRECT":
            _, portal, mac = picked
            sessions = _database_sessions()
            idx = next((i + 1 for i, s in enumerate(sessions) if s["portal"] == portal and s["mac"] == mac), len(sessions))
            return "DIRECT", (portal, mac, idx, len(sessions))

        idx = sessions.index(picked) + 1
        return "ONE", (picked["portal"], picked["mac"], idx, len(sessions))


# ============================================================
# PAGE 2 — MAIN HUB
# ============================================================
# Sequential hub order: one Enter runs the full order automatically.
_ORDER = ["A1", "LIVE", "VOD", "CHECK"]
_hub_pos = 0


def run_hub_handshake(client, json_mgr, quiet=False):
    """Three screens: work, fresh menu plus pause, menu plus saved line."""
    _, _, desc, _, _ = get_step_info("A1")
    print()
    print("  Executing: A1 — {}".format(desc))
    print()
    success, _ = run_handshake_step(client, json_mgr)
    if success:
        json_mgr.mark_done("A1")
    if not quiet:
        time.sleep(3)
    print()
    print("  -> session saved")
    if not quiet:
        time.sleep(3)
    return success


def show_hub_header(json_mgr):
    """Print hub header/menu without input prompt."""
    clear_screen()
    meta = json_mgr.data.get("_meta", {})
    paint_header("Mac2List Advanced v1.2 — {} — {}".format(
        _domain_of(meta.get("portal", "")) or "Main Hub", meta.get("mac", "")))
    print()
    paint_caption('  #  {:<45} {}'.format('Item', 'Status'))
    paint_rows(['  {} {:<45} {}'.format(">>" if _hub_pos == 0 else "  ", "handshake", handshake_status(json_mgr))])
    print()
    paint_rows(['  {} {:<45} {}'.format(">>" if _hub_pos == 1 else "  ", _side_label("Channels", json_mgr, "C5", "C4"), _side_status(json_mgr, "scrape_live", "C5", "C4"))])
    print()
    paint_rows(['  {} {:<45} {}'.format(">>" if _hub_pos == 2 else "  ", _side_label("Vod", json_mgr, "D4", "D3"), _side_status(json_mgr, "scrape_movies", "D4", "D3"))])
    print()
    paint_rows(['  {} {:<45} {}'.format(">>" if _hub_pos == 3 else "  ", "Health Checker", _row_status(json_mgr, "CHK"))])
    if _hub_full:
        paint_footer("  {} out of {}".format(_hub_title_idx, _hub_title_total))
    else:
        paint_footer("  [Enter] Start | [B] Back")


def show_hub(json_mgr):
    """Display Main Hub. Returns user choice string."""
    show_hub_header(json_mgr)
    return input().strip().upper()


def hub_loop(client, json_mgr, is_restored, quiet=False):
    """Main Hub loop: one Enter runs the full order automatically."""
    global _hub_pos
    _hub_pos = 0
    if not _hub_full and not quiet:
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
            reset_scrape_fail_reason()
            for next_code in ("C2", "C5", "C4"):
                if not quiet:
                    show_hub_header(json_mgr)
                    print()
                else:
                    portal_show_hub_header(json_mgr)
                    print()
                idx, _, desc, info, is_auto = get_step_info(next_code)
                run_single_step(client, json_mgr, next_code, desc, info, is_auto, quiet)
        elif code == "VOD":
            if not quiet:
                show_hub_header(json_mgr)
                print()
            else:
                portal_show_hub_header(json_mgr)
                print()
            idx, _, desc, info, is_auto = get_step_info("D1")
            run_single_step(client, json_mgr, "D1", desc, info, is_auto, quiet)
            meta = json_mgr.data.setdefault("_meta", {})
            if _core_hub.scrape_fail_reason:
                meta["scrape_status"] = "failed"
                meta["scrape_reason"] = _core_hub.scrape_fail_reason
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
                if not quiet:
                    show_hub_header(json_mgr)
                    print()
                else:
                    portal_show_hub_header(json_mgr)
                    print()
                idx, _, desc, info, is_auto = get_step_info("D4")
                run_single_step(client, json_mgr, "D4", desc, info, is_auto, quiet)
                if not quiet:
                    show_hub_header(json_mgr)
                    print()
                else:
                    portal_show_hub_header(json_mgr)
                    print()
                idx, _, desc, info, is_auto = get_step_info("D3")
                run_single_step(client, json_mgr, "D3", desc, info, is_auto, quiet)
        elif code == "CHECK":
            if not quiet:
                show_hub_header(json_mgr)
                print()
            else:
                portal_show_hub_header(json_mgr)
                print()
            ok = run_single_step(client, json_mgr, "CHK", "Health Checker", "", False, quiet)
        else:
            if not quiet:
                show_hub_header(json_mgr)
                print()
            else:
                portal_show_hub_header(json_mgr)
                print()
            idx, _, desc, info, is_auto = get_step_info(code)
            ok = run_single_step(client, json_mgr, code, desc, info, is_auto, quiet)
            if ok and not (json_mgr.data.get("account") or {}).get("phone"):
                _, _, bdesc, binfo, _ = get_step_info("B1")
                if not quiet:
                    show_hub_header(json_mgr)
                    print()
                else:
                    portal_show_hub_header(json_mgr)
                    print()
                run_single_step(client, json_mgr, "B1", bdesc, binfo, True, quiet)
        if not ok:
            if not quiet:
                time.sleep(3)
            break


# ============================================================
# HANDSHAKE FROM HUB (interface only)
# ============================================================
def run_single_step(client, json_mgr, code, desc, info, is_auto, quiet=False):
    """Execute a single step. Screen shell around core run_step_work."""
    if code == "A1":
        return run_hub_handshake(client, json_mgr, quiet)
    print()
    print("  Executing: {} — {}".format(code, desc))
    print()

    def _probe_bar(cur, total):
        progress_bar(cur, total, prefix="  Loading Categories: ")

    def _fetch_bar(done, total):
        line = "  -> Fetching: [{}/{}] done".format(done, total)
        sys.stdout.write(chr(13) + line.ljust(80))
        sys.stdout.flush()
        time.sleep(0.1)

    def _resolve_bar(done, total, fails):
        line = "  -> Resolving: [{}/{}]".format(done, total)
        if fails:
            line += "  |  {} failed".format(fails)
        sys.stdout.write(chr(13) + line.ljust(80))
        sys.stdout.flush()
        time.sleep(0.3)

    def _check_bar(opened, total, fails):
        line = "  -> Checking: [{}/{}]".format(opened, total)
        if fails:
            line += "  |  {} failed".format(fails)
        sys.stdout.write(chr(13) + line.ljust(80))
        sys.stdout.flush()
        time.sleep(0.5)

    success, step_msg, empty = run_step_work(
        client, json_mgr, code, desc,
        probe_progress=_probe_bar, fetch_progress=_fetch_bar,
        resolve_progress=_resolve_bar, check_progress=_check_bar)
    if empty:
        print(step_msg)
        _cooldown()
    elif step_msg:
        print(step_msg)
    _clear_batch_counter()

    if not quiet:
        time.sleep(3)
    print()
    if success:
        json_mgr.mark_done(code)
    print("  -> session saved")
    print()
    if not quiet:
        _cooldown()
    return success


# ============================================================
# PORTAL SCREENS (inlined from mac2list-portal.py, prefixed)
# ============================================================
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
        clear_screen()
        hdr_total = header_total_fn() if header_total_fn else total
        paint_header('{} — Page {}/{} — {} of {} pending'.format(title, page + 1, max_page + 1, total_pending, hdr_total))
        print()
        paint_caption('  {:<4} {:<8} {:<40} {:<10}'.format('#', 'Status', 'Name', right_label))
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
        if total_pending == 0:
            paint_footer('  [Enter] Next page  |  [B] Back')
        else:
            if with_failed:
                help_line = '  [Enter] Next page  |  [A] {} ALL  |  [1-{}] Select #  |  [B] Back'.format(action_word, end - start)
            else:
                help_line = '  [A] {} ALL  |  [Enter] Next page  |  [1-{}] Select #  |  [B] Back'.format(action_word, end - start)
            paint_footer(help_line)
        choice = input().strip().upper()
        if choice == 'A':
            if total_pending > 0:
                return ('all', page)
        elif choice == 'B':
            return ('done' if total_pending == 0 else None, page)
        elif choice == '':
            page = page + 1 if page < max_page else 0
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
                return (selection, page)

def portal_view_categories(json_mgr, section, client=None):
    """Paginated category viewer showing ONLY pending categories. 20 per page.
    Returns list of category IDs to fetch, "done", or None if back to menu."""
    if section not in ('live', 'movies', 'series'):
        return []
    title, all_cats, pending, fetched_list, failed_list, cats = _ordered_categories(json_mgr, section)
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
        _clear_batch_counter()
        print('  -> [OK] {} fetched. {} pages failed across {} categories.'.format(len(to_fetch), total_failed_pages, len(retry_queue)))
        if retry_queue:
            retry_ok, retry_still_failed = retry_category_pages(client, json_mgr, retry_queue, progress=progress_bar)
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
        series_items = _collect_series_items(json_mgr)
        if not series_items:
            print('  No series available. Fetch series first.')
            _countdown()
            return False
        pending = [it for it in series_items if not it.get('seasons')]
        fetched = [it for it in series_items if it.get('seasons')]
        items = pending + fetched
        if not items:
            print('  No series available.')
            _countdown()
            return False

        def prepare():
            return (items, len(pending), len(fetched))
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
        _countdown()
        return False
    page = 0
    while True:
        pending, resolved, items = _split_pending_resolved(items)
        if not items:
            print('  No items available.')
            _countdown()
            return True

        def prepare():
            return (items, len(pending), len(resolved))
        result, page = portal__paged_picker('Resolve Link', prepare, lambda it: it.get('name', it.get('title', 'Unknown')), lambda it: str(it.get('id', ''))[:8], 'ID', lambda total, fcnt: 'All items resolved. {}/{} items.'.format(len(resolved), total), 'Resolve', divider_label='Already resolved', id_fn=lambda it: it.get('id', ''), start_page=page)
        if result == 'done':
            return 'done'
        if result is None:
            return None
        to_resolve = pending[:] if result == 'all' else result
        if not to_resolve:
            continue
        print()

        def _resolve_bar(done, total, fails):
            pct = done / total * 100 if total else 100
            filled = int(30 * done / total) if total else 30
            bar = '=' * filled + '-' * (30 - filled)
            line = '  Resolving: [{}] {:5.1f}% ({}/{})'.format(bar, pct, done, total)
            if fails:
                line += '  |  {} failed'.format(fails)
            sys.stdout.write(chr(13) + line.ljust(80))
            sys.stdout.flush()
            time.sleep(0.3)

        _ok_count, fail_count = resolve_items_batch(
            client, json_mgr, step_code, to_resolve, action_type,
            progress=_resolve_bar)
        print()
        if fail_count:
            print('  -> [OK] Resolved {}/{} items. {} failed.'.format(_ok_count, len(to_resolve), fail_count))
        else:
            print('  -> [OK] Resolved {}/{} items.'.format(len(to_resolve), len(to_resolve)))
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

    while True:
        page = 0
        series_items = _collect_series_with_episodes(json_mgr)
        if not series_items:
            print('  No series available. Fetch series first.')
            _countdown()
            return False
        pending = [s for s in series_items if not _is_series_resolved(s)]
        resolved = [s for s in series_items if _is_series_resolved(s)]
        series_items = pending + resolved
        if not series_items:
            print('  No series available.')
            _countdown()
            return False

        def prepare():
            return (series_items, len(pending), len(resolved))

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
            all_episodes = _flatten_all_episodes(pending)
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
                step_data = {'_status': 'done', '_total_resolved': len(existing_responses), '_total_failed': fail_count, 'responses': existing_responses}
                cache.write_step(step_code, step_data)
            time.sleep(0.5)
            selected_series = None
            continue
        seasons = selected_series.get('seasons', [])
        if not seasons:
            print('  No seasons available for this series.')
            time.sleep(0.5)
            continue
        episodes = _flatten_series_episodes(selected_series)
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
            step_data = {'_status': 'done', '_total_resolved': len(existing_responses), '_total_failed': fail_count, 'responses': existing_responses}
            cache.write_step(step_code, step_data)
        time.sleep(0.5)
        selected_series = None

def portal_run_resolve_step_auto(client, json_mgr, step_code):
    """Resolve links by picking from list view. No manual cmd entry."""
    if step_code == 'C4':
        items = _collect_live_items(json_mgr)
        return portal__resolve_items_list(client, json_mgr, step_code, items, 'Live Channels', 'itv')
    elif step_code == 'D3':
        items = _collect_movie_items(json_mgr)
        return portal__resolve_items_list(client, json_mgr, step_code, items, 'VOD Movies', 'vod')
    elif step_code == 'E4':
        return portal__resolve_episodes(client, json_mgr, step_code)
    else:
        return False

def portal_show_hub_header(json_mgr):
    """Print hub header/menu without input prompt."""
    clear_screen()
    paint_header('Mac2List Advanced v1.2 — Portal Scanner')
    print()
    paint_caption('  [#] {:<35}   {}'.format('Item', 'Status'))
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
    paint_rows(['  [1] {:<35}   {}'.format('Scrape Categories', cat_status)])
    print()
    paint_rows(['  [2] {:<35}   {}'.format('Live Channels', _section_status(json_mgr, 'live', 'ch')),
                '  [3] {:<35}   {}'.format('VOD Movies', _section_status(json_mgr, 'movies', 'movies')),
                '  [4] {:<35}   {}'.format('Series', _section_status(json_mgr, 'series', 'series'))])
    print()
    paint_rows(['  [5] {:<35}   {} ch, {} movies, {} series'.format('Watch', live_count, movie_count, series_count),
                '  [6] {:<35}   {}'.format('Export to m3u', convert_status)])
    paint_footer('  [S] Setting  |  [A] Auth  |  [B] Back')

def portal_show_hub(json_mgr):
    """Display Main Hub. Returns user choice string."""
    portal_show_hub_header(json_mgr)
    return input().strip().upper()

def portal_hub_loop(client, json_mgr, is_restored, first_choice=None):
    """Main Hub loop."""
    global _hub_full, _hub_pos
    first = first_choice
    while True:
        if first is not None:
            choice, first = first, None
        else:
            choice = portal_show_hub(json_mgr)
        if choice == 'B':
            break
        elif choice == '1':
            portal_show_hub_header(json_mgr)
            print()
            cat_codes = ['C2', 'D1', 'E1']
            all_done = all((json_mgr.is_done(c) for c in cat_codes))
            if all_done:
                portal_show_hub_header(json_mgr)
                print()
                print()
                ans = input('  Portal Already scraped. do you want to Re-scrape? [y/n] > ').strip().upper()
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
            _quick = input('\n  do you want to scan the health of your portal before scraping?  [y/n] > ').strip().upper()
            print()
            if _quick in ('', 'Y'):
                _m0 = json_mgr.data.get('_meta', {})
                _tmgr = JSONManager(_m0.get('portal', ''), _m0.get('mac', ''))
                _clean_hub_memory(_tmgr)
                clear_outcomes_memory(_tmgr)
                _tmgr.set_transient(True)
                try:
                    _hub_full = False
                    _hub_pos = 0
                    hub_loop(client, _tmgr, True, True)
                    print()
                    print('  -> [OK] quick scan complete.' if _tmgr.is_done('CHK') else '  -> [!] quick scan stopped.')
                finally:
                    _tmgr.set_transient(False)
                _real = persist_failures_only(
                    _m0.get('portal', ''), _m0.get('mac', ''),
                    _tmgr.data.get('_meta', {}))
                sync_outer_meta(json_mgr.data.setdefault('_meta', {}),
                                _real.data.get('_meta', {}))
                _tphone = (_tmgr.data.get('account') or {}).get('phone', '')
                if _tphone and not (json_mgr.data.get('account') or {}).get('phone', ''):
                    json_mgr.data.setdefault('account', {})['phone'] = _tphone
                    json_mgr.save()
            if not json_mgr.is_done('A1'):
                _, _, _desc, _, _ = get_step_info('A1')
                print()
                print('  Executing: A1 — {}'.format(_desc))
                print()
                _ok, _msg = run_handshake_step(client, json_mgr)
                print(_msg)
                if _ok:
                    json_mgr.mark_done('A1')
                time.sleep(3)
                print()
                print('  -> session saved')
                if not _ok:
                    _countdown()
                    continue
            if not (json_mgr.data.get('account') or {}).get('phone', ''):
                _, _, _bdesc, _binfo, _ = get_step_info('B1')
                portal_show_hub_header(json_mgr)
                print()
                portal_run_single_step(client, json_mgr, 'B1', _bdesc, _binfo, True)
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
            _m3u_files = generate_m3u(json_mgr)
            json_mgr.mark_done('G1')
            print()
            print('  -> [OK] M3U files generated:')
            print('     Live:   {}'.format(_m3u_files.get('live', '')))
            print('     Movies: {}'.format(_m3u_files.get('movies', '')))
            print('     Series: {}'.format(_m3u_files.get('series', '')))
            _cooldown(2)
        elif choice == 'S':
            portal_run_settings_submenu(client, json_mgr)
        elif choice == 'A':
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
        clear_screen()
        if sec_key == 'Live Channels':
            paint_header('Mac2List Advanced v1.2 — Live Channels')
        else:
            paint_header('{}'.format(sec['title']))
        print()
        paint_caption('  [#] {:<45} {}'.format('Item', 'Status'))
        paint_rows(['  [{}] {:<45} {}'.format(j + 1, desc, _step_progress(json_mgr, code))
                    for j, (code, desc, info, _) in enumerate(visible_items)])
        if len(visible_items) > 1:
            paint_footer('  [1-{}] Pick step  |  [B] Back'.format(len(visible_items)))
        else:
            paint_footer('  [1] Pick step  |  [B] Back')
        choice = input().strip().upper()
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
    clear_screen()
    done_count = sum((1 for code in SETTINGS_STEP_CODES if json_mgr.is_done(code)))
    total = len(SETTINGS_STEP_CODES)
    paint_header('Settings — {}/{} done'.format(done_count, total))
    print()
    paint_caption('  {:<3} {:<50} {}'.format('#', 'Item', 'Info'))
    sec = SECTIONS['Settings']
    paint_rows(['  {} {:<50} {}'.format(
        '[x]' if json_mgr.is_done(code) else '[I]' if json_mgr.is_ignored(code) else '[>]',
        desc, info) for j, (code, desc, info, _) in enumerate(sec['items'])])
    paint_footer('  [Enter] Continue next pending  |  [B] Back')

def portal_run_settings_submenu(client, json_mgr):
    """Settings sub-menu loop."""
    while True:
        portal_print_settings_submenu(json_mgr)
        choice = input().strip().upper()
        if choice == 'B':
            break
        elif choice == '':
            next_code = get_next_pending_step(json_mgr, SETTINGS_STEP_CODES)
            if next_code is None:
                print('  -> [OK] All settings steps complete.')
                _countdown()
            else:
                idx, sec_key, desc, info, is_auto = get_step_info(next_code)
                portal_run_single_step(client, json_mgr, next_code, desc, info, is_auto)
        else:
            print('  Invalid choice.')
            time.sleep(0.5)

def portal_run_convert_submenu(json_mgr):
    """Convert action."""
    clear_screen()
    paint_header('Convert')
    paint_gap()
    files = generate_m3u(json_mgr)
    json_mgr.mark_done('G1')
    paint_rows(['  -> [OK] M3U files generated:',
                '     Live:   {}'.format(files.get('live', '')),
                '     Movies: {}'.format(files.get('movies', '')),
                '     Series: {}'.format(files.get('series', ''))])
    paint_footer('  [R] Regenerate  |  [B] Back')
    choice = input().strip().upper()
    if choice == 'R':
        files = generate_m3u(json_mgr)
        print('  -> [OK] Regenerated:')
        print('     Live:   {}'.format(files.get('live', '')))
        print('     Movies: {}'.format(files.get('movies', '')))
        print('     Series: {}'.format(files.get('series', '')))
        _countdown()

def portal_show_watch_submenu(json_mgr):
    """Display Watch sub-menu."""
    clear_screen()
    paint_header('Watch — Browse fetched content')
    print()
    paint_caption('  [#] {:<22}{}'.format('Item', 'Count'))
    live_count, movie_count, series_count = resolved_counts(json_mgr)
    paint_rows(['  [1] {:<22} {} channels'.format('Live Channels', live_count),
                '  [2] {:<22} {} movies'.format('VOD Movies', movie_count),
                '  [3] {:<22} {} series'.format('Series', series_count)])
    paint_footer('  [B] Back')

def portal_run_watch_submenu(json_mgr):
    """Watch sub-menu loop."""
    portal_get_vlc_path(json_mgr)
    while True:
        portal_show_watch_submenu(json_mgr)
        choice = input().strip().upper()
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
    print()
    print('  Executing: {} — {}'.format(code, desc))
    print()
    success = False
    step_msg = ''
    if is_auto:

        def _probe_bar(cur, total):
            progress_bar(cur, total, prefix='  Loading Categories: ')
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
        _countdown()
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
        _countdown()
        return
    page = 0
    max_page = (total - 1) // page_size
    while True:
        clear_screen()
        paint_header('{} — Page {}/{} — {} total'.format(title, page + 1, max_page + 1, total))
        print()
        if len(headers) > 1:
            paint_caption('  {:<4} {}'.format(headers[0], '  '.join(headers[1:])))
        else:
            paint_caption('  ' + headers[0])
        start = page * page_size
        end = min(start + page_size, total)
        paint_rows([row_fmt_fn(items[i], i + 1) for i in range(start, end)])
        if get_urls_fn:
            if max_page > 0:
                if page < max_page:
                    paint_footer('  [Enter] Next page  |  [1-{}] Play  |  [B] Back'.format(end - start))
                else:
                    paint_footer('  [Enter] First page  |  [1-{}] Play  |  [B] Back'.format(end - start))
            else:
                paint_footer('  [1-{}] Play  |  [B] Back'.format(end - start))
        elif max_page > 0:
            if page < max_page:
                paint_footer('  [Enter] Next page  |  [B] Back')
            else:
                paint_footer('  [Enter] First page  |  [B] Back')
        else:
            paint_footer('  [B] Back')
        choice = input().strip().upper()
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
        _countdown()
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
        _countdown()
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
        _countdown()
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


def _run_one_portal(portal, mac, first_choice=None):
    """Open one portal in the portal hub: handshake already ran on the list."""
    json_mgr = JSONManager(portal, mac)
    meta = json_mgr.data.setdefault("_meta", {})
    if not meta.get("portal") or not meta.get("mac"):
        json_mgr.set_meta(portal, mac)
    client = Mac2ListPortal(portal, mac)
    portal_hub_loop(client, json_mgr, True, first_choice)


def main():
    """Boot straight into the scanner list. N creates below the screen."""
    global _hub_full, _hub_pos
    while True:
        mode, payload = run_resume_or_new()
        if mode in ("BACK", "EMPTY"):
            return
        if mode == "FULL":
            todo = [s for s in payload if _scan_again(s)]
            for i, session in enumerate(todo):
                _hub_full = True
                _run_one(session["portal"], session["mac"], i + 1, len(todo))
            _hub_full = False
        elif mode == "DIRECT":
            _hub_full = False
            portal, mac, idx, total = payload
            _run_one_portal(portal, mac, first_choice='1')
        else:
            _hub_full = False
            portal, mac, idx, total = payload
            _run_one_portal(portal, mac)


if __name__ == "__main__":
    main()