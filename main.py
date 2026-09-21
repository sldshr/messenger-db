# main.py
# pip install fastapi uvicorn

import base64
import hashlib
import json
import secrets
import time
from typing import Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ============================================================
#                    ЗАЩИТА / "ШИФРОВАНИЕ"
# ============================================================
# Все POST-тела приходят зашифрованными (XOR + base64).
# Для реального продакшена используйте HTTPS + TLS.

XK = b"DirectSecret2024"


def _xor(b: bytes) -> bytes:
    return bytes(c ^ XK[i % len(XK)] for i, c in enumerate(b))


def enc_str(s: str) -> str:
    return base64.b64encode(_xor(s.encode("utf-8"))).decode()


def dec_str(s: str) -> str:
    return _xor(base64.b64decode(s.encode())).decode("utf-8")


# ============================================================
#                    ХРАНИЛИЩЕ (ОПЕРАТИВКА)
# ============================================================

USERS: Dict[str, dict] = {}      # nick -> {name, password, avatar, contacts, blacklist}
SESSIONS: Dict[str, str] = {}    # token -> nick
CHATS: Dict[str, list] = {}      # "a|b" -> [ {from, to, text, ts} ]


def chat_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    return salt + "$" + hashlib.sha256((salt + pw).encode()).hexdigest()


def verify_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    return hashlib.sha256((salt + pw).encode()).hexdigest() == h


# ============================================================
#                         FASTAPI
# ============================================================

app = FastAPI(title="Direct")


class EncBody(BaseModel):
    data: str


def parse(body: EncBody) -> dict:
    try:
        return json.loads(dec_str(body.data))
    except Exception:
        raise HTTPException(400, "Повреждённый запрос")


def auth(token: Optional[str]) -> str:
    if not token or token not in SESSIONS:
        raise HTTPException(401, "Не авторизован")
    return SESSIONS[token]


def user_public(nick: str) -> dict:
    u = USERS[nick]
    return {"nick": nick, "name": u["name"], "avatar": u["avatar"]}


# ---------------------------- AUTH ----------------------------

@app.post("/api/register")
def register(body: EncBody):
    d = parse(body)
    nick = (d.get("nick") or "").strip()
    name = (d.get("name") or "").strip()
    pw = d.get("password") or ""
    pw2 = d.get("password2") or ""
    avatar = d.get("avatar") or []

    if not nick or not name or not pw:
        raise HTTPException(400, "Заполните все поля")
    if len(nick) < 3:
        raise HTTPException(400, "Ник минимум 3 символа")
    if any(c in nick for c in " |"):
        raise HTTPException(400, "Ник не должен содержать пробелы и |")
    if pw != pw2:
        raise HTTPException(400, "Пароли не совпадают")
    if len(pw) < 4:
        raise HTTPException(400, "Пароль минимум 4 символа")
    if nick in USERS:
        raise HTTPException(400, "Такой ник уже занят")

    if not isinstance(avatar, list) or len(avatar) != 64:
        avatar = ["#ffffff"] * 64
    avatar = [
        a if isinstance(a, str) and a.startswith("#") and len(a) == 7 else "#ffffff"
        for a in avatar
    ]

    USERS[nick] = {
        "name": name,
        "nick": nick,
        "password": hash_pw(pw),
        "avatar": avatar,
        "contacts": set(),
        "blacklist": set(),
    }
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = nick
    return {"ok": True, "token": token, "me": user_public(nick)}


@app.post("/api/login")
def login(body: EncBody):
    d = parse(body)
    nick = (d.get("nick") or "").strip()
    pw = d.get("password") or ""
    u = USERS.get(nick)
    if not u or not verify_pw(pw, u["password"]):
        raise HTTPException(400, "Неверный ник или пароль")
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = nick
    return {"ok": True, "token": token, "me": user_public(nick)}


@app.get("/api/me")
def me(x_token: Optional[str] = Header(None)):
    nick = auth(x_token)
    u = USERS[nick]
    return {
        "me": user_public(nick),
        "contacts": [user_public(n) for n in u["contacts"] if n in USERS],
        "blacklist": [user_public(n) for n in u["blacklist"] if n in USERS],
    }


# --------------------------- ПОИСК ----------------------------

@app.post("/api/search")
def search(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = auth(x_token)
    d = parse(body)
    q = (d.get("nick") or "").strip()
    if not q:
        raise HTTPException(400, "Введите ник")
    if q == nick:
        raise HTTPException(400, "Это ваш собственный ник")
    if q not in USERS:
        raise HTTPException(404, "Пользователь не найден")
    if nick in USERS[q]["blacklist"]:
        raise HTTPException(403, "Пользователь недоступен")
    if q in USERS[nick]["blacklist"]:
        raise HTTPException(403, "Пользователь в вашем чёрном списке")
    return {"user": user_public(q)}


# --------------------------- ЧАТЫ -----------------------------

@app.post("/api/chat/start")
def start_chat(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = auth(x_token)
    d = parse(body)
    peer = d.get("nick")
    if peer not in USERS or peer == nick:
        raise HTTPException(400, "Некорректный пользователь")
    if peer in USERS[nick]["blacklist"]:
        raise HTTPException(403, "Пользователь в чёрном списке")
    if nick in USERS[peer]["blacklist"]:
        raise HTTPException(403, "Пользователь недоступен")
    USERS[nick]["contacts"].add(peer)
    USERS[peer]["contacts"].add(nick)
    return {"ok": True}


@app.post("/api/chat/send")
def send_msg(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = auth(x_token)
    d = parse(body)
    peer = d.get("to")
    text = (d.get("text") or "").strip()
    if peer not in USERS or not text:
        raise HTTPException(400, "Ошибка отправки")
    if len(text) > 2000:
        raise HTTPException(400, "Сообщение слишком длинное")
    if peer in USERS[nick]["blacklist"]:
        raise HTTPException(403, "Вы добавили пользователя в чёрный список")
    if nick in USERS[peer]["blacklist"]:
        raise HTTPException(403, "Пользователь добавил вас в чёрный список")

    k = chat_key(nick, peer)
    msg = {"from": nick, "to": peer, "text": text, "ts": time.time()}
    CHATS.setdefault(k, []).append(msg)
    return {"ok": True, "msg": msg}


@app.post("/api/chat/messages")
def messages(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = auth(x_token)
    d = parse(body)
    peer = d.get("peer")
    if peer not in USERS:
        raise HTTPException(400, "Пользователь не найден")
    k = chat_key(nick, peer)
    return {"messages": CHATS.get(k, []), "peer": user_public(peer)}


# ------------------------- ЧЁРНЫЙ СПИСОК ----------------------

@app.post("/api/blacklist/add")
def bl_add(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = auth(x_token)
    d = parse(body)
    peer = d.get("nick")
    if peer not in USERS or peer == nick:
        raise HTTPException(400, "Некорректный пользователь")
    USERS[nick]["blacklist"].add(peer)
    USERS[nick]["contacts"].discard(peer)
    return {"ok": True}


@app.post("/api/blacklist/remove")
def bl_remove(body: EncBody, x_token: Optional[str] = Header(None)):
    nick = auth(x_token)
    d = parse(body)
    peer = d.get("nick")
    USERS[nick]["blacklist"].discard(peer)
    return {"ok": True}


# ============================================================
#                          FRONTEND
# ============================================================

PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#0a84ff">
<title>Direct</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,sans-serif;overscroll-behavior:none}
:root{
  --bg:#f2f2f7;--fg:#111;--card:#fff;--muted:#8a8a8e;--border:#e3e3e8;
  --accent:#0a84ff;--accent-fg:#fff;--danger:#ff3b30;
  --in-bub:#fff;--in-fg:#111;--out-bub:#0a84ff;--out-fg:#fff;--overlay:rgba(0,0,0,.45);
}
body.dark{
  --bg:#0d0d0f;--fg:#f2f2f7;--card:#1c1c1e;--muted:#8e8e93;--border:#2c2c2e;
  --accent:#0a84ff;--accent-fg:#fff;--danger:#ff453a;
  --in-bub:#2c2c2e;--in-fg:#f2f2f7;--out-bub:#0a84ff;--out-fg:#fff;--overlay:rgba(0,0,0,.65);
}
body{background:var(--bg);color:var(--fg);transition:background .2s,color .2s}
#app{
  max-width:480px;margin:0 auto;height:100vh;height:100dvh;
  display:flex;flex-direction:column;position:relative;overflow:hidden;
  background:var(--bg);
}
@media(min-width:700px){
  body{display:flex;justify-content:center;align-items:center;background:#000}
  body.dark{background:#000}
  #app{height:88vh;max-height:820px;border-radius:22px;box-shadow:0 25px 80px rgba(0,0,0,.55);border:1px solid var(--border)}
}
.screen{position:absolute;inset:0;display:none;flex-direction:column;background:var(--bg)}
.screen.active{display:flex}

/* ---------- AUTH ---------- */
#screen-auth{overflow-y:auto}
.auth-wrap{padding:28px 22px;display:flex;flex-direction:column;gap:14px;min-height:100%}
.logo{font-size:38px;font-weight:800;letter-spacing:-1px;text-align:center;margin:14px 0 4px;background:linear-gradient(135deg,#0a84ff,#5856d6);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.tabs{display:flex;background:var(--card);border-radius:12px;padding:4px;gap:4px;border:1px solid var(--border)}
.tab{flex:1;padding:10px;border:0;background:transparent;color:var(--fg);border-radius:9px;font-size:15px;font-weight:600;cursor:pointer;transition:.15s}
.tab.active{background:var(--accent);color:#fff}
.tabpane{display:flex;flex-direction:column;gap:10px}
.tabpane.hidden{display:none}
input{
  width:100%;padding:13px 14px;border-radius:12px;border:1px solid var(--border);
  background:var(--card);color:var(--fg);font-size:16px;outline:none;transition:.15s;
}
input:focus{border-color:var(--accent)}
.btn{
  padding:13px 16px;border-radius:12px;border:1px solid var(--border);
  background:var(--card);color:var(--fg);font-size:15px;font-weight:600;cursor:pointer;transition:.15s;
}
.btn:active{transform:scale(.97)}
.btn.primary{background:var(--accent);color:#fff;border-color:transparent}
.btn.danger{background:var(--danger);color:#fff;border-color:transparent}
.btn.full{width:100%}
.btn.small{padding:8px 12px;font-size:13px}
.row{display:flex;gap:8px}
.row input{flex:1}
.err{color:var(--danger);font-size:14px;text-align:center;min-height:18px}
.muted{color:var(--muted);font-size:13px}
.big-name{font-size:20px;font-weight:700;margin-top:6px}
.center{text-align:center;align-items:center}

/* ---------- AVATAR EDITOR ---------- */
.ava-editor{display:flex;flex-direction:column;gap:10px;align-items:center;background:var(--card);padding:14px;border-radius:16px;border:1px solid var(--border)}
#avaCanvas{width:220px;height:220px;border-radius:12px;background:#fff;touch-action:none;cursor:crosshair;image-rendering:pixelated}
.palette{display:grid;grid-template-columns:repeat(8,1fr);gap:6px;width:100%}
.swatch{width:100%;aspect-ratio:1;border-radius:8px;border:2px solid transparent;cursor:pointer;transition:.1s}
.swatch.active{border-color:var(--accent);transform:scale(1.12)}
.ava-tools{display:flex;gap:8px;width:100%}
.ava-tools .btn{flex:1}

/* ---------- TOP BAR ---------- */
.topbar{
  display:flex;align-items:center;gap:10px;padding:10px 12px;
  background:var(--card);border-bottom:1px solid var(--border);
  padding-top:max(10px,env(safe-area-inset-top));
}
.ava-small{width:40px;height:40px;border-radius:50%;flex-shrink:0;background:#ddd;image-rendering:pixelated}
.me-info{flex:1;min-width:0;overflow:hidden}
.me-name{font-size:15px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.me-nick{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.icon-btn{
  width:40px;height:40px;border-radius:50%;border:0;background:transparent;color:var(--fg);
  font-size:20px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;
  transition:.15s;
}
.icon-btn:active{background:var(--border)}
.icon-btn.danger{color:var(--danger)}

/* ---------- CONTACTS ---------- */
.list{flex:1;overflow-y:auto;padding:10px}
.contact{
  display:flex;align-items:center;gap:12px;padding:12px;border-radius:14px;
  background:var(--card);margin-bottom:8px;cursor:pointer;transition:.15s;border:1px solid var(--border);
}
.contact:active{transform:scale(.98);background:var(--border)}
.empty{text-align:center;color:var(--muted);padding:60px 20px;font-size:15px;line-height:1.5}

/* ---------- FAB ---------- */
.fab{
  position:absolute;bottom:calc(24px + env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);
  width:62px;height:62px;border-radius:50%;border:0;background:var(--accent);color:#fff;
  font-size:34px;font-weight:300;line-height:1;cursor:pointer;box-shadow:0 8px 28px rgba(10,132,255,.45);
  transition:.15s;display:flex;align-items:center;justify-content:center;padding-bottom:4px;
}
.fab:active{transform:translateX(-50%) scale(.92)}

/* ---------- CHAT ---------- */
.messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:6px}
.bubble{
  max-width:78%;padding:9px 13px;border-radius:18px;font-size:15px;line-height:1.35;
  word-wrap:break-word;white-space:pre-wrap;animation:pop .15s ease;
}
@keyframes pop{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.bubble.out{align-self:flex-end;background:var(--out-bub);color:var(--out-fg);border-bottom-right-radius:6px}
.bubble.in{align-self:flex-start;background:var(--in-bub);color:var(--in-fg);border-bottom-left-radius:6px;border:1px solid var(--border)}
.composer{
  display:flex;gap:8px;padding:10px;padding-bottom:max(10px,env(safe-area-inset-bottom));
  background:var(--card);border-top:1px solid var(--border);
}
.composer input{border-radius:20px}
.composer .btn{border-radius:50%;width:46px;height:46px;padding:0;font-size:19px;flex-shrink:0}

/* ---------- MODALS ---------- */
.overlay{
  position:absolute;inset:0;background:var(--overlay);display:flex;align-items:flex-end;
  justify-content:center;z-index:50;animation:fade .15s ease;
}
@keyframes fade{from{opacity:0}to{opacity:1}}
.overlay.hidden{display:none}
.modal{
  background:var(--card);width:100%;max-height:88%;border-radius:22px 22px 0 0;
  display:flex;flex-direction:column;animation:slideUp .22s cubic-bezier(.2,.8,.3,1);
  padding-bottom:env(safe-area-inset-bottom);
}
@keyframes slideUp{from{transform:translateY(100%)}to{transform:none}}
@media(min-width:700px){
  .overlay{align-items:center}
  .modal{max-width:440px;border-radius:20px;max-height:80%}
}
.modal-head{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--border);font-size:17px;font-weight:700}
.modal-body{padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:14px}
.setting{display:flex;align-items:center;justify-content:space-between;gap:10px}
.setting-col{display:flex;flex-direction:column;gap:8px}
.setting-title{font-size:14px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.seg{display:flex;background:var(--bg);border-radius:10px;padding:3px;gap:3px;border:1px solid var(--border)}
.seg-btn{padding:7px 14px;border:0;background:transparent;color:var(--fg);border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;transition:.15s}
.seg-btn.active{background:var(--accent);color:#fff}
.search-card{display:flex;flex-direction:column;align-items:center;gap:8px;padding:16px;background:var(--bg);border-radius:16px;border:1px solid var(--border)}
.ava-mid{width:96px;height:96px;border-radius:50%;image-rendering:pixelated;background:#ddd}
#infoAva{width:140px;height:140px;border-radius:50%;image-rendering:pixelated;background:#ddd;margin:0 auto}
.bl-item{display:flex;align-items:center;gap:10px;padding:8px;background:var(--bg);border-radius:12px;margin-bottom:6px}
.bl-item canvas{width:32px;height:32px;border-radius:50%;image-rendering:pixelated;flex-shrink:0}
.bl-item .me-info{flex:1}
.bl-item button{width:32px;height:32px;border-radius:50%;border:0;background:var(--danger);color:#fff;font-size:16px;cursor:pointer;flex-shrink:0}
</style>
</head>
<body>
<div id="app">

  <!-- ================= AUTH ================= -->
  <div class="screen active" id="screen-auth">
    <div class="auth-wrap">
      <h1 class="logo">Direct</h1>
      <div class="tabs">
        <button class="tab active" data-tab="login" data-i18n="login">Вход</button>
        <button class="tab" data-tab="reg" data-i18n="register">Регистрация</button>
      </div>

      <div id="tab-login" class="tabpane">
        <input id="li-nick" data-i18n-ph="ph_nick" placeholder="Ник" autocomplete="username">
        <input id="li-pass" type="password" data-i18n-ph="ph_pass" placeholder="Пароль" autocomplete="current-password">
        <button class="btn primary" onclick="doLogin()" data-i18n="login">Войти</button>
      </div>

      <div id="tab-reg" class="tabpane hidden">
        <div class="ava-editor">
          <canvas id="avaCanvas" width="256" height="256"></canvas>
          <div class="palette" id="palette"></div>
          <div class="ava-tools">
            <button class="btn small" onclick="clearAvatar()" data-i18n="clear">Очистить</button>
            <button class="btn small" onclick="randomAvatar()" data-i18n="random">Случайно</button>
            <button class="btn small" onclick="fillAvatar()" data-i18n="fill">Залить</button>
          </div>
        </div>
        <input id="rg-name" data-i18n-ph="ph_name" placeholder="Имя">
        <input id="rg-nick" data-i18n-ph="ph_nick" placeholder="Ник">
        <input id="rg-pass" type="password" data-i18n-ph="ph_pass" placeholder="Пароль">
        <input id="rg-pass2" type="password" data-i18n-ph="ph_pass2" placeholder="Повтор пароля">
        <button class="btn primary" onclick="doRegister()" data-i18n="create">Создать аккаунт</button>
      </div>

      <div id="auth-err" class="err"></div>
    </div>
  </div>

  <!-- ================= MAIN ================= -->
  <div class="screen" id="screen-main">
    <header class="topbar">
      <canvas class="ava-small" id="meAva" width="40" height="40"></canvas>
      <div class="me-info">
        <div class="me-name" id="meName">—</div>
        <div class="me-nick" id="meNick">@—</div>
      </div>
      <button class="icon-btn" onclick="openSettings()" title="Настройки">⚙</button>
    </header>
    <div class="list" id="contactsList"></div>
    <button class="fab" onclick="openSearch()" title="Найти">+</button>
  </div>

  <!-- ================= CHAT ================= -->
  <div class="screen" id="screen-chat">
    <header class="topbar">
      <button class="icon-btn" onclick="closeChat()" title="Назад">←</button>
      <canvas class="ava-small" id="peerAva" width="40" height="40"></canvas>
      <div class="me-info">
        <div class="me-name" id="peerName">—</div>
        <div class="me-nick" id="peerNick">@—</div>
      </div>
      <button class="icon-btn" onclick="openInfo()" title="Информация">i</button>
      <button class="icon-btn danger" onclick="endChat()" title="Завершить">✕</button>
    </header>
    <div class="messages" id="messages"></div>
    <form class="composer" onsubmit="sendMsg(event)">
      <input id="msgInput" data-i18n-ph="ph_msg" placeholder="Сообщение..." autocomplete="off">
      <button class="btn primary" type="submit">→</button>
    </form>
  </div>

  <!-- ================= MODAL: SEARCH ================= -->
  <div class="overlay hidden" id="modal-search" onclick="backdropClose(event,'modal-search')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head">
        <span data-i18n="find_user">Найти пользователя</span>
        <button class="icon-btn" onclick="closeModal('modal-search')">✕</button>
      </div>
      <div class="modal-body">
        <div class="row">
          <input id="searchNick" data-i18n-ph="ph_nick" placeholder="Ник">
          <button class="btn primary" onclick="doSearch()" data-i18n="find">Найти</button>
        </div>
        <div id="searchResult"></div>
      </div>
    </div>
  </div>

  <!-- ================= MODAL: SETTINGS ================= -->
  <div class="overlay hidden" id="modal-settings" onclick="backdropClose(event,'modal-settings')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head">
        <span data-i18n="settings">Настройки</span>
        <button class="icon-btn" onclick="closeModal('modal-settings')">✕</button>
      </div>
      <div class="modal-body">
        <div class="setting">
          <span data-i18n="theme">Тема</span>
          <div class="seg">
            <button class="seg-btn" data-theme="light" onclick="applyTheme('light')" data-i18n="light">Светлая</button>
            <button class="seg-btn" data-theme="dark" onclick="applyTheme('dark')" data-i18n="dark">Тёмная</button>
          </div>
        </div>
        <div class="setting">
          <span data-i18n="lang">Язык</span>
          <div class="seg">
            <button class="seg-btn" data-lang="ru" onclick="applyLang('ru')">RU</button>
            <button class="seg-btn" data-lang="en" onclick="applyLang('en')">EN</button>
          </div>
        </div>
        <div class="setting-col">
          <div class="setting-title" data-i18n="blacklist">Чёрный список</div>
          <div id="blacklistBox"></div>
          <div class="row">
            <input id="blNick" data-i18n-ph="ph_nick" placeholder="Ник">
            <button class="btn" onclick="blAdd()" data-i18n="add">+</button>
          </div>
        </div>
        <button class="btn danger full" onclick="logout()" data-i18n="logout">Выйти</button>
      </div>
    </div>
  </div>

  <!-- ================= MODAL: INFO ================= -->
  <div class="overlay hidden" id="modal-info" onclick="backdropClose(event,'modal-info')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head">
        <span data-i18n="about_user">О человеке</span>
        <button class="icon-btn" onclick="closeModal('modal-info')">✕</button>
      </div>
      <div class="modal-body center">
        <canvas id="infoAva" width="160" height="160"></canvas>
        <div id="infoName" class="big-name"></div>
        <div id="infoNick" class="muted"></div>
        <button class="btn danger full" onclick="endChat()" data-i18n="end_chat">Завершить чат</button>
      </div>
    </div>
  </div>

</div>

<script>
/* ============================================================
                        ШИФРОВАНИЕ
   ============================================================ */
const XK = new TextEncoder().encode("DirectSecret2024");
function xorBytes(bytes){
  const out = new Uint8Array(bytes.length);
  for (let i=0;i<bytes.length;i++) out[i] = bytes[i] ^ XK[i % XK.length];
  return out;
}
function encStr(str){
  const b = new TextEncoder().encode(str);
  const x = xorBytes(b);
  let bin = '';
  for (let i=0;i<x.length;i++) bin += String.fromCharCode(x[i]);
  return btoa(bin);
}

/* ============================================================
                          I18N
   ============================================================ */
const I18N = {
  ru: {
    login:"Вход", register:"Регистрация", create:"Создать аккаунт",
    ph_nick:"Ник", ph_pass:"Пароль", ph_pass2:"Повтор пароля", ph_name:"Имя",
    ph_msg:"Сообщение...",
    clear:"Очистить", random:"Случайно", fill:"Залить",
    settings:"Настройки", theme:"Тема", lang:"Язык", light:"Светлая", dark:"Тёмная",
    blacklist:"Чёрный список", add:"Добавить", logout:"Выйти",
    find_user:"Найти пользователя", find:"Найти",
    start_chat:"Начать общение", about_user:"О человеке", end_chat:"Завершить чат",
    no_contacts:"Пока нет контактов.\nНажмите + чтобы найти друзей.",
    no_bl:"Список пуст",
    confirm_logout:"Выйти из аккаунта?",
    remove:"Убрать"
  },
  en: {
    login:"Sign in", register:"Sign up", create:"Create account",
    ph_nick:"Nickname", ph_pass:"Password", ph_pass2:"Repeat password", ph_name:"Name",
    ph_msg:"Message...",
    clear:"Clear", random:"Random", fill:"Fill",
    settings:"Settings", theme:"Theme", lang:"Language", light:"Light", dark:"Dark",
    blacklist:"Blacklist", add:"Add", logout:"Log out",
    find_user:"Find user", find:"Find",
    start_chat:"Start chat", about_user:"About user", end_chat:"End chat",
    no_contacts:"No contacts yet.\nTap + to find friends.",
    no_bl:"List is empty",
    confirm_logout:"Log out?",
    remove:"Remove"
  }
};
let LANG = localStorage.getItem('direct_lang') || 'ru';
function t(k){ return (I18N[LANG] && I18N[LANG][k]) || k; }

/* ============================================================
                        STATE
   ============================================================ */
let token = localStorage.getItem('direct_token') || null;
let me = null;
let contacts = [];
let blacklist = [];
let currentPeer = null;
let currentPeerData = null;
let pollTimer = null;

/* ============================================================
                       API HELPER
   ============================================================ */
async function api(path, payload, method='POST'){
  const headers = {'Content-Type':'application/json'};
  if (token) headers['X-Token'] = token;
  const opts = {method, headers};
  if (method === 'POST'){
    opts.body = JSON.stringify({ data: encStr(JSON.stringify(payload || {})) });
  }
  const res = await fetch(path, opts);
  if (!res.ok){
    let msg = 'Ошибка';
    try { const j = await res.json(); msg = j.detail || msg; } catch(e){}
    throw new Error(msg);
  }
  return res.json();
}

/* ============================================================
                     AVATAR (8x8)
   ============================================================ */
const GRID = 8;
let avatarData = new Array(GRID*GRID).fill('#ffffff');
let currentColor = '#000000';

const PALETTE = [
  '#000000','#ffffff','#8e8e93','#c7c7cc',
  '#ff3b30','#ff9500','#ffcc00','#34c759',
  '#00c7be','#0a84ff','#5856d6','#af52de',
  '#ff2d55','#5c3b1e','#f5c6a5','#b3e5fc'
];

function paintAva(canvas, data){
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const cell = W / GRID;
  ctx.clearRect(0,0,W,H);
  for (let i=0;i<GRID*GRID;i++){
    ctx.fillStyle = (data && data[i]) || '#ffffff';
    const x = (i % GRID) * cell;
    const y = Math.floor(i / GRID) * cell;
    ctx.fillRect(x, y, cell, cell);
  }
}

const avaCanvas = document.getElementById('avaCanvas');
const avaCtx = avaCanvas.getContext('2d');

function renderEditor(){
  paintAva(avaCanvas, avatarData);
  // сетка
  const cell = avaCanvas.width / GRID;
  avaCtx.strokeStyle = 'rgba(0,0,0,.14)';
  avaCtx.lineWidth = 1;
  for (let i=1;i<GRID;i++){
    avaCtx.beginPath(); avaCtx.moveTo(i*cell,0); avaCtx.lineTo(i*cell,avaCanvas.height); avaCtx.stroke();
    avaCtx.beginPath(); avaCtx.moveTo(0,i*cell); avaCtx.lineTo(avaCanvas.width,i*cell); avaCtx.stroke();
  }
}

function buildPalette(){
  const box = document.getElementById('palette');
  box.innerHTML = '';
  PALETTE.forEach((c,i) => {
    const b = document.createElement('div');
    b.className = 'swatch' + (i===0 ? '' : '');
    b.style.background = c;
    b.onclick = () => {
      currentColor = c;
      document.querySelectorAll('.swatch').forEach(s=>s.classList.remove('active'));
      b.classList.add('active');
    };
    box.appendChild(b);
  });
  // по умолчанию чёрный
  const first = box.children[0];
  if (first) first.classList.add('active');
}

let drawing = false;
function posToCell(clientX, clientY){
  const r = avaCanvas.getBoundingClientRect();
  const x = Math.floor((clientX - r.left) / (r.width/GRID));
  const y = Math.floor((clientY - r.top) / (r.height/GRID));
  if (x < 0 || x > 7 || y < 0 || y > 7) return null;
  return [x,y];
}
function paintAt(clientX, clientY){
  const p = posToCell(clientX, clientY);
  if (!p) return;
  avatarData[p[1]*GRID + p[0]] = currentColor;
  renderEditor();
}
avaCanvas.addEventListener('pointerdown', e => {
  e.preventDefault();
  drawing = true;
  avaCanvas.setPointerCapture(e.pointerId);
  paintAt(e.clientX, e.clientY);
});
avaCanvas.addEventListener('pointermove', e => {
  if (!drawing) return;
  paintAt(e.clientX, e.clientY);
});
avaCanvas.addEventListener('pointerup', e => { drawing = false; });
avaCanvas.addEventListener('pointercancel', e => { drawing = false; });

function clearAvatar(){
  avatarData = new Array(64).fill('#ffffff');
  renderEditor();
}
function fillAvatar(){
  avatarData = new Array(64).fill(currentColor);
  renderEditor();
}
function randomAvatar(){
  const cols = PALETTE.slice(2);
  const c1 = cols[Math.floor(Math.random()*cols.length)];
  const c2 = cols[Math.floor(Math.random()*cols.length)];
  const c3 = cols[Math.floor(Math.random()*cols.length)];
  // симметричный случайный узор
  const half = [];
  for (let y=0;y<8;y++){
    const row = [];
    for (let x=0;x<4;x++){
      const r = Math.random();
      row.push(r < 0.4 ? c1 : r < 0.7 ? c2 : r < 0.9 ? c3 : '#ffffff');
    }
    half.push(row);
  }
  avatarData = [];
  for (let y=0;y<8;y++){
    for (let x=0;x<4;x++) avatarData.push(half[y][x]);
    for (let x=3;x>=0;x--) avatarData.push(half[y][x]);
  }
  renderEditor();
}

/* ============================================================
                       UI HELPERS
   ============================================================ */
function showScreen(id){
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}
function showErr(id, msg){
  const el = document.getElementById(id);
  el.textContent = msg;
  clearTimeout(el._t);
  el._t = setTimeout(()=>{ el.textContent = ''; }, 4000);
}
function openModal(id){
  document.getElementById(id).classList.remove('hidden');
}
function closeModal(id){
  document.getElementById(id).classList.add('hidden');
}
function backdropClose(e, id){
  if (e.target.id === id) closeModal(id);
}

/* ============================================================
                       THEME / LANG
   ============================================================ */
function applyTheme(th){
  document.body.classList.toggle('dark', th === 'dark');
  localStorage.setItem('direct_theme', th);
  document.querySelectorAll('[data-theme]').forEach(b =>
    b.classList.toggle('active', b.dataset.theme === th));
}
function applyLang(l){
  LANG = l;
  localStorage.setItem('direct_lang', l);
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const k = el.dataset.i18n;
    if (I18N[l][k]) el.textContent = I18N[l][k];
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    const k = el.dataset.i18nPh;
    if (I18N[l][k]) el.placeholder = I18N[l][k];
  });
  document.querySelectorAll('[data-lang]').forEach(b =>
    b.classList.toggle('active', b.dataset.lang === l));
  // перерендер списков, чтобы обновить тексты
  renderContacts();
  renderBlacklist();
}

/* ============================================================
                         AUTH
   ============================================================ */
document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const isLogin = tab.dataset.tab === 'login';
    document.getElementById('tab-login').classList.toggle('hidden', !isLogin);
    document.getElementById('tab-reg').classList.toggle('hidden', isLogin);
    document.getElementById('auth-err').textContent = '';
  };
});

async function doLogin(){
  const nick = document.getElementById('li-nick').value.trim();
  const password = document.getElementById('li-pass').value;
  if (!nick || !password){ showErr('auth-err', 'Введите ник и пароль'); return; }
  try {
    const r = await api('/api/login', { nick, password });
    token = r.token; me = r.me;
    localStorage.setItem('direct_token', token);
    await refreshMe();
    showScreen('screen-main');
  } catch(e){ showErr('auth-err', e.message); }
}

async function doRegister(){
  const name = document.getElementById('rg-name').value.trim();
  const nick = document.getElementById('rg-nick').value.trim();
  const password = document.getElementById('rg-pass').value;
  const password2 = document.getElementById('rg-pass2').value;
  try {
    const r = await api('/api/register', { name, nick, password, password2, avatar: avatarData });
    token = r.token; me = r.me;
    localStorage.setItem('direct_token', token);
    await refreshMe();
    showScreen('screen-main');
  } catch(e){ showErr('auth-err', e.message); }
}

function logout(){
  if (!confirm(t('confirm_logout'))) return;
  token = null; me = null; contacts = []; blacklist = [];
  localStorage.removeItem('direct_token');
  document.getElementById('li-nick').value = '';
  document.getElementById('li-pass').value = '';
  closeModal('modal-settings');
  clearInterval(pollTimer);
  showScreen('screen-auth');
}

/* ============================================================
                        ME / CONTACTS
   ============================================================ */
async function refreshMe(){
  const r = await api('/api/me', null, 'GET');
  me = r.me;
  contacts = r.contacts;
  blacklist = r.blacklist;
  paintAva(document.getElementById('meAva'), me.avatar);
  document.getElementById('meName').textContent = me.name;
  document.getElementById('meNick').textContent = '@' + me.nick;
  renderContacts();
  renderBlacklist();
}

function renderContacts(){
  const box = document.getElementById('contactsList');
  box.innerHTML = '';
  if (!contacts.length){
    const e = document.createElement('div');
    e.className = 'empty';
    e.textContent = t('no_contacts');
    box.appendChild(e);
    return;
  }
  contacts.forEach(c => {
    const el = document.createElement('div');
    el.className = 'contact';
    el.onclick = () => openChat(c.nick);
    const cv = document.createElement('canvas');
    cv.width = 48; cv.height = 48;
    cv.className = 'ava-small';
    paintAva(cv, c.avatar);
    el.appendChild(cv);
    const info = document.createElement('div');
    info.className = 'me-info';
    const nm = document.createElement('div');
    nm.className = 'me-name'; nm.textContent = c.name;
    const nk = document.createElement('div');
    nk.className = 'me-nick'; nk.textContent = '@' + c.nick;
    info.appendChild(nm); info.appendChild(nk);
    el.appendChild(info);
    box.appendChild(el);
  });
}

/* ============================================================
                       ПОИСК / ЧАТ
   ============================================================ */
function openSearch(){
  document.getElementById('searchNick').value = '';
  document.getElementById('searchResult').innerHTML = '';
  openModal('modal-search');
  setTimeout(()=>document.getElementById('searchNick').focus(), 250);
}

async function doSearch(){
  const nick = document.getElementById('searchNick').value.trim();
  const box = document.getElementById('searchResult');
  box.innerHTML = '';
  if (!nick) return;
  try {
    const r = await api('/api/search', { nick });
    const u = r.user;
    const card = document.createElement('div');
    card.className = 'search-card';
    const cv = document.createElement('canvas');
    cv.width = 96; cv.height = 96;
    cv.className = 'ava-mid';
    paintAva(cv, u.avatar);
    card.appendChild(cv);
    const nm = document.createElement('div');
    nm.className = 'big-name'; nm.textContent = u.name;
    const nk = document.createElement('div');
    nk.className = 'muted'; nk.textContent = '@' + u.nick;
    card.appendChild(nm); card.appendChild(nk);
    const btn = document.createElement('button');
    btn.className = 'btn primary full';
    btn.textContent = t('start_chat');
    btn.onclick = () => { closeModal('modal-search'); startChat(u.nick); };
    card.appendChild(btn);
    box.appendChild(card);
  } catch(e){
    const er = document.createElement('div');
    er.className = 'err';
    er.textContent = e.message;
    box.appendChild(er);
  }
}

async function startChat(peerNick){
  try {
    await api('/api/chat/start', { nick: peerNick });
    await refreshMe();
    openChat(peerNick);
  } catch(e){ alert(e.message); }
}

async function openChat(peerNick){
  try {
    const r = await api('/api/chat/messages', { peer: peerNick });
    currentPeer = peerNick;
    currentPeerData = r.peer;
    paintAva(document.getElementById('peerAva'), r.peer.avatar);
    document.getElementById('peerName').textContent = r.peer.name;
    document.getElementById('peerNick').textContent = '@' + r.peer.nick;
    renderMessages(r.messages);
    showScreen('screen-chat');
    clearInterval(pollTimer);
    pollTimer = setInterval(pollMessages, 1500);
    setTimeout(()=>document.getElementById('msgInput').focus(), 100);
  } catch(e){ alert(e.message); }
}

async function pollMessages(){
  if (!currentPeer) return;
  try {
    const r = await api('/api/chat/messages', { peer: currentPeer });
    currentPeerData = r.peer;
    renderMessages(r.messages);
  } catch(e){}
}

function renderMessages(msgs){
  const box = document.getElementById('messages');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.innerHTML = '';
  msgs.forEach(m => {
    const el = document.createElement('div');
    el.className = 'bubble ' + (m.from === me.nick ? 'out' : 'in');
    el.textContent = m.text;
    box.appendChild(el);
  });
  if (atBottom) box.scrollTop = box.scrollHeight;
}

async function sendMsg(e){
  e.preventDefault();
  const inp = document.getElementById('msgInput');
  const text = inp.value.trim();
  if (!text || !currentPeer) return;
  inp.value = '';
  try {
    await api('/api/chat/send', { to: currentPeer, text });
    await pollMessages();
  } catch(err){
    alert(err.message);
    inp.value = text;
  }
}

function closeChat(){
  clearInterval(pollTimer);
  pollTimer = null;
  currentPeer = null;
  currentPeerData = null;
  showScreen('screen-main');
  refreshMe();
}

function endChat(){
  closeModal('modal-info');
  closeChat();
}

/* ============================================================
                       INFO
   ============================================================ */
function openInfo(){
  if (!currentPeerData) return;
  paintAva(document.getElementById('infoAva'), currentPeerData.avatar);
  document.getElementById('infoName').textContent = currentPeerData.name;
  document.getElementById('infoNick').textContent = '@' + currentPeerData.nick;
  openModal('modal-info');
}

/* ============================================================
                       SETTINGS
   ============================================================ */
function openSettings(){
  renderBlacklist();
  openModal('modal-settings');
}

function renderBlacklist(){
  const box = document.getElementById('blacklistBox');
  box.innerHTML = '';
  if (!blacklist.length){
    const e = document.createElement('div');
    e.className = 'muted';
    e.style.padding = '6px 2px';
    e.textContent = t('no_bl');
    box.appendChild(e);
    return;
  }
  blacklist.forEach(u => {
    const el = document.createElement('div');
    el.className = 'bl-item';
    const cv = document.createElement('canvas');
    cv.width = 32; cv.height = 32;
    paintAva(cv, u.avatar);
    el.appendChild(cv);
    const info = document.createElement('div');
    info.className = 'me-info';
    const nm = document.createElement('div');
    nm.className = 'me-name'; nm.textContent = u.name;
    const nk = document.createElement('div');
    nk.className = 'me-nick'; nk.textContent = '@' + u.nick;
    info.appendChild(nm); info.appendChild(nk);
    el.appendChild(info);
    const btn = document.createElement('button');
    btn.textContent = '✕';
    btn.title = t('remove');
    btn.onclick = () => blRemove(u.nick);
    el.appendChild(btn);
    box.appendChild(el);
  });
}

async function blAdd(){
  const inp = document.getElementById('blNick');
  const nick = inp.value.trim();
  if (!nick) return;
  try {
    await api('/api/blacklist/add', { nick });
    inp.value = '';
    await refreshMe();
  } catch(e){ alert(e.message); }
}

async function blRemove(nick){
  try {
    await api('/api/blacklist/remove', { nick });
    await refreshMe();
  } catch(e){ alert(e.message); }
}

/* ============================================================
                       INIT
   ============================================================ */
(function init(){
  applyTheme(localStorage.getItem('direct_theme') || 'light');
  applyLang(LANG);
  buildPalette();
  randomAvatar();

  // попытка авто-входа
  if (token){
    api('/api/me', null, 'GET')
      .then(r => {
        me = r.me; contacts = r.contacts; blacklist = r.blacklist;
        paintAva(document.getElementById('meAva'), me.avatar);
        document.getElementById('meName').textContent = me.name;
        document.getElementById('meNick').textContent = '@' + me.nick;
        renderContacts();
        renderBlacklist();
        showScreen('screen-main');
      })
      .catch(() => {
        token = null;
        localStorage.removeItem('direct_token');
      });
  }

  // Enter для поиска
  document.getElementById('searchNick').addEventListener('keydown', e => {
    if (e.key === 'Enter'){ e.preventDefault(); doSearch(); }
  });
})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


# ============================================================
#                        ЗАПУСК
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
