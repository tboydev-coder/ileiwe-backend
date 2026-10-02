from pathlib import Path
from pydantic import model_validator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_ROOT / ".env", extra="ignore")
    app_name: str = "ile-iwe"
    app_env: str = "development"
    port: int = 8000
    forwarded_allow_ips: str = "127.0.0.1"
    database_url: str = "sqlite:///./ile-iwe.db"
    auto_migrate: bool = True
    jwt_secret_key: str = "local-development-only-replace-with-a-long-random-secret"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30
    temporary_password_expire_hours: int = Field(default=48, ge=1, le=168)
    frontend_url: str = "http://127.0.0.1:5173"
    cors_origins: str = "http://127.0.0.1:5173"
    allow_school_registration: bool = True
    require_school_approval: bool = False
    local_storage_path: str = "./storage"
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@localhost"
    smtp_from_name: str = "ile-iwe"
    smtp_use_tls: bool = True
    log_level: str = "INFO"

    @model_validator(mode="after")
    def resolve_local_paths(self):
        # Launching from another directory must not create databases/storage there.
        from sqlalchemy.engine import make_url

        url = make_url(self.database_url)
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            path = Path(url.database)
            if not path.is_absolute():
                self.database_url = url.set(database=(BACKEND_ROOT / path).resolve().as_posix()).render_as_string(
                    hide_password=False
                )
        storage = Path(self.local_storage_path)
        if not storage.is_absolute():
            self.local_storage_path = str((BACKEND_ROOT / storage).resolve())
        return self

    @model_validator(mode="after")
    def production_safety(self):
        if self.app_env == "production":
            if len(self.jwt_secret_key) < 40 or self.jwt_secret_key.startswith("local-development"):
                raise ValueError("Set JWT_SECRET_KEY to a random secret of at least 40 characters.")
            if not self.database_url.startswith(("postgres://", "postgresql")):
                raise ValueError("Production requires PostgreSQL.")
            if not self.frontend_url.startswith("https://"):
                raise ValueError("Production FRONTEND_URL must use HTTPS.")
        return self


def get_settings():
    return Settings()
