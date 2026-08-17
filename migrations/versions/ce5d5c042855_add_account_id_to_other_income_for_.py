"""add account_id to other_income for credit method

Revision ID: ce5d5c042855
Revises: fe480775b5a0
Create Date: 2026-08-17 23:46:15.818895

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ce5d5c042855'
down_revision = 'fe480775b5a0'
branch_labels = None
depends_on = None


def upgrade():
    # Explicitly named (rather than None/anonymous, which is what a plain
    # db.ForeignKey(...) in models.py produces): batch mode's SQLite
    # table-recreate requires every constraint it ADDS to have a name, and
    # a name is required to target it for drop_constraint() in downgrade()
    # below - same lesson as fk_{table}_pump_id_pump in
    # 7beb2487b281_add_pump_tenancy.py.
    with op.batch_alter_table('other_income', schema=None) as batch_op:
        batch_op.add_column(sa.Column('account_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_other_income_account_id_account', 'account', ['account_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('other_income', schema=None) as batch_op:
        batch_op.drop_constraint('fk_other_income_account_id_account', type_='foreignkey')
        batch_op.drop_column('account_id')
