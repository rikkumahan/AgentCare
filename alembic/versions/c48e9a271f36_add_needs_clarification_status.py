"""add needs_clarification to workflow_status

Revision ID: c48e9a271f36
Revises: b7e2f4a91c3d
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c48e9a271f36'
down_revision: Union[str, Sequence[str], None] = 'b7e2f4a91c3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE workflow_status ADD VALUE IF NOT EXISTS 'needs_clarification'")


def downgrade() -> None:
    """Downgrade schema."""
    # Postgres has no direct "drop enum value" operation - reversing this
    # would require recreating the workflow_status type without the value
    # and remapping every existing row. Left as a no-op: this migration is
    # purely additive (a new allowed status value, no column/table change),
    # and nothing in this phase requires a working downgrade path for it.
    pass
