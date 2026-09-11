# Synora speech to text

Two ways in, what they charge, and when the charge lands.

| You want | Route | What it costs |
| --- | --- | --- |
| One file transcribed | `POST /stt/transcribe` | The duration the service measured |
| Live text as somebody speaks | `WS /stt/stream` | The speech, plus a connection fee |
| What you transcribed, and the audio back | `GET /stt/transcriptions` | Nothing |

Like the speech side, this is a **gateway** rather than a wrapper. The
transcription box upstream holds one token, that token never leaves this
process, and a signed-in user calls us with their own JWT. We hold credit
against the audio, relay the upload, settle on what the service says it
transcribed, and write the row `GET /usage` reads from. The same count produces
the bill and the report, because there is only one count.

The one thing to read before writing client code is
[What a call costs, and when](#what-a-call-costs-and-when), because the hold
and the charge are deliberately different numbers.

```bash
API=http://127.0.0.1:8000/api/v1        # production: https://back.synora-ai.uz/api/v1
TOKEN=$(curl -s -X POST "$API/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"ali@example.com","password":"Str0ngPassw0rd"}' | jq -r .access_token)
A="Authorization: Bearer $TOKEN"
```

---

## `POST /stt/transcribe`

```bash
curl -X POST "$API/stt/transcribe" -H "$A" \
  -F 'file=@clip.wav' -F 'language=uz'
```

```json
{
  "ok": true,
  "ai_session_id": "db0e0ef5-b331-4362-b165-b0d5e23d9c76",
  "text": "Assalomu alaykum, bugun havo juda yaxshi.",
  "language": "uz",
  "audio_ms": 1280,
  "price_micros": 1200000,
  "price": "1.200000",
  "infer_seconds": 0.41
}
```

The same numbers come back on headers, for a client that wants the price
without deserialising:

```
x-synora-session-id: db0e0ef5-…
x-synora-audio-ms:   1280
x-synora-price:      1.200000
x-synora-price-micros: 1200000
```

`multipart/form-data`, two parts. `file` is the audio — anything the service's
decoder accepts, which is anything ffmpeg reads. `language` is one of `uz`,
`ru`, `en` and defaults to `uz`; anything else is refused here rather than
after a hold has been placed.

| Limit | Default | Refused with |
| --- | --- | --- |
| Upload size | 25 MB | `400 stt_audio_too_large` |
| Duration | 10 minutes | `400 stt_audio_too_long` |
| Empty body | — | `400 stt_audio_empty` |

**Send compressed audio.** The two limits are independent, and for uncompressed
WAV the size one bites first — long before ten minutes:

| Format | Ten minutes weighs | Verdict |
| --- | --- | --- |
| mp3 / m4a at 128 kbps | ~9 MB | fits, with room |
| WAV 16 kHz mono | 18 MB | fits |
| WAV 48 kHz mono | 55 MB | refused at ~4.5 minutes |
| WAV 48 kHz stereo | 110 MB | refused at ~2 minutes |

Nothing is lost by compressing: the service resamples to 16 kHz mono before it
transcribes, so a 48 kHz stereo WAV is ten times the bytes for identical text.
A WAV that is refused on size says so in its message, with the duration read
out of its own header and an estimate of what the same audio would weigh
compressed.

---

## What a call costs, and when

Transcription is sold **by duration**. One metric, `stt_audio_ms`, priced from
the versioned price book — the placeholder seed is 1 200 000 micro-credits per
minute, rounded **up** to the whole unit, so a one-second clip and a
fifty-nine-second clip both cost 1.200000.

```
price = ceil(audio_ms / 60000) * price_per_minute
```

### The hold is an estimate; the charge is not

This is the one place where the speech and transcription gateways differ in
kind. For TTS the billable quantity is in the request — it is the length of the
text — so the price is known before any work starts. Here the billable quantity
is the duration of a file nothing on our side has decoded.

So credit is held against an estimate and the bill is settled against the
service's own count:

```
POST /stt/transcribe
  │
  ├─ estimate the duration      exact for wav, generous for everything else
  ├─ price it, place the hold ──────────► 402 if the wallet cannot cover it
  ├─ upload ────────────────────────────► 400/429/502/503 here: hold released, charge 0
  │
  └─ 200 with the transcript
       └─ release the hold, debit `audio_seconds` — one transaction
```

**For `wav` and `pcm` the estimate is exact**, read straight out of the RIFF
header. For compressed formats it is the byte count over a deliberately low
assumed bitrate — 64 kbps, where speech is usually 128 — so the estimate lands
*above* the true duration.

The direction is chosen rather than the accuracy. An over-estimate reserves
more credit than the call costs and gives the difference straight back at
settlement: a caller may briefly see a larger `reserved` than the final price,
which is visible and harmless. An under-estimate is silent, because a
settlement is clamped at the hold — the call would bill less than the audio it
transcribed and only a `disputed` flag would record it. One of those is a
support question; the other is revenue nobody counts.

**If you want a smaller hold, send `wav`.**

### An upstream that over-reports is clamped

A settlement can never exceed the hold. A service that starts reporting ten
minutes for a ten-second clip is charged at the hold and its session is flagged
`disputed` for review, rather than quietly emptying a wallet.

### Nothing is charged for a transcription that did not happen

Audio the service cannot decode, a language it does not serve, a model that is
still loading, a network that dropped — every one of them releases the hold in
full and writes no usage event. The only thing that is billed is a response
that arrived.

---

## `WS /stt/stream` — realtime

Live transcription over a websocket: send audio as it is captured, get a
transcript back per segment as the speaker pauses. OpenAPI cannot describe a
websocket, which is why this section exists instead of a `responses=` block.

```
wss://…/api/v1/stt/stream

→ {"type":"start","token":"<access token>","language":"uz","sample_rate":16000}
→ binary frames: little-endian PCM16, mono, at the declared rate
→ {"type":"stop"}

← {"type":"ready","ai_session_id":"…","max_seconds":600}
← {"type":"speech_started"}
← {"type":"final","seq":0,"text":"Assalomu alaykum","language":"uz","audio_ms":1136}
← {"type":"done","segments":6,"audio_ms":14576,"session_ms":16177,
    "price":"1.400000","price_micros":1400000,"end_reason":"stop_requested"}
← {"type":"error","code":"…","message":"…"}
```

**The token goes in the `start` message, not in the query string.** A browser
cannot set headers on a `WebSocket`, so something has to carry it — and a URL
is the one place a credential must not go, because query strings land in nginx
access logs, in `Referer` headers and in error reports. A non-browser client
may send `Authorization: Bearer` instead; that is checked first.

**Wait for `done`, not for the first `final`.** One spoken turn routinely
closes several VAD segments — a fifteen-second voicemail in testing produced
six — so a client that closes on the first one truncates its own transcript.
`done` is also where the bill is.

`speech_started` is relayed unchanged from the model's VAD and is the
**barge-in trigger**: the moment to stop whatever your agent is playing.

### What a live session costs

Two metrics, and they are not the same number:

| Metric | What it is |
| --- | --- |
| `stt_audio_ms` | the audio VAD closed a segment on — the speech |
| `session_ms` | the wall clock the socket was open |

Both are charged. Audio alone would let a caller hold a GPU slot open in
silence for nothing, and the transcription service's own documentation is
explicit that a live session occupies the card for its whole duration. Wall
clock alone would charge the same for ten minutes of speech as for ten minutes
of nothing, and would make the identical recording cost differently depending
on whether it was streamed or uploaded.

The gap between them is real and worth expecting on an invoice: the test call
above was **14.6 seconds of speech across a socket open for 16.2 seconds**. VAD
trims the silence; the connection fee covers it.

### The hold is the ceiling

Credit is reserved up front for the **whole cap** — `STT_STREAM_MAX_SECONDS` of
connection *and* the same span of continuous speech — because a websocket has
no length until it ends. At the seeded prices a ten-minute cap reserves about
14 credits, and everything unused comes back at settlement.

That is a real constraint, stated plainly: **an account with less than the
ceiling cannot open a stream at all**, even to ask a ten-second question. The
fix is hold extension mid-session, which this API does not have yet; until it
does, `STT_STREAM_MAX_SECONDS` is the dial.

### How a session ends

| `end_reason` | Cause |
| --- | --- |
| `stop_requested` | you sent `{"type":"stop"}` |
| `client_disconnected` | the socket dropped |
| `grace_exhausted` | the session hit `STT_STREAM_MAX_SECONDS` |
| `heartbeat_timeout` | no audio for `STT_STREAM_IDLE_SECONDS` |
| `upstream_error` | the transcription service went away |

Every one of them settles. Whatever ended the socket, `stop` is still sent
upstream first, so segments it has already closed — transcript you have paid
for — are not thrown away.

A handshake the service refuses charges nothing at all: no audio moved, the
hold goes back whole, and the `error` event carries the same code the file
route would have returned.

---

## Kept transcriptions

Every charged transcription is kept: the transcript, and the audio that was
uploaded.

```bash
curl "$API/stt/transcriptions?limit=25" -H "$A"
```

```json
{
  "ok": true,
  "transcriptions": [
    {
      "id": "5a7c0e19-…",
      "ai_session_id": "db0e0ef5-…",
      "text": "Assalomu alaykum, bugun havo juda yaxshi.",
      "language": "uz",
      "audio_ms": 1280,
      "infer_ms": 413,
      "filename": "clip.wav",
      "content_type": "audio/wav",
      "audio_bytes": 169004,
      "sha256": "9f2c…",
      "created_at": "2026-09-11T06:12:44.031Z"
    }
  ],
  "page": { "next_cursor": null, "has_more": false, "limit": 25 }
}
```

The audio is a second request, and it costs nothing — the work was paid for
when it happened:

```bash
curl "$API/stt/transcriptions/$ID/audio" -H "$A" -o uploaded.wav
shasum -a 256 uploaded.wav      # equals the `sha256` field
```

### One file can belong to two records

Audio is stored under the sha256 of its own bytes, so **transcribing something
you synthesised here stores one file with a row in each table** — one in
`tts_recordings`, one in `stt_transcriptions`. That is the ordinary result of
using both gateways together, not a corner case.

So neither delete unlinks a file on sight. Deleting a transcription removes the
row and only removes the file once no record of either kind still names it:

```bash
curl -X DELETE "$API/stt/transcriptions/$ID" -H "$A"
```

Erasing what was transcribed does not erase that it was paid for —
`usage_events`, the ledger and `GET /usage` are untouched.

### What is not kept

| Case | Why |
| --- | --- |
| The service refused the audio or the language | Nothing was produced and nothing was charged |
| A retry under a spent `Idempotency-Key` | It never reached the model; the original is already here |
| Anything at all while `RECORDINGS_ENABLED=false` | The deployment opted out |

`RECORDINGS_ENABLED` is one switch over both gateways. What it keeps on this
side is audio a *user* uploaded, which is a heavier thing than speech we
produced — worth deciding deliberately rather than inheriting.

---

## Errors

Branch on `code`, never on the message text.

| Code | Status | Means | Do |
| --- | --- | --- | --- |
| `stt_not_configured` | 503 | This deployment has no transcription service wired up | Nothing client-side. Set `STT_BASE_URL` and `STT_API_KEY` |
| `stt_key_rejected` | 503 | Upstream refused **our** token | Nothing client-side. Logged at ERROR; somebody rotates a token |
| `stt_not_ready` | 503 | The model is still loading | Retry — this one carries `Retry-After` |
| `stt_busy` | 429 | Upstream is saturated | Honour `Retry-After` |
| `stt_rejected_input` | 400 | Upstream refused **your** audio or language. The message is its own | Fix the file; do not retry unchanged |
| `stt_language_unsupported` | 400 | Not one of `uz`, `ru`, `en` | Ours, before any hold |
| `stt_audio_too_large` | 400 | Past the size ceiling. For a WAV the message says how long it is and what it would weigh compressed | Compress it. Ours, before any hold |
| `stt_audio_too_long` | 400 | Past the duration ceiling — exact for WAV, an over-estimate for compressed audio | Split it. Ours, before any hold |
| `stt_audio_empty` | 400 | Nothing at all | Ours, before any hold |
| `stt_unreachable` | 502 | Timeout, transport failure, redirect or upstream 5xx | Retry with backoff |
| `stt_unreadable` | 502 | A 2xx we could not parse as a transcript | Retry once, then report it |
| `stt_idempotency_spent` | 409 | This key has already been charged | Send a new key |
| `transcription_not_found` | 404 | No such transcription, or it is not yours | Nothing. A 403 would confirm one exists |
| `transcription_audio_missing` | 404 | The row is there, the file is not | Report it. A restore missed `RECORDINGS_DIR` |
| `insufficient_balance` | 402 | The wallet cannot cover the hold | Top up by `shortfallMicros` |

Note which 503s are worth retrying: only `stt_not_ready`, and it is the only
one that carries a `Retry-After`. The other two need a human.

### `Idempotency-Key`

It means *do not charge twice*, and it cannot mean *give me the first answer*:
transcripts are not stored, so there is nothing to hand back. A spent key is a
`409 stt_idempotency_spent`, which tells a retry loop to stop rather than to
open a second session.

---

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `STT_BASE_URL` | unset | Empty ⇒ the route answers `503 stt_not_configured` |
| `STT_API_KEY` | unset | Sent as **`X-Token`**, not `X-API-Key` — this is a different upstream from the speech box. Setting one of the pair without the other is refused at boot outside development |
| `STT_MODEL_KEY` | `synora-stt` | Which price book row transcription bills against |
| `STT_CONNECT_TIMEOUT_SECONDS` | `10` | |
| `STT_READ_TIMEOUT_SECONDS` | `300` | Covers a cold model load and the upload itself |
| `STT_MAX_AUDIO_BYTES` | `26214400` | 25 MB |
| `STT_MAX_AUDIO_SECONDS` | `600` | Past this, a caller wants a job rather than a request that hangs |
| `STT_ASSUMED_BYTES_PER_SECOND` | `8000` | For the hold only, and only for compressed audio |
| `STT_STREAM_MAX_SECONDS` | `600` | The cap on one live session, and what its hold is priced from |
| `STT_STREAM_IDLE_SECONDS` | `60` | A socket that sends no audio for this long is closed and settled |
| `RECORDINGS_ENABLED` / `RECORDINGS_DIR` | `true` / `data/recordings` | Keep the transcript and the uploaded audio. One switch over both gateways |

Prices are not here. They live in the database, versioned, and are published
through the admin API — see
[Credits and the wallet](../README.md#credits-and-the-wallet).

---

## Trying it locally

Everything in [docs/TTS.md → Trying it locally](TTS.md#trying-it-locally)
applies; add the two variables and point them at a transcription service:

```bash
STT_BASE_URL=https://your-stt-box.example
STT_API_KEY=...
```

The startup log says whether it took:

```
synora: STT gateway: https://your-stt-box.example
```

`not configured (transcription answers 503)` means one of the pair did not
reach the process.

```bash
curl -s -X POST "$API/stt/transcribe" -H "$A" -F 'file=@clip.wav' -F 'language=uz'
curl -s "$API/usage" -H "$A"          # → stt/stt_audio_ms
curl -s "$API/wallet" -H "$A"         # → reserved back at 0.000000
```

**`reserved` back at zero is the assertion worth making every time**, exactly
as it is for speech: a charge that is a little wrong is a bug, and a hold that
never came back is credit the customer cannot spend that nothing will notice.
