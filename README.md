# Synora API

Authentication backend for the Synora AI frontend — FastAPI, SQLAlchemy 2 (async),
JWT, and email one-time codes.

Registration is deliberately two steps: `register` mails a code and stores the
account **unverified**; `verify-otp` activates it and signs the user in. An
unverified row cannot log in.

## Quick start

```bash
python -m venv .venv
source .venv/Scripts/activate   # Git Bash. PowerShell: .venv\Scripts\activate
pip install -r requirements.txt # macOS/Linux:  source .venv/bin/activate

cp .env.example .env            # PowerShell: copy .env.example .env
uvicorn app.main:app --reload
```

`activate` needs the leading `source` in Git Bash — without it the script runs
in a subshell and the current shell never sees the virtualenv, so `uvicorn`
stays "command not found". To skip activation entirely:

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

- Swagger UI — <http://127.0.0.1:8000/docs>
- ReDoc — <http://127.0.0.1:8000/redoc>
- OpenAPI JSON — <http://127.0.0.1:8000/openapi.json>

On the default SQLite URL, tables are created on startup, so there is nothing
to migrate for a first run. On Postgres — which production requires — Alembic
owns the schema and `init_db()` deliberately does nothing:

```bash
alembic upgrade head
python3 devtools/seed_price_book.py   # prices, so the AI routes can bill
```

**No mail server needed to develop.** With `SMTP_HOST` unset the code is printed
to the console, and while `ENVIRONMENT=development` the `register` and
`resend-otp` responses also carry it as `dev_code`.

## Endpoints

All under `/api/v1`.

| Method | Path                | Purpose |
| ------ | ------------------- | ------- |
| `POST` | `/auth/register`    | Step 1 — email + password, mails a 6-digit code |
| `POST` | `/auth/verify-otp`  | Step 2 — confirms the code, activates the account, returns tokens |
| `POST` | `/auth/resend-otp`  | New code for an unfinished signup |
| `POST` | `/auth/login`       | Email + password → tokens |
| `POST` | `/auth/refresh`     | Refresh token → a new pair |
| `GET`  | `/auth/me`          | The signed-in user (`Authorization: Bearer …`) |
| `POST` | `/auth/forgot-password`  | Reset step 1 — mails a code, and doubles as the resend |
| `POST` | `/auth/verify-reset-otp` | Reset step 2 — code → `reset_token` |
| `POST` | `/auth/reset-password`   | Reset step 3 — sets the new password |
| `GET`  | `/auth/oauth/providers` | Which providers this server has credentials for |
| `GET`  | `/auth/oauth/{provider}/authorize` | Consent URL for Google / GitHub |
| `POST` | `/auth/oauth/{provider}/callback`  | `code` + `state` → tokens |
| `POST` | `/auth/oauth/telegram/callback`    | Login-widget payload → tokens |
| `GET`  | `/auth/oauth/accounts` | Providers linked to the signed-in account |
| `POST` | `/auth/oauth/{provider}/link` | Add a provider to the signed-in account |
| `DELETE` | `/auth/oauth/{provider}/link` | Remove one |
| `GET`  | `/health`           | Liveness probe |
| `GET`  | `/wallet` | Credit balance: spendable, held, and why |
| `GET`  | `/wallet/transactions` | Every credit movement, newest first, cursor-paginated |
| `POST` | `/tts/speech` | Synthesise and stream the audio back. Metered |
| `POST` | `/tts/estimate` | What text would cost, holding nothing |
| `GET`  | `/tts/voices` | Built-in voices and clones |
| `POST` | `/tts/voices` | Clone a voice from a 3–30 second clip |
| `DELETE` | `/tts/voices/{voice_id}` | Remove a clone — for everyone on this deployment |
| `POST` | `/tts/batch` | Queue a corpus. Priced and held for up front |
| `GET`  | `/tts/batch` | Your jobs, newest first, cursor-paginated |
| `GET`  | `/tts/batch/{job_id}` | One job, refreshed against the speech service |
| `DELETE` | `/tts/batch/{job_id}` | Cancel, and settle at what was produced |
| `GET`  | `/tts/batch/{job_id}/results` | Per-item results |
| `GET`  | `/tts/recordings` | Every synthesis this account has kept |
| `GET`  | `/tts/recordings/{id}` | One of them |
| `GET`  | `/tts/recordings/{id}/audio` | Play it back. Costs nothing |
| `DELETE` | `/tts/recordings/{id}` | Erase one |
| `GET`  | `/usage` | Your own consumption, by service and metric |
| `GET`  | `/admin/wallets/{user_id}` | Any user's balance (superuser) |
| `POST` | `/admin/wallets/{user_id}/credits` | Grant credit by hand (superuser) |
| `POST` | `/admin/wallets/{user_id}/freeze` | Put a wallet on hold (superuser) |
| `POST` | `/admin/wallets/{user_id}/unfreeze` | Take it off hold (superuser) |
| `POST` | `/admin/reconcile` | Check every wallet against its ledger, finish batch jobs past their deadline, and release stranded holds (superuser) |

The AI microservices talk to a separate surface, **not** under `/api/v1`:

| Method | Path | |
| ------ | ---- | --- |
| `GET`  | `/internal/v1/health` | Readiness, published price version, our clock |
| `POST` | `/internal/v1/debug/echo-signature` | Shows the string we signed (dev/staging only) |

These are HMAC-signed rather than bearer-authenticated, and they sit outside
`API_PREFIX` so nginx can allowlist `location /internal/` at the edge — a
path-prefix ACL being much harder to get wrong than a list of route names. The
contract the other team codes against, including reference signers in Python
and Node, is [docs/INTERNAL_API.md](docs/INTERNAL_API.md).

### The registration flow

```bash
# 1. Register — the response carries dev_code while ENVIRONMENT=development
curl -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"ali@example.com","password":"Str0ngPassw0rd"}'

# → 201 {"ok":true,"email":"ali@example.com","expires_in":600,
#        "resend_available_in":60,"dev_code":"482913"}

# 2. Verify — activates the account and signs the user in
curl -X POST http://127.0.0.1:8000/api/v1/auth/verify-otp \
  -H "Content-Type: application/json" \
  -d '{"email":"ali@example.com","code":"482913"}'

# → 200 {"access_token":"…","refresh_token":"…","token_type":"bearer",
#        "expires_in":1800,"user":{…}}
```

Logging in before that step returns `403` / `email_not_verified`, which is the
frontend's cue to send the user back to the code screen.

### The password-reset flow

`forgot-password` → `verify-reset-otp` → `reset-password`, mirroring the pages
under `/forgot-password`.

Step 1 answers identically whether or not the account exists, so it cannot be
used to discover who is registered — which also means a `200` is not a promise
that mail was sent. Unverified signups get nothing: they have no confirmed
mailbox, and registering again replaces the password anyway.

Step 2 returns a `reset_token`: a short-lived JWT carrying a fingerprint of the
password hash it was issued against. That makes it single-use without any
ticket table — completing the reset changes the hash, so a replay stops
matching and returns `reset_token_used`.

Two things this flow deliberately does **not** do:

- **Sessions survive a reset.** Access and refresh tokens are stateless and
  carry no password version, so anyone already signed in stays signed in.
  Kicking them out needs a `token_version` column on `users`, checked when a
  token is decoded — and, since `init_db()` cannot alter an existing table, a
  migration for the deployed database.
- **Step 2 still leaks a little.** A wrong code answers `otp_invalid` only when
  a code was really issued, and `otp_not_found` otherwise. Closing that means
  storing decoy codes for addresses nobody registered.

### Signing in with a provider

Google and GitHub use the ordinary authorization-code flow, with the exchange
done here rather than in the browser — the client secret never leaves the
server. The frontend's own route is the `redirect_uri`, so the code comes back
to the page and is posted to the API.

```bash
# 1. Ask where to send the browser. redirect_uri defaults to the first
#    OAUTH_REDIRECT_URIS entry; anything else must match one exactly.
curl "$API/auth/oauth/google/authorize"
# → 200 {"provider":"google","authorization_url":"https://accounts.google.com/…",
#        "state":"eyJ…","redirect_uri":"http://localhost:3000/auth/callback",
#        "expires_in":600}

# 2. The user consents, Google redirects to
#    http://localhost:3000/auth/callback?code=…&state=…
#    Post both back, unchanged:
curl -X POST "$API/auth/oauth/google/callback" \
     -H 'Content-Type: application/json' \
     -d '{"code":"4/0Aean…","state":"eyJ…"}'
# → 200 the same {"access_token":…,"refresh_token":…,"user":{…}} as /auth/login
```

`state` is a short-lived signed token, not a database row: it carries the
provider and the redirect URI, so a code cannot be replayed against a
different provider or bounced to another page, and there is nothing to sweep.

**Telegram has no OAuth server.** Its login widget authenticates the user
inside Telegram and hands the browser a small profile object signed with
`HMAC-SHA256(sha256(bot_token), …)`. Verifying that signature *is* the
authentication, so there is no `/authorize` step — render the widget for
`bot_username` and post what it gives you, every field included, since the
signature covers all of them:

```bash
curl -X POST "$API/auth/oauth/telegram/callback" \
     -H 'Content-Type: application/json' \
     -d '{"id":987654321,"first_name":"Ali","username":"ali",
          "auth_date":1735689600,"hash":"a3f1…"}'
```

Payloads older than `TELEGRAM_AUTH_TTL_SECONDS` are refused, so one captured
from a browser does not work forever.

#### Which account you land on

1. **A provider account already linked** signs in as its owner. The match is on
   the provider's account id, so changing the email on Google's side keeps the
   same Synora account.
2. **A verified provider email** joins the account that holds that address —
   register with a password today, use Google tomorrow, one account. The
   provider has proved control of the mailbox, which is what our own OTP
   proves, so this is a link and not a takeover.
3. **Otherwise** an account is created, with no password.

An *unverified* provider email is refused (`oauth_email_unverified`) rather
than used, since anyone can type someone else's address into a throwaway
profile. One case worth knowing: if a provider email matches a signup that
never verified, that row is claimed **and its password is discarded** — it was
chosen by someone who never proved they could read the mailbox, and leaving it
in place would hand them the account.

An account with no password answers `403 password_login_unavailable` on
`/auth/login`, naming the providers it does use. `POST /auth/forgot-password`
works for it as a "set a first password" flow, as long as it has an email —
a Telegram-only account has neither, and Telegram stays its only way in. That
is also why unlinking the last provider from a password-less account is refused
with `oauth_last_login_method`.

`GET /auth/oauth/providers` lists only the providers this deployment has
credentials for, so the frontend can render exactly the buttons that work.

### Errors

Every non-2xx body has the same shape:

```json
{
  "detail": "That code is not correct. Please try again.",
  "statusMessage": "That code is not correct. Please try again.",
  "code": "otp_invalid"
}
```

`statusMessage` duplicates `detail` because the Nuxt pages read errors from
`error.data.statusMessage` — so they work against this API unchanged. Branch on
`code`, not on the message text:

| Code | Status | Meaning |
| ---- | ------ | ------- |
| `email_already_registered` | 409 | A verified account owns this address |
| `email_already_verified`   | 400 | Verification already done — sign in instead |
| `email_not_verified`       | 403 | Password was right, but the signup is unfinished |
| `invalid_credentials`      | 401 | Unknown email **or** wrong password (indistinguishable on purpose) |
| `otp_not_found`            | 400 | No pending verification for this address |
| `otp_invalid`              | 400 | Wrong code |
| `otp_expired`              | 400 | Code older than `OTP_TTL_MINUTES` |
| `otp_too_many_attempts`    | 429 | Guess cap hit; the code was discarded |
| `otp_cooldown`             | 429 | Resend asked for too soon (`Retry-After` header) |
| `reset_token_invalid`      | 400 | Not a reset token, or not for this account |
| `reset_token_expired`      | 400 | Older than `RESET_TOKEN_TTL_MINUTES` |
| `reset_token_used`         | 400 | The password already changed under it |
| `token_expired` / `token_invalid` | 401 | Bad or stale bearer token |
| `account_disabled`         | 403 | `is_active` is false |
| `insufficient_balance`     | 402 | Not enough credit; body carries `shortfallMicros` |
| `wallet_frozen`            | 403 | On hold after a reversal or an admin action |
| `wallet_not_found`         | 404 | Admin route, and that user has no wallet |
| `wallet_busy`              | 409 | Too many concurrent writes to one wallet; retry |
| `price_book_missing`       | 503 | Nothing is published, so nothing can be billed |
| `cursor_invalid`           | 400 | Malformed pagination cursor |
| `idempotency_key_required` | 400 | An admin money route was called without the header |
| `admin_required`           | 403 | Not a superuser |
| `signature_missing`        | 401 | An internal request arrived unsigned |
| `signature_invalid`        | 401 | The HMAC does not match |
| `signature_timestamp_skew` | 401 | Caller's clock is out; body carries `serverTime` |
| `signature_nonce_invalid`  | 401 | Nonce is not 16–128 URL-safe characters |
| `signature_replayed`       | 401 | That nonce was already used |
| `service_key_unknown` / `_revoked` / `_expired` | 401 | Which is which matters when you are reading a log at 3am |
| `service_key_forbidden`    | 403 | The key lacks the scope for this route |

## Credits and the wallet

The AI services are metered and paid for from a prepaid balance. Everything
about how that balance is stored follows from one decision: **money is an
integer count of micro-credits**, and

```
1 credit = 1 000 000 micros
```

That scale is not arbitrary. A single LLM token can cost a small fraction of a
credit, so charges routinely land in the hundreds of micros; a coarser unit
would round individual events to zero. And integers rather than `NUMERIC` or a
float because a balance that can drift is a balance you cannot reconcile —
SQLite round-trips `NUMERIC` through a C double, and `0.1 + 0.2` is why no
JavaScript client should be doing arithmetic on a parsed amount either. Every
API response therefore carries both: `available_micros` to compute with, and
`available` as a fixed-point string to display.

Som is equally integral: UZS is carried in **tiyin** (1 UZS = 100 tiyin), and
the two only meet at a top-up, whose exchange rate is recorded on the row that
used it — so a two-year-old receipt is still explicable after the price of a
credit has changed three times.

### Three counters, not one

| | |
| --- | --- |
| `paid_micros` | Bought with money. Never expires. |
| `bonus_micros` | Granted — a welcome credit, a campaign, a goodwill gesture. Can expire, and is **spent first** for exactly that reason. |
| `reserved_micros` | Committed to a session that has not settled yet. Held, not spent. |

```
available = paid + unexpired bonus - reserved
```

The reserve is what stops two concurrent calls from spending the same som. A
session takes a hold before it starts, tops it up as it runs, and gives back
whatever it did not use. Expired bonus stops counting the instant it lapses,
not whenever a cleanup job next runs.

### Every movement is on the ledger

`ledger_entries` is append-only, and that is enforced by a database trigger
rather than by review — a correction is a new entry, so history never moves.
The invariant is

```
wallets.<bucket>_micros == SUM(ledger_entries.amount_micros for that bucket)
```

and it holds because `app/services/billing/wallet_repo.py` is the only code
permitted to move a balance, writing the wallet and its ledger rows in one
transaction. A test walks the source tree to keep that true, and
`POST /admin/reconcile` checks it against the live data.

One operation can write more than one row: a charge that takes the last of a
bonus and the rest from paid credit writes one of each. Rows sharing a
`group_id` were written together and record the same resulting balance, so
group on it to show one line per operation.

### Running out mid-call

A charge that exceeds the balance is refused with **402** and a
`shortfallMicros` field, so the client can say how much to top up instead of
making the user guess.

A *live* call is different — cutting someone off mid-sentence because the
balance crossed zero between two heartbeats is a bad experience for the sake of
a few micros. So the caller is warned, then gets `BILLING_GRACE_SECONDS` and
`BILLING_GRACE_MICROS` of overrun, and then the call ends cleanly with a
reason. The overrun is **written off, not lent**: a prepaid product should not
acquire a debt nobody will collect, and refusing to let a balance go negative
keeps the strongest constraints in the schema intact. A write-off moves no
money, so it appears on a counter and not on the ledger.

### Prices are data, not configuration

Prices live in the database, versioned and immutable once published, and are
changed by publishing a new version — never by editing one. A session pins the
version it opened under, so a price published mid-call cannot re-rate a call
already in progress, and an invoice from March is still reproducible in
December. The som-per-credit rate is versioned separately, so changing what a
character costs does not silently reprice every top-up in flight.

A fresh install has no prices and will refuse to bill (`503
price_book_missing`) rather than guess. For local work:

```bash
python3 devtools/seed_price_book.py          # placeholder numbers
python3 devtools/seed_price_book.py --show   # what is published
```

**Those numbers are placeholders.** Replace them before anyone is charged.

### Admin access

The admin routes move real money, so `users.is_superuser` is not settable
through the API — there is no promote-yourself endpoint. It is a column rather
than an `ADMIN_EMAILS` allowlist because a Telegram-only account has no email
and could never be an admin, because an email allowlist would turn a future
"change my email" endpoint into privilege escalation, and because every action
needs attributing to a real user id, which the ledger records alongside the
required `note`.

```bash
python3 devtools/set_superuser.py ali@example.com
python3 devtools/set_superuser.py --list
```

## Text to speech

The speech service runs on its own box with its own GPU, and this API sits in
front of it as a **gateway** rather than a wrapper. The upstream `sk_live_…`
key never leaves this process; a signed-in user calls `/api/v1/tts/speech` with
their own JWT, we price their text, hold the credit, stream the audio through
and settle.

Handing that key to the frontend instead would be one line of code and would
put metering — which is the entire product — in the browser's hands. It would
also make "what did this cost?" answerable only from a header the upstream box
happens to send, rather than from `usage_events` in our own database. The price
of the gateway is one more hop and one more place a request can fail; that is
why every upstream failure is mapped to one of a handful of stable codes in a
single function, and why `docs/TTS.md` exists.

```bash
curl -X POST "$API/tts/speech" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -D - -o hello.mp3 \
  -d '{"text":"Salom! Bugungi ob-havo haqida qisqacha aytib beraman."}'

# → 200, audio/mpeg, and the bill on the headers:
#   x-synora-session-id: 6f1c9de7-…   x-synora-characters: 53
#   x-synora-price-micros: 250000     x-synora-price: 0.250000
```

### Billed by the character, and the whole text

Synthesis is priced on `tts_characters` and nothing else — the one quantity a
caller can count before spending anything. Because the entire input is in the
request body before any work starts, the price is known *before* the request
goes upstream: the hold is the price rather than a guess at it, the settlement
can never exceed the hold, and our own `200` can carry the final amount on its
headers.

That is also why **a client that hangs up mid-stream still pays for the whole
text.** The quote a caller sees before it commits is only worth having if it is
also the amount it pays, and by the time any audio is moving every character
has already gone to the GPU, so disconnecting saves no work. The one escape is
the honest one: if not a single byte of audio reaches us, the session is
abandoned, the hold goes back in full and nothing is charged.

Audio duration is measured and reported and never priced. The price book has no
`tts`/`tts_audio_ms` row, and `price_cumulative` raises on an unpriced metric
with a quantity above zero — so reporting seconds "for the dashboard" would not
mis-price a call, it would fail the settlement inside a `finally` where nobody
is left to catch it.

`POST /tts/estimate` prices text through the same call the charge uses, against
the same active price book, so the two cannot disagree by construction.

### Batch, and the queue that is optional

`POST /tts/batch` takes up to 500 clips, prices and holds for the whole job at
creation — a `402` before any GPU time is spent, rather than an hour into the
work — and settles at what the speech service reports having *actually*
synthesised, which is lower whenever items failed.

With `RABBITMQ_URL` set, the job is published and a worker
(`python -m app.workers.tts_batch`) submits and polls it. Without one, the
creating request submits it inline and reading `GET /tts/batch/{job_id}` is
what advances it. Both paths call the same two service functions, deliberately:
if the route billed anything of its own, a deployment would charge different
amounts depending on whether RabbitMQ happened to be running, and nobody would
find that from an invoice.

The broker is there for admission control in front of a single GPU —
`RABBITMQ_PREFETCH` is the number of batch items in flight against the card —
and not because "async is nicer". It is deliberately absent from the streaming
path, where a queue hop would eat most of a 180 ms first-audio budget, and from
the wallet ledger, where a debit behind a queue is not a debit but a promise of
one. [docs/QUEUEING.md](docs/QUEUEING.md) is that argument in full, with the
topology and the worker's runbook.

### Reading further

| | |
| --- | --- |
| [docs/TTS.md](docs/TTS.md) | The integration contract: every route with a curl example, what is billed and when, the `402` shape, the error-code table |
| [Trying it locally](docs/TTS.md#trying-it-locally) | Zero to a synthesis you paid for: a throwaway database, a funded account, and what each response should say |
| [docs/QUEUEING.md](docs/QUEUEING.md) | Where RabbitMQ is used, where it is refused, and how to run the worker |
| `/docs` | The Swagger page, where every route's failure modes are written out |

Leave `TTS_BASE_URL` or `TTS_API_KEY` empty and every `/tts` route answers
`503 tts_not_configured`; nothing else in the API changes. Setting one without
the other is refused at boot outside development, for the same reason a
half-configured OAuth provider is: a surface that advertises itself as
available and then fails on first use is worse than one that is plainly off.

## Kept syntheses

A synthesis used to leave counters and nothing else: `usage_events` could say a
wallet paid a quarter of a credit for a thousand characters, and nothing could
say which thousand or hand the audio back. `tts_recordings` is the other half.
Every delivered stream writes a row — the exact text, the voice, the format —
and the audio goes to a file under `RECORDINGS_DIR`, named by the sha256 of its
own bytes.

```bash
curl -s "$API/tts/recordings" -H "$A"                       # newest first, cursor-paged
curl -s "$API/tts/recordings/$ID/audio" -H "$A" -o out.wav  # the same bytes, verifiable
curl -s -X DELETE "$API/tts/recordings/$ID" -H "$A"         # erase one
```

Content-addressing is doing real work there. The same text in the same voice is
the same bytes, so a client that retries stores one file and two rows — and
deleting a recording therefore deletes the row and only unlinks the file once no
row names it any more. Unlinking on sight is how one user's delete silently
empties another user's playback, and the symptom would arrive weeks later as a
200 with an empty body.

Three things are deliberately not kept: a call upstream refused before the first
byte, which charged nothing; a retry under a spent `Idempotency-Key`, which
re-synthesised audio the original already has a row for; and anything at all
while `RECORDINGS_ENABLED` is false.

Nothing here is on the billing path. `recording_store` swallows its own
failures, the row is written on its own session after the settlement has
committed, and a full disk costs a recording rather than a stream somebody is
paying for — `tests/test_tts_recordings.py` drives exactly that case. Deleting a
recording changes nothing about the charge either: the ledger and `GET /usage`
are the record of what was billed, and erasing what was said does not erase that
it was paid for.

The trade is that this keeps customer text and customer audio indefinitely.
There is no automatic expiry; `DELETE` is the user's own control, and
`RECORDINGS_ENABLED=false` is the deployment's.

## Clicking through it in a browser

Swagger at `/docs` covers most of the API, but `POST /tts/speech` returns audio,
and Swagger hands that back as a download link with no player. `dev-ui/` is one
dependency-free HTML file that drives every route from a browser instead:

```bash
.venv/bin/uvicorn app.main:app --port 8000
python dev-ui/serve.py                       # → http://localhost:3000
```

Port 3000 because that is what `CORS_ORIGINS` defaults to. Register with
`dev_code` filled in automatically, grant yourself credit, synthesise, and hear
it — with the `X-Synora-*` headers, the time to first byte, and the ledger
entries beside it. After every synthesis it checks the two things worth checking
by hand: that the balance fell by exactly `X-Synora-Price-Micros`, and that
`reserved` went back to zero.

No speech box needed. `dev-ui/fake_speech_box.py` stands in for the GPU — it
serves the paths `tts_client` expects and streams a tone sized from the
character count, so the whole billing path is exercised without synthesising
anything real:

```bash
python dev-ui/fake_speech_box.py             # → :8100
TTS_BASE_URL=http://127.0.0.1:8100 TTS_API_KEY=fake-key \
    .venv/bin/uvicorn app.main:app --port 8000
```

Swapping `TTS_API_KEY` for `reject-me`, `quota`, `busy`, `bad-input` or
`garbage` makes it answer with the upstream failure of that name, which is how
to see `tts_key_rejected`, `tts_busy` and the rest without breaking anything.
Details in [dev-ui/README.md](dev-ui/README.md).

## Watching it work

`/metrics` exposes what this process counted while it was working, in
Prometheus's text format, and `grafana/` is a two-container stack that graphs
it:

```bash
.venv/bin/uvicorn app.main:app --port 8000
docker compose -f grafana/docker-compose.yml up -d     # → http://localhost:3001
```

The dashboard is provisioned from files, so it is the same on everyone's
machine and a panel change is a diff rather than a click.

Nothing on it is derived from logs or recomputed from the database afterwards.
Every counter is incremented by the code that already knew the answer:
`_finalise` knows how a stream ended, `settle_oneshot` knows what was debited,
`reconcile_all` knows what diverged. A metric read off a second source is a
metric that eventually disagrees with the first one, and the argument for
metering on this side of the gateway is that there is only one count.

Four things a counter cannot know — credit held by unsettled sessions, open
sessions, open batch jobs, balances — are read from the rows at scrape time by
`refresh_db_gauges`. That is what makes **held credit** a graph rather than a
guess, and a floor that climbs across a quiet night is a hold that never came
back.

Two things switch the endpoint off rather than configure it wrongly, and
neither is a boot failure — a dashboard must not be able to fail a release of
the API it watches. Outside development `/metrics` answers `404` until
`METRICS_TOKEN` is set, because nginx proxies `location /` and the endpoint
publishes call volumes and credit movements. `WORKER_COUNT > 1` switches it
off too, because the registry is per-process and Prometheus would scrape
whichever worker the proxy picked. The startup log names the reason in both
cases:

```
synora: Metrics: not served (set METRICS_TOKEN; /metrics answers 404 without one)
```

Details in [grafana/README.md](grafana/README.md) and in the module docstring
of `app/core/metrics.py`.

## The Nuxt frontend

`Synora-frontend` is already wired to this API. The browser calls it directly —
`CORS_ORIGINS` lists `http://localhost:3000` — via `NUXT_PUBLIC_API_BASE`,
which defaults to `http://127.0.0.1:8000/api/v1`. Run both dev servers and the
signup and sign-in flows work end to end.

What lives on that side:

| File | Role |
| ---- | ---- |
| `app/composables/useAuthSession.ts` | Tokens and the current user |
| `app/composables/useAuth.ts` | `register` / `verifyOtp` / `resendOtp` / `login` / `logout` |
| `app/plugins/api.ts` | `$api` — attaches the bearer token, retries once after refreshing |
| `app/middleware/auth.ts` | Guards signed-in pages |
| `app/middleware/guest.ts` | Keeps signed-in users off the auth pages |
| `app/utils/apiError.ts` | Reads `code` / `statusMessage` off an error |

Both guards run in the browser only, and `/` is client-rendered
(`routeRules`) — the session lives in cookies the browser owns, so there is
nothing for the server to render it from.

The password-reset pages still use the in-memory stubs under
`server/api/auth/` because this API does not implement that flow yet.

## Sending the code by email

Out of the box nothing is sent — the mailer logs the code instead. To deliver
real mail, point `SMTP_*` at a server; [.env.example](.env.example) has ready
blocks for Gmail, Resend and Brevo.

**Gmail** is the fastest way to see a real message. Turn on 2-step
verification, create an *App Password*, and use that — the account password is
rejected. It is capped near 500 a day and forces the From address to your Gmail
one, so it is for testing.

**Production sends as `no-reply@synora-ai.uz` through Resend**, and the DNS for
it is in place — these records live in the ahost.uz panel that serves the domain:

| Record | Name | Purpose |
| ------ | ---- | ------- |
| TXT | `resend._domainkey` | DKIM key, so each message is signed |
| CNAME | `rsend` | Resend's sending host |
| CNAME | `send` | Custom return path, which is what aligns SPF |

Gmail reports SPF, DKIM and DMARC all passing, with DKIM signed by
`synora-ai.uz` itself rather than a provider subdomain — that alignment is what
keeps the code out of spam. Changing the sending domain means redoing these
records; without them mail either lands in spam or is rejected, which is the
usual reason "the email never arrives".

The domain also has its own mail server (MX → `mail.synora-ai.uz`, plus
`default._domainkey`), but ports 25, 465 and 587 all time out there, so it is
not an option until ahost.uz opens SMTP.

The template lives in [`app/services/mailer.py`](app/services/mailer.py) and is
table-based with inline styles, because Outlook renders HTML through Word and
drops flexbox, grid and `<style>` blocks. `tests/test_mailer.py` sends through a
real local SMTP server, so changes to it are checked end to end.

One thing to know before launch: the message goes out inside the request, so
`register` waits on the SMTP handshake — typically 1–3 seconds. That is fine at
low volume; past that, move the send to a background task or a queue.

## Configuration

Everything lives in `.env`; see [.env.example](.env.example) for the full list.

| Variable | Default | Notes |
| -------- | ------- | ----- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./synora.db` | Postgres: `postgresql+asyncpg://user:pass@host:5432/synora` |
| `JWT_SECRET` | dev placeholder | **Must** be changed outside development — see below |
| `ACCESS_TOKEN_TTL_MINUTES` | `30` | |
| `REFRESH_TOKEN_TTL_DAYS` | `30` | |
| `OTP_TTL_MINUTES` | `10` | |
| `OAUTH_REDIRECT_URIS` | `http://localhost:3000/auth/callback` | Comma separated, matched exactly; first is the default |
| `OAUTH_STATE_TTL_MINUTES` | `10` | How long a sign-in may sit mid-flow |
| `GOOGLE_CLIENT_ID` / `_SECRET` | unset | Unset ⇒ Google is absent from `/auth/oauth/providers` |
| `GITHUB_CLIENT_ID` / `_SECRET` | unset | Same |
| `TELEGRAM_BOT_TOKEN` | unset | Also the widget's verification key — guard it like `JWT_SECRET` |
| `TELEGRAM_BOT_USERNAME` | unset | Only for rendering the widget |
| `TELEGRAM_AUTH_TTL_SECONDS` | `86400` | How old a widget payload may be |
| `OTP_MAX_ATTEMPTS` | `5` | Wrong guesses before the code is discarded |
| `OTP_RESEND_COOLDOWN_SECONDS` | `60` | Matches the frontend's resend timer |
| `EXPOSE_DEV_OTP` | `true` | Ignored unless `ENVIRONMENT=development` |
| `SMTP_HOST` | unset | Unset ⇒ codes are logged, not sent |
| `CORS_ORIGINS` | `http://localhost:3000,…` | Comma separated |
| `REDIS_URL` | unset | Unset ⇒ every cache falls back to Postgres. Required with `WORKER_COUNT > 1` |
| `REDIS_PREFIX` | `synora:` | The box hosts other projects; namespace everything |
| `WORKER_COUNT` | `1` | Declared, not detected. Only used to refuse a multi-worker boot without Redis |
| `BILLING_SIGNUP_BONUS_MICROS` | `0` | Welcome credit. `0` disables it |
| `BILLING_LOW_BALANCE_MICROS` | `0` | Below this, the wallet reports `is_low` |
| `BILLING_GRACE_SECONDS` / `_MICROS` | `30` / `5000000` | How far a live call may overrun before it is cut. Written off, not lent |
| `BILLING_HOLD_SECONDS` | `120` | How much of a realtime session to reserve up front |
| `BILLING_ROLLUP_TIMEZONE` | `Asia/Tashkent` | Local day boundary for usage reports |
| `TTS_BASE_URL` / `TTS_API_KEY` | unset | Unset ⇒ every `/tts` route answers `503`. One without the other is refused at boot. Full list in [docs/TTS.md](docs/TTS.md#configuration) |
| `RECORDINGS_ENABLED` / `RECORDINGS_DIR` | `true` / `data/recordings` | Keep the text and the audio of every delivered synthesis. `false` keeps neither |
| `METRICS_ENABLED` / `METRICS_TOKEN` | `true` / unset | `GET /metrics`. Outside development it answers `404` until a token is set — nginx proxies `location /`, so the endpoint is public the moment it exists |
| `RABBITMQ_URL` | unset | Unset ⇒ batch jobs are submitted inline and polled when read. See [docs/QUEUEING.md](docs/QUEUEING.md) |
| `RABBITMQ_PREFETCH` | `4` | Batch items in flight against the single GPU. A ceiling, not a throughput knob |

Prices are deliberately **not** in here. They live in the database, versioned,
and are published through the admin API — so changing what a character costs
needs no deploy, and every old invoice stays reproducible. Same for the
som-per-credit rate. See [Credits and the wallet](#credits-and-the-wallet).

Outside `ENVIRONMENT=development` the app refuses to start if `JWT_SECRET` is
still the default or shorter than 32 bytes, if `CORS_ORIGINS` is `*`, if a
provider has an id but no secret (the button would appear and then fail), or if
`OAUTH_REDIRECT_URIS` allows any target — signing tokens with a guessable
secret lets anyone mint a session, and an open redirect list turns a leaked
client id into a code thief. Generate a secret with `openssl rand -hex 32`.

It also refuses to start on a **SQLite** `DATABASE_URL`, or with
`WORKER_COUNT > 1` and no `REDIS_URL`. Both are money-safety checks rather than
tidiness: SQLite ignores `SELECT ... FOR UPDATE` silently, and a second worker
without Redis would run the background reaper twice and deliver balance events
to whichever worker the browser happened to connect to.

Where the provider credentials come from is written up in
[.env.example](.env.example), next to each block.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Over 300 tests cover both registration steps, the resend cooldown, the attempt
cap, login, token handling, the password reset, the three OAuth providers
(signature checks and account matching included), the OpenAPI schema, the
credit system — pricing arithmetic, the wallet, the ledger invariant,
reconciliation, pagination and the HTTP surface — and the speech gateway:
what a stream charges when the client hangs up, what it charges when nothing
arrives, the idempotency guard, the batch lifecycle end to end, and the
inline no-broker path. The provider round-trips are stubbed at
`identity_from_code` and the speech service at the transport, so the suite
needs no network and no real credentials.

Five of them are skipped by default. Concurrency cannot be tested on SQLite —
its writers serialise at the file and `with_for_update()` compiles to nothing —
so a "twenty callers race for eight credits" test there would pass while
proving nothing at all. Point the suite at a real Postgres to run them:

```bash
createdb synora_api_test
TEST_DATABASE_URL=postgresql+asyncpg://synora_api:synora_api@localhost/synora_api_test pytest
```

Worth doing before anything touching money ships, and worth having as a second
CI job. Everything else passes on both.

**One database per run.** `clean_database` drops and recreates the whole schema
before every test, so two pytest processes sharing a Postgres database would
tear each other's tables down mid-test. On Postgres the fixture takes an
advisory lock for the duration of each test, so a second process queues instead
of interleaving — which matters because the failures that interleaving produces
look nothing like the cause: rows vanishing between two statements, a duplicate
key on an email the test just created, and a different test failing each run.

**Redis is not needed to run the tests.** `REDIS_URL` is empty in `conftest.py`
on purpose, so the whole suite exercises the Postgres fallback — a fallback
nobody runs is a fallback that does not work. The Redis-specific behaviour is
driven through a stub client in `tests/test_cache.py`, which is the better test
anyway: a stub can be made to fail on command, and "what happens when Redis
disappears mid-request" is the half that matters.

To exercise the real thing by hand:

```bash
brew install redis && redis-server --daemonize yes
REDIS_URL=redis://localhost:6379/0 uvicorn app.main:app --reload
```

`GET /internal/v1/health` then reports `redis: "up"` and `state: "ready"`, and
replaying an identical signed request gets `401 signature_replayed`. Stop Redis
and the same request still succeeds, with `state: "degraded"` — the outage is
logged once, not once per request.

## Layout

```
alembic/                 Migrations. The schema authority — see alembic/README
app/
├── main.py              FastAPI app, CORS, lifespan, Swagger metadata
├── core/
│   ├── config.py        Settings from .env
│   ├── security.py      bcrypt, OTP generation, JWT
│   ├── exceptions.py    Typed errors + the shared error body
│   ├── money.py         Micro-credits: the one money unit, and its arithmetic
│   ├── signing.py       HMAC request signing for the microservices
│   ├── cache.py         Redis, and the Postgres-shaped hole where Redis isn't
│   └── broker.py        RabbitMQ, or nothing: admission control for one GPU
├── db/                  Declarative base, naming convention, async session
├── models/              User, OtpCode, OAuthAccount, billing tables, tts jobs
├── schemas/             Request/response models (also the Swagger examples)
│   └── common.py        The cursor-pagination convention
├── services/
│   ├── auth_service.py  register / verify / login / password reset
│   ├── otp_service.py   Code lifecycle
│   ├── mailer.py        SMTP
│   ├── oauth_service.py Provider identity -> user, linking, unlinking
│   ├── oauth/           One module per provider, behind one interface
│   ├── ai/
│   │   ├── recording_store.py   Audio files, content-addressed. No rows
│   │   ├── tts_recording_service.py  Rows for delivered audio, and ownership
│   │   ├── tts_client.py        The speech box, and nothing else. No money
│   │   ├── tts_service.py       One metered stream: hold, relay, settle
│   │   └── tts_batch_service.py A job, its hold and its settlement
│   └── billing/
│       ├── wallet_repo.py       THE only code that moves a balance
│       ├── wallet_service.py    Wallet lifecycle and the read model
│       ├── pricing.py           Quantities -> micro-credits
│       ├── session_service.py   Open, settle or abandon a metered call
│       └── reconcile_service.py Does the ledger still add up?
├── api/
│   ├── deps.py          Session, bearer-token and superuser dependencies
│   ├── internal/        The microservice surface. HMAC-signed, not for browsers
│   └── v1/
│       ├── auth.py      Email + password routes
│       ├── oauth.py     Provider routes
│       ├── wallet.py    Balance and statement
│       ├── tts.py       Speech: the metered stream, voices, batch jobs
│       ├── usage.py     What this account consumed, from our own rows
│       └── admin.py     Superuser-only money routes
└── workers/             Processes that are not the API. All of them optional
    └── tts_batch.py     python -m app.workers.tts_batch
docs/
├── INTERNAL_API.md      The contract the AI microservices code against
├── TTS.md               The speech contract: routes, billing, errors
└── QUEUEING.md          Where RabbitMQ is used, and where it deliberately isn't
dev-ui/                  One HTML file that drives every route from a browser
grafana/                 Prometheus + Grafana, provisioned from files
devtools/                Scripts for things the API deliberately will not do
deploy/                  Release script, systemd unit, one-time server setup
```

## Deployment

Live at **https://back.synora-ai.uz** — Swagger at `/docs`.

The box at `169.58.183.151` hosts several unrelated projects, so this deploy
stays in its own lane:

| | |
| --- | --- |
| Code | `/opt/synora-backend` (venv at `.venv`, database in `data/`) |
| Service | `synora-api.service`, running as the `synora` system user |
| Port | `127.0.0.1:8010` — **8000 belongs to the online-talim container** |
| nginx | `/etc/nginx/sites-available/back.synora-ai.uz.conf` |
| TLS | Let's Encrypt, auto-renewing |

The port is bound to loopback only; nginx is the sole way in, and ufw allows
just SSH, 80 and 443.

```bash
systemctl status synora-api          # is it up
journalctl -u synora-api -f          # live logs
systemctl restart synora-api         # after an .env or code change
```

### Shipping a new version

**Push to `main`.** [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)
runs the test suite and, only if it is green, SSHes in as the `deploy` user and
releases that commit. `/opt/synora-backend` is itself the checkout systemd runs,
so a release is `git reset --hard <sha>`, `alembic upgrade head`,
`pip install -r requirements.txt`, restart, and a health check on
`127.0.0.1:8010/health` — which, if it fails, puts the previous commit back
before the run is marked failed.

**The migration step is not optional and is not decorative.** Alembic is the
schema authority and `init_db()` deliberately does nothing outside SQLite, so
without it a release restarts onto tables that do not exist. That failure is
worse than it sounds, because it is silent: `/health` touches no table, so the
health check passes, the deploy is marked green, the rollback never fires, and
the first symptom is a 500 on every request that reads a wallet. It runs before
the restart and as `synora` — `DATABASE_URL` lives in `.env`, which `deploy`
deliberately cannot read — and a failure there aborts the release with the old
code still serving, which is the outcome to want.

Migrating before the restart is safe only because every revision here is
additive: the old process keeps working against the new schema for the seconds
until it is replaced, and a rollback that leaves the schema forward is
harmless for the same reason. A revision that drops or rewrites a column the
running code still reads breaks both halves of that and does not belong in an
unattended release.

**A database that predates Alembic must be stamped once, by hand**, or the
first migrating release aborts trying to create tables that are already there.
[`deploy/README.md`](deploy/README.md#known-gaps) has the three commands.

The same script deploys by hand, so the two paths cannot drift:

```bash
ssh deploy@169.58.183.151 /usr/local/sbin/synora-api-deploy       # deploy main
ssh deploy@169.58.183.151 'DEPLOY_REF=<sha> /usr/local/sbin/synora-api-deploy'
```

The server's `.env` and `data/` are gitignored, and the release runs `git clean`
without `-x`, so neither is ever touched — `.env` holds the production
`JWT_SECRET`, and replacing it would sign out every user at once.

[`deploy/`](deploy/) holds the release script, the unit file and the one-time
server setup; [`deploy/README.md`](deploy/README.md) is the full write-up,
including the `DEPLOY_SSH_KEY` secret the workflow needs.

**If the box runs the batch worker, restart it in the same breath:**
`systemctl restart synora-api synora-tts-worker`. `synora-tts-worker.service` —
the unit is in [docs/QUEUEING.md](docs/QUEUEING.md#on-the-server) — is a second
process running `python -m app.workers.tts_batch` out of the same directory and
the same `.env`, and it settles batch jobs with the same code the API does.
Restarting only the API leaves the previous release's billing path consuming
from the queue against the new schema. Nothing new goes in the tarball for it:
`app` already carries the worker. The unit exists only where `RABBITMQ_URL` is
set; without a broker there is nothing for it to consume and it refuses to
start.

### Still to do

- **Move production to Postgres.** This is now a hard requirement rather than a
  preference: the app refuses to boot outside development on a SQLite
  `DATABASE_URL`. SQLite serialises writers, ignores `SELECT ... FOR UPDATE`
  silently, and is not a database to keep money in. `asyncpg` is already
  pinned, so it is a URL change plus `alembic upgrade head`.
- **Back the database up, and rehearse the restore.** Nothing copies it
  anywhere yet. Losing the users table lost accounts; losing the wallets table
  loses money that is owed. `pg_dump` on a timer with off-box retention, and a
  restore somebody has actually performed once.
- **One worker still.** Going to two needs `REDIS_URL` set — the background-job
  lease and the balance event stream both need somewhere shared to coordinate —
  and `assert_production_ready()` refuses to start with `WORKER_COUNT > 1` and
  no Redis, rather than letting the reaper run twice and the events go missing.
- **Watch for top-ups stuck in `paid`.** A row that reached `paid` but not
  `credited` for more than a minute means money arrived and credit did not.
  `journalctl -u synora-api | grep topup_credit_pending`.
- **Give the speech service a stable hostname.** The `TTS_BASE_URL` in
  `.env.example` is a `trycloudflare.com` quick tunnel, and those change every
  time the tunnel restarts. A changed URL is `502 tts_unreachable` on every
  synthesis, with nothing wrong on either box. A real DNS name and a persistent
  tunnel — or the GPU box behind our own nginx — before anyone depends on it.
- **Nothing runs `POST /admin/reconcile` on a schedule.** It is the only caller
  of the session reaper, and the reaper is what gives back a hold left behind by
  a process that died between placing it and settling — a client gone before the
  response body started, a settlement that failed on a dead connection, a worker
  killed mid-charge. It is now also the only caller of the batch sweeper, so
  **both** backstops in this system are waiting on the same missing timer. A
  stranded hold does not expire on its own: it waits to be noticed, and the
  customer's credit is frozen the whole time. Until it is on a timer — a cron
  calling the route, or an in-process job behind the same Redis lease
  `WORKER_COUNT > 1` already needs — "run it after anything unusual" is the only
  policy there is, and nobody knows when something unusual happened.
- **The batch sweeper exists; nothing puts it on a timer.**
  `TTS_BATCH_MAX_POLL_SECONDS` used to be reachable only from
  `tts_batch_service.refresh_job` — a worker's poll, or a read of
  `GET /tts/batch/{job_id}` — so with no worker and no reader (a dead-lettered
  submit, a client that gave up) the credit was held forever. It is now also
  reachable from `tts_batch_service.sweep_stale_jobs`, which `POST
  /admin/reconcile` calls beside the wallet passes. That sweep is a pass of its
  own because the reconcile *reaper* deliberately refuses to be this backstop:
  it skips any metered session a non-terminal batch job points at, since the
  session's clock starts when it was opened and the job's when upstream accepted
  it, and a reaper acting on the earlier of the two was closing the sessions of
  healthy jobs and settling them at zero. It is composed at the route rather
  than inside `reconcile_service` for a second reason worth keeping: a billing
  module importing from `app/services/ai/` would invert the layering. So a
  stranded batch hold is recoverable now rather than permanent — but only as
  often as somebody calls that route, which is the bullet above. Until that is
  done, the backstop is a person.
- **No batch worker runs in production yet.** RabbitMQ is not installed on the
  box, so `RABBITMQ_URL` is empty and every batch job is submitted inline by the
  request that creates it and advanced only when someone reads it. That is a
  supported configuration and the whole batch path is built for it — but it
  means nothing limits how many jobs hit the single GPU at once, and the
  stranded-hold point above applies in full. `synora-tts-worker.service` is
  written out in [docs/QUEUEING.md](docs/QUEUEING.md#on-the-server) and is not
  installed anywhere; nothing new goes in the release tarball for it, since
  `app` already carries the worker. Install the broker, the unit and
  `RABBITMQ_URL` when batch traffic stops being occasional.
- **RabbitMQ and Redis are both optional, and both stop being optional at the
  second box.** Without a broker the hand-over to the speech service happens
  inline on the creating request: correct on one machine, wrong on two, because
  admission control in front of a single GPU is exactly the thing that cannot be
  per-process. Without Redis the per-account synthesis cap counts nothing at all
  — `NullCache.increment` returns zero, so `TTS_MAX_CONCURRENT_PER_USER` is off
  rather than enforced — and `assert_production_ready()` already refuses
  `WORKER_COUNT > 1` without it. Neither is a bug on today's single-box
  deployment. Both are prerequisites for the next one, and the concurrency cap
  being silently absent is worth knowing before it is quoted to anybody.
- **`Idempotency-Key` does not cover the retry people mean.** On `/tts/speech`
  it collapses a retry that *races* the original and nothing else: once that
  synthesis has ended — settled, or abandoned after an upstream refusal — the key
  is spent and comes back `409 tts_idempotency_spent`. The route description and
  [docs/TTS.md](docs/TTS.md) now say so plainly, which is the honest fix and not
  the complete one. Making a key answer the ordinary "my connection dropped, is
  it done?" needs somewhere to keep the outcome — at minimum the session id,
  character count and price, so a spent key can be answered with the original
  bill instead of a refusal; at most the audio itself, which is megabytes of a
  customer's content per call and a retention decision nobody has made. Until
  then, clients must branch on the code rather than retry blindly.
- **Batch audio has nowhere to be fetched from.** `GET /tts/batch/{job_id}/results`
  returns the speech service's own storage paths, not URLs this API serves, so
  a client can see that a clip rendered and cannot download it. Serving them
  needs a decision about where the files live and who pays for the egress; until
  then, the paths are for support requests.

## Notes for production

- **Migrations.** Alembic is the schema authority now. `init_db()` still
  exists, but it only runs when `DATABASE_URL` is SQLite — it creates missing
  tables for local work and for the test suite, and it can never alter one.
  On anything else it logs and does nothing, because creating tables Alembic
  does not know about and then never altering them again is the failure mode
  it used to have.
  Revision `0001` reproduces the pre-Alembic auth schema exactly, so a fresh
  database and a stamped one converge. An **already-deployed** database is
  brought under Alembic with `alembic stamp 0001` and then `alembic upgrade
  head` — but note it will carry database-picked constraint names rather than
  the `pk_users` / `fk_…` convention the models now declare, which only matters
  the day a migration wants to drop one of them by name.
- **Refresh tokens are stateless.** They stay valid until they expire — there is
  no revocation list, so "log out everywhere" needs a stored token id or a
  per-user token version.
- **OAuth tokens are not kept.** The provider's access token is used once, to
  read the profile, and then dropped — nothing here calls Google or GitHub on
  the user's behalf later. Add refresh-token storage only if that changes.
- **A Telegram-only account has no email**, so `user.email` is `null` in every
  response. Nothing yet lets such a user add one; that is the missing piece
  before they can receive any mail from us.
- **Rate limiting** covers OTP issuance only. Login is not throttled; put a
  limiter (nginx, Redis) in front of it before going public.
- **The ledger is append-only, and a trigger enforces it.** Not review — an
  actual `BEFORE UPDATE OR DELETE` trigger, so a psql session cannot quietly
  fix a row either. If a balance is wrong, post a correcting entry; the history
  is the only evidence of how it got wrong.
- **`ON DELETE RESTRICT` on the money tables is deliberate.** `DELETE FROM
  users` now fails for anyone who ever held credit, which makes deleting an
  account an anonymise-in-place operation rather than a row removal. That is
  correct for financial records and it needs a product and legal decision
  before anyone builds a delete-my-account button on top of it.
- **Reconciliation runs on demand, not yet on a timer.** `POST
  /admin/reconcile` is the check; wiring it to a schedule is part of the
  background-jobs work. Until then, run it after anything unusual. It releases
  reserved credit that no live session claims — a leaked hold silently freezes
  a paying customer's money — but never touches a balance that disagrees with
  its ledger, because that needs a human and the number is the evidence.
  Two exclusions are worth knowing before treating it as a catch-all. It leaves
  alone a session that is *still making progress*, because a streaming response
  is paced by whoever is reading it and a deadline on starting is not a deadline
  on finishing. And it leaves alone any session a non-terminal batch job points
  at, because that job's own deadline is the one that owns it — reaping on the
  session's earlier clock was closing healthy jobs and settling them at zero.
  That second exclusion is why the route runs a pass the reconciler does not
  own: `tts_batch_service.sweep_stale_jobs` finishes jobs past their *own*
  deadline, reported as `swept`, and it is called from the route beside
  `reconcile_all` rather than from inside it so that the billing layer never
  imports the AI services. What is left in **Still to do** is the timer both
  passes are waiting on.
- **A write-off is not on the ledger.** When a live call overruns into grace,
  the uncollected part lands on `wallets.lifetime_writeoff_micros` and writes
  no entry, because no credit moved. It is the one denormalised counter
  reconciliation deliberately does not check, and that is worth knowing before
  someone "fixes" the omission.
