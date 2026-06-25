"""add 'partial' to task_files.status vocabulary

Revision ID: 0004_partial_status
Revises: 0003_spec_alignment
Create Date: 2026-06-25

A file that indexed but contained scanned / unextractable pages is reported as
``partial`` instead of ``completed``. SQLite can't ALTER a CHECK constraint in
place, so we recreate the constraint via batch mode (table copy); existing rows
are preserved.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004_partial_status"
down_revision: Union[str, None] = "0003_spec_alignment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "status IN ('pending','processing','completed','failed','skipped')"
_NEW = "status IN ('pending','processing','completed','failed','skipped','partial')"


def upgrade() -> None:
    with op.batch_alter_table("task_files") as batch_op:
        batch_op.drop_constraint("ck_task_files_status", type_="check")
        batch_op.create_check_constraint("ck_task_files_status", _NEW)


def downgrade() -> None:
    with op.batch_alter_table("task_files") as batch_op:
        batch_op.drop_constraint("ck_task_files_status", type_="check")
        batch_op.create_check_constraint("ck_task_files_status", _OLD)
