"""Shared test helpers for the DocuSign connector."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


def jwt_config() -> dict[str, Any]:
    return {
        "integration_key": "ikey-abc",
        "user_id": "user-guid-1",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
        "auth_server": "account-d.docusign.com",
    }


def auth_code_config() -> dict[str, Any]:
    return {
        "client_id": "ikey-abc",
        "client_secret": "secret-xyz",
        "refresh_token": "refresh-tok",
        "auth_server": "account-d.docusign.com",
    }


def make_envelope(
    envelope_id: str = "env-1",
    status: str = "completed",
    subject: str = "Please sign",
    sender_email: str = "sender@example.com",
    **extra: Any,
) -> MagicMock:
    env = MagicMock()
    env.envelope_id = envelope_id
    env.status = status
    env.email_subject = subject
    env.sender = MagicMock(email=sender_email)
    env.created_date_time = "2024-01-15T10:30:00Z"
    env.completed_date_time = "2024-06-01T08:45:00Z"
    env.to_dict.return_value = {
        "envelope_id": envelope_id,
        "status": status,
        "email_subject": subject,
        **extra,
    }
    return env


def make_document(
    document_id: str = "1",
    name: str = "contract.pdf",
) -> MagicMock:
    doc = MagicMock()
    doc.document_id = document_id
    doc.name = name
    return doc


def make_oauth_user_info(
    account_id: str = "account-xyz",
    base_uri: str = "https://demo.docusign.net",
    is_default: bool = True,
) -> MagicMock:
    account = MagicMock()
    account.account_id = account_id
    account.base_uri = base_uri
    account.is_default = is_default
    ui = MagicMock()
    ui.accounts = [account]
    return ui


def make_oauth_token(access_token: str = "access-token-fresh") -> MagicMock:
    tok = MagicMock()
    tok.access_token = access_token
    tok.refresh_token = "new-refresh"
    tok.expires_in = 3600
    return tok
