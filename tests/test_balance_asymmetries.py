"""Pins two REAL, pre-existing disagreements between the two balance
implementations, so a later "cleanup" cannot silently change what the
app reports.

Neither of these is asserted to be correct. They are asserted to be
UNCHANGED. Both were found while consolidating the term tables into
balance_terms.py, and both were deliberately preserved rather than fixed,
because fixing a bug inside a behaviour-preserving refactor makes the
refactor unreviewable and the fix unbisectable.

If you decide to fix either one, do it as its own change, update this
file in the same commit, and say so in the message - that is exactly the
signal this file exists to produce.

    python tests/test_balance_asymmetries.py
"""
import os
import sys
import tempfile
from datetime import date, datetime

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "asym.db").replace("\\", "/")
os.environ["SKIP_DB_BOOTSTRAP"] = "1"
os.environ["SECRET_KEY"] = "asym-key"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A                     # noqa: E402
import ledger_logic as L            # noqa: E402
from extensions import db           # noqa: E402
from models import (                # noqa: E402
    Account, BankAccount, FuelType, Pump, SalaryPayment, SalesReturn, Shift,
    Tank, User,
)
from tenancy import unscoped        # noqa: E402

results = []


def check(cond, msg):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + msg)


def _fixture():
    p = Pump(name="Asym")
    db.session.add(p)
    db.session.flush()
    u = User(pump_id=p.id, username="o", email="asym@test.invalid", role="owner")
    u.set_password("asymmetrypassword1")
    sh = Shift(pump_id=p.id, name="S", sort_order=0)
    ft = FuelType(pump_id=p.id, name="P", price_per_liter=100.0,
                  entry_mode="meter", direct_entry_combined=False)
    db.session.add_all([u, sh, ft])
    db.session.flush()
    tk = Tank(pump_id=p.id, number=1, fuel_type_id=ft.id, capacity_liters=100,
              starting_stock_liters=0, low_stock_threshold=0)
    bk = BankAccount(pump_id=p.id, name="B", opening_balance=1000.0,
                     opening_balance_date=date(2026, 6, 1),
                     created_at=datetime(2026, 5, 1))
    emp = Account(pump_id=p.id, name="Emp", account_type="employee",
                  opening_balance=0, opening_balance_date=date(2026, 6, 1))
    db.session.add_all([tk, bk, emp])
    db.session.flush()
    return p, u, sh, ft, tk, bk, emp


with A.app.app_context():
    db.drop_all()
    db.create_all()
    with unscoped():
        p, u, sh, ft, tk, bk, emp = _fixture()

        # ASYMMETRY 1 - the sales-return method filter.
        # BankAccount.balance sums the sales_returns backref with NO
        # method filter; bank_account_balance_as_of()'s SQL carries
        # `SalesReturn.method == "bank"`. Both were written assuming
        # bank_account_id is only ever set for method == "bank". This row
        # breaks that assumption, and the two then disagree by the full
        # amount of the return.
        db.session.add(SalesReturn(
            pump_id=p.id, user_id=u.id, entry_date=date(2026, 6, 2), shift_id=sh.id,
            fuel_type_id=ft.id, tank_id=tk.id, liters=1.0, price_per_liter=500.0,
            amount=500.0, method="cash", bank_account_id=bk.id,
            recorded_at=datetime(2026, 6, 1, 0, 0, 1)))
        db.session.commit()

        check(bk.balance == 500.0,
              "BankAccount.balance DEDUCTS a non-bank-method sales return (1000 - 500)")
        check(L.bank_account_balance_as_of(bk, date(2026, 6, 30)) == 1000.0,
              "bank_account_balance_as_of IGNORES the same row (filters method == 'bank')")
        check(bk.balance != L.bank_account_balance_as_of(bk, date(2026, 6, 30)),
              "...so the two disagree by 500 on this row - pinned, not endorsed")

        # ASYMMETRY 2 - per-row rounding of salary net pay.
        # SalaryPayment.net_paid rounds (gross - deduction) to 2dp PER
        # ROW; the SQL side sums the raw difference and rounds once at the
        # end. With enough rows whose difference has a third decimal, the
        # two diverge.
        db.session.query(SalesReturn).delete()
        for i in range(8):
            db.session.add(SalaryPayment(
                pump_id=p.id, user_id=u.id, account_id=emp.id,
                entry_date=date(2026, 6, 3), gross_amount=10.005, deduction_amount=0.0,
                method="bank", bank_account_id=bk.id,
                recorded_at=datetime(2026, 6, 1, 0, 1, i)))
        db.session.commit()

        py = bk.balance
        sql = L.bank_account_balance_as_of(bk, date(2026, 6, 30))
        check(py == 919.92, f"BankAccount.balance rounds net_paid PER ROW -> {py} (expected 919.92)")
        check(sql == 919.96, f"bank_account_balance_as_of rounds ONCE at the end -> {sql} (expected 919.96)")
        check(py != sql,
              "...so 8 salary rows of 10.005 drift the two apart by 4 paisa - pinned, not endorsed")

print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
