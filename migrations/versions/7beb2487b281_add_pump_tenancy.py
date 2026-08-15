"""add pump tenancy

Stage 1 of multi-tenant support: introduces the `pump` table (the tenant
root - see models.Pump) and a `pump_id` column on every business table
(see models.TenantScoped), then replaces 7 previously-global unique
constraints with per-pump composite ones.

Written to work correctly on a POPULATED database, not just an empty one:
`pump_id` cannot simply be added as NOT NULL in one step, because SQLite
(and Postgres) will refuse a NOT NULL column with no default on a table
that already has rows. So each table is migrated in three phases, in this
exact order:

  1. add pump_id as NULLABLE (plus its index and FK to pump.id) - safe on
     a populated table, since nothing is required yet.
  2. backfill every existing row's pump_id to the single default pump
     this migration creates, via a raw bulk UPDATE (not the ORM - the
     ORM's tenant filter/stamping in tenancy.py isn't relevant here and
     wouldn't even be wired up during a migration anyway).
  3. tighten pump_id to NOT NULL, now that every row has one. For the 7
     tables listed in COMPOSITE_UNIQUES, this same phase also swaps the
     old single-column UNIQUE for the new (pump_id, column) composite.

Revision ID: 7beb2487b281
Revises: 73feef9be953
Create Date: 2026-08-15 16:37:36.150874

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7beb2487b281'
down_revision = '73feef9be953'
branch_labels = None
depends_on = None


# Every business table gets a pump_id - see models.TenantScoped's
# docstring for why even tables that could derive their pump through a
# join (e.g. fuel_price_history -> fuel_type) get their own column
# instead. Alphabetical, purely to keep this migration's diff readable.
TENANT_TABLES = [
    "account", "bank_account", "bank_sale", "cash_account", "cash_deposit",
    "cash_handover", "credit_given", "dispenser", "employee_loan", "expense",
    "fuel_price_history", "fuel_type", "nozzle", "nozzle_reset", "nozzle_testing",
    "product", "product_purchase", "product_rate_history", "product_sale",
    "receipt", "salary_payment", "sale", "sales_return", "shift",
    "stock_purchase", "supplier_payment", "tank", "tank_dip", "tank_dip_chart",
    "user",
]

# (table, column, new composite constraint name) - the 7 columns that used
# to be globally unique (a bare column-level unique=True in models.py)
# and are now unique per pump instead (see models.py's __table_args__ on
# each of these classes).
COMPOSITE_UNIQUES = [
    ("user", "username", "uq_user_pump_username"),
    ("shift", "name", "uq_shift_pump_name"),
    ("fuel_type", "name", "uq_fueltype_pump_name"),
    ("tank", "number", "uq_tank_pump_number"),
    ("dispenser", "number", "uq_dispenser_pump_number"),
    ("bank_account", "name", "uq_bankaccount_pump_name"),
    ("product", "name", "uq_product_pump_name"),
]
COMPOSITE_BY_TABLE = {t: (col, name) for t, col, name in COMPOSITE_UNIQUES}

# Every row already in the database belongs to one seed pump once this
# migration finishes - there is no signup flow before this point, so every
# pre-existing row has to land somewhere. Its id is whatever the database
# allocates (read back in upgrade()), NOT a hardcoded 1 - see the insert
# there for why pinning it would break the Postgres sequence.

# Only used to give a deterministic, droppable name to the OLD unique
# constraints being replaced below, which were created anonymously (a
# bare `Column(unique=True)` in the pre-Stage-1 models.py, with no
# explicit __table_args__ name). SQLite reflects an anonymous unique
# constraint with name=None - batch_alter_table can't target a
# constraint to drop without a name (confirmed empirically: passing None
# raises "Constraint must have a name", and the anonymous constraint is
# otherwise carried forward as-is by the table recreate, NOT silently
# dropped) - so this convention is handed to batch_alter_table purely so
# reflection assigns the anonymous constraint the same name this
# migration then drops it by. It intentionally matches the
# `uq_<table>_<column>` fallback used below for Postgres, where the
# column's constraint is never anonymous in the first place (Postgres
# auto-names it `<table>_<column>_key` at CREATE TABLE time) - so this
# naming_convention never actually renames anything there, it only fills
# in a name for SQLite's reflected None.
UNIQUE_NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _find_single_column_unique_name(inspector, table, column):
    """The name of TABLE's existing unique constraint on exactly [column],
    if any, as actually reflected right now (before naming_convention is
    applied) - so this returns the real name on Postgres
    (`<table>_<column>_key`) and None on SQLite (anonymous)."""
    for uc in inspector.get_unique_constraints(table):
        if uc["column_names"] == [column]:
            return uc["name"]
    return None


def upgrade():
    bind = op.get_bind()

    op.create_table(
        "pump",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # Raw Core SQL, not the ORM. The id is deliberately NOT supplied:
    # pump.id is an auto-incrementing integer primary key, which on
    # Postgres means a SERIAL backed by a sequence, and inserting an
    # EXPLICIT id does not advance that sequence. Seeding id=1 by hand
    # would leave the sequence still pointing at 1, so the very first
    # pump created afterwards through signup would collide on the primary
    # key and every signup would fail. Letting the database allocate the
    # id keeps the sequence honest; the id it chose is then read back for
    # the backfill below. (SQLite has no sequence to desync, which is
    # exactly why this only ever shows up in production.)
    op.execute(
        sa.text(
            "INSERT INTO pump (name, created_at) VALUES (:name, CURRENT_TIMESTAMP)"
        ).bindparams(name="My Pump")
    )
    default_pump_id = bind.execute(sa.text("SELECT MIN(id) FROM pump")).scalar()

    # --- Phase 1: add pump_id as NULLABLE everywhere -----------------------
    for table in TENANT_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("pump_id", sa.Integer(), nullable=True))
            batch_op.create_index(f"ix_{table}_pump_id", ["pump_id"], unique=False)
            # Explicitly named (rather than None/anonymous, which is what
            # a plain `db.ForeignKey(...)` in models.py produces): batch
            # mode's SQLite table-recreate requires every constraint it
            # ADDS to have a name, unlike ones it merely carries forward
            # from reflection.
            batch_op.create_foreign_key(
                f"fk_{table}_pump_id_pump", "pump", ["pump_id"], ["id"]
            )

    # --- Phase 2: backfill every existing row ------------------------------
    # Table names are quoted through the dialect's own identifier preparer
    # rather than interpolated bare. "user" is a RESERVED WORD in Postgres
    # (it's the niladic current-user function), so a bare
    # `UPDATE user SET ...` is a syntax error there - while SQLite accepts
    # it happily, which is why this passed every local test and then took
    # the live site down on deploy. The preparer quotes exactly what each
    # dialect needs and leaves everything else alone.
    preparer = bind.dialect.identifier_preparer
    for table in TENANT_TABLES:
        op.execute(
            sa.text(
                f"UPDATE {preparer.quote(table)} SET pump_id = :pid"
            ).bindparams(pid=default_pump_id)
        )

    # --- Phase 3: tighten to NOT NULL, swap the 7 unique constraints -------
    inspector = sa.inspect(bind)
    for table in TENANT_TABLES:
        old_unique_name = None
        if table in COMPOSITE_BY_TABLE:
            col, _new_name = COMPOSITE_BY_TABLE[table]
            old_unique_name = _find_single_column_unique_name(inspector, table, col)
            # Falls back to the same name UNIQUE_NAMING_CONVENTION would
            # assign that constraint on reflection (see its docstring) -
            # needed on SQLite, where the constraint is real but unnamed.
            drop_name = old_unique_name or f"uq_{table}_{col}"

        naming_convention = UNIQUE_NAMING_CONVENTION if table in COMPOSITE_BY_TABLE else None
        with op.batch_alter_table(
            table, schema=None, naming_convention=naming_convention
        ) as batch_op:
            batch_op.alter_column("pump_id", existing_type=sa.Integer(), nullable=False)
            if table in COMPOSITE_BY_TABLE:
                col, new_name = COMPOSITE_BY_TABLE[table]
                batch_op.drop_constraint(drop_name, type_="unique")
                batch_op.create_unique_constraint(new_name, ["pump_id", col])


def downgrade():
    # Reverse of phase 3: drop the composite uniques and restore the
    # original single-column ones, then drop pump_id (FK/index/column)
    # from every table, then the pump table itself. Reverse-alphabetical
    # order purely to mirror the upgrade path.
    for table in reversed(TENANT_TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            if table in COMPOSITE_BY_TABLE:
                col, new_name = COMPOSITE_BY_TABLE[table]
                batch_op.drop_constraint(new_name, type_="unique")
                batch_op.create_unique_constraint(f"uq_{table}_{col}", [col])
            batch_op.drop_constraint(f"fk_{table}_pump_id_pump", type_="foreignkey")
            batch_op.drop_index(f"ix_{table}_pump_id")
            batch_op.drop_column("pump_id")

    op.drop_table("pump")
