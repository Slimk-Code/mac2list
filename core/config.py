# ============================================================
# PATH CONSTANTS
# ============================================================
import os

DATA_DIR     = "data"
SESSION_DIR  = "data/session"
CACHE_DIR    = "data/cache"
OUTPUT_DIR   = "data/output"
DATABASE_FILE = os.path.join(DATA_DIR, "database.json")

# ============================================================
# SECTIONS & STEPS DEFINITION
# ============================================================
SECTIONS = {
    "Auth": {
        "title": "Auth, Profile & Account",
        "items": [
            ("A1", "type=stb&action=handshake", "Auth token", False),
            ("A2", "type=stb&action=get_profile", "STB profile (mac,sn)", True),
            ("B1", "type=account_info&action=get_main_info", "Phone,status,connections", True),
            ("B2", "type=account_info&action=get_info", "Full account details", True),
            ("B3", "type=account_info&action=get_tariff_plans", "Subscription plans", True),
        ]
    },
        "Live Channels": {
        "title": "Live Channels",
        "items": [
            ("C2", "type=itv&action=get_genres", "Channel categories", True),
            ("C5", "type=itv&action=get_ordered_list", "Channels by genre (all pages)", False),
            ("C4", "type=itv&action=create_link", "Resolve live stream URL", False),
        ]
    },
    "VOD Movies": {
        "title": "VOD Movies",
        "items": [
            ("D1", "type=vod&action=get_categories", "VOD categories", True),
            ("D4", "type=vod&action=get_ordered_list", "VOD by category (all pages)", False),
            ("D3", "type=vod&action=create_link", "Resolve VOD stream URL", False),
        ]
    },
    "Series": {
        "title": "Series",
        "items": [
            ("E1", "type=series&action=get_categories", "Series categories", True),
            ("E5", "type=series&action=get_ordered_list", "Series by category (all pages)", False),
            ("E3", "type=series&action=get_ordered_list&movie_id=...", "Episodes list", False),
            ("E4", "type=vod&action=create_link&series=N", "Resolve episode stream URL", False),
        ]
    },
    "Settings": {
        "title": "Settings & Unlock",
        "items": [
            ("F1", "type=settings&action=get", "Portal settings", True),
            ("F2", "type=settings&action=get_parental_lock", "Parental lock status", True),
            ("F3", "type=itv&action=set_parental_lock", "Unlock adult (tests 0000,1234,3333)", False),
        ]
    },
    "Convert": {
        "title": "Convert / Status",
        "items": [
            ("G1", "generate_m3u", "Generate M3U playlists", False),
        ]
    },
}

# ============================================================
# HUB FLOW CONSTANTS
# ============================================================
SCRAPE_SECTION_KEYS = ["Live Channels", "VOD Movies", "Series"]
SETTINGS_SECTION_KEYS = ["Settings"]

SETTINGS_STEP_CODES = []
for sec_key in SETTINGS_SECTION_KEYS:
    for code, _, _, _ in SECTIONS[sec_key]["items"]:
        SETTINGS_STEP_CODES.append(code)


STEP_PARAMS = {
    "A2": {"type": "stb", "action": "get_profile", "JsHttpRequest": "1-xml"},
    "B1": {"type": "account_info", "action": "get_main_info", "JsHttpRequest": "1-xml"},
    "B2": {"type": "account_info", "action": "get_info", "JsHttpRequest": "1-xml"},
    "B3": {"type": "account_info", "action": "get_tariff_plans", "JsHttpRequest": "1-xml"},
    "C2": {"type": "itv", "action": "get_genres", "JsHttpRequest": "1-xml"},
    "D1": {"type": "vod", "action": "get_categories", "JsHttpRequest": "1-xml"},
    "E1": {"type": "series", "action": "get_categories", "JsHttpRequest": "1-xml"},
    "F1": {"type": "settings", "action": "get", "JsHttpRequest": "1-xml"},
    "F2": {"type": "settings", "action": "get_parental_lock", "JsHttpRequest": "1-xml"},
}

FLAT_STEPS = []
for sec_key, sec in SECTIONS.items():
    for code, desc, info, is_auto in sec["items"]:
        FLAT_STEPS.append((sec_key, code, desc, info, is_auto))

# ============================================================
# STEP → FILE MAP  (relative to cache_root)
# ============================================================
STEP_FILE_MAP = {
    "A1": "01_auth/01_handshake.json",
    "A2": "01_auth/02_profile.json",
    "B1": "01_auth/03_account_main.json",
    "B2": "01_auth/04_account_full.json",
    "B3": "01_auth/05_tariff.json",
    "C2": "02_live/01_categories.json",
    "C5": "02_live/02_channels.json",
    "C4": "02_live/03_resolve.json",
    "D1": "03_vod/01_categories.json",
    "D4": "03_vod/02_movies.json",
    "D3": "03_vod/03_resolve.json",
    "E1": "04_series/01_categories.json",
    "E5": "04_series/02_items.json",
    "E3": "04_series/03_episodes.json",
    "E4": "04_series/04_resolve.json",
    "F1": "05_settings/01_portal_settings.json",
    "F2": "05_settings/02_parental_lock.json",
    "F3": "05_settings/03_unlock.json",
    "G1": "06_convert/01_generate.json",
}