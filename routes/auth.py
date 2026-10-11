"""
Authentication endpoints.

POST /api/auth/register
POST /api/auth/login
GET  /api/auth/me
POST /api/auth/refresh   # <-- New endpoint
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, EmailStr
from limiter_config import limiter
from slowapi.util import get_remote_address
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from logger import get_logger
from models import User
from services.auth_service import (
    create_access_token,
    hash_password,
    is_reserved_username,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)


# ── Schemas ────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserProfile(BaseModel):
    id: int
    username: str
    email: str


# ── Register ───────────────────────────────────────────────────────────────────

@router.post("/register", status_code=201)
@limiter.limit("3/minute")
def register(
    request: Request,
    body: RegisterRequest,
    db: Session = Depends(get_db),
):
    """Create a new user account."""
    # The admin username is reserved so nobody can claim admin by registering it.
    if is_reserved_username(body.username):
        raise HTTPException(status_code=400, detail="Username already taken")

    email = str(body.email).lower()

    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=400, detail="Username already taken")
    # Emails are stored lowercase, so compare case-insensitively.
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        username=body.username,
        email=email,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    db.commit()

    log.info("user_registered", username=body.username, ip=get_remote_address(request))
    return {"message": "Account created successfully", "username": body.username}


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """Login with username/password."""
    user = db.query(User).filter(User.username == form.username).first()
    if not user or not verify_password(form.password, user.hashed_password):
        log.warning("login_failed", username=form.username, ip=get_remote_address(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    log.info("login_success", username=user.username, ip=get_remote_address(request))
    return TokenResponse(access_token=create_access_token(user.username))


# ── Me ────────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserProfile)
@limiter.limit("60/minute")
def me(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return UserProfile(
        id=current_user.id,
        username=current_user.username,
        email=current_user.email,
    )


# ── Refresh Token ─────────────────────────────────────────────────────────────
# <-- NEW ENDPOINT

@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("5/minute")
def refresh_token(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """
    Refresh the access token.
    Returns a new JWT with updated expiry.
    """
    new_token = create_access_token(current_user.username)
    log.info("token_refreshed", username=current_user.username, ip=get_remote_address(request))
    return TokenResponse(access_token=new_token)