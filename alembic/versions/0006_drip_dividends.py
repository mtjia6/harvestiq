"""DRIP guard inputs on securities

Automatic dividend reinvestment is the most common way retail investors
accidentally trigger the wash-sale rule: the broker's reinvestment is a
purchase inside the ±30-day window, and the harvested loss gets disallowed
months later on a tax form. The engine can't see broker settings, so the
user declares them per security:

- securities.drip_enabled — is automatic reinvestment on for this holding
- securities.next_dividend_date — when the next (or most recent) dividend
  lands; the harvest engine warns when it falls inside the wash window

Revision ID: 0006_drip_dividends
Revises: c9f4e8b31d75
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006_drip_dividends'
down_revision: Union[str, Sequence[str], None] = 'c9f4e8b31d75'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'securities',
        sa.Column('drip_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'securities',
        sa.Column('next_dividend_date', sa.Date(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('securities', 'next_dividend_date')
    op.drop_column('securities', 'drip_enabled')
