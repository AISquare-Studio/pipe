"""GDriveSink.push — data coercion, conflict modes, failure reporting."""

from __future__ import annotations

import io
from unittest import mock

import pytest

from aisquare.pipe.core.envelope import DataEnvelope, PushParams

from aisquare_pipe_gdrive.connector import GDriveSink

CONFIG = {"access_token": "test-token"}


@pytest.fixture()
def fake_client():
    inst = mock.Mock()
    inst.should_chunk.side_effect = lambda size: size > 5 * 1024 * 1024
    inst.ensure_folder_path.side_effect = lambda path, parent: parent
    inst.find_child.return_value = None
    inst.upload.return_value = {
        "id": "new-1",
        "name": "a.txt",
        "webViewLink": "https://drive/x",
    }
    inst.update_content.return_value = {"id": "old-1", "name": "a.txt"}
    inst.upload_resumable.return_value = {"id": "big-1", "name": "big.bin"}
    with mock.patch("aisquare_pipe_gdrive.connector.GDriveClient") as cls:
        cls.return_value = inst
        yield inst


def _envelope(data=b"hello", filename="a.txt", content_type="text/plain", stream=None):
    return DataEnvelope(
        content_type=content_type,
        data=data,
        source_id="test",
        metadata={"filename": filename},
        stream=stream,
    )


class TestPush:
    def test_uploads_bytes_and_returns_success(self, fake_client):
        result = GDriveSink().push(_envelope(), dict(CONFIG))
        assert result.success is True
        assert result.ref == "new-1"
        assert result.metadata["web_view_link"] == "https://drive/x"

    def test_str_data_is_encoded(self, fake_client):
        GDriveSink().push(_envelope(data="hello"), dict(CONFIG))
        assert fake_client.upload.call_args.args[3] == b"hello"

    def test_dict_data_is_json_encoded(self, fake_client):
        GDriveSink().push(_envelope(data={"k": "v"}), dict(CONFIG))
        assert fake_client.upload.call_args.args[3] == b'{"k": "v"}'

    def test_large_bytes_use_resumable_upload(self, fake_client):
        big = b"z" * (6 * 1024 * 1024)
        result = GDriveSink().push(_envelope(data=big, filename="big.bin"), dict(CONFIG))
        assert result.ref == "big-1"
        fake_client.upload_resumable.assert_called_once()
        fake_client.upload.assert_not_called()

    def test_stream_envelope_uses_resumable_upload(self, fake_client):
        env = _envelope(data=b"", filename="big.bin", stream=io.BytesIO(b"z" * 100))
        env.metadata["size"] = 100
        GDriveSink().push(env, dict(CONFIG))
        fake_client.upload_resumable.assert_called_once()

    def test_target_path_is_resolved(self, fake_client):
        params = PushParams(params={"target_path": "reports/2026"})
        GDriveSink().push(_envelope(), dict(CONFIG), params)
        fake_client.ensure_folder_path.assert_called_once_with("reports/2026", "root")

    def test_missing_filename_is_a_clean_failure(self, fake_client):
        env = DataEnvelope(
            content_type="text/plain", data=b"x", source_id="test", metadata={}
        )
        result = GDriveSink().push(env, dict(CONFIG))
        assert result.success is False
        assert "filename" in (result.error or "")

    def test_client_exception_becomes_failure_result(self, fake_client):
        fake_client.upload.side_effect = RuntimeError("boom")
        result = GDriveSink().push(_envelope(), dict(CONFIG))
        assert result.success is False
        assert "boom" in (result.error or "")

    def test_unsupported_data_type_is_a_clean_failure(self, fake_client):
        env = DataEnvelope(
            content_type="text/plain",
            data=12345,  # type: ignore[arg-type]
            source_id="test",
            metadata={"filename": "a.txt"},
        )
        result = GDriveSink().push(env, dict(CONFIG))
        assert result.success is False
        assert "Unsupported data type" in (result.error or "")


class TestConflictModes:
    def test_fail_mode_refuses_when_file_exists(self, fake_client):
        fake_client.find_child.return_value = {"id": "old-1", "name": "a.txt"}
        result = GDriveSink().push(_envelope(), dict(CONFIG))
        assert result.success is False
        assert "already exists" in (result.error or "")
        fake_client.upload.assert_not_called()

    def test_overwrite_mode_updates_existing(self, fake_client):
        fake_client.find_child.return_value = {"id": "old-1", "name": "a.txt"}
        params = PushParams(params={"conflict": "overwrite"})
        result = GDriveSink().push(_envelope(), dict(CONFIG), params)
        assert result.success is True
        assert result.ref == "old-1"
        fake_client.update_content.assert_called_once()

    def test_rename_mode_uploads_with_suffix(self, fake_client):
        fake_client.find_child.side_effect = [
            {"id": "old-1", "name": "a.txt"},
            None,
        ]
        params = PushParams(params={"conflict": "rename"})
        result = GDriveSink().push(_envelope(), dict(CONFIG), params)
        assert result.success is True
        assert fake_client.upload.call_args.args[0] == "a (1).txt"

    def test_no_conflict_check_when_file_absent(self, fake_client):
        result = GDriveSink().push(_envelope(), dict(CONFIG))
        assert result.success is True
        fake_client.upload.assert_called_once()


class TestValidateConfig:
    def test_empty_config_is_false(self):
        assert GDriveSink().validate_config({}) is False

    def test_max_size_is_none(self):
        assert GDriveSink().max_size() is None
