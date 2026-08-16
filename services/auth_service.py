"""
Auth service: password hashing with argon2 (winner of Password Hashing Competition)
+ JWT creation/decoding.
"""
import os
from datetime import datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

SECRET_KEY         = os.getenv("SECRET_KEY", "dev-secret-CHANGE-IN-PRODUCTION")
ALGORITHM          = "HS256"
TOKEN_EXPIRY_HOURS = int(os.getenv("TOKEN_EXPIRY_HOURS", "24"))

# Argon2: winner of the Password Hashing Competition (2015).
# Far stronger than bcrypt or sha256_crypt against GPU cracking attacks.
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(username: str) -> str:
    expire  = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRY_HOURS)
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Raises JWTError on invalid / expired token."""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
