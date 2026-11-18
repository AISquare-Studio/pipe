"""Through a REAL requests.Session (tests.fakes.real_session): a body that
breaks off midway fails as it would off a socket, wrapped by urllib3 and
requests, and still comes out as a typed GCS error. No socket is opened
(core's guard, conftest)."""

from __future__ import annotations

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_gcs import GcsAPIError, GcsSource, GcsToolkit
from tests.fakes import FakeGoogle, StoredObject, blob, real_session

BODY = b"0123456789" * 1000


def _google() -> FakeGoogle:
    return FakeGoogle(
        objects={
            "policies/a.txt": StoredObject(BODY, "text/plain", generation="7"),
            "policies/b.txt": StoredObject(b"bee", "text/plain", generation="8"),
        }
    )


def _get(google_: FakeGoogle, name: str = "policies/a.txt"):
    return GcsToolkit(session=real_session(google_)).call_tool(
        "GCS_GET_OBJECT", {"name": name}, blob()
    )


def _pull(google_: FakeGoogle) -> list:
    source = GcsSource(session=real_session(google_))
    return list(source.pull(blob(), PullParams(params={})))


def test_list_read_and_pull_work_over_a_real_requests_session():
    google_ = _google()
    listed = GcsToolkit(session=real_session(google_)).call_tool(
        "GCS_LIST_OBJECTS", {}, blob()
    )
    assert listed.successful, listed.error
    assert [o["generation"] for o in listed.data["objects"]] == ["7", "8"]
    read = _get(google_)
    assert read.successful and read.files[0].data == BODY
    assert [e.data for e in _pull(google_)] == [BODY, b"bee"]
    # One token for all of it, minted with a real form POST.
    assert len(google_.token_requests) == 1


def test_a_read_timeout_mid_download_is_a_timeout():
    """requests turns urllib3's ReadTimeoutError during iter_content into a
    ConnectionError; before, that came out as upstream_error from the
    toolkit and as a raw requests ConnectionError from the source."""
    google_ = _google()
    google_.media_break = TimeoutError("timed out")
    result = _get(google_)
    assert not result.successful and result.error_code == "timeout", result.error
    with pytest.raises(GcsAPIError) as raised:
        _pull(google_)
    assert raised.value.timed_out


def test_a_connection_reset_mid_download_is_an_upstream_error():
    """urllib3 ProtocolError -> requests ChunkedEncodingError, which escaped
    the source raw before."""
    google_ = _google()
    google_.media_break = ConnectionResetError(54, "Connection reset by peer")
    result = _get(google_)
    assert not result.successful and result.error_code == "upstream_error"
    assert "could not read the object from Cloud Storage (ChunkedEncodingError)" in (
        result.error or ""
    )
    with pytest.raises(GcsAPIError) as raised:
        _pull(google_)
    assert not raised.value.timed_out and raised.value.status is None


def test_a_token_answer_that_times_out_midway_is_a_timeout():
    google_ = _google()
    google_.token_body_break = TimeoutError("timed out")
    result = _get(google_)
    assert not result.successful and result.error_code == "timeout", result.error
    assert "service account key" not in (result.error or "")
    assert google_.storage_calls() == []
