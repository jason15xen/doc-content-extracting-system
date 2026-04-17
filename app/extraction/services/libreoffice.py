import os
import pathlib
import shutil
import subprocess
import tempfile

from app.errors import ConversionError
from app.extraction.config import LIBREOFFICE_TIMEOUT_SEC


def soffice_available() -> bool:
    return shutil.which("soffice") is not None


def convert(src_path: str, target_format: str) -> str:
    """Convert `src_path` to `target_format` via headless LibreOffice.

    Returns the path to a converted file inside a caller-owned temp directory.
    Caller is responsible for cleaning up the returned file's parent directory.
    """
    if not soffice_available():
        raise ConversionError("soffice (LibreOffice) not found on PATH")

    outdir = tempfile.mkdtemp(prefix="soffice-")
    # Per-invocation user profile so concurrent requests don't collide on the
    # shared ~/.config/libreoffice lock.
    user_profile = tempfile.mkdtemp(prefix="soffice-profile-")
    user_profile_uri = pathlib.Path(user_profile).as_uri()

    cmd = [
        "soffice",
        f"-env:UserInstallation={user_profile_uri}",
        "--headless",
        "--norestore",
        "--nologo",
        "--nolockcheck",
        "--convert-to",
        target_format,
        "--outdir",
        outdir,
        src_path,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=LIBREOFFICE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(outdir, ignore_errors=True)
        raise ConversionError(f"soffice timed out after {LIBREOFFICE_TIMEOUT_SEC}s") from exc
    finally:
        shutil.rmtree(user_profile, ignore_errors=True)

    if proc.returncode != 0:
        shutil.rmtree(outdir, ignore_errors=True)
        raise ConversionError(f"soffice failed: {proc.stderr.decode(errors='replace').strip()}")

    base = os.path.splitext(os.path.basename(src_path))[0]
    target_ext = target_format.split(":", 1)[0]
    out_path = os.path.join(outdir, f"{base}.{target_ext}")
    if not os.path.exists(out_path):
        shutil.rmtree(outdir, ignore_errors=True)
        raise ConversionError(f"soffice produced no output at {out_path}")
    return out_path
