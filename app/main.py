"""The application object: assembly, and the two orderings that matter.

Nothing here decides anything a route or a service could decide instead. What
this module does own is the shape of the process — which routers are mounted
where, which response headers a browser is allowed to read, and the order the
lifespan starts and stops things in — and each of those is a place where being
wrong is silent rather than loud, which is why they are all commented in place.

## The startup checks run in the lifespan, not at import

`settings.assert_production_ready()` is the boot-time refusal for a default
`JWT_SECRET`, a SQLite `DATABASE_URL`, `CORS_ORIGINS: *` and the rest. It lives
inside `lifespan` because the test suite and every tooling import — Alembic's
`env.py`, a `python -c "from app.main import app"` smoke check, Swagger
generation in CI — import this module without ever starting the server. Raising
at import would turn "generate the OpenAPI document" into a configuration
error on any machine that is not production, and the check would then be
deleted rather than fixed.

## Shutdown is ordered by what each step still needs to be alive

The settlements go first and the closes go last, and the gap between them is
the point: `tts_service.drain_settlements` bounds how long we *wait* for an
in-flight charge without cancelling it, so a settlement that outlives the bound
is still running while the HTTP client, the broker and the engine are closed
behind it. Those three closes are therefore extra time for it to land in, not
an interruption of it. Reversing the order — or, worse, cancelling the drain —
turns a hold that comes back late into a charge that is lost, and the customer
notices the second one on their invoice.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from hmac import compare_digest

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST

from app.api.internal.router import internal_router
from app.api.v1.router import api_router
from app.core.broker import close_broker
from app.core.config import settings
from app.core.exceptions import (
    NotFoundError,
    UnauthorizedError,
    register_exception_handlers,
)
from app.core.metrics import MetricsMiddleware, refresh_db_gauges, render
from app.db.session import close_db, init_db
from app.services.ai import tts_client, tts_service
from app.services.oauth import configured_providers

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
# These log every driver round-trip at DEBUG, which buries our own output.
for noisy in ("aiosqlite", "asyncio", "aiosmtplib"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("synora")

DESCRIPTION = """
Authentication for **Synora AI**.

### Registering takes two steps

1. `POST /auth/register` — email + password. The account is stored **unverified**
   and a 6-digit code is mailed to the address.
2. `POST /auth/verify-otp` — the code. The account is activated and the response
   already contains the tokens, so the user lands signed in.

`POST /auth/resend-otp` issues a new code between the two steps.

### Or sign in with a provider

`GET /auth/oauth/providers` lists what this server has credentials for.

* **Google, GitHub** — `GET /auth/oauth/{provider}/authorize` returns a consent
  URL; the redirect comes back with `code` and `state`, and
  `POST /auth/oauth/{provider}/callback` turns those into the same token pair.
* **Telegram** — no redirect: render the login widget and post what it gives
  you to `POST /auth/oauth/telegram/callback`.

A verified provider email joins the account that already holds it, so signing
up with a password and later using Google lands on one account. An account
created through a provider has no password (`has_password: false`) and, for
Telegram, no email either.

### Tokens

`login`, `verify-otp` and `refresh` all return an `access_token` (short-lived)
and a `refresh_token`. Send the access token as `Authorization: Bearer <token>`.

### Credits

The AI services are metered and paid for from a prepaid balance held in
**micro-credits** — `1 credit = 1 000 000 micros`. That scale exists because a
single LLM token can cost a small fraction of a credit; anything coarser would
round individual charges to nothing. Every amount is also returned as a
fixed-point string, because a JavaScript client should not be doing money
arithmetic on a parsed float.

* `GET /wallet` — what can be spent right now, and why.
* `GET /wallet/transactions` — every movement, newest first, cursor-paginated.

A balance has two parts. **Paid** credit never expires. **Bonus** credit can,
and is always spent first for that reason. **Reserved** credit is committed to
a session that has not settled yet — held, not spent — which is what stops two
concurrent calls from spending the same som.

A charge that runs past the balance mid-call is refused with **402**, carrying
`shortfallMicros` so the client can say how much to top up rather than making
the user guess.

### Text to speech

`POST /tts/speech` streams audio back as the GPU produces it. It is a gateway,
not a redirect: our credential with the speech service never leaves this server,
so every character is metered here and the browser never learns that a supplier
exists.

The charge is the length of the text — all of it, including when the connection
drops halfway through, because by then the whole text has already been sent to
the GPU. That is what makes the price knowable before the first byte, and it is
why the price is on the response itself: `X-Synora-Characters`,
`X-Synora-Price-Micros` and `X-Synora-Session-Id`, the last being the id the
hold and the debit appear under in `GET /wallet/transactions`.

* `POST /tts/estimate` — what text would cost, holding nothing.
* `GET /tts/voices` — built-ins and clones. Shared across this deployment, since
  the whole server synthesises through one upstream account.
* `POST /tts/batch` — a whole corpus, priced and held for up front, then polled
  with `GET /tts/batch/{job_id}`. Billed at what the speech service reports
  synthesising, so items that failed cost nothing. **Poll a job until
  `is_terminal`**: reading it is what advances it on a deployment with no
  worker, and a job nobody reads again keeps its hold.
* `GET /usage` — your own consumption by service and metric, summed from the
  very rows the charges were priced from.

### Errors

Every non-2xx body has the same shape — `detail`, `statusMessage` (the same
text, where the Nuxt frontend reads it) and a stable `code` such as
`otp_expired`, `insufficient_balance` or `email_already_registered`, which is
what clients should branch on.
"""

TAGS = [
    {"name": "Auth", "description": "Registration, email verification and sign-in."},
    {"name": "OAuth", "description": "Sign in with Google, GitHub or Telegram, and link providers."},
    {
        "name": "Wallet",
        "description": "Prepaid credit: the balance, and the statement behind it.",
    },
    {
        "name": "TTS",
        "description": "Metered speech synthesis: streaming, batch jobs and voices.",
    },
    {
        "name": "Usage",
        "description": "What you have consumed, counted on our side of the gateway.",
    },
    {
        "name": "Admin",
        "description": "Superuser-only. Manual credit, freezing a wallet, reconciliation.",
    },
    {
        "name": "Internal",
        "description": "Called by the AI microservices, not by browsers. "
        "HMAC-signed; see docs/INTERNAL_API.md.",
    },
    {"name": "Health", "description": "Liveness probe."},
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.assert_production_ready()
    await init_db()
    # ASCII only: Windows consoles default to a codepage that mangles dashes.
    logger.info("Synora API ready (environment=%s)", settings.environment)
    if settings.expose_otp:
        logger.warning("EXPOSE_DEV_OTP is on: verification codes are returned in API responses.")
    if not settings.smtp_host:
        logger.warning("SMTP is not configured: verification codes are printed to this console.")

    enabled = [provider.label for provider in configured_providers()]
    logger.info("OAuth providers: %s", ", ".join(enabled) if enabled else "none configured")
    # Logged for the same reason the providers are: half a configuration is the
    # commonest deployment mistake, and "the speech routes 503 and nobody knows
    # why" is a support ticket this line answers at boot. The base URL carries
    # no credential, so it is safe to print; the AMQP URL carries a password,
    # so only its presence is.
    logger.info(
        "TTS gateway: %s",
        settings.tts_base_url if settings.has_tts else "not configured (speech routes answer 503)",
    )
    logger.info(
        "Batch queue: %s",
        "RabbitMQ" if settings.has_broker else "not configured (batch jobs submit inline)",
    )
    # The one place a switched-off scrape endpoint explains itself. The
    # endpoint answers 404 rather than saying why, so without this line the
    # symptom is a Prometheus target that has been down since a deploy and a
    # dashboard nobody trusts.
    logger.info("Metrics: %s", settings.metrics_status)
    yield
    # Shutdown is ordered by what each step still needs to be alive, and the
    # settlements go first. `tts_service` finishes a stream's billing in a
    # detached task, because a client disconnect reaches us as a cancellation
    # and an awaited settlement would be cancelled along with the request it
    # belongs to. Nothing else ever awaits those tasks, so a deploy landing
    # mid-stream used to drop them: the session stayed ACTIVE with the hold
    # still on it, and the customer's credit was frozen until
    # `reconcile_service.reap_expired_sessions` came past. The reaper is the
    # backstop for a process that died; it is not an excuse for one that is
    # shutting down politely. Draining has to happen before both closes — a
    # settlement writes to the database, and it may still be sitting inside the
    # upstream request whose client is closed on the next line.
    #
    # `drain_settlements` waits on those tasks without owning them: it observes
    # a deadline and never cancels, because a cancelled settlement is a lost
    # charge and a silent one — `CancelledError` is a `BaseException` and slips
    # straight past `_finalise`'s handler, taking the log line that names the
    # stranded session with it. So a straggler past the bound is still running
    # when this returns, which is exactly why the three closes below are the
    # last thing in this function and not the first: every second they take is
    # another second that settlement has to land in.
    drained = await tts_service.drain_settlements()
    if drained:
        logger.info("Waited for up to %d in-flight settlement(s).", drained)
    # Both hold sockets and both are built lazily on first use, so both may be
    # nothing at all; closing them is idempotent either way. Ordered before
    # `close_db` because a settlement in flight still needs a database — and one
    # that outlives even this fails against a disposed pool, logs
    # `tts_settle_failed`, and leaves its hold to
    # `reconcile_service.reap_expired_sessions`. Late, rather than lost.
    await tts_client.aclose_client()
    await close_broker()
    await close_db()


app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    version="1.0.0",
    openapi_tags=TAGS,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    swagger_ui_parameters={"persistAuthorization": True, "docExpansion": "list"},
)

register_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # `allow_headers` is about the *request*; it does nothing for the response.
    # A browser hands `fetch` only the CORS-safelisted response headers unless
    # they are named here, so without this the price, the character count and
    # the session id on a `/tts/speech` response are simply `undefined` — no
    # exception, nothing in the console, and the header plainly visible in the
    # network tab, which is as silent as a failure gets.
    expose_headers=list(tts_service.EXPOSED_HEADERS),
)

# Outermost of the two, so the time it records includes CORS and every
# exception handler — the latency a caller actually experiences, not the
# latency of our endpoint function. Raw ASGI rather than an HTTP middleware,
# for the reason spelled out on the class.
app.add_middleware(MetricsMiddleware)

app.include_router(api_router, prefix=settings.api_prefix)
# Mounted outside `api_prefix` on purpose: the microservice surface gets its own
# path prefix so nginx can allowlist `location /internal/` at the edge, which is
# far harder to get wrong than a list of route names.
app.include_router(internal_router, prefix="/internal/v1")


@app.get("/health", tags=["Health"], summary="Liveness check")
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus exposition. Not in the OpenAPI document, and not a JSON route.

    Off the API prefix on purpose: `/api/v1/metrics` would sit inside the
    surface a customer's token is meant to reach, and this is not a customer
    endpoint. Off the `/internal` prefix too, because Prometheus does not sign
    requests the way a microservice does — the token below is the whole of its
    authentication, which is why `assert_production_ready` insists on one.

    `METRICS_TOKEN` is compared with `compare_digest`: a `==` on a secret
    leaks its length and its first differing byte to anyone who can time the
    two responses, and there is no reason to be the exception. Without a token
    outside development the endpoint is simply absent, which is the whole of
    the protection and costs a deployment nothing when it is not configured.
    """
    if not settings.serves_metrics:
        # 404 rather than 503, and the reason is on the startup log instead of
        # in this response: an endpoint that is off should look like one that
        # was never built, or a scanner learns that it exists and will start
        # answering as soon as somebody configures it. `serves_metrics` is
        # also why a missing token is not a boot failure — see the property.
        raise NotFoundError("Not found.", code="not_found")

    expected = settings.metrics_token.strip()
    if expected:
        offered = request.headers.get("Authorization", "")
        scheme, _, credential = offered.partition(" ")
        if scheme.lower() != "bearer" or not compare_digest(credential, expected):
            raise UnauthorizedError(
                "Metrics require a bearer token.", code="metrics_unauthorized"
            )

    if settings.metrics_db_gauges:
        await refresh_db_gauges()

    return Response(content=render(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"name": settings.app_name, "docs": "/docs", "openapi": "/openapi.json"}
