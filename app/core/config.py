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

    # --- CORS --------------------------------------------------------------
    # Comma separated so the .env stays readable; "*" allows any origin.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

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
