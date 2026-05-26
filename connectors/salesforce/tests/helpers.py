"""Shared test helpers for the Salesforce connector."""

from __future__ import annotations

from typing import Any


def make_record(
    record_id: str = "001xx000003DGb1AAG",
    name: str = "Acme Corp",
    object_type: str = "Account",
    **extra: Any,
) -> dict[str, Any]:
    """Build a Salesforce-shaped record dict (matches simple_salesforce output, attributes included)."""
    rec: dict[str, Any] = {
        "attributes": {"type": object_type, "url": f"/services/data/v55.0/sobjects/{object_type}/{record_id}"},
        "Id": record_id,
        "Name": name,
        "CreatedDate": "2024-01-15T10:30:00.000+0000",
        "LastModifiedDate": "2024-06-01T08:45:00.000+0000",
    }
    rec.update(extra)
    return rec


def userpass_config() -> dict[str, str]:
    return {
        "username": "test@example.com",
        "password": "secret",
        "security_token": "tok",
    }


def oauth_config() -> dict[str, str]:
    return {
        "client_id": "id",
        "client_secret": "sec",
        "refresh_token": "ref",
        "instance_url": "https://test.my.salesforce.com",
    }
