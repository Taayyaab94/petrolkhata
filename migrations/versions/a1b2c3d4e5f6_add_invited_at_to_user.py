"""add invited_at to user

Invite-based team management (Spec B). Adds one column to `user`:

- invited_at: set only by settings_invite_user() when the owner invites a
  colleague by email instead of typing a password for them. Together with
  is_active_user (False until accepted) and email_verified_at (None until
  accepted), this is what makes User.is_pending_invite derivable rather
  than stored as its own separate flag. Nullable with no backfill -
  existing rows were all created directly (signup or the old
  settings_add_user), never invited, so they correctly read as
  not-pending.

PasswordResetToken.purpose gains a third value, "invite" - it's already
String(10), so no column change is needed there.

Revision ID: a1b2c3d4e5f6
Revises: 9f3c6a1e4b2d
Create Date: 2026-08-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '9f3c6a1e4b2d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('invited_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('invited_at')
