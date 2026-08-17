"""Pure helpers for computing book stock and reading history.

Stock is intentionally never stored as a mutable counter (see Tank in
models.py) - every figure here is derived fresh from ledger rows so that
editing or backfilling a past date can never leave numbers out of sync.
"""

import bisect
from datetime import datetime, timedelta

from sqlalchemy import func

from extensions import db
from models import (
    BankSale,
    CashAccount,
    CashDeposit,
    CashHandover,
    CreditGiven,
    DirectSale,
    EmployeeLoan,
    Expense,
    FuelPriceHistory,
    FuelType,
    Nozzle,
    NozzleReset,
    NozzleTesting,
    OtherIncome,
    Product,
    ProductPurchase,
    ProductRateHistory,
    ProductSale,
    Receipt,
    Sale,
    SalaryPayment,
    SalesReturn,
    Shift,
    StockPurchase,
    SupplierPayment,
    Tank,
    TankDipChart,
)


def book_stock(tank, as_of_date):
    """Book stock for `tank` at the END of as_of_date - starting stock
    plus every purchase and sales return into this tank, minus every sale
    from a nozzle on this tank, minus every DirectSale recorded directly
    against this tank (see DirectSale's docstring in models.py - the two
    never overlap for the same tank/date/shift, since a fuel type's
    entry_mode picks exactly one of them at a time, but both are summed
    unconditionally here so a tank's history across a mode switch - some
    dates metered, others direct - still adds up correctly).

    tank.starting_stock_liters is the level at the START of
    tank.starting_stock_date (equivalently, the END of the day before it -
    see Tank in models.py). That splits into three cases:

    - starting_stock_date is None: back-compat for every tank that
      existed before this column did. Treat the baseline as sitting
      before all recorded history and sum every purchase/return/sale up
      to and including as_of_date - exactly the original,
      only-ever-had-one-mode behaviour.
    - as_of_date >= starting_stock_date (FORWARD): sum purchases/returns/
      sales from starting_stock_date through as_of_date, inclusive on
      both ends.
    - as_of_date < starting_stock_date (BACKWARD): there's no ledger
      history before the baseline to sum forward from, so instead undo
      everything strictly between the two dates - subtract back out each
      purchase and return, add back each sale. Stock only ever moves via
      those kinds of entry, so running the ledger backwards from the
      baseline is valid arithmetic. This is what makes "measure today's
      stock, then backfill months of older records" come out correct
      instead of subtracting sales that today's baseline already
      reflects.

    A SalesReturn is a stock inflow just like a StockPurchase - fuel a
    customer brings back physically re-enters the tank - so it's summed
    and undone exactly the same way purchases are, with no special case.
    """
    if tank.starting_stock_date is None:
        purchased = (
            db.session.query(func.coalesce(func.sum(StockPurchase.liters), 0))
            .filter(StockPurchase.tank_id == tank.id, StockPurchase.entry_date <= as_of_date)
            .scalar()
        )
        returned = (
            db.session.query(func.coalesce(func.sum(SalesReturn.liters), 0))
            .filter(SalesReturn.tank_id == tank.id, SalesReturn.entry_date <= as_of_date)
            .scalar()
        )
        sold = (
            db.session.query(func.coalesce(func.sum(Sale.liters), 0))
            .join(Nozzle, Sale.nozzle_id == Nozzle.id)
            .filter(Nozzle.tank_id == tank.id, Sale.entry_date <= as_of_date)
            .scalar()
        )
        direct_sold = (
            db.session.query(func.coalesce(func.sum(DirectSale.liters), 0))
            .filter(DirectSale.tank_id == tank.id, DirectSale.entry_date <= as_of_date)
            .scalar()
        )
        return round(tank.starting_stock_liters + purchased + returned - sold - direct_sold, 2)

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
        returned = (
            db.session.query(func.coalesce(func.sum(SalesReturn.liters), 0))
            .filter(
                SalesReturn.tank_id == tank.id,
                SalesReturn.entry_date >= tank.starting_stock_date,
                SalesReturn.entry_date <= as_of_date,
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
        direct_sold = (
            db.session.query(func.coalesce(func.sum(DirectSale.liters), 0))
            .filter(
                DirectSale.tank_id == tank.id,
                DirectSale.entry_date >= tank.starting_stock_date,
                DirectSale.entry_date <= as_of_date,
            )
            .scalar()
        )
        return round(tank.starting_stock_liters + purchased + returned - sold - direct_sold, 2)

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
    returned = (
        db.session.query(func.coalesce(func.sum(SalesReturn.liters), 0))
        .filter(
            SalesReturn.tank_id == tank.id,
            SalesReturn.entry_date > as_of_date,
            SalesReturn.entry_date < tank.starting_stock_date,
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
    direct_sold = (
        db.session.query(func.coalesce(func.sum(DirectSale.liters), 0))
        .filter(
            DirectSale.tank_id == tank.id,
            DirectSale.entry_date > as_of_date,
            DirectSale.entry_date < tank.starting_stock_date,
        )
        .scalar()
    )
    return round(tank.starting_stock_liters - purchased - returned + sold + direct_sold, 2)


def split_combined_direct_sale(tanks, total_liters, as_of_date):
    """Split one combined "Total litres sold" figure for a multi-tank fuel
    type into a per-tank {tank_id: liters} dict, summing EXACTLY to
    total_liters.

    This is an ESTIMATE, not a measurement: there is no way to know from a
    single combined number alone how much actually came out of each
    physical tank, so the split is proportional to each tank's own
    book_stock() as of the day BEFORE as_of_date (i.e. what it held going
    into this sale) - the tank that had more fuel sitting in it absorbs
    proportionally more of the sale. Nothing here corrects for that
    estimate being wrong; the periodic tank dip (TankDip / variance) is
    what actually catches and corrects any drift this introduces over
    time, exactly the same safety net that already exists for every other
    source of stock-tracking error in this app (meter drift, spillage,
    theft, ...) - this is not a new or weaker guarantee than what
    metered tanks already rely on.

    If every tank's stock share is 0 (e.g. all empty, or none of them
    have a starting baseline yet), split evenly instead of dividing by
    zero.

    Each tank's share is rounded to 2dp; any rounding remainder (positive
    or negative) is given entirely to whichever tank got the LARGEST
    share, so the parts always sum EXACTLY to total_liters - never
    silently drop or invent a fraction of a litre. `tanks` must be
    non-empty.
    """
    if not tanks:
        return {}
    if len(tanks) == 1:
        return {tanks[0].id: round(total_liters, 2)}

    yesterday = as_of_date - timedelta(days=1)
    stocks = {t.id: max(book_stock(t, yesterday), 0.0) for t in tanks}
    total_stock = sum(stocks.values())

    if total_stock <= 0:
        # Every tank is empty (or otherwise contributes nothing) - fall
        # back to an even split rather than dividing by zero.
        shares = {t.id: total_liters / len(tanks) for t in tanks}
    else:
        shares = {tid: total_liters * (stock / total_stock) for tid, stock in stocks.items()}

    rounded = {tid: round(share, 2) for tid, share in shares.items()}
    remainder = round(total_liters - sum(rounded.values()), 2)
    if remainder:
        # Give the whole remainder to whichever tank got the largest raw
        # share (ties broken by tank id for determinism) so the parts
        # always sum to EXACTLY total_liters, to the cent.
        largest_tank_id = max(shares, key=lambda tid: (shares[tid], -tid))
        rounded[largest_tank_id] = round(rounded[largest_tank_id] + remainder, 2)

    return rounded


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


def sync_sale_testing(nozzle_id, entry_date, shift_id):
    """Reconcile the Sale at (nozzle_id, entry_date, shift_id) with its
    NozzleTesting rows - the single writer of Sale.testing_liters (see
    NozzleTesting's docstring in models.py; nothing else may assign that
    column). Every route that adds or deletes a NozzleTesting row must
    call this afterward for the slot(s) it touched.

    If no Sale exists yet for this slot, there's nothing to reconcile -
    a testing entry recorded before its meter reading just sits there
    unmatched until a reading is eventually saved for the same slot
    (ledger_readings() calls this again once it creates/updates that Sale,
    which is what folds it in). Returns (None, 0.0) in that case.

    Otherwise: sums this slot's NozzleTesting rows, then re-derives
    Sale.liters/testing_liters/total_amount from the meter's own gross
    difference (current_reading - previous_reading) minus that sum -
    exactly the split Sale's docstring in models.py describes. The sum is
    CLAMPED to the gross difference - testing can never exceed what the
    meter actually moved, or Sale.liters would go negative and produce
    negative revenue - and the amount clamped off is returned as
    `over_by` so the CALLER can surface it (this function only clamps and
    reports; it never silently swallows the discrepancy).

    Callers that just added or deleted a NozzleTesting row in the current
    session MUST db.session.flush() first, so the SUM query below sees
    it - autoflush would normally cover this, but the caller may be about
    to read `sale` (which this function returns) rather than issue
    another query, so relying on an implicit flush elsewhere isn't safe.

    Returns (sale_or_None, over_by).
    """
    sale = Sale.query.filter_by(nozzle_id=nozzle_id, entry_date=entry_date, shift_id=shift_id).first()
    if not sale:
        return None, 0.0

    testing = (
        db.session.query(func.coalesce(func.sum(NozzleTesting.liters), 0))
        .filter(
            NozzleTesting.nozzle_id == nozzle_id,
            NozzleTesting.entry_date == entry_date,
            NozzleTesting.shift_id == shift_id,
        )
        .scalar()
    )
    testing = round(testing, 2)
    gross = round(sale.current_reading - sale.previous_reading, 2)

    if testing > gross:
        over_by = round(testing - gross, 2)
        testing = gross
    else:
        over_by = 0.0

    sale.testing_liters = testing
    sale.liters = round(gross - testing, 2)
    sale.total_amount = round(sale.liters * sale.price_per_liter, 2)

    return sale, over_by


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


def reprice_entries(start, end, apply_changes=False):
    """Re-derive every date-priced entry between start and end from the
    CURRENT FuelPriceHistory, so a price recorded after the fact can be
    applied to entries that were saved before it existed.

    This is the repair path for the one thing correcting a price can't fix
    on its own: Sale/DirectSale/CreditGiven/SalesReturn each snapshot
    price_per_liter and their money figure at save time (deliberately -
    an entry must not silently change under you), so fixing the price
    history afterwards leaves those rows priced at whatever was in effect
    when they were typed. Re-saving each reading by hand does the same
    job one date at a time; this does it for a whole range at once.

    LITRES ARE NEVER TOUCHED - they come from meter readings/physical
    tank-gauge totals, not from price. Only price_per_liter and the money
    derived from it are recomputed.

    Deliberately SKIPS a CreditGiven whose amount doesn't equal
    liters * its own stored price: that mismatch is the signature of an
    amount-mode entry (see ledger_credit()), i.e. a deliberate discount
    where the amount is the number the customer actually agreed to.
    Re-pricing those would silently overwrite the discount, so they're
    reported as skipped instead and left for the owner to review by hand.

    apply_changes=False (the default) computes the changes WITHOUT writing
    anything, so a caller can show a before/after preview and let the
    owner confirm before any money figure is rewritten. Nothing here
    commits - the caller owns the transaction.

    Returns a dict of change lists; each change carries the row, its fuel,
    the old and new price, and the old and new money figure.
    """
    fuel_types = FuelType.query.all()
    resolve = price_resolver(fuel_types)

    changes = {"sales": [], "direct_sales": [], "credits": [], "returns": [], "skipped_credits": []}

    sales = (
        Sale.query.filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .join(Nozzle, Sale.nozzle_id == Nozzle.id)
        .join(Tank, Nozzle.tank_id == Tank.id)
        .all()
    )
    for s in sales:
        fuel = s.nozzle.tank.fuel_type
        new_price = resolve(fuel, s.entry_date)
        new_total = round(s.liters * new_price, 2)
        if abs(new_price - s.price_per_liter) < 0.0001 and abs(new_total - s.total_amount) < 0.01:
            continue
        changes["sales"].append({
            "obj": s, "fuel": fuel.name, "label": s.nozzle.label,
            "liters": s.liters,
            "old_price": s.price_per_liter, "new_price": new_price,
            "old_amount": s.total_amount, "new_amount": new_total,
        })
        if apply_changes:
            s.price_per_liter = new_price
            s.total_amount = new_total

    # DirectSale snapshots price_per_liter/total_amount at save time
    # exactly like Sale does (see DirectSale's docstring in models.py) -
    # so it goes stale the same way once a price is corrected after the
    # fact, and needs the same repair path. Tank-keyed already, so no
    # Nozzle join is needed to reach the fuel type.
    direct_sales = (
        DirectSale.query.filter(DirectSale.entry_date >= start, DirectSale.entry_date <= end)
        .join(Tank, DirectSale.tank_id == Tank.id)
        .all()
    )
    for ds in direct_sales:
        fuel = ds.tank.fuel_type
        new_price = resolve(fuel, ds.entry_date)
        new_total = round(ds.liters * new_price, 2)
        if abs(new_price - ds.price_per_liter) < 0.0001 and abs(new_total - ds.total_amount) < 0.01:
            continue
        changes["direct_sales"].append({
            "obj": ds, "fuel": fuel.name, "label": ds.tank.label,
            "liters": ds.liters,
            "old_price": ds.price_per_liter, "new_price": new_price,
            "old_amount": ds.total_amount, "new_amount": new_total,
        })
        if apply_changes:
            ds.price_per_liter = new_price
            ds.total_amount = new_total

    for c in CreditGiven.query.filter(CreditGiven.entry_date >= start, CreditGiven.entry_date <= end).all():
        looks_amount_mode = abs(c.amount - round(c.liters * c.price_per_liter, 2)) > 0.01
        new_price = resolve(c.fuel_type, c.entry_date)
        if looks_amount_mode:
            # Keep the agreed amount; only note it so the owner can see it
            # was left alone rather than silently re-priced.
            changes["skipped_credits"].append({
                "obj": c, "fuel": c.fuel_type.name, "label": c.account.name,
                "liters": c.liters, "old_price": c.price_per_liter,
                "new_price": new_price, "old_amount": c.amount, "new_amount": c.amount,
            })
            continue
        new_amount = round(c.liters * new_price, 2)
        if abs(new_price - c.price_per_liter) < 0.0001 and abs(new_amount - c.amount) < 0.01:
            continue
        changes["credits"].append({
            "obj": c, "fuel": c.fuel_type.name, "label": c.account.name,
            "liters": c.liters,
            "old_price": c.price_per_liter, "new_price": new_price,
            "old_amount": c.amount, "new_amount": new_amount,
        })
        if apply_changes:
            c.price_per_liter = new_price
            c.amount = new_amount

    for sr in SalesReturn.query.filter(SalesReturn.entry_date >= start, SalesReturn.entry_date <= end).all():
        new_price = resolve(sr.fuel_type, sr.entry_date)
        new_amount = round(sr.liters * new_price, 2)
        if abs(new_price - sr.price_per_liter) < 0.0001 and abs(new_amount - sr.amount) < 0.01:
            continue
        changes["returns"].append({
            "obj": sr, "fuel": sr.fuel_type.name, "label": sr.tank.label,
            "liters": sr.liters,
            "old_price": sr.price_per_liter, "new_price": new_price,
            "old_amount": sr.amount, "new_amount": new_amount,
        })
        if apply_changes:
            sr.price_per_liter = new_price
            sr.amount = new_amount

    changed = changes["sales"] + changes["direct_sales"] + changes["credits"] + changes["returns"]
    changes["count"] = len(changed)
    changes["old_total"] = round(sum(c["old_amount"] for c in changed), 2)
    changes["new_total"] = round(sum(c["new_amount"] for c in changed), 2)
    changes["difference"] = round(changes["new_total"] - changes["old_total"], 2)
    return changes


def fuels_missing_price_on(entry_date, fuel_types=None):
    """Fuel types with NO FuelPriceHistory row effective on or before
    entry_date, i.e. the ones price_on_date() can only answer by falling
    back to FuelType.price_per_liter - today's price.

    That fallback exists so a lookup never crashes, but for a date EARLIER
    than any recorded price it's silently wrong in the most expensive
    possible way: backfilling May's readings in August prices every litre
    at August's rate, and because Sale.total_amount is snapshotted at save
    time, correcting the price history afterwards does NOT repair those
    rows. Callers use this to warn BEFORE anything is entered for such a
    date, rather than leaving the guess invisible.

    One query for the whole catalogue, not one per fuel - this runs on
    every Ledger page load.
    """
    types = fuel_types if fuel_types is not None else FuelType.query.all()
    if not types:
        return []
    priced_ids = {
        row[0]
        for row in db.session.query(FuelPriceHistory.fuel_type_id)
        .filter(
            FuelPriceHistory.fuel_type_id.in_([f.id for f in types]),
            FuelPriceHistory.effective_date <= entry_date,
        )
        .distinct()
        .all()
    }
    return [f for f in types if f.id not in priced_ids]


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


def price_resolver(fuel_types=None):
    """Bulk-loaded equivalent of price_on_date(), for pages that need a
    date-specific price for MANY rows at once (e.g. one lookup per
    historical ledger entry). price_on_date() issues one SELECT per call,
    which is fine for the handful of lookups the ledger page does (one per
    fuel type, or one per nozzle, for a single date) but turns into an N+1
    storm - entries x fuel types queries - on a page like account_detail
    that resolves a price for every credit entry in an account's history.

    Loads every relevant FuelPriceHistory row in ONE query, then resolves
    "latest effective_date <= on_date" with a binary search per lookup
    instead of a database round trip. Pass the FuelType objects the caller
    already has (fuel_types) so the fallback price - FuelType.price_per_liter,
    read when no history row applies yet - is available with no extra
    query either; omitting it loads every fuel type instead.

    Returns resolve(fuel_type_or_id, on_date) -> price, matching
    price_on_date()'s semantics exactly, including its fallback and its
    same-day tie-break (the highest id wins when two rows share an
    effective_date).
    """
    if fuel_types is not None:
        types_by_id = {f.id: f for f in fuel_types}
        history_q = FuelPriceHistory.query.filter(FuelPriceHistory.fuel_type_id.in_(types_by_id.keys()))
    else:
        types_by_id = {f.id: f for f in FuelType.query.all()}
        history_q = FuelPriceHistory.query

    # Ascending order within each fuel type - by effective_date, then id so
    # that same-day rows land with the highest id last, matching
    # price_on_date()'s "effective_date desc, id desc" tie-break once we
    # bisect back from the right below.
    rows = history_q.order_by(
        FuelPriceHistory.fuel_type_id, FuelPriceHistory.effective_date, FuelPriceHistory.id
    ).all()

    dates_by_fuel = {}
    prices_by_fuel = {}
    for row in rows:
        dates_by_fuel.setdefault(row.fuel_type_id, []).append(row.effective_date)
        prices_by_fuel.setdefault(row.fuel_type_id, []).append(row.price_per_liter)

    def resolve(fuel_type_or_id, on_date):
        fuel_id = fuel_type_or_id.id if hasattr(fuel_type_or_id, "id") else fuel_type_or_id
        dates = dates_by_fuel.get(fuel_id)
        if dates:
            # bisect_right lands just past every row dated on_date or
            # earlier - the entry immediately before that point is the
            # latest one in force, mirroring effective_date <= on_date.
            idx = bisect.bisect_right(dates, on_date) - 1
            if idx >= 0:
                return prices_by_fuel[fuel_id][idx]
        fuel_type = types_by_id.get(fuel_id)
        if fuel_type is None:
            # Only reachable when fuel_types was passed but didn't include
            # this id - fall back to a direct lookup rather than crashing.
            fuel_type = db.session.get(FuelType, fuel_id)
            types_by_id[fuel_id] = fuel_type
        return fuel_type.price_per_liter

    return resolve


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
    returns_by_day = dict(
        db.session.query(SalesReturn.entry_date, func.sum(SalesReturn.liters))
        .filter(
            SalesReturn.tank_id == tank.id,
            SalesReturn.entry_date >= start,
            SalesReturn.entry_date <= dates[-1],
        )
        .group_by(SalesReturn.entry_date)
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
    direct_sales_by_day = dict(
        db.session.query(DirectSale.entry_date, func.sum(DirectSale.liters))
        .filter(
            DirectSale.tank_id == tank.id,
            DirectSale.entry_date >= start,
            DirectSale.entry_date <= dates[-1],
        )
        .group_by(DirectSale.entry_date)
        .all()
    )

    series = []
    for d in dates:
        running += (
            purchases_by_day.get(d, 0)
            + returns_by_day.get(d, 0)
            - sales_by_day.get(d, 0)
            - direct_sales_by_day.get(d, 0)
        )
        series.append(round(running, 2))
    return series


def sales_breakdown_for_date(entry_date, shift_id=None):
    """Total nozzle + direct sales for entry_date, split by how they were
    collected: credit (owed by a customer), bank (reconciled to a bank
    account), and cash (whatever's left over).

    Passing shift_id narrows every component to that one shift, which is
    what makes a per-shift cash reconciliation possible - summing the
    breakdowns of every shift on a date gives exactly the whole-date
    breakdown, since all three tables carry the same shift_id."""
    sale_q = db.session.query(func.coalesce(func.sum(Sale.total_amount), 0)).filter(
        Sale.entry_date == entry_date
    )
    direct_sale_q = db.session.query(func.coalesce(func.sum(DirectSale.total_amount), 0)).filter(
        DirectSale.entry_date == entry_date
    )
    credit_q = db.session.query(func.coalesce(func.sum(CreditGiven.amount), 0)).filter(
        CreditGiven.entry_date == entry_date
    )
    bank_q = db.session.query(func.coalesce(func.sum(BankSale.amount), 0)).filter(
        BankSale.entry_date == entry_date
    )
    if shift_id is not None:
        sale_q = sale_q.filter(Sale.shift_id == shift_id)
        direct_sale_q = direct_sale_q.filter(DirectSale.shift_id == shift_id)
        credit_q = credit_q.filter(CreditGiven.shift_id == shift_id)
        bank_q = bank_q.filter(BankSale.shift_id == shift_id)

    total = sale_q.scalar() + direct_sale_q.scalar()
    credit, bank = credit_q.scalar(), bank_q.scalar()
    cash = round(total - credit - bank, 2)
    return {"total": total, "credit": credit, "bank": bank, "cash": cash}


def default_shift():
    """The CURRENT PUMP's shift that new entries fall into when it hasn't
    set up its own - lowest sort_order among that pump's active shifts.
    Scoped implicitly by the tenant filter in tenancy.py (Shift.query is
    filtered to current_pump_id() the same as every other query), so this
    can only ever return a shift belonging to the pump making the request.

    Every pump gets exactly one "Full Day" shift seeded for it (see
    ensure_seed_users() in app.py for the very first pump, and Stage 2's
    pump-provisioning flow for every pump after that) - not "seeded once
    at startup" as this used to claim back when there was only ever one
    pump in the whole database. Returns None if called with no pump
    context (see tenancy.current_pump_id) rather than another pump's
    shift."""
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
    require tracking which specific delivery each liter came from.

    Each tank's OWN starting stock is folded in as one more (cost, liters)
    pair alongside real StockPurchase rows - effectively an implicit first
    "purchase" for that tank, dated starting_stock_date. Without this, a
    tank's opening stock would be sold with a zero cost basis (it's never
    a StockPurchase row), overstating profit. A tank only contributes when
    starting_stock_cost_per_liter is set - NULL means the historical cost
    is genuinely unknown (see Tank's docstring in models.py) and that
    tank's starting stock is treated as functionally invisible here,
    exactly as it always has been (this function never looked at Tank at
    all before this column existed). The date condition mirrors
    StockPurchase's own (entry_date <= as_of_date): starting_stock_date is
    None means "beginning of time" (same meaning it carries everywhere
    else in this codebase), so it always qualifies; otherwise it only
    counts once as_of_date has reached it."""
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

    starting_row = (
        db.session.query(
            func.coalesce(func.sum(Tank.starting_stock_liters * Tank.starting_stock_cost_per_liter), 0),
            func.coalesce(func.sum(Tank.starting_stock_liters), 0),
        )
        .filter(
            Tank.fuel_type_id == fuel_type.id,
            Tank.starting_stock_cost_per_liter.isnot(None),
            db.or_(Tank.starting_stock_date.is_(None), Tank.starting_stock_date <= as_of_date),
        )
        .first()
    )
    total_cost += starting_row[0] or 0
    total_liters += starting_row[1] or 0

    if not total_liters:
        return 0.0
    return round(total_cost / total_liters, 4)


def cogs_for_period(start, end):
    """Cost of the fuel actually sold between start and end, NET of any
    SalesReturn in the same window - a return un-sells the fuel, so both
    the liters and the revenue it's costed against have to come back out
    HERE, or it gets charged for fuel that came back into the tank while
    also being credited the full retail refund elsewhere (see the
    Sale.liters/testing_liters split for the equivalent idea on the
    other side of a sale). This is the ONLY place sales returns are
    netted into cost/margin - callers (reports_monthly(), reports_trends())
    must not subtract a sales-returns figure again on top of anything
    derived from here, or the refund gets double-counted.

    For each fuel type: net liters (gross sold minus gross returned) x
    that fuel's weighted average purchase cost as of the end of the
    period. Returns (total_cost, per_fuel_detail); total_cost is already
    net. Each detail dict keeps its original keys ("liters"/"revenue" are
    the NET figures margin is actually computed from) plus "gross_revenue",
    "returns_liters" and "returns_amount" so a caller can show the
    deduction instead of hiding it.
    """
    detail = []
    total = 0.0
    for ft in FuelType.query.order_by(FuelType.name).all():
        gross_liters = (
            db.session.query(func.coalesce(func.sum(Sale.liters), 0))
            .join(Nozzle, Sale.nozzle_id == Nozzle.id)
            .join(Tank, Nozzle.tank_id == Tank.id)
            .filter(Tank.fuel_type_id == ft.id, Sale.entry_date >= start, Sale.entry_date <= end)
            .scalar()
        )
        gross_revenue = (
            db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
            .join(Nozzle, Sale.nozzle_id == Nozzle.id)
            .join(Tank, Nozzle.tank_id == Tank.id)
            .filter(Tank.fuel_type_id == ft.id, Sale.entry_date >= start, Sale.entry_date <= end)
            .scalar()
        )
        # DirectSale is already tank-keyed - no Nozzle join needed, just
        # Tank -> fuel_type_id directly.
        direct_gross_liters = (
            db.session.query(func.coalesce(func.sum(DirectSale.liters), 0))
            .join(Tank, DirectSale.tank_id == Tank.id)
            .filter(Tank.fuel_type_id == ft.id, DirectSale.entry_date >= start, DirectSale.entry_date <= end)
            .scalar()
        )
        direct_gross_revenue = (
            db.session.query(func.coalesce(func.sum(DirectSale.total_amount), 0))
            .join(Tank, DirectSale.tank_id == Tank.id)
            .filter(Tank.fuel_type_id == ft.id, DirectSale.entry_date >= start, DirectSale.entry_date <= end)
            .scalar()
        )
        gross_liters += direct_gross_liters
        gross_revenue += direct_gross_revenue
        returns_liters = (
            db.session.query(func.coalesce(func.sum(SalesReturn.liters), 0))
            .filter(
                SalesReturn.fuel_type_id == ft.id,
                SalesReturn.entry_date >= start,
                SalesReturn.entry_date <= end,
            )
            .scalar()
        )
        returns_amount = (
            db.session.query(func.coalesce(func.sum(SalesReturn.amount), 0))
            .filter(
                SalesReturn.fuel_type_id == ft.id,
                SalesReturn.entry_date >= start,
                SalesReturn.entry_date <= end,
            )
            .scalar()
        )
        net_liters = gross_liters - returns_liters
        net_revenue = gross_revenue - returns_amount
        unit_cost = weighted_avg_cost(ft, end)
        # A full (or, across a window, an over-) return nets to zero or
        # fewer liters actually kept sold - cost can't go negative just
        # because more came back than was sold in this particular window,
        # and this also sidesteps ever dividing by a zero/negative liters
        # figure anywhere margin-per-liter is computed downstream.
        cost = 0.0 if net_liters <= 0 else round(net_liters * unit_cost, 2)
        total += cost
        if gross_liters or returns_liters:
            detail.append(
                {
                    "fuel": ft.name,
                    "gross_revenue": round(gross_revenue, 2),
                    "returns_liters": round(returns_liters, 2),
                    "returns_amount": round(returns_amount, 2),
                    "liters": round(net_liters, 2),
                    "revenue": round(net_revenue, 2),
                    "unit_cost": unit_cost,
                    "cost": cost,
                    "margin": round(net_revenue - cost, 2),
                }
            )
    return round(total, 2), detail


def credit_aging(account, as_of_date):
    """Age the account's outstanding debit balance into 0-30 / 31-60 /
    61-90 / 90+ day buckets.

    Receipts are applied FIFO against the oldest unsettled debit entries
    (the way a shopkeeper actually clears a khata), so what's left is the
    genuinely oldest money still owed. Only meaningful for a positive
    (owed-to-us) balance; a settled or creditor account ages to nothing.

    CRITICAL INVARIANT: the debit and credit sides built here MUST mirror
    Account.balance's terms EXACTLY, kind for kind and sign for sign - the
    buckets this returns have to sum to that same account's .balance (see
    test_credit_aging_matches_balance_all_kinds() in the scratchpad, which
    asserts exactly that across every kind at once). This drifted three
    times before this comment existed - a Phase 1 gap on credit-method
    SalesReturn, and two Phase 2B gaps on ProductSale/ProductPurchase -
    each time leaving an aging table that silently disagreed with the
    balance printed right above it on the Accounts/Statement pages, which
    is exactly backwards for a feature whose only job is deciding who to
    chase. Adding any new account-affecting entry type to Account.balance
    in the future means adding the matching line HERE in the same change,
    not as a follow-up.
    """
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
    for ps in account.product_sales:
        # account_id is only ever set on a ProductSale for method ==
        # "credit" (see ProductSale's docstring in models.py) - same
        # defensive guard account_ledger_events() uses for this backref.
        if ps.method == "credit":
            debits.append({"date": ps.entry_date, "amount": ps.amount})
    for oi in account.other_income_entries:
        # account_id is only ever set on an OtherIncome row for method ==
        # "credit" (see OtherIncome's docstring in models.py) - same
        # defensive guard account_ledger_events() uses for this backref.
        if oi.method == "credit":
            debits.append({"date": oi.entry_date, "amount": oi.amount})

    credits_total = (
        sum(r.amount for r in account.receipts)
        + sum((p.cost or 0) for p in account.stock_purchases if p.payment_type == "credit")
        + sum(s.deduction_amount for s in account.salary_payments)
        # A sales return refunded "on account" (method == "credit")
        # reduces what this account owes, the same direction as a
        # receipt - mirrors the identical guard in account_ledger_events().
        + sum(sr.amount for sr in account.sales_returns if sr.method == "credit")
        + (-account.opening_balance if account.opening_balance < 0 else 0)
    )
    # ProductPurchase.total_cost can be NEGATIVE (a return to the supplier
    # or a stock correction posted on credit against this account - see
    # ProductPurchase's docstring in models.py). A positive total_cost is
    # a credit here exactly like a fuel purchase's cost above (it reduces
    # what this account owes), but a negative one flips direction: it
    # claws back some of that credit, which is really a DEBIT - and it
    # needs its own date to age correctly, so it goes into the dated
    # debits list rather than getting netted into credits_total's single
    # undated figure the way a same-signed sum would.
    for pp in account.product_purchases:
        if pp.payment_type != "credit":
            continue
        if pp.total_cost >= 0:
            credits_total += pp.total_cost
        else:
            debits.append({"date": pp.entry_date, "amount": -pp.total_cost})

    debits.sort(key=lambda d: d["date"])

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
    computed from nozzle meter reading differences (Sale rows) AND direct
    tank-level entries (DirectSale rows), the same figures the sales stat
    cards are built from. A fuel type only ever has one or the other on
    any given date/tank in practice (see FuelType.entry_mode), but both
    are folded into the same totals unconditionally so a fuel type's
    history across a mode switch still adds up."""
    sales = Sale.query.filter_by(entry_date=entry_date).join(Nozzle).all()
    by_fuel = {}
    for s in sales:
        d = by_fuel.setdefault(s.nozzle.tank.fuel_type.name, {"liters": 0.0, "revenue": 0.0})
        d["liters"] += s.liters
        d["revenue"] += s.total_amount
    direct_sales = DirectSale.query.filter_by(entry_date=entry_date).join(Tank).all()
    for ds in direct_sales:
        d = by_fuel.setdefault(ds.tank.fuel_type.name, {"liters": 0.0, "revenue": 0.0})
        d["liters"] += ds.liters
        d["revenue"] += ds.total_amount
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
    for sr in account.sales_returns:
        # Refunded "on account" reduces what this account owes, the same
        # direction as a receipt - account_id is only ever set on a
        # SalesReturn for method == "credit", so this backref never picks
        # up a cash/bank return by mistake.
        if sr.method == "credit":
            events.append(
                {"kind": "sales_return", "entry_date": sr.entry_date, "sort_key": (sr.entry_date, sr.recorded_at), "obj": sr, "delta": -sr.amount}
            )
    for ps in account.product_sales:
        # account_id is only ever set on a ProductSale for method ==
        # "credit" - same defensive guard as sales_returns above.
        if ps.method == "credit":
            events.append(
                {"kind": "product_sale", "entry_date": ps.entry_date, "sort_key": (ps.entry_date, ps.recorded_at), "obj": ps, "delta": ps.amount}
            )
    for oi in account.other_income_entries:
        # account_id is only ever set on an OtherIncome row for method ==
        # "credit" - same defensive guard as product_sales above.
        if oi.method == "credit":
            events.append(
                {"kind": "other_income", "entry_date": oi.entry_date, "sort_key": (oi.entry_date, oi.recorded_at), "obj": oi, "delta": oi.amount}
            )
    for pp in account.product_purchases:
        if pp.payment_type == "credit":
            events.append(
                {"kind": "product_purchase", "entry_date": pp.entry_date, "sort_key": (pp.entry_date, pp.recorded_at), "obj": pp, "delta": -(pp.total_cost or 0)}
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
    sales - Sale AND DirectSale combined - minus credit minus bank sales)
    and every cash-method receipt, minus cash physically deposited into a
    bank account and every cash-method outflow (loans, expenses, fuel
    purchases, supplier payments - each of those can instead be routed
    through a specific bank account via "Paid via", in which case it hits
    that bank's balance instead and is excluded here)."""
    total_sales = db.session.query(func.coalesce(func.sum(Sale.total_amount), 0)).scalar()
    total_sales += db.session.query(func.coalesce(func.sum(DirectSale.total_amount), 0)).scalar()
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
    total_cash_returns = (
        db.session.query(func.coalesce(func.sum(SalesReturn.amount), 0))
        .filter(SalesReturn.method == "cash")
        .scalar()
    )
    total_cash_product_sales = (
        db.session.query(func.coalesce(func.sum(ProductSale.amount), 0))
        .filter(ProductSale.method == "cash")
        .scalar()
    )
    total_cash_product_purchases = (
        db.session.query(func.coalesce(func.sum(ProductPurchase.total_cost), 0))
        .filter(ProductPurchase.payment_type == "cash", ProductPurchase.method == "cash")
        .scalar()
    )
    total_cash_other_income = (
        db.session.query(func.coalesce(func.sum(OtherIncome.amount), 0))
        .filter(OtherIncome.method == "cash")
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
        - total_cash_salaries
        - total_cash_returns
        + total_cash_product_sales
        - total_cash_product_purchases
        + total_cash_other_income,
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

    # Cash portion of nozzle + direct sales for a date is total sales
    # minus credit minus bank sales on that same date - mirrors
    # sales_breakdown_for_date, just summed per date across all of
    # history in one grouped query each rather than one query per date.
    sales_by_date = dict(
        db.session.query(Sale.entry_date, func.sum(Sale.total_amount)).group_by(Sale.entry_date).all()
    )
    direct_sales_by_date = dict(
        db.session.query(DirectSale.entry_date, func.sum(DirectSale.total_amount))
        .group_by(DirectSale.entry_date)
        .all()
    )
    credit_by_date = dict(
        db.session.query(CreditGiven.entry_date, func.sum(CreditGiven.amount))
        .group_by(CreditGiven.entry_date)
        .all()
    )
    bank_sales_by_date = dict(
        db.session.query(BankSale.entry_date, func.sum(BankSale.amount)).group_by(BankSale.entry_date).all()
    )
    for entry_date in set(sales_by_date) | set(direct_sales_by_date) | set(credit_by_date) | set(bank_sales_by_date):
        cash_amount = (
            sales_by_date.get(entry_date, 0)
            + direct_sales_by_date.get(entry_date, 0)
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
    add(
        db.session.query(SalesReturn.entry_date, func.sum(SalesReturn.amount))
        .filter(SalesReturn.method == "cash")
        .group_by(SalesReturn.entry_date)
        .all(),
        sign=-1,
    )
    add(
        db.session.query(ProductSale.entry_date, func.sum(ProductSale.amount))
        .filter(ProductSale.method == "cash")
        .group_by(ProductSale.entry_date)
        .all()
    )
    add(
        db.session.query(ProductPurchase.entry_date, func.sum(ProductPurchase.total_cost))
        .filter(ProductPurchase.payment_type == "cash", ProductPurchase.method == "cash")
        .group_by(ProductPurchase.entry_date)
        .all(),
        sign=-1,
    )
    add(
        db.session.query(OtherIncome.entry_date, func.sum(OtherIncome.amount))
        .filter(OtherIncome.method == "cash")
        .group_by(OtherIncome.entry_date)
        .all()
    )

    return changes


def cash_account_balance_as_of(cash_account, as_of_date):
    """Cash-in-hand as it stood at the END of as_of_date - the closing
    balance for that date, not the all-time figure cash_account_balance()
    returns. Same components as _cash_daily_net_changes() (which already
    includes DirectSale in its cash-sales component), summed only through
    as_of_date.

    A new, additional function - used ONLY on the Ledger and Daily Report
    pages, which are date-driven and need to show the balance as it stood
    on whatever date is being paged to, not today's all-time figure.
    cash_account_balance() itself is deliberately left untouched: it's
    still used everywhere else (dashboard, account pages, settings) for
    the current/all-time figure, unconditionally."""
    changes = _cash_daily_net_changes()
    return round(sum(v for d, v in changes.items() if d <= as_of_date), 2)


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

    # Union of Sale AND DirectSale dates - a date with ONLY direct-entry
    # activity (no metered Sale at all) still has a nonzero cash
    # contribution via sales_breakdown_for_date() (already DirectSale-
    # aware), and skipping it here would silently drop that day's cash
    # sales row from the history list, breaking every running balance
    # shown after it.
    sale_dates = {row[0] for row in db.session.query(Sale.entry_date).distinct().all()}
    sale_dates |= {row[0] for row in db.session.query(DirectSale.entry_date).distinct().all()}
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
    for sr in SalesReturn.query.filter_by(method="cash").all():
        events.append(
            {"kind": "sales_return", "entry_date": sr.entry_date, "sort_key": (sr.entry_date, sr.recorded_at), "obj": sr, "delta": -sr.amount}
        )
    for ps in ProductSale.query.filter_by(method="cash").all():
        events.append(
            {"kind": "product_sale", "entry_date": ps.entry_date, "sort_key": (ps.entry_date, ps.recorded_at), "obj": ps, "delta": ps.amount}
        )
    for pp in ProductPurchase.query.filter_by(payment_type="cash", method="cash").all():
        events.append(
            {"kind": "product_purchase", "entry_date": pp.entry_date, "sort_key": (pp.entry_date, pp.recorded_at), "obj": pp, "delta": -(pp.total_cost or 0)}
        )
    for oi in OtherIncome.query.filter_by(method="cash").all():
        events.append(
            {"kind": "other_income", "entry_date": oi.entry_date, "sort_key": (oi.entry_date, oi.recorded_at), "obj": oi, "delta": oi.amount}
        )

    events.sort(key=lambda e: e["sort_key"])
    running = 0.0
    for e in events:
        running += e["delta"]
        e["running_balance"] = round(running, 2)

    events.reverse()
    return events


# ------------------------------------------------------- product catalogue

def product_rates_on_date(product, entry_date):
    """The (purchase_rate, retail_rate) actually in effect on entry_date,
    from ProductRateHistory - not necessarily the product's current cached
    rates. Falls back to the product's current purchase_rate/retail_rate
    if no history row applies (e.g. a product that predates rate history
    being tracked). Mirrors price_on_date() exactly, including its
    same-day tie-break (the highest id wins when two rows share an
    effective_date)."""
    row = (
        ProductRateHistory.query.filter(
            ProductRateHistory.product_id == product.id,
            ProductRateHistory.effective_date <= entry_date,
        )
        .order_by(ProductRateHistory.effective_date.desc(), ProductRateHistory.id.desc())
        .first()
    )
    if row:
        return row.purchase_rate, row.retail_rate
    return product.purchase_rate, product.retail_rate


def record_product_rates(product, purchase_rate, retail_rate, effective_date):
    """Log a rate change effective as of effective_date, and keep
    Product.purchase_rate/retail_rate (the "current rate" cache read
    everywhere that just wants today's rates) pointing at whichever
    history row is latest as of today - so a same-day change becomes
    today's rate, and a correction to an older date doesn't make today's
    rate stale. Mirrors record_fuel_price()."""
    db.session.add(
        ProductRateHistory(
            product_id=product.id,
            purchase_rate=purchase_rate,
            retail_rate=retail_rate,
            effective_date=effective_date,
        )
    )
    db.session.flush()
    product.purchase_rate, product.retail_rate = product_rates_on_date(product, datetime.now().date())


def product_rate_resolver(products=None):
    """Bulk-loaded equivalent of product_rates_on_date(), for pages that
    need date-specific rates for MANY rows at once - see price_resolver()'s
    docstring for why this matters: resolving one product's rate per row
    with product_rates_on_date() would fire one query per row per product,
    an N+1 storm this codebase has already been bitten by once.

    Loads every relevant ProductRateHistory row in ONE query, then
    resolves "latest effective_date <= on_date" with a binary search per
    lookup instead of a database round trip. Pass the Product objects the
    caller already has (products) so the fallback rates -
    Product.purchase_rate/retail_rate, read when no history row applies
    yet - are available with no extra query either; omitting it loads
    every product instead.

    Returns resolve(product_or_id, on_date) -> (purchase_rate, retail_rate),
    matching product_rates_on_date()'s semantics exactly, including its
    fallback and its same-day tie-break.
    """
    if products is not None:
        products_by_id = {p.id: p for p in products}
        history_q = ProductRateHistory.query.filter(ProductRateHistory.product_id.in_(products_by_id.keys()))
    else:
        products_by_id = {p.id: p for p in Product.query.all()}
        history_q = ProductRateHistory.query

    # Ascending order within each product - by effective_date, then id so
    # that same-day rows land with the highest id last, matching
    # product_rates_on_date()'s "effective_date desc, id desc" tie-break
    # once we bisect back from the right below.
    rows = history_q.order_by(
        ProductRateHistory.product_id, ProductRateHistory.effective_date, ProductRateHistory.id
    ).all()

    dates_by_product = {}
    rates_by_product = {}
    for row in rows:
        dates_by_product.setdefault(row.product_id, []).append(row.effective_date)
        rates_by_product.setdefault(row.product_id, []).append((row.purchase_rate, row.retail_rate))

    def resolve(product_or_id, on_date):
        product_id = product_or_id.id if hasattr(product_or_id, "id") else product_or_id
        dates = dates_by_product.get(product_id)
        if dates:
            idx = bisect.bisect_right(dates, on_date) - 1
            if idx >= 0:
                return rates_by_product[product_id][idx]
        product = products_by_id.get(product_id)
        if product is None:
            # Only reachable when products was passed but didn't include
            # this id - fall back to a direct lookup rather than crashing.
            product = db.session.get(Product, product_id)
            products_by_id[product_id] = product
        return product.purchase_rate, product.retail_rate

    return resolve


def product_stock(product, as_of_date):
    """Book stock for `product` at the END of as_of_date - opening stock
    plus every purchase minus every sale. Mirrors book_stock()'s
    three-branch structure EXACTLY (see book_stock() above for the full
    reasoning - the only difference is there's no separate "returns"
    table here: a product return is just a negative-quantity
    ProductPurchase, see its docstring in models.py, so it falls out of
    the same purchases sum with no special case).

    product.opening_stock is the level at the START of
    product.opening_stock_date (equivalently, the END of the day before
    it - see Product in models.py), which splits into the same three
    cases book_stock() handles:

    - opening_stock_date is None: back-compat/beginning-of-time - sum
      every purchase and sale up to and including as_of_date.
    - as_of_date >= opening_stock_date (FORWARD): sum purchases/sales from
      opening_stock_date through as_of_date, inclusive on both ends.
    - as_of_date < opening_stock_date (BACKWARD): there's no ledger
      history before the baseline to sum forward from, so instead undo
      everything strictly between the two dates. Stock only ever moves by
      purchase in and sale out, so running it backwards is valid
      arithmetic - this is what makes "count stock today, then backfill
      last month" come out correct.
    """
    if product.opening_stock_date is None:
        purchased = (
            db.session.query(func.coalesce(func.sum(ProductPurchase.quantity), 0))
            .filter(ProductPurchase.product_id == product.id, ProductPurchase.entry_date <= as_of_date)
            .scalar()
        )
        sold = (
            db.session.query(func.coalesce(func.sum(ProductSale.quantity), 0))
            .filter(ProductSale.product_id == product.id, ProductSale.entry_date <= as_of_date)
            .scalar()
        )
        return round(product.opening_stock + purchased - sold, 2)

    if as_of_date >= product.opening_stock_date:
        purchased = (
            db.session.query(func.coalesce(func.sum(ProductPurchase.quantity), 0))
            .filter(
                ProductPurchase.product_id == product.id,
                ProductPurchase.entry_date >= product.opening_stock_date,
                ProductPurchase.entry_date <= as_of_date,
            )
            .scalar()
        )
        sold = (
            db.session.query(func.coalesce(func.sum(ProductSale.quantity), 0))
            .filter(
                ProductSale.product_id == product.id,
                ProductSale.entry_date >= product.opening_stock_date,
                ProductSale.entry_date <= as_of_date,
            )
            .scalar()
        )
        return round(product.opening_stock + purchased - sold, 2)

    # BACKWARD: as_of_date < opening_stock_date - undo the entries
    # strictly between the two dates instead of summing forward.
    purchased = (
        db.session.query(func.coalesce(func.sum(ProductPurchase.quantity), 0))
        .filter(
            ProductPurchase.product_id == product.id,
            ProductPurchase.entry_date > as_of_date,
            ProductPurchase.entry_date < product.opening_stock_date,
        )
        .scalar()
    )
    sold = (
        db.session.query(func.coalesce(func.sum(ProductSale.quantity), 0))
        .filter(
            ProductSale.product_id == product.id,
            ProductSale.entry_date > as_of_date,
            ProductSale.entry_date < product.opening_stock_date,
        )
        .scalar()
    )
    return round(product.opening_stock - purchased + sold, 2)


def product_stock_summary(as_of_date, products=None):
    """Per-product stock summary as of as_of_date: opening stock,
    received, sold, and on-hand stock - one row per product. Defaults to
    every ACTIVE product (alphabetical) when products isn't given; pass
    an explicit list (e.g. including inactive ones) to override that.

    Computed with two GROUPED queries (purchases-by-product-and-date,
    sales-by-product-and-date) instead of calling product_stock() once per
    product, which would be 2 extra queries per product - an N+1 this
    codebase treats as a bug rather than a style nit (see
    product_rate_resolver()'s docstring for the same argument applied to
    rates). "on_hand" for each product is computed with exactly the same
    forward/backward window product_stock() would use for that product's
    own opening_stock_date, so the two MUST always agree - this is
    asserted in tests rather than just hoped for.
    """
    if products is None:
        products = Product.query.filter_by(is_active=True).order_by(Product.name).all()
    if not products:
        return []

    ids = [p.id for p in products]

    purchase_rows = (
        db.session.query(ProductPurchase.product_id, ProductPurchase.entry_date, func.sum(ProductPurchase.quantity))
        .filter(ProductPurchase.product_id.in_(ids))
        .group_by(ProductPurchase.product_id, ProductPurchase.entry_date)
        .all()
    )
    sale_rows = (
        db.session.query(ProductSale.product_id, ProductSale.entry_date, func.sum(ProductSale.quantity))
        .filter(ProductSale.product_id.in_(ids))
        .group_by(ProductSale.product_id, ProductSale.entry_date)
        .all()
    )

    purchases_by_product = {}
    for pid, d, qty in purchase_rows:
        purchases_by_product.setdefault(pid, []).append((d, qty))
    sales_by_product = {}
    for pid, d, qty in sale_rows:
        sales_by_product.setdefault(pid, []).append((d, qty))

    summary = []
    for p in products:
        purchases = purchases_by_product.get(p.id, [])
        sales = sales_by_product.get(p.id, [])

        if p.opening_stock_date is None or as_of_date >= p.opening_stock_date:
            lower = p.opening_stock_date  # None means no lower bound - mirrors product_stock()'s back-compat branch
            received = sum(qty for d, qty in purchases if (lower is None or d >= lower) and d <= as_of_date)
            sold = sum(qty for d, qty in sales if (lower is None or d >= lower) and d <= as_of_date)
            on_hand = p.opening_stock + received - sold
        else:
            # BACKWARD - undo entries strictly between as_of_date and
            # opening_stock_date, same as product_stock()'s BACKWARD branch.
            received = sum(qty for d, qty in purchases if as_of_date < d < p.opening_stock_date)
            sold = sum(qty for d, qty in sales if as_of_date < d < p.opening_stock_date)
            on_hand = p.opening_stock - received + sold

        on_hand = round(on_hand, 2)
        summary.append(
            {
                "product": p,
                "opening": p.opening_stock,
                "received": round(received, 2),
                "sold": round(sold, 2),
                "on_hand": on_hand,
                "is_low": on_hand <= p.low_stock_threshold,
            }
        )
    return summary


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
    for sr in bank_account.sales_returns:
        # bank_account_id is only ever set on a SalesReturn for
        # method == "bank", so this backref never picks up a cash/credit
        # return by mistake.
        if sr.method == "bank":
            events.append(
                {"kind": "sales_return", "entry_date": sr.entry_date, "sort_key": (sr.entry_date, sr.recorded_at), "obj": sr, "delta": -sr.amount}
            )
    for ps in bank_account.product_sales:
        if ps.method == "bank":
            events.append(
                {"kind": "product_sale", "entry_date": ps.entry_date, "sort_key": (ps.entry_date, ps.recorded_at), "obj": ps, "delta": ps.amount}
            )
    for pp in bank_account.product_purchases:
        if pp.payment_type == "cash":
            events.append(
                {"kind": "product_purchase", "entry_date": pp.entry_date, "sort_key": (pp.entry_date, pp.recorded_at), "obj": pp, "delta": -(pp.total_cost or 0)}
            )
    # No method == "bank" filter needed - other_income_entries is only ever
    # populated for method == "bank" rows (bank_account_id is only ever set
    # that way, see the route), unlike product_sales above which is a
    # shared backref across both cash and bank rows.
    for oi in bank_account.other_income_entries:
        events.append(
            {"kind": "other_income", "entry_date": oi.entry_date, "sort_key": (oi.entry_date, oi.recorded_at), "obj": oi, "delta": oi.amount}
        )

    events.sort(key=lambda e: e["sort_key"])
    running = 0.0
    for e in events:
        running += e["delta"]
        e["running_balance"] = round(running, 2)

    events.reverse()
    return events


def bank_account_balance_as_of(bank_account, as_of_date):
    """This bank account's balance as it stood at the END of as_of_date -
    mirrors BankAccount.balance's component list EXACTLY (see that
    property in models.py: bank_sales, deposits, receipts,
    employee_loans_paid, expenses, fuel_purchases restricted to
    payment_type == "cash", supplier_payments_paid, salary_payments_paid
    net of deduction, sales_returns restricted to method == "bank",
    product_sales restricted to method == "bank", product_purchases
    restricted to payment_type == "cash", other_income_entries) - with
    entry_date <= as_of_date added to each component.

    DirectSale never appears here - it's fuel revenue exactly like Sale,
    and neither table ever touches a bank account directly; only BankSale
    (a reclassification of revenue collected via bank, independent of
    whether the underlying sale was recorded via meter or direct entry -
    see the module-level notes on this app's design) does, and it's
    already covered by bank_sales above.

    Explicit SQL sums (not Python-summing the relationship backrefs, which
    have no natural place to add a date filter without loading everything
    into memory first) - same style as cash_account_balance().

    A new, additional function - used ONLY on the Ledger and Daily Report
    pages. BankAccount.balance itself is deliberately left untouched for
    every other page (Accounts, bank account detail, dashboard, ...),
    which must keep showing the current/all-time figure exactly as before."""
    bank_sales_total = (
        db.session.query(func.coalesce(func.sum(BankSale.amount), 0))
        .filter(BankSale.bank_account_id == bank_account.id, BankSale.entry_date <= as_of_date)
        .scalar()
    )
    deposits_total = (
        db.session.query(func.coalesce(func.sum(CashDeposit.amount), 0))
        .filter(CashDeposit.bank_account_id == bank_account.id, CashDeposit.entry_date <= as_of_date)
        .scalar()
    )
    receipts_total = (
        db.session.query(func.coalesce(func.sum(Receipt.amount), 0))
        .filter(Receipt.bank_account_id == bank_account.id, Receipt.entry_date <= as_of_date)
        .scalar()
    )
    loans_total = (
        db.session.query(func.coalesce(func.sum(EmployeeLoan.amount), 0))
        .filter(EmployeeLoan.bank_account_id == bank_account.id, EmployeeLoan.entry_date <= as_of_date)
        .scalar()
    )
    expenses_total = (
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.bank_account_id == bank_account.id, Expense.entry_date <= as_of_date)
        .scalar()
    )
    fuel_purchases_total = (
        db.session.query(func.coalesce(func.sum(StockPurchase.cost), 0))
        .filter(
            StockPurchase.bank_account_id == bank_account.id,
            StockPurchase.payment_type == "cash",
            StockPurchase.entry_date <= as_of_date,
        )
        .scalar()
    )
    supplier_payments_total = (
        db.session.query(func.coalesce(func.sum(SupplierPayment.amount), 0))
        .filter(SupplierPayment.bank_account_id == bank_account.id, SupplierPayment.entry_date <= as_of_date)
        .scalar()
    )
    salaries_total = (
        db.session.query(
            func.coalesce(func.sum(SalaryPayment.gross_amount - SalaryPayment.deduction_amount), 0)
        )
        .filter(SalaryPayment.bank_account_id == bank_account.id, SalaryPayment.entry_date <= as_of_date)
        .scalar()
    )
    sales_returns_total = (
        db.session.query(func.coalesce(func.sum(SalesReturn.amount), 0))
        .filter(
            SalesReturn.bank_account_id == bank_account.id,
            SalesReturn.method == "bank",
            SalesReturn.entry_date <= as_of_date,
        )
        .scalar()
    )
    product_sales_total = (
        db.session.query(func.coalesce(func.sum(ProductSale.amount), 0))
        .filter(
            ProductSale.bank_account_id == bank_account.id,
            ProductSale.method == "bank",
            ProductSale.entry_date <= as_of_date,
        )
        .scalar()
    )
    product_purchases_total = (
        db.session.query(func.coalesce(func.sum(ProductPurchase.total_cost), 0))
        .filter(
            ProductPurchase.bank_account_id == bank_account.id,
            ProductPurchase.payment_type == "cash",
            ProductPurchase.entry_date <= as_of_date,
        )
        .scalar()
    )
    # No method == "bank" filter needed in the SQL itself - bank_account_id
    # is only ever set on an OtherIncome row for method == "bank" (see the
    # route), exactly as deposits_total/receipts_total above already rely
    # on for their own bank_account_id filter.
    other_income_total = (
        db.session.query(func.coalesce(func.sum(OtherIncome.amount), 0))
        .filter(OtherIncome.bank_account_id == bank_account.id, OtherIncome.entry_date <= as_of_date)
        .scalar()
    )
    # The opening balance is anchored on its own opening_balance_date (or
    # created_at.date() if unset - same fallback _cash_daily_net_changes()
    # uses for the cash account) and only counted once as_of_date has
    # reached it. Unlike BankAccount.balance (always-unconditional - it
    # has no date to be "as of" in the first place), an AS-OF balance for
    # a date before the account's own baseline would otherwise show money
    # that, as of that date, hadn't been declared into this account yet -
    # exactly the double-counting book_stock()/Account.balance already
    # guard against for a tank/account's own opening figure. Matters for
    # backfilling: an owner setting up a bank account today and then
    # entering older paper records from before that setup date must see
    # this account read as empty on those older dates, not carrying an
    # opening balance backwards in time.
    opening_date = bank_account.opening_balance_date or bank_account.created_at.date()
    opening = bank_account.opening_balance if as_of_date >= opening_date else 0.0
    return round(
        opening
        + bank_sales_total
        + deposits_total
        + receipts_total
        - loans_total
        - expenses_total
        - fuel_purchases_total
        - supplier_payments_total
        - salaries_total
        - sales_returns_total
        + product_sales_total
        - product_purchases_total
        + other_income_total,
        2,
    )


def product_margin_for_period(start, end):
    """Dealer commission earned on non-fuel products (lubricants, filters,
    shop items) sold between start and end.

    Unlike cogs_for_period() - which has to value fuel SOLD at a weighted
    average of every purchase invoice, because liters pooled in a tank are
    fungible and any one liter sold can't be traced to one delivery - this
    is EXACT, no weighted average involved at all. Every ProductSale row
    already carries its own purchase_rate, snapshotted at the moment of
    sale (see ProductSale's docstring in models.py), so a line's cost is
    just quantity x that row's own rate. There's no "as of end date" cost
    lookup here the way weighted_avg_cost() needs one - the rate a sale
    locked in at the time is the number that counts, forever, regardless
    of where rates move to afterwards (see product_margin exactness in the
    Phase 2B acceptance tests).

    Returns (total_revenue, total_cost, total_commission,
    detail_by_category) - detail_by_category has one row per category
    (lubricant/filter/shop/other) that had at least one unit sold in the
    period, each with quantity/revenue/cost/margin, computed with ONE
    grouped query (join + group by category) rather than a per-product
    loop - see product_rate_resolver()'s docstring for why that matters on
    a ~95-SKU catalogue.
    """
    qty_expr = func.coalesce(func.sum(ProductSale.quantity), 0)
    revenue_expr = func.coalesce(func.sum(ProductSale.amount), 0)
    cost_expr = func.coalesce(func.sum(ProductSale.quantity * ProductSale.purchase_rate), 0)

    rows = (
        db.session.query(Product.category, qty_expr, revenue_expr, cost_expr)
        .select_from(ProductSale)
        .join(Product, ProductSale.product_id == Product.id)
        .filter(ProductSale.entry_date >= start, ProductSale.entry_date <= end)
        .group_by(Product.category)
        .all()
    )

    detail = []
    total_revenue = 0.0
    total_cost = 0.0
    for category, quantity, revenue, cost in rows:
        if not quantity:
            continue
        revenue = round(float(revenue), 2)
        cost = round(float(cost), 2)
        total_revenue += revenue
        total_cost += cost
        detail.append(
            {
                "category": category,
                "quantity": round(quantity, 2),
                "revenue": revenue,
                "cost": cost,
                "margin": round(revenue - cost, 2),
            }
        )
    detail.sort(key=lambda d: d["category"])

    total_revenue = round(total_revenue, 2)
    total_cost = round(total_cost, 2)
    total_commission = round(total_revenue - total_cost, 2)
    return total_revenue, total_cost, total_commission, detail
