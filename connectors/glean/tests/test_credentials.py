"""The credential blob: host allow-listing, token kinds, act_as."""

from __future__ import annotations

import pytest

from aisquare_pipe_glean import GleanNotConfigured, load_credentials
from tests.fakes import GLOBAL_BLOB, HOST, TOKEN, USER_BLOB


def test_user_token_loads_and_sends_no_act_as():
    creds = load_credentials(USER_BLOB)
    assert (creds.host, creds.token_kind, creds.act_as) == (HOST, "user", None)
    assert creds.base_url == f"https://{HOST}/rest/api/v1/"
    assert creds.headers() == {"Authorization": f"Bearer {TOKEN}"}
    assert TOKEN not in repr(creds)


def test_token_kind_defaults_to_user():
    blob = {k: v for k, v in USER_BLOB.items() if k != "token_kind"}
    assert load_credentials(blob).token_kind == "user"


def test_global_token_always_carries_act_as():
    creds = load_credentials(GLOBAL_BLOB)
    assert creds.headers() == {
        "Authorization": f"Bearer {TOKEN}",
        "X-Glean-ActAs": "reader@acme.com",
    }


@pytest.mark.parametrize("act_as", [None, "", "  ", "not-an-email", 7])
def test_global_token_without_a_valid_act_as_is_not_configured(act_as):
    blob = {**GLOBAL_BLOB, "act_as": act_as}
    with pytest.raises(GleanNotConfigured, match="global token needs act_as"):
        load_credentials(blob)


def test_user_token_with_act_as_is_refused():
    with pytest.raises(GleanNotConfigured, match="only for a global token"):
        load_credentials({**USER_BLOB, "act_as": "someone@acme.com"})


@pytest.mark.parametrize(
    "host",
    [
        "evil.com",
        "acme.glean.com",
        "acme-be.glean.com.evil.com",
        "acme-be.glean.co",
        "acme_be.glean.com",
        "https://acme-be.glean.com",
        "acme-be.glean.com:443",
        "acme-be.glean.com/rest",
        "user@acme-be.glean.com",
        "acme-be.glean.com.",
        "-be.glean.com",
        "169.254.169.254",
        "localhost",
    ],
)
def test_host_outside_the_glean_pattern_is_refused(host):
    with pytest.raises(GleanNotConfigured) as e:
        load_credentials({**USER_BLOB, "host": host})
    assert TOKEN not in str(e.value)


def test_host_is_normalised_to_lowercase():
    assert load_credentials({**USER_BLOB, "host": " ACME-be.Glean.com "}).host == HOST


def test_an_explicit_allowed_host_in_the_blob_is_accepted():
    blob = {
        **USER_BLOB,
        "host": "glean.acme.internal",
        "allowed_hosts": ["glean.acme.internal"],
    }
    assert load_credentials(blob).host == "glean.acme.internal"
    # The allow-list admits exactly its entries.
    with pytest.raises(GleanNotConfigured, match="not a Glean host"):
        load_credentials({**blob, "host": "other.acme.internal"})


@pytest.mark.parametrize(
    "allowed", ["glean.acme.internal", [7], ["https://x.acme.com"]]
)
def test_a_malformed_allow_list_is_refused(allowed):
    with pytest.raises(GleanNotConfigured, match="allowed_hosts"):
        load_credentials(
            {**USER_BLOB, "host": "glean.acme.internal", "allowed_hosts": allowed}
        )


@pytest.mark.parametrize(
    "blob",
    [
        None,
        {},
        "token",
        {"host": HOST},
        {"host": HOST, "token": ""},
        {"host": HOST, "token": "   "},
        {"host": HOST, "token": "two words"},
        {"host": HOST, "token": "tok\x00en"},
        {"token": TOKEN},
        {"host": HOST, "token": TOKEN, "token_kind": "admin"},
    ],
)
def test_missing_or_malformed_blob_is_not_configured(blob):
    with pytest.raises(GleanNotConfigured) as e:
        load_credentials(blob)
    assert TOKEN not in str(e.value)
