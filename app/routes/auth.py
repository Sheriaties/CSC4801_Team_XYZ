"""Registration, login, and logout routes (FP-AUTH-1)."""

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from app.errors import AppError, RateLimitError
from app.routes.helpers import clear_session, current_user, login_required
from app.services import auth as auth_service


def _start_session(user_id: int):
    """Log a user in while keeping the CSRF token bound to this browser session."""
    csrf = session.get("csrf_token")
    session.clear()
    session["user_id"] = user_id
    session["database_generation"] = g.database_generation
    session["csrf_token"] = csrf


bp = Blueprint("auth", __name__)


@bp.route("/")
def home():
    user = current_user()
    if user is None:
        return redirect(url_for("auth.login"))
    if user["role"] == "Employer":
        return redirect(url_for("employer.jobs"))
    return redirect(url_for("candidate.dashboard"))


def _limiter():
    return current_app.extensions["rate_limiter"] if current_app.config["RATE_LIMIT_ENABLED"] else None


def _check(limiter, key, limit, window):
    wait = limiter.blocked(key, limit, window)
    if wait:
        raise RateLimitError("Too many attempts, please try again later", retry_after=wait)


@bp.route("/register", methods=("GET", "POST"))
def register():
    if current_user() is not None:
        return redirect(url_for("auth.home"))
    if request.method == "POST":
        limiter = _limiter()
        if limiter is not None:
            key = ("register", request.remote_addr)
            _check(limiter, key, current_app.config["REGISTER_LIMIT"], current_app.config["REGISTER_WINDOW_SECONDS"])
            limiter.hit(key, current_app.config["REGISTER_WINDOW_SECONDS"])
        role = request.form.get("role", "")
        try:
            user_id = auth_service.register_user(
                email=request.form.get("email", ""),
                password=request.form.get("password", ""),
                role=role,
                display_name=request.form.get("display_name", ""),
                company_name=request.form.get("company_name", ""),
            )
        except AppError as exc:
            return render_template("auth/register.html", error=exc.message), exc.status
        _start_session(user_id)
        flash("Account created. You are now logged in.", "success")
        if role == "Employer":
            return redirect(url_for("employer.jobs"))
        return redirect(url_for("candidate.dashboard"))
    return render_template("auth/register.html", error=None)


@bp.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        limiter = _limiter()
        window = current_app.config["LOGIN_WINDOW_SECONDS"]
        pair_key, ip_key = ("login", request.remote_addr, email), ("login-ip", request.remote_addr)
        if limiter is not None:
            _check(limiter, pair_key, current_app.config["LOGIN_FAILURE_LIMIT"], window)
            _check(limiter, ip_key, current_app.config["LOGIN_IP_FAILURE_LIMIT"], window)
        user = auth_service.authenticate(email, request.form.get("password", ""))
        if user is None:
            if limiter is not None:
                limiter.hit(pair_key, window)
                limiter.hit(ip_key, window)
            # 401: unknown email and wrong password are indistinguishable.
            return render_template("auth/login.html", error="Invalid email or password"), 401
        if limiter is not None:
            limiter.reset(pair_key)
        _start_session(user["id"])
        flash("Logged in.", "success")
        if user["role"] == "Employer":
            return redirect(url_for("employer.jobs"))
        return redirect(url_for("candidate.dashboard"))
    return render_template("auth/login.html", error=None)


@bp.route("/logout", methods=("POST",))
@login_required
def logout():
    clear_session()
    flash("Logged out.", "success")
    return redirect(url_for("auth.login"))
