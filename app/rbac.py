from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth import read_session_token
from app.db import get_db
from app.models import User

SESSION_COOKIE_NAME = "agentcare_session"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user_id = read_session_token(token)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_role(role: str):
    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role.value != role:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Requires {role} role")
        return user

    return _check
