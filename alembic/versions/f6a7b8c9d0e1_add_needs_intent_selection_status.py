"""add needs_intent_selection to workflow_status

Revision ID: f6a7b8c9d0e1
Revises: e59f1a2b3c4d
Create Date: 2026-07-28 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e59f1a2b3c4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE workflow_status ADD VALUE IF NOT EXISTS 'needs_intent_selection'")


def downgrade() -> None:
    """Downgrade schema."""
    # Same as prior workflow_status additions: Postgres has no direct
    # "drop enum value" operation. Purely additive, no downgrade needed.
    pass
