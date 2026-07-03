"""The access-log poll filter must drop the UI's task-status polls (which hit
the real /doc-prefixed routes) while keeping every other access-log line."""
import logging

import pytest

from app.services.logging_setup import _AccessLogPollFilter


def _access_record(method: str, path: str) -> logging.LogRecord:
    """Build a record shaped like uvicorn.access emits:
    '%s - "%s %s HTTP/%s" %d' % (client, method, path, version, status)."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", method, path, "1.1", 200),
        exc_info=None,
    )


@pytest.mark.parametrize(
    "method,path,kept",
    [
        ("GET", "/doc/tasks", False),                  # tasks-tab poll → dropped
        ("GET", "/doc/tasks?limit=50", False),         # poll with query → dropped
        ("GET", "/doc/status/abc-123", False),         # upload-widget poll → dropped
        ("POST", "/doc/tasks/abc/cancel", True),       # user action → kept
        ("DELETE", "/doc/tasks", True),                # user action → kept
        ("GET", "/doc", True),                         # other GETs → kept
        ("POST", "/query", True),
        ("GET", "/health", True),
    ],
)
def test_poll_filter(method: str, path: str, kept: bool):
    assert _AccessLogPollFilter().filter(_access_record(method, path)) is kept
