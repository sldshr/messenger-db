# main.py
import os, time, uuid, json, hmac, hashlib, secrets, asyncio, re, urllib.request, logging, ipaddress
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sld")

START_TIME = time.time()
VERSION = "1.6.0"

app = FastAPI(title="SLD", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=800)

# ============================================================================
# SUPABASE
# ============================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("[SLD] Supabase connected")
    except Exception as e:
        log.error("[SLD] Supabase init error: %s", e)

# In-memory fallback (если Supabase недоступен)
USERS: Dict[str, dict] = {}
SESSIONS: Dict[str, dict] = {}
POSTS_MEM: Dict[str, dict] = {}
NOTIFS_MEM: Dict[str, List[dict]] = {}
_OG_CACHE: Dict[str, Optional[dict]] = {}
_NICK_FAILS: Dict[str, List[float]] = {}
_RATE: Dict[str, List[float]] = {}
_USER_CACHE: Dict[str, Tuple[float, dict]] = {}
_lang_cache: Dict[str, str] = {}
_geo_cache: Dict[str, dict] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=12)

MAX_POST_LEN = 5000
MAX_COMMENT_LEN = 1500
MAX_BIO_LEN = 300
FEED_LIMIT = 100
TRUNCATE_LINES = 15
TRUNCATE_CHARS = 800
SESSION_TTL = 30 * 24 * 3600
MAX_SESSIONS_PER_USER = 10
USER_CACHE_TTL = 60.0
SESSION_USER_TTL = 15.0
RATE_WINDOW = 60.0
SSE_HEARTBEAT_SEC = 12
MAX_BODY_BYTES = 1_000_000
NICK_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
MENTION_RE = re.compile(r"(?<![a-zA-Z0-9_])@([a-zA-Z0-9_]{3,20})")
URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')
RU_COUNTRIES = {"RU","BY","KZ","UA","KG","TJ","UZ","AM","AZ","MD"}
RESERVED_NICKS = {"admin","root","system","moderator","mod","sld","слд","support","help","api","null","undefined","me","user","users","login","register","settings","policy","notifications","p","u","static","assets"}
DEFAULT_EMOJI = "😀"
_DUMMY_HASH = None


def _parallel(*fns):
    if not fns: return []
    if len(fns) == 1: return [fns[0]()]
    futures = [_EXECUTOR.submit(f) for f in fns]
    out = []
    for fu in futures:
        try: out.append(fu.result())
        except Exception as e:
            log.warning("[parallel] %s", e); out.append(None)
    return out


EMOJI_DATA: List[Tuple[str, str, str]] = [
    ("😀","Улыбашка","Grinning"),("😃","Улыбка с глазами","Big smile"),
    ("😄","Смех","Laughing"),("😁","Сияющая улыбка","Beaming"),
    ("😆","Хохот","Grinning squint"),("😅","Улыбка с потом","Nervous laugh"),
    ("😂","Слёзы смеха","Tears of joy"),("😊","Смущённая улыбка","Blush"),
    ("😇","Ангел","Angel"),("🙂","Лёгкая улыбка","Slight smile"),
    ("🙃","Перевёрнутая улыбка","Upside down"),("😉","Подмигивание","Wink"),
    ("😌","Спокойствие","Relieved"),("😍","Влюблённые глаза","Heart eyes"),
    ("😘","Воздушный поцелуй","Kiss"),("😎","Крутой","Cool"),
    ("😏","Ухмылка","Smirk"),("😒","Недовольство","Unamused"),
    ("😞","Разочарование","Disappointed"),("😔","Печаль","Pensive"),
    ("😢","Слеза","Crying"),("😭","Громкий плач","Sobbing"),
    ("😡","Ярость","Rage"),("😱","Крик","Scream"),
    ("🤗","Обнимашки","Hug"),("🤔","Размышление","Thinking"),
    ("😐","Нейтрально","Neutral"),("🙄","Закатывает глаза","Eye roll"),
    ("😴","Сон","Sleeping"),("🤖","Робот","Robot"),
    ("🎃","Тыква","Pumpkin"),("😺","Кот улыбается","Grinning cat"),
    ("😻","Кот влюблён","Heart eyes cat"),("😿","Кот плачет","Crying cat"),
    ("🐶","Собака","Dog"),("🐱","Кошка","Cat"),
    ("🐭","Мышь","Mouse"),("🐹","Хомяк","Hamster"),
    ("🐰","Кролик","Rabbit"),("🐻","Медведь","Bear"),
    ("🐼","Панда","Panda"),("🐨","Коала","Koala"),
    ("🐯","Тигр","Tiger"),("🦊","Лиса","Fox"),
    ("🐮","Корова","Cow"),("🐷","Свинья","Pig"),
    ("🐸","Лягушка","Frog"),("🐵","Обезьяна","Monkey"),
    ("🐧","Пингвин","Penguin"),("🦉","Сова","Owl"),
    ("🦄","Единорог","Unicorn"),("🐝","Пчела","Bee"),
    ("🦋","Бабочка","Butterfly"),("🐢","Черепаха","Turtle"),
    ("🐙","Осьминог","Octopus"),("🐬","Дельфин","Dolphin"),
    ("🐳","Кит","Whale"),("🌵","Кактус","Cactus"),
    ("🎄","Ёлка","Christmas tree"),("🌲","Сосна","Evergreen"),
    ("🌳","Дерево","Tree"),("🌴","Пальма","Palm"),
    ("🌱","Росток","Seedling"),("🍀","Клевер","Clover"),
    ("🍁","Кленовый лист","Maple leaf"),("🌷","Тюльпан","Tulip"),
    ("🌹","Роза","Rose"),("🌸","Сакура","Blossom"),
    ("🌻","Подсолнух","Sunflower"),("💐","Букет","Bouquet"),
    ("🌞","Солнце с лицом","Sun with face"),("🌙","Полумесяц","Crescent"),
    ("⭐","Звезда","Star"),("🌟","Сияющая звезда","Glowing star"),
    ("✨","Искры","Sparkles"),("⚡","Молния","Lightning"),
    ("🔥","Огонь","Fire"),("💥","Взрыв","Collision"),
    ("🌈","Радуга","Rainbow"),("💧","Капля","Droplet"),
    ("🌊","Волна","Wave"),("🍎","Яблоко","Apple"),
    ("🍊","Мандарин","Tangerine"),("🍋","Лимон","Lemon"),
    ("🍌","Банан","Banana"),("🍉","Арбуз","Watermelon"),
    ("🍇","Виноград","Grapes"),("🍓","Клубника","Strawberry"),
    ("🍒","Вишня","Cherries"),("🍑","Персик","Peach"),
    ("🍍","Ананас","Pineapple"),("🍅","Помидор","Tomato"),
    ("🍔","Бургер","Burger"),("🍟","Картошка фри","Fries"),
    ("🍕","Пицца","Pizza"),("🍜","Лапша","Noodles"),
    ("🍣","Суши","Sushi"),("🍦","Мороженое","Ice cream"),
    ("🍩","Пончик","Donut"),("🍪","Печенье","Cookie"),
    ("🎂","Торт","Birthday cake"),("🍰","Кусок торта","Shortcake"),
    ("🍫","Шоколад","Chocolate"),("🍬","Конфета","Candy"),
    ("🍺","Пиво","Beer"),("☕","Кофе","Coffee"),
    ("🚀","Ракета","Rocket"),("✈️","Самолёт","Airplane"),
    ("🚗","Машина","Car"),("🏠","Дом","House"),
    ("🎮","Геймпад","Gamepad"),("🎧","Наушники","Headphones"),
    ("🎸","Гитара","Guitar"),("🎨","Палитра","Palette"),
    ("📚","Книги","Books"),("💻","Ноутбук","Laptop"),
    ("📱","Телефон","Phone"),("⌚","Часы","Watch"),
    ("💎","Алмаз","Diamond"),("🎁","Подарок","Gift"),
    ("💡","Идея","Idea"),("🏆","Кубок","Trophy"),
    ("⚽","Футбол","Soccer"),("🏀","Баскетбол","Basketball"),
    ("🎯","Мишень","Target"),("🎲","Кубик","Dice"),
    ("🧩","Пазл","Puzzle"),("🕹️","Джойстик","Joystick"),
]
EMOJIS = [e[0] for e in EMOJI_DATA]


# ============================================================================
# SECURITY middleware
# ============================================================================
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "payload_too_large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "bad_request"}, status_code=400)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    ct = response.headers.get("content-type", "")
    if ct.startswith("text/html"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: https: http:; "
            "font-src 'self'; connect-src 'self'; media-src 'none'; "
            "object-src 'none'; frame-src 'none'; worker-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
    return response


# ============================================================================
# EventBus (SSE)
# ============================================================================
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
    global MAIN_LOOP, _DUMMY_HASH
    MAIN_LOOP = asyncio.get_running_loop()
    _DUMMY_HASH = hash_password("dummy_password_for_constant_time_check")


def _run_sync(fn, *args):
    if MAIN_LOOP and not MAIN_LOOP.is_closed():
        try: MAIN_LOOP.call_soon_threadsafe(fn, *args); return
        except RuntimeError: pass
    fn(*args)


def bus_publish(nick: str, ev: dict) -> None:
    if not nick: return
    _run_sync(bus._deliver, nick, ev)


def bus_broadcast(room: str, ev: dict, except_nick: Optional[str] = None) -> None:
    if not room: return
    _run_sync(bus.broadcast_room, room, ev, except_nick)


def broadcast_post_change(post: dict, except_nick: Optional[str] = None, feed: bool = False) -> None:
    if not post: return
    pid = post.get("id")
    if pid: bus_broadcast("post:" + pid, {"type": "refresh"}, except_nick=except_nick)
    author = post.get("author")
    if author: bus_broadcast("profile:" + author, {"type": "refresh"}, except_nick=except_nick)
    if feed: bus_broadcast("feed", {"type": "refresh"}, except_nick=except_nick)


# ============================================================================
# Utils
# ============================================================================
def _is_private_host(host: str) -> bool:
    if not host: return True
    h = host.strip().lower().strip("[]")
    if h in ("localhost","localhost.localdomain","ip6-localhost","ip6-loopback"): return True
    if h.endswith(".local") or h.endswith(".internal") or h.endswith(".lan"): return True
    if h in ("metadata.google.internal","metadata.google.com","metadata"): return True
    try:
        ip = ipaddress.ip_address(h)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified): return True
        if str(ip).startswith("169.254."): return True
        return False
    except ValueError:
        return False


def _safe_url(u) -> str:
    if not u or not isinstance(u, str): return ""
    u = u.strip()
    if not u or len(u) > 500: return ""
    try: p = urlparse(u)
    except Exception: return ""
    if p.scheme not in ("http","https"): return ""
    if not p.netloc: return ""
    if _is_private_host(p.hostname or ""): return ""
    return u


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


def check_password_constant_time(password: str, stored: Optional[str]) -> bool:
    global _DUMMY_HASH
    if not stored:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password("dummy_password_for_constant_time_check")
        check_password(password, _DUMMY_HASH)
        return False
    return check_password(password, stored)


def validate_password(pw: str) -> str:
    if len(pw) < 8: return "err_short_pass"
    if len(pw) > 200: return "err_short_pass"
    if not re.search(r"[A-Za-z]", pw): return "err_pass_weak"
    if not re.search(r"\d", pw): return "err_pass_weak"
    return ""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def create_session(nick: str, user: Optional[dict] = None) -> str:
    existing = [(t, s.get("created", 0)) for t, s in SESSIONS.items() if s.get("nick") == nick]
    if len(existing) >= MAX_SESSIONS_PER_USER:
        existing.sort(key=lambda x: x[1])
        for t, _ in existing[:len(existing) - MAX_SESSIONS_PER_USER + 1]:
            SESSIONS.pop(t, None)
    token = new_token()
    SESSIONS[token] = {"nick": nick, "created": time.time(),
                       "user": user, "user_ts": time.time()}
    return token


def rate_limit(key: str, max_req: int, window: float = RATE_WINDOW) -> bool:
    now = time.time()
    arr = [t for t in _RATE.get(key, []) if now - t < window]
    if len(arr) >= max_req:
        _RATE[key] = arr; return False
    arr.append(now); _RATE[key] = arr; return True


def _nick_login_ok(nick: str) -> bool:
    now = time.time()
    arr = [t for t in _NICK_FAILS.get(nick.lower(), []) if now - t < 900]
    if len(arr) >= 10:
        _NICK_FAILS[nick.lower()] = arr; return False
    return True


def _nick_login_fail(nick: str) -> None:
    arr = _NICK_FAILS.get(nick.lower(), [])
    arr.append(time.time())
    _NICK_FAILS[nick.lower()] = arr[-10:]


def _nick_login_ok_clear(nick: str) -> None:
    _NICK_FAILS.pop(nick.lower(), None)


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
    markers = ("mobile","android","iphone","ipod","ipad","windows phone",
               "webos","blackberry","opera mini")
    return "mobile" if any(x in ua_l for x in markers) else "desktop"


def fetch_og_data(url: str) -> Optional[dict]:
    if not url: return None
    safe = _safe_url(url)
    if not safe: return None
    url = safe
    if url in _OG_CACHE: return _OG_CACHE[url]
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; SLDbot/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        })
        class _NoPrivateRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                if not _safe_url(newurl): return None
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        opener = urllib.request.build_opener(_NoPrivateRedirect())
        with opener.open(req, timeout=4) as r:
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
        "url": _safe_url(url),
        "title": (og("title") or "").strip()[:200],
        "description": (og("description") or "").strip()[:300],
        "image": _safe_url((og("image") or "").strip()[:500]),
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
            or ip.startswith("172.") or ip in ("::1","localhost"))


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
        result = {"city": data.get("city","") or "",
                  "country": data.get("country","") or "",
                  "country_code": (data.get("countryCode") or "").upper()}
        _geo_cache[ip] = result; return result
    except Exception:
        _geo_cache[ip] = {}; return {}


def detect_lang(ip: str, accept_language: str) -> str:
    key = ip or "unknown"
    if key in _lang_cache: return _lang_cache[key]
    lang = None
    geo = get_geo(ip)
    cc = geo.get("country_code","")
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
    now = time.time()
    cached = sess.get("user")
    cached_ts = sess.get("user_ts", 0)
    if cached and now - cached_ts < SESSION_USER_TTL:
        return cached
    u = db_load_user_cached(nick)
    if u:
        sess["user"] = u; sess["user_ts"] = now
    return u


def require_user(request: Request) -> dict:
    u = get_current_user(request)
    if not u: raise HTTPException(401, "unauthorized")
    return u


def invalidate_user_cache(nick: Optional[str] = None) -> None:
    if nick is None: _USER_CACHE.clear()
    else: _USER_CACHE.pop(nick.lower(), None)
    for s in SESSIONS.values():
        if nick is None or (s.get("nick") or "").lower() == nick.lower():
            s.pop("user", None); s.pop("user_ts", None)


def clean_emoji(s: Optional[str]) -> str:
    if not s: return ""
    return s.strip()[:8]


NOTIFY_FIELDS = [
    "notify_on_new_post","notify_on_follow","notify_on_comment",
    "notify_on_reply","notify_on_mention","notify_on_quote",
]
NOTIFY_TYPE_TO_FIELD = {
    "new_post":"notify_on_new_post","follow":"notify_on_follow",
    "comment":"notify_on_comment","reply":"notify_on_reply",
    "mention":"notify_on_mention","quote":"notify_on_quote",
}
USER_BOOL_DEFAULTS = {
    "notify_on_new_post":True,"notify_on_follow":True,"notify_on_comment":True,
    "notify_on_reply":True,"notify_on_mention":True,"notify_on_quote":True,
    "show_link_previews":True,"allow_followers_view":True,"allow_following_view":True,
    "show_device_badge":True,
}


# ============================================================================
# DB LAYER (Supabase + memory fallback)
# ============================================================================
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
        except Exception as e: log.error("db_load_user: %s", e)
        return None
    return USERS.get(nick.lower())


def db_load_user_cached(nick: str) -> Optional[dict]:
    now = time.time()
    e = _USER_CACHE.get(nick.lower())
    if e and now - e[0] < USER_CACHE_TTL: return e[1]
    u = db_load_user(nick)
    if u is not None: _USER_CACHE[nick.lower()] = (now, u)
    return u


def db_load_users_batch(nicks: List[str]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    missing, seen = [], set()
    now = time.time()
    for n in nicks:
        if not n: continue
        ln = n.lower()
        if ln in seen: continue
        seen.add(ln)
        e = _USER_CACHE.get(ln)
        if e and now - e[0] < USER_CACHE_TTL: out[ln] = e[1]
        else: missing.append(n)
    if not missing: return out
    if supabase:
        try:
            r = supabase.table("users").select("*").in_("nick", list(set(missing))).execute()
            for row in (r.data or []):
                u = _norm_user_row(row)
                out[u["nick"].lower()] = u
                _USER_CACHE[u["nick"].lower()] = (now, u)
        except Exception as e: log.error("db_load_users_batch: %s", e)
    else:
        for n in missing:
            u = USERS.get(n.lower())
            if u: out[u["nick"].lower()] = u
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
    if not supabase:
        USERS[u["nick"].lower()] = u; invalidate_user_cache(u["nick"]); return
    try: supabase.table("users").upsert(_user_payload(u)).execute()
    except Exception as e: log.error("db_save_user: %s", e)
    invalidate_user_cache(u["nick"])


def db_update_user_fields(nick: str, patch: dict) -> None:
    if not supabase:
        u = USERS.get(nick.lower())
        if u: u.update(patch)
        invalidate_user_cache(nick); return
    try: supabase.table("users").update(patch).eq("nick", nick).execute()
    except Exception as e: log.error("db_update_user_fields: %s", e)
    invalidate_user_cache(nick)


def db_all_users() -> List[dict]:
    if supabase:
        try:
            r = supabase.table("users").select("*").execute()
            return [_norm_user_row(row) for row in (r.data or [])]
        except Exception as e: log.error("db_all_users: %s", e); return []
    return list(USERS.values())


def db_create_post(p: dict) -> None:
    if not supabase:
        POSTS_MEM[p["id"]] = p; return
    payload = {
        "id": p["id"], "text": p["text"], "author": p["author"],
        "created_at": ts_to_iso(p["created_at"]),
        "quoted_post_id": p.get("quoted_post_id"),
        "og_data": p.get("og_data"),
    }
    if p.get("device"): payload["device"] = p["device"]
    try: supabase.table("posts").insert(payload).execute()
    except Exception as e:
        log.error("db_create_post: %s", e)
        payload.pop("device", None)
        try: supabase.table("posts").insert(payload).execute()
        except Exception as e2: log.error("db_create_post retry: %s", e2)


def db_update_post_text(pid: str, text: str) -> None:
    if not supabase:
        p = POSTS_MEM.get(pid)
        if p: p["text"] = text
        return
    try: supabase.table("posts").update({"text": text}).eq("id", pid).execute()
    except Exception as e: log.error("db_update_post: %s", e)


def db_delete_post(pid: str) -> None:
    if not supabase:
        POSTS_MEM.pop(pid, None); return
    try:
        supabase.table("notifications").delete().eq("post_id", pid).execute()
        supabase.table("posts").delete().eq("id", pid).execute()
    except Exception as e: log.error("db_delete_post: %s", e)


def db_get_post(pid: str) -> Optional[dict]:
    if supabase:
        try:
            r = supabase.table("posts").select("*").eq("id", pid).limit(1).execute()
            if not r.data: return None
            row = r.data[0]
            row["created_at"] = iso_to_ts(row.get("created_at"))
            return row
        except Exception as e: log.error("db_get_post: %s", e); return None
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
    except Exception as e: log.error("db_inc_views: %s", e)
    return 0


def view_should_count(pid: str, vid: str) -> bool:
    if not supabase: return True
    try:
        r = (supabase.table("post_views").select("last_viewed_at")
             .eq("post_id", pid).eq("viewer_id", vid).limit(1).execute())
        if r.data:
            last_ts = iso_to_ts(r.data[0].get("last_viewed_at"))
            if time.time() - last_ts < 8 * 3600: return False
    except Exception as e: log.error("view_should_count: %s", e)
    return True


def view_mark_counted(pid: str, vid: str) -> None:
    if not supabase: return
    try:
        supabase.table("post_views").upsert({
            "post_id": pid, "viewer_id": vid,
            "last_viewed_at": ts_to_iso(time.time())}).execute()
    except Exception as e: log.error("view_mark: %s", e)


def db_get_quotes(post_ids: List[str]) -> Dict[str, dict]:
    out = {}
    if not post_ids: return out
    if supabase:
        try:
            r = supabase.table("posts").select("*").in_("id", post_ids).execute()
            for row in r.data or []:
                row["created_at"] = iso_to_ts(row.get("created_at"))
                out[row["id"]] = row
        except Exception as e: log.error("db_get_quotes: %s", e)
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
        except Exception as e: log.error("db_list_posts: %s", e); return []
    items = list(POSTS_MEM.values())
    if author: items = [p for p in items if (p.get("author") or "").lower() == author.lower()]
    if subscriptions_of:
        u = db_load_user_cached(subscriptions_of)
        fol = set(x.lower() for x in (u.get("following") or set())) if u else set()
        items = [p for p in items if (p.get("author") or "").lower() in fol]
    if q:
        n = q.lower()
        items = [p for p in items if n in (p.get("text") or "").lower()]
    items.sort(key=lambda p: p.get("created_at", 0), reverse=True)
    return items[:FEED_LIMIT]


def db_post_votes(post_ids: List[str]) -> List[dict]:
    if not post_ids: return []
    if supabase:
        try:
            return supabase.table("post_votes").select("*").in_("post_id", post_ids).execute().data or []
        except Exception as e: log.error("db_post_votes: %s", e); return []
    out = []
    for pid in post_ids:
        p = POSTS_MEM.get(pid)
        if p:
            for vid, d in (p.get("votes") or {}).items():
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
        except Exception as e: log.error("db_set_post_vote: %s", e)
        return
    p = POSTS_MEM.get(post_id)
    if not p: return
    p.setdefault("votes", {})
    if direction == 0: p["votes"].pop(voter_id, None)
    else: p["votes"][voter_id] = direction


def db_comment_counts(post_ids: List[str]) -> Dict[str, int]:
    if not post_ids: return {}
    if supabase:
        try:
            r = supabase.table("comments").select("post_id").in_("post_id", post_ids).execute()
            out: Dict[str, int] = {}
            for row in r.data or []:
                pid = row.get("post_id")
                if pid: out[pid] = out.get(pid, 0) + 1
            return out
        except Exception as e: log.error("db_comment_counts: %s", e); return {}
    out = {}
    for pid in post_ids:
        p = POSTS_MEM.get(pid)
        if p: out[pid] = len(p.get("comments") or [])
    return out


def db_comments_for_posts(post_ids: List[str]) -> List[dict]:
    if not post_ids: return []
    if supabase:
        try:
            rows = (supabase.table("comments").select("*").in_("post_id", post_ids)
                    .order("created_at").execute().data or [])
            for row in rows: row["created_at"] = iso_to_ts(row.get("created_at"))
            return rows
        except Exception as e: log.error("db_comments_for_posts: %s", e); return []
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
        except Exception as e: log.error("db_comment_votes: %s", e); return []
    out = []
    for pid, p in POSTS_MEM.items():
        for c in p.get("comments", []):
            if c["id"] in comment_ids:
                for vid, d in (c.get("votes") or {}).items():
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
    except Exception as e: log.error("db_create_comment: %s", e)


def db_update_comment_text(cid: str, text: str) -> None:
    if not supabase:
        for p in POSTS_MEM.values():
            for c in p.get("comments", []):
                if c["id"] == cid: c["text"] = text; return
        return
    try: supabase.table("comments").update({"text": text}).eq("id", cid).execute()
    except Exception as e: log.error("db_update_comment: %s", e)


def db_delete_comment(cid: str) -> None:
    if not supabase:
        for p in POSTS_MEM.values():
            comments = p.get("comments", [])
            if not comments: continue
            to_del = {cid}
            for c in comments:
                if c.get("parent_id") == cid: to_del.add(c["id"])
            p["comments"] = [c for c in comments if c["id"] not in to_del]
        return
    try:
        supabase.table("notifications").delete().eq("comment_id", cid).execute()
        supabase.table("comments").delete().eq("parent_id", cid).execute()
        supabase.table("comments").delete().eq("id", cid).execute()
    except Exception as e: log.error("db_delete_comment: %s", e)


def db_get_comment(cid: str) -> Optional[dict]:
    if supabase:
        try:
            r = supabase.table("comments").select("*").eq("id", cid).limit(1).execute()
            if not r.data: return None
            row = r.data[0]
            row["created_at"] = iso_to_ts(row.get("created_at"))
            return row
        except Exception as e: log.error("db_get_comment: %s", e); return None
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
        except Exception as e: log.error("db_set_comment_vote: %s", e)
        return
    for p in POSTS_MEM.values():
        for c in p.get("comments", []):
            if c["id"] == cid:
                c.setdefault("votes", {})
                if direction == 0: c["votes"].pop(voter_id, None)
                else: c["votes"][voter_id] = direction
                return


def _should_notify(to_nick: str, ntype: str, from_nick: str,
                   users_map: Optional[Dict[str, dict]] = None) -> bool:
    if not to_nick or to_nick.lower() == (from_nick or "").lower(): return False
    field = NOTIFY_TYPE_TO_FIELD.get(ntype)
    if not field: return True
    u = None
    if users_map is not None: u = users_map.get(to_nick.lower())
    else: u = db_load_user_cached(to_nick)
    if u is None: return True
    return bool(u.get(field, True))


def db_notify_many(notifications: List[dict], users_map: Optional[Dict[str, dict]] = None) -> None:
    if not notifications: return
    if users_map is None:
        to_nicks = list(set(n["to_nick"] for n in notifications if n.get("to_nick")))
        users_map = db_load_users_batch(to_nicks)
    valid = []
    for n in notifications:
        if not _should_notify(n["to_nick"], n["ntype"], n["from_nick"], users_map): continue
        valid.append({
            "id": uuid.uuid4().hex[:10],
            "to_nick": n["to_nick"], "type": n["ntype"],
            "from_nick": n["from_nick"],
            "post_id": n.get("post_id","") or "",
            "comment_id": n.get("comment_id") or None,
            "text": n.get("text","") or "",
            "read": False,
            "created_at": ts_to_iso(time.time()),
        })
    if not valid: return
    if supabase:
        try: supabase.table("notifications").insert(valid).execute()
        except Exception as e: log.error("db_notify_many: %s", e)
    else:
        for row in valid:
            NOTIFS_MEM.setdefault(row["to_nick"], []).append({**row, "created_at": time.time()})
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
        except Exception as e: log.error("db_notifications: %s", e); return []
    items = list(NOTIFS_MEM.get(nick, []))
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items[:100]


def db_notifications_unread_count(nick: str) -> int:
    if supabase:
        try:
            r = (supabase.table("notifications").select("id", count="exact")
                 .eq("to_nick", nick).eq("read", False).limit(1).execute())
            return r.count or 0
        except Exception as e: log.error("db_notif_unread: %s", e); return 0
    return sum(1 for n in NOTIFS_MEM.get(nick, []) if not n.get("read"))


def db_notifications_mark_read(nick: str) -> None:
    if supabase:
        try: supabase.table("notifications").update({"read": True}).eq("to_nick", nick).eq("read", False).execute()
        except Exception as e: log.error("db_notif_read: %s", e)
        return
    for n in NOTIFS_MEM.get(nick, []): n["read"] = True


def db_notifications_clear(nick: str) -> None:
    if supabase:
        try: supabase.table("notifications").delete().eq("to_nick", nick).execute()
        except Exception as e: log.error("db_notif_clear: %s", e)
        return
    NOTIFS_MEM[nick] = []


def _rename_user_everywhere(old_nick: str, new_nick: str) -> None:
    if not supabase:
        old = USERS.pop(old_nick.lower(), None)
        if old:
            old["nick"] = new_nick
            USERS[new_nick.lower()] = old
        for p in POSTS_MEM.values():
            if (p.get("author") or "").lower() == old_nick.lower(): p["author"] = new_nick
            for c in p.get("comments", []):
                if (c.get("author") or "").lower() == old_nick.lower(): c["author"] = new_nick
        for u in USERS.values():
            fl = u.get("following") or set()
            if old_nick.lower() in {x.lower() for x in fl}:
                u["following"] = set(new_nick if x.lower() == old_nick.lower() else x for x in fl)
            fw = u.get("followers") or set()
            if old_nick.lower() in {x.lower() for x in fw}:
                u["followers"] = set(new_nick if x.lower() == old_nick.lower() else x for x in fw)
        old_items = NOTIFS_MEM.pop(old_nick, None)
        if old_items:
            new_items = NOTIFS_MEM.setdefault(new_nick, [])
            for n in old_items: n["to_nick"] = new_nick; new_items.append(n)
        for items in NOTIFS_MEM.values():
            for n in items:
                if (n.get("from_nick") or "").lower() == old_nick.lower(): n["from_nick"] = new_nick
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
            if old_nick.lower() in {x.lower() for x in fl}:
                patch["following"] = [new_nick if x.lower() == old_nick.lower() else x for x in fl]
            fw = u.get("followers") or set()
            if old_nick.lower() in {x.lower() for x in fw}:
                patch["followers"] = [new_nick if x.lower() == old_nick.lower() else x for x in fw]
            if patch: db_update_user_fields(u["nick"], patch)
    except Exception as e: log.error("_rename_user_everywhere: %s", e)
    invalidate_user_cache()


def normalize_og(raw):
    if not raw: return None
    if not isinstance(raw, dict): return None
    url = _safe_url(raw.get("url"))
    if not url: return None
    return {
        "url": url,
        "title": str(raw.get("title") or "")[:200],
        "description": str(raw.get("description") or "")[:300],
        "image": _safe_url(raw.get("image")),
        "site_name": str(raw.get("site_name") or "")[:100],
    }


def build_posts_full(posts: List[dict], voter_id: str, with_comments: bool = True) -> List[dict]:
    if not posts: return []
    post_ids = [p["id"] for p in posts]
    author_nicks = set(p.get("author") for p in posts if p.get("author"))
    quoted_ids = [p.get("quoted_post_id") for p in posts if p.get("quoted_post_id")]

    tasks = [
        (lambda: db_get_quotes(list(set(quoted_ids))) if quoted_ids else {}),
        (lambda: db_post_votes(post_ids)),
        (lambda: db_comments_for_posts(post_ids) if with_comments else []),
        (lambda: db_comment_counts(post_ids) if not with_comments else {}),
    ]
    quotes, votes, comments, ccounts = _parallel(*tasks)
    quotes = quotes or {}; votes = votes or []; comments = comments or []; ccounts = ccounts or {}
    for q in quotes.values():
        if q.get("author"): author_nicks.add(q["author"])

    users_map = db_load_users_batch(list(author_nicks))

    vmap: Dict[str, Dict[str, int]] = {}
    for v in votes: vmap.setdefault(v["post_id"], {})[v["voter_id"]] = v["direction"]

    cmap: Dict[str, List[dict]] = {}
    for c in comments: cmap.setdefault(c["post_id"], []).append(c)
    comment_ids = [c["id"] for c in comments]
    cvmap: Dict[str, Dict[str, int]] = {}
    if comment_ids:
        cvotes = db_comment_votes(comment_ids)
        for v in cvotes: cvmap.setdefault(v["comment_id"], {})[v["voter_id"]] = v["direction"]

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
        else: comment_count = ccounts.get(p["id"], 0)

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
            "comments": clist, "comment_count": comment_count,
        })
    return out


def serialize_user(u: dict, viewer_nick: Optional[str] = None) -> dict:
    is_me = bool(viewer_nick) and viewer_nick.lower() == (u.get("nick") or "").lower()
    d = {"nick": u["nick"], "name": u["name"], "bio": u.get("bio") or "",
         "created_at": u["created_at"],
         "followers": len(u.get("followers") or []),
         "following": len(u.get("following") or []),
         "is_me": is_me,
         "avatar_emoji": u.get("avatar_emoji", DEFAULT_EMOJI) or DEFAULT_EMOJI}
    if is_me:
        d["allow_followers_view"] = u.get("allow_followers_view", True)
        d["allow_following_view"] = u.get("allow_following_view", True)
        d["show_link_previews"] = u.get("show_link_previews", True)
        d["show_device_badge"] = u.get("show_device_badge", True)
        for f in NOTIFY_FIELDS: d[f] = bool(u.get(f, True))
    return d


# ============================================================================
# Pydantic
# ============================================================================
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


# ============================================================================
# UI-API manifest
# ============================================================================
UI_MANIFEST = {
    "version": VERSION,
    "features": {
        "og_previews": True, "device_badge": True, "sound": True,
        "link_previews": True, "quote": True, "notifications_sound": True,
        "views": True,
    },
    "screens": ["feed","post","profile","users","notifications","settings","policy","followers","following"],
    "nav": [
        {"id":"home","icon":"home","route":"/","i18n":"nav_home","auth":True,"order":10},
        {"id":"users","icon":"users","route":"/users","i18n":"nav_users","auth":True,"order":20},
        {"id":"notifs","icon":"bell","route":"/notifications","i18n":"nav_notifications","auth":True,"order":30,"badge":"notif"},
        {"id":"profile","icon":"user","route":"/u/{nick}","i18n":"nav_profile","auth":True,"order":40},
        {"id":"settings","icon":"gear","route":"/settings","i18n":"nav_settings","auth":True,"order":50},
    ],
    "auth_nav": [
        {"id":"login","icon":"login","route":"/login","i18n":"nav_login","auth":False,"order":10},
        {"id":"register","icon":"plus","route":"/register","i18n":"nav_register","auth":False,"order":20},
    ],
}


@app.get("/api/ui/manifest")
def api_ui_manifest():
    return UI_MANIFEST


# ============================================================================
# API: auth
# ============================================================================
@app.get("/api/check_nick")
def api_check_nick(nick: str, request: Request):
    ip = get_client_ip(request)
    if not rate_limit("chknick:" + ip, 90, 60): raise HTTPException(429, "err_rate_limit")
    n = (nick or "").strip().lstrip("@")
    if not NICK_RE.match(n): return {"available": False, "reason": "err_bad_nick"}
    if n.lower() in RESERVED_NICKS: return {"available": False, "reason": "err_nick_reserved"}
    if db_load_user(n): return {"available": False, "reason": "err_nick_taken"}
    return {"available": True, "reason": ""}


@app.post("/api/room")
def api_room(data: RoomIn, request: Request):
    u = get_current_user(request)
    if u: bus.set_rooms(u["nick"], data.rooms or []); return {"ok": True}
    if data.anon_id:
        nick = "anon:" + re.sub(r"[^a-zA-Z0-9]", "", data.anon_id)[:32]
        if nick != "anon:":
            bus.set_rooms(nick, data.rooms or ["feed"])
        return {"ok": True}
    return {"ok": True}


@app.get("/api/uptime")
def api_uptime(request: Request):
    up = time.time() - START_TIME
    return {"uptime_seconds": int(up), "started_at": ts_to_iso(START_TIME),
            "version": VERSION, "db": "supabase" if supabase else "memory"}


@app.post("/api/register")
def api_register(data: RegisterIn, request: Request):
    ip = get_client_ip(request)
    if not rate_limit("reg:" + ip, 5, 3600): raise HTTPException(429, "err_rate_limit")
    name = data.name.strip()
    nick = data.nick.strip().lstrip("@")
    if not NICK_RE.match(nick): raise HTTPException(400, "err_bad_nick")
    if nick.lower() in RESERVED_NICKS: raise HTTPException(400, "err_nick_reserved")
    if len(name) < 1 or len(name) > 50: raise HTTPException(400, "err_bad_name")
    perr = validate_password(data.password)
    if perr: raise HTTPException(400, perr)
    if data.password != data.password_confirm: raise HTTPException(400, "err_pass_mismatch")
    if db_load_user(nick): raise HTTPException(400, "err_nick_taken")
    u = {"nick": nick, "name": name, "bio": "",
         "password": hash_password(data.password), "created_at": time.time(),
         "following": set(), "followers": set(),
         "allow_followers_view": True, "allow_following_view": True,
         "avatar_emoji": DEFAULT_EMOJI, "show_link_previews": True, "show_device_badge": True}
    for f in NOTIFY_FIELDS: u[f] = True
    db_save_user(u)
    token = create_session(nick, u)
    log.info("[reg] %s from %s", nick, ip)
    return {"token": token, "user": serialize_user(u, nick)}


@app.post("/api/login")
def api_login(data: LoginIn, request: Request):
    ip = get_client_ip(request)
    nick = data.nick.strip().lstrip("@")
    if not rate_limit("log:" + ip, 10, 300): raise HTTPException(429, "err_rate_limit")
    if not _nick_login_ok(nick): raise HTTPException(429, "err_rate_limit")
    u = db_load_user(nick)
    stored = u["password"] if u else None
    ok = check_password_constant_time(data.password, stored)
    if not u or not ok:
        _nick_login_fail(nick)
        log.info("[login-fail] %s from %s", nick, ip)
        raise HTTPException(400, "err_bad_login")
    _nick_login_ok_clear(nick)
    token = create_session(u["nick"], u)
    log.info("[login] %s from %s", u["nick"], ip)
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
    ua = request.headers.get("user-agent","")
    ip = get_client_ip(request)
    browser, os_name = parse_user_agent(ua)
    geo = get_geo(ip)
    return {"browser": browser, "os": os_name,
            "city": geo.get("city",""), "country": geo.get("country","")}


@app.put("/api/users/me")
def api_update_me(data: ProfileUpdateIn, request: Request):
    me = require_user(request)
    if not rate_limit("upd:" + me["nick"], 10, 60): raise HTTPException(429, "err_rate_limit")
    name = data.name.strip(); nick = data.nick.strip().lstrip("@"); bio = data.bio.strip()
    if len(name) < 1 or len(name) > 50: raise HTTPException(400, "err_bad_name")
    if not NICK_RE.match(nick): raise HTTPException(400, "err_bad_nick")
    if len(bio) > MAX_BIO_LEN: raise HTTPException(400, "err_bio_too_long")
    avatar = clean_emoji(data.avatar_emoji) or DEFAULT_EMOJI
    old_nick = me["nick"]
    nick_changed = (nick.lower() != old_nick.lower())
    if nick_changed:
        if nick.lower() in RESERVED_NICKS: raise HTTPException(400, "err_nick_reserved")
        exists = db_load_user(nick)
        if exists and exists["nick"].lower() != old_nick.lower():
            raise HTTPException(400, "err_nick_taken")
    db_update_user_fields(old_nick, {"name": name, "bio": bio, "avatar_emoji": avatar})
    if nick_changed:
        _rename_user_everywhere(old_nick, nick)
        for t, s in list(SESSIONS.items()):
            if (s.get("nick") or "").lower() == old_nick.lower(): s["nick"] = nick
        invalidate_user_cache()
    u = db_load_user(nick)
    return {"ok": True, "user": serialize_user(u, u["nick"])}


@app.post("/api/users/me/settings")
def api_set_settings(data: SettingsIn, request: Request):
    me = require_user(request)
    if not rate_limit("set:" + me["nick"], 20, 60): raise HTTPException(429, "err_rate_limit")
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
    fol = {x.lower() for x in (viewer.get("following") or set())} if viewer else set()
    data["is_following"] = bool(viewer and u["nick"].lower() in fol)
    fw = {x.lower() for x in (u.get("followers") or set())}
    data["is_follower"] = bool(viewer and viewer["nick"].lower() in fw)
    return data


@app.get("/api/users/{nick}/followers")
def api_followers(nick: str, request: Request):
    u = db_load_user(nick)
    if not u: raise HTTPException(404, "not found")
    viewer = get_current_user(request)
    vn = viewer["nick"] if viewer else None
    is_owner = bool(vn and vn.lower() == u["nick"].lower())
    allow = u.get("allow_followers_view", True)
    followers = list(u.get("followers") or [])
    if not allow and not is_owner:
        if vn and vn.lower() in {x.lower() for x in followers}:
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
    is_owner = bool(vn and vn.lower() == u["nick"].lower())
    allow = u.get("allow_following_view", True)
    following = list(u.get("following") or [])
    if not allow and not is_owner:
        if vn and vn.lower() in {x.lower() for x in following}:
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
    if not rate_limit("fol:" + me["nick"], 30, 60): raise HTTPException(429, "err_rate_limit")
    target = db_load_user(nick)
    if not target: raise HTTPException(404, "not found")
    if target["nick"].lower() == me["nick"].lower(): raise HTTPException(400, "self")
    if target["nick"].lower() not in {x.lower() for x in (me.get("following") or set())}:
        nf = set(me.get("following") or set()); nf.add(target["nick"])
        me["following"] = nf; invalidate_user_cache(me["nick"])
        db_update_user_fields(me["nick"], {"following": list(nf)})
        nfw = set(target.get("followers") or set()); nfw.add(me["nick"])
        target["followers"] = nfw; invalidate_user_cache(target["nick"])
        db_update_user_fields(target["nick"], {"followers": list(nfw)})
        db_notify(target["nick"], "follow", me["nick"])
        bus_broadcast("profile:" + target["nick"], {"type": "refresh"})
    target = db_load_user(nick)
    data = serialize_user(target, me["nick"]); data["is_following"] = True
    return data


@app.post("/api/users/{nick}/unfollow")
def api_unfollow(nick: str, request: Request):
    me = require_user(request)
    if not rate_limit("fol:" + me["nick"], 30, 60): raise HTTPException(429, "err_rate_limit")
    target = db_load_user(nick)
    if not target: raise HTTPException(404, "not found")
    if target["nick"].lower() in {x.lower() for x in (me.get("following") or set())}:
        nf = {x for x in (me.get("following") or set()) if x.lower() != target["nick"].lower()}
        me["following"] = nf; invalidate_user_cache(me["nick"])
        db_update_user_fields(me["nick"], {"following": list(nf)})
        nfw = {x for x in (target.get("followers") or set()) if x.lower() != me["nick"].lower()}
        target["followers"] = nfw; invalidate_user_cache(target["nick"])
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
    if (p.get("author") or "").lower() == u["nick"].lower():
        return {"ok": True, "counted": False}
    vid = "u:" + u["nick"]
    if not view_should_count(pid, vid): return {"ok": True, "counted": False}
    view_mark_counted(pid, vid)
    new_views = db_inc_views(pid)
    ev = {"type": "view_update", "post_id": pid, "views": new_views}
    bus_broadcast("post:" + pid, ev); bus_broadcast("feed", ev)
    return {"ok": True, "counted": True, "views": new_views}


@app.post("/api/posts")
def api_create(payload: PostIn, request: Request):
    u = require_user(request)
    ip = get_client_ip(request)
    if not rate_limit("post:" + ip, 30, 60): raise HTTPException(429, "err_rate_limit")
    if not rate_limit("post:" + u["nick"], 30, 60): raise HTTPException(429, "err_rate_limit")
    text = payload.text.strip()
    if not text and not payload.quoted_post_id: raise HTTPException(400, "empty")
    if len(text) > MAX_POST_LEN: raise HTTPException(400, "too long")
    pid = uuid.uuid4().hex[:10]
    og = _build_og_for_text(text, payload.og_enabled)
    device = detect_device(request.headers.get("user-agent",""))
    p = {"id": pid, "text": text, "author": u["nick"],
         "created_at": time.time(), "device": device,
         "quoted_post_id": payload.quoted_post_id or None, "og_data": og,
         "votes": {}, "comments": []}
    db_create_post(p)

    mentions = extract_mentions(text)
    mention_nicks = [m for m in mentions if m.lower() != u["nick"].lower()]
    mention_users = db_load_users_batch(mention_nicks) if mention_nicks else {}
    notifications: List[dict] = []; notified = set()
    for m in mention_nicks:
        k = m.lower()
        if k in notified or k not in mention_users: continue
        notifications.append({"to_nick": m, "ntype": "mention", "from_nick": u["nick"],
                              "post_id": pid, "text": text[:140]})
        notified.add(k)
    for f in (u.get("followers") or set()):
        if f.lower() == u["nick"].lower() or f.lower() in notified: continue
        notifications.append({"to_nick": f, "ntype": "new_post", "from_nick": u["nick"],
                              "post_id": pid, "text": text[:140]})
        notified.add(f.lower())
    if payload.quoted_post_id:
        qp = db_get_post(payload.quoted_post_id)
        if qp and qp.get("author") and qp["author"].lower() != u["nick"].lower():
            notifications.append({"to_nick": qp["author"], "ntype": "quote",
                                  "from_nick": u["nick"], "post_id": pid, "text": text[:140]})
    if notifications: db_notify_many(notifications)
    broadcast_post_change(p, feed=True)
    return build_posts_full([p], "u:" + u["nick"], with_comments=True)[0]


@app.put("/api/posts/{pid}")
def api_edit_post(pid: str, payload: PostEditIn, request: Request):
    u = require_user(request)
    if not rate_limit("edit:" + u["nick"], 30, 60): raise HTTPException(429, "err_rate_limit")
    p = db_get_post(pid)
    if not p: raise HTTPException(404, "not found")
    if (p["author"] or "").lower() != u["nick"].lower(): raise HTTPException(403, "forbidden")
    text = payload.text.strip()
    if not text: raise HTTPException(400, "empty")
    if len(text) > MAX_POST_LEN: raise HTTPException(400, "too long")
    db_update_post_text(pid, text); p["text"] = text
    broadcast_post_change(p, feed=False)
    return {"ok": True, "text": text}


@app.delete("/api/posts/{pid}")
def api_delete_post(pid: str, request: Request):
    u = require_user(request)
    if not rate_limit("del:" + u["nick"], 30, 60): raise HTTPException(429, "err_rate_limit")
    p = db_get_post(pid)
    if not p: raise HTTPException(404, "not found")
    if (p["author"] or "").lower() != u["nick"].lower(): raise HTTPException(403, "forbidden")
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
    if not rate_limit("like:" + u["nick"], 120, 60): raise HTTPException(429, "err_rate_limit")
    vid = "u:" + u["nick"]
    votes = p.setdefault("votes", {})
    cur = votes.get(vid, 0)
    new = 0 if cur == 1 else 1
    db_set_post_vote(pid, vid, new)
    likes = sum(1 for d in votes.values() if d == 1)
    broadcast_post_change(p, except_nick=u["nick"], feed=False)
    return {"ok": True, "likes": likes, "user_like": new}


@app.post("/api/posts/{pid}/comments")
def api_add_comment(pid: str, c: CommentIn, request: Request):
    u = require_user(request)
    ip = get_client_ip(request)
    if not rate_limit("cmt:" + ip, 60, 60): raise HTTPException(429, "err_rate_limit")
    if not rate_limit("cmt:" + u["nick"], 60, 60): raise HTTPException(429, "err_rate_limit")
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
    mentions = extract_mentions(text)
    notifications: List[dict] = []; notified = set()
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
        if k in notified or k not in mention_users: continue
        notifications.append({"to_nick": m, "ntype": "mention", "from_nick": u["nick"],
                              "post_id": pid, "comment_id": cid, "text": text[:140]})
        notified.add(k)
    if notifications: db_notify_many(notifications, users_map=mention_users)
    broadcast_post_change(post, except_nick=u["nick"], feed=False)
    return {
        "ok": True, "post_id": pid, "post_author": post["author"],
        "comment": {"id": cid, "text": text, "author": u["nick"],
                    "parent_id": parent_id, "created_at": ts_to_iso(now_ts),
                    "likes": 0, "user_like": 0}
    }


@app.put("/api/posts/{pid}/comments/{cid}")
def api_edit_comment(pid: str, cid: str, payload: CommentEditIn, request: Request):
    u = require_user(request)
    if not rate_limit("editc:" + u["nick"], 60, 60): raise HTTPException(429, "err_rate_limit")
    c = db_get_comment(cid)
    if not c or c["post_id"] != pid: raise HTTPException(404, "not found")
    if (c["author"] or "").lower() != u["nick"].lower(): raise HTTPException(403, "forbidden")
    text = payload.text.strip()
    if not text: raise HTTPException(400, "empty")
    if len(text) > MAX_COMMENT_LEN: raise HTTPException(400, "too long")
    db_update_comment_text(cid, text)
    post = db_get_post(pid)
    broadcast_post_change(post, except_nick=u["nick"], feed=False)
    return {"ok": True, "text": text}


@app.delete("/api/posts/{pid}/comments/{cid}")
def api_delete_comment(pid: str, cid: str, request: Request):
    u = require_user(request)
    if not rate_limit("delc:" + u["nick"], 60, 60): raise HTTPException(429, "err_rate_limit")
    c = db_get_comment(cid)
    if not c or c["post_id"] != pid: raise HTTPException(404, "not found")
    if (c["author"] or "").lower() != u["nick"].lower(): raise HTTPException(403, "forbidden")
    db_delete_comment(cid)
    post = db_get_post(pid)
    broadcast_post_change(post, except_nick=u["nick"], feed=False)
    return {"ok": True}


@app.post("/api/posts/{pid}/comments/{cid}/like")
def api_like_comment(pid: str, cid: str, request: Request):
    p = db_get_post(pid)
    if not p: raise HTTPException(404, "not found")
    u = require_user(request)
    if not rate_limit("like:" + u["nick"], 120, 60): raise HTTPException(429, "err_rate_limit")
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
    except Exception: likes = 0
    broadcast_post_change(p, except_nick=u["nick"], feed=False)
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
    if not rate_limit("nc:" + me["nick"], 10, 60): raise HTTPException(429, "err_rate_limit")
    db_notifications_clear(me["nick"]); return {"ok": True}


@app.get("/api/events")
async def api_events(request: Request, token: str = "", anon: str = ""):
    if token:
        sess = SESSIONS.get(token)
        if not sess: raise HTTPException(401, "unauthorized")
        nick = sess["nick"]
        if not db_load_user_cached(nick): raise HTTPException(401, "unauthorized")
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
            yield "retry: 2000\n\n"
            yield ": connected\n\n"
            yield f"data: {json.dumps({'type':'hello','nick':nick}, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected(): break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SEC)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError: pass
        except Exception as e: log.warning("[SSE] %s: %s", nick, e)
        finally: bus.unsubscribe(nick, q)
    return StreamingResponse(gen(), media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })


# ============================================================================
# TEXTS
# ============================================================================
TEXTS = {
    "ru": {
        "site_name": "СЛД",
        "auth_tagline": "Минималистичная соцсеть. Посты, комментарии, подписки — без лишнего.",
        "session_expired": "Сессия истекла. Войдите снова.",
        "session_expired_hint": "Ваша сессия была завершена на сервере. Войдите заново, чтобы продолжить.",
        "network_error_hint": "Не удалось связаться с сервером. Проверьте соединение и попробуйте снова.",
        "booting": "Загрузка…", "loading": "Загрузка…",
        "check_nick": "Проверить ник", "checking": "Проверка…", "nick_available": "Ник свободен",
        "refresh": "Обновить", "change_lang": "Язык", "change_theme": "Тема",
        "search_ph": "Поиск людей и постов", "post_ph": "Что нового?",
        "comment_ph": "Комментарий... Shift+Enter — отправить", "reply_ph": "Ответ...",
        "publish": "Опубликовать", "send_comment": "Отправить",
        "reply": "Ответить", "cancel_reply": "Отмена",
        "no_posts": "Здесь пока пусто", "not_found": "Ничего не найдено",
        "page_not_found": "Страница не найдена или была удалена", "back_home": "На главную",
        "just_now": "только что", "sec_ago": "с", "min_ago": "мин", "hour_ago": "ч", "day_ago": "д",
        "read_more": "Показать полностью", "copy": "Копировать",
        "edit": "Редактировать", "delete": "Удалить", "save": "Сохранить", "cancel": "Отмена",
        "confirm_delete": "Удалить без возможности восстановления?",
        "confirm_delete_yes": "Да, удалить",
        "confirm_logout": "Вы действительно хотите выйти?",
        "confirm_yes": "Да, выйти", "confirm_no": "Отмена",
        "notif_clear": "Очистить", "notif_clear_confirm": "Удалить все уведомления?",
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
        "to_login": "Уже зарегистрированы? Войти", "to_reg": "Нет аккаунта? Зарегистрироваться",
        "err_bad_nick": "Ник: 3-20 символов, латиница, цифры, _",
        "err_nick_reserved": "Этот ник зарезервирован",
        "err_bad_name": "Имя: от 1 до 50 символов",
        "err_short_pass": "Пароль: минимум 8 символов",
        "err_pass_weak": "Пароль должен содержать буквы и цифры",
        "err_pass_mismatch": "Пароли не совпадают",
        "err_nick_taken": "Этот ник уже занят",
        "err_bad_login": "Неверный ник или пароль",
        "err_rate_limit": "Слишком много запросов, подождите",
        "err_private_followers": "Пользователь скрыл подписчиков",
        "err_private_following": "Пользователь скрыл подписки",
        "err_bio_too_long": "Описание слишком длинное (до 300 символов)",
        "login_to_post": "Войдите, чтобы публиковать",
        "login_to_comment": "Войдите, чтобы комментировать", "go_login": "Войти",
        "profile_followers": "подписчиков", "profile_following": "подписок",
        "follow": "Подписаться", "unfollow": "Отписаться",
        "edit_profile": "Редактировать", "edit_profile_title": "Изменить профиль",
        "no_user_posts": "Здесь пока пусто",
        "settings_title": "Настройки", "settings_account": "Аккаунт",
        "settings_privacy": "Конфиденциальность", "settings_notifications": "Уведомления",
        "settings_appearance": "Внешний вид", "settings_info": "О приложении",
        "settings_account_desc": "Профиль, аватар, выход",
        "settings_privacy_desc": "Приватность и уведомления",
        "settings_appearance_desc": "Тема, язык, цвета, звук",
        "settings_info_desc": "О сервисе, политика, устройство, аптайм",
        "settings_theme": "Тема", "settings_lang": "Язык",
        "settings_colors": "Основные цвета",
        "settings_colors_hint": "Цвет кнопок, переключателей и подсветок",
        "settings_allow_followers": "Показывать список подписчиков",
        "settings_allow_following": "Показывать список подписок",
        "settings_show_link_previews": "Показывать превью ссылок (OpenGraph)",
        "settings_show_link_previews_hint": "Если включено, к постам со ссылками будут добавляться карточки предпросмотра",
        "settings_show_device_badge": "Показывать значок устройства на постах",
        "settings_show_device_badge_hint": "Рядом с вашим именем будет отображаться иконка телефона или компьютера",
        "settings_device": "Ваше устройство", "settings_device_browser": "Браузер",
        "settings_device_os": "Система", "settings_device_city": "Город",
        "settings_server": "Сервер", "settings_uptime": "Аптайм", "settings_version": "Версия",
        "settings_policy": "Политика конфиденциальности",
        "settings_desc": "СЛД — минималистичная соцсеть: посты и комментарии.",
        "settings_authors": "Авторы", "settings_logout": "Выйти",
        "settings_sound": "Звук уведомлений", "settings_sound_hint": "Проигрывать лёгкий звук при новом уведомлении",
        "settings_profile_section": "Профиль", "settings_session_section": "Сессия",
        "settings_saved": "Сохранено",
        "theme_light": "Светлая", "theme_dark": "Тёмная",
        "notif_title": "Уведомления", "notif_empty": "Здесь пока пусто",
        "notif_follow": "подписался на вас", "notif_comment": "оставил комментарий",
        "notif_reply": "ответил на ваш комментарий", "notif_mention": "упомянул вас",
        "notif_new_post": "опубликовал новый пост", "notif_quote": "процитировал ваш пост",
        "notify_new_post": "Новые посты подписок", "notify_follow": "Новые подписчики",
        "notify_comment": "Комментарии к моим постам", "notify_reply": "Ответы на мои комментарии",
        "notify_mention": "Упоминания меня", "notify_quote": "Цитаты моих постов",
        "back": "Назад", "bio_ph": "О себе (до 300 символов)...",
        "followers_title": "Подписчики", "following_title": "Подписки",
        "no_followers": "Подписчиков пока нет", "no_following": "Подписок пока нет",
        "limited_list": "Список скрыт. Вам виден только ваш аккаунт.",
        "people_title": "Люди", "people_search_ph": "Поиск людей",
        "people_all": "Все", "people_subs": "Подписки", "no_users": "Никого не найдено",
        "policy_title": "Политика конфиденциальности",
        "policy_content": (
            "# Политика конфиденциальности\n"
            "Мы создали СЛД как минималистичную социальную сеть и серьёзно относимся к вашей приватности. Здесь прозрачно описано, какие данные мы собираем, как их используем и как вы можете ими управлять.\n\n"
            "## 1. Какие данные мы храним\n"
            "• Имя, отображаемое в профиле\n"
            "• Уникальный ник (идентификатор)\n"
            "• Эмодзи-аватар и описание профиля (до 300 символов)\n"
            "• Хеш пароля (pbkdf2-hmac-sha256, 100 000 итераций, случайная соль)\n"
            "• Ваши посты и их содержимое\n"
            "• Комментарии и ответы\n"
            "• Лайки постов и комментариев\n"
            "• Подписки (кто на кого подписан)\n"
            "• Уведомления и их прочтение\n"
            "• Настройки приватности, уведомлений и внешнего вида\n"
            "• Тип устройства (телефон / компьютер) в момент публикации поста\n\n"
            "## 2. Чего мы НЕ храним\n"
            "• Пароль в открытом виде — только криптографический хеш\n"
            "• Ваш IP-адрес (используется в момент запроса, не сохраняется)\n"
            "• Cookies третьих лиц, аналитику, трекеры, рекламные идентификаторы\n"
            "• Данные о вашем местоположении за пределами города\n\n"
            "## 3. Где хранятся данные\n"
            "Все данные хранятся на серверах Supabase (PostgreSQL). Обмен между клиентом и сервером идёт по HTTPS. Мы не продаём и не передаём ваши данные третьим лицам. Резервные копии делаются автоматически и защищены теми же правилами.\n\n"
            "## 4. Как мы используем данные\n"
            "• Для отображения вашего профиля и постов другим пользователям\n"
            "• Для показа ваших постов в лентах ваших подписчиков\n"
            "• Для отправки уведомлений о действиях других пользователей\n"
            "• Для проверки прав доступа\n"
            "• Для определения языка интерфейса и отображения города в ваших настройках\n"
            "• Для защиты от спама, ботов и подбора пароля\n\n"
            "## 5. Безопасность аккаунта\n"
            "• Пароль минимум 8 символов, обязательно с буквами и цифрами\n"
            "• Хеш pbkdf2-hmac-sha256, 100 000 итераций\n"
            "• Проверка пароля выполняется за постоянное время\n"
            "• Сессии привязаны к случайному токену, максимум 10 активных сессий\n"
            "• Ограничение попыток входа по IP и нику\n"
            "• Все операции изменения данных проверяют владельца на сервере\n"
            "• Зарезервированные ники недоступны для регистрации\n"
            "• Строгие заголовки безопасности: CSP, X-Frame-Options, Referrer-Policy, HSTS\n"
            "• Защита от SSRF при загрузке превью ссылок\n"
            "• Ограничение размера тела запроса (1 МБ)\n\n"
            "## 6. Ваши права\n"
            "• Смотреть и редактировать свой профиль\n"
            "• Удалять свои посты, комментарии и их ответы\n"
            "• Отписываться от пользователей\n"
            "• Отключать любой тип уведомлений отдельно\n"
            "• Отключать звук уведомлений\n"
            "• Скрывать список подписчиков и подписок\n"
            "• Отключать показ значка устройства\n"
            "• Удалить аккаунт — напишите нам, данные удаляются в течение 30 дней\n\n"
            "## 7. Хранение и удаление\n"
            "Данные хранятся, пока активен ваш аккаунт. При удалении аккаунта все посты, комментарии, лайки, подписки и уведомления удаляются безвозвратно.\n\n"
            "## 8. Cookies и localStorage\n"
            "• localStorage: SLD_token, SLD_theme, SLD_lang, SLD_colors, SLD_sound, SLD_anon_id, SLD_welcome_seen\n"
            "• sessionStorage: черновик поста, состояние настроек, цитата\n"
            "• Один cookie: SLD_lang — только чтобы помнить язык\n"
            "Сторонних cookies нет.\n\n"
            "## 9. Дети\n"
            "Сервис не предназначен для лиц младше 13 лет.\n\n"
            "## 10. Изменения политики\n"
            "Мы можем обновлять эту политику. Актуальная версия всегда доступна по этой ссылке.\n\n"
            "## 11. Контакты\n"
            "По вопросам приватности, удаления аккаунта или данных — напишите нам через профиль разработчика.\n\n"
            "Сервис предоставляется «как есть», без гарантий."
        ),
        "quote": "Цитировать", "avatar_choose": "Выберите эмодзи",
        "avatar_current": "Текущий аватар", "og_enable": "Превью ссылок",
        "color_blue": "Синий", "color_green": "Зелёный", "color_purple": "Фиолетовый",
        "color_pink": "Розовый", "color_orange": "Оранжевый", "color_red": "Красный",
        "color_teal": "Бирюзовый", "color_indigo": "Индиго",
        "color_accent": "Акцент", "color_likes": "Лайки",
        "reset_colors": "Сбросить цвета",
        "sent_from_mobile": "Отправлено с телефона",
        "sent_from_desktop": "Отправлено с компьютера",
        "og_img_fallback": "Упсс... не удалось загрузить :(",
        "welcome_title": "Добро пожаловать!",
        "welcome_continue": "Продолжить",
        "welcome_text": (
            "Вы в СЛД — минималистичной социальной сети для тех, кто ценит простоту и приватность.\n\n"
            "Здесь нет рекламы, алгоритмов и бесконечной ленты — только вы, ваши мысли и люди, которые вам интересны.\n\n"
            "Что можно делать:\n"
            "• Публиковать посты до 5000 символов\n"
            "• Комментировать и отвечать на комментарии\n"
            "• Ставить лайки постам и комментариям\n"
            "• Подписываться на интересных людей и читать их ленту\n"
            "• Настраивать эмодзи-аватар и описание профиля\n"
            "• Управлять уведомлениями — включать и отключать каждый тип отдельно\n"
            "• Менять тему, цвета акцента и звук уведомлений\n\n"
            "Что мы НЕ делаем:\n"
            "• Не показываем рекламу\n"
            "• Не используем трекеры и аналитику\n"
            "• Не продаём данные третьим лицам\n"
            "• Не храним пароль в открытом виде\n\n"
            "Безопасность:\n"
            "Пароль хешируется через pbkdf2-hmac-sha256 (100 000 итераций). Проверка выполняется за постоянное время. Сессии привязаны к случайному токену, количество активных сессий ограничено.\n\n"
            "Всё, что вы публикуете, можно в любой момент отредактировать или удалить. Профиль, подписки, посты, комментарии — всё под вашим контролем.\n\n"
            "Приятного общения! Если что-то пойдёт не так — напишите через профиль разработчика."
        ),
        "page_title_feed": "Лента", "page_title_post": "Пост",
        "page_title_profile": "Профиль", "page_title_users": "Люди",
        "page_title_notifications": "Уведомления", "page_title_settings": "Настройки",
        "page_title_edit_profile": "Изменить профиль", "page_title_policy": "Политика",
        "page_title_register": "Регистрация", "page_title_login": "Вход",
        "page_title_followers": "Подписчики", "page_title_following": "Подписки",
        "page_title_notfound": "Не найдено",
    },
    "en": {
        "site_name": "SLD",
        "auth_tagline": "Minimalist social network. Posts, comments, follows — nothing extra.",
        "session_expired": "Session expired. Please log in again.",
        "session_expired_hint": "Your session has ended on the server. Please log in again to continue.",
        "network_error_hint": "Could not reach the server. Check your connection and try again.",
        "booting": "Loading…", "loading": "Loading…",
        "check_nick": "Check nick", "checking": "Checking…", "nick_available": "Nick is available",
        "refresh": "Refresh", "change_lang": "Language", "change_theme": "Theme",
        "search_ph": "Search people and posts", "post_ph": "What's new?",
        "comment_ph": "Comment... Shift+Enter to send", "reply_ph": "Reply...",
        "publish": "Publish", "send_comment": "Send",
        "reply": "Reply", "cancel_reply": "Cancel",
        "no_posts": "Nothing here yet", "not_found": "Not found",
        "page_not_found": "Page not found or was deleted", "back_home": "Back to home",
        "just_now": "just now", "sec_ago": "s", "min_ago": "min", "hour_ago": "h", "day_ago": "d",
        "read_more": "Show more", "copy": "Copy",
        "edit": "Edit", "delete": "Delete", "save": "Save", "cancel": "Cancel",
        "confirm_delete": "Delete permanently?", "confirm_delete_yes": "Yes, delete",
        "confirm_logout": "Are you sure you want to log out?",
        "confirm_yes": "Yes, log out", "confirm_no": "Cancel",
        "notif_clear": "Clear", "notif_clear_confirm": "Clear all notifications?",
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
        "to_login": "Already have an account? Log in", "to_reg": "No account? Sign up",
        "err_bad_nick": "Nick: 3-20 chars, letters/digits/_",
        "err_nick_reserved": "This nick is reserved",
        "err_bad_name": "Name: 1-50 chars",
        "err_short_pass": "Password: min 8 chars",
        "err_pass_weak": "Password must contain letters and digits",
        "err_pass_mismatch": "Passwords do not match",
        "err_nick_taken": "Nick already taken",
        "err_bad_login": "Wrong nick or password",
        "err_rate_limit": "Too many requests, please wait",
        "err_private_followers": "User hid their followers",
        "err_private_following": "User hid their following",
        "err_bio_too_long": "Bio is too long (max 300 chars)",
        "login_to_post": "Log in to publish", "login_to_comment": "Log in to comment",
        "go_login": "Log in",
        "profile_followers": "followers", "profile_following": "following",
        "follow": "Follow", "unfollow": "Unfollow",
        "edit_profile": "Edit", "edit_profile_title": "Edit profile",
        "no_user_posts": "Nothing here yet",
        "settings_title": "Settings", "settings_account": "Account",
        "settings_privacy": "Privacy", "settings_notifications": "Notifications",
        "settings_appearance": "Appearance", "settings_info": "About",
        "settings_account_desc": "Profile, avatar, sign out",
        "settings_privacy_desc": "Privacy and notifications",
        "settings_appearance_desc": "Theme, language, colors, sound",
        "settings_info_desc": "About, policy, device, uptime",
        "settings_theme": "Theme", "settings_lang": "Language",
        "settings_colors": "Accent colors",
        "settings_colors_hint": "Color of buttons, toggles and highlights",
        "settings_allow_followers": "Show followers list",
        "settings_allow_following": "Show following list",
        "settings_show_link_previews": "Show link previews (OpenGraph)",
        "settings_show_link_previews_hint": "If enabled, posts with links will show preview cards",
        "settings_show_device_badge": "Show device badge on posts",
        "settings_show_device_badge_hint": "A phone or desktop icon will appear next to your name on posts",
        "settings_device": "Your device", "settings_device_browser": "Browser",
        "settings_device_os": "System", "settings_device_city": "City",
        "settings_server": "Server", "settings_uptime": "Uptime", "settings_version": "Version",
        "settings_policy": "Privacy policy",
        "settings_desc": "SLD — minimalist social network: posts and comments.",
        "settings_authors": "Authors", "settings_logout": "Log out",
        "settings_sound": "Notification sound", "settings_sound_hint": "Play a soft sound on new notification",
        "settings_profile_section": "Profile", "settings_session_section": "Session",
        "settings_saved": "Saved",
        "theme_light": "Light", "theme_dark": "Dark",
        "notif_title": "Notifications", "notif_empty": "Nothing here yet",
        "notif_follow": "followed you", "notif_comment": "commented",
        "notif_reply": "replied to your comment", "notif_mention": "mentioned you",
        "notif_new_post": "published a new post", "notif_quote": "quoted your post",
        "notify_new_post": "New posts from subscriptions", "notify_follow": "New followers",
        "notify_comment": "Comments on my posts", "notify_reply": "Replies to my comments",
        "notify_mention": "Mentions of me", "notify_quote": "Quotes of my posts",
        "back": "Back", "bio_ph": "Bio (max 300 chars)...",
        "followers_title": "Followers", "following_title": "Following",
        "no_followers": "No followers yet", "no_following": "No following yet",
        "limited_list": "List is hidden. You can see only your account.",
        "people_title": "People", "people_search_ph": "Search people",
        "people_all": "All", "people_subs": "Subscriptions", "no_users": "No users found",
        "policy_title": "Privacy Policy",
        "policy_content": (
            "# Privacy Policy\n"
            "We built SLD as a minimalist social network with your privacy in mind. Here we transparently explain what data we collect, how we use it, and how you stay in control.\n\n"
            "## 1. Data we store\n"
            "• Display name shown on your profile\n"
            "• Unique nick (identifier)\n"
            "• Emoji avatar and bio (up to 300 characters)\n"
            "• Password hash (pbkdf2-hmac-sha256, 100 000 iterations, random salt)\n"
            "• Your posts and their content\n"
            "• Comments and replies\n"
            "• Likes on posts and comments\n"
            "• Follows (who follows whom)\n"
            "• Notifications and their read status\n"
            "• Privacy, notification and appearance settings\n"
            "• Device type (mobile / desktop) at publish time\n\n"
            "## 2. What we do NOT store\n"
            "• Your password in plain text\n"
            "• Your IP address (used at request time, not saved)\n"
            "• Third-party cookies, analytics, trackers, ad identifiers\n"
            "• Data about your location beyond city\n\n"
            "## 3. Where data is stored\n"
            "All data is stored on Supabase servers (PostgreSQL). Client-server communication uses HTTPS. We never sell or share your data with third parties. Backups are automatic and protected by the same rules.\n\n"
            "## 4. How we use data\n"
            "• To display your profile and posts to other users\n"
            "• To show your posts in your subscribers' feeds\n"
            "• To send you notifications about other users' actions\n"
            "• To verify access rights\n"
            "• To detect interface language and show city in your settings\n"
            "• To protect against spam, bots and brute-force\n\n"
            "## 5. Account security\n"
            "• Password min 8 characters, must contain letters and digits\n"
            "• pbkdf2-hmac-sha256 hash, 100 000 iterations\n"
            "• Password check runs in constant time\n"
            "• Sessions bound to a random token, max 10 active sessions per account\n"
            "• Login attempts limited per IP and per nick\n"
            "• All data modification operations verify the owner on the server\n"
            "• Reserved nicks unavailable for registration\n"
            "• Strict security headers: CSP, X-Frame-Options, Referrer-Policy, HSTS\n"
            "• SSRF protection when fetching link previews\n"
            "• Request body size limit (1 MB)\n\n"
            "## 6. Your rights\n"
            "• View and edit your profile\n"
            "• Delete your posts, comments and their replies\n"
            "• Unfollow users\n"
            "• Disable any type of notification individually\n"
            "• Turn off notification sound\n"
            "• Hide your followers / following lists\n"
            "• Disable the device badge on your posts\n"
            "• Delete your account — write to us\n\n"
            "## 7. Retention and deletion\n"
            "Data is retained while your account is active. When you delete your account, all data is permanently removed.\n\n"
            "## 8. Cookies and localStorage\n"
            "• localStorage: SLD_token, SLD_theme, SLD_lang, SLD_colors, SLD_sound, SLD_anon_id, SLD_welcome_seen\n"
            "• sessionStorage: post draft, settings state, quote\n"
            "• One cookie: SLD_lang — only to remember your language choice\n"
            "No third-party cookies.\n\n"
            "## 9. Children\n"
            "The service is not intended for users under 13.\n\n"
            "## 10. Policy changes\n"
            "We may update this policy. The current version is always available at this link.\n\n"
            "## 11. Contact\n"
            "For privacy, account deletion or data questions — write to us via the developer's profile.\n\n"
            "The service is provided \"as is\", without warranties."
        ),
        "quote": "Quote", "avatar_choose": "Choose an emoji",
        "avatar_current": "Current avatar", "og_enable": "Link previews",
        "color_blue": "Blue", "color_green": "Green", "color_purple": "Purple",
        "color_pink": "Pink", "color_orange": "Orange", "color_red": "Red",
        "color_teal": "Teal", "color_indigo": "Indigo",
        "color_accent": "Accent", "color_likes": "Likes",
        "reset_colors": "Reset colors",
        "sent_from_mobile": "Sent from a phone", "sent_from_desktop": "Sent from a computer",
        "og_img_fallback": "Oops... could not load :(",
        "welcome_title": "Welcome!",
        "welcome_continue": "Continue",
        "welcome_text": (
            "You're in SLD — a minimalist social network for those who value simplicity and privacy.\n\n"
            "No ads, no algorithms, no endless feed — just you, your thoughts, and the people you care about.\n\n"
            "What you can do:\n"
            "• Publish posts up to 5000 characters\n"
            "• Comment and reply to comments\n"
            "• Like posts and comments\n"
            "• Follow interesting people and read their feed\n"
            "• Set up your emoji avatar and bio\n"
            "• Control notifications — toggle each type individually\n"
            "• Change theme, accent colors and notification sound\n\n"
            "What we do NOT do:\n"
            "• No ads\n• No trackers or analytics\n"
            "• No selling your data to third parties\n"
            "• No storing your password in plain text\n\n"
            "Security:\n"
            "Password is hashed with pbkdf2-hmac-sha256 (100 000 iterations). Verification runs in constant time. Sessions are bound to a random token, and the number of active sessions is limited.\n\n"
            "Everything you publish can be edited or deleted at any time. Profile, follows, posts, comments — all under your control.\n\n"
            "Enjoy! If something goes wrong — reach out via the developer's profile."
        ),
        "page_title_feed": "Feed", "page_title_post": "Post",
        "page_title_profile": "Profile", "page_title_users": "People",
        "page_title_notifications": "Notifications", "page_title_settings": "Settings",
        "page_title_edit_profile": "Edit profile", "page_title_policy": "Policy",
        "page_title_register": "Sign up", "page_title_login": "Log in",
        "page_title_followers": "Followers", "page_title_following": "Following",
        "page_title_notfound": "Not found",
    },
}


# ============================================================================
# SVG icons
# ============================================================================
def svg(paths: str, size: int = 20, sw: float = 2) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="{sw}" stroke-linecap="round" '
            f'stroke-linejoin="round">{paths}</svg>')


I_HOME = svg('<path d="M3 10l9-7 9 7v11a2 2 0 0 1-2 2h-4v-8h-6v8H5a2 2 0 0 1-2-2z"/>')
I_USERS = svg('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>')
I_USER = svg('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>')
I_USER_LG = svg('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>', size=18)
I_BELL = svg('<path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>')
I_GEAR = svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>')
I_LOGOUT = svg('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>')
I_LOGIN = svg('<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>')
I_PLUS = svg('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>')
I_SEND = svg('<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>', size=20)
I_HEART = svg('<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>', size=16)
I_HEART_FILLED = svg('<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z" fill="currentColor"/>', size=16)
I_COMMENT = svg('<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>', size=16)
I_QUOTE = svg('<path d="M6 17h3l2-4V7H5v6h3z"/><path d="M14 17h3l2-4V7h-6v6h3z"/>', size=16)
I_COPY = svg('<rect x="9" y="9" width="12" height="12"/><path d="M5 15H3V3h12v2"/>', size=16)
I_EDIT = svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>', size=16)
I_TRASH = svg('<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/>', size=16)
I_BACK = svg('<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>')
I_CHEVRON = svg('<polyline points="9 18 15 12 9 6"/>', size=18)
I_SEARCH = svg('<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>')
I_MOON = svg('<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>')
I_SUN = svg('<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/>')
I_CAL = svg('<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>', size=14)
I_CHECK = svg('<polyline points="20 6 9 17 4 12"/>', size=14, sw=3)
I_CHECK_LG = svg('<polyline points="20 6 9 17 4 12"/>', size=18, sw=3)
I_X = svg('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>', size=18, sw=3)
I_MOBILE = svg('<rect x="5" y="2" width="14" height="20" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/>', size=12)
I_DESKTOP = svg('<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>', size=12)
I_SERVER = svg('<rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><line x1="6" y1="7" x2="6.01" y2="7"/><line x1="6" y1="17" x2="6.01" y2="17"/>', size=14)
I_EYE = svg('<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>', size=18)
I_EYE_OFF = svg('<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>', size=18)
I_LOCK = svg('<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>', size=18)
I_AT = svg('<circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-3.92 7.94"/>', size=18)
I_INFO = svg('<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>', size=18)
I_REFRESH = svg('<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>', size=18)
I_GLOBE = svg('<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>', size=16)


def _urlenc(svg_str: str) -> str:
    return (svg_str.replace("%", "%25").replace("#", "%23")
            .replace("<", "%3C").replace(">", "%3E").replace("'", "%27")
            .replace('"', "%22").replace(" ", "%20").replace("\n", " "))


def _favicon(text: str, bg: str, fg: str, font_size: int) -> str:
    y = int(50 + font_size * 0.36)
    svg_str = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
        f"<circle cx='50' cy='50' r='50' fill='{bg}'/>"
        f"<text x='50' y='{y}' font-family='Arial Black, Arial, sans-serif' "
        f"font-weight='900' font-size='{font_size}' text-anchor='middle' "
        f"letter-spacing='-1' fill='{fg}'>{text}</text>"
        "</svg>"
    )
    return "data:image/svg+xml," + _urlenc(svg_str)


FAVICON_RU_DARK  = _favicon("СЛД", "#000000", "#ffffff", 34)
FAVICON_RU_LIGHT = _favicon("СЛД", "#ffffff", "#000000", 34)
FAVICON_EN_DARK  = _favicon("SLD", "#000000", "#ffffff", 40)
FAVICON_EN_LIGHT = _favicon("SLD", "#ffffff", "#000000", 40)
FAVICON = FAVICON_RU_DARK


# ============================================================================
# Server-side OpenGraph helpers
# ============================================================================
def _og_escape(s) -> str:
    """HTML-escape for use inside an attribute value."""
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


def _build_og_base(request: Request, lang: str) -> dict:
    t = TEXTS[lang]
    url = str(request.url).split("#", 1)[0]
    return {
        "site_name": t["site_name"],
        "url": url,
        "type": "website",
        "title": t["site_name"],
        "description": t["auth_tagline"],
        "image": None,
    }


def build_og_post(post: dict, request: Request, lang: str) -> dict:
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    author = (post.get("author") or "").strip()
    text = (post.get("text") or "").strip()
    first_line = text.split("\n", 1)[0].strip()
    if len(first_line) > 80:
        first_line = first_line[:80].rstrip() + "…"
    if author and first_line:
        title = f"@{author}: {first_line}"
    elif author:
        title = f"@{author} — {t['site_name']}"
    elif first_line:
        title = first_line
    else:
        title = t["site_name"]
    description = text[:300]
    if len(text) > 300:
        description = description.rstrip() + "…"
    image = None
    og_raw = post.get("og_data")
    if isinstance(og_raw, dict):
        img = og_raw.get("image")
        if img:
            image = _safe_url(img) or None
        if not description:
            description = str(og_raw.get("description") or "")[:300]
    if not description:
        description = t["auth_tagline"]
    quoted_id = post.get("quoted_post_id")
    if quoted_id:
        try:
            qp = db_get_post(quoted_id)
            if qp and qp.get("author"):
                qline = f"\n↩ @{qp['author']}: {(qp.get('text') or '')[:140]}"
                description = (description + qline)[:400]
        except Exception:
            pass
    og["title"] = title
    og["description"] = description
    og["image"] = image
    og["type"] = "article"
    og["url"] = f"{og['url'].split('?')[0].rstrip('/')}"
    return og


def build_og_user(u: dict, request: Request, lang: str) -> dict:
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    nick = (u.get("nick") or "").strip()
    name = (u.get("name") or nick).strip()
    bio = (u.get("bio") or "").strip()
    title = f"{name} (@{nick}) — {t['site_name']}" if nick else name
    description = bio or f"@{nick}" if nick else t["auth_tagline"]
    og["title"] = title
    og["description"] = description[:300]
    og["type"] = "profile"
    return og


# ============================================================================
# CSS
# ============================================================================
CSS = """
:root, [data-theme="dark"] {
  --bg:#0a0a0b; --bg-elev:#101012; --card:#141417; --card-2:#1a1a1e; --card-3:#202025;
  --line:rgba(255,255,255,.06); --line-2:rgba(255,255,255,.10); --line-3:rgba(255,255,255,.16);
  --text:#ececef; --text-2:#b4b4bb; --muted:#7a7a85; --muted-2:#5c5c66;
  --hover:rgba(255,255,255,.045); --hover-2:rgba(255,255,255,.075);
  --accent:#3b82f6; --accent-fg:#ffffff;
  --accent-soft:rgba(59,130,246,.12); --accent-soft-2:rgba(59,130,246,.18);
  --like:#ef4444; --danger:#ef4444; --mention:#7aa2ff; --ok:#22c55e;
  --shadow-sm:0 1px 2px rgba(0,0,0,.25); --shadow-md:0 2px 8px rgba(0,0,0,.30);
  --shadow-lg:0 10px 30px rgba(0,0,0,.45);
}
[data-theme="light"] {
  --bg:#f6f6f8; --bg-elev:#ffffff; --card:#ffffff; --card-2:#f2f3f6; --card-3:#eaecf0;
  --line:rgba(0,0,0,.06); --line-2:rgba(0,0,0,.10); --line-3:rgba(0,0,0,.18);
  --text:#0d0d0f; --text-2:#3a3a42; --muted:#7b7b85; --muted-2:#a0a0a8;
  --hover:rgba(0,0,0,.035); --hover-2:rgba(0,0,0,.06);
  --accent:#2563eb; --accent-fg:#ffffff;
  --accent-soft:rgba(37,99,235,.10); --accent-soft-2:rgba(37,99,235,.16);
  --like:#dc2626; --danger:#dc2626; --mention:#2563eb; --ok:#16a34a;
  --shadow-sm:0 1px 2px rgba(15,20,40,.04); --shadow-md:0 2px 8px rgba(15,20,40,.06);
  --shadow-lg:0 10px 30px rgba(15,20,40,.10);
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { height: 100vh; height: 100dvh; margin: 0; padding: 0; overflow: hidden; overscroll-behavior: none; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Inter", "Helvetica Neue", Arial, sans-serif;
  background: var(--bg); color: var(--text); font-size: 15px; line-height: 1.5;
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
  user-select: none; -webkit-user-select: none;
}
input, textarea, button { font-family: inherit; }
input, textarea { user-select: text; -webkit-user-select: text; font-size: 16px; }
* { scrollbar-width: none; -ms-overflow-style: none; }
*::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }
button { cursor: pointer; }
a { -webkit-tap-highlight-color: transparent; }

.layout { display: flex; width: 100%; height: 100vh; height: 100dvh; background: var(--bg); max-width: 1100px; margin: 0 auto; }
body[data-mode="auth"] .layout,
body[data-mode="boot"] .layout { justify-content: center; align-items: center; max-width: 100%; }
body[data-mode="auth"] .sidebar,
body[data-mode="boot"] .sidebar { display: none !important; }
body[data-mode="auth"] .main,
body[data-mode="boot"] .main { max-width: 100%; background: transparent; overflow-y: auto; overflow-x: hidden; }

.sidebar { flex: 0 0 232px; width: 232px; background: var(--bg); display: flex; flex-direction: column; padding: 28px 16px 20px; overflow: hidden; }
.sidebar .logo { font-size: 22px; font-weight: 900; letter-spacing: -0.5px; padding: 0 14px 22px; color: var(--text); user-select: none; }
.sidebar .logo::after { content: '.'; color: var(--accent); }
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-btn {
  display: flex; align-items: center; gap: 14px; width: 100%; padding: 11px 14px;
  border: none; background: transparent; color: var(--text-2); font: inherit; font-size: 14.5px; font-weight: 500;
  cursor: pointer; text-align: left; border-radius: 11px; transition: background .15s ease, color .15s ease; position: relative;
}
.nav-btn:hover { background: var(--hover); color: var(--text); }
.nav-btn.active { background: var(--accent-soft); color: var(--text); font-weight: 600; }
.nav-btn svg { flex-shrink: 0; color: currentColor; opacity: .8; }
.nav-btn.active svg { color: var(--accent); opacity: 1; }
.nav-btn .badge { margin-left: auto; min-width: 22px; height: 22px; background: var(--accent); color: var(--accent-fg); font-size: 11.5px; font-weight: 700; line-height: 22px; text-align: center; padding: 0 7px; border-radius: 11px; }
.sidebar .spacer { flex: 1; }
.sidebar-logout { display: flex; align-items: center; gap: 14px; padding: 11px 14px; color: var(--text-2); font-size: 14.5px; font-weight: 500; background: transparent; border: none; cursor: pointer; border-radius: 11px; text-align: left; width: 100%; transition: background .15s ease, color .15s ease; overflow: hidden; }
.sidebar-logout:hover { background: var(--hover); color: var(--danger); }
.sidebar-logout svg { color: currentColor; opacity: .8; flex-shrink: 0; }
.sidebar-logout span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.main { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; background: var(--bg); overflow: hidden; }
.main-body { flex: 1 1 auto; overflow-y: auto; overflow-x: hidden; -webkit-overflow-scrolling: touch; }
.main-inner { max-width: 680px; margin: 0 auto; padding: 8px 24px 60px; }
.main-header { display: flex; align-items: center; gap: 10px; padding: 20px 24px 12px; max-width: 680px; margin: 0 auto; width: 100%; }
.main-header.wide { max-width: 100%; padding-left: 20px; padding-right: 20px; }
.main-header .title { flex: 1; font-size: 21px; font-weight: 800; letter-spacing: -0.3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.icon-btn { width: 40px; height: 40px; display: inline-flex; align-items: center; justify-content: center; background: transparent; border: none; color: var(--text-2); cursor: pointer; padding: 0; text-decoration: none; border-radius: 10px; transition: background .15s ease, color .15s ease; flex-shrink: 0; }
.icon-btn:hover { background: var(--hover); color: var(--text); }
.icon-btn.danger:hover { color: var(--danger); }
.icon-btn.spinning { color: var(--accent); pointer-events: none; }
.icon-btn.spinning svg { animation: spin .6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.pill-tabs { position: relative; display: flex; gap: 0; padding: 4px; background: var(--card-2); border-radius: 12px; margin-bottom: 16px; isolation: isolate; }
.pill-tabs .pill-slider { position: absolute; top: 4px; left: 0; height: calc(100% - 8px); background: var(--bg-elev); border-radius: 9px; pointer-events: none; z-index: 0; transition: transform .32s cubic-bezier(.4, 0, .2, 1), width .32s cubic-bezier(.4, 0, .2, 1); box-shadow: var(--shadow-sm); will-change: transform, width; }
[data-theme="dark"] .pill-tabs .pill-slider { background: var(--card-3); }
.pill-tab { position: relative; z-index: 1; flex: 1; padding: 9px 14px; background: transparent; border: none; color: var(--muted); font-family: inherit; font-size: 13.5px; font-weight: 600; cursor: pointer; border-radius: 9px; transition: color .2s ease; white-space: nowrap; text-align: center; }
.pill-tab:hover { color: var(--text-2); }
.pill-tab.active { color: var(--text); }

.search-box { display: flex; align-items: center; gap: 12px; background: var(--card-2); border-radius: 14px; padding: 11px 16px; margin-bottom: 16px; transition: background .15s ease, box-shadow .15s ease; }
.search-box:focus-within { background: var(--card); box-shadow: 0 0 0 3px var(--accent-soft); }
.search-box svg { color: var(--muted); flex-shrink: 0; }
.search-box input { flex: 1; background: transparent; border: none; outline: none; color: var(--text); font-size: 15px; font-family: inherit; }
.search-box input::placeholder { color: var(--muted); }

.card { background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 18px; margin-bottom: 14px; }

.composer-avatar-row { display: flex; gap: 14px; align-items: flex-start; }
.composer-avatar-col { display: flex; flex-direction: column; gap: 8px; align-items: center; flex-shrink: 0; }
.composer-body { flex: 1; min-width: 0; }
.composer-body textarea { display: block; width: 100%; background: transparent; border: none; outline: none; resize: none; color: var(--text); font-family: inherit; font-size: 16px; line-height: 1.55; min-height: 52px; max-height: 500px; padding: 4px 0 0; overflow: hidden; }
.composer-body textarea::placeholder { color: var(--muted); }
.composer-actions { display: flex; align-items: center; gap: 10px; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line); flex-wrap: wrap; }
.composer-hint { font-size: 11px; color: var(--muted-2); letter-spacing: .2px; }
.og-toggle { display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--muted); cursor: pointer; user-select: none; transition: color .15s ease; }
.og-toggle:hover { color: var(--text-2); }
.og-toggle input { position: absolute; opacity: 0; pointer-events: none; }
.og-toggle .cb { width: 18px; height: 18px; flex-shrink: 0; border: 1.5px solid var(--line-2); border-radius: 5px; background: transparent; display: inline-flex; align-items: center; justify-content: center; transition: background .15s ease, border-color .15s ease; color: transparent; }
.og-toggle input:checked + .cb { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
.og-toggle .cb svg { display: block; }
.composer-actions .spacer { flex: 1; }
.publish-btn { height: 38px; padding: 0 18px; background: var(--accent); color: var(--accent-fg); border: none; border-radius: 10px; font-family: inherit; font-size: 14px; font-weight: 700; cursor: pointer; transition: opacity .15s ease, transform .1s ease; }
.publish-btn:hover { opacity: .9; }
.publish-btn:active { transform: scale(.97); }
.publish-btn:disabled { opacity: .35; cursor: default; }
.publish-btn-mobile { display: none !important; }

.quote-preview { margin-top: 10px; padding: 12px 14px; background: var(--card-2); border-radius: 12px; border-left: 3px solid var(--accent); position: relative; }
.quote-preview .qp-author { font-size: 12.5px; font-weight: 700; color: var(--text); margin-bottom: 4px; }
.quote-preview .qp-text { font-size: 13.5px; color: var(--muted); white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; max-height: 100px; overflow: hidden; line-height: 1.5; }
.quote-preview .qp-close { position: absolute; top: 8px; right: 8px; width: 26px; height: 26px; background: transparent; border: none; color: var(--muted); cursor: pointer; border-radius: 8px; display: inline-flex; align-items: center; justify-content: center; transition: color .15s ease, background .15s ease; }
.quote-preview .qp-close:hover { color: var(--danger); background: var(--hover); }

.avatar { width: 40px; height: 40px; flex-shrink: 0; border-radius: 50%; background: var(--card-2); border: 1px solid var(--line); display: inline-flex; align-items: center; justify-content: center; font-size: 22px; line-height: 1; user-select: none; overflow: hidden; text-align: center; }
.avatar.sm { width: 36px; height: 36px; font-size: 18px; }
.avatar.lg { width: 76px; height: 76px; font-size: 42px; border-width: 2px; }

.profile-hero { background: var(--card); border: 1px solid var(--line); border-radius: 20px; padding: 20px; margin-bottom: 16px; }
.profile-hero-row { display: flex; gap: 16px; align-items: flex-start; margin-bottom: 14px; }
.profile-hero-avatar { flex-shrink: 0; }
.profile-hero-info { flex: 1; min-width: 0; }
.profile-name { font-size: 22px; font-weight: 800; letter-spacing: -0.3px; margin: 0 0 4px; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.profile-line { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; margin: 0 0 10px; line-height: 1.4; }
.profile-line.only-nick { margin-bottom: 8px; }
.profile-nick { font-size: 14px; color: var(--muted); flex-shrink: 0; }
.profile-bio-inline { font-size: 14px; line-height: 1.45; color: var(--text-2); min-width: 0; word-wrap: break-word; overflow-wrap: anywhere; }
.profile-stats { display: flex; gap: 18px; font-size: 13px; color: var(--muted); margin: 0; line-height: 1.3; flex-wrap: wrap; }
.profile-stat { cursor: pointer; padding: 2px 0; transition: color .15s ease; }
.profile-stat:hover { color: var(--text); }
.profile-stat b { color: var(--text); font-weight: 700; margin-right: 4px; font-variant-numeric: tabular-nums; }
.profile-stat:hover b { color: var(--accent); }
.profile-meta { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; margin-top: 8px; line-height: 1.3; }
.profile-hero-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

.pill-action { height: 36px; padding: 0 16px; background: var(--card-2); color: var(--text); border: 1px solid var(--line); border-radius: 10px; font-family: inherit; font-size: 13.5px; font-weight: 700; cursor: pointer; display: inline-flex; align-items: center; gap: 8px; justify-content: center; text-decoration: none; transition: background .15s ease, border-color .15s ease, opacity .15s ease; }
.pill-action:hover { background: var(--hover); }
.pill-action.primary { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
.pill-action.primary:hover { opacity: .9; background: var(--accent); }
.round-action { width: 36px; height: 36px; background: var(--card-2); border: 1px solid var(--line); color: var(--text); border-radius: 10px; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; text-decoration: none; transition: background .15s ease; }
.round-action:hover { background: var(--hover); }

.post-card { background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 18px; margin-bottom: 12px; transition: border-color .15s ease; }
.post-card:hover { border-color: var(--line-2); }
.post-header { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 10px; }
.post-header .meta { flex: 1; min-width: 0; }
.post-header .who { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 14px; line-height: 1.3; }
.post-author { font-weight: 700; color: var(--text); text-decoration: none; letter-spacing: -0.1px; }
.post-author:hover { color: var(--accent); }
.post-time { color: var(--muted); font-size: 12.5px; }
.post-time::before { content: '·'; margin-right: 5px; color: var(--muted-2); }
.device-badge { display: inline-flex; align-items: center; justify-content: center; color: var(--muted); flex-shrink: 0; line-height: 0; opacity: .75; }
.device-badge svg { display: block; }
.post-menu { display: flex; gap: 2px; margin-left: auto; flex-shrink: 0; align-self: flex-start; }
.post-menu .act-btn { height: 30px; width: 30px; padding: 0; justify-content: center; }
.post-text { font-size: 15px; line-height: 1.6; white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; color: var(--text); margin-bottom: 4px; }
.mention { color: var(--mention); text-decoration: none; font-weight: 600; }
.mention:hover { text-decoration: underline; }
.ext-link { color: var(--accent); text-decoration: none; word-break: break-all; transition: opacity .15s ease; }
.ext-link:hover { opacity: .75; text-decoration: underline; }
.read-more { display: inline-block; margin-top: 6px; color: var(--accent); text-decoration: none; font-size: 13.5px; font-weight: 600; cursor: pointer; }
.read-more:hover { text-decoration: underline; }

.og-card { display: block; margin-top: 12px; background: var(--card-2); border-radius: 14px; text-decoration: none; color: inherit; overflow: hidden; border: 1px solid var(--line); transition: background .15s ease, border-color .15s ease; }
.og-card:hover { background: var(--hover-2); border-color: var(--line-2); }
.og-image { width: 100%; height: 200px; overflow: hidden; background: var(--card-3); position: relative; }
.og-image img { width: 100%; height: 100%; object-fit: cover; display: block; }
.og-fallback { display: flex; align-items: center; justify-content: center; width: 100%; height: 100%; color: var(--muted); font-size: 13px; text-align: center; padding: 20px; background: var(--card-3); }
.og-body { padding: 12px 14px; }
.og-site { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .6px; font-weight: 700; margin-bottom: 4px; }
.og-title { font-size: 14.5px; font-weight: 700; color: var(--text); margin-bottom: 4px; word-wrap: break-word; overflow-wrap: anywhere; line-height: 1.35; }
.og-desc { font-size: 13px; color: var(--muted); line-height: 1.5; word-wrap: break-word; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

.quoted-post { margin-top: 12px; padding: 12px 14px; background: var(--card-2); border-radius: 12px; border-left: 3px solid var(--accent); cursor: pointer; transition: background .15s ease; }
.quoted-post:hover { background: var(--hover-2); }
.quoted-post .q-author { font-size: 12.5px; font-weight: 700; color: var(--text); margin-bottom: 4px; }
.quoted-post .q-author a { color: var(--text); text-decoration: none; }
.quoted-post .q-text { font-size: 13.5px; color: var(--text-2); line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; max-height: 120px; overflow: hidden; }

.post-actions { display: flex; align-items: center; gap: 2px; margin-top: 10px; margin-left: -8px; flex-wrap: wrap; }
.act-btn { display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 10px; background: transparent; border: none; color: var(--muted); cursor: pointer; font-family: inherit; font-size: 12.5px; font-weight: 600; border-radius: 9px; transition: background .15s ease, color .15s ease; }
.act-btn:hover { background: var(--hover); color: var(--text); }
.act-btn svg { display: block; }
.act-btn.danger:hover { color: var(--danger); }
.like-btn { display: inline-flex; align-items: center; gap: 6px; }
.like-btn:hover { color: var(--like); }
.like-btn.active { color: var(--like); }
.like-btn .num { font-variant-numeric: tabular-nums; }
.views-badge { margin-left: auto; color: var(--muted-2); font-size: 12.5px; display: inline-flex; align-items: center; gap: 5px; padding: 0 8px; }

.not-found { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 80px 20px; gap: 18px; background: var(--card); border: 1px solid var(--line); border-radius: 20px; min-height: 320px; }
.not-found-code { font-size: 76px; font-weight: 900; line-height: 1; color: var(--line-3); letter-spacing: -3px; }
.not-found-text { font-size: 15px; color: var(--muted); text-align: center; }

.user-row { display: flex; align-items: center; gap: 14px; background: var(--card); border: 1px solid var(--line); padding: 14px 16px; border-radius: 14px; margin-bottom: 8px; cursor: pointer; transition: background .15s ease, border-color .15s ease; }
.user-row:hover { background: var(--hover-2); border-color: var(--line-2); }
.user-row .info { flex: 1; min-width: 0; }
.user-row .nick { font-weight: 700; color: var(--text); font-size: 15px; display: block; text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.user-row .nick:hover { color: var(--accent); }
.user-row .name { font-size: 13px; color: var(--muted); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.notif-row { display: flex; align-items: flex-start; gap: 12px; background: var(--card); border: 1px solid var(--line); padding: 14px 16px; border-radius: 14px; margin-bottom: 8px; text-decoration: none; color: inherit; transition: background .15s ease, border-color .15s ease; position: relative; }
.notif-row.unread { background: var(--card-2); border-color: var(--accent-soft-2); }
.notif-row.unread::before { content: ''; position: absolute; left: -1px; top: 12px; bottom: 12px; width: 3px; background: var(--accent); border-radius: 3px; }
.notif-row:hover { background: var(--hover-2); }
.notif-row .info { flex: 1; min-width: 0; }
.notif-row .line { font-size: 14px; line-height: 1.45; color: var(--text-2); }
.notif-row .line b { font-weight: 700; color: var(--text); }
.notif-row .snippet { margin-top: 8px; padding: 8px 12px; background: var(--card-3); border-radius: 10px; font-size: 13px; color: var(--muted); white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; line-height: 1.5; }
.notif-row .time { font-size: 12px; color: var(--muted-2); margin-top: 6px; }

.settings-layout { display: flex; gap: 24px; max-width: 100%; margin: 0 auto; width: 100%; padding: 4px 24px 40px; min-height: 100%; }
.settings-nav { flex: 0 0 200px; display: flex; flex-direction: column; gap: 2px; padding-top: 4px; }
.settings-nav-btn { display: flex; align-items: center; gap: 12px; padding: 11px 14px; text-align: left; background: transparent; border: none; color: var(--text-2); cursor: pointer; border-radius: 10px; font: inherit; font-size: 14.5px; font-weight: 500; transition: background .15s ease, color .15s ease; }
.settings-nav-btn:hover { background: var(--hover); color: var(--text); }
.settings-nav-btn.active { background: var(--accent-soft); color: var(--text); font-weight: 600; }
.settings-nav-btn.active svg { color: var(--accent); }
.settings-nav-btn svg { color: currentColor; opacity: .8; flex-shrink: 0; }
.settings-content { flex: 1; min-width: 0; }
.settings-block { background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 20px; margin-bottom: 16px; }
.settings-block h2 { font-size: 15px; font-weight: 800; margin: 0 0 4px; color: var(--text); letter-spacing: -0.1px; }
.settings-subhead { font-size: 11.5px; font-weight: 800; letter-spacing: .6px; text-transform: uppercase; color: var(--muted); margin: 20px 0 4px; }
.settings-subhead:first-child { margin-top: 0; }
.opt-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
.opt { height: 38px; padding: 0 18px; background: var(--card-2); color: var(--text-2); border: 1px solid var(--line); border-radius: 10px; font-family: inherit; font-size: 13.5px; font-weight: 600; cursor: pointer; transition: background .15s ease, color .15s ease, border-color .15s ease; }
.opt:hover { background: var(--hover); color: var(--text); }
.opt.active { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
.toggle-row { display: flex; align-items: center; justify-content: space-between; padding: 13px 0; font-size: 14.5px; gap: 16px; }
.toggle-row + .toggle-row { border-top: 1px solid var(--line); }
.toggle { position: relative; width: 42px; height: 24px; background: var(--card-3); cursor: pointer; border: 1px solid var(--line-2); flex-shrink: 0; border-radius: 12px; transition: background .2s ease, border-color .2s ease; padding: 0; }
.toggle::after { content: ''; position: absolute; left: 2px; top: 1px; width: 18px; height: 18px; background: #fff; transition: transform .2s ease; border-radius: 50%; box-shadow: 0 1px 3px rgba(0,0,0,.25); }
.toggle.on { background: var(--accent); border-color: var(--accent); }
.toggle.on::after { transform: translateX(18px); }
.settings-desc { font-size: 13px; line-height: 1.55; color: var(--muted); margin: 0 0 14px; padding-top: 4px; }
.settings-link { display: inline-block; color: var(--accent); text-decoration: none; font-size: 14px; font-weight: 600; }
.settings-link:hover { text-decoration: underline; }
.device-info { display: flex; flex-direction: column; gap: 6px; }
.device-info-row { display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: var(--card-2); border-radius: 10px; font-size: 13.5px; gap: 12px; }
.device-info-row .label { color: var(--muted); font-weight: 500; display: inline-flex; align-items: center; gap: 8px; flex-shrink: 0; }
.device-info-row .value { color: var(--text); font-weight: 700; text-align: right; word-break: break-word; }
.color-swatches { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; padding-top: 4px; }
.color-swatch { width: 34px; height: 34px; border-radius: 50%; border: 2px solid transparent; cursor: pointer; padding: 0; position: relative; transition: transform .15s ease, border-color .15s ease, box-shadow .15s ease; box-shadow: 0 0 0 1px var(--line-2); }
.color-swatch:hover { transform: scale(1.1); }
.color-swatch.active { border-color: var(--bg); box-shadow: 0 0 0 2px var(--text); }
.account-hero { display: flex; align-items: center; gap: 14px; padding: 14px; background: var(--card-2); border-radius: 14px; margin-bottom: 16px; }
.account-hero-avatar { flex-shrink: 0; }
.account-hero-info { flex: 1; min-width: 0; }
.account-hero-name { font-size: 17px; font-weight: 800; color: var(--text); margin-bottom: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.account-hero-nick { font-size: 13px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.account-emoji-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(42px, 1fr)); gap: 4px; max-height: 180px; overflow-y: auto; padding: 6px; background: var(--card-2); border-radius: 12px; }
.account-emoji-opt { aspect-ratio: 1 / 1; background: transparent; border: 2px solid transparent; border-radius: 10px; font-size: 22px; line-height: 1; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background .15s ease, border-color .15s ease, transform .1s ease; padding: 0; }
.account-emoji-opt:hover { background: var(--hover); transform: scale(1.05); }
.account-emoji-opt.active { background: var(--accent-soft); border-color: var(--accent); }
.settings-list-mobile { display: flex; flex-direction: column; gap: 8px; padding: 4px 16px 40px; max-width: 680px; margin: 0 auto; width: 100%; }
.settings-list-row { display: flex; align-items: center; gap: 14px; background: var(--card); border: 1px solid var(--line); padding: 16px; border-radius: 16px; cursor: pointer; text-align: left; color: var(--text); transition: background .15s ease, border-color .15s ease; width: 100%; font-family: inherit; }
.settings-list-row:hover { background: var(--hover-2); border-color: var(--line-2); }
.settings-list-row .sl-icon { width: 40px; height: 40px; flex-shrink: 0; border-radius: 12px; background: var(--accent-soft); display: inline-flex; align-items: center; justify-content: center; color: var(--accent); }
.settings-list-row .sl-icon svg { width: 20px; height: 20px; }
.settings-list-row .sl-info { flex: 1; min-width: 0; }
.settings-list-row .sl-title { font-size: 15px; font-weight: 700; color: var(--text); margin-bottom: 2px; }
.settings-list-row .sl-desc { font-size: 12.5px; color: var(--muted); line-height: 1.3; }
.settings-list-row .sl-chevron { color: var(--muted-2); flex-shrink: 0; }
.emoji-current { display: flex; align-items: center; gap: 14px; padding: 14px 16px; background: var(--card-2); border-radius: 12px; margin-bottom: 14px; }
.emoji-current .preview { font-size: 36px; line-height: 1; }
.emoji-current .label-wrap { display: flex; flex-direction: column; gap: 2px; }
.emoji-current .label-cap { font-size: 11px; color: var(--muted-2); text-transform: uppercase; letter-spacing: .5px; font-weight: 700; }
.emoji-current .label { font-size: 15px; color: var(--text); font-weight: 700; }
.emoji-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(46px, 1fr)); gap: 4px; max-height: 360px; overflow-y: auto; padding: 6px; background: var(--card-2); border-radius: 12px; }
.emoji-opt { aspect-ratio: 1 / 1; background: transparent; border: 2px solid transparent; border-radius: 10px; font-size: 24px; line-height: 1; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background .15s ease, border-color .15s ease, transform .1s ease; padding: 0; }
.emoji-opt:hover { background: var(--hover); transform: scale(1.05); }
.emoji-opt.active { background: var(--accent-soft); border-color: var(--accent); }
.boot-screen { min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 20px; background: var(--bg); padding: 24px; }
.boot-logo { width: 68px; height: 68px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; background: var(--accent); color: var(--accent-fg); font-weight: 900; font-size: 24px; letter-spacing: -1px; animation: bootPulse 1.8s ease-in-out infinite; box-shadow: 0 8px 24px var(--accent-soft-2); }
@keyframes bootPulse { 0%,100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.06); opacity: .85; } }
.boot-text { color: var(--muted); font-size: 13.5px; }
.auth-page { position: relative; min-height: 100vh; min-height: 100dvh; display: flex; align-items: center; justify-content: center; padding: 24px; background: var(--bg); overflow: hidden; width: 100%; }
.auth-page::before { content: ''; position: absolute; width: 60vmax; height: 60vmax; top: -30vmax; left: -20vmax; border-radius: 50%; background: radial-gradient(circle, var(--accent-soft-2) 0%, transparent 65%); filter: blur(60px); pointer-events: none; z-index: 0; }
.auth-page::after { content: ''; position: absolute; width: 50vmax; height: 50vmax; bottom: -25vmax; right: -15vmax; border-radius: 50%; background: radial-gradient(circle, var(--accent-soft) 0%, transparent 65%); filter: blur(60px); pointer-events: none; z-index: 0; }
.auth-card { position: relative; z-index: 1; width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--line); border-radius: 24px; padding: 32px 28px 28px; box-shadow: var(--shadow-lg); }
.auth-topbar { position: absolute; top: 14px; left: 14px; right: 14px; display: flex; align-items: center; justify-content: space-between; pointer-events: none; }
.auth-topbar > * { pointer-events: auto; }
.auth-topbar-btn { height: 34px; min-width: 34px; padding: 0 10px; background: transparent; border: 1px solid transparent; color: var(--muted); cursor: pointer; border-radius: 10px; display: inline-flex; align-items: center; justify-content: center; gap: 6px; transition: background .15s ease, color .15s ease, border-color .15s ease; font-weight: 700; font-size: 12px; letter-spacing: .5px; font-family: inherit; }
.auth-topbar-btn:hover { background: var(--hover); color: var(--text); border-color: var(--line-2); }
.auth-topbar-btn svg { display: block; }
.auth-lang-btn { font-size: 11px; font-weight: 900; letter-spacing: .8px; }
.auth-brand { display: flex; flex-direction: column; align-items: center; gap: 10px; margin-bottom: 22px; padding-top: 12px; }
.auth-brand-logo { width: 64px; height: 64px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; background: var(--accent); color: var(--accent-fg); font-weight: 900; font-size: 22px; letter-spacing: -1px; box-shadow: 0 8px 24px var(--accent-soft-2); }
.auth-brand-name { font-size: 18px; font-weight: 800; color: var(--text); letter-spacing: -0.3px; }
.auth-brand-tagline { font-size: 13px; color: var(--muted); text-align: center; line-height: 1.5; max-width: 320px; }
.auth-pill-tabs { margin-bottom: 18px; }
.auth-pill-tabs .pill-tab { font-size: 13px; padding: 10px 8px; }
.auth-field { position: relative; margin-bottom: 10px; }
.auth-field .auth-input-icon { position: absolute; left: 14px; top: 50%; transform: translateY(-50%); color: var(--muted); pointer-events: none; display: inline-flex; }
.auth-field input { width: 100%; padding: 14px 44px 14px 46px; background: var(--card-2); border: 1px solid var(--line); color: var(--text); font-family: inherit; font-size: 15px; outline: none; border-radius: 12px; transition: border-color .15s ease, background .15s ease, box-shadow .15s ease; }
.auth-field input:focus { border-color: var(--accent); background: var(--card); box-shadow: 0 0 0 3px var(--accent-soft); }
.auth-field input::placeholder { color: var(--muted); }
.auth-field.no-right input { padding-right: 14px; }
.auth-field-btn { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); width: 34px; height: 34px; background: transparent; border: none; color: var(--muted); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; border-radius: 9px; transition: background .15s ease, color .15s ease; }
.auth-field-btn:hover { background: var(--hover); color: var(--text); }
.auth-field-btn:disabled { opacity: .5; cursor: default; }
.auth-field-btn.success { color: var(--ok); }
.auth-field-btn.error { color: var(--danger); }
.nick-status { font-size: 12.5px; margin: -4px 4px 10px; min-height: 18px; line-height: 1.4; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.nick-status.ok { color: var(--ok); }
.nick-status.err { color: var(--danger); }
.nick-status svg { flex-shrink: 0; }
.auth-btn { margin-top: 8px; width: 100%; height: 50px; background: var(--accent); color: var(--accent-fg); border: none; border-radius: 12px; font-family: inherit; font-size: 15px; font-weight: 700; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 10px; transition: opacity .15s ease, transform .1s ease; }
.auth-btn:hover { opacity: .92; }
.auth-btn:active { transform: scale(.99); }
.auth-btn:disabled { opacity: .55; cursor: default; }
.auth-btn-spinner { width: 16px; height: 16px; border: 2px solid rgba(255,255,255,.4); border-top-color: #fff; border-radius: 50%; animation: spin .7s linear infinite; }
.auth-error { color: var(--danger); font-size: 13px; min-height: 18px; margin: 6px 2px; line-height: 1.4; padding: 0 2px; }
.session-banner { display: flex; align-items: center; gap: 10px; padding: 12px 14px; background: var(--accent-soft); border: 1px solid var(--accent-soft-2); border-radius: 12px; color: var(--text-2); font-size: 13.5px; margin-bottom: 16px; line-height: 1.45; }
.session-banner svg { color: var(--accent); flex-shrink: 0; }
.toast-container { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 2000; display: flex; flex-direction: column; gap: 8px; pointer-events: none; max-width: 90vw; }
.toast { background: var(--card); border: 1px solid var(--line-2); border-radius: 12px; padding: 10px 16px; font-size: 13.5px; font-weight: 600; color: var(--text); box-shadow: var(--shadow-lg); display: inline-flex; align-items: center; gap: 10px; pointer-events: auto; animation: toastIn .25s ease; }
@keyframes toastIn { from { opacity: 0; transform: translateY(-12px); } to { opacity: 1; transform: translateY(0); } }
.toast svg { flex-shrink: 0; }
.toast.success svg { color: var(--ok); }
.toast.error svg { color: var(--danger); }
.policy { max-width: 720px; margin: 0 auto; padding: 12px 28px 80px; }
.policy-h1 { font-size: 24px; font-weight: 800; letter-spacing: -0.4px; margin: 0 0 12px; color: var(--text); padding-bottom: 14px; border-bottom: 1px solid var(--line); }
.policy-h2 { font-size: 17px; font-weight: 800; letter-spacing: -0.2px; margin: 32px 0 10px; color: var(--text); display: flex; align-items: baseline; gap: 10px; }
.policy-h2::before { content: ''; width: 4px; height: 16px; background: var(--accent); border-radius: 2px; display: inline-block; flex-shrink: 0; align-self: center; }
.policy p { font-size: 14.5px; line-height: 1.75; color: var(--text-2); margin: 0 0 12px; }
.policy-list { list-style: none; padding: 0; margin: 4px 0 16px; display: flex; flex-direction: column; gap: 6px; }
.policy-list li { position: relative; padding-left: 22px; font-size: 14px; line-height: 1.65; color: var(--text-2); }
.policy-list li::before { content: ''; position: absolute; left: 6px; top: 10px; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); opacity: .8; }
.policy-gap { height: 4px; }
.policy-footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--line); font-size: 12.5px; color: var(--muted-2); text-align: center; line-height: 1.6; }
.empty { padding: 56px 20px; text-align: center; color: var(--muted); font-size: 13.5px; background: var(--card); border: 1px dashed var(--line-2); border-radius: 16px; }
.spinner-wrap { padding: 60px 0; text-align: center; }
.spinner { display: inline-block; width: 26px; height: 26px; border: 2.5px solid var(--line-2); border-top-color: var(--accent); animation: spin .7s linear infinite; border-radius: 50%; }
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.55); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); display: flex; align-items: center; justify-content: center; z-index: 1000; padding: 20px; animation: fadeIn .14s ease; }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
.modal { background: var(--card); border: 1px solid var(--line-2); border-radius: 18px; padding: 24px; max-width: 380px; width: 100%; box-shadow: var(--shadow-lg); animation: popIn .16s ease; }
@keyframes popIn { from { opacity: 0; transform: scale(.96); } to { opacity: 1; transform: scale(1); } }
.modal-text { font-size: 15px; line-height: 1.55; color: var(--text); margin-bottom: 20px; text-align: center; }
.modal-actions { display: flex; gap: 8px; }
.modal-btn { flex: 1; height: 48px; font-family: inherit; font-size: 15px; font-weight: 700; cursor: pointer; border: none; border-radius: 11px; transition: opacity .15s ease, background .15s ease; }
.modal-btn.secondary { background: var(--card-2); color: var(--text); border: 1px solid var(--line); }
.modal-btn.secondary:hover { background: var(--hover-2); }
.modal-btn.danger { background: var(--danger); color: #fff; }
.modal-btn.danger:hover { opacity: .9; }
.modal.welcome { max-width: 520px; padding: 28px 26px 24px; }
.welcome-title { font-size: 22px; font-weight: 800; letter-spacing: -0.3px; text-align: center; margin: 0 0 14px; color: var(--text); }
.welcome-text { font-size: 14.5px; line-height: 1.7; color: var(--text-2); white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; margin-bottom: 22px; max-height: 55vh; overflow-y: auto; padding-right: 6px; }
.welcome-btn { width: 100%; height: 52px; background: var(--accent); color: var(--accent-fg); border: none; border-radius: 12px; font-family: inherit; font-size: 15px; font-weight: 700; cursor: pointer; transition: opacity .15s ease, transform .1s ease; }
.welcome-btn:hover { opacity: .92; }
.welcome-btn:active { transform: scale(.99); }
.comment.highlight { animation: flash 1.6s ease-out; }
@keyframes flash { 0% { background: var(--accent-soft-2); } 100% { background: var(--card-2); } }
.comment.pending { opacity: .65; }

/* ====== Comments tree ====== */
.comments { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line); }
.comment { padding: 10px 14px; background: var(--card-2); border-radius: 12px; margin-top: 6px; }
.comment.is-author { box-shadow: inset 0 0 0 1px var(--accent-soft-2); }

/* Reply now uses the SAME card style as a normal comment + L-shaped connector */
.comment.reply {
  position: relative;
  margin-left: 34px;
  margin-top: 6px;
  padding: 10px 14px;
  background: var(--card-2);
  border-radius: 12px;
}
/* Vertical + horizontal line (corner) from the parent comment */
.comment.reply::before {
  content: '';
  position: absolute;
  left: -22px;
  top: -8px;
  width: 18px;
  height: 20px;
  border-left: 2px solid var(--line-3);
  border-bottom: 2px solid var(--line-3);
  border-bottom-left-radius: 10px;
  pointer-events: none;
}
/* Small arrowhead at the end of the horizontal line */
.comment.reply::after {
  content: '';
  position: absolute;
  left: -5px;
  top: 7px;
  width: 0;
  height: 0;
  border-top: 4px solid transparent;
  border-bottom: 4px solid transparent;
  border-left: 5px solid var(--line-3);
  pointer-events: none;
}

.comment-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
.comment-author { font-size: 13px; font-weight: 700; color: var(--text); text-decoration: none; }
.comment-author:hover { color: var(--accent); }
.comment-author-badge { font-size: 9.5px; font-weight: 800; text-transform: uppercase; letter-spacing: .5px; padding: 2px 7px; border-radius: 5px; background: var(--accent-soft); color: var(--accent); }
.comment-time { font-size: 11.5px; color: var(--muted-2); margin-left: auto; }
.comment-text { font-size: 13.5px; line-height: 1.55; white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; color: var(--text); margin-bottom: 4px; }
.comment-actions { display: flex; align-items: center; gap: 0; flex-wrap: wrap; margin-left: -6px; }
.comment-actions .act-btn { height: 26px; padding: 0 8px; font-size: 11.5px; }
.inline-editor { margin-top: 8px; }
.inline-editor textarea { width: 100%; padding: 12px 14px; min-height: 90px; border: 1px solid var(--line); background: var(--card-2); color: var(--text); font-family: inherit; font-size: 15px; line-height: 1.5; outline: none; resize: none; border-radius: 12px; transition: border-color .15s ease; }
.inline-editor textarea:focus { border-color: var(--accent); }
.inline-editor .edit-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
.inline-editor .edit-actions button { height: 36px; padding: 0 14px; font-family: inherit; font-size: 13.5px; font-weight: 700; cursor: pointer; border: 1px solid var(--line); border-radius: 10px; background: var(--card-2); color: var(--text); transition: background .15s ease; }
.inline-editor .edit-actions button:hover { background: var(--hover-2); }
.inline-editor .edit-actions button.edit-save { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
.edit-bio-textarea { padding: 14px 16px; background: var(--card-2); border: 1px solid var(--line); color: var(--text); font-family: inherit; font-size: 15px; outline: none; border-radius: 12px; min-height: 60px; max-height: 200px; resize: none; line-height: 1.5; transition: border-color .15s ease; }
.edit-bio-textarea:focus { border-color: var(--accent); }

@media (max-width: 1100px) and (min-width: 901px) {
  .layout { max-width: 100%; }
  .sidebar { flex: 0 0 76px; width: 76px; padding: 20px 8px 16px; }
  .sidebar .logo { font-size: 16px; padding: 0 6px 18px; text-align: center; letter-spacing: -0.3px; }
  .nav-btn { flex-direction: column; gap: 4px; padding: 10px 4px; font-size: 10.5px; justify-content: center; align-items: center; text-align: center; }
  .nav-btn span { font-size: 10.5px; line-height: 1; word-break: break-word; }
  .nav-btn .badge { position: absolute; top: 2px; right: 6px; min-width: 16px; height: 16px; line-height: 16px; font-size: 9.5px; padding: 0 4px; }
  .sidebar-logout { flex-direction: column; gap: 4px; padding: 10px 4px; font-size: 10.5px; justify-content: center; align-items: center; text-align: center; border-radius: 10px; }
  .sidebar-logout span { font-size: 10.5px; line-height: 1.1; word-break: break-word; white-space: normal; }
  .sidebar-logout svg { width: 20px; height: 20px; }
}
@media (max-width: 900px) {
  .layout { flex-direction: column; max-width: 100%; }
  .main { order: 1; height: calc(100vh - 64px - env(safe-area-inset-bottom, 0px)); height: calc(100dvh - 64px - env(safe-area-inset-bottom, 0px)); }
  .sidebar { order: 2; width: 100%; height: calc(64px + env(safe-area-inset-bottom, 0px)); flex-direction: row; border-top: 1px solid var(--line); padding: 0 0 env(safe-area-inset-bottom, 0px); flex: 0 0 auto; background: var(--bg-elev); }
  .sidebar .logo { display: none; }
  .sidebar .spacer { display: none; }
  .sidebar-logout { display: none; }
  .nav { flex-direction: row; flex: 1; justify-content: space-around; align-items: stretch; }
  .nav-btn { flex-direction: column; gap: 2px; padding: 8px 2px; flex: 1; justify-content: center; align-items: center; text-align: center; border-radius: 0; min-height: 62px; }
  .nav-btn span { font-size: 10px; line-height: 1; font-weight: 500; }
  .nav-btn svg { width: 22px; height: 22px; }
  .nav-btn.active { background: transparent; }
  .nav-btn.active svg { color: var(--accent); }
  .nav-btn.active span { color: var(--text); }
  .nav-btn .badge { position: absolute; top: 4px; right: 22%; min-width: 16px; height: 16px; line-height: 16px; font-size: 9.5px; padding: 0 4px; border-radius: 8px; }
  .main-header { padding: 14px 16px 8px; gap: 6px; }
  .main-header .title { font-size: 19px; }
  .main-inner { padding: 4px 12px 40px; }
  .card, .post-card, .profile-hero { border-radius: 16px; padding: 14px; }
  .profile-hero { padding: 16px; }
  .profile-name { font-size: 19px; }
  .profile-hero-row { margin-bottom: 10px; gap: 12px; }
  .avatar.lg { width: 64px; height: 64px; font-size: 36px; }
  .profile-line { gap: 6px; margin-bottom: 8px; }
  .profile-stats { font-size: 12px; gap: 14px; }
  .profile-meta { margin-top: 6px; font-size: 11.5px; }
  .composer-avatar-row { gap: 10px; }
  .composer-avatar-col { gap: 6px; }
  .composer-body textarea { min-height: 40px; font-size: 15px; }
  .publish-btn-mobile { display: inline-flex !important; align-items: center; justify-content: center; width: 40px; height: 40px; padding: 0; border-radius: 12px; }
  .publish-btn-mobile svg { width: 18px; height: 18px; }
  .publish-btn-desktop { display: none !important; }
  .composer-hint { display: none; }
  .composer-actions { gap: 8px; }
  /* On narrow screens keep reply connector compact */
  .comment.reply { margin-left: 26px; }
  .comment.reply::before { left: -18px; width: 16px; height: 18px; }
  .comment.reply::after { left: -5px; top: 6px; }
  .settings-layout { flex-direction: column; padding: 4px 12px 40px; gap: 12px; }
  .settings-nav { display: none; }
  .settings-block { padding: 16px; border-radius: 14px; }
  .modal { padding: 22px 18px 18px; border-radius: 16px; max-width: 100%; }
  .modal-text { font-size: 15.5px; margin-bottom: 20px; }
  .modal-actions { flex-direction: column-reverse; gap: 10px; }
  .modal-btn { height: 56px; font-size: 16px; border-radius: 14px; flex: 0 0 auto; width: 100%; font-weight: 700; }
  .modal.welcome { padding: 24px 20px 20px; border-radius: 18px; }
  .welcome-title { font-size: 20px; margin-bottom: 12px; }
  .welcome-text { font-size: 14.5px; line-height: 1.65; max-height: 58vh; margin-bottom: 20px; }
  .welcome-btn { height: 56px; font-size: 16px; border-radius: 14px; }
  .emoji-grid { grid-template-columns: repeat(auto-fill, minmax(42px, 1fr)); gap: 3px; max-height: 300px; }
  .emoji-opt { font-size: 22px; }
  .og-image { height: 160px; }
  .not-found { padding: 60px 16px; min-height: 260px; }
  .not-found-code { font-size: 64px; }
  .post-time::before { content: ''; margin-right: 0; }
  .post-header .who { gap: 8px; }
  .policy { padding: 12px 18px 60px; }
  .policy-h1 { font-size: 21px; }
  .policy-h2 { font-size: 16px; margin-top: 26px; }
  .auth-page { padding: 16px; }
  .auth-card { padding: 30px 22px 24px; border-radius: 22px; }
  .auth-topbar { top: 10px; left: 10px; right: 10px; }
  .auth-topbar-btn { height: 32px; min-width: 32px; padding: 0 8px; }
  .auth-brand { padding-top: 16px; gap: 8px; margin-bottom: 18px; }
  .auth-brand-logo { width: 58px; height: 58px; font-size: 20px; }
  .auth-brand-name { font-size: 17px; }
  .auth-brand-tagline { font-size: 12.5px; }
  .auth-field input { padding: 13px 42px 13px 44px; font-size: 15px; }
  .auth-btn { height: 50px; font-size: 15px; }
}
@media (max-width: 500px) {
  .main-inner { padding: 4px 10px 30px; }
  .card, .post-card { border-radius: 14px; padding: 12px; }
  .profile-hero { border-radius: 14px; padding: 14px; }
  .profile-stats { font-size: 12px; gap: 12px; }
  .og-image { height: 140px; }
  .settings-layout { padding: 4px 10px 30px; }
  .settings-list-mobile { padding: 4px 10px 30px; }
  .policy { padding: 12px 14px 60px; }
  .modal { padding: 20px 16px 16px; border-radius: 16px; }
  .modal-btn { height: 58px; font-size: 16px; }
  .modal.welcome { padding: 22px 18px 18px; }
  .welcome-title { font-size: 19px; }
  .welcome-text { font-size: 14px; line-height: 1.65; max-height: 60vh; }
  .welcome-btn { height: 58px; font-size: 16px; }
  .auth-page { padding: 10px; align-items: stretch; }
  .auth-card { padding: 28px 18px 22px; border-radius: 20px; max-width: 100%; margin: auto 0; align-self: center; }
  .auth-topbar { top: 8px; left: 8px; right: 8px; }
  .auth-topbar-btn { height: 30px; min-width: 30px; padding: 0 8px; font-size: 11px; }
  .auth-brand { padding-top: 18px; gap: 6px; margin-bottom: 16px; }
  .auth-brand-logo { width: 52px; height: 52px; font-size: 18px; }
  .auth-brand-name { font-size: 16px; }
  .auth-brand-tagline { font-size: 12px; max-width: 280px; }
  .auth-field input { padding: 12px 40px 12px 42px; font-size: 15px; border-radius: 11px; }
  .auth-field .auth-input-icon { left: 12px; }
  .auth-field-btn { right: 4px; width: 32px; height: 32px; }
  .auth-btn { height: 48px; font-size: 14.5px; }
  .auth-pill-tabs .pill-tab { font-size: 12.5px; padding: 9px 6px; }
  .session-banner { font-size: 12.5px; padding: 10px 12px; }
}
"""


# ============================================================================
# JS
# ============================================================================
JS = r"""
var ICONS = {
  home: __I_HOME__, users: __I_USERS__, user: __I_USER__, userLg: __I_USER_LG__,
  bell: __I_BELL__, gear: __I_GEAR__,
  logout: __I_LOGOUT__, login: __I_LOGIN__, plus: __I_PLUS__,
  send: __I_SEND__,
  heart: __I_HEART__, heart_filled: __I_HEART_FILLED__, comment: __I_COMMENT__,
  copy: __I_COPY__, edit: __I_EDIT__, trash: __I_TRASH__,
  back: __I_BACK__, chevron: __I_CHEVRON__, search: __I_SEARCH__,
  moon: __I_MOON__, sun: __I_SUN__, quote: __I_QUOTE__,
  cal: __I_CAL__, check: __I_CHECK__, checkLg: __I_CHECK_LG__, x: __I_X__,
  mobile: __I_MOBILE__, desktop: __I_DESKTOP__, server: __I_SERVER__,
  eye: __I_EYE__, eyeOff: __I_EYE_OFF__, lock: __I_LOCK__, at: __I_AT__,
  info: __I_INFO__, refresh: __I_REFRESH__, globe: __I_GLOBE__
};
var EMOJI_NAMES = __EMOJI_NAMES__;
var EMOJIS = Object.keys(EMOJI_NAMES);
var DEFAULT_EMOJI = '😀';
var SITE_NAME = (LANG === 'ru') ? 'СЛД' : 'SLD';
var FAVICON_RU_DARK  = "__FAVICON_RU_DARK__";
var FAVICON_RU_LIGHT = "__FAVICON_RU_LIGHT__";
var FAVICON_EN_DARK  = "__FAVICON_EN_DARK__";
var FAVICON_EN_LIGHT = "__FAVICON_EN_LIGHT__";
function faviconFor(theme) {
  if (LANG === 'ru') return theme === 'light' ? FAVICON_RU_LIGHT : FAVICON_RU_DARK;
  return theme === 'light' ? FAVICON_EN_LIGHT : FAVICON_EN_DARK;
}
var COLOR_PRESETS = {
  accent: [
    { id:'blue',ru:'Синий',en:'Blue',dark:'#3b82f6',light:'#2563eb' },
    { id:'green',ru:'Зелёный',en:'Green',dark:'#22c55e',light:'#16a34a' },
    { id:'purple',ru:'Фиолетовый',en:'Purple',dark:'#a855f7',light:'#9333ea' },
    { id:'pink',ru:'Розовый',en:'Pink',dark:'#ec4899',light:'#db2777' },
    { id:'orange',ru:'Оранжевый',en:'Orange',dark:'#f97316',light:'#ea580c' },
    { id:'teal',ru:'Бирюзовый',en:'Teal',dark:'#14b8a6',light:'#0d9488' },
    { id:'indigo',ru:'Индиго',en:'Indigo',dark:'#6366f1',light:'#4f46e5' },
  ],
  like: [
    { id:'red',ru:'Красный',en:'Red',dark:'#ef4444',light:'#dc2626' },
    { id:'pink',ru:'Розовый',en:'Pink',dark:'#ec4899',light:'#db2777' },
    { id:'orange',ru:'Оранжевый',en:'Orange',dark:'#f97316',light:'#ea580c' },
    { id:'purple',ru:'Фиолетовый',en:'Purple',dark:'#a855f7',light:'#9333ea' },
    { id:'blue',ru:'Синий',en:'Blue',dark:'#3b82f6',light:'#2563eb' },
  ]
};
function hexToRgb(hex) {
  hex = hex.replace('#','');
  if (hex.length === 3) hex = hex.split('').map(function(c){return c+c;}).join('');
  return [parseInt(hex.slice(0,2),16),parseInt(hex.slice(2,4),16),parseInt(hex.slice(4,6),16)];
}
function rgba(hex,a){var c=hexToRgb(hex);return 'rgba('+c[0]+','+c[1]+','+c[2]+','+a+')';}
function loadColors(){try{return JSON.parse(localStorage.getItem('SLD_colors')||'{}')||{};}catch(e){return {};}}
function saveColors(c){localStorage.setItem('SLD_colors',JSON.stringify(c));}
function applyColors() {
  var c = loadColors();
  var theme = document.documentElement.getAttribute('data-theme') || 'dark';
  var root = document.documentElement.style;
  var accentVal = null;
  if (c.accent) {
    var p = COLOR_PRESETS.accent.find(function(x){return x.id === c.accent;});
    if (p) accentVal = theme === 'dark' ? p.dark : p.light;
  }
  if (accentVal) {
    root.setProperty('--accent', accentVal);
    root.setProperty('--accent-fg', '#ffffff');
    root.setProperty('--accent-soft', rgba(accentVal, theme==='dark'?0.14:0.10));
    root.setProperty('--accent-soft-2', rgba(accentVal, theme==='dark'?0.22:0.16));
    root.setProperty('--mention', accentVal);
  } else {
    root.removeProperty('--accent'); root.removeProperty('--accent-fg');
    root.removeProperty('--accent-soft'); root.removeProperty('--accent-soft-2');
    root.removeProperty('--mention');
  }
  if (c.like) {
    var pl = COLOR_PRESETS.like.find(function(x){return x.id === c.like;});
    if (pl) root.setProperty('--like', theme==='dark'?pl.dark:pl.light);
  } else root.removeProperty('--like');
}
function setColor(key,id){var c=loadColors();c[key]=id;saveColors(c);applyColors();}
var anonId = localStorage.getItem('SLD_anon_id');
if (!anonId) { anonId = Math.random().toString(36).slice(2,10) + Date.now().toString(36); localStorage.setItem('SLD_anon_id', anonId); }

var state = {
  user: null, token: localStorage.getItem('SLD_token') || null,
  booting: false, sessionExpired: false, bootError: null, authMode: 'login',
  view: VIEW, viewData: VIEW_DATA || {},
  unreadNotif: parseInt(localStorage.getItem('SLD_un')||'0',10)||0,
  feedMode: 'all', peopleTab: 'all', peopleQuery: '', searchQuery: '',
  composerDraft: '', quotePostId: null, quotePreview: null, replyTo: null,
  highlightComment: null, suppressRefresh: 0, settingsSection: 'account',
  settingsOpen: false, es: null, currentEmoji: DEFAULT_EMOJI, whoami: null,
  currentRooms: [], refreshTimer: null, sseErrors: 0, sseWasConnected: false,
  soundEnabled: localStorage.getItem('SLD_sound') !== '0',
  welcomeShown: false
};
var feedCache = { all:{posts:null,query:null}, subs:{posts:null,query:null} };
var feedReqId = 0, peopleReqId = 0, profileReqId = 0, fallbackPollTimer = null;

function setUser(u) { state.user = u; }
function setNotifCount(n) { if (typeof n === 'number') { state.unreadNotif = n; localStorage.setItem('SLD_un', String(n)); } }

function showToast(text, type) {
  var cont = document.getElementById('toastContainer');
  if (!cont) {
    cont = document.createElement('div');
    cont.className = 'toast-container';
    cont.id = 'toastContainer';
    document.body.appendChild(cont);
  }
  var el = document.createElement('div');
  el.className = 'toast' + (type ? ' ' + type : '');
  var icon = '';
  if (type === 'success') icon = ICONS.checkLg;
  else if (type === 'error') icon = ICONS.info;
  el.innerHTML = (icon || '') + '<span>' + escapeHtml(text) + '</span>';
  cont.appendChild(el);
  setTimeout(function(){
    el.style.transition = 'opacity .2s ease, transform .2s ease';
    el.style.opacity = '0';
    el.style.transform = 'translateY(-8px)';
    setTimeout(function(){ if (el.parentNode) el.parentNode.removeChild(el); }, 220);
  }, 3200);
}

function showWelcomeModal() {
  if (state.welcomeShown) return;
  state.welcomeShown = true;
  var modal = document.createElement('div');
  modal.className = 'modal-overlay';
  modal.innerHTML = '<div class="modal welcome">'
    + '<div class="welcome-title">' + escapeHtml(tr('welcome_title')) + '</div>'
    + '<div class="welcome-text">' + escapeHtml(tr('welcome_text')) + '</div>'
    + '<button type="button" class="welcome-btn" data-modal-continue>' + escapeHtml(tr('welcome_continue')) + '</button>'
    + '</div>';
  document.body.appendChild(modal);
  function close() {
    try { localStorage.setItem('SLD_welcome_seen', '1'); } catch(e) {}
    if (modal.parentNode) modal.parentNode.removeChild(modal);
  }
  modal.querySelector('[data-modal-continue]').addEventListener('click', close);
  modal.addEventListener('click', function(e){ if (e.target === modal) close(); });
}
function maybeShowWelcome() {
  if (!state.user || state.welcomeShown) return;
  var seen = '0';
  try { seen = localStorage.getItem('SLD_welcome_seen') || '0'; } catch(e) {}
  if (seen === '1') return;
  setTimeout(showWelcomeModal, 350);
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
    var wasAuthed = !!state.token || !!state.user;
    setUser(null); state.token = null;
    localStorage.removeItem('SLD_token');
    disconnectSSE();
    if (wasAuthed) {
      state.sessionExpired = true; state.booting = false;
      showToast(tr('session_expired'), 'error');
      try { renderRoot(); } catch(e) {}
    }
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
  return escaped.replace(/(^|[^a-zA-Z0-9_])@([a-zA-Z0-9_]{3,20})/g, function(_,pre,nick){
    return pre + '<a class="mention" href="/u/' + encodeURIComponent(nick) + '" data-link>@' + nick + '</a>';
  });
}
function linkifyText(raw) {
  if (!raw) return '';
  var parts = []; var re = /(https?:\/\/[^\s<>"']+)/g; var last = 0, m;
  while ((m = re.exec(raw)) !== null) {
    if (m.index > last) parts.push({type:'text', v: raw.slice(last, m.index)});
    parts.push({type:'url', v: m[0]});
    last = m.index + m[0].length;
  }
  if (last < raw.length) parts.push({type:'text', v: raw.slice(last)});
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
  var m = Math.floor(d/60); if (m < 60) return m + ' ' + tr('min_ago');
  var h = Math.floor(m/60); if (h < 24) return h + ' ' + tr('hour_ago');
  var days = Math.floor(h/24); if (days < 30) return days + ' ' + tr('day_ago');
  return new Date(ts*1000).toLocaleDateString();
}
function fmtDate(ts) {
  try { return new Date(ts*1000).toLocaleDateString(LANG==='ru'?'ru-RU':'en-US', {year:'numeric',month:'long'}); }
  catch(e) { return new Date(ts*1000).toLocaleDateString(); }
}
function fmtUptime(sec) {
  sec = Math.max(0, Math.floor(sec));
  var d = Math.floor(sec/86400), h = Math.floor((sec%86400)/3600);
  var m = Math.floor((sec%3600)/60), s = sec % 60;
  var parts = []; var ru = (LANG === 'ru');
  if (d > 0) parts.push(d + (ru?' д':'d'));
  if (h > 0) parts.push(h + (ru?' ч':'h'));
  if (m > 0) parts.push(m + (ru?' мин':'m'));
  if (!parts.length) parts.push(s + (ru?' с':'s'));
  return parts.join(' ');
}
function truncateText(text) {
  var lines = text.split('\n'); var out = text, truncated = false;
  if (lines.length > TRUNCATE_LINES) { out = lines.slice(0,TRUNCATE_LINES).join('\n'); truncated = true; }
  if (out.length > TRUNCATE_CHARS) { out = out.slice(0,TRUNCATE_CHARS); truncated = true; }
  if (truncated) out = out.replace(/\s+$/, '') + '…';
  return { text: out, truncated: truncated };
}
function spinner() { return '<div class="spinner-wrap"><div class="spinner"></div></div>'; }
function notFoundHtml() {
  return '<div class="not-found"><div class="not-found-code">404</div>'
    + '<div class="not-found-text">' + escapeHtml(tr('page_not_found')) + '</div>'
    + '<a class="pill-action primary" href="/" data-link>' + escapeHtml(tr('back_home')) + '</a></div>';
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
    + '<button type="button" class="modal-btn secondary" data-modal-cancel>' + escapeHtml(noText) + '</button>'
    + '<button type="button" class="modal-btn danger" data-modal-confirm>' + escapeHtml(yesText) + '</button>'
    + '</div></div>';
  document.body.appendChild(modal);
  function close() { if (modal.parentNode) modal.parentNode.removeChild(modal); }
  modal.querySelector('[data-modal-cancel]').addEventListener('click', close);
  modal.querySelector('[data-modal-confirm]').addEventListener('click', function(){ close(); onConfirm(); });
  modal.addEventListener('click', function(e){ if (e.target === modal) close(); });
}
function updateFavicon() {
  var theme = document.documentElement.getAttribute('data-theme') || 'dark';
  var link = document.getElementById('sld-favicon');
  if (link) link.href = faviconFor(theme);
}
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('SLD_theme', theme);
  applyColors(); updateFavicon();
}
function toggleTheme() {
  var cur = document.documentElement.getAttribute('data-theme') || 'dark';
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}
applyTheme(localStorage.getItem('SLD_theme') || 'dark');
applyColors();
updateFavicon();

try {
  var _qid = sessionStorage.getItem('SLD_q_id');
  var _qp = sessionStorage.getItem('SLD_q_prev');
  if (_qid && _qp) { state.quotePostId = _qid; state.quotePreview = JSON.parse(_qp); }
} catch(e) {}
function saveQuoteState() {
  try {
    if (state.quotePostId && state.quotePreview) {
      sessionStorage.setItem('SLD_q_id', state.quotePostId);
      sessionStorage.setItem('SLD_q_prev', JSON.stringify(state.quotePreview));
    } else { sessionStorage.removeItem('SLD_q_id'); sessionStorage.removeItem('SLD_q_prev'); }
  } catch(e) {}
}
function clearQuoteState() { state.quotePostId = null; state.quotePreview = null; saveQuoteState(); }

var _audioCtx = null;
function ensureAudioCtx() {
  if (_audioCtx) return _audioCtx;
  try { var Ctx = window.AudioContext || window.webkitAudioContext; if (Ctx) _audioCtx = new Ctx(); } catch(e) {}
  return _audioCtx;
}
document.addEventListener('pointerdown', function unlockAudio() {
  ensureAudioCtx();
  if (_audioCtx && _audioCtx.state === 'suspended') _audioCtx.resume().catch(function(){});
}, { passive: true });
function playNotifSound() {
  if (!state.soundEnabled) return;
  var ctx = ensureAudioCtx();
  if (!ctx) return;
  try {
    if (ctx.state === 'suspended') ctx.resume();
    var now = ctx.currentTime;
    var osc1 = ctx.createOscillator(), osc2 = ctx.createOscillator(), gain = ctx.createGain();
    osc1.connect(gain); osc2.connect(gain); gain.connect(ctx.destination);
    osc1.type = 'sine'; osc2.type = 'sine';
    osc1.frequency.setValueAtTime(880, now);
    osc1.frequency.exponentialRampToValueAtTime(1108, now + 0.08);
    osc2.frequency.setValueAtTime(1320, now + 0.08);
    osc2.frequency.exponentialRampToValueAtTime(1760, now + 0.22);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.11, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.06, now + 0.10);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.42);
    osc1.start(now); osc1.stop(now + 0.16);
    osc2.start(now + 0.08); osc2.stop(now + 0.42);
  } catch(e) {}
}

var PAGE_TITLES = {
  feed:'page_title_feed',post:'page_title_post',profile:'page_title_profile',
  users:'page_title_users',notifications:'page_title_notifications',
  settings:'page_title_settings',edit_profile:'page_title_edit_profile',
  policy:'page_title_policy',register:'page_title_register',login:'page_title_login',
  followers:'page_title_followers',following:'page_title_following',
  not_found:'page_title_notfound'
};
function updateTitle() {
  var key = PAGE_TITLES[state.view]; var sub = key ? tr(key) : '';
  document.title = sub ? (SITE_NAME + ' — ' + sub) : SITE_NAME;
}
function themeIconHtml() {
  var theme = document.documentElement.getAttribute('data-theme') || 'dark';
  return theme === 'dark' ? ICONS.sun : ICONS.moon;
}
function updateAllThemeIcons() {
  var h = themeIconHtml();
  document.querySelectorAll('#mainThemeBtn, #authThemeBtn').forEach(function(x){ x.innerHTML = h; });
}
function bindThemeBtn() {
  var b = document.getElementById('mainThemeBtn');
  if (!b) return;
  b.innerHTML = themeIconHtml();
  b.addEventListener('click', function(){ toggleTheme(); updateAllThemeIcons(); });
}
function navigate(url, force) {
  if (!force && location.pathname + location.hash === url) return;
  history.pushState({}, '', url); handleRoute();
}
function computeRooms() {
  var rooms = [];
  if (state.view === 'feed') rooms.push('feed');
  else if (state.view === 'post') rooms.push('post:' + (state.viewData.post_id || ''));
  else if (state.view === 'profile') rooms.push('profile:' + (state.viewData.nick || ''));
  return rooms;
}
function roomsEqual(a,b) {
  if (!a || !b || a.length !== b.length) return false;
  return a.slice().sort().join('|') === b.slice().sort().join('|');
}
function updateRoom() {
  var rooms = computeRooms();
  if (roomsEqual(rooms, state.currentRooms)) return;
  state.currentRooms = rooms;
  var body = { rooms: rooms };
  if (!state.token) body.anon_id = anonId;
  api('/api/room', { method:'POST', body: body }).catch(function(){});
}
function handleRoute() {
  var path = location.pathname, m;
  var wasSettings = (state.view === 'settings');
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
  else if (path === '/settings/profile') { state.view = 'settings'; state.viewData = {}; state.settingsSection = 'account'; state.settingsOpen = true; }
  else if (path === '/policy') { state.view = 'policy'; state.viewData = {}; }
  else if (path === '/register') { state.view = 'login'; state.viewData = {}; state.authMode = 'register'; }
  else if (path === '/login') { state.view = 'login'; state.viewData = {}; }
  else { state.view = 'not_found'; state.viewData = {}; }
  if (wasSettings && state.view !== 'settings') state.settingsOpen = false;
  renderRoot(); updateRoom();
}
window.addEventListener('popstate', handleRoute);

function renderRoot() {
  var body = document.body, sb = document.getElementById('sidebar'), main = document.getElementById('main');
  if (!main) return;
  if (state.booting) {
    body.setAttribute('data-mode', 'boot');
    if (sb) sb.innerHTML = '';
    main.innerHTML = '<div class="boot-screen"><div class="boot-logo">' + escapeHtml(SITE_NAME) + '</div>'
      + '<div class="boot-text">' + escapeHtml(tr('booting')) + '</div></div>';
    document.title = SITE_NAME;
    return;
  }
  if (!state.user) { body.setAttribute('data-mode', 'auth'); renderAuthScreen(); return; }
  body.setAttribute('data-mode', 'app');
  updateTitle(); renderSidebar(); renderMain();
}

/* ============ UI-API ============ */
var UI = (function() {
  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      var v = attrs[k]; if (v == null) continue;
      if (k === 'class') e.className = v;
      else if (k === 'html') e.innerHTML = v;
      else if (k === 'text') e.textContent = v;
      else if (k.indexOf('on') === 0 && typeof v === 'function') e.addEventListener(k.slice(2).toLowerCase(), v);
      else e.setAttribute(k, v);
    }
    if (children) (Array.isArray(children)?children:[children]).forEach(function(c){
      if (c == null) return;
      if (typeof c === 'string') e.appendChild(document.createTextNode(c));
      else if (c instanceof Node) e.appendChild(c);
    });
    return e;
  }
  function html(str) { var t = document.createElement('template'); t.innerHTML = (str||'').trim(); return t.content; }
  function card(opts) {
    opts = opts || {};
    return el('div', { class: 'card' + (opts.class ? ' ' + opts.class : '') }, [
      opts.title ? el('h3', { class:'ui-card-title', text: opts.title }) : null,
      opts.body || opts.html || ''
    ]);
  }
  function btn(opts) {
    opts = opts || {};
    return el('button', {
      type: opts.type || 'button',
      class: 'ui-btn ' + (opts.variant ? 'ui-btn-' + opts.variant : '') + (opts.class ? ' ' + opts.class : ''),
      onclick: opts.onClick,
      disabled: opts.disabled ? '' : null
    }, [opts.icon || '', opts.label ? el('span', { text: opts.label }) : '']);
  }
  function input(opts) {
    opts = opts || {};
    var iconHtml = opts.icon ? '<span class="auth-input-icon">' + opts.icon + '</span>' : '';
    var btnHtml = opts.button ? '<button type="button" class="auth-field-btn">' + opts.button + '</button>' : '';
    return html('<div class="auth-field ' + (opts.noRight?'no-right':'') + '">' + iconHtml
      + '<input type="' + (opts.type||'text') + '" name="' + (opts.name||'') + '"'
      + ' placeholder="' + escapeHtml(opts.placeholder||'') + '"'
      + (opts.value != null ? ' value="' + escapeHtml(opts.value) + '"' : '')
      + (opts.required?' required':'') + (opts.maxlength?' maxlength="'+opts.maxlength+'"':'')
      + ' />' + btnHtml + '</div>');
  }
  function modal(opts) {
    opts = opts || {};
    var overlay = el('div', { class:'modal-overlay' });
    var m = el('div', { class:'modal' + (opts.class ? ' ' + opts.class : '') });
    if (opts.title) m.appendChild(el('div', { class:'modal-text', text: opts.title }));
    if (opts.body) {
      if (typeof opts.body === 'string') m.appendChild(html('<div>' + opts.body + '</div>'));
      else m.appendChild(opts.body);
    }
    var actions = el('div', { class:'modal-actions' });
    if (opts.cancel) actions.appendChild(btn({ variant:'secondary', label: opts.cancel.label || tr('confirm_no'), onClick: function(){ close(); opts.cancel.onClick && opts.cancel.onClick(); } }));
    if (opts.confirm) actions.appendChild(btn({ variant:'danger', label: opts.confirm.label || tr('confirm_yes'), onClick: function(){ close(); opts.confirm.onClick && opts.confirm.onClick(); } }));
    if (actions.childNodes.length) m.appendChild(actions);
    overlay.appendChild(m);
    document.body.appendChild(overlay);
    function close() { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }
    overlay.addEventListener('click', function(e){ if (e.target === overlay) close(); });
    return { el: overlay, close: close };
  }
  function toast(text, type) { showToast(text, type); }
  var screens = {};
  function registerScreen(name, fn) { screens[name] = fn; }
  function getScreen(name) { return screens[name]; }
  var features = {};
  function setFeatures(f) { features = f || {}; }
  function hasFeature(name) { return !!features[name]; }
  var extraNav = [];
  function addNavItem(item) { extraNav.push(item); }
  function getExtraNav() { return extraNav.slice(); }
  return {
    el:el, html:html, card:card, btn:btn, input:input, modal:modal, toast:toast,
    registerScreen:registerScreen, getScreen:getScreen, screens:screens,
    setFeatures:setFeatures, hasFeature:hasFeature, features:features,
    addNavItem:addNavItem, getExtraNav:getExtraNav
  };
})();

async function loadUIManifest() {
  try {
    var m = await fetch('/api/ui/manifest').then(function(r){ return r.json(); });
    UI.setFeatures(m.features || {});
    window.__UI_MANIFEST = m;
  } catch(e) {}
}

/* ============ AUTH SCREEN ============ */
function authLoginFormHtml() {
  return '<form id="authLoginForm" autocomplete="on" novalidate>'
    + '<div class="auth-field no-right"><span class="auth-input-icon">' + ICONS.at + '</span>'
    + '<input type="text" name="nick" placeholder="' + escapeHtml(tr('nick_ph')) + '" required autocomplete="username" maxlength="20" /></div>'
    + '<div class="auth-field"><span class="auth-input-icon">' + ICONS.lock + '</span>'
    + '<input type="password" name="password" placeholder="' + escapeHtml(tr('pass_ph')) + '" required autocomplete="current-password" />'
    + '<button type="button" class="auth-field-btn" data-toggle-pw>' + ICONS.eye + '</button></div>'
    + '<div class="auth-error" id="authErr"></div>'
    + '<button type="submit" class="auth-btn" id="authSubmit">' + escapeHtml(tr('log_btn')) + '</button></form>';
}
function authRegisterFormHtml() {
  return '<form id="authRegisterForm" autocomplete="on" novalidate>'
    + '<div class="auth-field no-right"><span class="auth-input-icon">' + ICONS.userLg + '</span>'
    + '<input type="text" name="name" placeholder="' + escapeHtml(tr('name_ph')) + '" required autocomplete="name" maxlength="50" /></div>'
    + '<div class="auth-field"><span class="auth-input-icon">' + ICONS.at + '</span>'
    + '<input type="text" name="nick" placeholder="' + escapeHtml(tr('nick_ph')) + '" required autocomplete="username" maxlength="20" />'
    + '<button type="button" class="auth-field-btn" id="nickCheckBtn">' + ICONS.check + '</button></div>'
    + '<div class="nick-status" id="nickStatus"></div>'
    + '<div class="auth-field"><span class="auth-input-icon">' + ICONS.lock + '</span>'
    + '<input type="password" name="password" placeholder="' + escapeHtml(tr('pass_ph')) + '" required autocomplete="new-password" />'
    + '<button type="button" class="auth-field-btn" data-toggle-pw>' + ICONS.eye + '</button></div>'
    + '<div class="auth-field"><span class="auth-input-icon">' + ICONS.lock + '</span>'
    + '<input type="password" name="password_confirm" placeholder="' + escapeHtml(tr('pass2_ph')) + '" required autocomplete="new-password" />'
    + '<button type="button" class="auth-field-btn" data-toggle-pw>' + ICONS.eye + '</button></div>'
    + '<div class="auth-error" id="authErr"></div>'
    + '<button type="submit" class="auth-btn" id="authSubmit">' + escapeHtml(tr('reg_btn')) + '</button></form>';
}
function authPillTabsHtml() {
  var m = state.authMode || 'login'; var isLogin = (m === 'login');
  return '<div class="pill-tabs auth-pill-tabs"><div class="pill-slider"></div>'
    + '<button type="button" class="pill-tab' + (isLogin?' active':'') + '" data-auth-tab="login">' + escapeHtml(tr('log_title')) + '</button>'
    + '<button type="button" class="pill-tab' + (!isLogin?' active':'') + '" data-auth-tab="register">' + escapeHtml(tr('reg_title')) + '</button></div>';
}
function renderAuthScreen() {
  var main = document.getElementById('main'), sb = document.getElementById('sidebar');
  if (!main) return;
  if (sb) sb.innerHTML = '';
  var note = '';
  if (state.sessionExpired) note = '<div class="session-banner">' + ICONS.info + '<span>' + escapeHtml(tr('session_expired_hint')) + '</span></div>';
  else if (state.bootError === 'network') note = '<div class="session-banner">' + ICONS.info + '<span>' + escapeHtml(tr('network_error_hint')) + '</span></div>';
  var theme = document.documentElement.getAttribute('data-theme') || 'dark';
  var themeIcon = theme === 'dark' ? ICONS.sun : ICONS.moon;
  var html = '<div class="auth-page"><div class="auth-card">';
  html += '<div class="auth-topbar">';
  html += '<button type="button" class="auth-topbar-btn auth-lang-btn" id="authLangBtn">' + (LANG==='ru'?'RU':'EN') + '</button>';
  html += '<button type="button" class="auth-topbar-btn" id="authThemeBtn">' + themeIcon + '</button>';
  html += '</div>';
  html += '<div class="auth-brand"><div class="auth-brand-logo">' + escapeHtml(SITE_NAME) + '</div>'
    + '<div class="auth-brand-name">' + escapeHtml(SITE_NAME) + '</div>'
    + '<div class="auth-brand-tagline">' + escapeHtml(tr('auth_tagline')) + '</div></div>';
  if (note) html += note;
  html += authPillTabsHtml();
  html += '<div id="authFormWrap">' + ((state.authMode==='login')?authLoginFormHtml():authRegisterFormHtml()) + '</div>';
  html += '</div></div>';
  main.innerHTML = html;
  document.title = SITE_NAME + ' — ' + tr(state.authMode==='login'?'log_title':'reg_title');

  var langBtn = document.getElementById('authLangBtn');
  if (langBtn) langBtn.addEventListener('click', function(){
    var next = (LANG==='ru')?'en':'ru';
    document.cookie = 'SLD_lang=' + next + '; path=/; max-age=' + (60*60*24*365);
    location.href = (state.authMode==='register')?'/register':'/login';
    location.reload();
  });
  var themeBtn = document.getElementById('authThemeBtn');
  if (themeBtn) themeBtn.addEventListener('click', function(){ toggleTheme(); updateAllThemeIcons(); });

  var pt = main.querySelector('.pill-tabs');
  var slider = pt ? pt.querySelector('.pill-slider') : null;
  function positionSlider() {
    if (!pt || !slider) return;
    var active = pt.querySelector('.pill-tab.active'); if (!active) return;
    slider.style.width = active.offsetWidth + 'px';
    slider.style.transform = 'translateX(' + active.offsetLeft + 'px)';
  }
  requestAnimationFrame(positionSlider);
  window.addEventListener('resize', function(){ requestAnimationFrame(positionSlider); });
  main.querySelectorAll('[data-auth-tab]').forEach(function(b){
    b.addEventListener('click', function(){
      var mode = b.dataset.authTab;
      if (mode === state.authMode) return;
      state.authMode = mode; state.sessionExpired = false; state.bootError = null;
      pt.querySelectorAll('.pill-tab').forEach(function(x){ x.classList.toggle('active', x === b); });
      requestAnimationFrame(positionSlider);
      var wrap = document.getElementById('authFormWrap');
      if (wrap) {
        wrap.innerHTML = (mode==='login')?authLoginFormHtml():authRegisterFormHtml();
        wrap.style.animation = 'none'; void wrap.offsetWidth; wrap.style.animation = 'fadeIn .22s ease';
      }
      document.title = SITE_NAME + ' — ' + tr(mode==='login'?'log_title':'reg_title');
      if (mode === 'login') bindAuthLogin(); else bindAuthRegister();
      main.querySelectorAll('[data-toggle-pw]').forEach(function(btn){
        btn.addEventListener('click', function(){
          var inp = btn.parentNode.querySelector('input'); if (!inp) return;
          var show = inp.type === 'password';
          inp.type = show ? 'text' : 'password';
          btn.innerHTML = show ? ICONS.eyeOff : ICONS.eye;
        });
      });
    });
  });
  main.querySelectorAll('[data-toggle-pw]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var inp = btn.parentNode.querySelector('input'); if (!inp) return;
      var show = inp.type === 'password';
      inp.type = show ? 'text' : 'password';
      btn.innerHTML = show ? ICONS.eyeOff : ICONS.eye;
    });
  });
  if (state.authMode === 'login') bindAuthLogin(); else bindAuthRegister();
}
function setAuthSubmitting(btn, loading) {
  if (!btn) return;
  if (loading) {
    btn.disabled = true;
    if (!btn.dataset.orig) btn.dataset.orig = btn.innerHTML;
    btn.innerHTML = '<span class="auth-btn-spinner"></span><span>' + escapeHtml(tr('loading')) + '</span>';
  } else {
    btn.disabled = false;
    if (btn.dataset.orig) { btn.innerHTML = btn.dataset.orig; delete btn.dataset.orig; }
  }
}
function bindAuthLogin() {
  var form = document.getElementById('authLoginForm'); if (!form) return;
  var errEl = document.getElementById('authErr'), btn = document.getElementById('authSubmit');
  form.addEventListener('submit', async function(e){
    e.preventDefault(); errEl.textContent = '';
    var fd = new FormData(form);
    var nick = (fd.get('nick')||'').toString().trim();
    var pass = (fd.get('password')||'').toString();
    if (!nick || !pass) { errEl.textContent = tr('err_bad_login'); return; }
    setAuthSubmitting(btn, true);
    try { await doLogin({ nick: nick, password: pass }); }
    catch(err) { errEl.textContent = tr(err.message) || err.message; }
    finally { setAuthSubmitting(btn, false); }
  });
}
function bindAuthRegister() {
  var form = document.getElementById('authRegisterForm'); if (!form) return;
  var errEl = document.getElementById('authErr'), btn = document.getElementById('authSubmit');
  var nickInp = form.querySelector('input[name="nick"]');
  var nickBtn = document.getElementById('nickCheckBtn');
  var nickStatus = document.getElementById('nickStatus');
  function setNickStatus(st, text) {
    if (!nickStatus) return;
    nickStatus.className = 'nick-status' + (st ? ' ' + st : '');
    if (st === 'ok') nickStatus.innerHTML = ICONS.checkLg + '<span>' + escapeHtml(text) + '</span>';
    else if (st === 'err') nickStatus.innerHTML = ICONS.x + '<span>' + escapeHtml(text) + '</span>';
    else nickStatus.textContent = text || '';
  }
  if (nickBtn && nickInp) {
    nickBtn.addEventListener('click', async function(){
      var nick = (nickInp.value || '').trim().replace(/^@/, '');
      if (!nick) { setNickStatus('', ''); return; }
      nickBtn.disabled = true; setNickStatus('', tr('checking'));
      try {
        var data = await api('/api/check_nick?nick=' + encodeURIComponent(nick));
        if (data.available) {
          setNickStatus('ok', tr('nick_available'));
          nickBtn.classList.add('success'); nickBtn.classList.remove('error');
          nickBtn.innerHTML = ICONS.check;
        } else {
          setNickStatus('err', tr(data.reason || 'err_nick_taken'));
          nickBtn.classList.add('error'); nickBtn.classList.remove('success');
          nickBtn.innerHTML = ICONS.x;
        }
      } catch(e) { setNickStatus('err', tr(e.message) || e.message); }
      finally { nickBtn.disabled = false; }
    });
  }
  if (nickInp) {
    nickInp.addEventListener('input', function(){
      if (nickStatus && nickStatus.textContent) setNickStatus('', '');
      if (nickBtn) {
        nickBtn.classList.remove('success'); nickBtn.classList.remove('error');
        nickBtn.innerHTML = ICONS.check;
      }
    });
  }
  form.addEventListener('submit', async function(e){
    e.preventDefault(); errEl.textContent = '';
    var fd = new FormData(form);
    var name = (fd.get('name')||'').toString().trim();
    var nick = (fd.get('nick')||'').toString().trim().replace(/^@/, '');
    var pass = (fd.get('password')||'').toString();
    var pass2 = (fd.get('password_confirm')||'').toString();
    if (!name) { errEl.textContent = tr('err_bad_name'); return; }
    if (pass !== pass2) { errEl.textContent = tr('err_pass_mismatch'); return; }
    setAuthSubmitting(btn, true);
    try { await doRegister({ name: name, nick: nick, password: pass, password_confirm: pass2 }); }
    catch(err) { errEl.textContent = tr(err.message) || err.message; }
    finally { setAuthSubmitting(btn, false); }
  });
}

/* ============ AUTH ACTIONS ============ */
async function loadMe() {
  if (!state.token) { state.booting = false; return; }
  try {
    var u = await api('/api/me');
    setUser(u); state.sessionExpired = false; state.bootError = null;
  } catch(e) {
    if (e && e.message === 'unauthorized') {}
    else state.bootError = 'network';
    setUser(null);
  } finally { state.booting = false; }
}
async function doRegister(data) {
  var res = await api('/api/register', { method:'POST', body:data });
  state.token = res.token; localStorage.setItem('SLD_token', res.token);
  setUser(res.user); state.sessionExpired = false; state.bootError = null; state.booting = false;
  try { localStorage.removeItem('SLD_welcome_seen'); } catch(e) {}
  state.welcomeShown = false;
  connectSSE(); navigate('/', true); maybeShowWelcome();
}
async function doLogin(data) {
  var res = await api('/api/login', { method:'POST', body:data });
  state.token = res.token; localStorage.setItem('SLD_token', res.token);
  setUser(res.user); state.sessionExpired = false; state.bootError = null; state.booting = false;
  connectSSE(); navigate('/', true); maybeShowWelcome();
}
function doLogoutConfirm() {
  showConfirm(tr('confirm_logout'), async function(){
    try { await api('/api/logout', { method:'POST' }); } catch(e) {}
    disconnectSSE(); setUser(null); state.token = null;
    setNotifCount(0); localStorage.removeItem('SLD_token');
    state.sessionExpired = false; state.bootError = null; state.welcomeShown = false;
    feedCache = { all:{posts:null,query:null}, subs:{posts:null,query:null} };
    navigate('/', true); connectSSE();
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
  state.currentRooms = []; state.sseErrors = 0;
}
function connectSSE() {
  disconnectSSE();
  var url = '/api/events';
  if (state.token) url += '?token=' + encodeURIComponent(state.token);
  else url += '?anon=' + encodeURIComponent(anonId);
  var es;
  try { es = new EventSource(url); } catch(e) { setTimeout(connectSSE, 5000); return; }
  state.es = es;
  es.onopen = function() {
    state.sseErrors = 0;
    if (state.sseWasConnected) scheduleRefresh();
    state.sseWasConnected = true; state.currentRooms = []; updateRoom();
  };
  es.onmessage = function(e) { state.sseErrors = 0; try { handleEvent(JSON.parse(e.data)); } catch(err) {} };
  es.onerror = function() {
    state.sseErrors = (state.sseErrors || 0) + 1;
    if (state.sseErrors >= 3) {
      try { es.close(); } catch(e) {}
      if (state.es === es) state.es = null;
      var delay = Math.min(30000, 1200 * Math.pow(1.6, state.sseErrors - 3));
      setTimeout(function(){ if (state.es === null) connectSSE(); }, delay);
    }
  };
  setTimeout(updateRoom, 300);
}
function startFallbackPoll() {
  if (fallbackPollTimer) return;
  fallbackPollTimer = setInterval(function(){
    if (document.hidden || !state.user) return;
    var connected = state.es && state.es.readyState === 1;
    if (!connected) { try { refreshCurrentView(); } catch(e) {} }
  }, 20000);
}
function scheduleRefresh() {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(function(){
    state.refreshTimer = null;
    if (document.querySelector('.inline-editor')) { scheduleRefresh(); return; }
    var active = document.activeElement;
    if (active && active.closest && active.closest('.search-box')) { scheduleRefresh(); return; }
    if (state.suppressRefresh && Date.now() < state.suppressRefresh) { scheduleRefresh(); return; }
    try { refreshCurrentView(); } catch(e) {}
  }, 400);
}
function handleEvent(ev) {
  if (!ev || !ev.type) return;
  if (ev.type === 'hello') return;
  if (ev.type === 'notif_changed') {
    refreshCounters(); playNotifSound();
    if (state.view === 'notifications') loadNotifications();
    return;
  }
  if (ev.type === 'view_update') {
    document.querySelectorAll('[data-post-id="' + ev.post_id + '"] [data-views]').forEach(function(el){ el.textContent = ev.views; });
    return;
  }
  if (ev.type === 'post_deleted') {
    if (state.view === 'post' && state.viewData.post_id === ev.post_id) {
      var main = document.getElementById('main');
      if (main) main.innerHTML = '<div class="main-body"><div class="main-inner">' + notFoundHtml() + '</div></div>';
      bindLinks(main);
    } else if (state.view === 'feed' || state.view === 'profile') scheduleRefresh();
    return;
  }
  if (ev.type === 'refresh') { scheduleRefresh(); return; }
}
document.addEventListener('visibilitychange', function(){ if (!document.hidden && state.sseWasConnected) scheduleRefresh(); });
window.addEventListener('focus', function(){ if (state.sseWasConnected) scheduleRefresh(); });

/* ============ SIDEBAR ============ */
function navBtn(icon, label, active, action, count) {
  var cls = 'nav-btn' + (active ? ' active' : '');
  var badge = (count && count > 0) ? '<span class="badge">' + (count > 99 ? '99+' : count) + '</span>' : '';
  return '<button type="button" class="' + cls + '" data-nav="' + action + '" title="' + escapeHtml(label) + '">'
    + icon + '<span>' + escapeHtml(label) + '</span>' + badge + '</button>';
}
function renderSidebar() {
  var el = document.getElementById('sidebar'); if (!el) return;
  if (!state.user) { el.innerHTML = ''; return; }
  var html = '<div class="logo">' + SITE_NAME + '</div><div class="nav">';
  html += navBtn(ICONS.home, tr('nav_home'), state.view === 'feed', 'home');
  html += navBtn(ICONS.users, tr('nav_users'), state.view === 'users', 'users');
  html += navBtn(ICONS.bell, tr('nav_notifications'), state.view === 'notifications', 'notifications', state.unreadNotif);
  html += navBtn(ICONS.user, tr('nav_profile'), state.view === 'profile' && state.viewData.nick === state.user.nick, 'profile');
  html += navBtn(ICONS.gear, tr('nav_settings'), state.view === 'settings', 'settings');
  UI.getExtraNav().forEach(function(item){
    html += '<button type="button" class="nav-btn" data-nav-extra="' + escapeHtml(item.id) + '">'
      + (ICONS[item.icon] || '') + '<span>' + escapeHtml(item.label) + '</span></button>';
  });
  html += '</div><div class="spacer"></div>';
  html += '<button type="button" class="sidebar-logout" data-nav="logout">' + ICONS.logout + '<span>' + tr('nav_logout') + '</span></button>';
  el.innerHTML = html;
  el.querySelectorAll('[data-nav]').forEach(function(b){
    b.addEventListener('click', function(){
      var nav = b.dataset.nav;
      if (nav === 'home') navigate('/');
      else if (nav === 'users') navigate('/users');
      else if (nav === 'profile') navigate('/u/' + encodeURIComponent(state.user.nick));
      else if (nav === 'notifications') navigate('/notifications');
      else if (nav === 'settings') navigate('/settings');
      else if (nav === 'logout') doLogoutConfirm();
    });
  });
  el.querySelectorAll('[data-nav-extra]').forEach(function(b){
    b.addEventListener('click', function(){
      var item = UI.getExtraNav().find(function(x){ return x.id === b.dataset.navExtra; });
      if (item && item.onClick) item.onClick();
    });
  });
}
function pillTabsHtml(items) {
  var html = '<div class="pill-tabs"><div class="pill-slider"></div>';
  items.forEach(function(it){
    html += '<button type="button" class="pill-tab' + (it.active?' active':'') + '" data-key="' + it.key + '">' + escapeHtml(it.label) + '</button>';
  });
  html += '</div>'; return html;
}
function pillGroupHtml(groupKey, items, activeKey) {
  var html = '<div class="pill-tabs" data-pill-group="' + groupKey + '"><div class="pill-slider"></div>';
  items.forEach(function(it){
    html += '<button type="button" class="pill-tab' + (it.key === activeKey?' active':'') + '" data-pill="' + it.key + '">' + escapeHtml(it.label) + '</button>';
  });
  html += '</div>'; return html;
}
function setupPillSlider(container) {
  if (!container) return;
  var tabsRoot = container.querySelector('.pill-tabs'); if (!tabsRoot) return;
  var slider = tabsRoot.querySelector('.pill-slider'); if (!slider) return;
  function position() {
    var active = tabsRoot.querySelector('.pill-tab.active'); if (!active) return;
    slider.style.width = active.offsetWidth + 'px';
    slider.style.transform = 'translateX(' + active.offsetLeft + 'px)';
  }
  requestAnimationFrame(position);
  window.addEventListener('resize', function(){ requestAnimationFrame(position); });
  return { position: position };
}
function renderMain() {
  var el = document.getElementById('main'); if (!el) return;
  if (!state.user) { renderAuthScreen(); return; }
  if (state.view === 'not_found') {
    el.innerHTML = '<div class="main-header"><div class="title">' + tr('page_title_notfound') + '</div>'
      + '<button type="button" class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>'
      + '<div class="main-body"><div class="main-inner">' + notFoundHtml() + '</div></div>';
    bindThemeBtn(); bindLinks(el);
    return;
  }
  var fn = UI.getScreen(state.view);
  if (fn) { fn(el); return; }
  if (state.view === 'feed') renderFeedView(el);
  else if (state.view === 'post') renderPostView(el);
  else if (state.view === 'profile') renderProfileView(el);
  else if (state.view === 'followers' || state.view === 'following') renderFollowListView(el);
  else if (state.view === 'users') renderUsersView(el);
  else if (state.view === 'notifications') renderNotificationsView(el);
  else if (state.view === 'settings') renderSettingsView(el);
  else if (state.view === 'policy') renderPolicyView(el);
  else renderFeedView(el);
}
function bindLinks(root) {
  if (!root) return;
  root.querySelectorAll('[data-link]').forEach(function(a){
    if (a.dataset.linkBound) return;
    a.dataset.linkBound = '1';
    a.addEventListener('click', function(e){ e.preventDefault(); e.stopPropagation(); navigate(a.getAttribute('href')); });
  });
}
function bindOgImages(root) {
  if (!root) return;
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
  var u = state.user; if (!u) return '';
  var placeholder = opts.placeholder || tr('post_ph');
  var sendLabel = opts.sendLabel || tr('publish');
  var idPrefix = opts.idPrefix || 'post';
  var ogOn = (u.show_link_previews !== false);
  return '<div class="card"><div class="composer-avatar-row">'
    + '<div class="composer-avatar-col">' + avatarHtml(u.avatar_emoji, 'sm')
    + '<button type="button" class="publish-btn publish-btn-mobile" id="' + idPrefix + 'SendMobile" disabled>' + ICONS.send + '</button></div>'
    + '<div class="composer-body">'
    + '<textarea id="' + idPrefix + 'Input" maxlength="' + MAX_POST_LEN + '" placeholder="' + escapeHtml(placeholder) + '">' + escapeHtml(state.composerDraft || '') + '</textarea>'
    + '<div id="' + idPrefix + 'QuoteBox"></div>'
    + '<div class="composer-actions">'
    + '<label class="og-toggle"><input type="checkbox" id="' + idPrefix + 'OgEnabled"' + (ogOn?' checked':'') + '/><span class="cb">' + ICONS.check + '</span><span>' + tr('og_enable') + '</span></label>'
    + '<div class="spacer"></div><span class="composer-hint">Shift+Enter</span>'
    + '<span id="' + idPrefix + 'Counter" style="font-size:12px;color:var(--muted-2)">0 / ' + MAX_POST_LEN + '</span>'
    + '<button type="button" class="publish-btn publish-btn-desktop" id="' + idPrefix + 'Send" disabled>' + escapeHtml(sendLabel) + '</button>'
    + '</div></div></div></div>';
}
function renderQuoteBox(idPrefix) {
  var el = document.getElementById(idPrefix + 'QuoteBox'); if (!el) return;
  if (!state.quotePreview) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="quote-preview"><div class="qp-author">@' + escapeHtml(state.quotePreview.author) + '</div>'
    + '<div class="qp-text">' + escapeHtml(state.quotePreview.text) + '</div>'
    + '<button type="button" class="qp-close">✕</button></div>';
  el.querySelector('.qp-close').addEventListener('click', function(){
    clearQuoteState(); renderQuoteBox(idPrefix);
    var s = document.getElementById(idPrefix + 'Send'), sm = document.getElementById(idPrefix + 'SendMobile');
    var inp = document.getElementById(idPrefix + 'Input');
    if (s) s.disabled = (!inp || !inp.value.trim());
    if (sm) sm.disabled = (!inp || !inp.value.trim());
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
    if (e.key === 'Enter' && e.shiftKey && !e.isComposing) { e.preventDefault(); trigger(); }
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
      inputEl.value = ''; state.composerDraft = '';
      clearQuoteState(); renderQuoteBox(idPrefix); upd();
    } catch(e) { alert(tr(e.message) || e.message); }
    finally { upd(); }
  }
  if (sendBtn) sendBtn.addEventListener('click', trigger);
  if (sendBtnMobile) sendBtnMobile.addEventListener('click', trigger);
  renderQuoteBox(idPrefix); upd(); autoGrow(inputEl);
}
function renderFeedPosts(feedEl, posts) {
  if (!posts || !posts.length) { feedEl.innerHTML = '<div class="empty">' + escapeHtml(tr('no_posts')) + '</div>'; return; }
  feedEl.innerHTML = posts.map(function(p){ return renderPostHtml(p, false); }).join('');
  bindPostActions(feedEl); bindLinks(feedEl); bindOgImages(feedEl);
}
function renderFeedView(el) {
  var html = '<div class="main-header"><div class="title">' + tr('nav_home') + '</div>'
    + '<button type="button" class="icon-btn" id="refreshBtn">' + ICONS.refresh + '</button>'
    + '<button type="button" class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner">';
  html += '<div class="search-box">' + ICONS.search + '<input id="search" type="search" placeholder="' + escapeHtml(tr('search_ph')) + '" value="' + escapeHtml(state.searchQuery) + '" /></div>';
  html += pillTabsHtml([
    { key:'all', label: tr('feed_all'), active: state.feedMode === 'all' },
    { key:'subs', label: tr('feed_subs'), active: state.feedMode === 'subs' },
  ]);
  html += composerHtml({ idPrefix:'post', placeholder: tr('post_ph') });
  html += '<div id="feed">' + spinner() + '</div>';
  html += '</div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el); bindRefreshBtn('refreshBtn');
  var slider = setupPillSlider(el);
  el.querySelectorAll('.pill-tab').forEach(function(t){
    t.addEventListener('click', function(){
      if (state.feedMode === t.dataset.key) return;
      state.feedMode = t.dataset.key;
      el.querySelectorAll('.pill-tab').forEach(function(x){ x.classList.toggle('active', x === t); });
      if (slider) requestAnimationFrame(slider.position);
      loadFeed();
    });
  });
  var searchEl = document.getElementById('search'); var tId;
  searchEl.addEventListener('input', function(){
    state.searchQuery = searchEl.value;
    clearTimeout(tId); tId = setTimeout(loadFeed, 250);
  });
  bindComposer({
    idPrefix: 'post',
    onSend: async function(text, quotedId, ogEnabled){
      state.suppressRefresh = Date.now() + 1500;
      var p = await api('/api/posts', { method:'POST', body:{ text:text, quoted_post_id:quotedId, og_enabled:ogEnabled } });
      state.searchQuery = '';
      feedCache.all = { posts:null, query:null };
      feedCache.subs = { posts:null, query:null };
      var feedEl = document.getElementById('feed');
      if (feedEl) {
        var wrap = document.createElement('div');
        wrap.innerHTML = renderPostHtml(p, false);
        var newEl = wrap.firstChild;
        var em = feedEl.querySelector('.empty'); if (em) em.remove();
        if (feedEl.firstChild) feedEl.insertBefore(newEl, feedEl.firstChild);
        else feedEl.appendChild(newEl);
        bindPostActions(feedEl); bindLinks(feedEl); bindOgImages(feedEl);
      }
    }
  });
  loadFeed();
}
async function loadFeed(opts) {
  opts = opts || {};
  var feedEl = document.getElementById('feed'); if (!feedEl) return;
  var myId = ++feedReqId;
  var mode = state.feedMode, q = state.searchQuery.trim();
  var cache = feedCache[mode];
  var hasCache = cache.posts !== null && cache.query === q && !opts.force;
  if (hasCache) renderFeedPosts(feedEl, cache.posts);
  try {
    var feed = mode === 'subs' ? '&feed=subs' : '';
    var data = await api('/api/posts?q=' + encodeURIComponent(q) + feed);
    if (myId !== feedReqId || state.feedMode !== mode) return;
    if (!document.getElementById('feed')) return;
    var posts = data.posts || [];
    feedCache[mode] = { posts: posts, query: q };
    if (!hasCache || JSON.stringify(posts.map(function(p){return p.id;})) !==
        JSON.stringify((cache.posts || []).map(function(p){return p.id;}))) {
      renderFeedPosts(feedEl, posts);
    }
  } catch(e) { if (myId === feedReqId && !hasCache) feedEl.innerHTML = '<div class="empty">—</div>'; }
}
function renderPostView(el) {
  var html = '<div class="main-header">'
    + '<button type="button" class="icon-btn" id="backBtn">' + ICONS.back + '</button>'
    + '<div class="title">' + tr('page_title_post') + '</div>'
    + '<button type="button" class="icon-btn" id="refreshBtn">' + ICONS.refresh + '</button>'
    + '<button type="button" class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner">';
  html += '<div id="feed">' + spinner() + '</div>';
  html += composerHtml({ idPrefix:'comment', placeholder: tr('comment_ph'), sendLabel: tr('send_comment') });
  html += '</div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindRefreshBtn('refreshBtn');
  document.getElementById('backBtn').addEventListener('click', function(){ navigate('/'); });
  bindLinks(el);
  var replyBanner = document.createElement('div');
  replyBanner.id = 'replyBanner';
  var inputEl = document.getElementById('commentInput');
  inputEl.parentNode.insertBefore(replyBanner, inputEl);
  function renderReplyBanner() {
    if (!state.replyTo) { replyBanner.innerHTML = ''; inputEl.placeholder = tr('comment_ph'); return; }
    replyBanner.innerHTML = '<div class="quote-preview" style="margin-bottom:8px">'
      + '<div class="qp-author">' + tr('reply') + ': @' + escapeHtml(state.replyTo.author) + '</div>'
      + '<button type="button" class="qp-close">✕</button></div>';
    inputEl.placeholder = tr('reply_ph');
    replyBanner.querySelector('.qp-close').addEventListener('click', function(){ state.replyTo = null; renderReplyBanner(); });
  }
  renderReplyBanner();
  state._renderReplyBanner = renderReplyBanner;
  bindComposer({
    idPrefix: 'comment', placeholder: tr('comment_ph'), maxLen: MAX_COMMENT_LEN,
    onSend: async function(text){
      var parentId = state.replyTo ? state.replyTo.id : null;
      state.suppressRefresh = Date.now() + 1500;
      var tempId = 'tmp-' + Date.now().toString(36) + Math.random().toString(36).slice(2,6);
      var fakeComment = { id: tempId, text: text, author: state.user.nick, parent_id: parentId, created_at: Date.now()/1000, likes: 0, user_like: 0 };
      var postEl = document.querySelector('[data-post-id="' + state.viewData.post_id + '"]');
      var postAuthor = postEl ? postEl.dataset.author : null;
      insertCommentIntoDom(state.viewData.post_id, fakeComment, postAuthor);
      state.replyTo = null; renderReplyBanner();
      try {
        var body = { text: text }; if (parentId) body.parent_id = parentId;
        var res = await api('/api/posts/' + state.viewData.post_id + '/comments', { method:'POST', body:body });
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
  loadPostView();
}
async function loadPostView() {
  var feedEl = document.getElementById('feed'); if (!feedEl) return;
  try {
    var p = await api('/api/posts/' + state.viewData.post_id);
    if (!document.getElementById('feed')) return;
    feedEl.innerHTML = renderPostHtml(p, true);
    bindPostActions(feedEl); bindLinks(feedEl); bindOgImages(feedEl);
    if (state.user && p.author !== state.user.nick) {
      api('/api/posts/' + state.viewData.post_id + '/view', { method:'POST' })
        .then(function(res){
          if (res && res.counted) {
            var viewsEl = feedEl.querySelector('[data-views]');
            if (viewsEl) viewsEl.textContent = (parseInt(viewsEl.textContent, 10) || 0) + 1;
          }
        }).catch(function(){});
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
  var postEl = document.querySelector('[data-post-id="' + pid + '"]'); if (!postEl) return;
  var commentsEl = postEl.querySelector('.comments');
  if (!commentsEl) { commentsEl = document.createElement('div'); commentsEl.className = 'comments'; postEl.appendChild(commentsEl); }
  var html = renderCommentHtml(c, postAuthor, pid, !!c.parent_id);
  if (c.parent_id) {
    var parentEl = commentsEl.querySelector('[data-comment-id="' + c.parent_id + '"]');
    if (parentEl) {
      var next = parentEl.nextElementSibling;
      while (next && next.classList && next.classList.contains('reply')) next = next.nextElementSibling;
      if (next) next.insertAdjacentHTML('beforebegin', html);
      else commentsEl.insertAdjacentHTML('beforeend', html);
    } else commentsEl.insertAdjacentHTML('beforeend', html);
  } else commentsEl.insertAdjacentHTML('beforeend', html);
  var btn = postEl.querySelector('button[data-action="open-post"]');
  if (btn) { var span = btn.querySelector('span'); if (span) span.textContent = (parseInt(span.textContent) || 0) + 1; }
  var added = commentsEl.querySelector('[data-comment-id="' + c.id + '"]');
  if (added) added.classList.add('pending');
  bindPostActions(commentsEl); bindLinks(commentsEl);
}
function decrementCommentCount(pid, by) {
  by = by || 1;
  var postEl = document.querySelector('[data-post-id="' + pid + '"]'); if (!postEl) return;
  var btn = postEl.querySelector('button[data-action="open-post"]');
  if (btn) { var span = btn.querySelector('span'); if (span) span.textContent = Math.max(0, (parseInt(span.textContent) || 0) - by); }
}
function renderProfileView(el) {
  var html = '<div class="main-header"><div class="title">' + tr('page_title_profile') + '</div>'
    + '<button type="button" class="icon-btn" id="refreshBtn">' + ICONS.refresh + '</button>'
    + '<button type="button" class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner" id="profileRoot">' + spinner() + '</div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el); bindRefreshBtn('refreshBtn');
  loadProfile(state.viewData.nick);
}
async function loadProfile(nick) {
  var root = document.getElementById('profileRoot'); if (!root) return;
  var myId = ++profileReqId;
  try {
    var u = await api('/api/users/' + encodeURIComponent(nick));
    if (myId !== profileReqId) return;
    if (!document.getElementById('profileRoot')) return;
    var isMe = state.user && state.user.nick.toLowerCase() === u.nick.toLowerCase();
    var actionsHtml = '';
    if (isMe) {
      actionsHtml = '<a class="round-action" href="/settings" data-link title="' + escapeHtml(tr('nav_settings')) + '">' + ICONS.gear + '</a>';
    } else {
      actionsHtml = '<button type="button" class="pill-action ' + (u.is_following?'':'primary') + '" id="followBtn">'
        + (u.is_following ? tr('unfollow') : tr('follow')) + '</button>';
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
    html += '<div class="profile-stat" id="followersLink"><b>' + u.followers + '</b><span>' + tr('profile_followers') + '</span></div>';
    html += '<div class="profile-stat" id="followingLink"><b>' + u.following + '</b><span>' + tr('profile_following') + '</span></div>';
    html += '</div>';
    html += '<div class="profile-meta">' + ICONS.cal + '<span>' + (LANG==='ru'?'Регистрация: ':'Joined: ') + fmtDate(u.created_at) + '</span></div>';
    html += '</div></div>';
    html += '<div class="profile-hero-actions">' + actionsHtml + '</div>';
    html += '</div>';
    html += '<div id="profileContent">' + spinner() + '</div>';
    root.innerHTML = html;
    bindLinks(root);
    document.getElementById('followersLink').addEventListener('click', function(){ navigate('/u/' + encodeURIComponent(u.nick) + '/followers'); });
    document.getElementById('followingLink').addEventListener('click', function(){ navigate('/u/' + encodeURIComponent(u.nick) + '/following'); });
    var btn = document.getElementById('followBtn');
    if (btn) bindOptimisticFollow(btn, u);
    loadProfileContent(u, isMe);
  } catch(e) {
    if (myId !== profileReqId) return;
    if (!document.getElementById('profileRoot')) return;
    root.innerHTML = notFoundHtml(); bindLinks(root);
  }
}
function bindOptimisticFollow(btn, u) {
  btn.addEventListener('click', async function(){
    var wasFollowing = btn.textContent.trim() === tr('unfollow');
    var followersEl = document.querySelector('#followersLink b');
    btn.textContent = wasFollowing ? tr('follow') : tr('unfollow');
    btn.classList.toggle('primary', wasFollowing);
    btn.disabled = true;
    var followersCount = followersEl ? parseInt(followersEl.textContent) || 0 : 0;
    if (followersEl) followersEl.textContent = Math.max(0, followersCount + (wasFollowing ? -1 : 1));
    try {
      if (wasFollowing) await api('/api/users/' + encodeURIComponent(u.nick) + '/unfollow', { method:'POST' });
      else await api('/api/users/' + encodeURIComponent(u.nick) + '/follow', { method:'POST' });
      feedCache.subs = { posts:null, query:null };
    } catch(e) {
      btn.textContent = wasFollowing ? tr('unfollow') : tr('follow');
      btn.classList.toggle('primary', !wasFollowing);
      if (followersEl) followersEl.textContent = followersCount;
      alert(tr(e.message) || e.message);
    } finally { btn.disabled = false; }
  });
}
async function loadProfileContent(u, isMe) {
  var c = document.getElementById('profileContent'); if (!c) return;
  c.innerHTML = spinner();
  var html = '';
  if (isMe) html += composerHtml({ idPrefix:'profile_post', placeholder: tr('post_ph') });
  try {
    var d = await api('/api/posts?author=' + encodeURIComponent(u.nick));
    if (!document.getElementById('profileContent')) return;
    var posts = (d.posts || []).slice();
    posts.sort(function(a,b){ return b.created_at - a.created_at; });
    if (!posts.length) html += '<div class="empty">' + escapeHtml(tr('no_user_posts')) + '</div>';
    else html += posts.map(function(p){ return renderPostHtml(p, false); }).join('');
    c.innerHTML = html;
    bindPostActions(c); bindLinks(c); bindOgImages(c);
    if (isMe) {
      bindComposer({
        idPrefix: 'profile_post',
        onSend: async function(text, quotedId, ogEnabled){
          state.suppressRefresh = Date.now() + 1500;
          var p = await api('/api/posts', { method:'POST', body:{ text:text, quoted_post_id:quotedId, og_enabled:ogEnabled } });
          feedCache.all = { posts:null, query:null };
          feedCache.subs = { posts:null, query:null };
          var wrap = document.createElement('div');
          wrap.innerHTML = renderPostHtml(p, false);
          var newEl = wrap.firstChild;
          var composerEl = c.querySelector('.card');
          var em = c.querySelector('.empty'); if (em) em.remove();
          if (composerEl && composerEl.nextSibling) c.insertBefore(newEl, composerEl.nextSibling);
          else c.appendChild(newEl);
          bindPostActions(c); bindLinks(c); bindOgImages(c);
        }
      });
    }
  } catch(e) { c.innerHTML = html + '<div class="empty">—</div>'; }
}
function renderFollowListView(el) {
  var nick = state.viewData.nick;
  var isFollowers = state.view === 'followers';
  var title = isFollowers ? tr('followers_title') : tr('following_title');
  var html = '<div class="main-header"><button type="button" class="icon-btn" id="backBtn">' + ICONS.back + '</button>'
    + '<div class="title">' + escapeHtml(title) + '</div>'
    + '<button type="button" class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner"><div id="list">' + spinner() + '</div></div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  document.getElementById('backBtn').addEventListener('click', function(){ navigate('/u/' + encodeURIComponent(nick)); });
  loadFollowList(nick, isFollowers);
}
function userRowHtml(u) {
  return '<div class="user-row" data-link-row="/u/' + encodeURIComponent(u.nick) + '">'
    + avatarHtml(u.avatar_emoji, 'sm')
    + '<div class="info"><a class="nick" href="/u/' + encodeURIComponent(u.nick) + '" data-link>@' + escapeHtml(u.nick) + '</a>'
    + '<div class="name">' + escapeHtml(u.name) + '</div></div></div>';
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
    if (!document.getElementById('list')) return;
    var users = data.users || [];
    var limitedNote = data.limited ? '<div class="empty" style="margin-bottom:12px;padding:16px">' + escapeHtml(tr('limited_list')) + '</div>' : '';
    if (!users.length) { wrap.innerHTML = '<div class="empty">' + escapeHtml(isFollowers ? tr('no_followers') : tr('no_following')) + '</div>'; return; }
    wrap.innerHTML = limitedNote + users.map(userRowHtml).join('');
    bindLinks(wrap); bindUserRows(wrap);
  } catch(e) { if (wrap) wrap.innerHTML = '<div class="empty">' + escapeHtml(tr(e.message) || '—') + '</div>'; }
}
function renderUsersView(el) {
  var html = '<div class="main-header"><div class="title">' + tr('people_title') + '</div>'
    + '<button type="button" class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner">';
  html += '<div class="search-box">' + ICONS.search + '<input id="peopleSearch" type="text" placeholder="' + escapeHtml(tr('people_search_ph')) + '" value="' + escapeHtml(state.peopleQuery) + '" /></div>';
  html += pillTabsHtml([
    { key:'all', label: tr('people_all'), active: state.peopleTab === 'all' },
    { key:'subs', label: tr('people_subs'), active: state.peopleTab === 'subs' },
  ]);
  html += '<div id="list">' + spinner() + '</div>';
  html += '</div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  var slider = setupPillSlider(el);
  el.querySelectorAll('.pill-tab').forEach(function(t){
    t.addEventListener('click', function(){
      if (state.peopleTab === t.dataset.key) return;
      state.peopleTab = t.dataset.key;
      el.querySelectorAll('.pill-tab').forEach(function(x){ x.classList.toggle('active', x === t); });
      if (slider) requestAnimationFrame(slider.position);
      loadPeople();
    });
  });
  var searchEl = document.getElementById('peopleSearch'); var tId;
  searchEl.addEventListener('input', function(){
    state.peopleQuery = searchEl.value;
    clearTimeout(tId); tId = setTimeout(loadPeople, 250);
  });
  loadPeople();
}
async function loadPeople() {
  var wrap = document.getElementById('list'); if (!wrap) return;
  var myId = ++peopleReqId;
  var tab = state.peopleTab;
  try {
    var q = (state.peopleQuery || '').trim();
    var url = '/api/users?q=' + encodeURIComponent(q);
    if (tab === 'subs') url += '&only_following=1';
    var data = await api(url);
    if (myId !== peopleReqId || state.peopleTab !== tab) return;
    if (!document.getElementById('list')) return;
    var users = data.users || [];
    if (!users.length) { wrap.innerHTML = '<div class="empty">' + escapeHtml(tr('no_users')) + '</div>'; return; }
    wrap.innerHTML = users.map(userRowHtml).join('');
    bindLinks(wrap); bindUserRows(wrap);
  } catch(e) { if (myId === peopleReqId && wrap) wrap.innerHTML = '<div class="empty">—</div>'; }
}
function renderNotificationsView(el) {
  var html = '<div class="main-header"><div class="title">' + tr('notif_title') + '</div>'
    + '<button type="button" class="icon-btn danger" id="clearNotifsBtn">' + ICONS.trash + '</button>'
    + '<button type="button" class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="main-inner"><div id="notifList">' + spinner() + '</div></div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  document.getElementById('clearNotifsBtn').addEventListener('click', function(){
    showConfirm(tr('notif_clear_confirm'), async function(){
      try {
        await api('/api/notifications/clear', { method:'POST' });
        setNotifCount(0); renderSidebar(); loadNotifications();
      } catch(e) { alert(tr(e.message) || e.message); }
    }, { yesText: tr('notif_clear') });
  });
  loadNotifications();
}
async function loadNotifications() {
  var wrap = document.getElementById('notifList'); if (!wrap) return;
  try {
    var data = await api('/api/notifications');
    if (!document.getElementById('notifList')) return;
    var items = data.items || [];
    setNotifCount(data.unread || 0);
    if (data.unread > 0) {
      api('/api/notifications/read', { method:'POST' }).catch(function(){});
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
  if (n.type === 'follow') { text = author + ' ' + tr('notif_follow'); link = '/u/' + encodeURIComponent(n.from_nick); }
  else if (n.type === 'comment' || n.type === 'reply' || n.type === 'mention') {
    var label = n.type === 'comment' ? tr('notif_comment') : n.type === 'reply' ? tr('notif_reply') : tr('notif_mention');
    text = author + ' ' + label;
    link = n.post_id ? ('/p/' + n.post_id + (n.comment_id ? ('#c-' + n.comment_id) : '')) : null;
  } else if (n.type === 'new_post' || n.type === 'quote') {
    var lbl = n.type === 'new_post' ? tr('notif_new_post') : tr('notif_quote');
    text = author + ' ' + lbl;
    link = n.post_id ? ('/p/' + n.post_id) : null;
  } else text = author;
  var snippet = n.text ? '<div class="snippet">' + escapeHtml(n.text) + '</div>' : '';
  var inner = '<div class="info"><div class="line">' + text + '</div>' + snippet + '<div class="time">' + timeAgo(n.created_at) + '</div></div>';
  if (link) return '<a class="' + cls + '" href="' + link + '" data-link>' + avatarHtml(null, 'sm') + inner + '</a>';
  return '<div class="' + cls + '">' + avatarHtml(null, 'sm') + inner + '</div>';
}
function settingsToggleRow(label, key, on) {
  return '<div class="toggle-row"><span>' + escapeHtml(label) + '</span>'
    + '<div class="toggle' + (on?' on':'') + '" data-toggle="' + key + '"></div></div>';
}
function renderSettingsListView(el) {
  var html = '<div class="main-header wide"><div class="title">' + tr('settings_title') + '</div>'
    + '<button type="button" class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="settings-list-mobile">';
  var rows = [
    { key:'account', icon: ICONS.user, title: tr('settings_account'), desc: tr('settings_account_desc') },
    { key:'privacy', icon: ICONS.bell, title: tr('settings_privacy'), desc: tr('settings_privacy_desc') },
    { key:'appearance', icon: ICONS.sun, title: tr('settings_appearance'), desc: tr('settings_appearance_desc') },
    { key:'info', icon: ICONS.server, title: tr('settings_info'), desc: tr('settings_info_desc') },
  ];
  rows.forEach(function(r){
    html += '<button type="button" class="settings-list-row" data-section="' + r.key + '">'
      + '<div class="sl-icon">' + r.icon + '</div>'
      + '<div class="sl-info"><div class="sl-title">' + escapeHtml(r.title) + '</div>'
      + '<div class="sl-desc">' + escapeHtml(r.desc) + '</div></div>'
      + '<div class="sl-chevron">' + ICONS.chevron + '</div></button>';
  });
  html += '</div></div>';
  el.innerHTML = html;
  bindThemeBtn();
  el.querySelectorAll('.settings-list-row').forEach(function(r){
    r.addEventListener('click', function(){
      state.settingsSection = r.dataset.section;
      state.settingsOpen = true;
      renderSettingsView(el);
    });
  });
}
function renderAccountSection(me) {
  var avatarEmoji = me.avatar_emoji || DEFAULT_EMOJI;
  var emojiOpts = '';
  EMOJIS.forEach(function(e){
    emojiOpts += '<button type="button" class="account-emoji-opt' + (e === avatarEmoji?' active':'') + '" data-emoji="' + escapeHtml(e) + '" title="' + escapeHtml(emojiNameOf(e)) + '">' + e + '</button>';
  });
  return ''
    + '<div class="account-hero"><div class="account-hero-avatar" id="acctAvatarPreview">' + avatarHtml(avatarEmoji, 'lg') + '</div>'
    + '<div class="account-hero-info"><div class="account-hero-name">' + escapeHtml(me.name) + '</div>'
    + '<div class="account-hero-nick">@' + escapeHtml(me.nick) + '</div></div></div>'
    + '<div class="settings-subhead">' + escapeHtml(tr('settings_profile_section')) + '</div>'
    + '<form id="accountForm" autocomplete="off" novalidate style="display:flex;flex-direction:column;gap:10px">'
    + '<div class="auth-field no-right"><span class="auth-input-icon">' + ICONS.userLg + '</span>'
    + '<input type="text" name="name" maxlength="50" value="' + escapeHtml(me.name) + '" placeholder="' + escapeHtml(tr('name_ph')) + '" required /></div>'
    + '<div class="auth-field no-right"><span class="auth-input-icon">' + ICONS.at + '</span>'
    + '<input type="text" name="nick" maxlength="20" value="' + escapeHtml(me.nick) + '" placeholder="' + escapeHtml(tr('nick_ph')) + '" required /></div>'
    + '<textarea name="bio" maxlength="' + MAX_BIO_LEN + '" placeholder="' + escapeHtml(tr('bio_ph')) + '" class="edit-bio-textarea">' + escapeHtml(me.bio || '') + '</textarea>'
    + '<div class="auth-error" id="acctErr"></div>'
    + '<button type="submit" class="publish-btn" style="height:44px">' + escapeHtml(tr('save')) + '</button></form>'
    + '<div class="settings-subhead">' + escapeHtml(tr('avatar_choose')) + '</div>'
    + '<div class="account-emoji-grid" id="acctEmojiGrid">' + emojiOpts + '</div>'
    + '<div class="settings-subhead">' + escapeHtml(tr('settings_session_section')) + '</div>'
    + '<button type="button" class="modal-btn danger" style="width:auto;padding:0 18px;height:42px;font-size:14px" id="settingsLogout">' + escapeHtml(tr('settings_logout')) + '</button>';
}
function renderSettingsView(el) {
  var isMobile = window.matchMedia('(max-width: 900px)').matches;
  if (isMobile && !state.settingsOpen) { renderSettingsListView(el); return; }
  var theme = document.documentElement.getAttribute('data-theme') || 'dark';
  var me = state.user || {};
  var colors = loadColors();
  var sec = state.settingsSection || 'account';
  var soundOn = state.soundEnabled !== false;
  var headerLeft = isMobile ? '<button type="button" class="icon-btn" id="settingsBackBtn">' + ICONS.back + '</button>' : '';
  var html = '<div class="main-header wide">' + headerLeft
    + '<div class="title">' + tr('settings_title') + '</div>'
    + '<button type="button" class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="settings-layout">';
  if (!isMobile) {
    html += '<nav class="settings-nav">';
    html += '<button type="button" class="settings-nav-btn' + (sec==='account'?' active':'') + '" data-section="account">' + ICONS.user + ' ' + tr('settings_account') + '</button>';
    html += '<button type="button" class="settings-nav-btn' + (sec==='privacy'?' active':'') + '" data-section="privacy">' + ICONS.bell + ' ' + tr('settings_privacy') + '</button>';
    html += '<button type="button" class="settings-nav-btn' + (sec==='appearance'?' active':'') + '" data-section="appearance">' + ICONS.sun + ' ' + tr('settings_appearance') + '</button>';
    html += '<button type="button" class="settings-nav-btn' + (sec==='info'?' active':'') + '" data-section="info">' + ICONS.server + ' ' + tr('settings_info') + '</button>';
    html += '</nav>';
  }
  html += '<div class="settings-content">';
  html += '<div class="settings-block" id="section-account"><h2>' + tr('settings_account') + '</h2>';
  html += renderAccountSection(me);
  html += '</div>';
  html += '<div class="settings-block" id="section-privacy"><h2>' + tr('settings_privacy') + '</h2>';
  html += '<div class="settings-subhead">' + escapeHtml(tr('settings_privacy')) + '</div>';
  html += settingsToggleRow(tr('settings_allow_followers'), 'allow_followers_view', me.allow_followers_view !== false);
  html += settingsToggleRow(tr('settings_allow_following'), 'allow_following_view', me.allow_following_view !== false);
  html += settingsToggleRow(tr('settings_show_device_badge'), 'show_device_badge', me.show_device_badge !== false);
  html += '<p class="settings-desc">' + escapeHtml(tr('settings_show_device_badge_hint')) + '</p>';
  html += '<div class="settings-subhead">' + escapeHtml(tr('settings_notifications')) + '</div>';
  html += settingsToggleRow(tr('notify_new_post'), 'notify_on_new_post', me.notify_on_new_post !== false);
  html += settingsToggleRow(tr('notify_follow'), 'notify_on_follow', me.notify_on_follow !== false);
  html += settingsToggleRow(tr('notify_comment'), 'notify_on_comment', me.notify_on_comment !== false);
  html += settingsToggleRow(tr('notify_reply'), 'notify_on_reply', me.notify_on_reply !== false);
  html += settingsToggleRow(tr('notify_mention'), 'notify_on_mention', me.notify_on_mention !== false);
  html += settingsToggleRow(tr('notify_quote'), 'notify_on_quote', me.notify_on_quote !== false);
  html += '</div>';
  html += '<div class="settings-block" id="section-appearance"><h2>' + tr('settings_appearance') + '</h2>';
  html += '<div class="settings-subhead">' + tr('settings_theme') + '</div>';
  html += pillGroupHtml('theme', [
    { key:'dark', label: tr('theme_dark') },
    { key:'light', label: tr('theme_light') }
  ], theme);
  html += '<div class="settings-subhead">' + tr('settings_lang') + '</div>';
  html += pillGroupHtml('lang', [
    { key:'ru', label:'Русский' },
    { key:'en', label:'English' }
  ], LANG);
  html += '<div class="settings-subhead">' + (LANG==='ru'?'Прочее':'Other') + '</div>';
  html += settingsToggleRow(tr('settings_sound'), 'sound_enabled', soundOn);
  html += '<p class="settings-desc">' + escapeHtml(tr('settings_sound_hint')) + '</p>';
  html += settingsToggleRow(tr('settings_show_link_previews'), 'show_link_previews', me.show_link_previews !== false);
  html += '<p class="settings-desc">' + escapeHtml(tr('settings_show_link_previews_hint')) + '</p>';
  html += '<div class="settings-subhead">' + escapeHtml(tr('settings_colors')) + '</div>';
  html += '<p class="settings-desc">' + escapeHtml(tr('settings_colors_hint')) + '</p>';
  html += '<div style="font-size:12.5px;color:var(--muted);margin-bottom:8px;font-weight:600">' + escapeHtml(tr('color_accent')) + '</div>';
  html += '<div class="color-swatches" data-color-group="accent">';
  COLOR_PRESETS.accent.forEach(function(p){
    var isActive = (colors.accent || 'blue') === p.id;
    var val = theme === 'dark' ? p.dark : p.light;
    html += '<button type="button" class="color-swatch' + (isActive?' active':'') + '" data-color="' + p.id + '" title="' + escapeHtml(LANG==='ru'?p.ru:p.en) + '" style="background:' + val + '"></button>';
  });
  html += '</div>';
  html += '<div style="font-size:12.5px;color:var(--muted);margin-bottom:8px;font-weight:600">' + escapeHtml(tr('color_likes')) + '</div>';
  html += '<div class="color-swatches" data-color-group="like">';
  COLOR_PRESETS.like.forEach(function(p){
    var isActive = (colors.like || 'red') === p.id;
    var val = theme === 'dark' ? p.dark : p.light;
    html += '<button type="button" class="color-swatch' + (isActive?' active':'') + '" data-color="' + p.id + '" title="' + escapeHtml(LANG==='ru'?p.ru:p.en) + '" style="background:' + val + '"></button>';
  });
  html += '</div>';
  html += '<button type="button" class="opt" id="resetColors" style="margin-top:4px">' + escapeHtml(tr('reset_colors')) + '</button>';
  html += '</div>';
  html += '<div class="settings-block" id="section-info"><h2>' + tr('settings_info') + '</h2>';
  html += '<p class="settings-desc">' + tr('settings_desc') + '</p>';
  html += '<p style="margin:0 0 16px"><a class="settings-link" href="/policy" data-link>' + tr('settings_policy') + '</a></p>';
  html += '<div class="settings-subhead">' + tr('settings_authors') + '</div>';
  html += '<div style="font-size:14.5px;margin-bottom:8px;color:var(--text)">SldShr, DeepSeek</div>';
  html += '<div class="settings-subhead">' + escapeHtml(tr('settings_server')) + '</div>';
  html += '<div class="device-info" id="serverInfo">' + spinner() + '</div>';
  html += '<div class="settings-subhead">' + escapeHtml(tr('settings_device')) + '</div>';
  html += '<div class="device-info" id="deviceInfo">' + spinner() + '</div>';
  html += '</div>';
  html += '</div></div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  if (isMobile) {
    var bb = document.getElementById('settingsBackBtn');
    if (bb) bb.addEventListener('click', function(){ state.settingsOpen = false; renderSettingsView(el); });
  }
  function applySectionVisibility() {
    var sections = el.querySelectorAll('.settings-block');
    sections.forEach(function(s){
      if (!s.id || s.id.indexOf('section-') !== 0) return;
      s.style.display = (s.id.replace('section-', '') === state.settingsSection) ? '' : 'none';
    });
    el.querySelectorAll('.settings-nav-btn').forEach(function(b){
      b.classList.toggle('active', b.dataset.section === state.settingsSection);
    });
  }
  applySectionVisibility();
  el.querySelectorAll('.settings-nav-btn').forEach(function(b){
    b.addEventListener('click', function(){ state.settingsSection = b.dataset.section; applySectionVisibility(); });
  });
  el.querySelectorAll('.pill-tabs[data-pill-group]').forEach(function(group){
    var slider = group.querySelector('.pill-slider');
    function position() {
      var active = group.querySelector('.pill-tab.active');
      if (slider && active) {
        slider.style.width = active.offsetWidth + 'px';
        slider.style.transform = 'translateX(' + active.offsetLeft + 'px)';
      }
    }
    requestAnimationFrame(position);
    group.querySelectorAll('[data-pill]').forEach(function(b){
      b.addEventListener('click', function(){
        var groupKey = group.dataset.pillGroup;
        if (b.classList.contains('active')) return;
        group.querySelectorAll('.pill-tab').forEach(function(x){ x.classList.toggle('active', x === b); });
        requestAnimationFrame(position);
        if (groupKey === 'theme') { applyTheme(b.dataset.pill); updateAllThemeIcons(); }
        else if (groupKey === 'lang') {
          document.cookie = 'SLD_lang=' + b.dataset.pill + '; path=/; max-age=' + (60*60*24*365);
          try {
            sessionStorage.setItem('SLD_reload_settings', JSON.stringify({ open: !!state.settingsOpen, section: state.settingsSection || 'account' }));
          } catch(e) {}
          location.href = '/settings'; location.reload();
        }
      });
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
  if (rc) rc.addEventListener('click', function(){ saveColors({}); applyColors(); renderSettingsView(el); });

  var acctForm = document.getElementById('accountForm');
  var acctErr = document.getElementById('acctErr');
  var acctEmojiGrid = document.getElementById('acctEmojiGrid');
  var acctCurrentEmoji = me.avatar_emoji || DEFAULT_EMOJI;
  if (acctEmojiGrid) {
    acctEmojiGrid.querySelectorAll('.account-emoji-opt').forEach(function(b){
      b.addEventListener('click', function(){
        acctCurrentEmoji = b.dataset.emoji;
        var prev = document.getElementById('acctAvatarPreview');
        if (prev) prev.outerHTML = '<div class="account-hero-avatar" id="acctAvatarPreview">' + avatarHtml(acctCurrentEmoji, 'lg') + '</div>';
        acctEmojiGrid.querySelectorAll('.account-emoji-opt').forEach(function(x){ x.classList.toggle('active', x === b); });
      });
    });
  }
  if (acctForm) {
    acctForm.addEventListener('submit', async function(e){
      e.preventDefault();
      if (acctErr) acctErr.textContent = '';
      var fd = new FormData(acctForm);
      var body = {
        name: (fd.get('name')||'').toString().trim(),
        nick: (fd.get('nick')||'').toString().trim().replace(/^@/, ''),
        bio: (fd.get('bio')||'').toString().trim(),
        avatar_emoji: acctCurrentEmoji || DEFAULT_EMOJI
      };
      var sb = acctForm.querySelector('button[type="submit"]');
      if (sb) sb.disabled = true;
      try {
        var r = await api('/api/users/me', { method:'PUT', body:body });
        setUser(r.user); renderSidebar();
        showToast(tr('settings_saved'), 'success');
        renderSettingsView(el);
      } catch(err) {
        if (acctErr) acctErr.textContent = tr(err.message) || err.message;
        if (sb) sb.disabled = false;
      }
    });
  }
  var lo = document.getElementById('settingsLogout');
  if (lo) lo.addEventListener('click', doLogoutConfirm);
  el.querySelectorAll('[data-toggle]').forEach(function(t){
    t.addEventListener('click', async function(){
      var key = t.dataset.toggle;
      var newVal = !t.classList.contains('on');
      t.classList.toggle('on', newVal);
      if (key === 'sound_enabled') {
        state.soundEnabled = newVal;
        localStorage.setItem('SLD_sound', newVal ? '1' : '0');
        if (newVal) playNotifSound();
        return;
      }
      var patch = {}; patch[key] = newVal;
      var meNow = state.user || {}; meNow[key] = newVal; setUser(meNow);
      try { await api('/api/users/me/settings', { method:'POST', body:patch }); }
      catch(e) {
        t.classList.toggle('on', !newVal);
        meNow[key] = !newVal; setUser(meNow);
        alert(tr(e.message) || e.message);
      }
    });
  });
  var si = document.getElementById('serverInfo');
  if (si) {
    api('/api/uptime').then(function(info){
      var uptime = fmtUptime(info.uptime_seconds || 0);
      si.innerHTML = '<div class="device-info-row"><span class="label">' + escapeHtml(tr('settings_uptime')) + '</span>'
        + '<span class="value">' + escapeHtml(uptime) + '</span></div>'
        + '<div class="device-info-row"><span class="label">' + escapeHtml(tr('settings_version')) + '</span>'
        + '<span class="value">' + escapeHtml(info.version || '—') + '</span></div>';
    }).catch(function(){ si.innerHTML = '<div class="device-info-row"><span class="label">—</span></div>'; });
  }
  var di = document.getElementById('deviceInfo');
  if (di) {
    api('/api/whoami').then(function(info){
      var city = info.city || '—';
      var country = info.country ? (', ' + info.country) : '';
      var cityFull = (city === '—') ? '—' : (city + country);
      di.innerHTML = '<div class="device-info-row"><span class="label">' + escapeHtml(tr('settings_device_browser')) + '</span>'
        + '<span class="value">' + escapeHtml(info.browser || '—') + '</span></div>'
        + '<div class="device-info-row"><span class="label">' + escapeHtml(tr('settings_device_os')) + '</span>'
        + '<span class="value">' + escapeHtml(info.os || '—') + '</span></div>'
        + '<div class="device-info-row"><span class="label">' + escapeHtml(tr('settings_device_city')) + '</span>'
        + '<span class="value">' + escapeHtml(cityFull) + '</span></div>';
    }).catch(function(){ di.innerHTML = '<div class="device-info-row"><span class="label">—</span></div>'; });
  }
}
function emojiNameOf(e) {
  var m = EMOJI_NAMES[e];
  if (!m) return '';
  return LANG === 'ru' ? m.ru : m.en;
}
function renderPolicyHtml(raw) {
  var lines = String(raw || '').split('\n'); var html = ''; var inList = false;
  function closeList() { if (inList) { html += '</ul>'; inList = false; } }
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    if (line.indexOf('# ') === 0) { closeList(); html += '<h2 class="policy-h1">' + escapeHtml(line.slice(2)) + '</h2>'; }
    else if (line.indexOf('## ') === 0) { closeList(); html += '<h3 class="policy-h2">' + escapeHtml(line.slice(3)) + '</h3>'; }
    else if (line.indexOf('• ') === 0) {
      if (!inList) { html += '<ul class="policy-list">'; inList = true; }
      html += '<li>' + escapeHtml(line.slice(2)) + '</li>';
    } else if (line.trim() === '') { closeList(); html += '<div class="policy-gap"></div>'; }
    else { closeList(); html += '<p>' + escapeHtml(line) + '</p>'; }
  }
  closeList(); return html;
}
function renderPolicyView(el) {
  var html = '<div class="main-header"><button type="button" class="icon-btn" id="backBtn">' + ICONS.back + '</button>'
    + '<div class="title">' + tr('policy_title') + '</div>'
    + '<button type="button" class="icon-btn" id="mainThemeBtn">' + themeIconHtml() + '</button></div>';
  html += '<div class="main-body"><div class="policy">';
  html += renderPolicyHtml(tr('policy_content'));
  html += '<div class="policy-footer">' + escapeHtml(tr('settings_desc')) + '</div>';
  html += '</div></div>';
  el.innerHTML = html;
  bindThemeBtn(); bindLinks(el);
  document.getElementById('backBtn').addEventListener('click', function(){ navigate('/settings'); });
}
function renderOgCard(og) {
  if (!og) return '';
  if (state.user && state.user.show_link_previews === false) return '';
  var site = og.site_name || '';
  if (!site) { try { site = new URL(og.url).hostname; } catch(e) {} }
  var imgHtml = og.image ? '<div class="og-image"><img src="' + escapeHtml(og.image) + '" alt="" loading="lazy" /></div>' : '';
  return '<a class="og-card" href="' + escapeHtml(og.url) + '" target="_blank" rel="noopener noreferrer">'
    + imgHtml + '<div class="og-body">'
    + (site ? '<div class="og-site">' + escapeHtml(site) + '</div>' : '')
    + (og.title ? '<div class="og-title">' + escapeHtml(og.title) + '</div>' : '')
    + (og.description ? '<div class="og-desc">' + escapeHtml(og.description) + '</div>' : '')
    + '</div></a>';
}
function renderPostHtml(p, showComments) {
  var liked = p.user_like === 1;
  var likeCls = liked ? 'active' : '';
  var displayText = p.text, truncated = false;
  if (!showComments) { var res = truncateText(p.text); displayText = res.text; truncated = res.truncated; }
  var bodyHtml = linkifyText(displayText);
  var readMore = truncated ? '<span class="read-more" data-action="open-post" data-post-id="' + p.id + '">' + escapeHtml(tr('read_more')) + '</span>' : '';
  var isMine = state.user && p.author && p.author.toLowerCase() === state.user.nick.toLowerCase();
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
      + '<div class="q-text">' + qHtml + '</div></div>';
  }
  var menuHtml = '';
  if (isMine) {
    menuHtml = '<div class="post-menu">'
      + '<button type="button" class="act-btn" data-action="edit-post" data-post-id="' + p.id + '">' + ICONS.edit + '</button>'
      + '<button type="button" class="act-btn danger" data-action="delete-post" data-post-id="' + p.id + '">' + ICONS.trash + '</button></div>';
  }
  var ogHtml = renderOgCard(p.og_data);
  var commentsHtml = '';
  if (showComments && p.comments && p.comments.length) {
    commentsHtml = '<div class="comments">' + renderCommentsTree(p.comments, p.author, p.id) + '</div>';
  }
  var cCount = (typeof p.comment_count === 'number') ? p.comment_count : (p.comments ? p.comments.length : 0);
  var viewsHtml = p.views ? '<span class="views-badge">' + ICONS.eye + '<span data-views>' + p.views + '</span></span>' : '';
  var heartIcon = liked ? ICONS.heart_filled : ICONS.heart;
  return ''
    + '<div class="post-card" data-post-id="' + p.id + '" data-author="' + escapeHtml(p.author || '') + '">'
    +   '<div class="post-header">' + avatarHtml(p.author_avatar_emoji)
    +     '<div class="meta"><div class="who">' + authorHtml + deviceBadge + '<span class="post-time">' + timeAgo(p.created_at) + '</span></div></div>'
    +     menuHtml
    +   '</div>'
    +   (displayText ? '<div class="post-text" data-raw="' + escapeHtml(p.text) + '">' + bodyHtml + '</div>' : '')
    +   readMore + ogHtml + quotedHtml
    +   '<div class="post-actions">'
    +     '<button type="button" class="act-btn like-btn ' + likeCls + '" data-action="like" data-post-id="' + p.id + '">' + heartIcon + '<span class="num">' + (p.likes || 0) + '</span></button>'
    +     '<button type="button" class="act-btn" data-action="open-post" data-post-id="' + p.id + '">' + ICONS.comment + '<span>' + cCount + '</span></button>'
    +     '<button type="button" class="act-btn" data-action="quote" data-post-id="' + p.id + '">' + ICONS.quote + '</button>'
    +     '<button type="button" class="act-btn" data-action="copy" data-post-id="' + p.id + '">' + ICONS.copy + '</button>'
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
  var isAuthor = postAuthor && c.author && c.author.toLowerCase() === postAuthor.toLowerCase();
  var isMine = state.user && c.author && c.author.toLowerCase() === state.user.nick.toLowerCase();
  var cls = 'comment' + (isReply ? ' reply' : '') + (isAuthor ? ' is-author' : '');
  var authorHtml = c.author ? '<a class="comment-author" href="/u/' + encodeURIComponent(c.author) + '" data-link>@' + escapeHtml(c.author) + '</a>' : '';
  var badge = isAuthor ? '<span class="comment-author-badge">' + escapeHtml(tr('author_badge')) + '</span>' : '';
  var replyBtn = '';
  if (!isReply) replyBtn = '<button type="button" class="act-btn" data-action="reply" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-author="' + escapeHtml(c.author || '') + '">' + tr('reply') + '</button>';
  var editBtn = isMine ? '<button type="button" class="act-btn" data-action="edit-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '">' + ICONS.edit + '</button>' : '';
  var delBtn = isMine ? '<button type="button" class="act-btn danger" data-action="delete-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '">' + ICONS.trash + '</button>' : '';
  var bodyHtml = linkifyText(c.text);
  var heartIcon = liked ? ICONS.heart_filled : ICONS.heart;
  return ''
    + '<div class="' + cls + '" data-comment-id="' + c.id + '">'
    +   '<div class="comment-head">' + authorHtml + badge + '<span class="comment-time">' + timeAgo(c.created_at) + '</span></div>'
    +   '<div class="comment-text" data-raw="' + escapeHtml(c.text) + '">' + bodyHtml + '</div>'
    +   '<div class="comment-actions">'
    +     '<button type="button" class="act-btn like-btn ' + likeCls + '" data-action="like-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '">' + heartIcon + '<span class="num">' + (c.likes || 0) + '</span></button>'
    +     replyBtn + editBtn + delBtn
    +   '</div></div>';
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
  editor.innerHTML = '<textarea></textarea><div class="edit-actions">'
    + '<button type="button" class="edit-cancel">' + escapeHtml(tr('cancel')) + '</button>'
    + '<button type="button" class="edit-save">' + escapeHtml(tr('save')) + '</button></div>';
  textEl.style.display = 'none';
  container.insertBefore(editor, textEl);
  var ta = editor.querySelector('textarea');
  ta.value = initialText; ta.focus();
  try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch(e) {}
  function close() { if (editor.parentNode) editor.parentNode.removeChild(editor); textEl.style.display = ''; }
  editor.querySelector('.edit-cancel').addEventListener('click', close);
  editor.querySelector('.edit-save').addEventListener('click', async function(){
    var v = ta.value.trim(); if (!v) return;
    var b = editor.querySelector('.edit-save'); b.disabled = true;
    try {
      await onSave(v);
      textEl.setAttribute('data-raw', v);
      textEl.innerHTML = linkifyText(v);
      close();
    } catch(e) { alert(tr(e.message) || e.message); b.disabled = false; }
  });
}
function checkEmptyFeed(parent) {
  if (!parent || parent.id !== 'feed') return;
  if (!parent.querySelector('.post-card')) parent.innerHTML = '<div class="empty">' + escapeHtml(tr('no_posts')) + '</div>';
}
function bindPostActions(root) {
  root.querySelectorAll('[data-action]').forEach(function(btn){
    if (btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', async function(e){
      e.preventDefault(); e.stopPropagation();
      var action = btn.dataset.action;
      var postId = btn.dataset.postId;
      var commentId = btn.dataset.commentId;
      if (action === 'like') {
        var snap = applyLikeUI(btn);
        state.suppressRefresh = Date.now() + 2000;
        try { await api('/api/posts/' + postId + '/like', { method:'POST', body:{} }); }
        catch(err) { revertLike(btn, snap); alert(tr(err.message) || err.message); }
        return;
      }
      if (action === 'like-comment') {
        var snap2 = applyLikeUI(btn);
        state.suppressRefresh = Date.now() + 2000;
        try { await api('/api/posts/' + postId + '/comments/' + commentId + '/like', { method:'POST', body:{} }); }
        catch(err) { revertLike(btn, snap2); alert(tr(err.message) || err.message); }
        return;
      }
      if (action === 'open-post') { navigate('/p/' + postId); return; }
      if (action === 'copy') {
        try { var pp = await api('/api/posts/' + postId); copyText(pp.text || ''); } catch(e) {}
        return;
      }
      if (action === 'quote') {
        try {
          var p = await api('/api/posts/' + postId);
          state.quotePostId = postId;
          state.quotePreview = { author: p.author, text: p.text };
          saveQuoteState();
          if (state.view !== 'feed') { navigate('/'); return; }
          renderQuoteBox('post');
          var inp = document.getElementById('postInput');
          if (inp) { inp.value = inp.value; inp.focus(); autoGrow(inp); }
          var s = document.getElementById('postSend'), sm = document.getElementById('postSendMobile');
          if (s) s.disabled = false;
          if (sm) sm.disabled = false;
        } catch(e) { alert(tr(e.message) || e.message); }
        return;
      }
      if (action === 'reply') {
        state.replyTo = { id: commentId, author: btn.dataset.author || '' };
        if (state.view !== 'post') { navigate('/p/' + postId); return; }
        if (state._renderReplyBanner) state._renderReplyBanner();
        var ta = document.getElementById('commentInput'); if (ta) ta.focus();
        return;
      }
      if (action === 'edit-post') {
        var postEl = btn.closest('.post-card');
        var textEl = postEl.querySelector('.post-text'); if (!textEl) return;
        var raw = textEl.getAttribute('data-raw') || '';
        state.suppressRefresh = Date.now() + 2000;
        startInlineEdit(postEl, textEl, raw, async function(newText){
          await api('/api/posts/' + postId, { method:'PUT', body:{ text:newText } });
          feedCache.all = { posts:null, query:null };
          feedCache.subs = { posts:null, query:null };
        });
        return;
      }
      if (action === 'delete-post') {
        showConfirm(tr('confirm_delete'), async function(){
          var postEl = document.querySelector('[data-post-id="' + postId + '"]');
          var parentEl = postEl ? postEl.parentNode : null;
          var nextSib = postEl ? postEl.nextSibling : null;
          if (postEl && parentEl) parentEl.removeChild(postEl);
          state.suppressRefresh = Date.now() + 2000;
          feedCache.all = { posts:null, query:null };
          feedCache.subs = { posts:null, query:null };
          try {
            await api('/api/posts/' + postId, { method:'DELETE' });
            checkEmptyFeed(parentEl);
          } catch(err) {
            if (postEl && parentEl) {
              if (nextSib && nextSib.parentNode === parentEl) parentEl.insertBefore(postEl, nextSib);
              else parentEl.appendChild(postEl);
            }
            alert(tr(err.message) || err.message);
          }
        }, { yesText: tr('confirm_delete_yes') });
        return;
      }
      if (action === 'edit-comment') {
        var cEl = btn.closest('.comment');
        var cTextEl = cEl.querySelector('.comment-text');
        var cRaw = cTextEl.getAttribute('data-raw') || '';
        state.suppressRefresh = Date.now() + 2000;
        startInlineEdit(cEl, cTextEl, cRaw, async function(newText){
          await api('/api/posts/' + postId + '/comments/' + commentId, { method:'PUT', body:{ text:newText } });
        });
        return;
      }
      if (action === 'delete-comment') {
        showConfirm(tr('confirm_delete'), async function(){
          var cEl = document.querySelector('[data-comment-id="' + commentId + '"]');
          var removed = 0;
          if (cEl) {
            if (!cEl.classList.contains('reply')) {
              var nx = cEl.nextElementSibling;
              while (nx && nx.classList && nx.classList.contains('reply')) {
                var toRemove = nx; nx = nx.nextElementSibling;
                if (toRemove.parentNode) toRemove.parentNode.removeChild(toRemove);
                removed++;
              }
            }
            if (cEl.parentNode) cEl.parentNode.removeChild(cEl);
            removed++;
          }
          if (removed > 0) decrementCommentCount(postId, removed);
          state.suppressRefresh = Date.now() + 2000;
          try { await api('/api/posts/' + postId + '/comments/' + commentId, { method:'DELETE' }); }
          catch(err) { alert(tr(err.message) || err.message); if (state.view === 'post') loadPostView(); }
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
function bindRefreshBtn(id) {
  var b = document.getElementById(id || 'refreshBtn'); if (!b) return;
  b.addEventListener('click', async function(){
    if (b.disabled) return;
    b.classList.add('spinning'); b.disabled = true;
    var start = Date.now();
    try { await refreshCurrentView({ force:true }); } catch(e) {}
    var elapsed = Date.now() - start; var minMs = 600;
    if (elapsed < minMs) await new Promise(function(r){ setTimeout(r, minMs - elapsed); });
    b.classList.remove('spinning'); b.disabled = false;
  });
}
async function refreshCurrentView(opts) {
  opts = opts || {};
  if (state.view === 'feed') await loadFeed(opts);
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

/* Регистрация экранов в UI-API */
UI.registerScreen('feed',          renderFeedView);
UI.registerScreen('post',          renderPostView);
UI.registerScreen('profile',       renderProfileView);
UI.registerScreen('users',         renderUsersView);
UI.registerScreen('notifications', renderNotificationsView);
UI.registerScreen('settings',      renderSettingsView);
UI.registerScreen('policy',        renderPolicyView);
UI.registerScreen('followers',     renderFollowListView);
UI.registerScreen('following',     renderFollowListView);

(async function init() {
  try {
    var rs = sessionStorage.getItem('SLD_reload_settings');
    if (rs) {
      var obj = JSON.parse(rs);
      if (obj && typeof obj === 'object') {
        state.settingsOpen = !!obj.open;
        if (obj.section) state.settingsSection = obj.section;
      }
      sessionStorage.removeItem('SLD_reload_settings');
    }
  } catch(e) {}
  await loadUIManifest();
  if (state.token) {
    state.booting = true;
    renderRoot();
    await loadMe();
    renderRoot();
    if (state.user) { connectSSE(); refreshCounters(); updateRoom(); maybeShowWelcome(); }
    else { connectSSE(); }
  } else {
    state.booting = false;
    renderRoot();
    connectSSE();
  }
  startFallbackPoll();
})();
"""


# ============================================================================
# Page render (with server-side OpenGraph)
# ============================================================================
def _render_og_meta(og: Optional[dict], t: dict, init_fav: str) -> str:
    if not og:
        og = {
            "site_name": t["site_name"],
            "title": t["site_name"],
            "description": t["auth_tagline"],
            "url": "",
            "type": "website",
            "image": None,
        }
    title = og.get("title") or og.get("site_name") or t["site_name"]
    description = og.get("description") or t["auth_tagline"]
    url = og.get("url") or ""
    ogtype = og.get("type") or "website"
    site_name = og.get("site_name") or t["site_name"]
    image = og.get("image") or ""
    lines = [
        f'<meta name="description" content="{_og_escape(description)}" />',
        f'<meta property="og:site_name" content="{_og_escape(site_name)}" />',
        f'<meta property="og:type" content="{_og_escape(ogtype)}" />',
        f'<meta property="og:title" content="{_og_escape(title)}" />',
        f'<meta property="og:description" content="{_og_escape(description)}" />',
    ]
    if url:
        lines.append(f'<meta property="og:url" content="{_og_escape(url)}" />')
    if image:
        lines.append(f'<meta property="og:image" content="{_og_escape(image)}" />')
        lines.append('<meta name="twitter:card" content="summary_large_image" />')
        lines.append(f'<meta name="twitter:image" content="{_og_escape(image)}" />')
    else:
        lines.append('<meta name="twitter:card" content="summary" />')
    lines.append(f'<meta name="twitter:title" content="{_og_escape(title)}" />')
    lines.append(f'<meta name="twitter:description" content="{_og_escape(description)}" />')
    return "\n".join(lines)


def render_page(lang: str, view: str, view_data: Optional[dict] = None,
                og: Optional[dict] = None) -> str:
    t = TEXTS[lang]
    view_data = view_data or {}
    emoji_names = {e[0]: {"ru": e[1], "en": e[2]} for e in EMOJI_DATA}
    js = (JS
          .replace("__I_HOME__", json.dumps(I_HOME))
          .replace("__I_USERS__", json.dumps(I_USERS))
          .replace("__I_USER__", json.dumps(I_USER))
          .replace("__I_USER_LG__", json.dumps(I_USER_LG))
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
          .replace("__I_CHEVRON__", json.dumps(I_CHEVRON))
          .replace("__I_SEARCH__", json.dumps(I_SEARCH))
          .replace("__I_MOON__", json.dumps(I_MOON))
          .replace("__I_SUN__", json.dumps(I_SUN))
          .replace("__I_QUOTE__", json.dumps(I_QUOTE))
          .replace("__I_CAL__", json.dumps(I_CAL))
          .replace("__I_CHECK__", json.dumps(I_CHECK))
          .replace("__I_CHECK_LG__", json.dumps(I_CHECK_LG))
          .replace("__I_X__", json.dumps(I_X))
          .replace("__I_MOBILE__", json.dumps(I_MOBILE))
          .replace("__I_DESKTOP__", json.dumps(I_DESKTOP))
          .replace("__I_SERVER__", json.dumps(I_SERVER))
          .replace("__I_EYE__", json.dumps(I_EYE))
          .replace("__I_EYE_OFF__", json.dumps(I_EYE_OFF))
          .replace("__I_LOCK__", json.dumps(I_LOCK))
          .replace("__I_AT__", json.dumps(I_AT))
          .replace("__I_INFO__", json.dumps(I_INFO))
          .replace("__I_REFRESH__", json.dumps(I_REFRESH))
          .replace("__I_GLOBE__", json.dumps(I_GLOBE))
          .replace("__EMOJI_NAMES__", json.dumps(emoji_names, ensure_ascii=False))
          .replace("__FAVICON_RU_DARK__", FAVICON_RU_DARK)
          .replace("__FAVICON_RU_LIGHT__", FAVICON_RU_LIGHT)
          .replace("__FAVICON_EN_DARK__", FAVICON_EN_DARK)
          .replace("__FAVICON_EN_LIGHT__", FAVICON_EN_LIGHT))
    init_fav = FAVICON_RU_DARK if lang == "ru" else FAVICON_EN_DARK
    og_html = _render_og_meta(og, t, init_fav)
    return ('<!DOCTYPE html>\n'
        f'<html lang="{lang}" data-theme="dark">\n'
        '<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />\n'
        '<meta name="color-scheme" content="dark light" />\n'
        '<meta name="theme-color" content="#0a0a0b" />\n'
        f'<link id="sld-favicon" rel="icon" type="image/svg+xml" href="{init_fav}" />\n'
        + og_html + '\n'
        f'<title>{t["site_name"]}</title>\n'
        '<style>' + CSS + '</style>\n'
        '</head>\n'
        '<body data-mode="boot">\n'
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
    init_fav = FAVICON_RU_DARK if lang == "ru" else FAVICON_EN_DARK
    return ('<!DOCTYPE html>\n'
        f'<html lang="{lang}" data-theme="dark">\n'
        '<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />\n'
        '<meta name="color-scheme" content="dark light" />\n'
        f'<link id="sld-favicon" rel="icon" type="image/svg+xml" href="{init_fav}" />\n'
        f'<title>{t["site_name"]} — 404</title>\n'
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


# ============================================================================
# HTML routes
# ============================================================================
@app.get("/", response_class=HTMLResponse)
def page_index(request: Request):
    lang = get_lang(request)
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    og["title"] = f"{t['site_name']} — {t['page_title_feed']}"
    og["description"] = t["auth_tagline"]
    return render_page(lang, "feed", og=og)


@app.get("/p/{post_id}", response_class=HTMLResponse)
def page_post(post_id: str, request: Request):
    p = db_get_post(post_id)
    if not p:
        raise HTTPException(404, "not found")
    lang = get_lang(request)
    og = build_og_post(p, request, lang)
    return render_page(lang, "post", {"post_id": post_id}, og=og)


@app.get("/u/{nick}", response_class=HTMLResponse)
def page_user(nick: str, request: Request):
    lang = get_lang(request)
    u = db_load_user(nick)
    if u:
        og = build_og_user(u, request, lang)
    else:
        og = _build_og_base(request, lang)
    return render_page(lang, "profile", {"nick": nick}, og=og)


@app.get("/u/{nick}/followers", response_class=HTMLResponse)
def page_followers(nick: str, request: Request):
    lang = get_lang(request)
    u = db_load_user(nick)
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    og["title"] = f"{t['page_title_followers']} — @{nick} — {t['site_name']}"
    if u:
        og["description"] = (u.get("bio") or "")[:300] or f"@{nick}"
    return render_page(lang, "followers", {"nick": nick}, og=og)


@app.get("/u/{nick}/following", response_class=HTMLResponse)
def page_following(nick: str, request: Request):
    lang = get_lang(request)
    u = db_load_user(nick)
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    og["title"] = f"{t['page_title_following']} — @{nick} — {t['site_name']}"
    if u:
        og["description"] = (u.get("bio") or "")[:300] or f"@{nick}"
    return render_page(lang, "following", {"nick": nick}, og=og)


@app.get("/users", response_class=HTMLResponse)
def page_users(request: Request):
    lang = get_lang(request)
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    og["title"] = f"{t['page_title_users']} — {t['site_name']}"
    return render_page(lang, "users", og=og)


@app.get("/notifications", response_class=HTMLResponse)
def page_notifications(request: Request):
    lang = get_lang(request)
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    og["title"] = f"{t['page_title_notifications']} — {t['site_name']}"
    return render_page(lang, "notifications", og=og)


@app.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request):
    lang = get_lang(request)
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    og["title"] = f"{t['page_title_settings']} — {t['site_name']}"
    return render_page(lang, "settings", og=og)


@app.get("/settings/profile", response_class=HTMLResponse)
def page_edit_profile(request: Request):
    lang = get_lang(request)
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    og["title"] = f"{t['page_title_settings']} — {t['site_name']}"
    return render_page(lang, "settings", og=og)


@app.get("/policy", response_class=HTMLResponse)
def page_policy(request: Request):
    lang = get_lang(request)
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    og["title"] = f"{t['page_title_policy']} — {t['site_name']}"
    og["description"] = t["policy_content"][:300]
    return render_page(lang, "policy", og=og)


@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request):
    lang = get_lang(request)
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    og["title"] = f"{t['page_title_register']} — {t['site_name']}"
    og["description"] = t["auth_tagline"]
    return render_page(lang, "login", og=og)


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    lang = get_lang(request)
    t = TEXTS[lang]
    og = _build_og_base(request, lang)
    og["title"] = f"{t['page_title_login']} — {t['site_name']}"
    og["description"] = t["auth_tagline"]
    return render_page(lang, "login", og=og)
