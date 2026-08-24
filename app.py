import csv
import hashlib
import hmac
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
from sqlalchemy.orm import joinedload
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth

import charts
from formatting import format_number
import email_service
from exports import build_pdf, build_xlsx
from extensions import db, login_manager, migrate
from tenancy import current_pump_id, register_tenancy_events, unscoped
from ledger_logic import (
    account_ledger_events,
    account_positions,
    active_shifts,
    allocate_group_payment,
    attendant_variance_summary,
    bank_account_balance_as_of,
    bank_account_ledger_events,
    book_stock,
    break_even_liters,
    cash_account_balance,
    cash_account_balance_as_of,
    cash_account_ledger_events,
    cash_balance_sparkline,
    cash_would_go_negative,
    cash_movement_for_date,
    cash_movement_for_period,
    best_sales_day_for_period,
    cogs_for_period,
    credit_aging,
    credit_discounts_for_period,
    credit_given_sparkline,
    customer_concentration,
    daily_margin,
    dashboard_trend_series,
    day_completeness,
    dead_stock,
    default_shift,
    dip_variance_for_date,
    first_negative_cash_date,
    fuel_movement_for_range,
    fuel_rate_cards,
    fuel_sales_for_date,
    fuel_totals_by_type,
    _group_sum_by_day,
    handover_rows_for_date,
    humanize_since,
    inventory_insights,
    last_activity_at,
    latest_dip_by_tank,
    latest_reset_for,
    liters_from_dip_cm,
    max_cash_available_on,
    nearest_earlier_reading,
    next_sale_on_or_after,
    nozzle_throughput,
    payables_schedule,
    previous_reading_for,
    previous_slot,
    fuels_missing_price_on,
    price_on_date,
    receivables_aging,
    reprice_entries,
    price_resolver,
    product_margin_for_period,
    product_rate_resolver,
    product_rates_on_date,
    product_stock,
    product_stock_summary,
    record_fuel_price,
    record_product_rates,
    revenue_mix_for_date,
    sales_breakdown_for_date,
    sales_sparkline,
    split_combined_direct_sale,
    stock_purchases_by_fuel_for_period,
    stock_series,
    sync_sale_testing,
    tank_book_vs_actual,
    tank_stock_rows,
    weighted_avg_cost,
    weighted_avg_costs,
    working_capital,
    CONCENTRATION_PCT,
    DEAD_STOCK_DAYS,
    DIP_VARIANCE_MIN_LITERS,
    DIP_VARIANCE_PCT,
    HANDOVER_VARIANCE_TOLERANCE,
    LOW_DAYS_OF_STOCK,
    MONTHLY_SHORTFALL_TOLERANCE,
    THIN_MARGIN_PER_LITER,
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
# Vercel terminates TLS at its own proxy and forwards to this app over
# plain HTTP, setting X-Forwarded-Proto to say so. Without this,
# url_for(..., _external=True) - which Google OAuth's redirect_uri
# depends on - reports "http://", and Google rejects a http redirect_uri
# outright. x_proto=1 trusts exactly one hop of that header, which is
# correct here: Vercel's own edge is the only thing in front of this
# app. Harmless locally (no proxy means no X-Forwarded-Proto, so this is
# a no-op there).
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
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

# Single shared number-display rule for every template (see
# formatting.py's own docstring) - registered once here so any page using
# |fmt automatically gets comma thousands separators and a dropped ".00".
app.jinja_env.filters["fmt"] = format_number

db.init_app(app)
# Wires the tenant-scoping enforcement (see tenancy.py) into every session
# created through db.session from here on: a do_orm_execute event filters
# every read down to the current pump, and a before_flush event stamps
# pump_id onto every new row. Must run after db.init_app(app) so the
# Flask-SQLAlchemy session machinery already exists.
register_tenancy_events(db)
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

# ------------------------------------------------------------ google ----

app.config["GOOGLE_CLIENT_ID"] = os.environ.get("GOOGLE_CLIENT_ID")
app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get("GOOGLE_CLIENT_SECRET")

if app.config["GOOGLE_CLIENT_ID"] and not os.environ.get("SECRET_KEY"):
    # Authlib keeps the OAuth `state` value in the Flask session cookie,
    # which is only readable back if SECRET_KEY (used to sign it) is
    # stable across requests. get_or_create_secret_key() falls back to a
    # per-instance file when SECRET_KEY isn't set in the environment -
    # fine for plain cookie sessions, but on a serverless host that file
    # doesn't survive a cold start, so a different key gets generated
    # per instance and every Google callback's state check fails
    # intermittently (whichever instance issued the redirect vs. whichever
    # handles the callback).
    app.logger.warning(
        "GOOGLE_CLIENT_ID is set but SECRET_KEY is not - Google sign-in "
        "needs a stable SECRET_KEY set in the environment, or sign-ins "
        "will fail intermittently (especially on serverless)."
    )

# oauth.register() with a server_metadata_url does OpenID discovery lazily,
# on first actual use of oauth.google (authorize_redirect/
# authorize_access_token) - not at import/registration time - so this is
# harmless to call even when the two config values above are empty
# strings/None. Confirmed empirically: see the verification section.
oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=app.config["GOOGLE_CLIENT_ID"],
    client_secret=app.config["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def google_enabled():
    return bool(app.config["GOOGLE_CLIENT_ID"]) and bool(app.config["GOOGLE_CLIENT_SECRET"])


@app.context_processor
def inject_google_enabled():
    return {"google_enabled": google_enabled()}


@login_manager.user_loader
def load_user(user_id):
    # unscoped(): this runs before current_user is resolved for the
    # request (Flask-Login calls this to resolve it in the first place),
    # so current_pump_id() has no pump_id to filter on yet - filtering
    # here would either recurse (current_pump_id() reading current_user
    # while current_user is still being resolved) or, worse, silently
    # fail every login by scoping to the unauthenticated sentinel. This
    # is one of the two legitimate unscoped() uses (see its docstring):
    # you don't know a user's pump until you've found the user.
    with unscoped():
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
    """Cash-in-hand is a singleton PER PUMP - one register per pump, not
    one register for the whole app. Lazily creates that pump's register on
    first access rather than requiring a setup step.

    Resolves/creates explicitly against current_pump_id() rather than
    relying only on the implicit read-side tenant filter, because this
    function also WRITES (creates a row) when none exists yet - and a
    write has to know exactly which pump it's for. Returns None if there
    is no usable pump context (no request, or an unauthenticated request)
    rather than ever creating or returning a register that isn't
    unambiguously this pump's."""
    pump_id = current_pump_id()
    if not pump_id or pump_id < 0:
        return None
    cash_account = CashAccount.query.filter_by(pump_id=pump_id).first()
    if not cash_account:
        cash_account = CashAccount(opening_balance=0, pump_id=pump_id)
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
        f"Rs {format_number(max_cash_available_on(entry_date))} is available then without going negative later)."
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


# ------------------------------------------------------- auth: helpers ----

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 10  # financial app - length beats complexity rules
RESET_TOKEN_TTL_HOURS = 1
VERIFY_TOKEN_TTL_HOURS = 24
INVITE_TOKEN_TTL_HOURS = 168  # 7 days


def _password_errors(password, confirm, username, email):
    """Shared password rules for EVERY way a password can be set: signup,
    self-service reset, self-service change, the owner creating a staff
    login, and the owner resetting someone's password.

    Deliberately one bar everywhere. These used to split - 10 characters
    on the self-service paths, 6 on the two owner-driven ones - which set
    the app's real password floor at 6, since an attacker only has to
    find the weakest way in. A staff login opens the same books as the
    owner's own.

    Length over complexity rules on purpose: this is a financial app, so
    length is what actually matters, and character-class requirements
    just push people toward predictable substitutions.

    confirm=None skips the match check, for the owner-driven forms that
    only have a single password box."""
    errors = []
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if username and password.lower() == username.lower():
        errors.append("Password can't be the same as your username.")
    if email and password.lower() == email.lower():
        errors.append("Password can't be the same as your email.")
    if confirm is not None and password != confirm:
        errors.append("The two passwords don't match.")
    return errors


def _hash_token(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _issue_auth_token(user, purpose, ttl_hours):
    """Creates a new PasswordResetToken for `user` (purpose "reset" or
    "verify"), first invalidating any outstanding unused token of the
    same purpose for that user, and returns the RAW token - only its
    hash is ever stored (see PasswordResetToken's docstring in
    models.py). Caller is responsible for emailing the raw value; it is
    never retrievable again after this call returns.

    unscoped(): PasswordResetToken is TenantScoped, but this runs from
    both a fully unauthenticated context (forgot-password, where
    current_pump_id() would otherwise resolve to the unauthenticated
    sentinel and silently find zero outstanding tokens to invalidate)
    and an authenticated one (verification email right after signup/
    login, "resend verification" - both of those work fine without this,
    but sharing one function keeps the invalidate-then-issue logic in
    one place). Narrowed to just this function's query + write; pump_id
    is set explicitly from user.pump_id on the new row rather than
    relying on before_flush's auto-stamp (which refuses to guess one
    with no pump context - see tenancy.py). Every statement here is
    already scoped by this one already-resolved user's own id, so this
    can't reach or invalidate another user's tokens."""
    with unscoped():
        stale = PasswordResetToken.query.filter_by(
            user_id=user.id, purpose=purpose, used_at=None
        ).all()
        now = datetime.now()
        for t in stale:
            t.used_at = now

        raw_token = secrets.token_urlsafe(32)
        db.session.add(
            PasswordResetToken(
                user_id=user.id,
                pump_id=user.pump_id,
                purpose=purpose,
                token_hash=_hash_token(raw_token),
                expires_at=now + timedelta(hours=ttl_hours),
            )
        )
        db.session.commit()
    return raw_token


def _find_valid_token(raw_token, purpose):
    """Read-only: returns the matching PasswordResetToken row if raw_token
    exists, is unused, unexpired, and of the right purpose - else None.
    Does NOT mark it used (see the "used_at = ..." calls at each call
    site) - split out so a route can finish validating the REST of a
    submission (e.g. password rules) before spending the token; a
    rejected password on attempt 1 must not burn the user's only link.

    The final comparison uses hmac.compare_digest rather than relying on
    the filter_by(token_hash=...) equality alone, to make the actual
    accept/reject decision constant-time against the stored hash as
    required by the Stage 2 spec.

    unscoped(): reached from /reset-password/<token> and
    /verify-email/<token>, both fully unauthenticated - same reasoning as
    _issue_auth_token. The lookup key is the token's own sha256 hash: a
    32-byte cryptographically random value, unguessable and unique to
    one row, so resolving it can only ever land on that one token's own
    user - nothing here is influenced by any pump/session state, because
    there isn't any yet.

    joinedload(.user): candidate.user is a lazy relationship - without
    eager-loading it HERE, inside unscoped(), the caller's later
    `candidate.user` access would issue its OWN fresh SELECT outside
    this function, back in the caller's (unauthenticated) request
    context, which the tenant filter would then scope to the
    unauthenticated sentinel and silently resolve to None (confirmed
    empirically: this was a real 500 in this app's own verification run,
    not a hypothetical). Fetching the user in the SAME statement, while
    still unscoped, is what makes candidate.user safe to read anywhere
    after this function returns."""
    token_hash = _hash_token(raw_token)
    with unscoped():
        candidate = PasswordResetToken.query.options(joinedload(PasswordResetToken.user)).filter_by(
            token_hash=token_hash, purpose=purpose
        ).first()
    if candidate is None or not hmac.compare_digest(candidate.token_hash, token_hash):
        return None
    if candidate.used_at is not None or candidate.expires_at < datetime.now():
        return None
    return candidate


def _send_reset_email(user, reset_url):
    html = (
        f"<p>Hello {user.label},</p>"
        f"<p>Someone asked to reset the password for your Petrol Khata account "
        f"({user.email}). If this was you, click the link below - it expires in "
        f"{RESET_TOKEN_TTL_HOURS} hour(s) and can only be used once:</p>"
        f'<p><a href="{reset_url}">{reset_url}</a></p>'
        f"<p>If you didn't request this, you can safely ignore this email - "
        f"your password hasn't been changed.</p>"
    )
    email_service.send_email(user.email, "Reset your Petrol Khata password", html)


def _send_verification_email(user):
    if not user.email:
        return
    raw_token = _issue_auth_token(user, "verify", VERIFY_TOKEN_TTL_HOURS)
    verify_url = url_for("verify_email", token=raw_token, _external=True)
    html = (
        f"<p>Hello {user.label},</p>"
        f"<p>Please confirm this is your email address for your Petrol Khata "
        f"account by clicking the link below. It expires in "
        f"{VERIFY_TOKEN_TTL_HOURS} hours:</p>"
        f'<p><a href="{verify_url}">{verify_url}</a></p>'
    )
    email_service.send_email(user.email, "Verify your Petrol Khata email", html)


def _send_invite_email(user, inviter, link):
    """Mirrors _send_verification_email's style, but returns the
    send_email() result (True/False) rather than discarding it -
    settings_invite_user needs that to decide which flash message to
    show (see its own comment)."""
    if not user.email:
        return False
    pump = db.session.get(Pump, user.pump_id)
    pump_name = pump.name if pump else "Petrol Khata"
    html = (
        f"<p>Hello,</p>"
        f"<p>{inviter.label} has invited you to join <strong>{pump_name}</strong> "
        f"on Petrol Khata as a <strong>{user.role}</strong>. Click the link below "
        f"to set your own password and finish setting up your account. It expires "
        f"in {INVITE_TOKEN_TTL_HOURS // 24} days and can only be used once:</p>"
        f'<p><a href="{link}">{link}</a></p>'
    )
    return email_service.send_email(user.email, f"You've been invited to {pump_name}", html)


@app.before_request
def enforce_setup_flow():
    if request.endpoint in (
        None,
        "static",
        "login",
        "logout",
        "change_password",
        # Stage 2: every one of these must work for a visitor who either
        # isn't authenticated yet (signup, forgot/reset password - the
        # whole point) or is authenticated but whose brand-new pump has
        # no setup yet (a freshly-signed-up owner's own verification
        # link would otherwise get redirected into the setup wizard
        # before ever reaching verify_email). None of them expose
        # another pump's data - see each route's own unscoped() comments.
        "signup",
        "forgot_password",
        "reset_password",
        "verify_email",
        "resend_verification",
        # Google sign-in: google_login redirects an unauthenticated
        # visitor to Google, and google_callback is where they land back
        # -  before login_user() has run - so both have to be reachable
        # pre-authentication exactly like login/signup. google_login is
        # also the "connect my account" entry point for an ALREADY
        # authenticated owner/staff whose pump has no setup yet; letting
        # this fall through to the setup-wizard redirect below would
        # bounce them before Authlib ever got to redirect them to Google.
        "google_login",
        "google_callback",
        # An invitee's pump may have no setup done yet (they're staff, not
        # the owner who runs the wizard) - accept_invite has to be
        # reachable before/around that exactly like signup/verify above.
        "accept_invite",
    ):
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
        identifier = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # Deliberately the SAME message for "no such user", "wrong
        # password", and "deactivated" (see the two flash() call sites
        # below) - a login form that answers differently for any of
        # those is a way to discover which emails/usernames are
        # registered, which this app must not offer.
        generic_error = "Incorrect username/email or password."

        # unscoped(): the request isn't authenticated yet, so
        # current_pump_id() would scope every lookup below to the
        # unauthenticated sentinel (matching no one) - there is no pump
        # to filter by until a user has actually been resolved. This is
        # the same established pattern as Stage 1's login lookup (see
        # tenancy.py's unscoped() docstring) and Flask-Login's
        # user_loader just below.
        #
        # An "@" in the input means email - globally unique (see
        # User.email in models.py) - so that branch always resolves to
        # at most one user, unambiguously. A bare username is only
        # unique PER PUMP (User.__table_args__), so once two pumps
        # happen to share one, matching it alone is ambiguous; that's
        # refused outright below rather than guessed at.
        with unscoped():
            if "@" in identifier:
                user = User.query.filter_by(email=identifier.lower()).first()
            else:
                matches = User.query.filter(
                    func.lower(User.username) == identifier.lower(),
                    User.is_active_user.is_(True),
                ).all()
                if len(matches) > 1:
                    user = "ambiguous"
                else:
                    user = matches[0] if matches else None

        if user == "ambiguous":
            # Two or more active users share this username across
            # different pumps - do NOT log anyone in, and do not reveal
            # how many pumps matched, or anything about them (that alone
            # would confirm another business's username/existence).
            flash(
                "That username is used on more than one account. "
                "Please sign in with your email address instead.",
                "error",
            )
            return render_template("login.html")

        if user and user.check_password(password):
            if not user.is_active_user:
                flash(generic_error, "error")
                return render_template("login.html")
            login_user(user)
            return redirect(url_for("ledger"))
        flash(generic_error, "error")

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

        errors = _password_errors(new, confirm, current_user.username, current_user.email)
        if not current_user.check_password(current):
            flash("Your current password is incorrect.", "error")
        elif errors:
            for e in errors:
                flash(e, "error")
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


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Public: creates a brand-new Pump plus its owner User, in one
    atomic transaction, then logs the owner in and sends them to the
    setup wizard. See models.py/tenancy.py for why every row created
    here needs an EXPLICIT pump_id: this whole request runs before
    login_user() is called, so current_pump_id() is the unauthenticated
    sentinel and before_flush would otherwise (correctly) refuse to
    stamp any of these rows.

    No client-supplied pump id is ever read from the request - the pump
    is always a fresh row created right here, so there is no field an
    attacker could set to attach themselves to an existing pump.

    Google sign-in: if google_callback() (see below) couldn't resolve an
    existing account for a Google identity, it stashes
    session["pending_google"] = {"sub", "email", "name"} and sends the
    visitor here instead of silently creating a pump for them. When that
    key is present, this form pins the email (already Google-verified,
    so re-typing it would let it drift, and re-checking a typo'd address
    would just fail EMAIL_RE) and skips the password fields (a Google
    signup sets no password - see User.set_unusable_password())."""
    pending = session.get("pending_google")

    if current_user.is_authenticated:
        # A stale pending-Google entry must not leak into a later, normal
        # signup by some other visitor sharing this session/browser.
        session.pop("pending_google", None)
        return redirect(url_for("ledger"))

    if pending:
        form_values = {"pump_name": "", "email": pending["email"], "username": pending.get("name", "")}
    else:
        form_values = {"pump_name": "", "email": "", "username": ""}

    if request.method == "POST":
        # Re-read rather than trust the GET-time snapshot above - the
        # session is the source of truth for the life of this request.
        pending = session.get("pending_google")

        pump_name = request.form.get("pump_name", "").strip()
        email = pending["email"] if pending else request.form.get("email", "").strip().lower()
        username = request.form.get("username", "").strip()
        password = "" if pending else request.form.get("password", "")
        confirm = "" if pending else request.form.get("confirm_password", "")
        form_values = {"pump_name": pump_name, "email": email, "username": username}

        errors = []
        if not pump_name:
            errors.append("Please enter your pump/business name.")

        if not email:
            errors.append("Please enter an email address.")
        elif not pending and not EMAIL_RE.match(email):
            errors.append("That doesn't look like a valid email address.")
        else:
            # unscoped(): nobody is authenticated yet (this IS signup) -
            # there is no pump to scope this to, and email uniqueness is
            # GLOBAL across every pump by design (see User.email in
            # models.py), so this has to search every pump regardless.
            # Only used to decide whether to reject the signup with a
            # validation message - never returns another pump's row (or
            # any of its fields) to the caller. Re-checked here even for
            # the pending-Google case (already checked once back in
            # google_callback()) to close the race between that callback
            # and this submit - two tabs, or two separate Google
            # round-trips, completing for the same email in between.
            with unscoped():
                email_taken = User.query.filter_by(email=email).first() is not None
            if email_taken:
                errors.append("An account with that email already exists. Try logging in instead.")

        if not username:
            errors.append("Please enter a username.")
        elif len(username) > 80:
            errors.append("Username is too long (80 characters max).")

        if not pending:
            errors.extend(_password_errors(password, confirm, username, email))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("signup.html", pending_google=bool(pending), **form_values)

        pump = Pump(name=pump_name)
        db.session.add(pump)
        # Pump isn't TenantScoped (see its docstring) so this flush isn't
        # about tenancy at all - it just assigns pump.id so the rows
        # below can be stamped with it. Still inside the same DB
        # transaction as the commit() further down, so a failure there
        # rolls this insert back too - see that commit's comment.
        db.session.flush()

        owner = User(
            username=username,
            email=email,
            role="owner",
            is_active_user=True,
            pump_id=pump.id,
        )
        if pending:
            # Google verified this email and the visitor proved control
            # of it by completing the OAuth round-trip - no separate
            # verification email needed, and no password to set.
            owner.set_unusable_password()
            owner.google_sub = pending["sub"]
            owner.email_verified_at = datetime.now()
        else:
            owner.set_password(password)
        db.session.add(owner)
        # Every pump needs its default shift (default_shift() /
        # book_stock() / every reading-credit-bank-sale route assumes
        # one exists) and its cash-in-hand register - see
        # ensure_seed_users() in this file for the exact same pattern
        # used to seed the first, bootstrap pump.
        db.session.add(Shift(name="Full Day", sort_order=0, pump_id=pump.id))
        db.session.add(CashAccount(opening_balance=0, pump_id=pump.id))

        try:
            # One commit for all four rows (pump already flushed into
            # the same open transaction above) - all-or-nothing. A
            # duplicate email slipping in between the check above and
            # here (a race between two concurrent signups) is the
            # realistic way this fails; the rollback below undoes the
            # pump/shift/cash-account too, leaving no orphan behind.
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(
                "Something went wrong creating your account (that email or "
                "username may already be taken). Please try again.",
                "error",
            )
            return render_template("signup.html", pending_google=bool(pending), **form_values)

        login_user(owner)
        session.pop("pending_google", None)

        # Verification email - best-effort, must never block signup
        # (Stage 2 spec: email verification is non-blocking). Runs AFTER
        # commit()+login_user() so current_user is now authenticated and
        # the ordinary before_flush auto-stamp in tenancy.py handles
        # this new token row's pump_id on its own - no unscoped()/
        # explicit pump_id needed for this particular call. Skipped
        # entirely for a Google signup: email_verified_at is already set
        # above, Google having verified it is why we're here at all.
        if not pending:
            try:
                _send_verification_email(owner)
            except Exception:
                app.logger.exception(
                    "signup: failed to send verification email for user %s", owner.id
                )

        flash(f'Welcome! "{pump.name}" is set up - let\'s add your tanks.', "success")
        return redirect(url_for("setup_tanks"))

    return render_template("signup.html", pending_google=bool(pending), **form_values)


@app.route("/auth/google")
def google_login():
    """Kicks off the Google OAuth round-trip. Reused for two different
    purposes distinguished only by whether someone is already logged in:
    a fresh sign-in/sign-up for an anonymous visitor, or "connect my
    Google account" for an already-authenticated owner/staff member
    visiting from Settings - see the google_link_mode flag below and its
    handling in google_callback()."""
    if not google_enabled():
        flash("Google sign-in isn't configured.", "error")
        return redirect(url_for("login"))

    if current_user.is_authenticated:
        session["google_link_mode"] = True

    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def google_callback():
    """Where Google sends the visitor back after /auth/google. Every
    exit from here is a flash()+redirect, never a raw exception surfaced
    to the visitor - a mismatched `state` (CSRF check), the visitor
    pressing Cancel on Google's consent screen, or a Google-side outage
    are all routine, not server errors."""
    if not google_enabled():
        flash("Google sign-in isn't configured.", "error")
        return redirect(url_for("login"))

    try:
        # authorize_access_token() verifies the `state` param against the
        # one Authlib stashed in the session cookie at /auth/google, and
        # (for an OpenID flow) the id_token's signature/nonce/audience.
        token = oauth.google.authorize_access_token()
        info = token.get("userinfo") or oauth.google.parse_id_token(token)
    except Exception:
        app.logger.exception("google_callback: OAuth exchange failed")
        flash("Google sign-in didn't complete. Please try again.", "error")
        return redirect(url_for("login"))

    sub = info.get("sub")
    email = (info.get("email") or "").strip().lower()
    email_verified = info.get("email_verified")
    name = info.get("name") or ""

    if not sub or not email:
        flash("Google didn't return the information we need to sign you in.", "error")
        return redirect(url_for("login"))

    # Hard rule, deliberately not softened: an unverified email could be
    # set on a Google account by anyone, not just its true owner, so
    # trusting it here would let an attacker sign in as / link to
    # whichever of our users happens to share that address.
    if email_verified is not True:
        flash(
            "Google hasn't verified that email address, so we can't sign "
            "you in with it.",
            "error",
        )
        return redirect(url_for("login"))

    # Deliberately the SAME message login() uses for "no such user",
    # "wrong password", and "deactivated" - see its comment. A disabled
    # account must read identically here, not reveal that it exists.
    generic_error = "Incorrect username/email or password."
    link_mode = session.pop("google_link_mode", None)

    if link_mode and current_user.is_authenticated:
        # unscoped(): current_user is already resolved and trusted (the
        # signed session cookie), but the LOOKUP below has to see every
        # pump's users to know whether this google_sub is already taken
        # by someone else - see tenancy.py's unscoped() docstring.
        with unscoped():
            other = User.query.filter_by(google_sub=sub).first()
        if other is not None and other.id != current_user.id:
            flash(
                "That Google account is already linked to a different login.",
                "error",
            )
            return redirect(url_for("settings"))

        current_user.google_sub = sub
        # Only mark OUR email verified if Google's email actually matches
        # it - do NOT overwrite current_user.email with Google's, which
        # could silently change who future logins/notifications reach.
        if email == current_user.email:
            current_user.email_verified_at = datetime.now()
        db.session.commit()
        flash("Google account connected.", "success")
        return redirect(url_for("settings"))

    # Sign-in ladder (not link mode) - see tenancy.py's unscoped()
    # docstring for why every lookup here has to run unscoped: the
    # request isn't authenticated yet, so current_pump_id() would filter
    # every query down to the unauthenticated sentinel, matching nobody.
    with unscoped():
        user = User.query.filter_by(google_sub=sub).first()
        linking = False
        if user is None:
            user = User.query.filter_by(email=email).first()
            linking = user is not None

    if user is not None:
        if not user.is_active_user:
            flash(generic_error, "error")
            return redirect(url_for("login"))
        if linking:
            user.google_sub = sub
            if user.email_verified_at is None:
                user.email_verified_at = datetime.now()
            db.session.commit()
        login_user(user)
        return redirect(url_for("ledger"))

    # No account at all - do NOT silently create a pump for a stranger
    # who merely owns a Google account. Send them through the normal
    # signup form instead, pre-filled (see signup()'s pending_google
    # handling above) so they still choose their pump name and username.
    session["pending_google"] = {"sub": sub, "email": email, "name": name}
    flash("Finish creating your pump to sign in with Google.", "info")
    return redirect(url_for("signup"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Public. Always shows the same response whether or not the address
    is registered - see the flash() call at the end, reached from every
    path through this POST handler."""
    if current_user.is_authenticated:
        return redirect(url_for("ledger"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email:
            flash("Please enter your email address.", "error")
            return render_template("forgot_password.html")

        # unscoped(): unauthenticated by definition - there is no pump to
        # scope this to, and email is globally unique anyway (see
        # User.email in models.py). Only used to decide whether to issue
        # a token; existence/non-existence is never revealed in the
        # response (see the identical flash() below).
        with unscoped():
            user = User.query.filter_by(email=email, is_active_user=True).first()

        if user:
            try:
                raw_token = _issue_auth_token(user, "reset", RESET_TOKEN_TTL_HOURS)
                reset_url = url_for("reset_password", token=raw_token, _external=True)
                _send_reset_email(user, reset_url)
            except Exception:
                # A broken/slow email provider must never surface as an
                # error here - the response is identical either way.
                app.logger.exception("forgot-password: failed to issue/send reset token")

        flash(
            "If that email address has an account, we've sent a link to reset "
            f"the password. It expires in {RESET_TOKEN_TTL_HOURS} hour.",
            "info",
        )
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Public. `token` is the raw, unhashed value from the emailed link -
    see _find_valid_token()/PasswordResetToken in models.py for how it's
    verified against the stored hash."""
    if current_user.is_authenticated:
        return redirect(url_for("ledger"))

    candidate = _find_valid_token(token, "reset")
    if not candidate:
        flash("That reset link is invalid or has expired. Request a new one below.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        user = candidate.user
        errors = _password_errors(password, confirm, user.username, user.email)

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("reset_password.html", token=token)

        # Re-validate right before writing (defends against the token
        # expiring, or being used from a second tab, between the GET
        # above and this POST) - only marked used once we know the new
        # password itself is acceptable, so a bad password on the first
        # try doesn't cost the user their only link.
        candidate = _find_valid_token(token, "reset")
        if not candidate:
            flash("That reset link is invalid or has expired. Request a new one below.", "error")
            return redirect(url_for("forgot_password"))

        user = candidate.user
        user.set_password(password)
        candidate.used_at = datetime.now()
        db.session.commit()
        flash("Password reset. You can log in with your new password now.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


@app.route("/verify-email/<token>")
def verify_email(token):
    """Public (reachable whether or not the clicker happens to be logged
    in - the link is mailed out and may be opened in any browser/tab).
    Never blocks anything; see the banner in base.html for the only
    place verification status is surfaced."""
    candidate = _find_valid_token(token, "verify")
    if not candidate:
        flash("That verification link is invalid or has expired.", "error")
        return redirect(url_for("ledger") if current_user.is_authenticated else url_for("login"))

    user = candidate.user
    user.email_verified_at = datetime.now()
    candidate.used_at = datetime.now()
    db.session.commit()
    flash("Email verified - thanks!", "success")
    return redirect(url_for("ledger") if current_user.is_authenticated else url_for("login"))


@app.route("/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    """Self-service, mirrors change_password()'s spirit: the logged-in
    user can always ask for a fresh link for their OWN account. Never
    resends/verifies anyone else's."""
    if current_user.email_verified_at:
        flash("Your email is already verified.", "info")
    elif not current_user.email:
        flash("Your account has no email on file - ask the owner to add one in Settings.", "error")
    else:
        try:
            _send_verification_email(current_user)
            flash(
                "Verification email sent - please check your inbox.",
                "success",
            )
        except Exception:
            app.logger.exception("resend-verification: failed to send for user %s", current_user.id)
            flash("Couldn't send the verification email right now - please try again shortly.", "error")

    return redirect(request.referrer or url_for("ledger"))


@app.route("/accept-invite/<token>", methods=["GET", "POST"])
def accept_invite(token):
    """Public. Modeled line-for-line on reset_password() above - same
    _find_valid_token usage, same re-validate-right-before-writing
    defence against a token spent in a parallel tab. `token` is the raw,
    unhashed value from the emailed/copied link."""
    if current_user.is_authenticated:
        return redirect(url_for("ledger"))

    candidate = _find_valid_token(token, "invite")
    if not candidate:
        flash("This invitation link is no longer valid. Ask the owner to send a new one.", "error")
        return redirect(url_for("login"))

    user = candidate.user
    # unscoped(): this request is unauthenticated (there is no
    # current_pump_id() to resolve yet), but Pump isn't TenantScoped in
    # the first place - see models.py - so this plain get() needs no
    # unscoped() wrapper at all; it's here purely to fetch the invited
    # user's OWN pump for display, never anyone else's.
    pump = db.session.get(Pump, user.pump_id)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors = []
        if not username:
            errors.append("Please enter a username.")
        elif len(username) > 80:
            errors.append("Username is too long (80 characters max).")
        else:
            # Scoped explicitly to the invited user's OWN pump
            # (candidate.user.pump_id), not current_pump_id() - there is
            # no authenticated session yet, so that would resolve to the
            # unauthenticated sentinel and match nothing, silently
            # letting any username through. Username collisions are only
            # meaningful within one pump (see User.__table_args__).
            with unscoped():
                taken = User.query.filter(
                    User.pump_id == user.pump_id,
                    User.id != user.id,
                    func.lower(User.username) == username.lower(),
                ).first() is not None
            if taken:
                errors.append(f'A user named "{username}" already exists on this pump.')

        errors.extend(_password_errors(password, confirm, username, user.email))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("accept_invite.html", token=token, user=user, pump=pump, username=username)

        # Re-validate right before writing (same reasoning as
        # reset_password): defends against the token expiring, or being
        # accepted from a second tab, between the GET above and this
        # POST.
        candidate = _find_valid_token(token, "invite")
        if not candidate:
            flash("This invitation link is no longer valid. Ask the owner to send a new one.", "error")
            return redirect(url_for("login"))

        user = candidate.user
        with unscoped():
            taken = User.query.filter(
                User.pump_id == user.pump_id,
                User.id != user.id,
                func.lower(User.username) == username.lower(),
            ).first() is not None
        if taken:
            flash(f'A user named "{username}" already exists on this pump.', "error")
            return render_template("accept_invite.html", token=token, user=user, pump=pump, username=username)

        user.username = username
        user.set_password(password)
        user.is_active_user = True
        # Receiving the link proves control of the mailbox - the same
        # reasoning verify_email() already uses.
        user.email_verified_at = datetime.now()
        candidate.used_at = datetime.now()
        db.session.commit()
        login_user(user)
        flash(f"Welcome to {pump.name if pump else 'Petrol Khata'}!", "success")
        return redirect(url_for("ledger"))

    return render_template("accept_invite.html", token=token, user=user, pump=pump, username=user.username)


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
    accounts = Account.query.order_by(Account.name).all()
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


def _resolve_entry_mode(form):
    """liters-vs-amount toggle shared by ledger_credit() and
    account_entry_credit_edit() - see ledger_credit()'s docstring for what
    each mode means."""
    entry_mode = form.get("entry_mode", "liters")
    if entry_mode not in ("liters", "amount"):
        entry_mode = "liters"
    return entry_mode


def _credit_amount_error(fuel, entry_date, entry_mode, liters_in, amount_in):
    """The liters/amount cross-validation shared by ledger_credit() and
    account_entry_credit_edit() (see ledger_credit()'s docstring for what
    each entry_mode means). Assumes `fuel` is already known non-None.
    Returns the flash error text, or None if the combination is valid."""
    if entry_mode == "liters" and (not liters_in or liters_in <= 0):
        return "Liters must be a positive number."
    if entry_mode == "amount" and (not amount_in or amount_in <= 0):
        return "Amount must be a positive number."
    if entry_mode == "liters" and amount_in is not None and amount_in <= 0:
        return "Amount must be a positive number."
    if entry_mode == "amount" and liters_in is not None and liters_in <= 0:
        return "Liters must be a positive number."
    if entry_mode == "amount" and price_on_date(fuel, entry_date) <= 0:
        return "This fuel has no price set yet - please set a price before recording credit by amount."
    return None


def _derive_credit_liters_amount(entry_mode, liters_in, amount_in, price):
    """liters/amount derivation shared by ledger_credit() and
    account_entry_credit_edit() - see ledger_credit()'s docstring for the
    discount mechanics this implements."""
    if entry_mode == "amount":
        amount = amount_in
        liters = round(liters_in, 2) if liters_in is not None else round(amount_in / price, 2)
    else:
        liters = liters_in
        amount = round(amount_in, 2) if amount_in is not None else round(liters * price, 2)
    return liters, amount


def _amount_cash_overdraw_error(amount, entry_date):
    """Shared "positive amount, then cash-overdraw" guard for simple new
    cash/bank entries (bank sale, cash deposit): returns the flash error
    text, or None if the amount is valid."""
    if not amount or amount <= 0:
        return "Amount must be a positive number."
    if would_overdraw_cash(amount, entry_date):
        return cash_shortfall_message(entry_date)
    return None


def _validate_amount_cash_edit(entry, entry_date, amount):
    """Shared amount/cash-overdraw validation for the plain "amount only"
    edit handlers (bank sale, cash deposit) whose bodies were otherwise
    identical. Returns the flash error text, or None if the edit is
    valid."""
    if not amount or amount <= 0:
        return "Amount must be a positive number."
    if would_overdraw_cash(amount, entry_date, entry.amount, entry.entry_date):
        return cash_shortfall_message(entry_date)
    return None


def _validate_cash_payment_edit(entry, entry_date, amount, form):
    """Shared amount/payment-method/cash-overdraw validation for the
    simple "amount + paid_via" edit handlers (supplier payment, employee
    loan) whose bodies were otherwise identical. Returns
    (method, bank_account, error) where error is the flash text to show
    on failure, or None if the edit is valid."""
    method, bank_account, method_error = resolve_payment_method(form)
    old_cash_amount = entry.amount if entry.method == "cash" else 0
    new_cash_amount = amount if (amount and method == "cash") else 0

    if not amount or amount <= 0:
        error = "Amount must be a positive number."
    elif method_error:
        error = method_error
    elif would_overdraw_cash(new_cash_amount, entry_date, old_cash_amount, entry.entry_date):
        error = cash_shortfall_message(entry_date)
    else:
        error = None
    return method, bank_account, error


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


# ------------------------------------------------------------ dashboard ---

_ATTENTION_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_NARRATIVE_SEVERITY_ORDER = {"critical": 0, "warning": 1, "good": 2, "info": 3}


def attention_items(
    entry_date,
    *,
    tank_rows,
    dip_variance,
    completeness,
    fuel_rate_cards,
    aging,
    concentration,
    handover_rows,
    dead_stock_rows,
    nozzle_rows,
    bad_cash_date,
):
    """Everything about entry_date that an owner should look at, as a
    ranked list (critical -> warning -> info) - Block F, "Needs attention".

    Takes already-computed inputs rather than recomputing them: every one
    of tank_rows/dip_variance/completeness/fuel_rate_cards/aging/
    concentration/handover_rows/dead_stock_rows/nozzle_rows is a card the
    Dashboard route has already built for its own section further down the
    page - this only reads them a second time. bad_cash_date
    (first_negative_cash_date()) is the one genuinely new call the route
    makes for this block, and it's a single bounded pass over the cash
    ledger, not an account walk - it doesn't reopen the Part 0 problem.

    Every threshold below is a named constant imported from
    ledger_logic.py (DIP_VARIANCE_PCT, DIP_VARIANCE_MIN_LITERS,
    LOW_DAYS_OF_STOCK, CONCENTRATION_PCT, HANDOVER_VARIANCE_TOLERANCE,
    DEAD_STOCK_DAYS) - that module owns them (dead_stock() and
    customer_concentration() read the same two constants when building
    dead_stock_rows/concentration in the first place) so there is exactly
    ONE place to review and retune every judgment call this block makes.

    Returns a list of {"severity": "critical"|"warning"|"info", "title":
    str, "detail": str, "url": str|None} dicts, sorted critical -> warning
    -> info (stable, so same-severity rules keep the order they were
    appended in below - roughly "how bad" within a severity)."""
    items = []

    # ---------------------------------------------------------- critical --
    for row in tank_rows:
        if row["negative"]:
            items.append(
                {
                    "severity": "critical",
                    "title": f"{row['tank'].label}: negative stock",
                    "detail": (
                        f"Book stock is {format_number(row['stock'])} L - a data-entry error "
                        "(a missed purchase or an over-recorded sale), not a real dip below empty."
                    ),
                    "url": url_for("inventory"),
                }
            )
        if row["over_capacity"]:
            items.append(
                {
                    "severity": "critical",
                    "title": f"{row['tank'].label}: stock above capacity",
                    "detail": f"Book stock {format_number(row['stock'])} L exceeds the tank's {format_number(row['capacity'])} L capacity.",
                    "url": url_for("inventory"),
                }
            )

    if bad_cash_date:
        items.append(
            {
                "severity": "critical",
                "title": "Cash in hand has gone negative",
                "detail": (
                    f"Cash-in-hand first goes negative on {bad_cash_date.strftime('%d %b %Y')} "
                    "- review entries on or after that date."
                ),
                "url": url_for("cash_account_detail"),
            }
        )

    for card in fuel_rate_cards:
        if card["cost_per_liter"] is not None and card["rate"] < card["cost_per_liter"]:
            items.append(
                {
                    "severity": "critical",
                    "title": f"{card['fuel_type'].name}: selling below cost",
                    "detail": (
                        f"Rate Rs {format_number(card['rate'])}/L is below the weighted-average "
                        f"cost Rs {format_number(card['cost_per_liter'])}/L."
                    ),
                    "url": url_for("dashboard", date=entry_date.isoformat()),
                }
            )

    # ----------------------------------------------------------- warning --
    for row in dip_variance["rows"]:
        book_stock_liters = row["dip"].dip_liters - row["variance_liters"]
        threshold = max(DIP_VARIANCE_MIN_LITERS, DIP_VARIANCE_PCT * book_stock_liters)
        if abs(row["variance_liters"]) > threshold:
            items.append(
                {
                    "severity": "warning",
                    "title": f"{row['tank'].label}: dip variance over threshold",
                    "detail": (
                        f"Variance {format_number(row['variance_liters'])} L exceeds the "
                        f"{threshold:.1f} L threshold for this tank's book stock."
                    ),
                    "url": url_for("reports", date=entry_date.isoformat()),
                }
            )

    if aging["buckets"]["90+"] > 0:
        items.append(
            {
                "severity": "warning",
                "title": "Debt aged 90+ days",
                "detail": f"Rs {format_number(aging['buckets']['90+'])} has been outstanding for more than 90 days.",
                "url": url_for("accounts", kind="debitors"),
            }
        )

    if concentration["is_concentrated"]:
        items.append(
            {
                "severity": "warning",
                "title": "Customer concentration risk",
                "detail": (
                    f"Top 3 customers hold {concentration['top3_share_pct']:.1f}% of "
                    f"receivables (Rs {format_number(concentration['total_receivable'])} total)."
                ),
                "url": url_for("accounts", kind="debitors"),
            }
        )

    for row in tank_rows:
        if row["days_of_stock"] is not None and row["days_of_stock"] < LOW_DAYS_OF_STOCK:
            items.append(
                {
                    "severity": "warning",
                    "title": f"{row['tank'].label}: low / near dry",
                    "detail": f"Only {row['days_of_stock']:.1f} days of cover left at the current consumption rate.",
                    "url": url_for("inventory"),
                }
            )

    for c in completeness:
        if c["kind"] == "unread_nozzles":
            items.append(
                {
                    "severity": "warning",
                    "title": "Unread nozzles",
                    "detail": c["message"],
                    "url": url_for("ledger", date=entry_date.isoformat()),
                }
            )
        elif c["kind"] == "no_handover":
            items.append(
                {
                    "severity": "warning",
                    "title": "Shift not handed over",
                    "detail": c["message"],
                    "url": url_for("ledger", date=entry_date.isoformat()),
                }
            )

    for row in handover_rows:
        if row["variance"] is not None and abs(row["variance"]) > HANDOVER_VARIANCE_TOLERANCE:
            items.append(
                {
                    "severity": "warning",
                    "title": f"{row['shift'].name}: handover variance",
                    "detail": f"Rs {format_number(row['variance'])} variance against the expected cash for this shift.",
                    "url": url_for("reports", date=entry_date.isoformat()),
                }
            )

    # -------------------------------------------------------------- info --
    dead_total = round(sum(r["value"] for r in dead_stock_rows), 2)
    if dead_total > 0:
        items.append(
            {
                "severity": "info",
                "title": "Dead stock tying up cash",
                "detail": (
                    f"Rs {format_number(dead_total)} tied up in {len(dead_stock_rows)} product(s) "
                    f"with no sale in {DEAD_STOCK_DAYS}+ days."
                ),
                "url": url_for("inventory"),
            }
        )

    for row in nozzle_rows:
        if row["underperforming"]:
            items.append(
                {
                    "severity": "info",
                    "title": f"{row['nozzle'].label}: underperforming",
                    "detail": (
                        f"{row['share'] * 100:.1f}% of its dispenser's throughput over the "
                        f"last {row['days']} days - worth checking the meter or the attendant."
                    ),
                    "url": url_for("inventory"),
                }
            )

    items.sort(key=lambda item: _ATTENTION_SEVERITY_ORDER[item["severity"]])
    return items


def monthly_narrative(start, end, ctx, prior_ctx, prior_month_label, best_day):
    """Rule-based, deterministic observations about one month - the
    Monthly Report's counterpart to attention_items(), same return shape
    (severity/title/detail/url) so it reuses the Dashboard's
    .attention-list markup verbatim. Every number quoted comes from
    ctx/prior_ctx/best_day, which the caller has already computed; this
    function issues ZERO queries of its own. Adds one severity value
    attention_items() doesn't have - "good" - because unlike "Needs
    Attention" this list is also meant to say what went right. Thresholds
    come from the named constants in ledger_logic.py."""
    items = []
    detail = ctx["cogs_detail"]

    def mpl(r):
        return r["margin"] / r["liters"] if r["liters"] else 0.0

    # R1 - loss-making month (critical).
    if ctx["net_profit"] < 0:
        items.append(
            {
                "severity": "critical",
                "title": "The month closed at a loss",
                "detail": (
                    f"Net profit for {start.strftime('%B %Y')} is Rs {format_number(ctx['net_profit'])}. "
                    f"Total gross margin Rs {format_number(ctx['total_gross_margin'])}, tanker deal margin "
                    f"Rs {format_number(ctx['tanker_margin'])} and other income "
                    f"Rs {format_number(ctx['other_income_total'])} did not cover Rs {format_number(ctx['expenses_total'])} "
                    f"of expenses and Rs {format_number(ctx['salaries_total'])} of salaries."
                ),
                "url": None,
            }
        )

    # R2 - a fuel sold below cost (critical).
    for r in detail:
        if r["liters"] > 0 and mpl(r) < 0:
            items.append(
                {
                    "severity": "critical",
                    "title": f"{r['fuel']}: sold below cost this month",
                    "detail": (
                        f"Rs {format_number(mpl(r))} margin per litre across {format_number(r['liters'])} L - net revenue "
                        f"Rs {format_number(r['revenue'])} against Rs {format_number(r['cost'])} of cost at the "
                        "weighted-average purchase price."
                    ),
                    "url": None,
                }
            )

    # R3 - thin margin (warning).
    for r in detail:
        if r["liters"] > 0 and 0 <= mpl(r) < THIN_MARGIN_PER_LITER:
            items.append(
                {
                    "severity": "warning",
                    "title": f"{r['fuel']}: thin margin",
                    "detail": (
                        f"Only Rs {format_number(mpl(r))} margin per litre this month, under the "
                        f"Rs {format_number(THIN_MARGIN_PER_LITER)}/L mark - Rs {format_number(r['margin'])} earned on "
                        f"{format_number(r['liters'])} L."
                    ),
                    "url": None,
                }
            )

    # R4 - profit against the prior full month (good/warning/info).
    if prior_ctx["net_profit"] != 0:
        delta = round(ctx["net_profit"] - prior_ctx["net_profit"], 2)
        pct = round(delta / abs(prior_ctx["net_profit"]) * 100, 1)
        if delta >= 0:
            items.append(
                {
                    "severity": "good",
                    "title": "Profit is up on last month",
                    "detail": (
                        f"Rs {format_number(ctx['net_profit'])} this month against Rs {format_number(prior_ctx['net_profit'])} "
                        f"in {prior_month_label} - up Rs {format_number(delta)} ({pct:+.1f}%)."
                    ),
                    "url": None,
                }
            )
        else:
            items.append(
                {
                    "severity": "warning",
                    "title": "Profit is down on last month",
                    "detail": (
                        f"Rs {format_number(ctx['net_profit'])} this month against Rs {format_number(prior_ctx['net_profit'])} "
                        f"in {prior_month_label} - down Rs {format_number(abs(delta))} ({pct:+.1f}%)."
                    ),
                    "url": None,
                }
            )
    else:
        items.append(
            {
                "severity": "info",
                "title": "No comparable prior month",
                "detail": f"{prior_month_label} recorded no net profit figure to compare against.",
                "url": None,
            }
        )

    # R5 - best fuel by revenue (good).
    total_net_fuel_revenue = sum(r["revenue"] for r in detail)
    if detail and total_net_fuel_revenue > 0:
        top = max(detail, key=lambda r: r["revenue"])
        share = top["revenue"] / total_net_fuel_revenue * 100
        items.append(
            {
                "severity": "good",
                "title": f"{top['fuel']} led the month",
                "detail": (
                    f"{format_number(top['liters'])} L sold for Rs {format_number(top['revenue'])} - {share:.1f}% of net "
                    f"fuel revenue, at Rs {format_number(mpl(top))} margin per litre."
                ),
                "url": None,
            }
        )

    # R6 - best single sales day (good).
    if best_day is not None:
        items.append(
            {
                "severity": "good",
                "title": f"Best day: {best_day['date'].strftime('%d %b %Y')}",
                "detail": f"Rs {format_number(best_day['amount'])} of fuel sold that day, the highest of the month.",
                "url": url_for("reports", date=best_day["date"].isoformat()),
            }
        )

    # R7 - attendant cash shortfall (warning). Only the single worst
    # attendant is reported - the full table is right below on the page.
    if ctx["attendant_variances"]:
        w = ctx["attendant_variances"][0]
        if w["total_variance"] < -MONTHLY_SHORTFALL_TOLERANCE:
            items.append(
                {
                    "severity": "warning",
                    "title": f"{w['name']}: cash short this month",
                    "detail": (
                        f"Rs {format_number(abs(w['total_variance']))} net short across {w['shifts']} reconciled "
                        f"shift(s), {w['shortfalls']} of them short."
                    ),
                    "url": url_for("account_detail", account_id=w["account"].id) if w["account"] else None,
                }
            )

    # R8 - stock cover vs sales (info).
    if ctx["purchases_liters"] > 0 or ctx["liters_sold"] > 0:
        if ctx["purchases_liters"] < ctx["liters_sold"]:
            items.append(
                {
                    "severity": "info",
                    "title": "Sold more than was received",
                    "detail": (
                        f"{format_number(ctx['liters_sold'])} L sold against {format_number(ctx['purchases_liters'])} L "
                        "received - the difference came out of tank stock."
                    ),
                    "url": url_for("inventory"),
                }
            )
        else:
            items.append(
                {
                    "severity": "info",
                    "title": "Received more than was sold",
                    "detail": (
                        f"{format_number(ctx['purchases_liters'])} L received against {format_number(ctx['liters_sold'])} L "
                        "sold - the difference is still in the tanks."
                    ),
                    "url": url_for("inventory"),
                }
            )

    items.sort(key=lambda i: _NARRATIVE_SEVERITY_ORDER[i["severity"]])
    return items


def profit_walkthrough(ctx):
    """The income-statement figures already in ctx, re-expressed as an
    ordered walk with a running total, so the page can show HOW net
    profit was reached instead of a flat row of cards. Adds no arithmetic
    of its own beyond the running total - every `amount` is a ctx value
    read verbatim, and the walk's final `running` MUST equal
    ctx["net_profit"] to 2dp; if it ever doesn't,
    _reports_monthly_context()'s own sales-returns double-count invariant
    (see its docstring) has been broken. Deliberately assertion-free (a
    report must render, not 500) - the equality above is restated here as
    the thing to check, not enforced with an assert."""
    r = ctx
    rows = []
    running = 0.0

    def start_row(label, amount, note):
        nonlocal running
        running = amount
        rows.append({"kind": "start", "label": label, "amount": amount, "running": running, "note": note})

    def memo_row(label, amount, note):
        rows.append({"kind": "memo", "label": label, "amount": amount, "running": None, "note": note})

    def less_row(label, amount, note):
        nonlocal running
        running = round(running - amount, 2)
        rows.append({"kind": "less", "label": label, "amount": amount, "running": running, "note": note})

    def add_row(label, amount, note):
        nonlocal running
        running = round(running + amount, 2)
        rows.append({"kind": "add", "label": label, "amount": amount, "running": running, "note": note})

    def subtotal_row(label, amount, note):
        rows.append({"kind": "subtotal", "label": label, "amount": amount, "running": running, "note": note})

    def total_row(label, amount, note):
        rows.append({"kind": "total", "label": label, "amount": amount, "running": amount, "note": note})

    start_row(
        "Fuel Revenue, Gross", r["revenue"],
        f"{format_number(r['liters_sold'])} L sold · {format_number(r['testing_liters'])} L testing",
    )
    memo_row(
        "Discounts already netted out above", r["total_discounts"],
        "Credit sales billed below list price - subtracted from Fuel Revenue, Gross, not again below.",
    )
    less_row(
        "Sales Returns", r["sales_returns_amount"],
        f"{format_number(r['sales_returns_liters'])} L returned, any refund method",
    )
    subtotal_row("Net Fuel Revenue", r["net_revenue"], "Gross revenue less sales returns")
    less_row("Cost of Fuel Sold", r["cogs"], "Net litres sold × weighted-average purchase cost")
    subtotal_row(
        "Fuel Gross Margin", r["gross_margin"],
        f"{(r['gross_margin'] / r['net_revenue'] * 100) if r['net_revenue'] else 0:.1f}% of net fuel revenue",
    )
    memo_row("Product Revenue", r["product_revenue"], "")
    memo_row(
        "Less: Cost of Products Sold", r["product_cost"],
        "Exact per line - the rate snapshotted at the moment of that sale, not a weighted average",
    )
    add_row(
        "Product Gross Margin", r["product_commission"],
        f"{(r['product_commission'] / r['product_revenue'] * 100) if r['product_revenue'] else 0:.1f}% of product revenue",
    )
    subtotal_row("Total Gross Margin", r["total_gross_margin"], "Fuel + product gross margin")
    add_row(
        "Tanker Deal Margin", r["tanker_margin"],
        f"Pass-through deals - Rs {format_number(r['tanker_revenue'])} sold against "
        f"Rs {format_number(r['tanker_cost'])} cost on {format_number(r['tanker_liters'])} L. "
        "No tank stock involved: this fuel went straight from the supplier to the "
        "customer, so it is not in Fuel Revenue or Cost of Fuel Sold above.",
    )
    add_row(
        "Other Income", r["other_income_total"],
        "Rent, side-business share and similar - no associated cost",
    )
    less_row("Expenses", r["expenses_total"], "")
    less_row("Salaries", r["salaries_total"], "Full salary earned, before deductions")
    total_row(
        "Net Profit", r["net_profit"],
        "Total gross margin + tanker deal margin + other income − expenses − salaries",
    )
    return rows


@app.route("/dashboard")
@login_required
def dashboard():
    today = date.today()
    selected_date = parse_date_param(request.args.get("date"))

    sales_total = (
        db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
        .filter(Sale.entry_date == selected_date)
        .scalar()
    )
    sales_total += (
        db.session.query(func.coalesce(func.sum(DirectSale.total_amount), 0))
        .filter(DirectSale.entry_date == selected_date)
        .scalar()
    )
    total_liters = (
        db.session.query(func.coalesce(func.sum(Sale.liters), 0))
        .filter(Sale.entry_date == selected_date)
        .scalar()
    )
    total_liters += (
        db.session.query(func.coalesce(func.sum(DirectSale.liters), 0))
        .filter(DirectSale.entry_date == selected_date)
        .scalar()
    )
    sale_count = Sale.query.filter_by(entry_date=selected_date).count() + DirectSale.query.filter_by(
        entry_date=selected_date
    ).count()

    # Resolved ONCE and threaded through every helper below that needs a
    # cost, rather than each one calling weighted_avg_cost() per tank/fuel
    # in its own loop - see weighted_avg_costs()'s docstring.
    costs = weighted_avg_costs(selected_date)

    # Block C (tanks) is attendant-visible too, same as Block A's Sales
    # card and Block B's rate cards - they already see stock day to day,
    # so gating this by role would just be noise for no privacy benefit.
    tank_rows = tank_stock_rows(selected_date, costs=costs)
    low_stock = [r for r in tank_rows if r["is_low"]]
    stock_value_total = round(sum(r["value"] for r in tank_rows), 2)
    stock_liters_total = round(sum(r["stock"] for r in tank_rows), 2)

    # Next-day paging is capped at today, matching the Ledger/Reports date
    # nav - None here means "hide the next-day control", not "no page".
    next_date = selected_date + timedelta(days=1) if selected_date < today else None

    last_activity = last_activity_at()

    # Computed once here (rather than inline in the context= dict below)
    # because attention_items() (owner-only, further down) needs the same
    # rows for its "selling below cost" rule and must not call this a
    # second time.
    fuel_rate_cards_rows = fuel_rate_cards(selected_date, costs=costs)

    context = dict(
        selected_date=selected_date,
        today=today,
        prev_date=selected_date - timedelta(days=1),
        next_date=next_date,
        yesterday_date=today - timedelta(days=1),
        sales_total=sales_total,
        total_liters=total_liters,
        sale_count=sale_count,
        tank_rows=tank_rows,
        low_stock=low_stock,
        stock_value_total=stock_value_total,
        stock_liters_total=stock_liters_total,
        fuel_rate_cards=fuel_rate_cards_rows,
        last_activity_at=last_activity,
        freshness_text=(
            f"Last entry {humanize_since(last_activity)}" if last_activity else "No entries recorded yet"
        ),
        # Sales is the one Block A card an attendant sees too (see the
        # comment on tank_rows above), so its sparkline is built outside
        # the owner-only block below - one grouped-query pair, always
        # cheap regardless of role (see sales_sparkline()'s own docstring).
        sales_spark=charts.sparkline(sales_sparkline(selected_date)),
    )

    if current_user.is_owner:
        credit_given_total = (
            db.session.query(func.coalesce(func.sum(CreditGiven.amount), 0))
            .filter(CreditGiven.entry_date == selected_date)
            .scalar()
        )
        credit_given_pct = round(credit_given_total / sales_total * 100, 1) if sales_total else None

        bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
        bank_balances_by_id = {b.id: bank_account_balance_as_of(b, selected_date) for b in bank_accounts}
        cash_account = get_cash_account()
        cash_balance = cash_account_balance_as_of(cash_account, selected_date)

        margin = daily_margin(selected_date, costs=costs)
        dip_variance = dip_variance_for_date(selected_date, costs=costs)
        # Owner-only, computed here rather than left to the template to
        # hide - an attendant seeing "no dip recorded" for a tank they
        # don't manage is noise, not a useful signal. Also feeds
        # attention_items() below (Block F), so it's a local variable
        # rather than an inline kwarg in context.update().
        completeness = day_completeness(selected_date)

        # Part 0 (Phase 3): every account walked ONCE, eagerly loaded, and
        # shared by every account-consuming helper below - receivables
        # aging, working capital, customer concentration, and the payables
        # schedule all take this SAME list rather than each re-walking
        # Account.query.all() on its own. See account_positions()'s own
        # docstring for the >97%-of-backend-time problem this replaces
        # (previously: receivables_aging() and working_capital() each ran
        # their own independent Account.query.all() + relationship walk).
        positions = account_positions(selected_date)
        # Receivables total sourced from aging (below) rather than a
        # separate all_balances scan here - working_capital() computes the
        # same figure independently for the working-capital sum, and the
        # verification suite asserts the two agree exactly rather than
        # this route silently trusting that they will.
        aging = receivables_aging(selected_date, positions=positions)
        wc = working_capital(selected_date, positions=positions)
        # Phase 3 analytics that also read `positions` rather than walking
        # accounts again (spec sections 5-6).
        concentration = customer_concentration(positions)
        payables = payables_schedule(positions, selected_date)

        cash_movement = cash_movement_for_date(selected_date)
        revenue_mix = revenue_mix_for_date(selected_date)
        # include_previous=True additionally computes the preceding 30-day
        # window's profit series (one more pass of the same grouped
        # queries) for the profit chart's compare-to-previous-period
        # "ghost" overlay - see dashboard_trend_series()'s own docstring.
        trend = dashboard_trend_series(selected_date, days=30, include_previous=True)
        # Per-shift cash reconciliation for the carried-over-from-Phase-1
        # shift scorecard (spec section 0) - same data the Daily Report's
        # own Cash Handover table already renders.
        handover_rows = handover_rows_for_date(selected_date)

        # Phase 3 analytics (spec sections 1-4) - none of these walk
        # accounts, so they're independent of account_positions() above.
        pace = month_to_date_pace(selected_date)
        break_even = break_even_liters(selected_date, costs=costs)
        dead_stock_rows = dead_stock(selected_date)
        nozzle_data = nozzle_throughput(selected_date)

        # Block F - "Needs attention" (spec section 7). Every input here
        # is a card the rest of this route already built for its own
        # section further down the page; bad_cash_date is the one
        # genuinely new (and cheap, bounded) query this block adds.
        bad_cash_date = first_negative_cash_date()
        attention = attention_items(
            selected_date,
            tank_rows=tank_rows,
            dip_variance=dip_variance,
            completeness=completeness,
            fuel_rate_cards=fuel_rate_cards_rows,
            aging=aging,
            concentration=concentration,
            handover_rows=handover_rows,
            dead_stock_rows=dead_stock_rows,
            nozzle_rows=nozzle_data["rows"],
            bad_cash_date=bad_cash_date,
        )

        # Chart colors are theme tokens (var(--chart-N)), not literal hex -
        # see the matching comment in reports_trends() for the mapping.
        tank_colors = [
            "var(--chart-3)", "var(--chart-2)", "var(--chart-1)",
            "var(--chart-4)", "var(--chart-6)", "var(--chart-5)",
        ]
        profit_chart = charts.line_chart(
            [trend["profit"], trend["profit_previous"]],
            trend["labels"],
            ["var(--chart-2)", "var(--muted)"],
            ["Profit", "Profit (previous 30 days)"],
            interactive=True,
            ghost_indices=(1,),
        )
        cash_credit_chart = charts.stacked_bar_chart(
            trend["cash"], trend["credit"], trend["labels"], ["var(--chart-2)", "var(--chart-4)"], ["Cash Sales", "Credit Given"],
            interactive=True,
        )
        fuel_liters_chart = (
            charts.line_chart(
                list(trend["fuel_liters"].values()),
                trend["labels"],
                [tank_colors[i % len(tank_colors)] for i in range(len(trend["fuel_types"]))],
                trend["fuel_types"],
                interactive=True,
            )
            if trend["fuel_types"]
            else ""
        )
        # Rupee-valued twin of the litres chart above (same window, same
        # per-fuel-type colours) for the litres/rupees unit toggle - both
        # render server-side (spec section 8: no client-side re-rendering),
        # the client only ever flips which one is visible.
        fuel_amount_chart = (
            charts.line_chart(
                list(trend["fuel_amount"].values()),
                trend["labels"],
                [tank_colors[i % len(tank_colors)] for i in range(len(trend["fuel_types"]))],
                trend["fuel_types"],
                interactive=True,
            )
            if trend["fuel_types"]
            else ""
        )
        revenue_mix_donut = charts.donut_chart(revenue_mix["segments"])

        # Block A sparklines (Phase 2): Sales is built outside this
        # owner-only block (see context= above, staff sees it too).
        # Margin's sparkline reuses dashboard_trend_series()'s own profit
        # series unchanged, exactly as the spec directs, rather than
        # computing a second one - it already matches Total Margin's
        # definition ("fuel + products + other income"). Cash/Credit are
        # each one more cheap grouped-query helper (measured: 16 and 1
        # extra SQL statements respectively on a 10-tank/20-account scratch
        # DB - see the Phase 2 verification notes).
        #
        # Stock Value DELIBERATELY has NO sparkline, despite
        # stock_value_sparkline() existing and being correctly O(tanks) not
        # O(tanks x days) (stock_series() is a running total, not a
        # query-per-day - see its docstring). Measured on the same 10-tank
        # scratch DB: 80 SQL statements (~110-250ms depending on cache
        # warmth) for that ONE sparkline - roughly DOUBLING the Dashboard's
        # existing tank-related query volume (tank_stock_rows() itself is
        # 44 queries for 10 tanks) for one decorative trend squiggle. That
        # clears the spec's literal "not a query storm" bar but still isn't
        # a good trade against the ~1s page-budget this phase is required
        # to protect, so it's shipped in ledger_logic.py (importable,
        # tested) but not wired into this route. See the Phase 2 report for
        # the full measurement.
        margin_spark = charts.sparkline(trend["profit"])
        cash_spark = charts.sparkline(cash_balance_sparkline(selected_date))
        credit_spark = charts.sparkline(credit_given_sparkline(selected_date))

        # The nine-value "At a glance" rail (Phase 2 makes it sticky): a
        # deliberate mix of figures that already headline elsewhere on the
        # page (cash, receivables) alongside ones with no other card
        # (bank total, payables, net working capital, blended margin/
        # litre, credit % of sales, today's dip variance) - built entirely
        # from values already computed above, so it costs zero extra
        # queries. Phase 2 adds an optional "href" per item (spec section
        # 5's drill-down table) - only the values that table actually
        # names get one; the rest render as plain text same as before.
        kpi_rail = [
            {"label": "Cash in hand", "value": f"Rs {format_number(wc['cash'])}", "href": url_for("cash_account_detail")},
            {"label": "Bank balance", "value": f"Rs {format_number(wc['bank'])}"},
            {
                "label": "Receivables (all-time)",
                "value": f"Rs {format_number(wc['receivables'])}",
                "href": url_for("accounts"),
            },
            {"label": "Payables (all-time)", "value": f"Rs {format_number(wc['payables'])}", "href": url_for("accounts")},
            {"label": "Net working capital", "value": f"Rs {format_number(wc['net'])}", "href": url_for("accounts")},
            {"label": "Stock value", "value": f"Rs {format_number(stock_value_total)}", "href": url_for("inventory")},
            {
                "label": "Fuel margin / litre",
                "value": (
                    f"Rs {format_number(margin['margin_per_liter'])}" if margin["margin_per_liter"] is not None else "-"
                ),
            },
            {
                "label": "Credit as % of sales",
                "value": f"{credit_given_pct:.1f}%" if credit_given_pct is not None else "-",
            },
            {
                "label": "Dip variance today",
                "value": f"Rs {format_number(dip_variance['total_value'])}",
                "href": url_for("reports", date=selected_date.isoformat()),
            },
        ]

        context.update(
            credit_given_total=credit_given_total,
            credit_given_pct=credit_given_pct,
            bank_accounts=bank_accounts,
            bank_balances_by_id=bank_balances_by_id,
            cash_balance=cash_balance,
            completeness=completeness,
            margin=margin,
            dip_variance=dip_variance,
            aging=aging,
            working_capital=wc,
            cash_movement=cash_movement,
            revenue_mix=revenue_mix,
            handover_rows=handover_rows,
            profit_chart=profit_chart,
            cash_credit_chart=cash_credit_chart,
            fuel_liters_chart=fuel_liters_chart,
            fuel_amount_chart=fuel_amount_chart,
            revenue_mix_donut=revenue_mix_donut,
            kpi_rail=kpi_rail,
            margin_spark=margin_spark,
            cash_spark=cash_spark,
            credit_spark=credit_spark,
            # ---------------------------------------------- Phase 3 ----
            attention_items=attention,
            pace=pace,
            break_even=break_even,
            dead_stock_rows=dead_stock_rows,
            dead_stock_total=round(sum(r["value"] for r in dead_stock_rows), 2),
            nozzle_throughput=nozzle_data,
            concentration=concentration,
            payables=payables,
        )

    return render_template("dashboard.html", **context)


# ------------------------------------------------------------ inventory ---

def _inventory_range(args):
    """Parses the Inventory page's fuel-movement date-range controls:
    range=7|14 (default 7), or a custom start/end pair which wins when
    both are present (reversed dates are swapped, not rejected). Hard-
    capped at 14 days - if the resolved span exceeds that, start is
    pulled forward to end - 13 days and clamped=True comes back so the
    template can show a notice. end never exceeds today.
    fuel_movement_for_range() itself places no cap of its own; this is
    the one place a user-controlled range enters the app, so the cap has
    to live here."""
    today = date.today()
    raw_start = (args.get("start") or "").strip()
    raw_end = (args.get("end") or "").strip()
    clamped = False

    if raw_start and raw_end:
        start = parse_date_param(raw_start, fallback=today)
        end = parse_date_param(raw_end, fallback=today)
        if start > end:
            start, end = end, start
        preset = "custom"
    else:
        days = 14 if args.get("range") == "14" else 7
        preset = str(days)
        end = today
        start = end - timedelta(days=days - 1)

    if end > today:
        end = today
    if start > end:
        start = end

    if (end - start).days + 1 > 14:
        start = end - timedelta(days=13)
        clamped = True

    return start, end, preset, clamped


@app.route("/inventory")
@login_required
def inventory():
    today = date.today()
    costs = weighted_avg_costs(today)

    tank_rows = tank_stock_rows(today, costs=costs)
    variance_rows = tank_book_vs_actual(today, tank_rows, costs)

    range_start, range_end, range_preset, range_clamped = _inventory_range(request.args)
    movement = fuel_movement_for_range(range_start, range_end)
    fuel_summary = movement["fuels"]
    movement_days = movement["days"]

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

    # Rates for the non-fuel table, resolved in ONE bulk pass rather than
    # per row in the template: product_rates_on_date() is a query each,
    # and calling it inside a Jinja loop is exactly the N+1 storm
    # product_rate_resolver() exists to prevent. The retail rate is what
    # the shop sells at today; stock value is deliberately valued at the
    # PURCHASE rate (cost), matching how fuel stock value and dead_stock()
    # are valued elsewhere on this same page - mixing a retail-valued
    # figure in beside cost-valued ones would make the page's money
    # columns incomparable.
    resolve_product_rate = product_rate_resolver(products)
    product_stock_value = 0.0
    for row in product_rows:
        purchase_rate, retail_rate = resolve_product_rate(row["product"], today)
        row["purchase_rate"] = purchase_rate
        row["retail_rate"] = retail_rate
        row["stock_value"] = round(row["on_hand"] * (purchase_rate or 0.0), 2)
        product_stock_value += row["stock_value"]
    product_stock_value = round(product_stock_value, 2)

    insights = inventory_insights(today, tank_rows, variance_rows, product_rows)

    # "As at" the newest ledger entry that could move stock - falls back
    # to today on an empty pump (no purchases/sales recorded yet).
    as_at_candidates = [
        db.session.query(func.max(Sale.entry_date)).scalar(),
        db.session.query(func.max(DirectSale.entry_date)).scalar(),
        db.session.query(func.max(StockPurchase.entry_date)).scalar(),
    ]
    as_at_candidates = [d for d in as_at_candidates if d is not None]
    as_at_date = max(as_at_candidates) if as_at_candidates else today

    total_stock_value = round(sum(r["value"] for r in tank_rows), 2)
    total_liters = round(sum(r["stock"] for r in tank_rows), 2)
    # One headline card per fuel type instead of one combined litre count
    # - see fuel_totals_by_type(). total_liters stays in the context (the
    # template still uses it for the "across N tanks" cross-check line and
    # it costs nothing), it just no longer gets a card of its own.
    fuel_totals = fuel_totals_by_type(tank_rows)
    fuel_types = FuelType.query.order_by(FuelType.name).all()

    return render_template(
        "inventory.html",
        tank_rows=tank_rows,
        variance_rows=variance_rows,
        fuel_summary=fuel_summary,
        movement_days=movement_days,
        insights=insights,
        range_start=range_start,
        range_end=range_end,
        range_preset=range_preset,
        range_clamped=range_clamped,
        as_at_date=as_at_date,
        total_stock_value=total_stock_value,
        total_liters=total_liters,
        fuel_totals=fuel_totals,
        fuel_types=fuel_types,
        today=today,
        dispensers=dispensers,
        recent_purchases=recent_purchases,
        suppliers=suppliers,
        product_rows=product_rows,
        product_stock_value=product_stock_value,
    )


@app.route("/inventory/update-prices", methods=["POST"])
@login_required
@owner_required
def inventory_update_prices():
    """Set new SELLING (retail) prices for non-fuel products, effective
    from a date the owner picks - the only place selling prices are
    edited now that Settings has been reduced to cost/indent rates.

    Two things this route is careful about:

    ALL-OR-NOTHING. A price update is a batch the owner types in one go,
    so a single bad cell aborts the whole submission with nothing written.
    Half-applying it would leave the owner with no way to tell which
    products took the new price and which kept the old one, and the fix
    (retyping the batch) would then double-write history rows for the ones
    that did land. Everything is therefore validated into a pending list
    BEFORE any record_product_rates() call.

    THE PURCHASE RATE IS PASSED THROUGH UNCHANGED. ProductRateHistory
    carries both rates in one row, so writing a retail-only change still
    has to supply a purchase rate - and it must be the one in effect ON
    THE EFFECTIVE DATE (product_rates_on_date(), not Product.purchase_rate,
    which is only the current-rate cache and would be wrong for a
    backdated change). Getting this wrong would silently reset costs and
    corrupt every margin figure computed after that date.

    Blank inputs mean "leave this product's price alone" and are skipped
    entirely - the owner should never have to retype prices that are not
    changing. Product.query is tenant-scoped, so an id from another pump
    resolves to None here and is rejected rather than silently skipped:
    silence would tell the owner the price was set when it was not.
    """
    # parse_date_param() falls back to TODAY on a blank or malformed
    # value, which would silently apply the batch on the wrong date - so
    # it is given date.min as the fallback purely as a sentinel meaning
    # "did not parse", and that is rejected below. (A literal 0001-01-01
    # typed by hand is rejected too; that is not a real effective date.)
    raw_date = (request.form.get("effective_date") or "").strip()
    effective_date = parse_date_param(raw_date, fallback=date.min)
    if effective_date == date.min:
        flash("Please pick a valid date for the new prices to take effect from.", "error")
        return redirect(url_for("inventory"))

    pending = []
    for field, raw_value in request.form.items():
        if not field.startswith("new_price_"):
            continue
        raw_value = (raw_value or "").strip()
        if not raw_value:
            continue  # blank = leave this product's price alone

        try:
            product_id = int(field[len("new_price_"):])
        except ValueError:
            flash("That price form was malformed - nothing was changed.", "error")
            return redirect(url_for("inventory"))

        product = Product.query.filter_by(id=product_id).first()
        if product is None:
            flash("One of those products no longer exists - nothing was changed.", "error")
            return redirect(url_for("inventory"))

        try:
            new_retail = float(raw_value)
        except ValueError:
            flash(f"\"{raw_value}\" isn't a valid price for {product.label} - nothing was changed.", "error")
            return redirect(url_for("inventory"))
        if new_retail <= 0:
            flash(f"The new selling price for {product.label} has to be more than 0 - nothing was changed.", "error")
            return redirect(url_for("inventory"))

        pending.append((product, new_retail))

    if not pending:
        flash("No new prices were entered, so nothing was changed.", "info")
        return redirect(url_for("inventory"))

    for product, new_retail in pending:
        purchase_rate, _ = product_rates_on_date(product, effective_date)
        record_product_rates(product, purchase_rate, new_retail, effective_date)
    db.session.commit()

    flash(
        f"Updated the selling price of {len(pending)} product(s), effective "
        f"{effective_date.strftime('%d %b %Y')}.",
        "success",
    )
    return redirect(url_for("inventory"))


# ------------------------------------------------------------ accounts ---

ACCOUNT_TYPES = ("customer", "supplier", "employee", "owner")


def _validate_parent(account, parent_id):
    """Guard the parent/sub-account link. Returns an error string to flash,
    or None when the link is allowed.

    Sub-accounts are ONE LEVEL DEEP and nothing in the database enforces
    that (the FK is self-referential and would happily allow a chain, or
    even a cycle) - these three checks are the only thing keeping the tree
    flat, which is what lets group_balance stay a single non-recursive sum
    and lets every balance/aging consumer keep treating a sub-account as
    an ordinary account.

    account may be None (the add-account path, where the row does not
    exist yet) - the self-parent and has-own-children checks simply do not
    apply to a brand new account.

    A parent id that matches no account is treated as "no parent" rather
    than an error: Account.query is already tenant-scoped (see tenancy.py),
    so an id belonging to another pump resolves to None here and can never
    become a cross-pump parent - there is nothing extra to re-check.
    """
    if parent_id is None:
        return None

    if account is not None and parent_id == account.id:
        return "An account can't be its own parent."

    parent = db.session.get(Account, parent_id)
    if parent is None:
        return None

    if parent.parent_account_id is not None:
        return f"{parent.name} is already a sub-account - sub-accounts are only one level deep."

    if account is not None and account.children:
        return f"{account.name} has its own sub-accounts, so it can't become one."

    return None


def _parent_id_from_form(form, field="parent_account_id"):
    """Read the optional parent picker off a form. An empty string (the
    blank first option) means "top-level", i.e. None - which is also what
    clearing an existing link submits, so editing an account back to
    top-level works through the same path."""
    raw = (form.get(field) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _group_aging(account, children, as_of_date):
    """The aging buckets shown on ONE Accounts-list row.

    For a childless account this is plain credit_aging() and nothing has
    changed. For a parent - whose children are not listed separately on
    that page - the row's buckets are its own plus each child's, summed
    bucket by bucket, with the worst (largest) oldest_days of the group,
    so the "Oldest Unpaid" badge reflects the oldest money anywhere in the
    group rather than only the parent's own.

    This is a per-row DISPLAY aggregate only. The page's aging_totals are
    built separately from every account individually (see the invariant
    comment in _accounts_context) - never from these row figures."""
    own = credit_aging(account, as_of_date) if account.balance > 0 else None
    if not children:
        return own

    buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    oldest_days = None
    oldest_date = None
    parts = [own] + [credit_aging(c, as_of_date) if c.balance > 0 else None for c in children]
    found = False
    for part in parts:
        if not part:
            continue
        found = True
        for bucket, value in part["buckets"].items():
            buckets[bucket] = round(buckets[bucket] + value, 2)
        if part["oldest_days"] is not None and (oldest_days is None or part["oldest_days"] > oldest_days):
            oldest_days = part["oldest_days"]
            oldest_date = part["oldest_date"]
    if not found:
        return None
    return {
        "buckets": buckets,
        "outstanding": round(sum(buckets.values()), 2),
        "oldest_date": oldest_date,
        "oldest_days": oldest_days,
    }


def _accounts_context(kind, type_filter, q=""):
    """Shared by the Accounts page and its PDF/Excel export, so the two
    can never quietly drift apart - same filters, same rows, same aging
    totals, just rendered differently.

    q is the name search box. It is applied HERE, not in the view, for
    that same reason: an export that silently ignored the search would
    hand the owner a document that does not match the screen it was
    exported from. It filters by name only, case-insensitively, on a
    plain "contains" match - a khata search is someone half-remembering a
    name, not a query language.

    WHAT q DELIBERATELY DOES NOT TOUCH: aging_totals. Those are the
    headline "how old is the money owed to you" figures and are computed
    from every account in the type filter (see the invariant note further
    down). Narrowing them to whatever the owner happened to type into a
    search box would make a headline total mean something different from
    one keystroke to the next.
    """
    q = (q or "").strip()

    def matches_search(name):
        """Case-insensitive substring match, done in Python rather than as
        a SQL ilike so bank accounts and the synthetic Cash-in-Hand row -
        which are not Account rows and never go through that query - are
        filtered by exactly the same rule as accounts are."""
        return not q or q.casefold() in (name or "").casefold()
    if type_filter in ("bank", "cash"):
        # Debtor/creditor is a concept that only applies to customer/
        # supplier/employee accounts - bank accounts and cash-in-hand are
        # the pump's own money, not a relationship with someone else, so
        # they always show under "All" regardless of a stale kind= param.
        kind = "all"

    rows = []
    if type_filter not in ("bank", "cash"):
        # TOP-LEVEL ONLY: a sub-account is reached through its parent's
        # detail page, never listed here in its own right. That is also
        # what stops this list double-counting - a parent row shows the
        # rolled-up group figure, so listing its children alongside it
        # would show the same money twice.
        query = Account.query.filter(Account.parent_account_id.is_(None))
        if type_filter in ACCOUNT_TYPES:
            query = query.filter_by(account_type=type_filter)
        for a in query.all():
            # Debitor/creditor is purely a function of the account's current
            # balance sign - not its type label - so an account's
            # classification here can shift over time as its balance shifts.
            children = a.children
            # A childless account shows its OWN balance exactly as before.
            # A parent shows the group total, because its children are not
            # on this list to be seen separately - and every filter and
            # sort below then works off whatever is actually displayed.
            # Searched on the PARENT's name only, matching what the page
            # lists: a sub-account is not a row here, so matching one
            # would surface a row whose name does not contain the search
            # text and give no clue why.
            if not matches_search(a.name):
                continue
            balance = a.group_balance if children else a.balance
            aging = _group_aging(a, children, date.today())
            rows.append(
                {
                    "kind": "account",
                    "obj": a,
                    "name": a.name,
                    "balance": balance,
                    "aging": aging,
                    "child_count": len(children),
                    "is_group": bool(children),
                    "notes": a.notes,
                }
            )

    if kind == "all" and type_filter in ("all", "bank"):
        for b in BankAccount.query.all():
            # Bank accounts (and cash-in-hand) are the pump's own money,
            # not a debitor/creditor relationship, so they only show up
            # under "All" - not under the Debitors/Creditors filter.
            if not matches_search(b.name):
                continue
            rows.append({"kind": "bank", "obj": b, "name": b.name, "balance": b.balance})

    if kind == "all" and type_filter in ("all", "cash") and matches_search("Cash in Hand"):
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
    # Every top-level account, for the Add-an-Account form's optional
    # parent picker - deliberately NOT `rows` above, which is narrowed by
    # the page's type/kind filters and would otherwise hide perfectly
    # valid parents just because the owner is looking at "Suppliers".
    parent_candidates = (
        Account.query.filter(Account.parent_account_id.is_(None)).order_by(Account.name).all()
    )

    # Aging totals across every debitor, so overdue money is visible at a
    # glance instead of one account at a time.
    #
    # INVARIANT - these totals are computed from EVERY account, including
    # sub-accounts, deliberately NOT from `rows` above (which is filtered
    # to top-level accounts and carries rolled-up group figures). "Total
    # owed to you" has to keep meaning exactly what it means today: every
    # account counted once. Summing the rows instead would either drop
    # sub-accounts from a type/kind-filtered list or double-count them
    # against their parent's group figure - both of which would silently
    # change a headline number that nothing about grouping should touch.
    aging_query = Account.query
    if type_filter in ACCOUNT_TYPES:
        aging_query = aging_query.filter_by(account_type=type_filter)
    aging_totals = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    # Only a debit balance ages, so the Creditors view has nothing to
    # total here - exactly as before, when every creditor row carried
    # aging=None.
    if type_filter not in ("bank", "cash") and kind != "creditors":
        today_ = date.today()
        for a in aging_query.all():
            if a.balance <= 0:
                continue
            aging = credit_aging(a, today_)
            for bucket, value in aging["buckets"].items():
                aging_totals[bucket] = round(aging_totals[bucket] + value, 2)

    # The summary band at the top of the page. Computed from the rows
    # actually on screen - unlike aging_totals above, which is a headline
    # figure with a fixed meaning - because this band is a description OF
    # THIS LIST, so it has to move with the filters and the search or it
    # would be describing a list nobody is looking at.
    #
    # BANK AND CASH ROWS ARE EXCLUDED FROM THE MONEY FIGURES, deliberately.
    # They are the pump's OWN money, not a debitor/creditor relationship
    # with anyone (the same reason they only ever appear under "All", and
    # the same reason the page says so in prose). Folding a negative cash
    # balance into "You Owe" would claim the pump owes its own till money.
    # They still count as rows, though - the count describes the list.
    party_rows = [r for r in rows if r["kind"] == "account"]
    total_owed_to_you = round(sum(r["balance"] for r in party_rows if r["balance"] > 0), 2)
    total_you_owe = round(-sum(r["balance"] for r in party_rows if r["balance"] < 0), 2)

    return {
        "rows": rows,
        "kind": kind,
        "type_filter": type_filter,
        "q": q,
        "summary": {
            "owed_to_you": total_owed_to_you,
            "you_owe": total_you_owe,
            "net": round(total_owed_to_you - total_you_owe, 2),
            "count": len(rows),
        },
        "expenses": expenses,
        "bank_accounts": bank_accounts,
        "parent_candidates": parent_candidates,
        "aging_totals": aging_totals,
        "aging_total": round(sum(aging_totals.values()), 2),
    }


@app.route("/accounts")
@login_required
def accounts():
    kind = request.args.get("kind", "all")
    type_filter = request.args.get("type", "all")
    q = request.args.get("q", "")
    ctx = _accounts_context(kind, type_filter, q)
    return render_template("accounts.html", today=date.today(), **ctx)


@app.route("/accounts/export")
@login_required
def accounts_export():
    """Mirrors accounts() - same kind/type filters, same name search, same
    rows and aging totals - as either a PDF or an Excel workbook. Available to staff too,
    matching the page itself (owner-only content there is limited to the
    Expenses/Add-account panels, which this export doesn't include)."""
    fmt = _resolve_export_format()
    kind = request.args.get("kind", "all")
    type_filter = request.args.get("type", "all")
    q = request.args.get("q", "")
    ctx = _accounts_context(kind, type_filter, q)

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
    if ctx["q"]:
        filter_bits.append('search "' + ctx["q"] + '"')
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
    # Optional grouping - blank means an ordinary top-level account, which
    # is what every account was before this field existed.
    parent_account_id = _parent_id_from_form(request.form)
    parent_error = _validate_parent(None, parent_account_id)

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
    elif parent_error:
        flash(parent_error, "error")
    else:
        db.session.add(
            Account(
                name=name,
                phone=phone or None,
                account_type=account_type,
                opening_balance=opening_balance,
                opening_balance_date=opening_balance_date,
                parent_account_id=parent_account_id,
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
    children = sorted(account.children, key=lambda c: c.name.lower())
    # The edit form's parent picker only ever offers TOP-LEVEL accounts
    # other than this one - offering a sub-account would create a second
    # level, and offering itself would create a self-parent. Both are
    # rejected by _validate_parent() anyway; this just keeps the dropdown
    # from suggesting something that can only fail.
    parent_candidates = (
        Account.query.filter(Account.parent_account_id.is_(None), Account.id != account.id)
        .order_by(Account.name)
        .all()
    )
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
        children=children,
        group_balance=account.group_balance,
        parent_candidates=parent_candidates,
    )


@app.route("/accounts/<int:account_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_edit(account_id):
    account = db.session.get(Account, account_id) or abort(404)
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    account_type = request.form.get("account_type", "")
    # Blank = top-level, so clearing the picker un-links a sub-account
    # again through this same path.
    parent_account_id = _parent_id_from_form(request.form)
    parent_error = _validate_parent(account, parent_account_id)
    # Free-text context (see Account.notes). Normalised to None when blank
    # so "no note" is one value in the database rather than two - the
    # Accounts list's note indicator tests truthiness, and an account
    # whose note was cleared must stop showing one.
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Please enter a name.", "error")
    elif account_type not in ACCOUNT_TYPES:
        flash("Please choose an account type.", "error")
    elif parent_error:
        flash(parent_error, "error")
    else:
        account.name = name
        account.phone = phone or None
        account.account_type = account_type
        account.parent_account_id = parent_account_id
        account.notes = notes or None
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

    if account.children:
        flash(f"Can't delete {account.name} - it still has sub-accounts. Move them out first.", "error")
    elif has_entries or account.opening_balance:
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
    # Honours an optional hidden "next" field exactly as entry_delete()
    # does, so this one editor serves both the account page (which posts
    # no "next" and so falls back to itself) and the Ledger feed.
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)
    entry_date = parse_date_param(request.form.get("entry_date"))
    fuel_type_id = request.form.get("fuel_type_id", type=int)
    entry_mode = _resolve_entry_mode(request.form)
    liters_in = request.form.get("liters", type=float)
    amount_in = request.form.get("amount", type=float)
    vehicle_number = request.form.get("vehicle_number", "").strip()
    note = request.form.get("note", "").strip()
    fuel = db.session.get(FuelType, fuel_type_id) if fuel_type_id else None

    if not fuel:
        flash("Please choose a valid fuel type.", "error")
    elif (amount_error := _credit_amount_error(fuel, entry_date, entry_mode, liters_in, amount_in)):
        flash(amount_error, "error")
    else:
        price = price_on_date(fuel, entry_date)
        liters, amount = _derive_credit_liters_amount(entry_mode, liters_in, amount_in, price)
        entry.entry_date = entry_date
        entry.fuel_type_id = fuel.id
        entry.liters = liters
        entry.price_per_liter = price
        entry.amount = amount
        entry.vehicle_number = vehicle_number or None
        entry.note = note or None
        db.session.commit()
        flash("Credit entry updated.", "success")

    return redirect(next_url)


@app.route("/accounts/entry/receipt/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_receipt_edit(entry_id):
    entry = db.session.get(Receipt, entry_id) or abort(404)
    # Honours an optional hidden "next" field exactly as entry_delete()
    # does, so this one editor serves both the account page (which posts
    # no "next" and so falls back to itself) and the Ledger feed.
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)
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

    return redirect(next_url)


@app.route("/accounts/entry/purchase/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_purchase_edit(entry_id):
    entry = db.session.get(StockPurchase, entry_id) or abort(404)
    # Honours an optional hidden "next" field exactly as entry_delete()
    # does, so this one editor serves both the account page (which posts
    # no "next" and so falls back to itself) and the Ledger feed.
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)
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

    return redirect(next_url)


@app.route("/accounts/entry/supplier-payment/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_supplier_payment_edit(entry_id):
    entry = db.session.get(SupplierPayment, entry_id) or abort(404)
    # Honours an optional hidden "next" field exactly as entry_delete()
    # does, so this one editor serves both the account page (which posts
    # no "next" and so falls back to itself) and the Ledger feed.
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, error = _validate_cash_payment_edit(entry, entry_date, amount, request.form)

    if error:
        flash(error, "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Payment updated.", "success")

    return redirect(next_url)


@app.route("/accounts/entry/employee-loan/<int:entry_id>/edit", methods=["POST"])
@login_required
@owner_required
def account_entry_employee_loan_edit(entry_id):
    entry = db.session.get(EmployeeLoan, entry_id) or abort(404)
    # Honours an optional hidden "next" field exactly as entry_delete()
    # does, so this one editor serves both the account page (which posts
    # no "next" and so falls back to itself) and the Ledger feed.
    next_url = request.form.get("next") or url_for("account_detail", account_id=entry.account_id)
    entry_date = parse_date_param(request.form.get("entry_date"))
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    method, bank_account, error = _validate_cash_payment_edit(entry, entry_date, amount, request.form)

    if error:
        flash(error, "error")
    else:
        entry.entry_date = entry_date
        entry.amount = amount
        entry.method = method
        entry.bank_account_id = bank_account.id if bank_account else None
        entry.note = note or None
        db.session.commit()
        flash("Loan updated.", "success")

    return redirect(next_url)


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
            f"{entry.account.name} only owes Rs {format_number(max(outstanding_without_entry, 0))}, so you "
            f"can't deduct Rs {format_number(deduction)}.",
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
    "other_income": "Other Income",
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
        bits = [f"{format_number(obj.liters)} L {obj.fuel_type.name}"]
        if obj.vehicle_number:
            bits.append(obj.vehicle_number)
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] == "purchase":
        bits = [f"{format_number(obj.liters)} L {obj.tank.label}"]
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] == "sales_return":
        bits = [f"{format_number(obj.liters)} L {obj.fuel_type.name} returned to {obj.tank.label}"]
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] in ("product_sale", "product_purchase"):
        bits = [f"{format_number(obj.quantity)} {obj.product.unit} {obj.product.label}"]
        if obj.note:
            bits.append(obj.note)
        return " - ".join(bits)
    if e["kind"] == "salary":
        text = obj.period_label or "Salary"
        if obj.deduction_amount:
            text += f" - Rs {format_number(obj.deduction_amount)} deducted against advance"
        return text
    if e["kind"] == "other_income":
        # An Other Income row only ever reaches an account's statement when
        # method == "credit" - account_id is never set otherwise - so
        # there's no payment-method text to show here, just the
        # description, mirroring how product_sale/product_purchase's
        # branch above doesn't show payment method either.
        return obj.description
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
    error = _validate_amount_cash_edit(entry, entry_date, amount)

    if error:
        flash(error, "error")
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
    error = _validate_amount_cash_edit(entry, entry_date, amount)

    if error:
        flash(error, "error")
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
    # Direct-entry tank-level sales for the same date - see DirectSale's
    # docstring in models.py. There is no per-nozzle breakdown table for
    # these (they're not in the returned context below, only folded into
    # the same totals Sale contributes to), since DirectSale has no
    # nozzle to break down by in the first place.
    direct_sales = DirectSale.query.filter_by(entry_date=selected_date).all()
    # Every sum() below starts from 0.0 rather than the default 0 - on a
    # date with none of that kind, plain sum(empty) is an int, which the
    # HTML template's "%.2f" format papers over but the PDF export's
    # formatter (which has to tell a count apart from a money figure by
    # its Python type) would otherwise render as a bare "0" instead of
    # "0.00", inconsistent with every other row.
    total_sales = sum((s.total_amount for s in sales), 0.0) + sum((d.total_amount for d in direct_sales), 0.0)
    # Sale/DirectSale always record fuel at full list price with no
    # discount capability of their own - net out any discretionary discount
    # given via a CreditGiven row on this date (see
    # credit_discounts_for_period()'s docstring), or total_sales (and
    # everything below derived from it - cash_sales, net_cash_flow) would
    # overstate revenue/cash by the discount.
    total_discounts = credit_discounts_for_period(selected_date, selected_date)
    total_sales -= total_discounts
    total_liters = sum((s.liters for s in sales), 0.0) + sum((d.liters for d in direct_sales), 0.0)
    # Testing has no DirectSale equivalent at all (see models.py) - it
    # only ever comes from metered Sale rows.
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
    # cash_sales is deliberately NOT computed here: total_sales,
    # total_credit_given and total_bank_sales all still gain their
    # pass-through tanker terms further down (see the tanker block), and
    # cash is the remainder once all three are final.

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

    # Income that isn't a product sale (rent, a side-business profit
    # share, ...) - cash/bank only, no credit option (see OtherIncome in
    # models.py), so unlike sales returns/product sales above there's no
    # "on account" method to exclude from the cash total.
    other_income_entries = (
        OtherIncome.query.filter_by(entry_date=selected_date).order_by(OtherIncome.recorded_at).all()
    )
    total_other_income = sum((oi.amount for oi in other_income_entries), 0.0)
    cash_other_income_total = sum((oi.amount for oi in other_income_entries if oi.method == "cash"), 0.0)

    # Pass-through tanker deals (see TankerDeal in models.py). Their SALE
    # side is money collected on this date, so it does count in the
    # collected-money cards - Total Sales / Credit Given / Bank Sales,
    # and therefore Cash Sales as the remainder - matching
    # sales_breakdown_for_date() on the Ledger. They are still NEVER
    # folded into total_liters, by_fuel or the tank rows: no fuel left a
    # tank and none was dispensed, so adding them there would inflate
    # litres sold and corrupt every per-litre figure derived from it. The
    # tanker_* keys below stay as they are - they are the PROFIT view
    # (the "Tanker Deal Margin" card), which is a different question from
    # how the money came in.
    tanker_deals = TankerDeal.query.filter_by(entry_date=selected_date).order_by(TankerDeal.recorded_at).all()
    tanker_liters = sum((d.liters for d in tanker_deals), 0.0)
    tanker_revenue = sum((d.sale_amount for d in tanker_deals), 0.0)
    tanker_cost = sum((d.purchase_cost for d in tanker_deals), 0.0)
    tanker_margin = round(tanker_revenue - tanker_cost, 2)
    tanker_credit_sales_total = sum(
        (d.sale_amount for d in tanker_deals if d.sale_payment_type == "credit"), 0.0
    )
    tanker_bank_sales_total = sum(
        (d.sale_amount for d in tanker_deals if d.sale_payment_type == "bank"), 0.0
    )
    cash_tanker_sales_total = sum(
        (d.sale_amount for d in tanker_deals if d.sale_payment_type == "cash"), 0.0
    )
    cash_tanker_purchases_total = sum(
        (d.purchase_cost for d in tanker_deals if d.purchase_payment_type == "cash"), 0.0
    )

    # The pump-only figures are kept under their own names because
    # net_cash_flow below still needs them: it adds the cash-settled
    # tanker sale as its own explicit term, so it must not also read a
    # total_sales that already contains it.
    pump_sales_total = total_sales
    pump_credit_given = total_credit_given
    total_sales += tanker_revenue
    total_credit_given += tanker_credit_sales_total
    total_bank_sales += tanker_bank_sales_total
    # Cash is the remainder, so a cash-settled tanker sale lands in it
    # automatically - there is no fourth tanker term here, and nothing
    # downstream may add cash_tanker_sales_total to cash_sales.
    cash_sales = total_sales - total_credit_given - total_bank_sales

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

    # Built from the PUMP-only sales/credit figures plus the cash-settled
    # tanker sale as its own term. Using the tanker-inclusive total_sales /
    # total_credit_given here instead would double-count a cash tanker sale
    # (it is inside total_sales already) and would silently pull a
    # BANK-settled tanker sale into a cash-flow figure. Same reason
    # cash_movement_for_date() dropped its separate "Tanker sales" inflow
    # once the breakdown started carrying it.
    net_cash_flow = (
        pump_sales_total
        - pump_credit_given
        + total_payments
        - total_expenses
        - cash_purchases_total
        - total_supplier_payments
        - total_salaries_net
        - cash_sales_returns_total
        + cash_product_sales_total
        - cash_product_purchases_total
        + cash_other_income_total
        + cash_tanker_sales_total
        - cash_tanker_purchases_total
    )
    outstanding_credit = sum((b for a in Account.query.all() if (b := a.balance) > 0), 0.0)
    # Date-aware closing balances for the SELECTED date, not the all-time
    # figure cash_account_balance()/BankAccount.balance return - the
    # Ledger/Daily Report/Dashboard pages are date-driven, so paging back
    # to an older date must show cash-in-hand and each bank's balance as
    # they stood at the END of that date (see cash_account_balance_as_of()
    # and bank_account_balance_as_of() in ledger_logic.py). Pages with no
    # date to page against (Accounts, bank account detail, Settings) keep
    # calling the plain all-time functions, unchanged.
    cash_balance = cash_account_balance_as_of(get_cash_account(), selected_date)
    bank_accounts = BankAccount.query.order_by(BankAccount.name).all()
    bank_balances_by_id = {b.id: bank_account_balance_as_of(b, selected_date) for b in bank_accounts}

    return {
        "total_sales": total_sales,
        "total_discounts": total_discounts,
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
        "bank_balances_by_id": bank_balances_by_id,
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
        "other_income_entries": other_income_entries,
        "total_other_income": total_other_income,
        "tanker_deals": tanker_deals,
        "tanker_liters": tanker_liters,
        "tanker_revenue": tanker_revenue,
        "tanker_cost": tanker_cost,
        "tanker_margin": tanker_margin,
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
        ("Other Income (Rs)", ctx["total_other_income"]),
        ("Tanker Deal Margin (Rs)", ctx["tanker_margin"]),
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
        summary_rows.append((f"{b.name} (Rs)", ctx["bank_balances_by_id"][b.id]))

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
            "heading": "Other Income",
            "columns": ["Description", "Method", "Amount (Rs)"],
            "rows": [
                [oi.description, "Via " + oi.bank_account.name if oi.method == "bank" else "Cash", oi.amount]
                for oi in ctx["other_income_entries"]
            ],
            "align": ["left", "left", "right"],
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
    + Tanker Deal Margin + Other Income - Expenses - Salaries = Net
    Profit. cogs_for_period() nets sales
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
    # revenue/liters_sold include DirectSale (see models.py) as well as
    # Sale - without this, gross_margin/net_profit below would silently
    # undercount for any period with direct-entry days, even though
    # cogs_for_period() (their COGS counterpart) already folds DirectSale
    # into its own per-fuel gross figures. testing has no DirectSale
    # equivalent at all (see Sale/DirectSale docstrings in models.py).
    revenue = float(
        db.session.query(func.coalesce(func.sum(Sale.total_amount), 0))
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .scalar()
    )
    revenue += float(
        db.session.query(func.coalesce(func.sum(DirectSale.total_amount), 0))
        .filter(DirectSale.entry_date >= start, DirectSale.entry_date <= end)
        .scalar()
    )
    # Sale/DirectSale always record fuel at full list price with no
    # discount capability of their own - net out any discretionary discount
    # given via a CreditGiven row in this period (see
    # credit_discounts_for_period()'s docstring), or revenue (and every
    # figure below derived from it - net_revenue, gross_margin, net_profit)
    # would overstate what was actually earned on a discounted credit sale.
    total_discounts = credit_discounts_for_period(start, end)
    revenue -= total_discounts
    liters_sold = float(
        db.session.query(func.coalesce(func.sum(Sale.liters), 0))
        .filter(Sale.entry_date >= start, Sale.entry_date <= end)
        .scalar()
    )
    liters_sold += float(
        db.session.query(func.coalesce(func.sum(DirectSale.liters), 0))
        .filter(DirectSale.entry_date >= start, DirectSale.entry_date <= end)
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
    # Per-fuel breakdown of those same two all-fuel totals - purchases_liters
    # and purchases_cost above must equal the sum of this list's liters/cost.
    purchases_by_fuel = stock_purchases_by_fuel_for_period(start, end)
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
    # Income that isn't a product sale (rent, a side-business profit
    # share, ...) - has no associated cost, so unlike product_commission
    # above it's a pure addition, not a revenue-minus-cost margin.
    other_income_total = float(
        db.session.query(func.coalesce(func.sum(OtherIncome.amount), 0))
        .filter(OtherIncome.entry_date >= start, OtherIncome.entry_date <= end)
        .scalar()
    )
    # No further "- sales_returns_amount" here: net_revenue (and therefore
    # gross_margin) is already net of returns. Subtracting it again would
    # double-count the same refund - that was the bug this comment is
    # guarding against. Net Profit is Total Gross Margin (fuel + product)
    # plus Other Income, minus operating costs.
    # Pass-through tanker deals (see TankerDeal in models.py) - their own
    # revenue/cost/margin, deliberately kept OUT of revenue, liters_sold,
    # cogs and gross_margin above. Nothing was dispensed from a tank and
    # the cost is this one tanker's exact invoice, not a weighted average,
    # so folding it into those would both inflate litres sold and make
    # Cost of Fuel Sold mean two different things at once. It is a real
    # profit though, so it is added into net_profit as its own line, the
    # same pure-addition way other_income_total is.
    tanker_liters = float(
        db.session.query(func.coalesce(func.sum(TankerDeal.liters), 0))
        .filter(TankerDeal.entry_date >= start, TankerDeal.entry_date <= end)
        .scalar()
    )
    tanker_revenue = float(
        db.session.query(func.coalesce(func.sum(TankerDeal.sale_amount), 0))
        .filter(TankerDeal.entry_date >= start, TankerDeal.entry_date <= end)
        .scalar()
    )
    tanker_cost = float(
        db.session.query(func.coalesce(func.sum(TankerDeal.purchase_cost), 0))
        .filter(TankerDeal.entry_date >= start, TankerDeal.entry_date <= end)
        .scalar()
    )
    tanker_margin = round(tanker_revenue - tanker_cost, 2)
    net_profit = round(
        total_gross_margin + tanker_margin + other_income_total - expenses_total - salaries_total, 2
    )

    return {
        "revenue": revenue,
        "total_discounts": total_discounts,
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
        "tanker_liters": tanker_liters,
        "tanker_revenue": tanker_revenue,
        "tanker_cost": tanker_cost,
        "tanker_margin": tanker_margin,
        "other_income_total": other_income_total,
        "expenses_total": expenses_total,
        "expenses_by_category": expenses_by_category,
        "salaries_total": salaries_total,
        "net_profit": net_profit,
        "credit_given": credit_given,
        "receipts_total": receipts_total,
        "purchases_liters": purchases_liters,
        "purchases_cost": purchases_cost,
        "purchases_by_fuel": purchases_by_fuel,
        "sales_returns_liters": sales_returns_liters,
        "sales_returns_amount": sales_returns_amount,
        "attendant_variances": attendant_variance_summary(start, end),
    }


def _fuel_color_map(cogs_detail, purchases_by_fuel):
    """The SAME fuel must be the SAME colour in both Monthly Report donuts
    and both card grids, so colour is keyed on the fuel NAME in a stable
    alphabetical order, not on per-donut rank (where the 2nd-biggest fuel
    in one donut could otherwise land on a different colour than the
    2nd-biggest in the other). Palette and ordering match
    revenue_mix_for_date()'s (chart-2, chart-4, chart-3, chart-6,
    chart-1), with chart-5 appended for a 6th fuel type."""
    palette = [
        "var(--chart-2)", "var(--chart-4)", "var(--chart-3)",
        "var(--chart-6)", "var(--chart-1)", "var(--chart-5)",
    ]
    names = sorted({r["fuel"] for r in cogs_detail} | {r["fuel"] for r in purchases_by_fuel})
    return {name: palette[i % len(palette)] for i, name in enumerate(names)}


def _reports_monthly_extras(start, end, ctx):
    """Everything the redesigned Monthly Report page needs that is
    deliberately NOT in _reports_monthly_context() - because
    month_to_date_pace() calls that context function three times on every
    Dashboard load, and account_positions() / _cash_daily_net_changes() /
    a prior-month context call are each far too expensive to run 3x for a
    card that only reads net_revenue/net_profit. This function is called
    exactly twice: once by reports_monthly(), once (partially) by
    reports_monthly_export()."""
    today = date.today()

    # 1. Period cash summary (month-scoped, honest).
    cash_period = cash_movement_for_period(start, end)

    # 2. Payables / receivables. ONE account walk, shared by both.
    positions = account_positions(today, include_aging=False)
    wc = working_capital(today, positions=positions)
    payables_rows = payables_schedule(positions, today)
    receivable_rows = sorted(
        (
            {"account": p["account"], "balance": round(p["balance"], 2)}
            for p in positions
            if p["balance"] > 0
        ),
        key=lambda r: r["balance"],
        reverse=True,
    )

    # 3. Prior FULL month context, for the profit-vs-last-month narrative rule.
    prior_end = start - timedelta(days=1)
    prior_start = prior_end.replace(day=1)
    prior_ctx = _reports_monthly_context(prior_start, prior_end)
    prior_month_label = prior_start.strftime("%B %Y")

    # 4. Best sales day inside the month.
    best_day = best_sales_day_for_period(start, end)

    # 5. Stable per-fuel colour map, shared by both donuts and both card grids.
    fuel_colors = _fuel_color_map(ctx["cogs_detail"], ctx["purchases_by_fuel"])

    # 6. Donut segments (money-valued only - donut_chart() prints "Rs").
    fuel_sold_segments = sorted(
        (
            {"label": r["fuel"], "amount": round(r["revenue"], 2), "color": fuel_colors[r["fuel"]]}
            for r in ctx["cogs_detail"]
            if r["revenue"] > 0
        ),
        key=lambda s: s["amount"],
        reverse=True,
    )
    stock_ordered_segments = sorted(
        (
            {"label": r["fuel"], "amount": round(r["cost"], 2), "color": fuel_colors[r["fuel"]]}
            for r in ctx["purchases_by_fuel"]
            if r["cost"] > 0
        ),
        key=lambda s: s["amount"],
        reverse=True,
    )

    # 7. Narrative.
    narrative = monthly_narrative(start, end, ctx, prior_ctx, prior_month_label, best_day)

    # 8. Step-by-step profit walk.
    profit_steps = profit_walkthrough(ctx)

    return {
        "cash_period": cash_period,
        "wc": wc,
        "payables_rows": payables_rows,
        "payables_total": wc["payables"],
        "receivable_rows": receivable_rows,
        "receivables_total": wc["receivables"],
        "balances_as_of": today,
        "prior_ctx": prior_ctx,
        "prior_month_label": prior_month_label,
        "best_day": best_day,
        "fuel_colors": fuel_colors,
        "fuel_sold_donut": charts.donut_chart(fuel_sold_segments),
        "stock_ordered_donut": charts.donut_chart(stock_ordered_segments),
        "narrative": narrative,
        "profit_steps": profit_steps,
        "thin_margin_per_liter": THIN_MARGIN_PER_LITER,
    }


def month_to_date_pace(as_of_date):
    """MTD revenue/profit pace for the Dashboard's "This month so far"
    card: what's happened this month, what the SAME number of days into
    last month looked like (a fair comparison - 18 partial days of August
    against all 31 days of July would always look like August is losing,
    even at an identical daily run rate), last month's full total (to
    answer "will we beat last month?"), and a straight-line projection to
    month end.

    Built entirely from _reports_monthly_context() - called up to three
    times (this month to date, last month to the same day-count, last
    month in full) rather than reimplementing any margin/COGS/sales-returns
    arithmetic here. That function's own docstring documents the
    sales-returns double-count bug this phase must not reopen; three calls
    to it (a handful of grouped, bounded queries each) is a completely
    different order of cost from the O(accounts) problem Part 0 exists to
    fix, so this stays well inside the "bounded queries" constraint.

    revenue/profit figures used throughout are the NET ones
    (_reports_monthly_context()'s "net_revenue"/"net_profit" - already net
    of sales returns) rather than the gross "revenue" key, so the pace
    comparison and the projection are never contaminated by a return that
    hasn't been netted out yet.

    days_elapsed is 1..days_in_month, inclusive of as_of_date itself (day 1
    of the month has days_elapsed == 1, not 0), so the projection formula
    (mtd / days_elapsed * days_in_month) is never a divide-by-zero."""
    month_start = as_of_date.replace(day=1)
    days_elapsed = (as_of_date - month_start).days + 1
    month_end = (month_start + timedelta(days=31)).replace(day=1) - timedelta(days=1)
    days_in_month = month_end.day

    prior_month_end = month_start - timedelta(days=1)
    prior_month_start = prior_month_end.replace(day=1)
    prior_days_in_month = prior_month_end.day
    # Clamped so e.g. "day 31" of a 31-day month doesn't overrun a 28/29/30
    # day February when walking back one month - the comparison then uses
    # as many days as the prior month actually has, which is the fairest
    # number available rather than an out-of-range date.
    prior_same_day_count = min(days_elapsed, prior_days_in_month)
    prior_mtd_end = prior_month_start + timedelta(days=prior_same_day_count - 1)

    mtd_ctx = _reports_monthly_context(month_start, as_of_date)
    prior_mtd_ctx = _reports_monthly_context(prior_month_start, prior_mtd_end)
    prior_full_ctx = _reports_monthly_context(prior_month_start, prior_month_end)

    mtd_revenue = mtd_ctx["net_revenue"]
    mtd_profit = mtd_ctx["net_profit"]
    prior_mtd_revenue = prior_mtd_ctx["net_revenue"]
    prior_mtd_profit = prior_mtd_ctx["net_profit"]
    prior_full_revenue = prior_full_ctx["net_revenue"]
    prior_full_profit = prior_full_ctx["net_profit"]

    projected_revenue = round(mtd_revenue / days_elapsed * days_in_month, 2)
    projected_profit = round(mtd_profit / days_elapsed * days_in_month, 2)

    def _delta_pct(current, prior):
        # abs(prior) as the denominator keeps the sign of the delta
        # meaningful even when prior itself was a loss (a negative
        # prior_mtd_profit that improves toward zero should read as a
        # POSITIVE delta, not a confusing negative-over-negative).
        if not prior:
            return None
        return round((current - prior) / abs(prior) * 100, 1)

    revenue_delta_pct = _delta_pct(mtd_revenue, prior_mtd_revenue)
    profit_delta_pct = _delta_pct(mtd_profit, prior_mtd_profit)

    return {
        "month_start": month_start,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "mtd_revenue": mtd_revenue,
        "mtd_profit": mtd_profit,
        "prior_mtd_revenue": prior_mtd_revenue,
        "prior_mtd_profit": prior_mtd_profit,
        "prior_full_revenue": prior_full_revenue,
        "prior_full_profit": prior_full_profit,
        "projected_revenue": projected_revenue,
        "projected_profit": projected_profit,
        "revenue_delta_pct": revenue_delta_pct,
        "profit_delta_pct": profit_delta_pct,
        "ahead": profit_delta_pct is not None and profit_delta_pct >= 0,
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
    extras = _reports_monthly_extras(start, end, ctx)

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
        **extras,
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
    cash_period = cash_movement_for_period(start, end)

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
                ("Tanker Deal Margin (Rs)", ctx["tanker_margin"]),
                ("Other Income (Rs)", ctx["other_income_total"]),
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
            "heading": "Stock Ordered by Fuel Type",
            "columns": ["Fuel", "Litres Received", "Total Cost (Rs)", "Avg Cost / L"],
            "rows": [
                [r["fuel"], r["liters"], r["cost"], r["avg_cost_per_liter"]]
                for r in ctx["purchases_by_fuel"]
            ],
            "align": ["left", "right", "right", "right"],
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
            "heading": "Cash Summary",
            "columns": ["Line", "Amount (Rs)"],
            "rows": (
                [["Opening cash in hand", cash_period["opening"]]]
                + [[f"+ {row['label']}", row["amount"]] for row in cash_period["inflows"]]
                + [["Total cash received", cash_period["total_in"]]]
                + [[f"- {row['label']}", row["amount"]] for row in cash_period["outflows"]]
                + [
                    ["Total cash paid", cash_period["total_out"]],
                    ["Closing cash in hand", cash_period["closing"]],
                ]
            ),
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
        return _group_sum_by_day(model, value_col, start, end, date_col)

    sales_by_day = group_sum(Sale, Sale.total_amount)
    # Fold DirectSale (see models.py) into the same by-day revenue totals
    # Sale contributes to - every series/total built from sales_by_day
    # below (cash_series, profit_series, totals.total_sales, ...) must
    # reflect BOTH entry methods, exactly like sales_breakdown_for_date()
    # and cash_account_balance() already do.
    for d, v in group_sum(DirectSale, DirectSale.total_amount).items():
        sales_by_day[d] = sales_by_day.get(d, 0) + v
    # Net out per-day discretionary discounts on discounted CreditGiven rows
    # (see credit_discounts_for_period()'s docstring) - sales_by_day above is
    # gross Sale/DirectSale (always list price), so without this cash_series,
    # profit_series, and totals.total_sales (all built from sales_by_day)
    # would overstate revenue/cash by the discount. Row-by-row, grouped by
    # date, mirroring dashboard_trend_series()'s own _profit_series_for_window()
    # fix in ledger_logic.py rather than one credit_discounts_for_period()
    # call per day.
    for entry_date_val, liters, price, amount in (
        db.session.query(CreditGiven.entry_date, CreditGiven.liters, CreditGiven.price_per_liter, CreditGiven.amount)
        .filter(CreditGiven.entry_date >= start, CreditGiven.entry_date <= end)
        .all()
    ):
        list_amount = round(liters * price, 2)
        if abs(amount - list_amount) > 0.01:
            sales_by_day[entry_date_val] = sales_by_day.get(entry_date_val, 0) - (list_amount - amount)
    credit_by_day = group_sum(CreditGiven, CreditGiven.amount)
    payments_by_day = group_sum(Receipt, Receipt.amount)
    expenses_by_day = group_sum(Expense, Expense.amount)
    purchase_liters_by_day = group_sum(StockPurchase, StockPurchase.liters)
    salaries_by_day = group_sum(SalaryPayment, SalaryPayment.gross_amount)
    returns_amount_by_day = group_sum(SalesReturn, SalesReturn.amount)
    product_revenue_by_day = group_sum(ProductSale, ProductSale.amount)
    other_income_by_day = group_sum(OtherIncome, OtherIncome.amount)
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
    # DirectSale is already tank-keyed - no Nozzle join needed.
    for entry_date_val, fuel_type_id, liters in (
        db.session.query(DirectSale.entry_date, Tank.fuel_type_id, func.sum(DirectSale.liters))
        .join(Tank, DirectSale.tank_id == Tank.id)
        .filter(DirectSale.entry_date >= start, DirectSale.entry_date <= end)
        .group_by(DirectSale.entry_date, Tank.fuel_type_id)
        .all()
    ):
        key = (entry_date_val, fuel_type_id)
        sold_liters_by_day_fuel[key] = sold_liters_by_day_fuel.get(key, 0) + (liters or 0)
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
    # Per-day pass-through tanker margin (see TankerDeal in models.py),
    # grouped by date the same way every other term here is, and added to
    # profit as its OWN addend - never into sales_by_day (which also backs
    # cash_series and the by-fuel litre/rupee charts, all of which mean
    # fuel actually dispensed) and never into cogs_by_day (a weighted
    # per-litre cost this deal has no part in). This mirrors
    # _profit_series_for_window() in ledger_logic.py line for line, and is
    # what keeps this page reconciling with _reports_monthly_context()'s
    # net_profit, which adds tanker_margin the same separate way.
    tanker_margin_by_day = {}
    for entry_date_val, sale_amount, purchase_cost in (
        db.session.query(TankerDeal.entry_date, TankerDeal.sale_amount, TankerDeal.purchase_cost)
        .filter(TankerDeal.entry_date >= start, TankerDeal.entry_date <= end)
        .all()
    ):
        tanker_margin_by_day[entry_date_val] = round(
            tanker_margin_by_day.get(entry_date_val, 0) + (sale_amount - purchase_cost), 2
        )
    profit_series = [
        round(
            (sales_by_day.get(d, 0) - returns_amount_by_day.get(d, 0))  # net fuel revenue
            - cogs_by_day.get(d, 0)  # net fuel COGS
            - expenses_by_day.get(d, 0)
            - salaries_by_day.get(d, 0)
            + product_margin_series[i]
            + other_income_by_day.get(d, 0)
            + tanker_margin_by_day.get(d, 0),
            2,
        )
        for i, d in enumerate(all_dates)
    ]

    # Chart colors are theme tokens (var(--chart-N)), not literal hex, so
    # every chart re-themes with the rest of the app. The mapping keeps
    # each old hex's "identity" wherever it appeared (green=cash-positive
    # -> chart-2, red=credit/expense -> chart-4, indigo=neutral secondary
    # series -> chart-3) so a color still means the same thing across every
    # chart on this page, same intent as revenue_mix_for_date()'s own
    # docstring in ledger_logic.py.
    sales_chart = charts.stacked_bar_chart(
        cash_series, credit_series, labels, ["var(--chart-2)", "var(--chart-4)"], ["Cash Sales", "Credit Given"]
    )
    cashflow_chart = charts.line_chart(
        [receipts_series, expenses_series], labels, ["var(--chart-3)", "var(--chart-4)"], ["Receipts", "Expenses"]
    )
    purchases_chart = charts.bar_chart(purchases_series, labels, "var(--chart-3)")
    profit_chart = charts.line_chart([profit_series], labels, ["var(--chart-2)"], ["Profit (Est.)"])

    tanks = Tank.query.order_by(Tank.number).all()
    tank_colors = [
        "var(--chart-3)", "var(--chart-2)", "var(--chart-1)",
        "var(--chart-4)", "var(--chart-6)", "var(--chart-5)",
    ]
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
        by_day = {r[0]: r[1] or 0 for r in rows}
        # DirectSale is already tank-keyed - no Nozzle join needed.
        direct_rows = (
            db.session.query(DirectSale.entry_date, func.sum(DirectSale.liters))
            .join(Tank, DirectSale.tank_id == Tank.id)
            .filter(Tank.fuel_type_id == ft.id, DirectSale.entry_date >= start, DirectSale.entry_date <= end)
            .group_by(DirectSale.entry_date)
            .all()
        )
        for d, liters in direct_rows:
            by_day[d] = by_day.get(d, 0) + (liters or 0)
        fuel_sold_by_day[ft.name] = by_day
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
        total_other_income=round(sum(other_income_by_day.values()), 2),
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

    # This whole function runs with no request context (see the module
    # docstring in tenancy.py) - current_pump_id() is None here, so every
    # query below is naturally unscoped already. But the two writes further
    # down (Shift, User) need an explicit pump_id: the before_flush
    # auto-stamp (tenancy.py) refuses to guess one with no pump context,
    # by design, so it has to be set by hand here instead. The Stage 1
    # migration always creates exactly one pump before this runs, so
    # there's always exactly one to seed against.
    with unscoped():
        pump = Pump.query.first()
    if not pump:
        pump = Pump(name="My Pump")
        db.session.add(pump)
        db.session.commit()

    # Every reading/credit/bank-sale row needs a shift, so one always has
    # to exist. A pump that doesn't split its day just leaves this single
    # shift in place and never sees a shift selector anywhere.
    with unscoped():
        has_shift = Shift.query.filter_by(pump_id=pump.id).count() > 0
    if not has_shift:
        db.session.add(Shift(name="Full Day", sort_order=0, pump_id=pump.id))
        db.session.commit()

    with unscoped():
        has_user = User.query.filter_by(pump_id=pump.id).count() > 0
    if not has_user:
        owner = User(username="owner", role="owner", pump_id=pump.id)
        owner.set_password("owner123")
        staff = User(username="staff", role="staff", pump_id=pump.id)
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
