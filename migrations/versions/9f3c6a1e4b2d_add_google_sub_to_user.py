"""add google_sub to user

Google Sign-In (Authlib, no Clerk). Adds one column to `user`:

- google_sub: Google's `sub` claim - stable, and the only safe join key
  once a Google account is linked (a Google account's email can change;
  `sub` cannot). Nullable with no backfill - existing rows are untouched,
  and most users will never link Google at all. Globally unique, named
  explicitly (uq_user_google_sub) for the same reason ce5be2e3dfbf named
  uq_user_email: SQLite's batch-mode table recreate needs a name to drop
  a constraint it added, and an anonymous one (plain unique=True in
  models.py) can't be targeted later.

Revision ID: 9f3c6a1e4b2d
Revises: c7d94e21ab63
Create Date: 2026-08-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9f3c6a1e4b2d'
down_revision = 'c7d94e21ab63'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('google_sub', sa.String(length=255), nullable=True))
        batch_op.create_unique_constraint('uq_user_google_sub', ['google_sub'])


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_constraint('uq_user_google_sub', type_='unique')
        batch_op.drop_column('google_sub')
