# ============================================================
# HEALTH CHECK  —  pure logic, no print/input/screen code.
# Progress is reported through an optional callback so every
# UI shell stays a thin renderer.
# ============================================================
import os
import shutil

import requests

from .config import OUTPUT_DIR

CHECK_TIMEOUT = 10


def check_link(url, timeout=CHECK_TIMEOUT):
    """Open one collected link, read the first chunk only.
    Returns (alive_bool, reason)."""
    try:
        resp = requests.get(url, timeout=timeout, stream=True,
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


def collect_link_sides(session_id):
    """Read saved m3u links per side. Returns {'live': [...], 'vod': [...]}."""
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
    return sides


def run_check_links(client, json_mgr, progress=None):
    """Check m3u links per side up to 50 alive each, persist alive shares.

    progress(opened, total, fail_count) is called after each check.
    Returns True when at least one link is alive.
    """
    from .storage import save_step_outcome

    _ = client
    session_id = json_mgr.cache.session_id
    sides = collect_link_sides(session_id)
    total = len(sides["live"]) + len(sides["vod"])
    meta = json_mgr.data.setdefault("_meta", {})
    if total == 0:
        meta["check_alive"] = 0
        meta["check_total"] = 0
        save_step_outcome(json_mgr, "check", False, "empty")
        return False
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
            ok, err = check_link(url)
            if ok:
                alive += 1
                side_alive[folder] += 1
            else:
                fail_count += 1
                if not first_err:
                    first_err = err or "unknown"
            if progress:
                progress(opened, total, fail_count)
    meta["check_alive"] = alive
    meta["check_total"] = total
    meta["check_live_alive"] = side_alive["live"]
    meta["check_vod_alive"] = side_alive["vod"]
    if alive == 0:
        save_step_outcome(json_mgr, "check", False, first_err)
    else:
        save_step_outcome(json_mgr, "check", True)
        src_root = os.path.join(OUTPUT_DIR, "dead", session_id)
        dst_root = os.path.join(OUTPUT_DIR, "success", session_id)
        if os.path.isdir(dst_root):
            shutil.rmtree(dst_root)
        if os.path.isdir(src_root):
            shutil.move(src_root, dst_root)
    return alive > 0
