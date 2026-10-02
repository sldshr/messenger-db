import secrets
import time
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

app = FastAPI()

users: dict[str, str] = {}
chats: dict[str, dict] = {}
sockets: dict[str, dict[str, WebSocket]] = {}


@app.get("/", response_class=HTMLResponse)
def root():
    return PAGE


@app.post("/api/login")
def login(data: dict):
    nick = (data.get("nickname") or "").strip()
    if not nick or len(nick) > 20:
        raise HTTPException(400, "bad nickname")
    token = secrets.token_urlsafe(16)
    users[token] = nick
    return {"token": token, "nickname": nick}


@app.get("/api/chats")
def my_chats(token: str):
    nick = users.get(token)
    if not nick:
        raise HTTPException(401)
    out = []
    for cid, c in chats.items():
        if nick in c["members"]:
            last = c["messages"][-1]["text"] if c["messages"] else "Нет сообщений"
            out.append({"id": cid, "name": c["name"], "last": last})
    return out


@app.post("/api/chats")
def create_chat(data: dict):
    nick = users.get(data.get("token"))
    if not nick:
        raise HTTPException(401)
    name = (data.get("name") or "").strip()[:40] or "Без названия"
    cid = secrets.token_hex(3)
    while cid in chats:
        cid = secrets.token_hex(3)
    chats[cid] = {"name": name, "members": {nick}, "messages": []}
    return {"id": cid, "name": name}


@app.post("/api/chats/join")
def join_chat(data: dict):
    nick = users.get(data.get("token"))
    if not nick:
        raise HTTPException(401)
    cid = (data.get("id") or "").strip().lower()
    if cid not in chats:
        raise HTTPException(404, "not found")
    chats[cid]["members"].add(nick)
    return {"id": cid, "name": chats[cid]["name"]}


@app.websocket("/ws/{cid}")
async def chat_ws(websocket: WebSocket, cid: str, token: str = ""):
    nick = users.get(token)
    if not nick or cid not in chats:
        await websocket.close(code=4001)
        return
    await websocket.accept()
    chats[cid]["members"].add(nick)
    sockets.setdefault(cid, {})[token] = websocket
    try:
        for m in chats[cid]["messages"][-200:]:
            await websocket.send_json({"type": "msg", **m})
        while True:
            data = await websocket.receive_json()
            text = (data.get("text") or "").strip()[:2000]
            if not text:
                continue
            msg = {"nick": nick, "text": text, "ts": int(time.time() * 1000)}
            chats[cid]["messages"].append(msg)
            for t, ws in list(sockets.get(cid, {}).items()):
                try:
                    await ws.send_json({"type": "msg", **msg})
                except Exception:
                    sockets.get(cid, {}).pop(t, None)
    except WebSocketDisconnect:
        pass
    finally:
        sockets.get(cid, {}).pop(token, None)


PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Чаты</title>
<style>
:root{
  --primary:#6750A4;--on-primary:#FFFFFF;
  --primary-container:#EADDFF;--on-primary-container:#21005D;
  --surface:#FEF7FF;--on-surface:#1D1B20;
  --surface-c:#F3EDF7;--surface-ch:#ECE6F0;--surface-cl:#E6E0E9;
  --on-surface-v:#49454F;--outline:#79747E;--outline-v:#CAC4D0;
  --error:#B3261E;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overscroll-behavior:none}
body{
  font-family:Roboto,system-ui,-apple-system,"Segoe UI",sans-serif;
  background:var(--surface);color:var(--on-surface);overflow:hidden;
}
.screen{
  position:fixed;inset:0;display:flex;flex-direction:column;
  background:var(--surface);
}
.hidden{display:none!important}

/* login */
#login{align-items:center;justify-content:center;padding:24px}
.login-card{width:100%;max-width:400px;display:flex;flex-direction:column;gap:28px}
.login-card h1{
  font-size:38px;font-weight:400;letter-spacing:.5px;
  text-align:center;color:var(--primary);
}

/* text field */
.tf{position:relative}
.tf input{
  width:100%;padding:16px;border:1px solid var(--outline);
  border-radius:6px;background:transparent;font-size:16px;
  color:var(--on-surface);outline:none;font-family:inherit;
  transition:border-color .15s,border-width .15s;
}
.tf input:focus{border:2px solid var(--primary);padding:15px}
.tf label{
  position:absolute;left:12px;top:50%;transform:translateY(-50%);
  background:var(--surface);padding:0 4px;font-size:16px;
  color:var(--on-surface-v);pointer-events:none;transition:.15s;
}
.tf input:focus + label,
.tf input:not(:placeholder-shown) + label{
  top:0;font-size:12px;color:var(--primary);
}

/* buttons */
.btn{
  border:none;border-radius:100px;padding:16px 24px;
  font-size:15px;font-weight:500;font-family:inherit;
  cursor:pointer;transition:box-shadow .15s,opacity .15s;
}
.btn:active{opacity:.85}
.btn-filled{background:var(--primary);color:var(--on-primary)}
.btn-filled:hover{box-shadow:0 2px 6px rgba(0,0,0,.25)}
.btn-tonal{background:var(--primary-container);color:var(--on-primary-container)}
.btn-text{background:transparent;color:var(--primary);padding:12px 16px}
.icon-btn{
  background:transparent;border:none;color:var(--on-surface-v);
  width:40px;height:40px;border-radius:50%;cursor:pointer;
  font-size:20px;display:flex;align-items:center;justify-content:center;
  transition:background .15s;font-family:inherit;
}
.icon-btn:hover{background:var(--surface-c)}

/* top bar */
.topbar{
  display:flex;align-items:center;gap:12px;
  padding:12px 20px;min-height:64px;
  background:var(--surface);flex-shrink:0;
  padding-top:calc(12px + env(safe-area-inset-top));
}
.topbar h2{font-size:22px;font-weight:400;flex:1;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}

/* chat list */
.chat-list{flex:1;overflow-y:auto;padding:8px 0 100px}
.chat-item{
  display:flex;align-items:center;gap:16px;
  padding:12px 20px;cursor:pointer;transition:background .15s;
}
.chat-item:hover{background:var(--surface-c)}
.avatar{
  width:48px;height:48px;border-radius:50%;flex-shrink:0;
  background:var(--primary-container);color:var(--on-primary-container);
  display:flex;align-items:center;justify-content:center;
  font-size:20px;font-weight:500;
}
.chat-info{flex:1;min-width:0}
.chat-name{font-size:16px;font-weight:500;margin-bottom:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-last{font-size:14px;color:var(--on-surface-v);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.empty{
  text-align:center;color:var(--on-surface-v);
  padding:64px 24px;font-size:15px;line-height:1.5;
}

/* fab */
.fab{
  position:fixed;right:20px;
  bottom:calc(20px + env(safe-area-inset-bottom));
  width:56px;height:56px;border-radius:16px;
  background:var(--primary-container);color:var(--on-primary-container);
  border:none;font-size:26px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 3px 10px rgba(0,0,0,.18);
  transition:box-shadow .15s,transform .15s;
}
.fab:hover{box-shadow:0 5px 16px rgba(0,0,0,.25)}
.fab:active{transform:scale(.96)}

/* dialog */
.backdrop{
  position:fixed;inset:0;background:rgba(0,0,0,.4);
  display:flex;align-items:center;justify-content:center;
  padding:24px;z-index:100;
}
.dialog{
  background:var(--surface-ch);border-radius:28px;padding:24px;
  width:100%;max-width:420px;max-height:90vh;overflow-y:auto;
  display:flex;flex-direction:column;gap:16px;
}
.dialog h3{font-size:22px;font-weight:400}
.dialog .divider{height:1px;background:var(--outline-v);margin:4px 0}
.dialog .row{display:flex;justify-content:flex-end;gap:8px}

/* messages */
.messages{
  flex:1;overflow-y:auto;padding:12px 16px;
  display:flex;flex-direction:column;gap:8px;
}
.msg{
  max-width:78%;padding:10px 14px;border-radius:18px;
  font-size:15px;line-height:1.35;word-wrap:break-word;
  white-space:pre-wrap;
}
.msg.own{align-self:flex-end;background:var(--primary);color:var(--on-primary);
  border-bottom-right-radius:4px}
.msg.other{align-self:flex-start;background:var(--surface-ch);
  border-bottom-left-radius:4px}
.msg .nick{font-size:12px;font-weight:500;opacity:.75;margin-bottom:2px;color:var(--primary)}

/* msg form */
.msg-form{
  display:flex;gap:8px;padding:8px 12px;
  padding-bottom:calc(12px + env(safe-area-inset-bottom));
  background:var(--surface);flex-shrink:0;
}
.msg-form input{
  flex:1;border:none;background:var(--surface-ch);
  border-radius:100px;padding:14px 20px;font-size:15px;
  outline:none;color:var(--on-surface);font-family:inherit;
}
.msg-form input:focus{background:var(--surface-cl)}
.send{
  width:48px;height:48px;border-radius:50%;flex-shrink:0;
  background:var(--primary);color:var(--on-primary);
  border:none;cursor:pointer;font-size:20px;
  display:flex;align-items:center;justify-content:center;
}
.send:hover{box-shadow:0 2px 6px rgba(0,0,0,.25)}

@media (min-width:720px){
  .chat-list{padding-left:max(0px,calc(50vw - 360px));
             padding-right:max(0px,calc(50vw - 360px))}
  .messages{padding-left:max(16px,calc(50vw - 360px));
            padding-right:max(16px,calc(50vw - 360px))}
  .msg-form{padding-left:max(12px,calc(50vw - 360px));
            padding-right:max(12px,calc(50vw - 360px))}
  .fab{right:calc(50vw - 360px + 20px)}
}
</style>
</head>
<body>

<div id="login" class="screen">
  <div class="login-card">
    <h1>Чаты</h1>
    <div class="tf">
      <input id="nick" placeholder=" " maxlength="20" autocomplete="off">
      <label for="nick">Ваш ник</label>
    </div>
    <button class="btn btn-filled" id="continueBtn">Продолжить</button>
  </div>
</div>

<div id="listScreen" class="screen hidden">
  <header class="topbar"><h2>Чаты</h2></header>
  <div id="chatList" class="chat-list"></div>
  <button class="fab" id="fab">+</button>
</div>

<div id="roomScreen" class="screen hidden">
  <header class="topbar">
    <button class="icon-btn" id="back">&#8592;</button>
    <h2 id="roomName"></h2>
  </header>
  <div id="messages" class="messages"></div>
  <form id="msgForm" class="msg-form">
    <input id="msgInput" placeholder="Сообщение..." autocomplete="off" maxlength="2000">
    <button type="submit" class="send">&#10148;</button>
  </form>
</div>

<div id="dialog" class="backdrop hidden">
  <div class="dialog">
    <h3>Новый чат</h3>
    <div class="tf">
      <input id="chatName" placeholder=" " maxlength="40">
      <label for="chatName">Название</label>
    </div>
    <button class="btn btn-tonal" id="createBtn">Создать</button>
    <div class="divider"></div>
    <h3>Найти чат</h3>
    <div class="tf">
      <input id="findId" placeholder=" " maxlength="20">
      <label for="findId">ID чата</label>
    </div>
    <button class="btn btn-tonal" id="findBtn">Войти</button>
    <div class="row">
      <button class="btn btn-text" id="closeDialog">Закрыть</button>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
let token = null, nick = null, currentChat = null, ws = null;

const esc = s => String(s).replace(/[&<>"']/g, m =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));

async function api(path, method='GET', body){
  const r = await fetch(path, {
    method,
    headers: {'Content-Type':'application/json'},
    body: body ? JSON.stringify(body) : undefined
  });
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}

function show(id){
  ['login','listScreen','roomScreen'].forEach(s =>
    document.getElementById(s).classList.toggle('hidden', s !== id));
}

$('#continueBtn').onclick = async () => {
  const n = $('#nick').value.trim();
  if(!n) return;
  try {
    const r = await api('/api/login','POST',{nickname:n});
    token = r.token; nick = r.nickname;
    openList();
  } catch(e) { alert('Не удалось войти'); }
};
$('#nick').addEventListener('keydown', e => { if(e.key === 'Enter') $('#continueBtn').click(); });

async function openList(){
  show('listScreen');
  const list = await api('/api/chats?token=' + encodeURIComponent(token));
  const el = $('#chatList');
  el.innerHTML = '';
  if(!list.length){
    el.innerHTML = '<div class="empty">Пока нет чатов.<br>Нажмите + чтобы создать или найти.</div>';
    return;
  }
  for(const c of list){
    const d = document.createElement('div');
    d.className = 'chat-item';
    d.innerHTML = `
      <div class="avatar">${esc((c.name[0] || '?').toUpperCase())}</div>
      <div class="chat-info">
        <div class="chat-name">${esc(c.name)}</div>
        <div class="chat-last">${esc(c.last)}</div>
      </div>`;
    d.onclick = () => openRoom(c);
    el.appendChild(d);
  }
}

function openRoom(chat){
  currentChat = chat;
  $('#roomName').textContent = chat.name + ' · #' + chat.id;
  $('#messages').innerHTML = '';
  show('roomScreen');
  if(ws) ws.close();
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/${chat.id}?token=${encodeURIComponent(token)}`);
  ws.onmessage = e => {
    const d = JSON.parse(e.data);
    if(d.type === 'msg') addMessage(d);
  };
}

function addMessage(d){
  const box = $('#messages');
  const m = document.createElement('div');
  m.className = 'msg ' + (d.nick === nick ? 'own' : 'other');
  m.innerHTML = (d.nick === nick ? '' : `<div class="nick">${esc(d.nick)}</div>`) + esc(d.text);
  box.appendChild(m);
  box.scrollTop = box.scrollHeight;
}

$('#back').onclick = () => { if(ws){ ws.close(); ws = null; } openList(); };

$('#msgForm').onsubmit = e => {
  e.preventDefault();
  const v = $('#msgInput').value.trim();
  if(!v || !ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({text: v}));
  $('#msgInput').value = '';
};

$('#fab').onclick = () => $('#dialog').classList.remove('hidden');
$('#closeDialog').onclick = () => $('#dialog').classList.add('hidden');
$('#dialog').onclick = e => { if(e.target.id === 'dialog') $('#dialog').classList.add('hidden'); };

$('#createBtn').onclick = async () => {
  const name = $('#chatName').value.trim();
  const c = await api('/api/chats','POST',{token, name});
  $('#chatName').value = '';
  $('#dialog').classList.add('hidden');
  openRoom(c);
};

$('#findBtn').onclick = async () => {
  const id = $('#findId').value.trim();
  if(!id) return;
  try {
    const c = await api('/api/chats/join','POST',{token, id});
    $('#findId').value = '';
    $('#dialog').classList.add('hidden');
    openRoom(c);
  } catch(e) { alert('Чат не найден'); }
};

$('#nick').focus();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
