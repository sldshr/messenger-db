# main.py
# Мини-соцнежь: FastAPI, всё в оперативной памяти, один файл.
# Запуск:  pip install fastapi uvicorn
#          uvicorn main:app --reload
# Открыть: http://127.0.0.1:8000

import hashlib
import html
import re
import secrets
import uuid
from datetime import datetime
from typing import Optional

from fastapi import Cookie, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

app = FastAPI(title="MiniNet")

# --------------------------------------------------------------------------
# ХРАНИЛИЩЕ
# --------------------------------------------------------------------------
USERS: dict = {}          # username -> {"salt", "hash", "created"}
POSTS: list = []          # [{"id","author","text","ts","likes": set()}]
SESSIONS: dict = {}       # token -> username
NOTIFICATIONS: list = []  # [{"id","user","from","kind","post_id","ts","read"}]

MAX_POST = 500
MENTION_RE = re.compile(r"@([A-Za-z0-9_]{3,20})")


# --------------------------------------------------------------------------
# ХЕЛПЕРЫ
# --------------------------------------------------------------------------
def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def current_user(session: Optional[str]) -> Optional[str]:
    if session and session in SESSIONS:
        return SESSIONS[session]
    return None


def find_user(name: str) -> Optional[str]:
    return next((u for u in USERS if u.lower() == name.lower()), None)


def add_notification(recipient: str, kind: str, from_user: str,
                     post_id: Optional[str] = None) -> None:
    if not recipient or recipient == from_user:
        return
    NOTIFICATIONS.insert(0, {
        "id": uuid.uuid4().hex[:12],
        "user": recipient,
        "from": from_user,
        "kind": kind,
        "post_id": post_id,
        "ts": datetime.now().timestamp(),
        "read": False,
    })


def unread_count(user: Optional[str]) -> int:
    if not user:
        return 0
    return sum(1 for n in NOTIFICATIONS if n["user"] == user and not n["read"])


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def back(request: Request) -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


def render_text(text: str) -> str:
    escaped = html.escape(text)

    def repl(m):
        name = m.group(1)
        actual = find_user(name)
        if actual:
            return (f'<a class="mention" href="/u/{html.escape(actual)}">'
                    f'@{html.escape(actual)}</a>')
        return m.group(0)

    return MENTION_RE.sub(repl, escaped)


def avatar(name: str, cls: str = "avatar") -> str:
    return f'<div class="{cls}">{html.escape(name[0].upper())}</div>'


# --------------------------------------------------------------------------
# СТИЛИ
# --------------------------------------------------------------------------
CSS = """
* {
  box-sizing: border-box;
  -webkit-tap-highlight-color: transparent;
  user-select: none;
  -webkit-user-select: none;
}
input, textarea {
  user-select: text;
  -webkit-user-select: text;
}
* { scrollbar-width: none; -ms-overflow-style: none; }
*::-webkit-scrollbar { display: none; width: 0; height: 0; }

:root {
  --bg: #f1f3f5;
  --surface: #ffffff;
  --surface-2: #f5f7f9;
  --surface-3: #e8ecf0;
  --text: #1a1d23;
  --muted: #6b7785;
  --border: #e3e7eb;
  --primary: #4f5fd0;
  --primary-hover: #3f4fc0;
  --on-primary: #ffffff;
  --like: #e91e63;
  --danger: #e53935;
  --shadow-sm: 0 1px 2px rgba(0,0,0,.06);
  --shadow-md: 0 4px 16px rgba(0,0,0,.10);
  --shadow-lg: 0 12px 40px rgba(0,0,0,.18);
  --topbar-h: 56px;
  --bottomnav-h: 62px;
  --radius: 14px;
  --radius-sm: 10px;
  --radius-pill: 999px;
  --icon-btn-size: 40px;
}
[data-theme="dark"] {
  --bg: #0f1115;
  --surface: #1a1d23;
  --surface-2: #23272e;
  --surface-3: #2c3138;
  --text: #e8eaed;
  --muted: #9aa3ad;
  --border: #2c3138;
  --primary: #8b98e0;
  --primary-hover: #a3aee8;
  --on-primary: #10131a;
  --like: #f06292;
  --danger: #ef5350;
  --shadow-sm: 0 1px 2px rgba(0,0,0,.4);
  --shadow-md: 0 4px 16px rgba(0,0,0,.5);
  --shadow-lg: 0 12px 40px rgba(0,0,0,.7);
}

html, body { margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  font-size: 15px;
  line-height: 1.5;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
  transition: background .2s, color .2s;
}
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
.mention { color: var(--primary); font-weight: 500; }

/* ---------- TOPBAR ---------- */
.topbar {
  position: sticky; top: 0; z-index: 90;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  height: var(--topbar-h);
}
.topbar-inner {
  max-width: 720px; margin: 0 auto;
  height: 100%; padding: 0 10px;
  display: flex; align-items: center; gap: 8px;
}
.brand {
  font-weight: 700; font-size: 1.15rem;
  color: var(--text);
  letter-spacing: -.4px;
  text-decoration: none;
  margin-right: auto;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
}
.brand:hover { text-decoration: none; }
.topbar-actions { display: flex; align-items: center; gap: 4px; }

/* ---------- ICON BUTTON ---------- */
.icon-btn {
  width: var(--icon-btn-size);
  height: var(--icon-btn-size);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: none;
  color: var(--text);
  cursor: pointer;
  text-decoration: none;
  font-family: inherit;
  position: relative;
  transition: background .15s, color .15s;
  flex-shrink: 0;
}
.icon-btn:hover { background: var(--surface-2); text-decoration: none; }
.icon-btn:active { background: var(--surface-3); }
.icon-btn .material-icons { font-size: 22px; }
.icon-btn.liked { color: var(--like); }
.icon-btn.danger:hover { color: var(--danger); }
.icon-btn.avatar-btn {
  background: var(--primary);
  color: var(--on-primary);
  font-weight: 700;
  font-size: 1rem;
}
.icon-btn.avatar-btn:hover { background: var(--primary-hover); }

/* ---------- BADGE ---------- */
.badge {
  position: absolute;
  top: 3px; right: 3px;
  background: #f44336; color: #fff;
  font-size: 10px; font-weight: 700;
  min-width: 16px; height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  line-height: 1;
  pointer-events: none;
}

/* ---------- PRIMARY BUTTON ---------- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 0 20px;
  min-height: 40px;
  border: none;
  border-radius: var(--radius-pill);
  background: var(--primary);
  color: var(--on-primary);
  font-family: inherit;
  font-size: .92rem;
  font-weight: 500;
  cursor: pointer;
  text-decoration: none;
  transition: background .15s;
}
.btn:hover { background: var(--primary-hover); text-decoration: none; }
.btn:active { transform: scale(.98); }
.btn-block { width: 100%; }
.btn .material-icons { font-size: 18px; }

/* ---------- CONTAINER ---------- */
.container {
  max-width: 720px;
  margin: 0 auto;
  padding: 16px 12px 40px;
}

/* ---------- FEED SEARCH ---------- */
.feed-search {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  padding: 0 16px;
  height: 46px;
  margin-bottom: 14px;
  transition: border-color .15s, box-shadow .15s;
}
.feed-search:focus-within {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 20%, transparent);
}
.feed-search .material-icons { color: var(--muted); font-size: 20px; }
.feed-search input {
  flex: 1;
  border: none;
  background: none;
  outline: none;
  color: var(--text);
  font-family: inherit;
  font-size: .95rem;
  height: 100%;
  padding: 0;
  min-width: 0;
}
.feed-search input::placeholder { color: var(--muted); }

/* ---------- CARDS ---------- */
.card {
  background: var(--surface);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  margin-bottom: 14px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.card-body { padding: 16px; }
.card-footer {
  padding: 4px 8px;
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 4px;
}
.card-footer .spacer { flex: 1; }
.likes-count {
  font-size: .85rem;
  color: var(--muted);
  margin-left: 2px;
  margin-right: 4px;
}

/* ---------- AVATAR ---------- */
.avatar {
  width: 44px; height: 44px;
  border-radius: 50%;
  background: var(--primary);
  color: var(--on-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 19px;
  flex-shrink: 0;
  user-select: none;
}
.avatar-lg { width: 72px; height: 72px; font-size: 30px; }
.avatar-sm { width: 36px; height: 36px; font-size: 15px; }

/* ---------- POST ---------- */
.card-header-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.post-author { color: var(--text); font-weight: 600; }
.post-author:hover { color: var(--primary); text-decoration: none; }
.post-time { font-size: .82rem; color: var(--muted); }
.post-text {
  white-space: pre-wrap;
  word-wrap: break-word;
  font-size: 1rem;
  line-height: 1.55;
}

/* ---------- COMPOSER ---------- */
.composer textarea {
  width: 100%;
  background: var(--surface-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  font-family: inherit;
  font-size: .95rem;
  resize: vertical;
  min-height: 72px;
  line-height: 1.5;
  outline: none;
  transition: border-color .15s, box-shadow .15s;
}
.composer textarea:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 20%, transparent);
}
.composer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 10px;
}
.hint { font-size: .8rem; color: var(--muted); }
.counter { font-size: .8rem; color: var(--muted); }

/* ---------- INPUTS ---------- */
.input-field { margin-bottom: 14px; }
.input-field label {
  display: block;
  font-size: .82rem;
  color: var(--muted);
  margin-bottom: 6px;
  font-weight: 500;
}
.input-field input {
  width: 100%;
  background: var(--surface-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  font-family: inherit;
  font-size: .95rem;
  outline: none;
  transition: border-color .15s, box-shadow .15s;
}
.input-field input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 20%, transparent);
}

/* ---------- AUTH ---------- */
.auth-wrap { max-width: 420px; margin: 0 auto; }
.auth-wrap h1 { font-weight: 300; font-size: 1.6rem; margin: 4px 0 20px; }
.auth-alt { text-align: center; color: var(--muted); font-size: .9rem; margin-top: 14px; }
.error-box {
  background: rgba(229,57,53,.1);
  color: var(--danger);
  border: 1px solid rgba(229,57,53,.25);
  padding: 10px 14px;
  border-radius: var(--radius-sm);
  margin-bottom: 14px;
  font-size: .9rem;
}

/* ---------- EMPTY ---------- */
.empty { padding: 44px 20px; text-align: center; color: var(--muted); }
.empty .material-icons {
  font-size: 48px;
  opacity: .35;
  display: block;
  margin: 0 auto 10px;
}

/* ---------- PROFILE ---------- */
.profile-header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.profile-name { margin: 0; font-weight: 500; font-size: 1.4rem; }
.profile-stats { color: var(--muted); font-size: .9rem; margin-top: 4px; }
.profile-actions { margin-left: auto; }

/* ---------- SEARCH RESULTS ---------- */
.user-chip {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  color: var(--text);
  text-decoration: none;
}
.user-chip:last-child { border-bottom: none; }
.user-chip:hover { background: var(--surface-2); text-decoration: none; }

/* ---------- NOTIFICATIONS DROPDOWN ---------- */
.notif-dropdown {
  position: fixed;
  top: calc(var(--topbar-h) + 8px);
  right: 12px;
  width: 360px;
  max-width: calc(100vw - 24px);
  max-height: calc(100vh - var(--topbar-h) - 24px);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
  z-index: 200;
  display: none;
  overflow: hidden;
  flex-direction: column;
}
.notif-dropdown.open { display: flex; }
.notif-head {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
  font-size: .95rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}
.notif-head .muted { font-weight: 400; font-size: .85rem; }
.notif-list { overflow-y: auto; flex: 1; min-height: 0; }
.notif-item {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  color: var(--text);
  text-decoration: none;
  transition: background .15s;
}
.notif-item:last-child { border-bottom: none; }
.notif-item:hover { background: var(--surface-2); text-decoration: none; }
.notif-item.unread { background: rgba(79,95,208,.07); }
[data-theme="dark"] .notif-item.unread { background: rgba(139,152,224,.10); }
.notif-icon {
  width: 38px; height: 38px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--surface-2);
  flex-shrink: 0;
}
.notif-icon .material-icons { font-size: 19px; color: var(--primary); }
.notif-body { flex: 1; min-width: 0; }
.notif-text { font-size: .92rem; line-height: 1.4; }
.notif-text b { color: var(--text); font-weight: 600; }
.notif-time { font-size: .76rem; color: var(--muted); margin-top: 3px; }
.notif-empty {
  padding: 40px 20px;
  text-align: center;
  color: var(--muted);
  font-size: .9rem;
}

/* ---------- BOTTOM NAV ---------- */
.bottomnav {
  display: none;
  position: fixed;
  bottom: 0; left: 0; right: 0;
  height: calc(var(--bottomnav-h) + env(safe-area-inset-bottom));
  padding-bottom: env(safe-area-inset-bottom);
  background: var(--surface);
  border-top: 1px solid var(--border);
  z-index: 95;
}
.bottomnav-inner {
  height: var(--bottomnav-h);
  max-width: 720px;
  margin: 0 auto;
  display: flex;
  align-items: stretch;
}
.bottomnav .icon-btn {
  flex: 1;
  width: auto;
  height: 100%;
  border-radius: 0;
}
.bottomnav .icon-btn .material-icons { font-size: 24px; }
.bottomnav .icon-btn.avatar-btn {
  background: none;
  color: var(--primary);
}
.bottomnav .icon-btn.avatar-btn:hover { background: var(--surface-2); }

/* ---------- UTILITY ---------- */
.muted { color: var(--muted); }
.small { font-size: .85rem; }
.page-title { font-size: 1.3rem; font-weight: 500; margin: 0 0 14px 4px; }
.section-title {
  font-size: .8rem;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .5px;
  margin: 18px 4px 8px;
}

/* ---------- RESPONSIVE ---------- */
@media (min-width: 641px) {
  .only-mobile { display: none !important; }
}
@media (max-width: 640px) {
  .only-desktop { display: none !important; }
  .bottomnav { display: block; }
  .container {
    padding: 12px 8px calc(var(--bottomnav-h) + env(safe-area-inset-bottom) + 20px);
  }
  .topbar-inner { padding: 0 8px; gap: 4px; }
  .brand { font-size: 1.05rem; padding: 6px 4px; }
  .card-body { padding: 14px; }
  .post-text { font-size: .97rem; }
  .notif-dropdown {
    top: auto;
    bottom: calc(var(--bottomnav-h) + env(safe-area-inset-bottom) + 8px);
    left: 8px; right: 8px;
    width: auto;
    max-width: none;
    max-height: 65vh;
  }
  .profile-header { gap: 12px; }
  .profile-name { font-size: 1.2rem; }
  .avatar-lg { width: 60px; height: 60px; font-size: 24px; }
}
"""


# --------------------------------------------------------------------------
# ФРАГМЕНТЫ
# --------------------------------------------------------------------------
def render_notif_item(n: dict) -> str:
    if n["kind"] == "mention":
        text = f'<b>{html.escape(n["from"])}</b> упомянул вас в посте'
        icon = "alternate_email"
    elif n["kind"] == "like":
        text = f'<b>{html.escape(n["from"])}</b> оценил ваш пост'
        icon = "favorite"
    else:
        text = html.escape(n["from"])
        icon = "notifications"
    href = f'/post/{n["post_id"]}' if n.get("post_id") else "/"
    cls = "notif-item" + ("" if n["read"] else " unread")
    return f"""
<a class="{cls}" href="{href}">
  <div class="notif-icon"><i class="material-icons">{icon}</i></div>
  <div class="notif-body">
    <div class="notif-text">{text}</div>
    <div class="notif-time">{fmt_time(n['ts'])}</div>
  </div>
</a>"""


def render_notif_dropdown(user: Optional[str]) -> str:
    if not user:
        return ""
    mine = [n for n in NOTIFICATIONS if n["user"] == user][:40]
    if mine:
        items = "".join(render_notif_item(n) for n in mine)
    else:
        items = '<div class="notif-empty">Уведомлений пока нет</div>'
    return f"""
<div class="notif-dropdown" id="notifDropdown">
  <div class="notif-head">
    <span>Уведомления</span>
    <span class="muted">последние {len(mine)}</span>
  </div>
  <div class="notif-list">{items}</div>
</div>"""


# --------------------------------------------------------------------------
# LAYOUT
# --------------------------------------------------------------------------
def layout(title: str, content: str, user: Optional[str] = None,
           theme: str = "light") -> str:
    theme = "dark" if theme == "dark" else "light"
    unread = unread_count(user)

    bell_icon = "notifications" if unread else "notifications_none"
    badge = (f'<span class="badge">{unread if unread < 100 else "99+"}</span>'
             if unread else "")

    # ---- верхняя панель: справа иконки ----
    if user:
        desktop_actions = (
            f'<a href="/u/{html.escape(user)}" class="icon-btn avatar-btn only-desktop" '
            f'title="{html.escape(user)}">{html.escape(user[0].upper())}</a>'
            f'<form class="only-desktop" action="/logout" method="post" '
            f'style="display:inline-flex;margin:0">'
            f'<button class="icon-btn" type="submit" title="Выйти">'
            f'<i class="material-icons">logout</i></button></form>'
        )
    else:
        desktop_actions = (
            '<a href="/login" class="icon-btn only-desktop" title="Войти">'
            '<i class="material-icons">person_outline</i></a>'
        )

    theme_icon = "light_mode" if theme == "dark" else "dark_mode"

    notif_btn_top = ""
    if user:
        notif_btn_top = (
            f'<button class="icon-btn only-desktop" data-notif-toggle '
            f'onclick="toggleNotif(event)" title="Уведомления">'
            f'<i class="material-icons">{bell_icon}</i>{badge}</button>'
        )

    topbar = f"""
<nav class="topbar">
  <div class="topbar-inner">
    <a href="/" class="brand">MiniNet</a>
    <div class="topbar-actions">
      {notif_btn_top}
      <a href="/toggle-theme" class="icon-btn" title="Сменить тему">
        <i class="material-icons">{theme_icon}</i>
      </a>
      {desktop_actions}
    </div>
  </div>
</nav>"""

    # ---- нижняя навигация (только мобильные) ----
    if user:
        bottom_profile = (
            f'<a href="/u/{html.escape(user)}" class="icon-btn avatar-btn only-mobile" '
            f'title="Профиль">{html.escape(user[0].upper())}</a>'
        )
        notif_btn_bottom = (
            f'<button class="icon-btn" data-notif-toggle '
            f'onclick="toggleNotif(event)" title="Уведомления">'
            f'<i class="material-icons">{bell_icon}</i>{badge}</button>'
        )
    else:
        bottom_profile = (
            '<a href="/login" class="icon-btn only-mobile" title="Войти">'
            '<i class="material-icons">person_outline</i></a>'
        )
        notif_btn_bottom = (
            '<a href="/login" class="icon-btn" title="Войти">'
            '<i class="material-icons">notifications_none</i></a>'
        )

    bottomnav = f"""
<nav class="bottomnav">
  <div class="bottomnav-inner">
    <a href="/" class="icon-btn" title="Главная">
      <i class="material-icons">home</i>
    </a>
    <a href="/#feedSearch" class="icon-btn" onclick="return goSearch(event)" title="Поиск">
      <i class="material-icons">search</i>
    </a>
    {notif_btn_bottom}
    {bottom_profile}
  </div>
</nav>"""

    notif_dropdown = render_notif_dropdown(user)

    return f"""<!DOCTYPE html>
<html lang="ru" data-theme="{theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)} · MiniNet</title>
<link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
{topbar}
<main class="container">{content}</main>
{bottomnav}
{notif_dropdown}
<script>
// Запрет контекстного меню
document.addEventListener('contextmenu', function (e) {{
  var t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
  e.preventDefault();
}});

// Счётчик символов
document.querySelectorAll('textarea[maxlength]').forEach(function (ta) {{
  var counter = ta.parentElement.querySelector('.counter');
  if (!counter) return;
  function upd() {{ counter.textContent = ta.value.length + ' / ' + ta.maxLength; }}
  ta.addEventListener('input', upd); upd();
}});

// Уведомления
function toggleNotif(e) {{
  if (e) e.preventDefault();
  var dd = document.getElementById('notifDropdown');
  if (!dd) return;
  var willOpen = !dd.classList.contains('open');
  dd.classList.toggle('open');
  if (willOpen) {{
    fetch('/api/notifications/read', {{method: 'POST', credentials: 'same-origin'}})
      .then(function () {{
        document.querySelectorAll('.badge').forEach(function (b) {{ b.remove(); }});
        document.querySelectorAll('.notif-item.unread').forEach(function (n) {{
          n.classList.remove('unread');
        }});
      }})
      .catch(function () {{}});
  }}
}}
document.addEventListener('click', function (e) {{
  var dd = document.getElementById('notifDropdown');
  if (!dd || !dd.classList.contains('open')) return;
  if (dd.contains(e.target)) return;
  if (e.target.closest('[data-notif-toggle]')) return;
  dd.classList.remove('open');
}});
document.addEventListener('keydown', function (e) {{
  if (e.key === 'Escape') {{
    var dd = document.getElementById('notifDropdown');
    if (dd) dd.classList.remove('open');
  }}
}});

// Поиск из нижней навигации
function goSearch(e) {{
  var inp = document.getElementById('feedSearch');
  if (inp) {{
    e.preventDefault();
    inp.scrollIntoView({{behavior: 'smooth', block: 'center'}});
    setTimeout(function () {{ inp.focus(); }}, 250);
    return false;
  }}
  return true;
}}
</script>
</body>
</html>"""


# --------------------------------------------------------------------------
# КОМПОНЕНТЫ
# --------------------------------------------------------------------------
def render_post(post: dict, user: Optional[str], show_delete: bool = True) -> str:
    liked = user is not None and user in post["likes"]
    likes_n = len(post["likes"])

    if user:
        like_btn = (
            f'<form action="/like/{post["id"]}" method="post" style="display:inline;margin:0">'
            f'<button class="icon-btn {"liked" if liked else ""}" type="submit" '
            f'title="{"Убрать лайк" if liked else "Нравится"}">'
            f'<i class="material-icons">{"favorite" if liked else "favorite_border"}</i>'
            f'</button></form>'
        )
    else:
        like_btn = (
            '<a href="/login" class="icon-btn" title="Войдите, чтобы лайкать">'
            '<i class="material-icons">favorite_border</i></a>'
        )

    delete_html = ""
    if show_delete and user == post["author"]:
        delete_html = (
            f'<form action="/delete/{post["id"]}" method="post" '
            f'style="display:inline;margin:0" '
            f'onsubmit="return confirm(\'Удалить этот пост?\')">'
            f'<button class="icon-btn danger" type="submit" title="Удалить">'
            f'<i class="material-icons">delete_outline</i></button></form>'
        )

    return f"""
<div class="card" id="post-{post['id']}">
  <div class="card-body">
    <div class="card-header-row">
      {avatar(post['author'])}
      <div style="min-width:0">
        <a class="post-author" href="/u/{html.escape(post['author'])}">{html.escape(post['author'])}</a><br>
        <span class="post-time">{fmt_time(post['ts'])}</span>
      </div>
    </div>
    <div class="post-text">{render_text(post['text'])}</div>
  </div>
  <div class="card-footer">
    {like_btn}
    <span class="likes-count">{likes_n}</span>
    <a class="icon-btn" href="/post/{post['id']}" title="Открыть пост">
      <i class="material-icons">chat_bubble_outline</i>
    </a>
    <div class="spacer"></div>
    {delete_html}
  </div>
</div>"""


def composer_html(user: str) -> str:
    return f"""
<div class="card composer">
  <div class="card-body">
    <form action="/post" method="post">
      <textarea name="text" maxlength="{MAX_POST}" required
                placeholder="Что нового, {html.escape(user)}?"></textarea>
      <div class="composer-footer">
        <span class="hint">Используйте @username для упоминания</span>
        <div style="display:flex;align-items:center;gap:12px">
          <span class="counter">0 / {MAX_POST}</span>
          <button class="btn" type="submit">
            <i class="material-icons">send</i>Опубликовать
          </button>
        </div>
      </div>
    </form>
  </div>
</div>"""


def guest_composer() -> str:
    return """
<div class="card"><div class="card-body" style="text-align:center">
  <p class="muted" style="margin:0 0 12px">Войдите или зарегистрируйтесь, чтобы публиковать.</p>
  <a class="btn" href="/login">Войти</a>
  <a class="btn" href="/register" style="margin-left:6px">Регистрация</a>
</div></div>"""


def feed_search_html(q: str = "") -> str:
    return f"""
<form class="feed-search" action="/search" method="get">
  <i class="material-icons">search</i>
  <input id="feedSearch" type="text" name="q" placeholder="Поиск людей и постов"
         autocomplete="off" value="{html.escape(q)}">
</form>"""


def empty_card(text: str, icon: str = "inbox") -> str:
    return (f'<div class="card"><div class="empty">'
            f'<i class="material-icons">{icon}</i>{html.escape(text)}</div></div>')


# --------------------------------------------------------------------------
# СТРАНИЦЫ
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(session: Optional[str] = Cookie(default=None),
          theme: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    head = composer_html(user) if user else guest_composer()

    if POSTS:
        posts_html = "".join(render_post(p, user) for p in POSTS)
    else:
        posts_html = empty_card("Пока нет ни одного поста", "article")

    stats = (f'<p class="muted small" style="margin:0 0 12px 4px">'
             f'Постов: {len(POSTS)} · Пользователей: {len(USERS)}</p>')

    content = feed_search_html() + head + stats + posts_html
    return HTMLResponse(layout("Лента", content, user, theme or "light"))


@app.get("/toggle-theme")
def toggle_theme(request: Request, theme: Optional[str] = Cookie(default=None)):
    new_theme = "light" if theme == "dark" else "dark"
    resp = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    resp.set_cookie("theme", new_theme, max_age=365 * 24 * 3600, samesite="lax")
    return resp


@app.post("/api/notifications/read")
def mark_notifications_read(session: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    for n in NOTIFICATIONS:
        if n["user"] == user:
            n["read"] = True
    return JSONResponse({"ok": True})


# ------------------------------ Авторизация ------------------------------
def auth_field(name: str, label: str, type_: str = "text") -> str:
    return (f'<div class="input-field">'
            f'<label for="{name}">{html.escape(label)}</label>'
            f'<input id="{name}" name="{name}" type="{type_}" required></div>')


def auth_page(title: str, fields_html: str, action: str, submit: str,
              alt_html: str, error: Optional[str] = None) -> str:
    err = f'<div class="error-box">{html.escape(error)}</div>' if error else ""
    return f"""
<div class="auth-wrap">
  <h1>{html.escape(title)}</h1>
  {err}
  <div class="card"><div class="card-body">
    <form action="{action}" method="post">
      {fields_html}
      <button class="btn btn-block" type="submit" style="margin-top:6px">{html.escape(submit)}</button>
    </form>
  </div></div>
  <p class="auth-alt">{alt_html}</p>
</div>"""


@app.get("/register", response_class=HTMLResponse)
def register_page(session: Optional[str] = Cookie(default=None),
                  theme: Optional[str] = Cookie(default=None)):
    if current_user(session):
        return RedirectResponse("/", status_code=303)
    fields = (auth_field("username", "Имя пользователя") +
              auth_field("password", "Пароль", "password") +
              auth_field("password2", "Повторите пароль", "password"))
    body = auth_page("Регистрация", fields, "/register", "Создать аккаунт",
                     'Уже есть аккаунт? <a href="/login">Войти</a>')
    return HTMLResponse(layout("Регистрация", body, None, theme or "light"))


@app.post("/register")
def register(username: str = Form(...),
             password: str = Form(...),
             password2: str = Form(...),
             theme: Optional[str] = Cookie(default=None)):
    username = username.strip()
    err = None
    if not (3 <= len(username) <= 20) or not username.replace("_", "").isalnum():
        err = "Имя: 3–20 символов, только буквы, цифры и «_»."
    elif len(password) < 4:
        err = "Пароль должен быть не короче 4 символов."
    elif password != password2:
        err = "Пароли не совпадают."
    elif find_user(username):
        err = "Такое имя уже занято."

    if err:
        fields = (auth_field("username", "Имя пользователя") +
                  auth_field("password", "Пароль", "password") +
                  auth_field("password2", "Повторите пароль", "password"))
        body = auth_page("Регистрация", fields, "/register", "Создать аккаунт",
                         'Уже есть аккаунт? <a href="/login">Войти</a>', err)
        return HTMLResponse(layout("Регистрация", body, None, theme or "light"),
                            status_code=400)

    salt = secrets.token_hex(16)
    USERS[username] = {
        "salt": salt,
        "hash": hash_password(password, salt),
        "created": datetime.now().timestamp(),
    }
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True,
                    max_age=7 * 24 * 3600, samesite="lax")
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(session: Optional[str] = Cookie(default=None),
               theme: Optional[str] = Cookie(default=None)):
    if current_user(session):
        return RedirectResponse("/", status_code=303)
    fields = (auth_field("username", "Имя пользователя") +
              auth_field("password", "Пароль", "password"))
    body = auth_page("Вход", fields, "/login", "Войти",
                     'Нет аккаунта? <a href="/register">Зарегистрироваться</a>')
    return HTMLResponse(layout("Вход", body, None, theme or "light"))


@app.post("/login")
def login(username: str = Form(...),
          password: str = Form(...),
          theme: Optional[str] = Cookie(default=None)):
    username = username.strip()
    user = find_user(username)
    ok = False
    if user:
        ok = secrets.compare_digest(USERS[user]["hash"],
                                    hash_password(password, USERS[user]["salt"]))
    if not ok:
        fields = (auth_field("username", "Имя пользователя") +
                  auth_field("password", "Пароль", "password"))
        body = auth_page("Вход", fields, "/login", "Войти",
                         'Нет аккаунта? <a href="/register">Зарегистрироваться</a>',
                         "Неверное имя пользователя или пароль.")
        return HTMLResponse(layout("Вход", body, None, theme or "light"),
                            status_code=401)

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = user
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True,
                    max_age=7 * 24 * 3600, samesite="lax")
    return resp


@app.post("/logout")
def logout(session: Optional[str] = Cookie(default=None)):
    if session:
        SESSIONS.pop(session, None)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session")
    return resp


# ------------------------------ Посты ------------------------------
@app.post("/post")
def create_post(request: Request,
                text: str = Form(...),
                session: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    if not user:
        return RedirectResponse("/login", status_code=303)

    text = text.strip()[:MAX_POST]
    if text:
        post = {
            "id": uuid.uuid4().hex[:12],
            "author": user,
            "text": text,
            "ts": datetime.now().timestamp(),
            "likes": set(),
        }
        POSTS.insert(0, post)

        for name in set(MENTION_RE.findall(text)):
            actual = find_user(name)
            if actual:
                add_notification(actual, "mention", user, post["id"])

    return back(request)


@app.post("/like/{post_id}")
def like(post_id: str,
         request: Request,
         session: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    if not user:
        return RedirectResponse("/login", status_code=303)

    for p in POSTS:
        if p["id"] == post_id:
            if user in p["likes"]:
                p["likes"].discard(user)
            else:
                p["likes"].add(user)
                add_notification(p["author"], "like", user, p["id"])
            break
    return back(request)


@app.post("/delete/{post_id}")
def delete_post(post_id: str,
                request: Request,
                session: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    if not user:
        return RedirectResponse("/login", status_code=303)

    for i, p in enumerate(POSTS):
        if p["id"] == post_id and p["author"] == user:
            POSTS.pop(i)
            break
    return back(request)


@app.get("/post/{post_id}", response_class=HTMLResponse)
def post_page(post_id: str,
              session: Optional[str] = Cookie(default=None),
              theme: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    post = next((p for p in POSTS if p["id"] == post_id), None)
    if not post:
        return HTMLResponse(
            layout("Пост не найден",
                   empty_card("Пост не найден или удалён", "search_off"),
                   user, theme or "light"),
            status_code=404)
    content = feed_search_html() + render_post(post, user)
    return HTMLResponse(layout("Пост", content, user, theme or "light"))


# ------------------------------ Профиль ------------------------------
@app.get("/u/{username}", response_class=HTMLResponse)
def profile(username: str,
            session: Optional[str] = Cookie(default=None),
            theme: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    target = find_user(username)

    if not target:
        return HTMLResponse(
            layout("404", empty_card("Пользователь не найден", "person_off"),
                   user, theme or "light"),
            status_code=404)

    user_posts = [p for p in POSTS if p["author"] == target]
    likes_total = sum(len(p["likes"]) for p in user_posts)

    logout_btn = ""
    if user == target:
        logout_btn = (
            '<form class="profile-actions" action="/logout" method="post" style="margin:0">'
            '<button class="btn" type="submit">Выйти</button></form>'
        )

    header = f"""
<div class="card"><div class="card-body profile-header">
  {avatar(target, "avatar avatar-lg")}
  <div>
    <h1 class="profile-name">{html.escape(target)}</h1>
    <div class="profile-stats">Постов: {len(user_posts)} · Лайков получено: {likes_total}</div>
  </div>
  {logout_btn}
</div></div>"""

    if user_posts:
        posts_html = "".join(render_post(p, user) for p in user_posts)
    else:
        posts_html = empty_card("Постов пока нет", "article")

    content = feed_search_html() + header + posts_html
    return HTMLResponse(layout(f"@{target}", content, user, theme or "light"))


# ------------------------------ Поиск ------------------------------
@app.get("/search", response_class=HTMLResponse)
def search(q: str = "",
           session: Optional[str] = Cookie(default=None),
           theme: Optional[str] = Cookie(default=None)):
    user = current_user(session)
    q = q.strip()

    parts = [feed_search_html(q)]

    if not q:
        parts.append(empty_card("Введите запрос", "search"))
        return HTMLResponse(layout("Поиск", "".join(parts), user, theme or "light"))

    ql = q.lower()
    users_found = [u for u in USERS if ql in u.lower()]
    posts_found = [p for p in POSTS
                   if ql in p["text"].lower() or ql in p["author"].lower()]

    parts.append(f'<h2 class="page-title">Результаты: «{html.escape(q)}»</h2>')

    if users_found:
        chips = "".join(
            f'<a class="user-chip" href="/u/{html.escape(u)}">'
            f'{avatar(u, "avatar avatar-sm")}'
            f'<div><b>{html.escape(u)}</b>'
            f'<div class="small muted">'
            f'Постов: {sum(1 for p in POSTS if p["author"] == u)}</div></div></a>'
            for u in users_found
        )
        parts.append(f'<div class="section-title">Люди</div>'
                     f'<div class="card">{chips}</div>')

    if posts_found:
        parts.append('<div class="section-title">Посты</div>')
        parts.append("".join(render_post(p, user, show_delete=False)
                             for p in posts_found))

    if not users_found and not posts_found:
        parts.append(empty_card("Ничего не найдено", "search_off"))

    return HTMLResponse(layout("Поиск", "".join(parts), user, theme or "light"))
