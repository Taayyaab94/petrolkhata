"""Ledger routes: the day's ledger page and every entry-creation route
(readings, direct sale, dip, handover(+write-off), salary, receipt,
credit, sales return, testing, expense, purchase, tanker sale,
product sale, other income, product purchase, supplier payment, bank
sale, cash deposit, employee loan, fuel price), plus the generic entry
delete/edit dispatch and its per-kind _edit_*() handlers, and the
account/product/shift/payment-method resolver helpers used only by this
group.

resolve_payment_method(), _resolve_entry_mode(), _credit_amount_error(),
and _derive_credit_liters_amount() stay behind in app.py instead: they're
also used by the /accounts/entry/... edit routes in routes_accounts.py,
so per the "used by 2+ route modules stays in app.py" rule they can't
move here even though they sit right next to the helpers that could.

Moved verbatim out of app.py - this is the ledger/... route group (plus
entry create/edit/delete), unchanged except for its location.
"""

from datetime import date

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from formatting import format_number
from extensions import db
from balance_terms import ACCOUNT_TERMS, BANK_TERMS, eager_load
from ledger_logic import (
    active_shifts,
    allocate_group_payment,
    bank_account_balance_as_of,
    book_stock,
    cash_account_balance_as_of,
    default_shift,
    first_negative_cash_date,
    fuel_sales_for_date,
    fuels_missing_price_on,
    handover_rows_for_date,
    liters_from_dip_cm,
    nearest_earlier_reading,
    next_sale_on_or_after,
    previous_reading_for,
    previous_slot,
    price_on_date,
    product_rate_resolver,
    product_rates_on_date,
    product_stock,
    product_stock_summary,
    record_fuel_price,
    record_product_rates,
    sales_breakdown_for_date,
    split_combined_direct_sale,
    sync_sale_testing,
)
from models import (
    Account,
    BankAccount,
    BankSale,
    CashDeposit,
    CashHandover,
    CreditGiven,
    DirectSale,
    Dispenser,
    EmployeeLoan,
    Expense,
    FuelType,
    Nozzle,
    NozzleReset,
    NozzleTesting,
    OtherIncome,
    Product,
    ProductPurchase,
    ProductSale,
    Receipt,
    SalaryPayment,
    Sale,
    SalesReturn,
    Shift,
    StockPurchase,
    SupplierPayment,
    Tank,
    TankDip,
    TankerDeal,
)
from app import (
    app,
    PRODUCT_CATEGORIES,
    PRODUCT_UNITS,
    _credit_amount_error,
    _derive_credit_liters_amount,
    _resolve_entry_mode,
    cash_shortfall_message,
    get_cash_account,
    owner_required,
    parse_date_param,
    resolve_payment_method,
    would_overdraw_cash,
)

# -------------------------------------------------------------- ledger ----

def get_feed_for_date(entry_date, full_visibility):
    events = []
    for s in Sale.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "sale", "sort": s.recorded_at, "obj": s})
    for ds in DirectSale.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "direct_sale", "sort": ds.recorded_at, "obj": ds})
    for r in Receipt.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "receipt", "sort": r.recorded_at, "obj": r})
    for c in CreditGiven.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "credit", "sort": c.recorded_at, "obj": c})
    for sr in SalesReturn.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "sales_return", "sort": sr.recorded_at, "obj": sr})
    for dip in TankDip.query.filter_by(entry_date=entry_date).all():
        variance = round(dip.dip_liters - book_stock(dip.tank, dip.entry_date), 2)
        events.append({"kind": "dip", "sort": dip.recorded_at, "obj": dip, "variance": variance})
    for bs in BankSale.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "bank_sale", "sort": bs.recorded_at, "obj": bs})
    for el in EmployeeLoan.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "employee_loan", "sort": el.recorded_at, "obj": el})
    for h in CashHandover.query.filter_by(entry_date=entry_date).all():
        expected = sales_breakdown_for_date(entry_date, shift_id=h.shift_id)["cash"]
        events.append(
            {
                "kind": "handover",
                "sort": h.recorded_at,
                "obj": h,
                "variance": round(h.declared_amount - expected, 2),
            }
        )
    for ps in ProductSale.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "product_sale", "sort": ps.recorded_at, "obj": ps})
    for oi in OtherIncome.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "other_income", "sort": oi.recorded_at, "obj": oi})
    for nt in NozzleTesting.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "nozzle_testing", "sort": nt.recorded_at, "obj": nt})
    # A pass-through tanker deal (see TankerDeal in models.py) shows on
    # the day's feed like any other entry, but as ONE row carrying its own
    # margin rather than a sale row and a purchase row - it is a single
    # deal, and splitting it would make it look like the pump both bought
    # stock and dispensed fuel, which is exactly what it did not do.
    for td in TankerDeal.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "tanker_deal", "sort": td.recorded_at, "obj": td})

    if full_visibility:
        for e in Expense.query.filter_by(entry_date=entry_date).all():
            events.append({"kind": "expense", "sort": e.recorded_at, "obj": e})
        for pu in StockPurchase.query.filter_by(entry_date=entry_date).all():
            events.append({"kind": "purchase", "sort": pu.recorded_at, "obj": pu})
        for sp in SupplierPayment.query.filter_by(entry_date=entry_date).all():
            events.append({"kind": "supplier_payment", "sort": sp.recorded_at, "obj": sp})
        for cd in CashDeposit.query.filter_by(entry_date=entry_date).all():
            events.append({"kind": "cash_deposit", "sort": cd.recorded_at, "obj": cd})
        for sal in SalaryPayment.query.filter_by(entry_date=entry_date).all():
            events.append({"kind": "salary", "sort": sal.recorded_at, "obj": sal})
        for pp in ProductPurchase.query.filter_by(entry_date=entry_date).all():
            events.append({"kind": "product_purchase", "sort": pp.recorded_at, "obj": pp})

    events.sort(key=lambda e: e["sort"], reverse=True)
    return events


@app.route("/ledger")
@login_required
def ledger():
    selected_date = parse_date_param(request.args.get("date"))
    shifts = active_shifts()
    # Which shift's readings this page is editing. With a single shift
    # configured there's nothing to choose and the selector stays hidden.
    requested_shift_id = request.args.get("shift", type=int)
    selected_shift = next((s for s in shifts if s.id == requested_shift_id), None) or (
        shifts[0] if shifts else None
    )

    dispensers = Dispenser.query.order_by(Dispenser.number).all()
    nozzle_rows = []
    for d in dispensers:
        for n in d.nozzles:
            existing = Sale.query.filter_by(
                nozzle_id=n.id, entry_date=selected_date, shift_id=selected_shift.id
            ).first()
            prev_value, prev_auto = previous_reading_for(n, selected_date, selected_shift)
            if not prev_auto and existing:
                prev_value = existing.previous_reading
            nozzle_rows.append(
                {
                    "nozzle": n,
                    "dispenser": d,
                    "previous_reading": prev_value,
                    "previous_is_auto": prev_auto,
                    "existing_reading": existing.current_reading if existing else None,
                    "existing_testing": existing.testing_liters if existing else None,
                    # Read-only info for the reading row's calc-preview (the
                    # per-nozzle "Testing (L)" input is gone - testing is
                    # now recorded separately via the Sales Return/Testing
                    # section and reconciled by sync_sale_testing()) and for
                    # the Testing form's own per-nozzle calc-preview.
                    "existing_gross": round(existing.current_reading - existing.previous_reading, 2) if existing else None,
                    # The price in effect ON selected_date, not necessarily
                    # today's current price - so paging back to an earlier
                    # date shows what fuel cost then, and a price change
                    # made today doesn't retroactively relabel past days.
                    "price": price_on_date(n.fuel_type, selected_date),
                }
            )

    tanks = Tank.query.order_by(Tank.number).all()
    tank_rows = []
    for t in tanks:
        stock = book_stock(t, selected_date)
        existing_dip = TankDip.query.filter_by(tank_id=t.id, entry_date=selected_date).first()
        tank_rows.append(
            {
                "tank": t,
                "book_stock": stock,
                "existing_dip": existing_dip.dip_liters if existing_dip else None,
                "existing_dip_cm": existing_dip.dip_cm if existing_dip else None,
                "existing_water_cm": existing_dip.water_cm if existing_dip else None,
                "is_low": stock <= t.low_stock_threshold,
                # A tank with a calibration chart collects a depth reading
                # (what a dip stick actually measures) and converts; one
                # without keeps taking liters directly. The chart is handed
                # to the page as [cm, liters] pairs so the live variance
                # preview can interpolate the same way the server does.
                "has_chart": bool(t.dip_chart_rows),
                "chart": [
                    [r.depth_cm, r.liters]
                    for r in sorted(t.dip_chart_rows, key=lambda r: r.depth_cm)
                ],
            }
        )

    handover_rows = handover_rows_for_date(selected_date)

    fuel_types = FuelType.query.order_by(FuelType.name).all()
    # Price shown/used for selected_date, not necessarily today's current
    # price - see the "price" key on nozzle_rows above for why.
    fuel_prices_by_id = {f.id: price_on_date(f, selected_date) for f in fuel_types}
    # Fuels with no recorded price on/before this date resolve to today's
    # price instead - a silently wrong figure when backfilling an older
    # date, and one that can't be repaired after the fact because each
    # Sale snapshots its own price. Surfaced as a warning on the page.
    fuels_without_price = fuels_missing_price_on(selected_date, fuel_types)

    # Nozzle Meter Readings, grouped by fuel type - see FuelType.entry_mode's
    # docstring in models.py. HISTORICAL-ACCURACY RULE: for a given (fuel
    # type, selected_date), whatever was actually recorded for that date
    # wins over today's setting - if this fuel type's tanks already have
    # Sale rows for selected_date, render meter rows regardless of the
    # fuel type's CURRENT entry_mode; if they have DirectSale rows
    # instead, render direct fields; only a genuinely blank date falls
    # back to entry_mode. This mirrors price_on_date() never using
    # today's price for an old date - a date's own recorded reality
    # always wins over today's settings.
    tanks_by_fuel_id = {}
    for t in tanks:
        tanks_by_fuel_id.setdefault(t.fuel_type_id, []).append(t)
    nozzle_rows_by_fuel_id = {}
    for row in nozzle_rows:
        nozzle_rows_by_fuel_id.setdefault(row["nozzle"].tank.fuel_type_id, []).append(row)

    fuel_groups = []
    for ft in fuel_types:
        ft_tanks = tanks_by_fuel_id.get(ft.id, [])
        if not ft_tanks:
            # A fuel type with no tanks at all has nothing to show either
            # way - skip it rather than rendering an empty, actionless group.
            continue
        ft_tank_ids = [t.id for t in ft_tanks]
        ft_nozzle_rows = nozzle_rows_by_fuel_id.get(ft.id, [])

        has_sale = any(row["existing_reading"] is not None for row in ft_nozzle_rows)
        direct_rows_for_ft = (
            DirectSale.query.filter(
                DirectSale.tank_id.in_(ft_tank_ids),
                DirectSale.entry_date == selected_date,
                DirectSale.shift_id == selected_shift.id,
            ).all()
            if selected_shift
            else []
        )
        if has_sale:
            effective_mode = "meter"
        elif direct_rows_for_ft:
            effective_mode = "direct"
        else:
            effective_mode = ft.entry_mode

        direct_by_tank_id = {d.tank_id: d for d in direct_rows_for_ft}
        # Historical-accuracy rule extended to the combined/per-tank
        # sub-choice, not just meter-vs-direct: once real DirectSale rows
        # exist for this date, ALWAYS show them broken out per tank - never
        # summed into a combined box - regardless of the fuel type's
        # CURRENT direct_entry_combined setting. A combined row is never
        # stored unattributed (see DirectSale's docstring); the per-tank
        # figures on file for this date are the actual ground truth. If
        # this instead showed a combined box pre-filled with their sum,
        # re-submitting that same total would ask
        # split_combined_direct_sale() to re-split it against TODAY's
        # stock - which can differ from what it was when this date was
        # first entered, e.g. if a purchase was backfilled to an earlier
        # date since - silently rewriting how litres were attributed
        # between tanks for an already-reconciled day. The combined
        # convenience only ever applies to a genuinely blank date.
        combined = (
            bool(ft.direct_entry_combined) and len(ft_tanks) > 1 and not direct_rows_for_ft
        )
        direct_tank_rows = [
            {
                "tank": t,
                "existing_liters": direct_by_tank_id[t.id].liters if t.id in direct_by_tank_id else None,
            }
            for t in ft_tanks
        ]
        # Prefill for the combined field: the sum of whatever per-tank
        # DirectSale rows already exist for this date/shift - so revisiting
        # a date to correct it shows what's already there, the same as
        # existing_reading prefills a nozzle row.
        existing_combined_total = (
            round(sum(d.liters for d in direct_rows_for_ft), 2) if direct_rows_for_ft else None
        )

        fuel_groups.append(
            {
                "fuel_type": ft,
                "tanks": ft_tanks,
                "nozzle_rows": ft_nozzle_rows,
                "effective_mode": effective_mode,
                # The toggle control always reflects/acts on the fuel type's
                # CURRENT mode regardless of which date is being viewed -
                # flipping it always affects going forward from the
                # currently-viewed date (see settings_reset_nozzle_meter()'s
                # equivalent reasoning for the meter-reset side of this).
                "current_mode": ft.entry_mode,
                "combined": combined,
                "direct_tank_rows": direct_tank_rows,
                "existing_combined_total": existing_combined_total,
            }
        )

    # Every picker on this page (customer, supplier, employee) lists every
    # account - an account's type label is just a default/hint, not a
    # restriction, so any account can receive any kind of entry (e.g. an
    # account labelled "supplier" can still be given customer credit).
    # Each picker still sorts its own "relevant" type first, though, so
    # the common case doesn't require scrolling past every other type.
    # account_groups below reads every sub-account's .balance, and the
    # template reads more of them - each one walking 12 relationships.
    # Fetched up front that is 12 queries for the page instead of 12 per
    # account; the summing itself is untouched.
    accounts = eager_load(
        eager_load(Account.query, Account, ACCOUNT_TERMS),
        Account, ACCOUNT_TERMS, via=Account.children,
    ).order_by(Account.name).all()
    accounts_customer_first = prioritize_accounts(accounts, "customer")
    accounts_supplier_first = prioritize_accounts(accounts, "supplier")
    accounts_employee_first = prioritize_accounts(accounts, "employee")
    accounts_owner_first = prioritize_accounts(accounts, "owner")
    # Everything the Receipt / Payment-to-Supplier forms need to reshape
    # themselves in place when the picked account turns out to be a parent
    # with sub-accounts. Built here, in one pass over the account list
    # already loaded above, rather than queried per account: Account.children
    # is lazy="selectin", so the whole tree came back with that one query.
    #
    # Only parents appear as keys - the overwhelmingly common childless
    # account is simply absent from the blob, and the form's JS treats
    # "not in here" as "behave exactly as it always has". That is what
    # keeps the ordinary single-amount path untouched.
    account_groups = {
        str(a.id): {
            "name": a.name,
            "children": [
                {"id": c.id, "name": c.name, "balance": c.balance}
                for c in sorted(a.children, key=lambda c: c.name.lower())
            ],
        }
        for a in accounts
        if a.children
    }
    bank_accounts = eager_load(
        BankAccount.query, BankAccount, BANK_TERMS
    ).order_by(BankAccount.name).all()

    # Sorted by category first so the Non-Fuel Sale/Product Purchase
    # pickers group lubricants/filters/shop items into visible blocks, same
    # ordering as the Inventory page's product table.
    products = (
        Product.query.filter_by(is_active=True)
        .order_by(Product.category, func.lower(Product.name))
        .all()
    )
    # One bulk-loaded rate lookup and one grouped stock summary for the
    # WHOLE catalogue - not one query per product - see
    # product_rate_resolver()'s and product_stock_summary()'s docstrings
    # for why an N+1 here would matter on a ~95-SKU catalogue.
    resolve_product_rate = product_rate_resolver(products)
    stock_by_product_id = {
        row["product"].id: row["on_hand"] for row in product_stock_summary(selected_date, products=products)
    }
    product_info = {
        p.id: (*resolve_product_rate(p, selected_date), stock_by_product_id.get(p.id, 0.0))
        for p in products
    }
    # One JSON blob for BOTH product pickers (Non-Fuel Sale, Product
    # Purchase) - the catalogue runs to ~95 SKUs, so a search-as-you-type
    # combobox reads this client-side instead of rendering ~95 <option>
    # nodes into the DOM twice.
    products_json = [
        {
            "id": p.id,
            "label": p.label,
            "category": p.category,
            "purchase": product_info[p.id][0],
            "retail": product_info[p.id][1],
            "stock": product_info[p.id][2],
        }
        for p in products
    ]

    breakdown = sales_breakdown_for_date(selected_date)
    total_sales = breakdown["total"]
    fuel_sales = fuel_sales_for_date(selected_date)

    feed = get_feed_for_date(selected_date, full_visibility=current_user.is_owner)

    summary = None
    cash_balance = None
    bank_balances_by_id = None
    if current_user.is_owner:
        credit_total = breakdown["credit"]
        receipts_total = (
            db.session.query(func.coalesce(func.sum(Receipt.amount), 0))
            .filter(Receipt.entry_date == selected_date)
            .scalar()
        )
        expenses_total = (
            db.session.query(func.coalesce(func.sum(Expense.amount), 0))
            .filter(Expense.entry_date == selected_date)
            .scalar()
        )
        cash_purchases_total = (
            db.session.query(func.coalesce(func.sum(StockPurchase.cost), 0))
            .filter(StockPurchase.entry_date == selected_date, StockPurchase.payment_type == "cash")
            .scalar()
        )
        supplier_payments_total = (
            db.session.query(func.coalesce(func.sum(SupplierPayment.amount), 0))
            .filter(SupplierPayment.entry_date == selected_date)
            .scalar()
        )
        salaries_total = (
            db.session.query(
                func.coalesce(func.sum(SalaryPayment.gross_amount - SalaryPayment.deduction_amount), 0)
            )
            .filter(SalaryPayment.entry_date == selected_date)
            .scalar()
        )
        summary = dict(
            debit_total=total_sales + receipts_total - credit_total,
            credit_total=expenses_total
            + cash_purchases_total
            + supplier_payments_total
            + salaries_total,
        )
        # Date-aware closing balances for selected_date, not the all-time
        # figure cash_account_balance()/BankAccount.balance return - see
        # cash_account_balance_as_of()/bank_account_balance_as_of() in
        # ledger_logic.py. Used here, on the Daily Report, and on the
        # Dashboard; pages with no date to page against (Accounts,
        # Settings, ...) keep showing the current/all-time figure.
        cash_balance = cash_account_balance_as_of(get_cash_account(), selected_date)
        bank_balances_by_id = {b.id: bank_account_balance_as_of(b, selected_date) for b in bank_accounts}

    return render_template(
        "ledger.html",
        selected_date=selected_date,
        today=date.today(),
        shifts=shifts,
        selected_shift=selected_shift,
        nozzle_rows=nozzle_rows,
        fuel_groups=fuel_groups,
        tank_rows=tank_rows,
        handover_rows=handover_rows,
        fuel_types=fuel_types,
        fuel_prices_by_id=fuel_prices_by_id,
        fuels_without_price=fuels_without_price,
        accounts=accounts,
        accounts_customer_first=accounts_customer_first,
        accounts_supplier_first=accounts_supplier_first,
        accounts_employee_first=accounts_employee_first,
        accounts_owner_first=accounts_owner_first,
        account_groups=account_groups,
        bank_accounts=bank_accounts,
        bank_balances_by_id=bank_balances_by_id,
        products=products,
        product_info=product_info,
        products_json=products_json,
        feed=feed,
        total_sales=total_sales,
        fuel_sales=fuel_sales,
        breakdown=breakdown,
        summary=summary,
        cash_balance=cash_balance,
    )


@app.route("/ledger/fuel-price", methods=["POST"])
@login_required
@owner_required
def ledger_fuel_price():
    """Change a fuel's price effective as of whichever date is currently
    selected on the Ledger. Paging back to an earlier date afterward shows
    the price that was in effect then (from FuelPriceHistory), not this
    new one - see price_on_date()."""
    entry_date = parse_date_param(request.form.get("entry_date"))
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    price = request.form.get("price", type=float)
    fuel = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None

    if not fuel:
        flash("Please choose a valid fuel type.", "error")
    elif not price or price <= 0:
        flash("Please enter a valid price.", "error")
    else:
        record_fuel_price(fuel, price, entry_date)
        db.session.commit()
        flash(f"Updated {fuel.name} price to Rs {format_number(price)}/L, effective {entry_date}.", "success")

    return redirect(url_for("ledger", date=entry_date))


def prioritize_accounts(accounts, priority_type):
    """Reorder an already-alphabetical account list so priority_type comes
    first (still alphabetical within each group) - e.g. a Supplier picker
    shows suppliers before customers/employees, without hiding the rest,
    since any account can still receive any kind of entry."""
    primary = [a for a in accounts if a.account_type == priority_type]
    rest = [a for a in accounts if a.account_type != priority_type]
    return primary + rest


def resolve_account(form, id_field, new_name_field, default_type, label, new_phone_field=None):
    """Shared lookup/quick-create for the four account pickers on the
    Ledger (customer/supplier/employee, each just a differently-labelled
    view over the same Account pool). default_type only sets the label on
    a freshly quick-created account - it never restricts what that
    account can later be used for."""
    account_id = form.get(id_field, "")
    if account_id == "__new__":
        name = form.get(new_name_field, "").strip()
        if not name:
            return None, f"Please enter a name for the new {label}."
        phone = form.get(new_phone_field, "").strip() if new_phone_field else ""
        account = Account(name=name, phone=phone or None, account_type=default_type)
        db.session.add(account)
        db.session.flush()
        return account, None

    account = db.session.get(Account, int(account_id)) if account_id else None
    if not account:
        return None, f"Please choose a {label}."
    return account, None


def resolve_customer(form):
    return resolve_account(form, "customer_id", "new_customer_name", "customer", "customer", "new_customer_phone")


def resolve_supplier(form):
    return resolve_account(form, "supplier_id", "new_supplier_name", "supplier", "supplier")


def resolve_employee(form):
    return resolve_account(form, "employee_id", "new_employee_name", "employee", "employee")


def resolve_owner(form):
    return resolve_account(form, "owner_id", "new_owner_name", "owner", "owner")


def resolve_product(form, entry_date, fallback_purchase_rate=None):
    """Shared lookup/quick-create for the Non-Fuel Sale and Product
    Purchase product pickers - same __new__ convention as resolve_account(),
    but a product isn't a bare name like a customer/supplier/employee, so
    quick-creating one reuses settings_add_product()'s full validation
    (including its "retail can't undercut purchase" guard) rather than
    resolve_account()'s couple of lines. Returns (product_or_None,
    error_or_None).

    fallback_purchase_rate lets ledger_product_purchase() hand in the
    purchase's own unit_cost when the inline fieldset (deliberately, see
    that form in ledger.html) doesn't ask for a separate purchase rate -
    the purchase being recorded IS the indent-rate delivery, so asking
    twice would just let the two numbers disagree.

    A product_id that isn't __new__ but doesn't resolve to an ACTIVE
    product is treated as "none chosen" - same as the direct lookups
    ledger_product_sale()/ledger_product_purchase() used to do inline
    before this helper replaced them; a deactivated product can't be
    picked going forward.
    """
    product_id = form.get("product_id", "")
    if product_id != "__new__":
        # Parsed defensively rather than with a bare int(): unlike the
        # account pickers (whose <select> can only ever submit an id the
        # server itself rendered), this arrives from a hidden input driven
        # by the search combobox, so a stale back-button restore or a
        # hand-rolled POST can put anything here - and an unhandled
        # ValueError would be a 500 instead of a flash.
        product = db.session.get(Product, int(product_id)) if product_id.isdigit() else None
        if not product or not product.is_active:
            return None, "Please choose a valid product."
        return product, None

    name = form.get("new_product_name", "").strip()
    if not name:
        return None, "Please enter a name for the new product."

    existing = Product.query.filter(func.lower(Product.name) == name.lower()).first()
    if existing:
        return None, (
            f"A product named \"{existing.name}\" already exists - search for it by "
            "name instead of adding it again."
        )

    category = form.get("new_product_category", "lubricant").strip() or "lubricant"
    if category not in PRODUCT_CATEGORIES:
        category = "lubricant"
    pack_size = form.get("new_product_pack_size", "").strip() or None
    unit = form.get("new_product_unit", "piece").strip() or "piece"
    if unit not in PRODUCT_UNITS:
        unit = "piece"

    purchase_rate = form.get("new_product_purchase_rate", type=float)
    if purchase_rate is None:
        purchase_rate = fallback_purchase_rate
    retail_rate = form.get("new_product_retail_rate", type=float)
    opening_stock = form.get("new_product_opening_stock", type=float)
    if opening_stock is None:
        # Omitted entirely on the Product Purchase form - that purchase
        # itself IS the stock arriving, so there's nothing to set aside
        # as an opening balance.
        opening_stock = 0.0

    if purchase_rate is None or purchase_rate < 0:
        return None, "Please enter a valid purchase rate for the new product."
    if retail_rate is None or retail_rate < 0:
        return None, "Please enter a valid retail rate for the new product."
    if retail_rate < purchase_rate:
        return None, (
            "Retail rate can't be less than the purchase rate - that's a guaranteed "
            "loss and almost always a typo."
        )
    if opening_stock < 0:
        return None, "Please enter a valid opening stock for the new product."

    product = Product(
        name=name,
        category=category,
        pack_size=pack_size,
        unit=unit,
        opening_stock=opening_stock,
        # As-of entry_date, not today - unlike settings_add_product() (which
        # has no entry date to anchor to and so defaults to today), a
        # product quick-created while backfilling an old date has its
        # opening stock dated to THAT date.
        opening_stock_date=entry_date,
        low_stock_threshold=0,
    )
    db.session.add(product)
    db.session.flush()
    # record_product_rates() flushes internally after adding the
    # ProductRateHistory row (verified by reading its source - it needs
    # product.id to resolve product_rates_on_date() against right after),
    # so the caller's very next product_rates_on_date()/product_stock()
    # call already sees this product and its new rate history row with no
    # extra flush needed here.
    record_product_rates(product, purchase_rate, retail_rate, entry_date)
    return product, None


def resolve_bank_account(form, field="bank_account_id", new_field="new_bank_account_name"):
    bank_account_id = form.get(field, "")
    if bank_account_id == "__new__":
        name = form.get(new_field, "").strip()
        if not name:
            return None, "Please enter a name for the new bank account."
        bank_account = BankAccount(name=name)
        db.session.add(bank_account)
        db.session.flush()
        return bank_account, None

    bank_account = db.session.get(BankAccount, int(bank_account_id)) if bank_account_id else None
    if not bank_account:
        return None, "Please choose a bank account."
    return bank_account, None




def _amount_cash_overdraw_error(amount, entry_date):
    """Shared "positive amount, then cash-overdraw" guard for simple new
    cash/bank entries (bank sale, cash deposit): returns the flash error
    text, or None if the amount is valid."""
    if not amount or amount <= 0:
        return "Amount must be a positive number."
    if would_overdraw_cash(amount, entry_date):
        return cash_shortfall_message(entry_date)
    return None


def resolve_return_method(form, field="method"):
    """Refund method for a Sales Return: cash, a specific bank account
    (existing or quick-added inline, same __new__ convention as "Paid
    via"), or "credit" - refunded by reducing what a credit customer
    owes, resolved the same way any other customer picker on the Ledger
    is. Returns (method, bank_account_or_None, account_or_None, error)."""
    value = form.get(field, "cash")
    if value in ("", "cash"):
        return "cash", None, None, None

    if value == "credit":
        account, error = resolve_customer(form)
        return "credit", None, account, error

    if value == "__new__":
        name = form.get("new_bank_account_name", "").strip()
        if not name:
            return None, None, None, "Please enter a name for the new bank account."
        bank_account = BankAccount(name=name)
        db.session.add(bank_account)
        db.session.flush()
        return "bank", bank_account, None, None

    bank_account = db.session.get(BankAccount, int(value))
    if not bank_account:
        return None, None, None, "Please choose a valid refund method."
    return "bank", bank_account, None, None


@app.route("/ledger/readings", methods=["POST"])
@login_required
def ledger_readings():
    entry_date = parse_date_param(request.form.get("entry_date"))
    shift = resolve_shift(request.form)
    nozzles = Nozzle.query.all()
    saved = 0
    errors = []

    for nozzle in nozzles:
        raw = request.form.get(f"reading_{nozzle.id}", "").strip()
        if not raw:
            continue
        try:
            current_reading = float(raw)
        except ValueError:
            errors.append(f"{nozzle.label}: not a valid number.")
            continue

        auto_previous, is_auto = previous_reading_for(nozzle, entry_date, shift)
        backfill_prior = None

        if is_auto:
            previous = auto_previous
        else:
            raw_previous = request.form.get(f"previous_{nozzle.id}", "").strip()
            if not raw_previous:
                errors.append(
                    f"{nozzle.label}: there's no entry for the slot before this one, so "
                    f"please enter both the previous and current reading."
                )
                continue
            try:
                previous = float(raw_previous)
            except ValueError:
                errors.append(f"{nozzle.label}: previous reading is not a valid number.")
                continue

            floor = nearest_earlier_reading(nozzle, entry_date, shift)
            if previous < floor:
                errors.append(
                    f"{nozzle.label}: previous reading ({previous:g}) can't be lower than an "
                    f"earlier recorded reading ({floor:g})."
                )
                continue

            # Close the gap: the immediately preceding slot gets a Sale of
            # its own, using the previous reading just typed in as ITS
            # current reading - but only when that slot is still empty
            # (never overwrite a real entry) and only when ITS own previous
            # reading can be determined automatically. A deeper gap is left
            # for the user to fill in when they visit those slots directly,
            # rather than guessing on their behalf.
            prior_date, prior_shift = previous_slot(entry_date, shift)
            if prior_shift is not None and not Sale.query.filter_by(
                nozzle_id=nozzle.id, entry_date=prior_date, shift_id=prior_shift.id
            ).first():
                prior_previous, prior_is_auto = previous_reading_for(nozzle, prior_date, prior_shift)
                if prior_is_auto:
                    backfill_prior = {
                        "entry_date": prior_date,
                        "shift_id": prior_shift.id,
                        "previous_reading": prior_previous,
                        "current_reading": previous,
                    }

        if current_reading < previous:
            errors.append(
                f"{nozzle.label}: reading ({current_reading:g}) is less than the previous "
                f"reading ({previous:g})."
            )
            continue

        next_sale = next_sale_on_or_after(nozzle.id, entry_date, shift)
        if next_sale and current_reading > next_sale.current_reading:
            errors.append(
                f"{nozzle.label}: reading ({current_reading:g}) is more than a later reading "
                f"already recorded on {next_sale.entry_date} ({next_sale.current_reading:g})."
            )
            continue

        gross = round(current_reading - previous, 2)

        # liters is written provisionally as the full gross meter
        # difference, testing_liters=0 - sync_sale_testing() below then
        # carves out whatever's already on file in NozzleTesting for this
        # slot (recorded via the Sales Return/Testing section, in either
        # order relative to this reading) and overwrites
        # liters/testing_liters/total_amount to match (see Sale's
        # docstring in models.py and NozzleTesting's in the same file).
        liters = gross
        existing = Sale.query.filter_by(
            nozzle_id=nozzle.id, entry_date=entry_date, shift_id=shift.id
        ).first()
        # A day that's pure testing nets to liters == 0 once synced, but
        # is still a real record - only the true no-op (nothing typed, no
        # existing row, no gap to backfill) gets skipped. liters == gross
        # here (testing hasn't been subtracted yet), so "liters == 0 and
        # testing == 0" reduces to exactly "gross == 0".
        if gross == 0 and not existing and not backfill_prior:
            continue

        # Price as of entry_date, not today's current price - so backfilling
        # or correcting an old date re-prices at the rate that was actually
        # in effect then, never at whatever the price happens to be today.
        price = price_on_date(nozzle.fuel_type, entry_date)
        total_amount = round(liters * price, 2)

        if backfill_prior:
            bf_price = price_on_date(nozzle.fuel_type, backfill_prior["entry_date"])
            bf_liters = round(
                backfill_prior["current_reading"] - backfill_prior["previous_reading"], 2
            )
            db.session.add(
                Sale(
                    nozzle_id=nozzle.id,
                    shift_id=backfill_prior["shift_id"],
                    entry_date=backfill_prior["entry_date"],
                    previous_reading=backfill_prior["previous_reading"],
                    current_reading=backfill_prior["current_reading"],
                    liters=bf_liters,
                    # Provisional, same as the main slot below - a testing
                    # entry could already exist for this earlier slot (it
                    # can be recorded in either order), and the
                    # sync_sale_testing() call after the flush will fold
                    # it in if so.
                    testing_liters=0,
                    price_per_liter=bf_price,
                    total_amount=round(bf_liters * bf_price, 2),
                    user_id=current_user.id,
                )
            )

        if existing:
            existing.previous_reading = previous
            existing.current_reading = current_reading
            existing.liters = liters
            existing.testing_liters = 0
            existing.price_per_liter = price
            existing.total_amount = total_amount
            existing.user_id = current_user.id
        else:
            db.session.add(
                Sale(
                    nozzle_id=nozzle.id,
                    shift_id=shift.id,
                    entry_date=entry_date,
                    previous_reading=previous,
                    current_reading=current_reading,
                    liters=liters,
                    testing_liters=0,
                    price_per_liter=price,
                    total_amount=total_amount,
                    user_id=current_user.id,
                )
            )
        saved += 1

        # Flush so both Sale rows just added/updated above are visible to
        # sync_sale_testing()'s own queries, then reconcile each slot
        # against whatever NozzleTesting rows already exist for it -
        # re-saving a reading must never wipe testing already on file.
        db.session.flush()
        if backfill_prior:
            _, bf_over_by = sync_sale_testing(
                nozzle.id, backfill_prior["entry_date"], backfill_prior["shift_id"]
            )
            if bf_over_by:
                errors.append(
                    f"{nozzle.label}: testing already recorded for {backfill_prior['entry_date']} "
                    f"exceeds that slot's meter difference by {bf_over_by:g} L - clamped so the "
                    f"sale doesn't go negative."
                )
        _, over_by = sync_sale_testing(nozzle.id, entry_date, shift.id)
        if over_by:
            errors.append(
                f"{nozzle.label}: testing already recorded for this slot exceeds the meter "
                f"difference by {over_by:g} L - clamped so the sale doesn't go negative."
            )

    if saved:
        try:
            db.session.commit()
            flash(f"Saved {saved} nozzle reading(s) for {entry_date} ({shift.name}).", "success")
        except IntegrityError:
            # Two near-simultaneous submits for the same nozzle/date/shift
            # (a double-tap, a network retry) - the DB's own uniqueness
            # constraint caught it. Fail safely instead of creating a
            # duplicate Sale that would silently double-count the day.
            db.session.rollback()
            flash(
                "Someone else just saved a reading for this date at the same time - "
                "please check the entries below and try again.",
                "error",
            )
    if errors:
        for e in errors:
            flash(e, "error")
    if not saved and not errors:
        flash("No new readings entered.", "error")

    return redirect(url_for("ledger", date=entry_date, shift=shift.id))


def resolve_shift(form, field="shift_id"):
    """The shift an entry belongs to. Falls back to the default (first
    active) shift so a single-shift pump never has to send this field at
    all, and so an entry can't end up shiftless."""
    shift_id = form.get(field, type=int)
    shift = db.session.get(Shift, shift_id) if shift_id else None
    return shift or default_shift()


@app.route("/ledger/direct-sale", methods=["POST"])
@login_required
def ledger_direct_sale():
    """Total-liters-per-tank alternative to nozzle meter readings - see
    DirectSale's docstring in models.py. NOT owner-only: staff already
    enter nozzle readings today, so direct entry has to be just as
    available to them.

    Accepts either one liters_<tank_id> field per tank (the default,
    fuel_type.direct_entry_combined == False, or a single-tank fuel type
    where the flag is never consulted at all), or one combined_liters
    field for the whole fuel type (fuel_type.direct_entry_combined ==
    True on a multi-tank fuel type), which split_combined_direct_sale()
    then splits into real per-tank rows before anything is saved - so
    every downstream consumer only ever sees ordinary per-tank DirectSale
    rows, never an unattributed fuel-type-level figure."""
    entry_date = parse_date_param(request.form.get("entry_date"))
    shift = resolve_shift(request.form)
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    fuel_type = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None

    if not fuel_type:
        flash("Please choose a valid fuel type.", "error")
        return redirect(url_for("ledger", date=entry_date, shift=shift.id))

    tanks = Tank.query.filter_by(fuel_type_id=fuel_type.id).order_by(Tank.number).all()
    if not tanks:
        flash(f"{fuel_type.name} has no tanks configured.", "error")
        return redirect(url_for("ledger", date=entry_date, shift=shift.id))

    # Price as of entry_date, not today's current price - so backfilling
    # or correcting an old date re-prices at the rate that was actually in
    # effect then, exactly like nozzle readings already do.
    price = price_on_date(fuel_type, entry_date)
    combine = fuel_type.direct_entry_combined and len(tanks) > 1

    liters_by_tank_id = {}
    errors = []
    if combine:
        raw = request.form.get("combined_liters", "").strip()
        if not raw:
            flash(f"Please enter total litres sold for {fuel_type.name}.", "error")
            return redirect(url_for("ledger", date=entry_date, shift=shift.id))
        try:
            total_liters = float(raw)
        except ValueError:
            flash(f"{fuel_type.name}: not a valid number.", "error")
            return redirect(url_for("ledger", date=entry_date, shift=shift.id))
        if total_liters <= 0:
            flash(f"{fuel_type.name}: litres must be a positive number.", "error")
            return redirect(url_for("ledger", date=entry_date, shift=shift.id))
        liters_by_tank_id = split_combined_direct_sale(tanks, total_liters, entry_date)
    else:
        any_given = False
        for t in tanks:
            raw = request.form.get(f"liters_{t.id}", "").strip()
            if not raw:
                continue
            try:
                liters = float(raw)
            except ValueError:
                errors.append(f"{t.label}: not a valid number.")
                continue
            if liters <= 0:
                errors.append(f"{t.label}: litres must be a positive number.")
                continue
            liters_by_tank_id[t.id] = liters
            any_given = True
        if not any_given and not errors:
            flash("No litres entered.", "error")
            return redirect(url_for("ledger", date=entry_date, shift=shift.id))

    saved = 0
    for t in tanks:
        liters = liters_by_tank_id.get(t.id)
        if liters is None:
            continue
        total_amount = round(liters * price, 2)
        existing = DirectSale.query.filter_by(
            tank_id=t.id, entry_date=entry_date, shift_id=shift.id
        ).first()
        if existing:
            existing.liters = liters
            existing.price_per_liter = price
            existing.total_amount = total_amount
            existing.user_id = current_user.id
        else:
            db.session.add(
                DirectSale(
                    tank_id=t.id,
                    shift_id=shift.id,
                    entry_date=entry_date,
                    liters=liters,
                    price_per_liter=price,
                    total_amount=total_amount,
                    user_id=current_user.id,
                )
            )
        saved += 1

    if saved:
        try:
            db.session.commit()
            flash(f"Saved direct sales entry for {fuel_type.name} ({entry_date}, {shift.name}).", "success")
        except IntegrityError:
            # Two near-simultaneous submits for the same tank/date/shift -
            # the DB's own uniqueness constraint caught it. Fail safely
            # instead of creating a duplicate DirectSale that would
            # silently double-count the day, mirroring ledger_readings().
            db.session.rollback()
            flash(
                "Someone else just saved this entry at the same time - "
                "please check the entries below and try again.",
                "error",
            )
    if errors:
        for e in errors:
            flash(e, "error")

    return redirect(url_for("ledger", date=entry_date, shift=shift.id))


@app.route("/ledger/fuel-type/<int:fuel_type_id>/entry-mode", methods=["POST"])
@login_required
@owner_required
def ledger_fuel_type_entry_mode(fuel_type_id):
    """Flip a fuel type between Nozzle Meter Readings and Direct Sales
    Entry - see FuelType.entry_mode's docstring in models.py. Owner-only:
    this is a structural change to how the pump tracks fuel, not routine
    data entry.

    Meter -> Direct: no side effects. Existing Sale rows are untouched and
    remain correct on whatever past dates they belong to.

    Direct -> Meter: the meter-reading chain was broken while direct entry
    was active (no nozzle readings were taken), so every nozzle on every
    tank of this fuel type gets a NozzleReset dated to whichever Ledger
    date the owner was viewing when they flipped it - reusing the exact
    existing mechanism settings_reset_nozzle_meter() uses, which makes
    previous_reading_for()/nearest_earlier_reading()/next_sale_on_or_after()
    (ledger_logic.py) stop enforcing continuity across the boundary and
    require a fresh manual previous+current entry, precisely the behaviour
    wanted here."""
    fuel_type = db.session.get(FuelType, fuel_type_id) or abort(404)
    target_mode = request.form.get("target_mode")
    selected_date = parse_date_param(request.form.get("selected_date"))
    combined_raw = request.form.get("direct_entry_combined")

    if target_mode not in ("meter", "direct"):
        flash("Please choose a valid entry mode.", "error")
        return redirect(url_for("ledger", date=selected_date))

    changed = False
    if target_mode != fuel_type.entry_mode:
        if target_mode == "meter":
            tanks = Tank.query.filter_by(fuel_type_id=fuel_type.id).all()
            tank_ids = [t.id for t in tanks]
            nozzles = Nozzle.query.filter(Nozzle.tank_id.in_(tank_ids)).all() if tank_ids else []
            for n in nozzles:
                db.session.add(
                    NozzleReset(
                        nozzle_id=n.id,
                        reset_date=selected_date,
                        note=f"Auto-reset: {fuel_type.name} switched back from Direct Sales Entry",
                        user_id=current_user.id,
                    )
                )
            fuel_type.entry_mode = "meter"
        else:
            fuel_type.entry_mode = "direct"
        changed = True

    # direct_entry_combined is only ever meaningful in direct mode - only
    # relevant when turning direct mode ON for a multi-tank fuel type, or
    # (as a small usability extension beyond that) when already in direct
    # mode and the owner wants to change how it splits going forward.
    if target_mode == "direct" and combined_raw is not None:
        tank_count = Tank.query.filter_by(fuel_type_id=fuel_type.id).count()
        if tank_count > 1:
            new_combined = combined_raw in ("1", "true", "on")
            if new_combined != fuel_type.direct_entry_combined:
                fuel_type.direct_entry_combined = new_combined
                changed = True

    if changed:
        db.session.commit()
        mode_label = "Direct Sales Entry" if fuel_type.entry_mode == "direct" else "Nozzle Meter Readings"
        flash(f"{fuel_type.name} is now tracked via {mode_label}.", "success")
    else:
        db.session.rollback()
        flash("No change made.", "info")

    return redirect(url_for("ledger", date=selected_date))


@app.route("/ledger/dip", methods=["POST"])
@login_required
def ledger_dip():
    entry_date = parse_date_param(request.form.get("entry_date"))
    tanks = Tank.query.all()
    saved = 0

    for tank in tanks:
        # A tank with a calibration chart is measured in cm (what a dip
        # stick actually reads) and converted; one without keeps taking
        # liters directly.
        has_chart = bool(tank.dip_chart_rows)
        field = f"dipcm_{tank.id}" if has_chart else f"dip_{tank.id}"
        raw = request.form.get(field, "").strip()
        if not raw:
            continue
        try:
            entered = float(raw)
        except ValueError:
            flash(f"Tank {tank.number}: not a valid number.", "error")
            continue
        if entered < 0:
            flash(f"Tank {tank.number}: dip must be zero or more.", "error")
            continue

        if has_chart:
            dip_cm = entered
            dip_value = liters_from_dip_cm(tank, entered)
            if dip_value is None:
                flash(f"Tank {tank.number}: no dip chart found.", "error")
                continue
        else:
            dip_cm = None
            dip_value = entered

        # Purely diagnostic - a stick measurement in cm regardless of
        # whether the tank's own dip above is in cm or liters (see
        # TankDip.water_cm in models.py). Blank stays None rather than 0,
        # so "not measured" is distinct from "measured, none found".
        raw_water = request.form.get(f"water_{tank.id}", "").strip()
        water_cm = None
        if raw_water:
            try:
                water_cm = float(raw_water)
            except ValueError:
                flash(f"Tank {tank.number}: water level is not a valid number.", "error")
                continue
            if water_cm < 0:
                flash(f"Tank {tank.number}: water level must be zero or more.", "error")
                continue

        existing = TankDip.query.filter_by(tank_id=tank.id, entry_date=entry_date).first()
        if existing:
            existing.dip_cm = dip_cm
            existing.dip_liters = dip_value
            existing.water_cm = water_cm
            existing.user_id = current_user.id
        else:
            db.session.add(
                TankDip(
                    tank_id=tank.id,
                    entry_date=entry_date,
                    dip_cm=dip_cm,
                    dip_liters=dip_value,
                    water_cm=water_cm,
                    user_id=current_user.id,
                )
            )
        saved += 1

    if saved:
        try:
            db.session.commit()
            flash(f"Saved {saved} dip reading(s) for {entry_date}.", "success")
        except IntegrityError:
            db.session.rollback()
            flash(
                "Someone else just saved a dip reading for this date at the same time - "
                "please check the entries below and try again.",
                "error",
            )
    else:
        flash("No dip readings entered.", "error")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/handover", methods=["POST"])
@login_required
def ledger_handover():
    """Record what was physically counted at the end of a shift. Purely a
    reconciliation record - see CashHandover in models.py for why it
    deliberately doesn't move any money itself."""
    entry_date = parse_date_param(request.form.get("entry_date"))
    shift = resolve_shift(request.form)
    declared = request.form.get("declared_amount", type=float)
    attendant_id = request.form.get("attendant_id", type=int)
    note = request.form.get("note", "").strip()
    attendant = db.session.get(Account, attendant_id) if attendant_id else None

    if declared is None or declared < 0:
        flash("Please enter the cash counted (zero or more).", "error")
    else:
        existing = CashHandover.query.filter_by(entry_date=entry_date, shift_id=shift.id).first()
        if existing:
            existing.declared_amount = declared
            existing.attendant_id = attendant.id if attendant else None
            existing.note = note or None
            existing.user_id = current_user.id
        else:
            db.session.add(
                CashHandover(
                    entry_date=entry_date,
                    shift_id=shift.id,
                    attendant_id=attendant.id if attendant else None,
                    declared_amount=declared,
                    note=note or None,
                    user_id=current_user.id,
                )
            )
        try:
            db.session.commit()
            expected = sales_breakdown_for_date(entry_date, shift_id=shift.id)["cash"]
            variance = round(declared - expected, 2)
            if abs(variance) < 0.01:
                flash(f"{shift.name}: cash counted matches the ledger exactly.", "success")
            elif variance < 0:
                flash(
                    f"{shift.name}: short by Rs {format_number(abs(variance))} "
                    f"(expected Rs {format_number(expected)}, counted Rs {format_number(declared)}).",
                    "error",
                )
            else:
                flash(
                    f"{shift.name}: over by Rs {format_number(variance)} "
                    f"(expected Rs {format_number(expected)}, counted Rs {format_number(declared)}).",
                    "error",
                )
        except IntegrityError:
            db.session.rollback()
            flash("That shift was just reconciled by someone else - please reload and check.", "error")

    return redirect(url_for("ledger", date=entry_date, shift=shift.id))


@app.route("/ledger/handover/<int:handover_id>/write-off", methods=["POST"])
@login_required
@owner_required
def ledger_handover_write_off(handover_id):
    """Turn a confirmed shortfall into a real Expense, which is what
    actually moves cash-in-hand down to match what's physically there.
    Kept as a separate deliberate step rather than automatic, because
    absorbing a shortfall and recovering it from the attendant are
    different decisions with different bookkeeping."""
    handover = db.session.get(CashHandover, handover_id) or abort(404)
    expected = sales_breakdown_for_date(handover.entry_date, shift_id=handover.shift_id)["cash"]
    shortfall = round(expected - handover.declared_amount, 2)

    if shortfall <= 0.01:
        flash("That shift isn't short, so there's nothing to write off.", "error")
    elif would_overdraw_cash(shortfall, handover.entry_date):
        flash(cash_shortfall_message(handover.entry_date), "error")
    else:
        who = f" ({handover.attendant.name})" if handover.attendant else ""
        db.session.add(
            Expense(
                entry_date=handover.entry_date,
                category="Cash Shortfall",
                description=f"{handover.shift.name} shift shortfall{who}",
                amount=shortfall,
                method="cash",
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded Rs {format_number(shortfall)} shortfall as a cash expense.", "success")

    return redirect(url_for("ledger", date=handover.entry_date, shift=handover.shift_id))


@app.route("/ledger/salary", methods=["POST"])
@login_required
@owner_required
def ledger_salary():
    """Pay an employee's salary for a period, optionally withholding part of
    it against an advance they already owe - see SalaryPayment in models.py
    for how gross/deduction/net split across the books."""
    entry_date = parse_date_param(request.form.get("entry_date"))
    employee, error = resolve_employee(request.form)
    gross = request.form.get("gross_amount", type=float)
    deduction = request.form.get("deduction_amount", type=float) or 0
    period_label = request.form.get("period_label", "").strip()
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)
    net = round((gross or 0) - deduction, 2)
    outstanding = employee.balance if employee else 0

    if error:
        db.session.rollback()
        flash(error, "error")
    elif not gross or gross <= 0:
        db.session.rollback()
        flash("Salary amount must be a positive number.", "error")
    elif deduction < 0:
        db.session.rollback()
        flash("Deduction can't be negative.", "error")
    elif deduction > gross:
        db.session.rollback()
        flash("Deduction can't be more than the salary itself.", "error")
    elif deduction > outstanding + 0.01:
        db.session.rollback()
        flash(
            f"{employee.name} only owes Rs {format_number(max(outstanding, 0))}, so you can't deduct "
            f"Rs {format_number(deduction)}.",
            "error",
        )
    elif method_error:
        db.session.rollback()
        flash(method_error, "error")
    elif method == "cash" and would_overdraw_cash(net, entry_date):
        db.session.rollback()
        flash(cash_shortfall_message(entry_date), "error")
    else:
        db.session.add(
            SalaryPayment(
                account_id=employee.id,
                entry_date=entry_date,
                period_label=period_label or None,
                gross_amount=gross,
                deduction_amount=deduction,
                method=method,
                bank_account_id=bank_account.id if bank_account else None,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        if deduction:
            flash(
                f"Paid {employee.name} Rs {format_number(net)} (Rs {format_number(gross)} salary less "
                f"Rs {format_number(deduction)} against their advance).",
                "success",
            )
        else:
            flash(f"Paid {employee.name} Rs {format_number(net)} salary.", "success")

    return redirect(url_for("ledger", date=entry_date))


def resolve_receipt_account(form):
    return resolve_account(form, "account_id", "new_account_name", "customer", "account", "new_account_phone")


def resolve_group_allocation(parent, form, entry_date, amount, direction="receivable"):
    """Work out how one payment recorded against a PARENT account should be
    split across its sub-accounts. Returns (allocations, total, error) where
    allocations is the [{"account": <Account>, "amount": float}] shape
    allocate_group_payment() already produces, so both callers can turn it
    into ordinary per-child rows without knowing which mode produced it.

    This decides only the split - it creates no rows and moves no money. The
    caller writes one perfectly ordinary Receipt / SupplierPayment per
    allocation, exactly the row that typing that payment in by hand on that
    sub-account would have created, so every balance, statement and report
    downstream keeps seeing plain entries and needs to know nothing at all
    about groups.

    Two modes, from the form's alloc_mode field:

    - "auto" (default): one lump sum, split oldest-debt-first by
      allocate_group_payment() (ledger_logic.py). The lump sum is the
      form's ordinary single Amount field.
    - "manual": one figure typed per sub-account, in sub_amount_<child_id>
      fields. Blank or 0 means "this sub-account is not part of this
      payment" - that is the whole of the partial-group case, which is why
      there is no separate tick-box step. The total is whatever the typed
      figures add up to; the single Amount field is not used at all.

    OVER-PAYMENT IS REFUSED IN BOTH MODES, never absorbed. In auto mode
    that is allocate_group_payment()'s leftover; in manual mode it is the
    per-child check that no typed figure exceeds that child's own balance.
    They are the same rule seen from two angles: this app never silently
    invents an advance or a credit balance on an account nobody chose. An
    owner who genuinely wants to overpay records a direct entry on that
    one sub-account, where the resulting negative balance is an explicit,
    visible decision.

    DELIBERATE: a parent that carries its OWN direct balance as well as
    having children is not paid down by this at all - in either mode the
    money goes only to the sub-accounts. A parent's own balance is settled
    by picking... nothing else; there is no way to reach it from here,
    because a form that sometimes silently paid the parent and sometimes
    the children would be unreadable. To settle a parent's own balance,
    move its sub-accounts out first, or record against a sub-account.

    Sub-accounts are one level deep by construction (see _validate_parent),
    so there is no recursion to do here.

    DIRECTION. Account.balance is positive when the account owes the pump
    and negative when the pump owes the account, so "how much can this
    child absorb" is read off the opposite sign on the two forms. The
    Receipt form takes the default direction="receivable"; the
    Payment-to-Supplier form passes direction="payable", where a child's
    outstanding figure - and therefore the manual-mode per-child cap - is
    `-balance`. Both modes must agree on this or the two over-payment
    refusals would disagree with each other: capping a supplier against
    its raw (negative) balance refuses every payment, which is the bug
    this parameter exists to fix.
    """
    children = sorted(parent.children, key=lambda c: c.name.lower())
    alloc_mode = form.get("alloc_mode", "auto")
    payable = direction == "payable"

    def outstanding_for(child):
        """What this child can absorb in THIS direction, as a positive
        number - always 0 or more, so a wrong-signed child simply caps at
        zero and takes no part in the payment."""
        balance = child.balance
        return max(round(-balance if payable else balance, 2), 0.0)

    if alloc_mode != "manual":
        if not amount or amount <= 0:
            return None, 0.0, "Amount must be a positive number."
        allocations, leftover = allocate_group_payment(children, amount, entry_date, direction=direction)
        if leftover > 0.01:
            owed = round(amount - leftover, 2)
            direction_phrase = "owed to" if payable else "owed across"
            return None, 0.0, (
                f"Rs {format_number(amount)} is more than the Rs {format_number(owed)} "
                f"currently {direction_phrase} {parent.name}'s sub-accounts."
            )
        return allocations, round(amount, 2), None

    children_by_id = {c.id: c for c in children}
    typed = {}
    for key in form:
        if not key.startswith("sub_amount_"):
            continue
        try:
            child_id = int(key[len("sub_amount_") :])
        except ValueError:
            child_id = None
        # Not silently dropped: a posted field naming something that isn't
        # a sub-account of THIS parent means the form was tampered with or
        # is stale against a regrouping that happened in another tab, and
        # quietly ignoring it would record a payment whose shape the owner
        # did not actually authorise.
        if child_id is None or child_id not in children_by_id:
            return None, 0.0, f"One of the submitted sub-accounts doesn't belong to {parent.name}."
        typed[child_id] = form.get(key, "").strip()

    allocations = []
    # Iterate children (not the form) so the resulting rows land in a
    # stable, name-sorted order regardless of how the browser serialised
    # the fields.
    for child in children:
        raw = typed.get(child.id, "")
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            return None, 0.0, f"\"{raw}\" is not a valid amount for {child.name}."
        if value == 0:
            continue
        if value < 0:
            return None, 0.0, f"The amount for {child.name} can't be negative."
        # The manual-mode equivalent of auto mode's leftover check, read
        # in whichever direction this form is running in.
        cap = outstanding_for(child)
        if value > cap + 0.01:
            owes_phrase = "is currently owed" if payable else "currently owes"
            return None, 0.0, (
                f"Rs {format_number(value)} is more than the Rs {format_number(cap)} "
                f"{child.name} {owes_phrase}."
            )
        allocations.append({"account": child, "amount": round(value, 2)})

    if not allocations:
        return None, 0.0, "Enter an amount for at least one sub-account."

    return allocations, round(sum(a["amount"] for a in allocations), 2), None


def group_allocation_summary(parent, total, allocations, verb):
    """The flash line for a completed group payment - names every
    sub-account and its share, because the whole point of the group form
    is that the owner typed one number and the app decided several, and a
    decision the app made on the owner's behalf has to be shown back."""
    parts = ", ".join(f"{a['account'].name} Rs {format_number(a['amount'])}" for a in allocations)
    return (
        f"{verb} Rs {format_number(total)} across {len(allocations)} of "
        f"{parent.name}'s sub-account(s): {parts}."
    )


@app.route("/ledger/receipt", methods=["POST"])
@login_required
def ledger_receipt():
    """Money received from an account. When the picked account is an
    ordinary (childless) one - which is every account unless someone has
    deliberately grouped some - this behaves exactly as it always has: one
    amount, one Receipt row, nothing else consulted.

    When the picked account is a PARENT with sub-accounts, the same form
    instead records one ordinary Receipt per sub-account, split by
    resolve_group_allocation() (see its docstring for the two modes and
    for why an over-payment is refused rather than absorbed). Nothing new
    is stored either way - a group payment is just several plain receipts
    written in one go.
    """
    entry_date = parse_date_param(request.form.get("entry_date"))
    account, error = resolve_receipt_account(request.form)
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)

    if error:
        db.session.rollback()
        flash(error, "error")
    elif account.children:
        allocations, total, group_error = resolve_group_allocation(
            account, request.form, entry_date, amount
        )
        if group_error:
            # resolve_payment_method() can db.session.add() a quick-added
            # bank account, so every rejection rolls back rather than
            # leaving that half-made row sitting in the session.
            db.session.rollback()
            flash(group_error, "error")
        elif method_error:
            db.session.rollback()
            flash(method_error, "error")
        else:
            for allocation in allocations:
                db.session.add(
                    Receipt(
                        account_id=allocation["account"].id,
                        entry_date=entry_date,
                        amount=allocation["amount"],
                        method=method,
                        bank_account_id=bank_account.id if bank_account else None,
                        note=note or None,
                        user_id=current_user.id,
                    )
                )
            db.session.commit()
            flash(group_allocation_summary(account, total, allocations, "Recorded"), "success")
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Amount must be a positive number.", "error")
    elif method_error:
        db.session.rollback()
        flash(method_error, "error")
    else:
        db.session.add(
            Receipt(
                account_id=account.id,
                entry_date=entry_date,
                amount=amount,
                method=method,
                bank_account_id=bank_account.id if bank_account else None,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded receipt of Rs {format_number(amount)} from {account.name}.", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/credit", methods=["POST"])
@login_required
def ledger_credit():
    """Fuel already sold (see CreditGiven's docstring in models.py) handed
    to a customer on account instead of collected as cash. entry_mode
    decides which of liters/amount is what the user actually typed
    (primary) versus its computed equivalent (secondary) - and the
    secondary side can itself be overridden downward to record a
    discretionary discount, e.g. lower the litres billed in "By Amount"
    mode, or lower the amount billed in "By Litres" mode:

    - "liters" (default): liters is the primary figure. amount defaults to
      liters * price, but the submitted "amount" field (pre-filled with
      that default, editable client-side) is taken as authoritative if
      present - so amount can be reduced below liters * price to bill the
      customer a negotiated, lower total.
    - "amount": amount is the primary figure, taken exactly as typed. The
      submitted "liters" field defaults to amount / price but can be
      reduced client-side, e.g. to bill the full amount while crediting
      only part of the litres.

    price_per_liter stored is always this date's default price regardless
    of mode or override - it's never itself overridden. The discount lives
    entirely in the gap between amount and liters * price_per_liter, which
    is also the signature reprice_entries() (ledger_logic.py) uses to
    detect a deliberately-discounted row and leave it alone.

    Fuel type is required in both modes - price_on_date() needs it
    regardless of which direction the calculation runs.
    """
    entry_date = parse_date_param(request.form.get("entry_date"))
    customer, error = resolve_customer(request.form)
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    entry_mode = _resolve_entry_mode(request.form)
    liters_in = request.form.get("liters", type=float)
    amount_in = request.form.get("amount", type=float)
    vehicle_number = request.form.get("vehicle_number", "").strip()
    note = request.form.get("note", "").strip()
    shift = resolve_shift(request.form)

    fuel = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None

    if error:
        db.session.rollback()
        flash(error, "error")
    elif not fuel:
        db.session.rollback()
        flash("Please choose a valid fuel type.", "error")
    elif (amount_error := _credit_amount_error(fuel, entry_date, entry_mode, liters_in, amount_in)):
        db.session.rollback()
        flash(amount_error, "error")
    else:
        price = price_on_date(fuel, entry_date)
        liters, amount = _derive_credit_liters_amount(entry_mode, liters_in, amount_in, price)
        db.session.add(
            CreditGiven(
                account_id=customer.id,
                fuel_type_id=fuel.id,
                shift_id=shift.id,
                entry_date=entry_date,
                liters=liters,
                price_per_liter=price,
                amount=amount,
                vehicle_number=vehicle_number or None,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        if entry_mode == "amount":
            flash(
                f"Recorded Rs {format_number(amount)} ({liters:g} L equiv.) {fuel.name} on credit for {customer.name}.",
                "success",
            )
        else:
            flash(
                f"Recorded {liters:g} L {fuel.name} (Rs {format_number(amount)}) on credit for {customer.name}.",
                "success",
            )

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/sales-return", methods=["POST"])
@login_required
def ledger_sales_return():
    """Fuel a customer physically brings back into a tank, refunded to
    them - distinct from Testing (NozzleTesting in models.py), which
    never involved a customer at all and moves no money. Not owner-only:
    staff take these at the forecourt the same as any other sale-side
    entry."""
    entry_date = parse_date_param(request.form.get("entry_date"))
    shift = resolve_shift(request.form)
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    tank_id = request.form.get("tank_id", type=int)
    liters = request.form.get("liters", type=float)
    note = request.form.get("note", "").strip()
    fuel = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None
    tank = db.session.get(Tank, tank_id) if tank_id else None
    method, bank_account, account, method_error = resolve_return_method(request.form)

    if not fuel:
        db.session.rollback()
        flash("Please choose a valid fuel type.", "error")
    elif not tank:
        db.session.rollback()
        flash("Please choose which tank the fuel goes back into.", "error")
    elif not liters or liters <= 0:
        db.session.rollback()
        flash("Liters must be a positive number.", "error")
    elif method_error:
        db.session.rollback()
        flash(method_error, "error")
    else:
        # Priced from the date's own history, same as any other sale, so
        # a return of fuel sold weeks ago refunds the rate actually
        # charged then - not today's rate.
        price = price_on_date(fuel, entry_date)
        amount = round(liters * price, 2)
        if method == "cash" and would_overdraw_cash(amount, entry_date):
            db.session.rollback()
            flash(cash_shortfall_message(entry_date), "error")
        else:
            db.session.add(
                SalesReturn(
                    entry_date=entry_date,
                    shift_id=shift.id,
                    fuel_type_id=fuel.id,
                    tank_id=tank.id,
                    liters=liters,
                    price_per_liter=price,
                    amount=amount,
                    method=method,
                    bank_account_id=bank_account.id if bank_account else None,
                    account_id=account.id if account else None,
                    note=note or None,
                    user_id=current_user.id,
                )
            )
            db.session.commit()
            flash(
                f"Recorded return of {liters:g} L {fuel.name} into {tank.label} (Rs {format_number(amount)}).",
                "success",
            )

    return redirect(url_for("ledger", date=entry_date, shift=shift.id))


@app.route("/ledger/testing", methods=["POST"])
@login_required
def ledger_testing():
    """Fuel run through a nozzle to test it - its own NozzleTesting row
    rather than a field on the meter reading itself, so it can be entered
    in either order relative to that reading and deleted on its own (see
    NozzleTesting's docstring in models.py). Not owner-only: staff do this
    the same as they record readings and sales returns.

    No would_overdraw_cash() guard, deliberately: testing moves no money
    at all - it only reduces revenue the exact same way typing in a
    smaller meter reading would, and ledger_readings() has no cash guard
    either."""
    entry_date = parse_date_param(request.form.get("entry_date"))
    shift = resolve_shift(request.form)
    nozzle_id = request.form.get("nozzle_id", type=int)
    liters = request.form.get("liters", type=float)
    note = request.form.get("note", "").strip()
    nozzle = db.session.get(Nozzle, nozzle_id) if nozzle_id else None

    if not nozzle:
        db.session.rollback()
        flash("Please choose a valid nozzle.", "error")
    elif not liters or liters <= 0:
        db.session.rollback()
        flash("Liters must be a positive number.", "error")
    else:
        db.session.add(
            NozzleTesting(
                nozzle_id=nozzle.id,
                shift_id=shift.id,
                entry_date=entry_date,
                liters=liters,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.flush()
        sale, over_by = sync_sale_testing(nozzle.id, entry_date, shift.id)
        db.session.commit()
        flash(f"Recorded {liters:g} L testing on {nozzle.label}.", "success")
        if sale is None:
            flash(
                f"No meter reading has been saved yet for {nozzle.label} on {entry_date} "
                f"({shift.name}) - this testing will be carved out of the sale automatically "
                f"as soon as that reading is saved.",
                "info",
            )
        elif over_by:
            flash(
                f"{nozzle.label}: total testing on this slot now exceeds the meter difference "
                f"by {over_by:g} L - clamped so the sale doesn't go negative.",
                "error",
            )

    return redirect(url_for("ledger", date=entry_date, shift=shift.id))


@app.route("/ledger/expense", methods=["POST"])
@login_required
@owner_required
def ledger_expense():
    entry_date = parse_date_param(request.form.get("entry_date"))
    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()
    amount = request.form.get("amount", type=float)
    method, bank_account, method_error = resolve_payment_method(request.form)

    if not category:
        flash("Please enter an expense category.", "error")
    elif not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    elif method_error:
        flash(method_error, "error")
    elif method == "cash" and would_overdraw_cash(amount, entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        db.session.add(
            Expense(
                entry_date=entry_date,
                category=category,
                description=description or None,
                amount=amount,
                method=method,
                bank_account_id=bank_account.id if bank_account else None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Logged expense: {category} - Rs {format_number(amount)}", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/purchase", methods=["POST"])
@login_required
@owner_required
def ledger_purchase():
    entry_date = parse_date_param(request.form.get("entry_date"))
    tank_id = request.form.get("tank_id", type=int)
    liters = request.form.get("liters", type=float)
    cost = request.form.get("cost", type=float)
    payment_type = request.form.get("payment_type", "cash")
    payment_type = payment_type if payment_type in ("cash", "credit") else "cash"
    note = request.form.get("note", "").strip()

    tank = db.session.get(Tank, tank_id) if tank_id else None

    supplier = None
    supplier_error = None
    method, bank_account, method_error = "cash", None, None
    if payment_type == "credit":
        supplier, supplier_error = resolve_supplier(request.form)
    else:
        method, bank_account, method_error = resolve_payment_method(request.form)

    if not tank:
        db.session.rollback()
        flash("Please choose a valid tank.", "error")
    elif not liters or liters <= 0:
        db.session.rollback()
        flash("Liters must be a positive number.", "error")
    elif not cost or cost <= 0:
        db.session.rollback()
        flash(
            "Cost must be a positive number - a delivery can't be recorded without its cost, "
            "or the amount owed/paid for it would silently be treated as zero.",
            "error",
        )
    elif payment_type == "credit" and supplier_error:
        db.session.rollback()
        flash(supplier_error, "error")
    elif payment_type == "cash" and method_error:
        db.session.rollback()
        flash(method_error, "error")
    elif payment_type == "cash" and method == "cash" and would_overdraw_cash(cost, entry_date):
        db.session.rollback()
        flash(cash_shortfall_message(entry_date), "error")
    else:
        db.session.add(
            StockPurchase(
                tank_id=tank.id,
                entry_date=entry_date,
                liters=liters,
                cost=cost,
                payment_type=payment_type,
                method=method,
                bank_account_id=bank_account.id if bank_account else None,
                account_id=supplier.id if supplier else None,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Added {liters:g} L to {tank.label} ({entry_date}).", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/tanker-sale", methods=["POST"])
@login_required
@owner_required
def ledger_tanker_sale():
    """Record a Direct Sale from Tanker - fuel bought from a supplier and
    sent straight to a customer without ever entering the pump's tanks
    (see TankerDeal's docstring in models.py for why this is its own
    entry kind and not a DirectSale).

    Owner-only, matching ledger_purchase(): this commits the pump to a
    supplier bill, which is the same reason a fuel delivery is owner-only.

    There is deliberately NO shift field. A shift exists to split a day's
    DISPENSING between crews; nothing was dispensed here, so attributing
    the deal to a shift would put pass-through money into a shift's cash
    handover reconciliation, where it does not belong.

    The two sides are resolved independently - each has its own payment
    type, its own bank picker (two DISTINCT form field names,
    purchase_paid_via / sale_paid_via, so one form can carry both without
    resolve_payment_method() reading the wrong one) and its own account
    picker. A deal can perfectly well be bought in cash and sold on
    credit, or bought on credit and received into a bank.

    The cash guard is only applied to a cash-method PURCHASE, exactly as
    ledger_purchase() applies it, and deliberately against the full
    purchase_cost rather than the deal's net: the money leaves the drawer
    when the tanker is paid for, whether or not the customer settles the
    same day, so netting a credit-sale side against it would wave through
    an outflow the register genuinely cannot cover.
    """
    entry_date = parse_date_param(request.form.get("entry_date"))
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    liters = request.form.get("liters", type=float)
    purchase_cost = request.form.get("purchase_cost", type=float)
    sale_amount = request.form.get("sale_amount", type=float)
    note = request.form.get("note", "").strip()

    purchase_payment_type = request.form.get("purchase_payment_type", "cash")
    if purchase_payment_type not in ("cash", "credit"):
        purchase_payment_type = "cash"
    sale_payment_type = request.form.get("sale_payment_type", "cash")
    if sale_payment_type not in ("cash", "credit"):
        sale_payment_type = "cash"

    fuel_type = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None

    # Buy side. resolve_payment_method() collapses "cash" vs a chosen bank
    # into (method, bank_account) - a "bank" method is stored as
    # purchase_payment_type == "bank" here (unlike StockPurchase, which
    # keeps a separate method column), so the stored payment type is
    # always exactly one of cash | bank | credit.
    supplier = None
    supplier_error = None
    purchase_bank_account = None
    purchase_method_error = None
    if purchase_payment_type == "credit":
        supplier, supplier_error = resolve_supplier(request.form)
    else:
        purchase_payment_type, purchase_bank_account, purchase_method_error = resolve_payment_method(
            request.form, field="purchase_paid_via", new_field="new_purchase_bank_account_name"
        )

    # Sell side, resolved the same way against its own field names.
    customer = None
    customer_error = None
    sale_bank_account = None
    sale_method_error = None
    if sale_payment_type == "credit":
        customer, customer_error = resolve_customer(request.form)
    else:
        sale_payment_type, sale_bank_account, sale_method_error = resolve_payment_method(
            request.form, field="sale_paid_via", new_field="new_sale_bank_account_name"
        )

    if not fuel_type:
        db.session.rollback()
        flash("Please choose a valid fuel type.", "error")
    elif not liters or liters <= 0:
        db.session.rollback()
        flash("Liters must be a positive number.", "error")
    elif not purchase_cost or purchase_cost <= 0:
        db.session.rollback()
        flash(
            "Purchase cost must be a positive number - without it the amount owed to "
            "the supplier, and the whole margin on this deal, would silently be zero.",
            "error",
        )
    elif not sale_amount or sale_amount <= 0:
        db.session.rollback()
        flash("Sale amount must be a positive number.", "error")
    elif supplier_error:
        db.session.rollback()
        flash(supplier_error, "error")
    elif purchase_method_error:
        db.session.rollback()
        flash(purchase_method_error, "error")
    elif customer_error:
        db.session.rollback()
        flash(customer_error, "error")
    elif sale_method_error:
        db.session.rollback()
        flash(sale_method_error, "error")
    elif purchase_payment_type == "cash" and would_overdraw_cash(purchase_cost, entry_date):
        db.session.rollback()
        flash(cash_shortfall_message(entry_date), "error")
    else:
        deal = TankerDeal(
            entry_date=entry_date,
            fuel_type_id=fuel_type.id,
            liters=liters,
            purchase_cost=purchase_cost,
            purchase_payment_type=purchase_payment_type,
            purchase_bank_account_id=purchase_bank_account.id if purchase_bank_account else None,
            supplier_account_id=supplier.id if supplier else None,
            sale_amount=sale_amount,
            sale_payment_type=sale_payment_type,
            sale_bank_account_id=sale_bank_account.id if sale_bank_account else None,
            customer_account_id=customer.id if customer else None,
            note=note or None,
            user_id=current_user.id,
        )
        db.session.add(deal)
        db.session.commit()
        margin = round(sale_amount - purchase_cost, 2)
        flash(
            f"Tanker deal recorded: sold Rs {format_number(sale_amount)} against "
            f"Rs {format_number(purchase_cost)} cost - margin Rs {format_number(margin)}.",
            "success",
        )

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/product-sale", methods=["POST"])
@login_required
def ledger_product_sale():
    """A non-fuel sale (lubricant/filter/shop item) at the forecourt - not
    owner-only, staff sell these the same as fuel. This only ever
    INCREASES cash/bank/what a customer owes, so unlike a purchase there's
    nothing here that could draw the register down - deliberately no
    would_overdraw_cash() call on this route."""
    entry_date = parse_date_param(request.form.get("entry_date"))
    shift = resolve_shift(request.form)
    quantity = request.form.get("quantity", type=float)
    note = request.form.get("note", "").strip()
    # Same cash/bank/credit shape a Sales Return refund uses - "on
    # account" here means the customer's balance grows instead of shrinks,
    # but the resolution (cash, a specific bank, or a customer picker) is
    # identical, so this reuses that resolver rather than duplicating it.
    method, bank_account, account, method_error = resolve_return_method(request.form)
    product, product_error = resolve_product(request.form, entry_date)

    raw_retail_override = request.form.get("retail_rate_override", "").strip()
    retail_override = None
    retail_override_error = None
    if raw_retail_override:
        try:
            retail_override = float(raw_retail_override)
        except ValueError:
            retail_override_error = "Retail rate override is not a valid number."
        if retail_override is not None and retail_override <= 0:
            retail_override_error = "Retail rate override must be a positive number."

    if product_error:
        db.session.rollback()
        flash(product_error, "error")
    elif not quantity or quantity <= 0:
        db.session.rollback()
        flash("Quantity must be a positive number.", "error")
    elif method_error:
        db.session.rollback()
        flash(method_error, "error")
    elif retail_override_error:
        db.session.rollback()
        flash(retail_override_error, "error")
    else:
        # Resolved for entry_date, never read from the product's cached
        # rate directly - a backdated sale has to snapshot the rates that
        # were actually in effect on ITS date (see product_rates_on_date()).
        # purchase_rate is NEVER touched by the override below - cost keeps
        # coming from product_rates_on_date() unconditionally.
        purchase_rate, retail_rate = product_rates_on_date(product, entry_date)
        if retail_override is not None:
            retail_rate = retail_override
        amount = round(quantity * retail_rate, 2)
        # Computed BEFORE the new row is added, so it reflects stock as it
        # stood going into this sale.
        stock_before = product_stock(product, entry_date)
        db.session.add(
            ProductSale(
                product_id=product.id,
                shift_id=shift.id,
                entry_date=entry_date,
                quantity=quantity,
                retail_rate=retail_rate,
                purchase_rate=purchase_rate,
                amount=amount,
                method=method,
                bank_account_id=bank_account.id if bank_account else None,
                account_id=account.id if account else None,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded sale of {quantity:g} {product.label} (Rs {format_number(amount)}).", "success")
        if quantity > stock_before:
            # Overselling means the STOCK COUNT is wrong, not that this
            # sale didn't happen - refusing to save would stop the
            # attendant from recording a sale that physically occurred, so
            # this warns and still saves.
            flash(
                f"{product.label}: sold {quantity:g} but only {stock_before:g} were in stock as "
                f"of {entry_date} - the stock count may need correcting; the sale was still saved.",
                "error",
            )

    return redirect(url_for("ledger", date=entry_date, shift=shift.id))


@app.route("/ledger/other-income", methods=["POST"])
@login_required
def ledger_other_income():
    """Income that isn't a product sale - rent, a side-business profit
    share, etc. Not owner-only: lives under the same "Other Income" entry
    point as Non-Fuel Product Sales, which staff can already enter (see
    ledger_product_sale()'s docstring). Only ever INCREASES cash/bank, so
    like ledger_product_sale(), deliberately no would_overdraw_cash() call."""
    entry_date = parse_date_param(request.form.get("entry_date"))
    description = request.form.get("description", "").strip()
    amount = request.form.get("amount", type=float)
    method, bank_account, account, method_error = resolve_return_method(request.form)

    if not description:
        db.session.rollback()
        flash("Please enter a description.", "error")
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Amount must be a positive number.", "error")
    elif method_error:
        db.session.rollback()
        flash(method_error, "error")
    else:
        db.session.add(
            OtherIncome(
                entry_date=entry_date,
                description=description,
                amount=amount,
                method=method,
                bank_account_id=bank_account.id if bank_account else None,
                account_id=account.id if account else None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded other income: {description} - Rs {format_number(amount)}", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/product-purchase", methods=["POST"])
@login_required
@owner_required
def ledger_product_purchase():
    """Stock received for a non-fuel product - mirrors ledger_purchase()
    (fuel) exactly, including the cash/credit payment_type split. quantity
    may be negative for a return to the supplier or a stock-count
    correction (see ProductPurchase's docstring in models.py); the sign
    of total_cost always comes from quantity, never from unit_cost, which
    is why unit_cost itself must always be a positive per-unit price."""
    entry_date = parse_date_param(request.form.get("entry_date"))
    quantity = request.form.get("quantity", type=float)
    unit_cost = request.form.get("unit_cost", type=float)
    payment_type = request.form.get("payment_type", "cash")
    payment_type = payment_type if payment_type in ("cash", "credit") else "cash"
    note = request.form.get("note", "").strip()

    # A quick-created product's purchase rate falls back to this purchase's
    # own unit_cost (see resolve_product()'s docstring) - only when
    # unit_cost is itself a valid positive number; an invalid unit_cost
    # must surface AS an invalid-unit-cost error below, not get laundered
    # into a confusing "invalid purchase rate" one.
    product, product_error = resolve_product(
        request.form, entry_date, fallback_purchase_rate=unit_cost if unit_cost and unit_cost > 0 else None
    )

    supplier = None
    supplier_error = None
    method, bank_account, method_error = "cash", None, None
    if payment_type == "credit":
        supplier, supplier_error = resolve_supplier(request.form)
    else:
        method, bank_account, method_error = resolve_payment_method(request.form)

    # quantity/unit_cost are checked BEFORE product_error so a bad unit
    # cost is always reported as exactly that - if product_error were
    # checked first, a __new__ product whose purchase rate falls back to
    # this same bad unit_cost would fail there instead, naming the wrong
    # field. Either way, the rollback below discards any Product
    # resolve_product() already flushed above.
    if quantity is None or quantity == 0:
        db.session.rollback()
        flash("Quantity can't be zero - use a negative quantity for a return or correction.", "error")
    elif not unit_cost or unit_cost <= 0:
        db.session.rollback()
        flash(
            "Unit cost must be a positive number - the sign of a return comes from a negative "
            "quantity, never from the cost.",
            "error",
        )
    elif product_error:
        db.session.rollback()
        flash(product_error, "error")
    elif payment_type == "credit" and supplier_error:
        db.session.rollback()
        flash(supplier_error, "error")
    elif payment_type == "cash" and method_error:
        db.session.rollback()
        flash(method_error, "error")
    else:
        total_cost = round(quantity * unit_cost, 2)
        # Only a POSITIVE total_cost paid in cash can draw the register
        # down - a negative-quantity return/correction puts money back
        # (or, on credit, reduces what's owed to the supplier), so it can
        # never overdraw and must skip the guard entirely.
        if (
            payment_type == "cash"
            and method == "cash"
            and total_cost > 0
            and would_overdraw_cash(total_cost, entry_date)
        ):
            db.session.rollback()
            flash(cash_shortfall_message(entry_date), "error")
        else:
            db.session.add(
                ProductPurchase(
                    product_id=product.id,
                    entry_date=entry_date,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    total_cost=total_cost,
                    payment_type=payment_type,
                    method=method,
                    bank_account_id=bank_account.id if bank_account else None,
                    account_id=supplier.id if supplier else None,
                    note=note or None,
                    user_id=current_user.id,
                )
            )
            db.session.commit()
            verb = "Received" if quantity > 0 else "Returned/corrected"
            flash(f"{verb} {abs(quantity):g} {product.label} (Rs {format_number(abs(total_cost))}).", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/supplier-payment", methods=["POST"])
@login_required
@owner_required
def ledger_supplier_payment():
    """Money paid out to a supplier. Mirrors ledger_receipt() exactly:
    an ordinary (childless) supplier takes the untouched single-amount
    path, and a PARENT supplier records one ordinary SupplierPayment per
    sub-account via resolve_group_allocation().

    The cash-overdraw guard is checked against the TOTAL of the split, not
    per share, and before any row is written - the till is drained once by
    the whole payment, so splitting it across sub-accounts must not let a
    payment through that a single payment of the same size would refuse.
    """
    entry_date = parse_date_param(request.form.get("entry_date"))
    supplier, error = resolve_supplier(request.form)
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)

    if error:
        db.session.rollback()
        flash(error, "error")
    elif supplier.children:
        allocations, total, group_error = resolve_group_allocation(
            supplier, request.form, entry_date, amount, direction="payable"
        )
        if group_error:
            db.session.rollback()
            flash(group_error, "error")
        elif method_error:
            db.session.rollback()
            flash(method_error, "error")
        elif method == "cash" and would_overdraw_cash(total, entry_date):
            db.session.rollback()
            flash(cash_shortfall_message(entry_date), "error")
        else:
            for allocation in allocations:
                db.session.add(
                    SupplierPayment(
                        account_id=allocation["account"].id,
                        entry_date=entry_date,
                        amount=allocation["amount"],
                        method=method,
                        bank_account_id=bank_account.id if bank_account else None,
                        note=note or None,
                        user_id=current_user.id,
                    )
                )
            db.session.commit()
            flash(group_allocation_summary(supplier, total, allocations, "Paid"), "success")
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Payment amount must be a positive number.", "error")
    elif method_error:
        db.session.rollback()
        flash(method_error, "error")
    elif method == "cash" and would_overdraw_cash(amount, entry_date):
        db.session.rollback()
        flash(cash_shortfall_message(entry_date), "error")
    else:
        db.session.add(
            SupplierPayment(
                account_id=supplier.id,
                entry_date=entry_date,
                amount=amount,
                method=method,
                bank_account_id=bank_account.id if bank_account else None,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded payment of Rs {format_number(amount)} to {supplier.name}.", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/bank-sale", methods=["POST"])
@login_required
def ledger_bank_sale():
    entry_date = parse_date_param(request.form.get("entry_date"))
    bank_account, error = resolve_bank_account(request.form)
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    shift = resolve_shift(request.form)

    if error:
        db.session.rollback()
        flash(error, "error")
    elif (amount_error := _amount_cash_overdraw_error(amount, entry_date)):
        db.session.rollback()
        flash(amount_error, "error")
    else:
        db.session.add(
            BankSale(
                bank_account_id=bank_account.id,
                shift_id=shift.id,
                entry_date=entry_date,
                amount=amount,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded Rs {format_number(amount)} bank sale to {bank_account.name}.", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/cash-deposit", methods=["POST"])
@login_required
@owner_required
def ledger_cash_deposit():
    entry_date = parse_date_param(request.form.get("entry_date"))
    bank_account, error = resolve_bank_account(request.form)
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()

    if error:
        db.session.rollback()
        flash(error, "error")
    elif (amount_error := _amount_cash_overdraw_error(amount, entry_date)):
        db.session.rollback()
        flash(amount_error, "error")
    else:
        db.session.add(
            CashDeposit(
                bank_account_id=bank_account.id,
                entry_date=entry_date,
                amount=amount,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded deposit of Rs {format_number(amount)} to {bank_account.name}.", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/employee-loan", methods=["POST"])
@login_required
def ledger_employee_loan():
    """Records either a loan/advance to an employee (kind == "loan", the
    default) or an owner drawing (kind == "drawing") - see EmployeeLoan's
    docstring in models.py for why these share one table/route. Not
    @owner_required overall (staff legitimately record employee loans
    every day) - a drawing specifically is gated by hand below, since it's
    the owner's own money leaving the business."""
    entry_date = parse_date_param(request.form.get("entry_date"))
    kind = request.form.get("kind", "loan")
    if kind not in ("loan", "drawing"):
        kind = "loan"

    if kind == "drawing" and not current_user.is_owner:
        flash("Only the owner can record an owner drawing.", "error")
        return redirect(url_for("ledger", date=entry_date))

    if kind == "drawing":
        account, error = resolve_owner(request.form)
    else:
        account, error = resolve_employee(request.form)
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)

    if error:
        db.session.rollback()
        flash(error, "error")
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Amount must be a positive number.", "error")
    elif method_error:
        db.session.rollback()
        flash(method_error, "error")
    elif method == "cash" and would_overdraw_cash(amount, entry_date):
        db.session.rollback()
        flash(cash_shortfall_message(entry_date), "error")
    else:
        db.session.add(
            EmployeeLoan(
                account_id=account.id,
                entry_date=entry_date,
                amount=amount,
                kind=kind,
                method=method,
                bank_account_id=bank_account.id if bank_account else None,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        if kind == "drawing":
            flash(f"Recorded drawing of Rs {format_number(amount)} for {account.name}.", "success")
        else:
            flash(f"Recorded loan/advance of Rs {format_number(amount)} to {account.name}.", "success")

    return redirect(url_for("ledger", date=entry_date))


# Every entry kind that can be deleted, keyed by the URL segment used in
# entry_delete() below - one generic route instead of twelve near-identical
# ones. Adding a new deletable kind later is just one more line here.
DELETABLE_ENTRIES = {
    "sale": (Sale, "nozzle reading"),
    "direct-sale": (DirectSale, "direct sales entry"),
    "dip": (TankDip, "dip reading"),
    "handover": (CashHandover, "cash handover"),
    "credit": (CreditGiven, "credit entry"),
    "sales-return": (SalesReturn, "sales return"),
    "receipt": (Receipt, "receipt"),
    "purchase": (StockPurchase, "fuel purchase"),
    "supplier-payment": (SupplierPayment, "supplier payment"),
    "employee-loan": (EmployeeLoan, "loan/advance"),
    "salary": (SalaryPayment, "salary payment"),
    "bank-sale": (BankSale, "bank sale"),
    "cash-deposit": (CashDeposit, "cash deposit"),
    "expense": (Expense, "expense"),
    "product-sale": (ProductSale, "product sale"),
    "product-purchase": (ProductPurchase, "product purchase"),
    "nozzle-testing": (NozzleTesting, "testing entry"),
    "other-income": (OtherIncome, "other income entry"),
    "tanker-deal": (TankerDeal, "tanker deal"),
}


@app.route("/entry/<kind>/<int:entry_id>/delete", methods=["POST"])
@login_required
@owner_required
def entry_delete(kind, entry_id):
    """Delete any single ledger row, from whichever page linked here (via
    the hidden "next" field) - the two months of paper records about to be
    transcribed will inevitably produce a few mis-slotted entries that
    editing alone can't fix.

    Deliberately does NOT block on either of the two ways a delete can
    ripple: a deleted Sale breaks the meter-reading chain for whatever
    comes after it, and any deleted entry can leave a LATER day's
    cash-in-hand negative. Refusing the delete in either case would trap
    the user with the bad entry still on file - instead both are surfaced
    as flashes after the fact, the same way a gap or a variance is
    surfaced elsewhere rather than prevented.
    """
    model_label = DELETABLE_ENTRIES.get(kind)
    if not model_label:
        abort(404)
    model, label = model_label
    entry = db.session.get(model, entry_id) or abort(404)
    next_url = request.form.get("next") or url_for("ledger")

    # Read what's needed for the post-delete Sale flash now - once the row
    # is committed as deleted, the ORM instance's attributes are no longer
    # safe to read (SQLAlchemy expires them on commit).
    sale_gap_info = (entry.entry_date, entry.nozzle.label) if kind == "sale" else None
    # Same reasoning: read the slot a deleted testing row belonged to
    # before it's gone, so its Sale (if any) can be re-synced without it.
    testing_slot = (entry.nozzle_id, entry.entry_date, entry.shift_id) if kind == "nozzle-testing" else None

    db.session.delete(entry)
    if testing_slot:
        db.session.flush()
        sync_sale_testing(*testing_slot)
    db.session.commit()
    flash(f"Deleted {label}.", "success")

    if sale_gap_info:
        gap_date, nozzle_label = sale_gap_info
        # The next reading for this nozzle - if one already exists after
        # this slot - just fell back to "no entry for the day before",
        # exactly like any other gap in the chain; nothing to fix
        # automatically, just worth knowing about.
        flash(
            f"Removed the {gap_date} reading for {nozzle_label} - the next "
            "recorded reading for this nozzle may need its previous "
            "reading re-entered by hand.",
            "info",
        )

    bad_date = first_negative_cash_date()
    if bad_date:
        flash(
            f"Heads up: cash in hand is now negative on {bad_date} - you "
            "may need to correct or remove entries on or after that date.",
            "error",
        )

    return redirect(next_url)


# --------------------------------------------------------------- edit ---
#
# One editor per entry kind, all dispatched from the single entry_edit()
# route below the way entry_delete() dispatches deletes - thirteen
# near-identical routes would be thirteen places to forget a guard.
#
# Each handler takes (entry, form) and returns
# (error_or_None, success_message, [(note, category), ...]).
#
# THE CONTRACT EVERY HANDLER KEEPS: it must not mutate `entry` until
# every check has passed, so a rejected edit writes nothing at all. The
# route rolls back on error anyway (the resolve_* helpers can have
# quick-created an Account/BankAccount by then), but "validate fully,
# then assign" is what makes that rollback a safety net rather than the
# mechanism.
#
# Each mirrors its own CREATE route's validation and field handling, and
# reuses the same resolver helpers - a rule that must survive future
# edits: if a create route grows a guard, its editor grows the same one,
# or the ledger gets a value through the back door that the front door
# refuses.


def _edit_sale(entry, form):
    """Re-enter a nozzle's meter readings for the slot this Sale already
    occupies. The slot itself (nozzle/date/shift) is deliberately fixed -
    Sale is unique per (nozzle, date, shift) and moving one would either
    collide with a real row or silently punch a hole in the reading
    chain; delete and re-enter is the honest way to move a reading.

    price_per_liter is NOT re-resolved. total_amount is re-derived from
    the row's OWN stored price, because re-pricing to today's rate would
    silently rewrite history - repricing is reprice_entries()'s job
    (ledger_logic.py), triggered deliberately from Settings.

    The same three chain guards ledger_readings() applies are applied
    here: a previous reading can't dip below an earlier recorded one, a
    current reading can't fall below its own previous, and it can't
    overshoot a later reading already on file.
    """
    previous = form.get("previous_reading", type=float)
    current = form.get("current_reading", type=float)
    label = entry.nozzle.label

    if previous is None:
        return f"{label}: previous reading is not a valid number.", None, []
    if current is None:
        return f"{label}: reading is not a valid number.", None, []

    floor = nearest_earlier_reading(entry.nozzle, entry.entry_date, entry.shift)
    if previous < floor:
        return (
            f"{label}: previous reading ({previous:g}) can't be lower than an "
            f"earlier recorded reading ({floor:g}).",
            None,
            [],
        )
    if current < previous:
        return (
            f"{label}: reading ({current:g}) is less than the previous "
            f"reading ({previous:g}).",
            None,
            [],
        )
    next_sale = next_sale_on_or_after(entry.nozzle_id, entry.entry_date, entry.shift)
    if next_sale and current > next_sale.current_reading:
        return (
            f"{label}: reading ({current:g}) is more than a later reading "
            f"already recorded on {next_sale.entry_date} ({next_sale.current_reading:g}).",
            None,
            [],
        )

    entry.previous_reading = previous
    entry.current_reading = current
    # Provisional, exactly as ledger_readings() writes it: the full gross
    # meter difference with testing not yet carved out. sync_sale_testing()
    # below is the only writer of testing_liters and re-derives
    # liters/total_amount from gross minus whatever testing is on file.
    entry.liters = round(current - previous, 2)
    entry.total_amount = round(entry.liters * entry.price_per_liter, 2)
    db.session.flush()
    _, over_by = sync_sale_testing(entry.nozzle_id, entry.entry_date, entry.shift_id)

    notes = []
    if over_by:
        notes.append(
            (
                f"{label}: testing recorded for this slot exceeds the meter difference "
                f"by {over_by:g} L - clamped so the sale doesn't go negative.",
                "error",
            )
        )
    if next_sale and round(next_sale.previous_reading, 2) != round(current, 2):
        # Identical situation to deleting a reading (see entry_delete):
        # the following slot's previous_reading no longer matches what
        # this slot now ends at, so the chain has a step in it. Nothing to
        # fix automatically - guessing on the user's behalf is exactly
        # what ledger_readings() refuses to do past one slot.
        notes.append(
            (
                f"Changed the {entry.entry_date} reading for {label} - the next "
                f"recorded reading for this nozzle may need its previous "
                f"reading re-entered by hand.",
                "info",
            )
        )
    return None, f"Updated {label}'s reading for {entry.entry_date} ({entry.liters:g} L).", notes


def _edit_direct_sale(entry, form):
    """Litres only. The tank/date/shift slot is fixed for the same reason
    a Sale's is (DirectSale is unique per tank/date/shift), and
    price_per_liter stays the row's own for the same reason a Sale's
    does - total_amount is re-derived from it, never carried over."""
    liters = form.get("liters", type=float)
    if not liters or liters <= 0:
        return f"{entry.tank.label}: litres must be a positive number.", None, []

    entry.liters = liters
    entry.total_amount = round(liters * entry.price_per_liter, 2)
    return (
        None,
        f"Updated direct sales entry for {entry.tank.label} ({liters:g} L).",
        [],
    )


def _edit_dip(entry, form):
    """A dip is measured in cm on a tank with a calibration chart and in
    litres on one without - the same split ledger_dip() makes, resolved
    from the tank rather than trusted from the form so a hand-rolled POST
    can't store an unconverted cm figure as litres."""
    tank = entry.tank
    has_chart = bool(tank.dip_chart_rows)
    raw = (form.get("dip") or "").strip()
    if not raw:
        return f"Tank {tank.number}: please enter a dip reading.", None, []
    try:
        entered = float(raw)
    except ValueError:
        return f"Tank {tank.number}: not a valid number.", None, []
    if entered < 0:
        return f"Tank {tank.number}: dip must be zero or more.", None, []

    if has_chart:
        dip_cm = entered
        dip_value = liters_from_dip_cm(tank, entered)
        if dip_value is None:
            return f"Tank {tank.number}: no dip chart found.", None, []
    else:
        dip_cm = None
        dip_value = entered

    raw_water = (form.get("water_cm") or "").strip()
    water_cm = None
    if raw_water:
        try:
            water_cm = float(raw_water)
        except ValueError:
            return f"Tank {tank.number}: water level is not a valid number.", None, []
        if water_cm < 0:
            return f"Tank {tank.number}: water level must be zero or more.", None, []

    entry.dip_cm = dip_cm
    entry.dip_liters = dip_value
    entry.water_cm = water_cm
    return None, f"Updated dip reading for {tank.label} ({entry.entry_date}).", []


def _edit_handover(entry, form):
    """Purely a reconciliation record - it moves no money itself (see
    CashHandover in models.py), so there is no cash guard here any more
    than there is on ledger_handover(). The variance against the day's
    expected cash is recomputed from the ledger, never stored."""
    declared = form.get("declared_amount", type=float)
    attendant_id = form.get("attendant_id", type=int)
    note = (form.get("note") or "").strip()
    attendant = db.session.get(Account, attendant_id) if attendant_id else None

    if declared is None or declared < 0:
        return "Please enter the cash counted (zero or more).", None, []

    entry.declared_amount = declared
    entry.attendant_id = attendant.id if attendant else None
    entry.note = note or None

    expected = sales_breakdown_for_date(entry.entry_date, shift_id=entry.shift_id)["cash"]
    variance = round(declared - expected, 2)
    if abs(variance) < 0.01:
        message = f"{entry.shift.name}: cash counted matches the ledger exactly."
    elif variance < 0:
        message = (
            f"{entry.shift.name}: updated - short by Rs {format_number(abs(variance))} "
            f"(expected Rs {format_number(expected)}, counted Rs {format_number(declared)})."
        )
    else:
        message = (
            f"{entry.shift.name}: updated - over by Rs {format_number(variance)} "
            f"(expected Rs {format_number(expected)}, counted Rs {format_number(declared)})."
        )
    return None, message, []


def _edit_sales_return(entry, form):
    """Fuel handed back and refunded. price_per_liter stays the row's own
    (the rate actually charged when the fuel was sold), so amount is
    re-derived as litres x that price - which is also why the fuel type
    isn't editable here: changing it would leave the stored price
    belonging to a different fuel."""
    tank_id = form.get("tank_id", type=int)
    liters = form.get("liters", type=float)
    note = (form.get("note") or "").strip()
    tank = db.session.get(Tank, tank_id) if tank_id else None
    method, bank_account, account, method_error = resolve_return_method(form)

    if not tank:
        return "Please choose which tank the fuel goes back into.", None, []
    if not liters or liters <= 0:
        return "Liters must be a positive number.", None, []
    if method_error:
        return method_error, None, []

    amount = round(liters * entry.price_per_liter, 2)
    old_cash = entry.amount if entry.method == "cash" else 0
    new_cash = amount if method == "cash" else 0
    if would_overdraw_cash(new_cash, entry.entry_date, old_cash, entry.entry_date):
        return cash_shortfall_message(entry.entry_date), None, []

    entry.tank_id = tank.id
    entry.liters = liters
    entry.amount = amount
    entry.method = method
    entry.bank_account_id = bank_account.id if bank_account else None
    entry.account_id = account.id if account else None
    entry.note = note or None
    return (
        None,
        f"Updated return of {liters:g} L {entry.fuel_type.name} into {tank.label} "
        f"(Rs {format_number(amount)}).",
        [],
    )


def _edit_bank_sale(entry, form):
    """Cash taken out of the drawer and banked as a sale - guarded exactly
    like ledger_bank_sale(), but with the row's own old figure handed to
    would_overdraw_cash() so raising an existing bank sale is only
    checked against the DIFFERENCE, not against the whole new amount on
    top of itself."""
    entry_date = parse_date_param(form.get("entry_date"), entry.entry_date)
    bank_account, error = resolve_bank_account(form)
    amount = form.get("amount", type=float)
    note = (form.get("note") or "").strip()

    if error:
        return error, None, []
    if not amount or amount <= 0:
        return "Amount must be a positive number.", None, []
    if would_overdraw_cash(amount, entry_date, entry.amount, entry.entry_date):
        return cash_shortfall_message(entry_date), None, []

    entry.entry_date = entry_date
    entry.bank_account_id = bank_account.id
    entry.amount = amount
    entry.note = note or None
    return None, f"Updated bank sale to {bank_account.name} (Rs {format_number(amount)}).", []


def _edit_cash_deposit(entry, form):
    """Same shape and same guard as _edit_bank_sale - a deposit also
    leaves the drawer."""
    entry_date = parse_date_param(form.get("entry_date"), entry.entry_date)
    bank_account, error = resolve_bank_account(form)
    amount = form.get("amount", type=float)
    note = (form.get("note") or "").strip()

    if error:
        return error, None, []
    if not amount or amount <= 0:
        return "Amount must be a positive number.", None, []
    if would_overdraw_cash(amount, entry_date, entry.amount, entry.entry_date):
        return cash_shortfall_message(entry_date), None, []

    entry.entry_date = entry_date
    entry.bank_account_id = bank_account.id
    entry.amount = amount
    entry.note = note or None
    return None, f"Updated deposit to {bank_account.name} (Rs {format_number(amount)}).", []


def _edit_expense(entry, form):
    """Mirrors ledger_expense(). Only the cash-method side of the old and
    new figures counts toward the overdraw guard - switching an expense
    from cash to a bank puts the whole old amount back in the drawer."""
    entry_date = parse_date_param(form.get("entry_date"), entry.entry_date)
    category = (form.get("category") or "").strip()
    description = (form.get("description") or "").strip()
    amount = form.get("amount", type=float)
    method, bank_account, method_error = resolve_payment_method(form)
    old_cash = entry.amount if entry.method == "cash" else 0
    new_cash = amount if (amount and method == "cash") else 0

    if not category:
        return "Please enter an expense category.", None, []
    if not amount or amount <= 0:
        return "Amount must be a positive number.", None, []
    if method_error:
        return method_error, None, []
    if would_overdraw_cash(new_cash, entry_date, old_cash, entry.entry_date):
        return cash_shortfall_message(entry_date), None, []

    entry.entry_date = entry_date
    entry.category = category
    entry.description = description or None
    entry.amount = amount
    entry.method = method
    entry.bank_account_id = bank_account.id if bank_account else None
    return None, f"Updated expense: {category} - Rs {format_number(amount)}", []


def _edit_product_sale(entry, form):
    """Mirrors ledger_product_sale(), including its deliberate absence of
    a cash guard (a sale only ever puts money in) and its "overselling
    warns but still saves" rule - a stock count being wrong is not a
    reason to refuse a sale that physically happened.

    purchase_rate is re-resolved from product_rates_on_date() for the
    entry's date and is never overridable, exactly as on create - it is
    cost, not a negotiated price. The retail rate is submitted through
    the same retail_rate_override field the create form uses (prefilled
    with the row's current rate, so saving an untouched form keeps the
    figure that was actually charged).
    """
    entry_date = parse_date_param(form.get("entry_date"), entry.entry_date)
    quantity = form.get("quantity", type=float)
    note = (form.get("note") or "").strip()
    method, bank_account, account, method_error = resolve_return_method(form)
    product, product_error = resolve_product(form, entry_date)

    raw_retail_override = (form.get("retail_rate_override") or "").strip()
    retail_override = None
    retail_override_error = None
    if raw_retail_override:
        try:
            retail_override = float(raw_retail_override)
        except ValueError:
            retail_override_error = "Retail rate override is not a valid number."
        if retail_override is not None and retail_override <= 0:
            retail_override_error = "Retail rate override must be a positive number."

    if product_error:
        return product_error, None, []
    if not quantity or quantity <= 0:
        return "Quantity must be a positive number.", None, []
    if method_error:
        return method_error, None, []
    if retail_override_error:
        return retail_override_error, None, []

    purchase_rate, retail_rate = product_rates_on_date(product, entry_date)
    if retail_override is not None:
        retail_rate = retail_override
    amount = round(quantity * retail_rate, 2)
    # Stock as it stands with THIS sale's old quantity still counted -
    # backed out below so the warning is judged against what was
    # available to sell, not against the row being replaced.
    unchanged_slot = product.id == entry.product_id and entry_date == entry.entry_date
    stock_before = round(
        product_stock(product, entry_date) + (entry.quantity if unchanged_slot else 0), 2
    )

    entry.entry_date = entry_date
    entry.product_id = product.id
    entry.quantity = quantity
    entry.retail_rate = retail_rate
    entry.purchase_rate = purchase_rate
    entry.amount = amount
    entry.method = method
    entry.bank_account_id = bank_account.id if bank_account else None
    entry.account_id = account.id if account else None
    entry.note = note or None

    notes = []
    if quantity > stock_before:
        notes.append(
            (
                f"{product.label}: sold {quantity:g} but only {stock_before:g} were in stock as "
                f"of {entry_date} - the stock count may need correcting; the edit was still saved.",
                "error",
            )
        )
    return (
        None,
        f"Updated sale of {quantity:g} {product.label} (Rs {format_number(amount)}).",
        notes,
    )


def _edit_product_purchase(entry, form):
    """Mirrors ledger_product_purchase() field for field, including the
    negative-quantity return/correction case and the rule that the sign of
    total_cost comes from quantity alone, never from unit_cost."""
    entry_date = parse_date_param(form.get("entry_date"), entry.entry_date)
    quantity = form.get("quantity", type=float)
    unit_cost = form.get("unit_cost", type=float)
    payment_type = form.get("payment_type", "cash")
    payment_type = payment_type if payment_type in ("cash", "credit") else "cash"
    note = (form.get("note") or "").strip()

    product, product_error = resolve_product(
        form, entry_date, fallback_purchase_rate=unit_cost if unit_cost and unit_cost > 0 else None
    )

    supplier = None
    supplier_error = None
    method, bank_account, method_error = "cash", None, None
    if payment_type == "credit":
        supplier, supplier_error = resolve_supplier(form)
    else:
        method, bank_account, method_error = resolve_payment_method(form)

    # Same ordering as the create route: a bad unit cost must be reported
    # as exactly that, never laundered into a "bad purchase rate" error
    # from the quick-create fallback above.
    if quantity is None or quantity == 0:
        return "Quantity can't be zero - use a negative quantity for a return or correction.", None, []
    if not unit_cost or unit_cost <= 0:
        return (
            "Unit cost must be a positive number - the sign of a return comes from a negative "
            "quantity, never from the cost.",
            None,
            [],
        )
    if product_error:
        return product_error, None, []
    if payment_type == "credit" and supplier_error:
        return supplier_error, None, []
    if payment_type == "cash" and method_error:
        return method_error, None, []

    total_cost = round(quantity * unit_cost, 2)
    # Signed on purpose, unlike the create route's "only guard a positive
    # total_cost" shortcut. A negative total_cost is money coming BACK in,
    # so as the new figure it can never overdraw (the guard sees a
    # negative outflow and passes, exactly as skipping it would) - but as
    # the OLD figure being replaced it has to be backed out with its sign
    # intact, or removing a refund would look free.
    old_cash = entry.total_cost if (entry.payment_type == "cash" and entry.method == "cash") else 0
    new_cash = total_cost if (payment_type == "cash" and method == "cash") else 0
    if would_overdraw_cash(new_cash, entry_date, old_cash, entry.entry_date):
        return cash_shortfall_message(entry_date), None, []

    entry.entry_date = entry_date
    entry.product_id = product.id
    entry.quantity = quantity
    entry.unit_cost = unit_cost
    entry.total_cost = total_cost
    entry.payment_type = payment_type
    entry.method = method
    entry.bank_account_id = bank_account.id if bank_account else None
    entry.account_id = supplier.id if supplier else None
    entry.note = note or None
    verb = "Received" if quantity > 0 else "Returned/corrected"
    return (
        None,
        f"Updated: {verb.lower()} {abs(quantity):g} {product.label} (Rs {format_number(abs(total_cost))}).",
        [],
    )


def _edit_nozzle_testing(entry, form):
    """Testing can be re-slotted (nozzle, date and shift are all editable
    here, unlike a Sale's), so BOTH the slot it left and the slot it
    joined have to be re-reconciled - the Sale it used to be carved out
    of gets those litres back, and the Sale it now belongs to has them
    taken out. Missing either half would leave one of the two Sales
    permanently wrong.

    No cash guard, deliberately, exactly as on ledger_testing(): testing
    moves no money at all, it only reduces revenue the way a smaller
    meter reading would.
    """
    old_slot = (entry.nozzle_id, entry.entry_date, entry.shift_id)
    entry_date = parse_date_param(form.get("entry_date"), entry.entry_date)
    shift = resolve_shift(form)
    nozzle_id = form.get("nozzle_id", type=int)
    liters = form.get("liters", type=float)
    note = (form.get("note") or "").strip()
    nozzle = db.session.get(Nozzle, nozzle_id) if nozzle_id else None

    if not nozzle:
        return "Please choose a valid nozzle.", None, []
    if not liters or liters <= 0:
        return "Liters must be a positive number.", None, []

    entry.nozzle_id = nozzle.id
    entry.entry_date = entry_date
    entry.shift_id = shift.id
    entry.liters = liters
    entry.note = note or None

    new_slot = (nozzle.id, entry_date, shift.id)
    db.session.flush()
    notes = []
    if old_slot != new_slot:
        sync_sale_testing(*old_slot)
    sale, over_by = sync_sale_testing(*new_slot)
    if sale is None:
        notes.append(
            (
                f"No meter reading has been saved yet for {nozzle.label} on {entry_date} "
                f"({shift.name}) - this testing will be carved out of the sale automatically "
                f"as soon as that reading is saved.",
                "info",
            )
        )
    elif over_by:
        notes.append(
            (
                f"{nozzle.label}: total testing on this slot now exceeds the meter difference "
                f"by {over_by:g} L - clamped so the sale doesn't go negative.",
                "error",
            )
        )
    return None, f"Updated testing to {liters:g} L on {nozzle.label}.", notes


def _edit_other_income(entry, form):
    """Mirrors ledger_other_income() - only ever increases cash/bank/what a
    customer owes, so no cash guard here either."""
    entry_date = parse_date_param(form.get("entry_date"), entry.entry_date)
    description = (form.get("description") or "").strip()
    amount = form.get("amount", type=float)
    method, bank_account, account, method_error = resolve_return_method(form)

    if not description:
        return "Please enter a description.", None, []
    if not amount or amount <= 0:
        return "Amount must be a positive number.", None, []
    if method_error:
        return method_error, None, []

    entry.entry_date = entry_date
    entry.description = description
    entry.amount = amount
    entry.method = method
    entry.bank_account_id = bank_account.id if bank_account else None
    entry.account_id = account.id if account else None
    return None, f"Updated other income: {description} - Rs {format_number(amount)}", []


def _edit_tanker_deal(entry, form):
    """Mirrors ledger_tanker_sale(): two independently-resolved sides,
    each with its own payment type, bank picker field name and account
    picker, and the cash guard applied only to a cash-method PURCHASE and
    against the full purchase cost - never netted against the sale side,
    since the drawer is drained when the tanker is paid for whether or
    not the customer has settled.

    Nothing here touches tank stock, on edit any more than on create -
    the fuel never entered a tank (see TankerDeal in models.py). The
    margin shown everywhere is derived from purchase_cost/sale_amount on
    read, so correcting either figure moves profit and nothing else.
    """
    entry_date = parse_date_param(form.get("entry_date"), entry.entry_date)
    fuel_type_id = form.get("fuel_type_id", type=int)
    liters = form.get("liters", type=float)
    purchase_cost = form.get("purchase_cost", type=float)
    sale_amount = form.get("sale_amount", type=float)
    note = (form.get("note") or "").strip()

    purchase_payment_type = form.get("purchase_payment_type", "cash")
    if purchase_payment_type not in ("cash", "credit"):
        purchase_payment_type = "cash"
    sale_payment_type = form.get("sale_payment_type", "cash")
    if sale_payment_type not in ("cash", "credit"):
        sale_payment_type = "cash"

    fuel_type = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None

    supplier = None
    supplier_error = None
    purchase_bank_account = None
    purchase_method_error = None
    if purchase_payment_type == "credit":
        supplier, supplier_error = resolve_supplier(form)
    else:
        purchase_payment_type, purchase_bank_account, purchase_method_error = resolve_payment_method(
            form, field="purchase_paid_via", new_field="new_purchase_bank_account_name"
        )

    customer = None
    customer_error = None
    sale_bank_account = None
    sale_method_error = None
    if sale_payment_type == "credit":
        customer, customer_error = resolve_customer(form)
    else:
        sale_payment_type, sale_bank_account, sale_method_error = resolve_payment_method(
            form, field="sale_paid_via", new_field="new_sale_bank_account_name"
        )

    if not fuel_type:
        return "Please choose a valid fuel type.", None, []
    if not liters or liters <= 0:
        return "Liters must be a positive number.", None, []
    if not purchase_cost or purchase_cost <= 0:
        return (
            "Purchase cost must be a positive number - without it the amount owed to "
            "the supplier, and the whole margin on this deal, would silently be zero.",
            None,
            [],
        )
    if not sale_amount or sale_amount <= 0:
        return "Sale amount must be a positive number.", None, []
    if supplier_error:
        return supplier_error, None, []
    if purchase_method_error:
        return purchase_method_error, None, []
    if customer_error:
        return customer_error, None, []
    if sale_method_error:
        return sale_method_error, None, []

    old_cash = entry.purchase_cost if entry.purchase_payment_type == "cash" else 0
    new_cash = purchase_cost if purchase_payment_type == "cash" else 0
    if would_overdraw_cash(new_cash, entry_date, old_cash, entry.entry_date):
        return cash_shortfall_message(entry_date), None, []

    entry.entry_date = entry_date
    entry.fuel_type_id = fuel_type.id
    entry.liters = liters
    entry.purchase_cost = purchase_cost
    entry.purchase_payment_type = purchase_payment_type
    entry.purchase_bank_account_id = purchase_bank_account.id if purchase_bank_account else None
    entry.supplier_account_id = supplier.id if supplier else None
    entry.sale_amount = sale_amount
    entry.sale_payment_type = sale_payment_type
    entry.sale_bank_account_id = sale_bank_account.id if sale_bank_account else None
    entry.customer_account_id = customer.id if customer else None
    entry.note = note or None

    margin = round(sale_amount - purchase_cost, 2)
    return (
        None,
        f"Tanker deal updated: sold Rs {format_number(sale_amount)} against "
        f"Rs {format_number(purchase_cost)} cost - margin Rs {format_number(margin)}.",
        [],
    )


# The thirteen kinds the Ledger's feed edits through entry_edit() below,
# keyed by the same URL segment DELETABLE_ENTRIES uses. The six
# account-side kinds (credit, receipt, purchase, supplier-payment,
# employee-loan, salary) are deliberately absent: they already have
# editors under /accounts/entry/..., and the feed posts to THOSE with a
# "next" field rather than growing a second implementation of each.
EDITABLE_ENTRIES = {
    "sale": (Sale, _edit_sale),
    "direct-sale": (DirectSale, _edit_direct_sale),
    "dip": (TankDip, _edit_dip),
    "handover": (CashHandover, _edit_handover),
    "sales-return": (SalesReturn, _edit_sales_return),
    "bank-sale": (BankSale, _edit_bank_sale),
    "cash-deposit": (CashDeposit, _edit_cash_deposit),
    "expense": (Expense, _edit_expense),
    "product-sale": (ProductSale, _edit_product_sale),
    "product-purchase": (ProductPurchase, _edit_product_purchase),
    "nozzle-testing": (NozzleTesting, _edit_nozzle_testing),
    "other-income": (OtherIncome, _edit_other_income),
    "tanker-deal": (TankerDeal, _edit_tanker_deal),
}


@app.route("/entry/<kind>/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def entry_edit(kind, entry_id):
    """Edit one ledger row in place, from whichever page linked here (via
    the hidden "next" field) - the counterpart to entry_delete(), and one
    dispatching route rather than thirteen near-identical ones.

    DOWNSTREAM RECALCULATION IS AUTOMATIC - PLEASE DON'T "FIX" THIS BY
    ADDING A CACHE. This app stores no running totals anywhere: account
    balances, credit aging, tank book stock, product stock, cash in hand,
    bank balances, the dashboard and every report all derive themselves
    fresh from the rows on each read (see Account.balance in models.py,
    and book_stock() / cash_account_balance() / credit_aging() in
    ledger_logic.py). Changing a row here therefore changes every figure
    that row feeds, with nothing to invalidate and nothing to recompute.

    Exactly two things do NOT fall out of that, and both are handled by
    the per-kind handlers above rather than here:
      * Sale.previous_reading/current_reading form a chain across slots,
        so editing one reading can leave the NEXT slot's previous reading
        stale - surfaced as a flash, never silently patched.
      * Sale.testing_liters is a stored split written only by
        sync_sale_testing(), so a Sale or NozzleTesting edit re-runs it
        for every slot it touched.

    Everything else - the cash-overdraw guard, the "cash went negative on
    a later date" warning below - is the same treatment the create routes
    and entry_delete() already give. A rejected edit writes nothing: the
    handlers validate before assigning, and the rollback here also
    discards any account/bank account a resolve_* helper quick-created
    while parsing the rejected form.
    """
    editable = EDITABLE_ENTRIES.get(kind)
    if not editable:
        abort(404)
    model, handler = editable
    entry = db.session.get(model, entry_id) or abort(404)

    error, success, notes = handler(entry, request.form)
    if error:
        db.session.rollback()
        flash(error, "error")
    else:
        db.session.commit()
        flash(success, "success")
        for note, category in notes:
            flash(note, category)
        bad_date = first_negative_cash_date()
        if bad_date:
            # Same after-the-fact warning entry_delete() gives: an edit on
            # one date can only be judged against that date, but the money
            # it moved is spent again on every later one.
            flash(
                f"Heads up: cash in hand is now negative on {bad_date} - you "
                "may need to correct or remove entries on or after that date.",
                "error",
            )

    return redirect(request.form.get("next") or url_for("ledger", date=entry.entry_date))
