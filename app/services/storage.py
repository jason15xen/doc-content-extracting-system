import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path


def upload_path(uploads_dir: Path, doc_id: uuid.UUID, ext: str) -> Path:
    return uploads_dir / f"{doc_id}{ext}"


def temp_upload_path(ext: str) -> Path:
    """Allocate a fresh empty file under the OS temp directory and return its
    path. The caller owns the file and MUST unlink it after use — by design
    the source bytes never land under storage/uploads, so the ingest pipeline
    cleans up here regardless of success or failure."""
    fd, path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    return Path(path)


def iter_upload_files(uploads_dir: Path) -> Iterator[Path]:
    if not uploads_dir.exists():
        return
    for p in uploads_dir.iterdir():
        if p.is_file():
            yield p


def try_unlink(path: Path | str | None) -> None:
    if path is None:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError:
        return
