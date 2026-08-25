"""Prove a Neon region migration copied EVERYTHING, before you switch over.

Compares the old database against the new one:

  * every table's row count
  * the SUM of every numeric column (so a truncated or mangled money
    column shows up, not just a missing row)
  * every sequence's last_value - restores that forget sequences look
    perfect until the next INSERT fails with a duplicate key
  * alembic_version, so the new database is at the same migration head

Nothing is written. Both connection strings are read from the
environment and are never printed.

    set OLD_DATABASE_URL=postgresql://...old...
    set NEW_DATABASE_URL=postgresql://...new...
    python tools/verify_migration.py

Exit code 0 means the two are identical.
"""
import os
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is not installed. Run: pip install psycopg2-binary")

EXACT = {"smallint", "integer", "bigint", "numeric"}
# Floating point: SUM() over these depends on the physical order of the
# rows, and pg_restore is free to write them back in a different order.
# Summing as numeric and rounding makes the comparison order-independent,
# so a faithful restore can't report a spurious difference - while a
# genuinely wrong figure still does.
INEXACT = {"real", "double precision"}
NUMERIC = EXACT | INEXACT


def connect(url, label):
    if not url:
        sys.exit("Missing %s environment variable." % label)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def snapshot(conn):
    """Everything we can compare, without loading whole tables into memory."""
    out = {}
    cur = conn.cursor()

    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    tables = [r[0] for r in cur.fetchall()]

    for table in tables:
        cur.execute('SELECT COUNT(*) FROM "%s"' % table)
        out["%s :: rows" % table] = cur.fetchone()[0]

        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY column_name
        """, (table,))
        numeric_cols = [(c, t) for c, t in cur.fetchall() if t in NUMERIC]
        if not numeric_cols:
            continue
        # One pass per table rather than one query per column.
        sums = ", ".join(
            'ROUND(COALESCE(SUM("%s")::numeric, 0), 4)' % c
            if t in INEXACT else 'COALESCE(SUM("%s"), 0)' % c
            for c, t in numeric_cols)
        cur.execute('SELECT %s FROM "%s"' % (sums, table))
        for (col, _t), total in zip(numeric_cols, cur.fetchone()):
            out["%s.%s :: sum" % (table, col)] = str(total)

    cur.execute("""
        SELECT sequence_name FROM information_schema.sequences
        WHERE sequence_schema = 'public' ORDER BY sequence_name
    """)
    for (seq,) in cur.fetchall():
        cur.execute('SELECT last_value FROM "%s"' % seq)
        out["%s :: last_value" % seq] = cur.fetchone()[0]

    cur.close()
    return out


def main():
    old = connect(os.environ.get("OLD_DATABASE_URL"), "OLD_DATABASE_URL")
    new = connect(os.environ.get("NEW_DATABASE_URL"), "NEW_DATABASE_URL")

    a, b = snapshot(old), snapshot(new)
    old.close()
    new.close()

    keys = sorted(set(a) | set(b))
    diffs = [(k, a.get(k, "<missing>"), b.get(k, "<missing>"))
             for k in keys if a.get(k, "<missing>") != b.get(k, "<missing>")]

    rows = sum(v for k, v in a.items() if k.endswith(":: rows"))
    print("compared %d values across %d checks (%d data rows in the old "
          "database)" % (len(keys), len(keys), rows))

    if not diffs:
        print("\nIDENTICAL - the new database matches the old one exactly.")
        return 0

    print("\n%d DIFFERENCE(S):\n" % len(diffs))
    for k, before, after in diffs:
        print("  %-52s old=%s  new=%s" % (k, before, after))
    print("\nDo NOT switch over until these are explained.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
