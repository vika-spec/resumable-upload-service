"""
Rate limiting tests.
Each test explicitly re-enables the limiter and resets counters for its duration.
"""
import pytest
from limiter_config import limiter


@pytest.fixture(autouse=True)
def enable_limiter():
    """Enable rate limiter + fresh counters only for tests in this module."""
    limiter._storage.reset()   # clear any counts from previous tests
    limiter.enabled = True
    yield
    limiter.enabled = False    # restore disabled state for all other test modules
    limiter._storage.reset()   # clear counts so upload tests aren't affected


def test_login_rate_limit(client):
    """
    POST /api/auth/login is capped at 10/minute per IP.
    The 11th rapid attempt must return 429.
    """
    client.post("/api/auth/register", json={
        "username": "rl_login_user",
        "email":    "rl_login@test.com",
        "password": "testpass123",
    })
    status_codes = [
        client.post(
            "/api/auth/login",
            data={"username": "rl_login_user", "password": "wrong"},
        ).status_code
        for _ in range(12)
    ]
    assert 429 in status_codes, f"Expected 429 after 10 rapid login attempts, got: {status_codes}"


def test_create_upload_rate_limit(client):
    """
    POST /api/uploads is capped at 5/minute per IP.
    The 6th attempt must return 429.
    """
    from services.auth_service import create_access_token
    client.post("/api/auth/register", json={
        "username": "rl_upload_user",
        "email":    "rl_upload@test.com",
        "password": "testpass123",
    })
    headers = {"Authorization": f"Bearer {create_access_token('rl_upload_user')}"}

    status_codes = []
    # Use valid chunk size (≥ 1MB) so the request passes validation
    for i in range(7):
        r = client.post("/api/uploads", json={
            "filename": f"f{i}.bin",
            "size": 1048576,        # 1MB
            "chunk_size": 1048576,  # 1MB
        }, headers=headers)
        status_codes.append(r.status_code)
        # Clean up if created (to avoid affecting other tests, though not critical)
        if r.status_code == 201:
            upload_id = r.json()["upload_id"]
            client.delete(f"/api/uploads/{upload_id}", headers=headers)

    assert 429 in status_codes, f"Expected 429 after 5 rapid upload creations, got: {status_codes}"