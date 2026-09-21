# main.py — litodon
# Запуск:
#   pip install fastapi uvicorn python-multipart
#   python main.py

import secrets, time, re
from datetime import datetime
from html import escape
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

app = FastAPI(title="litodon")

# ============================================================
#   ХРАНИЛИЩЕ (В ОПЕРАТИВКЕ)
# ============================================================

sessions: dict = {}    # sid -> {"id": int}
posts: list = []
_counter = {"post": 0, "comment": 0, "anon": 0}
MAX_POST = 4000            # лимит «осмысленного» текста
MAX_RAW  = 8_000_000       # жёсткий потолок сырой длины (≈6 МБ картинки в base64)


@app.middleware("http")
async def anon_middleware(request: Request, call_next):
    token = request.cookies.get("sid")
    created = False
    if not token or token not in sessions:
        token = secrets.token_hex(16)
        _counter["anon"] += 1
        sessions[token] = {"id": _counter["anon"]}
        created = True
    request.state.anon = sessions[token]
    request.state.token = token
    response = await call_next(request)
    if created:
        response.set_cookie("sid", token, httponly=True,
                            max_age=60 * 60 * 24 * 365, samesite="lax")
    return response


def fmt_time(ts: float) -> str:
    d = int(time.time() - ts)
    if d < 60:      return f"{d} сек. назад"
    if d < 3600:    return f"{d // 60} мин. назад"
    if d < 86400:   return f"{d // 3600} ч. назад"
    if d < 604800:  return f"{d // 86400} дн. назад"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def safe_redirect(request: Request) -> str:
    ref = request.headers.get("referer", "")
    if ref:
        path = urlparse(ref).path or "/"
        if path.startswith("/"):
            return path
    return "/"


# ============================================================
#   BASE64 КАРТИНКИ
# ============================================================

# полный payload data:image/...;base64,....
B64_PAYLOAD_RE = re.compile(r'data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+')
# строгая проверка URL (без пробелов)
B64_URL_RE = re.compile(r'^data:image/(png|jpe?g|gif|webp|bmp);base64,[A-Za-z0-9+/=]+$', re.I)


def effective_length(text: str) -> int:
    """Длина текста без учёта base64-картинок."""
    return len(B64_PAYLOAD_RE.sub('data:image/...;base64,...', text))


def clean_data_url(u: str) -> str:
    return re.sub(r'\s+', '', u)


# ============================================================
#   SVG ИКОНКИ
# ============================================================

ICON_PATHS = {
    "home":      '<path d="M8 1.5 15 7.5h-2.2V15H9.5v-4.2h-3V15H3.2V7.5H1z"/>',
    "plus":      '<path d="M7 2h2v5h5v2H9v5H7V9H2V7h5z"/>',
    "up":        '<path d="M8 3 13 9H9.5v4h-3V9H3z"/>',
    "down":      '<path d="M8 13 3 7h3.5V3h3v4H13z"/>',
    "comment":   '<path d="M2 3h12v9H8l-3.2 3v-3H2z"/>',
    "edit":      '<path d="m11 2 3 3-9 9H2v-3z"/>',
    "trash":     '<path d="M6 1h4v1.5h4V4H2V2.5h4z"/><path d="M3.5 5.5h9V15h-9z"/>',
    "arrow-left":'<path d="M7 3 2 8l5 5V9.5h7v-3H7z"/>',
    "send":      '<path d="M1.5 8 14.5 1.5 8 14.5l-1.8-5z"/>',
    "link":      '<path d="M6.5 9.5 9.5 6.5M6 4.5 7.5 3a3 3 0 0 1 4.2 0l1.3 1.3a3 3 0 0 1 0 4.2L11.5 10M10 11.5 8.5 13a3 3 0 0 1-4.2 0L3 11.7a3 3 0 0 1 0-4.2L4.5 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
    "image":     '<rect x="2" y="3" width="12" height="10" rx="1" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="5.5" cy="6.5" r="1.2"/><path d="m2.5 12 3.5-3.5 3 3 2-2 3 3" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    "upload":    '<path d="M8 11V3M4.5 6.5 8 3l3.5 3.5M2 13h12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    "code":      '<path d="M5.5 5 2 8l3.5 3M10.5 5 14 8l-3.5 3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    "codeblock": '<rect x="1.5" y="2.5" width="13" height="11" rx="1" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M5 6 3.8 8 5 10M11 6l1.2 2L11 10" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>',
    "bold":      '<text x="8" y="12" text-anchor="middle" font-size="11" font-weight="700" font-family="Arial,sans-serif" fill="currentColor">B</text>',
    "italic":    '<text x="8" y="12" text-anchor="middle" font-size="11" font-style="italic" font-family="Georgia,serif" fill="currentColor">I</text>',
    "strike":    '<text x="8" y="12" text-anchor="middle" font-size="11" font-family="Arial,sans-serif" fill="currentColor">S</text><path d="M3 8.2h10" stroke="currentColor" stroke-width="1.3"/>',
    "h1":        '<text x="8" y="12" text-anchor="middle" font-size="9" font-weight="700" font-family="Arial,sans-serif" fill="currentColor">H1</text>',
    "h2":        '<text x="8" y="12" text-anchor="middle" font-size="9" font-weight="700" font-family="Arial,sans-serif" fill="currentColor">H2</text>',
    "h3":        '<text x="8" y="12" text-anchor="middle" font-size="9" font-weight="700" font-family="Arial,sans-serif" fill="currentColor">H3</text>',
    "ul":        '<circle cx="3" cy="5" r="1"/><circle cx="3" cy="8" r="1"/><circle cx="3" cy="11" r="1"/><path d="M6 5h8M6 8h8M6 11h8" stroke="currentColor" stroke-width="1.3" fill="none"/>',
    "ol":        '<text x="3" y="6.8" text-anchor="middle" font-size="6" font-family="Arial" fill="currentColor">1</text><text x="3" y="10.2" text-anchor="middle" font-size="6" font-family="Arial" fill="currentColor">2</text><text x="3" y="13.6" text-anchor="middle" font-size="6" font-family="Arial" fill="currentColor">3</text><path d="M6 5h8M6 8h8M6 11h8" stroke="currentColor" stroke-width="1.3" fill="none"/>',
    "quote":     '<path d="M3 5v4h2.5L4 12M9 5v4h2.5L10 12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>',
    "hr":        '<path d="M2 8h12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="4" cy="4" r="0.7"/><circle cx="12" cy="12" r="0.7"/>',
    "table":     '<rect x="1.5" y="2.5" width="13" height="11" rx="1" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M1.5 6h13M1.5 9.5h13M5.5 2.5v11M10.5 2.5v11" stroke="currentColor" stroke-width="1.2" fill="none"/>',
    "task":      '<rect x="2" y="2" width="12" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="m5 8 2 2 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    "highlight": '<path d="M3 13h10M4 11 9 6l3 3-5 5H4z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>',
    "spoiler":   '<path d="M1.5 8s2.5-4 6.5-4 6.5 4 6.5 4-2.5 4-6.5 4S1.5 8 1.5 8z" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="8" cy="8" r="2" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="m2 14 12-12" stroke="currentColor" stroke-width="1.4"/>',
    "sup":       '<text x="8" y="11" text-anchor="middle" font-size="9" font-family="Arial" fill="currentColor">x</text><text x="12" y="7" text-anchor="middle" font-size="6" font-family="Arial" fill="currentColor">2</text>',
    "emoji":     '<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="6" cy="6.5" r="0.8" fill="currentColor"/><circle cx="10" cy="6.5" r="0.8" fill="currentColor"/><path d="M5.5 9.5c.7 1 1.5 1.5 2.5 1.5s1.8-.5 2.5-1.5" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>',
}


def ic(name: str, size: int = 14) -> str:
    p = ICON_PATHS.get(name, "")
    return (f'<svg class="ic" viewBox="0 0 16 16" width="{size}" height="{size}" '
            f'fill="currentColor" aria-hidden="true">{p}</svg>')


# ============================================================
#   MARKDOWN
# ============================================================

EMOJI = {
    "smile": "😊", "grin": "😁", "joy": "😂", "laugh": "😆", "wink": "😉",
    "heart": "❤️", "thumbsup": "👍", "+1": "👍", "thumbsdown": "👎", "-1": "👎",
    "fire": "🔥", "star": "⭐", "rocket": "🚀", "check": "✅", "x": "❌",
    "warning": "⚠️", "info": "ℹ️", "question": "❓", "bulb": "💡", "idea": "💡",
    "cry": "😢", "angry": "😠", "cool": "😎", "wave": "👋", "clap": "👏",
    "ok": "👌", "pray": "🙏", "eyes": "👀", "cat": "🐱", "dog": "🐶",
    "sun": "☀️", "moon": "🌙", "zap": "⚡", "sparkles": "✨", "tada": "🎉",
    "coffee": "☕", "pizza": "🍕", "beer": "🍺", "gift": "🎁", "lock": "🔒",
    "key": "🔑", "book": "📖", "pencil": "✏️", "memo": "📝", "chart": "📊",
    "bug": "🐛", "ghost": "👻", "skull": "💀", "alien": "👽", "robot": "🤖",
}


def safe_url(u: str) -> str:
    u = u.strip()
    # разрешаем data:image/* (только растровые, без svg — там может быть JS)
    if B64_URL_RE.match(u):
        return u
    if re.match(r'^(javascript|data|vbscript):', u, re.I):
        return "#"
    return u


def md_inline(text: str) -> str:
    text = escape(text)
    stash = []

    def put(html: str) -> str:
        stash.append(html)
        return f"\x00{len(stash) - 1}\x00"

    # 1) код
    text = re.sub(r'`([^`\n]+)`', lambda m: put(f'<code>{m.group(1)}</code>'), text)

    # 2) base64-изображения — обрабатываем ДО обычного image, т.к. payload длинный и может содержать переносы
    text = re.sub(
        r'!\[([^\]]*)\]\(\s*(data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+)\s*\)',
        lambda m: put(f'<img src="{safe_url(clean_data_url(m.group(2)))}" alt="{m.group(1)}" loading="lazy">'),
        text)

    # 3) обычные изображения и ссылки
    text = re.sub(r'!\[([^\]]*)\]\(([^)\s]+)\)',
                  lambda m: put(f'<img src="{safe_url(m.group(2))}" alt="{m.group(1)}" loading="lazy">'),
                  text)
    text = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)',
                  lambda m: put(f'<a href="{safe_url(m.group(2))}" target="_blank" rel="noopener nofollow">{m.group(1)}</a>'),
                  text)
    text = re.sub(r'(?<![\w"\'=/>])(https?://[^\s<>"\'()]+)',
                  lambda m: put(f'<a href="{safe_url(m.group(1))}" target="_blank" rel="noopener nofollow">{m.group(1)}</a>'),
                  text)

    # 4) emoji
    text = re.sub(r':([a-z0-9_+\-]+):',
                  lambda m: EMOJI.get(m.group(1), m.group(0)), text)

    # 5) оформление
    text = re.sub(r'==(.+?)==', r'<mark>\1</mark>', text)
    text = re.sub(r'\|\|(.+?)\|\|', r'<span class="spoiler">\1</span>', text)
    text = re.sub(r'\^([^\s^][^^\n]*?)\^', r'<sup>\1</sup>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\w)__(.+?)__(?!\w)', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)', r'<em>\1</em>', text)
    text = re.sub(r'(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)', r'<em>\1</em>', text)
    text = re.sub(r'~~(.+?)~~', r'<del>\1</del>', text)

    text = re.sub(r'\x00(\d+)\x00', lambda m: stash[int(m.group(1))], text)
    return text


HR_RE = re.compile(r'^((-\s*){3,}|(\*\s*){3,}|(_\s*){3,})$')
TABLE_SEP_RE = re.compile(r'^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$')


def _parse_table(lines, i, n, out, close_list):
    header_line = lines[i].strip()
    headers = [c.strip() for c in header_line.strip("|").split("|")]
    i += 2
    rows = []
    while i < n and lines[i].strip() and "|" in lines[i]:
        rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
        i += 1
    close_list()
    html = ["<table><thead><tr>"]
    for h in headers:
        html.append(f"<th>{md_inline(h)}</th>")
    html.append("</tr></thead><tbody>")
    for row in rows:
        html.append("<tr>")
        for j in range(len(headers)):
            cell = row[j] if j < len(row) else ""
            html.append(f"<td>{md_inline(cell)}</td>")
        html.append("</tr>")
    html.append("</tbody></table>")
    out.append("".join(html))
    return i


def md_block(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    i, n = 0, len(lines)
    in_code = False
    code_buf, code_lang = [], ""
    stack = []

    def close_list():
        while stack:
            out.append(f"</{stack.pop()}>")

    def is_break(s: str) -> bool:
        if not s: return True
        if s.startswith("```") or s.startswith("#") or s.startswith(">"): return True
        if re.match(r'^[-*+]\s', s): return True
        if re.match(r'^\d+\.\s', s): return True
        if HR_RE.match(s): return True
        if "|" in s: return True
        return False

    while i < n:
        raw = lines[i]
        s = raw.strip()

        if s.startswith("```"):
            if not in_code:
                in_code = True
                code_lang = s[3:].strip()
                code_buf = []
            else:
                in_code = False
                close_list()
                cls = f' class="lang-{escape(code_lang)}"' if code_lang else ""
                out.append(f'<pre><code{cls}>{escape(chr(10).join(code_buf))}</code></pre>')
            i += 1
            continue

        if in_code:
            code_buf.append(raw)
            i += 1
            continue

        if not s:
            close_list()
            i += 1
            continue

        if HR_RE.match(s):
            close_list()
            out.append("<hr>")
            i += 1
            continue

        if "|" in s and i + 1 < n:
            nxt = lines[i + 1].strip()
            if TABLE_SEP_RE.match(nxt):
                i = _parse_table(lines, i, n, out, close_list)
                continue

        m = re.match(r'^(#{1,6})\s+(.+)$', s)
        if m:
            close_list()
            lvl = len(m.group(1))
            out.append(f'<h{lvl}>{md_inline(m.group(2))}</h{lvl}>')
            i += 1
            continue

        if s.startswith(">"):
            close_list()
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].lstrip())
                i += 1
            out.append("<blockquote>" + "<br>".join(md_inline(x) for x in buf) + "</blockquote>")
            continue

        m = re.match(r'^[-*+]\s+\[([ xX])\]\s+(.+)$', s)
        if m:
            if not stack or stack[-1] != "task":
                close_list(); stack.append("task"); out.append('<ul class="task-list">')
            checked = " checked" if m.group(1).lower() == "x" else ""
            out.append(f'<li><input type="checkbox" disabled{checked}><span>{md_inline(m.group(2))}</span></li>')
            i += 1
            continue

        m = re.match(r'^[-*+]\s+(.+)$', s)
        if m:
            if not stack or stack[-1] != "ul":
                close_list(); stack.append("ul"); out.append("<ul>")
            out.append(f'<li>{md_inline(m.group(1))}</li>')
            i += 1
            continue

        m = re.match(r'^\d+\.\s+(.+)$', s)
        if m:
            if not stack or stack[-1] != "ol":
                close_list(); stack.append("ol"); out.append("<ol>")
            out.append(f'<li>{md_inline(m.group(1))}</li>')
            i += 1
            continue

        close_list()
        para = [s]
        i += 1
        while i < n and lines[i].strip() and not is_break(lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + "<br>".join(md_inline(x) for x in para) + "</p>")

    if in_code:
        close_list()
        out.append(f'<pre><code>{escape(chr(10).join(code_buf))}</code></pre>')
    close_list()
    return "".join(out)


render_md = md_block


# ============================================================
#   CSS
# ============================================================

CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: #e3efe3;
  font-family: Verdana, Geneva, Tahoma, sans-serif;
  font-size: 13px;
  color: #17381a;
  line-height: 1.45;
}
a { color: #2e7d32; text-decoration: none; }
a:hover { color: #1b5e20; text-decoration: underline; }

* { scrollbar-width: thin; scrollbar-color: #a5cfa5 #eaf6ea; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: #eaf6ea; border-radius: 5px; }
::-webkit-scrollbar-thumb {
  background: #a5cfa5; border-radius: 5px;
  border: 2px solid #eaf6ea; background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover { background: #66bb6a; border: 2px solid #eaf6ea; background-clip: padding-box; }
::-webkit-scrollbar-corner { background: #eaf6ea; }
::-webkit-scrollbar-button { display: none; }

.ic { vertical-align: -2px; }

.header {
  background: linear-gradient(#4aa350, #2e7d32);
  border-bottom: 3px solid #1b5e20;
  box-shadow: 0 2px 6px rgba(0,0,0,0.22);
}
.header-inner {
  max-width: 1760px; margin: 0 auto; padding: 10px 18px;
  display: flex; align-items: center; justify-content: space-between;
}
.logo {
  font-size: 28px; font-weight: bold; color: #fff; text-decoration: none;
  letter-spacing: -1.5px;
  text-shadow: 1px 1px 0 #1b5e20, 2px 2px 4px rgba(0,0,0,0.35);
}
.logo:hover { text-decoration: none; color: #fff; }
.logo span {
  color: #c8e6c9; font-size: 11px; letter-spacing: 0;
  margin-left: 8px; font-weight: normal; text-shadow: none;
  vertical-align: middle;
}
.header-right { display: flex; align-items: center; gap: 8px; }
.header-btn {
  display: inline-flex; align-items: center; gap: 6px;
  background: rgba(255,255,255,0.18); color: #fff;
  border: 1px solid rgba(255,255,255,0.4);
  padding: 5px 12px; border-radius: 3px; font-size: 12px; font-weight: bold;
}
.header-btn:hover { background: rgba(255,255,255,0.32); color: #fff; text-decoration: none; }

.layout {
  width: 1080px; margin: 16px auto 30px;
  display: flex; align-items: flex-start; gap: 14px;
}
.layout.wide { width: min(1760px, 97vw); }
.sidebar { width: 220px; flex-shrink: 0; }
.content { flex: 1; min-width: 0; }

.side-card {
  background: #fff; border: 1px solid #a5cfa5; border-radius: 4px;
  margin-bottom: 10px; padding: 8px;
  box-shadow: 0 1px 2px rgba(27,94,32,0.10);
}
.side-nav { padding: 4px; }
.side-link {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 10px; border-radius: 3px;
  color: #1b5e20; font-weight: bold; font-size: 13px;
  text-decoration: none; cursor: pointer;
  width: 100%; border: none; background: none;
  font-family: inherit; text-align: left;
}
.side-link:hover { background: #eaf6ea; text-decoration: none; color: #1b5e20; }
.side-link.active { background: linear-gradient(#66bb6a, #43a047); color: #fff; }
.side-link.active .ic { color: #fff; }

.side-stats { font-size: 12px; color: #4b6b4b; }
.side-stat { padding: 3px 4px; display: flex; align-items: center; gap: 6px; }
.side-stat b { color: #1b5e20; }

.box {
  background: #fff; border: 1px solid #a5cfa5; border-radius: 4px;
  margin-bottom: 12px; box-shadow: 0 1px 2px rgba(27,94,32,0.12);
}
.empty { padding: 22px; text-align: center; color: #7a8f7a; font-size: 13px; }
.page-title { margin: 0 0 12px; font-size: 18px; color: #1b5e20; font-weight: bold; }
.back-link { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; margin-bottom: 8px; }

.post { display: flex; overflow: hidden; }
.votes {
  width: 58px; flex-shrink: 0; background: #f3faf3;
  border-right: 1px solid #d6ead6; padding: 10px 6px;
  display: flex; flex-direction: column; align-items: center; gap: 4px;
}
.vote-form { margin: 0; padding: 0; }
.vote-btn {
  width: 34px; height: 24px; padding: 0;
  background: #eaf6ea; color: #2e7d32;
  border: 1px solid #a5cfa5; border-radius: 3px;
  cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
  font-family: inherit; text-shadow: none;
}
.vote-btn:hover { background: #c8e6c9; }
.vote-btn.active-up { background: #43a047; color: #fff; border-color: #2e7d32; }
.vote-btn.active-down { background: #e53935; color: #fff; border-color: #c62828; }
.score { font-weight: bold; font-size: 15px; }
.score-pos { color: #2e7d32; }
.score-neg { color: #c62828; }
.score-zero { color: #7a8f7a; }

.post-main { flex: 1; padding: 10px 12px; min-width: 0; }
.post-head { margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
.time { color: #8aa38a; font-size: 11px; }
.edited { color: #a8c0a8; font-size: 11px; }
.post-text { font-size: 14px; word-wrap: break-word; overflow-wrap: break-word; }

.post-actions { margin-top: 8px; display: flex; gap: 4px; align-items: center; flex-wrap: wrap; }
.post-act {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; color: #6b8a6b; padding: 3px 7px;
  border-radius: 3px; background: none; border: none;
  cursor: pointer; font-family: inherit; text-decoration: none;
}
.post-act:hover { background: #eaf6ea; color: #1b5e20; text-decoration: none; }
.post-act-danger:hover { background: #fdecea; color: #c62828; }
.inline-form { margin: 0; padding: 0; display: inline; }

.comments { margin-top: 10px; border-top: 1px dashed #c8e6c9; padding-top: 6px; }
.comments-title {
  font-size: 11px; color: #6b8a6b; text-transform: uppercase;
  letter-spacing: 0.5px; font-weight: bold; margin-bottom: 4px;
  display: flex; align-items: center; gap: 5px;
}
.comment { padding: 6px 0; border-bottom: 1px dotted #e0f0e0; }
.comment:last-child { border-bottom: none; }
.c-time { color: #9cb89c; font-size: 11px; }
.c-text { font-size: 12px; margin-top: 2px; }

.cform { margin-top: 8px; display: flex; gap: 6px; }
.cform input[type=text] { flex: 1; }

input[type=text], textarea {
  border: 1px solid #a5cfa5; border-radius: 3px;
  padding: 6px 8px; font-family: inherit; font-size: 13px;
  background: #f7fdf7; color: #17381a; outline: none; width: 100%;
}
input:focus, textarea:focus { border-color: #4caf50; background: #fff; }

button, .btn-primary {
  background: linear-gradient(#66bb6a, #43a047);
  border: 1px solid #2e7d32; color: #fff;
  padding: 6px 14px; border-radius: 3px; cursor: pointer;
  font-family: inherit; font-size: 12px; font-weight: bold;
  text-shadow: 0 1px 0 rgba(0,0,0,0.2);
  display: inline-flex; align-items: center; gap: 5px;
}
button:hover, .btn-primary:hover { background: linear-gradient(#7cc87f, #4caf50); }
button:active { background: #2e7d32; }

.editor { overflow: hidden; }
.editor-toolbar {
  display: flex; align-items: center; gap: 2px;
  padding: 6px 8px; background: linear-gradient(#eaf6ea, #d9ecd9);
  border-bottom: 1px solid #a5cfa5; flex-wrap: wrap;
}
.tb {
  background: transparent; border: 1px solid transparent; color: #2e5233;
  padding: 5px 7px; border-radius: 3px; cursor: pointer;
  font-family: inherit; font-size: 12px; text-shadow: none;
  display: inline-flex; align-items: center; gap: 4px;
  min-width: 28px; justify-content: center;
}
.tb:hover { background: #fff; border-color: #a5cfa5; }
.tb.active { background: #43a047; color: #fff; border-color: #2e7d32; }
.tb-text { font-weight: bold; padding: 5px 10px; }
.tb-sep { width: 1px; height: 20px; background: #a5cfa5; margin: 0 4px; }
.tb-spacer { flex: 1; }

.editor-body {
  display: flex;
  height: min(780px, 80vh);
  min-height: 520px;
  align-items: stretch;
}
.editor-pane {
  flex: 1 1 50%; min-width: 0;
  display: flex; flex-direction: column; overflow: hidden;
}
.editor-pane textarea {
  flex: 1 1 auto; height: 100%; min-height: 0;
  border: none; border-radius: 0; background: #fff; resize: none;
  padding: 14px 16px;
  font-family: Consolas, Monaco, "Courier New", monospace;
  font-size: 13.5px; line-height: 1.55; overflow-y: auto;
}
.editor-pane textarea:focus { background: #fff; }
.editor-preview {
  border-left: 1px solid #d6ead6; background: #fafdfa;
  padding: 14px 18px; height: 100%; overflow-y: auto;
  font-family: Verdana, sans-serif; font-size: 13.5px;
  line-height: 1.55;
}
.editor-preview img,
.post-text img { max-width: 100%; height: auto; }

.editor-foot {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 10px; background: #f3faf3; border-top: 1px solid #a5cfa5;
  gap: 10px; flex-wrap: wrap;
}
.editor-hint { font-size: 11px; color: #6b8a6b; flex: 1; min-width: 200px; line-height: 1.8; }
.editor-hint code {
  background: #eaf6ea; border: 1px solid #d6ead6; padding: 0 4px;
  border-radius: 2px; font-size: 11px; color: #2e7d32;
}
.editor-actions { display: flex; align-items: center; gap: 12px; }
.counter { font-size: 11px; color: #7a8f7a; font-variant-numeric: tabular-nums; }
.counter.warn { color: #c62828; font-weight: bold; }
.counter .b64hint { color: #9cb89c; }

.md { line-height: 1.55; }
.md > *:first-child { margin-top: 0; }
.md > *:last-child { margin-bottom: 0; }
.md p { margin: 0 0 9px; }
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6 {
  margin: 14px 0 6px; color: #1b5e20; line-height: 1.25;
}
.md h1 { font-size: 20px; border-bottom: 1px solid #c8e6c9; padding-bottom: 4px; }
.md h2 { font-size: 17px; }
.md h3 { font-size: 15px; }
.md h4, .md h5, .md h6 { font-size: 13px; color: #2e5233; }
.md ul, .md ol { margin: 8px 0; padding-left: 24px; }
.md li { margin: 2px 0; }
.md blockquote {
  margin: 10px 0; padding: 8px 12px;
  border-left: 3px solid #66bb6a; background: #f3faf3; color: #2e5233;
  border-radius: 0 3px 3px 0;
}
.md code {
  background: #eef6ee; border: 1px solid #d6ead6;
  padding: 1px 5px; border-radius: 3px;
  font-family: Consolas, Monaco, monospace; font-size: 12px; color: #1b5e20;
}
.md pre {
  background: #f3faf3; border: 1px solid #d6ead6;
  padding: 10px 12px; border-radius: 3px;
  overflow-x: auto; margin: 10px 0;
}
.md pre code { background: none; border: none; padding: 0; font-size: 12px; }
.md hr { border: none; border-top: 1px solid #c8e6c9; margin: 14px 0; }
.md img { max-width: 100%; border-radius: 3px; margin: 6px 0; display: block; }
.md del { color: #9cb89c; }
.md a { color: #2e7d32; text-decoration: underline; }

.md mark { background: #fff59d; color: #17381a; padding: 0 2px; border-radius: 2px; }
.md sup { font-size: 0.75em; vertical-align: super; line-height: 0; }
.md .spoiler {
  background: #2e7d32; color: #2e7d32; border-radius: 3px;
  padding: 0 4px; cursor: help;
  transition: background .15s, color .15s;
}
.md .spoiler:hover { background: #eaf6ea; color: #17381a; }

.md table {
  border-collapse: collapse; margin: 10px 0; font-size: 13px;
  max-width: 100%;
}
.md th, .md td {
  border: 1px solid #c8e6c9; padding: 5px 10px;
  text-align: left; vertical-align: top;
}
.md th { background: #eaf6ea; color: #1b5e20; font-weight: bold; }
.md tr:nth-child(even) td { background: #f7fdf7; }
.md table code { font-size: 11px; }

.md ul.task-list { list-style: none; padding-left: 4px; }
.md ul.task-list li {
  display: flex; align-items: flex-start; gap: 8px; margin: 3px 0;
  padding-left: 0;
}
.md ul.task-list li input[type=checkbox] {
  margin: 3px 0 0; accent-color: #43a047; flex-shrink: 0;
}

.footer { text-align: center; color: #7d9c7d; font-size: 11px; padding: 6px 0 30px; }
"""


# ============================================================
#   LAYOUT
# ============================================================

def layout(anon: dict, content: str, active: str = "", wide: bool = False) -> str:
    def nav(href, icon_name, label, key):
        cls = "side-link active" if active == key else "side-link"
        return f'<a href="{href}" class="{cls}">{ic(icon_name, 14)}<span>{label}</span></a>'

    nav_items = nav("/", "home", "Лента", "feed")
    nav_items += nav("/create", "plus", "Создать пост", "create")

    layout_cls = "layout wide" if wide else "layout"

    return f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1760">
<title>litodon</title>
<style>{CSS}</style>
</head>
<body>
<header class="header">
  <div class="header-inner">
    <a href="/" class="logo">litodon<span>анонимная соцсеть</span></a>
    <div class="header-right">
      <a href="/create" class="header-btn">{ic("plus", 14)} Новый пост</a>
    </div>
  </div>
</header>
<div class="{layout_cls}">
  <aside class="sidebar">
    <div class="side-card side-nav">{nav_items}</div>
    <div class="side-card side-stats">
      <div class="side-stat">{ic("home", 12)} <b>{len(posts)}</b> постов</div>
      <div class="side-stat">{ic("comment", 12)} <b>{_counter['comment']}</b> комментариев</div>
    </div>
  </aside>
  <main class="content">
{content}
  </main>
</div>
<div class="footer">litodon &copy; 2026 &middot; полностью анонимно &middot; всё хранится в оперативной памяти</div>
</body>
</html>'''


# ============================================================
#   КОМПОНЕНТЫ
# ============================================================

def post_card(p: dict, anon: dict, link_back: bool = True) -> str:
    pid = p["id"]
    aid = anon["id"]
    score = len(p["up"]) - len(p["down"])
    score_cls = "score-pos" if score > 0 else ("score-neg" if score < 0 else "score-zero")

    up_cls = "vote-btn active-up" if aid in p["up"] else "vote-btn"
    down_cls = "vote-btn active-down" if aid in p["down"] else "vote-btn"
    votes = f'''
    <form method="post" action="/vote/{pid}" class="vote-form">
      <input type="hidden" name="value" value="up">
      <button class="{up_cls}" title="Плюс">{ic("up", 12)}</button>
    </form>
    <div class="score {score_cls}">{score}</div>
    <form method="post" action="/vote/{pid}" class="vote-form">
      <input type="hidden" name="value" value="down">
      <button class="{down_cls}" title="Минус">{ic("down", 12)}</button>
    </form>'''

    edited = ' <span class="edited">(изменено)</span>' if p.get("edited") else ""
    body = f'<div class="post-text md">{render_md(p["text"])}</div>'

    actions = []
    if link_back:
        actions.append(f'<a href="/p/{pid}" class="post-act">{ic("comment", 12)} {len(p["comments"])}</a>')
    if aid == p["author_id"]:
        actions.append(f'<a href="/p/{pid}/edit" class="post-act">{ic("edit", 12)} Редактировать</a>')
        actions.append(
            f'<form method="post" action="/p/{pid}/delete" class="inline-form" '
            f'onsubmit="return confirm(\'Удалить этот пост?\')">'
            f'<button type="submit" class="post-act post-act-danger">'
            f'{ic("trash", 12)} Удалить</button></form>'
        )
    actions_html = '<div class="post-actions">' + "".join(actions) + '</div>' if actions else ""

    comments_html = ""
    if link_back and p["comments"]:
        items = "".join(
            f'<div class="comment">'
            f'<div class="c-time">{fmt_time(c["created"])}</div>'
            f'<div class="c-text md">{render_md(c["text"])}</div>'
            f'</div>'
            for c in p["comments"]
        )
        comments_html = (f'<div class="comments">'
                         f'<div class="comments-title">{ic("comment", 12)} Комментарии ({len(p["comments"])})</div>'
                         f'{items}</div>')

    cform = ""
    if link_back:
        cform = (
            f'<form method="post" action="/comment/{pid}" class="cform">'
            f'<input type="text" name="text" maxlength="1000" '
            f'placeholder="Анонимный комментарий (markdown)..." required>'
            f'<button type="submit" title="Отправить">{ic("send", 12)}</button></form>'
        )

    return f'''
    <article class="box post" id="p{pid}">
      <div class="votes">{votes}</div>
      <div class="post-main">
        <div class="post-head">
          <span class="time">{fmt_time(p["created"])}{edited}</span>
        </div>
        {body}
        {actions_html}
        {comments_html}
        {cform}
      </div>
    </article>'''


def editor_view(p: dict | None = None, action: str = "/create") -> str:
    if p:
        text_value = escape(p["text"])
        heading = "Редактировать пост"
        submit_label = "Сохранить"
    else:
        text_value = ""
        heading = "Новый пост"
        submit_label = "Опубликовать"

    tb = lambda name, title, js: (
        f'<button type="button" class="tb" title="{title}" onclick="{js}">{ic(name, 14)}</button>'
    )

    return f'''
    <h1 class="page-title">{heading}</h1>
    <div class="box editor">
      <form method="post" action="{action}" id="post-form">
        <div class="editor-toolbar">
          {tb("bold",   "Жирный (Ctrl+B)",   "insertMd('**','**','жирный текст')")}
          {tb("italic", "Курсив (Ctrl+I)",   "insertMd('*','*','курсив')")}
          {tb("strike", "Зачёркнутый",       "insertMd('~~','~~','текст')")}
          {tb("code",   "Код в строке",      "insertMd('`','`','код')")}
          {tb("codeblock", "Блок кода",      "insertBlock('```\\n','\\n```')")}
          <span class="tb-sep"></span>
          {tb("h1", "Заголовок 1", "prefixLine('# ')")}
          {tb("h2", "Заголовок 2", "prefixLine('## ')")}
          {tb("h3", "Заголовок 3", "prefixLine('### ')")}
          <span class="tb-sep"></span>
          {tb("ul",    "Маркированный список", "prefixLine('- ')")}
          {tb("ol",    "Нумерованный список",  "prefixLine('1. ')")}
          {tb("task",  "Чек-лист",             "prefixLine('- [ ] ')")}
          {tb("quote", "Цитата",               "prefixLine('> ')")}
          {tb("hr",    "Разделитель (---)",    "insertBlock('\\n---\\n')")}
          <span class="tb-sep"></span>
          {tb("link",      "Ссылка",         "insertMd('[','](https://)','текст ссылки')")}
          {tb("image",     "Вставить как markdown по ссылке", "insertMd('![','](https://)','alt')")}
          {tb("upload",    "Загрузить картинку с ПК",        "document.getElementById('file-input').click()")}
          {tb("table",     "Таблица",        "insertBlock('\\n| Столбец 1 | Столбец 2 |\\n|-----------|-----------|\\n| Ячейка    | Ячейка    |\\n')")}
          {tb("highlight", "Выделение",      "insertMd('==','==','выделенный текст')")}
          {tb("spoiler",   "Спойлер",        "insertMd('||','||','скрытый текст')")}
          {tb("sup",       "Верхний индекс", "insertMd('^','^','2')")}
          {tb("emoji",     "Emoji ( :smile: )", "insertBlock(':smile: ')" )}
          <span class="tb-spacer"></span>
          <button type="button" class="tb tb-text active" id="preview-btn" onclick="togglePreview()">Предпросмотр</button>
        </div>
        <input type="file" id="file-input" accept="image/*" style="display:none">
        <div class="editor-body">
          <div class="editor-pane">
            <textarea id="editor" name="text" required
placeholder="Напишите что-нибудь...&#10;&#10;Markdown:&#10;**жирный**  *курсив*  ~~зачёркнутый~~  `код`  ==выделение==  ||спойлер||  ^верхний^&#10;# H1  ## H2  ### H3&#10;- список   1. список   - [ ] задача   &gt; цитата   --- разделитель&#10;[ссылка](https://)  ![img](https://)&#10;| табл | лицо |&#10;|------|------|&#10;| a    | b    |&#10;:smile: :fire: :heart:&#10;&#10;Картинки: жми иконку загрузки или просто Ctrl+V из буфера — вставится как base64.">{text_value}</textarea>
          </div>
          <div class="editor-pane editor-preview" id="preview-wrap">
            <div class="md" id="preview"></div>
          </div>
        </div>
        <div class="editor-foot">
          <div class="editor-hint">
            <b>Markdown:</b>
            <code>**жирный**</code> &middot; <code>*курсив*</code> &middot; <code>~~зачёркнутый~~</code> &middot;
            <code>`код`</code> &middot; <code>```блок```</code> &middot;
            <code># H1</code>/<code>## H2</code>/<code>### H3</code> &middot;
            <code>- список</code> &middot; <code>1. список</code> &middot; <code>- [ ] чек-лист</code> &middot;
            <code>&gt; цитата</code> &middot; <code>---</code> &middot;
            <code>[текст](url)</code> &middot; <code>![alt](url)</code> &middot;
            <code>==выделение==</code> &middot; <code>||спойлер||</code> &middot; <code>^верхний^</code> &middot;
            <code>:smile:</code> &middot;
            таблицы: <code>| a | b |</code> + <code>|---|---|</code> &middot;
            <b>картинки</b>: кнопка {ic("upload", 12)} или Ctrl+V (base64 не считается за символы)
          </div>
          <div class="editor-actions">
            <span class="counter" id="counter">0 / {MAX_POST}</span>
            <button type="submit" class="btn-primary">{ic("send", 14)} {submit_label}</button>
          </div>
        </div>
      </form>
    </div>
    <script>{EDITOR_JS.replace("__MAX__", str(MAX_POST))}</script>'''


EDITOR_JS = r"""
(function(){
  var ta = document.getElementById('editor');
  var counter = document.getElementById('counter');
  var previewEl = document.getElementById('preview');
  var previewWrap = document.getElementById('preview-wrap');
  var previewBtn = document.getElementById('preview-btn');
  var form = document.getElementById('post-form');
  var fileInput = document.getElementById('file-input');
  var previewOn = true;
  var previewTimer = null;
  var MAX = __MAX__;
  var MAX_IMG_BYTES = 5 * 1024 * 1024;   // 5 МБ на одну картинку

  // тот же regex, что и в Python: заменяем payload на заглушку для подсчёта
  var B64_RE = /data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+/g;

  function effectiveLength(s){
    return s.replace(B64_RE, 'data:image/...;base64,...').length;
  }

  function updateCounter(){
    var raw = ta.value;
    var eff = effectiveLength(raw);
    var imgs = (raw.match(/data:image\/[a-zA-Z0-9.+-]+;base64,/g) || []).length;
    var suffix = imgs ? ' <span class="b64hint">(+' + imgs + ' img)</span>' : '';
    counter.innerHTML = eff + ' / ' + MAX + suffix;
    if (eff > MAX) counter.classList.add('warn');
    else counter.classList.remove('warn');
  }

  function schedulePreview(){
    if (!previewOn) return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(runPreview, 220);
  }

  function runPreview(){
    if (!previewOn) return;
    var fd = new FormData();
    fd.append('text', ta.value);
    fetch('/api/preview', { method: 'POST', body: fd })
      .then(function(r){ return r.json(); })
      .then(function(j){
        previewEl.innerHTML = j.html || '<p style="color:#8aa38a">Пусто</p>';
      })
      .catch(function(){});
  }

  window.togglePreview = function(){
    previewOn = !previewOn;
    previewWrap.style.display = previewOn ? 'flex' : 'none';
    previewBtn.classList.toggle('active', previewOn);
    if (previewOn) runPreview();
  };

  window.insertMd = function(before, after, ph){
    var s = ta.selectionStart, e = ta.selectionEnd;
    var sel = ta.value.substring(s, e);
    var ins = sel || ph || '';
    ta.value = ta.value.substring(0, s) + before + ins + after + ta.value.substring(e);
    if (sel){
      ta.selectionStart = s + before.length;
      ta.selectionEnd = s + before.length + ins.length;
    } else {
      ta.selectionStart = ta.selectionEnd = s + before.length + ins.length;
    }
    ta.focus(); updateCounter(); schedulePreview();
  };

  window.prefixLine = function(prefix){
    var s = ta.selectionStart, e = ta.selectionEnd;
    var before = ta.value.substring(0, s);
    var lineStart = before.lastIndexOf('\n') + 1;
    var afterSel = ta.value.substring(e);
    var nl = afterSel.indexOf('\n');
    var lineEnd = nl === -1 ? ta.value.length : e + nl;
    var block = ta.value.substring(lineStart, lineEnd);
    var lines = block.split('\n');
    var nb = lines.map(function(l){ return prefix + l; }).join('\n');
    ta.value = ta.value.substring(0, lineStart) + nb + ta.value.substring(lineEnd);
    ta.selectionStart = lineStart;
    ta.selectionEnd = lineStart + nb.length;
    ta.focus(); updateCounter(); schedulePreview();
  };

  window.insertBlock = function(before, after){
    var s = ta.selectionStart, e = ta.selectionEnd;
    var sel = ta.value.substring(s, e);
    var ins = before + sel + (after || '');
    ta.value = ta.value.substring(0, s) + ins + ta.value.substring(e);
    ta.selectionStart = ta.selectionEnd = s + ins.length;
    ta.focus(); updateCounter(); schedulePreview();
  };

  function insertImageFile(file, altText){
    if (!file) return;
    if (file.size > MAX_IMG_BYTES){
      alert('Картинка слишком большая (макс 5 МБ). Текущий размер: ' +
            (file.size/1024/1024).toFixed(2) + ' МБ.');
      return;
    }
    var reader = new FileReader();
    reader.onload = function(ev){
      var dataUrl = ev.target.result;
      var alt = (altText || file.name || 'image').replace(/[\[\]()]/g, '');
      // вставляем на новой строке, чтобы не склеивалось с текстом
      var prefix = ta.selectionStart > 0 && ta.value[ta.selectionStart - 1] !== '\n' ? '\n' : '';
      window.insertBlock(prefix + '![' + alt + '](' + dataUrl + ')\n', '');
    };
    reader.readAsDataURL(file);
  }

  // --- загрузка через файловый пикер ---
  if (fileInput){
    fileInput.addEventListener('change', function(e){
      var f = e.target.files && e.target.files[0];
      insertImageFile(f);
      e.target.value = '';   // позволяем выбрать тот же файл повторно
    });
  }

  // --- вставка из буфера (Ctrl+V со скриншотом) ---
  ta.addEventListener('paste', function(e){
    if (!e.clipboardData || !e.clipboardData.items) return;
    var items = e.clipboardData.items;
    for (var i = 0; i < items.length; i++){
      var it = items[i];
      if (it.kind === 'file' && it.type.indexOf('image/') === 0){
        e.preventDefault();
        insertImageFile(it.getAsFile(), 'вставленная картинка');
        return;
      }
    }
    // если вставили просто текст с data:image — тоже перехватываем, чтобы не сломать счётчик
    var txt = e.clipboardData.getData('text/plain');
    if (txt && /data:image\/[a-zA-Z0-9.+-]+;base64,/.test(txt)){
      e.preventDefault();
      window.insertBlock(txt, '');
      return;
    }
  });

  // --- Ctrl+B / Ctrl+I / Tab ---
  ta.addEventListener('keydown', function(ev){
    if (ev.key === 'Tab'){
      ev.preventDefault();
      window.insertMd('  ', '', '');
      return;
    }
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'b'){
      ev.preventDefault(); window.insertMd('**','**','жирный текст'); return;
    }
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'i'){
      ev.preventDefault(); window.insertMd('*','*','курсив'); return;
    }
  });

  // --- не отправляем, если по эффективной длине перебор ---
  form.addEventListener('submit', function(e){
    var eff = effectiveLength(ta.value);
    if (eff > MAX){
      e.preventDefault();
      alert('Слишком длинный текст: ' + eff + ' / ' + MAX + ' символов.\n' +
            'Картинки в base64 не считаются, значит превышение по самому тексту.');
    }
  });

  ta.addEventListener('input', function(){ updateCounter(); schedulePreview(); });

  updateCounter();
  runPreview();
})();
"""


# ============================================================
#   РОУТЫ
# ============================================================

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    anon = request.state.anon
    if not posts:
        feed = '<div class="box empty">Постов пока нет. <a href="/create">Создайте первый!</a></div>'
    else:
        ordered = sorted(posts, key=lambda x: x["created"], reverse=True)
        feed = "".join(post_card(p, anon) for p in ordered)
    return layout(anon, '<h1 class="page-title">Лента</h1>' + feed, active="feed")


@app.get("/create", response_class=HTMLResponse)
def create_page(request: Request):
    anon = request.state.anon
    return layout(anon, editor_view(), active="create", wide=True)


@app.post("/create")
def create_post(request: Request, text: str = Form(...)):
    anon = request.state.anon
    text = text.strip()
    # валидация: эффективная длина (без base64) ≤ MAX_POST, сырая ≤ MAX_RAW
    if text and effective_length(text) <= MAX_POST and len(text) <= MAX_RAW:
        _counter["post"] += 1
        pid = _counter["post"]
        posts.append({
            "id": pid,
            "author_id": anon["id"],
            "text": text,              # НЕ обрезаем по MAX_POST — base64 может быть длинным
            "created": time.time(),
            "edited": None,
            "up": set(),
            "down": set(),
            "comments": [],
        })
        return RedirectResponse(f"/p/{pid}", status_code=303)
    return RedirectResponse("/create", status_code=303)


@app.get("/p/{post_id}", response_class=HTMLResponse)
def post_page(post_id: int, request: Request):
    anon = request.state.anon
    p = next((x for x in posts if x["id"] == post_id), None)
    if not p:
        return HTMLResponse(
            layout(anon, '<div class="box empty">Пост не найден. <a href="/">На главную</a></div>'),
            status_code=404)
    back = f'<a href="/" class="back-link">{ic("arrow-left", 14)} Назад к ленте</a>'
    return layout(anon, back + post_card(p, anon), active="feed")


@app.get("/p/{post_id}/edit", response_class=HTMLResponse)
def edit_page(post_id: int, request: Request):
    anon = request.state.anon
    p = next((x for x in posts if x["id"] == post_id), None)
    if not p:
        return HTMLResponse(layout(anon, '<div class="box empty">Пост не найден.</div>'),
                            status_code=404)
    if anon["id"] != p["author_id"]:
        return RedirectResponse(f"/p/{post_id}", status_code=303)
    back = f'<a href="/p/{post_id}" class="back-link">{ic("arrow-left", 14)} Назад к посту</a>'
    return layout(anon, back + editor_view(p, action=f"/p/{post_id}/edit"),
                  active="feed", wide=True)


@app.post("/p/{post_id}/edit")
def edit_post(post_id: int, request: Request, text: str = Form(...)):
    anon = request.state.anon
    p = next((x for x in posts if x["id"] == post_id), None)
    if not p or anon["id"] != p["author_id"]:
        return RedirectResponse("/", status_code=303)
    text = text.strip()
    if text and effective_length(text) <= MAX_POST and len(text) <= MAX_RAW:
        p["text"] = text
        p["edited"] = time.time()
        return RedirectResponse(f"/p/{post_id}", status_code=303)
    return RedirectResponse(f"/p/{post_id}/edit", status_code=303)


@app.post("/p/{post_id}/delete")
def delete_post(post_id: int, request: Request):
    anon = request.state.anon
    p = next((x for x in posts if x["id"] == post_id), None)
    if p and anon["id"] == p["author_id"]:
        posts.remove(p)
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(f"/p/{post_id}", status_code=303)


@app.post("/vote/{post_id}")
def vote(post_id: int, request: Request, value: str = Form(...)):
    aid = request.state.anon["id"]
    p = next((x for x in posts if x["id"] == post_id), None)
    if p:
        if value == "up":
            if aid in p["up"]:
                p["up"].discard(aid)
            else:
                p["up"].add(aid); p["down"].discard(aid)
        elif value == "down":
            if aid in p["down"]:
                p["down"].discard(aid)
            else:
                p["down"].add(aid); p["up"].discard(aid)
    return RedirectResponse(safe_redirect(request), status_code=303)


@app.post("/comment/{post_id}")
def add_comment(post_id: int, request: Request, text: str = Form(...)):
    p = next((x for x in posts if x["id"] == post_id), None)
    text = text.strip()
    if p and text:
        _counter["comment"] += 1
        p["comments"].append({
            "id": _counter["comment"],
            "text": text[:1000],
            "created": time.time(),
        })
    return RedirectResponse(safe_redirect(request), status_code=303)


@app.post("/api/preview")
def api_preview(text: str = Form("")):
    return JSONResponse({"html": render_md(text[:MAX_RAW])})


# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
