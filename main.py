"""UI-клон клиента Mastodon на FastAPI (одним файлом).

Только интерфейс — никакого ActivityPub, базы данных и реальных запросов.
Все посты, тренды и рекомендации — моковые данные на клиенте.

Запуск:
    pip install fastapi uvicorn
    python main.py
    Открыть: http://127.0.0.1:8000
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Mastodon UI Clone")


INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mastodon</title>
<style>
  *{box-sizing:border-box;}
  :root{
    --bg:#191b22;
    --panel:#282c37;
    --panel-2:#313543;
    --border:#393f4f;
    --text:#d9e1e8;
    --muted:#9baec8;
    --accent:#6364ff;
    --accent-hover:#7b7bff;
    --accent-soft:rgba(99,100,255,.15);
    --green:#79bd9a;
    --yellow:#ca8f04;
    --red:#df405a;
  }
  html{scrollbar-color:#393f4f transparent;}
  body{
    margin:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    font-size:15px;line-height:20px;-webkit-font-smoothing:antialiased;
  }
  a{color:inherit;text-decoration:none;}
  button{font-family:inherit;font-size:inherit;}
  ::-webkit-scrollbar{width:8px;height:8px;}
  ::-webkit-scrollbar-thumb{background:#393f4f;border-radius:4px;}
  ::-webkit-scrollbar-track{background:transparent;}

  /* ══════════ Сетка ══════════ */
  .app{
    display:grid;
    grid-template-columns:285px minmax(0,1fr) 285px;
    gap:10px;max-width:1260px;margin:0 auto;
    padding:0 10px 80px;align-items:start;
  }

  /* ══════════ Левая колонка ══════════ */
  .sidebar-left{
    position:sticky;top:0;height:100vh;display:flex;flex-direction:column;
    padding:16px 0;overflow-y:auto;
  }
  .logo{display:flex;align-items:center;gap:10px;padding:6px 12px 18px;font-size:20px;font-weight:700;letter-spacing:-.3px;}
  .logo-mark{
    width:34px;height:34px;border-radius:10px;flex:0 0 34px;
    background:linear-gradient(135deg,#6364ff,#8e5bff);
    display:grid;place-items:center;color:#fff;font-weight:800;font-size:19px;font-style:italic;
  }
  .nav-item{
    display:flex;align-items:center;gap:14px;padding:10px 12px;border-radius:10px;
    color:var(--text);font-size:16px;font-weight:500;
    transition:background .12s,color .12s;cursor:pointer;
  }
  .nav-item:hover{background:var(--panel);}
  .nav-item.active{color:var(--accent);background:var(--accent-soft);}
  .nav-icon{width:22px;height:22px;flex:0 0 22px;}
  .nav-badge{
    margin-left:auto;background:var(--accent);color:#fff;font-size:12px;font-weight:700;
    padding:1px 7px;border-radius:10px;line-height:16px;
  }
  .sidebar-compose{
    margin:18px 12px;padding:12px;border:0;border-radius:10px;
    background:var(--accent);color:#fff;font-weight:700;font-size:15px;cursor:pointer;
    transition:background .12s;
  }
  .sidebar-compose:hover{background:var(--accent-hover);}
  .profile-card{
    margin-top:auto;display:flex;align-items:center;gap:10px;
    padding:10px 12px;border-radius:10px;cursor:pointer;
  }
  .profile-card:hover{background:var(--panel);}
  .profile-card .meta{min-width:0;}
  .profile-card .name{font-weight:700;font-size:14px;}
  .profile-card .handle{color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}

  /* ══════════ Центральная колонка ══════════ */
  .column{background:var(--panel);border-radius:12px;min-height:100vh;overflow:hidden;}
  .column-header{
    position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:16px;
    padding:0 16px;height:53px;
    background:rgba(40,44,55,.9);backdrop-filter:blur(8px);
    border-bottom:1px solid var(--border);
  }
  .column-title{font-size:16px;font-weight:700;}
  .header-icon{
    margin-left:auto;width:34px;height:34px;display:grid;place-items:center;
    border:0;background:none;color:var(--muted);border-radius:8px;cursor:pointer;
  }
  .header-icon:hover{color:var(--accent);background:var(--accent-soft);}
  .header-icon svg{width:19px;height:19px;}

  /* ── Композер ── */
  .compose{padding:14px 16px;border-bottom:1px solid var(--border);}
  .compose-row{display:flex;gap:12px;align-items:flex-start;}
  .compose-main{flex:1;min-width:0;}
  .compose textarea{
    width:100%;background:var(--panel-2);border:1px solid var(--border);border-radius:10px;
    color:var(--text);padding:10px 12px;font-size:15px;line-height:20px;font-family:inherit;
    resize:none;min-height:56px;outline:none;transition:border-color .12s;
  }
  .compose textarea:focus{border-color:var(--accent);}
  .compose textarea::placeholder{color:var(--muted);}
  .compose-tools{display:flex;align-items:center;gap:2px;margin-top:8px;}
  .tool{
    width:32px;height:32px;display:grid;place-items:center;border:0;background:none;
    color:var(--accent);border-radius:8px;cursor:pointer;transition:background .12s;
  }
  .tool:hover{background:var(--accent-soft);}
  .tool svg{width:18px;height:18px;}
  .counter{
    font-size:13px;color:var(--muted);margin-left:auto;margin-right:10px;
    font-variant-numeric:tabular-nums;transition:color .12s;
  }
  .counter.warn{color:var(--yellow);}
  .counter.over{color:var(--red);font-weight:700;}
  .btn-primary{
    border:0;border-radius:999px;background:var(--accent);color:#fff;font-weight:700;
    padding:8px 18px;cursor:pointer;transition:background .12s,opacity .12s;
  }
  .btn-primary:hover:not(:disabled){background:var(--accent-hover);}
  .btn-primary:disabled{opacity:.45;cursor:not-allowed;}

  /* ── Аватар ── */
  .avatar{
    width:44px;height:44px;flex:0 0 44px;border-radius:12px;
    display:grid;place-items:center;color:#fff;font-weight:700;font-size:17px;
    user-select:none;background:linear-gradient(135deg,var(--c1),var(--c2));
  }
  .avatar.sm{width:36px;height:36px;flex-basis:36px;border-radius:10px;font-size:14px;}

  /* ── Пост ── */
  .post{
    display:flex;gap:12px;padding:14px 16px;
    border-bottom:1px solid var(--border);transition:background .1s;
  }
  .post:hover{background:rgba(49,53,67,.5);}
  .post-body{flex:1;min-width:0;}
  .post-head{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;}
  .display-name{font-weight:700;}
  .handle{color:var(--muted);font-size:14px;}
  .post-head .time{color:var(--muted);font-size:14px;}
  .post-content{margin:4px 0 10px;white-space:pre-wrap;overflow-wrap:anywhere;}
  .post-actions{display:flex;gap:2px;margin-left:-6px;color:var(--muted);}
  .action{
    display:flex;align-items:center;gap:6px;padding:4px 8px;border:0;background:none;
    color:inherit;border-radius:8px;cursor:pointer;font-size:13px;
    font-variant-numeric:tabular-nums;transition:color .12s,background .12s;
  }
  .action svg{width:18px;height:18px;flex:0 0 18px;}
  .action:hover{color:var(--accent);background:var(--accent-soft);}
  .action.active.fav{color:var(--yellow);}
  .action.active.boost{color:var(--green);}

  /* ══════════ Правая колонка ══════════ */
  .sidebar-right{position:sticky;top:0;padding:16px 0;display:flex;flex-direction:column;gap:10px;}
  .widget{background:var(--panel);border-radius:12px;overflow:hidden;}
  .widget-head{padding:12px 16px;font-weight:700;font-size:15px;border-bottom:1px solid var(--border);}
  .search-box{padding:10px 12px;}
  .search-box input{
    width:100%;background:var(--panel-2);border:1px solid var(--border);border-radius:999px;
    color:var(--text);padding:9px 14px;font-size:14px;outline:none;font-family:inherit;
    transition:border-color .12s;
  }
  .search-box input:focus{border-color:var(--accent);}
  .search-box input::placeholder{color:var(--muted);}
  .trend{padding:10px 16px;cursor:pointer;transition:background .12s;}
  .trend:hover{background:var(--panel-2);}
  .trend-idx{color:var(--muted);font-size:13px;}
  .trend-tag{font-weight:700;}
  .trend-count{color:var(--muted);font-size:13px;}
  .suggest{display:flex;align-items:center;gap:10px;padding:10px 16px;}
  .suggest-info{flex:1;min-width:0;}
  .suggest-name{font-weight:700;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .suggest-handle{color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .btn-ghost{
    border:1px solid var(--accent);background:none;color:var(--accent);border-radius:999px;
    padding:4px 12px;font-size:13px;font-weight:700;cursor:pointer;transition:background .12s;
  }
  .btn-ghost:hover{background:var(--accent-soft);}
  .widget-foot{
    padding:12px 16px;color:var(--accent);font-size:14px;font-weight:600;
    cursor:pointer;border-top:1px solid var(--border);
  }
  .widget-foot:hover{background:var(--panel-2);}

  /* ══════════ Адаптив ══════════ */
  @media (max-width:1080px){
    .app{grid-template-columns:285px minmax(0,1fr);}
    .sidebar-right{display:none;}
  }
  @media (max-width:760px){
    .app{grid-template-columns:minmax(0,1fr);padding:0 0 40px;}
    .sidebar-left{display:none;}
    .column{border-radius:0;}
  }
</style>
</head>
<body>
<div class="app">

  <!-- ═══════════ ЛЕВАЯ КОЛОНКА ═══════════ -->
  <nav class="sidebar-left">
    <div class="logo">
      <div class="logo-mark">m</div>
      <span>Mastodon</span>
    </div>

    <a class="nav-item active" href="#">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9.5 12 3l9 6.5V20a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/></svg>
      <span>Главная</span>
    </a>

    <a class="nav-item" href="#">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>
      <span>Уведомления</span>
      <span class="nav-badge">3</span>
    </a>

    <a class="nav-item" href="#">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <span>Обзор</span>
    </a>

    <a class="nav-item" href="#">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 6-10 7L2 6"/></svg>
      <span>Личные сообщения</span>
    </a>

    <a class="nav-item" href="#">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>
      <span>Списки</span>
    </a>

    <a class="nav-item" href="#">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>
      <span>Закладки</span>
    </a>

    <a class="nav-item" href="#">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
      <span>Профиль</span>
    </a>

    <a class="nav-item" href="#">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>
      <span>Настройки</span>
    </a>

    <button class="sidebar-compose" id="sidebar-compose-btn">Опубликовать</button>

    <div class="profile-card">
      <div class="avatar sm" style="--c1:#2b90d9;--c2:#6364ff">В</div>
      <div class="meta">
        <div class="name">Вы</div>
        <div class="handle">@you@mastodon.social</div>
      </div>
    </div>
  </nav>

  <!-- ═══════════ ЦЕНТРАЛЬНАЯ КОЛОНКА ═══════════ -->
  <main class="column">
    <div class="column-header">
      <span class="column-title">Главная</span>
      <button class="header-icon" title="Настройки ленты">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      </button>
    </div>

    <!-- Композер -->
    <div class="compose">
      <div class="compose-row">
        <div class="avatar" style="--c1:#2b90d9;--c2:#6364ff">В</div>
        <div class="compose-main">
          <textarea id="compose-text" rows="2" placeholder="Что нового?"></textarea>
          <div class="compose-tools">
            <button class="tool" title="Медиа">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>
            </button>
            <button class="tool" title="Опрос">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 20V10M12 20V4M18 20v-7"/></svg>
            </button>
            <button class="tool" title="Эмодзи">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><path d="M9 9h.01M15 9h.01"/></svg>
            </button>
            <button class="tool" title="Видимость: публично">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z"/></svg>
            </button>
            <span class="counter" id="counter">500</span>
            <button class="btn-primary" id="post-btn" disabled>Опубликовать</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Лента -->
    <div id="timeline"></div>
  </main>

  <!-- ═══════════ ПРАВАЯ КОЛОНКА ═══════════ -->
  <aside class="sidebar-right">
    <div class="widget">
      <div class="search-box">
        <input type="text" placeholder="Поиск в Mastodon">
      </div>
    </div>

    <div class="widget">
      <div class="widget-head">Что происходит</div>
      <div class="trend">
        <div class="trend-idx">1 · Актуально в вашем регионе</div>
        <div class="trend-tag">#Mastodon</div>
        <div class="trend-count">12,4 тыс. постов</div>
      </div>
      <div class="trend">
        <div class="trend-idx">2 · Технологии</div>
        <div class="trend-tag">#FastAPI</div>
        <div class="trend-count">8 213 постов</div>
      </div>
      <div class="trend">
        <div class="trend-idx">3 · Программирование</div>
        <div class="trend-tag">#Python</div>
        <div class="trend-count">5 902 поста</div>
      </div>
      <div class="trend">
        <div class="trend-idx">4 · Актуально</div>
        <div class="trend-tag">#OpenSource</div>
        <div class="trend-count">3 145 постов</div>
      </div>
      <div class="widget-foot">Показать больше</div>
    </div>

    <div class="widget">
      <div class="widget-head">Кого читать</div>
      <div class="suggest">
        <div class="avatar sm" style="--c1:#e56b6f;--c2:#f2a65a">М</div>
        <div class="suggest-info">
          <div class="suggest-name">Мария К.</div>
          <div class="suggest-handle">@maria@mastodon.social</div>
        </div>
        <button class="btn-ghost">Подписаться</button>
      </div>
      <div class="suggest">
        <div class="avatar sm" style="--c1:#43aa8b;--c2:#90be6d">Д</div>
        <div class="suggest-info">
          <div class="suggest-name">Дмитрий Орлов</div>
          <div class="suggest-handle">@dmitry@techhub.social</div>
        </div>
        <button class="btn-ghost">Подписаться</button>
      </div>
      <div class="suggest">
        <div class="avatar sm" style="--c1:#9b5de5;--c2:#f15bb5">А</div>
        <div class="suggest-info">
          <div class="suggest-name">Anna Dev</div>
          <div class="suggest-handle">@anna@fosstodon.org</div>
        </div>
        <button class="btn-ghost">Подписаться</button>
      </div>
      <div class="widget-foot">Показать больше</div>
    </div>
  </aside>
</div>

<script>
/* ═══════════ Иконки для действий под постом ═══════════ */
const ICONS = {
  reply: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
  boost: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m17 2 4 4-4 4"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>',
  star: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>',
  bookmark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>',
  more: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>'
};

/* ═══════════ Моковые данные ═══════════ */
const state = {
  me: { name: 'Вы', handle: '@you@mastodon.social', initials: 'В', c1: '#2b90d9', c2: '#6364ff' },
  posts: [
    {
      id: 1, name: 'Алиса Ветрова', handle: '@alice@mastodon.social',
      initials: 'А', c1: '#6364ff', c2: '#8e5bff', time: '5 мин',
      content: 'Привет, Mastodon! 🎉 Это первый пост в моём UI-клоне федеративной ленты. Дизайн повторяет привычный веб-клиент — тёмная тема, три колонки, всё как надо.',
      replies: 4, boosts: 12, favs: 48, faved: false, boosted: false
    },
    {
      id: 2, name: 'Дмитрий Орлов', handle: '@dmitry@techhub.social',
      initials: 'Д', c1: '#43aa8b', c2: '#90be6d', time: '32 мин',
      content: 'Собрал за вечер прототип клиента на FastAPI + чистый HTML. Оказалось, что для UI вообще не нужен фронтенд-фреймворк, если не нужна реактивность на сервере.',
      replies: 7, boosts: 31, favs: 96, faved: true, boosted: false
    },
    {
      id: 3, name: 'Anna Dev', handle: '@anna@fosstodon.org',
      initials: 'A', c1: '#9b5de5', c2: '#f15bb5', time: '1 ч',
      content: 'Напоминание: федерация — это не про один сервер, а про сеть. Выбирайте инстанс по сообществу, а не по размеру. #Mastodon #OpenSource',
      replies: 12, boosts: 87, favs: 214, faved: false, boosted: false
    },
    {
      id: 4, name: 'Пётр Соколов', handle: '@petr@mstdn.social',
      initials: 'П', c1: '#f2a65a', c2: '#e56b6f', time: '2 ч',
      content: 'Кто-нибудь уже пробовал ActivityPub на практике? Интересно, насколько сложно поднять собственный инстанс для небольшой команды.',
      replies: 23, boosts: 5, favs: 41, faved: false, boosted: false
    },
    {
      id: 5, name: 'Мария К.', handle: '@maria@mastodon.social',
      initials: 'М', c1: '#e56b6f', c2: '#f2a65a', time: '3 ч',
      content: 'Тёмная тема Mastodon — эталон. Глаза не устают даже после целого дня чтения ленты. Кто ещё ждёт кастомные темы в веб-клиенте?',
      replies: 9, boosts: 18, favs: 132, faved: false, boosted: true
    }
  ]
};

/* ═══════════ Утилиты ═══════════ */
function esc(str) {
  return String(str).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function postHTML(p) {
  return `
  <article class="post" data-id="${p.id}">
    <div class="avatar" style="--c1:${p.c1};--c2:${p.c2}">${esc(p.initials)}</div>
    <div class="post-body">
      <div class="post-head">
        <span class="display-name">${esc(p.name)}</span>
        <span class="handle">${esc(p.handle)}</span>
        <span class="time">· ${esc(p.time)}</span>
      </div>
      <div class="post-content">${esc(p.content)}</div>
      <div class="post-actions">
        <button class="action" data-act="reply" data-id="${p.id}" title="Ответить">${ICONS.reply}<span>${p.replies}</span></button>
        <button class="action boost ${p.boosted ? 'active' : ''}" data-act="boost" data-id="${p.id}" title="Поделиться">${ICONS.boost}<span>${p.boosts}</span></button>
        <button class="action fav ${p.faved ? 'active' : ''}" data-act="fav" data-id="${p.id}" title="В избранное">${ICONS.star}<span>${p.favs}</span></button>
        <button class="action" data-act="bookmark" data-id="${p.id}" title="В закладки">${ICONS.bookmark}</button>
        <button class="action" data-act="more" data-id="${p.id}" title="Ещё">${ICONS.more}</button>
      </div>
    </div>
  </article>`;
}

/* ═══════════ Рендер ленты ═══════════ */
const timelineEl = document.getElementById('timeline');

function render() {
  timelineEl.innerHTML = state.posts.map(postHTML).join('');
}

/* ═══════════ Обработка кликов по действиям ═══════════ */
timelineEl.addEventListener('click', function (e) {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;

  const id = Number(btn.dataset.id);
  const post = state.posts.find(function (p) { return p.id === id; });
  if (!post) return;

  const act = btn.dataset.act;

  if (act === 'fav') {
    post.faved = !post.faved;
    post.favs += post.faved ? 1 : -1;
  } else if (act === 'boost') {
    post.boosted = !post.boosted;
    post.boosts += post.boosted ? 1 : -1;
  } else if (act === 'reply') {
    const ta = document.getElementById('compose-text');
    ta.value = '@' + post.handle.split('@')[1] + ' ';
    ta.focus();
    updateCompose();
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  } else {
    // bookmark / more — просто визуальная заглушка
    return;
  }

  render();
});

/* ═══════════ Композер ═══════════ */
const ta = document.getElementById('compose-text');
const counterEl = document.getElementById('counter');
const postBtn = document.getElementById('post-btn');
const LIMIT = 500;

function updateCompose() {
  const len = ta.value.length;
  const left = LIMIT - len;

  counterEl.textContent = left;
  counterEl.classList.toggle('warn', left <= 50 && left >= 0);
  counterEl.classList.toggle('over', left < 0);

  postBtn.disabled = len === 0 || left < 0;

  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 240) + 'px';
}

ta.addEventListener('input', updateCompose);

postBtn.addEventListener('click', function () {
  const text = ta.value.trim();
  if (!text) return;

  state.posts.unshift({
    id: Date.now(),
    name: state.me.name,
    handle: state.me.handle,
    initials: state.me.initials,
    c1: state.me.c1,
    c2: state.me.c2,
    time: 'сейчас',
    content: text,
    replies: 0, boosts: 0, favs: 0,
    faved: false, boosted: false
  });

  ta.value = '';
  updateCompose();
  render();
});

document.getElementById('sidebar-compose-btn').addEventListener('click', function () {
  ta.focus();
  window.scrollTo({ top: 0, behavior: 'smooth' });
});

/* ═══════════ Переключение активного пункта меню ═══════════ */
document.querySelectorAll('.nav-item').forEach(function (item) {
  item.addEventListener('click', function (e) {
    e.preventDefault();
    document.querySelectorAll('.nav-item').forEach(function (i) { i.classList.remove('active'); });
    item.classList.add('active');
  });
});

/* ═══════════ Кнопки «Подписаться» ═══════════ */
document.querySelectorAll('.btn-ghost').forEach(function (btn) {
  btn.addEventListener('click', function () {
    const subscribed = btn.textContent.trim() === 'Подписаться';
    btn.textContent = subscribed ? 'Отписаться' : 'Подписаться';
    btn.style.background = subscribed ? 'var(--accent-soft)' : 'none';
  });
});

/* ═══════════ Старт ═══════════ */
render();
updateCompose();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Отдаёт единственную страницу с UI-клоном Mastodon."""
    return INDEX_HTML


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
