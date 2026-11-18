"""Every unit test runs under core's socket guard: any INET socket, connect or
DNS lookup is recorded, blocked, and fails the test. Live tests (``-m live``)
are exempt. Minted tokens are forgotten between tests."""

from __future__ import annotations

import pytest
from aisquare.pipe.testing.validation import no_network

from aisquare_pipe_bigquery.credentials import clear_token_cache


@pytest.fixture(autouse=True)
def _hermetic(request):
    clear_token_cache()
    if request.node.get_closest_marker("live"):
        yield
        return
    with no_network() as log:
        yield
    clear_token_cache()
    assert not log.attempts, f"unit test touched the network: {log.attempts}"
