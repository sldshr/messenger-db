# SLDCommunity — анонимный форум в стиле 2010
# Запуск:  python main.py
# Зависимости:  pip install fastapi uvicorn pillow python-multipart

import asyncio
import base64
import html
import io
import time

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from PIL import Image

try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE = Image.LANCZOS


app = FastAPI(title="SLDCommunity")

# ---------------------------------------------------------------------------
#  ДАННЫЕ — только в оперативке
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
#  РЕАЛТАЙМ: SSE-событие
# ---------------------------------------------------------------------------
stats_event = asyncio.Event()

def notify_change() -> None:
    stats_event.set()


# ---------------------------------------------------------------------------
#  SVG-иконки
# ---------------------------------------------------------------------------
def _svg(paths: str, size: int = 14) -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{paths}</svg>'
    )


ICON_PENCIL  = _svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>')
ICON_COMMENT = _svg('<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>')
ICON_IMAGE   = _svg('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>')
ICON_FOLDER  = _svg('<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>')
ICON_HOME    = _svg('<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>')
ICON_CLOCK   = _svg('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>', 12)
ICON_LOGO    = _svg('<circle cx="12" cy="12" r="10"/><path d="M12 8v8M8 12h8"/>', 22)
ICON_PLUS    = _svg('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>', 20)
ICON_X       = _svg('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>', 14)
ICON_CHEVRON = _svg('<polyline points="6 9 12 15 18 9"/>', 16)
ICON_CLIP    = _svg('<rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>', 12)
ICON_SEND    = _svg('<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>', 12)

SECTION_ICONS = {
    "general": _svg('<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>'),
    "tech":    _svg('<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/>'),
    "humor":   _svg('<circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" y1="9" x2="9.01" y2="9"/><line x1="15" y1="9" x2="15.01" y2="9"/>'),
    "life":    _svg('<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>'),
    "games":   _svg('<line x1="6" y1="11" x2="10" y2="11"/><line x1="8" y1="9" x2="8" y2="13"/><line x1="15" y1="12" x2="15.01" y2="12"/><line x1="18" y1="10" x2="18.01" y2="10"/><path d="M17.32 5H6.68a4 4 0 0 0-3.978 3.59c-.006.052-.01.101-.017.152C2.604 9.416 2 14.456 2 16a3 3 0 0 0 3 3c1 0 1.5-.5 2-1l1.414-1.414A2 2 0 0 1 9.828 16h4.344a2 2 0 0 1 1.414.586L17 18c.5.5 1 1 2 1a3 3 0 0 0 3-3c0-1.545-.604-6.584-.685-7.258-.007-.05-.011-.1-.017-.151A4 4 0 0 0 17.32 5z"/>'),
    "music":   _svg('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>'),
    "art":     _svg('<circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/>'),
    "help":    _svg('<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),
}


# ---------------------------------------------------------------------------
#  Утилиты
# ---------------------------------------------------------------------------
def esc(s) -> str:
    return html.escape(s or "")

def nl2br(s: str) -> str:
    return esc(s).replace("\n", "<br>")

def time_ago(ts: float) -> str:
    d = int(time.time() - ts)
    if d < 60:    return "только что"
    if d < 3600:  return f"{d // 60} мин."
    if d < 86400: return f"{d // 3600} ч."
    return f"{d // 86400} дн."

def get_stats() -> tuple[int, int]:
    return len(posts), sum(len(p["comments"]) for p in posts)

def section_counts() -> dict:
    c = {k: 0 for k, _ in SECTIONS}
    for p in posts:
        c[p["section"]] = c.get(p["section"], 0) + 1
    return c


# ---------------------------------------------------------------------------
#  CSS
# ---------------------------------------------------------------------------
CSS = """
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body {
  font-family: Verdana, Tahoma, Geneva, Arial, sans-serif;
  font-size: 13px;
  line-height: 1.45;
  color: #1b2a3a;
  background: linear-gradient(#b3cbe4 0%, #dbe8f5 45%, #eaf2fa 100%) fixed;
  min-height: 100vh;
  padding-bottom: env(safe-area-inset-bottom);
}
a { color: #1c4f8f; text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 900px; margin: 0 auto; padding: 0 10px; }

/* --- sticky header --- */
.header { position: sticky; top: 0; z-index: 50; }
.topbar {
  background: linear-gradient(#5d92cd 0%, #3a6fae 45%, #2b5a95 55%, #1e4577 100%);
  border-bottom: 1px solid #163459;
  box-shadow: 0 2px 6px rgba(0,0,0,.3);
  color: #fff;
  padding: 8px 0;
}
.topbar-inner { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.logo {
  font-size: 20px; font-weight: bold; letter-spacing: .3px;
  text-shadow: 0 1px 0 #0d2440;
  display: flex; align-items: center; gap: 8px;
  color: #fff;
}
.logo:hover { text-decoration: none; }
.tagline { font-size: 11px; color: #cfe2f7; text-shadow: 0 1px 0 #0d2440; }
.stats-link {
  font-size: 11px; color: #cfe2f7; text-shadow: 0 1px 0 #0d2440;
  padding: 4px 8px; border: 1px solid rgba(255,255,255,.25); border-radius: 4px;
}
.stats-link:hover { background: rgba(255,255,255,.1); text-decoration: none; }

/* --- nav sections --- */
.nav {
  background: linear-gradient(#eef5fc, #d7e5f4);
  border-bottom: 1px solid #a9c1da;
  box-shadow: 0 1px 3px rgba(0,0,0,.1);
  padding: 6px 0;
}
.nav-scroll {
  display: flex; gap: 5px;
  overflow-x: auto;
  padding: 2px 10px;
  scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
}
.nav-scroll::-webkit-scrollbar { display: none; }
.navbtn {
  flex: 0 0 auto;
  display: inline-flex; align-items: center; gap: 5px;
  padding: 7px 12px;
  font-size: 12px;
  color: #1c3f66;
  background: linear-gradient(#ffffff, #e6eefa 48%, #d8e5f4 52%, #c6d8ec);
  border: 1px solid #8fadcd;
  border-radius: 16px;
  box-shadow: inset 0 1px 0 #fff;
  white-space: nowrap;
  min-height: 34px;
}
.navbtn:hover { text-decoration: none; background: linear-gradient(#ffffff, #f0f6fd 48%, #e4eef9 52%, #d4e3f3); }
.navbtn.active {
  background: linear-gradient(#4b82c0, #2b5a95);
  color: #fff; border-color: #1e4577;
  text-shadow: 0 1px 0 #16345a;
}
.navbtn svg { width: 13px; height: 13px; flex-shrink: 0; }
.navcount {
  background: rgba(0,0,0,.12);
  border-radius: 8px;
  padding: 0 6px;
  font-size: 10px;
  min-width: 18px;
  text-align: center;
}
.navbtn.active .navcount { background: rgba(255,255,255,.25); }

/* --- compose --- */
.compose {
  background: #fff;
  border: 1px solid #93b0cf;
  border-radius: 6px;
  margin: 12px 0;
  box-shadow: 0 1px 4px rgba(0,0,0,.12);
  overflow: hidden;
}
.compose-head {
  display: flex; align-items: center; gap: 8px;
  background: linear-gradient(#f4f9fe, #dbe7f5);
  padding: 10px 12px;
  font-weight: bold; font-size: 13px;
  color: #1c3f66;
  cursor: pointer;
  user-select: none;
  list-style: none;
  min-height: 44px;
}
.compose-head::-webkit-details-marker { display: none; }
.compose-head .chev { margin-left: auto; transition: transform .15s; color: #6c8299; }
.compose[open] .compose-head .chev { transform: rotate(180deg); }
.compose[open] .compose-head { border-bottom: 1px solid #c3d6ea; }
.compose-body { padding: 10px 12px 12px; }

textarea, input[type=text], select {
  font-family: inherit;
  font-size: 16px;              /* не даёт iOS зумить */
  color: #1b2a3a;
  border: 1px solid #8aa8c8;
  border-radius: 4px;
  padding: 9px 10px;
  background: #fbfdff;
  box-shadow: inset 0 1px 2px rgba(0,0,0,.07);
  outline: none;
  width: 100%;
  -webkit-appearance: none;
  appearance: none;
}
textarea:focus, input[type=text]:focus, select:focus { border-color: #4b82c0; background: #fff; }
textarea { resize: vertical; min-height: 80px; line-height: 1.5; }

.chiprow {
  display: flex; gap: 5px; overflow-x: auto;
  margin: 8px -12px 0;
  padding: 2px 12px 6px;
  scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
}
.chiprow::-webkit-scrollbar { display: none; }
.chip {
  flex: 0 0 auto;
  display: inline-flex; align-items: center; gap: 4px;
  padding: 6px 11px;
  font-family: inherit;
  font-size: 12px;
  color: #1c3f66;
  background: #f0f5fb;
  border: 1px solid #b9cee0;
  border-radius: 14px;
  cursor: pointer;
  white-space: nowrap;
  min-height: 32px;
}
.chip svg { width: 12px; height: 12px; }
.chip.active {
  background: linear-gradient(#4b82c0, #2b5a95);
  color: #fff; border-color: #1e4577;
}

.attach-row {
  display: flex; align-items: center; gap: 8px;
  margin-top: 8px; flex-wrap: wrap;
}
.iconbtn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 8px 12px;
  font-family: inherit;
  font-size: 12px;
  color: #1c3f66;
  background: linear-gradient(#ffffff, #e6eefa 48%, #d8e5f4 52%, #c6d8ec);
  border: 1px solid #8fadcd;
  border-radius: 4px;
  box-shadow: inset 0 1px 0 #fff;
  cursor: pointer;
  min-height: 38px;
}
.iconbtn:hover { background: linear-gradient(#ffffff, #f0f6fd 48%, #e4eef9 52%, #d4e3f3); }
.iconbtn.ghost { background: transparent; border-color: #c3d6ea; color: #6c8299; }
.iconbtn.ghost:hover { background: #f0f5fb; }

.preview {
  position: relative;
  margin-top: 8px;
  border: 1px dashed #93b0cf;
  border-radius: 4px;
  padding: 6px;
  background: #f7fafd;
  display: inline-block;
  max-width: 100%;
}
.preview img { display: block; max-width: 140px; max-height: 140px; border-radius: 3px; }
.preview .rm {
  position: absolute; top: -8px; right: -8px;
  width: 24px; height: 24px; border-radius: 50%;
  background: #c14b3a; color: #fff; border: 2px solid #fff;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; box-shadow: 0 1px 3px rgba(0,0,0,.3);
}

.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 5px;
  padding: 10px 16px;
  font-family: inherit;
  font-size: 13px;
  font-weight: bold;
  color: #14314f;
  background: linear-gradient(#ffffff, #e6eefa 48%, #d8e5f4 52%, #c6d8ec);
  border: 1px solid #7f9dc0;
  border-radius: 5px;
  box-shadow: inset 0 1px 0 #fff, 0 1px 1px rgba(0,0,0,.08);
  cursor: pointer;
  min-height: 42px;
}
.btn:hover { background: linear-gradient(#ffffff, #f2f8fe 48%, #e6f0fb 52%, #d6e5f5); }
.btn:active { background: #c6d8ec; box-shadow: inset 0 1px 3px rgba(0,0,0,.2); }
.btn.primary {
  background: linear-gradient(#5d92cd, #2b5a95);
  color: #fff; border-color: #1e4577;
  text-shadow: 0 1px 0 #16345a;
}
.btn.primary:hover { background: linear-gradient(#6fa3dd, #356ba6); }
.btn.small { padding: 7px 12px; font-size: 12px; font-weight: normal; min-height: 36px; }

/* --- posts --- */
.post {
  background: #fff;
  border: 1px solid #9ab3cc;
  border-radius: 6px;
  margin-bottom: 12px;
  overflow: hidden;
  box-shadow: 0 1px 4px rgba(0,0,0,.1);
  scroll-margin-top: 110px;
}
.post:target { animation: flash 2.5s ease-out; }
@keyframes flash {
  0%   { box-shadow: 0 0 0 3px #ffcf4a, 0 1px 4px rgba(0,0,0,.1); }
  100% { box-shadow: 0 1px 4px rgba(0,0,0,.1); }
}
.phead {
  background: linear-gradient(#f4f9fe, #dde9f5);
  border-bottom: 1px solid #c3d6ea;
  padding: 8px 10px;
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; flex-wrap: wrap;
  font-size: 11px;
}
.badge {
  background: linear-gradient(#6fa3dd, #3d74b5);
  color: #fff;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 11px;
  border: 1px solid #2c5a92;
  text-shadow: 0 1px 0 #24486f;
  display: inline-flex; align-items: center; gap: 5px;
}
.badge svg { width: 12px; height: 12px; }
.ptime { color: #6c8299; display: inline-flex; align-items: center; gap: 4px; }
.ptext { padding: 12px 13px; word-wrap: break-word; overflow-wrap: anywhere; font-size: 14px; }
.postimg { padding: 0 13px 12px; }
.postimg img {
  max-width: 100%; height: auto; display: block;
  border: 1px solid #93b0cf; border-radius: 4px;
  background: #eef4fa;
  box-shadow: 0 1px 3px rgba(0,0,0,.12);
}

/* --- comments --- */
.comments {
  background: #f7fafd;
  border-top: 1px solid #dbe6f2;
  padding: 10px 12px 12px;
}
.ctitle {
  font-size: 11px; font-weight: bold; color: #48688b;
  display: flex; align-items: center; gap: 5px;
  margin-bottom: 8px; text-transform: uppercase; letter-spacing: .3px;
}
.comment {
  background: #fff;
  border: 1px solid #dbe6f2;
  border-left: 3px solid #7ba7d4;
  border-radius: 3px;
  padding: 8px 10px;
  margin-bottom: 6px;
  font-size: 13px;
}
.cmain { white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere; }
.cmeta { color: #8299b0; font-size: 11px; margin-top: 4px; display: flex; align-items: center; gap: 4px; }
.nocomments { color: #91a6bb; font-size: 11px; font-style: italic; margin-bottom: 8px; }
.cform { margin-top: 8px; }
.cform textarea { min-height: 44px; font-size: 14px; }
.cform .btn { margin-top: 6px; width: 100%; }

.empty {
  background: #fff;
  border: 1px dashed #9ab3cc;
  border-radius: 6px;
  padding: 40px 20px;
  text-align: center;
  color: #6c8299;
}

/* --- FAB --- */
.fab {
  position: fixed;
  right: 16px;
  bottom: calc(16px + env(safe-area-inset-bottom));
  width: 56px; height: 56px;
  border-radius: 50%;
  background: linear-gradient(#5d92cd, #2b5a95);
  color: #fff;
  border: 1px solid #1e4577;
  box-shadow: 0 4px 14px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.4);
  display: none;
  align-items: center; justify-content: center;
  cursor: pointer;
  z-index: 100;
}
.fab:active { transform: scale(.95); }

.footer {
  text-align: center;
  color: #6c8299;
  font-size: 11px;
  padding: 20px 0 26px;
  text-shadow: 0 1px 0 #fff;
}
.footer a { color: #5d7a99; }

/* --- мобилки --- */
@media (max-width: 640px) {
  body { font-size: 13px; }
  .wrap { padding: 0 8px; }
  .logo { font-size: 17px; }
  .logo svg { width: 18px; height: 18px; }
  .tagline { display: none; }
  .navbtn { padding: 6px 10px; font-size: 11px; min-height: 32px; }
  .ptext { padding: 10px 11px; font-size: 14px; }
  .postimg { padding: 0 11px 10px; }
  .comments { padding: 9px 11px 10px; }
  .fab { display: flex; }
  .compose-body { padding: 10px; }
  .compose-head { padding: 10px 12px; }
  .post { scroll-margin-top: 100px; }
  .cform textarea { font-size: 15px; }
  .footer { padding-bottom: 84px; }
}

@media (min-width: 641px) {
  .compose-head { cursor: default; }
  .compose-head .chev { display: none; }
  textarea, input[type=text], select { font-size: 13px; }
  .cform .btn { width: auto; }
}
"""


# ---------------------------------------------------------------------------
#  Рендер
# ---------------------------------------------------------------------------
def render_post(p: dict, back: str) -> str:
    sec_icon = SECTION_ICONS.get(p["section"], ICON_FOLDER)

    img_html = ""
    if p.get("image"):
        img_html = f'<div class="postimg"><img src="{p["image"]}" alt="фото" loading="lazy"></div>'

    if p["comments"]:
        c_html = "".join(
            '<div class="comment">'
            f'<div class="cmain">{nl2br(c["text"])}</div>'
            f'<div class="cmeta">{ICON_CLOCK} {time_ago(c["time"])}</div>'
            '</div>'
            for c in p["comments"]
        )
    else:
        c_html = '<div class="nocomments">Комментариев пока нет. Будь первым анонимом.</div>'

    return f"""
<div class="post" id="p{p['id']}">
  <div class="phead">
    <span class="badge">{sec_icon} {esc(SECTION_MAP.get(p['section'], p['section']))}</span>
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
      <button class="btn small" type="submit">{ICON_SEND} Отправить</button>
    </form>
  </div>
</div>"""


def build_nav(active, counts: dict) -> str:
    total = sum(counts.values())
    items = (
        f'<a class="navbtn{" active" if active is None else ""}" href="/">'
        f'{ICON_HOME} Все <span class="navcount">{total}</span></a>'
    )
    for key, name in SECTIONS:
        cls = "navbtn active" if active == key else "navbtn"
        n = counts.get(key, 0)
        icon = SECTION_ICONS.get(key, ICON_FOLDER)
        items += (
            f'<a class="{cls}" href="/s/{key}">'
            f'{icon} {esc(name)} <span class="navcount">{n}</span></a>'
        )
    return f'<div class="nav"><div class="nav-scroll">{items}</div></div>'


# -- JS для мобильного UX + вставки из буфера --
JS = r"""
(function () {
  // 1) На мобильных compose по умолчанию свёрнут, на десктопе — раскрыт
  var compose = document.getElementById('compose');
  var isMobile = window.matchMedia('(max-width: 640px)').matches;
  if (compose && isMobile) compose.removeAttribute('open');

  // 2) FAB — открыть compose, скролл к нему, фокус в textarea
  var fab = document.getElementById('fab');
  var composeText = document.getElementById('composeText');
  if (fab && compose && composeText) {
    fab.addEventListener('click', function () {
      compose.setAttribute('open', '');
      compose.scrollIntoView({behavior: 'smooth', block: 'start'});
      setTimeout(function () { composeText.focus(); }, 280);
    });
  }

  // 3) Выбор раздела через чипы
  var sectionInput = document.getElementById('sectionInput');
  window.pickSection = function (key) {
    if (sectionInput) sectionInput.value = key;
    document.querySelectorAll('.chip[data-section]').forEach(function (c) {
      c.classList.toggle('active', c.getAttribute('data-section') === key);
    });
  };

  // 4) Прикрепление фото: файл, вставка из буфера, drag&drop
  var attachedImage = null;
  var prevWrap = document.getElementById('previewWrap');
  var prevImg  = document.getElementById('previewImg');
  var fileIn   = document.getElementById('fileInput');
  var fname    = document.getElementById('fname');

  function showPreview(file) {
    attachedImage = file;
    var r = new FileReader();
    r.onload = function (e) {
      if (prevImg) prevImg.src = e.target.result;
      if (prevWrap) prevWrap.hidden = false;
    };
    r.readAsDataURL(file);
    if (fname) fname.textContent = file.name || 'вставлено из буфера';
  }

  window.clearImage = function () {
    attachedImage = null;
    if (prevImg) prevImg.src = '';
    if (prevWrap) prevWrap.hidden = true;
    if (fileIn) fileIn.value = '';
    if (fname) fname.textContent = '';
  };

  if (fileIn) {
    fileIn.addEventListener('change', function () {
      var f = fileIn.files && fileIn.files[0];
      if (f) showPreview(f);
    });
  }

  // ---- ВСТАВКА ИЗ БУФЕРА ОБМЕНА ----
  if (composeText) {
    composeText.addEventListener('paste', function (e) {
      var items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        if (it.kind === 'file' && it.type && it.type.indexOf('image/') === 0) {
          var f = it.getAsFile();
          if (f) {
            showPreview(f);
            e.preventDefault();
            return;
          }
        }
      }
    });
  }
  // вставка, когда фокус где-то в форме
  var composeForm = document.getElementById('composeForm');
  if (composeForm) {
    composeForm.addEventListener('paste', function (e) {
      if (e.target === composeText) return;
      var items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        if (it.kind === 'file' && it.type && it.type.indexOf('image/') === 0) {
          var f = it.getAsFile();
          if (f) { showPreview(f); e.preventDefault(); return; }
        }
      }
    });
    // drag&drop
    composeForm.addEventListener('dragover', function (e) { e.preventDefault(); });
    composeForm.addEventListener('drop', function (e) {
      e.preventDefault();
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f && f.type && f.type.indexOf('image/') === 0) showPreview(f);
    });
  }

  // 5) Отправка формы через fetch, чтобы подсунуть вставленную картинку
  if (composeForm) {
    composeForm.addEventListener('submit', function (e) {
      if (!attachedImage) return; // обычная отправка
      e.preventDefault();
      var fd = new FormData(composeForm);
      fd.delete('image');
      fd.set('image', attachedImage, attachedImage.name || 'pasted.jpg');
      var btn = composeForm.querySelector('button[type=submit]');
      if (btn) { btn.disabled = true; btn.textContent = 'Отправка…'; }
      fetch(composeForm.action, {method: 'POST', body: fd, redirect: 'follow'})
        .then(function (r) { window.location.href = r.url || '/'; })
        .catch(function (err) {
          alert('Не удалось отправить: ' + err.message);
          if (btn) { btn.disabled = false; btn.textContent = 'Опубликовать'; }
        });
    });
  }

  // 6) Подсказка "Ctrl+V" при фокусе в textarea
  if (composeText) {
    composeText.addEventListener('focus', function () {
      if (fname && !fname.textContent && !attachedImage) {
        fname.textContent = '';
      }
    });
  }
})();
"""


def layout(content: str, active=None) -> str:
    counts = section_counts()
    head = (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        '<meta name="theme-color" content="#2b5a95">'
        '<title>SLDCommunity — анонимный форум</title>'
        '<style>' + CSS + '</style></head><body>'
    )
    header = (
        '<div class="header">'
        '<div class="topbar"><div class="wrap topbar-inner">'
        f'<a class="logo" href="/">{ICON_LOGO} SLDCommunity</a>'
        '<div class="tagline">анонимно &bull; без регистрации</div>'
        '<a class="stats-link" href="/stats/foned" target="_blank">статистика</a>'
        '</div></div>'
        + build_nav(active, counts) +
        '</div>'
    )
    body = f'<div class="wrap">{content}'
    fab = f'<button class="fab" id="fab" title="Написать пост" aria-label="Написать пост">{ICON_PLUS}</button>'
    foot = (
        '<div class="footer">SLDCommunity &copy; 2010&mdash;2026 &middot; '
        'данные хранятся в оперативке и исчезнут при перезапуске</div>'
        '</div>'
        f'<script>{JS}</script>'
        '</body></html>'
    )
    return head + header + body + foot + fab


def render_compose(active_section: str) -> str:
    chips = ""
    for key, name in SECTIONS:
        cls = "chip active" if key == active_section else "chip"
        icon = SECTION_ICONS.get(key, ICON_FOLDER)
        chips += (
            f'<button type="button" class="{cls}" data-section="{key}" '
            f'onclick="pickSection(\'{key}\')">{icon} {esc(name)}</button>'
        )

    return f"""
<details class="compose" id="compose" open>
  <summary class="compose-head">
    {ICON_PENCIL} Новый анонимный пост
    <span class="chev">{ICON_CHEVRON}</span>
  </summary>
  <div class="compose-body">
    <form id="composeForm" action="/post" method="post" enctype="multipart/form-data">
      <input type="hidden" name="section" id="sectionInput" value="{active_section}">
      <textarea id="composeText" name="text"
                placeholder="Что происходит? Никто не узнает, кто ты… Вставь фото через Ctrl+V"
                maxlength="5000" required></textarea>

      <div class="chiprow">{chips}</div>

      <div class="attach-row">
        <label class="iconbtn" for="fileInput">{ICON_IMAGE} Фото</label>
        <input type="file" id="fileInput" name="image" accept="image/*" hidden>
        <span class="iconbtn ghost" style="cursor:default">{ICON_CLIP} Ctrl+V — вставить</span>
        <span id="fname" class="fname" style="font-size:11px;color:#6c8299"></span>
      </div>

      <div class="preview" id="previewWrap" hidden>
        <img id="previewImg" alt="превью">
        <button type="button" class="rm" onclick="clearImage()" aria-label="Убрать фото">{ICON_X}</button>
      </div>

      <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="btn primary" type="submit">{ICON_PENCIL} Опубликовать</button>
      </div>
    </form>
  </div>
</details>"""


def render_page(section_filter: str = "all", active=None) -> str:
    if section_filter == "all":
        items = posts
        back = "/"
    else:
        items = [p for p in posts if p["section"] == section_filter]
        back = f"/s/{section_filter}"

    items = sorted(items, key=lambda p: p["id"], reverse=True)

    compose = render_compose(active if active else "general")

    if items:
        posts_html = "".join(render_post(p, back) for p in items)
    else:
        posts_html = '<div class="empty">Здесь пока пусто. Напиши первый анонимный пост!</div>'

    return layout(compose + posts_html, active=active)


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

    pid = next_id
    posts.append({
        "id": pid,
        "section": section,
        "text": text[:5000],
        "image": img_data,
        "time": time.time(),
        "comments": [],
    })
    next_id += 1

    notify_change()
    return RedirectResponse(f"/s/{section}#p{pid}", status_code=303)


@app.post("/comment/{post_id}")
async def add_comment(post_id: int, text: str = Form(...), back: str = Form("/")):
    text = (text or "").strip()
    if text:
        for p in posts:
            if p["id"] == post_id:
                p["comments"].append({"text": text[:1000], "time": time.time()})
                break

    notify_change()
    if not back.startswith("/"):
        back = "/"
    return RedirectResponse(f"{back}#p{post_id}", status_code=303)


# ---------------------------------------------------------------------------
#  СТАТИСТИКА
# ---------------------------------------------------------------------------
def _fmt_stats(p: int, c: int) -> str:
    return (
        "SLDCommunity — статистика\n"
        "=========================\n"
        f"Всего постов:        {p}\n"
        f"Всего комментариев:  {c}\n"
    )


STATS_PAGE = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Статистика — SLDCommunity</title>
<style>
  html, body {
    margin: 0; padding: 22px;
    background: transparent;
    color: #1b2a3a;
    font-family: "Courier New", Consolas, "Liberation Mono", monospace;
    font-size: 14px; line-height: 1.55;
  }
  pre { margin: 0; white-space: pre-wrap; font: inherit; }
  .live { display: inline-flex; align-items: center; gap: 6px; margin-top: 10px; font-size: 12px; color: #5d7a99; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #3aa655; box-shadow: 0 0 6px #3aa655; animation: pulse 1.6s ease-in-out infinite; }
  .dot.off { background: #c14b3a; box-shadow: 0 0 6px #c14b3a; animation: none; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
</style></head>
<body>
<pre id="out">__INITIAL__</pre>
<div class="live"><span class="dot" id="dot"></span><span id="label">live</span></div>
<script>
(function () {
  var out = document.getElementById('out');
  var dot = document.getElementById('dot');
  var lbl = document.getElementById('label');
  function render(p, c) {
    return 'SLDCommunity — статистика\\n'
         + '=========================\\n'
         + 'Всего постов:        ' + p + '\\n'
         + 'Всего комментариев:  ' + c + '\\n';
  }
  if (!window.EventSource) { dot.classList.add('off'); lbl.textContent = 'SSE не поддерживается'; return; }
  var es = new EventSource('/stats/foned/stream');
  es.onopen    = function () { dot.classList.remove('off'); lbl.textContent = 'live'; };
  es.onerror   = function () { dot.classList.add('off');    lbl.textContent = 'reconnecting…'; };
  es.onmessage = function (e) {
    var parts = e.data.split(' ');
    out.textContent = render(parts[0], parts[1]);
    document.title = 'Постов: ' + parts[0] + ' · Комментов: ' + parts[1];
  };
})();
</script>
</body></html>"""


@app.get("/stats/foned", response_class=HTMLResponse)
def stats_foned():
    p, c = get_stats()
    page = STATS_PAGE.replace("__INITIAL__", esc(_fmt_stats(p, c)))
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/stats/foned.txt", response_class=PlainTextResponse)
def stats_foned_txt():
    p, c = get_stats()
    return PlainTextResponse(_fmt_stats(p, c), headers={"Cache-Control": "no-store"})


@app.get("/stats/foned/stream")
async def stats_foned_stream():
    async def gen():
        last = None
        last_beat = time.time()
        try:
            p, c = get_stats()
            last = (p, c)
            yield f"data: {p} {c}\n\n"
            while True:
                try:
                    await asyncio.wait_for(stats_event.wait(), timeout=5.0)
                    stats_event.clear()
                except asyncio.TimeoutError:
                    pass
                p, c = get_stats()
                if (p, c) != last:
                    last = (p, c)
                    yield f"data: {p} {c}\n\n"
                    last_beat = time.time()
                elif time.time() - last_beat >= 15:
                    yield ": ping\n\n"
                    last_beat = time.time()
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
#  Сжатие фото
# ---------------------------------------------------------------------------
async def compress_image(file: UploadFile) -> str | None:
    try:
        raw = await file.read()
        if not raw:
            return None
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((420, 420), RESAMPLE)
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
