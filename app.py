import os
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf import CSRFProtect
from sqlalchemy import func

import charts
from extensions import db, login_manager
from ledger_logic import (
    account_ledger_events,
    book_stock,
    cash_account_balance,
    nearest_earlier_reading,
    next_sale_on_or_after,
    previous_reading_for,
    sales_breakdown_for_date,
    stock_series,
)
from models import (
    Account,
    BankAccount,
    BankSale,
    CashAccount,
    CashDeposit,
    CreditGiven,
    CustomerPayment,
    Dispenser,
    EmployeeLoan,
    EmployeeRepayment,
    Expense,
    FuelType,
    Nozzle,
    Sale,
    StockPurchase,
    SupplierPayment,
    Tank,
    TankDip,
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


@app.before_request
def enforce_setup_flow():
    if request.endpoint in (None, "static", "login", "logout"):
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
            login_user(user)
            return redirect(url_for("ledger"))
        flash("Incorrect username or password.", "error")

    return render_template("login.html")


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
            flash("Setup complete! Munchi is ready to use.", "success")
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
    return render_template(
        "settings.html",
        tanks=tanks,
        fuel_types=fuel_types,
        dispensers=dispensers,
        bank_accounts=bank_accounts,
        cash_account=cash_account,
        cash_balance=cash_account_balance(cash_account),
        today=date.today(),
    )


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
        fuel.price_per_liter = price
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
    for p in CustomerPayment.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "payment", "sort": p.recorded_at, "obj": p})
    for c in CreditGiven.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "credit", "sort": c.recorded_at, "obj": c})
    for dip in TankDip.query.filter_by(entry_date=entry_date).all():
        variance = round(dip.dip_liters - book_stock(dip.tank, dip.entry_date), 2)
        events.append({"kind": "dip", "sort": dip.recorded_at, "obj": dip, "variance": variance})
    for bs in BankSale.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "bank_sale", "sort": bs.recorded_at, "obj": bs})
    for el in EmployeeLoan.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "employee_loan", "sort": el.recorded_at, "obj": el})
    for er in EmployeeRepayment.query.filter_by(entry_date=entry_date).all():
        events.append({"kind": "employee_repayment", "sort": er.recorded_at, "obj": er})

    if full_visibility:
        for e in Expense.query.filter_by(entry_date=entry_date).all():
            events.append({"kind": "expense", "sort": e.recorded_at, "obj": e})
        for pu in StockPurchase.query.filter_by(entry_date=entry_date).all():
            events.append({"kind": "purchase", "sort": pu.recorded_at, "obj": pu})
        for sp in SupplierPayment.query.filter_by(entry_date=entry_date).all():
            events.append({"kind": "supplier_payment", "sort": sp.recorded_at, "obj": sp})
        for cd in CashDeposit.query.filter_by(entry_date=entry_date).all():
            events.append({"kind": "cash_deposit", "sort": cd.recorded_at, "obj": cd})

    events.sort(key=lambda e: e["sort"], reverse=True)
    return events


@app.route("/ledger")
@login_required
def ledger():
    selected_date = parse_date_param(request.args.get("date"))

    dispensers = Dispenser.query.order_by(Dispenser.number).all()
    nozzle_rows = []
    for d in dispensers:
        for n in d.nozzles:
            existing = Sale.query.filter_by(nozzle_id=n.id, entry_date=selected_date).first()
            prev_value, prev_auto = previous_reading_for(n, selected_date)
            if not prev_auto and existing:
                prev_value = existing.previous_reading
            nozzle_rows.append(
                {
                    "nozzle": n,
                    "dispenser": d,
                    "previous_reading": prev_value,
                    "previous_is_auto": prev_auto,
                    "existing_reading": existing.current_reading if existing else None,
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
                "is_low": stock <= t.low_stock_threshold,
            }
        )

    fuel_types = FuelType.query.order_by(FuelType.name).all()
    # Every picker on this page (customer, supplier, employee) lists every
    # account - an account's type label is just a default/hint, not a
    # restriction, so any account can receive any kind of entry (e.g. an
    # account labelled "supplier" can still be given customer credit).
    accounts = Account.query.order_by(Account.name).all()
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()

    breakdown = sales_breakdown_for_date(selected_date)
    total_sales = breakdown["total"]

    feed = get_feed_for_date(selected_date, full_visibility=current_user.is_owner)

    summary = None
    if current_user.is_owner:
        credit_total = breakdown["credit"]
        receipts_total = (
            db.session.query(func.coalesce(func.sum(CustomerPayment.amount), 0))
            .filter(CustomerPayment.entry_date == selected_date)
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
        summary = dict(
            debit_total=total_sales + receipts_total - credit_total,
            credit_total=expenses_total + cash_purchases_total + supplier_payments_total,
        )

    return render_template(
        "ledger.html",
        selected_date=selected_date,
        today=date.today(),
        nozzle_rows=nozzle_rows,
        tank_rows=tank_rows,
        fuel_types=fuel_types,
        accounts=accounts,
        bank_accounts=bank_accounts,
        feed=feed,
        total_sales=total_sales,
        breakdown=breakdown,
        summary=summary,
    )


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


@app.route("/ledger/readings", methods=["POST"])
@login_required
def ledger_readings():
    entry_date = parse_date_param(request.form.get("entry_date"))
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

        auto_previous, is_auto = previous_reading_for(nozzle, entry_date)
        backfill_prior = None

        if is_auto:
            previous = auto_previous
        else:
            raw_previous = request.form.get(f"previous_{nozzle.id}", "").strip()
            if not raw_previous:
                errors.append(
                    f"{nozzle.label}: there's no entry for the day before {entry_date}, so "
                    f"please enter both the previous and current reading."
                )
                continue
            try:
                previous = float(raw_previous)
            except ValueError:
                errors.append(f"{nozzle.label}: previous reading is not a valid number.")
                continue

            floor = nearest_earlier_reading(nozzle, entry_date)
            if previous < floor:
                errors.append(
                    f"{nozzle.label}: previous reading ({previous:g}) can't be lower than an "
                    f"earlier recorded reading ({floor:g})."
                )
                continue

            # Close the gap: the prior calendar day gets a Sale of its own,
            # using the previous reading just typed in as ITS current
            # reading - but only when that day is still empty (never
            # overwrite a real entry) and only when ITS own previous
            # reading can be determined automatically. A deeper multi-day
            # gap is left for the user to fill in when they visit those
            # dates directly, rather than guessing on their behalf.
            prior_date = entry_date - timedelta(days=1)
            if not Sale.query.filter_by(nozzle_id=nozzle.id, entry_date=prior_date).first():
                prior_previous, prior_is_auto = previous_reading_for(nozzle, prior_date)
                if prior_is_auto:
                    backfill_prior = {
                        "entry_date": prior_date,
                        "previous_reading": prior_previous,
                        "current_reading": previous,
                    }

        if current_reading < previous:
            errors.append(
                f"{nozzle.label}: reading ({current_reading:g}) is less than the previous "
                f"reading ({previous:g})."
            )
            continue

        next_sale = next_sale_on_or_after(nozzle.id, entry_date)
        if next_sale and current_reading > next_sale.current_reading:
            errors.append(
                f"{nozzle.label}: reading ({current_reading:g}) is more than a later reading "
                f"already recorded on {next_sale.entry_date} ({next_sale.current_reading:g})."
            )
            continue

        liters = round(current_reading - previous, 2)
        existing = Sale.query.filter_by(nozzle_id=nozzle.id, entry_date=entry_date).first()
        if liters == 0 and not existing and not backfill_prior:
            continue

        price = nozzle.tank.fuel_type.price_per_liter
        total_amount = round(liters * price, 2)

        if backfill_prior:
            bf_liters = round(
                backfill_prior["current_reading"] - backfill_prior["previous_reading"], 2
            )
            db.session.add(
                Sale(
                    nozzle_id=nozzle.id,
                    entry_date=backfill_prior["entry_date"],
                    previous_reading=backfill_prior["previous_reading"],
                    current_reading=backfill_prior["current_reading"],
                    liters=bf_liters,
                    price_per_liter=price,
                    total_amount=round(bf_liters * price, 2),
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
        db.session.commit()
        flash(f"Saved {saved} nozzle reading(s) for {entry_date}.", "success")
    if errors:
        for e in errors:
            flash(e, "error")
    if not saved and not errors:
        flash("No new readings entered.", "error")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/dip", methods=["POST"])
@login_required
def ledger_dip():
    entry_date = parse_date_param(request.form.get("entry_date"))
    tanks = Tank.query.all()
    saved = 0

    for tank in tanks:
        raw = request.form.get(f"dip_{tank.id}", "").strip()
        if not raw:
            continue
        try:
            dip_value = float(raw)
        except ValueError:
            flash(f"Tank {tank.number}: not a valid number.", "error")
            continue
        if dip_value < 0:
            flash(f"Tank {tank.number}: dip must be zero or more.", "error")
            continue

        existing = TankDip.query.filter_by(tank_id=tank.id, entry_date=entry_date).first()
        if existing:
            existing.dip_liters = dip_value
            existing.user_id = current_user.id
        else:
            db.session.add(
                TankDip(
                    tank_id=tank.id, entry_date=entry_date, dip_liters=dip_value, user_id=current_user.id
                )
            )
        saved += 1

    if saved:
        db.session.commit()
        flash(f"Saved {saved} dip reading(s) for {entry_date}.", "success")
    else:
        flash("No dip readings entered.", "error")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/payment", methods=["POST"])
@login_required
def ledger_payment():
    entry_date = parse_date_param(request.form.get("entry_date"))
    customer, error = resolve_customer(request.form)
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()

    if error:
        db.session.rollback()
        flash(error, "error")
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Payment amount must be a positive number.", "error")
    else:
        db.session.add(
            CustomerPayment(
                account_id=customer.id,
                entry_date=entry_date,
                amount=amount,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded payment of Rs {amount:,.2f} from {customer.name}.", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/credit", methods=["POST"])
@login_required
def ledger_credit():
    entry_date = parse_date_param(request.form.get("entry_date"))
    customer, error = resolve_customer(request.form)
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    liters = request.form.get("liters", type=float)
    note = request.form.get("note", "").strip()

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
        amount = round(liters * fuel.price_per_liter, 2)
        db.session.add(
            CreditGiven(
                account_id=customer.id,
                fuel_type_id=fuel.id,
                entry_date=entry_date,
                liters=liters,
                price_per_liter=fuel.price_per_liter,
                amount=amount,
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

    if not category:
        flash("Please enter an expense category.", "error")
    elif not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    else:
        db.session.add(
            Expense(
                entry_date=entry_date,
                category=category,
                description=description or None,
                amount=amount,
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
    if payment_type == "credit":
        supplier, supplier_error = resolve_supplier(request.form)

    if not tank:
        db.session.rollback()
        flash("Please choose a valid tank.", "error")
    elif not liters or liters <= 0:
        db.session.rollback()
        flash("Liters must be a positive number.", "error")
    elif payment_type == "credit" and supplier_error:
        db.session.rollback()
        flash(supplier_error, "error")
    else:
        db.session.add(
            StockPurchase(
                tank_id=tank.id,
                entry_date=entry_date,
                liters=liters,
                cost=cost,
                payment_type=payment_type,
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

    if error:
        db.session.rollback()
        flash(error, "error")
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Payment amount must be a positive number.", "error")
    else:
        db.session.add(
            SupplierPayment(
                account_id=supplier.id,
                entry_date=entry_date,
                amount=amount,
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

    if error:
        db.session.rollback()
        flash(error, "error")
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Amount must be a positive number.", "error")
    else:
        db.session.add(
            BankSale(
                bank_account_id=bank_account.id,
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

    if error:
        db.session.rollback()
        flash(error, "error")
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Amount must be a positive number.", "error")
    else:
        db.session.add(
            EmployeeLoan(
                account_id=employee.id,
                entry_date=entry_date,
                amount=amount,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded loan/advance of Rs {amount:,.2f} to {employee.name}.", "success")

    return redirect(url_for("ledger", date=entry_date))


@app.route("/ledger/employee-repayment", methods=["POST"])
@login_required
def ledger_employee_repayment():
    entry_date = parse_date_param(request.form.get("entry_date"))
    employee, error = resolve_employee(request.form)
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()

    if error:
        db.session.rollback()
        flash(error, "error")
    elif not amount or amount <= 0:
        db.session.rollback()
        flash("Amount must be a positive number.", "error")
    else:
        db.session.add(
            EmployeeRepayment(
                account_id=employee.id,
                entry_date=entry_date,
                amount=amount,
                note=note or None,
                user_id=current_user.id,
            )
        )
        db.session.commit()
        flash(f"Recorded repayment of Rs {amount:,.2f} from {employee.name}.", "success")

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

    query = Account.query
    if type_filter in ACCOUNT_TYPES:
        query = query.filter_by(account_type=type_filter)

    rows = []
    for a in query.all():
        # Debitor/creditor is purely a function of the account's current
        # balance sign - not its type label - so an account's
        # classification here can shift over time as its balance shifts.
        rows.append({"account": a, "balance": a.balance})

    if kind == "debitors":
        rows = [r for r in rows if r["balance"] > 0]
    elif kind == "creditors":
        rows = [r for r in rows if r["balance"] < 0]

    rows.sort(key=lambda r: r["account"].name.lower())

    return render_template(
        "accounts.html", rows=rows, kind=kind, type_filter=type_filter, today=date.today()
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
    return render_template(
        "account_detail.html",
        account=account,
        events=events,
        today=date.today(),
        fuel_types=fuel_types,
        tanks=tanks,
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


@app.route("/accounts/entry/credit/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_credit_edit(entry_id):
    entry = db.session.get(CreditGiven, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    liters = request.form.get("liters", type=float)
    note = request.form.get("note", "").strip()
    fuel = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None

    if not fuel:
        flash("Please choose a valid fuel type.", "error")
    elif not liters or liters <= 0:
        flash("Liters must be a positive number.", "error")
    else:
        entry.entry_date = entry_date
        entry.fuel_type_id = fuel.id
        entry.liters = liters
        entry.price_per_liter = fuel.price_per_liter
        entry.amount = round(liters * fuel.price_per_liter, 2)
        entry.note = note or None
        db.session.commit()
        flash("Credit entry updated.", "success")

    return redirect(url_for("account_detail", account_id=entry.account_id))


@app.route("/accounts/entry/customer-payment/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_customer_payment_edit(entry_id):
    entry = db.session.get(CustomerPayment, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()

    if not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.note = note or None
        db.session.commit()
        flash("Payment updated.", "success")

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

    if not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
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

    if not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.note = note or None
        db.session.commit()
        flash("Loan updated.", "success")

    return redirect(url_for("account_detail", account_id=entry.account_id))


@app.route("/accounts/entry/employee-repayment/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_employee_repayment_edit(entry_id):
    entry = db.session.get(EmployeeRepayment, entry_id) or abort(404)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()

    if not amount or amount <= 0:
        flash("Amount must be a positive number.", "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.note = note or None
        db.session.commit()
        flash("Repayment updated.", "success")

    return redirect(url_for("account_detail", account_id=entry.account_id))


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

    by_fuel = {}
    for s in sales:
        d = by_fuel.setdefault(s.nozzle.tank.fuel_type.name, {"liters": 0.0, "revenue": 0.0})
        d["liters"] += s.liters
        d["revenue"] += s.total_amount

    credit_given = CreditGiven.query.filter_by(entry_date=selected_date).all()
    total_credit_given = sum(c.amount for c in credit_given)

    bank_sales = BankSale.query.filter_by(entry_date=selected_date).all()
    total_bank_sales = sum(b.amount for b in bank_sales)
    cash_sales = total_sales - total_credit_given - total_bank_sales

    payments = CustomerPayment.query.filter_by(entry_date=selected_date).all()
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
    )
    outstanding_credit = sum(b for a in Account.query.all() if (b := a.balance) > 0)

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
    payments_by_day = group_sum(CustomerPayment, CustomerPayment.amount)
    expenses_by_day = group_sum(Expense, Expense.amount)
    purchase_liters_by_day = group_sum(StockPurchase, StockPurchase.liters)

    labels = [d.strftime(label_fmt) for d in all_dates]
    cash_series = [round(sales_by_day.get(d, 0) - credit_by_day.get(d, 0), 2) for d in all_dates]
    credit_series = [round(credit_by_day.get(d, 0), 2) for d in all_dates]
    receipts_series = [round(payments_by_day.get(d, 0), 2) for d in all_dates]
    expenses_series = [round(expenses_by_day.get(d, 0), 2) for d in all_dates]
    purchases_series = [round(purchase_liters_by_day.get(d, 0), 2) for d in all_dates]

    sales_chart = charts.stacked_bar_chart(
        cash_series, credit_series, labels, ["#059669", "#dc2626"], ["Cash Sales", "Credit Given"]
    )
    cashflow_chart = charts.line_chart(
        [receipts_series, expenses_series], labels, ["#4f46e5", "#dc2626"], ["Receipts", "Expenses"]
    )
    purchases_chart = charts.bar_chart(purchases_series, labels, "#4f46e5")

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

    totals = dict(
        total_sales=sum(sales_by_day.values()),
        total_credit_given=sum(credit_by_day.values()),
        total_receipts=sum(payments_by_day.values()),
        total_expenses=sum(expenses_by_day.values()),
        total_purchased_liters=sum(purchase_liters_by_day.values()),
    )

    return render_template(
        "trends.html",
        days=days,
        range_options=RANGE_OPTIONS,
        sales_chart=sales_chart,
        cashflow_chart=cashflow_chart,
        purchases_chart=purchases_chart,
        stock_chart=stock_chart,
        totals=totals,
    )


# --------------------------------------------------------- first-time db --

def ensure_seed_users():
    db.create_all()

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
