"""add history_symbol to securities

Splits vendor symbol conventions. `securities.symbol` is canonical and is the
FINNHUB symbol (what the live websocket subscribes to); `history_symbol` is the
Yahoo override used only for historical bars, because Finnhub's candle
endpoints are premium on our plan.

Note on existing data: this migration deliberately does NOT rewrite
`securities.symbol`. Symbols are stored as plain strings on `transactions` and
`tax_lots` (no FK), so renaming a security here would silently orphan its
history. Existing crypto rows that were seeded in Yahoo format are given a
`history_symbol` so charts keep working; converting them to Finnhub format so
they actually stream is a separate, deliberate data migration.

Revision ID: a1c4f2b9e07d
Revises: 46e6748cbe94
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4f2b9e07d'
down_revision: Union[str, Sequence[str], None] = '46e6748cbe94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'securities',
        sa.Column('history_symbol', sa.String(), nullable=True),
    )

    # Anything already stored in Yahoo's crypto convention ("BTC-USD") can
    # keep charting: point history_symbol at the existing value.
    # Bound parameter rather than a literal '%-USD' so the '%' can't be
    # mistaken for a paramstyle placeholder by the DBAPI.
    op.execute(
        sa.text(
            "UPDATE securities SET history_symbol = symbol "
            "WHERE symbol LIKE :pattern"
        ).bindparams(pattern='%-USD')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('securities', 'history_symbol')
