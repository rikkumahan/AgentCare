from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Department


@audited("lookup_departments", "Department")
def lookup_departments(db: Session, query_hint: str) -> list[dict]:
    departments = db.query(Department).filter(Department.active.is_(True)).all()

    hint_words = [w for w in (query_hint or "").strip().lower().split() if w]
    if hint_words:
        matched = [
            d
            for d in departments
            if any(w in d.name.lower() or w in (d.description or "").lower() for w in hint_words)
        ]
        if matched:
            departments = matched

    return [{"id": str(d.id), "name": d.name, "description": d.description} for d in departments]


@tool(response_format="content_and_artifact")
def lookup_departments_tool(query_hint: str, config: RunnableConfig):
    """List active hospital departments that might match the patient's
    request. query_hint should be a short phrase describing what the
    request is about (e.g. "chest pain follow-up", "general checkup")."""
    db = config["configurable"]["db"]
    result = lookup_departments(db, query_hint)
    return f"Found {len(result)} department(s)", result
