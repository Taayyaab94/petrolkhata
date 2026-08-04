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
    CashAccount,
    CashDeposit,
    CashHandover,
    CreditGiven,
    EmployeeLoan,
    Expense,
    FuelPriceHistory,
    FuelType,
    Nozzle,
    NozzleReset,
    Receipt,
    Sale,
    SalaryPayment,
    Shift,
    StockPurchase,
    SupplierPayment,
    Tank,
    TankDipChart,
)


def book_stock(tank, as_of_date):
    """Book stock for `tank` at the END of as_of_date - starting stock
    plus every purchase into this tank, minus every sale from a nozzle on
    this tank.

    tank.starting_stock_liters is the level at the START of
    tank.starting_stock_date (equivalently, the END of the day before it -
    see Tank in models.py). That splits into three cases:

    - starting_stock_date is None: back-compat for every tank that
      existed before this column did. Treat the baseline as sitting
      before all recorded history and sum every purchase/sale up to and
      including as_of_date - exactly the original, only-ever-had-one-mode
      behaviour.
    - as_of_date >= starting_stock_date (FORWARD): sum purchases/sales
      from starting_stock_date through as_of_date, inclusive on both
      ends.
    - as_of_date < starting_stock_date (BACKWARD): there's no ledger
      history before the baseline to sum forward from, so instead undo
      everything strictly between the two dates - subtract back out each
      purchase, add back each sale. Stock only ever moves via those two
      kinds of entry, so running the ledger backwards from the baseline
      is valid arithmetic. This is what makes "measure today's stock,
      then backfill months of older records" come out correct instead of
      subtracting sales that today's baseline already reflects.
    """
    if tank.starting_stock_date is None:
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

    if as_of_date >= tank.starting_stock_date:
        purchased = (
            db.session.query(func.coalesce(func.sum(StockPurchase.liters), 0))
            .filter(
                StockPurchase.tank_id == tank.id,
                StockPurchase.entry_date >= tank.starting_stock_date,
                StockPurchase.entry_date <= as_of_date,
            )
            .scalar()
        )
        sold = (
            db.session.query(func.coalesce(func.sum(Sale.liters), 0))
            .join(Nozzle, Sale.nozzle_id == Nozzle.id)
            .filter(
                Nozzle.tank_id == tank.id,
                Sale.entry_date >= tank.starting_stock_date,
                Sale.entry_date <= as_of_date,
            )
            .scalar()
        )
        return round(tank.starting_stock_liters + purchased - sold, 2)

    # BACKWARD: as_of_date < starting_stock_date - undo the entries
    # strictly between the two dates instead of summing forward.
    purchased = (
        db.session.query(func.coalesce(func.sum(StockPurchase.liters), 0))
        .filter(
            StockPurchase.tank_id == tank.id,
            StockPurchase.entry_date > as_of_date,
            StockPurchase.entry_date < tank.starting_stock_date,
        )
        .scalar()
    )
    sold = (
        db.session.query(func.coalesce(func.sum(Sale.liters), 0))
        .join(Nozzle, Sale.nozzle_id == Nozzle.id)
        .filter(
            Nozzle.tank_id == tank.id,
            Sale.entry_date > as_of_date,
            Sale.entry_date < tank.starting_stock_date,
        )
        .scalar()
    )
    return round(tank.starting_stock_liters - purchased + sold, 2)


def previous_slot(entry_date, shift):
    """The (date, shift) immediately before this one in reading order.

    A nozzle's meter runs continuously through the day, so with several
    shifts configured the chain threads through them in sort_order -
    Morning's closing reading is Evening's opening - and only rolls to the
    previous calendar day when we're looking at the day's first shift."""
    shifts = active_shifts()
    if not shifts:
        return entry_date - timedelta(days=1), None
    ids = [s.id for s in shifts]
    idx = ids.index(shift.id) if shift and shift.id in ids else 0
    if idx > 0:
        return entry_date, shifts[idx - 1]
    return entry_date - timedelta(days=1), shifts[-1]


def previous_reading_for(nozzle, entry_date, shift=None):
    """The reading a new entry on (entry_date, shift) should be measured from.

    Returns (value, is_auto). The reading history is meant to form one
    continuous chain - each slot's current_reading becomes the next slot's
    previous_reading - so this only auto-fills from the immediately
    preceding slot (see previous_slot), not just "whatever the nearest
    earlier entry happens to be". If that slot has no Sale, is_auto is
    False and value is None, meaning the caller must ask the user to type
    both readings by hand.

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

    if shift is None:
        shift = default_shift()
    prior_date, prior_shift = previous_slot(entry_date, shift)
    query = Sale.query.filter_by(nozzle_id=nozzle.id, entry_date=prior_date)
    if prior_shift is not None:
        query = query.filter_by(shift_id=prior_shift.id)
    prior_sale = query.first()
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


def nearest_earlier_reading(nozzle, entry_date, shift=None):
    """Nearest known reading strictly before this (date, shift) slot,
    regardless of any gap. Used only to sanity-check a manually typed
    previous reading - meter readings can't go backwards over time even
    across a gap. Falls back to 0 when nothing has ever been recorded for
    this nozzle, or when a meter reset means nothing before the reset date
    counts anymore."""
    reset = latest_reset_for(nozzle, entry_date)
    order = shift.sort_order if shift else 0
    query = (
        Sale.query.join(Shift, Sale.shift_id == Shift.id)
        .filter(
            Sale.nozzle_id == nozzle.id,
            db.or_(
                Sale.entry_date < entry_date,
                db.and_(Sale.entry_date == entry_date, Shift.sort_order < order),
            ),
        )
    )
    if reset:
        query = query.filter(Sale.entry_date >= reset.reset_date)
    sale = query.order_by(Sale.entry_date.desc(), Shift.sort_order.desc(), Sale.id.desc()).first()
    return sale.current_reading if sale else 0.0


def next_sale_on_or_after(nozzle_id, entry_date, shift=None):
    """The next recorded Sale after this (date, shift) slot, used to stop an
    edit from exceeding a later reading already on file. A meter reset that
    happens after entry_date breaks that comparison (the new era can
    legitimately start lower), so readings past the next reset aren't
    considered."""
    next_reset = (
        NozzleReset.query.filter(NozzleReset.nozzle_id == nozzle_id, NozzleReset.reset_date > entry_date)
        .order_by(NozzleReset.reset_date.asc(), NozzleReset.id.asc())
        .first()
    )
    order = shift.sort_order if shift else 0
    query = (
        Sale.query.join(Shift, Sale.shift_id == Shift.id)
        .filter(
            Sale.nozzle_id == nozzle_id,
            db.or_(
                Sale.entry_date > entry_date,
                db.and_(Sale.entry_date == entry_date, Shift.sort_order > order),
            ),
        )
    )
    if next_reset:
        query = query.filter(Sale.entry_date < next_reset.reset_date)
    return query.order_by(Sale.entry_date.asc(), Shift.sort_order.asc(), Sale.id.asc()).first()


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
    query per day.

    Seeded with book_stock() at the end of the day before the window
    starts, rather than re-deriving that figure here - book_stock() has
    already done the forward/backward work of anchoring it to
    starting_stock_date, so this only has to agree with it once at the
    seed; walking forward by each day's net purchases-minus-sales change
    is the same arithmetic on either side of that date."""
    if not dates:
        return []
    start = dates[0]
    running = book_stock(tank, start - timedelta(days=1))

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


def sales_breakdown_for_date(entry_date, shift_id=None):
    """Total nozzle sales for entry_date, split by how they were
    collected: credit (owed by a customer), bank (reconciled to a bank
    account), and cash (whatever's left over).

    Passing shift_id narrows every component to that one shift, which is
    what makes a per-shift cash reconciliation possible - summing the
    breakdowns of every shift on a date gives exactly the whole-date
    breakdown, since all three tables carry the same shift_id."""
    sale_q = db.session.query(func.coalesce(func.sum(Sale.total_amount), 0)).filter(
        Sale.entry_date == entry_date
    )
    credit_q = db.session.query(func.coalesce(func.sum(CreditGiven.amount), 0)).filter(
        CreditGiven.entry_date == entry_date
    )
    bank_q = db.session.query(func.coalesce(func.sum(BankSale.amount), 0)).filter(
        BankSale.entry_date == entry_date
    )
    if shift_id is not None:
        sale_q = sale_q.filter(Sale.shift_id == shift_id)
        credit_q = credit_q.filter(CreditGiven.shift_id == shift_id)
        bank_q = bank_q.filter(BankSale.shift_id == shift_id)

    total, credit, bank = sale_q.scalar(), credit_q.scalar(), bank_q.scalar()
    cash = round(total - credit - bank, 2)
    return {"total": total, "credit": credit, "bank": bank, "cash": cash}


def default_shift():
    """The shift new entries fall into when the pump hasn't set up its own -
    lowest sort_order among active shifts. There's always at least one
    (seeded at startup), so this never returns None in practice."""
    return Shift.query.filter_by(is_active=True).order_by(Shift.sort_order, Shift.id).first()


def active_shifts():
    return Shift.query.filter_by(is_active=True).order_by(Shift.sort_order, Shift.id).all()


def handover_rows_for_date(entry_date):
    """Per-shift cash reconciliation for one date: what the ledger says
    should have been collected in cash for each shift, what was actually
    declared as counted, and the variance between them. Shifts with no
    declared handover yet still appear (declared/variance None) so an
    unreconciled shift is visible rather than silently absent."""
    rows = []
    for shift in active_shifts():
        breakdown = sales_breakdown_for_date(entry_date, shift_id=shift.id)
        handover = CashHandover.query.filter_by(entry_date=entry_date, shift_id=shift.id).first()
        expected = breakdown["cash"]
        declared = handover.declared_amount if handover else None
        rows.append(
            {
                "shift": shift,
                "expected": expected,
                "declared": declared,
                "variance": round(declared - expected, 2) if handover else None,
                "handover": handover,
                "sales_total": breakdown["total"],
                "credit_total": breakdown["credit"],
                "bank_total": breakdown["bank"],
            }
        )
    return rows


def attendant_variance_summary(start, end):
    """Total cash variance per attendant across a date range, worst
    (most negative) first - so a pattern of repeat shortfalls stands out
    rather than being buried one shift at a time."""
    summary = {}
    handovers = CashHandover.query.filter(
        CashHandover.entry_date >= start, CashHandover.entry_date <= end
    ).all()
    for h in handovers:
        expected = sales_breakdown_for_date(h.entry_date, shift_id=h.shift_id)["cash"]
        variance = round(h.declared_amount - expected, 2)
        name = h.attendant.name if h.attendant else "Unassigned"
        row = summary.setdefault(
            name, {"name": name, "account": h.attendant, "shifts": 0, "total_variance": 0.0, "shortfalls": 0}
        )
        row["shifts"] += 1
        row["total_variance"] = round(row["total_variance"] + variance, 2)
        if variance < -0.01:
            row["shortfalls"] += 1
    return sorted(summary.values(), key=lambda r: r["total_variance"])


def liters_from_dip_cm(tank, depth_cm):
    """Convert a dip-stick depth to liters using this tank's calibration
    chart, linearly interpolating between the two nearest rows. Returns
    None when the tank has no chart, which is the caller's signal to fall
    back to accepting liters directly."""
    rows = (
        TankDipChart.query.filter_by(tank_id=tank.id).order_by(TankDipChart.depth_cm).all()
    )
    if not rows:
        return None
    if depth_cm <= rows[0].depth_cm:
        return round(rows[0].liters if depth_cm == rows[0].depth_cm else 0.0, 2)
    if depth_cm >= rows[-1].depth_cm:
        return round(rows[-1].liters, 2)
    for lower, upper in zip(rows, rows[1:]):
        if lower.depth_cm <= depth_cm <= upper.depth_cm:
            span = upper.depth_cm - lower.depth_cm
            if span <= 0:
                return round(lower.liters, 2)
            ratio = (depth_cm - lower.depth_cm) / span
            return round(lower.liters + ratio * (upper.liters - lower.liters), 2)
    return round(rows[-1].liters, 2)


def weighted_avg_cost(fuel_type, as_of_date):
    """Average cost per liter actually paid for this fuel, across every
    purchase up to as_of_date. Used to value fuel *sold* (COGS) rather
    than fuel *bought*, so a big delivery doesn't crater one day's profit
    while the stock is still sitting in the tank.

    A moving average over all history is a deliberate simplification over
    strict FIFO/LIFO lot tracking - it's stable, explainable, and doesn't
    require tracking which specific delivery each liter came from."""
    row = (
        db.session.query(
            func.coalesce(func.sum(StockPurchase.cost), 0),
            func.coalesce(func.sum(StockPurchase.liters), 0),
        )
        .join(Tank, StockPurchase.tank_id == Tank.id)
        .filter(Tank.fuel_type_id == fuel_type.id, StockPurchase.entry_date <= as_of_date)
        .first()
    )
    total_cost, total_liters = row[0] or 0, row[1] or 0
    if not total_liters:
        return 0.0
    return round(total_cost / total_liters, 4)


def cogs_for_period(start, end):
    """Cost of the fuel actually sold between start and end: for each fuel
    type, liters sold x that fuel's weighted average purchase cost as of
    the end of the period. Returns (total_cost, per_fuel_detail)."""
    detail = []
    total = 0.0
    for ft in FuelType.query.order_by(FuelType.name).all():
        liters = (
            db.session.query(func.coalesce(func.sum(Sale.liters), 0))
            .join(Nozzle, Sale.nozzle_id == Nozzle.id)
            .join(Tank, Nozzle.tank_id == Tank.id)
            .filter(Tank.fuel_type_id == ft.id, Sale.entry_date >= start, Sale.entry_date <= end)
            .scalar()
        )
        revenue = (
            db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
            .join(Nozzle, Sale.nozzle_id == Nozzle.id)
            .join(Tank, Nozzle.tank_id == Tank.id)
            .filter(Tank.fuel_type_id == ft.id, Sale.entry_date >= start, Sale.entry_date <= end)
            .scalar()
        )
        unit_cost = weighted_avg_cost(ft, end)
        cost = round(liters * unit_cost, 2)
        total += cost
        if liters:
            detail.append(
                {
                    "fuel": ft.name,
                    "liters": round(liters, 2),
                    "revenue": round(revenue, 2),
                    "unit_cost": unit_cost,
                    "cost": cost,
                    "margin": round(revenue - cost, 2),
                }
            )
    return round(total, 2), detail


def credit_aging(account, as_of_date):
    """Age the account's outstanding debit balance into 0-30 / 31-60 /
    61-90 / 90+ day buckets.

    Receipts are applied FIFO against the oldest unsettled debit entries
    (the way a shopkeeper actually clears a khata), so what's left is the
    genuinely oldest money still owed. Only meaningful for a positive
    (owed-to-us) balance; a settled or creditor account ages to nothing."""
    debits = []
    if account.opening_balance > 0:
        opening_date = account.opening_balance_date or account.created_at.date()
        debits.append({"date": opening_date, "amount": account.opening_balance})
    for c in account.credit_entries:
        debits.append({"date": c.entry_date, "amount": c.amount})
    for l in account.employee_loans:
        debits.append({"date": l.entry_date, "amount": l.amount})
    for sp in account.supplier_payments:
        debits.append({"date": sp.entry_date, "amount": sp.amount})
    debits.sort(key=lambda d: d["date"])

    credits_total = (
        sum(r.amount for r in account.receipts)
        + sum((p.cost or 0) for p in account.stock_purchases if p.payment_type == "credit")
        + sum(s.deduction_amount for s in account.salary_payments)
        + (-account.opening_balance if account.opening_balance < 0 else 0)
    )

    # Clear the oldest debits first with everything that's come in.
    remaining = credits_total
    for d in debits:
        if remaining <= 0:
            break
        applied = min(remaining, d["amount"])
        d["amount"] = round(d["amount"] - applied, 2)
        remaining = round(remaining - applied, 2)

    buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    oldest_date = None
    for d in debits:
        if d["amount"] <= 0.01:
            continue
        if oldest_date is None:
            oldest_date = d["date"]
        age = (as_of_date - d["date"]).days
        if age <= 30:
            buckets["0-30"] += d["amount"]
        elif age <= 60:
            buckets["31-60"] += d["amount"]
        elif age <= 90:
            buckets["61-90"] += d["amount"]
        else:
            buckets["90+"] += d["amount"]
    buckets = {k: round(v, 2) for k, v in buckets.items()}
    return {
        "buckets": buckets,
        "outstanding": round(sum(buckets.values()), 2),
        "oldest_date": oldest_date,
        "oldest_days": (as_of_date - oldest_date).days if oldest_date else None,
    }


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
    for s in account.salary_payments:
        # Only the deducted slice changes what this account owes; the net
        # handed over is pay, tracked against cash/bank instead.
        events.append(
            {"kind": "salary", "entry_date": s.entry_date, "sort_key": (s.entry_date, s.recorded_at), "obj": s, "delta": -s.deduction_amount}
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
    # Only the net handed over leaves the register - the deducted portion of
    # a salary settles an advance and never moves as cash.
    total_cash_salaries = (
        db.session.query(
            func.coalesce(func.sum(SalaryPayment.gross_amount - SalaryPayment.deduction_amount), 0)
        )
        .filter(SalaryPayment.method == "cash")
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
        - total_supplier_payments
        - total_cash_salaries,
        2,
    )


def _cash_daily_net_changes():
    """Net cash-in-hand change contributed by each individual calendar
    date, from exactly the same components cash_account_balance() sums
    over all history - just grouped per entry_date (one SQL query per
    component) instead of collapsed into a single total, so a date-aware
    running balance can be walked day by day.

    The opening balance is anchored on its own opening_balance_date (or
    created_at.date() if unset), same as cash_account_balance() and
    cash_account_ledger_events()."""
    changes = {}

    def add(rows, sign=1):
        for entry_date, amount in rows:
            if entry_date is None or not amount:
                continue
            changes[entry_date] = changes.get(entry_date, 0.0) + sign * amount

    cash_account = CashAccount.query.first()
    if cash_account and cash_account.opening_balance:
        opening_date = cash_account.opening_balance_date or cash_account.created_at.date()
        add([(opening_date, cash_account.opening_balance)])

    # Cash portion of nozzle sales for a date is total sales minus credit
    # minus bank sales on that same date - mirrors sales_breakdown_for_date,
    # just summed per date across all of history in one grouped query each
    # rather than one query per date.
    sales_by_date = dict(
        db.session.query(Sale.entry_date, func.sum(Sale.total_amount)).group_by(Sale.entry_date).all()
    )
    credit_by_date = dict(
        db.session.query(CreditGiven.entry_date, func.sum(CreditGiven.amount))
        .group_by(CreditGiven.entry_date)
        .all()
    )
    bank_sales_by_date = dict(
        db.session.query(BankSale.entry_date, func.sum(BankSale.amount)).group_by(BankSale.entry_date).all()
    )
    for entry_date in set(sales_by_date) | set(credit_by_date) | set(bank_sales_by_date):
        cash_amount = (
            sales_by_date.get(entry_date, 0)
            - credit_by_date.get(entry_date, 0)
            - bank_sales_by_date.get(entry_date, 0)
        )
        add([(entry_date, cash_amount)])

    add(
        db.session.query(Receipt.entry_date, func.sum(Receipt.amount))
        .filter(Receipt.method == "cash")
        .group_by(Receipt.entry_date)
        .all()
    )
    add(
        db.session.query(EmployeeLoan.entry_date, func.sum(EmployeeLoan.amount))
        .filter(EmployeeLoan.method == "cash")
        .group_by(EmployeeLoan.entry_date)
        .all(),
        sign=-1,
    )
    add(
        db.session.query(Expense.entry_date, func.sum(Expense.amount))
        .filter(Expense.method == "cash")
        .group_by(Expense.entry_date)
        .all(),
        sign=-1,
    )
    add(
        db.session.query(StockPurchase.entry_date, func.sum(StockPurchase.cost))
        .filter(StockPurchase.payment_type == "cash", StockPurchase.method == "cash")
        .group_by(StockPurchase.entry_date)
        .all(),
        sign=-1,
    )
    add(
        db.session.query(SupplierPayment.entry_date, func.sum(SupplierPayment.amount))
        .filter(SupplierPayment.method == "cash")
        .group_by(SupplierPayment.entry_date)
        .all(),
        sign=-1,
    )
    add(
        db.session.query(CashDeposit.entry_date, func.sum(CashDeposit.amount))
        .group_by(CashDeposit.entry_date)
        .all(),
        sign=-1,
    )
    add(
        db.session.query(
            SalaryPayment.entry_date, func.sum(SalaryPayment.gross_amount - SalaryPayment.deduction_amount)
        )
        .filter(SalaryPayment.method == "cash")
        .group_by(SalaryPayment.entry_date)
        .all(),
        sign=-1,
    )

    return changes


def cash_would_go_negative(hypothetical_changes):
    """True if layering hypothetical_changes on top of the real ledger
    would ever leave cash-in-hand negative at the end of some day on or
    after the earliest date touched.

    hypothetical_changes is a list of (date, delta) tuples - delta
    negative for a new outflow, positive for restoring an edited entry's
    old value before applying its new one. This has to simulate the whole
    timeline rather than just check today's total: someone backfilling
    paper records out of order can enter a February expense after March
    data already exists, and that entry might leave February's own
    balance fine while still pushing a LATER day (e.g. one after a March
    bank deposit already drew the register down further) below zero.

    The check is deliberately end-of-day: it only looks at the running
    balance after all of a day's changes are applied, never mid-day.
    That's what stops a cash expense entered before that same day's sales
    reading from false-alarming just because it was typed in first - the
    order entries are keyed in within a day never matters, only the
    day's net."""
    if not hypothetical_changes:
        return False

    changes = _cash_daily_net_changes()
    for entry_date, delta in hypothetical_changes:
        changes[entry_date] = changes.get(entry_date, 0.0) + delta

    earliest = min(entry_date for entry_date, _ in hypothetical_changes)
    running = 0.0
    for entry_date in sorted(changes):
        running += changes[entry_date]
        if entry_date >= earliest and running < -0.01:
            return True
    return False


def first_negative_cash_date():
    """The earliest date whose end-of-day cash-in-hand running balance is
    below -0.01, or None if cash never goes negative anywhere in the
    timeline. Unlike cash_would_go_negative() (which simulates a
    hypothetical change before it's applied, to decide whether to reject
    it), this reads the ledger as it actually stands right now - used
    after a delete, which is never blocked even if it leaves a later day
    negative, to name the day that needs attention. Reuses the same
    daily-net-changes walk as cash_would_go_negative()."""
    changes = _cash_daily_net_changes()
    running = 0.0
    for entry_date in sorted(changes):
        running += changes[entry_date]
        if running < -0.01:
            return entry_date
    return None


def max_cash_available_on(entry_date):
    """The most that could be spent in cash on entry_date without any day
    on or after it - given everything already on the books - ending up
    negative. This is the minimum end-of-day running balance from
    entry_date onward, which is exactly the ceiling would_overdraw_cash()
    is checking against; routes use it to word their rejection message.
    Floored at 0 for display - the guard itself is what blocks an entry
    against an already-negative position, this is only ever shown as an
    amount someone could still spend."""
    changes = _cash_daily_net_changes()
    running = 0.0
    floor = None
    for d in sorted(changes):
        running += changes[d]
        if d >= entry_date:
            floor = running if floor is None else min(floor, running)
    if floor is None:
        # Nothing on or after entry_date - the balance just holds at
        # wherever the last real change left it, forever.
        floor = running
    return max(round(floor, 2), 0.0)


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
    for s in SalaryPayment.query.filter_by(method="cash").all():
        events.append(
            {"kind": "salary", "entry_date": s.entry_date, "sort_key": (s.entry_date, s.recorded_at), "obj": s, "delta": -s.net_paid}
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
    for s in bank_account.salary_payments_paid:
        events.append(
            {"kind": "salary", "entry_date": s.entry_date, "sort_key": (s.entry_date, s.recorded_at), "obj": s, "delta": -s.net_paid}
        )

    events.sort(key=lambda e: e["sort_key"])
    running = 0.0
    for e in events:
        running += e["delta"]
        e["running_balance"] = round(running, 2)

    events.reverse()
    return events
