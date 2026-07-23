import os

# Local port-conflict workaround for this machine only (native Windows Postgres holds 5432); a fresh clone without the conflict should use 5432, matching .env.example.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://agentcare:agentcare@localhost:5433/agentcare")
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-prod")

import pytest

from app.db import SessionLocal, engine
from app.models import Base


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session():
    # ponytail: no per-test transaction rollback isolation — tests use
    # unique data instead. Add rollback isolation if parallel test runs
    # are needed later.
    session = SessionLocal()
    yield session
    session.close()
