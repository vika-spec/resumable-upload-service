"""
Auth service: password hashing with argon2 (winner of Password Hashing Competition)
+ JWT creation/decoding.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone  # ✅ use timezone-aware

from jose import JWTError, jwt
from passlib.context import CryptContext

from logger import get_logger

log = get_logger(__name__)

# Fail fast: never boot with a missing or placeholder signing key, otherwise
# anyone who has read the source could forge tokens.
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY.startswith("REPLACE_WITH"):
    raise RuntimeError(
        "SECRET_KEY is not set. Generate one with:\n"
        '  python -c "import secrets; print(secrets.token_hex(32))"\n'
        "and put it in your .env file or hosting environment variables."
    )

ALGORITHM          = "HS256"
TOKEN_EXPIRY_HOURS = int(os.getenv("TOKEN_EXPIRY_HOURS", "24"))

# The admin account. This username is reserved: it cannot be registered through
# the public /register endpoint, and the account is created at startup from
# ADMIN_PASSWORD (see ensure_admin_user).
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(username: str) -> str:
    expire  = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRY_HOURS)  # ✅ use timezone-aware
    payload = {
        "sub": username,
        "exp": expire,
        "jti": uuid.uuid4().hex,   #  unique ID to guarantee a new token each time
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Raises JWTError on invalid / expired token."""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def is_reserved_username(username: str) -> bool:
    """True if the (case-insensitive) username is the reserved admin name."""
    return username.strip().lower() == ADMIN_USERNAME.strip().lower()


def ensure_admin_user(db) -> None:
    """
    Create the admin account on startup if ADMIN_PASSWORD is set.

    Admin access is granted by username, and that username can't be
    registered publicly, so the only way to hold it is to be created here
    by whoever controls the server's environment. An existing admin account
    is left untouched (its password is not overwritten on restart).
    """
    from models import User  # local import: models -> database, avoids cycles

    password = os.getenv("ADMIN_PASSWORD", "")
    if not password:
        log.warning(
            "admin_account_disabled",
            reason="ADMIN_PASSWORD is not set; /api/admin/* will return 403 for everyone",
        )
        return

    if db.query(User).filter(User.username == ADMIN_USERNAME).first():
        return

    db.add(User(
        username=ADMIN_USERNAME,
        email=os.getenv("ADMIN_EMAIL", f"{ADMIN_USERNAME}@admin.invalid").lower(),
        hashed_password=hash_password(password),
    ))
    db.commit()
    log.info("admin_account_created", username=ADMIN_USERNAME)