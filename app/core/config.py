from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---------------------------------------------------------------
    app_name: str = "Synora API"
    api_prefix: str = "/api/v1"
    environment: str = "development"
    debug: bool = True
    # Declared rather than detected: uvicorn's worker count cannot be read
    # back reliably, and an honest declared value beats a clever wrong one.
    # Used only by `assert_production_ready`, to refuse a multi-worker boot
    # without Redis.
    worker_count: int = 1

    # --- Database ----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./synora.db"

    # --- Auth --------------------------------------------------------------
    jwt_secret: str = "dev-secret-change-me-before-deploying"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30
    # How long the holder of a verified reset code has to choose a new password.
    reset_token_ttl_minutes: int = 15
    password_min_length: int = 8

    # --- OTP ---------------------------------------------------------------
    otp_length: int = 6
    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60

    # --- OAuth -------------------------------------------------------------
    # How long a sign-in may sit between `/authorize` and `/callback`.
    oauth_state_ttl_minutes: int = 10
    # Exact-match allowlist for the `redirect_uri` a client may ask for. Without
    # it a stolen client id could point the provider at an attacker's page and
    # collect authorization codes there. The first entry is the default.
    oauth_redirect_uris: str = "http://localhost:3000/auth/callback"
    oauth_http_timeout_seconds: float = 15.0

    google_client_id: str | None = None
    google_client_secret: str | None = None

    github_client_id: str | None = None
    github_client_secret: str | None = None

    # The widget signs its payload with sha256(bot_token), so the token is both
    # the credential and the verification key.
    telegram_bot_token: str | None = None
    # Only needed to render the widget; the frontend reads it from
    # `GET /auth/oauth/providers`.
    telegram_bot_username: str | None = None
    telegram_auth_ttl_seconds: int = 86400

    # --- Mail --------------------------------------------------------------
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "Synora <no-reply@synora.ai>"
    smtp_starttls: bool = True

    # Echo the OTP back in the API response. Honoured in development only —
    # `expose_otp` below refuses to do it anywhere else, whatever the .env says.
    expose_dev_otp: bool = True

    # --- Redis -------------------------------------------------------------
    # Empty means "run without Redis": every call site falls back to Postgres,
    # which is what the test suite exercises. Required once there is more than
    # one worker, because the background-job lease and the SSE fan-out both
    # depend on it — `assert_production_ready` refuses to boot otherwise.
    redis_url: str = ""
    # The box hosts several unrelated projects, so every key is namespaced.
    redis_prefix: str = "synora:"

    # --- AI microservices --------------------------------------------------
    # The TTS box holds a live upstream key and is reached through a tunnel.
    # That key never leaves this process: the browser calls our route with its
    # own JWT, we hold, stream and settle, and usage stays ours to meter. Empty
    # base url or key means the /tts routes answer a clean 503 rather than a
    # connection error six frames down in httpx.
    tts_base_url: str = ""
    tts_api_key: str = ""
    # The price book row every synthesis is billed against, alongside service
    # "tts". Changing it without seeding a price for the new key does not
    # change the price — it makes `price_cumulative` raise on an unpriced
    # metric, which is the safe direction but is still an outage.
    tts_model_key: str = "synora-tts"
    # Split on purpose. Connecting is either quick or hopeless, while reading a
    # long high-fidelity stream legitimately takes minutes; one shared timeout
    # would either cut paid audio or leave dead sockets pinned to a wallet hold.
    tts_connect_timeout_seconds: float = 10.0
    tts_read_timeout_seconds: float = 300.0
    # Upstream's own per-request cap, restated here so the refusal is ours and
    # arrives before a hold is placed that would then have to be released.
    tts_max_characters: int = 5_000
    # Concurrent syntheses per user. Counted in Redis, so without REDIS_URL
    # there is no cap at all — see `app/core/cache.py`.
    tts_max_concurrent_per_user: int = 3
    # Far below upstream's 5 000 items, and deliberately so: one job is one hold
    # against one wallet, and a job large enough to reserve someone's whole
    # balance for hours is a support ticket rather than a feature.
    tts_batch_max_items: int = 500
    tts_batch_max_characters: int = 500_000
    # How often a submitted job is polled, and how long it may run before it is
    # declared expired and settled at the last usage upstream reported. Six
    # hours is longer than the largest job we accept takes on one card.
    tts_batch_poll_seconds: int = 10
    tts_batch_max_poll_seconds: int = 21_600

    # --- RabbitMQ ----------------------------------------------------------
    # Optional exactly as Redis is: empty means batch jobs are submitted inline
    # by the request that created them and the worker is not needed. The test
    # suite runs that way, so the no-broker path is exercised, not merely
    # intended.
    rabbitmq_url: str = ""
    # The box hosts several unrelated projects, so exchanges and queues are
    # namespaced for the same reason the Redis keys are.
    rabbitmq_prefix: str = "synora."
    # Admission control, not throughput. This is the number of batch items in
    # flight against a single GPU; raising it does not make the card faster, it
    # moves the queue into the card's own scheduler where nothing can reorder,
    # delay or cancel it. A ceiling, not a target.
    rabbitmq_prefetch: int = 4
    # A publish that hangs must not hold up the HTTP response waiting on it.
    # Past this the API gives up on the broker and submits the job inline.
    rabbitmq_publish_timeout_seconds: float = 5.0

    # --- Internal API ------------------------------------------------------
    # The master key every microservice credential is derived from. Same blast
    # radius as JWT_SECRET, and guarded the same way. Never the same value:
    # sharing them would let a metering service mint user sessions.
    internal_key_secret: str = "dev-internal-secret-change-me-before-deploying"
    # How far a signed request's clock may be from ours. Clock skew is the
    # commonest cross-team integration failure, so the error body reports our
    # time rather than leaving the other side guessing.
    internal_signature_window_seconds: int = 300
    # A kill switch for the whole /internal surface, independent of nginx.
    internal_api_enabled: bool = True

    # --- Billing -----------------------------------------------------------
    # Credit granted to a new account on first verification. Zero disables it.
    billing_signup_bonus_micros: int = 0
    billing_signup_bonus_days: int = 30
    # Below this, `GET /v1/wallet` reports `low_balance` and the SSE stream
    # emits a warning. Zero disables it.
    billing_low_balance_micros: int = 0
    # A live call that runs out gets this long, and this much unfunded spend,
    # before it is cut. Whichever runs out first wins. The overrun is written
    # off rather than lent: a prepaid product should not acquire a debt nobody
    # will collect, and keeping the balance non-negative keeps the strongest
    # check constraints in the schema intact.
    billing_grace_seconds: int = 30
    billing_grace_micros: int = 5_000_000
    # How much of a realtime session to hold up front, and the bounds on it.
    billing_hold_seconds: int = 120
    billing_hold_min_micros: int = 1_000_000
    billing_hold_max_micros: int = 500_000_000
    # Extend the hold once this fraction of it has been consumed. A declined
    # extension is the zero-balance signal, which is why it fires early: it
    # gives the warning a hold-window of lead time instead of arriving at the
    # same instant as the disconnect.
    billing_hold_extend_at_percent: int = 70
    # Local day boundary for the usage rollups. A user comparing "today"
    # against their own clock is five hours out if this is UTC.
    billing_rollup_timezone: str = "Asia/Tashkent"

    # --- CORS --------------------------------------------------------------
    # Comma separated so the .env stays readable; "*" allows any origin.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # --- Metrics -----------------------------------------------------------
    # `GET /metrics`, for Prometheus. Recording is unconditional; this switch
    # only decides whether the endpoint answers, because a flag that also
    # silenced the counters would make a metrics bug look like an app bug.
    metrics_enabled: bool = True
    # A bearer token for that endpoint. Empty is fine on a loopback dev box and
    # nowhere else: this API is reached through an nginx `location /` proxy, so
    # a route we add is public the moment it exists, and this one reports call
    # volumes, credit movements and customer counts. Rather than refuse the
    # boot over it — see `serves_metrics` — an untokened deployment simply does
    # not serve the endpoint.
    metrics_token: str = ""
    # The three aggregates `refresh_db_gauges` runs per scrape: held credit,
    # open sessions, open batch jobs. Nothing else can report those, and
    # nothing else notices a hold that never came back.
    metrics_db_gauges: bool = True

    @property
    def is_development(self) -> bool:
        return self.environment.lower() in {"development", "dev", "local"}

    @property
    def expose_otp(self) -> bool:
        return self.expose_dev_otp and self.is_development

    @property
    def oauth_redirect_uri_list(self) -> list[str]:
        return [uri.strip() for uri in self.oauth_redirect_uris.split(",") if uri.strip()]

    @property
    def has_redis(self) -> bool:
        return bool(self.redis_url.strip())

    @property
    def has_tts(self) -> bool:
        # Both halves or neither: a base url with no key is a route that exists
        # and gets a 401 from upstream, which reads to the caller as their fault.
        return bool(self.tts_base_url.strip() and self.tts_api_key.strip())

    @property
    def has_broker(self) -> bool:
        return bool(self.rabbitmq_url.strip())

    @property
    def serves_metrics(self) -> bool:
        """Whether `GET /metrics` answers at all, and the two ways it will not.

        Deliberately not a `assert_production_ready` refusal, which is what
        this was first written as. That version would have failed the next
        production release outright: `METRICS_TOKEN` is a new variable, no
        deployed `.env` has one, and the boot check fires before anything else
        — so a dashboard nobody had asked for yet would have taken down a
        release that changed nothing else. An observability feature must not be
        able to do that. Missing configuration turns the endpoint off and says
        so in the startup log; it never stops the API from serving customers.

        Silent to the outside either way, because the alternative advertises
        it. A `503 metrics_disabled` tells a scanner there is a metrics
        endpoint here and it will start answering once somebody configures it;
        a 404 says nothing at all.
        """
        if not self.metrics_enabled:
            return False
        # No token outside development. Everything this endpoint publishes —
        # request volumes, credits debited, how many wallets exist — is worth
        # exactly one `curl` to a competitor, and Prometheus has supported
        # bearer tokens for a decade.
        if not self.metrics_token.strip() and not self.is_development:
            return False
        # One registry per process, and Prometheus scrapes whichever worker the
        # proxy hands it — counters that halve and double at random are worse
        # than none. See the module docstring of `app/core/metrics.py`.
        return self.worker_count <= 1

    @property
    def metrics_status(self) -> str:
        """One line for the startup log, naming the reason when it is off."""
        if not self.metrics_enabled:
            return "disabled (METRICS_ENABLED=false)"
        if not self.metrics_token.strip() and not self.is_development:
            return "not served (set METRICS_TOKEN; /metrics answers 404 without one)"
        if self.worker_count > 1:
            return "not served (WORKER_COUNT > 1 needs prometheus_client multiprocess mode)"
        if self.metrics_token.strip():
            return "/metrics, bearer token required"
        return "/metrics, no token (development)"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def assert_production_ready(self) -> None:
        """Refuse to boot outside development on a secret anyone could guess.

        Signing tokens with the shipped default would let anyone mint a valid
        session, so this fails loudly at startup rather than quietly serving.
        """
        if self.is_development:
            return

        problems = []
        if self.jwt_secret == Settings.model_fields["jwt_secret"].default:
            problems.append("JWT_SECRET is still the built-in default")
        if len(self.jwt_secret.encode()) < 32:
            problems.append("JWT_SECRET is shorter than 32 bytes")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS allows any origin")

        # Half-configured providers are worse than absent ones: the button
        # appears and then fails at the token exchange.
        for provider, client_id, secret in (
            ("GOOGLE", self.google_client_id, self.google_client_secret),
            ("GITHUB", self.github_client_id, self.github_client_secret),
        ):
            if bool(client_id) != bool(secret):
                problems.append(f"{provider}_CLIENT_ID and {provider}_CLIENT_SECRET must be set together")
        if any(uri == "*" for uri in self.oauth_redirect_uri_list):
            problems.append("OAUTH_REDIRECT_URIS allows any redirect target")

        # Same failure as a half-configured OAuth provider, one layer further
        # out: the /tts routes advertise themselves as available and then every
        # call dies at the upstream's own auth check.
        if bool(self.tts_base_url.strip()) != bool(self.tts_api_key.strip()):
            problems.append("TTS_BASE_URL and TTS_API_KEY must be set together")
        if self.tts_max_characters < 1:
            problems.append("TTS_MAX_CHARACTERS must be positive")

        # SQLite serialises writers and silently ignores `FOR UPDATE`. It is
        # fine for development and for the tests; it is not a money database.
        if self.internal_key_secret == Settings.model_fields["internal_key_secret"].default:
            problems.append("INTERNAL_KEY_SECRET is still the built-in default")
        if len(self.internal_key_secret.encode()) < 32:
            problems.append("INTERNAL_KEY_SECRET is shorter than 32 bytes")
        if self.internal_key_secret == self.jwt_secret:
            problems.append(
                "INTERNAL_KEY_SECRET must differ from JWT_SECRET "
                "(sharing them lets a metering service mint user sessions)"
            )
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL still points at SQLite")
        if self.worker_count > 1 and not self.has_redis:
            problems.append(
                "WORKER_COUNT > 1 requires REDIS_URL "
                "(the background-job lease and the SSE fan-out both need it)"
            )
        if self.billing_grace_micros < 0 or self.billing_grace_seconds < 0:
            problems.append("BILLING_GRACE_* must not be negative")
        if not 1 <= self.billing_hold_extend_at_percent <= 100:
            problems.append("BILLING_HOLD_EXTEND_AT_PERCENT must be between 1 and 100")

        if problems:
            raise RuntimeError(
                f"Refusing to start in '{self.environment}': "
                + "; ".join(problems)
                + ". Generate one with `openssl rand -hex 32`."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
