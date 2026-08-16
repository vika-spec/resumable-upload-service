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
from datetime import datetime, timedelta,timezone
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
)
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
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
from services.storage_service import StorageService


router = APIRouter()
storage = StorageService()
log = get_logger(__name__)


# ── Limits ────────────────────────────────────────────────────────────────────

UPLOAD_EXPIRY_HOURS = int(
    os.getenv("UPLOAD_EXPIRY_HOURS", "2")
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
    os.getenv("QUOTA_DAILY_UPLOADS", "20")
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
        raise HTTPException(
            404,
            f"Upload '{upload_id}' not found",
        )

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
        raise HTTPException(
            404,
            f"Upload '{upload_id}' not found",
        )

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

    uploaded = {
        c.chunk_number
        for c in upload.chunks
    }

    return sorted(
        set(range(1, upload.total_chunks + 1))
        - uploaded
    )

def _progress(upload: Upload) -> float:

    if not upload.total_chunks:
        return 0.0

    done = upload.total_chunks - len(
        _missing_chunks(upload)
    )

    return round(
        done / upload.total_chunks * 100,
        2,
    )


def _lock_global_quota(db: Session) -> None:
    """
    PostgreSQL advisory transaction lock.

    This prevents two different users from simultaneously
    passing the global quota check.

    SQLite development mode simply skips this.
    """

    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": 918273645},
        )


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

    # ── File limits ────────────────────────────────────────────────────────────

    if body.size > MAX_FILE_SIZE:
        raise HTTPException(
            400,
            f"File exceeds maximum allowed size of "
            f"{os.getenv('MAX_FILE_SIZE_GB', '10')} GB",
        )

    if body.chunk_size < MIN_CHUNK_SIZE:
        raise HTTPException(
            400,
            f"chunk_size must be at least "
            f"{MIN_CHUNK_SIZE // 1024**2} MB",
        )

    if body.chunk_size > MAX_CHUNK_SIZE:
        raise HTTPException(
            400,
            f"chunk_size exceeds maximum of "
            f"{MAX_CHUNK_SIZE // 1024**2} MB",
        )

    # ── Lock global quota ──────────────────────────────────────────────────────

    _lock_global_quota(db)

    # ── Daily session limit ────────────────────────────────────────────────────

    today_start = datetime.now(timezone.utc).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    daily_count = (
        db.query(func.count(Upload.upload_id))
        .filter(
            Upload.user_id == user.id,
            Upload.created_at >= today_start,
        )
        .scalar()
        or 0
    )

    if daily_count >= QUOTA_DAILY_UPLOADS:
        raise HTTPException(
            429,
            f"Daily upload limit of "
            f"{QUOTA_DAILY_UPLOADS} sessions reached.",
        )

    # ── Active upload limit per user ───────────────────────────────────────────

    active_user_uploads = (
        db.query(func.count(Upload.upload_id))
        .filter(
            Upload.user_id == user.id,
            Upload.status.in_([
                UploadStatus.uploading,
                UploadStatus.assembling,
            ]),
        )
        .scalar()
        or 0
    )

    if active_user_uploads >= MAX_ACTIVE_UPLOADS_PER_USER:
        raise HTTPException(
            429,
            f"Maximum of "
            f"{MAX_ACTIVE_UPLOADS_PER_USER} active uploads allowed.",
        )

    # ── Per-user reserved storage ──────────────────────────────────────────────

    user_bytes = (
        db.query(func.sum(Upload.size))
        .filter(
            Upload.user_id == user.id,
            Upload.status.in_([
                UploadStatus.uploading,
                UploadStatus.assembling,
                UploadStatus.completed,
            ]),
        )
        .scalar()
        or 0
    )

    if user_bytes + body.size > QUOTA_USER_GB * 1024**3:
        raise HTTPException(
            429,
            f"Storage quota exceeded. "
            f"You have approximately "
            f"{user_bytes / 1024**3:.2f} GB reserved/used "
            f"out of {QUOTA_USER_GB} GB.",
        )

    # ── Global active upload count ─────────────────────────────────────────────

    active_global_uploads = (
        db.query(func.count(Upload.upload_id))
        .filter(
            Upload.status.in_([
                UploadStatus.uploading,
                UploadStatus.assembling,
            ])
        )
        .scalar()
        or 0
    )

    if active_global_uploads >= MAX_ACTIVE_UPLOADS_GLOBAL:
        raise HTTPException(
            503,
            "Service is temporarily at maximum upload capacity.",
        )

    # ── Global reserved storage ────────────────────────────────────────────────

    global_bytes = (
        db.query(func.sum(Upload.size))
        .filter(
            Upload.status.in_([
                UploadStatus.uploading,
                UploadStatus.assembling,
                UploadStatus.completed,
            ])
        )
        .scalar()
        or 0
    )

    if global_bytes + body.size > QUOTA_GLOBAL_GB * 1024**3:
        raise HTTPException(
            503,
            "Service is at storage capacity. Please try again later.",
        )

    # ── Create upload ──────────────────────────────────────────────────────────

    upload_id = f"up_{uuid.uuid4().hex[:16]}"

    total_chunks = math.ceil(
        body.size / body.chunk_size
    )

    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(hours=UPLOAD_EXPIRY_HOURS)
    )

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
        raise HTTPException(
            500,
            "Could not create upload session.",
        )

    await storage.create_upload_dir(upload_id)

    log.info(
        "upload_created",
        upload_id=upload_id,
        filename=body.filename,
        user=user.username,
        chunks=total_chunks,
        size_bytes=body.size,
    )

    return CreateUploadResponse(
        upload_id=upload_id,
        total_chunks=total_chunks,
        chunk_size=body.chunk_size,
        expires_at=expires_at,
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

    # Lock the upload row to prevent concurrent duplicate chunks.
    upload = _get_upload_locked(
        upload_id,
        db,
        user,
    )

    _assert_active(upload)

    if not (
        1 <= chunk_number <= upload.total_chunks
    ):
        raise HTTPException(
            400,
            f"chunk_number must be between "
            f"1 and {upload.total_chunks}",
        )

    # ── Idempotency ────────────────────────────────────────────────────────────

    existing = (
        db.query(UploadChunk)
        .filter(
            UploadChunk.upload_id == upload_id,
            UploadChunk.chunk_number == chunk_number,
        )
        .first()
    )

    if existing:
        db.rollback()

        return ChunkUploadResponse(
            chunk_number=chunk_number,
            status="already_uploaded",
            already_uploaded=True,
        )

    # Commit/close the lock transaction before potentially
    # waiting on a large request body.
    db.commit()

    # ── Stream chunk into storage ──────────────────────────────────────────────

    try:
        computed, total_size = (
            await storage.save_chunk_from_stream(
                upload_id,
                chunk_number,
                request.stream(),
                MAX_CHUNK_SIZE,
            )
        )

    except ValueError as exc:
        raise HTTPException(
            400,
            str(exc),
        )

    # ── Verify checksum ────────────────────────────────────────────────────────

    if x_checksum and computed != x_checksum.lower():

        # Remove the physical upload because the chunk is invalid.
        await storage.delete_upload(upload_id)

        log.warning(
            "checksum_mismatch",
            upload_id=upload_id,
            chunk=chunk_number,
        )

        raise HTTPException(
            400,
            f"Checksum mismatch. "
            f"Expected {x_checksum}, got {computed}.",
        )

    # ── Save database record ───────────────────────────────────────────────────

    db.add(
        UploadChunk(
            upload_id=upload_id,
            chunk_number=chunk_number,
            size=total_size,
            checksum=computed,
        )
    )

    try:
        db.commit()

    except IntegrityError:
        db.rollback()

        # Another concurrent request won the race.
        return ChunkUploadResponse(
            chunk_number=chunk_number,
            status="already_uploaded",
            already_uploaded=True,
        )

    log.debug(
        "chunk_saved",
        upload_id=upload_id,
        chunk=chunk_number,
        bytes=total_size,
    )

    return ChunkUploadResponse(
        chunk_number=chunk_number,
        status="uploaded",
        already_uploaded=False,
    )


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

    upload = _get_upload_or_404(
        upload_id,
        db,
        user,
    )

    missing = _missing_chunks(upload)

    return UploadStatusResponse(
        upload_id=upload.upload_id,
        filename=upload.filename,
        size=upload.size,
        status=upload.status,
        total_chunks=upload.total_chunks,
        uploaded_chunks=(
            upload.total_chunks - len(missing)
        ),
        missing_chunks=missing,
        progress=_progress(upload),
        created_at=upload.created_at,
        completed_at=upload.completed_at,
        final_hash=upload.final_hash,
    )


# ── Complete ───────────────────────────────────────────────────────────────────

@router.post(
    "/uploads/{upload_id}/complete",
    response_model=CompleteUploadResponse,
)
@limiter.limit("10/minute")
async def complete_upload(
    request: Request,
    upload_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    # Lock row so only one completion operation can win.
    upload = _get_upload_locked(
        upload_id,
        db,
        user,
    )

    if upload.status == UploadStatus.completed:

        db.rollback()

        return CompleteUploadResponse(
            upload_id=upload_id,
            filename=upload.filename,
            status="completed",
            final_hash=upload.final_hash,
        )

    if upload.status == UploadStatus.assembling:
        db.rollback()

        raise HTTPException(
            409,
            "Upload is already being assembled.",
        )

    _assert_active(upload)

    missing = _missing_chunks(upload)

    if missing:
        db.rollback()

        raise HTTPException(
            400,
            f"Missing chunks: {missing[:10]}"
            + (
                f" (+{len(missing)-10} more)"
                if len(missing) > 10
                else ""
            ),
        )

    # Mark assembling before expensive storage work.
    upload.status = UploadStatus.assembling
    db.commit()

    try:

        final_hash = await storage.assemble_file(
            upload_id,
            upload.total_chunks,
            upload.filename,
        )

    except Exception as exc:

        upload = (
            db.query(Upload)
            .filter(Upload.upload_id == upload_id)
            .first()
        )

        if upload:
            upload.status = UploadStatus.failed
            db.commit()

        log.error(
            "assembly_failed",
            upload_id=upload_id,
            error=str(exc),
        )

        raise HTTPException(
            500,
            "Assembly failed.",
        )

    # ── Verify complete-file hash ──────────────────────────────────────────────

    hash_verified: Optional[bool] = None

    upload = (
        db.query(Upload)
        .filter(Upload.upload_id == upload_id)
        .first()
    )

    if not upload:
        raise HTTPException(
            404,
            "Upload no longer exists.",
        )

    if upload.file_hash:

        hash_verified = (
            final_hash == upload.file_hash.lower()
        )

        if not hash_verified:

            upload.status = UploadStatus.failed
            db.commit()

            # Delete the assembled object.
            await storage.delete_upload(upload_id)

            raise HTTPException(
                400,
                "File integrity verification failed.",
            )

    # ── Mark completed ─────────────────────────────────────────────────────────

    upload.status = UploadStatus.completed
    upload.final_hash = final_hash
    upload.completed_at = datetime.now(timezone.utc)
    db.commit()

    log.info(
        "upload_completed",
        upload_id=upload_id,
        user=user.username,
        filename=upload.filename,
        hash=final_hash,
    )

    return CompleteUploadResponse(
        upload_id=upload_id,
        filename=upload.filename,
        status="completed",
        final_hash=final_hash,
        hash_verified=hash_verified,
    )


# ── Delete ─────────────────────────────────────────────────────────────────────

@router.delete(
    "/uploads/{upload_id}"
)
@limiter.limit("20/minute")
async def delete_upload(
    request: Request,
    upload_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    upload = _get_upload_locked(
        upload_id,
        db,
        user,
    )

    # Don't allow deletion while another request is assembling.
    if upload.status == UploadStatus.assembling:
        db.rollback()

        raise HTTPException(
            409,
            "Upload is currently being assembled.",
        )

    filename = upload.filename

    # Mark cancelled first.
    upload.status = UploadStatus.cancelled
    db.commit()

    try:

        await storage.delete_upload(
            upload_id
        )

        db.query(UploadChunk).filter(
            UploadChunk.upload_id == upload_id
        ).delete(
            synchronize_session=False
        )

        upload = (
            db.query(Upload)
            .filter(Upload.upload_id == upload_id)
            .first()
        )

        if upload:
            db.delete(upload)

        db.commit()

        log.info(
            "upload_deleted",
            upload_id=upload_id,
            user=user.username,
            filename=filename,
        )

        return {
            "message": "Upload deleted permanently",
            "upload_id": upload_id,
        }

    except Exception as exc:

        db.rollback()

        log.error(
            "upload_delete_failed",
            upload_id=upload_id,
            error=str(exc),
        )

        raise HTTPException(
            500,
            "Delete failed.",
        )


# ── Download ───────────────────────────────────────────────────────────────────

@router.get(
    "/uploads/{upload_id}/download"
)
@limiter.limit("20/minute")
async def download_file(
    request: Request,
    upload_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    upload = _get_upload_or_404(
        upload_id,
        db,
        user,
    )

    if upload.status != UploadStatus.completed:
        raise HTTPException(
            400,
            "File not ready for download",
        )

    sas_url = await storage.get_download_url(
        upload_id,
        upload.filename,
    )

    if sas_url:

        log.info(
            "download_redirect_sas",
            upload_id=upload_id,
            user=user.username,
        )

        return RedirectResponse(
            sas_url
        )

    log.info(
        "download_local",
        upload_id=upload_id,
        user=user.username,
    )

    return FileResponse(
        path=storage.completed_file_path(
            upload_id,
            upload.filename,
        ),
        filename=upload.filename,
        media_type="application/octet-stream",
    )


# ── List uploads ───────────────────────────────────────────────────────────────

@router.get("/uploads")
@limiter.limit("30/minute")
async def list_uploads(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    uploads = (
        db.query(Upload)
        .filter(Upload.user_id == user.id)
        .order_by(Upload.created_at.desc())
        .limit(30)
        .all()
    )

    result = []

    for upload in uploads:

        missing = _missing_chunks(upload)

        uploaded = (
            upload.total_chunks
            - len(missing)
        )

        result.append({
            "upload_id": upload.upload_id,
            "filename": upload.filename,
            "size": upload.size,
            "status": upload.status.value,
            "progress": (
                round(
                    uploaded / upload.total_chunks * 100,
                    2,
                )
                if upload.total_chunks
                else 0
            ),
            "total_chunks": upload.total_chunks,
            "uploaded_chunks": uploaded,
            "created_at": upload.created_at.isoformat(),
            "completed_at": (
                upload.completed_at.isoformat()
                if upload.completed_at
                else None
            ),
            "final_hash": upload.final_hash,
        })

    return result