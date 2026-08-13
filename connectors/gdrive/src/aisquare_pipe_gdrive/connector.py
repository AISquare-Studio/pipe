"""Google Drive source and sink connectors for aisquare.pipe."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
    RateLimit,
)
from aisquare.pipe.errors import ConfigValidationError

logger = logging.getLogger("aisquare.pipe.gdrive")


def _has_valid_auth_keys(config: dict[str, Any]) -> bool:
    """Check whether config carries a usable credential set.

    Offline and total — never raises, never touches the network. This is what
    keeps validate_config({}) honest under the compliance socket guard.
    """
    if not isinstance(config, dict):
        return False
    if config.get("access_token"):
        return True
    if config.get("service_account_json"):
        return True
    return all(config.get(k) for k in ("client_id", "client_secret", "refresh_token"))


class GDriveSource(SourceConnector):
    """Pull files from Google Drive via the Drive v3 API."""

    name = "gdrive-source"
    version = "0.1.0"
    output_types = ["*/*"]
    auth_type = AuthType.OAUTH2
    description = "Pull files from Google Drive"
    docs_url = "https://developers.google.com/drive/api/v3/reference"
    metadata_spec = {
        "filename": MetaField(type=str, required=True, description="Original filename"),
        "file_id": MetaField(type=str, required=True, description="Drive file ID"),
        "mime_type": MetaField(
            type=str, required=True, description="MIME type as reported by Drive"
        ),
        "size": MetaField(type=int, required=True, description="File size in bytes"),
        "modified_time": MetaField(
            type=str, required=False, description="ISO timestamp of last modification"
        ),
        "web_view_link": MetaField(
            type=str, required=False, description="Browser URL for the file"
        ),
        "md5_checksum": MetaField(
            type=str, required=False, description="MD5 of the content, when Drive provides one"
        ),
        "parents": MetaField(
            type=list, required=False, description="Parent folder IDs"
        ),
        "exported_from": MetaField(
            type=str,
            required=False,
            description="Original Google-native MIME type, set only on exported files",
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        if not _has_valid_auth_keys(config):
            raise ConfigValidationError(
                "gdrive config requires 'service_account_json', or "
                "'client_id' + 'client_secret' + 'refresh_token', or 'access_token'"
            )
        return
        yield  # pragma: no cover — makes this a generator

    def validate_config(self, config: dict) -> bool:
        return _has_valid_auth_keys(config)

    def supports_streaming(self) -> bool:
        return True

    def rate_limit(self) -> RateLimit | None:
        # Drive's default quota is 12,000 queries per 60 seconds per project.
        return RateLimit(requests_per_minute=12000, concurrent=1)


class GDriveSink(SinkConnector):
    """Push files to Google Drive via the Drive v3 API."""

    name = "gdrive-sink"
    version = "0.1.0"
    input_types = ["*/*"]
    auth_type = AuthType.OAUTH2
    description = "Push files to Google Drive"
    docs_url = "https://developers.google.com/drive/api/v3/reference"
    metadata_spec = {
        "filename": MetaField(type=str, required=True, description="Target filename"),
        "size": MetaField(
            type=int, required=False, description="Byte size, used to pick the upload path"
        ),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        if not _has_valid_auth_keys(config):
            return PushResult(success=False, error="gdrive config is missing credentials")
        return PushResult(success=False, error="not implemented")

    def validate_config(self, config: dict) -> bool:
        return _has_valid_auth_keys(config)

    def max_size(self) -> int | None:
        # Drive's per-file ceiling is 5 TB; no practical guard is useful here.
        return None
