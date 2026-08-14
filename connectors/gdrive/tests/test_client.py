"""GDriveClient — credential resolution, request retry, error mapping."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_gdrive.client import GDriveClient
from aisquare_pipe_gdrive.constants import FOLDER_MIME
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


class TestUpload:
    def test_should_chunk_threshold(self, sample_config, mock_session):
        client = GDriveClient(sample_config)
        assert client.should_chunk(6 * 1024 * 1024) is True
        assert client.should_chunk(1024) is False

    def test_upload_sends_multipart_and_returns_file(self, sample_config, mock_session):
        mock_session.request.return_value = _json_response(
            {"id": "new-1", "name": "a.txt", "webViewLink": "https://drive/x"}
        )
        client = GDriveClient(sample_config)
        result = client.upload("a.txt", "root", "text/plain", b"hello")
        assert result["id"] == "new-1"
        assert mock_session.request.call_args.kwargs["params"]["uploadType"] == "multipart"

    def test_update_content_patches_existing_file(self, sample_config, mock_session):
        mock_session.request.return_value = _json_response({"id": "old-1"})
        client = GDriveClient(sample_config)
        assert client.update_content("old-1", "text/plain", b"new")["id"] == "old-1"
        assert mock_session.request.call_args.args[0] == "PATCH"

    def test_upload_resumable_uploads_in_chunks(self, sample_config, mock_session):
        import io

        session_resp = MagicMock(status_code=200, headers={"Location": "https://upload/x"})
        final = MagicMock(status_code=200)
        final.json.return_value = {"id": "big-1"}
        mock_session.request.side_effect = [session_resp, final]
        client = GDriveClient(sample_config)
        payload = b"z" * 1024
        result = client.upload_resumable(
            "big.bin", "root", "application/octet-stream", io.BytesIO(payload), len(payload)
        )
        assert result["id"] == "big-1"

    def test_upload_resumable_multi_chunk_308_flow(self, sample_config, mock_session):
        import io
        from unittest.mock import patch

        session_resp = MagicMock(status_code=200, headers={"Location": "https://upload/x"})
        chunk1_308 = MagicMock(status_code=308)
        chunk2_200 = MagicMock(status_code=200)
        chunk2_200.json.return_value = {"id": "big-1"}
        mock_session.request.side_effect = [session_resp, chunk1_308, chunk2_200]
        client = GDriveClient(sample_config)
        payload = b"z" * 1024
        with patch("aisquare_pipe_gdrive.client.CHUNK_SIZE", 512):
            result = client.upload_resumable(
                "big.bin", "root", "application/octet-stream", io.BytesIO(payload), len(payload)
            )
        assert result["id"] == "big-1"
        # Verify two chunk PUTs were made (both called with PUT method)
        chunk_calls = [c for c in mock_session.request.call_args_list if c[0][0] == "PUT"]
        assert len(chunk_calls) == 2
        # Verify Content-Range headers advance correctly
        assert "bytes 0-511/1024" in str(chunk_calls[0])
        assert "bytes 512-1023/1024" in str(chunk_calls[1])

    def test_upload_resumable_retry_exhaustion_raises(self, sample_config, mock_session):
        import io

        session_resp = MagicMock(status_code=200, headers={"Location": "https://upload/x"})
        # Simulate persistent 503 responses
        throttled = MagicMock(status_code=503, headers={})
        mock_session.request.side_effect = [session_resp] + [throttled] * 10
        client = GDriveClient(sample_config)
        payload = b"z" * 1024
        with patch("aisquare_pipe_gdrive.client.time.sleep"):
            with pytest.raises(PipelineError) as exc:
                client.upload_resumable(
                    "big.bin", "root", "application/octet-stream", io.BytesIO(payload), len(payload)
                )
        assert "exhausted" in str(exc.value).lower()

    def test_upload_resumable_unexpected_status_raises(self, sample_config, mock_session):
        import io

        session_resp = MagicMock(status_code=200, headers={"Location": "https://upload/x"})
        error_resp = MagicMock(status_code=400, text="invalid request")
        mock_session.request.side_effect = [session_resp, error_resp]
        client = GDriveClient(sample_config)
        payload = b"z" * 1024
        with pytest.raises(PipelineError) as exc:
            client.upload_resumable(
                "big.bin", "root", "application/octet-stream", io.BytesIO(payload), len(payload)
            )
        assert "chunk failed" in str(exc.value).lower()

    def test_find_child_returns_none_when_absent(self, sample_config, mock_session):
        mock_session.request.return_value = _json_response({"files": []})
        client = GDriveClient(sample_config)
        assert client.find_child("nope.txt", "root") is None

    def test_find_child_returns_first_match(self, sample_config, mock_session):
        mock_session.request.return_value = _json_response(
            {"files": [{"id": "f-1", "name": "a.txt"}]}
        )
        client = GDriveClient(sample_config)
        assert client.find_child("a.txt", "root")["id"] == "f-1"

    def test_ensure_folder_path_creates_missing_segments(self, sample_config, mock_session):
        # "a/b": a is missing (create), b is missing (create).
        mock_session.request.side_effect = [
            _json_response({"files": []}),
            _json_response({"id": "folder-a"}),
            _json_response({"files": []}),
            _json_response({"id": "folder-b"}),
        ]
        client = GDriveClient(sample_config)
        assert client.ensure_folder_path("a/b", "root") == "folder-b"

    def test_ensure_folder_path_reuses_existing(self, sample_config, mock_session):
        mock_session.request.return_value = _json_response(
            {"files": [{"id": "folder-a", "name": "a", "mimeType": FOLDER_MIME}]}
        )
        client = GDriveClient(sample_config)
        assert client.ensure_folder_path("a", "root") == "folder-a"

    def test_ensure_folder_path_empty_returns_parent(self, sample_config, mock_session):
        client = GDriveClient(sample_config)
        assert client.ensure_folder_path("", "root") == "root"
