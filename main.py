# main.py — SLD Talk (SMS + calls, in-memory)
import time, random, html, uuid as uuidlib
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="SLD Talk", version="1.0")

users = {}   # uuid -> {uuid, code, nick, created_at}
codes = {}   # code -> uuid
sms   = []   # {id, from_uuid, to_uuid, text, ts, read}
calls = []   # {id, from_uuid, to_uuid, status, started, ended}

# ============== MODELS ==============
class RegisterReq(BaseModel):
    uuid: str
    nick: str = ""

class SendSmsReq(BaseModel):
    from_uuid: str
    to_uuid: str
    text: str

class CallReq(BaseModel):
    from_uuid: str
    to_uuid: str

class AnswerReq(BaseModel):
    call_id: str
    accept: bool

class EndReq(BaseModel):
    call_id: str

# ============== UTILS ==============
def now_ms(): return int(time.time() * 1000)

def gen_code():
    for _ in range(200):
        c = f"{random.randint(100000, 999999):06d}"
        if c not in codes:
            return c
    raise HTTPException(500, "code generation failed")

def esc(s): return html.escape(s or "", quote=True)
def fmt_time(ms):
    try: return time.strftime("%d.%m.%Y %H:%M", time.localtime(ms/1000))
    except Exception: return ""

def ensure_user(uid, nick=None):
    u = users.get(uid)
    if u is None:
        code = gen_code()
        u = {"uuid": uid, "code": code, "nick": (nick or "").strip() or ("User" + code[-3:]),
             "created_at": now_ms()}
        users[uid] = u
        codes[code] = uid
    return u

def user_public(u):
    return {"uuid": u["uuid"], "code": u["code"], "nick": u["nick"]}

# ============== HTML ==============
CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#0e1621;color:#fff;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;min-height:100vh}
a{color:#8fd3ff;text-decoration:none}
.container{max-width:640px;margin:0 auto;padding:20px 18px 60px}
.header{background:#17212b;padding:16px 20px;display:flex;align-items:center;gap:12px;
  border-bottom:1px solid #0a1018}
.header .logo{width:40px;height:40px;border-radius:50%;
  background:linear-gradient(135deg,#3a7bd5,#8f3ad5);
  display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:20px;color:#fff}
.header .title{font-weight:700;font-size:20px;color:#fff}
.header .sub{color:#7d8b99;font-size:12px;margin-top:2px}
.hero{text-align:center;padding:40px 16px 10px}
.hero .big-logo{width:96px;height:96px;border-radius:50%;
  background:linear-gradient(135deg,#3a7bd5,#8f3ad5);
  display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:46px;color:#fff;margin:0 auto 18px}
.hero h1{font-size:26px;margin:0 0 8px}
.hero p{color:#7d8b99;margin:0;font-size:14px}
.card{background:#17212b;border-radius:14px;padding:18px;margin-top:16px}
.section-label{color:#aab6c3;font-size:13px;margin-bottom:8px}
input[type=text]{flex:1;background:#0c1014;color:#fff;border:1px solid #0a1018;
  border-radius:10px;padding:14px;font-size:18px;letter-spacing:.15em;
  outline:none;font-family:inherit}
input[type=text]:focus{border-color:#5288c1}
input[type=text]::placeholder{color:#5e6a76;letter-spacing:normal}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;
  border:none;border-radius:10px;padding:14px 22px;font-size:15px;font-weight:600;
  color:#fff;background:#2a3340;cursor:pointer;text-decoration:none;
  transition:transform .06s ease,filter .12s ease;font-family:inherit}
.btn:hover{filter:brightness(1.1)}
.btn:active{transform:scale(.97)}
.btn-primary{background:#5288c1}
.btn-block{width:100%}
.btn-svg{width:20px;height:20px;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.user-badge{display:inline-block;background:#222c3a;color:#8fd3ff;
  padding:6px 12px;border-radius:8px;font-weight:700;font-size:15px;letter-spacing:.08em}
.not-found{text-align:center;padding:60px 16px}
.not-found .code{font-size:72px;font-weight:800;color:#ff7676;letter-spacing:.1em}
.not-found p{color:#7d8b99}
.footer{text-align:center;color:#5e6a76;font-size:12px;padding:30px 16px 10px}
.legal{color:#c5cfdb;font-size:14px;line-height:1.6}
.legal h2{color:#fff;font-size:18px;margin:22px 0 8px}
"""

SVG_DL = '<svg class="btn-svg" viewBox="0 0 24 24"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 21h16"/></svg>'
SVG_SEARCH = '<svg class="btn-svg" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M16.5 16.5L21 21"/></svg>'
SVG_BACK = '<svg class="btn-svg" viewBox="0 0 24 24"><path d="M15 5L8 12l7 7"/></svg>'

DL_JS = """
function downloadSoon(b){var o=b.innerHTML;b.innerHTML='Скоро!';b.disabled=true;
setTimeout(function(){b.innerHTML=o;b.disabled=false;},1600);}
"""

def page(title, body):
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — SLD</title><style>{CSS}</style></head><body>
<div class="header">
  <div class="logo">S</div>
  <div><div class="title"><a href="/" style="color:#fff">SLD Talk</a></div>
  <div class="sub">SMS и звонки без номера</div></div>
</div>
{body}
<div class="footer">SLD · <a href="/privacy">Политика конфиденциальности</a></div>
</body></html>"""

def index_page():
    return page("Главная", f"""
<div class="container">
  <div class="hero">
    <div class="big-logo">S</div>
    <h1>SLD Talk</h1>
    <p>SMS и звонки без номера телефона</p>
  </div>

  <button class="btn btn-primary btn-block" style="margin-top:24px"
          onclick="downloadSoon(this)">{SVG_DL} Скачать приложение</button>

  <div class="card">
    <div class="section-label">Найти пользователя по коду</div>
    <form class="search-row" onsubmit="event.preventDefault(); openCode();">
      <input id="code" type="text" inputmode="numeric" pattern="[0-9]{{6}}"
             maxlength="6" placeholder="6 цифр" autocomplete="off">
      <button type="submit" class="btn btn-primary">{SVG_SEARCH} Найти</button>
    </form>
    <div id="err" class="error" style="color:#ff7676;font-size:13px;margin-top:8px"></div>
  </div>
</div>
<script>
{DL_JS}
function openCode(){{
  var v=document.getElementById('code').value.trim();
  var e=document.getElementById('err');
  if(!/^\\d{{6}}$/.test(v)){{e.textContent='Введите ровно 6 цифр';return;}}
  e.textContent='';window.location.href='/'+v;
}}
document.getElementById('code').addEventListener('keydown',function(ev){{
  if(ev.key==='Enter'){{ev.preventDefault();openCode();}}
}});
</script>""")

def user_page(u):
    return page(u['code'], f"""
<div class="container">
  <a class="btn" href="/" style="margin-top:6px">{SVG_BACK} На главную</a>
  <div class="card">
    <div style="text-align:center;padding:12px 0">
      <div style="width:88px;height:88px;border-radius:50%;margin:0 auto 14px;
        background:linear-gradient(135deg,#5288c1,#8f3ad5);
        display:flex;align-items:center;justify-content:center;
        font-size:38px;font-weight:700">{esc((u['nick'] or '?')[:1].upper())}</div>
      <div style="font-size:20px;font-weight:700">{esc(u['nick'])}</div>
      <div style="margin-top:8px"><span class="user-badge">{esc(u['code'])}</span></div>
    </div>
  </div>
  <button class="btn btn-primary btn-block" style="margin-top:20px"
          onclick="downloadSoon(this)">{SVG_DL} Скачать приложение</button>
</div>
<script>{DL_JS}</script>""")

def not_found_page(code):
    return page("Не найдено", f"""
<div class="container"><div class="not-found">
  <div class="code">{esc(code)}</div>
  <p>Пользователь не найден</p>
  <a class="btn btn-primary" href="/" style="margin-top:20px">{SVG_BACK} На главную</a>
</div></div>""")

def privacy_page():
    return page("Политика", """
<div class="container"><a class="btn" href="/" style="margin-top:6px">Назад</a>
<div class="card"><h1 style="margin-top:0">Политика конфиденциальности</h1>
<div class="legal">
<p>SLD Talk — анонимная платформа SMS и звонков. Никакой реальный номер
телефона не требуется.</p>
<h2>1. Какие данные мы храним</h2>
<ul><li><b>Анонимный UUID устройства</b> и <b>виртуальный 6-значный код</b>,
автоматически выданный при регистрации.</li>
<li><b>Ник</b>, который вы ввели (можно любой).</li>
<li><b>Содержимое SMS</b> и <b>история звонков</b> — только между участниками.</li></ul>
<h2>2. Чего мы НЕ делаем</h2>
<ul><li>Не запрашиваем реальный телефон, email, имя.</li>
<li>Не используем трекеры и рекламу.</li>
<li>Не передаём данные третьим лицам.</li></ul>
<h2>3. Где хранятся данные</h2>
<p>В оперативной памяти сервера и стираются при перезапуске.</p>
<h2>4. Ваши права</h2>
<ul><li>Удалить аккаунт можно в настройках приложения.</li></ul>
<h2>5. Контакты</h2>
<p>privacy@sldchat.fastapicloud.dev</p>
<p style="color:#7d8b99;margin-top:24px">Обновлено: 2025</p>
</div></div></div>""")

# ============== ROUTES ==============
@app.get("/health")
def health():
    return {"status": "ok", "ts": now_ms(), "users": len(users),
            "sms": len(sms), "calls": len(calls)}

@app.post("/register")
def register(r: RegisterReq):
    u = ensure_user(r.uuid, r.nick)
    if (r.nick or "").strip():
        u["nick"] = r.nick.strip()[:32]
    return user_public(u)

@app.get("/me")
def me(uuid: str):
    u = users.get(uuid)
    if not u: raise HTTPException(404, "user not found")
    return user_public(u)

@app.get("/users")
def all_users():
    return [user_public(u) for u in users.values()]

@app.get("/search")
def search(q: str):
    q = (q or "").strip()
    if not q: return []
    res = []
    ql = q.lower()
    for u in users.values():
        if q.isdigit() and len(q) == 6 and u["code"] == q:
            res.append(user_public(u)); continue
        if ql in (u["nick"] or "").lower():
            res.append(user_public(u))
    return res

@app.get("/user/{code}")
def user_by_code(code: str):
    uid = codes.get(code)
    if not uid: raise HTTPException(404, "not found")
    return user_public(users[uid])

@app.post("/sms")
def send_sms(r: SendSmsReq):
    if r.from_uuid not in users: raise HTTPException(404, "sender not found")
    to = users.get(r.to_uuid) or ensure_user(r.to_uuid)
    text = (r.text or "").strip()
    if not text: raise HTTPException(400, "empty text")
    m = {"id": str(uuidlib.uuid4()), "from_uuid": r.from_uuid,
         "to_uuid": to["uuid"], "text": text[:2000],
         "ts": now_ms(), "read": False}
    sms.append(m)
    return m

@app.get("/sms/thread")
def sms_thread(u1: str, u2: str, since: int = 0):
    out = []
    for m in sms:
        if m["ts"] <= since: continue
        if (m["from_uuid"] == u1 and m["to_uuid"] == u2) or \
           (m["from_uuid"] == u2 and m["to_uuid"] == u1):
            out.append(m)
    return out

@app.get("/sms/threads/{uid}")
def sms_threads(uid: str):
    last = {}
    for m in sms:
        if m["from_uuid"] == uid: partner = m["to_uuid"]
        elif m["to_uuid"] == uid: partner = m["from_uuid"]
        else: continue
        if partner not in last or m["ts"] > last[partner]["ts"]:
            last[partner] = m
    res = []
    for p, m in last.items():
        pu = users.get(p) or ensure_user(p)
        res.append({"partner_uuid": p, "partner_code": pu["code"],
                    "partner_nick": pu["nick"], "last_text": m["text"],
                    "last_ts": m["ts"], "last_from": m["from_uuid"]})
    res.sort(key=lambda x: x["last_ts"], reverse=True)
    return res

@app.get("/sms/inbox")
def sms_inbox(uid: str, since: int = 0):
    out = []
    for m in sms:
        if m["to_uuid"] != uid or m["ts"] <= since: continue
        mm = dict(m)
        fu = users.get(m["from_uuid"]) or ensure_user(m["from_uuid"])
        mm["from_code"] = fu["code"]; mm["from_nick"] = fu["nick"]
        out.append(mm)
    return out

@app.post("/call")
def start_call(r: CallReq):
    if r.from_uuid not in users: raise HTTPException(404, "caller not found")
    to = users.get(r.to_uuid) or ensure_user(r.to_uuid)
    c = {"id": str(uuidlib.uuid4()), "from_uuid": r.from_uuid, "to_uuid": to["uuid"],
         "status": "ringing", "started": now_ms(), "ended": None}
    calls.append(c)
    return c

@app.get("/call/pending")
def call_pending(uid: str):
    for c in reversed(calls):
        if c["to_uuid"] == uid and c["status"] == "ringing":
            return c
    return None

@app.get("/call/state/{cid}")
def call_state(cid: str):
    for c in calls:
        if c["id"] == cid: return c
    raise HTTPException(404, "call not found")

@app.post("/call/answer")
def call_answer(r: AnswerReq):
    for c in calls:
        if c["id"] == r.call_id:
            if r.accept: c["status"] = "accepted"
            else: c["status"] = "declined"; c["ended"] = now_ms()
            return c
    raise HTTPException(404, "call not found")

@app.post("/call/end")
def call_end(r: EndReq):
    for c in calls:
        if c["id"] == r.call_id:
            if c["status"] in ("ringing", "accepted"):
                c["status"] = "ended"; c["ended"] = now_ms()
            return c
    raise HTTPException(404, "call not found")

@app.get("/call/history/{uid}")
def call_history(uid: str):
    res = []
    for c in calls:
        if c["from_uuid"] != uid and c["to_uuid"] != uid: continue
        other = c["to_uuid"] if c["from_uuid"] == uid else c["from_uuid"]
        ou = users.get(other) or ensure_user(other)
        res.append({"id": c["id"], "other_uuid": other, "other_code": ou["code"],
                    "other_nick": ou["nick"], "outgoing": c["from_uuid"] == uid,
                    "status": c["status"], "started": c["started"], "ended": c["ended"]})
    res.sort(key=lambda x: x["started"], reverse=True)
    return res

@app.post("/reset/{uid}")
def reset(uid: str):
    u = users.pop(uid, None)
    if u: codes.pop(u["code"], None)
    global sms
    sms = [m for m in sms if m["from_uuid"] != uid and m["to_uuid"] != uid]
    return {"ok": True}

# ============== WEB ==============
@app.get("/", response_class=HTMLResponse)
def web_index(): return index_page()

@app.get("/privacy", response_class=HTMLResponse)
def web_privacy(): return privacy_page()

@app.get("/{code}", response_class=HTMLResponse)
def web_user(code: str):
    if not (len(code) == 6 and code.isdigit()):
        return HTMLResponse(not_found_page(code), status_code=404)
    uid = codes.get(code)
    if not uid:
        return HTMLResponse(not_found_page(code), status_code=404)
    return user_page(users[uid])
