# ============================================================
# CONTENT FILTERS  —  pure logic, no print/input/screen code.
# ============================================================

def channel_name_ok(name):
    """Keep HD/SD names only, drop 4K/8K/UHD/HEVC/FHD names."""
    low = str(name or "").lower()
    for bad in ("4k", "8k", "uhd", "hevc", "fhd"):
        if bad in low:
            return False
    return ("hd" in low) or ("sd" in low)


def channel_name_clean(name):
    """Clean names: no bad tags and no hd/sd, used only to fill the gap."""
    low = str(name or "").lower()
    for bad in ("4k", "8k", "uhd", "hevc", "fhd"):
        if bad in low:
            return False
    return not channel_name_ok(name)


def movie_title_ok(title):
    """Keep 2010 to 2026 titles only."""
    text = str(title or "")
    for year in range(2010, 2027):
        if str(year) in text:
            return True
    return False
