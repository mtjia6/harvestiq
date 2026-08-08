"""watch-only external accounts for cross-account wash sales

The wash-sale rule applies to the taxpayer across ALL accounts, but each
broker only sees its own — sell at a loss at Schwab, your IRA auto-buys the
same fund next week, and the loss is gone (permanently, for a retirement
account, per Rev. Rul. 2008-5). No mainstream software catches this.

- external_accounts — watch-only: we never sell from them, we only count
  their purchases against the wash window.
- external_activities — observed BUYs in those accounts (manual entry),
  with replacement_used so one purchase can't wash two sales.
- realized_gains.wash_sale_note — where a disallowed loss went when the
  replacement purchase was external (that account's books, or destroyed).

Revision ID: 0007_external_accounts
Revises: 0006_drip_dividends
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007_external_accounts'
down_revision: Union[str, Sequence[str], None] = '0006_drip_dividends'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'external_accounts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('broker', sa.String(), nullable=False),
        sa.Column('account_type', sa.String(), nullable=False),
    )
    op.create_table(
        'external_activities',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('external_accounts.id'), nullable=False),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('quantity', sa.Numeric(20, 8), nullable=False),
        sa.Column('trade_date', sa.Date(), nullable=False),
        sa.Column('replacement_used', sa.Numeric(20, 8), nullable=False, server_default='0'),
    )
    op.add_column(
        'realized_gains',
        sa.Column('wash_sale_note', sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('realized_gains', 'wash_sale_note')
    op.drop_table('external_activities')
    op.drop_table('external_accounts')
