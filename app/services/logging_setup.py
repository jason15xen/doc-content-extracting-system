import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HANDLER_TAG = "rag-file-log"

# uvicorn configures these loggers with propagate=False, so messages don't
# bubble up to root. We attach the file handler directly and keep
# propagate=False to also prevent double-logging via root.
_DETACHED_LOGGERS = ("uvicorn", "uvicorn.access")

# Matches rotated file names produced by _namer below.
_DATED_RE = re.compile(r"^app-(\d{4}-\d{2}-\d{2})\.txt$")


def _namer(default_name: str) -> str:
    """TimedRotatingFileHandler rolls `app.txt` → `app.txt.YYYY-MM-DD` by
    default. Rewrite that to `app-YYYY-MM-DD.txt` so rotated files keep the
    `.txt` extension and sort chronologically when listed."""
    base = Path(default_name)
    _, _, date = base.name.partition("app.txt.")
    if date:
        return str(base.with_name(f"app-{date}.txt"))
    return default_name


class DailyLogHandler(TimedRotatingFileHandler):
    """TimedRotatingFileHandler whose retention logic understands the
    renamed-filename scheme ``app-YYYY-MM-DD.txt``.

    The base class's ``getFilesToDelete`` looks for files that start with
    ``app.txt.`` — but our custom ``namer`` rewrites them, so the base
    method would never find anything to delete and ``backupCount`` would
    silently stop working. Overriding the method keeps retention correct.
    """

    def getFilesToDelete(self) -> list[str]:
        log_dir = Path(self.baseFilename).parent
        dated = sorted(
            str(p)
            for p in log_dir.iterdir()
            if p.is_file() and _DATED_RE.match(p.name)
        )
        if len(dated) <= self.backupCount:
            return []
        # Oldest first; keep the newest `backupCount` files.
        return dated[: len(dated) - self.backupCount]


def setup_file_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    """Attach a daily-rotating file handler to the root and uvicorn loggers.

    Current day:   {logs_dir}/app.txt
    Rotated days:  {logs_dir}/app-YYYY-MM-DD.txt
    Retention:     30 most-recent rotated files (~30 days of history)

    Idempotent: re-runs skip loggers that already carry the tagged handler.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "app.txt"

    handler = DailyLogHandler(
        filename=str(log_path),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=True,
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    handler.setLevel(level)
    handler.set_name(_HANDLER_TAG)
    handler.namer = _namer

    def _attach(target: logging.Logger) -> bool:
        if any(h.get_name() == _HANDLER_TAG for h in target.handlers):
            return False
        target.addHandler(handler)
        return True

    # Root captures anything that propagates (app-level logs, sqlalchemy,
    # azure-sdk, etc.). Most loggers default to propagate=True.
    root = logging.getLogger()
    if _attach(root):
        root.setLevel(level)

    # uvicorn's access/error loggers have propagate=False by design. Attach
    # directly and lock propagate off so these don't bubble up to root,
    # which would cause double-writes since root also has our handler.
    for name in _DETACHED_LOGGERS:
        sub = logging.getLogger(name)
        _attach(sub)
        sub.propagate = False
