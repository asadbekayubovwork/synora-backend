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
| `GET`  | `/auth/oauth/providers` | Which providers this server has credentials for |
| `GET`  | `/auth/oauth/{provider}/authorize` | Consent URL for Google / GitHub |
| `POST` | `/auth/oauth/{provider}/callback`  | `code` + `state` → tokens |
| `POST` | `/auth/oauth/telegram/callback`    | Login-widget payload → tokens |
| `GET`  | `/auth/oauth/accounts` | Providers linked to the signed-in account |
| `POST` | `/auth/oauth/{provider}/link` | Add a provider to the signed-in account |
| `DELETE` | `/auth/oauth/{provider}/link` | Remove one |
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

Outside `ENVIRONMENT=development` the app refuses to start if `JWT_SECRET` is
still the default or shorter than 32 bytes, if `CORS_ORIGINS` is `*`, if a
provider has an id but no secret (the button would appear and then fail), or if
`OAUTH_REDIRECT_URIS` allows any target — signing tokens with a guessable
secret lets anyone mint a session, and an open redirect list turns a leaked
client id into a code thief. Generate a secret with `openssl rand -hex 32`.

Where the provider credentials come from is written up in
[.env.example](.env.example), next to each block.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

77 tests cover both registration steps, the resend cooldown, the attempt cap,
login, token handling, the password reset, the three OAuth providers
(signature checks and account matching included) and the OpenAPI schema. The
provider round-trips are stubbed at `identity_from_code`, so the suite needs no
network and no real credentials.

## Layout

```
app/
├── main.py              FastAPI app, CORS, lifespan, Swagger metadata
├── core/
│   ├── config.py        Settings from .env
│   ├── security.py      bcrypt, OTP generation, JWT
│   └── exceptions.py    Typed errors + the shared error body
├── db/                  Declarative base and the async session
├── models/              User, OtpCode, OAuthAccount
├── schemas/             Request/response models (also the Swagger examples)
├── services/
│   ├── auth_service.py  register / verify / login / password reset
│   ├── otp_service.py   Code lifecycle
│   ├── mailer.py        SMTP
│   ├── oauth_service.py Provider identity -> user, linking, unlinking
│   └── oauth/           One module per provider, behind one interface
└── api/
    ├── deps.py          Session and bearer-token dependencies
    └── v1/
        ├── auth.py      Email + password routes
        └── oauth.py     Provider routes

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
so a release is `git reset --hard <sha>`, `pip install -r requirements.txt`,
restart, and a health check on `127.0.0.1:8010/health` — which, if it fails,
puts the previous commit back before the run is marked failed.

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

### Still to do

- **Back up `data/synora.db`.** Nothing copies it anywhere yet; losing the disk
  loses every account.
- **One worker only**, because SQLite serialises writers. Adding workers means
  moving to Postgres first — and Postgres means Alembic, since `init_db()` only
  creates missing tables.

## Notes for production

- **Migrations.** `init_db()` only creates missing tables; it will not alter
  existing ones. Add Alembic before the schema changes under real data.
  **This applies to the OAuth work:** `users.email` and `users.password_hash`
  became nullable and `full_name` / `avatar_url` were added, and an existing
  database will not pick any of that up. On the current SQLite deploy the
  quickest honest fix is to recreate `data/synora.db` (it holds test accounts
  only); with real data, write the `ALTER TABLE`s first.
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
