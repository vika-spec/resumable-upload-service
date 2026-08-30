"""
Tests for POST /api/auth/register, POST /api/auth/login, GET /api/auth/me
"""
import pytest
import jwt


# ── Register ───────────────────────────────────────────────────────────────────

def test_register_success(client):
    r = client.post("/api/auth/register", json={
        "username": "alice",
        "email":    "alice@example.com",
        "password": "securepass1",
    })
    assert r.status_code == 201
    assert r.json()["username"] == "alice"


def test_register_duplicate_username(client):
    client.post("/api/auth/register", json={
        "username": "bob", "email": "bob1@example.com", "password": "securepass1"
    })
    r = client.post("/api/auth/register", json={
        "username": "bob", "email": "bob2@example.com", "password": "securepass1"
    })
    assert r.status_code == 400
    assert "Username" in r.json()["detail"]


def test_register_duplicate_email(client):
    client.post("/api/auth/register", json={
        "username": "carol1", "email": "shared@example.com", "password": "securepass1"
    })
    r = client.post("/api/auth/register", json={
        "username": "carol2", "email": "shared@example.com", "password": "securepass1"
    })
    assert r.status_code == 400
    assert "Email" in r.json()["detail"]


def test_register_short_password(client):
    r = client.post("/api/auth/register", json={
        "username": "dave", "email": "dave@example.com", "password": "short"
    })
    assert r.status_code == 422   # pydantic validation


# ── Login ──────────────────────────────────────────────────────────────────────

def test_login_success(client):
    client.post("/api/auth/register", json={
        "username": "eve", "email": "eve@example.com", "password": "securepass1"
    })
    r = client.post("/api/auth/login", data={"username": "eve", "password": "securepass1"})
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 20


def test_login_wrong_password(client):
    client.post("/api/auth/register", json={
        "username": "frank", "email": "frank@example.com", "password": "securepass1"
    })
    r = client.post("/api/auth/login", data={"username": "frank", "password": "wrongpass"})
    assert r.status_code == 401


def test_login_nonexistent_user(client):
    r = client.post("/api/auth/login", data={"username": "nobody", "password": "anything"})
    assert r.status_code == 401


# ── /me ────────────────────────────────────────────────────────────────────────

def test_me_returns_profile(client, headers):
    r = client.get("/api/auth/me", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "username" in body
    assert "email"    in body
    assert "id"       in body


def test_me_without_token_is_401(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_me_with_bad_token_is_401(client):
    r = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.real.token"})
    assert r.status_code == 401

## tests added on the date 30/08/2026 vikash kumar
def test_refresh_token_success(client, auth):
    """Should return a new token when given a valid token."""
    username, password, headers = auth
    r = client.post("/api/auth/refresh", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    # New token should be different from old one
    old_token = headers["Authorization"].replace("Bearer ", "")
    assert body["access_token"] != old_token


def test_refresh_token_rate_limit(client, auth):
    """Refresh endpoint should be rate limited (5/minute)."""
    username, password, headers = auth
    status_codes = []
    for _ in range(7):
        r = client.post("/api/auth/refresh", headers=headers)
        status_codes.append(r.status_code)
    assert 429 in status_codes


def test_refresh_token_with_invalid_token(client):
    """Should reject invalid tokens."""
    r = client.post("/api/auth/refresh", headers={"Authorization": "Bearer invalid_token"})
    assert r.status_code == 401


def test_refresh_token_with_expired_token(client):
    """Should reject expired tokens."""
    # Create expired token (if possible in test)
    # Or just ensure the endpoint validates token properly
    import jwt
    from services.auth_service import SECRET_KEY
    expired_token = jwt.encode(
        {"sub": "testuser", "exp": 0},  # Already expired
        SECRET_KEY,
        algorithm="HS256"
    )
    r = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {expired_token}"})
    assert r.status_code == 401
