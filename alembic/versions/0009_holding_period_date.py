"""separate holding_period_date from opened_date on tax lots

Wash-sale tacking previously backdated opened_date in place on the whole
replacement lot, which had two bugs: two losses washing into one lot
compounded the backdating (shares aged twice), and a partial wash smeared
the basis bump and the age across clean shares that were never replacements.

Now opened_date is always the actual purchase date (FIFO and the wash
window need it), and holding_period_date — null meaning "same as
opened_date" — carries the tacked date. A partial wash SPLITS the
replacement lot: the washed shares become their own lot with adjusted
basis and tacked holding date; the clean shares keep their real basis
and age.

Revision ID: 0009_holding_period_date
Revises: 0008_deferral_settings
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009_holding_period_date'
down_revision: Union[str, Sequence[str], None] = '0008_deferral_settings'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'tax_lots',
        sa.Column('holding_period_date', sa.Date(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tax_lots', 'holding_period_date')
