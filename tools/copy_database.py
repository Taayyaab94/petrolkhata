"""Copy this app's database into a brand-new, EMPTY one - no pg_dump needed.

Written for the Virginia -> Singapore region move. It:

  1. refuses to run unless the target is empty, so it can never overwrite
     live data;
  2. builds the schema on the target from this project's own Alembic
     migrations - the same migrations that built the original;
  3. copies every table with COPY, naming columns explicitly so a
     difference in column ORDER between the two databases cannot silently
     shift values into the wrong column;
  4. sets every sequence to match the source (a copy that forgets this
     looks perfect until the next saved entry fails on a duplicate key);
  5. runs the full comparison from verify_migration.py and reports.

The source database is only ever READ from.

Both connection strings come from the environment and are never printed -
only the host's region is shown, so you can confirm where each one lives.

    set OLD_DATABASE_URL=postgresql://...   (the current, Virginia one)
    set NEW_DATABASE_URL=postgresql://...   (the new, Singapore one)
    python tools/copy_database.py

Use the DIRECT (non-pooled) connection string for both - a pooler runs in
transaction mode and will break the COPY stream.
"""
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is not installed. Run: pip install psycopg2-binary")

from verify_migration import snapshot  # noqa: E402  (same folder)


def normalise(url, label):
    if not url:
        sys.exit("Missing %s environment variable." % label)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def describe_host(url):
    """Region and host prefix only - never the password."""
    m = re.search(r"@([^/:?]+)", url)
    host = m.group(1) if m else "?"
    region = "unknown region"
    for token, name in (
        ("us-east-1", "Virginia (us-east-1)"),
        ("us-east-2", "Ohio (us-east-2)"),
        ("us-west-2", "Oregon (us-west-2)"),
        ("eu-central-1", "Frankfurt (eu-central-1)"),
        ("eu-west-2", "London (eu-west-2)"),
        ("ap-southeast-1", "SINGAPORE (ap-southeast-1)"),
        ("ap-southeast-2", "Sydney (ap-southeast-2)"),
        ("sa-east-1", "Sao Paulo (sa-east-1)"),
    ):
        if token in host:
            region = name
            break
    pooled = "-pooler" in host
    return region, pooled


def tables_in_fk_order(cur):
    """Every table, ordered so a table always comes after the tables it
    references. Self-references (Account.parent_account_id) are ignored -
    they are satisfied within a single table's own copy."""
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)
    tables = {r[0] for r in cur.fetchall()}
    tables.discard("alembic_version")

    cur.execute("""
        SELECT src.relname, tgt.relname
        FROM pg_constraint c
        JOIN pg_class src ON src.oid = c.conrelid
        JOIN pg_class tgt ON tgt.oid = c.confrelid
        JOIN pg_namespace n ON n.oid = src.relnamespace
        WHERE c.contype = 'f' AND n.nspname = 'public'
    """)
    deps = {t: set() for t in tables}
    for child, parent in cur.fetchall():
        if child in tables and parent in tables and child != parent:
            deps[child].add(parent)

    ordered, remaining = [], dict(deps)
    while remaining:
        ready = sorted(t for t, d in remaining.items()
                       if not (d & set(remaining)))
        if not ready:                      # a cycle - copy the rest as-is
            ordered.extend(sorted(remaining))
            break
        ordered.extend(ready)
        for t in ready:
            remaining.pop(t)
    return ordered


def self_ref_columns(cur, table):
    """Columns whose foreign key points back at their own table (today:
    account.parent_account_id).

    Postgres checks a foreign key as each row is inserted, so a child row
    landing before its parent inside the SAME copy would be rejected -
    and which comes first depends on physical row order, which is not
    something to leave to chance with real data. These columns are
    therefore copied as NULL and filled in afterwards, once every row of
    the table exists."""
    cur.execute("""
        SELECT a.attname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN LATERAL unnest(c.conkey) AS k(attnum) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'f' AND c.conrelid = c.confrelid
          AND n.nspname = 'public' AND t.relname = %s
    """, (table,))
    return [r[0] for r in cur.fetchall()]


def primary_key(cur, table):
    cur.execute("""
        SELECT a.attname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN LATERAL unnest(c.conkey) AS k(attnum) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'p' AND n.nspname = 'public' AND t.relname = %s
    """, (table,))
    rows = [r[0] for r in cur.fetchall()]
    return rows[0] if len(rows) == 1 else None


def columns(cur, table):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table,))
    return [r[0] for r in cur.fetchall()]


def build_schema(new_url):
    """Run this project's migrations against the target database."""
    script = (
        "import os, sys;"
        "sys.path.insert(0, %r);"
        "import app as A;"
        "from flask_migrate import upgrade;"
        "ctx = A.app.app_context(); ctx.push();"
        "upgrade(directory=os.path.join(%r, 'migrations'));"
        "print('migrations applied')" % (ROOT, ROOT)
    )
    env = dict(os.environ)
    env["DATABASE_URL"] = new_url
    env["SKIP_DB_BOOTSTRAP"] = "1"        # no default pump/shift/users
    env.setdefault("SECRET_KEY", "copy-database-temp-key")
    r = subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:], file=sys.stderr)
        sys.exit("Could not build the schema on the new database.")
    print("   " + r.stdout.strip().splitlines()[-1])


def main():
    old_url = normalise(os.environ.get("OLD_DATABASE_URL"), "OLD_DATABASE_URL")
    new_url = normalise(os.environ.get("NEW_DATABASE_URL"), "NEW_DATABASE_URL")

    old_region, old_pooled = describe_host(old_url)
    new_region, new_pooled = describe_host(new_url)
    print("source : %s%s" % (old_region, "  [POOLED]" if old_pooled else ""))
    print("target : %s%s" % (new_region, "  [POOLED]" if new_pooled else ""))
    if old_pooled or new_pooled:
        sys.exit("\nOne of these is a POOLED connection string (it has "
                 "'-pooler' in the host).\nUse the direct one for both - a "
                 "pooler breaks the COPY stream.")
    if old_url == new_url:
        sys.exit("\nBoth variables point at the same database.")

    def connect(url, which):
        try:
            return psycopg2.connect(url)
        except psycopg2.Error as e:
            # str(e) can echo the host but never the password.
            sys.exit("\nCould not connect to the %s database:\n  %s\n"
                     "Check that you pasted the whole connection string, "
                     "including the ?sslmode=require at the end."
                     % (which, str(e).strip().splitlines()[0]))

    old = connect(old_url, "OLD (source)")
    new = connect(new_url, "NEW (target)")
    old.set_session(readonly=True)
    oc, nc = old.cursor(), new.cursor()

    # --- safety: the target must be empty -------------------------------
    nc.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)
    existing = [r[0] for r in nc.fetchall()]
    for t in existing:
        nc.execute('SELECT COUNT(*) FROM "%s"' % t)
        n = nc.fetchone()[0]
        if n and t != "alembic_version":
            sys.exit("\nThe target already has data (%s has %d rows).\n"
                     "This script only ever writes into an EMPTY database. "
                     "Delete and recreate it, then run again." % (t, n))

    # --- schema ----------------------------------------------------------
    if not existing:
        print("\nbuilding schema from this project's migrations...")
        build_schema(new_url)
        new.close()
        new = psycopg2.connect(new_url)
        nc = new.cursor()
    else:
        print("\ntarget already has the schema (and no data) - reusing it")

    # --- data -------------------------------------------------------------
    order = tables_in_fk_order(oc)
    print("\ncopying %d tables..." % len(order))
    total = 0
    todo = []          # self-referencing links to restore after the copy
    for table in order:
        src_cols = columns(oc, table)
        dst_cols = set(columns(nc, table))
        missing = [c for c in src_cols if c not in dst_cols]
        if missing:
            sys.exit("Table %s: the new database has no column(s) %s. "
                     "The two schemas do not match; stopping before any "
                     "data is mangled." % (table, ", ".join(missing)))

        deferred = self_ref_columns(oc, table)
        collist = ", ".join('"%s"' % c for c in src_cols)
        # Self-referencing columns go over as NULL and are filled in below.
        selectlist = ", ".join(
            ("NULL AS \"%s\"" % c) if c in deferred else ('"%s"' % c)
            for c in src_cols)

        buf = io.StringIO()
        oc.copy_expert('COPY (SELECT %s FROM "%s") TO STDOUT WITH (FORMAT csv)'
                       % (selectlist, table), buf)
        buf.seek(0)
        nc.copy_expert('COPY "%s" (%s) FROM STDIN WITH (FORMAT csv)'
                       % (table, collist), buf)
        nc.execute('SELECT COUNT(*) FROM "%s"' % table)
        n = nc.fetchone()[0]
        total += n
        if deferred:
            pk = primary_key(oc, table)
            if not pk:
                sys.exit("Table %s references itself but has no single-column "
                         "primary key; cannot fill in %s safely."
                         % (table, ", ".join(deferred)))
            todo.append((table, pk, deferred))
        print("   %-26s %7d rows%s" % (table, n,
              "   (%s filled in below)" % ", ".join(deferred) if deferred else ""))

    # Now that every row exists, restore the self-referencing links.
    for table, pk, cols in todo:
        sel = ", ".join('"%s"' % c for c in [pk] + cols)
        oc.execute('SELECT %s FROM "%s" WHERE %s' % (
            sel, table, " OR ".join('"%s" IS NOT NULL' % c for c in cols)))
        rows = oc.fetchall()
        assigns = ", ".join('"%s" = %%s' % c for c in cols)
        for row in rows:
            nc.execute('UPDATE "%s" SET %s WHERE "%s" = %%s'
                       % (table, assigns, pk), tuple(row[1:]) + (row[0],))
        print("   restored %d self-reference(s) in %s" % (len(rows), table))
    new.commit()

    # --- sequences --------------------------------------------------------
    print("\nsetting sequences...")
    oc.execute("""
        SELECT sequence_name FROM information_schema.sequences
        WHERE sequence_schema = 'public' ORDER BY sequence_name
    """)
    seqs = [r[0] for r in oc.fetchall()]
    for seq in seqs:
        oc.execute('SELECT last_value, is_called FROM "%s"' % seq)
        last, called = oc.fetchone()
        nc.execute("SELECT setval(%s, %s, %s)", (seq, last, called))
    new.commit()
    print("   %d sequences set" % len(seqs))

    # --- verify ------------------------------------------------------------
    print("\nverifying (row counts, column totals, sequences, "
          "migration version)...")
    a, b = snapshot(old), snapshot(new)
    old.close()
    new.close()

    keys = sorted(set(a) | set(b))
    diffs = [(k, a.get(k, "<missing>"), b.get(k, "<missing>"))
             for k in keys if a.get(k, "<missing>") != b.get(k, "<missing>")]

    print("   compared %d values, %d rows copied in total" % (len(keys), total))
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
