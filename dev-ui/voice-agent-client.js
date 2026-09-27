/**
 * Synora voice-agent client — one call, from the button to the bill.
 *
 * Talks to the Synora API, never to the agent: the agent's key lives on the
 * server, and this file only ever sends the user's own access token. No SDK,
 * no build step, no dependencies — copy it into the frontend as it stands.
 *
 *   import { startVoiceCall } from "./voice-agent-client.js";
 *
 *   const call = await startVoiceCall({
 *     apiBase: "https://back.synora-ai.uz/api/v1",
 *     getToken: () => auth.freshAccessToken(),   // must return a valid token, refreshed if need be
 *     audioElement: document.querySelector("#agent-audio"),
 *     onTranscript: ({ role, text, final }) => render(role, text, final),
 *     onState: (state) => setStatus(state),
 *     onEnd: (bill, reason) => showSummary(bill, reason),
 *   });
 *   stopButton.onclick = () => call.stop();
 *
 * Call `startVoiceCall` from a click handler: a call started without a user
 * gesture has the agent's voice muted by the browser's autoplay policy.
 * `getToken` is called before every request, and a call can outlive an access
 * token — so it has to hand back a fresh one, not whatever was stored at login.
 *
 * What it does that the agent's reference client does not, and why:
 *
 * - **The three silent failures are handled.** A video transceiver is
 *   negotiated (no camera is opened) or no audio flows; the data channel is
 *   created here, because the agent never creates one; and it is pinged every
 *   second, because the agent drops a call whose channel goes quiet for three.
 * - **ICE candidates are batched and trimmed.** Candidates gathered while the
 *   offer is in flight are sent as one request once the call has an id, only
 *   for the transport the answer bundled everything onto, and later ones are
 *   coalesced for a few milliseconds — typically one request per call.
 * - **The heartbeat is the bill.** The server never sees the audio, so it bills
 *   from the agent's answer to the last heartbeat. The first one is sent the
 *   moment the media connects — a call that never connects costs nothing. A
 *   missed beat is retried quickly, inside the server's timeout, and a call
 *   whose heartbeats cannot get through is hung up here too, rather than left
 *   talking to an agent the server has stopped billing.
 * - **The ceiling is kept on both sides.** The server's `stop` ends the call,
 *   and so does this file's own timer at the ceiling, in case that `stop` never
 *   arrives.
 * - **A blip is not a hang-up.** WebRTC reports `disconnected` on a network
 *   switch and usually recovers by itself, so the call is given a few seconds
 *   before it is ended.
 * - **Hang-up survives the page closing.** The `DELETE` is sent with
 *   `keepalive`, from Stop and from `pagehide` alike, so a closed tab is billed
 *   to the second it closed rather than to a heartbeat timeout.
 */

const PING_INTERVAL_MS = 1000;
// How long a `disconnected` peer connection may take to come back on its own.
const RECONNECT_GRACE_MS = 5000;
// From the agent's answer to media flowing. ICE on one network takes well under
// a second; through two NATs it has been measured at over thirty. Past this it
// is not coming — the agent drops a peer that never connected about a minute
// after its offer anyway — and TURN is what is missing.
const CONNECT_TIMEOUT_MS = 45000;
// Coalescing window for trickle candidates after the first flush.
const CANDIDATE_BATCH_MS = 25;
const CANDIDATES_PER_REQUEST = 32;
const CANDIDATES_PER_CALL = 64;
// A heartbeat is a tiny request: give up on one quickly and try again, so a
// single stalled socket cannot eat the server's whole timeout.
const HEARTBEAT_REQUEST_TIMEOUT_MS = 5000;
const HEARTBEAT_RETRY_MS = 2000;
// How long `stop()` waits for the hang-up's bill before reporting without it.
const HANGUP_WAIT_MS = 5000;

/** A refusal from the API or the browser, with the API's stable `code`. */
export class VoiceCallError extends Error {
  constructor(message, { code = "voice_call_failed", status = 0, retryAfter = null, data = null, sessionId = null } = {}) {
    super(message);
    this.name = "VoiceCallError";
    this.code = code;
    this.status = status;
    this.retryAfter = retryAfter;
    this.data = data;
    /** Set when the server had already opened the call; it has been hung up. */
    this.sessionId = sessionId;
  }
}

function endpoint(apiBase, path) {
  return String(apiBase).replace(/\/+$/, "") + path;
}

async function request(fetchImpl, apiBase, token, method, path, { body, signal, keepalive } = {}) {
  const headers = { Authorization: `Bearer ${token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetchImpl(endpoint(apiBase, path), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      keepalive,
    });
  } catch (error) {
    if (error?.name === "AbortError" || error?.name === "TimeoutError") throw error;
    throw new VoiceCallError("The Synora API could not be reached.", { code: "network_error" });
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!response.ok) {
    throw new VoiceCallError(data?.detail || `HTTP ${response.status}`, {
      code: data?.code || "http_error",
      status: response.status,
      retryAfter: data?.retryAfter ?? null,
      data,
    });
  }
  return data;
}

/** `GET /voice/config`: ICE servers, heartbeat cadence, and what a call costs. */
export async function fetchVoiceConfig({ apiBase, getToken, signal, fetch: fetchImpl = globalThis.fetch.bind(globalThis) } = {}) {
  return request(fetchImpl, apiBase, await getToken(), "GET", "/voice/config", { signal });
}

function microphoneError(error) {
  if (error?.name === "NotAllowedError" || error?.name === "SecurityError") {
    return new VoiceCallError("Microphone access is required for a call.", { code: "microphone_denied" });
  }
  if (error?.name === "NotFoundError" || error?.name === "OverconstrainedError") {
    return new VoiceCallError("No microphone was found.", { code: "microphone_missing" });
  }
  return new VoiceCallError(error?.message || "The microphone could not be opened.", { code: "microphone_failed" });
}

/** The first mid of the answer's BUNDLE group: the one transport ICE will use. */
function bundledMid(sdp) {
  const group = /^a=group:BUNDLE (.+)$/m.exec(sdp || "");
  return group ? group[1].trim().split(/\s+/)[0] : null;
}

/** Resolve with `promise`, or reject as soon as `signal` aborts — without cancelling `promise`. */
function unlessAborted(promise, signal) {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(new DOMException("aborted", "AbortError"));
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(new DOMException("aborted", "AbortError"));
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => { signal.removeEventListener("abort", onAbort); resolve(value); },
      (error) => { signal.removeEventListener("abort", onAbort); reject(error); },
    );
  });
}

/**
 * Start a call. Resolves once the agent has answered; the media connects a
 * moment later, and `onState("connected")` says when.
 *
 * @param {object} options
 * @param {string} options.apiBase            e.g. "https://back.synora-ai.uz/api/v1"
 * @param {() => string|Promise<string>} options.getToken  a valid access token, refreshed if need be
 * @param {HTMLAudioElement} options.audioElement          plays the agent's voice
 * @param {(event: object) => void} [options.onEvent]      every data-channel event, raw
 * @param {(t: {role: "user"|"agent", text: string, final: boolean, interrupted?: boolean}) => void} [options.onTranscript]
 *        `interrupted` marks agent text the user talked over; it may not all have been said.
 * @param {(state: string) => void} [options.onState]
 *        "connecting" | "connected" | "listening" | "speaking" | "idle" | "warning" | "ended"
 * @param {(pulse: object) => void} [options.onHeartbeat]  every heartbeat answer
 * @param {(bill: object|null, reason: string) => void} [options.onEnd]  once, however a started call ended
 * @param {object} [options.config]           a `GET /voice/config` answer already in hand
 * @param {MediaTrackConstraints} [options.microphone]
 * @param {AbortSignal} [options.signal]      cancels a call still connecting
 * @param {typeof fetch} [options.fetch]      for logging or tests; defaults to the global one
 */
export async function startVoiceCall(options) {
  const {
    apiBase, getToken, audioElement, config: knownConfig, signal,
    onEvent = () => {}, onTranscript = () => {}, onState = () => {}, onEnd = () => {}, onHeartbeat = () => {},
    microphone = { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    fetch: fetchImpl = globalThis.fetch.bind(globalThis),
  } = options;

  // A callback that throws must never stop a hang-up: the hold would then wait
  // for the server's timeout instead of coming back now.
  const safe = (fn) => (...args) => { try { fn(...args); } catch (error) { console.error(error); } };
  const emit = { event: safe(onEvent), transcript: safe(onTranscript), state: safe(onState), end: safe(onEnd), heartbeat: safe(onHeartbeat) };

  // The last token seen, for the one request that cannot wait for a promise:
  // the `pagehide` hang-up has to be sent synchronously or not at all.
  let token = await getToken();
  const freshToken = async () => (token = (await getToken()) || token);
  const api = async (method, path, init) => request(fetchImpl, apiBase, await freshToken(), method, path, init);

  const config = knownConfig || (await fetchVoiceConfig({ apiBase, getToken, signal, fetch: fetchImpl }));
  if (!config.available) {
    throw new VoiceCallError("Voice calls are not available on this server.", { code: "voice_agent_not_configured" });
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new VoiceCallError("The microphone needs a secure page (https, or localhost).", { code: "insecure_origin" });
  }

  emit.state("connecting");
  let mic;
  try {
    mic = await navigator.mediaDevices.getUserMedia({ audio: microphone, video: false });
  } catch (error) {
    throw microphoneError(error);
  }
  if (signal?.aborted) {
    // Cancelled while the permission prompt was up: nothing is open yet, so
    // nothing is sent — no hold, no pipeline built on the agent for nobody.
    for (const track of mic.getTracks()) track.stop();
    throw new DOMException("aborted", "AbortError");
  }

  let pc;
  let channel;
  try {
    pc = new RTCPeerConnection({ iceServers: config.ice_servers });
    channel = pc.createDataChannel("events", { ordered: true });
  } catch (error) {
    // A malformed ICE server entry throws here, after the microphone is open.
    for (const track of mic.getTracks()) track.stop();
    throw new VoiceCallError(error?.message || "The ICE server configuration is invalid.", { code: "voice_ice_config_invalid" });
  }

  const interval = (config.heartbeat_interval_seconds || 15) * 1000;
  const timeout = (config.heartbeat_timeout_seconds || 45) * 1000;
  let sessionId = null;
  let posted = false; // whether the offer ever left, so an abort knows if there is a call to hang up
  let ended = false;
  let startFailed = false; // a start that throws reports through the throw, not `onEnd`
  let everConnected = false;
  let warned = false;
  let lastBeatOk = 0;
  let relayed = 0;
  let mid = null;
  const timers = { ping: 0, heartbeat: 0, reconnect: 0, connect: 0, candidates: 0, ceiling: 0 };
  const pending = [];
  const unspoken = []; // agent sentences announced (`new`) and not yet completed
  let flushing = null;
  let resolveDone;
  const done = new Promise((resolve) => (resolveDone = resolve));

  const clearTimers = () => {
    clearInterval(timers.ping);
    for (const key of ["heartbeat", "reconnect", "connect", "candidates", "ceiling"]) clearTimeout(timers[key]);
  };

  const release = () => {
    clearTimers();
    try { channel.close(); } catch { /* already closed */ }
    try { pc.close(); } catch { /* already closed */ }
    for (const track of mic.getTracks()) track.stop(); // clears the recording indicator
    if (audioElement) audioElement.srcObject = null;
    window.removeEventListener("pagehide", onPageHide);
  };

  // --- hanging up ---------------------------------------------------------
  async function finish(reason) {
    if (ended) return done;
    ended = true;
    release();
    if (!startFailed) emit.state("ended");
    let bill = null;
    if (sessionId) {
      // Sent now, with the token already in hand, and `keepalive`: a Stop
      // followed at once by a navigation or a closed tab must still land, and
      // awaiting a token refresh first is how it would not. A 401 — the cached
      // token expired — is retried once with a fresh one.
      const path = `/voice/sessions/${sessionId}`;
      const hangUp = request(fetchImpl, apiBase, token, "DELETE", path, { keepalive: true })
        .catch(async (error) => {
          if (error?.status !== 401) throw error;
          return request(fetchImpl, apiBase, await freshToken(), "DELETE", path, { keepalive: true });
        })
        .catch(() => null); // the server ends and bills the call on its own anyway
      // Bounded: `heartbeat_lost` fires exactly when the API is unreachable,
      // and the UI must not wait on a request that may hang for minutes. The
      // request itself is left to land.
      bill = await Promise.race([hangUp, new Promise((resolve) => setTimeout(() => resolve(null), HANGUP_WAIT_MS))]);
    }
    if (!startFailed) emit.end(bill, reason);
    resolveDone(bill);
    return done;
  }

  function onPageHide() {
    if (ended) return;
    ended = true;
    release();
    if (sessionId) {
      // Fire and forget: the page is going, and only `keepalive` outlives it.
      fetchImpl(endpoint(apiBase, `/voice/sessions/${sessionId}`), {
        method: "DELETE", keepalive: true, headers: { Authorization: `Bearer ${token}` },
      }).catch(() => {});
    }
    resolveDone(null);
  }
  window.addEventListener("pagehide", onPageHide);

  // --- the agent's voice and events --------------------------------------
  pc.ontrack = (event) => {
    if (event.track.kind !== "audio" || !audioElement) return;
    audioElement.srcObject = event.streams[0] || new MediaStream([event.track]);
    audioElement.play?.().catch(() => { /* autoplay: the click already happened */ });
  };

  channel.onopen = () => {
    clearInterval(timers.ping);
    timers.ping = setInterval(() => {
      if (channel.readyState === "open") channel.send("ping");
    }, PING_INTERVAL_MS);
  };
  channel.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    emit.event(message);
    const data = message.data || {};
    switch (message.type) {
      case "user-transcription":
        // Partials arrive as `final: false` and are superseded; render them
        // greyed if you like, commit only the final one.
        if (data.text) emit.transcript({ role: "user", text: data.text, final: Boolean(data.final) });
        break;
      case "bot-output":
        // Fires once as `new` and once as `completed` per sentence, so only
        // the completed one is committed. A sentence the user talked over never
        // completes — the live agent sends `new`, then `bot-interrupted`, and
        // nothing more — so the `new` ones are kept until one or the other.
        if (!data.text) break;
        if (data.spoken_status === "new") unspoken.push(data.text);
        if (data.spoken_status === "completed") {
          const at = unspoken.indexOf(data.text);
          if (at !== -1) unspoken.splice(at, 1);
          emit.transcript({ role: "agent", text: data.text, final: true });
        }
        break;
      case "bot-interrupted":
        // What the agent had started saying when it was cut off: committed,
        // and marked, so the transcript does not silently lose the greeting.
        if (unspoken.length) {
          emit.transcript({ role: "agent", text: unspoken.join(" "), final: true, interrupted: true });
          unspoken.length = 0;
        }
        emit.state("listening");
        break;
      case "user-started-speaking": emit.state("listening"); break;
      case "bot-started-speaking": emit.state("speaking"); break;
      case "bot-stopped-speaking": emit.state("idle"); break;
      default: break;
    }
  };

  // --- trickle ICE, batched -----------------------------------------------
  async function flushCandidates() {
    if (!sessionId || ended || flushing) return;
    // Only the bundled transport's candidates reach ICE; the rest would spend
    // the call's 64 on nothing.
    const wanted = pending.splice(0).filter((c) => mid === null || c.sdpMid === mid || c.sdpMid == null);
    const batch = wanted.slice(0, Math.min(CANDIDATES_PER_REQUEST, CANDIDATES_PER_CALL - relayed));
    pending.unshift(...wanted.slice(batch.length));
    if (!batch.length) {
      if (relayed >= CANDIDATES_PER_CALL) pending.length = 0; // the call's budget is spent
      return;
    }
    flushing = api("POST", `/voice/sessions/${sessionId}/candidates`, { body: { candidates: batch } })
      .then((answer) => { relayed += answer?.relayed ?? batch.length; })
      .catch((error) => {
        if (error.code === "voice_call_gone") finish("agent_ended");
        else if (error.code === "voice_call_ended") finish("server_stop");
        else if (error.code === "voice_candidates_exhausted") relayed = CANDIDATES_PER_CALL;
        // Anything else: a lost candidate is not fatal, ICE has others.
      });
    await flushing;
    flushing = null;
    if (pending.length && relayed < CANDIDATES_PER_CALL) flushCandidates();
  }
  pc.onicecandidate = (event) => {
    if (!event.candidate || !event.candidate.candidate) return;
    pending.push(event.candidate.toJSON());
    if (!sessionId) return; // flushed in one go once the offer is answered
    clearTimeout(timers.candidates);
    timers.candidates = setTimeout(flushCandidates, CANDIDATE_BATCH_MS);
  };

  // --- staying alive, and the bill ----------------------------------------
  async function beat() {
    if (ended) return;
    try {
      const pulse = await api("POST", `/voice/sessions/${sessionId}/heartbeat`, {
        signal: AbortSignal.timeout?.(HEARTBEAT_REQUEST_TIMEOUT_MS),
      });
      if (ended) return;
      lastBeatOk = Date.now();
      emit.heartbeat(pulse);
      if (pulse.action === "stop") return void finish("server_stop");
      if (pulse.action === "warn" && !warned) { warned = true; emit.state("warning"); }
      timers.heartbeat = setTimeout(beat, (pulse.next_heartbeat_seconds || interval / 1000) * 1000);
    } catch (error) {
      if (ended) return;
      if (error?.status === 404) return void finish("not_found");
      // The server gives up on a call it has not heard from in `timeout`, and
      // bills it to the last beat it did hear. Past that point the call is no
      // longer being billed, so it is hung up here rather than left running.
      // Retried every couple of seconds rather than a whole interval later, so
      // one lost request never costs the call: the server waits `timeout`.
      if (Date.now() - lastBeatOk >= timeout) return void finish("heartbeat_lost");
      timers.heartbeat = setTimeout(beat, HEARTBEAT_RETRY_MS);
    }
  }

  pc.onconnectionstatechange = () => {
    const state = pc.connectionState;
    if (state === "connected") {
      clearTimeout(timers.reconnect);
      clearTimeout(timers.connect);
      if (!everConnected) {
        everConnected = true;
        lastBeatOk = Date.now();
        emit.state("connected");
        beat(); // the first heartbeat is what makes the call billable
      }
    } else if (state === "disconnected") {
      clearTimeout(timers.reconnect);
      timers.reconnect = setTimeout(() => {
        if (pc.connectionState !== "connected") finish("connection_lost");
      }, RECONNECT_GRACE_MS);
    } else if (state === "failed") {
      finish(everConnected ? "connection_lost" : "connection_failed");
    }
  };

  // --- negotiating ----------------------------------------------------------
  try {
    // Both transceivers: the second is what the agent needs to send any audio
    // at all, and it never touches the camera.
    pc.addTransceiver(mic.getAudioTracks()[0], { direction: "sendrecv", streams: [mic] });
    pc.addTransceiver("video", { direction: "sendrecv" });
    await pc.setLocalDescription(await pc.createOffer());

    // Not given the signal: an offer abandoned mid-flight is still answered by
    // the server, and a call nobody knows the id of holds its line until the
    // server times it out. So the POST runs to completion, and an abort that
    // lands in the meantime hangs the answered call up with its id in hand.
    // The token first, then the last look at the signal, then the POST in the
    // same step as `posted`: an abort while a token refresh is in flight must
    // not let the offer out afterwards for a start that has already rejected.
    const offerToken = await freshToken();
    if (signal?.aborted) throw new DOMException("aborted", "AbortError");
    posted = true;
    const offered = request(fetchImpl, apiBase, offerToken, "POST", "/voice/sessions", {
      body: { sdp: pc.localDescription.sdp, type: pc.localDescription.type },
    });
    offered.then((answer) => { sessionId = answer.ai_session_id; }, () => {});
    const answer = await unlessAborted(offered, signal);
    sessionId = answer.ai_session_id;
    mid = bundledMid(answer.sdp);
    await pc.setRemoteDescription({ type: "answer", sdp: answer.sdp });
    if (signal?.aborted) throw new DOMException("aborted", "AbortError");
    flushCandidates();
    timers.connect = setTimeout(() => {
      if (!everConnected) finish("connection_failed");
    }, CONNECT_TIMEOUT_MS);
    // The ceiling, on this side too: a `stop` the server sends to a heartbeat
    // that never arrives would otherwise leave the call running past it.
    timers.ceiling = setTimeout(() => finish("max_duration"), (config.max_session_seconds || 600) * 1000);
  } catch (error) {
    // Anything that fails before the answer is applied ends the call here, and
    // a call the server has already opened is hung up so its hold comes back.
    startFailed = true;
    if (error?.name === "AbortError" && !sessionId && posted) hangUpWhenAnswered();
    const opened = sessionId;
    await finish(error?.name === "AbortError" ? "cancelled" : "start_failed");
    if (error?.name === "AbortError") throw Object.assign(error, { sessionId: opened });
    throw error instanceof VoiceCallError
      ? Object.assign(error, { sessionId: opened })
      : new VoiceCallError(error?.message || "The call could not be started.", { sessionId: opened });
  }

  function hangUpWhenAnswered() {
    // Aborted with the POST still in flight: `finish` runs before the answer
    // exists, so the answered call is hung up the moment its id arrives.
    const wait = setInterval(() => {
      if (!sessionId) return;
      clearInterval(wait);
      api("DELETE", `/voice/sessions/${sessionId}`, { keepalive: true }).catch(() => {});
    }, 250);
    setTimeout(() => clearInterval(wait), 120000);
  }

  return {
    get id() { return sessionId; },
    get ended() { return ended; },
    get muted() { return mic.getAudioTracks().every((track) => !track.enabled); },
    /** Resolves with the bill — `GET /voice/sessions/{id}`'s shape — or null. */
    done,
    /** Hang up. Idempotent; resolves with the bill. */
    stop: () => finish("hangup"),
    /** Mute the microphone without ending the call. */
    mute(muted = true) { for (const track of mic.getAudioTracks()) track.enabled = !muted; },
  };
}
