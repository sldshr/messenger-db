# main.py
# MiniSearch — простая поисковая система на FastAPI с хранением в оперативной памяти.
# Запуск:  pip install fastapi uvicorn httpx beautifulsoup4 lxml
#          uvicorn main:app --reload
# Открой:  http://127.0.0.1:8000/

import asyncio
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import BackgroundTasks, FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# --------------------------------------------------------------------------- #
#  Настройки
# --------------------------------------------------------------------------- #
USER_AGENT = "MiniSearchBot/1.0 (+http://localhost)"
TOKEN_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]{2,}")
STOP_WORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "on", "for",
    "with", "as", "at", "by", "be", "this", "that", "are", "was", "were",
    "from", "but", "not", "you", "your", "we", "our", "they", "their",
}


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOP_WORDS]


# --------------------------------------------------------------------------- #
#  Поисковый движок (инвертированный индекс + TF-IDF)
# --------------------------------------------------------------------------- #
class SearchEngine:
    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}                  # url -> документ
        self.index: dict[str, dict[str, int]] = defaultdict(dict)  # term -> {url: tf}
        self.doc_len: dict[str, int] = {}                 # url -> количество токенов
        self.started_at = datetime.now(timezone.utc)

    # ---- индексация ----
    def add(self, url: str, title: str, text: str, links: list[str]) -> bool:
        if url in self.pages:
            return False
        tokens = tokenize(text)
        if len(tokens) < 5:
            return False
        counts = Counter(tokens)
        self.pages[url] = {
            "url": url,
            "title": (title or url).strip()[:200],
            "text": text[:20000],
            "links": links[:300],
            "added": datetime.now(timezone.utc).isoformat(),
        }
        self.doc_len[url] = len(tokens)
        for term, cnt in counts.items():
            self.index[term][url] = cnt
        return True

    def clear(self) -> None:
        self.pages.clear()
        self.index.clear()
        self.doc_len.clear()

    # ---- поиск ----
    def search(self, query: str, limit: int = 10, offset: int = 0) -> list[dict]:
        terms = tokenize(query)
        if not terms or not self.pages:
            return []

        N = len(self.pages)
        scores: dict[str, float] = defaultdict(float)
        matched_terms: dict[str, set] = defaultdict(set)

        for term in set(terms):
            postings = self.index.get(term)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1.0 + N / df)
            for url, tf in postings.items():
                dl = self.doc_len.get(url) or 1
                scores[url] += (1.0 + math.log(tf)) / math.sqrt(dl) * idf
                matched_terms[url].add(term)

        # небольшая премия за совпадение во всех терминах запроса
        uniq_terms = set(terms)
        if len(uniq_terms) > 1:
            for url in scores:
                coverage = len(matched_terms[url]) / len(uniq_terms)
                scores[url] *= (0.5 + 0.5 * coverage)

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        ranked = ranked[offset: offset + limit]

        results = []
        for url, score in ranked:
            page = self.pages[url]
            results.append({
                "url": url,
                "title": page["title"],
                "snippet": make_snippet(page["text"], list(uniq_terms)),
                "score": round(score, 4),
                "terms": sorted(matched_terms[url]),
            })
        return results


def make_snippet(text: str, terms: list[str], length: int = 240) -> str:
    if not text:
        return ""
    low = text.lower()
    pos = -1
    for t in terms:
        p = low.find(t)
        if p != -1 and (pos == -1 or p < pos):
            pos = p
    if pos == -1:
        return text[:length] + ("…" if len(text) > length else "")
    start = max(0, pos - length // 3)
    end = min(len(text), start + length)
    snip = text[start:end].strip()
    if start > 0:
        snip = "…" + snip
    if end < len(text):
        snip = snip + "…"
    return snip


engine = SearchEngine()
crawl_state: dict = {"running": False, "started": None, "last_result": None, "log": []}


# --------------------------------------------------------------------------- #
#  Краулер
# --------------------------------------------------------------------------- #
async def fetch(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    ctype = r.headers.get("content-type", "").lower()
    if "html" not in ctype and "xml" not in ctype:
        return None
    return r.text


def parse_html(html: str, base_url: str) -> tuple[str, str, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    # главный контент, если есть
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))

    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urljoin(base_url, href).split("#")[0]
        p = urlparse(full)
        if p.scheme in ("http", "https") and p.netloc:
            links.append(full)

    # уникализируем, сохраняя порядок
    seen, uniq = set(), []
    for l in links:
        if l not in seen:
            seen.add(l)
            uniq.append(l)
    return title, text, uniq


async def crawl_site(start_url: str, max_pages: int = 20, max_depth: int = 2,
                     same_domain: bool = True) -> dict:
    if crawl_state["running"]:
        return {"error": "crawl already running"}
    crawl_state["running"] = True
    crawl_state["started"] = datetime.now(timezone.utc).isoformat()
    crawl_state["log"] = []

    start_domain = urlparse(start_url).netloc
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put((start_url, 0))
    seen = {start_url}
    added = 0

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=12.0,
            follow_redirects=True,
        ) as client:
            while not queue.empty() and added < max_pages:
                url, depth = await queue.get()
                if url in engine.pages:
                    continue

                html = await fetch(client, url)
                if html is None:
                    crawl_state["log"].append(f"skip {url}")
                    continue

                title, text, links = parse_html(html, url)
                if engine.add(url, title, text, links):
                    added += 1
                    crawl_state["log"].append(f"indexed {url}")
                else:
                    crawl_state["log"].append(f"skipped (empty) {url}")

                if depth < max_depth:
                    for link in links:
                        if link in seen:
                            continue
                        if same_domain and urlparse(link).netloc != start_domain:
                            continue
                        seen.add(link)
                        if len(seen) <= max_pages * 15:
                            await queue.put((link, depth + 1))
    finally:
        crawl_state["running"] = False
        crawl_state["last_result"] = {
            "added": added,
            "total_pages": len(engine.pages),
            "finished": datetime.now(timezone.utc).isoformat(),
        }

    return crawl_state["last_result"]


# --------------------------------------------------------------------------- #
#  FastAPI
# --------------------------------------------------------------------------- #
app = FastAPI(title="MiniSearch", version="1.0")


HTML_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    margin: 0; padding: 0; background: #fff; color: #202124;
  }}
  .wrap {{ max-width: 780px; margin: 0 auto; padding: 28px 20px 80px; }}
  .brand {{ font-size: 34px; font-weight: 700; letter-spacing: -1px; margin: 0 0 6px; }}
  .brand span:nth-child(1) {{ color: #4285f4; }}
  .brand span:nth-child(2) {{ color: #ea4335; }}
  .brand span:nth-child(3) {{ color: #fbbc05; }}
  .brand span:nth-child(4) {{ color: #4285f4; }}
  .brand span:nth-child(5) {{ color: #34a853; }}
  .brand span:nth-child(6) {{ color: #ea4335; }}
  .brand span:nth-child(7) {{ color: #fbbc05; }}
  .brand span:nth-child(8) {{ color: #4285f4; }}
  .brand span:nth-child(9) {{ color: #34a853; }}
  form.search {{ display: flex; gap: 8px; margin: 18px 0 10px; }}
  input[type=text] {{
    flex: 1; padding: 12px 16px; font-size: 16px; border: 1px solid #dfe1e5;
    border-radius: 24px; outline: none; transition: .15s;
  }}
  input[type=text]:focus {{ border-color: #4285f4; box-shadow: 0 1px 6px rgba(32,33,36,.18); }}
  button {{
    padding: 12px 22px; font-size: 15px; border: 1px solid #dfe1e5; background: #f8f9fa;
    border-radius: 24px; cursor: pointer;
  }}
  button:hover {{ background: #f1f3f4; box-shadow: 0 1px 2px rgba(0,0,0,.1); }}
  .stats {{ color: #70757a; font-size: 13px; margin: 8px 0 22px; }}
  .result {{ margin-bottom: 26px; }}
  .result a {{ color: #1a0dab; font-size: 18px; text-decoration: none; }}
  .result a:hover {{ text-decoration: underline; }}
  .result .url {{ color: #5f6368; font-size: 12px; margin-top: 2px; word-break: break-all; }}
  .result .snip {{ color: #4d5156; font-size: 14px; line-height: 1.5; margin-top: 6px; }}
  mark {{ background: #fff3b0; color: inherit; padding: 0 2px; border-radius: 2px; }}
  .empty {{ color: #5f6368; font-size: 15px; padding: 30px 0; }}
  .panel {{
    margin-top: 40px; padding: 16px; border: 1px solid #e8eaed;
    border-radius: 10px; background: #fafafa; font-size: 13px; color: #5f6368;
  }}
  .panel h3 {{ margin: 0 0 8px; font-size: 14px; color: #202124; }}
  .panel code {{ background: #eef1f3; padding: 1px 5px; border-radius: 4px; }}
  .panel form {{ display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }}
  .panel input {{ flex: 1; min-width: 220px; padding: 8px 12px; border-radius: 8px; border: 1px solid #dfe1e5; }}
  .panel button {{ padding: 8px 14px; border-radius: 8px; }}
  .nav {{ margin-bottom: 12px; }}
  .nav a {{ color: #1a73e8; text-decoration: none; font-size: 14px; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="nav"><a href="/">← На главную</a></div>
  <h1 class="brand"><span>M</span><span>i</span><span>n</span><span>i</span><span>S</span><span>e</span><span>a</span><span>r</span><span>ch</span></h1>

  <form class="search" action="/" method="get">
    <input type="text" name="q" value="{q_esc}" placeholder="Поиск по проиндексированным страницам…" autofocus>
    <button type="submit">Найти</button>
  </form>

  <div class="stats">{stats}</div>

  {results_html}

  <div class="panel">
    <h3>Проиндексировать сайт</h3>
    <form action="/crawl" method="get">
      <input type="text" name="url" placeholder="https://example.com" required>
      <input type="number" name="max_pages" value="15" min="1" max="200" style="max-width:100px">
      <button type="submit">Сканировать</button>
    </form>
    <div style="margin-top:10px">
      API: <code>GET /api/search?q=…</code> · <code>POST /api/crawl</code> ·
      <code>GET /api/stats</code> · <code>POST /api/clear</code>
    </div>
  </div>
</div>
</body>
</html>
"""


def highlight(text: str, terms: list[str]) -> str:
    out = escape(text)
    for t in sorted({t for t in terms if len(t) >= 2}, key=len, reverse=True):
        out = re.sub(rf"\b({re.escape(t)}[а-яa-z]*)", r"<mark>\1</mark>",
                     out, flags=re.IGNORECASE)
    return out


def render_page(q: str, results: list[dict], extra_stats: str = "") -> str:
    if q and results:
        parts = []
        for r in results:
            parts.append(f"""
            <div class="result">
              <a href="{escape(r['url'])}" target="_blank" rel="noopener">{escape(r['title'])}</a>
              <div class="url">{escape(r['url'])}</div>
              <div class="snip">{highlight(r['snippet'], r['terms'])}</div>
            </div>""")
        results_html = "".join(parts)
    elif q:
        results_html = '<div class="empty">Ничего не найдено. Попробуйте другой запрос или проиндексируйте сайт ниже.</div>'
    else:
        results_html = ""

    stats = f"Страниц в индексе: {len(engine.pages)} · Терминов: {len(engine.index)}"
    if extra_stats:
        stats += f" · {extra_stats}"
    if q:
        stats = f"Найдено {len(results)} результатов · " + stats

    return HTML_PAGE.format(
        title=escape(q) if q else "MiniSearch",
        q_esc=escape(q),
        stats=escape(stats),
        results_html=results_html,
    )


# ---------------- HTML-роуты ----------------
@app.get("/", response_class=HTMLResponse)
async def index(q: str = Query("", max_length=200)):
    results = engine.search(q, limit=10) if q.strip() else []
    extra = ""
    if crawl_state["running"]:
        extra = "идёт сканирование…"
    return HTMLResponse(render_page(q.strip(), results, extra))


@app.get("/crawl")
async def crawl_html(background: BackgroundTasks,
                     url: str = Query(..., min_length=4),
                     max_pages: int = Query(15, ge=1, le=200),
                     max_depth: int = Query(2, ge=0, le=4)):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not crawl_state["running"]:
        background.add_task(crawl_site, url, max_pages, max_depth, True)
    return RedirectResponse("/", status_code=303)


# ---------------- JSON API ----------------
@app.get("/api/search")
async def api_search(q: str = Query(..., min_length=1),
                     limit: int = Query(10, ge=1, le=50),
                     offset: int = Query(0, ge=0)):
    results = engine.search(q, limit=limit, offset=offset)
    return {
        "query": q,
        "count": len(results),
        "results": results,
    }


@app.post("/api/crawl")
async def api_crawl(background: BackgroundTasks,
                    url: str = Query(..., min_length=4),
                    max_pages: int = Query(20, ge=1, le=500),
                    max_depth: int = Query(2, ge=0, le=5),
                    same_domain: bool = True):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if crawl_state["running"]:
        return JSONResponse({"status": "busy", "message": "уже идёт сканирование"},
                            status_code=409)
    background.add_task(crawl_site, url, max_pages, max_depth, same_domain)
    return {"status": "started", "url": url, "max_pages": max_pages}


@app.get("/api/stats")
async def api_stats():
    return {
        "pages": len(engine.pages),
        "terms": len(engine.index),
        "started_at": engine.started_at.isoformat(),
        "crawl": {
            "running": crawl_state["running"],
            "started": crawl_state["started"],
            "last_result": crawl_state["last_result"],
        },
        "top_terms": sorted(
            ((t, len(p)) for t, p in engine.index.items()),
            key=lambda x: -x[1],
        )[:20],
        "urls": list(engine.pages.keys())[:50],
    }


@app.post("/api/clear")
async def api_clear():
    engine.clear()
    crawl_state["log"] = []
    crawl_state["last_result"] = None
    return {"status": "cleared"}


@app.get("/api/page")
async def api_page(url: str):
    page = engine.pages.get(url)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    return page


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
