"""Strict FP-MATCH-1 verification: reference oracle, exhaustive subsets, invariants, views."""

import itertools
import random
from fractions import Fraction
from math import floor

import pytest

from app.matching import (
    match_score,
    normalize_skills,
    sort_applicants_for_employer,
    sort_jobs_for_candidate,
)

UNIVERSE = ["python", "sql", "rust", "go", "java"]


def oracle(candidate, required):
    """Independent restatement of the contract using exact rational arithmetic."""
    c = {s.strip().lower() for s in candidate if s and s.strip()}
    r = {s.strip().lower() for s in required if s and s.strip()}
    if not r:
        return 100
    return floor(Fraction(100 * len(c & r), len(r)))


def _subsets(items):
    for n in range(len(items) + 1):
        yield from itertools.combinations(items, n)


def test_strict_exhaustive_subsets_match_oracle():
    # 32 x 32 = 1024 (C, R) pairs: every combination of a 5-skill universe.
    count = 0
    for c in _subsets(UNIVERSE):
        for r in _subsets(UNIVERSE):
            got = match_score(list(c), list(r))
            assert got == oracle(c, r), (c, r)
            assert type(got) is int and 0 <= got <= 100
            count += 1
    assert count == 1024


def test_strict_required_examples_literal():
    table = [
        (["Python", "SQL"], ["Python", "SQL"], 100),
        (["Python"], ["Python", "SQL"], 50),
        (["Rust"], ["Python", "SQL"], 0),
        (["Anything"], [], 100),
        ([], [], 100),
    ]
    for c, r, expected in table:
        assert match_score(c, r) == expected


@pytest.mark.parametrize("seed", range(5))
def test_strict_random_noise_invariance(seed):
    """Case, padding, ordering and duplicates never change the score."""
    rng = random.Random(seed)
    for _ in range(200):
        c = rng.sample(UNIVERSE, rng.randint(0, 5))
        r = rng.sample(UNIVERSE, rng.randint(0, 5))
        base = match_score(c, r)
        noisy_c = [f"{' ' * rng.randint(0, 2)}{s.upper() if rng.random() < .5 else s}{' ' * rng.randint(0, 2)}"
                   for s in c for _ in range(rng.randint(1, 3))] + ["", "   "]
        noisy_r = [f"\t{s.title()} " for s in r for _ in range(rng.randint(1, 2))] + [""]
        rng.shuffle(noisy_c)
        rng.shuffle(noisy_r)
        assert match_score(noisy_c, noisy_r) == base == oracle(c, r)


def test_strict_monotonic_and_bounds():
    required = ["python", "sql", "rust", "go"]
    previous = -1
    have = []
    for skill in required:
        have.append(skill)
        score = match_score(have, required)
        assert score > previous  # each newly matched skill strictly raises the score
        previous = score
    assert previous == 100
    # Extra unrelated skills never lower or raise it.
    assert match_score(have + ["cobol", "fortran"], required) == 100
    assert match_score(["cobol"], required) == 0
    # Empty required list: always 100 even for an empty candidate.
    assert match_score([], []) == match_score(["x"], []) == match_score(["", "  "], ["", " "]) == 100
    # Floors: n/7 for every n.
    seven = [f"s{i}" for i in range(7)]
    for n in range(8):
        assert match_score(seven[:n], seven) == (100 * n) // 7


def test_strict_inputs_not_mutated_and_order_independent():
    c = ["B", "a", "A"]
    r = ["a", "b", "c"]
    c_copy, r_copy = list(c), list(r)
    assert match_score(c, r) == match_score(reversed(c), reversed(r)) == 66
    assert c == c_copy and r == r_copy
    assert normalize_skills(c) == ["b", "a"]  # first-seen order, deduplicated


def test_strict_sorting_is_total_and_input_order_independent():
    # Title order deliberately contradicts id order, so id-only sorting would fail.
    rows = [
        {"job_id": 1, "title": "beta", "score": 50},
        {"job_id": 2, "title": "Alpha", "score": 50},
        {"job_id": 3, "title": "alpha", "score": 50},
        {"job_id": 4, "title": "ALPHA", "score": 50},
        {"job_id": 5, "title": "Zed", "score": 80},
        {"job_id": 6, "title": "aaa", "score": 0},
    ]
    expected = [5, 2, 3, 4, 1, 6]
    for perm in itertools.permutations(rows):
        assert [r["job_id"] for r in sort_jobs_for_candidate(perm)] == expected
    people = [
        {"candidate_id": 4, "display_name": "bob", "score": 10},
        {"candidate_id": 2, "display_name": "Bob", "score": 10},
        {"candidate_id": 3, "display_name": "alice", "score": 10},
        {"candidate_id": 1, "display_name": "Zed", "score": 90},
    ]
    for perm in itertools.permutations(people):
        assert [r["candidate_id"] for r in sort_applicants_for_employer(perm)] == [1, 3, 2, 4]
    # Sorting is non-destructive.
    original = list(rows)
    sort_jobs_for_candidate(rows)
    assert rows == original


def test_strict_dashboard_and_applicants_agree_and_ignore_resume(client, app, world):
    from app.services import applications as applications_service
    from app.services import jobs as jobs_service
    from app.services import profiles as profiles_service

    ids = world(include_cara=True)
    backend = ids["jobs"]["backend"]
    with app.app_context():
        for who in ("alice", "bob", "cara"):
            applications_service.apply_to_job(ids["users"][who], backend)
        # Same-score tie to exercise the tie-break through real service paths.
        profiles_service.update_candidate_profile(ids["users"]["cara"], "bob patel", "Python")
        applicants = applications_service.list_applicants(ids["users"]["ana"], backend)
        for row in applicants:
            dash = jobs_service.list_jobs_with_scores(row["candidate_id"])
            dash_score = next(j["score"] for j in dash if j["job_id"] == backend)
            assert dash_score == row["score"]  # one implementation, two views
        assert [r["score"] for r in applicants] == [100, 50, 50]
        # Tie: "Bob Patel" (id bob) vs "bob patel" (id cara) -> same lowercase, lower id first.
        assert [r["candidate_id"] for r in applicants][1:] == sorted(
            [ids["users"]["bob"], ids["users"]["cara"]]
        )
        # Resume text is never an input.
        before = jobs_service.list_jobs_with_scores(ids["users"]["bob"])
        profiles_service.replace_resume(
            ids["users"]["bob"], ids["users"]["bob"], "python sql rust go java everything"
        )
        assert jobs_service.list_jobs_with_scores(ids["users"]["bob"]) == before


def test_strict_skills_are_stored_normalized(app, world):
    from app.db import get_db
    from app.services import jobs as jobs_service
    from app.services import profiles as profiles_service

    ids = world()
    with app.app_context():
        profiles_service.update_candidate_profile(
            ids["users"]["alice"], "Alice Chen", "  Python ,PYTHON\nSQL  ,, Go "
        )
        job_id = jobs_service.create_job(ids["users"]["ana"], "T", "D", " Rust, rust ,SQL")
        db = get_db()
        cand = [r["skill"] for r in db.execute(
            "SELECT skill FROM candidate_skills WHERE user_id = ? ORDER BY skill", (ids["users"]["alice"],))]
        req = [r["skill"] for r in db.execute(
            "SELECT skill FROM job_skills WHERE job_id = ? ORDER BY skill", (job_id,))]
        assert cand == ["go", "python", "sql"]
        assert req == ["rust", "sql"]


def test_strict_tie_breaks_use_casefold_not_lower():
    # casefold folds "ß" to "ss": "Straße" sorts with "strasse", not after "z".
    rows = [
        {"job_id": 1, "title": "Zebra", "score": 10},
        {"job_id": 2, "title": "Straße", "score": 10},
        {"job_id": 3, "title": "Strasse B", "score": 10},
    ]
    assert [r["job_id"] for r in sort_jobs_for_candidate(rows)] == [2, 3, 1]
    people = [
        {"candidate_id": 1, "display_name": "Zoë", "score": 5},
        {"candidate_id": 2, "display_name": "STRASSE", "score": 5},
        {"candidate_id": 3, "display_name": "straße", "score": 5},  # equal after casefold -> id
    ]
    assert [r["candidate_id"] for r in sort_applicants_for_employer(people)] == [2, 3, 1]


def test_strict_list_applicants_never_reads_resume_text(app, world):
    from app.db import get_db
    from app.services import applications as applications_service
    from app.services import profiles as profiles_service

    ids = world()
    with app.app_context():
        applications_service.apply_to_job(ids["users"]["alice"], ids["jobs"]["backend"])
        profiles_service.replace_resume(ids["users"]["alice"], ids["users"]["alice"], "SECRET-RESUME-TEXT")
        statements = []
        get_db().set_trace_callback(statements.append)
        rows = applications_service.list_applicants(ids["users"]["ana"], ids["jobs"]["backend"])
        get_db().set_trace_callback(None)
    assert rows and not any("resume_text" in s for s in statements)
    assert "SECRET-RESUME-TEXT" not in repr(rows)
