# Synora internal API

The surface the TTS, STT, chat and voice-agent services call. Not the browser
API — different base path, different credential, different rules.

Base path: **`/internal/v1`**, e.g. `https://back.synora-ai.uz/internal/v1/health`.

Two things to read before writing any code: [Signing a request](#signing-a-request),
because that is where integrations lose their time, and
[What to do when we are down](#what-to-do-when-we-are-down), because the answer
is different for different endpoints and getting it wrong either drops revenue
or cuts off live calls.

---

## Credentials

You get one string:

```
svc_voice_agent_7f3a1c9e.KZ8xQm2_R4pL0sT8vN1yZ3bF6dG5aH2jK4mP7qR8s
└──────── key id ───────┘ └──────────────── secret ────────────────┘
```

The part before the dot is public and goes in a header. The part after it never
leaves your configuration and never goes over the wire. **We cannot recover it
for you** — there is nowhere it is stored. Lose it and we mint a new key and
revoke the old one, which is the right recovery path regardless.

Keys are scoped, and bound to one service. A voice-agent key cannot open a TTS
session, and a key scoped `usage:write` cannot authorize sessions. Ask for the
scopes you need:

| scope | lets you |
| --- | --- |
| `sessions:authorize` | claim a session ticket and start billable work |
| `sessions:report` | send heartbeats and finalize a session |
| `usage:write` | report usage |
| `health:read` | poll `/health` and use the signing helper |

**Every key expires**, ninety days by default. Rotation is: ask for a second
key, deploy it, tell us, we revoke the first. Both work at once, so there is no
flag day.

---

## Signing a request

Four headers on every request:

```http
X-Synora-Key-Id:     svc_voice_agent_7f3a1c9e
X-Synora-Timestamp:  1757250000
X-Synora-Nonce:      qX8s2Lm9Tz-0aB4cD7eF1g
X-Synora-Signature:  v1=Ck9J7hQm2xR4pL0sT8vN1yZ3bF6dG5aH2jK4mP7qR8s
X-Synora-Trace-Id:   trc_01J9ZQ8H3V2K            (optional, echoed in our logs)
```

The signature covers an eight-line **canonical string**, joined with `\n` and
with **no trailing newline**:

```
SYNORA-HMAC-V1
POST
/internal/v1/usage/events
seq=42
1757250000
qX8s2Lm9Tz-0aB4cD7eF1g
svc_voice_agent_7f3a1c9e
a3f1c9de7b2c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6
```

| line | content |
| --- | --- |
| 1 | the literal `SYNORA-HMAC-V1` |
| 2 | HTTP method, uppercase |
| 3 | the path, exactly as sent, including `/internal/v1` |
| 4 | canonical query — see below — or an **empty line** if there is no query |
| 5 | the `X-Synora-Timestamp` value, verbatim |
| 6 | the `X-Synora-Nonce` value, verbatim |
| 7 | the `X-Synora-Key-Id` value, verbatim |
| 8 | `sha256(raw request body)`, lowercase hex |

Then:

```
signature = "v1=" + base64url_without_padding(HMAC_SHA256(secret, canonical))
```

Each line is there for a reason, and skipping any of them opens something:

- **Lines 2–4** stop a captured request being replayed against a different
  endpoint. Without the path, one signed request authenticates everything.
- **Line 8** stops the body being edited in flight — which for a usage report
  means editing the bill.
- **Line 7** stops a signature being moved between keys.
- **Lines 5–6** bound replay to a five-minute window, which we then close
  entirely by remembering the nonce.

### The three details that cause every integration bug

**Hash the raw bytes.** Not re-serialised JSON. Take the exact byte string you
are about to put on the wire and hash that. Key order, unicode escaping and
float formatting all differ between your JSON library and ours, so two sides
that agree on the *meaning* of a payload will disagree on its bytes.

**Sort the query.** Parse the query string, sort by `(name, value)`,
percent-encode each part, join with `&`. Proxies and HTTP clients reorder
parameters; a signature that depends on your dict's ordering fails
intermittently, which is far worse to debug than failing always.

**Empty query means an empty line**, not a missing line. The canonical string
always has eight lines.

### Reference implementation — Python

```python
import base64, hashlib, hmac, secrets, time
from urllib.parse import parse_qsl, quote

def canonical(method, path, query, timestamp, nonce, key_id, body: bytes) -> str:
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    canonical_query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)
    return "\n".join([
        "SYNORA-HMAC-V1",
        method.upper(),
        path,
        canonical_query,
        str(timestamp),
        nonce,
        key_id,
        hashlib.sha256(body).hexdigest(),
    ])

def sign(key_id: str, secret: str, method: str, path: str,
         query: str = "", body: bytes = b"") -> dict[str, str]:
    timestamp = int(time.time())
    nonce = secrets.token_urlsafe(16)
    message = canonical(method, path, query, timestamp, nonce, key_id, body)
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return {
        "X-Synora-Key-Id": key_id,
        "X-Synora-Timestamp": str(timestamp),
        "X-Synora-Nonce": nonce,
        "X-Synora-Signature": "v1=" + base64.urlsafe_b64encode(digest).decode().rstrip("="),
    }
```

### Reference implementation — Node

```js
const crypto = require('node:crypto');

function canonical(method, path, query, timestamp, nonce, keyId, body) {
  const pairs = [...new URLSearchParams(query)].sort(
    (a, b) => a[0].localeCompare(b[0]) || a[1].localeCompare(b[1]),
  );
  const canonicalQuery = pairs
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join('&');
  return [
    'SYNORA-HMAC-V1',
    method.toUpperCase(),
    path,
    canonicalQuery,
    String(timestamp),
    nonce,
    keyId,
    crypto.createHash('sha256').update(body).digest('hex'),
  ].join('\n');
}

function sign(keyId, secret, method, path, query = '', body = Buffer.alloc(0)) {
  const timestamp = Math.floor(Date.now() / 1000);
  const nonce = crypto.randomBytes(16).toString('base64url');
  const message = canonical(method, path, query, timestamp, nonce, keyId, body);
  const signature = crypto.createHmac('sha256', secret).update(message).digest('base64url');
  return {
    'X-Synora-Key-Id': keyId,
    'X-Synora-Timestamp': String(timestamp),
    'X-Synora-Nonce': nonce,
    'X-Synora-Signature': `v1=${signature}`,
  };
}
```

Note `encodeURIComponent` and Python's `quote(safe='')` agree on the characters
that matter here. If you use a different language, check that space encodes as
`%20` and not `+`.

### When it does not match

Do not guess. On development and staging we expose the string we computed:

```bash
curl -X POST https://staging.example/internal/v1/debug/echo-signature \
  -H 'Content-Type: application/json' \
  -H "X-Synora-Key-Id: $KEY_ID" -H "X-Synora-Timestamp: $TS" \
  -H "X-Synora-Nonce: $NONCE" -H "X-Synora-Signature: $SIG" \
  -d '{"hello":"world"}'
```

```json
{
  "ok": true,
  "canonical": "SYNORA-HMAC-V1\nPOST\n/internal/v1/debug/echo-signature\n\n1757250000\n...",
  "body_sha256": "a3f1c9de7b2...",
  "key_id": "svc_voice_agent_7f3a1c9e",
  "scopes": ["health:read", "usage:write"],
  "signature_matched": true,
  "server_time": "2026-09-07T09:14:03.221Z"
}
```

Diff `canonical` against yours. The difference is the bug. This endpoint returns
**404 in production**, deliberately.

---

## Errors

Every failure uses the same body as the rest of the API:

```json
{ "detail": "human text", "statusMessage": "human text", "code": "signature_invalid" }
```

**Branch on `code`, never on the message text.** The messages change; the codes
do not.

| code | status | what it means | what to do |
| --- | --- | --- | --- |
| `signature_missing` | 401 | one of the four headers is absent | fix the client; do not retry |
| `service_key_unknown` | 401 | no such key id | fix the configuration |
| `service_key_revoked` | 401 | the key was revoked | switch to the new key |
| `service_key_expired` | 401 | past its expiry | rotate; this was scheduled |
| `signature_timestamp_skew` | 401 | your clock is more than 300s from ours | **run NTP.** The body carries `serverTime` |
| `signature_nonce_invalid` | 401 | nonce is not 16–128 URL-safe characters | fix the client |
| `signature_invalid` | 401 | the HMAC does not match | use `/debug/echo-signature` |
| `signature_replayed` | 401 | this exact nonce was already used | **use a fresh nonce on every retry** |
| `service_key_forbidden` | 403 | the key lacks the scope | ask for the scope |
| `not_found` | 404 | the internal API is switched off, or wrong path | check with us |

---

## Retrying

- **Retry on:** network errors, timeouts, `408`, `429`, and any `5xx`.
- **Never retry:** `400`, `401`, `403`, `404`, `413`, `422`. These are bugs, not
  weather; retrying turns one alert into a flood.
- **Backoff:** full-jitter exponential — base 500 ms, double each time, cap at
  10 s. Honour `Retry-After` when it is present.
- **Every retry needs a fresh `X-Synora-Nonce` and a fresh
  `X-Synora-Timestamp`.** Re-sending the identical headers is by definition a
  replay and will be refused. Keep the *idempotency key* in the body the same —
  that is what makes the retry safe.

---

## What to do when we are down

The answer differs by endpoint, and the difference is deliberate.

**Starting a session fails closed.** If you cannot reach us to authorize, refuse
the session. An unstarted session costs nothing; an unmetered one costs money
for as long as it runs.

**An established session fails open, with a bound.** Do not cut off a customer
who is mid-sentence because our box hiccuped. Keep going for up to
**120 seconds** and up to the overdraft allowance we gave you at authorize time,
whichever comes first. Then end the session with `end_reason:
"backend_unreachable"`, **write your final cumulative report to durable local
storage**, and replay it when we come back. We will accept it as an adjustment
for 24 hours.

**Buffer exactly one report per session.** Because reports are cumulative (see
below), an older one is strictly redundant — the newest report restates
everything. That keeps your retry buffer at constant size no matter how long we
are away, which is the main reason the wire format is what it is.

---

## Reporting usage

Two rules, and they are the whole contract.

### 1. Report cumulative totals, never deltas

Every report carries the running total for the session since it started. We
compute the difference. So the write on our side is
`stored = max(stored, incoming)`, which means:

- **A duplicate is harmless.** Sending the same report twice changes nothing.
- **Order does not matter.** An old report arriving late is ignored.
- **A lost report repairs itself.** The next one restates the total, so nothing
  is under-billed and you do not need exactly-once delivery.
- **Your buffer is one report per session**, forever, in constant memory.

With deltas, every one of those becomes your problem instead.

### 2. Never reset a session's counters

Not on an internal reconnect, not on a worker handover, not ever. If your
process restarts and loses the counters, that session is over — finalize it (or
let it time out) and start a new one. A counter that goes backwards is rejected.

### Units

Milliseconds for time, integers for everything. No floats anywhere in a billing
payload — a float is how `0.1 + 0.2` ends up on an invoice.

| metric | unit |
| --- | --- |
| `session_ms` | wall-clock milliseconds the session has been connected |
| `stt_audio_ms` | milliseconds of audio transcribed |
| `tts_characters` | characters synthesised |
| `tts_audio_ms` | milliseconds of audio produced |
| `llm_input_tokens` | prompt tokens |
| `llm_cached_input_tokens` | prompt tokens served from cache |
| `llm_output_tokens` | generated tokens |

Report only the metrics your service actually produces. An unknown metric name
is rejected with `422` rather than quietly stored — a stored-but-unpriced metric
is revenue nobody ever charges for, and a mistyped one is a dashboard that lies.

**Never send money.** No prices, no costs, no currency. We compute both what
the user pays and what it cost us from our own price book, which is versioned
and pinned per session. Sending an amount would make your service part of our
billing trust boundary, and neither of us wants that.

### The cap

Every session has a hard ceiling — the budget we quote at authorize time, plus
its overdraft. We clamp reported totals to it, do not bill the excess, and flag
the session for review. This is not a trap: it is the backstop that means a bug
in your service costs one session's worth of credit rather than a customer's
whole balance. Design your own limits to sit inside it.

---

## Endpoints

`GET /internal/v1/health` — scope `health:read`

```json
{
  "ok": true,
  "state": "ready",
  "price_book_version": 1,
  "redis": "up",
  "server_time": "2026-09-07T09:14:03.221Z"
}
```

Poll every 30 seconds. Alarm on two things: `state: "degraded"` (we are serving
gating counters from Postgres — usage reporting still works, expect more
latency), and `server_time` differing from your clock by more than two seconds,
which is a 401 waiting to happen.

`POST /internal/v1/debug/echo-signature` — scope `health:read`, development and
staging only. See [When it does not match](#when-it-does-not-match).

> **Session lifecycle and usage ingest — `authorize`, `usage/events`,
> `heartbeat`, `finalize` — are being built next.** The signing scheme, the
> credential model, the error codes, the retry rules and the cumulative wire
> format above are settled and will not change, so a client can be written
> against them now. `/health` and `/debug/echo-signature` are live, which is
> enough to prove an end-to-end signed request works before the rest lands.

---

## Getting set up

1. Tell us which service you are and which scopes you need. You get a key id
   and a secret.
2. Point a signed `GET /internal/v1/health` at staging. If it returns 200, your
   signing is correct and everything else is application logic.
3. If it does not, `POST /internal/v1/debug/echo-signature` and diff.
4. Check your clock. Really — run NTP.
