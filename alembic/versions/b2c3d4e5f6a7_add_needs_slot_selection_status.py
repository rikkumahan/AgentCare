"""add needs_slot_selection to workflow_status

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-28 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE workflow_status ADD VALUE IF NOT EXISTS 'needs_slot_selection'")


def downgrade() -> None:
    """Downgrade schema."""
    # Same as prior workflow_status additions: Postgres has no direct
    # "drop enum value" operation. Purely additive, no downgrade needed.
    pass
