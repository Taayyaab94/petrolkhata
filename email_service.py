"""Outbound email via Resend's plain HTTPS API (https://resend.com).

Deliberately NOT SMTP/Flask-Mail: this app deploys to Vercel serverless
functions, which commonly cannot open outbound SMTP ports at all - an
HTTPS API is the only option that's reliably reachable from there.
Resend was picked over the alternatives for two practical reasons: its
free tier (3,000 emails/month, 100/day) comfortably covers this app's
signup/reset volume, and it will send from its own
`onboarding@resend.dev` address with no domain verification step, which
is exactly what's needed while this is still a testing-stage app with no
custom domain configured yet.

Every caller in app.py goes through send_email() and nothing else -
swapping providers later (or adding a fallback) means editing this one
module, not any route.

RESEND_API_KEY unset -> send_email() logs the full message (see below)
and returns False instead of raising. This is deliberate, not a
fallback-for-errors: it is what makes signup/forgot-password/etc.
testable right now with zero provider account, per the Stage 2 spec, and
it must never turn into a 500 for the request that triggered the send.
"""
import datetime
import os
import sys
import traceback

import requests

RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_MAIL_FROM = "onboarding@resend.dev"


def _log(message):
    """Writes one line to the server log. Deliberately a plain
    print(file=sys.stderr) rather than the `logging` module: a
    logging.StreamHandler() caches whatever `sys.stderr` object was
    current when the handler was constructed (import time, here), but
    Flask's CLI (via Click) can swap in a wrapped stdout/stderr for
    ANSI-color handling on Windows AFTER that - confirmed empirically in
    this app's own verification run: messages logged that way never
    reached `flask run`'s redirected log file at all, while Werkzeug's
    own request-log lines (which resolve their stream lazily) did.
    print(file=sys.stderr) looks up sys.stderr fresh on every call, so it
    can't go stale the same way, on any platform."""
    timestamp = datetime.datetime.now().isoformat(sep=" ", timespec="seconds")
    print(f"{timestamp} [email_service] {message}", file=sys.stderr, flush=True)


def send_email(to, subject, html):
    """Best-effort send; never raises. Returns True if handed off to the
    provider successfully, False otherwise (including "no provider
    configured"). Callers must not treat False as a request-level error -
    see the module docstring: a failed/unconfigured send must always
    still let the user-facing flow (signup, forgot-password, ...)
    complete normally."""
    api_key = os.environ.get("RESEND_API_KEY")
    mail_from = os.environ.get("MAIL_FROM", DEFAULT_MAIL_FROM)

    if not api_key:
        # No provider configured - log the full email, link and all, so
        # the flow stays testable with zero setup. Safe only because this
        # branch is exactly the "nothing was actually sent anywhere"
        # case; the configured branch below never logs the body.
        _log(
            "RESEND_API_KEY not set - email NOT sent, logging instead.\n"
            f"  To: {to}\n  Subject: {subject}\n  Body:\n{html}"
        )
        return False

    try:
        response = requests.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": mail_from, "to": [to], "subject": subject, "html": html},
            timeout=10,
        )
        response.raise_for_status()
    except Exception:
        # Network error, bad key, Resend outage, etc. - a real provider
        # is configured here, so the body (which may carry a live
        # reset/verification link) is deliberately NOT included in this
        # log line.
        _log(f"Failed to send email to {to} (subject: {subject!r}):\n{traceback.format_exc()}")
        return False

    _log(f"Email sent to {to}: {subject!r}")
    return True
