# ============================================================
# BATCH LIBRARY  —  pure logic, no print/input/screen code.
# Progress is reported through optional callbacks so every
# UI shell stays a thin renderer.
# ============================================================
import json
import os

from .config import OUTPUT_DIR
from .fetch import category_status as _category_status
from .filters import channel_name_clean, channel_name_ok, movie_title_ok
from .resolve import resolve_live, resolve_vod
from .storage import save_step_outcome


def fetch_all_live_no_viewer(client, json_mgr, progress=None):
    """Fetch 1st page of every pending live category first,
    then filter the entire set at once and keep the first 50.

    progress(done, total) is called after each category.
    Returns True on success.
    """
    _, pending, _, _ = _category_status(json_mgr, "live")
    if not pending:
        return True
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
        if progress:
            progress(i + 1, len(pending))
    if not fetched_ids:
        from .hub import fetch_reason
        save_step_outcome(json_mgr, "fetch_live",
                           False, fetch_reason(first_error) if first_error is not None else "unknown")
        return False
    # Phase 2: filter the entire set at once, keep first 50
    kept = [(cid, it) for cid, items in collected for it in items
            if channel_name_ok(it.get("name", it.get("title", "")))]
    kept = kept[:50]
    if len(kept) < 50:
        kept_ids = set(id(it) for _, it in kept)
        clean = [(cid, it) for cid, items in collected for it in items
                 if channel_name_clean(it.get("name", it.get("title", "")))
                 and id(it) not in kept_ids]
        kept = kept + clean[:50 - len(kept)]
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
            save_step_outcome(json_mgr, "fetch_live", False, "no channel")
        else:
            save_step_outcome(json_mgr, "fetch_live", False, "empty")
    else:
        save_step_outcome(json_mgr, "fetch_live", True)
    return True


def fetch_all_movies_no_viewer(client, json_mgr, progress=None):
    """Fetch 1st page of every pending VOD category first,
    then filter the entire set at once and keep the first 50.

    progress(done, total) is called after each category.
    Returns True on success.
    """
    _, pending, _, _ = _category_status(json_mgr, "movies")
    if not pending:
        return True
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
        if progress:
            progress(i + 1, len(pending))
    if not fetched_ids:
        from .hub import fetch_reason
        save_step_outcome(json_mgr, "fetch_movies",
                           False, fetch_reason(first_error) if first_error is not None else "unknown")
        return False
    # Phase 2: filter the entire set at once, keep first 50
    kept = [(cid, it) for cid, items in collected for it in items
            if movie_title_ok(it.get("name", it.get("title", "")))]
    kept = kept[:50]
    if len(kept) < 50:
        kept_ids = set(id(it) for _, it in kept)
        clean = [(cid, it) for cid, items in collected for it in items
                 if not movie_title_ok(it.get("name", it.get("title", "")))
                 and id(it) not in kept_ids]
        kept = kept + clean[:50 - len(kept)]
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
            save_step_outcome(json_mgr, "fetch_movies", False, "no channel")
        else:
            save_step_outcome(json_mgr, "fetch_movies", False, "empty")
    else:
        save_step_outcome(json_mgr, "fetch_movies", True)
    return True


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


def resolve_all_no_viewer(client, json_mgr, step_code, section, bucket, action_type, folder,
                          progress=None):
    """Resolve every pending item at once, no picker, then save section m3u.

    progress(done, total, fail_count) is called after each item.
    Returns (ok, empty): ok True on success, empty True when no items exist.
    """
    items = []
    for cat in json_mgr.data[section].get("categories", []):
        for it in cat.get(bucket, []):
            items.append(it)
    if not items:
        return False, True

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
        if progress:
            progress(i + 1, total, fail_count)
    if cache:
        step_data = {
            "_status": "done",
            "_total_resolved": len(existing_responses),
            "_total_failed": fail_count,
            "responses": existing_responses
        }
        cache.write_step(step_code, step_data)

    save_section_m3u(json_mgr, section, folder)
    rkey = "resolve_live" if section == "live" else "resolve_movies"
    dead = fail_count - nocmd_count
    live = total - fail_count
    if total == 0:
        save_step_outcome(json_mgr, rkey, True)
    elif live > 0 and dead > 0:
        save_step_outcome(json_mgr, rkey, True, "{}%".format(int(live * 100 / total)))
    elif dead > 0:
        save_step_outcome(json_mgr, rkey, False, "{} dead".format(dead))
    elif nocmd_count > 0:
        save_step_outcome(json_mgr, rkey, False, "no cmd")
    else:
        save_step_outcome(json_mgr, rkey, True)
    return True, False


def split_pending_resolved(items, key="resolved_url"):
    """Split items into (pending, resolved, ordered) by presence of key."""
    pending = [it for it in items if not it.get(key)]
    resolved = [it for it in items if it.get(key)]
    return pending, resolved, pending + resolved


def collect_series_items(json_mgr):
    """All series items across categories."""
    items = []
    for cat in json_mgr.data.get("series", {}).get("categories", []):
        for item in cat.get("items", []):
            items.append(item)
    return items


def collect_series_with_episodes(json_mgr):
    """Series items that have seasons with episodes."""
    items = []
    for cat in json_mgr.data.get("series", {}).get("categories", []):
        for s in cat.get("items", []):
            seasons = s.get("seasons", [])
            if seasons and any(season.get("episodes") for season in seasons):
                items.append(s)
    return items


def collect_live_items(json_mgr):
    """All live channels across categories."""
    items = []
    for cat in json_mgr.data.get("live", {}).get("categories", []):
        for ch in cat.get("channels", []):
            items.append(ch)
    return items


def collect_movie_items(json_mgr):
    """All VOD movies across categories."""
    items = []
    for cat in json_mgr.data.get("movies", {}).get("categories", []):
        for m in cat.get("items", []):
            items.append(m)
    return items


def is_series_resolved(series):
    """True when any season has a resolved_ep_ key."""
    for season in series.get("seasons", []):
        for key in season:
            if key.startswith("resolved_ep_"):
                return True
    return False


def flatten_all_episodes(pending_series):
    """Flatten pending series seasons into episode dicts."""
    all_episodes = []
    for s in pending_series:
        for season in s.get("seasons", []):
            s_name = season.get("name", "Unknown")
            s_cmd = season.get("cmd", "")
            for ep_num in season.get("episodes", []):
                all_episodes.append({
                    "season_name": s_name,
                    "episode_num": ep_num,
                    "cmd": s_cmd,
                    "series_name": s.get("name", "Unknown"),
                    "series_obj": s,
                })
    return all_episodes


def flatten_series_episodes(series):
    """Flatten one series seasons into episode dicts."""
    episodes = []
    for season in series.get("seasons", []):
        season_name = season.get("name", "Unknown")
        season_cmd = season.get("cmd", "")
        for ep_num in season.get("episodes", []):
            episodes.append({
                "season_name": season_name,
                "episode_num": ep_num,
                "cmd": season_cmd,
            })
    return episodes


def resolve_items_batch(client, json_mgr, step_code, items, action_type, progress=None):
    """Resolve a picked list of items. Shared by every UI resolver picker.

    progress(done, total, fail_count) is called after each item.
    Returns (resolved_ok_count, fail_count). Step file write is skipped when
    the manager runs transient (memory-only).
    """
    cache = getattr(json_mgr, "cache", None)
    existing_responses = []
    if cache and not getattr(json_mgr, "transient", False):
        step_path = cache.step_path(step_code)
        if step_path and os.path.exists(step_path):
            try:
                with open(step_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    existing_responses = existing.get("responses", [])
            except Exception:
                pass
    fail_count = 0
    for i, item in enumerate(items):
        if action_type == "itv":
            resolved_url, raw = resolve_live(client, json_mgr, item)
            existing_responses.append({
                "id": item.get("id", ""),
                "name": item.get("name", item.get("title", "")),
                "raw_response": raw if raw else item.get("cmd", ""),
            })
            if not resolved_url:
                fail_count += 1
        else:
            resolved_url, raw = resolve_vod(client, json_mgr, item)
            if resolved_url:
                existing_responses.append({
                    "id": item.get("id", ""),
                    "name": item.get("name", item.get("title", "")),
                    "raw_response": raw,
                })
            else:
                fail_count += 1
        if progress:
            progress(i + 1, len(items), fail_count)
    if cache:
        step_data = {
            "_status": "done",
            "_total_resolved": len(existing_responses),
            "_total_failed": fail_count,
            "responses": existing_responses,
        }
        cache.write_step(step_code, step_data)
    return len(items) - fail_count, fail_count
