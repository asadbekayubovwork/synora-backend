from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.db.session import close_db, init_db
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

### Errors

Every non-2xx body has the same shape — `detail`, `statusMessage` (the same
text, where the Nuxt frontend reads it) and a stable `code` such as
`otp_expired` or `email_already_registered`, which is what clients should
branch on.
"""

TAGS = [
    {"name": "Auth", "description": "Registration, email verification and sign-in."},
    {"name": "OAuth", "description": "Sign in with Google, GitHub or Telegram, and link providers."},
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
    yield
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
)

app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/health", tags=["Health"], summary="Liveness check")
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"name": settings.app_name, "docs": "/docs", "openapi": "/openapi.json"}
