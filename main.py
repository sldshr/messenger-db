"""
Анонимный форум (в стиле 4chan)
Python + FastAPI + Uvicorn
Всё хранится в оперативной памяти.
"""

import time
import re
import hashlib
import html as _html
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.exceptions import HTTPException as FastAPIHTTPException
import uvicorn

# ==================== НАСТРОЙКИ ====================
SALT = "izmeni_etot_sol_12345"
ADMIN_KEY = "admin_secret_change_me"
MAX_THREADS_PER_BOARD = 60
MAX_REPLIES_PER_THREAD = 300
RATE_LIMIT_SEC = 5
MAX_CONTENT_LEN = 4000
HOST = "0.0.0.0"
PORT = 8000

# ==================== SVG ИКОНКИ ====================
_ICON_COMMON = (
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" '
    'style="vertical-align:-2px;margin-right:4px;flex-shrink:0"'
)

ICON_HOME = (
    '<svg viewBox="0 0 24 24" width="14" height="14" ' + _ICON_COMMON + '>'
    '<path d="M3 9.5L12 2l9 7.5"/>'
    '<path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10"/>'
    '</svg>'
)
ICON_HELP = (
    '<svg viewBox="0 0 24 24" width="14" height="14" ' + _ICON_COMMON + '>'
    '<circle cx="12" cy="12" r="10"/>'
    '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>'
    '<line x1="12" y1="17" x2="12.01" y2="17"/>'
    '</svg>'
)
ICON_API = (
    '<svg viewBox="0 0 24 24" width="14" height="14" ' + _ICON_COMMON + '>'
    '<polyline points="16 18 22 12 16 6"/>'
    '<polyline points="8 6 2 12 8 18"/>'
    '</svg>'
)
ICON_PENCIL = (
    '<svg viewBox="0 0 24 24" width="16" height="16" ' + _ICON_COMMON + '>'
    '<path d="M12 20h9"/>'
    '<path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>'
    '</svg>'
)
ICON_REPLY = (
    '<svg viewBox="0 0 24 24" width="14" height="14" ' + _ICON_COMMON + '>'
    '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'
    '</svg>'
)
ICON_TRASH = (
    '<svg viewBox="0 0 24 24" width="14" height="14" ' + _ICON_COMMON + '>'
    '<polyline points="3 6 5 6 21 6"/>'
    '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>'
    '<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
    '</svg>'
)
ICON_BOOK = (
    '<svg viewBox="0 0 24 24" width="14" height="14" ' + _ICON_COMMON + '>'
    '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>'
    '<path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>'
    '</svg>'
)
ICON_SEND = (
    '<svg viewBox="0 0 24 24" width="16" height="16" ' + _ICON_COMMON + '>'
    '<line x1="22" y1="2" x2="11" y2="13"/>'
    '<polygon points="22 2 15 22 11 13 2 9 22 2"/>'
    '</svg>'
)
ICON_LOCK = (
    '<svg viewBox="0 0 24 24" width="18" height="18" ' + _ICON_COMMON + '>'
    '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>'
    '<path d="M7 11V7a5 5 0 0 1 10 0v4"/>'
    '</svg>'
)
ICON_BACK = (
    '<svg viewBox="0 0 24 24" width="14" height="14" ' + _ICON_COMMON + '>'
    '<line x1="19" y1="12" x2="5" y2="12"/>'
    '<polyline points="12 19 5 12 12 5"/>'
    '</svg>'
)
ICON_TOP = (
    '<svg viewBox="0 0 24 24" width="14" height="14" ' + _ICON_COMMON + '>'
    '<line x1="12" y1="19" x2="12" y2="5"/>'
    '<polyline points="5 12 12 5 19 12"/>'
    '</svg>'
)
ICON_BOARD = (
    '<svg viewBox="0 0 24 24" width="16" height="16" ' + _ICON_COMMON + '>'
    '<rect x="3" y="3" width="7" height="7"/>'
    '<rect x="14" y="3" width="7" height="7"/>'
    '<rect x="14" y="14" width="7" height="7"/>'
    '<rect x="3" y="14" width="7" height="7"/>'
    '</svg>'
)

# ==================== ХРАНИЛИЩЕ ====================
boards: dict = {}
threads: dict = {}
posts: dict = {}
_counter = {"n": 0}
rate_map: dict = {}

# ==================== ВСПОМОГАТЕЛЬНОЕ ====================
def now_str() -> str:
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    t = time.localtime()
    return (f"{t.tm_mday:02d}.{t.tm_mon:02d}.{t.tm_year} "
            f"({days[t.tm_wday]}) {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}")

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
        line = re.sub(
            r'&gt;&gt;(\d+)',
            r'<a href="#p\1" class="quotelink" '
            r'onclick="quotePost(\1);return false;">&gt;&gt;\1</a>',
            line,
        )
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

# ==================== ПРИЛОЖЕНИЕ ====================
app = FastAPI(title="Анонимный форум", docs_url=None, redoc_url=None)

CSS = """
*{box-sizing:border-box}
body{background:#FFFFEE;color:#800000;font-family:arial,helvetica,sans-serif;font-size:11pt;margin:0;padding:0}
a{color:#0000EE;text-decoration:none}
a:hover{color:#DD0000}
a.quotelink{color:#DD0000;text-decoration:underline;cursor:pointer}
.header{text-align:center;padding:12px 8px 6px}
.header h1{color:#AF0A0F;font-family:Tahoma,sans-serif;font-size:30px;margin:0;letter-spacing:1px}
.header .sub{color:#800000;font-size:10pt;margin-top:4px}
.navbar{background:#FEDCBA;padding:6px 8px;text-align:center;border-top:1px solid #D9BFB7;border-bottom:1px solid #D9BFB7;font-size:10pt;display:flex;justify-content:center;gap:14px;flex-wrap:wrap}
.navbar a{font-weight:700;display:inline-flex;align-items:center}
.boardtitle{text-align:center;color:#AF0A0F;font-size:24px;font-family:Tahoma,sans-serif;font-weight:700;padding:14px 10px 4px}
.boarddesc{text-align:center;color:#800000;padding-bottom:10px;font-size:10pt}
.form-box{background:#F0E0D6;border:1px solid #D9BFB7;padding:14px 16px;margin:14px auto;max-width:780px;border-radius:3px}
.form-box h3{margin:0 0 12px;color:#AF0A0F;font-size:14pt;font-family:Tahoma,sans-serif;display:flex;align-items:center}
.form-row{margin-bottom:12px}
.form-row label{display:block;font-weight:700;color:#800000;margin-bottom:4px;font-size:10pt}
.form-row .hint{font-size:9pt;color:#707070;font-weight:400;margin-left:6px}
textarea,input[type=text],input[type=password]{background:#FFFFEE;border:1px solid #D9BFB7;color:#800000;font-family:arial;font-size:11pt;padding:6px 8px;width:100%}
textarea{resize:vertical;min-height:130px}
button{background:#F0E0D6;border:1px solid #D9BFB7;padding:8px 22px;cursor:pointer;color:#800000;font-size:11pt;font-family:arial;font-weight:700;border-radius:3px;display:inline-flex;align-items:center}
button:hover{background:#FEDCBA}
button.big{padding:10px 30px;font-size:12pt}
.post{background:#F0E0D6;border:1px solid #D9BFB7;padding:8px 10px;margin:6px auto;max-width:920px;word-wrap:break-word;border-radius:3px}
.post.reply{margin-left:44px}
.post.deleted{opacity:.6;font-style:italic}
.posthead{font-size:11pt;margin-bottom:6px}
.postername{color:#117743;font-weight:700}
.postdate{color:#800000;margin-left:6px}
.postnum{color:#800000;margin-left:6px}
.postlink{color:#0000EE}
.postbody{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
.postimg{max-width:240px;max-height:240px;border:1px solid #D9BFB7;background:#fff}
.postmessage{margin:0;color:#800000;font-family:arial;font-size:11pt;flex:1;min-width:220px;white-space:normal;line-height:1.45}
.quote{color:#789922}
.thread{margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid #D9BFB7}
.omitted{color:#707070;text-align:center;margin:8px 0;font-size:10pt}
.footer{text-align:center;font-size:9pt;color:#800000;padding:18px 8px;border-top:1px solid #D9BFB7;margin-top:20px}
.sage{color:#707070;font-size:10pt}
.locked{color:#DD0000;font-weight:700}
.sticky{color:#DD0000;font-weight:700}
.subject{color:#AF0A0F;font-weight:700;margin-right:6px}
.wrap{max-width:920px;margin:0 auto;padding:0 10px}
.small{font-size:9pt;color:#707070}
.btn-row{margin-top:8px;display:flex;align-items:center;flex-wrap:wrap;gap:6px}
.board-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin:14px auto;max-width:960px;padding:0 10px}
.board-card{background:#F0E0D6;border:1px solid #D9BFB7;padding:12px 14px;border-radius:3px;transition:background .15s}
.board-card:hover{background:#FEDCBA}
.board-card a{display:flex;align-items:center;font-size:12pt;font-weight:700;color:#0000EE}
.board-card .name{color:#AF0A0F;font-size:11pt;margin-top:2px;font-weight:700}
.board-card .desc{color:#800000;font-size:10pt;margin-top:6px}
.board-card .cnt{color:#707070;font-size:9pt;margin-top:6px}
.group-title{text-align:center;color:#AF0A0F;font-family:Tahoma,sans-serif;font-size:14pt;font-weight:700;margin:22px 10px 6px}
.help{background:#FCF5EE;border:1px solid #D9BFB7;border-left:4px solid #AF0A0F;padding:10px 14px;margin:12px auto;max-width:820px;font-size:10pt;color:#800000;border-radius:3px}
.help b{color:#AF0A0F}
.reply-btn{display:inline-flex;align-items:center;background:#F0E0D6;border:1px solid #D9BFB7;padding:5px 12px;border-radius:3px;font-size:10pt;font-weight:700;color:#0000EE}
.reply-btn:hover{background:#FEDCBA;color:#DD0000}
.lockbox{text-align:center;color:#DD0000;font-weight:700;font-size:12pt;display:flex;align-items:center;justify-content:center;gap:8px}
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
  var pw=prompt('Введите пароль, который вы указали при создании поста, для его удаления:');
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
    nav = (
        f'<a href="/">{ICON_HOME}Главная</a>'
        f'<a href="/help">{ICON_HELP}Помощь</a>'
        f'<a href="/api/boards">{ICON_API}API</a>'
    )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<a name="top"></a>
<div class="header">
  <h1>Анонимный форум</h1>
  <div class="sub">Все сообщения анонимны. Никаких имён, регистраций и аккаунтов.</div>
</div>
<div class="navbar">{nav}</div>
{body}
<div class="footer">
  Все сообщения анонимны. Пароль необязателен &mdash; укажите его, чтобы потом удалить свой пост.<br>
  Работает на FastAPI &middot; {now_str()}
</div>
{JS}
</body>
</html>"""

def render_post(p: dict, is_op: bool = False, show_actions: bool = True) -> str:
    if not p or p.get("deleted"):
        if p:
            return f'<div class="post reply deleted">Пост №{p["id"]} удалён.</div>'
        return ""
    cls = "post" + (" op" if is_op else " reply")
    flags = ""
    if is_op:
        t = threads.get(p["id"], {})
        if t.get("sticky"):
            flags += ' <span class="sticky">[Закреплён]</span>'
        if t.get("locked"):
            flags += ' <span class="locked">[Закрыт]</span>'
    sage = ' <span class="sage">(без поднятия)</span>' if (p.get("sage") and not is_op) else ""
    subj = f'<span class="subject">{esc(p["subject"])}</span> ' if (is_op and p.get("subject")) else ""
    img = ""
    if p.get("image_url"):
        img = (f'<img src="{esc(p["image_url"])}" class="postimg" alt="" '
               f'loading="lazy" onerror="this.style.display=\'none\'">')
    actions = ""
    if show_actions:
        actions = (
            '<div class="btn-row">'
            f'<a class="reply-btn" href="javascript:void(0)" '
            f'onclick="quotePost({p["id"]})">{ICON_REPLY}Ответить</a>'
            f'<a class="reply-btn" href="javascript:void(0)" '
            f'onclick="deletePost({p["id"]})">{ICON_TRASH}Удалить</a>'
            '</div>'
        )
    return f'''
<div class="{cls}" id="p{p["id"]}">
  <div class="posthead">
    {subj}<span class="postername">Аноним</span>
    <span class="postdate">{p["time"]}</span>
    <span class="postnum">№</span><a class="postlink" href="#p{p["id"]}">{p["id"]}</a>{sage}{flags}
  </div>
  <div class="postbody">
    {img}
    <blockquote class="postmessage">{format_content(p["content"])}</blockquote>
  </div>
  {actions}
</div>'''

def render_thread_in_index(tid: int, max_replies: int = 3) -> str:
    t = threads.get(tid)
    if not t:
        return ""
    op = posts.get(tid)
    if not op or op["deleted"]:
        return ""
    parts = ['<div class="thread">']
    parts.append(render_post(op, is_op=True, show_actions=False))
    replies = [pid for pid in t["posts"][1:] if pid in posts and not posts[pid]["deleted"]]
    shown = replies[:max_replies]
    omitted = len(replies) - len(shown)
    for pid in shown:
        parts.append(render_post(posts[pid], show_actions=False))
    if omitted > 0:
        if omitted == 1:
            word = "ответ"
        elif 2 <= omitted <= 4:
            word = "ответа"
        else:
            word = "ответов"
        parts.append(
            f'<div class="omitted">Пропущено {omitted} {word}. '
            f'<a href="/{t["board"]}/thread/{tid}">Нажмите, чтобы открыть весь тред.</a></div>'
        )
    parts.append(
        f'<div class="btn-row">'
        f'<a class="reply-btn" href="/{t["board"]}/thread/{tid}">'
        f'{ICON_BOOK}Открыть тред и ответить</a>'
        f'<span class="small">Ответов: {t["reply_count"]} &middot; Картинок: {t["image_count"]}</span>'
        f'</div>'
    )
    parts.append('</div>')
    return "".join(parts)

# ==================== ОБРАБОТЧИК ОШИБОК ====================
@app.exception_handler(FastAPIHTTPException)
async def http_exc_handler(request: Request, exc: FastAPIHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": exc.detail, "status": exc.status_code},
            status_code=exc.status_code,
        )
    body = (
        f'<div class="boardtitle">Ошибка {exc.status_code}</div>'
        f'<div style="text-align:center;padding:24px;font-size:12pt">{esc(str(exc.detail))}</div>'
        f'<div style="text-align:center;padding-bottom:20px">'
        f'<a class="reply-btn" href="/">{ICON_HOME}На главную</a></div>'
    )
    return HTMLResponse(page(f"Ошибка {exc.status_code}", body), status_code=exc.status_code)

# ==================== API ====================
@app.get("/api/boards")
def api_boards():
    return [
        {"slug": s, "name": b["name"], "desc": b["desc"], "thread_count": len(b["threads"])}
        for s, b in boards.items()
    ]

@app.get("/api/{board}/threads")
def api_threads(board: str):
    if board not in boards:
        raise HTTPException(404, "Раздел не найден")
    out = []
    for tid in boards[board]["threads"]:
        t = threads.get(tid)
        if not t:
            continue
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
        raise HTTPException(404, "Тред не найден")
    items = []
    for pid in t["posts"]:
        p = posts.get(pid)
        if not p:
            continue
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

# ==================== АДМИН ====================
@app.post("/admin/{board}/{tid}/sticky")
async def admin_sticky(board: str, tid: int, key: str = Form("")):
    if key != ADMIN_KEY:
        raise HTTPException(403, "Неверный ключ администратора")
    t = threads.get(tid)
    if not t:
        raise HTTPException(404, "Тред не найден")
    t["sticky"] = not t["sticky"]
    return RedirectResponse(f"/{board}/thread/{tid}", status_code=303)

@app.post("/admin/{board}/{tid}/lock")
async def admin_lock(board: str, tid: int, key: str = Form("")):
    if key != ADMIN_KEY:
        raise HTTPException(403, "Неверный ключ администратора")
    t = threads.get(tid)
    if not t:
        raise HTTPException(404, "Тред не найден")
    t["locked"] = not t["locked"]
    return RedirectResponse(f"/{board}/thread/{tid}", status_code=303)

# ==================== ГРУППЫ РАЗДЕЛОВ ====================
BOARD_GROUPS = [
    ("Общие", ["b", "news", "int", "rnd"]),
    ("Технологии", ["g", "diy", "prog", "hard", "soft", "web", "sec"]),
    ("Игры", ["v", "retro", "vgm", "mmo"]),
    ("Кино, музыка, книги", ["mu", "tv", "cin", "lit", "an", "a"]),
    ("Творчество", ["art", "p", "fa", "po", "ph"]),
    ("Наука и учёба", ["sci", "his", "math", "lang"]),
    ("Жизнь", ["fit", "ck", "out", "sp", "biz", "adv", "trv", "auto"]),
    ("Разное", ["pol", "r", "x", "weird", "dev"]),
]

# ==================== СТРАНИЦЫ ====================
@app.get("/", response_class=HTMLResponse)
def home():
    body = '<div class="boardtitle">Добро пожаловать на Анонимный форум</div>'
    body += '<div class="boarddesc">Выберите раздел &mdash; всё как на старом добром 4chan, но на русском.</div>'
    body += (
        '<div class="help">'
        '<b>Как пользоваться:</b> выберите раздел &rarr; создайте новую тему '
        'или откройте существующую &rarr; напишите сообщение. '
        'Имя указывать не нужно, все сообщения анонимны. '
        'Если хотите удалить свой пост позже &mdash; придумайте пароль и запомните его.'
        '</div>'
    )

    used = set()
    for group_name, slugs in BOARD_GROUPS:
        present = [s for s in slugs if s in boards]
        if not present:
            continue
        body += f'<div class="group-title">&mdash; {esc(group_name)} &mdash;</div>'
        body += '<div class="board-grid">'
        for s in present:
            b = boards[s]
            used.add(s)
            body += (
                f'<div class="board-card">'
                f'<a href="/{s}/">{ICON_BOARD}/{s}/</a>'
                f'<div class="name">{esc(b["name"])}</div>'
                f'<div class="desc">{esc(b["desc"])}</div>'
                f'<div class="cnt">Тем: {len(b["threads"])}</div>'
                f'</div>'
            )
        body += '</div>'

    rest = [s for s in boards if s not in used]
    if rest:
        body += '<div class="group-title">&mdash; Прочие разделы &mdash;</div>'
        body += '<div class="board-grid">'
        for s in rest:
            b = boards[s]
            body += (
                f'<div class="board-card">'
                f'<a href="/{s}/">{ICON_BOARD}/{s}/</a>'
                f'<div class="name">{esc(b["name"])}</div>'
                f'<div class="desc">{esc(b["desc"])}</div>'
                f'<div class="cnt">Тем: {len(b["threads"])}</div>'
                f'</div>'
            )
        body += '</div>'

    body += '<div class="wrap">'
    body += (
        f'<div class="small" style="margin-top:20px;text-align:center">'
        f'Всего разделов: {len(boards)} &middot; Тем: {len(threads)} &middot; '
        f'Сообщений: {len(posts)}</div>'
    )
    body += '</div>'
    return page("Анонимный форум — Главная", body)

@app.get("/help", response_class=HTMLResponse)
def help_page():
    body = """
<div class="boardtitle">Помощь</div>
<div class="wrap" style="max-width:820px">

<div class="help">
<b>Что это такое?</b><br>
Это анонимный форум в стиле классического 4chan. Никаких регистраций, имён, лайков
и профилей. Только анонимные сообщения.
</div>

<div class="help">
<b>Как создать новую тему?</b><br>
1. На главной странице выберите любой раздел (например /b/).<br>
2. На странице раздела сверху будет форма <b>«Создать новую тему»</b>.<br>
3. Впишите тему (необязательно), текст сообщения и нажмите кнопку отправки.
</div>

<div class="help">
<b>Как ответить в существующую тему?</b><br>
1. Откройте тему, кликнув по ней.<br>
2. Прокрутите вниз &mdash; там будет форма ответа.<br>
3. Или нажмите кнопку «Ответить» под любым сообщением.
</div>

<div class="help">
<b>Что такое &gt;&gt;123?</b><br>
Это ссылка на другой пост по его номеру. Если вы впишете в тексте
<code>&gt;&gt;123</code>, появится кликабельная ссылка на пост №123.
Если строка начинается с <code>&gt;</code> (одного), она станет зелёной &mdash;
это «цитата».
</div>

<div class="help">
<b>Как прикрепить картинку?</b><br>
Просто вставьте ссылку на изображение в поле «Ссылка на картинку».
Подойдут прямые ссылки из интернета, оканчивающиеся на .jpg, .png, .gif или .webp.
</div>

<div class="help">
<b>Как удалить свой пост?</b><br>
При создании поста укажите <b>пароль</b> (любой, который запомните). Потом под своим
постом нажмите кнопку «Удалить» и введите тот же пароль. Если удалить первый пост
в теме &mdash; вся тема исчезнет.
</div>

<div class="help">
<b>Что такое «без поднятия» (sage)?</b><br>
Если отметить эту галочку при ответе, тема не будет подниматься наверх списка,
но ответ всё равно добавится. Так делают, чтобы не «бампать» тему зря.
</div>

<div class="help">
<b>Почему мои сообщения не появляются сразу?</b><br>
Есть ограничение: одно сообщение раз в 5 секунд с одного IP. Просто подождите пару секунд.
</div>

<div class="help">
<b>Что-то сломалось / пропало?</b><br>
Форум работает в оперативной памяти. При перезапуске сервера все данные стираются.
Это учебный проект.
</div>

<div style="text-align:center;margin-top:20px">
<a class="reply-btn" href="/">На главную</a>
</div>
</div>
"""
    return page("Помощь", body)

@app.get("/{board_slug}/", response_class=HTMLResponse)
def board_index(board_slug: str):
    if board_slug not in boards:
        raise HTTPException(404, "Раздел не найден")
    b = boards[board_slug]
    sorted_threads = sorted(
        [threads[tid] for tid in b["threads"] if tid in threads],
        key=lambda t: (not t.get("sticky", False), -t["bumped"]),
    )

    form = f'''
<div class="form-box">
  <h3>{ICON_PENCIL}Создать новую тему</h3>
  <form method="post" action="/{board_slug}/new">
    <div class="form-row">
      <label>Тема <span class="hint">(необязательно &mdash; короткое название темы)</span></label>
      <input type="text" name="subject" maxlength="120" placeholder="Например: Обсуждаем новые игры">
    </div>
    <div class="form-row">
      <label>Сообщение <span class="hint">(обязательно)</span></label>
      <textarea id="reply-text" name="content" rows="7" maxlength="{MAX_CONTENT_LEN}"
        placeholder="Напишите здесь текст. Строка, начинающаяся с символа &gt;, станет зелёной цитатой."></textarea>
    </div>
    <div class="form-row">
      <label>Ссылка на картинку <span class="hint">(необязательно, прямая ссылка на .jpg/.png/.gif)</span></label>
      <input type="text" name="image_url" placeholder="https://example.com/cat.jpg">
    </div>
    <div class="form-row">
      <label>Пароль <span class="hint">(необязательно &mdash; чтобы потом удалить свой пост)</span></label>
      <input type="password" name="password" maxlength="100"
        placeholder="Запомните его, если хотите удалить пост позже">
    </div>
    <div><button type="submit" class="big">{ICON_SEND}Создать тему</button></div>
  </form>
</div>'''

    body = f'<div class="boardtitle">/{board_slug}/ &mdash; {esc(b["name"])}</div>'
    body += f'<div class="boarddesc">{esc(b["desc"])}</div>'
    body += '<div class="wrap">'
    body += form
    if not sorted_threads:
        body += (
            '<div style="text-align:center;padding:30px;color:#707070;font-size:12pt">'
            'Пока тут пусто. Создайте первую тему &mdash; форма выше.</div>'
        )
    else:
        for t in sorted_threads:
            body += render_thread_in_index(t["id"])
    body += '</div>'
    return page(f'/{board_slug}/ — {b["name"]}', body)

@app.get("/{board_slug}/thread/{tid}", response_class=HTMLResponse)
def thread_view(board_slug: str, tid: int):
    if board_slug not in boards:
        raise HTTPException(404, "Раздел не найден")
    t = threads.get(tid)
    if not t or t["board"] != board_slug:
        raise HTTPException(404, "Тема не найдена")
    op = posts.get(tid)
    if not op:
        raise HTTPException(404, "Тема не найдена")

    body = f'<div class="boardtitle">/{board_slug}/ &mdash; Тема №{tid}</div>'
    body += (
        f'<div class="wrap">'
        f'<a class="reply-btn" href="/{board_slug}/">{ICON_BACK}Вернуться в /{board_slug}/</a>'
        f'</div>'
    )
    body += '<div class="wrap" style="margin-top:10px">'
    body += render_post(op, is_op=True, show_actions=False)
    for pid in t["posts"][1:]:
        p = posts.get(pid)
        if p and not p["deleted"]:
            body += render_post(p, show_actions=False)
        elif p and p["deleted"]:
            body += f'<div class="post reply deleted">Пост №{pid} удалён.</div>'
    body += '</div>'

    if t.get("locked"):
        body += (
            f'<div class="form-box lockbox">'
            f'{ICON_LOCK}Эта тема закрыта. Новые ответы запрещены.'
            f'</div>'
        )
    else:
        body += f'''
<div class="form-box">
  <h3>{ICON_REPLY}Ответить в тему №{tid}</h3>
  <form method="post" action="/{board_slug}/thread/{tid}/reply">
    <div class="form-row">
      <label>Сообщение <span class="hint">(обязательно)</span></label>
      <textarea id="reply-text" name="content" rows="7" maxlength="{MAX_CONTENT_LEN}"
        placeholder="Напишите ответ. Кнопка «Ответить» под сообщением вставит ссылку &gt;&gt;номер."></textarea>
    </div>
    <div class="form-row">
      <label>Ссылка на картинку <span class="hint">(необязательно)</span></label>
      <input type="text" name="image_url" placeholder="https://example.com/pic.png">
    </div>
    <div class="form-row">
      <label>Пароль <span class="hint">(необязательно &mdash; чтобы удалить ответ позже)</span></label>
      <input type="password" name="password" maxlength="100">
    </div>
    <div class="form-row">
      <label style="font-weight:400;display:flex;align-items:center;gap:8px">
        <input type="checkbox" name="sage" value="1" style="width:auto">
        <span><b>Без поднятия темы</b>
        <span class="hint">— ответ не поднимет тему наверх</span></span>
      </label>
    </div>
    <div><button type="submit" class="big">{ICON_SEND}Отправить ответ</button></div>
  </form>
</div>'''
    body += (
        f'<div class="wrap" style="margin-bottom:24px;text-align:center">'
        f'<a class="reply-btn" href="/{board_slug}/">{ICON_BACK}В раздел</a> '
        f'<a class="reply-btn" href="#top">{ICON_TOP}Наверх</a></div>'
    )
    return page(f'/{board_slug}/ — Тема {tid}', body)

# ==================== ДЕЙСТВИЯ ====================
@app.post("/{board_slug}/new")
async def create_thread(
    board_slug: str, request: Request,
    subject: str = Form(""),
    content: str = Form(""),
    image_url: str = Form(""),
    password: str = Form(""),
):
    if board_slug not in boards:
        raise HTTPException(404, "Раздел не найден")
    ip = get_ip(request)
    if not check_rate(ip):
        raise HTTPException(429, "Слишком часто. Подождите несколько секунд.")
    content = (content or "").strip()[:MAX_CONTENT_LEN]
    subject = (subject or "").strip()[:120]
    image_url = (image_url or "").strip()[:500]
    if not content and not image_url:
        raise HTTPException(400, "Нужно написать текст или прикрепить картинку")
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
        raise HTTPException(404, "Раздел не найден")
    t = threads.get(tid)
    if not t or t["board"] != board_slug:
        raise HTTPException(404, "Тема не найдена")
    if t.get("locked"):
        raise HTTPException(403, "Тема закрыта")
    if len(t["posts"]) >= MAX_REPLIES_PER_THREAD:
        raise HTTPException(403, "Тема переполнена")
    ip = get_ip(request)
    if not check_rate(ip):
        raise HTTPException(429, "Слишком часто. Подождите несколько секунд.")
    content = (content or "").strip()[:MAX_CONTENT_LEN]
    image_url = (image_url or "").strip()[:500]
    if not content and not image_url:
        raise HTTPException(400, "Нужно написать текст или прикрепить картинку")
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
        raise HTTPException(404, "Пост не найден")
    if not p["pw_hash"] or hash_pw(password) != p["pw_hash"]:
        raise HTTPException(403, "Неверный пароль")
    board = p["board"]
    tid = p["thread_id"]
    p["deleted"] = True
    p["content"] = "[удалено]"
    p["image_url"] = ""
    if p["is_op"]:
        t = threads.pop(tid, None)
        if t:
            for rid in t["posts"]:
                rp = posts.get(rid)
                if rp:
                    rp["deleted"] = True
                    rp["content"] = "[удалено]"
                    rp["image_url"] = ""
            if tid in boards.get(board, {}).get("threads", []):
                boards[board]["threads"].remove(tid)
        return RedirectResponse(f"/{board}/", status_code=303)
    return RedirectResponse(f"/{board}/thread/{tid}", status_code=303)

# ==================== СЛУЖЕБНОЕ ====================
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"

# ==================== СПИСОК РАЗДЕЛОВ ====================
def init_boards():
    # Общие
    create_board("b",    "Разное",              "Всё подряд. О чём угодно.")
    create_board("news", "Новости",             "Обсуждение новостей и событий.")
    create_board("int",  "Международный",       "Разговоры о странах и мире.")
    create_board("rnd",  "Случайности",         "Случайные темы без правил.")

    # Технологии
    create_board("g",    "Технологии",          "Гаджеты, компьютеры, техника.")
    create_board("prog", "Программирование",    "Код, языки, разработка.")
    create_board("hard", "Железо",              "Комплектующие, сборка ПК.")
    create_board("soft", "Софт",                "Программы и приложения.")
    create_board("web",  "Веб-разработка",      "Сайты, HTML/CSS/JS, фреймворки.")
    create_board("sec",  "Кибербезопасность",   "Хакеры, защита, уязвимости.")
    create_board("diy",  "Сделай сам",          "Мастерская, самоделки.")

    # Игры
    create_board("v",    "Видеоигры",           "Обсуждение игр.")
    create_board("retro","Ретро-игры",          "Старые консоли и DOS-игры.")
    create_board("vgm",  "Игровая музыка",      "Саундтреки и чиптюн.")
    create_board("mmo",  "Онлайн-игры",         "MMO, шутеры, кооп.")

    # Кино / музыка / книги
    create_board("mu",   "Музыка",              "Всё о музыке и группах.")
    create_board("tv",   "Кино и сериалы",      "Фильмы, сериалы, аниме.")
    create_board("cin",  "Кинематограф",        "Режиссёры, киноискусство.")
    create_board("lit",  "Литература",          "Книги, стихи, писатели.")
    create_board("an",   "Аниме и манга",       "Обсуждение аниме и манги.")
    create_board("a",    "Аниме-арт",           "Картинки, арт по аниме.")

    # Творчество
    create_board("art",  "Искусство",           "Рисунки, живопись, галереи.")
    create_board("p",    "Фотография",          "Фото и техника съёмки.")
    create_board("fa",   "Рисование",           "Уроки и работы художников.")
    create_board("po",   "Поэзия",              "Стихи и проза.")
    create_board("ph",   "Философия",           "Размышления и дискуссии.")

    # Наука и учёба
    create_board("sci",  "Наука",               "Физика, химия, биология.")
    create_board("his",  "История",             "История стран и событий.")
    create_board("math", "Математика",          "Числа, формулы, задачи.")
    create_board("lang", "Иностранные языки",   "Английский и любые другие.")

    # Жизнь
    create_board("fit",  "Фитнес и здоровье",   "Спорт, питание, ЗОЖ.")
    create_board("ck",   "Еда и кулинария",     "Рецепты и готовка.")
    create_board("out",  "Природа и туризм",    "Походы, рыбалка, кемпинг.")
    create_board("sp",   "Спорт",               "Футбол, хоккей, единоборства.")
    create_board("biz",  "Работа и деньги",     "Бизнес, фриланс, зарплаты.")
    create_board("adv",  "Советы",              "Спроси совета у анонимов.")
    create_board("trv",  "Путешествия",         "Страны, города, маршруты.")
    create_board("auto", "Авто",                "Машины, мотоциклы, ремонт.")

    # Разное
    create_board("pol",  "Политика",            "Политические обсуждения.")
    create_board("r",    "Религия",             "Вера и религии мира.")
    create_board("x",    "Взрослое (18+)",      "Раздел для взрослых тем.")
    create_board("weird","Странное",            "Всё необычное и непонятное.")
    create_board("dev",  "Разработка форума",   "Обсуждение самого форума.")

init_boards()

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    print("=" * 64)
    print("  Анонимный форум запущен")
    print(f"  Открой в браузере:  http://localhost:{PORT}/")
    print(f"  Ключ администратора: {ADMIN_KEY}")
    print(f"  Разделов: {len(boards)}")
    print("=" * 64)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
