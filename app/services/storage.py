import os
import uuid
from collections.abc import Iterator
from pathlib import Path


def upload_path(uploads_dir: Path, doc_id: uuid.UUID, ext: str) -> Path:
    return uploads_dir / f"{doc_id}{ext}"


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
