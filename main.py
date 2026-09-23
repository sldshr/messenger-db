"""
Anonymous Imageboard (4chan-style)
Python + FastAPI + Uvicorn
Everything in-memory.
"""

import time
import re
import hashlib
import html as _html
from typing import Optional
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.exceptions import HTTPException as FastAPIHTTPException
import uvicorn

# ==================== CONFIG ====================
SALT = "please_change_this_salt_12345"
ADMIN_KEY = "admin_secret_change_me"
MAX_THREADS_PER_BOARD = 50
MAX_REPLIES_PER_THREAD = 300
RATE_LIMIT_SEC = 5
MAX_CONTENT_LEN = 4000
HOST = "0.0.0.0"
PORT = 8000

# ==================== STORAGE ====================
boards: dict = {}
threads: dict = {}
posts: dict = {}
_counter = {"n": 0}
rate_map: dict = {}

# ==================== HELPERS ====================
def now_str() -> str:
    return time.strftime("%y/%m/%d(%a)%H:%M:%S", time.localtime())

def hash_ip(ip: str) -> str:
    return hashlib.sha256((SALT + ip).encode()).hexdigest()[:16]

def hash_pw(pw: str) -> str:
    return hashlib.sha256((SALT + pw).encode()).hexdigest()

def esc(s: str) -> str:
    return _html.escape(s or "", quote=True)

def get_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"

def check_rate(ip: str) -> bool:
    t = rate_map.get(ip, 0)
    if time.time() - t < RATE_LIMIT_SEC:
        return False
    rate_map[ip] = time.time()
    return True

def new_id() -> int:
    _counter["n"] += 1
    return _counter["n"]

def format_content(text: str) -> str:
    if not text:
        return ""
    text = esc(text)
    lines = text.split("\n")
    out = []
    for line in lines:
        line = re.sub(r'&gt;&gt;(\d+)',
                      r'<a href="#p\1" class="quotelink" onclick="quotePost(\1);return false;">&gt;&gt;\1</a>',
                      line)
        if line.startswith("&gt;") and not line.startswith("&gt;&gt;") and "<a" not in line[:4]:
            line = f'<span class="quote">{line}</span>'
        out.append(line)
    return "<br>".join(out)

def create_board(slug: str, name: str, desc: str) -> None:
    boards[slug] = {"slug": slug, "name": name, "desc": desc, "threads": []}

def purge_old_threads(board_slug: str) -> None:
    b = boards[board_slug]
    if len(b["threads"]) <= MAX_THREADS_PER_BOARD:
        return
    nonsticky = [tid for tid in b["threads"]
                 if tid in threads and not threads[tid].get("sticky")]
    nonsticky.sort(key=lambda tid: threads[tid]["bumped"])
    while len(b["threads"]) > MAX_THREADS_PER_BOARD and nonsticky:
        tid = nonsticky.pop(0)
        t = threads.pop(tid, None)
        if t:
            for pid in t["posts"]:
                posts.pop(pid, None)
            if tid in b["threads"]:
                b["threads"].remove(tid)

# ==================== APP ====================
app = FastAPI(title="Anonymous Imageboard", docs_url=None, redoc_url=None)

CSS = """
*{box-sizing:border-box}
body{background:#FFFFEE;color:#800000;font-family:arial,helvetica,sans-serif;font-size:10pt;margin:0;padding:0}
a{color:#0000EE;text-decoration:none}
a:hover{color:#DD0000}
a.quotelink{color:#DD0000;text-decoration:underline;cursor:pointer}
.header{text-align:center;padding:10px 8px 4px}
.header h1{color:#AF0A0F;font-family:Tahoma,sans-serif;font-size:30px;margin:0;letter-spacing:1px}
.header .sub{color:#800000;font-size:9pt;margin-top:4px}
.navbar{background:#FEDCBA;padding:5px 8px;text-align:center;border-top:1px solid #D9BFB7;border-bottom:1px solid #D9BFB7}
.navbar a{margin:0 5px;font-weight:700}
.boardtitle{text-align:center;color:#AF0A0F;font-size:22px;font-family:Tahoma,sans-serif;font-weight:700;padding:12px 10px 4px}
.boarddesc{text-align:center;color:#800000;padding-bottom:8px;font-size:9pt}
.form-box{background:#F0E0D6;border:1px solid #D9BFB7;padding:8px 10px;margin:10px auto;max-width:750px}
.post{background:#F0E0D6;border:1px solid #D9BFB7;padding:6px 8px;margin:4px auto;max-width:900px;word-wrap:break-word}
.post.reply{margin-left:40px}
.post.deleted{opacity:.6;font-style:italic}
.posthead{font-size:10pt;margin-bottom:4px}
.postername{color:#117743;font-weight:700}
.postdate{color:#800000;margin-left:4px}
.postnum{color:#800000;margin-left:4px}
.postlink{color:#0000EE}
.postbody{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap}
.postimg{max-width:220px;max-height:220px;border:1px solid #D9BFB7;background:#fff}
.postmessage{margin:0;color:#800000;font-family:arial;font-size:10pt;flex:1;min-width:200px;white-space:normal}
.quote{color:#789922}
.thread{margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #D9BFB7}
.omitted{color:#707070;text-align:center;margin:6px 0;font-size:9pt}
textarea,input[type=text],input[type=password]{background:#FFFFEE;border:1px solid #D9BFB7;color:#800000;font-family:arial;font-size:10pt;padding:3px}
textarea{width:100%;resize:vertical}
button{background:#F0E0D6;border:1px solid #D9BFB7;padding:4px 14px;cursor:pointer;color:#800000;font-size:10pt;font-family:arial}
button:hover{background:#FEDCBA}
.footer{text-align:center;font-size:9pt;color:#800000;padding:14px 8px}
.sage{color:#707070;font-size:9pt}
.locked{color:#DD0000;font-weight:700}
.sticky{color:#DD0000;font-weight:700}
.subject{color:#AF0A0F;font-weight:700;margin-right:6px}
.tbl{border-collapse:collapse;width:100%}
.tbl th,.tbl td{border:1px solid #D9BFB7;padding:5px 8px;text-align:left}
.tbl th{background:#F0E0D6;color:#AF0A0F}
.tbl tr:nth-child(even) td{background:#FCF5EE}
.wrap{max-width:900px;margin:0 auto;padding:0 8px}
.small{font-size:9pt;color:#707070}
"""

JS = """
<script>
function quotePost(id){
  var ta=document.getElementById('reply-text');
  if(!ta)return;
  var v=ta.value;
  if(v.length && !v.endsWith('\\n'))v+='\\n';
  ta.value=v+'>>'+id+'\\n';
  ta.focus();
  try{ta.scrollIntoView({behavior:'smooth',block:'center'});}catch(e){}
}
function deletePost(id){
  var pw=prompt('Password for deletion:');
  if(pw===null)return;
  var f=document.createElement('form');
  f.method='POST';f.action='/post/'+id+'/delete';
  var i=document.createElement('input');
  i.type='hidden';i.name='password';i.value=pw;
  f.appendChild(i);document.body.appendChild(f);f.submit();
}
</script>
"""

def page(title: str, body: str) -> str:
    nav = '<a href="/">[Home]</a>'
    for s in boards:
        nav += f'<a href="/{s}/">[{s}]</a>'
    nav += '<a href="/api/boards">[API]</a>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<a name="top"></a>
<div class="header">
  <h1>Anonymous Imageboard</h1>
  <div class="sub">All posts are anonymous. No names. No accounts.</div>
</div>
<div class="navbar">{nav}</div>
{body}
<div class="footer">
  All posts are anonymous. Passwords are optional &mdash; set one to enable deletion.<br>
  Powered by FastAPI &middot; {now_str()}
</div>
{JS}
</body>
</html>"""

def render_post(p: dict, is_op: bool = False, show_actions: bool = True) -> str:
    if not p or p.get("deleted"):
        return f'<div class="post reply deleted">Post No.{p["id"] if p else "?"} has been deleted.</div>' if p else ""
    cls = "post" + (" op" if is_op else " reply")
    flags = ""
    if is_op:
        t = threads.get(p["id"], {})
        if t.get("sticky"): flags += ' <span class="sticky">[Sticky]</span>'
        if t.get("locked"): flags += ' <span class="locked">[Locked]</span>'
    sage = ' <span class="sage">(sage)</span>' if (p.get("sage") and not is_op) else ""
    subj = f'<span class="subject">{esc(p["subject"])}</span> ' if (is_op and p.get("subject")) else ""
    img = f'<img src="{esc(p["image_url"])}" class="postimg" alt="" loading="lazy">' if p.get("image_url") else ""
    actions = ""
    if show_actions:
        actions = ('<div style="margin-top:6px;font-size:9pt;color:#707070">'
                   f'<a href="javascript:void(0)" onclick="quotePost({p["id"]})">[Reply]</a> '
                   f'<a href="/{p["board"]}/thread/{p["thread_id"]}#p{p["id"]}">[View]</a> '
                   f'<a href="javascript:void(0)" onclick="deletePost({p["id"]})">[Delete]</a>'
                   '</div>')
    return f'''
<div class="{cls}" id="p{p["id"]}">
  <div class="posthead">
    {subj}<span class="postername">Anonymous</span>
    <span class="postdate">{p["time"]}</span>
    <span class="postnum">No.</span><a class="postlink" href="#p{p["id"]}">{p["id"]}</a>{sage}{flags}
  </div>
  <div class="postbody">
    {img}
    <blockquote class="postmessage">{format_content(p["content"])}</blockquote>
  </div>
  {actions}
</div>'''

def render_thread_in_index(tid: int, max_replies: int = 3) -> str:
    t = threads.get(tid)
    if not t: return ""
    op = posts.get(tid)
    if not op or op["deleted"]: return ""
    parts = ['<div class="thread">']
    parts.append(render_post(op, is_op=True, show_actions=False))
    replies = [pid for pid in t["posts"][1:] if pid in posts and not posts[pid]["deleted"]]
    shown = replies[:max_replies]
    omitted = len(replies) - len(shown)
    for pid in shown:
        parts.append(render_post(posts[pid], show_actions=False))
    if omitted > 0:
        parts.append(
            f'<div class="omitted">{omitted} repl{"y" if omitted==1 else "ies"} omitted. '
            f'<a href="/{t["board"]}/thread/{tid}">Click here to view.</a></div>'
        )
    parts.append(
        f'<div style="margin-top:6px;font-size:9pt">'
        f'<a href="/{t["board"]}/thread/{tid}">[Reply]</a> &middot; '
        f'<span class="small">{t["reply_count"]} replies, {t["image_count"]} images</span>'
        f'</div>'
    )
    parts.append('</div>')
    return "".join(parts)

# ==================== EXCEPTION HANDLER ====================
@app.exception_handler(FastAPIHTTPException)
async def http_exc_handler(request: Request, exc: FastAPIHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail, "status": exc.status_code},
                            status_code=exc.status_code)
    body = (f'<div class="boardtitle">Error {exc.status_code}</div>'
            f'<div style="text-align:center;padding:20px">{esc(str(exc.detail))}</div>'
            f'<div style="text-align:center"><a href="/">[Return Home]</a></div>')
    return HTMLResponse(page(f"Error {exc.status_code}", body), status_code=exc.status_code)

# ==================== ROUTES: API (must be first) ====================
@app.get("/api/boards")
def api_boards():
    return [
        {"slug": s, "name": b["name"], "desc": b["desc"], "thread_count": len(b["threads"])}
        for s, b in boards.items()
    ]

@app.get("/api/{board}/threads")
def api_threads(board: str):
    if board not in boards:
        raise HTTPException(404, "Board not found")
    out = []
    for tid in boards[board]["threads"]:
        t = threads.get(tid)
        if not t: continue
        op = posts.get(tid)
        out.append({
            "id": tid,
            "subject": op["subject"] if op else "",
            "reply_count": t["reply_count"],
            "image_count": t["image_count"],
            "bumped": t["bumped"],
            "created": t["created"],
            "sticky": t["sticky"],
            "locked": t["locked"],
        })
    out.sort(key=lambda x: (not x["sticky"], -x["bumped"]))
    return out

@app.get("/api/thread/{tid}")
def api_thread(tid: int):
    t = threads.get(tid)
    if not t:
        raise HTTPException(404, "Thread not found")
    items = []
    for pid in t["posts"]:
        p = posts.get(pid)
        if not p: continue
        items.append({
            "id": p["id"],
            "content": p["content"],
            "time": p["time"],
            "time_ts": p["time_ts"],
            "image_url": p["image_url"],
            "is_op": p["is_op"],
            "sage": p["sage"],
            "deleted": p["deleted"],
        })
    return {
        "id": tid, "board": t["board"], "sticky": t["sticky"],
        "locked": t["locked"], "reply_count": t["reply_count"],
        "posts": items,
    }

@app.get("/api/stats")
def api_stats():
    return {
        "boards": len(boards),
        "threads": len(threads),
        "posts": len(posts),
        "total_created": _counter["n"],
    }

# ==================== ROUTES: ADMIN ====================
@app.post("/admin/{board}/{tid}/sticky")
async def admin_sticky(board: str, tid: int, key: str = Form("")):
    if key != ADMIN_KEY:
        raise HTTPException(403, "Bad admin key")
    t = threads.get(tid)
    if not t:
        raise HTTPException(404, "Thread not found")
    t["sticky"] = not t["sticky"]
    return RedirectResponse(f"/{board}/thread/{tid}", status_code=303)

@app.post("/admin/{board}/{tid}/lock")
async def admin_lock(board: str, tid: int, key: str = Form("")):
    if key != ADMIN_KEY:
        raise HTTPException(403, "Bad admin key")
    t = threads.get(tid)
    if not t:
        raise HTTPException(404, "Thread not found")
    t["locked"] = not t["locked"]
    return RedirectResponse(f"/{board}/thread/{tid}", status_code=303)

# ==================== ROUTES: HTML ====================
@app.get("/", response_class=HTMLResponse)
def home():
    body = '<div class="boardtitle">Anonymous Imageboard</div>'
    body += '<div class="wrap">'
    body += '<div class="boarddesc">Choose a board. All posts are anonymous.</div>'
    body += '<table class="tbl"><tr><th>Board</th><th>Description</th><th style="width:90px;text-align:center">Threads</th></tr>'
    for s, b in boards.items():
        body += (f'<tr>'
                 f'<td><a href="/{s}/">/{s}/ &mdash; {esc(b["name"])}</a></td>'
                 f'<td>{esc(b["desc"])}</td>'
                 f'<td style="text-align:center">{len(b["threads"])}</td>'
                 f'</tr>')
    body += '</table>'
    body += f'<div class="small" style="margin-top:12px">Total boards: {len(boards)} &middot; '
    body += f'Threads: {len(threads)} &middot; Posts: {len(posts)}</div>'
    body += '</div>'
    return page("Anonymous Imageboard", body)

@app.get("/{board_slug}/", response_class=HTMLResponse)
def board_index(board_slug: str):
    if board_slug not in boards:
        raise HTTPException(404, "Board not found")
    b = boards[board_slug]
    sorted_threads = sorted(
        [threads[tid] for tid in b["threads"] if tid in threads],
        key=lambda t: (not t.get("sticky", False), -t["bumped"])
    )
    form = f'''
<div class="form-box">
  <form method="post" action="/{board_slug}/new">
    <div style="margin-bottom:8px;color:#AF0A0F;font-weight:700">Create New Thread</div>
    <div style="margin-bottom:6px">Subject: <input type="text" name="subject" size="45" maxlength="120"></div>
    <div style="margin-bottom:6px">Comment:<br>
      <textarea id="reply-text" name="content" rows="7" maxlength="{MAX_CONTENT_LEN}"></textarea>
    </div>
    <div style="margin-bottom:6px">Image URL (optional): <input type="text" name="image_url" size="55" placeholder="https://..."></div>
    <div style="margin-bottom:6px">Password (for deletion, optional): <input type="password" name="password" size="22" maxlength="100"></div>
    <div><button type="submit">Post Thread</button></div>
  </form>
</div>'''
    body = f'<div class="boardtitle">/{board_slug}/ &mdash; {esc(b["name"])}</div>'
    body += f'<div class="boarddesc">{esc(b["desc"])}</div>'
    body += form
    body += '<div class="wrap">'
    if not sorted_threads:
        body += '<div style="text-align:center;padding:24px;color:#707070">No threads yet. Be the first to post!</div>'
    else:
        for t in sorted_threads:
            body += render_thread_in_index(t["id"])
    body += '</div>'
    return page(f'/{board_slug}/ - {b["name"]}', body)

@app.get("/{board_slug}/thread/{tid}", response_class=HTMLResponse)
def thread_view(board_slug: str, tid: int):
    if board_slug not in boards:
        raise HTTPException(404, "Board not found")
    t = threads.get(tid)
    if not t or t["board"] != board_slug:
        raise HTTPException(404, "Thread not found")
    op = posts.get(tid)
    if not op:
        raise HTTPException(404, "Thread not found")

    body = f'<div class="boardtitle">/{board_slug}/ &mdash; Thread No.{tid}</div>'
    body += f'<div class="wrap"><a href="/{board_slug}/">[Return to /{board_slug}/]</a></div>'
    body += '<div class="wrap" style="margin-top:8px">'
    body += render_post(op, is_op=True, show_actions=False)
    for pid in t["posts"][1:]:
        p = posts.get(pid)
        if p and not p["deleted"]:
            body += render_post(p, show_actions=False)
        elif p and p["deleted"]:
            body += f'<div class="post reply deleted">Post No.{pid} has been deleted.</div>'
    body += '</div>'

    if t.get("locked"):
        body += '<div class="form-box" style="text-align:center;color:#DD0000;font-weight:700">Thread is locked.</div>'
    else:
        body += f'''
<div class="form-box">
  <form method="post" action="/{board_slug}/thread/{tid}/reply">
    <div style="margin-bottom:6px;color:#AF0A0F;font-weight:700">Reply to Thread No.{tid}</div>
    <div style="margin-bottom:6px">Comment:<br>
      <textarea id="reply-text" name="content" rows="7" maxlength="{MAX_CONTENT_LEN}"></textarea>
    </div>
    <div style="margin-bottom:6px">Image URL (optional): <input type="text" name="image_url" size="55"></div>
    <div style="margin-bottom:6px">Password (optional): <input type="password" name="password" size="22" maxlength="100"></div>
    <div style="margin-bottom:6px">
      <label><input type="checkbox" name="sage" value="1"> Sage (do not bump thread)</label>
    </div>
    <div><button type="submit">Post Reply</button></div>
  </form>
</div>'''
    body += f'<div class="wrap" style="margin-bottom:20px"><a href="/{board_slug}/">[Return]</a> &middot; <a href="#top">[Top]</a></div>'
    return page(f'/{board_slug}/ - Thread {tid}', body)

# ==================== ROUTES: POST ACTIONS ====================
@app.post("/{board_slug}/new")
async def create_thread(
    board_slug: str, request: Request,
    subject: str = Form(""),
    content: str = Form(""),
    image_url: str = Form(""),
    password: str = Form(""),
):
    if board_slug not in boards:
        raise HTTPException(404, "Board not found")
    ip = get_ip(request)
    if not check_rate(ip):
        raise HTTPException(429, "Slow down. Wait a few seconds.")
    content = (content or "").strip()[:MAX_CONTENT_LEN]
    subject = (subject or "").strip()[:120]
    image_url = (image_url or "").strip()[:500]
    if not content and not image_url:
        raise HTTPException(400, "Comment or image required")
    pid = new_id()
    posts[pid] = {
        "id": pid, "thread_id": pid, "board": board_slug,
        "subject": subject, "content": content, "image_url": image_url,
        "time": now_str(), "time_ts": time.time(),
        "pw_hash": hash_pw(password) if password else None,
        "ip_hash": hash_ip(ip), "is_op": True, "sage": False, "deleted": False,
    }
    threads[pid] = {
        "id": pid, "board": board_slug, "posts": [pid],
        "created": time.time(), "bumped": time.time(),
        "locked": False, "sticky": False,
        "reply_count": 0, "image_count": 1 if image_url else 0,
    }
    boards[board_slug]["threads"].append(pid)
    purge_old_threads(board_slug)
    return RedirectResponse(f"/{board_slug}/thread/{pid}", status_code=303)

@app.post("/{board_slug}/thread/{tid}/reply")
async def post_reply(
    board_slug: str, tid: int, request: Request,
    content: str = Form(""),
    image_url: str = Form(""),
    password: str = Form(""),
    sage: str = Form(""),
):
    if board_slug not in boards:
        raise HTTPException(404, "Board not found")
    t = threads.get(tid)
    if not t or t["board"] != board_slug:
        raise HTTPException(404, "Thread not found")
    if t.get("locked"):
        raise HTTPException(403, "Thread is locked")
    if len(t["posts"]) >= MAX_REPLIES_PER_THREAD:
        raise HTTPException(403, "Thread is full")
    ip = get_ip(request)
    if not check_rate(ip):
        raise HTTPException(429, "Slow down. Wait a few seconds.")
    content = (content or "").strip()[:MAX_CONTENT_LEN]
    image_url = (image_url or "").strip()[:500]
    if not content and not image_url:
        raise HTTPException(400, "Comment or image required")
    is_sage = bool(sage)
    pid = new_id()
    posts[pid] = {
        "id": pid, "thread_id": tid, "board": board_slug,
        "subject": "", "content": content, "image_url": image_url,
        "time": now_str(), "time_ts": time.time(),
        "pw_hash": hash_pw(password) if password else None,
        "ip_hash": hash_ip(ip), "is_op": False, "sage": is_sage, "deleted": False,
    }
    t["posts"].append(pid)
    t["reply_count"] += 1
    if image_url:
        t["image_count"] += 1
    if not is_sage:
        t["bumped"] = time.time()
    return RedirectResponse(f"/{board_slug}/thread/{tid}#p{pid}", status_code=303)

@app.post("/post/{pid}/delete")
async def delete_post(pid: int, password: str = Form("")):
    p = posts.get(pid)
    if not p or p["deleted"]:
        raise HTTPException(404, "Post not found")
    if not p["pw_hash"] or hash_pw(password) != p["pw_hash"]:
        raise HTTPException(403, "Incorrect password")
    board = p["board"]
    tid = p["thread_id"]
    p["deleted"] = True
    p["content"] = "[deleted]"
    p["image_url"] = ""
    if p["is_op"]:
        t = threads.pop(tid, None)
        if t:
            for rid in t["posts"]:
                rp = posts.get(rid)
                if rp:
                    rp["deleted"] = True
                    rp["content"] = "[deleted]"
                    rp["image_url"] = ""
            if tid in boards.get(board, {}).get("threads", []):
                boards[board]["threads"].remove(tid)
        return RedirectResponse(f"/{board}/", status_code=303)
    return RedirectResponse(f"/{board}/thread/{tid}", status_code=303)

# ==================== UTILITY PAGES ====================
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"

# ==================== BOOTSTRAP ====================
def init_boards():
    create_board("b", "Random", "The birthplace of Anonymous. Anything goes.")
    create_board("g", "Technology", "Technology discussion.")
    create_board("v", "Video Games", "Video games discussion.")
    create_board("a", "Anime & Manga", "Anime and manga.")
    create_board("pol", "Politically Incorrect", "Politics.")
    create_board("fit", "Fitness", "Health, fitness, nutrition.")
    create_board("mu", "Music", "Music discussion.")
    create_board("ck", "Food & Cooking", "Recipes, cooking, food.")
    create_board("sci", "Science & Math", "Science and mathematics.")
    create_board("int", "International", "International / regional discussion.")
    create_board("diy", "Do It Yourself", "DIY projects and crafts.")
    create_board("out", "Outdoors", "Hiking, camping, nature.")

init_boards()

if __name__ == "__main__":
    print("=" * 60)
    print(" Anonymous Imageboard running")
    print(f" http://localhost:{PORT}/")
    print(f" Admin key: {ADMIN_KEY}")
    print("=" * 60)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
