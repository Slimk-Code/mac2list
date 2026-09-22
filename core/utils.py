import re
from datetime import datetime


def time_ago(iso_str):
    if not iso_str:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(iso_str)
        delta = datetime.now() - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return "just now"
        mins = secs // 60
        if mins < 60:
            return "{} min ago".format(mins)
        hours = mins // 60
        if hours < 24:
            return "{} hours ago".format(hours)
        days = hours // 24
        return "{} days ago".format(days)
    except Exception:
        return "Unknown"


def expiry_label(phone):
    if not phone:
        return ""
    try:
        dt = datetime.strptime(str(phone).strip(), "%B %d, %Y, %I:%M %p")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return ""


def is_valid_mac(mac):
    return bool(re.match(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$", mac))


def domain_of(url):
    """Return the domain of a portal URL, without scheme, path or port."""
    if not url:
        return ""
    host = str(url).strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host