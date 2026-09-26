# SLDCommunity — анонимный форум в стиле 2010
# Запуск:  python main.py
# Или:     uvicorn main:app --host 0.0.0.0 --port 8000
#
# Зависимости:  pip install fastapi uvicorn pillow python-multipart

import base64
import html
import io
import time

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from PIL import Image

try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:          # старые версии Pillow
    RESAMPLE = Image.LANCZOS


app = FastAPI(title="SLDCommunity")

# ---------------------------------------------------------------------------
#  ДАННЫЕ — всё живёт только в оперативной памяти
# ---------------------------------------------------------------------------
posts: list[dict] = []
next_id = 1

SECTIONS = [
    ("general", "Общее"),
    ("tech",    "Технологии"),
    ("humor",   "Юмор"),
    ("life",    "Жизнь"),
    ("games",   "Игры"),
    ("music",   "Музыка"),
    ("art",     "Творчество"),
    ("help",    "Помощь"),
]
SECTION_MAP = dict(SECTIONS)


# ---------------------------------------------------------------------------
#  SVG-иконки
# ---------------------------------------------------------------------------
def _svg(paths: str, size: int = 14) -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{paths}</svg>'
    )


ICON_PENCIL = _svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>')
ICON_COMMENT = _svg('<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>')
ICON_IMAGE = _svg('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>')
ICON_FOLDER = _svg('<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>')
ICON_HOME = _svg('<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>')
ICON_CLOCK = _svg('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>', 12)
ICON_LOGO = _svg('<circle cx="12" cy="12" r="10"/><path d="M12 8v8M8 12h8"/>', 22)
ICON_CHART = _svg('<path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="7"/><rect x="12" y="6" width="3" height="11"/><rect x="17" y="13" width="3" height="4"/>')


# ---------------------------------------------------------------------------
#  Утилиты
# ---------------------------------------------------------------------------
def esc(s) -> str:
    return html.escape(s or "")


def nl2br(s: str) -> str:
    return esc(s).replace("\n", "<br>")


def time_ago(ts: float) -> str:
    d = int(time.time() - ts)
    if d < 60:
        return "только что"
    if d < 3600:
        return f"{d // 60} мин. назад"
    if d < 86400:
        return f"{d // 3600} ч. назад"
    return f"{d // 86400} дн. назад"


def get_stats():
    total_posts = len(posts)
    total_comments = sum(len(p["comments"]) for p in posts)
    return total_posts, total_comments


# ---------------------------------------------------------------------------
#  CSS — старый добрый 2010: градиенты, бевелы, Verdana
# ---------------------------------------------------------------------------
CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: Verdana, Tahoma, Geneva, Arial, sans-serif;
  font-size: 13px;
  line-height: 1.45;
  color: #1b2a3a;
  background-color: #c6d8ea;
  background-image: linear-gradient(#b3cbe4 0%, #dbe8f5 45%, #eaf2fa 100%);
  background-attachment: fixed;
  min-height: 100vh;
}
a { color: #1c4f8f; text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 900px; margin: 0 auto; padding: 0 10px; }

/* ---- шапка ---- */
.topbar {
  background: linear-gradient(#5d92cd 0%, #3a6fae 45%, #2b5a95 55%, #1e4577 100%);
  border-bottom: 3px solid #163459;
  box-shadow: 0 2px 8px rgba(0,0,0,.35);
  color: #fff;
  padding: 10px 0;
}
.topbar-inner { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px; }
.logo {
  font-size: 22px; font-weight: bold; letter-spacing: .5px;
  text-shadow: 0 1px 0 #0d2440, 0 0 10px rgba(255,255,255,.25);
  display: flex; align-items: center; gap: 8px;
}
.tagline { font-size: 11px; color: #cfe2f7; text-shadow: 0 1px 0 #0d2440; }

/* ---- навигация по разделам ---- */
.nav {
  display: flex; flex-wrap: wrap; gap: 6px;
  margin: 12px 0;
  padding: 8px;
  background: linear-gradient(#f4f9fe, #dde9f6);
  border: 1px solid #93b0cf;
  border-radius: 6px;
  box-shadow: inset 0 1px 0 #fff, 0 1px 3px rgba(0,0,0,.12);
}
.navbtn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 10px;
  font-size: 12px;
  color: #1c3f66;
  background: linear-gradient(#ffffff, #e6eefa 48%, #d8e5f4 52%, #c6d8ec);
  border: 1px solid #8fadcd;
  border-radius: 4px;
  box-shadow: inset 0 1px 0 #fff;
}
.navbtn:hover { background: linear-gradient(#ffffff, #f0f6fd 48%, #e4eef9 52%, #d4e3f3); text-decoration: none; }
.navbtn.active {
  background: linear-gradient(#4b82c0, #2b5a95);
  color: #fff; border-color: #1e4577;
  text-shadow: 0 1px 0 #16345a;
}

/* ---- форма нового поста ---- */
.newpost {
  background: #fff;
  border: 1px solid #93b0cf;
  border-radius: 6px;
  margin-bottom: 14px;
  overflow: hidden;
  box-shadow: 0 1px 4px rgba(0,0,0,.15);
}
.newpost .nphead {
  background: linear-gradient(#f4f9fe, #dbe7f5);
  border-bottom: 1px solid #c3d6ea;
  padding: 7px 12px;
  font-weight: bold;
  font-size: 12px;
  color: #1c3f66;
  display: flex; align-items: center; gap: 6px;
}
.newpost .npbody { padding: 10px 12px 12px; }
textarea, input[type=text], select {
  font-family: inherit;
  font-size: 13px;
  color: #1b2a3a;
  border: 1px solid #8aa8c8;
  border-radius: 3px;
  padding: 6px 8px;
  background: #fbfdff;
  box-shadow: inset 0 1px 2px rgba(0,0,0,.08);
  outline: none;
}
textarea:focus, input[type=text]:focus, select:focus { border-color: #4b82c0; background: #fff; }
textarea { width: 100%; resize: vertical; min-height: 70px; }
.nprow { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 8px; }
.nprow select { flex: 0 1 auto; }
.filebtn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 6px 12px;
  font-size: 12px;
  color: #1c3f66;
  background: linear-gradient(#ffffff, #e6eefa 48%, #d8e5f4 52%, #c6d8ec);
  border: 1px solid #8fadcd;
  border-radius: 4px;
  box-shadow: inset 0 1px 0 #fff;
  cursor: pointer;
  user-select: none;
}
.filebtn:hover { background: linear-gradient(#ffffff, #f0f6fd 48%, #e4eef9 52%, #d4e3f3); }
.fname { font-size: 11px; color: #5d7displaced; }
.fname { font-size: 11px; color: #5d7a99; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ---- кнопки ---- */
.btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 6px 14px;
  font-family: inherit;
  font-size: 12px;
  font-weight: bold;
  color: #14314f;
  background: linear-gradient(#ffffff, #e6eefa 48%, #d8e5f4 52%, #c6d8ec);
  border: 1px solid #7f9dc0;
  border-radius: 4px;
  box-shadow: inset 0 1px 0 #fff, 0 1px 1px rgba(0,0,0,.08);
  cursor: pointer;
}
.btn:hover { background: linear-gradient(#ffffff, #f2f8fe 48%, #e6f0fb 52%, #d6e5f5); }
.btn:active { background: #c6d8ec; box-shadow: inset 0 1px 3px rgba(0,0,0,.2); }
.btn.small { padding: 4px 10px; font-size: 11px; font-weight: normal; }

/* ---- пост ---- */
.post {
  background: #fff;
  border: 1px solid #9ab3cc;
  border-radius: 6px;
  margin-bottom: 14px;
  overflow: hidden;
  box-shadow: 0 1px 4px rgba(0,0,0,.13);
}
.phead {
  background: linear-gradient(#f4f9fe, #dde9f5);
  border-bottom: 1px solid #c3d6ea;
  padding: 7px 10px;
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; flex-wrap: wrap;
  font-size: 11px;
}
.badge {
  background: linear-gradient(#6fa3dd, #3d74b5);
  color: #fff;
  padding: 2px 9px;
  border-radius: 10px;
  font-size: 11px;
  border: 1px solid #2c5a92;
  text-shadow: 0 1px 0 #24486f;
  display: inline-flex; align-items: center; gap: 4px;
}
.ptime { color: #6c8299; display: inline-flex; align-items: center; gap: 4px; }
.ptext { padding: 11px 13px; word-wrap: break-word; overflow-wrap: anywhere; }
.postimg { padding: 0 13px 11px; }
.postimg img {
  max-width: 100%; height: auto; display: block;
  border: 1px solid #93b0cf; border-radius: 4px;
  background: #eef4fa;
  box-shadow: 0 1px 3px rgba(0,0,0,.15);
}

/* ---- комментарии ---- */
.comments {
  background: #f7fafd;
  border-top: 1px solid #dbe6f2;
  padding: 9px 12px 11px;
}
.ctitle {
  font-size: 11px; font-weight: bold; color: #48688b;
  display: flex; align-items: center; gap: 5px;
  margin-bottom: 7px; text-transform: uppercase; letter-spacing: .3px;
}
.comment {
  background: #fff;
  border: 1px solid #dbe6f2;
  border-left: 3px solid #7ba7d4;
  border-radius: 3px;
  padding: 6px 9px;
  margin-bottom: 6px;
}
.cmain { white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; }
.cmeta { color: #8299b0; font-size: 11px; margin-top: 3px; display: flex; align-items: center; gap: 4px; }
.nocomments { color: #91a6bb; font-size: 11px; font-style: italic; margin-bottom: 8px; }
.cform { margin-top: 8px; }
.cform textarea { min-height: 46px; font-size: 12px; }
.cform .btn { margin-top: 6px; }

.empty {
  background: #fff;
  border: 1px dashed #9ab3cc;
  border-radius: 6px;
  padding: 30px 20px;
  text-align: center;
  color: #6c8299;
  font-size: 13px;
}

.footer {
  text-align: center;
  color: #6c8299;
  font-size: 11px;
  padding: 18px 0 26px;
  text-shadow: 0 1px 0 #fff;
}

/* ---- мобилки ---- */
@media (max-width: 640px) {
  body { font-size: 12.5px; }
  .wrap { padding: 0 8px; }
  .logo { font-size: 18px; }
  .logo svg { width: 18px; height: 18px; }
  .tagline { display: none; }
  .nav { gap: 4px; padding: 6px; margin: 8px 0; }
  .navbtn { padding: 5px 7px; font-size: 11px; gap: 3px; }
  .navbtn svg { width: 12px; height: 12px; }
  .ptext { padding: 9px 10px; }
  .postimg { padding: 0 10px 9px; }
  .comments { padding: 8px 10px 10px; }
  .nprow { gap: 6px; }
  .filebtn, .btn { padding: 6px 10px; font-size: 11px; }
}
"""


# ---------------------------------------------------------------------------
#  Рендер
# ---------------------------------------------------------------------------
def render_post(p: dict, back: str) -> str:
    img_html = ""
    if p.get("image"):
        img_html = f'<div class="postimg"><img src="{p["image"]}" alt="фото" loading="lazy"></div>'

    if p["comments"]:
        c_html = ""
        for c in p["comments"]:
            c_html += (
                '<div class="comment">'
                f'<div class="cmain">{nl2br(c["text"])}</div>'
                f'<div class="cmeta">{ICON_CLOCK} {time_ago(c["time"])}</div>'
                '</div>'
            )
    else:
        c_html = '<div class="nocomments">Комментариев пока нет. Будь первым анонимом.</div>'

    return f"""
<div class="post" id="p{p['id']}">
  <div class="phead">
    <span class="badge">{ICON_FOLDER} {esc(SECTION_MAP.get(p['section'], p['section']))}</span>
    <span class="ptime">{ICON_CLOCK} {time_ago(p['time'])}</span>
  </div>
  <div class="ptext">{nl2br(p['text'])}</div>
  {img_html}
  <div class="comments">
    <div class="ctitle">{ICON_COMMENT} Комментарии ({len(p['comments'])})</div>
    {c_html}
    <form class="cform" action="/comment/{p['id']}" method="post">
      <input type="hidden" name="back" value="{esc(back)}">
      <textarea name="text" placeholder="Анонимный комментарий…" maxlength="1000" required></textarea>
      <button class="btn small" type="submit">{ICON_COMMENT} Отправить</button>
    </form>
  </div>
</div>"""


def build_nav(active) -> str:
    items = f'<a class="navbtn{" active" if active is None else ""}" href="/">{ICON_HOME} Все</a>'
    for key, name in SECTIONS:
        cls = "navbtn active" if active == key else "navbtn"
        items += f'<a class="{cls}" href="/s/{key}">{ICON_FOLDER} {esc(name)}</a>'
    return items


def layout(content: str, active=None) -> str:
    head = (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="theme-color" content="#2b5a95">'
        '<title>SLDCommunity — анонимный форум</title>'
        '<style>' + CSS + '</style></head><body>'
    )
    top = (
        '<div class="topbar"><div class="wrap topbar-inner">'
        f'<div class="logo">{ICON_LOGO} SLDCommunity</div>'
        '<div class="tagline">анонимно &bull; без регистрации &bull; без ников &bull; без id</div>'
        '</div></div>'
    )
    body = f'<div class="wrap"><div class="nav">{build_nav(active)}</div>{content}'
    foot = (
        '<div class="footer">SLDCommunity &copy; 2010&mdash;2026 &middot; '
        'всё хранится только в оперативной памяти и исчезнет при перезапуске</div>'
        '</div></body></html>'
    )
    return head + top + body + foot


def render_page(section_filter: str = "all", active=None) -> str:
    if section_filter == "all":
        items = posts
        back = "/"
    else:
        items = [p for p in posts if p["section"] == section_filter]
        back = f"/s/{section_filter}"

    items = sorted(items, key=lambda p: p["id"], reverse=True)

    # --- форма нового поста ---
    options = ""
    for key, name in SECTIONS:
        sel = " selected" if active == key else ""
        options += f'<option value="{key}"{sel}>{esc(name)}</option>'

    form = f"""
<form class="newpost" action="/post" method="post" enctype="multipart/form-data">
  <div class="nphead">{ICON_PENCIL} Новый анонимный пост</div>
  <div class="npbody">
    <textarea name="text" placeholder="Что происходит? Никто не узнает, кто ты…" maxlength="5000" required></textarea>
    <div class="nprow">
      <select name="section">{options}</select>
      <label class="filebtn">
        {ICON_IMAGE} Прикрепить фото
        <input type="file" name="image" accept="image/*" hidden
               onchange="document.getElementById('fname').textContent = this.files[0] ? this.files[0].name : '';">
      </label>
      <span id="fname" class="fname"></span>
      <button class="btn" type="submit">{ICON_PENCIL} Опубликовать</button>
    </div>
  </div>
</form>"""

    # --- список постов ---
    if items:
        posts_html = "".join(render_post(p, back) for p in items)
    else:
        posts_html = '<div class="empty">Здесь пока пусто. Напиши первый анонимный пост!</div>'

    return layout(form + posts_html, active=active)


# ---------------------------------------------------------------------------
#  Маршруты
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(render_page("all", None))


@app.get("/s/{section}", response_class=HTMLResponse)
def section_page(section: str):
    if section not in SECTION_MAP:
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(render_page(section, section))


@app.post("/post")
async def create_post(
    text: str = Form(...),
    section: str = Form("general"),
    image: UploadFile | None = File(None),
):
    global next_id

    text = (text or "").strip()
    if not text:
        return RedirectResponse("/", status_code=303)

    if section not in SECTION_MAP:
        section = "general"

    img_data = None
    if image is not None and image.filename:
        img_data = await compress_image(image)

    posts.append({
        "id": next_id,
        "section": section,
        "text": text[:5000],
        "image": img_data,
        "time": time.time(),
        "comments": [],
    })
    next_id += 1

    return RedirectResponse(f"/s/{section}", status_code=303)


@app.post("/comment/{post_id}")
async def add_comment(post_id: int, text: str = Form(...), back: str = Form("/")):
    text = (text or "").strip()
    if text:
        for p in posts:
            if p["id"] == post_id:
                p["comments"].append({"text": text[:1000], "time": time.time()})
                break

    if not back.startswith("/"):
        back = "/"
    return RedirectResponse(f"{back}#p{post_id}", status_code=303)


@app.get("/stats/foned", response_class=PlainTextResponse)
def stats_foned():
    """Статистика без фона — чистый текст."""
    total_posts, total_comments = get_stats()
    return (
        "SLDCommunity — статистика\n"
        "=========================\n"
        f"Всего постов:        {total_posts}\n"
        f"Всего комментариев:  {total_comments}\n"
    )


# ---------------------------------------------------------------------------
#  Сжатие фото
# ---------------------------------------------------------------------------
async def compress_image(file: UploadFile) -> str | None:
    """Ужимает картинку до минимального размера и отдаёт data-URL."""
    try:
        raw = await file.read()
        if not raw:
            return None

        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")

        # 1) уменьшаем до 420px по длинной стороне
        img.thumbnail((420, 420), RESAMPLE)

        # 2) подбираем качество так, чтобы влезть в ~60 КБ
        quality = 55
        data = b""
        while quality >= 20:
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
            data = buf.getvalue()
            if len(data) <= 60_000:
                break
            quality -= 10

        return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    except Exception:
        return None


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
