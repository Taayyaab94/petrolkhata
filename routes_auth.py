"""Auth routes: login, logout, signup, change/forgot/reset password,
email verification (verify + resend), accepting a staff invite, and
Google OAuth (login + callback - google/disconnect is a /settings/...
route and lives in routes_settings.py instead, grouped by URL like the
rest of that module).

Moved verbatim out of app.py - this is the auth route group, unchanged
except for its location.
"""

from datetime import datetime

from flask import (
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
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from extensions import db
from tenancy import unscoped
from models import CashAccount, Pump, Shift, User
from app import (
    app,
    EMAIL_RE,
    RESET_TOKEN_TTL_HOURS,
    _find_valid_token,
    _issue_auth_token,
    _password_errors,
    _send_reset_email,
    _send_verification_email,
    google_enabled,
    oauth,
)

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
