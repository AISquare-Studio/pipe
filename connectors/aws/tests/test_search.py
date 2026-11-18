"""The installed toolkits register themselves (entry points) and the router's search finds them."""

from aisquare.pipe import discover_connectors, search_connectors, search_scored
from aisquare.pipe.core.registry import discover_toolkits


def test_both_toolkits_and_both_sources_are_registered():
    toolkits = discover_toolkits()
    assert {"bedrock_agentcore", "s3"} <= set(toolkits)
    assert {"bedrock-agentcore-policies-source", "s3-source"} <= set(
        discover_connectors()
    )


def test_search_scored_finds_the_agentcore_policy_tools():
    hits = search_scored(
        "export the cedar policies from our agentcore policy engine", limit=5
    )
    assert hits and hits[0].connector == "bedrock_agentcore", [
        (h.connector, h.tool.slug) for h in hits
    ]
    assert hits[0].tool.slug in {
        "BEDROCK_AGENTCORE_LIST_POLICIES",
        "BEDROCK_AGENTCORE_GET_POLICY",
    }


def test_search_scored_finds_the_s3_tools():
    hits = search_scored("read the rule book file from our s3 bucket", limit=5)
    assert hits and hits[0].connector == "s3", [
        (h.connector, h.tool.slug) for h in hits
    ]
    pinned = search_scored("list objects", connectors=["s3"], limit=3)
    assert pinned[0].tool.slug == "S3_LIST_OBJECTS"


def test_search_connectors_shortlists_them():
    assert "bedrock_agentcore" in [
        name for name, _ in search_connectors("aws agentcore cedar policies")
    ]
    assert "s3" in [
        name for name, _ in search_connectors("files in an amazon s3 bucket")
    ]
