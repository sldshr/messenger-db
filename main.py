import asyncio
import json
import math
import random
import time
import uuid
from typing import Optional, Dict, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

# ---------- Константы поля и игры ----------
FIELD_W, FIELD_H = 800, 500
BALL_R = 8
PADDLE_W, PADDLE_H = 12, 80
PADDLE_X = 30
WIN_SCORE = 8
WARMUP_SEC = 60
COUNTDOWN_SEC = 5
TICK = 1.0 / 60.0
BALL_SPEED = 350
BALL_MAX_SPEED = 800

app = FastAPI()

# ---------- Лидерборд (в оперативке) ----------
LEADERBOARD: Dict[str, dict] = {}


def lb_get(nick: str) -> dict:
    if nick not in LEADERBOARD:
        LEADERBOARD[nick] = {
            "wins_pvp": 0, "losses_pvp": 0,
            "wins_bot_easy": 0, "losses_bot_easy": 0,
            "wins_bot_medium": 0, "losses_bot_medium": 0,
            "wins_bot_hard": 0, "losses_bot_hard": 0,
        }
    return LEADERBOARD[nick]


# ---------- Сессия подключения ----------
class Session:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.id = str(uuid.uuid4())
        self.nick: Optional[str] = None
        self.game: Optional["Game"] = None
        self.role: Optional[str] = None  # "left" | "right"
        self.in_queue = False
        self.paddle_target = FIELD_H / 2


# ---------- Игра ----------
class Game:
    def __init__(self, s1: Session, s2: Optional[Session], bot: Optional[str] = None):
        self.s1 = s1
        self.s2 = s2
        self.bot = bot  # None | "easy" | "medium" | "hard"
        self.paddle1_y = FIELD_H / 2
        self.paddle2_y = FIELD_H / 2
        self.score1 = 0
        self.score2 = 0
        self.phase = "warmup"  # warmup -> countdown -> playing -> finished
        self.phase_start = time.time()
        self.running = True
        self.winner: Optional[str] = None  # "left" | "right" | None

        self.ball_x = FIELD_W / 2
        self.ball_y = FIELD_H / 2
        self.ball_vx = 0.0
        self.ball_vy = 0.0
        self.ball_frozen_until: Optional[float] = None
        self.ball_pending_vx = 0.0
        self.ball_pending_vy = 0.0
        self._kick_ball(random.choice([-1, 1]), freeze=0.4)

    # --- служебное ---
    def _kick_ball(self, direction: int, freeze: float = 0.5):
        self.ball_x = FIELD_W / 2
        self.ball_y = FIELD_H / 2
        angle = random.uniform(-0.4, 0.4)
        vx = direction * BALL_SPEED * math.cos(angle)
        vy = BALL_SPEED * math.sin(angle)
        self.ball_vx = 0.0
        self.ball_vy = 0.0
        self.ball_pending_vx = vx
        self.ball_pending_vy = vy
        self.ball_frozen_until = time.time() + freeze

    def _freeze_ball(self):
        self.ball_x = FIELD_W / 2
        self.ball_y = FIELD_H / 2
        self.ball_vx = 0.0
        self.ball_vy = 0.0
        self.ball_pending_vx = 0.0
        self.ball_pending_vy = 0.0
        self.ball_frozen_until = float("inf")

    def launch_ball(self):
        self._kick_ball(random.choice([-1, 1]), freeze=0.35)

    def finish(self, winner: str):
        self.winner = winner
        self.phase = "finished"
        self.phase_start = time.time()

    # --- физика ---
    def update_ball(self, dt: float):
        now = time.time()
        if self.ball_frozen_until is not None:
            if now < self.ball_frozen_until:
                return
            self.ball_vx = self.ball_pending_vx
            self.ball_vy = self.ball_pending_vy
            self.ball_frozen_until = None

        self.ball_x += self.ball_vx * dt
        self.ball_y += self.ball_vy * dt

        # стены
        if self.ball_y - BALL_R < 0:
            self.ball_y = BALL_R
            self.ball_vy = abs(self.ball_vy)
        if self.ball_y + BALL_R > FIELD_H:
            self.ball_y = FIELD_H - BALL_R
            self.ball_vy = -abs(self.ball_vy)

        left_px = PADDLE_X
        right_px = FIELD_W - PADDLE_X - PADDLE_W

        # левая ракетка
        if (self.ball_vx < 0 and
                self.ball_x - BALL_R <= left_px + PADDLE_W and
                self.ball_x + BALL_R >= left_px and
                self.paddle1_y - PADDLE_H / 2 <= self.ball_y <= self.paddle1_y + PADDLE_H / 2):
            self.ball_x = left_px + PADDLE_W + BALL_R
            rel = max(-1.0, min(1.0, (self.ball_y - self.paddle1_y) / (PADDLE_H / 2)))
            speed = min(math.hypot(self.ball_vx, self.ball_vy) * 1.03, BALL_MAX_SPEED)
            angle = rel * (math.pi / 4)
            self.ball_vx = speed * math.cos(angle)
            self.ball_vy = speed * math.sin(angle)

        # правая ракетка
        if (self.ball_vx > 0 and
                self.ball_x + BALL_R >= right_px and
                self.ball_x - BALL_R <= right_px + PADDLE_W and
                self.paddle2_y - PADDLE_H / 2 <= self.ball_y <= self.paddle2_y + PADDLE_H / 2):
            self.ball_x = right_px - BALL_R
            rel = max(-1.0, min(1.0, (self.ball_y - self.paddle2_y) / (PADDLE_H / 2)))
            speed = min(math.hypot(self.ball_vx, self.ball_vy) * 1.03, BALL_MAX_SPEED)
            angle = rel * (math.pi / 4)
            self.ball_vx = -speed * math.cos(angle)
            self.ball_vy = speed * math.sin(angle)

        # голы
        if self.ball_x < -BALL_R:
            if self.phase == "playing":
                self.score2 += 1
                if self.score2 >= WIN_SCORE:
                    self.finish("right")
                    return
            self._kick_ball(1)
        elif self.ball_x > FIELD_W + BALL_R:
            if self.phase == "playing":
                self.score1 += 1
                if self.score1 >= WIN_SCORE:
                    self.finish("left")
                    return
            self._kick_ball(-1)

    def update_bot(self, dt: float):
        if self.bot == "easy":
            speed, err = 220, 60
        elif self.bot == "medium":
            speed, err = 380, 25
        else:
            speed, err = 550, 8

        target_y = self.ball_y + random.uniform(-err, err)
        diff = target_y - self.paddle2_y
        max_move = speed * dt
        if abs(diff) <= max_move:
            self.paddle2_y = target_y
        else:
            self.paddle2_y += math.copysign(max_move, diff)
        self.paddle2_y = max(PADDLE_H / 2, min(FIELD_H - PADDLE_H / 2, self.paddle2_y))

    def state_dict(self) -> dict:
        return {
            "type": "state",
            "field": [FIELD_W, FIELD_H],
            "ball": [self.ball_x, self.ball_y, BALL_R],
            "paddle_w": PADDLE_W,
            "paddle_h": PADDLE_H,
            "paddle1_y": self.paddle1_y,
            "paddle2_y": self.paddle2_y,
            "score1": self.score1,
            "score2": self.score2,
            "phase": self.phase,
            "phase_elapsed": time.time() - self.phase_start,
            "warmup_sec": WARMUP_SEC,
            "countdown_sec": COUNTDOWN_SEC,
            "win_score": WIN_SCORE,
            "winner": self.winner,
        }


# ---------- Игровой цикл ----------
async def game_loop(game: Game):
    last = time.time()
    while game.running:
        now = time.time()
        dt = min(now - last, 0.1)
        last = now

        # фазы
        if game.phase == "warmup":
            if now - game.phase_start >= WARMUP_SEC:
                game.phase = "countdown"
                game.phase_start = now
                game._freeze_ball()
        elif game.phase == "countdown":
            if now - game.phase_start >= COUNTDOWN_SEC:
                game.phase = "playing"
                game.phase_start = now
                game.launch_ball()

        # ракетки
        if game.s1 and game.s1.game is game:
            game.paddle1_y = game.s1.paddle_target
        if game.s2 and game.s2.game is game:
            game.paddle2_y = game.s2.paddle_target
        elif game.bot:
            game.update_bot(dt)

        game.paddle1_y = max(PADDLE_H / 2, min(FIELD_H - PADDLE_H / 2, game.paddle1_y))
        game.paddle2_y = max(PADDLE_H / 2, min(FIELD_H - PADDLE_H / 2, game.paddle2_y))

        if game.phase in ("warmup", "playing"):
            game.update_ball(dt)

        # рассылка состояния
        msg = json.dumps(game.state_dict())
        for s in (game.s1, game.s2):
            if s:
                try:
                    await s.ws.send_text(msg)
                except Exception:
                    game.running = False

        if game.phase == "finished":
            break
        if not game.running:
            break

        await asyncio.sleep(TICK)

    await end_game(game)


async def end_game(game: Game):
    # лидерборд
    if game.winner:
        if game.bot:
            human = game.s1
            if human and human.nick:
                lb = lb_get(human.nick)
                if game.winner == "left":
                    lb[f"wins_bot_{game.bot}"] += 1
                else:
                    lb[f"losses_bot_{game.bot}"] += 1
        else:
            w, l = (game.s1, game.s2) if game.winner == "left" else (game.s2, game.s1)
            if w and w.nick:
                lb_get(w.nick)["wins_pvp"] += 1
            if l and l.nick:
                lb_get(l.nick)["losses_pvp"] += 1

    for s in (game.s1, game.s2):
        if not s:
            continue
        try:
            await s.ws.send_text(json.dumps({
                "type": "game_over",
                "winner": game.winner,
                "your_role": s.role,
                "score1": game.score1,
                "score2": game.score2,
            }))
        except Exception:
            pass
        if s.game is game:
            s.game = None
            s.role = None


# ---------- Matchmaking ----------
pvp_queue: List[Session] = []


def build_leaderboard() -> list:
    rows = []
    for nick, d in LEADERBOARD.items():
        total = (d["wins_pvp"] + d["wins_bot_easy"]
                 + d["wins_bot_medium"] + d["wins_bot_hard"])
        rows.append({
            "nick": nick,
            "wins_pvp": d["wins_pvp"],
            "wins_bot_easy": d["wins_bot_easy"],
            "wins_bot_medium": d["wins_bot_medium"],
            "wins_bot_hard": d["wins_bot_hard"],
            "total": total,
        })
    rows.sort(key=lambda r: -r["total"])
    return rows


# ---------- WebSocket ----------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    sess = Session(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")

            if t == "set_nick":
                nick = str(msg.get("nick", "")).strip()[:20]
                if not nick:
                    await ws.send_text(json.dumps({"type": "error", "msg": "Ник не может быть пустым"}))
                    continue
                sess.nick = nick
                lb_get(nick)
                await ws.send_text(json.dumps({"type": "nick_ok", "nick": nick}))

            elif t == "queue_pvp":
                if sess.game or not sess.nick:
                    continue
                # чистим мёртвые сессии
                pvp_queue[:] = [s for s in pvp_queue if s.game is None and s.nick]
                if sess in pvp_queue:
                    pvp_queue.remove(sess)
                if pvp_queue:
                    opp = pvp_queue.pop(0)
                    game = Game(opp, sess, bot=None)
                    opp.game = game
                    opp.role = "left"
                    opp.paddle_target = FIELD_H / 2
                    sess.game = game
                    sess.role = "right"
                    sess.paddle_target = FIELD_H / 2
                    try:
                        await opp.ws.send_text(json.dumps({
                            "type": "match_start", "role": "left",
                            "opponent": sess.nick, "is_bot": False,
                        }))
                        await ws.send_text(json.dumps({
                            "type": "match_start", "role": "right",
                            "opponent": opp.nick, "is_bot": False,
                        }))
                    except Exception:
                        pass
                    asyncio.create_task(game_loop(game))
                else:
                    sess.in_queue = True
                    pvp_queue.append(sess)
                    await ws.send_text(json.dumps({"type": "queue_status", "in_queue": True}))

            elif t == "cancel_queue":
                if sess in pvp_queue:
                    pvp_queue.remove(sess)
                sess.in_queue = False
                await ws.send_text(json.dumps({"type": "queue_status", "in_queue": False}))

            elif t == "start_bot":
                if sess.game or not sess.nick:
                    continue
                diff = msg.get("difficulty", "easy")
                if diff not in ("easy", "medium", "hard"):
                    diff = "easy"
                game = Game(sess, None, bot=diff)
                sess.game = game
                sess.role = "left"
                sess.paddle_target = FIELD_H / 2
                await ws.send_text(json.dumps({
                    "type": "match_start", "role": "left",
                    "opponent": f"БОТ [{diff}]", "is_bot": True, "difficulty": diff,
                }))
                asyncio.create_task(game_loop(game))

            elif t == "paddle":
                if sess.game:
                    try:
                        sess.paddle_target = float(msg.get("y", FIELD_H / 2))
                    except Exception:
                        pass

            elif t == "leave":
                if sess in pvp_queue:
                    pvp_queue.remove(sess)
                if sess.game:
                    sess.game.winner = None
                    sess.game.running = False
                    sess.game = None
                    sess.role = None

            elif t == "get_leaderboard":
                await ws.send_text(json.dumps({
                    "type": "leaderboard",
                    "data": build_leaderboard(),
                }))

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if sess in pvp_queue:
            pvp_queue.remove(sess)
        if sess.game:
            sess.game.winner = None
            sess.game.running = False
        sess.game = None


# ---------- HTML-клиент ----------
PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>PONG</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #0a0a0a; color: #d8d8d8;
    font-family: "Courier New", monospace;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh;
  }
  .screen { display: none; padding: 20px; text-align: center; }
  .screen.active { display: block; }
  h1 { color: #fff; letter-spacing: 6px; font-weight: normal; margin: 0 0 24px; }
  h2 { color: #fff; letter-spacing: 3px; font-weight: normal; }
  button {
    background: #111; color: #eee; border: 2px solid #444;
    padding: 10px 18px; font-family: inherit; font-size: 15px;
    margin: 4px; cursor: pointer; letter-spacing: 1px;
  }
  button:hover { background: #1e1e1e; border-color: #888; }
  button:active { background: #333; }
  input {
    background: #111; color: #eee; border: 2px solid #444;
    padding: 9px 12px; font-family: inherit; font-size: 15px;
    outline: none;
  }
  input:focus { border-color: #888; }
  canvas { border: 2px solid #333; background: #000; display: block; max-width: 95vw; max-height: 70vh; }
  .row { margin: 10px 0; }
  table { margin: 0 auto 16px; border-collapse: collapse; font-size: 14px; }
  th, td { border: 1px solid #333; padding: 6px 12px; }
  th { background: #161616; color: #fff; }
  tr:nth-child(even) td { background: #0f0f0f; }
  .hint { color: #666; font-size: 12px; margin-top: 16px; }
  .hidden { display: none !important; }
  #game-info { margin-bottom: 8px; color: #aaa; font-size: 14px; }
</style>
</head>
<body>

<div id="menu" class="screen active">
  <h1>PONG</h1>
  <div class="row">
    <input id="nick" placeholder="Введите ник" maxlength="20" autocomplete="off">
    <button id="setnick">ОК</button>
  </div>
  <div id="menu-options" class="hidden">
    <div class="row"><button id="btn-pvp">Играть с игроком</button></div>
    <div class="row">
      <span>Против бота:</span>
      <button class="btn-bot" data-diff="easy">Лёгкий</button>
      <button class="btn-bot" data-diff="medium">Средний</button>
      <button class="btn-bot" data-diff="hard">Сложный</button>
    </div>
    <div class="row"><button id="btn-lb">Лидерборд</button></div>
  </div>
  <div class="hint">Управление — мышью по полю. До 8 очков. Перед матчем — 60 сек разминки.</div>
</div>

<div id="queue" class="screen">
  <h1>ПОИСК СОПЕРНИКА</h1>
  <p>Ожидание игрока...</p>
  <button id="btn-cancel">Отмена</button>
</div>

<div id="lobby" class="screen">
  <h1>ЛИДЕРБОРД</h1>
  <div id="lb-content"></div>
  <button id="btn-lb-back">Назад</button>
</div>

<div id="game" class="screen">
  <div id="game-info"></div>
  <canvas id="canvas" width="800" height="500"></canvas>
  <div style="margin-top:8px;"><button id="btn-leave">Покинуть</button></div>
</div>

<div id="gameover" class="screen">
  <h1 id="go-title">—</h1>
  <p id="go-info"></p>
  <button id="btn-menu-back">В меню</button>
</div>

<script>
const $ = (id) => document.getElementById(id);
let ws = null, nick = null, role = null, gameState = null;

function showScreen(id) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
  $(id).classList.add("active");
}

function send(obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => {
    const saved = localStorage.getItem("nick") || $("nick").value.trim();
    if (saved) send({ type: "set_nick", nick: saved });
  };
  ws.onmessage = (e) => {
    try { handleMessage(JSON.parse(e.data)); } catch (err) {}
  };
  ws.onclose = () => setTimeout(connect, 1000);
}

function handleMessage(msg) {
  switch (msg.type) {
    case "nick_ok":
      nick = msg.nick;
      localStorage.setItem("nick", nick);
      $("menu-options").classList.remove("hidden");
      break;
    case "queue_status":
      if (msg.in_queue) showScreen("queue");
      else showScreen("menu");
      break;
    case "match_start":
      role = msg.role;
      gameState = null;
      $("game-info").textContent = `Вы: ${nick}   |   Соперник: ${msg.opponent}`;
      showScreen("game");
      break;
    case "state":
      gameState = msg;
      break;
    case "game_over":
      gameState = null;
      if (msg.winner === null) {
        $("go-title").textContent = "МАТЧ ПРЕРВАН";
        $("go-info").textContent = "Соперник покинул игру.";
      } else {
        const won = (msg.your_role === msg.winner);
        $("go-title").textContent = won ? "ПОБЕДА" : "ПОРАЖЕНИЕ";
        $("go-info").textContent = `Счёт: ${msg.score1} : ${msg.score2}`;
      }
      showScreen("gameover");
      break;
    case "leaderboard":
      renderLeaderboard(msg.data);
      break;
    case "error":
      alert(msg.msg);
      break;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function renderLeaderboard(rows) {
  if (!rows.length) {
    $("lb-content").innerHTML = "<p>Пока пусто.</p>";
    return;
  }
  let html = "<table><tr><th>#</th><th>Ник</th><th>PvP</th><th>Бот Л</th><th>Бот С</th><th>Бот Т</th><th>Всего</th></tr>";
  rows.forEach((r, i) => {
    html += `<tr><td>${i+1}</td><td>${escapeHtml(r.nick)}</td><td>${r.wins_pvp}</td><td>${r.wins_bot_easy}</td><td>${r.wins_bot_medium}</td><td>${r.wins_bot_hard}</td><td>${r.total}</td></tr>`;
  });
  html += "</table>";
  $("lb-content").innerHTML = html;
}

// ---------- Рендер ----------
const canvas = $("canvas");
const ctx = canvas.getContext("2d");

function draw(s) {
  const [W, H] = s.field;
  if (canvas.width !== W) canvas.width = W;
  if (canvas.height !== H) canvas.height = H;

  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, W, H);

  // центральная линия
  ctx.strokeStyle = "#2a2a2a";
  ctx.setLineDash([10, 12]);
  ctx.beginPath();
  ctx.moveTo(W / 2, 0);
  ctx.lineTo(W / 2, H);
  ctx.stroke();
  ctx.setLineDash([]);

  // ракетки
  ctx.fillStyle = "#e8e8e8";
  ctx.fillRect(PADDLE_X_(), s.paddle1_y - s.paddle_h / 2, s.paddle_w, s.paddle_h);
  ctx.fillRect(W - PADDLE_X_() - s.paddle_w, s.paddle2_y - s.paddle_h / 2, s.paddle_w, s.paddle_h);

  // мяч
  ctx.beginPath();
  ctx.arc(s.ball[0], s.ball[1], s.ball[2], 0, Math.PI * 2);
  ctx.fill();

  // счёт
  ctx.fillStyle = "#e8e8e8";
  ctx.font = "48px 'Courier New', monospace";
  ctx.textAlign = "center";
  ctx.fillText(String(s.score1), W / 2 - 60, 64);
  ctx.fillText(String(s.score2), W / 2 + 60, 64);

  // оверлеи
  if (s.phase === "warmup") {
    const remain = Math.max(0, s.warmup_sec - s.phase_elapsed);
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    ctx.fillRect(0, H / 2 - 70, W, 140);
    ctx.fillStyle = "#fff";
    ctx.font = "34px 'Courier New', monospace";
    ctx.fillText("РАЗМИНКА", W / 2, H / 2 - 10);
    ctx.font = "22px 'Courier New', monospace";
    ctx.fillStyle = "#bbb";
    ctx.fillText(`старт через ${Math.ceil(remain)} сек`, W / 2, H / 2 + 35);
  } else if (s.phase === "countdown") {
    const remain = Math.max(0, s.countdown_sec - s.phase_elapsed);
    ctx.fillStyle = "rgba(0,0,0,0.72)";
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = "#fff";
    ctx.font = "140px 'Courier New', monospace";
    ctx.fillText(String(Math.ceil(remain)), W / 2, H / 2 + 50);
    ctx.font = "22px 'Courier New', monospace";
    ctx.fillStyle = "#999";
    ctx.fillText("ПРИГОТОВЬТЕСЬ", W / 2, H / 2 + 110);
  }
}

function PADDLE_X_() { return 30; }

let renderRunning = false;
function renderLoop() {
  renderRunning = true;
  const step = () => {
    if (gameState && $("game").classList.contains("active")) {
      draw(gameState);
    }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// ---------- Управление ----------
canvas.addEventListener("mousemove", (e) => {
  if (!gameState) return;
  const rect = canvas.getBoundingClientRect();
  const y = (e.clientY - rect.top) / rect.height * gameState.field[1];
  send({ type: "paddle", y: y });
});

// ---------- Кнопки меню ----------
$("setnick").onclick = () => {
  const v = $("nick").value.trim();
  if (!v) return;
  send({ type: "set_nick", nick: v });
};

$("nick").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("setnick").click();
});

$("btn-pvp").onclick = () => {
  showScreen("queue");
  send({ type: "queue_pvp" });
};

document.querySelectorAll(".btn-bot").forEach(b => {
  b.onclick = () => send({ type: "start_bot", difficulty: b.dataset.diff });
});

$("btn-cancel").onclick = () => {
  send({ type: "cancel_queue" });
  showScreen("menu");
};

$("btn-lb").onclick = () => {
  send({ type: "get_leaderboard" });
  showScreen("lobby");
};

$("btn-lb-back").onclick = () => showScreen("menu");

$("btn-leave").onclick = () => {
  send({ type: "leave" });
  gameState = null;
  showScreen("menu");
};

$("btn-menu-back").onclick = () => showScreen("menu");

// автостарт
const saved = localStorage.getItem("nick");
if (saved) $("nick").value = saved;
connect();
renderLoop();
</script>
</body>
</html>
"""


@app.get("/")
async def index():
    return HTMLResponse(PAGE)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
