"""Failure contract: every failure class carries core's canonical
``error_code`` (and ``retry_after`` for rate limits); the host's
``config["timeout_seconds"]`` bounds a whole call; concurrent calls share no
HTTP session."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
import requests
from aisquare.pipe import ERROR_CODES

from aisquare_pipe_github import GitHubToolkit, rest
from aisquare_pipe_github.rest import GitHubAPIError, GitHubRestClient
from aisquare_pipe_github.toolkit import error_code, time_budget

from .fakes import CONFIG, TOKEN, FakeSession, harness, response

NOW = int(time.time())
REPO = {"owner": "octocat", "repo": "Hello-World"}
GET = ("GITHUB_GET_A_REPOSITORY", REPO)
POST = ("GITHUB_CREATE_AN_ISSUE", {**REPO, "title": "t"})


def _limited(status: int, retry_after: str, n: int = 1):
    return [response(status, {"message": "secondary rate limit"}, headers={"Retry-After": retry_after}) for _ in range(n)]


# (id, tool call, scripted answers, error_code, retry_after)
FAILURES = [
    ("401", GET, [response(401, {"message": "Bad credentials"})], "unauthorized", None),
    (
        "403 primary limit, reset in 1h",
        GET,
        [
            response(
                403,
                {"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(NOW + 3600)},
            )
        ],
        "rate_limited",
        "about-3600",
    ),
    ("403 Retry-After beyond the cap", GET, _limited(403, "120"), "rate_limited", 120.0),
    ("403 Retry-After, retries exhausted", GET, _limited(403, "5", 3), "rate_limited", 5.0),
    ("429, retries exhausted", GET, _limited(429, "5", 3), "rate_limited", 5.0),
    ("403 forbidden", GET, [response(403, {"message": "Resource not accessible by integration"})], "forbidden", None),
    ("404", GET, [response(404, {"message": "Not Found"})], "not_found", None),
    ("409", POST, [response(409, {"message": "Git Repository is empty."})], "conflict", None),
    (
        "422",
        POST,
        [response(422, {"message": "Validation Failed", "errors": [{"resource": "Issue", "field": "title", "code": "missing_field"}]})],
        "invalid_arguments",
        None,
    ),
    ("500", GET, [response(500, {"message": "boom"})], "upstream_error", None),
    ("502 x3 on a GET", GET, [response(502, content=b"")] * 3, "upstream_error", None),
    ("503 on a POST", POST, [response(503, {"message": "unavailable"})], "upstream_error", None),
    ("504", POST, [response(504, content=b"")], "timeout", None),
    ("ReadTimeout", GET, [requests.ReadTimeout("read timed out")], "timeout", None),
    ("ConnectTimeout", GET, [requests.ConnectTimeout("connect timed out")], "timeout", None),
    ("ConnectionError", GET, [requests.ConnectionError("refused")], "upstream_error", None),
]


@pytest.mark.parametrize(
    ("call", "answers", "code", "retry_after"),
    [f[1:] for f in FAILURES],
    ids=[f[0] for f in FAILURES],
)
def test_error_code_per_failure_class(call, answers, code, retry_after):
    slug, args = call
    h = harness(*answers)
    result = h.call(slug, args)
    assert result.successful is False
    assert result.error_code == code
    assert result.error_code in ERROR_CODES
    if retry_after == "about-3600":
        assert 3590 <= result.retry_after <= 3610
    else:
        assert result.retry_after == retry_after
    assert TOKEN not in result.error
    assert result.to_dict()["error_code"] == code


ARGUMENT_ERRORS = [
    ("dot segment in path", "GITHUB_GET_RAW_REPOSITORY_CONTENT", {**REPO, "path": "../../x"}, []),
    ("dot owner", "GITHUB_GET_A_REPOSITORY", {"owner": "..", "repo": "user"}, []),
    ("raw read of the root", "GITHUB_GET_RAW_REPOSITORY_CONTENT", {**REPO, "path": "/"}, []),
    (
        "file write to the root",
        "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS",
        {**REPO, "path": "", "message": "m", "content": "c"},
        [],
    ),
    (
        "raw read of a directory",
        "GITHUB_GET_RAW_REPOSITORY_CONTENT",
        {**REPO, "path": "docs"},
        [response(200, [{"name": "a.md", "path": "docs/a.md", "type": "file", "sha": "a"}])],
    ),
    (
        "bad base64",
        "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS",
        {**REPO, "path": "a.bin", "message": "m", "content": "not base64!", "encoding": "base64"},
        [],
    ),
    ("pull request without title", "GITHUB_CREATE_A_PULL_REQUEST", {**REPO, "head": "h", "base": "main"}, []),
    ("review without body", "GITHUB_CREATE_A_REVIEW_FOR_A_PULL_REQUEST", {**REPO, "pull_number": 1, "event": "COMMENT"}, []),
    ("schema violation", "GITHUB_LIST_REPOSITORY_ISSUES", {**REPO, "state": "sideways"}, []),
]


@pytest.mark.parametrize(
    ("slug", "args", "answers"), [a[1:] for a in ARGUMENT_ERRORS], ids=[a[0] for a in ARGUMENT_ERRORS]
)
def test_argument_errors_are_invalid_arguments(slug, args, answers):
    h = harness(*answers)
    result = h.call(slug, args)
    assert result.successful is False
    assert result.error_code == "invalid_arguments"
    assert len(h.session.calls) == len(answers)


def test_missing_credentials_are_unauthorized():
    result = harness().call("GITHUB_GET_THE_AUTHENTICATED_USER", {}, config={})
    assert result.error_code == "unauthorized"


def test_unknown_slug():
    assert harness().call("GITHUB_DELETE_EVERYTHING", {}).error_code == "unknown_tool"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (GitHubAPIError(429, "x", retry_after=1), "rate_limited"),
        (GitHubAPIError(403, "x", retry_after=1), "rate_limited"),
        (GitHubAPIError(0, "x", timed_out=True), "timeout"),
        (GitHubAPIError(0, "x"), "upstream_error"),
        (GitHubAPIError(410, "Gone"), "not_found"),
        (GitHubAPIError(412, "x"), "conflict"),
        (GitHubAPIError(400, "x"), "invalid_arguments"),
    ],
)
def test_error_code_mapping(error, code):
    assert error_code(error) == code


# --------------------------------------------------------------------------
# timeout_seconds: the whole call's budget
# --------------------------------------------------------------------------


class FakeClock:
    """Monotonic time that only moves when the client sleeps or a request
    'takes' time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _budgeted(budget, *answers, **kwargs):
    clock = FakeClock()
    session = FakeSession(list(answers))
    client = GitHubRestClient(
        TOKEN, session=session, budget=budget, sleep=clock.sleep, monotonic=clock, **kwargs  # type: ignore[arg-type]
    )
    return client, session, clock


class TestTimeBudget:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(5, 5.0), (2.5, 2.5), (None, None), (0, None), (-1, None), (True, None), ("10", None), (float("nan"), None), (float("inf"), None)],
    )
    def test_budget_from_config(self, value, expected):
        assert time_budget({**CONFIG, "timeout_seconds": value}) == expected

    @pytest.mark.parametrize(("budget", "timeout"), [(5, 5.0), (100, rest.DEFAULT_TIMEOUT), (None, rest.DEFAULT_TIMEOUT)])
    def test_request_timeout_is_capped_by_the_budget(self, budget, timeout):
        h = harness(response(200, {}))
        config = {**CONFIG, "timeout_seconds": budget} if budget is not None else CONFIG
        h.call("GITHUB_GET_THE_AUTHENTICATED_USER", {}, config=config)
        assert h.session.last.timeout == pytest.approx(timeout, abs=0.5)

    def test_retry_sleep_never_outlasts_the_budget(self):
        h = harness(*_limited(429, "20"))
        result = h.call("GITHUB_GET_A_REPOSITORY", REPO, config={**CONFIG, "timeout_seconds": 10})
        assert h.sleeps == []
        assert len(h.session.calls) == 1
        assert result.error_code == "rate_limited"
        assert result.retry_after == 20.0

    def test_remaining_budget_shrinks_each_attempt(self):
        client, session, clock = _budgeted(5, *_limited(429, "3"), response(200, {}))
        client.request("GET", "/user")
        assert clock.sleeps == [3.0]
        assert [c.timeout for c in session.calls] == [5.0, 2.0]

    def test_a_retry_that_would_outlast_the_budget_fails_now(self):
        client, session, clock = _budgeted(5, *_limited(429, "3", 2))
        with pytest.raises(GitHubAPIError) as exc:
            client.request("GET", "/user")
        assert exc.value.retry_after == 3.0
        assert clock.sleeps == [3.0]  # the second 3s wait would pass the 5s budget
        assert len(session.calls) == 2

    @pytest.mark.parametrize("retry_after", ["5", "4.9", "3.5"])
    def test_a_sleep_must_leave_room_for_the_request(self, retry_after):
        # budget 5: waiting 5 / 4.9 / 3.5 s would leave < min(30, 2) s for the retry.
        client, session, clock = _budgeted(5, *_limited(429, retry_after))
        with pytest.raises(GitHubAPIError) as exc:
            client.request("GET", "/user")
        assert clock.sleeps == []
        assert len(session.calls) == 1
        assert exc.value.retry_after == float(retry_after)
        assert not exc.value.timed_out

    def test_a_sleep_that_leaves_exactly_the_floor_is_taken(self):
        client, session, clock = _budgeted(5, *_limited(429, "3"), response(200, {}))
        client.request("GET", "/user")
        assert clock.sleeps == [3.0]
        assert session.calls[-1].timeout == 2.0

    def test_floor_follows_a_short_request_timeout(self):
        # With a 1 s request timeout the floor is 1 s, so a 3.5 s wait fits a 5 s budget.
        client, _, clock = _budgeted(5, *_limited(429, "3.5"), response(200, {}), timeout=1.0)
        client.request("GET", "/user")
        assert clock.sleeps == [3.5]

    def test_server_error_backoff_leaves_room_too(self):
        client, session, clock = _budgeted(2.5, response(502, content=b""))
        with pytest.raises(GitHubAPIError) as exc:
            client.request("GET", "/user")
        assert clock.sleeps == []  # 1 s backoff + 2 s floor > 2.5 s
        assert len(session.calls) == 1
        assert (exc.value.status, exc.value.retry_after) == (502, None)

    def test_rate_limit_at_the_budget_edge_is_rate_limited_not_timeout(self):
        h = harness(*_limited(429, "5"))
        result = h.call("GITHUB_GET_A_REPOSITORY", REPO, config={**CONFIG, "timeout_seconds": 5})
        assert result.error_code == "rate_limited"
        assert result.retry_after == 5.0
        assert h.sleeps == []

    def test_spent_budget_times_out_without_a_request(self):
        client, session, clock = _budgeted(5)
        clock.now = 5.0
        with pytest.raises(GitHubAPIError) as exc:
            client.request("GET", "/user")
        assert exc.value.timed_out
        assert session.calls == []

    def test_request_timeout_names_the_seconds_not_the_url(self):
        h = harness(requests.ReadTimeout("HTTPSConnectionPool: /search/code?q=project-x-secret"))
        result = h.call("GITHUB_SEARCH_CODE", {"q": "project-x-secret"}, config={**CONFIG, "timeout_seconds": 7})
        assert result.error_code == "timeout"
        assert result.error == "GitHub did not answer within 7s (ReadTimeout)"


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


class TestConcurrency:
    def test_each_call_gets_its_own_session_and_closes_it(self):
        made: list[FakeSession] = []
        lock = threading.Lock()

        def new_session():
            session = FakeSession([response(200, {"login": "octocat"})])
            with lock:
                made.append(session)
            return session

        toolkit = GitHubToolkit()
        with (
            mock.patch.object(rest.requests, "Session", side_effect=new_session),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(pool.map(lambda _: toolkit.call_tool("GITHUB_GET_THE_AUTHENTICATED_USER", {}, CONFIG), range(16)))
        assert all(r.successful for r in results)
        assert len(made) == 16
        assert len({id(s) for s in made}) == 16
        assert all(s.closed and len(s.calls) == 1 for s in made)

    def test_fresh_instances_in_parallel_do_not_cross_talk(self):
        def run(i: int):
            h = harness(response(200, {"number": i}))
            result = h.call("GITHUB_GET_AN_ISSUE", {**REPO, "issue_number": i})
            return i, result.data["number"], h.session.last.path

        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, number, path in pool.map(run, range(1, 33)):
                assert number == i
                assert path.endswith(f"/issues/{i}")
