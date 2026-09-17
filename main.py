import os
import time
import secrets
import hashlib
import base64
from typing import Dict, List, Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

SERVER_NAME = os.environ.get("SLDCHAT_DOMAIN", "localhost:8000")
MAX_TOTAL_ATTACH = int(os.environ.get("SLDCHAT_MAX_TOTAL_ATTACH", 8 * 1024 * 1024))

app = FastAPI(title="SLDCHAT Mail", version="0.3")

# ---------------------- RAM STORAGE ----------------------
USERS: Dict[str, Dict[str, Any]] = {}                       # username -> {salt, pw_hash, token}
MESSAGES: Dict[str, Dict[str, Any]] = {}                    # msg_id -> message
USER_STATE: Dict[str, Dict[str, Dict[str, Any]]] = {}       # username -> msg_id -> {read, folder, starred}
DRAFTS: Dict[str, Dict[str, Dict[str, Any]]] = {}           # username -> draft_id -> draft
CONTACTS: Dict[str, List[str]] = {}                         # username -> [address, ...]


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
    attachments: List[Dict[str, Any]] = []


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


class FederationMsg(BaseModel):
    msg_id: str
    from_addr: str
    to: List[str] = []
    cc: List[str] = []
    subject: str = ""
    body: str = ""
    ts: float
    attachments: List[Dict[str, Any]] = []
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
        "attachments": msg.attachments,
    }
    st = USER_STATE.setdefault(user, {})
    if msg.msg_id not in st:
        st[msg.msg_id] = {"read": False, "folder": "inbox", "starred": False}
    cl = CONTACTS.setdefault(user, [])
    if msg.from_addr not in cl:
        cl.append(msg.from_addr)
    return {"status": "ok"}


async def federate_http(domain: str, payload: dict) -> tuple[bool, str]:
    """
    follow_redirects=False — иначе httpx превращает POST в GET на 301/302,
    и удалённый /federation/receive отдаёт 405.
    """
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
        "attachments": msg["attachments"],
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
        raise HTTPException(400, "укажите логин и пароль")
    if not u.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "логин: только буквы/цифры/_-. ")
    if u in USERS:
        raise HTTPException(409, "логин занят")
    salt = secrets.token_hex(8)
    USERS[u] = {"salt": salt, "pw_hash": hash_pw(req.password, salt), "token": ""}
    return {"status": "ok", "address": full(u), "server": SERVER_NAME}


@app.post("/api/login")
async def login(req: LoginReq):
    u = req.username.strip().lower()
    d = USERS.get(u)
    if not d or d["pw_hash"] != hash_pw(req.password, d["salt"]):
        raise HTTPException(401, "неверный логин/пароль")
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
    rows = []

    if folder == "drafts":
        items = sorted(DRAFTS.get(u, {}).values(), key=lambda x: -x["ts"])
        items = items[(page - 1) * per_page: page * per_page]
        return {"items": [
            {"msg_id": "draft:" + d["id"], "from": full(u), "to": [t for t in [d.get("to", "")] if t],
             "subject": d.get("subject", "") or "(черновик)", "ts": d["ts"],
             "read": True, "starred": False, "has_attach": False,
             "preview": (d.get("body", "") or "")[:120], "is_draft": True}
            for d in items
        ], "total": len(DRAFTS.get(u, {})), "page": page, "per_page": per_page, "folder": folder}

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
                m.get("subject", ""),
                m.get("body", ""),
                m.get("from_addr", ""),
                " ".join(m.get("to", [])),
                " ".join(m.get("cc", [])),
            ]).lower()
            if q_lower not in hay:
                continue
        rows.append((mid, s, m))

    rows.sort(key=lambda x: -x[2]["ts"])
    total = len(rows)
    chunk = rows[(page - 1) * per_page: page * per_page]
    return {
        "items": [
            {
                "msg_id": mid,
                "from": m["from_addr"],
                "to": m.get("to", []),
                "subject": m.get("subject", "") or "(без темы)",
                "ts": m["ts"],
                "read": s.get("read", False),
                "starred": s.get("starred", False),
                "has_attach": bool(m.get("attachments")),
                "preview": (m.get("body", "") or "")[:120],
            }
            for mid, s, m in chunk
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
        "folder": folder,
    }


@app.get("/api/message/{msg_id}")
async def get_message(msg_id: str, mark_read: bool = True, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    m = MESSAGES.get(msg_id)
    if not m:
        raise HTTPException(404, "сообщение не найдено")
    st = USER_STATE.get(u, {})
    if msg_id not in st:
        raise HTTPException(403, "нет доступа")
    if mark_read:
        st[msg_id]["read"] = True
    atts = []
    for i, a in enumerate(m.get("attachments", [])):
        raw = a.get("data", "") or ""
        atts.append({
            "idx": i,
            "name": a.get("name", "file"),
            "type": a.get("type", "application/octet-stream"),
            "size": len(raw) * 3 // 4,
        })
    return {
        "msg_id": msg_id,
        "from": m["from_addr"],
        "to": m.get("to", []),
        "cc": m.get("cc", []),
        "subject": m.get("subject", ""),
        "body": m.get("body", ""),
        "ts": m["ts"],
        "attachments": atts,
        "read": st[msg_id].get("read", False),
        "starred": st[msg_id].get("starred", False),
        "folder": st[msg_id].get("folder", "inbox"),
    }


@app.post("/api/message/{msg_id}/flags")
async def set_flags(msg_id: str, req: FlagReq, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    st = USER_STATE.get(u, {})
    if msg_id not in st:
        raise HTTPException(404, "не найдено")
    s = st[msg_id]
    if req.read is not None:
        s["read"] = req.read
    if req.starred is not None:
        s["starred"] = req.starred
    if req.folder is not None:
        if req.folder not in ("inbox", "sent", "trash", "archive"):
            raise HTTPException(400, "неверная папка")
        s["folder"] = req.folder
    return {"status": "ok", "state": s}


@app.post("/api/message/{msg_id}/delete")
async def delete_message(msg_id: str, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    st = USER_STATE.get(u, {})
    if msg_id not in st:
        raise HTTPException(404, "не найдено")
    if st[msg_id].get("folder") == "trash":
        del st[msg_id]
    else:
        st[msg_id]["folder"] = "trash"
    return {"status": "ok"}


@app.get("/api/attachment/{msg_id}/{idx}")
async def get_attachment(msg_id: str, idx: int, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    if msg_id not in USER_STATE.get(u, {}):
        raise HTTPException(403, "нет доступа")
    m = MESSAGES.get(msg_id)
    if not m:
        raise HTTPException(404, "не найдено")
    atts = m.get("attachments", [])
    if idx < 0 or idx >= len(atts):
        raise HTTPException(404, "нет вложения")
    a = atts[idx]
    try:
        data = base64.b64decode(a.get("data", "") or "")
    except Exception:
        raise HTTPException(400, "битые данные вложения")
    fname = (a.get("name") or "file").replace('"', "")
    return Response(
        content=data,
        media_type=a.get("type", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/send")
async def send(req: SendReq, authorization: Optional[str] = Header(None)):
    u = auth(authorization)

    to_list = [x for x in (parse_addr(a) for a in req.to) if x]
    cc_list = [x for x in (parse_addr(a) for a in req.cc) if x]
    bcc_list = [x for x in (parse_addr(a) for a in req.bcc) if x]
    if not (to_list or cc_list or bcc_list):
        raise HTTPException(400, "укажите хотя бы одного получателя")

    total_b64 = sum(len((a.get("data") or "")) for a in req.attachments)
    if total_b64 * 3 // 4 > MAX_TOTAL_ATTACH:
        raise HTTPException(413, f"суммарный размер вложений превышает {MAX_TOTAL_ATTACH // 1024} КБ")

    mid = secrets.token_hex(16)
    msg = {
        "msg_id": mid,
        "from_addr": full(u),
        "to": to_list,
        "cc": cc_list,
        "subject": req.subject,
        "body": req.body,
        "ts": time.time(),
        "attachments": req.attachments,
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


# ---------------------- Drafts ----------------------
@app.post("/api/drafts")
async def save_draft(req: DraftReq, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    did = secrets.token_hex(8)
    DRAFTS.setdefault(u, {})[did] = {
        "id": did,
        "to": req.to, "cc": req.cc, "bcc": req.bcc,
        "subject": req.subject, "body": req.body,
        "ts": time.time(),
    }
    return {"status": "ok", "id": did}


@app.get("/api/drafts/{did}")
async def get_draft(did: str, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    d = DRAFTS.get(u, {}).get(did)
    if not d:
        raise HTTPException(404, "черновик не найден")
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
  margin: 0; padding: 0;
  font-family: Arial, Helvetica, Verdana, sans-serif;
  font-size: 13px; color: #000; background: #dfe6ee;
}
a { color: #003399; text-decoration: underline; cursor: pointer; }
a:hover { color: #cc3300; }
input, textarea, button, select {
  font-family: inherit; font-size: 13px;
}
button, .btn {
  background: #e8eef5; color: #003399;
  border: 1px solid #7a8fa6; padding: 2px 9px;
  cursor: pointer; text-decoration: none;
  display: inline-block;
}
button:hover, .btn:hover { background: #cfdbe9; }
button:disabled { color: #888; cursor: default; }

.header {
  background: #1a3e6e; color: #fff;
  padding: 6px 12px; font-size: 15px; font-weight: bold;
  border-bottom: 3px solid #0a2649;
}
.header .right { float: right; font-weight: normal; font-size: 12px; padding-top: 3px; }
.header .right a { color: #cce0ff; }
.header .right span { margin-right: 12px; }

.layout { width: 100%; border-collapse: collapse; }
.layout > tbody > tr > td { vertical-align: top; }
.sidebar { width: 172px; background: #d5dfec; border-right: 1px solid #8fa3b8; padding: 8px 0; }
.side-title { padding: 4px 12px; font-size: 11px; text-transform: uppercase; color: #4a5a6e; letter-spacing: .06em; }
.folder {
  display: block; padding: 3px 12px; color: #003399;
  text-decoration: none; border-bottom: 1px solid #c0ccd9; font-size: 12px;
}
.folder:hover { background: #c3d0e0; color: #003399; }
.folder.active { background: #fff; font-weight: bold; border-left: 3px solid #1a3e6e; padding-left: 9px; }
.folder .cnt { float: right; color: #666; font-weight: normal; }
.folder .unread { color: #b03030; font-weight: bold; }
.sidebar .sep { height: 12px; border-bottom: 1px solid #b0c0d0; margin: 6px 12px; }

.content { background: #fff; }
.toolbar {
  background: #c8d5e3; border-bottom: 1px solid #8899aa;
  padding: 5px 8px;
}
.toolbar button, .toolbar .btn { margin-right: 4px; }
.toolbar .sep { display: inline-block; width: 1px; height: 18px; background: #8899aa;
                vertical-align: middle; margin: 0 6px; }
.toolbar .search { float: right; }
.toolbar .search input { padding: 2px 4px; border: 1px solid #7a8fa6; width: 180px; }

table.list { width: 100%; border-collapse: collapse; }
table.list th {
  background: #c8d5e3; text-align: left; padding: 4px 6px;
  border-bottom: 1px solid #8899aa; font-size: 12px;
  border-right: 1px solid #b0bfd0;
}
table.list td { padding: 4px 6px; border-bottom: 1px solid #e0e8f0; font-size: 13px; vertical-align: top; }
table.list tr.unread td { font-weight: bold; background: #f5f9ff; }
table.list tr:hover td { background: #eef4fb; }
table.list tr.starred-row td.starcell a { color: #cc9900; }
.starcell { width: 22px; text-align: center; }
.starcell a { text-decoration: none; font-size: 15px; }
.col-from { width: 22%; }
.col-date { width: 110px; white-space: nowrap; text-align: right; color: #555; }
.col-subj { }
.attach-icon { color: #666; font-size: 12px; margin-left: 4px; }
.preview { color: #777; font-size: 12px; }
.unread .preview { color: #333; }
.empty { padding: 30px; text-align: center; color: #777; }
.pager { padding: 6px 10px; font-size: 12px; background: #eef4fb; border-top: 1px solid #c8d5e3; }
.pager a, .pager span { margin-right: 10px; }

.view { padding: 12px 16px; }
.hdr-table { width: 100%; border-collapse: collapse; margin-bottom: 12px; }
.hdr-table td { padding: 2px 0; font-size: 12px; vertical-align: top; }
.hdr-table .lbl { width: 60px; color: #666; }
.subject-line { font-size: 16px; font-weight: bold; border-bottom: 1px solid #ccc; padding: 6px 0 8px; margin-bottom: 10px; }
.msg-body {
  white-space: pre-wrap; font-size: 13px;
  border-top: 1px dashed #ccc; border-bottom: 1px dashed #ccc;
  padding: 12px 0; margin: 8px 0 14px; min-height: 60px;
}
.att-list { margin: 8px 0; padding: 8px; background: #f4f7fb; border: 1px solid #d5e0ec; font-size: 12px; }
.att-list a { margin-right: 12px; }
.actions { margin-top: 10px; }
.actions button { margin-right: 6px; }

.compose { padding: 8px 10px; }
.compose table { width: 100%; border-collapse: collapse; }
.compose td { padding: 3px 4px; vertical-align: top; }
.compose .lbl { width: 90px; text-align: right; color: #555; font-size: 12px; padding-right: 6px; padding-top: 6px; }
.compose input[type=text] {
  width: 100%; border: 1px solid #7a8fa6; padding: 3px 5px;
}
.compose textarea {
  width: 100%; min-height: 280px; border: 1px solid #7a8fa6;
  padding: 5px; font-family: "Courier New", monospace; font-size: 13px;
}
.compose .attach-row { font-size: 12px; }
.compose .att-chip { display: inline-block; background: #eef4fb; border: 1px solid #c8d5e3;
                     padding: 2px 6px; margin: 2px 4px 2px 0; }
.compose .att-chip a { color: #b03030; text-decoration: none; margin-left: 6px; }
.compose-buttons { margin-top: 8px; }
.compose-buttons button { margin-right: 6px; }

.contacts table { width: 100%; border-collapse: collapse; }
.contacts th { background: #c8d5e3; text-align: left; padding: 4px 6px; border-bottom: 1px solid #8899aa; font-size: 12px; }
.contacts td { padding: 4px 6px; border-bottom: 1px solid #e0e8f0; }
.contacts .newform { padding: 8px; background: #eef4fb; border-bottom: 1px solid #c8d5e3; }
.contacts .newform input { padding: 3px 5px; border: 1px solid #7a8fa6; width: 260px; }

/* auth */
.auth-wrap {
  min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 30px;
}
.auth-box {
  width: 420px; background: #fff; border: 1px solid #7a8fa6;
}
.auth-box .top {
  background: #1a3e6e; color: #fff; font-weight: bold; padding: 8px 12px;
}
.auth-box .body { padding: 18px; }
.auth-box .tabs { border-bottom: 1px solid #c0ccd9; margin-bottom: 14px; }
.auth-box .tab {
  display: inline-block; padding: 6px 14px; cursor: pointer;
  border: 1px solid #c0ccd9; border-bottom: none; background: #eef4fb;
  color: #003399; margin-right: 4px; position: relative; top: 1px;
}
.auth-box .tab.active { background: #fff; font-weight: bold; }
.auth-box .row { margin-bottom: 8px; }
.auth-box .row label { display: inline-block; width: 90px; color: #555; }
.auth-box .row input { padding: 3px 5px; border: 1px solid #7a8fa6; width: 250px; }
.auth-box .actions { margin-top: 14px; }
.notice { padding: 6px 10px; font-size: 12px; margin-top: 10px; }
.notice.ok { background: #e8f4e0; border: 1px solid #a8cc88; color: #2a5a10; }
.notice.err { background: #fbe6e6; border: 1px solid #d09090; color: #802020; }

.footer { padding: 6px 12px; font-size: 11px; color: #667; text-align: center; }
</style>
</head>
<body>

<div class="header">
  SLDCHAT MAIL
  <span class="right">
    <span id="who">не авторизован</span>
    <a id="logout_link" style="display:none" onclick="doLogout()">Выход</a>
  </span>
</div>

<div id="auth_screen" class="auth-wrap">
  <div class="auth-box">
    <div class="top">Вход в почту</div>
    <div class="body">
      <div class="tabs">
        <div id="tab_login" class="tab active" onclick="switchTab('login')">Вход</div>
        <div id="tab_reg" class="tab" onclick="switchTab('reg')">Регистрация</div>
      </div>
      <div id="form_login">
        <div class="row"><label>Логин:</label><input id="log_user" autocomplete="username"></div>
        <div class="row"><label>Пароль:</label><input id="log_pass" type="password" autocomplete="current-password"></div>
        <div class="actions"><button onclick="doLogin()">Войти</button></div>
      </div>
      <div id="form_reg" style="display:none">
        <div class="row"><label>Логин:</label><input id="reg_user" autocomplete="username"></div>
        <div class="row"><label>Пароль:</label><input id="reg_pass" type="password" autocomplete="new-password"></div>
        <div class="row"><label>&nbsp;</label><span style="color:#777;font-size:12px">сервер: __SERVER__</span></div>
        <div class="actions"><button onclick="doRegister()">Создать ящик</button></div>
      </div>
      <div id="auth_status" class="notice" style="display:none"></div>
    </div>
  </div>
</div>

<div id="main_screen" style="display:none">
  <table class="layout"><tbody><tr>
    <td class="sidebar">
      <div class="side-title">Папки</div>
      <div id="folders"></div>
      <div class="sep"></div>
      <a class="folder" onclick="showContacts()">☎ Контакты</a>
    </td>
    <td class="content">
      <div class="toolbar" id="toolbar"></div>
      <div id="view"></div>
    </td>
  </tr></tbody></table>
</div>

<div class="footer" id="footer"></div>

<script>
const SERVER_DOMAIN = "__SERVER__";
const FOLDERS = [
  ["inbox","Входящие"], ["sent","Отправленные"], ["drafts","Черновики"],
  ["starred","Помеченные"], ["archive","Архив"], ["trash","Удалённые"]
];

const S = {
  token: localStorage.getItem("sldchat_token") || "",
  username: localStorage.getItem("sldchat_user") || "",
  folder: "inbox",
  q: "",
  page: 1,
  view: "list",              // list | read | compose | contacts
  currentMsg: null,
  compose: null,             // {to, cc, bcc, subject, body, draft_id}
  attachments: [],
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
  return pad(d.getDate()) + "." + pad(d.getMonth()+1) + "." + d.getFullYear()
       + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " Б";
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + " КБ";
  return (bytes/(1024*1024)).toFixed(2) + " МБ";
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
  if (!u || !p) return notice(st, "заполните поля");
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
  if (!u || !p) return notice(st, "заполните поля");
  try {
    const d = await api("/api/login", {method:"POST", body: JSON.stringify({username:u, password:p})});
    S.token = d.token; S.username = d.username;
    localStorage.setItem("sldchat_token", S.token);
    localStorage.setItem("sldchat_user", S.username);
    S.folder = "inbox"; S.page = 1; S.view = "list";
    renderShell();
    refreshFolders(); refreshList();
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
  document.getElementById("auth_screen").style.display = logged ? "none" : "";
  document.getElementById("main_screen").style.display = logged ? "" : "none";
  document.getElementById("who").textContent = logged ? meAddr() : "не авторизован";
  document.getElementById("logout_link").style.display = logged ? "" : "none";
  document.getElementById("footer").textContent = "SLDCHAT MAIL 0.3 — сервер " + SERVER_DOMAIN;
}

function renderFolders() {
  const box = document.getElementById("folders");
  box.innerHTML = FOLDERS.map(([key, label]) => {
    const c = S.folderCounts[key] || {total:0, unread:0};
    let right = "";
    if (key === "inbox") {
      right = c.unread > 0
        ? `<span class="cnt unread">${c.unread}</span>`
        : (c.total > 0 ? `<span class="cnt">${c.total}</span>` : "");
    } else if (c.total > 0) {
      right = `<span class="cnt">${c.total}</span>`;
    }
    const active = (S.view !== "contacts" && S.folder === key) ? " active" : "";
    return `<a class="folder${active}" onclick="openFolder('${key}')">${label}${right}</a>`;
  }).join("");
}

function renderToolbar() {
  const t = document.getElementById("toolbar");
  if (S.view === "contacts") {
    t.innerHTML = `<button onclick="openFolder(S.folder)">← К папкам</button>`;
    return;
  }
  if (S.view === "compose") {
    t.innerHTML = `<button onclick="cancelCompose()">← Отмена</button>`;
    return;
  }
  if (S.view === "read") {
    const mid = S.currentMsg ? S.currentMsg.msg_id : "";
    t.innerHTML = `
      <button onclick="openFolder(S.folder)">← К списку</button>
      <span class="sep"></span>
      <button onclick="replyCurrent(false)">Ответить</button>
      <button onclick="replyCurrent(true)">Ответить всем</button>
      <button onclick="forwardCurrent()">Переслать</button>
      <span class="sep"></span>
      <button onclick="toggleStarCurrent()">${S.currentMsg && S.currentMsg.starred ? "Снять метку" : "Пометить"}</button>
      <button onclick="toggleUnreadCurrent()">${S.currentMsg && S.currentMsg.read ? "Пометить непрочитанным" : "Пометить прочитанным"}</button>
      <span class="sep"></span>
      <button onclick="archiveCurrent()">В архив</button>
      <button onclick="deleteCurrent()">Удалить</button>
    `;
    return;
  }
  // list view
  const folder = S.folder;
  let extra = "";
  if (folder === "trash") extra = `<button onclick="emptyTrash()">Очистить корзину</button>`;
  t.innerHTML = `
    <button onclick="newCompose()">Написать</button>
    <button onclick="refreshList()">Обновить</button>
    <span class="sep"></span>
    ${extra}
    <span class="search">
      <input id="q_input" placeholder="поиск…" value="${esc(S.q)}"
             onkeydown="if(event.key==='Enter'){S.q=this.value;S.page=1;refreshList();}">
      <button onclick="S.q=document.getElementById('q_input').value;S.page=1;refreshList()">Найти</button>
      ${S.q ? `<button onclick="S.q='';S.page=1;refreshList()">Сброс</button>` : ""}
    </span>
  `;
}

/* ---------- folders ---------- */
async function refreshFolders() {
  try {
    S.folderCounts = await api("/api/folders");
    renderFolders();
  } catch (e) { /* ignore */ }
}

function openFolder(f) {
  S.folder = f;
  S.page = 1;
  S.view = "list";
  S.currentMsg = null;
  renderShell(); renderFolders(); renderToolbar();
  refreshList();
}

/* ---------- list ---------- */
async function refreshList() {
  if (S.view !== "list") return;
  const v = document.getElementById("view");
  try {
    const params = new URLSearchParams({folder: S.folder, q: S.q, page: S.page, per_page: 30});
    const d = await api("/api/list?" + params.toString());
    const rows = d.items;

    const showTo = (S.folder === "sent" || S.folder === "drafts");
    const headers = `
      <tr>
        <th class="starcell"></th>
        <th class="col-from">${showTo ? "Кому" : "От кого"}</th>
        <th class="col-subj">Тема</th>
        <th class="col-date">Дата</th>
      </tr>`;

    let body;
    if (!rows.length) {
      body = `<tr><td colspan="4" class="empty">Пусто.</td></tr>`;
    } else {
      body = rows.map(r => {
        const cls = (!r.read && S.folder === "inbox") ? "unread" : "";
        const starCls = r.starred ? "starred-row" : "";
        const starChar = r.starred ? "★" : "☆";
        const attach = r.has_attach ? `<span class="attach-icon">📎</span>` : "";
        const who = showTo ? (r.to.join(", ") || "(нет получателя)") : r.from;
        const whoShort = who.split(",").map(a => shortAddr(a.trim())).join(", ");
        const subj = r.is_draft ? "✎ " + r.subject : r.subject;
        return `
          <tr class="${cls} ${starCls}">
            <td class="starcell"><a onclick="event.stopPropagation();toggleStar('${r.msg_id}')">${starChar}</a></td>
            <td class="col-from" title="${esc(who)}">${esc(whoShort)}</td>
            <td class="col-subj">
              <a onclick="openMessage('${r.msg_id}')">${esc(subj)}</a>
              ${attach}
              <div class="preview">${esc(r.preview)}</div>
            </td>
            <td class="col-date">${fmtDate(r.ts)}</td>
          </tr>`;
      }).join("");
    }

    let pager = "";
    const totalPages = Math.max(1, Math.ceil(d.total / d.per_page));
    if (totalPages > 1) {
      pager = `<div class="pager">
        ${S.page > 1 ? `<a onclick="gotoPage(${S.page-1})">« Пред</a>` : ""}
        <span>Стр. ${S.page} из ${totalPages} (${d.total})</span>
        ${S.page < totalPages ? `<a onclick="gotoPage(${S.page+1})">След »</a>` : ""}
      </div>`;
    } else {
      pager = `<div class="pager">Всего: ${d.total}</div>`;
    }

    v.innerHTML = `<table class="list">${headers}${body}</table>${pager}`;
    refreshFolders();
  } catch (e) {
    v.innerHTML = `<div class="empty">Ошибка: ${esc(e.message)}</div>`;
  }
}

function gotoPage(p) { S.page = p; refreshList(); }

async function toggleStar(msgId) {
  try {
    if (msgId.startsWith("draft:")) return;
    const cur = S.currentMsg && S.currentMsg.msg_id === msgId ? S.currentMsg.starred : undefined;
    // Для списка — узнаем по item через /api/message (без mark_read)
    const m = await api("/api/message/" + msgId + "?mark_read=false");
    await api("/api/message/" + msgId + "/flags", {method:"POST", body: JSON.stringify({starred: !m.starred})});
    refreshList(); refreshFolders();
  } catch (e) { alert(e.message); }
}

/* ---------- read view ---------- */
async function openMessage(msgId) {
  if (msgId.startsWith("draft:")) {
    const did = msgId.slice(6);
    try {
      const d = await api("/api/drafts/" + did);
      startCompose({
        to: d.to, cc: d.cc, bcc: d.bcc,
        subject: d.subject, body: d.body,
        draft_id: did,
      });
    } catch (e) { alert(e.message); }
    return;
  }
  try {
    const m = await api("/api/message/" + msgId);
    S.currentMsg = m;
    S.view = "read";
    renderToolbar();
    renderRead();
    refreshFolders();
  } catch (e) { alert(e.message); }
}

function renderRead() {
  const v = document.getElementById("view");
  const m = S.currentMsg;
  if (!m) { v.innerHTML = ""; return; }
  const atts = m.attachments.length
    ? `<div class="att-list"><b>Вложения:</b><br>${m.attachments.map(a =>
        `<a href="/api/attachment/${m.msg_id}/${a.idx}" target="_blank">📎 ${esc(a.name)}</a>
         <span style="color:#777">(${fmtSize(a.size)})</span>`).join(" &nbsp; ")}</div>`
    : "";
  const ccLine = m.cc && m.cc.length ? `<tr><td class="lbl">Копия:</td><td>${esc(m.cc.join(", "))}</td></tr>` : "";
  v.innerHTML = `
    <div class="view">
      <div class="subject-line">${esc(m.subject || "(без темы)")}</div>
      <table class="hdr-table">
        <tr><td class="lbl">От:</td><td><b>${esc(m.from)}</b></td></tr>
        <tr><td class="lbl">Кому:</td><td>${esc(m.to.join(", "))}</td></tr>
        ${ccLine}
        <tr><td class="lbl">Дата:</td><td>${fmtFull(m.ts)}</td></tr>
      </table>
      <div class="msg-body">${esc(m.body)}</div>
      ${atts}
      <div class="actions">
        <button onclick="replyCurrent(false)">Ответить</button>
        <button onclick="replyCurrent(true)">Ответить всем</button>
        <button onclick="forwardCurrent()">Переслать</button>
      </div>
    </div>`;
}

function toggleStarCurrent() {
  const m = S.currentMsg;
  if (!m) return;
  api("/api/message/" + m.msg_id + "/flags", {method:"POST", body: JSON.stringify({starred: !m.starred})})
    .then(() => { m.starred = !m.starred; renderToolbar(); refreshFolders(); })
    .catch(e => alert(e.message));
}

function toggleUnreadCurrent() {
  const m = S.currentMsg;
  if (!m) return;
  api("/api/message/" + m.msg_id + "/flags", {method:"POST", body: JSON.stringify({read: !m.read})})
    .then(() => { m.read = !m.read; renderToolbar(); refreshFolders(); })
    .catch(e => alert(e.message));
}

function archiveCurrent() {
  const m = S.currentMsg;
  if (!m) return;
  api("/api/message/" + m.msg_id + "/flags", {method:"POST", body: JSON.stringify({folder: "archive"})})
    .then(() => { openFolder(S.folder); })
    .catch(e => alert(e.message));
}

function deleteCurrent() {
  const m = S.currentMsg;
  if (!m) return;
  if (!confirm("Удалить сообщение?")) return;
  api("/api/message/" + m.msg_id + "/delete", {method:"POST"})
    .then(() => { openFolder(S.folder); })
    .catch(e => alert(e.message));
}

async function emptyTrash() {
  if (!confirm("Удалить все сообщения из корзины?")) return;
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
  S.attachments = [];
  S.view = "compose";
  renderToolbar();
  renderCompose();
}

function cancelCompose() {
  if (S.compose && (S.compose.body || S.compose.subject || S.compose.to)) {
    if (!confirm("Закрыть без сохранения? Нажмите Отмена — черновик сохранится отдельно кнопкой.")) {}
  }
  S.view = "list";
  S.compose = null;
  renderToolbar();
  refreshList();
}

function renderCompose() {
  const v = document.getElementById("view");
  const c = S.compose;
  v.innerHTML = `
    <div class="compose">
      <table>
        <tr><td class="lbl">Кому:</td>
            <td><input type="text" id="c_to" value="${esc(c.to)}" placeholder="user@domain, user2@domain2"></td></tr>
        <tr><td class="lbl">Копия:</td>
            <td><input type="text" id="c_cc" value="${esc(c.cc)}"></td></tr>
        <tr><td class="lbl">Скрытая:</td>
            <td><input type="text" id="c_bcc" value="${esc(c.bcc)}"></td></tr>
        <tr><td class="lbl">Тема:</td>
            <td><input type="text" id="c_subject" value="${esc(c.subject)}"></td></tr>
        <tr><td class="lbl">Текст:</td>
            <td><textarea id="c_body">${esc(c.body)}</textarea></td></tr>
        <tr><td class="lbl">Файлы:</td>
            <td class="attach-row">
              <input type="file" id="c_files" multiple onchange="addFiles(this)">
              <div id="att_chips"></div>
            </td></tr>
      </table>
      <div class="compose-buttons">
        <button onclick="sendCompose()">Отправить</button>
        <button onclick="saveDraft()">Сохранить черновик</button>
        <button onclick="cancelCompose()">Отмена</button>
        <span style="color:#777;font-size:12px;margin-left:10px">Ctrl+Enter — отправить</span>
      </div>
    </div>`;
  renderAttachments();
  const ta = document.getElementById("c_body");
  ta.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      sendCompose();
    }
  });
}

function addFiles(input) {
  const files = Array.from(input.files || []);
  const maxOne = 2 * 1024 * 1024;
  const cur = S.attachments.reduce((s, a) => s + (a.data.length * 3 / 4), 0);
  let total = cur;
  for (const f of files) {
    if (f.size > maxOne) { alert("Файл слишком большой: " + f.name); continue; }
    total += f.size;
    if (total > 8 * 1024 * 1024) { alert("Сумма вложений превышает 8 МБ"); break; }
    const reader = new FileReader();
    reader.onload = () => {
      const b64 = String(reader.result).split(",")[1] || "";
      S.attachments.push({name: f.name, type: f.type || "application/octet-stream", data: b64});
      renderAttachments();
    };
    reader.readAsDataURL(f);
  }
  input.value = "";
}

function renderAttachments() {
  const el = document.getElementById("att_chips");
  if (!el) return;
  el.innerHTML = S.attachments.map((a, i) =>
    `<span class="att-chip">📎 ${esc(a.name)} <a onclick="removeAttach(${i})">✕</a></span>`
  ).join("");
}

function removeAttach(i) { S.attachments.splice(i, 1); renderAttachments(); }

function splitAddrs(s) {
  return (s || "").split(/[,;\s]+/).map(x => x.trim()).filter(Boolean);
}

async function sendCompose() {
  const to = document.getElementById("c_to").value;
  const cc = document.getElementById("c_cc").value;
  const bcc = document.getElementById("c_bcc").value;
  const subject = document.getElementById("c_subject").value;
  const body = document.getElementById("c_body").value;
  if (!splitAddrs(to).length && !splitAddrs(cc).length && !splitAddrs(bcc).length) {
    alert("Укажите получателя."); return;
  }
  try {
    const d = await api("/api/send", {
      method: "POST",
      body: JSON.stringify({
        to: splitAddrs(to), cc: splitAddrs(cc), bcc: splitAddrs(bcc),
        subject, body, attachments: S.attachments,
      }),
    });
    if (S.compose.draft_id) {
      await api("/api/drafts/" + S.compose.draft_id, {method:"DELETE"}).catch(()=>{});
    }
    if (d.status === "partial") {
      alert("Отправлено частично. Ошибки:\n" + d.errors.join("\n"));
    }
    S.compose = null; S.attachments = [];
    openFolder("sent");
  } catch (e) { alert("Ошибка отправки: " + e.message); }
}

async function saveDraft() {
  const to = document.getElementById("c_to").value;
  const cc = document.getElementById("c_cc").value;
  const bcc = document.getElementById("c_bcc").value;
  const subject = document.getElementById("c_subject").value;
  const body = document.getElementById("c_body").value;
  try {
    if (S.compose.draft_id) {
      await api("/api/drafts/" + S.compose.draft_id, {method:"DELETE"}).catch(()=>{});
    }
    const d = await api("/api/drafts", {
      method: "POST",
      body: JSON.stringify({to, cc, bcc, subject, body}),
    });
    S.compose.draft_id = d.id;
    alert("Черновик сохранён.");
    refreshFolders();
  } catch (e) { alert(e.message); }
}

/* ---------- reply / forward ---------- */
function quoteBody(m) {
  const quoted = (m.body || "").split("\n").map(l => "> " + l).join("\n");
  return "\n\n\n--- " + fmtFull(m.ts) + ", " + m.from + " пишет: ---\n" + quoted;
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
  const subj = (m.subject || "").match(/^Re:/i) ? m.subject : "Re: " + (m.subject || "");
  startCompose({to, cc, bcc:"", subject: subj, body: quoteBody(m)});
}
function forwardCurrent() {
  const m = S.currentMsg;
  if (!m) return;
  const subj = (m.subject || "").match(/^Fwd:/i) ? m.subject : "Fwd: " + (m.subject || "");
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
  renderToolbar();
  const v = document.getElementById("view");
  try {
    const d = await api("/api/contacts");
    const rows = d.contacts.length
      ? d.contacts.map(a => `
          <tr>
            <td><a onclick="composeTo('${esc(a)}')">${esc(a)}</a></td>
            <td style="width:120px">
              <button onclick="composeTo('${esc(a)}')">Написать</button>
            </td>
          </tr>`).join("")
      : `<tr><td colspan="2" style="color:#777;padding:20px;text-align:center">Пусто. Начните переписку или добавьте адрес вручную.</td></tr>`;
    const localRows = d.local_users.length
      ? d.local_users.map(a => `
          <tr>
            <td><a onclick="composeTo('${esc(a)}')">${esc(a)}</a></td>
            <td><button onclick="composeTo('${esc(a)}')">Написать</button></td>
          </tr>`).join("")
      : `<tr><td colspan="2" style="color:#777;padding:10px">— нет других пользователей на этом сервере —</td></tr>`;
    v.innerHTML = `
      <div class="contacts">
        <div class="newform">
          Добавить адрес:
          <input id="new_contact" placeholder="user@domain">
          <button onclick="addContactForm()">Добавить</button>
        </div>
        <div style="padding:8px 10px;background:#f4f7fb;border-bottom:1px solid #c8d5e3;font-weight:bold">На этом сервере (${SERVER_DOMAIN})</div>
        <table>${localRows}</table>
        <div style="padding:8px 10px;background:#f4f7fb;border-bottom:1px solid #c8d5e3;font-weight:bold">Все контакты</div>
        <table>${rows}</table>
      </div>`;
  } catch (e) {
    v.innerHTML = `<div class="empty">Ошибка: ${esc(e.message)}</div>`;
  }
}

function composeTo(a) {
  S.view = "list"; // чтобы toolbar сбросился
  startCompose({to: a, cc:"", bcc:"", subject:"", body:""});
}

async function addContactForm() {
  const el = document.getElementById("new_contact");
  const v = (el.value || "").trim().toLowerCase();
  if (!v || v.indexOf("@") < 0) return alert("Формат: user@domain");
  try {
    // сохраняем через невидимый триггер — просто добавим в контакты отправив черновик-заглушку? Нет — используем contacts через message send не нужно.
    // Простейший способ: использовать /api/contacts POST не реализован — эмулируем через сохранение в localStorage? Не подходит.
    // Реализовано на сервере через no-op: см. ниже. Если эндпоинт отсутствует — просто добавим в поле "Кому" при следующем compose.
    // Для простоты и надёжности — локально запомним и обновим позже. Но лучше: эндпоинт не нужен, эта строка необязательна.
    // Итог: временно отправляем через серверный неявный путь — прикрепим к контактам путём создания черновика НЕ надо.
    // Просто вызовем /api/contacts POST — реализовано ниже в main.py (см. правку).
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
  renderShell();
  refreshFolders();
  refreshList();
  startPolling();
}
</script>
</body>
</html>
"""


# Эндпоинт добавления контакта (используется UI)
class ContactReq(BaseModel):
    address: str


@app.post("/api/contacts")
async def add_contact(req: ContactReq, authorization: Optional[str] = Header(None)):
    u = auth(authorization)
    a = parse_addr(req.address)
    if not a:
        raise HTTPException(400, "формат: user@domain")
    cl = CONTACTS.setdefault(u, [])
    if a not in cl:
        cl.append(a)
    return {"status": "ok", "contacts": sorted(cl)}


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE.replace("__SERVER__", SERVER_NAME)
