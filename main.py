# main.py
# 🐢 Черепашка-Снайпер — мини-игра в стиле Graphwar на черепашьих командах.
# Запуск:  python main.py   →   http://127.0.0.1:8000

import math
import random
import re
from collections import deque

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Turtle Graphwar")

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
        # запасной вариант — пустая комната
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

MAX_STEPS = 20000  # защита от бесконечности


def parse_commands(text: str):
    steps = []
    if not text:
        return steps
    parts = re.split(r"[\n,;]+", text)
    for p in parts:
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
    """Прогоняем команды черепашки по шагам, проверяя столкновения."""
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
            n = max(1, int(length / 2.0))     # шаг ~2 пикселя
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
<title>🐢 Черепашка-Снайпер</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; background: #0b0e14; color: #e8ecf1;
       font-family: ui-sans-serif, system-ui, "Segoe UI", sans-serif; }
  .wrap { display: flex; gap: 16px; padding: 16px; align-items: flex-start; flex-wrap: wrap; }
  canvas { background: #0f141c; border: 1px solid #1e2633; border-radius: 10px; display: block; }
  .panel { background: #0f141c; border: 1px solid #1e2633; border-radius: 10px;
           padding: 16px; width: 360px; }
  h1 { margin: 0 0 8px; font-size: 20px; }
  .muted { color: #8ea3c9; font-size: 13px; line-height: 1.5; }
  textarea { width: 100%; height: 180px; background: #0b0e14; color: #d9e2ef;
             border: 1px solid #263041; border-radius: 8px; padding: 10px;
             font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 14px;
             resize: vertical; outline: none; }
  textarea:focus { border-color: #4a6fa5; }
  button { background: #1c2740; color: #dfe8f5; border: 1px solid #2b3a58;
           border-radius: 8px; padding: 8px 12px; cursor: pointer; font-size: 14px;
           margin-right: 6px; margin-top: 8px; transition: background .15s; }
  button:hover { background: #243252; }
  button.primary { background: #2f6b46; border-color: #3d8f5c; }
  button.primary:hover { background: #388054; }
  #status { margin-top: 10px; padding: 8px 12px; border-radius: 8px; background: #0f141c;
            border: 1px solid #1e2633; font-size: 14px; min-height: 38px; }
  .ok   { color: #5ff59a; border-color: #2f6b46 !important; }
  .bad  { color: #ff7c88; border-color: #6b2f3a !important; }
  .warn { color: #ffc46b; border-color: #6b552f !important; }
  pre { background: #0b0e14; border: 1px solid #1e2633; border-radius: 8px;
        padding: 10px; font-size: 12px; margin-top: 10px; white-space: pre-wrap; }
  kbd { background: #1c2740; border-radius: 4px; padding: 1px 6px; font-size: 12px;
        border: 1px solid #2b3a58; }
</style>
</head>
<body>
<div class="wrap">
  <div>
    <canvas id="cv" width="900" height="520"></canvas>
    <div id="status">Загрузка…</div>
  </div>
  <div class="panel">
    <h1>🐢 Черепашка-Снайпер</h1>
    <p class="muted">
      Пиши команды для черепашки и проведи её сквозь препятствия прямо во врага.
      ИИ генерирует уровень, расставляет стены и врага так, чтобы путь существовал.
    </p>
    <p class="muted">
      <kbd>forward 50</kbd> — вперёд<br>
      <kbd>backward 20</kbd> — назад<br>
      <kbd>right 90</kbd> — поворот направо<br>
      <kbd>left 45</kbd> — поворот налево<br>
      (синонимы: fd, bk, rt, lt; можно через запятую)
    </p>
    <textarea id="cmds">forward 200
right 90
forward 150
left 90
forward 400</textarea>
    <div>
      <button class="primary" id="run">🚀 Запустить</button>
      <button id="regen">🎲 Новый уровень</button>
      <button id="next">➡️ Следующая комната</button>
    </div>
    <pre id="log">Комната 1</pre>
  </div>
</div>

<script>
const cv = document.getElementById('cv');
const ctx = cv.getContext('2d');
const statusEl = document.getElementById('status');
const logEl = document.getElementById('log');
const cmdsEl = document.getElementById('cmds');

let roomId = '1';
let room = null;
let result = null;
let animIdx = 0;
let animTimer = null;

async function loadRoom(id) {
  roomId = String(id);
  const r = await fetch(`/api/room/${roomId}`);
  room = await r.json();
  result = null;
  animIdx = 0;
  if (animTimer) { clearInterval(animTimer); animTimer = null; }
  statusEl.textContent = `Комната ${roomId} · сложность ${room.difficulty}`;
  statusEl.className = '';
  logEl.textContent = `Комната ${roomId}. ИИ сгенерировал уровень: препятствий — ${room.obstacles.length}.`;
  draw();
}

function draw() {
  ctx.fillStyle = '#0f141c';
  ctx.fillRect(0, 0, cv.width, cv.height);

  // сетка
  ctx.strokeStyle = '#151c27';
  ctx.lineWidth = 1;
  for (let x = 0; x <= cv.width; x += 50) {
    ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, cv.height); ctx.stroke();
  }
  for (let y = 0; y <= cv.height; y += 50) {
    ctx.beginPath(); ctx.moveTo(0, y + 0.5); ctx.lineTo(cv.width, y + 0.5); ctx.stroke();
  }

  if (!room) return;

  // препятствия
  for (const o of room.obstacles) {
    ctx.fillStyle = '#2a3244';
    ctx.fillRect(o.x, o.y, o.w, o.h);
    ctx.strokeStyle = '#4b5872';
    ctx.lineWidth = 1;
    ctx.strokeRect(o.x + 0.5, o.y + 0.5, o.w - 1, o.h - 1);
  }

  // траектория
  if (result && result.path && result.path.length > 1) {
    const colors = {
      success: '#5ff59a',
      hit_obstacle: '#ff7c88',
      out_of_bounds: '#ffc46b',
      out_of_commands: '#8ea3c9',
      too_long: '#8ea3c9',
    };
    const col = colors[result.status] || '#8ea3c9';
    const path = result.path;
    const end = Math.min(animIdx, path.length - 1);

    // призрак полного пути
    ctx.beginPath();
    ctx.moveTo(path[0][0], path[0][1]);
    for (let i = 1; i < path.length; i++) ctx.lineTo(path[i][0], path[i][1]);
    ctx.strokeStyle = 'rgba(142,163,201,0.18)';
    ctx.lineWidth = 1;
    ctx.stroke();

    // пройденный путь
    ctx.beginPath();
    ctx.moveTo(path[0][0], path[0][1]);
    for (let i = 1; i <= end; i++) ctx.lineTo(path[i][0], path[i][1]);
    ctx.strokeStyle = col;
    ctx.lineWidth = 2.5;
    ctx.stroke();

    // голова
    const head = path[end];
    ctx.beginPath();
    ctx.arc(head[0], head[1], 6, 0, Math.PI * 2);
    ctx.fillStyle = col;
    ctx.fill();
  }

  // враг
  const e = room.enemy;
  ctx.beginPath();
  ctx.arc(e.x, e.y, e.r, 0, Math.PI * 2);
  ctx.fillStyle = '#e14b5c';
  ctx.fill();
  ctx.strokeStyle = '#ff9aa4';
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(e.x, e.y, e.r * 0.45, 0, Math.PI * 2);
  ctx.fillStyle = '#1a0308';
  ctx.fill();
  ctx.beginPath();
  ctx.arc(e.x + e.r * 0.15, e.y - e.r * 0.1, e.r * 0.18, 0, Math.PI * 2);
  ctx.fillStyle = '#ff7c88';
  ctx.fill();

  // черепашка на старте
  const p = room.player_start;
  ctx.beginPath();
  ctx.arc(p.x, p.y, 12, 0, Math.PI * 2);
  ctx.fillStyle = '#4ddf7c';
  ctx.fill();
  ctx.strokeStyle = '#a6ffb9';
  ctx.lineWidth = 2;
  ctx.stroke();
  // направление
  const ax = Math.cos(p.angle * Math.PI / 180);
  const ay = Math.sin(p.angle * Math.PI / 180);
  ctx.beginPath();
  ctx.moveTo(p.x, p.y);
  ctx.lineTo(p.x + ax * 22, p.y + ay * 22);
  ctx.strokeStyle = '#a6ffb9';
  ctx.lineWidth = 2;
  ctx.stroke();
}

function startAnimation() {
  if (animTimer) clearInterval(animTimer);
  animIdx = 0;
  if (!result) return;
  const total = result.path.length;
  if (total <= 1) { draw(); return; }
  const speed = Math.max(1, Math.floor(total / 120)); // ~120 кадров
  animTimer = setInterval(() => {
    animIdx += speed;
    if (animIdx >= total - 1) {
      animIdx = total - 1;
      clearInterval(animTimer);
      animTimer = null;
    }
    draw();
  }, 16);
}

async function shoot() {
  if (!room) return;
  const r = await fetch(`/api/room/${roomId}/shoot`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ commands: cmdsEl.value }),
  });
  result = await r.json();

  const messages = {
    success: ['🎯 Попадание! Враг поражён.', 'ok'],
    hit_obstacle: ['💥 Черепашка врезалась в препятствие.', 'bad'],
    out_of_bounds: ['🚧 Черепашка вышла за границы поля.', 'warn'],
    out_of_commands: ['🐢 Команды закончились, цель не достигнута.', ''],
    too_long: ['⏳ Слишком много шагов.', 'warn'],
  };
  const [msg, cls] = messages[result.status] || ['Готово', ''];
  statusEl.textContent = msg;
  statusEl.className = cls;
  logEl.textContent =
    `Статус: ${result.status}\n` +
    `Точек траектории: ${result.path.length}\n` +
    `Команд: ${result.steps}`;
  startAnimation();
}

document.getElementById('run').onclick = shoot;
document.getElementById('regen').onclick = async () => {
  const r = await fetch(`/api/room/${roomId}/regen`, { method: 'POST' });
  room = await r.json();
  result = null;
  animIdx = 0;
  if (animTimer) { clearInterval(animTimer); animTimer = null; }
  statusEl.textContent = `Комната ${roomId} пересоздана`;
  statusEl.className = '';
  logEl.textContent = `Новый уровень: препятствий — ${room.obstacles.length}`;
  draw();
};
document.getElementById('next').onclick = () => {
  loadRoom(String(parseInt(roomId, 10) + 1));
};

loadRoom('1');
</script>
</body>
</html>
"""


# ============================================================
#                          ЗАПУСК
# ============================================================

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
