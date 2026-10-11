"""
Admin dashboard — GET /api/admin/stats

Protected: only the user whose username matches ADMIN_USERNAME env var can access.
No model changes needed — just an env var check.
"""
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request  # <-- Added Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from logger import get_logger
from models import Upload, UploadChunk, UploadStatus, User
from services.auth_service import ADMIN_USERNAME
from services.storage_service import StorageService
from limiter_config import limiter  # <-- Added import

router = APIRouter(prefix="/admin", tags=["admin"])
storage = StorageService()
log = get_logger(__name__)

# Mirrors the limits set in uploads.py (read from same env vars)
QUOTA_GLOBAL_GB = int(os.getenv("QUOTA_GLOBAL_GB", "40"))
QUOTA_USER_GB = int(os.getenv("QUOTA_USER_STORAGE_GB", "1"))


def _require_admin(user: User = Depends(get_current_user)) -> User:
    if user.username != ADMIN_USERNAME:
        raise HTTPException(403, "Admin access required")
    return user


@router.get("/stats")
@limiter.limit("10/minute")  # <-- Added rate limit decorator
async def admin_stats(
    request: Request,  # <-- Added request parameter (required by slowapi)
    db: Session = Depends(get_db),
    user: User = Depends(_require_admin),
):
    """
    Full service overview: users, uploads by status,
    storage consumed, quota usage, top uploaders, recent activity.
    """
    now = datetime.utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ── Users ──────────────────────────────────────────────────────────────────
    total_users = db.query(func.count(User.id)).scalar() or 0
    new_users_today = db.query(func.count(User.id)).filter(
        User.created_at >= day_start).scalar() or 0

    # ── Uploads by status ──────────────────────────────────────────────────────
    status_counts = {
        row.status.value: row.cnt
        for row in db.query(Upload.status, func.count(Upload.upload_id).label("cnt"))
        .group_by(Upload.status).all()
    }
    active_sessions = status_counts.get("uploading", 0)

    # ── Storage ────────────────────────────────────────────────────────────────
    completed_bytes = db.query(func.sum(Upload.size)).filter(
        Upload.status == UploadStatus.completed
    ).scalar() or 0

    in_progress_bytes = db.query(func.sum(Upload.size)).filter(
        Upload.status == UploadStatus.uploading
    ).scalar() or 0

    global_limit_bytes = QUOTA_GLOBAL_GB * 1024 ** 3
    quota_pct = round(completed_bytes / global_limit_bytes * 100, 2) if global_limit_bytes else 0

    # ── Activity today ─────────────────────────────────────────────────────────
    uploads_today = db.query(func.count(Upload.upload_id)).filter(
        Upload.created_at >= day_start
    ).scalar() or 0

    completed_today = db.query(func.count(Upload.upload_id)).filter(
        Upload.completed_at >= day_start,
        Upload.status == UploadStatus.completed,
    ).scalar() or 0

    # ── Top 5 uploaders (by total completed bytes) ─────────────────────────────
    top_uploaders = db.query(
        User.username,
        func.count(Upload.upload_id).label("uploads"),
        func.coalesce(func.sum(Upload.size), 0).label("bytes"),
    ).join(Upload, Upload.user_id == User.id, isouter=True) \
        .filter(Upload.status == UploadStatus.completed) \
        .group_by(User.id) \
        .order_by(func.sum(Upload.size).desc().nullslast()) \
        .limit(5).all()

    # ── Recent 10 uploads ──────────────────────────────────────────────────────
    recent = db.query(Upload, User.username) \
        .join(User, User.id == Upload.user_id, isouter=True) \
        .order_by(Upload.created_at.desc()) \
        .limit(10).all()

    log.info("admin_stats_viewed", admin=user.username)

    return {
        "generated_at": now.isoformat(),
        "users": {
            "total": total_users,
            "new_today": new_users_today,
        },
        "uploads": {
            "by_status": status_counts,
            "active_sessions": active_sessions,
            "uploads_today": uploads_today,
            "completed_today": completed_today,
        },
        "storage": {
            "completed_bytes": completed_bytes,
            "completed_gb": round(completed_bytes / 1024 ** 3, 3),
            "in_progress_bytes": in_progress_bytes,
            "in_progress_gb": round(in_progress_bytes / 1024 ** 3, 3),
            "global_limit_gb": QUOTA_GLOBAL_GB,
            "quota_used_pct": quota_pct,
            "per_user_limit_gb": QUOTA_USER_GB,
            "backend": "azure" if storage.using_azure() else "local_disk",
        },
        "top_uploaders": [
            {
                "username": row.username,
                "uploads": row.uploads,
                "bytes": row.bytes,
                "gb": round(row.bytes / 1024 ** 3, 3),
            }
            for row in top_uploaders
        ],
        "recent_uploads": [
            {
                "upload_id": u.upload_id,
                "filename": u.filename,
                "size_mb": round(u.size / 1024 ** 2, 1),
                "status": u.status.value,
                "user": username,
                "created_at": u.created_at.isoformat(),
            }
            for u, username in recent
        ],
    }