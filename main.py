import os
import time
import secrets
import hashlib
from typing import Dict, List, Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

SERVER_NAME = os.environ.get("SLDCHAT_DOMAIN", "localhost:8000")

app = FastAPI(title="SLDCHAT Mail", version="0.4")

# ---------------------- RAM STORAGE ----------------------
USERS: Dict[str, Dict[str, Any]] = {}
MESSAGES: Dict[str, Dict[str, Any]] = {}
USER_STATE: Dict[str, Dict[str, Dict[str, Any]]] = {}
DRAFTS: Dict[str, Dict[str, Dict[str, Any]]] = {}
CONTACTS: Dict[str, List[str]] = {}


# ---------------------- Models ----------------------
class RegisterReq(BaseModel):
    username: str
    password: str


class LoginReq(BaseModel):
    username: str
    password: str


class SendReq(BaseModel):
    to: List[str] = []
    cc: List[str] = []
    bcc: List[str] = []
    subject: str = ""
    body: str = ""


class DraftReq(BaseModel):
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    body: str = ""


class FlagReq(BaseModel):
    read: Optional[bool] = None
    starred: Optional[bool] = None
    folder: Optional[str] = None


class ContactReq(BaseModel):
    address: str


class FederationMsg(BaseModel):
    msg_id: str
    from_addr: str
    to: List[str] = []
    cc: List[str] = []
    subject: str = ""
    body: str = ""
    ts: float
    deliver_to: str


# ---------------------- Utils ----------------------
def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def full(username: str) -> str:
    return f"{username}@{SERVER_NAME}"


def parse_addr(a: str) -> Optional[str]:
    if not a:
        return None
    a = a.strip().lower()
    if "@" not in a:
        return None
    local, _, domain = a.rpartition("@")
    if not local or not domain or "." not in domain:
        return None
    return f"{local}@{domain}"


def auth(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "unauthorized")
    tok = authorization[7:].strip()
    for u, d in USERS.items():
        if d.get("token") and secrets.compare_digest(d["token"], tok):
            return u
    raise HTTPException(401, "unauthorized")


# ---------------------- Discovery ----------------------
@app.get("/.well-known/sldchat")
async def well_known():
    return {"protocol": "sldchat-mail", "version": 1, "server": SERVER_NAME}


# ---------------------- Federation ----------------------
@app.post("/federation/receive")
async def federation_receive(msg: FederationMsg):
    to = msg.deliver_to.strip().lower()
    user, _, domain = to.rpartition("@")
    if domain != SERVER_NAME:
        raise HTTPException(400, "not for this server")
    if user not in USERS:
        raise HTTPException(404, f"no such user {user}")

    MESSAGES[msg.msg_id] = {
        "msg_id": msg.msg_id,
        "from_addr": msg.from_addr,
        "to": msg.to,
        "cc": msg.cc,
        "subject": msg.subject,
        "body": msg.body,
        "ts": msg.ts,
    }
    st = USER_STATE.setdefault(user, {})
    if msg.msg_id not in st:
        st[msg.msg_id] = {"read": False, "folder": "inbox", "starred": False}
    cl = CONTACTS.setdefault(user, [])
    if msg.from_addr not in cl:
        cl.append(msg.from_addr)
    return {"status": "ok"}


async def federate_http(domain: str, payload: dict) -> tuple[bool, str]:
    """follow_redirects=False, иначе POST становится GET на 301/302 и получаем 405."""
    last_err = ""
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/federation/receive"
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
                current = url
                r = None
                for _ in range(6):
                    r = await client.post(current, json=payload)
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("location")
                        if not loc:
                            break
                        current = str(httpx.URL(current).join(loc))
                        continue
                    break
            if r is None:
                last_err = "no response"
                continue
            if r.status_code < 400:
                return True, ""
            last_err = f"{r.status_code} {r.text[:300]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, last_err


def deliver_local(recipient_full: str, msg: dict) -> tuple[bool, str]:
    user, _, domain = recipient_full.rpartition("@")
    if user not in USERS:
        return False, f"нет пользователя {recipient_full}"
    MESSAGES[msg["msg_id"]] = {
        "msg_id": msg["msg_id"],
        "from_addr": msg["from_addr"],
        "to": msg["to"],
        "cc": msg["cc"],
        "subject": msg["subject"],
        "body": msg["body"],
        "ts": msg["ts"],
    }
    st = USER_STATE.setdefault(user, {})
    if msg["msg_id"] not in st:
        st[msg["msg_id"]] = {"read": False, "folder": "inbox", "starred": False}
    cl = CONTACTS.setdefault(user, [])
    if msg["from_addr"] not in cl:
        cl.append(msg["from_addr"])
    return True, ""


async def deliver(recipient_full: str, msg: dict) -> tuple[bool, str]:
    _, _, domain = recipient_full.rpartition("@")
    payload = dict(msg)
    payload["deliver_to"] = recipient_full
    if domain == SERVER_NAME:
        return deliver_local(recipient_full, payload)
    return await federate_http(domain, payload)


# ---------------------- User API ----------------------
@app.post("/api/register")
async def register(req: RegisterReq):
    u = req.username.strip().lower()
    if not u or not req.password:
        raise HTTPException(400, "Укажите логин и пароль.")
    if not u.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "Логин может содержать только буквы, цифры, _ - .")
    if u in USERS:
        raise HTTPException(409, "Такой логин уже занят.")
    salt = secrets.token_hex(8)
    USERS[u] = {"salt": salt, "pw_hash": hash_pw(req.password, salt), "token": ""}
    return {"status": "ok", "address": full(u), "server": SERVER_NAME}


@app.post("/api/login")
async def login(req: LoginReq):
    u = req.username.strip().lower()
    d = USERS.get(u)
    if not d or d["pw_hash"] != hash_pw(req.password, d["salt"]):
        raise HTTPException(401, "Неверный логин или пароль.")
    d["token"] = secrets.token_urlsafe(24)
    return {"status": "ok", "token": d["token"], "username": u, "server": SERVER_NAME}


@app.get("/api/me")
async def me(authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    return {"username": u, "server": SERVER_NAME, "address": full(u)}


@app.get("/api/folders")
async def folders(authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    counts = {
        "inbox":   {"total": 0, "unread": 0},
        "sent":    {"total": 0, "unread": 0},
        "drafts":  {"total": len(DRAFTS.get(u, {})), "unread": 0},
        "trash":   {"total": 0, "unread": 0},
        "archive": {"total": 0, "unread": 0},
        "starred": {"total": 0, "unread": 0},
    }
    for mid, s in USER_STATE.get(u, {}).items():
        f = s.get("folder", "inbox")
        if f in counts:
            counts[f]["total"] += 1
            if f == "inbox" and not s.get("read", False):
                counts[f]["unread"] += 1
        if s.get("starred"):
            counts["starred"]["total"] += 1
    return counts


@app.get("/api/list")
async def list_messages(
    folder: str = "inbox",
    q: str = "",
    page: int = 1,
    per_page: int = 30,
    authorization: Optional[str] = Header(None),
):
    u = auth(authorization)
    st = USER_STATE.get(u, {})
    q_lower = q.strip().lower()

    if folder == "drafts":
        items = sorted(DRAFTS.get(u, {}).values(), key=lambda x: -x["ts"])
        total = len(items)
        items = items[(page - 1) * per_page: page * per_page]
        return {"items": [
            {"msg_id": "draft:" + d["id"], "from": full(u),
             "to": [t for t in [d.get("to", "")] if t],
             "subject": d.get("subject", "") or "(черновик без темы)",
             "ts": d["ts"], "read": True, "starred": False,
             "preview": (d.get("body", "") or "").replace("\n", " ")[:120],
             "is_draft": True}
            for d in items
        ], "total": total, "page": page, "per_page": per_page, "folder": folder}

    rows = []
    for mid, s in st.items():
        if folder == "starred":
            if not s.get("starred"):
                continue
        else:
            if s.get("folder", "inbox") != folder:
                continue
        m = MESSAGES.get(mid)
        if not m:
            continue
        if q_lower:
            hay = " ".join([
                m.get("subject", ""), m.get("body", ""),
                m.get("from_addr", ""), " ".join(m.get("to", [])),
                " ".join(m.get("cc", [])),
            ]).lower()
            if q_lower not in hay:
                continue
        rows.append((mid, s, m))

    rows.sort(key=lambda x: -x[2]["ts"])
    total = len(rows)
    chunk = rows[(page - 1) * per_page: page * per_page]
    return {
        "items": [{
            "msg_id": mid,
            "from": m["from_addr"],
            "to": m.get("to", []),
            "subject": m.get("subject", "") or "(без темы)",
            "ts": m["ts"],
            "read": s.get("read", False),
            "starred": s.get("starred", False),
            "preview": (m.get("body", "") or "").replace("\n", " ")[:120],
        } for mid, s, m in chunk],
        "total": total, "page": page, "per_page": per_page, "folder": folder,
    }


@app.get("/api/message/{msg_id}")
async def get_message(msg_id: str, mark_read: bool = True, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    m = MESSAGES.get(msg_id)
    if not m:
        raise HTTPException(404, "Сообщение не найдено.")
    st = USER_STATE.get(u, {})
    if msg_id not in st:
        raise HTTPException(403, "Нет доступа к этому сообщению.")
    if mark_read:
        st[msg_id]["read"] = True
    return {
        "msg_id": msg_id,
        "from": m["from_addr"],
        "to": m.get("to", []),
        "cc": m.get("cc", []),
        "subject": m.get("subject", ""),
        "body": m.get("body", ""),
        "ts": m["ts"],
        "read": st[msg_id].get("read", False),
        "starred": st[msg_id].get("starred", False),
        "folder": st[msg_id].get("folder", "inbox"),
    }


@app.post("/api/message/{msg_id}/flags")
async def set_flags(msg_id: str, req: FlagReq, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    st = USER_STATE.get(u, {})
    if msg_id not in st:
        raise HTTPException(404, "Сообщение не найдено.")
    s = st[msg_id]
    if req.read is not None:
        s["read"] = req.read
    if req.starred is not None:
        s["starred"] = req.starred
    if req.folder is not None:
        if req.folder not in ("inbox", "sent", "trash", "archive"):
            raise HTTPException(400, "Неверная папка.")
        s["folder"] = req.folder
    return {"status": "ok", "state": s}


@app.post("/api/message/{msg_id}/delete")
async def delete_message(msg_id: str, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    st = USER_STATE.get(u, {})
    if msg_id not in st:
        raise HTTPException(404, "Сообщение не найдено.")
    if st[msg_id].get("folder") == "trash":
        del st[msg_id]
    else:
        st[msg_id]["folder"] = "trash"
    return {"status": "ok"}


@app.post("/api/send")
async def send(req: SendReq, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    to_list = [x for x in (parse_addr(a) for a in req.to) if x]
    cc_list = [x for x in (parse_addr(a) for a in req.cc) if x]
    bcc_list = [x for x in (parse_addr(a) for a in req.bcc) if x]
    if not (to_list or cc_list or bcc_list):
        raise HTTPException(400, "Укажите хотя бы одного получателя.")

    mid = secrets.token_hex(16)
    msg = {
        "msg_id": mid,
        "from_addr": full(u),
        "to": to_list,
        "cc": cc_list,
        "subject": req.subject,
        "body": req.body,
        "ts": time.time(),
    }
    MESSAGES[mid] = msg
    USER_STATE.setdefault(u, {})[mid] = {"read": True, "folder": "sent", "starred": False}

    errors: List[str] = []
    for r in to_list + cc_list + bcc_list:
        ok, err = await deliver(r, msg)
        if not ok:
            errors.append(f"{r}: {err}")

    cl = CONTACTS.setdefault(u, [])
    for r in to_list + cc_list + bcc_list:
        if r not in cl:
            cl.append(r)

    if errors:
        return {"status": "partial", "msg_id": mid, "errors": errors}
    return {"status": "ok", "msg_id": mid}


@app.get("/api/contacts")
async def get_contacts(authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    me = full(u)
    local = [full(x) for x in USERS if x != u]
    seen = set(CONTACTS.get(u, []))
    for mid in USER_STATE.get(u, {}).keys():
        m = MESSAGES.get(mid)
        if not m:
            continue
        if m["from_addr"] != me:
            seen.add(m["from_addr"])
        for r in m.get("to", []) + m.get("cc", []):
            if r != me:
                seen.add(r)
    return {"contacts": sorted(seen), "local_users": sorted(local)}


@app.post("/api/contacts")
async def add_contact(req: ContactReq, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    a = parse_addr(req.address)
    if not a:
        raise HTTPException(400, "Формат адреса: user@domain")
    cl = CONTACTS.setdefault(u, [])
    if a not in cl:
        cl.append(a)
    return {"status": "ok", "contacts": sorted(cl)}


# ---------------------- Drafts ----------------------
@app.post("/api/drafts")
async def save_draft(req: DraftReq, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    did = secrets.token_hex(8)
    DRAFTS.setdefault(u, {})[did] = {
        "id": did, "to": req.to, "cc": req.cc, "bcc": req.bcc,
        "subject": req.subject, "body": req.body, "ts": time.time(),
    }
    return {"status": "ok", "id": did}


@app.get("/api/drafts/{did}")
async def get_draft(did: str, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    d = DRAFTS.get(u, {}).get(did)
    if not d:
        raise HTTPException(404, "Черновик не найден.")
    return d


@app.delete("/api/drafts/{did}")
async def del_draft(did: str, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    DRAFTS.get(u, {}).pop(did, None)
    return {"status": "ok"}


# ---------------------- UI ----------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SLDCHAT MAIL</title>
<style>
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; min-height: 100%;
  font-family: Arial, Helvetica, Verdana, sans-serif;
  font-size: 14px; color: #000; background: #eef2f7;
}
a { color: #003399; text-decoration: none; cursor: pointer; }
a:hover { color: #cc3300; text-decoration: underline; }
input, textarea, button, select { font-family: inherit; font-size: 14px; }

/* ---------- buttons ---------- */
button, .btn {
  display: inline-block;
  background: #e8eef5; color: #00295f;
  border: 1px solid #7a8fa6; border-radius: 3px;
  padding: 6px 12px; cursor: pointer;
  line-height: 1.2; text-decoration: none; font-size: 13px;
}
button:hover, .btn:hover { background: #d3dfee; }
button.primary {
  background: #2e6fbf; color: #fff; border-color: #234f8a;
  font-weight: bold;
}
button.primary:hover { background: #245b9e; }
button.danger { color: #8a2020; border-color: #b08080; }
button.danger:hover { background: #fbe6e6; }
button.big {
  padding: 8px 18px; font-size: 14px;
}

/* ---------- header ---------- */
.header {
  background: #1a3e6e; color: #fff;
  padding: 8px 16px;
  display: flex; justify-content: space-between; align-items: center;
  border-bottom: 3px solid #0a2649;
}
.header .brand { font-size: 16px; font-weight: bold; letter-spacing: .5px; }
.header .user { font-size: 13px; }
.header .user a { color: #cce0ff; margin-left: 14px; }

/* ---------- top nav ---------- */
.topnav {
  background: #dbe4ef; border-bottom: 1px solid #a8b8cc;
  padding: 6px 12px;
  display: flex; gap: 6px; align-items: center;
}
.topnav .sep { flex: 1; }
.topnav button { padding: 6px 14px; }

/* ---------- layout ---------- */
.layout {
  display: grid;
  grid-template-columns: 190px 1fr;
  min-height: calc(100vh - 100px);
}
.sidebar {
  background: #d9e2ee; border-right: 1px solid #a8b8cc;
  padding: 10px 0;
}
.side-title {
  padding: 6px 16px 4px; font-size: 11px;
  text-transform: uppercase; color: #4a5a6e; letter-spacing: .07em;
}
.folder {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 16px; color: #00295f; font-size: 13px;
  border-left: 3px solid transparent;
  cursor: pointer;
}
.folder:hover { background: #c7d3e2; }
.folder.active {
  background: #fff; font-weight: bold;
  border-left-color: #1a3e6e;
}
.folder .cnt { color: #777; font-size: 12px; font-weight: normal; }
.folder .cnt.unread {
  background: #c33; color: #fff; font-weight: bold;
  padding: 1px 7px; border-radius: 9px; font-size: 11px;
}

/* ---------- content ---------- */
.content { background: #fff; }
.panel-toolbar {
  background: #f2f5f9; border-bottom: 1px solid #d5dfec;
  padding: 8px 12px;
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
}
.panel-toolbar .search {
  margin-left: auto; display: flex; gap: 6px; align-items: center;
}
.panel-toolbar .search input {
  padding: 5px 8px; border: 1px solid #7a8fa6; border-radius: 3px;
  width: 220px;
}

/* ---------- list ---------- */
table.list { width: 100%; border-collapse: collapse; }
table.list th {
  background: #e5ebf3; text-align: left;
  padding: 8px 10px; font-size: 12px; color: #3a4a5e;
  border-bottom: 1px solid #a8b8cc;
  border-right: 1px solid #d5dfec; font-weight: bold;
}
table.list th:last-child { border-right: 0; }
table.list td {
  padding: 9px 10px; font-size: 13px;
  border-bottom: 1px solid #eef2f7; vertical-align: middle;
}
table.list tr { cursor: pointer; }
table.list tr:hover td { background: #eef4fb; }
table.list tr.unread td { font-weight: bold; background: #f7faff; }
table.list tr.unread:hover td { background: #eef4fb; }
.col-star { width: 30px; text-align: center; }
.col-from { width: 22%; }
.col-date { width: 120px; white-space: nowrap; text-align: right; color: #555; font-size: 12px; }
.row-subject { display: block; }
.row-preview {
  color: #777; font-size: 12px; font-weight: normal;
  margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
tr.unread .row-preview { color: #444; }
.star-btn {
  color: #c9c9c9; font-size: 17px; line-height: 1;
  text-decoration: none; user-select: none;
}
.star-btn.on { color: #f0b400; }
.empty { padding: 40px; text-align: center; color: #777; }

.pager {
  padding: 10px 14px; font-size: 12px;
  background: #f2f5f9; border-top: 1px solid #d5dfec;
  display: flex; align-items: center; gap: 12px;
}
.pager a {
  padding: 3px 10px; border: 1px solid #7a8fa6; border-radius: 3px;
  background: #fff;
}
.pager a:hover { background: #e8eef5; text-decoration: none; }

/* ---------- read view ---------- */
.read { padding: 20px 26px; max-width: 900px; }
.read .subject {
  font-size: 20px; font-weight: bold; line-height: 1.25;
  padding-bottom: 12px; border-bottom: 1px solid #e0e6ee; margin-bottom: 14px;
}
.read .hdr {
  background: #f6f9fc; border: 1px solid #e0e6ee; border-radius: 3px;
  padding: 10px 14px; margin-bottom: 16px;
}
.read .hdr-row { padding: 3px 0; font-size: 13px; }
.read .hdr-row .lbl { color: #667; display: inline-block; width: 70px; }
.read .body {
  white-space: pre-wrap; word-wrap: break-word;
  font-size: 14px; line-height: 1.5;
  padding: 4px 0 20px;
  border-bottom: 1px solid #e0e6ee; margin-bottom: 20px;
  min-height: 80px;
}
.read .actions { display: flex; gap: 8px; flex-wrap: wrap; }

/* big action buttons at top of read view */
.read-head-actions {
  display: flex; gap: 8px; flex-wrap: wrap;
  padding: 12px 14px; background: #f2f5f9;
  border-bottom: 1px solid #d5dfec;
}

/* ---------- compose ---------- */
.compose { padding: 20px 26px; max-width: 900px; }
.compose h2 {
  margin: 0 0 16px 0; font-size: 16px; color: #1a3e6e;
  font-weight: bold;
}
.compose .row { display: flex; align-items: flex-start; margin-bottom: 8px; }
.compose .row label {
  width: 90px; padding-top: 7px; color: #555; font-size: 13px; flex-shrink: 0;
}
.compose .row .val { flex: 1; }
.compose input[type=text] {
  width: 100%; border: 1px solid #7a8fa6; border-radius: 3px;
  padding: 6px 9px;
}
.compose textarea {
  width: 100%; min-height: 260px; border: 1px solid #7a8fa6;
  border-radius: 3px; padding: 8px 10px; resize: vertical;
  font-family: "Segoe UI", Arial, sans-serif; font-size: 14px;
}
.compose .hint { color: #777; font-size: 12px; margin-top: 4px; }
.compose .buttons {
  margin-top: 16px; padding-top: 14px; border-top: 1px solid #e0e6ee;
  display: flex; gap: 8px; flex-wrap: wrap;
}

/* ---------- contacts ---------- */
.contacts { padding: 20px 26px; max-width: 900px; }
.contacts h2 {
  margin: 0 0 14px 0; font-size: 16px; color: #1a3e6e;
}
.contacts .addbox {
  background: #f6f9fc; border: 1px solid #e0e6ee; border-radius: 3px;
  padding: 12px; margin-bottom: 20px;
  display: flex; gap: 8px; align-items: center;
}
.contacts .addbox input {
  flex: 1; padding: 6px 9px; border: 1px solid #7a8fa6; border-radius: 3px;
}
.contacts .section {
  font-weight: bold; color: #1a3e6e; font-size: 13px;
  padding: 8px 0 6px; margin-top: 14px;
  border-bottom: 1px solid #e0e6ee;
}
.contacts table { width: 100%; border-collapse: collapse; margin-top: 8px; }
.contacts td { padding: 6px 4px; border-bottom: 1px solid #eef2f7; font-size: 13px; }
.contacts td.act { text-align: right; width: 130px; }
.contacts .empty { padding: 20px; color: #888; font-size: 13px; }

/* ---------- auth ---------- */
.auth-wrap {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 30px; background: #eef2f7;
}
.auth-box { width: 420px; background: #fff; border: 1px solid #7a8fa6; border-radius: 4px; overflow: hidden; }
.auth-box .top { background: #1a3e6e; color: #fff; padding: 12px 16px; font-weight: bold; }
.auth-box .body { padding: 20px; }
.auth-tabs { display: flex; border-bottom: 1px solid #d5dfec; margin-bottom: 16px; }
.auth-tab {
  padding: 8px 18px; cursor: pointer; color: #003399;
  border: 1px solid #d5dfec; border-bottom: 0; background: #f2f5f9;
  margin-right: 4px; margin-bottom: -1px; border-radius: 3px 3px 0 0;
}
.auth-tab.active { background: #fff; font-weight: bold; color: #1a3e6e; }
.auth-row { margin-bottom: 10px; display: flex; align-items: center; }
.auth-row label { width: 90px; color: #555; font-size: 13px; }
.auth-row input {
  flex: 1; padding: 6px 9px; border: 1px solid #7a8fa6; border-radius: 3px;
}
.auth-actions { margin-top: 16px; }
.notice { padding: 8px 12px; font-size: 13px; border-radius: 3px; margin-top: 12px; }
.notice.ok { background: #e8f4e0; border: 1px solid #a8cc88; color: #2a5a10; }
.notice.err { background: #fbe6e6; border: 1px solid #d09090; color: #802020; }

.footer { padding: 10px; font-size: 11px; color: #667; text-align: center; }
</style>
</head>
<body>

<!-- ============ AUTH ============ -->
<div id="auth_screen" class="auth-wrap">
  <div class="auth-box">
    <div class="top">SLDCHAT MAIL — вход</div>
    <div class="body">
      <div class="auth-tabs">
        <div id="tab_login" class="auth-tab active" onclick="switchTab('login')">Вход</div>
        <div id="tab_reg" class="auth-tab" onclick="switchTab('reg')">Регистрация</div>
      </div>
      <div id="form_login">
        <div class="auth-row"><label>Логин:</label><input id="log_user" autocomplete="username"></div>
        <div class="auth-row"><label>Пароль:</label><input id="log_pass" type="password" autocomplete="current-password"></div>
        <div class="auth-actions"><button class="primary big" onclick="doLogin()">Войти</button></div>
      </div>
      <div id="form_reg" style="display:none">
        <div class="auth-row"><label>Логин:</label><input id="reg_user" autocomplete="username"></div>
        <div class="auth-row"><label>Пароль:</label><input id="reg_pass" type="password" autocomplete="new-password"></div>
        <div class="auth-row"><label></label><span style="color:#777;font-size:12px">Ваш адрес: логин@__SERVER__</span></div>
        <div class="auth-actions"><button class="primary big" onclick="doRegister()">Создать ящик</button></div>
      </div>
      <div id="auth_status" class="notice" style="display:none"></div>
    </div>
  </div>
</div>

<!-- ============ APP ============ -->
<div id="app_screen" style="display:none">
  <div class="header">
    <div class="brand">✉ SLDCHAT MAIL</div>
    <div class="user">
      <span id="who">—</span>
      <a onclick="doLogout()">Выход</a>
    </div>
  </div>

  <div class="topnav">
    <button class="primary big" onclick="newCompose()">✎ Написать письмо</button>
    <button onclick="openFolder('inbox')">📥 Входящие</button>
    <button onclick="openFolder('sent')">📤 Отправленные</button>
    <button onclick="openFolder('drafts')">📝 Черновики</button>
    <span class="sep"></span>
    <button onclick="showContacts()">☎ Контакты</button>
  </div>

  <div class="layout">
    <div class="sidebar">
      <div class="side-title">Папки</div>
      <div id="folders"></div>
    </div>
    <div class="content" id="content"></div>
  </div>

  <div class="footer" id="footer"></div>
</div>

<script>
const SERVER_DOMAIN = "__SERVER__";
const FOLDERS = [
  ["inbox","Входящие"], ["sent","Отправленные"], ["drafts","Черновики"],
  ["starred","Помеченные"], ["archive","Архив"], ["trash","Удалённые"]
];
const FOLDER_TITLE = {
  inbox: "Входящие", sent: "Отправленные", drafts: "Черновики",
  starred: "Помеченные", archive: "Архив", trash: "Удалённые"
};

const S = {
  token: localStorage.getItem("sldchat_token") || "",
  username: localStorage.getItem("sldchat_user") || "",
  folder: "inbox",
  q: "",
  page: 1,
  view: "list",       // list | read | compose | contacts
  currentMsg: null,
  compose: null,
  pollTimer: null,
  folderCounts: {},
};

/* ---------- http ---------- */
async function api(path, opts = {}) {
  opts.headers = Object.assign({"Content-Type":"application/json"}, opts.headers || {});
  if (S.token) opts.headers["Authorization"] = "Bearer " + S.token;
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(data.detail || r.statusText);
    e.status = r.status;
    if (r.status === 401 && S.token) doLogout();
    throw e;
  }
  return data;
}

/* ---------- helpers ---------- */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function fmtDate(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const pad = n => String(n).padStart(2, "0");
  if (d.toDateString() === now.toDateString()) return pad(d.getHours()) + ":" + pad(d.getMinutes());
  const months = ["янв","фев","мар","апр","мая","июн","июл","авг","сен","окт","ноя","дек"];
  if (d.getFullYear() === now.getFullYear()) return d.getDate() + " " + months[d.getMonth()];
  return pad(d.getDate()) + "." + pad(d.getMonth()+1) + "." + d.getFullYear();
}
function fmtFull(ts) {
  const d = new Date(ts * 1000);
  const pad = n => String(n).padStart(2, "0");
  const months = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
  return pad(d.getDate()) + " " + months[d.getMonth()] + " " + d.getFullYear()
       + ", " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
function meAddr() { return S.username + "@" + SERVER_DOMAIN; }
function shortAddr(a) { return a ? a.replace(/@.*$/, "") : ""; }
function notice(el, msg, kind) {
  if (!msg) { el.style.display = "none"; return; }
  el.style.display = "block";
  el.className = "notice " + (kind || "err");
  el.textContent = msg;
}

/* ---------- auth ---------- */
function switchTab(t) {
  document.getElementById("tab_login").classList.toggle("active", t === "login");
  document.getElementById("tab_reg").classList.toggle("active", t === "reg");
  document.getElementById("form_login").style.display = t === "login" ? "" : "none";
  document.getElementById("form_reg").style.display = t === "reg" ? "" : "none";
  notice(document.getElementById("auth_status"), "");
}

async function doRegister() {
  const st = document.getElementById("auth_status");
  const u = document.getElementById("reg_user").value.trim();
  const p = document.getElementById("reg_pass").value;
  if (!u || !p) return notice(st, "Заполните оба поля.");
  try {
    const d = await api("/api/register", {method:"POST", body: JSON.stringify({username:u, password:p})});
    notice(st, "Ящик создан: " + d.address + ". Теперь войдите.", "ok");
    document.getElementById("log_user").value = u;
    switchTab("login");
  } catch (e) { notice(st, e.message); }
}

async function doLogin() {
  const st = document.getElementById("auth_status");
  const u = document.getElementById("log_user").value.trim();
  const p = document.getElementById("log_pass").value;
  if (!u || !p) return notice(st, "Введите логин и пароль.");
  try {
    const d = await api("/api/login", {method:"POST", body: JSON.stringify({username:u, password:p})});
    S.token = d.token; S.username = d.username;
    localStorage.setItem("sldchat_token", S.token);
    localStorage.setItem("sldchat_user", S.username);
    S.folder = "inbox"; S.page = 1; S.view = "list";
    renderShell();
    refreshFolders();
    openFolder("inbox");
    startPolling();
  } catch (e) { notice(st, e.message); }
}

function doLogout() {
  S.token = ""; S.username = ""; S.currentMsg = null;
  localStorage.removeItem("sldchat_token");
  localStorage.removeItem("sldchat_user");
  if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
  renderShell();
}

/* ---------- shell ---------- */
function renderShell() {
  const logged = !!S.token;
  document.getElementById("auth_screen").style.display = logged ? "none" : "flex";
  document.getElementById("app_screen").style.display = logged ? "" : "none";
  document.getElementById("who").textContent = logged ? meAddr() : "";
  document.getElementById("footer").textContent = "SLDCHAT MAIL — ваш сервер: " + SERVER_DOMAIN;
}

function renderFolders() {
  const box = document.getElementById("folders");
  box.innerHTML = FOLDERS.map(([key, label]) => {
    const c = S.folderCounts[key] || {total:0, unread:0};
    let right = "";
    if (key === "inbox" && c.unread > 0) {
      right = `<span class="cnt unread">${c.unread}</span>`;
    } else if (c.total > 0) {
      right = `<span class="cnt">${c.total}</span>`;
    }
    const active = (S.view === "list" && S.folder === key) ? " active" : "";
    return `<div class="folder${active}" onclick="openFolder('${key}')">
      <span>${label}</span>${right}
    </div>`;
  }).join("");
}

/* ---------- folder ---------- */
function openFolder(f) {
  S.folder = f;
  S.page = 1;
  S.view = "list";
  S.currentMsg = null;
  renderFolders();
  refreshList();
}

/* ---------- list ---------- */
async function refreshList() {
  if (S.view !== "list") return;
  renderFolders();
  const v = document.getElementById("content");

  const toolbar = `
    <div class="panel-toolbar">
      <button class="primary" onclick="newCompose()">✎ Написать</button>
      <button onclick="refreshList()">⟳ Обновить</button>
      ${S.folder === "trash" ? '<button class="danger" onclick="emptyTrash()">🗑 Очистить корзину</button>' : ""}
      <div class="search">
        <input id="q_input" placeholder="Поиск по письмам…" value="${esc(S.q)}"
               onkeydown="if(event.key==='Enter'){doSearch();}">
        <button onclick="doSearch()">Найти</button>
        ${S.q ? `<button onclick="clearSearch()">Сброс</button>` : ""}
      </div>
    </div>
  `;

  try {
    const params = new URLSearchParams({folder: S.folder, q: S.q, page: S.page, per_page: 30});
    const d = await api("/api/list?" + params.toString());
    const rows = d.items;
    const showTo = (S.folder === "sent" || S.folder === "drafts");

    const heading = `<div style="padding:12px 16px;border-bottom:1px solid #e0e6ee;background:#f9fbfd">
      <b style="color:#1a3e6e;font-size:15px">${FOLDER_TITLE[S.folder] || S.folder}</b>
      <span style="color:#777;font-size:12px;margin-left:10px">${d.total} писем${S.q ? " (поиск: "+esc(S.q)+")" : ""}</span>
    </div>`;

    let body;
    if (!rows.length) {
      body = `<div class="empty">Здесь пока пусто.</div>`;
    } else {
      body = `<table class="list">
        <thead>
          <tr>
            <th class="col-star"></th>
            <th class="col-from">${showTo ? "Кому" : "От кого"}</th>
            <th>Тема</th>
            <th class="col-date">Дата</th>
          </tr>
        </thead>
        <tbody>
        ${rows.map(r => {
          const cls = (!r.read && S.folder === "inbox") ? "unread" : "";
          const star = r.starred ? "★" : "☆";
          const starCls = r.starred ? "star-btn on" : "star-btn";
          const who = showTo
            ? (r.to.map(a => shortAddr(a)).join(", ") || "(нет получателя)")
            : shortAddr(r.from);
          const subj = r.is_draft ? "✎ " + r.subject : r.subject;
          return `<tr class="${cls}" onclick="openMessage('${r.msg_id}')">
            <td class="col-star" onclick="event.stopPropagation(); toggleStar('${r.msg_id}')">
              <span class="${starCls}">${star}</span>
            </td>
            <td class="col-from" title="${esc(r.from)}">${esc(who)}</td>
            <td>
              <span class="row-subject">${esc(subj)}</span>
              <div class="row-preview">${esc(r.preview)}</div>
            </td>
            <td class="col-date">${fmtDate(r.ts)}</td>
          </tr>`;
        }).join("")}
        </tbody>
      </table>`;
    }

    const totalPages = Math.max(1, Math.ceil(d.total / d.per_page));
    const pager = `<div class="pager">
      ${S.page > 1 ? `<a onclick="gotoPage(${S.page-1})">← Предыдущие</a>` : ""}
      <span>Страница ${S.page} из ${totalPages}</span>
      ${S.page < totalPages ? `<a onclick="gotoPage(${S.page+1})">Следующие →</a>` : ""}
    </div>`;

    v.innerHTML = toolbar + heading + body + pager;
    refreshFolders();
  } catch (e) {
    v.innerHTML = toolbar + `<div class="empty">Ошибка: ${esc(e.message)}</div>`;
  }
}

function doSearch() {
  const el = document.getElementById("q_input");
  S.q = el.value.trim();
  S.page = 1;
  refreshList();
}
function clearSearch() { S.q = ""; S.page = 1; refreshList(); }
function gotoPage(p) { S.page = p; refreshList(); }

async function toggleStar(msgId) {
  if (msgId.startsWith("draft:")) return;
  try {
    const m = await api("/api/message/" + msgId + "?mark_read=false");
    await api("/api/message/" + msgId + "/flags", {
      method: "POST", body: JSON.stringify({starred: !m.starred})
    });
    refreshList(); refreshFolders();
  } catch (e) { alert(e.message); }
}

/* ---------- read ---------- */
async function openMessage(msgId) {
  if (msgId.startsWith("draft:")) {
    const did = msgId.slice(6);
    try {
      const d = await api("/api/drafts/" + did);
      startCompose({
        to: d.to, cc: d.cc, bcc: d.bcc,
        subject: d.subject, body: d.body, draft_id: did,
      });
    } catch (e) { alert(e.message); }
    return;
  }
  try {
    const m = await api("/api/message/" + msgId);
    S.currentMsg = m;
    S.view = "read";
    renderRead();
    refreshFolders();
  } catch (e) { alert(e.message); }
}

function renderRead() {
  const v = document.getElementById("content");
  const m = S.currentMsg;
  if (!m) { v.innerHTML = ""; return; }

  const ccLine = m.cc && m.cc.length
    ? `<div class="hdr-row"><span class="lbl">Копия:</span> ${esc(m.cc.join(", "))}</div>`
    : "";

  v.innerHTML = `
    <div class="read-head-actions">
      <button class="primary big" onclick="replyCurrent(false)">↩ Ответить</button>
      <button onclick="replyCurrent(true)">↩↩ Ответить всем</button>
      <button onclick="forwardCurrent()">→ Переслать</button>
      <button onclick="openFolder(S.folder)">← К списку писем</button>
      <span style="flex:1"></span>
      <button onclick="toggleStarCurrent()">${m.starred ? "★ Снять метку" : "☆ Пометить"}</button>
      <button onclick="archiveCurrent()">В архив</button>
      <button class="danger" onclick="deleteCurrent()">🗑 Удалить</button>
    </div>
    <div class="read">
      <div class="subject">${esc(m.subject || "(без темы)")}</div>
      <div class="hdr">
        <div class="hdr-row"><span class="lbl">От:</span> <b>${esc(m.from)}</b></div>
        <div class="hdr-row"><span class="lbl">Кому:</span> ${esc(m.to.join(", "))}</div>
        ${ccLine}
        <div class="hdr-row"><span class="lbl">Дата:</span> ${fmtFull(m.ts)}</div>
      </div>
      <div class="body">${esc(m.body)}</div>
      <div class="actions">
        <button class="primary big" onclick="replyCurrent(false)">↩ Ответить</button>
        <button onclick="replyCurrent(true)">↩↩ Ответить всем</button>
        <button onclick="forwardCurrent()">→ Переслать</button>
      </div>
    </div>
  `;
}

function toggleStarCurrent() {
  const m = S.currentMsg;
  if (!m) return;
  api("/api/message/" + m.msg_id + "/flags", {method:"POST", body: JSON.stringify({starred: !m.starred})})
    .then(() => { m.starred = !m.starred; renderRead(); refreshFolders(); })
    .catch(e => alert(e.message));
}

function archiveCurrent() {
  const m = S.currentMsg;
  if (!m) return;
  api("/api/message/" + m.msg_id + "/flags", {method:"POST", body: JSON.stringify({folder: "archive"})})
    .then(() => openFolder(S.folder))
    .catch(e => alert(e.message));
}

function deleteCurrent() {
  const m = S.currentMsg;
  if (!m) return;
  if (!confirm("Удалить это сообщение?")) return;
  api("/api/message/" + m.msg_id + "/delete", {method:"POST"})
    .then(() => openFolder(S.folder))
    .catch(e => alert(e.message));
}

async function emptyTrash() {
  if (!confirm("Удалить все письма из корзины безвозвратно?")) return;
  try {
    const d = await api("/api/list?folder=trash&per_page=1000");
    for (const it of d.items) {
      await api("/api/message/" + it.msg_id + "/delete", {method:"POST"});
    }
    refreshList(); refreshFolders();
  } catch (e) { alert(e.message); }
}

/* ---------- compose ---------- */
function newCompose() {
  startCompose({to:"", cc:"", bcc:"", subject:"", body:""});
}

function startCompose(c) {
  S.compose = Object.assign({to:"", cc:"", bcc:"", subject:"", body:"", draft_id:null}, c);
  S.view = "compose";
  S.currentMsg = null;
  renderCompose();
}

function renderCompose() {
  const v = document.getElementById("content");
  const c = S.compose;
  const title = c.draft_id ? "Редактирование черновика" :
    (c.to ? "Ответ на письмо" : "Новое письмо");
  v.innerHTML = `
    <div class="compose">
      <h2>${esc(title)}</h2>
      <div class="row">
        <label>Кому:</label>
        <div class="val">
          <input type="text" id="c_to" value="${esc(c.to)}" placeholder="user@domain, user2@domain2">
          <div class="hint">Через запятую. Можно отправить на другой сервер, например: bob@other.example.com</div>
        </div>
      </div>
      <div class="row">
        <label>Копия:</label>
        <div class="val"><input type="text" id="c_cc" value="${esc(c.cc)}" placeholder="(необязательно)"></div>
      </div>
      <div class="row">
        <label>Скрытая:</label>
        <div class="val"><input type="text" id="c_bcc" value="${esc(c.bcc)}" placeholder="(необязательно)"></div>
      </div>
      <div class="row">
        <label>Тема:</label>
        <div class="val"><input type="text" id="c_subject" value="${esc(c.subject)}"></div>
      </div>
      <div class="row">
        <label>Текст:</label>
        <div class="val">
          <textarea id="c_body" placeholder="Текст письма…">${esc(c.body)}</textarea>
          <div class="hint">Подсказка: Ctrl+Enter — отправить сразу.</div>
        </div>
      </div>
      <div class="buttons">
        <button class="primary big" onclick="sendCompose()">✉ Отправить</button>
        <button onclick="saveDraft()">📝 Сохранить черновик</button>
        <button onclick="cancelCompose()">Отмена</button>
      </div>
    </div>`;

  const ta = document.getElementById("c_body");
  ta.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      sendCompose();
    }
  });
}

function cancelCompose() {
  S.compose = null;
  openFolder(S.folder || "inbox");
}

function splitAddrs(s) {
  return (s || "").split(/[,;\s]+/).map(x => x.trim()).filter(Boolean);
}

async function sendCompose() {
  const to = document.getElementById("c_to").value;
  const cc = document.getElementById("c_cc").value;
  const bcc = document.getElementById("c_bcc").value;
  const subject = document.getElementById("c_subject").value;
  const body = document.getElementById("c_body").value;
  const any = splitAddrs(to).length + splitAddrs(cc).length + splitAddrs(bcc).length;
  if (!any) {
    alert("Укажите получателя в поле «Кому».");
    document.getElementById("c_to").focus();
    return;
  }
  try {
    const d = await api("/api/send", {
      method: "POST",
      body: JSON.stringify({
        to: splitAddrs(to), cc: splitAddrs(cc), bcc: splitAddrs(bcc),
        subject, body,
      }),
    });
    if (S.compose && S.compose.draft_id) {
      await api("/api/drafts/" + S.compose.draft_id, {method:"DELETE"}).catch(()=>{});
    }
    if (d.status === "partial") {
      alert("Письмо отправлено частично. Не доставлено:\n" + d.errors.join("\n"));
    }
    S.compose = null;
    openFolder("sent");
  } catch (e) {
    alert("Ошибка отправки: " + e.message);
  }
}

async function saveDraft() {
  const to = document.getElementById("c_to").value;
  const cc = document.getElementById("c_cc").value;
  const bcc = document.getElementById("c_bcc").value;
  const subject = document.getElementById("c_subject").value;
  const body = document.getElementById("c_body").value;
  try {
    if (S.compose && S.compose.draft_id) {
      await api("/api/drafts/" + S.compose.draft_id, {method:"DELETE"}).catch(()=>{});
    }
    const d = await api("/api/drafts", {
      method: "POST",
      body: JSON.stringify({to, cc, bcc, subject, body}),
    });
    S.compose.draft_id = d.id;
    alert("Черновик сохранён. Найти его можно в папке «Черновики».");
    refreshFolders();
  } catch (e) { alert(e.message); }
}

/* ---------- reply / forward ---------- */
function quoteBody(m) {
  const quoted = (m.body || "").split("\n").map(l => "> " + l).join("\n");
  return "\n\n--- " + fmtFull(m.ts) + ", " + m.from + " пишет: ---\n" + quoted;
}
function replyCurrent(all) {
  const m = S.currentMsg;
  if (!m) return;
  const to = m.from;
  let cc = "";
  if (all) {
    const me = meAddr();
    cc = (m.to.concat(m.cc)).filter(a => a !== me && a !== to).join(", ");
  }
  const subj = /^Re:/i.test(m.subject || "") ? m.subject : "Re: " + (m.subject || "");
  startCompose({to, cc, bcc:"", subject: subj, body: quoteBody(m)});
}
function forwardCurrent() {
  const m = S.currentMsg;
  if (!m) return;
  const subj = /^Fwd:/i.test(m.subject || "") ? m.subject : "Fwd: " + (m.subject || "");
  const hdr = "---------- Пересылаемое сообщение ----------\n"
            + "От: " + m.from + "\n"
            + "Кому: " + m.to.join(", ") + "\n"
            + (m.cc && m.cc.length ? "Копия: " + m.cc.join(", ") + "\n" : "")
            + "Дата: " + fmtFull(m.ts) + "\n"
            + "Тема: " + (m.subject || "") + "\n\n";
  startCompose({to:"", cc:"", bcc:"", subject: subj, body: hdr + (m.body || "")});
}

/* ---------- contacts ---------- */
async function showContacts() {
  S.view = "contacts";
  S.currentMsg = null;
  renderFolders();
  const v = document.getElementById("content");
  try {
    const d = await api("/api/contacts");
    const localRows = d.local_users.length
      ? d.local_users.map(a => `
          <tr>
            <td><a onclick="composeTo('${esc(a)}')">${esc(a)}</a></td>
            <td class="act"><button onclick="composeTo('${esc(a)}')">✎ Написать</button></td>
          </tr>`).join("")
      : `<tr><td colspan="2" class="empty">На этом сервере пока нет других пользователей.</td></tr>`;
    const rows = d.contacts.length
      ? d.contacts.map(a => `
          <tr>
            <td><a onclick="composeTo('${esc(a)}')">${esc(a)}</a></td>
            <td class="act"><button onclick="composeTo('${esc(a)}')">✎ Написать</button></td>
          </tr>`).join("")
      : `<tr><td colspan="2" class="empty">Пока никого нет. Начните переписку или добавьте адрес вручную.</td></tr>`;

    v.innerHTML = `
      <div class="panel-toolbar">
        <button class="primary" onclick="newCompose()">✎ Написать письмо</button>
        <button onclick="openFolder(S.folder || 'inbox')">← К письмам</button>
      </div>
      <div class="contacts">
        <h2>Контакты</h2>
        <div class="addbox">
          <input id="new_contact" placeholder="user@domain — добавить адрес в контакты">
          <button onclick="addContactForm()">Добавить</button>
        </div>

        <div class="section">Пользователи сервера ${SERVER_DOMAIN}</div>
        <table>${localRows}</table>

        <div class="section">Все контакты</div>
        <table>${rows}</table>
      </div>`;
  } catch (e) {
    v.innerHTML = `<div class="empty">Ошибка: ${esc(e.message)}</div>`;
  }
}

function composeTo(a) {
  startCompose({to: a, cc:"", bcc:"", subject:"", body:""});
}

async function addContactForm() {
  const el = document.getElementById("new_contact");
  const v = (el.value || "").trim().toLowerCase();
  if (!v || v.indexOf("@") < 0) return alert("Формат: user@domain");
  try {
    await api("/api/contacts", {method:"POST", body: JSON.stringify({address: v})});
    el.value = "";
    showContacts();
  } catch (e) { alert(e.message); }
}

/* ---------- polling ---------- */
function startPolling() {
  if (S.pollTimer) clearInterval(S.pollTimer);
  S.pollTimer = setInterval(() => {
    if (!S.token) return;
    if (S.view === "list") { refreshFolders(); refreshList(); }
    else { refreshFolders(); }
  }, 15000);
}

/* ---------- init ---------- */
renderShell();
if (S.token) {
  refreshFolders();
  openFolder("inbox");
  startPolling();
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE.replace("__SERVER__", SERVER_NAME)
