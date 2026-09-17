# main.py
# Запуск:  pip install fastapi uvicorn
#          uvicorn main:app --reload --host 0.0.0.0 --port 8000
# Открыть: http://localhost:8000

from typing import Dict, List, Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI(title="CollabBoard", version="1.0.0")

MAX_ELEMENTS = 20000


class Room:
    def __init__(self) -> None:
        self.clients: Dict[WebSocket, str] = {}
        self.elements: List[dict] = []

    def snapshot(self) -> dict:
        return {
            "type": "init",
            "elements": self.elements,
            "users": list(self.clients.values()),
        }


rooms: Dict[str, Room] = {}


async def broadcast(room: Room, payload: dict, exclude: Optional[WebSocket] = None) -> None:
    dead = []
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
async def ws_endpoint(websocket: WebSocket, room_id: str, nick: str = Query("Гость")):
    await websocket.accept()
    room = rooms.setdefault(room_id, Room())
    room.clients[websocket] = nick[:24] or "Гость"

    await websocket.send_json(room.snapshot())
    await broadcast(room, {"type": "presence", "users": list(room.clients.values())}, exclude=websocket)

    try:
        while True:
            msg = await websocket.receive_json()
            t = msg.get("type")

            if t == "add":
                el = msg.get("element")
                if el and len(room.elements) < MAX_ELEMENTS:
                    room.elements.append(el)
                await broadcast(room, {"type": "add", "element": el, "from": nick}, exclude=websocket)

            elif t == "batch":
                els = msg.get("elements") or []
                for el in els:
                    if len(room.elements) < MAX_ELEMENTS:
                        room.elements.append(el)
                await broadcast(room, {"type": "batch", "elements": els}, exclude=websocket)

            elif t == "update":
                el = msg.get("element") or {}
                for i, cur in enumerate(room.elements):
                    if cur.get("id") == el.get("id"):
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

            elif t == "laser":
                await broadcast(room, msg, exclude=websocket)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        room.clients.pop(websocket, None)
        await broadcast(room, {"type": "presence", "users": list(room.clients.values())})


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

/* ---------- TOPBAR ---------- */
.topbar{
  height:54px; display:flex; align-items:center; gap:12px; padding:0 14px;
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
.board-pill{
  display:flex;align-items:center;gap:8px;padding:6px 12px;border-radius:8px;
  background:#101b30;border:1px solid var(--line);font-weight:600;font-size:12.5px;
}
.board-pill .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
.spacer{flex:1}
.users{display:flex;align-items:center}
.avatar{
  width:29px;height:29px;border-radius:50%;display:grid;place-items:center;
  font-size:11px;font-weight:700;color:#08111f;border:2px solid #0e1729;margin-left:-8px;
  transition:transform .15s;
}
.avatar:first-child{margin-left:0}
.avatar:hover{transform:translateY(-2px)}
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
.btn svg{width:15px;height:15px}

/* ---------- LAYOUT ---------- */
.workspace{position:absolute;inset:54px 0 0 0;display:flex}

/* ---------- RAIL ---------- */
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
.tool .tt{
  position:absolute;left:52px;top:50%;transform:translateY(-50%) scale(.95);
  background:#0a1322;border:1px solid var(--line2);padding:5px 9px;border-radius:7px;
  white-space:nowrap;font-size:11.5px;font-weight:600;opacity:0;pointer-events:none;transition:.13s;z-index:60;
  box-shadow:0 8px 24px -8px #000;
}
.tool .tt kbd{background:#1b2b47;border-radius:4px;padding:1px 5px;margin-left:6px;font-size:10px;color:var(--muted)}
.tool:hover .tt{opacity:1;transform:translateY(-50%) scale(1)}
.rail-sep{width:26px;height:1px;background:var(--line);margin:6px 0}

/* ---------- STAGE ---------- */
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

/* ---------- PROPS ---------- */
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
.switch span{
  position:absolute;inset:0;background:#22314f;border-radius:20px;cursor:pointer;transition:.2s;
}
.switch span::before{
  content:"";position:absolute;width:13px;height:13px;left:3px;top:3px;background:#7c8db0;border-radius:50%;transition:.2s;
}
.switch input:checked + span{background:#4f46e5}
.switch input:checked + span::before{transform:translateX(15px);background:#fff}
.empty-note{padding:24px 14px;color:var(--muted);font-size:12px;line-height:1.6;text-align:center}

/* ---------- OVERLAY ---------- */
.overlay{
  position:fixed;inset:0;background:radial-gradient(1000px 600px at 50% 0%,#16224a 0%,#070c16 60%);
  display:grid;place-items:center;z-index:100;
}
.overlay.hidden{display:none}
.card{
  width:390px;background:#0e1729;border:1px solid #23324f;border-radius:18px;padding:34px 32px 30px;
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

/* ---------- TEXT EDITOR ---------- */
#textEditor{
  position:absolute;display:none;z-index:30;background:transparent;border:1px dashed #6366f1;
  outline:none;resize:none;overflow:hidden;padding:0;margin:0;line-height:1.35;
  white-space:pre;color:#0f172a;
}
.toast{
  position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(80px);
  background:#132340;border:1px solid #2b3f68;border-radius:10px;padding:10px 18px;font-size:12.5px;
  z-index:200;opacity:0;transition:.28s cubic-bezier(.2,.9,.3,1);box-shadow:0 18px 40px -14px #000;
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
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
  <div class="spacer"></div>
  <div class="users" id="users"></div>
  <div class="users-count" id="usersCount"></div>
  <div class="zoom">
    <button id="zoomOut" title="Уменьшить">−</button>
    <button class="zval" id="zoomVal" title="Сбросить масштаб">100%</button>
    <button id="zoomIn" title="Увеличить">+</button>
  </div>
  <button class="btn ghost" id="btnUndo" title="Отменить (Ctrl+Z)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/></svg>
    Отменить
  </button>
  <button class="btn" id="btnExport">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
    Экспорт
  </button>
  <button class="btn danger" id="btnClear">Очистить</button>
</header>

<main class="workspace">
  <aside class="rail" id="rail"></aside>
  <section class="stage" id="stage">
    <canvas id="canvas"></canvas>
    <textarea id="textEditor" spellcheck="false"></textarea>
    <div class="hud">
      <div class="conn" id="conn"><span class="led"></span><span id="connTxt">Подключение…</span></div>
      <div class="divider" style="height:14px"></div>
      <span>Объектов: <b id="statCount">0</b></span>
      <div class="divider" style="height:14px"></div>
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
    <div class="hint">Все участники комнаты видят изменения мгновенно</div>
  </div>
</div>

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
   TOOLS  (15 инструментов)
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
  {id:'laser',       name:'Лазер',        icon:'zap',         key:'k', sub:'Временная указка'}
];
const TOOL_BY_ID = Object.fromEntries(TOOLS.map(t=>[t.id,t]));

/* ============================================================
   НАСТРОЙКИ (100+)
   ============================================================ */
const S = {
  /* --- Холст (21) --- */
  bgColor:'#ffffff',
  bgPattern:'grid',
  patternSize:24,
  patternColor:'#cbd5e1',
  patternOpacity:0.7,
  patternWidth:1,
  patternAngle:0,
  snapEnabled:false,
  snapGrid:20,
  snapObjects:true,
  snapThreshold:8,
  showRulers:true,
  rulerUnit:'px',
  showOrigin:false,
  pageMode:'infinite',
  pageWidth:1920,
  pageHeight:1080,
  zoomMin:0.1,
  zoomMax:8,
  canvasShadow:true,
  canvasShadowBlur:30,

  /* --- Обводка (14) --- */
  stroke:'#0f172a',
  strokeWidth:3,
  strokeOpacity:1,
  lineCap:'round',
  lineJoin:'round',
  miterLimit:10,
  dashPreset:'solid',
  dashLength:10,
  dashGap:8,
  dashOffset:0,
  strokeGradient:false,
  strokeGradType:'linear',
  strokeGradColor2:'#6366f1',
  strokeGradAngle:0,

  /* --- Заливка (6) --- */
  fillEnabled:false,
  fill:'#a5b4fc',
  fillOpacity:0.45,
  fillGradient:false,
  fillGradColor2:'#f472b6',
  fillGradAngle:0,

  /* --- Перо (12) --- */
  penSmoothing:0.6,
  penStabilizer:0.3,
  penPressure:true,
  penMinWidth:0.25,
  penMaxWidth:1.6,
  penVelocity:0.4,
  penTaperStart:0.0,
  penTaperEnd:0.2,
  penSpacing:2,
  penTexture:false,
  penTextureDensity:0.3,
  penTextureOpacity:0.35,

  /* --- Маркер (5) --- */
  hlOpacity:0.35,
  hlBlend:'multiply',
  hlWidthMul:5,
  hlRoundTip:true,
  hlCap:'butt',

  /* --- Ластик (5) --- */
  eraserSize:24,
  eraserMode:'object',
  eraserHardness:0.8,
  eraserFalloff:0.4,
  eraserCurrentLayerOnly:false,

  /* --- Фигуры (13) --- */
  cornerRadius:12,
  polySides:6,
  shapeRotation:0,
  lockAspect:false,
  starPoints:5,
  starInner:0.45,
  closePath:true,
  shadowEnabled:false,
  shadowBlur:18,
  shadowX:0,
  shadowY:6,
  shadowColor:'#0f172a',
  shadowOpacity:0.25,

  /* --- Линия/стрелка (6) --- */
  arrowHeadSize:14,
  arrowHeadStyle:'triangle',
  arrowTailStyle:'none',
  arrowCurve:0,
  arrowDouble:false,
  arrowHeadAngle:28,

  /* --- Текст (11) --- */
  fontFamily:'Inter, system-ui, sans-serif',
  fontSize:22,
  fontWeight:500,
  fontItalic:false,
  fontUnderline:false,
  textAlign:'left',
  lineHeight:1.35,
  letterSpacing:0,
  textColor:'#0f172a',
  textBgEnabled:false,
  textBgColor:'#fef9c3',

  /* --- Стикер (7) --- */
  stickyColor:'#fde68a',
  stickyFontSize:18,
  stickyFontFamily:'Inter, system-ui, sans-serif',
  stickyTextColor:'#1f2937',
  stickyShadow:true,
  stickyRadius:10,
  stickyPadding:14,

  /* --- Лазер (4) --- */
  laserColor:'#ef4444',
  laserWidth:4,
  laserFade:700,
  laserGlow:true,

  /* --- Выделение (5) --- */
  showBBox:true,
  bboxColor:'#6366f1',
  handleSize:8,
  handleColor:'#ffffff',
  snapRotation:false,

  /* --- Экспорт (5) --- */
  exportFormat:'png',
  exportScale:2,
  exportTransparent:false,
  exportQuality:0.92,
  exportIncludeGrid:false,

  /* --- Объект / слой (4) --- */
  elementOpacity:1,
  blendMode:'source-over',
  locked:false,
  visible:true
};

/* ============================================================
   СХЕМА ПАНЕЛИ
   ============================================================ */
const C = (k,l,t,o={}) => Object.assign({k,l,t}, o);

const SECTIONS = [
  /* ---------- ХОЛСТ ---------- */
  { id:'canvas', tab:'canvas', title:'Оформление холста', items:[
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

  /* ---------- ОБВОДКА ---------- */
  { id:'stroke', tools:['pen','highlighter','line','arrow','rect','ellipse','triangle','polygon','star'], title:'Обводка', items:[
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

  /* ---------- ЗАЛИВКА ---------- */
  { id:'fill', tools:['rect','ellipse','triangle','polygon','star'], title:'Заливка', items:[
    C('fillEnabled','Заливать фигуру','check'),
    C('fill','Цвет заливки','color'),
    C('fillOpacity','Непрозрачность','range',{min:0,max:1,step:0.01,pct:true}),
    C('fillGradient','Градиент','check'),
    C('fillGradColor2','Второй цвет','color'),
    C('fillGradAngle','Угол градиента','range',{min:0,max:360,step:1,suf:'°'}),
  ]},

  /* ---------- ПЕРО ---------- */
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

  /* ---------- МАРКЕР ---------- */
  { id:'hl', tools:['highlighter'], title:'Параметры маркера', items:[
    C('hlOpacity','Непрозрачность','range',{min:0.05,max:1,step:0.01,pct:true}),
    C('hlBlend','Режим наложения','select',{options:[['multiply','Умножение'],['source-over','Обычный'],['screen','Экран'],['overlay','Перекрытие'],['darken','Затемнение']]}),
    C('hlWidthMul','Множитель толщины','range',{min:1,max:14,step:0.5,suf:'×'}),
    C('hlRoundTip','Круглый наконечник','check'),
    C('hlCap','Окончание','select',{options:[['butt','Плоское'],['round','Круглое'],['square','Квадратное']]}),
  ]},

  /* ---------- ЛАСТИК ---------- */
  { id:'eraser', tools:['eraser'], title:'Параметры ластика', items:[
    C('eraserSize','Размер','range',{min:4,max:200,step:2,suf:'px'}),
    C('eraserMode','Режим','select',{options:[['object','Удалять объект'],['partial','Стирать часть']]}),
    C('eraserHardness','Жёсткость','range',{min:0,max:1,step:0.05,pct:true}),
    C('eraserFalloff','Затухание','range',{min:0,max:1,step:0.05,pct:true}),
    C('eraserCurrentLayerOnly','Только текущий слой','check'),
  ]},

  /* ---------- ФИГУРЫ ---------- */
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

  /* ---------- ЛИНИЯ / СТРЕЛКА ---------- */
  { id:'arrow', tools:['line','arrow'], title:'Линия и стрелка', items:[
    C('arrowHeadSize','Размер наконечника','range',{min:4,max:60,step:1,suf:'px'}),
    C('arrowHeadAngle','Угол наконечника','range',{min:10,max:70,step:1,suf:'°'}),
    C('arrowHeadStyle','Стиль наконечника','select',{options:[['triangle','Треугольник'],['open','Открытый'],['diamond','Ромб'],['circle','Круг'],['bar','Черта']]}),
    C('arrowTailStyle','Начало линии','select',{options:[['none','Нет'],['triangle','Треугольник'],['open','Открытый'],['diamond','Ромб'],['circle','Круг'],['bar','Черта']]}),
    C('arrowCurve','Изгиб','range',{min:-200,max:200,step:2}),
    C('arrowDouble','Двусторонняя','check'),
  ]},

  /* ---------- ТЕКСТ ---------- */
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

  /* ---------- СТИКЕР ---------- */
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

  /* ---------- ЛАЗЕР ---------- */
  { id:'laser', tools:['laser'], title:'Указка', items:[
    C('laserColor','Цвет','color'),
    C('laserWidth','Толщина','range',{min:1,max:20,step:1,suf:'px'}),
    C('laserFade','Время затухания','range',{min:150,max:4000,step:50,suf:'мс'}),
    C('laserGlow','Свечение','check'),
  ]},

  /* ---------- ВЫДЕЛЕНИЕ ---------- */
  { id:'selection', tools:['select'], title:'Выделение', items:[
    C('showBBox','Показывать рамку','check'),
    C('bboxColor','Цвет рамки','color'),
    C('handleSize','Размер маркеров','range',{min:4,max:20,step:1,suf:'px'}),
    C('handleColor','Цвет маркеров','color'),
    C('snapRotation','Привязка поворота','check'),
  ]},

  /* ---------- ЭКСПОРТ ---------- */
  { id:'export', tab:'export', title:'Экспорт изображения', items:[
    C('exportFormat','Формат','select',{options:[['png','PNG'],['jpeg','JPEG'],['webp','WebP']]}),
    C('exportScale','Масштаб','range',{min:0.5,max:6,step:0.5,suf:'×'}),
    C('exportTransparent','Прозрачный фон','check'),
    C('exportQuality','Качество','range',{min:0.1,max:1,step:0.01,pct:true}),
    C('exportIncludeGrid','Включить сетку','check'),
  ]},
];

/* ============================================================
   БАЗОВОЕ СОСТОЯНИЕ
   ============================================================ */
const state = {
  tool:'pen',
  view:{x:0, y:0, zoom:1},
  elements:[],
  selected:null,
  draft:null,
  lasers:[],
  ws:null,
  nick:'',
  room:'main',
  tab:'tool',
  undo:[],
  panning:false,
  erasing:false,
  drawing:false,
  spaceDown:false,
  pendingPoints:null,
  collapsed:{}
};

const $  = (s,r=document)=>r.querySelector(s);
const $$ = (s,r=document)=>[...r.querySelectorAll(s)];
const uid = ()=> (crypto.randomUUID ? crypto.randomUUID() : 'id-'+Math.random().toString(36).slice(2)+Date.now());

const canvas = $('#canvas');
const ctx = canvas.getContext('2d');
const stage = $('#stage');
const editor = $('#textEditor');
let W=0,H=0,DPR=1;
let dirty = true;
const markDirty = ()=>{ dirty = true; };

/* ============================================================
   ИНТЕРФЕЙС: RAIL
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
}

function setTool(id){
  if(state.tool===id) return;
  state.tool = id;
  $$('.tool').forEach(el=>el.classList.toggle('active', el.dataset.tool===id));
  const t = TOOL_BY_ID[id];
  $('#statTool').textContent = t.name;
  stage.classList.toggle('hand', id==='hand');
  stage.classList.toggle('select', id==='select' || id==='text');
  editor.style.display='none';
  renderPanel();
  markDirty();
}

/* ============================================================
   ИНТЕРФЕЙС: ПАНЕЛЬ НАСТРОЕК
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
    body.innerHTML = `<div class="empty-note">У этого инструмента нет дополнительных параметров.<br/>Выберите другой инструмент.</div>`;
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

  if(el.type==='range' || el.type==='number'){
    if(isNaN(v)) return;
  }
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
   ГЕОМЕТРИЯ / УТИЛИТЫ
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
    case 'line':
    case 'arrow': {
      const x = Math.min(el.x1,el.x2), y = Math.min(el.y1,el.y2);
      return {x, y, w: Math.abs(el.x2-el.x1), h: Math.abs(el.y2-el.y1)};
    }
    case 'rect':
    case 'sticky': return {x:el.x, y:el.y, w:el.w, h:el.h};
    case 'ellipse': return {x:el.cx-el.rx, y:el.cy-el.ry, w:el.rx*2, h:el.ry*2};
    case 'poly':
    case 'star': return {x:el.cx-el.r, y:el.cy-el.r, w:el.r*2, h:el.r*2};
    case 'text': return {x:el.x, y:el.y, w:el.w||120, h:el.h||30};
    default: return {x:0,y:0,w:0,h:0};
  }
}

function moveElement(el, dx, dy){
  switch(el.type){
    case 'path': el.points = el.points.map(p=>[p[0]+dx, p[1]+dy]); break;
    case 'line': case 'arrow': el.x1+=dx; el.y1+=dy; el.x2+=dx; el.y2+=dy; break;
    case 'rect': case 'sticky': case 'text': el.x+=dx; el.y+=dy; break;
    case 'ellipse': case 'poly': case 'star': el.cx+=dx; el.cy+=dy; break;
  }
}

function hitTest(el, p, tol){
  const b = elementBounds(el);
  const pad = tol || 6/state.view.zoom;
  if(el.type==='path'){
    // расстояние до ломаной
    const pts = el.points;
    for(let i=0;i<pts.length-1;i++){
      const d = segDist(p, pts[i], pts[i+1]);
      if(d < (el.width/2 + pad)) return true;
    }
    if(pts.length===1){
      const d = Math.hypot(p[0]-pts[0][0], p[1]-pts[0][1]);
      if(d < el.width/2 + pad) return true;
    }
    return false;
  }
  return p[0] >= b.x-pad && p[0] <= b.x+b.w+pad && p[1] >= b.y-pad && p[1] <= b.y+b.h+pad;
}

function segDist(p, a, b){
  const vx = b[0]-a[0], vy = b[1]-a[1];
  const wx = p[0]-a[0], wy = p[1]-a[1];
  const L = vx*vx+vy*vy;
  let t = L ? (wx*vx+wy*vy)/L : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(p[0]-(a[0]+t*vx), p[1]-(a[1]+t*vy));
}

/* ============================================================
   СТИЛИ ЭЛЕМЕНТОВ
   ============================================================ */
function strokeStyle(){
  return {
    stroke:S.stroke,
    width:S.strokeWidth,
    opacity:S.strokeOpacity,
    cap:S.lineCap,
    join:S.lineJoin,
    miter:S.miterLimit,
    dash:S.dashPreset,
    dashLength:S.dashLength,
    dashGap:S.dashGap,
    dashOffset:S.dashOffset,
    grad:S.strokeGradient,
    gradType:S.strokeGradType,
    gradColor2:S.strokeGradColor2,
    gradAngle:S.strokeGradAngle
  };
}
function fillStyle(){
  return {
    enabled:S.fillEnabled,
    color:S.fill,
    opacity:S.fillOpacity,
    grad:S.fillGradient,
    gradColor2:S.fillGradColor2,
    gradAngle:S.fillGradAngle
  };
}
function shadowStyle(){
  if(!S.shadowEnabled) return null;
  return {blur:S.shadowBlur, x:S.shadowX, y:S.shadowY, color:S.shadowColor, opacity:S.shadowOpacity};
}

/* ============================================================
   РЕНДЕР
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

function makeGradient(c, el, bbox, isStroke){
  const angle = ((isStroke ? el.gradAngle : el.gradAngle) || 0) * Math.PI/180;
  const cx = bbox.x + bbox.w/2, cy = bbox.y + bbox.h/2;
  const r = Math.max(bbox.w, bbox.h)/2 || 1;
  if(el.gradType==='radial'){
    const g = c.createRadialGradient(cx, cy, 0, cx, cy, r);
    g.addColorStop(0, el.stroke);
    g.addColorStop(1, el.gradColor2);
    return g;
  }
  const dx = Math.cos(angle)*r, dy = Math.sin(angle)*r;
  const g = c.createLinearGradient(cx-dx, cy-dy, cx+dx, cy+dy);
  g.addColorStop(0, isStroke ? el.stroke : el.color);
  g.addColorStop(1, el.gradColor2);
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
  if(pts.length<2) {
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
    case 'line':
    case 'arrow': drawLineArrow(c, el); break;
    case 'rect': {
      const p = pathRect(c, el);
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
    case 'text': drawText(c, el, bbox); break;
    case 'sticky': drawSticky(c, el); break;
  }
  c.restore();
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

function pathRect(c, el){
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

function drawLineArrow(c, el){
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

  const ang = Math.atan2(y2-y1, x2-x1);
  if(el.type==='arrow'){
    if(el.headStyle!=='none') head(c, x2, y2, ang, el);
    if(el.doubleHead || el.tailStyle!=='none'){
      const st = el.doubleHead ? (el.headStyle||'triangle') : el.tailStyle;
      head(c, x1, y1, ang+Math.PI, Object.assign({}, el, {headStyle:st}));
    }
  }
}

function head(c, x, y, ang, el){
  const s = el.headSize;
  const a = (el.headAngle||28) * Math.PI/180;
  c.save();
  c.setLineDash([]);
  c.lineCap = 'round';
  c.lineJoin = 'round';
  const style = el.headStyle;
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

function drawText(c, el, bbox){
  c.font = `${el.italic?'italic ':''}${el.weight} ${el.size}px ${el.family}`;
  c.textBaseline = 'top';
  c.letterSpacing = (el.spacing||0)+'px';
  const lines = (el.text||'').split('\n');
  const lh = el.size * el.lineHeight;

  let maxW = 0;
  for(const ln of lines) maxW = Math.max(maxW, c.measureText(ln).width);
  const totalH = lines.length * lh;

  if(el.bgEnabled){
    c.save();
    c.shadowBlur = 0;
    c.fillStyle = el.bgColor;
    c.fillRect(el.x-6, el.y-4, maxW+12, totalH+8);
    c.restore();
  }

  c.fillStyle = el.color;
  lines.forEach((ln,i)=>{
    const w = c.measureText(ln).width;
    let x = el.x;
    if(el.align==='center') x = el.x + (maxW - w)/2;
    else if(el.align==='right') x = el.x + (maxW - w);
    c.fillText(ln, x, el.y + i*lh);
    if(el.underline){
      c.save();
      c.strokeStyle = el.color;
      c.lineWidth = Math.max(1, el.size*0.06);
      c.setLineDash([]);
      c.beginPath();
      c.moveTo(x, el.y + i*lh + el.size*1.12);
      c.lineTo(x + w, el.y + i*lh + el.size*1.12);
      c.stroke();
      c.restore();
    }
  });
  el.w = Math.max(40, maxW);
  el.h = Math.max(el.size, totalH);
}

function drawSticky(c, el){
  c.save();
  if(el.shadow){
    c.shadowBlur = 22; c.shadowOffsetX = 0; c.shadowOffsetY = 8;
    c.shadowColor = 'rgba(15,23,42,0.28)';
  }
  const r = Math.min(el.radius, el.w/2, el.h/2);
  const p = new Path2D();
  p.moveTo(el.x+r, el.y);
  p.lineTo(el.x+el.w-r, el.y); p.quadraticCurveTo(el.x+el.w, el.y, el.x+el.w, el.y+r);
  p.lineTo(el.x+el.w, el.y+el.h-r); p.quadraticCurveTo(el.x+el.w, el.y+el.h, el.x+el.w-r, el.y+el.h);
  p.lineTo(el.x+r, el.y+el.h); p.quadraticCurveTo(el.x, el.y+el.h, el.x, el.y+el.h-r);
  p.lineTo(el.x, el.y+r); p.quadraticCurveTo(el.x, el.y, el.x+r, el.y);
  p.closePath();
  c.fillStyle = el.color;
  c.fill(p);
  c.restore();

  c.save();
  c.font = `${el.fontSize}px ${el.fontFamily}`;
  c.fillStyle = el.textColor;
  c.textBaseline = 'top';
  c.letterSpacing = '0px';
  const pad = el.padding;
  const lines = (el.text||'').split('\n');
  const lh = el.fontSize*1.3;
  const maxW = el.w - pad*2;
  let y = el.y + pad;
  for(const raw of lines){
    let line = raw;
    // простая переноска по словам
    while(c.measureText(line).width > maxW && line.length>1){
      let cut = line.length;
      while(cut>1 && c.measureText(line.slice(0,cut)).width > maxW) cut--;
      c.fillText(line.slice(0,cut), el.x+pad, y);
      line = line.slice(cut);
      y += lh;
      if(y > el.y+el.h-pad) break;
    }
    if(y > el.y+el.h-pad) break;
    c.fillText(line, el.x+pad, y);
    y += lh;
  }
  c.restore();
}

function hexA(hex, a){
  if(!hex) return `rgba(0,0,0,${a})`;
  if(hex.startsWith('rgb')) return hex;
  let h = hex.replace('#','');
  if(h.length===3) h = h.split('').map(x=>x+x).join('');
  const n = parseInt(h,16);
  return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a===undefined?1:a})`;
}

/* ---------- Сетка ---------- */
function drawGrid(c){
  const {x:vx, y:vy, zoom} = state.view;
  const size = S.patternSize * zoom;
  if(size < 4 || S.bgPattern==='none') return;

  c.save();
  c.globalAlpha = S.patternOpacity;
  c.strokeStyle = S.patternColor;
  c.fillStyle = S.patternColor;
  c.lineWidth = S.patternWidth;

  if(S.bgPattern==='grid' || S.bgPattern==='lines'){
    c.beginPath();
    let startX = vx % size;
    for(let x = startX; x < W; x += size){ c.moveTo(x, 0); c.lineTo(x, H); }
    if(S.bgPattern==='grid'){
      let startY = vy % size;
      for(let y = startY; y < H; y += size){ c.moveTo(0, y); c.lineTo(W, y); }
    }
    c.stroke();
  } else if(S.bgPattern==='dots'){
    const r = Math.max(0.7, S.patternWidth*0.9);
    let startX = vx % size;
    let startY = vy % size;
    for(let x = startX; x < W; x += size){
      for(let y = startY; y < H; y += size){
        c.beginPath();
        c.arc(x, y, r, 0, Math.PI*2);
        c.fill();
      }
    }
  }
  c.restore();
}

function drawRulers(c){
  if(!S.showRulers) return;
  const h = 18;
  c.save();
  c.fillStyle = '#0b1424';
  c.fillRect(0,0,W,h);
  c.fillRect(0,0,h,H);
  c.strokeStyle = '#1d2a44';
  c.beginPath(); c.moveTo(0,h); c.lineTo(W,h); c.moveTo(h,0); c.lineTo(h,H); c.stroke();

  const zoom = state.view.zoom;
  let step = 50;
  while(step*zoom < 44) step *= 2;
  while(step*zoom > 200) step /= 2;

  c.fillStyle = '#55688c';
  c.font = '9px Inter, sans-serif';
  c.textBaseline = 'middle';

  const startWorldX = Math.floor((-state.view.x/zoom)/step)*step;
  const endWorldX = (W - state.view.x)/zoom;
  for(let wx = startWorldX; wx < endWorldX; wx += step){
    const sx = wx*zoom + state.view.x;
    if(sx < h) continue;
    c.beginPath(); c.moveTo(sx, h-6); c.lineTo(sx, h); c.stroke();
    c.textAlign = 'left';
    c.fillText(Math.round(wx), sx+3, h/2);
  }

  const startWorldY = Math.floor((-state.view.y/zoom)/step)*step;
  const endWorldY = (H - state.view.y)/zoom;
  for(let wy = startWorldY; wy < endWorldY; wy += step){
    const sy = wy*zoom + state.view.y;
    if(sy < h) continue;
    c.beginPath(); c.moveTo(h-6, sy); c.lineTo(h, sy); c.stroke();
    c.save();
    c.translate(h/2, sy+3);
    c.rotate(-Math.PI/2);
    c.textAlign = 'right';
    c.fillText(Math.round(wy), 0, 0);
    c.restore();
  }
  c.restore();
}

/* ---------- Главный рендер ---------- */
function render(){
  ctx.setTransform(DPR,0,0,DPR,0,0);
  ctx.clearRect(0,0,W,H);

  // фон
  ctx.fillStyle = '#0a1120';
  ctx.fillRect(0,0,W,H);

  // страница
  const vx = state.view.x, vy = state.view.y, z = state.view.zoom;
  const pageW = S.pageMode==='page' ? S.pageWidth : 4000;
  const pageH = S.pageMode==='page' ? S.pageHeight : 3000;
  const px = S.pageMode==='page' ? vx : vx - 2000*z;
  const py = S.pageMode==='page' ? vy : vy - 1500*z;

  ctx.save();
  if(S.canvasShadow){
    ctx.shadowBlur = S.canvasShadowBlur;
    ctx.shadowColor = 'rgba(0,0,0,0.55)';
    ctx.shadowOffsetY = 8;
  }
  ctx.fillStyle = S.bgColor;
  ctx.fillRect(px, py, pageW*z, pageH*z);
  ctx.restore();

  drawGrid(ctx);

  // объекты
  ctx.save();
  ctx.translate(vx, vy);
  ctx.scale(z, z);
  for(const el of state.elements) drawElement(ctx, el);
  if(state.draft) drawElement(ctx, state.draft);
  ctx.restore();

  // лазер
  const now = performance.now();
  const fade = S.laserFade;
  for(const l of state.lasers){
    const alive = l.pts.filter(p => now - p[2] < fade);
    if(alive.length < 2) continue;
    ctx.save();
    ctx.translate(vx, vy);
    ctx.scale(z, z);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    if(S.laserGlow){ ctx.shadowBlur = 16; ctx.shadowColor = l.color; }
    for(let i=0;i<alive.length-1;i++){
      const a = 1 - (now - alive[i][2])/fade;
      ctx.globalAlpha = Math.max(0, a);
      ctx.strokeStyle = l.color;
      ctx.lineWidth = l.width;
      ctx.beginPath();
      ctx.moveTo(alive[i][0], alive[i][1]);
      ctx.lineTo(alive[i+1][0], alive[i+1][1]);
      ctx.stroke();
    }
    ctx.restore();
  }

  // ластик-курсор
  if(state.tool==='eraser'){
    const p = state.lastScreen;
    if(p){
      ctx.save();
      ctx.strokeStyle = '#94a3b8';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(p[0], p[1], S.eraserSize*z/2, 0, Math.PI*2);
      ctx.stroke();
      ctx.restore();
    }
  }

  // выделение
  if(state.selected && S.showBBox){
    const el = state.elements.find(e=>e.id===state.selected);
    if(el){
      const b = elementBounds(el);
      const a = w2s([b.x, b.y]);
      const bb = w2s([b.x+b.w, b.y+b.h]);
      ctx.save();
      ctx.strokeStyle = S.bboxColor;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([5,4]);
      ctx.strokeRect(a[0]-3, a[1]-3, (bb[0]-a[0])+6, (bb[1]-a[1])+6);
      ctx.setLineDash([]);
      const hs = S.handleSize;
      const pts = [[a[0],a[1]],[bb[0],a[1]],[bb[0],bb[1]],[a[0],bb[1]]];
      for(const hp of pts){
        ctx.fillStyle = S.handleColor;
        ctx.strokeStyle = S.bboxColor;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.rect(hp[0]-hs/2, hp[1]-hs/2, hs, hs);
        ctx.fill();
        ctx.stroke();
      }
      ctx.restore();
    }
  }

  drawRulers(ctx);
}

function loop(){
  const now = performance.now();
  state.lasers = state.lasers.filter(l => l.pts.some(p => now - p[2] < S.laserFade));
  if(dirty || state.lasers.length){
    render();
    dirty = false;
  }
  requestAnimationFrame(loop);
}

/* ============================================================
   ВЗАИМОДЕЙСТВИЕ
   ============================================================ */
let panStart = null;
let drawStart = null;
let moveStart = null;
let moveTarget = null;

canvas.addEventListener('pointerdown', e=>{
  if(e.button===2) return;
  canvas.setPointerCapture(e.pointerId);
  const p = pointerWorld(e);
  state.lastScreen = null;

  // панорамирование
  if(state.spaceDown || state.tool==='hand' || e.button===1){
    state.panning = true;
    stage.classList.add('dragging');
    panStart = {sx:e.clientX, sy:e.clientY, vx:state.view.x, vy:state.view.y};
    return;
  }

  if(state.tool==='laser'){
    state.lasers.push({pts:[[p[0],p[1],performance.now()]], color:S.laserColor, width:S.laserWidth});
    state.laserActive = true;
    return;
  }

  if(state.tool==='select'){
    // ищем сверху вниз
    let hit = null;
    for(let i=state.elements.length-1;i>=0;i--){
      if(hitTest(state.elements[i], p)){
        hit = state.elements[i];
        break;
      }
    }
    state.selected = hit ? hit.id : null;
    if(hit){
      moveStart = p;
      moveTarget = hit;
      const before = JSON.parse(JSON.stringify(hit));
      state._moveSnapshot = before;
    }
    markDirty();
    return;
  }

  if(state.tool==='eraser'){
    state.erasing = true;
    eraseAt(p);
    return;
  }

  if(state.tool==='text'){
    const el = {
      id:uid(), type:'text',
      x:p[0], y:p[1], text:'',
      color:S.textColor, family:S.fontFamily, size:S.fontSize, weight:S.fontWeight,
      italic:S.fontItalic, underline:S.fontUnderline, align:S.textAlign,
      lineHeight:S.lineHeight, spacing:S.letterSpacing,
      bgEnabled:S.textBgEnabled, bgColor:S.textBgColor,
      stroke:S.stroke, width:S.strokeWidth, opacity:S.strokeOpacity,
      cap:S.lineCap, join:S.lineJoin, miter:S.miterLimit,
      dash:S.dashPreset, dashLength:S.dashLength, dashGap:S.dashGap, dashOffset:S.dashOffset,
      elOpacity:S.elementOpacity, blend:S.blendMode,
      shadow:shadowStyle(), visible:true, w:120, h:S.fontSize
    };
    state.elements.push(el);
    send({type:'add', element:el});
    state.undo.push({kind:'add', id:el.id});
    openEditor(el, true);
    markDirty();
    return;
  }

  if(state.tool==='sticky'){
    const el = {
      id:uid(), type:'sticky',
      x:p[0]-90, y:p[1]-70, w:180, h:140, text:'',
      color:S.stickyColor, fontSize:S.stickyFontSize, fontFamily:S.stickyFontFamily,
      textColor:S.stickyTextColor, padding:S.stickyPadding, radius:S.stickyRadius,
      shadow:S.stickyShadow,
      stroke:S.stroke, width:S.strokeWidth, opacity:S.strokeOpacity,
      cap:S.lineCap, join:S.lineJoin, miter:S.miterLimit,
      dash:S.dashPreset, dashLength:S.dashLength, dashGap:S.dashGap, dashOffset:S.dashOffset,
      elOpacity:S.elementOpacity, blend:S.blendMode, visible:true
    };
    state.elements.push(el);
    send({type:'add', element:el});
    state.undo.push({kind:'add', id:el.id});
    openEditor(el, false);
    markDirty();
    return;
  }

  // рисование
  state.drawing = true;
  const sp = snapPoint(p);
  drawStart = sp;

  const common = Object.assign({ id:uid(), opacity:S.strokeOpacity, elOpacity:S.elementOpacity,
    blend:S.blendMode, shadow:shadowStyle(), visible:true }, strokeStyle());

  if(state.tool==='pen'){
    state.draft = Object.assign(common, {
      type:'path', tool:'pen', points:[[sp[0],sp[1]]],
      variable: S.penPressure, taperStart:S.penTaperStart, taperEnd:S.penTaperEnd,
      smoothing:S.penSmoothing, texture:S.penTexture
    });
  } else if(state.tool==='highlighter'){
    state.draft = Object.assign(common, {
      type:'path', tool:'highlighter', points:[[sp[0],sp[1]]],
      width: S.strokeWidth * S.hlWidthMul,
      opacity: S.hlOpacity, hlBlend:S.hlBlend, cap:S.hlCap
    });
  } else if(state.tool==='line' || state.tool==='arrow'){
    state.draft = Object.assign(common, {
      type: state.tool, x1:sp[0], y1:sp[1], x2:sp[0], y2:sp[1],
      headSize:S.arrowHeadSize, headStyle:S.arrowHeadStyle, tailStyle:S.arrowTailStyle,
      curve:S.arrowCurve, doubleHead:S.arrowDouble, headAngle:S.arrowHeadAngle
    });
  } else {
    const fill = fillStyle();
    const base = Object.assign(common, {
      fillEnabled:fill.enabled, fill:fill.color, fillOpacity:fill.opacity,
      fillGrad:fill.grad, fillGradColor2:fill.gradColor2, fillGradAngle:fill.gradAngle
    });
    if(state.tool==='rect'){
      state.draft = Object.assign(base, { type:'rect', x:sp[0], y:sp[1], w:0, h:0, radius:S.cornerRadius });
    } else if(state.tool==='ellipse'){
      state.draft = Object.assign(base, { type:'ellipse', cx:sp[0], cy:sp[1], rx:0, ry:0 });
    } else if(state.tool==='triangle'){
      state.draft = Object.assign(base, { type:'poly', cx:sp[0], cy:sp[1], r:0, sides:3, rotation:0 });
    } else if(state.tool==='polygon'){
      state.draft = Object.assign(base, { type:'poly', cx:sp[0], cy:sp[1], r:0, sides:S.polySides, rotation:S.shapeRotation });
    } else if(state.tool==='star'){
      state.draft = Object.assign(base, { type:'star', cx:sp[0], cy:sp[1], r:0,
        pointsCount:S.starPoints, innerRatio:S.starInner, rotation:S.shapeRotation });
    }
  }
  markDirty();
});

canvas.addEventListener('pointermove', e=>{
  const r = canvas.getBoundingClientRect();
  state.lastScreen = [e.clientX - r.left, e.clientY - r.top];
  if(state.tool==='eraser') markDirty();

  if(state.panning && panStart){
    state.view.x = panStart.vx + (e.clientX - panStart.sx);
    state.view.y = panStart.vy + (e.clientY - panStart.sy);
    markDirty();
    return;
  }

  const p = pointerWorld(e);

  if(state.laserActive){
    const last = state.lasers[state.lasers.length-1];
    last.pts.push([p[0], p[1], performance.now()]);
    throttleLaser(p);
    markDirty();
    return;
  }

  if(state.erasing){
    eraseAt(p);
    return;
  }

  if(moveTarget && moveStart){
    const dx = p[0]-moveStart[0], dy = p[1]-moveStart[1];
    const snap = JSON.parse(JSON.stringify(state._moveSnapshot));
    moveElement(moveTarget, dx, dy);
    Object.assign(moveTarget, snap);
    moveElement(moveTarget, dx, dy);
    markDirty();
    return;
  }

  if(!state.drawing || !state.draft) return;
  const sp = snapPoint(p);
  const d = state.draft;

  if(d.type==='path'){
    d.points.push([p[0], p[1]]);
  } else if(d.type==='line' || d.type==='arrow'){
    d.x2 = sp[0]; d.y2 = sp[1];
    if(S.lockAspect){
      const dx = d.x2-d.x1, dy = d.y2-d.y1;
      const a = Math.abs(dx) > Math.abs(dy) ? Math.abs(dx) : Math.abs(dy);
      d.x2 = d.x1 + Math.sign(dx||1)*a;
      d.y2 = d.y1 + Math.sign(dy||1)*a;
    }
  } else if(d.type==='rect'){
    d.w = sp[0]-d.x; d.h = sp[1]-d.y;
    if(S.lockAspect){
      const s = Math.max(Math.abs(d.w), Math.abs(d.h));
      d.w = Math.sign(d.w||1)*s; d.h = Math.sign(d.h||1)*s;
    }
  } else if(d.type==='ellipse'){
    d.rx = Math.abs(sp[0]-d.cx); d.ry = Math.abs(sp[1]-d.cy);
    if(S.lockAspect) d.ry = d.rx;
  } else if(d.type==='poly' || d.type==='star'){
    d.r = Math.hypot(sp[0]-d.cx, sp[1]-d.cy);
  }
  markDirty();
});

function finishDraw(){
  const d = state.draft;
  state.draft = null;
  state.drawing = false;
  if(!d) return;

  // отбрасываем пустышки
  const b = elementBounds(d);
  if(b.w < 2 && b.h < 2 && d.type!=='path') return;
  if(d.type==='path' && d.points.length < 2) return;

  state.elements.push(d);
  send({type:'add', element:d});
  state.undo.push({kind:'add', id:d.id});
  updateStats();
  markDirty();
}

function endInteraction(){
  if(state.panning){ state.panning = false; stage.classList.remove('dragging'); panStart = null; }
  if(state.laserActive){ state.laserActive = false; }
  if(state.erasing){ state.erasing = false; }
  if(state.drawing) finishDraw();
  if(moveTarget){
    send({type:'update', element: moveTarget});
    state.undo.push({kind:'move', id:moveTarget.id, before:state._moveSnapshot});
    moveTarget = null; moveStart = null; state._moveSnapshot = null;
  }
}

window.addEventListener('pointerup', endInteraction);
canvas.addEventListener('pointercancel', endInteraction);
canvas.addEventListener('contextmenu', e=>e.preventDefault());

/* ---------- ластик ---------- */
function eraseAt(p){
  const rad = S.eraserSize/2;
  const removed = [];

  if(S.eraserMode==='object'){
    for(let i=state.elements.length-1;i>=0;i--){
      const el = state.elements[i];
      if(hitTest(el, p, rad)){ removed.push(el.id); state.elements.splice(i,1); }
    }
  } else {
    for(const el of state.elements){
      if(el.type!=='path') continue;
      const pts = el.points;
      const keep = [];
      let cur = [];
      for(const pt of pts){
        const d = Math.hypot(pt[0]-p[0], pt[1]-p[1]);
        if(d > rad){ cur.push(pt); }
        else {
          if(cur.length>1) keep.push(cur);
          cur = [];
        }
      }
      if(cur.length>1) keep.push(cur);
      if(keep.length===0){ removed.push(el.id); }
      else if(keep.length===1 && keep[0].length===pts.length){ /* не тронут */ }
      else {
        removed.push(el.id);
        for(const seg of keep){
          const ne = JSON.parse(JSON.stringify(el));
          ne.id = uid();
          ne.points = seg;
          state.elements.push(ne);
          send({type:'add', element:ne});
        }
      }
    }
    if(removed.length){
      state.elements = state.elements.filter(e => !removed.includes(e.id));
    }
  }

  if(removed.length){
    send({type:'delete', ids:removed});
    state.undo.push({kind:'delete', ids:removed});
    updateStats();
  }
  markDirty();
}

/* ---------- лазер (троттлинг сети) ---------- */
let lastLaserSend = 0;
function throttleLaser(p){
  const now = performance.now();
  if(now - lastLaserSend < 40) return;
  lastLaserSend = now;
  send({type:'laser', pts:[[p[0],p[1]]], color:S.laserColor, width:S.laserWidth});
}

/* ============================================================
   РЕДАКТОР ТЕКСТА
   ============================================================ */
let editingEl = null;
let editorNew = false;

function openEditor(el, isNew){
  editingEl = el;
  editorNew = isNew;
  const b = elementBounds(el);
  const a = w2s([b.x, b.y]);
  editor.style.display = 'block';
  editor.style.left = a[0]+'px';
  editor.style.top = a[1]+'px';
  editor.style.width = Math.max(120, b.w*state.view.zoom + 30)+'px';
  editor.style.height = Math.max(30, b.h*state.view.zoom + 10)+'px';

  if(el.type==='sticky'){
    editor.style.font = `${el.fontSize*state.view.zoom}px ${el.fontFamily}`;
    editor.style.color = el.textColor;
    editor.style.background = el.color;
    editor.style.borderRadius = (el.radius*state.view.zoom)+'px';
    editor.style.padding = (el.padding*state.view.zoom)+'px';
    editor.style.width = (el.w*state.view.zoom)+'px';
    editor.style.height = (el.h*state.view.zoom)+'px';
  } else {
    editor.style.font = `${el.italic?'italic ':''}${el.weight} ${el.size*state.view.zoom}px ${el.family}`;
    editor.style.color = el.color;
    editor.style.background = el.bgEnabled ? el.bgColor : 'transparent';
    editor.style.padding = '0';
    editor.style.borderRadius = '0';
  }
  editor.value = el.text || '';
  editor.focus();
  editor.select();
}

function closeEditor(commit){
  if(!editingEl) return;
  const el = editingEl;
  const val = editor.value;
  editingEl = null;
  editor.style.display = 'none';

  if(commit){
    el.text = val;
    if(!val.trim() && editorNew){
      state.elements = state.elements.filter(e=>e.id!==el.id);
      send({type:'delete', ids:[el.id]});
      state.undo = state.undo.filter(u=>u.id!==el.id);
    } else {
      send({type:'update', element:el});
    }
  } else if(editorNew){
    state.elements = state.elements.filter(e=>e.id!==el.id);
    send({type:'delete', ids:[el.id]});
    state.undo = state.undo.filter(u=>u.id!==el.id);
  }
  editorNew = false;
  updateStats();
  markDirty();
}

editor.addEventListener('blur', ()=>closeEditor(true));
editor.addEventListener('keydown', e=>{
  if(e.key==='Escape'){ e.preventDefault(); closeEditor(true); }
  if(e.key==='Enter' && (e.ctrlKey||e.metaKey)){ e.preventDefault(); closeEditor(true); }
  e.stopPropagation();
});

/* ============================================================
   ЗУМ / ПАНОРАМА
   ============================================================ */
function setZoom(z, cx, cy){
  z = Math.max(S.zoomMin, Math.min(S.zoomMax, z));
  const r = canvas.getBoundingClientRect();
  if(cx===undefined){ cx = W/2; cy = H/2; }
  const wx = (cx - state.view.x)/state.view.zoom;
  const wy = (cy - state.view.y)/state.view.zoom;
  state.view.zoom = z;
  state.view.x = cx - wx*z;
  state.view.y = cy - wy*z;
  updateZoomLabel();
  markDirty();
}
function updateZoomLabel(){ $('#zoomVal').textContent = Math.round(state.view.zoom*100)+'%'; }

canvas.addEventListener('wheel', e=>{
  e.preventDefault();
  const r = canvas.getBoundingClientRect();
  if(e.ctrlKey || e.metaKey || !e.shiftKey){
    const factor = Math.exp(-e.deltaY * 0.0016);
    setZoom(state.view.zoom * factor, e.clientX-r.left, e.clientY-r.top);
  } else {
    state.view.x -= e.deltaX;
    state.view.y -= e.deltaY;
    markDirty();
  }
}, {passive:false});

$('#zoomIn').onclick = ()=>setZoom(state.view.zoom*1.2);
$('#zoomOut').onclick = ()=>setZoom(state.view.zoom/1.2);
$('#zoomVal').onclick = ()=>{ state.view.zoom = 1; state.view.x = 0; state.view.y = 0; updateZoomLabel(); markDirty(); };

/* ============================================================
   КЛАВИАТУРА
   ============================================================ */
window.addEventListener('keydown', e=>{
  if(editingEl) return;
  const tag = (e.target.tagName||'').toLowerCase();
  if(tag==='input' || tag==='select' || tag==='textarea') return;

  if(e.code==='Space'){ state.spaceDown = true; e.preventDefault(); return; }

  if((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='z'){ e.preventDefault(); doUndo(); return; }

  if(e.key==='Delete' || e.key==='Backspace'){
    if(state.selected){
      e.preventDefault();
      const id = state.selected;
      state.elements = state.elements.filter(el=>el.id!==id);
      send({type:'delete', ids:[id]});
      state.undo.push({kind:'delete', ids:[id]});
      state.selected = null;
      updateStats();
      markDirty();
    }
    return;
  }

  const k = e.key.toLowerCase();
  const tool = TOOLS.find(t=>t.key===k);
  if(tool){ setTool(tool.id); }
});
window.addEventListener('keyup', e=>{
  if(e.code==='Space') state.spaceDown = false;
});

function doUndo(){
  const a = state.undo.pop();
  if(!a) return;
  if(a.kind==='add'){
    state.elements = state.elements.filter(e=>e.id!==a.id);
    send({type:'delete', ids:[a.id]});
  } else if(a.kind==='delete'){
    // вернуть нельзя без снапшота — игнорируем
    toast('Отмена удаления недоступна');
    return;
  } else if(a.kind==='move'){
    const el = state.elements.find(x=>x.id===a.id);
    if(el){
      Object.assign(el, a.before);
      send({type:'update', element:el});
    }
  }
  updateStats();
  markDirty();
}

/* ============================================================
   WEBSOCKET
   ============================================================ */
function send(obj){
  if(state.ws && state.ws.readyState===WebSocket.OPEN){
    state.ws.send(JSON.stringify(obj));
  }
}

function connect(){
  const proto = location.protocol==='https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws/${encodeURIComponent(state.room)}?nick=${encodeURIComponent(state.nick)}`;
  setConn('warn','Подключение…');
  const ws = new WebSocket(url);
  state.ws = ws;

  ws.onopen = ()=> setConn('ok','Онлайн');
  ws.onclose = ()=>{ setConn('off','Нет связи'); setTimeout(connect, 1800); };
  ws.onerror = ()=> setConn('off','Ошибка');

  ws.onmessage = ev=>{
    let m;
    try{ m = JSON.parse(ev.data); }catch(_){ return; }
    switch(m.type){
      case 'init':
        state.elements = m.elements || [];
        renderUsers(m.users||[]);
        updateStats();
        markDirty();
        break;
      case 'add':
        if(m.element && !state.elements.some(e=>e.id===m.element.id)){
          state.elements.push(m.element);
          updateStats(); markDirty();
        }
        break;
      case 'batch':
        for(const el of (m.elements||[])){
          if(!state.elements.some(e=>e.id===el.id)) state.elements.push(el);
        }
        updateStats(); markDirty();
        break;
      case 'update': {
        const i = state.elements.findIndex(e=>e.id===m.element.id);
        if(i>=0) state.elements[i] = m.element;
        else state.elements.push(m.element);
        markDirty();
        break;
      }
      case 'delete': {
        const ids = new Set(m.ids||[]);
        state.elements = state.elements.filter(e=>!ids.has(e.id));
        if(ids.has(state.selected)) state.selected = null;
        updateStats(); markDirty();
        break;
      }
      case 'clear':
        state.elements = [];
        state.selected = null;
        updateStats(); markDirty();
        break;
      case 'presence':
        renderUsers(m.users||[]);
        break;
      case 'laser':
        state.lasers.push({
          pts:(m.pts||[]).map(p=>[p[0],p[1],performance.now()]),
          color:m.color||'#ef4444',
          width:m.width||4
        });
        markDirty();
        break;
    }
  };
}

function setConn(kind, text){
  const el = $('#conn');
  el.className = 'conn' + (kind==='ok'?'':(kind==='warn'?' warn':' off'));
  $('#connTxt').textContent = text;
}

/* ============================================================
   ПОЛЬЗОВАТЕЛИ
   ============================================================ */
const AVATAR_COLORS = ['#818cf8','#34d399','#fbbf24','#f472b6','#38bdf8','#a78bfa','#fb923c','#4ade80','#f87171','#22d3ee'];
function hash(s){ let h=0; for(let i=0;i<s.length;i++) h=(h*31+s.charCodeAt(i))|0; return Math.abs(h); }

function renderUsers(list){
  const box = $('#users');
  box.innerHTML = '';
  list.slice(0,6).forEach(n=>{
    const d = document.createElement('div');
    d.className = 'avatar';
    d.style.background = AVATAR_COLORS[hash(n) % AVATAR_COLORS.length];
    d.textContent = (n||'?').trim().slice(0,2).toUpperCase();
    d.title = n;
    box.appendChild(d);
  });
  $('#usersCount').textContent = list.length + ' онлайн';
  $('#statCount').textContent = state.elements.length;
}
function updateStats(){ $('#statCount').textContent = state.elements.length; }

/* ============================================================
   ЭКСПОРТ
   ============================================================ */
function exportImage(){
  const b = (()=>{
    if(!state.elements.length) return {x:-400,y:-300,w:800,h:600};
    let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
    for(const el of state.elements){
      const bb = elementBounds(el);
      minX = Math.min(minX, bb.x); minY = Math.min(minY, bb.y);
      maxX = Math.max(maxX, bb.x+bb.w); maxY = Math.max(maxY, bb.y+bb.h);
    }
    const pad = 40;
    return {x:minX-pad, y:minY-pad, w:(maxX-minX)+pad*2, h:(maxY-minY)+pad*2};
  })();

  const scale = S.exportScale;
  const off = document.createElement('canvas');
  off.width = Math.max(1, Math.round(b.w*scale));
  off.height = Math.max(1, Math.round(b.h*scale));
  const c = off.getContext('2d');

  if(!S.exportTransparent){
    c.fillStyle = S.bgColor;
    c.fillRect(0,0,off.width,off.height);
  }
  if(S.exportIncludeGrid && S.bgPattern!=='none'){
    c.save();
    c.strokeStyle = S.patternColor;
    c.globalAlpha = S.patternOpacity;
    c.lineWidth = S.patternWidth;
    const sz = S.patternSize*scale;
    c.beginPath();
    for(let x=0;x<off.width;x+=sz){ c.moveTo(x,0); c.lineTo(x,off.height); }
    for(let y=0;y<off.height;y+=sz){ c.moveTo(0,y); c.lineTo(off.width,y); }
    c.stroke();
    c.restore();
  }

  c.translate(-b.x*scale, -b.y*scale);
  c.scale(scale, scale);
  for(const el of state.elements) drawElement(c, el);

  const fmt = S.exportFormat;
  const mime = fmt==='png' ? 'image/png' : fmt==='jpeg' ? 'image/jpeg' : 'image/webp';
  const quality = (fmt==='png') ? undefined : S.exportQuality;

  off.toBlob(blob=>{
    if(!blob){ toast('Не удалось экспортировать'); return; }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `collabboard-${state.room}-${Date.now()}.${fmt}`;
    a.click();
    setTimeout(()=>URL.revokeObjectURL(a.href), 4000);
    toast('Файл сохранён');
  }, mime, quality);
}

/* ============================================================
   ТОСТ
   ============================================================ */
let toastTimer = null;
function toast(msg){
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>t.classList.remove('show'), 2200);
}

/* ============================================================
   КНОПКИ
   ============================================================ */
$('#btnExport').onclick = ()=>{
  state.tab = 'export';
  $$('.prop-tab').forEach(x=>x.classList.toggle('active', x.dataset.tab==='export'));
  renderPanel();
  exportImage();
};
$('#btnUndo').onclick = doUndo;
$('#btnClear').onclick = ()=>{
  if(!confirm('Очистить доску для всех участников?')) return;
  state.elements = [];
  state.selected = null;
  state.undo = [];
  send({type:'clear'});
  updateStats();
  markDirty();
  toast('Доска очищена');
};

/* ============================================================
   JOIN
   ============================================================ */
$('#joinBtn').onclick = join;
$('#nickInput').addEventListener('keydown', e=>{ if(e.key==='Enter') join(); });
$('#roomInput').addEventListener('keydown', e=>{ if(e.key==='Enter') join(); });

function join(){
  const nick = ($('#nickInput').value || '').trim() || 'Гость';
  const room = ($('#roomInput').value || '').trim() || 'main';
  state.nick = nick;
  state.room = room;
  $('#boardLabel').textContent = room;
  $('#overlay').classList.add('hidden');
  connect();
  toast(`Добро пожаловать, ${nick}!`);
}

/* ============================================================
   СТАРТ
   ============================================================ */
function boot(){
  buildRail();
  renderPanel();
  resize();
  updateZoomLabel();
  setTool('pen');
  requestAnimationFrame(loop);
  $('#nickInput').focus();
}
boot();
</script>
</body>
</html>
"""
