"""expected future rate for honest deferral math

Harvesting a loss usually postpones tax rather than erasing it: the
replacement position's cost basis is lower by the harvested loss, so the
eventual sale owes that gain back at the future LT rate. "It's only an
interest-free loan if your future tax bracket is the same." The engine now
shows saves-now / costs-later / net, using this user-declared expected
future rate (null = assume today's LT rate).

Revision ID: 0008_deferral_settings
Revises: 0007_external_accounts
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008_deferral_settings'
down_revision: Union[str, Sequence[str], None] = '0007_external_accounts'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'tax_settings',
        sa.Column('expected_future_lt_rate', sa.Numeric(6, 4), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tax_settings', 'expected_future_lt_rate')
