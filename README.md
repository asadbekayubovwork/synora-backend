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

Tables are created on startup, so there is nothing to migrate for a first run.

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
| `GET`  | `/health`           | Liveness probe |

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
| `OTP_MAX_ATTEMPTS` | `5` | Wrong guesses before the code is discarded |
| `OTP_RESEND_COOLDOWN_SECONDS` | `60` | Matches the frontend's resend timer |
| `EXPOSE_DEV_OTP` | `true` | Ignored unless `ENVIRONMENT=development` |
| `SMTP_HOST` | unset | Unset ⇒ codes are logged, not sent |
| `CORS_ORIGINS` | `http://localhost:3000,…` | Comma separated |

Outside `ENVIRONMENT=development` the app refuses to start if `JWT_SECRET` is
still the default or shorter than 32 bytes, or if `CORS_ORIGINS` is `*` —
signing tokens with a guessable secret lets anyone mint a session. Generate one
with `openssl rand -hex 32`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

28 tests cover both registration steps, the resend cooldown, the attempt cap,
login, token handling and the OpenAPI schema.

## Layout

```
app/
├── main.py              FastAPI app, CORS, lifespan, Swagger metadata
├── core/
│   ├── config.py        Settings from .env
│   ├── security.py      bcrypt, OTP generation, JWT
│   └── exceptions.py    Typed errors + the shared error body
├── db/                  Declarative base and the async session
├── models/              User, OtpCode
├── schemas/             Request/response models (also the Swagger examples)
├── services/            register / verify / login, OTP lifecycle, mailer
└── api/
    ├── deps.py          Session and bearer-token dependencies
    └── v1/auth.py       The routes
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

```bash
# from the project root, on your machine
tar --exclude='.venv' --exclude='__pycache__' --exclude='*.db' --exclude='.env' \
    -czf /tmp/synora.tar.gz app requirements.txt

scp /tmp/synora.tar.gz root@169.58.183.151:/tmp/
ssh root@169.58.183.151 '
  tar -xzf /tmp/synora.tar.gz -C /opt/synora-backend
  /opt/synora-backend/.venv/bin/pip install -r /opt/synora-backend/requirements.txt -q
  chown -R synora:synora /opt/synora-backend
  systemctl restart synora-api'
```

The server's `.env` is *not* in that archive and is never overwritten by a
deploy — it holds the production `JWT_SECRET`, and replacing it would sign out
every user at once.

### Still to do

- **Back up `data/synora.db`.** Nothing copies it anywhere yet; losing the disk
  loses every account.
- **One worker only**, because SQLite serialises writers. Adding workers means
  moving to Postgres first — and Postgres means Alembic, since `init_db()` only
  creates missing tables.

## Notes for production

- **Migrations.** `init_db()` only creates missing tables; it will not alter
  existing ones. Add Alembic before the schema changes under real data.
- **Refresh tokens are stateless.** They stay valid until they expire — there is
  no revocation list, so "log out everywhere" needs a stored token id or a
  per-user token version.
- **Rate limiting** covers OTP issuance only. Login is not throttled; put a
  limiter (nginx, Redis) in front of it before going public.
