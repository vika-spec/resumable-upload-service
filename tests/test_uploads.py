"""
Tests for all upload endpoints.
Covers: create, chunk upload, idempotency, bad checksum,
        status/progress, complete, cancel, isolation between users.
"""
import hashlib
import pytest
from tests.conftest import make_user

CHUNK = b"Hello, world! This is chunk data."  # 33 bytes


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_upload(client, headers, filename="test.bin", size=33, chunk_size=33):
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
        content = data,
        headers = {**headers, "Content-Type": "application/octet-stream", "X-Checksum": cs},
    )


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
    assert "upload_id"    in data
    assert data["total_chunks"] == 1
    assert data["chunk_size"]   == 33
    assert "expires_at"   in data


def test_create_upload_calculates_chunks_correctly(client, headers):
    # 100 bytes / 30 bytes per chunk = ceil(100/30) = 4 chunks
    data = _make_upload(client, headers, size=100, chunk_size=30)
    assert data["total_chunks"] == 4


# ── Chunk upload ───────────────────────────────────────────────────────────────

def test_upload_chunk_success(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    r   = _upload_chunk(client, headers, uid, 1)
    assert r.status_code == 200
    body = r.json()
    assert body["chunk_number"]    == 1
    assert body["status"]          == "uploaded"
    assert body["already_uploaded"] is False


def test_upload_chunk_idempotency(client, headers):
    """Uploading the same chunk twice must return already_uploaded=True, not error."""
    uid = _make_upload(client, headers)["upload_id"]
    _upload_chunk(client, headers, uid, 1)
    r = _upload_chunk(client, headers, uid, 1)   # second time
    assert r.status_code == 200
    assert r.json()["already_uploaded"] is True


def test_upload_chunk_bad_checksum_rejected(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    r   = _upload_chunk(client, headers, uid, 1, bad_checksum=True)
    assert r.status_code == 400
    assert "mismatch" in r.json()["detail"].lower()


def test_upload_invalid_chunk_number(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    r   = _upload_chunk(client, headers, uid, 99)   # only 1 chunk expected
    assert r.status_code == 400


# ── Status / progress ──────────────────────────────────────────────────────────

def test_status_before_any_chunks(client, headers):
    uid  = _make_upload(client, headers)["upload_id"]
    r    = client.get(f"/api/uploads/{uid}", headers=headers)
    body = r.json()
    assert body["progress"]        == 0.0
    assert body["uploaded_chunks"] == 0
    assert body["missing_chunks"]  == [1]
    assert body["status"]          == "uploading"


def test_status_after_chunk_uploaded(client, headers):
    # 2-chunk file
    uid = _make_upload(client, headers, size=66, chunk_size=33)["upload_id"]
    _upload_chunk(client, headers, uid, 1)
    r    = client.get(f"/api/uploads/{uid}", headers=headers)
    body = r.json()
    assert body["uploaded_chunks"] == 1
    assert body["missing_chunks"]  == [2]
    assert body["progress"]        == 50.0


# ── Complete upload ────────────────────────────────────────────────────────────

def test_complete_upload_success(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    _upload_chunk(client, headers, uid, 1)

    r    = client.post(f"/api/uploads/{uid}/complete", headers=headers)
    body = r.json()
    assert r.status_code == 200
    assert body["status"]      == "completed"
    assert body["final_hash"]  is not None
    assert len(body["final_hash"]) == 64   # SHA-256 hex


def test_complete_upload_with_missing_chunks_fails(client, headers):
    # 2-chunk upload but only upload 1 chunk
    uid = _make_upload(client, headers, size=66, chunk_size=33)["upload_id"]
    _upload_chunk(client, headers, uid, 1)
    r = client.post(f"/api/uploads/{uid}/complete", headers=headers)
    assert r.status_code == 400
    assert "missing" in r.json()["detail"].lower()


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
    assert r.status_code == 200
    assert r.json()["hash_verified"] is True


# ── Cancel upload ──────────────────────────────────────────────────────────────

def test_cancel_upload(client, headers):
    uid = _make_upload(client, headers)["upload_id"]
    r   = client.delete(f"/api/uploads/{uid}", headers=headers)
    assert r.status_code == 200

    # Subsequent chunk upload should fail
    r2 = _upload_chunk(client, headers, uid, 1)
    assert r2.status_code == 400


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
    filenames = [u["filename"] for u in r.json()]
    assert "userA_file.bin" in filenames
    assert "userB_file.bin" not in filenames


def test_list_uploads_404_for_other_users_upload(client, headers):
    """User B cannot fetch status of User A's upload."""
    uid = _make_upload(client, headers)["upload_id"]

    _, _, headers_b = make_user(client)
    r = client.get(f"/api/uploads/{uid}", headers=headers_b)
    assert r.status_code == 404
