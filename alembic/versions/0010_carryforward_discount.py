"""explicit carryforward pricing for the harvest plan

The plan previously ranked lots by a tuple (savings, carryforward), which
silently declared that carryforward is worth something but always less
than a dollar of current-year savings — an unpriced strategic call. Now
the user prices it: carryforward_discount (0..1) scales the future-rate
value of new carryforward in the plan's single-number score.

  0 (default) — no gains coming: don't spend unrealized loss past the
     current-year cap; the plan sizes partial sells to land exactly on it.
  1 — a big gain is coming: bank everything; carryforward ≈ face value.

Revision ID: 0010_carryforward_discount
Revises: 0009_holding_period_date
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010_carryforward_discount'
down_revision: Union[str, Sequence[str], None] = '0009_holding_period_date'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'tax_settings',
        sa.Column('carryforward_discount', sa.Numeric(6, 4), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tax_settings', 'carryforward_discount')
