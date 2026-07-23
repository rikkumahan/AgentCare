import functools
import json
import uuid

from app.models import AuditEvent


def audited(action: str, entity_type: str):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(db, *args, **kwargs):
            try:
                result = fn(db, *args, **kwargs)
            except Exception as exc:
                db.rollback()
                db.add(
                    AuditEvent(
                        actor_id=None,
                        action=action,
                        entity_type=entity_type,
                        entity_id=None,
                        event_metadata={"error": str(exc)},
                    )
                )
                db.commit()
                raise

            entity_id = None
            if isinstance(result, dict) and result.get("id"):
                try:
                    entity_id = uuid.UUID(str(result["id"]))
                except ValueError:
                    entity_id = None

            db.add(
                AuditEvent(
                    actor_id=None,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    event_metadata={"result": _json_safe(result)},
                )
            )
            db.commit()
            return result

        return wrapper

    return decorator


def _json_safe(value):
    return json.loads(json.dumps(value, default=str))
