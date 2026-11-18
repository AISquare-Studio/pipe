"""GcsToolkit against a fake Google: listing, paging, reads, markers, refusals."""

from __future__ import annotations

import pytest
from aisquare.pipe import PRIMARY_TAG

from aisquare_pipe_gcs import GcsToolkit
from aisquare_pipe_gcs.toolkit import GET_OBJECT, LIST_OBJECTS, MAX_PAGE_TOKEN_CHARS
from tests.fakes import FakeGoogle, StoredObject, blob

PDF = b"%PDF-1.7 travel policy"
TEXT = b"Travel policy: economy class.\n"


def _google(**extra: StoredObject) -> FakeGoogle:
    objects = {
        "policies/travel.pdf": StoredObject(
            PDF, "application/pdf", generation="1700000000000007"
        ),
        "policies/handbook.txt": StoredObject(
            TEXT, "text/plain", generation="1700000000000008"
        ),
        "policies/2026/retention.md": StoredObject(b"# Retention", "text/markdown"),
        "policies/2026/": StoredObject(b"", "application/x-directory"),
        "hr/salaries.csv": StoredObject(b"secret,1", "text/csv"),
    }
    objects.update(extra)
    return FakeGoogle(objects=objects)


def _call(google_: FakeGoogle, slug: str, arguments: dict, config: dict | None = None):
    return GcsToolkit(session=google_).call_tool(
        slug, arguments, blob() if config is None else config
    )


def test_list_objects_returns_each_objects_version_markers():
    google_ = _google()
    result = _call(google_, "GCS_LIST_OBJECTS", {})
    assert result.successful, result.error
    data = result.data
    assert (data["bucket"], data["prefix"], data["has_more"]) == (
        "acme-policies",
        "policies/",
        False,
    )
    names = [o["name"] for o in data["objects"]]
    assert "hr/salaries.csv" not in names  # outside the configured prefix
    travel = next(o for o in data["objects"] if o["name"] == "policies/travel.pdf")
    assert travel["generation"] == "1700000000000007"
    assert travel["md5_hash"] == "XrY7u+Ae7tCTyyK7j1rNww=="
    assert travel["updated"] == "2026-11-17T10:00:00.000Z"
    assert (travel["size"], travel["content_type"]) == (len(PDF), "application/pdf")
    (call,) = google_.storage_calls()
    assert call["params"]["prefix"] == "policies/"
    assert (
        "generation" in call["params"]["fields"]
        and "md5Hash" in call["params"]["fields"]
    )
    assert call["allow_redirects"] is False


def test_list_objects_pages_with_the_token():
    google_ = _google()
    first = _call(google_, "GCS_LIST_OBJECTS", {"page_size": 2}).data
    assert first["has_more"] and first["next_page_token"]
    second = _call(
        google_,
        "GCS_LIST_OBJECTS",
        {"page_size": 2, "page_token": first["next_page_token"]},
    ).data
    seen = [o["name"] for o in first["objects"] + second["objects"]]
    assert len(seen) == len(set(seen)) == 4 and second["has_more"] is False


def test_list_objects_one_level_with_folders():
    data = _call(_google(), "GCS_LIST_OBJECTS", {"folders": True}).data
    assert data["folders"] == ["policies/2026/"]
    assert sorted(o["name"] for o in data["objects"]) == [
        "policies/handbook.txt",
        "policies/travel.pdf",
    ]


def test_get_object_returns_the_file_its_text_and_its_version_markers():
    google_ = _google()
    result = _call(google_, "GCS_GET_OBJECT", {"name": "policies/handbook.txt"})
    assert result.successful, result.error
    assert result.data["content"] == TEXT.decode()
    assert result.data["generation"] == "1700000000000008"
    (envelope,) = result.files
    assert envelope.data == TEXT and envelope.content_type == "text/plain"
    assert envelope.metadata["generation"] == "1700000000000008"
    assert envelope.metadata["md5_hash"] and envelope.metadata["updated"]
    assert envelope.metadata["filename"] == "handbook.txt"
    # The download is pinned to the generation the metadata read saw.
    media = [c for c in google_.storage_calls() if c["params"].get("alt") == "media"]
    assert media[0]["params"]["generation"] == "1700000000000008"

    pdf = _call(google_, "GCS_GET_OBJECT", {"name": "policies/travel.pdf"})
    assert "content" not in pdf.data and pdf.files[0].data == PDF


def test_get_object_metadata_only_downloads_nothing():
    google_ = _google()
    result = _call(
        google_,
        "GCS_GET_OBJECT",
        {"name": "policies/travel.pdf", "include_content": False},
    )
    assert result.successful and result.files == []
    assert result.data["md5_hash"] == "XrY7u+Ae7tCTyyK7j1rNww=="
    assert all(c["params"].get("alt") != "media" for c in google_.storage_calls())


def test_get_object_over_max_bytes_is_refused_before_the_download():
    google_ = _google()
    result = _call(
        google_, "GCS_GET_OBJECT", {"name": "policies/travel.pdf", "max_bytes": 5}
    )
    assert not result.successful and result.error_code == "invalid_arguments"
    assert all(c["params"].get("alt") != "media" for c in google_.storage_calls())


def test_a_name_outside_the_configured_prefix_is_forbidden_without_a_request():
    google_ = _google()
    for slug, arguments in (
        ("GCS_GET_OBJECT", {"name": "hr/salaries.csv"}),
        ("GCS_LIST_OBJECTS", {"prefix": "hr/"}),
    ):
        result = _call(google_, slug, arguments)
        assert not result.successful and result.error_code == "forbidden", slug
    assert google_.storage_calls() == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "policies/a.pdf", "bucket": "someone-elses-bucket"},
        {"name": "policies/a.pdf", "service_account_json": {"type": "service_account"}},
        {"name": "policies/a.pdf", "generation": "latest"},
    ],
)
def test_a_caller_cannot_override_a_server_key(arguments):
    google_ = _google()
    result = _call(google_, "GCS_GET_OBJECT", arguments)
    assert not result.successful and result.error_code == "invalid_arguments"
    assert google_.requests == []


def test_without_credentials_every_tool_fails_closed_without_a_request():
    google_ = _google()
    for config in ({}, {"bucket": "acme-policies"}, blob(service_account_json=None)):
        for slug in ("GCS_LIST_OBJECTS", "GCS_GET_OBJECT"):
            result = GcsToolkit(session=google_).call_tool(
                slug, {"name": "policies/a"}, config
            )
            assert not result.successful and result.error_code == "not_connected", (
                slug,
                config,
            )
            assert result.error.startswith("Google Cloud Storage is not configured")
    assert google_.requests == []


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (403, {}, "forbidden"),
        (404, {}, "not_found"),
        (429, {"Retry-After": "7"}, "rate_limited"),
        (500, {}, "upstream_error"),
        (302, {"Location": "https://elsewhere.example/"}, "upstream_error"),
    ],
)
def test_cloud_storage_failures_map_to_canonical_codes(status, headers, code):
    google_ = _google()
    google_.fail_with = (
        status,
        {"error": {"code": status, "message": "boom"}},
        headers,
    )
    result = _call(google_, "GCS_LIST_OBJECTS", {})
    assert not result.successful and result.error_code == code
    if code == "rate_limited":
        assert result.retry_after == 7.0
    if status == 403:
        assert "roles/storage.objectViewer" in (result.error or "")
    for token in google_.tokens:
        assert token not in (result.error or "")


def test_a_timeout_is_a_timeout():
    google_ = _google()
    google_.raise_timeout = True
    result = _call(google_, "GCS_LIST_OBJECTS", {})
    assert not result.successful and result.error_code == "timeout"


def test_the_tools_are_read_only_and_the_object_read_is_primary():
    assert all(t.read_only for t in GcsToolkit().tools())
    assert PRIMARY_TAG in GET_OBJECT.tags and PRIMARY_TAG not in LIST_OBJECTS.tags
    for spec in GcsToolkit().tools():
        assert spec.input_schema["additionalProperties"] is False


def test_the_toolkit_is_found_by_search_scored_and_search_connectors():
    from aisquare.pipe.core.search import search_connectors, search_scored

    assert search_connectors("google cloud storage bucket", limit=3)[0][0] == "gcs"
    hits = search_scored(
        "read the policy pdf from our gcs bucket", connectors=["gcs"], limit=2
    )
    assert hits[0].tool.slug == "GCS_GET_OBJECT"
    top = search_scored("download a file from google cloud storage", limit=5)
    assert any(h.connector == "gcs" for h in top)


@pytest.mark.parametrize("name", [".", ".."])
def test_dot_and_dotdot_are_refused_before_any_request(name):
    """With no prefix they are 'inside' it, but urllib3 would collapse
    /o/. and /o/.. into the listing and the bucket URLs."""
    google_ = _google()
    result = _call(google_, "GCS_GET_OBJECT", {"name": name}, blob(prefix=None))
    assert not result.successful and result.error_code == "invalid_arguments"
    assert google_.storage_calls() == []


def test_string_arguments_are_bounded_before_any_request():
    """Core checks types only, so the lengths the schemas declare are
    checked here: a page_token over its maxLength is refused unsent."""
    assert LIST_OBJECTS.input_schema["properties"]["page_token"]["maxLength"] == (
        MAX_PAGE_TOKEN_CHARS
    )
    google_ = _google()
    for slug, arguments in (
        ("GCS_LIST_OBJECTS", {"page_token": "x" * (MAX_PAGE_TOKEN_CHARS + 1)}),
        ("GCS_LIST_OBJECTS", {"prefix": "policies/" + "x" * 1024}),
        ("GCS_GET_OBJECT", {"name": "policies/" + "x" * 1024}),
    ):
        result = _call(google_, slug, arguments)
        assert not result.successful and result.error_code == "invalid_arguments", slug
    assert google_.requests == []


@pytest.mark.parametrize("status", [400, 403, 404, 500])
def test_an_error_body_that_echoes_the_bearer_token_is_redacted(status):
    google_ = _google()
    google_.fail_with = (
        status,
        {"error": {"code": status, "message": "bad token {token} for this bucket"}},
        {},
    )
    result = _call(google_, "GCS_LIST_OBJECTS", {})
    assert not result.successful
    assert "bad token [redacted] for this bucket" in (result.error or "")
    (token,) = google_.tokens
    assert token not in (result.error or "")


@pytest.mark.parametrize(
    ("status", "message", "code", "hint", "not_hint"),
    [
        (
            403,
            (
                "Request is prohibited by organization's policy. "
                "vpcServiceControlsUniqueIdentifier: abc123"
            ),
            "forbidden",
            "VPC Service Controls perimeter",
            "roles/storage.objectViewer",
        ),
        (
            403,
            (
                "reader@acme-proj.iam.gserviceaccount.com does not have "
                "storage.objects.list access to the Google Cloud Storage bucket."
            ),
            "forbidden",
            "roles/storage.objectViewer",
            "VPC Service Controls",
        ),
        (
            400,
            "Bucket is a requester pays bucket but no user project provided.",
            "invalid_arguments",
            "requester-pays buckets are not supported",
            "objectViewer",
        ),
        (
            400,
            "The target object is encrypted by a customer-supplied encryption key.",
            "invalid_arguments",
            "customer-supplied key (CSEK) can't be read",
            "objectViewer",
        ),
    ],
)
def test_failures_say_what_to_fix(status, message, code, hint, not_hint):
    google_ = _google()
    google_.fail_with = (status, {"error": {"code": status, "message": message}}, {})
    result = _call(google_, "GCS_LIST_OBJECTS", {})
    assert not result.successful and result.error_code == code
    assert hint in (result.error or "") and not_hint not in (result.error or "")
    assert "service account key" not in (result.error or "")


def test_a_gzip_stored_object_says_its_markers_describe_the_stored_bytes():
    google_ = _google(
        **{
            "policies/log.txt": StoredObject(
                b"inflated text", "text/plain", content_encoding="gzip"
            )
        }
    )
    listed = _call(google_, "GCS_LIST_OBJECTS", {}).data["objects"]
    assert (
        next(o for o in listed if o["name"] == "policies/log.txt")["content_encoding"]
        == "gzip"
    )
    assert "contentEncoding" in google_.storage_calls()[0]["params"]["fields"]
    result = _call(google_, "GCS_GET_OBJECT", {"name": "policies/log.txt"})
    assert result.data["content_encoding"] == "gzip"
    assert result.files[0].metadata["content_encoding"] == "gzip"
    plain = _call(google_, "GCS_GET_OBJECT", {"name": "policies/handbook.txt"})
    assert plain.data["content_encoding"] is None
    assert "content_encoding" not in plain.files[0].metadata
