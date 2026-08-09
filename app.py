import csv
import io
import os
import re
import secrets
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import (
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_migrate import stamp as migrate_stamp
from flask_migrate import upgrade as migrate_upgrade
from flask_wtf import CSRFProtect
from sqlalchemy import func, inspect
from sqlalchemy.exc import IntegrityError

import charts
from exports import build_pdf, build_xlsx
from extensions import db, login_manager, migrate
from ledger_logic import (
    account_ledger_events,
    active_shifts,
    attendant_variance_summary,
    bank_account_ledger_events,
    book_stock,
    cash_account_balance,
    cash_account_ledger_events,
    cash_would_go_negative,
    cogs_for_period,
    credit_aging,
    default_shift,
    first_negative_cash_date,
    fuel_sales_for_date,
    handover_rows_for_date,
    latest_reset_for,
    liters_from_dip_cm,
    max_cash_available_on,
    nearest_earlier_reading,
    next_sale_on_or_after,
    previous_reading_for,
    previous_slot,
    price_on_date,
    price_resolver,
    product_margin_for_period,
    product_rate_resolver,
    product_rates_on_date,
    product_stock,
    product_stock_summary,
    record_fuel_price,
    record_product_rates,
    sales_breakdown_for_date,
    stock_series,
    sync_sale_testing,
    weighted_avg_cost,
)
from models import (
    Account,
    BankAccount,
    BankSale,
    CashAccount,
    CashDeposit,
    CashHandover,
    CreditGiven,
    Dispenser,
    EmployeeLoan,
    Expense,
    FuelPriceHistory,
    FuelType,
    Nozzle,
    NozzleReset,
    NozzleTesting,
    Product,
    ProductPurchase,
    ProductRateHistory,
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
    TankDipChart,
    User,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")

# On a serverless host (Vercel) the project directory is read-only and
# there is no local Postgres-vs-SQLite choice to make - DATABASE_URL is
# always set there. Only touch the filesystem (instance/ dir, secret key
# file) when running locally without it, so this stays a no-op in prod.
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    os.makedirs(INSTANCE_DIR, exist_ok=True)


def get_or_create_secret_key():
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_path = os.path.join(INSTANCE_DIR, "secret_key.txt")
    if os.path.exists(key_path):
        with open(key_path, "r") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(key_path, "w") as f:
        f.write(key)
    return key


app = Flask(__name__)
app.config["SECRET_KEY"] = get_or_create_secret_key()
if DATABASE_URL:
    # Postgres providers (Neon, Vercel Postgres, etc.) commonly hand out
    # "postgres://" URLs - SQLAlchemy 2.x / psycopg2 require "postgresql://".
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(
        INSTANCE_DIR, "petrolpump.db"
    )
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
# Batch mode rewrites the whole table for an ALTER on SQLite (which can't
# ALTER a column/constraint in place) - only needed there; Postgres in
# production applies migrations directly. directory is explicit (rather
# than the default relative "migrations") because a serverless host's
# cwd at cold start isn't guaranteed to be the project root.
migrate.init_app(
    app,
    db,
    directory=os.path.join(BASE_DIR, "migrations"),
    render_as_batch=app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"),
)
login_manager.init_app(app)
csrf = CSRFProtect(app)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def owner_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_owner:
            abort(403)
        return f(*args, **kwargs)

    return wrapped


def parse_date_param(raw, fallback=None):
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    return fallback or date.today()


def parse_stock_date(raw):
    """Parse a tank's starting-stock as-of date. Unlike parse_date_param,
    this can't silently fall back to today - a wrong physical-stock date
    corrupts every backward/forward book_stock() calculation for that
    tank, so a malformed or future date has to be rejected rather than
    guessed at.

    Returns (date_or_None, error) - blank input parses to (None, None),
    meaning "no baseline date" (the caller decides whether that means
    "default to today" or "clear it to NULL"). error is None on success,
    else "invalid" or "future" for the caller to turn into a
    field-specific flash message."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None, "invalid"
    if parsed > date.today():
        return None, "future"
    return parsed, None


def setup_is_complete():
    return Dispenser.query.count() > 0


def get_cash_account():
    """Cash-in-hand is a singleton - there's only one register. Lazily
    create it on first access rather than requiring a setup step."""
    cash_account = CashAccount.query.first()
    if not cash_account:
        cash_account = CashAccount(opening_balance=0)
        db.session.add(cash_account)
        db.session.commit()
    return cash_account


def would_overdraw_cash(amount, entry_date, old_amount=0, old_date=None):
    """True if spending `amount` in cash on entry_date would push the
    cash-in-hand register below zero on that date or any later date - a
    physical cash drawer can't go negative. For an in-place edit rather
    than a brand-new entry, old_amount/old_date describe the value being
    replaced (old_date defaults to entry_date), so the entry's old amount
    is restored before the new one is applied rather than double-counted.

    This delegates to cash_would_go_negative() for the actual simulation,
    since whether this is safe can depend on dates other than entry_date -
    see that function's docstring for why a whole-history, date-aware
    check is necessary rather than comparing against today's total."""
    changes = [(entry_date, -amount)]
    if old_amount:
        changes.append((old_date or entry_date, old_amount))
    return cash_would_go_negative(changes)


def cash_shortfall_message(entry_date):
    """Standard wording for a would_overdraw_cash() rejection, shared by
    every route that calls it - reports the date-aware ceiling rather
    than today's whole-history balance, since that's what the guard
    actually checked against."""
    return (
        f"Not enough cash in hand on {entry_date} for this (at most "
        f"Rs {max_cash_available_on(entry_date):,.2f} is available then without going negative later)."
    )


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _resolve_export_format():
    """?format=pdf|xlsx on every export route, defaulting to pdf. Anything
    else falls back to pdf with a flash rather than a hard 400 - a stray
    or stale query string shouldn't dead-end someone trying to get a
    report out."""
    fmt = request.args.get("format", "pdf")
    if fmt not in ("pdf", "xlsx"):
        flash(f'Unrecognized export format "{fmt}" - showing a PDF instead.', "error")
        return "pdf"
    return fmt


def _send_export(fmt, pdf_title, pdf_subtitle, xlsx_sheet_name, blocks, filename_base):
    """Shared tail end of every export route once its blocks are built."""
    if fmt == "xlsx":
        buffer = build_xlsx([{"name": xlsx_sheet_name, "blocks": blocks}])
        return send_file(
            buffer, mimetype=XLSX_MIME, as_attachment=True, download_name=f"{filename_base}.xlsx"
        )
    buffer = build_pdf(pdf_title, pdf_subtitle, blocks)
    return send_file(
        buffer, mimetype="application/pdf", as_attachment=True, download_name=f"{filename_base}.pdf"
    )


def slugify(text):
    """Lowercase, hyphenated, filesystem/URL-safe version of text - used
    for download filenames (e.g. an account's name) that might otherwise
    contain spaces or punctuation."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "account"


@app.before_request
def enforce_setup_flow():
    if request.endpoint in (None, "static", "login", "logout", "change_password"):
        return
    if not current_user.is_authenticated:
        return

    is_setup_route = request.endpoint.startswith("setup_")

    if not setup_is_complete():
        if is_setup_route:
            if not current_user.is_owner:
                abort(403)
            return
        if current_user.is_owner:
            return redirect(url_for("setup_tanks"))
        return render_template("setup_waiting.html")
    else:
        if is_setup_route:
            return redirect(url_for("ledger"))


# ---------------------------------------------------------------- auth ----

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("ledger"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            if not user.is_active_user:
                flash("That account has been deactivated. Ask the owner to re-enable it.", "error")
                return render_template("login.html")
            login_user(user)
            return redirect(url_for("ledger"))
        flash("Incorrect username or password.", "error")

    return render_template("login.html")


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Self-service, available to owner and staff alike - previously there
    was no way to change a password at all, which left the seeded
    owner/staff defaults in place on a publicly reachable deployment."""
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not current_user.check_password(current):
            flash("Your current password is incorrect.", "error")
        elif len(new) < 6:
            flash("New password must be at least 6 characters.", "error")
        elif new != confirm:
            flash("The two new passwords don't match.", "error")
        else:
            current_user.set_password(new)
            db.session.commit()
            flash("Password changed.", "success")
            return redirect(url_for("ledger"))

    return render_template("change_password.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def home():
    return redirect(url_for("ledger"))


# -------------------------------------------------------------- setup -----

@app.route("/setup/tanks", methods=["GET", "POST"])
@login_required
@owner_required
def setup_tanks():
    if request.method == "POST":
        count = request.form.get("tank_count", type=int) or 0
        tanks = []
        error = None
        for i in range(count):
            fuel_name = request.form.get(f"fuel_name_{i}", "").strip()
            capacity = request.form.get(f"capacity_{i}", type=float)
            stock = request.form.get(f"stock_{i}", type=float)
            cost_per_liter = request.form.get(f"cost_per_liter_{i}", type=float)
            raw_stock_date = request.form.get(f"stock_date_{i}", "").strip()
            if not fuel_name:
                error = f"Tank {i + 1}: please enter a fuel name."
                break
            if not capacity or capacity <= 0:
                error = f"Tank {i + 1}: please enter a valid capacity."
                break
            if stock is None or stock < 0:
                error = f"Tank {i + 1}: please enter a valid current stock."
                break
            if stock > capacity:
                error = f"Tank {i + 1}: current stock can't exceed capacity."
                break
            if cost_per_liter is None or cost_per_liter < 0:
                error = f"Tank {i + 1}: please enter a valid cost per liter."
                break
            stock_date, date_error = parse_stock_date(raw_stock_date)
            if date_error == "invalid":
                error = f"Tank {i + 1}: please enter a valid stock date."
                break
            if date_error == "future":
                error = f"Tank {i + 1}: stock date can't be in the future."
                break
            stock_date = stock_date or date.today()
            # Stored as an ISO string, not a date - the session is JSON-serialised
            # into a cookie between setup steps, and a date object won't survive that.
            # cost_per_liter is a plain float - JSON-serializes fine as-is,
            # unlike stock_date it needs no string round-trip.
            tanks.append(
                {
                    "fuel_name": fuel_name,
                    "capacity": capacity,
                    "stock": stock,
                    "cost_per_liter": cost_per_liter,
                    "stock_date": stock_date.isoformat(),
                }
            )

        if not tanks and not error:
            error = "Please add at least one tank."

        if error:
            flash(error, "error")
        else:
            session["setup"] = {"tanks": tanks}
            return redirect(url_for("setup_prices"))

    return render_template("setup_tanks.html", today=date.today())


@app.route("/setup/prices", methods=["GET", "POST"])
@login_required
@owner_required
def setup_prices():
    setup = session.get("setup")
    if not setup or "tanks" not in setup:
        return redirect(url_for("setup_tanks"))

    seen = {}
    for t in setup["tanks"]:
        key = t["fuel_name"].lower()
        if key not in seen:
            seen[key] = t["fuel_name"]
    fuel_names = list(seen.values())

    if request.method == "POST":
        prices = {}
        error = None
        for i, name in enumerate(fuel_names):
            price = request.form.get(f"price_{i}", type=float)
            if not price or price <= 0:
                error = f"Please enter a valid price for {name}."
                break
            prices[name.lower()] = price

        if error:
            flash(error, "error")
        else:
            setup["fuel_prices"] = prices
            session["setup"] = setup
            return redirect(url_for("setup_dispensers"))

    return render_template("setup_prices.html", fuel_names=fuel_names)


@app.route("/setup/dispensers", methods=["GET", "POST"])
@login_required
@owner_required
def setup_dispensers():
    setup = session.get("setup")
    if not setup or "fuel_prices" not in setup:
        return redirect(url_for("setup_tanks"))

    tank_options = [
        {"index": i, "label": f"Tank {i + 1} - {t['fuel_name']} ({t['capacity']:,.0f} L)"}
        for i, t in enumerate(setup["tanks"])
    ]

    if request.method == "POST":
        dispenser_count = request.form.get("dispenser_count", type=int) or 0
        dispensers = []
        error = None
        for d in range(dispenser_count):
            nozzle_count = request.form.get(f"nozzle_count_{d}", type=int) or 0
            if nozzle_count < 1:
                error = f"Dispenser {d + 1}: please add at least one nozzle."
                break
            nozzles = []
            for n in range(nozzle_count):
                tank_index = request.form.get(f"tank_{d}_{n}", type=int)
                if tank_index is None or not (0 <= tank_index < len(setup["tanks"])):
                    error = f"Dispenser {d + 1}, Nozzle {n + 1}: please choose a tank."
                    break
                nozzles.append({"tank_index": tank_index})
            if error:
                break
            dispensers.append(nozzles)

        if not dispensers and not error:
            error = "Please add at least one dispenser."

        if error:
            flash(error, "error")
        else:
            fuel_type_by_name = {}
            for name_lower, price in setup["fuel_prices"].items():
                display_name = next(
                    t["fuel_name"] for t in setup["tanks"] if t["fuel_name"].lower() == name_lower
                )
                fuel_type = FuelType(name=display_name, price_per_liter=price)
                db.session.add(fuel_type)
                fuel_type_by_name[name_lower] = fuel_type
            db.session.flush()
            for fuel_type in fuel_type_by_name.values():
                db.session.add(
                    FuelPriceHistory(
                        fuel_type_id=fuel_type.id,
                        price_per_liter=fuel_type.price_per_liter,
                        effective_date=date.today(),
                    )
                )

            created_tanks = []
            for i, t in enumerate(setup["tanks"]):
                # stock_date was serialised to an ISO string for the session
                # cookie (see setup_tanks()) - parse it back to a date here.
                raw_stock_date = t.get("stock_date")
                stock_date = (
                    datetime.strptime(raw_stock_date, "%Y-%m-%d").date() if raw_stock_date else None
                )
                tank = Tank(
                    number=i + 1,
                    fuel_type_id=fuel_type_by_name[t["fuel_name"].lower()].id,
                    capacity_liters=t["capacity"],
                    starting_stock_liters=t["stock"],
                    starting_stock_date=stock_date,
                    starting_stock_cost_per_liter=t["cost_per_liter"],
                    low_stock_threshold=round(t["capacity"] * 0.1, 2),
                )
                db.session.add(tank)
                created_tanks.append(tank)
            db.session.flush()

            for d, nozzles in enumerate(dispensers):
                dispenser = Dispenser(number=d + 1)
                db.session.add(dispenser)
                db.session.flush()
                for n, nz in enumerate(nozzles):
                    db.session.add(
                        Nozzle(
                            dispenser_id=dispenser.id,
                            nozzle_number=n + 1,
                            tank_id=created_tanks[nz["tank_index"]].id,
                        )
                    )

            db.session.commit()
            session.pop("setup", None)
            flash("Setup complete! Petrol Khata is ready to use.", "success")
            return redirect(url_for("ledger"))

    return render_template("setup_dispensers.html", tank_options=tank_options)


# ------------------------------------------------------------ settings ----

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
]


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
    fuel = db.session.get(FuelType, fuel_type_id) or abort(404)
    price = request.form.get("price", type=float)

    if not price or price <= 0:
        flash("Please enter a valid price.", "error")
    else:
        record_fuel_price(fuel, price, date.today())
        db.session.commit()
        flash(f"Updated price for {fuel.name}.", "success")

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
    has_purchases = StockPurchase.query.filter_by(tank_id=tank.id).count() > 0
    has_dips = TankDip.query.filter_by(tank_id=tank.id).count() > 0

    if has_sales or has_purchases or has_dips:
        flash(f"Can't delete {tank.label} - it already has purchase, sale, or dip history.", "error")
    else:
        for n in nozzles:
            db.session.delete(n)
        db.session.delete(tank)
        db.session.commit()
        flash(f"Deleted {tank.label}.", "success")

    return redirect(url_for("settings"))


# --------------------------------------------------- product catalogue ----

PRODUCT_CATEGORIES = ("lubricant", "filter", "shop", "other")
PRODUCT_UNITS = ("piece", "litre")


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
    product = db.session.get(Product, product_id) or abort(404)
    name = request.form.get("name", "").strip()
    category = request.form.get("category", product.category).strip() or product.category
    pack_size = request.form.get("pack_size", "").strip() or None
    unit = request.form.get("unit", product.unit).strip() or product.unit
    purchase_rate = request.form.get("purchase_rate", type=float)
    retail_rate = request.form.get("retail_rate", type=float)
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
        if purchase_rate != product.purchase_rate or retail_rate != product.retail_rate:
            record_product_rates(product, purchase_rate, retail_rate, rate_effective or date.today())
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
    username = request.form.get("username", "").strip()
    display_name = request.form.get("display_name", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "")

    if not username:
        flash("Please enter a username.", "error")
    elif User.query.filter(func.lower(User.username) == username.lower()).first():
        flash(f'A user named "{username}" already exists.', "error")
    elif role not in ("owner", "staff"):
        flash("Please choose a role.", "error")
    elif len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
    else:
        user = User(username=username, display_name=display_name or None, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash(f'Added {role} "{username}".', "success")

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

    if user.id == current_user.id:
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

    if len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
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


# -------------------------------------------------------------- ledger ----

def get_feed_for_date(entry_date, full_visibility):
    events = []
    for s in Sale.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "sale", "sort": s.recorded_at, "obj": s})
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
    for nt in NozzleTesting.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "nozzle_testing", "sort": nt.recorded_at, "obj": nt})

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
    # Every picker on this page (customer, supplier, employee) lists every
    # account - an account's type label is just a default/hint, not a
    # restriction, so any account can receive any kind of entry (e.g. an
    # account labelled "supplier" can still be given customer credit).
    # Each picker still sorts its own "relevant" type first, though, so
    # the common case doesn't require scrolling past every other type.
    accounts = Account.query.order_by(Account.name).all()
    accounts_customer_first = prioritize_accounts(accounts, "customer")
    accounts_supplier_first = prioritize_accounts(accounts, "supplier")
    accounts_employee_first = prioritize_accounts(accounts, "employee")
    accounts_owner_first = prioritize_accounts(accounts, "owner")
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()

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
        cash_balance = cash_account_balance(get_cash_account())

    return render_template(
        "ledger.html",
        selected_date=selected_date,
        today=date.today(),
        shifts=shifts,
        selected_shift=selected_shift,
        nozzle_rows=nozzle_rows,
        tank_rows=tank_rows,
        handover_rows=handover_rows,
        fuel_types=fuel_types,
        fuel_prices_by_id=fuel_prices_by_id,
        accounts=accounts,
        accounts_customer_first=accounts_customer_first,
        accounts_supplier_first=accounts_supplier_first,
        accounts_employee_first=accounts_employee_first,
        accounts_owner_first=accounts_owner_first,
        bank_accounts=bank_accounts,
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
        flash(f"Updated {fuel.name} price to Rs {price:,.2f}/L, effective {entry_date}.", "success")

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


def resolve_payment_method(form, field="paid_via", new_field="new_bank_account_name"):
    """Shared "Paid via" lookup for receipts, employee loans, and expenses:
    either plain cash, or a specific bank account (existing or quick-added
    inline, same __new__ convention as the other account pickers).
    Returns (method, bank_account_or_None, error)."""
    value = form.get(field, "cash")
    if value in ("", "cash"):
        return "cash", None, None

    if value == "__new__":
        name = form.get(new_field, "").strip()
        if not name:
            return None, None, "Please enter a name for the new bank account."
        bank_account = BankAccount(name=name)
        db.session.add(bank_account)
        db.session.flush()
        return "bank", bank_account, None

    bank_account = db.session.get(BankAccount, int(value))
    if not bank_account:
        return None, None, "Please choose a valid payment method."
    return "bank", bank_account, None


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
                    f"{shift.name}: short by Rs {abs(variance):,.2f} "
                    f"(expected Rs {expected:,.2f}, counted Rs {declared:,.2f}).",
                    "error",
                )
            else:
                flash(
                    f"{shift.name}: over by Rs {variance:,.2f} "
                    f"(expected Rs {expected:,.2f}, counted Rs {declared:,.2f}).",
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
        flash(f"Recorded Rs {shortfall:,.2f} shortfall as a cash expense.", "success")

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
            f"{employee.name} only owes Rs {max(outstanding, 0):,.2f}, so you can't deduct "
            f"Rs {deduction:,.2f}.",
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
                f"Paid {employee.name} Rs {net:,.2f} (Rs {gross:,.2f} salary less "
                f"Rs {deduction:,.2f} against their advance).",
                "success",
            )
        else:
            flash(f"Paid {employee.name} Rs {net:,.2f} salary.", "success")

    return redirect(url_for("ledger", date=entry_date))


def resolve_receipt_account(form):
    return resolve_account(form, "account_id", "new_account_name", "customer", "account", "new_account_phone")


@app.route("/ledger/receipt", methods=["POST"])
@login_required
def ledger_receipt():
    entry_date = parse_date_param(request.form.get("entry_date"))
    account, error = resolve_receipt_account(request.form)
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
        flash(f"Recorded receipt of Rs {amount:,.2f} from {account.name}.", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/credit", methods=["POST"])
@login_required
def ledger_credit():
    """Fuel already sold (see CreditGiven's docstring in models.py) handed
    to a customer on account instead of collected as cash. entry_mode
    decides which of liters/amount is what the user actually typed and
    which is derived from it via this date's price:

    - "liters" (default): liters is entered, amount = liters * price -
      unchanged from this route's original behaviour.
    - "amount": amount is entered EXACTLY as typed (never recomputed), and
      liters is derived from it purely for record-keeping (it doesn't
      touch stock either way - the Sale already did). This is how a
      discount that doesn't cleanly equal liters * price gets recorded:
      the discount lives entirely in the gap between amount and
      liters * price_per_liter.

    Fuel type is required in both modes - price_on_date() needs it
    regardless of which direction the calculation runs.
    """
    entry_date = parse_date_param(request.form.get("entry_date"))
    customer, error = resolve_customer(request.form)
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    entry_mode = request.form.get("entry_mode", "liters")
    if entry_mode not in ("liters", "amount"):
        entry_mode = "liters"
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
    elif entry_mode == "liters" and (not liters_in or liters_in <= 0):
        db.session.rollback()
        flash("Liters must be a positive number.", "error")
    elif entry_mode == "amount" and (not amount_in or amount_in <= 0):
        db.session.rollback()
        flash("Amount must be a positive number.", "error")
    elif entry_mode == "amount" and price_on_date(fuel, entry_date) <= 0:
        db.session.rollback()
        flash("This fuel has no price set yet - please set a price before recording credit by amount.", "error")
    else:
        price = price_on_date(fuel, entry_date)
        if entry_mode == "amount":
            amount = amount_in
            liters = round(amount_in / price, 2)
        else:
            liters = liters_in
            amount = round(liters * price, 2)
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
                f"Recorded Rs {amount:,.2f} ({liters:g} L equiv.) {fuel.name} on credit for {customer.name}.",
                "success",
            )
        else:
            flash(
                f"Recorded {liters:g} L {fuel.name} (Rs {amount:,.2f}) on credit for {customer.name}.",
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
                f"Recorded return of {liters:g} L {fuel.name} into {tank.label} (Rs {amount:,.2f}).",
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
        flash(f"Logged expense: {category} - Rs {amount:,.2f}", "success")

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

    if product_error:
        db.session.rollback()
        flash(product_error, "error")
    elif not quantity or quantity <= 0:
        db.session.rollback()
        flash("Quantity must be a positive number.", "error")
    elif method_error:
        db.session.rollback()
        flash(method_error, "error")
    else:
        # Resolved for entry_date, never read from the product's cached
        # rate directly - a backdated sale has to snapshot the rates that
        # were actually in effect on ITS date (see product_rates_on_date()).
        purchase_rate, retail_rate = product_rates_on_date(product, entry_date)
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
        flash(f"Recorded sale of {quantity:g} {product.label} (Rs {amount:,.2f}).", "success")
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
            flash(f"{verb} {abs(quantity):g} {product.label} (Rs {abs(total_cost):,.2f}).", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/supplier-payment", methods=["POST"])
@login_required
@owner_required
def ledger_supplier_payment():
    entry_date = parse_date_param(request.form.get("entry_date"))
    supplier, error = resolve_supplier(request.form)
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)

    if error:
        db.session.rollback()
        flash(error, "error")
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
        flash(f"Recorded payment of Rs {amount:,.2f} to {supplier.name}.", "success")

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
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Amount must be a positive number.", "error")
    elif would_overdraw_cash(amount, entry_date):
        db.session.rollback()
        flash(cash_shortfall_message(entry_date), "error")
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
        flash(f"Recorded Rs {amount:,.2f} bank sale to {bank_account.name}.", "success")

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
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Amount must be a positive number.", "error")
    elif would_overdraw_cash(amount, entry_date):
        db.session.rollback()
        flash(cash_shortfall_message(entry_date), "error")
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
        flash(f"Recorded deposit of Rs {amount:,.2f} to {bank_account.name}.", "success")

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
            flash(f"Recorded drawing of Rs {amount:,.2f} for {account.name}.", "success")
        else:
            flash(f"Recorded loan/advance of Rs {amount:,.2f} to {account.name}.", "success")

    return redirect(url_for("ledger", date=entry_date))


# Every entry kind that can be deleted, keyed by the URL segment used in
# entry_delete() below - one generic route instead of twelve near-identical
# ones. Adding a new deletable kind later is just one more line here.
DELETABLE_ENTRIES = {
    "sale": (Sale, "nozzle reading"),
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


# ------------------------------------------------------------ dashboard ---

@app.route("/dashboard")
@login_required
def dashboard():
    today = date.today()

    today_sales_total = (
        db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
        .filter(Sale.entry_date == today)
        .scalar()
    )
    today_liters = (
        db.session.query(func.coalesce(func.sum(Sale.liters), 0))
        .filter(Sale.entry_date == today)
        .scalar()
    )
    today_sale_count = Sale.query.filter_by(entry_date=today).count()

    tanks = Tank.query.order_by(Tank.number).all()
    tank_rows = [{"tank": t, "stock": book_stock(t, today)} for t in tanks]
    low_stock = [r for r in tank_rows if r["stock"] <= r["tank"].low_stock_threshold]

    context = dict(
        today_total=today_sales_total,
        today_liters=today_liters,
        today_sale_count=today_sale_count,
        tank_rows=tank_rows,
        low_stock=low_stock,
    )

    if current_user.is_owner:
        today_expenses = (
            db.session.query(func.coalesce(func.sum(Expense.amount), 0))
            .filter(Expense.entry_date == today)
            .scalar()
        )
        today_credit_given = (
            db.session.query(func.coalesce(func.sum(CreditGiven.amount), 0))
            .filter(CreditGiven.entry_date == today)
            .scalar()
        )
        # Debitors/creditors are now determined by each account's current
        # balance sign, not by a fixed type label - an account can owe us
        # money from one kind of entry while we owe it money from another.
        all_balances = [a.balance for a in Account.query.all()]
        outstanding_credit = sum(b for b in all_balances if b > 0)
        outstanding_supplier = -sum(b for b in all_balances if b < 0)
        bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
        cash_account = get_cash_account()
        context.update(
            today_expenses=today_expenses,
            today_credit_given=today_credit_given,
            outstanding_credit=outstanding_credit,
            outstanding_supplier=outstanding_supplier,
            bank_accounts=bank_accounts,
            cash_balance=cash_account_balance(cash_account),
        )

    return render_template("dashboard.html", **context)


# ------------------------------------------------------------ inventory ---

@app.route("/inventory")
@login_required
def inventory():
    today = date.today()
    tanks = Tank.query.order_by(Tank.number).all()
    tank_rows = []
    by_fuel = {}
    for t in tanks:
        stock = book_stock(t, today)
        tank_rows.append({"tank": t, "stock": stock, "is_low": stock <= t.low_stock_threshold})
        agg = by_fuel.setdefault(t.fuel_type.name, {"stock": 0.0, "capacity": 0.0})
        agg["stock"] += stock
        agg["capacity"] += t.capacity_liters

    dispensers = Dispenser.query.order_by(Dispenser.number).all()
    recent_purchases = (
        StockPurchase.query.order_by(StockPurchase.recorded_at.desc()).limit(15).all()
    )
    # Any account with at least one credit purchase against it, regardless
    # of its type label (an account doesn't have to be labelled "supplier"
    # to have sold us fuel on credit).
    suppliers = sorted(
        (a for a in Account.query.all() if any(p.payment_type == "credit" for p in a.stock_purchases)),
        key=lambda a: a.name.lower(),
    )

    # Sorted by category first so lubricants/filters/shop items each form
    # a visible block rather than being interleaved alphabetically - shop
    # profit is reported separately precisely because it's a different
    # kind of line, and the table should read that way too.
    products = (
        Product.query.filter_by(is_active=True)
        .order_by(Product.category, func.lower(Product.name))
        .all()
    )
    product_rows = product_stock_summary(today, products=products)

    return render_template(
        "inventory.html",
        tank_rows=tank_rows,
        by_fuel=by_fuel,
        dispensers=dispensers,
        recent_purchases=recent_purchases,
        suppliers=suppliers,
        product_rows=product_rows,
    )


# ------------------------------------------------------------ accounts ---

ACCOUNT_TYPES = ("customer", "supplier", "employee", "owner")


def _accounts_context(kind, type_filter):
    """Shared by the Accounts page and its PDF/Excel export, so the two
    can never quietly drift apart - same filters, same rows, same aging
    totals, just rendered differently."""
    if type_filter in ("bank", "cash"):
        # Debtor/creditor is a concept that only applies to customer/
        # supplier/employee accounts - bank accounts and cash-in-hand are
        # the pump's own money, not a relationship with someone else, so
        # they always show under "All" regardless of a stale kind= param.
        kind = "all"

    rows = []
    if type_filter not in ("bank", "cash"):
        query = Account.query
        if type_filter in ACCOUNT_TYPES:
            query = query.filter_by(account_type=type_filter)
        for a in query.all():
            # Debitor/creditor is purely a function of the account's current
            # balance sign - not its type label - so an account's
            # classification here can shift over time as its balance shifts.
            balance = a.balance
            aging = credit_aging(a, date.today()) if balance > 0 else None
            rows.append(
                {
                    "kind": "account",
                    "obj": a,
                    "name": a.name,
                    "balance": balance,
                    "aging": aging,
                }
            )

    if kind == "all" and type_filter in ("all", "bank"):
        for b in BankAccount.query.all():
            # Bank accounts (and cash-in-hand) are the pump's own money,
            # not a debitor/creditor relationship, so they only show up
            # under "All" - not under the Debitors/Creditors filter.
            rows.append({"kind": "bank", "obj": b, "name": b.name, "balance": b.balance})

    if kind == "all" and type_filter in ("all", "cash"):
        cash_account = get_cash_account()
        rows.append(
            {
                "kind": "cash",
                "obj": cash_account,
                "name": "Cash in Hand",
                "balance": cash_account_balance(cash_account),
            }
        )

    if kind == "debitors":
        rows = [r for r in rows if r["balance"] > 0]
    elif kind == "creditors":
        rows = [r for r in rows if r["balance"] < 0]

    rows.sort(key=lambda r: r["name"].lower())

    expenses = Expense.query.order_by(Expense.entry_date.desc(), Expense.recorded_at.desc()).all()
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()

    # Aging totals across every debitor, so overdue money is visible at a
    # glance instead of one account at a time.
    aging_totals = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    for r in rows:
        if r.get("aging"):
            for bucket, value in r["aging"]["buckets"].items():
                aging_totals[bucket] = round(aging_totals[bucket] + value, 2)

    return {
        "rows": rows,
        "kind": kind,
        "type_filter": type_filter,
        "expenses": expenses,
        "bank_accounts": bank_accounts,
        "aging_totals": aging_totals,
        "aging_total": round(sum(aging_totals.values()), 2),
    }


@app.route("/accounts")
@login_required
def accounts():
    kind = request.args.get("kind", "all")
    type_filter = request.args.get("type", "all")
    ctx = _accounts_context(kind, type_filter)
    return render_template("accounts.html", today=date.today(), **ctx)


@app.route("/accounts/export")
@login_required
def accounts_export():
    """Mirrors accounts() - same kind/type filters, same rows and aging
    totals - as either a PDF or an Excel workbook. Available to staff too,
    matching the page itself (owner-only content there is limited to the
    Expenses/Add-account panels, which this export doesn't include)."""
    fmt = _resolve_export_format()
    kind = request.args.get("kind", "all")
    type_filter = request.args.get("type", "all")
    ctx = _accounts_context(kind, type_filter)

    def oldest_label(row):
        aging = row.get("aging")
        if aging and aging.get("oldest_days") is not None:
            return f"{aging['oldest_days']} days"
        return "-"

    table_rows = []
    for r in ctx["rows"]:
        type_label = {"bank": "Bank Account", "cash": "Cash"}.get(r["kind"]) or r["obj"].account_type.capitalize()
        phone = (r["obj"].phone or "-") if r["kind"] == "account" else "-"
        table_rows.append([r["name"], type_label, phone, r["balance"], oldest_label(r)])

    blocks = [
        {
            "type": "table",
            "heading": "Accounts",
            "columns": ["Name", "Type", "Phone", "Balance (Rs)", "Oldest Unpaid"],
            "rows": table_rows,
            "align": ["left", "left", "left", "right", "left"],
        },
        {"type": "note", "text": "How old is the money owed to you - across every account with a positive balance."},
        {
            "type": "summary",
            "rows": [
                ("0-30 days (Rs)", ctx["aging_totals"]["0-30"]),
                ("31-60 days (Rs)", ctx["aging_totals"]["31-60"]),
                ("61-90 days (Rs)", ctx["aging_totals"]["61-90"]),
                ("Over 90 days (Rs)", ctx["aging_totals"]["90+"]),
                ("Total Owed To You (Rs)", ctx["aging_total"]),
            ],
        },
    ]

    filter_bits = []
    if kind != "all":
        filter_bits.append(kind)
    if type_filter != "all":
        filter_bits.append(type_filter)
    subtitle = f"As of {date.today().isoformat()}" + (f" - {' / '.join(filter_bits)}" if filter_bits else "")

    return _send_export(
        fmt,
        pdf_title="Accounts",
        pdf_subtitle=subtitle,
        xlsx_sheet_name="Accounts",
        blocks=blocks,
        filename_base=f"petrol-khata-accounts-{date.today().isoformat()}",
    )


@app.route("/accounts/add", methods=["POST"])
@login_required
@owner_required
def accounts_add():
    account_type = request.form.get("account_type", "")
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    opening_balance = request.form.get("opening_balance", type=float) or 0
    raw_date = request.form.get("opening_balance_date", "").strip()
    opening_balance_date = parse_date_param(raw_date) if raw_date else None

    if not name:
        flash("Please enter a name.", "error")
    elif account_type == "bank":
        db.session.add(
            BankAccount(
                name=name,
                opening_balance=opening_balance,
                opening_balance_date=opening_balance_date,
            )
        )
        db.session.commit()
        flash(f"Added bank account \"{name}\".", "success")
    elif account_type not in ACCOUNT_TYPES:
        flash("Please choose an account type.", "error")
    else:
        db.session.add(
            Account(
                name=name,
                phone=phone or None,
                account_type=account_type,
                opening_balance=opening_balance,
                opening_balance_date=opening_balance_date,
            )
        )
        db.session.commit()
        flash(f"Added {account_type} \"{name}\".", "success")

    return redirect(url_for("accounts"))


@app.route("/accounts/<int:account_id>")
@login_required
def account_detail(account_id):
    account = db.session.get(Account, account_id) or abort(404)
    events = account_ledger_events(account)
    fuel_types = FuelType.query.order_by(FuelType.name).all()
    # A resolver bulk-loads FuelPriceHistory once instead of a query per
    # (entry, fuel type) pair - price_on_date() would be entries x fuel
    # types SELECTs here, which is fine for a handful of rows but an N+1
    # storm on an account with a long credit history (see price_resolver()).
    resolve_price = price_resolver(fuel_types)
    for e in events:
        if e["kind"] == "credit":
            # The edit form's fuel dropdown must label each option with the
            # rate in force on THIS entry's own date, not today's cached
            # price - otherwise editing an old entry shows a price that was
            # never actually charged (see price_on_date()). Only credit
            # entries have an edit form with a fuel picker, so no other kind
            # needs this.
            e["fuel_prices"] = {f.id: resolve_price(f, e["entry_date"]) for f in fuel_types}
    tanks = Tank.query.order_by(Tank.number).all()
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
    today = date.today()
    return render_template(
        "account_detail.html",
        account=account,
        events=events,
        today=today,
        month_start=today.replace(day=1),
        fuel_types=fuel_types,
        tanks=tanks,
        bank_accounts=bank_accounts,
        aging=credit_aging(account, today) if account.balance > 0 else None,
    )


@app.route("/accounts/<int:account_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_edit(account_id):
    account = db.session.get(Account, account_id) or abort(404)
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    account_type = request.form.get("account_type", "")

    if not name:
        flash("Please enter a name.", "error")
    elif account_type not in ACCOUNT_TYPES:
        flash("Please choose an account type.", "error")
    else:
        account.name = name
        account.phone = phone or None
        account.account_type = account_type
        db.session.commit()
        flash("Account details updated.", "success")

    return redirect(url_for("account_detail", account_id=account.id))


@app.route("/accounts/<int:account_id>/opening-balance", methods=["POST"])
@login_required
@owner_required
def account_opening_balance(account_id):
    account = db.session.get(Account, account_id) or abort(404)
    opening_balance = request.form.get("opening_balance", type=float)
    raw_date = request.form.get("opening_balance_date", "").strip()

    if opening_balance is None:
        flash("Please enter an opening balance (use 0 to clear it).", "error")
    elif opening_balance and not raw_date:
        flash("Please choose an as-of date for the opening balance.", "error")
    else:
        account.opening_balance = opening_balance
        account.opening_balance_date = parse_date_param(raw_date) if raw_date else None
        db.session.commit()
        flash("Opening balance updated.", "success")

    return redirect(url_for("account_detail", account_id=account.id))


@app.route("/accounts/<int:account_id>/delete", methods=["POST"])
@login_required
@owner_required
def account_delete(account_id):
    """Only allowed for an account with no transaction history and no
    opening balance - a genuine "made this by mistake" case. An account
    that's actually been used has to stay for the numbers to add up; you
    can still just stop using it."""
    account = db.session.get(Account, account_id) or abort(404)
    has_entries = (
        account.credit_entries
        or account.receipts
        or account.stock_purchases
        or account.supplier_payments
        or account.employee_loans
    )

    if has_entries or account.opening_balance:
        flash(f"Can't delete {account.name} - it already has transaction history or a nonzero opening balance.", "error")
    else:
        name = account.name
        db.session.delete(account)
        db.session.commit()
        flash(f'Deleted "{name}".', "success")

    return redirect(url_for("accounts"))


@app.route("/accounts/entry/credit/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_credit_edit(entry_id):
    """Same entry_mode toggle as ledger_credit() (see its docstring) - lets
    an existing credit-given entry be switched between liters-mode and
    amount-mode, or stay in either, on save."""
    entry = db.session.get(CreditGiven, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    entry_mode = request.form.get("entry_mode", "liters")
    if entry_mode not in ("liters", "amount"):
        entry_mode = "liters"
    liters_in = request.form.get("liters", type=float)
    amount_in = request.form.get("amount", type=float)
    vehicle_number = request.form.get("vehicle_number", "").strip()
    note = request.form.get("note", "").strip()
    fuel = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None

    if not fuel:
        flash("Please choose a valid fuel type.", "error")
    elif entry_mode == "liters" and (not liters_in or liters_in <= 0):
        flash("Liters must be a positive number.", "error")
    elif entry_mode == "amount" and (not amount_in or amount_in <= 0):
        flash("Amount must be a positive number.", "error")
    elif entry_mode == "amount" and price_on_date(fuel, entry_date) <= 0:
        flash("This fuel has no price set yet - please set a price before recording credit by amount.", "error")
    else:
        price = price_on_date(fuel, entry_date)
        if entry_mode == "amount":
            amount = amount_in
            liters = round(amount_in / price, 2)
        else:
            liters = liters_in
            amount = round(liters * price, 2)
        entry.entry_date = entry_date
        entry.fuel_type_id = fuel.id
        entry.liters = liters
        entry.price_per_liter = price
        entry.amount = amount
        entry.vehicle_number = vehicle_number or None
        entry.note = note or None
        db.session.commit()
        flash("Credit entry updated.", "success")

    return redirect(url_for("account_detail", account_id=entry.account_id))


@app.route("/accounts/entry/receipt/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_receipt_edit(entry_id):
    entry = db.session.get(Receipt, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)

    if not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    elif method_error:
        flash(method_error, "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Receipt updated.", "success")

    return redirect(url_for("account_detail", account_id=entry.account_id))


@app.route("/accounts/entry/purchase/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_purchase_edit(entry_id):
    entry = db.session.get(StockPurchase, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    tank_id = request.form.get("tank_id", type=int)
    liters = request.form.get("liters", type=float)
    cost = request.form.get("cost", type=float)
    note = request.form.get("note", "").strip()
    tank = db.session.get(Tank, tank_id) if tank_id else None

    if not tank:
        flash("Please choose a valid tank.", "error")
    elif not liters or liters <= 0:
        flash("Liters must be a positive number.", "error")
    elif not cost or cost <= 0:
        flash("Cost must be a positive number.", "error")
    else:
        entry.entry_date = entry_date
        entry.tank_id = tank.id
        entry.liters = liters
        entry.cost = cost
        entry.note = note or None
        db.session.commit()
        flash("Purchase updated.", "success")

    return redirect(url_for("account_detail", account_id=entry.account_id))


@app.route("/accounts/entry/supplier-payment/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_supplier_payment_edit(entry_id):
    entry = db.session.get(SupplierPayment, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)
    old_cash_amount = entry.amount if entry.method == "cash" else 0
    new_cash_amount = amount if (amount and method == "cash") else 0

    if not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    elif method_error:
        flash(method_error, "error")
    elif would_overdraw_cash(new_cash_amount, entry_date, old_cash_amount, entry.entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Payment updated.", "success")

    return redirect(url_for("account_detail", account_id=entry.account_id))


@app.route("/accounts/entry/employee-loan/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_employee_loan_edit(entry_id):
    entry = db.session.get(EmployeeLoan, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)
    old_cash_amount = entry.amount if entry.method == "cash" else 0
    new_cash_amount = amount if (amount and method == "cash") else 0

    if not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    elif method_error:
        flash(method_error, "error")
    elif would_overdraw_cash(new_cash_amount, entry_date, old_cash_amount, entry.entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Loan updated.", "success")

    return redirect(url_for("account_detail", account_id=entry.account_id))


@app.route("/accounts/entry/salary/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_salary_edit(entry_id):
    entry = db.session.get(SalaryPayment, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    gross = request.form.get("gross_amount", type=float)
    deduction = request.form.get("deduction_amount", type=float) or 0
    period_label = request.form.get("period_label", "").strip()
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)

    old_cash_net = entry.net_paid if entry.method == "cash" else 0
    new_net = round((gross or 0) - deduction, 2)
    new_cash_net = new_net if method == "cash" else 0
    # What this account would owe with THIS entry's deduction backed out,
    # so the new deduction is checked against the right ceiling.
    outstanding_without_entry = round(entry.account.balance + entry.deduction_amount, 2)

    if not gross or gross <= 0:
        flash("Salary amount must be a positive number.", "error")
    elif deduction < 0:
        flash("Deduction can't be negative.", "error")
    elif deduction > gross:
        flash("Deduction can't be more than the salary itself.", "error")
    elif deduction > outstanding_without_entry + 0.01:
        flash(
            f"{entry.account.name} only owes Rs {max(outstanding_without_entry, 0):,.2f}, so you "
            f"can't deduct Rs {deduction:,.2f}.",
            "error",
        )
    elif method_error:
        flash(method_error, "error")
    elif would_overdraw_cash(new_cash_net, entry_date, old_cash_net, entry.entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        entry.entry_date = entry_date
        entry.gross_amount = gross
        entry.deduction_amount = deduction
        entry.period_label = period_label or None
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Salary payment updated.", "success")

    return redirect(next_url)


def _account_statement_context(account, from_date, to_date):
    """The balance carried into a date range, every entry inside it, and
    the closing balance - all derived from the same account_ledger_events()
    the account detail page uses. Shared by the Statement page and its
    PDF/Excel export."""
    all_events = list(reversed(account_ledger_events(account)))  # oldest first
    before = [e for e in all_events if e["entry_date"] < from_date]
    inside = [e for e in all_events if from_date <= e["entry_date"] <= to_date]
    # sum() starts from 0.0, not the default 0 - a brand-new account (or a
    # range with nothing before it) would otherwise leave `opening` as a
    # bare int, which the PDF/Excel export would then render as "0"
    # instead of "0.00" alongside every other money figure.
    opening = round(sum((e["delta"] for e in before), 0.0), 2)

    return {
        "opening": opening,
        "events": inside,
        "closing": inside[-1]["running_balance"] if inside else opening,
        "aging": credit_aging(account, to_date),
    }


@app.route("/accounts/<int:account_id>/statement")
@login_required
def account_statement(account_id):
    """A print-friendly statement for a date range - the thing you actually
    hand or send to a credit customer."""
    account = db.session.get(Account, account_id) or abort(404)
    today = date.today()
    from_date = parse_date_param(request.args.get("from"), fallback=today.replace(day=1))
    to_date = parse_date_param(request.args.get("to"), fallback=today)
    ctx = _account_statement_context(account, from_date, to_date)

    return render_template(
        "account_statement.html",
        account=account,
        from_date=from_date,
        to_date=to_date,
        today=today,
        **ctx,
    )


_STATEMENT_KIND_LABELS = {
    "opening": "Opening Balance",
    "credit": "Fuel on Credit",
    "receipt": "Payment Received",
    "purchase": "Purchase (Credit)",
    "supplier_payment": "Payment Made",
    "employee_loan": "Loan / Advance",
    "salary": "Salary",
    "sales_return": "Sales Return",
    "product_sale": "Non-Fuel Sale",
    "product_purchase": "Product Purchase (Credit)",
}


def _statement_kind_label(e):
    """Same "Type" column text account_statement.html shows for one event
    (see its kind if/elif chain), reused so the export doesn't drift from
    the page - mirrors _statement_event_details() below for the Details
    column. employee_loan is the one kind whose label isn't a static
    per-kind string: it reads "Owner Drawing" or "Loan / Advance"
    depending on the row's own EmployeeLoan.kind (see models.py)."""
    if e["kind"] == "employee_loan":
        return "Owner Drawing" if e["obj"].kind == "drawing" else "Loan / Advance"
    return _STATEMENT_KIND_LABELS.get(e["kind"], e["kind"])


def _statement_event_details(e):
    """Same "Details" column text the account_statement.html table shows
    for one event, reused so the export doesn't drift from the page."""
    obj = e["obj"]
    if e["kind"] == "credit":
        bits = [f"{obj.liters:.2f} L {obj.fuel_type.name}"]
        if obj.vehicle_number:
            bits.append(obj.vehicle_number)
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] == "purchase":
        bits = [f"{obj.liters:.2f} L {obj.tank.label}"]
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] == "sales_return":
        bits = [f"{obj.liters:.2f} L {obj.fuel_type.name} returned to {obj.tank.label}"]
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] in ("product_sale", "product_purchase"):
        bits = [f"{obj.quantity:.2f} {obj.product.unit} {obj.product.label}"]
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] == "salary":
        text = obj.period_label or "Salary"
        if obj.deduction_amount:
            text += f" - Rs {obj.deduction_amount:,.2f} deducted against advance"
        return text
    if obj is not None:
        text = f"Via {obj.bank_account.name}" if obj.method == "bank" else "Cash"
        if obj.note:
            text += f" - {obj.note}"
        return text
    return "-"


@app.route("/accounts/<int:account_id>/statement/export")
@login_required
def account_statement_export(account_id):
    """Mirrors account_statement() - same account, same date range - as a
    PDF or Excel download instead of a page."""
    fmt = _resolve_export_format()
    account = db.session.get(Account, account_id) or abort(404)
    today = date.today()
    from_date = parse_date_param(request.args.get("from"), fallback=today.replace(day=1))
    to_date = parse_date_param(request.args.get("to"), fallback=today)
    ctx = _account_statement_context(account, from_date, to_date)

    statement_rows = [
        [from_date.isoformat(), "Brought Forward", "Balance carried into this period", "", "", ctx["opening"]]
    ]
    for e in ctx["events"]:
        statement_rows.append(
            [
                e["entry_date"].isoformat(),
                _statement_kind_label(e),
                _statement_event_details(e),
                e["delta"] if e["delta"] > 0 else "",
                -e["delta"] if e["delta"] < 0 else "",
                e["running_balance"],
            ]
        )
    statement_rows.append(
        ["", "", f"Closing balance as of {to_date.isoformat()}", "", "", ctx["closing"]]
    )

    blocks = [
        {
            "type": "summary",
            "rows": [
                ("Account", account.name),
                ("Type", account.account_type.capitalize()),
                ("Phone", account.phone or "-"),
                ("Period", f"{from_date.isoformat()} to {to_date.isoformat()}"),
            ],
        },
        {
            "type": "table",
            "heading": "Statement",
            "columns": ["Date", "Type", "Details", "Debit (Rs)", "Credit (Rs)", "Balance (Rs)"],
            "rows": statement_rows,
            "align": ["left", "left", "left", "right", "right", "right"],
        },
    ]
    if ctx["aging"] and ctx["aging"]["outstanding"]:
        blocks.append(
            {
                "type": "table",
                "heading": "Age of Outstanding Amount",
                "columns": ["0-30 days", "31-60 days", "61-90 days", "Over 90 days"],
                "rows": [
                    [
                        ctx["aging"]["buckets"]["0-30"],
                        ctx["aging"]["buckets"]["31-60"],
                        ctx["aging"]["buckets"]["61-90"],
                        ctx["aging"]["buckets"]["90+"],
                    ]
                ],
                "align": ["right", "right", "right", "right"],
            }
        )

    return _send_export(
        fmt,
        pdf_title=f"Statement - {account.name}",
        pdf_subtitle=f"{from_date.strftime('%d %b %Y')} to {to_date.strftime('%d %b %Y')}",
        xlsx_sheet_name="Statement",
        blocks=blocks,
        filename_base=f"petrol-khata-statement-{slugify(account.name)}-{date.today().isoformat()}",
    )


@app.route("/accounts/bank/<int:bank_account_id>")
@login_required
def bank_account_detail(bank_account_id):
    bank_account = db.session.get(BankAccount, bank_account_id) or abort(404)
    events = bank_account_ledger_events(bank_account)
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
    tanks = Tank.query.order_by(Tank.number).all()
    return render_template(
        "bank_account_detail.html",
        bank_account=bank_account,
        events=events,
        today=date.today(),
        bank_accounts=bank_accounts,
        tanks=tanks,
    )


@app.route("/accounts/bank/<int:bank_account_id>/edit", methods=["POST"])
@login_required
@owner_required
def bank_account_edit(bank_account_id):
    bank_account = db.session.get(BankAccount, bank_account_id) or abort(404)
    name = request.form.get("name", "").strip()

    if not name:
        flash("Please enter a name.", "error")
    else:
        bank_account.name = name
        db.session.commit()
        flash("Bank account details updated.", "success")

    return redirect(url_for("bank_account_detail", bank_account_id=bank_account.id))


@app.route("/accounts/bank/<int:bank_account_id>/opening-balance", methods=["POST"])
@login_required
@owner_required
def bank_account_opening_balance(bank_account_id):
    bank_account = db.session.get(BankAccount, bank_account_id) or abort(404)
    opening_balance = request.form.get("opening_balance", type=float)
    raw_date = request.form.get("opening_balance_date", "").strip()

    if opening_balance is None:
        flash("Please enter an opening balance (use 0 to clear it).", "error")
    elif opening_balance and not raw_date:
        flash("Please choose an as-of date for the opening balance.", "error")
    else:
        bank_account.opening_balance = opening_balance
        bank_account.opening_balance_date = parse_date_param(raw_date) if raw_date else None
        db.session.commit()
        flash("Opening balance updated.", "success")

    return redirect(url_for("bank_account_detail", bank_account_id=bank_account.id))


@app.route("/accounts/bank/<int:bank_account_id>/delete", methods=["POST"])
@login_required
@owner_required
def bank_account_delete(bank_account_id):
    """Only allowed for a bank account with no transaction history and no
    opening balance - see account_delete for the same reasoning."""
    bank_account = db.session.get(BankAccount, bank_account_id) or abort(404)
    has_entries = (
        bank_account.bank_sales
        or bank_account.deposits
        or bank_account.receipts
        or bank_account.employee_loans_paid
        or bank_account.expenses
        or bank_account.fuel_purchases
        or bank_account.supplier_payments_paid
    )

    if has_entries or bank_account.opening_balance:
        flash(
            f"Can't delete {bank_account.name} - it already has transaction history or a nonzero opening balance.",
            "error",
        )
    else:
        name = bank_account.name
        db.session.delete(bank_account)
        db.session.commit()
        flash(f'Deleted "{name}".', "success")

    return redirect(url_for("accounts"))


@app.route("/accounts/entry/bank-sale/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_bank_sale_edit(entry_id):
    entry = db.session.get(BankSale, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()

    if not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    elif would_overdraw_cash(amount, entry_date, entry.amount, entry.entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.note = note or None
        db.session.commit()
        flash("Bank sale updated.", "success")

    return redirect(url_for("bank_account_detail", bank_account_id=entry.bank_account_id))


@app.route("/accounts/entry/cash-deposit/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_cash_deposit_edit(entry_id):
    entry = db.session.get(CashDeposit, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()

    if not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    elif would_overdraw_cash(amount, entry_date, entry.amount, entry.entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.note = note or None
        db.session.commit()
        flash("Cash deposit updated.", "success")

    return redirect(url_for("bank_account_detail", bank_account_id=entry.bank_account_id))


@app.route("/accounts/entry/expense/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_expense_edit(entry_id):
    """Shared expense editor, reachable from the Accounts page's all-time
    Expenses list, cash-in-hand's page, or a bank account's page -
    wherever the entry happens to be shown - redirecting back to
    whichever of those pages linked here via the hidden "next" field."""
    entry = db.session.get(Expense, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()
    amount = request.form.get("amount", type=float)
    method, bank_account, method_error = resolve_payment_method(request.form)
    next_url = request.form.get("next") or url_for("accounts")
    old_cash_amount = entry.amount if entry.method == "cash" else 0
    new_cash_amount = amount if (amount and method == "cash") else 0

    if not category:
        flash("Please enter an expense category.", "error")
    elif not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    elif method_error:
        flash(method_error, "error")
    elif would_overdraw_cash(new_cash_amount, entry_date, old_cash_amount, entry.entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        entry.entry_date = entry_date
        entry.category = category
        entry.description = description or None
        entry.amount = amount
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        db.session.commit()
        flash("Expense updated.", "success")

    return redirect(next_url)


@app.route("/accounts/entry/fuel-purchase/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_fuel_purchase_edit(entry_id):
    """Editor for a cash-paid fuel purchase (payment_type == "cash"),
    reachable from cash-in-hand's page or the bank account it was paid
    from - redirects back via the hidden "next" field. Credit purchases
    are edited from the supplier account's page instead
    (account_entry_purchase_edit) since payment_type can't be changed
    here."""
    entry = db.session.get(StockPurchase, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    tank_id = request.form.get("tank_id", type=int)
    liters = request.form.get("liters", type=float)
    cost = request.form.get("cost", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, method_error = resolve_payment_method(request.form)
    next_url = request.form.get("next") or url_for("accounts")
    tank = db.session.get(Tank, tank_id) if tank_id else None
    old_cash_amount = (entry.cost or 0) if entry.method == "cash" else 0
    new_cash_amount = cost if method == "cash" else 0

    if not tank:
        flash("Please choose a valid tank.", "error")
    elif not liters or liters <= 0:
        flash("Liters must be a positive number.", "error")
    elif not cost or cost <= 0:
        flash("Cost must be a positive number.", "error")
    elif method_error:
        flash(method_error, "error")
    elif would_overdraw_cash(new_cash_amount, entry_date, old_cash_amount, entry.entry_date):
        flash(cash_shortfall_message(entry_date), "error")
    else:
        entry.entry_date = entry_date
        entry.tank_id = tank.id
        entry.liters = liters
        entry.cost = cost
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Fuel purchase updated.", "success")

    return redirect(next_url)


@app.route("/accounts/cash")
@login_required
def cash_account_detail():
    cash_account = get_cash_account()
    events = cash_account_ledger_events(cash_account)
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
    tanks = Tank.query.order_by(Tank.number).all()
    return render_template(
        "cash_account_detail.html",
        cash_account=cash_account,
        balance=cash_account_balance(cash_account),
        events=events,
        today=date.today(),
        bank_accounts=bank_accounts,
        tanks=tanks,
    )


@app.route("/accounts/cash/opening-balance", methods=["POST"])
@login_required
@owner_required
def cash_account_opening_balance():
    cash_account = get_cash_account()
    opening_balance = request.form.get("opening_balance", type=float)
    raw_date = request.form.get("opening_balance_date", "").strip()

    if opening_balance is None or opening_balance < 0:
        flash("Please enter a valid opening balance (0 or more - cash in hand can't be negative).", "error")
    elif opening_balance and not raw_date:
        flash("Please choose an as-of date for the opening balance.", "error")
    else:
        cash_account.opening_balance = opening_balance
        cash_account.opening_balance_date = parse_date_param(raw_date) if raw_date else None
        db.session.commit()
        flash("Opening balance updated.", "success")

    return redirect(url_for("cash_account_detail"))


# -------------------------------------------------------------- reports ---

def _reports_context(selected_date):
    """Every figure the Daily Report shows, for one date - shared by the
    HTML page and its PDF/Excel export so the two can never drift apart."""
    sales = (
        Sale.query.filter_by(entry_date=selected_date)
        .join(Nozzle)
        .order_by(Nozzle.dispenser_id, Nozzle.nozzle_number)
        .all()
    )
    # Every sum() below starts from 0.0 rather than the default 0 - on a
    # date with none of that kind, plain sum(empty) is an int, which the
    # HTML template's "%.2f" format papers over but the PDF export's
    # formatter (which has to tell a count apart from a money figure by
    # its Python type) would otherwise render as a bare "0" instead of
    # "0.00", inconsistent with every other row.
    total_sales = sum((s.total_amount for s in sales), 0.0)
    total_liters = sum((s.liters for s in sales), 0.0)
    total_testing_liters = sum((s.testing_liters for s in sales), 0.0)
    by_fuel = fuel_sales_for_date(selected_date)
    # Testing isn't part of by_fuel (fuel_sales_for_date() means net sold,
    # same as everywhere else) - broken out per fuel here purely for this
    # table, since it's the only place that wants it split that way.
    testing_by_fuel = {}
    for s in sales:
        if s.testing_liters:
            name = s.nozzle.tank.fuel_type.name
            testing_by_fuel[name] = testing_by_fuel.get(name, 0.0) + s.testing_liters

    credit_given = CreditGiven.query.filter_by(entry_date=selected_date).all()
    total_credit_given = sum((c.amount for c in credit_given), 0.0)

    bank_sales = BankSale.query.filter_by(entry_date=selected_date).all()
    total_bank_sales = sum((b.amount for b in bank_sales), 0.0)
    cash_sales = total_sales - total_credit_given - total_bank_sales

    payments = Receipt.query.filter_by(entry_date=selected_date).all()
    total_payments = sum((p.amount for p in payments), 0.0)

    expenses = Expense.query.filter_by(entry_date=selected_date).order_by(Expense.recorded_at).all()
    total_expenses = sum((e.amount for e in expenses), 0.0)

    purchases = (
        StockPurchase.query.filter_by(entry_date=selected_date).order_by(StockPurchase.recorded_at).all()
    )
    total_purchased_liters = sum((p.liters for p in purchases), 0.0)
    cash_purchases_total = sum((p.cost or 0 for p in purchases if p.payment_type == "cash"), 0.0)

    supplier_payments = SupplierPayment.query.filter_by(entry_date=selected_date).all()
    total_supplier_payments = sum((p.amount for p in supplier_payments), 0.0)

    salaries = SalaryPayment.query.filter_by(entry_date=selected_date).all()
    total_salaries_net = sum((s.net_paid for s in salaries), 0.0)

    sales_returns = SalesReturn.query.filter_by(entry_date=selected_date).all()
    total_sales_returns_liters = sum((sr.liters for sr in sales_returns), 0.0)
    total_sales_returns_amount = sum((sr.amount for sr in sales_returns), 0.0)
    # Only a cash-method return actually draws down the register the same
    # day - a bank-method one hits that bank instead, and a credit-method
    # one just reduces what the customer owes, matching how only cash-paid
    # purchases (not credit ones) subtract from net_cash_flow below.
    cash_sales_returns_total = sum((sr.amount for sr in sales_returns if sr.method == "cash"), 0.0)

    # Non-fuel (lubricant/filter/shop) sales and purchases - counted in
    # UNITS, not liters, since a product isn't measured through a nozzle
    # (see Product.unit's docstring in models.py). Only a cash-method sale
    # or a cash-paid-cash-method purchase actually moves cash-in-hand the
    # same day, exactly the same distinction cash_sales_returns_total and
    # cash_purchases_total above already make for fuel.
    product_sales = ProductSale.query.filter_by(entry_date=selected_date).order_by(ProductSale.recorded_at).all()
    total_product_sales_units = sum((ps.quantity for ps in product_sales), 0.0)
    total_product_sales_amount = sum((ps.amount for ps in product_sales), 0.0)
    cash_product_sales_total = sum((ps.amount for ps in product_sales if ps.method == "cash"), 0.0)

    product_purchases = (
        ProductPurchase.query.filter_by(entry_date=selected_date).order_by(ProductPurchase.recorded_at).all()
    )
    total_product_purchases_units = sum((pp.quantity for pp in product_purchases), 0.0)
    total_product_purchases_cost = sum((pp.total_cost for pp in product_purchases), 0.0)
    cash_product_purchases_total = sum(
        (pp.total_cost for pp in product_purchases if pp.payment_type == "cash" and pp.method == "cash"), 0.0
    )

    tanks = Tank.query.order_by(Tank.number).all()
    tank_rows = []
    for t in tanks:
        stock = book_stock(t, selected_date)
        dip = TankDip.query.filter_by(tank_id=t.id, entry_date=selected_date).first()
        tank_rows.append(
            {
                "tank": t,
                "book_stock": stock,
                "dip": dip.dip_liters if dip else None,
                "variance": round(dip.dip_liters - stock, 2) if dip else None,
                "water_cm": dip.water_cm if dip else None,
            }
        )

    net_cash_flow = (
        total_sales
        - total_credit_given
        + total_payments
        - total_expenses
        - cash_purchases_total
        - total_supplier_payments
        - total_salaries_net
        - cash_sales_returns_total
        + cash_product_sales_total
        - cash_product_purchases_total
    )
    outstanding_credit = sum((b for a in Account.query.all() if (b := a.balance) > 0), 0.0)
    cash_balance = cash_account_balance(get_cash_account())
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()

    return {
        "total_sales": total_sales,
        "total_liters": total_liters,
        "total_testing_liters": total_testing_liters,
        "by_fuel": by_fuel,
        "testing_by_fuel": testing_by_fuel,
        "cash_sales": cash_sales,
        "total_credit_given": total_credit_given,
        "bank_sales": bank_sales,
        "total_bank_sales": total_bank_sales,
        "total_payments": total_payments,
        "expenses": expenses,
        "total_expenses": total_expenses,
        "purchases": purchases,
        "total_purchased_liters": total_purchased_liters,
        "total_supplier_payments": total_supplier_payments,
        "tank_rows": tank_rows,
        "net_cash_flow": net_cash_flow,
        "outstanding_credit": outstanding_credit,
        "cash_balance": cash_balance,
        "bank_accounts": bank_accounts,
        "salaries": salaries,
        "total_salaries_net": total_salaries_net,
        "sales_returns": sales_returns,
        "total_sales_returns_liters": total_sales_returns_liters,
        "total_sales_returns_amount": total_sales_returns_amount,
        "handover_rows": handover_rows_for_date(selected_date),
        "product_sales": product_sales,
        "total_product_sales_units": total_product_sales_units,
        "total_product_sales_amount": total_product_sales_amount,
        "product_purchases": product_purchases,
        "total_product_purchases_units": total_product_purchases_units,
        "total_product_purchases_cost": total_product_purchases_cost,
    }


@app.route("/reports")
@login_required
@owner_required
def reports():
    selected_date = parse_date_param(request.args.get("date"))
    ctx = _reports_context(selected_date)
    return render_template("reports.html", selected_date=selected_date, today=date.today(), **ctx)


@app.route("/reports/export")
@login_required
@owner_required
def reports_export():
    """Mirrors reports() - same figures, same date - as a PDF or Excel
    download instead of a page."""
    fmt = _resolve_export_format()
    selected_date = parse_date_param(request.args.get("date"))
    ctx = _reports_context(selected_date)

    summary_rows = [
        ("Total Sales (Rs)", ctx["total_sales"]),
        ("Liters Sold", ctx["total_liters"]),
        ("Testing (L)", ctx["total_testing_liters"]),
        ("Cash Sales (Rs)", ctx["cash_sales"]),
        ("Bank Sales (Rs)", ctx["total_bank_sales"]),
        ("Credit Given (Rs)", ctx["total_credit_given"]),
        ("Receipts from Customers (Rs)", ctx["total_payments"]),
        ("Sales Returns (L)", ctx["total_sales_returns_liters"]),
        ("Sales Returns (Rs)", ctx["total_sales_returns_amount"]),
        ("Non-Fuel Sales (units)", ctx["total_product_sales_units"]),
        ("Non-Fuel Sales (Rs)", ctx["total_product_sales_amount"]),
        ("Product Purchases (units)", ctx["total_product_purchases_units"]),
        ("Product Purchases (Rs)", ctx["total_product_purchases_cost"]),
        ("Expenses (Rs)", ctx["total_expenses"]),
        ("Payments to Suppliers (Rs)", ctx["total_supplier_payments"]),
        ("Salaries Paid Out, Net (Rs)", ctx["total_salaries_net"]),
        ("Net Cash Flow (Rs)", ctx["net_cash_flow"]),
        ("Outstanding Receivables (Rs)", ctx["outstanding_credit"]),
        ("Cash in Hand (Rs)", ctx["cash_balance"]),
    ]
    for b in ctx["bank_accounts"]:
        summary_rows.append((f"{b.name} (Rs)", b.balance))

    blocks = [
        {"type": "summary", "rows": summary_rows},
        {
            "type": "table",
            "heading": "Sales by Fuel Type",
            "columns": ["Fuel", "Liters Sold", "Testing (L)", "Revenue (Rs)"],
            "rows": [
                [name, d["liters"], ctx["testing_by_fuel"].get(name, 0.0), d["revenue"]]
                for name, d in ctx["by_fuel"].items()
            ],
            "align": ["left", "right", "right", "right"],
        },
        {
            "type": "table",
            "heading": "Stock / Dip per Tank",
            "columns": ["Tank", "Fuel", "Book Stock (L)", "Dip Reading (L)", "Variance (L)", "Water (cm)"],
            "rows": [
                [
                    r["tank"].label,
                    r["tank"].fuel_type.name,
                    r["book_stock"],
                    r["dip"] if r["dip"] is not None else "-",
                    r["variance"] if r["variance"] is not None else "-",
                    r["water_cm"] if r["water_cm"] is not None else "-",
                ]
                for r in ctx["tank_rows"]
            ],
            "align": ["left", "left", "right", "right", "right", "right"],
        },
        {
            "type": "table",
            "heading": "Sales Returns",
            "columns": ["Fuel", "Tank", "Liters", "Refund (Rs)", "Method", "Note"],
            "rows": [
                [
                    sr.fuel_type.name,
                    sr.tank.label,
                    sr.liters,
                    sr.amount,
                    {"cash": "Cash", "bank": f"Via {sr.bank_account.name}" if sr.bank_account else "Bank", "credit": f"On account ({sr.account.name})" if sr.account else "On account"}.get(sr.method, sr.method),
                    sr.note or "-",
                ]
                for sr in ctx["sales_returns"]
            ],
            "align": ["left", "left", "right", "right", "left", "left"],
        },
        {
            "type": "table",
            "heading": "Non-Fuel Sales",
            "columns": ["Product", "Units", "Rate (Rs)", "Amount (Rs)", "Method"],
            "rows": [
                [
                    ps.product.label,
                    ps.quantity,
                    ps.retail_rate,
                    ps.amount,
                    {"cash": "Cash", "bank": f"Via {ps.bank_account.name}" if ps.bank_account else "Bank", "credit": f"On account ({ps.account.name})" if ps.account else "On account"}.get(ps.method, ps.method),
                ]
                for ps in ctx["product_sales"]
            ],
            "align": ["left", "right", "right", "right", "left"],
        },
        {
            "type": "table",
            "heading": "Product Purchases",
            "columns": ["Product", "Units", "Unit Cost (Rs)", "Total (Rs)", "Payment"],
            "rows": [
                [pp.product.label, pp.quantity, pp.unit_cost, pp.total_cost, "Credit" if pp.payment_type == "credit" else "Cash"]
                for pp in ctx["product_purchases"]
            ],
            "align": ["left", "right", "right", "right", "left"],
        },
        {
            "type": "table",
            "heading": "Cash Handover - Shift Reconciliation",
            "columns": ["Shift", "Expected Cash (Rs)", "Counted (Rs)", "Variance (Rs)", "Attendant"],
            "rows": [
                [
                    r["shift"].name,
                    r["expected"],
                    r["declared"] if r["declared"] is not None else "-",
                    r["variance"] if r["variance"] is not None else "Not reconciled",
                    r["handover"].attendant.name if r["handover"] and r["handover"].attendant else "-",
                ]
                for r in ctx["handover_rows"]
            ],
            "align": ["left", "right", "right", "right", "left"],
        },
        {
            "type": "table",
            "heading": "Inventory Received",
            "columns": ["Tank", "Liters", "Payment"],
            "rows": [
                [p.tank.label, p.liters, "Credit" if p.payment_type == "credit" else "Cash"]
                for p in ctx["purchases"]
            ],
            "align": ["left", "right", "left"],
        },
        {
            "type": "table",
            "heading": "Expenses",
            "columns": ["Category", "Description", "Paid via", "Amount (Rs)"],
            "rows": [
                [
                    e.category,
                    e.description or "-",
                    f"Via {e.bank_account.name}" if e.method == "bank" else "Cash",
                    e.amount,
                ]
                for e in ctx["expenses"]
            ],
            "align": ["left", "left", "left", "right"],
        },
        {
            "type": "table",
            "heading": "Bank Sales",
            "columns": ["Bank Account", "Amount (Rs)"],
            "rows": [[b.bank_account.name, b.amount] for b in ctx["bank_sales"]],
            "align": ["left", "right"],
        },
        {
            "type": "table",
            "heading": "Salaries",
            "columns": ["Employee", "Period", "Salary (Rs)", "Deducted (Rs)", "Net Paid (Rs)", "Paid via"],
            "rows": [
                [
                    s.account.name,
                    s.period_label or "-",
                    s.gross_amount,
                    s.deduction_amount,
                    s.net_paid,
                    f"Via {s.bank_account.name}" if s.method == "bank" else "Cash",
                ]
                for s in ctx["salaries"]
            ],
            "align": ["left", "left", "right", "right", "right", "left"],
        },
    ]

    return _send_export(
        fmt,
        pdf_title="Daily Report",
        pdf_subtitle=selected_date.strftime("%A, %d %B %Y"),
        xlsx_sheet_name="Daily Report",
        blocks=blocks,
        filename_base=f"petrol-khata-daily-{selected_date.isoformat()}",
    )


def _month_range_from_param(raw_month):
    """Parse a "YYYY-MM" query param into (start, end) dates spanning that
    calendar month, falling back to the current month for anything
    missing or malformed - shared by the Monthly Report page and its
    export so an odd/blank ?month= behaves identically for both."""
    today = date.today()
    try:
        year, month = (int(p) for p in raw_month.split("-"))
        start = date(year, month, 1)
    except (ValueError, AttributeError):
        start = today.replace(day=1)
    end = (start + timedelta(days=31)).replace(day=1) - timedelta(days=1)
    return start, end


def _reports_monthly_context(start, end):
    """Every figure the Monthly Report shows, for one date range - shared
    by the HTML page and its PDF/Excel export.

    Presented as a traditional income statement: Fuel Revenue (gross) -
    Sales Returns = Net Fuel Revenue - Cost of Fuel Sold = Fuel Gross
    Margin; then Product Revenue - Cost of Products Sold = Product Gross
    Margin; Fuel Gross Margin + Product Gross Margin = Total Gross Margin
    - Expenses - Salaries = Net Profit. cogs_for_period() nets sales
    returns into COGS/margin already (see its docstring) - net_revenue
    below is the ONLY other place a return is subtracted, so net_profit
    must never subtract sales_returns_amount again on top of gross_margin,
    or the same refund gets double-counted (that was a real bug here - see
    Phase 1's follow-up fix). "gross_margin" stays FUEL-ONLY (unchanged by
    Phase 2B) precisely so that invariant, and the Trends reconciliation
    that depends on it, can't quietly break just because products now
    exist - product profit is added in separately via product_commission
    on the way to total_gross_margin/net_profit, never folded into
    gross_margin itself."""
    # Each of these is wrapped in float() because coalesce(sum(x), 0) comes
    # back as a Python int when nothing matches (SQLite has no rows to sum,
    # so it falls back to the literal 0) - the HTML template's "%.2f"
    # format hides that, but the PDF export's formatter tells a money
    # figure from a count by its Python type, so an unwrapped int here
    # would render as a bare "0" instead of "0.00".
    revenue = float(
        db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .scalar()
    )
    liters_sold = float(
        db.session.query(func.coalesce(func.sum(Sale.liters), 0))
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .scalar()
    )
    testing_liters = float(
        db.session.query(func.coalesce(func.sum(Sale.testing_liters), 0))
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .scalar()
    )
    cogs, cogs_detail = cogs_for_period(start, end)
    expenses_total = float(
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.entry_date >= start, Expense.entry_date <= end)
        .scalar()
    )
    salaries_total = float(
        db.session.query(func.coalesce(func.sum(SalaryPayment.gross_amount), 0))
        .filter(SalaryPayment.entry_date >= start, SalaryPayment.entry_date <= end)
        .scalar()
    )
    expenses_by_category = (
        db.session.query(Expense.category, func.sum(Expense.amount))
        .filter(Expense.entry_date >= start, Expense.entry_date <= end)
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
        .all()
    )

    credit_given = float(
        db.session.query(func.coalesce(func.sum(CreditGiven.amount), 0))
        .filter(CreditGiven.entry_date >= start, CreditGiven.entry_date <= end)
        .scalar()
    )
    receipts_total = float(
        db.session.query(func.coalesce(func.sum(Receipt.amount), 0))
        .filter(Receipt.entry_date >= start, Receipt.entry_date <= end)
        .scalar()
    )
    purchases_liters = float(
        db.session.query(func.coalesce(func.sum(StockPurchase.liters), 0))
        .filter(StockPurchase.entry_date >= start, StockPurchase.entry_date <= end)
        .scalar()
    )
    purchases_cost = float(
        db.session.query(func.coalesce(func.sum(StockPurchase.cost), 0))
        .filter(StockPurchase.entry_date >= start, StockPurchase.entry_date <= end)
        .scalar()
    )
    sales_returns_liters = float(
        db.session.query(func.coalesce(func.sum(SalesReturn.liters), 0))
        .filter(SalesReturn.entry_date >= start, SalesReturn.entry_date <= end)
        .scalar()
    )
    sales_returns_amount = float(
        db.session.query(func.coalesce(func.sum(SalesReturn.amount), 0))
        .filter(SalesReturn.entry_date >= start, SalesReturn.entry_date <= end)
        .scalar()
    )

    # Revenue stays available as the gross figure (what was actually rung
    # up on the nozzles) for its own line on the statement; net_revenue -
    # gross minus the same sales returns cogs_for_period() already netted
    # into COGS/margin - is what Gross Margin is actually built from, so
    # a return is subtracted exactly once on the way to Net Profit.
    net_revenue = round(revenue - sales_returns_amount, 2)
    gross_margin = round(net_revenue - cogs, 2)

    # Products have no weighted-average cost lookup at all (see
    # product_margin_for_period()'s docstring) - every line already
    # carries its own exact cost, so there's nothing here that mirrors
    # cogs_for_period()'s "as of end date" argument.
    product_revenue, product_cost, product_commission, product_category_detail = product_margin_for_period(
        start, end
    )
    total_gross_margin = round(gross_margin + product_commission, 2)
    # No further "- sales_returns_amount" here: net_revenue (and therefore
    # gross_margin) is already net of returns. Subtracting it again would
    # double-count the same refund - that was the bug this comment is
    # guarding against. Net Profit is Total Gross Margin (fuel + product)
    # minus operating costs only.
    net_profit = round(total_gross_margin - expenses_total - salaries_total, 2)

    return {
        "revenue": revenue,
        "net_revenue": net_revenue,
        "liters_sold": liters_sold,
        "testing_liters": testing_liters,
        "cogs": cogs,
        "cogs_detail": cogs_detail,
        "gross_margin": gross_margin,
        "product_revenue": product_revenue,
        "product_cost": product_cost,
        "product_commission": product_commission,
        "product_category_detail": product_category_detail,
        "total_gross_margin": total_gross_margin,
        "expenses_total": expenses_total,
        "expenses_by_category": expenses_by_category,
        "salaries_total": salaries_total,
        "net_profit": net_profit,
        "credit_given": credit_given,
        "receipts_total": receipts_total,
        "purchases_liters": purchases_liters,
        "purchases_cost": purchases_cost,
        "sales_returns_liters": sales_returns_liters,
        "sales_returns_amount": sales_returns_amount,
        "attendant_variances": attendant_variance_summary(start, end),
    }


@app.route("/reports/monthly")
@login_required
@owner_required
def reports_monthly():
    """Month-end closing statement. Unlike the Daily Report (which is a
    cash-movement view of one day), this is a period profit view: revenue
    against the cost of the fuel actually sold (COGS via weighted average
    purchase cost), then operating costs, then net."""
    today = date.today()
    start, end = _month_range_from_param(request.args.get("month", ""))
    ctx = _reports_monthly_context(start, end)

    # Previous/next month links, capped so you can't page into the future.
    prev_month = (start - timedelta(days=1)).replace(day=1)
    next_month = end + timedelta(days=1)
    return render_template(
        "reports_monthly.html",
        start=start,
        end=end,
        month_label=start.strftime("%B %Y"),
        prev_month=prev_month.strftime("%Y-%m"),
        next_month=next_month.strftime("%Y-%m") if next_month <= today else None,
        today=today,
        **ctx,
    )


@app.route("/reports/monthly/export")
@login_required
@owner_required
def reports_monthly_export():
    """Mirrors reports_monthly() - same figures, same month - as a PDF or
    Excel download instead of a page."""
    fmt = _resolve_export_format()
    start, end = _month_range_from_param(request.args.get("month", ""))
    ctx = _reports_monthly_context(start, end)

    blocks = [
        {
            # Traditional income-statement order - each "less:" line
            # subtracts from the one above it, ending at Net Profit.
            "type": "summary",
            "rows": [
                ("Fuel Revenue, Gross (Rs)", ctx["revenue"]),
                ("Liters Sold, Gross", ctx["liters_sold"]),
                ("Testing (L)", ctx["testing_liters"]),
                ("Less: Sales Returns (Rs)", ctx["sales_returns_amount"]),
                ("Sales Returns (L)", ctx["sales_returns_liters"]),
                ("Net Fuel Revenue (Rs)", ctx["net_revenue"]),
                ("Less: Cost of Fuel Sold (Rs)", ctx["cogs"]),
                ("Fuel Gross Margin (Rs)", ctx["gross_margin"]),
                ("Product Revenue (Rs)", ctx["product_revenue"]),
                ("Less: Cost of Products Sold (Rs)", ctx["product_cost"]),
                ("Product Gross Margin (Rs)", ctx["product_commission"]),
                ("Total Gross Margin (Rs)", ctx["total_gross_margin"]),
                ("Less: Expenses (Rs)", ctx["expenses_total"]),
                ("Less: Salaries (Rs)", ctx["salaries_total"]),
                ("Net Profit (Rs)", ctx["net_profit"]),
            ],
        },
        {
            "type": "table",
            "heading": "Margin by Fuel Type",
            "columns": [
                "Fuel", "Gross Revenue (Rs)", "Returns (L)", "Returns (Rs)",
                "Net Liters Sold", "Net Revenue (Rs)", "Avg Cost / L", "Cost (Rs)", "Margin (Rs)",
            ],
            "rows": [
                [
                    r["fuel"], r["gross_revenue"], r["returns_liters"], r["returns_amount"],
                    r["liters"], r["revenue"], r["unit_cost"], r["cost"], r["margin"],
                ]
                for r in ctx["cogs_detail"]
            ],
            "align": ["left", "right", "right", "right", "right", "right", "right", "right", "right"],
        },
        {
            "type": "table",
            "heading": "Margin by Product Category",
            "columns": ["Category", "Units", "Revenue (Rs)", "Cost (Rs)", "Margin (Rs)", "Margin %"],
            "rows": [
                [
                    r["category"].capitalize(), r["quantity"], r["revenue"], r["cost"], r["margin"],
                    round(r["margin"] / r["revenue"] * 100, 1) if r["revenue"] else 0.0,
                ]
                for r in ctx["product_category_detail"]
            ],
            "align": ["left", "right", "right", "right", "right", "right"],
        },
        {
            "type": "table",
            "heading": "Expenses by Category",
            "columns": ["Category", "Amount (Rs)"],
            "rows": [[category, amount] for category, amount in ctx["expenses_by_category"]],
            "align": ["left", "right"],
        },
        {
            "type": "table",
            "heading": "Credit & Stock Movement",
            "columns": ["Metric", "Value"],
            "rows": [
                ["Credit given to customers (Rs)", ctx["credit_given"]],
                ["Payments received (Rs)", ctx["receipts_total"]],
                ["Fuel purchased (L)", ctx["purchases_liters"]],
                ["Spent on fuel purchases (Rs)", ctx["purchases_cost"]],
            ],
            "align": ["left", "right"],
        },
        {
            "type": "table",
            "heading": "Cash Variance by Attendant",
            "columns": ["Attendant", "Shifts Reconciled", "Shifts Short", "Net Variance (Rs)"],
            "rows": [
                [r["name"], r["shifts"], r["shortfalls"], r["total_variance"]]
                for r in ctx["attendant_variances"]
            ],
            "align": ["left", "right", "right", "right"],
        },
    ]

    return _send_export(
        fmt,
        pdf_title=f"Monthly Report - {start.strftime('%B %Y')}",
        pdf_subtitle=f"{start.strftime('%d %b')} to {end.strftime('%d %b %Y')}",
        xlsx_sheet_name="Monthly Report",
        blocks=blocks,
        filename_base=f"petrol-khata-monthly-{start.strftime('%Y-%m')}",
    )


RANGE_OPTIONS = {15: "Past 15 days", 30: "Past month", 90: "Past 3 months", 365: "Past year"}


@app.route("/reports/trends")
@login_required
@owner_required
def reports_trends():
    days = request.args.get("range", 15, type=int)
    if days not in RANGE_OPTIONS:
        days = 15

    end = date.today()
    start = end - timedelta(days=days - 1)
    all_dates = [start + timedelta(days=i) for i in range(days)]
    label_fmt = "%b %d" if days <= 90 else "%b '%y"

    def group_sum(model, value_col, date_col="entry_date"):
        rows = (
            db.session.query(getattr(model, date_col), func.sum(value_col))
            .filter(getattr(model, date_col) >= start, getattr(model, date_col) <= end)
            .group_by(getattr(model, date_col))
            .all()
        )
        return {r[0]: r[1] or 0 for r in rows}

    sales_by_day = group_sum(Sale, Sale.total_amount)
    credit_by_day = group_sum(CreditGiven, CreditGiven.amount)
    payments_by_day = group_sum(Receipt, Receipt.amount)
    expenses_by_day = group_sum(Expense, Expense.amount)
    purchase_liters_by_day = group_sum(StockPurchase, StockPurchase.liters)
    salaries_by_day = group_sum(SalaryPayment, SalaryPayment.gross_amount)
    returns_amount_by_day = group_sum(SalesReturn, SalesReturn.amount)
    product_revenue_by_day = group_sum(ProductSale, ProductSale.amount)
    # Unlike cogs_by_day below, this needs no weighted-average/unit-cost
    # lookup at all - every ProductSale row already carries its own
    # purchase_rate, snapshotted at the moment of sale (see
    # product_margin_for_period()'s docstring), so quantity x that row's
    # own rate, summed per day, is already exact.
    product_cost_by_day = {
        r[0]: r[1] or 0
        for r in (
            db.session.query(ProductSale.entry_date, func.sum(ProductSale.quantity * ProductSale.purchase_rate))
            .filter(ProductSale.entry_date >= start, ProductSale.entry_date <= end)
            .group_by(ProductSale.entry_date)
            .all()
        )
    }

    # Daily profit values fuel at its weighted-average purchase cost (COGS)
    # rather than subtracting whatever was bought that day - otherwise a
    # tanker delivery shows as a huge "loss" on the day it arrives even
    # though the stock is still in the tank waiting to be sold. It also
    # nets sales returns into both revenue and COGS the same way
    # cogs_for_period()/_reports_monthly_context() do (net revenue minus
    # net COGS minus expenses minus salaries) - the Monthly Report and
    # this page used to disagree on the same month's profit because this
    # series ignored returns entirely; see the Phase 1 follow-up fix and
    # its regression test asserting the two pages reconcile. Product
    # margin (product revenue minus product cost, both exact per line -
    # see product_cost_by_day above) is added in on top, the same total
    # _reports_monthly_context() folds into total_gross_margin/net_profit,
    # so the two keep reconciling now that products exist too.
    unit_costs = {ft.id: weighted_avg_cost(ft, end) for ft in FuelType.query.all()}
    sold_liters_by_day_fuel = {}
    for entry_date_val, fuel_type_id, liters in (
        db.session.query(Sale.entry_date, Tank.fuel_type_id, func.sum(Sale.liters))
        .join(Nozzle, Sale.nozzle_id == Nozzle.id)
        .join(Tank, Nozzle.tank_id == Tank.id)
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .group_by(Sale.entry_date, Tank.fuel_type_id)
        .all()
    ):
        sold_liters_by_day_fuel[(entry_date_val, fuel_type_id)] = liters or 0
    returned_liters_by_day_fuel = {}
    for entry_date_val, fuel_type_id, liters in (
        db.session.query(SalesReturn.entry_date, SalesReturn.fuel_type_id, func.sum(SalesReturn.liters))
        .filter(SalesReturn.entry_date >= start, SalesReturn.entry_date <= end)
        .group_by(SalesReturn.entry_date, SalesReturn.fuel_type_id)
        .all()
    ):
        returned_liters_by_day_fuel[(entry_date_val, fuel_type_id)] = liters or 0

    # Net per (day, fuel) - a return reverses cost on the day it actually
    # happened (its own dated ledger entry), not retroactively on the
    # original sale's day. Summed across the whole window this always
    # equals cogs_for_period()'s per-fuel (gross sold - gross returned) x
    # unit cost, since that arithmetic distributes the same way regardless
    # of which day either side falls on - except in the edge case where
    # returns exceed sales for one fuel across the ENTIRE window, where
    # cogs_for_period() floors that fuel's cost at 0 for the period; a
    # day-by-day series has no single "period" to floor against, so it
    # isn't reproduced here. Not reachable in normal use (a fuel can't be
    # returned more than was ever sold of it).
    cogs_by_day = {}
    for key in set(sold_liters_by_day_fuel) | set(returned_liters_by_day_fuel):
        entry_date_val, fuel_type_id = key
        net_liters = sold_liters_by_day_fuel.get(key, 0) - returned_liters_by_day_fuel.get(key, 0)
        cogs_by_day[entry_date_val] = round(
            cogs_by_day.get(entry_date_val, 0) + net_liters * unit_costs.get(fuel_type_id, 0), 2
        )

    labels = [d.strftime(label_fmt) for d in all_dates]
    cash_series = [round(sales_by_day.get(d, 0) - credit_by_day.get(d, 0), 2) for d in all_dates]
    credit_series = [round(credit_by_day.get(d, 0), 2) for d in all_dates]
    receipts_series = [round(payments_by_day.get(d, 0), 2) for d in all_dates]
    expenses_series = [round(expenses_by_day.get(d, 0), 2) for d in all_dates]
    purchases_series = [round(purchase_liters_by_day.get(d, 0), 2) for d in all_dates]
    product_margin_series = [
        round(product_revenue_by_day.get(d, 0) - product_cost_by_day.get(d, 0), 2) for d in all_dates
    ]
    profit_series = [
        round(
            (sales_by_day.get(d, 0) - returns_amount_by_day.get(d, 0))  # net fuel revenue
            - cogs_by_day.get(d, 0)  # net fuel COGS
            - expenses_by_day.get(d, 0)
            - salaries_by_day.get(d, 0)
            + product_margin_series[i],
            2,
        )
        for i, d in enumerate(all_dates)
    ]

    sales_chart = charts.stacked_bar_chart(
        cash_series, credit_series, labels, ["#059669", "#dc2626"], ["Cash Sales", "Credit Given"]
    )
    cashflow_chart = charts.line_chart(
        [receipts_series, expenses_series], labels, ["#4f46e5", "#dc2626"], ["Receipts", "Expenses"]
    )
    purchases_chart = charts.bar_chart(purchases_series, labels, "#4f46e5")
    profit_chart = charts.line_chart([profit_series], labels, ["#059669"], ["Profit (Est.)"])

    tanks = Tank.query.order_by(Tank.number).all()
    tank_colors = ["#4f46e5", "#059669", "#d97706", "#dc2626", "#0891b2", "#7c3aed"]
    stock_series_list = [stock_series(t, all_dates) for t in tanks]
    stock_chart = (
        charts.line_chart(
            stock_series_list,
            labels,
            [tank_colors[i % len(tank_colors)] for i in range(len(tanks))],
            [t.label for t in tanks],
        )
        if tanks
        else ""
    )

    fuel_types = FuelType.query.order_by(FuelType.name).all()
    fuel_sold_by_day = {}
    for ft in fuel_types:
        rows = (
            db.session.query(Sale.entry_date, func.sum(Sale.liters))
            .join(Nozzle, Sale.nozzle_id == Nozzle.id)
            .join(Tank, Nozzle.tank_id == Tank.id)
            .filter(Tank.fuel_type_id == ft.id, Sale.entry_date >= start, Sale.entry_date <= end)
            .group_by(Sale.entry_date)
            .all()
        )
        fuel_sold_by_day[ft.name] = {r[0]: r[1] or 0 for r in rows}
    fuel_sold_series_list = [
        [round(fuel_sold_by_day[ft.name].get(d, 0), 2) for d in all_dates] for ft in fuel_types
    ]
    fuel_sold_totals = {ft.name: round(sum(fuel_sold_by_day[ft.name].values()), 2) for ft in fuel_types}
    fuel_sold_chart = (
        charts.line_chart(
            fuel_sold_series_list,
            labels,
            [tank_colors[i % len(tank_colors)] for i in range(len(fuel_types))],
            [ft.name for ft in fuel_types],
        )
        if fuel_types
        else ""
    )

    totals = dict(
        total_sales=sum(sales_by_day.values()),
        total_credit_given=sum(credit_by_day.values()),
        total_receipts=sum(payments_by_day.values()),
        total_expenses=sum(expenses_by_day.values()),
        total_purchased_liters=sum(purchase_liters_by_day.values()),
        total_cogs=round(sum(cogs_by_day.values()), 2),
        total_salaries=round(sum(salaries_by_day.values()), 2),
        total_sales_returns=round(sum(returns_amount_by_day.values()), 2),
        total_product_revenue=round(sum(product_revenue_by_day.values()), 2),
        total_product_cost=round(sum(product_cost_by_day.values()), 2),
        total_product_margin=round(sum(product_margin_series), 2),
        total_profit=round(sum(profit_series), 2),
    )

    return render_template(
        "trends.html",
        days=days,
        range_options=RANGE_OPTIONS,
        sales_chart=sales_chart,
        cashflow_chart=cashflow_chart,
        purchases_chart=purchases_chart,
        stock_chart=stock_chart,
        fuel_sold_chart=fuel_sold_chart,
        fuel_sold_totals=fuel_sold_totals,
        profit_chart=profit_chart,
        totals=totals,
    )


# --------------------------------------------------------- first-time db --

def ensure_seed_users():
    # The Alembic CLI (flask db migrate/upgrade/...) imports this module to
    # get at `app`, which re-runs this whole function - without this guard
    # a plain `flask db upgrade` would recurse into migrate_upgrade() from
    # here too, and generating a fresh autogenerate diff against a
    # partially-migrated DB would produce a broken migration.
    if os.environ.get("SKIP_DB_BOOTSTRAP") == "1":
        return

    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()
    if "user" in existing_tables and "alembic_version" not in existing_tables:
        # A pre-migrations database (every production DB and most local
        # ones as of adopting Alembic) - its schema already matches the
        # baseline migration exactly, since that migration was generated
        # from these same models. Stamping records it as already at head
        # without re-running (and failing on) CREATE TABLE for tables that
        # already exist.
        migrate_stamp()

    # No-op once at head; builds the full schema from migrations on a
    # brand-new empty database (replaces the old db.create_all()).
    migrate_upgrade()

    # Every reading/credit/bank-sale row needs a shift, so one always has
    # to exist. A pump that doesn't split its day just leaves this single
    # shift in place and never sees a shift selector anywhere.
    if Shift.query.count() == 0:
        db.session.add(Shift(name="Full Day", sort_order=0))
        db.session.commit()

    if User.query.count() == 0:
        owner = User(username="owner", role="owner")
        owner.set_password("owner123")
        staff = User(username="staff", role="staff")
        staff.set_password("staff123")
        db.session.add_all([owner, staff])
        print("=" * 60)
        print("Created default accounts (please change these passwords):")
        print("  Owner -> username: owner  password: owner123")
        print("  Staff -> username: staff  password: staff123")
        print("=" * 60)
        db.session.commit()


with app.app_context():
    ensure_seed_users()


if __name__ == "__main__":
    app.run(debug=True)
