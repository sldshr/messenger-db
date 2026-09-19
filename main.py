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

HOST = "0.0.0.0"
PORT = 8000

SITE_NAME       = "SldChat"
LOGO_POSITION   = "left"
AUTHOR          = "SldShrLab"
FONT_FAMILY     = "Verdana, Arial, sans-serif"
FONT_SIZE       = 15
MAX_WIDTH       = 640
BG_COLOR        = "#dcdcdc"
BOX_BG_COLOR    = "#ffffff"
ACCENT_COLOR    = "#0000cc"

MAX_POSTS              = 300
MAX_COMMENTS_PER_POST  = 100
MAX_POST_LEN           = 700
MAX_COMMENT_LEN        = 500
MAX_SEARCH_LEN         = 100

ALLOW_POSTING        = True
ALLOW_COMMENTS       = True
ALLOW_SEARCH         = True
SHOW_TIMESTAMPS      = True
SHOW_COPY_BUTTON     = True
AUTO_REFRESH_SECONDS = 0

LANG_DEFAULT = "ru"
LANG_OPTIONS = ("ru", "en")

# ==============================

STRINGS = {
    "ru": {
        "post_placeholder":     "Что думаешь? (до {n} символов)",
        "comment_placeholder":  "Анонимный комментарий (до {n} символов)",
        "send":                 "Отправить",
        "search_placeholder":   "Поиск по тексту...",
        "search_button":        "Найти",
        "sort_new":             "Новые",
        "sort_old":             "Старые",
        "sort_hot":             "Много комментов",
        "sort_cold":            "Мало комментов",
        "flt_all":              "Все",
        "flt_with":             "С комментами",
        "flt_without":          "Без комментов",
        "all_posts":            "Все посты",
        "nothing_found":        "Ничего не найдено.",
        "empty_feed":           "Пока пусто. Напиши первым.",
        "open_post":            "открыть пост",
        "comments_word":        "комментариев",
        "back_home":            "На главную",
        "post_heading":         "Пост",
        "comments_heading":     "Комментарии",
        "no_comments":          "Комментариев пока нет.",
        "copy_text":            "Скопировать текст",
        "copied":               "Скопировано",
        "privacy_link":         "Политика конфиденциальности",
        "privacy_title":        "Политика конфиденциальности",
        "posting_disabled":     "Отправка постов сейчас отключена.",
        "page_not_found_title": "404 — не найдено",
        "page_not_found":       "Страница не найдена",
        "post_not_found":       "Пост не найден",
        "no_page":              "Такой страницы на {site} нет.",
        "error_heading":        "Ошибка",
        "link_word":            "ссылка",
        "privacy_p1_title":     "{site} полностью анонимная.",
        "privacy_p1_body":      "Ни администратор сервера, ни хостинг, ни кто-либо ещё не знает, кто именно отправил тот или иной пост или комментарий. Мы не запрашиваем имя, e-mail, не ставим куки, не создаём аккаунты и не привязываем записи к человеку. Сервер не сохраняет IP-адреса посетителей, не логирует запросы и не передаёт их третьим лицам. Всё, что сохраняется — это сам текст и время отправки.",
        "privacy_p2_title":     "При перезагрузке сервера все данные удаляются.",
        "privacy_p2_body":      "Посты и комментарии хранятся только в оперативной памяти. После любого перезапуска или выключения сервера они исчезают безвозвратно и восстановлению не подлежат.",
        "privacy_p3_title":     "Администратор сервера не может менять правила на своём сервере.",
        "privacy_p3_body":      "Правила и принципы работы {site} зафиксированы и не подлежат изменению по желанию администратора или владельца хостинга. Обещанная анонимность и удаление данных при перезагрузке — неотъемлемая часть работы сервиса.",
    },
    "en": {
        "post_placeholder":     "What's on your mind? (up to {n} chars)",
        "comment_placeholder":  "Anonymous comment (up to {n} chars)",
        "send":                 "Send",
        "search_placeholder":   "Search text...",
        "search_button":        "Search",
        "sort_new":             "Newest",
        "sort_old":             "Oldest",
        "sort_hot":             "Most comments",
        "sort_cold":            "Fewest comments",
        "flt_all":              "All",
        "flt_with":             "With comments",
        "flt_without":          "Without comments",
        "all_posts":            "All posts",
        "nothing_found":        "Nothing found.",
        "empty_feed":           "Empty for now. Be the first.",
        "open_post":            "open post",
        "comments_word":        "comments",
        "back_home":            "Home",
        "post_heading":         "Post",
        "comments_heading":     "Comments",
        "no_comments":          "No comments yet.",
        "copy_text":            "Copy text",
        "copied":               "Copied",
        "privacy_link":         "Privacy policy",
        "privacy_title":        "Privacy policy",
        "posting_disabled":     "Posting is disabled right now.",
        "page_not_found_title": "404 — not found",
        "page_not_found":       "Page not found",
        "post_not_found":       "Post not found",
        "no_page":              "No such page on {site}.",
        "error_heading":        "Error",
        "link_word":            "link",
        "privacy_p1_title":     "{site} is fully anonymous.",
        "privacy_p1_body":      "Neither the server administrator, nor the hosting provider, nor anyone else knows who exactly sent a given post or comment. We do not ask for a name or e-mail, we do not set cookies, we do not create accounts, and we do not link records to a person. The server does not store visitors' IP addresses, does not log requests, and does not share them with third parties. All that is stored is the text itself and the time it was sent.",
        "privacy_p2_title":     "All data is deleted when the server restarts.",
        "privacy_p2_body":      "Posts and comments are kept only in RAM. After any restart or shutdown of the server they disappear forever and cannot be recovered.",
        "privacy_p3_title":     "The server administrator cannot change the rules on their own server.",
        "privacy_p3_body":      "The rules and principles of {site} are fixed and cannot be changed at the will of the administrator or the hosting owner. The promised anonymity and data deletion on restart are an integral part of how the service works.",
    },
}


def t(lang, key, **kw):
    table = STRINGS.get(lang) or STRINGS[LANG_DEFAULT]
    value = table.get(key) or STRINGS[LANG_DEFAULT].get(key, key)
    return value.format(**kw) if kw else value


def pick_lang(lang):
    return lang if lang in LANG_OPTIONS else LANG_DEFAULT


def with_lang(path, lang):
    if lang == LANG_DEFAULT:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}lang={lang}"


if LOGO_POSITION not in ("left", "center", "right"):
    LOGO_POSITION = "left"

app = FastAPI(title=SITE_NAME)
posts = {}


def page(title, body, lang, refresh=0):
    lang = pick_lang(lang)
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh > 0 else ""
    copy_label = t(lang, "copied")
    return f"""<!DOCTYPE html>
<html lang="{lang}">
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
  }}
  .comment {{ background: #f6f6f6; margin-left: 14px; }}

  /* сохранение переводов строк и перенос длинных слов */
  .txt {{
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-wrap: break-word;
  }}

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
  footer .lang-switch a.active {{
    color: #000;
    font-weight: bold;
    text-decoration: none;
  }}

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
  <a href="{with_lang('/privacy', lang)}">{t(lang, 'privacy_link')}</a>
  &middot;
  <span class="lang-switch">
    <a href="?lang=ru" class="{'active' if lang == 'ru' else ''}">RU</a>
    /
    <a href="?lang=en" class="{'active' if lang == 'en' else ''}">EN</a>
  </span>
</footer>
<script>
var COPY_LABEL = {copy_label!r};

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
        btn.textContent = COPY_LABEL;
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


def clean_text(raw):
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text


def not_found(message, lang):
    lang = pick_lang(lang)
    body = f"""
    <p><a href="{with_lang('/', lang)}">&larr; {t(lang, 'back_home')}</a></p>
    <h1>404</h1>
    <div class="post">
      <p><b>{html.escape(message)}</b></p>
      <p class="meta">{html.escape(t(lang, 'no_page', site=SITE_NAME))}</p>
    </div>
    """
    return page(t(lang, "page_not_found_title"), body, lang)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    lang = pick_lang(request.query_params.get("lang"))
    if exc.status_code == 404:
        return HTMLResponse(not_found(t(lang, "page_not_found"), lang), status_code=404)
    body = f"""
    <p><a href="{with_lang('/', lang)}">&larr; {t(lang, 'back_home')}</a></p>
    <h1>{t(lang, 'error_heading')} {exc.status_code}</h1>
    <div class="post">{html.escape(str(exc.detail))}</div>
    """
    return HTMLResponse(page(f"{t(lang, 'error_heading')} {exc.status_code}", body, lang),
                        status_code=exc.status_code)


@app.get("/", response_class=HTMLResponse)
def index(q: str = "", sort: str = "new", flt: str = "all", lang: str = LANG_DEFAULT):
    lang = pick_lang(lang)
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
            meta_parts.append(html.escape(stamp))
        meta_parts.append(f'<a href="{with_lang(f"/p/{p["id"]}", lang)}">{t(lang, "open_post")}</a>')
        if ALLOW_COMMENTS:
            meta_parts.append(f'{t(lang, "comments_word")}: {len(p["comments"])}')
        feed.append(f"""
        <div class="post">
          <div class="txt">{html.escape(p["text"])}</div>
          <div class="meta">{" &middot; ".join(meta_parts)}</div>
        </div>""")

    if feed:
        feed_html = "".join(feed)
    elif q or flt != "all":
        feed_html = f"<p>{html.escape(t(lang, 'nothing_found'))}</p>"
    else:
        feed_html = f"<p>{html.escape(t(lang, 'empty_feed'))}</p>"

    search_value = html.escape(q, quote=True)
    lang_input = f'<input type="hidden" name="lang" value="{lang}">' if lang != LANG_DEFAULT else ""

    if ALLOW_POSTING:
        compose_html = f"""
        <div class="compose">
          <form method="post" action="/post">
            <textarea name="text" data-counter="post-count" data-btn="post-btn"
                      data-max="{MAX_POST_LEN}"
                      placeholder="{html.escape(t(lang, 'post_placeholder', n=MAX_POST_LEN), quote=True)}"
                      required></textarea>
            <div class="compose-row">
              <button type="submit" id="post-btn">{html.escape(t(lang, 'send'))}</button>
              <span class="counter" id="post-count">0 / {MAX_POST_LEN}</span>
            </div>
            {lang_input}
          </form>
        </div>"""
    else:
        compose_html = f'<div class="compose"><p class="meta">{html.escape(t(lang, "posting_disabled"))}</p></div>'

    if ALLOW_SEARCH:
        sort_opts = options([
            ("new", t(lang, "sort_new")),
            ("old", t(lang, "sort_old")),
            ("hot", t(lang, "sort_hot")),
            ("cold", t(lang, "sort_cold")),
        ], sort)
        flt_opts = options([
            ("all", t(lang, "flt_all")),
            ("with", t(lang, "flt_with")),
            ("without", t(lang, "flt_without")),
        ], flt)
        search_html = f"""
        <form method="get" action="/" class="searchbar">
          <input type="text" name="q" value="{search_value}"
                 placeholder="{html.escape(t(lang, 'search_placeholder'), quote=True)}"
                 maxlength="{MAX_SEARCH_LEN}">
          <select name="sort">{sort_opts}</select>
          <select name="flt">{flt_opts}</select>
          <button type="submit">{html.escape(t(lang, 'search_button'))}</button>
          {lang_input}
        </form>"""
    else:
        search_html = ""

    body = f"""
    <h1>{html.escape(SITE_NAME)}</h1>
    {compose_html}
    {search_html}
    <h2>{html.escape(t(lang, 'all_posts'))} ({len(items)})</h2>
    {feed_html}
    """
    return page(SITE_NAME, body, lang, refresh=AUTO_REFRESH_SECONDS)


@app.post("/post")
def create_post(text: str = Form(...), lang: str = Form(LANG_DEFAULT)):
    lang = pick_lang(lang)
    if not ALLOW_POSTING:
        return RedirectResponse(with_lang("/", lang), status_code=303)

    text = clean_text(text)[:MAX_POST_LEN]
    if not text:
        return RedirectResponse(with_lang("/", lang), status_code=303)

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
    return RedirectResponse(with_lang(f"/p/{pid}", lang), status_code=303)


@app.get("/p/{pid}", response_class=HTMLResponse)
def view_post(pid: str, lang: str = LANG_DEFAULT):
    lang = pick_lang(lang)
    p = posts.get(pid)
    if not p:
        return HTMLResponse(not_found(t(lang, "post_not_found"), lang), status_code=404)

    copy_post_html = (
        f'<button type="button" class="copy-btn" data-target="post-body">{html.escape(t(lang, "copy_text"))}</button>'
        if SHOW_COPY_BUTTON else ""
    )

    stamp = p["created"].strftime("%d.%m.%Y %H:%M") if SHOW_TIMESTAMPS else ""
    meta_parts = []
    if stamp:
        meta_parts.append(html.escape(stamp))
    meta_parts.append(f'{html.escape(t(lang, "link_word"))}: <code>/p/{p["id"]}</code>')

    if ALLOW_COMMENTS:
        comments = []
        for c in p["comments"]:
            c_stamp = c["created"].strftime("%d.%m.%Y %H:%M") if SHOW_TIMESTAMPS else ""
            copy_html = (
                f'<button type="button" class="copy-btn" data-target="c-{c["id"]}">{html.escape(t(lang, "copy_text"))}</button>'
                if SHOW_COPY_BUTTON else ""
            )
            comments.append(f"""
            <div class="comment">
              <div class="txt" id="c-{c["id"]}">{html.escape(c["text"])}</div>
              {f'<div class="meta">{html.escape(c_stamp)}</div>' if c_stamp else ''}
              {copy_html}
            </div>""")
        comments_html = "".join(comments) if comments else f"<p class='meta'>{html.escape(t(lang, 'no_comments'))}</p>"

        compose_html = f"""
        <div class="compose">
          <form method="post" action="/p/{p["id"]}/comment">
            <textarea name="text" data-counter="cmt-count" data-btn="cmt-btn"
                      data-max="{MAX_COMMENT_LEN}"
                      placeholder="{html.escape(t(lang, 'comment_placeholder', n=MAX_COMMENT_LEN), quote=True)}"
                      required></textarea>
            <div class="compose-row">
              <button type="submit" id="cmt-btn">{html.escape(t(lang, 'send'))}</button>
              <span class="counter" id="cmt-count">0 / {MAX_COMMENT_LEN}</span>
            </div>
            <input type="hidden" name="lang" value="{lang}">
          </form>
        </div>"""
        comments_block = (
            f'<h2>{html.escape(t(lang, "comments_heading"))} ({len(p["comments"])})</h2>'
            f'{comments_html}{compose_html}'
        )
    else:
        comments_block = ""

    body = f"""
    <p><a href="{with_lang('/', lang)}">&larr; {html.escape(t(lang, 'back_home'))}</a></p>
    <h1>{html.escape(t(lang, 'post_heading'))}</h1>

    <div class="post">
      <div class="txt" id="post-body">{html.escape(p["text"])}</div>
      <div class="meta">{" &middot; ".join(meta_parts)}</div>
      {copy_post_html}
    </div>

    {comments_block}
    """
    return page(f"{t(lang, 'post_heading')} {pid}", body, lang)


@app.post("/p/{pid}/comment")
def add_comment(pid: str, text: str = Form(...), lang: str = Form(LANG_DEFAULT)):
    lang = pick_lang(lang)
    if not ALLOW_COMMENTS:
        return RedirectResponse(with_lang(f"/p/{pid}", lang), status_code=303)

    p = posts.get(pid)
    if not p:
        return RedirectResponse(with_lang("/", lang), status_code=303)

    text = clean_text(text)[:MAX_COMMENT_LEN]
    if text:
        p["comments"].append({
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "created": datetime.now(),
        })
        if len(p["comments"]) > MAX_COMMENTS_PER_POST:
            del p["comments"][:len(p["comments"]) - MAX_COMMENTS_PER_POST]

    return RedirectResponse(with_lang(f"/p/{pid}", lang), status_code=303)


@app.get("/privacy", response_class=HTMLResponse)
def privacy(lang: str = LANG_DEFAULT):
    lang = pick_lang(lang)
    body = f"""
    <p><a href="{with_lang('/', lang)}">&larr; {html.escape(t(lang, 'back_home'))}</a></p>
    <h1>{html.escape(t(lang, 'privacy_title'))}</h1>

    <div class="post">
      <ol class="privacy">
        <li>
          <b>{html.escape(t(lang, 'privacy_p1_title', site=SITE_NAME))}</b><br>
          {html.escape(t(lang, 'privacy_p1_body'))}
        </li>
        <li>
          <b>{html.escape(t(lang, 'privacy_p2_title'))}</b><br>
          {html.escape(t(lang, 'privacy_p2_body'))}
        </li>
        <li>
          <b>{html.escape(t(lang, 'privacy_p3_title'))}</b><br>
          {html.escape(t(lang, 'privacy_p3_body', site=SITE_NAME))}
        </li>
      </ol>
    </div>
    """
    return page(t(lang, "privacy_title"), body, lang)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        access_log=False,
        proxy_headers=False,
        log_level="warning",
    )
