import enum
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, BigInteger,
    DateTime, Enum, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from database import Base


class UploadStatus(str, enum.Enum):
    uploading  = "uploading"
    assembling = "assembling"
    completed  = "completed"
    cancelled  = "cancelled"
    failed     = "failed"
    expired    = "expired"


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    username        = Column(String, unique=True, nullable=False, index=True)
    email           = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    created_at      = Column(DateTime, default=datetime.utcnow)

    uploads = relationship("Upload", back_populates="owner")


class Upload(Base):
    __tablename__ = "uploads"

    upload_id    = Column(String,   primary_key=True, index=True)
    user_id      = Column(Integer,  ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    filename     = Column(String,   nullable=False)
    size         = Column(BigInteger, nullable=False)
    chunk_size   = Column(Integer,  nullable=False)
    total_chunks = Column(Integer,  nullable=False)
    status       = Column(Enum(UploadStatus), default=UploadStatus.uploading, nullable=False)
    file_hash    = Column(String,   nullable=True)
    final_hash   = Column(String,   nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    expires_at   = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    owner  = relationship("User", back_populates="uploads")
    chunks = relationship("UploadChunk", back_populates="upload", cascade="all, delete-orphan")


class UploadChunk(Base):
    __tablename__ = "upload_chunks"
    __table_args__ = (
        UniqueConstraint("upload_id", "chunk_number", name="uq_upload_chunk"),
    )

    id           = Column(Integer, primary_key=True, autoincrement=True)
    upload_id    = Column(String,  ForeignKey("uploads.upload_id", ondelete="CASCADE"), nullable=False)
    chunk_number = Column(Integer, nullable=False)
    size         = Column(Integer, nullable=False)
    checksum     = Column(String,  nullable=True)
    uploaded_at  = Column(DateTime, default=datetime.utcnow)

    upload = relationship("Upload", back_populates="chunks")
