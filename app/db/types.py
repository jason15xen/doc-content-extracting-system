from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Cross-dialect tz-aware UTC datetime.

    SQLite lacks a native timezone-aware datetime type: SQLAlchemy's
    ``DateTime(timezone=True)`` serializes tz-aware values to ISO strings but
    returns naive datetimes on read. This decorator normalizes both ends:

      write → strip tzinfo after converting to UTC (store naive UTC)
      read  → reattach ``timezone.utc``

    On Postgres (where the underlying ``TIMESTAMPTZ`` already round-trips
    offsets), the extra conversion is a no-op for already-UTC inputs.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(
        self, value: Any, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
