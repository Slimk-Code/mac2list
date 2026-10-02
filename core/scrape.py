import time


# ============================================================
# SCRAPE / PROBING  (menu 1 — Scrape Categories)
# ============================================================
def probe_categories(client, json_mgr, section, progress=None):
    """Probe per-category item counts. `progress(current, total)` optional."""
    if section == "live":
        cats = json_mgr.data["live"].get("categories", [])
        action_type = "itv"
        action = "get_ordered_list"
        id_key = "genre"
        mark_fn = json_mgr.mark_live_genre_probed
        grand_fn = json_mgr.set_live_grand_total
    elif section == "movies":
        cats = json_mgr.data["movies"].get("categories", [])
        action_type = "vod"
        action = "get_ordered_list"
        id_key = "category"
        mark_fn = json_mgr.mark_movie_category_probed
        grand_fn = json_mgr.set_movie_grand_total
    elif section == "series":
        cats = json_mgr.data["series"].get("categories", [])
        action_type = "series"
        action = "get_ordered_list"
        id_key = "category"
        mark_fn = json_mgr.mark_series_category_probed
        grand_fn = json_mgr.set_series_grand_total
    else:
        return

    total = len(cats)
    if total == 0:
        return

    wildcard = None
    for cat in cats:
        if str(cat.get("id")) == "*":
            wildcard = cat
            break
    if wildcard:
        params = {"type": action_type, "action": action, id_key: "*", "p": "1", "JsHttpRequest": "1-xml"}
        result = client.fetch(params)
        data = result.get("_data")
        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                grand_total = js.get("total_items") or 0
                grand_fn(grand_total)

    probed = 0
    for cat in cats:
        cat_id = str(cat.get("id", ""))
        if not cat_id or cat_id == "*":
            continue
        params = {"type": action_type, "action": action, id_key: cat_id, "p": "1", "JsHttpRequest": "1-xml"}
        result = client.fetch(params)
        data = result.get("_data")
        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                total_items = js.get("total_items") or 0
                cat["total_items"] = total_items
                mark_fn(cat_id)
                probed += 1
                if progress:
                    progress(probed, total - 1)
                time.sleep(0.3)
    json_mgr.save()