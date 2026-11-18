"""The toolkit and source register themselves (entry points) and the router's search finds them."""

from aisquare.pipe import (
    PRIMARY_TAG,
    discover_connectors,
    search_connectors,
    search_scored,
)
from aisquare.pipe.core.registry import discover_toolkits


def test_the_toolkit_and_the_source_are_registered():
    assert "redshift" in discover_toolkits()
    assert "redshift-query-source" in discover_connectors()


def test_run_select_is_the_primary_tool():
    toolkit = discover_toolkits()["redshift"]()
    primary = [t.slug for t in toolkit.tools() if PRIMARY_TAG in t.tags]
    assert primary == ["REDSHIFT_RUN_SELECT"]


def test_search_scored_finds_run_select():
    hits = search_scored(
        "run a sql query on our amazon redshift data warehouse", limit=5
    )
    assert hits and hits[0].connector == "redshift", [
        (h.connector, h.tool.slug) for h in hits
    ]
    assert hits[0].tool.slug == "REDSHIFT_RUN_SELECT"


def test_search_scored_finds_the_metadata_tools():
    pinned = search_scored("list tables", connectors=["redshift"], limit=3)
    assert pinned[0].tool.slug == "REDSHIFT_LIST_TABLES"
    pinned = search_scored(
        "which columns does the table have", connectors=["redshift"], limit=3
    )
    assert pinned[0].tool.slug == "REDSHIFT_DESCRIBE_TABLE"
    pinned = search_scored("list schemas", connectors=["redshift"], limit=3)
    assert pinned[0].tool.slug == "REDSHIFT_LIST_SCHEMAS"


def test_search_connectors_shortlists_it():
    assert "redshift" in [
        name for name, _ in search_connectors("amazon redshift warehouse sql")
    ]
