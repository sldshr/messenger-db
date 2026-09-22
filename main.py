# main.py
import os, time, uuid, json, hmac, hashlib, secrets, asyncio, re, urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Union
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel

app = FastAPI(title="SLD")
app.add_middleware(GZipMiddleware, minimum_size=800)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("[SLD] Supabase connected")
    except Exception as e:
        print("[SLD] Supabase init error:", e)

USERS: Dict[str, dict] = {}
SESSIONS: Dict[str, dict] = {}
POSTS_MEM: Dict[str, dict] = {}
NOTIFS_MEM: Dict[str, List[dict]] = {}
_VIEW_COOLDOWN: Dict[str, float] = {}
_OG_CACHE: Dict[str, Optional[dict]] = {}

MAX_POST_LEN = 5000
MAX_COMMENT_LEN = 1500
MAX_BIO_LEN = 300
FEED_LIMIT = 100
VIEW_COOLDOWN_SEC = 8 * 3600
TRUNCATE_LINES = 15
TRUNCATE_CHARS = 800
SESSION_TTL = 30 * 24 * 3600
USER_CACHE_TTL = 60.0
RATE_WINDOW = 60.0
NICK_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
MENTION_RE = re.compile(r"(?<![a-zA-Z0-9_])@([a-zA-Z0-9_]{3,20})")
URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')
RU_COUNTRIES = {"RU","BY","KZ","UA","KG","TJ","UZ","AM","AZ","MD"}
_lang_cache: Dict[str, str] = {}
_geo_cache: Dict[str, dict] = {}
_USER_CACHE: Dict[str, Tuple[float, dict]] = {}
_RATE: Dict[str, List[float]] = {}

DEFAULT_EMOJI = "🐱"

EMOJIS_RAW = (
    "😀😃😄😁😆😅😂😊😇🙂🙃😉😌😍😘😗😙😚😋😛😝😜😎"
    "😏😒😞😔😟😕🙁☹😣😖😫😩😢😭😤😠😡😳😱😨😰😥😓"
    "🤗🤔🤐😶😐😑😬🙄😯😦😧😮😲😴🤤😪😵🤢🤧😷🤒🤕"
    "😈👿👹👺💩💀👻👽👾🤖🎃😺😸😹😻😼😽🙀😿😾"
    "🐶🐱🐭🐹🐰🐻🐼🐨🐯🐮🐷🐸🐵"
    "🐔🐧🐦🐤🐺🐴🐝🐛🐢🐍🐙🐠🐟🐬🐳🐋"
    "🌵🎄🌲🌳🌴🌱🌿🍀🍃🍂🍁🍄🌾"
    "🌷🌹🌺🌸🌼🌻💐"
    "🌞🌝🌛🌜🌚🌕🌙🌎🌍🌏"
    "⭐🌟✨⚡🔥💥🌈💧🌊"
    "🍎🍊🍋🍌🍉🍇🍓🍒🍑🍍🍅"
    "🍞🍔🍟🍕🍜🍣🍦🍩🍪🎂🍰🍫🍬"
)
EMOJIS = list(EMOJIS_RAW)


class EventBus:
    def __init__(self):
        self.clients: Dict[str, List[asyncio.Queue]] = {}
        self.rooms: Dict[str, set] = {}
    async def subscribe(self, nick: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.clients.setdefault(nick, []).append(q)
        return q
    def unsubscribe(self, nick: str, q: asyncio.Queue):
        lst = self.clients.get(nick)
        if lst and q in lst:
            try: lst.remove(q)
            except ValueError: pass
        if not self.clients.get(nick):
            self.clients.pop(nick, None); self.rooms.pop(nick, None)
    def set_rooms(self, nick: str, rooms):
        if not nick: return
        if not rooms: self.rooms[nick] = set()
        elif isinstance(rooms, str): self.rooms[nick] = {rooms} if rooms else set()
        else: self.rooms[nick] = set(r for r in rooms if r)
    def broadcast_room(self, room: str, ev: dict, except_nick: Optional[str] = None):
        if not room: return
        for nick in list(self.clients.keys()):
            if nick == except_nick: continue
            if room in (self.rooms.get(nick) or set()):
                self._deliver(nick, ev)
    def _deliver(self, nick: str, ev: dict):
        for q in list(self.clients.get(nick, [])):
            try: q.put_nowait(ev)
            except asyncio.QueueFull: pass
            except Exception: pass


bus = EventBus()
MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


@app.on_event("startup")
async def _startup():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()


def _run_sync(fn, *args):
    if MAIN_LOOP and not MAIN_LOOP.is_closed():
        try:
            MAIN_LOOP.call_soon_threadsafe(fn, *args); return
        except RuntimeError: pass
    fn(*args)


def bus_publish(nick: str, ev: dict) -> None:
    if not nick: return
    _run_sync(bus._deliver, nick, ev)


def bus_broadcast(room: str, ev: dict, except_nick: Optional[str] = None) -> None:
    if not room: return
    _run_sync(bus.broadcast_room, room, ev, except_nick)


def broadcast_post_change(post: dict, except_nick: Optional[str] = None) -> None:
    if not post: return
    pid = post.get("id")
    if pid:
        bus_broadcast("post:" + pid, {"type": "refresh"}, except_nick=except_nick)
    author = post.get("author")
    if author:
        bus_broadcast("profile:" + author, {"type": "refresh"}, except_nick=except_nick)
    bus_broadcast("feed", {"type": "refresh"}, except_nick=except_nick)


def broadcast_view_update(pid: str, author: str, views: int) -> None:
    ev = {"type": "view_update", "post_id": pid, "views": views}
    bus_broadcast("post:" + pid, ev)
    bus_broadcast("feed", ev)
    if author:
        bus_broadcast("profile:" + author, ev)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iters = 100_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iters)
    return f"pbkdf2${iters}${salt}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if parts[0] == "pbkdf2":
            _, iters, salt, expected = parts
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
            return hmac.compare_digest(dk.hex(), expected)
        salt, expected = stored.split("$", 1)
        h = hashlib.sha256((salt + password).encode()).hexdigest()
        return hmac.compare_digest(h, expected)
    except Exception:
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def rate_limit(key: str, max_req: int, window: float = RATE_WINDOW) -> bool:
    now = time.time()
    arr = [t for t in _RATE.get(key, []) if now - t < window]
    if len(arr) >= max_req:
        _RATE[key] = arr; return False
    arr.append(now); _RATE[key] = arr; return True


def ts_to_iso(ts) -> str:
    if isinstance(ts, str): return ts
    try: return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception: return datetime.now(tz=timezone.utc).isoformat()


def iso_to_ts(s) -> float:
    if isinstance(s, (int, float)): return float(s)
    if not s: return time.time()
    try:
        if isinstance(s, str) and s.endswith("Z"): s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception: return time.time()


def extract_mentions(text: str) -> List[str]:
    out, seen = [], set()
    for m in MENTION_RE.finditer(text):
        n = m.group(1)
        if n.lower() in seen: continue
        seen.add(n.lower()); out.append(n)
    return out


def extract_first_url(text: str) -> Optional[str]:
    if not text: return None
    m = URL_RE.search(text)
    return m.group(0) if m else None


def detect_device(ua: str) -> str:
    ua_l = (ua or "").lower()
    markers = ("mobile", "android", "iphone", "ipod", "ipad", "windows phone",
               "webos", "blackberry", "opera mini")
    if any(x in ua_l for x in markers): return "mobile"
    return "desktop"


def fetch_og_data(url: str) -> Optional[dict]:
    if not url: return None
    if url in _OG_CACHE: return _OG_CACHE[url]
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; SLDbot/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=4) as r:
            raw = r.read(160000)
            html = raw.decode("utf-8", errors="ignore")
    except Exception:
        _OG_CACHE[url] = None; return None

    def og(prop):
        pats = [
            r'<meta[^>]+property=["\']og:' + prop + r'["\'][^>]+content=["\']([^"\']*)["\']',
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:' + prop + r'["\']',
            r'<meta[^>]+name=["\']og:' + prop + r'["\'][^>]+content=["\']([^"\']*)["\']',
        ]
        for p in pats:
            m = re.search(p, html, re.I)
            if m: return m.group(1)
        return ""

    data = {
        "url": url,
        "title": (og("title") or "").strip()[:200],
        "description": (og("description") or "").strip()[:300],
        "image": (og("image") or "").strip()[:500],
        "site_name": (og("site_name") or "").strip()[:100],
    }
    if not data["title"]:
        m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
        if m: data["title"] = m.group(1).strip()[:200]
    if not (data["title"] or data["description"] or data["image"]):
        _OG_CACHE[url] = None; return None
    _OG_CACHE[url] = data
    return data


def parse_user_agent(ua: str):
    ua = ua or ""
    browser = "Unknown"
    if "YaBrowser" in ua: browser = "Yandex"
    elif "Edg/" in ua: browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua: browser = "Opera"
    elif "Firefox/" in ua: browser = "Firefox"
    elif "Chrome/" in ua: browser = "Chrome"
    elif "Safari/" in ua: browser = "Safari"
    os_name = "Unknown"
    if "Windows NT" in ua: os_name = "Windows"
    elif "Mac OS X" in ua: os_name = "macOS"
    elif "Android" in ua: os_name = "Android"
    elif "iPhone" in ua or "iPad" in ua or "iPod" in ua: os_name = "iOS"
    elif "Linux" in ua: os_name = "Linux"
    return browser, os_name


def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    if fwd: return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _is_private_ip(ip: str) -> bool:
    if not ip: return True
    return (ip.startswith("127.") or ip.startswith("10.") or ip.startswith("192.168.")
            or ip.startswith("172.") or ip in ("::1", "localhost"))


def get_geo(ip: str) -> dict:
    if not ip: return {}
    if ip in _geo_cache: return _geo_cache[ip]
    if _is_private_ip(ip):
        _geo_cache[ip] = {}; return {}
    try:
        req = urllib.request.Request(
            f"http://ip-api.com/json/{ip}?fields=countryCode,city,country",
            headers={"User-Agent": "SLD"})
        with urllib.request.urlopen(req, timeout=2) as r:
            data = json.loads(r.read().decode())
        result = {"city": data.get("city", "") or "",
                  "country": data.get("country", "") or "",
                  "country_code": (data.get("countryCode") or "").upper()}
        _geo_cache[ip] = result; return result
    except Exception:
        _geo_cache[ip] = {}; return {}


def detect_lang(ip: str, accept_language: str) -> str:
    key = ip or "unknown"
    if key in _lang_cache: return _lang_cache[key]
    lang = None
    geo = get_geo(ip)
    cc = geo.get("country_code", "")
    if cc in RU_COUNTRIES: lang = "ru"
    elif cc: lang = "en"
    if not lang:
        al = (accept_language or "").lower()
        lang = "ru" if (al.startswith("ru") or ",ru" in al or "ru-" in al) else "en"
    _lang_cache[key] = lang
    return lang


def get_lang(request: Request) -> str:
    qp = request.query_params.get("lang")
    if qp in ("ru","en"): return qp
    ck = request.cookies.get("SLD_lang")
    if ck in ("ru","en"): return ck
    return detect_lang(get_client_ip(request), request.headers.get("accept-language",""))


def get_current_user(request: Request) -> Optional[dict]:
    token = request.headers.get("x-auth")
    if not token: return None
    sess = SESSIONS.get(token)
    if not sess: return None
    if time.time() - sess.get("created", 0) > SESSION_TTL:
        SESSIONS.pop(token, None); return None
    nick = sess.get("nick")
    if not nick: return None
    return db_load_user_cached(nick)


def require_user(request: Request) -> dict:
    u = get_current_user(request)
    if not u: raise HTTPException(401, "unauthorized")
    return u


def invalidate_user_cache(nick: Optional[str] = None) -> None:
    if nick is None: _USER_CACHE.clear()
    else: _USER_CACHE.pop(nick.lower(), None)


def clean_emoji(s: Optional[str]) -> str:
    if not s: return ""
    return s.strip()[:8]


NOTIFY_FIELDS = [
    "notify_on_new_post", "notify_on_follow", "notify_on_comment",
    "notify_on_reply", "notify_on_mention", "notify_on_quote",
]
NOTIFY_TYPE_TO_FIELD = {
    "new_post": "notify_on_new_post", "follow": "notify_on_follow",
    "comment": "notify_on_comment", "reply": "notify_on_reply",
    "mention": "notify_on_mention", "quote": "notify_on_quote",
}
USER_BOOL_DEFAULTS = {
    "notify_on_new_post": True, "notify_on_follow": True, "notify_on_comment": True,
    "notify_on_reply": True, "notify_on_mention": True, "notify_on_quote": True,
    "show_link_previews": True, "allow_followers_view": True, "allow_following_view": True,
    "show_device_badge": True,
}


def _norm_user_row(row: dict) -> dict:
    row["following"] = set(row.get("following") or [])
    row["followers"] = set(row.get("followers") or [])
    row["bio"] = row.get("bio") or ""
    row["created_at"] = iso_to_ts(row.get("created_at"))
    for f, d in USER_BOOL_DEFAULTS.items():
        row[f] = bool(row.get(f, d))
    row["avatar_emoji"] = row.get("avatar_emoji") or DEFAULT_EMOJI
    return row


def db_load_user(nick: str) -> Optional[dict]:
    if not nick: return None
    if supabase:
        try:
            r = supabase.table("users").select("*").ilike("nick", nick).limit(1).execute()
            if r.data: return _norm_user_row(r.data[0])
        except Exception as e: print("[SLD] db_load_user error:", e)
        return None
    for u in USERS.values():
        if u["nick"].lower() == nick.lower(): return u
    return None


def db_load_user_cached(nick: str) -> Optional[dict]:
    now = time.time()
    e = _USER_CACHE.get(nick.lower())
    if e and now - e[0] < USER_CACHE_TTL: return e[1]
    u = db_load_user(nick)
    if u is not None: _USER_CACHE[nick.lower()] = (now, u)
    return u


def db_load_users_batch(nicks: List[str]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not nicks: return out
    now = time.time()
    missing = []
    seen = set()
    for n in nicks:
        if not n: continue
        ln = n.lower()
        if ln in seen: continue
        seen.add(ln)
        e = _USER_CACHE.get(ln)
        if e and now - e[0] < USER_CACHE_TTL:
            out[ln] = e[1]
        else:
            missing.append(n)
    if not missing: return out
    if supabase:
        try:
            r = supabase.table("users").select("*").in_("nick", list(set(missing))).execute()
            for row in (r.data or []):
                u = _norm_user_row(row)
                out[u["nick"].lower()] = u
                _USER_CACHE[u["nick"].lower()] = (now, u)
        except Exception as e:
            print("[SLD] db_load_users_batch error:", e)
    else:
        for n in missing:
            for u in USERS.values():
                if u["nick"].lower() == n.lower():
                    out[u["nick"].lower()] = u
    return out


def _user_payload(u: dict) -> dict:
    p = {
        "nick": u["nick"], "name": u["name"], "bio": u.get("bio",""),
        "password": u["password"], "created_at": ts_to_iso(u["created_at"]),
        "following": list(u.get("following") or []),
        "followers": list(u.get("followers") or []),
        "allow_followers_view": u.get("allow_followers_view", True),
        "allow_following_view": u.get("allow_following_view", True),
        "avatar_emoji": u.get("avatar_emoji", DEFAULT_EMOJI),
        "show_link_previews": u.get("show_link_previews", True),
        "show_device_badge": u.get("show_device_badge", True),
    }
    for f in NOTIFY_FIELDS: p[f] = bool(u.get(f, True))
    return p


def db_save_user(u: dict) -> None:
    USERS[u["nick"]] = u
    if not supabase:
        invalidate_user_cache(u["nick"]); return
    try:
        supabase.table("users").upsert(_user_payload(u)).execute()
    except Exception as e:
        print("[SLD] db_save_user error:", e)
    USERS.pop(u["nick"], None)
    invalidate_user_cache(u["nick"])


def db_update_user_fields(nick: str, patch: dict) -> None:
    if not supabase:
        if nick in USERS: USERS[nick].update(patch)
        invalidate_user_cache(nick); return
    try:
        supabase.table("users").update(patch).eq("nick", nick).execute()
    except Exception as e:
        print("[SLD] db_update_user_fields error:", e)
    invalidate_user_cache(nick)


def db_all_users() -> List[dict]:
    if supabase:
        try:
            r = supabase.table("users").select("*").execute()
            return [_norm_user_row(row) for row in (r.data or [])]
        except Exception as e:
            print("[SLD] db_all_users error:", e); return []
    return list(USERS.values())


def db_create_post(p: dict) -> None:
    if not supabase:
        POSTS_MEM[p["id"]] = p; return
    payload = {
        "id": p["id"], "text": p["text"], "author": p["author"],
        "created_at": ts_to_iso(p["created_at"]),
        "quoted_post_id": p.get("quoted_post_id"),
        "og_data": json.dumps(p.get("og_data")) if p.get("og_data") else "",
    }
    if p.get("device"): payload["device"] = p["device"]
    try:
        supabase.table("posts").insert(payload).execute()
    except Exception as e:
        print("[SLD] db_create_post error:", e)
        payload.pop("device", None)
        try: supabase.table("posts").insert(payload).execute()
        except Exception as e2: print("[SLD] db_create_post retry error:", e2)


def db_update_post_text(pid: str, text: str) -> None:
    if not supabase:
        if pid in POSTS_MEM: POSTS_MEM[pid]["text"] = text
        return
    try: supabase.table("posts").update({"text": text}).eq("id", pid).execute()
    except Exception as e: print("[SLD] db_update_post error:", e)


def db_delete_post(pid: str) -> None:
    if not supabase:
        POSTS_MEM.pop(pid, None); return
    try:
        supabase.table("notifications").delete().eq("post_id", pid).execute()
        supabase.table("posts").delete().eq("id", pid).execute()
    except Exception as e: print("[SLD] db_delete_post error:", e)


def db_get_post(pid: str) -> Optional[dict]:
    if supabase:
        try:
            r = supabase.table("posts").select("*").eq("id", pid).limit(1).execute()
            if not r.data: return None
            row = r.data[0]
            row["created_at"] = iso_to_ts(row.get("created_at"))
            return row
        except Exception as e:
            print("[SLD] db_get_post error:", e); return None
    return POSTS_MEM.get(pid)


def db_inc_views(pid: str) -> int:
    if not supabase:
        p = POSTS_MEM.get(pid)
        if p:
            p["views"] = (p.get("views") or 0) + 1
            return p["views"]
        return 0
    try:
        supabase.rpc("increment_post_views", {"p_id": pid}).execute()
        r = supabase.table("posts").select("views").eq("id", pid).limit(1).execute()
        if r.data: return r.data[0].get("views") or 0
    except Exception as e:
        print("[SLD] db_inc_views error:", e)
    return 0


def view_should_count(pid: str, vid: str) -> bool:
    key = pid + "|" + vid
    now = time.time()
    last = _VIEW_COOLDOWN.get(key, 0)
    if now - last < VIEW_COOLDOWN_SEC: return False
    if supabase:
        try:
            r = (supabase.table("post_views").select("last_viewed_at")
                 .eq("post_id", pid).eq("viewer_id", vid).limit(1).execute())
            if r.data:
                last_ts = iso_to_ts(r.data[0].get("last_viewed_at"))
                if now - last_ts < VIEW_COOLDOWN_SEC:
                    _VIEW_COOLDOWN[key] = last_ts
                    return False
        except Exception as e:
            print("[SLD] view check error:", e)
    return True


def view_mark_counted(pid: str, vid: str) -> None:
    now = time.time()
    _VIEW_COOLDOWN[pid + "|" + vid] = now
    if supabase:
        try:
            supabase.table("post_views").upsert({
                "post_id": pid, "viewer_id": vid,
                "last_viewed_at": ts_to_iso(now)}).execute()
        except Exception as e: print("[SLD] view mark error:", e)


def db_get_quotes(post_ids: List[str]) -> Dict[str, dict]:
    out = {}
    if not post_ids: return out
    if supabase:
        try:
            r = supabase.table("posts").select("*").in_("id", post_ids).execute()
            for row in r.data or []:
                row["created_at"] = iso_to_ts(row.get("created_at"))
                out[row["id"]] = row
        except Exception as e:
            print("[SLD] db_get_quotes error:", e)
        return out
    for pid in post_ids:
        p = POSTS_MEM.get(pid)
        if p: out[pid] = p
    return out


def db_list_posts(q: str = "", author: str = "", subscriptions_of: str = "") -> List[dict]:
    if supabase:
        try:
            query = supabase.table("posts").select("*")
            if author: query = query.eq("author", author)
            if subscriptions_of:
                u = db_load_user_cached(subscriptions_of)
                fol = list(u.get("following") or []) if u else []
                if not fol: return []
                query = query.in_("author", fol)
            if q: query = query.ilike("text", f"%{q}%")
            query = query.order("created_at", desc=True).limit(FEED_LIMIT)
            rows = query.execute().data or []
            for row in rows: row["created_at"] = iso_to_ts(row.get("created_at"))
            return rows
        except Exception as e:
            print("[SLD] db_list_posts error:", e); return []
    items = list(POSTS_MEM.values())
    if author: items = [p for p in items if p["author"] == author]
    if subscriptions_of:
        u = db_load_user_cached(subscriptions_of)
        fol = set(u.get("following") or []) if u else set()
        items = [p for p in items if p["author"] in fol]
    if q:
        n = q.lower()
        items = [p for p in items if n in p["text"].lower()]
    items.sort(key=lambda p: p["created_at"], reverse=True)
    return items[:FEED_LIMIT]


def db_post_votes(post_ids: List[str]) -> List[dict]:
    if not post_ids: return []
    if supabase:
        try:
            return supabase.table("post_votes").select("*").in_("post_id", post_ids).execute().data or []
        except Exception as e:
            print("[SLD] db_post_votes error:", e); return []
    out = []
    for pid in post_ids:
        p = POSTS_MEM.get(pid)
        if p:
            for vid, d in p.get("votes", {}).items():
                out.append({"post_id": pid, "voter_id": vid, "direction": d})
    return out


def db_set_post_vote(post_id: str, voter_id: str, direction: int) -> None:
    if supabase:
        try:
            if direction == 0:
                supabase.table("post_votes").delete().eq("post_id", post_id).eq("voter_id", voter_id).execute()
            else:
                supabase.table("post_votes").upsert({
                    "post_id": post_id, "voter_id": voter_id, "direction": direction}).execute()
        except Exception as e: print("[SLD] db_set_post_vote error:", e)
        return
    p = POSTS_MEM.get(post_id)
    if not p: return
    if direction == 0: p["votes"].pop(voter_id, None)
    else: p["votes"][voter_id] = direction


def db_comment_counts(post_ids: List[str]) -> Dict[str, int]:
    """Лёгкий счётчик комментариев по постам — только колонка post_id."""
    if not post_ids: return {}
    if supabase:
        try:
            r = supabase.table("comments").select("post_id").in_("post_id", post_ids).execute()
            out: Dict[str, int] = {}
            for row in r.data or []:
                pid = row.get("post_id")
                if pid: out[pid] = out.get(pid, 0) + 1
            return out
        except Exception as e:
            print("[SLD] db_comment_counts error:", e); return {}
    out = {}
    for pid in post_ids:
        p = POSTS_MEM.get(pid)
        if p: out[pid] = len(p.get("comments", []))
    return out


def db_comments_for_posts(post_ids: List[str]) -> List[dict]:
    if not post_ids: return []
    if supabase:
        try:
            rows = (supabase.table("comments").select("*").in_("post_id", post_ids)
                    .order("created_at").execute().data or [])
            for row in rows: row["created_at"] = iso_to_ts(row.get("created_at"))
            return rows
        except Exception as e:
            print("[SLD] db_comments_for_posts error:", e); return []
    out = []
    for pid in post_ids:
        p = POSTS_MEM.get(pid)
        if p:
            for c in p.get("comments", []):
                out.append({"id": c["id"], "post_id": pid, "author": c["author"],
                            "parent_id": c.get("parent_id"), "text": c["text"],
                            "created_at": c["created_at"]})
    return out


def db_comment_votes(comment_ids: List[str]) -> List[dict]:
    if not comment_ids: return []
    if supabase:
        try:
            return supabase.table("comment_votes").select("*").in_("comment_id", comment_ids).execute().data or []
        except Exception as e:
            print("[SLD] db_comment_votes error:", e); return []
    out = []
    for pid, p in POSTS_MEM.items():
        for c in p.get("comments", []):
            if c["id"] in comment_ids:
                for vid, d in c.get("votes", {}).items():
                    out.append({"comment_id": c["id"], "voter_id": vid, "direction": d})
    return out


def db_create_comment(c: dict) -> None:
    if not supabase:
        p = POSTS_MEM.get(c["post_id"])
        if p:
            p.setdefault("comments", []).append({
                "id": c["id"], "author": c["author"], "parent_id": c.get("parent_id"),
                "text": c["text"], "created_at": c["created_at"], "votes": {}})
        return
    try:
        supabase.table("comments").insert({
            "id": c["id"], "post_id": c["post_id"], "author": c["author"],
            "parent_id": c.get("parent_id"), "text": c["text"],
            "created_at": ts_to_iso(c["created_at"])}).execute()
    except Exception as e: print("[SLD] db_create_comment error:", e)


def db_update_comment_text(cid: str, text: str) -> None:
    if not supabase:
        for p in POSTS_MEM.values():
            for c in p.get("comments", []):
                if c["id"] == cid: c["text"] = text; return
        return
    try: supabase.table("comments").update({"text": text}).eq("id", cid).execute()
    except Exception as e: print("[SLD] db_update_comment error:", e)


def db_delete_comment(cid: str) -> None:
    if not supabase:
        for p in POSTS_MEM.values():
            p["comments"] = [c for c in p.get("comments", []) if c["id"] != cid]
        return
    try:
        supabase.table("notifications").delete().eq("comment_id", cid).execute()
        supabase.table("comments").delete().eq("id", cid).execute()
    except Exception as e: print("[SLD] db_delete_comment error:", e)


def db_get_comment(cid: str) -> Optional[dict]:
    if supabase:
        try:
            r = supabase.table("comments").select("*").eq("id", cid).limit(1).execute()
            if not r.data: return None
            row = r.data[0]
            row["created_at"] = iso_to_ts(row.get("created_at"))
            return row
        except Exception as e:
            print("[SLD] db_get_comment error:", e); return None
    for p in POSTS_MEM.values():
        for c in p.get("comments", []):
            if c["id"] == cid:
                return {"id": c["id"], "post_id": p["id"], "author": c["author"],
                        "parent_id": c.get("parent_id"), "text": c["text"],
                        "created_at": c["created_at"]}
    return None


def db_set_comment_vote(cid: str, voter_id: str, direction: int) -> None:
    if supabase:
        try:
            if direction == 0:
                supabase.table("comment_votes").delete().eq("comment_id", cid).eq("voter_id", voter_id).execute()
            else:
                supabase.table("comment_votes").upsert({
                    "comment_id": cid, "voter_id": voter_id, "direction": direction}).execute()
        except Exception as e: print("[SLD] db_set_comment_vote error:", e)
        return
    for p in POSTS_MEM.values():
        for c in p.get("comments", []):
            if c["id"] == cid:
                if direction == 0: c["votes"].pop(voter_id, None)
                else: c["votes"][voter_id] = direction
                return


def _should_notify(to_nick: str, ntype: str, from_nick: str,
                   users_map: Optional[Dict[str, dict]] = None) -> bool:
    if not to_nick or to_nick == from_nick: return False
    field = NOTIFY_TYPE_TO_FIELD.get(ntype)
    if not field: return True
    u = None
    if users_map is not None:
        u = users_map.get(to_nick.lower())
    else:
        u = db_load_user_cached(to_nick)
    if u is None: return True
    return bool(u.get(field, True))


def db_notify_many(notifications: List[dict], users_map: Optional[Dict[str, dict]] = None) -> None:
    """Массовая вставка уведомлений одним запросом."""
    if not notifications: return
    # Batch-загрузка пользователей (если не передали)
    if users_map is None:
        to_nicks = list(set(n["to_nick"] for n in notifications if n.get("to_nick")))
        users_map = db_load_users_batch(to_nicks)
    valid = []
    for n in notifications:
        if not _should_notify(n["to_nick"], n["ntype"], n["from_nick"], users_map):
            continue
        valid.append({
            "id": uuid.uuid4().hex[:10],
            "to_nick": n["to_nick"], "type": n["ntype"],
            "from_nick": n["from_nick"],
            "post_id": n.get("post_id", "") or "",
            "comment_id": n.get("comment_id") or None,
            "text": n.get("text", "") or "",
            "read": False,
            "created_at": ts_to_iso(time.time()),
        })
    if not valid: return
    if supabase:
        try:
            supabase.table("notifications").insert(valid).execute()
        except Exception as e:
            print("[SLD] db_notify_many error:", e)
    else:
        for row in valid:
            NOTIFS_MEM.setdefault(row["to_nick"], []).append({
                **row, "created_at": time.time()})
    for row in valid:
        bus_publish(row["to_nick"], {"type": "notif_changed"})


def db_notify(to_nick: str, ntype: str, from_nick: str,
              post_id: str = "", comment_id: str = "", text: str = "") -> None:
    db_notify_many([{
        "to_nick": to_nick, "ntype": ntype, "from_nick": from_nick,
        "post_id": post_id, "comment_id": comment_id, "text": text,
    }])


def db_notifications(nick: str) -> List[dict]:
    if supabase:
        try:
            r = (supabase.table("notifications").select("*").eq("to_nick", nick)
                 .order("created_at", desc=True).limit(100).execute())
            rows = r.data or []
            for row in rows: row["created_at"] = iso_to_ts(row.get("created_at"))
            return rows
        except Exception as e:
            print("[SLD] db_notifications error:", e); return []
    items = list(NOTIFS_MEM.get(nick, []))
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items[:100]


def db_notifications_unread_count(nick: str) -> int:
    if supabase:
        try:
            r = (supabase.table("notifications").select("id", count="exact")
                 .eq("to_nick", nick).eq("read", False).limit(1).execute())
            return r.count or 0
        except Exception as e:
            print("[SLD] db_notif_unread error:", e); return 0
    return sum(1 for n in NOTIFS_MEM.get(nick, []) if not n.get("read"))


def db_notifications_mark_read(nick: str) -> None:
    if supabase:
        try: supabase.table("notifications").update({"read": True}).eq("to_nick", nick).eq("read", False).execute()
        except Exception as e: print("[SLD] db_notif_read error:", e)
        return
    for n in NOTIFS_MEM.get(nick, []): n["read"] = True


def db_notifications_clear(nick: str) -> None:
    if supabase:
        try: supabase.table("notifications").delete().eq("to_nick", nick).execute()
        except Exception as e: print("[SLD] db_notif_clear error:", e)
        return
    NOTIFS_MEM[nick] = []


def _rename_user_everywhere(old_nick: str, new_nick: str) -> None:
    if not supabase:
        if old_nick in USERS:
            u = USERS.pop(old_nick); u["nick"] = new_nick; USERS[new_nick] = u
        for p in POSTS_MEM.values():
            if p["author"] == old_nick: p["author"] = new_nick
            for c in p.get("comments", []):
                if c["author"] == old_nick: c["author"] = new_nick
        for u in USERS.values():
            if old_nick in u.get("following", set()):
                u["following"].discard(old_nick); u["following"].add(new_nick)
            if old_nick in u.get("followers", set()):
                u["followers"].discard(old_nick); u["followers"].add(new_nick)
        for items in NOTIFS_MEM.values():
            for n in items:
                if n.get("to_nick") == old_nick: n["to_nick"] = new_nick
                if n.get("from_nick") == old_nick: n["from_nick"] = new_nick
        invalidate_user_cache(); return
    try:
        old = db_load_user(old_nick)
        if not old: return
        payload = _user_payload(old); payload["nick"] = new_nick
        supabase.table("users").insert(payload).execute()
        supabase.table("users").delete().eq("nick", old_nick).execute()
        supabase.table("posts").update({"author": new_nick}).eq("author", old_nick).execute()
        supabase.table("comments").update({"author": new_nick}).eq("author", old_nick).execute()
        supabase.table("post_votes").update({"voter_id": "u:" + new_nick}).eq("voter_id", "u:" + old_nick).execute()
        supabase.table("comment_votes").update({"voter_id": "u:" + new_nick}).eq("voter_id", "u:" + old_nick).execute()
        supabase.table("notifications").update({"to_nick": new_nick}).eq("to_nick", old_nick).execute()
        supabase.table("notifications").update({"from_nick": new_nick}).eq("from_nick", old_nick).execute()
        for u in db_all_users():
            patch = {}
            fl = u.get("following") or set()
            if old_nick in fl: patch["following"] = [new_nick if x == old_nick else x for x in fl]
            fw = u.get("followers") or set()
            if old_nick in fw: patch["followers"] = [new_nick if x == old_nick else x for x in fw]
            if patch: db_update_user_fields(u["nick"], patch)
    except Exception as e: print("[SLD] _rename_user_everywhere error:", e)
    invalidate_user_cache()


def normalize_og(raw):
    if not raw: return None
    if isinstance(raw, dict): return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else None
        except Exception: return None
    return None


def build_posts_full(posts: List[dict], voter_id: str, with_comments: bool = True) -> List[dict]:
    """Оптимизированная сборка постов. Для ленты with_comments=False (без комментариев и голосов за них)."""
    if not posts: return []
    post_ids = [p["id"] for p in posts]

    author_nicks = set()
    for p in posts:
        if p.get("author"): author_nicks.add(p["author"])

    quoted_ids = [p.get("quoted_post_id") for p in posts if p.get("quoted_post_id")]
    quotes = db_get_quotes(list(set(quoted_ids))) if quoted_ids else {}
    for q in quotes.values():
        if q.get("author"): author_nicks.add(q["author"])

    users_map = db_load_users_batch(list(author_nicks))

    votes = db_post_votes(post_ids)
    vmap: Dict[str, Dict[str, int]] = {}
    for v in votes: vmap.setdefault(v["post_id"], {})[v["voter_id"]] = v["direction"]

    if with_comments:
        comments = db_comments_for_posts(post_ids)
        cmap: Dict[str, List[dict]] = {}
        for c in comments: cmap.setdefault(c["post_id"], []).append(c)
        comment_ids = [c["id"] for c in comments]
        cvmap: Dict[str, Dict[str, int]] = {}
        if comment_ids:
            cvotes = db_comment_votes(comment_ids)
            for v in cvotes: cvmap.setdefault(v["comment_id"], {})[v["voter_id"]] = v["direction"]
        ccounts: Dict[str, int] = {}
    else:
        ccounts = db_comment_counts(post_ids)
        cmap = {}; cvmap = {}

    out = []
    for p in posts:
        pvotes = vmap.get(p["id"], {})
        likes = sum(1 for d in pvotes.values() if d == 1)
        uv = pvotes.get(voter_id, 0)

        clist = []
        if with_comments:
            for c in cmap.get(p["id"], []):
                cv = cvmap.get(c["id"], {})
                clikes = sum(1 for d in cv.values() if d == 1)
                cuv = cv.get(voter_id, 0)
                clist.append({"id": c["id"], "text": c["text"], "created_at": c["created_at"],
                              "author": c.get("author"), "parent_id": c.get("parent_id"),
                              "likes": clikes, "user_like": cuv})
            comment_count = len(clist)
        else:
            comment_count = ccounts.get(p["id"], 0)

        quoted = None
        qid = p.get("quoted_post_id")
        if qid and qid in quotes:
            qp = quotes[qid]
            quoted = {"id": qp["id"], "text": qp["text"], "author": qp.get("author"),
                      "created_at": qp["created_at"]}

        author_u = users_map.get((p.get("author") or "").lower()) or {}
        out.append({
            "id": p["id"], "text": p["text"], "created_at": p["created_at"],
            "author": p.get("author"),
            "views": p.get("views") or 0,
            "og_data": normalize_og(p.get("og_data")),
            "device": p.get("device") or None,
            "author_name": author_u.get("name", p.get("author")),
            "author_avatar_emoji": author_u.get("avatar_emoji", DEFAULT_EMOJI),
            "author_show_device": bool(author_u.get("show_device_badge", True)),
            "quoted_post_id": qid, "quoted": quoted,
            "likes": likes, "user_like": uv,
            "comments": clist,
            "comment_count": comment_count,
        })
    return out


def serialize_user(u: dict, viewer_nick: Optional[str] = None) -> dict:
    d = {"nick": u["nick"], "name": u["name"], "bio": u.get("bio") or "",
         "created_at": u["created_at"],
         "followers": len(u.get("followers") or []),
         "following": len(u.get("following") or []),
         "is_me": viewer_nick == u["nick"],
         "avatar_emoji": u.get("avatar_emoji", DEFAULT_EMOJI) or DEFAULT_EMOJI}
    if viewer_nick == u["nick"]:
        d["allow_followers_view"] = u.get("allow_followers_view", True)
        d["allow_following_view"] = u.get("allow_following_view", True)
        d["show_link_previews"] = u.get("show_link_previews", True)
        d["show_device_badge"] = u.get("show_device_badge", True)
        for f in NOTIFY_FIELDS: d[f] = bool(u.get(f, True))
    return d


class PostIn(BaseModel):
    text: str
    quoted_post_id: Optional[str] = None
    og_enabled: bool = True
class PostEditIn(BaseModel): text: str
class CommentIn(BaseModel): text: str; parent_id: Optional[str] = None
class CommentEditIn(BaseModel): text: str
class RegisterIn(BaseModel): name: str; nick: str; password: str; password_confirm: str
class LoginIn(BaseModel): nick: str; password: str
class ProfileUpdateIn(BaseModel):
    name: str; nick: str; bio: str = ""; avatar_emoji: str = ""
class SettingsIn(BaseModel):
    allow_followers_view: Optional[bool] = None
    allow_following_view: Optional[bool] = None
    notify_on_new_post: Optional[bool] = None
    notify_on_follow: Optional[bool] = None
    notify_on_comment: Optional[bool] = None
    notify_on_reply: Optional[bool] = None
    notify_on_mention: Optional[bool] = None
    notify_on_quote: Optional[bool] = None
    show_link_previews: Optional[bool] = None
    show_device_badge: Optional[bool] = None
class RoomIn(BaseModel):
    rooms: List[str] = []
    anon_id: Optional[str] = None


def _build_og_for_text(text: str, enabled: bool):
    if not enabled: return None
    url = extract_first_url(text or "")
    if not url: return None
    return fetch_og_data(url)


@app.post("/api/room")
def api_room(data: RoomIn, request: Request):
    u = get_current_user(request)
    if u:
        bus.set_rooms(u["nick"], data.rooms or []); return {"ok": True}
    if data.anon_id:
        nick = "anon:" + re.sub(r"[^a-zA-Z0-9]", "", data.anon_id)[:32]
        if nick != "anon:":
            bus.set_rooms(nick, data.rooms or ["feed"])
        return {"ok": True}
    return {"ok": True}


@app.post("/api/register")
def api_register(data: RegisterIn, request: Request):
    ip = get_client_ip(request)
    if not rate_limit("reg:" + ip, 5, 3600): raise HTTPException(429, "err_rate_limit")
    name = data.name.strip()
    nick = data.nick.strip().lstrip("@")
    if not NICK_RE.match(nick): raise HTTPException(400, "err_bad_nick")
    if len(name) < 1 or len(name) > 50: raise HTTPException(400, "err_bad_name")
    if len(data.password) < 6: raise HTTPException(400, "err_short_pass")
    if data.password != data.password_confirm: raise HTTPException(400, "err_pass_mismatch")
    if db_load_user(nick): raise HTTPException(400, "err_nick_taken")
    u = {"nick": nick, "name": name, "bio": "",
         "password": hash_password(data.password), "created_at": time.time(),
         "following": set(), "followers": set(),
         "allow_followers_view": True, "allow_following_view": True,
         "avatar_emoji": DEFAULT_EMOJI, "show_link_previews": True,
         "show_device_badge": True}
    for f in NOTIFY_FIELDS: u[f] = True
    db_save_user(u)
    token = new_token()
    SESSIONS[token] = {"nick": nick, "created": time.time()}
    return {"token": token, "user": serialize_user(u, nick)}


@app.post("/api/login")
def api_login(data: LoginIn, request: Request):
    ip = get_client_ip(request)
    if not rate_limit("log:" + ip, 10, 300): raise HTTPException(429, "err_rate_limit")
    nick = data.nick.strip().lstrip("@")
    u = db_load_user(nick)
    if not u or not check_password(data.password, u["password"]):
        raise HTTPException(400, "err_bad_login")
    token = new_token()
    SESSIONS[token] = {"nick": u["nick"], "created": time.time()}
    return {"token": token, "user": serialize_user(u, u["nick"])}


@app.post("/api/logout")
def api_logout(request: Request):
    token = request.headers.get("x-auth")
    if token: SESSIONS.pop(token, None)
    return {"ok": True}


@app.get("/api/me")
def api_me(request: Request):
    u = require_user(request)
    return serialize_user(u, u["nick"])


@app.get("/api/whoami")
def api_whoami(request: Request):
    ua = request.headers.get("user-agent", "")
    ip = get_client_ip(request)
    browser, os_name = parse_user_agent(ua)
    geo = get_geo(ip)
    return {"browser": browser, "os": os_name,
            "city": geo.get("city", ""), "country": geo.get("country", "")}


@app.put("/api/users/me")
def api_update_me(data: ProfileUpdateIn, request: Request):
    me = require_user(request)
    name = data.name.strip(); nick = data.nick.strip().lstrip("@"); bio = data.bio.strip()
    if len(name) < 1 or len(name) > 50: raise HTTPException(400, "err_bad_name")
    if not NICK_RE.match(nick): raise HTTPException(400, "err_bad_nick")
    if len(bio) > MAX_BIO_LEN: raise HTTPException(400, "err_bio_too_long")
    avatar = clean_emoji(data.avatar_emoji) or DEFAULT_EMOJI
    old_nick = me["nick"]
    nick_changed = (nick.lower() != old_nick.lower())
    if nick_changed:
        exists = db_load_user(nick)
        if exists and exists["nick"].lower() != old_nick.lower():
            raise HTTPException(400, "err_nick_taken")
    db_update_user_fields(old_nick, {"name": name, "bio": bio, "avatar_emoji": avatar})
    if nick_changed:
        _rename_user_everywhere(old_nick, nick)
        for t, s in list(SESSIONS.items()):
            if s.get("nick") == old_nick: s["nick"] = nick
        invalidate_user_cache()
    u = db_load_user(nick)
    return {"ok": True, "user": serialize_user(u, u["nick"])}


@app.post("/api/users/me/settings")
def api_set_settings(data: SettingsIn, request: Request):
    me = require_user(request)
    patch = {}
    if data.allow_followers_view is not None: patch["allow_followers_view"] = bool(data.allow_followers_view)
    if data.allow_following_view is not None: patch["allow_following_view"] = bool(data.allow_following_view)
    if data.show_link_previews is not None: patch["show_link_previews"] = bool(data.show_link_previews)
    if data.show_device_badge is not None: patch["show_device_badge"] = bool(data.show_device_badge)
    for f in NOTIFY_FIELDS:
        v = getattr(data, f, None)
        if v is not None: patch[f] = bool(v)
    if patch:
        db_update_user_fields(me["nick"], patch)
        bus_broadcast("profile:" + me["nick"], {"type": "refresh"})
        bus_broadcast("feed", {"type": "refresh"})
    return {"ok": True}


@app.get("/api/users")
def api_users_list(request: Request, q: str = "", only_following: str = ""):
    viewer = get_current_user(request)
    vn = viewer["nick"] if viewer else None
    if only_following == "1" and viewer:
        users_map = db_load_users_batch(list(viewer.get("following") or []))
        out = [serialize_user(u, vn) for u in users_map.values()]
        out.sort(key=lambda x: x["nick"].lower())
        return {"users": out}
    users = db_all_users()
    if q:
        n = q.lower().strip().lstrip("@")
        users = [u for u in users if n in u["nick"].lower() or n in (u.get("name") or "").lower()]
    users.sort(key=lambda u: u["nick"].lower())
    return {"users": [serialize_user(u, vn) for u in users[:200]]}


@app.get("/api/users/{nick}")
def api_user(nick: str, request: Request):
    u = db_load_user(nick)
    if not u: raise HTTPException(404, "not found")
    viewer = get_current_user(request)
    data = serialize_user(u, viewer["nick"] if viewer else None)
    data["is_following"] = bool(viewer and u["nick"] in (viewer.get("following") or set()))
    data["is_follower"] = bool(viewer and viewer["nick"] in (u.get("followers") or set()))
    return data


@app.get("/api/users/{nick}/followers")
def api_followers(nick: str, request: Request):
    u = db_load_user(nick)
    if not u: raise HTTPException(404, "not found")
    viewer = get_current_user(request)
    vn = viewer["nick"] if viewer else None
    is_owner = vn == u["nick"]
    allow = u.get("allow_followers_view", True)
    followers = list(u.get("followers") or [])
    if not allow and not is_owner:
        if vn and vn in followers:
            me_u = db_load_user(vn)
            return {"users": [serialize_user(me_u, vn)], "limited": True}
        raise HTTPException(403, "err_private_followers")
    users_map = db_load_users_batch(followers)
    out = [serialize_user(users_map[n.lower()], vn) for n in followers if n.lower() in users_map]
    out.sort(key=lambda x: x["nick"].lower())
    return {"users": out, "limited": False}


@app.get("/api/users/{nick}/following")
def api_following(nick: str, request: Request):
    u = db_load_user(nick)
    if not u: raise HTTPException(404, "not found")
    viewer = get_current_user(request)
    vn = viewer["nick"] if viewer else None
    is_owner = vn == u["nick"]
    allow = u.get("allow_following_view", True)
    following = list(u.get("following") or [])
    if not allow and not is_owner:
        if vn and vn in following:
            me_u = db_load_user(vn)
            return {"users": [serialize_user(me_u, vn)], "limited": True}
        raise HTTPException(403, "err_private_following")
    users_map = db_load_users_batch(following)
    out = [serialize_user(users_map[n.lower()], vn) for n in following if n.lower() in users_map]
    out.sort(key=lambda x: x["nick"].lower())
    return {"users": out, "limited": False}


@app.post("/api/users/{nick}/follow")
def api_follow(nick: str, request: Request):
    me = require_user(request)
    target = db_load_user(nick)
    if not target: raise HTTPException(404, "not found")
    if target["nick"] == me["nick"]: raise HTTPException(400, "self")
    if target["nick"] not in (me.get("following") or set()):
        nf = set(me.get("following") or set()); nf.add(target["nick"])
        db_update_user_fields(me["nick"], {"following": list(nf)})
        nfw = set(target.get("followers") or set()); nfw.add(me["nick"])
        db_update_user_fields(target["nick"], {"followers": list(nfw)})
        db_notify(target["nick"], "follow", me["nick"])
        bus_broadcast("profile:" + target["nick"], {"type": "refresh"})
    target = db_load_user(nick)
    data = serialize_user(target, me["nick"]); data["is_following"] = True
    return data


@app.post("/api/users/{nick}/unfollow")
def api_unfollow(nick: str, request: Request):
    me = require_user(request)
    target = db_load_user(nick)
    if not target: raise HTTPException(404, "not found")
    if target["nick"] in (me.get("following") or set()):
        nf = set(me.get("following") or set()); nf.discard(target["nick"])
        db_update_user_fields(me["nick"], {"following": list(nf)})
        nfw = set(target.get("followers") or set()); nfw.discard(me["nick"])
        db_update_user_fields(target["nick"], {"followers": list(nfw)})
        bus_broadcast("profile:" + target["nick"], {"type": "refresh"})
    target = db_load_user(nick)
    data = serialize_user(target, me["nick"]); data["is_following"] = False
    return data


@app.get("/api/posts")
def api_list(request: Request, q: str = "", author: str = "", feed: str = ""):
    subscriptions_of = ""
    if feed == "subs":
        u = get_current_user(request)
        if u: subscriptions_of = u["nick"]
        else: return {"posts": []}
    posts = db_list_posts(q=q, author=author, subscriptions_of=subscriptions_of)
    u = get_current_user(request)
    vid = "u:" + u["nick"] if u else "c:anon"
    return {"posts": build_posts_full(posts, vid, with_comments=False)}


@app.get("/api/posts/{pid}")
def api_get(pid: str, request: Request):
    p = db_get_post(pid)
    if not p: raise HTTPException(404, "not found")
    u = get_current_user(request)
    vid = "u:" + u["nick"] if u else "c:anon"
    return build_posts_full([p], vid, with_comments=True)[0]


@app.post("/api/posts/{pid}/view")
def api_view_post(pid: str, request: Request):
    p = db_get_post(pid)
    if not p: raise HTTPException(404, "not found")
    u = get_current_user(request)
    if not u: return {"ok": True, "counted": False}
    if p.get("author") == u["nick"]: return {"ok": True, "counted": False}
    vid = "u:" + u["nick"]
    if not view_should_count(pid, vid): return {"ok": True, "counted": False}
    view_mark_counted(pid, vid)
    new_views = db_inc_views(pid)
    broadcast_view_update(pid, p.get("author"), new_views)
    return {"ok": True, "counted": True, "views": new_views}


@app.post("/api/posts")
def api_create(payload: PostIn, request: Request):
    u = require_user(request)
    ip = get_client_ip(request)
    if not rate_limit("post:" + ip, 30, 60): raise HTTPException(429, "err_rate_limit")
    text = payload.text.strip()
    if not text and not payload.quoted_post_id: raise HTTPException(400, "empty")
    if len(text) > MAX_POST_LEN: raise HTTPException(400, "too long")
    pid = uuid.uuid4().hex[:10]
    og = _build_og_for_text(text, payload.og_enabled)
    device = detect_device(request.headers.get("user-agent", ""))
    p = {"id": pid, "text": text, "author": u["nick"],
         "created_at": time.time(), "device": device,
         "quoted_post_id": payload.quoted_post_id or None,
         "og_data": og}
    db_create_post(p)

    # Batch mentions lookup + batch notify
    mentions = extract_mentions(text)
    mention_nicks = [m for m in mentions if m.lower() != u["nick"].lower()]
    mention_users = db_load_users_batch(mention_nicks) if mention_nicks else {}
    notifications: List[dict] = []
    notified = set()
    for m in mention_nicks:
        k = m.lower()
        if k in notified: continue
        if k not in mention_users: continue
        notifications.append({"to_nick": m, "ntype": "mention", "from_nick": u["nick"],
                              "post_id": pid, "text": text[:140]})
        notified.add(k)
    for f in (u.get("followers") or set()):
        if f.lower() == u["nick"].lower(): continue
        if f.lower() in notified: continue
        notifications.append({"to_nick": f, "ntype": "new_post", "from_nick": u["nick"],
                              "post_id": pid, "text": text[:140]})
        notified.add(f.lower())
    if payload.quoted_post_id:
        qp = db_get_post(payload.quoted_post_id)
        if qp and qp.get("author") and qp["author"].lower() != u["nick"].lower():
            notifications.append({"to_nick": qp["author"], "ntype": "quote", "from_nick": u["nick"],
                                  "post_id": pid, "text": text[:140]})
    if notifications:
        db_notify_many(notifications)
    broadcast_post_change(p)
    return build_posts_full([p], "u:" + u["nick"], with_comments=True)[0]


@app.put("/api/posts/{pid}")
def api_edit_post(pid: str, payload: PostEditIn, request: Request):
    u = require_user(request)
    p = db_get_post(pid)
    if not p: raise HTTPException(404, "not found")
    if p["author"] != u["nick"]: raise HTTPException(403, "forbidden")
    text = payload.text.strip()
    if not text: raise HTTPException(400, "empty")
    if len(text) > MAX_POST_LEN: raise HTTPException(400, "too long")
    db_update_post_text(pid, text)
    p["text"] = text
    broadcast_post_change(p)
    return {"ok": True, "text": text}


@app.delete("/api/posts/{pid}")
def api_delete_post(pid: str, request: Request):
    u = require_user(request)
    p = db_get_post(pid)
    if not p: raise HTTPException(404, "not found")
    if p["author"] != u["nick"]: raise HTTPException(403, "forbidden")
    author = p.get("author")
    db_delete_post(pid)
    bus_broadcast("post:" + pid, {"type": "post_deleted", "post_id": pid})
    bus_broadcast("feed", {"type": "refresh"}, except_nick=u["nick"])
    if author: bus_broadcast("profile:" + author, {"type": "refresh"}, except_nick=u["nick"])
    return {"ok": True}


@app.post("/api/posts/{pid}/like")
def api_like_post(pid: str, request: Request):
    p = db_get_post(pid)
    if not p: raise HTTPException(404, "not found")
    u = require_user(request)
    vid = "u:" + u["nick"]
    existing = db_post_votes([pid])
    cur = 0
    for row in existing:
        if row["voter_id"] == vid: cur = row["direction"]; break
    new = 0 if cur == 1 else 1
    db_set_post_vote(pid, vid, new)
    likes_delta = (1 if new == 1 else 0) - (1 if cur == 1 else 0)
    # Получим актуальное число лайков быстро
    try:
        allv = db_post_votes([pid])
        likes = sum(1 for r in allv if r["direction"] == 1)
    except Exception:
        likes = max(0, (p.get("likes") or 0) + likes_delta)
    p["text"] = p.get("text") or ""
    broadcast_post_change(p, except_nick=u["nick"])
    return {"ok": True, "likes": likes, "user_like": new}


@app.post("/api/posts/{pid}/comments")
def api_add_comment(pid: str, c: CommentIn, request: Request):
    u = require_user(request)
    ip = get_client_ip(request)
    if not rate_limit("cmt:" + ip, 60, 60): raise HTTPException(429, "err_rate_limit")
    post = db_get_post(pid)
    if not post: raise HTTPException(404, "not found")
    text = c.text.strip()
    if not text: raise HTTPException(400, "empty")
    if len(text) > MAX_COMMENT_LEN: raise HTTPException(400, "too long")
    parent_id = c.parent_id or None
    parent = None
    if parent_id:
        parent = db_get_comment(parent_id)
        if not parent or parent["post_id"] != pid: raise HTTPException(400, "bad parent")
        if parent.get("parent_id"): raise HTTPException(400, "reply_only_one_level")
    cid = uuid.uuid4().hex[:10]
    now_ts = time.time()
    db_create_comment({"id": cid, "post_id": pid, "author": u["nick"],
                       "parent_id": parent_id, "text": text, "created_at": now_ts})

    # Batch notifications
    mentions = extract_mentions(text)
    notifications: List[dict] = []
    notified = set()
    if parent_id and parent:
        if parent["author"].lower() != u["nick"].lower():
            notifications.append({"to_nick": parent["author"], "ntype": "reply",
                                  "from_nick": u["nick"], "post_id": pid,
                                  "comment_id": cid, "text": text[:140]})
            notified.add(parent["author"].lower())
    else:
        if post["author"].lower() != u["nick"].lower():
            notifications.append({"to_nick": post["author"], "ntype": "comment",
                                  "from_nick": u["nick"], "post_id": pid,
                                  "comment_id": cid, "text": text[:140]})
            notified.add(post["author"].lower())
    mention_nicks = [m for m in mentions if m.lower() != u["nick"].lower()]
    mention_users = db_load_users_batch(mention_nicks) if mention_nicks else {}
    for m in mention_nicks:
        k = m.lower()
        if k in notified: continue
        if k not in mention_users: continue
        notifications.append({"to_nick": m, "ntype": "mention", "from_nick": u["nick"],
                              "post_id": pid, "comment_id": cid, "text": text[:140]})
        notified.add(k)
    if notifications:
        db_notify_many(notifications, users_map=mention_users)

    broadcast_post_change(post, except_nick=u["nick"])
    return {
        "ok": True,
        "post_id": pid,
        "post_author": post["author"],
        "comment": {
            "id": cid, "text": text, "author": u["nick"],
            "parent_id": parent_id, "created_at": ts_to_iso(now_ts),
            "likes": 0, "user_like": 0,
        }
    }


@app.put("/api/posts/{pid}/comments/{cid}")
def api_edit_comment(pid: str, cid: str, payload: CommentEditIn, request: Request):
    u = require_user(request)
    c = db_get_comment(cid)
    if not c or c["post_id"] != pid: raise HTTPException(404, "not found")
    if c["author"] != u["nick"]: raise HTTPException(403, "forbidden")
    text = payload.text.strip()
    if not text: raise HTTPException(400, "empty")
    if len(text) > MAX_COMMENT_LEN: raise HTTPException(400, "too long")
    db_update_comment_text(cid, text)
    post = db_get_post(pid)
    broadcast_post_change(post, except_nick=u["nick"])
    return {"ok": True, "text": text}


@app.delete("/api/posts/{pid}/comments/{cid}")
def api_delete_comment(pid: str, cid: str, request: Request):
    u = require_user(request)
    c = db_get_comment(cid)
    if not c or c["post_id"] != pid: raise HTTPException(404, "not found")
    if c["author"] != u["nick"]: raise HTTPException(403, "forbidden")
    db_delete_comment(cid)
    post = db_get_post(pid)
    broadcast_post_change(post, except_nick=u["nick"])
    return {"ok": True}


@app.post("/api/posts/{pid}/comments/{cid}/like")
def api_like_comment(pid: str, cid: str, request: Request):
    p = db_get_post(pid)
    if not p: raise HTTPException(404, "not found")
    u = require_user(request)
    vid = "u:" + u["nick"]
    existing = db_comment_votes([cid])
    cur = 0
    for row in existing:
        if row["voter_id"] == vid: cur = row["direction"]; break
    new = 0 if cur == 1 else 1
    db_set_comment_vote(cid, vid, new)
    try:
        allv = db_comment_votes([cid])
        likes = sum(1 for r in allv if r["direction"] == 1)
    except Exception:
        likes = 0
    broadcast_post_change(p, except_nick=u["nick"])
    return {"ok": True, "likes": likes, "user_like": new}


@app.get("/api/counters")
def api_counters(request: Request):
    me = require_user(request)
    return {"notif": db_notifications_unread_count(me["nick"])}


@app.get("/api/notifications")
def api_notifications(request: Request):
    me = require_user(request)
    items = db_notifications(me["nick"])
    unread = sum(1 for n in items if not n.get("read"))
    return {"items": items, "unread": unread}


@app.post("/api/notifications/read")
def api_notifications_read(request: Request):
    me = require_user(request)
    db_notifications_mark_read(me["nick"]); return {"ok": True}


@app.post("/api/notifications/clear")
def api_notifications_clear(request: Request):
    me = require_user(request)
    db_notifications_clear(me["nick"]); return {"ok": True}


@app.get("/api/events")
async def api_events(request: Request, token: str = "", anon: str = ""):
    if token:
        sess = SESSIONS.get(token)
        if not sess: raise HTTPException(401, "unauthorized")
        nick = sess["nick"]
        u = db_load_user_cached(nick)
        if not u: raise HTTPException(401, "unauthorized")
    elif anon:
        clean = re.sub(r"[^a-zA-Z0-9]", "", anon)[:32]
        nick = "anon:" + (clean or uuid.uuid4().hex[:12])
        bus.set_rooms(nick, ["feed"])
    else:
        nick = "anon:" + uuid.uuid4().hex[:12]
        bus.set_rooms(nick, ["feed"])

    q = await bus.subscribe(nick)
    async def gen():
        try:
            yield "retry: 3000\n\n"
            yield f"data: {json.dumps({'type':'hello','nick':nick}, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected(): break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ka\n\n"
        except asyncio.CancelledError: pass
        finally:
            bus.unsubscribe(nick, q)
            invalidate_user_cache(nick)
    return StreamingResponse(gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform",
                 "X-Accel-Buffering": "no", "Connection": "keep-alive"})


TEXTS = {
    "ru": {
        "search_ph": "Поиск людей и постов",
        "post_ph": "Что нового?",
        "comment_ph": "Комментарий... Shift+Enter — отправить",
        "reply_ph": "Ответ...",
        "publish": "Опубликовать",
        "send_comment": "Отправить",
        "reply": "Ответить", "cancel_reply": "Отмена",
        "no_posts": "Здесь пока пусто",
        "not_found": "Ничего не найдено",
        "page_not_found": "Страница не найдена или была удалена",
        "back_home": "На главную",
        "just_now": "только что", "sec_ago": "с", "min_ago": "мин", "hour_ago": "ч", "day_ago": "д",
        "read_more": "Показать полностью",
        "copy": "Копировать",
        "edit": "Редактировать", "delete": "Удалить", "save": "Сохранить", "cancel": "Отмена",
        "confirm_delete": "Удалить без возможности восстановления?",
        "confirm_delete_yes": "Да, удалить",
        "confirm_logout": "Вы действительно хотите выйти?",
        "confirm_yes": "Да, выйти", "confirm_no": "Отмена",
        "notif_clear": "Очистить",
        "notif_clear_confirm": "Удалить все уведомления?",
        "author_badge": "автор",
        "feed_all": "Для вас", "feed_subs": "Подписки",
        "nav_home": "Лента", "nav_users": "Люди",
        "nav_profile": "Профиль", "nav_notifications": "Уведомления",
        "nav_settings": "Настройки", "nav_logout": "Выйти",
        "nav_login": "Войти", "nav_register": "Регистрация",
        "reg_title": "Регистрация", "log_title": "Вход",
        "name_ph": "Имя", "nick_ph": "Ник (без @)",
        "pass_ph": "Пароль", "pass2_ph": "Повтор пароля",
        "reg_btn": "Зарегистрироваться", "log_btn": "Войти",
        "to_login": "Уже зарегистрированы? Войти",
        "to_reg": "Нет аккаунта? Зарегистрироваться",
        "err_bad_nick": "Ник: 3-20 символов, латиница, цифры, _",
        "err_bad_name": "Имя: от 1 до 50 символов",
        "err_short_pass": "Пароль: минимум 6 символов",
        "err_pass_mismatch": "Пароли не совпадают",
        "err_nick_taken": "Этот ник уже занят",
        "err_bad_login": "Неверный ник или пароль",
        "err_rate_limit": "Слишком много запросов, подождите",
        "err_private_followers": "Пользователь скрыл подписчиков",
        "err_private_following": "Пользователь скрыл подписки",
        "err_bio_too_long": "Описание слишком длинное (до 300 символов)",
        "login_to_post": "Войдите, чтобы публиковать",
        "login_to_comment": "Войдите, чтобы комментировать",
        "go_login": "Войти",
        "profile_followers": "подписчиков", "profile_following": "подписок",
        "follow": "Подписаться", "unfollow": "Отписаться",
        "edit_profile": "Редактировать",
        "edit_profile_title": "Изменить профиль",
        "no_user_posts": "Здесь пока пусто",
        "tab_posts": "Посты",
        "settings_title": "Настройки",
        "settings_account": "Аккаунт",
        "settings_privacy": "Конфиденциальность",
        "settings_notifications": "Уведомления",
        "settings_appearance": "Внешний вид",
        "settings_info": "О приложении",
        "settings_theme": "Тема",
        "settings_lang": "Язык",
        "settings_colors": "Основные цвета",
        "settings_colors_hint": "Цвет кнопок, переключателей и подсветок",
        "settings_allow_followers": "Показывать список подписчиков",
        "settings_allow_following": "Показывать список подписок",
        "settings_show_link_previews": "Показывать превью ссылок (OpenGraph)",
        "settings_show_link_previews_hint": "Если включено, к постам со ссылками будут добавляться карточки предпросмотра",
        "settings_show_device_badge": "Показывать значок устройства на постах",
        "settings_show_device_badge_hint": "Рядом с вашим именем будет отображаться иконка телефона или компьютера, с которого был опубликован пост",
        "settings_device": "Ваше устройство",
        "settings_device_browser": "Браузер",
        "settings_device_os": "Система",
        "settings_device_city": "Город",
        "settings_policy": "Политика конфиденциальности",
        "settings_desc": "SLD — минималистичная соцсеть: посты и комментарии.",
        "settings_authors": "Авторы",
        "settings_logout": "Выйти",
        "theme_light": "Светлая", "theme_dark": "Тёмная",
        "notif_title": "Уведомления", "notif_empty": "Здесь пока пусто",
        "notif_follow": "подписался на вас",
        "notif_comment": "оставил комментарий",
        "notif_reply": "ответил на ваш комментарий",
        "notif_mention": "упомянул вас",
        "notif_new_post": "опубликовал новый пост",
        "notif_quote": "процитировал ваш пост",
        "notify_new_post": "Новые посты подписок",
        "notify_follow": "Новые подписчики",
        "notify_comment": "Комментарии к моим постам",
        "notify_reply": "Ответы на мои комментарии",
        "notify_mention": "Упоминания меня",
        "notify_quote": "Цитаты моих постов",
        "back": "Назад",
        "bio_ph": "О себе (до 300 символов)...",
        "followers_title": "Подписчики", "following_title": "Подписки",
        "no_followers": "Подписчиков пока нет", "no_following": "Подписок пока нет",
        "limited_list": "Список скрыт. Вам виден только ваш аккаунт.",
        "people_title": "Люди",
        "people_search_ph": "Поиск людей",
        "people_all": "Все", "people_subs": "Подписки",
        "no_users": "Никого не найдено",
        "policy_title": "Политика конфиденциальности",
        "policy_content": (
            "Мы уважаем вашу конфиденциальность и собираем минимум данных.\n\n"
            "КАКИЕ ДАННЫЕ ХРАНЯТСЯ\n"
            "• Имя, ник, эмодзи-аватар, описание (био)\n"
            "• Хеш пароля (pbkdf2-hmac-sha256, 100 000 итераций, соль). Восстановить пароль невозможно даже нам.\n"
            "• Посты, комментарии, лайки, подписки, уведомления\n"
            "• Ваши настройки приватности, уведомлений и внешнего вида\n"
            "• Устройство (мобильное / компьютерное) в момент публикации поста — только для отображения значка рядом с постом. Можно отключить в настройках.\n\n"
            "ЧЕГО МЫ НЕ ХРАНИМ\n"
            "• Пароль в открытом виде\n"
            "• Ваш IP-адрес. IP используется только для определения языка и города в момент загрузки страницы и не сохраняется в базе.\n"
            "• Историю просмотров постов (учитывается только счётчик, и только с интервалом 8 часов на пользователя).\n"
            "• Никаких сторонних cookies, аналитики, трекеров, рекламных идентификаторов.\n\n"
            "КАК ИСПОЛЬЗУЮТСЯ ДАННЫЕ\n"
            "• Для отображения вашего профиля и постов другим пользователям\n"
            "• Для отправки уведомлений о действиях других пользователей\n"
            "• Для определения языка и города — только чтобы показать их вам в настройках\n"
            "• Никакой аналитики, никакой рекламы, никакого трекинга.\n\n"
            "ГДЕ ХРАНЯТСЯ ДАННЫЕ\n"
            "Все данные хранятся на серверах Supabase (PostgreSQL). Обмен между клиентом и сервером происходит по HTTPS. Мы не продаём и не передаём данные третьим лицам.\n\n"
            "ВАШИ ПРАВА\n"
            "• Смотреть и редактировать свой профиль\n"
            "• Удалять свои посты и комментарии\n"
            "• Отписаться от пользователей в любой момент\n"
            "• Отключить уведомления любого типа в настройках\n"
            "• Скрыть список подписчиков и подписок\n"
            "• Отключить показ значка устройства на своих постах\n"
            "• Удалить аккаунт — напишите нам, и мы удалим все ваши данные в течение 30 дней\n\n"
            "ХРАНЕНИЕ И УДАЛЕНИЕ\n"
            "Данные хранятся пока активен ваш аккаунт. При удалении аккаунта все посты, комментарии, лайки, подписки и уведомления удаляются безвозвратно.\n\n"
            "COOKIES И LOCALSTORAGE\n"
            "Мы используем:\n"
            "• localStorage: SLD_token (токен сессии), SLD_user (кэш профиля), SLD_theme (тема), SLD_lang (язык), SLD_colors (цвета), SLD_anon_id (анонимный идентификатор для realtime)\n"
            "• 1 cookie: SLD_lang — только для хранения выбранного языка\n"
            "Сторонних cookies нет.\n\n"
            "ДЕТИ\n"
            "Сервис не предназначен для лиц младше 13 лет. Мы не собираем данные детей намеренно.\n\n"
            "ИЗМЕНЕНИЯ\n"
            "Мы можем обновлять эту политику. Актуальная версия всегда доступна по этой ссылке.\n\n"
            "КОНТАКТЫ\n"
            "По вопросам приватности и удаления данных — напишите нам через профиль разработчика.\n\n"
            "Сервис предоставляется «как есть», без гарантий."
        ),
        "quote": "Цитировать",
        "avatar_choose": "Выберите эмодзи",
        "avatar_current": "Текущий аватар",
        "og_enable": "Превью ссылок",
        "color_blue": "Синий",
        "color_green": "Зелёный",
        "color_purple": "Фиолетовый",
        "color_pink": "Розовый",
        "color_orange": "Оранжевый",
        "color_red": "Красный",
        "color_teal": "Бирюзовый",
        "color_indigo": "Индиго",
        "color_accent": "Акцент",
        "color_likes": "Лайки",
        "reset_colors": "Сбросить цвета",
        "sent_from_mobile": "Отправлено с телефона",
        "sent_from_desktop": "Отправлено с компьютера",
        "og_img_fallback": "Упсс... не удалось загрузить :(",
    },
    "en": {
        "search_ph": "Search people and posts",
        "post_ph": "What's new?",
        "comment_ph": "Comment... Shift+Enter to send",
        "reply_ph": "Reply...",
        "publish": "Publish",
        "send_comment": "Send",
        "reply": "Reply", "cancel_reply": "Cancel",
        "no_posts": "Nothing here yet",
        "not_found": "Not found",
        "page_not_found": "Page not found or was deleted",
        "back_home": "Back to home",
        "just_now": "just now", "sec_ago": "s", "min_ago": "min", "hour_ago": "h", "day_ago": "d",
        "read_more": "Show more",
        "copy": "Copy",
        "edit": "Edit", "delete": "Delete", "save": "Save", "cancel": "Cancel",
        "confirm_delete": "Delete permanently?",
        "confirm_delete_yes": "Yes, delete",
        "confirm_logout": "Are you sure you want to log out?",
        "confirm_yes": "Yes, log out", "confirm_no": "Cancel",
        "notif_clear": "Clear",
        "notif_clear_confirm": "Clear all notifications?",
        "author_badge": "author",
        "feed_all": "For you", "feed_subs": "Subscriptions",
        "nav_home": "Feed", "nav_users": "People",
        "nav_profile": "Profile", "nav_notifications": "Notifications",
        "nav_settings": "Settings", "nav_logout": "Log out",
        "nav_login": "Log in", "nav_register": "Sign up",
        "reg_title": "Sign up", "log_title": "Log in",
        "name_ph": "Name", "nick_ph": "Nick (no @)",
        "pass_ph": "Password", "pass2_ph": "Confirm password",
        "reg_btn": "Sign up", "log_btn": "Log in",
        "to_login": "Already have an account? Log in",
        "to_reg": "No account? Sign up",
        "err_bad_nick": "Nick: 3-20 chars, letters/digits/_",
        "err_bad_name": "Name: 1-50 chars",
        "err_short_pass": "Password: min 6 chars",
        "err_pass_mismatch": "Passwords do not match",
        "err_nick_taken": "Nick already taken",
        "err_bad_login": "Wrong nick or password",
        "err_rate_limit": "Too many requests, please wait",
        "err_private_followers": "User hid their followers",
        "err_private_following": "User hid their following",
        "err_bio_too_long": "Bio is too long (max 300 chars)",
        "login_to_post": "Log in to publish",
        "login_to_comment": "Log in to comment",
        "go_login": "Log in",
        "profile_followers": "followers", "profile_following": "following",
        "follow": "Follow", "unfollow": "Unfollow",
        "edit_profile": "Edit",
        "edit_profile_title": "Edit profile",
        "no_user_posts": "Nothing here yet",
        "tab_posts": "Posts",
        "settings_title": "Settings",
        "settings_account": "Account",
        "settings_privacy": "Privacy",
        "settings_notifications": "Notifications",
        "settings_appearance": "Appearance",
        "settings_info": "About",
        "settings_theme": "Theme",
        "settings_lang": "Language",
        "settings_colors": "Accent colors",
        "settings_colors_hint": "Color of buttons, toggles and highlights",
        "settings_allow_followers": "Show followers list",
        "settings_allow_following": "Show following list",
        "settings_show_link_previews": "Show link previews (OpenGraph)",
        "settings_show_link_previews_hint": "If enabled, posts with links will show preview cards",
        "settings_show_device_badge": "Show device badge on posts",
        "settings_show_device_badge_hint": "A phone or desktop icon will appear next to your name on posts, indicating the device used to publish",
        "settings_device": "Your device",
        "settings_device_browser": "Browser",
        "settings_device_os": "System",
        "settings_device_city": "City",
        "settings_policy": "Privacy policy",
        "settings_desc": "SLD — minimalist social network: posts and comments.",
        "settings_authors": "Authors",
        "settings_logout": "Log out",
        "theme_light": "Light", "theme_dark": "Dark",
        "notif_title": "Notifications", "notif_empty": "Nothing here yet",
        "notif_follow": "followed you",
        "notif_comment": "commented",
        "notif_reply": "replied to your comment",
        "notif_mention": "mentioned you",
        "notif_new_post": "published a new post",
        "notif_quote": "quoted your post",
        "notify_new_post": "New posts from subscriptions",
        "notify_follow": "New followers",
        "notify_comment": "Comments on my posts",
        "notify_reply": "Replies to my comments",
        "notify_mention": "Mentions of me",
        "notify_quote": "Quotes of my posts",
        "back": "Back",
        "bio_ph": "Bio (max 300 chars)...",
        "followers_title": "Followers", "following_title": "Following",
        "no_followers": "No followers yet", "no_following": "No following yet",
        "limited_list": "List is hidden. You can see only your account.",
        "people_title": "People",
        "people_search_ph": "Search people",
        "people_all": "All", "people_subs": "Subscriptions",
        "no_users": "No users found",
        "policy_title": "Privacy Policy",
        "policy_content": (
            "We respect your privacy and collect the minimum amount of data.\n\n"
            "DATA WE STORE\n"
            "• Name, nick, emoji avatar, bio\n"
            "• Password hash (pbkdf2-hmac-sha256, 100 000 iterations, salted). Even we cannot recover your password.\n"
            "• Posts, comments, likes, follows, notifications\n"
            "• Your privacy, notification and appearance settings\n"
            "• Device type (mobile / desktop) at the moment of posting — only to display a badge next to the post. Can be disabled in settings.\n\n"
            "WHAT WE DO NOT STORE\n"
            "• Your password in plain text\n"
            "• Your IP address. IP is used only to detect language and city at page load and is not saved to the database.\n"
            "• Post view history (only a counter is kept, at most once per 8 hours per user).\n"
            "• No third-party cookies, analytics, trackers, or ad identifiers.\n\n"
            "HOW WE USE DATA\n"
            "• To display your profile and posts to other users\n"
            "• To send you notifications about other users' actions\n"
            "• To detect language and city — only to show them to you in settings\n"
            "• No analytics, no ads, no tracking.\n\n"
            "WHERE DATA IS STORED\n"
            "All data is stored on Supabase servers (PostgreSQL). Client-server communication uses HTTPS. We never sell or share your data with third parties.\n\n"
            "YOUR RIGHTS\n"
            "• View and edit your profile\n"
            "• Delete your posts and comments\n"
            "• Unfollow users at any time\n"
            "• Disable any type of notification in settings\n"
            "• Hide your followers / following lists\n"
            "• Disable the device badge on your posts\n"
            "• Delete your account — write to us and we will remove all your data within 30 days\n\n"
            "RETENTION & DELETION\n"
            "Data is retained while your account is active. When you delete your account, all posts, comments, likes, follows and notifications are permanently removed.\n\n"
            "COOKIES AND LOCALSTORAGE\n"
            "We use:\n"
            "• localStorage: SLD_token (session token), SLD_user (profile cache), SLD_theme (theme), SLD_lang (language), SLD_colors (colors), SLD_anon_id (anonymous identifier for realtime)\n"
            "• One cookie: SLD_lang — only to store your language choice\n"
            "No third-party cookies.\n\n"
            "CHILDREN\n"
            "The service is not intended for users under 13. We do not knowingly collect data from children.\n\n"
            "CHANGES\n"
            "We may update this policy. The current version is always available at this link.\n\n"
            "CONTACT\n"
            "For privacy questions or data deletion — write to us via the developer's profile.\n\n"
            "The service is provided \"as is\", without warranties."
        ),
        "quote": "Quote",
        "avatar_choose": "Choose an emoji",
        "avatar_current": "Current avatar",
        "og_enable": "Link previews",
        "color_blue": "Blue", "color_green": "Green", "color_purple": "Purple",
        "color_pink": "Pink", "color_orange": "Orange", "color_red": "Red",
        "color_teal": "Teal", "color_indigo": "Indigo",
        "color_accent": "Accent", "color_likes": "Likes",
        "reset_colors": "Reset colors",
        "sent_from_mobile": "Sent from a phone",
        "sent_from_desktop": "Sent from a computer",
        "og_img_fallback": "Oops... could not load :(",
    },
}


def svg(paths: str, size: int = 20, sw: float = 2) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="{sw}" stroke-linecap="round" '
            f'stroke-linejoin="round">{paths}</svg>')


I_HOME = svg('<path d="M3 10l9-7 9 7v11a2 2 0 0 1-2 2h-4v-8h-6v8H5a2 2 0 0 1-2-2z"/>')
I_USERS = svg('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/>'
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/>')
I_USER = svg('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>')
I_BELL = svg('<path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>'
    '<path d="M13.7 21a2 2 0 0 1-3.4 0"/>')
I_GEAR = svg('<circle cx="12" cy="12" r="3"/>'
    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>')
I_LOGOUT = svg('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
    '<polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>')
I_LOGIN = svg('<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>'
    '<polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>')
I_PLUS = svg('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>')
I_SEND = svg('<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>', size=20)
I_HEART = svg('<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>', size=16)
I_HEART_FILLED = svg('<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z" fill="currentColor"/>', size=16)
I_COMMENT = svg('<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>', size=16)
I_QUOTE = svg('<path d="M6 17h3l2-4V7H5v6h3z"/><path d="M14 17h3l2-4V7h-6v6h3z"/>', size=16)
I_COPY = svg('<rect x="9" y="9" width="12" height="12"/><path d="M5 15H3V3h12v2"/>', size=16)
I_EDIT = svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>', size=16)
I_TRASH = svg('<polyline points="3 6 5 6 21 6"/>'
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
    '<path d="M10 11v6M14 11v6"/>', size=16)
I_BACK = svg('<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>')
I_SEARCH = svg('<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>')
I_MOON = svg('<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>')
I_SUN = svg('<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/>'
    '<line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/>'
    '<line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/>'
    '<line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/>'
    '<line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/>')
I_EYE = svg('<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>', size=15)
I_CAL = svg('<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>', size=14)
I_CHECK = svg('<polyline points="20 6 9 17 4 12"/>', size=14, sw=3)
I_MOBILE = svg('<rect x="5" y="2" width="14" height="20" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/>', size=12)
I_DESKTOP = svg('<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>', size=12)

# Круглая адаптивная иконка. В светлой теме — чёрным по белому, в тёмной — белым по чёрному.
FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
    "%3Cstyle%3E"
    ".bg%7Bfill:%23ffffff%7D"
    ".tx%7Bfill:%23000000%7D"
    "@media(prefers-color-scheme:dark)%7B"
    ".bg%7Bfill:%23000000%7D"
    ".tx%7Bfill:%23ffffff%7D"
    "%7D"
    "%3C/style%3E"
    "%3Ccircle class='bg' cx='50' cy='50' r='50'/%3E"
    "%3Ctext class='tx' x='50' y='68' font-family='Arial Black,Arial,sans-serif' "
    "font-weight='900' font-size='40' text-anchor='middle'%3ESLD%3C/text%3E"
    "%3C/svg%3E"
)


CSS = """
:root, [data-theme="dark"] {
  --bg:#0a0a0a; --card:#121212; --card-2:#1a1a1a; --line:#232323; --line-2:#2c2c2c;
  --text:#f2f2f2; --muted:#8a8a8a; --hover:#1e1e1e;
  --accent:#3b82f6; --accent-fg:#ffffff; --accent-soft:#152238;
  --like:#ef4444; --danger:#ef4444; --mention:#7aa2ff;
  --toggle-on:#3b82f6; --toggle-on-fg:#ffffff;
}
[data-theme="light"] {
  --bg:#f2f3f5; --card:#ffffff; --card-2:#f0f1f3; --line:#e6e6e9; --line-2:#d6d6da;
  --text:#0a0a0a; --muted:#707070; --hover:#f0f1f3;
  --accent:#2563eb; --accent-fg:#ffffff; --accent-soft:#e0ecff;
  --like:#dc2626; --danger:#dc2626; --mention:#2b6fff;
  --toggle-on:#2563eb; --toggle-on-fg:#ffffff;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body {
  height: 100vh; height: 100dvh; margin: 0; padding: 0;
  overflow: hidden; overscroll-behavior: none;
}
body {
  font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg); color: var(--text); font-size: 15px;
  -webkit-font-smoothing: antialiased;
  user-select: none; -webkit-user-select: none;
}
input, textarea { user-select: text; -webkit-user-select: text; font-size: 16px; }
* { scrollbar-width: none; -ms-overflow-style: none; }
*::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }

.layout {
  display: flex; width: 100%; height: 100vh; height: 100dvh;
  background: var(--bg); max-width: 1040px; margin: 0 auto;
}
.sidebar {
  flex: 0 0 240px; width: 240px;
  background: var(--bg); display: flex; flex-direction: column;
  padding: 24px 14px 16px; overflow: hidden;
}
.sidebar .logo {
  font-size: 22px; font-weight: 900; letter-spacing: 2px;
  padding: 4px 12px 28px; color: var(--text);
}
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-btn {
  display: flex; align-items: center; gap: 14px;
  width: 100%; padding: 12px 14px;
  border: none; background: transparent;
  color: var(--text); font: inherit; font-size: 15px; font-weight: 500;
  cursor: pointer; text-align: left; border-radius: 12px;
  transition: background .12s; position: relative;
}
.nav-btn:hover { background: var(--hover); }
.nav-btn.active { background: var(--accent-soft); color: var(--text); font-weight: 700; }
.nav-btn svg { flex-shrink: 0; color: var(--muted); }
.nav-btn.active svg { color: var(--accent); }
.nav-btn .badge {
  margin-left: auto; min-width: 22px; height: 22px;
  background: var(--danger); color: #fff;
  font-size: 12px; font-weight: 700; line-height: 22px;
  text-align: center; padding: 0 7px; border-radius: 11px;
}
.sidebar .spacer { flex: 1; }
.sidebar-logout {
  display: flex; align-items: center; gap: 14px;
  padding: 12px 14px; color: var(--text);
  font-size: 15px; font-weight: 500;
  background: transparent; border: none; cursor: pointer;
  border-radius: 12px; text-align: left; width: 100%;
  transition: background .12s; overflow: hidden;
}
.sidebar-logout:hover { background: var(--hover); }
.sidebar-logout svg { color: var(--muted); flex-shrink: 0; }
.sidebar-logout span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.main {
  flex: 1 1 auto; min-width: 0;
  display: flex; flex-direction: column;
  background: var(--bg); overflow: hidden;
}
.main-body { flex: 1 1 auto; overflow-y: auto; overflow-x: hidden; -webkit-overflow-scrolling: touch; }
.main-inner { max-width: 720px; margin: 0 auto; padding: 20px 20px 40px; }

.main-header {
  display: flex; align-items: center; gap: 10px;
  padding: 16px 20px; max-width: 720px; margin: 0 auto; width: 100%;
}
.main-header.wide { max-width: 100%; }
.main-header .title {
  flex: 1; font-size: 22px; font-weight: 800;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.main-header .icon-btn { margin-left: auto; }
.main-header .icon-btn + .icon-btn { margin-left: 0; }

.icon-btn {
  width: 40px; height: 40px;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--card); border: none; color: var(--text);
  cursor: pointer; padding: 0; text-decoration: none;
  border-radius: 12px; transition: background .12s, color .12s; flex-shrink: 0;
}
.icon-btn:hover { background: var(--hover); }
.icon-btn.danger:hover { color: var(--danger); }

.pill-tabs {
  display: flex; gap: 6px; padding: 4px;
  background: var(--card); border-radius: 16px; margin-bottom: 16px;
}
.pill-tab {
  flex: 1; padding: 10px 16px;
  background: transparent; border: none;
  color: var(--muted); font-family: inherit;
  font-size: 14px; font-weight: 600;
  cursor: pointer; border-radius: 12px;
  transition: background .12s, color .12s; white-space: nowrap;
}
.pill-tab:hover { color: var(--text); }
.pill-tab.active { background: var(--accent); color: var(--accent-fg); }

.search-box {
  display: flex; align-items: center; gap: 12px;
  background: var(--card); border-radius: 16px;
  padding: 12px 18px; margin-bottom: 16px;
}
.search-box svg { color: var(--muted); flex-shrink: 0; }
.search-box input {
  flex: 1; background: transparent; border: none; outline: none;
  color: var(--text); font-size: 15px; font-family: inherit;
}
.search-box input::placeholder { color: var(--muted); }

.card { background: var(--card); border-radius: 20px; padding: 18px; margin-bottom: 14px; }

.composer-avatar-row { display: flex; gap: 14px; align-items: flex-start; }
.composer-avatar-col {
  display: flex; flex-direction: column; gap: 8px;
  align-items: center; flex-shrink: 0;
}
.composer-body { flex: 1; min-width: 0; }
.composer-body textarea {
  display: block; width: 100%; background: transparent; border: none;
  outline: none; resize: none; color: var(--text);
  font-family: inherit; font-size: 16px; line-height: 1.55;
  min-height: 56px; max-height: 500px; padding: 6px 0 0; overflow: hidden;
}
.composer-body textarea::placeholder { color: var(--muted); }
.composer-actions {
  display: flex; align-items: center; gap: 10px;
  margin-top: 12px; flex-wrap: wrap;
}
.composer-hint { font-size: 11px; color: var(--muted); }

.og-toggle {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 13px; color: var(--muted);
  cursor: pointer; user-select: none;
}
.og-toggle input { position: absolute; opacity: 0; pointer-events: none; }
.og-toggle .cb {
  width: 20px; height: 20px; flex-shrink: 0;
  border: 2px solid var(--line-2);
  border-radius: 6px;
  background: transparent;
  display: inline-flex; align-items: center; justify-content: center;
  transition: background .12s, border-color .12s;
  color: transparent;
}
.og-toggle input:checked + .cb {
  background: var(--accent); border-color: var(--accent); color: var(--accent-fg);
}
.og-toggle .cb svg { display: block; }
.og-toggle input:focus-visible + .cb { box-shadow: 0 0 0 3px var(--accent-soft); }

.composer-actions .spacer { flex: 1; }
.publish-btn {
  height: 42px; padding: 0 22px;
  background: var(--accent); color: var(--accent-fg);
  border: none; border-radius: 12px;
  font-family: inherit; font-size: 15px; font-weight: 700;
  cursor: pointer; transition: opacity .12s;
}
.publish-btn:hover { opacity: .88; }
.publish-btn:disabled { opacity: .35; cursor: default; }
.publish-btn-mobile { display: none !important; }

.quote-preview {
  margin-top: 10px; padding: 12px 14px;
  background: var(--card-2); border-radius: 14px;
  border-left: 3px solid var(--accent); position: relative;
}
.quote-preview .qp-author { font-size: 13px; font-weight: 700; color: var(--text); margin-bottom: 4px; }
.quote-preview .qp-text {
  font-size: 14px; color: var(--muted);
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
  max-height: 100px; overflow: hidden;
}
.quote-preview .qp-close {
  position: absolute; top: 8px; right: 8px;
  width: 28px; height: 28px; background: transparent; border: none;
  color: var(--muted); cursor: pointer; border-radius: 8px;
}
.quote-preview .qp-close:hover { color: var(--danger); background: var(--hover); }

.avatar {
  width: 44px; height: 44px; flex-shrink: 0; border-radius: 50%;
  background: var(--card-2);
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 24px; line-height: 1; user-select: none; overflow: hidden; text-align: center;
}
.avatar.sm { width: 36px; height: 36px; font-size: 20px; }
.avatar.lg { width: 72px; height: 72px; font-size: 40px; }

.profile-hero {
  background: var(--card); border-radius: 20px; padding: 16px; margin-bottom: 12px;
}
.profile-hero-row { display: flex; gap: 14px; align-items: flex-start; margin-bottom: 10px; }
.profile-hero-avatar { flex-shrink: 0; }
.profile-hero-info { flex: 1; min-width: 0; }
.profile-name {
  font-size: 20px; font-weight: 800; margin: 0 0 2px; line-height: 1.2;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.profile-line {
  display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap;
  margin: 0 0 6px; line-height: 1.3;
}
.profile-line.only-nick { margin-bottom: 4px; }
.profile-nick { font-size: 14px; color: var(--muted); flex-shrink: 0; line-height: 1.3; }
.profile-bio-inline {
  font-size: 14px; line-height: 1.35; color: var(--text); min-width: 0;
  word-wrap: break-word; overflow-wrap: anywhere;
}
.profile-stats {
  display: flex; gap: 16px; font-size: 13px;
  color: var(--muted); margin: 0; line-height: 1.2;
}
.profile-stats b { color: var(--text); font-weight: 700; cursor: pointer; margin-right: 4px; }
.profile-stats b:hover { text-decoration: underline; }
.profile-meta {
  display: flex; align-items: center; gap: 6px;
  color: var(--muted); font-size: 12px; margin-top: 4px; line-height: 1.2;
}
.profile-hero-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.pill-action {
  height: 38px; padding: 0 18px;
  background: var(--card-2); color: var(--text);
  border: none; border-radius: 12px;
  font-family: inherit; font-size: 14px; font-weight: 700;
  cursor: pointer; display: inline-flex; align-items: center; gap: 8px; justify-content: center;
  text-decoration: none; transition: background .12s;
}
.pill-action:hover { background: var(--hover); }
.pill-action.primary { background: var(--accent); color: var(--accent-fg); }
.pill-action.primary:hover { opacity: .88; }
.round-action {
  width: 38px; height: 38px;
  background: var(--card-2); border: none; color: var(--text);
  border-radius: 12px; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  text-decoration: none; transition: background .12s;
}
.round-action:hover { background: var(--hover); }

.post-card { background: var(--card); border-radius: 20px; padding: 18px; margin-bottom: 14px; }
.post-header {
  display: flex; align-items: flex-start; gap: 12px; margin-bottom: 12px;
}
.post-header .meta { flex: 1; min-width: 0; }
.post-header .who {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 14px;
}
.post-author { font-weight: 700; color: var(--text); text-decoration: none; }
.post-author:hover { text-decoration: underline; }
.post-time { color: var(--muted); font-size: 13px; }
.device-badge {
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--muted); flex-shrink: 0; line-height: 0;
}
.device-badge svg { display: block; }

.post-menu {
  display: flex; gap: 2px; margin-left: auto; flex-shrink: 0; align-self: flex-start;
}
.post-menu .act-btn { height: 30px; width: 30px; padding: 0; justify-content: center; }

.post-text {
  font-size: 15px; line-height: 1.55;
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
  color: var(--text); margin-bottom: 8px;
}
.mention { color: var(--mention); text-decoration: none; font-weight: 600; }
.mention:hover { text-decoration: underline; }
.ext-link { color: var(--accent); text-decoration: none; word-break: break-all; }
.ext-link:hover { text-decoration: underline; }
.read-more {
  display: inline-block; margin-top: 4px;
  color: var(--muted); text-decoration: none;
  font-size: 14px; font-weight: 500; cursor: pointer;
}
.read-more:hover { color: var(--text); }

.og-card {
  display: block; margin-top: 10px;
  background: var(--card-2); border-radius: 14px;
  text-decoration: none; color: inherit;
  overflow: hidden; transition: background .12s;
  border: 1px solid var(--line);
}
.og-card:hover { background: var(--hover); }
.og-image {
  width: 100%; height: 180px; overflow: hidden;
  background: var(--line); position: relative;
}
.og-image img { width: 100%; height: 100%; object-fit: cover; display: block; }
.og-fallback {
  display: flex; align-items: center; justify-content: center;
  width: 100%; height: 100%; color: var(--muted);
  font-size: 13px; text-align: center; padding: 20px;
}
.og-body { padding: 12px 14px; }
.og-site {
  font-size: 12px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .5px; font-weight: 600; margin-bottom: 4px;
}
.og-title {
  font-size: 15px; font-weight: 700; color: var(--text); margin-bottom: 4px;
  word-wrap: break-word; overflow-wrap: anywhere;
}
.og-desc {
  font-size: 13px; color: var(--muted); line-height: 1.5;
  word-wrap: break-word; overflow-wrap: anywhere;
}

.quoted-post {
  margin-top: 10px; padding: 12px 14px;
  background: var(--card-2); border-radius: 14px;
  border-left: 3px solid var(--accent);
  cursor: pointer; transition: background .12s;
}
.quoted-post:hover { background: var(--hover); }
.quoted-post .q-author { font-size: 13px; font-weight: 700; color: var(--text); margin-bottom: 4px; }
.quoted-post .q-author a { color: var(--text); text-decoration: none; }
.quoted-post .q-author a:hover { text-decoration: underline; }
.quoted-post .q-text {
  font-size: 14px; color: var(--muted); line-height: 1.5;
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
  max-height: 120px; overflow: hidden;
}

.post-actions {
  display: flex; align-items: center; gap: 4px; margin-top: 14px; flex-wrap: wrap;
}
.act-btn {
  display: inline-flex; align-items: center; gap: 6px;
  height: 36px; padding: 0 10px;
  background: transparent; border: none;
  color: var(--muted); cursor: pointer;
  font-family: inherit; font-size: 13px; font-weight: 600;
  border-radius: 10px;
  transition: background .12s, color .12s;
}
.act-btn:hover { background: var(--hover); color: var(--text); }
.act-btn svg { display: block; }
.act-btn.danger:hover { color: var(--danger); }
.like-btn { display: inline-flex; align-items: center; gap: 6px; }
.like-btn:hover { color: var(--like); }
.like-btn.active { color: var(--like); }
.like-btn .num { font-variant-numeric: tabular-nums; }
.views-badge {
  margin-left: auto; color: var(--muted); font-size: 13px;
  display: inline-flex; align-items: center; gap: 5px;
}

.not-found {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 80px 20px; gap: 20px;
  background: var(--card); border-radius: 20px; min-height: 320px;
}
.not-found-code { font-size: 88px; font-weight: 900; line-height: 1; color: var(--line-2); letter-spacing: -2px; }
.not-found-text { font-size: 15px; color: var(--muted); text-align: center; }

.user-row {
  display: flex; align-items: center; gap: 14px;
  background: var(--card); padding: 14px 16px;
  border-radius: 16px; margin-bottom: 8px;
  cursor: pointer; transition: background .12s;
}
.user-row:hover { background: var(--hover); }
.user-row .info { flex: 1; min-width: 0; }
.user-row .nick {
  font-weight: 700; color: var(--text); font-size: 15px;
  display: block; text-decoration: none;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.user-row .name {
  font-size: 13px; color: var(--muted); margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.notif-row {
  display: flex; align-items: flex-start; gap: 12px;
  background: var(--card); padding: 14px 16px;
  border-radius: 16px; margin-bottom: 8px;
  text-decoration: none; color: inherit; transition: background .12s;
  border-left: 3px solid transparent;
}
.notif-row.unread { background: var(--card-2); border-left-color: var(--accent); }
.notif-row:hover { background: var(--hover); }
.notif-row .info { flex: 1; min-width: 0; }
.notif-row .line { font-size: 14px; line-height: 1.45; }
.notif-row .line b { font-weight: 700; }
.notif-row .snippet {
  margin-top: 6px; padding: 8px 12px;
  background: var(--card-2); border-radius: 10px;
  font-size: 13px; color: var(--muted);
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
}
.notif-row .time { font-size: 12px; color: var(--muted); margin-top: 6px; }

.settings-layout {
  display: flex; gap: 20px;
  max-width: 100%; margin: 0 auto; width: 100%;
  padding: 20px; min-height: 100%;
}
.settings-nav { flex: 0 0 200px; display: flex; flex-direction: column; gap: 4px; }
.settings-nav-btn {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px; text-align: left;
  background: transparent; border: none; color: var(--text);
  cursor: pointer; border-radius: 12px;
  font: inherit; font-size: 15px; font-weight: 600;
  transition: background .12s;
}
.settings-nav-btn:hover { background: var(--hover); }
.settings-nav-btn.active { background: var(--card); color: var(--text); }
.settings-nav-btn.active svg { color: var(--accent); }
.settings-nav-btn svg { color: var(--muted); flex-shrink: 0; }
.settings-content { flex: 1; min-width: 0; }
.settings-block {
  background: var(--card); border-radius: 20px; padding: 20px; margin-bottom: 16px;
}
.settings-block h2 { font-size: 15px; font-weight: 800; margin: 0 0 16px; color: var(--text); }
.settings-subhead {
  font-size: 12px; font-weight: 800; letter-spacing: .5px;
  text-transform: uppercase; color: var(--muted); margin: 18px 0 4px;
}
.settings-subhead:first-child { margin-top: 0; }
.opt-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
.opt {
  height: 40px; padding: 0 20px;
  background: var(--card-2); color: var(--text);
  border: none; border-radius: 12px;
  font-family: inherit; font-size: 14px; font-weight: 600;
  cursor: pointer; transition: background .12s;
}
.opt:hover { background: var(--hover); }
.opt.active { background: var(--accent); color: var(--accent-fg); }

.toggle-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 0; font-size: 15px; gap: 16px;
}
.toggle-row + .toggle-row { border-top: 1px solid var(--line); }
.toggle {
  position: relative; width: 46px; height: 26px;
  background: var(--line-2); cursor: pointer;
  border: none; flex-shrink: 0; border-radius: 13px;
  transition: background .18s;
}
.toggle::after {
  content: ''; position: absolute; left: 2px; top: 2px;
  width: 22px; height: 22px; background: #fff;
  transition: transform .18s; border-radius: 50%;
}
.toggle.on { background: var(--toggle-on); }
.toggle.on::after { transform: translateX(20px); }

.settings-desc { font-size: 13px; line-height: 1.55; color: var(--muted); margin: 0 0 14px; }
.settings-link { display: inline-block; color: var(--accent); text-decoration: underline; font-size: 14px; font-weight: 500; }

.device-info { display: flex; flex-direction: column; gap: 8px; }
.device-info-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; background: var(--card-2);
  border-radius: 12px; font-size: 14px;
}
.device-info-row .label { color: var(--muted); font-weight: 500; }
.device-info-row .value { color: var(--text); font-weight: 700; }

.color-swatches { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
.color-swatch {
  width: 36px; height: 36px; border-radius: 50%;
  border: 2px solid transparent; cursor: pointer;
  padding: 0; position: relative;
  transition: transform .12s, border-color .12s;
}
.color-swatch:hover { transform: scale(1.08); }
.color-swatch.active { border-color: var(--text); }

.emoji-current {
  display: flex; align-items: center; gap: 14px;
  padding: 14px; background: var(--card-2);
  border-radius: 14px; margin-bottom: 14px;
}
.emoji-current .label { font-size: 13px; color: var(--muted); }
.emoji-current .preview { font-size: 40px; line-height: 1; }
.emoji-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(46px, 1fr));
  gap: 6px; max-height: 320px; overflow-y: auto;
  padding: 4px; background: var(--card-2); border-radius: 14px;
}
.emoji-opt {
  aspect-ratio: 1 / 1; background: transparent;
  border: 2px solid transparent; border-radius: 10px;
  font-size: 24px; line-height: 1; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: background .12s, border-color .12s; padding: 0;
}
.emoji-opt:hover { background: var(--hover); }
.emoji-opt.active { background: var(--accent-soft); border-color: var(--accent); }

.auth-page { min-height: 100%; display: flex; align-items: center; justify-content: center; padding: 40px 20px; }
.auth-card { background: var(--card); border-radius: 24px; padding: 32px; max-width: 400px; width: 100%; }
.auth-card h1 { font-size: 26px; font-weight: 800; margin: 0 0 24px; }
.auth-card form { display: flex; flex-direction: column; gap: 10px; }
.auth-card input {
  padding: 15px 18px; background: var(--card-2); border: none;
  color: var(--text); font-family: inherit; font-size: 15px;
  outline: none; border-radius: 14px;
}
.auth-card input::placeholder { color: var(--muted); }
.auth-card button[type="submit"] {
  margin-top: 8px; height: 52px;
  background: var(--accent); color: var(--accent-fg);
  border: none; border-radius: 14px;
  font-family: inherit; font-size: 16px; font-weight: 700;
  cursor: pointer;
}
.auth-card button[type="submit"]:disabled { opacity: .5; cursor: default; }
.auth-error { color: var(--danger); font-size: 14px; min-height: 20px; }
.auth-switch { margin-top: 18px; font-size: 14px; color: var(--muted); text-align: center; }
.auth-switch a { color: var(--text); cursor: pointer; text-decoration: underline; font-weight: 600; }

.policy { max-width: 720px; margin: 0 auto; padding: 20px; }
.policy h1 { font-size: 24px; margin: 0 0 20px; font-weight: 800; }
.policy p { font-size: 15px; line-height: 1.7; color: var(--text); white-space: pre-wrap; margin: 0; }

.empty {
  padding: 60px 20px; text-align: center;
  color: var(--muted); font-size: 14px;
  background: var(--card); border-radius: 20px;
}
.spinner-wrap { padding: 60px 0; text-align: center; }
.spinner {
  display: inline-block; width: 28px; height: 28px;
  border: 3px solid var(--line-2); border-top-color: var(--accent);
  animation: spin .7s linear infinite; border-radius: 50%;
}
@keyframes spin { to { transform: rotate(360deg); } }

.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.6);
  backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000; padding: 20px;
  animation: fadeIn .12s ease;
}
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
.modal { background: var(--card); border-radius: 22px; padding: 26px; max-width: 400px; width: 100%; }
.modal-text { font-size: 16px; line-height: 1.5; color: var(--text); margin-bottom: 22px; text-align: center; }
.modal-actions { display: flex; gap: 8px; }
.modal-btn {
  flex: 1; height: 48px;
  font-family: inherit; font-size: 15px; font-weight: 700;
  cursor: pointer; border: none; border-radius: 14px;
}
.modal-btn.secondary { background: var(--card-2); color: var(--text); }
.modal-btn.danger { background: var(--danger); color: #fff; }

.comment.highlight { animation: flash 1.6s ease-out; }
@keyframes flash {
  0% { background: var(--like); color: #fff; }
  100% { background: var(--card-2); }
}
.comment.pending { opacity: .6; }

.comments { margin-top: 16px; padding-top: 4px; }
.comment { padding: 12px 14px; background: var(--card-2); border-radius: 14px; margin-top: 8px; }
.comment.reply { margin-left: 28px; background: transparent; }
.comment.is-author { box-shadow: 0 0 0 2px var(--line-2) inset; }
.comment-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.comment-author { font-size: 13px; font-weight: 700; color: var(--text); text-decoration: none; }
.comment-author:hover { text-decoration: underline; }
.comment-author-badge {
  font-size: 10px; font-weight: 800; text-transform: uppercase;
  letter-spacing: .5px; padding: 2px 8px; border-radius: 6px;
  background: var(--accent); color: var(--accent-fg);
}
.comment-time { font-size: 12px; color: var(--muted); margin-left: auto; }
.comment-text {
  font-size: 14px; line-height: 1.5;
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
  color: var(--text); margin-bottom: 6px;
}
.comment-actions { display: flex; align-items: center; gap: 2px; flex-wrap: wrap; }
.comment-actions .act-btn { height: 30px; padding: 0 10px; font-size: 12px; }

.inline-editor { margin-top: 8px; }
.inline-editor textarea {
  width: 100%; padding: 12px 14px; min-height: 90px;
  border: none; background: var(--card-2); color: var(--text);
  font-family: inherit; font-size: 15px; line-height: 1.5;
  outline: none; resize: none; border-radius: 14px;
}
.inline-editor .edit-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
.inline-editor .edit-actions button {
  height: 38px; padding: 0 16px; font-family: inherit;
  font-size: 14px; font-weight: 700; cursor: pointer;
  border: none; border-radius: 12px;
  background: var(--card-2); color: var(--text);
}
.inline-editor .edit-actions button.edit-save { background: var(--accent); color: var(--accent-fg); }

.edit-bio-textarea {
  padding: 15px 18px; background: var(--card-2); border: none;
  color: var(--text); font-family: inherit; font-size: 15px;
  outline: none; border-radius: 14px;
  min-height: 60px; max-height: 200px; resize: none; line-height: 1.4;
}

/* TABLET */
@media (max-width: 1100px) and (min-width: 901px) {
  .layout { max-width: 100%; }
  .sidebar { flex: 0 0 76px; width: 76px; padding: 16px 8px; }
  .sidebar .logo { font-size: 16px; padding: 4px 6px 18px; text-align: center; letter-spacing: 1px; }
  .nav-btn { flex-direction: column; gap: 4px; padding: 10px 4px; font-size: 11px;
    justify-content: center; align-items: center; text-align: center; }
  .nav-btn span { font-size: 11px; line-height: 1; word-break: break-word; }
  .nav-btn .badge { position: absolute; top: 2px; right: 6px;
    min-width: 18px; height: 18px; line-height: 18px; font-size: 10px; padding: 0 5px; }
  .sidebar-logout {
    flex-direction: column; gap: 4px;
    padding: 10px 4px; font-size: 11px;
    justify-content: center; align-items: center;
    text-align: center; border-radius: 12px;
  }
  .sidebar-logout span { font-size: 11px; line-height: 1; word-break: break-word; white-space: normal; }
  .sidebar-logout svg { width: 22px; height: 22px; }
}

/* MOBILE */
@media (max-width: 900px) {
  .layout { flex-direction: column; max-width: 100%; }
  .main {
    order: 1;
    height: calc(100vh - 68px - env(safe-area-inset-bottom, 0px));
    height: calc(100dvh - 68px - env(safe-area-inset-bottom, 0px));
  }
  .sidebar {
    order: 2; width: 100%;
    height: calc(68px + env(safe-area-inset-bottom, 0px));
    flex-direction: row; border-top: 1px solid var(--line);
    padding: 0 0 env(safe-area-inset-bottom, 0px);
    flex: 0 0 auto; background: var(--card);
  }
  .sidebar .logo { display: none; }
  .sidebar .spacer { display: none; }
  .sidebar-logout { display: none; }
  .nav { flex-direction: row; flex: 1; justify-content: space-around; align-items: stretch; }
  .nav-btn {
    flex-direction: column; gap: 2px; padding: 8px 2px;
    flex: 1; justify-content: center; align-items: center;
    text-align: center; border-radius: 0; min-height: 66px;
  }
  .nav-btn span { font-size: 10px; line-height: 1; font-weight: 500; }
  .nav-btn svg { width: 22px; height: 22px; }
  .nav-btn.active { background: transparent; }
  .nav-btn.active svg, .nav-btn.active span { color: var(--text); }
  .nav-btn .badge {
    position: absolute; top: 2px; right: 16%;
    min-width: 16px; height: 16px; line-height: 16px;
    font-size: 10px; padding: 0 4px; border-radius: 8px;
  }
  .main-header { padding: 14px 16px; }
  .main-header .title { font-size: 20px; }
  .main-inner { padding: 16px 12px 40px; }
  .card, .post-card, .profile-hero { border-radius: 18px; padding: 14px; }
  .profile-name { font-size: 18px; }
  .avatar.lg { width: 60px; height: 60px; font-size: 34px; }
  .profile-hero { padding: 14px; }
  .profile-hero-row { margin-bottom: 8px; gap: 12px; }
  .profile-line { gap: 6px; margin-bottom: 4px; }
  .profile-line.only-nick { margin-bottom: 2px; }
  .profile-stats { font-size: 12px; gap: 12px; }
  .profile-meta { margin-top: 2px; font-size: 11px; }

  /* Публикация поста на телефоне: кнопка под аватаркой, только иконка */
  .composer-avatar-row { gap: 10px; }
  .composer-avatar-col { gap: 6px; align-items: center; }
  .composer-body textarea { min-height: 44px; font-size: 15px; }
  .publish-btn-mobile {
    display: inline-flex !important;
    align-items: center; justify-content: center;
    width: 40px; height: 40px; padding: 0;
    border-radius: 12px;
  }
  .publish-btn-mobile svg { width: 18px; height: 18px; }
  .publish-btn-desktop { display: none !important; }
  .composer-hint { display: none; }

  .settings-layout { flex-direction: column; padding: 12px; }
  .settings-nav {
    flex: 0 0 auto; flex-direction: row;
    overflow-x: auto; scrollbar-width: none; margin-bottom: 8px;
  }
  .settings-nav-btn {
    flex-shrink: 0; padding: 10px 16px;
    background: var(--card); border-radius: 12px; font-size: 13px;
  }
  .settings-nav-btn.active { background: var(--accent); color: var(--accent-fg); }
  .settings-nav-btn.active svg { color: var(--accent-fg); }
  .settings-block { padding: 16px; border-radius: 16px; }

  .modal { padding: 22px 18px 18px; border-radius: 18px; }
  .modal-text { font-size: 16px; margin-bottom: 20px; }
  .modal-actions { flex-direction: column-reverse; gap: 10px; }
  .modal-btn { height: 56px; font-size: 16px; border-radius: 14px; flex: 0 0 auto; width: 100%; }

  .emoji-grid { grid-template-columns: repeat(auto-fill, minmax(42px, 1fr)); gap: 5px; max-height: 280px; }
  .emoji-opt { font-size: 22px; }
  .og-image { height: 140px; }

  .not-found { padding: 60px 16px; min-height: 260px; }
  .not-found-code { font-size: 72px; }
}
@media (max-width: 500px) {
  .main-inner { padding: 12px 10px 30px; }
  .card, .post-card, .profile-hero { border-radius: 16px; padding: 12px; }
  .profile-stats { font-size: 12px; gap: 10px; }
  .og-image { height: 120px; }
  .settings-layout { padding: 10px; }
}
"""


JS = r"""
var ICONS = {
  home: __I_HOME__, users: __I_USERS__, user: __I_USER__,
  bell: __I_BELL__, gear: __I_GEAR__,
  logout: __I_LOGOUT__, login: __I_LOGIN__, plus: __I_PLUS__,
  send: __I_SEND__,
  heart: __I_HEART__, heart_filled: __I_HEART_FILLED__, comment: __I_COMMENT__,
  copy: __I_COPY__, edit: __I_EDIT__, trash: __I_TRASH__,
  back: __I_BACK__, search: __I_SEARCH__,
  moon: __I_MOON__, sun: __I_SUN__, quote: __I_QUOTE__,
  eye: __I_EYE__, cal: __I_CAL__, check: __I_CHECK__,
  mobile: __I_MOBILE__, desktop: __I_DESKTOP__
};

var EMOJIS = __EMOJIS__;
var DEFAULT_EMOJI = '🐱';

var COLOR_PRESETS = {
  accent: [
    { id: 'blue',    ru: 'Синий',     en: 'Blue',     dark: '#3b82f6', light: '#2563eb' },
    { id: 'green',   ru: 'Зелёный',   en: 'Green',    dark: '#22c55e', light: '#16a34a' },
    { id: 'purple',  ru: 'Фиолетовый',en: 'Purple',   dark: '#a855f7', light: '#9333ea' },
    { id: 'pink',    ru: 'Розовый',   en: 'Pink',     dark: '#ec4899', light: '#db2777' },
    { id: 'orange',  ru: 'Оранжевый', en: 'Orange',   dark: '#f97316', light: '#ea580c' },
    { id: 'teal',    ru: 'Бирюзовый', en: 'Teal',     dark: '#14b8a6', light: '#0d9488' },
    { id: 'indigo',  ru: 'Индиго',    en: 'Indigo',   dark: '#6366f1', light: '#4f46e5' },
  ],
  like: [
    { id: 'red',    ru: 'Красный',   en: 'Red',     dark: '#ef4444', light: '#dc2626' },
    { id: 'pink',   ru: 'Розовый',   en: 'Pink',    dark: '#ec4899', light: '#db2777' },
    { id: 'orange', ru: 'Оранжевый', en: 'Orange',  dark: '#f97316', light: '#ea580c' },
    { id: 'purple', ru: 'Фиолетовый',en: 'Purple',  dark: '#a855f7', light: '#9333ea' },
    { id: 'blue',   ru: 'Синий',     en: 'Blue',    dark: '#3b82f6', light: '#2563eb' },
  ]
};

function loadColors() {
  try { return JSON.parse(localStorage.getItem('SLD_colors') || '{}') || {}; }
  catch(e) { return {}; }
}
function saveColors(c) { localStorage.setItem('SLD_colors', JSON.stringify(c)); }
function applyColors() {
  var c = loadColors();
  var theme = document.documentElement.getAttribute('data-theme') || 'dark';
  var root = document.documentElement.style;
  if (c.accent) {
    var p = COLOR_PRESETS.accent.find(function(x){ return x.id === c.accent; });
    if (p) {
      var val = theme === 'dark' ? p.dark : p.light;
      root.setProperty('--accent', val);
      root.setProperty('--toggle-on', val);
      root.setProperty('--accent-fg', '#ffffff');
      root.setProperty('--accent-soft', val + '22');
    }
  } else {
    root.removeProperty('--accent');
    root.removeProperty('--toggle-on');
    root.removeProperty('--accent-fg');
    root.removeProperty('--accent-soft');
  }
  if (c.like) {
    var pl = COLOR_PRESETS.like.find(function(x){ return x.id === c.like; });
    if (pl) root.setProperty('--like', theme === 'dark' ? pl.dark : pl.light);
  } else {
    root.removeProperty('--like');
  }
}
function setColor(key, id) {
  var c = loadColors(); c[key] = id; saveColors(c); applyColors();
}

var cachedUser = null;
try { cachedUser = JSON.parse(localStorage.getItem('SLD_user') || 'null'); } catch(e) { cachedUser = null; }

var anonId = localStorage.getItem('SLD_anon_id');
if (!anonId) {
  anonId = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  localStorage.setItem('SLD_anon_id', anonId);
}

var state = {
  user: cachedUser,
  token: localStorage.getItem('SLD_token') || null,
  view: VIEW, viewData: VIEW_DATA || {},
  unreadNotif: parseInt(localStorage.getItem('SLD_un') || '0', 10) || 0,
  feedMode: 'all',
  peopleTab: 'all',
  peopleQuery: '',
  searchQuery: '',
  composerDraft: '',
  quotePostId: null,
  quotePreview: null,
  replyTo: null,
  highlightComment: null,
  suppressRefresh: 0,
  settingsSection: 'account',
  es: null,
  currentEmoji: DEFAULT_EMOJI,
  whoami: null,
  currentRooms: [],
  refreshTimer: null
};

function setUser(u) {
  state.user = u;
  if (u) localStorage.setItem('SLD_user', JSON.stringify(u));
  else localStorage.removeItem('SLD_user');
}
function setNotifCount(n) {
  if (typeof n === 'number') { state.unreadNotif = n; localStorage.setItem('SLD_un', String(n)); }
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  if (state.token) opts.headers['X-Auth'] = state.token;
  if (opts.body && typeof opts.body === 'object') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  var r = await fetch(path, opts);
  if (r.status === 401) {
    setUser(null); state.token = null;
    localStorage.removeItem('SLD_token');
    disconnectSSE();
    throw new Error('unauthorized');
  }
  if (!r.ok) {
    var err = {};
    try { err = await r.json(); } catch(e) {}
    throw new Error(err.detail || 'error');
  }
  return r.json();
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function linkifyMentions(escaped) {
  return escaped.replace(/(^|[^a-zA-Z0-9_])@([a-zA-Z0-9_]{3,20})/g, function(_, pre, nick){
    return pre + '<a class="mention" href="/u/' + encodeURIComponent(nick) + '" data-link>@' + nick + '</a>';
  });
}
function linkifyText(raw) {
  if (!raw) return '';
  var parts = [];
  var re = /(https?:\/\/[^\s<>"']+)/g;
  var last = 0, m;
  while ((m = re.exec(raw)) !== null) {
    if (m.index > last) parts.push({ type: 'text', v: raw.slice(last, m.index) });
    parts.push({ type: 'url', v: m[0] });
    last = m.index + m[0].length;
  }
  if (last < raw.length) parts.push({ type: 'text', v: raw.slice(last) });
  return parts.map(function(p){
    if (p.type === 'url') {
      var safe = escapeHtml(p.v);
      return '<a class="ext-link" href="' + safe + '" target="_blank" rel="noopener noreferrer">' + safe + '</a>';
    }
    return linkifyMentions(escapeHtml(p.v));
  }).join('');
}
function tr(k) { return T[k] || k; }
function timeAgo(ts) {
  var d = Math.floor(Date.now()/1000 - ts);
  if (d < 5) return tr('just_now');
  if (d < 60) return d + ' ' + tr('sec_ago');
  var m = Math.floor(d/60);
  if (m < 60) return m + ' ' + tr('min_ago');
  var h = Math.floor(m/60);
  if (h < 24) return h + ' ' + tr('hour_ago');
  var days = Math.floor(h/24);
  if (days < 30) return days + ' ' + tr('day_ago');
  return new Date(ts*1000).toLocaleDateString();
}
function fmtDate(ts) {
  try { return new Date(ts*1000).toLocaleDateString(LANG === 'ru' ? 'ru-RU' : 'en-US', { year: 'numeric', month: 'long' }); }
  catch(e) { return new Date(ts*1000).toLocaleDateString(); }
}
function truncateText(text) {
  var lines = text.split('\n');
  var out = text, truncated = false;
  if (lines.length > TRUNCATE_LINES) { out = lines.slice(0, TRUNCATE_LINES).join('\n'); truncated = true; }
  if (out.length > TRUNCATE_CHARS) { out = out.slice(0, TRUNCATE_CHARS); truncated = true; }
  if (truncated) out = out.replace(/\s+$/, '') + '…';
  return { text: out, truncated: truncated };
}
function spinner() { return '<div class="spinner-wrap"><div class="spinner"></div></div>'; }

function notFoundHtml() {
  return '<div class="not-found">'
    + '<div class="not-found-code">404</div>'
    + '<div class="not-found-text">' + escapeHtml(tr('page_not_found')) + '</div>'
    + '<a class="pill-action primary" href="/" data-link>' + escapeHtml(tr('back_home')) + '</a>'
    + '</div>';
}
function autoGrow(el) {
  if (!el) return;
  el.style.height = 'auto';
  var maxH = 500;
  var h = Math.min(el.scrollHeight, maxH);
  el.style.height = h + 'px';
  el.style.overflowY = (el.scrollHeight > maxH) ? 'auto' : 'hidden';
}
function showConfirm(text, onConfirm, opts) {
  opts = opts || {};
  var yesText = opts.yesText || tr('confirm_yes');
  var noText = opts.noText || tr('confirm_no');
  var modal = document.createElement('div');
  modal.className = 'modal-overlay';
  modal.innerHTML = '<div class="modal"><div class="modal-text">' + escapeHtml(text) + '</div>'
    + '<div class="modal-actions">'
    + '<button class="modal-btn secondary" data-modal-cancel>' + escapeHtml(noText) + '</button>'
    + '<button class="modal-btn danger" data-modal-confirm>' + escapeHtml(yesText) + '</button>'
    + '</div></div>';
  document.body.appendChild(modal);
  function close() { if (modal.parentNode) modal.parentNode.removeChild(modal); }
  modal.querySelector('[data-modal-cancel]').addEventListener('click', close);
  modal.querySelector('[data-modal-confirm]').addEventListener('click', function(){ close(); onConfirm(); });
  modal.addEventListener('click', function(e){ if (e.target === modal) close(); });
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('SLD_theme', theme);
  applyColors();
}
function toggleTheme() {
  var cur = document.documentElement.getAttribute('data-theme') || 'dark';
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}
applyTheme(localStorage.getItem('SLD_theme') || 'dark');
applyColors();

function themeIconHtml() {
  var theme = document.documentElement.getAttribute('data-theme') || 'dark';
  return theme === 'dark' ? ICONS.sun : ICONS.moon;
}
function bindThemeBtn() {
  var b = document.getElementById('mainThemeBtn');
  if (!b) return;
  b.innerHTML = themeIconHtml();
  b.addEventListener('click', function(){
    toggleTheme();
    document.querySelectorAll('#mainThemeBtn').forEach(function(x){ x.innerHTML = themeIconHtml(); });
  });
}

function navigate(url, force) {
  if (!force && location.pathname + location.hash === url) return;
  history.pushState({}, '', url);
  handleRoute();
}
function computeRooms() {
  var rooms = [];
  if (state.view === 'feed') rooms.push('feed');
  else if (state.view === 'post') rooms.push('post:' + (state.viewData.post_id || ''));
  else if (state.view === 'profile') rooms.push('profile:' + (state.viewData.nick || ''));
  return rooms;
}
function roomsEqual(a, b) {
  if (!a || !b) return false;
  if (a.length !== b.length) return false;
  return a.slice().sort().join('|') === b.slice().sort().join('|');
}
function updateRoom() {
  var rooms = computeRooms();
  if (roomsEqual(rooms, state.currentRooms)) return;
  state.currentRooms = rooms;
  var body = { rooms: rooms };
  if (!state.token) body.anon_id = anonId;
  api('/api/room', { method: 'POST', body: body }).catch(function(){});
}

function handleRoute() {
  var path = location.pathname, m;
  if (path === '/' || path === '') { state.view = 'feed'; state.viewData = {}; }
  else if ((m = path.match(/^\/p\/([a-z0-9]+)$/))) {
    state.view = 'post'; state.viewData = { post_id: m[1] };
    state.highlightComment = (location.hash && location.hash.match(/^#c-(.+)$/)) ? location.hash.slice(3) : null;
  }
  else if ((m = path.match(/^\/u\/([^\/]+)\/followers$/))) {
    state.view = 'followers'; state.viewData = { nick: decodeURIComponent(m[1]) };
  }
  else if ((m = path.match(/^\/u\/([^\/]+)\/following$/))) {
    state.view = 'following'; state.viewData = { nick: decodeURIComponent(m[1]) };
  }
  else if ((m = path.match(/^\/u\/(.+)$/))) {
    state.view = 'profile'; state.viewData = { nick: decodeURIComponent(m[1]) };
  }
  else if (path === '/users') { state.view = 'users'; state.viewData = {}; state.peopleTab = 'all'; }
  else if (path === '/notifications') { state.view = 'notifications'; state.viewData = {}; }
  else if (path === '/settings') { state.view = 'settings'; state.viewData = {}; }
  else if (path === '/settings/profile') { state.view = 'edit_profile'; state.viewData = {}; }
  else if (path === '/policy') { state.view = 'policy'; state.viewData = {}; }
  else if (path === '/register') { state.view = 'register'; state.viewData = {}; }
  else if (path === '/login') { state.view = 'login'; state.viewData = {}; }
  else { state.view = 'not_found'; state.viewData = {}; }

  if (state.view !== 'feed') {
    state.quotePostId = null; state.quotePreview = null;
  }
  renderSidebar(); renderMain();
  updateRoom();
}
window.addEventListener('popstate', handleRoute);

async function loadMe() {
  if (!state.token) return;
  try { var u = await api('/api/me'); setUser(u); }
  catch(e) { setUser(null); }
}
async function doRegister(data) {
  var res = await api('/api/register', { method: 'POST', body: data });
  state.token = res.token; localStorage.setItem('SLD_token', res.token);
  setUser(res.user); connectSSE(); navigate('/', true);
}
async function doLogin(data) {
  var res = await api('/api/login', { method: 'POST', body: data });
  state.token = res.token; localStorage.setItem('SLD_token', res.token);
  setUser(res.user); connectSSE(); navigate('/', true);
}
function doLogoutConfirm() {
  showConfirm(tr('confirm_logout'), async function(){
    try { await api('/api/logout', { method: 'POST' }); } catch(e) {}
    disconnectSSE(); setUser(null); state.token = null;
    setNotifCount(0); localStorage.removeItem('SLD_token');
    navigate('/', true);
    connectSSE();
  }, { yesText: tr('confirm_yes') });
}
function refreshCounters() {
  if (!state.token) return;
  api('/api/counters').then(function(data){
    var changed = (data.notif !== state.unreadNotif);
    setNotifCount(data.notif || 0);
    if (changed) renderSidebar();
  }).catch(function(){});
}
function disconnectSSE() {
  if (state.es) { try { state.es.close(); } catch(e) {} state.es = null; }
  state.currentRooms = [];
}
function connectSSE() {
  disconnectSSE();
  var url = '/api/events';
  if (state.token) url += '?token=' + encodeURIComponent(state.token);
  else url += '?anon=' + encodeURIComponent(anonId);
  var es = new EventSource(url);
  state.es = es;
  es.onmessage = function(e) { try { handleEvent(JSON.parse(e.data)); } catch(err) {} };
  es.onerror = function() {};
  setTimeout(updateRoom, 300);
}
function scheduleRefresh() {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(function(){
    state.refreshTimer = null;
    // Не перебиваем ввод
    var active = document.activeElement;
    if (active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT')) return;
    // Не мешаем оптимистичным апдейтам
    if (state.suppressRefresh && Date.now() < state.suppressRefresh) return;
    refreshCurrentView();
  }, 500);
}
function handleEvent(ev) {
  if (!ev || !ev.type) return;
  if (ev.type === 'hello') return;
  if (ev.type === 'notif_changed') {
    refreshCounters();
    if (state.view === 'notifications') loadNotifications();
    return;
  }
  if (ev.type === 'view_update') {
    document.querySelectorAll('[data-post-id="' + ev.post_id + '"] [data-views]').forEach(function(el){
      el.textContent = ev.views;
    });
    return;
  }
  if (ev.type === 'post_deleted') {
    if (state.view === 'post' && state.viewData.post_id === ev.post_id) {
      var main = document.getElementById('main');
      if (main) main.innerHTML = '<div class="main-body"><div class="main-inner">' + notFoundHtml() + '</div></div>';
      bindLinks(main);
    }
    return;
  }
  if (ev.type === 'refresh') { scheduleRefresh(); return; }
}

function navBtn(icon, label, active, action, count) {
  var cls = 'nav-btn' + (active ? ' active' : '');
  var badge = (count && count > 0) ? '<span class="badge">' + (count > 99 ? '99+' : count) + '</span>' : '';
  return '<button class="' + cls + '" data-nav="' + action + '" title="' + escapeHtml(label) + '">'
    + icon + '<span>' + escapeHtml(label) + '</span>' + badge + '</button>';
}
function renderSidebar() {
  var el = document.getElementById('sidebar');
  if (!el) return;
  var html = '<div class="logo">SLD</div><div class="nav">';
  html += navBtn(ICONS.home, tr('nav_home'), state.view === 'feed', 'home');
  html += navBtn(ICONS.users, tr('nav_users'), state.view === 'users', 'users');
  if (state.user) {
    html += navBtn(ICONS.bell, tr('nav_notifications'),
      state.view === 'notifications', 'notifications', state.unreadNotif);
    html += navBtn(ICONS.user, tr('nav_profile'),
      state.view === 'profile' && state.viewData.nick === state.user.nick, 'profile');
    html += navBtn(ICONS.gear, tr('nav_settings'),
      state.view === 'settings' || state.view === 'edit_profile', 'settings');
    html += '</div><div class="spacer"></div>';
    html += '<button class="sidebar-logout" data-nav="logout">' + ICONS.logout + '<span>' + tr('nav_logout') + '</span></button>';
  } else {
    html += navBtn(ICONS.gear, tr('nav_settings'), state.view === 'settings', 'settings');
    html += navBtn(ICONS.login, tr('nav_login'), state.view === 'login', 'login');
    html += navBtn(ICONS.plus, tr('nav_register'), state.view === 'register', 'register');
    html += '</div><div class="spacer"></div>';
  }
  el.innerHTML = html;
  el.querySelectorAll('[data-nav]').forEach(function(b){
    b.addEventListener('click', function(){
      var nav = b.dataset.nav;
      if (nav === 'home') navigate('/');
      else if (nav === 'users') navigate('/users');
      else if (nav === 'profile') navigate('/u/' + encodeURIComponent(state.user.nick));
      else if (nav === 'notifications') navigate('/notifications');
      else if (nav === 'settings') navigate('/settings');
      else if (nav === 'register') navigate('/register');
      else if (nav === 'login') navigate('/login');
      else if (nav === 'logout') doLogoutConfirm();
    });
  });
}

function renderMain() {
  var el = document.getElementById('main');
  if (state.view === 'not_found') {
    el.innerHTML = '<div class="main-header"><div class="title">404</div>'
      + '<button class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>'
      + '<div class="main-body"><div class="main-inner">' + notFoundHtml() + '</div></div>';
    bindThemeBtn(); bindLinks(el);
    return;
  }
  if (state.view === 'feed') renderFeedView(el);
  else if (state.view === 'post') renderPostView(el);
  else if (state.view === 'profile') renderProfileView(el);
  else if (state.view === 'followers' || state.view === 'following') renderFollowListView(el);
  else if (state.view === 'users') renderUsersView(el);
  else if (state.view === 'notifications') renderNotificationsView(el);
  else if (state.view === 'settings') renderSettingsView(el);
  else if (state.view === 'edit_profile') renderEditProfileView(el);
  else if (state.view === 'policy') renderPolicyView(el);
  else if (state.view === 'register') renderRegisterView(el);
  else if (state.view === 'login') renderLoginView(el);
  else renderFeedView(el);
}
function bindLinks(root) {
  root.querySelectorAll('[data-link]').forEach(function(a){
    if (a.dataset.linkBound) return;
    a.dataset.linkBound = '1';
    a.addEventListener('click', function(e){ e.preventDefault(); e.stopPropagation(); navigate(a.getAttribute('href')); });
  });
}
function bindOgImages(root) {
  root.querySelectorAll('.og-image img').forEach(function(img){
    if (img.dataset.errBound) return;
    img.dataset.errBound = '1';
    img.addEventListener('error', function(){
      if (!img.parentNode) return;
      var fb = document.createElement('div');
      fb.className = 'og-fallback';
      fb.textContent = tr('og_img_fallback');
      img.parentNode.replaceChild(fb, img);
    });
  });
}

function avatarHtml(emoji, size) {
  var cls = 'avatar' + (size ? ' ' + size : '');
  var e = emoji || DEFAULT_EMOJI;
  return '<div class="' + cls + '">' + escapeHtml(e) + '</div>';
}

function composerHtml(opts) {
  opts = opts || {};
  var u = state.user;
  if (!u) return '';
  var placeholder = opts.placeholder || tr('post_ph');
  var sendLabel = opts.sendLabel || tr('publish');
  var idPrefix = opts.idPrefix || 'post';
  var ogOn = (u.show_link_previews !== false);
  return '<div class="card">'
    + '<div class="composer-avatar-row">'
    + '<div class="composer-avatar-col">'
    +   avatarHtml(u.avatar_emoji, 'sm')
    +   '<button type="button" class="publish-btn publish-btn-mobile" id="' + idPrefix + 'SendMobile" disabled title="' + escapeHtml(sendLabel) + '">' + ICONS.send + '</button>'
    + '</div>'
    + '<div class="composer-body">'
    + '<textarea id="' + idPrefix + 'Input" maxlength="' + MAX_POST_LEN + '" placeholder="' + escapeHtml(placeholder) + '">' + escapeHtml(state.composerDraft || '') + '</textarea>'
    + '<div id="' + idPrefix + 'QuoteBox"></div>'
    + '<div class="composer-actions">'
    + '<label class="og-toggle"><input type="checkbox" id="' + idPrefix + 'OgEnabled"' + (ogOn ? ' checked' : '') + '/><span class="cb">' + ICONS.check + '</span><span>' + tr('og_enable') + '</span></label>'
    + '<div class="spacer"></div>'
    + '<span class="composer-hint">Shift+Enter</span>'
    + '<span id="' + idPrefix + 'Counter" style="font-size:12px;color:var(--muted)">0 / ' + MAX_POST_LEN + '</span>'
    + '<button class="publish-btn publish-btn-desktop" id="' + idPrefix + 'Send" disabled>' + escapeHtml(sendLabel) + '</button>'
    + '</div></div></div></div>';
}

function renderQuoteBox(idPrefix) {
  var el = document.getElementById(idPrefix + 'QuoteBox');
  if (!el) return;
  if (!state.quotePreview) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="quote-preview">'
    + '<div class="qp-author">@' + escapeHtml(state.quotePreview.author) + '</div>'
    + '<div class="qp-text">' + escapeHtml(state.quotePreview.text) + '</div>'
    + '<button class="qp-close" type="button">✕</button>'
    + '</div>';
  el.querySelector('.qp-close').addEventListener('click', function(){
    state.quotePostId = null; state.quotePreview = null;
    renderQuoteBox(idPrefix);
  });
}

function bindComposer(opts) {
  opts = opts || {};
  var idPrefix = opts.idPrefix || 'post';
  var onSend = opts.onSend;
  var inputEl = document.getElementById(idPrefix + 'Input');
  var sendBtn = document.getElementById(idPrefix + 'Send');
  var sendBtnMobile = document.getElementById(idPrefix + 'SendMobile');
  var counter = document.getElementById(idPrefix + 'Counter');
  if (!inputEl) return;
  var maxLen = opts.maxLen || MAX_POST_LEN;
  function upd(){
    var len = inputEl.value.length;
    if (counter) counter.textContent = len + ' / ' + maxLen;
    var empty = len === 0 && !state.quotePostId;
    var dis = empty || len > maxLen;
    if (sendBtn) sendBtn.disabled = dis;
    if (sendBtnMobile) sendBtnMobile.disabled = dis;
    state.composerDraft = inputEl.value;
    autoGrow(inputEl);
  }
  inputEl.addEventListener('input', upd);
  inputEl.addEventListener('keydown', function(e){
    if (e.key === 'Enter' && e.shiftKey && !e.isComposing) {
      e.preventDefault();
      trigger();
    }
  });
  async function trigger(){
    var text = inputEl.value.trim();
    if (!text && !state.quotePostId) return;
    var ogCheckbox = document.getElementById(idPrefix + 'OgEnabled');
    var ogEnabled = ogCheckbox ? ogCheckbox.checked : true;
    if (sendBtn) sendBtn.disabled = true;
    if (sendBtnMobile) sendBtnMobile.disabled = true;
    try {
      await onSend(text, state.quotePostId, ogEnabled);
      inputEl.value = '';
      state.composerDraft = '';
      state.quotePostId = null; state.quotePreview = null;
      renderQuoteBox(idPrefix);
      upd();
    } catch(e) { alert(tr(e.message) || e.message); }
    finally { upd(); }
  }
  if (sendBtn) sendBtn.addEventListener('click', trigger);
  if (sendBtnMobile) sendBtnMobile.addEventListener('click', trigger);
  renderQuoteBox(idPrefix);
  upd();
  autoGrow(inputEl);
}

/* ============ FEED ============ */
function renderFeedView(el) {
  var html = '<div class="main-header"><div class="title">' + tr('nav_home') + '</div>'
    + '<button class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner">';
  html += '<div class="search-box">' + ICONS.search + '<input id="search" type="search" placeholder="' + escapeHtml(tr('search_ph')) + '" value="' + escapeHtml(state.searchQuery) + '" /></div>';
  html += '<div class="pill-tabs">';
  html += '<button class="pill-tab' + (state.feedMode==='all'?' active':'') + '" data-mode="all">' + tr('feed_all') + '</button>';
  html += '<button class="pill-tab' + (state.feedMode==='subs'?' active':'') + '" data-mode="subs">' + tr('feed_subs') + '</button>';
  html += '</div>';
  if (state.user) html += composerHtml({ idPrefix: 'post', placeholder: tr('post_ph') });
  else html += '<div class="card" style="text-align:center"><a href="/login" data-link style="color:var(--accent)">' + tr('go_login') + '</a></div>';
  html += '<div id="feed">' + spinner() + '</div>';
  html += '</div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  el.querySelectorAll('.pill-tab').forEach(function(t){
    t.addEventListener('click', function(){
      state.feedMode = t.dataset.mode;
      el.querySelectorAll('.pill-tab').forEach(function(x){ x.classList.toggle('active', x === t); });
      loadFeed();
    });
  });
  var searchEl = document.getElementById('search');
  var tId;
  searchEl.addEventListener('input', function(){
    state.searchQuery = searchEl.value;
    clearTimeout(tId); tId = setTimeout(loadFeed, 250);
  });
  if (state.user) {
    bindComposer({
      idPrefix: 'post',
      onSend: async function(text, quotedId, ogEnabled){
        state.suppressRefresh = Date.now() + 1500;
        var p = await api('/api/posts', { method: 'POST', body: { text: text, quoted_post_id: quotedId, og_enabled: ogEnabled } });
        state.searchQuery = '';
        // Оптимистично вставим в начало ленты
        var feedEl = document.getElementById('feed');
        if (feedEl) {
          var wrap = document.createElement('div');
          wrap.innerHTML = renderPostHtml(p, false);
          var newEl = wrap.firstChild;
          if (feedEl.firstChild) feedEl.insertBefore(newEl, feedEl.firstChild);
          else feedEl.appendChild(newEl);
          bindPostActions(feedEl); bindLinks(feedEl); bindOgImages(feedEl);
          // Уберём пустое состояние, если было
          var em = feedEl.querySelector('.empty');
          if (em) em.remove();
        }
      }
    });
  }
  loadFeed();
}
async function loadFeed() {
  var feedEl = document.getElementById('feed');
  if (!feedEl) return;
  try {
    var q = state.searchQuery.trim();
    var feed = state.feedMode === 'subs' ? '&feed=subs' : '';
    var data = await api('/api/posts?q=' + encodeURIComponent(q) + feed);
    var posts = data.posts || [];
    if (!posts.length) { feedEl.innerHTML = '<div class="empty">' + escapeHtml(tr('no_posts')) + '</div>'; return; }
    feedEl.innerHTML = posts.map(function(p){ return renderPostHtml(p, false); }).join('');
    bindPostActions(feedEl); bindLinks(feedEl); bindOgImages(feedEl);
  } catch(e) { feedEl.innerHTML = '<div class="empty">—</div>'; }
}

/* ============ POST VIEW ============ */
function renderPostView(el) {
  var html = '<div class="main-header">'
    + '<button class="icon-btn" id="backBtn">' + ICONS.back + '</button>'
    + '<div class="title">' + tr('nav_home') + '</div>'
    + '<button class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner">';
  html += '<div id="feed">' + spinner() + '</div>';
  if (state.user) html += composerHtml({ idPrefix: 'comment', placeholder: tr('comment_ph'), sendLabel: tr('send_comment') });
  html += '</div></div>';
  el.innerHTML = html;
  bindThemeBtn();
  document.getElementById('backBtn').addEventListener('click', function(){ navigate('/'); });
  bindLinks(el);
  if (state.user) {
    var replyBanner = document.createElement('div');
    replyBanner.id = 'replyBanner';
    var inputEl = document.getElementById('commentInput');
    inputEl.parentNode.insertBefore(replyBanner, inputEl);
    function renderReplyBanner() {
      if (!state.replyTo) { replyBanner.innerHTML = ''; inputEl.placeholder = tr('comment_ph'); return; }
      replyBanner.innerHTML = '<div class="quote-preview" style="margin-bottom:8px">'
        + '<div class="qp-author">' + tr('reply') + ': @' + escapeHtml(state.replyTo.author) + '</div>'
        + '<button class="qp-close" type="button">✕</button></div>';
      inputEl.placeholder = tr('reply_ph');
      replyBanner.querySelector('.qp-close').addEventListener('click', function(){
        state.replyTo = null; renderReplyBanner();
      });
    }
    renderReplyBanner();
    state._renderReplyBanner = renderReplyBanner;
    bindComposer({
      idPrefix: 'comment',
      placeholder: tr('comment_ph'),
      maxLen: MAX_COMMENT_LEN,
      onSend: async function(text){
        var parentId = state.replyTo ? state.replyTo.id : null;
        state.suppressRefresh = Date.now() + 1500;
        // Оптимистично вставляем комментарий
        var tempId = 'tmp-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
        var fakeComment = {
          id: tempId, text: text, author: state.user.nick,
          parent_id: parentId, created_at: Date.now()/1000,
          likes: 0, user_like: 0
        };
        var postEl = document.querySelector('[data-post-id="' + state.viewData.post_id + '"]');
        var postAuthor = postEl ? postEl.dataset.author : null;
        insertCommentIntoDom(state.viewData.post_id, fakeComment, postAuthor);
        state.replyTo = null; renderReplyBanner();
        try {
          var body = { text: text };
          if (parentId) body.parent_id = parentId;
          var res = await api('/api/posts/' + state.viewData.post_id + '/comments', { method: 'POST', body: body });
          // Заменим временный id на реальный
          var elC = document.querySelector('[data-comment-id="' + tempId + '"]');
          if (elC && res.comment) {
            var realId = res.comment.id;
            elC.setAttribute('data-comment-id', realId);
            elC.querySelectorAll('[data-comment-id]').forEach(function(b){ b.dataset.commentId = realId; });
            elC.classList.remove('pending');
          }
        } catch(e) {
          var elC = document.querySelector('[data-comment-id="' + tempId + '"]');
          if (elC) elC.parentNode.removeChild(elC);
          decrementCommentCount(state.viewData.post_id);
          alert(tr(e.message) || e.message);
        }
      }
    });
  }
  loadPostView();
}
async function loadPostView() {
  var feedEl = document.getElementById('feed');
  if (!feedEl) return;
  try {
    var p = await api('/api/posts/' + state.viewData.post_id);
    feedEl.innerHTML = renderPostHtml(p, true);
    bindPostActions(feedEl); bindLinks(feedEl); bindOgImages(feedEl);
    if (state.user && p.author !== state.user.nick) {
      api('/api/posts/' + state.viewData.post_id + '/view', { method: 'POST' }).catch(function(){});
    }
    if (state.highlightComment) {
      var node = feedEl.querySelector('[data-comment-id="' + state.highlightComment + '"]');
      if (node) {
        node.classList.add('highlight');
        setTimeout(function(){ node.scrollIntoView({behavior:'smooth', block:'center'}); }, 60);
        state.highlightComment = null;
      }
    }
  } catch(e) { feedEl.innerHTML = notFoundHtml(); bindLinks(feedEl); }
}

function insertCommentIntoDom(pid, c, postAuthor) {
  var postEl = document.querySelector('[data-post-id="' + pid + '"]');
  if (!postEl) return;
  var commentsEl = postEl.querySelector('.comments');
  if (!commentsEl) {
    commentsEl = document.createElement('div');
    commentsEl.className = 'comments';
    postEl.appendChild(commentsEl);
  }
  var html = renderCommentHtml(c, postAuthor, pid, !!c.parent_id);
  if (c.parent_id) {
    var parentEl = commentsEl.querySelector('[data-comment-id="' + c.parent_id + '"]');
    if (parentEl) {
      var next = parentEl.nextElementSibling;
      while (next && next.classList && next.classList.contains('reply')) next = next.nextElementSibling;
      if (next) next.insertAdjacentHTML('beforebegin', html);
      else commentsEl.insertAdjacentHTML('beforeend', html);
    } else {
      commentsEl.insertAdjacentHTML('beforeend', html);
    }
  } else {
    commentsEl.insertAdjacentHTML('beforeend', html);
  }
  // Инкремент счётчика комментариев
  var btn = postEl.querySelector('button[data-action="open-post"]');
  if (btn) {
    var span = btn.querySelector('span');
    if (span) span.textContent = (parseInt(span.textContent) || 0) + 1;
  }
  // Стилизуем pending комментарий
  var added = commentsEl.querySelector('[data-comment-id="' + c.id + '"]');
  if (added) added.classList.add('pending');
  bindPostActions(commentsEl); bindLinks(commentsEl);
}
function decrementCommentCount(pid) {
  var postEl = document.querySelector('[data-post-id="' + pid + '"]');
  if (!postEl) return;
  var btn = postEl.querySelector('button[data-action="open-post"]');
  if (btn) {
    var span = btn.querySelector('span');
    if (span) span.textContent = Math.max(0, (parseInt(span.textContent) || 0) - 1);
  }
}

/* ============ PROFILE ============ */
function renderProfileView(el) {
  var html = '<div class="main-header"><div class="title">' + tr('nav_profile') + '</div>'
    + '<button class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner" id="profileRoot">' + spinner() + '</div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  loadProfile(state.viewData.nick);
}
async function loadProfile(nick) {
  var root = document.getElementById('profileRoot');
  if (!root) return;
  try {
    var u = await api('/api/users/' + encodeURIComponent(nick));
    var isMe = state.user && state.user.nick === u.nick;

    var actionsHtml = '';
    if (isMe) {
      actionsHtml = '<a class="round-action" href="/settings" data-link title="' + escapeHtml(tr('nav_settings')) + '">' + ICONS.gear + '</a>'
                  + '<a class="pill-action primary" href="/settings/profile" data-link>' + escapeHtml(tr('edit_profile')) + '</a>';
    } else if (state.user) {
      actionsHtml = '<button class="pill-action ' + (u.is_following ? '' : 'primary') + '" id="followBtn">'
                  + (u.is_following ? tr('unfollow') : tr('follow')) + '</button>';
    } else {
      actionsHtml = '<a class="pill-action primary" href="/login" data-link>' + tr('go_login') + '</a>';
    }

    var bioHtml = u.bio ? '<span class="profile-bio-inline">' + escapeHtml(u.bio) + '</span>' : '';
    var lineClass = u.bio ? 'profile-line' : 'profile-line only-nick';

    var html = '<div class="profile-hero">';
    html += '<div class="profile-hero-row">';
    html += '<div class="profile-hero-avatar">' + avatarHtml(u.avatar_emoji, 'lg') + '</div>';
    html += '<div class="profile-hero-info">';
    html += '<div class="profile-name">' + escapeHtml(u.name) + '</div>';
    html += '<div class="' + lineClass + '">';
    html += '<span class="profile-nick">@' + escapeHtml(u.nick) + '</span>';
    html += bioHtml;
    html += '</div>';
    html += '<div class="profile-stats">';
    html += '<div><b id="followersLink">' + u.followers + '</b><span>' + tr('profile_followers') + '</span></div>';
    html += '<div><b id="followingLink">' + u.following + '</b><span>' + tr('profile_following') + '</span></div>';
    html += '</div>';
    html += '<div class="profile-meta">' + ICONS.cal + '<span>' + (LANG === 'ru' ? 'Регистрация: ' : 'Joined: ') + fmtDate(u.created_at) + '</span></div>';
    html += '</div></div>';
    html += '<div class="profile-hero-actions">' + actionsHtml + '</div>';
    html += '</div>';
    html += '<div id="profileContent">' + spinner() + '</div>';
    root.innerHTML = html;
    bindLinks(root);

    document.getElementById('followersLink').addEventListener('click', function(){
      navigate('/u/' + encodeURIComponent(u.nick) + '/followers');
    });
    document.getElementById('followingLink').addEventListener('click', function(){
      navigate('/u/' + encodeURIComponent(u.nick) + '/following');
    });
    var btn = document.getElementById('followBtn');
    if (btn) btn.addEventListener('click', async function(){
      try {
        if (btn.textContent.trim() === tr('unfollow')) {
          await api('/api/users/' + encodeURIComponent(u.nick) + '/unfollow', { method: 'POST' });
        } else {
          await api('/api/users/' + encodeURIComponent(u.nick) + '/follow', { method: 'POST' });
        }
        loadProfile(nick);
      } catch(e) { alert(tr(e.message) || e.message); }
    });

    loadProfileContent(u, isMe);
  } catch(e) {
    root.innerHTML = notFoundHtml();
    bindLinks(root);
  }
}
async function loadProfileContent(u, isMe) {
  var c = document.getElementById('profileContent');
  if (!c) return;
  c.innerHTML = spinner();
  var html = '';
  if (state.user && isMe) {
    html += composerHtml({ idPrefix: 'profile_post', placeholder: tr('post_ph') });
  }
  try {
    var d = await api('/api/posts?author=' + encodeURIComponent(u.nick));
    var posts = (d.posts || []).slice();
    posts.sort(function(a, b){ return b.created_at - a.created_at; });
    if (!posts.length) html += '<div class="empty">' + escapeHtml(tr('no_user_posts')) + '</div>';
    else html += posts.map(function(p){ return renderPostHtml(p, false); }).join('');
    c.innerHTML = html;
    bindPostActions(c); bindLinks(c); bindOgImages(c);
    if (state.user && isMe) {
      bindComposer({
        idPrefix: 'profile_post',
        onSend: async function(text, quotedId, ogEnabled){
          state.suppressRefresh = Date.now() + 1500;
          var p = await api('/api/posts', { method: 'POST', body: { text: text, quoted_post_id: quotedId, og_enabled: ogEnabled } });
          var wrap = document.createElement('div');
          wrap.innerHTML = renderPostHtml(p, false);
          var newEl = wrap.firstChild;
          // Вставляем после композера
          var composerEl = c.querySelector('.card');
          if (composerEl && composerEl.nextSibling) c.insertBefore(newEl, composerEl.nextSibling);
          else c.appendChild(newEl);
          var em = c.querySelector('.empty');
          if (em) em.remove();
          bindPostActions(c); bindLinks(c); bindOgImages(c);
        }
      });
    }
  } catch(e) { c.innerHTML = html + '<div class="empty">—</div>'; }
}

/* ============ EDIT PROFILE ============ */
function renderEditProfileView(el) {
  if (!state.user) { navigate('/login'); return; }
  var u = state.user;
  state.currentEmoji = u.avatar_emoji || DEFAULT_EMOJI;

  var html = '<div class="main-header"><button class="icon-btn" id="backBtn">' + ICONS.back + '</button>'
    + '<div class="title">' + tr('edit_profile_title') + '</div>'
    + '<button class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner">';

  html += '<div class="card">';
  html += '<div class="emoji-current">'
    + '<div class="preview" id="emojiPreview">' + escapeHtml(state.currentEmoji) + '</div>'
    + '<div class="label">' + escapeHtml(tr('avatar_current')) + '</div>'
    + '</div>';
  html += '<div style="font-size:14px;font-weight:700;margin-bottom:10px">' + escapeHtml(tr('avatar_choose')) + '</div>';
  html += '<div class="emoji-grid" id="emojiGrid">';
  for (var i = 0; i < EMOJIS.length; i++) {
    var e = EMOJIS[i];
    html += '<button type="button" class="emoji-opt' + (e === state.currentEmoji ? ' active' : '') + '" data-emoji="' + escapeHtml(e) + '">' + e + '</button>';
  }
  html += '</div></div>';

  html += '<div class="card"><form id="editForm" style="display:flex;flex-direction:column;gap:10px">'
    + '<input type="text" name="name" maxlength="50" placeholder="' + escapeHtml(tr('name_ph')) + '" value="' + escapeHtml(u.name) + '" required style="padding:15px 18px;background:var(--card-2);border:none;color:var(--text);font-family:inherit;font-size:15px;outline:none;border-radius:14px" />'
    + '<input type="text" name="nick" maxlength="20" placeholder="' + escapeHtml(tr('nick_ph')) + '" value="' + escapeHtml(u.nick) + '" required style="padding:15px 18px;background:var(--card-2);border:none;color:var(--text);font-family:inherit;font-size:15px;outline:none;border-radius:14px" />'
    + '<textarea name="bio" maxlength="' + MAX_BIO_LEN + '" placeholder="' + escapeHtml(tr('bio_ph')) + '" class="edit-bio-textarea">' + escapeHtml(u.bio || '') + '</textarea>'
    + '<div class="auth-error" id="editError"></div>'
    + '<button type="submit" class="publish-btn" style="height:52px;font-size:16px;width:100%">' + tr('save') + '</button>'
    + '</form></div>';

  html += '</div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  document.getElementById('backBtn').addEventListener('click', function(){ navigate('/settings'); });

  var previewEl = document.getElementById('emojiPreview');
  var gridEl = document.getElementById('emojiGrid');
  gridEl.querySelectorAll('.emoji-opt').forEach(function(b){
    b.addEventListener('click', function(e){
      e.preventDefault();
      state.currentEmoji = b.dataset.emoji;
      previewEl.textContent = state.currentEmoji;
      gridEl.querySelectorAll('.emoji-opt').forEach(function(x){
        x.classList.toggle('active', x.dataset.emoji === state.currentEmoji);
      });
    });
  });

  var form = document.getElementById('editForm');
  var errEl = document.getElementById('editError');
  form.addEventListener('submit', async function(e){
    e.preventDefault();
    errEl.textContent = '';
    var fd = new FormData(form);
    var body = {
      name: (fd.get('name') || '').toString().trim(),
      nick: (fd.get('nick') || '').toString().trim().replace(/^@/, ''),
      bio: (fd.get('bio') || '').toString().trim(),
      avatar_emoji: state.currentEmoji || DEFAULT_EMOJI
    };
    var btn = form.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
      var r = await api('/api/users/me', { method: 'PUT', body: body });
      setUser(r.user); renderSidebar();
      navigate('/u/' + encodeURIComponent(r.user.nick), true);
    } catch(err) {
      errEl.textContent = tr(err.message) || err.message;
      btn.disabled = false;
    }
  });
}

/* ============ FOLLOWERS/FOLLOWING ============ */
function renderFollowListView(el) {
  var nick = state.viewData.nick;
  var isFollowers = state.view === 'followers';
  var title = isFollowers ? tr('followers_title') : tr('following_title');
  var html = '<div class="main-header"><button class="icon-btn" id="backBtn">' + ICONS.back + '</button>'
    + '<div class="title">' + escapeHtml(title) + '</div>'
    + '<button class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner"><div id="list">' + spinner() + '</div></div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  document.getElementById('backBtn').addEventListener('click', function(){ navigate('/u/' + encodeURIComponent(nick)); });
  loadFollowList(nick, isFollowers);
}
function userRowHtml(u) {
  return '<div class="user-row" data-link-row="/u/' + encodeURIComponent(u.nick) + '">'
    + avatarHtml(u.avatar_emoji, 'sm')
    + '<div class="info">'
    + '<a class="nick" href="/u/' + encodeURIComponent(u.nick) + '" data-link>@' + escapeHtml(u.nick) + '</a>'
    + '<div class="name">' + escapeHtml(u.name) + '</div>'
    + '</div></div>';
}
function bindUserRows(root) {
  root.querySelectorAll('[data-link-row]').forEach(function(r){
    r.addEventListener('click', function(e){
      if (e.target.closest('[data-link]')) return;
      navigate(r.dataset.linkRow);
    });
  });
}
async function loadFollowList(nick, isFollowers) {
  var wrap = document.getElementById('list');
  try {
    var path = isFollowers
      ? ('/api/users/' + encodeURIComponent(nick) + '/followers')
      : ('/api/users/' + encodeURIComponent(nick) + '/following');
    var data = await api(path);
    var users = data.users || [];
    var limitedNote = data.limited ? '<div class="empty" style="margin-bottom:12px;padding:16px">' + escapeHtml(tr('limited_list')) + '</div>' : '';
    if (!users.length) { wrap.innerHTML = '<div class="empty">' + escapeHtml(isFollowers ? tr('no_followers') : tr('no_following')) + '</div>'; return; }
    wrap.innerHTML = limitedNote + users.map(userRowHtml).join('');
    bindLinks(wrap); bindUserRows(wrap);
  } catch(e) { wrap.innerHTML = '<div class="empty">' + escapeHtml(tr(e.message) || '—') + '</div>'; }
}

/* ============ PEOPLE ============ */
function renderUsersView(el) {
  var html = '<div class="main-header"><div class="title">' + tr('people_title') + '</div>'
    + '<button class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner">';
  html += '<div class="search-box">' + ICONS.search + '<input id="peopleSearch" type="text" placeholder="' + escapeHtml(tr('people_search_ph')) + '" value="' + escapeHtml(state.peopleQuery) + '" /></div>';
  html += '<div class="pill-tabs">';
  html += '<button class="pill-tab' + (state.peopleTab==='all'?' active':'') + '" data-tab="all">' + tr('people_all') + '</button>';
  html += '<button class="pill-tab' + (state.peopleTab==='subs'?' active':'') + '" data-tab="subs">' + tr('people_subs') + '</button>';
  html += '</div>';
  html += '<div id="list">' + spinner() + '</div>';
  html += '</div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  el.querySelectorAll('.pill-tab').forEach(function(t){
    t.addEventListener('click', function(){
      state.peopleTab = t.dataset.tab;
      el.querySelectorAll('.pill-tab').forEach(function(x){ x.classList.toggle('active', x === t); });
      loadPeople();
    });
  });
  var searchEl = document.getElementById('peopleSearch');
  var tId;
  searchEl.addEventListener('input', function(){
    state.peopleQuery = searchEl.value;
    clearTimeout(tId); tId = setTimeout(loadPeople, 250);
  });
  loadPeople();
}
async function loadPeople() {
  var wrap = document.getElementById('list');
  if (!wrap) return;
  if (!state.user && state.peopleTab === 'subs') {
    wrap.innerHTML = '<div class="empty">' + escapeHtml(tr('login_to_post')) + '</div>';
    return;
  }
  try {
    var q = (state.peopleQuery || '').trim();
    var url = '/api/users?q=' + encodeURIComponent(q);
    if (state.peopleTab === 'subs') url += '&only_following=1';
    var data = await api(url);
    var users = data.users || [];
    if (!users.length) { wrap.innerHTML = '<div class="empty">' + escapeHtml(tr('no_users')) + '</div>'; return; }
    wrap.innerHTML = users.map(userRowHtml).join('');
    bindLinks(wrap); bindUserRows(wrap);
  } catch(e) { wrap.innerHTML = '<div class="empty">—</div>'; }
}

/* ============ NOTIFICATIONS ============ */
function renderNotificationsView(el) {
  var html = '<div class="main-header"><div class="title">' + tr('notif_title') + '</div>'
    + '<button class="icon-btn danger" id="clearNotifsBtn">' + ICONS.trash + '</button>'
    + '<button class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner"><div id="notifList">' + spinner() + '</div></div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  document.getElementById('clearNotifsBtn').addEventListener('click', function(){
    showConfirm(tr('notif_clear_confirm'), async function(){
      try {
        await api('/api/notifications/clear', { method: 'POST' });
        setNotifCount(0); renderSidebar(); loadNotifications();
      } catch(e) { alert(tr(e.message) || e.message); }
    }, { yesText: tr('notif_clear') });
  });
  loadNotifications();
}
async function loadNotifications() {
  var wrap = document.getElementById('notifList');
  if (!wrap) return;
  try {
    var data = await api('/api/notifications');
    var items = data.items || [];
    setNotifCount(data.unread || 0);
    if (data.unread > 0) {
      api('/api/notifications/read', { method: 'POST' }).catch(function(){});
      items.forEach(function(n){ n.read = true; });
      setNotifCount(0); renderSidebar();
    }
    if (!items.length) { wrap.innerHTML = '<div class="empty">' + escapeHtml(tr('notif_empty')) + '</div>'; return; }
    wrap.innerHTML = items.map(renderNotifHtml).join('');
    bindLinks(wrap);
  } catch(e) { wrap.innerHTML = '<div class="empty">—</div>'; }
}
function renderNotifHtml(n) {
  var cls = 'notif-row' + (n.read ? '' : ' unread');
  var author = '<b>@' + escapeHtml(n.from_nick || '?') + '</b>';
  var text = '', link = null;
  if (n.type === 'follow') {
    text = author + ' ' + tr('notif_follow');
    link = '/u/' + encodeURIComponent(n.from_nick);
  } else if (n.type === 'comment' || n.type === 'reply' || n.type === 'mention') {
    var label = n.type === 'comment' ? tr('notif_comment') : n.type === 'reply' ? tr('notif_reply') : tr('notif_mention');
    text = author + ' ' + label;
    link = n.post_id ? ('/p/' + n.post_id + (n.comment_id ? ('#c-' + n.comment_id) : '')) : null;
  } else if (n.type === 'new_post' || n.type === 'quote') {
    var lbl = n.type === 'new_post' ? tr('notif_new_post') : tr('notif_quote');
    text = author + ' ' + lbl;
    link = n.post_id ? ('/p/' + n.post_id) : null;
  } else {
    text = author;
  }
  var snippet = '';
  if (n.text) snippet = '<div class="snippet">' + escapeHtml(n.text) + '</div>';
  var inner = '<div class="info"><div class="line">' + text + '</div>'
    + snippet
    + '<div class="time">' + timeAgo(n.created_at) + '</div></div>';
  if (link) return '<a class="' + cls + '" href="' + link + '" data-link>' + avatarHtml(null, 'sm') + inner + '</a>';
  return '<div class="' + cls + '">' + avatarHtml(null, 'sm') + inner + '</div>';
}

/* ============ SETTINGS ============ */
function settingsToggleRow(label, key, on) {
  return '<div class="toggle-row"><span>' + escapeHtml(label) + '</span>'
    + '<div class="toggle' + (on ? ' on' : '') + '" data-toggle="' + key + '"></div></div>';
}
function renderSettingsView(el) {
  var theme = document.documentElement.getAttribute('data-theme') || 'dark';
  var me = state.user || {};
  var colors = loadColors();
  var sec = state.settingsSection || 'account';

  var html = '<div class="main-header wide"><div class="title">' + tr('settings_title') + '</div>'
    + '<button class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="settings-layout">';
  html += '<nav class="settings-nav">';
  html += '<button class="settings-nav-btn' + (sec==='account'?' active':'') + '" data-section="account">' + ICONS.user + ' ' + tr('settings_account') + '</button>';
  html += '<button class="settings-nav-btn' + (sec==='privacy'?' active':'') + '" data-section="privacy">' + ICONS.bell + ' ' + tr('settings_privacy') + '</button>';
  html += '<button class="settings-nav-btn' + (sec==='appearance'?' active':'') + '" data-section="appearance">' + ICONS.sun + ' ' + tr('settings_appearance') + '</button>';
  html += '<button class="settings-nav-btn' + (sec==='info'?' active':'') + '" data-section="info">' + ICONS.gear + ' ' + tr('settings_info') + '</button>';
  html += '</nav>';
  html += '<div class="settings-content">';

  /* ACCOUNT */
  html += '<div class="settings-block" id="section-account"><h2>' + tr('settings_account') + '</h2>';
  if (state.user) {
    html += '<a class="publish-btn" href="/settings/profile" data-link style="display:inline-block;text-decoration:none;line-height:44px;height:44px;margin-bottom:14px">' + tr('edit_profile') + '</a>';
    html += '<div style="margin-top:6px"><button class="modal-btn danger" style="width:auto;padding:0 20px;height:44px" id="settingsLogout">' + tr('settings_logout') + '</button></div>';
  } else {
    html += '<a class="publish-btn" href="/login" data-link style="display:inline-block;text-decoration:none;line-height:44px;height:44px">' + tr('go_login') + '</a>';
  }
  html += '</div>';

  /* PRIVACY */
  html += '<div class="settings-block" id="section-privacy"><h2>' + tr('settings_privacy') + '</h2>';
  if (state.user) {
    html += '<div class="settings-subhead">' + escapeHtml(tr('settings_privacy')) + '</div>';
    html += settingsToggleRow(tr('settings_allow_followers'), 'allow_followers_view', me.allow_followers_view !== false);
    html += settingsToggleRow(tr('settings_allow_following'), 'allow_following_view', me.allow_following_view !== false);
    html += settingsToggleRow(tr('settings_show_device_badge'), 'show_device_badge', me.show_device_badge !== false);
    html += '<p class="settings-desc" style="margin-top:6px">' + escapeHtml(tr('settings_show_device_badge_hint')) + '</p>';
    html += '<div class="settings-subhead">' + escapeHtml(tr('settings_notifications')) + '</div>';
    html += settingsToggleRow(tr('notify_new_post'),  'notify_on_new_post',  me.notify_on_new_post  !== false);
    html += settingsToggleRow(tr('notify_follow'),    'notify_on_follow',    me.notify_on_follow    !== false);
    html += settingsToggleRow(tr('notify_comment'),   'notify_on_comment',   me.notify_on_comment   !== false);
    html += settingsToggleRow(tr('notify_reply'),     'notify_on_reply',     me.notify_on_reply     !== false);
    html += settingsToggleRow(tr('notify_mention'),   'notify_on_mention',   me.notify_on_mention   !== false);
    html += settingsToggleRow(tr('notify_quote'),     'notify_on_quote',     me.notify_on_quote     !== false);
  } else {
    html += '<p class="settings-desc">' + escapeHtml(tr('login_to_post')) + '</p>';
  }
  html += '</div>';

  /* APPEARANCE */
  html += '<div class="settings-block" id="section-appearance"><h2>' + tr('settings_appearance') + '</h2>';
  html += '<div style="font-size:13px;color:var(--muted);margin-bottom:8px">' + tr('settings_theme') + '</div>';
  html += '<div class="opt-row">';
  html += '<button class="opt' + (theme==='dark'?' active':'') + '" data-set-theme="dark">' + tr('theme_dark') + '</button>';
  html += '<button class="opt' + (theme==='light'?' active':'') + '" data-set-theme="light">' + tr('theme_light') + '</button>';
  html += '</div>';
  html += '<div style="font-size:13px;color:var(--muted);margin-bottom:8px">' + tr('settings_lang') + '</div>';
  html += '<div class="opt-row">';
  html += '<button class="opt' + (LANG==='ru'?' active':'') + '" data-set-lang="ru">Русский</button>';
  html += '<button class="opt' + (LANG==='en'?' active':'') + '" data-set-lang="en">English</button>';
  html += '</div>';
  if (state.user) {
    html += settingsToggleRow(tr('settings_show_link_previews'), 'show_link_previews', me.show_link_previews !== false);
    html += '<p class="settings-desc" style="margin-top:6px">' + escapeHtml(tr('settings_show_link_previews_hint')) + '</p>';
  }
  html += '<div class="settings-subhead" style="margin-top:16px">' + escapeHtml(tr('settings_colors')) + '</div>';
  html += '<p class="settings-desc" style="margin-bottom:10px">' + escapeHtml(tr('settings_colors_hint')) + '</p>';
  html += '<div style="font-size:13px;color:var(--muted);margin-bottom:6px">' + escapeHtml(tr('color_accent')) + '</div>';
  html += '<div class="color-swatches" data-color-group="accent">';
  COLOR_PRESETS.accent.forEach(function(p){
    var isActive = (colors.accent || 'blue') === p.id;
    var val = theme === 'dark' ? p.dark : p.light;
    html += '<button type="button" class="color-swatch' + (isActive?' active':'') + '" data-color="' + p.id + '" title="' + escapeHtml(LANG === 'ru' ? p.ru : p.en) + '" style="background:' + val + '"></button>';
  });
  html += '</div>';
  html += '<div style="font-size:13px;color:var(--muted);margin-bottom:6px">' + escapeHtml(tr('color_likes')) + '</div>';
  html += '<div class="color-swatches" data-color-group="like">';
  COLOR_PRESETS.like.forEach(function(p){
    var isActive = (colors.like || 'red') === p.id;
    var val = theme === 'dark' ? p.dark : p.light;
    html += '<button type="button" class="color-swatch' + (isActive?' active':'') + '" data-color="' + p.id + '" title="' + escapeHtml(LANG === 'ru' ? p.ru : p.en) + '" style="background:' + val + '"></button>';
  });
  html += '</div>';
  html += '<button class="opt" id="resetColors" style="margin-top:4px">' + escapeHtml(tr('reset_colors')) + '</button>';
  html += '</div>';

  /* INFO */
  html += '<div class="settings-block" id="section-info"><h2>' + tr('settings_info') + '</h2>';
  html += '<p class="settings-desc">' + tr('settings_desc') + '</p>';
  html += '<p style="margin:0 0 14px"><a class="settings-link" href="/policy" data-link>' + tr('settings_policy') + '</a></p>';
  html += '<div style="font-size:12px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px;font-weight:700">' + tr('settings_authors') + '</div>';
  html += '<div style="font-size:15px;margin-bottom:16px">SldShr, DeepSeek</div>';
  html += '<div class="settings-subhead">' + escapeHtml(tr('settings_device')) + '</div>';
  html += '<div class="device-info" id="deviceInfo">' + spinner() + '</div>';
  html += '</div>';

  html += '</div></div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);

  function applySectionVisibility() {
    var isMobile = window.matchMedia('(max-width: 900px)').matches;
    var sections = el.querySelectorAll('.settings-block');
    if (isMobile) sections.forEach(function(s){ s.style.display = ''; });
    else sections.forEach(function(s){
      if (!s.id || s.id.indexOf('section-') !== 0) return;
      s.style.display = (s.id.replace('section-', '') === state.settingsSection) ? '' : 'none';
    });
    el.querySelectorAll('.settings-nav-btn').forEach(function(b){
      b.classList.toggle('active', b.dataset.section === state.settingsSection);
    });
  }
  applySectionVisibility();

  el.querySelectorAll('.settings-nav-btn').forEach(function(b){
    b.addEventListener('click', function(){
      state.settingsSection = b.dataset.section;
      var isMobile = window.matchMedia('(max-width: 900px)').matches;
      if (isMobile) {
        var target = document.getElementById('section-' + state.settingsSection);
        if (target) {
          var bodyEl = el.querySelector('.main-body');
          var hdr = el.querySelector('.main-header');
          var offset = (hdr ? hdr.offsetHeight : 0);
          if (bodyEl) bodyEl.scrollTo({ top: target.offsetTop - offset - 60, behavior: 'smooth' });
        }
      }
      applySectionVisibility();
    });
  });
  el.querySelectorAll('[data-set-theme]').forEach(function(b){
    b.addEventListener('click', function(){ applyTheme(b.dataset.setTheme); renderSettingsView(el); });
  });
  el.querySelectorAll('[data-set-lang]').forEach(function(b){
    b.addEventListener('click', function(){
      document.cookie = 'SLD_lang=' + b.dataset.setLang + '; path=/; max-age=' + (60*60*24*365);
      // сохраняем активную секцию и текущую страницу
      try {
        sessionStorage.setItem('SLD_set_sec', state.settingsSection);
        sessionStorage.setItem('SLD_reload_path', '/settings');
      } catch(e) {}
      location.href = '/settings';
      location.reload();
    });
  });
  el.querySelectorAll('[data-color-group]').forEach(function(group){
    var groupKey = group.dataset.colorGroup;
    group.querySelectorAll('.color-swatch').forEach(function(sw){
      sw.addEventListener('click', function(){
        setColor(groupKey, sw.dataset.color);
        group.querySelectorAll('.color-swatch').forEach(function(x){ x.classList.toggle('active', x === sw); });
      });
    });
  });
  var rc = document.getElementById('resetColors');
  if (rc) rc.addEventListener('click', function(){
    saveColors({}); applyColors(); renderSettingsView(el);
  });

  el.querySelectorAll('[data-toggle]').forEach(function(t){
    t.addEventListener('click', async function(){
      var key = t.dataset.toggle;
      var newVal = !t.classList.contains('on');
      t.classList.toggle('on', newVal);
      var patch = {}; patch[key] = newVal;
      var meNow = state.user || {}; meNow[key] = newVal; setUser(meNow);
      try { await api('/api/users/me/settings', { method: 'POST', body: patch }); }
      catch(e) {
        t.classList.toggle('on', !newVal);
        meNow[key] = !newVal; setUser(meNow);
        alert(tr(e.message) || e.message);
      }
    });
  });

  var di = document.getElementById('deviceInfo');
  if (di) {
    api('/api/whoami').then(function(info){
      state.whoami = info;
      var city = info.city || '—';
      var country = info.country ? (', ' + info.country) : '';
      var cityFull = (city === '—') ? '—' : (city + country);
      di.innerHTML = ''
        + '<div class="device-info-row"><span class="label">' + escapeHtml(tr('settings_device_browser')) + '</span>'
        + '<span class="value">' + escapeHtml(info.browser || '—') + '</span></div>'
        + '<div class="device-info-row"><span class="label">' + escapeHtml(tr('settings_device_os')) + '</span>'
        + '<span class="value">' + escapeHtml(info.os || '—') + '</span></div>'
        + '<div class="device-info-row"><span class="label">' + escapeHtml(tr('settings_device_city')) + '</span>'
        + '<span class="value">' + escapeHtml(cityFull) + '</span></div>';
    }).catch(function(){
      di.innerHTML = '<div class="device-info-row"><span class="label">—</span></div>';
    });
  }

  var lo = document.getElementById('settingsLogout');
  if (lo) lo.addEventListener('click', doLogoutConfirm);
}

function renderPolicyView(el) {
  var html = '<div class="main-header"><button class="icon-btn" id="backBtn">' + ICONS.back + '</button>'
    + '<div class="title">' + tr('policy_title') + '</div>'
    + '<button class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="policy"><h1>' + tr('policy_title') + '</h1><p>' + escapeHtml(tr('policy_content')) + '</p></div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  document.getElementById('backBtn').addEventListener('click', function(){ navigate('/settings'); });
}

/* ============ AUTH ============ */
function renderRegisterView(el) {
  var html = '<div class="main-body"><div class="auth-page"><div class="auth-card">';
  html += '<h1>' + tr('reg_title') + '</h1>';
  html += '<form id="regForm">';
  html += '<input type="text" name="name" placeholder="' + escapeHtml(tr('name_ph')) + '" maxlength="50" required />';
  html += '<input type="text" name="nick" placeholder="' + escapeHtml(tr('nick_ph')) + '" maxlength="20" required />';
  html += '<input type="password" name="password" placeholder="' + escapeHtml(tr('pass_ph')) + '" required />';
  html += '<input type="password" name="password_confirm" placeholder="' + escapeHtml(tr('pass2_ph')) + '" required />';
  html += '<div class="auth-error" id="regError"></div>';
  html += '<button type="submit">' + tr('reg_btn') + '</button>';
  html += '</form>';
  html += '<div class="auth-switch"><a id="toLogin">' + tr('to_login') + '</a></div>';
  html += '</div></div></div>';
  el.innerHTML = html;
  document.getElementById('toLogin').addEventListener('click', function(){ navigate('/login'); });
  var form = document.getElementById('regForm');
  var errEl = document.getElementById('regError');
  form.addEventListener('submit', async function(e){
    e.preventDefault(); errEl.textContent = '';
    var fd = new FormData(form);
    try {
      await doRegister({
        name: fd.get('name'), nick: fd.get('nick'),
        password: fd.get('password'), password_confirm: fd.get('password_confirm') });
    } catch(err) { errEl.textContent = tr(err.message) || err.message; }
  });
}
function renderLoginView(el) {
  var html = '<div class="main-body"><div class="auth-page"><div class="auth-card">';
  html += '<h1>' + tr('log_title') + '</h1>';
  html += '<form id="logForm">';
  html += '<input type="text" name="nick" placeholder="' + escapeHtml(tr('nick_ph')) + '" required />';
  html += '<input type="password" name="password" placeholder="' + escapeHtml(tr('pass_ph')) + '" required />';
  html += '<div class="auth-error" id="logError"></div>';
  html += '<button type="submit">' + tr('log_btn') + '</button>';
  html += '</form>';
  html += '<div class="auth-switch"><a id="toReg">' + tr('to_reg') + '</a></div>';
  html += '</div></div></div>';
  el.innerHTML = html;
  document.getElementById('toReg').addEventListener('click', function(){ navigate('/register'); });
  var form = document.getElementById('logForm');
  var errEl = document.getElementById('logError');
  form.addEventListener('submit', async function(e){
    e.preventDefault(); errEl.textContent = '';
    var fd = new FormData(form);
    try { await doLogin({ nick: fd.get('nick'), password: fd.get('password') }); }
    catch(err) { errEl.textContent = tr(err.message) || err.message; }
  });
}

/* ============ POST RENDER ============ */
function renderOgCard(og) {
  if (!og) return '';
  if (state.user && state.user.show_link_previews === false) return '';
  var site = og.site_name || '';
  if (!site) { try { site = new URL(og.url).hostname; } catch(e) {} }
  var imgHtml = og.image
    ? '<div class="og-image"><img src="' + escapeHtml(og.image) + '" alt="" loading="lazy" /></div>'
    : '';
  return '<a class="og-card" href="' + escapeHtml(og.url) + '" target="_blank" rel="noopener noreferrer">'
    + imgHtml
    + '<div class="og-body">'
    + (site ? '<div class="og-site">' + escapeHtml(site) + '</div>' : '')
    + (og.title ? '<div class="og-title">' + escapeHtml(og.title) + '</div>' : '')
    + (og.description ? '<div class="og-desc">' + escapeHtml(og.description) + '</div>' : '')
    + '</div></a>';
}

function renderPostHtml(p, showComments) {
  var liked = p.user_like === 1;
  var likeCls = liked ? 'active' : '';
  var displayText = p.text, truncated = false;
  if (!showComments) {
    var res = truncateText(p.text);
    displayText = res.text; truncated = res.truncated;
  }
  var bodyHtml = linkifyText(displayText);
  var readMore = truncated
    ? '<span class="read-more" data-action="open-post" data-post-id="' + p.id + '">' + escapeHtml(tr('read_more')) + '</span>'
    : '';
  var isMine = state.user && p.author === state.user.nick;

  var authorHtml = '<a class="post-author" href="/u/' + encodeURIComponent(p.author) + '" data-link>@' + escapeHtml(p.author) + '</a>';

  var deviceBadge = '';
  if (p.device && p.author_show_device !== false) {
    var isMob = p.device === 'mobile';
    var icon = isMob ? ICONS.mobile : ICONS.desktop;
    var title = isMob ? tr('sent_from_mobile') : tr('sent_from_desktop');
    deviceBadge = '<span class="device-badge" title="' + escapeHtml(title) + '">' + icon + '</span>';
  }

  var quotedHtml = '';
  if (p.quoted) {
    var qHtml = linkifyText(p.quoted.text || '');
    quotedHtml = '<div class="quoted-post" data-action="open-post" data-post-id="' + p.quoted.id + '">'
      + '<div class="q-author">@' + escapeHtml(p.quoted.author || '?') + '</div>'
      + '<div class="q-text">' + qHtml + '</div>'
      + '</div>';
  }

  var menuHtml = '';
  if (isMine) {
    menuHtml = '<div class="post-menu">'
      + '<button class="act-btn" data-action="edit-post" data-post-id="' + p.id + '" title="' + escapeHtml(tr('edit')) + '">' + ICONS.edit + '</button>'
      + '<button class="act-btn danger" data-action="delete-post" data-post-id="' + p.id + '" title="' + escapeHtml(tr('delete')) + '">' + ICONS.trash + '</button>'
      + '</div>';
  }

  var ogHtml = renderOgCard(p.og_data);

  var commentsHtml = '';
  if (showComments && p.comments && p.comments.length) {
    commentsHtml = '<div class="comments">' + renderCommentsTree(p.comments, p.author, p.id) + '</div>';
  }
  var cCount = (typeof p.comment_count === 'number') ? p.comment_count
             : (p.comments ? p.comments.length : 0);
  var viewsHtml = '';
  if (p.views) viewsHtml = '<span class="views-badge">' + ICONS.eye + '<span data-views>' + p.views + '</span></span>';
  var heartIcon = liked ? ICONS.heart_filled : ICONS.heart;

  return ''
    + '<div class="post-card" data-post-id="' + p.id + '" data-author="' + escapeHtml(p.author || '') + '">'
    +   '<div class="post-header">'
    +     avatarHtml(p.author_avatar_emoji)
    +     '<div class="meta">'
    +       '<div class="who">' + authorHtml + deviceBadge + '<span class="post-time">' + timeAgo(p.created_at) + '</span></div>'
    +     '</div>'
    +     menuHtml
    +   '</div>'
    +   (displayText ? '<div class="post-text" data-raw="' + escapeHtml(p.text) + '">' + bodyHtml + '</div>' : '')
    +   readMore
    +   ogHtml
    +   quotedHtml
    +   '<div class="post-actions">'
    +     '<button class="act-btn like-btn ' + likeCls + '" data-action="like" data-post-id="' + p.id + '">' + heartIcon + '<span class="num">' + (p.likes || 0) + '</span></button>'
    +     '<button class="act-btn" data-action="open-post" data-post-id="' + p.id + '">' + ICONS.comment + '<span>' + cCount + '</span></button>'
    +     '<button class="act-btn" data-action="quote" data-post-id="' + p.id + '" title="' + escapeHtml(tr('quote')) + '">' + ICONS.quote + '</button>'
    +     '<button class="act-btn" data-action="copy" data-post-id="' + p.id + '" title="' + escapeHtml(tr('copy')) + '">' + ICONS.copy + '</button>'
    +     viewsHtml
    +   '</div>'
    +   commentsHtml
    + '</div>';
}
function renderCommentsTree(comments, postAuthor, postId) {
  var tops = comments.filter(function(c){ return !c.parent_id; }).sort(function(a,b){ return a.created_at - b.created_at; });
  var repliesBy = {};
  comments.forEach(function(c){ if (c.parent_id) (repliesBy[c.parent_id] = repliesBy[c.parent_id] || []).push(c); });
  var html = '';
  tops.forEach(function(c){
    html += renderCommentHtml(c, postAuthor, postId, false);
    var reps = repliesBy[c.id] || [];
    reps.sort(function(a,b){ return a.created_at - b.created_at; });
    reps.forEach(function(r){ html += renderCommentHtml(r, postAuthor, postId, true); });
  });
  return html;
}
function renderCommentHtml(c, postAuthor, postId, isReply) {
  var liked = c.user_like === 1;
  var likeCls = liked ? 'active' : '';
  var isAuthor = postAuthor && c.author === postAuthor;
  var isMine = state.user && c.author === state.user.nick;
  var cls = 'comment' + (isReply ? ' reply' : '') + (isAuthor ? ' is-author' : '');
  var authorHtml = c.author ? '<a class="comment-author" href="/u/' + encodeURIComponent(c.author) + '" data-link>@' + escapeHtml(c.author) + '</a>' : '';
  var badge = isAuthor ? '<span class="comment-author-badge">' + escapeHtml(tr('author_badge')) + '</span>' : '';
  var replyBtn = '';
  if (!isReply && state.user) {
    replyBtn = '<button class="act-btn" data-action="reply" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-author="' + escapeHtml(c.author || '') + '">' + tr('reply') + '</button>';
  }
  var editBtn = isMine ? '<button class="act-btn" data-action="edit-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '">' + ICONS.edit + '</button>' : '';
  var delBtn = isMine ? '<button class="act-btn danger" data-action="delete-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '">' + ICONS.trash + '</button>' : '';
  var bodyHtml = linkifyText(c.text);
  var heartIcon = liked ? ICONS.heart_filled : ICONS.heart;
  return ''
    + '<div class="' + cls + '" data-comment-id="' + c.id + '">'
    +   '<div class="comment-head">' + authorHtml + badge + '<span class="comment-time">' + timeAgo(c.created_at) + '</span></div>'
    +   '<div class="comment-text" data-raw="' + escapeHtml(c.text) + '">' + bodyHtml + '</div>'
    +   '<div class="comment-actions">'
    +     '<button class="act-btn like-btn ' + likeCls + '" data-action="like-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '">' + heartIcon + '<span class="num">' + (c.likes || 0) + '</span></button>'
    +     replyBtn + editBtn + delBtn
    +   '</div>'
    + '</div>';
}

function applyLikeUI(btn) {
  var was = btn.classList.contains('active');
  var numEl = btn.querySelector('.num');
  var old = parseInt(numEl ? numEl.textContent : '0', 10) || 0;
  var newVal = !was;
  btn.classList.toggle('active', newVal);
  btn.innerHTML = (newVal ? ICONS.heart_filled : ICONS.heart) + '<span class="num">' + (old + (newVal ? 1 : -1)) + '</span>';
  return { old: old, was: was };
}
function revertLike(btn, snap) {
  btn.classList.toggle('active', snap.was);
  btn.innerHTML = (snap.was ? ICONS.heart_filled : ICONS.heart) + '<span class="num">' + snap.old + '</span>';
}

function startInlineEdit(container, textEl, initialText, onSave) {
  var editor = document.createElement('div');
  editor.className = 'inline-editor';
  editor.innerHTML = '<textarea></textarea>'
    + '<div class="edit-actions">'
    + '<button class="edit-cancel">' + escapeHtml(tr('cancel')) + '</button>'
    + '<button class="edit-save">' + escapeHtml(tr('save')) + '</button>'
    + '</div>';
  textEl.style.display = 'none';
  container.insertBefore(editor, textEl);
  var ta = editor.querySelector('textarea');
  ta.value = initialText; ta.focus();
  try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch(e) {}
  function close() { if (editor.parentNode) editor.parentNode.removeChild(editor); textEl.style.display = ''; }
  editor.querySelector('.edit-cancel').addEventListener('click', close);
  editor.querySelector('.edit-save').addEventListener('click', async function(){
    var v = ta.value.trim();
    if (!v) return;
    var b = editor.querySelector('.edit-save'); b.disabled = true;
    try {
      var res = await onSave(v);
      // Обновляем DOM без перезагрузки
      textEl.setAttribute('data-raw', v);
      textEl.innerHTML = linkifyText(v);
      close();
    }
    catch(e) { alert(tr(e.message) || e.message); b.disabled = false; }
  });
}

function bindPostActions(root) {
  root.querySelectorAll('[data-action]').forEach(function(btn){
    if (btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', async function(e){
      e.preventDefault();
      e.stopPropagation();
      var action = btn.dataset.action;
      var postId = btn.dataset.postId;
      var commentId = btn.dataset.commentId;
      if (action === 'like') {
        if (!state.user) { navigate('/login'); return; }
        var snap = applyLikeUI(btn);
        state.suppressRefresh = Date.now() + 2000;
        try { await api('/api/posts/' + postId + '/like', { method: 'POST', body: {} }); }
        catch(err) { revertLike(btn, snap); alert(tr(err.message) || err.message); }
        return;
      }
      if (action === 'like-comment') {
        if (!state.user) { navigate('/login'); return; }
        var snap2 = applyLikeUI(btn);
        state.suppressRefresh = Date.now() + 2000;
        try { await api('/api/posts/' + postId + '/comments/' + commentId + '/like', { method: 'POST', body: {} }); }
        catch(err) { revertLike(btn, snap2); alert(tr(err.message) || err.message); }
        return;
      }
      if (action === 'open-post') { navigate('/p/' + postId); return; }
      if (action === 'copy') {
        try { var pp = await api('/api/posts/' + postId); copyText(pp.text || ''); } catch(e) {}
        return;
      }
      if (action === 'quote') {
        if (!state.user) { navigate('/login'); return; }
        try {
          var p = await api('/api/posts/' + postId);
          state.quotePostId = postId;
          state.quotePreview = { author: p.author, text: p.text };
          if (state.view !== 'feed') { navigate('/'); return; }
          renderQuoteBox('post');
          var inp = document.getElementById('postInput');
          if (inp) { inp.value = ''; inp.focus(); autoGrow(inp); }
          var s = document.getElementById('postSend');
          var sm = document.getElementById('postSendMobile');
          if (s) s.disabled = false;
          if (sm) sm.disabled = false;
        } catch(e) { alert(tr(e.message) || e.message); }
        return;
      }
      if (action === 'reply') {
        state.replyTo = { id: commentId, author: btn.dataset.author || '' };
        if (state.view !== 'post') { navigate('/p/' + postId); return; }
        if (state._renderReplyBanner) state._renderReplyBanner();
        var ta = document.getElementById('commentInput');
        if (ta) ta.focus();
        return;
      }
      if (action === 'edit-post') {
        var postEl = btn.closest('.post-card');
        var textEl = postEl.querySelector('.post-text');
        if (!textEl) return;
        var raw = textEl.getAttribute('data-raw') || '';
        state.suppressRefresh = Date.now() + 2000;
        startInlineEdit(postEl, textEl, raw, async function(newText){
          await api('/api/posts/' + postId, { method: 'PUT', body: { text: newText } });
        });
        return;
      }
      if (action === 'delete-post') {
        showConfirm(tr('confirm_delete'), async function(){
          try {
            state.suppressRefresh = Date.now() + 2000;
            await api('/api/posts/' + postId, { method: 'DELETE' });
            var postEl = document.querySelector('[data-post-id="' + postId + '"]');
            if (postEl && postEl.parentNode) postEl.parentNode.removeChild(postEl);
          } catch(err) { alert(tr(err.message) || err.message); }
        }, { yesText: tr('confirm_delete_yes') });
        return;
      }
      if (action === 'edit-comment') {
        var cEl = btn.closest('.comment');
        var cTextEl = cEl.querySelector('.comment-text');
        var cRaw = cTextEl.getAttribute('data-raw') || '';
        state.suppressRefresh = Date.now() + 2000;
        startInlineEdit(cEl, cTextEl, cRaw, async function(newText){
          await api('/api/posts/' + postId + '/comments/' + commentId, { method: 'PUT', body: { text: newText } });
        });
        return;
      }
      if (action === 'delete-comment') {
        showConfirm(tr('confirm_delete'), async function(){
          try {
            state.suppressRefresh = Date.now() + 2000;
            await api('/api/posts/' + postId + '/comments/' + commentId, { method: 'DELETE' });
            var cEl = document.querySelector('[data-comment-id="' + commentId + '"]');
            if (cEl && cEl.parentNode) cEl.parentNode.removeChild(cEl);
            decrementCommentCount(postId);
          } catch(err) { alert(tr(err.message) || err.message); }
        }, { yesText: tr('confirm_delete_yes') });
        return;
      }
    });
  });
  root.querySelectorAll('[data-link]').forEach(function(a){
    if (a.dataset.linkBound) return;
    a.dataset.linkBound = '1';
    a.addEventListener('click', function(e){ e.preventDefault(); e.stopPropagation(); navigate(a.getAttribute('href')); });
  });
  bindOgImages(root);
}

async function refreshCurrentView() {
  if (state.view === 'feed') await loadFeed();
  else if (state.view === 'post') await loadPostView();
  else if (state.view === 'profile') await loadProfile(state.viewData.nick);
}

async function copyText(txt) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(txt);
    else {
      var ta = document.createElement('textarea');
      ta.value = txt; ta.style.position='fixed'; ta.style.opacity='0';
      document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
    }
  } catch(e) {}
}

(function init() {
  try {
    var savedSec = sessionStorage.getItem('SLD_set_sec');
    if (savedSec && ['account','privacy','appearance','info'].indexOf(savedSec) >= 0) {
      state.settingsSection = savedSec;
      sessionStorage.removeItem('SLD_set_sec');
    }
  } catch(e) {}
  if (state.user && (state.view === 'login' || state.view === 'register')) {
    history.replaceState({}, '', '/');
    state.view = 'feed'; state.viewData = {};
  }
  renderSidebar(); renderMain();
  updateRoom();
  connectSSE();
  loadMe().then(function(){
    if (state.user && (state.view === 'login' || state.view === 'register')) {
      history.replaceState({}, '', '/');
      state.view = 'feed'; state.viewData = {};
    }
    renderSidebar(); renderMain();
    if (state.user) { refreshCounters(); }
  });
})();
"""


def render_page(lang: str, view: str, view_data: Optional[dict] = None) -> str:
    t = TEXTS[lang]
    view_data = view_data or {}
    js = (JS
          .replace("__I_HOME__", json.dumps(I_HOME))
          .replace("__I_USERS__", json.dumps(I_USERS))
          .replace("__I_USER__", json.dumps(I_USER))
          .replace("__I_BELL__", json.dumps(I_BELL))
          .replace("__I_GEAR__", json.dumps(I_GEAR))
          .replace("__I_LOGOUT__", json.dumps(I_LOGOUT))
          .replace("__I_LOGIN__", json.dumps(I_LOGIN))
          .replace("__I_PLUS__", json.dumps(I_PLUS))
          .replace("__I_SEND__", json.dumps(I_SEND))
          .replace("__I_HEART__", json.dumps(I_HEART))
          .replace("__I_HEART_FILLED__", json.dumps(I_HEART_FILLED))
          .replace("__I_COMMENT__", json.dumps(I_COMMENT))
          .replace("__I_COPY__", json.dumps(I_COPY))
          .replace("__I_EDIT__", json.dumps(I_EDIT))
          .replace("__I_TRASH__", json.dumps(I_TRASH))
          .replace("__I_BACK__", json.dumps(I_BACK))
          .replace("__I_SEARCH__", json.dumps(I_SEARCH))
          .replace("__I_MOON__", json.dumps(I_MOON))
          .replace("__I_SUN__", json.dumps(I_SUN))
          .replace("__I_QUOTE__", json.dumps(I_QUOTE))
          .replace("__I_EYE__", json.dumps(I_EYE))
          .replace("__I_CAL__", json.dumps(I_CAL))
          .replace("__I_CHECK__", json.dumps(I_CHECK))
          .replace("__I_MOBILE__", json.dumps(I_MOBILE))
          .replace("__I_DESKTOP__", json.dumps(I_DESKTOP))
          .replace("__EMOJIS__", json.dumps(EMOJIS)))
    return ('<!DOCTYPE html>\n'
        f'<html lang="{lang}" data-theme="dark">\n'
        '<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />\n'
        '<meta name="color-scheme" content="dark light" />\n'
        '<meta name="theme-color" content="#0a0a0a" />\n'
        f'<link rel="icon" type="image/svg+xml" href="{FAVICON}" />\n'
        '<title>SLD</title>\n'
        '<style>' + CSS + '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="layout">\n'
        '  <aside class="sidebar" id="sidebar"></aside>\n'
        '  <main class="main" id="main"></main>\n'
        '</div>\n'
        '<script>\n'
        f'const LANG = {json.dumps(lang)};\n'
        f'const VIEW = {json.dumps(view)};\n'
        f'const VIEW_DATA = {json.dumps(view_data)};\n'
        f'const MAX_POST_LEN = {MAX_POST_LEN};\n'
        f'const MAX_COMMENT_LEN = {MAX_COMMENT_LEN};\n'
        f'const MAX_BIO_LEN = {MAX_BIO_LEN};\n'
        f'const TRUNCATE_LINES = {TRUNCATE_LINES};\n'
        f'const TRUNCATE_CHARS = {TRUNCATE_CHARS};\n'
        f'const T = {json.dumps(t, ensure_ascii=False)};\n'
        + js + '\n</script>\n</body>\n</html>')


def render_404_page(lang: str) -> str:
    t = TEXTS[lang]
    return ('<!DOCTYPE html>\n'
        f'<html lang="{lang}" data-theme="dark">\n'
        '<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />\n'
        '<meta name="color-scheme" content="dark light" />\n'
        f'<link rel="icon" type="image/svg+xml" href="{FAVICON}" />\n'
        '<title>404 — SLD</title>\n'
        '<style>' + CSS + '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="layout" style="justify-content:center;align-items:center;">'
        '<div class="not-found">'
        '<div class="not-found-code">404</div>'
        f'<div class="not-found-text">{t["page_not_found"]}</div>'
        f'<a class="pill-action primary" href="/">{t["back_home"]}</a>'
        '</div></div></body></html>')


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 404:
        return HTMLResponse(render_404_page(get_lang(request)), status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/", response_class=HTMLResponse)
def page_index(request: Request): return render_page(get_lang(request), "feed")

@app.get("/p/{post_id}", response_class=HTMLResponse)
def page_post(post_id: str, request: Request):
    if not db_get_post(post_id):
        raise HTTPException(404, "not found")
    return render_page(get_lang(request), "post", {"post_id": post_id})

@app.get("/u/{nick}", response_class=HTMLResponse)
def page_user(nick: str, request: Request):
    return render_page(get_lang(request), "profile", {"nick": nick})

@app.get("/u/{nick}/followers", response_class=HTMLResponse)
def page_followers(nick: str, request: Request):
    return render_page(get_lang(request), "followers", {"nick": nick})

@app.get("/u/{nick}/following", response_class=HTMLResponse)
def page_following(nick: str, request: Request):
    return render_page(get_lang(request), "following", {"nick": nick})

@app.get("/users", response_class=HTMLResponse)
def page_users(request: Request): return render_page(get_lang(request), "users")

@app.get("/notifications", response_class=HTMLResponse)
def page_notifications(request: Request): return render_page(get_lang(request), "notifications")

@app.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request): return render_page(get_lang(request), "settings")

@app.get("/settings/profile", response_class=HTMLResponse)
def page_edit_profile(request: Request): return render_page(get_lang(request), "edit_profile")

@app.get("/policy", response_class=HTMLResponse)
def page_policy(request: Request): return render_page(get_lang(request), "policy")

@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request): return render_page(get_lang(request), "register")

@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request): return render_page(get_lang(request), "login")
