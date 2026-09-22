import re


# ============================================================
# SHARED HELPERS
# ============================================================
def _extract_cmd(data):
    """Pull the resolved cmd string out of a create_link response, or None."""
    if not data or not isinstance(data, dict):
        return None
    js_val = data.get("js")
    if isinstance(js_val, str):
        return js_val
    if isinstance(js_val, dict):
        return js_val.get("cmd")
    return None


def _strip_ffmpeg(url):
    return url[7:] if url.startswith("ffmpeg ") else url


# ============================================================
# LINK RESOLUTION  (menu 2/3/4 — resolve stream URLs)
# ============================================================
def resolve_live(client, json_mgr, item):
    """Resolve a live channel. Mutates item['resolved_url'], saves.
    Returns (resolved_url, raw_response)."""
    cmd = item.get("cmd", "")
    if not cmd:
        return "", None
    params = {"type": "itv", "action": "create_link", "cmd": cmd, "series": "",
              "forced_storage": "undefined", "disable_ad": "0", "download": "0", "JsHttpRequest": "1-xml"}
    result = client.fetch(params)
    data = result.get("_data")
    server_cmd = _extract_cmd(data)
    resolved_url = server_cmd or ""
    if server_cmd:
        m_res = re.search(r"[?&]stream=([^&]*)", resolved_url)
        if m_res and m_res.group(1) == "":
            resolved_url = re.sub(r"stream=[^&]*",
                                  "stream=" + str(item.get("id", "")),
                                  resolved_url, count=1)
    resolved_url = _strip_ffmpeg(resolved_url)
    item["resolved_url"] = resolved_url
    json_mgr.save()
    return resolved_url, (data if data else cmd)


def resolve_vod(client, json_mgr, item):
    """Resolve a VOD movie. Mutates item['resolved_url'], saves.
    Returns (resolved_url, raw_response) — raw_response None on failure."""
    cmd = item.get("cmd", "")
    if not cmd:
        return "", None
    params = {"type": "vod", "action": "create_link", "cmd": cmd, "series": "",
              "forced_storage": "undefined", "disable_ad": "0", "download": "0", "JsHttpRequest": "1-xml"}
    result = client.fetch(params)
    data = result.get("_data")
    resolved_url = _extract_cmd(data)
    raw = None
    if resolved_url:
        resolved_url = _strip_ffmpeg(resolved_url)
        item["resolved_url"] = resolved_url
        json_mgr.save()
        raw = data
    return resolved_url or "", raw


def resolve_episode(client, json_mgr, series_obj, season_name, ep_num, cmd):
    """Resolve one episode URL. Stores season['resolved_ep_N'], saves.
    Returns (resolved_url, raw_response) — raw_response None on failure."""
    params = {
        "type": "vod", "action": "create_link", "cmd": cmd,
        "series": str(ep_num),
        "forced_storage": "undefined", "disable_ad": "0",
        "download": "0", "JsHttpRequest": "1-xml"
    }
    result = client.fetch(params)
    data = result.get("_data")
    resolved_url = _extract_cmd(data) or ""
    if resolved_url:
        resolved_url = _strip_ffmpeg(resolved_url)
        for season in series_obj.get("seasons", []):
            if season.get("name") == season_name:
                season["resolved_ep_{}".format(ep_num)] = resolved_url
                break
        json_mgr.save()
        return resolved_url, data
    return "", None