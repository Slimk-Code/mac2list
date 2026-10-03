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


def _group_type(group):
    """App owning a database group: 'xtream' or 'mac' (untyped = mac)."""
    return group.get("type") if group.get("type") == "xtream" else "mac"


def read_database(app="mac"):
    """Return flat list of {portal, mac, kind} entries from data/database.json.
    All MACs sharing a portal live in the macs[] list; Xtream logins sharing
    a portal live in the users[] list. Only groups of *app* are listed.
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
        if _group_type(group) != app:
            continue
        portal = group["portal"]
        if app == "xtream":
            users = group.get("users")
            if not isinstance(users, list):
                users = []
            for u in users:
                if isinstance(u, dict) and u.get("username"):
                    entries.append({"portal": portal, "mac": u["username"],
                                    "password": u.get("password", ""),
                                    "kind": "active"})
            continue
        macs = group.get("macs")
        if isinstance(macs, str):
            macs = [macs] if macs else []
        if not isinstance(macs, list):
            macs = []
            if isinstance(group.get("active_mac"), str) and group.get("active_mac"):
                macs.append(group.get("active_mac"))
            for m in (group.get("pending_macs") or []) + (group.get("Archive_mac") or []):
                if isinstance(m, str) and m and m not in macs:
                    macs.append(m)
        for m in macs:
            if isinstance(m, str) and m:
                entries.append({"portal": portal, "mac": m, "kind": "active"})
    return entries


def database_sessions(app="mac"):
    """Database entries shaped for the app. The expiry (phone) is pulled from
    the matching saved session file, since the database holds only portal+MAC."""
    sessions = []
    for e in read_database(app):
        session_id = make_session_id(e["portal"], e["mac"])
        session = _read_json(os.path.join(SESSION_DIR, f"{session_id}.json"))
        phone = ""
        password = e.get("password", "")
        if session is not None and isinstance(session, dict):
            phone = (session.get("account") or {}).get("phone", "")
            password = password or session.get("_meta", {}).get("password", "")
        sessions.append({
            "file": "",
            "portal": e["portal"],
            "mac": e["mac"],
            "password": password,
            "phone": phone,
            "kind": e.get("kind", "active"),
        })
    return sessions


def register_session(portal, mac):
    """Add a portal+MAC entry in data/database.json (creates the
    file the first time a new session is made). All MACs sharing a
    portal are kept in the macs[] list."""
    data = _read_json(DATABASE_FILE)
    if not data or not isinstance(data, dict):
        data = {"portals": []}
    portals = data.get("portals")
    if not isinstance(portals, list):
        portals = []
        data["portals"] = portals
    group = next((g for g in portals if isinstance(g, dict) and g.get("portal") == portal and _group_type(g) == "mac"), None)
    if group is None:
        group = {"portal": portal, "type": "mac", "macs": []}
        portals.append(group)
    macs = group.get("macs")
    if isinstance(macs, str):
        macs = [macs] if macs else []
        group["macs"] = macs
    if not isinstance(macs, list):
        macs = []
        if isinstance(group.get("active_mac"), str) and group.get("active_mac"):
            macs.append(group.get("active_mac"))
        for m in (group.get("pending_macs") or []) + (group.get("Archive_mac") or []):
            if isinstance(m, str) and m and m not in macs:
                macs.append(m)
        group["macs"] = macs
    for key in ("active_mac", "pending_macs", "Archive_mac"):
        group.pop(key, None)
    if mac not in macs:
        macs.append(mac)
    _write_json(DATABASE_FILE, data)


def register_xtream(portal, username, password=""):
    """Add an Xtream login in data/database.json under the users[] key.
    Same portal may hold both a mac group and an xtream group."""
    username = (username or "").strip()
    if not portal or not username:
        return
    data = _read_json(DATABASE_FILE)
    if not data or not isinstance(data, dict):
        data = {"portals": []}
    portals = data.get("portals")
    if not isinstance(portals, list):
        portals = []
        data["portals"] = portals
    group = next((g for g in portals if isinstance(g, dict) and g.get("portal") == portal and _group_type(g) == "xtream"), None)
    if group is None:
        group = {"portal": portal, "type": "xtream", "users": []}
        portals.append(group)
    users = group.get("users")
    if not isinstance(users, list):
        users = []
        group["users"] = users
    for u in users:
        if isinstance(u, dict) and u.get("username") == username:
            u["password"] = password or ""
            break
    else:
        users.append({"username": username, "password": password or ""})
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