#!/usr/bin/env python3
"""Soxta nutq xizmati — GPU'siz `/tts` oqimini uchidan-uchiga ko'rish uchun.

Haqiqiy speech box o'rniga turadi: `tts_client` kutgan yo'llar, JSON kalitlari
va `x-audio-sample-rate` headeri. Audio — oddiy sinus toni, nutq emas: maqsad
ovozni tinglash emas, **pulning harakatini** ko'rish (hold → release → debit,
`X-Synora-Price`, `reserved` nolga qaytishi).

    python dev-ui/fake_speech_box.py            # → http://127.0.0.1:8100

API'ni shunga qarating:

    TTS_BASE_URL=http://127.0.0.1:8100 TTS_API_KEY=fake-key \
        .venv/bin/uvicorn app.main:app --port 8000

Xato yo'llarini sinash uchun `TTS_API_KEY` ni almashtirasiz — kalitning o'zi
rejimni tanlaydi:

| TTS_API_KEY | Upstream | Bizning javob |
| --- | --- | --- |
| boshqa har qanday | 200 | audio |
| `reject-me` | 401 | `503 tts_key_rejected` |
| `quota` | 402 | `503 tts_quota_exhausted` |
| `busy` | 429 + Retry-After | `429 tts_busy` |
| `bad-input` | 422 | `400 tts_rejected_input` |
| `garbage` | 200, JSON emas | JSON routelarda `502 tts_unreadable` |

`garbage` faqat JSON javob kutilgan joyda ko'rinadi — `/tts/voices`, `/tts/batch`.
`/tts/speech` esa 2xx tanasini o'qimaydi, xom baytlarni uzatadi, shuning uchun u
`200` qaytaradi va buzuq "audio" pleyerga boradi. Bu xato emas: "birinchi
baytdan keyingi nosozlik hisoblanadi va uzatiladi" degan qoidaning ko'rinishi.

Batch har o'qishda bir qadam siljiydi: pending → running → succeeded.
"""

from __future__ import annotations

import json
import math
import re
import struct
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Belgiga to'g'ri keladigan audio. Haqiqiy sintezga o'xshab, uzun matn uzun
# audio beradi — shunda oqim bo'lak-bo'lak kelishi ko'rinadi.
MS_PER_CHARACTER = 40
MIN_MS, MAX_MS = 300, 20_000
CHUNK_MS = 200          # bir yozishda ketadigan audio
CHUNK_DELAY = 0.05      # bo'laklar orasidagi pauza, GPU o'rnida

VOICES = [
    {"voice_id": "uz-male-1", "display_name": "Sardor", "supports_ultimate": True,
     "reference_seconds": 12.5, "has_speaker_embedding": True},
    {"voice_id": "uz-female-1", "display_name": "Nilufar", "supports_ultimate": False,
     "reference_seconds": 8.0, "has_speaker_embedding": True},
]

jobs: dict[str, dict] = {}
clones: list[dict] = []
tenant_characters = 0


def wav(samples: bytes, sample_rate: int) -> bytes:
    """16-bit mono RIFF. Baytlar xom namunalar bo'lgani uchun bayt soni va
    davomiylik bir faktning ikki o'lchovi — `tts_service` ham shunga tayanadi."""
    return (
        b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(samples)) + samples
    )


def tone(ms: int, sample_rate: int) -> bytes:
    """Ozgina tebranadigan sinus — jim fayl bilan buzuq faylni ajratish uchun."""
    total = int(sample_rate * ms / 1000)
    out = bytearray()
    for i in range(total):
        t = i / sample_rate
        envelope = min(1.0, t * 8) * min(1.0, (ms / 1000 - t) * 8 + 0.001)
        value = math.sin(2 * math.pi * (200 + 40 * math.sin(t * 3)) * t)
        out += struct.pack("<h", int(max(-1, min(1, value * envelope)) * 12000))
    return bytes(out)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeSpeechBox/1"

    # -------------------------------------------------------------- yordamchi
    @property
    def mode(self) -> str:
        return self.headers.get("X-API-Key", "")

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def send_json(self, status: int, payload, extra: dict | None = None) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def refuse(self) -> bool:
        """Kalit bilan tanlangan xato rejimi. `True` — javob allaqachon ketdi."""
        if self.mode == "reject-me":
            self.send_json(401, {"detail": "API kaliti rad etildi"})
        elif self.mode == "quota":
            self.send_json(402, {"detail": "tenant kvotasi tugagan"})
        elif self.mode == "busy":
            self.send_json(429, {"detail": "GPU band"}, {"Retry-After": "7"})
        elif self.mode == "bad-input":
            self.send_json(422, {"detail": [{"loc": ["body", "text"], "msg": "matn juda uzun"}]})
        elif self.mode == "garbage":
            data = b"<html>bu JSON emas</html>"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            return False
        return True

    # -------------------------------------------------------------- routelar
    def do_GET(self) -> None:
        if self.path == "/readyz":
            self.send_json(200, {"ready": True})
            return
        if self.refuse():
            return
        if self.path == "/v1/voices":
            self.send_json(200, {"voices": VOICES + clones})
            return
        if self.path == "/v1/usage":
            self.send_json(200, {"characters": tenant_characters})
            return
        m = re.fullmatch(r"/v1/batch/([^/]+)", self.path)
        if m:
            self.send_json(*self.job_status(m.group(1)))
            return
        m = re.fullmatch(r"/v1/batch/([^/]+)/results", self.path)
        if m:
            job = jobs.get(m.group(1))
            if not job:
                self.send_json(404, {"detail": "bunday job yo'q"})
                return
            self.send_json(200, {"job_id": job["job_id"], "state": job["state"],
                                 "results": job["results"]})
            return
        self.send_json(404, {"detail": "bunday yo'l yo'q: " + self.path})

    def do_POST(self) -> None:
        payload = self.body()
        if self.refuse():
            return
        if self.path == "/v1/tts/stream":
            self.stream(payload)
        elif self.path == "/v1/voices":
            voice = {"voice_id": "clone-" + uuid.uuid4().hex[:8],
                     "display_name": payload.get("display_name") or "klon",
                     "supports_ultimate": False,
                     "reference_seconds": 6.0, "has_speaker_embedding": True}
            clones.append(voice)
            self.send_json(201, voice)
        elif self.path == "/v1/batch":
            self.send_json(202, self.create_job(payload))
        else:
            self.send_json(404, {"detail": "bunday yo'l yo'q: " + self.path})

    def do_DELETE(self) -> None:
        if self.refuse():
            return
        m = re.fullmatch(r"/v1/voices/(.+)", self.path)
        if m:
            before = len(clones)
            clones[:] = [v for v in clones if v["voice_id"] != m.group(1)]
            self.send_json(200 if len(clones) < before else 404,
                           {"deleted": m.group(1)} if len(clones) < before
                           else {"detail": "bunday ovoz yo'q"})
            return
        m = re.fullmatch(r"/v1/batch/([^/]+)", self.path)
        if m:
            job = jobs.get(m.group(1))
            if not job:
                self.send_json(404, {"detail": "bunday job yo'q"})
                return
            job["state"] = "cancelled"
            self.send_json(200, self.job_payload(job))
            return
        self.send_json(404, {"detail": "bunday yo'l yo'q: " + self.path})

    # -------------------------------------------------------------- sintez
    def stream(self, payload: dict) -> None:
        """Audio, bo'lak-bo'lak. Headerlar birinchi baytdan oldin ketadi —
        gateway ham narxni aynan shu paytda qat'iylashtiradi."""
        global tenant_characters
        text = payload.get("text") or ""
        sample_rate = int(payload.get("sample_rate") or 48000)
        fmt = (payload.get("format") or "wav").lower()
        ms = max(MIN_MS, min(MAX_MS, len(text) * MS_PER_CHARACTER))
        samples = tone(ms, sample_rate)
        audio = samples if fmt == "pcm" else wav(samples, sample_rate)
        tenant_characters += len(text)

        if fmt in ("mp3", "opus"):
            print(f"  ! {fmt} so'raldi — soxta box faqat wav/pcm chiqaradi, "
                  f"pleyer bu baytlarni ochmasligi mumkin", file=sys.stderr)

        self.send_response(200)
        self.send_header("Content-Type", {"mp3": "audio/mpeg", "opus": "audio/opus",
                                          "pcm": "application/octet-stream"}.get(fmt, "audio/wav"))
        self.send_header("Content-Length", str(len(audio)))
        # Streaming endpointning yagona usage-ga o'xshash headeri.
        self.send_header("x-audio-sample-rate", str(sample_rate))
        self.end_headers()

        step = max(1, int(sample_rate * 2 * CHUNK_MS / 1000))
        for i in range(0, len(audio), step):
            self.wfile.write(audio[i:i + step])
            self.wfile.flush()
            time.sleep(CHUNK_DELAY)
        print(f"  → {len(text)} belgi, {ms} ms, {len(audio)} bayt, {fmt}", file=sys.stderr)

    # -------------------------------------------------------------- batch
    def create_job(self, payload: dict) -> dict:
        items = payload.get("items") or []
        job_id = "fake-" + uuid.uuid4().hex[:10]
        jobs[job_id] = {
            "job_id": job_id,
            "state": "pending",
            "polls": 0,
            "items": items,
            "results": [],
            "characters": sum(len(it.get("text") or "") for it in items),
        }
        return self.job_payload(jobs[job_id])

    def job_status(self, job_id: str):
        job = jobs.get(job_id)
        if not job:
            return 404, {"detail": "bunday job yo'q"}
        # Har o'qishda bir qadam: pending → running → succeeded.
        if job["state"] in ("pending", "running"):
            job["polls"] += 1
            job["state"] = "running" if job["polls"] < 2 else "succeeded"
            if job["state"] == "succeeded":
                job["results"] = [
                    {"id": it.get("id"), "ok": True,
                     "path": f"/audio/{job_id}/{it.get('id')}.wav",
                     "characters": len(it.get("text") or ""),
                     "audio_seconds": round(len(it.get("text") or "") * MS_PER_CHARACTER / 1000, 3),
                     "similarity": 0.97, "retries": 0}
                    for it in job["items"]
                ]
        return 200, self.job_payload(job)

    def job_payload(self, job: dict) -> dict:
        done = [r for r in job["results"] if r["ok"]]
        return {
            "job_id": job["job_id"],
            "state": job["state"],
            "completed_items": len(done),
            "failed_items": 0,
            # Gateway aynan `usage.characters` ustidan hisoblaydi — hisob-kitob
            # yuborilgan matndan emas, upstream tan olgan miqdordan chiqadi.
            "usage": {
                "characters": sum(r["characters"] for r in done),
                "audio_seconds": sum(r["audio_seconds"] for r in done),
            },
        }

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s\n" % (fmt % args))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"Soxta speech box: http://127.0.0.1:{port}")
        print(f"  TTS_BASE_URL=http://127.0.0.1:{port} TTS_API_KEY=fake-key")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
