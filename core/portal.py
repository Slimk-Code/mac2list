import requests
import json
import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


class Mac2ListPortal:
    def __init__(self, base_url, mac_address):
        self.base_url = base_url.rstrip("/")
        self.mac = mac_address.upper().strip()
        self.session = requests.Session()
        self.token = None
        self.locked_url = f"{self.base_url}/portal.php"
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 4 rev: 1812 Safari/533.3",
            "X-User-Agent": "Model: MAG250; Link: Ethernet",
            "Referer": f"{self.base_url}/c/",
            "Cookie": f"mac={self.mac}; stb_lang=en; timezone=Europe%2FLondon",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _get(self, params):
        url = self.locked_url
        try:
            resp = self.session.get(url, params=params, timeout=15, allow_redirects=True)
            status = resp.status_code
            text = resp.text
            try:
                data = resp.json()
                return {"_data": data, "_status": status, "_url": url, "_text": "", "_error": None, "_lockedpath": None}
            except json.JSONDecodeError:
                pass
            try:
                if text.strip().startswith("{"):
                    data = json.loads(text)
                    return {"_data": data, "_status": status, "_url": url, "_text": "", "_error": None, "_lockedpath": None}
                elif "js" in text:
                    start = text.find("{")
                    end = text.rfind("}") + 1
                    if start >= 0 and end > start:
                        data = json.loads(text[start:end])
                        return {"_data": data, "_status": status, "_url": url, "_text": "", "_error": None, "_lockedpath": None}
            except:
                pass
            return {
                "_data": None, "_status": status, "_url": url,
                "_text": text[:200], "_error": None,
                "_lockedpath": [
                    {"url": url, "status": status,
                     "result": "HTTP OK but no parseable JSON",
                     "preview": text[:150]}
                ]
            }
        except requests.exceptions.RequestException as e:
            return {
                "_data": None, "_status": None, "_url": url,
                "_text": "", "_error": str(e),
                "_lockedpath": [
                    {"url": url, "status": None,
                     "result": f"Connection failed: {str(e)[:100]}"}
                ]
            }

    def handshake(self):
        params = {"type": "stb", "action": "handshake", "JsHttpRequest": "1-xml"}
        result = self._get(params)
        data = result.get("_data")
        if data and isinstance(data, dict):
            js = data.get("js", {})
            if isinstance(js, dict):
                token = js.get("token")
                if token:
                    self.token = token
                    self.session.headers["Authorization"] = f"Bearer {token}"
        return result

    def fetch(self, params):
        return self._get(params)

    def _fetch_single_page(self, p, params_template, delay, pages, all_data, fetched_items, done_count, failed_pages, lock):
        p_params = dict(params_template)
        p_params["p"] = str(p)
        for attempt in range(3):
            page_result = self._get(p_params)
            page_data = page_result.get("_data", {})
            if page_data and isinstance(page_data, dict):
                items = page_data.get("js", {}).get("data", [])
                if items:
                    with lock:
                        all_data.extend(items)
                        done_count[0] += 1
                        fetched_items[0] += len(items)
                    return True
            if attempt < 2:
                time.sleep(delay * 2)
        with lock:
            done_count[0] += 1
            failed_pages.append(p)
        return False

    def fetch_all_pages(self, params_template, delay=0.5):
        result = self._get(params_template)
        data = result.get("_data")
        if not data or not isinstance(data, dict):
            return result
        js = data.get("js", {})
        if not isinstance(js, dict):
            return result
        total = int(js.get("total_items") or 0)
        per_page = int(js.get("max_page_items") or (len(js.get("data", [])) or 1))
        pages = math.ceil(total / per_page) if per_page else 1
        if pages <= 1:
            return result
        all_data = js.get("data", [])
        failed_pages = []
        done_count = [0]
        fetched_items = [len(all_data)]
        lock = threading.Lock()

        def _fetch_page_thread(p):
            success = self._fetch_single_page(p, params_template, delay, pages, all_data, fetched_items, done_count, failed_pages, lock)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_fetch_page_thread, p) for p in range(2, pages + 1)]
            for future in as_completed(futures):
                future.result()

        recovered_pages = []
        still_failed = failed_pages  # Retry deferred to caller

        merged = dict(data)
        merged_js = dict(js)
        merged_js["data"] = all_data
        merged_js["_fetched_pages"] = pages
        merged_js["_total_items_expected"] = total
        merged_js["_total_items_fetched"] = len(all_data)
        missing = total - len(all_data)
        merged_js["_missing_items"] = missing if missing > 0 else 0
        merged_js["_failed_pages"] = failed_pages
        merged_js["_failed_pages_count"] = len(failed_pages)
        merged_js["_recovered_pages"] = recovered_pages
        merged_js["_recovered_pages_count"] = len(recovered_pages)
        if total > 0:
            rate = (len(all_data) / total) * 100
        else:
            rate = 100.0
        merged_js["_success_rate"] = "{:.1f}%".format(rate)
        if failed_pages:
            merged_js["_status"] = "INCOMPLETE — some pages failed even after retry"
            if recovered_pages:
                merged_js["_note"] = "Expected {} items but only got {}. {} items missing across {} failed pages ({} recovered on retry).".format(total, len(all_data), missing, len(failed_pages), len(recovered_pages))
            else:
                merged_js["_note"] = "Expected {} items but only got {}. {} items missing across {} failed pages (none recovered).".format(total, len(all_data), missing, len(failed_pages))
        else:
            merged_js["_status"] = "COMPLETE"
            if recovered_pages:
                merged_js["_note"] = "All {} items fetched successfully across {} pages ({} recovered on retry).".format(total, pages, len(recovered_pages))
            else:
                merged_js["_note"] = "All {} items fetched successfully across {} pages.".format(total, pages)
        merged["js"] = merged_js
        return {
            "_data": merged, "_status": result.get("_status"),
            "_url": result.get("_url"), "_text": "",
            "_error": None, "_lockedpath": None
        }


class XtreamPortal:
    """Xtream Codes API client with the same surface as Mac2ListPortal.

    Translates MAC-style step params into player_api actions and normalizes
    every response into the MAC-shaped {"js": ...} dicts, so the shared
    hub/engine/fetch/library pipeline runs unchanged.
    """

    def __init__(self, base_url, username, password=""):
        self.base_url = base_url.rstrip("/")
        self.username = (username or "").strip()
        self.password = password or ""
        self.session = requests.Session()
        self.token = None
        self.api_url = f"{self.base_url}/player_api.php"
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        })
        self._server = ""

    def _auth(self):
        return {"username": self.username, "password": self.password}

    def _get_api(self, extra):
        params = dict(self._auth())
        params.update(extra or {})
        url = self.api_url
        try:
            resp = self.session.get(url, params=params, timeout=15)
            status = resp.status_code
            try:
                data = resp.json()
                return {"_data": data, "_status": status, "_url": url,
                        "_text": "", "_error": None, "_lockedpath": None}
            except json.JSONDecodeError:
                text = resp.text
                return {"_data": None, "_status": status, "_url": url,
                        "_text": text[:200], "_error": None,
                        "_lockedpath": [{"url": url, "status": status,
                                         "result": "HTTP OK but no parseable JSON",
                                         "preview": text[:150]}]}
        except requests.exceptions.RequestException as e:
            return {"_data": None, "_status": None, "_url": url,
                    "_text": "", "_error": str(e),
                    "_lockedpath": [{"url": url, "status": None,
                                     "result": f"Connection failed: {str(e)[:100]}"}]}

    def handshake(self):
        result = self._get_api({})
        data = result.get("_data")
        ok = False
        if isinstance(data, dict):
            user = data.get("user_info") or {}
            if str(user.get("auth", "0")) == "1":
                ok = True
                self.token = "xtream"
                server = data.get("server_info") or {}
                url = server.get("url") or ""
                port = str(server.get("port") or "")
                if url:
                    self._server = url if url.startswith("http") else f"http://{url}"
                    if port and port not in ("80", "443"):
                        self._server = f"{self._server}:{port}"
        if not ok:
            result["_error"] = result.get("_error") or "auth failed"
        return result

    def _stream_base(self):
        return self._server or self.base_url

    def _live_url(self, stream_id, ext="m3u8"):
        return f"{self._stream_base()}/live/{self.username}/{self.password}/{stream_id}.{ext}"

    def _movie_url(self, stream_id, ext="mp4"):
        return f"{self._stream_base()}/movie/{self.username}/{self.password}/{stream_id}.{ext}"

    def _series_url(self, stream_id, ext="mp4"):
        return f"{self._stream_base()}/series/{self.username}/{self.password}/{stream_id}.{ext}"

    @staticmethod
    def _norm_cats(items):
        cats = []
        for c in items or []:
            if not isinstance(c, dict):
                continue
            cid = c.get("category_id", c.get("id", ""))
            name = c.get("category_name", c.get("title", c.get("name", "Unknown")))
            if str(cid) == "*":
                continue
            cats.append({"id": str(cid), "title": name,
                         "name": name, "total_items": 0})
        return cats

    def _norm_streams(self, items, kind):
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            sid = it.get("stream_id", it.get("series_id", it.get("id", "")))
            name = it.get("name", it.get("title", "Unknown"))
            icon = it.get("stream_icon", "")
            if kind == "live":
                url = self._live_url(sid)
            elif kind == "movie":
                ext = (it.get("container_extension") or "mp4").strip() or "mp4"
                url = self._movie_url(sid, ext)
            else:
                ext = (it.get("container_extension") or "mp4").strip() or "mp4"
                url = self._series_url(sid, ext)
            out.append({"id": str(sid), "name": name, "title": name,
                        "cmd": url, "logo": icon,
                        "xtream_stream_id": str(sid)})
        return out

    def fetch(self, params):
        params = dict(params or {})
        ptype = params.get("type", "")
        action = params.get("action", "")
        # Auth / profile / account info steps: pass through to the API.
        if (ptype, action) in (("stb", "handshake"), ("stb", "get_profile")) or (
                ptype == "account_info"):
            if action == "handshake":
                return self.handshake()
            res = self._get_api({})
            data = res.get("_data")
            if isinstance(data, dict):
                user = data.get("user_info") or {}
                try:
                    exp = int(user.get("exp_date") or 0)
                except (TypeError, ValueError):
                    exp = 0
                if exp:
                    from datetime import datetime as _dt
                    phone = _dt.fromtimestamp(exp).strftime("%B %d, %Y, %I:%M %p")
                else:
                    phone = ""
                res["_data"] = {"js": {"phone": phone,
                                       "username": self.username,
                                       "status": user.get("status", "")}}
            return res
        # Category scrapes.
        if (ptype, action) in (("itv", "get_genres"),):
            res = self._get_api({"action": "get_live_categories"})
            data = res.get("_data")
            cats = self._norm_cats(data if isinstance(data, list) else [])
            res["_data"] = {"js": cats}
            return res
        if (ptype, action) in (("vod", "get_categories"), ("series", "get_categories")):
            act = ("get_vod_categories" if ptype == "vod"
                   else "get_series_categories")
            res = self._get_api({"action": act})
            data = res.get("_data")
            cats = self._norm_cats(data if isinstance(data, list) else [])
            res["_data"] = {"js": cats}
            return res
        # Direct resolve: MAC pipeline sends create_link with a cmd.
        # Our normalized items already carry the final URL as cmd.
        if action == "create_link":
            cmd = params.get("cmd", "")
            if cmd.startswith("http"):
                return {"_data": {"js": cmd}, "_status": 200,
                        "_url": self.api_url, "_text": "", "_error": None,
                        "_lockedpath": None}
            # Episode resolve: cmd carries the series_id, series carries ep num.
            if params.get("series"):
                url = self._resolve_episode_url(cmd, params.get("series"))
                if url:
                    return {"_data": {"js": url}, "_status": 200,
                            "_url": self.api_url, "_text": "", "_error": None,
                            "_lockedpath": None}
            return {"_data": None, "_status": 200, "_url": self.api_url,
                    "_text": "", "_error": "no cmd",
                    "_lockedpath": [{"url": self.api_url, "status": 200,
                                     "result": "No stream URL in cmd",
                                     "preview": str(cmd)[:150]}]}

        # Item fetches (translated by fetch_all_pages or direct callers).
        if ptype == "itv" and action == "get_ordered_list":
            cid = params.get("genre", "")
            res = self._get_api({"action": "get_live_streams",
                                 "category_id": cid} if cid else
                                {"action": "get_live_streams"})
            data = res.get("_data")
            items = self._norm_streams(data if isinstance(data, list) else [],
                                       "live")
            res["_data"] = {"js": {"data": items, "total_items": len(items)}}
            return res
        if ptype == "vod" and action == "get_ordered_list":
            cid = params.get("category", "")
            res = self._get_api({"action": "get_vod_streams",
                                 "category_id": cid} if cid else
                                {"action": "get_vod_streams"})
            data = res.get("_data")
            items = self._norm_streams(data if isinstance(data, list) else [],
                                       "movie")
            res["_data"] = {"js": {"data": items, "total_items": len(items)}}
            return res
        if ptype == "series" and action == "get_ordered_list" and params.get("movie_id"):
            sid = params.get("movie_id")
            res = self._get_api({"action": "get_series_info",
                                 "series_id": sid})
            data = res.get("_data")
            seasons = []
            if isinstance(data, dict):
                seps = data.get("episodes") or {}
                if isinstance(seps, dict):
                    for sname in sorted(seps.keys()):
                        eps = seps[sname]
                        if not isinstance(eps, list):
                            continue
                        nums = []
                        for ep in eps:
                            if not isinstance(ep, dict):
                                continue
                            try:
                                nums.append(int(ep.get("episode_num", 0)) or 0)
                            except (TypeError, ValueError):
                                continue
                        nums = [n for n in nums if n] or list(range(1, len(eps) + 1))
                        seasons.append({"id": str(sname), "name": "Season {}".format(sname),
                                        "series": nums, "cmd": str(sid)})
            res["_data"] = {"js": {"data": seasons, "total_items": len(seasons)}}
            return res
        if ptype == "series" and action == "get_ordered_list":
            cid = params.get("category", "")
            res = self._get_api({"action": "get_series",
                                 "category_id": cid} if cid else
                                {"action": "get_series"})
            data = res.get("_data")
            items = self._norm_streams(data if isinstance(data, list) else [],
                                       "series")
            res["_data"] = {"js": {"data": items, "total_items": len(items)}}
            return res
        return self._get_api({})

    def _resolve_episode_url(self, series_id, ep_num):
        """Build a direct series-episode URL via get_series_info."""
        res = self._get_api({"action": "get_series_info",
                             "series_id": series_id})
        data = res.get("_data")
        if not isinstance(data, dict):
            return ""
        episodes = data.get("episodes") or {}
        flat = []
        if isinstance(episodes, dict):
            for season_eps in episodes.values():
                if isinstance(season_eps, list):
                    flat.extend(e for e in season_eps if isinstance(e, dict))
        for ep in flat:
            if str(ep.get("episode_num", "")) == str(ep_num):
                ext = (ep.get("container_extension") or "mp4").strip() or "mp4"
                return self._series_url(ep.get("id", ""), ext)
        # Fallback: positional match across all episodes in order.
        try:
            idx = int(ep_num) - 1
        except (TypeError, ValueError):
            return ""
        if 0 <= idx < len(flat):
            ext = (flat[idx].get("container_extension") or "mp4").strip() or "mp4"
            return self._series_url(flat[idx].get("id", ""), ext)
        return ""

    def _fetch_single_page(self, p, params_template, delay, pages, all_data,
                           fetched_items, done_count, failed_pages, lock):
        # Xtream answers in one shot; pagination is a no-op that still
        # satisfies the shared fetch_all_pages contract.
        return True

    def fetch_all_pages(self, params_template, delay=0.5):
        result = self.fetch(params_template)
        data = result.get("_data")
        if not data or not isinstance(data, dict):
            return result
        js = data.get("js", {})
        if not isinstance(js, dict):
            return result
        items = js.get("data", [])
        total = js.get("total_items") or len(items)
        merged_js = dict(js)
        merged_js["data"] = items
        merged_js["_fetched_pages"] = 1
        merged_js["_total_items_expected"] = total
        merged_js["_total_items_fetched"] = len(items)
        missing = total - len(items)
        merged_js["_missing_items"] = missing if missing > 0 else 0
        merged_js["_failed_pages"] = []
        merged_js["_failed_pages_count"] = 0
        merged_js["_recovered_pages"] = []
        merged_js["_recovered_pages_count"] = 0
        merged_js["_success_rate"] = "100.0%"
        merged_js["_status"] = "COMPLETE"
        merged_js["_note"] = "All {} items fetched successfully across 1 page.".format(len(items))
        merged = dict(data)
        merged["js"] = merged_js
        return {"_data": merged, "_status": result.get("_status"),
                "_url": result.get("_url"), "_text": "",
                "_error": None, "_lockedpath": None}