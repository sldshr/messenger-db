# main.py
# sldchat — минималистичный realtime-чат на FastAPI + WebSocket.
# Всё в памяти, ничего не пишет на диск.
# Запуск:  python main.py
# Админ:    admin / admin

import hashlib
import hmac
import html
import json
import re
import secrets
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

app = FastAPI(title="sldchat")

# ────────────────────────────────────────────────────────────
#  ХРАНИЛИЩЕ
# ────────────────────────────────────────────────────────────

USERS: Dict[str, dict] = {}
SESSIONS: Dict[str, dict] = {}                 # token -> {user, expires}
CHANNELS: Dict[str, dict] = {}                 # name -> {topic}
MESSAGES: Dict[str, List[dict]] = {}           # channel -> [msg]
WS_CLIENTS: Dict[WebSocket, dict] = {}         # ws -> {user, channel}
LOGIN_ATTEMPTS: Dict[str, List[float]] = {}
KICKS: Dict[str, float] = {}                   # username -> kicked_until

CONFIG = {
    "server_name": "sldchat",
    "motd": "Добро пожаловать в sldchat!",
    "max_msg_len": 500,
    "allow_registration": True,
}

SESSION_TTL = 86400 * 7
LOGIN_WINDOW = 60.0
LOGIN_MAX = 8
KICK_COOLDOWN = 60.0
REP_COOLDOWN = 86400.0
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,20}$")
CHAN_RE = re.compile(r"^[a-z0-9_\-]{2,30}$")


# ────────────────────────────────────────────────────────────
#  ХЕЛПЕРЫ
# ────────────────────────────────────────────────────────────

def hash_pw(password: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"{salt.hex()}${dk.hex()}"


def verify_pw(password: str, stored: str) -> bool:
    try:
        salt_hex, _ = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(hash_pw(password, salt), stored)


def new_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"user": username, "expires": time.time() + SESSION_TTL}
    return token


def get_session(request) -> Optional[str]:
    token = request.cookies.get("session")
    if not token:
        return None
    s = SESSIONS.get(token)
    if not s:
        return None
    if s["expires"] < time.time():
        SESSIONS.pop(token, None)
        return None
    return s["user"]


def rate_limited(ip: str) -> bool:
    now = time.time()
    bucket = LOGIN_ATTEMPTS.setdefault(ip, [])
    bucket[:] = [t for t in bucket if now - t < LOGIN_WINDOW]
    if len(bucket) >= LOGIN_MAX:
        return True
    bucket.append(now)
    return False


def check_origin(obj) -> bool:
    headers = obj.headers
    origin = headers.get("origin")
    if not origin:
        return True
    host = headers.get("host", "")
    return origin.endswith("//" + host) or origin.endswith("://" + host)


def make_user(password: str, is_admin: bool = False) -> dict:
    return {
        "password": hash_pw(password),
        "is_admin": is_admin,
        "joined": time.time(),
        "banned": False,
        "rep": 0,
        "rep_given_to": {},
        "stats": {
            "messages_total": 0,
            "by_day": {},
            "by_channel": {},
        },
    }


def add_channel(name: str, topic: str = "") -> bool:
    if not name:
        return False
    name = name.strip().lower().replace(" ", "-")
    if not CHAN_RE.match(name) or name in CHANNELS:
        return False
    CHANNELS[name] = {"topic": (topic or "Без описания")[:120]}
    MESSAGES[name] = []
    return True


def record_message(username: str, channel: str):
    u = USERS.get(username)
    if not u:
        return
    s = u.setdefault("stats", {})
    s["messages_total"] = s.get("messages_total", 0) + 1
    by_day = s.setdefault("by_day", {})
    today = date.today().isoformat()
    by_day[today] = by_day.get(today, 0) + 1
    by_ch = s.setdefault("by_channel", {})
    by_ch[channel] = by_ch.get(channel, 0) + 1
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    for k in list(by_day.keys()):
        if k < cutoff:
            del by_day[k]


# стартовые каналы
add_channel("general", "Общий чат")
add_channel("random", "Всякое")

USERS["admin"] = make_user("admin", is_admin=True)


# ────────────────────────────────────────────────────────────
#  BROADCAST
# ────────────────────────────────────────────────────────────

async def send_ws(ws: WebSocket, payload: dict):
    try:
        await ws.send_json(payload)
    except Exception:
        WS_CLIENTS.pop(ws, None)


def channel_users(channel: str) -> List[dict]:
    names = sorted({c["user"] for c in WS_CLIENTS.values() if c.get("channel") == channel})
    return [{"name": n, "rep": USERS[n]["rep"] if n in USERS else 0} for n in names]


def all_online() -> List[str]:
    return sorted({c["user"] for c in WS_CLIENTS.values() if c.get("user")})


def channels_payload() -> dict:
    return {"type": "channels",
            "channels": {n: {"topic": c["topic"]} for n, c in CHANNELS.items()}}


async def broadcast_all(payload: dict):
    for ws in list(WS_CLIENTS.keys()):
        await send_ws(ws, payload)


async def broadcast_channel(channel: str, payload: dict):
    for ws, c in list(WS_CLIENTS.items()):
        if c.get("channel") == channel:
            await send_ws(ws, payload)


async def broadcast_user(username: str, payload: dict):
    for ws, c in list(WS_CLIENTS.items()):
        if c.get("user") == username:
            await send_ws(ws, payload)


async def broadcast_user_list(channel: str):
    await broadcast_channel(channel, {
        "type": "users",
        "channel": channel,
        "users": channel_users(channel),
    })


async def broadcast_channels():
    await broadcast_all(channels_payload())


# ────────────────────────────────────────────────────────────
#  SVG ИКОНКИ
# ────────────────────────────────────────────────────────────

def svg(body: str, cls: str = "icon") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</svg>')


ICON_CHAT = svg('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>')
ICON_USERS = svg('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
                 '<circle cx="9" cy="7" r="4"/>'
                 '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>'
                 '<path d="M16 3.13a4 4 0 0 1 0 7.75"/>')
ICON_LOGOUT = svg('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
                  '<polyline points="16 17 21 12 16 7"/>'
                  '<line x1="21" y1="12" x2="9" y2="12"/>')
ICON_SETTINGS = svg('<circle cx="12" cy="12" r="3"/>'
                    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09A1.65 1.65 0 0 0 15 4.6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>')
ICON_SEND = svg('<line x1="22" y1="2" x2="11" y2="13"/>'
                '<polygon points="22 2 15 22 11 13 2 9 22 2"/>')
ICON_PLUS = svg('<line x1="12" y1="5" x2="12" y2="19"/>'
                '<line x1="5" y1="12" x2="19" y2="12"/>')
ICON_TRASH = svg('<polyline points="3 6 5 6 21 6"/>'
                 '<path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/>'
                 '<path d="M10 11v6M14 11v6"/>')
ICON_USER = svg('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>'
                '<circle cx="12" cy="7" r="4"/>')
ICON_PALETTE = svg('<circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/>'
                   '<circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/>'
                   '<circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/>'
                   '<circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/>'
                   '<path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/>')
ICON_KICK = svg('<circle cx="12" cy="12" r="10"/>'
                '<line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>')
ICON_DOWNLOAD = svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
                    '<polyline points="7 10 12 15 17 10"/>'
                    '<line x1="12" y1="15" x2="12" y2="3"/>')
ICON_UPLOAD = svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
                  '<polyline points="17 8 12 3 7 8"/>'
                  '<line x1="12" y1="3" x2="12" y2="15"/>')
ICON_THUMB_UP = svg('<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/>')
ICON_THUMB_DOWN = svg('<path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/>')


# ────────────────────────────────────────────────────────────
#  CSS + ТЕМЫ
# ────────────────────────────────────────────────────────────

CSS = """
:root{
  --bg:#eef1f5; --surface:#ffffff; --surface-2:#f7f8fa; --surface-3:#f0f2f5;
  --border:#e1e5eb; --border-strong:#d5dae0;
  --text:#2c2f38; --text-dim:#4b5563; --text-muted:#8891a0;
  --accent:#3a7bd5; --accent-hover:#2f66b5; --accent-soft:#e4ecfa;
  --danger:#e74c3c; --danger-hover:#c0392b;
  --success:#4caf7d; --warning:#e5a53c;
  --input-bg:#ffffff; --hover:#eef1f5;
  --shadow:0 1px 3px rgba(20,25,40,.05);
  --radius:4px;
}
[data-theme="dark"]{
  --bg:#14161c; --surface:#1c1f27; --surface-2:#171a21; --surface-3:#232732;
  --border:#2b2f3a; --border-strong:#3a4050;
  --text:#e6e9ef; --text-dim:#b6bccb; --text-muted:#7a8296;
  --accent:#5b9bf0; --accent-hover:#7cafe8; --accent-soft:#1e2a40;
  --danger:#e26d60; --danger-hover:#d34c3c;
  --success:#5fc08a; --warning:#e5a53c;
  --input-bg:#232732; --hover:#232732;
  --shadow:0 1px 3px rgba(0,0,0,.35);
}
[data-theme="solar"]{
  --bg:#fdf6e3; --surface:#fffbef; --surface-2:#f7efd8; --surface-3:#f2e8cd;
  --border:#e8dfc4; --border-strong:#d8cda9;
  --text:#584c34; --text-dim:#6e614a; --text-muted:#a49779;
  --accent:#b58900; --accent-hover:#9a7400; --accent-soft:#f4e8bf;
  --danger:#dc322f; --danger-hover:#b52624;
  --success:#859900; --warning:#cb4b16;
  --input-bg:#fffbef; --hover:#f2e8cd;
  --shadow:0 1px 3px rgba(120,100,50,.08);
}
[data-theme="ocean"]{
  --bg:#e8f0f7; --surface:#ffffff; --surface-2:#f0f6fc; --surface-3:#e3edf7;
  --border:#d5e2ee; --border-strong:#c1d3e5;
  --text:#1a2c42; --text-dim:#334a63; --text-muted:#6f86a3;
  --accent:#1e88e5; --accent-hover:#1565c0; --accent-soft:#d4e6f8;
  --danger:#e53935; --danger-hover:#c62828;
  --success:#43a047; --warning:#fb8c00;
  --input-bg:#ffffff; --hover:#e3edf7;
}
[data-theme="forest"]{
  --bg:#e8f2ec; --surface:#ffffff; --surface-2:#f1f8f3; --surface-3:#e3efe7;
  --border:#d3e4d8; --border-strong:#bfd7c6;
  --text:#1f3327; --text-dim:#3b5443; --text-muted:#77917f;
  --accent:#2e9e5b; --accent-hover:#26824a; --accent-soft:#d0e9d9;
  --danger:#c0392b; --danger-hover:#a02d21;
  --success:#2e9e5b; --warning:#c78b1e;
  --input-bg:#ffffff; --hover:#e3efe7;
}

*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
     background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;
     transition:background .2s,color .2s}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
button{font:inherit;cursor:pointer}
input,select,textarea{font:inherit;padding:8px 10px;border:1px solid var(--border-strong);
     border-radius:var(--radius);background:var(--input-bg);outline:none;width:100%;color:var(--text)}
input:focus,select:focus,textarea:focus{border-color:var(--accent)}
textarea{resize:vertical;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.btn{padding:8px 14px;border:1px solid transparent;border-radius:var(--radius);
     background:var(--accent);color:#fff;transition:background .15s;display:inline-flex;
     align-items:center;gap:6px;white-space:nowrap}
.btn:hover{background:var(--accent-hover);text-decoration:none}
.btn.secondary{background:var(--surface);border-color:var(--border-strong);color:var(--text)}
.btn.secondary:hover{background:var(--hover)}
.btn.danger{background:var(--danger)}
.btn.danger:hover{background:var(--danger-hover)}
.btn.sm{padding:5px 9px;font-size:12px}
.icon{width:16px;height:16px;vertical-align:-2px}
.icon-lg{width:22px;height:22px}
.muted{color:var(--text-muted)}
.small{font-size:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:720px){.grid2{grid-template-columns:1fr}}

/* AUTH */
.auth-wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.auth-box{width:100%;max-width:340px;background:var(--surface);border:1px solid var(--border);
     border-radius:var(--radius);padding:28px 24px 22px;box-shadow:var(--shadow)}
.auth-logo{display:flex;align-items:center;gap:8px;justify-content:center;margin-bottom:6px;color:var(--accent)}
.auth-logo .icon{width:28px;height:28px}
.auth-title{text-align:center;font-size:20px;font-weight:600;margin-bottom:4px}
.auth-sub{text-align:center;font-size:12px;color:var(--text-muted);margin-bottom:20px}
.auth-box input{margin-bottom:10px}
.auth-box .btn{width:100%;padding:10px;justify-content:center}
.auth-foot{text-align:center;margin-top:14px;font-size:13px}
.auth-err{background:color-mix(in srgb,var(--danger) 12%,transparent);
     color:var(--danger);border:1px solid color-mix(in srgb,var(--danger) 40%,transparent);
     border-radius:var(--radius);padding:8px 10px;font-size:13px;margin-bottom:12px}
.auth-top{position:fixed;top:14px;right:14px;z-index:10}

/* APP */
.app{display:flex;height:100vh;background:var(--surface);overflow:hidden}
.sidebar{width:230px;flex:0 0 230px;background:var(--surface-2);border-right:1px solid var(--border);
     display:flex;flex-direction:column}
.brand{display:flex;align-items:center;gap:8px;padding:16px;font-weight:600;font-size:15px;
     border-bottom:1px solid var(--border);color:var(--text)}
.brand .icon{color:var(--accent);width:18px;height:18px}
.section-title{padding:14px 16px 6px;font-size:11px;text-transform:uppercase;
     letter-spacing:.6px;color:var(--text-muted);font-weight:600}
.channels{list-style:none;flex:1;overflow-y:auto;padding-bottom:8px}
.channels a{display:flex;align-items:center;gap:8px;padding:7px 16px;color:var(--text-dim);font-size:13px}
.channels a:hover{background:var(--hover);text-decoration:none}
.channels a.active{background:var(--accent-soft);color:var(--accent);font-weight:600}
.channels .hash{color:var(--text-muted);font-weight:400}
.channels a.active .hash{color:var(--accent)}
.user-box{position:relative;display:flex;align-items:center;gap:10px;padding:10px 12px;
     border-top:1px solid var(--border);background:var(--surface-3)}
.avatar{width:30px;height:30px;border-radius:50%;background:var(--accent);color:#fff;
     display:flex;align-items:center;justify-content:center;font-weight:600;font-size:13px;flex:0 0 30px}
.user-meta{flex:1;min-width:0}
.user-name{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.user-role{font-size:11px;color:var(--text-muted)}
.user-actions{display:flex;gap:2px;align-items:center}
.user-actions a,.user-actions button{display:inline-flex;padding:5px;border-radius:var(--radius);
     color:var(--text-muted);background:none;border:none}
.user-actions a:hover,.user-actions button:hover{background:var(--border);color:var(--text);text-decoration:none}

.theme-menu{position:absolute;bottom:52px;right:8px;background:var(--surface);
     border:1px solid var(--border);border-radius:var(--radius);padding:6px;
     box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:50;min-width:150px}
.theme-menu.open{display:block}
.theme-menu button{display:flex;align-items:center;gap:9px;width:100%;padding:7px 10px;
     background:none;border:none;color:var(--text);font-size:13px;border-radius:3px;text-align:left}
.theme-menu button:hover{background:var(--hover)}
.theme-menu button.active{background:var(--accent-soft);color:var(--accent);font-weight:600}
.swatch{width:14px;height:14px;border-radius:50%;border:1px solid var(--border-strong);flex:0 0 14px}

.chat{flex:1;display:flex;flex-direction:column;min-width:0}
.chat-head{display:flex;align-items:center;gap:10px;padding:14px 18px;
     border-bottom:1px solid var(--border);background:var(--surface)}
.chat-head h2{font-size:16px;font-weight:600}
.chat-head .hash{color:var(--text-muted);font-size:18px}
.chat-head .topic{color:var(--text-muted);font-size:13px;margin-left:6px;
     white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.chat-head .user-count{display:inline-flex;align-items:center;gap:5px;color:var(--text-dim);
     font-size:12px;background:var(--surface-3);padding:4px 9px;border-radius:12px}
.messages{flex:1;overflow-y:auto;padding:14px 18px;background:var(--surface)}
.msg{padding:6px 0;font-size:14px}
.msg-user{font-weight:600;color:var(--accent)}
.msg-user a{color:inherit}
.msg-time{font-size:11px;color:var(--text-muted);margin-left:6px}
.msg-text{margin-top:1px;word-wrap:break-word;overflow-wrap:anywhere}
.system-msg{font-size:12px;color:var(--text-muted);padding:5px 0;font-style:italic}
.system-msg::before{content:"— ";color:var(--border-strong)}
.composer{display:flex;gap:8px;padding:12px 14px;border-top:1px solid var(--border);background:var(--surface-2)}
.composer input{flex:1;padding:10px 12px;border-radius:var(--radius);background:var(--input-bg)}
.composer button{background:var(--accent);color:#fff;border:none;border-radius:var(--radius);
     padding:0 16px;display:flex;align-items:center;justify-content:center;transition:.15s}
.composer button:hover{background:var(--accent-hover)}
.users-panel{width:210px;flex:0 0 210px;border-left:1px solid var(--border);
     background:var(--surface-2);display:flex;flex-direction:column}
.users-panel ul{list-style:none;padding:0 6px 12px;overflow-y:auto;flex:1}
.users-panel li{padding:6px 10px;font-size:13px;color:var(--text-dim);border-radius:3px;
     display:flex;align-items:center;gap:7px}
.users-panel li::before{content:"●";color:var(--success);font-size:9px;flex:0 0 auto}
.users-panel li .u-name{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.users-panel li .u-rep{font-size:11px;color:var(--text-muted);font-variant-numeric:tabular-nums}
.users-panel li .u-rep.pos{color:var(--success)}
.users-panel li .u-rep.neg{color:var(--danger)}

/* ADMIN */
.admin-wrap{max-width:1000px;margin:0 auto;padding:26px 20px}
.admin-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;gap:14px;flex-wrap:wrap}
.admin-head h1{font-size:20px;font-weight:600;display:flex;align-items:center;gap:8px}
.admin-head h1 .icon{color:var(--accent)}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border-strong);margin-bottom:18px;flex-wrap:wrap}
.tabs a{padding:9px 14px;color:var(--text-muted);font-size:13px;border-bottom:2px solid transparent;margin-bottom:-1px}
.tabs a:hover{color:var(--text);text-decoration:none}
.tabs a.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
     padding:18px;margin-bottom:16px;box-shadow:var(--shadow)}
.card h3{font-size:14px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:7px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:middle}
th{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted);font-weight:600}
tr:last-child td{border-bottom:none}
.tag{display:inline-block;padding:2px 7px;font-size:11px;border-radius:10px;
     background:var(--accent-soft);color:var(--accent)}
.tag.admin{background:color-mix(in srgb,var(--warning) 25%,transparent);color:var(--warning)}
.tag.banned{background:color-mix(in srgb,var(--danger) 20%,transparent);color:var(--danger)}
.tag.online{background:color-mix(in srgb,var(--success) 22%,transparent);color:var(--success)}
td form{display:inline}
.flash{background:color-mix(in srgb,var(--success) 15%,transparent);
     color:var(--success);border:1px solid color-mix(in srgb,var(--success) 40%,transparent);
     padding:9px 12px;border-radius:var(--radius);margin-bottom:14px;font-size:13px}
.flash.err{background:color-mix(in srgb,var(--danger) 12%,transparent);
     color:var(--danger);border-color:color-mix(in srgb,var(--danger) 40%,transparent)}
details.import-box{margin-top:12px}
details.import-box summary{cursor:pointer;color:var(--accent);font-size:13px;padding:4px 0}
details.import-box[open] summary{margin-bottom:10px}

/* PROFILE */
.profile-wrap{max-width:820px;margin:0 auto;padding:26px 20px}
.profile-head{display:flex;align-items:center;gap:18px;margin-bottom:22px;flex-wrap:wrap}
.profile-avatar{width:74px;height:74px;border-radius:50%;background:var(--accent);color:#fff;
     display:flex;align-items:center;justify-content:center;font-weight:600;font-size:30px;flex:0 0 74px}
.profile-info{flex:1;min-width:220px}
.profile-name{font-size:22px;font-weight:600;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.profile-sub{color:var(--text-muted);font-size:13px;margin-top:4px}
.rep-box{display:flex;flex-direction:column;align-items:center;gap:6px;padding:12px 18px;
     background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);min-width:130px}
.rep-value{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.rep-value.pos{color:var(--success)}
.rep-value.neg{color:var(--danger)}
.rep-label{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted)}
.rep-buttons{display:flex;gap:6px}
.rep-buttons form{display:inline}
.stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
     padding:14px 16px;box-shadow:var(--shadow)}
.stat-val{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.stat-lbl{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted);margin-top:3px}
.chart-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
     padding:16px;box-shadow:var(--shadow);margin-bottom:20px}
.chart-box h3{font-size:14px;font-weight:600;margin-bottom:10px}
.chan-list{display:flex;flex-wrap:wrap;gap:6px}
.chan-pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;background:var(--surface-3);
     border-radius:12px;font-size:12px;color:var(--text-dim)}
.chan-pill b{color:var(--accent)}
"""


# ────────────────────────────────────────────────────────────
#  РЕНДЕР СТРАНИЦ
# ────────────────────────────────────────────────────────────

THEME_HEAD_SCRIPT = """
<script>
(function(){
  try {
    var t = localStorage.getItem('sldchat-theme') || 'light';
    document.documentElement.setAttribute('data-theme', t);
  } catch(e){}
})();
</script>
"""

THEME_MENU_HTML = """
<div class="theme-menu" id="theme-menu">
  <button type="button" data-theme-btn="light"><span class="swatch" style="background:#3a7bd5"></span>Светлая</button>
  <button type="button" data-theme-btn="dark"><span class="swatch" style="background:#1c1f27"></span>Тёмная</button>
  <button type="button" data-theme-btn="solar"><span class="swatch" style="background:#b58900"></span>Solar</button>
  <button type="button" data-theme-btn="ocean"><span class="swatch" style="background:#1e88e5"></span>Ocean</button>
  <button type="button" data-theme-btn="forest"><span class="swatch" style="background:#2e9e5b"></span>Forest</button>
</div>
"""

THEME_SCRIPT = """
<script>
(function(){
  var cur = localStorage.getItem('sldchat-theme') || 'light';
  function setTheme(t){
    cur = t;
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('sldchat-theme', t); } catch(e){}
    document.querySelectorAll('[data-theme-btn]').forEach(function(b){
      b.classList.toggle('active', b.dataset.themeBtn === t);
    });
  }
  var toggleBtn = document.getElementById('theme-toggle');
  var menu = document.getElementById('theme-menu');
  if (toggleBtn && menu) {
    toggleBtn.addEventListener('click', function(e){
      e.stopPropagation();
      menu.classList.toggle('open');
    });
    document.addEventListener('click', function(){ menu.classList.remove('open'); });
    menu.addEventListener('click', function(e){ e.stopPropagation(); });
  }
  document.querySelectorAll('[data-theme-btn]').forEach(function(b){
    b.addEventListener('click', function(){
      setTheme(b.dataset.themeBtn);
      menu.classList.remove('open');
    });
    b.classList.toggle('active', b.dataset.themeBtn === cur);
  });
  setTheme(cur);
})();
</script>
"""


def page(title: str, body: str, with_theme: bool = True) -> HTMLResponse:
    head_theme = THEME_HEAD_SCRIPT if with_theme else ""
    body_theme = THEME_SCRIPT if with_theme else ""
    doc = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
{head_theme}
<style>{CSS}</style></head>
<body>{body}{body_theme}</body></html>"""
    return HTMLResponse(doc)


def theme_picker_btn() -> str:
    return (f'<div style="position:relative;display:inline-flex">'
            f'<button type="button" id="theme-toggle" title="Тема" '
            f'style="background:none;border:none;color:inherit;padding:5px;cursor:pointer">'
            f'{ICON_PALETTE}</button>'
            f'{THEME_MENU_HTML}'
            f'</div>')


def render_login(err: str = "") -> HTMLResponse:
    err_html = f'<div class="auth-err">{html.escape(err)}</div>' if err else ""
    body = f"""
<div class="auth-top">
  <button type="button" id="theme-toggle" style="background:var(--surface);border:1px solid var(--border);
    border-radius:var(--radius);padding:7px;color:var(--text);cursor:pointer">{ICON_PALETTE}</button>
  {THEME_MENU_HTML}
</div>
<div class="auth-wrap"><div class="auth-box">
  <div class="auth-logo">{ICON_CHAT}<span style="font-size:20px;font-weight:600;color:var(--text)">sldchat</span></div>
  <div class="auth-sub">{html.escape(CONFIG['server_name'])}</div>
  {err_html}
  <form method="post" action="/login">
    <input name="username" placeholder="Логин" required autofocus>
    <input name="password" type="password" placeholder="Пароль" required>
    <button class="btn" type="submit">Войти</button>
  </form>
  <div class="auth-foot">Нет аккаунта? <a href="/register">Зарегистрироваться</a></div>
</div></div>"""
    return page("Вход — sldchat", body)


def render_register(err: str = "") -> HTMLResponse:
    err_html = f'<div class="auth-err">{html.escape(err)}</div>' if err else ""
    if not CONFIG["allow_registration"]:
        body = f"""
<div class="auth-wrap"><div class="auth-box">
  <div class="auth-logo">{ICON_CHAT}</div>
  <div class="auth-title">Регистрация закрыта</div>
  <div class="auth-sub">Администратор отключил регистрацию.</div>
  <div class="auth-foot"><a href="/login">← Вернуться ко входу</a></div>
</div></div>"""
        return page("Регистрация — sldchat", body)
    body = f"""
<div class="auth-top">
  <button type="button" id="theme-toggle" style="background:var(--surface);border:1px solid var(--border);
    border-radius:var(--radius);padding:7px;color:var(--text);cursor:pointer">{ICON_PALETTE}</button>
  {THEME_MENU_HTML}
</div>
<div class="auth-wrap"><div class="auth-box">
  <div class="auth-logo">{ICON_CHAT}<span style="font-size:20px;font-weight:600;color:var(--text)">sldchat</span></div>
  <div class="auth-sub">Создание аккаунта</div>
  {err_html}
  <form method="post" action="/register">
    <input name="username" placeholder="Логин" required autofocus
           pattern="[A-Za-z0-9_\\-]{{3,20}}" title="3-20 символов: буквы, цифры, _ и -">
    <input name="password" type="password" placeholder="Пароль" required minlength="3">
    <button class="btn" type="submit">Зарегистрироваться</button>
  </form>
  <div class="auth-foot">Уже есть аккаунт? <a href="/login">Войти</a></div>
</div></div>"""
    return page("Регистрация — sldchat", body)


def render_chat(username: str, channel: str) -> HTMLResponse:
    user = USERS[username]
    initial = html.escape(username[0].upper())
    role = "admin" if user["is_admin"] else "участник"
    admin_btn = (f'<a href="/admin" title="Админ-панель">{ICON_SETTINGS}</a>'
                 if user["is_admin"] else "")

    channels_payload_json = json.dumps(
        {n: {"topic": c["topic"]} for n, c in CHANNELS.items()},
        ensure_ascii=False
    )

    body = f"""
<div class="app">
  <aside class="sidebar">
    <div class="brand">{ICON_CHAT}<span>{html.escape(CONFIG['server_name'])}</span></div>
    <div class="section-title">Каналы</div>
    <ul class="channels" id="channels"></ul>
    <div class="user-box">
      <div class="avatar">{initial}</div>
      <div class="user-meta">
        <a class="user-name" href="/profile">{html.escape(username)}</a>
        <div class="user-role">{role}</div>
      </div>
      <div class="user-actions">
        {admin_btn}
        {theme_picker_btn()}
        <a href="/logout" title="Выйти">{ICON_LOGOUT}</a>
      </div>
    </div>
  </aside>

  <main class="chat">
    <header class="chat-head">
      <span class="hash">#</span>
      <h2 id="chan-title">{html.escape(channel)}</h2>
      <span class="topic" id="chan-topic">{html.escape(CHANNELS[channel]['topic'])}</span>
      <span class="user-count">{ICON_USERS}<span id="user-count">0</span></span>
    </header>
    <div class="messages" id="messages"></div>
    <form class="composer" id="composer" autocomplete="off">
      <input id="msg-input" placeholder="Написать сообщение..."
             maxlength="{CONFIG['max_msg_len']}" autofocus>
      <button type="submit" title="Отправить">{ICON_SEND}</button>
    </form>
  </main>

  <aside class="users-panel">
    <div class="section-title">Участники</div>
    <ul id="user-list"></ul>
  </aside>
</div>

<script>
(function(){{
  var ME = {username!r};
  var INITIAL_CHANNEL = {channel!r};
  var CHANNELS = {channels_payload_json};
  var MAX_LEN = {CONFIG['max_msg_len']};

  var state = {{ channel: INITIAL_CHANNEL, users: [], ws: null, retry: 0 }};

  var $messages = document.getElementById('messages');
  var $channels = document.getElementById('channels');
  var $userList = document.getElementById('user-list');
  var $userCnt  = document.getElementById('user-count');
  var $title    = document.getElementById('chan-title');
  var $topic    = document.getElementById('chan-topic');
  var $input    = document.getElementById('msg-input');
  var $form     = document.getElementById('composer');

  function esc(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
      return ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c];
    }});
  }}

  function renderChannels(){{
    var names = Object.keys(CHANNELS).sort();
    $channels.innerHTML = names.map(function(n){{
      var act = n === state.channel ? ' active' : '';
      return '<li><a href="/?c=' + encodeURIComponent(n) + '" data-channel="' + esc(n) + '" class="ch' + act + '">'
        + '<span class="hash">#</span>' + esc(n) + '</a></li>';
    }}).join('');
  }}

  function applyTopic(){{
    var info = CHANNELS[state.channel];
    $topic.textContent = info ? info.topic : '';
  }}

  function setUsers(list){{
    state.users = list;
    $userCnt.textContent = list.length;
    $userList.innerHTML = list.map(function(u){{
      var rep = u.rep || 0;
      var cls = rep > 0 ? 'pos' : (rep < 0 ? 'neg' : '');
      var repTxt = rep > 0 ? '+' + rep : (rep < 0 ? String(rep) : '0');
      return '<li><span class="u-name"><a href="/profile/' + encodeURIComponent(u.name) + '">'
        + esc(u.name) + '</a></span><span class="u-rep ' + cls + '">' + repTxt + '</span></li>';
    }}).join('');
  }}

  function addMessage(m){{
    var d = document.createElement('div');
    d.className = 'msg';
    d.innerHTML = '<a class="msg-user" href="/profile/' + encodeURIComponent(m.user) + '">'
      + esc(m.user) + '</a><span class="msg-time">' + esc(m.time) + '</span>'
      + '<div class="msg-text">' + esc(m.text) + '</div>';
    $messages.appendChild(d);
    $messages.scrollTop = $messages.scrollHeight;
  }}

  function addSystem(t){{
    var d = document.createElement('div');
    d.className = 'system-msg';
    d.textContent = t;
    $messages.appendChild(d);
    $messages.scrollTop = $messages.scrollHeight;
  }}

  function clearMessages(){{ $messages.innerHTML = ''; }}

  function setChannel(name, push){{
    if (name === state.channel) return;
    if (!CHANNELS[name]) return;
    state.channel = name;
    if (push !== false) {{
      history.pushState({{}}, '', '/?c=' + encodeURIComponent(name));
    }}
    $title.textContent = name;
    applyTopic();
    renderChannels();
    clearMessages();
    setUsers([]);
    if (state.ws && state.ws.readyState === 1) {{
      state.ws.send(JSON.stringify({{ type: 'join', channel: name }}));
    }}
  }}

  document.addEventListener('click', function(e){{
    var a = e.target.closest('a[data-channel]');
    if (!a) return;
    e.preventDefault();
    setChannel(a.dataset.channel);
  }});

  window.addEventListener('popstate', function(){{
    var p = new URLSearchParams(location.search);
    var c = p.get('c');
    if (c && CHANNELS[c]) setChannel(c, false);
  }});

  function connect(){{
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    state.ws = new WebSocket(proto + location.host + '/ws');

    state.ws.onopen = function(){{
      state.retry = 0;
      state.ws.send(JSON.stringify({{ type: 'join', channel: state.channel }}));
    }};

    state.ws.onmessage = function(e){{
      var d;
      try {{ d = JSON.parse(e.data); }} catch(_) {{ return; }}

      if (d.type === 'hello') {{
        CHANNELS = d.channels || CHANNELS;
        renderChannels();
        applyTopic();
      }} else if (d.type === 'channels') {{
        CHANNELS = d.channels || {{}};
        renderChannels();
        applyTopic();
        if (!CHANNELS[state.channel]) {{
          var first = Object.keys(CHANNELS).sort()[0];
          if (first) {{
            state.channel = first;
            $title.textContent = first;
            history.replaceState({{}}, '', '/?c=' + encodeURIComponent(first));
            if (state.ws.readyState === 1) {{
              state.ws.send(JSON.stringify({{ type: 'join', channel: first }}));
            }}
          }}
        }}
      }} else if (d.type === 'history') {{
        if (d.channel !== state.channel) return;
        clearMessages();
        (d.messages || []).forEach(addMessage);
      }} else if (d.type === 'message') {{
        if (d.channel !== state.channel) return;
        addMessage(d);
      }} else if (d.type === 'system') {{
        if (d.channel && d.channel !== state.channel) return;
        addSystem(d.text);
      }} else if (d.type === 'users') {{
        if (d.channel !== state.channel) return;
        setUsers(d.users || []);
      }} else if (d.type === 'rep') {{
        state.users.forEach(function(u){{ if (u.name === d.user) u.rep = d.rep; }});
        setUsers(state.users);
      }} else if (d.type === 'kicked') {{
        alert(d.reason || 'Вас выгнали с сервера');
        location.href = '/login';
      }} else if (d.type === 'ban') {{
        alert('Ваш аккаунт заблокирован');
        location.href = '/login';
      }}
    }};

    state.ws.onclose = function(){{
      addSystem('Соединение потеряно, переподключение...');
      var delay = Math.min(5000, 800 * Math.pow(1.6, state.retry++));
      setTimeout(connect, delay);
    }};
  }}

  $form.addEventListener('submit', function(e){{
    e.preventDefault();
    var text = $input.value.trim().slice(0, MAX_LEN);
    if (!text || !state.ws || state.ws.readyState !== 1) return;
    state.ws.send(JSON.stringify({{ type: 'message', text: text }}));
    $input.value = '';
  }});

  renderChannels();
  applyTopic();
  connect();
}})();
</script>"""
    return page(f"#{channel} — sldchat", body)


def render_chart(by_day: Dict[str, int], days: int = 14, width: int = 760, height: int = 220) -> str:
    today = date.today()
    labels, values = [], []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        labels.append(d.strftime("%d.%m"))
        values.append(by_day.get(d.isoformat(), 0))
    max_v = max(values + [1])
    pad_l, pad_r, pad_t, pad_b = 32, 8, 20, 34
    cw = width - pad_l - pad_r
    ch = height - pad_t - pad_b
    step = cw / days
    bar_w = step * 0.62
    parts = []
    # горизонтальные линии
    for i in range(5):
        y = pad_t + ch - (ch * i / 4)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l+cw}" y2="{y:.1f}" '
                     f'stroke="var(--border)" stroke-dasharray="2 3" opacity="0.6"/>')
        val = round(max_v * i / 4)
        parts.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" text-anchor="end" '
                     f'font-size="9" fill="var(--text-muted)">{val}</text>')
    # столбцы
    for i, v in enumerate(values):
        bh = (v / max_v) * ch if max_v else 0
        x = pad_l + i * step + (step - bar_w) / 2
        y = pad_t + ch - bh
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
                     f'rx="2" fill="var(--accent)" opacity="0.85"/>')
        if v > 0:
            parts.append(f'<text x="{x+bar_w/2:.1f}" y="{y-4:.1f}" text-anchor="middle" '
                         f'font-size="10" fill="var(--text-dim)">{v}</text>')
        # подпись через один день, чтобы не теснилось
        if i % 2 == 0 or i == days - 1:
            parts.append(f'<text x="{x+bar_w/2:.1f}" y="{height-12}" text-anchor="middle" '
                         f'font-size="9" fill="var(--text-muted)">{labels[i]}</text>')
    return f'<svg viewBox="0 0 {width} {height}" style="width:100%;height:auto">{"".join(parts)}</svg>'


def render_profile(target: str, viewer: str) -> HTMLResponse:
    u = USERS[target]
    stats = u.get("stats", {})
    by_day = stats.get("by_day", {})
    by_channel = stats.get("by_channel", {})
    total = stats.get("messages_total", 0)
    rep = u["rep"]

    joined = datetime.fromtimestamp(u["joined"]).strftime("%d.%m.%Y")
    initial = html.escape(target[0].upper())

    rep_cls = "pos" if rep > 0 else ("neg" if rep < 0 else "")
    rep_txt = ("+" + str(rep)) if rep > 0 else str(rep)

    rep_buttons = ""
    if target != viewer and not u["banned"]:
        rep_buttons = f"""
<div class="rep-buttons">
  <form method="post" action="/rep">
    <input type="hidden" name="target" value="{html.escape(target)}">
    <input type="hidden" name="delta" value="1">
    <button class="btn secondary sm" type="submit" title="Плюс">{ICON_THUMB_UP} +1</button>
  </form>
  <form method="post" action="/rep">
    <input type="hidden" name="target" value="{html.escape(target)}">
    <input type="hidden" name="delta" value="-1">
    <button class="btn secondary sm" type="submit" title="Минус">{ICON_THUMB_DOWN} −1</button>
  </form>
</div>"""

    # список каналов где писал
    chans = sorted(by_channel.items(), key=lambda kv: -kv[1])
    chan_html = ""
    if chans:
        chan_html = '<div class="chan-list">' + "".join(
            f'<span class="chan-pill">#{html.escape(c)} <b>{n}</b></span>'
            for c, n in chans
        ) + "</div>"
    else:
        chan_html = '<div class="muted small">Пока ничего не написал</div>'

    chart_svg = render_chart(by_day, days=14)

    tags = ""
    if u["is_admin"]: tags += ' <span class="tag admin">admin</span>'
    if u["banned"]: tags += ' <span class="tag banned">забанен</span>'
    if target in all_online(): tags += ' <span class="tag online">online</span>'

    body = f"""
<div class="profile-wrap">
  <div class="row" style="margin-bottom:18px;justify-content:space-between">
    <a class="btn secondary" href="/">← В чат</a>
    {theme_picker_btn()}
  </div>

  <div class="profile-head">
    <div class="profile-avatar">{initial}</div>
    <div class="profile-info">
      <div class="profile-name">{html.escape(target)}{tags}</div>
      <div class="profile-sub">На сервере с {joined}</div>
      <div class="profile-sub">Всего сообщений: <b style="color:var(--text)">{total}</b></div>
    </div>
    <div class="rep-box">
      <div class="rep-label">Репутация</div>
      <div class="rep-value {rep_cls}">{rep_txt}</div>
      {rep_buttons}
    </div>
  </div>

  <div class="stat-row">
    <div class="stat"><div class="stat-val">{total}</div><div class="stat-lbl">Всего сообщений</div></div>
    <div class="stat"><div class="stat-val">{len(by_channel)}</div><div class="stat-lbl">Каналов</div></div>
    <div class="stat"><div class="stat-val">{by_day.get(date.today().isoformat(), 0)}</div><div class="stat-lbl">Сегодня</div></div>
    <div class="stat"><div class="stat-val">{rep_txt}</div><div class="stat-lbl">Репутация</div></div>
  </div>

  <div class="chart-box">
    <h3>Активность (14 дней)</h3>
    {chart_svg}
  </div>

  <div class="chart-box">
    <h3>Активность по каналам</h3>
    {chan_html}
  </div>
</div>"""
    return page(f"{target} — профиль", body)


def render_admin(username: str, tab: str = "channels",
                 flash: str = "", flash_err: bool = False) -> HTMLResponse:
    tabs = [("channels", "Каналы"), ("users", "Пользователи"), ("settings", "Настройки")]
    tabs_html = "".join(
        f'<a class="{"active" if k == tab else ""}" href="/admin?tab={k}">{lbl}</a>'
        for k, lbl in tabs
    )

    flash_html = ""
    if flash:
        cls = "flash err" if flash_err else "flash"
        flash_html = f'<div class="{cls}">{html.escape(flash)}</div>'

    content = ""
    if tab == "channels":
        rows = ""
        external_forms = ""
        for cname, cinfo in CHANNELS.items():
            cnt = len(MESSAGES.get(cname, []))
            online = len({c["user"] for c in WS_CLIENTS.values() if c.get("channel") == cname})
            rows += f"""
<tr>
  <td><b>#{html.escape(cname)}</b></td>
  <td><input name="topic_{html.escape(cname)}" value="{html.escape(cinfo['topic'])}" maxlength="120"></td>
  <td class="muted small">{cnt} сообщ.<br>{online} online</td>
  <td style="white-space:nowrap">
    <button form="clear-{html.escape(cname)}" class="btn secondary sm" type="submit">Очистить</button>
    <button form="del-{html.escape(cname)}" class="btn danger sm" type="submit" title="Удалить">{ICON_TRASH}</button>
  </td>
</tr>"""
            external_forms += (
                f'<form id="clear-{html.escape(cname)}" method="post" action="/admin/channels/clear" '
                f'style="display:none"><input type="hidden" name="name" value="{html.escape(cname)}"></form>'
                f'<form id="del-{html.escape(cname)}" method="post" action="/admin/channels/delete" '
                f'onsubmit="return confirm(\'Удалить #{html.escape(cname)}?\')" style="display:none">'
                f'<input type="hidden" name="name" value="{html.escape(cname)}"></form>'
            )

        export_json = json.dumps({
            "version": 1,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "channels": [{"name": n, "topic": c["topic"]} for n, c in CHANNELS.items()],
        }, ensure_ascii=False, indent=2)

        content = f"""
<div class="card">
  <h3>{ICON_PLUS} Создать канал</h3>
  <form method="post" action="/admin/channels/create" class="row">
    <input name="name" placeholder="название (a-z, 0-9, -)" required
           pattern="[a-z0-9_\\-]{{2,30}}" style="max-width:280px">
    <input name="topic" placeholder="описание" maxlength="120">
    <button class="btn" type="submit">Создать</button>
  </form>
</div>

<div class="card">
  <h3>{ICON_USERS} Существующие каналы ({len(CHANNELS)})</h3>
  <form method="post" action="/admin/channels/save">
    <table>
      <thead><tr><th>Канал</th><th>Описание</th><th>Статистика</th><th></th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <div style="margin-top:14px">
      <button class="btn" type="submit">Сохранить все описания</button>
    </div>
  </form>
  {external_forms}
  <details class="import-box">
    <summary>{ICON_DOWNLOAD} Экспорт / импорт списка каналов</summary>
    <div class="row" style="margin-bottom:10px">
      <a class="btn secondary" href="/admin/channels/export" download>Скачать JSON</a>
      <button type="button" class="btn secondary" onclick="copyExport()">Скопировать</button>
    </div>
    <textarea id="export-json" rows="8" readonly>{html.escape(export_json)}</textarea>
    <form method="post" action="/admin/channels/import" style="margin-top:10px">
      <label class="fld"><span>Вставить JSON для импорта (существующие каналы обновятся по имени):</span></label>
      <textarea name="payload" rows="6" placeholder='{{"channels":[{{"name":"general","topic":"..."}}]}}' required></textarea>
      <div style="margin-top:10px">
        <button class="btn" type="submit">{ICON_UPLOAD} Импортировать</button>
      </div>
    </form>
  </details>
</div>
<script>
function copyExport(){{
  var t = document.getElementById('export-json');
  t.select(); document.execCommand('copy');
}}
</script>"""

    elif tab == "users":
        online = set(all_online())

        # онлайн-пользователи с кнопкой кик
        online_rows = ""
        for uname in sorted(online):
            chans = sorted({c["channel"] for c in WS_CLIENTS.values()
                            if c.get("user") == uname and c.get("channel")})
            chans_txt = ", ".join("#" + c for c in chans) or "—"
            kick_btn = ""
            if uname != username:
                kick_btn = (f'<form method="post" action="/admin/users/kick" '
                            f'onsubmit="return confirm(\'Выгнать {html.escape(uname)}?\')">'
                            f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                            f'<button class="btn danger sm" type="submit">{ICON_KICK} Выгнать</button></form>')
            online_rows += f"""<tr>
  <td><b>{html.escape(uname)}</b> <span class="tag online">online</span></td>
  <td class="muted small">{html.escape(chans_txt)}</td>
  <td>{kick_btn}</td>
</tr>"""
        if not online_rows:
            online_rows = '<tr><td colspan="3" class="muted small">Никого нет онлайн</td></tr>'

        # все пользователи
        rows = ""
        for uname, udata in sorted(USERS.items(), key=lambda kv: kv[0].lower()):
            tags = ""
            if udata["is_admin"]: tags += ' <span class="tag admin">admin</span>'
            if udata.get("banned"): tags += ' <span class="tag banned">бан</span>'
            if uname in online: tags += ' <span class="tag online">online</span>'
            joined = datetime.fromtimestamp(udata["joined"]).strftime("%d.%m.%Y")
            rep = udata["rep"]

            buttons = []
            if uname != "admin":
                if udata["is_admin"]:
                    buttons.append(
                        f'<form method="post" action="/admin/users/demote">'
                        f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                        f'<button class="btn secondary sm" type="submit">−админ</button></form>'
                    )
                else:
                    buttons.append(
                        f'<form method="post" action="/admin/users/promote">'
                        f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                        f'<button class="btn secondary sm" type="submit">+админ</button></form>'
                    )
                if udata.get("banned"):
                    buttons.append(
                        f'<form method="post" action="/admin/users/unban">'
                        f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                        f'<button class="btn secondary sm" type="submit">Разбан</button></form>'
                    )
                else:
                    buttons.append(
                        f'<form method="post" action="/admin/users/ban" '
                        f'onsubmit="return confirm(\'Забанить {html.escape(uname)}?\')">'
                        f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                        f'<button class="btn danger sm" type="submit">Бан</button></form>'
                    )
                buttons.append(
                    f'<form method="post" action="/admin/users/delete" '
                    f'onsubmit="return confirm(\'Удалить {html.escape(uname)} навсегда?\')">'
                    f'<input type="hidden" name="username" value="{html.escape(uname)}">'
                    f'<button class="btn danger sm" type="submit" title="Удалить">{ICON_TRASH}</button></form>'
                )
            rows += f"""<tr>
  <td><a href="/profile/{html.escape(uname)}"><b>{html.escape(uname)}</b></a>{tags}</td>
  <td class="muted small">{joined}</td>
  <td style="font-variant-numeric:tabular-nums">{rep:+d}</td>
  <td style="white-space:nowrap">{''.join(buttons)}</td>
</tr>"""

        content = f"""
<div class="card">
  <h3>{ICON_USERS} Онлайн ({len(online)})</h3>
  <table><thead><tr><th>Пользователь</th><th>Каналы</th><th></th></tr></thead>
  <tbody>{online_rows}</tbody></table>
</div>
<div class="card">
  <h3>Все пользователи ({len(USERS)})</h3>
  <table>
    <thead><tr><th>Пользователь</th><th>Регистрация</th><th>Репутация</th><th>Действия</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""

    else:  # settings
        content = f"""
<div class="card">
  <h3>{ICON_SETTINGS} Общие настройки сервера</h3>
  <form method="post" action="/admin/settings">
    <label class="fld"><span>Название сервера</span>
      <input name="server_name" value="{html.escape(CONFIG['server_name'])}" maxlength="60" required>
    </label>
    <label class="fld"><span>Приветствие (MOTD)</span>
      <input name="motd" value="{html.escape(CONFIG['motd'])}" maxlength="200">
    </label>
    <div class="grid2">
      <label class="fld"><span>Максимум символов в сообщении</span>
        <input type="number" name="max_msg_len" min="50" max="5000"
               value="{CONFIG['max_msg_len']}" required>
      </label>
      <label class="fld"><span>Регистрация новых пользователей</span>
        <select name="allow_registration">
          <option value="1" {"selected" if CONFIG['allow_registration'] else ""}>Разрешена</option>
          <option value="0" {"" if CONFIG['allow_registration'] else "selected"}>Запрещена</option>
        </select>
      </label>
    </div>
    <button class="btn" type="submit">Сохранить настройки</button>
  </form>
</div>

<div class="card">
  <h3>{ICON_USER} Аккаунт администратора</h3>
  <form method="post" action="/admin/account">
    <div class="grid2">
      <label class="fld"><span>Новый логин (пусто — не менять)</span>
        <input name="new_username" placeholder="{html.escape(username)}" maxlength="20"
               pattern="[A-Za-z0-9_\\-]{{3,20}}">
      </label>
      <label class="fld"><span>Текущий пароль (обязательно)</span>
        <input name="current_password" type="password" required>
      </label>
      <label class="fld"><span>Новый пароль (пусто — не менять)</span>
        <input name="new_password" type="password" minlength="3" maxlength="128">
      </label>
      <label class="fld"><span>Повторите новый пароль</span>
        <input name="new_password2" type="password" minlength="3" maxlength="128">
      </label>
    </div>
    <button class="btn" type="submit">Применить</button>
  </form>
</div>"""

    body = f"""
<div class="admin-wrap">
  <div class="admin-head">
    <h1>{ICON_SETTINGS} Админ-панель — {html.escape(CONFIG['server_name'])}</h1>
    <div class="row">
      {theme_picker_btn()}
      <a class="btn secondary" href="/">← К чату</a>
    </div>
  </div>
  <div class="tabs">{tabs_html}</div>
  {flash_html}
  {content}
</div>"""
    return page("Админ — sldchat", body)


# ────────────────────────────────────────────────────────────
#  AUTH
# ────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page(request: Request):
    if get_session(request):
        return redirect("/")
    return render_login()


@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    if not check_origin(request) or rate_limited(ip):
        return render_login("Слишком много попыток. Подожди минуту.")
    uname = username.strip()
    user = USERS.get(uname)
    if not user or not verify_pw(password, user["password"]):
        return render_login("Неверный логин или пароль")
    if user.get("banned"):
        return render_login("Аккаунт заблокирован")
    token = new_session(uname)
    resp = redirect("/")
    resp.set_cookie("session", token, httponly=True, max_age=SESSION_TTL, samesite="lax")
    return resp


@app.get("/register")
async def register_page(request: Request):
    if get_session(request):
        return redirect("/")
    return render_register()


@app.post("/register")
async def register_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if not CONFIG["allow_registration"]:
        return render_register("Регистрация закрыта")
    if not check_origin(request):
        return render_register("Некорректный запрос")
    uname = username.strip()
    if not NAME_RE.match(uname):
        return render_register("Некорректный логин (3-20: буквы, цифры, _ -)")
    if uname.lower() == "admin" or uname in USERS:
        return render_register("Такой логин уже занят")
    if len(password) < 3 or len(password) > 128:
        return render_register("Пароль слишком короткий или длинный")
    USERS[uname] = make_user(password)
    token = new_session(uname)
    resp = redirect("/")
    resp.set_cookie("session", token, httponly=True, max_age=SESSION_TTL, samesite="lax")
    return resp


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        SESSIONS.pop(token, None)
    resp = redirect("/login")
    resp.delete_cookie("session")
    return resp


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# ────────────────────────────────────────────────────────────
#  ЧАТ
# ────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    user = get_session(request)
    if not user or user not in USERS or USERS[user].get("banned"):
        return redirect("/login")
    ch = request.query_params.get("c")
    if not ch or ch not in CHANNELS:
        ch = next(iter(sorted(CHANNELS)), None)
    if not ch:
        add_channel("general", "Общий чат")
        ch = "general"
    return render_chat(user, ch)


@app.websocket("/ws")
async def ws_route(ws: WebSocket):
    if not check_origin(ws):
        await ws.close(code=1008); return
    username = get_session(ws)
    if not username or username not in USERS or USERS[username].get("banned"):
        await ws.close(code=1008); return
    if KICKS.get(username, 0) > time.time():
        await ws.close(code=1008); return

    await ws.accept()
    WS_CLIENTS[ws] = {"user": username, "channel": None}

    await send_ws(ws, {
        "type": "hello",
        "username": username,
        "channels": {n: {"topic": c["topic"]} for n, c in CHANNELS.items()},
        "is_admin": USERS[username]["is_admin"],
        "rep": USERS[username]["rep"],
        "config": CONFIG,
    })

    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > 8000:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            t = data.get("type")
            if t == "join":
                await handle_join(ws, username, str(data.get("channel") or ""))
            elif t == "message":
                await handle_message(username, str(data.get("text") or ""))
            elif t == "rep":
                await handle_rep(username, str(data.get("target") or ""),
                                 int(data.get("delta") or 0))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        info = WS_CLIENTS.pop(ws, None)
        if info and info.get("channel"):
            ch = info["channel"]
            await broadcast_channel(ch, {"type": "system", "channel": ch,
                                         "text": f"{username} покинул #{ch}"})
            await broadcast_user_list(ch)


async def handle_join(ws: WebSocket, username: str, channel: str):
    if channel not in CHANNELS:
        return
    info = WS_CLIENTS.get(ws)
    if not info:
        return
    old = info.get("channel")
    if old == channel:
        return
    if old:
        info["channel"] = None
        await broadcast_channel(old, {"type": "system", "channel": old,
                                      "text": f"{username} покинул #{old}"})
        await broadcast_user_list(old)
    info["channel"] = channel
    history = MESSAGES.get(channel, [])[-100:]
    await send_ws(ws, {"type": "history", "channel": channel, "messages": history})
    await broadcast_channel(channel, {"type": "system", "channel": channel,
                                      "text": f"{username} присоединился к #{channel}"})
    await broadcast_user_list(channel)
    if CONFIG["motd"]:
        await send_ws(ws, {"type": "system", "channel": channel, "text": CONFIG["motd"]})


async def handle_message(username: str, text: str):
    info = next((c for c in WS_CLIENTS.values() if c.get("user") == username), None)
    if not info or not info.get("channel"):
        return
    channel = info["channel"]
    text = text.strip()[:CONFIG["max_msg_len"]]
    if not text:
        return
    msg = {"user": username, "text": text, "time": datetime.now().strftime("%H:%M")}
    MESSAGES.setdefault(channel, []).append(msg)
    if len(MESSAGES[channel]) > 500:
        MESSAGES[channel] = MESSAGES[channel][-500:]
    record_message(username, channel)
    await broadcast_channel(channel, {"type": "message", "channel": channel, **msg})


async def handle_rep(giver: str, target: str, delta: int):
    if delta not in (1, -1):
        return
    if giver == target or giver not in USERS or target not in USERS:
        return
    g = USERS[giver]
    last = g.setdefault("rep_given_to", {}).get(target, 0)
    if time.time() - last < REP_COOLDOWN:
        return
    g["rep_given_to"][target] = time.time()
    USERS[target]["rep"] += delta
    await broadcast_all({"type": "rep", "user": target, "rep": USERS[target]["rep"]})


# ────────────────────────────────────────────────────────────
#  ПРОФИЛЬ
# ────────────────────────────────────────────────────────────

@app.get("/profile")
async def profile_self(request: Request):
    user = get_session(request)
    if not user:
        return redirect("/login")
    return render_profile(user, user)


@app.get("/profile/{username}")
async def profile_user(request: Request, username: str):
    viewer = get_session(request)
    if not viewer:
        return redirect("/login")
    if username not in USERS:
        return redirect("/")
    return render_profile(username, viewer)


@app.post("/rep")
async def rep_post(request: Request, target: str = Form(...), delta: int = Form(...)):
    giver = get_session(request)
    if not giver or not check_origin(request):
        return redirect("/login")
    await handle_rep(giver, target, delta)
    return redirect(f"/profile/{target}")


# ────────────────────────────────────────────────────────────
#  АДМИН
# ────────────────────────────────────────────────────────────

def require_admin(request: Request) -> Optional[str]:
    user = get_session(request)
    if not user or user not in USERS or not USERS[user]["is_admin"]:
        return None
    if not check_origin(request):
        return None
    return user


@app.get("/admin")
async def admin_page(request: Request, tab: str = "channels",
                     ok: str = "", err: str = ""):
    admin = require_admin(request)
    if not admin:
        return redirect("/login")
    if tab not in ("channels", "users", "settings"):
        tab = "channels"
    flash, flash_err = "", False
    if ok == "1":
        flash = "Сохранено."
    elif ok == "import":
        flash = "Каналы импортированы."
    elif err == "pass":
        flash, flash_err = "Неверный текущий пароль.", True
    elif err == "name":
        flash, flash_err = "Логин занят или некорректен.", True
    elif err == "pw2":
        flash, flash_err = "Новые пароли не совпадают.", True
    return render_admin(admin, tab, flash, flash_err)


@app.post("/admin/channels/create")
async def admin_channel_create(request: Request, name: str = Form(...), topic: str = Form("")):
    if not require_admin(request):
        return redirect("/login")
    if add_channel(name, topic.strip()):
        await broadcast_channels()
    return redirect("/admin?tab=channels")


@app.post("/admin/channels/save")
async def admin_channels_save(request: Request):
    if not require_admin(request):
        return redirect("/login")
    form = await request.form()
    for key, value in form.items():
        if isinstance(key, str) and key.startswith("topic_"):
            cname = key[6:]
            if cname in CHANNELS:
                CHANNELS[cname]["topic"] = (str(value).strip()[:120]) or "Без описания"
    await broadcast_channels()
    return redirect("/admin?tab=channels&ok=1")


@app.post("/admin/channels/clear")
async def admin_channel_clear(request: Request, name: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if name in MESSAGES:
        MESSAGES[name] = []
    return redirect("/admin?tab=channels")


@app.post("/admin/channels/delete")
async def admin_channel_delete(request: Request, name: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    CHANNELS.pop(name, None)
    MESSAGES.pop(name, None)
    for ws, c in list(WS_CLIENTS.items()):
        if c.get("channel") == name:
            c["channel"] = None
    await broadcast_channels()
    return redirect("/admin?tab=channels")


@app.get("/admin/channels/export")
async def admin_channels_export(request: Request):
    if not require_admin(request):
        return redirect("/login")
    data = json.dumps({
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "channels": [{"name": n, "topic": c["topic"]} for n, c in CHANNELS.items()],
    }, ensure_ascii=False, indent=2)
    return PlainTextResponse(
        data, media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="sldchat-channels.json"'}
    )


@app.post("/admin/channels/import")
async def admin_channels_import(request: Request, payload: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    try:
        data = json.loads(payload)
    except Exception:
        return redirect("/admin?tab=channels&err=json")
    channels = data.get("channels") if isinstance(data, dict) else data
    if not isinstance(channels, list):
        return redirect("/admin?tab=channels&err=json")
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        name = str(ch.get("name", "")).strip().lower().replace(" ", "-")
        topic = str(ch.get("topic", ""))[:120]
        if not name:
            continue
        if name in CHANNELS:
            CHANNELS[name]["topic"] = topic or "Без описания"
        else:
            add_channel(name, topic)
    await broadcast_channels()
    return redirect("/admin?tab=channels&ok=import")


@app.post("/admin/users/promote")
async def admin_user_promote(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if username in USERS:
        USERS[username]["is_admin"] = True
    return redirect("/admin?tab=users")


@app.post("/admin/users/demote")
async def admin_user_demote(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if username in USERS and username != "admin":
        USERS[username]["is_admin"] = False
    return redirect("/admin?tab=users")


@app.post("/admin/users/ban")
async def admin_user_ban(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if username in USERS and username != "admin":
        USERS[username]["banned"] = True
        for tok, s in list(SESSIONS.items()):
            if s["user"] == username:
                SESSIONS.pop(tok, None)
        for ws, c in list(WS_CLIENTS.items()):
            if c.get("user") == username:
                await send_ws(ws, {"type": "ban"})
                WS_CLIENTS.pop(ws, None)
                try:
                    await ws.close(code=1008)
                except Exception:
                    pass
        for ch in list(CHANNELS.keys()):
            await broadcast_user_list(ch)
    return redirect("/admin?tab=users")


@app.post("/admin/users/unban")
async def admin_user_unban(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if username in USERS:
        USERS[username]["banned"] = False
    return redirect("/admin?tab=users")


@app.post("/admin/users/kick")
async def admin_user_kick(request: Request, username: str = Form(...)):
    admin = require_admin(request)
    if not admin:
        return redirect("/login")
    if username == admin or username not in USERS:
        return redirect("/admin?tab=users")
    KICKS[username] = time.time() + KICK_COOLDOWN
    for ws, c in list(WS_CLIENTS.items()):
        if c.get("user") == username:
            await send_ws(ws, {"type": "kicked",
                               "reason": "Вас выгнал администратор. Попробуйте через минуту."})
            WS_CLIENTS.pop(ws, None)
            try:
                await ws.close(code=1008)
            except Exception:
                pass
    for ch in list(CHANNELS.keys()):
        await broadcast_user_list(ch)
    return redirect("/admin?tab=users")


@app.post("/admin/users/delete")
async def admin_user_delete(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return redirect("/login")
    if username in USERS and username != "admin":
        USERS.pop(username, None)
        for tok, s in list(SESSIONS.items()):
            if s["user"] == username:
                SESSIONS.pop(tok, None)
        for ws, c in list(WS_CLIENTS.items()):
            if c.get("user") == username:
                await send_ws(ws, {"type": "kicked", "reason": "Аккаунт удалён"})
                WS_CLIENTS.pop(ws, None)
                try:
                    await ws.close(code=1001)
                except Exception:
                    pass
        for ch in list(CHANNELS.keys()):
            await broadcast_user_list(ch)
    return redirect("/admin?tab=users")


@app.post("/admin/settings")
async def admin_settings(request: Request, server_name: str = Form(...),
                         motd: str = Form(""), max_msg_len: int = Form(...),
                         allow_registration: str = Form("1")):
    if not require_admin(request):
        return redirect("/login")
    CONFIG["server_name"] = server_name.strip()[:60] or "sldchat"
    CONFIG["motd"] = motd.strip()[:200]
    CONFIG["max_msg_len"] = max(50, min(5000, int(max_msg_len)))
    CONFIG["allow_registration"] = allow_registration == "1"
    await broadcast_all({"type": "config", "config": CONFIG})
    return redirect("/admin?tab=settings&ok=1")


@app.post("/admin/account")
async def admin_account(request: Request,
                        new_username: str = Form(""),
                        current_password: str = Form(...),
                        new_password: str = Form(""),
                        new_password2: str = Form("")):
    admin = require_admin(request)
    if not admin:
        return redirect("/login")
    if not verify_pw(current_password, USERS[admin]["password"]):
        return redirect("/admin?tab=settings&err=pass")
    new_username = new_username.strip()
    if new_username and new_username != admin:
        if not NAME_RE.match(new_username) or new_username in USERS:
            return redirect("/admin?tab=settings&err=name")
        USERS[new_username] = USERS.pop(admin)
        for s in SESSIONS.values():
            if s["user"] == admin:
                s["user"] = new_username
        for c in WS_CLIENTS.values():
            if c.get("user") == admin:
                c["user"] = new_username
        admin = new_username
    if new_password:
        if new_password != new_password2:
            return redirect("/admin?tab=settings&err=pw2")
        if len(new_password) < 3 or len(new_password) > 128:
            return redirect("/admin?tab=settings&err=pw2")
        USERS[admin]["password"] = hash_pw(new_password)
    return redirect("/admin?tab=settings&ok=1")


if __name__ == "__main__":
    print("=" * 52)
    print("  sldchat запущен")
    print("  http://127.0.0.1:8000")
    print("  admin / admin")
    print("=" * 52)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
