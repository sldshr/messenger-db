# main.py
# 🐢 Turtle Sniper — IDE-стиль мини-игра в духе Graphwar на черепашьих командах.
# Запуск:  python main.py   →   http://127.0.0.1:8000

import math
import random
import re
from collections import deque

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Turtle Sniper")

W, H = 900, 520  # размеры игрового поля (в пикселях)


# ============================================================
#                        ЛОГИКА ИГРЫ
# ============================================================

def has_path(obstacles, start, end, cell=12):
    """BFS-проверка: есть ли путь от start до end, минуя препятствия."""
    cols = W // cell + 1
    rows = H // cell + 1

    def blocked(cx, cy):
        x, y = cx * cell, cy * cell
        for ob in obstacles:
            if (ob["x"] - 1 <= x <= ob["x"] + ob["w"] + 1 and
                    ob["y"] - 1 <= y <= ob["y"] + ob["h"] + 1):
                return True
        return False

    sx = max(0, min(cols - 1, int(start[0] / cell)))
    sy = max(0, min(rows - 1, int(start[1] / cell)))
    ex = max(0, min(cols - 1, int(end[0] / cell)))
    ey = max(0, min(rows - 1, int(end[1] / cell)))

    if blocked(sx, sy) or blocked(ex, ey):
        return False

    q = deque([(sx, sy)])
    seen = {(sx, sy)}
    while q:
        x, y = q.popleft()
        if (x, y) == (ex, ey):
            return True
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < cols and 0 <= ny < rows and (nx, ny) not in seen and not blocked(nx, ny):
                seen.add((nx, ny))
                q.append((nx, ny))
    return False


class Room:
    """Комната = уровень. ИИ-генератор создаёт препятствия и позицию врага."""

    def __init__(self, rid: str, difficulty: int = 1):
        self.id = rid
        self.difficulty = max(1, difficulty)
        self.player_start = {"x": 60.0, "y": float(H - 60), "angle": 0.0}
        self.obstacles = []
        self.enemy = {"x": W - 100, "y": 100, "r": 18}
        self.generate()

    # ---- ИИ-генерация уровня ----
    def _random_obstacles(self):
        n = min(2 + self.difficulty, 8)
        obs = []
        for _ in range(n):
            w = random.randint(30, 80 + self.difficulty * 8)
            h = random.randint(30, 80 + self.difficulty * 12)
            x = random.randint(140, max(141, W - 200))
            y = random.randint(40, max(41, H - 40 - h))
            obs.append({"x": x, "y": y, "w": w, "h": h})
        return obs

    @staticmethod
    def _circle_hits_rect(cx, cy, r, o):
        nx = max(o["x"], min(cx, o["x"] + o["w"]))
        ny = max(o["y"], min(cy, o["y"] + o["h"]))
        return (nx - cx) ** 2 + (ny - cy) ** 2 <= r * r

    def _overlaps_any(self, obs, cx, cy, r):
        return any(self._circle_hits_rect(cx, cy, r, o) for o in obs)

    def generate(self):
        """Пробуем 120 раз создать валидный уровень (враг достижим, старт свободен)."""
        for _ in range(120):
            obs = self._random_obstacles()
            ex = random.randint(W - 180, W - 70)
            ey = random.randint(70, H - 70)
            r = 18
            if self._overlaps_any(obs, self.player_start["x"], self.player_start["y"], 22):
                continue
            if self._overlaps_any(obs, ex, ey, r + 6):
                continue
            if not has_path(obs, (self.player_start["x"], self.player_start["y"]), (ex, ey)):
                continue
            self.obstacles = obs
            self.enemy = {"x": ex, "y": ey, "r": r}
            return
        self.obstacles = []
        self.enemy = {"x": W - 100, "y": H // 2, "r": 18}

    def to_dict(self):
        return {
            "id": self.id,
            "difficulty": self.difficulty,
            "player_start": self.player_start,
            "obstacles": self.obstacles,
            "enemy": self.enemy,
        }


# ---- Разбор команд черепашки ----
ALIASES = {
    "forward": "forward", "fd": "forward", "fwd": "forward", "f": "forward",
    "backward": "backward", "back": "backward", "bk": "backward", "bwd": "backward", "b": "backward",
    "right": "right", "rt": "right", "r": "right",
    "left": "left", "lt": "left", "l": "left",
    "вперёд": "forward", "вперед": "forward",
    "назад": "backward",
    "вправо": "right", "направо": "right",
    "влево": "left", "налево": "left",
}

MAX_STEPS = 20000


def parse_commands(text: str):
    steps = []
    if not text:
        return steps
    # убираем комментарии
    lines = []
    for ln in text.split("\n"):
        ln = ln.split("#", 1)[0]
        lines.append(ln)
    text = "\n".join(lines)

    for p in re.split(r"[\n,;]+", text):
        p = p.strip()
        if not p:
            continue
        m = re.match(r"^([A-Za-zА-Яа-яЁё]+)\s*\(?\s*(-?\d+(?:[.,]\d+)?)\s*\)?$", p)
        if not m:
            continue
        name = m.group(1).lower()
        num = float(m.group(2).replace(",", "."))
        cmd = ALIASES.get(name, name)
        if cmd in ("forward", "backward", "right", "left"):
            steps.append((cmd, num))
    return steps


def simulate(room: Room, text: str):
    x = room.player_start["x"]
    y = room.player_start["y"]
    angle = room.player_start["angle"]
    path = [(x, y)]
    steps = parse_commands(text)
    total_moves = 0

    def finish(status):
        return {"path": path, "status": status, "steps": len(steps)}

    for cmd, arg in steps:
        if cmd == "right":
            angle += arg
        elif cmd == "left":
            angle -= arg
        elif cmd in ("forward", "backward"):
            dist = arg if cmd == "forward" else -arg
            if dist == 0:
                continue
            sign = 1 if dist > 0 else -1
            length = abs(dist)
            n = max(1, int(length / 2.0))
            step = length / n
            ux = math.cos(math.radians(angle)) * step * sign
            uy = math.sin(math.radians(angle)) * step * sign
            for _ in range(n):
                total_moves += 1
                if total_moves > MAX_STEPS:
                    return finish("too_long")
                x += ux
                y += uy
                path.append((x, y))

                if x < 0 or x > W or y < 0 or y > H:
                    return finish("out_of_bounds")

                for o in room.obstacles:
                    if (o["x"] <= x <= o["x"] + o["w"] and
                            o["y"] <= y <= o["y"] + o["h"]):
                        return finish("hit_obstacle")

                dx = x - room.enemy["x"]
                dy = y - room.enemy["y"]
                if dx * dx + dy * dy <= room.enemy["r"] ** 2:
                    return finish("success")

    return finish("out_of_commands")


# ============================================================
#                          API
# ============================================================

rooms: dict = {}


class ShootRequest(BaseModel):
    commands: str = ""


def get_room(rid: str) -> Room:
    if rid not in rooms:
        try:
            diff = max(1, int(rid))
        except ValueError:
            diff = 1
        rooms[rid] = Room(rid, diff)
    return rooms[rid]


@app.get("/api/room/{rid}")
def api_get_room(rid: str):
    return get_room(rid).to_dict()


@app.post("/api/room/{rid}/shoot")
def api_shoot(rid: str, req: ShootRequest):
    r = get_room(rid)
    return simulate(r, req.commands)


@app.post("/api/room/{rid}/regen")
def api_regen(rid: str):
    r = get_room(rid)
    r.generate()
    return r.to_dict()


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


# ============================================================
#                      HTML / CSS / JS
# ============================================================

HTML_PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Turtle Sniper</title>
<style>
  :root {
    --bg:        #0d1117;
    --bg-2:      #010409;
    --panel:     #161b22;
    --panel-2:   #0d1117;
    --panel-3:   #1c2128;
    --border:    #21262d;
    --border-2:  #30363d;
    --text:      #c9d1d9;
    --text-dim:  #8b949e;
    --text-faint:#484f58;
    --accent:    #58a6ff;
    --accent-2:  #1f6feb;
    --success:   #3fb950;
    --success-2: #2ea043;
    --danger:    #f85149;
    --warn:      #d29922;
    --code-cmd:  #ff7b72;
    --code-num:  #79c0ff;
    --code-cmt:  #6e7681;
    --code-err:  #f85149;
    --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, "Liberation Mono", monospace;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; height: 100%;
    background: var(--bg-2);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    font-size: 14px;
    overflow: hidden;
  }
  button { font-family: inherit; }

  .app { display: flex; flex-direction: column; height: 100vh; }

  /* ---------- Header ---------- */
  .hdr {
    height: 52px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 0 16px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
  }
  .brand {
    display: flex; align-items: center; gap: 10px;
    font-weight: 600; letter-spacing: -0.01em;
    font-size: 15px;
  }
  .brand svg { width: 22px; height: 22px; color: var(--accent); }
  .brand .sub { color: var(--text-faint); font-weight: 400; font-size: 12px; margin-left: 6px; }

  .hdr-spacer { flex: 1; }

  .room-chip {
    display: flex; align-items: center; gap: 8px;
    height: 32px; padding: 0 12px;
    background: var(--panel-3);
    border: 1px solid var(--border-2);
    border-radius: 8px;
    font-size: 13px;
    color: var(--text-dim);
  }
  .room-chip svg { width: 14px; height: 14px; color: var(--text-faint); }
  .room-chip b { color: var(--text); font-weight: 600; }
  .room-chip .diff {
    padding: 1px 7px;
    border-radius: 999px;
    background: rgba(88,166,255,0.14);
    color: var(--accent);
    font-size: 11px;
    font-weight: 600;
  }

  /* ---------- Main grid ---------- */
  .main {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(360px, 1fr) minmax(440px, 620px);
    gap: 12px;
    padding: 12px;
  }

  /* ---------- Left column: canvas + status ---------- */
  .left { display: flex; flex-direction: column; gap: 12px; min-width: 0; min-height: 0; }

  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .card-head {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-faint);
  }
  .card-head svg { width: 13px; height: 13px; }
  .card-head .dot { width: 8px; height: 8px; border-radius: 50%; }

  .canvas-card { flex: 1; min-height: 0; }
  .canvas-body {
    flex: 1; min-height: 0;
    padding: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    background:
      radial-gradient(1200px 400px at 50% -10%, rgba(88,166,255,0.05), transparent 70%),
      var(--panel-2);
    border-radius: 0 0 10px 10px;
  }
  canvas {
    max-width: 100%;
    max-height: 100%;
    width: auto;
    height: auto;
    border-radius: 6px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    display: block;
  }

  .status-card { flex-shrink: 0; }
  .status-body {
    display: flex; align-items: center; gap: 12px;
    padding: 12px 14px;
    min-height: 56px;
  }
  .status-icon {
    width: 32px; height: 32px;
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    background: var(--panel-3);
    color: var(--text-dim);
  }
  .status-icon svg { width: 18px; height: 18px; }
  .status-title { font-weight: 600; font-size: 14px; }
  .status-sub { font-size: 12px; color: var(--text-dim); margin-top: 2px; }
  .status-body.ok   .status-icon { background: rgba(63,185,80,0.15); color: var(--success); }
  .status-body.bad  .status-icon { background: rgba(248,81,73,0.15); color: var(--danger); }
  .status-body.warn .status-icon { background: rgba(210,153,34,0.15); color: var(--warn); }
  .status-body.info .status-icon { background: rgba(88,166,255,0.15); color: var(--accent); }

  /* ---------- Right column: IDE ---------- */
  .ide {
    display: flex;
    flex-direction: column;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    min-height: 0;
  }
  .tabbar {
    display: flex;
    align-items: center;
    background: var(--panel-2);
    border-bottom: 1px solid var(--border);
    height: 40px;
    flex-shrink: 0;
  }
  .tabs { display: flex; height: 100%; }
  .tab {
    display: flex; align-items: center; gap: 8px;
    padding: 0 14px;
    font-size: 13px;
    color: var(--text-dim);
    border-right: 1px solid var(--border);
    background: var(--panel);
    position: relative;
  }
  .tab.active {
    color: var(--text);
    background: var(--panel);
  }
  .tab.active::after {
    content: "";
    position: absolute;
    left: 0; right: 0; top: 0;
    height: 2px;
    background: var(--accent);
  }
  .tab svg { width: 14px; height: 14px; color: #ffa657; }
  .tab .dot { color: var(--text-faint); margin-left: 6px; }

  .actions {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 6px;
    padding-right: 8px;
  }
  .btn {
    display: inline-flex; align-items: center; gap: 6px;
    height: 28px; padding: 0 10px;
    border-radius: 6px;
    border: 1px solid var(--border-2);
    background: var(--panel-3);
    color: var(--text);
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
    transition: background .12s, border-color .12s, transform .06s;
  }
  .btn svg { width: 14px; height: 14px; }
  .btn:hover { background: #262c36; border-color: #3d444d; }
  .btn:active { transform: translateY(1px); }
  .btn.primary {
    background: var(--success-2);
    border-color: var(--success-2);
    color: #fff;
  }
  .btn.primary:hover { background: #2cbf51; border-color: #2cbf51; }
  .btn.ghost { background: transparent; }
  .btn.ghost:hover { background: var(--panel-3); }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* ---------- Editor ---------- */
  .editor-wrap {
    flex: 1;
    min-height: 0;
    display: flex;
    position: relative;
    background: var(--panel);
    overflow: hidden;
  }
  .gutter {
    width: 52px;
    flex-shrink: 0;
    padding: 14px 8px 14px 0;
    text-align: right;
    font-family: var(--mono);
    font-size: 13px;
    line-height: 21px;
    color: var(--text-faint);
    background: var(--panel-2);
    border-right: 1px solid var(--border);
    user-select: none;
    overflow: hidden;
    position: relative;
  }
  .gutter-inner { will-change: transform; }
  .gutter-inner .ln { display: block; }
  .gutter-inner .ln.cur { color: var(--text-dim); }

  .code-area {
    flex: 1;
    position: relative;
    overflow: hidden;
    min-width: 0;
  }
  .code-area pre.highlight,
  .code-area textarea {
    margin: 0;
    border: 0;
    outline: 0;
    padding: 14px 16px;
    font-family: var(--mono);
    font-size: 13px;
    line-height: 21px;
    tab-size: 4;
    -moz-tab-size: 4;
    white-space: pre;
    word-wrap: normal;
    overflow-wrap: normal;
    letter-spacing: 0;
  }
  .code-area pre.highlight {
    position: absolute;
    inset: 0;
    overflow: hidden;
    pointer-events: none;
    color: var(--text);
    background: transparent;
  }
  .code-area pre.highlight code {
    font: inherit;
    display: block;
    will-change: transform;
  }
  .code-area textarea {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    background: transparent;
    color: transparent;
    caret-color: var(--accent);
    resize: none;
    overflow: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--border-2) transparent;
  }
  .code-area textarea::selection { background: rgba(88,166,255,0.30); color: transparent; }
  .code-area textarea::-webkit-scrollbar { width: 10px; height: 10px; }
  .code-area textarea::-webkit-scrollbar-thumb {
    background: var(--border-2); border-radius: 6px; border: 2px solid var(--panel);
  }
  .code-area textarea::-webkit-scrollbar-thumb:hover { background: #4a5260; }

  /* Токены */
  .tk-cmd { color: var(--code-cmd); }
  .tk-num { color: var(--code-num); }
  .tk-cmt { color: var(--code-cmt); font-style: italic; }
  .tk-id  { color: #d2a8ff; }

  /* ---------- Output ---------- */
  .output {
    flex-shrink: 0;
    border-top: 1px solid var(--border);
    background: var(--panel-2);
    display: flex;
    flex-direction: column;
    height: 148px;
  }
  .output-head {
    display: flex; align-items: center; gap: 8px;
    height: 32px;
    padding: 0 12px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-faint);
    border-bottom: 1px solid var(--border);
  }
  .output-head .chip {
    margin-left: auto;
    padding: 2px 8px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.05em;
    border-radius: 4px;
    background: var(--panel-3);
    color: var(--text-dim);
    text-transform: none;
  }
  .output-head .chip.ok { background: rgba(63,185,80,0.15); color: var(--success); }
  .output-head .chip.bad { background: rgba(248,81,73,0.15); color: var(--danger); }
  .output-head .chip.warn { background: rgba(210,153,34,0.15); color: var(--warn); }
  .output-body {
    flex: 1;
    min-height: 0;
    padding: 10px 14px;
    font-family: var(--mono);
    font-size: 12.5px;
    line-height: 1.55;
    color: var(--text-dim);
    overflow: auto;
    white-space: pre-wrap;
    scrollbar-width: thin;
    scrollbar-color: var(--border-2) transparent;
  }
  .output-body::-webkit-scrollbar { width: 8px; }
  .output-body::-webkit-scrollbar-thumb { background: var(--border-2); border-radius: 4px; }
  .output-body b { color: var(--text); }
  .output-body .k { color: var(--accent); }

  /* Hint chips */
  .hints {
    display: flex; flex-wrap: wrap; gap: 6px;
    padding: 8px 12px;
    border-top: 1px solid var(--border);
    background: var(--panel-2);
  }
  .hint {
    font-family: var(--mono);
    font-size: 11px;
    padding: 3px 8px;
    border-radius: 4px;
    background: var(--panel-3);
    color: var(--text-dim);
    border: 1px solid var(--border);
    cursor: pointer;
    user-select: none;
    transition: background .12s;
  }
  .hint:hover { background: #262c36; color: var(--text); }
  .hint .k { color: var(--code-cmd); }

  /* Toast */
  .toast-wrap {
    position: fixed;
    bottom: 20px; right: 20px;
    display: flex; flex-direction: column; gap: 8px;
    z-index: 50;
    pointer-events: none;
  }
  .toast {
    background: var(--panel-3);
    border: 1px solid var(--border-2);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    color: var(--text);
    box-shadow: 0 8px 24px rgba(0,0,0,0.5);
    animation: slideIn .18s ease-out;
  }
  .toast.ok { border-color: var(--success-2); }
  .toast.bad { border-color: var(--danger); }
  @keyframes slideIn { from { transform: translateX(20px); opacity: 0; } to { transform: none; opacity: 1; } }

  /* Confetti-less victory flash */
  .flash {
    position: absolute;
    inset: 0;
    pointer-events: none;
    border-radius: 10px;
    box-shadow: inset 0 0 0 2px transparent;
    animation: flashRing .6s ease-out;
  }
  @keyframes flashRing {
    0% { box-shadow: inset 0 0 0 2px rgba(63,185,80,0.9); }
    100% { box-shadow: inset 0 0 0 2px rgba(63,185,80,0); }
  }

  @media (max-width: 980px) {
    .main { grid-template-columns: 1fr; }
    .ide { min-height: 520px; }
  }
</style>
</head>
<body>
<div class="app">

  <!-- ============ HEADER ============ -->
  <header class="hdr">
    <div class="brand">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3 L20 7.5 L20 16.5 L12 21 L4 16.5 L4 7.5 Z"/>
        <circle cx="12" cy="12" r="3"/>
        <path d="M12 3 V9 M20 7.5 L15 10 M20 16.5 L15 14 M12 21 V15 M4 16.5 L9 14 M4 7.5 L9 10"/>
      </svg>
      <span>Turtle Sniper</span>
      <span class="sub">Graphwar on turtle commands</span>
    </div>

    <div class="hdr-spacer"></div>

    <div class="room-chip">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M3 21V7l9-4 9 4v14"/>
        <path d="M9 21V12h6v9"/>
      </svg>
      <span>Комната <b id="roomNum">1</b></span>
      <span class="diff" id="roomDiff">ур. 1</span>
    </div>
  </header>

  <!-- ============ MAIN ============ -->
  <div class="main">

    <!-- ----- LEFT ----- -->
    <section class="left">
      <div class="card canvas-card">
        <div class="card-head">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2"/>
            <path d="M3 9h18 M9 21V9"/>
          </svg>
          Поле
          <span style="margin-left:auto; font-weight:500; letter-spacing:0; text-transform:none; font-size:11px; color:var(--text-faint)">
            <span style="color:var(--success)">●</span> старт
            <span style="color:var(--danger); margin-left:8px">●</span> враг
            <span style="color:#4b5872; margin-left:8px">●</span> стены
          </span>
        </div>
        <div class="canvas-body" id="canvasBody">
          <canvas id="cv" width="900" height="520"></canvas>
        </div>
      </div>

      <div class="card status-card">
        <div class="status-body info" id="statusBody">
          <div class="status-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="9"/>
              <path d="M12 8v4l3 2"/>
            </svg>
          </div>
          <div>
            <div class="status-title" id="statusTitle">Загрузка…</div>
            <div class="status-sub" id="statusSub">Готовим комнату</div>
          </div>
        </div>
      </div>
    </section>

    <!-- ----- RIGHT: IDE ----- -->
    <section class="ide">
      <div class="tabbar">
        <div class="tabs">
          <div class="tab active">
            <svg viewBox="0 0 24 24" fill="currentColor">
              <path d="M9.4 2h5.2l.6 2.1 2 .8 1.9-1.1 3.7 3.7-1.1 1.9.8 2 2.1.6v5.2l-2.1.6-.8 2 1.1 1.9-3.7 3.7-1.9-1.1-2 .8-.6 2.1H9.4l-.6-2.1-2-.8-1.9 1.1L1.2 18.6l1.1-1.9-.8-2L-.6 14.1V8.9l2.1-.6.8-2L1.2 4.4 4.9.7l1.9 1.1 2-.8.6-2z"/>
            </svg>
            main.py
            <span class="dot">●</span>
          </div>
        </div>
        <div class="actions">
          <button class="btn" id="regen" title="Сгенерировать новый уровень в этой комнате">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
              <path d="M23 4v6h-6"/>
              <path d="M1 20v-6h6"/>
              <path d="M3.5 9a9 9 0 0 1 14.9-3.4L23 10"/>
              <path d="M20.5 15a9 9 0 0 1-14.9 3.4L1 14"/>
            </svg>
            Заново
          </button>
          <button class="btn" id="next" title="Следующая комната">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
              <path d="M5 12h14"/>
              <path d="m12 5 7 7-7 7"/>
            </svg>
            Дальше
          </button>
          <button class="btn primary" id="run" title="Ctrl/Cmd + Enter">
            <svg viewBox="0 0 24 24" fill="currentColor">
              <path d="M8 5v14l11-7z"/>
            </svg>
            Запустить
          </button>
        </div>
      </div>

      <div class="editor-wrap" id="editorWrap">
        <div class="gutter"><div class="gutter-inner" id="gutterInner"></div></div>
        <div class="code-area">
          <pre class="highlight" aria-hidden="true"><code id="hl"></code></pre>
          <textarea id="code" wrap="off" spellcheck="false" autocapitalize="off"
                    autocomplete="off" autocorrect="off"></textarea>
        </div>
      </div>

      <div class="hints">
        <span class="hint" data-cmd="forward 100"><span class="k">forward</span> N</span>
        <span class="hint" data-cmd="backward 50"><span class="k">backward</span> N</span>
        <span class="hint" data-cmd="right 90"><span class="k">right</span> N</span>
        <span class="hint" data-cmd="left 45"><span class="k">left</span> N</span>
        <span class="hint" data-cmd="# комментарий"># комментарий</span>
      </div>

      <div class="output">
        <div class="output-head">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:13px">
            <path d="M4 17l6-6-6-6"/>
            <path d="M12 19h8"/>
          </svg>
          Вывод
          <span class="chip" id="outChip">—</span>
        </div>
        <div class="output-body" id="outBody">
          Напиши команды в редакторе — превью появится автоматически.
        </div>
      </div>
    </section>

  </div>
</div>

<div class="toast-wrap" id="toasts"></div>

<script>
/* ============================================================
   Состояние
============================================================ */
const state = {
  roomId: '1',
  room: null,
  preview: null,   // результат последнего предпросмотра
  run: null,       // { result, idx } — результат запуска + индекс анимации
  rafId: null,
  previewReq: 0,
  previewTimer: null,
  solvedRooms: new Set(),
};

/* ============================================================
   DOM
============================================================ */
const cv = document.getElementById('cv');
const ctx = cv.getContext('2d');

const codeEl = document.getElementById('code');
const hlEl = document.getElementById('hl');
const gutterInner = document.getElementById('gutterInner');

const statusBody = document.getElementById('statusBody');
const statusTitle = document.getElementById('statusTitle');
const statusSub = document.getElementById('statusSub');
const statusIcon = statusBody.querySelector('.status-icon');

const outBody = document.getElementById('outBody');
const outChip = document.getElementById('outChip');
const roomNumEl = document.getElementById('roomNum');
const roomDiffEl = document.getElementById('roomDiff');
const canvasBody = document.getElementById('canvasBody');

/* ============================================================
   SVG-иконки для статус-бара
============================================================ */
const ICONS = {
  info: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round">
           <circle cx="12" cy="12" r="9"/><path d="M12 16v-4 M12 8h.01"/></svg>`,
  ok:   `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
           stroke-linecap="round" stroke-linejoin="round">
           <path d="M20 6L9 17l-5-5"/></svg>`,
  bad:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
           stroke-linecap="round" stroke-linejoin="round">
           <circle cx="12" cy="12" r="9"/>
           <path d="M15 9l-6 6 M9 9l6 6"/></svg>`,
  warn: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
           stroke-linecap="round" stroke-linejoin="round">
           <path d="M10.3 3.6 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.6a2 2 0 0 0-3.4 0z"/>
           <path d="M12 9v4 M12 17h.01"/></svg>`,
};

/* ============================================================
   Подсветка синтаксиса
============================================================ */
const CMD_SET = new Set([
  'forward','backward','right','left','fd','bk','rt','lt','f','b','r','l','fwd','bwd',
  'вперёд','вперед','назад','вправо','направо','влево','налево'
]);

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function highlightCode(code) {
  return code.split('\n').map(line => {
    let out = '';
    let i = 0;
    while (i < line.length) {
      const ch = line[i];
      if (ch === '#') {
        out += `<span class="tk-cmt">${escapeHtml(line.slice(i))}</span>`;
        break;
      }
      if (/\s/.test(ch)) {
        out += escapeHtml(ch); i++; continue;
      }
      const wm = line.slice(i).match(/^[A-Za-zА-Яа-яЁё]+/);
      if (wm) {
        const w = wm[0];
        const low = w.toLowerCase();
        if (CMD_SET.has(low)) out += `<span class="tk-cmd">${w}</span>`;
        else out += `<span class="tk-id">${w}</span>`;
        i += w.length; continue;
      }
      const nm = line.slice(i).match(/^-?\d+(?:[.,]\d+)?/);
      if (nm) {
        out += `<span class="tk-num">${nm[0]}</span>`;
        i += nm[0].length; continue;
      }
      out += escapeHtml(ch); i++;
    }
    return out;
  }).join('\n');
}

function refreshEditor() {
  const raw = codeEl.value;
  hlEl.innerHTML = highlightCode(raw) + '\n';

  // gutter
  const lines = raw.split('\n').length;
  const curLine = raw.slice(0, codeEl.selectionStart).split('\n').length;
  let g = '';
  for (let i = 1; i <= Math.max(lines, 1); i++) {
    g += `<span class="ln${i === curLine ? ' cur' : ''}">${i}</span>`;
  }
  gutterInner.innerHTML = g;
  syncScroll();
}

function syncScroll() {
  const st = codeEl.scrollTop, sl = codeEl.scrollLeft;
  hlEl.style.transform = `translate(${-sl}px, ${-st}px)`;
  gutterInner.style.transform = `translateY(${-st}px)`;
}

codeEl.addEventListener('scroll', syncScroll, { passive: true });
codeEl.addEventListener('input', () => {
  refreshEditor();
  schedulePreview();
});
codeEl.addEventListener('click', refreshEditor);
codeEl.addEventListener('keyup', () => {
  // обновляем "текущую" строку в гуттере
  const raw = codeEl.value;
  const curLine = raw.slice(0, codeEl.selectionStart).split('\n').length;
  [...gutterInner.children].forEach((el, idx) => {
    el.classList.toggle('cur', idx + 1 === curLine);
  });
});
codeEl.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault(); runShot(); return;
  }
  if (e.key === 'Tab') {
    e.preventDefault();
    const s = codeEl.selectionStart, en = codeEl.selectionEnd;
    const v = codeEl.value;
    codeEl.value = v.slice(0, s) + '    ' + v.slice(en);
    codeEl.selectionStart = codeEl.selectionEnd = s + 4;
    refreshEditor();
    schedulePreview();
  }
});

/* ============================================================
   Комнаты
============================================================ */
async function loadRoom(id, silent) {
  state.roomId = String(id);
  const r = await fetch(`/api/room/${state.roomId}`);
  state.room = await r.json();
  state.preview = null;
  stopAnim();
  state.run = null;
  roomNumEl.textContent = state.roomId;
  roomDiffEl.textContent = `ур. ${state.room.difficulty}`;
  setStatus('info', `Комната ${state.roomId}`, `Препятствий: ${state.room.obstacles.length}`);
  logOut([
    ['k', `# Комната ${state.roomId}`],
    ['', `Сложность: ${state.room.difficulty}`],
    ['', `Стены: ${state.room.obstacles.length}`],
    ['', 'Напиши команды — превью покажет путь.'],
  ]);
  outChip.textContent = '—';
  outChip.className = 'chip';
  draw();
  if (!silent) schedulePreview(0);
}

/* ============================================================
   Запросы
============================================================ */
function schedulePreview(delay = 180) {
  if (state.previewTimer) clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(runPreview, delay);
}

async function runPreview() {
  if (!state.room) return;
  const myId = ++state.previewReq;
  try {
    const r = await fetch(`/api/room/${state.roomId}/shoot`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ commands: codeEl.value }),
    });
    const data = await r.json();
    if (myId !== state.previewReq) return;
    state.preview = data;
    state.run = null;
    stopAnim();
    draw();
  } catch (e) { /* тихо */ }
}

async function runShot() {
  if (!state.room) return;
  if (state.previewTimer) clearTimeout(state.previewTimer);
  state.previewReq++; // отменяем старые превью
  const r = await fetch(`/api/room/${state.roomId}/shoot`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ commands: codeEl.value }),
  });
  const data = await r.json();
  state.preview = null;
  state.run = { result: data, idx: 0 };

  const M = {
    success:         ['ok',   'Попадание',        'Враг поражён. Жми «Дальше»'],
    hit_obstacle:    ['bad',  'Столкновение',     'Черепашка врезалась в стену'],
    out_of_bounds:   ['warn', 'Вне поля',         'Черепашка покинула игровое поле'],
    out_of_commands: ['info', 'Команды закончились', 'Цель не достигнута'],
    too_long:        ['warn', 'Слишком долго',    'Слишком много шагов'],
  };
  const [cls, title, sub] = M[data.status] || ['info', 'Готово', ''];
  setStatus(cls, title, sub);
  updateChip(data.status);
  logOut([
    ['k', `$ run  (${data.status})`],
    ['', `точек траектории: ${data.path.length}`],
    ['', `команд: ${data.steps}`],
  ]);

  if (data.status === 'success') {
    if (!state.solvedRooms.has(state.roomId)) {
      state.solvedRooms.add(state.roomId);
      toast(`Комната ${state.roomId} пройдена`, 'ok');
    }
    flashVictory();
  }

  startAnim();
}

function updateChip(status) {
  const map = {
    success: ['OK', 'ok'],
    hit_obstacle: ['СТЕНА', 'bad'],
    out_of_bounds: ['ГРАНИЦА', 'warn'],
    out_of_commands: ['—', ''],
    too_long: ['—', 'warn'],
  };
  const [txt, cls] = map[status] || ['—', ''];
  outChip.textContent = txt;
  outChip.className = 'chip ' + cls;
}

function logOut(rows) {
  outBody.innerHTML = rows.map(([cls, txt]) =>
    cls === 'k' ? `<b>${txt}</b>` : txt
  ).join('\n');
}

function setStatus(cls, title, sub) {
  statusBody.className = 'status-body ' + cls;
  statusIcon.innerHTML = ICONS[cls] || ICONS.info;
  statusTitle.textContent = title;
  statusSub.textContent = sub;
}

/* ============================================================
   Тост
============================================================ */
function toast(text, cls = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + cls;
  el.textContent = text;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s, transform .3s';
    el.style.opacity = '0';
    el.style.transform = 'translateX(20px)';
    setTimeout(() => el.remove(), 320);
  }, 2200);
}

/* ============================================================
   Анимация
============================================================ */
function stopAnim() {
  if (state.rafId) cancelAnimationFrame(state.rafId);
  state.rafId = null;
}

function startAnim() {
  stopAnim();
  state.run.idx = 0;
  const path = state.run.result.path;
  const total = path.length;
  if (total <= 1) { draw(); return; }
  const step = Math.max(1, Math.ceil(total / 130));
  const tick = () => {
    state.run.idx += step;
    if (state.run.idx >= total - 1) {
      state.run.idx = total - 1;
      draw();
      state.rafId = null;
      return;
    }
    draw();
    state.rafId = requestAnimationFrame(tick);
  };
  state.rafId = requestAnimationFrame(tick);
}

/* ============================================================
   Отрисовка
============================================================ */
function draw() {
  // фон
  ctx.fillStyle = '#0b0f16';
  ctx.fillRect(0, 0, cv.width, cv.height);

  // тонкая сетка
  ctx.strokeStyle = 'rgba(88,166,255,0.045)';
  ctx.lineWidth = 1;
  for (let x = 0; x <= cv.width; x += 30) {
    ctx.beginPath(); ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, cv.height); ctx.stroke();
  }
  for (let y = 0; y <= cv.height; y += 30) {
    ctx.beginPath(); ctx.moveTo(0, y + .5); ctx.lineTo(cv.width, y + .5); ctx.stroke();
  }
  // крупная сетка
  ctx.strokeStyle = 'rgba(88,166,255,0.10)';
  for (let x = 0; x <= cv.width; x += 150) {
    ctx.beginPath(); ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, cv.height); ctx.stroke();
  }
  for (let y = 0; y <= cv.height; y += 150) {
    ctx.beginPath(); ctx.moveTo(0, y + .5); ctx.lineTo(cv.width, y + .5); ctx.stroke();
  }

  if (!state.room) return;

  // стены
  for (const o of state.room.obstacles) {
    const g = ctx.createLinearGradient(o.x, o.y, o.x, o.y + o.h);
    g.addColorStop(0, '#2c3446');
    g.addColorStop(1, '#1e2532');
    ctx.fillStyle = g;
    ctx.fillRect(o.x, o.y, o.w, o.h);
    ctx.strokeStyle = 'rgba(120,140,180,0.35)';
    ctx.lineWidth = 1;
    ctx.strokeRect(o.x + .5, o.y + .5, o.w - 1, o.h - 1);
  }

  const isRun = !!state.run;
  const result = isRun ? state.run.result : state.preview;

  // призрак полного пути при запуске
  if (isRun) {
    const path = result.path;
    ctx.beginPath();
    ctx.moveTo(path[0][0], path[0][1]);
    for (let i = 1; i < path.length; i++) ctx.lineTo(path[i][0], path[i][1]);
    ctx.strokeStyle = 'rgba(88,166,255,0.10)';
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    ctx.stroke();
  }

  // путь
  if (result && result.path && result.path.length > 1) {
    const status = result.status;
    const palette = {
      success:         ['rgba(63,185,80,0.95)',   '#3fb950'],
      hit_obstacle:    ['rgba(248,81,73,0.95)',   '#f85149'],
      out_of_bounds:   ['rgba(210,153,34,0.95)',  '#d29922'],
      out_of_commands: ['rgba(88,166,255,0.65)',  '#58a6ff'],
      too_long:        ['rgba(210,153,34,0.95)',  '#d29922'],
    };
    const [lineColor, headColor] = palette[status] || palette.out_of_commands;
    const path = result.path;
    const end = isRun ? Math.min(state.run.idx, path.length - 1) : path.length - 1;

    // превью — пунктир, run — сплошная
    ctx.beginPath();
    ctx.moveTo(path[0][0], path[0][1]);
    for (let i = 1; i <= end; i++) ctx.lineTo(path[i][0], path[i][1]);
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = isRun ? 2.6 : 1.8;
    ctx.setLineDash(isRun ? [] : [6, 6]);
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
    ctx.setLineDash([]);

    // голова
    if (isRun && end > 0) {
      const [hx, hy] = path[end];
      ctx.beginPath();
      ctx.arc(hx, hy, 7, 0, Math.PI * 2);
      ctx.fillStyle = headColor;
      ctx.shadowColor = headColor;
      ctx.shadowBlur = 14;
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  }

  // враг — мишень
  drawEnemy(state.room.enemy);

  // старт (черепашка)
  drawTurtle(state.room.player_start);
}

function drawEnemy(e) {
  // внешнее свечение
  const grd = ctx.createRadialGradient(e.x, e.y, 0, e.x, e.y, e.r * 2.2);
  grd.addColorStop(0, 'rgba(248,81,73,0.35)');
  grd.addColorStop(1, 'rgba(248,81,73,0)');
  ctx.fillStyle = grd;
  ctx.beginPath();
  ctx.arc(e.x, e.y, e.r * 2.2, 0, Math.PI * 2);
  ctx.fill();

  // тело
  ctx.beginPath();
  ctx.arc(e.x, e.y, e.r, 0, Math.PI * 2);
  ctx.fillStyle = '#8a1c26';
  ctx.fill();
  ctx.strokeStyle = '#f85149';
  ctx.lineWidth = 2;
  ctx.stroke();

  // мишень
  ctx.beginPath();
  ctx.arc(e.x, e.y, e.r * 0.68, 0, Math.PI * 2);
  ctx.strokeStyle = 'rgba(255,180,180,0.65)';
  ctx.lineWidth = 1.4;
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(e.x, e.y, e.r * 0.35, 0, Math.PI * 2);
  ctx.fillStyle = '#ff9aa4';
  ctx.fill();

  ctx.beginPath();
  ctx.arc(e.x, e.y, 2.4, 0, Math.PI * 2);
  ctx.fillStyle = '#2b0609';
  ctx.fill();

  // "перекрестие" по краям
  ctx.strokeStyle = 'rgba(248,81,73,0.5)';
  ctx.lineWidth = 1.2;
  const t = e.r + 6, L = 6;
  ctx.beginPath();
  ctx.moveTo(e.x, e.y - t); ctx.lineTo(e.x, e.y - t - L);
  ctx.moveTo(e.x, e.y + t); ctx.lineTo(e.x, e.y + t + L);
  ctx.moveTo(e.x - t, e.y); ctx.lineTo(e.x - t - L, e.y);
  ctx.moveTo(e.x + t, e.y); ctx.lineTo(e.x + t + L, e.y);
  ctx.stroke();
}

function drawTurtle(p) {
  const r = 12;
  const ang = p.angle * Math.PI / 180;
  const ax = Math.cos(ang), ay = Math.sin(ang);

  // свечение
  const grd = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 2.4);
  grd.addColorStop(0, 'rgba(63,185,80,0.30)');
  grd.addColorStop(1, 'rgba(63,185,80,0)');
  ctx.fillStyle = grd;
  ctx.beginPath();
  ctx.arc(p.x, p.y, r * 2.4, 0, Math.PI * 2);
  ctx.fill();

  // направление
  ctx.beginPath();
  ctx.moveTo(p.x + ax * (r + 4), p.y + ay * (r + 4));
  ctx.lineTo(p.x + ax * (r + 26), p.y + ay * (r + 26));
  ctx.strokeStyle = 'rgba(166,255,185,0.75)';
  ctx.lineWidth = 2;
  ctx.lineCap = 'round';
  ctx.stroke();

  // стрелка на конце
  const tipX = p.x + ax * (r + 26);
  const tipY = p.y + ay * (r + 26);
  const perpX = -ay, perpY = ax;
  ctx.beginPath();
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(tipX - ax * 7 + perpX * 4, tipY - ay * 7 + perpY * 4);
  ctx.lineTo(tipX - ax * 7 - perpX * 4, tipY - ay * 7 - perpY * 4);
  ctx.closePath();
  ctx.fillStyle = 'rgba(166,255,185,0.85)';
  ctx.fill();

  // панцирь (шестиугольник)
  ctx.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = ang + i * Math.PI / 3;
    const x = p.x + Math.cos(a) * r;
    const y = p.y + Math.sin(a) * r;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.closePath();
  const shell = ctx.createLinearGradient(p.x, p.y - r, p.x, p.y + r);
  shell.addColorStop(0, '#4ddf7c');
  shell.addColorStop(1, '#1f7c3f');
  ctx.fillStyle = shell;
  ctx.fill();
  ctx.strokeStyle = '#a6ffb9';
  ctx.lineWidth = 1.8;
  ctx.stroke();

  // центр
  ctx.beginPath();
  ctx.arc(p.x, p.y, 3.2, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(10,30,15,0.75)';
  ctx.fill();
}

/* ============================================================
   Победа — вспышка на канвасе
============================================================ */
function flashVictory() {
  const el = document.createElement('div');
  el.className = 'flash';
  canvasBody.style.position = 'relative';
  canvasBody.appendChild(el);
  setTimeout(() => el.remove(), 700);
}

/* ============================================================
   Кнопки
============================================================ */
document.getElementById('run').onclick   = runShot;
document.getElementById('regen').onclick = async () => {
  const r = await fetch(`/api/room/${state.roomId}/regen`, { method: 'POST' });
  state.room = await r.json();
  state.preview = null;
  state.run = null;
  stopAnim();
  setStatus('info', `Комната ${state.roomId} пересоздана`, `Препятствий: ${state.room.obstacles.length}`);
  logOut([['k', '# Уровень пересоздан'], ['', `Стен: ${state.room.obstacles.length}`]]);
  outChip.textContent = '—';
  outChip.className = 'chip';
  draw();
  schedulePreview(0);
};
document.getElementById('next').onclick = () => {
  loadRoom(String(parseInt(state.roomId, 10) + 1));
};

/* чипы-подсказки вставляют команду */
document.querySelectorAll('.hint').forEach(el => {
  el.addEventListener('click', () => {
    const cmd = el.getAttribute('data-cmd');
    const v = codeEl.value;
    const sep = v.endsWith('\n') || v.length === 0 ? '' : '\n';
    codeEl.value = v + sep + cmd + '\n';
    codeEl.focus();
    codeEl.selectionStart = codeEl.selectionEnd = codeEl.value.length;
    refreshEditor();
    schedulePreview(0);
  });
});

/* ============================================================
   Bootstrap
============================================================ */
const DEFAULT_CODE = `# Проведи черепашку к врагу, не задев стены
forward 200
right 90
forward 150
left 90
forward 400`;

codeEl.value = DEFAULT_CODE;
refreshEditor();

loadRoom('1').then(() => {
  // первичное превью
  schedulePreview(50);
});

// слегка перерисовываем canvas при ресайзе окна (для чёткости)
window.addEventListener('resize', () => { /* canvas масштабируется через CSS */ });
</script>
</body>
</html>
"""


# ============================================================
#                          ЗАПУСК
# ============================================================

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
