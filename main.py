"""
Мини-Википедия на FastAPI. Хранилище — в оперативке (dict).
Запуск:  uvicorn main:app --reload
Открыть: http://127.0.0.1:8000
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import DictLoader, Environment, select_autoescape

# ---------- Markdown ----------
try:
    import markdown as _md

    def render_md(text: str) -> str:
        return _md.markdown(text, extensions=["fenced_code", "tables", "nl2br"])
except ImportError:
    import html as _html

    def render_md(text: str) -> str:
        return "<pre>" + _html.escape(text) + "</pre>"


# ---------- Хранилище в памяти ----------
# slug -> {"slug", "title", "content", "created_at", "updated_at"}
ARTICLES: Dict[str, dict] = {}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------- Slug ----------
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "article"


def unique_slug(desired: str, ignore: Optional[str] = None) -> str:
    if desired not in ARTICLES or desired == ignore:
        return desired
    n = 1
    base = desired
    while f"{base}-{n}" in ARTICLES and f"{base}-{n}" != ignore:
        n += 1
    return f"{base}-{n}"


def preview_of(content: str, limit: int = 160) -> str:
    text = re.sub(r"```.*?```", " ", content, flags=re.S)
    text = re.sub(r"[#*_>`\[\]()!]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------- Шаблоны ----------
TEMPLATES = {
    "base.html": """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}MiniWiki{% endblock %}</title>
<style>
  :root { --link:#3366cc; --border:#a2a9b1; --bg:#f6f6f6; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: -apple-system, "Segoe UI", Arial, sans-serif;
         color:#202122; background:#fff; }
  header { border-bottom:1px solid var(--border); padding:10px 20px;
           display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  header .logo { font-family: Georgia, serif; font-size:22px; font-weight:bold; }
  header .logo a { color:#202122; text-decoration:none; }
  header form.search { display:flex; gap:6px; margin-left:auto; }
  header input[type=search] { padding:6px 10px; border:1px solid var(--border);
                              border-radius:2px; width:220px; }
  .container { max-width:900px; margin:0 auto; padding:24px 20px 60px; }
  h1 { font-family: Georgia, serif; font-weight:normal;
       border-bottom:1px solid var(--border); padding-bottom:8px; }
  h2 { font-family: Georgia, serif; font-weight:normal;
       border-bottom:1px solid var(--border); padding-bottom:4px; margin-top:28px; }
  a { color: var(--link); }
  .btn { display:inline-block; padding:6px 12px; border:1px solid var(--border);
         background:var(--bg); border-radius:2px; text-decoration:none;
         color:#202122; cursor:pointer; font-size:14px; font-family:inherit; }
  .btn:hover { background:#eaecf0; }
  .btn.primary { background:#36c; color:#fff; border-color:#36c; }
  .btn.primary:hover { background:#2a4b8d; }
  .btn.danger { color:#b32424; }
  input[type=text], textarea { width:100%; padding:8px 10px;
        border:1px solid var(--border); border-radius:2px;
        font-family:inherit; font-size:15px; }
  textarea { min-height:320px; font-family: ui-monospace, Menlo, monospace;
             line-height:1.5; }
  label { display:block; margin:14px 0 6px; font-weight:bold; }
  .article-body { font-family: Georgia, serif; font-size:16px; line-height:1.7; }
  .article-body pre { background:var(--bg); padding:12px; overflow:auto; font-size:14px;
                      border:1px solid #eaecf0; border-radius:2px; }
  .article-body code { background:var(--bg); padding:2px 4px; border-radius:2px; }
  .article-body pre code { background:transparent; padding:0; }
  .article-body blockquote { border-left:4px solid #eaecf0; margin:1em 0;
                             padding:0 1em; color:#54595d; }
  .article-body table { border-collapse:collapse; }
  .article-body th, .article-body td { border:1px solid var(--border); padding:6px 10px; }
  .meta { color:#72777d; font-size:13px; margin-top:32px;
          border-top:1px solid var(--border); padding-top:8px; }
  ul.articles { list-style:none; padding:0; }
  ul.articles li { padding:10px 0; border-bottom:1px solid #eaecf0; }
  ul.articles .desc { color:#54595d; font-size:14px; margin-top:4px; }
  .actions { display:flex; gap:8px; margin:16px 0; }
  .actions form { margin:0; }
  .empty { color:#72777d; font-style:italic; }
  .error { background:#fee7e6; color:#b32424; padding:8px 12px;
           border:1px solid #f8c3c0; border-radius:2px; margin:12px 0; }
  .hint { color:#72777d; font-size:13px; margin-top:4px; }
  .note { background:#eaf3ff; border:1px solid #c8dcf5; color:#2a4b8d;
          padding:8px 12px; border-radius:2px; margin:12px 0; font-size:14px; }
</style>
</head>
<body>
<header>
  <div class="logo"><a href="/">📖 MiniWiki</a></div>
  <form class="search" action="/search" method="get">
    <input type="search" name="q" placeholder="Поиск…" value="{{ q or '' }}">
    <button class="btn" type="submit">Найти</button>
  </form>
  <a class="btn primary" href="/new">+ Новая статья</a>
</header>
<div class="container">
{% block content %}{% endblock %}
</div>
</body>
</html>
""",

    "index.html": """
{% extends "base.html" %}
{% block content %}
<h1>Все статьи</h1>
<div class="note">
  ⚠️ Статьи хранятся <b>только в оперативной памяти</b>.
  После перезапуска сервера они исчезнут.
</div>
{% if articles %}
<ul class="articles">
{% for a in articles %}
  <li>
    <a href="/wiki/{{ a.slug }}"><strong>{{ a.title }}</strong></a>
    <div class="desc">{{ a.preview }}</div>
  </li>
{% endfor %}
</ul>
{% else %}
<p class="empty">Пока нет статей. <a href="/new">Создайте первую!</a></p>
{% endif %}
{% endblock %}
""",

    "search.html": """
{% extends "base.html" %}
{% block title %}Поиск: {{ q }} — MiniWiki{% endblock %}
{% block content %}
<h1>Поиск: «{{ q }}»</h1>
{% if articles %}
  <p>Найдено статей: {{ articles|length }}</p>
  <ul class="articles">
  {% for a in articles %}
    <li>
      <a href="/wiki/{{ a.slug }}"><strong>{{ a.title }}</strong></a>
      <div class="desc">{{ a.preview }}</div>
    </li>
  {% endfor %}
  </ul>
{% else %}
  <p class="empty">Ничего не найдено.</p>
{% endif %}
{% endblock %}
""",

    "article.html": """
{% extends "base.html" %}
{% block title %}{{ article.title }} — MiniWiki{% endblock %}
{% block content %}
<h1>{{ article.title }}</h1>
<div class="actions">
  <a class="btn" href="/edit/{{ article.slug }}">✏️ Редактировать</a>
  <form method="post" action="/delete/{{ article.slug }}"
        onsubmit="return confirm('Удалить статью «{{ article.title }}»?')">
    <button class="btn danger" type="submit">🗑 Удалить</button>
  </form>
</div>
<div class="article-body">{{ content|safe }}</div>
<div class="meta">
  Создано: {{ article.created_at }} · Обновлено: {{ article.updated_at }} ·
  slug: <code>{{ article.slug }}</code>
</div>
{% endblock %}
""",

    "form.html": """
{% extends "base.html" %}
{% block title %}{{ heading }} — MiniWiki{% endblock %}
{% block content %}
<h1>{{ heading }}</h1>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
<form method="post" action="{{ action }}">
  <label for="title">Заголовок</label>
  <input type="text" id="title" name="title" value="{{ title }}" required autofocus>

  <label for="slug">Slug (URL)</label>
  <input type="text" id="slug" name="slug" value="{{ slug }}"
         placeholder="оставьте пустым — сгенерируется автоматически">
  <div class="hint">Например: <code>python-fastapi</code></div>

  <label for="content">Содержимое (Markdown)</label>
  <textarea id="content" name="content">{{ content }}</textarea>
  <div class="hint">
    Поддерживается Markdown: <code># заголовок</code>, <code>**жирный**</code>,
    <code>*курсив*</code>, <code>[ссылка](url)</code>, <code>```код```</code>,
    списки, таблицы.
  </div>

  <div class="actions" style="margin-top:18px">
    <button class="btn primary" type="submit">💾 Сохранить</button>
    <a class="btn" href="{{ cancel_url }}">Отмена</a>
  </div>
</form>
{% endblock %}
""",

    "404.html": """
{% extends "base.html" %}
{% block title %}Не найдено — MiniWiki{% endblock %}
{% block content %}
<h1>404 — страница не найдена</h1>
<p class="empty">{{ message }}</p>
<p><a href="/">← На главную</a></p>
{% endblock %}
""",
}

env = Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(["html"]))


def render(name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return HTMLResponse(env.get_template(name).render(**ctx), status_code=status_code)


# ---------- Приложение ----------
app = FastAPI(title="MiniWiki")


# ---------- Роуты ----------
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    articles = sorted(
        (
            {
                "slug": a["slug"],
                "title": a["title"],
                "preview": preview_of(a["content"]),
            }
            for a in ARTICLES.values()
        ),
        key=lambda x: x["title"].lower(),
    )
    return render("index.html", articles=articles)


@app.get("/search", response_class=HTMLResponse)
def search(q: str = "") -> HTMLResponse:
    q = q.strip()
    if not q:
        return RedirectResponse("/", status_code=303)
    needle = q.lower()
    found = [
        {
            "slug": a["slug"],
            "title": a["title"],
            "preview": preview_of(a["content"]),
        }
        for a in ARTICLES.values()
        if needle in a["title"].lower() or needle in a["content"].lower()
    ]
    found.sort(key=lambda x: x["title"].lower())
    return render("search.html", q=q, articles=found)


@app.get("/wiki/{slug}", response_class=HTMLResponse)
def view_article(slug: str) -> HTMLResponse:
    article = ARTICLES.get(slug)
    if not article:
        raise HTTPException(status_code=404, detail=f"Статья «{slug}» не существует.")
    return render(
        "article.html",
        article=article,
        content=render_md(article["content"]),
    )


# ---- Создание ----
@app.get("/new", response_class=HTMLResponse)
def new_article_form() -> HTMLResponse:
    return render(
        "form.html",
        heading="Новая статья",
        action="/new",
        cancel_url="/",
        title="",
        slug="",
        content="",
        error=None,
    )


@app.post("/new")
def create_article(
    title: str = Form(...),
    content: str = Form(""),
    slug: str = Form(""),
):
    title = title.strip()
    if not title:
        return render(
            "form.html",
            heading="Новая статья",
            action="/new",
            cancel_url="/",
            title=title,
            slug=slug,
            content=content,
            error="Заголовок не может быть пустым.",
            status_code=400,
        )

    desired = slugify(slug.strip() or title)
    final_slug = unique_slug(desired)
    ts = now_str()
    ARTICLES[final_slug] = {
        "slug": final_slug,
        "title": title,
        "content": content,
        "created_at": ts,
        "updated_at": ts,
    }
    return RedirectResponse(f"/wiki/{final_slug}", status_code=303)


# ---- Редактирование ----
@app.get("/edit/{slug}", response_class=HTMLResponse)
def edit_article_form(slug: str) -> HTMLResponse:
    article = ARTICLES.get(slug)
    if not article:
        raise HTTPException(status_code=404, detail=f"Статья «{slug}» не найдена.")
    return render(
        "form.html",
        heading=f"Редактирование: {article['title']}",
        action=f"/edit/{slug}",
        cancel_url=f"/wiki/{slug}",
        title=article["title"],
        slug=article["slug"],
        content=article["content"],
        error=None,
    )


@app.post("/edit/{slug}")
def update_article(
    slug: str,
    title: str = Form(...),
    content: str = Form(""),
    slug_new: str = Form("", alias="slug"),
):
    if slug not in ARTICLES:
        raise HTTPException(status_code=404, detail="Статья не найдена.")

    title = title.strip()
    if not title:
        return render(
            "form.html",
            heading=f"Редактирование: {slug}",
            action=f"/edit/{slug}",
            cancel_url=f"/wiki/{slug}",
            title=title,
            slug=slug_new,
            content=content,
            error="Заголовок не может быть пустым.",
            status_code=400,
        )

    desired = slugify(slug_new.strip() or title)
    final_slug = unique_slug(desired, ignore=slug)

    article = ARTICLES.pop(slug)
    article["title"] = title
    article["content"] = content
    article["slug"] = final_slug
    article["updated_at"] = now_str()
    ARTICLES[final_slug] = article

    return RedirectResponse(f"/wiki/{final_slug}", status_code=303)


# ---- Удаление ----
@app.post("/delete/{slug}")
def delete_article(slug: str):
    if slug not in ARTICLES:
        raise HTTPException(status_code=404, detail="Статья не найдена.")
    del ARTICLES[slug]
    return RedirectResponse("/", status_code=303)


# ---- Обработчик 404 ----
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    msg = exc.detail if isinstance(exc.detail, str) else "Страница не найдена."
    return render("404.html", message=msg, status_code=404)


# ---------- Запуск ----------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
