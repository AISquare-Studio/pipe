"""Tests for SalesforceSink."""

from __future__ import annotations

import json

from aisquare.pipe.core.envelope import DataEnvelope, PushParams, PushResult

from aisquare_pipe_salesforce.connector import SalesforceSink


def _envelope(data, metadata=None) -> DataEnvelope:
    return DataEnvelope(
        content_type="application/json",
        data=data,
        source_id="test",
        metadata=metadata or {},
    )


class TestPushOperationDispatch:
    def test_insert_when_no_id_no_external_field(self, mock_client, sample_userpass_config):
        mock_client.create.return_value = {"id": "001NEW", "success": True}
        result = SalesforceSink().push(
            _envelope({"Name": "Acme"}, {"object_type": "Account"}),
            sample_userpass_config,
        )
        assert result.success is True
        assert result.ref == "001NEW"
        assert result.metadata["operation"] == "insert"
        mock_client.create.assert_called_once_with("Account", {"Name": "Acme"})

    def test_update_when_salesforce_id_present(self, mock_client, sample_userpass_config):
        mock_client.update.return_value = 204
        result = SalesforceSink().push(
            _envelope(
                {"Industry": "Tech"},
                {"object_type": "Account", "salesforce_id": "001EXISTING"},
            ),
            sample_userpass_config,
        )
        assert result.success is True
        assert result.ref == "001EXISTING"
        assert result.metadata["operation"] == "update"
        mock_client.update.assert_called_once_with(
            "Account", "001EXISTING", {"Industry": "Tech"}
        )

    def test_upsert_when_external_id_field_present(self, mock_client, sample_userpass_config):
        mock_client.upsert.return_value = 201
        result = SalesforceSink().push(
            _envelope(
                {"Name": "Acme"},
                {"object_type": "Account", "external_id_field": "Ext__c"},
            ),
            sample_userpass_config,
            PushParams(params={"external_id_value": "ext-42"}),
        )
        assert result.success is True
        assert result.ref == "ext-42"
        assert result.metadata["operation"] == "upsert"
        mock_client.upsert.assert_called_once_with(
            "Account", "Ext__c", "ext-42", {"Name": "Acme"}
        )

    def test_explicit_operation_overrides_inference(
        self, mock_client, sample_userpass_config
    ):
        mock_client.create.return_value = {"id": "001NEW"}
        # Even though salesforce_id is set (would infer update), explicit insert wins
        result = SalesforceSink().push(
            _envelope(
                {"Name": "Acme"},
                {"object_type": "Account", "salesforce_id": "001ANY"},
            ),
            sample_userpass_config,
            PushParams(params={"operation": "insert"}),
        )
        assert result.metadata["operation"] == "insert"
        mock_client.create.assert_called_once()
        mock_client.update.assert_not_called()


class TestPushDataCoercion:
    def test_dict_data(self, mock_client, sample_userpass_config):
        mock_client.create.return_value = {"id": "001"}
        result = SalesforceSink().push(
            _envelope({"Name": "X"}, {"object_type": "Account"}),
            sample_userpass_config,
        )
        assert result.success is True

    def test_json_string_data(self, mock_client, sample_userpass_config):
        mock_client.create.return_value = {"id": "001"}
        result = SalesforceSink().push(
            _envelope(json.dumps({"Name": "X"}), {"object_type": "Account"}),
            sample_userpass_config,
        )
        assert result.success is True
        mock_client.create.assert_called_once_with("Account", {"Name": "X"})

    def test_json_bytes_data(self, mock_client, sample_userpass_config):
        mock_client.create.return_value = {"id": "001"}
        result = SalesforceSink().push(
            _envelope(json.dumps({"Name": "X"}).encode(), {"object_type": "Account"}),
            sample_userpass_config,
        )
        assert result.success is True
        mock_client.create.assert_called_once_with("Account", {"Name": "X"})

    def test_unsupported_data_returns_failure(self, mock_client, sample_userpass_config):
        result = SalesforceSink().push(
            _envelope(12345, {"object_type": "Account"}),
            sample_userpass_config,
        )
        assert result.success is False
        assert "Unsupported data type" in result.error


class TestPushErrorPaths:
    def test_missing_object_type_returns_failure(self, mock_client, sample_userpass_config):
        result = SalesforceSink().push(
            _envelope({"Name": "X"}, {}),
            sample_userpass_config,
        )
        assert result.success is False
        assert "object_type" in result.error

    def test_update_without_id_returns_failure(self, mock_client, sample_userpass_config):
        result = SalesforceSink().push(
            _envelope({"Name": "X"}, {"object_type": "Account"}),
            sample_userpass_config,
            PushParams(params={"operation": "update"}),
        )
        assert result.success is False
        assert "salesforce_id" in result.error

    def test_upsert_without_external_value_returns_failure(
        self, mock_client, sample_userpass_config
    ):
        result = SalesforceSink().push(
            _envelope(
                {"Name": "X"},
                {"object_type": "Account", "external_id_field": "Ext__c"},
            ),
            sample_userpass_config,
        )
        assert result.success is False
        assert "external_id_value" in result.error

    def test_exception_in_client_returns_failure(self, mock_client, sample_userpass_config):
        mock_client.create.side_effect = RuntimeError("boom")
        result = SalesforceSink().push(
            _envelope({"Name": "X"}, {"object_type": "Account"}),
            sample_userpass_config,
        )
        assert result.success is False
        assert "boom" in result.error

    def test_returns_push_result_for_empty_envelope(self, sample_userpass_config):
        """Compliance: push() must always return PushResult, even for garbage input."""
        result = SalesforceSink().push(
            DataEnvelope(content_type="text/plain", data="not-json", source_id="t"),
            sample_userpass_config,
        )
        assert isinstance(result, PushResult)
        assert result.success is False
