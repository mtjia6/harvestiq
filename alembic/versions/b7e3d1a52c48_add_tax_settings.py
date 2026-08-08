"""add tax_settings (prior-year carryforward inputs)

The tax engine now applies the real IRS netting waterfall, and the one input
it cannot derive from transaction history is the capital-loss carryforward a
user brings into the year. Stored as POSITIVE loss amounts, split by
character (short/long), in a single-row table (id=1). No row means no
carryforward — the engine treats a missing row as zeros.

Revision ID: b7e3d1a52c48
Revises: a1c4f2b9e07d
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e3d1a52c48'
down_revision: Union[str, Sequence[str], None] = 'a1c4f2b9e07d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'tax_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('carryforward_st', sa.Numeric(20, 8), nullable=False, server_default='0'),
        sa.Column('carryforward_lt', sa.Numeric(20, 8), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('tax_settings')
