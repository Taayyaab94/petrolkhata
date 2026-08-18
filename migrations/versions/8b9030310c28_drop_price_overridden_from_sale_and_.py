"""drop price_overridden from sale and direct_sale

Revision ID: 8b9030310c28
Revises: 9ba9e030090d
Create Date: 2026-08-18 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8b9030310c28'
down_revision = '9ba9e030090d'
branch_labels = None
depends_on = None


def upgrade():
    # Nozzle-reading and direct-sale price overrides were reverted - fuel
    # sold via meter readings/direct entry is always priced at
    # price_on_date() again, so this flag no longer has a purpose.
    # Discounts now live only on CreditGiven (credit-to-customer entries).
    with op.batch_alter_table('sale', schema=None) as batch_op:
        batch_op.drop_column('price_overridden')

    with op.batch_alter_table('direct_sale', schema=None) as batch_op:
        batch_op.drop_column('price_overridden')


def downgrade():
    with op.batch_alter_table('direct_sale', schema=None) as batch_op:
        batch_op.add_column(sa.Column('price_overridden', sa.Boolean(), nullable=False, server_default=sa.false()))

    with op.batch_alter_table('sale', schema=None) as batch_op:
        batch_op.add_column(sa.Column('price_overridden', sa.Boolean(), nullable=False, server_default=sa.false()))
