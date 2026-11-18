"""Strict FP-SEC-1..4 verification: authorization matrix, SQLi, XSS, static scans."""

import ast
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.db import get_db
from app.services import applications as applications_service
from app.services import scheduling as scheduling_service
from tests.conftest import FormClient, make_future_slot_start, make_slot_end

ROOT = Path(__file__).resolve().parents[2]
PW = "Password123!"


def _as(app, who):
    c = FormClient(app.test_client())
    if who:
        assert c.login(f"{who}@test.local", PW).status_code == 302
    return c


@pytest.fixture()
def scene(app, world):
    ids = world(include_cara=True)
    start = make_future_slot_start()
    with app.app_context():
        alice_app = applications_service.apply_to_job(ids["users"]["alice"], ids["jobs"]["backend"])
        slot = scheduling_service.create_slot(
            ids["users"]["ana"], ids["jobs"]["backend"], start, make_slot_end(start)
        )
    ids["alice_app"], ids["slot"] = alice_app, slot
    return ids


def test_strict_get_authorization_matrix(app, scene):
    ids = scene
    alice, bob = ids["users"]["alice"], ids["users"]["bob"]
    backend, aapp = ids["jobs"]["backend"], ids["alice_app"]
    # (url, {actor: expected_status}); None = anonymous (GET -> login redirect).
    matrix = {
        f"/candidates/{alice}/resume": {None: 302, "alice": 200, "bob": 403, "ana": 403, "ben": 403},
        f"/candidates/{bob}/resume": {"alice": 403, "bob": 200, "ana": 403},
        "/candidates/987654/resume": {"alice": 404, "ana": 403},
        f"/applications/{aapp}": {None: 302, "alice": 200, "bob": 403, "cara": 403, "ana": 200, "ben": 403},
        "/applications/987654": {"alice": 404, "ana": 404},
        f"/jobs/{backend}/edit": {None: 302, "ana": 200, "ben": 403, "alice": 403},
        f"/jobs/{backend}/applicants": {None: 302, "ana": 200, "ben": 403, "alice": 403},
        "/jobs/987654/edit": {"ana": 404},
        "/jobs/987654/applicants": {"ana": 404},
        "/dashboard": {None: 302, "alice": 200, "ana": 403},
        "/profile": {None: 302, "alice": 200, "ana": 403},
        "/applications": {None: 302, "alice": 200, "ana": 403},
        "/company": {None: 302, "ana": 200, "alice": 403},
        "/employer/jobs": {None: 302, "ana": 200, "alice": 403},
        "/employer/slots": {None: 302, "ana": 200, "bob": 403},
    }
    for url, cases in matrix.items():
        for actor, expected in cases.items():
            r = _as(app, actor).get(url)
            assert r.status_code == expected, (url, actor, r.status_code)
            if expected == 302:
                assert "/login" in r.headers["Location"]


def test_strict_post_authorization_matrix_and_no_side_effects(app, scene):
    ids = scene
    backend, aapp, slot = ids["jobs"]["backend"], ids["alice_app"], ids["slot"]
    alice = ids["users"]["alice"]
    with app.app_context():
        applications_service.set_status(ids["users"]["ana"], aapp, "Interviewing")

    def state():
        with app.app_context():
            db = get_db()
            return (
                db.execute("SELECT status FROM applications WHERE id = ?", (aapp,)).fetchone()["status"],
                db.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"],
                db.execute("SELECT COUNT(*) n FROM interview_slots").fetchone()["n"],
                db.execute("SELECT COUNT(*) n FROM bookings").fetchone()["n"],
                db.execute("SELECT resume_text FROM candidate_profiles WHERE user_id = ?", (alice,)).fetchone()[0],
                db.execute("SELECT title FROM jobs WHERE id = ?", (backend,)).fetchone()["title"],
            )

    before = state()
    forbidden = [
        # (actor, url, data, expected)
        ("ben", f"/applications/{aapp}/status", {"status": "Accepted", "job_id": backend}, 403),
        ("alice", f"/applications/{aapp}/status", {"status": "Accepted", "job_id": backend}, 403),
        ("bob", f"/applications/{aapp}/status", {"status": "Accepted", "job_id": backend}, 403),
        ("ben", f"/jobs/{backend}/delete", {}, 403),
        ("alice", f"/jobs/{backend}/delete", {}, 403),
        ("ben", f"/jobs/{backend}/edit", {"title": "X", "description": "X", "skills": ""}, 403),
        ("ben", f"/slots/{slot}/delete", {}, 403),
        ("alice", f"/slots/{slot}/delete", {}, 403),
        ("bob", "/bookings", {"application_id": aapp, "slot_id": slot}, 403),
        ("cara", "/bookings", {"application_id": aapp, "slot_id": slot}, 403),
        ("ana", "/bookings", {"application_id": aapp, "slot_id": slot}, 403),
        ("bob", f"/candidates/{alice}/resume", {"resume_text": "pwned"}, 403),
        ("ana", f"/candidates/{alice}/resume", {"resume_text": "pwned"}, 403),
        ("ben", "/employer/slots", {"job_id": backend, "start_utc": make_future_slot_start()}, 403),
        ("alice", f"/jobs/{ids['jobs']['analyst']}/apply", {}, 302),  # allowed: baseline sanity
    ]
    for actor, url, data, expected in forbidden:
        r = _as(app, actor).post(url, data)
        assert r.status_code == expected, (actor, url, r.status_code)
        if expected == 403:
            assert state() == before, (actor, url)
    # Anonymous POSTs (through a client that holds a CSRF token) are 401.
    anon = FormClient(app.test_client())
    for url, data in ((f"/jobs/{backend}/apply", {}), ("/bookings", {"application_id": aapp, "slot_id": slot}),
                      (f"/slots/{slot}/delete", {}), (f"/jobs/{backend}/delete", {})):
        assert anon.post(url, data).status_code == 401, url
    assert state() == before


def test_strict_missing_objects_are_404_not_403_or_500(app, scene):
    ana = _as(app, "ana")
    alice = _as(app, "alice")
    assert ana.post("/slots/987654/delete").status_code == 404
    assert ana.post("/jobs/987654/delete").status_code == 404
    assert alice.post("/jobs/987654/apply").status_code == 404
    assert alice.post("/bookings", {"application_id": 987654, "slot_id": 1}).status_code == 404
    assert alice.post("/bookings", {"application_id": scene["alice_app"], "slot_id": 987654}).status_code == 404
    assert alice.post("/bookings", {"application_id": "abc", "slot_id": ""}).status_code == 400


def test_strict_csrf_required_and_no_state_change(app, scene):
    c = FormClient(app.test_client())
    c.login("alice@test.local", PW)
    raw = c._client  # bypass the CSRF-injecting wrapper
    with app.app_context():
        before = get_db().execute("SELECT COUNT(*) n FROM applications").fetchone()["n"]
    r = raw.post(f"/jobs/{scene['jobs']['analyst']}/apply", data={})
    assert r.status_code == 403
    r = raw.post(f"/jobs/{scene['jobs']['analyst']}/apply", data={"csrf_token": "wrong"})
    assert r.status_code == 403
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) n FROM applications").fetchone()["n"] == before


SQLI = [
    "' OR '1'='1", "'; DROP TABLE users; --", "\" OR \"\"=\"", "1; DELETE FROM jobs",
    "Robert'); DROP TABLE jobs;--", "' UNION SELECT password_hash FROM users --", "%' OR 1=1 --",
]


@pytest.mark.parametrize("payload", SQLI)
def test_strict_sql_metacharacters_everywhere(app, scene, payload):
    ana, alice = _as(app, "ana"), _as(app, "alice")
    # Login with injection in both fields never authenticates.
    assert FormClient(app.test_client()).login(payload, payload).status_code == 401
    assert FormClient(app.test_client()).login("alice@test.local' --", PW).status_code == 401
    # Stored through every write path as inert data.
    assert ana.post("/employer/jobs/new", {"title": payload, "description": payload, "skills": payload}).status_code == 302
    assert ana.post("/company", {"company_name": payload, "description": payload}).status_code == 302
    assert alice.post("/profile", {"display_name": payload, "skills": payload}).status_code == 302
    assert alice.post(f"/candidates/{scene['users']['alice']}/resume", {"resume_text": payload}).status_code == 302
    reg = FormClient(app.test_client()).register(f"{abs(hash(payload))}@x.test", PW, "Candidate", display_name=payload)
    assert reg.status_code == 302
    with app.app_context():
        db = get_db()
        # Every table is intact and the payloads are stored verbatim (bound values).
        for table in ("users", "jobs", "applications", "interview_slots", "candidate_profiles", "company_profiles"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE title = ?", (payload,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM candidate_profiles WHERE resume_text = ?", (payload,)).fetchone()[0] == 1
    # Pages that render the payload still work (200) and the dashboard lists it.
    assert alice.get("/dashboard").status_code == 200


def test_strict_static_all_sql_is_constant_text():
    """Every .execute()/.executemany() first argument is a literal (no f-strings/concat/format).

    The documented exception is reset_db's script over a hard-coded table list.
    """
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("execute", "executemany", "executescript") and node.args):
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    continue
                if isinstance(first, ast.Name) and first.id in ("SCHEMA", "SESSION_GENERATION_DDL"):
                    continue
                if (isinstance(first, ast.IfExp) and isinstance(first.body, ast.Constant)
                        and isinstance(first.orelse, ast.Constant)):
                    continue  # e.g. "BEGIN IMMEDIATE" if immediate else "BEGIN"
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert len(offenders) == 1 and offenders[0].startswith("app/db.py:"), offenders  # only reset_db's DROP
    db_src = (ROOT / "app/db.py").read_text()
    assert 'f"DROP TABLE IF EXISTS {table};"' in db_src
    assert 'tables = [' in db_src  # constant list, never user input


XSS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "' onmouseover='alert(1)",
    '" autofocus onfocus="alert(1)',
    "<svg/onload=alert(1)>",
    "javascript:alert(1)",
]


@pytest.mark.parametrize("payload", XSS)
def test_strict_xss_every_user_controlled_field(app, scene, payload):
    ids = scene
    ana, alice = _as(app, "ana"), _as(app, "alice")
    assert alice.post("/profile", {"display_name": payload, "skills": payload}).status_code == 302
    alice.post(f"/candidates/{ids['users']['alice']}/resume", {"resume_text": payload})
    ana.post("/company", {"company_name": payload, "description": payload})
    ana.post("/employer/jobs/new", {"title": payload, "description": payload, "skills": payload})
    with app.app_context():
        new_job = get_db().execute("SELECT MAX(id) m FROM jobs").fetchone()["m"]
        applications_service.apply_to_job(ids["users"]["alice"], new_job)
    pages = [
        (alice, "/dashboard"), (alice, "/profile"), (alice, "/applications"),
        (alice, f"/candidates/{ids['users']['alice']}/resume"), (alice, f"/jobs/{new_job}"),
        (ana, "/company"), (ana, "/employer/jobs"), (ana, "/employer/slots"),
        (ana, f"/jobs/{new_job}/applicants"), (ana, f"/jobs/{new_job}/edit"),
    ]
    for client, url in pages:
        r = client.get(url)
        assert r.status_code == 200, url
        html = r.get_data(as_text=True)
        # Raw payload must never appear unescaped in markup.
        assert payload not in html or payload == "javascript:alert(1)", (url, payload)
        # Parse like a browser: no injected tags and no event-handler/autofocus attributes.
        tags, attrs = _parse(html)
        assert not tags & {"svg", "img", "iframe", "object"}, (url, tags)
        collector = _Collector()
        collector.feed(html)
        # The only script on any page is our own same-origin file; nothing injected.
        assert collector.scripts == ["/static/confirm.js"], (url, collector.scripts)
        assert not attrs & {"onerror", "onload", "onmouseover", "onfocus", "autofocus"}, (url, attrs)
        # javascript: only ever appears as escaped text, never as a link/form target.
        assert "javascript:" not in _link_targets(html), url


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.targets, self.scripts = set(), set(), [], []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        if tag == "script":
            self.scripts.append(dict(attrs).get("src"))  # None => inline script
        for name, value in attrs:
            self.attrs.add(name)
            if name in ("href", "src", "action") and value:
                self.targets.append(value)


def _parse(html):
    c = _Collector()
    c.feed(html)
    return c.tags, c.attrs


def _link_targets(html):
    c = _Collector()
    c.feed(html)
    return " ".join(t for t in c.targets if t.lower().startswith("javascript:")) or ""


def test_strict_templates_never_disable_escaping():
    for path in (ROOT / "app/templates").rglob("*.html"):
        text = path.read_text()
        assert "|safe" not in text.replace(" ", ""), path
        assert "autoescape false" not in text.replace("  ", " "), path
        assert "Markup(" not in text, path
    for path in (ROOT / "app").rglob("*.py"):
        assert "Markup(" not in path.read_text() or path.name == "security.py", path


def test_strict_secrets_never_exposed_and_hash_is_salted(app, scene):
    c = _as(app, "alice")
    with app.app_context():
        h = get_db().execute("SELECT password_hash FROM users WHERE email = 'alice@test.local'").fetchone()[0]
        h2 = get_db().execute("SELECT password_hash FROM users WHERE email = 'bob@test.local'").fetchone()[0]
    assert PW not in h and h != h2 and (h.startswith("scrypt:") or h.startswith("pbkdf2:"))
    for url in ("/dashboard", "/profile", "/applications", f"/applications/{scene['alice_app']}", "/login"):
        body = c.get(url).get_data(as_text=True)
        assert h not in body and "password_hash" not in body and PW not in body, url
    cookie = [v for k, v in c._client.get("/dashboard").headers if k == "Set-Cookie"]
    assert all("HttpOnly" in v and "SameSite=Lax" in v for v in cookie) or cookie == []


def test_strict_cookie_flags_present(app):
    c = app.test_client()
    r = c.get("/login")
    cookies = r.headers.getlist("Set-Cookie")
    assert cookies and all("HttpOnly" in v and "SameSite=Lax" in v for v in cookies)


def test_strict_repository_has_no_real_secrets():
    tracked = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    patterns = [r"sk-ant-[A-Za-z0-9_-]{10,}", r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                r"ghp_[A-Za-z0-9]{30,}", r"xox[bap]-[0-9A-Za-z-]{10,}"]
    for name in tracked:
        p = ROOT / name
        if not p.is_file() or p.suffix in (".png", ".ico", ".sqlite3", ".DS_Store") or p.name == ".DS_Store":
            continue
        if name.endswith("test_strict_security.py"):
            continue  # this file contains the patterns themselves
        text = p.read_text(errors="ignore")
        for pat in patterns:
            assert not re.search(pat, text), (name, pat)
    env_example = (ROOT / ".env.example").read_text()
    for line in env_example.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            raise AssertionError(f"active assignment in .env.example: {line}")
        if "KEY=" in line:
            assert "replace-with" in line  # placeholder only
    assert not (ROOT / ".env").exists() or ".env" in (ROOT / ".gitignore").read_text()


def test_strict_tests_use_isolated_databases(app, tmp_path):
    from flask import current_app

    with app.app_context():
        path = Path(current_app.config["DATABASE"])
    assert str(path).startswith(str(tmp_path)) and path.name == "test.sqlite3"
    assert app.config["LLM_API_KEY"] is None and app.config["LLM_CLIENT"] is None
    assert path != ROOT / "data" / "app.sqlite3"


def test_strict_security_headers_on_every_response_type(app, scene):
    c = _as(app, "alice")
    responses = [
        c.get("/dashboard"), c.get("/jobs/987654"),            # 200 / 404 page
        app.test_client().get("/login"), app.test_client().get("/nope"),
        _as(app, "ben").get(f"/jobs/{scene['jobs']['backend']}/edit"),  # 403 page
        c.get("/dashboard", method="HEAD") if False else c.get("/applications"),
    ]
    for r in responses:
        h = r.headers
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["X-Frame-Options"] == "DENY"
        assert h["Referrer-Policy"] == "same-origin"
        assert "camera=()" in h["Permissions-Policy"]
        csp = h["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "script-src 'self' 'unsafe" not in csp and "frame-ancestors 'none'" in csp and "default-src 'none'" in csp
        assert "'unsafe-inline'" not in csp.split("style-src")[0]  # inline allowed for styles only
    assert c.get("/dashboard").headers["Cache-Control"] == "no-store"
    assert app.test_client().get("/login").headers["Cache-Control"] == "no-store"


def test_strict_no_inline_scripts_or_handlers_in_templates(client, app, world):
    """CSP allows only same-origin scripts, so templates may not rely on inline ones."""
    for path in (ROOT / "app/templates").rglob("*.html"):
        text = path.read_text().lower()
        assert not re.search(r"<script(?![^>]*\ssrc=)", text), path  # only <script src=...>
        assert not re.search(r"\son[a-z]+\s*=", text), path
    # The one external script is served same-origin and keeps delete confirmation working.
    ids = world()
    client.login("ana@test.local", "Password123!")
    page = client.get("/employer/jobs").get_data(as_text=True)
    assert 'data-confirm="Delete this job posting?"' in page
    assert 'src="/static/confirm.js"' in page
    js = client.get("/static/confirm.js")
    assert js.status_code == 200 and b"data-confirm" in js.data
    assert js.headers["Content-Security-Policy"].startswith("default-src 'none'")


def test_strict_secure_cookie_follows_transport(app):
    plain = app.test_client().get("/login")
    assert all("Secure" not in c for c in plain.headers.getlist("Set-Cookie"))
    assert "Strict-Transport-Security" not in plain.headers
    https = app.test_client().get("/login", base_url="https://localhost")
    cookies = https.headers.getlist("Set-Cookie")
    assert cookies and all("Secure" in c and "HttpOnly" in c and "SameSite=Lax" in c for c in cookies)
    assert "max-age" in https.headers["Strict-Transport-Security"]
    # Explicit configuration wins in both directions.
    app.config["SESSION_COOKIE_SECURE"] = True
    assert all("Secure" in c for c in app.test_client().get("/login").headers.getlist("Set-Cookie"))
    app.config["SESSION_COOKIE_SECURE"] = False
    assert all("Secure" not in c for c in app.test_client().get(
        "/login", base_url="https://localhost").headers.getlist("Set-Cookie"))


def test_strict_login_rate_limit(app, scene):
    def attempt(email, password, c):
        return c.login(email, password)

    c = FormClient(app.test_client())
    for _ in range(5):
        assert attempt("alice@test.local", "wrong-password", c).status_code == 401
    blocked = attempt("alice@test.local", "wrong-password", c)
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    # Even the right password is refused while locked (no oracle during lockout).
    assert attempt("alice@test.local", PW, c).status_code == 429
    # Other accounts from the same client are unaffected, and case does not dodge the lock.
    assert attempt("ALICE@test.local", PW, c).status_code == 429
    assert attempt("bob@test.local", PW, c).status_code == 302


def test_strict_login_success_resets_failure_counter(app, scene):
    c = FormClient(app.test_client())
    for _ in range(4):
        assert c.login("alice@test.local", "nope").status_code == 401
    assert c.login("alice@test.local", PW).status_code == 302  # success resets
    c.logout()
    for _ in range(4):
        assert c.login("alice@test.local", "nope").status_code == 401  # budget restored
    assert c.login("alice@test.local", PW).status_code == 302


def test_strict_ip_wide_failure_limit_and_register_limit(app, scene):
    app.config["LOGIN_IP_FAILURE_LIMIT"] = 6
    c = FormClient(app.test_client())
    for i in range(6):
        assert c.login(f"ghost{i}@test.local", "x").status_code == 401  # spraying many emails
    assert c.login("ghost99@test.local", "x").status_code == 429
    app.config["REGISTER_LIMIT"] = 3
    for i in range(3):
        assert c.register(f"new{i}@test.local", PW, "Candidate", display_name="N").status_code == 302
        c = FormClient(app.test_client())  # fresh browser, same IP
    assert c.register("new9@test.local", PW, "Candidate", display_name="N").status_code == 429


def test_strict_rate_limiter_window_and_disable(app, scene):
    from app.security import RateLimiter

    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    for _ in range(3):
        limiter.hit("k")
    assert limiter.blocked("k", 3, 60) >= 1
    assert limiter.blocked("other", 3, 60) == 0
    now[0] = 61.0  # window slides: old events expire
    assert limiter.blocked("k", 3, 60) == 0
    app.config["RATE_LIMIT_ENABLED"] = False
    c = FormClient(app.test_client())
    for _ in range(12):
        assert c.login("alice@test.local", "bad").status_code == 401


def test_strict_rate_limiter_memory_is_bounded_and_empty_limiter_is_usable():
    from app.security import RateLimiter

    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert limiter is not None and limiter.size() == 0
    assert limiter.blocked("never-seen", 1, 60) == 0
    assert limiter.size() == 0  # reading must not create state
    for i in range(RateLimiter.MAX_KEYS + 500):  # a flood of unique emails
        limiter.hit(("login", "1.2.3.4", f"user{i}@x.test"), 900)
    assert limiter.size() <= RateLimiter.MAX_KEYS
    now[0] = 10_000.0  # everything has expired; the periodic sweep reclaims it
    for i in range(RateLimiter.SWEEP_EVERY):
        limiter.hit(("fresh", i), 900)
    assert limiter.size() <= RateLimiter.SWEEP_EVERY


def test_strict_login_limiter_is_off_in_docker_and_honours_proxy_headers(app, scene):
    from app import create_app

    # The image disables the IP-keyed limits (shared gateway IP) via an env var.
    assert "RATE_LIMIT_ENABLED=0" in (ROOT / "Dockerfile").read_text()
    app.config["RATE_LIMIT_ENABLED"] = False
    c = FormClient(app.test_client())
    for _ in range(40):
        assert c.login("alice@test.local", "nope").status_code == 401  # nobody is locked out
    assert c.login("alice@test.local", PW).status_code == 302

    # Behind a trusted proxy, clients are told apart by X-Forwarded-For.
    proxied = create_app({"TESTING": True, "DATABASE": app.config["DATABASE"], "SECRET_KEY": "k",
                          "TRUSTED_PROXY_COUNT": 1, "LLM_API_KEY": None})
    attacker = FormClient(proxied.test_client())
    victim = FormClient(proxied.test_client())
    for _ in range(5):
        assert attacker.post("/login", {"email": "alice@test.local", "password": "no"},
                             environ_overrides={"HTTP_X_FORWARDED_FOR": "203.0.113.9"}).status_code == 401
    assert attacker.post("/login", {"email": "alice@test.local", "password": "no"},
                         environ_overrides={"HTTP_X_FORWARDED_FOR": "203.0.113.9"}).status_code == 429
    # A different real client is not locked by the attacker's failures.
    assert victim.post("/login", {"email": "alice@test.local", "password": PW},
                       environ_overrides={"HTTP_X_FORWARDED_FOR": "198.51.100.7"}).status_code == 302


def test_strict_server_threads_exceed_busy_timeout_pressure():
    import app.db as db_module

    assert db_module.BUSY_TIMEOUT_SECONDS <= 5  # a lock wait holds a worker thread
    for name in ("manage.py", "run.py"):
        assert "threads=16" in (ROOT / name).read_text(), name
