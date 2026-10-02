"""UI-клон Mastodon на FastAPI (один файл).

Только интерфейс: без ActivityPub, БД и реальных сетевых запросов.
Запуск:
    pip install fastapi uvicorn
    python main.py
    http://127.0.0.1:8000
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Mastodon UI")


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mastodon</title>

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200" rel="stylesheet">

<style>
:root {
  /* Material Design 3 — dark scheme */
  --surface:              #141218;
  --surface-low:          #1D1B20;
  --surface-container:    #211F26;
  --surface-high:         #2B2930;
  --surface-highest:      #36343B;
  --on-surface:           #E6E0E9;
  --on-surface-variant:   #CAC4D0;
  --outline:              #938F99;
  --outline-variant:      #49454F;

  --primary:              #D0BCFF;
  --on-primary:           #381E72;
  --primary-container:    #4F378B;
  --on-primary-container: #EADDFF;

  --secondary-container:    #4A4458;
  --on-secondary-container: #E8DEF8;

  --tertiary:               #EFB8C8;
  --on-tertiary:            #492532;

  --error:          #F2B8B5;
  --on-error:       #601410;

  --inverse-surface:    #E6E0E9;
  --inverse-on-surface: #322F35;

  --fav:   #FDD663;
  --boost: #81C995;

  --shape-xs: 8px;
  --shape-sm: 12px;
  --shape-md: 16px;
  --shape-lg: 20px;
  --shape-xl: 28px;

  --ease: cubic-bezier(0.2, 0, 0, 1);
}

* { box-sizing: border-box; }

html, body {
  height: 100%;
  margin: 0;
  overflow: hidden;
}

body {
  background: var(--surface);
  color: var(--on-surface);
  font-family: 'Roboto', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 20px;
  letter-spacing: 0.25px;
  -webkit-font-smoothing: antialiased;
}

button, input, textarea {
  font-family: inherit;
  font-size: inherit;
  letter-spacing: inherit;
}

.material-symbols-outlined {
  font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
  font-size: 22px;
  line-height: 1;
  user-select: none;
  display: inline-block;
}
.fill { font-variation-settings: 'FILL' 1, 'wght' 400, 'GRAD' 0, 'opsz' 24; }

/* ═══════════ Общий каркас ═══════════ */
.app {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr) 320px;
  gap: 12px;
  max-width: 1320px;
  margin: 0 auto;
  height: 100vh;
  padding: 12px;
}

.sidebar-left,
.sidebar-right {
  height: calc(100vh - 24px);
  overflow: hidden;                 /* меню не скроллятся */
  display: flex;
  flex-direction: column;
}

.sidebar-left { padding: 4px 0; gap: 2px; }
.sidebar-right { gap: 12px; }

/* ═══════════ Левая колонка ═══════════ */
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px 16px;
  font-size: 20px;
  font-weight: 500;
  letter-spacing: 0;
}
.brand-mark {
  width: 36px; height: 36px;
  border-radius: var(--shape-sm);
  background: linear-gradient(140deg, #6364ff, #A78BFA);
  display: grid;
  place-items: center;
  color: #fff;
  font-weight: 700;
  font-size: 20px;
  font-style: italic;
  letter-spacing: -1px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 48px;
  padding: 0 16px;
  border: 0;
  border-radius: 24px;              /* MD3 pill */
  background: transparent;
  color: var(--on-surface-variant);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  text-align: left;
  width: 100%;
  transition: background .15s var(--ease), color .15s var(--ease);
}
.nav-item:hover { background: rgba(230, 224, 233, .08); }
.nav-item.active {
  background: var(--secondary-container);
  color: var(--on-secondary-container);
}
.nav-item .badge {
  margin-left: auto;
  background: var(--primary);
  color: var(--on-primary);
  font-size: 11px;
  font-weight: 500;
  min-width: 20px;
  height: 20px;
  padding: 0 6px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  line-height: 1;
}

.sidebar-cta {
  margin: 16px 0 12px;
  height: 44px;
  border: 0;
  border-radius: 22px;
  background: var(--primary);
  color: var(--on-primary);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: box-shadow .15s var(--ease), background .15s var(--ease);
}
.sidebar-cta:hover {
  box-shadow: 0 1px 3px rgba(0,0,0,.35), 0 1px 2px rgba(0,0,0,.2);
}

.user-tile {
  margin-top: auto;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px;
  border: 0;
  background: transparent;
  border-radius: 28px;
  cursor: pointer;
  width: 100%;
  color: inherit;
  text-align: left;
  transition: background .15s var(--ease);
}
.user-tile:hover { background: rgba(230, 224, 233, .08); }
.user-tile .meta { min-width: 0; flex: 1; }
.user-tile .name { font-weight: 500; font-size: 14px; }
.user-tile .handle {
  color: var(--on-surface-variant);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ═══════════ Аватар ═══════════ */
.avatar {
  width: 44px; height: 44px;
  flex: 0 0 44px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  color: #fff;
  font-weight: 500;
  font-size: 16px;
  letter-spacing: 0;
  user-select: none;
  background: linear-gradient(135deg, var(--c1), var(--c2));
}
.avatar.sm { width: 40px; height: 40px; flex-basis: 40px; font-size: 14px; }
.avatar.xs { width: 36px; height: 36px; flex-basis: 36px; font-size: 13px; }

/* ═══════════ Центральная колонка ═══════════ */
.feed {
  height: calc(100vh - 24px);
  display: grid;
  grid-template-rows: auto auto 1fr;   /* header / compose / feed */
  background: var(--surface-low);
  border-radius: var(--shape-md);
  overflow: hidden;                    /* скроллится только .timeline */
}

.feed-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 8px 0 20px;
  height: 64px;
  border-bottom: 1px solid var(--outline-variant);
}
.feed-title {
  font-size: 20px;
  font-weight: 500;
  letter-spacing: 0;
}
.icon-btn {
  width: 40px; height: 40px;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--on-surface-variant);
  display: grid;
  place-items: center;
  cursor: pointer;
  transition: background .15s var(--ease);
}
.icon-btn:hover { background: rgba(230, 224, 233, .08); }

/* Композер */
.composer {
  padding: 16px 20px;
  border-bottom: 1px solid var(--outline-variant);
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.composer-body { flex: 1; min-width: 0; }
.composer textarea {
  width: 100%;
  min-height: 52px;
  max-height: 220px;
  resize: none;
  padding: 12px 16px;
  border-radius: var(--shape-sm);
  border: 1px solid var(--outline);
  background: transparent;
  color: var(--on-surface);
  outline: none;
  transition: border-color .15s var(--ease);
}
.composer textarea::placeholder { color: var(--on-surface-variant); }
.composer textarea:focus { border-color: var(--primary); }

.composer-tools {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
}
.tool {
  width: 36px; height: 36px;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--primary);
  display: grid;
  place-items: center;
  cursor: pointer;
  transition: background .15s var(--ease);
}
.tool:hover { background: rgba(208, 188, 255, .12); }
.tool .material-symbols-outlined { font-size: 20px; }

.counter {
  margin-left: auto;
  font-size: 12px;
  color: var(--on-surface-variant);
  font-variant-numeric: tabular-nums;
  padding-right: 8px;
}
.counter.warn { color: var(--fav); }
.counter.over { color: var(--error); font-weight: 500; }

.btn-filled {
  height: 40px;
  padding: 0 24px;
  border: 0;
  border-radius: 20px;
  background: var(--primary);
  color: var(--on-primary);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: box-shadow .15s var(--ease), background .15s var(--ease);
}
.btn-filled:hover:not(:disabled) {
  box-shadow: 0 1px 3px rgba(0,0,0,.35), 0 1px 2px rgba(0,0,0,.2);
}
.btn-filled:disabled {
  background: rgba(230, 224, 233, .12);
  color: rgba(230, 224, 233, .38);
  cursor: not-allowed;
}

/* Лента — единственная скроллящаяся область */
.timeline {
  overflow-y: auto;
  overflow-x: hidden;
  scrollbar-width: thin;
  scrollbar-color: var(--outline-variant) transparent;
}
.timeline::-webkit-scrollbar { width: 6px; }
.timeline::-webkit-scrollbar-thumb {
  background: var(--outline-variant);
  border-radius: 3px;
}
.timeline::-webkit-scrollbar-track { background: transparent; }

/* Пост */
.post {
  display: flex;
  gap: 12px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--outline-variant);
  transition: background .15s var(--ease);
}
.post:hover { background: rgba(230, 224, 233, .03); }

.post-body { flex: 1; min-width: 0; }
.post-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  column-gap: 6px;
  row-gap: 2px;
}
.post-name { font-weight: 500; color: var(--on-surface); cursor: pointer; }
.post-name:hover { text-decoration: underline; }
.post-handle { color: var(--on-surface-variant); font-size: 13px; }
.post-dot   { color: var(--on-surface-variant); font-size: 13px; }
.post-time  { color: var(--on-surface-variant); font-size: 13px; }

.post-content {
  margin: 4px 0 10px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--on-surface);
}
.post-content .tag { color: var(--primary); cursor: pointer; }
.post-content .tag:hover { text-decoration: underline; }
.post-content .mention { color: var(--primary); cursor: pointer; }

.post-actions {
  display: flex;
  gap: 0;
  margin-left: -8px;
}
.action {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 10px;
  border: 0;
  border-radius: 16px;
  background: transparent;
  color: var(--on-surface-variant);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
  transition: background .15s var(--ease), color .15s var(--ease);
}
.action .material-symbols-outlined { font-size: 18px; }
.action:hover { background: rgba(208, 188, 255, .10); }
.action[data-act="reply"]:hover   { color: var(--primary); }
.action[data-act="boost"]:hover   { color: var(--boost); background: rgba(129, 201, 149, .12); }
.action[data-act="fav"]:hover     { color: var(--fav);   background: rgba(253, 214, 99, .12); }
.action[data-act="bookmark"]:hover{ color: var(--primary); }

.action.active[data-act="boost"]    { color: var(--boost); }
.action.active[data-act="fav"]      { color: var(--fav); }
.action.active[data-act="bookmark"] { color: var(--primary); }

.empty-state {
  padding: 64px 24px;
  text-align: center;
  color: var(--on-surface-variant);
  font-size: 14px;
}

/* ═══════════ Правая колонка ═══════════ */
.widget {
  background: var(--surface-container);
  border-radius: var(--shape-md);
  overflow: hidden;
  flex: 0 0 auto;
}
.widget-head {
  padding: 14px 16px;
  font-size: 15px;
  font-weight: 500;
  letter-spacing: 0;
}
.search-wrap { padding: 12px; }
.search-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 44px;
  padding: 0 14px;
  border-radius: 22px;
  background: var(--surface-high);
}
.search-bar .material-symbols-outlined { color: var(--on-surface-variant); font-size: 20px; }
.search-bar input {
  flex: 1;
  border: 0;
  outline: none;
  background: transparent;
  color: var(--on-surface);
  min-width: 0;
}
.search-bar input::placeholder { color: var(--on-surface-variant); }

.trend {
  display: block;
  width: 100%;
  text-align: left;
  border: 0;
  background: transparent;
  color: inherit;
  padding: 8px 16px;
  cursor: pointer;
  transition: background .15s var(--ease);
}
.trend:hover { background: rgba(230, 224, 233, .06); }
.trend-idx  { color: var(--on-surface-variant); font-size: 12px; }
.trend-tag  { font-weight: 500; font-size: 14px; color: var(--on-surface); }
.trend-count{ color: var(--on-surface-variant); font-size: 12px; }

.suggest {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
}
.suggest-info { flex: 1; min-width: 0; }
.suggest-name {
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.suggest-handle {
  color: var(--on-surface-variant);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.btn-outlined {
  height: 32px;
  padding: 0 16px;
  border-radius: 16px;
  border: 1px solid var(--outline);
  background: transparent;
  color: var(--primary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background .15s var(--ease), border-color .15s var(--ease);
  white-space: nowrap;
}
.btn-outlined:hover { background: rgba(208, 188, 255, .10); }
.btn-outlined.following {
  border-color: var(--outline-variant);
  color: var(--on-surface-variant);
}
.btn-outlined.following:hover { background: rgba(242, 184, 181, .10); color: var(--error); }

.widget-more {
  display: block;
  width: 100%;
  padding: 12px 16px;
  border: 0;
  background: transparent;
  text-align: left;
  color: var(--primary);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: background .15s var(--ease);
}
.widget-more:hover { background: rgba(208, 188, 255, .08); }

/* ═══════════ Меню поста ═══════════ */
.menu {
  position: fixed;
  min-width: 200px;
  padding: 8px 0;
  border-radius: var(--shape-sm);
  background: var(--surface-high);
  box-shadow: 0 4px 8px 3px rgba(0,0,0,.15), 0 1px 3px rgba(0,0,0,.3);
  opacity: 0;
  transform: scale(.95);
  transform-origin: top left;
  pointer-events: none;
  transition: opacity .12s var(--ease), transform .12s var(--ease);
  z-index: 100;
}
.menu.show { opacity: 1; transform: scale(1); pointer-events: auto; }
.menu-item {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 10px 16px;
  border: 0;
  background: transparent;
  color: var(--on-surface);
  font-size: 14px;
  text-align: left;
  cursor: pointer;
  transition: background .12s var(--ease);
}
.menu-item:hover { background: rgba(230, 224, 233, .08); }
.menu-item .material-symbols-outlined { font-size: 20px; color: var(--on-surface-variant); }
.menu-item.danger { color: var(--error); }
.menu-item.danger .material-symbols-outlined { color: var(--error); }

/* ═══════════ Toast ═══════════ */
.toast {
  position: fixed;
  left: 50%;
  bottom: 24px;
  transform: translate(-50%, 80px);
  background: var(--inverse-surface);
  color: var(--inverse-on-surface);
  padding: 12px 20px;
  border-radius: var(--shape-xs);
  font-size: 14px;
  opacity: 0;
  pointer-events: none;
  transition: transform .25s var(--ease), opacity .25s var(--ease);
  z-index: 200;
  box-shadow: 0 4px 8px 3px rgba(0,0,0,.15);
}
.toast.show { transform: translate(-50%, 0); opacity: 1; }

/* ═══════════ Адаптив ═══════════ */
@media (max-width: 1140px) {
  .app { grid-template-columns: 260px minmax(0, 1fr); }
  .sidebar-right { display: none; }
}
@media (max-width: 760px) {
  .app { grid-template-columns: minmax(0, 1fr); padding: 0; gap: 0; }
  .sidebar-left { display: none; }
  .feed { height: 100vh; border-radius: 0; }
}
</style>
</head>
<body>

<div class="app">

  <!-- ═══════════════ ЛЕВАЯ КОЛОНКА ═══════════════ -->
  <aside class="sidebar-left">
    <div class="brand">
      <div class="brand-mark">m</div>
      <span>Mastodon</span>
    </div>

    <button class="nav-item active" data-view="home">
      <span class="material-symbols-outlined fill">home</span>
      <span>Главная</span>
    </button>

    <button class="nav-item" data-view="notifications">
      <span class="material-symbols-outlined">notifications</span>
      <span>Уведомления</span>
      <span class="badge">3</span>
    </button>

    <button class="nav-item" data-view="explore">
      <span class="material-symbols-outlined">explore</span>
      <span>Обзор</span>
    </button>

    <button class="nav-item" data-view="lists">
      <span class="material-symbols-outlined">format_list_bulleted</span>
      <span>Списки</span>
    </button>

    <button class="nav-item" data-view="bookmarks">
      <span class="material-symbols-outlined">bookmark</span>
      <span>Закладки</span>
    </button>

    <button class="nav-item" data-view="profile">
      <span class="material-symbols-outlined">person</span>
      <span>Профиль</span>
    </button>

    <button class="nav-item" data-view="settings">
      <span class="material-symbols-outlined">settings</span>
      <span>Настройки</span>
    </button>

    <button class="sidebar-cta" id="cta-compose">Опубликовать</button>

    <button class="user-tile" id="user-tile">
      <div class="avatar sm" style="--c1:#2b90d9;--c2:#6364ff">В</div>
      <div class="meta">
        <div class="name">Вы</div>
        <div class="handle">@you@mastodon.social</div>
      </div>
    </button>
  </aside>

  <!-- ═══════════════ ЦЕНТР ═══════════════ -->
  <main class="feed">
    <header class="feed-header">
      <span class="feed-title" id="feed-title">Главная</span>
      <button class="icon-btn" id="btn-settings" style="margin-left:auto" aria-label="Настройки ленты">
        <span class="material-symbols-outlined">tune</span>
      </button>
    </header>

    <section class="composer">
      <div class="avatar" style="--c1:#2b90d9;--c2:#6364ff">В</div>
      <div class="composer-body">
        <textarea id="compose-text" rows="2" placeholder="Что нового?"></textarea>
        <div class="composer-tools">
          <button class="tool" data-tool="media"   aria-label="Медиа"><span class="material-symbols-outlined">image</span></button>
          <button class="tool" data-tool="poll"    aria-label="Опрос"><span class="material-symbols-outlined">bar_chart</span></button>
          <button class="tool" data-tool="emoji"   aria-label="Эмодзи"><span class="material-symbols-outlined">mood</span></button>
          <button class="tool" data-tool="privacy" aria-label="Видимость"><span class="material-symbols-outlined">public</span></button>
          <span class="counter" id="counter">500</span>
          <button class="btn-filled" id="post-btn" disabled>Опубликовать</button>
        </div>
      </div>
    </section>

    <section class="timeline" id="timeline" aria-live="polite"></section>
  </main>

  <!-- ═══════════════ ПРАВАЯ КОЛОНКА ═══════════════ -->
  <aside class="sidebar-right">
    <div class="widget">
      <div class="search-wrap">
        <div class="search-bar">
          <span class="material-symbols-outlined">search</span>
          <input type="text" id="search-input" placeholder="Поиск" aria-label="Поиск">
        </div>
      </div>
    </div>

    <div class="widget" id="trends-widget">
      <div class="widget-head">Что происходит</div>
      <div id="trends-list"></div>
      <button class="widget-more" id="trends-more">Показать больше</button>
    </div>

    <div class="widget" id="suggest-widget">
      <div class="widget-head">Кого читать</div>
      <div id="suggest-list"></div>
      <button class="widget-more" id="suggest-more">Показать больше</button>
    </div>
  </aside>

</div>

<!-- Меню поста -->
<div class="menu" id="post-menu" role="menu">
  <button class="menu-item" data-menu="copy">
    <span class="material-symbols-outlined">link</span> Скопировать ссылку
  </button>
  <button class="menu-item" data-menu="mute">
    <span class="material-symbols-outlined">volume_off</span> Скрыть автора
  </button>
  <button class="menu-item" data-menu="mute-post">
    <span class="material-symbols-outlined">visibility_off</span> Скрыть пост
  </button>
  <button class="menu-item danger" data-menu="report">
    <span class="material-symbols-outlined">flag</span> Пожаловаться
  </button>
</div>

<!-- Toast -->
<div class="toast" id="toast" role="status"></div>

<script>
/* ═══════════ Утилиты ═══════════ */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(str) {
  return String(str).replace(/[&<>"']/g, c =>
    ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
}

/* Подсветка #тегов и @упоминаний */
function rich(text) {
  let safe = esc(text);
  safe = safe.replace(/(^|\s)(#[\wа-яёА-ЯЁ_]+)/gu,
    (_, p1, p2) => `${p1}<span class="tag">${p2}</span>`);
  safe = safe.replace(/(^|\s)(@[\w.]+(?:@[\w.]+)?)/gu,
    (_, p1, p2) => `${p1}<span class="mention">${p2}</span>`);
  return safe;
}

let toastTimer;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

/* ═══════════ Состояние ═══════════ */
const me = { name: 'Вы', handle: '@you@mastodon.social', initials: 'В', c1: '#2b90d9', c2: '#6364ff' };

const state = {
  view: 'home',
  query: '',
  posts: [
    {
      id: 1, name: 'Алиса Ветрова', handle: '@alice@mastodon.social',
      initials: 'А', c1: '#A78BFA', c2: '#6364ff', time: '5 мин',
      content: 'Разобралась наконец с rsync для бэкапов. Оказалось, что --delete делает совсем не то, что я думала. Хорошо, что сначала тестировала на копии.',
      replies: 4, boosts: 12, favs: 48,
      faved: false, boosted: false, bookmarked: false, hidden: false,
    },
    {
      id: 2, name: 'Дмитрий Орлов', handle: '@dmitry@techhub.social',
      initials: 'Д', c1: '#43aa8b', c2: '#90be6d', time: '32 мин',
      content: 'Собрал прототип клиента на FastAPI за вечер. Для интерфейса хватило чистого HTML и CSS — фронтенд-фреймворк не понадобился. #FastAPI',
      replies: 7, boosts: 31, favs: 96,
      faved: true, boosted: false, bookmarked: false, hidden: false,
    },
    {
      id: 3, name: 'Anna Dev', handle: '@anna@fosstodon.org',
      initials: 'A', c1: '#9b5de5', c2: '#f15bb5', time: '1 ч',
      content: 'Федерация — это сеть, а не один сервер. Выбирайте инстанс по сообществу, а не по размеру. #Mastodon #OpenSource',
      replies: 12, boosts: 87, favs: 214,
      faved: false, boosted: false, bookmarked: false, hidden: false,
    },
    {
      id: 4, name: 'Пётр Соколов', handle: '@petr@mstdn.social',
      initials: 'П', c1: '#f2a65a', c2: '#e56b6f', time: '2 ч',
      content: 'Кто-нибудь поднимал собственный инстанс для команды до 20 человек? Интересно, сколько ресурсов реально уходит на небольшой сервер.',
      replies: 23, boosts: 5, favs: 41,
      faved: false, boosted: false, bookmarked: false, hidden: false,
    },
    {
      id: 5, name: 'Мария К.', handle: '@maria@mastodon.social',
      initials: 'М', c1: '#e56b6f', c2: '#f2a65a', time: '3 ч',
      content: 'Тёмная тема в Mastodon сделана аккуратно. Глаза не устают после целого дня чтения ленты.',
      replies: 9, boosts: 18, favs: 132,
      faved: false, boosted: true, bookmarked: true, hidden: false,
    },
  ],
};

/* ═══════════ Рендер постов ═══════════ */
const ICON = {
  reply:    'chat_bubble',
  boost:    'repeat',
  fav:      'star',
  bookmark: 'bookmark',
  more:     'more_horiz',
};

function postHTML(p) {
  return `
  <article class="post" data-id="${p.id}">
    <div class="avatar" style="--c1:${p.c1};--c2:${p.c2}">${esc(p.initials)}</div>
    <div class="post-body">
      <div class="post-head">
        <span class="post-name">${esc(p.name)}</span>
        <span class="post-handle">${esc(p.handle)}</span>
        <span class="post-dot">·</span>
        <span class="post-time">${esc(p.time)}</span>
      </div>
      <div class="post-content">${rich(p.content)}</div>
      <div class="post-actions">
        <button class="action" data-act="reply" data-id="${p.id}" aria-label="Ответить">
          <span class="material-symbols-outlined">${ICON.reply}</span><span>${p.replies}</span>
        </button>
        <button class="action ${p.boosted ? 'active' : ''}" data-act="boost" data-id="${p.id}" aria-label="Поделиться">
          <span class="material-symbols-outlined ${p.boosted ? 'fill' : ''}">${ICON.boost}</span><span>${p.boosts}</span>
        </button>
        <button class="action ${p.faved ? 'active' : ''}" data-act="fav" data-id="${p.id}" aria-label="В избранное">
          <span class="material-symbols-outlined ${p.faved ? 'fill' : ''}">${ICON.fav}</span><span>${p.favs}</span>
        </button>
        <button class="action ${p.bookmarked ? 'active' : ''}" data-act="bookmark" data-id="${p.id}" aria-label="В закладки">
          <span class="material-symbols-outlined ${p.bookmarked ? 'fill' : ''}">${ICON.bookmark}</span>
        </button>
        <button class="action" data-act="more" data-id="${p.id}" aria-label="Ещё">
          <span class="material-symbols-outlined">${ICON.more}</span>
        </button>
      </div>
    </div>
  </article>`;
}

function getVisiblePosts() {
  let list = state.posts.filter(p => !p.hidden);
  const q = state.query.trim().toLowerCase().replace(/^#/, '').replace(/^@/, '');
  if (q) {
    list = list.filter(p =>
      p.content.toLowerCase().includes(q) ||
      p.name.toLowerCase().includes(q) ||
      p.handle.toLowerCase().includes(q)
    );
  }
  return list;
}

function renderTimeline() {
  const el = $('#timeline');
  const list = getVisiblePosts();

  if (!list.length) {
    el.innerHTML = `<div class="empty-state">
      ${state.query ? 'Ничего не найдено по запросу «' + esc(state.query) + '»' : 'Пока нет постов'}
    </div>`;
    return;
  }
  el.innerHTML = list.map(postHTML).join('');
}

/* ═══════════ Клики по действиям поста ═══════════ */
$('#timeline').addEventListener('click', e => {
  const actionBtn = e.target.closest('.action');
  const tagEl     = e.target.closest('.tag, .mention');

  if (tagEl) {
    const text = tagEl.textContent;
    $('#search-input').value = text;
    state.query = text;
    renderTimeline();
    return;
  }

  if (!actionBtn) return;

  const id   = Number(actionBtn.dataset.id);
  const post = state.posts.find(p => p.id === id);
  if (!post) return;

  const act = actionBtn.dataset.act;

  if (act === 'fav') {
    post.faved = !post.faved;
    post.favs += post.faved ? 1 : -1;
    renderTimeline();
  } else if (act === 'boost') {
    post.boosted = !post.boosted;
    post.boosts += post.boosted ? 1 : -1;
    renderTimeline();
  } else if (act === 'bookmark') {
    post.bookmarked = !post.bookmarked;
    renderTimeline();
    toast(post.bookmarked ? 'Добавлено в закладки' : 'Убрано из закладок');
  } else if (act === 'reply') {
    const ta = $('#compose-text');
    const local = post.handle.split('@')[1];
    if (!ta.value.trim().startsWith('@' + local)) {
      ta.value = '@' + local + ' ' + ta.value;
    }
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    updateComposer();
    $('#timeline').scrollTo({ top: 0, behavior: 'smooth' });
  } else if (act === 'more') {
    const r = actionBtn.getBoundingClientRect();
    openMenu(r.left - 160, r.bottom + 4, post.id);
  }
});

/* ═══════════ Меню поста ═══════════ */
const menu = $('#post-menu');
let menuPostId = null;

function openMenu(x, y, postId) {
  menuPostId = postId;
  menu.style.left = Math.max(8, Math.min(x, window.innerWidth - 220)) + 'px';
  menu.style.top  = Math.max(8, Math.min(y, window.innerHeight - 200)) + 'px';
  menu.classList.add('show');
}
function closeMenu() {
  menu.classList.remove('show');
  menuPostId = null;
}

document.addEventListener('click', e => {
  if (!menu.classList.contains('show')) return;
  if (e.target.closest('#post-menu')) return;
  if (e.target.closest('.action[data-act="more"]')) return;
  closeMenu();
});
window.addEventListener('keydown', e => { if (e.key === 'Escape') closeMenu(); });

menu.addEventListener('click', e => {
  const item = e.target.closest('.menu-item');
  if (!item || menuPostId === null) return;
  const post = state.posts.find(p => p.id === menuPostId);
  closeMenu();
  if (!post) return;

  const act = item.dataset.menu;
  if (act === 'copy') {
    const url = `https://mastodon.social/@user/${post.id}`;
    navigator.clipboard?.writeText(url).then(
      () => toast('Ссылка скопирована'),
      () => toast(url)
    );
  } else if (act === 'mute') {
    state.posts = state.posts.filter(p => p.handle !== post.handle);
    renderTimeline();
    toast('Автор скрыт из ленты');
  } else if (act === 'mute-post') {
    post.hidden = true;
    renderTimeline();
    toast('Пост скрыт');
  } else if (act === 'report') {
    toast('Жалоба отправлена модераторам');
  }
});

/* ═══════════ Композер ═══════════ */
const ta        = $('#compose-text');
const counterEl = $('#counter');
const postBtn   = $('#post-btn');
const LIMIT     = 500;

function updateComposer() {
  const len  = ta.value.length;
  const left = LIMIT - len;

  counterEl.textContent = left;
  counterEl.classList.toggle('warn', left <= 50 && left >= 0);
  counterEl.classList.toggle('over', left < 0);
  postBtn.disabled = len === 0 || left < 0;

  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 220) + 'px';
}

ta.addEventListener('input', updateComposer);

postBtn.addEventListener('click', () => {
  const text = ta.value.trim();
  if (!text) return;

  state.posts.unshift({
    id: Date.now(),
    name: me.name, handle: me.handle,
    initials: me.initials, c1: me.c1, c2: me.c2,
    time: 'сейчас',
    content: text,
    replies: 0, boosts: 0, favs: 0,
    faved: false, boosted: false, bookmarked: false, hidden: false,
  });

  ta.value = '';
  updateComposer();
  renderTimeline();
  $('#timeline').scrollTo({ top: 0, behavior: 'smooth' });
  toast('Пост опубликован');
});

$('#cta-compose').addEventListener('click', () => {
  ta.focus();
  window.scrollTo({ top: 0 });
  $('#feed-title').scrollIntoView({ behavior: 'smooth' });
});

/* Заглушки инструментов композера */
$$('.tool').forEach(btn => {
  btn.addEventListener('click', () => {
    const t = btn.dataset.tool;
    const map = {
      media:   'Выбор медиа',
      poll:    'Опрос',
      emoji:   'Эмодзи',
      privacy: 'Видимость: публично',
    };
    toast(map[t] + ' — демо-режим');
  });
});

/* ═══════════ Навигация ═══════════ */
const VIEW_TITLES = {
  home:          'Главная',
  notifications: 'Уведомления',
  explore:       'Обзор',
  lists:         'Списки',
  bookmarks:     'Закладки',
  profile:       'Профиль',
  settings:      'Настройки',
};

$$('.nav-item').forEach(item => {
  item.addEventListener('click', () => {
    $$('.nav-item').forEach(x => {
      x.classList.toggle('active', x === item);
      const icon = x.querySelector('.material-symbols-outlined');
      icon.classList.toggle('fill', x === item);
    });

    const view = item.dataset.view;
    state.view = view;
    $('#feed-title').textContent = VIEW_TITLES[view] || 'Mastodon';

    if (view === 'bookmarks') {
      state.query = '';
      $('#search-input').value = '';
      renderBookmarks();
    } else if (view === 'home') {
      renderTimeline();
    } else {
      $('#timeline').innerHTML =
        `<div class="empty-state">Раздел «${esc(VIEW_TITLES[view])}» — демонстрационный</div>`;
    }
  });
});

function renderBookmarks() {
  const el = $('#timeline');
  const list = state.posts.filter(p => p.bookmarked && !p.hidden);
  if (!list.length) {
    el.innerHTML = `<div class="empty-state">В закладках пока пусто</div>`;
    return;
  }
  el.innerHTML = list.map(postHTML).join('');
}

$('#btn-settings').addEventListener('click', () => toast('Настройки ленты — демо-режим'));
$('#user-tile').addEventListener('click',  () => toast('Профиль пользователя — демо-режим'));

/* ═══════════ Поиск ═══════════ */
$('#search-input').addEventListener('input', e => {
  state.query = e.target.value;
  renderTimeline();
});

/* ═══════════ Тренды ═══════════ */
const TRENDS_ALL = [
  { idx: 'Актуально в вашем регионе', tag: '#Mastodon',    count: '12,4 тыс. постов' },
  { idx: 'Технологии',                tag: '#FastAPI',     count: '8 213 постов' },
  { idx: 'Программирование',          tag: '#Python',      count: '5 902 поста' },
  { idx: 'Актуально',                 tag: '#OpenSource',  count: '3 145 постов' },
  { idx: 'Разработка',                tag: '#Linux',       count: '2 780 постов' },
  { idx: 'Наука',                     tag: '#Astronomy',   count: '1 902 поста' },
];

const SUGGEST_ALL = [
  { name: 'Мария К.',      handle: '@maria@mastodon.social', initials: 'М', c1: '#e56b6f', c2: '#f2a65a' },
  { name: 'Дмитрий Орлов', handle: '@dmitry@techhub.social', initials: 'Д', c1: '#43aa8b', c2: '#90be6d' },
  { name: 'Anna Dev',      handle: '@anna@fosstodon.org',    initials: 'A', c1: '#9b5de5', c2: '#f15bb5' },
  { name: 'Пётр Соколов',  handle: '@petr@mstdn.social',     initials: 'П', c1: '#f2a65a', c2: '#e56b6f' },
];

let trendsLimit  = 4;
let suggestLimit = 3;

function renderTrends() {
  $('#trends-list').innerHTML = TRENDS_ALL.slice(0, trendsLimit).map((t, i) => `
    <button class="trend" data-query="${esc(t.tag)}">
      <div class="trend-idx">${i + 1} · ${esc(t.idx)}</div>
      <div class="trend-tag">${esc(t.tag)}</div>
      <div class="trend-count">${esc(t.count)}</div>
    </button>`).join('');

  const btn = $('#trends-more');
  btn.textContent = trendsLimit >= TRENDS_ALL.length ? 'Свернуть' : 'Показать больше';
}

function renderSuggest() {
  $('#suggest-list').innerHTML = SUGGEST_ALL.slice(0, suggestLimit).map((s, i) => `
    <div class="suggest" data-idx="${i}">
      <div class="avatar xs" style="--c1:${s.c1};--c2:${s.c2}">${esc(s.initials)}</div>
      <div class="suggest-info">
        <div class="suggest-name">${esc(s.name)}</div>
        <div class="suggest-handle">${esc(s.handle)}</div>
      </div>
      <button class="btn-outlined" data-follow="${i}">Подписаться</button>
    </div>`).join('');

  const btn = $('#suggest-more');
  btn.textContent = suggestLimit >= SUGGEST_ALL.length ? 'Свернуть' : 'Показать больше';
}

$('#trends-more').addEventListener('click', () => {
  trendsLimit = trendsLimit >= TRENDS_ALL.length ? 4 : TRENDS_ALL.length;
  renderTrends();
});
$('#suggest-more').addEventListener('click', () => {
  suggestLimit = suggestLimit >= SUGGEST_ALL.length ? 3 : SUGGEST_ALL.length;
  renderSuggest();
});

$('#trends-list').addEventListener('click', e => {
  const t = e.target.closest('.trend');
  if (!t) return;
  const q = t.dataset.query;
  $('#search-input').value = q;
  state.query = q;
  renderTimeline();
  toast('Показаны посты по ' + q);
});

$('#suggest-list').addEventListener('click', e => {
  const btn = e.target.closest('[data-follow]');
  if (!btn) return;
  const following = btn.classList.toggle('following');
  btn.textContent = following ? 'Отписаться' : 'Подписаться';
  toast(following ? 'Вы подписались' : 'Вы отписались');
});

/* ═══════════ Медиазапросы ═══════════ */
const mq = window.matchMedia('(max-width: 1140px)');

/* ═══════════ Старт ═══════════ */
renderTrends();
renderSuggest();
renderTimeline();
updateComposer();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
