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

# Every static URL carries a ?v=<content hash> (see _static_cache_buster
# below), so a file's URL changes whenever the file itself does. That is
# what makes caching it for a year safe. Without this Flask sends
# "Cache-Control: no-cache", which forced the browser to revalidate
# style.css on EVERY page view - a full round trip to the function region
# (measured ~280-550ms) to be told "304, nothing changed, no body".
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000  # one year


_STATIC_FINGERPRINTS = {}


def static_fingerprint(filename):
    """Short content hash of a file under static/, computed once per
    process and remembered. Returns None if the file isn't readable, in
    which case the URL is left unversioned rather than breaking the page."""
    if filename not in _STATIC_FINGERPRINTS:
        digest = None
        try:
            with open(os.path.join(app.static_folder, filename), "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()[:10]
        except OSError:
            digest = None
        _STATIC_FINGERPRINTS[filename] = digest
    return _STATIC_FINGERPRINTS[filename]


@app.after_request
def _cache_static_at_the_edge(response):
    """Vercel's CDN only caches a function's response when it is told to
    with s-maxage; a plain max-age is a browser-only instruction, which is
    why every static file was a MISS and woke the function in Virginia.
    With this the edge nearest the user (Mumbai) serves style.css itself.

    Only ever applied to the `static` endpoint - no page is cached, since
    every page is per-pump and per-user data."""
    if request.endpoint == "static":
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, s-maxage=31536000, immutable"
        )
    return response


# A view query string as sent by static/ledger-ajax.js. Deliberately
# strict: this value ends up in a Location header, so anything that could
# carry a newline (header injection) or a second host must not get through.
_LEDGER_VIEW = re.compile(r"\?[A-Za-z0-9_\-=&%.+]*\Z")


@app.after_request
def _ajax_save_lands_on_the_viewed_page(response):
    """Point a ledger save's redirect at the page the user is actually
    looking at, so the AJAX layer doesn't have to fetch it separately.

    Every ledger POST ends `redirect(url_for("ledger", date=entry_date))`
    - the entry's own date, with no ?shift=. That is usually NOT the view
    the user is on, so static/ledger-ajax.js threw the followed response
    away and re-fetched the current URL, rendering the whole ledger a
    SECOND time: two full renders (165 queries each) per saved entry.

    When the save is an AJAX one the browser sends the view it is on, and
    the redirect is pointed there instead - so the response fetch follows
    is already the page the script wants to graft in, and one render does
    the job. Nothing else is touched: the handler still validates, still
    flashes, still redirects, and a save with JavaScript off takes the
    original path unchanged.
    """
    view = request.headers.get("X-Ledger-View")
    if (
        view is not None
        and request.method == "POST"
        and request.headers.get("X-Requested-With") == "XMLHttpRequest"
        and response.status_code in (301, 302, 303, 307, 308)
        and (view == "" or _LEDGER_VIEW.match(view))
    ):
        ledger_path = url_for("ledger")
        location = response.headers.get("Location") or ""
        # Only ever rewrite a redirect that was already going to the
        # ledger - a handler that bounces somewhere else (login, an
        # account page) must keep its own destination.
        if location.partition("?")[0].rstrip("/") == ledger_path.rstrip("/"):
            response.headers["Location"] = ledger_path + view
    return response


@app.url_defaults
def _static_cache_buster(endpoint, values):
    """Append the content hash to every url_for('static', ...). Templates
    are left completely untouched - they keep calling url_for the way they
    always did, and the version appears automatically."""
    if endpoint == "static" and "filename" in values and "v" not in values:
        digest = static_fingerprint(values["filename"])
        if digest:
            values["v"] = digest


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

# --------------------------------------------------- product catalogue ----

PRODUCT_CATEGORIES = ("lubricant", "filter", "shop", "other")
PRODUCT_UNITS = ("piece", "litre")



# -------------------------------------------------------------- ledger ----
# (routes moved to routes_ledger.py - these four helpers stay because
# routes_accounts.py's edit handlers use them too)

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



# ------------------------------------------------------------ dashboard ---

_ATTENTION_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


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


# Route modules register their @app.route handlers on this same `app`
# object - imported down here, after everything they depend on
# (helpers, models, extensions) is already defined above.
import routes_settings  # noqa: E402,F401
import routes_accounts  # noqa: E402,F401
import routes_reports  # noqa: E402,F401
import routes_auth  # noqa: E402,F401
import routes_ledger  # noqa: E402,F401

# month_to_date_pace lives in routes_reports.py (it's built entirely on
# top of that module's _reports_monthly_context()), but dashboard() above
# is its only caller. dashboard() only looks the name up when a request
# actually calls it - by then this import has already run - so this
# works the same way the bottom-of-file route-module imports do, without
# needing a deferred import inside dashboard() itself.
from routes_reports import month_to_date_pace  # noqa: E402,F401


with app.app_context():
    ensure_seed_users()


if __name__ == "__main__":
    app.run(debug=True)
