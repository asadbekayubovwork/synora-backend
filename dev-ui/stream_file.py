#!/usr/bin/env python3
"""Send a wav file through `WS /stt/stream`, as if a microphone were speaking.

    python dev-ui/stream_file.py clip.wav                       # local API
    python dev-ui/stream_file.py clip.wav --language en \
        --api http://127.0.0.1:8000/api/v1 \
        --email ali@example.com --password Str0ngPassw0rd

The browser tab in `index.html` is the better demo — it uses a real microphone
and shows the bill next to the transcript. This exists for the two cases that
tab cannot cover: a machine with no microphone, and a repeatable input. The
same file every time is what makes "did that change break the transcript"
answerable.

Audio is paced in real time on purpose. Firing a whole file at the socket as
fast as it reads would still transcribe, but it would tell you nothing about
latency and would make `session_ms` — which the connection fee is charged on —
a number with no relationship to a real call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave

FRAME_SECONDS = 0.02


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("clip", help="a mono PCM16 wav file")
    parser.add_argument("--api", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--language", default="uz", choices=("uz", "ru", "en"))
    parser.add_argument("--email", default="ali@example.com")
    parser.add_argument("--password", default="Str0ngPassw0rd")
    parser.add_argument("--token", help="skip the login and use this access token")
    args = parser.parse_args()

    import httpx
    import websockets

    with wave.open(args.clip, "rb") as clip:
        if clip.getnchannels() != 1 or clip.getsampwidth() != 2:
            print("The clip must be mono PCM16. Convert it:", file=sys.stderr)
            print(
                f"  ffmpeg -i {args.clip} -ar 16000 -ac 1 -c:a pcm_s16le out.wav",
                file=sys.stderr,
            )
            return 2
        rate = clip.getframerate()
        pcm = clip.readframes(clip.getnframes())

    token = args.token
    if not token:
        async with httpx.AsyncClient(timeout=30) as http:
            answer = await http.post(
                f"{args.api}/auth/login",
                json={"email": args.email, "password": args.password},
            )
            answer.raise_for_status()
            token = answer.json()["access_token"]

    url = args.api.replace("http://", "ws://").replace("https://", "wss://") + "/stt/stream"
    print(f"{len(pcm) / 2 / rate:.1f}s of audio at {rate} Hz → {url}")

    started = time.perf_counter()
    finished = asyncio.Event()

    async with websockets.connect(url, max_size=None) as socket:
        # The token goes in `start`, not in the query string: a URL is the one
        # place a credential must not be, because it lands in access logs.
        await socket.send(
            json.dumps(
                {
                    "type": "start",
                    "token": token,
                    "language": args.language,
                    "sample_rate": rate,
                }
            )
        )

        async def read() -> None:
            async for raw in socket:
                event = json.loads(raw)
                at = time.perf_counter() - started
                kind = event.get("type")
                if kind == "final":
                    print(
                        f"[{at:6.2f}s] final #{event['seq']} "
                        f"{event['audio_ms']:>6} ms  {event['text']}"
                    )
                elif kind == "done":
                    print(
                        f"[{at:6.2f}s] done   segments={event['segments']} "
                        f"speech={event['audio_ms']}ms socket={event['session_ms']}ms "
                        f"price={event['price']} ({event['end_reason']})"
                    )
                    finished.set()
                    return
                elif kind == "error":
                    print(f"[{at:6.2f}s] error  {event['code']}: {event['message']}")
                    finished.set()
                    return
                else:
                    print(f"[{at:6.2f}s] {kind}")

        reader = asyncio.create_task(read())
        step = int(rate * FRAME_SECONDS) * 2
        for offset in range(0, len(pcm), step):
            if finished.is_set():
                break
            await socket.send(pcm[offset : offset + step])
            await asyncio.sleep(FRAME_SECONDS)

        if not finished.is_set():
            print(f"[{time.perf_counter() - started:6.2f}s] audio sent → stop")
            await socket.send(json.dumps({"type": "stop"}))
            # `done` is the end of the transcript, not the first `final`: one
            # spoken turn routinely closes several VAD segments.
            await asyncio.wait_for(finished.wait(), timeout=120)
        reader.cancel()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
