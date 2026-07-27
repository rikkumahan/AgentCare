"""add required_document_types to departments

Revision ID: b7e2f4a91c3d
Revises: 1dd0ad4bbe02
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2f4a91c3d'
down_revision: Union[str, Sequence[str], None] = '1dd0ad4bbe02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'departments',
        sa.Column('required_document_types', sa.JSON(), nullable=False, server_default='[]'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('departments', 'required_document_types')
