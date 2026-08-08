"""multi-wallet: accounts registry + basis-preserving transfers

Per-wallet cost basis (mandatory for crypto since Rev. Proc. 2024-28,
sensible for everything) was already half-enforced by broker-scoped sells;
this makes the concept explicit:

- accounts — registry of OUR wallets/accounts (brokerage, exchange,
  self-custody). Backfilled from every distinct broker string already in
  transactions and tax_lots so historical data keeps working; new names
  are auto-registered on first use.
- transfers — audit rows for inter-wallet moves. A transfer re-homes (or
  splits) tax lots carrying basis, opened_date, and holding_period_date
  with them — the thing a blockchain transaction doesn't do for you.

Revision ID: 0011_wallets_and_transfers
Revises: 0010_carryforward_discount
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011_wallets_and_transfers'
down_revision: Union[str, Sequence[str], None] = '0010_carryforward_discount'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'accounts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(), nullable=False, unique=True),
        sa.Column('kind', sa.String(), nullable=False, server_default='unknown'),
    )
    op.create_table(
        'transfers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('quantity', sa.Numeric(20, 8), nullable=False),
        sa.Column('from_account', sa.String(), nullable=False),
        sa.Column('to_account', sa.String(), nullable=False),
        sa.Column('transfer_date', sa.Date(), nullable=False),
    )
    # Backfill the registry from every broker name already on the books.
    op.execute(
        "INSERT INTO accounts (name, kind) "
        "SELECT DISTINCT broker, 'unknown' FROM transactions "
        "WHERE broker IS NOT NULL "
        "UNION "
        "SELECT DISTINCT broker, 'unknown' FROM tax_lots "
        "WHERE broker IS NOT NULL AND broker NOT IN (SELECT broker FROM transactions WHERE broker IS NOT NULL)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('transfers')
    op.drop_table('accounts')
