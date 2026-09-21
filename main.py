# main.py
# Запуск:  pip install fastapi uvicorn
#          uvicorn main:app --reload
# Открыть: http://127.0.0.1:8000

import time
import uuid
import json
import hashlib
import re
import urllib.request
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="sldChat")

# ------------------------------------------------------------------
# Хранилища в оперативке
# ------------------------------------------------------------------
USERS: Dict[str, dict] = {}          # nick -> user
SESSIONS: Dict[str, str] = {}        # token -> nick
POSTS: Dict[str, dict] = {}          # post_id -> post
NOTIFICATIONS: Dict[str, List[dict]] = {}  # nick -> [notif, ...]

MAX_POST_LEN = 1000
MAX_COMMENT_LEN = 500
TRUNCATE_LINES = 100
TRUNCATE_CHARS = 500

NICK_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")

RU_COUNTRIES = {"RU", "BY", "KZ", "UA", "KG", "TJ", "UZ", "AM", "AZ", "MD"}
_lang_cache: Dict[str, str] = {}


# ------------------------------------------------------------------
# Утилиты
# ------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = uuid.uuid4().hex
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return salt + "$" + h


def check_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except Exception:
        return False
    return hashlib.sha256((salt + password).encode()).hexdigest() == h


def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def detect_lang(ip: str, accept_language: str) -> str:
    key = ip or "unknown"
    if key in _lang_cache:
        return _lang_cache[key]
    lang = None
    private = (
        not ip
        or ip.startswith("127.")
        or ip.startswith("10.")
        or ip.startswith("192.168.")
        or ip.startswith("172.")
        or ip in ("::1", "localhost")
    )
    if not private:
        try:
            req = urllib.request.Request(
                f"http://ip-api.com/json/{ip}?fields=countryCode",
                headers={"User-Agent": "sldChat"},
            )
            with urllib.request.urlopen(req, timeout=2) as r:
                data = json.loads(r.read().decode())
                cc = (data.get("countryCode") or "").upper()
                if cc in RU_COUNTRIES:
                    lang = "ru"
                elif cc:
                    lang = "en"
        except Exception:
            pass
    if not lang:
        al = (accept_language or "").lower()
        lang = "ru" if (al.startswith("ru") or ",ru" in al or "ru-" in al) else "en"
    _lang_cache[key] = lang
    return lang


def get_lang(request: Request) -> str:
    qp = request.query_params.get("lang")
    if qp in ("ru", "en"):
        return qp
    ck = request.cookies.get("sldchat_lang")
    if ck in ("ru", "en"):
        return ck
    ip = get_client_ip(request)
    al = request.headers.get("accept-language", "")
    return detect_lang(ip, al)


def get_current_user(request: Request) -> Optional[dict]:
    token = request.headers.get("x-auth")
    if not token:
        return None
    nick = SESSIONS.get(token)
    if not nick:
        return None
    return USERS.get(nick)


def get_voter_id(request: Request, client_id: str = "") -> str:
    user = get_current_user(request)
    if user:
        return "u:" + user["nick"]
    return "c:" + (client_id or "anon")


def notify(to_nick: str, ntype: str, from_nick: str, post_id: str = "", text: str = ""):
    if not to_nick or to_nick == from_nick:
        return
    if to_nick not in NOTIFICATIONS:
        NOTIFICATIONS[to_nick] = []
    NOTIFICATIONS[to_nick].append({
        "id": uuid.uuid4().hex[:10],
        "type": ntype,
        "from_nick": from_nick,
        "post_id": post_id,
        "text": text,
        "created_at": time.time(),
        "read": False,
    })


# ------------------------------------------------------------------
# Модели
# ------------------------------------------------------------------
class PostIn(BaseModel):
    text: str
    private: bool = False


class VoteIn(BaseModel):
    direction: int
    client_id: str = ""


class CommentIn(BaseModel):
    text: str
    client_id: str = ""


class RegisterIn(BaseModel):
    name: str
    nick: str
    password: str
    password_confirm: str


class LoginIn(BaseModel):
    nick: str
    password: str


# ------------------------------------------------------------------
# Сериализация
# ------------------------------------------------------------------
def serialize_user(u: dict, viewer_nick: Optional[str] = None) -> dict:
    return {
        "nick": u["nick"],
        "name": u["name"],
        "created_at": u["created_at"],
        "followers": len(u["followers"]),
        "following": len(u["following"]),
        "is_me": viewer_nick == u["nick"],
    }


def serialize_post(p: dict, voter_id: str = "") -> dict:
    up = sum(1 for v in p["votes"].values() if v == 1)
    down = sum(1 for v in p["votes"].values() if v == -1)
    user_vote = p["votes"].get(voter_id, 0) if voter_id else 0
    comments = []
    for c in p["comments"]:
        cup = sum(1 for v in c["votes"].values() if v == 1)
        cdown = sum(1 for v in c["votes"].values() if v == -1)
        cuv = c["votes"].get(voter_id, 0) if voter_id else 0
        comments.append({
            "id": c["id"],
            "text": c["text"],
            "created_at": c["created_at"],
            "author": c.get("author"),
            "upvotes": cup,
            "downvotes": cdown,
            "user_vote": cuv,
        })
    return {
        "id": p["id"],
        "text": p["text"],
        "created_at": p["created_at"],
        "private": p.get("private", False),
        "author": p.get("author"),
        "upvotes": up,
        "downvotes": down,
        "user_vote": user_vote,
        "comments": comments,
    }


# ------------------------------------------------------------------
# API: авторизация
# ------------------------------------------------------------------
@app.post("/api/register")
def api_register(data: RegisterIn):
    name = data.name.strip()
    nick = data.nick.strip().lstrip("@")
    if not NICK_RE.match(nick):
        raise HTTPException(400, "err_bad_nick")
    if len(name) < 1 or len(name) > 50:
        raise HTTPException(400, "err_bad_name")
    if len(data.password) < 6:
        raise HTTPException(400, "err_short_pass")
    if data.password != data.password_confirm:
        raise HTTPException(400, "err_pass_mismatch")
    key = nick.lower()
    if any(u["nick"].lower() == key for u in USERS.values()):
        raise HTTPException(400, "err_nick_taken")

    u = {
        "nick": nick,
        "name": name,
        "password": hash_password(data.password),
        "created_at": time.time(),
        "following": set(),
        "followers": set(),
    }
    USERS[nick] = u
    token = uuid.uuid4().hex
    SESSIONS[token] = nick
    return {"token": token, "user": serialize_user(u, nick)}


@app.post("/api/login")
def api_login(data: LoginIn):
    nick = data.nick.strip().lstrip("@")
    # ищем без учёта регистра
    found = None
    for u in USERS.values():
        if u["nick"].lower() == nick.lower():
            found = u
            break
    if not found or not check_password(data.password, found["password"]):
        raise HTTPException(400, "err_bad_login")
    token = uuid.uuid4().hex
    SESSIONS[token] = found["nick"]
    return {"token": token, "user": serialize_user(found, found["nick"])}


@app.post("/api/logout")
def api_logout(request: Request):
    token = request.headers.get("x-auth")
    if token:
        SESSIONS.pop(token, None)
    return {"ok": True}


@app.get("/api/me")
def api_me(request: Request):
    u = get_current_user(request)
    if not u:
        raise HTTPException(401, "unauthorized")
    return serialize_user(u, u["nick"])


# ------------------------------------------------------------------
# API: посты
# ------------------------------------------------------------------
@app.get("/api/posts")
def api_list(request: Request, q: str = "", author: str = "", client_id: str = ""):
    viewer = get_current_user(request)
    viewer_nick = viewer["nick"] if viewer else None
    vid = get_voter_id(request, client_id)

    items = list(POSTS.values())

    if author:
        items = [p for p in items if p.get("author") == author]
        # приватные видны только автору
        if author != viewer_nick:
            items = [p for p in items if not p.get("private")]
    else:
        # в общей ленте приватных нет
        items = [p for p in items if not p.get("private")]

    if q:
        needle = q.strip().lower()
        if needle:
            items = [p for p in items if needle in p["text"].lower()]

    items.sort(key=lambda p: p["created_at"], reverse=True)
    return {"posts": [serialize_post(p, vid) for p in items]}


@app.get("/api/posts/{pid}")
def api_get(pid: str, request: Request, client_id: str = ""):
    p = POSTS.get(pid)
    if not p:
        raise HTTPException(404, "not found")
    vid = get_voter_id(request, client_id)
    return serialize_post(p, vid)


@app.post("/api/posts")
def api_create(payload: PostIn, request: Request):
    u = get_current_user(request)
    if not u:
        raise HTTPException(401, "unauthorized")
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_POST_LEN:
        raise HTTPException(400, "too long")
    pid = uuid.uuid4().hex[:10]
    p = {
        "id": pid,
        "text": text,
        "created_at": time.time(),
        "private": bool(payload.private),
        "author": u["nick"],
        "votes": {},
        "comments": [],
    }
    POSTS[pid] = p
    return serialize_post(p, "u:" + u["nick"])


@app.post("/api/posts/{pid}/vote")
def api_vote_post(pid: str, v: VoteIn, request: Request):
    p = POSTS.get(pid)
    if not p:
        raise HTTPException(404, "not found")
    if v.direction not in (-1, 1):
        raise HTTPException(400, "bad request")
    vid = get_voter_id(request, v.client_id)
    cur = p["votes"].get(vid, 0)
    if cur == v.direction:
        p["votes"].pop(vid, None)
    else:
        p["votes"][vid] = v.direction
    return serialize_post(p, vid)


@app.post("/api/posts/{pid}/comments")
def api_add_comment(pid: str, c: CommentIn, request: Request):
    u = get_current_user(request)
    if not u:
        raise HTTPException(401, "unauthorized")
    p = POSTS.get(pid)
    if not p:
        raise HTTPException(404, "not found")
    text = c.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if len(text) > MAX_COMMENT_LEN:
        raise HTTPException(400, "too long")
    comment = {
        "id": uuid.uuid4().hex[:10],
        "text": text,
        "created_at": time.time(),
        "author": u["nick"],
        "votes": {},
    }
    p["comments"].append(comment)
    # уведомление автору поста
    notify(p.get("author"), "comment", u["nick"], post_id=p["id"], text=text[:120])
    return serialize_post(p, "u:" + u["nick"])


@app.post("/api/posts/{pid}/comments/{cid}/vote")
def api_vote_comment(pid: str, cid: str, v: VoteIn, request: Request):
    p = POSTS.get(pid)
    if not p:
        raise HTTPException(404, "not found")
    c = next((x for x in p["comments"] if x["id"] == cid), None)
    if not c:
        raise HTTPException(404, "not found")
    if v.direction not in (-1, 1):
        raise HTTPException(400, "bad request")
    vid = get_voter_id(request, v.client_id)
    cur = c["votes"].get(vid, 0)
    if cur == v.direction:
        c["votes"].pop(vid, None)
    else:
        c["votes"][vid] = v.direction
    return serialize_post(p, vid)


# ------------------------------------------------------------------
# API: пользователи и фолловеры
# ------------------------------------------------------------------
@app.get("/api/users/{nick}")
def api_user(nick: str, request: Request):
    u = USERS.get(nick)
    if not u:
        # попробуем найти без учёта регистра
        for x in USERS.values():
            if x["nick"].lower() == nick.lower():
                u = x
                break
    if not u:
        raise HTTPException(404, "not found")
    viewer = get_current_user(request)
    viewer_nick = viewer["nick"] if viewer else None
    data = serialize_user(u, viewer_nick)
    data["is_following"] = bool(viewer and u["nick"] in viewer["following"])
    return data


@app.post("/api/users/{nick}/follow")
def api_follow(nick: str, request: Request):
    me = get_current_user(request)
    if not me:
        raise HTTPException(401, "unauthorized")
    target = USERS.get(nick)
    if not target:
        for x in USERS.values():
            if x["nick"].lower() == nick.lower():
                target = x
                break
    if not target:
        raise HTTPException(404, "not found")
    if target["nick"] == me["nick"]:
        raise HTTPException(400, "self")
    if target["nick"] not in me["following"]:
        me["following"].add(target["nick"])
        target["followers"].add(me["nick"])
        notify(target["nick"], "follow", me["nick"])
    return serialize_user(target, me["nick"])


@app.post("/api/users/{nick}/unfollow")
def api_unfollow(nick: str, request: Request):
    me = get_current_user(request)
    if not me:
        raise HTTPException(401, "unauthorized")
    target = USERS.get(nick)
    if not target:
        for x in USERS.values():
            if x["nick"].lower() == nick.lower():
                target = x
                break
    if not target:
        raise HTTPException(404, "not found")
    if target["nick"] in me["following"]:
        me["following"].discard(target["nick"])
        target["followers"].discard(me["nick"])
    return serialize_user(target, me["nick"])


# ------------------------------------------------------------------
# API: уведомления
# ------------------------------------------------------------------
@app.get("/api/notifications")
def api_notifications(request: Request):
    me = get_current_user(request)
    if not me:
        raise HTTPException(401, "unauthorized")
    items = list(NOTIFICATIONS.get(me["nick"], []))
    items.sort(key=lambda n: n["created_at"], reverse=True)
    unread = sum(1 for n in items if not n["read"])
    return {"items": items[:100], "unread": unread}


@app.post("/api/notifications/read")
def api_notifications_read(request: Request):
    me = get_current_user(request)
    if not me:
        raise HTTPException(401, "unauthorized")
    for n in NOTIFICATIONS.get(me["nick"], []):
        n["read"] = True
    return {"ok": True}


# ------------------------------------------------------------------
# Локализация
# ------------------------------------------------------------------
TEXTS = {
    "ru": {
        "search_ph": "Поиск по постам",
        "search": "Поиск",
        "theme": "Сменить тему",
        "post_ph": "Написать пост (до 1000 символов)",
        "comment_ph": "Написать комментарий",
        "publish": "Опубликовать",
        "send_comment": "Отправить",
        "no_posts": "Постов пока нет",
        "not_found": "Не найдено",
        "just_now": "только что",
        "sec_ago": "с",
        "min_ago": "мин",
        "hour_ago": "ч",
        "day_ago": "д",
        "read_more": "читать дальше",
        "copy": "Копировать",
        "copied": "Скопировано",
        "private": "Приватный",
        "f_new": "Новые",
        "f_top": "Лучшие",
        "f_bottom": "Худшие",
        "f_old": "Старые",
        "f_all": "Все",
        "f_many": "Много комм.",
        "f_some": "Есть комм.",
        "f_none": "Без комм.",
        "nav_home": "Главная",
        "nav_profile": "Профиль",
        "nav_notifications": "Уведомления",
        "nav_settings": "Настройки",
        "nav_logout": "Выйти",
        "nav_register": "Регистрация",
        "nav_login": "Вход",
        "reg_title": "Регистрация",
        "log_title": "Вход",
        "name_ph": "Имя",
        "nick_ph": "Ник (@nick)",
        "pass_ph": "Пароль",
        "pass2_ph": "Повтор пароля",
        "reg_btn": "Создать аккаунт",
        "log_btn": "Войти",
        "to_login": "Уже есть аккаунт? Войти",
        "to_reg": "Нет аккаунта? Регистрация",
        "err_bad_nick": "Ник: 3-20 символов, латиница, цифры, _",
        "err_bad_name": "Имя: от 1 до 50 символов",
        "err_short_pass": "Пароль: минимум 6 символов",
        "err_pass_mismatch": "Пароли не совпадают",
        "err_nick_taken": "Ник уже занят",
        "err_bad_login": "Неверный ник или пароль",
        "err_auth_required": "Требуется вход",
        "login_to_post": "Войдите, чтобы писать посты",
        "login_to_comment": "Войдите, чтобы писать комментарии",
        "go_login": "Войти",
        "go_register": "Регистрация",
        "profile_followers": "подписчиков",
        "profile_following": "подписок",
        "follow": "Подписаться",
        "unfollow": "Отписаться",
        "own_profile": "Это ваш профиль",
        "no_user_posts": "Постов пока нет",
        "settings_title": "Настройки",
        "settings_theme": "Тема",
        "settings_lang": "Язык",
        "theme_light": "Светлая",
        "theme_dark": "Тёмная",
        "notif_title": "Уведомления",
        "notif_empty": "Уведомлений нет",
        "notif_follow": "подписался на вас",
        "notif_comment": "оставил комментарий:",
        "back_to_main": "В главное меню",
    },
    "en": {
        "search_ph": "Search posts",
        "search": "Search",
        "theme": "Toggle theme",
        "post_ph": "Write a post (up to 1000 chars)",
        "comment_ph": "Write a comment",
        "publish": "Publish",
        "send_comment": "Send",
        "no_posts": "No posts yet",
        "not_found": "Not found",
        "just_now": "just now",
        "sec_ago": "s",
        "min_ago": "min",
        "hour_ago": "h",
        "day_ago": "d",
        "read_more": "read more",
        "copy": "Copy",
        "copied": "Copied",
        "private": "Private",
        "f_new": "New",
        "f_top": "Top",
        "f_bottom": "Worst",
        "f_old": "Old",
        "f_all": "All",
        "f_many": "Many",
        "f_some": "Some",
        "f_none": "None",
        "nav_home": "Home",
        "nav_profile": "Profile",
        "nav_notifications": "Notifications",
        "nav_settings": "Settings",
        "nav_logout": "Log out",
        "nav_register": "Sign up",
        "nav_login": "Log in",
        "reg_title": "Sign up",
        "log_title": "Log in",
        "name_ph": "Name",
        "nick_ph": "Nick (@nick)",
        "pass_ph": "Password",
        "pass2_ph": "Confirm password",
        "reg_btn": "Create account",
        "log_btn": "Log in",
        "to_login": "Already have an account? Log in",
        "to_reg": "No account? Sign up",
        "err_bad_nick": "Nick: 3-20 chars, letters/digits/_",
        "err_bad_name": "Name: 1-50 chars",
        "err_short_pass": "Password: min 6 chars",
        "err_pass_mismatch": "Passwords do not match",
        "err_nick_taken": "Nick already taken",
        "err_bad_login": "Wrong nick or password",
        "err_auth_required": "Login required",
        "login_to_post": "Log in to write posts",
        "login_to_comment": "Log in to write comments",
        "go_login": "Log in",
        "go_register": "Sign up",
        "profile_followers": "followers",
        "profile_following": "following",
        "follow": "Follow",
        "unfollow": "Unfollow",
        "own_profile": "This is your profile",
        "no_user_posts": "No posts yet",
        "settings_title": "Settings",
        "settings_theme": "Theme",
        "settings_lang": "Language",
        "theme_light": "Light",
        "theme_dark": "Dark",
        "notif_title": "Notifications",
        "notif_empty": "No notifications",
        "notif_follow": "followed you",
        "notif_comment": "commented:",
        "back_to_main": "Back to main",
    },
}


# ------------------------------------------------------------------
# SVG иконки
# ------------------------------------------------------------------
def svg(paths: str, size: int = 16, sw: float = 2) -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="{sw}" stroke-linecap="round" '
        f'stroke-linejoin="round">{paths}</svg>'
    )


ICON_SEARCH = svg('<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>')
ICON_MOON = svg('<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>')
ICON_SUN = svg(
    '<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/>'
    '<line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/>'
    '<line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/>'
    '<line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/>'
    '<line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/>'
)
ICON_BACK = svg('<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>')
ICON_UP = svg('<polyline points="6 15 12 9 18 15"/>', size=12, sw=2.5)
ICON_DOWN = svg('<polyline points="6 9 12 15 18 9"/>', size=12, sw=2.5)
ICON_COMMENT = svg(
    '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
    size=13,
)
ICON_COPY = svg(
    '<rect x="9" y="9" width="12" height="12"/><path d="M5 15H3V3h12v2"/>', size=13
)
ICON_CHECK = svg('<polyline points="20 6 9 17 4 12"/>', size=13, sw=2.5)
ICON_LOCK = svg(
    '<rect x="4" y="11" width="16" height="10"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    size=10, sw=2.2,
)
ICON_HOME = svg('<path d="M3 10l9-7 9 7v11a2 2 0 0 1-2 2h-4v-8h-6v8H5a2 2 0 0 1-2-2z"/>', size=15)
ICON_USER = svg(
    '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>', size=15
)
ICON_BELL = svg(
    '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>'
    '<path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    size=15,
)
ICON_GEAR = svg(
    '<circle cx="12" cy="12" r="3"/>'
    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    size=15,
)
ICON_LOGOUT = svg(
    '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
    '<polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    size=15,
)
ICON_PLUS = svg('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>', size=15)
ICON_LOGIN = svg(
    '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>'
    '<polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>',
    size=15,
)

FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' fill='%23101010'/%3E"
    "%3Cpath d='M14 16h36v24H28l-14 12V16z' fill='%23ffffff'/%3E"
    "%3C/svg%3E"
)


# ------------------------------------------------------------------
# CSS
# ------------------------------------------------------------------
CSS = """
:root, [data-theme="light"] {
  --bg:#ebebeb; --card:#ffffff; --line:#d4d4d4; --line-strong:#a8a8a8;
  --text:#101010; --muted:#767676; --hover:#f0f0f0;
  --accent:#101010; --accent-fg:#ffffff;
  --up:#1f9d55; --down:#d84343; --comment-bg:#f6f6f6; --private:#7a5cff;
  --danger:#d84343;
}
[data-theme="dark"] {
  --bg:#0a0a0a; --card:#141414; --line:#282828; --line-strong:#3a3a3a;
  --text:#ececec; --muted:#888888; --hover:#1e1e1e;
  --accent:#ececec; --accent-fg:#101010;
  --up:#2ecc71; --down:#e74c3c; --comment-bg:#1c1c1c; --private:#a08cff;
  --danger:#e74c3c;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg); color: var(--text); font-size: 14px;
  -webkit-font-smoothing: antialiased;
  user-select: none; -webkit-user-select: none; -ms-user-select: none;
  display: flex; align-items: center; justify-content: center;
  padding: 24px; min-height: 100vh;
}
input, textarea { user-select: text; -webkit-user-select: text; -ms-user-select: text; }

.layout {
  display: flex; width: 100%; max-width: 1000px;
  height: min(820px, calc(100vh - 48px));
  background: var(--card);
  border: 1px solid var(--line);
}

/* ===================== MAIN ===================== */
.main {
  flex: 1 1 auto; min-width: 0;
  display: flex; flex-direction: column;
  background: var(--card);
}
.main-header {
  flex: 0 0 auto; display: flex; align-items: center; gap: 6px;
  padding: 10px 12px; border-bottom: 1px solid var(--line);
}
.main-header .icon-btn { flex-shrink: 0; }
.main-header .title {
  flex: 1; font-size: 15px; font-weight: 600; padding: 0 4px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.main-body { flex: 1 1 auto; overflow-y: auto; }

/* ===================== SIDEBAR ===================== */
.sidebar {
  flex: 0 0 240px; width: 240px;
  border-left: 1px solid var(--line);
  background: var(--card);
  display: flex; flex-direction: column;
  padding: 16px 12px;
}
.sidebar .logo {
  font-size: 20px; font-weight: 700; letter-spacing: -0.5px;
  padding: 4px 12px 20px;
}
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-btn {
  display: flex; align-items: center; gap: 10px;
  width: 100%; padding: 9px 12px;
  border: none; background: transparent;
  color: var(--text); font: inherit; font-size: 14px;
  cursor: pointer; text-align: left;
  transition: background .12s;
}
.nav-btn:hover { background: var(--hover); }
.nav-btn.active { background: var(--hover); font-weight: 600; }
.nav-btn svg { flex-shrink: 0; color: var(--muted); }
.nav-btn.active svg { color: var(--text); }
.nav-btn .badge {
  margin-left: auto;
  background: var(--danger); color: #fff;
  font-size: 11px; font-weight: 700;
  padding: 1px 6px; min-width: 18px; text-align: center;
}
.sidebar .spacer { flex: 1; }
.sidebar-user {
  padding: 10px 12px; font-size: 13px; color: var(--muted);
  border-top: 1px solid var(--line);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* ===================== ICON BTN ===================== */
.icon-btn {
  width: 32px; height: 32px;
  display: inline-flex; align-items: center; justify-content: center;
  background: transparent; border: 1px solid var(--line);
  color: var(--text); cursor: pointer; padding: 0; text-decoration: none;
  transition: background .12s, border-color .12s;
  flex-shrink: 0;
}
.icon-btn:hover { background: var(--hover); border-color: var(--line-strong); }
.icon-btn svg { display: block; }

/* ===================== SEARCH & FILTERS ===================== */
header.search-header {
  flex: 0 0 auto; display: flex; align-items: center; gap: 6px;
  padding: 10px 12px; border-bottom: 1px solid var(--line);
}
header.search-header input[type="search"] {
  flex: 1; min-width: 0; padding: 0 12px; height: 32px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-size: 14px; font-family: inherit;
  outline: none; transition: border-color .12s;
}
header.search-header input[type="search"]:focus { border-color: var(--line-strong); }

.filters {
  flex: 0 0 auto; display: flex; align-items: center;
  padding: 6px 8px; border-bottom: 1px solid var(--line);
  overflow-x: auto; scrollbar-width: none;
}
.filters::-webkit-scrollbar { display: none; }
.filter-group { display: flex; align-items: center; gap: 1px; flex-shrink: 0; }
.filter-sep {
  width: 1px; height: 16px; background: var(--line); margin: 0 8px; flex-shrink: 0;
}
.filter {
  height: 26px; padding: 0 10px; border: none; background: transparent;
  color: var(--muted); font-family: inherit; font-size: 12px; font-weight: 500;
  cursor: pointer; white-space: nowrap; transition: color .12s, background .12s;
}
.filter:hover { background: var(--hover); color: var(--text); }
.filter.active { color: var(--text); background: var(--hover); }

/* ===================== POSTS ===================== */
.empty {
  padding: 80px 20px; text-align: center;
  color: var(--muted); font-size: 13px;
}
.post {
  padding: 14px 18px; border-bottom: 1px solid var(--line);
  background: var(--card);
}
.post-meta {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 6px; font-size: 12px;
}
.post-author {
  font-weight: 600; color: var(--text); text-decoration: none;
  font-size: 13px;
}
.post-author:hover { text-decoration: underline; }
.post-time { color: var(--muted); font-size: 12px; margin-left: auto; }
.post-text {
  font-size: 15px; line-height: 1.5; white-space: pre-wrap;
  word-wrap: break-word; overflow-wrap: anywhere; color: var(--text);
}
.read-more {
  display: inline-block; margin-top: 6px; color: var(--muted);
  text-decoration: none; font-size: 13px; border-bottom: 1px dashed currentColor;
}
.read-more:hover { color: var(--text); }
.post-actions {
  display: flex; align-items: center; gap: 1px;
  margin-top: 10px; font-size: 12px; color: var(--muted);
}
.vote-btn, .action-btn {
  display: inline-flex; align-items: center; gap: 5px;
  height: 26px; padding: 0 8px;
  background: transparent; border: none;
  color: var(--muted); cursor: pointer;
  font-family: inherit; font-size: 12px; font-weight: 500;
  transition: color .12s, background .12s;
}
.vote-btn:hover, .action-btn:hover { background: var(--hover); }
.vote-btn svg, .action-btn svg { display: block; }
.vote-btn.up:hover { color: var(--up); }
.vote-btn.down:hover { color: var(--down); }
.vote-btn.up.active { color: var(--up); }
.vote-btn.down.active { color: var(--down); }
.action-btn:hover { color: var(--text); }
.action-btn.copied { color: var(--up); }
.score {
  min-width: 18px; padding: 0 2px; text-align: center;
  font-weight: 600; font-size: 12px; color: var(--muted);
}
.score.up { color: var(--up); }
.score.down { color: var(--down); }
.private-badge {
  display: inline-flex; align-items: center; gap: 4px;
  margin-left: 8px; font-size: 11px; color: var(--private);
  border: 1px solid var(--private); padding: 1px 6px; height: 18px;
}
.private-badge svg { display: block; }

/* ===================== COMMENTS ===================== */
.comments { margin-top: 12px; border-top: 1px solid var(--line); }
.comment {
  padding: 10px 12px; margin-top: 8px;
  background: var(--comment-bg); border-left: 2px solid var(--line-strong);
}
.comment-meta {
  display: flex; align-items: center; gap: 8px; margin-bottom: 4px;
}
.comment-author {
  font-size: 12px; font-weight: 600; color: var(--text);
  text-decoration: none;
}
.comment-author:hover { text-decoration: underline; }
.comment-time { font-size: 11px; color: var(--muted); margin-left: auto; }
.comment-text {
  font-size: 13px; line-height: 1.5; white-space: pre-wrap;
  word-wrap: break-word; overflow-wrap: anywhere; color: var(--text);
}
.comment-actions {
  display: flex; align-items: center; gap: 1px;
  margin-top: 6px; font-size: 11px; color: var(--muted);
}
.comment-actions .vote-btn { height: 22px; padding: 0 6px; font-size: 11px; }
.comment-actions .score { font-size: 11px; min-width: 14px; }

/* ===================== FOOTER / COMPOSER ===================== */
.composer {
  flex: 0 0 auto; border-top: 1px solid var(--line);
  background: var(--card); padding: 10px 12px;
}
.composer textarea {
  display: block; width: 100%; min-height: 62px; padding: 10px 12px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-family: inherit; font-size: 14px; line-height: 1.45;
  outline: none; resize: none; transition: border-color .12s;
}
.composer textarea:focus { border-color: var(--line-strong); }
.composer textarea::placeholder { color: var(--muted); }
.composer textarea:disabled { opacity: .5; cursor: not-allowed; }
.composer-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; margin-top: 8px;
}
.composer-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.counter {
  font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
}
.counter.warn { color: var(--danger); }
.private-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--muted); cursor: pointer;
  user-select: none; padding: 4px 10px; border: 1px solid var(--line);
  transition: border-color .12s, color .12s;
}
.private-toggle:hover { border-color: var(--line-strong); color: var(--text); }
.private-toggle input { margin: 0; cursor: pointer; accent-color: var(--private); }
.private-toggle.checked { color: var(--private); border-color: var(--private); }

button.send {
  height: 32px; padding: 0 18px;
  background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent);
  font-family: inherit; font-size: 13px; font-weight: 500;
  cursor: pointer; transition: opacity .12s;
}
button.send:hover { opacity: .82; }
button.send:disabled { opacity: .28; cursor: default; }

.login-prompt {
  padding: 14px; text-align: center; color: var(--muted); font-size: 13px;
}
.login-prompt a { color: var(--text); }

/* ===================== AUTH FORMS ===================== */
.auth-wrap {
  max-width: 380px; margin: 0 auto; padding: 40px 20px;
}
.auth-title {
  font-size: 22px; font-weight: 700; margin: 0 0 24px;
  letter-spacing: -0.5px;
}
.auth-form { display: flex; flex-direction: column; gap: 10px; }
.auth-form input {
  width: 100%; padding: 11px 12px;
  border: 1px solid var(--line); background: transparent;
  color: var(--text); font-family: inherit; font-size: 14px;
  outline: none; transition: border-color .12s;
}
.auth-form input:focus { border-color: var(--line-strong); }
.auth-form button {
  margin-top: 6px; height: 40px;
  background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent);
  font-family: inherit; font-size: 14px; font-weight: 600;
  cursor: pointer; transition: opacity .12s;
}
.auth-form button:hover { opacity: .85; }
.auth-form button:disabled { opacity: .5; cursor: default; }
.auth-error {
  color: var(--danger); font-size: 13px; min-height: 18px;
}
.auth-switch {
  margin-top: 20px; font-size: 13px; color: var(--muted);
  text-align: center;
}
.auth-switch a { color: var(--text); cursor: pointer; text-decoration: underline; }

/* ===================== PROFILE ===================== */
.profile-header { padding: 24px 18px; border-bottom: 1px solid var(--line); }
.profile-nick { font-size: 22px; font-weight: 700; letter-spacing: -0.4px; }
.profile-name { color: var(--muted); font-size: 14px; margin-top: 4px; }
.profile-stats {
  display: flex; gap: 20px; margin-top: 14px; font-size: 13px; color: var(--muted);
}
.profile-stats b { color: var(--text); font-weight: 600; }
.profile-actions { margin-top: 16px; }
.follow-btn {
  height: 32px; padding: 0 20px;
  background: var(--accent); color: var(--accent-fg);
  border: 1px solid var(--accent);
  font-family: inherit; font-size: 13px; font-weight: 600;
  cursor: pointer; transition: opacity .12s;
}
.follow-btn:hover { opacity: .85; }
.follow-btn.following {
  background: transparent; color: var(--text); border-color: var(--line-strong);
}
.own-note { font-size: 13px; color: var(--muted); }

/* ===================== NOTIFICATIONS ===================== */
.notif-list { padding: 6px 0; }
.notif {
  padding: 12px 18px; border-bottom: 1px solid var(--line);
  font-size: 14px; line-height: 1.5;
  display: flex; flex-direction: column; gap: 4px;
}
.notif.unread { background: var(--hover); }
.notif-head { display: flex; gap: 8px; align-items: baseline; }
.notif-author {
  font-weight: 600; color: var(--text); text-decoration: none;
}
.notif-author:hover { text-decoration: underline; }
.notif-text { color: var(--muted); font-size: 13px; }
.notif-snippet {
  margin-top: 4px; padding: 8px 10px;
  background: var(--comment-bg); border-left: 2px solid var(--line-strong);
  font-size: 13px; color: var(--text);
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
}
.notif-time { font-size: 11px; color: var(--muted); }

/* ===================== SETTINGS ===================== */
.settings { padding: 24px 18px; }
.settings h2 {
  font-size: 13px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .5px; color: var(--muted);
  margin: 0 0 12px;
}
.settings-section { margin-bottom: 28px; }
.opt-row { display: flex; gap: 6px; flex-wrap: wrap; }
.opt {
  height: 32px; padding: 0 16px;
  background: transparent; color: var(--text);
  border: 1px solid var(--line);
  font-family: inherit; font-size: 13px; cursor: pointer;
  transition: border-color .12s, background .12s;
}
.opt:hover { background: var(--hover); }
.opt.active { background: var(--hover); border-color: var(--line-strong); font-weight: 600; }
"""


# ------------------------------------------------------------------
# JS
# ------------------------------------------------------------------
JS = r"""
var ICONS = {
  up: __ICON_UP__, down: __ICON_DOWN__, comment: __ICON_COMMENT__,
  copy: __ICON_COPY__, check: __ICON_CHECK__, lock: __ICON_LOCK__,
  moon: __ICON_MOON__, sun: __ICON_SUN__, back: __ICON_BACK__,
  home: __ICON_HOME__, user: __ICON_USER__, bell: __ICON_BELL__,
  gear: __ICON_GEAR__, logout: __ICON_LOGOUT__, plus: __ICON_PLUS__,
  login: __ICON_LOGIN__
};

var state = {
  user: null,
  token: localStorage.getItem('sldchat_token') || null,
  view: VIEW,
  viewData: VIEW_DATA || {},
  unread: 0,
  sortMode: 'new',
  commentFilter: 'any',
  searchQuery: '',
  composerDraft: '',
  composerPrivate: false,
  lastNotifyCount: 0
};

var feedRefreshTimer = null;

// ================= HTTP helper =================
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
    state.user = null;
    state.token = null;
    localStorage.removeItem('sldchat_token');
    throw new Error('unauthorized');
  }
  if (!r.ok) {
    var err = {};
    try { err = await r.json(); } catch(e) {}
    var msg = err.detail || 'error';
    throw new Error(msg);
  }
  return r.json();
}

// ================= Utils =================
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function tr(key) { return T[key] || key; }
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
function scoreClass(up, down) {
  var s = up - down; if (s > 0) return 'up'; if (s < 0) return 'down'; return '';
}
function truncateText(text) {
  var lines = text.split('\n');
  var out = text, truncated = false;
  if (lines.length > TRUNCATE_LINES) {
    out = lines.slice(0, TRUNCATE_LINES).join('\n'); truncated = true;
  }
  if (out.length > TRUNCATE_CHARS) {
    out = out.slice(0, TRUNCATE_CHARS); truncated = true;
  }
  if (truncated) out = out.replace(/\s+$/, '') + '…';
  return { text: out, truncated: truncated };
}

// ================= Theme =================
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('sldchat_theme', theme);
  var el = document.getElementById('mainThemeBtn');
  if (el) el.innerHTML = (theme === 'dark') ? ICONS.sun : ICONS.moon;
}
function toggleTheme() {
  var cur = document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}
applyTheme(localStorage.getItem('sldchat_theme') || 'light');

// ================= Navigation =================
function navigate(url) {
  history.pushState({}, '', url);
  handleRoute();
}
function handleRoute() {
  var path = location.pathname;
  var m;
  if (path === '/' || path === '') {
    state.view = 'feed'; state.viewData = {};
  } else if ((m = path.match(/^\/p\/([a-z0-9]+)$/))) {
    state.view = 'post'; state.viewData = { post_id: m[1] };
  } else if ((m = path.match(/^\/u\/(.+)$/))) {
    state.view = 'profile'; state.viewData = { nick: decodeURIComponent(m[1]) };
  } else if (path === '/notifications') {
    state.view = 'notifications'; state.viewData = {};
  } else if (path === '/settings') {
    state.view = 'settings'; state.viewData = {};
  } else if (path === '/register') {
    state.view = 'register'; state.viewData = {};
  } else if (path === '/login') {
    state.view = 'login'; state.viewData = {};
  } else {
    state.view = 'feed'; state.viewData = {};
  }
  renderSidebar();
  renderMain();
}
window.addEventListener('popstate', handleRoute);

// ================= Auth =================
async function loadMe() {
  if (!state.token) return;
  try {
    state.user = await api('/api/me');
  } catch(e) {
    state.user = null;
  }
}
async function doRegister(data) {
  var res = await api('/api/register', { method: 'POST', body: data });
  state.token = res.token;
  localStorage.setItem('sldchat_token', res.token);
  state.user = res.user;
  navigate('/');
}
async function doLogin(data) {
  var res = await api('/api/login', { method: 'POST', body: data });
  state.token = res.token;
  localStorage.setItem('sldchat_token', res.token);
  state.user = res.user;
  navigate('/');
}
async function doLogout() {
  try { await api('/api/logout', { method: 'POST' }); } catch(e) {}
  state.user = null;
  state.token = null;
  localStorage.removeItem('sldchat_token');
  navigate('/');
}

// ================= Sidebar =================
function navBtn(icon, label, active, onClick, badge) {
  var cls = 'nav-btn' + (active ? ' active' : '');
  var badgeHtml = (badge && badge > 0) ? '<span class="badge">' + badge + '</span>' : '';
  return '<button class="' + cls + '" data-nav="' + onClick + '">'
    + icon + '<span>' + label + '</span>' + badgeHtml + '</button>';
}
function renderSidebar() {
  var el = document.getElementById('sidebar');
  var isPost = state.view === 'post';
  var goHome = function() { navigate('/'); };

  var html = '<div class="logo">sldchat</div>';
  html += '<div class="nav">';
  html += navBtn(ICONS.home, tr('nav_home'), state.view === 'feed', 'home');

  if (state.user) {
    html += navBtn(ICONS.user, tr('nav_profile'), state.view === 'profile' && state.viewData.nick === state.user.nick, 'profile');
    html += navBtn(ICONS.bell, tr('nav_notifications'), state.view === 'notifications', 'notifications', state.unread);
    html += navBtn(ICONS.gear, tr('nav_settings'), state.view === 'settings', 'settings');
    html += navBtn(ICONS.logout, tr('nav_logout'), false, 'logout');
  } else {
    html += navBtn(ICONS.gear, tr('nav_settings'), state.view === 'settings', 'settings');
    html += navBtn(ICONS.plus, tr('nav_register'), state.view === 'register', 'register');
    html += navBtn(ICONS.login, tr('nav_login'), state.view === 'login', 'login');
  }
  html += '</div><div class="spacer"></div>';
  if (state.user) {
    html += '<div class="sidebar-user">@' + escapeHtml(state.user.nick) + '</div>';
  }
  el.innerHTML = html;

  el.querySelectorAll('[data-nav]').forEach(function(b){
    b.addEventListener('click', function(){
      var nav = b.dataset.nav;
      if (nav === 'home') navigate('/');
      else if (nav === 'profile') navigate('/u/' + encodeURIComponent(state.user.nick));
      else if (nav === 'notifications') navigate('/notifications');
      else if (nav === 'settings') navigate('/settings');
      else if (nav === 'register') navigate('/register');
      else if (nav === 'login') navigate('/login');
      else if (nav === 'logout') doLogout();
    });
  });
}

// ================= Main dispatcher =================
function renderMain() {
  var el = document.getElementById('main');
  if (state.view === 'feed') renderFeedView(el);
  else if (state.view === 'post') renderPostView(el);
  else if (state.view === 'profile') renderProfileView(el);
  else if (state.view === 'notifications') renderNotificationsView(el);
  else if (state.view === 'settings') renderSettingsView(el);
  else if (state.view === 'register') renderRegisterView(el);
  else if (state.view === 'login') renderLoginView(el);
  else renderFeedView(el);
}

function attachThemeBtn() {
  var b = document.getElementById('mainThemeBtn');
  if (b) {
    b.innerHTML = (document.documentElement.getAttribute('data-theme') === 'dark') ? ICONS.sun : ICONS.moon;
    b.addEventListener('click', toggleTheme);
  }
}

// ================= Feed view =================
function renderFeedView(el) {
  var html = '';
  html += '<header class="search-header">';
  html += '<input id="search" type="search" placeholder="' + escapeHtml(tr('search_ph')) + '" autocomplete="off" spellcheck="false" value="' + escapeHtml(state.searchQuery) + '" />';
  html += '<button class="icon-btn" id="searchBtn" title="' + escapeHtml(tr('search')) + '">' + ICONS.moon.replace(ICONS.moon, ICONS.moon) + '</button>';
  // костыль: searchBtn должен быть лупа
  html = html.replace(/<button class="icon-btn" id="searchBtn"[^>]*>.*?<\/button>/, '<button class="icon-btn" id="searchBtn" title="' + escapeHtml(tr('search')) + '">' + ICONS.search_loupe + '</button>');
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';

  html += '<div class="filters" id="filters">';
  html += '<div class="filter-group">';
  html += '<button class="filter' + (state.sortMode==='new'?' active':'') + '" data-sort="new">' + tr('f_new') + '</button>';
  html += '<button class="filter' + (state.sortMode==='top'?' active':'') + '" data-sort="top">' + tr('f_top') + '</button>';
  html += '<button class="filter' + (state.sortMode==='bottom'?' active':'') + '" data-sort="bottom">' + tr('f_bottom') + '</button>';
  html += '<button class="filter' + (state.sortMode==='old'?' active':'') + '" data-sort="old">' + tr('f_old') + '</button>';
  html += '</div><div class="filter-sep"></div><div class="filter-group">';
  html += '<button class="filter' + (state.commentFilter==='any'?' active':'') + '" data-comments="any">' + tr('f_all') + '</button>';
  html += '<button class="filter' + (state.commentFilter==='many'?' active':'') + '" data-comments="many">' + tr('f_many') + '</button>';
  html += '<button class="filter' + (state.commentFilter==='some'?' active':'') + '" data-comments="some">' + tr('f_some') + '</button>';
  html += '<button class="filter' + (state.commentFilter==='none'?' active':'') + '" data-comments="none">' + tr('f_none') + '</button>';
  html += '</div></div>';

  html += '<div class="main-body"><div id="feed"><div class="empty">…</div></div></div>';

  if (state.user) {
    html += '<div class="composer">';
    html += '<textarea id="newPost" maxlength="' + MAX_POST_LEN + '" placeholder="' + escapeHtml(tr('post_ph')) + '"></textarea>';
    html += '<div class="composer-row">';
    html += '<div class="composer-left">';
    html += '<div id="counter" class="counter">0 / ' + MAX_POST_LEN + '</div>';
    html += '<label class="private-toggle" id="privateLabel"><input type="checkbox" id="privateCheck" /> <span>' + tr('private') + '</span></label>';
    html += '</div>';
    html += '<button id="send" class="send" disabled>' + tr('publish') + '</button>';
    html += '</div></div>';
  } else {
    html += '<div class="composer"><div class="login-prompt">'
      + escapeHtml(tr('login_to_post')) + ' · <a href="/login" data-link>' + tr('go_login') + '</a> · <a href="/register" data-link>' + tr('go_register') + '</a>'
      + '</div></div>';
  }

  el.innerHTML = html;
  attachThemeBtn();

  // filters
  var filtersEl = document.getElementById('filters');
  filtersEl.addEventListener('click', function(e){
    var b = e.target.closest('.filter');
    if (!b) return;
    if (b.dataset.sort) {
      state.sortMode = b.dataset.sort;
      filtersEl.querySelectorAll('[data-sort]').forEach(function(x){ x.classList.toggle('active', x === b); });
    } else if (b.dataset.comments) {
      state.commentFilter = b.dataset.comments;
      filtersEl.querySelectorAll('[data-comments]').forEach(function(x){ x.classList.toggle('active', x === b); });
    }
    loadFeed();
  });

  // search
  var searchEl = document.getElementById('search');
  var searchBtn = document.getElementById('searchBtn');
  var tId;
  searchEl.addEventListener('input', function(){
    state.searchQuery = searchEl.value;
    clearTimeout(tId);
    tId = setTimeout(loadFeed, 250);
  });
  searchEl.addEventListener('keydown', function(e){
    if (e.key === 'Enter') { e.preventDefault(); loadFeed(); }
  });
  searchBtn.addEventListener('click', function(e){ e.preventDefault(); loadFeed(); });

  // composer
  if (state.user) {
    var inputEl = document.getElementById('newPost');
    var sendBtn = document.getElementById('send');
    var counter = document.getElementById('counter');
    var privateCheck = document.getElementById('privateCheck');
    var privateLabel = document.getElementById('privateLabel');

    inputEl.value = state.composerDraft || '';
    privateCheck.checked = !!state.composerPrivate;
    function updatePriv(){ privateLabel.classList.toggle('checked', privateCheck.checked); state.composerPrivate = privateCheck.checked; }
    privateCheck.addEventListener('change', updatePriv);
    updatePriv();

    function updateCounter(){
      var len = inputEl.value.length;
      counter.textContent = len + ' / ' + MAX_POST_LEN;
      counter.classList.toggle('warn', len >= MAX_POST_LEN);
      sendBtn.disabled = len === 0 || len > MAX_POST_LEN;
    }
    inputEl.addEventListener('input', function(){ state.composerDraft = inputEl.value; updateCounter(); });
    inputEl.addEventListener('keydown', function(e){
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendPost(); }
    });
    sendBtn.addEventListener('click', sendPost);
    updateCounter();

    async function sendPost(){
      var text = inputEl.value.trim();
      if (!text) return;
      sendBtn.disabled = true;
      try {
        var created = await api('/api/posts', { method: 'POST', body: { text: text, private: privateCheck.checked } });
        inputEl.value = '';
        state.composerDraft = '';
        privateCheck.checked = false;
        updatePriv();
        updateCounter();
        if (created.private) {
          navigate('/p/' + created.id);
          return;
        }
        state.searchQuery = '';
        await loadFeed();
      } catch(e) {
        alert(tr(e.message) || e.message);
      } finally {
        updateCounter();
      }
    }
  }

  document.querySelectorAll('[data-link]').forEach(function(a){
    a.addEventListener('click', function(e){
      e.preventDefault();
      navigate(a.getAttribute('href'));
    });
  });

  loadFeed();
}

async function loadFeed() {
  var feedEl = document.getElementById('feed');
  if (!feedEl) return;
  try {
    var q = state.searchQuery.trim();
    var url = '/api/posts?q=' + encodeURIComponent(q);
    var data = await api(url);
    var posts = applyFilters(data.posts || []);
    if (!posts.length) {
      feedEl.innerHTML = '<div class="empty">' + escapeHtml(tr('no_posts')) + '</div>';
      return;
    }
    feedEl.innerHTML = posts.map(function(p){ return renderPostHtml(p, false); }).join('');
    bindPostActions(feedEl);
  } catch(e) {
    feedEl.innerHTML = '<div class="empty">—</div>';
  }
}

function applyFilters(posts) {
  if (state.commentFilter === 'some') posts = posts.filter(function(p){ return p.comments.length > 0; });
  else if (state.commentFilter === 'many') posts = posts.filter(function(p){ return p.comments.length >= 3; });
  else if (state.commentFilter === 'none') posts = posts.filter(function(p){ return p.comments.length === 0; });

  posts.sort(function(a, b){
    var sa = a.upvotes - a.downvotes, sb = b.upvotes - b.downvotes;
    if (state.sortMode === 'new') return b.created_at - a.created_at;
    if (state.sortMode === 'old') return a.created_at - b.created_at;
    if (state.sortMode === 'top') return (sb - sa) || (b.created_at - a.created_at);
    if (state.sortMode === 'bottom') return (sa - sb) || (b.created_at - a.created_at);
    return 0;
  });
  return posts;
}

// ================= Post view =================
function renderPostView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title"></div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="feed"><div class="empty">…</div></div></div>';

  if (state.user) {
    html += '<div class="composer">';
    html += '<textarea id="newComment" maxlength="' + MAX_COMMENT_LEN + '" placeholder="' + escapeHtml(tr('comment_ph')) + '"></textarea>';
    html += '<div class="composer-row">';
    html += '<div class="composer-left"><div id="counter" class="counter">0 / ' + MAX_COMMENT_LEN + '</div></div>';
    html += '<button id="send" class="send" disabled>' + tr('send_comment') + '</button>';
    html += '</div></div>';
  } else {
    html += '<div class="composer"><div class="login-prompt">'
      + escapeHtml(tr('login_to_comment')) + ' · <a href="/login" data-link>' + tr('go_login') + '</a>'
      + '</div></div>';
  }

  el.innerHTML = html;
  attachThemeBtn();

  document.querySelectorAll('[data-link]').forEach(function(a){
    a.addEventListener('click', function(e){
      e.preventDefault();
      navigate(a.getAttribute('href'));
    });
  });

  if (state.user) {
    var inputEl = document.getElementById('newComment');
    var sendBtn = document.getElementById('send');
    var counter = document.getElementById('counter');
    function updateCounter(){
      var len = inputEl.value.length;
      counter.textContent = len + ' / ' + MAX_COMMENT_LEN;
      counter.classList.toggle('warn', len >= MAX_COMMENT_LEN);
      sendBtn.disabled = len === 0 || len > MAX_COMMENT_LEN;
    }
    inputEl.addEventListener('input', updateCounter);
    inputEl.addEventListener('keydown', function(e){
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendComment(); }
    });
    sendBtn.addEventListener('click', sendComment);
    updateCounter();

    async function sendComment(){
      var text = inputEl.value.trim();
      if (!text) return;
      sendBtn.disabled = true;
      try {
        await api('/api/posts/' + state.viewData.post_id + '/comments', {
          method: 'POST', body: { text: text }
        });
        inputEl.value = '';
        updateCounter();
        await loadPostView();
      } catch(e) {
        alert(tr(e.message) || e.message);
      } finally {
        updateCounter();
      }
    }
  }

  loadPostView();
}

async function loadPostView() {
  var feedEl = document.getElementById('feed');
  if (!feedEl) return;
  try {
    var p = await api('/api/posts/' + state.viewData.post_id);
    feedEl.innerHTML = renderPostHtml(p, true);
    bindPostActions(feedEl);
  } catch(e) {
    feedEl.innerHTML = '<div class="empty">' + escapeHtml(tr('not_found')) + '</div>';
  }
}

// ================= Profile view =================
function renderProfileView(el) {
  var nick = state.viewData.nick || '';
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">@' + escapeHtml(nick) + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="profileHeader"></div><div id="feed"><div class="empty">…</div></div></div>';
  el.innerHTML = html;
  attachThemeBtn();

  document.querySelectorAll('[data-link]').forEach(function(a){
    a.addEventListener('click', function(e){
      e.preventDefault();
      navigate(a.getAttribute('href'));
    });
  });

  loadProfile(nick);
}

async function loadProfile(nick) {
  var headerEl = document.getElementById('profileHeader');
  var feedEl = document.getElementById('feed');
  try {
    var u = await api('/api/users/' + encodeURIComponent(nick));
    var isMe = state.user && state.user.nick === u.nick;

    var h = '<div class="profile-header">';
    h += '<div class="profile-nick">@' + escapeHtml(u.nick) + '</div>';
    h += '<div class="profile-name">' + escapeHtml(u.name) + '</div>';
    h += '<div class="profile-stats">';
    h += '<span><b>' + u.followers + '</b> ' + tr('profile_followers') + '</span>';
    h += '<span><b>' + u.following + '</b> ' + tr('profile_following') + '</span>';
    h += '</div>';
    h += '<div class="profile-actions">';
    if (isMe) {
      h += '<span class="own-note">' + tr('own_profile') + '</span>';
    } else if (state.user) {
      h += '<button class="follow-btn' + (u.is_following ? ' following' : '') + '" id="followBtn">'
        + (u.is_following ? tr('unfollow') : tr('follow')) + '</button>';
    } else {
      h += '<a class="follow-btn" href="/login" data-link style="display:inline-flex;align-items:center;justify-content:center;text-decoration:none">' + tr('go_login') + '</a>';
    }
    h += '</div></div>';
    headerEl.innerHTML = h;

    document.querySelectorAll('[data-link]').forEach(function(a){
      a.addEventListener('click', function(e){
        e.preventDefault();
        navigate(a.getAttribute('href'));
      });
    });

    var btn = document.getElementById('followBtn');
    if (btn) {
      btn.addEventListener('click', async function(){
        try {
          if (btn.classList.contains('following')) {
            await api('/api/users/' + encodeURIComponent(u.nick) + '/unfollow', { method: 'POST' });
          } else {
            await api('/api/users/' + encodeURIComponent(u.nick) + '/follow', { method: 'POST' });
          }
          loadProfile(nick);
        } catch(e) {
          alert(tr(e.message) || e.message);
        }
      });
    }

    var data = await api('/api/posts?author=' + encodeURIComponent(u.nick));
    var posts = data.posts || [];
    posts.sort(function(a, b){ return b.created_at - a.created_at; });
    if (!posts.length) {
      feedEl.innerHTML = '<div class="empty">' + escapeHtml(tr('no_user_posts')) + '</div>';
    } else {
      feedEl.innerHTML = posts.map(function(p){ return renderPostHtml(p, false); }).join('');
      bindPostActions(feedEl);
    }
  } catch(e) {
    headerEl.innerHTML = '<div class="empty">' + escapeHtml(tr('not_found')) + '</div>';
    feedEl.innerHTML = '';
  }
}

// ================= Notifications view =================
function renderNotificationsView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('notif_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div id="notifList"><div class="empty">…</div></div></div>';
  el.innerHTML = html;
  attachThemeBtn();

  document.querySelectorAll('[data-link]').forEach(function(a){
    a.addEventListener('click', function(e){
      e.preventDefault();
      navigate(a.getAttribute('href'));
    });
  });

  loadNotifications();
}

async function loadNotifications() {
  var wrap = document.getElementById('notifList');
  if (!wrap) return;
  try {
    var data = await api('/api/notifications');
    var items = data.items || [];
    // пометим прочитанными
    if (data.unread > 0) {
      api('/api/notifications/read', { method: 'POST' }).catch(function(){});
      state.unread = 0;
      renderSidebar();
    }
    if (!items.length) {
      wrap.innerHTML = '<div class="empty">' + escapeHtml(tr('notif_empty')) + '</div>';
      return;
    }
    wrap.innerHTML = '<div class="notif-list">' + items.map(renderNotifHtml).join('') + '</div>';
    wrap.querySelectorAll('[data-link]').forEach(function(a){
      a.addEventListener('click', function(e){
        e.preventDefault();
        navigate(a.getAttribute('href'));
      });
    });
  } catch(e) {
    wrap.innerHTML = '<div class="empty">—</div>';
  }
}

function renderNotifHtml(n) {
  var cls = 'notif' + (n.read ? '' : ' unread');
  var author = '<a class="notif-author" href="/u/' + encodeURIComponent(n.from_nick) + '" data-link>@' + escapeHtml(n.from_nick) + '</a>';
  var text = '';
  var link = null;
  if (n.type === 'follow') {
    text = '<div class="notif-text">' + author + ' ' + tr('notif_follow') + '</div>';
    link = '/u/' + encodeURIComponent(n.from_nick);
  } else if (n.type === 'comment') {
    text = '<div class="notif-head">' + author + '<span class="notif-text">' + tr('notif_comment') + '</span></div>';
    if (n.text) text += '<div class="notif-snippet">' + escapeHtml(n.text) + '</div>';
    link = n.post_id ? ('/p/' + n.post_id) : null;
  } else {
    text = '<div class="notif-text">' + author + '</div>';
  }
  var wrapOpen = '', wrapClose = '';
  if (link) {
    wrapOpen = '<a href="' + link + '" data-link style="text-decoration:none;color:inherit;display:block">';
    wrapClose = '</a>';
  }
  return '<div class="' + cls + '">'
    + wrapOpen
    + text
    + '<div class="notif-time">' + timeAgo(n.created_at) + '</div>'
    + wrapClose
    + '</div>';
}

// ================= Settings view =================
function renderSettingsView(el) {
  var theme = document.documentElement.getAttribute('data-theme') || 'light';
  var lang = LANG;
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('settings_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div class="settings">';
  html += '<div class="settings-section"><h2>' + tr('settings_theme') + '</h2><div class="opt-row">';
  html += '<button class="opt' + (theme==='light'?' active':'') + '" data-set-theme="light">' + tr('theme_light') + '</button>';
  html += '<button class="opt' + (theme==='dark'?' active':'') + '" data-set-theme="dark">' + tr('theme_dark') + '</button>';
  html += '</div></div>';
  html += '<div class="settings-section"><h2>' + tr('settings_lang') + '</h2><div class="opt-row">';
  html += '<button class="opt' + (lang==='ru'?' active':'') + '" data-set-lang="ru">Русский</button>';
  html += '<button class="opt' + (lang==='en'?' active':'') + '" data-set-lang="en">English</button>';
  html += '</div></div>';
  html += '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();

  document.querySelectorAll('[data-link]').forEach(function(a){
    a.addEventListener('click', function(e){
      e.preventDefault();
      navigate(a.getAttribute('href'));
    });
  });

  el.querySelectorAll('[data-set-theme]').forEach(function(b){
    b.addEventListener('click', function(){
      applyTheme(b.dataset.setTheme);
      renderSettingsView(el);
    });
  });
  el.querySelectorAll('[data-set-lang]').forEach(function(b){
    b.addEventListener('click', function(){
      document.cookie = 'sldchat_lang=' + b.dataset.setLang + '; path=/; max-age=' + (60*60*24*365);
      location.reload();
    });
  });
}

// ================= Register view =================
function renderRegisterView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('reg_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div class="auth-wrap">';
  html += '<h1 class="auth-title">' + tr('reg_title') + '</h1>';
  html += '<form class="auth-form" id="regForm">';
  html += '<input type="text" name="name" placeholder="' + escapeHtml(tr('name_ph')) + '" maxlength="50" required />';
  html += '<input type="text" name="nick" placeholder="' + escapeHtml(tr('nick_ph')) + '" maxlength="20" required />';
  html += '<input type="password" name="password" placeholder="' + escapeHtml(tr('pass_ph')) + '" required />';
  html += '<input type="password" name="password_confirm" placeholder="' + escapeHtml(tr('pass2_ph')) + '" required />';
  html += '<div class="auth-error" id="regError"></div>';
  html += '<button type="submit">' + tr('reg_btn') + '</button>';
  html += '</form>';
  html += '<div class="auth-switch"><a id="toLogin">' + tr('to_login') + '</a></div>';
  html += '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();

  document.querySelectorAll('[data-link]').forEach(function(a){
    a.addEventListener('click', function(e){
      e.preventDefault();
      navigate(a.getAttribute('href'));
    });
  });
  document.getElementById('toLogin').addEventListener('click', function(){ navigate('/login'); });

  var form = document.getElementById('regForm');
  var errEl = document.getElementById('regError');
  form.addEventListener('submit', async function(e){
    e.preventDefault();
    errEl.textContent = '';
    var fd = new FormData(form);
    var data = {
      name: fd.get('name'),
      nick: fd.get('nick'),
      password: fd.get('password'),
      password_confirm: fd.get('password_confirm')
    };
    try {
      await doRegister(data);
    } catch(err) {
      errEl.textContent = tr(err.message) || err.message;
    }
  });
}

// ================= Login view =================
function renderLoginView(el) {
  var html = '';
  html += '<header class="main-header">';
  html += '<a href="/" class="icon-btn" data-link title="' + escapeHtml(tr('back_to_main')) + '">' + ICONS.back + '</a>';
  html += '<div class="title">' + tr('log_title') + '</div>';
  html += '<button class="icon-btn" id="mainThemeBtn" title="' + escapeHtml(tr('theme')) + '">' + ICONS.moon + '</button>';
  html += '</header>';
  html += '<div class="main-body"><div class="auth-wrap">';
  html += '<h1 class="auth-title">' + tr('log_title') + '</h1>';
  html += '<form class="auth-form" id="logForm">';
  html += '<input type="text" name="nick" placeholder="' + escapeHtml(tr('nick_ph')) + '" required />';
  html += '<input type="password" name="password" placeholder="' + escapeHtml(tr('pass_ph')) + '" required />';
  html += '<div class="auth-error" id="logError"></div>';
  html += '<button type="submit">' + tr('log_btn') + '</button>';
  html += '</form>';
  html += '<div class="auth-switch"><a id="toReg">' + tr('to_reg') + '</a></div>';
  html += '</div></div>';
  el.innerHTML = html;
  attachThemeBtn();

  document.querySelectorAll('[data-link]').forEach(function(a){
    a.addEventListener('click', function(e){
      e.preventDefault();
      navigate(a.getAttribute('href'));
    });
  });
  document.getElementById('toReg').addEventListener('click', function(){ navigate('/register'); });

  var form = document.getElementById('logForm');
  var errEl = document.getElementById('logError');
  form.addEventListener('submit', async function(e){
    e.preventDefault();
    errEl.textContent = '';
    var fd = new FormData(form);
    try {
      await doLogin({ nick: fd.get('nick'), password: fd.get('password') });
    } catch(err) {
      errEl.textContent = tr(err.message) || err.message;
    }
  });
}

// ================= Post HTML + actions =================
function renderPostHtml(p, showComments) {
  var score = p.upvotes - p.downvotes;
  var upCls = p.user_vote === 1 ? 'active' : '';
  var downCls = p.user_vote === -1 ? 'active' : '';
  var scCls = scoreClass(p.upvotes, p.downvotes);

  var displayText = p.text;
  var truncated = false;
  if (!showComments) {
    var res = truncateText(p.text);
    displayText = res.text;
    truncated = res.truncated;
  }
  var readMore = truncated
    ? '<a class="read-more" href="/p/' + p.id + '" data-link>… ' + escapeHtml(tr('read_more')) + '</a>'
    : '';

  var authorLink = p.author
    ? '<a class="post-author" href="/u/' + encodeURIComponent(p.author) + '" data-link>@' + escapeHtml(p.author) + '</a>'
    : '';

  var commentBtn = '<button class="action-btn" data-action="comment" data-post-id="' + p.id + '">'
    + ICONS.comment + '<span>' + (p.comments ? p.comments.length : 0) + '</span></button>';

  var copyBtn = '<button class="action-btn" data-action="copy" data-post-id="' + p.id + '" title="' + escapeHtml(tr('copy')) + '">'
    + ICONS.copy + '</button>';

  var privateBadge = p.private
    ? '<span class="private-badge" title="' + escapeHtml(tr('private')) + '">' + ICONS.lock + '</span>'
    : '';

  var commentsHtml = '';
  if (showComments && p.comments && p.comments.length) {
    commentsHtml = '<div class="comments">'
      + p.comments.map(function(c){ return renderCommentHtml(c, p.id); }).join('')
      + '</div>';
  }

  return ''
    + '<div class="post" data-post-id="' + p.id + '">'
    +   '<div class="post-meta">'
    +     authorLink
    +     '<span class="post-time">' + timeAgo(p.created_at) + '</span>'
    +   '</div>'
    +   '<div class="post-text">' + escapeHtml(displayText) + '</div>'
    +   readMore
    +   '<div class="post-actions">'
    +     '<button class="vote-btn up ' + upCls + '" data-action="vote" data-post-id="' + p.id + '" data-dir="1">' + ICONS.up + '</button>'
    +     '<span class="score ' + scCls + '">' + score + '</span>'
    +     '<button class="vote-btn down ' + downCls + '" data-action="vote" data-post-id="' + p.id + '" data-dir="-1">' + ICONS.down + '</button>'
    +     commentBtn
    +     copyBtn
    +     privateBadge
    +   '</div>'
    +   commentsHtml
    + '</div>';
}

function renderCommentHtml(c, postId) {
  var score = c.upvotes - c.downvotes;
  var upCls = c.user_vote === 1 ? 'active' : '';
  var downCls = c.user_vote === -1 ? 'active' : '';
  var scCls = scoreClass(c.upvotes, c.downvotes);
  var author = c.author
    ? '<a class="comment-author" href="/u/' + encodeURIComponent(c.author) + '" data-link>@' + escapeHtml(c.author) + '</a>'
    : '';
  return ''
    + '<div class="comment" data-comment-id="' + c.id + '">'
    +   '<div class="comment-meta">' + author + '<span class="comment-time">' + timeAgo(c.created_at) + '</span></div>'
    +   '<div class="comment-text">' + escapeHtml(c.text) + '</div>'
    +   '<div class="comment-actions">'
    +     '<button class="vote-btn up ' + upCls + '" data-action="vote-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-dir="1">' + ICONS.up + '</button>'
    +     '<span class="score ' + scCls + '">' + score + '</span>'
    +     '<button class="vote-btn down ' + downCls + '" data-action="vote-comment" data-post-id="' + postId + '" data-comment-id="' + c.id + '" data-dir="-1">' + ICONS.down + '</button>'
    +   '</div>'
    + '</div>';
}

function bindPostActions(root) {
  root.querySelectorAll('[data-action]').forEach(function(btn){
    btn.addEventListener('click', async function(e){
      e.preventDefault();
      var action = btn.dataset.action;
      var postId = btn.dataset.postId;
      var commentId = btn.dataset.commentId;
      var dir = parseInt(btn.dataset.dir || '0', 10);
      try {
        if (action === 'vote') {
          await api('/api/posts/' + postId + '/vote', { method: 'POST', body: { direction: dir } });
          refreshCurrentView();
        } else if (action === 'vote-comment') {
          await api('/api/posts/' + postId + '/comments/' + commentId + '/vote', { method: 'POST', body: { direction: dir } });
          refreshCurrentView();
        } else if (action === 'comment') {
          navigate('/p/' + postId);
        } else if (action === 'copy') {
          await copyPost(postId, btn);
        }
      } catch(err) {
        alert(tr(err.message) || err.message);
      }
    });
  });
  // data-link внутри постов
  root.querySelectorAll('[data-link]').forEach(function(a){
    if (a.dataset.linkBound) return;
    a.dataset.linkBound = '1';
    a.addEventListener('click', function(e){
      e.preventDefault();
      navigate(a.getAttribute('href'));
    });
  });
}

async function refreshCurrentView() {
  if (state.view === 'feed') await loadFeed();
  else if (state.view === 'post') await loadPostView();
  else if (state.view === 'profile') await loadProfile(state.viewData.nick);
}

async function copyPost(postId, btn) {
  try {
    var p = await api('/api/posts/' + postId);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(p.text);
    } else {
      var ta = document.createElement('textarea');
      ta.value = p.text; ta.style.position='fixed'; ta.style.opacity='0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); document.body.removeChild(ta);
    }
    btn.innerHTML = ICONS.check;
    btn.classList.add('copied');
    setTimeout(function(){
      btn.innerHTML = ICONS.copy;
      btn.classList.remove('copied');
    }, 1200);
  } catch(e) { alert('Copy failed'); }
}

// ================= Notifications polling =================
async function pollNotifications() {
  if (!state.user) return;
  try {
    var data = await api('/api/notifications');
    var newUnread = data.unread;
    var changed = (newUnread !== state.unread);
    state.unread = newUnread;
    if (changed) renderSidebar();
  } catch(e) {}
}

// ================= Init =================
(async function init() {
  // подгружаем пользователя если есть токен
  await loadMe();

  // редирект со страниц логина/регистрации, если уже вошли
  if (state.user && (state.view === 'login' || state.view === 'register')) {
    history.replaceState({}, '', '/');
    state.view = 'feed'; state.viewData = {};
  }

  renderSidebar();
  renderMain();

  if (state.user) {
    pollNotifications();
    setInterval(pollNotifications, 15000);
  }

  // автообновление текущего вида раз в 5 сек
  setInterval(function(){
    if (document.hidden) return;
    if (state.view === 'feed') loadFeed();
    else if (state.view === 'post') loadPostView();
  }, 5000);
})();
"""


# ------------------------------------------------------------------
# Рендер страницы
# ------------------------------------------------------------------
def render_page(lang: str, view: str, view_data: Optional[dict] = None) -> str:
    t = TEXTS[lang]
    view_data = view_data or {}

    js = (JS
          .replace("__ICON_UP__", json.dumps(ICON_UP))
          .replace("__ICON_DOWN__", json.dumps(ICON_DOWN))
          .replace("__ICON_COMMENT__", json.dumps(ICON_COMMENT))
          .replace("__ICON_COPY__", json.dumps(ICON_COPY))
          .replace("__ICON_CHECK__", json.dumps(ICON_CHECK))
          .replace("__ICON_LOCK__", json.dumps(ICON_LOCK))
          .replace("__ICON_MOON__", json.dumps(ICON_MOON))
          .replace("__ICON_SUN__", json.dumps(ICON_SUN))
          .replace("__ICON_BACK__", json.dumps(ICON_BACK))
          .replace("__ICON_HOME__", json.dumps(ICON_HOME))
          .replace("__ICON_USER__", json.dumps(ICON_USER))
          .replace("__ICON_BELL__", json.dumps(ICON_BELL))
          .replace("__ICON_GEAR__", json.dumps(ICON_GEAR))
          .replace("__ICON_LOGOUT__", json.dumps(ICON_LOGOUT))
          .replace("__ICON_PLUS__", json.dumps(ICON_PLUS))
          .replace("__ICON_LOGIN__", json.dumps(ICON_LOGIN)))

    # подставляем иконку поиска-лупы и фиксим мелкий косяк (см. костыль выше)
    js = js.replace(
        "ICONS.search_loupe",
        json.dumps(ICON_SEARCH)
    )

    return (
        '<!DOCTYPE html>\n'
        f'<html lang="{lang}" data-theme="light">\n'
        '<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=1000" />\n'
        '<meta name="color-scheme" content="light dark" />\n'
        f'<link rel="icon" type="image/svg+xml" href="{FAVICON}" />\n'
        '<title>sldChat</title>\n'
        '<style>' + CSS + '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="layout">\n'
        '  <main class="main" id="main"></main>\n'
        '  <aside class="sidebar" id="sidebar"></aside>\n'
        '</div>\n'
        '<script>\n'
        f'const LANG = {json.dumps(lang)};\n'
        f'const VIEW = {json.dumps(view)};\n'
        f'const VIEW_DATA = {json.dumps(view_data)};\n'
        f'const MAX_POST_LEN = {MAX_POST_LEN};\n'
        f'const MAX_COMMENT_LEN = {MAX_COMMENT_LEN};\n'
        f'const TRUNCATE_LINES = {TRUNCATE_LINES};\n'
        f'const TRUNCATE_CHARS = {TRUNCATE_CHARS};\n'
        f'const T = {json.dumps(t, ensure_ascii=False)};\n'
        + js +
        '\n</script>\n'
        '</body>\n'
        '</html>'
    )


# ------------------------------------------------------------------
# Роуты
# ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def page_index(request: Request):
    return render_page(get_lang(request), "feed")


@app.get("/p/{post_id}", response_class=HTMLResponse)
def page_post(post_id: str, request: Request):
    if post_id not in POSTS:
        raise HTTPException(404, "not found")
    return render_page(get_lang(request), "post", {"post_id": post_id})


@app.get("/u/{nick}", response_class=HTMLResponse)
def page_user(nick: str, request: Request):
    return render_page(get_lang(request), "profile", {"nick": nick})


@app.get("/notifications", response_class=HTMLResponse)
def page_notifications(request: Request):
    return render_page(get_lang(request), "notifications")


@app.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request):
    return render_page(get_lang(request), "settings")


@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request):
    return render_page(get_lang(request), "register")


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return render_page(get_lang(request), "login")
