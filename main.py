"""
sldchat — анонимный форум в стиле 4chan
Python + FastAPI + Uvicorn
Всё хранится в оперативной памяти.
"""

import time
import re
import hashlib
import html as _html
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.exceptions import HTTPException as FastAPIHTTPException
import uvicorn

# ==================== НАСТРОЙКИ ====================
SALT = "izmeni_etot_sol_12345"
ADMIN_KEY = "admin_secret_change_me"
MAX_THREADS_PER_BOARD = 60
MAX_REPLIES_PER_THREAD = 300
MAX_IMAGES_PER_POST = 5
RATE_LIMIT_SEC = 5
MAX_CONTENT_LEN = 4000
HOST = "0.0.0.0"
PORT = 8000

# ==================== ПЕРЕВОДЫ ====================
T = {
    "ru": {
        "site_title": "sldchat",
        "site_subtitle": "Анонимный форум без имён, регистраций и аккаунтов.",
        "nav_home": "Главная",
        "nav_help": "Помощь",
        "lang_label": "EN",
        "welcome_title": "Добро пожаловать на sldchat",
        "welcome_desc": "Выберите раздел — всё как на классических имиджбордах, но по-русски.",
        "howto": ("<b>Как пользоваться:</b> выберите раздел &rarr; создайте новую тему или откройте "
                  "существующую &rarr; напишите сообщение. Имя указывать не нужно. "
                  "Если хотите удалить свой пост позже &mdash; придумайте пароль и запомните его."),
        "stats": "Всего разделов: {b} &middot; Тем: {t} &middot; Сообщений: {p}",

        "group_general": "Общие",
        "group_tech": "Технологии",
        "group_games": "Игры",
        "group_media": "Кино, музыка, книги",
        "group_creative": "Творчество",
        "group_science": "Наука и учёба",
        "group_life": "Жизнь",
        "group_misc": "Разное",
        "group_other": "Прочие разделы",

        "form_new_thread": "Создать новую тему",
        "form_reply_title": "Ответить в тему №{tid}",
        "lbl_subject": "Тема",
        "hint_subject": "(необязательно &mdash; короткое название)",
        "ph_subject": "Например: Обсуждаем новые игры",
        "lbl_message": "Сообщение",
        "hint_message": "(обязательно)",
        "ph_message": "Напишите текст. Строка, начинающаяся с &gt;, станет зелёной цитатой. Ссылки станут кликабельными автоматически.",
        "lbl_images": "Ссылки на картинки",
        "hint_images": "(необязательно, по одной на строку, до 5 шт.)",
        "ph_images": "https://example.com/cat.jpg\nhttps://example.com/dog.png",
        "lbl_password": "Пароль",
        "hint_password": "(необязательно &mdash; чтобы потом удалить свой пост)",
        "ph_password": "Запомните его, если хотите удалить пост позже",
        "btn_create": "Создать тему",
        "btn_send": "Отправить ответ",
        "btn_reply": "Ответить",
        "btn_delete": "Удалить",
        "btn_open": "Открыть тред и ответить",
        "btn_back": "В раздел",
        "btn_top": "Наверх",
        "btn_home": "На главную",
        "lbl_sage": "Без поднятия темы",
        "hint_sage": "(ответ не поднимет тему наверх)",

        "postnum": "№",
        "post_deleted": "Пост №{pid} удалён.",
        "thread_locked": "Эта тема закрыта. Новые ответы запрещены.",
        "no_threads": "Пока тут пусто. Создайте первую тему — форма выше.",
        "omitted": "Пропущено ответов: {n}.",
        "omitted_link": "Нажмите, чтобы открыть весь тред.",
        "meta_replies": "Ответов: {n}",
        "meta_images": "Картинок: {n}",
        "footer1": "Все сообщения анонимны. Пароль необязателен &mdash; укажите его, чтобы потом удалить свой пост.",
        "footer2": "Работает на FastAPI",

        "err_title": "Ошибка {code}",
        "err_404_board": "Раздел не найден",
        "err_404_thread": "Тема не найдена",
        "err_404_post": "Пост не найден",
        "err_429": "Слишком часто. Подождите несколько секунд.",
        "err_400_empty": "Нужно написать текст или прикрепить картинку",
        "err_403_lock": "Тема закрыта",
        "err_403_full": "Тема переполнена",
        "err_403_pw": "Неверный пароль",
        "err_403_admin": "Неверный ключ администратора",

        "help_title": "Помощь",
        "help_html": """
<div class="help">
<b>Что такое sldchat?</b><br>
Анонимный форум в стиле классических имиджбордов. Никаких регистраций, имён и профилей &mdash;
только анонимные сообщения с номерами.
</div>
<div class="help">
<b>Как создать новую тему?</b><br>
1. На главной выберите раздел (например <code>/b/</code>).<br>
2. На странице раздела сверху будет форма <b>«Создать новую тему»</b>.<br>
3. Впишите тему (необязательно), сообщение и нажмите кнопку.
</div>
<div class="help">
<b>Как ответить в существующую тему?</b><br>
Откройте тему кликом &mdash; внизу будет форма ответа. Или нажмите «Ответить» под нужным постом.
</div>
<div class="help">
<b>Что такое &gt;&gt;123?</b><br>
Это ссылка на пост №123. Если строка начинается с <code>&gt;</code> (одного), она станет
зелёной цитатой.
</div>
<div class="help">
<b>Ссылки</b><br>
Все ссылки, начинающиеся с <code>http://</code> или <code>https://</code>, автоматически
становятся кликабельными.
</div>
<div class="help">
<b>Как прикрепить картинку?</b><br>
Вставьте ссылки на изображения &mdash; по одной на строку, до 5 штук за пост.
</div>
<div class="help">
<b>Как удалить свой пост?</b><br>
При создании поста укажите <b>пароль</b> (любой, который запомните). Потом под своим постом
нажмите «Удалить» и введите тот же пароль. Если удалить первый пост в теме &mdash; вся тема исчезнет.
</div>
<div class="help">
<b>Что такое «без поднятия» (sage)?</b><br>
Если отметить эту галочку при ответе, тема не поднимется наверх списка, но ответ добавится.
</div>
<div class="help">
<b>Почему мои сообщения не появляются сразу?</b><br>
Есть ограничение: одно сообщение раз в 5 секунд с одного IP.
</div>
<div class="help">
<b>Что-то сломалось / пропало?</b><br>
Форум работает в оперативной памяти. При перезапуске сервера все данные стираются.
</div>
"""
    },
    "en": {
        "site_title": "sldchat",
        "site_subtitle": "Anonymous forum with no names, no signups, no accounts.",
        "nav_home": "Home",
        "nav_help": "Help",
        "lang_label": "RU",
        "welcome_title": "Welcome to sldchat",
        "welcome_desc": "Pick a board &mdash; classic imageboard style, no fluff.",
        "howto": ("<b>How to use:</b> pick a board &rarr; create a new thread or open an existing one "
                  "&rarr; write a message. No name is needed. "
                  "If you want to delete your post later &mdash; set a password and remember it."),
        "stats": "Boards: {b} &middot; Threads: {t} &middot; Posts: {p}",

        "group_general": "General",
        "group_tech": "Technology",
        "group_games": "Games",
        "group_media": "Movies, Music, Books",
        "group_creative": "Creative",
        "group_science": "Science & Study",
        "group_life": "Life",
        "group_misc": "Miscellaneous",
        "group_other": "Other boards",

        "form_new_thread": "Create new thread",
        "form_reply_title": "Reply to thread No.{tid}",
        "lbl_subject": "Subject",
        "hint_subject": "(optional &mdash; a short title)",
        "ph_subject": "For example: New games discussion",
        "lbl_message": "Message",
        "hint_message": "(required)",
        "ph_message": "Write your message. A line starting with &gt; becomes a green quote. URLs become clickable automatically.",
        "lbl_images": "Image URLs",
        "hint_images": "(optional, one per line, up to 5)",
        "ph_images": "https://example.com/cat.jpg\nhttps://example.com/dog.png",
        "lbl_password": "Password",
        "hint_password": "(optional &mdash; to delete your post later)",
        "ph_password": "Remember it if you want to delete the post later",
        "btn_create": "Create thread",
        "btn_send": "Post reply",
        "btn_reply": "Reply",
        "btn_delete": "Delete",
        "btn_open": "Open thread and reply",
        "btn_back": "Back to board",
        "btn_top": "Top",
        "btn_home": "Home",
        "lbl_sage": "Sage (no bump)",
        "hint_sage": "(reply won't bump the thread)",

        "postnum": "No.",
        "post_deleted": "Post No.{pid} was deleted.",
        "thread_locked": "This thread is locked. New replies are disabled.",
        "no_threads": "Nothing here yet. Create the first thread using the form above.",
        "omitted": "{n} replies omitted.",
        "omitted_link": "Click to view the full thread.",
        "meta_replies": "Replies: {n}",
        "meta_images": "Images: {n}",
        "footer1": "All posts are anonymous. Password is optional &mdash; set one to delete your post later.",
        "footer2": "Powered by FastAPI",

        "err_title": "Error {code}",
        "err_404_board": "Board not found",
        "err_404_thread": "Thread not found",
        "err_404_post": "Post not found",
        "err_429": "Too fast. Wait a few seconds.",
        "err_400_empty": "Message text or an image is required",
        "err_403_lock": "Thread is locked",
        "err_403_full": "Thread is full",
        "err_403_pw": "Wrong password",
        "err_403_admin": "Invalid admin key",

        "help_title": "Help",
        "help_html": """
<div class="help">
<b>What is sldchat?</b><br>
An anonymous forum in classic imageboard style. No signups, no names, no profiles &mdash;
just anonymous messages with numbers.
</div>
<div class="help">
<b>How to create a new thread?</b><br>
1. On the home page pick a board (e.g. <code>/b/</code>).<br>
2. On the board page there is a <b>&laquo;Create new thread&raquo;</b> form on top.<br>
3. Enter a subject (optional), your message, and submit.
</div>
<div class="help">
<b>How to reply to a thread?</b><br>
Click the thread to open it &mdash; there is a reply form at the bottom. Or press &laquo;Reply&raquo; under any post.
</div>
<div class="help">
<b>What is &gt;&gt;123?</b><br>
It's a link to post No.123. If a line starts with <code>&gt;</code> (single), it becomes a green quote.
</div>
<div class="help">
<b>Links</b><br>
Any link starting with <code>http://</code> or <code>https://</code> becomes clickable automatically.
</div>
<div class="help">
<b>How to attach images?</b><br>
Paste image URLs &mdash; one per line, up to 5 images per post.
</div>
<div class="help">
<b>How to delete my post?</b><br>
Set a <b>password</b> when posting (any string you remember). Later press &laquo;Delete&raquo; under your post
and enter the same password. Deleting the first post deletes the whole thread.
</div>
<div class="help">
<b>What is sage?</b><br>
If you check this box while replying, the thread won't be bumped to the top, but the reply is still added.
</div>
<div class="help">
<b>Why don't my posts appear immediately?</b><br>
There is a rate limit: one post per 5 seconds per IP.
</div>
<div class="help">
<b>Something broke / disappeared?</b><br>
The forum runs in RAM. Restarting the server wipes all data.
</div>
"""
    },
}

def tr(lang: str, key: str, **kw) -> str:
    d = T.get(lang, T["ru"])
    s = d.get(key) or T["ru"].get(key, key)
    return s.format(**kw) if kw else s

# ==================== SVG ИКОНКИ ====================
_IC = ('fill="none" stroke="currentColor" stroke-width="2" '
       'stroke-linecap="round" stroke-linejoin="round" '
       'style="vertical-align:-2px;margin-right:6px;flex-shrink:0"')

def _svg(path: str, size: int = 14) -> str:
    return f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" {_IC}>{path}</svg>'

ICON_HOME   = _svg('<path d="M3 9.5L12 2l9 7.5"/><path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10"/>')
ICON_HELP   = _svg('<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>')
ICON_GLOBE  = _svg('<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>')
ICON_PENCIL = _svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>', 16)
ICON_REPLY  = _svg('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>')
ICON_TRASH  = _svg('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>')
ICON_BOOK   = _svg('<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>')
ICON_SEND   = _svg('<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>', 16)
ICON_LOCK   = _svg('<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>', 18)
ICON_BACK   = _svg('<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>')
ICON_TOP    = _svg('<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>')
ICON_BOARD  = _svg('<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>', 16)
ICON_IMG    = _svg('<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>')

# ==================== ХРАНИЛИЩЕ ====================
boards: dict = {}
threads: dict = {}
posts: dict = {}
_counter = {"n": 0}
rate_map: dict = {}

# ==================== ВСПОМОГАТЕЛЬНОЕ ====================
def fmt_time(ts: float, lang: str) -> str:
    t = time.localtime(ts)
    if lang == "en":
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"{t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d} ({days[t.tm_wday]}) {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}"
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return f"{t.tm_mday:02d}.{t.tm_mon:02d}.{t.tm_year} ({days[t.tm_wday]}) {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}"

def now_str(lang: str) -> str:
    return fmt_time(time.time(), lang)

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

def get_lang(request: Request) -> str:
    c = request.cookies.get("lang", "ru")
    return c if c in ("ru", "en") else "ru"

def check_rate(ip: str) -> bool:
    t = rate_map.get(ip, 0)
    if time.time() - t < RATE_LIMIT_SEC:
        return False
    rate_map[ip] = time.time()
    return True

def new_id() -> int:
    _counter["n"] += 1
    return _counter["n"]

# ==================== ФОРМАТИРОВАНИЕ ТЕКСТА ====================
_URL_RE = re.compile(r'(https?://[^\s<>"\']+)', re.IGNORECASE)
_QUOTE_RE = re.compile(r'&gt;&gt;(\d+)')

def format_content(text: str) -> str:
    if not text:
        return ""
    text = esc(text)
    # кликабельные ссылки
    text = _URL_RE.sub(
        r'<a href="\1" target="_blank" rel="noopener nofollow" class="extlink">\1</a>',
        text,
    )
    # >>123
    text = _QUOTE_RE.sub(
        r'<a href="#p\1" class="quotelink" onclick="quotePost(\1);return false;">&gt;&gt;\1</a>',
        text,
    )
    lines = text.split("\n")
    out = []
    for line in lines:
        if (line.startswith("&gt;")
                and not line.startswith("&gt;&gt;")
                and "<a" not in line[:4]):
            line = f'<span class="quote">{line}</span>'
        out.append(line)
    return "<br>".join(out)

def parse_images(raw: str) -> list:
    if not raw:
        return []
    out = []
    for line in raw.split("\n"):
        u = line.strip()[:500]
        if u and (u.startswith("http://") or u.startswith("https://")):
            out.append(u)
        if len(out) >= MAX_IMAGES_PER_POST:
            break
    return out

# ==================== УПРАВЛЕНИЕ ДОСКАМИ ====================
def create_board(slug, name_ru, name_en, desc_ru, desc_en):
    boards[slug] = {
        "slug": slug,
        "name": {"ru": name_ru, "en": name_en},
        "desc": {"ru": desc_ru, "en": desc_en},
        "threads": [],
    }

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
app = FastAPI(title="sldchat", docs_url=None, redoc_url=None)

CSS = """
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:#FFFFEE;color:#800000;font-family:Arial,Helvetica,sans-serif;font-size:11pt;margin:0;padding:0;line-height:1.45}
a{color:#0000EE;text-decoration:none;transition:color .15s}
a:hover{color:#DD0000}
a.quotelink{color:#DD0000;text-decoration:underline;cursor:pointer}
a.extlink{color:#0000EE;text-decoration:underline;word-break:break-all}
a.extlink:hover{color:#DD0000}
.header{text-align:center;padding:18px 8px 8px}
.header h1{color:#AF0A0F;font-family:Tahoma,Verdana,sans-serif;font-size:34px;margin:0;letter-spacing:1.5px;font-weight:800}
.header h1:hover{text-shadow:0 1px 0 #FEDCBA}
.header .sub{color:#800000;font-size:10pt;margin-top:6px}
.navbar{background:#FEDCBA;padding:8px 12px;border-top:1px solid #D9BFB7;border-bottom:1px solid #D9BFB7;font-size:10pt;display:flex;align-items:center;justify-content:center;gap:20px;flex-wrap:wrap;position:relative}
.navbar a{font-weight:700;display:inline-flex;align-items:center;transition:color .15s}
.navbar .lang-btn{position:absolute;right:10px;top:50%;transform:translateY(-50%);background:#FFFFEE;border:1px solid #D9BFB7;padding:3px 10px;border-radius:3px}
.navbar .lang-btn:hover{background:#FFF;color:#DD0000}
.boardtitle{text-align:center;color:#AF0A0F;font-size:24px;font-family:Tahoma,sans-serif;font-weight:800;padding:16px 10px 4px}
.boarddesc{text-align:center;color:#800000;padding-bottom:12px;font-size:10pt}
.form-box{background:#F0E0D6;border:1px solid #D9BFB7;padding:16px 18px;margin:16px auto;max-width:800px;border-radius:5px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.form-box h3{margin:0 0 14px;color:#AF0A0F;font-size:14pt;font-family:Tahoma,sans-serif;display:flex;align-items:center;font-weight:800}
.form-row{margin-bottom:14px}
.form-row label{display:block;font-weight:700;color:#800000;margin-bottom:5px;font-size:10pt}
.form-row .hint{font-size:9pt;color:#707070;font-weight:400;margin-left:6px}
textarea,input[type=text],input[type=password]{background:#FFFFEE;border:1px solid #D9BFB7;color:#800000;font-family:Arial;font-size:11pt;padding:8px 10px;width:100%;border-radius:3px;transition:border-color .15s,box-shadow .15s}
textarea:focus,input[type=text]:focus,input[type=password]:focus{outline:none;border-color:#AF0A0F;box-shadow:0 0 0 3px rgba(175,10,15,.12)}
textarea{resize:vertical;min-height:140px}
button{background:#F0E0D6;border:1px solid #D9BFB7;padding:9px 24px;cursor:pointer;color:#800000;font-size:11pt;font-family:Arial;font-weight:700;border-radius:4px;display:inline-flex;align-items:center;transition:background .15s,box-shadow .15s,transform .08s}
button:hover{background:#FEDCBA;box-shadow:0 2px 6px rgba(0,0,0,.08)}
button:active{transform:translateY(1px)}
button.big{padding:11px 32px;font-size:12pt}
.post{background:#F0E0D6;border:1px solid #D9BFB7;padding:10px 12px;margin:8px auto;max-width:940px;word-wrap:break-word;border-radius:5px;box-shadow:0 1px 3px rgba(0,0,0,.05);transition:box-shadow .15s}
.post:hover{box-shadow:0 2px 8px rgba(0,0,0,.08)}
.post.reply{margin-left:48px}
.post.deleted{opacity:.6;font-style:italic}
.posthead{font-size:11pt;margin-bottom:8px;color:#707070}
.postdate{color:#800000}
.postnum{color:#800000;margin-left:8px}
.postlink{color:#0000EE}
.postbody{display:flex;flex-direction:column;gap:10px}
.postimages{display:flex;flex-wrap:wrap;gap:10px}
.postimg{max-width:240px;max-height:240px;border:1px solid #D9BFB7;background:#fff;border-radius:4px;transition:transform .15s}
.postimg:hover{transform:scale(1.02)}
.postmessage{margin:0;color:#800000;font-family:Arial;font-size:11pt;white-space:normal;line-height:1.5;word-wrap:break-word}
.quote{color:#789922}
.thread{margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid #D9BFB7}
.omitted{color:#707070;text-align:center;margin:10px 0;font-size:10pt}
.footer{text-align:center;font-size:9pt;color:#800000;padding:20px 8px;border-top:1px solid #D9BFB7;margin-top:24px;line-height:1.6}
.sage{color:#707070;font-size:10pt}
.locked{color:#DD0000;font-weight:700}
.sticky{color:#DD0000;font-weight:700}
.subject{color:#AF0A0F;font-weight:700;margin-right:8px;font-size:11pt}
.wrap{max-width:940px;margin:0 auto;padding:0 12px}
.small{font-size:9pt;color:#707070}
.btn-row{margin-top:10px;display:flex;align-items:center;flex-wrap:wrap;gap:8px}
.board-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin:16px auto;max-width:980px;padding:0 12px}
.board-card{background:#F0E0D6;border:1px solid #D9BFB7;padding:14px 16px;border-radius:5px;transition:background .15s,transform .12s,box-shadow .15s}
.board-card:hover{background:#FEDCBA;transform:translateY(-2px);box-shadow:0 4px 10px rgba(0,0,0,.07)}
.board-card a{display:flex;align-items:center;font-size:12pt;font-weight:700;color:#0000EE}
.board-card .name{color:#AF0A0F;font-size:11pt;margin-top:4px;font-weight:700}
.board-card .desc{color:#800000;font-size:10pt;margin-top:6px}
.board-card .cnt{color:#707070;font-size:9pt;margin-top:8px}
.group-title{text-align:center;color:#AF0A0F;font-family:Tahoma,sans-serif;font-size:14pt;font-weight:800;margin:26px 10px 4px}
.help{background:#FCF5EE;border:1px solid #D9BFB7;border-left:4px solid #AF0A0F;padding:12px 16px;margin:12px auto;max-width:820px;font-size:10pt;color:#800000;border-radius:4px;line-height:1.55}
.help b{color:#AF0A0F}
.help code{background:#FFF;border:1px solid #D9BFB7;padding:1px 5px;border-radius:3px;font-size:9pt}
.reply-btn{display:inline-flex;align-items:center;background:#F0E0D6;border:1px solid #D9BFB7;padding:6px 14px;border-radius:4px;font-size:10pt;font-weight:700;color:#0000EE;transition:background .15s,color .15s,box-shadow .15s}
.reply-btn:hover{background:#FEDCBA;color:#DD0000;box-shadow:0 2px 5px rgba(0,0,0,.06)}
.lockbox{text-align:center;color:#DD0000;font-weight:700;font-size:12pt;display:flex;align-items:center;justify-content:center;gap:8px}
@media (max-width:640px){
  .post.reply{margin-left:14px}
  .header h1{font-size:26px}
  .navbar .lang-btn{position:static;transform:none;margin-left:auto}
  .navbar{justify-content:flex-start}
}
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
function deletePost(id, msg){
  var pw=prompt(msg||'Password:');
  if(pw===null)return;
  var f=document.createElement('form');
  f.method='POST';f.action='/post/'+id+'/delete';
  var i=document.createElement('input');
  i.type='hidden';i.name='password';i.value=pw;
  f.appendChild(i);document.body.appendChild(f);f.submit();
}
</script>
"""

def page(title: str, body: str, lang: str) -> str:
    other = "en" if lang == "ru" else "ru"
    nav = (
        f'<a href="/">{ICON_HOME}{tr(lang,"nav_home")}</a>'
        f'<a href="/help">{ICON_HELP}{tr(lang,"nav_help")}</a>'
        f'<a class="lang-btn" href="/lang/{other}">{ICON_GLOBE}{tr(lang,"lang_label")}</a>'
    )
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<a id="top"></a>
<div class="header">
  <h1><a href="/" style="color:#AF0A0F">{esc(tr(lang,"site_title"))}</a></h1>
  <div class="sub">{tr(lang,"site_subtitle")}</div>
</div>
<div class="navbar">{nav}</div>
{body}
<div class="footer">
  {tr(lang,"footer1")}<br>
  {tr(lang,"footer2")} &middot; {now_str(lang)}
</div>
{JS}
</body>
</html>"""

def render_post(p: dict, lang: str, is_op: bool = False, show_actions: bool = True) -> str:
    if not p or p.get("deleted"):
        if p:
            return f'<div class="post reply deleted">{esc(tr(lang,"post_deleted",pid=p["id"]))}</div>'
        return ""
    cls = "post" + (" op" if is_op else " reply")
    flags = ""
    if is_op:
        t = threads.get(p["id"], {})
        if t.get("sticky"):
            flags += ' <span class="sticky">[Sticky]</span>' if lang == "en" else ' <span class="sticky">[Закреплён]</span>'
        if t.get("locked"):
            flags += ' <span class="locked">[Locked]</span>' if lang == "en" else ' <span class="locked">[Закрыт]</span>'
    sage = ' <span class="sage">(sage)</span>' if (p.get("sage") and not is_op) else ""
    subj = f'<span class="subject">{esc(p["subject"])}</span>' if (is_op and p.get("subject")) else ""

    imgs_html = ""
    if p.get("image_urls"):
        items = "".join(
            f'<img src="{esc(u)}" class="postimg" alt="" loading="lazy" '
            f'onerror="this.style.display=\'none\'">'
            for u in p["image_urls"]
        )
        imgs_html = f'<div class="postimages">{items}</div>'

    actions = ""
    if show_actions:
        del_msg = "Password:" if lang == "en" else "Введите пароль:"
        actions = (
            '<div class="btn-row">'
            f'<a class="reply-btn" href="javascript:void(0)" '
            f'onclick="quotePost({p["id"]})">{ICON_REPLY}{tr(lang,"btn_reply")}</a>'
            f'<a class="reply-btn" href="javascript:void(0)" '
            f'onclick="deletePost({p["id"]},\'{del_msg}\')">{ICON_TRASH}{tr(lang,"btn_delete")}</a>'
            '</div>'
        )

    return f'''
<div class="{cls}" id="p{p["id"]}">
  <div class="posthead">
    {subj}<span class="postdate">{fmt_time(p["time_ts"], lang)}</span>
    <span class="postnum">{tr(lang,"postnum")}</span><a class="postlink" href="#p{p["id"]}">{p["id"]}</a>{sage}{flags}
  </div>
  <div class="postbody">
    {imgs_html}
    <blockquote class="postmessage">{format_content(p["content"])}</blockquote>
  </div>
  {actions}
</div>'''

def render_thread_in_index(tid: int, lang: str, max_replies: int = 3) -> str:
    t = threads.get(tid)
    if not t:
        return ""
    op = posts.get(tid)
    if not op or op["deleted"]:
        return ""
    parts = ['<div class="thread">']
    parts.append(render_post(op, lang, is_op=True, show_actions=False))
    replies = [pid for pid in t["posts"][1:] if pid in posts and not posts[pid]["deleted"]]
    shown = replies[:max_replies]
    omitted = len(replies) - len(shown)
    for pid in shown:
        parts.append(render_post(posts[pid], lang, show_actions=False))
    if omitted > 0:
        parts.append(
            f'<div class="omitted">{esc(tr(lang,"omitted",n=omitted))} '
            f'<a href="/{t["board"]}/thread/{tid}">{esc(tr(lang,"omitted_link"))}</a></div>'
        )
    parts.append(
        f'<div class="btn-row">'
        f'<a class="reply-btn" href="/{t["board"]}/thread/{tid}">'
        f'{ICON_BOOK}{tr(lang,"btn_open")}</a>'
        f'<span class="small">{tr(lang,"meta_replies",n=t["reply_count"])} &middot; '
        f'{tr(lang,"meta_images",n=t["image_count"])}</span>'
        f'</div>'
    )
    parts.append('</div>')
    return "".join(parts)

# ==================== ОБРАБОТЧИК ОШИБОК ====================
@app.exception_handler(FastAPIHTTPException)
async def http_exc_handler(request: Request, exc: FastAPIHTTPException):
    lang = get_lang(request)
    body = (
        f'<div class="boardtitle">{esc(tr(lang,"err_title",code=exc.status_code))}</div>'
        f'<div style="text-align:center;padding:24px;font-size:12pt">{esc(str(exc.detail))}</div>'
        f'<div style="text-align:center;padding-bottom:20px">'
        f'<a class="reply-btn" href="/">{ICON_HOME}{tr(lang,"btn_home")}</a></div>'
    )
    return HTMLResponse(page(tr(lang,"err_title",code=exc.status_code), body, lang),
                        status_code=exc.status_code)

# ==================== СМЕНА ЯЗЫКА ====================
@app.get("/lang/{code}")
def switch_lang(code: str, request: Request):
    if code not in ("ru", "en"):
        code = "ru"
    ref = request.headers.get("referer") or "/"
    resp = RedirectResponse(ref, status_code=303)
    resp.set_cookie("lang", code, max_age=60 * 60 * 24 * 365,
                    httponly=True, samesite="lax")
    return resp

# ==================== АДМИН ====================
@app.post("/admin/{board}/{tid}/sticky")
async def admin_sticky(board: str, tid: int, key: str = Form("")):
    lang = "ru"
    if key != ADMIN_KEY:
        raise HTTPException(403, tr(lang, "err_403_admin"))
    t = threads.get(tid)
    if not t:
        raise HTTPException(404, tr(lang, "err_404_thread"))
    t["sticky"] = not t["sticky"]
    return RedirectResponse(f"/{board}/thread/{tid}", status_code=303)

@app.post("/admin/{board}/{tid}/lock")
async def admin_lock(board: str, tid: int, key: str = Form("")):
    lang = "ru"
    if key != ADMIN_KEY:
        raise HTTPException(403, tr(lang, "err_403_admin"))
    t = threads.get(tid)
    if not t:
        raise HTTPException(404, tr(lang, "err_404_thread"))
    t["locked"] = not t["locked"]
    return RedirectResponse(f"/{board}/thread/{tid}", status_code=303)

# ==================== ГРУППЫ РАЗДЕЛОВ ====================
BOARD_GROUPS = [
    ("group_general",  ["b", "news", "int", "rnd"]),
    ("group_tech",     ["g", "diy", "prog", "hard", "soft", "web", "sec"]),
    ("group_games",    ["v", "retro", "vgm", "mmo"]),
    ("group_media",    ["mu", "tv", "cin", "lit", "an", "a"]),
    ("group_creative", ["art", "p", "fa", "po", "ph"]),
    ("group_science",  ["sci", "his", "math", "lang"]),
    ("group_life",     ["fit", "ck", "out", "sp", "biz", "adv", "trv", "auto"]),
    ("group_misc",     ["pol", "r", "x", "weird", "dev"]),
]

# ==================== СТРАНИЦЫ ====================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    lang = get_lang(request)
    body = f'<div class="boardtitle">{esc(tr(lang,"welcome_title"))}</div>'
    body += f'<div class="boarddesc">{esc(tr(lang,"welcome_desc"))}</div>'
    body += f'<div class="help">{tr(lang,"howto")}</div>'

    used = set()
    for gkey, slugs in BOARD_GROUPS:
        present = [s for s in slugs if s in boards]
        if not present:
            continue
        body += f'<div class="group-title">&mdash; {esc(tr(lang,gkey))} &mdash;</div>'
        body += '<div class="board-grid">'
        for s in present:
            b = boards[s]
            used.add(s)
            body += (
                f'<div class="board-card">'
                f'<a href="/{s}/">{ICON_BOARD}/{s}/</a>'
                f'<div class="name">{esc(b["name"][lang])}</div>'
                f'<div class="desc">{esc(b["desc"][lang])}</div>'
                f'<div class="cnt">{len(b["threads"])}</div>'
                f'</div>'
            )
        body += '</div>'

    rest = [s for s in boards if s not in used]
    if rest:
        body += f'<div class="group-title">&mdash; {esc(tr(lang,"group_other"))} &mdash;</div>'
        body += '<div class="board-grid">'
        for s in rest:
            b = boards[s]
            body += (
                f'<div class="board-card">'
                f'<a href="/{s}/">{ICON_BOARD}/{s}/</a>'
                f'<div class="name">{esc(b["name"][lang])}</div>'
                f'<div class="desc">{esc(b["desc"][lang])}</div>'
                f'<div class="cnt">{len(b["threads"])}</div>'
                f'</div>'
            )
        body += '</div>'

    body += f'<div class="wrap"><div class="small" style="margin-top:22px;text-align:center">'
    body += tr(lang, "stats", b=len(boards), t=len(threads), p=len(posts))
    body += '</div></div>'
    return page(tr(lang, "site_title"), body, lang)

@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    lang = get_lang(request)
    body = f'<div class="boardtitle">{esc(tr(lang,"help_title"))}</div>'
    body += '<div class="wrap" style="max-width:820px">'
    body += tr(lang, "help_html")
    body += (
        f'<div style="text-align:center;margin-top:22px">'
        f'<a class="reply-btn" href="/">{ICON_HOME}{tr(lang,"btn_home")}</a></div>'
    )
    body += '</div>'
    return page(tr(lang, "help_title"), body, lang)

@app.get("/{board_slug}/", response_class=HTMLResponse)
def board_index(board_slug: str, request: Request):
    lang = get_lang(request)
    if board_slug not in boards:
        raise HTTPException(404, tr(lang, "err_404_board"))
    b = boards[board_slug]
    sorted_threads = sorted(
        [threads[tid] for tid in b["threads"] if tid in threads],
        key=lambda t: (not t.get("sticky", False), -t["bumped"]),
    )

    form = f'''
<div class="form-box">
  <h3>{ICON_PENCIL}{tr(lang,"form_new_thread")}</h3>
  <form method="post" action="/{board_slug}/new">
    <div class="form-row">
      <label>{tr(lang,"lbl_subject")} <span class="hint">{tr(lang,"hint_subject")}</span></label>
      <input type="text" name="subject" maxlength="120" placeholder="{esc(tr(lang,"ph_subject"))}">
    </div>
    <div class="form-row">
      <label>{tr(lang,"lbl_message")} <span class="hint">{tr(lang,"hint_message")}</span></label>
      <textarea id="reply-text" name="content" rows="7" maxlength="{MAX_CONTENT_LEN}"
        placeholder="{esc(tr(lang,"ph_message"))}"></textarea>
    </div>
    <div class="form-row">
      <label>{ICON_IMG}{tr(lang,"lbl_images")} <span class="hint">{tr(lang,"hint_images")}</span></label>
      <textarea name="image_urls" rows="2" placeholder="{esc(tr(lang,"ph_images"))}"
        style="min-height:60px"></textarea>
    </div>
    <div class="form-row">
      <label>{tr(lang,"lbl_password")} <span class="hint">{tr(lang,"hint_password")}</span></label>
      <input type="password" name="password" maxlength="100" placeholder="{esc(tr(lang,"ph_password"))}">
    </div>
    <div><button type="submit" class="big">{ICON_SEND}{tr(lang,"btn_create")}</button></div>
  </form>
</div>'''

    body = f'<div class="boardtitle">/{board_slug}/ &mdash; {esc(b["name"][lang])}</div>'
    body += f'<div class="boarddesc">{esc(b["desc"][lang])}</div>'
    body += '<div class="wrap">'
    body += form
    if not sorted_threads:
        body += f'<div style="text-align:center;padding:30px;color:#707070;font-size:12pt">{esc(tr(lang,"no_threads"))}</div>'
    else:
        for t in sorted_threads:
            body += render_thread_in_index(t["id"], lang)
    body += '</div>'
    return page(f'/{board_slug}/ — {b["name"][lang]}', body, lang)

@app.get("/{board_slug}/thread/{tid}", response_class=HTMLResponse)
def thread_view(board_slug: str, tid: int, request: Request):
    lang = get_lang(request)
    if board_slug not in boards:
        raise HTTPException(404, tr(lang, "err_404_board"))
    t = threads.get(tid)
    if not t or t["board"] != board_slug:
        raise HTTPException(404, tr(lang, "err_404_thread"))
    op = posts.get(tid)
    if not op:
        raise HTTPException(404, tr(lang, "err_404_thread"))

    body = f'<div class="boardtitle">/{board_slug}/ &mdash; {tr(lang,"postnum")}{tid}</div>'
    body += (
        f'<div class="wrap">'
        f'<a class="reply-btn" href="/{board_slug}/">{ICON_BACK}{tr(lang,"btn_back")}</a>'
        f'</div>'
    )
    body += '<div class="wrap" style="margin-top:12px">'
    body += render_post(op, lang, is_op=True, show_actions=False)
    for pid in t["posts"][1:]:
        p = posts.get(pid)
        if p and not p["deleted"]:
            body += render_post(p, lang, show_actions=False)
        elif p and p["deleted"]:
            body += f'<div class="post reply deleted">{esc(tr(lang,"post_deleted",pid=pid))}</div>'
    body += '</div>'

    if t.get("locked"):
        body += (
            f'<div class="form-box lockbox">'
            f'{ICON_LOCK}{esc(tr(lang,"thread_locked"))}'
            f'</div>'
        )
    else:
        body += f'''
<div class="form-box">
  <h3>{ICON_REPLY}{tr(lang,"form_reply_title",tid=tid)}</h3>
  <form method="post" action="/{board_slug}/thread/{tid}/reply">
    <div class="form-row">
      <label>{tr(lang,"lbl_message")} <span class="hint">{tr(lang,"hint_message")}</span></label>
      <textarea id="reply-text" name="content" rows="7" maxlength="{MAX_CONTENT_LEN}"
        placeholder="{esc(tr(lang,"ph_message"))}"></textarea>
    </div>
    <div class="form-row">
      <label>{ICON_IMG}{tr(lang,"lbl_images")} <span class="hint">{tr(lang,"hint_images")}</span></label>
      <textarea name="image_urls" rows="2" placeholder="{esc(tr(lang,"ph_images"))}"
        style="min-height:60px"></textarea>
    </div>
    <div class="form-row">
      <label>{tr(lang,"lbl_password")} <span class="hint">{tr(lang,"hint_password")}</span></label>
      <input type="password" name="password" maxlength="100">
    </div>
    <div class="form-row">
      <label style="font-weight:400;display:flex;align-items:center;gap:8px">
        <input type="checkbox" name="sage" value="1" style="width:auto">
        <span><b>{tr(lang,"lbl_sage")}</b>
        <span class="hint">{tr(lang,"hint_sage")}</span></span>
      </label>
    </div>
    <div><button type="submit" class="big">{ICON_SEND}{tr(lang,"btn_send")}</button></div>
  </form>
</div>'''
    body += (
        f'<div class="wrap" style="margin-bottom:24px;text-align:center;display:flex;gap:8px;justify-content:center">'
        f'<a class="reply-btn" href="/{board_slug}/">{ICON_BACK}{tr(lang,"btn_back")}</a>'
        f'<a class="reply-btn" href="#top">{ICON_TOP}{tr(lang,"btn_top")}</a></div>'
    )
    return page(f'/{board_slug}/ — {tr(lang,"postnum")}{tid}', body, lang)

# ==================== ДЕЙСТВИЯ ====================
@app.post("/{board_slug}/new")
async def create_thread(
    board_slug: str, request: Request,
    subject: str = Form(""),
    content: str = Form(""),
    image_urls: str = Form(""),
    password: str = Form(""),
):
    lang = get_lang(request)
    if board_slug not in boards:
        raise HTTPException(404, tr(lang, "err_404_board"))
    ip = get_ip(request)
    if not check_rate(ip):
        raise HTTPException(429, tr(lang, "err_429"))
    content = (content or "").strip()[:MAX_CONTENT_LEN]
    subject = (subject or "").strip()[:120]
    imgs = parse_images(image_urls)
    if not content and not imgs:
        raise HTTPException(400, tr(lang, "err_400_empty"))
    pid = new_id()
    posts[pid] = {
        "id": pid, "thread_id": pid, "board": board_slug,
        "subject": subject, "content": content, "image_urls": imgs,
        "time_ts": time.time(),
        "pw_hash": hash_pw(password) if password else None,
        "ip_hash": hash_ip(ip), "is_op": True, "sage": False, "deleted": False,
    }
    threads[pid] = {
        "id": pid, "board": board_slug, "posts": [pid],
        "created": time.time(), "bumped": time.time(),
        "locked": False, "sticky": False,
        "reply_count": 0, "image_count": len(imgs),
    }
    boards[board_slug]["threads"].append(pid)
    purge_old_threads(board_slug)
    return RedirectResponse(f"/{board_slug}/thread/{pid}", status_code=303)

@app.post("/{board_slug}/thread/{tid}/reply")
async def post_reply(
    board_slug: str, tid: int, request: Request,
    content: str = Form(""),
    image_urls: str = Form(""),
    password: str = Form(""),
    sage: str = Form(""),
):
    lang = get_lang(request)
    if board_slug not in boards:
        raise HTTPException(404, tr(lang, "err_404_board"))
    t = threads.get(tid)
    if not t or t["board"] != board_slug:
        raise HTTPException(404, tr(lang, "err_404_thread"))
    if t.get("locked"):
        raise HTTPException(403, tr(lang, "err_403_lock"))
    if len(t["posts"]) >= MAX_REPLIES_PER_THREAD:
        raise HTTPException(403, tr(lang, "err_403_full"))
    ip = get_ip(request)
    if not check_rate(ip):
        raise HTTPException(429, tr(lang, "err_429"))
    content = (content or "").strip()[:MAX_CONTENT_LEN]
    imgs = parse_images(image_urls)
    if not content and not imgs:
        raise HTTPException(400, tr(lang, "err_400_empty"))
    is_sage = bool(sage)
    pid = new_id()
    posts[pid] = {
        "id": pid, "thread_id": tid, "board": board_slug,
        "subject": "", "content": content, "image_urls": imgs,
        "time_ts": time.time(),
        "pw_hash": hash_pw(password) if password else None,
        "ip_hash": hash_ip(ip), "is_op": False, "sage": is_sage, "deleted": False,
    }
    t["posts"].append(pid)
    t["reply_count"] += 1
    t["image_count"] += len(imgs)
    if not is_sage:
        t["bumped"] = time.time()
    return RedirectResponse(f"/{board_slug}/thread/{tid}#p{pid}", status_code=303)

@app.post("/post/{pid}/delete")
async def delete_post(pid: int, password: str = Form("")):
    p = posts.get(pid)
    if not p or p["deleted"]:
        raise HTTPException(404, tr("ru", "err_404_post"))
    if not p["pw_hash"] or hash_pw(password) != p["pw_hash"]:
        raise HTTPException(403, tr("ru", "err_403_pw"))
    board = p["board"]
    tid = p["thread_id"]
    p["deleted"] = True
    p["content"] = ""
    p["image_urls"] = []
    if p["is_op"]:
        t = threads.pop(tid, None)
        if t:
            for rid in t["posts"]:
                rp = posts.get(rid)
                if rp:
                    rp["deleted"] = True
                    rp["content"] = ""
                    rp["image_urls"] = []
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
    create_board("b",    "Разное",              "Всё подряд. О чём угодно.",
                         "Random",              "Anything goes.")
    create_board("news", "Новости",             "Обсуждение новостей и событий.",
                         "News",                "News and current events.")
    create_board("int",  "Международный",       "Разговоры о странах и мире.",
                         "International",       "Countries and the world.")
    create_board("rnd",  "Случайности",         "Случайные темы без правил.",
                         "Random stuff",        "Random threads, no rules.")

    create_board("g",    "Технологии",          "Гаджеты, компьютеры, техника.",
                         "Technology",          "Gadgets, computers, hardware.")
    create_board("prog", "Программирование",    "Код, языки, разработка.",
                         "Programming",         "Code, languages, development.")
    create_board("hard", "Железо",              "Комплектующие, сборка ПК.",
                         "Hardware",            "PC parts and builds.")
    create_board("soft", "Софт",                "Программы и приложения.",
                         "Software",            "Apps and programs.")
    create_board("web",  "Веб-разработка",      "Сайты, HTML/CSS/JS, фреймворки.",
                         "Web dev",             "Sites, HTML/CSS/JS, frameworks.")
    create_board("sec",  "Кибербезопасность",   "Защита, уязвимости, крипто.",
                         "Cybersecurity",       "Security, vulns, crypto.")
    create_board("diy",  "Сделай сам",          "Мастерская, самоделки.",
                         "DIY",                 "Workshop and crafts.")

    create_board("v",    "Видеоигры",           "Обсуждение игр.",
                         "Video Games",         "Games discussion.")
    create_board("retro","Ретро-игры",          "Старые консоли и DOS-игры.",
                         "Retro Games",         "Old consoles and DOS games.")
    create_board("vgm",  "Игровая музыка",      "Саундтреки и чиптюн.",
                         "Video Game Music",    "Soundtracks and chiptune.")
    create_board("mmo",  "Онлайн-игры",         "MMO, шутеры, кооп.",
                         "Online Games",        "MMOs, shooters, co-op.")

    create_board("mu",   "Музыка",              "Всё о музыке и группах.",
                         "Music",               "Music and bands.")
    create_board("tv",   "Кино и сериалы",      "Фильмы, сериалы, аниме.",
                         "Movies & TV",         "Films, series, anime.")
    create_board("cin",  "Кинематограф",        "Режиссёры, киноискусство.",
                         "Cinematography",      "Directors and film art.")
    create_board("lit",  "Литература",          "Книги, стихи, писатели.",
                         "Literature",          "Books, poetry, writers.")
    create_board("an",   "Аниме и манга",       "Обсуждение аниме и манги.",
                         "Anime & Manga",       "Anime and manga discussion.")
    create_board("a",    "Аниме-арт",           "Картинки и арт по аниме.",
                         "Anime Art",           "Anime images and art.")

    create_board("art",  "Искусство",           "Рисунки, живопись, галереи.",
                         "Art",                 "Drawing, painting, galleries.")
    create_board("p",    "Фотография",          "Фото и техника съёмки.",
                         "Photography",         "Photos and camera gear.")
    create_board("fa",   "Рисование",           "Уроки и работы художников.",
                         "Drawing",             "Lessons and artworks.")
    create_board("po",   "Поэзия",              "Стихи и проза.",
                         "Poetry",              "Poems and prose.")
    create_board("ph",   "Философия",           "Размышления и дискуссии.",
                         "Philosophy",          "Thoughts and debates.")

    create_board("sci",  "Наука",               "Физика, химия, биология.",
                         "Science",             "Physics, chemistry, biology.")
    create_board("his",  "История",             "История стран и событий.",
                         "History",             "History of nations and events.")
    create_board("math", "Математика",          "Числа, формулы, задачи.",
                         "Math",                "Numbers, formulas, problems.")
    create_board("lang", "Иностранные языки",   "Английский и любые другие.",
                         "Languages",           "English and others.")

    create_board("fit",  "Фитнес и здоровье",   "Спорт, питание, ЗОЖ.",
                         "Fitness & Health",    "Training, nutrition, health.")
    create_board("ck",   "Еда и кулинария",     "Рецепты и готовка.",
                         "Food & Cooking",      "Recipes and cooking.")
    create_board("out",  "Природа и туризм",    "Походы, рыбалка, кемпинг.",
                         "Outdoors",            "Hiking, fishing, camping.")
    create_board("sp",   "Спорт",               "Футбол, хоккей, единоборства.",
                         "Sports",              "Football, hockey, MMA.")
    create_board("biz",  "Работа и деньги",     "Бизнес, фриланс, зарплаты.",
                         "Business & Money",    "Business, freelance, salaries.")
    create_board("adv",  "Советы",              "Спроси совета у анонимов.",
                         "Advice",              "Ask for advice.")
    create_board("trv",  "Путешествия",         "Страны, города, маршруты.",
                         "Travel",              "Countries, cities, routes.")
    create_board("auto", "Авто",                "Машины, мотоциклы, ремонт.",
                         "Auto",                "Cars, bikes, repairs.")

    create_board("pol",  "Политика",            "Политические обсуждения.",
                         "Politics",            "Political discussion.")
    create_board("r",    "Религия",             "Вера и религии мира.",
                         "Religion",            "Faith and world religions.")
    create_board("x",    "Взрослое (18+)",      "Раздел для взрослых тем.",
                         "Adult (18+)",         "Adult topics only.")
    create_board("weird","Странное",            "Всё необычное и непонятное.",
                         "Weird",               "Strange and unusual.")
    create_board("dev",  "Разработка форума",   "Обсуждение самого sldchat.",
                         "Meta",                "Discussion about sldchat itself.")

init_boards()

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    print("=" * 64)
    print("  sldchat — анонимный форум запущен")
    print(f"  Открой в браузере:   http://localhost:{PORT}/")
    print(f"  Ключ администратора: {ADMIN_KEY}")
    print(f"  Разделов:            {len(boards)}")
    print("=" * 64)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
