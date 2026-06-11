"""End-to-end: Pipeline(GitHubRepoSource -> GraphifyConverter -> exact-typed sink).

The github client is faked (no network); graphify is the recording stub; the
sink is in-test (gateway-style, no cross-connector imports in connector code).
Proves the real Pipeline wiring: converter fires via exact type matching, the
checkout stays alive through convert+push, metadata reaches the sink.
"""

from __future__ import annotations

import os
from unittest import mock

from aisquare.pipe import AuthType, Pipeline, PushResult, SinkConnector

from aisquare_pipe_github.connector import GitHubRepoSource
from aisquare_pipe_graphify.constants import GRAPH_CONTENT_TYPE
from aisquare_pipe_graphify.converter import GraphifyConverter


class RecordingGraphSink(SinkConnector):
    name = "test-graph-sink"
    version = "0.1.0"
    input_types = [GRAPH_CONTENT_TYPE]  # EXACT — a wildcard would skip the converter
    auth_type = AuthType.NONE
    received: list

    def __init__(self) -> None:
        self.received = []

    def push(self, envelope, config, params=None):
        self.received.append(envelope)
        return PushResult(success=True, ref=str(len(self.received)))

    def validate_config(self, config):
        return True


def test_checkout_to_graph_bundle_end_to_end(stub, tmp_path):
    workdir = tmp_path / "wd"
    workdir.mkdir()

    inst = mock.Mock()
    inst.full_name = "owner/name"
    inst.branch = "main"
    inst.ls_remote_head.return_value = ""
    inst.make_workdir.return_value = (str(workdir), True)

    def _clone(wd):
        checkout = os.path.join(wd, "checkout")
        os.makedirs(checkout, exist_ok=True)
        with open(os.path.join(checkout, "app.py"), "w") as fh:
            fh.write("def main():\n    return 42\n")
        return checkout

    inst.clone.side_effect = _clone
    inst.rev_parse_head.return_value = "b" * 40
    sink = RecordingGraphSink()

    with mock.patch("aisquare_pipe_github.connector.GitHubRepoClient") as cls:
        cls.return_value = inst
        cls.cleanup.side_effect = lambda wd: None
        result = Pipeline(
            source=GitHubRepoSource(),
            converters=[GraphifyConverter(graphify_bin=str(stub.bin), preflight=False)],
            sink=sink,
        ).run({"github": {"full_name": "owner/name"}})

    assert result.success_count == 1
    assert result.failure_count == 0
    assert len(sink.received) == 1
    bundle = sink.received[0]
    assert bundle.content_type == GRAPH_CONTENT_TYPE
    assert bundle.data["stats"]["nodes"] == 12
    assert bundle.metadata["head_sha"] == "b" * 40  # source metadata survived the converter
    assert bundle.metadata["tier"] == "ast"
