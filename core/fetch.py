import json
import os
import time

from .storage import trim_channel, trim_movie, trim_series_item


def _category_list(json_mgr, section):
    """Shared bucket lookup for the three content sections."""
    return json_mgr.data[section]["categories"]


def category_status(json_mgr, section):
    """Shared pending/fetched/failed split of a section's categories.
    Returns (all_cats, pending, fetched_list, failed_list)."""
    cats = _category_list(json_mgr, section)
    getters = {
        "live": (json_mgr.get_live_fetched, json_mgr.get_live_failed),
        "movies": (json_mgr.get_movie_fetched, json_mgr.get_movie_failed),
        "series": (json_mgr.get_series_fetched, json_mgr.get_series_failed),
    }
    get_fetched, get_failed = getters[section]
    fetched = set(get_fetched())
    failed = set(get_failed())
    listed = [c for c in cats if str(c.get("id")) != "*"]
    pending = [
        c for c in listed
        if str(c.get("id")) not in fetched and str(c.get("id")) not in failed
    ]
    fetched_list = [c for c in listed if str(c.get("id")) in fetched]
    failed_list = [c for c in listed if str(c.get("id")) in failed]
    return cats, pending, fetched_list, failed_list


# ============================================================
# CATEGORY BATCH FETCH  (menu 2/3/4 — fetch items)
# ============================================================
def fetch_single_category(client, json_mgr, section, cat_id):
    """Fetch one category (all pages). Returns (success, items_count, failed_pages, params)."""
    param_map = {
        "live": ("itv", "get_ordered_list", "genre"),
        "movies": ("vod", "get_ordered_list", "category"),
        "series": ("series", "get_ordered_list", "category"),
    }
    action_type, action, id_key = param_map[section]
    params = {"type": action_type, "action": action, id_key: cat_id, "p": "1", "JsHttpRequest": "1-xml"}
    if action_type == "vod" or action_type == "series":
        params["fav"] = "0"
        params["sortby"] = "added"
        params["hd"] = "0"
    result = client.fetch_all_pages(params)
    data = result.get("_data")
    failed_pages = []
    if data and isinstance(data, dict):
        # Save raw page data into the section's step file via CacheManager
        cache = getattr(json_mgr, "cache", None)
        if cache:
            step_code = {"live": "C5", "movies": "D4", "series": "E5"}.get(section)
            if step_code:
                step_path = cache.step_path(step_code)
                if step_path:
                    os.makedirs(os.path.dirname(step_path), exist_ok=True)
                    with open(step_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)

        js = data.get("js", {})
        if isinstance(js, dict):
            items = js.get("data", [])
            total_items = js.get("total_items") or len(items)
            failed_pages = js.get("_failed_pages", [])
            if section == "live":
                json_mgr.update_live_channels(cat_id, items, total_items)
            elif section == "movies":
                json_mgr.update_movie_items(cat_id, items, total_items)
            elif section == "series":
                json_mgr.update_series_items(cat_id, items, total_items)
            return True, total_items, failed_pages, params
    if section == "live":
        json_mgr.mark_live_genre_failed(cat_id)
    elif section == "movies":
        json_mgr.mark_movie_category_failed(cat_id)
    elif section == "series":
        json_mgr.mark_series_category_failed(cat_id)
    return False, 0, [], params


def retry_category_pages(client, json_mgr, retry_queue, progress=None):
    """Retry failed pages. queue = [(params_template, [page_numbers], cat_id, section)].
    Returns (retry_ok, retry_still_failed)."""
    all_retry_pages = sum(len(pages) for _, pages, _, _ in retry_queue)
    retry_done = 0
    retry_ok = 0
    retry_still_failed = 0
    for p_template, page_numbers, cat_id, sec in retry_queue:
        for p in page_numbers:
            p_params = dict(p_template)
            p_params["p"] = str(p)
            recovered = False
            for attempt in range(3):
                page_result = client._get(p_params)
                page_data = page_result.get("_data", {})
                if page_data and isinstance(page_data, dict):
                    items = page_data.get("js", {}).get("data", [])
                    if items:
                        existing = _category_list(json_mgr, sec)
                        for cat in existing:
                            if str(cat.get("id")) == str(cat_id):
                                if sec == "live":
                                    cat["channels"].extend([trim_channel(ch) for ch in items])
                                elif sec == "movies":
                                    cat["items"].extend([trim_movie(m) for m in items])
                                elif sec == "series":
                                    cat["items"].extend([trim_series_item(s) for s in items])
                                break
                        json_mgr.save()
                        retry_ok += 1
                        recovered = True
                        break
                if attempt < 2:
                    time.sleep(0.5 * 2)
            retry_done += 1
            if not recovered:
                retry_still_failed += 1
            if progress:
                progress(retry_done, all_retry_pages)
    return retry_ok, retry_still_failed


# ============================================================
# SERIES EPISODES  (E3 — menu 4 Series)
# ============================================================
def fetch_episodes(client, json_mgr, series_items, existing_responses=None, progress=None):
    """Fetch episodes for *series_items*. Appends raw responses into
    existing_responses (in place). Returns (ok_count, fail_count)."""
    if existing_responses is None:
        existing_responses = []
    ok_count = 0
    fail_count = 0
    total = len(series_items)
    for i, item in enumerate(series_items):
        sid = item.get("id", "")
        name = item.get("name", item.get("title", "Unknown"))
        params = {
            "type": "series", "action": "get_ordered_list",
            "movie_id": sid, "season_id": "0", "episode_id": "0",
            "row": "0", "JsHttpRequest": "1-xml"
        }
        result = client.fetch(params)
        data = result.get("_data")
        if data:
            js = data.get("js", {}) if isinstance(data, dict) else {}
            items = js.get("data", []) if isinstance(js, dict) else []
            seasons = []
            for season_item in items:
                seasons.append({
                    "season_id": season_item.get("id", ""),
                    "name": season_item.get("name", "Unknown"),
                    "episodes": season_item.get("series", []),
                    "cmd": season_item.get("cmd", "")
                })
            json_mgr.update_series_episodes(sid, seasons)
            existing_responses.append({
                "id": sid,
                "name": name,
                "raw_response": data
            })
            ok_count += 1
        else:
            fail_count += 1
        if progress:
            progress(i + 1, total, fail_count)
        time.sleep(0.3)

    # Persist the E3 step file
    cache = getattr(json_mgr, "cache", None)
    if cache:
        step_path = cache.step_path("E3")
        if step_path:
            step_data = {
                "_status": "done",
                "_total_fetched": len(existing_responses),
                "_total_failed": fail_count,
                "responses": existing_responses
            }
            os.makedirs(os.path.dirname(step_path), exist_ok=True)
            with open(step_path, "w", encoding="utf-8") as f:
                json.dump(step_data, f, indent=2, ensure_ascii=False)
    return ok_count, fail_count