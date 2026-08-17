"""
Shared test fixtures.

Rate limiter is DISABLED globally during tests.
Only test_rate_limiting.py re-enables it for its own scope.
"""
import os
import uuid
import time

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_uploads.db")
os.environ.setdefault("SECRET_KEY",   "test-secret-key-not-for-production")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, close_all_sessions

from database import Base, get_db
from main import app
from limiter_config import limiter          # limiter is the slowapi Limiter instance
from services.auth_service import create_access_token, hash_password

# Disable rate limiting for all tests by default
# (test_rate_limiting.py re-enables it per-test)
limiter.enabled = False

TEST_DB_URL    = os.environ["DATABASE_URL"]
test_engine    = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    # Create all tables
    Base.metadata.create_all(bind=test_engine)
    yield
    # Cleanup after all tests
    # 1. Close all database sessions
    close_all_sessions()
    
    # 2. Dispose the engine to close all connections
    test_engine.dispose()
    
    # 3. Wait a moment for connections to fully close
    time.sleep(0.2)
    
    # 4. Drop all tables
    Base.metadata.drop_all(bind=test_engine)
    
    # 5. Remove the database file
    try:
        if os.path.exists("test_uploads.db"):
            os.remove("test_uploads.db")
    except PermissionError:
        # If file is still locked, try one more time
        time.sleep(0.5)
        try:
            if os.path.exists("test_uploads.db"):
                os.remove("test_uploads.db")
        except PermissionError:
            # Ignore if still can't delete - it will be cleaned up on next run
            pass


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def make_user(client: TestClient) -> tuple[str, str, dict]:
    """
    Register a unique user and return (username, password, auth_headers).
    Token is minted directly via the auth service — not via the
    rate-limited /login endpoint — so rate-limit tests don't bleed here.
    """
    username = f"user_{uuid.uuid4().hex[:8]}"
    password = "Passw0rd!secure"
    client.post("/api/auth/register", json={
        "username": username,
        "email":    f"{username}@test.com",
        "password": password,
    })
    token   = create_access_token(username)   # bypass /login endpoint
    headers = {"Authorization": f"Bearer {token}"}
    return username, password, headers


@pytest.fixture(scope="module")
def auth(client):
    return make_user(client)


@pytest.fixture(scope="module")
def headers(auth):
    return auth[2]