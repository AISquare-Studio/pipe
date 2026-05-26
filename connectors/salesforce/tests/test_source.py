"""Tests for SalesforceSource."""

from __future__ import annotations

import pytest

from aisquare.pipe.core.envelope import PullParams, RateLimit

from aisquare_pipe_salesforce.connector import SalesforceSource, _build_soql

from tests.helpers import make_record


class TestBuildSOQL:
    def test_minimal(self):
        soql = _build_soql("Account", ["Id"], None, None, None, None)
        assert soql == "SELECT Id FROM Account"

    def test_multiple_fields(self):
        soql = _build_soql("Contact", ["Id", "Email", "Name"], None, None, None, None)
        assert soql == "SELECT Id, Email, Name FROM Contact"

    def test_where_clause(self):
        soql = _build_soql("Account", ["Id"], "Industry = 'Tech'", None, None, None)
        assert "WHERE (Industry = 'Tech')" in soql

    def test_modified_after_appended(self):
        soql = _build_soql("Account", ["Id"], None, None, None, "2024-01-01T00:00:00Z")
        assert "LastModifiedDate > 2024-01-01T00:00:00Z" in soql

    def test_where_and_modified_after_combined(self):
        soql = _build_soql(
            "Account", ["Id"], "Industry = 'Tech'", None, None, "2024-01-01T00:00:00Z"
        )
        assert "(Industry = 'Tech') AND LastModifiedDate > 2024-01-01T00:00:00Z" in soql

    def test_order_by_and_limit(self):
        soql = _build_soql("Account", ["Id"], None, "CreatedDate DESC", 50, None)
        assert "ORDER BY CreatedDate DESC" in soql
        assert "LIMIT 50" in soql

    def test_custom_object_supported(self):
        soql = _build_soql("Foo__c", ["Id", "Bar__c"], None, None, None, None)
        assert soql == "SELECT Id, Bar__c FROM Foo__c"


class TestSalesforceSourcePull:
    def test_yields_envelopes_with_clean_data(self, mock_client, sample_userpass_config):
        mock_client.query_iter.return_value = iter(
            [make_record("001A", "Acme"), make_record("001B", "Globex")]
        )

        source = SalesforceSource()
        params = PullParams(params={"object_type": "Account"})
        envelopes = list(source.pull(sample_userpass_config, params))

        assert len(envelopes) == 2
        assert envelopes[0].content_type == "application/json"
        # attributes key stripped
        assert "attributes" not in envelopes[0].data
        assert envelopes[0].data["Id"] == "001A"
        assert envelopes[0].metadata["salesforce_id"] == "001A"
        assert envelopes[0].metadata["object_type"] == "Account"
        assert envelopes[0].metadata["created_date"] == "2024-01-15T10:30:00.000+0000"
        assert envelopes[0].source_id == "salesforce-source"

    def test_missing_object_type_raises(self, mock_client, sample_userpass_config):
        source = SalesforceSource()
        gen = source.pull(sample_userpass_config, PullParams())
        with pytest.raises(ValueError):
            next(gen)

    def test_builds_soql_from_params(self, mock_client, sample_userpass_config):
        mock_client.query_iter.return_value = iter([])
        source = SalesforceSource()
        params = PullParams(params={
            "object_type": "Account",
            "fields": ["Id", "Name"],
            "where": "Industry = 'Tech'",
            "limit": 10,
        })
        list(source.pull(sample_userpass_config, params))

        soql_used = mock_client.query_iter.call_args.args[0]
        assert "SELECT Id, Name FROM Account" in soql_used
        assert "Industry = 'Tech'" in soql_used
        assert "LIMIT 10" in soql_used

    def test_soql_escape_hatch_used_verbatim(self, mock_client, sample_userpass_config):
        mock_client.query_iter.return_value = iter([])
        source = SalesforceSource()
        params = PullParams(params={
            "object_type": "Account",
            "soql": "SELECT Id, (SELECT Id FROM Contacts) FROM Account",
            # These should be ignored when soql is supplied
            "fields": ["ShouldBeIgnored"],
            "where": "Should be ignored",
        })
        list(source.pull(sample_userpass_config, params))

        soql_used = mock_client.query_iter.call_args.args[0]
        assert soql_used == "SELECT Id, (SELECT Id FROM Contacts) FROM Account"

    def test_custom_object_supported(self, mock_client, sample_userpass_config):
        mock_client.query_iter.return_value = iter(
            [make_record("a01A", "Custom row", object_type="Foo__c")]
        )
        source = SalesforceSource()
        params = PullParams(params={"object_type": "Foo__c"})
        envelopes = list(source.pull(sample_userpass_config, params))
        assert envelopes[0].metadata["object_type"] == "Foo__c"


class TestSalesforceSourceValidate:
    def test_returns_false_for_empty_config(self):
        assert SalesforceSource().validate_config({}) is False

    def test_returns_true_when_client_validates(self, mock_client, sample_userpass_config):
        mock_client.validate.return_value = True
        assert SalesforceSource().validate_config(sample_userpass_config) is True

    def test_returns_false_when_client_raises(self, sample_userpass_config):
        from unittest.mock import patch
        with patch("aisquare_pipe_salesforce.connector.SalesforceClient") as mock_cls:
            mock_cls.side_effect = Exception("connection failed")
            assert SalesforceSource().validate_config(sample_userpass_config) is False


class TestSalesforceSourceMisc:
    def test_rate_limit_returned(self):
        rl = SalesforceSource().rate_limit()
        assert isinstance(rl, RateLimit)
        assert rl.requests_per_second == 10

    def test_list_resources_filters_to_queryable(self, mock_client, sample_userpass_config):
        mock_client.describe_sobjects.return_value = [
            {"name": "Account", "label": "Account", "queryable": True, "custom": False,
             "createable": True, "updateable": True},
            {"name": "DeletedThing", "label": "Deleted", "queryable": False},
            {"name": "Foo__c", "label": "Foo", "queryable": True, "custom": True,
             "createable": True, "updateable": True},
        ]
        resources = SalesforceSource().list_resources(sample_userpass_config)
        names = [r.id for r in resources]
        assert names == ["Account", "Foo__c"]
        assert resources[1].metadata["custom"] is True
