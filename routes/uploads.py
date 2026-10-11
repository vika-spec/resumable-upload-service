"""
Secure resumable upload routes.

All endpoints require JWT authentication.

Abuse/cost controls:

- MAX_FILE_SIZE_GB
- MAX_CHUNK_SIZE_MB
- MIN_CHUNK_SIZE_MB
- QUOTA_USER_STORAGE_GB
- QUOTA_GLOBAL_GB
- QUOTA_DAILY_UPLOADS
- MAX_ACTIVE_UPLOADS_PER_USER
- MAX_ACTIVE_UPLOADS_GLOBAL
"""

import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    BackgroundTasks,  # ✅ Added
)
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db, SessionLocal  # ✅ Added SessionLocal for background task
from deps import get_current_user
from limiter_config import limiter
from logger import get_logger
from models import Upload, UploadChunk, UploadStatus, User
from schemas import (
    ChunkUploadResponse,
    CompleteUploadResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    UploadStatusResponse,
)
from services.storage_service import ChecksumMismatch, StorageService


router = APIRouter()
storage = StorageService()
log = get_logger(__name__)


def _utcnow() -> datetime:
    """Naive UTC "now" for database columns.

    The DateTime columns are timezone-naive and the models/cleanup worker use
    naive UTC, so every value written to or compared against the DB goes
    through here. This avoids tz-aware values being shifted by the database
    server's local timezone on PostgreSQL.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Limits ────────────────────────────────────────────────────────────────────

UPLOAD_EXPIRY_HOURS = int(
    os.getenv("UPLOAD_EXPIRY_HOURS", "2")
)

# How long an upload may stay in "assembling" before the cleanup worker assumes
# the server restarted mid-assembly and hands the upload back to the client.
ASSEMBLY_TIMEOUT_MINUTES = int(
    os.getenv("ASSEMBLY_TIMEOUT_MINUTES", "60")
)

MAX_CHUNK_SIZE = int(
    os.getenv("MAX_CHUNK_SIZE_MB", "50")
) * 1024**2

MIN_CHUNK_SIZE = int(
    os.getenv("MIN_CHUNK_SIZE_MB", "1")
) * 1024**2

MAX_FILE_SIZE = int(
    os.getenv("MAX_FILE_SIZE_GB", "10")
) * 1024**3


# ── Abuse / cost controls ─────────────────────────────────────────────────────

QUOTA_USER_GB = int(
    os.getenv("QUOTA_USER_STORAGE_GB", "1")
)

QUOTA_GLOBAL_GB = int(
    os.getenv("QUOTA_GLOBAL_GB", "40")
)

QUOTA_DAILY_UPLOADS = int(
    os.getenv("QUOTA_DAILY_UPLOADS", "3")
)

MAX_ACTIVE_UPLOADS_PER_USER = int(
    os.getenv("MAX_ACTIVE_UPLOADS_PER_USER", "3")
)

MAX_ACTIVE_UPLOADS_GLOBAL = int(
    os.getenv("MAX_ACTIVE_UPLOADS_GLOBAL", "100")
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_upload_or_404(
    upload_id: str,
    db: Session,
    user: User,
) -> Upload:
    upload = (
        db.query(Upload)
        .filter(
            Upload.upload_id == upload_id,
            Upload.user_id == user.id,
        )
        .first()
    )
    if not upload:
        raise HTTPException(404, f"Upload '{upload_id}' not found")
    return upload


def _get_upload_locked(
    upload_id: str,
    db: Session,
    user: User,
) -> Upload:
    upload = (
        db.query(Upload)
        .filter(
            Upload.upload_id == upload_id,
            Upload.user_id == user.id,
        )
        .with_for_update()
        .first()
    )
    if not upload:
        raise HTTPException(404, f"Upload '{upload_id}' not found")
    return upload


def _assert_active(upload: Upload) -> None:
    terminal = {
        UploadStatus.cancelled,
        UploadStatus.completed,
        UploadStatus.expired,
        UploadStatus.failed,
    }
    if upload.status in terminal:
        raise HTTPException(
            400,
            f"Upload is in terminal state: {upload.status.value}",
        )


def _missing_chunks(upload: Upload) -> list[int]:
    uploaded = {c.chunk_number for c in upload.chunks}
    return sorted(set(range(1, upload.total_chunks + 1)) - uploaded)


def _progress(upload: Upload) -> float:
    if not upload.total_chunks:
        return 0.0
    done = upload.total_chunks - len(_missing_chunks(upload))
    return round(done / upload.total_chunks * 100, 2)


def _lock_global_quota(db: Session) -> None:
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": 918273645},
        )


# ── Background Assembly Function ─────────────────────────────────────────────

async def assemble_upload_in_background(upload_id: str, user_id: int, filename: str) -> None:
    """Background task for file assembly."""
    db = SessionLocal()
    try:
        upload = db.query(Upload).filter(Upload.upload_id == upload_id).first()
        if not upload:
            log.error("upload_not_found_in_background", upload_id=upload_id)
            return

        log.info(
            "background_assembly_started",
            upload_id=upload_id,
            user_id=user_id,
            filename=filename,
            total_chunks=upload.total_chunks,
            size_bytes=upload.size,
        )

        final_hash = await storage.assemble_file(
            upload_id=upload_id,
            total_chunks=upload.total_chunks,
            filename=upload.filename,
        )

        # Verify hash if provided
        hash_verified = None
        if upload.file_hash:
            hash_verified = (final_hash == upload.file_hash.lower())
            if not hash_verified:
                upload.status = UploadStatus.failed
                db.commit()
                log.warning("background_assembly_hash_mismatch", upload_id=upload_id)
                await storage.delete_upload(upload_id)
                return

        upload.status = UploadStatus.completed
        upload.final_hash = final_hash
        upload.completed_at = _utcnow()
        db.commit()

        log.info("background_assembly_completed", upload_id=upload_id, hash=final_hash)

    except Exception as exc:
        log.error("background_assembly_failed", upload_id=upload_id, error=str(exc))
        try:
            upload = db.query(Upload).filter(Upload.upload_id == upload_id).first()
            if upload and upload.status != UploadStatus.completed:
                upload.status = UploadStatus.failed
                db.commit()
        except Exception as db_err:
            log.error("background_assembly_db_error", upload_id=upload_id, error=str(db_err))
    finally:
        db.close()


# ── Create upload ──────────────────────────────────────────────────────────────

@router.post(
    "/uploads",
    response_model=CreateUploadResponse,
    status_code=201,
)
@limiter.limit("5/minute")
async def create_upload(
    request: Request,
    body: CreateUploadRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # File limits
    if body.size > MAX_FILE_SIZE:
        raise HTTPException(400, f"File exceeds maximum allowed size of {os.getenv('MAX_FILE_SIZE_GB', '10')} GB")
    if body.chunk_size < MIN_CHUNK_SIZE:
        raise HTTPException(400, f"chunk_size must be at least {MIN_CHUNK_SIZE // 1024**2} MB")
    if body.chunk_size > MAX_CHUNK_SIZE:
        raise HTTPException(400, f"chunk_size exceeds maximum of {MAX_CHUNK_SIZE // 1024**2} MB")

    _lock_global_quota(db)

    # Daily session limit
    today_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_count = db.query(func.count(Upload.upload_id)).filter(
        Upload.user_id == user.id,
        Upload.created_at >= today_start,
    ).scalar() or 0
    if daily_count >= QUOTA_DAILY_UPLOADS:
        raise HTTPException(429, f"Daily upload limit of {QUOTA_DAILY_UPLOADS} sessions reached.")

    # Active upload limit per user
    active_user_uploads = db.query(func.count(Upload.upload_id)).filter(
        Upload.user_id == user.id,
        Upload.status.in_([UploadStatus.uploading, UploadStatus.assembling]),
    ).scalar() or 0
    if active_user_uploads >= MAX_ACTIVE_UPLOADS_PER_USER:
        raise HTTPException(429, f"Maximum of {MAX_ACTIVE_UPLOADS_PER_USER} active uploads allowed.")

    # Per-user reserved storage
    user_bytes = db.query(func.sum(Upload.size)).filter(
        Upload.user_id == user.id,
        Upload.status.in_([UploadStatus.uploading, UploadStatus.assembling, UploadStatus.completed]),
    ).scalar() or 0
    if user_bytes + body.size > QUOTA_USER_GB * 1024**3:
        raise HTTPException(429, f"Storage quota exceeded. You have approximately {user_bytes / 1024**3:.2f} GB reserved/used out of {QUOTA_USER_GB} GB.")

    # Global active upload count
    active_global_uploads = db.query(func.count(Upload.upload_id)).filter(
        Upload.status.in_([UploadStatus.uploading, UploadStatus.assembling])
    ).scalar() or 0
    if active_global_uploads >= MAX_ACTIVE_UPLOADS_GLOBAL:
        raise HTTPException(503, "Service is temporarily at maximum upload capacity.")

    # Global reserved storage
    global_bytes = db.query(func.sum(Upload.size)).filter(
        Upload.status.in_([UploadStatus.uploading, UploadStatus.assembling, UploadStatus.completed])
    ).scalar() or 0
    if global_bytes + body.size > QUOTA_GLOBAL_GB * 1024**3:
        raise HTTPException(503, "Service is at storage capacity. Please try again later.")

    # Create upload
    upload_id = f"up_{uuid.uuid4().hex[:16]}"
    total_chunks = math.ceil(body.size / body.chunk_size)
    expires_at = _utcnow() + timedelta(hours=UPLOAD_EXPIRY_HOURS)

    upload = Upload(
        upload_id=upload_id,
        user_id=user.id,
        filename=body.filename,
        size=body.size,
        chunk_size=body.chunk_size,
        total_chunks=total_chunks,
        file_hash=body.file_hash,
        expires_at=expires_at,
        status=UploadStatus.uploading,
    )
    db.add(upload)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(500, "Could not create upload session.")

    await storage.create_upload_dir(upload_id)

    log.info("upload_created", upload_id=upload_id, filename=body.filename, user=user.username, chunks=total_chunks, size_bytes=body.size)

    return CreateUploadResponse(
        upload_id=upload_id,
        total_chunks=total_chunks,
        chunk_size=body.chunk_size,
        expires_at=expires_at.replace(tzinfo=timezone.utc),  # explicit UTC for clients
    )


# ── Upload chunk ───────────────────────────────────────────────────────────────

@router.put(
    "/uploads/{upload_id}/chunks/{chunk_number}",
    response_model=ChunkUploadResponse,
)
@limiter.limit("60/minute")
async def upload_chunk(
    upload_id: str,
    chunk_number: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    x_checksum: Optional[str] = Header(None),
):
    upload = _get_upload_locked(upload_id, db, user)
    _assert_active(upload)

    if not (1 <= chunk_number <= upload.total_chunks):
        raise HTTPException(400, f"chunk_number must be between 1 and {upload.total_chunks}")

    # Idempotency
    existing = db.query(UploadChunk).filter(
        UploadChunk.upload_id == upload_id,
        UploadChunk.chunk_number == chunk_number,
    ).first()
    if existing:
        db.rollback()
        return ChunkUploadResponse(chunk_number=chunk_number, status="already_uploaded", already_uploaded=True)

    db.commit()

    # Stream chunk. The checksum is verified inside the storage layer before the
    # chunk is stored, so a corrupted chunk is rejected on its own and the
    # client can simply re-send it; the rest of the upload is untouched.
    try:
        computed, total_size = await storage.save_chunk_from_stream(
            upload_id,
            chunk_number,
            request.stream(),
            MAX_CHUNK_SIZE,
            expected_checksum=x_checksum,
        )
    except ChecksumMismatch as exc:
        log.warning("checksum_mismatch", upload_id=upload_id, chunk=chunk_number)
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # Save record
    db.add(UploadChunk(
        upload_id=upload_id,
        chunk_number=chunk_number,
        size=total_size,
        checksum=computed,
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return ChunkUploadResponse(chunk_number=chunk_number, status="already_uploaded", already_uploaded=True)

    log.debug("chunk_saved", upload_id=upload_id, chunk=chunk_number, bytes=total_size)
    return ChunkUploadResponse(chunk_number=chunk_number, status="uploaded", already_uploaded=False)


# ── Status ─────────────────────────────────────────────────────────────────────

@router.get(
    "/uploads/{upload_id}",
    response_model=UploadStatusResponse,
)
@limiter.limit("60/minute")
async def get_upload_status(
    request: Request,
    upload_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    upload = _get_upload_or_404(upload_id, db, user)
    missing = _missing_chunks(upload)

    return UploadStatusResponse(
        upload_id=upload.upload_id,
        filename=upload.filename,
        size=upload.size,
        status=upload.status,
        total_chunks=upload.total_chunks,
        uploaded_chunks=(upload.total_chunks - len(missing)),
        missing_chunks=missing,
        progress=_progress(upload),
        created_at=upload.created_at,
        completed_at=upload.completed_at,
        final_hash=upload.final_hash,
    )


# ── Complete (Background) ─────────────────────────────────────────────────────

@router.post(
    "/uploads/{upload_id}/complete",
    status_code=202,  # ✅ 202 Accepted – async processing
)
@limiter.limit("10/minute")
async def complete_upload(
    request: Request,
    upload_id: str,
    background_tasks: BackgroundTasks,  # ✅ Added
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Start background assembly and return 202 Accepted."""
    upload = _get_upload_locked(upload_id, db, user)

    if upload.status == UploadStatus.completed:
        db.rollback()
        return {
            "upload_id": upload_id,
            "filename": upload.filename,
            "status": "completed",
            "final_hash": upload.final_hash,
            "message": "Upload already completed",
        }

    if upload.status == UploadStatus.assembling:
        db.rollback()
        return {
            "upload_id": upload_id,
            "status": "assembling",
            "message": "Upload is already being assembled in the background",
            "status_url": f"/api/uploads/{upload_id}",
        }

    _assert_active(upload)

    missing = _missing_chunks(upload)
    if missing:
        db.rollback()
        raise HTTPException(400, f"Missing chunks: {missing[:10]}" + (f" (+{len(missing)-10} more)" if len(missing) > 10 else ""))

    # Mark as assembling and commit. expires_at doubles as this state's
    # deadline: if the server dies mid-assembly the cleanup worker uses it to
    # recover the upload instead of leaving it stuck in "assembling" forever.
    upload.status = UploadStatus.assembling
    upload.expires_at = _utcnow() + timedelta(minutes=ASSEMBLY_TIMEOUT_MINUTES)
    db.commit()

    # Offload assembly to background
    background_tasks.add_task(
        assemble_upload_in_background,
        upload_id=upload_id,
        user_id=user.id,
        filename=upload.filename,
    )

    log.info("upload_assembly_started_background", upload_id=upload_id, user=user.username, filename=upload.filename)

    return {
        "upload_id": upload_id,
        "filename": upload.filename,
        "status": "assembling",
        "message": "File assembly started in background",
        "status_url": f"/api/uploads/{upload_id}",
        "estimated_time": "Varies by file size. Check status endpoint.",
    }


# ── Delete ─────────────────────────────────────────────────────────────────────

@router.delete("/uploads/{upload_id}")
@limiter.limit("20/minute")
async def delete_upload(
    request: Request,
    upload_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    upload = _get_upload_locked(upload_id, db, user)

    if upload.status == UploadStatus.assembling:
        db.rollback()
        raise HTTPException(409, "Upload is currently being assembled.")

    filename = upload.filename

    try:
        await storage.delete_upload(upload_id)
        db.query(UploadChunk).filter(UploadChunk.upload_id == upload_id).delete(synchronize_session=False)
        db.delete(upload)
        db.commit()

        log.info("upload_deleted", upload_id=upload_id, user=user.username, filename=filename)
        return {"message": "Upload deleted permanently", "upload_id": upload_id}

    except Exception as exc:
        db.rollback()
        db.query(Upload).filter(Upload.upload_id == upload_id).update(
            {"status": UploadStatus.failed}
        )
        db.commit()
        log.error("upload_delete_failed", upload_id=upload_id, error=str(exc))
        raise HTTPException(500, "Delete failed.")


# ── Download ───────────────────────────────────────────────────────────────────

@router.get("/uploads/{upload_id}/download")
@limiter.limit("20/minute")
async def download_file(
    request: Request,
    upload_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    upload = _get_upload_or_404(upload_id, db, user)

    if upload.status != UploadStatus.completed:
        raise HTTPException(400, "File not ready for download")

    sas_url = await storage.get_download_url(upload_id, upload.filename)
    if sas_url:
        log.info("download_redirect_sas", upload_id=upload_id, user=user.username)
        return RedirectResponse(sas_url)

    log.info("download_local", upload_id=upload_id, user=user.username)
    return FileResponse(
        path=storage.completed_file_path(upload_id, upload.filename),
        filename=upload.filename,
        media_type="application/octet-stream",
    )


# ── List uploads with Pagination ─────────────────────────────────────────────

@router.get("/uploads")
@limiter.limit("30/minute")
async def list_uploads(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    skip: int = 0,      # ✅ Pagination: skip
    limit: int = 20,    # ✅ Pagination: limit
):
    """List user's uploads with pagination."""
    if limit > 100:
        limit = 100
    if skip < 0:
        skip = 0

    query = db.query(Upload).filter(Upload.user_id == user.id)
    total = query.count()
    uploads = query.order_by(Upload.created_at.desc()).offset(skip).limit(limit).all()

    result = []
    for upload in uploads:
        missing = _missing_chunks(upload)
        uploaded = upload.total_chunks - len(missing)
        result.append({
            "upload_id": upload.upload_id,
            "filename": upload.filename,
            "size": upload.size,
            "status": upload.status.value,
            "progress": _progress(upload),
            "total_chunks": upload.total_chunks,
            "uploaded_chunks": uploaded,
            "created_at": upload.created_at.isoformat(),
            "completed_at": upload.completed_at.isoformat() if upload.completed_at else None,
            "final_hash": upload.final_hash,
        })

    pages = (total + limit - 1) // limit if total > 0 else 0
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "pages": pages,
        "has_next": skip + limit < total,
        "has_previous": skip > 0,
        "items": result,
    }