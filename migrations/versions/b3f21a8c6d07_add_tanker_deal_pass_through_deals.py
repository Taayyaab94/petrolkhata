"""add tanker_deal (Direct Sale from Tanker - pass-through deals)

Revision ID: b3f21a8c6d07
Revises: a1c7d4e90b52
Create Date: 2026-08-23 00:00:00.000000

Creates ONE new table and touches nothing else. No existing table is
altered, no column is added to one, and no data is backfilled or
rewritten - so every pre-existing row, balance, aging bucket and report
figure is untouched by construction: a database with no tanker_deal rows
computes byte-for-byte what it computed before this migration ran.

pump_id is NOT NULL and indexed because TankerDeal uses the TenantScoped
mixin (see models.py / tenancy.py): every read is filtered on it and every
write is stamped with it, so a row without one would be invisible to its
own pump and is refused before it reaches the database.

There are TWO foreign keys to `account` (supplier and customer) and TWO to
`bank_account` (the side that paid and the side that was paid into), so
every constraint is given an explicit name - both because an unnamed
constraint cannot be dropped again on SQLite, and because two anonymous
FKs to the same table are indistinguishable in a schema dump.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3f21a8c6d07'
down_revision = 'a1c7d4e90b52'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'tanker_deal',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pump_id', sa.Integer(), nullable=False),
        sa.Column('entry_date', sa.Date(), nullable=False),
        sa.Column('fuel_type_id', sa.Integer(), nullable=False),
        # Informational only - never fed into litres-sold or
        # margin-per-litre figures (see TankerDeal's docstring).
        sa.Column('liters', sa.Float(), nullable=False),
        sa.Column('purchase_cost', sa.Float(), nullable=False),
        sa.Column('purchase_payment_type', sa.String(length=10), nullable=False, server_default='cash'),
        sa.Column('purchase_bank_account_id', sa.Integer(), nullable=True),
        sa.Column('supplier_account_id', sa.Integer(), nullable=True),
        sa.Column('sale_amount', sa.Float(), nullable=False),
        sa.Column('sale_payment_type', sa.String(length=10), nullable=False, server_default='cash'),
        sa.Column('sale_bank_account_id', sa.Integer(), nullable=True),
        sa.Column('customer_account_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.String(length=200), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['pump_id'], ['pump.id'], name='fk_tanker_deal_pump_id'),
        sa.ForeignKeyConstraint(['fuel_type_id'], ['fuel_type.id'], name='fk_tanker_deal_fuel_type_id'),
        sa.ForeignKeyConstraint(
            ['purchase_bank_account_id'], ['bank_account.id'], name='fk_tanker_deal_purchase_bank_account_id'
        ),
        sa.ForeignKeyConstraint(
            ['sale_bank_account_id'], ['bank_account.id'], name='fk_tanker_deal_sale_bank_account_id'
        ),
        sa.ForeignKeyConstraint(
            ['supplier_account_id'], ['account.id'], name='fk_tanker_deal_supplier_account_id'
        ),
        sa.ForeignKeyConstraint(
            ['customer_account_id'], ['account.id'], name='fk_tanker_deal_customer_account_id'
        ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_tanker_deal_user_id'),
        sa.PrimaryKeyConstraint('id', name='pk_tanker_deal'),
    )
    with op.batch_alter_table('tanker_deal', schema=None) as batch_op:
        batch_op.create_index('ix_tanker_deal_pump_id', ['pump_id'], unique=False)


def downgrade():
    with op.batch_alter_table('tanker_deal', schema=None) as batch_op:
        batch_op.drop_index('ix_tanker_deal_pump_id')
    op.drop_table('tanker_deal')
