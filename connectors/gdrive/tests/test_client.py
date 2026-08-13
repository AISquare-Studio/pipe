"""GDriveClient — credential resolution, request retry, error mapping."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_gdrive.client import GDriveClient


class TestCredentialResolution:
    def test_empty_config_raises_config_validation_error(self):
        with pytest.raises(ConfigValidationError):
            GDriveClient({})

    def test_access_token_config_builds_client(self, sample_config, mock_session):
        client = GDriveClient(sample_config)
        assert client._session is mock_session

    def test_refresh_token_config_builds_client(self, sample_config_refresh, mock_session):
        client = GDriveClient(sample_config_refresh)
        assert client._session is mock_session

    def test_service_account_dict_builds_client(self, sample_service_account, mock_session):
        with patch(
            "aisquare_pipe_gdrive.client.service_account.Credentials."
            "from_service_account_info"
        ) as from_info:
            from_info.return_value = MagicMock()
            GDriveClient({"service_account_json": sample_service_account})
        from_info.assert_called_once()

    def test_service_account_path_builds_client(self, tmp_path, sample_service_account, mock_session):
        import json

        key_file = tmp_path / "svc.json"
        key_file.write_text(json.dumps(sample_service_account))
        with patch(
            "aisquare_pipe_gdrive.client.service_account.Credentials."
            "from_service_account_file"
        ) as from_file:
            from_file.return_value = MagicMock()
            GDriveClient({"service_account_json": str(key_file)})
        from_file.assert_called_once()

    def test_service_account_subject_applies_delegation(
        self, sample_service_account, mock_session
    ):
        creds = MagicMock()
        with patch(
            "aisquare_pipe_gdrive.client.service_account.Credentials."
            "from_service_account_info"
        ) as from_info:
            from_info.return_value = creds
            GDriveClient(
                {
                    "service_account_json": sample_service_account,
                    "subject": "user@example.com",
                }
            )
        creds.with_subject.assert_called_once_with("user@example.com")

    def test_service_account_takes_precedence_over_refresh_token(
        self, sample_service_account, sample_config_refresh, mock_session
    ):
        merged = {**sample_config_refresh, "service_account_json": sample_service_account}
        with patch(
            "aisquare_pipe_gdrive.client.service_account.Credentials."
            "from_service_account_info"
        ) as from_info:
            from_info.return_value = MagicMock()
            GDriveClient(merged)
        from_info.assert_called_once()


class TestRequest:
    def test_success_returns_response(self, sample_config, mock_session):
        resp = MagicMock(status_code=200)
        mock_session.request.return_value = resp
        client = GDriveClient(sample_config)
        assert client._request("GET", "https://example.test") is resp

    def test_401_raises_config_validation_error(self, sample_config, mock_session):
        mock_session.request.return_value = MagicMock(status_code=401, text="bad creds")
        client = GDriveClient(sample_config)
        with pytest.raises(ConfigValidationError):
            client._request("GET", "https://example.test")

    def test_404_raises_pipeline_error(self, sample_config, mock_session):
        mock_session.request.return_value = MagicMock(status_code=404, text="not found")
        client = GDriveClient(sample_config)
        with pytest.raises(PipelineError):
            client._request("GET", "https://example.test")

    def test_429_retries_then_succeeds(self, sample_config, mock_session):
        throttled = MagicMock(status_code=429, headers={"Retry-After": "0"})
        ok = MagicMock(status_code=200)
        mock_session.request.side_effect = [throttled, ok]
        client = GDriveClient(sample_config)
        with patch("aisquare_pipe_gdrive.client.time.sleep"):
            assert client._request("GET", "https://example.test") is ok
        assert mock_session.request.call_count == 2

    def test_persistent_429_raises_after_max_retries(self, sample_config, mock_session):
        mock_session.request.return_value = MagicMock(
            status_code=429, headers={"Retry-After": "0"}
        )
        client = GDriveClient(sample_config)
        with patch("aisquare_pipe_gdrive.client.time.sleep"):
            with pytest.raises(PipelineError):
                client._request("GET", "https://example.test")

    def test_error_body_is_truncated(self, sample_config, mock_session):
        # 404 is not in _RETRY_STATUS, so this hits the immediate-raise path
        # (via _body_tail) rather than the retry-exhaustion path, which is
        # what actually exercises truncation.
        mock_session.request.return_value = MagicMock(status_code=404, text="x" * 5000)
        client = GDriveClient(sample_config)
        with patch("aisquare_pipe_gdrive.client.time.sleep"):
            with pytest.raises(PipelineError) as exc:
                client._request("GET", "https://example.test")
        message = str(exc.value)
        assert len(message) < 1200
        assert message.count("x") < 5000
