"""
Tests for admin endpoints.

The admin account is NOT created through /register (that username is
reserved). It is created at app startup from ADMIN_USERNAME / ADMIN_PASSWORD,
which tests/conftest.py sets before the app is imported.
"""
import pytest
from tests.conftest import make_user

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "securepass123"


def _admin_headers(client):
    r = client.post("/api/auth/login", data={
        "username": ADMIN_USERNAME,
        "password": ADMIN_PASSWORD,
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_admin_stats_requires_admin(client, headers):
    """Non-admin users should not access admin stats."""
    r = client.get("/api/admin/stats", headers=headers)
    assert r.status_code == 403


def test_admin_username_cannot_be_registered(client):
    """Registering the admin username must fail, in any letter case."""
    for name in ("admin", "Admin", "ADMIN"):
        r = client.post("/api/auth/register", json={
            "username": name,
            "email": f"{name.lower()}-attacker@example.com",
            "password": "attacker-password-1",
        })
        assert r.status_code == 400, f"{name!r} was accepted"


def test_admin_stats_success(client):
    """The bootstrapped admin account can access stats."""
    r = client.get("/api/admin/stats", headers=_admin_headers(client))
    assert r.status_code == 200
    body = r.json()
    assert "users" in body
    assert "uploads" in body
    assert "storage" in body
    assert "top_uploaders" in body


def test_admin_stats_rate_limit(client):
    """Admin stats endpoint should be rate limited (10/minute)."""
    from limiter_config import limiter
    limiter.reset()
    limiter.enabled = True
    try:
        headers = _admin_headers(client)

        # Make 12 requests quickly
        status_codes = [
            client.get("/api/admin/stats", headers=headers).status_code
            for _ in range(12)
        ]

        # At least one should be 429
        assert 429 in status_codes
    finally:
        # Disable limiter again for other tests
        limiter.enabled = False