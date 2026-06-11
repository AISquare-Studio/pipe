"""Tests for the Layer A contract-check machinery (testing/validation.py)."""

from __future__ import annotations

import socket
from collections.abc import Iterator

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, PullParams, PushParams, PushResult
from aisquare.pipe.errors import ConfigValidationError
from aisquare.pipe.testing.mock_connectors import MockSink, MockSource
from aisquare.pipe.testing.validation import (
    NetworkBlockedError,
    check_entry_point_name,
    check_pull_contract,
    check_push_contract,
    check_source_validate_config,
    failures,
    no_network,
    run_contract_checks,
)


def _failed_ids(results) -> set[str]:
    return {r.id for r in failures(results)}


class TestNoNetworkGuard:
    def test_blocks_create_connection_and_records(self):
        try:
            with no_network() as log:
                try:
                    socket.create_connection(("example.com", 443))
                except NetworkBlockedError:
                    pass
        finally:
            pass
        assert log.attempts and "create_connection" in log.attempts[0]

    def test_blocks_dns(self):
        with no_network() as log:
            try:
                socket.getaddrinfo("example.com", 443)
            except NetworkBlockedError:
                pass
        assert log.attempts and "getaddrinfo" in log.attempts[0]

    def test_blocks_inet_socket_creation(self):
        with no_network() as log:
            try:
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except NetworkBlockedError:
                pass
        assert log.attempts

    def test_allows_af_unix(self):
        with no_network() as log:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.close()
        assert log.attempts == []

    def test_restores_after_exit(self):
        original_socket = socket.socket
        original_create = socket.create_connection
        original_gai = socket.getaddrinfo
        with no_network():
            assert socket.socket is not original_socket
        assert socket.socket is original_socket
        assert socket.create_connection is original_create
        assert socket.getaddrinfo is original_gai

    def test_restores_after_exception(self):
        original_socket = socket.socket
        try:
            with no_network():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert socket.socket is original_socket


class TestMocksMustPass:
    """The framework's own mock connectors pass every contract check —
    pinned forever; loosening this means the checks broke, not the mocks."""

    def test_mock_source(self):
        assert failures(run_contract_checks(MockSource, "mock-source")) == []

    def test_mock_sink(self):
        assert failures(run_contract_checks(MockSink, "mock-sink")) == []


class _BadValidateRaises(SourceConnector):
    name = "bad-validate-raises"
    version = "0.1.0"
    output_types = ["text/plain"]
    auth_type = AuthType.API_KEY

    def pull(self, config: dict, params: PullParams | None = None) -> Iterator[DataEnvelope]:
        yield from ()

    def validate_config(self, config: dict) -> bool:
        raise KeyError("api_key")


class _BadValidateTrueWithAuth(SourceConnector):
    name = "bad-validate-true"
    version = "0.1.0"
    output_types = ["text/plain"]
    auth_type = AuthType.API_KEY

    def pull(self, config: dict, params: PullParams | None = None) -> Iterator[DataEnvelope]:
        yield from ()

    def validate_config(self, config: dict) -> bool:
        return True  # claims empty config is valid despite API_KEY auth


class _BadValidateNonBool(SourceConnector):
    name = "bad-validate-nonbool"
    version = "0.1.0"
    output_types = ["text/plain"]
    auth_type = AuthType.NONE

    def pull(self, config: dict, params: PullParams | None = None) -> Iterator[DataEnvelope]:
        yield from ()

    def validate_config(self, config: dict):  # type: ignore[override]
        return "yes"


class _BadPullKeyError(SourceConnector):
    name = "bad-pull-keyerror"
    version = "0.1.0"
    output_types = ["text/plain"]
    auth_type = AuthType.NONE

    def pull(self, config: dict, params: PullParams | None = None) -> Iterator[DataEnvelope]:
        yield DataEnvelope(
            content_type="text/plain", data=config["required"], source_id=self.name
        )

    def validate_config(self, config: dict) -> bool:
        return True


class _CleanPullRaises(SourceConnector):
    name = "clean-pull-raises"
    version = "0.1.0"
    output_types = ["text/plain"]
    auth_type = AuthType.API_KEY

    def pull(self, config: dict, params: PullParams | None = None) -> Iterator[DataEnvelope]:
        raise ConfigValidationError("missing api_key")
        yield  # pragma: no cover — makes this a generator

    def validate_config(self, config: dict) -> bool:
        return False


class _PullDoesNetwork(SourceConnector):
    name = "pull-does-network"
    version = "0.1.0"
    output_types = ["text/plain"]
    auth_type = AuthType.NONE

    def pull(self, config: dict, params: PullParams | None = None) -> Iterator[DataEnvelope]:
        socket.create_connection(("example.com", 443))
        yield DataEnvelope(content_type="text/plain", data="x", source_id=self.name)

    def validate_config(self, config: dict) -> bool:
        return True


class _PushReturnsNone(SinkConnector):
    name = "push-returns-none"
    version = "0.1.0"
    input_types = ["*/*"]
    auth_type = AuthType.NONE

    def push(self, envelope: DataEnvelope, config: dict, params: PushParams | None = None):  # type: ignore[override]
        return None

    def validate_config(self, config: dict) -> bool:
        return True


class _PushSwallowsNetworkError(SinkConnector):
    """A sink whose broad except hides the network attempt — the guard's
    record-then-raise design must still fail it."""

    name = "push-swallows-network"
    version = "0.1.0"
    input_types = ["*/*"]
    auth_type = AuthType.NONE

    def push(
        self, envelope: DataEnvelope, config: dict, params: PushParams | None = None
    ) -> PushResult:
        try:
            socket.create_connection(("api.example.com", 443))
            return PushResult(success=True)
        except Exception as e:
            return PushResult(success=False, error=str(e))

    def validate_config(self, config: dict) -> bool:
        return True


class TestBrokenFakes:
    def test_validate_config_raises_fails(self):
        assert "contract.source.validate-config-no-creds" in _failed_ids(
            check_source_validate_config(_BadValidateRaises)
        )

    def test_validate_config_true_with_auth_fails(self):
        assert "contract.source.validate-config-no-creds" in _failed_ids(
            check_source_validate_config(_BadValidateTrueWithAuth)
        )

    def test_validate_config_non_bool_fails(self):
        assert "contract.source.validate-config-no-creds" in _failed_ids(
            check_source_validate_config(_BadValidateNonBool)
        )

    def test_pull_keyerror_fails(self):
        assert "contract.source.pull-no-creds" in _failed_ids(
            check_pull_contract(_BadPullKeyError)
        )

    def test_pull_clean_error_passes(self):
        assert failures(check_pull_contract(_CleanPullRaises)) == []

    def test_pull_network_fails(self):
        results = check_pull_contract(_PullDoesNetwork)
        failed = {r.id: r.message for r in failures(results)}
        assert "contract.source.pull-no-creds" in failed
        assert "network" in failed["contract.source.pull-no-creds"]

    def test_push_returning_none_fails(self):
        assert "contract.sink.push-no-creds" in _failed_ids(
            check_push_contract(_PushReturnsNone)
        )

    def test_push_swallowing_network_error_still_fails(self):
        results = check_push_contract(_PushSwallowsNetworkError)
        failed = {r.id: r.message for r in failures(results)}
        assert "contract.sink.push-no-creds" in failed
        assert "network" in failed["contract.sink.push-no-creds"]


class TestEntryPointNameRule:
    def test_exact_match(self):
        assert failures(check_entry_point_name(MockSource, "mock-source")) == []

    def test_prefix_match(self):
        class N8nLike(MockSource):
            name = "n8n"

        assert failures(check_entry_point_name(N8nLike, "n8n-source")) == []

    def test_mismatch_fails(self):
        assert "contract.entry-point-name" in _failed_ids(
            check_entry_point_name(MockSource, "completely-different")
        )

    def test_prefix_requires_dash_boundary(self):
        class Google(MockSource):
            name = "google"

        assert "contract.entry-point-name" in _failed_ids(
            check_entry_point_name(Google, "googlecalendar-source")
        )


class TestRunContractChecks:
    def test_instantiation_failure_short_circuits(self):
        class NeedsArgs(MockSource):
            def __init__(self, required):  # type: ignore[no-untyped-def]
                super().__init__()

        results = run_contract_checks(NeedsArgs)
        assert [r.id for r in results] == ["contract.instantiate"]
        assert not results[0].passed
