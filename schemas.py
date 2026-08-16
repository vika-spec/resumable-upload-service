from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime
from models import UploadStatus


MIN_CHUNK_SIZE = 1 * 1024 * 1024       # 1 MB
MAX_FILENAME_LENGTH = 255


# ── Request bodies ─────────────────────────────────────────────────────────────

class CreateUploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=MAX_FILENAME_LENGTH)

    size: int = Field(
        ...,
        gt=0,
        description="Total file size in bytes",
    )

    chunk_size: int = Field(
        5 * 1024 * 1024,
        ge=MIN_CHUNK_SIZE,
        description="Bytes per chunk. Minimum 1 MB.",
    )

    file_hash: Optional[str] = Field(
        None,
        min_length=64,
        max_length=64,
        description="SHA-256 of the entire file (optional)",
    )

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        # Never allow filesystem path separators.
        if "/" in value or "\\" in value:
            raise ValueError("Filename must not contain path separators")

        if "\x00" in value:
            raise ValueError("Filename contains invalid characters")

        value = value.strip()

        if not value:
            raise ValueError("Filename cannot be empty")

        return value

    @field_validator("file_hash")
    @classmethod
    def validate_hash(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            value = value.lower()

            if any(c not in "0123456789abcdef" for c in value):
                raise ValueError("file_hash must be a SHA-256 hexadecimal string")

        return value


# ── Response bodies ────────────────────────────────────────────────────────────

class CreateUploadResponse(BaseModel):
    upload_id: str
    total_chunks: int
    chunk_size: int
    expires_at: datetime


class UploadStatusResponse(BaseModel):
    upload_id: str
    filename: str
    size: int
    status: UploadStatus
    total_chunks: int
    uploaded_chunks: int
    missing_chunks: List[int]
    progress: float
    created_at: datetime
    completed_at: Optional[datetime] = None
    final_hash: Optional[str] = None


class ChunkUploadResponse(BaseModel):
    chunk_number: int
    status: str
    already_uploaded: bool = False


class CompleteUploadResponse(BaseModel):
    upload_id: str
    filename: str
    status: str
    final_hash: Optional[str] = None
    hash_verified: Optional[bool] = None