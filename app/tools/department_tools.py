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


def _departments_summary(departments: list[dict]) -> str:
    # Same gotcha as appointment_tools._slots_summary: `content` is the
    # only part of the tool result the model sees again - `artifact` never
    # gets sent back. A bare count gives the model no real names to choose
    # from, so it falls back to pattern-matching the patient's own wording
    # instead of reading actual data - confirmed against the real API.
    if not departments:
        return "Found 0 department(s)."
    lines = [f"- {d['name']}: {d['description'] or 'no description'}" for d in departments]
    return f"Found {len(departments)} department(s):\n" + "\n".join(lines)


@tool(response_format="content_and_artifact")
def lookup_departments_tool(query_hint: str, config: RunnableConfig):
    """List active hospital departments that might match the patient's
    request. query_hint should be a short phrase describing what the
    request is about (e.g. "chest pain follow-up", "general checkup")."""
    db = config["configurable"]["db"]
    result = lookup_departments(db, query_hint)
    return _departments_summary(result), result
