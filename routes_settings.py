"""Settings routes: pump configuration - tanks, dispensers, nozzles,
fuel prices/products, bank accounts, users/invites, shifts, dip charts,
cash-account opening balance, and the CSV backup export.

Moved verbatim out of app.py (see routes_settings.py's own history) -
this is the settings/... route group, unchanged except for its location.
"""

import csv
import io
import zipfile
from datetime import date, datetime

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

import email_service
from formatting import format_number
from extensions import db
from tenancy import unscoped
from ledger_logic import (
    cash_account_balance,
    first_negative_cash_date,
    product_rates_on_date,
    product_stock_summary,
    record_fuel_price,
    record_product_rates,
    reprice_entries,
)
from models import (
    Account,
    BankAccount,
    BankSale,
    CashAccount,
    CashDeposit,
    CashHandover,
    CreditGiven,
    DirectSale,
    Dispenser,
    EmployeeLoan,
    Expense,
    FuelPriceHistory,
    FuelType,
    Nozzle,
    NozzleReset,
    NozzleTesting,
    OtherIncome,
    PasswordResetToken,
    Product,
    ProductPurchase,
    ProductRateHistory,
    ProductSale,
    Pump,
    Receipt,
    SalaryPayment,
    Sale,
    SalesReturn,
    Shift,
    StockPurchase,
    SupplierPayment,
    Tank,
    TankDip,
    TankDipChart,
    TankerDeal,
    User,
)
from app import (
    app,
    EMAIL_RE,
    INVITE_TOKEN_TTL_HOURS,
    PRODUCT_CATEGORIES,
    PRODUCT_UNITS,
    _issue_auth_token,
    _password_errors,
    _send_invite_email,
    get_cash_account,
    owner_required,
    parse_date_param,
    parse_stock_date,
)

@app.route("/settings")
@login_required
@owner_required
def settings():
    tanks = Tank.query.order_by(Tank.number).all()
    fuel_types = FuelType.query.order_by(FuelType.name).all()
    dispensers = Dispenser.query.order_by(Dispenser.number).all()
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
    cash_account = get_cash_account()
    users = User.query.order_by(User.role, User.username).all()
    shifts = Shift.query.order_by(Shift.sort_order, Shift.id).all()
    dip_charts = {
        t.id: sorted(t.dip_chart_rows, key=lambda r: r.depth_cm) for t in tanks
    }
    # Active first, then deactivated - within each group alphabetical
    # (case-insensitive) so a 95-SKU catalogue is still scannable.
    today = date.today()
    products = Product.query.order_by(Product.is_active.desc(), func.lower(Product.name)).all()
    # Includes inactive products (unlike product_stock_summary()'s own
    # default) - this table has to show a deactivated product's last-known
    # stock too, not just active ones.
    product_stock_rows = {
        row["product"].id: row for row in product_stock_summary(today, products=products)
    }
    pump = db.session.get(Pump, current_user.pump_id)
    # session.pop: the invite/resend link is meant to be shown exactly
    # once, right after the action that created it (see
    # settings_invite_user/settings_resend_invite) - a later plain GET of
    # /settings must not keep re-displaying a stale one.
    invite_link = session.pop("pending_invite_link", None)
    return render_template(
        "settings.html",
        tanks=tanks,
        fuel_types=fuel_types,
        dispensers=dispensers,
        bank_accounts=bank_accounts,
        cash_account=cash_account,
        cash_balance=cash_account_balance(cash_account),
        users=users,
        shifts=shifts,
        dip_charts=dip_charts,
        products=products,
        product_stock_rows=product_stock_rows,
        today=today,
        pump=pump,
        invite_link=invite_link,
        email_configured=email_service.is_configured(),
        email_sender=email_service.sender_address(),
    )


# Every ledger table, in no particular order - a full backup is one CSV
# per model rather than a single dump, so each file opens cleanly in a
# spreadsheet on its own.
BACKUP_MODELS = [
    User,
    Shift,
    FuelType,
    FuelPriceHistory,
    Tank,
    Dispenser,
    Nozzle,
    NozzleReset,
    Account,
    Sale,
    DirectSale,
    CreditGiven,
    SalesReturn,
    NozzleTesting,
    StockPurchase,
    SupplierPayment,
    Receipt,
    EmployeeLoan,
    Expense,
    BankAccount,
    BankSale,
    CashDeposit,
    CashAccount,
    TankDip,
    TankDipChart,
    CashHandover,
    SalaryPayment,
    # Product catalogue (Phase 2A) - Ledger wiring for these lands in a
    # later phase, but the tables exist now and a backup has to be
    # complete regardless of whether anything has been entered yet.
    Product,
    ProductRateHistory,
    ProductPurchase,
    ProductSale,
    OtherIncome,
    # Pass-through tanker deals - not a stock table, but it carries real
    # money on both sides, so a backup that skipped it would restore an
    # incomplete ledger.
    TankerDeal,
]


@app.route("/settings/google/disconnect", methods=["POST"])
@login_required
def google_disconnect():
    """Unlink the CURRENT user's own Google account. Not owner_required:
    any logged-in user (owner or staff) may manage their own link, same
    as change_password()."""
    if not current_user.has_usable_password:
        # Clearing google_sub here would leave this user with no way to
        # log in at all: set_unusable_password() stored a sentinel, not a
        # hash, so no password they could type will ever authenticate.
        flash(
            "Set a password first (Change Password, or Forgot Password "
            "if you don't have one) before disconnecting Google - "
            "otherwise you'd lock yourself out.",
            "error",
        )
        return redirect(url_for("settings"))

    current_user.google_sub = None
    db.session.commit()
    flash("Google account disconnected.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/export-backup")
@login_required
@owner_required
def settings_export_backup():
    """A complete copy of every record as one CSV per table, zipped in
    memory - the only way to get data out of whatever database (a local
    SQLite file or a hosted Postgres instance) is currently behind the
    app, independent of this app's own UI ever being available again."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for model in BACKUP_MODELS:
            # password_hash never leaves this database, backup or not -
            # a stolen backup file must not be a stolen credential store.
            columns = [
                c.name
                for c in model.__table__.columns
                if not (model is User and c.name == "password_hash")
            ]
            text_buffer = io.StringIO()
            writer = csv.writer(text_buffer)
            writer.writerow(columns)
            for row in model.query.order_by(model.id).all():
                values = []
                for col in columns:
                    value = getattr(row, col)
                    if isinstance(value, (datetime, date)):
                        value = value.isoformat()
                    elif value is None:
                        value = ""
                    values.append(value)
                writer.writerow(values)
            zf.writestr(f"{model.__tablename__}.csv", text_buffer.getvalue())

    buffer.seek(0)
    filename = f"petrol-khata-backup-{date.today().isoformat()}.zip"
    return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name=filename)


@app.route("/settings/add-tank", methods=["POST"])
@login_required
@owner_required
def settings_add_tank():
    fuel_name = request.form.get("fuel_name", "").strip()
    capacity = request.form.get("capacity", type=float)
    stock = request.form.get("stock", type=float)
    cost_per_liter = request.form.get("cost_per_liter", type=float)
    stock_date, date_error = parse_stock_date(request.form.get("stock_date", ""))

    if not fuel_name:
        flash("Please enter a fuel name.", "error")
    elif not capacity or capacity <= 0:
        flash("Please enter a valid capacity.", "error")
    elif stock is None or stock < 0 or stock > capacity:
        flash("Please enter a valid starting stock (not more than capacity).", "error")
    elif cost_per_liter is None or cost_per_liter < 0:
        flash("Please enter a valid cost per liter for the starting stock.", "error")
    elif date_error == "invalid":
        flash("Please enter a valid stock date.", "error")
    elif date_error == "future":
        flash("Stock date can't be in the future.", "error")
    else:
        fuel_type = FuelType.query.filter(func.lower(FuelType.name) == fuel_name.lower()).first()
        if not fuel_type:
            price = request.form.get("price", type=float) or 0
            fuel_type = FuelType(name=fuel_name, price_per_liter=price)
            db.session.add(fuel_type)
            db.session.flush()
            db.session.add(
                FuelPriceHistory(fuel_type_id=fuel_type.id, price_per_liter=price, effective_date=date.today())
            )
        number = (db.session.query(func.coalesce(func.max(Tank.number), 0)).scalar()) + 1
        db.session.add(
            Tank(
                number=number,
                fuel_type_id=fuel_type.id,
                capacity_liters=capacity,
                starting_stock_liters=stock,
                starting_stock_date=stock_date or date.today(),
                starting_stock_cost_per_liter=cost_per_liter,
                low_stock_threshold=round(capacity * 0.1, 2),
            )
        )
        db.session.commit()
        flash(f"Added Tank {number} ({fuel_type.name}).", "success")

    return redirect(url_for("settings"))


@app.route("/settings/edit-tank/<int:tank_id>", methods=["POST"])
@login_required
@owner_required
def settings_edit_tank(tank_id):
    tank = db.session.get(Tank, tank_id) or abort(404)
    capacity = request.form.get("capacity", type=float)
    threshold = request.form.get("threshold", type=float)
    stock = request.form.get("stock", type=float)
    # Unlike stock_date below, blank here means "no change" rather than
    # "clear it" - a historically-unknowable cost can't be force-typed on
    # every edit (e.g. just bumping capacity), so leaving the field blank
    # must not silently erase an already-recorded value. Only a non-blank,
    # non-negative number ever touches the column.
    raw_cost_per_liter = request.form.get("cost_per_liter", "").strip()
    cost_per_liter = request.form.get("cost_per_liter", type=float) if raw_cost_per_liter else None
    stock_date, date_error = parse_stock_date(request.form.get("stock_date", ""))

    if not capacity or capacity <= 0:
        flash("Please enter a valid capacity.", "error")
    elif threshold is None or threshold < 0:
        flash("Please enter a valid low-stock alert level.", "error")
    elif stock is None or stock < 0 or stock > capacity:
        flash("Please enter a valid starting stock (not more than capacity).", "error")
    elif raw_cost_per_liter and (cost_per_liter is None or cost_per_liter < 0):
        flash("Please enter a valid cost per liter for the starting stock.", "error")
    elif date_error == "invalid":
        flash("Please enter a valid stock date.", "error")
    elif date_error == "future":
        flash("Stock date can't be in the future.", "error")
    else:
        tank.capacity_liters = capacity
        tank.low_stock_threshold = threshold
        tank.starting_stock_liters = stock
        if raw_cost_per_liter:
            tank.starting_stock_cost_per_liter = cost_per_liter
        # A blank date clears the baseline to NULL, i.e. "beginning of
        # time" - the same fallback every tank had before this column
        # existed, and the escape hatch for "I don't actually know when
        # this figure was measured".
        tank.starting_stock_date = stock_date
        db.session.commit()
        flash(f"Updated Tank {tank.number}.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/edit-price/<int:fuel_type_id>", methods=["POST"])
@login_required
@owner_required
def settings_edit_price(fuel_type_id):
    """Record what a fuel costs, effective from a given date.

    The effective date is required rather than assumed to be today: this
    form used to hardcode date.today(), which meant backfilling old
    records was impossible from here - typing May's price in August filed
    it as an August price, so every May entry still resolved to the
    August rate (see price_on_date()) and was billed at the wrong price.
    A price is a historical fact with a date, not a single current value.
    """
    fuel = db.session.get(FuelType, fuel_type_id) or abort(404)
    price = request.form.get("price", type=float)
    effective_date, date_error = parse_stock_date(request.form.get("effective_date", ""))

    if not price or price <= 0:
        flash("Please enter a valid price.", "error")
    elif date_error == "invalid":
        flash("Please enter a valid date for when this price took effect.", "error")
    elif date_error == "future":
        flash("A price can't take effect in the future.", "error")
    else:
        # Blank means today, matching what this form did before the date
        # field existed - so the common "price changed today" case still
        # works without touching the date.
        effective = effective_date or date.today()
        record_fuel_price(fuel, price, effective)
        db.session.commit()
        flash(f"Set {fuel.name} to Rs {format_number(price)}/L, effective {effective}.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/reprice", methods=["POST"])
@login_required
@owner_required
def settings_reprice():
    """Re-price existing entries in a date range against the corrected
    price history - see reprice_entries() in ledger_logic.py for what is
    and isn't touched.

    Two-step by design: the first submit only PREVIEWS (nothing is
    written, the session is rolled back), and the owner has to confirm
    before any money figure is rewritten. Rewriting historical figures in
    bulk is exactly the kind of thing that should never happen on one
    click - the preview shows every affected row's old and new total, so
    what's about to change is visible before it changes.
    """
    start, start_error = parse_stock_date(request.form.get("start_date", ""))
    end, end_error = parse_stock_date(request.form.get("end_date", ""))
    confirmed = request.form.get("confirm") == "yes"

    if start_error or end_error or not start or not end:
        flash("Please choose a valid start and end date for the re-price.", "error")
        return redirect(url_for("settings"))
    if start > end:
        flash("The start date must be on or before the end date.", "error")
        return redirect(url_for("settings"))

    if not confirmed:
        preview = reprice_entries(start, end, apply_changes=False)
        # Nothing was written, but the ORM may have loaded rows - roll back
        # so a preview can never leave anything half-applied.
        db.session.rollback()
        # Skipped (deliberate-discount) credits alone are not something to
        # confirm - there'd be nothing to apply - so say so plainly rather
        # than showing a preview page with an empty Apply panel.
        if not preview["count"]:
            msg = (
                f"Nothing to re-price between {start} and {end} - every entry there already "
                "matches the recorded price history."
            )
            if preview["skipped_credits"]:
                msg += (
                    f" ({len(preview['skipped_credits'])} credit entr"
                    f"{'y' if len(preview['skipped_credits']) == 1 else 'ies'} looked like a "
                    "deliberate discount and would be left alone in any case.)"
                )
            flash(msg, "info")
            return redirect(url_for("settings"))
        return render_template(
            "reprice_preview.html",
            start=start,
            end=end,
            preview=preview,
            today=date.today(),
        )

    result = reprice_entries(start, end, apply_changes=True)
    db.session.commit()
    flash(
        f"Re-priced {result['count']} entr{'y' if result['count'] == 1 else 'ies'} between "
        f"{start} and {end}. Total changed from Rs {format_number(result['old_total'])} to "
        f"Rs {format_number(result['new_total'])} "
        f"({'+' if result['difference'] >= 0 else ''}{format_number(result['difference'])}).",
        "success",
    )
    if result["skipped_credits"]:
        flash(
            f"{len(result['skipped_credits'])} credit entr"
            f"{'y was' if len(result['skipped_credits']) == 1 else 'ies were'} left alone because "
            "the amount doesn't match litres x price - those look like deliberate discounts, so "
            "they were not overwritten. Review them by hand if that's not right.",
            "info",
        )
    bad_date = first_negative_cash_date()
    if bad_date:
        flash(
            f"Heads up: cash in hand is now negative on {bad_date} - re-pricing changed how much "
            "cash each day brought in, so you may need to correct entries on or after that date.",
            "error",
        )
    return redirect(url_for("settings"))


@app.route("/settings/add-dispenser", methods=["POST"])
@login_required
@owner_required
def settings_add_dispenser():
    nozzle_count = request.form.get("nozzle_count", type=int) or 0
    tanks = Tank.query.order_by(Tank.number).all()
    nozzles = []
    error = None
    for n in range(nozzle_count):
        tank_id = request.form.get(f"tank_{n}", type=int)
        tank = db.session.get(Tank, tank_id) if tank_id else None
        if not tank:
            error = f"Nozzle {n + 1}: please choose a tank."
            break
        nozzles.append({"tank_id": tank.id})

    if not nozzles and not error:
        error = "Please add at least one nozzle."

    if error:
        flash(error, "error")
    else:
        number = (db.session.query(func.coalesce(func.max(Dispenser.number), 0)).scalar()) + 1
        dispenser = Dispenser(number=number)
        db.session.add(dispenser)
        db.session.flush()
        for n, nz in enumerate(nozzles):
            db.session.add(
                Nozzle(
                    dispenser_id=dispenser.id,
                    nozzle_number=n + 1,
                    tank_id=nz["tank_id"],
                )
            )
        db.session.commit()
        flash(f"Added Dispenser {number} with {len(nozzles)} nozzle(s).", "success")

    return redirect(url_for("settings"))


@app.route("/settings/add-nozzle", methods=["POST"])
@login_required
@owner_required
def settings_add_nozzle():
    dispenser_id = request.form.get("dispenser_id", type=int)
    tank_id = request.form.get("tank_id", type=int)

    dispenser = db.session.get(Dispenser, dispenser_id) if dispenser_id else None
    tank = db.session.get(Tank, tank_id) if tank_id else None

    if not dispenser:
        flash("Please choose a dispenser.", "error")
    elif not tank:
        flash("Please choose a tank.", "error")
    else:
        next_number = (
            db.session.query(func.coalesce(func.max(Nozzle.nozzle_number), 0))
            .filter(Nozzle.dispenser_id == dispenser.id)
            .scalar()
        ) + 1
        db.session.add(
            Nozzle(
                dispenser_id=dispenser.id,
                nozzle_number=next_number,
                tank_id=tank.id,
            )
        )
        db.session.commit()
        flash(f"Added Nozzle {next_number} to Dispenser {dispenser.number}.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/edit-nozzle-tank/<int:nozzle_id>", methods=["POST"])
@login_required
@owner_required
def settings_edit_nozzle_tank(nozzle_id):
    """Only allowed before the nozzle has any reading history - a Sale's
    fuel type is derived live from nozzle -> tank -> fuel_type (not frozen
    at Sale-creation time), so reassigning a nozzle that already has sales
    would silently relabel their historical fuel type too."""
    nozzle = db.session.get(Nozzle, nozzle_id) or abort(404)
    tank_id = request.form.get("tank_id", type=int)
    tank = db.session.get(Tank, tank_id) if tank_id else None

    if not tank:
        flash("Please choose a valid tank.", "error")
    elif Sale.query.filter_by(nozzle_id=nozzle.id).count() > 0:
        flash(
            f"Can't reassign {nozzle.label} - it already has reading history, "
            "and moving it now would silently relabel past sales.",
            "error",
        )
    else:
        nozzle.tank_id = tank.id
        db.session.commit()
        flash(f"{nozzle.label} reassigned to {tank.label}.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/delete-nozzle/<int:nozzle_id>", methods=["POST"])
@login_required
@owner_required
def settings_delete_nozzle(nozzle_id):
    nozzle = db.session.get(Nozzle, nozzle_id) or abort(404)

    if Sale.query.filter_by(nozzle_id=nozzle.id).count() > 0:
        flash(f"Can't delete {nozzle.label} - it already has reading history.", "error")
    else:
        label = nozzle.label
        db.session.delete(nozzle)
        db.session.commit()
        flash(f"Deleted {label}.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/delete-dispenser/<int:dispenser_id>", methods=["POST"])
@login_required
@owner_required
def settings_delete_dispenser(dispenser_id):
    dispenser = db.session.get(Dispenser, dispenser_id) or abort(404)
    nozzle_ids = [n.id for n in dispenser.nozzles]
    has_sales = nozzle_ids and Sale.query.filter(Sale.nozzle_id.in_(nozzle_ids)).count() > 0

    if has_sales:
        flash(f"Can't delete Dispenser {dispenser.number} - one of its nozzles already has reading history.", "error")
    else:
        for n in list(dispenser.nozzles):
            db.session.delete(n)
        db.session.delete(dispenser)
        db.session.commit()
        flash(f"Deleted Dispenser {dispenser.number}.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/delete-tank/<int:tank_id>", methods=["POST"])
@login_required
@owner_required
def settings_delete_tank(tank_id):
    tank = db.session.get(Tank, tank_id) or abort(404)
    nozzles = Nozzle.query.filter_by(tank_id=tank.id).all()
    nozzle_ids = [n.id for n in nozzles]
    has_sales = nozzle_ids and Sale.query.filter(Sale.nozzle_id.in_(nozzle_ids)).count() > 0
    has_direct_sales = DirectSale.query.filter_by(tank_id=tank.id).count() > 0
    has_purchases = StockPurchase.query.filter_by(tank_id=tank.id).count() > 0
    has_dips = TankDip.query.filter_by(tank_id=tank.id).count() > 0

    if has_sales or has_direct_sales or has_purchases or has_dips:
        flash(f"Can't delete {tank.label} - it already has purchase, sale, or dip history.", "error")
    else:
        for n in nozzles:
            db.session.delete(n)
        db.session.delete(tank)
        db.session.commit()
        flash(f"Deleted {tank.label}.", "success")

    return redirect(url_for("settings"))


# --------------------------------------------------- product catalogue ----



@app.route("/settings/add-product", methods=["POST"])
@login_required
@owner_required
def settings_add_product():
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "lubricant").strip() or "lubricant"
    if category not in PRODUCT_CATEGORIES:
        category = "lubricant"
    pack_size = request.form.get("pack_size", "").strip() or None
    unit = request.form.get("unit", "piece").strip() or "piece"
    if unit not in PRODUCT_UNITS:
        unit = "piece"
    purchase_rate = request.form.get("purchase_rate", type=float)
    retail_rate = request.form.get("retail_rate", type=float)
    opening_stock = request.form.get("opening_stock", type=float)
    low_stock_threshold = request.form.get("low_stock_threshold", type=float)
    stock_date, date_error = parse_stock_date(request.form.get("opening_stock_date", ""))

    existing = (
        Product.query.filter(func.lower(Product.name) == name.lower()).first() if name else None
    )

    if not name:
        flash("Please enter a product name.", "error")
    elif existing:
        flash(f"A product named \"{existing.name}\" already exists.", "error")
    elif purchase_rate is None or purchase_rate < 0:
        flash("Please enter a valid purchase rate.", "error")
    elif retail_rate is None or retail_rate < 0:
        flash("Please enter a valid retail rate.", "error")
    elif retail_rate < purchase_rate:
        flash(
            "Retail rate can't be less than the purchase rate - that's a guaranteed "
            "loss and almost always a typo.",
            "error",
        )
    elif opening_stock is None or opening_stock < 0:
        flash("Please enter a valid opening stock.", "error")
    elif date_error == "invalid":
        flash("Please enter a valid opening stock date.", "error")
    elif date_error == "future":
        flash("Opening stock date can't be in the future.", "error")
    elif low_stock_threshold is None or low_stock_threshold < 0:
        flash("Please enter a valid low-stock alert level.", "error")
    else:
        # Same convention as settings_add_tank(): a blank date defaults to
        # today for a brand-new product, rather than NULL - NULL is only
        # ever an explicit "I don't know when this was measured" (see
        # settings_edit_product()).
        opening_date = stock_date or date.today()
        product = Product(
            name=name,
            category=category,
            pack_size=pack_size,
            unit=unit,
            opening_stock=opening_stock,
            opening_stock_date=opening_date,
            low_stock_threshold=low_stock_threshold,
        )
        db.session.add(product)
        db.session.flush()
        record_product_rates(product, purchase_rate, retail_rate, opening_date)
        db.session.commit()
        flash(f"Added {product.label}.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/edit-product/<int:product_id>", methods=["POST"])
@login_required
@owner_required
def settings_edit_product(product_id):
    """Edit a product's catalogue details and its PURCHASE (indent) rate.

    The selling price is deliberately NOT editable here any more - it
    lives on the Inventory page (inventory_update_prices()), where the
    owner sets an effective date first and sees current prices and stock
    beside each other. Settings keeps the cost side only.

    Because ProductRateHistory stores both rates in one row, this route
    must still supply a retail rate when it writes history - and it takes
    the one already in effect ON THE EFFECTIVE DATE via
    product_rates_on_date(), passed straight through. Anything else
    (Product.retail_rate, or worse a form field) would let a cost
    correction silently reset the selling price the owner set on
    Inventory.
    """
    product = db.session.get(Product, product_id) or abort(404)
    name = request.form.get("name", "").strip()
    category = request.form.get("category", product.category).strip() or product.category
    pack_size = request.form.get("pack_size", "").strip() or None
    unit = request.form.get("unit", product.unit).strip() or product.unit
    purchase_rate = request.form.get("purchase_rate", type=float)
    opening_stock = request.form.get("opening_stock", type=float)
    low_stock_threshold = request.form.get("low_stock_threshold", type=float)
    stock_date, date_error = parse_stock_date(request.form.get("opening_stock_date", ""))
    rate_effective, rate_date_error = parse_stock_date(request.form.get("rate_effective_date", ""))

    existing = (
        Product.query.filter(func.lower(Product.name) == name.lower(), Product.id != product.id).first()
        if name
        else None
    )

    if not name:
        flash("Please enter a product name.", "error")
    elif existing:
        flash(f"A product named \"{existing.name}\" already exists.", "error")
    elif purchase_rate is None or purchase_rate < 0:
        flash("Please enter a valid purchase rate.", "error")
    elif opening_stock is None or opening_stock < 0:
        flash("Please enter a valid opening stock.", "error")
    elif date_error == "invalid":
        flash("Please enter a valid opening stock date.", "error")
    elif date_error == "future":
        flash("Opening stock date can't be in the future.", "error")
    elif low_stock_threshold is None or low_stock_threshold < 0:
        flash("Please enter a valid low-stock alert level.", "error")
    elif rate_date_error == "invalid":
        flash("Please enter a valid effective date for the rate change.", "error")
    elif rate_date_error == "future":
        flash("Rate effective date can't be in the future.", "error")
    else:
        product.name = name
        product.category = category if category in PRODUCT_CATEGORIES else product.category
        product.pack_size = pack_size
        product.unit = unit if unit in PRODUCT_UNITS else product.unit
        product.opening_stock = opening_stock
        # A blank date clears the baseline to NULL, i.e. "beginning of
        # time" - same escape hatch settings_edit_tank() gives a tank.
        product.opening_stock_date = stock_date
        product.low_stock_threshold = low_stock_threshold
        # Only a genuine rate CHANGE creates history - editing a typo'd
        # name/pack/threshold alongside unchanged rates must not leave a
        # spurious ProductRateHistory row behind.
        if purchase_rate != product.purchase_rate:
            # The retail rate in effect on the SAME date this cost change
            # lands on, written back unchanged - see the docstring.
            effective_on = rate_effective or date.today()
            _, existing_retail = product_rates_on_date(product, effective_on)
            record_product_rates(product, purchase_rate, existing_retail, effective_on)
        db.session.commit()
        flash(f"Updated {product.label}.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/toggle-product/<int:product_id>", methods=["POST"])
@login_required
@owner_required
def settings_toggle_product(product_id):
    product = db.session.get(Product, product_id) or abort(404)
    # Deactivating is always allowed - it never blocks or undoes anything
    # on file, it only hides the product from future sale pickers
    # (is_active is exactly the filter the next agent's entry forms rely
    # on to keep a retired SKU out of new sales).
    product.is_active = not product.is_active
    db.session.commit()
    flash(f"{'Reactivated' if product.is_active else 'Deactivated'} {product.label}.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/delete-product/<int:product_id>", methods=["POST"])
@login_required
@owner_required
def settings_delete_product(product_id):
    product = db.session.get(Product, product_id) or abort(404)
    has_sales = ProductSale.query.filter_by(product_id=product.id).count() > 0
    has_purchases = ProductPurchase.query.filter_by(product_id=product.id).count() > 0

    if has_sales or has_purchases:
        flash(
            f"Can't delete {product.label} - it already has sale or purchase history. "
            "Deactivate it instead.",
            "error",
        )
    else:
        label = product.label
        ProductRateHistory.query.filter_by(product_id=product.id).delete()
        db.session.delete(product)
        db.session.commit()
        flash(f"Deleted {label}.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/import-products", methods=["POST"])
@login_required
@owner_required
def settings_import_products():
    """Bulk upsert from a pasted price list - see settings_dip_chart() for
    the same tab-or-comma paste convention. This is the feature that makes
    the catalogue usable at all: ~95 SKUs (Shell Helix/Rimula/Ultra grades,
    dozens of vehicle-specific filters, etc.) can't be typed in one at a
    time, and re-pasting next month's price list has to UPDATE existing
    products' rates (recording history) rather than create 95 duplicates -
    matching is by name, case-insensitive, which is the whole point.
    """
    category = request.form.get("category", "lubricant").strip() or "lubricant"
    if category not in PRODUCT_CATEGORIES:
        category = "lubricant"
    unit = request.form.get("unit", "piece").strip() or "piece"
    if unit not in PRODUCT_UNITS:
        unit = "piece"
    effective_date, date_error = parse_stock_date(request.form.get("effective_date", ""))

    if date_error == "invalid":
        flash("Please enter a valid effective date.", "error")
        return redirect(url_for("settings"))
    if date_error == "future":
        flash("Effective date can't be in the future.", "error")
        return redirect(url_for("settings"))
    effective_date = effective_date or date.today()

    raw = request.form.get("catalogue", "")
    parsed = []
    errors = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip().replace("\t", ",")
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            errors.append(
                f"Line {lineno}: expected at least name, pack size, purchase rate, retail rate."
            )
            continue
        name = parts[0]
        pack_size = parts[1]
        pack_size = None if pack_size in ("", "-") else pack_size
        try:
            purchase_rate = float(parts[2])
            retail_rate = float(parts[3])
        except ValueError:
            errors.append(f"Line {lineno}: purchase/retail rate must be numbers.")
            continue
        # Whether column 6 (threshold) was actually present on this line,
        # as opposed to defaulting to 0 because it was omitted - an
        # UPDATE must tell those two apart (see below): a price-list
        # re-paste that only has 4 columns must never destroy a threshold
        # someone set separately via the single-row edit form.
        threshold_given = len(parts) > 5 and parts[5] != ""
        try:
            opening_stock = float(parts[4]) if len(parts) > 4 and parts[4] != "" else 0.0
            low_stock_threshold = float(parts[5]) if threshold_given else 0.0
        except ValueError:
            errors.append(f"Line {lineno}: opening stock/low-stock alert must be numbers.")
            continue
        if not name:
            errors.append(f"Line {lineno}: name is required.")
            continue
        if purchase_rate < 0 or retail_rate < 0 or opening_stock < 0 or low_stock_threshold < 0:
            errors.append(f"Line {lineno}: values can't be negative.")
            continue
        if retail_rate < purchase_rate:
            errors.append(f"Line {lineno}: retail rate can't be less than the purchase rate.")
            continue
        parsed.append(
            {
                "name": name,
                "pack_size": pack_size,
                "purchase_rate": purchase_rate,
                "retail_rate": retail_rate,
                "opening_stock": opening_stock,
                "low_stock_threshold": low_stock_threshold,
                "threshold_given": threshold_given,
            }
        )

    if errors:
        # All-or-nothing: a half-imported catalogue is worse than none,
        # since the next paste of the same list would then only be able to
        # tell it was already partially there by checking every row.
        for e in errors[:5]:
            flash(e, "error")
        if len(errors) > 5:
            flash(f"...and {len(errors) - 5} more error(s). Nothing was imported.", "error")
        else:
            flash("Nothing was imported.", "error")
        return redirect(url_for("settings"))

    if not parsed:
        flash("Nothing to import - paste at least one product line.", "error")
        return redirect(url_for("settings"))

    # A duplicate name within one paste has the LAST line win, the same
    # "seen" dict settings_dip_chart() uses - within a single batch that's
    # far more likely a paste mistake than two products meant to import as
    # separate rows.
    by_name = {}
    order = []
    for row in parsed:
        key = row["name"].lower()
        if key not in by_name:
            order.append(key)
        by_name[key] = row

    existing_products = {
        p.name.lower(): p for p in Product.query.filter(func.lower(Product.name).in_(order)).all()
    }

    created, updated, rate_changes = 0, 0, 0
    for key in order:
        row = by_name[key]
        product = existing_products.get(key)
        if product is None:
            product = Product(
                name=row["name"],
                category=category,
                pack_size=row["pack_size"],
                unit=unit,
                opening_stock=row["opening_stock"],
                opening_stock_date=effective_date,
                low_stock_threshold=row["low_stock_threshold"],
            )
            db.session.add(product)
            db.session.flush()
            record_product_rates(product, row["purchase_rate"], row["retail_rate"], effective_date)
            created += 1
            rate_changes += 1
        else:
            product.category = category
            product.pack_size = row["pack_size"]
            product.unit = unit
            # A re-paste must never DESTROY a value configured separately
            # from the catalogue (e.g. a threshold set via the single-row
            # edit form) just because this batch's line omitted that
            # optional column - only overwrite it when the line actually
            # supplied one. opening_stock has no equivalent branch: it's
            # simply never touched on an update (see the create branch
            # above), so there's nothing to protect there.
            if row["threshold_given"]:
                product.low_stock_threshold = row["low_stock_threshold"]
            if row["purchase_rate"] != product.purchase_rate or row["retail_rate"] != product.retail_rate:
                record_product_rates(product, row["purchase_rate"], row["retail_rate"], effective_date)
                rate_changes += 1
            updated += 1

    db.session.commit()
    flash(f"{created} product(s) created, {updated} updated, {rate_changes} rate change(s) recorded.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/reset-nozzle-meter", methods=["POST"])
@login_required
@owner_required
def settings_reset_nozzle_meter():
    """Marks a nozzle's physical meter as replaced/rolled over as of
    reset_date - see NozzleReset in models.py. From that date on, reading
    continuity (previous/floor/later-reading checks) is only enforced
    within the new era, so a lower reading right after doesn't get
    rejected as an error."""
    nozzle_id = request.form.get("nozzle_id", type=int)
    reset_date = parse_date_param(request.form.get("reset_date"))
    note = request.form.get("note", "").strip()
    nozzle = db.session.get(Nozzle, nozzle_id) if nozzle_id else None

    if not nozzle:
        flash("Please choose a valid nozzle.", "error")
    else:
        db.session.add(
            NozzleReset(
                nozzle_id=nozzle.id,
                reset_date=reset_date,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(
            f"{nozzle.label}'s meter is now treated as reset from {reset_date} onward - readings "
            "before that date are unaffected, but continuity is no longer enforced across it.",
            "success",
        )

    return redirect(url_for("settings"))


@app.route("/settings/add-bank-account", methods=["POST"])
@login_required
@owner_required
def settings_add_bank_account():
    name = request.form.get("name", "").strip()
    opening_balance = request.form.get("opening_balance", type=float) or 0
    raw_date = request.form.get("opening_balance_date", "").strip()
    opening_balance_date = parse_date_param(raw_date) if raw_date else None

    if not name:
        flash("Please enter a bank account name.", "error")
    elif BankAccount.query.filter(func.lower(BankAccount.name) == name.lower()).first():
        flash(f'A bank account named "{name}" already exists.', "error")
    elif opening_balance < 0:
        flash("Opening balance can't be negative.", "error")
    else:
        db.session.add(
            BankAccount(
                name=name,
                opening_balance=opening_balance,
                opening_balance_date=opening_balance_date,
            )
        )
        db.session.commit()
        flash(f'Added bank account "{name}".', "success")

    return redirect(url_for("settings"))


@app.route("/settings/add-user", methods=["POST"])
@login_required
@owner_required
def settings_add_user():
    """Owner-scoped user creation. The new row lands on the OWNER's OWN
    pump automatically - current_user is authenticated here, so
    tenancy.py's before_flush auto-stamp handles pump_id without this
    route setting it explicitly (verified: no pump_id= is passed to
    User() below). Username uniqueness is enforced PER PUMP, exactly
    like the check this replaced - User.query is itself tenant-filtered
    for an authenticated request, so this only ever matches a collision
    within the owner's own pump, never another pump's user."""
    username = request.form.get("username", "").strip()
    display_name = request.form.get("display_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "")

    errors = []
    if not username:
        errors.append("Please enter a username.")
    elif len(username) > 80:
        errors.append("Username is too long (80 characters max).")
    elif User.query.filter(func.lower(User.username) == username.lower()).first():
        errors.append(f'A user named "{username}" already exists on this pump.')

    if email:
        if not EMAIL_RE.match(email):
            errors.append("That doesn't look like a valid email address.")
        else:
            # unscoped(): email is GLOBALLY unique (User.email), not per
            # pump - checking it has to look across every pump. This
            # only ever produces a yes/no "is it taken" answer; it never
            # returns or reveals which pump/account holds it.
            with unscoped():
                email_taken = User.query.filter_by(email=email).first() is not None
            if email_taken:
                errors.append(f'"{email}" is already registered to another account.')

    if role not in ("owner", "staff"):
        errors.append("Please choose a role.")
    # Same bar as signup/reset - a staff login opens the same books the
    # owner's own does, so it can't be the weak way in (see
    # _password_errors). confirm=None: this form has one password box.
    errors.extend(_password_errors(password, None, username, email))

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("settings"))

    user = User(
        username=username,
        display_name=display_name or None,
        email=email or None,
        role=role,
    )
    user.set_password(password)
    db.session.add(user)
    try:
        db.session.commit()
        flash(f'Added {role} "{username}".', "success")
    except IntegrityError:
        # Pre-checks above are best-effort against a race (two
        # near-simultaneous adds); the DB's own unique constraints are
        # the real backstop. Fail with a clear message, not a raw 500.
        db.session.rollback()
        flash(
            f'Could not add "{username}" - that username or email is already in use.',
            "error",
        )

    return redirect(url_for("settings"))


def _issue_and_flash_invite(user, inviter):
    """Shared by settings_invite_user and settings_resend_invite: issue a
    fresh invite token, try to email it, and set up the flash/session
    state the Settings page reads back (see settings()'s
    session.pop("pending_invite_link", ...)).

    Wrapped exactly like forgot_password() wraps its own issue+send: a
    failure here must not 500 the settings page, and must not leave the
    (already-committed) user row stranded with no way to invite them
    again - Resend covers that, so there's nothing else to unwind."""
    try:
        raw = _issue_auth_token(user, "invite", INVITE_TOKEN_TTL_HOURS)
        link = url_for("accept_invite", token=raw, _external=True)
        sent = _send_invite_email(user, inviter, link)
    except Exception:
        app.logger.exception("invite: failed to issue/send for user %s", user.id)
        flash(
            "Could not create the invite link right now - please try Resend again shortly.",
            "error",
        )
        return

    session["pending_invite_link"] = link
    if email_service.is_configured() and sent:
        flash(f"Invitation sent to {user.email}.", "success")
    else:
        flash(
            "Email isn't configured on this deployment, so nothing was sent. "
            "Copy the invite link below and pass it on yourself.",
            "warning",
        )


@app.route("/settings/invite-user", methods=["POST"])
@login_required
@owner_required
def settings_invite_user():
    """Invite-based user creation: the owner supplies an email/role, the
    invitee sets their OWN password via accept_invite() - see the module
    docstring at the top of this file's Spec B section. Mirrors
    settings_add_user()'s validation for every field they share; the
    difference is no password field at all and is_active_user starts
    False."""
    username = request.form.get("username", "").strip()
    display_name = request.form.get("display_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "")

    errors = []
    if not username:
        errors.append("Please enter a username.")
    elif len(username) > 80:
        errors.append("Username is too long (80 characters max).")
    elif User.query.filter(func.lower(User.username) == username.lower()).first():
        errors.append(f'A user named "{username}" already exists on this pump.')

    if not email:
        errors.append("Please enter an email address.")
    elif not EMAIL_RE.match(email):
        errors.append("That doesn't look like a valid email address.")
    else:
        # unscoped(): email is GLOBALLY unique (User.email), not per pump
        # - see settings_add_user()'s identical comment above. Only ever
        # produces a yes/no "is it taken" answer.
        with unscoped():
            email_taken = User.query.filter_by(email=email).first() is not None
        if email_taken:
            errors.append(f'"{email}" is already registered to another account.')

    if role not in ("owner", "staff"):
        errors.append("Please choose a role.")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("settings"))

    user = User(
        username=username,
        display_name=display_name or None,
        email=email,
        role=role,
        is_active_user=False,
        invited_at=datetime.now(),
        email_verified_at=None,
    )
    user.set_unusable_password()
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(
            f'Could not invite "{username}" - that username or email is already in use.',
            "error",
        )
        return redirect(url_for("settings"))

    _issue_and_flash_invite(user, current_user)
    return redirect(url_for("settings"))


@app.route("/settings/users/<int:user_id>/resend-invite", methods=["POST"])
@login_required
@owner_required
def settings_resend_invite(user_id):
    """db.session.get() is tenant-filtered, so another pump's user (or
    user id) 404s here exactly like every other /settings/users/<id>/...
    route."""
    user = db.session.get(User, user_id) or abort(404)
    if not user.is_pending_invite:
        flash("That invite is no longer pending.", "error")
        return redirect(url_for("settings"))

    _issue_and_flash_invite(user, current_user)
    return redirect(url_for("settings"))


@app.route("/settings/users/<int:user_id>/cancel-invite", methods=["POST"])
@login_required
@owner_required
def settings_cancel_invite(user_id):
    """Hard delete rather than the deactivate every other user path uses
    (see settings_toggle_user) - safe ONLY here because a pending invitee
    has never logged in: login() refuses inactive users, and their
    password hash is the unusable sentinel (set_unusable_password), so
    they cannot have authored any row. Deleting them therefore cannot
    orphan any ledger row's user_id foreign key. Their PasswordResetToken
    rows are deleted first (FK)."""
    user = db.session.get(User, user_id) or abort(404)
    if not user.is_pending_invite:
        flash("That invite is no longer pending.", "error")
        return redirect(url_for("settings"))

    username = user.username
    with unscoped():
        PasswordResetToken.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    flash(f'Invitation to "{username}" cancelled.', "success")
    return redirect(url_for("settings"))


@app.route("/settings/pump-name", methods=["POST"])
@login_required
@owner_required
def settings_rename_pump():
    """The pump name is set once at signup and, until now, could never be
    corrected. Pump is NOT TenantScoped (see models.py) - loaded
    explicitly by current_user.pump_id, never from a form field, so
    there's no field an attacker could use to rename another pump."""
    name = request.form.get("name", "").strip()
    if not name:
        flash("Pump name can't be empty.", "error")
    elif len(name) > 120:
        flash("Pump name is too long (120 characters max).", "error")
    else:
        pump = db.session.get(Pump, current_user.pump_id)
        pump.name = name
        db.session.commit()
        flash("Pump name updated.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@owner_required
def settings_toggle_user(user_id):
    """Deactivate rather than delete - see the User docstring: entry
    history references user_id, so a user who has recorded anything has to
    stay resolvable. Guards against locking yourself out or removing the
    last active owner."""
    user = db.session.get(User, user_id) or abort(404)
    active_owners = User.query.filter_by(role="owner", is_active_user=True).count()

    if user.is_pending_invite:
        # Activating a pending invitee would be a one-way dead end: their
        # password is the unusable sentinel, so they still could not log
        # in, but is_pending_invite would flip to False and both Resend
        # and Cancel would then refuse them - leaving a user who can
        # never sign in, never be re-invited, and never be removed, while
        # permanently holding their (globally unique) email address.
        flash(
            f'"{user.username}" has not accepted their invitation yet - '
            "use Resend or Cancel instead.",
            "error",
        )
    elif user.id == current_user.id:
        flash("You can't deactivate your own account.", "error")
    elif user.is_active_user and user.is_owner and active_owners <= 1:
        flash("There has to be at least one active owner.", "error")
    else:
        user.is_active_user = not user.is_active_user
        db.session.commit()
        state = "reactivated" if user.is_active_user else "deactivated"
        flash(f'User "{user.username}" {state}.', "success")

    return redirect(url_for("settings"))


@app.route("/settings/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@owner_required
def settings_reset_user_password(user_id):
    user = db.session.get(User, user_id) or abort(404)
    password = request.form.get("password", "")

    # Same bar as every other password path (see _password_errors).
    # confirm=None: this inline form has a single password box.
    errors = _password_errors(password, None, user.username, user.email)
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        user.set_password(password)
        db.session.commit()
        flash(f'Password reset for "{user.username}".', "success")

    return redirect(url_for("settings"))


@app.route("/settings/add-shift", methods=["POST"])
@login_required
@owner_required
def settings_add_shift():
    name = request.form.get("name", "").strip()
    sort_order = request.form.get("sort_order", type=int)

    if not name:
        flash("Please enter a shift name.", "error")
    elif Shift.query.filter(func.lower(Shift.name) == name.lower()).first():
        flash(f'A shift named "{name}" already exists.', "error")
    else:
        if sort_order is None:
            sort_order = (db.session.query(func.coalesce(func.max(Shift.sort_order), -1)).scalar()) + 1
        db.session.add(Shift(name=name, sort_order=sort_order))
        db.session.commit()
        flash(f'Added shift "{name}".', "success")

    return redirect(url_for("settings"))


@app.route("/settings/shifts/<int:shift_id>/edit", methods=["POST"])
@login_required
@owner_required
def settings_edit_shift(shift_id):
    shift = db.session.get(Shift, shift_id) or abort(404)
    name = request.form.get("name", "").strip()
    sort_order = request.form.get("sort_order", type=int)

    clash = Shift.query.filter(func.lower(Shift.name) == name.lower(), Shift.id != shift.id).first()
    if not name:
        flash("Please enter a shift name.", "error")
    elif clash:
        flash(f'A shift named "{name}" already exists.', "error")
    else:
        shift.name = name
        if sort_order is not None:
            shift.sort_order = sort_order
        db.session.commit()
        flash("Shift updated.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/shifts/<int:shift_id>/toggle", methods=["POST"])
@login_required
@owner_required
def settings_toggle_shift(shift_id):
    """Deactivating hides a shift from new entries without disturbing rows
    already recorded against it. At least one active shift must remain,
    since every reading/credit/bank-sale needs one."""
    shift = db.session.get(Shift, shift_id) or abort(404)
    active_count = Shift.query.filter_by(is_active=True).count()

    if shift.is_active and active_count <= 1:
        flash("There has to be at least one active shift.", "error")
    else:
        shift.is_active = not shift.is_active
        db.session.commit()
        state = "reactivated" if shift.is_active else "deactivated"
        flash(f'Shift "{shift.name}" {state}.', "success")

    return redirect(url_for("settings"))


@app.route("/settings/dip-chart/<int:tank_id>", methods=["POST"])
@login_required
@owner_required
def settings_dip_chart(tank_id):
    """Replace a tank's calibration table wholesale from a pasted block of
    "depth_cm,liters" lines - that's how these charts arrive (a printed
    sheet from the tank maker), and editing 100+ rows individually would
    be unusable."""
    tank = db.session.get(Tank, tank_id) or abort(404)
    raw = request.form.get("chart", "")
    rows = []
    errors = []

    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip().replace("\t", ",")
        if not line:
            continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) < 2:
            errors.append(f"Line {lineno}: expected \"depth,liters\".")
            continue
        try:
            depth, liters = float(parts[0]), float(parts[1])
        except ValueError:
            errors.append(f"Line {lineno}: not valid numbers.")
            continue
        if depth < 0 or liters < 0:
            errors.append(f"Line {lineno}: values can't be negative.")
            continue
        rows.append((depth, liters))

    seen = {}
    for depth, liters in rows:
        seen[depth] = liters

    if errors:
        for e in errors[:5]:
            flash(e, "error")
    else:
        TankDipChart.query.filter_by(tank_id=tank.id).delete()
        for depth in sorted(seen):
            db.session.add(TankDipChart(tank_id=tank.id, depth_cm=depth, liters=seen[depth]))
        db.session.commit()
        if seen:
            flash(f"Saved a {len(seen)}-point dip chart for {tank.label}.", "success")
        else:
            flash(f"Cleared the dip chart for {tank.label}.", "success")

    return redirect(url_for("settings"))


@app.route("/settings/edit-cash-account", methods=["POST"])
@login_required
@owner_required
def settings_edit_cash_account():
    cash_account = get_cash_account()
    opening_balance = request.form.get("opening_balance", type=float)
    raw_date = request.form.get("opening_balance_date", "").strip()

    if opening_balance is None or opening_balance < 0:
        flash("Please enter a valid opening balance.", "error")
    else:
        cash_account.opening_balance = opening_balance
        cash_account.opening_balance_date = parse_date_param(raw_date) if raw_date else None
        db.session.commit()
        flash("Updated cash-in-hand opening balance.", "success")

    return redirect(url_for("settings"))
