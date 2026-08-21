"""add parent_account_id to account (parent / sub-account grouping)

Revision ID: a1c7d4e90b52
Revises: 8b9030310c28
Create Date: 2026-08-22 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1c7d4e90b52'
down_revision = '8b9030310c28'
branch_labels = None
depends_on = None


def upgrade():
    # A self-referential, NULLABLE FK: NULL means "ordinary top-level
    # account", which is what every already-existing row becomes. There is
    # deliberately NO server_default and NO backfill - the entire point of
    # this column is that adding it changes nothing at all for existing
    # data; balances, aging and every report stay byte-for-byte what they
    # were, and accounts only start behaving as a group once an owner
    # explicitly links them from the UI.
    #
    # batch mode: SQLite cannot ADD a foreign key in place, so Alembic has
    # to rebuild the table - the FK is named explicitly because an
    # unnamed constraint cannot be dropped again by downgrade() on SQLite.
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_account_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_account_parent_account_id', 'account', ['parent_account_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.drop_constraint('fk_account_parent_account_id', type_='foreignkey')
        batch_op.drop_column('parent_account_id')
