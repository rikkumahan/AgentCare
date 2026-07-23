import os

# Local port-conflict workaround for this machine only (native Windows Postgres holds 5432); a fresh clone without the conflict should use 5432, matching .env.example.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://agentcare:agentcare@localhost:5433/agentcare")
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-prod")

import pytest
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.models import Base


def _drop_alembic_version():
    # alembic_version isn't part of Base.metadata (Alembic owns it, not the
    # ORM), so Base.metadata.drop_all/create_all never touches it. Left
    # alone, it survives this fixture's drop_all and keeps pointing at
    # "head" even though the tables below were just wiped — the next
    # `alembic upgrade head` then no-ops on a database with no tables at
    # all. Drop it explicitly so schema state and Alembic's bookkeeping
    # never disagree.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(bind=engine)
    _drop_alembic_version()
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    _drop_alembic_version()


@pytest.fixture()
def db_session():
    # ponytail: no per-test transaction rollback isolation — tests use
    # unique data instead. Add rollback isolation if parallel test runs
    # are needed later.
    session = SessionLocal()
    yield session
    session.close()
