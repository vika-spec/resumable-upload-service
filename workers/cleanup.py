import asyncio
import os
from datetime import datetime

from database import SessionLocal
from logger import get_logger
from models import Upload, UploadStatus
from services.storage_service import StorageService


log = get_logger(__name__)
storage = StorageService()

CLEANUP_INTERVAL = int(
    os.getenv("CLEANUP_INTERVAL_SECONDS", "300")
)


async def start_cleanup_worker() -> None:

    log.info(
        "cleanup_worker_started",
        interval_seconds=CLEANUP_INTERVAL,
    )

    while True:

        try:

            cleaned = await _run_cleanup()

            if cleaned:
                log.info(
                    "cleanup_done",
                    removed=cleaned,
                )

        except Exception as exc:

            log.error(
                "cleanup_error",
                error=str(exc),
            )

        await asyncio.sleep(
            CLEANUP_INTERVAL
        )


async def _run_cleanup() -> int:

    db = SessionLocal()
    cleaned = 0

    try:

        now = datetime.utcnow()

        stale = (
            db.query(Upload)
            .filter(
                Upload.expires_at < now,
                Upload.status.in_([
                    UploadStatus.uploading,
                    UploadStatus.failed,
                ]),
            )
            .all()
        )

        for upload in stale:

            upload_id = upload.upload_id
            filename = upload.filename

            try:

                await storage.delete_upload(
                    upload_id
                )

                upload.status = UploadStatus.expired

                log.info(
                    "upload_expired",
                    upload_id=upload_id,
                    filename=filename,
                )

                cleaned += 1

            except Exception as exc:

                log.error(
                    "upload_expire_delete_failed",
                    upload_id=upload_id,
                    error=str(exc),
                )

        db.commit()

    finally:

        db.close()

    return cleaned