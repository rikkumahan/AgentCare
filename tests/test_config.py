def test_settings_load_from_explicit_env_vars(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("SECRET_KEY", "abc123")

    from app.config import Settings

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://u:p@localhost:5432/db"
    assert settings.secret_key == "abc123"
    assert settings.storage_dir == "./storage"
    assert settings.env == "dev"


def test_settings_normalizes_bare_postgres_url_to_psycopg3(monkeypatch):
    # Managed Postgres providers (Railway, Supabase, Heroku-style) hand back
    # postgres:// or postgresql:// with no driver suffix - only psycopg3 is
    # installed here (requirements.txt has psycopg[binary], not psycopg2),
    # so SQLAlchemy's default driver resolution would fail to connect.
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host:5432/railway")
    monkeypatch.setenv("SECRET_KEY", "abc123")

    from app.config import Settings

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://u:p@host:5432/railway"
