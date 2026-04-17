import hashlib
import io
from pathlib import Path

import pytest
from fastapi import UploadFile

from app.services.hashing import OversizeError, save_upload_with_hash


def _upload(data: bytes, filename: str = "file.bin") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename)


@pytest.mark.asyncio
async def test_hash_matches_hashlib(tmp_path: Path):
    data = b"the quick brown fox jumps over the lazy dog" * 1000
    dest = tmp_path / "out.bin"
    digest, size = await save_upload_with_hash(_upload(data), dest, max_bytes=10 * 1024 * 1024)
    assert digest == hashlib.sha256(data).hexdigest()
    assert size == len(data)
    assert dest.read_bytes() == data


@pytest.mark.asyncio
async def test_oversize_aborts_and_unlinks(tmp_path: Path):
    data = b"x" * (1024 * 1024 + 1)
    dest = tmp_path / "out.bin"
    with pytest.raises(OversizeError):
        await save_upload_with_hash(_upload(data), dest, max_bytes=1024 * 1024)
    assert not dest.exists()
