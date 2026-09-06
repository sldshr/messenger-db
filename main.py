import asyncio
import json
import math
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

HOST = "0.0.0.0"
PORT = 8000
TICK_RATE = 30
MAX_PLAYERS = 16
PLAYER_SPEED = 6.0
MAX_HEALTH = 100
SHOT_COOLDOWN = 0.09

app = FastAPI(title="Python FPS Server")


@dataclass
class Player:
    id: str
    name: str
    x: float = 0.0
    y: float = 1.0
    z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    health: int = MAX_HEALTH
    last_shot: float = 0.0


class GameServer:
    def __init__(self):
        self.clients: Dict[str, WebSocket] = {}
        self.players: Dict[str, Player] = {}
        self.lock = asyncio.Lock()

    async def add(self, websocket: WebSocket, name: str):
        async with self.lock:
            if len(self.clients) >= MAX_PLAYERS:
                await websocket.close(code=1013, reason="Server full")
                return None

            player_id = uuid.uuid4().hex[:8]
            safe_name = (name or "Player")[:24]
            player = Player(
                id=player_id,
                name=safe_name,
                x=(len(self.players) % 4) * 2.0,
                y=1.0,
                z=(len(self.players) // 4) * 2.0,
            )
            self.clients[player_id] = websocket
            self.players[player_id] = player

        await self.send(player_id, {
            "type": "welcome",
            "id": player_id,
            "player": asdict(player),
            "players": [asdict(p) for p in self.players.values()],
        })
        await self.broadcast({
            "type": "player_joined",
            "player": asdict(player),
        }, exclude=player_id)
        return player_id

    async def remove(self, player_id: str):
        async with self.lock:
            self.clients.pop(player_id, None)
            self.players.pop(player_id, None)
        await self.broadcast({"type": "player_left", "id": player_id})

    async def send(self, player_id: str, payload: dict):
        ws = self.clients.get(player_id)
        if not ws:
            return
        try:
            await ws.send_text(json.dumps(payload, separators=(",", ":")))
        except Exception:
            pass

    async def broadcast(self, payload: dict, exclude: str | None = None):
        text = json.dumps(payload, separators=(",", ":"))
        dead = []
        for pid, ws in list(self.clients.items()):
            if pid == exclude:
                continue
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(pid)
        for pid in dead:
            await self.remove(pid)

    async def handle(self, player_id: str, msg: dict):
        player = self.players.get(player_id)
        if not player:
            return

        msg_type = msg.get("type")

        if msg_type == "state":
            # Prototype server: movement is client-driven, server clamps obvious nonsense.
            x = float(msg.get("x", player.x))
            y = float(msg.get("y", player.y))
            z = float(msg.get("z", player.z))
            yaw = float(msg.get("yaw", player.yaw))
            pitch = float(msg.get("pitch", player.pitch))

            distance = math.sqrt(
                (x - player.x) ** 2 +
                (y - player.y) ** 2 +
                (z - player.z) ** 2
            )
            # 30Hz * speed plus tolerance. Prevent giant teleports.
            max_distance = PLAYER_SPEED / TICK_RATE * 3.0
            if distance <= max_distance:
                player.x, player.y, player.z = x, y, z

            player.yaw = yaw
            player.pitch = max(-89.0, min(89.0, pitch))

        elif msg_type == "shoot":
            now = time.monotonic()
            if now - player.last_shot < SHOT_COOLDOWN:
                return
            player.last_shot = now

            # Client sends a hit candidate. This is intentionally prototype-level.
            target_id = str(msg.get("target_id", ""))
            damage = max(0, min(100, int(msg.get("damage", 25))))

            target = self.players.get(target_id)
            if target and target_id != player_id and target.health > 0:
                target.health = max(0, target.health - damage)
                await self.broadcast({
                    "type": "hit",
                    "attacker": player_id,
                    "target": target_id,
                    "health": target.health,
                    "damage": damage,
                })

                if target.health <= 0:
                    await self.broadcast({
                        "type": "kill",
                        "attacker": player_id,
                        "target": target_id,
                    })
                    await asyncio.sleep(2.0)
                    if target_id in self.players:
                        target.health = MAX_HEALTH
                        target.x = 0.0
                        target.y = 1.0
                        target.z = 0.0
                        await self.broadcast({
                            "type": "respawn",
                            "player": asdict(target),
                        })

    async def snapshot_loop(self):
        delay = 1.0 / TICK_RATE
        while True:
            await asyncio.sleep(delay)
            if not self.clients:
                continue
            snapshot = {
                "type": "snapshot",
                "players": [asdict(p) for p in self.players.values()],
                "server_time": time.time(),
            }
            await self.broadcast(snapshot)


game = GameServer()


@app.get("/")
async def root():
    return {
        "name": "Python FPS Server",
        "status": "online",
        "players": len(game.players),
    }


@app.get("/status")
async def status():
    return {
        "online": True,
        "players": len(game.players),
        "max_players": MAX_PLAYERS,
        "tick_rate": TICK_RATE,
    }


@app.on_event("startup")
async def startup():
    asyncio.create_task(game.snapshot_loop())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    player_id = None
    try:
        raw = await websocket.receive_text()
        hello = json.loads(raw)

        if hello.get("type") != "hello":
            await websocket.close(code=1002, reason="Expected hello")
            return

        player_id = await game.add(websocket, hello.get("name", "Player"))
        if not player_id:
            return

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            await game.handle(player_id, msg)

    except WebSocketDisconnect:
        if player_id:
            await game.remove(player_id)
    except Exception:
        if player_id:
            await game.remove(player_id)
