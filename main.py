# main.py
# pip install fastapi uvicorn
# uvicorn main:app --reload --host 0.0.0.0 --port 8000

import secrets
from typing import Dict, List, Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI(title="CollabBoard", version="2.0.0")

MAX_ELEMENTS = 20000
MAX_NICK = 24

AVATAR_COLORS = [
    "#818cf8", "#34d399", "#fbbf24", "#f472b6", "#38bdf8",
    "#a78bfa", "#fb923c", "#4ade80", "#f87171", "#22d3ee",
    "#e879f9", "#2dd4bf", "#facc15", "#60a5fa",
]


def color_for(nick: str) -> str:
    h = 0
    for ch in nick:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return AVATAR_COLORS[h % len(AVATAR_COLORS)]


class Client:
    __slots__ = ("ws", "nick", "color", "cursor", "cid", "is_owner")

    def __init__(self, ws: WebSocket, nick: str, color: str, cid: str, is_owner: bool):
        self.ws = ws
        self.nick = nick
        self.color = color
        self.cursor: Optional[List[float]] = None
        self.cid = cid
        self.is_owner = is_owner

    def pub(self) -> dict:
        return {
            "id": self.cid,
            "nick": self.nick,
            "color": self.color,
            "cursor": self.cursor,
        }


class Room:
    def __init__(self) -> None:
        self.owner_token: str = secrets.token_urlsafe(20)
        self.clients: Dict[WebSocket, Client] = {}
        self.elements: List[dict] = []

    def users(self) -> List[dict]:
        return [c.pub() for c in self.clients.values()]


rooms: Dict[str, Room] = {}


async def send_json(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_json(payload)
    except Exception:
        pass


async def broadcast(room: Room, payload: dict, exclude: Optional[WebSocket] = None) -> None:
    dead: List[WebSocket] = []
    for ws in list(room.clients.keys()):
        if ws is exclude:
            continue
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        room.clients.pop(ws, None)


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.websocket("/ws/{room_id}")
async def ws_endpoint(
    websocket: WebSocket,
    room_id: str,
    nick: str = Query("Гость"),
    token: str = Query(""),
    cid: str = Query(""),
):
    await websocket.accept()
    nick = (nick or "Гость")[:MAX_NICK].strip() or "Гость"
    if not cid:
        cid = secrets.token_hex(6)

    room = rooms.get(room_id)
    created = False
    if room is None:
        room = Room()
        rooms[room_id] = room
        created = True

    # Ownership:
    #  - создатель комнаты получает токен
    #  - при повторном входе с тем же токеном — владелец
    #  - если комната пуста и токен не подошёл — новый владелец забирает её
    is_owner = created or (bool(token) and token == room.owner_token)
    if not is_owner and not room.clients:
        room.owner_token = secrets.token_urlsafe(20)
        is_owner = True

    client = Client(websocket, nick, color_for(nick), cid, is_owner)
    room.clients[websocket] = client

    await send_json(websocket, {
        "type": "init",
        "elements": room.elements,
        "users": room.users(),
        "is_owner": is_owner,
        "owner_token": room.owner_token if is_owner else None,
        "you": client.pub(),
        "room": room_id,
    })
    await broadcast(room, {"type": "presence", "users": room.users()}, exclude=websocket)

    try:
        while True:
            msg = await websocket.receive_json()
            t = msg.get("type")

            # --- не требует прав владельца ---
            if t == "cursor":
                x = msg.get("x")
                y = msg.get("y")
                client.cursor = [float(x), float(y)] if (x is not None and y is not None) else None
                await broadcast(room, {
                    "type": "cursor",
                    "id": cid,
                    "nick": nick,
                    "color": client.color,
                    "x": x,
                    "y": y,
                }, exclude=websocket)
                continue

            if t == "laser":
                await broadcast(room, msg, exclude=websocket)
                continue

            if t == "ping":
                await send_json(websocket, {"type": "pong"})
                continue

            # --- дальше — только владелец ---
            if not is_owner:
                continue

            if t == "add":
                el = msg.get("element")
                if el and len(room.elements) < MAX_ELEMENTS:
                    room.elements.append(el)
                await broadcast(room, {"type": "add", "element": el}, exclude=websocket)

            elif t == "batch":
                els = msg.get("elements") or []
                for el in els:
                    if len(room.elements) < MAX_ELEMENTS:
                        room.elements.append(el)
                await broadcast(room, {"type": "batch", "elements": els}, exclude=websocket)

            elif t == "update":
                el = msg.get("element") or {}
                eid = el.get("id")
                for i, cur in enumerate(room.elements):
                    if cur.get("id") == eid:
                        room.elements[i] = el
                        break
                await broadcast(room, {"type": "update", "element": el}, exclude=websocket)

            elif t == "delete":
                ids = set(msg.get("ids") or [])
                room.elements = [e for e in room.elements if e.get("id") not in ids]
                await broadcast(room, {"type": "delete", "ids": list(ids)}, exclude=websocket)

            elif t == "clear":
                room.elements.clear()
                await broadcast(room, {"type": "clear"}, exclude=websocket)

            elif t == "sync":
                els = msg.get("elements")
                if isinstance(els, list):
                    room.elements = els[:MAX_ELEMENTS]
                    await broadcast(room, {"type": "sync", "elements": room.elements}, exclude=websocket)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        room.clients.pop(websocket, None)
        await broadcast(room, {"type": "presence", "users": room.users()})
        await broadcast(room, {"type": "user_leave", "id": cid})


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CollabBoard — совместная доска</title>
<style>
:root{
  --bg:#080d19; --panel:#0e1729; --panel2:#121d33; --line:#1d2a44; --line2:#26365a;
  --text:#e7eefb; --muted:#8a9ab5; --accent:#6366f1; --accent2:#818cf8;
  --ok:#22c55e; --danger:#ef4444; --warn:#f59e0b; --r:10px;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{
  font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,sans-serif;
  background:var(--bg); color:var(--text); overflow:hidden; -webkit-font-smoothing:antialiased;
  font-size:13px;
}
button{font-family:inherit;color:inherit}
input,select,textarea{font-family:inherit}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#22314f;border-radius:6px}
::-webkit-scrollbar-thumb:hover{background:#2f4268}

.topbar{
  height:54px; display:flex; align-items:center; gap:10px; padding:0 12px;
  background:linear-gradient(180deg,#0e1729,#0b1424);
  border-bottom:1px solid var(--line); position:relative; z-index:20;
}
.brand{display:flex;align-items:center;gap:10px}
.logo{
  width:32px;height:32px;border-radius:9px;display:grid;place-items:center;font-weight:800;font-size:12px;
  background:linear-gradient(135deg,#6366f1,#22d3ee); color:#04101f; letter-spacing:-.5px;
  box-shadow:0 0 0 1px #1e2b47, 0 6px 18px -6px #6366f1aa;
}
.brand-txt{display:flex;flex-direction:column;line-height:1.1}
.brand-txt b{font-size:13.5px;letter-spacing:.2px}
.brand-txt span{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1.2px}
.divider{width:1px;height:26px;background:var(--line)}
.divider.sm{height:14px}
.board-pill{
  display:flex;align-items:center;gap:8px;padding:6px 12px;border-radius:8px;
  background:#101b30;border:1px solid var(--line);font-weight:600;font-size:12.5px;
}
.board-pill .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
.role-badge{
  padding:4px 9px;border-radius:20px;font-size:10.5px;font-weight:700;letter-spacing:.6px;
  text-transform:uppercase;border:1px solid;
}
.role-badge.owner{color:#c7d2fe;background:#312e81aa;border-color:#4338ca}
.role-badge.viewer{color:#fde68a;background:#78350f66;border-color:#b45309}
.spacer{flex:1}
.users{display:flex;align-items:center}
.avatar{
  width:29px;height:29px;border-radius:50%;display:grid;place-items:center;
  font-size:11px;font-weight:700;color:#08111f;border:2px solid #0e1729;margin-left:-8px;
  transition:transform .15s;cursor:default;
}
.avatar:first-child{margin-left:0}
.avatar:hover{transform:translateY(-2px)}
.avatar.me{box-shadow:0 0 0 2px #6366f1}
.users-count{font-size:11px;color:var(--muted);margin-left:9px}
.zoom{display:flex;align-items:center;background:#101b30;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.zoom button{background:none;border:0;padding:6px 11px;cursor:pointer;font-size:13px;color:var(--muted);transition:.15s}
.zoom button:hover{background:#182741;color:var(--text)}
.zoom .zval{min-width:56px;text-align:center;font-variant-numeric:tabular-nums;font-size:12px;font-weight:600}
.btn{
  background:#16233c;border:1px solid var(--line2);border-radius:8px;padding:7px 13px;
  cursor:pointer;font-size:12.5px;font-weight:600;transition:.15s;display:flex;align-items:center;gap:7px;
}
.btn:hover{background:#1d2d4d;border-color:#31456e}
.btn.primary{background:linear-gradient(180deg,#6366f1,#4f46e5);border-color:#4f46e5;color:#fff}
.btn.primary:hover{filter:brightness(1.1)}
.btn.ghost{background:transparent}
.btn.danger:hover{background:#3a1622;border-color:#7f2033;color:#ffb4c0}
.btn.icon{padding:7px 9px}
.btn svg{width:15px;height:15px}
.btn:disabled{opacity:.4;cursor:not-allowed}

.workspace{position:absolute;inset:54px 0 0 0;display:flex}

.rail{
  width:56px;background:var(--panel);border-right:1px solid var(--line);
  display:flex;flex-direction:column;align-items:center;gap:3px;padding:9px 0;overflow-y:auto;z-index:10;
}
.tool{
  width:40px;height:40px;border-radius:9px;display:grid;place-items:center;cursor:pointer;
  color:var(--muted);position:relative;transition:.13s;border:1px solid transparent;
}
.tool svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.tool:hover{background:#16233c;color:var(--text)}
.tool.active{background:linear-gradient(180deg,#243467,#1a2749);color:#c7d2fe;border-color:#3b4f8a;box-shadow:0 4px 14px -6px #6366f1}
.tool.active::before{content:"";position:absolute;left:-9px;top:9px;bottom:9px;width:3px;border-radius:0 3px 3px 0;background:var(--accent2)}
.tool:disabled{opacity:.28;cursor:not-allowed}
.tool:disabled:hover{background:transparent;color:var(--muted)}
.tool .tt{
  position:absolute;left:52px;top:50%;transform:translateY(-50%) scale(.95);
  background:#0a1322;border:1px solid var(--line2);padding:5px 9px;border-radius:7px;
  white-space:nowrap;font-size:11.5px;font-weight:600;opacity:0;pointer-events:none;transition:.13s;z-index:60;
  box-shadow:0 8px 24px -8px #000;
}
.tool .tt kbd{background:#1b2b47;border-radius:4px;padding:1px 5px;margin-left:6px;font-size:10px;color:var(--muted)}
.tool:hover .tt{opacity:1;transform:translateY(-50%) scale(1)}
.rail-sep{width:26px;height:1px;background:var(--line);margin:6px 0}

.stage{flex:1;position:relative;overflow:hidden;background:#0a1120}
#canvas{display:block;position:absolute;inset:0;touch-action:none;cursor:crosshair}
.stage.hand #canvas{cursor:grab}
.stage.hand.dragging #canvas{cursor:grabbing}
.stage.select #canvas{cursor:default}
.hud{
  position:absolute;left:14px;bottom:14px;display:flex;gap:8px;align-items:center;
  background:#0e1729e6;backdrop-filter:blur(10px);border:1px solid var(--line);
  border-radius:9px;padding:6px 12px;font-size:11.5px;color:var(--muted);z-index:5;
  font-variant-numeric:tabular-nums;
}
.hud b{color:var(--text);font-weight:600}
.conn{display:flex;align-items:center;gap:6px}
.conn .led{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
.conn.off .led{background:var(--danger);box-shadow:0 0 8px var(--danger)}
.conn.warn .led{background:var(--warn);box-shadow:0 0 8px var(--warn)}

.props{
  width:296px;background:var(--panel);border-left:1px solid var(--line);
  display:flex;flex-direction:column;z-index:10;
}
.prop-tabs{display:flex;padding:9px 9px 0;gap:5px;border-bottom:1px solid var(--line)}
.prop-tab{
  flex:1;background:none;border:0;padding:8px 6px 10px;cursor:pointer;color:var(--muted);
  font-size:11.5px;font-weight:600;border-bottom:2px solid transparent;transition:.15s;border-radius:7px 7px 0 0;
}
.prop-tab:hover{color:var(--text);background:#141f36}
.prop-tab.active{color:#c7d2fe;border-bottom-color:var(--accent)}
.prop-head{padding:13px 15px 9px;display:flex;align-items:center;gap:9px}
.prop-head .pi{width:28px;height:28px;border-radius:8px;background:#1a2749;display:grid;place-items:center;color:#a5b4fc}
.prop-head .pi svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.prop-head h3{margin:0;font-size:13px;font-weight:700;letter-spacing:.2px}
.prop-head p{margin:1px 0 0;font-size:10.5px;color:var(--muted)}
.props-body{flex:1;overflow-y:auto;padding:0 12px 24px}
.sec{border-top:1px solid var(--line);margin-top:8px;padding-top:8px}
.sec:first-child{border-top:0;margin-top:0;padding-top:4px}
.sec-title{
  font-size:10px;text-transform:uppercase;letter-spacing:1.3px;color:#5f7192;font-weight:700;
  padding:9px 3px 8px;cursor:pointer;display:flex;align-items:center;justify-content:space-between;user-select:none;
}
.sec-title:hover{color:#8ea2c4}
.sec-title .chev{transition:.18s;font-size:9px;color:#42557a}
.sec.collapsed .chev{transform:rotate(-90deg)}
.sec.collapsed .sec-items{display:none}
.ctl{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:5px 3px;min-height:30px}
.ctl > label{font-size:11.5px;color:#a9b8d0;flex:1;line-height:1.3}
.ctl-in{display:flex;align-items:center;gap:8px;flex-shrink:0}
.ctl input[type=range]{width:104px;-webkit-appearance:none;height:3px;border-radius:3px;background:#25355a;outline:none}
.ctl input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;width:13px;height:13px;border-radius:50%;background:#c7d2fe;cursor:pointer;
  border:2px solid #4f46e5;box-shadow:0 0 0 3px #6366f122;transition:.12s;
}
.ctl input[type=range]::-webkit-slider-thumb:hover{background:#fff;transform:scale(1.12)}
.ctl input[type=color]{
  -webkit-appearance:none;width:34px;height:24px;border:1px solid var(--line2);border-radius:6px;
  background:none;cursor:pointer;padding:2px;
}
.ctl input[type=color]::-webkit-color-swatch-wrapper{padding:0}
.ctl input[type=color]::-webkit-color-swatch{border:none;border-radius:4px}
.ctl input[type=number],.ctl input[type=text],.ctl select{
  background:#101b30;border:1px solid var(--line2);border-radius:7px;color:var(--text);
  padding:5px 8px;font-size:11.5px;outline:none;transition:.15s;width:104px;
}
.ctl input[type=number]{width:66px;font-variant-numeric:tabular-nums}
.ctl select{cursor:pointer;width:118px}
.ctl input:focus,.ctl select:focus{border-color:var(--accent);box-shadow:0 0 0 3px #6366f122}
.val{font-size:10.5px;color:#6f83a6;min-width:32px;text-align:right;font-variant-numeric:tabular-nums}
.switch{position:relative;display:inline-block;width:34px;height:19px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.switch span{position:absolute;inset:0;background:#22314f;border-radius:20px;cursor:pointer;transition:.2s}
.switch span::before{content:"";position:absolute;width:13px;height:13px;left:3px;top:3px;background:#7c8db0;border-radius:50%;transition:.2s}
.switch input:checked + span{background:#4f46e5}
.switch input:checked + span::before{transform:translateX(15px);background:#fff}
.empty-note{padding:24px 14px;color:var(--muted);font-size:12px;line-height:1.6;text-align:center}

.overlay{
  position:fixed;inset:0;background:radial-gradient(1000px 600px at 50% 0%,#16224a 0%,#070c16 60%);
  display:grid;place-items:center;z-index:100;
}
.overlay.hidden{display:none}
.card{
  width:400px;background:#0e1729;border:1px solid #23324f;border-radius:18px;padding:34px 32px 30px;
  box-shadow:0 40px 90px -30px #000, 0 0 0 1px #ffffff08 inset;
}
.card .logo{width:46px;height:46px;font-size:16px;border-radius:13px;margin-bottom:18px}
.card h1{margin:0 0 6px;font-size:22px;letter-spacing:-.4px}
.card .sub{margin:0 0 26px;color:var(--muted);font-size:12.5px;line-height:1.5}
.field{margin-bottom:16px}
.field label{display:block;font-size:10.5px;text-transform:uppercase;letter-spacing:1.2px;color:#6f83a6;font-weight:700;margin-bottom:7px}
.field input{
  width:100%;background:#0a1220;border:1px solid #23324f;border-radius:10px;padding:12px 14px;
  color:var(--text);font-size:14px;outline:none;transition:.15s;
}
.field input:focus{border-color:var(--accent);box-shadow:0 0 0 4px #6366f11f}
.card .btn{width:100%;justify-content:center;padding:12px;font-size:14px;margin-top:8px;border-radius:10px}
.hint{margin-top:16px;font-size:11px;color:#5f7192;text-align:center;line-height:1.5}

#textEditor{
  position:absolute;display:none;z-index:30;background:transparent;border:1px dashed #6366f1;
  outline:none;resize:none;overflow:hidden;padding:0;margin:0;line-height:1.35;
  white-space:pre-wrap;word-break:break-word;
}

.ctxmenu{
  position:fixed;background:#0e1729;border:1px solid var(--line2);border-radius:9px;
  padding:5px;min-width:220px;box-shadow:0 20px 50px -12px #000;display:none;z-index:300;
}
.ctxmenu.show{display:block}
.ctxmenu button{
  display:flex;width:100%;align-items:center;gap:10px;background:none;border:0;
  padding:8px 10px;border-radius:6px;cursor:pointer;font-size:12.5px;text-align:left;
  color:var(--text);transition:.1s;
}
.ctxmenu button:hover:not(:disabled){background:#1a2749}
.ctxmenu button:disabled{color:#4a5a78;cursor:default}
.ctxmenu button .kbd{margin-left:auto;color:#6f83a6;font-size:10.5px;font-family:inherit}
.ctxmenu .sep{height:1px;background:var(--line);margin:4px 2px}

.toast{
  position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(80px);
  background:#132340;border:1px solid #2b3f68;border-radius:10px;padding:10px 18px;font-size:12.5px;
  z-index:200;opacity:0;transition:.28s cubic-bezier(.2,.9,.3,1);box-shadow:0 18px 40px -14px #000;
  pointer-events:none;
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

.modal{
  position:fixed;inset:0;background:rgba(3,8,18,.7);backdrop-filter:blur(6px);
  display:none;place-items:center;z-index:250;
}
.modal.show{display:grid}
.modal-card{
  width:520px;max-height:80vh;overflow-y:auto;background:#0e1729;border:1px solid #23324f;
  border-radius:14px;padding:24px;box-shadow:0 40px 90px -30px #000;
}
.modal-card h2{margin:0 0 16px;font-size:17px}
.kbd-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #16223c;font-size:12.5px}
.kbd-row:last-child{border-bottom:0}
.kbd-row .keys{display:flex;gap:5px;align-items:center}
kbd{
  background:#1b2b47;border:1px solid #2b3f68;border-radius:5px;padding:2px 7px;
  font-size:11px;font-family:inherit;font-weight:600;color:#c7d2fe;
}
.section-h{font-size:10px;text-transform:uppercase;letter-spacing:1.3px;color:#5f7192;font-weight:700;margin:16px 0 8px}
.section-h:first-child{margin-top:0}
</style>
</head>
<body>

<header class="topbar">
  <div class="brand">
    <div class="logo">CB</div>
    <div class="brand-txt"><b>CollabBoard</b><span>workspace</span></div>
  </div>
  <div class="divider"></div>
  <div class="board-pill"><span class="dot"></span><span id="boardLabel">main</span></div>
  <span class="role-badge owner" id="roleBadge" style="display:none">Владелец</span>
  <div class="spacer"></div>
  <div class="users" id="users"></div>
  <div class="users-count" id="usersCount"></div>
  <div class="zoom">
    <button id="zoomOut" title="Уменьшить (Ctrl+-)">−</button>
    <button class="zval" id="zoomVal" title="Сбросить (Ctrl+0)">100%</button>
    <button id="zoomIn" title="Увеличить (Ctrl+=)">+</button>
  </div>
  <button class="btn ghost icon" id="btnHelp" title="Горячие клавиши (?)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.7"/><circle cx="12" cy="17" r=".5" fill="currentColor"/></svg>
  </button>
  <button class="btn ghost" id="btnUndo" title="Отменить (Ctrl+Z)">Отменить</button>
  <button class="btn ghost" id="btnRedo" title="Повторить (Ctrl+Y)">Повторить</button>
  <button class="btn" id="btnExport" title="Экспорт (Ctrl+S)">Экспорт</button>
  <button class="btn danger" id="btnClear" title="Очистить доску">Очистить</button>
</header>

<main class="workspace">
  <aside class="rail" id="rail"></aside>
  <section class="stage" id="stage">
    <canvas id="canvas"></canvas>
    <textarea id="textEditor" spellcheck="false"></textarea>
    <div class="hud">
      <div class="conn" id="conn"><span class="led"></span><span id="connTxt">Подключение…</span></div>
      <div class="divider sm"></div>
      <span>Объектов: <b id="statCount">0</b></span>
      <div class="divider sm"></div>
      <span>Выбрано: <b id="statSel">0</b></span>
      <div class="divider sm"></div>
      <span id="statTool">Перо</span>
    </div>
  </section>
  <aside class="props">
    <div class="prop-tabs">
      <button class="prop-tab active" data-tab="tool">Инструмент</button>
      <button class="prop-tab" data-tab="canvas">Холст</button>
      <button class="prop-tab" data-tab="export">Экспорт</button>
    </div>
    <div class="prop-head">
      <div class="pi" id="propIcon"></div>
      <div><h3 id="propTitle">Перо</h3><p id="propSub">Настройки инструмента</p></div>
    </div>
    <div class="props-body" id="props"></div>
  </aside>
</main>

<div class="overlay" id="overlay">
  <div class="card">
    <div class="logo">CB</div>
    <h1>CollabBoard</h1>
    <p class="sub">Совместная доска в реальном времени.<br/>Введите ник и присоединяйтесь к комнате.</p>
    <div class="field">
      <label>Ваш ник</label>
      <input id="nickInput" maxlength="24" placeholder="Например, Алекс" autocomplete="off"/>
    </div>
    <div class="field">
      <label>Комната</label>
      <input id="roomInput" maxlength="32" value="main" autocomplete="off"/>
    </div>
    <button class="btn primary" id="joinBtn">Войти на доску</button>
    <div class="hint">
      Первый, кто создаёт комнату, становится её <b>владельцем</b> и может редактировать доску.<br/>
      Остальные подключаются как наблюдатели.
    </div>
  </div>
</div>

<div class="modal" id="helpModal">
  <div class="modal-card">
    <h2>Горячие клавиши</h2>
    <div class="section-h">Инструменты</div>
    <div id="helpTools"></div>
    <div class="section-h">Редактирование</div>
    <div class="kbd-row"><span>Выделить всё</span><span class="keys"><kbd>Ctrl</kbd><kbd>A</kbd></span></div>
    <div class="kbd-row"><span>Копировать</span><span class="keys"><kbd>Ctrl</kbd><kbd>C</kbd></span></div>
    <div class="kbd-row"><span>Вставить</span><span class="keys"><kbd>Ctrl</kbd><kbd>V</kbd></span></div>
    <div class="kbd-row"><span>Дублировать</span><span class="keys"><kbd>Ctrl</kbd><kbd>D</kbd></span></div>
    <div class="kbd-row"><span>Удалить</span><span class="keys"><kbd>Del</kbd></span></div>
    <div class="kbd-row"><span>Отменить / Повторить</span><span class="keys"><kbd>Ctrl</kbd><kbd>Z</kbd> / <kbd>Ctrl</kbd><kbd>Y</kbd></span></div>
    <div class="kbd-row"><span>Снять выделение</span><span class="keys"><kbd>Esc</kbd></span></div>
    <div class="kbd-row"><span>Сдвинуть на 1 / 10 px</span><span class="keys"><kbd>←↑→↓</kbd> / <kbd>Shift</kbd>+<kbd>←↑→↓</kbd></span></div>
    <div class="section-h">Навигация</div>
    <div class="kbd-row"><span>Панорама</span><span class="keys"><kbd>Space</kbd>+drag / СКМ / <kbd>H</kbd></span></div>
    <div class="kbd-row"><span>Зум</span><span class="keys"><kbd>Ctrl</kbd>+колесо / <kbd>Ctrl</kbd><kbd>+/−</kbd></span></div>
    <div class="kbd-row"><span>Показать всё</span><span class="keys"><kbd>Ctrl</kbd><kbd>1</kbd></span></div>
    <div class="kbd-row"><span>Масштаб 100%</span><span class="keys"><kbd>Ctrl</kbd><kbd>0</kbd></span></div>
    <div class="kbd-row"><span>Экспорт</span><span class="keys"><kbd>Ctrl</kbd><kbd>S</kbd></span></div>
    <div class="section-h">Мышь</div>
    <div class="kbd-row"><span>Мульти-выбор</span><span class="keys"><kbd>Shift</kbd>+клик</span></div>
    <div class="kbd-row"><span>Дублировать при перетаскивании</span><span class="keys"><kbd>Alt</kbd>+drag</span></div>
    <div class="kbd-row"><span>Редактировать текст/стикер</span><span class="keys">двойной клик</span></div>
    <div class="kbd-row"><span>Контекстное меню</span><span class="keys">правый клик</span></div>
  </div>
</div>

<div class="ctxmenu" id="ctxmenu"></div>
<div class="toast" id="toast"></div>

<script>
"use strict";

/* ============================================================
   ICONS
   ============================================================ */
const ICONS = {
  select:'<path d="M4 2.5l7.5 18 2.4-7.1 7.1-2.4z"/>',
  hand:'<path d="M18 11V6.5a1.5 1.5 0 0 0-3 0V11m0-1V4.5a1.5 1.5 0 0 0-3 0V10m0-.5v-4a1.5 1.5 0 0 0-3 0V13"/><path d="M18 11a1.5 1.5 0 0 1 3 0v3a8 8 0 0 1-8 8h-1a8 8 0 0 1-7-4l-2.5-4a1.5 1.5 0 0 1 2.4-1.8L6 14"/>',
  pen:'<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L8 18l-4.5 1.5L5 15z"/><path d="M14 6l3 3"/>',
  highlighter:'<path d="M9 11l-3 3v4h4l3-3"/><path d="M18.5 5.5a2.12 2.12 0 0 0-3-3L9 9l3 3z"/><path d="M3 21h8"/>',
  eraser:'<path d="M20 20H8.5L3.7 15.2a2 2 0 0 1 0-2.8l8.7-8.7a2 2 0 0 1 2.8 0l5.1 5.1a2 2 0 0 1 0 2.8L11 20"/>',
  line:'<path d="M4 20L20 4"/>',
  arrow:'<path d="M6 18L18 6"/><path d="M9 6h9v9"/>',
  rect:'<rect x="3.5" y="3.5" width="17" height="17" rx="2.5"/>',
  ellipse:'<ellipse cx="12" cy="12" rx="8.5" ry="8.5"/>',
  triangle:'<path d="M12 3.5L21 20H3z"/>',
  polygon:'<path d="M12 2.5l8.2 4.75v9.5L12 21.5 3.8 16.75v-9.5z"/>',
  star:'<path d="M12 2.5l2.9 5.9 6.5.95-4.7 4.6 1.1 6.5L12 17.4l-5.8 3.05 1.1-6.5-4.7-4.6 6.5-.95z"/>',
  type:'<path d="M4 7V4.5h16V7"/><path d="M9.5 19.5h5"/><path d="M12 4.5v15"/>',
  sticky:'<path d="M4.5 4.5h15v9.5l-5 5h-10z"/><path d="M19.5 14h-5v5"/>',
  zap:'<path d="M13 2.5L3.5 14H12l-1 7.5L20.5 10H12z"/>',
  canvas:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>',
  download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>'
};

/* ============================================================
   TOOLS
   ============================================================ */
const TOOLS = [
  {id:'select',      name:'Выделение',    icon:'select',      key:'v', sub:'Перемещение и удаление объектов'},
  {id:'hand',        name:'Рука',         icon:'hand',        key:'h', sub:'Панорамирование холста'},
  {id:'pen',         name:'Перо',         icon:'pen',         key:'p', sub:'Свободное рисование'},
  {id:'highlighter', name:'Маркер',       icon:'highlighter', key:'m', sub:'Полупрозрачная подсветка'},
  {id:'eraser',      name:'Ластик',       icon:'eraser',      key:'e', sub:'Удаление объектов'},
  {id:'line',        name:'Линия',        icon:'line',        key:'l', sub:'Прямой отрезок'},
  {id:'arrow',       name:'Стрелка',      icon:'arrow',       key:'a', sub:'Вектор со стрелкой'},
  {id:'rect',        name:'Прямоугольник',icon:'rect',        key:'r', sub:'Прямоугольник со скруглением'},
  {id:'ellipse',     name:'Эллипс',       icon:'ellipse',     key:'o', sub:'Овал / круг'},
  {id:'triangle',    name:'Треугольник',  icon:'triangle',    key:'t', sub:'Равносторонний треугольник'},
  {id:'polygon',     name:'Многоугольник',icon:'polygon',     key:'g', sub:'N-угольник с числом сторон'},
  {id:'star',        name:'Звезда',       icon:'star',        key:'s', sub:'Многолучевая звезда'},
  {id:'text',        name:'Текст',        icon:'type',        key:'x', sub:'Текстовая надпись'},
  {id:'sticky',      name:'Стикер',       icon:'sticky',      key:'n', sub:'Заметка на доске'},
  {id:'laser',       name:'Лазер',        icon:'zap',         key:'k', sub:'Временная указка (для всех)'}
];
const TOOL_BY_ID = Object.fromEntries(TOOLS.map(t=>[t.id,t]));

const VIEWER_TOOLS = new Set(['hand','laser']);

/* ============================================================
   SETTINGS (100+)
   ============================================================ */
const S = {
  /* canvas 21 */
  bgColor:'#ffffff', bgPattern:'grid', patternSize:24, patternColor:'#cbd5e1',
  patternOpacity:0.7, patternWidth:1, patternAngle:0,
  snapEnabled:false, snapGrid:20, snapObjects:true, snapThreshold:8,
  showRulers:false, rulerUnit:'px', showOrigin:false,
  pageMode:'infinite', pageWidth:1920, pageHeight:1080,
  zoomMin:0.1, zoomMax:8, canvasShadow:true, canvasShadowBlur:30,
  /* stroke 14 */
  stroke:'#0f172a', strokeWidth:3, strokeOpacity:1,
  lineCap:'round', lineJoin:'round', miterLimit:10,
  dashPreset:'solid', dashLength:10, dashGap:8, dashOffset:0,
  strokeGradient:false, strokeGradType:'linear', strokeGradColor2:'#6366f1', strokeGradAngle:0,
  /* fill 6 */
  fillEnabled:false, fill:'#a5b4fc', fillOpacity:0.45,
  fillGradient:false, fillGradColor2:'#f472b6', fillGradAngle:0,
  /* pen 12 */
  penSmoothing:0.6, penStabilizer:0.3, penPressure:true, penMinWidth:0.25, penMaxWidth:1.6,
  penVelocity:0.4, penTaperStart:0.0, penTaperEnd:0.2, penSpacing:2,
  penTexture:false, penTextureDensity:0.3, penTextureOpacity:0.35,
  /* highlighter 5 */
  hlOpacity:0.35, hlBlend:'multiply', hlWidthMul:5, hlRoundTip:true, hlCap:'butt',
  /* eraser 5 */
  eraserSize:24, eraserMode:'object', eraserHardness:0.8, eraserFalloff:0.4, eraserCurrentLayerOnly:false,
  /* shapes 13 */
  cornerRadius:12, polySides:6, shapeRotation:0, lockAspect:false,
  starPoints:5, starInner:0.45, closePath:true,
  shadowEnabled:false, shadowBlur:18, shadowX:0, shadowY:6, shadowColor:'#0f172a', shadowOpacity:0.25,
  /* arrow 6 */
  arrowHeadSize:14, arrowHeadStyle:'triangle', arrowTailStyle:'none', arrowCurve:0,
  arrowDouble:false, arrowHeadAngle:28,
  /* text 11 */
  fontFamily:'Inter, system-ui, sans-serif', fontSize:22, fontWeight:500,
  fontItalic:false, fontUnderline:false, textAlign:'left', lineHeight:1.35, letterSpacing:0,
  textColor:'#0f172a', textBgEnabled:false, textBgColor:'#fef9c3',
  /* sticky 7 */
  stickyColor:'#fde68a', stickyFontSize:18, stickyFontFamily:'Inter, system-ui, sans-serif',
  stickyTextColor:'#1f2937', stickyShadow:true, stickyRadius:10, stickyPadding:14,
  /* laser 4 */
  laserColor:'#ef4444', laserWidth:4, laserFade:700, laserGlow:true,
  /* selection 5 */
  showBBox:true, bboxColor:'#6366f1', handleSize:8, handleColor:'#ffffff', snapRotation:false,
  /* export 5 */
  exportFormat:'png', exportScale:2, exportTransparent:false, exportQuality:0.92, exportIncludeGrid:false,
  /* misc 4 */
  elementOpacity:1, blendMode:'source-over', locked:false, visible:true
};

/* ============================================================
   SECTIONS
   ============================================================ */
const C = (k,l,t,o={}) => Object.assign({k,l,t}, o);

const SECTIONS = [
  { id:'canvas1', tab:'canvas', title:'Оформление холста', items:[
    C('bgColor','Цвет фона','color'),
    C('bgPattern','Узор','select',{options:[['none','Нет'],['grid','Сетка'],['dots','Точки'],['lines','Линии']]}),
    C('patternSize','Размер ячейки','range',{min:4,max:200,step:1,suf:'px'}),
    C('patternColor','Цвет узора','color'),
    C('patternOpacity','Прозрачность узора','range',{min:0,max:1,step:0.01,pct:true}),
    C('patternWidth','Толщина линий','range',{min:0.5,max:6,step:0.5,suf:'px'}),
    C('patternAngle','Наклон узора','range',{min:0,max:180,step:1,suf:'°'}),
  ]},
  { id:'canvas2', tab:'canvas', title:'Сетка и привязка', items:[
    C('snapEnabled','Привязка к сетке','check'),
    C('snapGrid','Шаг привязки','range',{min:2,max:100,step:1,suf:'px'}),
    C('snapObjects','Привязка к объектам','check'),
    C('snapThreshold','Порог привязки','range',{min:1,max:40,step:1,suf:'px'}),
    C('showOrigin','Отметка начала координат','check'),
  ]},
  { id:'canvas3', tab:'canvas', title:'Страница и вид', items:[
    C('pageMode','Режим','select',{options:[['infinite','Бесконечный'],['page','Страница']]}),
    C('pageWidth','Ширина страницы','range',{min:320,max:8000,step:10,suf:'px'}),
    C('pageHeight','Высота страницы','range',{min:240,max:8000,step:10,suf:'px'}),
    C('canvasShadow','Тень страницы','check'),
    C('canvasShadowBlur','Размытие тени','range',{min:0,max:80,step:1,suf:'px'}),
    C('showRulers','Линейки','check'),
    C('rulerUnit','Единицы','select',{options:[['px','px'],['pt','pt'],['cm','cm'],['in','in']]}),
    C('zoomMin','Мин. масштаб','range',{min:0.05,max:1,step:0.05}),
    C('zoomMax','Макс. масштаб','range',{min:1,max:16,step:0.5}),
  ]},

  { id:'stroke', tools:['pen','highlighter','line','arrow','rect','ellipse','triangle','polygon','star','text'], title:'Обводка', items:[
    C('stroke','Цвет','color'),
    C('strokeWidth','Толщина','range',{min:0.5,max:60,step:0.5,suf:'px'}),
    C('strokeOpacity','Непрозрачность','range',{min:0,max:1,step:0.01,pct:true}),
    C('lineCap','Окончание','select',{options:[['butt','Плоское'],['round','Круглое'],['square','Квадратное']]}),
    C('lineJoin','Соединение','select',{options:[['miter','Острое'],['round','Круглое'],['bevel','Срезанное']]}),
    C('miterLimit','Предел miter','range',{min:1,max:30,step:1}),
    C('dashPreset','Штрих','select',{options:[['solid','Сплошной'],['dashed','Штрих'],['dotted','Точки'],['dashdot','Штрих-точка']]}),
    C('dashLength','Длина штриха','range',{min:1,max:60,step:1,suf:'px'}),
    C('dashGap','Промежуток','range',{min:1,max:60,step:1,suf:'px'}),
    C('dashOffset','Смещение штриха','range',{min:0,max:60,step:1,suf:'px'}),
  ]},
  { id:'stroke2', tools:['pen','line','arrow','rect','ellipse','triangle','polygon','star'], title:'Градиент обводки', items:[
    C('strokeGradient','Включить градиент','check'),
    C('strokeGradType','Тип','select',{options:[['linear','Линейный'],['radial','Радиальный']]}),
    C('strokeGradColor2','Второй цвет','color'),
    C('strokeGradAngle','Угол','range',{min:0,max:360,step:1,suf:'°'}),
  ]},

  { id:'fill', tools:['rect','ellipse','triangle','polygon','star'], title:'Заливка', items:[
    C('fillEnabled','Заливать фигуру','check'),
    C('fill','Цвет заливки','color'),
    C('fillOpacity','Непрозрачность','range',{min:0,max:1,step:0.01,pct:true}),
    C('fillGradient','Градиент','check'),
    C('fillGradColor2','Второй цвет','color'),
    C('fillGradAngle','Угол градиента','range',{min:0,max:360,step:1,suf:'°'}),
  ]},

  { id:'pen', tools:['pen'], title:'Параметры пера', items:[
    C('penSmoothing','Сглаживание','range',{min:0,max:1,step:0.01,pct:true}),
    C('penStabilizer','Стабилизатор','range',{min:0,max:1,step:0.01,pct:true}),
    C('penPressure','Учёт скорости','check'),
    C('penVelocity','Влияние скорости','range',{min:0,max:1,step:0.01,pct:true}),
    C('penMinWidth','Мин. толщина','range',{min:0.05,max:1,step:0.05}),
    C('penMaxWidth','Макс. толщина','range',{min:0.5,max:3,step:0.05}),
    C('penTaperStart','Сужение в начале','range',{min:0,max:1,step:0.05,pct:true}),
    C('penTaperEnd','Сужение в конце','range',{min:0,max:1,step:0.05,pct:true}),
    C('penSpacing','Шаг точек','range',{min:0.5,max:12,step:0.5,suf:'px'}),
    C('penTexture','Текстура штриха','check'),
    C('penTextureDensity','Плотность','range',{min:0.05,max:1,step:0.05,pct:true}),
    C('penTextureOpacity','Сила текстуры','range',{min:0,max:1,step:0.05,pct:true}),
  ]},

  { id:'hl', tools:['highlighter'], title:'Параметры маркера', items:[
    C('hlOpacity','Непрозрачность','range',{min:0.05,max:1,step:0.01,pct:true}),
    C('hlBlend','Режим наложения','select',{options:[['multiply','Умножение'],['source-over','Обычный'],['screen','Экран'],['overlay','Перекрытие'],['darken','Затемнение']]}),
    C('hlWidthMul','Множитель толщины','range',{min:1,max:14,step:0.5,suf:'×'}),
    C('hlRoundTip','Круглый наконечник','check'),
    C('hlCap','Окончание','select',{options:[['butt','Плоское'],['round','Круглое'],['square','Квадратное']]}),
  ]},

  { id:'eraser', tools:['eraser'], title:'Параметры ластика', items:[
    C('eraserSize','Размер','range',{min:4,max:200,step:2,suf:'px'}),
    C('eraserMode','Режим','select',{options:[['object','Удалять объект'],['partial','Стирать часть']]}),
    C('eraserHardness','Жёсткость','range',{min:0,max:1,step:0.05,pct:true}),
    C('eraserFalloff','Затухание','range',{min:0,max:1,step:0.05,pct:true}),
    C('eraserCurrentLayerOnly','Только текущий слой','check'),
  ]},

  { id:'shape', tools:['rect','ellipse','triangle','polygon','star'], title:'Геометрия фигуры', items:[
    C('cornerRadius','Радиус скругления','range',{min:0,max:120,step:1,suf:'px'}),
    C('polySides','Число сторон','range',{min:3,max:24,step:1}),
    C('starPoints','Лучей у звезды','range',{min:3,max:20,step:1}),
    C('starInner','Внутренний радиус','range',{min:0.05,max:0.95,step:0.01,pct:true}),
    C('shapeRotation','Поворот','range',{min:-180,max:180,step:1,suf:'°'}),
    C('lockAspect','Сохранять пропорции','check'),
    C('closePath','Замкнутый контур','check'),
  ]},
  { id:'shapeShadow', tools:['rect','ellipse','triangle','polygon','star','sticky','text'], title:'Тень', items:[
    C('shadowEnabled','Включить тень','check'),
    C('shadowBlur','Размытие','range',{min:0,max:80,step:1,suf:'px'}),
    C('shadowX','Смещение X','range',{min:-60,max:60,step:1,suf:'px'}),
    C('shadowY','Смещение Y','range',{min:-60,max:60,step:1,suf:'px'}),
    C('shadowColor','Цвет тени','color'),
    C('shadowOpacity','Плотность тени','range',{min:0,max:1,step:0.01,pct:true}),
  ]},

  { id:'arrow', tools:['line','arrow'], title:'Линия и стрелка', items:[
    C('arrowHeadSize','Размер наконечника','range',{min:4,max:60,step:1,suf:'px'}),
    C('arrowHeadAngle','Угол наконечника','range',{min:10,max:70,step:1,suf:'°'}),
    C('arrowHeadStyle','Стиль наконечника','select',{options:[['triangle','Треугольник'],['open','Открытый'],['diamond','Ромб'],['circle','Круг'],['bar','Черта']]}),
    C('arrowTailStyle','Начало линии','select',{options:[['none','Нет'],['triangle','Треугольник'],['open','Открытый'],['diamond','Ромб'],['circle','Круг'],['bar','Черта']]}),
    C('arrowCurve','Изгиб','range',{min:-200,max:200,step:2}),
    C('arrowDouble','Двусторонняя','check'),
  ]},

  { id:'text', tools:['text'], title:'Типографика', items:[
    C('fontFamily','Шрифт','select',{options:[
      ['Inter, system-ui, sans-serif','Inter'],
      ['Georgia, serif','Georgia'],
      ['"Times New Roman", serif','Times New Roman'],
      ['Arial, Helvetica, sans-serif','Arial'],
      ['"Courier New", monospace','Courier New'],
      ['Verdana, sans-serif','Verdana'],
      ['"Comic Sans MS", cursive','Comic Sans']
    ]}),
    C('fontSize','Размер','range',{min:8,max:200,step:1,suf:'px'}),
    C('fontWeight','Насыщенность','select',{options:[[300,'Light'],[400,'Regular'],[500,'Medium'],[600,'Semi Bold'],[700,'Bold'],[800,'Extra Bold']]}),
    C('fontItalic','Курсив','check'),
    C('fontUnderline','Подчёркивание','check'),
    C('textAlign','Выравнивание','select',{options:[['left','По левому'],['center','По центру'],['right','По правому']]}),
    C('lineHeight','Межстрочный интервал','range',{min:0.8,max:3,step:0.05}),
    C('letterSpacing','Межбуквенный интервал','range',{min:-5,max:20,step:0.5,suf:'px'}),
    C('textColor','Цвет текста','color'),
    C('textBgEnabled','Подложка','check'),
    C('textBgColor','Цвет подложки','color'),
  ]},

  { id:'sticky', tools:['sticky'], title:'Стикер', items:[
    C('stickyColor','Цвет стикера','color'),
    C('stickyFontSize','Размер шрифта','range',{min:8,max:60,step:1,suf:'px'}),
    C('stickyFontFamily','Шрифт','select',{options:[
      ['Inter, system-ui, sans-serif','Inter'],
      ['Georgia, serif','Georgia'],
      ['"Comic Sans MS", cursive','Comic Sans'],
      ['"Courier New", monospace','Courier New']
    ]}),
    C('stickyTextColor','Цвет текста','color'),
    C('stickyShadow','Тень стикера','check'),
    C('stickyRadius','Скругление','range',{min:0,max:40,step:1,suf:'px'}),
    C('stickyPadding','Внутренний отступ','range',{min:4,max:50,step:1,suf:'px'}),
  ]},

  { id:'laser', tools:['laser'], title:'Указка', items:[
    C('laserColor','Цвет','color'),
    C('laserWidth','Толщина','range',{min:1,max:20,step:1,suf:'px'}),
    C('laserFade','Время затухания','range',{min:150,max:4000,step:50,suf:'мс'}),
    C('laserGlow','Свечение','check'),
  ]},

  { id:'selection', tools:['select'], title:'Выделение', items:[
    C('showBBox','Показывать рамку','check'),
    C('bboxColor','Цвет рамки','color'),
    C('handleSize','Размер маркеров','range',{min:4,max:20,step:1,suf:'px'}),
    C('handleColor','Цвет маркеров','color'),
    C('snapRotation','Привязка поворота','check'),
  ]},

  { id:'export', tab:'export', title:'Экспорт изображения', items:[
    C('exportFormat','Формат','select',{options:[['png','PNG'],['jpeg','JPEG'],['webp','WebP']]}),
    C('exportScale','Масштаб','range',{min:0.5,max:6,step:0.5,suf:'×'}),
    C('exportTransparent','Прозрачный фон','check'),
    C('exportQuality','Качество','range',{min:0.1,max:1,step:0.01,pct:true}),
    C('exportIncludeGrid','Включить сетку','check'),
  ]},
];

/* ============================================================
   STATE
   ============================================================ */
const state = {
  tool:'pen',
  view:{x:0, y:0, zoom:1},
  elements:[],
  selection:new Set(),
  draft:null,
  lasers:[],
  ws:null,
  nick:'',
  room:'main',
  isOwner:false,
  ownerToken:null,
  myId:null,
  myColor:'#818cf8',
  remoteUsers:new Map(),   // id -> {nick,color,cursor,cursorAnim}
  tab:'tool',
  undoStack:[],
  redoStack:[],
  clipboard:[],
  panning:false,
  spaceDown:false,
  drawing:false,
  erasing:false,
  laserActive:false,
  moveStart:null,
  moveSnapshot:null,
  altDuplicate:false,
  lastScreen:null,
  lastEditorClose:0,
  collapsed:{}
};

const $  = (s,r=document)=>r.querySelector(s);
const $$ = (s,r=document)=>[...r.querySelectorAll(s)];
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : 'id-'+Math.random().toString(36).slice(2)+Date.now());

const canvas = $('#canvas');
const ctx = canvas.getContext('2d');
const stage = $('#stage');
const editor = $('#textEditor');
let W=0,H=0,DPR=1;
let dirty = true;
const markDirty = ()=>{ dirty = true; };
const isOwner = ()=> state.isOwner;
const canEdit = ()=> state.isOwner;

/* ============================================================
   RAIL
   ============================================================ */
function buildRail(){
  const rail = $('#rail');
  rail.innerHTML = '';
  TOOLS.forEach((t,i)=>{
    if(i===2 || i===5 || i===13) rail.insertAdjacentHTML('beforeend','<div class="rail-sep"></div>');
    const b = document.createElement('button');
    b.className = 'tool' + (t.id===state.tool?' active':'');
    b.dataset.tool = t.id;
    b.innerHTML = `<svg viewBox="0 0 24 24">${ICONS[t.icon]}</svg>
      <span class="tt">${t.name}<kbd>${t.key.toUpperCase()}</kbd></span>`;
    b.onclick = ()=> setTool(t.id);
    rail.appendChild(b);
  });
  updateToolAvailability();
}

function updateToolAvailability(){
  $$('.tool').forEach(el=>{
    const id = el.dataset.tool;
    const allowed = canEdit() || VIEWER_TOOLS.has(id);
    el.disabled = !allowed;
  });
}

function setTool(id){
  if(!canEdit() && !VIEWER_TOOLS.has(id)){
    toast('Только владелец комнаты может редактировать');
    return;
  }
  if(state.tool===id) return;
  state.tool = id;
  $$('.tool').forEach(el=>el.classList.toggle('active', el.dataset.tool===id));
  const t = TOOL_BY_ID[id];
  $('#statTool').textContent = t.name;
  stage.classList.toggle('hand', id==='hand');
  stage.classList.toggle('select', id==='select');
  closeEditor(true);
  renderPanel();
  markDirty();
}

/* ============================================================
   PANEL
   ============================================================ */
function fmt(v,c){
  if(c.pct) return Math.round(v*100)+'%';
  if(typeof v === 'number'){
    const r = Math.round(v*100)/100;
    return r + (c.suf||'');
  }
  return v;
}

function controlHTML(c){
  const v = S[c.k];
  if(c.t==='color') return `<input type="color" data-k="${c.k}" value="${v}">`;
  if(c.t==='range') return `<input type="range" data-k="${c.k}" min="${c.min}" max="${c.max}" step="${c.step}" value="${v}"><span class="val" data-val="${c.k}">${fmt(v,c)}</span>`;
  if(c.t==='check') return `<label class="switch"><input type="checkbox" data-k="${c.k}" ${v?'checked':''}><span></span></label>`;
  if(c.t==='select') return `<select data-k="${c.k}">${c.options.map(o=>`<option value="${o[0]}" ${String(v)===String(o[0])?'selected':''}>${o[1]}</option>`).join('')}</select>`;
  if(c.t==='number') return `<input type="number" data-k="${c.k}" value="${v}" step="${c.step||1}">`;
  return `<input type="text" data-k="${c.k}" value="${v}">`;
}

function renderPanel(){
  const body = $('#props');
  const tool = TOOL_BY_ID[state.tool];
  const isToolTab = state.tab==='tool';

  $('#propIcon').innerHTML = `<svg viewBox="0 0 24 24">${ICONS[isToolTab ? tool.icon : (state.tab==='canvas'?'canvas':'download')]}</svg>`;
  $('#propTitle').textContent = isToolTab ? tool.name : (state.tab==='canvas' ? 'Холст' : 'Экспорт');
  $('#propSub').textContent = isToolTab ? tool.sub : (state.tab==='canvas' ? 'Параметры рабочей области' : 'Сохранение результата');

  const secs = SECTIONS.filter(s=>{
    if(s.tab) return s.tab === state.tab;
    if(state.tab!=='tool') return false;
    return s.tools && s.tools.includes(state.tool);
  });

  if(!secs.length){
    body.innerHTML = `<div class="empty-note">У этого инструмента нет дополнительных параметров.</div>`;
    return;
  }

  let html = '';
  for(const sec of secs){
    const collapsed = state.collapsed[sec.id] ? ' collapsed' : '';
    html += `<div class="sec${collapsed}" data-sec="${sec.id}">
      <div class="sec-title" data-toggle="${sec.id}"><span>${sec.title}</span><span class="chev">▼</span></div>
      <div class="sec-items">${sec.items.map(c=>`<div class="ctl"><label>${c.l}</label><div class="ctl-in">${controlHTML(c)}</div></div>`).join('')}</div>
    </div>`;
  }
  body.innerHTML = html;
}

$('#props').addEventListener('click', e=>{
  const t = e.target.closest('[data-toggle]');
  if(t){
    const id = t.dataset.toggle;
    state.collapsed[id] = !state.collapsed[id];
    t.parentElement.classList.toggle('collapsed', state.collapsed[id]);
  }
});

$('#props').addEventListener('input', e=>{
  const el = e.target;
  const k = el.dataset.k;
  if(!k) return;
  let v;
  if(el.type==='checkbox') v = el.checked;
  else if(el.type==='range' || el.type==='number') v = parseFloat(el.value);
  else v = el.value;
  if((el.type==='range' || el.type==='number') && isNaN(v)) return;
  S[k] = v;
  const valEl = $(`[data-val="${k}"]`, $('#props'));
  if(valEl){
    const def = SECTIONS.flatMap(s=>s.items).find(c=>c.k===k);
    valEl.textContent = fmt(v, def||{});
  }
  markDirty();
});

$$('.prop-tab').forEach(tab=>{
  tab.onclick = ()=>{
    $$('.prop-tab').forEach(x=>x.classList.toggle('active', x===tab));
    state.tab = tab.dataset.tab;
    renderPanel();
  };
});

/* ============================================================
   GEOMETRY
   ============================================================ */
const w2s = p => [p[0]*state.view.zoom + state.view.x, p[1]*state.view.zoom + state.view.y];
const s2w = (x,y) => [(x - state.view.x)/state.view.zoom, (y - state.view.y)/state.view.zoom];

function pointerWorld(e){
  const r = canvas.getBoundingClientRect();
  return s2w(e.clientX - r.left, e.clientY - r.top);
}

function snapPoint(p){
  if(!S.snapEnabled) return p;
  const g = S.snapGrid;
  return [Math.round(p[0]/g)*g, Math.round(p[1]/g)*g];
}

function elementBounds(el){
  switch(el.type){
    case 'path': {
      let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
      for(const p of el.points){
        if(p[0]<minX)minX=p[0]; if(p[0]>maxX)maxX=p[0];
        if(p[1]<minY)minY=p[1]; if(p[1]>maxY)maxY=p[1];
      }
      const pad = (el.width||3)/2;
      return {x:minX-pad,y:minY-pad,w:(maxX-minX)+pad*2,h:(maxY-minY)+pad*2};
    }
    case 'line': case 'arrow': {
      const x = Math.min(el.x1,el.x2), y = Math.min(el.y1,el.y2);
      return {x, y, w: Math.abs(el.x2-el.x1), h: Math.abs(el.y2-el.y1)};
    }
    case 'rect': case 'sticky': return {x:el.x, y:el.y, w:el.w, h:el.h};
    case 'ellipse': return {x:el.cx-el.rx, y:el.cy-el.ry, w:el.rx*2, h:el.ry*2};
    case 'poly': case 'star': return {x:el.cx-el.r, y:el.cy-el.r, w:el.r*2, h:el.r*2};
    case 'text': return {x:el.x, y:el.y, w:el.w||120, h:el.h||30};
    default: return {x:0,y:0,w:0,h:0};
  }
}

function unionBounds(els){
  if(!els.length) return null;
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  for(const el of els){
    const b = elementBounds(el);
    minX = Math.min(minX, b.x); minY = Math.min(minY, b.y);
    maxX = Math.max(maxX, b.x+b.w); maxY = Math.max(maxY, b.y+b.h);
  }
  return {x:minX, y:minY, w:maxX-minX, h:maxY-minY};
}

function moveElement(el, dx, dy){
  switch(el.type){
    case 'path': el.points = el.points.map(p=>[p[0]+dx, p[1]+dy]); break;
    case 'line': case 'arrow': el.x1+=dx; el.y1+=dy; el.x2+=dx; el.y2+=dy; break;
    case 'rect': case 'sticky': case 'text': el.x+=dx; el.y+=dy; break;
    case 'ellipse': case 'poly': case 'star': el.cx+=dx; el.cy+=dy; break;
  }
}

function segDist(p, a, b){
  const vx = b[0]-a[0], vy = b[1]-a[1];
  const wx = p[0]-a[0], wy = p[1]-a[1];
  const L = vx*vx+vy*vy;
  let t = L ? (wx*vx+wy*vy)/L : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(p[0]-(a[0]+t*vx), p[1]-(a[1]+t*vy));
}

function hitTest(el, p, tol){
  if(el.visible === false) return false;
  if(el.type==='path'){
    const pad = tol || 6/state.view.zoom;
    const pts = el.points;
    if(pts.length===1){
      return Math.hypot(p[0]-pts[0][0], p[1]-pts[0][1]) < el.width/2 + pad;
    }
    for(let i=0;i<pts.length-1;i++){
      if(segDist(p, pts[i], pts[i+1]) < (el.width/2 + pad)) return true;
    }
    return false;
  }
  const b = elementBounds(el);
  const pad = tol || 6/state.view.zoom;
  return p[0] >= b.x-pad && p[0] <= b.x+b.w+pad && p[1] >= b.y-pad && p[1] <= b.y+b.h+pad;
}

function topElementAt(p){
  for(let i=state.elements.length-1;i>=0;i--){
    if(hitTest(state.elements[i], p)) return state.elements[i];
  }
  return null;
}

/* ============================================================
   RENDER
   ============================================================ */
function resize(){
  const r = stage.getBoundingClientRect();
  DPR = window.devicePixelRatio || 1;
  W = r.width; H = r.height;
  canvas.width = Math.max(1, Math.floor(W*DPR));
  canvas.height = Math.max(1, Math.floor(H*DPR));
  canvas.style.width = W+'px';
  canvas.style.height = H+'px';
  markDirty();
}
window.addEventListener('resize', resize);

function applyDash(c, el){
  if(!el.dash || el.dash==='solid'){ c.setLineDash([]); c.lineDashOffset = 0; return; }
  let d;
  if(el.dash==='dashed') d = [el.dashLength, el.dashGap];
  else if(el.dash==='dotted') d = [Math.max(0.5, el.width*0.5), el.dashGap];
  else if(el.dash==='dashdot') d = [el.dashLength, el.dashGap, Math.max(0.5, el.width*0.5), el.dashGap];
  else d = [el.dashLength, el.dashGap];
  c.setLineDash(d);
  c.lineDashOffset = el.dashOffset || 0;
}

function hexA(hex, a){
  if(!hex) return `rgba(0,0,0,${a===undefined?1:a})`;
  if(hex.startsWith('rgb')) return hex;
  let h = hex.replace('#','');
  if(h.length===3) h = h.split('').map(x=>x+x).join('');
  const n = parseInt(h,16);
  return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a===undefined?1:a})`;
}

function grad2(c, col1, col2, angleDeg, bbox){
  const a = (angleDeg||0)*Math.PI/180;
  const cx = bbox.x+bbox.w/2, cy = bbox.y+bbox.h/2;
  const r = Math.max(bbox.w, bbox.h)/2 || 1;
  const dx = Math.cos(a)*r, dy = Math.sin(a)*r;
  const g = c.createLinearGradient(cx-dx, cy-dy, cx+dx, cy+dy);
  g.addColorStop(0, col1);
  g.addColorStop(1, col2);
  return g;
}

function drawPathPoints(c, pts, smooth){
  if(pts.length===0) return;
  c.beginPath();
  if(pts.length===1){
    c.moveTo(pts[0][0], pts[0][1]);
    c.lineTo(pts[0][0]+0.01, pts[0][1]);
    return;
  }
  c.moveTo(pts[0][0], pts[0][1]);
  if(smooth && pts.length>2){
    for(let i=1;i<pts.length-1;i++){
      const mx = (pts[i][0]+pts[i+1][0])/2;
      const my = (pts[i][1]+pts[i+1][1])/2;
      c.quadraticCurveTo(pts[i][0], pts[i][1], mx, my);
    }
    c.lineTo(pts[pts.length-1][0], pts[pts.length-1][1]);
  } else {
    for(let i=1;i<pts.length;i++) c.lineTo(pts[i][0], pts[i][1]);
  }
}

function drawVariableStroke(c, el){
  const pts = el.points;
  if(pts.length<2){
    c.beginPath();
    c.arc(pts[0][0], pts[0][1], el.width/2, 0, Math.PI*2);
    c.fillStyle = el.stroke;
    c.fill();
    return;
  }
  const n = pts.length;
  for(let i=0;i<n-1;i++){
    const t = i/(n-1);
    let k = 1;
    if(el.taperStart>0 && t < el.taperStart) k = Math.max(0.06, t/el.taperStart);
    if(el.taperEnd>0 && t > 1-el.taperEnd) k = Math.min(k, Math.max(0.06, (1-t)/el.taperEnd));
    const w = el.width * k;
    c.beginPath();
    c.lineWidth = w;
    c.lineCap = 'round';
    c.moveTo(pts[i][0], pts[i][1]);
    c.lineTo(pts[i+1][0], pts[i+1][1]);
    c.stroke();
  }
}

function pathRect(el){
  const p = new Path2D();
  const r = Math.min(el.radius||0, Math.abs(el.w)/2, Math.abs(el.h)/2);
  const x = el.x, y = el.y, w = el.w, h = el.h;
  if(r>0){
    p.moveTo(x+r, y);
    p.lineTo(x+w-r, y); p.quadraticCurveTo(x+w, y, x+w, y+r);
    p.lineTo(x+w, y+h-r); p.quadraticCurveTo(x+w, y+h, x+w-r, y+h);
    p.lineTo(x+r, y+h); p.quadraticCurveTo(x, y+h, x, y+h-r);
    p.lineTo(x, y+r); p.quadraticCurveTo(x, y, x+r, y);
    p.closePath();
  } else {
    p.rect(x,y,w,h);
  }
  return p;
}

function drawArrowHead(c, x, y, ang, el, style){
  const s = el.headSize;
  const a = (el.headAngle||28) * Math.PI/180;
  c.save();
  c.setLineDash([]);
  c.lineCap = 'round';
  c.lineJoin = 'round';
  if(style==='triangle'){
    c.beginPath();
    c.moveTo(x,y);
    c.lineTo(x - s*Math.cos(ang-a), y - s*Math.sin(ang-a));
    c.lineTo(x - s*Math.cos(ang+a), y - s*Math.sin(ang+a));
    c.closePath();
    c.fillStyle = c.strokeStyle;
    c.fill();
  } else if(style==='open'){
    c.beginPath();
    c.moveTo(x - s*Math.cos(ang-a), y - s*Math.sin(ang-a));
    c.lineTo(x,y);
    c.lineTo(x - s*Math.cos(ang+a), y - s*Math.sin(ang+a));
    c.stroke();
  } else if(style==='diamond'){
    const mx = x - s*0.6*Math.cos(ang), my = y - s*0.6*Math.sin(ang);
    c.beginPath();
    c.moveTo(x,y);
    c.lineTo(mx - s*0.4*Math.cos(ang-a), my - s*0.4*Math.sin(ang-a));
    c.lineTo(x - s*1.2*Math.cos(ang), y - s*1.2*Math.sin(ang));
    c.lineTo(mx - s*0.4*Math.cos(ang+a), my - s*0.4*Math.sin(ang+a));
    c.closePath();
    c.fillStyle = c.strokeStyle;
    c.fill();
  } else if(style==='circle'){
    c.beginPath();
    c.arc(x - s*0.5*Math.cos(ang), y - s*0.5*Math.sin(ang), s*0.42, 0, Math.PI*2);
    c.fillStyle = c.strokeStyle;
    c.fill();
  } else if(style==='bar'){
    c.beginPath();
    const px = -Math.sin(ang)*s*0.6, py = Math.cos(ang)*s*0.6;
    c.moveTo(x+px, y+py);
    c.lineTo(x-px, y-py);
    c.stroke();
  }
  c.restore();
}

function drawElement(c, el){
  if(el.visible === false) return;
  c.save();
  c.globalAlpha = (el.opacity ?? 1) * (el.elOpacity ?? 1);
  c.globalCompositeOperation = el.blend || 'source-over';

  if(el.shadow){
    c.shadowBlur = el.shadow.blur;
    c.shadowOffsetX = el.shadow.x;
    c.shadowOffsetY = el.shadow.y;
    c.shadowColor = hexA(el.shadow.color, el.shadow.opacity);
  }

  const bbox = elementBounds(el);
  c.strokeStyle = el.stroke;
  c.fillStyle = el.color || '#000';
  c.lineWidth = el.width;
  c.lineCap = el.cap;
  c.lineJoin = el.join;
  c.miterLimit = el.miter;
  applyDash(c, el);

  switch(el.type){
    case 'path': {
      if(el.tool==='highlighter'){
        c.globalCompositeOperation = el.hlBlend || 'multiply';
        c.globalAlpha = (el.opacity ?? 1) * (el.elOpacity ?? 1);
        c.lineCap = el.cap;
        drawPathPoints(c, el.points, true);
        c.stroke();
      } else if(el.variable && el.points.length>1){
        drawVariableStroke(c, el);
      } else {
        drawPathPoints(c, el.points, true);
        c.stroke();
      }
      break;
    }
    case 'line': case 'arrow': {
      const {x1,y1,x2,y2} = el;
      c.beginPath();
      c.moveTo(x1,y1);
      if(el.curve){
        const mx = (x1+x2)/2, my = (y1+y2)/2;
        const dx = x2-x1, dy = y2-y1;
        const len = Math.hypot(dx,dy) || 1;
        const nx = -dy/len, ny = dx/len;
        c.quadraticCurveTo(mx + nx*el.curve, my + ny*el.curve, x2, y2);
      } else {
        c.lineTo(x2,y2);
      }
      c.stroke();

      if(el.type==='arrow'){
        const ang = Math.atan2(y2-y1, x2-x1);
        if(el.headStyle!=='none') drawArrowHead(c, x2, y2, ang, el, el.headStyle);
        if(el.doubleHead || el.tailStyle!=='none'){
          const st = el.doubleHead ? (el.headStyle||'triangle') : el.tailStyle;
          drawArrowHead(c, x1, y1, ang+Math.PI, el, st);
        }
      }
      break;
    }
    case 'rect': {
      const p = pathRect(el);
      if(el.fillEnabled){
        c.fillStyle = el.fillGrad ? grad2(c, el.fill, el.fillGradColor2, el.fillGradAngle, bbox) : hexA(el.fill, el.fillOpacity);
        c.fill(p);
      }
      c.stroke(p);
      break;
    }
    case 'ellipse': {
      c.beginPath();
      c.ellipse(el.cx, el.cy, Math.abs(el.rx), Math.abs(el.ry), 0, 0, Math.PI*2);
      if(el.fillEnabled){
        c.fillStyle = el.fillGrad ? grad2(c, el.fill, el.fillGradColor2, el.fillGradAngle, bbox) : hexA(el.fill, el.fillOpacity);
        c.fill();
      }
      c.stroke();
      break;
    }
    case 'poly': {
      c.beginPath();
      const n = el.sides;
      for(let i=0;i<n;i++){
        const a = (i/n)*Math.PI*2 - Math.PI/2 + (el.rotation||0)*Math.PI/180;
        const x = el.cx + Math.cos(a)*el.r;
        const y = el.cy + Math.sin(a)*el.r;
        i? c.lineTo(x,y) : c.moveTo(x,y);
      }
      c.closePath();
      if(el.fillEnabled){
        c.fillStyle = el.fillGrad ? grad2(c, el.fill, el.fillGradColor2, el.fillGradAngle, bbox) : hexA(el.fill, el.fillOpacity);
        c.fill();
      }
      c.stroke();
      break;
    }
    case 'star': {
      c.beginPath();
      const n = el.pointsCount;
      const inner = el.r * el.innerRatio;
      for(let i=0;i<n*2;i++){
        const a = (i/(n*2))*Math.PI*2 - Math.PI/2 + (el.rotation||0)*Math.PI/180;
        const rr = i%2 ? inner : el.r;
        const x = el.cx + Math.cos(a)*rr;
        const y = el.cy + Math.sin(a)*rr;
        i? c.lineTo(x,y) : c.moveTo(x,y);
      }
      c.closePath();
      if(el.fillEnabled){
        c.fillStyle = el.fillGrad ? grad2(c, el.fill, el.fillGradColor2, el.fillGradAngle, bbox) : hexA(el.fill, el.fillOpacity);
        c.fill();
      }
      c.stroke();
      break;
    }
    case 'text': drawText(c, el); break;
    case 'sticky': drawSticky(c, el); break;
  }
  c.restore();
}

function drawText(c, el){
  c.font = `${el.italic?'italic ':''}${el.weight} ${el.size}px ${el.family}`;
  c.textBaseline = 'top';
  if('letterSpacing' in c) c.letterSpacing = (el.spacing||0)+'px';
  const lines = (el.text||'').split('\n');
  const lh = el.size * el.lineHeight;

  let maxW = 0;
  for(const ln of lines) maxW = Math.max(maxW, c.measureText(ln).width);
  const totalH = lines.length * lh;

  if(el.bgEnabled && el.bgEnabled){
    c.save();
    c.shadowBlur = 0;
    c.fillStyle = el.bgColor;
    c.fillRect(el.x-6, el.y-4, maxW+12, totalH+8);
    c.restore();
  }

  c.fillStyle = el.color
