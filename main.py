import os
import json
import datetime
import secrets
import re
import time
import httpx
import uuid
import base64
import hashlib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form, Request
from fastapi.responses import HTMLResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn

SERVER_START_TIME = datetime.datetime.now(datetime.timezone.utc).isoformat()
TURNSTILE_SECRET = "0x4AAAAAAEt2kX9fNPZNVSsCEur4myw93h4"
TURNSTILE_SITEKEY = "0x4AAAAAAEt2kcFzE58AuS_r"
SALT_IP = secrets.token_bytes(32)  # Соль для хэширования IP-адресов

DEFAULT_CHANNELS = [
    "#general", "#random", "#news", "#music", "#gaming",
    "#programming", "#python", "#javascript", "#movies", "#anime",
    "#books", "#science", "#space", "#technology", "#hardware",
    "#art", "#design", "#photography", "#memes", "#sports",
    "#fitness", "#food", "#travel", "#cars", "#help"
]

NICK_REGEX = re.compile(r"^[a-zA-Z0-9_\-\u0400-\u04FF]{2,20}$")
CHANNEL_REGEX = re.compile(r"^#[a-zA-Z0-9_\-\u0400-\u04FF]{2,30}$")

VALID_SESSIONS: dict[str, dict] = {}
MAX_SESSION_AGE = 86400  # 24 hours
IP_CONNECTIONS: dict[str, int] = {}
MAX_CONNS_PER_IP = 15
MESSAGE_TIMESTAMPS: dict[WebSocket, list[float]] = {}

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IRC Lite Web</title>
    <!-- Cloudflare Turnstile SDK (SRI не используется, так как скрипт динамически обновляется CDN) -->
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
    <style nonce="NONCE_PLACEHOLDER">
        :root {
            --bg-dark: #0d1117;
            --bg-card: #161b22;
            --bg-panel: #21262d;
            --bg-input: #0d1117;
            --border-main: #30363d;
            --text-primary: #c9d1d9;
            --text-muted: #8b949e;
            --text-heading: #f0f6fc;
            --accent-blue: #2f81f7;
            --accent-hover: #58a6ff;
            --status-green: #3fb950;
        }

        * {
            box-sizing: border-box !important;
        }

        body {
            margin: 0;
            padding: 0;
            background-color: var(--bg-dark);
            color: var(--text-primary);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .hidden { display: none !important; }

        .icon {
            display: inline-block;
            vertical-align: middle;
            fill: currentColor;
            flex-shrink: 0;
        }

        /* Top bar styling */
        .top-bar {
            height: 42px;
            background: var(--bg-card);
            border-bottom: 1px solid var(--border-main);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 20px;
            font-size: 13px;
            color: var(--text-primary);
            flex-shrink: 0;
            z-index: 100;
        }

        .top-bar-brand {
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 700;
            color: var(--text-heading);
        }

        .top-bar-uptime {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
            color: var(--text-muted);
            background: var(--bg-dark);
            padding: 4px 12px;
            border-radius: 20px;
            border: 1px solid var(--border-main);
            font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
        }

        .pulse-dot {
            width: 8px;
            height: 8px;
            background-color: var(--status-green);
            border-radius: 50%;
            box-shadow: 0 0 8px var(--status-green);
            animation: pulse-animation 2s infinite;
        }

        @keyframes pulse-animation {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(63, 185, 80, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(63, 185, 80, 0); }
        }

        /* Login Page */
        .login-page {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            flex: 1;
            background: radial-gradient(circle at center, #161b22 0%, #0d1117 100%);
            padding: 24px;
            overflow-y: auto;
        }

        .login-card {
            background: var(--bg-card);
            border: 1px solid var(--border-main);
            border-radius: 12px;
            padding: 32px 28px;
            width: 100%;
            max-width: 440px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.6);
        }

        .login-card-header {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 8px;
        }

        .login-card h2 {
            color: var(--text-heading);
            font-size: 22px;
            margin: 0;
            font-weight: 700;
        }

        .login-card p.subtitle {
            color: var(--text-muted);
            font-size: 13px;
            margin-top: 4px;
            margin-bottom: 24px;
            line-height: 1.4;
        }

        .form-group {
            margin-bottom: 20px;
        }

        .form-label {
            display: block;
            color: var(--text-heading);
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 8px;
        }

        .form-control-custom {
            width: 100%;
            padding: 10px 14px;
            background: var(--bg-input);
            border: 1px solid var(--border-main);
            border-radius: 6px;
            color: var(--text-heading);
            font-size: 14px;
            outline: none;
            transition: border-color 0.15s ease;
        }

        .form-control-custom:focus {
            border-color: var(--accent-blue);
        }

        /* 
           ИСПРАВЛЕНИЕ: Растягиваем текст чекбоксов на весь фрейм. 
           Добавлено flex: 1 для label и width: 100% для контейнера.
        */
        .checkbox-container {
            display: flex;
            align-items: flex-start;
            gap: 12px;
            background: var(--bg-panel);
            padding: 12px 14px;
            border-radius: 6px;
            border: 1px solid var(--border-main);
            margin-bottom: 12px;
            width: 100%;
        }

        .checkbox-container input[type="checkbox"] {
            width: 18px;
            height: 18px;
            min-width: 18px;
            min-height: 18px;
            margin-top: 2px;
            cursor: pointer;
            accent-color: var(--accent-blue);
        }

        .checkbox-container label {
            flex: 1; /* Растягиваем текст на всё свободное пространство */
            font-size: 13px;
            color: var(--text-primary);
            line-height: 1.4;
            margin: 0;
            cursor: pointer;
            user-select: none;
            display: block;
            width: 100%;
        }

        .checkbox-container a {
            color: var(--accent-hover);
            text-decoration: underline;
        }

        .turnstile-wrapper {
            display: flex;
            justify-content: center;
            align-items: center;
            background: var(--bg-panel);
            border: 1px solid var(--border-main);
            border-radius: 6px;
            padding: 8px;
            margin-bottom: 20px;
            min-height: 75px;
            width: 100%;
        }

        .cf-turnstile {
            display: inline-block;
            margin: 0 auto;
        }

        .btn-primary-custom {
            width: 100%;
            padding: 12px 16px;
            background: #238636;
            color: #ffffff;
            border: 1px solid rgba(240,246,252,0.1);
            border-radius: 6px;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
            transition: background 0.15s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }

        .btn-primary-custom:hover {
            background: #2ea043;
        }

        .btn-primary-custom:disabled {
            background: #194a21;
            cursor: not-allowed;
            opacity: 0.8;
        }

        /* Modal */
        .modal-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.8);
            backdrop-filter: blur(4px);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 9999;
            padding: 16px;
        }

        .modal-card {
            background: var(--bg-card);
            border: 1px solid var(--border-main);
            border-radius: 8px;
            padding: 24px;
            max-width: 420px;
            width: 100%;
            box-shadow: 0 16px 32px rgba(0, 0, 0, 0.8);
        }

        .modal-card h3 {
            margin-top: 0;
            color: var(--text-heading);
            font-size: 18px;
            margin-bottom: 12px;
        }

        .modal-card p, .modal-card ul {
            font-size: 13px;
            line-height: 1.5;
            color: var(--text-primary);
            margin-bottom: 16px;
        }

        /* App Chat Area */
        .app-container {
            display: flex;
            flex: 1;
            height: calc(100vh - 42px);
            width: 100vw;
            overflow: hidden;
        }

        .sidebar-left, .sidebar-right {
            background: var(--bg-card);
            border-right: 1px solid var(--border-main);
            display: flex;
            flex-direction: column;
            user-select: none;
        }

        .sidebar-left { width: 250px; min-width: 250px; }
        .sidebar-right { width: 240px; min-width: 240px; border-right: none; border-left: 1px solid var(--border-main); }

        .sidebar-header {
            padding: 14px 16px;
            border-bottom: 1px solid var(--border-main);
            font-weight: 700;
            font-size: 14px;
            color: var(--text-heading);
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--bg-dark);
        }

        .sidebar-scroll {
            flex: 1;
            overflow-y: auto;
            padding: 12px 8px;
        }

        .section-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
            padding: 6px 8px;
            font-weight: 700;
        }

        .channel-item, .user-item {
            display: flex;
            align-items: center;
            padding: 6px 10px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
            color: var(--text-primary);
            margin-bottom: 2px;
            gap: 8px;
        }

        .channel-item:hover, .user-item:hover { background: var(--bg-panel); color: var(--text-heading); }
        .channel-item.active { background: var(--accent-blue); color: #ffffff; font-weight: 600; }

        .user-status-dot { width: 7px; height: 7px; background-color: var(--status-green); border-radius: 50%; flex-shrink: 0; }
        .user-badge { margin-left: auto; font-size: 10px; background: rgba(255, 255, 255, 0.1); padding: 1px 5px; border-radius: 4px; color: var(--text-muted); }

        .chat-main {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: var(--bg-dark);
        }

        .chat-header {
            height: 50px;
            border-bottom: 1px solid var(--border-main);
            padding: 0 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--bg-card);
        }

        .chat-header-title {
            display: flex;
            align-items: center;
            gap: 8px;
            color: var(--text-heading);
            font-weight: 700;
            font-size: 16px;
        }

        .chat-messages {
            flex: 1;
            overflow-y: auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
        }

        .msg-line {
            font-size: 13px;
            line-height: 1.5;
            word-break: break-word;
            display: flex;
            gap: 8px;
            align-items: baseline;
        }

        .msg-time { color: var(--text-muted); font-size: 11px; flex-shrink: 0; }
        .msg-sender { font-weight: 600; color: var(--accent-hover); flex-shrink: 0; }
        .msg-sender.me { color: var(--status-green); }
        .msg-text { color: var(--text-primary); }

        .sys-line { font-size: 12px; color: var(--text-muted); font-style: italic; padding: 2px 0; }

        .chat-image {
            max-width: 300px;
            max-height: 300px;
            border-radius: 6px;
            margin-top: 6px;
            border: 1px solid var(--border-main);
            display: block;
            object-fit: contain;
        }

        .attachment-preview {
            display: none;
            padding: 10px 16px;
            background: var(--bg-card);
            border-top: 1px solid var(--border-main);
        }
        .attachment-preview.active {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .attachment-thumb {
            width: 48px;
            height: 48px;
            object-fit: cover;
            border-radius: 4px;
            border: 1px solid var(--border-main);
        }
        .attachment-remove {
            background: var(--bg-panel);
            border: 1px solid var(--border-main);
            color: var(--text-primary);
            border-radius: 4px;
            padding: 4px 10px;
            cursor: pointer;
            font-size: 12px;
            transition: 0.15s;
        }
        .attachment-remove:hover {
            background: #f85149;
            color: white;
            border-color: #f85149;
        }

        .chat-input-area {
            padding: 12px 16px;
            background: var(--bg-card);
            border-top: 1px solid var(--border-main);
        }

        .chat-input-form {
            display: flex;
            gap: 8px;
            margin: 0;
            align-items: center;
        }

        .chat-input-form input[type="text"] {
            flex: 1;
            height: 38px;
            padding: 0 12px;
            background: var(--bg-input);
            border: 1px solid var(--border-main);
            border-radius: 6px;
            color: var(--text-heading);
            font-size: 13px;
            outline: none;
        }

        .chat-input-form input[type="text"]:focus { border-color: var(--accent-blue); }

        .btn-icon {
            padding: 0 12px;
            height: 38px;
            background: var(--bg-panel);
            color: var(--text-primary);
            border: 1px solid var(--border-main);
            border-radius: 6px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: background 0.15s;
        }

        .btn-icon:hover { background: var(--border-main); }
        .btn-icon:disabled { opacity: 0.5; cursor: not-allowed; }

        .chat-input-form button[type="submit"] {
            height: 38px;
            padding: 0 16px;
            background: var(--accent-blue);
            color: #ffffff;
            border: none;
            border-radius: 6px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .chat-input-form button[type="submit"]:hover { background: #388bfd; }

        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #484f58; }
    </style>
</head>
<body>
    <div class="top-bar">
        <div class="top-bar-brand">
            <svg class="icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
            <span>IRC Lite Web</span>
        </div>
        <div class="top-bar-uptime">
            <span class="pulse-dot"></span>
            <span>Uptime: </span>
            <strong id="uptime-counter">00d 00h 00m 00s</strong>
        </div>
    </div>

    <!-- Страница входа -->
    <div class="login-page" id="login-view">
        <div class="login-card">
            <div class="login-card-header">
                <svg class="icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
                <h2>IRC Lite Web</h2>
            </div>
            <p class="subtitle">Защищённый мессенджер без сохранения истории (RAM 512MB)</p>
            
            <form id="login-form" onsubmit="login(event)">
                <div class="form-group">
                    <label class="form-label" for="username">Имя пользователя</label>
                    <input type="text" id="username" class="form-control-custom" placeholder="Hacker99" maxlength="20" required autocomplete="off">
                </div>

                <!-- Новая галочка: Запомнить меня -->
                <div class="checkbox-container">
                    <input type="checkbox" id="remember-me">
                    <label for="remember-me">Запомнить меня (Автоматический вход)</label>
                </div>

                <!-- Галочка соглашения (Текст теперь полностью растягивается) -->
                <div class="checkbox-container" style="margin-bottom: 20px;">
                    <input type="checkbox" id="privacy" required>
                    <label for="privacy">Мне есть 18 лет, я принимаю <a href="javascript:void(0)" onclick="togglePrivacyModal(true)">условия конфиденциальности</a>.</label>
                </div>

                <div class="form-group">
                    <label class="form-label">Защита Cloudflare</label>
                    <div class="turnstile-wrapper">
                        <div class="cf-turnstile" data-sitekey="TURNSTILE_SITEKEY_PLACEHOLDER" data-theme="dark"></div>
                    </div>
                </div>

                <button type="submit" id="login-btn" class="btn-primary-custom">
                    <svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
                    <span>Войти в чат</span>
                </button>
                <div id="login-error" style="color: #f85149; font-size: 12px; margin-top: 12px; text-align: center; font-weight: 600;"></div>
            </form>
        </div>
    </div>

    <!-- Модальное окно политики -->
    <div id="privacy-modal" class="modal-overlay hidden">
        <div class="modal-card">
            <h3>Условия конфиденциальности</h3>
            <p>Наш мессенджер функционирует исключительно в оперативной памяти (RAM) сервера:</p>
            <ul>
                <li>Сообщения и изображения <strong>не сохраняются</strong> на диск.</li>
                <li>История удаляется при перезагрузке, фотографии хранятся во временном буфере.</li>
                <li>Логирование персональных данных не ведется.</li>
            </ul>
            <button type="button" class="btn-primary-custom" onclick="togglePrivacyModal(false)">Понятно</button>
        </div>
    </div>

    <!-- Интерфейс чата -->
    <div class="app-container hidden" id="chat-view">
        <div class="sidebar-left">
            <div class="sidebar-header">
                <svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="9" x2="20" y2="9"></line><line x1="4" y1="15" x2="20" y2="15"></line><line x1="10" y1="3" x2="8" y2="21"></line><line x1="16" y1="3" x2="14" y2="21"></line></svg>
                <span>Каналы</span>
            </div>
            
            <div class="sidebar-scroll">
                <div class="section-title">Публичные</div>
                <div id="channel-list"></div>

                <div class="section-title" style="margin-top: 16px;">Секретная комната</div>
                <div style="padding: 0 6px; margin-bottom: 20px;">
                    <form onsubmit="joinCustomChannel(event)" style="margin:0;">
                        <input type="text" id="custom-channel" class="form-control-custom" placeholder="#секрет" style="padding:6px 8px; font-size:12px; margin-bottom:6px;" maxlength="30" required>
                        <button type="submit" class="btn-primary-custom" style="padding:6px; font-size:12px; background:var(--accent-blue);">
                            <svg class="icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
                            <span>Войти / Создать</span>
                        </button>
                    </form>
                </div>
            </div>
            
            <div style="padding: 12px; border-top: 1px solid var(--border-main); background: var(--bg-dark);">
                <button type="button" class="btn-primary-custom" style="background: transparent; border: 1px solid var(--border-main); color: #f85149;" onclick="doLogout()">
                    <svg class="icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line></svg>
                    <span>Выйти</span>
                </button>
            </div>
        </div>

        <div class="chat-main">
            <div class="chat-header">
                <div class="chat-header-title">
                    <svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="9" x2="20" y2="9"></line><line x1="4" y1="15" x2="20" y2="15"></line><line x1="10" y1="3" x2="8" y2="21"></line><line x1="16" y1="3" x2="14" y2="21"></line></svg>
                    <span id="current-channel-title">general</span>
                </div>
                <div>
                    <span class="user-badge" id="channel-users-count">Участников: 0</span>
                </div>
            </div>

            <div class="chat-messages" id="chat-box">
                <div class="sys-line">*** Добро пожаловать в IRC Lite Web.</div>
            </div>

            <!-- Зона превью прикрепленного фото -->
            <div id="attachment-container" class="attachment-preview">
                <img id="attachment-img" class="attachment-thumb" src="">
                <button type="button" class="attachment-remove" onclick="clearAttachment()">Удалить фото</button>
            </div>

            <div class="chat-input-area">
                <form class="chat-input-form" onsubmit="sendMessage(event)">
                    <!-- Кнопка загрузки картинки -->
                    <input type="file" id="file-input" accept="image/*" class="hidden" onchange="handleFileUpload(event)">
                    <button type="button" class="btn-icon" id="upload-btn" onclick="document.getElementById('file-input').click()" title="Отправить фото">
                        <svg class="icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg>
                    </button>

                    <input type="text" id="message-input" placeholder="Написать сообщение..." maxlength="500" autocomplete="off">
                    <button type="submit">
                        <svg class="icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
                    </button>
                </form>
            </div>
        </div>

        <div class="sidebar-right">
            <div class="sidebar-header">
                <svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg>
                <span>Участники</span>
            </div>

            <div class="sidebar-scroll">
                <div class="section-title">В канале (<span id="count-in-channel">0</span>)</div>
                <div id="users-in-channel-list"></div>

                <div class="section-title" style="margin-top: 20px;">Все онлайн (<span id="count-global">0</span>)</div>
                <div id="users-global-list"></div>
            </div>
        </div>
    </div>

    <script nonce="NONCE_PLACEHOLDER">
        let currentUser = "";
        let sessionToken = "";
        let currentChannel = "#general";
        let ws = null;
        let pendingImageBase64 = null;
        let pingInterval = null;
        let wsStartTime = 0;
        const defaultChannels = DEFAULT_CHANNELS_PLACEHOLDER;
        const serverUptimeSec = SERVER_UPTIME_PLACEHOLDER; 
        const localStartTimestamp = Math.floor(Date.now() / 1000) - serverUptimeSec;

        const hashSvg = `<svg class="icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="9" x2="20" y2="9"></line><line x1="4" y1="15" x2="20" y2="15"></line><line x1="10" y1="3" x2="8" y2="21"></line><line x1="16" y1="3" x2="14" y2="21"></line></svg>`;

        // Авто-логин (Запомнить меня) при загрузке страницы
        window.addEventListener('DOMContentLoaded', () => {
            const savedToken = localStorage.getItem('irc_token');
            const savedUser = localStorage.getItem('irc_user');
            if (savedToken && savedUser) {
                currentUser = savedUser;
                sessionToken = savedToken;
                document.getElementById('login-view').classList.add('hidden');
                document.getElementById('chat-view').classList.remove('hidden');
                renderChannels();
                connectWs(currentChannel);
            }
        });

        function updateUptime() {
            const now = Math.floor(Date.now() / 1000);
            let diff = Math.max(0, now - localStartTimestamp);
            const days = Math.floor(diff / 86400); diff %= 86400;
            const hours = Math.floor(diff / 3600); diff %= 3600;
            const minutes = Math.floor(diff / 60); const seconds = diff % 60;
            const pad = (n) => String(n).padStart(2, '0');
            const uptimeStr = `${days > 0 ? days + 'd ' : ''}${pad(hours)}h ${pad(minutes)}m ${pad(seconds)}s`;
            const elem = document.getElementById('uptime-counter');
            if (elem) elem.innerText = uptimeStr;
        }

        setInterval(updateUptime, 1000);
        updateUptime();

        function togglePrivacyModal(show) {
            const modal = document.getElementById('privacy-modal');
            if (show) modal.classList.remove('hidden');
            else modal.classList.add('hidden');
        }

        async function login(e) {
            e.preventDefault();
            const btn = document.getElementById('login-btn');
            const errorSpan = document.getElementById('login-error');
            const username = document.getElementById('username').value.trim();
            const turnstileElem = document.querySelector('[name="cf-turnstile-response"]');
            const turnstileResponse = turnstileElem ? turnstileElem.value : "";

            if (!turnstileResponse) {
                errorSpan.innerText = "Пожалуйста, пройдите проверку капчи.";
                return;
            }

            btn.disabled = true;
            errorSpan.innerText = "Проверка...";

            const formData = new FormData();
            formData.append('username', username);
            formData.append('cf_turnstile_response', turnstileResponse);

            try {
                const res = await fetch('/login', { method: 'POST', body: formData });
                const data = await res.json();

                if (data.status === "ok") {
                    currentUser = data.username;
                    sessionToken = data.token;
                    
                    // Сохранение сессии (Запомнить меня)
                    if (document.getElementById('remember-me').checked) {
                        localStorage.setItem('irc_token', sessionToken);
                        localStorage.setItem('irc_user', currentUser);
                    } else {
                        localStorage.removeItem('irc_token');
                        localStorage.removeItem('irc_user');
                    }

                    document.getElementById('login-view').classList.add('hidden');
                    document.getElementById('chat-view').classList.remove('hidden');
                    renderChannels();
                    connectWs(currentChannel);
                } else {
                    errorSpan.innerText = data.message || "Ошибка входа.";
                    btn.disabled = false;
                }
            } catch (err) {
                errorSpan.innerText = "Ошибка соединения.";
                btn.disabled = false;
            }
        }

        function renderChannels() {
            const list = document.getElementById('channel-list');
            list.innerHTML = "";
            defaultChannels.forEach(ch => {
                const div = document.createElement('div');
                div.className = "channel-item" + (ch === currentChannel ? " active" : "");
                div.innerHTML = `${hashSvg} <span>${ch.replace('#', '')}</span>`;
                div.onclick = () => switchChannel(ch);
                list.appendChild(div);
            });
        }

        function switchChannel(channelName) {
            if (!channelName.startsWith('#')) channelName = '#' + channelName;
            if (channelName === currentChannel) return;
            
            currentChannel = channelName;
            document.getElementById('current-channel-title').innerText = currentChannel.replace('#', '');
            
            const chatBox = document.getElementById('chat-box');
            chatBox.innerHTML = `<div class="sys-line">*** Переход в канал ${escapeHtml(currentChannel)}...</div>`;
            
            renderChannels();
            connectWs(currentChannel);
        }

        function joinCustomChannel(e) {
            e.preventDefault();
            const customInput = document.getElementById('custom-channel');
            let ch = customInput.value.trim();
            if (ch) {
                customInput.value = "";
                switchChannel(ch);
            }
        }

        function connectWs(channel) {
            if (ws) ws.close();
            
            const protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
            const wsUrl = protocol + window.location.host + "/ws/" + encodeURIComponent(sessionToken) + "/" + encodeURIComponent(channel);
            
            ws = new WebSocket(wsUrl);
            wsStartTime = Date.now();
            
            ws.onopen = function() {
                if (pingInterval) clearInterval(pingInterval);
                // Отправка пинга каждые 20 секунд для предотвращения разрыва соединения
                pingInterval = setInterval(() => {
                    if (ws && ws.readyState === WebSocket.OPEN) {
                        ws.send(JSON.stringify({ type: "ping" }));
                    }
                }, 20000);
            };

            ws.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === "system" && data.text === "AUTH_ERROR") {
                        forceLogout("Сессия устарела. Пожалуйста, войдите заново.");
                        return;
                    }
                    handleIncomingPacket(data);
                } catch(e) {}
            };
            
            ws.onclose = function(e) {
                if (pingInterval) clearInterval(pingInterval);
                
                // Если закрылось почти сразу (ошибка авторизации или бан)
                if (Date.now() - wsStartTime < 2000 || e.code === 4001 || e.code === 403) {
                    forceLogout("Ошибка сессии. Пожалуйста, авторизуйтесь заново.");
                } else if (e.code === 4002) {
                    appendSystemMessage("*** Превышен лимит подключений.");
                } else {
                    appendSystemMessage("*** Соединение потеряно.");
                }
            };
        }

        function forceLogout(errorMsg = null) {
            localStorage.removeItem('irc_token');
            localStorage.removeItem('irc_user');
            document.getElementById('chat-view').classList.add('hidden');
            document.getElementById('login-view').classList.remove('hidden');
            
            if (errorMsg) {
                document.getElementById('login-error').innerText = errorMsg;
            }
            
            if (ws) {
                ws.close();
                ws = null;
            }
        }

        async function doLogout() {
            try {
                const fd = new FormData();
                fd.append('token', sessionToken);
                await fetch('/logout', { method: 'POST', body: fd });
            } catch (e) {}
            forceLogout();
        }

        function handleIncomingPacket(packet) {
            if (packet.type === "message") {
                appendMessage(packet.username, packet.text, packet.timestamp, packet.image_b64);
            } else if (packet.type === "system") {
                appendSystemMessage("*** " + packet.text);
            } else if (packet.type === "presence") {
                updateUserLists(packet.channel_users, packet.global_users);
            }
        }

        function appendMessage(sender, text, timestamp, image_b64) {
            const chatBox = document.getElementById('chat-box');
            const isMe = sender === currentUser;
            const row = document.createElement('div');
            row.className = "msg-line";

            let contentHtml = `<span class="msg-text">${escapeHtml(text)}</span>`;
            if (image_b64) {
                contentHtml += `<br><img src="${image_b64}" class="chat-image" onload="scrollToBottom()">`;
            }

            row.innerHTML = `
                <span class="msg-time">[${timestamp}]</span>
                <span class="msg-sender ${isMe ? 'me' : ''}">&lt;${escapeHtml(sender)}&gt;</span>
                <div style="display:inline-block; flex:1;">${contentHtml}</div>
            `;

            chatBox.appendChild(row);
            scrollToBottom();
        }

        function appendSystemMessage(text) {
            const chatBox = document.getElementById('chat-box');
            const div = document.createElement('div');
            div.className = "sys-line";
            div.innerText = text;
            chatBox.appendChild(div);
            scrollToBottom();
        }

        function scrollToBottom() {
            const chatBox = document.getElementById('chat-box');
            chatBox.scrollTop = chatBox.scrollHeight;
        }

        function updateUserLists(channelUsers, globalUsers) {
            const channelUsersBox = document.getElementById('users-in-channel-list');
            document.getElementById('count-in-channel').innerText = channelUsers.length;
            document.getElementById('channel-users-count').innerText = `Участников: ${channelUsers.length}`;
            
            channelUsersBox.innerHTML = "";
            channelUsers.forEach(u => {
                const div = document.createElement('div');
                div.className = "user-item";
                div.innerHTML = `<span class="user-status-dot"></span><span>${escapeHtml(u)}</span>${u === currentUser ? '<span class="user-badge">вы</span>' : ''}`;
                channelUsersBox.appendChild(div);
            });

            const globalUsersBox = document.getElementById('users-global-list');
            document.getElementById('count-global').innerText = globalUsers.length;
            
            globalUsersBox.innerHTML = "";
            globalUsers.forEach(u => {
                const div = document.createElement('div');
                div.className = "user-item";
                div.innerHTML = `<span class="user-status-dot" style="background:#58a6ff;"></span><span>${escapeHtml(u)}</span>`;
                globalUsersBox.appendChild(div);
            });
        }

        function sendMessage(e) {
            e.preventDefault();
            const input = document.getElementById('message-input');
            const msg = input.value.trim();
            if ((msg || pendingImageBase64) && ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: "chat", text: msg, image_b64: pendingImageBase64 }));
                input.value = "";
                clearAttachment();
            }
        }

        function clearAttachment() {
            pendingImageBase64 = null;
            document.getElementById('attachment-container').classList.remove('active');
            document.getElementById('attachment-img').src = "";
            document.getElementById('file-input').value = "";
        }

        // Захват файла и локальное сжатие
        function handleFileUpload(event) {
            const file = event.target.files[0];
            if (!file) return;
            
            const reader = new FileReader();
            reader.onload = function(e) {
                const img = new Image();
                img.onload = function() {
                    const canvas = document.createElement('canvas');
                    let width = img.width;
                    let height = img.height;

                    // Ограничение размера до 512x512 для передачи по WebSocket
                    const maxSize = 512;
                    if (width > maxSize || height > maxSize) {
                        if (width > height) {
                            height = Math.round((height * maxSize) / width);
                            width = maxSize;
                        } else {
                            width = Math.round((width * maxSize) / height);
                            height = maxSize;
                        }
                    }

                    canvas.width = width;
                    canvas.height = height;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(img, 0, 0, width, height);

                    // Конвертируем в JPEG, качество 0.8 (вес будет около 20-60кб)
                    pendingImageBase64 = canvas.toDataURL('image/jpeg', 0.8);
                    
                    document.getElementById('attachment-img').src = pendingImageBase64;
                    document.getElementById('attachment-container').classList.add('active');
                };
                img.src = e.target.result;
            };
            reader.readAsDataURL(file);
        }

        function escapeHtml(str) {
            return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
        }
    </script>
</body>
</html>
"""

app = FastAPI()

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        
        # CSP устанавливается в маршрутах для поддержки динамического Nonce, 
        # но если его нет, ставим базовый fallback
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' https://challenges.cloudflare.com; "
                "style-src 'self'; "
                "frame-src https://challenges.cloudflare.com; "
                "img-src 'self' data: blob:; "
                "connect-src 'self' ws: wss:; "
                "frame-ancestors 'none';"
            )
            
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

app.add_middleware(SecurityHeadersMiddleware)

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, dict[WebSocket, str]] = {
            ch: {} for ch in DEFAULT_CHANNELS
        }

    async def connect(self, websocket: WebSocket, channel: str, username: str):
        await websocket.accept()
        if channel not in self.active_connections:
            self.active_connections[channel] = {}
        self.active_connections[channel][websocket] = username
        
        await self.broadcast_json({
            "type": "system",
            "text": f"Пользователь {username} вошел в канал."
        }, channel)
        
        await self.broadcast_presence(channel)

    async def disconnect(self, websocket: WebSocket, channel: str, username: str):
        if channel in self.active_connections:
            if websocket in self.active_connections[channel]:
                del self.active_connections[channel][websocket]
            
            if channel not in DEFAULT_CHANNELS and len(self.active_connections[channel]) == 0:
                del self.active_connections[channel]
            else:
                await self.broadcast_json({
                    "type": "system",
                    "text": f"Пользователь {username} покинул канал."
                }, channel)
                await self.broadcast_presence(channel)

    def get_channel_users(self, channel: str) -> list[str]:
        if channel in self.active_connections:
            return list(self.active_connections[channel].values())
        return []

    def get_global_users(self) -> list[str]:
        all_users = set()
        for ch_conns in self.active_connections.values():
            all_users.update(ch_conns.values())
        return sorted(list(all_users))

    async def broadcast_presence(self, channel: str):
        global_users = self.get_global_users()
        channel_users = self.get_channel_users(channel)

        await self.broadcast_json({
            "type": "presence",
            "channel_users": channel_users,
            "global_users": global_users
        }, channel)

    async def broadcast_json(self, data: dict, channel: str):
        if channel in self.active_connections:
            dead_sockets = []
            for ws in list(self.active_connections[channel].keys()):
                try:
                    await ws.send_json(data)
                except Exception:
                    dead_sockets.append(ws)
            
            for ws in dead_sockets:
                if ws in self.active_connections[channel]:
                    del self.active_connections[channel][ws]

manager = ConnectionManager()

@app.get("/")
async def get_home():
    nonce = secrets.token_urlsafe(16)
    uptime_sec = int(time.time() - datetime.datetime.fromisoformat(SERVER_START_TIME).timestamp())
    
    html = HTML_CONTENT.replace("TURNSTILE_SITEKEY_PLACEHOLDER", TURNSTILE_SITEKEY)
    html = html.replace("DEFAULT_CHANNELS_PLACEHOLDER", json.dumps(DEFAULT_CHANNELS))
    html = html.replace("SERVER_UPTIME_PLACEHOLDER", str(uptime_sec))
    html = html.replace("NONCE_PLACEHOLDER", nonce)
    
    response = HTMLResponse(html)
    
    # Строгий CSP с Nonce для устранения уязвимости unsafe-inline
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' https://challenges.cloudflare.com; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "frame-src https://challenges.cloudflare.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none';"
    )
    return response

@app.post("/logout")
async def logout_endpoint(token: str = Form(...)):
    if token in VALID_SESSIONS:
        del VALID_SESSIONS[token]
    return {"status": "ok"}

@app.post("/login")
async def login(request: Request, username: str = Form(...), cf_turnstile_response: str = Form(...)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    
    username = username.strip()
    if not NICK_REGEX.match(username):
        return {"status": "error", "message": "Никнейм должен содержать 2-20 символов."}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data={
                "secret": TURNSTILE_SECRET,
                "response": cf_turnstile_response,
                "remoteip": client_ip
            }, timeout=5.0)
            data = resp.json()
            if not data.get("success"):
                return {"status": "error", "message": "Капча Cloudflare не пройдена."}
        except Exception:
            return {"status": "error", "message": "Ошибка соединения с Cloudflare."}

    now = time.time()
    expired = [t for t, s in VALID_SESSIONS.items() if now - s["created_at"] > MAX_SESSION_AGE]
    for t in expired:
        del VALID_SESSIONS[t]

    token = secrets.token_hex(16)
    VALID_SESSIONS[token] = {
        "username": username,
        "created_at": now,
        "ip": client_ip
    }

    return {"status": "ok", "username": username, "token": token}

def get_hashed_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode() + SALT_IP).hexdigest()

@app.websocket("/ws/{token}/{channel}")
async def websocket_endpoint(websocket: WebSocket, token: str, channel: str):
    # Принимаем соединение СРАЗУ, чтобы избежать жесткой ошибки HTTP 403.
    # Если сессия неверна, мы закроем его корректным кодом.
    await websocket.accept()

    session = VALID_SESSIONS.get(token)
    if not session:
        await websocket.send_json({"type": "system", "text": "AUTH_ERROR"})
        await websocket.close(code=4001, reason="Unauthorized session token")
        return
    
    username = session["username"]
    raw_ip = websocket.client.host if websocket.client else "127.0.0.1"
    hashed_ip = get_hashed_ip(raw_ip) # Хэшируем IP для безопасности
    
    current_conns = IP_CONNECTIONS.get(hashed_ip, 0)
    if current_conns >= MAX_CONNS_PER_IP:
        await websocket.close(code=4002, reason="Too many connections")
        return

    channel = channel.strip().lower()
    if not channel.startswith("#"):
        channel = "#" + channel
    if not CHANNEL_REGEX.match(channel):
        channel = "#general"

    IP_CONNECTIONS[hashed_ip] = current_conns + 1
    MESSAGE_TIMESTAMPS[websocket] = []

    await manager.connect(websocket, channel, username)
    try:
        while True:
            raw_data = await websocket.receive_text()
            
            now = time.time()
            timestamps = MESSAGE_TIMESTAMPS.get(websocket, [])
            timestamps = [t for t in timestamps if now - t < 3.0]
            if len(timestamps) >= 5:
                await websocket.send_json({
                    "type": "system",
                    "text": "⚠️ Слишком частые сообщения. Подождите 3 секунды."
                })
                continue
            
            timestamps.append(now)
            MESSAGE_TIMESTAMPS[websocket] = timestamps

            try:
                packet = json.loads(raw_data)
                
                # Обработка heartbeats (пингов) для удержания соединения
                if packet.get("type") == "ping":
                    continue
                    
                text = packet.get("text", "").strip()
                image_b64 = packet.get("image_b64", "")
                
                if text or image_b64:
                    text = text[:500]
                    text = "".join(ch for ch in text if ch.isprintable() or ch in "\n\r\t")
                    now_str = datetime.datetime.now().strftime("%H:%M")
                    await manager.broadcast_json({
                        "type": "message",
                        "username": username,
                        "text": text,
                        "image_b64": image_b64,
                        "timestamp": now_str
                    }, channel)
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        pass
    finally:
        if websocket in MESSAGE_TIMESTAMPS:
            del MESSAGE_TIMESTAMPS[websocket]
        if hashed_ip in IP_CONNECTIONS:
            IP_CONNECTIONS[hashed_ip] = max(0, IP_CONNECTIONS[hashed_ip] - 1)
        await manager.disconnect(websocket, channel, username)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
