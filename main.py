import asyncio
import gc
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# НАСТРОЙКИ / КОНСТАНТЫ
# ---------------------------------------------------------------------------

# Чанк, которым читаем поток и грузим в Hugging Face (10 МБ — компромисс между
# числом HTTP-запросов и потреблением RAM). Единственный крупный буфер в памяти.
CHUNK_SIZE = 10 * 1024 * 1024  # 10 МБ

# Лимиты структур данных в памяти, чтобы не переполнить 512 МБ RAM.
MAX_LOGS = 200         # сколько строк лога храним (для /api/logs; отдаём последние 50)
MAX_QUEUE_RECORDS = 200  # максимум записей истории очереди
MAX_VIDEOS = 500       # максимум записей в «базе данных» (список загруженных видео)

# Время в секундах, через которое активную задачу считаем «зависшей» (не используется
# для остановки, только чтобы следить за статусом — на будущее).
TASK_STALE_SECONDS = 60 * 60 * 6

# ENDPOINT Hugging Face (можно переопределить через env для private hub).
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co")

# REPO_ID и токен читаем из окружения. Если их нет — сервер всё равно стартует,
# но задачи на загрузку будут падать с понятной ошибкой (см. check_hf_config).
REPO_ID = os.environ.get("REPO_ID", "").strip()
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()

# Под каким именем храним файлы видео в датасете (префикс).
VIDEO_FOLDER = "videos"

# ---------------------------------------------------------------------------
# ЛОГИРОВАНИЕ И «ПАМЯТНЫЙ» ЛОГ
# ---------------------------------------------------------------------------

# Кольцевой буфер логов в оперативке (deque с лимитом MAX_LOGS).
# Отдаём последние 50 строк через /api/logs.
mem_logs: deque = deque(maxlen=MAX_LOGS)


def log(level, message):
    """Пишем в stdout + дублируем в память (для API /api/logs)."""
    text = str(message)
    ts = datetime.now().strftime("%H:%M:%S")
    _ts_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{_ts_full}] [{level.upper()}] {text}")
    # Храним как [время, уровень, текст] — лёгкая структура. Уровень — для подсветки.
    mem_logs.append([ts, level.upper(), text])


# ---------------------------------------------------------------------------
# АНАЛИТИКА В ПАМЯТИ
# ---------------------------------------------------------------------------

SERVER_START = time.time()  # unix-таймстамп старта сервера (для аптайма)


class MemoryStore:
    """Единая «база данных» приложения, живущая в RAM.

    Все поля обычные списки/словари с лимитами. Никаких СУБД.
    """

    def __init__(self):
        # Счётчики по всем задачам (валидные и ошибочные).
        self.total_attempts = 0
        self.successful_videos = 0       # число успешно загруженных видео
        self.failed_videos = 0           # число ошибок
        self.total_bytes_uploaded = 0    # суммарный объём в байтах (все попытки)
        self.total_bytes_success = 0     # суммарный объём успешно загруженных
        self.elapsed_seconds = 0.0       # суммарное время скачивания (все попытки)

        # История загрузок по дням: dict "YYYY-MM-DD" -> число видео за этот день.
        self.daily_counts = {}

        # История очереди (для отображения «что было/запланировано»).
        self.queue_history = deque(maxlen=MAX_QUEUE_RECORDS)

        # «База данных» успешно загруженных видео.
        self.videos = deque(maxlen=MAX_VIDEOS)

    # -- helpers ------------------------------------------------------------
    def record_success(self, entry):
        self.successful_videos += 1
        day = datetime.now().strftime("%Y-%m-%d")
        self.daily_counts[day] = self.daily_counts.get(day, 0) + 1
        self.videos.append(entry)

    def record_attempt(self):
        self.total_attempts += 1

    def record_failure(self):
        self.failed_videos += 1


store = MemoryStore()

# ---------------------------------------------------------------------------
# ОЧЕРЕДЬ ЗАДАЧ (актуальные задачи + очередь)
# ---------------------------------------------------------------------------

# asyncio.Queue для строго последовательной обработки (по одному видео за раз).
task_queue: asyncio.Queue = asyncio.Queue()

# Текущая активная задача (одна штука) — None, если ничего не качается.
current_task = None

# Задачи, ожидающие в очереди (для /api/status). Это список dict-«заглушек»,
# которые дублируют содержимое asyncio.Queue, но доступны из API синхронно.
pending_tasks = []

class RequestBody(BaseModel):
    """Общая схема тела запроса для POST-эндпоинтов."""
    url: str = ""
    format_id: str = ""


# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ---------------------------------------------------------------------------

def check_hf_config():
    """Проверяем, что заданы REPO_ID и HF_TOKEN. Иначе — понятная ошибка."""
    if not REPO_ID:
        raise HTTPException(status_code=400, detail="REPO_ID не задан в переменных окружения")
    if not HF_TOKEN:
        raise HTTPException(status_code=400, detail="HF_TOKEN не задан в переменных окружения")


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """Делаем безопасное имя файла из названия видео (без запрещённых символов)."""
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    name = re.sub(r"\s+", "_", name)
    if not name:
        name = "video"
    return name[:max_len]


def _extract_direct_url(info, format_id):
    """
    Достаёт DIRECT URL стрима из словаря info для выбранного формата.

    Берём только те форматы, которые представляют собой ОДИН готовый файл:
      - combined  — видео+аудио уже объединены (прогрессивный mp4);
      - video_only — DASH-поток с видео без звука (тоже один mp4-файл,
        аудио в него не вшито). Оба варианта НЕ требуют ffmpeg и диска.

    Форматы, где есть только аудио (vcodec=='none'), либо нет прямого url,
    отбрасываем — их нельзя «бесшовно» залить одним файлом.

    Возвращает (direct_url, filesize_or_None, video_ext) либо (None, None, None),
    если формат не подходит для загрузки одним файлом.
    """
    if not format_id:
        return None, None, None

    # Ищем выбранный формат в списке форматов.
    fmt = None
    if isinstance(info.get("formats"), list):
        for f in info["formats"]:
            if isinstance(f, dict) and str(f.get("format_id")) == str(format_id):
                fmt = f
                break

    # Если список форматов не отдали по выбранному id, пробуем сам info
    # (это случай, когда yt-dlp с опцией format=... вернул один формат).
    if fmt is None:
        fmt = info

    if not isinstance(fmt, dict):
        return None, None, None

    url = fmt.get("url")
    if not url:
        return None, None, None

    # Должна быть видео-дорожка (без видео файл заливать бессмысленно).
    vcodec = str(fmt.get("vcodec") or "")
    if "none" in vcodec or not vcodec:
        return None, None, None

    filesize = fmt.get("filesize") or fmt.get("filesize_approx")
    ext = fmt.get("ext") or "mp4"
    return url, filesize, ext


def _get_available_formats(info):
    """
    Возвращает форматы, которые можно скачать ОДНИМ готовым файлом без ffmpeg:

      1. combined   — прогрессивные mp4, видео+аудио в одном файле (приоритет);
      2. video_only — DASH mp4-поток только с видео (без звука), тоже один файл.

    Современный YouTube массово отдаёт только DASH-потоки, поэтому без второго
    пункта список почти всегда пуст. Оба варианта грузятся стримом без диска.
    """
    result = []
    formats = info.get("formats") or []
    for f in formats:
        if not isinstance(f, dict):
            continue
        # Только mp4 и только там, где есть готовый url.
        if str(f.get("ext", "")).lower() != "mp4":
            continue
        vcodec = str(f.get("vcodec") or "")
        acodec = str(f.get("acodec") or "")
        if "none" in vcodec or not vcodec:
            continue  # чисто аудио-поток
        if not f.get("url"):
            continue
        height = f.get("height") or 0
        filesize = f.get("filesize") or f.get("filesize_approx")
        # kind = "combined" если видео+аудио уже вместе, иначе "video_only".
        kind = "combined" if "none" not in acodec and acodec else "video_only"
        label = f"{height}p (mp4)" if height else "mp4"
        if kind == "video_only":
            label += " · только видео"
        result.append({
            "format_id": str(f.get("format_id")),
            "height": height,
            "ext": "mp4",
            "filesize": filesize,
            "kind": kind,  # 'combined' | 'video_only'
            "label": label,
        })
    # Сначала combined, потом video_only; внутри — по убыванию разрешения.
    result.sort(key=lambda x: (x["kind"] == "video_only", -(x["height"] or 0)))
    return result

# ---------------------------------------------------------------------------
# ЗАГРУЗКА В HUGGING FACE (прямой стриминг чанками, без диска)
# ---------------------------------------------------------------------------

def hf_upload_stream(direct_url: str, filename: str, filesize, progress_cb=None):
    """
    Скачиваем видео по direct_url и грузим его в HF Datasets прямо из памяти,
    чанками по CHUNK_SIZE. Никаких файлов на диске.

    Используется «решумаблий» (resumable) протокол загрузки HF Hub:
      1) POST  {endpoint}/api/datasets/{repo_id}/upload/{filename}  -> upload_url
      2) PUT   {upload_url}  с заголовком X-Upload-Part: {n}, тело = чанк (10 МБ)
      3) PUT   {upload_url}  с заголовком X-Upload-Parts: {total}  -> commit_url

    Память: в любой момент времени держим максимум один чанк (10 МБ) + небольшой
    служебный буфер requests. Это безопасно для лимита в 512 МБ RAM даже при
    файлах по 1 ГБ+.

    Возвращает commit_url (ссылку на датасет).
    progress_cb(bytes_so_far) вызывается после каждого чанка.
    """
    auth_headers = {"Authorization": f"Bearer {HF_TOKEN}"}

    # 1) Инициализация загрузки: получаем upload_url (внутри S3/CDN).
    init_url = f"{HF_ENDPOINT}/api/datasets/{REPO_ID}/upload/{filename}"
    resp = requests.post(init_url, headers=auth_headers, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(
            f"HF init upload failed: HTTP {resp.status_code}: {resp.text[:300]}"
        )
    payload = resp.json()
    upload_url = payload.get("upload_url") or payload.get("url")
    if not upload_url:
        raise RuntimeError(f"HF init upload: нет upload_url в ответе: {resp.text[:300]}")
    log("info", f"HF: инициализирована загрузка файла {filename}")

    # 2) Скачиваем стрим чанками и отправляем каждый чанк как отдельную часть.
    part_number = 0
    total_uploaded = 0

    r = requests.get(direct_url, stream=True, timeout=300)
    try:
        if r.status_code != 200:
            raise RuntimeError(
                f"Не удалось скачать стрим: HTTP {r.status_code} для {direct_url[:120]}"
            )
        for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            # Заголовок X-Upload-Part указывает номер части (0-based).
            part_headers = {
                **auth_headers,
                "X-Upload-Part": str(part_number),
                "Content-Type": "application/octet-stream",
            }
            up = requests.put(upload_url, headers=part_headers, data=chunk, timeout=300)
            if up.status_code != 200:
                raise RuntimeError(
                    f"Upload part {part_number} failed: HTTP {up.status_code}: {up.text[:300]}"
                )
            part_number += 1
            total_uploaded += len(chunk)
            if progress_cb:
                progress_cb(total_uploaded)

            # Локально освобождаем чанк — важный момент для контроля памяти.
            del chunk
    finally:
        r.close()

    if part_number == 0:
        raise RuntimeError("Видео пустое: не получено ни одного байта стрима.")

    # 3) Финализация: сообщаем HF количество загруженных частей.
    final_headers = {
        **auth_headers,
        "X-Upload-Parts": str(part_number),
        "Content-Type": "application/json",
    }
    fin = requests.put(upload_url, headers=final_headers, data=b"{}", timeout=120)
    if fin.status_code != 200:
        raise RuntimeError(
            f"Finalize upload failed: HTTP {fin.status_code}: {fin.text[:300]}"
        )
    commit_data = fin.json()
    commit_url = (
        commit_data.get("commitUrl")
        or commit_data.get("commit_url")
        or f"{HF_ENDPOINT}/datasets/{REPO_ID}"
    )

    log("info", f"HF: загрузка завершена, {part_number} частей, commit_url укорочен {filename[:40]}...")
    return commit_url

# ---------------------------------------------------------------------------
# ЯДРО ОБРАБОТКИ ОДНОГО ВИДЕО
# ---------------------------------------------------------------------------

async def process_video(task: dict):
    """
    Полностью обрабатывает одно видео: получает метаданные, достаёт direct URL,
    стримит в HF чанками, обновляет прогресс, пишет в «базу данных».

    Вызывается строго последовательно из worker (по одному за раз).
    """
    from yt_dlp import YoutubeDL

    task_id = task["task_id"]
    url = task["url"]
    format_id = task["format_id"]
    start_time = time.time()

    # Прогресс — объект, который читает фронтенд через /api/status.
    task["status"] = "downloading"
    task["bytes_downloaded"] = 0
    task["total_bytes"] = None
    task["speed_bps"] = 0.0
    task["speed_mbps"] = 0.0
    task["percent"] = 0.0
    task["message"] = "Получение информации о видео..."
    task["last_update"] = time.time()
    task["_speed_start"] = time.time()
    task["_speed_prev"] = 0

    def progress_cb(uploaded):
        """Обновляем прогресс после каждого скачанного чанка."""
        task["bytes_downloaded"] = uploaded
        total = task["total_bytes"]
        if total and total > 0:
            task["percent"] = round(uploaded / total * 100, 1)
        # Скорость считаем по скользящему окну (в среднем за 5 сек).
        now = time.time()
        elapsed = now - task["_speed_start"]
        if elapsed >= 5.0:
            task["speed_bps"] = (uploaded - task["_speed_prev"]) / elapsed
            task["speed_mbps"] = round(task["speed_bps"] / (1024 * 1024), 2)
            task["_speed_prev"] = uploaded
            task["_speed_start"] = now
        task["last_update"] = now

    try:
        # --- Шаг 1: метаданные + выбор single-file mp4 формата ---
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,  # ничего не скачиваем, нужен только info
            "format": f"{format_id}",
            "noplaylist": True,
            "cachedir": False,      # НЕ пишем временные файлы (диска нет)
            "socket_timeout": 30,
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get("title") or "video"
        author = info.get("uploader") or info.get("channel") or "unknown"
        thumbnail = info.get("thumbnail") or ""
        duration = info.get("duration")
        webpage = info.get("webpage_url") or url

        # Достаём прямой URL выбранного формата + размер.
        direct_url, filesize, _ext = _extract_direct_url(info, format_id)
        if not direct_url:
            raise RuntimeError(
                f"Формат {format_id} не является готовым single-file (mp4) "
                "или не найден. Выберите другой формат."
            )
        task["total_bytes"] = filesize
        log("info", f"Старт: «{title}» -> {REPO_ID} (формат {format_id})")

        # ---------------------------------------------------------------
        # Освобождаем большой словарь yt-dlp (в нём ~все форматы видео)
        # ДО начала потоковой загрузки. Это важно для контроля памяти:
        # в момент передачи 1 ГБ файла лишние метаданные в RAM не нужны.
        # ---------------------------------------------------------------
        try:
            del info
        except NameError:
            pass
        gc.collect()

        # --- Шаг 2: стриминг direct_url в HF ---
        filename = f"{VIDEO_FOLDER}/{sanitize_filename(title)}_{task_id[:8]}.mp4"
        task["message"] = "Скачивание и загрузка в Hugging Face..."
        commit_url = hf_upload_stream(direct_url, filename, filesize, progress_cb)

        # --- Шаг 3: финал, статистика, «база данных» ---
        downloaded = task["bytes_downloaded"]
        end_time = time.time()
        elapsed = end_time - start_time

        store.record_success({
            "id": task_id,
            "title": title,
            "author": author,
            "duration": duration,
            "thumbnail": thumbnail,
            "webpage_url": webpage,
            "filename": filename,
            "size_bytes": downloaded,
            "size_mb": round(downloaded / (1024 * 1024), 2),
            "hf_url": commit_url,
            "format_id": format_id,
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        store.total_bytes_uploaded += downloaded
        store.total_bytes_success += downloaded
        store.elapsed_seconds += elapsed
        store.queue_history.append({
            "task_id": task_id,
            "title": title,
            "status": "done",
            "at": datetime.now().strftime("%H:%M:%S"),
        })

        task["status"] = "done"
        task["percent"] = 100.0
        task["message"] = "Готово!"
        log("info",
            f"Готово: «{title}» {round(downloaded / 1024 / 1024, 2)} МБ за {elapsed:.1f} с")
        return {"ok": True, "task_id": task_id, "title": title}

    except Exception as exc:  # noqa: BLE001 — ловим всё, чтобы worker не упал
        store.record_failure()
        task["status"] = "error"
        task["message"] = f"Ошибка: {exc}"
        store.queue_history.append({
            "task_id": task_id,
            "title": url,
            "status": "error",
            "at": datetime.now().strftime("%H:%M:%S"),
        })
        log("error", f"Ошибка обработки: {exc}")
        return {"ok": False, "task_id": task_id, "error": str(exc)}

    finally:
        # Защита от утечек памяти: очищаем тяжёлые ссылки и вызываем GC.
        # Это требование пункта 5 ТЗ — после каждого видео.
        try:
            info = None  # noqa: F841 — освобождаем большой словарь с форматами
        except Exception:
            pass
        task.pop("_speed_start", None)
        task.pop("_speed_prev", None)
        collected = gc.collect()
        log("debug", f"gc.collect(): собрано {collected} объектов")
# ---------------------------------------------------------------------------
# WORKER: строго последовательная обработка очереди
# ---------------------------------------------------------------------------

async def worker_loop():
    """
    Фоновая задача: бесконечно берёт по одной задаче из asyncio.Queue и
    обрабатывает её. Это гарантирует, что качается строго одно видео за раз —
    иначе память (512 МБ) и пропускная способность не выдержат.
    """
    global current_task
    while True:
        task = await task_queue.get()
        # Убираем задачу из списка ожидающих (она стала активной).
        if task in pending_tasks:
            pending_tasks.remove(task)
        current_task = task
        try:
            await process_video(task)
        finally:
            current_task = None
            task_queue.task_done()
            # Тяжёлые структуры могли остаться — сразу собираем мусор.
            gc.collect()

# ---------------------------------------------------------------------------
# FASTAPI ПРИЛОЖЕНИЕ И ЭНДПОИНТЫ
# ---------------------------------------------------------------------------

app = FastAPI(title="YouTube → Hugging Face Datasets uploader")

# Разрешаем CORS (нужно, если фронтенд открыт с другого origin).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    """Запускаем фоновый worker-цикл при старте сервера."""
    asyncio.create_task(worker_loop())
    log("info", "Сервер запущен. Ожидание задач...")


@app.get("/")
async def root():
    """Отдаём фронтенд-панель (index.html)."""
    return FileResponse("index.html")


# --- POST /api/fetch-info -------------------------------------------------
@app.post("/api/fetch-info")
async def fetch_info(body: RequestBody):
    """
    Принимает URL YouTube, использует yt-dlp с download=False, достаёт метаданные
    (название, превью, автор, длительность) и список доступных комбинированных
    mp4-форматов (видео+аудио в одном файле). Возвращает это на фронтенд.
    """
    from yt_dlp import YoutubeDL

    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL пустой")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 30,
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        log("error", f"fetch-info ошибка: {exc}")
        raise HTTPException(status_code=400, detail=f"Не удалось получить информацию: {exc}")

    formats = _get_available_formats(info)
    result = {
        "title": info.get("title") or "Без названия",
        "thumbnail": info.get("thumbnail") or "",
        "author": info.get("uploader") or info.get("channel") or "unknown",
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url") or url,
        "formats": formats,  # список {format_id,height,ext,filesize,label}
    }
    # Освобождаем крупный словарь с форматами после извлечения нужного.
    del info
    gc.collect()

    log("info", f"fetch-info: «{result['title']}» — {len(formats)} готовых форматов")
    return result

# --- POST /api/download ---------------------------------------------------
@app.post("/api/download")
async def download(body: RequestBody):
    """
    Принимает URL и выбранный format_id, ставит задачу в очередь (asyncio.Queue).
    Возвращает task_id, чтобы фронтенд мог следить за прогрессом.
    """
    url = (body.url or "").strip()
    format_id = (body.format_id or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL пустой")
    if not format_id:
        raise HTTPException(status_code=400, detail="format_id пустой")

    task_id = uuid.uuid4().hex
    task = {
        "task_id": task_id,
        "url": url,
        "format_id": format_id,
        "status": "queued",     # queued -> downloading -> done/error
        "message": "В очереди",
        "bytes_downloaded": 0,
        "total_bytes": None,
        "percent": 0.0,
        "speed_mbps": 0.0,
        "created_at": time.time(),
    }
    # Запоминаем в списке ожидающих (для /api/status) и в очереди
    # (для строго последовательной обработки).
    pending_tasks.append(task)
    await task_queue.put(task)
    store.record_attempt()

    store.queue_history.append({
        "task_id": task_id,
        "title": url,
        "status": "queued",
        "at": datetime.now().strftime("%H:%M:%S"),
    })

    log("info", f"Задача {task_id} добавлена в очередь: {url} [{format_id}]")
    return {"ok": True, "task_id": task_id}


# --- GET /api/status ------------------------------------------------------
@app.get("/api/status")
async def status():
    """
    Возвращает текущий статус: активная задача + прогресс (МБ/проценты),
    а также список задач, ожидающих в очереди.
    """
    active = None
    if current_task and current_task.get("status") in ("downloading", "queued"):
        # Не отдаём служебные ключи '_' наружу.
        active = {k: v for k, v in current_task.items() if not k.startswith("_")}
    return {
        "active": active,
        "queue_size": len(pending_tasks),
        "pending": [
            {k: v for k, v in t.items() if not k.startswith("_")}
            for t in pending_tasks
        ],
    }


# --- GET /api/stats -------------------------------------------------------
@app.get("/api/stats")
async def stats():
    """
    Реальная аналитика из памяти: всего скачано видео, суммарный объём в ГБ,
    аптайм сервера, история загрузок по дням для графика.
    """
    uptime_seconds = int(time.time() - SERVER_START)
    uptime = str(timedelta(seconds=uptime_seconds))

    # История за последние 7 дней (для графика на фронтенде).
    history = []
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        history.append({
            "date": day,
            "count": store.daily_counts.get(day, 0),
        })

    total_gb = round(store.total_bytes_uploaded / (1024 ** 3), 3)
    success_gb = round(store.total_bytes_success / (1024 ** 3), 3)
    # Средняя скорость по всем успешным загрузкам (МБ/с).
    avg_mbps = (
        round((store.total_bytes_success / store.elapsed_seconds) / (1024 * 1024), 2)
        if store.elapsed_seconds > 0 else 0.0
    )

    return {
        "uptime_seconds": uptime_seconds,
        "uptime": uptime,
        "total_videos": store.successful_videos,
        "failed_videos": store.failed_videos,
        "total_attempts": store.total_attempts,
        "total_gb": total_gb,
        "success_gb": success_gb,
        "avg_speed_mbps": avg_mbps,
        "history": history,  # [{date, count}] за 7 дней
        "queue_history": list(store.queue_history)[-50:],
        "server_start": datetime.fromtimestamp(SERVER_START).strftime("%Y-%m-%d %H:%M:%S"),
    }


# --- GET /api/videos ------------------------------------------------------
@app.get("/api/videos")
async def videos():
    """
    Список успешно загруженных видео за сессию (наша «база данных» в RAM).
    """
    return {"videos": list(store.videos)}


# --- GET /api/logs --------------------------------------------------------
@app.get("/api/logs")
async def logs():
    """
    Последние 50 строк логов из оперативной памяти.
    """
    return {"logs": list(mem_logs)[-50:]}


# ---------------------------------------------------------------------------
# ТОЧКА ВХОДА (позволяет запуск через `python app.py`)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
