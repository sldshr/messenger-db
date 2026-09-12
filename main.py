import asyncio
import smtplib
import uvicorn
import dns.resolver
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
from email.utils import make_msgid
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from aiosmtpd.controller import Controller
from contextlib import asynccontextmanager

# База данных в оперативной памяти
# Формат: {"username": [{"id": 1, "from": "...", "to": "...", "subject": "...", "body": "...", "date": "..."}]}
MAILBOX = {}
MESSAGE_COUNTER = 1
LOCAL_DOMAIN = "0.0.0.0" # Поменяй на свой IP, если сервер смотрит в интернет (например "123.45.67.89")

# Модели Pydantic для API
class EmailSendRequest(BaseModel):
    from_user: str # Например 'admin' (будет превращено в admin@LOCAL_DOMAIN)
    to_address: str # Куда отправляем (может быть gmail.com)
    subject: str
    body: str

class EmailMessageModel(BaseModel):
    id: int
    from_address: str
    to_address: str
    subject: str
    body: str
    date: str

def deliver_message_locally(to_user: str, message_data: dict):
    """Доставляет письмо в локальный словарь пользователя"""
    global MESSAGE_COUNTER
    if to_user not in MAILBOX:
        MAILBOX[to_user] = []
    
    msg_copy = message_data.copy()
    msg_copy["id"] = MESSAGE_COUNTER
    MESSAGE_COUNTER += 1
    
    # Добавляем в начало списка (новые сверху)
    MAILBOX[to_user].insert(0, msg_copy)
    print(f"[LOCAL] Письмо доставлено локальному пользователю: {to_user}")

def send_external_email(from_addr: str, to_addr: str, subject: str, body: str):
    """Отправляет письмо на внешний домен (например, Gmail) путем поиска MX записей"""
    try:
        domain = to_addr.split('@')[1]
        
        # Получаем MX запись для домена получателя (адрес почтового сервера)
        answers = dns.resolver.resolve(domain, 'MX')
        mx_record = str(answers[0].exchange)
        print(f"[SMTP] Найден MX сервер для {domain}: {mx_record}")

        # Формируем письмо
        msg = EmailMessage()
        msg.set_content(body)
        msg['Subject'] = subject
        msg['From'] = from_addr
        msg['To'] = to_addr
        msg['Message-ID'] = make_msgid(domain=from_addr.split('@')[1])

        # Отправляем через SMTP
        with smtplib.SMTP(mx_record, 25) as server:
            # Для отладки можно раскомментировать следующую строку
            # server.set_debuglevel(1)
            server.send_message(msg)
        print(f"[SMTP] Письмо успешно отправлено на {to_addr}")
    except Exception as e:
        print(f"[ERROR] Ошибка отправки письма на {to_addr}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Ошибка отправки на внешний сервер: {str(e)}")

class LocalSMTPHandler:
    """Обрабатывает входящие SMTP подключения на наш сервер"""
    async def handle_DATA(self, server, session, envelope):
        try:
            # Парсим входящее письмо
            msg = BytesParser(policy=policy.default).parsebytes(envelope.content)
            
            # Извлекаем данные
            from_addr = envelope.mail_from
            subject = msg['subject'] if msg['subject'] else "(Без темы)"
            body = msg.get_body(preferencelist=('plain')).get_content() if msg.get_body(preferencelist=('plain')) else ""
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            for rcpt in envelope.rcpt_tos:
                print(f"[SMTP-IN] Получено письмо для: {rcpt}")
                if f"@{LOCAL_DOMAIN}" in rcpt:
                    # Извлекаем имя пользователя (все до @)
                    username = rcpt.split('@')[0]
                    message_data = {
                        "from_address": from_addr,
                        "to_address": rcpt,
                        "subject": subject,
                        "body": body,
                        "date": date_str
                    }
                    deliver_message_locally(username, message_data)
                else:
                    print(f"[SMTP-IN] Игнорируем письмо для чужого домена: {rcpt}")

            return '250 Message accepted for delivery'
        except Exception as e:
            print(f"[SMTP-IN ERROR] {str(e)}")
            return '500 Could not process your message'

# Запуск и остановка SMTP сервера вместе с FastAPI
smtp_controller = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global smtp_controller
    # Запускаем SMTP сервер на порту 2525 (или 25, если есть права суперпользователя)
    # Если хочешь принимать реальные письма из интернета, нужно использовать порт 25 и белый IP
    smtp_controller = Controller(LocalSMTPHandler(), hostname='0.0.0.0', port=2525)
    smtp_controller.start()
    print("[SYSTEM] Внутренний SMTP сервер запущен на порту 2525")
    yield
    smtp_controller.stop()
    print("[SYSTEM] Внутренний SMTP сервер остановлен")

app = FastAPI(lifespan=lifespan, title="PyMail Server")

@app.get("/api/messages/{username}", response_model=List[EmailMessageModel])
def get_messages(username: str):
    """Возвращает список писем для указанного пользователя"""
    return MAILBOX.get(username, [])

@app.post("/api/send")
def send_message(req: EmailSendRequest):
    """Отправляет письмо (внутреннее или внешнее)"""
    from_full_address = f"{req.from_user}@{LOCAL_DOMAIN}"
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Сохраняем исходящее письмо у отправителя для истории
    sent_msg_data = {
        "from_address": from_full_address,
        "to_address": req.to_address,
        "subject": req.subject,
        "body": req.body,
        "date": date_str
    }
    deliver_message_locally(req.from_user, sent_msg_data)

    # Проверяем, локальная это доставка или внешняя
    if req.to_address.endswith(f"@{LOCAL_DOMAIN}"):
        to_user = req.to_address.split('@')[0]
        deliver_message_locally(to_user, sent_msg_data)
        return {"status": "success", "message": "Локальное письмо доставлено"}
    else:
        # Внешняя доставка (например, на Gmail)
        send_external_email(from_full_address, req.to_address, req.subject, req.body)
        return {"status": "success", "message": "Письмо отправлено на внешний сервер"}

@app.post("/api/reset")
def reset_server():
    """Сбрасывает всю оперативную память (удаляет все письма)"""
    global MAILBOX, MESSAGE_COUNTER
    MAILBOX.clear()
    MESSAGE_COUNTER = 1
    return {"status": "success", "message": "ОЗУ очищена, все письма удалены"}

# Веб-интерфейс, встроенный прямо в приложение
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PyMail - Локальный Почтовик</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; background-color: #f3f4f6; }
        .tab-active { border-bottom: 2px solid #3b82f6; color: #1d4ed8; font-weight: 600; }
        .tab-inactive { color: #6b7280; }
        .tab-inactive:hover { color: #374151; }
    </style>
</head>
<body class="h-screen flex flex-col">

    <!-- Navbar -->
    <header class="bg-white shadow-sm px-6 py-4 flex justify-between items-center">
        <div class="flex items-center gap-2">
            <div class="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center text-white font-bold text-xl">M</div>
            <h1 class="text-xl font-bold text-gray-800">PyMail</h1>
        </div>
        <div class="flex items-center gap-4">
            <span class="text-sm text-gray-500">Домен сервера: <strong id="server-domain"></strong></span>
            <button onclick="resetServer()" class="bg-red-500 hover:bg-red-600 text-white px-4 py-2 rounded-md text-sm font-medium transition-colors shadow-sm">
                Сбросить ОЗУ
            </button>
        </div>
    </header>

    <div class="flex flex-1 overflow-hidden">
        <!-- Sidebar / Auth -->
        <div class="w-64 bg-white border-r border-gray-200 p-6 flex flex-col gap-6">
            <div>
                <label class="block text-sm font-semibold text-gray-700 mb-2">Твой Username</label>
                <div class="flex border rounded-md overflow-hidden focus-within:ring-2 focus-within:ring-blue-500 transition-shadow">
                    <input type="text" id="username" placeholder="admin" value="admin" class="w-full px-3 py-2 outline-none" onkeyup="loadEmails()">
                </div>
                <p class="text-xs text-gray-500 mt-2">Твой адрес: <span id="full-address" class="font-mono bg-gray-100 px-1 rounded">admin@localhost</span></p>
            </div>
            
            <nav class="flex flex-col gap-2">
                <button onclick="switchTab('inbox')" id="btn-inbox" class="flex items-center gap-3 px-3 py-2 rounded-md bg-blue-50 text-blue-700 font-medium transition-colors">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"></path></svg>
                    Входящие / История
                </button>
                <button onclick="switchTab('compose')" id="btn-compose" class="flex items-center gap-3 px-3 py-2 rounded-md hover:bg-gray-100 text-gray-700 font-medium transition-colors">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"></path></svg>
                    Написать письмо
                </button>
            </nav>
        </div>

        <!-- Main Content -->
        <main class="flex-1 bg-gray-50 overflow-y-auto p-8 relative">
            
            <!-- Toast Notification -->
            <div id="toast" class="absolute top-4 right-8 bg-gray-800 text-white px-4 py-2 rounded shadow-lg transform transition-all duration-300 translate-y-[-150%] opacity-0 z-50">
                Уведомление
            </div>

            <!-- Inbox View -->
            <div id="view-inbox" class="block max-w-4xl mx-auto">
                <h2 class="text-2xl font-bold text-gray-800 mb-6 flex items-center gap-3">
                    Почтовый ящик
                    <button onclick="loadEmails()" class="text-sm font-normal text-blue-600 hover:underline cursor-pointer bg-blue-100 px-2 py-1 rounded-md">Обновить</button>
                </h2>
                
                <div id="emails-container" class="space-y-4">
                    <!-- Загрузка писем -->
                    <p class="text-gray-500">Загрузка...</p>
                </div>
            </div>

            <!-- Compose View -->
            <div id="view-compose" class="hidden max-w-3xl mx-auto bg-white p-8 rounded-xl shadow-sm border border-gray-100">
                <h2 class="text-2xl font-bold text-gray-800 mb-6">Новое письмо</h2>
                <form id="compose-form" onsubmit="sendEmail(event)" class="flex flex-col gap-5">
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1">Кому (можно на gmail)</label>
                        <input type="email" id="to_address" required placeholder="example@gmail.com или test@localhost" class="w-full px-4 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none transition-shadow">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1">Тема</label>
                        <input type="text" id="subject" required placeholder="Важное сообщение" class="w-full px-4 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none transition-shadow">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1">Сообщение</label>
                        <textarea id="body" required rows="8" placeholder="Текст письма..." class="w-full px-4 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none transition-shadow resize-none"></textarea>
                    </div>
                    <div class="flex justify-end pt-2">
                        <button type="submit" id="send-btn" class="bg-blue-600 hover:bg-blue-700 text-white px-6 py-2.5 rounded-md font-medium transition-colors shadow-sm flex items-center gap-2">
                            <span>Отправить</span>
                            <svg class="w-4 h-4 transform rotate-45 mb-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"></path></svg>
                        </button>
                    </div>
                </form>
            </div>

        </main>
    </div>

    <!-- Application Logic -->
    <script>
        const LOCAL_DOMAIN = "localhost"; // Отражение переменной из Python
        document.getElementById('server-domain').textContent = LOCAL_DOMAIN;

        function showToast(message, isError = false) {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.className = `absolute top-4 right-8 px-4 py-2 rounded shadow-lg transform transition-all duration-300 z-50 ${isError ? 'bg-red-500 text-white' : 'bg-gray-800 text-white'}`;
            toast.style.transform = 'translateY(0)';
            toast.style.opacity = '1';
            setTimeout(() => {
                toast.style.transform = 'translateY(-150%)';
                toast.style.opacity = '0';
            }, 3000);
        }

        // Обновление отображаемого адреса при вводе username
        document.getElementById('username').addEventListener('input', (e) => {
            const user = e.target.value.trim() || 'anonymous';
            document.getElementById('full-address').textContent = `${user}@${LOCAL_DOMAIN}`;
        });

        function switchTab(tabName) {
            document.getElementById('view-inbox').classList.add('hidden');
            document.getElementById('view-compose').classList.add('hidden');
            
            document.getElementById('btn-inbox').className = 'flex items-center gap-3 px-3 py-2 rounded-md hover:bg-gray-100 text-gray-700 font-medium transition-colors';
            document.getElementById('btn-compose').className = 'flex items-center gap-3 px-3 py-2 rounded-md hover:bg-gray-100 text-gray-700 font-medium transition-colors';

            if (tabName === 'inbox') {
                document.getElementById('view-inbox').classList.remove('hidden');
                document.getElementById('btn-inbox').className = 'flex items-center gap-3 px-3 py-2 rounded-md bg-blue-50 text-blue-700 font-medium transition-colors';
                loadEmails();
            } else {
                document.getElementById('view-compose').classList.remove('hidden');
                document.getElementById('btn-compose').className = 'flex items-center gap-3 px-3 py-2 rounded-md bg-blue-50 text-blue-700 font-medium transition-colors';
            }
        }

        async function loadEmails() {
            const username = document.getElementById('username').value.trim() || 'admin';
            const container = document.getElementById('emails-container');
            
            try {
                const res = await fetch(`/api/messages/${username}`);
                const emails = await res.json();
                
                if (emails.length === 0) {
                    container.innerHTML = `
                        <div class="bg-white p-10 rounded-xl shadow-sm text-center border border-gray-100">
                            <svg class="w-16 h-16 text-gray-300 mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path></svg>
                            <h3 class="text-lg font-medium text-gray-900">Писем пока нет</h3>
                            <p class="text-gray-500 mt-1">Тут появятся все входящие и исходящие сообщения.</p>
                        </div>
                    `;
                    return;
                }

                container.innerHTML = emails.map(email => `
                    <div class="bg-white p-5 rounded-xl shadow-sm border border-gray-100 hover:shadow-md transition-shadow">
                        <div class="flex justify-between items-start mb-2">
                            <div>
                                <span class="font-bold text-gray-900">${email.subject}</span>
                            </div>
                            <span class="text-xs text-gray-400 font-mono">${email.date}</span>
                        </div>
                        <div class="text-sm text-gray-600 mb-3 flex flex-col gap-1">
                            <div><span class="text-gray-400">От:</span> <span class="bg-gray-100 px-1 rounded font-mono text-xs">${email.from_address}</span></div>
                            <div><span class="text-gray-400">Кому:</span> <span class="bg-gray-100 px-1 rounded font-mono text-xs">${email.to_address}</span></div>
                        </div>
                        <div class="text-gray-800 text-sm whitespace-pre-wrap bg-gray-50 p-4 rounded-lg border border-gray-100">${email.body}</div>
                    </div>
                `).join('');

            } catch (error) {
                console.error("Ошибка загрузки писем", error);
                showToast("Ошибка загрузки писем", true);
            }
        }

        async function sendEmail(e) {
            e.preventDefault();
            const btn = document.getElementById('send-btn');
            const originalText = btn.innerHTML;
            btn.innerHTML = 'Отправка...';
            btn.disabled = true;

            const from_user = document.getElementById('username').value.trim() || 'admin';
            const to_address = document.getElementById('to_address').value.trim();
            const subject = document.getElementById('subject').value.trim();
            const body = document.getElementById('body').value.trim();

            try {
                const res = await fetch('/api/send', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ from_user, to_address, subject, body })
                });
                
                const data = await res.json();
                
                if (res.ok) {
                    showToast(data.message);
                    document.getElementById('compose-form').reset();
                    setTimeout(() => switchTab('inbox'), 500);
                } else {
                    showToast(data.detail || "Ошибка отправки", true);
                }
            } catch (error) {
                showToast("Сетевая ошибка", true);
            } finally {
                btn.innerHTML = originalText;
                btn.disabled = false;
            }
        }

        async function resetServer() {
            try {
                const res = await fetch('/api/reset', { method: 'POST' });
                const data = await res.json();
                showToast(data.message);
                loadEmails();
            } catch (error) {
                showToast("Ошибка сброса", true);
            }
        }

        // Инициализация
        document.getElementById('full-address').textContent = `${document.getElementById('username').value || 'admin'}@${LOCAL_DOMAIN}`;
        loadEmails();
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def serve_html():
    """Отдает HTML интерфейс при заходе на корень сайта"""
    return HTML_TEMPLATE

if __name__ == "__main__":
    # Запуск сервера
    print("\n--- Запуск PyMail ---")
    print("Веб-интерфейс: http://localhost:8000")
    print("---------------------\n")
    uvicorn.run("mail_server:app", host="0.0.0.0", port=8000, reload=False)
