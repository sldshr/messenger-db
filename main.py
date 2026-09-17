import asyncio
import json
import secrets
import string
from datetime import datetime
from typing import Dict, Set, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


app = FastAPI(title="SldMeet")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# DATA
# ============================================================

rooms: Dict[str, dict] = {}
connections: Dict[str, Set[WebSocket]] = {}


def make_room_id(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    while True:
        room_id = "".join(secrets.choice(chars) for _ in range(length))
        if room_id not in rooms:
            return room_id


def clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        return "Гость"

    return name[:32]


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ============================================================
# HTML
# ============================================================

HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SldMeet</title>

<style>
* {
    box-sizing: border-box;
}

html, body {
    margin: 0;
    padding: 0;
    width: 100%;
    height: 100%;
    font-family: Arial, Helvetica, sans-serif;
    background: #d6d6d6;
    color: #111;
}

button,
input,
select,
textarea {
    font-family: Arial, Helvetica, sans-serif;
}

button {
    cursor: pointer;
}

.topbar {
    height: 54px;
    background: linear-gradient(#f7f7f7, #cfcfcf);
    border-bottom: 1px solid #888;
    box-shadow: 0 1px 2px #fff inset;
    display: flex;
    align-items: center;
    padding: 0 14px;
}

.logo {
    font-size: 23px;
    font-weight: bold;
    color: #174c86;
    text-shadow: 1px 1px white;
}

.logo span {
    color: #333;
}

.top-right {
    margin-left: auto;
    font-size: 12px;
    color: #555;
}

.page {
    min-height: calc(100% - 54px);
    padding: 25px;
}

.window {
    max-width: 900px;
    margin: auto;
    background: #eee;
    border: 1px solid #777;
    box-shadow: 2px 2px 8px #777;
}

.titlebar {
    background: linear-gradient(#447bb0, #1f4e7d);
    color: white;
    padding: 7px 10px;
    font-weight: bold;
    text-shadow: 1px 1px #234;
}

.content {
    padding: 18px;
}

h2 {
    margin-top: 0;
    font-size: 20px;
}

h3 {
    font-size: 15px;
    margin-bottom: 8px;
}

.field {
    margin-bottom: 13px;
}

label {
    display: block;
    font-size: 12px;
    font-weight: bold;
    margin-bottom: 4px;
}

input[type=text],
input[type=number],
select,
textarea {
    width: 100%;
    border: 1px solid #777;
    background: white;
    padding: 7px;
    box-shadow: inset 1px 1px 2px #ccc;
}

textarea {
    resize: vertical;
}

button {
    border: 1px solid #666;
    background: linear-gradient(#fff, #c9c9c9);
    padding: 7px 13px;
    color: #111;
}

button:hover {
    background: linear-gradient(#fff, #ddd);
}

button:active {
    background: #bbb;
}

.primary {
    background: linear-gradient(#6199d0, #275f94);
    color: white;
    border-color: #234b70;
    font-weight: bold;
}

.primary:hover {
    background: linear-gradient(#76a9d9, #316da3);
}

.row {
    display: flex;
    gap: 15px;
}

.col {
    flex: 1;
}

.box {
    border: 1px solid #999;
    background: #ddd;
    padding: 12px;
    margin-bottom: 15px;
}

.checkbox {
    margin: 8px 0;
    font-size: 13px;
}

.checkbox input {
    vertical-align: middle;
}

.hidden {
    display: none !important;
}

.info {
    background: #ffffd5;
    border: 1px solid #aaa;
    padding: 9px;
    font-size: 12px;
    margin-bottom: 12px;
}

.error {
    color: #a00000;
    font-size: 12px;
    margin-top: 8px;
}

.success {
    color: #075f14;
    font-size: 12px;
    margin-top: 8px;
}

/* ROOM */

.room {
    width: 100%;
    height: calc(100vh - 54px);
    display: flex;
    flex-direction: column;
}

.roombar {
    height: 43px;
    background: linear-gradient(#f5f5f5, #c7c7c7);
    border-bottom: 1px solid #888;
    display: flex;
    align-items: center;
    padding: 5px 8px;
}

.room-title {
    font-weight: bold;
    color: #174c86;
}

.room-link {
    margin-left: 15px;
    font-size: 11px;
    color: #555;
}

.room-body {
    flex: 1;
    display: flex;
    min-height: 0;
}

.video-area {
    flex: 1;
    background: #242424;
    padding: 10px;
    overflow: auto;
}

.videos {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 9px;
}

.video {
    position: relative;
    background: #111;
    border: 1px solid #555;
    min-height: 190px;
    aspect-ratio: 16 / 10;
}

.video video {
    width: 100%;
    height: 100%;
    object-fit: cover;
}

.video-name {
    position: absolute;
    bottom: 5px;
    left: 5px;
    background: rgba(0,0,0,.75);
    color: white;
    font-size: 12px;
    padding: 3px 6px;
}

.no-video {
    color: #aaa;
    display: flex;
    height: 100%;
    align-items: center;
    justify-content: center;
    font-size: 13px;
}

.sidebar {
    width: 280px;
    background: #eee;
    border-left: 1px solid #777;
    display: flex;
    flex-direction: column;
}

.tabs {
    display: flex;
    border-bottom: 1px solid #888;
}

.tab {
    flex: 1;
    padding: 7px;
    border: 0;
    border-right: 1px solid #aaa;
    background: #ddd;
    font-size: 12px;
}

.tab.active {
    background: #f5f5f5;
    font-weight: bold;
}

.tab-content {
    flex: 1;
    min-height: 0;
    overflow: auto;
}

.chat {
    padding: 8px;
    height: 100%;
    display: flex;
    flex-direction: column;
}

.messages {
    flex: 1;
    overflow-y: auto;
    background: white;
    border: 1px solid #999;
    padding: 7px;
}

.msg {
    margin-bottom: 7px;
    font-size: 12px;
}

.msg .meta {
    color: #777;
    font-size: 10px;
}

.chat-input {
    display: flex;
    margin-top: 7px;
}

.chat-input input {
    flex: 1;
}

.chat-input button {
    margin-left: 4px;
}

.people {
    padding: 10px;
}

.person {
    padding: 7px;
    background: white;
    border: 1px solid #aaa;
    margin-bottom: 5px;
    font-size: 12px;
}

.controls {
    height: 54px;
    background: linear-gradient(#e9e9e9, #bdbdbd);
    border-top: 1px solid #777;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
}

.control {
    min-width: 70px;
}

.danger {
    background: linear-gradient(#e99a9a, #bb5050);
    color: white;
    border-color: #873434;
}

@media(max-width: 750px) {
    .room-body {
        flex-direction: column;
    }

    .sidebar {
        width: 100%;
        height: 230px;
    }

    .video-area {
        min-height: 0;
    }

    .row {
        flex-direction: column;
        gap: 0;
    }

    .page {
        padding: 8px;
    }
}
</style>
</head>

<body>

<div class="topbar">
    <div class="logo">Sld<span>Meet</span></div>
    <div class="top-right">Video conference system</div>
</div>

<div id="home" class="page">
    <div class="window">

        <div class="titlebar">
            Новая конференция
        </div>

        <div class="content">

            <h2>Создать комнату</h2>

            <div class="info">
                Создайте комнату и отправьте полученную ссылку участникам.
            </div>

            <div class="box">

                <div class="field">
                    <label>Название конференции</label>
                    <input id="roomName" type="text"
                           value="Новая конференция"
                           maxlength="80">
                </div>

                <div class="row">

                    <div class="col">
                        <div class="field">
                            <label>Максимум участников</label>
                            <select id="maxUsers">
                                <option value="2">2</option>
                                <option value="5">5</option>
                                <option value="10" selected>10</option>
                                <option value="20">20</option>
                                <option value="50">50</option>
                            </select>
                        </div>
                    </div>

                    <div class="col">
                        <div class="field">
                            <label>Режим комнаты</label>
                            <select id="roomMode">
                                <option value="video" selected>Видео + звук</option>
                                <option value="audio">Только звук</option>
                                <option value="presentation">Презентация</option>
                            </select>
                        </div>
                    </div>

                </div>

                <div class="field">
                    <label>Пароль комнаты</label>
                    <input id="password" type="text"
                           placeholder="Оставьте пустым, если пароль не нужен"
                           maxlength="50">
                </div>

                <div class="checkbox">
                    <input id="allowChat" type="checkbox" checked>
                    Разрешить общий чат
                </div>

                <div class="checkbox">
                    <input id="allowGuests" type="checkbox" checked>
                    Разрешить подключение по ссылке
                </div>

                <div class="checkbox">
                    <input id="hostMute" type="checkbox">
                    Участники входят с выключенным микрофоном
                </div>

                <div class="checkbox">
                    <input id="hostVideo" type="checkbox">
                    Участники входят с выключенной камерой
                </div>

                <div class="checkbox">
                    <input id="waitingRoom" type="checkbox">
                    Использовать комнату ожидания
                </div>

            </div>

            <button class="primary" onclick="createRoom()">
                Создать конференцию
            </button>

            <div id="createError" class="error"></div>

            <hr>

            <h2>Подключиться</h2>

            <div class="field">
                <label>Ссылка или ID комнаты</label>
                <input id="joinRoom" type="text"
                       placeholder="Например: Ab12Cd34">
            </div>

            <button onclick="showJoin()">Подключиться</button>

        </div>
    </div>
</div>


<div id="joinPage" class="page hidden">
    <div class="window">

        <div class="titlebar">
            Подключение к конференции
        </div>

        <div class="content">

            <h2>Введите имя</h2>

            <div class="field">
                <label>Ваш никнейм</label>
                <input id="nickname" type="text"
                       maxlength="32"
                       placeholder="Например: Alex">
            </div>

            <div id="passwordBox" class="field hidden">
                <label>Пароль комнаты</label>
                <input id="joinPassword" type="password">
            </div>

            <button class="primary" onclick="joinRoom()">
                Войти в конференцию
            </button>

            <button onclick="goHome()">
                Назад
            </button>

            <div id="joinError" class="error"></div>

        </div>
    </div>
</div>


<div id="room" class="room hidden">

    <div class="roombar">
        <div id="roomTitle" class="room-title">
            Конференция
        </div>

        <div id="roomLink" class="room-link"></div>
    </div>

    <div class="room-body">

        <div class="video-area">
            <div id="videos" class="videos"></div>
        </div>

        <div class="sidebar">

            <div class="tabs">
                <button id="chatTab"
                        class="tab active"
                        onclick="showTab('chat')">
                    Чат
                </button>

                <button id="peopleTab"
                        class="tab"
                        onclick="showTab('people')">
                    Участники
                </button>
            </div>

            <div id="chatPanel" class="tab-content">
                <div class="chat">

                    <div id="messages" class="messages"></div>

                    <div class="chat-input">
                        <input id="chatInput"
                               type="text"
                               maxlength="500"
                               placeholder="Сообщение...">

                        <button onclick="sendChat()">
                            Отправить
                        </button>
                    </div>

                </div>
            </div>

            <div id="peoplePanel" class="tab-content hidden">
                <div id="people" class="people"></div>
            </div>

        </div>

    </div>

    <div class="controls">

        <button id="micBtn"
                class="control"
                onclick="toggleMic()">
            🎤 Микрофон
        </button>

        <button id="camBtn"
                class="control"
                onclick="toggleCamera()">
            Камера
        </button>

        <button class="control"
                onclick="copyRoomLink()">
            Ссылка
        </button>

        <button class="control danger"
                onclick="leaveRoom()">
            Покинуть
        </button>

    </div>

</div>


<script>
let roomId = null;
let nickname = null;
let password = null;
let ws = null;

let localStream = null;
let peers = {};

let micEnabled = true;
let cameraEnabled = true;

let roomSettings = {};
let participants = {};


function $(id) {
    return document.getElementById(id);
}


function show(id) {
    $(id).classList.remove("hidden");
}


function hide(id) {
    $(id).classList.add("hidden");
}


function goHome() {
    hide("joinPage");
    hide("room");
    show("home");
}


async function createRoom() {

    const data = {
        name: $("roomName").value,
        max_users: Number($("maxUsers").value),
        mode: $("roomMode").value,
        password: $("password").value,
        allow_chat: $("allowChat").checked,
        allow_guests: $("allowGuests").checked,
        host_mute: $("hostMute").checked,
        host_video: $("hostVideo").checked,
        waiting_room: $("waitingRoom").checked
    };

    const response = await fetch("/api/create", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify(data)
    });

    const result = await response.json();

    if (!response.ok) {
        $("createError").textContent =
            result.detail || "Ошибка создания комнаты";
        return;
    }

    roomId = result.room_id;

    $("joinRoom").value =
        location.origin + "/?room=" + roomId;

    showJoin();
}


function showJoin() {

    const value = $("joinRoom").value.trim();

    if (!value) {
        $("joinError").textContent = "Введите ID или ссылку комнаты";
        return;
    }

    let id = value;

    try {
        const url = new URL(value);
        const fromUrl = url.searchParams.get("room");

        if (fromUrl) {
            id = fromUrl;
        }
    } catch (_) {}

    roomId = id;

    hide("home");
    show("joinPage");

    $("nickname").focus();

    checkRoom();
}


async function checkRoom() {

    const response = await fetch(
        "/api/room/" + encodeURIComponent(roomId)
    );

    const result = await response.json();

    if (!response.ok) {
        $("joinError").textContent =
            result.detail || "Комната не найдена";
        return;
    }

    roomSettings = result;

    if (result.password_required) {
        show("passwordBox");
    } else {
        hide("passwordBox");
    }
}


async function joinRoom() {

    nickname = $("nickname").value.trim();

    if (!nickname) {
        $("joinError").textContent =
            "Введите никнейм";
        return;
    }

    password = $("joinPassword").value;

    const response = await fetch("/api/join", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            room_id: roomId,
            nickname: nickname,
            password: password
        })
    });

    const result = await response.json();

    if (!response.ok) {
        $("joinError").textContent =
            result.detail || "Не удалось войти";
        return;
    }

    roomSettings = result.room;

    startRoom();
}


async function startRoom() {

    hide("home");
    hide("joinPage");
    show("room");

    $("roomTitle").textContent =
        roomSettings.name || "Конференция";

    $("roomLink").textContent =
        location.origin + "/?room=" + roomId;

    if (!roomSettings.allow_chat) {
        hide("chatTab");
        hide("chatPanel");
        showTab("people");
    }

    micEnabled = !roomSettings.host_mute;
    cameraEnabled = !roomSettings.host_video;

    try {
        localStream = await navigator.mediaDevices.getUserMedia({
            audio: true,
            video: roomSettings.mode !== "audio"
        });

        localStream.getAudioTracks().forEach(
            track => track.enabled = micEnabled
        );

        localStream.getVideoTracks().forEach(
            track => track.enabled = cameraEnabled
        );

    } catch (e) {
        console.log("Media error:", e);
        localStream = new MediaStream();
    }

    addLocalVideo();

    connectWebSocket();
}


function addLocalVideo() {

    const div = document.createElement("div");
    div.className = "video";
    div.id = "localVideo";

    const video = document.createElement("video");
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;

    if (localStream) {
        video.srcObject = localStream;
    }

    const name = document.createElement("div");
    name.className = "video-name";
    name.textContent = nickname + " (Вы)";

    div.appendChild(video);
    div.appendChild(name);

    $("videos").appendChild(div);
}


function connectWebSocket() {

    const protocol =
        location.protocol === "https:" ? "wss:" : "ws:";

    ws = new WebSocket(
        protocol + "//" +
        location.host +
        "/ws/" +
        encodeURIComponent(roomId)
    );

    ws.onopen = () => {

        ws.send(JSON.stringify({
            type: "join",
            nickname: nickname,
            password: password
        }));

    };

    ws.onmessage = async event => {

        const data = JSON.parse(event.data);

        if (data.type === "room_state") {

            participants = data.participants || {};
            updatePeople();

            for (const id of Object.keys(participants)) {

                if (id !== data.client_id) {
                    await createOffer(id);
                }
            }

        } else if (data.type === "user_joined") {

            participants[data.client_id] =
                data.nickname;

            updatePeople();

        } else if (data.type === "user_left") {

            delete participants[data.client_id];

            removePeer(data.client_id);
            updatePeople();

        } else if (data.type === "chat") {

            addMessage(
                data.nickname,
                data.message,
                data.time
            );

        } else if (data.type === "offer") {

            await receiveOffer(
                data.client_id,
                data.offer
            );

        } else if (data.type === "answer") {

            const peer = peers[data.client_id];

            if (peer) {
                await peer.setRemoteDescription(
                    new RTCSessionDescription(data.answer)
                );
            }

        } else if (data.type === "candidate") {

            const peer = peers[data.client_id];

            if (peer && data.candidate) {

                try {
                    await peer.addIceCandidate(
                        new RTCIceCandidate(data.candidate)
                    );
                } catch (e) {
                    console.log(e);
                }
            }
        }
    };

    ws.onclose = () => {
        console.log("WebSocket closed");
    };
}


function createPeer(clientId) {

    if (peers[clientId]) {
        return peers[clientId];
    }

    const peer = new RTCPeerConnection({
        iceServers: []
    });

    peers[clientId] = peer;

    if (localStream) {
        localStream.getTracks().forEach(track => {
            peer.addTrack(track, localStream);
        });
    }

    peer.onicecandidate = event => {

        if (event.candidate && ws) {

            ws.send(JSON.stringify({
                type: "candidate",
                target: clientId,
                candidate: event.candidate
            }));
        }
    };

    peer.ontrack = event => {

        addRemoteVideo(
            clientId,
            event.streams[0]
        );
    };

    return peer;
}


async function createOffer(clientId) {

    if (clientId === window.clientId) {
        return;
    }

    const peer = createPeer(clientId);

    try {

        const offer = await peer.createOffer();

        await peer.setLocalDescription(offer);

        ws.send(JSON.stringify({
            type: "offer",
            target: clientId,
            offer: offer
        }));

    } catch (e) {
        console.log("Offer error:", e);
    }
}


async function receiveOffer(clientId, offer) {

    const peer = createPeer(clientId);

    try {

        await peer.setRemoteDescription(
            new RTCSessionDescription(offer)
        );

        const answer = await peer.createAnswer();

        await peer.setLocalDescription(answer);

        ws.send(JSON.stringify({
            type: "answer",
            target: clientId,
            answer: answer
        }));

    } catch (e) {
        console.log("Answer error:", e);
    }
}


function addRemoteVideo(clientId, stream) {

    let div = $("video-" + clientId);

    if (!div) {

        div = document.createElement("div");
        div.className = "video";
        div.id = "video-" + clientId;

        const video = document.createElement("video");
        video.autoplay = true;
        video.playsInline = true;
        video.srcObject = stream;

        const name = document.createElement("div");
        name.className = "video-name";
        name.textContent =
            participants[clientId] || "Участник";

        div.appendChild(video);
        div.appendChild(name);

        $("videos").appendChild(div);

    } else {

        const video = div.querySelector("video");
        video.srcObject = stream;
    }
}


function removePeer(clientId) {

    if (peers[clientId]) {

        peers[clientId].close();

        delete peers[clientId];
    }

    const video = $("video-" + clientId);

    if (video) {
        video.remove();
    }
}


function updatePeople() {

    $("people").innerHTML = "";

    const local = document.createElement("div");
    local.className = "person";
    local.textContent = nickname + " (Вы)";

    $("people").appendChild(local);

    for (const id in participants) {

        const div = document.createElement("div");
        div.className = "person";
        div.textContent = participants[id];

        $("people").appendChild(div);
    }
}


function addMessage(name, message, time) {

    const div = document.createElement("div");
    div.className = "msg";

    div.innerHTML =
        "<b>" +
        escapeHtml(name) +
        "</b> " +
        "<span class='meta'>" +
        escapeHtml(time) +
        "</span><br>" +
        escapeHtml(message);

    $("messages").appendChild(div);

    $("messages").scrollTop =
        $("messages").scrollHeight;
}


function escapeHtml(text) {

    return String(text)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


function sendChat() {

    const input = $("chatInput");
    const message = input.value.trim();

    if (!message || !ws) {
        return;
    }

    ws.send(JSON.stringify({
        type: "chat",
        message: message
    }));

    input.value = "";
}


$("chatInput").addEventListener(
    "keydown",
    event => {

        if (event.key === "Enter") {
            sendChat();
        }
    }
);


function toggleMic() {

    if (!localStream) {
        return;
    }

    micEnabled = !micEnabled;

    localStream.getAudioTracks().forEach(
        track => track.enabled = micEnabled
    );

    $("micBtn").textContent =
        micEnabled ? "🎤 Микрофон" : "🔇 Микрофон";
}


function toggleCamera() {

    if (!localStream) {
        return;
    }

    cameraEnabled = !cameraEnabled;

    localStream.getVideoTracks().forEach(
        track => track.enabled = cameraEnabled
    );

    $("camBtn").textContent =
        cameraEnabled ? "Камера" : "Камера выкл.";
}


function copyRoomLink() {

    const link =
        location.origin + "/?room=" + roomId;

    navigator.clipboard.writeText(link);

    alert("Ссылка скопирована:\n" + link);
}


function leaveRoom() {

    if (ws) {
        ws.close();
    }

    for (const id in peers) {
        peers[id].close();
    }

    peers = {};

    if (localStream) {

        localStream.getTracks().forEach(
            track => track.stop()
        );
    }

    $("videos").innerHTML = "";
    $("messages").innerHTML = "";

    goHome();
}


function showTab(tab) {

    if (tab === "chat") {

        show("chatPanel");
        hide("peoplePanel");

        $("chatTab").classList.add("active");
        $("peopleTab").classList.remove("active");

    } else {

        hide("chatPanel");
        show("peoplePanel");

        $("chatTab").classList.remove("active");
        $("peopleTab").classList.add("active");
    }
}


async function autoJoinFromUrl() {

    const params =
        new URLSearchParams(location.search);

    const id = params.get("room");

    if (id) {

        $("joinRoom").value =
            location.href;

        roomId = id;

        showJoin();
    }
}


autoJoinFromUrl();

</script>

</body>
</html>
"""


# ============================================================
# API
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.post("/api/create")
async def create_room(data: dict):

    name = str(data.get("name", "Новая конференция")).strip()

    if not name:
        name = "Новая конференция"

    max_users = int(data.get("max_users", 10))

    max_users = max(2, min(max_users, 50))

    room_id = make_room_id()

    rooms[room_id] = {
        "name": name[:80],
        "max_users": max_users,
        "mode": data.get("mode", "video"),
        "password": data.get("password", ""),
        "allow_chat": bool(data.get("allow_chat", True)),
        "allow_guests": bool(data.get("allow_guests", True)),
        "host_mute": bool(data.get("host_mute", False)),
        "host_video": bool(data.get("host_video", False)),
        "waiting_room": bool(data.get("waiting_room", False)),
        "created": datetime.now().isoformat()
    }

    connections[room_id] = set()

    return {
        "room_id": room_id,
        "url": "/?room=" + room_id
    }


@app.get("/api/room/{room_id}")
async def room_info(room_id: str):

    room = rooms.get(room_id)

    if not room:
        raise HTTPException(
            status_code=404,
            detail="Комната не найдена"
        )

    return {
        "name": room["name"],
        "max_users": room["max_users"],
        "mode": room["mode"],
        "allow_chat": room["allow_chat"],
        "allow_guests": room["allow_guests"],
        "password_required": bool(room["password"]),
        "waiting_room": room["waiting_room"]
    }


@app.post("/api/join")
async def check_join(data: dict):

    room_id = str(data.get("room_id", ""))
    password = str(data.get("password", ""))

    room = rooms.get(room_id)

    if not room:
        raise HTTPException(
            status_code=404,
            detail="Комната не найдена"
        )

    if room["password"] and password != room["password"]:
        raise HTTPException(
            status_code=403,
            detail="Неверный пароль"
        )

    if len(connections.get(room_id, set())) >= room["max_users"]:
        raise HTTPException(
            status_code=403,
            detail="Комната заполнена"
        )

    return {
        "room": {
            "name": room["name"],
            "max_users": room["max_users"],
            "mode": room["mode"],
            "allow_chat": room["allow_chat"],
            "allow_guests": room["allow_guests"],
            "host_mute": room["host_mute"],
            "host_video": room["host_video"],
            "waiting_room": room["waiting_room"]
        }
    }


# ============================================================
# WEBSOCKET
# ============================================================

async def send_to(
    room_id: str,
    target: WebSocket,
    data: dict
):

    try:
        await target.send_text(
            json.dumps(data, ensure_ascii=False)
        )
    except Exception:
        pass


async def broadcast(
    room_id: str,
    data: dict,
    exclude: Optional[WebSocket] = None
):

    dead = []

    for connection in list(connections.get(room_id, set())):

        if connection == exclude:
            continue

        try:

            await connection.send_text(
                json.dumps(data, ensure_ascii=False)
            )

        except Exception:

            dead.append(connection)

    for connection in dead:
        connections[room_id].discard(connection)


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    room_id: str
):

    await websocket.accept()

    if room_id not in rooms:
        await websocket.close(code=4004)
        return

    client_id = secrets.token_hex(8)

    nickname = "Гость"

    joined = False

    try:

        first_message = await websocket.receive_text()

        data = json.loads(first_message)

        if data.get("type") != "join":
            await websocket.close(code=4000)
            return

        nickname = clean_name(
            data.get("nickname", "Гость")
        )

        password = str(data.get("password", ""))

        room = rooms[room_id]

        if room["password"] and password != room["password"]:

            await websocket.send_text(
                json.dumps({
                    "type": "error",
                    "message": "Неверный пароль"
                }, ensure_ascii=False)
            )

            await websocket.close(code=4003)
            return

        if len(connections[room_id]) >= room["max_users"]:

            await websocket.send_text(
                json.dumps({
                    "type": "error",
                    "message": "Комната заполнена"
                }, ensure_ascii=False)
            )

            await websocket.close(code=4005)
            return

        connections[room_id].add(websocket)
        joined = True

        participants = {}

        for conn in connections[room_id]:

            if conn != websocket:

                # Участники хранятся отдельно в runtime.
                pass

        # Сохраняем имя прямо на объекте websocket.
        websocket.client_id = client_id
        websocket.nickname = nickname

        for conn in connections[room_id]:

            if conn != websocket:

                participants[
                    getattr(conn, "client_id", "")
                ] = getattr(
                    conn,
                    "nickname",
                    "Гость"
                )

        await websocket.send_text(
            json.dumps({
                "type": "room_state",
                "client_id": client_id,
                "participants": participants
            }, ensure_ascii=False)
        )

        await broadcast(
            room_id,
            {
                "type": "user_joined",
                "client_id": client_id,
                "nickname": nickname
            },
            exclude=websocket
        )

        while True:

            raw = await websocket.receive_text()

            data = json.loads(raw)

            message_type = data.get("type")

            if message_type == "chat":

                if not room["allow_chat"]:
                    continue

                message = str(
                    data.get("message", "")
                ).strip()

                if not message:
                    continue

                message = message[:500]

                await broadcast(
                    room_id,
                    {
                        "type": "chat",
                        "nickname": nickname,
                        "message": message,
                        "time": now()
                    }
                )

            elif message_type in (
                "offer",
                "answer",
                "candidate"
            ):

                target_id = data.get("target")

                if not target_id:
                    continue

                target = None

                for conn in connections[room_id]:

                    if getattr(
                        conn,
                        "client_id",
                        None
                    ) == target_id:

                        target = conn
                        break

                if target:

                    packet = dict(data)

                    packet["client_id"] = client_id

                    await send_to(
                        room_id,
                        target,
                        packet
                    )

    except WebSocketDisconnect:
        pass

    except Exception as e:
        print("WebSocket error:", e)

    finally:

        if joined:

            connections[room_id].discard(websocket)

            await broadcast(
                room_id,
                {
                    "type": "user_left",
                    "client_id": client_id,
                    "nickname": nickname
                }
            )


# ============================================================
# CLEANUP
# ============================================================

async def cleanup_rooms():

    while True:

        await asyncio.sleep(300)

        # Удаляем пустые комнаты.
        for room_id in list(rooms.keys()):

            if not connections.get(room_id):

                rooms.pop(room_id, None)
                connections.pop(room_id, None)


@app.on_event("startup")
async def startup():

    asyncio.create_task(
        cleanup_rooms()
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
