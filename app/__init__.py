"""Flask application factory (FP-ARCH-1).

Server-rendered UI with route-level authentication/role checks and service-level
business rules/ownership checks. Error pages carry the FP-AUTH-3 status contract.
"""

import os
import secrets
import sqlite3
from pathlib import Path

from flask import Flask, g, redirect, render_template, request, session, url_for
from flask.sessions import SecureCookieSessionInterface
from werkzeug.routing import IntegerConverter
from werkzeug.routing import ValidationError as RoutingValidationError

from app import db as db_module
from app.errors import AppError, AuthenticationError, AuthorizationError, RateLimitError
from app.security import RateLimiter, csrf_token_valid, new_csrf_token
from app.timeutils import format_display

BASE_DIR = Path(__file__).resolve().parent.parent


class SafeIntegerConverter(IntegerConverter):
    """Report an integer path segment that cannot be converted as a missing route.

    CPython 3.11+ caps int<->str conversion at 4300 digits. The default
    ``<int:...>`` converter lets that ``ValueError`` escape while matching the
    URL, which Flask surfaces as a 500. Re-raising it as Werkzeug's routing
    ``ValidationError`` makes the URL unmatched (404), so an id that cannot
    exist is reported as a missing object (FP-AUTH-3) instead of an error.
    """

    def to_python(self, value: str):
        try:
            return super().to_python(value)
        except ValueError as exc:
            raise RoutingValidationError() from exc


def _env_bool(name: str):
    value = os.environ.get(name)
    return None if value is None else value.strip().lower() in ("1", "true", "yes", "on")


SECURITY_HEADERS = {
    # Scripts only from our own origin (no inline/eval); inline <style> for the template styles.
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class _SessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):
        configured = app.config.get("SESSION_COOKIE_SECURE")
        return request.is_secure if configured is None else bool(configured)


def create_app(test_config=None) -> Flask:
    app = Flask(__name__)
    # Bound the ``<int:...>`` converter before any blueprint rule is added.
    app.url_map.converters["int"] = SafeIntegerConverter
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("DATABASE") or str(BASE_DIR / "data" / "app.sqlite3"),
        PORT=int(os.environ.get("PORT", "8080")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # None => "Secure" exactly when the request arrived over HTTPS (so plain-HTTP
        # local demos keep working); set True/False (env SESSION_COOKIE_SECURE=1/0) to force.
        SESSION_COOKIE_SECURE=_env_bool("SESSION_COOKIE_SECURE"),
        # Docker/NAT: every client may share one source IP, so the IP-keyed login/register
        # limits are switched off in the image (RATE_LIMIT_ENABLED=0 in the Dockerfile);
        # behind a real proxy set TRUSTED_PROXY_COUNT so the true client IP is used.
        RATE_LIMIT_ENABLED=_env_bool("RATE_LIMIT_ENABLED") is not False,
        TRUSTED_PROXY_COUNT=int(os.environ.get("TRUSTED_PROXY_COUNT", "0")),
        # Paid-API guard, keyed by *user id* (never by IP), so shared IPs cannot collide.
        EXPLAIN_LIMIT=10,
        EXPLAIN_WINDOW_SECONDS=600,
        LOGIN_FAILURE_LIMIT=5,  # failed logins per email+IP ...
        LOGIN_IP_FAILURE_LIMIT=30,  # ... or per IP ...
        LOGIN_WINDOW_SECONDS=900,  # ... within this window => 429
        REGISTER_LIMIT=20,  # registration attempts per IP per hour => 429
        REGISTER_WINDOW_SECONDS=3600,
        # Optional FP-MATCH-2 explanations. Unset => deterministic non-LLM text only.
        LLM_API_KEY=os.environ.get("ANTHROPIC_API_KEY") or None,
        LLM_MODEL=os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001"),
    )
    if test_config:
        app.config.update(test_config)

    if app.config["TRUSTED_PROXY_COUNT"] > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix

        n = app.config["TRUSTED_PROXY_COUNT"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n)
    app.session_interface = _SessionInterface()
    app.extensions["rate_limiter"] = RateLimiter()
    db_module.init_app(app)

    from app.routes.auth import bp as auth_bp
    from app.routes.candidate import bp as candidate_bp
    from app.routes.employer import bp as employer_bp
    from app.routes.helpers import clear_session
    from app.routes.jobs import bp as jobs_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(candidate_bp)
    app.register_blueprint(employer_bp)
    app.register_blueprint(jobs_bp)

    @app.before_request
    def load_user_and_csrf():
        if "csrf_token" not in session:
            session["csrf_token"] = new_csrf_token()
        g.user = None
        user_id = session.get("user_id")
        auth_post = request.method == "POST" and request.endpoint in {"auth.login", "auth.register"}
        g.database_generation = (
            db_module.session_generation() if user_id is not None or auth_post else None
        )
        if user_id is not None:
            if session.get("database_generation") == g.database_generation:
                row = db_module.get_db().execute(
                    "SELECT id, email, role FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                if row is not None:
                    g.user = dict(row)
            if g.user is None:
                clear_session()
        # Let Flask report unmatched routes/methods as 404/405. For protected
        # POSTs, authentication takes precedence over CSRF (FP-AUTH-3).
        if request.url_rule is None:
            return
        if (
            request.method == "POST"
            and g.user is None
            and request.endpoint not in {"auth.login", "auth.register"}
        ):
            raise AuthenticationError("Please log in")
        if request.method == "POST" and not csrf_token_valid(
            session.get("csrf_token", ""), request.form.get("csrf_token", "")
        ):
            raise AuthorizationError("Invalid or missing CSRF token")

    @app.after_request
    def finalize_response(response):
        # A reset can also commit while a page is being read or a login verified.
        generation = g.get("database_generation")
        if generation is not None and generation != db_module.session_generation():
            clear_session()
            g.user = None
            if request.method == "GET":
                response = app.make_response(redirect(url_for("auth.login")))
            else:
                response = app.make_response(handle_app_error(AuthenticationError("Please log in")))
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if g.get("user") is not None or request.endpoint in ("auth.login", "auth.register"):
            # Private pages must not be stored or replayed from caches/back button.
            response.headers["Cache-Control"] = "no-store"
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.errorhandler(AppError)
    def handle_app_error(error: AppError):
        response = render_template("errors/error.html", error=error), error.status
        if isinstance(error, RateLimitError):
            response = app.make_response(response)
            response.headers["Retry-After"] = str(error.retry_after)
        return response

    @app.errorhandler(404)
    def handle_404(_error):
        return render_template("errors/error.html", error=AppError("Not found", status=404)), 404

    @app.errorhandler(sqlite3.OperationalError)
    def handle_db_busy(error):
        if "locked" in str(error) or "busy" in str(error):
            # Writers are serialized (BEGIN IMMEDIATE); a very long wait surfaces as
            # a retryable 503 rather than a 500 or a half-applied change.
            return render_template(
                "errors/error.html", error=AppError("Database busy, please retry", status=503)
            ), 503
        raise error

    @app.errorhandler(OverflowError)
    def handle_overflow(_error):
        # An id too large for SQLite INTEGER can never name an existing object.
        return render_template("errors/error.html", error=AppError("Not found", status=404)), 404

    @app.errorhandler(405)
    def handle_405(_error):
        return render_template("errors/error.html", error=AppError("Method not allowed", status=405)), 405

    @app.template_filter("utc_display")
    def utc_display(value):
        return format_display(value) if value else ""

    @app.context_processor
    def inject_globals():
        from flask import session as flask_session

        return {
            "current_user": g.get("user"),
            "csrf_token": lambda: flask_session.get("csrf_token", ""),
        }

    return app
