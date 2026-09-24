# main.py
# Запуск:  pip install fastapi uvicorn
#          python main.py
# Открыть: http://localhost:8000/input   (пишем сообщения)
#          http://localhost:8000/output  (озвучивается голосом браузера)

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import asyncio

app = FastAPI(title="Chat TTS")

# ---------- Хранилище сообщений в памяти ----------
_messages: list[dict] = []
_next_id = 1
_lock = asyncio.Lock()


class MessageIn(BaseModel):
    text: str


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html><body style="font-family:system-ui;background:#111;color:#eee;padding:40px">
      <h1>Chat TTS</h1>
      <p><a style="color:#4caf50" href="/input">→ /input</a> — писать сообщения</p>
      <p><a style="color:#4caf50" href="/output">→ /output</a> — слушать (открой в отдельной вкладке)</p>
    </body></html>
    """


@app.post("/api/send")
async def send(m: MessageIn):
    global _next_id
    text = m.text.strip()
    if not text:
        return {"ok": False}
    async with _lock:
        item = {"id": _next_id, "text": text}
        _messages.append(item)
        _next_id += 1
    return item


@app.get("/api/messages")
async def get_messages(since: int = 0):
    return [m for m in _messages if m["id"] > since]


# ---------- Страница /input ----------
INPUT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Input — Chat</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; background:#111; color:#eee;
         margin:0; height:100vh; display:flex; flex-direction:column; }
  header { padding: 12px 20px; background:#1a1a1a; border-bottom:1px solid #333;
           font-weight:600; display:flex; justify-content:space-between; align-items:center; }
  header a { color:#4caf50; text-decoration:none; font-weight:400; font-size:14px; }
  #chat { flex:1; overflow-y:auto; padding: 16px 20px; }
  .msg { padding: 10px 14px; margin: 6px 0; background:#1e1e1e; border-radius:10px;
         border-left: 3px solid #4caf50; max-width: 75%; word-wrap:break-word; }
  form { display:flex; gap:8px; padding: 12px; background:#1a1a1a; border-top:1px solid #333; }
  input { flex:1; padding: 12px; font-size: 15px; border-radius:8px;
          border:1px solid #333; background:#0d0d0d; color:#eee; outline:none; }
  input:focus { border-color:#4caf50; }
  button { padding: 12px 20px; font-size: 15px; border:none; border-radius:8px;
           background:#4caf50; color:#fff; cursor:pointer; }
  button:hover { background:#45a049; }
</style>
</head>
<body>
<header>
  <span>💬 Input</span>
  <a href="/output" target="_blank">open /output →</a>
</header>
<div id="chat"></div>
<form id="form">
  <input id="text" autofocus autocomplete="off"
         placeholder="Напиши сообщение на русском или English...">
  <button type="submit">Send</button>
</form>
<script>
const chat = document.getElementById('chat');
const form = document.getElementById('form');
const text = document.getElementById('text');
let lastId = 0;

function addMsg(m){
  const d = document.createElement('div');
  d.className = 'msg';
  d.textContent = m.text;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
}

form.onsubmit = async (e) => {
  e.preventDefault();
  const t = text.value.trim();
  if (!t) return;
  text.value = '';
  const r = await fetch('/api/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: t})
  });
  if (r.ok){
    const m = await r.json();
    if (m.id) { lastId = Math.max(lastId, m.id); addMsg(m); }
  }
};

(async () => {
  const r = await fetch('/api/messages?since=0');
  const arr = await r.json();
  arr.forEach(m => { lastId = Math.max(lastId, m.id); addMsg(m); });
})();
</script>
</body>
</html>
"""


# ---------- Страница /output ----------
OUTPUT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Output — Voice</title>
<style>
  body { font-family: system-ui, sans-serif; background:#111; color:#eee;
         margin:0; padding:20px; }
  h1 { font-size: 18px; margin:0 0 8px; }
  #status { color:#888; font-size: 13px; margin-bottom: 12px; }
  #log { max-width: 800px; }
  .msg { padding: 10px 14px; margin: 6px 0; background:#1e1e1e; border-radius:10px;
         border-left: 3px solid #4caf50; word-wrap:break-word; transition: all .2s; }
  .msg.speaking { background:#2a3f2a; border-left-color:#ffeb3b; }
  #enable { padding: 10px 18px; font-size: 15px; border:none; border-radius:8px;
            background:#4caf50; color:#fff; cursor:pointer; margin-bottom:12px; }
  #enable:hover { background:#45a049; }
</style>
</head>
<body>
<h1>🔊 Output — голос браузера</h1>
<div id="status">Инициализация...</div>
<button id="enable">▶ Включить озвучку</button>
<div id="log"></div>

<script>
const log = document.getElementById('log');
const statusEl = document.getElementById('status');
let lastId = 0;
let queue = [];
let busy = false;
let enabled = false;

// Определяем язык по кириллице
function detectLang(t){
  return /[\\u0400-\\u04FF]/.test(t) ? 'ru-RU' : 'en-US';
}

// Подбираем голос под язык
function pickVoice(lang){
  const voices = speechSynthesis.getVoices();
  return voices.find(v => v.lang === lang)
      || voices.find(v => v.lang.replace('_','-') === lang)
      || voices.find(v => v.lang.toLowerCase().startsWith(lang.slice(0,2).toLowerCase()));
}

function next(){
  if (busy) return;
  const item = queue.shift();
  if (!item){
    statusEl.textContent = 'Ожидание сообщений...';
    return;
  }
  busy = true;
  statusEl.textContent = '🔈 ' + item.text;
  item.el.classList.add('speaking');

  const u = new SpeechSynthesisUtterance(item.text);
  const lang = detectLang(item.text);
  u.lang = lang;
  const v = pickVoice(lang);
  if (v) u.voice = v;
  u.rate = 1.0;
  u.pitch = 1.0;

  const done = () => {
    item.el.classList.remove('speaking');
    busy = false;
    next();
  };
  u.onend = done;
  u.onerror = done;
  speechSynthesis.speak(u);
}

function enqueue(text, el){
  queue.push({ text, el });
  next();
}

async function poll(){
  try {
    const r = await fetch('/api/messages?since=' + lastId);
    const arr = await r.json();
    for (const m of arr){
      lastId = Math.max(lastId, m.id);
      const d = document.createElement('div');
      d.className = 'msg';
      d.textContent = m.text;
      log.appendChild(d);
      if (enabled) enqueue(m.text, d);
    }
  } catch (e) { /* ignore */ }
  setTimeout(poll, 700);
}

// "Разогреваем" голоса
speechSynthesis.getVoices();
speechSynthesis.onvoiceschanged = () => speechSynthesis.getVoices();

document.getElementById('enable').onclick = () => {
  enabled = true;
  // «тихое» воспроизведение, чтобы разблокировать аудио в браузере
  const warm = new SpeechSynthesisUtterance(' ');
  warm.volume = 0;
  speechSynthesis.speak(warm);
  document.getElementById('enable').style.display = 'none';
  statusEl.textContent = 'Ожидание сообщений...';
};

poll();
</script>
</body>
</html>
"""


@app.get("/input", response_class=HTMLResponse)
async def input_page():
    return INPUT_HTML


@app.get("/output", response_class=HTMLResponse)
async def output_page():
    return OUTPUT_HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
