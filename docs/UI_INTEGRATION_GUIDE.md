# Voice Agent API — UI Integration Guide

A step-by-step guide to connecting any web UI to the Voice Agent API.

The agent is a black box: your UI streams microphone audio to it and receives
the agent's spoken reply as an audio stream, plus a live feed of text events
you can render. You do not need to know, configure, or handle anything about
how speech is recognised, generated, or reasoned about — that is entirely
server-side.

**What you build:** a Start button, a Stop button, an `<audio>` element, and
(optionally) a transcript view.

**Requirements:** any framework or none. The reference code below is plain
JavaScript using the browser's built-in `RTCPeerConnection` — no SDK, no
dependencies, no build step.

---

## 1. What you need before you start

| Item | Example | Where from |
|---|---|---|
| **Base URL** | `https://your-agent.example.com` | Your API operator |
| **API key** | `pv_ak_…` | Your API operator |
| **HTTPS origin** | `https://your-ui.example.com` | Your own site |

Three constraints to know up front:

1. **HTTPS is mandatory.** Browsers only grant microphone access on a secure
   origin. `http://localhost` counts as secure during development; any other
   plain-HTTP origin does not.
2. **Your origin must be allow-listed** by the API operator, or the browser
   will block every request. Send them the exact origin (scheme + host + port).
3. **The API key is not safe in a public browser bundle.** Anyone who opens
   devtools can read it. See §8 for what to do in production.

---

## 2. The API surface

Two endpoints. That is the entire integration.

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/healthz` | Is the service reachable? | none |
| `POST` | `/api/offer` | Open a voice session | `X-API-Key` |
| `PATCH` | `/api/offer` | Add network candidates to a session | `X-API-Key` |

Any other path returns `404`. That is deliberate — the surface is
intentionally limited to what a UI needs.

Ending a session needs **no** API call: you close the connection in the
browser and the server tears the session down on its own.

### Authentication

Send the key on both `/api/offer` calls. Either header works:

```http
X-API-Key: pv_ak_…
```
```http
Authorization: Bearer pv_ak_…
```

A missing or wrong key returns `401` with `{"detail": "unknown or missing api key"}`.

---

## 3. Step 1 — Confirm you can reach the service

Before wiring any audio, verify the base URL, HTTPS, and CORS in one call:

```js
const res = await fetch(`${BASE_URL}/healthz`);
console.log(res.status, await res.json());   // 200 {status: "ok"}
```

- **`200 {status:"ok"}`** — good, continue.
- **CORS error in console** — your origin is not allow-listed. Send it to the
  API operator. Nothing else will work until this is fixed.
- **Network error / timeout** — wrong base URL, or the service is down.

`/healthz` does not check your API key. It only proves the service answers.

---

## 4. Step 2 — Understand the connection

The session is a live two-way audio connection, not a request/response call.
Opening it is a short negotiation:

```
your UI                                          agent
   │  1. POST /api/offer   { sdp, type:"offer" }    │
   │───────────────────────────────────────────────►│
   │     { sdp, type:"answer", pc_id }              │
   │◄───────────────────────────────────────────────│
   │  2. PATCH /api/offer  { pc_id, candidates }    │
   │───────────────────────────────────────────────►│
   │  3. live audio, both directions                │
   │◄──────────────────────────────────────────────►│
   │  4. live text events (transcript, state)       │
   │◄───────────────────────────────────────────────│
```

You never parse the `sdp` strings — you pass them between the browser API and
the server unchanged. `pc_id` is the session handle; keep it for step 2.

### Three rules that will silently break you

These are the three mistakes that produce a session which *looks* connected
but does not work. Get them right and the rest is routine.

**Rule 1 — add a video transceiver, even though this is audio-only.**

```js
pc.addTransceiver(micTrack, { direction: "sendrecv" });
pc.addTransceiver("video",  { direction: "sendrecv" });   // required
```

Omit the second line and the connection establishes but no audio ever flows.
You will see no error in the browser. Your camera is never opened or
requested — only the channel is negotiated.

**Rule 2 — you must create the data channel.**

The server never creates one. If you skip this, audio works but you receive
**no transcript and no state events** — which makes this easy to
misdiagnose as "events are broken".

```js
const channel = pc.createDataChannel("events");
```

Create it *before* generating the offer.

**Rule 3 — send a keepalive once per second.**

Once the channel is open, the server expects a `ping` at least every 3
seconds. Stop sending and it treats the session as gone: the agent goes
silent mid-conversation.

```js
channel.onopen = () => setInterval(() => channel.send("ping"), 1000);
```

(Never opening a channel at all is safe. Opening one and then going quiet is
not — so if you use Rule 2, you must also do Rule 3.)

---

## 5. Step 3 — The Start button

Full, working implementation. Copy it as-is.

```js
const BASE_URL = "https://your-agent.example.com";
const API_KEY  = "pv_ak_…";

const headers = () => ({
  "Content-Type": "application/json",
  "X-API-Key": API_KEY,
});

async function startSession({ audioElement, onEvent, onStateChange }) {
  // 1. Microphone. Throws if the user denies permission.
  const micStream = await navigator.mediaDevices.getUserMedia({ audio: true });

  const pc = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });

  // 2. Play the agent's voice.
  pc.ontrack = (e) => {
    if (e.track.kind === "audio") audioElement.srcObject = e.streams[0];
  };

  // 3. Rule 1 — both transceivers.
  pc.addTransceiver(micStream.getAudioTracks()[0], { direction: "sendrecv" });
  pc.addTransceiver("video", { direction: "sendrecv" });

  // 4. Rules 2 and 3 — data channel plus keepalive.
  const channel = pc.createDataChannel("events");
  let keepalive = null;
  channel.onopen = () => {
    keepalive = setInterval(() => {
      if (channel.readyState === "open") channel.send("ping");
    }, 1000);
  };
  channel.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    onEvent?.(msg);
  };

  // 5. Network candidates. They can appear before we have a pc_id,
  //    so queue anything early and flush once the session exists.
  let pcId = null;
  const queued = [];
  const sendCandidate = (c) =>
    fetch(`${BASE_URL}/api/offer`, {
      method: "PATCH",
      headers: headers(),
      body: JSON.stringify({
        pc_id: pcId,
        candidates: [{
          candidate: c.candidate,
          sdp_mid: c.sdpMid,
          sdp_mline_index: c.sdpMLineIndex,
        }],
      }),
    }).catch(() => {});      // a lost candidate is not fatal
  pc.onicecandidate = (e) => {
    if (!e.candidate) return;
    if (pcId) sendCandidate(e.candidate);
    else queued.push(e.candidate);
  };

  // 6. Surface connection health to the UI.
  pc.onconnectionstatechange = () => {
    if (["disconnected", "failed", "closed"].includes(pc.connectionState)) {
      onStateChange?.("disconnected");
    } else if (pc.connectionState === "connected") {
      onStateChange?.("connected");
    }
  };

  // 7. Negotiate.
  await pc.setLocalDescription(await pc.createOffer());

  const res = await fetch(`${BASE_URL}/api/offer`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      sdp: pc.localDescription.sdp,
      type: pc.localDescription.type,
    }),
  });

  if (res.status === 401) throw new Error("Invalid or missing API key");
  if (!res.ok) throw new Error(`Could not start session (HTTP ${res.status})`);

  const answer = await res.json();
  pcId = answer.pc_id;
  await pc.setRemoteDescription(answer);

  for (const c of queued) await sendCandidate(c);
  queued.length = 0;

  // Return a handle the Stop button can use.
  return {
    stop() {
      if (keepalive) clearInterval(keepalive);
      try { channel.close(); } catch {}
      pc.close();
      for (const t of micStream.getTracks()) t.stop();   // clears the mic indicator
      audioElement.srcObject = null;
    },
  };
}
```

Wiring it to a button:

```js
let session = null;

startBtn.onclick = async () => {
  startBtn.disabled = true;
  try {
    session = await startSession({
      audioElement: document.getElementById("agent-audio"),
      onEvent: handleEvent,
      onStateChange: (s) => setStatus(s),
    });
    setStatus("listening");
  } catch (err) {
    setStatus(`error: ${err.message}`);
    startBtn.disabled = false;
  }
};
```

```html
<audio id="agent-audio" autoplay></audio>
```

> **Autoplay:** because the session starts from a click, the browser allows
> audio playback. If you ever start a session without a user gesture, the
> agent's voice will be muted by autoplay policy.

---

## 6. Step 4 — Render the live events

Each event arrives on the data channel as JSON with a `type` and a `data`
object. These are the ones worth handling:

| `type` | Meaning | Useful fields |
|---|---|---|
| `user-started-speaking` | The user began talking | — |
| `user-stopped-speaking` | The user stopped | — |
| `user-transcription` | What the user said, as text | `text`, `final` |
| `bot-started-speaking` | The agent began talking | — |
| `bot-stopped-speaking` | The agent finished | — |
| `bot-output` | A complete sentence from the agent | `text`, `spoken_status` |
| `bot-interrupted` | The user talked over the agent | — |
| `error` | Something went wrong server-side | `message` |

A minimal, correct handler:

```js
function handleEvent(msg) {
  switch (msg.type) {
    case "user-transcription":
      // May arrive as interim (final:false) then final. Commit the final one.
      if (msg.data?.final) addBubble("user", msg.data.text);
      break;

    case "bot-output":
      // Fires more than once per sentence as it is delivered.
      // Take the completed one to avoid duplicates.
      if (msg.data?.spoken_status === "completed") addBubble("agent", msg.data.text);
      break;

    case "user-started-speaking":  setStatus("listening"); break;
    case "bot-started-speaking":   setStatus("speaking");  break;
    case "bot-stopped-speaking":   setStatus("idle");      break;
    case "error":                  setStatus(`error: ${msg.data?.message}`); break;
  }
}
```

Two things that will bite you if you skip them:

- **`user-transcription` carries a `final` flag.** Partial results arrive
  with `final: false` and are superseded; only the `final: true` event is the
  finished sentence. Render partials greyed out if you want a live-typing
  effect, but only commit on `final: true`. (Whether partials arrive at all
  depends on the deployment — gating on `final` is correct either way.)
- **`bot-output` fires more than once for the same sentence** as it moves
  through delivery. Filter on `spoken_status === "completed"`, or you will
  render every reply two or three times.

The agent decides on its own when the user has finished a turn, and handles
being interrupted. You do not implement any of that — just reflect the
events in your UI.

---

## 7. Step 5 — The Stop button

```js
stopBtn.onclick = () => {
  session?.stop();
  session = null;
  setStatus("idle");
  startBtn.disabled = false;
};
```

`stop()` closes the connection and stops the microphone tracks. Stopping the
tracks matters: skip it and the browser keeps showing the "recording"
indicator after the user has ended the call.

Also call it on page unload, so sessions do not linger:

```js
window.addEventListener("pagehide", () => session?.stop());
```

---

## 8. Step 6 — Errors and edge cases

| Situation | How it reaches you | What to show |
|---|---|---|
| User denies the microphone | `getUserMedia` rejects (`NotAllowedError`) | "Microphone access is required" |
| No microphone present | `getUserMedia` rejects (`NotFoundError`) | "No microphone found" |
| Bad or missing API key | `POST /api/offer` → `401` | "Configuration error" — do not retry |
| Origin not allow-listed | fetch throws a CORS error | Contact the API operator |
| Wrong URL / service down | fetch throws, or `/healthz` fails | "Service unavailable" |
| Connection drops mid-call | `connectionState` → `failed`/`disconnected` | End the session, offer Retry |
| Connects, but no audio either way | usually Rule 1 (§4) | Check the video transceiver |
| Connects, audio fine, no events | Rule 2 (§4) | Check `createDataChannel` |
| Agent goes silent after ~3s | Rule 3 (§4) | Check the keepalive interval |

Two behaviours worth designing for:

**First connection after a restart can be slow.** The service may need a
warm-up period. Show a "connecting…" state rather than a spinner that looks
frozen, and allow up to ~30 seconds before timing out.

**Concurrent sessions are limited.** Every deployment has a ceiling on
simultaneous conversations. Ask your API operator what yours is, and queue or
refuse gracefully past it rather than letting quality degrade for everyone.

### Keeping the API key out of the browser

The key identifies your application, and a key in frontend JavaScript is
public. For anything beyond a prototype, put a thin proxy on your own backend:

```
browser  →  your backend  →  Voice Agent API
           (adds X-API-Key,
            checks your own user session)
```

Your backend forwards `POST` and `PATCH /api/offer` verbatim, adding the key
server-side. The browser code is unchanged except for `BASE_URL`, which now
points at your proxy. This also lets you enforce your own per-user limits.

---

## 9. Testing checklist

Work through these in order. Each one isolates a different failure.

- [ ] `GET /healthz` returns `200` from your UI's origin, with no CORS error
- [ ] Start button prompts for microphone permission
- [ ] Denying permission shows your error state, not a crash
- [ ] After allowing, `connectionState` reaches `connected`
- [ ] You hear the agent's voice through the `<audio>` element
- [ ] Speaking produces `user-transcription` events with your words
- [ ] The agent replies, and `bot-output` renders exactly once per sentence
- [ ] Talking over the agent interrupts it
- [ ] A session left open for 60+ seconds stays alive (keepalive works)
- [ ] Stop ends the call and clears the browser's recording indicator
- [ ] Start works again after Stop
- [ ] Reloading the page mid-call does not leave a session running
- [ ] A deliberately wrong API key shows your "configuration error" state

---

## 10. Quick reference

```
GET   {BASE_URL}/healthz
      → 200 {"status":"ok"}

POST  {BASE_URL}/api/offer
      X-API-Key: {key}
      { "sdp": "...", "type": "offer" }
      → 200 { "sdp": "...", "type": "answer", "pc_id": "..." }
      → 401 { "detail": "unknown or missing api key" }

PATCH {BASE_URL}/api/offer
      X-API-Key: {key}
      { "pc_id": "...", "candidates": [
          { "candidate": "...", "sdp_mid": "0", "sdp_mline_index": 0 } ] }
      → 200 { "status": "success" }
```

Client checklist: both transceivers · create the data channel · ping every
second · stop the mic tracks on Stop.
