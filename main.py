import os
import re
import uuid
import json
import html
import random
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Set, Tuple

from fastapi import FastAPI, Request, Response, HTTPException, Cookie, Query, Body
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from pydantic import BaseModel, Field


APP_NAME = "Chirp"
APP_TAGLINE = "Что происходит?"
MAX_TWEET_LEN = 280
SESSION_COOKIE = "chirp_session"


def now() -> datetime:
    return datetime.now(timezone.utc)


def esc(s: Any) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def short_id() -> str:
    return uuid.uuid4().hex[:10]


def parse_hashtags(text: str) -> List[str]:
    return list(dict.fromkeys(re.findall(r"#([A-Za-z0-9_\u0400-\u04FF]{1,50})", text)))


def parse_mentions(text: str) -> List[str]:
    return list(dict.fromkeys(re.findall(r"@([A-Za-z0-9_]{1,30})", text)))


def hash_password(password: str, salt: Optional[str] = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000)
    return salt + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000).hex()
    return secrets.compare_digest(calc, digest)


def fmt_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now() - dt
    s = delta.total_seconds()
    if s < 0:
        s = 0
    if s < 60:
        return f"{int(s)}с"
    if s < 3600:
        return f"{int(s // 60)}м"
    if s < 86400:
        return f"{int(s // 3600)}ч"
    if s < 604800:
        return f"{int(s // 86400)}д"
    if dt.year == now().year:
        return dt.strftime("%d %b")
    return dt.strftime("%d %b %Y")


def fmt_full_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%H:%M · %d %b %Y")


def initials(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "?"
    parts = re.split(r"\s+", name)
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    return name[:2].upper()


def color_for(seed: str) -> str:
    h = 0
    for ch in seed:
        h = (h * 31 + ord(ch)) % 360
    return f"hsl({h}, 62%, 48%)"


def linkify(text: str) -> str:
    out = esc(text)
    out = re.sub(
        r"(https?://[^\s<]+)",
        lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener" class="tlink">{m.group(1)}</a>',
        out,
    )
    out = re.sub(
        r"(?<![\w/])@([A-Za-z0-9_]{1,30})",
        lambda m: f'<a href="/u/{m.group(1)}" class="tlink">@{m.group(1)}</a>',
        out,
    )
    out = re.sub(
        r"(?<![\w/])#([A-Za-z0-9_\u0400-\u04FF]{1,50})",
        lambda m: f'<a href="/search?q=%23{m.group(1)}" class="tlink">#{m.group(1)}</a>',
        out,
    )
    out = out.replace("\n", "<br>")
    return out


USERS: Dict[str, dict] = {}
USERNAMES: Dict[str, str] = {}
EMAILS: Dict[str, str] = {}
SESSIONS: Dict[str, str] = {}
TWEETS: Dict[str, dict] = {}
FOLLOWING: Dict[str, Set[str]] = {}
FOLLOWERS: Dict[str, Set[str]] = {}
LIKES_BY_USER: Dict[str, Set[str]] = {}
LIKES_BY_TWEET: Dict[str, Set[str]] = {}
RETWEETS_BY_USER: Dict[str, Set[str]] = {}
RETWEETS_BY_TWEET: Dict[str, Set[str]] = {}
BOOKMARKS: Dict[str, Set[str]] = {}
NOTIFICATIONS: Dict[str, List[dict]] = {}
HASHTAGS: Dict[str, Set[str]] = {}
MESSAGES: Dict[str, List[dict]] = {}
MUTES: Dict[str, Set[str]] = {}
BLOCKS: Dict[str, Set[str]] = {}
VERIFY_CODES: Dict[str, str] = {}


def create_user(username: str, email: str, password: str, display_name: Optional[str] = None) -> dict:
    username = (username or "").strip().lower()
    email = (email or "").strip().lower()
    if not re.match(r"^[a-z0-9_]{3,20}$", username):
        raise ValueError("Ник должен быть от 3 до 20 символов, только a-z, 0-9 и _")
    if username in USERNAMES:
        raise ValueError("Этот ник уже занят")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("Некорректный email")
    if email in EMAILS:
        raise ValueError("Этот email уже зарегистрирован")
    if len(password) < 6:
        raise ValueError("Пароль должен быть минимум 6 символов")
    uid = new_id()
    USERS[uid] = {
        "id": uid,
        "username": username,
        "email": email,
        "password": hash_password(password),
        "display_name": display_name or username,
        "bio": "",
        "location": "",
        "website": "",
        "avatar_color": color_for(username),
        "created_at": now(),
        "verified": False,
        "pinned": None,
        "theme": "dark",
        "last_seen": now(),
    }
    USERNAMES[username] = uid
    EMAILS[email] = uid
    FOLLOWING[uid] = set()
    FOLLOWERS[uid] = set()
    LIKES_BY_USER[uid] = set()
    RETWEETS_BY_USER[uid] = set()
    BOOKMARKS[uid] = set()
    NOTIFICATIONS[uid] = []
    MUTES[uid] = set()
    BLOCKS[uid] = set()
    return USERS[uid]


def get_user_by_username(username: str) -> Optional[dict]:
    uid = USERNAMES.get((username or "").lower())
    if uid is None:
        return None
    return USERS.get(uid)


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = user_id
    return token


def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    uid = SESSIONS.get(token)
    if not uid:
        return None
    user = USERS.get(uid)
    if user:
        user["last_seen"] = now()
    return user


def create_tweet(
    user_id: str,
    text: str,
    parent_id: Optional[str] = None,
    retweet_of: Optional[str] = None,
    image_url: Optional[str] = None,
) -> dict:
    tid = new_id()
    tags = parse_hashtags(text or "")
    tweet = {
        "id": tid,
        "user_id": user_id,
        "text": (text or "").strip(),
        "created_at": now(),
        "parent_id": parent_id,
        "retweet_of": retweet_of,
        "image_url": image_url,
        "hashtags": tags,
        "views": 0,
    }
    TWEETS[tid] = tweet
    LIKES_BY_TWEET[tid] = set()
    RETWEETS_BY_TWEET[tid] = set()
    for tag in tags:
        HASHTAGS.setdefault(tag.lower(), set()).add(tid)
    for mentioned in parse_mentions(text or ""):
        mu = get_user_by_username(mentioned)
        if mu and mu["id"] != user_id:
            add_notification(mu["id"], "mention", user_id, tid)
    if parent_id and parent_id in TWEETS:
        parent = TWEETS[parent_id]
        if parent["user_id"] != user_id:
            add_notification(parent["user_id"], "reply", user_id, tid)
    return tweet


def delete_tweet(tweet_id: str, user_id: str) -> bool:
    tw = TWEETS.get(tweet_id)
    if not tw or tw["user_id"] != user_id:
        return False
    TWEETS.pop(tweet_id, None)
    LIKES_BY_TWEET.pop(tweet_id, None)
    RETWEETS_BY_TWEET.pop(tweet_id, None)
    for tag, ids in HASHTAGS.items():
        ids.discard(tweet_id)
    for uid in list(LIKES_BY_USER.keys()):
        LIKES_BY_USER[uid].discard(tweet_id)
    for uid in list(RETWEETS_BY_USER.keys()):
        RETWEETS_BY_USER[uid].discard(tweet_id)
    for uid in list(BOOKMARKS.keys()):
        BOOKMARKS[uid].discard(tweet_id)
    return True


def add_notification(user_id: str, kind: str, actor_id: str, tweet_id: Optional[str] = None) -> None:
    if user_id == actor_id:
        return
    NOTIFICATIONS.setdefault(user_id, []).append({
        "id": new_id(),
        "kind": kind,
        "actor_id": actor_id,
        "tweet_id": tweet_id,
        "created_at": now(),
        "read": False,
    })


def toggle_like(tweet_id: str, user_id: str) -> bool:
    if tweet_id not in LIKES_BY_TWEET:
        return False
    likes = LIKES_BY_TWEET[tweet_id]
    if user_id in likes:
        likes.discard(user_id)
        LIKES_BY_USER.setdefault(user_id, set()).discard(tweet_id)
        return False
    likes.add(user_id)
    LIKES_BY_USER.setdefault(user_id, set()).add(tweet_id)
    tw = TWEETS.get(tweet_id)
    if tw:
        add_notification(tw["user_id"], "like", user_id, tweet_id)
    return True


def toggle_retweet(tweet_id: str, user_id: str) -> bool:
    if tweet_id not in RETWEETS_BY_TWEET:
        return False
    rts = RETWEETS_BY_TWEET[tweet_id]
    if user_id in rts:
        rts.discard(user_id)
        RETWEETS_BY_USER.setdefault(user_id, set()).discard(tweet_id)
        return False
    rts.add(user_id)
    RETWEETS_BY_USER.setdefault(user_id, set()).add(tweet_id)
    tw = TWEETS.get(tweet_id)
    if tw:
        add_notification(tw["user_id"], "retweet", user_id, tweet_id)
    return True


def toggle_bookmark(tweet_id: str, user_id: str) -> bool:
    bm = BOOKMARKS.setdefault(user_id, set())
    if tweet_id in bm:
        bm.discard(tweet_id)
        return False
    bm.add(tweet_id)
    return True


def toggle_follow(target_id: str, user_id: str) -> bool:
    if target_id == user_id:
        return False
    following = FOLLOWING.setdefault(user_id, set())
    if target_id in following:
        following.discard(target_id)
        FOLLOWERS.setdefault(target_id, set()).discard(user_id)
        return False
    following.add(target_id)
    FOLLOWERS.setdefault(target_id, set()).add(user_id)
    add_notification(target_id, "follow", user_id, None)
    return True


def reply_ids(tweet_id: str) -> List[str]:
    return [t["id"] for t in TWEETS.values() if t.get("parent_id") == tweet_id]


def build_thread(tweet_id: str) -> List[dict]:
    chain = []
    cur = TWEETS.get(tweet_id)
    seen = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        chain.append(cur)
        pid = cur.get("parent_id")
        cur = TWEETS.get(pid) if pid else None
    chain.reverse()
    return chain


def user_timeline(user_id: str, limit: int = 50) -> List[dict]:
    following = FOLLOWING.get(user_id, set())
    out = []
    for t in TWEETS.values():
        if t.get("parent_id"):
            continue
        if t["user_id"] == user_id or t["user_id"] in following:
            out.append(t)
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out[:limit]


def global_timeline(limit: int = 100) -> List[dict]:
    out = [t for t in TWEETS.values() if not t.get("parent_id")]
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out[:limit]


def user_tweets(user_id: str, limit: int = 50) -> List[dict]:
    out = [t for t in TWEETS.values() if t["user_id"] == user_id and not t.get("parent_id")]
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out[:limit]


def user_replies(user_id: str, limit: int = 50) -> List[dict]:
    out = [t for t in TWEETS.values() if t["user_id"] == user_id and t.get("parent_id")]
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out[:limit]


def user_media(user_id: str, limit: int = 50) -> List[dict]:
    out = [t for t in TWEETS.values() if t["user_id"] == user_id and t.get("image_url")]
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out[:limit]


def user_likes(user_id: str, limit: int = 50) -> List[dict]:
    ids = LIKES_BY_USER.get(user_id, set())
    out = [TWEETS[i] for i in ids if i in TWEETS]
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out[:limit]


def search_tweets(q: str, limit: int = 60) -> List[dict]:
    ql = (q or "").strip().lower()
    if not ql:
        return []
    results = []
    if ql.startswith("#"):
        tag = ql[1:]
        ids = HASHTAGS.get(tag, set())
        results = [TWEETS[i] for i in ids if i in TWEETS]
    else:
        for t in TWEETS.values():
            if ql in t["text"].lower():
                results.append(t)
    results.sort(key=lambda x: x["created_at"], reverse=True)
    return results[:limit]


def search_users(q: str, limit: int = 30) -> List[dict]:
    ql = (q or "").strip().lower().lstrip("@")
    if not ql:
        return []
    res = []
    for u in USERS.values():
        if ql in u["username"].lower() or ql in (u.get("display_name") or "").lower():
            res.append(u)
    res.sort(key=lambda u: len(FOLLOWERS.get(u["id"], set())), reverse=True)
    return res[:limit]


def trending_hashtags(limit: int = 8) -> List[Tuple[str, int]]:
    items = []
    for tag, ids in HASHTAGS.items():
        cnt = len([i for i in ids if i in TWEETS])
        if cnt > 0:
            items.append((tag, cnt))
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:limit]


def suggested_users(user_id: str, limit: int = 3) -> List[dict]:
    following = FOLLOWING.get(user_id, set())
    following = set(following) | {user_id}
    res = [u for u in USERS.values() if u["id"] not in following]
    res.sort(key=lambda u: len(FOLLOWERS.get(u["id"], set())), reverse=True)
    return res[:limit]


def conversation_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def get_conversation(a: str, b: str) -> List[dict]:
    return MESSAGES.get(conversation_key(a, b), [])


def send_message(from_id: str, to_id: str, text: str) -> dict:
    key = conversation_key(from_id, to_id)
    msg = {
        "id": new_id(),
        "from_id": from_id,
        "to_id": to_id,
        "text": text.strip(),
        "created_at": now(),
        "read": False,
    }
    MESSAGES.setdefault(key, []).append(msg)
    return msg


def unread_notifications(user_id: str) -> int:
    return len([n for n in NOTIFICATIONS.get(user_id, []) if not n["read"]])


def unread_messages(user_id: str) -> int:
    count = 0
    for key, msgs in MESSAGES.items():
        if user_id in key.split("|"):
            count += len([m for m in msgs if m["to_id"] == user_id and not m["read"]])
    return count


def is_following(a: str, b: str) -> bool:
    return b in FOLLOWING.get(a, set())


ICON_PATHS = {
    "home": '<path d="M3 11.5 12 4l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "bell": '<path d="M12 3a6 6 0 0 0-6 6v4l-2 3h16l-2-3V9a6 6 0 0 0-6-6z"/><path d="M10 19a2 2 0 0 0 4 0"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    "bookmark": '<path d="M6 3h12v18l-6-4-6 4z"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "more": '<circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/>',
    "feather": '<path d="M20 4C13 4 7 10 7 17v3"/><path d="M5 15h8"/><path d="M9 11h6"/><path d="M13 7h6"/>',
    "heart": '<path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/>',
    "repost": '<path d="M4 12V8a3 3 0 0 1 3-3h10"/><path d="m13 1 4 4-4 4"/><path d="M20 12v4a3 3 0 0 1-3 3H7"/><path d="m11 23-4-4 4-4"/>',
    "comment": '<path d="M21 12a8 8 0 0 1-8 8H8l-5 3 1.5-5A8 8 0 1 1 21 12z"/>',
    "share": '<path d="M12 3v12"/><path d="m7 8 5-5 5 5"/><path d="M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/>',
    "trash": '<path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7"/>',
    "close": '<path d="M6 6l12 12"/><path d="M18 6 6 18"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="m4 18 5-5 4 4 3-3 4 4"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5h.1a1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    "verified": '<path d="m12 2 2.4 2.1 3.2-.2.9 3 2.7 1.7-1.2 3 1.2 3-2.7 1.7-.9 3-3.2-.2L12 22l-2.4-2.1-3.2.2-.9-3L2.8 15.4 4 12.4 2.8 9.4l2.7-1.7.9-3 3.2.2z"/><path d="m9 12 2 2 4-4"/>',
    "grid": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>',
    "arrow_left": '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>',
    "send": '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>',
    "moon": '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "hash": '<path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18"/>',
    "at": '<circle cx="12" cy="12" r="4"/><path d="M16 12v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-4 7.5"/>',
    "lock": '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "check": '<path d="m5 12 5 5L20 7"/>',
    "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "sparkles": '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/>',
    "pin": '<path d="M12 17v5"/><path d="M9 3h6l-1 8 3 3H7l3-3z"/>',
}


def icon(name: str, size: int = 22, cls: str = "ic", filled: bool = False) -> str:
    path = ICON_PATHS.get(name, "")
    fill = "currentColor" if filled else "none"
    return (
        f'<svg class="{cls}" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="{fill}" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{path}</svg>'
    )


CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
--bg:#000;--bg-elev:#16181c;--bg-hover:#1a1d21;--bg-soft:#0a0a0a;
--border:#2f3336;--text:#e7e9ea;--text-dim:#71767b;--text-soft:#536471;
--accent:#1d9bf0;--accent-hover:#1a8cd8;--accent-soft:rgba(29,155,240,.12);
--like:#f91880;--like-soft:rgba(249,24,128,.12);
--repost:#00ba7c;--repost-soft:rgba(0,186,124,.12);
--danger:#f4212e;--danger-soft:rgba(244,33,46,.12);
--radius:16px;--radius-pill:9999px;
--shadow:0 0 14px rgba(255,255,255,.05);
--header-h:56px;
}
[data-theme="light"]{
--bg:#fff;--bg-elev:#f7f9f9;--bg-hover:#f7f9f9;--bg-soft:#eff3f4;
--border:#eff3f4;--text:#0f1419;--text-dim:#536471;--text-soft:#8b98a5;
--accent-soft:rgba(29,155,240,.1);
--shadow:0 0 14px rgba(0,0,0,.06);
}
html,body{height:100%}
body{
background:var(--bg);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
font-size:15px;line-height:1.4;-webkit-font-smoothing:antialiased;
overscroll-behavior-y:none;
}
a{color:inherit;text-decoration:none}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
input,textarea{font:inherit;color:inherit;background:none;border:none;outline:none;width:100%}
img{max-width:100%;display:block}
::selection{background:var(--accent);color:#fff}
.app{max-width:1290px;margin:0 auto;display:grid;grid-template-columns:275px 600px 350px;gap:0;min-height:100vh}
.sidebar-left{position:sticky;top:0;height:100vh;padding:0 12px;display:flex;flex-direction:column;border-right:1px solid var(--border);overflow-y:auto}
.sidebar-left::-webkit-scrollbar{width:0}
.logo{width:52px;height:52px;display:flex;align-items:center;justify-content:center;margin:8px 0;border-radius:50%;color:var(--accent);transition:background .15s}
.logo:hover{background:var(--accent-soft)}
.nav-item{display:flex;align-items:center;gap:16px;padding:12px 12px;border-radius:var(--radius-pill);font-size:20px;font-weight:500;transition:background .15s;position:relative;margin-bottom:2px}
.nav-item:hover{background:var(--bg-hover)}
.nav-item.active{font-weight:700}
.nav-item .badge{
position:absolute;left:30px;top:6px;min-width:20px;height:20px;padding:0 6px;
background:var(--accent);color:#fff;border-radius:999px;font-size:12px;
display:flex;align-items:center;justify-content:center;font-weight:700;
}
.nav-item .label{font-size:20px}
.post-btn{
margin-top:16px;background:var(--accent);color:#fff;font-weight:700;font-size:17px;
padding:15px 0;border-radius:var(--radius-pill);text-align:center;transition:background .15s;
}
.post-btn:hover{background:var(--accent-hover)}
.post-btn .post-btn-ico{display:none}
.sidebar-user{
margin-top:auto;margin-bottom:12px;display:flex;align-items:center;gap:12px;
padding:12px;border-radius:var(--radius-pill);cursor:pointer;transition:background .15s;
}
.sidebar-user:hover{background:var(--bg-hover)}
.sidebar-user .who{flex:1;min-width:0}
.sidebar-user .who b{display:block;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sidebar-user .who span{display:block;color:var(--text-dim);font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.avatar{
width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;
color:#fff;font-weight:700;font-size:15px;flex-shrink:0;user-select:none;
}
.avatar.big{width:80px;height:80px;font-size:32px}
.avatar.huge{width:120px;height:120px;font-size:44px;border:4px solid var(--bg)}
.avatar.sm{width:32px;height:32px;font-size:13px}
.avatar.xs{width:24px;height:24px;font-size:11px}
.main{border-right:1px solid var(--border);min-height:100vh;min-width:0}
.topbar{
position:sticky;top:0;z-index:20;background:rgba(0,0,0,.7);
backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
border-bottom:1px solid var(--border);height:var(--header-h);
display:flex;align-items:center;padding:0 16px;gap:24px;
}
[data-theme="light"] .topbar{background:rgba(255,255,255,.85)}
.topbar h1{font-size:20px;font-weight:700}
.back-btn{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;transition:background .15s}
.back-btn:hover{background:var(--bg-hover)}
.composer{display:flex;gap:12px;padding:16px;border-bottom:1px solid var(--border)}
.composer textarea{
font-size:20px;resize:none;min-height:52px;line-height:1.4;padding-top:8px;color:var(--text);
}
.composer textarea::placeholder{color:var(--text-dim)}
.composer-body{flex:1;min-width:0}
.composer-actions{display:flex;align-items:center;justify-content:space-between;margin-top:8px;padding-top:12px;border-top:1px solid var(--border)}
.composer-tools{display:flex;gap:4px;margin-left:-8px}
.icon-btn{
width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;
color:var(--accent);transition:background .15s;
}
.icon-btn:hover{background:var(--accent-soft)}
.composer-submit{
background:var(--accent);color:#fff;font-weight:700;padding:8px 20px;border-radius:var(--radius-pill);
transition:background .15s;font-size:15px;
}
.composer-submit:disabled{opacity:.5;cursor:default}
.composer-submit:hover:not(:disabled){background:var(--accent-hover)}
.char-counter{font-size:13px;color:var(--text-dim);margin-right:8px}
.char-counter.warn{color:#ffd400}
.char-counter.danger{color:var(--danger)}
.tweet{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;gap:12px;cursor:pointer;transition:background .15s;position:relative}
.tweet:hover{background:var(--bg-hover)}
.tweet-main{flex:1;min-width:0}
.tweet-head{display:flex;align-items:center;gap:4px;flex-wrap:wrap;font-size:15px}
.tweet-head b{font-weight:700}
.tweet-head .handle{color:var(--text-dim)}
.tweet-head .dot{color:var(--text-dim)}
.tweet-head .time{color:var(--text-dim)}
.tweet-head .time:hover{text-decoration:underline}
.tweet-head .menu{margin-left:auto;color:var(--text-dim);width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center}
.tweet-head .menu:hover{background:var(--accent-soft);color:var(--accent)}
.tweet-text{font-size:15px;margin-top:2px;word-wrap:break-word;white-space:pre-wrap}
.tweet-media{margin-top:12px;border-radius:16px;overflow:hidden;border:1px solid var(--border)}
.tweet-media img{width:100%;height:auto;max-height:520px;object-fit:cover}
.tweet-quote{margin-top:12px;border:1px solid var(--border);border-radius:16px;padding:12px;transition:background .15s}
.tweet-quote:hover{background:var(--bg-hover)}
.tweet-actions{display:flex;justify-content:space-between;margin-top:12px;max-width:480px;margin-left:-8px}
.tweet-actions .act{
display:flex;align-items:center;gap:0;color:var(--text-dim);font-size:13px;
border-radius:var(--radius-pill);transition:color .15s;
}
.tweet-actions .act .icwrap{width:34px;height:34px;display:flex;align-items:center;justify-content:center;border-radius:50%;transition:background .15s}
.tweet-actions .act span{padding-right:8px;min-width:14px}
.tweet-actions .act.reply:hover{color:var(--accent)}
.tweet-actions .act.reply:hover .icwrap{background:var(--accent-soft)}
.tweet-actions .act.repost:hover,.tweet-actions .act.repost.on{color:var(--repost)}
.tweet-actions .act.repost:hover .icwrap{background:var(--repost-soft)}
.tweet-actions .act.like:hover,.tweet-actions .act.like.on{color:var(--like)}
.tweet-actions .act.like:hover .icwrap{background:var(--like-soft)}
.tweet-actions .act.book:hover,.tweet-actions .act.book.on{color:var(--accent)}
.tweet-actions .act.book:hover .icwrap{background:var(--accent-soft)}
.tweet-actions .act.share:hover{color:var(--accent)}
.tweet-actions .act.share:hover .icwrap{background:var(--accent-soft)}
.tweet-actions .act.liked{animation:pop .3s ease}
@keyframes pop{0%{transform:scale(1)}50%{transform:scale(1.3)}100%{transform:scale(1)}}
.repost-label{color:var(--text-dim);font-size:13px;display:flex;align-items:center;gap:8px;margin-left:52px;margin-bottom:4px;font-weight:600}
.verified-badge{color:var(--accent);display:inline-flex;vertical-align:-2px}
.tlink{color:var(--accent)}
.tlink:hover{text-decoration:underline}
.sidebar-right{position:sticky;top:0;height:100vh;overflow-y:auto;padding:12px 20px}
.sidebar-right::-webkit-scrollbar{width:6px}
.sidebar-right::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.search-box{
background:var(--bg-elev);border-radius:var(--radius-pill);display:flex;align-items:center;
padding:10px 16px;gap:12px;border:1px solid transparent;transition:border-color .15s,background .15s;
margin-bottom:16px;
}
.search-box:focus-within{border-color:var(--accent);background:var(--bg)}
.search-box input{font-size:15px}
.search-box svg{color:var(--text-dim);flex-shrink:0}
.card{background:var(--bg-elev);border-radius:16px;margin-bottom:16px;overflow:hidden}
.card h3{font-size:19px;font-weight:800;padding:12px 16px}
.card-item{padding:12px 16px;cursor:pointer;transition:background .15s;display:flex;gap:12px;align-items:center}
.card-item:hover{background:rgba(255,255,255,.03)}
[data-theme="light"] .card-item:hover{background:rgba(0,0,0,.03)}
.card-item .num{font-size:13px;color:var(--text-dim)}
.card-item .tag{font-weight:700;font-size:15px}
.follow-btn{
background:var(--text);color:var(--bg);font-weight:700;padding:6px 16px;border-radius:var(--radius-pill);
font-size:14px;transition:opacity .15s;flex-shrink:0;
}
.follow-btn:hover{opacity:.85}
.follow-btn.following{background:transparent;color:var(--text);border:1px solid var(--border)}
.follow-btn.following:hover{background:var(--danger-soft);color:var(--danger);border-color:var(--danger)}
.follow-btn.following:hover .label-following{display:none}
.follow-btn.following .label-unfollow{display:none}
.follow-btn.following:hover .label-unfollow{display:inline}
.profile-head{padding:0 16px}
.profile-banner{height:200px;background:linear-gradient(135deg,#1d9bf0,#8b5cf6,#f91880);margin:0 -16px;position:relative}
.profile-banner::after{content:"";position:absolute;inset:0;background:radial-gradient(circle at 30% 40%,rgba(255,255,255,.25),transparent 60%)}
.profile-top{display:flex;justify-content:space-between;align-items:flex-end;margin-top:-68px;position:relative}
.profile-actions{display:flex;gap:8px;margin-top:68px}
.btn-outline{border:1px solid var(--border);border-radius:var(--radius-pill);padding:8px 16px;font-weight:700;font-size:14px;transition:background .15s}
.btn-outline:hover{background:var(--bg-hover)}
.profile-info{margin-top:12px}
.profile-info h2{font-size:22px;font-weight:800;display:flex;align-items:center;gap:6px}
.profile-info .h{color:var(--text-dim);font-size:15px}
.profile-bio{margin-top:12px;font-size:15px;line-height:1.5;white-space:pre-wrap}
.profile-meta{margin-top:12px;display:flex;flex-wrap:wrap;gap:16px;color:var(--text-dim);font-size:14px;align-items:center}
.profile-meta svg{vertical-align:-4px;margin-right:4px}
.profile-stats{margin-top:12px;display:flex;gap:20px;font-size:14px;color:var(--text-dim)}
.profile-stats b{color:var(--text);font-weight:700}
.profile-stats a:hover b{text-decoration:underline}
.tabs{display:flex;border-bottom:1px solid var(--border);margin-top:16px;position:sticky;top:var(--header-h);background:rgba(0,0,0,.7);backdrop-filter:blur(12px);z-index:10}
[data-theme="light"] .tabs{background:rgba(255,255,255,.85)}
.tab{flex:1;padding:16px 4px;text-align:center;color:var(--text-dim);font-weight:600;font-size:15px;transition:background .15s;position:relative;cursor:pointer}
.tab:hover{background:var(--bg-hover);color:var(--text)}
.tab.active{color:var(--text);font-weight:700}
.tab.active::after{content:"";position:absolute;bottom:0;left:50%;transform:translateX(-50%);width:56px;height:4px;background:var(--accent);border-radius:2px}
.tab .count{font-weight:400;color:var(--text-dim);font-size:13px;margin-left:4px}
.empty{padding:48px 24px;text-align:center;color:var(--text-dim)}
.empty h3{color:var(--text);font-size:22px;font-weight:800;margin-bottom:8px}
.empty p{font-size:15px;max-width:380px;margin:0 auto}
.auth-wrap{min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 20% 20%,rgba(29,155,240,.15),transparent 50%),radial-gradient(circle at 80% 80%,rgba(249,24,128,.12),transparent 50%),var(--bg)}
.auth-card{
width:100%;max-width:420px;background:var(--bg);border:1px solid var(--border);
border-radius:24px;padding:32px;
}
.auth-card .brand{display:flex;align-items:center;gap:12px;justify-content:center;margin-bottom:24px;color:var(--accent);font-size:28px;font-weight:900;letter-spacing:-.5px}
.auth-card h1{font-size:26px;font-weight:800;margin-bottom:20px;text-align:center}
.field{
border:1px solid var(--border);border-radius:12px;padding:12px 14px;margin-bottom:12px;
background:var(--bg);transition:border-color .15s;
}
.field:focus-within{border-color:var(--accent)}
.field label{display:block;font-size:12px;color:var(--text-dim);margin-bottom:2px;font-weight:600}
.field input{font-size:16px;width:100%}
.field textarea{font-size:16px;resize:none;min-height:60px;width:100%}
.btn-primary{
width:100%;background:var(--accent);color:#fff;font-weight:700;font-size:17px;
padding:14px;border-radius:var(--radius-pill);transition:background .15s;
}
.btn-primary:hover{background:var(--accent-hover)}
.btn-primary:disabled{opacity:.5;cursor:default}
.switch{margin-top:20px;text-align:center;color:var(--text-dim);font-size:15px}
.switch a{color:var(--accent);font-weight:600}
.switch a:hover{text-decoration:underline}
.error{
background:var(--danger-soft);color:var(--danger);border:1px solid var(--danger);
border-radius:12px;padding:10px 14px;font-size:14px;margin-bottom:16px;
}
.success{background:var(--repost-soft);color:var(--repost);border:1px solid var(--repost);border-radius:12px;padding:10px 14px;font-size:14px;margin-bottom:16px}
.thread-head{padding:16px}
.thread-line{position:absolute;left:52px;top:64px;bottom:16px;width:2px;background:var(--border)}
.thread-wrap{position:relative}
.tweet.thread{padding:12px 16px 16px}
.reply-context{padding:8px 16px;color:var(--text-dim);font-size:14px}
.reply-context a{color:var(--accent)}
.reply-context a:hover{text-decoration:underline}
.modal-backdrop{position:fixed;inset:0;background:rgba(91,112,131,.4);z-index:1000;display:none;align-items:center;justify-content:center;padding:16px}
.modal-backdrop.show{display:flex}
.modal{
background:var(--bg);border-radius:20px;width:100%;max-width:600px;max-height:90vh;overflow-y:auto;
border:1px solid var(--border);
}
.modal-header{display:flex;align-items:center;padding:8px 16px;position:sticky;top:0;background:rgba(0,0,0,.85);backdrop-filter:blur(12px);z-index:1}
[data-theme="light"] .modal-header{background:rgba(255,255,255,.9)}
.modal-close{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;transition:background .15s}
.modal-close:hover{background:var(--bg-hover)}
.settings-row{display:flex;align-items:center;justify-content:space-between;padding:16px;border-bottom:1px solid var(--border)}
.settings-row:hover{background:var(--bg-hover)}
.settings-row b{display:block;font-size:15px}
.settings-row span{display:block;color:var(--text-dim);font-size:14px;margin-top:2px}
.toast{
position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);
background:var(--accent);color:#fff;padding:12px 20px;border-radius:var(--radius-pill);
font-weight:600;z-index:2000;transition:transform .3s cubic-bezier(.2,.8,.2,1);pointer-events:none;
}
.toast.show{transform:translateX(-50%) translateY(0)}
.bottom-nav{display:none;position:fixed;bottom:0;left:0;right:0;background:rgba(0,0,0,.85);backdrop-filter:blur(12px);border-top:1px solid var(--border);justify-content:space-around;padding:8px 0;z-index:100}
[data-theme="light"] .bottom-nav{background:rgba(255,255,255,.9)}
.bottom-nav a{flex:1;display:flex;align-items:center;justify-content:center;padding:10px 0;color:var(--text-dim);position:relative}
.bottom-nav a.active{color:var(--accent)}
.bottom-nav a .badge{position:absolute;top:4px;right:calc(50% - 20px);min-width:16px;height:16px;padding:0 4px;background:var(--accent);color:#fff;border-radius:999px;font-size:10px;display:flex;align-items:center;justify-content:center;font-weight:700}
.mobile-topbar{display:none;position:sticky;top:0;z-index:50;background:rgba(0,0,0,.85);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:0 12px;height:var(--header-h);align-items:center;gap:12px}
[data-theme="light"] .mobile-topbar{background:rgba(255,255,255,.9)}
.mobile-topbar .avatar{width:32px;height:32px;font-size:13px}
.mobile-topbar .brand{color:var(--accent);display:flex;align-items:center;gap:8px;font-weight:900;font-size:20px}
.fab{
position:fixed;right:20px;bottom:80px;width:56px;height:56px;border-radius:50%;background:var(--accent);
color:#fff;display:none;align-items:center;justify-content:center;box-shadow:0 4px 14px rgba(29,155,240,.4);z-index:99;
}
.msg-list{display:flex;flex-direction:column;gap:0}
.msg{padding:10px 14px;border-radius:18px;max-width:75%;font-size:15px;word-wrap:break-word;margin:2px 0}
.msg.out{background:var(--accent);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
.msg.in{background:var(--bg-elev);align-self:flex-start;border-bottom-left-radius:4px}
.msg-time{font-size:11px;opacity:.75;margin-top:4px;text-align:right}
.chat-input{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--border);background:var(--bg);position:sticky;bottom:0}
.chat-input .field{flex:1;margin:0;padding:10px 14px}
.chat-input button{background:var(--accent);color:#fff;border-radius:var(--radius-pill);padding:0 18px;font-weight:700}
.notif-row{display:flex;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s;align-items:flex-start}
.notif-row:hover{background:var(--bg-hover)}
.notif-row.unread{background:var(--accent-soft)}
.notif-ic{width:32px;height:32px;display:flex;align-items:center;justify-content:center;color:var(--accent)}
.notif-ic.like{color:var(--like)}
.notif-ic.repost{color:var(--repost)}
.notif-ic.follow{color:var(--accent)}
.notif-body{flex:1;min-width:0;font-size:15px}
.notif-body b{font-weight:700}
.notif-body .time{color:var(--text-dim);font-size:13px;margin-left:6px}
.notif-body .preview{color:var(--text-dim);margin-top:4px;font-size:14px;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
.pin-label{color:var(--text-dim);font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px;margin-left:52px;margin-bottom:4px}
.pill{display:inline-block;background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:4px 10px;font-size:13px;font-weight:600;margin-right:6px;margin-top:6px}
.pill:hover{background:var(--accent);color:#fff}
.link-btn{color:var(--accent);font-weight:600}
.link-btn:hover{text-decoration:underline}
.hidden{display:none!important}
.emoji-pick{display:flex;gap:2px;flex-wrap:wrap;max-width:280px;background:var(--bg-elev);border-radius:12px;padding:8px;border:1px solid var(--border)}
.emoji-pick button{font-size:20px;padding:4px 6px;border-radius:8px}
.emoji-pick button:hover{background:var(--bg-hover)}
@media (max-width:1280px){
.app{grid-template-columns:88px 600px 1fr}
.sidebar-left{padding:0 8px;align-items:center}
.nav-item .label{display:none}
.nav-item{justify-content:center;padding:12px}
.nav-item .badge{left:auto;right:12px;top:6px}
.post-btn{width:52px;height:52px;padding:0;display:flex;align-items:center;justify-content:center}
.post-btn .post-btn-text{display:none}
.post-btn .post-btn-ico{display:block}
.sidebar-user .who,.sidebar-user .more{display:none}
.sidebar-user{justify-content:center;padding:8px}
}
@media (max-width:1024px){
.app{grid-template-columns:88px 1fr}
.sidebar-right{display:none}
}
@media (max-width:720px){
.app{grid-template-columns:1fr;padding-bottom:64px}
.sidebar-left{display:none}
.sidebar-right{display:none}
.mobile-topbar{display:flex}
.bottom-nav{display:flex}
.fab{display:flex}
.tweet{padding:10px 12px}
.composer{padding:12px}
.composer textarea{font-size:17px}
.profile-banner{height:120px}
.profile-head{padding:0 12px}
.tabs{position:sticky;top:var(--header-h)}
.card{border-radius:12px}
.sidebar-right{border:none}
.auth-card{padding:24px 20px}
}
@media (min-width:721px){
.fab{display:none!important}
}
"""

JS = r"""
(function(){
const THEME_KEY="chirp_theme";
function setTheme(t){document.documentElement.setAttribute("data-theme",t);localStorage.setItem(THEME_KEY,t);}
const saved=localStorage.getItem(THEME_KEY);if(saved)setTheme(saved);
window.__toast=(msg)=>{
  let t=document.querySelector(".toast");
  if(!t){t=document.createElement("div");t.className="toast";document.body.appendChild(t);}
  t.textContent=msg;t.classList.add("show");
  clearTimeout(window.__toastT);
  window.__toastT=setTimeout(()=>t.classList.remove("show"),1800);
};
function esc(s){const d=document.createElement("div");d.textContent=s;return d.innerHTML;}
function fmtNum(n){if(n>=1e6)return (n/1e6).toFixed(1).replace(/\.0$/,"")+"M";if(n>=1e3)return (n/1e3).toFixed(1).replace(/\.0$/,"")+"K";return ""+n;}
async function api(url,opts){
  opts=opts||{};
  opts.headers=Object.assign({"Content-Type":"application/json"},opts.headers||{});
  const r=await fetch(url,opts);
  let data=null;
  try{data=await r.json();}catch(e){}
  if(!r.ok){const err=(data&&data.detail)||"Ошибка";throw new Error(err);}
  return data;
}
document.addEventListener("click",async(e)=>{
  const el=e.target.closest("[data-action]");
  if(!el)return;
  const action=el.dataset.action;
  e.preventDefault();
  e.stopPropagation();
  if(action==="theme"){
    const cur=document.documentElement.getAttribute("data-theme")==="light"?"dark":"light";
    setTheme(cur);
    try{await api("/api/theme",{method:"POST",body:JSON.stringify({theme:cur})});}catch(e){}
    return;
  }
  if(action==="like"){
    const id=el.dataset.id;
    try{
      const r=await api("/api/tweets/"+id+"/like",{method:"POST"});
      el.classList.toggle("on",r.liked);
      const wrap=el.querySelector(".icwrap");
      if(wrap){wrap.innerHTML=r.liked?filledHeart():outlineHeart();}
      const c=el.querySelector("span");
      if(c){c.textContent=r.count>0?r.count:"";}
      if(r.liked){el.classList.remove("liked");void el.offsetWidth;el.classList.add("liked");}
    }catch(err){window.__toast(err.message);}
    return;
  }
  if(action==="retweet"){
    const id=el.dataset.id;
    try{
      const r=await api("/api/tweets/"+id+"/retweet",{method:"POST"});
      el.classList.toggle("on",r.retweeted);
      const c=el.querySelector("span");
      if(c){c.textContent=r.count>0?r.count:"";}
      window.__toast(r.retweeted?"Репост":"Репост отменён");
    }catch(err){window.__toast(err.message);}
    return;
  }
  if(action==="bookmark"){
    const id=el.dataset.id;
    try{
      const r=await api("/api/tweets/"+id+"/bookmark",{method:"POST"});
      el.classList.toggle("on",r.bookmarked);
      window.__toast(r.bookmarked?"Добавлено в закладки":"Удалено из закладок");
    }catch(err){window.__toast(err.message);}
    return;
  }
  if(action==="share"){
    const id=el.dataset.id;
    const url=location.origin+"/t/"+id;
    try{
      if(navigator.share){await navigator.share({url});}
      else{await navigator.clipboard.writeText(url);window.__toast("Ссылка скопирована");}
    }catch(err){}
    return;
  }
  if(action==="follow"){
    const username=el.dataset.username;
    try{
      const r=await api("/api/users/"+username+"/follow",{method:"POST"});
      el.classList.toggle("following",r.following);
      const label=el.querySelector(".lbl");
      if(label)label.textContent=r.following?"Читаю":"Читать";
      window.__toast(r.following?"Вы подписались":"Вы отписались");
    }catch(err){window.__toast(err.message);}
    return;
  }
  if(action==="delete-tweet"){
    const id=el.dataset.id;
    if(!confirm("Удалить этот пост?"))return;
    try{
      await api("/api/tweets/"+id,{method:"DELETE"});
      const node=document.getElementById("tweet-"+id);
      if(node)node.remove();
      window.__toast("Пост удалён");
    }catch(err){window.__toast(err.message);}
    return;
  }
  if(action==="pin"){
    const id=el.dataset.id;
    try{
      const r=await api("/api/tweets/"+id+"/pin",{method:"POST"});
      window.__toast(r.pinned?"Закреплено":"Откреплено");
      location.reload();
    }catch(err){window.__toast(err.message);}
    return;
  }
  if(action==="logout"){
    try{await api("/api/logout",{method:"POST"});}catch(e){}
    location.href="/";
    return;
  }
  if(action==="open-compose"){
    const modal=document.getElementById("compose-modal");
    if(modal){modal.classList.add("show");const ta=modal.querySelector("textarea");if(ta)ta.focus();}
    return;
  }
  if(action==="close-modal"){
    const m=el.closest(".modal-backdrop");
    if(m)m.classList.remove("show");
    return;
  }
  if(action==="open-reply"){
    const id=el.dataset.id;
    const username=el.dataset.username;
    const modal=document.getElementById("reply-modal");
    if(modal){
      modal.classList.add("show");
      modal.querySelector("[data-reply-to]").textContent="@"+username;
      modal.querySelector("form").dataset.parentId=id;
      const ta=modal.querySelector("textarea");
      if(ta){ta.value="";ta.focus();}
    }
    return;
  }
  if(action==="emoji"){
    const target=document.querySelector(el.dataset.target||"");
    if(target){target.value+=el.textContent;target.dispatchEvent(new Event("input"));target.focus();}
    return;
  }
});
function outlineHeart(){return '<svg class="ic" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/></svg>';}
function filledHeart(){return '<svg class="ic" width="22" height="22" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/></svg>';}
document.addEventListener("input",(e)=>{
  const ta=e.target;
  if(!ta.matches("[data-counter]"))return;
  const max=parseInt(ta.dataset.counter,10)||280;
  const form=ta.closest("form");
  const counter=form?form.querySelector(".char-counter"):null;
  const btn=form?form.querySelector("[type=submit]"):null;
  const len=ta.value.length;
  if(counter){
    counter.textContent=max-len;
    counter.classList.toggle("warn",len>max*0.8&&len<=max);
    counter.classList.toggle("danger",len>max);
  }
  if(btn)btn.disabled=len>max||len===0;
});
document.addEventListener("submit",async(e)=>{
  const form=e.target.closest("[data-ajax]");
  if(!form)return;
  e.preventDefault();
  const url=form.action;
  const data={};
  const fd=new FormData(form);
  for(const [k,v] of fd.entries())data[k]=v;
  if(form.dataset.parentId)data.parent_id=form.dataset.parentId;
  const submit=form.querySelector("[type=submit]");
  if(submit)submit.disabled=true;
  try{
    const r=await api(url,{method:"POST",body:JSON.stringify(data)});
    if(r.redirect){location.href=r.redirect;return;}
    if(r.tweet){
      window.__toast("Опубликовано");
      form.reset();
      const modal=form.closest(".modal-backdrop");
      if(modal)modal.classList.remove("show");
      if(location.pathname==="/"||location.pathname==="/explore"){
        setTimeout(()=>location.reload(),300);
      }else{
        setTimeout(()=>location.reload(),300);
      }
    }
    if(r.message){window.__toast(r.message);}
  }catch(err){window.__toast(err.message);}
  finally{if(submit)submit.disabled=false;}
});
document.addEventListener("keydown",(e)=>{
  if(e.key==="Escape"){
    document.querySelectorAll(".modal-backdrop.show").forEach(m=>m.classList.remove("show"));
  }
  if(e.key==="n"&&(e.ctrlKey||e.metaKey)&&!e.altKey){
    e.preventDefault();
    const btn=document.querySelector("[data-action=open-compose]");
    if(btn)btn.click();
  }
});
})();
"""


def avatar_html(user: Optional[dict], size_class: str = "") -> str:
    if not user:
        return f'<div class="avatar {size_class}" style="background:#536471">?</div>'
    return (
        f'<div class="avatar {size_class}" style="background:{user["avatar_color"]}">'
        f'{esc(initials(user.get("display_name") or user["username"]))}</div>'
    )


def verified_badge() -> str:
    return f'<span class="verified-badge">{icon("verified", 18)}</span>'


def layout(title: str, body: str, current_user: Optional[dict], active: str = "", hide_chrome: bool = False) -> str:
    theme = "dark"
    if current_user and current_user.get("theme") == "light":
        theme = "light"
    if hide_chrome:
        chrome = ""
        mobile_topbar = ""
        bottom_nav = ""
        fab = ""
    else:
        chrome = render_sidebar_left(current_user, active)
        mobile_topbar = render_mobile_topbar(current_user)
        bottom_nav = render_bottom_nav(current_user, active)
        fab = (
            f'<button class="fab" data-action="open-compose">{icon("feather", 24)}</button>'
        )
    return (
        '<!DOCTYPE html>'
        f'<html lang="ru" data-theme="{theme}">'
        '<head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">'
        f'<title>{esc(title)} · {APP_NAME}</title>'
        f'<link rel="icon" href="data:image/svg+xml,{quote_svg(favicon_svg())}">'
        f'<style>{CSS}</style>'
        '</head>'
        '<body>'
        f'{mobile_topbar}'
        '<div class="app">'
        f'{chrome}'
        f'<main class="main">{body}</main>'
        f'{render_sidebar_right(current_user, active)}'
        '</div>'
        f'{bottom_nav}'
        f'{fab}'
        f'{render_modals(current_user)}'
        '<div class="toast" id="toast"></div>'
        f'<script>{JS}</script>'
        '</body></html>'
    )


def quote_svg(svg: str) -> str:
    from urllib.parse import quote
    return quote(svg, safe="")


def favicon_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="8" fill="#1d9bf0"/>'
        '<path d="M9 24V8h4l6 9V8h4v16h-4l-6-9v9z" fill="#fff"/>'
        '</svg>'
    )


def render_sidebar_left(user: Optional[dict], active: str) -> str:
    if not user:
        return "<aside class='sidebar-left'></aside>"
    items = [
        ("home", "Главная", "/"),
        ("search", "Обзор", "/explore"),
        ("bell", "Уведомления", "/notifications"),
        ("mail", "Сообщения", "/messages"),
        ("bookmark", "Закладки", "/bookmarks"),
        ("user", "Профиль", f"/u/{user['username']}"),
    ]
    parts = ['<aside class="sidebar-left">']
    parts.append(f'<a href="/" class="logo">{icon("sparkles", 30)}</a>')
    unread_n = unread_notifications(user["id"])
    unread_m = unread_messages(user["id"])
    for key, label, href in items:
        cls = "nav-item"
        if active == key:
            cls += " active"
        badge = ""
        if key == "bell" and unread_n > 0:
            badge = f'<span class="badge">{unread_n if unread_n < 100 else "99+"}</span>'
        if key == "mail" and unread_m > 0:
            badge = f'<span class="badge">{unread_m if unread_m < 100 else "99+"}</span>'
        parts.append(
            f'<a href="{href}" class="{cls}">{icon(key, 26)}{badge}<span class="label">{label}</span></a>'
        )
    parts.append(
        '<button class="post-btn" data-action="open-compose">'
        '<span class="post-btn-text">Пост</span>'
        f'<span class="post-btn-ico">{icon("feather", 24)}</span>'
        '</button>'
    )
    parts.append(
        '<div class="sidebar-user" data-action="theme" title="Сменить тему">'
        f'{avatar_html(user)}'
        f'<div class="who"><b>{esc(user.get("display_name") or user["username"])}</b>'
        f'<span>@{esc(user["username"])}</span></div>'
        f'<span class="more">{icon("more", 20)}</span>'
        '</div>'
    )
    parts.append('</aside>')
    return "".join(parts)


def render_mobile_topbar(user: Optional[dict]) -> str:
    if not user:
        return ""
    return (
        '<div class="mobile-topbar">'
        f'<a href="/u/{esc(user["username"])}">{avatar_html(user)}</a>'
        f'<a href="/" class="brand">{icon("sparkles", 24)} {APP_NAME}</a>'
        '<span style="flex:1"></span>'
        f'<a href="/settings" class="icon-btn">{icon("settings", 22)}</a>'
        '</div>'
    )


def render_bottom_nav(user: Optional[dict], active: str) -> str:
    if not user:
        return ""
    items = [
        ("home", "home", "/"),
        ("search", "search", "/explore"),
        ("bell", "bell", "/notifications"),
        ("mail", "mail", "/messages"),
        ("user", "user", f"/u/{user['username']}"),
    ]
    parts = ['<nav class="bottom-nav">']
    unread_n = unread_notifications(user["id"])
    unread_m = unread_messages(user["id"])
    for key, ic, href in items:
        cls = "active" if active == key else ""
        badge = ""
        if key == "bell" and unread_n > 0:
            badge = f'<span class="badge">{unread_n if unread_n < 100 else "99+"}</span>'
        if key == "mail" and unread_m > 0:
            badge = f'<span class="badge">{unread_m if unread_m < 100 else "99+"}</span>'
        parts.append(f'<a href="{href}" class="{cls}">{icon(ic, 26)}{badge}</a>')
    parts.append('</nav>')
    return "".join(parts)


def render_sidebar_right(user: Optional[dict], active: str) -> str:
    if not user:
        return "<aside class='sidebar-right'></aside>"
    parts = ['<aside class="sidebar-right">']
    parts.append(
        '<form class="search-box" action="/search" method="get">'
        f'{icon("search", 20)}'
        '<input type="text" name="q" placeholder="Поиск в Chirp" autocomplete="off">'
        '</form>'
    )
    trending = trending_hashtags(6)
    if trending:
        parts.append('<div class="card"><h3>Актуальные темы</h3>')
        for tag, cnt in trending:
            parts.append(
                f'<a href="/search?q=%23{esc(tag)}" class="card-item" style="display:block">'
                f'<div class="num">Актуально</div>'
                f'<div class="tag">#{esc(tag)}</div>'
                f'<div class="num">{cnt} постов</div>'
                '</a>'
            )
        parts.append('</div>')
    else:
        parts.append(
            '<div class="card"><h3>Актуальные темы</h3>'
            '<div style="padding:16px;color:var(--text-dim);font-size:14px">'
            'Пока нет трендов. Начните постить с хэштегами!'
            '</div></div>'
        )
    sug = suggested_users(user["id"], 3)
    if sug:
        parts.append('<div class="card"><h3>Кого читать</h3>')
        for u in sug:
            parts.append(render_user_card(u, user))
        parts.append('</div>')
    parts.append(
        '<div class="card"><h3>О сервисе</h3>'
        '<div style="padding:0 16px 16px;color:var(--text-dim);font-size:13px;line-height:1.5">'
        f'{APP_NAME} · мини-версия соцсети. Работает целиком на сервере, хранит всё в памяти. '
        'Ctrl+N — быстрый пост.'
        '</div></div>'
    )
    parts.append('</aside>')
    return "".join(parts)


def render_user_card(u: dict, viewer: Optional[dict]) -> str:
    is_me = viewer and viewer["id"] == u["id"]
    if is_me:
        btn = ""
    elif viewer and is_following(viewer["id"], u["id"]):
        btn = (
            f'<button class="follow-btn following" data-action="follow" data-username="{esc(u["username"])}">'
            '<span class="lbl">Читаю</span></button>'
        )
    else:
        btn = (
            f'<button class="follow-btn" data-action="follow" data-username="{esc(u["username"])}">'
            '<span class="lbl">Читать</span></button>'
        )
    return (
        f'<div class="card-item" onclick="location.href=\'/u/{esc(u["username"])}\'">'
        f'{avatar_html(u)}'
        f'<div style="flex:1;min-width:0">'
        f'<div style="font-weight:700;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
        f'{esc(u.get("display_name") or u["username"])}'
        f'{verified_badge() if u.get("verified") else ""}'
        f'</div>'
        f'<div style="color:var(--text-dim);font-size:14px">@{esc(u["username"])}</div>'
        f'</div>'
        f'{btn}'
        '</div>'
    )


def render_tweet(
    tweet: dict,
    viewer: Optional[dict],
    show_actions: bool = True,
    thread_mode: bool = False,
    show_context: bool = False,
) -> str:
    author = USERS.get(tweet["user_id"])
    if not author:
        return ""
    t_id = tweet["id"]
    liked = viewer and t_id in LIKES_BY_USER.get(viewer["id"], set())
    retweeted = viewer and t_id in RETWEETS_BY_USER.get(viewer["id"], set())
    bookmarked = viewer and t_id in BOOKMARKS.get(viewer["id"], set())
    likes_count = len(LIKES_BY_TWEET.get(t_id, set()))
    rt_count = len(RETWEETS_BY_TWEET.get(t_id, set()))
    replies = reply_ids(t_id)
    is_me = viewer and viewer["id"] == author["id"]
    is_pinned = viewer and viewer.get("pinned") == t_id

    parts = []
    if show_context and tweet.get("parent_id") and tweet["parent_id"] in TWEETS:
        parent = TWEETS[tweet["parent_id"]]
        pauthor = USERS.get(parent["user_id"])
        if pauthor:
            parts.append(
                f'<div class="reply-context">В ответ '
                f'<a href="/u/{esc(pauthor["username"])}">@{esc(pauthor["username"])}</a></div>'
            )
    if show_context and tweet.get("retweet_of") and tweet["retweet_of"] in TWEETS:
        parts.append(
            f'<div class="repost-label">{icon("repost", 16)} Репост</div>'
        )
    if is_pinned:
        parts.append(
            f'<div class="pin-label">{icon("pin", 14)} Закреплено</div>'
        )

    like_svg = (
        '<svg class="ic" width="22" height="22" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/></svg>'
        if liked
        else icon("heart", 22)
    )
    menu = ""
    if is_me:
        menu = (
            f'<div class="menu-wrap" onclick="event.stopPropagation()">'
            f'<button class="menu" data-action="pin" data-id="{t_id}" title="Закрепить">{icon("pin", 18)}</button>'
            f'<button class="menu" data-action="delete-tweet" data-id="{t_id}" title="Удалить">{icon("trash", 18)}</button>'
            f'</div>'
        )
    else:
        menu = f'<button class="menu">{icon("more", 18)}</button>'

    media = ""
    if tweet.get("image_url"):
        media = (
            f'<div class="tweet-media"><img src="{esc(tweet["image_url"])}" alt="" loading="lazy" '
            'onerror="this.style.display=\'none\'"></div>'
        )

    quote = ""
    if tweet.get("retweet_of"):
        orig = TWEETS.get(tweet["retweet_of"])
        if orig:
            oauthor = USERS.get(orig["user_id"])
            if oauthor:
                quote = (
                    f'<div class="tweet-quote" onclick="event.stopPropagation();location.href=\'/t/{orig["id"]}\'">'
                    f'<div style="display:flex;gap:8px;align-items:center;font-size:14px">'
                    f'{avatar_html(oauthor, "xs")}'
                    f'<b>{esc(oauthor.get("display_name") or oauthor["username"])}</b>'
                    f'{verified_badge() if oauthor.get("verified") else ""}'
                    f'<span style="color:var(--text-dim)">@{esc(oauthor["username"])} · {fmt_time(orig["created_at"])}</span>'
                    f'</div>'
                    f'<div style="margin-top:8px;font-size:14px;white-space:pre-wrap">{linkify(orig["text"])}</div>'
                    f'</div>'
                )

    actions_html = ""
    if show_actions:
        actions_html = (
            '<div class="tweet-actions">'
            f'<button class="act reply" data-action="open-reply" data-id="{t_id}" data-username="{esc(author["username"])}" onclick="event.stopPropagation()">'
            f'<span class="icwrap">{icon("comment", 20)}</span><span>{len(replies) if replies else ""}</span></button>'
            f'<button class="act repost {"on" if retweeted else ""}" data-action="retweet" data-id="{t_id}" onclick="event.stopPropagation()">'
            f'<span class="icwrap">{icon("repost", 20)}</span><span>{rt_count if rt_count else ""}</span></button>'
            f'<button class="act like {"on" if liked else ""}" data-action="like" data-id="{t_id}" onclick="event.stopPropagation()">'
            f'<span class="icwrap">{like_svg}</span><span>{likes_count if likes_count else ""}</span></button>'
            f'<button class="act book {"on" if bookmarked else ""}" data-action="bookmark" data-id="{t_id}" onclick="event.stopPropagation()">'
            f'<span class="icwrap">{icon("bookmark", 20)}</span></button>'
            f'<button class="act share" data-action="share" data-id="{t_id}" onclick="event.stopPropagation()">'
            f'<span class="icwrap">{icon("share", 20)}</span></button>'
            '</div>'
        )

    head_name = (
        f'<a href="/u/{esc(author["username"])}" onclick="event.stopPropagation()">'
        f'<b>{esc(author.get("display_name") or author["username"])}</b></a>'
        f'{verified_badge() if author.get("verified") else ""}'
        f'<a href="/u/{esc(author["username"])}" onclick="event.stopPropagation()" class="handle">@{esc(author["username"])}</a>'
        f'<span class="dot">·</span>'
        f'<a href="/t/{t_id}" class="time" onclick="event.stopPropagation()">{fmt_time(tweet["created_at"])}</a>'
    )

    body = (
        f'<div class="tweet-main">'
        f'<div class="tweet-head">{head_name}{menu}</div>'
        f'<div class="tweet-text">{linkify(tweet["text"])}</div>'
        f'{media}{quote}{actions_html}'
        f'</div>'
    )

    cls = "tweet"
    if thread_mode:
        cls += " thread"
    return (
        f'<article class="{cls}" id="tweet-{t_id}" onclick="location.href=\'/t/{t_id}\'">'
        f'<a href="/u/{esc(author["username"])}" onclick="event.stopPropagation()">{avatar_html(author)}</a>'
        f'{body}'
        '</article>'
    )


def render_tweet_list(tweets: List[dict], viewer: Optional[dict]) -> str:
    if not tweets:
        return ""
    return "".join(render_tweet(t, viewer, show_context=True) for t in tweets)


def render_modals(viewer: Optional[dict]) -> str:
    if not viewer:
        return ""
    return (
        '<div class="modal-backdrop" id="compose-modal">'
        '<div class="modal">'
        '<div class="modal-header">'
        '<button class="modal-close" data-action="close-modal">'
        f'{icon("close", 22)}</button>'
        '<h2 style="font-size:18px;font-weight:700;margin-left:8px">Новый пост</h2>'
        '</div>'
        f'<form data-ajax action="/api/tweets" method="post" style="padding:16px">'
        f'<div style="display:flex;gap:12px">'
        f'{avatar_html(viewer)}'
        '<div style="flex:1;min-width:0">'
        '<textarea name="text" data-counter="280" placeholder="Что происходит?" '
        'style="width:100%;font-size:18px;min-height:120px;resize:none;padding-top:6px"></textarea>'
        '<div class="field" style="margin-top:8px">'
        '<label>Ссылка на картинку (необязательно)</label>'
        '<input type="text" name="image_url" placeholder="https://...">'
        '</div>'
        '<div class="composer-actions">'
        f'<div class="composer-tools">{icon("image", 22)}</div>'
        '<div style="display:flex;align-items:center;gap:12px">'
        '<span class="char-counter">280</span>'
        '<button type="submit" class="composer-submit" disabled>Опубликовать</button>'
        '</div></div></div></div></form>'
        '</div></div>'
        '<div class="modal-backdrop" id="reply-modal">'
        '<div class="modal">'
        '<div class="modal-header">'
        '<button class="modal-close" data-action="close-modal">'
        f'{icon("close", 22)}</button>'
        '<h2 style="font-size:18px;font-weight:700;margin-left:8px">Ответить</h2>'
        '</div>'
        f'<form data-ajax action="/api/tweets" method="post" style="padding:16px">'
        f'<div style="display:flex;gap:12px">'
        f'{avatar_html(viewer)}'
        '<div style="flex:1;min-width:0">'
        '<div style="color:var(--text-dim);font-size:14px;margin-bottom:4px">В ответ <span data-reply-to></span></div>'
        '<textarea name="text" data-counter="280" placeholder="Ваш ответ" '
        'style="width:100%;font-size:18px;min-height:100px;resize:none;padding-top:6px"></textarea>'
        '<div class="composer-actions">'
        '<div class="composer-tools"></div>'
        '<div style="display:flex;align-items:center;gap:12px">'
        '<span class="char-counter">280</span>'
        '<button type="submit" class="composer-submit" disabled>Ответить</button>'
        '</div></div></div></div></form>'
        '</div></div>'
    )


def render_composer(user: dict, placeholder: str = "Что происходит?") -> str:
    return (
        f'<form class="composer" data-ajax action="/api/tweets" method="post">'
        f'{avatar_html(user)}'
        '<div class="composer-body">'
        f'<textarea name="text" data-counter="280" placeholder="{esc(placeholder)}"></textarea>'
        '<div class="composer-actions">'
        f'<div class="composer-tools">{icon("image", 22)}</div>'
        '<div style="display:flex;align-items:center;gap:12px">'
        '<span class="char-counter">280</span>'
        '<button type="submit" class="composer-submit" disabled>Опубликовать</button>'
        '</div></div></div></form>'
    )


class RegisterBody(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    email: str
    password: str = Field(..., min_length=6)
    display_name: Optional[str] = ""


class LoginBody(BaseModel):
    username: str
    password: str


class TweetBody(BaseModel):
    text: str = ""
    image_url: Optional[str] = None
    parent_id: Optional[str] = None


class MessageBody(BaseModel):
    text: str


class ProfileBody(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None


class ThemeBody(BaseModel):
    theme: str


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    feed = user_timeline(user["id"])
    composer = render_composer(user)
    if feed:
        feed_html = render_tweet_list(feed, user)
    else:
        feed_html = (
            '<div class="empty"><h3>Добро пожаловать в '
            f'{APP_NAME}!</h3>'
            '<p>Начните подписываться на интересных людей или создайте свой первый пост.</p>'
            '</div>'
        )
    body = (
        '<div class="topbar"><h1>Главная</h1></div>'
        f'{composer}{feed_html}'
    )
    return HTMLResponse(layout("Главная", body, user, active="home"))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None, mode: str = "login"):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/")
    err_html = ""
    if error:
        err_html = f'<div class="error">{esc(error)}</div>'
    if mode == "register":
        form = (
            '<h1>Создать аккаунт</h1>'
            f'{err_html}'
            '<form data-ajax action="/api/register" method="post">'
            '<div class="field"><label>Отображаемое имя</label>'
            '<input type="text" name="display_name" maxlength="40"></div>'
            '<div class="field"><label>Ник (username)</label>'
            '<input type="text" name="username" required autocomplete="username" maxlength="20"></div>'
            '<div class="field"><label>Email</label>'
            '<input type="email" name="email" required autocomplete="email"></div>'
            '<div class="field"><label>Пароль</label>'
            '<input type="password" name="password" required autocomplete="new-password" minlength="6"></div>'
            '<button type="submit" class="btn-primary">Зарегистрироваться</button>'
            '</form>'
            '<div class="switch">Уже есть аккаунт? '
            '<a href="/login">Войти</a></div>'
        )
        title = "Регистрация"
    else:
        form = (
            '<h1>Войти в Chirp</h1>'
            f'{err_html}'
            '<form data-ajax action="/api/login" method="post">'
            '<div class="field"><label>Ник или email</label>'
            '<input type="text" name="username" required autocomplete="username"></div>'
            '<div class="field"><label>Пароль</label>'
            '<input type="password" name="password" required autocomplete="current-password"></div>'
            '<button type="submit" class="btn-primary">Войти</button>'
            '</form>'
            '<div class="switch">Нет аккаунта? '
            '<a href="/login?mode=register">Создать</a></div>'
        )
        title = "Вход"
    body = (
        '<div class="auth-wrap">'
        '<div class="auth-card">'
        f'<div class="brand">{icon("sparkles", 32)} {APP_NAME}</div>'
        f'{form}'
        '</div></div>'
    )
    return HTMLResponse(layout(title, body, None, hide_chrome=True))


@app.get("/explore", response_class=HTMLResponse)
async def explore_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    feed = global_timeline(80)
    if feed:
        feed_html = render_tweet_list(feed, user)
    else:
        feed_html = '<div class="empty"><h3>Пока пусто</h3><p>Никто ещё не написал ни одного поста.</p></div>'
    tags = trending_hashtags(12)
    tags_html = ""
    if tags:
        pills = "".join(
            f'<a class="pill" href="/search?q=%23{esc(t)}">#{esc(t)} · {c}</a>' for t, c in tags
        )
        tags_html = f'<div style="padding:16px;border-bottom:1px solid var(--border)">{pills}</div>'
    body = (
        '<div class="topbar"><h1>Обзор</h1></div>'
        f'{tags_html}{feed_html}'
    )
    return HTMLResponse(layout("Обзор", body, user, active="search"))


@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    notifs = list(NOTIFICATIONS.get(user["id"], []))
    notifs.sort(key=lambda n: n["created_at"], reverse=True)
    for n in notifs:
        n["read"] = True
    if not notifs:
        body = (
            '<div class="topbar"><h1>Уведомления</h1></div>'
            '<div class="empty"><h3>Пока тихо</h3>'
            '<p>Здесь появятся уведомления о лайках, репостах, ответах и подписках.</p></div>'
        )
        return HTMLResponse(layout("Уведомления", body, user, active="bell"))
    parts = ['<div class="topbar"><h1>Уведомления</h1></div>']
    for n in notifs[:100]:
        actor = USERS.get(n["actor_id"])
        if not actor:
            continue
        tweet = TWEETS.get(n.get("tweet_id")) if n.get("tweet_id") else None
        icon_name, icon_cls, text = "bell", "", ""
        if n["kind"] == "like":
            icon_name, icon_cls, text = "heart", "like", "оценил(а) ваш пост"
        elif n["kind"] == "retweet":
            icon_name, icon_cls, text = "repost", "repost", "сделал(а) репост"
        elif n["kind"] == "follow":
            icon_name, icon_cls, text = "user", "follow", "подписался(ась) на вас"
        elif n["kind"] == "reply":
            icon_name, icon_cls, text = "comment", "", "ответил(а) на ваш пост"
        elif n["kind"] == "mention":
            icon_name, icon_cls, text = "at", "", "упомянул(а) вас"
        preview = ""
        if tweet:
            preview = f'<div class="preview">{linkify(tweet["text"])}</div>'
        link = f'/t/{tweet["id"]}' if tweet else f'/u/{actor["username"]}'
        parts.append(
            f'<a class="notif-row {"unread" if not n.get("read") else ""}" href="{link}">'
            f'<div class="notif-ic {icon_cls}">{icon(icon_name, 24)}</div>'
            f'{avatar_html(actor, "sm")}'
            f'<div class="notif-body">'
            f'<b>{esc(actor.get("display_name") or actor["username"])}</b> {text}'
            f'<span class="time">{fmt_time(n["created_at"])}</span>'
            f'{preview}'
            f'</div></a>'
        )
    body = "".join(parts)
    return HTMLResponse(layout("Уведомления", body, user, active="bell"))


@app.get("/bookmarks", response_class=HTMLResponse)
async def bookmarks_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    bm_ids = BOOKMARKS.get(user["id"], set())
    tweets = [TWEETS[i] for i in bm_ids if i in TWEETS]
    tweets.sort(key=lambda t: t["created_at"], reverse=True)
    if tweets:
        feed = render_tweet_list(tweets, user)
    else:
        feed = (
            '<div class="empty"><h3>Сохраняйте лучшее</h3>'
            '<p>Нажмите на иконку закладки под постом, чтобы сохранить его здесь.</p></div>'
        )
    body = f'<div class="topbar"><h1>Закладки</h1></div>{feed}'
    return HTMLResponse(layout("Закладки", body, user, active="bookmark"))


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    convs = {}
    for key, msgs in MESSAGES.items():
        parts = key.split("|")
        if user["id"] not in parts:
            continue
        other = parts[0] if parts[1] == user["id"] else parts[1]
        if other not in USERS:
            continue
        last = max(msgs, key=lambda m: m["created_at"]) if msgs else None
        convs[other] = last
    rows = []
    for other, last in sorted(convs.items(), key=lambda x: x[1]["created_at"] if x[1] else now(), reverse=True):
        u = USERS.get(other)
        if not u:
            continue
        preview = ""
        if last:
            pre = "Вы: " if last["from_id"] == user["id"] else ""
            preview = f'{pre}{esc(last["text"][:60])}'
        rows.append(
            f'<a class="card-item" href="/messages/{u["username"]}">'
            f'{avatar_html(u)}'
            f'<div style="flex:1;min-width:0">'
            f'<div style="display:flex;justify-content:space-between">'
            f'<b>{esc(u.get("display_name") or u["username"])}</b>'
            f'<span style="color:var(--text-dim);font-size:13px">{fmt_time(last["created_at"]) if last else ""}</span>'
            f'</div>'
            f'<div style="color:var(--text-dim);font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{preview}</div>'
            f'</div></a>'
        )
    if rows:
        body = f'<div class="topbar"><h1>Сообщения</h1></div><div class="card">{"".join(rows)}</div>'
    else:
        body = (
            '<div class="topbar"><h1>Сообщения</h1></div>'
            '<div class="empty"><h3>Нет сообщений</h3>'
            '<p>Начните беседу, зайдя в профиль интересного человека.</p></div>'
        )
    return HTMLResponse(layout("Сообщения", body, user, active="mail"))


@app.get("/messages/{username}", response_class=HTMLResponse)
async def chat_page(request: Request, username: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    other = get_user_by_username(username)
    if not other:
        raise HTTPException(404)
    msgs = list(get_conversation(user["id"], other["id"]))
    msgs.sort(key=lambda m: m["created_at"])
    for m in msgs:
        if m["to_id"] == user["id"]:
            m["read"] = True
    msgs_html = []
    for m in msgs:
        cls = "out" if m["from_id"] == user["id"] else "in"
        msgs_html.append(
            f'<div class="msg {cls}">{linkify(m["text"])}'
            f'<div class="msg-time">{fmt_time(m["created_at"])}</div></div>'
        )
    chat_area = "".join(msgs_html) or '<div class="empty"><p>Пока нет сообщений. Поздоровайтесь!</p></div>'
    body = (
        '<div class="topbar">'
        f'<a href="/messages" class="back-btn">{icon("arrow_left", 22)}</a>'
        f'{avatar_html(other, "sm")}'
        f'<div style="margin-left:4px">'
        f'<div style="font-weight:700">{esc(other.get("display_name") or other["username"])}</div>'
        f'<div style="color:var(--text-dim);font-size:13px">@{esc(other["username"])}</div>'
        f'</div></div>'
        f'<div style="display:flex;flex-direction:column;padding:16px 12px;gap:4px" id="chat-area">'
        f'{chat_area}</div>'
        f'<form class="chat-input" data-ajax action="/api/messages/{esc(other["username"])}" method="post">'
        '<div class="field" style="padding:10px 14px">'
        '<input type="text" name="text" placeholder="Начните сообщение" autocomplete="off" required></div>'
        f'<button type="submit">{icon("send", 20)}</button>'
        '</form>'
    )
    return HTMLResponse(layout(f"Чат с @{other['username']}", body, user, active="mail"))


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = ""):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    users = search_users(q)
    tweets = search_tweets(q)
    parts = [
        '<div class="topbar">'
        '<form action="/search" method="get" style="flex:1">'
        '<div class="search-box" style="margin:0">'
        f'{icon("search", 20)}'
        f'<input type="text" name="q" value="{esc(q)}" placeholder="Поиск" autofocus>'
        '</div></form></div>'
    ]
    if not q:
        parts.append(
            '<div class="empty"><h3>Что ищем?</h3>'
            '<p>Введите запрос, чтобы найти посты и людей.</p></div>'
        )
    else:
        if users:
            parts.append('<div style="padding:16px 16px 8px;font-weight:800;font-size:19px">Люди</div>')
            for u in users[:6]:
                parts.append(render_user_card(u, user))
        if tweets:
            parts.append('<div style="padding:16px 16px 8px;font-weight:800;font-size:19px">Посты</div>')
            parts.append(render_tweet_list(tweets, user))
        if not users and not tweets:
            parts.append(
                f'<div class="empty"><h3>Ничего не найдено</h3>'
                f'<p>По запросу «{esc(q)}» ничего нет.</p></div>'
            )
    return HTMLResponse(layout("Поиск", "".join(parts), user, active="search"))


@app.get("/u/{username}", response_class=HTMLResponse)
async def profile_page(request: Request, username: str, tab: str = "posts"):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    profile = get_user_by_username(username)
    if not profile:
        raise HTTPException(404)
    is_me = profile["id"] == user["id"]
    following = is_following(user["id"], profile["id"])
    followers_count = len(FOLLOWERS.get(profile["id"], set()))
    following_count = len(FOLLOWING.get(profile["id"], set()))
    tweets_count = len([t for t in TWEETS.values() if t["user_id"] == profile["id"] and not t.get("parent_id")])

    if is_me:
        action_btn = (
            '<a href="/settings" class="btn-outline">Изменить профиль</a>'
        )
    elif following:
        action_btn = (
            f'<button class="follow-btn following" data-action="follow" data-username="{esc(profile["username"])}">'
            '<span class="lbl">Читаю</span></button>'
        )
    else:
        action_btn = (
            f'<button class="follow-btn" data-action="follow" data-username="{esc(profile["username"])}">'
            '<span class="lbl">Читать</span></button>'
        )
    msg_btn = ""
    if not is_me:
        msg_btn = f'<a href="/messages/{esc(profile["username"])}" class="btn-outline">{icon("mail", 18)}</a>'

    banner_style = f'style="background:linear-gradient(135deg,{profile["avatar_color"]},#8b5cf6)"'

    bio_html = f'<div class="profile-bio">{linkify(profile["bio"])}</div>' if profile["bio"] else ""
    meta_bits = []
    if profile["location"]:
        meta_bits.append(f'{icon("pin", 16)} {esc(profile["location"])}')
    if profile["website"]:
        website = profile["website"]
        if not website.startswith("http"):
            website = "https://" + website
        meta_bits.append(
            f'<a href="{esc(website)}" target="_blank" rel="noopener" class="tlink">{esc(profile["website"])}</a>'
        )
    meta_bits.append(
        f'{icon("user", 16)} Регистрация: {profile["created_at"].strftime("%b %Y")}'
    )
    meta_html = f'<div class="profile-meta">{"".join(meta_bits)}</div>'

    head = (
        '<div class="topbar">'
        '<button class="back-btn" onclick="history.back()">'
        f'{icon("arrow_left", 22)}</button>'
        f'<div><h1 style="margin:0">{esc(profile.get("display_name") or profile["username"])}</h1>'
        f'<div style="font-size:13px;color:var(--text-dim)">{tweets_count} постов</div></div>'
        '</div>'
    )
    head_html = (
        '<div class="profile-head">'
        f'<div class="profile-banner" {banner_style}></div>'
        '<div class="profile-top">'
        f'{avatar_html(profile, "huge")}'
        f'<div class="profile-actions">{msg_btn}{action_btn}</div>'
        '</div>'
        '<div class="profile-info">'
        f'<h2>{esc(profile.get("display_name") or profile["username"])}'
        f'{verified_badge() if profile.get("verified") else ""}</h2>'
        f'<div class="h">@{esc(profile["username"])}</div>'
        f'{bio_html}{meta_html}'
        f'<div class="profile-stats">'
        f'<a href="/u/{esc(profile["username"])}?tab=following"><b>{following_count}</b> читаю</a>'
        f'<a href="/u/{esc(profile["username"])}?tab=followers"><b>{followers_count}</b> читателей</a>'
        '</div>'
        '</div>'
    )

    tabs = (
        '<div class="tabs">'
        f'<a class="tab {"active" if tab == "posts" else ""}" href="/u/{esc(profile["username"])}?tab=posts">Посты</a>'
        f'<a class="tab {"active" if tab == "replies" else ""}" href="/u/{esc(profile["username"])}?tab=replies">Ответы</a>'
        f'<a class="tab {"active" if tab == "media" else ""}" href="/u/{esc(profile["username"])}?tab=media">Медиа</a>'
        f'<a class="tab {"active" if tab == "likes" else ""}" href="/u/{esc(profile["username"])}?tab=likes">Нравится</a>'
        '</div>'
    )

    content = ""
    if tab == "posts":
        items = user_tweets(profile["id"])
        if profile.get("pinned") and profile["pinned"] in TWEETS:
            pin = TWEETS[profile["pinned"]]
            items = [pin] + [t for t in items if t["id"] != pin["id"]]
        content = render_tweet_list(items, user) or (
            '<div class="empty"><h3>Пока пусто</h3><p>Здесь появятся посты пользователя.</p></div>'
        )
    elif tab == "replies":
        items = user_replies(profile["id"])
        content = render_tweet_list(items, user) or (
            '<div class="empty"><h3>Пока нет ответов</h3></div>'
        )
    elif tab == "media":
        items = user_media(profile["id"])
        content = render_tweet_list(items, user) or (
            '<div class="empty"><h3>Нет медиа</h3></div>'
        )
    elif tab == "likes":
        items = user_likes(profile["id"])
        content = render_tweet_list(items, user) or (
            '<div class="empty"><h3>Нет лайков</h3></div>'
        )
    elif tab == "followers":
        items = [USERS[i] for i in FOLLOWERS.get(profile["id"], set()) if i in USERS]
        if items:
            content = "".join(render_user_card(u, user) for u in items)
        else:
            content = '<div class="empty"><h3>Нет читателей</h3></div>'
    elif tab == "following":
        items = [USERS[i] for i in FOLLOWING.get(profile["id"], set()) if i in USERS]
        if items:
            content = "".join(render_user_card(u, user) for u in items)
        else:
            content = '<div class="empty"><h3>Ни на кого не подписан</h3></div>'

    body = head + head_html + tabs + content
    return HTMLResponse(layout(f"@{profile['username']}", body, user, active="user"))


@app.get("/t/{tweet_id}", response_class=HTMLResponse)
async def tweet_page(request: Request, tweet_id: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    tweet = TWEETS.get(tweet_id)
    if not tweet:
        raise HTTPException(404)
    thread = build_thread(tweet_id)
    replies = [TWEETS[i] for i in reply_ids(tweet_id)]
    replies.sort(key=lambda t: t["created_at"])

    thread_html = []
    for i, t in enumerate(thread):
        thread_html.append(render_tweet(t, user, thread_mode=True, show_context=False))
    reply_html = []
    if replies:
        for r in replies:
            reply_html.append(render_tweet(r, user, show_context=True))
    else:
        reply_html.append(
            '<div class="empty"><p style="font-size:14px">Пока нет ответов. Будьте первым!</p></div>'
        )
    author = USERS.get(tweet["user_id"])
    head = (
        '<div class="topbar">'
        '<button class="back-btn" onclick="history.back()">'
        f'{icon("arrow_left", 22)}</button>'
        '<h1>Пост</h1></div>'
    )
    reply_composer = (
        f'<form class="composer" data-ajax action="/api/tweets" method="post" data-parent-id="{tweet_id}">'
        f'{avatar_html(user)}'
        '<div class="composer-body">'
        '<textarea name="text" data-counter="280" placeholder="Ответить"></textarea>'
        '<div class="composer-actions">'
        '<div class="composer-tools"></div>'
        '<div style="display:flex;align-items:center;gap:12px">'
        '<span class="char-counter">280</span>'
        '<button type="submit" class="composer-submit" disabled>Ответить</button>'
        '</div></div></div></form>'
    )
    body = head + "".join(thread_html) + reply_composer + "".join(reply_html)
    return HTMLResponse(layout("Пост", body, user))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    body = (
        '<div class="topbar">'
        '<button class="back-btn" onclick="history.back()">'
        f'{icon("arrow_left", 22)}</button><h1>Настройки</h1></div>'
        '<form data-ajax action="/api/profile" method="post" style="padding:16px">'
        '<div class="field"><label>Отображаемое имя</label>'
        f'<input type="text" name="display_name" value="{esc(user.get("display_name") or "")}"></div>'
        '<div class="field"><label>О себе</label>'
        f'<textarea name="bio" maxlength="160">{esc(user.get("bio") or "")}</textarea></div>'
        '<div class="field"><label>Локация</label>'
        f'<input type="text" name="location" value="{esc(user.get("location") or "")}"></div>'
        '<div class="field"><label>Сайт</label>'
        f'<input type="text" name="website" value="{esc(user.get("website") or "")}"></div>'
        '<button type="submit" class="btn-primary">Сохранить</button>'
        '</form>'
        '<div class="settings-row">'
        '<div><b>Тема</b><span>Тёмная или светлая</span></div>'
        f'<button class="btn-outline" data-action="theme">{icon("moon", 18)} Сменить</button>'
        '</div>'
        '<div class="settings-row">'
        '<div><b>Выйти из аккаунта</b><span>Завершить эту сессию</span></div>'
        f'<button class="btn-outline" data-action="logout">{icon("logout", 18)} Выйти</button>'
        '</div>'
        '<div style="padding:24px 16px;color:var(--text-dim);font-size:13px">'
        f'{APP_NAME} работает в памяти. Данные не сохраняются между перезапусками сервера.'
        '</div>'
    )
    return HTMLResponse(layout("Настройки", body, user))


@app.post("/api/register")
async def api_register(response: Response, body: RegisterBody):
    try:
        user = create_user(body.username, body.email, body.password, body.display_name or body.username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    token = create_session(user["id"])
    resp = JSONResponse({"ok": True, "redirect": "/"})
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/api/login")
async def api_login(request: Request, body: LoginBody):
    ident = (body.username or "").strip()
    user = None
    if "@" in ident:
        uid = EMAILS.get(ident.lower())
        if uid:
            user = USERS.get(uid)
    else:
        user = get_user_by_username(ident)
    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(400, "Неверный логин или пароль")
    token = create_session(user["id"])
    resp = JSONResponse({"ok": True, "redirect": "/"})
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        SESSIONS.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.post("/api/tweets")
async def api_create_tweet(request: Request, body: TweetBody):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    text = (body.text or "").strip()
    image = (body.image_url or "").strip() or None
    if not text and not image:
        raise HTTPException(400, "Пустой пост")
    if len(text) > MAX_TWEET_LEN:
        raise HTTPException(400, f"Максимум {MAX_TWEET_LEN} символов")
    parent_id = body.parent_id if body.parent_id and body.parent_id in TWEETS else None
    tweet = create_tweet(user["id"], text, parent_id=parent_id, image_url=image)
    return {"ok": True, "tweet": {"id": tweet["id"]}}


@app.delete("/api/tweets/{tweet_id}")
async def api_delete_tweet(request: Request, tweet_id: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    if not delete_tweet(tweet_id, user["id"]):
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/tweets/{tweet_id}/like")
async def api_like(request: Request, tweet_id: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    if tweet_id not in TWEETS:
        raise HTTPException(404)
    liked = toggle_like(tweet_id, user["id"])
    return {"ok": True, "liked": liked, "count": len(LIKES_BY_TWEET.get(tweet_id, set()))}


@app.post("/api/tweets/{tweet_id}/retweet")
async def api_retweet(request: Request, tweet_id: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    if tweet_id not in TWEETS:
        raise HTTPException(404)
    retweeted = toggle_retweet(tweet_id, user["id"])
    return {"ok": True, "retweeted": retweeted, "count": len(RETWEETS_BY_TWEET.get(tweet_id, set()))}


@app.post("/api/tweets/{tweet_id}/bookmark")
async def api_bookmark(request: Request, tweet_id: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    if tweet_id not in TWEETS:
        raise HTTPException(404)
    bookmarked = toggle_bookmark(tweet_id, user["id"])
    return {"ok": True, "bookmarked": bookmarked}


@app.post("/api/tweets/{tweet_id}/pin")
async def api_pin(request: Request, tweet_id: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    tw = TWEETS.get(tweet_id)
    if not tw or tw["user_id"] != user["id"]:
        raise HTTPException(404)
    if user.get("pinned") == tweet_id:
        user["pinned"] = None
        return {"ok": True, "pinned": False}
    user["pinned"] = tweet_id
    return {"ok": True, "pinned": True}


@app.post("/api/users/{username}/follow")
async def api_follow(request: Request, username: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    target = get_user_by_username(username)
    if not target:
        raise HTTPException(404)
    if target["id"] == user["id"]:
        raise HTTPException(400, "Нельзя подписаться на себя")
    following = toggle_follow(target["id"], user["id"])
    return {"ok": True, "following": following}


@app.post("/api/messages/{username}")
async def api_send_message(request: Request, username: str, body: MessageBody):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    target = get_user_by_username(username)
    if not target:
        raise HTTPException(404)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    send_message(user["id"], target["id"], text)
    return {"ok": True, "message": "Отправлено"}


@app.post("/api/profile")
async def api_update_profile(request: Request, body: ProfileBody):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    if body.display_name is not None:
        user["display_name"] = body.display_name.strip()[:40] or user["username"]
    if body.bio is not None:
        user["bio"] = body.bio.strip()[:160]
    if body.location is not None:
        user["location"] = body.location.strip()[:60]
    if body.website is not None:
        user["website"] = body.website.strip()[:120]
    return {"ok": True, "message": "Профиль обновлён", "redirect": f"/u/{user['username']}"}


@app.post("/api/theme")
async def api_theme(request: Request, body: ThemeBody):
    user = get_current_user(request)
    if user:
        user["theme"] = "light" if body.theme == "light" else "dark"
    return {"ok": True}


def seed_data():
    def make(u, e, p, d, bio="", loc="", site=""):
        usr = create_user(u, e, p, d)
        usr["bio"] = bio
        usr["location"] = loc
        usr["website"] = site
        usr["verified"] = u in ("chirp", "anna", "dmitry")
        return usr
    a = make("anna", "anna@example.com", "password", "Анна Смирнова",
             "Дизайнер интерфейсов. Люблю кофе и минимализм.", "Москва", "anna.design")
    b = make("dmitry", "dmitry@example.com", "password", "Дмитрий Орлов",
             "Backend-разработчик. Python, FastAPI, PostgreSQL.", "Санкт-Петербург", "dmitry.dev")
    c = make("maria", "maria@example.com", "password", "Мария Иванова",
             "Путешествую и фотографирую.", "Сочи")
    d = make("chirp", "team@chirp.app", "password", "Chirp Team",
             "Официальный аккаунт Chirp.", "Интернет", "chirp.app")
    e = make("olga", "olga@example.com", "password", "Ольга Петрова",
             "Пишу про книги и кино.", "Казань")
    f = make("sergey", "sergey@example.com", "password", "Сергей Кузнецов",
             "Фронтендер. JS, React, дизайн-системы.", "Новосибирск")

    toggle_follow(b["id"], a["id"])
    toggle_follow(c["id"], a["id"])
    toggle_follow(d["id"], a["id"])
    toggle_follow(e["id"], a["id"])
    toggle_follow(a["id"], b["id"])
    toggle_follow(d["id"], b["id"])
    toggle_follow(f["id"], b["id"])
    toggle_follow(a["id"], c["id"])
    toggle_follow(b["id"], c["id"])
    toggle_follow(a["id"], d["id"])
    toggle_follow(b["id"], d["id"])
    toggle_follow(c["id"], d["id"])
    toggle_follow(e["id"], d["id"])
    toggle_follow(f["id"], d["id"])
    toggle_follow(a["id"], e["id"])
    toggle_follow(a["id"], f["id"])
    toggle_follow(b["id"], f["id"])

    t1 = create_tweet(d["id"], "Добро пожаловать в Chirp! 🎉\n\nЭто минималистичная соцсеть на FastAPI. Всё работает в памяти, без базы данных.\n\nПопробуйте: #chirp #fastapi")
    t2 = create_tweet(a["id"], "Закончила редизайн личного сайта. Осталось только выложить 😅 #design #дизайн")
    t3 = create_tweet(b["id"], "FastAPI — это любовь. 2000 строк кода и полноценная соцсеть готова. #python #fastapi")
    t4 = create_tweet(c["id"], "Сегодня гуляла по набережной, поймала закат 🌅\n\nКто ещё любит вечерние прогулки?", image_url="https://images.unsplash.com/photo-1495616811223-4d98c6e9c869?w=900")
    t5 = create_tweet(e["id"], "Перечитала «Мастера и Маргариту». Каждый раз нахожу что-то новое. #книги")
    t6 = create_tweet(f["id"], "Дизайн-система — это не про компоненты, а про договорённости. #frontend")

    toggle_like(t1["id"], a["id"])
    toggle_like(t1["id"], b["id"])
    toggle_like(t1["id"], c["id"])
    toggle_like(t1["id"], e["id"])
    toggle_like(t1["id"], f["id"])
    toggle_like(t2["id"], b["id"])
    toggle_like(t2["id"], d["id"])
    toggle_like(t3["id"], a["id"])
    toggle_like(t3["id"], d["id"])
    toggle_like(t3["id"], f["id"])
    toggle_like(t4["id"], a["id"])
    toggle_like(t4["id"], d["id"])
    toggle_like(t5["id"], a["id"])
    toggle_like(t6["id"], a["id"])
    toggle_like(t6["id"], b["id"])

    toggle_retweet(t1["id"], a["id"])
    toggle_retweet(t3["id"], f["id"])
    toggle_retweet(t4["id"], a["id"])

    r1 = create_tweet(a["id"], "@chirp Спасибо, что сделали! Уже пользуюсь 😊", parent_id=t1["id"])
    r2 = create_tweet(b["id"], "@chirp FastAPI решает 👌", parent_id=t1["id"])
    r3 = create_tweet(c["id"], "@dmitry Полностью согласна! #fastapi", parent_id=t3["id"])
    r4 = create_tweet(a["id"], "Красиво! Что за место?", parent_id=t4["id"])
    r5 = create_tweet(d["id"], "@anna Добро пожаловать!", parent_id=t1["id"])

    toggle_like(r1["id"], d["id"])
    toggle_like(r2["id"], d["id"])
    toggle_like(r4["id"], c["id"])

    BOOKMARKS[a["id"]].add(t3["id"])
    BOOKMARKS[a["id"]].add(t1["id"])

    send_message(a["id"], b["id"], "Привет! Как дела?")
    send_message(b["id"], a["id"], "Привет! Отлично, работаю над новым проектом 😊")
    send_message(a["id"], b["id"], "Звучит круто, покажешь потом?")
    send_message(c["id"], a["id"], "Хочу сходить на выставку, ты со мной?")


seed_data()


@app.exception_handler(404)
async def not_found(request: Request, exc: HTTPException):
    user = get_current_user(request)
    body = (
        '<div class="topbar"><h1>404</h1></div>'
        '<div class="empty"><h3>Такой страницы нет</h3>'
        '<p>Возможно, она была удалена или никогда не существовала.</p>'
        '<p style="margin-top:16px"><a class="link-btn" href="/">На главную</a></p></div>'
    )
    return HTMLResponse(layout("404", body, user), status_code=404)


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail or "Ошибка"}, status_code=exc.status_code)
    if exc.status_code == 404:
        return await not_found(request, exc)
    user = get_current_user(request)
    body = (
        f'<div class="topbar"><h1>{exc.status_code}</h1></div>'
        f'<div class="empty"><h3>{esc(exc.detail or "Ошибка")}</h3>'
        '<p style="margin-top:16px"><a class="link-btn" href="/">На главную</a></p></div>'
    )
    return HTMLResponse(layout(str(exc.status_code), body, user), status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
