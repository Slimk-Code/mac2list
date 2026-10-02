import os
import subprocess

from .config import OUTPUT_DIR


def resolved_channels(json_mgr):
    """Live channels that already have a resolved stream URL."""
    return [
        ch
        for cat in json_mgr.data["live"].get("categories", [])
        for ch in cat.get("channels", [])
        if ch.get("resolved_url")
    ]


def resolved_movies(json_mgr):
    """VOD movies that already have a resolved stream URL."""
    return [
        m
        for cat in json_mgr.data["movies"].get("categories", [])
        for m in cat.get("items", [])
        if m.get("resolved_url")
    ]


def _series_has_resolved(s):
    """True when any season of *s* has a resolved episode URL."""
    return any(
        se.get("resolved_ep_{}".format(ep))
        for se in s.get("seasons", [])
        for ep in se.get("episodes", [])
    )


def resolved_series(json_mgr):
    """Series that have at least one resolved episode."""
    return [
        s
        for cat in json_mgr.data["series"].get("categories", [])
        for s in cat.get("items", [])
        if _series_has_resolved(s)
    ]


def series_episode_counts(s):
    """Return (resolved_count, total_count) of episodes for a series."""
    seasons = s.get("seasons", [])
    total = sum(len(se.get("episodes", [])) for se in seasons)
    resolved = sum(
        1
        for se in seasons
        for ep in se.get("episodes", [])
        if se.get("resolved_ep_{}".format(ep))
    )
    return resolved, total


def play_in_vlc(vlc_path, urls, names=None):
    """Play URLs in VLC. Single URL -> direct. Multiple -> temp M3U (overwritten each time)."""
    if not vlc_path or not urls:
        return
    if len(urls) == 1:
        subprocess.Popen([vlc_path, urls[0]])
    else:
        lines = ["#EXTM3U"]
        for i, url in enumerate(urls):
            label = names[i] if names and i < len(names) else "Track {}".format(i + 1)
            lines.append("#EXTINF:-1,{}".format(label))
            lines.append(url)
        tmp = os.path.join(OUTPUT_DIR, "temp_series_vlc.m3u")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        subprocess.Popen([vlc_path, tmp])