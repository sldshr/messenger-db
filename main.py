# -*- coding: utf-8 -*-
"""
СЛД — форум-соцсеть. Один файл.
Регистрация, вход, профиль с фото, темы, комментарии, поиск.
БД — Supabase (service_role).

Запуск:
    export SUPABASE_URL=...
    export SUPABASE_KEY=...
    uvicorn main:app --reload --port 8000
"""

import os
import io
import re
import html
import base64
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from supabase import create_client
from PIL import Image


# ===========================================================================
# Конфигурация
# ===========================================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Не заданы SUPABASE_URL и/или SUPABASE_KEY")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

SESSION_COOKIE = "sld_sid"
SESSION_DAYS = 30

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
POST_MAX = 5000
COMMENT_MAX = 2000
ABOUT_MAX = 2000

AVATAR_UPLOAD_MAX = 10 * 1024 * 1024
AVATAR_TARGET_BYTES = 150 * 1024
AVATAR_MAX_SIDE = 512

app = FastAPI(title="СЛД", docs_url=None, redoc_url=None)


# ===========================================================================
# Базовые утилиты
# ===========================================================================

def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iters = 120_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), iters)
    return f"pbkdf2_sha256${iters}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, hexhash = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), int(iters))
        return secrets.compare_digest(dk.hex(), hexhash)
    except Exception:
        return False


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн",
          "июл", "авг", "сен", "окт", "ноя", "дек"]


def fmt_dt(value: Any) -> str:
    dt = parse_dt(value)
    if not dt:
        return ""
    dt = dt.astimezone()
    now = datetime.now(timezone.utc).astimezone()
    if dt.date() == now.date():
        return f"сегодня, {dt:%H:%M}"
    if (now.date() - dt.date()).days == 1:
        return f"вчера, {dt:%H:%M}"
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}"


def fmt_iso(value: Any) -> str:
    dt = parse_dt(value)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def avatar_color(name: str) -> str:
    h = int(hashlib.md5((name or "?").encode("utf-8")).hexdigest()[:6], 16)
    r = 90 + (h & 0x5F)
    g = 80 + ((h >> 8) & 0x5F)
    b = 70 + ((h >> 16) & 0x4F)
    return f"#{r:02x}{g:02x}{b:02x}"


def avatar_html(username: str, avatar: Optional[str], size: int = 40) -> str:
    if avatar:
        return (f'<img class="av" src="{esc(avatar)}" '
                f'width="{size}" height="{size}" alt="" loading="lazy">')
    letter = esc((username or "?")[0].upper())
    color = avatar_color(username or "?")
    return (f'<div class="av av-letter" style="background:{color};'
            f'width:{size}px;height:{size}px;'
            f'font-size:{int(size * 0.42)}px">{letter}</div>')


def process_avatar(raw: bytes) -> Optional[str]:
    """Ресайз до 512 по длинной стороне и сжатие JPEG до 150 КБ."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (24, 21, 18))  # тёмный фон — под тему
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    img.thumbnail((AVATAR_MAX_SIDE, AVATAR_MAX_SIDE), Image.LANCZOS)

    def encode(im: Image.Image, q: int) -> bytes:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=q, optimize=True, progressive=True)
        return buf.getvalue()

    for q in (88, 82, 76, 70, 64, 58, 52, 46, 40, 34, 28):
        data = encode(img, q)
        if len(data) <= AVATAR_TARGET_BYTES:
            return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")

    w, h = img.size
    for scale in (0.85, 0.7, 0.6, 0.5, 0.4, 0.3):
        small = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                           Image.LANCZOS)
        data = encode(small, 70)
        if len(data) <= AVATAR_TARGET_BYTES:
            return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")

    data = encode(img.resize((128, 128), Image.LANCZOS), 55)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


# ===========================================================================
# Иконки (inline SVG)
# ===========================================================================

_ICONS = {
    "logo": '<path d="M12 3l2.7 5.7 6.3.9-4.6 4.4 1.1 6.2L12 17.3 6.5 20.2l1.1-6.2L3 9.6l6.3-.9L12 3z" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linejoin="round"/>',
    "feed": '<path d="M4 5h16M4 10h16M4 15h16M4 20h11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    "users": '<circle cx="9" cy="8" r="3.2" stroke="currentColor" stroke-width="1.7" fill="none"/><path d="M3.5 19.5c.3-3 2.7-5 5.5-5s5.2 2 5.5 5" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round"/><circle cx="17" cy="9" r="2.3" stroke="currentColor" stroke-width="1.7" fill="none"/><path d="M14.5 18c.3-1.9 1.3-3 2.5-3s2.2 1.1 2.5 3" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round"/>',
    "user": '<circle cx="12" cy="8" r="3.5" stroke="currentColor" stroke-width="1.7" fill="none"/><path d="M5 20c.5-3.5 3.5-6 7-6s6.5 2.5 7 6" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round"/>',
    "cog": '<circle cx="12" cy="12" r="2.8" stroke="currentColor" stroke-width="1.7" fill="none"/><path d="M12 3v2.6M12 18.4V21M3 12h2.6M18.4 12H21M5.6 5.6l1.9 1.9M16.5 16.5l1.9 1.9M5.6 18.4l1.9-1.9M16.5 7.5l1.9-1.9" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    "logout": '<path d="M15 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h9" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round"/><path d="M10 12h10m0 0l-3-3m3 3l-3 3" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "login": '<path d="M9 4h9a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H9" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round"/><path d="M14 12H4m0 0l3-3m-3 3l3 3" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "signup": '<circle cx="10" cy="8" r="3.3" stroke="currentColor" stroke-width="1.7" fill="none"/><path d="M4 20c.4-3.3 3-5.7 6-5.7s5.6 2.4 6 5.7" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round"/><path d="M18 8v6M15 11h6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    "edit": '<path d="M4 20h4l10-10-4-4L4 16v4z" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/><path d="M14 6l4 4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "comment": '<path d="M4 5h16v11H8l-4 4V5z" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/>',
    "send": '<path d="M3 12l18-8-6 18-3-7-9-3z" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/>',
    "plus": '<path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
    "search": '<circle cx="11" cy="11" r="6.5" stroke="currentColor" stroke-width="1.7" fill="none"/><path d="M16 16l4 4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    "back": '<path d="M20 12H4m0 0l6-6m-6 6l6 6" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "next": '<path d="M4 12h16m0 0l-6-6m6 6l-6 6" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "clock": '<circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.7" fill="none"/><path d="M12 7v5l3 2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" fill="none"/>',
    "hash": '<path d="M5 9h14M5 15h14M10 4l-2 16M16 4l-2 16" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" fill="none"/>',
    "file": '<path d="M6 3h8l4 4v14H6z" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/><path d="M14 3v4h4" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/>',
    "alert": '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7" fill="none"/><path d="M12 7v6M12 16.5v.5" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
    "check": '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7" fill="none"/><path d="M8 12.5l3 3 5-6" stroke="currentColor" stroke-width="1.9" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="2" stroke="currentColor" stroke-width="1.7" fill="none"/><circle cx="9" cy="10" r="1.7" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M4 18l5-5 4 4 3-3 4 4" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "home": '<path d="M4 11l8-7 8 7v9H4z" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/><path d="M10 20v-5h4v5" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/>',
    "tag": '<path d="M3 12l9-9h9v9l-9 9z" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/><circle cx="16" cy="8" r="1.6" stroke="currentColor" stroke-width="1.5" fill="none"/>',
}


def ic(name: str, size: int = 16, cls: str = "") -> str:
    p = _ICONS.get(name, "")
    c = f"ic {cls}".strip()
    return (f"<svg class='{c}' width='{size}' height='{size}' "
            f"viewBox='0 0 24 24' fill='none' aria-hidden='true'>{p}</svg>")


# ===========================================================================
# Сессии
# ===========================================================================

def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    sb.table("sessions").insert({
        "token": token, "user_id": user_id,
        "expires_at": expires.isoformat(),
    }).execute()
    return token


def set_session_cookie(resp: RedirectResponse, token: str) -> None:
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 24 * 3600,
                    httponly=True, samesite="lax", path="/")


def current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        rows = (sb.table("sessions").select("token,user_id,expires_at")
                .eq("token", token).limit(1).execute().data)
    except Exception:
        return None
    if not rows:
        return None
    exp = parse_dt(rows[0].get("expires_at"))
    if not exp or exp < datetime.now(timezone.utc):
        try:
            sb.table("sessions").delete().eq("token", token).execute()
        except Exception:
            pass
        return None
    try:
        u = (sb.table("users").select("*")
             .eq("id", rows[0]["user_id"]).limit(1).execute().data)
    except Exception:
        return None
    return u[0] if u else None


def destroy_session(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            sb.table("sessions").delete().eq("token", token).execute()
        except Exception:
            pass


# ===========================================================================
# Стиль
# ===========================================================================

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#131110;--bg-2:#191614;--surface:#1e1a17;--surface-hi:#26211c;
  --border:#302922;--border-2:#40372e;
  --text:#ebe4d8;--dim:#9c9183;--mute:#6b6156;
  --accent:#e0a04a;--accent-hi:#f2bb6b;--accent-bg:rgba(224,160,74,.10);
  --danger:#d0766a;--ok:#88b86e;
  --mono:ui-monospace,"JetBrains Mono","SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif;
  --radius:6px;
}
html,body{height:100%}
body{
  font-family:var(--sans);font-size:14px;line-height:1.55;
  color:var(--text);background:var(--bg);
  -webkit-font-smoothing:antialiased;
}
a{color:var(--accent);text-decoration:none}
a:hover{color:var(--accent-hi)}
svg{display:block}
button{font-family:inherit;cursor:pointer}
kbd{
  display:inline-block;padding:1px 6px;border-radius:3px;
  background:var(--surface-hi);border:1px solid var(--border-2);
  font-family:var(--mono);font-size:10px;color:var(--text);
  box-shadow:0 1px 0 var(--border-2);
}

/* Topbar */
.topbar{
  background:linear-gradient(180deg,#1c1815,#141110);
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:50;
}
.topbar-in{
  max-width:1120px;margin:0 auto;height:56px;
  display:flex;align-items:center;gap:20px;padding:0 20px;
}
.brand{
  display:flex;align-items:center;gap:9px;
  font-family:var(--mono);font-weight:700;font-size:16px;
  letter-spacing:2.5px;color:var(--text);flex:none;
}
.brand:hover{color:var(--accent-hi)}
.brand svg{color:var(--accent)}
.topnav{display:flex;gap:3px;align-items:center}
.topnav a{
  display:flex;align-items:center;gap:7px;
  padding:7px 12px;border-radius:5px;color:var(--dim);
  font-size:13px;font-weight:500;transition:background .12s,color .12s;
}
.topnav a:hover{background:var(--surface);color:var(--text)}
.topnav a.on{background:var(--accent-bg);color:var(--accent-hi)}
.topnav a.on svg{color:var(--accent)}
.topnav a svg{color:var(--mute)}
.topnav a:hover svg{color:var(--text)}

.userarea{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:13px;flex:none}
.userarea .who{
  display:flex;align-items:center;gap:9px;
  padding:4px 12px 4px 4px;border-radius:100px;
  background:var(--surface);border:1px solid var(--border);
  color:var(--text);
}
.userarea .who:hover{border-color:var(--border-2);color:var(--text)}
.userarea .who .av{border-radius:50%}
.userarea .who .uname{font-weight:600}
.iconbtn{
  display:flex;align-items:center;justify-content:center;
  width:34px;height:34px;border-radius:6px;
  color:var(--dim);background:transparent;
  border:1px solid transparent;transition:all .12s;
}
.iconbtn:hover{background:var(--surface);color:var(--text);border-color:var(--border)}

/* Layout */
.wrap{max-width:1120px;margin:0 auto;padding:22px 20px 60px}
.cols{display:grid;grid-template-columns:236px 1fr;gap:22px;align-items:start}
.side{position:sticky;top:78px;display:flex;flex-direction:column;gap:14px}
.content{min-width:0;display:flex;flex-direction:column;gap:14px}

/* Sidebar */
.side-block{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);overflow:hidden;
}
.side-title{
  padding:9px 14px;font-family:var(--mono);font-size:10.5px;
  letter-spacing:1.6px;text-transform:uppercase;
  color:var(--mute);border-bottom:1px solid var(--border);
  background:var(--bg-2);
}
.side-menu{display:flex;flex-direction:column;padding:6px}
.side-menu a{
  display:flex;align-items:center;gap:9px;
  padding:8px 10px;border-radius:5px;
  color:var(--dim);font-size:13px;transition:all .12s;
}
.side-menu a:hover{background:var(--surface-hi);color:var(--text)}
.side-menu a.on{background:var(--accent-bg);color:var(--accent-hi)}
.side-menu a svg{color:var(--mute);flex:none}
.side-menu a.on svg{color:var(--accent)}
.side-menu a:hover svg{color:var(--text)}

/* Card */
.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);overflow:hidden;
}
.card-head{
  padding:12px 16px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px;background:var(--bg-2);
}
.card-head .h-ic{color:var(--accent);flex:none}
.card-head h2{
  font-family:var(--mono);font-size:12.5px;font-weight:600;
  letter-spacing:.6px;color:var(--text);
}
.card-head .h-count{
  margin-left:auto;font-family:var(--mono);font-size:11px;
  color:var(--mute);
}
.card-body{padding:16px}

/* Buttons */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:7px;
  padding:8px 15px;border-radius:5px;
  font-size:13px;font-weight:500;line-height:1;
  background:var(--surface-hi);color:var(--text);
  border:1px solid var(--border-2);transition:all .12s;
}
.btn:hover{background:#2e2822;border-color:#4e4338;color:var(--text)}
.btn:active{transform:translateY(1px)}
.btn-primary{
  background:var(--accent);color:#1a1410;
  border-color:var(--accent);font-weight:600;
}
.btn-primary:hover{background:var(--accent-hi);border-color:var(--accent-hi);color:#1a1410}
.btn-ghost{background:transparent;border-color:var(--border);color:var(--dim)}
.btn-ghost:hover{color:var(--text);border-color:var(--border-2);background:var(--surface)}
.btn-sm{padding:5px 10px;font-size:12px}

/* Forms */
.field{display:flex;flex-direction:column;gap:5px;margin-bottom:14px}
.field label{font-size:12px;color:var(--dim);font-weight:500}
.field input[type=text],.field input[type=password]{
  padding:9px 12px;background:var(--bg-2);
  border:1px solid var(--border-2);border-radius:5px;
  color:var(--text);font-size:14px;transition:border-color .12s;
}
.field input:focus{outline:none;border-color:var(--accent);background:#1c1815}
textarea{
  width:100%;padding:11px 13px;background:var(--bg-2);
  border:1px solid var(--border-2);border-radius:5px;
  color:var(--text);font-size:14px;line-height:1.6;
  resize:vertical;transition:border-color .12s;font-family:inherit;
  min-height:80px;
}
textarea:focus{outline:none;border-color:var(--accent);background:#1c1815}
textarea::placeholder,input::placeholder{color:var(--mute)}
.hint{font-size:11px;color:var(--mute);font-family:var(--mono)}

/* Alerts */
.alert{
  display:flex;align-items:flex-start;gap:10px;
  padding:11px 14px;border-radius:var(--radius);
  font-size:13px;border:1px solid;
}
.alert svg{flex:none;margin-top:1px}
.alert-err{background:rgba(208,118,106,.08);border-color:rgba(208,118,106,.32);color:#e5a297}
.alert-err svg{color:var(--danger)}
.alert-ok{background:rgba(136,184,110,.08);border-color:rgba(136,184,110,.3);color:#b5d5a4}
.alert-ok svg{color:var(--ok)}

/* Avatar */
.av{
  display:block;border-radius:6px;object-fit:cover;
  background:var(--surface-hi);flex:none;
}
.av-letter{
  display:flex;align-items:center;justify-content:center;
  color:#fff;font-weight:700;text-shadow:0 1px 1px rgba(0,0,0,.4);
}

/* Post */
.post{padding:16px;border-bottom:1px solid var(--border);display:flex;gap:14px}
.post:last-child{border-bottom:none}
.post-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:8px}
.post-head{display:flex;align-items:center;gap:9px;font-size:13px;flex-wrap:wrap}
.post-author{font-weight:600;color:var(--text)}
.post-author:hover{color:var(--accent-hi)}
.post-time{
  color:var(--mute);font-size:11px;font-family:var(--mono);
  display:inline-flex;align-items:center;gap:4px;
}
.post-content{
  white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;
  font-size:14px;line-height:1.62;
}
.post-actions{
  display:flex;gap:4px;align-items:center;flex-wrap:wrap;
  padding-top:10px;border-top:1px solid var(--border);
}
.post-actions a,.post-actions button{
  display:inline-flex;align-items:center;gap:5px;
  padding:4px 10px;border-radius:4px;
  background:transparent;border:1px solid transparent;
  color:var(--dim);font-size:12px;transition:all .12s;
}
.post-actions a:hover,.post-actions button:hover{
  background:var(--surface-hi);color:var(--text);border-color:var(--border);
}
.post-actions svg{color:var(--mute);flex:none}
.post-actions a:hover svg,.post-actions button:hover svg{color:var(--text)}
.post-actions .danger:hover{color:#e5a297}
.post-actions .danger:hover svg{color:var(--danger)}
.post-actions form{display:inline;margin:0}

/* Empty */
.empty{
  padding:48px 20px;text-align:center;color:var(--mute);
  display:flex;flex-direction:column;align-items:center;gap:10px;
}
.empty svg{color:var(--border-2)}
.empty .big{font-family:var(--mono);font-size:12.5px;letter-spacing:.5px}

/* Breadcrumbs */
.crumbs{
  display:flex;align-items:center;gap:8px;font-size:12px;
  font-family:var(--mono);color:var(--mute);
}
.crumbs a{color:var(--dim)}
.crumbs a:hover{color:var(--accent)}
.crumbs svg{color:var(--border-2);flex:none}

/* Profile */
.profile-head{display:flex;gap:20px;padding:20px}
.profile-info{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}
.profile-name{
  font-size:22px;font-weight:700;color:var(--text);
  font-family:var(--mono);letter-spacing:-.3px;line-height:1.15;
}
.profile-status{
  color:var(--dim);font-style:italic;margin:7px 0 12px;font-size:13px;
}
.info-list{display:flex;flex-direction:column;margin-top:2px}
.info-row{
  display:flex;gap:12px;padding:7px 0;font-size:13px;
  border-bottom:1px dashed var(--border);
}
.info-row:last-child{border-bottom:none}
.info-row .k{
  color:var(--mute);font-family:var(--mono);font-size:10.5px;
  letter-spacing:.6px;width:130px;flex:none;text-transform:uppercase;
  padding-top:3px;
}

/* Mini user */
.mini-user{display:flex;gap:11px;align-items:center;padding:13px 14px}
.mini-user .uinfo{min-width:0;flex:1}
.mini-user .uname{font-weight:600;color:var(--text);font-size:13px;display:block}
.mini-user .uname:hover{color:var(--accent-hi)}
.mini-user .umeta{font-size:11px;color:var(--mute);font-family:var(--mono);margin-top:2px}

/* Comment */
.comment{
  display:flex;gap:12px;padding:14px 16px;border-bottom:1px solid var(--border);
}
.comment:last-child{border-bottom:none}
.comment-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:5px}
.comment-text{
  white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;
  font-size:13.5px;
}

/* Search */
.searchbar{
  display:flex;gap:10px;align-items:center;
  padding:0 14px;background:var(--surface);
  border:1px solid var(--border);border-radius:var(--radius);
  transition:border-color .12s;
}
.searchbar:focus-within{border-color:var(--accent)}
.searchbar .s-ic{color:var(--mute);flex:none}
.searchbar input{
  flex:1;background:transparent;border:none;color:var(--text);
  font-size:14px;padding:12px 0;
}
.searchbar input:focus{outline:none}
.searchbar .kbd-hint{font-family:var(--mono);font-size:10px;color:var(--mute)}

/* Char counter */
.cc{
  font-family:var(--mono);font-size:11px;color:var(--mute);
  text-align:right;margin-top:4px;transition:color .12s;
}
.cc.warn{color:var(--accent)}
.cc.over{color:var(--danger)}

/* Footer */
.foot{
  max-width:1120px;margin:0 auto;padding:22px 20px;
  color:var(--mute);font-size:11.5px;font-family:var(--mono);
  display:flex;justify-content:space-between;align-items:center;gap:16px;
  border-top:1px solid var(--border);flex-wrap:wrap;
}
.foot a{color:var(--dim)}
.foot a:hover{color:var(--accent)}

@media (max-width:860px){
  .cols{grid-template-columns:1fr}
  .side{position:static}
  .topbar-in{flex-wrap:wrap;height:auto;padding:10px 14px;gap:8px}
  .userarea{margin-left:auto}
  .topnav{order:3;width:100%;overflow-x:auto}
  .wrap{padding:16px}
  .profile-head{flex-direction:column;align-items:center;text-align:center}
  .profile-head .info-list{text-align:left;width:100%}
  .info-row .k{width:110px}
}
"""

JS = """
(function(){
  // Ctrl/Cmd + Enter -> submit nearest form
  document.addEventListener('keydown', function(e){
    if((e.ctrlKey||e.metaKey) && e.key === 'Enter'){
      var t = e.target;
      if(t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT')){
        var f = t.closest('form');
        if(f){
          if(typeof f.requestSubmit === 'function') f.requestSubmit();
          else f.submit();
        }
      }
    }
  });
  // Char counters
  document.querySelectorAll('textarea[data-max]').forEach(function(t){
    var max = parseInt(t.dataset.max, 10);
    if(!max) return;
    var box = document.createElement('div');
    box.className = 'cc';
    function upd(){
      var n = t.value.length;
      box.textContent = n + ' / ' + max;
      box.className = 'cc' + (n > max ? ' over' : (n > max * 0.9 ? ' warn' : ''));
    }
    t.addEventListener('input', upd);
    t.parentNode.insertBefore(box, t.nextSibling);
    upd();
  });
  // "/" -> focus search
  document.addEventListener('keydown', function(e){
    if(e.key === '/' && !/^(INPUT|TEXTAREA)$/.test(e.target.tagName)){
      var s = document.querySelector('input[name="q"]');
      if(s){ e.preventDefault(); s.focus(); s.select(); }
    }
  });
})();
"""


# ===========================================================================
# Каркас страницы
# ===========================================================================

def layout(title: str, user: Optional[dict], body: str, active: str = "") -> str:
    # Top-right
    if user:
        un = esc(user["username"])
        av = avatar_html(user["username"], user.get("avatar"), 26)
        userarea = (
            f'<a class="who" href="/u/{un}" title="Мой профиль">'
            f'{av}<span class="uname">{un}</span></a>'
            f'<a class="iconbtn" href="/settings" title="Настройки">{ic("cog", 18)}</a>'
            f'<a class="iconbtn" href="/logout" title="Выход">{ic("logout", 18)}</a>'
        )
    else:
        userarea = (
            f'<a class="btn btn-sm btn-ghost" href="/login">{ic("login", 15)}<span>Вход</span></a>'
            f'<a class="btn btn-sm btn-primary" href="/register">{ic("signup", 15)}<span>Регистрация</span></a>'
        )

    # Top nav
    nav_items = [
        ("/", "Лента", "feed", "feed"),
        ("/people", "Участники", "users", "people"),
    ]
    if user:
        nav_items.append((f"/u/{esc(user['username'])}", "Профиль", "user", "me"))

    nav_html = "".join(
        f'<a href="{h}"{" class=\'on\'" if k == active else ""}>{ic(icn, 16)}<span>{n}</span></a>'
        for h, n, icn, k in nav_items
    )

    # Sidebar
    side = ""

    # Mini user card
    if user:
        un = esc(user["username"])
        side += (
            f'<div class="side-block"><div class="mini-user">'
            f'{avatar_html(user["username"], user.get("avatar"), 44)}'
            f'<div class="uinfo"><a class="uname" href="/u/{un}">{un}</a>'
            f'<div class="umeta">{esc(fmt_dt(user.get("created_at")))}</div></div>'
            f'</div></div>'
        )

    # Nav menu
    menu_items = [("/", "Лента", "feed", "feed"),
                  ("/people", "Участники", "users", "people")]
    if user:
        menu_items.append((f"/u/{esc(user['username'])}", "Моя страница", "user", "me"))
        menu_items.append(("/settings", "Настройки", "cog", "settings"))

    menu_html = "".join(
        f'<a href="{h}"{" class=\'on\'" if k == active else ""}>{ic(icn, 16)}<span>{n}</span></a>'
        for h, n, icn, k in menu_items
    )
    side += f'<div class="side-block"><div class="side-title">Навигация</div><div class="side-menu">{menu_html}</div></div>'

    # Auth block if not logged in
    if not user:
        side += (
            '<div class="side-block"><div class="side-title">Вход</div>'
            '<div style="padding:14px">'
            '<form method="post" action="/login">'
            '<div class="field"><label>Логин</label>'
            '<input type="text" name="username" maxlength="20" style="width:100%"></div>'
            '<div class="field"><label>Пароль</label>'
            '<input type="password" name="password" style="width:100%"></div>'
            f'<button class="btn btn-primary" type="submit" style="width:100%">{ic("login", 15)}<span>Войти</span></button>'
            '</form></div></div>'
        )

    # Shortcuts
    side += (
        '<div class="side-block"><div class="side-title">Горячие клавиши</div>'
        '<div style="padding:10px 14px;font-size:12px;color:var(--dim);'
        'display:flex;flex-direction:column;gap:8px">'
        '<div><kbd>/</kbd> — поиск</div>'
        '<div><kbd>Ctrl</kbd>+<kbd>Enter</kbd> — отправить</div>'
        '</div></div>'
    )

    # About
    side += (
        '<div class="side-block"><div class="side-title">О проекте</div>'
        '<div style="padding:12px 14px;font-size:12px;color:var(--dim);line-height:1.55">'
        'СЛД — небольшой форум. Темы, комментарии, профили. '
        'Никакой рекламы и лишнего.'
        '</div></div>'
    )

    year = datetime.now().year
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — СЛД</title>
<style>{CSS}</style>
</head>
<body>

<div class="topbar"><div class="topbar-in">
  <a class="brand" href="/">{ic("logo", 22)}<span>СЛД</span></a>
  <nav class="topnav">{nav_html}</nav>
  <div class="userarea">{userarea}</div>
</div></div>

<div class="wrap"><div class="cols">
  <aside class="side">{side}</aside>
  <main class="content">{body}</main>
</div></div>

<div class="foot">
  <div>© {year} СЛД · форум</div>
  <div><a href="/">Лента</a> · <a href="/people">Участники</a></div>
</div>

<script>{JS}</script>
</body>
</html>"""


def alert_html(kind: str, text: str) -> str:
    if kind == "err":
        return f'<div class="alert alert-err">{ic("alert", 16)}<div>{esc(text)}</div></div>'
    return f'<div class="alert alert-ok">{ic("check", 16)}<div>{esc(text)}</div></div>'


def crumbs(items: list) -> str:
    parts = [f'<a href="/">{ic("home", 13)}</a>']
    for i, (label, href) in enumerate(items):
        parts.append(ic("next", 13))
        if href:
            parts.append(f'<a href="{esc(href)}">{esc(label)}</a>')
        else:
            parts.append(f'<span>{esc(label)}</span>')
    return f'<div class="crumbs">{"".join(parts)}</div>'


def empty_html(text: str) -> str:
    return (f'<div class="empty">{ic("file", 36)}'
            f'<div class="big">{esc(text)}</div></div>')


# ===========================================================================
# Помощники выборок и рендера
# ===========================================================================

POST_SELECT = "id,user_id,content,created_at,updated_at,users(username,avatar)"


def fetch_posts(limit: int = 50, user_id: Optional[str] = None,
                q: Optional[str] = None) -> list:
    query = (sb.table("posts").select(POST_SELECT)
             .order("created_at", desc=True).limit(limit))
    if user_id:
        query = query.eq("user_id", user_id)
    if q:
        query = query.ilike("content", f"%{q}%")
    return query.execute().data or []


def comment_counts_for(post_ids: list) -> dict:
    if not post_ids:
        return {}
    try:
        rows = (sb.table("comments").select("post_id")
                .in_("post_id", post_ids).execute().data) or []
    except Exception:
        return {}
    d: dict = {}
    for r in rows:
        d[r["post_id"]] = d.get(r["post_id"], 0) + 1
    return d


def render_posts(posts: list, me: Optional[dict], counts: dict,
                 empty_text: str = "Пока ничего нет.") -> str:
    if not posts:
        return empty_html(empty_text)

    out = []
    for p in posts:
        u = p.get("users") or {}
        if isinstance(u, list):
            u = u[0] if u else {}
        uname = u.get("username") or "удалён"
        pid = p.get("id")
        cnt = counts.get(pid, 0)

        edited = ""
        if p.get("updated_at"):
            edited = f' <span class="post-time">· ред. {esc(fmt_dt(p["updated_at"]))}</span>'

        actions = []
        if me and p.get("user_id") == me["id"]:
            actions.append(f'<a href="/posts/{pid}/edit" title="Редактировать">{ic("edit", 14)}<span>правка</span></a>')
            actions.append(
                f'<form method="post" action="/posts/{pid}/delete" '
                f'onsubmit="return confirm(\'Удалить тему?\')">'
                f'<button type="submit" class="danger" title="Удалить">{ic("trash", 14)}<span>удалить</span></button></form>'
            )
        actions_html = "".join(actions)

        out.append(f"""<article class="post" id="p{pid}">
  {avatar_html(uname, u.get("avatar"), 42)}
  <div class="post-body">
    <div class="post-head">
      <a class="post-author" href="/u/{esc(uname)}">{esc(uname)}</a>
      <span class="post-time" title="{esc(fmt_iso(p.get('created_at')))}">{ic("clock", 12)}{esc(fmt_dt(p.get("created_at")))}</span>{edited}
    </div>
    <div class="post-content">{esc(p.get('content', ''))}</div>
    <div class="post-actions">
      <a href="/posts/{pid}" title="Обсуждение">{ic("comment", 14)}<span>{cnt} {esc(plural(cnt, "коммент.", "коммент.", "коммент."))}</span></a>
      {actions_html}
      <a href="#p{pid}" title="Ссылка на запись" style="margin-left:auto">{ic("tag", 14)}<span>#{pid}</span></a>
    </div>
  </div>
</article>""")
    return "".join(out)


# ===========================================================================
# Главная
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str = "", err: str = "", q: str = ""):
    me = current_user(request)
    q = (q or "").strip()

    try:
        posts = fetch_posts(q=q or None)
    except Exception as e:
        posts = []
        err = err or f"Ошибка БД: {e}"

    counts = comment_counts_for([p["id"] for p in posts])

    alerts = ""
    if err:
        alerts += alert_html("err", err)
    if msg:
        alerts += alert_html("ok", msg)

    # Поиск
    search = (
        f'<form method="get" action="/" class="searchbar">'
        f'{ic("search", 16, "s-ic")}'
        f'<input type="text" name="q" placeholder="Поиск по темам..." value="{esc(q)}" autocomplete="off">'
        f'<span class="kbd-hint">/</span>'
        f'</form>'
    )

    # Форма новой темы
    if me:
        compose = f"""<section class="card">
  <div class="card-head">{ic("plus", 16, "h-ic")}<h2>Новая тема</h2></div>
  <div class="card-body">
    <form method="post" action="/posts">
      <textarea name="content" rows="4" maxlength="{POST_MAX}" data-max="{POST_MAX}"
        placeholder="Напишите что-нибудь. Ctrl+Enter — отправить."></textarea>
      <div style="margin-top:10px;display:flex;align-items:center;gap:10px">
        <button class="btn btn-primary" type="submit">{ic("send", 15)}<span>Опубликовать</span></button>
        <span class="hint">Ctrl+Enter</span>
      </div>
    </form>
  </div>
</section>"""
    else:
        compose = f"""<section class="card">
  <div class="card-body" style="display:flex;align-items:center;gap:12px">
    {ic("user", 18, "h-ic")}
    <div style="color:var(--dim)">Чтобы открывать темы, нужно <a href="/login">войти</a> или <a href="/register">зарегистрироваться</a>.</div>
  </div>
</section>"""

    n = len(posts)
    heading = f'Результаты поиска: «{esc(q)}»' if q else "Последние темы"
    clear = f'<a href="/" class="btn btn-sm btn-ghost" style="margin-left:auto">{ic("back", 13)}<span>сбросить</span></a>' if q else ""

    body = f"""{alerts}
{search}
{compose}
<section class="card">
  <div class="card-head">{ic("feed", 16, "h-ic")}<h2>{heading}</h2>{clear}<span class="h-count">{n}</span></div>
  {render_posts(posts, me, counts, empty_text=("Ничего не найдено." if q else "Тем пока нет. Будьте первым."))}
</section>"""
    return HTMLResponse(layout("Лента", me, body, active="feed"))


# ===========================================================================
# Регистрация / вход / выход
# ===========================================================================

def _auth_page(title: str, action: str, err: str = "", username: str = "") -> str:
    alert = alert_html("err", err) if err else ""

    if action == "/register":
        fields = f"""
  <div class="field"><label>Имя пользователя</label>
    <input type="text" name="username" maxlength="20" value="{esc(username)}" autofocus autocomplete="username"></div>
  <div class="hint" style="margin:-10px 0 14px">3–20 символов: латиница, цифры, подчёркивание</div>
  <div class="field"><label>Пароль</label>
    <input type="password" name="password" autocomplete="new-password"></div>
  <div class="field"><label>Пароль ещё раз</label>
    <input type="password" name="password2" autocomplete="new-password"></div>
  <button class="btn btn-primary" type="submit" style="width:100%">{ic("signup", 15)}<span>Создать аккаунт</span></button>"""
    else:
        fields = f"""
  <div class="field"><label>Имя пользователя</label>
    <input type="text" name="username" maxlength="20" value="{esc(username)}" autofocus autocomplete="username"></div>
  <div class="field"><label>Пароль</label>
    <input type="password" name="password" autocomplete="current-password"></div>
  <button class="btn btn-primary" type="submit" style="width:100%">{ic("login", 15)}<span>Войти</span></button>"""

    body = f"""{crumbs([(title, None)])}
<div style="max-width:420px">
  <section class="card">
    <div class="card-head">{ic("user", 16, "h-ic")}<h2>{esc(title)}</h2></div>
    <div class="card-body">
      {alert}
      <form method="post" action="{action}">{fields}</form>
    </div>
  </section>
</div>"""
    return layout(title, None, body)


@app.get("/register", response_class=HTMLResponse)
def register_get(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_auth_page("Регистрация", "/register"))


@app.post("/register", response_class=HTMLResponse)
def register_post(request: Request,
                  username: str = Form(""),
                  password: str = Form(""),
                  password2: str = Form("")):
    if current_user(request):
        return RedirectResponse("/", status_code=303)

    username = username.strip()
    err = None
    if not USERNAME_RE.fullmatch(username):
        err = "Имя: 3–20 символов, только латиница, цифры и подчёркивание."
    elif len(password) < 6:
        err = "Пароль должен быть не короче 6 символов."
    elif password != password2:
        err = "Пароли не совпадают."
    else:
        try:
            if (sb.table("users").select("id")
                    .ilike("username", username).limit(1).execute().data):
                err = "Такое имя уже занято."
        except Exception as e:
            err = f"Ошибка БД: {e}"

    if err:
        return HTMLResponse(_auth_page("Регистрация", "/register", err, username),
                            status_code=400)

    try:
        row = sb.table("users").insert({
            "username": username,
            "password_hash": hash_password(password),
        }).execute().data[0]
    except Exception as e:
        return HTMLResponse(_auth_page("Регистрация", "/register",
                                       f"Не удалось создать: {e}", username),
                            status_code=500)

    token = create_session(row["id"])
    resp = RedirectResponse("/", status_code=303)
    set_session_cookie(resp, token)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_auth_page("Вход", "/login"))


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request,
               username: str = Form(""),
               password: str = Form("")):
    if current_user(request):
        return RedirectResponse("/", status_code=303)

    username = username.strip()
    err = None
    row = None
    try:
        rows = (sb.table("users").select("*")
                .ilike("username", username).limit(1).execute().data)
        row = rows[0] if rows else None
    except Exception as e:
        err = f"Ошибка БД: {e}"

    if not err and (not row or not verify_password(password, row["password_hash"])):
        err = "Неверное имя пользователя или пароль."

    if err:
        return HTMLResponse(_auth_page("Вход", "/login", err, username),
                            status_code=400)

    token = create_session(row["id"])
    resp = RedirectResponse("/", status_code=303)
    set_session_cookie(resp, token)
    return resp


@app.get("/logout")
def logout(request: Request):
    destroy_session(request)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ===========================================================================
# Темы
# ===========================================================================

@app.post("/posts")
def create_post(request: Request, content: str = Form("")):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    content = (content or "").strip()
    if not content:
        return RedirectResponse("/?err=Пустое+сообщение", status_code=303)
    if len(content) > POST_MAX:
        content = content[:POST_MAX]

    try:
        sb.table("posts").insert({"user_id": me["id"], "content": content}).execute()
    except Exception:
        return RedirectResponse("/?err=Не+удалось+сохранить", status_code=303)

    return RedirectResponse("/?msg=Тема+опубликована", status_code=303)


@app.get("/posts/{post_id}", response_class=HTMLResponse)
def post_detail(request: Request, post_id: int, msg: str = "", err: str = ""):
    me = current_user(request)

    try:
        rows = (sb.table("posts").select(POST_SELECT)
                .eq("id", post_id).limit(1).execute().data)
    except Exception as e:
        body = alert_html("err", f"Ошибка БД: {e}")
        return HTMLResponse(layout("Ошибка", me, body), status_code=500)

    if not rows:
        return HTMLResponse(layout("Не найдено", me, empty_html("Тема не найдена.")),
                            status_code=404)

    p = rows[0]
    u = p.get("users") or {}
    if isinstance(u, list):
        u = u[0] if u else {}
    uname = u.get("username") or "удалён"

    try:
        comments = (sb.table("comments")
                    .select("id,content,created_at,user_id,users(username,avatar)")
                    .eq("post_id", post_id)
                    .order("created_at", desc=False).execute().data) or []
    except Exception:
        comments = []

    # Пост
    post_actions = ""
    if me and p.get("user_id") == me["id"]:
        post_actions = (
            f'<a href="/posts/{post_id}/edit" title="Редактировать">{ic("edit", 14)}<span>правка</span></a>'
            f'<form method="post" action="/posts/{post_id}/delete" '
            f'onsubmit="return confirm(\'Удалить тему?\')">'
            f'<button type="submit" class="danger" title="Удалить">{ic("trash", 14)}<span>удалить</span></button></form>'
        )

    edited = ""
    if p.get("updated_at"):
        edited = f' <span class="post-time">· ред. {esc(fmt_dt(p["updated_at"]))}</span>'

    post_html = f"""<article class="post" style="border-bottom:none;padding:18px">
  {avatar_html(uname, u.get("avatar"), 56)}
  <div class="post-body">
    <div class="post-head">
      <a class="post-author" href="/u/{esc(uname)}" style="font-size:15px">{esc(uname)}</a>
      <span class="post-time" title="{esc(fmt_iso(p.get('created_at')))}">{ic("clock", 12)}{esc(fmt_dt(p.get("created_at")))}</span>{edited}
    </div>
    <div class="post-content" style="font-size:15px">{esc(p.get('content', ''))}</div>
    <div class="post-actions">{post_actions}</div>
  </div>
</article>"""

    # Комментарии
    if comments:
        items = []
        for c in comments:
            cu = c.get("users") or {}
            if isinstance(cu, list):
                cu = cu[0] if cu else {}
            cname = cu.get("username") or "удалён"
            del_btn = ""
            if me and c.get("user_id") == me["id"]:
                del_btn = (
                    f'<form method="post" action="/comments/{c["id"]}/delete" '
                    f'onsubmit="return confirm(\'Удалить комментарий?\')">'
                    f'<button type="submit" class="danger" title="Удалить">{ic("trash", 13)}<span>удалить</span></button></form>'
                )
            items.append(f"""<div class="comment" id="c{c['id']}">
  {avatar_html(cname, cu.get("avatar"), 34)}
  <div class="comment-body">
    <div class="post-head">
      <a class="post-author" href="/u/{esc(cname)}">{esc(cname)}</a>
      <span class="post-time" title="{esc(fmt_iso(c.get('created_at')))}">{ic("clock", 11)}{esc(fmt_dt(c.get('created_at')))}</span>
      <a href="#c{c['id']}" class="post-time" style="margin-left:auto">#{c['id']}</a>
    </div>
    <div class="comment-text">{esc(c.get('content', ''))}</div>
    {f'<div class="post-actions" style="border:none;padding-top:0">{del_btn}</div>' if del_btn else ''}
  </div>
</div>""")
        comments_html = "".join(items)
    else:
        comments_html = empty_html("Комментариев пока нет.")

    # Форма коммента
    if me:
        form_html = f"""<section class="card">
  <div class="card-head">{ic("comment", 16, "h-ic")}<h2>Ваш комментарий</h2></div>
  <div class="card-body">
    <form method="post" action="/posts/{post_id}/comments">
      <textarea name="content" rows="4" maxlength="{COMMENT_MAX}" data-max="{COMMENT_MAX}"
        placeholder="Написать комментарий... Ctrl+Enter — отправить."></textarea>
      <div style="margin-top:10px;display:flex;align-items:center;gap:10px">
        <button class="btn btn-primary" type="submit">{ic("send", 15)}<span>Отправить</span></button>
        <span class="hint">Ctrl+Enter</span>
      </div>
    </form>
  </div>
</section>"""
    else:
        form_html = f"""<section class="card">
  <div class="card-body" style="color:var(--dim);display:flex;align-items:center;gap:10px">
    {ic("user", 18, "h-ic")}
    Чтобы оставить комментарий, <a href="/login">войдите</a> или <a href="/register">зарегистрируйтесь</a>.
  </div>
</section>"""

    alerts = ""
    if err:
        alerts += alert_html("err", err)
    if msg:
        alerts += alert_html("ok", msg)

    cnt = len(comments)
    body = f"""{crumbs([("Лента", "/"), (f"Тема #{post_id}", None)])}
{alerts}
<section class="card">
  <div class="card-head">{ic("file", 16, "h-ic")}<h2>Тема #{post_id}</h2>
    <span class="h-count">{cnt} {esc(plural(cnt, "комментарий", "комментария", "комментариев"))}</span>
  </div>
  {post_html}
</section>

<section class="card">
  <div class="card-head">{ic("comment", 16, "h-ic")}<h2>Обсуждение</h2><span class="h-count">{cnt}</span></div>
  {comments_html}
</section>

{form_html}

<div><a href="/" class="btn btn-ghost">{ic("back", 14)}<span>К ленте</span></a></div>"""
    return HTMLResponse(layout(f"Тема #{post_id}", me, body))


@app.get("/posts/{post_id}/edit", response_class=HTMLResponse)
def post_edit_get(request: Request, post_id: int):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("posts").select("id,user_id,content")
            .eq("id", post_id).limit(1).execute().data)
    if not rows:
        return HTMLResponse(layout("Не найдено", me, empty_html("Тема не найдена.")),
                            status_code=404)
    p = rows[0]
    if p["user_id"] != me["id"]:
        return HTMLResponse(layout("Отказано", me,
                                   alert_html("err", "Это не ваша тема.")),
                            status_code=403)

    body = f"""{crumbs([("Лента", "/"), (f"Тема #{post_id}", f"/posts/{post_id}"), ("Правка", None)])}
<section class="card">
  <div class="card-head">{ic("edit", 16, "h-ic")}<h2>Редактирование темы #{post_id}</h2></div>
  <div class="card-body">
    <form method="post" action="/posts/{post_id}/edit">
      <textarea name="content" rows="10" maxlength="{POST_MAX}" data-max="{POST_MAX}">{esc(p['content'])}</textarea>
      <div style="margin-top:10px;display:flex;gap:10px">
        <button class="btn btn-primary" type="submit">{ic("check", 15)}<span>Сохранить</span></button>
        <a class="btn btn-ghost" href="/posts/{post_id}">Отмена</a>
      </div>
    </form>
  </div>
</section>"""
    return HTMLResponse(layout("Редактирование", me, body))


@app.post("/posts/{post_id}/edit")
def post_edit_post(request: Request, post_id: int, content: str = Form("")):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("posts").select("id,user_id")
            .eq("id", post_id).limit(1).execute().data)
    if not rows or rows[0]["user_id"] != me["id"]:
        return RedirectResponse("/", status_code=303)

    content = (content or "").strip()
    if not content:
        return RedirectResponse(f"/posts/{post_id}/edit", status_code=303)
    if len(content) > POST_MAX:
        content = content[:POST_MAX]

    sb.table("posts").update({
        "content": content,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", post_id).execute()

    return RedirectResponse(f"/posts/{post_id}?msg=Тема+обновлена", status_code=303)


@app.post("/posts/{post_id}/delete")
def post_delete(request: Request, post_id: int):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("posts").select("id,user_id")
            .eq("id", post_id).limit(1).execute().data)
    if rows and rows[0]["user_id"] == me["id"]:
        sb.table("posts").delete().eq("id", post_id).execute()

    return RedirectResponse("/?msg=Тема+удалена", status_code=303)


# ===========================================================================
# Комментарии
# ===========================================================================

@app.post("/posts/{post_id}/comments")
def comment_create(request: Request, post_id: int, content: str = Form("")):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    content = (content or "").strip()
    if not content:
        return RedirectResponse(f"/posts/{post_id}?err=Пустой+комментарий", status_code=303)
    if len(content) > COMMENT_MAX:
        content = content[:COMMENT_MAX]

    if not sb.table("posts").select("id").eq("id", post_id).limit(1).execute().data:
        return RedirectResponse("/", status_code=303)

    sb.table("comments").insert({
        "post_id": post_id, "user_id": me["id"], "content": content,
    }).execute()

    return RedirectResponse(f"/posts/{post_id}?msg=Комментарий+добавлен", status_code=303)


@app.post("/comments/{comment_id}/delete")
def comment_delete(request: Request, comment_id: int):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    rows = (sb.table("comments").select("id,user_id,post_id")
            .eq("id", comment_id).limit(1).execute().data)
    if not rows:
        return RedirectResponse("/", status_code=303)

    c = rows[0]
    if c["user_id"] == me["id"]:
        sb.table("comments").delete().eq("id", comment_id).execute()

    return RedirectResponse(f"/posts/{c['post_id']}?msg=Комментарий+удалён",
                            status_code=303)


# ===========================================================================
# Профиль
# ===========================================================================

def _profile_info_rows(u: dict) -> str:
    rows = [
        ("Имя", u.get("username")),
        ("Город", u.get("city")),
        ("День рождения", u.get("birthday")),
        ("Сайт", u.get("site")),
        ("На сайте с", fmt_dt(u.get("created_at"))),
    ]
    html_rows = []
    for k, v in rows:
        if not v:
            continue
        if k == "Сайт":
            link = esc(v)
            if not link.startswith("http"):
                link = "http://" + link
            v_html = f'<a href="{link}" target="_blank" rel="noopener">{esc(v)}</a>'
        else:
            v_html = esc(v)
        html_rows.append(f'<div class="info-row"><div class="k">{esc(k)}</div><div>{v_html}</div></div>')
    if not html_rows:
        return '<div class="muted" style="color:var(--mute);font-size:12px">Информация не заполнена.</div>'
    return "".join(html_rows)


@app.get("/u/{username}", response_class=HTMLResponse)
def profile(request: Request, username: str, msg: str = "", err: str = ""):
    me = current_user(request)

    rows = (sb.table("users").select("*")
            .ilike("username", username).limit(1).execute().data)
    if not rows:
        return HTMLResponse(layout("Не найдено", me,
                                   empty_html("Пользователь не найден.")),
                            status_code=404)
    owner = rows[0]
    is_me = bool(me and me["id"] == owner["id"])

    try:
        posts = fetch_posts(user_id=owner["id"])
    except Exception as e:
        posts = []
        err = err or f"Ошибка БД: {e}"
    counts = comment_counts_for([p["id"] for p in posts])

    status_line = esc(owner.get("status") or "")
    status_html = (f'<div class="profile-status">«{status_line}»</div>'
                   if status_line else
                   '<div class="profile-status" style="opacity:.6">статус не указан</div>')

    edit_link = ""
    if is_me:
        edit_link = (f'<div style="margin-top:12px">'
                     f'<a class="btn btn-sm" href="/settings">{ic("cog", 14)}<span>Редактировать профиль</span></a>'
                     f'</div>')

    about = owner.get("about")
    about_block = ""
    if about:
        about_block = f"""<section class="card">
  <div class="card-head">{ic("file", 16, "h-ic")}<h2>О себе</h2></div>
  <div class="card-body" style="white-space:pre-wrap;line-height:1.6">{esc(about)}</div>
</section>"""

    n = len(posts)
    cnt_word = plural(n, "запись", "записи", "записей")

    alerts = ""
    if err:
        alerts += alert_html("err", err)
    if msg:
        alerts += alert_html("ok", msg)

    body = f"""{crumbs([("Участники", "/people"), (owner['username'], None)])}
{alerts}
<section class="card">
  <div class="profile-head">
    {avatar_html(owner["username"], owner.get("avatar"), 128)}
    <div class="profile-info">
      <div class="profile-name">{esc(owner['username'])}</div>
      {status_html}
      <div class="info-list">{_profile_info_rows(owner)}</div>
      {edit_link}
    </div>
  </div>
</section>

{about_block}

<section class="card">
  <div class="card-head">{ic("feed", 16, "h-ic")}<h2>Записи участника</h2>
    <span class="h-count">{n} {esc(cnt_word)}</span>
  </div>
  {render_posts(posts, me, counts,
                empty_text=("Вы ещё ничего не написали." if is_me else "Записей нет."))}
</section>"""
    return HTMLResponse(layout(owner["username"], me, body,
                               active="me" if is_me else ""))


# ===========================================================================
# Настройки профиля
# ===========================================================================

@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request, msg: str = "", err: str = ""):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    alerts = ""
    if err:
        alerts += alert_html("err", err)
    if msg:
        alerts += alert_html("ok", msg)

    body = f"""{crumbs([(me['username'], f"/u/{esc(me['username'])}"), ("Настройки", None)])}
{alerts}
<section class="card">
  <div class="card-head">{ic("cog", 16, "h-ic")}<h2>Настройки профиля</h2></div>
  <div class="card-body">
    <form method="post" action="/settings" enctype="multipart/form-data">

      <div style="font-family:var(--mono);font-size:11px;letter-spacing:1.4px;
                  text-transform:uppercase;color:var(--mute);margin-bottom:10px">
        Фотография
      </div>
      <div style="display:flex;gap:16px;align-items:flex-start;margin-bottom:18px">
        <div>{avatar_html(me["username"], me.get("avatar"), 108)}</div>
        <div style="flex:1;color:var(--dim);font-size:12.5px;line-height:1.55">
          JPEG, PNG или GIF. Автоматически сжимается до 512&times;512 и не более 150 КБ.<br>
          <input type="file" name="avatar" accept="image/*" style="margin-top:8px;color:var(--dim)">
          <label style="display:flex;gap:7px;align-items:center;margin-top:8px;cursor:pointer">
            <input type="checkbox" name="avatar_remove" value="1">
            <span>удалить текущую фотографию</span>
          </label>
        </div>
      </div>

      <div style="font-family:var(--mono);font-size:11px;letter-spacing:1.4px;
                  text-transform:uppercase;color:var(--mute);margin-bottom:10px">
        Основное
      </div>

      <div class="field"><label>Статус</label>
        <input type="text" name="status" maxlength="200" style="width:100%"
               value="{esc(me.get('status') or '')}"
               placeholder="Короткая фраза о себе"></div>
      <div class="field"><label>Город</label>
        <input type="text" name="city" maxlength="100"
               value="{esc(me.get('city') or '')}"></div>
      <div class="field"><label>День рождения</label>
        <input type="text" name="birthday" maxlength="20"
               value="{esc(me.get('birthday') or '')}"
               placeholder="например, 12 мая"></div>
      <div class="field"><label>Сайт</label>
        <input type="text" name="site" maxlength="200"
               value="{esc(me.get('site') or '')}"
               placeholder="http://..."></div>
      <div class="field"><label>О себе</label>
        <textarea name="about" rows="7" maxlength="{ABOUT_MAX}" data-max="{ABOUT_MAX}"
          placeholder="Пара слов о себе...">{esc(me.get('about') or '')}</textarea></div>

      <div style="margin-top:14px;display:flex;gap:10px">
        <button class="btn btn-primary" type="submit">{ic("check", 15)}<span>Сохранить</span></button>
        <a class="btn btn-ghost" href="/u/{esc(me['username'])}">Отмена</a>
      </div>
    </form>
  </div>
</section>

<section class="card">
  <div class="card-head">{ic("cog", 16, "h-ic")}<h2>Смена пароля</h2></div>
  <div class="card-body">
    <form method="post" action="/settings/password">
      <div class="field"><label>Текущий пароль</label>
        <input type="password" name="old_password"></div>
      <div class="field"><label>Новый пароль</label>
        <input type="password" name="new_password"></div>
      <div class="field"><label>Повтор нового</label>
        <input type="password" name="new_password2"></div>
      <button class="btn btn-primary" type="submit">{ic("check", 15)}<span>Сменить пароль</span></button>
    </form>
  </div>
</section>"""
    return HTMLResponse(layout("Настройки", me, body, active="settings"))


@app.post("/settings")
async def settings_post(
    request: Request,
    status: str = Form(""),
    city: str = Form(""),
    about: str = Form(""),
    site: str = Form(""),
    birthday: str = Form(""),
    avatar_remove: str = Form(""),
    avatar: UploadFile = File(None),
):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    update = {
        "status":   (status.strip()[:200] or None),
        "city":     (city.strip()[:100] or None),
        "about":    (about.strip()[:ABOUT_MAX] or None),
        "site":     (site.strip()[:200] or None),
        "birthday": (birthday.strip()[:20] or None),
    }

    if avatar_remove:
        update["avatar"] = None
    elif avatar and avatar.filename:
        raw = await avatar.read()
        if not raw:
            return RedirectResponse("/settings?err=Пустой+файл", status_code=303)
        if len(raw) > AVATAR_UPLOAD_MAX:
            return RedirectResponse("/settings?err=Файл+слишком+большой", status_code=303)
        data_url = process_avatar(raw)
        if not data_url:
            return RedirectResponse("/settings?err=Не+удалось+обработать+изображение",
                                    status_code=303)
        update["avatar"] = data_url

    try:
        sb.table("users").update(update).eq("id", me["id"]).execute()
    except Exception:
        return RedirectResponse("/settings?err=Не+удалось+сохранить", status_code=303)

    return RedirectResponse("/settings?msg=Профиль+сохранён", status_code=303)


@app.post("/settings/password")
def settings_password(request: Request,
                      old_password: str = Form(""),
                      new_password: str = Form(""),
                      new_password2: str = Form("")):
    me = current_user(request)
    if not me:
        return RedirectResponse("/login", status_code=303)

    if not verify_password(old_password, me["password_hash"]):
        return RedirectResponse("/settings?err=Неверный+текущий+пароль", status_code=303)
    if len(new_password) < 6:
        return RedirectResponse("/settings?err=Пароль+короче+6+символов", status_code=303)
    if new_password != new_password2:
        return RedirectResponse("/settings?err=Пароли+не+совпадают", status_code=303)

    sb.table("users").update({
        "password_hash": hash_password(new_password),
    }).eq("id", me["id"]).execute()

    token = request.cookies.get(SESSION_COOKIE)
    try:
        sb.table("sessions").delete().eq("user_id", me["id"]).neq("token", token).execute()
    except Exception:
        pass

    return RedirectResponse("/settings?msg=Пароль+изменён", status_code=303)


# ===========================================================================
# Участники
# ===========================================================================

@app.get("/people", response_class=HTMLResponse)
def people(request: Request):
    me = current_user(request)
    try:
        users = (sb.table("users")
                 .select("id,username,avatar,city,status,created_at")
                 .order("created_at", desc=True).limit(200).execute().data) or []
        db_err = ""
    except Exception as e:
        users = []
        db_err = str(e)

    items = []
    for u in users:
        un = esc(u["username"])
        bits = []
        if u.get("city"):
            bits.append(f'{ic("home", 12)} {esc(u["city"])}')
        if u.get("status"):
            bits.append(f'«{esc(u["status"])}»')
        meta = " · ".join(bits)

        items.append(f"""<article class="post">
  {avatar_html(u['username'], u.get('avatar'), 42)}
  <div class="post-body">
    <div class="post-head">
      <a class="post-author" href="/u/{un}">{un}</a>
      <span class="post-time" title="{esc(fmt_iso(u.get('created_at')))}">{ic("clock", 11)}{esc(fmt_dt(u.get('created_at')))}</span>
    </div>
    {f'<div style="font-size:12.5px;color:var(--dim)">{meta}</div>' if meta else ''}
  </div>
</article>""")

    inner = "".join(items) if items else empty_html("Пока никого.")
    alerts = alert_html("err", db_err) if db_err else ""

    body = f"""{crumbs([("Участники", None)])}
{alerts}
<section class="card">
  <div class="card-head">{ic("users", 16, "h-ic")}<h2>Участники</h2>
    <span class="h-count">{len(users)}</span>
  </div>
  {inner}
</section>"""
    return HTMLResponse(layout("Участники", me, body, active="people"))


# ===========================================================================
# Служебное
# ===========================================================================

@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
