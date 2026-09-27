#!/usr/bin/env python3
"""Brauzersiz ovozli qo'ng'iroq — `/voice` gateway'ni terminaldan uchidan-uchiga.

`stream_file.py` realtime STT uchun qilgan ishni ovozli agent uchun qiladi: bizning
API orqali haqiqiy WebRTC qo'ng'iroq ochadi, shunda brauzer, mikrofon va UI
bo'lmasa ham butun zanjir tekshiriladi — SDP bizdan o'zgarmay o'tadimi, agent
javob beradimi, audio ikki tomonga oqadimi, heartbeat va hisob to'g'rimi.

    pip install aiortc httpx                  # faqat dev uchun
    python dev-ui/voice_call.py --email ali@example.com --password Str0ngPassw0rd
    python dev-ui/voice_call.py ... --seconds 40 --wav clip.wav
    python dev-ui/voice_call.py ... --events              # har bir data channel hodisasi
    python dev-ui/voice_call.py ... --no-hangup           # qulagan tab: server o'zi yopadi
    python dev-ui/voice_call.py ... --seconds 240 --no-heartbeat --no-hangup
                                                          # heartbeat'siz suiiste'mol: baribir hisoblanadi

Agent sifatida `fake_voice_agent.py` ham, haqiqiy agent ham bo'ladi — farqi faqat
API'ning `VOICE_AGENT_BASE_URL`ida. Mikrofon o'rniga 440 Hz ton (yoki `--wav`)
yuboriladi; agentdan kelgan audio kadrlar sanaladi va oxirida chop etiladi.

Chiqish kodi 0 — qo'ng'iroq ulandi, audio ikki tomonga oqdi va hisob yopildi.
"""

from __future__ import annotations

import argparse
import asyncio
import fractions
import json
import math
import sys
import time

import httpx

try:
    import numpy as np
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.contrib.media import MediaPlayer
    from av import AudioFrame
except ImportError:  # pragma: no cover - dev tool
    sys.exit("aiortc kerak: pip install aiortc httpx")

SAMPLE_RATE = 48_000
FRAME_SAMPLES = 960  # 20 ms


class ToneTrack(MediaStreamTrack):
    """Mikrofon o'rnida: 20 ms'lik 440 Hz ton kadrlari, real vaqt tezligida."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._pts = 0
        self._started: float | None = None

    async def recv(self) -> AudioFrame:
        if self._started is None:
            self._started = time.monotonic()
        wait = self._started + self._pts / SAMPLE_RATE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        t = (np.arange(FRAME_SAMPLES) + self._pts) / SAMPLE_RATE
        samples = (0.2 * 32767 * np.sin(2 * math.pi * 440 * t)).astype(np.int16)
        frame = AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._pts += FRAME_SAMPLES
        return frame


def fail(message: str) -> None:
    print(f"XATO: {message}", file=sys.stderr)
    raise SystemExit(1)


async def call(args: argparse.Namespace) -> int:
    api = httpx.AsyncClient(base_url=args.api.rstrip("/"), timeout=60)
    login = await api.post("/auth/login", json={"email": args.email, "password": args.password})
    if login.status_code != 200:
        fail(f"login {login.status_code}: {login.text}")
    api.headers["Authorization"] = "Bearer " + login.json()["access_token"]

    config = (await api.get("/voice/config")).json()
    print(f"config: available={config['available']} hold={config.get('hold')} "
          f"per_minute={config.get('per_minute')} heartbeat={config['heartbeat_interval_seconds']}s")
    if not config["available"]:
        fail("voice agent sozlanmagan (VOICE_AGENT_BASE_URL/VOICE_AGENT_API_KEY)")

    pc = RTCPeerConnection(RTCConfiguration(iceServers=[
        RTCIceServer(**{k: v for k, v in server.items() if k in ("urls", "username", "credential")})
        for server in config["ice_servers"]
    ]))
    connected = asyncio.Event()
    frames_in = 0
    events: list[dict] = []

    source = MediaPlayer(args.wav).audio if args.wav else ToneTrack()
    pc.addTransceiver(source, direction="sendrecv")
    # 1-qoida: video transceiver, kamera ochilmaydi.
    pc.addTransceiver("video", direction="sendrecv")
    # 2-qoida: data channel'ni klient ochadi.
    channel = pc.createDataChannel("events")

    async def ping() -> None:
        # 3-qoida: har soniyada ping, aks holda agent 3 soniyada uzadi.
        while True:
            if channel.readyState == "open":
                channel.send("ping")
            await asyncio.sleep(1)

    @channel.on("message")
    def on_message(message) -> None:
        try:
            event = json.loads(message)
        except ValueError:
            return
        events.append(event)
        if args.events:
            print(f"  [event] {json.dumps(event, ensure_ascii=False)[:900]}")
        data = event.get("data") or {}
        if event.get("type") == "user-transcription" and data.get("final"):
            print(f"  siz:   {data.get('text')}")
        elif event.get("type") == "bot-output" and data.get("spoken_status") == "completed":
            print(f"  agent: {data.get('text')}")

    @pc.on("track")
    def on_track(track) -> None:
        async def drain() -> None:
            nonlocal frames_in
            while True:
                try:
                    await track.recv()
                except Exception:  # noqa: BLE001 - trek yopildi
                    return
                frames_in += 1
        asyncio.ensure_future(drain())

    @pc.on("connectionstatechange")
    def on_state() -> None:
        print(f"peer: {pc.connectionState}")
        if pc.connectionState == "connected":
            connected.set()

    # aiortc nomzodlarni setLocalDescription ichida to'liq yig'adi va offerga
    # yozadi, shuning uchun bu klientda /candidates ga ehtiyoj yo'q — brauzer
    # esa trickle qiladi, voice-agent-client.js ularni batch'lab yuboradi.
    await pc.setLocalDescription(await pc.createOffer())
    started = time.monotonic()
    opened = await api.post("/voice/sessions", json={"sdp": pc.localDescription.sdp, "type": "offer"})
    if opened.status_code != 201:
        await pc.close()
        fail(f"POST /voice/sessions {opened.status_code}: {opened.text}")
    session = opened.json()
    sid = session["ai_session_id"]
    print(f"answer: sessiya {sid} · reserved {session['reserved']} · "
          f"{(time.monotonic() - started) * 1000:.0f} ms")
    await pc.setRemoteDescription(RTCSessionDescription(sdp=session["sdp"], type="answer"))

    pinger = asyncio.ensure_future(ping())
    try:
        # Brauzer klientidagi CONNECT_TIMEOUT bilan bir xil: ikki NAT orqali ICE
        # 30 s dan ko'p vaqt olgani o'lchangan.
        await asyncio.wait_for(connected.wait(), timeout=45)
    except TimeoutError:
        pinger.cancel()
        await pc.close()
        bill = (await api.delete(f"/voice/sessions/{sid}")).json()
        fail(f"ICE ulanmadi — hisob {bill.get('price')} (ulanmagan qo'ng'iroq bepul bo'lishi kerak)")

    deadline = time.monotonic() + args.seconds
    interval = config["heartbeat_interval_seconds"]
    if args.no_heartbeat:
        # O'zgartirilgan klient: heartbeat yo'q, lekin agent bilan gaplashishda
        # davom etadi. Server buni agentdan so'rab bilishi va hisoblashi kerak.
        print(f"heartbeat yuborilmaydi — {args.seconds:.0f}s shunchaki gaplashamiz…")
        await asyncio.sleep(args.seconds)
    while not args.no_heartbeat and time.monotonic() < deadline:
        pulse = (await api.post(f"/voice/sessions/{sid}/heartbeat")).json()
        print(f"heartbeat: {pulse.get('action')} · {pulse.get('elapsed_ms')} ms · "
              f"qoldi {pulse.get('remaining_ms')} ms")
        if pulse.get("action") == "stop":
            break
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    pinger.cancel()
    await pc.close()
    if args.no_hangup:
        # Qulagan tab: DELETE yo'q, heartbeat to'xtadi. Hisobni server o'zi
        # yopishi kerak — oxirgi heartbeat + bitta interval, `disputed` bilan.
        print("DELETE yuborilmadi — server heartbeat timeout'ini kutyapti…")
        wait_until = time.monotonic() + config["heartbeat_timeout_seconds"] + 90
        while time.monotonic() < wait_until:
            bill = (await api.get(f"/voice/sessions/{sid}")).json()
            if bill["status"] == "ended":
                break
            await asyncio.sleep(5)
        else:
            fail("server qo'ng'iroqni o'zi yopmadi")
        again = bill
    else:
        bill = (await api.delete(f"/voice/sessions/{sid}")).json()
        again = (await api.delete(f"/voice/sessions/{sid}")).json()
    wallet = (await api.get("/wallet")).json()
    await api.aclose()

    print(f"hisob: {bill['billed_ms']} ms · {bill['price']} · {bill['end_reason']} · "
          f"disputed={bill['disputed']} · reserved={bill['reserved_micros']}")
    kinds: dict[str, int] = {}
    for event in events:
        kinds[str(event.get("type"))] = kinds.get(str(event.get("type")), 0) + 1
    print(f"agentdan audio kadrlar: {frames_in} · data channel hodisalari: {len(events)} "
          f"{dict(sorted(kinds.items()))}")
    print(f"wallet: available {wallet['available']} · reserved {wallet['reserved']}")

    problems = []
    if frames_in == 0:
        problems.append("agentdan audio kelmadi")
    if bill["status"] != "ended" or bill["reserved_micros"] != 0:
        problems.append("hold qaytmadi")
    if again != bill:
        problems.append("ikkinchi DELETE boshqa javob berdi")
    if wallet["reserved_micros"] != 0:
        problems.append("wallet.reserved nolga qaytmadi")
    for problem in problems:
        print(f"XATO: {problem}", file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--wav", help="mikrofon o'rniga fayl (ixtiyoriy)")
    parser.add_argument("--events", action="store_true", help="har bir data channel hodisasini chop etadi")
    parser.add_argument(
        "--no-heartbeat",
        action="store_true",
        help="heartbeat'siz qo'ng'iroq (suiiste'mol sinovi); --no-hangup bilan birga ishlating",
    )
    parser.add_argument(
        "--no-hangup",
        action="store_true",
        help="qulagan tabni taqlid qiladi: DELETE'siz ketadi va serverning o'zi yopishini kutadi",
    )
    return asyncio.run(call(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
