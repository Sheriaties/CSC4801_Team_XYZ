"""Job detail, applying, and interview booking routes (FP-CAN-2, FP-CAN-3, FP-SCHED-2)."""

from functools import partial

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from app import ai

from app.errors import RateLimitError, ValidationError
from app.routes.helpers import current_user, current_user_id, login_required, role_required
from app.services import applications as applications_service
from app.services import jobs as jobs_service
from app.services import profiles as profiles_service
from app.services import scheduling as scheduling_service

bp = Blueprint("jobs", __name__)


def _llm_client():
    """The configured LLM client, or None (deterministic explanation only)."""
    client = current_app.config.get("LLM_CLIENT")
    if client is not None:
        return client
    key = current_app.config.get("LLM_API_KEY")
    if key:
        return partial(ai.anthropic_complete, api_key=key, model=current_app.config["LLM_MODEL"])
    return None


def _render_detail(job_id: int, use_ai: bool):
    job = jobs_service.get_job(job_id)
    user = current_user()
    user_id = user["id"]
    applied = applications_service.has_applied(user_id, job_id)
    explanation = None
    if user["role"] == "Candidate":
        skills = profiles_service.get_candidate_profile(user_id)["skills"]
        explanation = ai.explain_match(
            skills, job["required_skills"], _llm_client() if use_ai else None,
            secrets=[current_app.config.get("LLM_API_KEY") or ""],
        )
    return render_template(
        "jobs/detail.html",
        job=job,
        applied=applied,
        explanation=explanation,
        ai_available=_llm_client() is not None,
        ai_asked=use_ai,
    )


@bp.route("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    return _render_detail(job_id, use_ai=False)


@bp.route("/jobs/<int:job_id>/explain", methods=("POST",))
@role_required("Candidate")
def explain(job_id: int):
    """Optional FP-MATCH-2: AI wording of the match. Never changes score/order."""
    limit = current_app.config["EXPLAIN_LIMIT"]
    if limit and _llm_client() is not None:
        limiter = current_app.extensions["rate_limiter"]
        key = ("explain", current_user_id())
        wait = limiter.blocked(key, limit, current_app.config["EXPLAIN_WINDOW_SECONDS"])
        if wait:
            raise RateLimitError("Too many AI explanations, please try again later", retry_after=wait)
        limiter.hit(key, current_app.config["EXPLAIN_WINDOW_SECONDS"])  # each call can cost money
    return _render_detail(job_id, use_ai=True)


@bp.route("/jobs/<int:job_id>/apply", methods=("POST",))
@role_required("Candidate")
def apply(job_id: int):
    applications_service.apply_to_job(current_user_id(), job_id)
    flash("Application submitted.", "success")
    return redirect(url_for("candidate.applications"))


@bp.route("/bookings", methods=("POST",))
@role_required("Candidate")
def create_booking():
    try:
        application_id = int(request.form.get("application_id", ""))
        slot_id = int(request.form.get("slot_id", ""))
    except ValueError:
        raise ValidationError("Invalid application or slot")
    scheduling_service.book_slot(current_user_id(), application_id, slot_id)
    flash("Interview booked.", "success")
    return redirect(url_for("candidate.application_detail", application_id=application_id))
