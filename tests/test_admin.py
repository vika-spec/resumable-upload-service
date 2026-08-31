"""
Tests for admin endpoints.
"""
import pytest
from tests.conftest import make_user


def test_admin_stats_requires_admin(client, headers):
    """Non-admin users should not access admin stats."""
    r = client.get("/api/admin/stats", headers=headers)
    assert r.status_code == 403


def test_admin_stats_success(client):
    """Admin should access stats."""
    # Register and login as admin
    client.post("/api/auth/register", json={
        "username": "admin",
        "email": "admin@example.com",
        "password": "securepass123",
    })
    r = client.post("/api/auth/login", data={
        "username": "admin",
        "password": "securepass123",
    })
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    # Access admin stats
    r = client.get("/api/admin/stats", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "users" in body
    assert "uploads" in body
    assert "storage" in body
    assert "top_uploaders" in body


def test_admin_stats_rate_limit(client):
    """Admin stats endpoint should be rate limited (10/minute)."""
    from limiter_config import limiter
    # Enable rate limiter and reset counters
    limiter.reset()          # ✅ FIXED
    limiter.enabled = True
    
    # Register the admin user (must match ADMIN_USERNAME env var, default is "admin")
    admin_username = "admin"
    client.post("/api/auth/register", json={
        "username": admin_username,
        "email": "admin@example.com",
        "password": "securepass123",
    })
    r = client.post("/api/auth/login", data={
        "username": admin_username,
        "password": "securepass123",
    })
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    # Make 12 requests quickly
    status_codes = []
    for _ in range(12):
        r = client.get("/api/admin/stats", headers=headers)
        status_codes.append(r.status_code)
    
    # At least one should be 429
    assert 429 in status_codes
    
    # Disable limiter again for other tests
    limiter.enabled = False