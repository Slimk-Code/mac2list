# ============================================================
# SESSION STATUS  —  pure logic, no print/input/screen code.
# Used by every UI shell (Advanced, Simple) through core only.
# ============================================================
import json
import os
from datetime import datetime

from .config import SESSION_DIR
from .sessions import make_session_id


def session_data(session):
    """Full saved session dict for a portal entry, {} when missing."""
    try:
        session_id = make_session_id(session.get("portal", ""), session.get("mac", ""))
        path = os.path.join(SESSION_DIR, session_id + ".json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def session_meta(session):
    """Saved session meta dict for a portal entry, {} when missing."""
    meta = session_data(session).get("_meta", {})
    return meta if isinstance(meta, dict) else {}


def portal_status(session):
    """HTTP first, checker share next, softer failures after, fresh states last."""
    data = session_data(session)
    meta = data.get("_meta", {})
    if not isinstance(meta, dict):
        meta = {}
    if meta.get("handshake_status") == "failed":
        return "Handshake - {}".format(meta.get("handshake_reason", "unknown"))

    if meta.get("scrape_status") == "failed" and meta.get("scrape_reason") == "no category":
        return "No category"

    reasons = []
    for key in ("scrape_live", "fetch_live", "resolve_live",
                "scrape_movies", "fetch_movies", "resolve_movies",
                "check"):
        if meta.get(key + "_status") == "failed":
            reasons.append(meta.get(key + "_reason", "unknown") or "unknown")
    for reason in reasons:
        if reason.startswith("HTTP"):
            return reason

    try:
        check_alive = int(meta.get("check_alive", 0) or 0)
    except (TypeError, ValueError):
        check_alive = 0
    try:
        check_total = int(meta.get("check_total", 0) or 0)
    except (TypeError, ValueError):
        check_total = 0
    try:
        check_live_alive = int(meta.get("check_live_alive", 0) or 0)
    except (TypeError, ValueError):
        check_live_alive = 0
    try:
        check_vod_alive = int(meta.get("check_vod_alive", 0) or 0)
    except (TypeError, ValueError):
        check_vod_alive = 0
    if check_live_alive >= 50 and check_vod_alive >= 50:
        return "health score - 100%"
    if check_alive > 0 and check_total > 0:
        return "health score - {}%".format(int(check_alive * 100 / check_total))
    if "timeout" in reasons:
        return "timeout"
    if "no channel" in reasons:
        return "No channel"
    if reasons:
        return "Broken data"
    ran = [meta.get("handshake_status"), meta.get("scrape_status"),
           meta.get("fetch_live_status"), meta.get("fetch_movies_status"),
           meta.get("resolve_live_status"), meta.get("resolve_movies_status"),
           meta.get("check_status")]
    if any(r in ("pass", "failed") for r in ran):
        return "No Data"
    return "-"


def health_status(meta):
    """Health text for a live _meta dict. Same 50+50 capped rule everywhere."""
    if not isinstance(meta, dict):
        meta = {}
    try:
        alive = int(meta.get("check_alive", 0) or 0)
    except (TypeError, ValueError):
        alive = 0
    try:
        total = int(meta.get("check_total", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        live = int(meta.get("check_live_alive", 0) or 0)
    except (TypeError, ValueError):
        live = 0
    try:
        vod = int(meta.get("check_vod_alive", 0) or 0)
    except (TypeError, ValueError):
        vod = 0
    if live >= 50 and vod >= 50:
        return "100%"
    if total > 0 and alive > 0:
        return "{}%".format(int(alive * 100 / total))
    if meta.get("check_status") == "failed":
        return "failed - {}".format(meta.get("check_reason", "unknown"))
    return "-"


def portal_rank(session):
    """Sort key follows the expiry date: latest expiry on top, then
    scraped-but-dateless ('unknown'), then never scraped ('-')."""
    if portal_status(session) == "-":
        return (2, 0, 0)
    try:
        ts = int(datetime.strptime(
            str(session.get("phone", "")).strip(),
            "%B %d, %Y, %I:%M %p").timestamp())
    except Exception:
        ts = 0
    if ts:
        return (0, -ts, 0)
    return (1, 0, 0)
