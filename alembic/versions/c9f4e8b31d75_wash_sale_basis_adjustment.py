"""wash-sale basis adjustment columns

Two columns supporting real IRC §1091 enforcement:

- realized_gains.disallowed_wash_loss — the portion of a realized loss
  disallowed because replacement shares were bought inside the ±30-day
  window. gain_loss becomes the TAX-REPORTABLE figure (economic result +
  disallowed portion); the disallowed loss rolls into the replacement
  lot's basis instead of vanishing.
- tax_lots.wash_replacement_used — shares of a lot already claimed as
  replacement shares, so the same shares can't absorb two losses.

Revision ID: c9f4e8b31d75
Revises: b7e3d1a52c48
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f4e8b31d75'
down_revision: Union[str, Sequence[str], None] = 'b7e3d1a52c48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'realized_gains',
        sa.Column('disallowed_wash_loss', sa.Numeric(20, 8), nullable=False, server_default='0'),
    )
    op.add_column(
        'tax_lots',
        sa.Column('wash_replacement_used', sa.Numeric(20, 8), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tax_lots', 'wash_replacement_used')
    op.drop_column('realized_gains', 'disallowed_wash_loss')
