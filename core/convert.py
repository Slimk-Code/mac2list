import os

from .config import OUTPUT_DIR


def generate_m3u(json_mgr):
    """G1 engine: generate one M3U per section. Returns dict of section -> output path."""
    session_id = json_mgr.cache.session_id
    out_dir = os.path.join(OUTPUT_DIR, session_id)
    os.makedirs(out_dir, exist_ok=True)

    files = {}

    # LIVE
    lines = ["#EXTM3U"]
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
    live_path = os.path.join(out_dir, "{}_LIVE.m3u".format(session_id))
    with open(live_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files["live"] = live_path

    # MOVIES
    lines = ["#EXTM3U"]
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
    movie_path = os.path.join(out_dir, "{}_MOVIE.m3u".format(session_id))
    with open(movie_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files["movies"] = movie_path

    # SERIES
    lines = ["#EXTM3U"]
    for cat in json_mgr.data["series"].get("categories", []):
        for item in cat.get("items", []):
            series_name = item.get("name", "Unknown")
            for season in item.get("seasons", []):
                season_name = season.get("name", "Unknown")
                for ep in season.get("episodes", []):
                    url = season.get("resolved_ep_{}".format(ep), "")
                    if not url:
                        continue
                    title = "{} - {} E{:02d}".format(series_name, season_name, ep)
                    lines.append('#EXTINF:-1 tvg-name="{}" group-title="Series",{}'.format(
                        series_name, title))
                    lines.append(url)
    series_path = os.path.join(out_dir, "{}_SERIE.m3u".format(session_id))
    with open(series_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files["series"] = series_path

    return files