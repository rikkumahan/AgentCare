from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
# scoped_session, not a plain sessionmaker: LangGraph's ToolNode dispatches
# every tool call through a ThreadPoolExecutor, so any Session reachable
# from a tool must tolerate being touched from a different thread than the
# one that created it. scoped_session hands each thread its own session
# transparently (proxying the full Session API), so callers keep doing
# SessionLocal() / db.query(...) exactly as before - see
# docs/memory/gotchas.md for the failure this fixes.
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        SessionLocal.remove()
