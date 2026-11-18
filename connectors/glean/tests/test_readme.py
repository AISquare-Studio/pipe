"""The README's claims about what pipe-as-service runs.

pipe-as-service's gateway catalogue marks a toolkit with no registry card and
no ``OAuthSpec`` as unsupported, so the ``GLEAN_*`` tools are not served there
yet. Its credential resolver does classify ``glean-source`` as FIRST_PARTY and
reads the tenant's ``glean`` blob, so the source works. The README must say
exactly that and must not imply the tools can be called through the service.
"""

from __future__ import annotations

from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"


def _text() -> str:
    return " ".join(README.read_text(encoding="utf-8").split())


def test_readme_does_not_claim_the_service_serves_the_toolkit():
    text = _text()
    # The over-claim this replaced.
    assert "pipe-as-service treats the connector as" not in text
    assert (
        "pipe-as-service does not serve the `GLEAN_SEARCH` and "
        "`GLEAN_GET_DOCUMENT` tools yet" in text
    )
    assert "Only `glean-source` runs through pipe-as-service" in text
