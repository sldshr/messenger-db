# sldchat — single-file messenger
# deps: pip install fastapi uvicorn httpx pydantic
# env:  SUPABASE_URL, SUPABASE_KEY
# run:  python main.py

import asyncio
import hashlib
import os
import secrets
from typing import Optional

import httpx
import uvicorn
from fastapi import (Depends, FastAPI, HTTPException, Query, Request, Response,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ------------------------------------------------------------------ config
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
REST = f"{SUPABASE_URL}/rest/v1"

http = httpx.AsyncClient(timeout=15.0)


def _h(prefer: Optional[str] = None) -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


async def sb_select(table: str, params: dict):
    r = await http.get(f"{REST}/{table}", headers=_h(), params=params)
    if r.status_code >= 300:
        raise HTTPException(500, f"db error: {r.text}")
    return r.json()


async def sb_insert(table: str, data):
    r = await http.post(f"{REST}/{table}", headers=_h("return=representation"), json=data)
    if r.status_code >= 300:
        raise HTTPException(500, f"db error: {r.text}")
    return r.json() if r.text else []


async def sb_update(table: str, params: dict, data: dict):
    r = await http.patch(f"{REST}/{table}", headers=_h("return=representation"),
                         params=params, json=data)
    if r.status_code >= 300:
        raise HTTPException(500, f"db error: {r.text}")
    return r.json()


async def sb_delete(table: str, params: dict):
    r = await http.delete(f"{REST}/{table}", headers=_h(), params=params)
    if r.status_code >= 300:
        raise HTTPException(500, f"db error: {r.text}")
    return True


# --------------------------------------------------------------- passwords
def hash_password(pw: str, salt: Optional[str] = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${dk}"


def check_password(pw: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(pw, salt), stored)


# ------------------------------------------------------------------ auth
async def current_user(request: Request) -> dict:
    token = request.cookies.get("sldchat_token")
    if not token:
        raise HTTPException(401, "not authenticated")
    rows = await sb_select("sessions", {"token": f"eq.{token}", "select": "user_id"})
    if not rows:
        raise HTTPException(401, "session expired")
    uid = rows[0]["user_id"]
    users = await sb_select("users", {"id": f"eq.{uid}",
                                      "select": "id,username,display_name,created_at"})
    if not users:
        raise HTTPException(401, "user not found")
    return users[0]


# ------------------------------------------------------------- ws hub
class Hub:
    def __init__(self):
        self.conns: dict[str, set] = {}
        self.lock = asyncio.Lock()

    async def add(self, uid: str, ws: WebSocket):
        async with self.lock:
            self.conns.setdefault(uid, set()).add(ws)

    async def remove(self, uid: str, ws: WebSocket):
        async with self.lock:
            s = self.conns.get(uid)
            if s:
                s.discard(ws)
                if not s:
                    self.conns.pop(uid, None)

    async def send(self, uid: str, payload: dict):
        for ws in list(self.conns.get(uid, ())):
            try:
                await ws.send_json(payload)
            except Exception:
                pass

    async def send_many(self, uids, payload: dict):
        for u in uids:
            await self.send(u, payload)


hub = Hub()


# ------------------------------------------------------------------ models
class RegisterIn(BaseModel):
    username: str
    display_name: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class CreateChatIn(BaseModel):
    user_id: str


class UpdateMeIn(BaseModel):
    display_name: str


class ContactRequestIn(BaseModel):
    username: str


app = FastAPI(title="sldchat", docs_url=None, redoc_url=None)


# ------------------------------------------------------------------ routes
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/register")
async def register(body: RegisterIn, response: Response):
    username = body.username.strip().lower()
    display_name = body.display_name.strip()
    if not (3 <= len(username) <= 32) or not all(c.isalnum() or c == "_" for c in username):
        raise HTTPException(400, "Invalid username (3-32, letters/digits/_)")
    if not (1 <= len(display_name) <= 40):
        raise HTTPException(400, "Invalid display name")
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    existing = await sb_select("users", {"username": f"eq.{username}", "select": "id"})
    if existing:
        raise HTTPException(409, "Username is already taken")

    rows = await sb_insert("users", {
        "username": username,
        "display_name": display_name,
        "password_hash": hash_password(body.password),
    })
    user = rows[0]
    token = secrets.token_urlsafe(32)
    await sb_insert("sessions", {"token": token, "user_id": user["id"]})
    response.set_cookie("sldchat_token", token, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
    return {"id": user["id"], "username": user["username"],
            "display_name": user["display_name"], "created_at": user["created_at"]}


@app.post("/api/login")
async def login(body: LoginIn, response: Response):
    username = body.username.strip().lower()
    rows = await sb_select("users", {
        "username": f"eq.{username}",
        "select": "id,username,display_name,created_at,password_hash",
    })
    if not rows or not check_password(body.password, rows[0]["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    user = rows[0]
    token = secrets.token_urlsafe(32)
    await sb_insert("sessions", {"token": token, "user_id": user["id"]})
    response.set_cookie("sldchat_token", token, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
    return {"id": user["id"], "username": user["username"],
            "display_name": user["display_name"], "created_at": user["created_at"]}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("sldchat_token")
    if token:
        await sb_delete("sessions", {"token": f"eq.{token}"})
    response.delete_cookie("sldchat_token")
    return {"ok": True}


@app.get("/api/me")
async def me(user=Depends(current_user)):
    return user


@app.patch("/api/me")
async def update_me(body: UpdateMeIn, user=Depends(current_user)):
    name = body.display_name.strip()
    if not (1 <= len(name) <= 40):
        raise HTTPException(400, "Invalid display name")
    rows = await sb_update("users", {"id": f"eq.{user['id']}"}, {"display_name": name})
    if not rows:
        raise HTTPException(500, "update failed")
    return rows[0]


@app.get("/api/users/search")
async def search_users(q: str = Query("", max_length=50), user=Depends(current_user)):
    q = q.strip()
    if len(q) < 1:
        return []
    q_esc = q.replace(",", "").replace("(", "").replace(")", "").replace("*", "")
    params = {
        "or": f"(username.ilike.*{q_esc}*,display_name.ilike.*{q_esc}*)",
        "select": "id,username,display_name",
        "limit": "20",
    }
    rows = await sb_select("users", params)
    return [r for r in rows if r["id"] != user["id"]]


# --------------------------------------------------------- contacts
@app.get("/api/contacts")
async def list_contacts(user=Depends(current_user)):
    me_id = user["id"]
    rows = await sb_select("contacts", {"owner_id": f"eq.{me_id}",
                                        "select": "contact_id,created_at"})
    if not rows:
        return []
    ids = [r["contact_id"] for r in rows]
    users = await sb_select("users", {"id": f"in.({','.join(ids)})",
                                      "select": "id,username,display_name"})
    u_map = {u["id"]: u for u in users}
    out = []
    for r in rows:
        u = u_map.get(r["contact_id"])
        if u:
            out.append({"id": u["id"], "username": u["username"],
                        "display_name": u["display_name"]})
    out.sort(key=lambda x: x["display_name"].lower())
    return out


@app.get("/api/contacts/requests")
async def list_contact_requests(user=Depends(current_user)):
    me_id = user["id"]
    rows = await sb_select("contact_requests", {
        "to_id": f"eq.{me_id}",
        "status": "eq.pending",
        "select": "id,from_id,created_at",
        "order": "created_at.desc",
    })
    if not rows:
        return []
    ids = [r["from_id"] for r in rows]
    users = await sb_select("users", {"id": f"in.({','.join(ids)})",
                                      "select": "id,username,display_name"})
    u_map = {u["id"]: u for u in users}
    out = []
    for r in rows:
        u = u_map.get(r["from_id"])
        if u:
            out.append({"id": r["id"], "from": u, "created_at": r["created_at"]})
    return out


@app.post("/api/contacts/request")
async def send_contact_request(body: ContactRequestIn, user=Depends(current_user)):
    me_id = user["id"]
    uname = body.username.strip().lstrip("@").lower()
    if not uname:
        raise HTTPException(400, "Enter a username")
    rows = await sb_select("users", {"username": f"eq.{uname}",
                                     "select": "id,username,display_name"})
    if not rows:
        raise HTTPException(404, "User not found")
    target = rows[0]
    if target["id"] == me_id:
        raise HTTPException(400, "You can't add yourself")

    already = await sb_select("contacts", {"owner_id": f"eq.{me_id}",
                                           "contact_id": f"eq.{target['id']}",
                                           "select": "contact_id"})
    if already:
        raise HTTPException(409, "Already in your contacts")

    incoming = await sb_select("contact_requests", {
        "from_id": f"eq.{target['id']}",
        "to_id": f"eq.{me_id}",
        "status": "eq.pending",
        "select": "id",
    })
    if incoming:
        raise HTTPException(409, "This user already sent you a request — check Contacts")

    dup = await sb_select("contact_requests", {
        "from_id": f"eq.{me_id}",
        "to_id": f"eq.{target['id']}",
        "status": "eq.pending",
        "select": "id",
    })
    if dup:
        raise HTTPException(409, "Request already sent")

    req = await sb_insert("contact_requests", {
        "from_id": me_id, "to_id": target["id"], "status": "pending",
    })
    row = req[0]
    payload = {
        "type": "contact_request",
        "request": {
            "id": row["id"],
            "from": {"id": me_id, "username": user["username"],
                     "display_name": user["display_name"]},
            "created_at": row["created_at"],
        }
    }
    await hub.send(target["id"], payload)
    return {"ok": True}


@app.post("/api/contacts/requests/{req_id}/accept")
async def accept_contact_request(req_id: str, user=Depends(current_user)):
    me_id = user["id"]
    rows = await sb_select("contact_requests", {
        "id": f"eq.{req_id}", "to_id": f"eq.{me_id}",
        "select": "id,from_id,to_id,status",
    })
    if not rows:
        raise HTTPException(404, "Request not found")
    req = rows[0]
    if req["status"] != "pending":
        raise HTTPException(400, "Already handled")
    from_id = req["from_id"]

    await sb_update("contact_requests", {"id": f"eq.{req_id}"}, {"status": "accepted"})

    existing = await sb_select("contacts", {"owner_id": f"eq.{me_id}",
                                            "contact_id": f"eq.{from_id}",
                                            "select": "contact_id"})
    if not existing:
        await sb_insert("contacts", [
            {"owner_id": me_id, "contact_id": from_id},
            {"owner_id": from_id, "contact_id": me_id},
        ])

    await hub.send(from_id, {
        "type": "contact_accepted",
        "contact": {"id": me_id, "username": user["username"],
                    "display_name": user["display_name"]},
    })
    return {"ok": True}


@app.post("/api/contacts/requests/{req_id}/decline")
async def decline_contact_request(req_id: str, user=Depends(current_user)):
    me_id = user["id"]
    rows = await sb_select("contact_requests", {
        "id": f"eq.{req_id}", "to_id": f"eq.{me_id}",
        "select": "id,from_id,status",
    })
    if not rows:
        raise HTTPException(404, "Request not found")
    req = rows[0]
    if req["status"] != "pending":
        raise HTTPException(400, "Already handled")
    await sb_update("contact_requests", {"id": f"eq.{req_id}"}, {"status": "declined"})
    await hub.send(req["from_id"], {
        "type": "contact_declined",
        "by": {"id": me_id, "username": user["username"],
               "display_name": user["display_name"]},
    })
    return {"ok": True}


# --------------------------------------------------------- chats
@app.get("/api/chats")
async def list_chats(user=Depends(current_user)):
    me_id = user["id"]
    my_members = await sb_select("chat_members", {"user_id": f"eq.{me_id}", "select": "chat_id"})
    chat_ids = [m["chat_id"] for m in my_members]
    if not chat_ids:
        return []
    in_list = "(" + ",".join(chat_ids) + ")"

    chats = await sb_select("chats", {"id": f"in.{in_list}",
                                      "select": "id,is_group,title,created_at"})
    members = await sb_select("chat_members", {"chat_id": f"in.{in_list}",
                                               "select": "chat_id,user_id"})
    user_ids = list({m["user_id"] for m in members})
    users = await sb_select("users", {"id": f"in.({','.join(user_ids)})",
                                      "select": "id,username,display_name"})
    user_map = {u["id"]: u for u in users}

    members_by_chat: dict[str, list[str]] = {}
    for m in members:
        members_by_chat.setdefault(m["chat_id"], []).append(m["user_id"])

    async def last_msg(cid):
        r = await sb_select("messages", {
            "chat_id": f"eq.{cid}",
            "select": "text,created_at,user_id",
            "order": "created_at.desc",
            "limit": "1",
        })
        return cid, (r[0] if r else None)

    last_results = await asyncio.gather(*[last_msg(c) for c in chat_ids])
    last_map = dict(last_results)

    result = []
    for c in chats:
        cid = c["id"]
        member_ids = members_by_chat.get(cid, [])
        others = [user_map[uid] for uid in member_ids if uid != me_id and uid in user_map]
        if c["is_group"]:
            title = c.get("title") or "Group"
            uname = None
        else:
            title = others[0]["display_name"] if others else "Saved Messages"
            uname = others[0]["username"] if others else None
        lm = last_map.get(cid)
        result.append({
            "id": cid,
            "title": title,
            "username": uname,
            "is_group": c["is_group"],
            "last_message": (lm or {}).get("text", ""),
            "last_message_at": (lm or {}).get("created_at") or c["created_at"],
            "peer_id": others[0]["id"] if (others and not c["is_group"]) else None,
        })
    result.sort(key=lambda x: x["last_message_at"] or "", reverse=True)
    return result


@app.post("/api/chats")
async def create_chat(body: CreateChatIn, user=Depends(current_user)):
    me_id = user["id"]
    other = body.user_id
    if other == me_id:
        raise HTTPException(400, "Cannot chat with yourself")
    ou = await sb_select("users", {"id": f"eq.{other}", "select": "id"})
    if not ou:
        raise HTTPException(404, "User not found")

    my_members = await sb_select("chat_members", {"user_id": f"eq.{me_id}", "select": "chat_id"})
    my_ids = [m["chat_id"] for m in my_members]
    if my_ids:
        shared = await sb_select("chat_members", {
            "chat_id": f"in.({','.join(my_ids)})",
            "user_id": f"eq.{other}",
            "select": "chat_id",
        })
        if shared:
            for s in shared:
                ch = await sb_select("chats", {"id": f"eq.{s['chat_id']}", "select": "is_group"})
                if ch and not ch[0]["is_group"]:
                    return {"chat_id": s["chat_id"]}

    ch = await sb_insert("chats", {"is_group": False})
    cid = ch[0]["id"]
    await sb_insert("chat_members", [
        {"chat_id": cid, "user_id": me_id},
        {"chat_id": cid, "user_id": other},
    ])
    await hub.send(other, {"type": "chat_created", "chat_id": cid})
    return {"chat_id": cid}


@app.get("/api/chats/{chat_id}/messages")
async def get_messages(chat_id: str, user=Depends(current_user)):
    m = await sb_select("chat_members", {
        "chat_id": f"eq.{chat_id}",
        "user_id": f"eq.{user['id']}",
        "select": "chat_id",
    })
    if not m:
        raise HTTPException(403, "not a member")
    msgs = await sb_select("messages", {
        "chat_id": f"eq.{chat_id}",
        "select": "id,user_id,text,created_at",
        "order": "created_at.asc",
        "limit": "500",
    })
    uids = list({x["user_id"] for x in msgs})
    user_map = {}
    if uids:
        users = await sb_select("users", {"id": f"in.({','.join(uids)})",
                                          "select": "id,username,display_name"})
        user_map = {u["id"]: u for u in users}
    for x in msgs:
        u = user_map.get(x["user_id"], {})
        x["username"] = u.get("username", "?")
        x["display_name"] = u.get("display_name", "?")
    return msgs


# ------------------------------------------------------------------ ws
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    token = ws.cookies.get("sldchat_token")
    if not token:
        await ws.close(code=4401)
        return
    rows = await sb_select("sessions", {"token": f"eq.{token}", "select": "user_id"})
    if not rows:
        await ws.close(code=4401)
        return
    uid = rows[0]["user_id"]
    users = await sb_select("users", {"id": f"eq.{uid}",
                                      "select": "id,username,display_name"})
    if not users:
        await ws.close(code=4401)
        return
    user = users[0]

    await ws.accept()
    await hub.add(uid, ws)
    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type")

            if t == "message":
                chat_id = data.get("chat_id")
                text = (data.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                text = text[:4000]
                m = await sb_select("chat_members", {
                    "chat_id": f"eq.{chat_id}",
                    "user_id": f"eq.{uid}",
                    "select": "chat_id",
                })
                if not m:
                    continue
                rows = await sb_insert("messages", {
                    "chat_id": chat_id, "user_id": uid, "text": text,
                })
                msg = rows[0]
                msg["username"] = user["username"]
                msg["display_name"] = user["display_name"]
                members = await sb_select("chat_members", {
                    "chat_id": f"eq.{chat_id}", "select": "user_id",
                })
                await hub.send_many([x["user_id"] for x in members],
                                    {"type": "message", "message": msg})

            elif t == "typing":
                chat_id = data.get("chat_id")
                if not chat_id:
                    continue
                members = await sb_select("chat_members", {
                    "chat_id": f"eq.{chat_id}", "select": "user_id",
                })
                for m in members:
                    if m["user_id"] != uid:
                        await hub.send(m["user_id"], {
                            "type": "typing",
                            "chat_id": chat_id,
                            "username": user["username"],
                            "display_name": user["display_name"],
                        })

            elif t == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("ws error:", e)
    finally:
        await hub.remove(uid, ws)


# ------------------------------------------------------------------ HTML
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<title>sldchat</title>
<script>
  (function(){
    try {
      var t = localStorage.getItem('sldchat-theme') || 'light';
      document.documentElement.setAttribute('data-theme', t);
    } catch(e){}
  })();
</script>
<style>
:root, html[data-theme="light"]{
  --blue:#517da2; --blue-d:#46698a; --blue-l:#6b9dc2;
  --bg:#e6ebee; --panel:#fff; --border:#dfe4e8; --border-2:#eef1f3;
  --text:#222; --text-2:#4a5560; --muted:#8a939b;
  --bubble-in:#fff; --bubble-out:#e5f3f8;
  --bubble-in-text:#1a1f24; --bubble-out-text:#1a1f24;
  --bubble-time-in:#96a0a8; --bubble-time-out:#7ba5ba;
  --date-sep-bg:rgba(255,255,255,.85); --date-sep-text:#68727b;
  --hover-bg:#f5f8fa; --active-bg:#e9f0f5; --active-bg-2:#e1eaf0;
  --input-bg:#f7f9fa; --section-bg:#f8fafb; --empty-icon:#c3ccd3;
  --header-bg:#517da2; --header-text:#fff;
  --header-grad:linear-gradient(160deg,#5a8bb5 0%,#3d6b8f 100%);
  --brand-grad:linear-gradient(160deg,#6b9dc2 0%,#3d6b8f 100%);
  --disabled:#c3ccd3;
  --btn-ghost-border:#dfe4e8;
  --btn-ghost-border-hover:#c8d2da;
  --danger:#d9534f; --danger-border:#f0d4d3; --danger-bg:#fbf1f1;
  --drawer-bg:#fff;
  --shadow-sm:0 1px 2px rgba(0,0,0,.06);
  --shadow-md:0 4px 16px rgba(0,0,0,.10);
  --shadow-lg:0 12px 48px rgba(0,0,0,.22);
  --scrollbar:rgba(0,0,0,.15);
  --scrollbar-h:rgba(0,0,0,.25);
}
html[data-theme="dark"]{
  --blue:#5a95c2; --blue-d:#4a7fa8; --blue-l:#78acd2;
  --bg:#0e1621; --panel:#17212b; --border:#242f3d; --border-2:#1e2936;
  --text:#e8eaec; --text-2:#b8c1c9; --muted:#7d8a97;
  --bubble-in:#182533; --bubble-out:#2b5278;
  --bubble-in-text:#e8eaec; --bubble-out-text:#f0f4f7;
  --bubble-time-in:#6b7685; --bubble-time-out:#a4c2d8;
  --date-sep-bg:rgba(30,42,55,.85); --date-sep-text:#a5b1bc;
  --hover-bg:#1e2a37; --active-bg:#243d52; --active-bg-2:#2a4761;
  --input-bg:#1e2936; --section-bg:#141d28; --empty-icon:#3d4c5a;
  --header-bg:#17212b; --header-text:#e8eaec;
  --header-grad:linear-gradient(160deg,#1e2a37 0%,#17212b 100%);
  --brand-grad:linear-gradient(160deg,#2b5278 0%,#17212b 100%);
  --disabled:#3d4c5a;
  --btn-ghost-border:#2a3645;
  --btn-ghost-border-hover:#3a4a5c;
  --danger:#ef6b6b; --danger-border:#5a2d2d; --danger-bg:#2b1c1c;
  --drawer-bg:#17212b;
  --shadow-sm:0 1px 2px rgba(0,0,0,.35);
  --shadow-md:0 4px 16px rgba(0,0,0,.45);
  --shadow-lg:0 12px 48px rgba(0,0,0,.65);
  --scrollbar:rgba(255,255,255,.12);
  --scrollbar-h:rgba(255,255,255,.22);
}
*{box-sizing:border-box}
html,body{height:100%;margin:0;overflow:hidden}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:14px;color:var(--text);background:var(--bg);
  -webkit-font-smoothing:antialiased;-webkit-tap-highlight-color:transparent;
  transition:background .18s,color .18s;
}
button{font:inherit;cursor:pointer;border:none;background:none;color:inherit;padding:0}
button:disabled{cursor:default}
input,textarea{font:inherit;color:inherit}
svg{display:block;flex-shrink:0}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:var(--scrollbar);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--scrollbar-h)}
::-webkit-scrollbar-track{background:transparent}

/* ============ AUTH ============ */
#login{position:fixed;inset:0;display:flex;background:var(--panel);z-index:100}
.login-brand{
  flex:1;background:var(--brand-grad);
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  color:#fff;padding:40px;position:relative;overflow:hidden;
}
.login-brand::before{
  content:"";position:absolute;top:-120px;right:-120px;width:400px;height:400px;
  border-radius:50%;background:rgba(255,255,255,.06);
}
.login-brand::after{
  content:"";position:absolute;bottom:-180px;left:-80px;width:500px;height:500px;
  border-radius:50%;background:rgba(255,255,255,.05);
}
.brand-icon{
  width:88px;height:88px;border-radius:50%;
  background:rgba(255,255,255,.18);
  display:flex;align-items:center;justify-content:center;margin-bottom:22px;
  position:relative;z-index:1;
}
.brand-icon svg{width:44px;height:44px}
.login-brand h1{font-size:38px;font-weight:600;margin:0;letter-spacing:.5px;position:relative;z-index:1}
.login-brand p{margin:8px 0 0;opacity:.85;font-size:15px;position:relative;z-index:1}

.login-form-wrap{flex:1;display:flex;align-items:center;justify-content:center;padding:40px 24px}
.login-card{width:100%;max-width:380px}
.login-card h2{margin:0 0 4px;font-size:22px;font-weight:600}
.login-card .lead{color:var(--muted);font-size:13px;margin:0 0 22px}

.auth-tabs{display:flex;gap:0;border-bottom:1px solid var(--border-2);margin-bottom:20px}
.auth-tab{
  flex:1;padding:12px 0;color:var(--muted);font-weight:500;font-size:14px;
  border-bottom:2px solid transparent;margin-bottom:-1px;transition:.15s;
}
.auth-tab:hover{color:var(--text-2)}
.auth-tab.active{color:var(--blue);border-bottom-color:var(--blue)}

.field{margin-bottom:14px}
.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px;font-weight:500}
.field input{
  width:100%;padding:11px 14px;border:1px solid var(--border);border-radius:8px;
  outline:none;background:var(--panel);transition:.15s;
}
.field input:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(81,125,162,.14)}
.field .hint{font-size:11px;color:var(--muted);margin-top:5px}

.btn-primary{
  width:100%;padding:12px;background:var(--blue);color:#fff;
  border-radius:8px;font-weight:500;font-size:15px;transition:.15s;
}
.btn-primary:hover{background:var(--blue-d)}
.btn-primary:disabled{opacity:.6;cursor:default}

.auth-error{
  color:var(--danger);font-size:13px;min-height:18px;margin-bottom:12px;
  line-height:1.3;
}

@media (max-width:860px){ .login-brand{display:none} }

/* ============ APP ============ */
#app{display:none;height:100vh;height:100dvh}
.app-layout{display:flex;height:100%;position:relative}

/* --- Sidebar --- */
.sidebar{
  width:340px;min-width:340px;background:var(--panel);
  border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:0;
}
.sidebar-head{
  display:flex;align-items:center;padding:8px 10px;gap:6px;
  background:var(--header-bg);color:var(--header-text);height:52px;flex-shrink:0;
}
.sidebar-head .title{flex:1;font-weight:600;font-size:15px;letter-spacing:.4px;padding:0 6px}
.icon-btn{
  width:36px;height:36px;border-radius:6px;
  display:inline-flex;align-items:center;justify-content:center;
  transition:background .12s;flex-shrink:0;
}
.icon-btn:hover{background:rgba(255,255,255,.15)}
.icon-btn:active{background:rgba(255,255,255,.25)}
.icon-btn svg{width:19px;height:19px}

.sidebar-body{flex:1;overflow-y:auto;overflow-x:hidden;min-height:0;display:flex;flex-direction:column}

.search-wrap{position:relative;padding:10px;border-bottom:1px solid var(--border-2);flex-shrink:0}
.search-wrap .search-icon{
  position:absolute;left:22px;top:50%;transform:translateY(-50%);
  width:15px;height:15px;color:var(--muted);pointer-events:none;
}
.search-wrap input{
  width:100%;padding:9px 12px 9px 36px;border:1px solid var(--border);
  border-radius:8px;outline:none;background:var(--input-bg);transition:.15s;
}
.search-wrap input:focus{border-color:var(--blue);background:var(--panel)}

.chat-list{flex:1;overflow-y:auto}
.chat-item{
  display:flex;align-items:center;gap:12px;
  padding:10px 14px;cursor:pointer;transition:background .12s;
}
.chat-item:hover{background:var(--hover-bg)}
.chat-item.active{background:var(--active-bg)}
.chat-item:active{background:var(--active-bg-2)}
.avatar{
  width:46px;height:46px;min-width:46px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  color:#fff;font-weight:600;font-size:17px;user-select:none;letter-spacing:.5px;
}
.chat-item-body{flex:1;min-width:0}
.chat-item-title{
  font-weight:500;color:var(--text);font-size:14px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.chat-item-sub{
  color:var(--muted);font-size:12.5px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;margin-top:3px;
}
.list-section{
  padding:10px 14px 6px;font-size:11px;text-transform:uppercase;
  color:var(--muted);font-weight:700;letter-spacing:.6px;background:var(--section-bg);
}
.empty{
  padding:40px 24px;text-align:center;color:var(--muted);font-size:13.5px;line-height:1.5;
}
.empty svg{width:44px;height:44px;margin:0 auto 14px;color:var(--empty-icon)}

.sub-view{flex:1;display:flex;flex-direction:column;overflow-y:auto}
.sub-head{
  padding:16px 18px 12px;border-bottom:1px solid var(--border-2);
  display:flex;align-items:center;justify-content:space-between;gap:10px;
}
.sub-head h2{margin:0;font-size:17px;font-weight:600}
.sub-head p{margin:4px 0 0;color:var(--muted);font-size:12.5px}
.sub-head .sub-head-actions{display:flex;gap:8px;flex-shrink:0}
.sub-body{padding:10px 0}
.sub-item{
  display:flex;align-items:center;gap:14px;padding:13px 18px;
  transition:background .12s;
}
.sub-item.clickable{cursor:pointer}
.sub-item.clickable:hover{background:var(--hover-bg)}
.sub-item svg{width:20px;height:20px;color:var(--muted);flex-shrink:0}
.sub-item .sub-item-body{flex:1;min-width:0}
.sub-item .sub-item-title{font-size:14px;font-weight:500;color:var(--text)}
.sub-item .sub-item-sub{font-size:12.5px;color:var(--muted);margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

.btn-mini{
  padding:7px 14px;border-radius:7px;font-weight:500;font-size:13px;
  background:var(--blue);color:#fff;transition:.15s;
  display:inline-flex;align-items:center;gap:6px;
}
.btn-mini:hover{background:var(--blue-d)}
.btn-mini svg{width:15px;height:15px}
.btn-mini.ghost{
  background:transparent;color:var(--text);
  border:1px solid var(--btn-ghost-border);
}
.btn-mini.ghost:hover{background:var(--hover-bg);border-color:var(--btn-ghost-border-hover)}
.btn-mini.accept{background:#5cb85c}
.btn-mini.accept:hover{background:#4cae4c}
.btn-mini.decline{background:transparent;color:var(--danger);border:1px solid var(--danger-border)}
.btn-mini.decline:hover{background:var(--danger-bg)}

/* contact request row */
.request-item{
  display:flex;align-items:center;gap:12px;padding:12px 16px;
  border-bottom:1px solid var(--border-2);
}
.request-item .chat-item-body{flex:1;min-width:0}
.request-item .request-actions{display:flex;gap:6px;flex-shrink:0}

/* theme toggle switch */
.switch{
  width:42px;height:24px;border-radius:12px;flex-shrink:0;
  background:var(--border);position:relative;transition:background .18s;
}
.switch::after{
  content:"";position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;
  background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.25);
  transition:transform .18s;
}
.switch.on{background:var(--blue)}
.switch.on::after{transform:translateX(18px)}

.profile-view{padding:24px 20px;text-align:center}
.profile-avatar{
  width:96px;height:96px;border-radius:50%;margin:0 auto 16px;
  display:flex;align-items:center;justify-content:center;
  color:#fff;font-weight:600;font-size:36px;letter-spacing:1px;
}
.profile-name{font-size:20px;font-weight:600;margin:0 0 4px}
.profile-username{color:var(--muted);font-size:13.5px}
.profile-meta{margin-top:6px;color:var(--muted);font-size:12px}
.profile-actions{margin-top:28px;display:flex;flex-direction:column;gap:10px}
.btn-ghost{
  padding:11px 14px;border:1px solid var(--btn-ghost-border);border-radius:8px;
  font-weight:500;color:var(--text);background:var(--panel);transition:.15s;
  display:flex;align-items:center;justify-content:center;gap:8px;
}
.btn-ghost:hover{background:var(--hover-bg);border-color:var(--btn-ghost-border-hover)}
.btn-ghost svg{width:17px;height:17px}
.btn-ghost.danger{color:var(--danger);border-color:var(--danger-border)}
.btn-ghost.danger:hover{background:var(--danger-bg)}

/* --- Main --- */
.main{flex:1;display:flex;flex-direction:column;background:var(--bg);min-width:0;min-height:0}
.main-header{
  height:52px;padding:0 12px;background:var(--header-bg);color:var(--header-text);
  display:flex;align-items:center;gap:8px;flex-shrink:0;
}
.main-header .chat-title{font-weight:600;font-size:15px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
.main-header .chat-status{
  font-size:12px;opacity:.9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  max-width:180px;transition:opacity .2s;
}
.back-btn{display:none}

.messages{
  flex:1;overflow-y:auto;padding:16px 20px;min-height:0;
  display:flex;flex-direction:column;scroll-behavior:smooth;
}
.no-chat{
  margin:auto;color:var(--muted);font-size:14px;text-align:center;
  padding:0 20px;max-width:320px;line-height:1.6;
}
.no-chat svg{width:64px;height:64px;color:var(--empty-icon);margin:0 auto 18px}

.date-sep{
  align-self:center;background:var(--date-sep-bg);color:var(--date-sep-text);
  font-size:12px;padding:4px 12px;border-radius:12px;margin:14px 0 8px;
  box-shadow:var(--shadow-sm);font-weight:500;
}
.msg{display:flex;margin:2px 0;animation:msgIn .18s ease-out}
.msg.out{justify-content:flex-end}
@keyframes msgIn{
  from{opacity:0;transform:translateY(4px)}
  to{opacity:1;transform:none}
}
.bubble{
  max-width:min(66%,520px);padding:7px 12px 20px;position:relative;
  word-wrap:break-word;overflow-wrap:anywhere;
  line-height:1.4;font-size:14.5px;box-shadow:var(--shadow-sm);
  background:var(--bubble-in);color:var(--bubble-in-text);
  border-radius:12px 12px 12px 4px;
}
.msg.out .bubble{
  background:var(--bubble-out);color:var(--bubble-out-text);
  border-radius:12px 12px 4px 12px;
}
.bubble .author{
  font-size:12.5px;font-weight:600;color:var(--blue);margin-bottom:2px;
  letter-spacing:.1px;
}
.bubble .text{white-space:pre-wrap}
.bubble .time{
  position:absolute;right:10px;bottom:4px;
  font-size:11px;font-weight:500;color:var(--bubble-time-in);
}
.msg.out .bubble .time{color:var(--bubble-time-out)}

.composer{
  display:flex;align-items:flex-end;gap:8px;
  padding:10px 12px;background:var(--panel);border-top:1px solid var(--border);
  flex-shrink:0;padding-bottom:calc(10px + env(safe-area-inset-bottom));
}
.composer textarea{
  flex:1;resize:none;border:1px solid var(--border);outline:none;
  padding:10px 14px;max-height:140px;min-height:42px;
  background:var(--input-bg);border-radius:21px;line-height:1.4;
  transition:border-color .15s,background .15s;font-size:14.5px;
}
.composer textarea:focus{border-color:var(--blue);background:var(--panel)}
.composer textarea:disabled{opacity:.55}
.send-btn{
  width:42px;height:42px;border-radius:50%;flex-shrink:0;
  background:var(--blue);color:#fff;
  display:inline-flex;align-items:center;justify-content:center;
  transition:background .15s,transform .1s;
}
.send-btn:hover{background:var(--blue-d)}
.send-btn:active{transform:scale(.94)}
.send-btn:disabled{background:var(--disabled);cursor:default}
.send-btn svg{width:20px;height:20px;margin-left:-2px}

/* ============ DRAWER ============ */
.drawer-backdrop{
  position:fixed;inset:0;background:rgba(0,0,0,.42);z-index:60;
  opacity:0;pointer-events:none;transition:opacity .22s;
}
.drawer-backdrop.open{opacity:1;pointer-events:auto}
.drawer{
  position:fixed;top:0;left:0;bottom:0;width:280px;background:var(--drawer-bg);z-index:70;
  transform:translateX(-100%);transition:transform .22s ease-out;
  box-shadow:6px 0 32px rgba(0,0,0,.18);
  display:flex;flex-direction:column;
}
.drawer.open{transform:translateX(0)}
.drawer-head{
  padding:20px 18px;background:var(--header-grad);
  color:#fff;display:flex;align-items:center;gap:14px;
}
.drawer-avatar{
  width:56px;height:56px;border-radius:50%;flex-shrink:0;
  background:rgba(255,255,255,.22);
  display:flex;align-items:center;justify-content:center;
  font-weight:600;font-size:22px;letter-spacing:.5px;
}
.drawer-info{min-width:0;flex:1}
.drawer-name{font-weight:600;font-size:15px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.drawer-username{font-size:12.5px;opacity:.8;margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.drawer-nav{padding:8px 0;flex:1;overflow-y:auto}
.drawer-item{
  width:100%;display:flex;align-items:center;gap:16px;
  padding:13px 22px;color:var(--text);font-size:14.5px;font-weight:500;
  transition:background .12s;text-align:left;
}
.drawer-item:hover{background:var(--hover-bg)}
.drawer-item:active{background:var(--active-bg)}
.drawer-item svg{width:20px;height:20px;color:var(--muted)}
.drawer-item.active{background:var(--active-bg);color:var(--blue)}
.drawer-item.active svg{color:var(--blue)}
.drawer-item .badge{
  margin-left:auto;background:var(--blue);color:#fff;font-size:11px;font-weight:600;
  padding:2px 7px;border-radius:10px;min-width:20px;text-align:center;
}
.drawer-footer{
  padding:12px 22px;border-top:1px solid var(--border-2);
  font-size:11.5px;color:var(--muted);letter-spacing:.3px;
}

/* ============ MODAL ============ */
.modal-backdrop{
  position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:80;
  display:flex;align-items:center;justify-content:center;padding:20px;
  opacity:0;pointer-events:none;transition:opacity .18s;
}
.modal-backdrop.open{opacity:1;pointer-events:auto}
.modal{
  background:var(--panel);border-radius:12px;padding:22px;width:100%;max-width:380px;
  box-shadow:var(--shadow-lg);transform:translateY(8px);
  transition:transform .18s;
}
.modal-backdrop.open .modal{transform:translateY(0)}
.modal h3{margin:0 0 6px;font-size:17px;font-weight:600}
.modal p{margin:0 0 16px;font-size:13px;color:var(--muted);line-height:1.5}
.modal input{
  width:100%;padding:11px 14px;border:1px solid var(--border);border-radius:8px;
  outline:none;background:var(--input-bg);transition:.15s;
}
.modal input:focus{border-color:var(--blue);background:var(--panel)}
.modal-error{
  color:var(--danger);font-size:12.5px;margin-top:8px;min-height:16px;
}
.modal-actions{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}

/* ============ TOAST ============ */
#toast-wrap{
  position:fixed;top:14px;left:50%;transform:translateX(-50%);
  z-index:90;display:flex;flex-direction:column;gap:8px;pointer-events:none;
  max-width:calc(100% - 24px);
}
.toast{
  background:var(--panel);border:1px solid var(--border);color:var(--text);
  padding:10px 16px;border-radius:10px;font-size:13.5px;
  box-shadow:var(--shadow-md);opacity:0;transform:translateY(-6px);
  transition:opacity .2s,transform .2s;pointer-events:auto;
  display:flex;align-items:center;gap:10px;
}
.toast.show{opacity:1;transform:translateY(0)}
.toast svg{width:18px;height:18px;color:var(--blue);flex-shrink:0}

/* ============ MOBILE ============ */
@media (max-width:760px){
  .sidebar{width:100%;min-width:0;border-right:none}
  .main{
    position:fixed;inset:0;z-index:40;
    transform:translateX(100%);transition:transform .24s ease-out;
    will-change:transform;
  }
  .main.mobile-open{transform:translateX(0)}
  .main .back-btn{display:inline-flex}
  .main-header .chat-status{max-width:120px}
  .bubble{max-width:82%}
  .messages{padding:12px 12px 16px}
  .composer{padding:8px 10px;padding-bottom:calc(8px + env(safe-area-inset-bottom))}
  .chat-item{padding:12px 14px}
  .avatar{width:48px;height:48px;min-width:48px}
  .drawer{width:min(300px,84vw)}
}
</style>
</head>
<body>

<div id="toast-wrap"></div>

<!-- ============== AUTH ============== -->
<div id="login">
  <div class="login-brand">
    <div class="brand-icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
      </svg>
    </div>
    <h1>sldchat</h1>
    <p>Simple. Fast. Yours.</p>
  </div>
  <div class="login-form-wrap">
    <div class="login-card">
      <h2 id="auth-heading">Sign in</h2>
      <p class="lead" id="auth-lead">Welcome back to sldchat</p>

      <div class="auth-tabs">
        <button class="auth-tab active" data-mode="login" type="button">Sign in</button>
        <button class="auth-tab" data-mode="register" type="button">Sign up</button>
      </div>

      <form id="auth-form" autocomplete="on">
        <div class="auth-error" id="auth-error"></div>
        <div class="field">
          <label for="in-username">Username</label>
          <input id="in-username" name="username" autocomplete="username"
                 placeholder="e.g. john" required>
        </div>
        <div class="field" id="field-display" style="display:none">
          <label for="in-display">Display name</label>
          <input id="in-display" name="display" autocomplete="name"
                 placeholder="e.g. John Doe" maxlength="40">
          <div class="hint">How others will see you</div>
        </div>
        <div class="field">
          <label for="in-password">Password</label>
          <input id="in-password" name="password" type="password"
                 autocomplete="current-password" placeholder="••••••••" required>
          <div class="hint" id="pw-hint" style="display:none">At least 6 characters</div>
        </div>
        <button class="btn-primary" id="btn-auth" type="submit">Sign in</button>
      </form>
    </div>
  </div>
</div>

<!-- ============== APP ============== -->
<div id="app">
  <div class="drawer-backdrop" id="drawer-backdrop"></div>
  <aside class="drawer" id="drawer">
    <div class="drawer-head">
      <div class="drawer-avatar" id="drawer-avatar">?</div>
      <div class="drawer-info">
        <div class="drawer-name" id="drawer-name">—</div>
        <div class="drawer-username" id="drawer-username">@—</div>
      </div>
    </div>
    <nav class="drawer-nav">
      <button class="drawer-item" data-view="chats" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
        <span>Chats</span>
      </button>
      <button class="drawer-item" data-view="notifications" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
        <span>Notifications</span>
        <span class="badge" id="nav-badge-notif" style="display:none">0</span>
      </button>
      <button class="drawer-item" data-view="contacts" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        <span>Contacts</span>
        <span class="badge" id="nav-badge-contacts" style="display:none">0</span>
      </button>
      <button class="drawer-item" data-view="settings" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        <span>Settings</span>
      </button>
      <button class="drawer-item" data-view="profile" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
        <span>Profile</span>
      </button>
    </nav>
    <div class="drawer-footer">sldchat · v1.1</div>
  </aside>

  <div class="app-layout">
    <aside class="sidebar">
      <div class="sidebar-head">
        <button class="icon-btn" id="btn-menu" title="Menu" type="button">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
        </button>
        <div class="title" id="sidebar-title">sldchat</div>
      </div>
      <div class="sidebar-body" id="sidebar-body"></div>
    </aside>

    <main class="main" id="main">
      <div class="main-header">
        <button class="icon-btn back-btn" id="btn-mobile-back" title="Back" type="button">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
        </button>
        <div class="chat-title" id="chat-title">sldchat</div>
        <div class="chat-status" id="chat-status"></div>
      </div>
      <div class="messages" id="messages">
        <div class="no-chat">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
          </svg>
          <div>Select a chat to start messaging</div>
        </div>
      </div>
      <div class="composer">
        <textarea id="input" placeholder="Write a message..." rows="1" disabled></textarea>
        <button class="send-btn" id="send" disabled title="Send" type="button">
          <svg viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
        </button>
      </div>
    </main>
  </div>
</div>

<!-- ============== MODAL: add contact ============== -->
<div class="modal-backdrop" id="contact-modal">
  <div class="modal">
    <h3>Add contact</h3>
    <p>Enter a username — they will get a request to accept or decline.</p>
    <input id="contact-username" placeholder="@username" autocomplete="off" maxlength="40">
    <div class="modal-error" id="contact-error"></div>
    <div class="modal-actions">
      <button class="btn-mini ghost" id="contact-cancel" type="button">Cancel</button>
      <button class="btn-mini" id="contact-submit" type="button">Send request</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

const ICONS = {
  search:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.35-4.35"/></svg>',
  bell:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
  users:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  userPlus:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>',
  user:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
  gear:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  logout:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
  edit:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  chat:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
  moon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
  sun:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>',
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  checkCircle:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
  x:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
};

// ----------------------------- state -----------------------------
let me = null;
let ws = null;
let chats = [];
let contacts = [];
let contactRequests = [];
let notifications = [];      // {id, type, text, ts, read}
let unreadNotif = 0;
let currentChatId = null;
let currentChat = null;
let messages = {};
let view = 'chats';
let searchQuery = '';
let typingTimer = null;
let typingHideTimer = null;
let isMobile = () => window.matchMedia('(max-width: 760px)').matches;

// ----------------------------- theme -----------------------------
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('sldchat-theme', t); } catch(e){}
}
function currentTheme(){
  return document.documentElement.getAttribute('data-theme') || 'light';
}
function toggleTheme(){
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  if(view === 'settings') renderSidebar();
}

// ----------------------------- helpers -----------------------------
function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function initials(name){
  if(!name) return '?';
  const p = String(name).trim().split(/\s+/);
  const a = (p[0] && p[0][0]) || '';
  const b = (p[1] && p[1][0]) || '';
  return (a+b).toUpperCase() || '?';
}
function avatarColor(id){
  const colors = ['#e17076','#7bc862','#e5ca77','#65aadd','#a695e7','#ee7aae','#6ec9cb','#faa774'];
  let h = 0;
  for(let i=0;i<id.length;i++) h = (h*31 + id.charCodeAt(i)) >>> 0;
  return colors[h % colors.length];
}
async function api(path, opts){
  const r = await fetch(path, Object.assign({
    headers: {'Content-Type':'application/json'}
  }, opts || {}));
  if(!r.ok){
    let msg = 'Request failed';
    try{ const j = await r.json(); msg = j.detail || msg; }catch(_){}
    throw new Error(msg);
  }
  if(r.status === 204) return null;
  try { return await r.json(); } catch(_) { return null; }
}
function fmtTime(iso){
  const d = new Date(iso);
  return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}
function fmtDate(iso){
  const d = new Date(iso);
  const today = new Date();
  const yest = new Date(); yest.setDate(today.getDate() - 1);
  if(d.toDateString() === today.toDateString()) return 'Today';
  if(d.toDateString() === yest.toDateString()) return 'Yesterday';
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return d.getDate() + ' ' + months[d.getMonth()] +
         (d.getFullYear() !== today.getFullYear() ? ' ' + d.getFullYear() : '');
}

// ----------------------------- toast -----------------------------
function toast(text, icon){
  const wrap = $('toast-wrap');
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = (icon || ICONS.checkCircle) + '<span>' + escapeHtml(text) + '</span>';
  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 250);
  }, 3200);
}

// ----------------------------- auth -----------------------------
let authMode = 'login';
function setAuthMode(mode){
  authMode = mode;
  document.querySelectorAll('.auth-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.mode === mode));
  const isLogin = mode === 'login';
  $('auth-heading').textContent = isLogin ? 'Sign in' : 'Create account';
  $('auth-lead').textContent = isLogin
    ? 'Welcome back to sldchat'
    : 'Join sldchat in a few seconds';
  $('btn-auth').textContent = isLogin ? 'Sign in' : 'Create account';
  $('field-display').style.display = isLogin ? 'none' : '';
  $('pw-hint').style.display = isLogin ? 'none' : '';
  $('in-password').setAttribute('autocomplete', isLogin ? 'current-password' : 'new-password');
  $('auth-error').textContent = '';
}
document.querySelectorAll('.auth-tab').forEach(tab => {
  tab.addEventListener('click', () => setAuthMode(tab.dataset.mode));
});

$('auth-form').addEventListener('submit', async e => {
  e.preventDefault();
  const username = $('in-username').value.trim().toLowerCase();
  const password = $('in-password').value;
  const display  = $('in-display').value.trim();
  $('auth-error').textContent = '';
  $('btn-auth').disabled = true;
  const prev = $('btn-auth').textContent;
  $('btn-auth').textContent = authMode === 'login' ? 'Signing in…' : 'Creating…';
  try {
    let user;
    if(authMode === 'login'){
      user = await api('/api/login', {method:'POST',
        body: JSON.stringify({username, password})});
    } else {
      user = await api('/api/register', {method:'POST',
        body: JSON.stringify({username, display_name: display, password})});
    }
    me = user;
    startApp();
  } catch(err){
    $('auth-error').textContent = err.message;
    $('btn-auth').disabled = false;
    $('btn-auth').textContent = prev;
  }
});

// ----------------------------- app boot -----------------------------
function startApp(){
  $('login').style.display = 'none';
  $('app').style.display = 'block';
  updateDrawerUser();
  bindGlobalUI();
  connectWS();
  loadChats();
  loadContacts();
  loadContactRequests();
}

function updateDrawerUser(){
  if(!me) return;
  $('drawer-avatar').textContent = initials(me.display_name);
  $('drawer-name').textContent = me.display_name;
  $('drawer-username').textContent = '@' + me.username;
}

function bindGlobalUI(){
  $('btn-menu').onclick = () => openDrawer();
  $('drawer-backdrop').onclick = () => closeDrawer();
  document.querySelectorAll('.drawer-item').forEach(btn => {
    btn.addEventListener('click', () => {
      closeDrawer();
      setView(btn.dataset.view);
    });
  });

  $('btn-mobile-back').onclick = () => {
    $('main').classList.remove('mobile-open');
    currentChatId = null; currentChat = null;
  };

  $('send').onclick = sendMessage;
  const inp = $('input');
  inp.addEventListener('keydown', e => {
    if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendMessage(); }
  });
  inp.addEventListener('input', () => {
    inp.style.height = 'auto';
    inp.style.height = Math.min(inp.scrollHeight, 140) + 'px';
    if(currentChatId && ws && ws.readyState === 1 && !typingTimer){
      ws.send(JSON.stringify({type:'typing', chat_id: currentChatId}));
      typingTimer = setTimeout(() => typingTimer = null, 2200);
    }
  });

  window.addEventListener('resize', () => {
    if(!isMobile()) $('main').classList.remove('mobile-open');
  });
  window.addEventListener('keydown', e => {
    if(e.key === 'Escape'){ closeDrawer(); closeContactModal(); }
  });

  // contact modal
  $('contact-cancel').onclick = closeContactModal;
  $('contact-submit').onclick = submitContactRequest;
  $('contact-username').addEventListener('keydown', e => {
    if(e.key === 'Enter'){ e.preventDefault(); submitContactRequest(); }
  });
  $('contact-modal').addEventListener('click', e => {
    if(e.target === $('contact-modal')) closeContactModal();
  });
}

function openDrawer(){
  $('drawer').classList.add('open');
  $('drawer-backdrop').classList.add('open');
}
function closeDrawer(){
  $('drawer').classList.remove('open');
  $('drawer-backdrop').classList.remove('open');
}

// ----------------------------- views -----------------------------
function setView(v){
  view = v;
  const titles = {chats:'sldchat', notifications:'Notifications',
                  contacts:'Contacts', settings:'Settings', profile:'Profile'};
  $('sidebar-title').textContent = titles[v] || 'sldchat';
  document.querySelectorAll('.drawer-item').forEach(b =>
    b.classList.toggle('active', b.dataset.view === v));
  if(v === 'notifications'){ unreadNotif = 0; updateBadges(); }
  renderSidebar();
}

function updateBadges(){
  const pendingReqs = contactRequests.length;
  const notifBadge = $('nav-badge-notif');
  const contactsBadge = $('nav-badge-contacts');
  if(unreadNotif > 0){ notifBadge.style.display=''; notifBadge.textContent = unreadNotif > 99 ? '99+' : unreadNotif; }
  else { notifBadge.style.display='none'; }
  const totalContacts = pendingReqs;
  if(totalContacts > 0){ contactsBadge.style.display=''; contactsBadge.textContent = totalContacts; }
  else { contactsBadge.style.display='none'; }
}

function renderSidebar(){
  const el = $('sidebar-body');
  el.innerHTML = '';
  if(view === 'chats') return renderChatsView(el);
  if(view === 'notifications') return renderNotificationsView(el);
  if(view === 'contacts') return renderContactsView(el);
  if(view === 'settings') return renderSettingsView(el);
  if(view === 'profile') return renderProfileView(el);
}

// --- chats view ---
function renderChatsView(root){
  const searchWrap = document.createElement('div');
  searchWrap.className = 'search-wrap';
  searchWrap.innerHTML =
    '<div class="search-icon">' + ICONS.search + '</div>' +
    '<input id="search" placeholder="Search" autocomplete="off">';
  root.appendChild(searchWrap);

  const listWrap = document.createElement('div');
  listWrap.className = 'chat-list';
  listWrap.id = 'chat-list';
  root.appendChild(listWrap);

  const input = searchWrap.querySelector('input');
  input.value = searchQuery;
  input.addEventListener('input', () => {
    searchQuery = input.value.trim();
    renderChatList();
  });

  renderChatList();
}

async function renderChatList(){
  const el = $('chat-list');
  if(!el) return;
  el.innerHTML = '';

  if(searchQuery){
    const filtered = chats.filter(c =>
      c.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (c.username || '').toLowerCase().includes(searchQuery.toLowerCase())
    );
    if(filtered.length){
      const h = document.createElement('div');
      h.className = 'list-section'; h.textContent = 'Chats';
      el.appendChild(h);
      filtered.forEach(c => el.appendChild(chatItem(c)));
    }
    try {
      const users = await api('/api/users/search?q=' + encodeURIComponent(searchQuery));
      if(users.length){
        const h = document.createElement('div');
        h.className = 'list-section'; h.textContent = 'Users';
        el.appendChild(h);
        users.forEach(u => {
          const d = document.createElement('div');
          d.className = 'chat-item';
          d.innerHTML =
            '<div class="avatar" style="background:'+avatarColor(u.id)+'">'+escapeHtml(initials(u.display_name))+'</div>' +
            '<div class="chat-item-body">' +
              '<div class="chat-item-title">'+escapeHtml(u.display_name)+'</div>' +
              '<div class="chat-item-sub">@'+escapeHtml(u.username)+'</div>' +
            '</div>';
          d.onclick = () => startChat(u.id);
          el.appendChild(d);
        });
      }
    } catch(_){}
    if(!el.children.length){
      el.innerHTML = '<div class="empty">'+ICONS.search+'<div>Nothing found</div></div>';
    }
    return;
  }

  if(!chats.length){
    el.innerHTML =
      '<div class="empty">'+ICONS.chat +
      '<div>No chats yet</div>' +
      '<div style="margin-top:8px;font-size:12.5px">Use the search bar to find people</div>' +
      '</div>';
    return;
  }
  chats.forEach(c => el.appendChild(chatItem(c)));
}

function chatItem(c){
  const div = document.createElement('div');
  div.className = 'chat-item' + (c.id === currentChatId ? ' active' : '');
  const sub = c.last_message
    ? (c.last_message.length > 46 ? c.last_message.slice(0,46) + '…' : c.last_message)
    : (c.username ? '@' + c.username : 'No messages yet');
  div.innerHTML =
    '<div class="avatar" style="background:'+avatarColor(c.id)+'">'+escapeHtml(initials(c.title))+'</div>' +
    '<div class="chat-item-body">' +
      '<div class="chat-item-title">'+escapeHtml(c.title)+'</div>' +
      '<div class="chat-item-sub">'+escapeHtml(sub)+'</div>' +
    '</div>';
  div.onclick = () => openChat(c.id);
  return div;
}

async function startChat(userId){
  try {
    const r = await api('/api/chats', {method:'POST',
      body: JSON.stringify({user_id: userId})});
    searchQuery = '';
    const searchInput = $('search');
    if(searchInput) searchInput.value = '';
    await loadChats();
    openChat(r.chat_id);
  } catch(e){ toast(e.message, ICONS.x); }
}

// --- notifications ---
function renderNotificationsView(root){
  const wrap = document.createElement('div');
  wrap.className = 'sub-view';
  const head = document.createElement('div');
  head.className = 'sub-head';
  head.innerHTML = '<h2>Notifications</h2><p>' +
    (notifications.length ? notifications.length + ' recent' : 'You\'re all caught up') +
    '</p>';
  wrap.appendChild(head);

  const body = document.createElement('div');
  body.className = 'sub-body';

  if(!notifications.length){
    body.innerHTML =
      '<div class="empty">'+ICONS.bell+
      '<div>No notifications</div>' +
      '<div style="margin-top:8px;font-size:12.5px">Contact requests and updates appear here</div></div>';
  } else {
    notifications.forEach(n => {
      const d = document.createElement('div');
      d.className = 'chat-item';
      const icon = n.type === 'contact_request' ? ICONS.userPlus
                 : n.type === 'contact_accepted' ? ICONS.checkCircle
                 : ICONS.chat;
      d.innerHTML =
        '<div class="avatar" style="background:'+avatarColor(n.id)+'">'+
          (n.type === 'contact_request' || n.type === 'contact_accepted'
            ? escapeHtml(initials(n.name)) : icon) + '</div>' +
        '<div class="chat-item-body">' +
          '<div class="chat-item-title">'+escapeHtml(n.title)+'</div>' +
          '<div class="chat-item-sub">'+escapeHtml(n.text)+'</div>' +
        '</div>' +
        '<div style="font-size:11.5px;color:var(--muted);margin-left:6px">'+fmtTime(n.ts)+'</div>';
      if(n.type === 'contact_request'){ d.onclick = () => { setView('contacts'); }; }
      else if(n.chat_id){ d.onclick = () => { setView('chats'); openChat(n.chat_id); }; }
      body.appendChild(d);
    });
  }
  wrap.appendChild(body);
  root.appendChild(wrap);
}

// --- contacts ---
function renderContactsView(root){
  const wrap = document.createElement('div');
  wrap.className = 'sub-view';

  const head = document.createElement('div');
  head.className = 'sub-head';
  const pending = contactRequests.length;
  const count = contacts.length;
  head.innerHTML =
    '<div>' +
      '<h2>Contacts</h2>' +
      '<p>' + (count ? count + ' contact' + (count===1?'':'s') : 'No contacts yet') +
      (pending ? ' · ' + pending + ' pending' : '') + '</p>' +
    '</div>' +
    '<div class="sub-head-actions">' +
      '<button class="btn-mini" id="btn-add-contact" type="button">' +
        ICONS.userPlus + '<span>Add contact</span>' +
      '</button>' +
    '</div>';
  wrap.appendChild(head);
  head.querySelector('#btn-add-contact').onclick = () => openContactModal();

  const body = document.createElement('div');
  body.className = 'sub-body';

  // Pending requests
  if(contactRequests.length){
    const s = document.createElement('div');
    s.className = 'list-section';
    s.textContent = 'Requests';
    body.appendChild(s);
    contactRequests.forEach(r => body.appendChild(requestItem(r)));
  }

  // Contacts
  if(contacts.length){
    const s = document.createElement('div');
    s.className = 'list-section';
    s.textContent = 'All contacts';
    body.appendChild(s);
    contacts.forEach(u => body.appendChild(contactItem(u)));
  }

  if(!contacts.length && !contactRequests.length){
    body.innerHTML +=
      '<div class="empty">'+ICONS.users+
      '<div>No contacts yet</div>' +
      '<div style="margin-top:8px;font-size:12.5px">Tap "Add contact" to send a request</div></div>';
  }

  wrap.appendChild(body);
  root.appendChild(wrap);
}

function requestItem(r){
  const div = document.createElement('div');
  div.className = 'request-item';
  div.innerHTML =
    '<div class="avatar" style="background:'+avatarColor(r.from.id)+'">'+
      escapeHtml(initials(r.from.display_name))+'</div>' +
    '<div class="chat-item-body">' +
      '<div class="chat-item-title">'+escapeHtml(r.from.display_name)+'</div>' +
      '<div class="chat-item-sub">@'+escapeHtml(r.from.username)+'</div>' +
    '</div>' +
    '<div class="request-actions">' +
      '<button class="btn-mini accept" type="button">Accept</button>' +
      '<button class="btn-mini decline" type="button">Decline</button>' +
    '</div>';
  const [acceptBtn, declineBtn] = div.querySelectorAll('.request-actions button');
  acceptBtn.onclick = async (e) => {
    e.stopPropagation();
    acceptBtn.disabled = true; declineBtn.disabled = true;
    try {
      await api('/api/contacts/requests/' + r.id + '/accept', {method:'POST'});
      contactRequests = contactRequests.filter(x => x.id !== r.id);
      await loadContacts();
      updateBadges();
      if(view === 'contacts') renderSidebar();
      toast('Contact added', ICONS.checkCircle);
    } catch(err){
      toast(err.message, ICONS.x);
      acceptBtn.disabled = false; declineBtn.disabled = false;
    }
  };
  declineBtn.onclick = async (e) => {
    e.stopPropagation();
    acceptBtn.disabled = true; declineBtn.disabled = true;
    try {
      await api('/api/contacts/requests/' + r.id + '/decline', {method:'POST'});
      contactRequests = contactRequests.filter(x => x.id !== r.id);
      updateBadges();
      if(view === 'contacts') renderSidebar();
      toast('Request declined', ICONS.x);
    } catch(err){
      toast(err.message, ICONS.x);
      acceptBtn.disabled = false; declineBtn.disabled = false;
    }
  };
  return div;
}

function contactItem(u){
  const div = document.createElement('div');
  div.className = 'chat-item';
  div.innerHTML =
    '<div class="avatar" style="background:'+avatarColor(u.id)+'">'+
      escapeHtml(initials(u.display_name))+'</div>' +
    '<div class="chat-item-body">' +
      '<div class="chat-item-title">'+escapeHtml(u.display_name)+'</div>' +
      '<div class="chat-item-sub">@'+escapeHtml(u.username)+'</div>' +
    '</div>';
  div.onclick = () => startChat(u.id);
  return div;
}

// --- settings ---
function renderSettingsView(root){
  const wrap = document.createElement('div');
  wrap.className = 'sub-view';
  const head = document.createElement('div');
  head.className = 'sub-head';
  head.innerHTML = '<h2>Settings</h2><p>App preferences</p>';
  wrap.appendChild(head);

  const isDark = currentTheme() === 'dark';
  const body = document.createElement('div');
  body.className = 'sub-body';

  const themeRow = document.createElement('div');
  themeRow.className = 'sub-item clickable';
  themeRow.innerHTML =
    (isDark ? ICONS.moon : ICONS.sun) +
    '<div class="sub-item-body">' +
      '<div class="sub-item-title">Dark theme</div>' +
      '<div class="sub-item-sub">' + (isDark ? 'Enabled' : 'Disabled') + '</div>' +
    '</div>' +
    '<div class="switch' + (isDark ? ' on' : '') + '"></div>';
  themeRow.onclick = () => toggleTheme();
  body.appendChild(themeRow);

  body.innerHTML +=
    '<div class="sub-item">' +
      ICONS.bell +
      '<div class="sub-item-body"><div class="sub-item-title">Notifications</div>' +
      '<div class="sub-item-sub">In-app only</div></div>' +
    '</div>' +
    '<div class="sub-item">' +
      ICONS.chat +
      '<div class="sub-item-body"><div class="sub-item-title">Messages</div>' +
      '<div class="sub-item-sub">Text only · Realtime</div></div>' +
    '</div>';
  wrap.appendChild(body);

  const actions = document.createElement('div');
  actions.style.padding = '16px 18px';
  const btn = document.createElement('button');
  btn.className = 'btn-ghost danger';
  btn.innerHTML = ICONS.logout + '<span>Log out</span>';
  btn.onclick = doLogout;
  actions.appendChild(btn);
  wrap.appendChild(actions);
  root.appendChild(wrap);
}

// --- profile ---
function renderProfileView(root){
  const wrap = document.createElement('div');
  wrap.className = 'sub-view';
  const body = document.createElement('div');
  body.className = 'profile-view';

  const created = me.created_at ? new Date(me.created_at).toLocaleDateString() : '—';
  body.innerHTML =
    '<div class="profile-avatar" style="background:'+avatarColor(me.id)+'">'+escapeHtml(initials(me.display_name))+'</div>' +
    '<div class="profile-name" id="profile-name">'+escapeHtml(me.display_name)+'</div>' +
    '<div class="profile-username">@'+escapeHtml(me.username)+'</div>' +
    '<div class="profile-meta">Member since '+escapeHtml(created)+'</div>';

  const actions = document.createElement('div');
  actions.className = 'profile-actions';

  const editBtn = document.createElement('button');
  editBtn.className = 'btn-ghost';
  editBtn.innerHTML = ICONS.edit + '<span>Edit display name</span>';
  editBtn.onclick = async () => {
    const nv = prompt('New display name:', me.display_name);
    if(nv === null) return;
    const val = nv.trim();
    if(!val || val === me.display_name) return;
    try {
      const u = await api('/api/me', {method:'PATCH',
        body: JSON.stringify({display_name: val})});
      me.display_name = u.display_name;
      updateDrawerUser();
      renderProfileView(root);
      toast('Profile updated', ICONS.checkCircle);
    } catch(e){ toast(e.message, ICONS.x); }
  };
  actions.appendChild(editBtn);

  const outBtn = document.createElement('button');
  outBtn.className = 'btn-ghost danger';
  outBtn.innerHTML = ICONS.logout + '<span>Log out</span>';
  outBtn.onclick = doLogout;
  actions.appendChild(outBtn);

  body.appendChild(actions);
  wrap.appendChild(body);
  root.appendChild(wrap);
}

async function doLogout(){
  try { await api('/api/logout', {method:'POST'}); } catch(_){}
  location.reload();
}

// ----------------------------- contact modal -----------------------------
function openContactModal(){
  $('contact-username').value = '';
  $('contact-error').textContent = '';
  $('contact-modal').classList.add('open');
  setTimeout(() => $('contact-username').focus(), 80);
}
function closeContactModal(){
  $('contact-modal').classList.remove('open');
}
async function submitContactRequest(){
  const raw = $('contact-username').value.trim().replace(/^@/, '').toLowerCase();
  if(!raw){ $('contact-error').textContent = 'Enter a username'; return; }
  const btn = $('contact-submit');
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = 'Sending…';
  $('contact-error').textContent = '';
  try {
    await api('/api/contacts/request', {method:'POST',
      body: JSON.stringify({username: raw})});
    closeContactModal();
    toast('Request sent to @' + raw, ICONS.checkCircle);
  } catch(e){
    $('contact-error').textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// ----------------------------- contacts load -----------------------------
async function loadContacts(){
  try { contacts = await api('/api/contacts'); } catch(_) { contacts = []; }
  if(view === 'contacts') renderSidebar();
}
async function loadContactRequests(){
  try { contactRequests = await api('/api/contacts/requests'); } catch(_) { contactRequests = []; }
  updateBadges();
  if(view === 'contacts') renderSidebar();
}

// ----------------------------- ws -----------------------------
function connectWS(){
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onmessage = e => {
    let d; try { d = JSON.parse(e.data); } catch(_) { return; }
    if(d.type === 'message') handleIncoming(d.message);
    else if(d.type === 'chat_created') loadChats();
    else if(d.type === 'typing') showTyping(d);
    else if(d.type === 'contact_request') onContactRequest(d.request);
    else if(d.type === 'contact_accepted') onContactAccepted(d.contact);
    else if(d.type === 'contact_declined') onContactDeclined(d.by);
  };
  ws.onclose = () => setTimeout(connectWS, 1500);
}

function onContactRequest(req){
  contactRequests.unshift(req);
  updateBadges();
  if(view === 'contacts') renderSidebar();
  // push into notifications
  notifications.unshift({
    id: req.from.id,
    type: 'contact_request',
    name: req.from.display_name,
    title: req.from.display_name + ' wants to add you',
    text: '@' + req.from.username + ' · tap to review',
    ts: req.created_at,
  });
  if(view !== 'notifications') unreadNotif++;
  updateBadges();
  toast(req.from.display_name + ' sent a contact request', ICONS.userPlus);
}

function onContactAccepted(contact){
  contacts.push(contact);
  contacts.sort((a,b) => a.display_name.localeCompare(b.display_name));
  notifications.unshift({
    id: contact.id,
    type: 'contact_accepted',
    name: contact.display_name,
    title: contact.display_name + ' accepted your request',
    text: '@' + contact.username + ' is now in your contacts',
    ts: new Date().toISOString(),
  });
  if(view !== 'notifications') unreadNotif++;
  updateBadges();
  if(view === 'contacts') renderSidebar();
  toast(contact.display_name + ' accepted your request', ICONS.checkCircle);
}

function onContactDeclined(by){
  notifications.unshift({
    id: by.id,
    type: 'contact_declined',
    name: by.display_name,
    title: by.display_name + ' declined your request',
    text: '@' + by.username,
    ts: new Date().toISOString(),
  });
  if(view !== 'notifications') unreadNotif++;
  updateBadges();
  toast(by.display_name + ' declined your request', ICONS.x);
}

function showTyping(d){
  if(d.chat_id !== currentChatId) return;
  const el = $('chat-status');
  el.textContent = (d.display_name || d.username) + ' is typing…';
  clearTimeout(typingHideTimer);
  typingHideTimer = setTimeout(() => { el.textContent = ''; }, 3000);
}

function handleIncoming(msg){
  const cid = msg.chat_id;
  if(!messages[cid]) messages[cid] = [];
  if(!messages[cid].some(m => m.id === msg.id)) messages[cid].push(msg);

  if(cid === currentChatId){
    appendMessage(msg);
    scrollBottom();
  }
  const c = chats.find(x => x.id === cid);
  if(c){
    c.last_message = msg.text;
    c.last_message_at = msg.created_at;
    chats.sort((a,b) => (b.last_message_at||'').localeCompare(a.last_message_at||''));
    if(view === 'chats') renderChatList();
  } else {
    loadChats();
  }
}

// ----------------------------- chats -----------------------------
async function loadChats(){
  chats = await api('/api/chats');
  if(view === 'chats') renderChatList();
}

async function openChat(cid){
  currentChatId = cid;
  currentChat = chats.find(x => x.id === cid) || null;
  $('chat-title').textContent = currentChat ? currentChat.title : '';
  $('chat-status').textContent = '';
  $('input').disabled = false;
  $('send').disabled = false;
  $('input').focus();

  if(isMobile()) $('main').classList.add('mobile-open');

  if(view === 'chats') renderChatList();
  if(!messages[cid]){
    messages[cid] = await api('/api/chats/' + cid + '/messages');
  }
  renderMessages();
}

function renderMessages(){
  const el = $('messages');
  el.innerHTML = '';
  const list = messages[currentChatId] || [];
  if(!list.length){
    el.innerHTML = '<div class="no-chat">'+ICONS.chat+
                   '<div>No messages yet</div>' +
                   '<div style="margin-top:8px;font-size:12.5px">Say hi!</div></div>';
    return;
  }
  let lastDate = '';
  list.forEach(m => {
    const d = new Date(m.created_at).toDateString();
    if(d !== lastDate){
      lastDate = d;
      const sep = document.createElement('div');
      sep.className = 'date-sep';
      sep.textContent = fmtDate(m.created_at);
      el.appendChild(sep);
    }
    el.appendChild(msgEl(m));
  });
  scrollBottom();
}

function appendMessage(m){
  const el = $('messages');
  const emptyEl = el.querySelector('.no-chat');
  if(emptyEl) emptyEl.remove();
  const list = messages[currentChatId] || [];
  const prev = list[list.length - 1];
  const needSep = !prev ||
    new Date(prev.created_at).toDateString() !== new Date(m.created_at).toDateString();
  if(needSep){
    const s = document.createElement('div');
    s.className = 'date-sep';
    s.textContent = fmtDate(m.created_at);
    el.appendChild(s);
  }
  el.appendChild(msgEl(m));
}

function msgEl(m){
  const out = m.user_id === me.id;
  const div = document.createElement('div');
  div.className = 'msg ' + (out ? 'out' : 'in');
  let author = '';
  if(!out && currentChat && currentChat.is_group){
    author = '<div class="author">' + escapeHtml(m.display_name || m.username) + '</div>';
  }
  div.innerHTML =
    '<div class="bubble">' +
      author +
      '<div class="text">' + escapeHtml(m.text) + '</div>' +
      '<div class="time">' + fmtTime(m.created_at) + '</div>' +
    '</div>';
  return div;
}

function scrollBottom(){
  const el = $('messages');
  el.scrollTop = el.scrollHeight;
}

function sendMessage(){
  const inp = $('input');
  const text = inp.value.trim();
  if(!text || !currentChatId || !ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({type:'message', chat_id: currentChatId, text}));
  inp.value = '';
  inp.style.height = 'auto';
  typingTimer = null;
}

// ----------------------------- boot -----------------------------
(async () => {
  try {
    me = await api('/api/me');
    startApp();
  } catch(_) {
    setAuthMode('login');
  }
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
