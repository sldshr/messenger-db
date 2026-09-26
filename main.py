#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SLDCommunity — анонимный форум в одном файле.

  * посты и комментарии — полностью анонимно (никаких ников, id, cookie)
  * всё хранится ТОЛЬКО в оперативной памяти (RAM), при перезапуске исчезает
  * к посту можно прикрепить одно фото — оно автоматически сжимается
  * разделы, SVG-иконки, адаптив под телефон и ПК
  * статистика: /stats/foned  (без фона, простой текст)

Запуск:  python main.py   ->  http://127.0.0.1:5000
"""

import io
import time
import html as _html
import itertools
import threading

from flask import Flask, request, redirect, Response, abort

# ----------------------------------------------------------------------------
# Pillow (для сжатия картинок). Если нет — фото сохраняется как есть.
# ----------------------------------------------------------------------------
try:
    from PIL import Image
    _HAS_PIL = True
    try:
        _LANCZOS = Image.Resampling.LANCZOS
    except AttributeError:            # старые версии Pillow
        _LANCZOS = Image.LANCZOS
except Exception:
    _HAS_PIL = False
    _LANCZOS = None

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024          # 10 МБ на запрос
app.config["JSON_AS_ASCII"] = False

# ----------------------------------------------------------------------------
# Настройки
# ----------------------------------------------------------------------------
MAX_POST_TEXT = 5000
MAX_COMMENT_TEXT = 2000

IMG_MAX_SIDE = 640          # максимальная сторона после сжатия
IMG_QUALITY = 60            # качество JPEG
IMG_MAX_UPLOAD = 8 * 1024 * 1024

SECTIONS = [
    ("general", "Общее",      "Общие темы, знакомства и объявления"),
    ("tech",    "Технологии", "Компьютеры, софт, гаджеты и интернет"),
    ("games",   "Игры",       "Игровые новости, обсуждения и советы"),
    ("music",   "Музыка",     "Что слушаете, что советуете"),
    ("humor",   "Юмор",       "Мемы, шутки и всё весёлое"),
    ("life",    "Жизнь",      "Личное, повседневное, истории"),
    ("help",    "Помощь",     "Вопросы и ответы, поддержка"),
    ("flood",   "Флудилка",   "Всё подряд и ни о чём"),
]
SECTION_MAP = {k: (n, d) for k, n, d in SECTIONS}

# ----------------------------------------------------------------------------
# Хранилище в оперативной памяти
# ----------------------------------------------------------------------------
_lock = threading.Lock()
_posts = {}                       # id -> dict
_post_ids = itertools.count(1)
_comment_ids = itertools.count(1)


def all_posts():
    """Все посты, новые сверху."""
    with _lock:
        return sorted(_posts.values(), key=lambda p: p["id"], reverse=True)


def section_posts(key):
    return [p for p in all_posts() if p["section"] == key]


def total_comments():
    with _lock:
        return sum(len(p["comments"]) for p in _posts.values())


# ----------------------------------------------------------------------------
# SVG-иконки
# ----------------------------------------------------------------------------
_ICONS = {
    "logo":    '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5z"/>',
    "general": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.5 3 2.5 15 0 18"/><path d="M12 3c-2.5 3-2.5 15 0 18"/>',
    "tech":    '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/>',
    "games":   '<rect x="2" y="6" width="20" height="12" rx="6"/><path d="M6 12h4M8 10v4"/><path d="M15.5 13h.01M18 11h.01"/>',
    "music":   '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    "humor":   '<circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><path d="M9 9h.01M15 9h.01"/>',
    "life":    '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1L12 21l7.7-7.6 1.1-1a5.5 5.5 0 0 0 0-7.8z"/>',
    "help":    '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    "flood":   '<path d="M3 12c2-3 5-3 7 0s5 3 7 0 4-2 4-2"/><path d="M3 18c2-3 5-3 7 0s5 3 7 0 4-2 4-2"/><path d="M3 6c2-3 5-3 7 0s5 3 7 0 4-2 4-2"/>',
    "image":   '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
    "comment": '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5z"/>',
    "stats":   '<path d="M18 20V10M12 20V4M6 20v-6"/>',
    "send":    '<path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/>',
    "clock":   '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "user":    '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "hash":    '<path d="M4 9h16M4 15h16M10 3L8 21M16 3l-2 18"/>',
    "back":    '<path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/>',
    "plus":    '<path d="M12 5v14M5 12h14"/>',
    "shield":  '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
}


def ico(name, size=16):
    path = _ICONS.get(name, _ICONS["general"])
    return ('<svg class="ico" width="%d" height="%d" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true">%s</svg>' % (size, size, path))


# ----------------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------------
def esc(s):
    return _html.escape(str(s), quote=True)


def fmt_time(ts):
    return time.strftime("%d.%m.%Y в %H:%M", time.localtime(ts))


def plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def compress_image(file_storage):
    """Возвращает (bytes, mime) или None. Картинка сжимается до минимума."""
    try:
        raw = file_storage.read()
    except Exception:
        return None
    if not raw or len(raw) > IMG_MAX_UPLOAD:
        return None

    if not _HAS_PIL:
        mime = file_storage.mimetype or "image/jpeg"
        if not mime.startswith("image/"):
            return None
        return raw, mime

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()

        # прозрачность -> белый фон
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")

        # уменьшаем по большей стороне
        w, h = img.size
        if max(w, h) > IMG_MAX_SIDE:
            if w >= h:
                nw, nh = IMG_MAX_SIDE, max(1, int(h * IMG_MAX_SIDE / w))
            else:
                nh, nw = IMG_MAX_SIDE, max(1, int(w * IMG_MAX_SIDE / h))
            img = img.resize((nw, nh), _LANCZOS)

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=IMG_QUALITY, optimize=True,
                 progressive=True)
        data = out.getvalue()

        # страховка: если сжатие не помогло — вернём оригинал
        if len(data) >= len(raw) and max(w, h) <= IMG_MAX_SIDE:
            return raw, (file_storage.mimetype or "image/jpeg")
        return data, "image/jpeg"
    except Exception:
        return None


# ----------------------------------------------------------------------------
# CSS / JS
# ----------------------------------------------------------------------------
CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:Verdana,Tahoma,Arial,Helvetica,sans-serif;
  font-size:12px;line-height:1.55;color:#1f2b38;
  background:#dbe4ee;
  background-image:linear-gradient(#dde7f1,#c6d4e5);
  background-attachment:fixed;
  -webkit-text-size-adjust:100%;
}
a{color:#1a5fa8;text-decoration:none}
a:hover{color:#c0392b;text-decoration:underline}
img{max-width:100%}
.ico{flex:none;vertical-align:-2px}

#header{
  background:#3b6ea8;
  background-image:linear-gradient(#5d92cd,#33628f);
  border:1px solid #234d75;border-top:0;
  border-radius:0 0 8px 8px;
  box-shadow:0 2px 5px rgba(20,50,90,.35);
  padding:10px 14px 9px;color:#fff;
}
.hrow{display:flex;align-items:center;justify-content:space-between;
      gap:10px;flex-wrap:wrap}
.logo{display:flex;align-items:center;gap:8px;font-size:20px;font-weight:bold;
      color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.45);letter-spacing:.3px}
.logo:hover{color:#fff;text-decoration:none}
.logo span{color:#ffe680}
.tagline{font-size:11px;color:#dbe9f8}

#wrap{max-width:1000px;margin:0 auto;padding:0 10px 40px}

.nav{display:flex;flex-wrap:wrap;gap:3px;background:#eef3f9;
     border:1px solid #b7c8dc;border-radius:6px;padding:5px;margin:10px 0}
.nav a{display:flex;align-items:center;gap:5px;padding:5px 9px;
       border-radius:5px;color:#20486f;font-size:12px;border:1px solid transparent;
       white-space:nowrap}
.nav a:hover{background:#fff;border-color:#b7c8dc;text-decoration:none;color:#c0392b}
.nav a.active{background:#3b6ea8;background-image:linear-gradient(#5d92cd,#33628f);
       color:#fff;border-color:#234d75}

.box{background:#fff;border:1px solid #b7c8dc;border-radius:6px;margin-bottom:12px;
     box-shadow:0 1px 2px rgba(30,60,100,.08);overflow:hidden}
.box-title{background:#eef3f9;background-image:linear-gradient(#f7fafd,#e4ecf6);
     border-bottom:1px solid #cdd9e7;padding:7px 10px;font-weight:bold;
     color:#20486f;display:flex;align-items:center;gap:6px;font-size:12px}
.box-body{padding:10px}

.hero{background:#fff;border:1px solid #b7c8dc;border-radius:6px;padding:14px;
      margin-bottom:12px;box-shadow:0 1px 2px rgba(30,60,100,.08)}
.hero h1{margin:0 0 6px;font-size:18px;color:#20486f}
.hero p{margin:0 0 10px;color:#4a5c6e}
.stats-row{display:flex;gap:8px;flex-wrap:wrap}
.stat{background:#eef3f9;border:1px solid #cdd9e7;border-radius:5px;
      padding:6px 10px;font-size:11px;color:#20486f;display:flex;align-items:center;gap:5px}
.stat b{font-size:13px;color:#c0392b}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:10px}
.card{display:block;background:#fff;border:1px solid #c3d2e2;border-radius:6px;
      padding:10px;color:#1f2b38;transition:.12s}
.card:hover{border-color:#5d92cd;background:#f7fbff;text-decoration:none;
      color:#1f2b38;box-shadow:0 1px 6px rgba(60,110,170,.25)}
.card-head{display:flex;align-items:center;gap:6px;font-size:13px;color:#20486f;
      font-weight:bold;margin-bottom:4px}
.card-desc{color:#5a6b7d;font-size:11px;min-height:30px}
.card-count{margin-top:6px;font-size:10px;color:#7b8b9c;border-top:1px dashed #dde7f1;
      padding-top:5px}

.post{background:#fff;border:1px solid #c3d2e2;border-radius:6px;margin-bottom:10px;
      overflow:hidden}
.post-head{background:#f1f6fb;border-bottom:1px solid #dde7f1;padding:6px 10px;
      font-size:11px;color:#5a6b7d;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.post-body{padding:10px;display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start}
.post-text{flex:1 1 240px;min-width:200px;white-space:pre-wrap;
      word-wrap:break-word;overflow-wrap:break-word}
.post-img{flex:0 0 auto}
.post-img img{max-width:240px;border:1px solid #b7c8dc;border-radius:4px;display:block;
      background:#f4f7fa}
.post-img img:hover{border-color:#5d92cd}
.post-foot{border-top:1px solid #e7eef6;padding:6px 10px;background:#fbfdff;
      font-size:11px;display:flex;gap:14px;flex-wrap:wrap}
.post-foot a{display:flex;align-items:center;gap:5px}

.badge{background:#3b6ea8;background-image:linear-gradient(#5d92cd,#33628f);
      color:#fff;border-radius:9px;padding:1px 8px;font-size:10px;font-weight:bold}
.anon{display:flex;align-items:center;gap:4px;color:#2e7d32;font-weight:bold}
.dot{color:#a9b8c8}

.comment{background:#fff;border:1px solid #d5e0ec;border-radius:5px;
      margin-bottom:8px;overflow:hidden}
.comment-head{background:#f5f9fd;border-bottom:1px solid #e7eef6;padding:5px 10px;
      font-size:11px;color:#6b7c8e;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.comment-text{padding:9px 10px;white-space:pre-wrap;word-wrap:break-word;
      overflow-wrap:break-word}

label.lbl{display:block;font-size:11px;color:#20486f;font-weight:bold;
      margin:0 0 4px}
textarea,input[type=text],select{
  width:100%;border:1px solid #b7c8dc;border-radius:4px;padding:6px 8px;
  font:12px Verdana,Tahoma,Arial,sans-serif;background:#fdfefe;color:#1f2b38;
  margin-bottom:8px}
textarea:focus,input:focus,select:focus{outline:none;border-color:#5d92cd;
      box-shadow:0 0 4px rgba(93,146,205,.65)}
textarea{resize:vertical;min-height:80px}

input[type=file]{width:100%;font-size:11px;margin-bottom:8px;
      border:1px dashed #b7c8dc;border-radius:4px;padding:8px;background:#f8fbfe}

.btn{display:inline-flex;align-items:center;gap:6px;
      background:#3b6ea8;background-image:linear-gradient(#5d92cd,#33628f);
      border:1px solid #234d75;color:#fff;border-radius:5px;padding:6px 14px;
      cursor:pointer;font:bold 12px Verdana,Tahoma,Arial,sans-serif;
      text-shadow:0 1px 1px rgba(0,0,0,.3)}
.btn:hover{background-image:linear-gradient(#6ba0d9,#3d6f9e);color:#fff;
      text-decoration:none}
.btn:active{background-image:linear-gradient(#33628f,#5d92cd)}

.form-row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.form-row .col{flex:1 1 200px}

#preview img{max-width:180px;border:1px solid #b7c8dc;border-radius:4px;
      display:block;margin-top:2px}

.notice{background:#fff8e1;border:1px solid #f0d98a;border-radius:5px;
      padding:8px 10px;color:#7a5c00;font-size:11px;margin-bottom:10px;
      display:flex;align-items:center;gap:6px}
.empty{color:#7b8b9c;font-style:italic;padding:14px;text-align:center;
      background:#fff;border:1px dashed #c3d2e2;border-radius:6px}

#footer{margin-top:18px;padding:10px 4px;border-top:1px solid #b7c8dc;
      color:#6b7c8e;font-size:11px;text-align:center;line-height:1.8}
#footer a{display:inline-flex;align-items:center;gap:4px}

.crumbs{font-size:11px;color:#6b7c8e;margin:0 0 8px;display:flex;
      align-items:center;gap:6px;flex-wrap:wrap}

@media (max-width:700px){
  body{font-size:12px}
  #header{padding:9px 10px 8px}
  .logo{font-size:17px}
  .tagline{font-size:10px}
  #wrap{padding:0 7px 30px}
  .nav{gap:3px;padding:4px}
  .nav a{padding:5px 7px;font-size:11px}
  .nav a span{display:none}
  .nav a{padding:7px 9px}
  .grid{grid-template-columns:1fr 1fr}
  .card-desc{display:none}
  .card{padding:8px}
  .card-head{font-size:12px}
  .post-body{flex-direction:column}
  .post-img{width:100%}
  .post-img img{max-width:100%}
  .hero h1{font-size:15px}
  textarea{min-height:90px}
}
@media (max-width:360px){
  .grid{grid-template-columns:1fr}
}
"""

JS = """
(function(){
  var f = document.querySelector('input[type=file][name=photo]');
  if(!f) return;
  f.addEventListener('change', function(){
    var box = document.getElementById('preview');
    if(!box) return;
    box.innerHTML = '';
    if(f.files && f.files[0]){
      var img = document.createElement('img');
      img.src = URL.createObjectURL(f.files[0]);
      img.alt = '\u043f\u0440\u0435\u0432\u044c\u044e';
      box.appendChild(img);
    }
  });
})();
"""


# ----------------------------------------------------------------------------
# Каркас страницы
# ----------------------------------------------------------------------------
def layout(title, content, active=""):
    nav = []
    for key, name, desc in SECTIONS:
        cls = " active" if key == active else ""
        nav.append(
            '<a class="%s" href="/s/%s" title="%s">%s<span>%s</span></a>'
            % (cls.strip(), key, esc(desc), ico(key, 14), esc(name))
        )
    nav = "".join(nav)
    year = time.strftime("%Y")

    return (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="theme-color" content="#3b6ea8">'
        '<title>' + esc(title) + ' — SLDCommunity</title>'
        '<style>' + CSS + '</style></head><body>'
        '<div id="header"><div class="hrow">'
        '<a class="logo" href="/">' + ico("logo", 22) + 'SLD<span>Community</span></a>'
        '<div class="tagline">анонимно &middot; без регистрации &middot; без ников</div>'
        '</div></div>'
        '<div id="wrap">'
        '<div class="nav">' + nav + '</div>'
        + content +
        '<div id="footer">SLDCommunity &copy; 2010&ndash;' + year +
        ' &middot; всё хранится только в оперативной памяти &middot; '
        '<a href="/stats/foned">' + ico("stats", 12) + 'статистика</a>'
        '</div></div>'
        '<script>' + JS + '</script>'
        '</body></html>'
    )


def render_post(p, with_comments_link=True):
    sec_name = SECTION_MAP.get(p["section"], ("Общее", ""))[0]
    n = len(p["comments"])

    img_html = ""
    if p.get("img"):
        img_html = (
            '<div class="post-img"><a href="/img/%d" target="_blank" '
            'title="Открыть в полном размере">'
            '<img src="/img/%d" alt="изображение" loading="lazy"></a></div>'
            % (p["id"], p["id"])
        )

    foot = ""
    if with_comments_link:
        foot = (
            '<div class="post-foot">'
            '<a href="/p/%d">%sКомментарии (%d)</a>'
            '%s'
            '</div>'
            % (p["id"], ico("comment", 13), n,
               ('<span style="color:#7b8b9c">с фото</span>' if p.get("img") else ""))
        )

    return (
        '<div class="post">'
        '<div class="post-head">'
        '<span class="badge">#%d</span>'
        '<span class="anon">%sАноним</span>'
        '<span class="dot">&middot;</span>'
        '<span>%s</span>'
        '<span class="dot">&middot;</span>'
        '<span>%s%s</span>'
        '</div>'
        '<div class="post-body">'
        '<div class="post-text">%s</div>'
        '%s'
        '</div>'
        '%s'
        '</div>'
        % (p["id"], ico("user", 12), fmt_time(p["ts"]),
           ico("hash", 11), esc(sec_name),
           esc(p["text"]), img_html, foot)
    )


def post_form(selected="general", compact=False):
    opts = "".join(
        '<option value="%s"%s>%s</option>'
        % (k, " selected" if k == selected else "", esc(n))
        for k, n, _ in SECTIONS
    )
    return (
        '<form class="box" method="post" action="/create" '
        'enctype="multipart/form-data">'
        '<div class="box-title">' + ico("plus", 13) + 'Новый пост</div>'
        '<div class="box-body">'
        '<div class="form-row">'
        '<div class="col" style="flex:0 0 190px">'
        '<label class="lbl" for="section">Раздел</label>'
        '<select id="section" name="section">' + opts + '</select>'
        '</div>'
        '<div class="col">'
        '<label class="lbl">Фото (необязательно, одно)</label>'
        '<input type="file" name="photo" accept="image/*">'
        '</div>'
        '</div>'
        '<label class="lbl" for="text">Текст сообщения</label>'
        '<textarea id="text" name="text" maxlength="' + str(MAX_POST_TEXT) + '" '
        'placeholder="Пиши анонимно. Никто не узнает, кто ты."></textarea>'
        '<div id="preview"></div>'
        '<button class="btn" type="submit">' + ico("send", 13) + 'Отправить</button>'
        '</div></form>'
    )


# ----------------------------------------------------------------------------
# Маршруты
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    posts = all_posts()
    tp = len(posts)
    tc = sum(len(p["comments"]) for p in posts)
    tw = sum(1 for p in posts if p.get("img"))

    cards = []
    for key, name, desc in SECTIONS:
        cnt = sum(1 for p in posts if p["section"] == key)
        cards.append(
            '<a class="card" href="/s/%s">'
            '<div class="card-head">%s%s</div>'
            '<div class="card-desc">%s</div>'
            '<div class="card-count">%d %s</div>'
            '</a>'
            % (key, ico(key, 17), esc(name), esc(desc),
               cnt, plural(cnt, "пост", "поста", "постов"))
        )

    latest = ""
    if posts:
        latest = "".join(render_post(p) for p in posts[:5])
    else:
        latest = '<div class="empty">Пока ни одного поста. Будь первым!</div>'

    content = (
        '<div class="hero">'
        '<h1>SLDCommunity — анонимный форум</h1>'
        '<p>Никаких ников, аккаунтов и идентификаторов. Пиши что думаешь — '
        'всё живёт только в оперативной памяти сервера и исчезает при перезапуске.</p>'
        '<div class="stats-row">'
        '<span class="stat">' + ico("hash", 12) + 'Постов: <b>' + str(tp) + '</b></span>'
        '<span class="stat">' + ico("comment", 12) + 'Комментариев: <b>' + str(tc) + '</b></span>'
        '<span class="stat">' + ico("image", 12) + 'С фото: <b>' + str(tw) + '</b></span>'
        '<span class="stat">' + ico("shield", 12) + 'Разделов: <b>' + str(len(SECTIONS)) + '</b></span>'
        '</div></div>'
        + post_form("general") +
        '<div class="box"><div class="box-title">' + ico("general", 13) +
        'Разделы форума</div><div class="box-body"><div class="grid">'
        + "".join(cards) +
        '</div></div></div>'
        '<div class="box"><div class="box-title">' + ico("clock", 13) +
        'Последние посты</div><div class="box-body">' + latest + '</div></div>'
    )
    return layout("Главная", content)


@app.route("/s/<key>")
def section(key):
    if key not in SECTION_MAP:
        abort(404)
    name, desc = SECTION_MAP[key]
    posts = section_posts(key)

    body = "".join(render_post(p) for p in posts)
    if not posts:
        body = '<div class="empty">В этом разделе пока пусто. Создай первый пост!</div>'

    content = (
        '<div class="crumbs"><a href="/">' + ico("back", 12) + 'Главная</a>'
        '<span class="dot">&rarr;</span><b>' + esc(name) + '</b></div>'
        '<div class="hero"><h1>' + ico(key, 18) + ' ' + esc(name) + '</h1>'
        '<p>' + esc(desc) + '</p>'
        '<div class="stats-row"><span class="stat">' + ico("hash", 12) +
        'Постов: <b>' + str(len(posts)) + '</b></span>'
        '<span class="stat">' + ico("comment", 12) + 'Комментариев: <b>' +
        str(sum(len(p["comments"]) for p in posts)) + '</b></span></div></div>'
        + post_form(key) +
        '<div class="box"><div class="box-title">' + ico(key, 13) +
        'Посты раздела</div><div class="box-body">' + body + '</div></div>'
    )
    return layout(name, content, active=key)


@app.route("/p/<int:pid>")
def post_page(pid):
    p = _posts.get(pid)
    if not p:
        abort(404)
    name = SECTION_MAP.get(p["section"], ("Общее", ""))[0]

    comments = ""
    if p["comments"]:
        parts = []
        for c in p["comments"]:
            parts.append(
                '<div class="comment">'
                '<div class="comment-head">'
                '<span class="anon">' + ico("user", 11) + 'Аноним</span>'
                '<span class="dot">&middot;</span>'
                '<span>' + fmt_time(c["ts"]) + '</span>'
                '<span class="dot">&middot;</span>'
                '<span>#' + str(c["id"]) + '</span>'
                '</div>'
                '<div class="comment-text">' + esc(c["text"]) + '</div>'
                '</div>'
            )
        comments = "".join(parts)
    else:
        comments = '<div class="empty">Комментариев пока нет. Напиши первым!</div>'

    comment_form = (
        '<form class="box" method="post" action="/p/' + str(pid) + '/comment">'
        '<div class="box-title">' + ico("comment", 13) + 'Анонимный комментарий</div>'
        '<div class="box-body">'
        '<textarea name="text" maxlength="' + str(MAX_COMMENT_TEXT) + '" '
        'placeholder="Твой комментарий..."></textarea>'
        '<button class="btn" type="submit">' + ico("send", 13) + 'Отправить</button>'
        '</div></form>'
    )

    content = (
        '<div class="crumbs"><a href="/">' + ico("back", 12) + 'Главная</a>'
        '<span class="dot">&rarr;</span>'
        '<a href="/s/' + p["section"] + '">' + esc(name) + '</a>'
        '<span class="dot">&rarr;</span><b>Пост #' + str(pid) + '</b></div>'
        + render_post(p, with_comments_link=False) +
        '<div class="box"><div class="box-title">' + ico("comment", 13) +
        'Комментарии (' + str(len(p["comments"])) + ')</div>'
        '<div class="box-body">' + comments + '</div></div>'
        + comment_form
    )
    return layout("Пост #%d" % pid, content, active=p["section"])


@app.route("/create", methods=["POST"])
def create():
    section_key = (request.form.get("section") or "general").strip()
    if section_key not in SECTION_MAP:
        section_key = "general"

    text = (request.form.get("text") or "").strip()
    if len(text) > MAX_POST_TEXT:
        text = text[:MAX_POST_TEXT]

    photo = request.files.get("photo")
    image = None
    if photo and photo.filename:
        image = compress_image(photo)

    if not text and not image:
        return redirect(request.referrer or "/")

    with _lock:
        pid = next(_post_ids)
        _posts[pid] = {
            "id": pid,
            "section": section_key,
            "text": text if text else "(без текста)",
            "img": image[0] if image else None,
            "mime": image[1] if image else None,
            "ts": time.time(),
            "comments": [],
        }
    return redirect("/p/%d" % pid)


@app.route("/p/<int:pid>/comment", methods=["POST"])
def add_comment(pid):
    p = _posts.get(pid)
    if not p:
        abort(404)
    text = (request.form.get("text") or "").strip()
    if len(text) > MAX_COMMENT_TEXT:
        text = text[:MAX_COMMENT_TEXT]
    if text:
        with _lock:
            p["comments"].append({
                "id": next(_comment_ids),
                "text": text,
                "ts": time.time(),
            })
    return redirect("/p/%d" % pid)


@app.route("/img/<int:pid>")
def get_image(pid):
    p = _posts.get(pid)
    if not p or not p.get("img"):
        abort(404)
    return Response(
        p["img"],
        mimetype=p["mime"] or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.route("/stats/foned")
def stats_foned():
    """Статистика без фона — простой текст."""
    posts = all_posts()
    tp = len(posts)
    tc = sum(len(p["comments"]) for p in posts)
    tw = sum(1 for p in posts if p.get("img"))

    lines = [
        "SLDCommunity :: /stats/foned",
        "",
        "Всего постов:       %d" % tp,
        "Всего комментариев: %d" % tc,
        "Постов с фото:      %d" % tw,
        "Разделов:           %d" % len(SECTIONS),
        "",
        "Хранилище: оперативная память (RAM), без базы данных.",
        "Время сервера: %s" % time.strftime("%d.%m.%Y %H:%M:%S"),
    ]
    return Response("\n".join(lines) + "\n",
                    mimetype="text/plain; charset=utf-8")


# ----------------------------------------------------------------------------
# Обработчики ошибок
# ----------------------------------------------------------------------------
def _err_page(code, title, text):
    content = (
        '<div class="hero"><h1>%s</h1><p>%s</p>'
        '<a class="btn" href="/">%sНа главную</a></div>'
        % (esc(title), esc(text), ico("back", 13))
    )
    return layout("Ошибка %d" % code, content), code


@app.errorhandler(404)
def err404(e):
    return _err_page(404, "404 — страница не найдена",
                     "Такой страницы здесь нет. Возможно, пост был удалён "
                     "или сервер перезапускался (всё хранится в памяти).")


@app.errorhandler(413)
def err413(e):
    return _err_page(413, "413 — файл слишком большой",
                     "Максимальный размер загружаемого файла — 10 МБ.")


@app.errorhandler(500)
def err500(e):
    return _err_page(500, "500 — внутренняя ошибка",
                     "Что-то пошло не так. Попробуй ещё раз.")


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 56)
    print("  SLDCommunity запущен")
    print("  Открой:  http://127.0.0.1:5000")
    print("  Статистика: http://127.0.0.1:5000/stats/foned")
    print("  Всё хранится в оперативной памяти (RAM).")
    print("=" * 56)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
