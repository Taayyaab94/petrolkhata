"""add email/verification to user, password reset token

Stage 2 of multi-tenant support: adds login-identity/email-verification
fields to `user` (see User's docstring in models.py for why email has to
be GLOBALLY unique rather than per-pump, unlike username) and the
`password_reset_token` table backing both the forgot-password and
email-verification flows (see PasswordResetToken's docstring).

Both changes are purely additive on already-existing tables/rows:
- user.email / user.email_verified_at are added NULLABLE with no
  default, so every pre-existing row (owner/staff created before Stage
  2, who never went through /signup) just gets NULL for both - exactly
  "no email on file yet / not verified", which is the correct reading
  for them. No backfill needed or possible (there is no real email to
  backfill from for a pre-Stage-2 account).
- password_reset_token is a brand new table; nothing to backfill.

The user.email unique constraint is named explicitly (uq_user_email)
rather than left anonymous (which is what plain `unique=True` in
models.py produces) for the same reason 7beb2487b281 explicitly named
its own added constraints: SQLite's batch-mode table recreate requires
every constraint it ADDS (as opposed to ones it merely carries forward
from reflection) to have a name to create AND to later drop by - an
anonymous one can't be targeted by drop_constraint() on downgrade.

Revision ID: ce5be2e3dfbf
Revises: 7beb2487b281
Create Date: 2026-08-15 21:38:53.790687

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ce5be2e3dfbf'
down_revision = '7beb2487b281'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'password_reset_token',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('purpose', sa.String(length=10), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('pump_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['pump_id'], ['pump.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('password_reset_token', schema=None) as batch_op:
        batch_op.create_index('ix_password_reset_token_hash', ['token_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_password_reset_token_pump_id'), ['pump_id'], unique=False)

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('email_verified_at', sa.DateTime(), nullable=True))
        # Explicitly named - see the module docstring above for why an
        # anonymous constraint here would break downgrade() on SQLite.
        batch_op.create_unique_constraint('uq_user_email', ['email'])


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_constraint('uq_user_email', type_='unique')
        batch_op.drop_column('email_verified_at')
        batch_op.drop_column('email')

    with op.batch_alter_table('password_reset_token', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_password_reset_token_pump_id'))
        batch_op.drop_index('ix_password_reset_token_hash')

    op.drop_table('password_reset_token')
