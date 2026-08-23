"""add notes to account (free-text context per account)

Revision ID: c7d94e21ab63
Revises: b3f21a8c6d07
Create Date: 2026-08-23 00:00:00.000000

Adds ONE nullable TEXT column to `account` and touches nothing else.

There is deliberately no server_default and no backfill: every row that
already exists simply gets NULL, which is exactly what "this account has
no note" means. Nothing in the app reads `notes` to compute a balance, an
aging bucket, a total or a report figure - it is displayed and edited and
that is all - so a database that has just run this migration produces
byte-for-byte the same numbers it produced before it.

Downgrade drops the column, losing only the notes themselves. It is
written with batch_alter_table because SQLite cannot DROP COLUMN in place
on older versions and rebuilds the table instead; on Postgres batch mode
issues a plain ALTER TABLE and costs nothing.
"""
from alembic import op
import sqlalchemy as sa


revision = "c7d94e21ab63"
down_revision = "b3f21a8c6d07"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.add_column(sa.Column("notes", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.drop_column("notes")
