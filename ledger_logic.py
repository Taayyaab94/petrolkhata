"""Pure helpers for computing book stock and reading history.

Stock is intentionally never stored as a mutable counter (see Tank in
models.py) - every figure here is derived fresh from ledger rows so that
editing or backfilling a past date can never leave numbers out of sync.
"""

from datetime import datetime, timedelta

from sqlalchemy import func

from extensions import db
from models import (
    BankSale,
    CashDeposit,
    CreditGiven,
    EmployeeLoan,
    Expense,
    FuelPriceHistory,
    Nozzle,
    NozzleReset,
    Receipt,
    Sale,
    StockPurchase,
    SupplierPayment,
)


def book_stock(tank, as_of_date):
    """Starting stock + purchases into this tank - sales from nozzles on
    this tank, all up to and including as_of_date."""
    purchased = (
        db.session.query(func.coalesce(func.sum(StockPurchase.liters), 0))
        .filter(StockPurchase.tank_id == tank.id, StockPurchase.entry_date <= as_of_date)
        .scalar()
    )
    sold = (
        db.session.query(func.coalesce(func.sum(Sale.liters), 0))
        .join(Nozzle, Sale.nozzle_id == Nozzle.id)
        .filter(Nozzle.tank_id == tank.id, Sale.entry_date <= as_of_date)
        .scalar()
    )
    return round(tank.starting_stock_liters + purchased - sold, 2)


def previous_reading_for(nozzle, entry_date):
    """The reading a new entry on entry_date should be measured from.

    Returns (value, is_auto). The reading history is meant to form one
    continuous chain - a date's current_reading becomes the next date's
    previous_reading - so this only auto-fills from the immediately
    preceding calendar day, not just "whatever the nearest earlier entry
    happens to be". If entry_date-1 has no Sale, is_auto is False and
    value is None, meaning the caller must ask the user to type both
    readings by hand.

    There's no setup-time baseline to fall back on - a nozzle with no
    Sale history at all behaves exactly like a gap: the very first
    reading ever logged for it always has to be entered by hand.

    A meter reset (NozzleReset) also forces a manual entry on its own
    reset_date, the same as a gap - a physically replaced/rolled-over
    meter has no continuous chain with what came before it.
    """
    reset = latest_reset_for(nozzle, entry_date)
    if reset and reset.reset_date == entry_date:
        return None, False

    prior_date = entry_date - timedelta(days=1)
    prior_sale = Sale.query.filter_by(nozzle_id=nozzle.id, entry_date=prior_date).first()
    if prior_sale:
        return prior_sale.current_reading, True

    return None, False


def latest_reset_for(nozzle, entry_date):
    """Most recent meter reset for this nozzle on or before entry_date, or
    None if it's never been reset (as of this date)."""
    return (
        NozzleReset.query.filter(
            NozzleReset.nozzle_id == nozzle.id, NozzleReset.reset_date <= entry_date
        )
        .order_by(NozzleReset.reset_date.desc(), NozzleReset.id.desc())
        .first()
    )


def nearest_earlier_reading(nozzle, entry_date):
    """Nearest known reading strictly before entry_date, regardless of any
    gap. Used only to sanity-check a manually typed previous reading -
    meter readings can't go backwards over time even across a gap. Falls
    back to 0 when nothing has ever been recorded for this nozzle, or when
    a meter reset means nothing before the reset date counts anymore."""
    reset = latest_reset_for(nozzle, entry_date)
    query = Sale.query.filter(Sale.nozzle_id == nozzle.id, Sale.entry_date < entry_date)
    if reset:
        query = query.filter(Sale.entry_date >= reset.reset_date)
    sale = query.order_by(Sale.entry_date.desc(), Sale.id.desc()).first()
    return sale.current_reading if sale else 0.0


def next_sale_on_or_after(nozzle_id, entry_date):
    """The next recorded Sale after entry_date, used to stop an edit from
    exceeding a later reading already on file. A meter reset that happens
    after entry_date breaks that comparison (the new era can legitimately
    start lower), so readings past the next reset aren't considered."""
    next_reset = (
        NozzleReset.query.filter(NozzleReset.nozzle_id == nozzle_id, NozzleReset.reset_date > entry_date)
        .order_by(NozzleReset.reset_date.asc(), NozzleReset.id.asc())
        .first()
    )
    query = Sale.query.filter(Sale.nozzle_id == nozzle_id, Sale.entry_date > entry_date)
    if next_reset:
        query = query.filter(Sale.entry_date < next_reset.reset_date)
    return query.order_by(Sale.entry_date.asc(), Sale.id.asc()).first()


def price_on_date(fuel_type, entry_date):
    """The price per liter actually in effect on entry_date, from
    FuelPriceHistory - not necessarily today's FuelType.price_per_liter.
    Falls back to the current price if no history row applies (e.g. a
    fuel type that predates price history being tracked)."""
    row = (
        FuelPriceHistory.query.filter(
            FuelPriceHistory.fuel_type_id == fuel_type.id,
            FuelPriceHistory.effective_date <= entry_date,
        )
        .order_by(FuelPriceHistory.effective_date.desc(), FuelPriceHistory.id.desc())
        .first()
    )
    return row.price_per_liter if row else fuel_type.price_per_liter


def record_fuel_price(fuel_type, price, effective_date):
    """Log a price change effective as of effective_date, and keep
    FuelType.price_per_liter (the "current price" cache read everywhere
    that just wants today's price) pointing at whichever history row is
    latest as of today - so a same-day change becomes today's price, and
    a correction to an older date doesn't make today's price stale."""
    db.session.add(
        FuelPriceHistory(fuel_type_id=fuel_type.id, price_per_liter=price, effective_date=effective_date)
    )
    db.session.flush()
    fuel_type.price_per_liter = price_on_date(fuel_type, datetime.now().date())


def stock_series(tank, dates):
    """Book stock for `tank` at the end of each date in `dates` (ascending,
    contiguous-ish is fine). Computed as one running total instead of one
    query per day."""
    if not dates:
        return []
    start = dates[0]
    running = tank.starting_stock_liters + (
        db.session.query(func.coalesce(func.sum(StockPurchase.liters), 0))
        .filter(StockPurchase.tank_id == tank.id, StockPurchase.entry_date < start)
        .scalar()
    ) - (
        db.session.query(func.coalesce(func.sum(Sale.liters), 0))
        .join(Nozzle, Sale.nozzle_id == Nozzle.id)
        .filter(Nozzle.tank_id == tank.id, Sale.entry_date < start)
        .scalar()
    )

    purchases_by_day = dict(
        db.session.query(StockPurchase.entry_date, func.sum(StockPurchase.liters))
        .filter(
            StockPurchase.tank_id == tank.id,
            StockPurchase.entry_date >= start,
            StockPurchase.entry_date <= dates[-1],
        )
        .group_by(StockPurchase.entry_date)
        .all()
    )
    sales_by_day = dict(
        db.session.query(Sale.entry_date, func.sum(Sale.liters))
        .join(Nozzle, Sale.nozzle_id == Nozzle.id)
        .filter(
            Nozzle.tank_id == tank.id,
            Sale.entry_date >= start,
            Sale.entry_date <= dates[-1],
        )
        .group_by(Sale.entry_date)
        .all()
    )

    series = []
    for d in dates:
        running += purchases_by_day.get(d, 0) - sales_by_day.get(d, 0)
        series.append(round(running, 2))
    return series


def sales_breakdown_for_date(entry_date):
    """Total nozzle sales for entry_date, split by how they were
    collected: credit (owed by a customer), bank (reconciled to a bank
    account), and cash (whatever's left over)."""
    total = (
        db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
        .filter(Sale.entry_date == entry_date)
        .scalar()
    )
    credit = (
        db.session.query(func.coalesce(func.sum(CreditGiven.amount), 0))
        .filter(CreditGiven.entry_date == entry_date)
        .scalar()
    )
    bank = (
        db.session.query(func.coalesce(func.sum(BankSale.amount), 0))
        .filter(BankSale.entry_date == entry_date)
        .scalar()
    )
    cash = round(total - credit - bank, 2)
    return {"total": total, "credit": credit, "bank": bank, "cash": cash}


def fuel_sales_for_date(entry_date):
    """Liters sold and revenue for entry_date, grouped by fuel type name -
    computed from nozzle meter reading differences (Sale rows), the same
    figures the sales stat cards are built from."""
    sales = Sale.query.filter_by(entry_date=entry_date).join(Nozzle).all()
    by_fuel = {}
    for s in sales:
        d = by_fuel.setdefault(s.nozzle.tank.fuel_type.name, {"liters": 0.0, "revenue": 0.0})
        d["liters"] += s.liters
        d["revenue"] += s.total_amount
    return by_fuel


def account_ledger_events(account):
    """Full transaction history for one account (opening balance plus every
    entry kind that can be posted to it), each tagged with the running
    balance immediately after it, most recent first.

    The running balance is computed by walking the events in ascending
    date order and accumulating - never stored - so editing any entry's
    amount or date (including the opening balance) and reloading this
    account always shows every later entry's running balance correctly
    rippled forward, the same way book_stock() and every other balance in
    this app is always derived fresh rather than cached.
    """
    events = []
    if account.opening_balance:
        opening_date = account.opening_balance_date or account.created_at.date()
        events.append(
            {
                "kind": "opening",
                "entry_date": opening_date,
                "sort_key": (opening_date, datetime.min),
                "obj": None,
                "delta": account.opening_balance,
            }
        )
    for c in account.credit_entries:
        events.append(
            {"kind": "credit", "entry_date": c.entry_date, "sort_key": (c.entry_date, c.recorded_at), "obj": c, "delta": c.amount}
        )
    for r in account.receipts:
        events.append(
            {"kind": "receipt", "entry_date": r.entry_date, "sort_key": (r.entry_date, r.recorded_at), "obj": r, "delta": -r.amount}
        )
    for pu in account.stock_purchases:
        if pu.payment_type == "credit":
            events.append(
                {"kind": "purchase", "entry_date": pu.entry_date, "sort_key": (pu.entry_date, pu.recorded_at), "obj": pu, "delta": -(pu.cost or 0)}
            )
    for sp in account.supplier_payments:
        events.append(
            {"kind": "supplier_payment", "entry_date": sp.entry_date, "sort_key": (sp.entry_date, sp.recorded_at), "obj": sp, "delta": sp.amount}
        )
    for l in account.employee_loans:
        events.append(
            {"kind": "employee_loan", "entry_date": l.entry_date, "sort_key": (l.entry_date, l.recorded_at), "obj": l, "delta": l.amount}
        )

    events.sort(key=lambda e: e["sort_key"])
    running = 0.0
    for e in events:
        running += e["delta"]
        e["running_balance"] = round(running, 2)

    events.reverse()
    return events


def cash_account_balance(cash_account):
    """Cash-in-hand: opening balance, plus every date's cash sales (total
    sales minus credit minus bank sales) and every cash-method receipt,
    minus cash physically deposited into a bank account and every
    cash-method outflow (loans, expenses, fuel purchases, supplier
    payments - each of those can instead be routed through a specific
    bank account via "Paid via", in which case it hits that bank's
    balance instead and is excluded here)."""
    total_sales = db.session.query(func.coalesce(func.sum(Sale.total_amount), 0)).scalar()
    total_credit = db.session.query(func.coalesce(func.sum(CreditGiven.amount), 0)).scalar()
    total_bank_sales = db.session.query(func.coalesce(func.sum(BankSale.amount), 0)).scalar()
    total_deposits = db.session.query(func.coalesce(func.sum(CashDeposit.amount), 0)).scalar()
    total_cash_receipts = (
        db.session.query(func.coalesce(func.sum(Receipt.amount), 0))
        .filter(Receipt.method == "cash")
        .scalar()
    )
    total_cash_loans = (
        db.session.query(func.coalesce(func.sum(EmployeeLoan.amount), 0))
        .filter(EmployeeLoan.method == "cash")
        .scalar()
    )
    total_cash_expenses = (
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.method == "cash")
        .scalar()
    )
    total_cash_purchases = (
        db.session.query(func.coalesce(func.sum(StockPurchase.cost), 0))
        .filter(StockPurchase.payment_type == "cash", StockPurchase.method == "cash")
        .scalar()
    )
    total_supplier_payments = (
        db.session.query(func.coalesce(func.sum(SupplierPayment.amount), 0))
        .filter(SupplierPayment.method == "cash")
        .scalar()
    )
    return round(
        cash_account.opening_balance
        + total_sales
        - total_credit
        - total_bank_sales
        - total_deposits
        + total_cash_receipts
        - total_cash_loans
        - total_cash_expenses
        - total_cash_purchases
        - total_supplier_payments,
        2,
    )


def cash_account_ledger_events(cash_account):
    """Full transaction history for cash-in-hand, most recent first, same
    always-recompute-fresh running balance as account_ledger_events() and
    bank_account_ledger_events(). Unlike an account or a bank account,
    cash-in-hand's biggest contributor - the cash portion of nozzle sales
    - isn't a single discrete entry (it's total sales minus credit minus
    bank sales, on a given date), so that shows as one read-only row per
    date rather than per-Sale; everything else here is a real entry, some
    editable directly (fuel purchases and expenses paid in cash have no
    other home) and some read-only with a link to where they're actually
    edited (receipts, loans, and supplier payments belong to an account;
    deposits belong to a bank account)."""
    events = []
    if cash_account.opening_balance:
        opening_date = cash_account.opening_balance_date or cash_account.created_at.date()
        events.append(
            {
                "kind": "opening",
                "entry_date": opening_date,
                "sort_key": (opening_date, datetime.min),
                "obj": None,
                "delta": cash_account.opening_balance,
            }
        )

    sale_dates = [row[0] for row in db.session.query(Sale.entry_date).distinct().all()]
    for d in sale_dates:
        breakdown = sales_breakdown_for_date(d)
        if breakdown["cash"]:
            events.append(
                {
                    "kind": "cash_sales",
                    "entry_date": d,
                    "sort_key": (d, datetime.min.replace(minute=1)),
                    "obj": None,
                    "delta": breakdown["cash"],
                }
            )

    for r in Receipt.query.filter_by(method="cash").all():
        events.append(
            {"kind": "receipt", "entry_date": r.entry_date, "sort_key": (r.entry_date, r.recorded_at), "obj": r, "delta": r.amount}
        )
    for l in EmployeeLoan.query.filter_by(method="cash").all():
        events.append(
            {"kind": "employee_loan", "entry_date": l.entry_date, "sort_key": (l.entry_date, l.recorded_at), "obj": l, "delta": -l.amount}
        )
    for e in Expense.query.filter_by(method="cash").all():
        events.append(
            {"kind": "expense", "entry_date": e.entry_date, "sort_key": (e.entry_date, e.recorded_at), "obj": e, "delta": -e.amount}
        )
    for pu in StockPurchase.query.filter_by(payment_type="cash", method="cash").all():
        events.append(
            {"kind": "fuel_purchase", "entry_date": pu.entry_date, "sort_key": (pu.entry_date, pu.recorded_at), "obj": pu, "delta": -(pu.cost or 0)}
        )
    for sp in SupplierPayment.query.filter_by(method="cash").all():
        events.append(
            {"kind": "supplier_payment", "entry_date": sp.entry_date, "sort_key": (sp.entry_date, sp.recorded_at), "obj": sp, "delta": -sp.amount}
        )
    for cd in CashDeposit.query.all():
        events.append(
            {"kind": "deposit", "entry_date": cd.entry_date, "sort_key": (cd.entry_date, cd.recorded_at), "obj": cd, "delta": -cd.amount}
        )

    events.sort(key=lambda e: e["sort_key"])
    running = 0.0
    for e in events:
        running += e["delta"]
        e["running_balance"] = round(running, 2)

    events.reverse()
    return events


def bank_account_ledger_events(bank_account):
    """Full transaction history for one bank account (opening balance plus
    every entry kind that can be routed to it), each tagged with the
    running balance immediately after it, most recent first. Same
    always-recompute-fresh approach as account_ledger_events()."""
    events = []
    if bank_account.opening_balance:
        opening_date = bank_account.opening_balance_date or bank_account.created_at.date()
        events.append(
            {
                "kind": "opening",
                "entry_date": opening_date,
                "sort_key": (opening_date, datetime.min),
                "obj": None,
                "delta": bank_account.opening_balance,
            }
        )
    for s in bank_account.bank_sales:
        events.append(
            {"kind": "bank_sale", "entry_date": s.entry_date, "sort_key": (s.entry_date, s.recorded_at), "obj": s, "delta": s.amount}
        )
    for d in bank_account.deposits:
        events.append(
            {"kind": "deposit", "entry_date": d.entry_date, "sort_key": (d.entry_date, d.recorded_at), "obj": d, "delta": d.amount}
        )
    for r in bank_account.receipts:
        events.append(
            {"kind": "receipt", "entry_date": r.entry_date, "sort_key": (r.entry_date, r.recorded_at), "obj": r, "delta": r.amount}
        )
    for l in bank_account.employee_loans_paid:
        events.append(
            {"kind": "employee_loan", "entry_date": l.entry_date, "sort_key": (l.entry_date, l.recorded_at), "obj": l, "delta": -l.amount}
        )
    for e in bank_account.expenses:
        events.append(
            {"kind": "expense", "entry_date": e.entry_date, "sort_key": (e.entry_date, e.recorded_at), "obj": e, "delta": -e.amount}
        )
    for pu in bank_account.fuel_purchases:
        if pu.payment_type == "cash":
            events.append(
                {"kind": "fuel_purchase", "entry_date": pu.entry_date, "sort_key": (pu.entry_date, pu.recorded_at), "obj": pu, "delta": -(pu.cost or 0)}
            )
    for sp in bank_account.supplier_payments_paid:
        events.append(
            {"kind": "supplier_payment", "entry_date": sp.entry_date, "sort_key": (sp.entry_date, sp.recorded_at), "obj": sp, "delta": -sp.amount}
        )

    events.sort(key=lambda e: e["sort_key"])
    running = 0.0
    for e in events:
        running += e["delta"]
        e["running_balance"] = round(running, 2)

    events.reverse()
    return events
