"""
Reusable FastAPI dependencies.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from database import get_db
from models import User
from services.auth_service import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def get_current_user(
    token: str     = Depends(oauth2_scheme),
    db:   Session  = Depends(get_db),
) -> User:
    """
    Validate the Bearer token and return the matching User.
    Raises HTTP 401 on any auth failure.
    """
    credentials_error = HTTPException(
        status_code = status.HTTP_401_UNAUTHORIZED,
        detail      = "Invalid or expired token",
        headers     = {"WWW-Authenticate": "Bearer"},
    )
    try:
        payload  = decode_token(token)
        username: str = payload.get("sub")
        if not username:
            raise credentials_error
    except JWTError:
        raise credentials_error

    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise credentials_error
    return user
