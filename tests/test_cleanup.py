"""
Tests for the cleanup worker.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

from models import Upload, UploadStatus
from tests.conftest import TestingSession
from workers.cleanup import _run_cleanup


def _insert_upload(status, expires_delta):
    uid = f"up_test_{uuid.uuid4().hex[:10]}"
    db = TestingSession()
    db.add(Upload(
        upload_id=uid,
        user_id=None,
        filename="x.bin",
        size=1048576,
        chunk_size=1048576,
        total_chunks=1,
        status=status,
        expires_at=datetime.utcnow() + expires_delta,
    ))
    db.commit()
    db.close()
    return uid


def _get(uid):
    db = TestingSession()
    try:
        row = db.query(Upload).filter(Upload.upload_id == uid).first()
        return row.status, row.expires_at
    finally:
        db.close()


def test_cleanup_expires_stale_upload():
    uid = _insert_upload(UploadStatus.uploading, timedelta(hours=-1))
    asyncio.run(_run_cleanup())
    status, _ = _get(uid)
    assert status == UploadStatus.expired


def test_cleanup_leaves_fresh_upload_alone():
    uid = _insert_upload(UploadStatus.uploading, timedelta(hours=1))
    asyncio.run(_run_cleanup())
    status, _ = _get(uid)
    assert status == UploadStatus.uploading


def test_cleanup_recovers_upload_stuck_in_assembling():
    """
    If the server restarted mid-assembly, the upload must not stay in
    "assembling" forever: it is handed back as "uploading" with a new expiry.
    """
    uid = _insert_upload(UploadStatus.assembling, timedelta(minutes=-5))
    asyncio.run(_run_cleanup())
    status, expires_at = _get(uid)
    assert status == UploadStatus.uploading
    assert expires_at > datetime.utcnow()


def test_cleanup_does_not_touch_active_assembly():
    """An assembly still inside its deadline must be left alone."""
    uid = _insert_upload(UploadStatus.assembling, timedelta(minutes=30))
    asyncio.run(_run_cleanup())
    status, _ = _get(uid)
    assert status == UploadStatus.assembling