"""add needs_appointment_reason to workflow_status

Revision ID: a1b2c3d4e5f6
Revises: c48e9a271f36
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'c48e9a271f36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE workflow_status ADD VALUE IF NOT EXISTS 'needs_appointment_reason'")


def downgrade() -> None:
    """Downgrade schema."""
    # Same as c48e9a271f36: Postgres has no direct "drop enum value"
    # operation. Purely additive, no downgrade path needed for this phase.
    pass
