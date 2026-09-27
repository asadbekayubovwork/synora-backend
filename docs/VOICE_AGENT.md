# Synora voice agent

A live spoken conversation with the agent over WebRTC: what passes through this
API, what does not, and how a call we never hear is billed.

| You want | Route | What it costs |
| --- | --- | --- |
| To know whether calls are on, and what one costs | `GET /voice/config` | Nothing |
| To start a call | `POST /voice/sessions` | Holds the ceiling. Charges nothing yet |
| To give the agent a network path to the browser | `POST /voice/sessions/{id}/candidates` | Nothing |
| To keep the call up | `POST /voice/sessions/{id}/heartbeat` | Nothing itself. It is the meter |
| To hang up | `DELETE /voice/sessions/{id}` | The call: answer to hang-up, per started minute |
| What you called, and what it cost | `GET /voice/sessions[/{id}]` | Nothing |

This is the third gateway, and the first one the work does not pass through.
Speech and transcription stream through this process, so each of them watches
its work end and bills what it saw. A voice call is WebRTC: the browser and the
agent exchange audio directly over UDP, and all that reaches us is the
negotiation — one SDP offer, its answer, and a burst of ICE candidates — inside
the first second of a call that may last ten minutes.

We still sit in the middle of that negotiation, for the reason every gateway
here exists: **the agent is opened with one key, and that key never leaves this
server.** The agent's own integration guide says a key in frontend JavaScript is
public, and that anything past a prototype should proxy `POST` and
`PATCH /api/offer` through its own backend, adding the key server-side. This is
that backend. The browser sends its user's JWT; we attach the key, place a
hold, and hand the offer on byte for byte.

What that costs is visibility. Nothing on our side sees the audio or sees a call
end, so **the client's heartbeat is the evidence of how long a call lasted** —
and the one check on it is a question the agent will answer: *do you still hold
this peer connection?* A client that goes quiet is not taken at its word while
the agent says its call is up. Read [How a call is billed](#how-a-call-is-billed)
before writing client code: it is the section that changes what the client has
to do, and [the reference client](#the-reference-client-dev-uivoice-agent-clientjs)
already does it.

```bash
API=http://127.0.0.1:8000/api/v1        # production: https://back.synora-ai.uz/api/v1
TOKEN=$(curl -s -X POST "$API/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"ali@example.com","password":"Str0ngPassw0rd"}' | jq -r .access_token)
A="Authorization: Bearer $TOKEN"
```

---

## What goes where

```
 browser ─── the user's JWT ───► Synora API ─── our X-API-Key ───► voice agent
    ▲                                                                   ▲
    │  config, offer,            relays with the key,                   │
    │  candidates,               holds and bills, and                   │
    │  heartbeat, hang-up        asks whether a call is up              │
    │                                                                   │
    └═════ audio both ways + the "events" data channel, direct UDP ═════┘
```

Three consequences worth knowing before the first bug report:

- **The agent's CORS allow-list does not apply to the frontend.** The browser
  never makes an HTTP request to the agent — it calls us, and we call the agent
  server to server. What matters is this API's `CORS_ORIGINS`. The agent
  guide's "send your origin to the operator" step does not exist here.
- **The media needs a route to the agent that the signalling does not.** A
  tunnel, a proxy and this API all carry SDP; the audio still goes from the
  browser to the agent's host over UDP. A call that works on the office network
  and not from a phone on mobile data is missing TURN — see
  [TURN, for calls across networks](#turn-for-calls-across-networks).
- **The agent's `pc_id` never reaches the browser.** It is the supplier's
  handle for the peer connection, kept on our row to relay candidates against
  and to ask the agent about. Our `ai_session_id` names the call everywhere:
  the URL, the ledger, `GET /usage`.

---

## One call, start to finish

```
browser                                Synora API                         voice agent
   │                                        │                                   │
   │─ GET /voice/config ───────────────────►│                                   │
   │◄────────── ice_servers, cadence, hold ─│                                   │
   │ getUserMedia, new RTCPeerConnection    │                                   │
   │ addTransceiver(mic) + ("video")        │                                   │
   │ createDataChannel("events")            │                                   │
   │ setLocalDescription(createOffer())     │                                   │
   │                                        │                                   │
   │─ POST /voice/sessions {sdp} ──────────►│                                   │
   │                                        │ check offer, throttle, line cap   │
   │                                        │ HOLD the ceiling                  │
   │                                        │─ POST /api/offer + X-API-Key ────►│
   │                                        │◄──── {sdp, type:"answer", pc_id} ─│
   │                                        │ answered_at: the ceiling runs from here │
   │◄──────────── 201 {ai_session_id, sdp} ─│                                   │
   │ setRemoteDescription(answer)           │                                   │
   │                                        │                                   │
   │─ POST …/candidates [batch] ───────────►│                                   │
   │                                        │─ PATCH /api/offer {pc_id, …} ────►│
   │                                        │                                   │
   │◄═══════════════════ audio + data channel, direct UDP ═════════════════════►│
   │═ "ping" every second ═════════════════════════════════════════════════════►│
   │                                        │                                   │
   │ connectionState: "connected"           │                                   │
   │─ POST …/heartbeat  (first) ───────────►│                                   │
   │                                        │ connected_at: now it is billed    │
   │◄──────────────── {action: "continue"} ─│                                   │
   │   … every 15 s …                       │ last_seen_at, on our clock        │
   │◄─────── {action: "warn"}  last minute ─│                                   │
   │                                        │                                   │
   │ pc.close(), stop the mic tracks        │       (the agent ends its side)   │
   │─ DELETE /voice/sessions/{id} ─────────►│                                   │
   │                                        │ bill answer → now,                │
   │                                        │ release the rest of the hold      │
   │◄───────────── {status:"ended", price} ─│                                   │
```

**Ending a call needs no request to the agent**, and there is none to make: its
whole public surface is `GET /healthz` and `POST`/`PATCH /api/offer`. The
browser closes its peer connection and the agent tears its side down on its
own. The `DELETE` is for us — it ends the *billing*. The same `PATCH` that
relays candidates doubles as the one question this side can ask about a call
already running; see [Asking the agent](#asking-the-agent-whether-a-call-is-still-up).

### Two keepalives, and they are not the same thing

| | `ping` | heartbeat |
| --- | --- | --- |
| Sent to | the agent, on the data channel | this API, `POST …/heartbeat` |
| Every | 1 second | `heartbeat_interval_seconds` (15) |
| Miss it and | the agent drops the call after 3 seconds — it goes silent mid-sentence | after `heartbeat_timeout_seconds` (45) the agent is asked about the call, and unless it still holds it the call is over, billed to the last heartbeat plus one interval. The reference client hangs up on its own side then too |
| It protects | the agent's resources | the bill |

A client needs both. The heartbeat does not keep the agent's side alive, and the
ping tells us nothing — it never passes through this server.

---

## `GET /voice/config`

```bash
curl -s "$API/voice/config" -H "$A"
```

```json
{
  "ok": true,
  "available": true,
  "ice_servers": [{ "urls": ["stun:stun.l.google.com:19302"] }],
  "heartbeat_interval_seconds": 15,
  "heartbeat_timeout_seconds": 45,
  "max_session_seconds": 600,
  "max_concurrent_calls": 1,
  "per_minute_micros": 500000,
  "per_minute": "0.500000",
  "hold_micros": 5000000,
  "hold": "5.000000"
}
```

With `VOICE_AGENT_TURN_URLS` set, `ice_servers` also carries one TURN entry
minted for the signed-in user on this request — see
[TURN](#turn-for-calls-across-networks):

```json
"ice_servers": [
  { "urls": ["stun:stun.l.google.com:19302"] },
  {
    "urls": ["turn:turn.example.com:3478?transport=udp", "turns:turn.example.com:5349"],
    "username": "1790243643:6f1c1d3e-5b0a-4c47-9d0e-2a51b1f0c9aa",
    "credential": "q3Xh3Wm6c0n8R2m1YQy9s3k0g1E="
  }
]
```

Read it before drawing the call button, and again for each call:

- **`available: false`** — this deployment has no agent. Hide the button. The
  route answers `200` rather than `503` precisely so that a missing agent is a
  normal case for the client rather than an error path. Such a deployment hands
  out no TURN credential and no prices.
- **`ice_servers`** goes to `new RTCPeerConnection({iceServers})` unchanged. It
  is the deployment's STUN and TURN list, plus the minted entry when there is
  one. **That entry expires** — `VOICE_AGENT_TURN_TTL_SECONDS` after this
  request, by default the ceiling plus ten minutes — so a config read when the
  page loaded an hour ago is not the one to start a call with.
- **`hold`** is the least an account needs to start a call: the price of one
  that runs to `max_session_seconds`, reserved when it opens, the unspent part
  coming back when it ends. Compare `hold_micros` with `available_micros` from
  `GET /wallet` and offer a top-up *before* the user presses the button, rather
  than showing them a `402` after.
- **`per_minute` and `hold` are `null`** when nothing is priced for
  `voice_agent` — no active price book, or no `session_ms` row — and on a
  deployment with no agent. `POST` refuses such a deployment, so read `null` as
  "calls cannot be sold here".

Needs a signed-in user, like every route in this document.
`503 voice_agent_misconfigured` means `VOICE_AGENT_ICE_SERVERS` is not valid
JSON, or holds a `turn:`/`turns:` entry without both `username` and
`credential` — a laptop's `.env`, since production refuses to boot with either.

---

## `POST /voice/sessions`

The browser's offer in, the agent's answer out. The call exists from here.

```bash
# offer.sdp is what pc.localDescription.sdp held after setLocalDescription(await pc.createOffer())
jq -n --rawfile sdp offer.sdp '{sdp: $sdp, type: "offer"}' |
  curl -s -X POST "$API/voice/sessions" -H "$A" -H 'Content-Type: application/json' -d @-
```

`201 Created`:

```json
{
  "ok": true,
  "ai_session_id": "2f4c4503-408a-4a07-8b57-f97a193098ca",
  "sdp": "v=0\r\no=- 3960175203 3960175203 IN IP4 0.0.0.0\r\n…",
  "type": "answer",
  "answered_at": "2026-09-24T09:14:03.221410Z",
  "expires_at": "2026-09-24T09:24:03.221410Z",
  "heartbeat_interval_seconds": 15,
  "heartbeat_timeout_seconds": 45,
  "reserved_micros": 5000000,
  "reserved": "5.000000"
}
```

Hand `{type: "answer", sdp}` to `pc.setRemoteDescription` unchanged. Nothing on
either side of this API parses an SDP: a proxy that rewrote one would be a
second WebRTC implementation with none of the testing the browser's has had.
Keep `ai_session_id` — every later route takes it, and the hold, the release
and the debit carry it in `GET /wallet/transactions`. `answered_at` is when
the agent answered; the ceiling runs from it, and `expires_at` is that ceiling,
the latest the call can be billed to. Billable time itself starts at the first
heartbeat — see [How a call is billed](#how-a-call-is-billed). `type` in the request may be left out; it is always
`offer`, and anything else is a `422`.

### What is checked, in order

| Step | Refused with | Holds anything? |
| --- | --- | --- |
| The body: `sdp` 1–65 536 characters, `type` `offer` or absent | `422 validation_error` | No |
| This deployment has an agent | `503 voice_agent_not_configured` | No |
| The offer can work: an SDP (`v=0…`) with an audio **and** a video media line | `400 voice_offer_invalid`, `voice_offer_no_audio`, `voice_offer_no_video` | No |
| Attempts in this minute, per account — every request that got this far, refused ones included. Counted in Redis, off without it | `429 voice_call_rate_limited` | No |
| Calls started in the last 60 seconds, per account — counted in the database | `429 voice_call_rate_limited` | No |
| Your own dead calls are settled first — each quiet one put to the agent — so a crashed tab is not what refuses this one | — | — |
| Calls up right now, per account | `429 voice_call_limit` | No |
| Ended calls whose agent connection is still up, per account | `429 voice_call_still_connected` | No |
| The hold, for the ceiling | `402 insufficient_balance`, `403 wallet_frozen`, `503 price_book_missing` | No — a `402` closes its session in the same transaction that declines |
| Calls up, counted again now that this one is a row — two starts racing | `429 voice_call_limit` | Released whole, `user_cancelled` |
| The agent | its refusals — see [Errors](#errors) | Released whole |

Everything above the hold costs nothing: the most it sends the agent is a
probe of an old call, or a nudge to one that never connected, and neither
builds anything. Everything after it gives the hold back whole: a call the
agent never answered is not a call.

**The offer must negotiate video**, although the call is voice only —
`pc.addTransceiver("video", {direction: "sendrecv"})` before `createOffer()`.
The camera is never opened; only the channel is negotiated. Without it the
agent connects the call and no audio ever flows, with no error anywhere, so it
is refused here instead, with a message that names the fix. The data channel is
deliberately *not* required: the agent's guide calls a call without one
legitimate, just blind to transcripts and state.

**The answer takes seconds, not milliseconds**: the agent builds the call's
pipeline before it answers — six to eight seconds against the live agent when
it was warm — and **the first answer after the agent restarts can take thirty
seconds** while it warms up. Show a connecting state rather than a spinner that
looks frozen. We wait up to `VOICE_AGENT_OFFER_TIMEOUT_SECONDS` plus the connect
timeout — 45 seconds by default — before answering `502 voice_agent_unreachable`.
Right after a hang-up the `POST` can spend up to about five seconds more before
the offer goes out, making sure the old call has let go of its line (below).
The proxy in front of this API has to wait longer than all of that — see
[The proxy in front of this API](#the-proxy-in-front-of-this-api).

**Two throttles, one in the database and one in Redis.** The one that always
holds is counted from the call rows: `VOICE_AGENT_MAX_OPENS_PER_MINUTE` (10)
calls this account started in the last 60 seconds — every call that got as far
as a hold, the ones the agent refused included — and `retryAfter` is when the
oldest of them leaves the window. It exists for a client stuck in a reconnect
loop: an open is cheap here and a whole pipeline for the agent. But a refused
open writes no row, and every attempt may ask the agent about the account's
old calls on the key all our users share, so attempts are counted too:
`max(30, 3 × VOICE_AGENT_MAX_OPENS_PER_MINUTE)` a minute per account, in a
fixed window aligned to the clock minute, with `retryAfter` the end of that
window. That one is a brake rather than a wall — Redis-counted like every other
throttle here, and off without Redis. Both answer `429 voice_call_rate_limited`.

**There is no `Idempotency-Key` here, and a retry is a second call.** With the
default line cap of one it is refused `429 voice_call_limit` rather than billed
twice. But if the first response was lost — a proxy that gave up, a dropped
connection — the first call is up and answered and the client does not know its
id. It costs nothing: its browser never applied the answer, so it never
connected, and once the sweep finds it quiet and the agent has let go of the
peer — the live agent drops one that never received a candidate about a minute
after the offer — it is released at zero, within about two and a half minutes
at the default cadences. To free the line at once, find it and hang it up:

```bash
curl -s "$API/voice/sessions?limit=5" -H "$A" \
  | jq -r '.calls[] | select(.status == "live") | .ai_session_id'
curl -s -X DELETE "$API/voice/sessions/$ID" -H "$A"
```

The reference client never strands a call this way on its own account: an
abort mid-offer lets the `POST` finish and hangs the answered call up.

### One line per account

`VOICE_AGENT_MAX_CONCURRENT_PER_USER` (1) is how many of the shared agent's
connections one account may occupy, and a line is occupied for as long as
either side says so. The two cases have different codes, because the user has
to do different things about them:

- **A live call** — any call not yet settled, including one that never
  heartbeated and that the agent is still holding. Refused
  `429 voice_call_limit`, *"You already have 1 call in progress. End it before
  starting another."*, with `retryAfter` the heartbeat timeout (45): how long a
  crashed tab's call can take to be released. Say "hang the other call up, or
  wait".
- **An ended call the agent still holds.** `DELETE` settles a call; it does not
  close the browser's peer connection, and a client that hangs up and leaves
  the connection open is still talking to the agent. So the ten most recent
  ended calls not yet confirmed gone — answered within the last 24 hours — are
  put to the agent, concurrently, and each one it still holds counts. Refused
  `429 voice_call_still_connected`, *"Your previous call is still connected to
  the voice agent. Close it — the tab or the app it is running in — and try
  again."*, with `retryAfter` 5 (`HANGUP_SETTLE_SECONDS`). Nothing on the
  server can end that connection; say "close the other tab or app". When every
  call counted is one that never connected — failed attempts the agent has not
  dropped yet — the same code says so instead: *"Your last connection attempts
  are still being closed by the voice agent. Please try again in a moment."*,
  `retryAfter` 30 (`ATTEMPT_TEARDOWN_SECONDS`).

The second is ordinary for a moment after an honest hang-up: the agent drops a
call when its DTLS session closes — within about four seconds of the browser
closing it, measured against the live agent — or after three seconds of a
silent data channel, and neither need have happened before our `DELETE` lands.
So a call ended less than five seconds ago is asked again once a second, for up
to five seconds, before it counts — a Start pressed straight after Stop takes
that much longer rather than failing.

Four rules keep it from refusing honest users, or from turning their retries
into traffic on the agent:

- **The two most recent calls that never connected do not count until they
  prove they did.** "The agent still holds it" is not "it connected": a peer
  whose ICE is still failing is held too, and the live agent keeps one for a
  minute. So the two most recent ended calls that never heartbeated count only
  once they are [proven connected](#a-call-that-never-heartbeated-the-nudge) —
  held 90 seconds after the nudge and 120 after the answer. Two, because that is
  how many of an honest user's failed attempts the agent can still be holding
  when they press the button a third time (`UNPROVEN_EXEMPT`); one was measured
  refusing exactly that third press. Only two, because an exemption per call
  let an account stack free agent connections by opening and `DELETE`-ing
  without ever heartbeating — with two it holds at most its cap plus two.
- **A "still held" answer is reused for five seconds.** It is stamped on the
  ended row (`agent_seen_at`, which billing never reads again once a call is
  settled), so a client that retries inside `retryAfter` is refused from that
  answer rather than probing the agent again.
- **A call the agent says is gone** is stamped `gone_at` and never asked about
  again.
- **When the agent cannot be asked** — no trusted
  [probe](#asking-the-agent-whether-a-call-is-still-up), or a probe that
  fails — an ended call counts for nothing.

---

## `POST /voice/sessions/{id}/candidates`

Trickle ICE: the browser's network candidates, relayed to the agent against the
call's `pc_id`.

```bash
curl -s -X POST "$API/voice/sessions/$ID/candidates" -H "$A" \
  -H 'Content-Type: application/json' -d '{"candidates": [
    {"candidate": "candidate:1467250027 1 udp 2122260223 192.168.1.20 51233 typ host",
     "sdpMid": "0", "sdpMLineIndex": 0, "usernameFragment": "Xk2q"},
    {"candidate": "candidate:842163049 1 udp 1677729535 203.0.113.7 51234 typ srflx",
     "sdp_mid": "0", "sdp_mline_index": 0}
  ]}'
```

```json
{ "ok": true, "relayed": 2 }
```

- **`event.candidate.toJSON()` can be posted as it stands.** The browser's
  `sdpMid` and `sdpMLineIndex` are accepted beside the agent's `sdp_mid` and
  `sdp_mline_index`, and extra fields such as `usernameFragment` are ignored.
- **Queue the early ones.** The browser starts gathering at
  `setLocalDescription`, before `POST /voice/sessions` has answered, and those
  candidates have nowhere to go until there is an id. Post the queue in one
  request once there is.
- **Send only the bundled transport's.** Once the answer's `a=group:BUNDLE`
  puts every media line on one transport, ICE runs on the first `mid` in that
  group and candidates gathered for the others are never used — posting them
  spends the call's 64 on nothing. The reference client filters to that `mid`.
- **Batch the rest.** Every candidate since the last post, as one list. The
  agent takes a list, so a batch costs it one round trip where the agent
  guide's reference code costs one per candidate.
- **The end-of-candidates marker** — an empty `candidate` — is skipped. A batch
  of nothing else answers `relayed: 0`, or `409 voice_call_ended` if the call
  is already over.

| Limit | Value | Past it |
| --- | --- | --- |
| Candidates per request | 32 | `422 validation_error` |
| Candidates per call | 64 | A batch that would cross it is cut to what fits, and `relayed` says how many went. Once none fit, `400 voice_candidates_exhausted` |
| One candidate | 1 024 characters, one line | `422 validation_error` |

Cut rather than refused, because the candidates a browser gathers last are its
TURN relays, and on a network where only a relay works they are the ones that
matter. A candidate counts against the 64 when it is sent, whether or not the
agent took it.

The per-call cap exists because every relay is an outbound request on the one
key all of our users share: a client looping on `onicecandidate` should run
into a refusal of its own rather than into the agent's rate limit for everybody.
A browser gathers a handful per network interface, so 64 is far above any
honest call. The one-line rule is not pedantry — a line break inside a candidate
is the way to smuggle a second attribute into the agent's SDP parser.

`409 voice_call_gone` means the agent has already torn this call down — the
browser closed it, or ICE never completed. It is the one authoritative end the
agent ever volunteers, so the call is **ended, settled and marked gone here
too** before the error is answered, and its line is free at once. Stop sending,
and close the peer connection.

`400 voice_candidate_rejected` is the agent refusing a candidate, in its own
words. Not worth retrying unchanged, and not fatal either: ICE has others.
`409 voice_call_busy` means two relays for the same call collided three times
over the budget; retry.

---

## `POST /voice/sessions/{id}/heartbeat`

```bash
curl -s -X POST "$API/voice/sessions/$ID/heartbeat" -H "$A"
```

```json
{ "ok": true, "action": "continue", "elapsed_ms": 73000, "remaining_ms": 527000, "next_heartbeat_seconds": 15 }
```

**Start when `pc.connectionState` reaches `connected`, not when the answer
arrives**, and send one every `next_heartbeat_seconds` until the hang-up. The
first one stamps `connected_at`, which is what turns a call from "released
free" into "billed" — so send it the moment the media is up, and not before.

| `action` | Means | Do |
| --- | --- | --- |
| `continue` | Carry on | The next one in `next_heartbeat_seconds` |
| `warn` | Inside the last minute before `max_session_seconds` (`remaining_ms` ≤ 60 000) | Tell the user the call is about to end. Keep heartbeating |
| `stop` | The call has been **ended and billed**. `elapsed_ms` is what was billed | Close the peer connection and stop the microphone. A `DELETE` is harmless and unnecessary |

`elapsed_ms` runs from `answered_at` on our clock, so it counts down to the
ceiling; the bill runs from the first heartbeat. A heartbeat carries no time
of its own: it moves `last_seen_at` to *our* now. A client can stop reporting;
it cannot report a shorter call than the one it had.

The ordinary heartbeat is one conditional `UPDATE`: the caller's call, live,
answered, inside its ceiling, and vouched for — by a heartbeat or by the agent
— within the timeout. Anything else takes the slow path, and this is what that
path does:

- **The ceiling.** The first heartbeat past `answered_at + max_session_seconds`
  settles the call at exactly the ceiling and answers `stop`, with
  `end_reason: max_duration`.
- **A heartbeat after a silence longer than the timeout.** A laptop that slept,
  a tab frozen in the background, a network that reaches the agent and not us:
  nobody has vouched for the call for more than `heartbeat_timeout_seconds`,
  so before answering, this heartbeat puts it to the agent. **Still held**: the
  client was cut off from us, not from the call, so it is revived —
  `continue`, billed from its first heartbeat as if the gap had not happened, and
  flagged `disputed` when it is settled. **Gone, or the agent cannot be
  asked**: the call was already over. It is billed to its last proof of life
  plus one interval, flagged `disputed`, and answered `stop`.
- **A first heartbeat more than `heartbeat_timeout_seconds` after the
  answer.** The same question. Held: the media did connect, late — `continue`,
  billed from that first heartbeat. Not held: treated as never having
  connected, released at zero and answered `stop`. The reference client gives
  up on ICE after forty-five seconds; the live agent drops a peer that never
  connected about a minute after its offer.
- **An ended call.** A heartbeat on a call that is already over answers `stop`
  with its bill, not an error.

One missed heartbeat is not a problem — the timeout is three intervals, so that
a phone switching from wifi to mobile can drop a request or two without the
call being ended. Retry a failed one after a couple of seconds rather than a
whole interval later, give each request a short timeout of its own so one
stalled socket cannot eat the window, and do not retry in a tight loop. That is
what the reference client does — every 2 seconds, 5 seconds a request — and it
hangs the call up on its own side (`heartbeat_lost`) once none has got through
for `heartbeat_timeout_seconds`, because past that the server may have ended
it.

**Every heartbeat carries the user's access token, and it has to stay valid for
the whole call.** An expired token is a `401 token_expired`, not a `stop`: the
call cannot be told it is over by a request we could not authenticate. To the
client it is a heartbeat that failed, and within `heartbeat_timeout_seconds` of
the last one that got through, the call ends `heartbeat_lost`, mid-sentence. See
[`getToken`](#gettoken-must-stay-fresh-for-the-whole-call).

`409 voice_call_not_answered` — a heartbeat on a call whose offer is still in
flight — cannot happen to a client that waits for the `201`.

---

## `DELETE /voice/sessions/{id}`

Hang up: the call is billed and the rest of its hold released.

```bash
curl -s -X DELETE "$API/voice/sessions/$ID" -H "$A"
```

```json
{
  "ok": true,
  "ai_session_id": "2f4c4503-408a-4a07-8b57-f97a193098ca",
  "status": "ended",
  "created_at": "2026-09-24T09:14:01.821410Z",
  "answered_at": "2026-09-24T09:14:03.221410Z",
  "connected_at": "2026-09-24T09:14:03.721410Z",
  "ended_at": "2026-09-24T09:15:16.240112Z",
  "end_reason": "client_hangup",
  "billed_ms": 73019,
  "price_micros": 1000000,
  "price": "1.000000",
  "reserved_micros": 0,
  "heartbeats": 5,
  "disputed": false
}
```

- **Send it from the Stop button *and* from `pagehide`, both with
  `keepalive`** — `fetch(url, {method: "DELETE", keepalive: true, headers})`
  survives the page closing, and a Stop followed at once by a navigation. A
  closed tab is then billed to the second it closed rather than to a heartbeat
  timeout, and its line is free for the reload once the agent has let go of the
  old connection.
- **Idempotent.** Ending an ended call answers with the first ending's bill,
  because Stop and `pagehide` routinely both fire for the same call.
- **It ends the billing, not the media.** Closing the peer connection is still
  the client's job; the agent tears its side down when the browser's
  connection closes, and nothing here can do that for it. Close first, then
  `DELETE`. A client that hangs up and leaves the connection open is billed to
  the `DELETE` — and its account cannot start another call while the agent
  holds the old one ([One line per account](#one-line-per-account)).
- A call that never connected ends with `price` `0.000000` and
  `connected_at: null`.

---

## `GET /voice/sessions` and `GET /voice/sessions/{id}`

The record `DELETE` returns, for every call this account has placed, newest
first, cursor-paginated exactly as `GET /stt/transcriptions` is.

```bash
curl -s "$API/voice/sessions?limit=25" -H "$A"
curl -s "$API/voice/sessions/$ID" -H "$A"
```

```json
{
  "ok": true,
  "calls": [
    {
      "ok": true,
      "ai_session_id": "2f4c4503-408a-4a07-8b57-f97a193098ca",
      "status": "ended",
      "created_at": "2026-09-24T09:14:01.821410Z",
      "answered_at": "2026-09-24T09:14:03.221410Z",
      "connected_at": "2026-09-24T09:14:03.721410Z",
      "ended_at": "2026-09-24T09:15:16.240112Z",
      "end_reason": "client_hangup",
      "billed_ms": 73019,
      "price_micros": 1000000,
      "price": "1.000000",
      "reserved_micros": 0,
      "heartbeats": 5,
      "disputed": false
    }
  ],
  "page": { "next_cursor": null, "has_more": false, "limit": 25 }
}
```

`limit` is 1–100 and defaults to 25; `page.next_cursor` goes back as `?cursor=`.
Somebody else's id is a `404 voice_call_not_found`, never a `403` — a 403 would
confirm the id exists.

While a call is live, `status` is `live`, `billed_ms` and `price` are zero and
`reserved_micros` is the hold. Once it ends, `reserved_micros` is zero and the
other two are the bill. `disputed` means the bill rests on an inference rather
than on a hang-up that arrived in time — the heartbeats stopped and the end was
estimated, or the agent had to vouch for the call because its client went
quiet — and is the first thing to look at when a charge is questioned.

**Every call that got as far as a hold is listed**, including the ones that
never became a conversation — an offer the agent refused, a call that never
connected, a start that lost a race for the line — at price zero. A request
refused before the hold (a `400`, a `429` from the throttle or the line cap, a
`402`) left no row, and is not.

| `end_reason` | What ended it | Billed |
| --- | --- | --- |
| `client_hangup` | `DELETE` | Answer to hang-up — zero if it never connected |
| `max_duration` | The ceiling | Exactly `max_session_seconds` — also for a call the agent kept running after its client went quiet |
| `heartbeat_timeout` | Nobody vouched for the call within the timeout, and the agent did not say it was up | To the last proof of life — a heartbeat, or the agent's last confirmation — plus one interval, `disputed`. Zero if it never connected |
| `upstream_error` | The agent refused the offer (`answered_at: null`), or said the call was gone | Zero if refused or never connected; answer to now if it was connected |
| `timeout` | Our own process died with the offer in flight | Zero |
| `user_cancelled` | Two starts raced for one line, and this one lost | Zero |
| `client_disconnected`, `internal_error` | We were shut down mid-offer, or could not record the call | Zero |

`end_reason` is what the billing concluded, not which route happened to run: a
`DELETE` that arrives after the heartbeats had already lapsed — and nothing
vouched for the call meanwhile — is billed and recorded as `heartbeat_timeout`,
and one past the ceiling as `max_duration`.

---

## How a call is billed

One metric, `session_ms`, under the service `voice_agent` and the model key
`VOICE_AGENT_MODEL_KEY`, priced from the versioned price book like everything
else here. With the seeded row's rounding — up, to the started minute:

```
price = max(ceil(billed_ms / 60000) * per_minute, min_charge)
```

### The price of a minute has to be the whole price

The agent runs recognition, a language model and synthesis for every call and
tells us about none of them: its public surface has no usage report. So
`session_ms` is the only quantity this gateway ever reports, and the
`session_ms` row it bills against **has to carry the whole price of a minute of
conversation** — not a connection fee on top of components, because the
components are never charged.

The placeholder seed does not do that yet. Its `voice_agent` rows were written
for the agent-reported model of [INTERNAL_API.md](INTERNAL_API.md) — a
0.5-credit-a-minute connection fee *plus* transcription, tokens and synthesis —
and this gateway reads only the first of them, so a seeded voice call costs 0.5
credits a minute: less than transcription alone. Before anyone is charged,
publish a `session_ms` row for `voice_agent` under the exact model key
`VOICE_AGENT_MODEL_KEY` names, priced as the whole minute. An exact row beats
the `*` wildcard, so the wildcard rows stay where they are for the
agent-reported sessions to bill under once those exist.

The examples below use the seed anyway — 0.5 a minute, rounded up, a 0.25
minimum — because the arithmetic is the point.

### The rules

```
      POST              agent answers        first heartbeat                 DELETE
        ●─────────────────────●─────────────────────●───── … every 15 s … ──────●
   hold placed           answered_at          connected_at                  ended_at
                              └─────────────────── billed_ms ───────────────────┘
```

And when the heartbeats stop before a hang-up does:

```
  last heartbeat       timeout passes    the agent is asked
        ●─────── 45 s ────────●───────────────────●
  last_seen_at                                    ├─ held → kept: agent_seen_at = now,
                                                  │         billing goes on
                                                  └─ gone, or no answer → billed to
                                                     the last proof of life + 15 s,
                                                     disputed
```

The agent is asked by the sweep, or by the next heartbeat if that comes first.

1. **The hold is the ceiling.** When the call opens, credit is reserved for one
   that runs to `VOICE_AGENT_MAX_SESSION_SECONDS` — 5 credits at the seed — and
   everything unused comes back when it ends. `session_service` has no hold
   extension, so this is the same bounded one-shot the live transcription is,
   with the same honest cost: **an account with less than the hold cannot start
   a call at all**, not even a short one. `GET /voice/config` says how much, so
   the client can say so before the button rather than after it.
2. **Billable time starts at the first heartbeat** (`connected_at`), which a
   client sends the moment its media is up — on our clock, not the client's.
   Not at the answer: ICE through two NATs was measured taking over thirty
   seconds after the answer on a real network, and nobody should pay for that
   silence. A call that never heartbeated but was taken as connected on the
   agent's word is billed from its answer instead, so a client cannot shave its
   bill by holding back that first heartbeat. The ceiling still runs from the
   answer, which is what the hold was priced from.
3. **The first heartbeat is what makes a call billable — or the agent, once
   the call has outlived a nudge.** A call that never sends one and that the
   agent lets go of never connected — ICE failed, most often for want of a TURN
   server, or the tab went away first — and nothing reached the user, so the
   hold goes back whole and the price is zero. A call that never sends one and
   that the agent *still* holds is nudged, and taken as connected from its
   answer only once it is still held 90 seconds after the nudge and 120 after
   the answer; from then it is billed like any other, `disputed`. See
   [A call that never heartbeated](#a-call-that-never-heartbeated-the-nudge).
4. **A hang-up bills answer to now.**
5. **A call nobody vouches for is put to the agent before it is ended.** No
   heartbeat and no confirmation for `heartbeat_timeout_seconds`, and the agent
   is asked whether it still holds the peer connection. **Held**: the call is
   *kept* — `agent_seen_at` becomes its proof of life, and billing carries on
   until the client hangs up, the agent lets go or the ceiling arrives.
   **Gone, or the agent cannot be asked**: it is billed to its last proof of
   life — the later of its last heartbeat and the agent's last confirmation —
   plus one interval. One interval is the latest the call could have ended
   without its client noticing it owed a report.
6. **An inferred bill is flagged `disputed`**: every call billed on that
   last-proof estimate, and every call the agent had to vouch for. Because the
   price is a function of the row's timestamps rather than of when the
   settlement ran, a crashed tab costs the same whether the sweeper found it in
   thirty seconds or the user's next call found it a week later.
7. **Nothing is billed past the ceiling**, whatever the client did after being
   told `stop`.
8. **A connected call is never billed zero.** At least one millisecond is
   reported, so the minimum applies to the one kind of call a minimum exists
   for.

| The call | `billed_ms` | `price` at the seed |
| --- | --- | --- |
| Hung up after 20 seconds | 20 000 | 0.500000 |
| Hung up after 73 seconds | 73 000 | 1.000000 — two started minutes |
| ICE failed; never connected | 0 | 0.000000 |
| Tab crashed; last heartbeat 3:05 after the first one | 200 000 — 3:05 plus 0:15 | 2.000000, `disputed` |
| ICE took 0:33 through two NATs, then 0:25 of conversation | 25 000 — from the first heartbeat | 0.500000 |
| Heartbeats stopped at 1:00 and the talking went on; the agent last confirmed the call at 3:50 | 245 000 — 3:50 plus 0:15 | 2.500000, `disputed` |
| Never heartbeated; the agent held it past both graces, last confirmed it at 4:40 and then let go | 295 000 — 4:40 plus 0:15, from the answer | 2.500000, `heartbeat_timeout`, `disputed` |
| Never heartbeated, and closed at 1:30 — before it could prove it connected | 0 | 0.000000 |
| Ran to the ten-minute ceiling | 600 000 | 5.000000, `max_duration` |

Measured against the live agent: ordinary calls of 16 to 40 seconds billed
0.500000 — one started minute at the seed — and a tab killed mid-call billed to
its last heartbeat plus 15 seconds, `disputed`.

The seed's 0.25 minimum never shows: rounded up to the started minute, the
smallest charge is already a whole minute's 0.5. A minimum below a minute's
price only means something on a row priced finer than per minute.

A call the agent keeps all the way to the ceiling is billed the ceiling,
`max_duration`, `disputed`. A quiet call past its ceiling is still put to the
agent before it is settled: held, and the fresh confirmation bills it *to* the
ceiling; gone, and it is billed to its last confirmation plus one interval,
never past the ceiling.

### A call that never heartbeated: the nudge

"The agent still holds this peer connection" is not "this call connected". A
browser that closes before relaying a single ICE candidate leaves the agent a
peer in ICE checking with nothing to check, and aioice, which the agent's
WebRTC stack runs on, never finishes such a check on its own. The live agent
drops a peer like that on a timeout of its own, about 60 seconds after the
offer (measured, not promised); an aiortc agent without that timeout keeps it
forever. Billing a held-but-silent call from its answer, as a connected call
that stopped reporting deserves, needs a way to tell the two apart. That is
the nudge:

- **What it is.** One unroutable candidate, `voice_agent_client.NUDGE_CANDIDATE`
  — `candidate:1 1 udp 1 192.0.2.1 9 typ host`, `sdp_mid` `"0"`,
  `sdp_mline_index` 0: TEST-NET-1 (RFC 5737) and the discard port, which can
  reach nobody — relayed to the call's `pc_id` through the same
  `PATCH /api/offer` the browser's candidates go through, with a 5-second
  budget. It never raises.
- **What it does.** A peer stuck with no candidate pairs gets one to fail, and
  aioice fails an unanswered pair about 64 seconds after it is relayed (seven
  STUN transmissions from half a second, doubling), which fails ICE and drops
  the peer. A peer that did connect has finished its checks and ignores it.
- **When it is sent.** The first time the sweep finds a never-heartbeated call
  quiet and still held (it is kept, not billed yet), and when a call that never
  connected is settled — a `DELETE` after a failed connect, say — so the agent
  lets go of a peer stuck in checking rather than holding a slot for it. Not to
  a call the agent already said is gone, and never again once one has been
  accepted.
- **`nudged_at` is stamped only when the agent accepted it** (a `2xx`). A nudge
  that timed out or was refused never starts the grace that trusts it; the next
  time the sweep keeps the call, it is sent again.
- **When a never-heartbeated call counts as connected** (`proven_connected`):
  still held at least `NUDGE_GRACE_SECONDS` (90) after the nudge **and**
  `NEVER_CONNECTED_GRACE_SECONDS` (120) after the answer. Both, not either:
  a peer that failed ICE is gone by then on every agent this has been run
  against — dropped by the agent's own 60-second timeout, or failed on the
  nudge's pair at about 64. It is then stamped `connected_at = answered_at` and
  billed from its answer. An earlier rule, sixty seconds after the answer
  alone, sat on the very second the live agent drops such a peer, and would
  have billed some ICE failures in full.

At the default cadences — quiet after 45 seconds, a sweep every 30, a kept call
asked again once its confirmation is 45 seconds old — the nudge goes out 45 to
75 seconds after the answer, and the call is proven on the third time it is
asked: 135 to 225 seconds after the answer. The graces are fixed in code and do
not shrink with shorter cadences.

### Who settles a call

Whoever notices first, and it does not matter which:

| Settler | When |
| --- | --- |
| `DELETE` | The client hung up |
| The heartbeat | It found the call past its ceiling, or quiet past the timeout and not vouched for by the agent |
| A candidate relay | The agent answered that it no longer knows the call — `409 voice_call_gone` |
| The user's next `POST /voice/sessions` | It sweeps that user's dead calls before counting the line |
| The in-process sweeper | Every `VOICE_AGENT_SWEEP_SECONDS`, in every worker, with or without an agent configured |
| `POST /admin/reconcile` | On demand, reported as `voice_ended` |

The last three are one pass. It picks live calls nobody has vouched for in the
timeout, calls past their ceiling, and offers still unanswered after twice
their deadline — 90 seconds by default: our own process died mid-offer. Each is
re-read before it is decided. The unanswered ones are settled at once. Every
quiet one — past its ceiling included — is put to the agent first, and read
again after the probe, because the probe took seconds and a heartbeat in
another worker may have revived it meanwhile. A call the agent still holds is
*kept*, not settled, and not counted in `voice_ended` — unless it is past its
ceiling, where the fresh confirmation bills it to the ceiling. The same pass
releases, at zero, a hold whose call row was never written — a process that
died between the two (`voice_call_orphaned`). The heartbeat's slow path does
the same: it records a `gone` answer and reads the row again before it
settles, so it never bills a call a sweep in another worker had just kept.

`session_service` makes a second settlement of the same session a replay of the
first, so the wallet moves once however many of them race — two releases of a
call that never connected included: the loser reads the winner's session row
afresh and leaves its end reason alone rather than stamping its own over it.
The generic session
reaper deliberately stands off every voice call — any session with a
`voice_calls` row, and a voice session whose row was never written, recognised
by the idempotency key only this gateway mints for its own sessions: its rule,
a session past its deadline is billed at its estimate, would charge a crashed
tab the whole ceiling.

What a call leaves behind: a hold, a release and a debit under its
`ai_session_id` in `GET /wallet/transactions`, and `voice_agent` /
`session_ms` in `GET /usage`. **`reserved` back at zero after the call is the
assertion worth making every time**, exactly as it is for speech.

### Asking the agent whether a call is still up

The agent has no route that says a call ended, but it has one that says a call
has not. `PATCH /api/offer` with an **empty** candidate list does nothing to a
peer connection the agent holds, and is a `404` for one it has dropped. So a
probe is exactly that request:

| The agent answers | The probe reads it as |
| --- | --- |
| `2xx` | `alive` — the agent holds the call |
| `404` | `gone` — the agent has torn it down |
| Anything else, no answer within 5 seconds, or no agent configured | `unknown` |

Against the live agent, an empty `PATCH` on a call it holds answers
`200 {"status": "success"}`, and one on a `pc_id` it does not hold
`404 {"detail": "Peer connection not found"}`; a call the browser closed is a
`404` within about four seconds.

That is a reading of the agent's behaviour, not a promise in its guide, so it is
checked before it is believed. **The canary**: before any probe is acted on,
one is sent for a `pc_id` no call has ever had (`synora-liveness-canary-…`).
Only an agent that answers *that* with a `404` has told us its `2xx` means
something, and only such an agent is trusted — one that said `200` to
everything would keep every quiet call alive to its ceiling and bill it ten
minutes. The verdict is cached per process, 600 seconds when trusted and 60
when not (unreachable counts as not), and logged once per change of mind:

```
voice agent liveness probes trusted
voice agent liveness probes not trusted (canary answered None); billing on heartbeats alone
```

**When the canary is not trusted, nothing is asked**, and every rule above falls
back to the heartbeat alone: a quiet call is settled at its last heartbeat plus
one interval, a call that never heartbeated is free, and an ended call never
holds a line. That is how the gateway billed before the agent could be asked,
and what is lost is spelled out in
[the known limitation](#known-limitation-a-hang-up-that-leaves-the-call-running).
The live agent passes the canary; see
[Verified against the live agent](#verified-against-the-live-agent).

Where a probe is asked, and what each answer does:

| Asked by | About | Held | Gone | Unknown |
| --- | --- | --- | --- | --- |
| The sweep | A live call nobody vouched for in the timeout — one past its ceiling too | Kept: `agent_seen_at` stamped, billing goes on. A call that never heartbeated is [nudged](#a-call-that-never-heartbeated-the-nudge) the first time, and connected from its answer once proven. Past the ceiling: settled at the ceiling, `max_duration` | Settled to its last proof of life plus one interval, `disputed` — or at zero if it never connected — and `gone_at` stamped | Settled the same, without `gone_at` |
| A heartbeat | Its own call, after a silence past the timeout | Revived: `continue` | Settled, `stop`, `gone_at` stamped | Settled, `stop` |
| `POST /voice/sessions` | The account's recent ended calls not yet confirmed gone — a never-connected one only once proven connected | Counts against the line; the answer is reused for 5 seconds | `gone_at` stamped, never asked again | Counts nothing |

Each probe is one small request on the shared key, and they stay few: a kept
call is asked again only once its last confirmation is older than the timeout —
about once a minute — an open asks about five ended calls at most, a refused
retry inside five seconds reuses the last answer, and attempts are throttled
per account. The nudge is a second kind of request on the same route, sent at
most once per call once the agent accepts it. Neither probes nor nudges count
as upstream errors; see [Watching it](#watching-it) for where they do show.

---

## Errors

Branch on `code`, never on the message text. The body is the shape every route
in this API uses:

```json
{
  "detail": "You already have 1 call in progress. End it before starting another.",
  "statusMessage": "You already have 1 call in progress. End it before starting another.",
  "code": "voice_call_limit",
  "retryAfter": 45
}
```

`retryAfter`, with a matching `Retry-After` header, is present only where
waiting helps.

| Code | Status | Route | Means | Do |
| --- | --- | --- | --- | --- |
| `voice_agent_not_configured` | 503 | `POST`, candidates | This deployment has no agent | Nothing client-side. `GET /voice/config` already said `available: false` |
| `voice_agent_misconfigured` | 503 | config | `VOICE_AGENT_ICE_SERVERS` is not valid JSON, or has a TURN entry without its credential | Nothing client-side |
| `voice_agent_key_rejected` | 503 | `POST`, candidates | The agent refused **our** key | Nothing client-side. Logged at ERROR; somebody updates `VOICE_AGENT_API_KEY` |
| `voice_agent_unavailable` | 503 | `POST`, candidates | The agent is warming up | Retry after `retryAfter` — the agent's own, or 15 s |
| `voice_offer_expired` | 503 | `POST` | The answer landed after the call had already been released | Retry. Rare: a clock that jumped, a timeout edited under a running process, another tab hanging the call up mid-offer |
| `voice_agent_busy` | 429 | `POST`, candidates | Every line on the agent is taken — its concurrency ceiling, shared by every user | "All lines are busy." Honour `retryAfter` — the agent's own, or 5 s |
| `voice_call_limit` | 429 | `POST` | This account already has a call up — another tab, or a crashed one not yet released. `retryAfter` is the heartbeat timeout | "Hang the other call up, or wait." See [One line per account](#one-line-per-account) |
| `voice_call_still_connected` | 429 | `POST` | An ended call's peer connection is still open on the agent — a tab or app that hung up and did not close it. `retryAfter` 5 | "Close the other tab or app." Nothing server-side can end it. Retry after `retryAfter`; inside it the same answer is reused |
| `voice_call_rate_limited` | 429 | `POST` | More than `VOICE_AGENT_MAX_OPENS_PER_MINUTE` calls started in the last 60 s, or more than `max(30, 3 × that)` attempts this minute (with Redis) | Honour `retryAfter`. Break any reconnect loop |
| `voice_offer_no_video` | 400 | `POST` | No video transceiver | `pc.addTransceiver("video", {direction: "sendrecv"})` before `createOffer()` |
| `voice_offer_no_audio` | 400 | `POST` | No audio media line | Add the microphone track before `createOffer()` |
| `voice_offer_invalid` | 400 | `POST` | Not an SDP — it does not start with `v=0` | Send `pc.localDescription.sdp` unchanged |
| `voice_offer_rejected` | 400 | `POST` | The agent refused your SDP. The message is its own | Do not retry unchanged |
| `voice_candidate_rejected` | 400 | candidates | The agent refused a candidate. The message is its own | Drop it; ICE has others |
| `voice_candidates_exhausted` | 400 | candidates | 64 relayed already | Stop. A call that needs more is a client looping on `onicecandidate` |
| `voice_agent_unreachable` | 502 | `POST`, candidates | Timeout, transport failure, redirect, 5xx — or a base URL that is not the agent | Retry with backoff. Every call failing at once: the agent's URL changed |
| `voice_agent_unreadable` | 502 | `POST` | A 2xx answer in the wrong shape | Retry once, then report it |
| `voice_call_gone` | 409 | candidates | The agent already tore the call down. Ended and settled here too | Close the peer connection |
| `voice_call_ended` | 409 | candidates | The call is over here — settled by a heartbeat, the sweep or another tab — an end-of-candidates marker included | Stop sending candidates and close the peer connection. The reference client ends the call as `server_stop` |
| `voice_call_not_answered` | 409 | candidates, heartbeat | The offer is still in flight | Wait for the `201` |
| `voice_call_busy` | 409 | candidates | Two relays for one call kept colliding | Retry |
| `voice_call_not_found` | 404 | every `{id}` route | No such call, or not yours | End the call client-side. A 403 would confirm one exists |
| `insufficient_balance` | 402 | `POST` | The wallet cannot cover the hold | Top up by `shortfallMicros`. The shape is in [TTS.md → The `402`](TTS.md#the-402) |
| `wallet_frozen` | 403 | `POST` | The wallet is on hold | Contact support |
| `wallet_busy` | 409 | `POST` | The wallet is being written too often | Retry |
| `price_book_missing` | 503 | `POST` | No price book is published | Nothing client-side |
| `usage_metric_unknown` | 400 | `POST` | `session_ms` is not priced for `voice_agent` | Nothing client-side; `GET /voice/config` showed `hold: null` |
| `validation_error` | 422 | all | An SDP over 64 KB, a `type` other than `offer`, over 32 candidates, a candidate with a line break | Fix the request |
| `token_expired`, `token_invalid` | 401 | all | The access token | Refresh and retry — during a call, before the heartbeat timeout |

Note which 503s are worth retrying: `voice_agent_unavailable`, the only one
with a `retryAfter`, and the rare `voice_offer_expired`. The others need a
human, and telling a client to retry them is telling it to hammer a wall.

`voice_offer_too_large`, and `voice_offer_invalid` for a `type` other than
`offer`, exist in the service for callers that do not come through the route;
over HTTP the request schema answers `422 validation_error` first.

Support will also see a few codes that are never an HTTP response — they are
the `error_code` on a call's metered session, saying why it was released at
zero: `voice_never_connected`, `voice_call_orphaned` (a hold whose call row was
never written: the process died mid-open), `voice_offer_cancelled`,
`voice_offer_failed`, `voice_call_not_recorded` and `voice_answer_not_recorded`
— or the code of the refusal that released it, such as `voice_agent_busy`, or
`voice_call_limit` for a start that lost a race for the line.

---

## The three rules that break a call silently

The agent's guide lists three mistakes that produce a call which *looks*
connected and is not. One of them is visible in the offer, and this API refuses
it. The other two happen on a channel we never see.

| Rule | Without it | Enforced here |
| --- | --- | --- |
| 1. Add a video transceiver, although the call is audio only | Connects; no audio either way; no error anywhere | **Yes** — `400 voice_offer_no_video`, before any hold |
| 2. Create the data channel yourself, `pc.createDataChannel("events")`, before `createOffer()` | Audio works; no transcript and no state events. The agent never creates one | No. A call without one is legitimate, just blind |
| 3. Send `ping` on that channel every second once it opens | The agent drops the call after 3 s of quiet — it goes silent mid-conversation | No. The data channel is browser ⇄ agent and never passes through us |

Never opening a channel is safe; opening one and going quiet is not — so rule 2
brings rule 3 with it. The reference client does all three.

And one rule of ours, which is not silent but is money: **heartbeat from
`connected` to the hang-up.**

---

## What the data channel carries

Everything the agent says about the conversation arrives on the `events`
channel the client created, as JSON in Pipecat's RTVI shape:

```json
{ "label": "rtvi-ai", "type": "user-transcription", "data": { "text": "…", "final": true, "timestamp": "…" } }
```

The client sends nothing on it but `"ping"`. None of it passes through this
API. These are the types seen on real calls against the live agent:

| `type` | What it is | The reference client |
| --- | --- | --- |
| `user-started-speaking`, `user-stopped-speaking` | The agent's voice-activity detector on the user's audio | `onState("listening")` on the first; the second is `onEvent` only |
| `user-transcription` | What the user said: `text`, `final`, `timestamp`. Partials arrive `final: false` and are superseded | `onTranscript({role: "user", text, final})` |
| `user-llm-text` | The user's turn as handed to the language model | `onEvent` only |
| `bot-llm-started`, `bot-llm-text`, `bot-llm-stopped` | The language model's reply: its start, the tokens as they stream, its end | `onEvent` only |
| `bot-tts-text`, `bot-tts-stopped` | Text handed to speech synthesis | `onEvent` only |
| `bot-started-speaking`, `bot-stopped-speaking` | The agent's audio starts and stops | `onState("speaking")`, `onState("idle")` |
| `bot-output` | One sentence the agent speaks, twice: `spoken_status: "new"` when it starts, `"completed"` once it has been said. Also `aggregated_by`, `segment_id`, `spoken`, `spoken_progress`, which the client does not use | `onTranscript({role: "agent", text, final: true})` on `completed` |
| `bot-transcription` | The agent's words as a transcript | `onEvent` only |
| `bot-interrupted` | The user talked over the agent | Commits the cut-off sentence (below); `onState("listening")` |
| `metrics` | The agent pipeline's own timings | `onEvent` only |

The agent's guide also lists `error` (`message`), not seen on these calls; the
reference client hands it to `onEvent` and nothing else. Render the transcript
from `user-transcription` and `bot-output` alone: the `bot-llm-*`,
`bot-tts-*` and `bot-transcription` events carry the same reply at other
stages of the pipeline, and a UI that renders more than one of them shows every
sentence twice.

**A sentence the user talks over is never `completed`.** The agent sends its
`bot-output` as `new`, then `bot-interrupted`, and nothing more for it. A UI that
commits only on `completed` — which is what the agent's guide recommends —
silently loses it, and the greeting, spoken before the user knows to wait, is
an easy one to talk over. The
reference client keeps every `new` sentence it has not yet seen completed, and
on `bot-interrupted` commits them as one line:
`{role: "agent", text, final: true, interrupted: true}`. That is what the agent
had *started* to say, not necessarily what the user heard; mark it — struck
through, or with a trailing dash — rather than rendering it as a finished
sentence.

**The agent speaks first, and replies take a while to start.** The live agent
opens with a greeting — in Uzbek, as a registrar-office assistant — so a user
who waits hears it without saying anything. Its language model measured about
thirteen seconds to its first byte, so there is a noticeable gap between
`user-stopped-speaking` and `bot-started-speaking`. `bot-llm-started` marks the
start of that wait: a "thinking…" state can hang off it through `onEvent`, so
the call does not look frozen.

---

## The reference client: `dev-ui/voice-agent-client.js`

One framework-free ES module: no SDK, no dependencies, no build step. It talks
to this API and never to the agent, and the only credential it ever holds is
the user's own access token. Copy it into the frontend as it stands.

What it does beyond the agent guide's reference code:

- **The three silent failures are handled** — the video transceiver, the data
  channel, the ping.
- **Candidates are batched and trimmed.** Those gathered while the offer is in
  flight go as one request once there is an id, only for the transport the
  answer bundled everything onto and never more than the call's 64; later ones
  are coalesced for 25 milliseconds — typically one request per call instead
  of one per candidate.
- **The heartbeat is the bill.** The first one is sent the moment the media
  connects, `warn` becomes a state, and the server's `stop` ends the call here
  too. A failed one is retried every two seconds, each with a five-second
  timeout, and once none has got through for the server's heartbeat timeout the
  call is hung up here too (`heartbeat_lost`), rather than left talking to an
  agent the server may have stopped billing.
- **The ceiling is kept on both sides.** A local timer ends the call at
  `max_session_seconds` after the answer (`max_duration`), in case the server's
  `stop` never arrives.
- **A blip is not a hang-up.** WebRTC reports `disconnected` on a network switch
  and usually recovers by itself, so the call gets five seconds before it is
  ended.
- **ICE that is not connected forty-five seconds after the answer is given up** —
  the call is hung up, never connected, and costs nothing.
- **A hang-up survives the page closing, and does not wait.** The `DELETE` goes
  with `keepalive` from Stop and from `pagehide` alike. Stop sends it at once
  with the token already in hand rather than after a `getToken` round trip —
  a Stop followed at once by a navigation must still land — and retries it
  once with a fresh token if that one had expired (`401`). `onEnd` waits at
  most five seconds for the bill: `heartbeat_lost` fires exactly when this API
  is unreachable, and the UI must not hang on a request that may take minutes.
  The request itself is left to land.
- **An interrupted sentence is not lost.** What the agent had started saying
  when the user talked over it is committed as
  `{role: "agent", text, final: true, interrupted: true}` — see
  [What the data channel carries](#what-the-data-channel-carries).
- **A server-side end is an end.** A candidate relay answered
  `409 voice_call_ended` — the call was settled elsewhere — ends the call here
  as `server_stop`; `409 voice_call_gone` ends it as `agent_ended`.
- **A start can be abandoned at any point, and nothing is left behind.** An
  abort while the microphone prompt is up, or before the offer has been
  posted, is honoured without posting anything: no hold, no pipeline built on
  the agent for nobody. An abort mid-offer lets the `POST` finish, and the call
  it answers is hung up the moment its id arrives, rather than holding the line
  with nobody knowing it. A start that fails after the call was opened hangs it
  up, so its hold comes back at once rather than after a timeout.
- **A throwing callback cannot skip the hang-up.** Every callback is guarded;
  what it throws goes to the console.

```js
import { startVoiceCall, fetchVoiceConfig, VoiceCallError } from "./voice-agent-client.js";

const call = await startVoiceCall({
  apiBase: "https://back.synora-ai.uz/api/v1",
  getToken: () => auth.freshAccessToken(),       // a valid token every time — refreshed if it is about to expire
  audioElement: document.querySelector("#agent-audio"),
  onTranscript: ({ role, text, final, interrupted }) => render(role, text, final, interrupted),
  onState: (state) => setStatus(state),
  onEnd: (bill, reason) => showSummary(bill, reason),
});
stopButton.onclick = () => call.stop();
```

### `startVoiceCall(options)`

Resolves with a handle once the agent has answered; the media connects a moment
later, and `onState("connected")` says when. Rejects with a `VoiceCallError` —
or an `AbortError`, for `signal` — if the call could not be started, and
nothing stays held: a call the server had already opened has been hung up — or,
after an abort mid-offer, is hung up the moment its answer arrives. Both kinds
of rejection carry `sessionId`: the call the server had opened, or `null` (or
absent) when it had not — which includes an abort mid-offer, whose id was not
known yet. A start that rejects calls neither `onEnd` nor `onState("ended")`:
the rejection is the report.

| Option | | |
| --- | --- | --- |
| `apiBase` | required | e.g. `https://back.synora-ai.uz/api/v1` |
| `getToken` | required | `() => string \| Promise<string>`, called before every request. **Must return a valid token**, refreshing one about to expire. See below |
| `audioElement` | required | An `<audio autoplay>` that plays the agent |
| `onTranscript` | | `({role: "user" \| "agent", text, final, interrupted?})`. User partials arrive `final: false` and are superseded; the agent's sentences arrive once, when completed — or, cut off by the user, once on `bot-interrupted` with `interrupted: true`: text the agent had started, not necessarily all said |
| `onState` | | `connecting`, `connected`, `listening`, `speaking`, `idle`, `warning` (once, from `warn`), `ended` |
| `onHeartbeat` | | `(pulse)`, every heartbeat answer — `{action, elapsed_ms, remaining_ms, next_heartbeat_seconds}`. A call clock on the server's time |
| `onEnd` | | `(bill, reason)`, once, however the call ended — after a start that succeeded. `bill` is the `DELETE` answer, or `null` if it could not be fetched within five seconds |
| `onEvent` | | Every data-channel message that parses as JSON, raw: `{label: "rtvi-ai", type, data}`. See [What the data channel carries](#what-the-data-channel-carries) |
| `config` | | A `GET /voice/config` answer already in hand. Saves a round trip — but read it for this call: its TURN credential expires |
| `microphone` | | `MediaTrackConstraints`. Defaults to echo cancellation, noise suppression, auto gain, mono |
| `signal` | | An `AbortSignal` that cancels a call still connecting — rejecting with an `AbortError` at once. Before the offer is posted — while the config is read, or the microphone prompt is up — nothing is posted at all. Mid-offer, the `POST` finishes and the call it answers is hung up. It has no effect once the start has resolved: from then on, `stop()` |
| `fetch` | | The `fetch` to use instead of the global one — for logging, or tests |

| The handle | |
| --- | --- |
| `id` | The `ai_session_id` |
| `ended` | Whether it is over |
| `muted` | Whether the microphone is muted |
| `done` | A promise of the bill — the `DELETE` answer — or `null`: the page closed, or the `DELETE` did not answer within five seconds |
| `stop()` | Hang up. Idempotent; resolves with the bill, as `done` does |
| `mute(muted = true)` | Mute the microphone without ending the call. Still billed |

| `onEnd` reason | Cause |
| --- | --- |
| `hangup` | `stop()` |
| `server_stop` | A heartbeat answered `stop` — the server settled the call: its ceiling, or a silence the agent could not vouch for — or a candidate relay answered `voice_call_ended`: settled elsewhere |
| `max_duration` | The client's own ceiling timer. Whichever of this and `server_stop` fires first ends a call that runs to the ceiling |
| `heartbeat_lost` | No heartbeat got through for `heartbeat_timeout_seconds` — the network, or an access token that expired |
| `connection_lost` | The media dropped after connecting and did not come back in five seconds |
| `connection_failed` | ICE never connected. Costs nothing |
| `agent_ended` | A candidate relay answered `voice_call_gone` — the agent tore the call down |
| `not_found` | A heartbeat got a `404` |

A closing page calls no callback at all: it closes the connection and sends the
`DELETE` — with the last token `getToken` returned, since there is no time to
wait for another — and `done` resolves with `null`.

`fetchVoiceConfig({apiBase, getToken, signal, fetch})` is `GET /voice/config`.
`VoiceCallError` carries the API's `code`, plus `status`, `retryAfter`, the
parsed body as `data`, and `sessionId` when the server had opened the call. It
adds a few codes of its own, from the browser's side:

| Code | Means |
| --- | --- |
| `insecure_origin` | The page is not a secure origin, so the browser offers no microphone at all. Checked before the microphone is asked for |
| `microphone_denied` | The user, or the browser's policy, refused the microphone |
| `microphone_missing` | No microphone |
| `microphone_failed` | Anything else `getUserMedia` threw |
| `voice_ice_config_invalid` | `new RTCPeerConnection` refused the ICE servers — a TURN entry without its credential, most often |
| `network_error` | This API could not be reached |
| `voice_agent_not_configured` | `GET /voice/config` said `available: false` |
| `http_error` | A non-2xx that carried no `code` |
| `voice_call_failed` | Anything else before the answer was applied — the browser refusing the answer's SDP, most often |

The microphone is asked for before the offer is sent, so a user who refuses it
has started nothing and holds nothing.

### `getToken` must stay fresh for the whole call

It is called before every request, heartbeats included, and the client does not
refresh on a `401` — to it, a `401` is a heartbeat that failed. The one
exception is Stop's `DELETE`, which goes first with the token already in hand
and is retried once, with a fresh one, on a `401`. An access token
lives `ACCESS_TOKEN_TTL_MINUTES` (30), so one issued twenty-five minutes before
a call expires five minutes into it; from there every heartbeat fails, and
within forty-five seconds the call is hung up with `heartbeat_lost` — a dropped
call in the middle of a sentence. Since `getToken` runs every fifteen seconds,
it is enough for it to **refresh a token that expires within the next minute**
before returning it. The `pagehide` hang-up goes with the last token it
returned, which is one more reason for that token to be current.

### In Nuxt

The module touches `window` and `RTCPeerConnection` only when it is called, so
importing it is safe during server rendering; calling it happens in a click
handler, which is client-side by construction. Copy it to
`app/utils/voice-agent-client.js`. Three rules on the Vue side:

1. **Start from a click.** A call started without a user gesture has the
   agent's voice muted by the browser's autoplay policy.
2. **Keep the handle in state** — a `shallowRef`, because it closes over a live
   peer connection that Vue has no business proxying.
3. **Stop on unmount — a start still in flight included.** Navigating to
   another page of a single-page app fires no `pagehide`: without
   `onBeforeUnmount`, leaving the page keeps the microphone open and the call
   running until somebody notices. And a start takes seconds — the config, the
   microphone prompt, an offer the agent answers in six to thirty — so the user
   can leave while one is still connecting. Give every start an
   `AbortController`, abort it on unmount, and stop a call that resolves after
   the component has gone: nobody is left to press its Stop button.

```ts
// app/composables/useVoiceCall.ts
import { computed, onBeforeUnmount, ref, shallowRef } from "vue";
import { fetchVoiceConfig, startVoiceCall, VoiceCallError } from "~/utils/voice-agent-client.js";

type Line = { role: "user" | "agent"; text: string; interrupted?: boolean };

export function useVoiceCall(getToken: () => string | Promise<string>) {
  const apiBase = useRuntimeConfig().public.apiBase as string;

  const handle = shallowRef<Awaited<ReturnType<typeof startVoiceCall>> | null>(null);
  const config = shallowRef<Record<string, any> | null>(null);
  const state = ref("idle");
  const starting = ref(false);
  const warned = ref(false);
  const lines = ref<Line[]>([]);
  const partial = ref("");
  const bill = ref<Record<string, any> | null>(null);
  const endReason = ref<string | null>(null);
  const error = ref<VoiceCallError | null>(null);
  const active = computed(() => handle.value !== null);
  // The start in flight, so Cancel — or leaving the page — can abort it.
  let pending: AbortController | null = null;
  let unmounted = false;

  async function loadConfig(signal?: AbortSignal) {
    config.value = await fetchVoiceConfig({ apiBase, getToken, signal });
    return config.value;
  }

  // From a click handler, and only from one.
  async function start(audioElement: HTMLAudioElement) {
    if (pending || handle.value || unmounted) return;
    const controller = new AbortController();
    pending = controller;
    starting.value = true;
    error.value = null;
    bill.value = null;
    endReason.value = null;
    warned.value = false;
    lines.value = [];
    partial.value = "";
    try {
      const call = await startVoiceCall({
        apiBase,
        getToken,
        audioElement,
        signal: controller.signal,
        // Read again for every call rather than reused from mount: the TURN
        // credential in it is minted per request, and it expires.
        config: await loadConfig(controller.signal),
        onState: (next: string) => {
          if (next === "warning") warned.value = true;
          else state.value = next;
        },
        onTranscript: ({ role, text, final, interrupted }: Line & { final: boolean }) => {
          if (!final) {
            partial.value = text;
            return;
          }
          if (role === "user") partial.value = "";
          lines.value = [...lines.value, { role, text, interrupted }];
        },
        onEnd: (result: Record<string, any> | null, reason: string) => {
          bill.value = result;
          endReason.value = reason;
          handle.value = null;
          state.value = "idle";
        },
      });
      if (unmounted) {
        // Resolved after the component went away — the abort landed too late
        // to stop it. Nobody can press Stop now, so hang it up here.
        call.stop();
        return;
      }
      handle.value = call.ended ? null : call;
    } catch (caught) {
      state.value = "idle";
      // Cancelled — by Cancel, or by leaving the page. Nothing to show: the
      // client posted nothing, or hangs up whatever the server answers.
      if (unmounted || (caught as Error)?.name === "AbortError") return;
      error.value =
        caught instanceof VoiceCallError
          ? caught
          : new VoiceCallError((caught as Error)?.message ?? "The call could not be started.");
    } finally {
      if (pending === controller) pending = null;
      starting.value = false;
    }
  }

  // Hang up a call, or cancel one still starting.
  function stop() {
    pending?.abort();
    return handle.value?.stop();
  }
  const mute = (muted = true) => handle.value?.mute(muted);

  onBeforeUnmount(() => {
    unmounted = true;
    stop();
  });

  return {
    config, state, starting, warned, lines, partial, bill, endReason, error, active,
    loadConfig, start, stop, mute,
  };
}
```

```vue
<!-- app/components/VoiceCall.vue -->
<script setup lang="ts">
// Adapt to what `useAuthSession` exposes. What matters: it is read per request,
// and it refreshes an access token that expires within the next minute.
const getToken = useAccessTokenGetter();

const { config, state, starting, warned, lines, partial, bill, error, active, loadConfig, start, stop } =
  useVoiceCall(getToken);
const agentAudio = ref<HTMLAudioElement | null>(null);

onMounted(() => loadConfig().catch(() => {}));
</script>

<template>
  <section v-if="config?.available">
    <audio ref="agentAudio" autoplay />
    <button v-if="!active && !starting" @click="start(agentAudio!)">Start</button>
    <button v-else @click="stop()">{{ starting ? "Cancel" : "Stop" }}</button>

    <p>{{ state }}</p>
    <p v-if="active && warned">The call ends in under a minute.</p>
    <p v-for="(line, i) in lines" :key="i" :class="[line.role, { interrupted: line.interrupted }]">
      {{ line.text }}<span v-if="line.interrupted"> —</span>
    </p>
    <p v-if="partial" class="partial">{{ partial }}</p>
    <p v-if="error">{{ error.message }}</p>
    <p v-if="bill">{{ Math.ceil(bill.billed_ms / 1000) }} s, {{ bill.price }} credits</p>
  </section>
</template>
```

`useAccessTokenGetter` stands for whatever the app's auth layer provides; it is
not part of this API. Branch on `error.code` for anything past a message —
`insufficient_balance` opens the top-up dialog with `error.data.shortfallMicros`;
`voice_call_limit` says a call is already up on this account — hang it up, or
wait `error.retryAfter` for a crashed tab's to be released;
`voice_call_still_connected` says the previous call is still connected from
another tab or app, asks the user to close it, and retries after
`error.retryAfter` (5); `voice_call_rate_limited` waits `error.retryAfter`;
`insecure_origin` and `microphone_denied` explain the permission;
`error.sessionId` is the call to look up when a start failed after the server
had opened it. Checking `config.hold_micros` against the wallet before enabling
Start saves the user the `402` altogether.

The page must be a secure origin for the browser to grant the microphone;
`http://localhost:3000` counts as one during development, and over plain
`http` anywhere else the client rejects with `insecure_origin` before asking.

---

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `VOICE_AGENT_BASE_URL` | unset | The agent's base URL. Empty ⇒ `POST /voice/sessions` and candidates answer `503 voice_agent_not_configured`, and `GET /voice/config` says `available: false`, with no prices and no TURN credential |
| `VOICE_AGENT_API_KEY` | unset | Sent as `X-API-Key`. The agent's **`ui`** roster entry — never the `admin` one, which is for maintenance on the agent's own box. One of the pair without the other is refused at boot outside development |
| `VOICE_AGENT_MODEL_KEY` | `synora-voice-agent` | The price row a call bills against, beside service `voice_agent`. Its `session_ms` price must be the whole price of a minute |
| `VOICE_AGENT_CONNECT_TIMEOUT_SECONDS` | `10` | Also the budget for one candidate relay |
| `VOICE_AGENT_OFFER_TIMEOUT_SECONDS` | `35` | Covers the agent's warm-up — up to 30 s after it restarts; a warm agent answered in 6–8 s when measured. The whole offer deadline is this plus the connect timeout — 45 s — and an offer still unanswered after twice that, 90 s, is released by the sweep. Give nginx's `location /api/v1/voice/` a `proxy_read_timeout` of at least 90 s — see [The proxy in front of this API](#the-proxy-in-front-of-this-api) |
| `VOICE_AGENT_MAX_SESSION_SECONDS` | `600` | The ceiling on one call, and what its hold is priced from. At least 60 outside development |
| `VOICE_AGENT_HEARTBEAT_SECONDS` | `15` | How often a connected client heartbeats — and the interval a lapsed call is billed past its last proof of life |
| `VOICE_AGENT_HEARTBEAT_TIMEOUT_SECONDS` | `45` | Silence past this puts the call to the agent, and ends it unless the agent holds it. At least twice the interval outside development, so one lost request does not end a call |
| `VOICE_AGENT_MAX_CONCURRENT_PER_USER` | `1` | Lines per account: live calls (`voice_call_limit`), plus ended ones whose connection the agent still holds (`voice_call_still_connected`) — one that never connected only once it has proved it did. Counted from the rows, so it holds with or without Redis. At least 1 outside development |
| `VOICE_AGENT_MAX_OPENS_PER_MINUTE` | `10` | Calls an account may start in any 60 seconds. Counted from the rows, so it holds with or without Redis. With Redis, attempts are throttled too, at three times this a minute and never below 30. At least 1 outside development — the boot refuses 0 — and there is no off switch: set it high to lift it. In development 0 refuses every open, `429` with `Retry-After` 60 |
| `VOICE_AGENT_ICE_SERVERS` | `[{"urls": ["stun:stun.l.google.com:19302"]}]` | A JSON list of `{urls, username?, credential?}`, handed to browsers verbatim. A `turn:`/`turns:` entry needs both `username` and `credential`. Malformed is refused at boot outside development, and `503 voice_agent_misconfigured` inside it |
| `VOICE_AGENT_TURN_URLS` | unset | Comma-separated TURN URLs to mint per-user credentials for, e.g. `turn:turn.example.com:3478?transport=udp,turns:turn.example.com:5349`. Set with the secret or not at all — one without the other is refused at boot outside development |
| `VOICE_AGENT_TURN_SECRET` | unset | The secret shared with the TURN server (coturn's `static-auth-secret`). At least 16 bytes outside development. Never leaves this process |
| `VOICE_AGENT_TURN_TTL_SECONDS` | `0` | How long a minted credential lasts. `0` ⇒ the ceiling plus 600 s |
| `VOICE_AGENT_SWEEP_SECONDS` | `30` | How often each process settles calls nobody hung up, asking the agent about the quiet ones first. `0` turns the loop off. It runs with or without an agent configured — it needs only the database |

Fixed in code rather than configured: `warn` starts 60 seconds before the
ceiling; an offer may be 64 KB; a request carries at most 32 candidates, a call
at most 64, a candidate at most 1 024 characters; a probe and a nudge each wait
5 seconds; the canary's verdict is kept 600 seconds when trusted and 60 when
not; a call that never heartbeated counts as connected once the agent still
holds it 90 seconds after its nudge and 120 after its answer
(`NUDGE_GRACE_SECONDS`, `NEVER_CONNECTED_GRACE_SECONDS`); a call hung up less
than 5 seconds ago is asked about for up to 5 seconds more, and a "still held"
answer is reused for 5 seconds (`HANGUP_SETTLE_SECONDS`); the line check looks
at the 10 most recent ended calls from the last 24 hours, the 2 most recent
unproven ones exempt; attempts are
throttled at `max(30, 3 × VOICE_AGENT_MAX_OPENS_PER_MINUTE)` a minute; an offer
still unanswered after twice its deadline — 90 seconds by default — is
released.

Prices are not here. They live in the database, versioned, and are published
through the admin API — see
[Credits and the wallet](../README.md#credits-and-the-wallet).

---

## Operating it

### TURN, for calls across networks

STUN alone is enough on a LAN and behind most home routers. Past that — a
corporate firewall, a phone on mobile data, symmetric NAT — the browser and the
agent cannot reach each other directly, and a TURN server has to relay the
media. The symptom is always the same and never an error: `connectionState`
never reaches `connected`, the reference client ends with `connection_failed`,
the call is recorded with `connected_at: null` at price zero, and
`synora_voice_calls_total{connected="no"}` climbs — the dashboard's "Calls
that never connected" stat.

**Mint credentials rather than write them down.** Point this gateway at a TURN
server that implements the TURN REST scheme — coturn's `use-auth-secret` — and
give both the same secret:

```bash
# .env here
VOICE_AGENT_TURN_URLS=turn:turn.example.com:3478?transport=udp,turns:turn.example.com:5349
VOICE_AGENT_TURN_SECRET=replace-me           # openssl rand -hex 32
VOICE_AGENT_TURN_TTL_SECONDS=0               # the ceiling plus ten minutes
```

```
# turnserver.conf on the TURN host
use-auth-secret
static-auth-secret=replace-me                # the same value
realm=turn.example.com
```

`GET /voice/config` then appends one entry per request, minted for the
signed-in user: the username is `<expiry>:<user_id>` — the unix time the
credential stops working, and whose it is — and the credential
`base64(hmac_sha1(secret, username))`. The TURN server checks the HMAC and the
expiry and needs no database, and a credential copied out of a browser stops
working about when the call it was minted for could no longer be running. It is
minted only when the agent is configured, and only for a signed-in account.
Clients must read the config per call, as the reference client does unless it
is handed one.

**Static credentials still work** in `VOICE_AGENT_ICE_SERVERS`, at a cost:
everything in that list reaches every signed-in account, so a password there is
shared and never expires, and anyone with an account can use the relay for
anything it permits. A `turn:` or `turns:` entry without both `username` and
`credential` is refused at boot: every browser's `RTCPeerConnection`
constructor throws on one — after the microphone is already open — which the
reference client would report as `voice_ice_config_invalid` on every call.

Either way, limit what the relay will do — quotas, and peers restricted to the
agent's address. Minted credentials expire; they do not stop a signed-in
account from minting one.

### The agent's URL changes when its tunnel restarts

The agent is reached through a temporary tunnel at the moment, and a quick
tunnel gets a new hostname every time it restarts. The symptom is every
`POST /voice/sessions` answering `502 voice_agent_unreachable`, with nothing
wrong on either box. Get the current URL from the agent's operator, set
`VOICE_AGENT_BASE_URL`, restart, and read the startup line:

```
synora: Voice agent: https://<current-tunnel>.trycloudflare.com
```

`not configured (opening a call answers 503; …)` means one of the pair did not
reach the process. A stable hostname belongs on the list before anyone depends
on calls, for the same reason as the speech box's. And the tunnel carries only
the signalling: it does nothing for the media, which is what TURN is for.

### The proxy in front of this API

`POST /voice/sessions` can legitimately take the offer timeout plus the connect
timeout — 45 seconds by default — and, before the offer goes out, whatever the
checks ahead of it need: settling the account's own dead calls, each quiet one
put to the agent first, and asking whether a just-hung-up call has let go,
which can take five seconds more. Against a healthy agent those checks are
milliseconds; against one slow to answer its probes, each waits up to five
seconds. nginx's `proxy_read_timeout` defaults to 60, which the ordinary case
fits under and the bad one need not.

Keep the proxy the longer of the two: one that gives up first answers the
browser `504` while the call is still being answered behind it, the client
never learns the id, and the user's line stays busy until the call is
released — free, but only once the sweep has found it quiet and the agent has
let go of it. Give the voice routes their own location, with the same
`proxy_set_header` lines as the existing `location /`:

```nginx
# /etc/nginx/sites-available/back.synora-ai.uz.conf
location /api/v1/voice/ {
    proxy_pass http://127.0.0.1:8010;
    proxy_read_timeout 90s;
    # … the proxy_set_header lines from `location /` …
}
```

Raise it again with `VOICE_AGENT_OFFER_TIMEOUT_SECONDS`: the proxy's timeout
should stay at least twice the offer deadline.

### Watching it

| Metric | Worth watching for |
| --- | --- |
| `synora_voice_upstream_seconds{operation}` | `offer` is six to eight seconds against a warm agent and runs to tens of seconds on a cold start; `candidates` and `probe` should be milliseconds. Nudges are not timed |
| `synora_voice_upstream_errors_total{operation,code}` | By our code, not by status — `voice_agent_key_rejected` is a rotated key, `voice_agent_unreachable` en masse is a moved tunnel. Probes and nudges never count here |
| `synora_voice_calls_live` | Calls not yet settled, read from `voice_calls` at scrape time. A floor that climbs on a quiet night is calls nobody is settling — check the sweep is running |
| `synora_voice_calls_total{end_reason,connected}` | `connected` is `yes` (billed), `no` (answered, media never came up — missing TURN, or a client that never heartbeats) or `unanswered` (the agent refused or never answered the offer — an agent problem, see the errors). Every combination is exported at zero from process start, so the first one after a restart is counted |
| `synora_voice_call_seconds_total` | Seconds billed, first heartbeat to hang-up, capped at the ceiling. Added when a call settles, all at once |
| `synora_voice_probes_total{result}` | `alive`, `gone`, `unknown` for probes of real calls; the canary counts apart, as `canary_trusted` or `canary_untrusted` — the second is an agent whose answers are not believed, and billing has fallen back to heartbeats alone. `unknown` climbing is an agent that cannot be reached |
| `synora_voice_nudges_total{result}` | `taken`, `refused`, `unreachable`. `refused` above zero is an agent that will not take the nudge, and never-heartbeated calls then stay unbilled |
| `synora_voice_calls_kept_total` | Quiet spells the agent vouched for, once per silence rather than once per re-check, plus a never-heartbeated call the moment it proves it connected: a network that reaches the agent and not us, a bug, or somebody trying it on |

The Grafana dashboard in `grafana/` has a **Voice agent** row built on these:
calls in progress, calls per minute by how they ended, conversation billed,
agent latency, agent failures, probes and nudges, kept spells, and the share of
answered calls that never connected.

The log lines to grep: `voice_call_answered` and `voice_call_ended` (with
`connected`, `billed_ms`, `reason` and `heartbeats`) for each call;
`voice_call_kept` and `voice_call_revived` at WARNING, a call whose client went
quiet and which the agent said was up — the call id is how to tell a network
from a bug from an experiment. `voice_call_kept` also says where a call that
never heartbeated stands: `connected=nudged` the first time (the only place a
nudge shows), `not yet` while its graces run, `yes` once it is proven and
billed from its answer. `voice_call_outlived_settlement` at WARNING, a
hung-up call the agent still holds, which is what a
`voice_call_still_connected` refusal is about; `voice_orphan_released` at WARNING, a
hold whose call row was never written — a process that died mid-open, and the
session id is the thread to pull; `voice agent liveness probes …`, once per
change of the canary's mind; `voice agent rejected our key` at ERROR.

### Known limitation: a hang-up that leaves the call running

Nothing on this side of the gateway can make a client hang up: the agent offers
no route to end a call, and the media never passes through us. What the agent
does answer is whether it still holds one, and while that answer is trusted:

- **Stopping the heartbeat and talking on is billed.** The call is kept while
  the agent holds it, billed to its last confirmation plus one interval once the
  agent lets go, or to the ceiling. It is `disputed`, and
  `synora_voice_calls_kept_total` counts it. A kept call is asked about roughly
  once a minute, so its bill can fall up to a minute short of when the agent
  actually let go.
- **Never sending the first heartbeat is billed too**, from the answer, once the
  call has [proved it connected](#a-call-that-never-heartbeated-the-nudge): still
  held 90 seconds after its nudge and 120 after its answer — 135 to 225 seconds
  in at the default cadences.

Two ways through remain.

**Never heartbeat, and leave before the call has proved it connected.** Until
then a call that never heartbeated looks exactly like an ICE failure, and an
ICE failure must be free. So a modified client that never heartbeats and
closes its peer connection within about two minutes of the answer always gets
that call free, and one that closes within about four may. It gets one such
call at a time — a live call holds the line — and as many a minute as the
throttles allow. `synora_voice_calls_kept_total` climbing with
`connected="no"` settlements behind it, and `voice_call_kept … connected=nudged`
on one account over and over, is what it looks like.

**Hang up with `DELETE` and keep the peer connection open.** The call is
settled at the `DELETE`, and the agent, which never hears from us, keeps
serving it for as long as the peer connection and its ping stay up. What bounds
it is the line: an ended call the agent still holds occupies its account's line
([One line per account](#one-line-per-account)), so one account holds at most
`VOICE_AGENT_MAX_CONCURRENT_PER_USER` of the agent's connections, billed or
not, and the shared agent's capacity stays everybody's. Each one is a
`voice_call_outlived_settlement` line.

**All of it depends on the canary.** Against an agent whose probe is not
trusted — unreachable from here, or answering `200` to a `pc_id` that cannot
exist — billing is the heartbeat's alone again: a client that stops
heartbeating is billed to its last heartbeat plus one interval, one that never
starts is free, and an ended call holds no line, so an account can hang up and
reopen as fast as the open throttle allows. The only hard ceiling on how many
free-running calls it ends up holding is then the agent's own concurrency
limit, which answers `voice_agent_busy` to everybody. That state announces
itself with one WARNING (`liveness probes not trusted`) and shows in
`synora_voice_probes_total{result="unknown"}`.

The fix is on the agent's side, and the day it exists this gateway should read
it: an agent-side session ceiling at or below `VOICE_AGENT_MAX_SESSION_SECONDS`,
or the agent reporting its own usage over the signed internal API
([INTERNAL_API.md](INTERNAL_API.md) — the credentials and wire format are
settled there; the session lifecycle and usage-ingest routes are the next thing
to be built). Until then, watch for it: `voice_call_outlived_settlement` and
`voice_call_kept` on one account, the `disputed` share of connected calls, and
the canary's line in the log.

---

## Testing checklist

Adapted from the agent guide's, for a client that goes through us. Work through
them in order; each isolates a different failure.

- [ ] `GET /voice/config` answers `available: true` from the frontend's origin,
      with no CORS error — this API's `CORS_ORIGINS`, not the agent's
- [ ] With less credit than `hold`, Start is disabled or `POST` answers `402`
      with `shortfallMicros` — and `GET /wallet` shows nothing reserved
- [ ] Start prompts for microphone permission
- [ ] Denying it shows your error state (`microphone_denied`), not a crash, and
      no call appears in `GET /voice/sessions`
- [ ] `POST /voice/sessions` answers `201`, and `reserved` equals the config's
      `hold`
- [ ] `connectionState` reaches `connected`, and `GET /voice/sessions/{id}`
      shows `connected_at` a moment later — the first heartbeat landed
- [ ] The agent greets you first: you hear it through the `<audio>` element
      without saying anything
- [ ] Speaking produces `user-transcription` events with your words
- [ ] The agent replies — after a pause of several seconds — and each sentence
      renders exactly once
- [ ] Talking over the agent interrupts it, and the sentence it was cut off in
      stays in the transcript, marked `interrupted`
- [ ] A call left open for 60+ seconds stays alive — both the ping and the
      heartbeat are running
- [ ] DevTools → Network → Offline for fifteen seconds and back does not end
      the call. Offline for longer than `heartbeat_timeout_seconds` ends it on
      the client with `heartbeat_lost`, and once back online the call reads
      `heartbeat_timeout`, `disputed`, billed to its last proof of life plus
      one interval
- [ ] Stop ends the call, clears the browser's recording indicator, and the
      `DELETE` answers `status: "ended"` with a price; `GET /wallet` shows
      `reserved` back at `0.000000`
- [ ] Start works again straight after Stop — a few seconds slower at most,
      never a `429`
- [ ] Reloading the page mid-call leaves the old call `client_hangup`, not
      `heartbeat_timeout`, and a new call starts without a `429`
- [ ] After a call that never connected (`connection_failed`), Start works
      again at once — never `voice_call_still_connected`
- [ ] Navigating away inside the app stops the call and the microphone — and
      so does navigating away while it is still connecting: no call is left
      `live` in `GET /voice/sessions`
- [ ] A second tab starting a call while one is up gets `429 voice_call_limit`
- [ ] Removing the video transceiver gets `400 voice_offer_no_video`
- [ ] With `VOICE_AGENT_MAX_SESSION_SECONDS=90` locally, `warning` arrives
      about 30 seconds in and the call ends itself at about 90, billed exactly
      90 000 ms with `end_reason: max_duration`
- [ ] From another network — a phone on mobile data — the call connects. If it
      does not, and it works on the LAN, TURN is missing; with
      `VOICE_AGENT_TURN_URLS` set, `GET /voice/config` lists an entry whose
      `username` is `<expiry>:<your user id>`
- [ ] After a call and a second Start, the API's log has
      `voice agent liveness probes trusted`. `not trusted` means the
      protections of [the known limitation](#known-limitation-a-hang-up-that-leaves-the-call-running)
      are off against this agent

---

## Verified against the live agent

Measured on 2026-09-25 with real WebRTC calls placed through this API against
the live agent, reached through a temporary Cloudflare tunnel whose hostname
changes on every restart.

| What | Measured |
| --- | --- |
| `GET /healthz` | `200` |
| `POST /api/offer` without a key | `401` — nobody but this server holds the key |
| The offer | Answered in 6–8 seconds while the agent builds the call's pipeline. `pc_id` looks like `SmallWebRTCConnection#4-` followed by 32 hex digits |
| `PATCH /api/offer`, empty list, a live `pc_id` | `200 {"status": "success"}` |
| `PATCH /api/offer`, an unknown `pc_id` | `404 {"detail": "Peer connection not found"}` — so the [canary](#asking-the-agent-whether-a-call-is-still-up) trusts it, and the probes are acted on |
| A call the browser closed | `404` to the probe within about 4 seconds |
| A peer that never received a single candidate | Dropped by the agent on its own, about 60 seconds after the offer |
| The conversation | The agent greets first, in Uzbek, as a registrar-office assistant. Its language model, `google/gemma-4-31B-it`, took about 13 seconds to its first byte |
| The data channel | The RTVI events listed in [What the data channel carries](#what-the-data-channel-carries), label `rtvi-ai`. A sentence the user talked over got `new` and then `bot-interrupted` — never `completed` |
| Ordinary calls of 16–40 seconds | Billed `0.500000`: one started minute at the seed |
| A tab killed mid-call | Billed to its last heartbeat plus 15 seconds, `disputed` |

Not among these measurements: the agent at its own concurrency ceiling
(`voice_agent_busy`) or warming up (`voice_agent_unavailable`) — both come
from its guide, and the fake agent imitates them — and a call across networks
through TURN.

---

## Trying it locally

No agent needed. `dev-ui/fake_voice_agent.py` stands in for it: the same three
routes, the same `X-API-Key`, and the same three rules — no video transceiver,
no audio; the client opens the data channel; three seconds without a `ping`
drops the call. It also gives the real agent's answer to a `pc_id` it does not
hold, a `404`, so the canary trusts it and every probe above runs against it
as it would against the agent. And like the live agent it drops a peer that
has not connected 60 seconds after its offer, with or without `aiortc` —
without that, an aiortc peer that never received a candidate would sit in ICE
checking forever and look connected to every probe.

```bash
pip install aiortc                             # dev only, not in requirements
python dev-ui/fake_voice_agent.py              # → http://127.0.0.1:8200  (--port to move it)
VOICE_AGENT_BASE_URL=http://127.0.0.1:8200 VOICE_AGENT_API_KEY=fake-key \
    .venv/bin/uvicorn app.main:app --port 8000
```

With `aiortc` installed it is a real WebRTC peer that echoes the microphone
back and emits `user-transcription`, `bot-output` (`new`, then `completed`) and
the speaking-state events on the data channel, labelled `rtvi-ai` — a subset
of the live agent's list, with no greeting, no `bot-llm-*` events and no
interruptions — so a browser call goes end to end: the voice you hear is your
own. It drops a closed call within three seconds, as the agent does. Without
`aiortc` the fake answers with an SDP no browser can connect to: enough to
watch the hold, the `201`, the candidates and the nudge, not a call. Such a call
never connects, so the fake drops it 60 seconds after the offer: hang one up,
and the next start is not refused (a call that never connected does not hold
the line until it proves it did, and by then the fake has let go); leave one
up — a `curl`ed offer — and the sweep may keep and nudge it once, then finds it
gone and releases it at zero.

Swap `VOICE_AGENT_API_KEY` to see the failure paths without breaking anything.
The fake answers the probe's `PATCH` with the same refusal, so with any of the
four the canary does not trust it and billing runs on heartbeats alone:

| Key | The fake answers | We answer |
| --- | --- | --- |
| anything else | `200` | a call |
| `reject-me` | `401` | `503 voice_agent_key_rejected` |
| `busy` | `429` | `429 voice_agent_busy` |
| `warming` | `503` | `503 voice_agent_unavailable` |
| `bad-offer` | `400` | `400 voice_offer_rejected` |

A throwaway database, a funded account and a seeded price book come first,
exactly as in [TTS.md → Trying it locally](TTS.md#trying-it-locally). Then:

```bash
curl -s "$API/voice/config" -H "$A"           # → available: true, hold: "5.000000"
# … place a call from the frontend, or from dev-ui/voice_call.py without a browser …
curl -s "$API/voice/sessions?limit=1" -H "$A" # → status "ended", billed_ms, price
curl -s "$API/usage" -H "$A"                  # → voice_agent/session_ms
curl -s "$API/wallet" -H "$A"                 # → reserved back at 0.000000
```

To watch the sweep without waiting on production cadences, shorten them — the
timeout must stay at least twice the interval:

```bash
VOICE_AGENT_HEARTBEAT_SECONDS=3 VOICE_AGENT_HEARTBEAT_TIMEOUT_SECONDS=9 VOICE_AGENT_SWEEP_SECONDS=5 …
```

### A call from the terminal: `dev-ui/voice_call.py`

A real call through this API with no browser: an aiortc peer, a 440 Hz tone
standing in for the microphone, the heartbeat at the server's cadence, and a
check of the bill at the end. It works against the fake and against the real
agent alike — the only difference is the API's `VOICE_AGENT_BASE_URL`.

```bash
pip install aiortc httpx                                      # dev only
python dev-ui/voice_call.py --email ali@example.com --password Str0ngPassw0rd
python dev-ui/voice_call.py --email … --password … --seconds 40 --wav clip.wav --events
python dev-ui/voice_call.py --email … --password … --no-hangup
python dev-ui/voice_call.py --email … --password … --no-heartbeat --seconds 240 --no-hangup
```

| Option | Default | |
| --- | --- | --- |
| `--api` | `http://127.0.0.1:8000/api/v1` | This API |
| `--email`, `--password` | required | The account that pays |
| `--seconds` | `20` | How long to stay on the call once it connects, heartbeating throughout |
| `--wav` | the tone | An audio file to send instead |
| `--events` | off | Print every data-channel event as it arrives, as JSON (cut at 900 characters) |
| `--no-heartbeat` | off | The abuse test: connect, keep pinging the agent, send no heartbeat for `--seconds`. The server has to learn about the call from the agent. Meant to be run with `--no-hangup` |
| `--no-hangup` | off | A crashed tab: close the peer connection, send no `DELETE`, and poll until the server ends the call on its own — for up to the heartbeat timeout plus 90 seconds |

It prints the user's and the agent's final lines as they arrive and, at the
end, the bill, how many audio frames came from the agent, and how many
data-channel events of each type — against the live agent, the list in
[What the data channel carries](#what-the-data-channel-carries). It exits `0`
only if audio arrived from the agent (against the fake, which echoes it, that
is audio both ways), the call ended with nothing still reserved, a second
`DELETE` answered with the same bill (skipped with `--no-hangup`), and the
wallet's `reserved` is back at zero.

With `--no-hangup` against the fake or the live agent, the bill should read
`heartbeat_timeout`, `disputed`, billed to the last heartbeat plus one
interval: the agent drops the closed peer within seconds, so the sweep's probe
answers `gone`.

With `--no-heartbeat`, what the bill says depends on how long the call ran,
which is the point. The sweep finds it quiet 45 to 75 seconds in, keeps it and
nudges it, and takes it as connected from its answer only once it is still held
past [both graces](#a-call-that-never-heartbeated-the-nudge) — 135 to 225
seconds in. `--seconds 240` is past that at the default cadences: expect it
billed from the answer to the agent's last confirmation plus one interval,
`disputed`, with `voice_call_kept … connected=nudged` and later
`connected=yes` in the API's log. Much shorter, and the call ends free — which
is the right answer for a call that has not yet proved it connected. The graces
are fixed, so shortening the heartbeat and sweep cadences brings the first
`kept` forward but not the moment it is billed.

aiortc writes every candidate into the offer, so this never exercises
`…/candidates`; the browser does. A hang-up that leaves the connection open is
not something either client does; `tests/test_voice_agent_liveness.py` pins it,
with the rest of the kept and lingering paths, against an agent that tracks its
peers.

### The agent team's own pages

`docs/UI_INTEGRATION_GUIDE.md`, `docs/QUICKSTART.md`, `docs/test-client.html`
and `docs/index.html` are the agent team's material for calling the agent
**directly**, with its key in the browser — the thing a production frontend
must not do. They are useful for one question: when calls fail, is the agent
itself up? `test-client.html` answers it without this API in the way.
`QUICKSTART.md` carries a live credential: `.gitignore` keeps it out of the
repository, it does not belong in anybody's hands outside the team, and when
that key is rotated, `VOICE_AGENT_API_KEY` here is the other place that
changes. `UI_INTEGRATION_GUIDE.md` is the same material with placeholders, and
the one to share.
