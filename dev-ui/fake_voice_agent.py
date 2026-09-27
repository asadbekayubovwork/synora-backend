#!/usr/bin/env python3
"""Soxta ovozli agent — haqiqiy agentsiz `/voice` oqimini uchidan-uchiga ko'rish uchun.

Haqiqiy agentning ommaviy yuzasini takrorlaydi — `GET /healthz`,
`POST /api/offer`, `PATCH /api/offer`, `X-API-Key` — va shu uchala qoidani ham:
video transceiver bo'lmasa ovoz yo'q, data channel'ni klient ochadi, `ping`
3 soniya kelmasa qo'ng'iroq uziladi. Maqsad nutq emas: **signalling, heartbeat
va pulning harakatini** ko'rish.

    .venv/bin/pip install aiortc              # faqat dev uchun, requirements'da yo'q
    python dev-ui/fake_voice_agent.py         # → http://127.0.0.1:8200

API'ni shunga qarating:

    VOICE_AGENT_BASE_URL=http://127.0.0.1:8200 VOICE_AGENT_API_KEY=fake-key \\
        .venv/bin/uvicorn app.main:app --port 8000

**aiortc bilan** bu haqiqiy WebRTC peer: mikrofoningizni qaytarib yuboradi
(o'z ovozingizni eshitasiz) va data channel orqali har bir necha soniyada
`user-transcription` / `bot-output` / `bot-started-speaking` kabi hodisalar
chiqaradi — xuddi haqiqiy agentdek, `bot-output` har gapga ikki marta
(`new`, keyin `completed`). **aiortc'siz** faqat soxta SDP javob qaytaradi:
backend oqimi (hold, 201, candidates) ko'rinadi, lekin brauzer ulana olmaydi.

Haqiqiy agent kabi noma'lum `pc_id` uchun `PATCH` 404 qaytaradi — backend'ning
tiriklik canary'si shunga tayanadi — va offerdan 60 s ichida ulanmagan peer'ni
yopadi (jonli agentda o'lchangan).

Xato yo'llarini `VOICE_AGENT_API_KEY` tanlaydi:

| Kalit | Upstream | Bizning javob |
| --- | --- | --- |
| boshqa har qanday | 200 | qo'ng'iroq |
| `reject-me` | 401 | `503 voice_agent_key_rejected` |
| `busy` | 429 + Retry-After | `429 voice_agent_busy` |
| `warming` | 503 + Retry-After | `503 voice_agent_unavailable` |
| `bad-offer` | 400 | `400 voice_offer_rejected` |
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.contrib.media import MediaRelay
    from aiortc.sdp import candidate_from_sdp

    HAVE_AIORTC = True
except ImportError:  # pragma: no cover - dev tool
    HAVE_AIORTC = False

logger = logging.getLogger("fake-voice-agent")

# Haqiqiy agentdek: ping 3 soniya kelmasa qo'ng'iroq tugaydi.
PING_TIMEOUT_SECONDS = 3.0
# Haqiqiy agentdek: offerdan keyin shuncha vaqtda ulanmagan peer yopiladi.
# Jonli agentda o'lchangan (~60 s). Busiz aiortc kandidat olmagan peer'ni abadiy
# "checking"da ushlab turadi va backend uni tirik deb biladi.
CONNECT_TIMEOUT_SECONDS = 60.0
# Soxta suhbat qadami: har shuncha soniyada bir "gap".
TURN_SECONDS = 4.0
PHRASES = [
    ("Salom, meni eshityapsizmi?", "Ha, sizni yaxshi eshityapman."),
    ("Bugun havo qanday?", "Bu soxta agent, lekin havo ajoyib deb o'ylayman."),
    ("Rahmat.", "Arzimaydi. Yana nima kerak?"),
]

app = FastAPI(title="fake voice agent")
peers: dict[str, "Peer"] = {}
_counter = itertools.count()


class Peer:
    """Bitta qo'ng'iroq: peer connection, data channel va ping kuzatuvchisi."""

    def __init__(self, pc_id: str) -> None:
        self.pc_id = pc_id
        self.pc = RTCPeerConnection() if HAVE_AIORTC else None
        self.relay = MediaRelay() if HAVE_AIORTC else None
        self.channel = None
        self.last_ping = time.monotonic()
        self.tasks: list[asyncio.Task] = []
        self.closed = False

    def send(self, kind: str, data: dict | None = None) -> None:
        if self.channel is not None and self.channel.readyState == "open":
            self.channel.send(json.dumps({"label": "rtvi-ai", "type": kind, "data": data or {}}))

    async def converse(self) -> None:
        """Haqiqiy agent chiqaradigan hodisalar ketma-ketligi, soxta matn bilan."""
        for user_text, bot_text in itertools.cycle(PHRASES):
            await asyncio.sleep(TURN_SECONDS)
            self.send("user-started-speaking")
            self.send("user-transcription", {"text": user_text[: len(user_text) // 2], "final": False})
            await asyncio.sleep(0.4)
            self.send("user-transcription", {"text": user_text, "final": True})
            self.send("user-stopped-speaking")
            self.send("bot-started-speaking")
            self.send("bot-output", {"text": bot_text, "spoken_status": "new"})
            await asyncio.sleep(0.8)
            self.send("bot-output", {"text": bot_text, "spoken_status": "completed"})
            self.send("bot-stopped-speaking")

    async def watch_pings(self) -> None:
        born = time.monotonic()
        while not self.closed:
            await asyncio.sleep(1)
            if self.channel is not None and time.monotonic() - self.last_ping > PING_TIMEOUT_SECONDS:
                logger.info("%s: ping kelmadi, qo'ng'iroq uzildi", self.pc_id)
                await self.close()
                return
            connected = self.pc is not None and self.pc.connectionState == "connected"
            if not connected and time.monotonic() - born > CONNECT_TIMEOUT_SECONDS:
                logger.info("%s: %ds ichida ulanmadi, yopildi", self.pc_id, CONNECT_TIMEOUT_SECONDS)
                await self.close()
                return

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for task in self.tasks:
            task.cancel()
        if self.pc is not None:
            await self.pc.close()
        peers.pop(self.pc_id, None)
        logger.info("%s: yopildi (%d ta tirik)", self.pc_id, len(peers))


def _refusal(key: str) -> JSONResponse | None:
    if not key or key == "reject-me":
        return JSONResponse({"detail": "unknown or missing api key"}, status_code=401)
    if key == "busy":
        return JSONResponse({"detail": "all lines busy"}, status_code=429, headers={"Retry-After": "3"})
    if key == "warming":
        return JSONResponse({"detail": "warming up"}, status_code=503, headers={"Retry-After": "5"})
    if key == "bad-offer":
        return JSONResponse({"detail": "invalid sdp: no usable media section"}, status_code=400)
    return None


def _key(request: Request) -> str:
    header = request.headers.get("x-api-key", "")
    if header:
        return header
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    return credential if scheme.lower() == "bearer" else ""


def _synthetic_answer() -> str:
    """aiortc yo'q bo'lganda: backend qabul qiladigan, brauzer ulana olmaydigan SDP."""
    return (
        "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
        "a=group:BUNDLE 0 1 2\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\nc=IN IP4 0.0.0.0\r\na=mid:0\r\na=sendrecv\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0\r\na=mid:1\r\na=inactive\r\n"
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\nc=IN IP4 0.0.0.0\r\na=mid:2\r\n"
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/offer")
async def offer(request: Request):
    refused = _refusal(_key(request))
    if refused is not None:
        return refused
    body = await request.json()
    if body.get("type") != "offer" or not str(body.get("sdp", "")).startswith("v=0"):
        return JSONResponse({"detail": "expected an sdp offer"}, status_code=400)

    pc_id = f"SmallWebRTCConnection#{next(_counter)}-{uuid.uuid4().hex[:8]}"
    peer = Peer(pc_id)
    peers[pc_id] = peer
    if not HAVE_AIORTC:
        logger.warning("%s: aiortc yo'q — soxta javob, brauzer ulana olmaydi", pc_id)
        # Hech qachon ulanmaydi, shuning uchun haqiqiy agentdek 60 s da unutiladi.
        peer.tasks.append(asyncio.ensure_future(peer.watch_pings()))
        return {"sdp": _synthetic_answer(), "type": "answer", "pc_id": pc_id}

    pc = peer.pc

    @pc.on("datachannel")
    def on_datachannel(channel) -> None:
        peer.channel = channel
        peer.last_ping = time.monotonic()
        peer.tasks.append(asyncio.ensure_future(peer.converse()))

        @channel.on("message")
        def on_message(message) -> None:
            if message == "ping":
                peer.last_ping = time.monotonic()

    @pc.on("track")
    def on_track(track) -> None:
        # Mikrofonni o'ziga qaytaradi: eshitilgan ovoz — ikki tomon ham ishlayotganining isboti.
        if track.kind == "audio":
            pc.addTrack(peer.relay.subscribe(track))

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        logger.info("%s: %s", pc_id, pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await peer.close()

    await pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type="offer"))
    await pc.setLocalDescription(await pc.createAnswer())
    peer.tasks.append(asyncio.ensure_future(peer.watch_pings()))
    logger.info("%s: javob berildi (%d ta tirik)", pc_id, len(peers))
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "pc_id": pc_id}


@app.patch("/api/offer")
async def candidates(request: Request):
    refused = _refusal(_key(request))
    if refused is not None:
        return refused
    body = await request.json()
    peer = peers.get(str(body.get("pc_id")))
    if peer is None:
        return JSONResponse({"detail": "unknown pc_id"}, status_code=404)
    if HAVE_AIORTC:
        for entry in body.get("candidates") or []:
            line = str(entry.get("candidate") or "")
            if not line:
                continue
            with contextlib.suppress(Exception):
                candidate = candidate_from_sdp(line.removeprefix("candidate:"))
                candidate.sdpMid = entry.get("sdp_mid")
                candidate.sdpMLineIndex = entry.get("sdp_mline_index")
                await peer.pc.addIceCandidate(candidate)
    return {"status": "success"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not HAVE_AIORTC:
        logger.warning("aiortc o'rnatilmagan: faqat soxta SDP javoblar. `pip install aiortc`")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
