from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
import uvicorn
import uuid
import html
from datetime import datetime

# ==============================
#           НАСТРОЙКИ
# ==============================

# --- сервер ---
HOST = "0.0.0.0"
PORT = 8000

# --- внешний вид ---
SITE_NAME       = "SldChat"
LOGO_POSITION   = "center"                    # left | center | right
AUTHOR          = "SldShrLab"
FONT_FAMILY     = "Verdana, Arial, sans-serif"
FONT_SIZE       = 15
MAX_WIDTH       = 640
BG_COLOR        = "#dcdcdc"
BOX_BG_COLOR    = "#ffffff"
ACCENT_COLOR    = "#0000cc"

# --- лимиты ---
MAX_POSTS              = 300    # сколько постов держим в памяти
MAX_COMMENTS_PER_POST  = 100    # сколько комментариев у одного поста
MAX_POST_LEN           = 700    # символов в посте
MAX_COMMENT_LEN        = 500    # символов в комментарии
MAX_SEARCH_LEN         = 100    # символов в поисковом запросе

# --- поведение ---
ALLOW_POSTING        = True     # можно ли создавать посты
ALLOW_COMMENTS       = True     # можно ли комментировать
ALLOW_SEARCH         = True     # показывать ли поиск
SHOW_TIMESTAMPS      = True     # показывать дату/время у постов
SHOW_COPY_BUTTON     = True     # кнопка "скопировать текст"
AUTO_REFRESH_SECONDS = 0        # автообновление ленты (0 = выключено)

# ==============================

if LOGO_POSITION not in ("left", "center", "right"):
    LOGO_POSITION = "left"

app = FastAPI(title=SITE_NAME)
posts = {}


def page(title, body, refresh=0):
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh > 0 else ""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_tag}
<title>{html.escape(title)}</title>
<style>
  :root {{
    --bg: {BG_COLOR};
    --box: {BOX_BG_COLOR};
    --accent: {ACCENT_COLOR};
    --font: {FONT_FAMILY};
    --size: {FONT_SIZE}px;
    --width: {MAX_WIDTH}px;
  }}

  * {{
    box-sizing: border-box;
    scrollbar-width: none;
    -ms-overflow-style: none;
  }}
  *::-webkit-scrollbar {{ width: 0; height: 0; display: none; }}

  html, body {{ height: 100%; }}
  body {{
    font-family: var(--font);
    background: var(--bg);
    color: #000;
    max-width: var(--width);
    margin: 0 auto;
    padding: 10px;
    font-size: var(--size);
    display: flex;
    flex-direction: column;
    min-height: 100vh;
    user-select: none;
    -webkit-user-select: none;
  }}
  input, textarea {{ user-select: text; -webkit-user-select: text; }}

  h1 {{
    font-size: 20px;
    margin: 10px 0;
    text-align: {LOGO_POSITION};
  }}
  h2 {{ font-size: 17px; margin: 14px 0 8px; }}

  a {{ color: var(--accent); }}
  a:hover {{ color: #cc0000; }}

  .post, .comment, .compose {{
    background: var(--box);
    border: 1px solid #999;
    padding: 10px;
    margin-bottom: 10px;
    overflow-wrap: anywhere;
    word-wrap: break-word;
  }}
  .comment {{ background: #f6f6f6; margin-left: 14px; }}

  textarea {{
    width: 100%;
    padding: 8px;
    font-size: var(--size);
    font-family: inherit;
    border: 1px solid #888;
    background: #fff;
    min-height: 90px;
    max-height: 260px;
    resize: vertical;
    overflow: auto;
  }}

  .compose-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-top: 8px;
    flex-wrap: wrap;
  }}
  .compose-row .counter {{ margin-left: auto; }}

  button {{
    padding: 8px 16px;
    font-size: var(--size);
    background: #eee;
    color: #000;
    border: 1px solid #666;
    cursor: pointer;
  }}
  button:hover:enabled {{ background: #ccc; }}
  button:disabled {{ opacity: 0.5; cursor: not-allowed; }}

  .counter {{ color: #666; font-size: 13px; }}
  .counter.over {{ color: #cc0000; font-weight: bold; }}

  .searchbar {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 12px;
  }}
  .searchbar input, .searchbar select {{
    padding: 8px;
    font-size: var(--size);
    font-family: inherit;
    border: 1px solid #888;
    background: #fff;
  }}
  .searchbar input[type="text"] {{ flex: 1 1 180px; min-width: 0; }}

  .meta {{ color: #666; font-size: 12px; margin-top: 8px; }}
  hr {{ border: none; border-top: 1px solid #999; margin: 14px 0; }}

  main {{ flex: 1 0 auto; }}

  footer {{
    flex-shrink: 0;
    margin-top: 20px;
    padding-top: 10px;
    border-top: 1px solid #999;
    color: #555;
    font-size: 12px;
    text-align: center;
  }}
  footer a {{ color: #555; }}
  footer a:hover {{ color: #000; }}

  ol.privacy li {{ margin-bottom: 10px; }}

  .copy-btn {{
    font-size: 12px;
    padding: 4px 10px;
    margin-top: 8px;
    background: #f0f0f0;
  }}
  .copy-btn:hover {{ background: #ddd; }}
</style>
</head>
<body>
<main>
{body}
</main>
<footer>
  {html.escape(SITE_NAME)} &copy; {html.escape(AUTHOR)} &middot;
  <a href="/privacy">Политика конфиденциальности</a>
</footer>
<script>
(function () {{
  document.querySelectorAll('[data-counter]').forEach(function (ta) {{
    var counter = document.getElementById(ta.dataset.counter);
    var btn = document.getElementById(ta.dataset.btn);
    var max = parseInt(ta.dataset.max, 10);
    function update() {{
      var n = ta.value.length;
      counter.textContent = n + ' / ' + max;
      if (n > max) {{
        counter.classList.add('over');
        btn.disabled = true;
      }} else {{
        counter.classList.remove('over');
        btn.disabled = false;
      }}
    }}
    ta.addEventListener('input', update);
    update();
  }});

  document.querySelectorAll('.copy-btn').forEach(function (btn) {{
    btn.addEventListener('click', function () {{
      var el = document.getElementById(btn.dataset.target);
      if (!el) return;
      var text = el.textContent.trim();
      var restore = function () {{
        var old = btn.textContent;
        btn.textContent = 'Скопировано';
        setTimeout(function () {{ btn.textContent = old; }}, 1200);
      }};
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text).then(restore, restore);
      }} else {{
        var tmp = document.createElement('textarea');
        tmp.value = text;
        document.body.appendChild(tmp);
        tmp.select();
        try {{ document.execCommand('copy'); }} catch (e) {{}}
        document.body.removeChild(tmp);
        restore();
      }}
    }});
  }});
}})();
</script>
</body>
</html>"""


def options(pairs, current):
    out = []
    for value, label in pairs:
        sel = " selected" if value == current else ""
        out.append(f'<option value="{html.escape(value, quote=True)}"{sel}>{html.escape(label)}</option>')
    return "".join(out)


def not_found(message):
    body = f"""
    <p><a href="/">&larr; На главную</a></p>
    <h1>404</h1>
    <div class="post">
      <p><b>{html.escape(message)}</b></p>
      <p class="meta">Такой страницы на {html.escape(SITE_NAME)} нет.</p>
    </div>
    """
    return page("404 — не найдено", body)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return HTMLResponse(not_found("Страница не найдена"), status_code=404)
    body = f"""
    <p><a href="/">&larr; На главную</a></p>
    <h1>Ошибка {exc.status_code}</h1>
    <div class="post">{html.escape(str(exc.detail))}</div>
    """
    return HTMLResponse(page(f"Ошибка {exc.status_code}", body), status_code=exc.status_code)


@app.get("/", response_class=HTMLResponse)
def index(q: str = "", sort: str = "new", flt: str = "all"):
    q = (q or "").strip()[:MAX_SEARCH_LEN]
    if sort not in ("new", "old", "hot", "cold"):
        sort = "new"
    if flt not in ("all", "with", "without"):
        flt = "all"

    items = list(posts.values())

    if q:
        needle = q.lower()
        items = [p for p in items if needle in p["text"].lower()]

    if flt == "with":
        items = [p for p in items if p["comments"]]
    elif flt == "without":
        items = [p for p in items if not p["comments"]]

    if sort == "new":
        items.sort(key=lambda p: p["created"], reverse=True)
    elif sort == "old":
        items.sort(key=lambda p: p["created"])
    elif sort == "hot":
        items.sort(key=lambda p: len(p["comments"]), reverse=True)
    else:
        items.sort(key=lambda p: len(p["comments"]))

    feed = []
    for p in items:
        stamp = p["created"].strftime("%d.%m.%Y %H:%M") if SHOW_TIMESTAMPS else ""
        meta_parts = []
        if stamp:
            meta_parts.append(stamp)
        meta_parts.append(f'<a href="/p/{p["id"]}">открыть пост</a>')
        if ALLOW_COMMENTS:
            meta_parts.append(f'комментариев: {len(p["comments"])}')
        feed.append(f"""
        <div class="post">
          <div>{html.escape(p["text"])}</div>
          <div class="meta">{" &middot; ".join(meta_parts)}</div>
        </div>""")

    if feed:
        feed_html = "".join(feed)
    elif q or flt != "all":
        feed_html = "<p>Ничего не найдено.</p>"
    else:
        feed_html = "<p>Пока пусто. Напиши первым.</p>"

    search_value = html.escape(q, quote=True)

    if ALLOW_POSTING:
        compose_html = f"""
        <div class="compose">
          <form method="post" action="/post">
            <textarea name="text" data-counter="post-count" data-btn="post-btn"
                      data-max="{MAX_POST_LEN}"
                      placeholder="Что думаешь? (до {MAX_POST_LEN} символов)" required></textarea>
            <div class="compose-row">
              <button type="submit" id="post-btn">Отправить</button>
              <span class="counter" id="post-count">0 / {MAX_POST_LEN}</span>
            </div>
          </form>
        </div>"""
    else:
        compose_html = '<div class="compose"><p class="meta">Отправка постов сейчас отключена.</p></div>'

    if ALLOW_SEARCH:
        sort_opts = options([
            ("new", "Новые"),
            ("old", "Старые"),
            ("hot", "Много комментов"),
            ("cold", "Мало комментов"),
        ], sort)
        flt_opts = options([
            ("all", "Все"),
            ("with", "С комментами"),
            ("without", "Без комментов"),
        ], flt)
        search_html = f"""
        <form method="get" action="/" class="searchbar">
          <input type="text" name="q" value="{search_value}"
                 placeholder="Поиск по тексту..." maxlength="{MAX_SEARCH_LEN}">
          <select name="sort">{sort_opts}</select>
          <select name="flt">{flt_opts}</select>
          <button type="submit">Найти</button>
        </form>"""
    else:
        search_html = ""

    body = f"""
    <h1>{html.escape(SITE_NAME)}</h1>
    {compose_html}
    {search_html}
    <h2>Все посты ({len(items)})</h2>
    {feed_html}
    """
    return page(SITE_NAME, body, refresh=AUTO_REFRESH_SECONDS)


@app.post("/post")
def create_post(text: str = Form(...)):
    if not ALLOW_POSTING:
        return RedirectResponse("/", status_code=303)

    text = (text or "").strip()[:MAX_POST_LEN]
    if not text:
        return RedirectResponse("/", status_code=303)

    while len(posts) >= MAX_POSTS:
        oldest = next(iter(posts))
        del posts[oldest]

    pid = uuid.uuid4().hex[:8]
    posts[pid] = {
        "id": pid,
        "text": text,
        "created": datetime.now(),
        "comments": [],
    }
    return RedirectResponse(f"/p/{pid}", status_code=303)


@app.get("/p/{pid}", response_class=HTMLResponse)
def view_post(pid: str):
    p = posts.get(pid)
    if not p:
        return HTMLResponse(not_found("Пост не найден"), status_code=404)

    copy_post_html = (
        '<button type="button" class="copy-btn" data-target="post-body">Скопировать текст</button>'
        if SHOW_COPY_BUTTON else ""
    )

    stamp = p["created"].strftime("%d.%m.%Y %H:%M") if SHOW_TIMESTAMPS else ""
    meta_parts = []
    if stamp:
        meta_parts.append(stamp)
    meta_parts.append(f'ссылка: <code>/p/{p["id"]}</code>')

    if ALLOW_COMMENTS:
        comments = []
        for c in p["comments"]:
            c_stamp = c["created"].strftime("%d.%m.%Y %H:%M") if SHOW_TIMESTAMPS else ""
            copy_html = (
                f'<button type="button" class="copy-btn" data-target="c-{c["id"]}">Скопировать текст</button>'
                if SHOW_COPY_BUTTON else ""
            )
            comments.append(f"""
            <div class="comment">
              <div id="c-{c["id"]}">{html.escape(c["text"])}</div>
              {f'<div class="meta">{c_stamp}</div>' if c_stamp else ''}
              {copy_html}
            </div>""")
        comments_html = "".join(comments) if comments else "<p class='meta'>Комментариев пока нет.</p>"

        compose_html = f"""
        <div class="compose">
          <form method="post" action="/p/{p["id"]}/comment">
            <textarea name="text" data-counter="cmt-count" data-btn="cmt-btn"
                      data-max="{MAX_COMMENT_LEN}"
                      placeholder="Анонимный комментарий (до {MAX_COMMENT_LEN} символов)" required></textarea>
            <div class="compose-row">
              <button type="submit" id="cmt-btn">Отправить</button>
              <span class="counter" id="cmt-count">0 / {MAX_COMMENT_LEN}</span>
            </div>
          </form>
        </div>"""
    else:
        comments_html = ""
        compose_html = ""

    body = f"""
    <p><a href="/">&larr; На главную</a></p>
    <h1>Пост</h1>

    <div class="post">
      <div id="post-body">{html.escape(p["text"])}</div>
      <div class="meta">{" &middot; ".join(meta_parts)}</div>
      {copy_post_html}
    </div>

    {f'<h2>Комментарии ({len(p["comments"])})</h2>' if ALLOW_COMMENTS else ''}
    {comments_html}
    {compose_html}
    """
    return page(f"Пост {pid}", body)


@app.post("/p/{pid}/comment")
def add_comment(pid: str, text: str = Form(...)):
    if not ALLOW_COMMENTS:
        return RedirectResponse(f"/p/{pid}", status_code=303)

    p = posts.get(pid)
    if not p:
        return RedirectResponse("/", status_code=303)

    text = (text or "").strip()[:MAX_COMMENT_LEN]
    if text:
        p["comments"].append({
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "created": datetime.now(),
        })
        if len(p["comments"]) > MAX_COMMENTS_PER_POST:
            del p["comments"][:len(p["comments"]) - MAX_COMMENTS_PER_POST]

    return RedirectResponse(f"/p/{pid}", status_code=303)


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    body = f"""
    <p><a href="/">&larr; На главную</a></p>
    <h1>Политика конфиденциальности</h1>

    <div class="post">
      <ol class="privacy">
        <li>
          <b>{html.escape(SITE_NAME)} полностью анонимная.</b><br>
          Ни администратор сервера, ни хостинг, ни кто-либо ещё не знает,
          кто именно отправил тот или иной пост или комментарий. Мы не
          запрашиваем имя, e-mail, не ставим куки, не создаём аккаунты и
          не привязываем записи к человеку. Сервер не сохраняет IP-адреса
          посетителей, не логирует запросы и не передаёт их третьим лицам.
          Всё, что сохраняется — это сам текст и время отправки.
        </li>
        <li>
          <b>При перезагрузке сервера все данные удаляются.</b><br>
          Посты и комментарии хранятся только в оперативной памяти.
          После любого перезапуска или выключения сервера они исчезают
          безвозвратно и восстановлению не подлежат.
        </li>
        <li>
          <b>Администратор сервера не может менять правила на своём сервере.</b><br>
          Правила и принципы работы {html.escape(SITE_NAME)} зафиксированы и не подлежат
          изменению по желанию администратора или владельца хостинга.
          Обещанная анонимность и удаление данных при перезагрузке —
          неотъемлемая часть работы сервиса.
        </li>
      </ol>
    </div>
    """
    return page("Политика конфиденциальности", body)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        access_log=False,
        proxy_headers=False,
        log_level="warning",
    )
