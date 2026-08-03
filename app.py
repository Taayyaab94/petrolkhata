import csv
import io
import os
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
    record_fuel_price,
    sales_breakdown_for_date,
    stock_series,
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
    Receipt,
    SalaryPayment,
    Sale,
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
            tanks.append({"fuel_name": fuel_name, "capacity": capacity, "stock": stock})

        if not tanks and not error:
            error = "Please add at least one tank."

        if error:
            flash(error, "error")
        else:
            session["setup"] = {"tanks": tanks}
            return redirect(url_for("setup_prices"))

    return render_template("setup_tanks.html")


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
                tank = Tank(
                    number=i + 1,
                    fuel_type_id=fuel_type_by_name[t["fuel_name"].lower()].id,
                    capacity_liters=t["capacity"],
                    starting_stock_liters=t["stock"],
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
        today=date.today(),
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

    if not fuel_name:
        flash("Please enter a fuel name.", "error")
    elif not capacity or capacity <= 0:
        flash("Please enter a valid capacity.", "error")
    elif stock is None or stock < 0 or stock > capacity:
        flash("Please enter a valid starting stock (not more than capacity).", "error")
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

    if not capacity or capacity <= 0:
        flash("Please enter a valid capacity.", "error")
    elif threshold is None or threshold < 0:
        flash("Please enter a valid low-stock alert level.", "error")
    else:
        tank.capacity_liters = capacity
        tank.low_stock_threshold = threshold
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
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()

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
        bank_accounts=bank_accounts,
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

        liters = round(current_reading - previous, 2)
        existing = Sale.query.filter_by(
            nozzle_id=nozzle.id, entry_date=entry_date, shift_id=shift.id
        ).first()
        if liters == 0 and not existing and not backfill_prior:
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
                    price_per_liter=bf_price,
                    total_amount=round(bf_liters * bf_price, 2),
                    user_id=current_user.id,
                )
            )

        if existing:
            existing.previous_reading = previous
            existing.current_reading = current_reading
            existing.liters = liters
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
                    price_per_liter=price,
                    total_amount=total_amount,
                    user_id=current_user.id,
                )
            )
        saved += 1

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

        existing = TankDip.query.filter_by(tank_id=tank.id, entry_date=entry_date).first()
        if existing:
            existing.dip_cm = dip_cm
            existing.dip_liters = dip_value
            existing.user_id = current_user.id
        else:
            db.session.add(
                TankDip(
                    tank_id=tank.id,
                    entry_date=entry_date,
                    dip_cm=dip_cm,
                    dip_liters=dip_value,
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
    entry_date = parse_date_param(request.form.get("entry_date"))
    customer, error = resolve_customer(request.form)
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    liters = request.form.get("liters", type=float)
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
    elif not liters or liters <= 0:
        db.session.rollback()
        flash("Liters must be a positive number.", "error")
    else:
        price = price_on_date(fuel, entry_date)
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
        flash(
            f"Recorded {liters:g} L {fuel.name} (Rs {amount:,.2f}) on credit for {customer.name}.",
            "success",
        )

    return redirect(url_for("ledger", date=entry_date))


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
    entry_date = parse_date_param(request.form.get("entry_date"))
    employee, error = resolve_employee(request.form)
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
                account_id=employee.id,
                entry_date=entry_date,
                amount=amount,
                method=method,
                bank_account_id=bank_account.id if bank_account else None,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded loan/advance of Rs {amount:,.2f} to {employee.name}.", "success")

    return redirect(url_for("ledger", date=entry_date))


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
    return render_template(
        "inventory.html",
        tank_rows=tank_rows,
        by_fuel=by_fuel,
        dispensers=dispensers,
        recent_purchases=recent_purchases,
        suppliers=suppliers,
    )


# ------------------------------------------------------------ accounts ---

ACCOUNT_TYPES = ("customer", "supplier", "employee")


@app.route("/accounts")
@login_required
def accounts():
    kind = request.args.get("kind", "all")
    type_filter = request.args.get("type", "all")
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

    return render_template(
        "accounts.html",
        rows=rows,
        kind=kind,
        type_filter=type_filter,
        today=date.today(),
        expenses=expenses,
        bank_accounts=bank_accounts,
        aging_totals=aging_totals,
        aging_total=round(sum(aging_totals.values()), 2),
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
    entry = db.session.get(CreditGiven, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    liters = request.form.get("liters", type=float)
    vehicle_number = request.form.get("vehicle_number", "").strip()
    note = request.form.get("note", "").strip()
    fuel = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None

    if not fuel:
        flash("Please choose a valid fuel type.", "error")
    elif not liters or liters <= 0:
        flash("Liters must be a positive number.", "error")
    else:
        price = price_on_date(fuel, entry_date)
        entry.entry_date = entry_date
        entry.fuel_type_id = fuel.id
        entry.liters = liters
        entry.price_per_liter = price
        entry.amount = round(liters * price, 2)
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


@app.route("/accounts/<int:account_id>/statement")
@login_required
def account_statement(account_id):
    """A print-friendly statement for a date range - the thing you actually
    hand or send to a credit customer. Shows the balance carried into the
    range, every entry inside it, and the closing balance, all derived from
    the same account_ledger_events() the detail page uses."""
    account = db.session.get(Account, account_id) or abort(404)
    today = date.today()
    from_date = parse_date_param(request.args.get("from"), fallback=today.replace(day=1))
    to_date = parse_date_param(request.args.get("to"), fallback=today)

    all_events = list(reversed(account_ledger_events(account)))  # oldest first
    before = [e for e in all_events if e["entry_date"] < from_date]
    inside = [e for e in all_events if from_date <= e["entry_date"] <= to_date]
    opening = round(sum(e["delta"] for e in before), 2)

    return render_template(
        "account_statement.html",
        account=account,
        from_date=from_date,
        to_date=to_date,
        opening=opening,
        events=inside,
        closing=inside[-1]["running_balance"] if inside else opening,
        aging=credit_aging(account, to_date),
        today=today,
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

@app.route("/reports")
@login_required
@owner_required
def reports():
    selected_date = parse_date_param(request.args.get("date"))

    sales = (
        Sale.query.filter_by(entry_date=selected_date)
        .join(Nozzle)
        .order_by(Nozzle.dispenser_id, Nozzle.nozzle_number)
        .all()
    )
    total_sales = sum(s.total_amount for s in sales)
    total_liters = sum(s.liters for s in sales)
    by_fuel = fuel_sales_for_date(selected_date)

    credit_given = CreditGiven.query.filter_by(entry_date=selected_date).all()
    total_credit_given = sum(c.amount for c in credit_given)

    bank_sales = BankSale.query.filter_by(entry_date=selected_date).all()
    total_bank_sales = sum(b.amount for b in bank_sales)
    cash_sales = total_sales - total_credit_given - total_bank_sales

    payments = Receipt.query.filter_by(entry_date=selected_date).all()
    total_payments = sum(p.amount for p in payments)

    expenses = Expense.query.filter_by(entry_date=selected_date).order_by(Expense.recorded_at).all()
    total_expenses = sum(e.amount for e in expenses)

    purchases = (
        StockPurchase.query.filter_by(entry_date=selected_date).order_by(StockPurchase.recorded_at).all()
    )
    total_purchased_liters = sum(p.liters for p in purchases)
    cash_purchases_total = sum(p.cost or 0 for p in purchases if p.payment_type == "cash")

    supplier_payments = SupplierPayment.query.filter_by(entry_date=selected_date).all()
    total_supplier_payments = sum(p.amount for p in supplier_payments)

    salaries = SalaryPayment.query.filter_by(entry_date=selected_date).all()
    total_salaries_net = sum(s.net_paid for s in salaries)

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
    )
    outstanding_credit = sum(b for a in Account.query.all() if (b := a.balance) > 0)
    cash_balance = cash_account_balance(get_cash_account())
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()

    return render_template(
        "reports.html",
        selected_date=selected_date,
        today=date.today(),
        total_sales=total_sales,
        total_liters=total_liters,
        by_fuel=by_fuel,
        cash_sales=cash_sales,
        total_credit_given=total_credit_given,
        bank_sales=bank_sales,
        total_bank_sales=total_bank_sales,
        total_payments=total_payments,
        expenses=expenses,
        total_expenses=total_expenses,
        purchases=purchases,
        total_purchased_liters=total_purchased_liters,
        total_supplier_payments=total_supplier_payments,
        tank_rows=tank_rows,
        net_cash_flow=net_cash_flow,
        outstanding_credit=outstanding_credit,
        cash_balance=cash_balance,
        bank_accounts=bank_accounts,
        salaries=salaries,
        total_salaries_net=total_salaries_net,
        handover_rows=handover_rows_for_date(selected_date),
    )


@app.route("/reports/monthly")
@login_required
@owner_required
def reports_monthly():
    """Month-end closing statement. Unlike the Daily Report (which is a
    cash-movement view of one day), this is a period profit view: revenue
    against the cost of the fuel actually sold (COGS via weighted average
    purchase cost), then operating costs, then net."""
    raw_month = request.args.get("month", "")
    today = date.today()
    try:
        year, month = (int(p) for p in raw_month.split("-"))
        start = date(year, month, 1)
    except (ValueError, AttributeError):
        start = today.replace(day=1)
    end = (start + timedelta(days=31)).replace(day=1) - timedelta(days=1)

    revenue = (
        db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .scalar()
    )
    liters_sold = (
        db.session.query(func.coalesce(func.sum(Sale.liters), 0))
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .scalar()
    )
    cogs, cogs_detail = cogs_for_period(start, end)
    expenses_total = (
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.entry_date >= start, Expense.entry_date <= end)
        .scalar()
    )
    salaries_total = (
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

    credit_given = (
        db.session.query(func.coalesce(func.sum(CreditGiven.amount), 0))
        .filter(CreditGiven.entry_date >= start, CreditGiven.entry_date <= end)
        .scalar()
    )
    receipts_total = (
        db.session.query(func.coalesce(func.sum(Receipt.amount), 0))
        .filter(Receipt.entry_date >= start, Receipt.entry_date <= end)
        .scalar()
    )
    purchases_liters = (
        db.session.query(func.coalesce(func.sum(StockPurchase.liters), 0))
        .filter(StockPurchase.entry_date >= start, StockPurchase.entry_date <= end)
        .scalar()
    )
    purchases_cost = (
        db.session.query(func.coalesce(func.sum(StockPurchase.cost), 0))
        .filter(StockPurchase.entry_date >= start, StockPurchase.entry_date <= end)
        .scalar()
    )

    gross_margin = round(revenue - cogs, 2)
    net_profit = round(gross_margin - expenses_total - salaries_total, 2)

    # Previous/next month links, capped so you can't page into the future.
    prev_month = (start - timedelta(days=1)).replace(day=1)
    next_month = (end + timedelta(days=1))
    return render_template(
        "reports_monthly.html",
        start=start,
        end=end,
        month_label=start.strftime("%B %Y"),
        prev_month=prev_month.strftime("%Y-%m"),
        next_month=next_month.strftime("%Y-%m") if next_month <= today else None,
        revenue=revenue,
        liters_sold=liters_sold,
        cogs=cogs,
        cogs_detail=cogs_detail,
        gross_margin=gross_margin,
        expenses_total=expenses_total,
        expenses_by_category=expenses_by_category,
        salaries_total=salaries_total,
        net_profit=net_profit,
        credit_given=credit_given,
        receipts_total=receipts_total,
        purchases_liters=purchases_liters,
        purchases_cost=purchases_cost,
        attendant_variances=attendant_variance_summary(start, end),
        today=today,
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

    # Daily profit values fuel at its weighted-average purchase cost (COGS)
    # rather than subtracting whatever was bought that day - otherwise a
    # tanker delivery shows as a huge "loss" on the day it arrives even
    # though the stock is still in the tank waiting to be sold.
    unit_costs = {ft.id: weighted_avg_cost(ft, end) for ft in FuelType.query.all()}
    cogs_rows = (
        db.session.query(Sale.entry_date, Tank.fuel_type_id, func.sum(Sale.liters))
        .join(Nozzle, Sale.nozzle_id == Nozzle.id)
        .join(Tank, Nozzle.tank_id == Tank.id)
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .group_by(Sale.entry_date, Tank.fuel_type_id)
        .all()
    )
    cogs_by_day = {}
    for entry_date_val, fuel_type_id, liters in cogs_rows:
        cogs_by_day[entry_date_val] = round(
            cogs_by_day.get(entry_date_val, 0) + (liters or 0) * unit_costs.get(fuel_type_id, 0), 2
        )

    labels = [d.strftime(label_fmt) for d in all_dates]
    cash_series = [round(sales_by_day.get(d, 0) - credit_by_day.get(d, 0), 2) for d in all_dates]
    credit_series = [round(credit_by_day.get(d, 0), 2) for d in all_dates]
    receipts_series = [round(payments_by_day.get(d, 0), 2) for d in all_dates]
    expenses_series = [round(expenses_by_day.get(d, 0), 2) for d in all_dates]
    purchases_series = [round(purchase_liters_by_day.get(d, 0), 2) for d in all_dates]
    profit_series = [
        round(
            sales_by_day.get(d, 0)
            - cogs_by_day.get(d, 0)
            - expenses_by_day.get(d, 0)
            - salaries_by_day.get(d, 0),
            2,
        )
        for d in all_dates
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
