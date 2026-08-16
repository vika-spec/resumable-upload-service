"""
Dual-mode storage service.

  • No AZURE_STORAGE_CONNECTION_STRING → local disk  (development)
  • AZURE_STORAGE_CONNECTION_STRING set             → Azure Blob Storage (production)

Only three async methods matter:
  save_chunk_from_stream()   – stream request bytes → storage
  assemble_file()            – join chunks → final blob/file, returns SHA-256
  delete_upload()            – remove all blobs/files for an upload
"""

import base64
import hashlib
import io
import os
import shutil
from typing import AsyncIterator
import aiofiles

# ── Local-disk paths (dev only) ───────────────────────────────────────────────
CHUNKS_DIR    = "./storage/chunks"
COMPLETED_DIR = "./storage/completed"


class StorageService:

    def __init__(self):
        self._conn_str  = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
        self._container = os.getenv("AZURE_STORAGE_CONTAINER", "uploads")
        if not self._conn_str:
            os.makedirs(CHUNKS_DIR,    exist_ok=True)
            os.makedirs(COMPLETED_DIR, exist_ok=True)

    def using_azure(self) -> bool:
        return bool(self._conn_str)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _chunk_path(self, upload_id: str, n: int) -> str:
        return os.path.join(CHUNKS_DIR, upload_id, f"chunk_{n:06d}")

    def _blob(self, client, path: str):
        return client.get_blob_client(self._container, path)

    # ── API ───────────────────────────────────────────────────────────────────

    async def create_upload_dir(self, upload_id: str) -> None:
        if not self.using_azure():
            os.makedirs(os.path.join(CHUNKS_DIR, upload_id), exist_ok=True)

    async def save_chunk_from_stream(
        self,
        upload_id: str,
        chunk_number: int,
        stream: AsyncIterator[bytes],
        max_size: int,
    ) -> tuple[str, int]:
        """
        Read `stream` piece by piece → compute SHA-256 → write to storage.
        Returns (checksum_hex, total_bytes).
        Raises ValueError for empty or oversized chunks.
        """
        sha256 = hashlib.sha256()
        total  = 0

        if self.using_azure():
            # ── Azure: buffer → upload ─────────────────────────────────────
            buf = io.BytesIO()
            async for piece in stream:
                buf.write(piece)
                sha256.update(piece)
                total += len(piece)
                if total > max_size:
                    raise ValueError("Chunk exceeds MAX_CHUNK_SIZE")
            if total == 0:
                raise ValueError("Empty chunk")
            buf.seek(0)

            from azure.storage.blob.aio import BlobServiceClient
            async with BlobServiceClient.from_connection_string(self._conn_str) as client:
                await self._blob(client, f"chunks/{upload_id}/chunk_{chunk_number:06d}") \
                         .upload_blob(buf, overwrite=True, length=total)
        else:
            # ── Local: stream → temp file → atomic rename ──────────────────
            chunk_path = self._chunk_path(upload_id, chunk_number)
            temp_path  = chunk_path + ".tmp"
            try:
                async with aiofiles.open(temp_path, "wb") as f:
                    async for piece in stream:
                        await f.write(piece)
                        sha256.update(piece)
                        total += len(piece)
                        if total > max_size:
                            raise ValueError("Chunk exceeds MAX_CHUNK_SIZE")
            except Exception:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
            if total == 0:
                os.remove(temp_path)
                raise ValueError("Empty chunk")
            os.rename(temp_path, chunk_path)

        return sha256.hexdigest(), total

    async def assemble_file(
        self, upload_id: str, total_chunks: int, filename: str
    ) -> str:
        """Concatenate chunks → final file. Returns SHA-256 of assembled file."""
        sha256 = hashlib.sha256()

        if self.using_azure():
            from azure.storage.blob import BlobBlock
            from azure.storage.blob.aio import BlobServiceClient

            async with BlobServiceClient.from_connection_string(self._conn_str) as client:
                final_blob = self._blob(client, f"completed/{upload_id}/{filename}")
                block_ids  = []

                for i in range(1, total_chunks + 1):
                    chunk_blob = self._blob(client, f"chunks/{upload_id}/chunk_{i:06d}")
                    dl   = await chunk_blob.download_blob()
                    data = await dl.readall()
                    sha256.update(data)

                    bid = base64.b64encode(f"{i:06d}".encode()).decode()
                    await final_blob.stage_block(bid, data)
                    block_ids.append(BlobBlock(block_id=bid))

                await final_blob.commit_block_list(block_ids)

                # Clean up chunk blobs
                for i in range(1, total_chunks + 1):
                    try:
                        await self._blob(
                            client, f"chunks/{upload_id}/chunk_{i:06d}"
                        ).delete_blob()
                    except Exception:
                        pass
        else:
            out_dir  = os.path.join(COMPLETED_DIR, upload_id)
            out_path = os.path.join(out_dir, filename)
            os.makedirs(out_dir, exist_ok=True)

            async with aiofiles.open(out_path, "wb") as outfile:
                for i in range(1, total_chunks + 1):
                    async with aiofiles.open(self._chunk_path(upload_id, i), "rb") as cf:
                        data = await cf.read()
                        await outfile.write(data)
                        sha256.update(data)

            shutil.rmtree(os.path.join(CHUNKS_DIR, upload_id), ignore_errors=True)

        return sha256.hexdigest()

    async def delete_upload(self, upload_id: str) -> None:
        if self.using_azure():
            from azure.storage.blob.aio import BlobServiceClient
            async with BlobServiceClient.from_connection_string(self._conn_str) as client:
                container_client = client.get_container_client(self._container)
                for prefix in (f"chunks/{upload_id}/", f"completed/{upload_id}/"):
                    async for blob in container_client.list_blobs(name_starts_with=prefix):
                        try:
                            await container_client.delete_blob(blob.name)
                        except Exception:
                            pass
        else:
            shutil.rmtree(os.path.join(CHUNKS_DIR,    upload_id), ignore_errors=True)
            shutil.rmtree(os.path.join(COMPLETED_DIR, upload_id), ignore_errors=True)

    async def get_download_url(self, upload_id: str, filename: str) -> str | None:
        """
        Azure: return a 1-hour SAS URL (redirect, no bandwidth cost on server).
        Local: return None (caller uses FileResponse instead).
        """
        if not self.using_azure():
            return None

        from azure.storage.blob import (
            BlobSasPermissions, BlobServiceClient as SyncClient,
            generate_blob_sas,
        )
        from datetime import datetime, timedelta, timezone

        blob_name   = f"completed/{upload_id}/{filename}"
        sync_client = SyncClient.from_connection_string(self._conn_str)
        sas = generate_blob_sas(
            account_name    = sync_client.account_name,
            container_name  = self._container,
            blob_name       = blob_name,
            account_key     = sync_client.credential.account_key,
            permission      = BlobSasPermissions(read=True),
            expiry = datetime.now(timezone.utc) + timedelta(minutes=15),
        )
        return (
            f"https://{sync_client.account_name}.blob.core.windows.net"
            f"/{self._container}/{blob_name}?{sas}"
        )

    def completed_file_path(self, upload_id: str, filename: str) -> str:
        """Local-only. Used by FileResponse when not on Azure."""
        return os.path.join(COMPLETED_DIR, upload_id, filename)
