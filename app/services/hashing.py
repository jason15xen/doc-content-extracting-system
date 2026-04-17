import hashlib
import os
from pathlib import Path

from fastapi import UploadFile

CHUNK_SIZE = 1024 * 1024


class OversizeError(Exception):
    pass


async def save_upload_with_hash(
    upload: UploadFile, dest: Path, max_bytes: int
) -> tuple[str, int]:
    """Stream `upload` to `dest`, enforcing `max_bytes`. Returns (sha256_hex, size)."""
    sha = hashlib.sha256()
    total = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise OversizeError(
                        f"file exceeds {max_bytes // (1024 * 1024)} MB limit"
                    )
                sha.update(chunk)
                f.write(chunk)
    except OversizeError:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    return sha.hexdigest(), total
