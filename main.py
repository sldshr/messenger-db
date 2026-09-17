import os
import time
import secrets
import hashlib
from typing import Dict, List, Any

import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

SERVER_NAME = os.environ.get("SLDCHAT_DOMAIN", "localhost:8000")

app = FastAPI(title="SLDCHAT", version="0.1")

# ---------------- RAM STORAGE (no disk) ----------------
USERS: Dict[str, Dict[str, str]] = {}          # username -> {"salt","pw_hash","token"}
INBOXES: Dict[str, List[Dict[str, Any]]] = {}  # username -> [messages]


# ---------------- Models ----------------
class RegisterReq(BaseModel):
    username: str
    password: str


class LoginReq(BaseModel):
    username: str
    password: str


class SendReq(BaseModel):
    to: str
    subject: str = ""
    body: str


class FederationMsg(BaseModel):
    msg_id: str
    from_addr: str
    to_addr: str
    subject: str = ""
    body: str
    ts: float


# ---------------- Utils ----------------
def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def auth(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization[7:].strip()
    for u, data in USERS.items():
        if secrets.compare_digest(data.get("token", ""), token):
            return u
    raise HTTPException(401, "Invalid token")


def deliver_local(username: str, msg: dict) -> None:
    if username not in USERS:
        raise HTTPException(404, f"User '{username}' not found on this server")
    INBOXES.setdefault(username, []).append(msg)


# ---------------- Discovery ----------------
@app.get("/.well-known/sldchat")
async def well_known():
    return {"protocol": "sldchat", "version": 1, "server": SERVER_NAME}


# ---------------- Federation (server <-> server) ----------------
@app.post("/federation/receive")
async def federation_receive(msg: FederationMsg):
    # В продакшене тут должна быть проверка подписи отправителя.
    to_user, _, to_domain = msg.to_addr.rpartition("@")
    if to_domain != SERVER_NAME:
        raise HTTPException(400, "Wrong server")
    if to_user not in USERS:
        raise HTTPException(404, f"No such user: {to_user}")
    INBOXES.setdefault(to_user, []).append(msg.model_dump())
    return {"status": "ok"}


async def federate(to_domain: str, payload: dict) -> tuple[bool, str]:
    """Пробуем https, потом http. Возвращаем (ok, err)."""
    last_err = ""
    for scheme in ("https", "http"):
        url = f"{scheme}://{to_domain}/federation/receive"
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                r = await client.post(url, json=payload)
            if r.status_code < 400:
                return True, ""
            last_err = f"{r.status_code} {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
    return False, last_err


# ---------------- User API ----------------
@app.post("/api/register")
async def register(req: RegisterReq):
    u = req.username.strip().lower()
    if not u or not req.password:
        raise HTTPException(400, "username/password required")
    if not u.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "Bad username")
    if u in USERS:
        raise HTTPException(409, "User exists")
    salt = secrets.token_hex(8)
    USERS[u] = {
        "salt": salt,
        "pw_hash": hash_pw(req.password, salt),
        "token": secrets.token_urlsafe(24),
    }
    INBOXES[u] = []
    return {"status": "ok", "address": f"{u}@{SERVER_NAME}", "server": SERVER_NAME}


@app.post("/api/login")
async def login(req: LoginReq):
    u = req.username.strip().lower()
    data = USERS.get(u)
    if not data or data["pw_hash"] != hash_pw(req.password, data["salt"]):
        raise HTTPException(401, "Bad credentials")
    data["token"] = secrets.token_urlsafe(24)
    return {"status": "ok", "token": data["token"], "username": u, "server": SERVER_NAME}


@app.get("/api/me")
async def me(authorization: str | None = Header(None)):
    u = auth(authorization)
    return {"username": u, "server": SERVER_NAME, "address": f"{u}@{SERVER_NAME}"}


@app.get("/api/inbox")
async def inbox(authorization: str | None = Header(None)):
    u = auth(authorization)
    return {"messages": INBOXES.get(u, [])}


@app.post("/api/send")
async def send(req: SendReq, authorization: str | None = Header(None)):
    u = auth(authorization)
    to = req.to.strip().lower()
    if "@" not in to:
        raise HTTPException(400, "Address must be user@domain")
    to_user, to_domain = to.rsplit("@", 1)

    from_addr = f"{u}@{SERVER_NAME}"
    msg = {
        "msg_id": secrets.token_hex(16),
        "from_addr": from_addr,
        "to_addr": to,
        "subject": req.subject,
        "body": req.body,
        "ts": time.time(),
    }

    if to_domain == SERVER_NAME:
        deliver_local(to_user, msg)
        return {"status": "delivered", "local": True}

    ok, err = await federate(to_domain, msg)
    if not ok:
        raise HTTPException(502, f"Federation to {to_domain} failed: {err}")
    return {"status": "delivered", "local": False, "via": to_domain}


# ---------------- Old-school HTML client ----------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>SLDCHAT</title>
<style>
  html,body{background:#0a0a0a;color:#33ff33;font-family:"Courier New",monospace;margin:0;padding:0;font-size:14px}
  .wrap{max-width:900px;margin:20px auto;padding:20px;border:1px solid #33ff33}
  pre.logo{margin:0 0 12px 0;font-size:11px;line-height:1.05;color:#33ff33}
  .row{margin-bottom:8px}
  label{display:inline-block;width:90px}
  input,textarea{background:#000;color:#33ff33;border:1px solid #33ff33;font-family:inherit;font-size:14px;padding:4px}
  textarea{width:100%;height:120px;box-sizing:border-box}
  button{background:#000;color:#33ff33;border:1px solid #33ff33;font-family:inherit;padding:4px 12px;cursor:pointer}
  button:hover{background:#33ff33;color:#000}
  hr{border:0;border-top:1px dashed #33ff33;margin:16px 0}
  .msg{border:1px solid #1a7a1a;padding:8px;margin-bottom:8px}
  .msg h4{margin:0 0 4px 0;color:#7fff7f}
  .meta{color:#1a9a1a;font-size:12px}
  .err{color:#ff4444}
  .hidden{display:none}
  .bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
</style>
</head>
<body>
<div class="wrap">
<pre class="logo">
 ____  _     ____   ____ _   _    _  _____
/ ___|| |   |  _ \ / ___| | | |  / \|_   _|
\___ \| |   | | | | |   | |_| | / _ \ | |
 ___) | |___| |_| | |___|  _  |/ ___ \| |
|____/|_____|____/ \____|_| |_/_/   \_\_|
</pre>
<div class="bar">
  <span class="meta">server: __SERVER__</span>
  <span class="meta">user: <span id="whoami">не авторизован</span></span>
</div>
<div id="status" class="meta"></div>
<hr>

<div id="auth_view">
  <div class="row"><b>-- регистрация --</b></div>
  <div class="row"><label>login:</label><input id="reg_user" autocomplete="off"></div>
  <div class="row"><label>password:</label><input id="reg_pass" type="password"></div>
  <div class="row"><button onclick="doRegister()">[ СОЗДАТЬ ]</button></div>

  <div class="row" style="margin-top:16px"><b>-- вход --</b></div>
  <div class="row"><label>login:</label><input id="log_user" autocomplete="off"></div>
  <div class="row"><label>password:</label><input id="log_pass" type="password"></div>
  <div class="row"><button onclick="doLogin()">[ ВОЙТИ ]</button></div>
</div>

<div id="mail_view" class="hidden">
  <div class="row"><b>-- новое сообщение --</b></div>
  <div class="row"><label>кому:</label><input id="to" style="width:340px" placeholder="user@otherdomain"></div>
  <div class="row"><label>тема:</label><input id="subject" style="width:340px"></div>
  <div class="row"><textarea id="body" placeholder="текст..."></textarea></div>
  <div class="row">
    <button onclick="doSend()">[ ОТПРАВИТЬ ]</button>
    <button onclick="refreshInbox()">[ ОБНОВИТЬ ]</button>
    <button onclick="doLogout()">[ ВЫЙТИ ]</button>
  </div>
  <hr>
  <div class="row"><b>-- входящие --</b></div>
  <div id="inbox"></div>
</div>

</div>
<script>
let token = localStorage.getItem('sldchat_token') || '';
let username = localStorage.getItem('sldchat_user') || '';
const SERVER_DOMAIN = "__SERVER__";

async function api(path, opts={}){
  opts.headers = Object.assign({'Content-Type':'application/json'}, opts.headers||{});
  if (token) opts.headers['Authorization'] = 'Bearer ' + token;
  const r = await fetch(path, opts);
  const data = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
function esc(s){ return (s||'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function setStatus(t, err){ const el=document.getElementById('status'); el.textContent=t; el.className = err?'err':'meta'; }

async function doRegister(){
  const u=document.getElementById('reg_user').value.trim();
  const p=document.getElementById('reg_pass').value;
  if(!u||!p) return setStatus('Заполните поля', true);
  try{
    const d = await api('/api/register',{method:'POST',body:JSON.stringify({username:u,password:p})});
    setStatus('OK: создан ' + d.address + ' (теперь войдите)');
  }catch(e){ setStatus('Ошибка: '+e.message, true); }
}

async function doLogin(){
  const u=document.getElementById('log_user').value.trim();
  const p=document.getElementById('log_pass').value;
  try{
    const d = await api('/api/login',{method:'POST',body:JSON.stringify({username:u,password:p})});
    token=d.token; username=d.username;
    localStorage.setItem('sldchat_token',token);
    localStorage.setItem('sldchat_user',username);
    setStatus('OK: добро пожаловать, '+username);
    render(); refreshInbox();
  }catch(e){ setStatus('Ошибка: '+e.message, true); }
}

function doLogout(){
  token=''; username='';
  localStorage.removeItem('sldchat_token');
  localStorage.removeItem('sldchat_user');
  setStatus('Вы вышли');
  render();
}

async function refreshInbox(){
  if(!token) return;
  try{
    const d = await api('/api/inbox');
    const box = document.getElementById('inbox');
    if(!d.messages.length){ box.innerHTML='<div class="meta">-- пусто --</div>'; return; }
    box.innerHTML = d.messages.map(m=>`
      <div class="msg">
        <h4>${esc(m.subject||'(без темы)')}</h4>
        <div class="meta">от: ${esc(m.from_addr)}<br>кому: ${esc(m.to_addr)}<br>${new Date(m.ts*1000).toLocaleString()}<br>id: ${esc(m.msg_id||'')}</div>
        <pre style="white-space:pre-wrap;margin:8px 0 0 0;">${esc(m.body)}</pre>
      </div>`).reverse().join('');
  }catch(e){ setStatus('Ошибка: '+e.message, true); }
}

async function doSend(){
  const to=document.getElementById('to').value.trim();
  const subject=document.getElementById('subject').value;
  const body=document.getElementById('body').value;
  if(!to||!body) return setStatus('Заполните "кому" и текст', true);
  try{
    const d = await api('/api/send',{method:'POST',body:JSON.stringify({to,subject,body})});
    setStatus(d.local ? 'Отправлено локально' : ('Отправлено через федерацию -> '+d.via));
    document.getElementById('subject').value='';
    document.getElementById('body').value='';
    refreshInbox();
  }catch(e){ setStatus('Ошибка: '+e.message, true); }
}

function render(){
  const logged = !!token;
  document.getElementById('auth_view').classList.toggle('hidden', logged);
  document.getElementById('mail_view').classList.toggle('hidden', !logged);
  document.getElementById('whoami').textContent = logged ? (username+'@'+SERVER_DOMAIN) : 'не авторизован';
}

render();
if (token) refreshInbox();
setInterval(()=>{ if(token) refreshInbox(); }, 5000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE.replace("__SERVER__", SERVER_NAME)
