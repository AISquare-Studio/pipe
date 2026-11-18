"""Every unit test runs under core's socket guard: any INET socket, connect or
DNS lookup is recorded, blocked, and fails the test. Live tests (``-m live``)
are exempt."""

from __future__ import annotations

import pytest
from aisquare.pipe.testing.validation import no_network


@pytest.fixture(autouse=True)
def _hermetic(request):
    if request.node.get_closest_marker("live"):
        yield
        return
    with no_network() as log:
        yield
    assert not log.attempts, f"unit test touched the network: {log.attempts}"
