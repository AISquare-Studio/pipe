"""GDriveClient — credential resolution, request retry, error mapping."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_gdrive.client import GDriveClient
from tests.helpers import make_file, make_folder, make_list_response


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


def _json_response(payload: dict) -> MagicMock:
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    return resp


class TestListFiles:
    def test_yields_files_from_single_page(self, sample_config, mock_session):
        files = [make_file(name="a.pdf"), make_file(name="b.txt", file_id="file-2")]
        mock_session.request.return_value = _json_response(make_list_response(files))
        client = GDriveClient(sample_config)
        assert [f["name"] for f in client.list_files()] == ["a.pdf", "b.txt"]

    def test_follows_pagination(self, sample_config, mock_session):
        page1 = make_list_response([make_file(name="a.pdf")], next_page_token="tok")
        page2 = make_list_response([make_file(name="b.pdf", file_id="file-2")])
        mock_session.request.side_effect = [
            _json_response(page1),
            _json_response(page2),
        ]
        client = GDriveClient(sample_config)
        assert [f["name"] for f in client.list_files()] == ["a.pdf", "b.pdf"]
        assert mock_session.request.call_count == 2

    def test_scopes_query_to_parent_folder(self, sample_config, mock_session):
        mock_session.request.return_value = _json_response(make_list_response([]))
        client = GDriveClient(sample_config)
        list(client.list_files(folder_id="folder-xyz"))
        sent = mock_session.request.call_args.kwargs["params"]["q"]
        assert "'folder-xyz' in parents" in sent
        assert "trashed = false" in sent

    def test_extra_query_is_conjoined(self, sample_config, mock_session):
        mock_session.request.return_value = _json_response(make_list_response([]))
        client = GDriveClient(sample_config)
        list(client.list_files(query="mimeType = 'application/pdf'"))
        sent = mock_session.request.call_args.kwargs["params"]["q"]
        assert "mimeType = 'application/pdf'" in sent
        assert " and " in sent

    def test_recursive_descends_into_subfolders(self, sample_config, mock_session):
        root_page = make_list_response([make_folder(file_id="sub-1"), make_file()])
        sub_page = make_list_response([make_file(name="nested.txt", file_id="file-9")])
        mock_session.request.side_effect = [
            _json_response(root_page),
            _json_response(sub_page),
        ]
        client = GDriveClient(sample_config)
        names = [f["name"] for f in client.list_files(recursive=True)]
        assert "nested.txt" in names

    def test_non_recursive_does_not_descend(self, sample_config, mock_session):
        mock_session.request.return_value = _json_response(
            make_list_response([make_folder(file_id="sub-1")])
        )
        client = GDriveClient(sample_config)
        list(client.list_files(recursive=False))
        assert mock_session.request.call_count == 1

    def test_shared_drive_params_included_when_drive_id_set(self, sample_config, mock_session):
        mock_session.request.return_value = _json_response(make_list_response([]))
        client = GDriveClient({**sample_config, "drive_id": "drive-123"})
        list(client.list_files())
        params = mock_session.request.call_args.kwargs["params"]
        assert params["driveId"] == "drive-123"
        assert params["corpora"] == "drive"
        assert params["includeItemsFromAllDrives"] is True
        assert params["supportsAllDrives"] is True


class TestDownloadAndExport:
    def test_download_returns_bytes(self, sample_config, mock_session):
        resp = MagicMock(status_code=200, content=b"hello")
        mock_session.request.return_value = resp
        client = GDriveClient(sample_config)
        assert client.download("file-1") == b"hello"
        assert mock_session.request.call_args.kwargs["params"]["alt"] == "media"

    def test_download_stream_returns_readable(self, sample_config, mock_session):
        resp = MagicMock(status_code=200)
        resp.raw = MagicMock()
        mock_session.request.return_value = resp
        client = GDriveClient(sample_config)
        stream = client.download_stream("file-1")
        assert hasattr(stream, "read")
        assert mock_session.request.call_args.kwargs["stream"] is True

    def test_export_returns_bytes_and_sends_target_mime(self, sample_config, mock_session):
        resp = MagicMock(status_code=200, content=b"docx-bytes")
        mock_session.request.return_value = resp
        client = GDriveClient(sample_config)
        target = (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        )
        assert client.export("doc-1", target) == b"docx-bytes"
        assert mock_session.request.call_args.kwargs["params"]["mimeType"] == target

    def test_export_size_limit_raises_clear_pipeline_error(self, sample_config, mock_session):
        mock_session.request.return_value = MagicMock(
            status_code=403,
            text='{"error": {"errors": [{"reason": "exportSizeLimitExceeded"}]}}',
        )
        client = GDriveClient(sample_config)
        with pytest.raises(PipelineError) as exc:
            client.export("doc-1", "application/pdf")
        assert "10 MB" in str(exc.value)
        assert "doc-1" in str(exc.value)
