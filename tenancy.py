"""Tenant (pump) scoping enforcement.

Every business table in this app belongs to exactly one pump (see
TenantScoped in models.py). This module is the single place that decides
which pump a given piece of code is allowed to see, plus the SQLAlchemy
event machinery that turns that decision into an actual filter on every
read and an actual stamp on every write.

The read side pushes a `with_loader_criteria(TenantScoped, pump_id == X)`
onto every statement executed through the session, via a `do_orm_execute`
event. This has been empirically verified (see the proof-of-concept this
was ported from) to cover every query shape this codebase uses:
`Model.query`/`select()`, `db.session.get(Model, pk)`, `db.session.query
(func.sum(...))` aggregates, relationship/backref loads, and joins across
two scoped models. It does NOT cover bulk `Query.delete()`/`Query.update()`
or raw SQL - those are audited by hand instead (see app.py/ledger_logic.py
call sites and the Stage 1 report for the full list).

The write side stamps `pump_id` on every new TenantScoped row in a
`before_flush` event, so ~100 individual `db.session.add(...)` call sites
don't each have to remember to set it by hand. `pump_id` is NOT NULL, so a
row that reaches the database with no pump context raises rather than
silently landing with a NULL or wrong pump_id.

Resolution order for current_pump_id() - fail-closed by design:
  1. unscoped() block active -> None (no filter - deliberate, narrow opt-out)
  2. request context + authenticated user -> that user's pump_id
  3. request context + NOT authenticated -> UNAUTHENTICATED_SENTINEL, a
     pump_id that matches no real pump. NOT None - an unauthenticated
     request must never see rows just because no user happens to be
     attached to it; @login_required is a second line of defence here,
     not the first.
  4. no request context at all (CLI, `flask db migrate`/`upgrade`,
     ensure_seed_users() running at import time) -> None (unscoped).
     This is a deliberate, understood hole: it is required for migrations
     and first-run bootstrap to be able to see/create the seed pump
     before any request (and therefore any pump_id) exists. There are no
     background jobs in this app, so this is the ONLY code that ever runs
     outside a request context.
"""
import contextvars
from contextlib import contextmanager

from flask import g, has_request_context

# A pump_id no real Pump row will ever have (ids start at 1). Used so an
# unauthenticated request is scoped to "nothing" rather than "everything".
UNAUTHENTICATED_SENTINEL = -1

# Fallback for when there is no Flask request context to hang a flag off
# of (g is only available inside one). contextvars is used rather than a
# bare module global so this is correct under any future concurrency model
# (threads, greenlets, asyncio) without extra work.
_unscoped_cv = contextvars.ContextVar("tenancy_unscoped", default=False)


def _unscoped_active():
    if has_request_context():
        return getattr(g, "_tenancy_unscoped", False)
    return _unscoped_cv.get()


def _set_unscoped(value):
    if has_request_context():
        g._tenancy_unscoped = value
    else:
        _unscoped_cv.set(value)


@contextmanager
def unscoped():
    """Deliberately bypass tenant filtering for this block.

    The ONLY legitimate uses in this app are: looking a user up by
    username at login (you don't know their pump until you've found
    them), Flask-Login's user_loader (same reason, runs on every request
    that carries a session cookie, before current_user is resolved), and
    bootstrap/migration code that runs with no request context anyway.

    Re-entrant safe: nesting unscoped() blocks (or calling it again after
    it's already active) always restores the exact previous value on
    exit, via `finally`, even if the block raises.
    """
    previous = _unscoped_active()
    _set_unscoped(True)
    try:
        yield
    finally:
        _set_unscoped(previous)


def current_pump_id():
    """The pump every tenant-scoped query/write is limited to right now,
    or None for a deliberately unscoped context. See the module docstring
    for the full resolution order."""
    if _unscoped_active():
        return None

    if has_request_context():
        # Imported lazily so this module has no hard import-time dependency
        # on flask_login being configured yet.
        from flask_login import current_user

        if current_user.is_authenticated:
            # unscoped(): current_user's own SQLAlchemy instance can be
            # EXPIRED at this point (Flask-SQLAlchemy expires every
            # mapped object after each commit) - reading .pump_id off an
            # expired instance triggers SQLAlchemy to transparently
            # re-SELECT that same User row, which re-enters
            # do_orm_execute, which calls current_pump_id() again to
            # build the filter, which reads current_user.pump_id again -
            # unbounded recursion (confirmed empirically: a real
            # RecursionError, not a hypothetical). Bypassing the filter
            # for this one lookup is safe: it's keyed by the user_id
            # already inside the signed, server-issued session cookie
            # (not attacker-controlled), and the only thing returned is
            # that single already-authenticated user's own pump_id - the
            # same trust boundary load_user() and the login lookup
            # already cross for the same reason (see unscoped()'s
            # docstring).
            with unscoped():
                return current_user.pump_id
        return UNAUTHENTICATED_SENTINEL

    return None


def register_tenancy_events(db):
    """Wire the enforcement mechanism into Flask-SQLAlchemy's session.
    Call this once, after db.init_app(app).
    """
    from sqlalchemy import event
    from sqlalchemy.orm import Session, with_loader_criteria

    from models import TenantScoped

    @event.listens_for(Session, "do_orm_execute")
    def _apply_tenant_filter(state):
        """Read side: filter every SELECT issued through the ORM down to
        the current pump. Guarded exactly as validated in the PoC:
        is_select (bulk UPDATE/DELETE and raw SQL are NOT is_select, and
        are NOT covered by this - they're audited by hand instead), and an
        explicit `skip_tenant_filter` execution-option escape hatch."""
        if not state.is_select:
            return
        if state.execution_options.get("skip_tenant_filter"):
            return

        pump_id = current_pump_id()
        if pump_id is None:
            return

        # The lambda MUST close over `pump_id` as a genuine free variable
        # (this exact shape - not a `_pump_id=pump_id` default-argument
        # capture, which was tried here and caused a confirmed, serious
        # bug: SQLAlchemy's statement cache keys `with_loader_criteria` by
        # tracking the lambda's __closure__ cells (track_closure_variables,
        # on by default), NOT its __defaults__. A default-argument value
        # is invisible to that tracking, so the compiled SQL from the
        # FIRST pump to run a given query shape got silently reused - with
        # that first pump's id baked in as a literal - for every OTHER
        # pump's identical-shaped query afterwards, regardless of the
        # value passed in. Reproduced empirically in the Stage 1 breach
        # test suite: two users on two different pumps issuing the same
        # `select(Account).where(Account.id == :id)` got the FIRST user's
        # pump_id filter both times. This exact closure form (matching the
        # validated PoC) is the only one confirmed safe - do not
        # "optimize" it back to a default-argument capture.
        state.statement = state.statement.options(
            with_loader_criteria(
                TenantScoped,
                lambda cls: cls.pump_id == pump_id,
                include_aliases=True,
            )
        )

    @event.listens_for(Session, "before_flush")
    def _stamp_pump_id(session, flush_context, instances):
        """Write side: auto-stamp pump_id on any pending TenantScoped
        object that doesn't already have one set, instead of requiring
        every one of the ~100 db.session.add(...) call sites across
        app.py/ledger_logic.py to remember to pass pump_id explicitly.

        Raises if there is no usable pump context, rather than letting a
        NULL (impossible - the column is NOT NULL, so this would just
        become a confusing IntegrityError) or a wrong pump_id (e.g. the
        unauthenticated sentinel) reach the database."""
        for obj in session.new:
            if not isinstance(obj, TenantScoped):
                continue
            if getattr(obj, "pump_id", None) is not None:
                continue
            pump_id = current_pump_id()
            if pump_id is None or pump_id == UNAUTHENTICATED_SENTINEL:
                raise RuntimeError(
                    f"Refusing to create a {type(obj).__name__} row with no "
                    "pump context - this would corrupt tenant data. Pass "
                    "pump_id explicitly (bootstrap/migration code), or make "
                    "sure this runs inside a request from an authenticated "
                    "user."
                )
            obj.pump_id = pump_id
