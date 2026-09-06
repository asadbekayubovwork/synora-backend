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
    password_min_length: int = 8

    # --- OTP ---------------------------------------------------------------
    otp_length: int = 6
    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60

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
