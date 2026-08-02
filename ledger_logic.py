"""Pure helpers for computing book stock and reading history.

Stock is intentionally never stored as a mutable counter (see Tank in
models.py) - every figure here is derived fresh from ledger rows so that
editing or backfilling a past date can never leave numbers out of sync.
"""

from datetime import datetime, timedelta

from sqlalchemy import func

from extensions import db
from models import BankSale, CashDeposit, CreditGiven, Nozzle, Sale, StockPurchase


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
    """
    prior_date = entry_date - timedelta(days=1)
    prior_sale = Sale.query.filter_by(nozzle_id=nozzle.id, entry_date=prior_date).first()
    if prior_sale:
        return prior_sale.current_reading, True

    return None, False


def nearest_earlier_reading(nozzle, entry_date):
    """Nearest known reading strictly before entry_date, regardless of any
    gap. Used only to sanity-check a manually typed previous reading -
    meter readings can't go backwards over time even across a gap. Falls
    back to 0 when nothing has ever been recorded for this nozzle."""
    sale = (
        Sale.query.filter(Sale.nozzle_id == nozzle.id, Sale.entry_date < entry_date)
        .order_by(Sale.entry_date.desc(), Sale.id.desc())
        .first()
    )
    return sale.current_reading if sale else 0.0


def next_sale_on_or_after(nozzle_id, entry_date):
    return (
        Sale.query.filter(Sale.nozzle_id == nozzle_id, Sale.entry_date > entry_date)
        .order_by(Sale.entry_date.asc(), Sale.id.asc())
        .first()
    )


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


def account_ledger_events(account):
    """Full transaction history for one account (opening balance plus all
    six entry kinds that can be posted to it), each tagged with the
    running balance immediately after it, most recent first.

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
    for p in account.customer_payments:
        events.append(
            {"kind": "customer_payment", "entry_date": p.entry_date, "sort_key": (p.entry_date, p.recorded_at), "obj": p, "delta": -p.amount}
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
    for r in account.employee_repayments:
        events.append(
            {"kind": "employee_repayment", "entry_date": r.entry_date, "sort_key": (r.entry_date, r.recorded_at), "obj": r, "delta": -r.amount}
        )

    events.sort(key=lambda e: e["sort_key"])
    running = 0.0
    for e in events:
        running += e["delta"]
        e["running_balance"] = round(running, 2)

    events.reverse()
    return events


def cash_account_balance(cash_account):
    """Cash-in-hand: opening balance, plus every date's cash sales
    (total sales minus credit minus bank sales), minus cash physically
    deposited into a bank account."""
    total_sales = db.session.query(func.coalesce(func.sum(Sale.total_amount), 0)).scalar()
    total_credit = db.session.query(func.coalesce(func.sum(CreditGiven.amount), 0)).scalar()
    total_bank_sales = db.session.query(func.coalesce(func.sum(BankSale.amount), 0)).scalar()
    total_deposits = db.session.query(func.coalesce(func.sum(CashDeposit.amount), 0)).scalar()
    return round(
        cash_account.opening_balance
        + total_sales
        - total_credit
        - total_bank_sales
        - total_deposits,
        2,
    )
