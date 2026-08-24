"""Golden-master (characterisation) test for Petrol Khata.

This app has no unit tests and computes money. Before any structural
change, run:

    python tests/golden_master.py --save

which builds a deterministic dataset (tests/seed.py), records what every
read-side calculation and every page currently produces, and writes it to
tests/golden_master.json. After the change, run:

    python tests/golden_master.py --compare

which rebuilds the same dataset, recomputes everything, and fails on any
difference. It does NOT assert that today's answers are *correct* - it
asserts that they have not CHANGED, which is exactly the property a
refactor has to preserve.

Determinism is the whole game: anything that varies between two runs of
identical code (CSRF tokens, timestamps, object ids in HTML) is scrubbed
in _normalise_html, or the harness reports noise as a regression and
becomes worthless. `--selftest` proves two consecutive runs agree.
"""
import argparse
import inspect
import json
import os
import re
import sys
import tempfile
import datetime as _dt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SNAPSHOT = os.path.join(HERE, "golden_master.json")

# Must be set before importing app: Flask-SQLAlchemy binds the engine at
# init_app(), so a later config change is silently ignored and every
# write would land in the real instance/petrolpump.db.
_TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "gm.db").replace("\\", "/")
os.environ["SKIP_DB_BOOTSTRAP"] = "1"
os.environ["SECRET_KEY"] = "golden-master-fixed-key"
os.environ.pop("RESEND_API_KEY", None)
os.environ.pop("GOOGLE_CLIENT_ID", None)
os.environ.pop("GOOGLE_CLIENT_SECRET", None)

sys.path.insert(0, ROOT)

import app as A                      # noqa: E402
import ledger_logic as L             # noqa: E402
from extensions import db            # noqa: E402
from models import (                 # noqa: E402
    Account, BankAccount, CashAccount, FuelType, Nozzle, Product, Shift, Tank,
)
from tenancy import unscoped         # noqa: E402
from tests.seed import seed, d       # noqa: E402

assert _TMP.replace("\\", "/") in A.app.config["SQLALCHEMY_DATABASE_URI"], \
    "harness is not isolated from the real database"
A.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="gm.test")


# --------------------------------------------------------------- helpers ---

def _jsonable(v, depth=0):
    """Round floats to 6dp so platform float-repr noise can't masquerade
    as a behaviour change, and render dates/objects stably."""
    if depth > 8:
        return "<deep>"
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (int, str, bool)) or v is None:
        return v
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x, depth + 1) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple, set)):
        items = [_jsonable(x, depth + 1) for x in v]
        return sorted(items, key=str) if isinstance(v, set) else items
    if hasattr(v, "__tablename__"):
        return f"<{type(v).__name__} id={getattr(v, 'id', '?')}>"
    if hasattr(v, "_asdict"):
        return _jsonable(v._asdict(), depth + 1)
    return f"<{type(v).__name__}>"


_SCRUB = [
    # Per-request values that legitimately differ between two identical runs.
    (re.compile(r'name="csrf_token"[^>]*value="[^"]*"'), 'name="csrf_token" value="SCRUBBED"'),
    (re.compile(r'\bcsrf_token=[A-Za-z0-9._\-]+'), 'csrf_token=SCRUBBED'),
    (re.compile(r'\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?'), 'TIMESTAMP'),
    (re.compile(r'\b\d{2}:\d{2}:\d{2}\b'), 'TIME'),
    (re.compile(r'\s+'), ' '),
]


def _normalise_html(html):
    for pat, rep in _SCRUB:
        html = pat.sub(rep, html)
    return html.strip()


# ----------------------------------------------------------- the snapshot ---

# Read-side functions taking no arguments beyond ones we can supply.
def _call_ledger_logic(ids):
    """Call every public ledger_logic function whose parameters we can
    satisfy from the seeded ids, and record what it returns."""
    out = {}
    dates = [_dt.date.fromisoformat(s) for s in ids["dates"]]
    start, end = dates[0], dates[7]
    suppliers = {
        "as_of_date": dates[6], "date": dates[2], "entry_date": dates[2],
        "on_date": dates[2], "day": dates[2],
        "start": start, "end": end, "start_date": start, "end_date": end,
        "account": None, "account_id": ids["account_ids"]["child_a"],
        "bank_account": None, "bank_account_id": ids["bank_id"],
        "tank": None, "tank_id": ids["tank_ids"]["petrol"],
        "fuel_type": None, "fuel_type_id": ids["fuel_ids"]["petrol"],
        "product": None, "product_id": ids["product_id"],
        "nozzle_id": ids["nozzle_ids"]["petrol"],
        "shift_id": ids["shift_ids"][0],
    }
    tanks = Tank.query.filter_by(pump_id=ids["pump_id"]).order_by(Tank.id).all()
    tank_rows = L.tank_stock_rows(dates[6]) if hasattr(L, "tank_stock_rows") else []
    positions = L.credit_positions() if hasattr(L, "credit_positions") else []
    cash_acct = CashAccount.query.filter_by(pump_id=ids["pump_id"]).first()
    children = [db.session.get(Account, ids["account_ids"]["child_a"]),
                db.session.get(Account, ids["account_ids"]["child_b"])]
    suppliers.update({
        "account": db.session.get(Account, ids["account_ids"]["child_a"]),
        "bank_account": db.session.get(BankAccount, ids["bank_id"]),
        "tank": tanks[0] if tanks else None,
        "tanks": tanks,
        "product": db.session.get(Product, ids["product_id"]),
        "cash_account": cash_acct,
        "fuel_type": db.session.get(FuelType, ids["fuel_ids"]["petrol"]),
        "nozzle": db.session.get(Nozzle, ids["nozzle_ids"]["petrol"]),
        "shift": db.session.get(Shift, ids["shift_ids"][0]),
        "children": children,
        "amount": 10000.0,
        "positions": positions,
        "tank_rows": tank_rows,
        "variance_rows": [],
        "product_rows": [],
        "costs": {},
        "dates": dates,
        "depth_cm": 100.0,
        "dt": _dt.datetime(2026, 6, 1, 12, 0, 0),
        "now": _dt.datetime(2026, 6, 8, 12, 0, 0),
        "model": None,
        "value_col": None,
        "total_liters": 100.0,
        "hypothetical_changes": [],
    })
    suppliers = {k: v for k, v in suppliers.items() if v is not None or k in ("model", "value_col")}

    # These MUTATE. A characterisation snapshot must be a pure read, or the
    # baseline run and the compare run start from different data. (The
    # --selftest determinism gate would catch an omission here, since a
    # mutating call makes two consecutive runs disagree.)
    writers = {"sync_sale_testing", "reprice_entries", "record_fuel_price",
               "record_product_rates"}
    for name in sorted(dir(L)):
        if name.startswith("_") or name in writers:
            continue
        fn = getattr(L, name)
        if not inspect.isfunction(fn) or fn.__module__ != "ledger_logic":
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        kwargs, ok = {}, True
        for pname, p in sig.parameters.items():
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            if pname in suppliers and suppliers[pname] is not None:
                kwargs[pname] = suppliers[pname]
            elif p.default is not p.empty:
                pass
            else:
                ok = False
                break
        if not ok:
            out[name] = "<skipped: unsatisfiable signature>"
            continue
        try:
            out[name] = _jsonable(fn(**kwargs))
        except Exception as e:                       # noqa: BLE001
            # A raise is itself behaviour worth pinning.
            out[name] = f"<raised {type(e).__name__}: {str(e)[:160]}>"
    return out


def _balances(ids):
    out = {}
    with unscoped():
        for a in Account.query.filter_by(pump_id=ids["pump_id"]).order_by(Account.id).all():
            out[f"account:{a.name}:balance"] = _jsonable(a.balance)
            out[f"account:{a.name}:group_balance"] = _jsonable(a.group_balance)
        for b in BankAccount.query.filter_by(pump_id=ids["pump_id"]).order_by(BankAccount.id).all():
            out[f"bank:{b.name}:balance"] = _jsonable(b.balance)
        for c in CashAccount.query.filter_by(pump_id=ids["pump_id"]).all():
            out["cash:balance"] = _jsonable(getattr(c, "balance", None))
    return out


GET_ROUTES = [
    "/dashboard", "/ledger", "/accounts", "/inventory", "/reports",
    "/reports/monthly", "/reports/trends", "/settings", "/accounts/cash",
]


def _pages(client, ids):
    out = {}
    for path in GET_ROUTES:
        try:
            r = client.get(path)
            out[path] = {"status": r.status_code,
                         "html": _normalise_html(r.get_data(as_text=True))}
        except Exception as e:                       # noqa: BLE001
            out[path] = {"status": "EXC", "html": f"{type(e).__name__}: {e}"}
    # Per-entity detail pages
    for label, path in (
        ("account_detail", f"/accounts/{ids['account_ids']['child_a']}"),
        ("parent_detail", f"/accounts/{ids['account_ids']['parent']}"),
        ("supplier_detail", f"/accounts/{ids['account_ids']['supplier']}"),
        ("ledger_dated", f"/ledger?date={ids['dates'][2]}"),
        ("bank_detail", f"/accounts/bank/{ids['bank_id']}"),
        ("statement", f"/accounts/{ids['account_ids']['child_a']}/statement"),
    ):
        r = client.get(path)
        out[label] = {"status": r.status_code,
                      "html": _normalise_html(r.get_data(as_text=True))}
    return out


def build():
    """Fresh database -> seed -> snapshot everything."""
    with A.app.app_context():
        db.drop_all()
        db.create_all()
        ids = seed()

    snap = {"ids": {k: v for k, v in ids.items() if k != "password"}}
    with A.app.app_context(), unscoped():
        snap["ledger_logic"] = _call_ledger_logic(ids)
        snap["balances"] = _balances(ids)

    with A.app.test_client() as c:
        c.post("/login", data={"username": ids["owner_email"],
                               "password": ids["password"]})
        snap["pages"] = _pages(c, ids)
    return snap


# ------------------------------------------------------------- comparison ---

def _flatten(obj, prefix=""):
    flat = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            flat.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            flat.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        flat[prefix] = obj
    return flat


def compare(old, new):
    a, b = _flatten(old), _flatten(new)
    diffs = []
    for k in sorted(set(a) | set(b)):
        if k not in a:
            diffs.append((k, "<absent>", b[k]))
        elif k not in b:
            diffs.append((k, a[k], "<absent>"))
        elif a[k] != b[k]:
            diffs.append((k, a[k], b[k]))
    return diffs


def _report(diffs, limit=40):
    if not diffs:
        print("IDENTICAL - no behaviour change detected.")
        return 0
    print(f"{len(diffs)} DIFFERENCE(S) DETECTED\n")
    for k, before, after in diffs[:limit]:
        sb, sa = str(before), str(after)
        if len(sb) > 220 or len(sa) > 220:
            # Long HTML: show the first divergence in context.
            i = next((n for n in range(min(len(sb), len(sa))) if sb[n] != sa[n]),
                     min(len(sb), len(sa)))
            sb, sa = "..." + sb[max(0, i - 60):i + 100], "..." + sa[max(0, i - 60):i + 100]
        print(f"  {k}\n    before: {sb}\n    after : {sa}")
    if len(diffs) > limit:
        print(f"  ... and {len(diffs) - limit} more")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true", help="record the baseline")
    ap.add_argument("--compare", action="store_true", help="diff against the baseline")
    ap.add_argument("--selftest", action="store_true",
                    help="prove two consecutive runs of unchanged code agree")
    args = ap.parse_args()

    if args.selftest:
        one, two = build(), build()
        print("Self-test (determinism gate):")
        return _report(compare(one, two))

    if args.save:
        snap = build()
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=1, sort_keys=True)
        flat = _flatten(snap)
        print(f"Baseline saved: {len(flat)} recorded values -> {SNAPSHOT}")
        return 0

    if args.compare:
        if not os.path.exists(SNAPSHOT):
            print("No baseline. Run --save first.")
            return 2
        with open(SNAPSHOT, encoding="utf-8") as f:
            old = json.load(f)
        return _report(compare(old, build()))

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
