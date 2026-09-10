# RabbitMQ, and where it is deliberately absent

There is one GPU behind the speech service. Concurrency against a single card
is not a throughput dial, it is a cliff: past the point where the model's
working set stops fitting, everything in flight gets slower together, and the
failures arrive as timeouts rather than as a queue that is visibly long. So the
batch path does not call upstream when the request arrives — it enqueues, and
`RABBITMQ_PREFETCH` is the number of batch items allowed in flight against the
card at once.

**That number is the reason the broker exists.** It is the admission control,
and it is the only knob worth turning. "Async is nicer" is not a reason for a
broker; the reason is that a handful of consumers with a small prefetch is a
load the card can hold, and two hundred concurrent HTTP handlers is not.

Raising `RABBITMQ_PREFETCH` does not make the card faster. It moves the queue
out of RabbitMQ, where it can be inspected, reordered, delayed and cancelled,
and into the card's own scheduler, where none of that is true. Treat it as a
ceiling, not a throughput setting.

The rest of this page is what runs through the queue, what deliberately does
not, and how to operate it.

---

## What goes through it

Exactly two kinds of message, both about batch jobs, both carrying nothing but
a job id.

| Routing key | Published by | Consumed by | Does |
| --- | --- | --- | --- |
| `tts.batch.submit` | `POST /api/v1/tts/batch`, after the row is committed | The worker | `submit_job` — reads `items_json`, hands the payload upstream, stores `upstream_job_id` |
| `tts.batch.poll` | The worker itself, delayed | The worker | `refresh_job` — asks upstream where the job is, settles it if it has finished |

Both handlers are functions in `app/services/ai/tts_batch_service.py` that the
HTTP routes call too. The worker owns no billing of its own, on purpose: if it
did, a deployment would charge different amounts depending on whether RabbitMQ
happened to be running, and nobody would ever find that bug from an invoice.

The submit message is published **after** the creating transaction commits. A
worker that picked it up in the microsecond before the row landed would find no
job and dead-letter one that was about to exist.

### Topology

Declared by `declare_topology()` in `app/core/broker.py`, called by both the
publisher and the worker from the same function — because the alternative is a
publisher declaring a queue with one set of arguments and a worker declaring it
with another, which RabbitMQ answers with a `406` that closes the channel under
whoever arrived second. That failure is easy to cause, ugly to read, and
impossible to reproduce locally once the queue already exists.

```
                POST /api/v1/tts/batch
                          │
             price · hold · commit the row
                          │
                publish "tts.batch.submit"
                          ▼
   ┌───────────────────────────────────────────────┐
   │  exchange  synora.tts         topic, durable  │
   └──────┬──────────────────────────────┬─────────┘
   tts.batch.submit                tts.batch.poll
          │                               │
          ▼                               ▼
 ┌────────────────────────┐   ┌────────────────────────┐
 │ synora.tts.batch.submit│   │ synora.tts.batch.poll  │
 │ x-dead-letter-exchange │   │ x-dead-letter-exchange │
 │        = synora.dlx    │   │        = synora.dlx    │
 └───────────┬────────────┘   └────────────┬───────────┘
             └───────────┬─────────────────┘
                         │  prefetch = RABBITMQ_PREFETCH
                         ▼
            python -m app.workers.tts_batch
                         │
       submit_job / refresh_job ──────────────► the GPU box
                         │
   ┌─────────────────────┼──────────────────────┐
   │ still running       │ terminal             │ permanent failure
   ▼                     ▼                      ▼
publish, delayed        ack             nack(requeue=False)
   │                                            │
   ▼                                            ▼
┌─────────────────────────────┐    ┌────────────────────────────┐
│ synora.tts.batch.poll       │    │ exchange  synora.dlx       │
│               .delay.10     │    │           direct, durable  │
│ x-message-ttl          10000│    └─────────────┬──────────────┘
│ nothing consumes this queue │       same routing key it died on
│ x-dead-letter-exchange      │                  ▼
│              synora.tts     │    ┌────────────────────────────┐
│ x-dead-letter-routing-key   │    │ synora.tts.dlq             │
│              tts.batch.poll │    │ nothing consumes this one  │
└──────────────┬──────────────┘    └────────────────────────────┘
               │
               └──► back into synora.tts when the TTL expires
```

Every name carries `RABBITMQ_PREFIX` (default `synora.`), because the box hosts
several unrelated projects and an unnamespaced `tts.batch.poll` is somebody
else's queue waiting to happen. Routing keys are deliberately *not* prefixed:
the exchange they travel through is already namespaced, and repeating it inside
the key would only make the bindings harder to read.

The dead-letter exchange is `direct` rather than `fanout`. The work queues set
no `x-dead-letter-routing-key`, so a dead letter keeps the key it died under,
and the DLQ's bindings double as the list of what is allowed to land in it — a
stray key going unroutable is a better outcome than a dead-letter queue that
quietly accepts anything.

### Delayed retry without the plugin

Polling a job means "ask again in ten seconds". The obvious answer is the
delayed-message-exchange plugin, which is one more thing to install on the box,
remember at the next upgrade, and discover missing at 3am.

The two primitives RabbitMQ already ships with compose into a scheduler.
A message whose `x-message-ttl` expires is dead-lettered — so **a queue nothing
consumes, whose dead-letter target is the work exchange, is a waiting room with
a timer on it.** `broker.publish(..., delay_seconds=10)` declares
`synora.tts.batch.poll.delay.10`, publishes into it through the default
exchange, and ten seconds later the broker itself moves the message back onto
`synora.tts` under `tts.batch.poll`.

The delay is part of the queue name for a reason that bites if you change it:
the queue's fixed `x-dead-letter-routing-key` is the only thing that gets the
message home, so one shared `…delay.10` queue across both work keys would
deliver delayed submits into the poll queue, silently. Each `(routing key,
delay)` pair gets its own waiting room, and the set stays small because the
delays are a fixed tuple — `(10, 60, 300)` for retries, plus
`TTS_BATCH_POLL_SECONDS` for the ordinary poll.

### Acknowledgement policy

This is the whole of the worker's design, and it is written to be read from a
log at 3am.

| Outcome | Action | Why |
| --- | --- | --- |
| Job reached a terminal state | `ack` | Settled. Nothing left to do |
| Job still running | Publish the next poll **delayed, then** `ack` | If the publish fails, the current message is requeued instead — a job must never lose its only remaining timer |
| Permanent failure — upstream refused the payload, unparseable message, job id that is not a job | `nack(requeue=False)` → DLQ | Requeueing would redeliver the same bytes to the same validator forever. The service has already settled the job and released the hold on every one of these branches |
| Transient failure — upstream unreachable, saturated (`429`), briefly unconfigured | Re-publish with a delay of 10 s, then 60 s, then 300 s; then DLQ | An immediate redelivery of a message that failed because "the GPU is busy" is precisely the load that made it busy |
| The follow-up publish itself fails | Pause, then requeue the message in hand — three times for **that message**, then DLQ | A broker that consumes happily while refusing publishes is a real state: a delay queue that already exists with different arguments answers the declaration with a `406` and closes the channel, while consuming carries on. A bare requeue against that is a hot loop; the pause is what makes it a retry |

Both counters count *consecutive* failures, and both belong to the message
rather than to the row: a poll that succeeds publishes a fresh message with no
attempts on it, so a job that is merely slow never exhausts a budget meant for a
job that is broken.

**The publish budget is per message and not per worker, and that distinction is
load-bearing.** `RABBITMQ_PREFETCH` handlers run concurrently, so a counter they
share is spent by whichever messages happen to be in flight when the broker
starts refusing — the third of them dead-lettered in the same event-loop turn as
the first, a job's only future timer thrown away 0.3 seconds into an outage that
had not yet cost anyone a retry. The worker-wide streak exists, but it is a
**circuit breaker** rather than a discard: past three consecutive failures
across handlers, each failing handler holds its slot for 300 s instead of 10 s,
and because prefetch counts unacked messages, every parked slot is one the
broker cannot refill. Consumption stops for as long as the outage lasts without
anyone cancelling a consumer, and resumes on the first publish that works.

The backstop for a job that never finishes is neither counter: it is
`TTS_BATCH_MAX_POLL_SECONDS`, which `refresh_job` turns into a settlement and an
`expired` state. Note what that means for a message that *is* discarded — the
job loses its timer, not its recoverability. Nothing polls it any more, so it
advances the next time somebody reads `GET /tts/batch/{job_id}`, and if nobody
ever does, its hold stands: `POST /admin/reconcile` does not reap a session a
live batch job points at. See [Running without a broker](#running-without-a-broker).

Nothing consumes the DLQ. It is a place for a human to look, not a retry tier.

### Shutting down

A signal cancels the consumers and then waits `DRAIN_TIMEOUT_SECONDS` (20) for
the handlers already running; whatever is still going when that expires is
cancelled and counted in the log. The bound is not a courtesy. A handler blocked
on `tts_client` holds its prefetch slot for up to `TTS_READ_TIMEOUT_SECONDS`
(300), while Kubernetes' default `terminationGracePeriodSeconds` is 30 and
`docker stop`'s is 10 — so an unbounded drain does not give the work more time
to finish, it guarantees the SIGKILL lands mid-handler with the broker
connection, the HTTP pool and the engine all dropped uncleanly.

An abandoned message was never acked, so the broker redelivers it, and both
handlers are built for that: `submit_job` sends our own job id upstream as its
idempotency key, and `refresh_job` is a poll.

Signals escalate, one step each: the first drains, the second cancels the
handlers in flight, and the third restores the signal's default disposition and
re-raises it. That ladder exists because the usual lever is gone —
`loop.add_signal_handler` replaces Python's SIGINT disposition, so Ctrl-C stops
raising `KeyboardInterrupt` for as long as this worker runs, and an operator
whose drain is stuck behind a 300-second upstream read would otherwise have
nothing but `kill -9`.

---

## Where it is deliberately not used

Two refusals, and both are load-bearing.

### The synthesis streaming path

`POST /api/v1/tts/speech` never touches the broker.

Upstream delivers first audio in about **100 ms**, and our end-to-end budget for
the first byte is **180 ms**. A broker round trip plus a worker pickup spends
most of that before any audio moves, and buys nothing back: the caller is
already blocked on the answer. Queueing here would only relocate the waiting to
a place it cannot be streamed from — the HTTP connection is held open either
way, so the "async" version is the same wait with an extra hop and a second
process that can be down.

There is a subtler reason. The streaming response settles in the `finally` of
the generator that relays it, under `asyncio.shield`, on a database session it
opens itself — because a client disconnect must still produce a charge for the
full text. Handing that settlement to a worker would mean the disconnect and
the charge live in different processes, and the thing that guarantees the
charge happens becomes a message rather than a `finally`.

Batch is the opposite case in every respect: nothing is waiting on the first
byte, the work runs for minutes or hours, and the number of items in flight is
exactly the thing that needs limiting. That is the line between the two paths,
and it is not "streaming is hard".

### The wallet ledger

No hold, debit, release or usage event is ever published anywhere.

A hold, the debit that settles it, and the usage event that justifies the debit
commit inside **one Postgres transaction**, and `wallet_repo` is the only module
allowed to write a balance — a test walks the source tree to keep that true.
The invariant that falls out is the one the whole billing system rests on:

```
wallets.<bucket>_micros == SUM(ledger_entries.amount_micros for that bucket)
```

Handing any part of that to a worker converts an invariant the database
enforces into a message that may be delivered twice, delivered late, or not at
all. Idempotency keys make a replayed message harmless; they do not make a
*missing* one harmless, and no amount of key discipline buys back the ability
to answer "is this balance right?" by reading one row.

The practical version: a debit behind a queue is not a debit, it is a promise
of a debit. A user could spend credit that a still-queued message was going to
take away, and `POST /admin/reconcile` would report a wallet that disagrees with
its ledger — with the explanation sitting in a broker nobody thought to check.
Money moves synchronously, or it is not money.

---

## Running without a broker

`RABBITMQ_URL` empty is a **supported configuration**, not a degraded one.

`broker.publish()` returns `False` when there is no broker configured and when a
publish failed, timed out or was refused — and every caller is required to have
a path that still works. This is exactly the contract `app/core/cache.py` sets
for Redis, for the same reason and with the same shape: a `Protocol`, a
`NullBroker`, a real one that downgrades every failure to the null answer.

For batch submission that path is to submit inline. Upstream's own
`POST /v1/batch` answers in milliseconds — it enqueues on its side too — so the
request that created the job can hand it over itself, and polling happens when
somebody reads `GET /tts/batch/{job_id}`. A queue that is merely absent must
not become an outage.

The test suite has no RabbitMQ, exactly as it has no Redis, which is what keeps
that claim honest: the inline path is the one that is exercised on every run,
not merely intended. A fallback nobody runs is a fallback that does not work.

What you give up without a worker:

- Nothing limits how many batch jobs hit the card at once except how many
  people press the button.
- **A job nobody ever reads again never advances, and nothing else releases its
  hold.** `refresh_job` is the only code that can reach
  `TTS_BATCH_MAX_POLL_SECONDS`, and it runs on a poll or on a read — there is no
  sweeper behind it. `POST /admin/reconcile` is not the backstop here either:
  its reaper deliberately skips any metered session a non-terminal batch job
  still points at, because the session's deadline starts when the session was
  opened and the job's when upstream accepted it, and a reaper acting on the
  earlier of the two was closing the sessions of healthy jobs and settling them
  at zero. The batch lifecycle owns its own deadline, alone.
- The creating request waits for upstream's `POST /v1/batch` — milliseconds,
  but on the request path.

The first and the third are traffic questions. The second is a correctness one
and is listed under **Still to do** in the [README](../README.md#still-to-do):
until something sweeps jobs on a timer, "poll your jobs to the end" is part of
the contract rather than advice.

---

## Runbook

### Local

```bash
brew install rabbitmq && brew services start rabbitmq       # macOS
# Debian:  apt install rabbitmq-server && systemctl enable --now rabbitmq-server
rabbitmq-plugins enable rabbitmq_management                  # :15672, guest/guest

export RABBITMQ_URL=amqp://guest:guest@localhost:5672/
uvicorn app.main:app --reload            # terminal 1 — publishes
python -m app.workers.tts_batch          # terminal 2 — consumes
```

The API says which mode it came up in, once, at startup:

```
RabbitMQ enabled (prefix=synora., prefetch=4)
No RABBITMQ_URL: batch jobs run inline
```

and the worker says what it is consuming:

```
Consuming tts.batch.submit and tts.batch.poll (prefix=synora., prefetch=4)
```

**The worker refuses to start without `RABBITMQ_URL`**, with a message saying
where the work goes instead. That is not pedantry: with no broker the API is
already correct, so a worker started against an unconfigured broker would not be
degraded — it would be a second, silent copy of the same code connected to
nothing.

It also refuses to start when `TTS_BASE_URL` and `TTS_API_KEY` are not both set,
because every job it picked up would fail at its first upstream call.

### On the server

The worker is a second unit next to `synora-api.service`, running as the same
`synora` user out of the same directory — settings are read from
`/opt/synora-backend/.env` relative to the working directory, so there is no
`EnvironmentFile` to keep in sync.

```ini
# /etc/systemd/system/synora-tts-worker.service
[Unit]
Description=Synora TTS batch worker
After=network-online.target rabbitmq-server.service
Wants=network-online.target

[Service]
User=synora
WorkingDirectory=/opt/synora-backend
ExecStart=/opt/synora-backend/.venv/bin/python -m app.workers.tts_batch
Restart=always
RestartSec=5
KillSignal=SIGTERM
# The worker bounds its own drain at DRAIN_TIMEOUT_SECONDS (20) and then
# cancels what is left, so a healthy SIGTERM is over in well under half a
# minute. This number is deliberately far above that: it is the outer guard for
# a process wedged somewhere the bound cannot reach, and it exists so that
# systemd is never the thing that decides a drain is over. A message killed
# between the upstream call and the ack is a job submitted twice, and one killed
# mid-settlement is a hold nothing releases.
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now synora-tts-worker
journalctl -u synora-tts-worker -f
```

Deploys carry it automatically — `app` is already in the release tarball — but
a code change needs `systemctl restart synora-tts-worker` alongside the API
restart, or the worker keeps running the old billing code against the new
schema.

### What to watch

```bash
# Depths and consumers. `consumers 0` on a work queue is the alarm: messages
# are piling up and nothing is reading them.
rabbitmqctl list_queues name messages consumers | grep '^synora\.'

# The one number worth alerting on.
rabbitmqctl list_queues name messages | grep 'synora.tts.dlq'
```

| Symptom | Means | Do |
| --- | --- | --- |
| `synora.tts.dlq` above zero and growing | Jobs are failing permanently, exhausting three handler retries, or exhausting three publish retries | Read the worker log for `dead-lettering`, and note which of the two it was. A **permanent or exhausted handler** failure has already settled its job and released the hold, so the DLQ entry is the record rather than the recovery. A **publish** failure has not: that job is untouched and simply has no timer left, so it advances only when somebody reads it. Those are the ones to chase — `GET /tts/batch/{job_id}` on each, or a `DELETE` if the owner has given up |
| `synora.tts.batch.submit` deep, `consumers 0` | The worker is down. Nothing is being submitted upstream | `systemctl status synora-tts-worker`. Jobs stay `queued` with their text and their hold; they recover on restart, or when somebody reads them |
| `synora.tts.batch.poll` deep with a live consumer | Upstream is slow or the prefetch is too small for the job mix | Check `tts_batch_*` log lines before touching `RABBITMQ_PREFETCH` |
| `RabbitMQ unavailable during publish …` in the API log | The broker is down. Batch creation has silently fallen back to inline submission | Fix the broker. Nothing is lost; jobs advance when read. The line is logged once per outage, not once per message |
| `RabbitMQ is accepting messages again` | It recovered | Nothing |
| Many `tts_batch_expired` lines | Jobs are outliving `TTS_BATCH_MAX_POLL_SECONDS` | Those sessions are flagged `disputed` and settled at the last known usage — a support question about refunds, and a signal the card is oversubscribed |

Worth knowing before reading the log: a delayed message spends its wait in a
`…delay.N` queue, so `synora.tts.batch.poll` reading zero while jobs are plainly
in flight is normal, not a stall. Count the delay queues too if you want the
real backlog.

### Turning it off

Stop the worker, clear `RABBITMQ_URL`, restart the API. Batch jobs revert to
inline submission and read-driven polling, and jobs already `queued` are picked
up by the first `GET /tts/batch/{job_id}` that touches them — `refresh_job`
resubmits anything upstream has never seen. Messages left in the broker are
harmless: a redelivered submit is idempotent on our job id all the way through
to upstream's own `idempotency_key`.

---

## See also

- [docs/TTS.md](TTS.md) — what the batch routes promise a client, and what each
  state costs.
- `app/core/broker.py` — the publisher, the topology, and the argument above in
  its original form.
- `app/workers/tts_batch.py` — the consumer and its shutdown.
- [Credits and the wallet](../README.md#credits-and-the-wallet) — why the ledger
  is synchronous.
