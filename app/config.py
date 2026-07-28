from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    groq_api_key: str = ""
    secret_key: str
    storage_dir: str = "./storage"
    env: str = "dev"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3_driver(cls, value: str) -> str:
        # Managed Postgres providers (Railway, Supabase, Heroku-style) hand
        # back a bare postgres:// or postgresql:// URL. SQLAlchemy defaults
        # that to psycopg2, which isn't installed here - only psycopg3 is
        # (see requirements.txt). Normalize so the app boots regardless of
        # which provider set DATABASE_URL, without needing every deploy
        # target to know this app's driver choice.
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value


settings = Settings()
