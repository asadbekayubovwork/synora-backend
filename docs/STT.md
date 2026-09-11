# Synora speech to text

One route, what it charges, and when the charge lands.

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
| `stt_audio_too_large` / `stt_audio_too_long` / `stt_audio_empty` | 400 | Past a ceiling, or nothing at all | Ours, before any hold |
| `stt_unreachable` | 502 | Timeout, transport failure, redirect or upstream 5xx | Retry with backoff |
| `stt_unreadable` | 502 | A 2xx we could not parse as a transcript | Retry once, then report it |
| `stt_idempotency_spent` | 409 | This key has already been charged | Send a new key |
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
