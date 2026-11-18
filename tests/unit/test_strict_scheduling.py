"""Strict FP-SCHED-1..3 verification: boundaries, atomic failures, multi-thread races."""

import threading
from datetime import timedelta

import pytest

from app.db import get_db
from app.errors import AuthorizationError, ConflictError, ValidationError
from app.services import applications as applications_service
from app.services import auth as auth_service
from app.services import scheduling as scheduling_service
from app.timeutils import plus_minutes, to_iso, utc_now
from tests.conftest import make_future_slot_start, make_slot_end


def _snapshot(db):
    return (
        [tuple(r) for r in db.execute("SELECT * FROM bookings ORDER BY id")],
        [tuple(r) for r in db.execute("SELECT * FROM interview_slots ORDER BY id")],
        [tuple(r) for r in db.execute("SELECT * FROM applications ORDER BY id")],
    )


def _interviewing(app, ids, who, job="backend", owner="ana"):
    with app.app_context():
        app_id = applications_service.apply_to_job(ids["users"][who], ids["jobs"][job])
        applications_service.set_status(ids["users"][owner], app_id, "Interviewing")
    return app_id


def test_strict_slot_duration_and_time_boundaries(app, world):
    ids = world()
    ana, backend = ids["users"]["ana"], ids["jobs"]["backend"]
    start = make_future_slot_start()
    with app.app_context():
        for minutes in (-30, -1, 0, 1, 15, 29, 31, 45, 60, 1440):
            with pytest.raises(ValidationError):
                scheduling_service.create_slot(ana, backend, start, plus_minutes(start, minutes))
        # Seconds-level drift is also rejected (29:59 / 30:01).
        with pytest.raises(ValidationError):
            scheduling_service.create_slot(ana, backend, start, plus_minutes(start, 30)[:-3] + "01Z")
        # Start already elapsed by a second -> rejected; a minute ahead -> accepted.
        just_past = to_iso(utc_now() - timedelta(seconds=1))
        with pytest.raises(ValidationError):
            scheduling_service.create_slot(ana, backend, just_past, plus_minutes(just_past, 30))
        soon = to_iso(utc_now() + timedelta(minutes=1))
        assert scheduling_service.create_slot(ana, backend, soon, plus_minutes(soon, 30))
        # Rejected creations never write rows (only the one valid slot exists).
        assert get_db().execute("SELECT COUNT(*) n FROM interview_slots").fetchone()["n"] == 1
        # Malformed inputs.
        for bad in ("", "tomorrow", "2026-13-40T99:00:00Z", None):
            with pytest.raises(ValidationError):
                scheduling_service.create_slot(ana, backend, bad, start)


def test_strict_offsets_are_normalized_to_utc(app, world):
    ids = world()
    ana, backend = ids["users"]["ana"], ids["jobs"]["backend"]
    start = make_future_slot_start()  # e.g. 2026-10-12T15:00:00Z
    local = start[:-1] + "+08:00"  # same wall clock, 8h earlier in UTC
    local_end = plus_minutes(start, 30)[:-1] + "+08:00"
    with app.app_context():
        slot_id = scheduling_service.create_slot(ana, backend, local, local_end)
        row = get_db().execute("SELECT * FROM interview_slots WHERE id = ?", (slot_id,)).fetchone()
        assert row["start_utc"].endswith("Z") and row["end_utc"].endswith("Z")
        assert row["start_utc"] == to_iso(__import__("app.timeutils", fromlist=["parse_iso"]).parse_iso(local))
        assert row["start_utc"] != start


def test_strict_failed_bookings_change_nothing(app, world):
    ids = world(include_cara=True)
    ana, ben = ids["users"]["ana"], ids["users"]["ben"]
    alice, bob, cara = (ids["users"][k] for k in ("alice", "bob", "cara"))
    start = make_future_slot_start()
    with app.app_context():
        slot = scheduling_service.create_slot(ana, ids["jobs"]["backend"], start, make_slot_end(start))
        foreign_slot = scheduling_service.create_slot(ben, ids["jobs"]["analyst"], start, make_slot_end(start))
        db = get_db()
        alice_app = applications_service.apply_to_job(alice, ids["jobs"]["backend"])  # Pending
        bob_app = applications_service.apply_to_job(bob, ids["jobs"]["backend"])
        applications_service.set_status(ana, bob_app, "Interviewing")
        cara_app = applications_service.apply_to_job(cara, ids["jobs"]["backend"])
        applications_service.set_status(ana, cara_app, "Interviewing")
        before = _snapshot(db)
        attempts = [
            (alice, alice_app, slot, ConflictError),         # Pending
            (bob, alice_app, slot, AuthorizationError),      # someone else's application
            (bob, bob_app, foreign_slot, AuthorizationError),  # other employer's slot
        ]
        for who, application, slot_id, err in attempts:
            with pytest.raises(err):
                scheduling_service.book_slot(who, application, slot_id)
            assert _snapshot(db) == before
        # Rejected / Accepted / Pending are all ineligible; only Interviewing books.
        for status in ("Pending", "Rejected", "Accepted"):
            applications_service.set_status(ana, bob_app, status)
            snap = _snapshot(db)
            with pytest.raises(ConflictError):
                scheduling_service.book_slot(bob, bob_app, slot)
            assert _snapshot(db) == snap
        applications_service.set_status(ana, bob_app, "Interviewing")
        assert scheduling_service.book_slot(bob, bob_app, slot)
        # A booked application cannot take a second slot; the first stays intact.
        second = scheduling_service.create_slot(
            ana, ids["jobs"]["backend"], plus_minutes(start, 60), plus_minutes(start, 90)
        )
        snap = _snapshot(db)
        with pytest.raises(ConflictError) as exc:
            scheduling_service.book_slot(bob, bob_app, second)
        assert exc.value.message == "Application already has a booking"
        assert _snapshot(db) == snap
        # A booked slot rejects another candidate with the contract message.
        with pytest.raises(ConflictError) as exc:
            scheduling_service.book_slot(cara, cara_app, slot)
        assert (exc.value.status, exc.value.message) == (409, "Slot already booked")
        assert _snapshot(db) == snap


def _make_candidates(app, ids, n):
    ids = dict(ids)
    apps = []
    with app.app_context():
        for i in range(n):
            uid = auth_service.register_user(
                f"racer{i}@test.local", "Password123!", "Candidate", display_name=f"Racer {i}"
            )
            application = applications_service.apply_to_job(uid, ids["jobs"]["backend"])
            applications_service.set_status(ids["users"]["ana"], application, "Interviewing")
            apps.append((uid, application))
    return apps


def _run_threads(app, targets):
    barrier = threading.Barrier(len(targets))
    results = [None] * len(targets)

    def runner(i, fn):
        with app.app_context():
            barrier.wait()
            try:
                results[i] = ("ok", fn())
            except ConflictError as exc:
                results[i] = ("conflict", exc.status, exc.message)
            except Exception as exc:  # any other outcome is a test failure
                results[i] = ("error", repr(exc))

    threads = [threading.Thread(target=runner, args=(i, fn)) for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.mark.parametrize("rounds", range(3))
def test_strict_eight_way_race_for_one_slot(app, world, rounds):
    ids = world()
    racers = _make_candidates(app, ids, 8)
    start = make_future_slot_start()
    with app.app_context():
        slot_id = scheduling_service.create_slot(
            ids["users"]["ana"], ids["jobs"]["backend"], start, make_slot_end(start)
        )
    results = _run_threads(
        app,
        [lambda u=u, a=a: scheduling_service.book_slot(u, a, slot_id) for u, a in racers],
    )
    assert [r[0] for r in results].count("ok") == 1, results
    losers = [r for r in results if r[0] != "ok"]
    assert len(losers) == 7
    assert all(r == ("conflict", 409, "Slot already booked") for r in losers), losers
    with app.app_context():
        rows = get_db().execute("SELECT * FROM bookings").fetchall()
        assert len(rows) == 1 and rows[0]["slot_id"] == slot_id


def test_strict_one_application_two_slots_race(app, world):
    """A double-click across two slots yields exactly one booking for the application."""
    ids = world()
    (uid, application), = _make_candidates(app, ids, 1)
    start = make_future_slot_start()
    with app.app_context():
        slots = [
            scheduling_service.create_slot(
                ids["users"]["ana"], ids["jobs"]["backend"],
                plus_minutes(start, 60 * i), plus_minutes(start, 60 * i + 30),
            )
            for i in range(4)
        ]
    results = _run_threads(
        app, [lambda s=s: scheduling_service.book_slot(uid, application, s) for s in slots]
    )
    assert [r[0] for r in results].count("ok") == 1, results
    assert all(r[0] in ("ok", "conflict") for r in results), results
    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) n FROM bookings WHERE application_id = ?", (application,)
        ).fetchone()["n"] == 1


def test_strict_booking_visibility_over_http(client, app, world):
    ids = world()
    app_id = _interviewing(app, ids, "alice")
    start = make_future_slot_start()
    with app.app_context():
        slot_id = scheduling_service.create_slot(
            ids["users"]["ana"], ids["jobs"]["backend"], start, make_slot_end(start)
        )
    client.login("alice@test.local", "Password123!")
    page = client.get(f"/applications/{app_id}")
    assert page.status_code == 200 and b"/bookings" in page.data  # booking form offered
    assert client.post("/bookings", {"application_id": app_id, "slot_id": slot_id}).status_code == 302
    detail = client.get(f"/applications/{app_id}")
    assert b"/bookings" not in detail.data  # no second booking form
    assert start[:10].encode() in detail.data
    client.logout()
    client.login("ana@test.local", "Password123!")
    assert start[:10].encode() in client.get(f"/jobs/{ids['jobs']['backend']}/applicants").data
    slots_page = client.get("/employer/slots")
    assert slots_page.status_code == 200
    # The booked slot cannot be deleted over HTTP and still exists.
    assert client.post(f"/slots/{slot_id}/delete").status_code == 409
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) n FROM interview_slots").fetchone()["n"] == 1


def test_strict_slot_state_distinguishes_past_from_available(app, world, client):
    from app.db import transaction

    ids = world()
    ana, backend = ids["users"]["ana"], ids["jobs"]["backend"]
    start = make_future_slot_start()
    with app.app_context():
        future = scheduling_service.create_slot(ana, backend, start, make_slot_end(start))
        past_start = to_iso(utc_now().replace(microsecond=0) - timedelta(days=1))
        with transaction():
            past = get_db().execute(
                "INSERT INTO interview_slots (job_id, employer_id, start_utc, end_utc, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (backend, ana, past_start, plus_minutes(past_start, 30), to_iso(utc_now())),
            ).lastrowid
        states = {s["slot_id"]: s["state"] for s in scheduling_service.list_slots_for_employer(ana)}
        assert states == {future: "available", past: "past"}
        assert [s["slot_id"] for s in scheduling_service.list_available_slots(ana)] == [future]
    client.login("ana@test.local", "Password123!")
    html = client.get("/employer/slots").get_data(as_text=True)
    assert ">expired<" in html and html.count(">available<") == 1


def test_strict_http_rejects_invalid_durations_and_accepts_valid_end(client, app, world):
    ids = world()
    job = ids["jobs"]["backend"]
    start = make_future_slot_start()
    client.login("ana@test.local", "Password123!")
    for end in (start, plus_minutes(start, -30), plus_minutes(start, 25), plus_minutes(start, 45), "garbage"):
        r = client.post("/employer/slots", {"job_id": job, "start_utc": start, "end_utc": end})
        assert r.status_code == 400, end
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) n FROM interview_slots").fetchone()["n"] == 0
    ok = client.post("/employer/slots", {"job_id": job, "start_utc": start, "end_utc": plus_minutes(start, 30)})
    assert ok.status_code == 302
    # Omitting end_utc keeps the form behaviour (start + 30 minutes).
    later = plus_minutes(start, 120)
    assert client.post("/employer/slots", {"job_id": job, "start_utc": later}).status_code == 302
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) n FROM interview_slots").fetchone()["n"] == 2


def test_strict_locked_database_is_503_not_500_and_changes_nothing(client, app, world, monkeypatch):
    import sqlite3

    import app.db as db_module

    ids = world()
    app_id = _interviewing(app, ids, "alice")
    start = make_future_slot_start()
    with app.app_context():
        slot_id = scheduling_service.create_slot(
            ids["users"]["ana"], ids["jobs"]["backend"], start, make_slot_end(start)
        )
        path = app.config["DATABASE"]
    monkeypatch.setattr(db_module, "BUSY_TIMEOUT_SECONDS", 0.2)
    client.login("alice@test.local", "Password123!")
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # another writer holds the lock
    try:
        r = client.post("/bookings", {"application_id": app_id, "slot_id": slot_id})
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert r.status_code == 503 and b"retry" in r.data
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) n FROM bookings").fetchone()["n"] == 0
    # After the lock is released the same request succeeds.
    assert client.post("/bookings", {"application_id": app_id, "slot_id": slot_id}).status_code == 302


def test_strict_resume_access_distinguishes_missing(client, app, world):
    ids = world()
    alice = ids["users"]["alice"]
    client.login("bob@test.local", "Password123!")
    existing = client.get(f"/candidates/{alice}/resume")
    missing = client.get("/candidates/987654/resume")
    assert existing.status_code == 403 and missing.status_code == 404
    assert client.post("/candidates/987654/resume", {"resume_text": "x"}).status_code == 404


def test_strict_http_end_utc_cannot_bypass_duration_validation(client, app, world):
    ids = world()
    job = ids["jobs"]["backend"]
    start = make_future_slot_start()  # ...T15:00:00Z
    base = start[:-1]
    client.login("ana@test.local", "Password123!")

    def post(start_value, end_value):
        return client.post("/employer/slots", {"job_id": job, "start_utc": start_value, "end_utc": end_value})

    # Sub-second drift used to be truncated away by the route and accepted.
    assert post(start, plus_minutes(start, 30)[:-1] + ".5Z").status_code == 400
    assert post(start, plus_minutes(start, 30)[:-1] + ".000001Z").status_code == 400
    assert post(base + ".5Z", plus_minutes(start, 30)).status_code == 400
    assert post(start, plus_minutes(start, 30)[:-3] + "01Z").status_code == 400  # 30:01
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) n FROM interview_slots").fetchone()["n"] == 0
    # Offsets are accepted and stored as UTC; naive means UTC.
    assert post(base + "+00:00", plus_minutes(start, 30)[:-1] + "+00:00").status_code == 302
    local_start = plus_minutes(start, 120)[:-1] + "-05:00"  # = 5h later in UTC
    local_end = plus_minutes(start, 150)[:-1] + "-05:00"
    assert post(local_start, local_end).status_code == 302
    assert post(plus_minutes(start, 300)[:-1], plus_minutes(start, 330)[:-1]).status_code == 302  # naive
    with app.app_context():
        rows = [r["start_utc"] for r in get_db().execute("SELECT start_utc FROM interview_slots ORDER BY id")]
    assert rows[0] == start and all(r.endswith("Z") for r in rows)
    assert rows[1] == plus_minutes(start, 120 + 300)  # -05:00 wall clock + 5h


def test_strict_equal_fractional_seconds_are_rejected(client, app, world):
    """start and end both carry .5s: duration is exactly 30 min, yet sub-second input is refused."""
    ids = world()
    start = make_future_slot_start()
    client.login("ana@test.local", "Password123!")
    r = client.post("/employer/slots", {
        "job_id": ids["jobs"]["backend"],
        "start_utc": start[:-1] + ".5Z",
        "end_utc": plus_minutes(start, 30)[:-1] + ".5Z",
    })
    assert r.status_code == 400
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) n FROM interview_slots").fetchone()["n"] == 0
        with pytest.raises(ValidationError):
            scheduling_service.create_slot(
                ids["users"]["ana"], ids["jobs"]["backend"],
                start[:-1] + ".250Z", plus_minutes(start, 30)[:-1] + ".250Z",
            )


@pytest.mark.parametrize("bad", [
    "2099-01-01",                      # date only
    "2099-01-01 10:00:00",             # space separator
    "20990101T100000Z",                # compact form
    "2099-W01-1",                      # week date
    "2099-01-01T10",                   # hour only
    "2099-01-01T10:00:00+0500",        # offset without colon
    "2099-01-01T10:00:00ZZ",
    " ", "garbage", "2099-01-01T25:00:00Z",
])
def test_strict_slot_time_format_is_strict(app, world, bad):
    ids = world()
    with app.app_context():
        with pytest.raises(ValidationError):
            scheduling_service.create_slot(ids["users"]["ana"], ids["jobs"]["backend"], bad, "2099-01-01T10:30:00Z")
        with pytest.raises(ValidationError):
            scheduling_service.create_slot(ids["users"]["ana"], ids["jobs"]["backend"], "2099-01-01T10:00:00Z", bad)
        assert get_db().execute("SELECT COUNT(*) n FROM interview_slots").fetchone()["n"] == 0


def test_strict_slot_time_accepted_forms_are_normalized_to_utc(app, world):
    ids = world()
    ana, job = ids["users"]["ana"], ids["jobs"]["backend"]
    forms = [  # one distinct day per form so none collides with another
        ("2099-01-01T10:00Z", "2099-01-01T10:30Z", "2099-01-01T10:00:00Z"),
        ("2099-01-02T10:00:00", "2099-01-02T10:30:00", "2099-01-02T10:00:00Z"),       # naive = UTC
        ("2099-01-03T10:00:37Z", "2099-01-03T10:30:37Z", "2099-01-03T10:00:37Z"),     # seconds ok
        ("2099-01-04T10:00:00+02:00", "2099-01-04T10:30:00+02:00", "2099-01-04T08:00:00Z"),
        ("2099-01-05T10:00:00-05:00", "2099-01-05T10:30:00-05:00", "2099-01-05T15:00:00Z"),
        # start and end may use different offsets as long as the instants are 30 minutes apart
        ("2099-01-06T10:00:00Z", "2099-01-06T05:30:00-05:00", "2099-01-06T10:00:00Z"),
    ]
    with app.app_context():
        for start, end, expected in forms:
            slot_id = scheduling_service.create_slot(ana, job, start, end)
            row = get_db().execute("SELECT start_utc FROM interview_slots WHERE id = ?", (slot_id,)).fetchone()
            assert row["start_utc"] == expected, (start, row["start_utc"])


def test_strict_identical_slot_is_rejected_but_distinct_ones_are_not(client, app, world):
    ids = world()
    ana, ben = ids["users"]["ana"], ids["users"]["ben"]
    backend, analyst = ids["jobs"]["backend"], ids["jobs"]["analyst"]
    start = make_future_slot_start()
    with app.app_context():
        first = scheduling_service.create_slot(ana, backend, start, make_slot_end(start))
        with pytest.raises(ConflictError) as exc:
            scheduling_service.create_slot(ana, backend, start, make_slot_end(start))
        assert exc.value.status == 409
        # Same instant written with a different offset is the same slot.
        with pytest.raises(ConflictError):
            scheduling_service.create_slot(ana, backend, start[:-1] + "+00:00", make_slot_end(start)[:-1] + "+00:00")
        # Allowed: another employer at the same time, a shifted slot, an overlapping-but-different one.
        assert scheduling_service.create_slot(ben, analyst, start, make_slot_end(start))
        assert scheduling_service.create_slot(ana, backend, plus_minutes(start, 30), plus_minutes(start, 60))
        assert scheduling_service.create_slot(ana, backend, plus_minutes(start, 15), plus_minutes(start, 45))
        # Deleting frees the identical slot again.
        scheduling_service.delete_slot(ana, first)
        assert scheduling_service.create_slot(ana, backend, start, make_slot_end(start))
        count = get_db().execute("SELECT COUNT(*) n FROM interview_slots WHERE employer_id = ?", (ana,)).fetchone()["n"]
        assert count == 3
    client.login("ana@test.local", "Password123!")
    dup = client.post("/employer/slots", {"job_id": backend, "start_utc": start})
    assert dup.status_code == 409 and b"identical slot" in dup.data


def test_strict_concurrent_identical_slot_creation_yields_one_row(app, world):
    ids = world()
    ana, job = ids["users"]["ana"], ids["jobs"]["backend"]
    start = make_future_slot_start()
    results = _run_threads(
        app,
        [lambda: scheduling_service.create_slot(ana, job, start, make_slot_end(start)) for _ in range(6)],
    )
    assert [r[0] for r in results].count("ok") == 1, results
    assert all(r[0] in ("ok", "conflict") for r in results), results
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) n FROM interview_slots").fetchone()["n"] == 1
