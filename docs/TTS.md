# Synora text to speech

The speech routes, what they charge, and when the charge lands.

This API is a **gateway**, not a wrapper. The GPU box upstream holds one
`sk_live_…` key, that key never leaves this process, and nobody outside gets a
token for it. A signed-in user calls us with their own JWT; we price their
text, hold the credit, stream the audio through and settle. That hop costs a
few milliseconds and buys the only thing that makes this a product rather than
a proxy: the count that produced the bill and the count we show you are the
same count, read from our own tables.

Two things to read before writing any client code:
[What a call costs, and when](#what-a-call-costs-and-when), because the
disconnect rule surprises people who expect to be billed for what they heard,
and [Failures land on one side of the first byte](#failures-land-on-one-side-of-the-first-byte),
because the same upstream problem is a `502` before it and a truncated `200`
after it.

Everything is under `/api/v1` and needs `Authorization: Bearer …`. The examples
assume:

```bash
API=http://127.0.0.1:8000/api/v1        # production: https://back.synora-ai.uz/api/v1
TOKEN=$(curl -s -X POST "$API/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"ali@example.com","password":"Str0ngPassw0rd"}' | jq -r .access_token)
```

---

## Which route do I want

| You want | Route | What it costs |
| --- | --- | --- |
| One utterance, audio starting now | `POST /tts/speech` | The whole text, charged when the stream ends |
| The price, without committing to it | `POST /tts/estimate` | Nothing. No session, no hold |
| A corpus — chapters, a catalogue, a hundred prompts | `POST /tts/batch` | Held at creation, charged at what was actually synthesised |
| To know which voices exist | `GET /tts/voices` | Nothing |
| A cloned voice from a clip | `POST /tts/voices` | Nothing |
| What this account has consumed | `GET /usage` | Nothing |
| The money side of the same events | `GET /wallet/transactions` | Nothing |

`/tts/speech` and `/tts/batch` are the only two routes that move credit. If a
deployment has no `TTS_BASE_URL` and `TTS_API_KEY`, every route here answers
`503 tts_not_configured` and nothing else in the API changes.

---

## What a call costs, and when

TTS is sold **by input character**. One metric, `tts_characters`, priced from
the versioned price book — the placeholder seed is 250 000 micro-credits per
1 000 characters, rounded **up** to the whole unit, so 1 001 characters is two
units and 53 characters is one.

```
price = ceil(len(text) / unit_size) * price_per_unit
```

Nothing else is priced. Audio duration is measured, reported and never
charged for: the price book has a `tts`/`tts_characters` row and no
`tts`/`tts_audio_ms` row at all, deliberately, because characters are the one
quantity a caller can count *before* spending anything.

### The streaming charge

```
POST /tts/speech
  │
  ├─ price len(text) ─────────────────► 402 if the wallet cannot cover it
  ├─ place hold = price               (nothing has been synthesised yet)
  ├─ open upstream stream ────────────► 502/503/400 here, hold released, charge 0
  │
  ├─ 200 + headers  ◄── the price is already on them
  ├─ audio … audio … audio
  │
  └─ stream ends (or the client hangs up)
       └─ release the hold, debit the price, close the session — one transaction
```

The hold goes on before a single byte of work is done, and the debit lands when
the stream ends, whatever ended it. Both movements appear in
`GET /wallet/transactions` against the `X-Synora-Session-Id` from the response
headers.

### Hanging up mid-stream is billed for the whole text

Stated plainly because it is the rule people trip over: **a client that
disconnects after the first byte pays for `len(text)`, not for what it
received.**

The reason is the same reason the price is on the response headers at all. The
entire input is in the request body before any work starts, so the price is
known before the request is even sent upstream — and a quote a client can see
before it commits is only worth having if it is also the amount it pays.
Billing per delivered character would mean the number on `X-Synora-Price` is a
guess, the hold is a guess, and no client can tell a user what a call will cost
until it is over. It would also be a fiction about the work: by the time any
audio is moving, every character has already been handed to the GPU, so hanging
up saves nothing on our side.

There is exactly one escape hatch, and it is the honest one: **if the speech
service fails before it hands us a single byte, nothing is charged.** The
session is abandoned, the hold goes back in full, and no usage event is written.
Our supplier's bad afternoon is not the caller's bill. Note what that hatch is
keyed on, though — upstream failing, not you going quiet.

`POST /tts/estimate` is how to find out the price without committing to it.

### A stream that stalls past its deadline is charged, and flagged

A client that opens the call, takes the headers and then stops pulling bytes is
not a client that went unserved: every character of `text` had already been
handed to the GPU before the `200` was written. Such a call holds credit and
delivers nothing, so once it has been silent past its ten-minute deadline
reconciliation ends it — and **it is charged for the text that was sent,
because the text was sent**, at the price `X-Synora-Price` had already quoted.

Settling one of these at zero is the alternative, and it is worse than it
sounds: a free synthesis with a printable recipe — open the stream, read
nothing, wait the deadline out — for exactly the work the price is computed
from.

The metered session is flagged `disputed` when this happens, the same marker an
`expired` batch job gets and for the same reason. **A charge made on a deadline
rather than on a delivery is a weaker thing than an ordinary settlement**, so
every one of them is left where support can find it rather than filed as
routine. Two things keep it off a slow but healthy client: the deadline is only
half the test — the reaper also asks when the session last made progress, and a
body still being pulled stamps that mark as it goes — and ten minutes bounds how
long a call may be *silent*, not how long it may take.

### The batch charge

The hold is placed at creation from the character counts in the payload — a
batch the wallet cannot cover is a `402` before any GPU time is spent, rather
than an hour into the work. What is finally charged is what the speech service
reports having synthesised, which is **lower** whenever items failed; nobody
pays for audio that was never produced.

It can never be higher. A settlement is clamped at the hold, and a clamp flags
the metered session `disputed` for review instead of quietly charging the
excess — an upstream that starts over-counting becomes a support question, not
an invoice.

| Job ends as | Charged |
| --- | --- |
| `succeeded` | The characters upstream reports |
| `failed` (items failed upstream) | The characters upstream reports for what it did produce |
| `failed` (upstream never accepted it) | Nothing. Hold released in full |
| `cancelled` | Whatever was reported up to the cancel |
| `expired` | The last usage upstream admitted to, and the session is flagged `disputed` |

---

## `POST /tts/speech`

```bash
curl -X POST "$API/tts/speech" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: chapter-01-take-3' \
  -D - -o chapter-01.mp3 \
  -d '{
        "text": "Salom! Bugungi ob-havo haqida qisqacha aytib beraman.",
        "voice_id": "vc_7f3a1c9e2b",
        "quality": "balanced",
        "audio_format": "mp3",
        "sample_rate": 48000
      }'
```

```http
HTTP/1.1 200 OK
content-type: audio/mpeg
x-synora-session-id: 6f1c9de7-2b4c-4d5e-8f70-8192a3b4c5d6
x-synora-characters: 53
x-synora-price-micros: 250000
x-synora-price: 0.250000
x-synora-sample-rate: 48000
x-audio-sample-rate: 48000
```

| Field | Default | Notes |
| --- | --- | --- |
| `text` | required | 1–`TTS_MAX_CHARACTERS` (5 000). Not trimmed — the charge is `len(text)` and a stripped string would bill a number you did not count |
| `voice_id` | service default | From `GET /tts/voices` |
| `quality` | `balanced` | `low_latency` reaches first audio soonest and sounds it; `high_fidelity` holds the connection open longer than a live player wants |
| `audio_format` | `mp3` | `pcm` \| `wav` \| `mp3` \| `opus` |
| `sample_rate` | `48000` | One of 8000, 16000, 22050, 24000, 32000, 44100, 48000 |
| `style` | none | A delivery hint, if the voice supports one |

`Content-Type` follows `audio_format`: `audio/mpeg`, `audio/wav`, `audio/ogg`
for Opus, and `application/octet-stream` for `pcm`. Raw PCM gets no audio media
type on purpose — nothing decodes headerless samples without being told the
rate, and claiming a type no player can open is worse than admitting they are
bytes. It is 16-bit mono at `X-Synora-Sample-Rate`.

### The response headers

| Header | Is |
| --- | --- |
| `X-Synora-Session-Id` | The metered session. The hold and the debit appear under it in `GET /wallet/transactions` |
| `X-Synora-Characters` | The quantity behind the price — `len(text)` |
| `X-Synora-Price-Micros` | The charge, in micro-credits, as an integer |
| `X-Synora-Price` | The same amount as a fixed-point string, for display |
| `X-Synora-Sample-Rate` | The rate we asked for |
| `x-audio-sample-rate` | The rate upstream says it synthesised at, relayed verbatim |

All six are named in `Access-Control-Expose-Headers`. **A browser cannot read a
response header that is not exposed, and it fails silently** — `undefined`
where the price should be, nothing in the console. If you add a header, add it
to `tts_service.EXPOSED_HEADERS` too; `main.py` reads the list from there.

### Failures land on one side of the first byte

Starlette sends the status line before it pulls the first chunk out of the
body, so where a failure happens decides what it can possibly look like.

**Before the first byte** — which is everything the service can refuse — you
get a real status code and no charge:

| What happened | Status | Code |
| --- | --- | --- |
| The wallet cannot cover the text | `402` | `insufficient_balance` |
| Too many syntheses already running for this account | `429` | `tts_too_many_concurrent` |
| The speech service refused the text, voice or format | `400` | `tts_rejected_input` |
| No such voice | `404` | `tts_not_found` |
| The speech service is saturated | `429` | `tts_busy` (with `Retry-After`) |
| The speech service is unreachable or timed out | `502` | `tts_unreachable` |
| Our key or our tenant quota is the problem | `503` | `tts_key_rejected` / `tts_quota_exhausted` |

**After the first byte** the `200` is already on the wire and there is no status
code left to choose. An upstream failure ends the stream short, is logged rather
than reported, and the call is still billed. Compare the bytes you received
against `X-Synora-Characters` if that distinction matters to you — it is the
only signal there is.

Note the two `503`s: upstream's own `401`, `403` and `402` are never relayed.
A rejected key is our configuration being wrong, and upstream's `402` is *our*
tenant quota, not the caller's wallet — relaying it would open the top-up
dialog in the Nuxt client and ask a user to pay for a shortfall on our account.

### Concurrency

`TTS_MAX_CONCURRENT_PER_USER` (default 3) syntheses per account, counted in a
rolling 60-second window in Redis. Over it, `429 tts_too_many_concurrent` with
`Retry-After: 60`. **With no `REDIS_URL` the cap is off entirely** — the null
cache counts nothing — which is the same trade every other Redis-gated feature
here makes.

### `Idempotency-Key`

Optional, scoped to your account and to this route. Read this section before
building a retry loop on it, because it is **narrower than the word usually
implies: it collapses a retry that races the original, not one that follows
it.**

While the first request under a key is still synthesising, a second one
carrying that key joins the same metered session — one hold, one charge, and
both callers are streamed audio. The audio is produced again rather than served
from storage: keeping megabytes of a customer's speech against the chance of a
retry costs disk, egress and a copy of their content we would then have to
secure and delete, and the retry rate does not come close to paying for it.

| You send | You get |
| --- | --- |
| The same request under the key, while the original is still streaming | `200` and audio. No second hold, no second charge |
| The same request under the key, once the original has ended | `409 tts_idempotency_spent` |
| Any other text, voice, quality, format, sample rate or style under it | `409 tts_idempotency_conflict`, in whatever state the original is |

"The same request" is exact and is checked as one: the session carries a
fingerprint of the text, voice, quality, format, sample rate and style it was
opened for, and a replay is compared against that rather than against its
price. Comparing prices was the earlier version and it was not enough — the
price book rounds to the thousand characters, so every text from 1 to 1 000
characters quotes the same amount and passed the check.

**A key is spent by the attempt, not by the charge.** There is nothing to hand
back for a call that already ended — the bytes went to a client, not to a
bucket — and re-running the GPU under a session somebody has already paid for
would be synthesis nobody is charged for. That is also true of a call that
failed: an upstream refusal releases the hold and charges nothing, but it
*closes the session*, so the key that opened it is spent as well. Retry with a
fresh key, and expect it to be charged if audio flows.

So the header is worth sending for the case it actually covers — a double
click, an impatient client-side timeout, a proxy that replays a request while
the first copy is still open — and is not a substitute for reading
`X-Synora-Characters` against the bytes you received. Branch on the two codes:
`tts_idempotency_spent` means "that work is done, stop retrying under this
key", and `tts_idempotency_conflict` means "you sent the wrong key".

At most **128 characters**, which is what the OpenAPI schema and this header
both publish and what `session_service.open_oneshot` enforces — one number, and
a longer key is `400 idempotency_key_too_long` naming it. `POST /tts/batch`'s
`idempotency_key` field carries the same ceiling and behaves differently in one
respect: a batch key never goes stale, because a job row survives its job. See
[Batch](#batch).

---

## `POST /tts/estimate`

Prices text without opening a session, placing a hold or moving a micro-credit.

```bash
curl -X POST "$API/tts/estimate" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Salom! Bugungi ob-havo haqida qisqacha aytib beraman."}'
```

```json
{
  "characters": 53,
  "price_micros": 250000,
  "price": "0.250000",
  "available_micros": 36633183,
  "available": "36.633183",
  "sufficient_credit": true,
  "shortfall_micros": 0,
  "shortfall": "0.000000",
  "price_book_version_id": "3f1c9de7-2b4c-4d5e-8f70-8192a3b4c5d6"
}
```

The quote comes from the same pricing call the charge does, against the same
active price book, so the two cannot disagree by construction. The one way to
be quoted one number and charged another is for a new price book to be
published between the two requests — which is why `price_book_version_id` is in
the response. Compare it against the session you were charged for.

`sufficient_credit` is false exactly when `/tts/speech` would answer `402` for
this text right now. Both it and `available_micros` are a snapshot: a
concurrent call that places a hold can turn a sufficient quote insufficient a
moment later.

The character ceiling here is the **batch** one (500 000), not the streaming
one. Pricing a corpus you have not committed to is most of what an estimate is
for.

---

## Voices

The list is **shared across the deployment, not per account**. This server holds
one credential with the speech service and everybody synthesises through it, so
a voice cloned by one account is visible to — and usable by — all of them, and
`DELETE` removes it for everyone. Treat `display_name` as public.

```bash
curl "$API/tts/voices" -H "Authorization: Bearer $TOKEN"
```

```json
{
  "voices": [
    {
      "voice_id": "vc_7f3a1c9e2b",
      "display_name": "Aziza",
      "supports_ultimate": true,
      "reference_seconds": 12.4,
      "has_speaker_embedding": true,
      "created_at": "2026-09-07T12:34:56Z"
    }
  ]
}
```

`has_speaker_embedding: false` means the clone has not finished processing;
synthesising against it before then comes out in the base voice at best.
Every field except `voice_id` is read defensively and may be absent — they
belong to the speech service's vocabulary, not to this API's promise, and a
field renamed upstream must not turn a voice picker into a 500.

### Cloning one

Free: no session, no hold, no charge. 3–30 seconds of a single speaker, sent
base64. A `data:audio/wav;base64,` prefix and MIME line breaks are both
accepted and stripped, and the clip is decoded here before anything goes
upstream, so an unparseable one is a `422` naming the field rather than
several megabytes pushed through the tunnel to be refused on the far side.

```bash
curl -X POST "$API/tts/voices" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"display_name\":\"Aziza\",
       \"audio_format\":\"wav\",
       \"transcript\":\"Assalomu alaykum, mening ismim Aziza.\",
       \"denoise\":false,
       \"audio_base64\":\"$(base64 -i sample.wav | tr -d '\n')\"}"
```

```json
{"voice_id":"vc_7f3a1c9e2b","display_name":"Aziza","supports_ultimate":true,
 "reference_seconds":12.4,"has_speaker_embedding":false,
 "created_at":"2026-09-09T22:10:03Z"}
```

Clean speech clones better than a long noisy sample. `denoise` is off by
default because it is destructive and makes a studio take worse. Supplying
`transcript` improves the clone; leaving it out makes the speech service
transcribe the clip itself. The base64 body is capped at 12 MB — thirty seconds
of 48 kHz 16-bit stereo WAV is 7.7 MB once base64 has added its third.

```bash
curl -X DELETE "$API/tts/voices/vc_7f3a1c9e2b" -H "Authorization: Bearer $TOKEN"
# → 200 {"ok":true,"message":"Voice removed."}
```

A batch job already accepted upstream keeps synthesising with a deleted voice;
a `/tts/speech` call naming it afterwards is refused.

---

## Batch

For a corpus: chapters, a catalogue, a hundred prompts. Up to 500 items and
500 000 characters in one job, each item at most 20 000 characters. Split
anything larger into several jobs — they queue behind each other either way.

```bash
curl -X POST "$API/tts/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "items": [
          {"id": "chapter-01", "text": "Birinchi bob. ..."},
          {"id": "chapter-02", "text": "Ikkinchi bob. ..."}
        ],
        "voice_id": "vc_7f3a1c9e2b",
        "quality": "high_fidelity",
        "audio_format": "wav",
        "sample_rate": 48000,
        "idempotency_key": "book-42-chapters"
      }'
```

```json
{
  "id": "9c2f8a71-5d3e-4b16-a0c7-1e2f3a4b5c6d",
  "ai_session_id": "6f1c9de7-2b4c-4d5e-8f70-8192a3b4c5d6",
  "upstream_job_id": "job_01J9ZQ8H3V2K",
  "state": "submitted",
  "is_terminal": false,
  "voice_id": "vc_7f3a1c9e2b",
  "audio_format": "wav",
  "quality": "high_fidelity",
  "sample_rate": 48000,
  "style": null,
  "total_items": 2,
  "completed_items": 0,
  "failed_items": 0,
  "submitted_characters": 128400,
  "billed_characters": 0,
  "audio_ms": 0,
  "estimated_micros": 32100000,
  "estimated": "32.100000",
  "reserved_micros": 32100000,
  "reserved": "32.100000",
  "settled_micros": 0,
  "settled": "0.000000",
  "error": null,
  "created_at": "2026-09-09T22:11:40Z",
  "submitted_at": "2026-09-09T22:11:40Z",
  "finished_at": null
}
```

`202`, because the audio does not exist yet. Defaults differ from the streaming
route on purpose: `high_fidelity` and `wav`, since nothing is waiting on the
first byte and these are files to keep rather than frames to play.

`idempotency_key` returns the job it already created instead of pricing and
holding for a second copy of the same corpus — even when the corpus differs,
since the answer is already priced, held and possibly half-synthesised. Omit it
and a retried POST is a second job with a second hold.

**Unlike the streaming `Idempotency-Key`, a batch key does not go stale.** It
keeps returning the same job after that job has settled, because a job row is a
durable answer where a finished stream is not. Use a fresh key for a new
corpus. Keys are scoped to the route as well as to the account, so the same
string here and on `/tts/speech` are two different requests rather than a
collision — which is what stops a one-character synthesis from settling a
half-million-character batch's session. The one refusal is
`409 tts_batch_idempotency_conflict`: the key names a metered session with no
job attached, which in practice means two requests raced on one key before the
first job row landed. Retry it.

### States

| `state` | Means | Terminal |
| --- | --- | --- |
| `queued` | Our row exists, priced and held for. Upstream has never seen it | no |
| `submitted` | Upstream accepted it; `upstream_job_id` is set | no |
| `running` | Upstream is synthesising | no |
| `succeeded` | Done. `billed_characters` is what was charged | yes |
| `failed` | Upstream refused it, lost it, or the items failed. `error` says which | yes |
| `cancelled` | You cancelled. Billed for what was produced first | yes |
| `expired` | Past `TTS_BATCH_MAX_POLL_SECONDS` (6 h). Settled at the last known usage, session flagged `disputed` | yes |

Poll until `is_terminal`. At that point the hold is gone, `settled_micros` is
what was charged, and nothing further changes. `billed_characters` below
`submitted_characters` means items failed and were not billed for.

The six-hour clock starts when the speech service accepted the job, and — for
one it never accepted — when the job was created and the credit was taken. A
job that upstream refused for a reason a retry could fix sits in `queued` with
its text and its hold intact, so `created_at` is the only start that reaches a
deadline at all; keyed off `submitted_at` alone, the job that most needs the
deadline would never reach one.

**Something has to look at a job for that deadline to fire.** Three things
can: the worker's next poll, a read of `GET /tts/batch/{job_id}`, and the batch
sweep inside `POST /admin/reconcile` — the only one of the three that happens on
nobody's behalf, and so the only thing that reaches a job whose submit was
dead-lettered or whose owner stopped polling. Nothing calls that route on a
timer yet (the README's *Still to do* says so), so on a deployment with no
worker a job nobody reads keeps its hold until somebody runs a reconcile. Poll
your jobs to the end, or read them once more after giving up on them.

### Reading a job is what moves it

```bash
curl "$API/tts/batch/9c2f8a71-5d3e-4b16-a0c7-1e2f3a4b5c6d" \
  -H "Authorization: Bearer $TOKEN"
```

This route asks the speech service for the job's counters, records them, and
settles the wallet if it has finished. On a deployment with no RabbitMQ worker
**it is the only thing that ever does** — see
[The no-broker fallback](#the-no-broker-fallback). A job upstream never
received is resubmitted here, which is how a queue message lost to a broker
restart recovers.

The deadline is checked before the resubmission, not after it. A job upstream
keeps refusing would otherwise answer every read with the same failing POST and
never reach the deadline that hands its hold back, however long it sat there
and however often its owner looked at it.

The upstream poll is rate-limited internally to one every
`TTS_BATCH_POLL_SECONDS` (10), so polling in a tight loop costs us nothing and
tells you nothing: until the next poll is due you get the row as it stands.

**A speech service that cannot be reached is not an error on this route.** If
the poll or the resubmission fails with a `502` or a `503`, that failure is
logged on our side and you get the job exactly as we last recorded it — the
same body a poll that was not yet due returns, and deliberately
indistinguishable from it. Everything you came for is ours rather than theirs:
`state`, the counters, `submitted_characters` and every amount are read from
our own database, so an outage that stops a job progressing must not also stop
its owner from looking at it. Keep polling; it resumes advancing when the
speech service does.

A `400` is the exception and still fails the read. It means the speech service
refused *this job's* payload rather than that it is having a bad afternoon, and
the job has already been failed and its hold released on the way past. `DELETE`
is the other exception, and for a different reason — see
[Cancelling](#cancelling).

`GET /tts/batch` lists your jobs, newest first, cursor-paginated exactly as
`GET /wallet/transactions` is:

```bash
curl "$API/tts/batch?limit=25" -H "Authorization: Bearer $TOKEN"
curl "$API/tts/batch?cursor=eyJ0IjoiMjAyNi0…" -H "Authorization: Bearer $TOKEN"
```

**The list route deliberately asks the speech service nothing.** A page of
twenty-five jobs would otherwise become twenty-five upstream calls on behalf of
someone who only wanted a list — so on a worker-less deployment the list can
show a job as `submitted` that has in fact already finished. Read the job
itself to advance it.

Someone else's job id is a `404`, the same answer an id that never existed
gets. "No such job" and "not yours" are the same sentence to anyone who should
not be able to tell the difference.

### Results

```bash
curl "$API/tts/batch/9c2f8a71-5d3e-4b16-a0c7-1e2f3a4b5c6d/results" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "job_id": "9c2f8a71-5d3e-4b16-a0c7-1e2f3a4b5c6d",
  "state": "succeeded",
  "results": [
    {"id":"chapter-01","ok":true,"path":"batches/job_01J9.../chapter-01.wav",
     "characters":64200,"audio_seconds":381.4,"similarity":0.92,"retries":0,"error":null},
    {"id":"chapter-02","ok":false,"path":null,
     "characters":0,"audio_seconds":null,"similarity":null,"retries":2,
     "error":"voice embedding unavailable"}
  ]
}
```

`state` is read from our own row rather than from the results payload, so
results and state cannot disagree about a job that settled between two reads.

Like `GET /tts/batch/{job_id}`, this refreshes the job first — which is what
advances it where there is no worker — and like it, a refresh the speech
service refuses is logged and skipped rather than failed. The results
themselves are the one thing on this page we keep no copy of, so an outage that
still lets you read the job's state can leave this route answering `502` for
its body. Read `GET /tts/batch/{job_id}` in the meantime.

`path` is **the speech service's own storage handle, not a URL this API
serves.** There is no download route here; quote the path in a support request.

**A job that ended having been billed nothing has no results here**, and answers
`409 tts_batch_results_unbilled`. A job cancelled before it was billed settles at
zero characters and its hold goes back in full, so whatever the speech service
happened to render before the cancel landed was never paid for — and results are
the product rather than metadata about it. Billed even partially, including a job
that failed after being charged for the items that did run, and the results are
yours. A live job upstream has not accepted yet comes back with an empty array
rather than a `404`.

### Cancelling

```bash
curl -X DELETE "$API/tts/batch/9c2f8a71-5d3e-4b16-a0c7-1e2f3a4b5c6d" \
  -H "Authorization: Bearer $TOKEN"
```

Not a refund: work already done is billed for, and the job settles at whatever
the speech service reports having produced by then. A job cancelled before it
was ever submitted charges nothing and its hold goes back in full.

**A cancel that cannot reach the speech service fails with `502` rather than
succeeding locally.** If we cannot stop the card we cannot stop the bill
either, and releasing the hold anyway would leave us synthesising audio nobody
can be charged for. Retry it. Cancelling an already-terminal job returns it
untouched, so a cancel racing the poller is safe.

### The no-broker fallback

Where a job goes after `POST /tts/batch` depends on the deployment. The billing
does not.

| | With `RABBITMQ_URL` | Without |
| --- | --- | --- |
| Handover to upstream | A worker picks the job off `tts.batch.submit` | Inline, on the request that created the job, before the response is written |
| Polling | A delayed message every `TTS_BATCH_POLL_SECONDS` | When somebody reads `GET /tts/batch/{job_id}` |
| Settlement | `tts_batch_service.settle_job` | The same function, called from the route |
| First response `state` | `queued` | `submitted` |

The route never bills anything itself — it calls the same two service functions
the worker calls. If it did not, a deployment would charge different amounts
depending on whether RabbitMQ happened to be running, and nobody would find
that bug from the invoice.

The inline path is a **supported configuration, not a degraded one**: upstream's
own `POST /v1/batch` answers in milliseconds because it only enqueues on its
side too, so the request that created the job can hand it over itself. A queue
that is merely absent must not become an outage. The test suite has no broker,
which is what keeps that claim honest.

What the broker buys, when it is there, is admission control in front of a
single GPU, plus polling that costs nobody a request.
[docs/QUEUEING.md](QUEUEING.md) is the whole argument, including where the queue
is deliberately *not* used.

One consequence worth planning for: **without a worker, a job nobody ever reads
again does not advance itself.** `POST /admin/reconcile` is the backstop, and
which half of it matters. Its session reaper is deliberately *not*: it skips any
metered session a non-terminal batch job still points at, because the two clocks
disagree, and a reaper acting on the session's deadline was closing the sessions
of perfectly healthy jobs and settling them at zero. The batch lifecycle owns
its own deadline instead, so the sweep that reads it lives in
`tts_batch_service` and the reconcile route calls it *alongside* the reaper
rather than through it — a billing module reaching into the AI services would
invert the layering the rest of this codebase holds to.

What is still missing is a schedule. Until something calls that route on a
timer, a queued job nobody reads is a hold waiting on an administrator rather
than on a clock. Poll your jobs, or run the worker.

---

## `GET /usage`

Your own consumption, grouped by service and metric, over a window. Summed from
the usage items the charges were priced from — the same rows
`GET /wallet/transactions` reports the money side of.

```bash
curl "$API/usage?start=2026-09-01T00:00:00Z&end=2026-10-01T00:00:00Z" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "period_start": "2026-09-01T00:00:00Z",
  "period_end": "2026-10-01T00:00:00Z",
  "lines": [
    {"service":"tts","metric":"tts_characters","quantity":128400,
     "events":37,"price_micros":32100000,"price":"32.100000"}
  ],
  "events": 37,
  "total_price_micros": 32100000,
  "total": "32.100000"
}
```

`start` is inclusive, `end` exclusive, and together they default to the last 30
days. Events are placed in the window by **when the work happened**, not by
when we wrote it down, so a report that reached us late still lands in the
period it belongs to.

This is not a relay. The speech service publishes a `GET /v1/usage` of its own
and it answers a different question — tenant-wide, what *we* have spent against
*it*, an operations number rather than a customer's. Where the two can differ is
a session clamped at its hold: this page shows what the line priced to, and the
statement is the authority on what the wallet actually paid.

---

## The `402`

Same shape as everywhere else in this API, plus three amounts:

```json
{
  "detail": "There is not enough credit to start this.",
  "statusMessage": "There is not enough credit to start this.",
  "code": "insufficient_balance",
  "requiredMicros": 250000,
  "availableMicros": 120000,
  "shortfallMicros": 130000
}
```

`shortfallMicros` is what to top up by, so the client can say how much instead
of making the user guess. **A `402` leaves no hold behind**: the metered session
is closed `failed` / `insufficient_credit` in the same transaction that
declines, so nothing is reserved against a call that never happened.

---

## Error codes

Branch on `code`, never on the message text. The messages change; the codes do
not.

| Code | Status | Means | Do |
| --- | --- | --- | --- |
| `tts_not_configured` | 503 | This deployment has no speech service wired up | Nothing client-side. Set `TTS_BASE_URL` and `TTS_API_KEY` |
| `tts_key_rejected` | 503 | Upstream refused **our** key | Nothing client-side. Logged at ERROR; somebody rotates a key |
| `tts_quota_exhausted` | 503 | **Our** tenant character quota is spent | Nothing client-side. Logged at ERROR |
| `tts_unreachable` | 502 | Timeout, transport failure, redirect or upstream 5xx | Retry with backoff |
| `tts_unreadable` | 502 | Upstream sent a 2xx we could not parse | Retry once, then report it |
| `tts_busy` | 429 | Upstream is saturated | Honour `Retry-After` |
| `tts_not_found` | 404 | No such voice, or no such upstream job | Refresh the voice list |
| `tts_rejected_input` | 400 | Upstream refused the payload; the message is upstream's own words about your text | Fix the input; do not retry |
| `tts_text_empty` | 400 | Nothing to synthesise | Fix the input |
| `tts_text_too_long` | 400 | Over `TTS_MAX_CHARACTERS` | Use `/tts/batch` |
| `tts_too_many_concurrent` | 429 | Over `TTS_MAX_CONCURRENT_PER_USER` starts for this account in the last 60 seconds | Wait out `Retry-After` |
| `tts_idempotency_spent` | 409 | The synthesis this `Idempotency-Key` opened has already ended — whether it was charged or abandoned | Retry with a **fresh** key, and expect to be charged |
| `tts_idempotency_conflict` | 409 | The key belongs to a different request: other text, voice, quality, format, sample rate or style | Use a fresh key for different input |
| `tts_batch_empty` | 400 | A batch with no items | Fix the input |
| `tts_batch_too_many_items` | 400 | Over `TTS_BATCH_MAX_ITEMS` | Split the job |
| `tts_batch_too_large` | 400 | Over `TTS_BATCH_MAX_CHARACTERS` in total | Split the job |
| `tts_batch_item_empty` | 400 | One item has no text; the message names it | Fix the input |
| `tts_batch_item_too_long` | 400 | One item is over 20 000 characters | Split that item |
| `tts_batch_duplicate_item_id` | 400 | Two items share an id, so results could not be attributed | Fix the ids |
| `tts_batch_not_found` | 404 | No such job — or not yours | Check the id |
| `tts_batch_results_unbilled` | 409 | The job ended having been billed for no synthesis at all, so its results are not served | Nothing to retry; submit the corpus again |
| `tts_batch_idempotency_conflict` | 409 | The key names a metered session with no job on it — two requests raced on one key before the first row landed | Retry |
| `tts_batch_conflict` | 409 | Two requests raced on one key and neither row survived | Retry |
| `tts_batch_upstream_id_conflict` | 502 | Upstream handed us a job id it had already given to another job | Retry; the job is settled and charged nothing |
| `idempotency_key_too_long` | 400 | Your key is over the 128 characters the schema and the header publish | Shorten it. What we store is your key behind a `{user_id}:{route}:` prefix, and the column has room for both |
| `insufficient_balance` | 402 | Not enough credit; body carries `shortfallMicros` | Top up |
| `wallet_frozen` | 403 | The wallet is on hold after a reversal or an admin action | Contact support |
| `price_book_missing` | 503 | Nothing is published, so nothing can be billed | `python3 devtools/seed_price_book.py` |
| `cursor_invalid` | 400 | Malformed `?cursor=` | Start the page walk again |
| `usage_window_invalid` | 400 | `start` is not before `end` | Swap them |

Schema violations — a `text` longer than `max_length`, an unknown
`audio_format`, a `sample_rate` that is not in the list — are FastAPI's own
`422` with the offending field named, and they happen **before** the wallet is
touched. That duplication with the codes above is deliberate: our rejection
costs a round trip, and upstream's rejection would cost a hold, a release and a
terminal session for text that was never going to be synthesised.

---

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `TTS_BASE_URL` | unset | Empty ⇒ every `/tts` route answers `503 tts_not_configured` |
| `TTS_API_KEY` | unset | The `sk_live_…` key. Never leaves this process. Setting one of the pair without the other is refused at boot outside development |
| `TTS_MODEL_KEY` | `synora-tts` | Which price book row synthesis bills against. Change it only together with a seeded price for the new key — an unpriced metric does not charge zero, it raises |
| `TTS_CONNECT_TIMEOUT_SECONDS` | `10` | Connecting is either quick or hopeless |
| `TTS_READ_TIMEOUT_SECONDS` | `300` | A long high-fidelity stream legitimately takes minutes |
| `TTS_MAX_CHARACTERS` | `5000` | Per streaming request |
| `TTS_MAX_CONCURRENT_PER_USER` | `3` | Rolling 60-second window. Needs Redis; without it the cap is off |
| `TTS_BATCH_MAX_ITEMS` | `500` | Ours, stricter than upstream's 5 000 |
| `TTS_BATCH_MAX_CHARACTERS` | `500000` | Per job |
| `TTS_BATCH_POLL_SECONDS` | `10` | How often a job may be polled upstream, worker or route |
| `TTS_BATCH_MAX_POLL_SECONDS` | `21600` | Six hours, then the job is settled `expired` and the hold released |
| `RABBITMQ_URL` | unset | Empty ⇒ batch runs inline. See [docs/QUEUEING.md](QUEUEING.md) |

Prices are not here. They live in the database, versioned, and are published
through the admin API — see
[Credits and the wallet](../README.md#credits-and-the-wallet).

---

## Checking the plumbing

```bash
# Is the speech service reachable, and are our credentials good? There is no
# probe route for it — the voice list is the cheapest call that proves both.
# 503 tts_key_rejected means our key, not yours.
curl -s "$API/tts/voices" -H "Authorization: Bearer $TOKEN" | jq '.voices | length'

# What did that call cost, and did the hold come back?
curl -s "$API/wallet" -H "Authorization: Bearer $TOKEN" | jq '{available, reserved}'
curl -s "$API/wallet/transactions?limit=5" -H "Authorization: Bearer $TOKEN"
```

A synthesis leaves three log lines on the server, in this order:

```
oneshot_open session=… user=… service=tts model=synora-tts price=250000 hold=250000
tts_stream session=… user=… chars=53 bytes=41280 audio_ms=0 reason=completed error=-
oneshot_settled session=… charge=250000 debited=250000 writeoff=0 clamped=False reason=completed
```

`oneshot_abandoned` instead of `oneshot_settled` means no audio arrived and
nothing was charged. `oneshot_clamped` means upstream reported more than the
hold covered, and that session is flagged for review.
