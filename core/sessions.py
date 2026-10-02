import os
import json
import re
import glob

from .config import SESSION_DIR, CACHE_DIR, DATABASE_FILE


def make_session_id(base_url, mac):
    """Create a filesystem-safe session identifier from portal URL + MAC."""
    clean = base_url.rstrip("/").replace("https://", "").replace("http://", "")
    safe_portal = re.sub(r"[^a-zA-Z0-9.\-]", "_", clean)
    safe_mac = mac.upper().replace(":", "")
    return f"{safe_portal}_{safe_mac}"


def _read_json(path):
    """Return parsed JSON dict, or None on missing/corrupt file."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path, data):
    """Write a JSON dict, creating the parent folder if needed."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def read_database():
    """Return flat list of {portal, mac, kind} entries from data/database.json.
    Active and pending MACs are listed; archived MACs are skipped.
    Empty list when the database file is missing or corrupt."""
    data = _read_json(DATABASE_FILE)
    if not data or not isinstance(data, dict):
        return []
    portals = data.get("portals") or []
    if not isinstance(portals, list):
        return []
    entries = []
    for group in portals:
        if not isinstance(group, dict) or not group.get("portal"):
            continue
        portal = group["portal"]
        active = group.get("active_mac", "")
        if isinstance(active, str) and active:
            entries.append({"portal": portal, "mac": active, "kind": "active"})
        pending = group.get("pending_macs") or []
        if isinstance(pending, list):
            for m in pending:
                if isinstance(m, str) and m:
                    entries.append({"portal": portal, "mac": m, "kind": "pending"})
    return entries


def database_sessions():
    """Database entries shaped for the app. The expiry (phone) is pulled from
    the matching saved session file, since the database holds only portal+MAC."""
    sessions = []
    for e in read_database():
        session_id = make_session_id(e["portal"], e["mac"])
        session = _read_json(os.path.join(SESSION_DIR, f"{session_id}.json"))
        phone = ""
        if session is not None and isinstance(session, dict):
            phone = (session.get("account") or {}).get("phone", "")
        sessions.append({
            "file": "",
            "portal": e["portal"],
            "mac": e["mac"],
            "phone": phone,
            "kind": e.get("kind", "active"),
        })
    return sessions


def register_session(portal, mac):
    """Add a portal+MAC entry in data/database.json (creates the
    file the first time a new session is made). Unknown MACs land in
    pending_macs: visible but locked, never auto-scanned."""
    data = _read_json(DATABASE_FILE)
    if not data or not isinstance(data, dict):
        data = {"portals": []}
    portals = data.get("portals")
    if not isinstance(portals, list):
        portals = []
        data["portals"] = portals
    group = next((g for g in portals if isinstance(g, dict) and g.get("portal") == portal), None)
    if group is None:
        group = {"portal": portal, "active_mac": "", "pending_macs": [], "Archive_mac": []}
        portals.append(group)
    if group.get("active_mac") == mac:
        return
    if mac in (group.get("Archive_mac") or []):
        return
    pending = group.get("pending_macs")
    if not isinstance(pending, list):
        pending = []
        group["pending_macs"] = pending
    if mac not in pending:
        pending.append(mac)
    _write_json(DATABASE_FILE, data)


def cleanup_orphans(entries):
    """Delete every saved session file (and its cache folder) whose portal+MAC
    is not listed in the database. The database is the main source; nothing in
    it is touched."""
    known = {(e["portal"], e["mac"]) for e in entries}
    for f in glob.glob(os.path.join(SESSION_DIR, "*.json")):
        session = _read_json(f)
        if session is not None:
            meta = session.get("_meta", {})
            portal = meta.get("portal")
            mac = meta.get("mac")
            if portal and mac and (portal, mac) in known:
                continue
        session_id = os.path.splitext(os.path.basename(f))[0]
        try:
            os.remove(f)
        except Exception:
            pass
        cache_folder = os.path.join(CACHE_DIR, session_id)
        try:
            if os.path.isdir(cache_folder):
                for root, dirs, files in os.walk(cache_folder, topdown=False):
                    for name in files:
                        os.remove(os.path.join(root, name))
                    for name in dirs:
                        os.rmdir(os.path.join(root, name))
                os.rmdir(cache_folder)
        except Exception:
            pass