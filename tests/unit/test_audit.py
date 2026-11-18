"""Regression tests for security-section self-audit findings (FP-SEC-*, FP-SCHED-1)."""

import pytest

from app.security import csrf_token_valid
from tests.conftest import make_future_slot_start


def test_audit_non_ascii_csrf_token_is_rejected_not_500(app):
    assert csrf_token_valid("abc", "é") is False
    r = app.test_client().post("/login", data={"email": "a", "password": "b", "csrf_token": "é☃"})
    assert r.status_code == 403


@pytest.mark.parametrize("path,expected", [
    ("/jobs/99999999999999999999", 404),
    ("/applications/99999999999999999999", 404),
    ("/candidates/99999999999999999999/resume", 404),  # missing resume: never 500
])
def test_audit_huge_ids_never_500(client, app, world, path, expected):
    world()
    client.login("alice@test.local", "Password123!")
    assert client.get(path).status_code == expected


def test_audit_huge_form_ids_and_far_future_slot(client, app, world):
    ids = world()
    client.login("alice@test.local", "Password123!")
    assert client.post("/bookings", {"application_id": "9" * 30, "slot_id": 1}).status_code == 404
    client.logout()
    client.login("ana@test.local", "Password123!")
    job = ids["jobs"]["backend"]
    assert client.post("/employer/slots", {"job_id": "9" * 30, "start_utc": make_future_slot_start()}).status_code == 404
    # Year 9999 overflows start + 30 min: must be a 400, not a 500.
    r = client.post("/employer/slots", {"job_id": job, "start_utc": "9999-12-31T23:45:00Z"})
    assert r.status_code == 400
