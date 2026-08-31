"""
Tests for all upload endpoints.
Covers: create, chunk upload, idempotency, bad checksum,
        status/progress, complete, cancel, isolation between users.
"""
import hashlib
import pytest
import time
from tests.conftest import make_user
from limiter_config import limiter

# 1MB chunk data (minimum required by API)
CHUNK = b"X" * 1048576


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_upload(client, headers, filename="test.bin", size=1048576, chunk_size=1048576):
    r = client.post("/api/uploads", json={
        "filename":   filename,
        "size":       size,
        "chunk_size": chunk_size,
    }, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _upload_chunk(client, headers, upload_id, chunk_number, data=CHUNK, bad_checksum=False):
    cs = "deadbeef" if bad_checksum else _checksum(data)
    return client.put(
        f"/api/uploads/{upload_id}/chunks/{chunk_number}",
        content=data,
        headers={**headers, "Content-Type": "application/octet-stream", "X-Checksum": cs},
    )


def _cleanup_upload(client, headers, upload_id):
    """Helper to clean up uploads after tests to avoid rate limiting"""
    try:
        client.delete(f"/api/uploads/{upload_id}", headers=headers)
    except:
        pass


def _poll_until_completed(client, headers, upload_id, max_attempts=30):
    """Poll status until completed or failed, with exponential backoff."""
    for attempt in range(max_attempts):
        sleep_time = min(0.1 * (2 ** attempt), 5)  # 0.1, 0.2, 0.4, ... up to 5s
        time.sleep(sleep_time)
        r = client.get(f"/api/uploads/{upload_id}", headers=headers)
        status = r.json()["status"]
        if status in ["completed", "failed", "cancelled"]:
            return r
    return r   # last attempt


# ── Auth guard ─────────────────────────────────────────────────────────────────

def test_create_upload_requires_auth(client):
    r = client.post("/api/uploads", json={"filename": "x.bin", "size": 10, "chunk_size": 10})
    assert r.status_code == 401


def test_list_uploads_requires_auth(client):
    r = client.get("/api/uploads")
    assert r.status_code == 401


# ── Create upload session ──────────────────────────────────────────────────────

def test_create_upload_success(client, headers):
    data = _make_upload(client, headers)
    assert "upload_id" in data
    assert data["total_chunks"] == 1
    assert data["chunk_size"] == 1048576
    assert "expires_at" in data
    _cleanup_upload(client, headers, data["upload_id"])


def test_create_upload_calculates_chunks_correctly(client, headers):
    # 3MB file with 1MB chunks = 3 chunks
    data = _make_upload(client, headers, size=1048576*3, chunk_size=1048576)
    assert data["total_chunks"] == 3
    _cleanup_upload(client, headers, data["upload_id"])


# ── Chunk upload ───────────────────────────────────────────────────────────────

def test_upload_chunk_success(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    r = _upload_chunk(client, headers, uid, 1)
    assert r.status_code == 200
    body = r.json()
    assert body["chunk_number"] == 1
    assert body["status"] == "uploaded"
    assert body["already_uploaded"] is False
    _cleanup_upload(client, headers, uid)


def test_upload_chunk_idempotency(client, headers):
    """Uploading the same chunk twice must return already_uploaded=True, not error."""
    uid = _make_upload(client, headers)["upload_id"]
    _upload_chunk(client, headers, uid, 1)
    r = _upload_chunk(client, headers, uid, 1)   # second time
    assert r.status_code == 200
    assert r.json()["already_uploaded"] is True
    _cleanup_upload(client, headers, uid)


def test_upload_chunk_bad_checksum_rejected(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    r = _upload_chunk(client, headers, uid, 1, bad_checksum=True)
    assert r.status_code == 400
    assert "mismatch" in r.json()["detail"].lower()
    _cleanup_upload(client, headers, uid)


def test_upload_invalid_chunk_number(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    r = _upload_chunk(client, headers, uid, 99)   # only 1 chunk expected
    assert r.status_code == 400
    _cleanup_upload(client, headers, uid)


# ── Status / progress ──────────────────────────────────────────────────────────

def test_status_before_any_chunks(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    r = client.get(f"/api/uploads/{uid}", headers=headers)
    body = r.json()
    assert body["progress"] == 0.0
    assert body["uploaded_chunks"] == 0
    assert body["missing_chunks"] == [1]
    assert body["status"] == "uploading"
    _cleanup_upload(client, headers, uid)


def test_status_after_chunk_uploaded(client, headers):
    # 2-chunk file (2MB file with 1MB chunks)
    uid = _make_upload(client, headers, size=1048576*2, chunk_size=1048576)["upload_id"]
    _upload_chunk(client, headers, uid, 1)
    r = client.get(f"/api/uploads/{uid}", headers=headers)
    body = r.json()
    assert body["uploaded_chunks"] == 1
    assert body["missing_chunks"] == [2]
    assert body["progress"] == 50.0
    _cleanup_upload(client, headers, uid)


# ── Complete upload (background) ──────────────────────────────────────────────

def test_complete_upload_success(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    _upload_chunk(client, headers, uid, 1)

    r = client.post(f"/api/uploads/{uid}/complete", headers=headers)
    assert r.status_code == 202   # now always returns 202
    body = r.json()
    assert body["status"] == "assembling"

    # Poll until completed
    final_resp = _poll_until_completed(client, headers, uid)
    assert final_resp.json()["status"] == "completed"
    final_hash = final_resp.json()["final_hash"]
    assert final_hash is not None
    assert len(final_hash) == 64   # SHA-256 hex

    _cleanup_upload(client, headers, uid)


def test_complete_upload_with_missing_chunks_fails(client, headers):
    # 2-chunk upload but only upload 1 chunk (2MB file with 1MB chunks)
    uid = _make_upload(client, headers, size=1048576*2, chunk_size=1048576)["upload_id"]
    _upload_chunk(client, headers, uid, 1)
    r = client.post(f"/api/uploads/{uid}/complete", headers=headers)
    assert r.status_code == 400
    assert "missing" in r.json()["detail"].lower()
    _cleanup_upload(client, headers, uid)


def test_complete_upload_with_file_hash_verification(client, headers):
    expected_hash = _checksum(CHUNK)
    r = client.post("/api/uploads", json={
        "filename":   "verified.bin",
        "size":       len(CHUNK),
        "chunk_size": len(CHUNK),
        "file_hash":  expected_hash,
    }, headers=headers)
    uid = r.json()["upload_id"]
    _upload_chunk(client, headers, uid, 1)
    r = client.post(f"/api/uploads/{uid}/complete", headers=headers)
    assert r.status_code == 202
    # Poll until completed
    final_resp = _poll_until_completed(client, headers, uid)
    assert final_resp.json()["status"] == "completed"
    assert final_resp.json()["final_hash"] == expected_hash
    _cleanup_upload(client, headers, uid)


# ── Cancel upload ──────────────────────────────────────────────────────────────

def test_cancel_upload(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    r = client.delete(f"/api/uploads/{uid}", headers=headers)
    assert r.status_code == 200

    # Subsequent chunk upload should fail (upload no longer exists)
    r2 = _upload_chunk(client, headers, uid, 1)
    # API returns 404 because the upload was deleted
    assert r2.status_code == 404


# ── List uploads ───────────────────────────────────────────────────────────────

def test_list_uploads_returns_own_uploads_only(client, headers):
    """User A must not see User B's uploads."""
    # Create upload as user A
    uid = _make_upload(client, headers, filename="userA_file.bin")["upload_id"]

    # Create a second user B
    _, _, headers_b = make_user(client)
    _make_upload(client, headers_b, filename="userB_file.bin")

    # User A's list should not contain userB_file.bin
    r = client.get("/api/uploads", headers=headers)
    assert r.status_code == 200
    data = r.json()
    items = data.get("items", [])
    filenames = [u["filename"] for u in items]
    assert "userA_file.bin" in filenames
    assert "userB_file.bin" not in filenames
    _cleanup_upload(client, headers, uid)


def test_list_uploads_404_for_other_users_upload(client, headers):
    """User B cannot fetch status of User A's upload."""
    uid = _make_upload(client, headers)["upload_id"]

    _, _, headers_b = make_user(client)
    r = client.get(f"/api/uploads/{uid}", headers=headers_b)
    assert r.status_code == 404
    _cleanup_upload(client, headers, uid)


# ── Daily upload limit ────────────────────────────────────────────────────────

def test_daily_upload_limit(client):
    """Should enforce daily upload limit."""
    from routes.uploads import QUOTA_DAILY_UPLOADS
    from tests.conftest import make_user

    #  Disable rate limiter to avoid hitting 5/min before daily limit
    limiter.enabled = False

    # Create a fresh user for this test
    _, _, headers = make_user(client)

    upload_ids = []
    # Create QUOTA_DAILY_UPLOADS + 1 uploads (e.g., 21)
    for i in range(QUOTA_DAILY_UPLOADS + 1):
        r = client.post("/api/uploads", json={
            "filename": f"file_{i}.bin",
            "size": 1048576,
            "chunk_size": 1048576,
        }, headers=headers)

        if i >= QUOTA_DAILY_UPLOADS:
            # The (QUOTA_DAILY_UPLOADS+1)-th upload should be rejected
            assert r.status_code == 429
            assert "daily upload limit" in r.json()["detail"].lower()
        else:
            # First QUOTA_DAILY_UPLOADS should succeed
            assert r.status_code == 201
            upload_ids.append(r.json()["upload_id"])

    # Clean up all created uploads after the test
    for uid in upload_ids:
        _cleanup_upload(client, headers, uid)

    # Re-enable limiter for other tests (though conftest will reset)
    limiter.enabled = True