"""The Redshift credential blob: the shared AWS cross-account blob plus a ``redshift`` section.

The host (aisquare-pipe-service) hands over one server-side blob, the tenant
secret ``redshift``. Its top level is exactly the blob of
:mod:`aisquare_pipe_aws.credentials` (``role_arn``, ``external_id``,
``regions``), parsed by that module, so the role is assumed the one way every
AWS connector assumes it. The ``redshift`` section names what the Data API
connects to:

.. code-block:: json

    {"role_arn": "arn:aws:iam::210987654321:role/AISquareReadOnly",
     "external_id": "<issued by AISquare>",
     "regions": ["us-east-1"],
     "redshift": {"cluster_identifier": "analytics", "database": "policies"}}

- exactly one of ``cluster_identifier`` (a provisioned cluster) and
  ``workgroup_name`` (Redshift Serverless);
- ``database`` (required);
- how the database user is found, at most one of:

  - neither: the role's own IAM identity (``IAMR:<role name>``), through
    ``redshift:GetClusterCredentialsWithIAM`` (cluster) or
    ``redshift-serverless:GetCredentials`` (workgroup);
  - ``db_user`` (a cluster only): that database user, through
    ``redshift:GetClusterCredentials``;
  - ``secret_arn``: a Secrets Manager secret holding the database user's
    ``username`` and ``password``, in the role's own account;

- ``region`` (default: the first of ``regions``).

Nothing here is ever read from a caller: every key above is server-side
(:data:`SERVER_KEYS`), and a param or argument naming one is refused.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_aws.credentials import (
    CONFIG_KEYS,
    AwsNotConfiguredError,
    CrossAccountConfig,
)

#: botocore's own pattern for ``ClusterIdentifier`` (1-63 characters).
_CLUSTER = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_CLUSTER_MAX = 63
#: A workgroup NAME (botocore also takes an ARN; we don't, so the account
#: is always the role's own).
_WORKGROUP = re.compile(r"^[a-z0-9-]{3,63}$")
#: Redshift names: a database, a database user. Delimited names may hold
#: more, but the blob's never need to.
_DATABASE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,126}$")
_DB_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$.@+-]{0,127}$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
#: ``arn:aws:secretsmanager:<region>:<account>:secret:<name>-<6 chars>``.
_SECRET_ARN = re.compile(
    r"^arn:aws:secretsmanager:[a-z]{2}(-[a-z]+)+-\d{1,2}:"
    r"(?P<account>\d{12}):secret:[A-Za-z0-9/_+=.@-]{1,512}$"
)

#: The ``redshift`` section's keys. Anything else in it is refused.
REDSHIFT_KEYS = frozenset(
    {
        "cluster_identifier",
        "workgroup_name",
        "database",
        "db_user",
        "secret_arn",
        "region",
    }
)
#: Every key a caller may never set: the shared AWS blob's, the section's
#: name and its keys.
SERVER_KEYS = frozenset({*CONFIG_KEYS, "redshift", *REDSHIFT_KEYS})


class RedshiftNotConfiguredError(AwsNotConfiguredError):
    """The blob's ``redshift`` section is missing or malformed. Names the
    problem, never a value."""


@dataclass(frozen=True)
class RedshiftTarget:
    """What the Data API connects to. Exactly one of ``cluster_identifier``
    and ``workgroup_name``; at most one of ``db_user`` and ``secret_arn``."""

    database: str
    region: str
    cluster_identifier: str | None = None
    workgroup_name: str | None = None
    db_user: str | None = None
    secret_arn: str | None = None

    @property
    def auth_mode(self) -> str:
        """How the database user is found (the README's table):
        ``secret``, ``db_user``, ``iam_cluster`` or ``iam_serverless``."""
        if self.secret_arn:
            return "secret"
        if self.db_user:
            return "db_user"
        return "iam_cluster" if self.cluster_identifier else "iam_serverless"

    def connection(self) -> dict[str, str]:
        """The Data API's connection params, for every call (ExecuteStatement,
        ListSchemas, ListTables, DescribeTable)."""
        params: dict[str, str] = {"Database": self.database}
        if self.cluster_identifier:
            params["ClusterIdentifier"] = self.cluster_identifier
        if self.workgroup_name:
            params["WorkgroupName"] = self.workgroup_name
        if self.db_user:
            params["DbUser"] = self.db_user
        if self.secret_arn:
            params["SecretArn"] = self.secret_arn
        return params


@dataclass(frozen=True)
class RedshiftConfig:
    """A validated blob: the shared AWS credential and the Redshift target."""

    aws: CrossAccountConfig
    target: RedshiftTarget

    @classmethod
    def from_config(cls, config: Any) -> RedshiftConfig:
        """Parse the blob. Raises :class:`AwsNotConfiguredError` (the AWS
        part) or :class:`RedshiftNotConfiguredError` naming the first
        problem; never echoes a value."""
        aws = CrossAccountConfig.from_config(config)
        return cls(aws=aws, target=_target(config.get("redshift"), aws))


def _optional(section: Mapping[str, Any], key: str) -> str | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RedshiftNotConfiguredError(
            f"Redshift is not configured: redshift.{key} must be a non-empty string"
        )
    return value


def _target(value: Any, aws: CrossAccountConfig) -> RedshiftTarget:
    if not isinstance(value, Mapping):
        raise RedshiftNotConfiguredError(
            "Redshift is not configured: the credential blob has no redshift section"
        )
    unknown = sorted(set(value) - REDSHIFT_KEYS)
    if unknown:
        raise RedshiftNotConfiguredError(
            "Redshift is not configured: unknown key(s) in the redshift section "
            f"(allowed: {', '.join(sorted(REDSHIFT_KEYS))})"
        )
    cluster = _optional(value, "cluster_identifier")
    workgroup = _optional(value, "workgroup_name")
    if (cluster is None) == (workgroup is None):
        raise RedshiftNotConfiguredError(
            "Redshift is not configured: set exactly one of redshift.cluster_identifier "
            "(a provisioned cluster) and redshift.workgroup_name (Redshift Serverless)"
        )
    if cluster is not None and (
        len(cluster) > _CLUSTER_MAX or not _CLUSTER.match(cluster)
    ):
        raise RedshiftNotConfiguredError(
            "Redshift is not configured: redshift.cluster_identifier is not a cluster "
            "identifier (lowercase letters, digits and single hyphens, at most 63)"
        )
    if workgroup is not None and not _WORKGROUP.match(workgroup):
        raise RedshiftNotConfiguredError(
            "Redshift is not configured: redshift.workgroup_name is not a workgroup name "
            "(3-63 lowercase letters, digits and hyphens; not an ARN)"
        )
    database = _optional(value, "database")
    if database is None or not _DATABASE.match(database):
        raise RedshiftNotConfiguredError(
            "Redshift is not configured: redshift.database must be a database name"
        )
    db_user = _optional(value, "db_user")
    secret_arn = _optional(value, "secret_arn")
    if db_user is not None and secret_arn is not None:
        raise RedshiftNotConfiguredError(
            "Redshift is not configured: set at most one of redshift.db_user and "
            "redshift.secret_arn"
        )
    if db_user is not None:
        if workgroup is not None:
            raise RedshiftNotConfiguredError(
                "Redshift is not configured: redshift.db_user works with a cluster only; "
                "a workgroup maps the role to its database user, or use redshift.secret_arn"
            )
        if not _DB_USER.match(db_user):
            raise RedshiftNotConfiguredError(
                "Redshift is not configured: redshift.db_user is not a database user name"
            )
    region = _optional(value, "region") or aws.regions[0]
    if not _REGION.match(region):
        raise RedshiftNotConfiguredError(
            "Redshift is not configured: redshift.region is not a region name"
        )
    if secret_arn is not None:
        match = _SECRET_ARN.match(secret_arn)
        if match is None:
            raise RedshiftNotConfiguredError(
                "Redshift is not configured: redshift.secret_arn is not a Secrets Manager "
                "secret ARN (arn:aws:secretsmanager:<region>:<account>:secret:<name>)"
            )
        if match.group("account") != aws.account_id:
            raise RedshiftNotConfiguredError(
                "Redshift is not configured: redshift.secret_arn must be in the role's own "
                "AWS account"
            )
    return RedshiftTarget(
        database=database,
        region=region,
        cluster_identifier=cluster,
        workgroup_name=workgroup,
        db_user=db_user,
        secret_arn=secret_arn,
    )


def is_configured(config: Any) -> bool:
    """Offline shape check: True when ``config`` parses (no network)."""
    try:
        RedshiftConfig.from_config(config)
    except AwsNotConfiguredError:
        return False
    return True


def refuse_server_keys(names: Any, *, what: str) -> None:
    """Raise :class:`ConfigValidationError` when ``names`` (a caller's
    params) hold a server-side key. Names the key, never a value."""
    server = sorted(SERVER_KEYS.intersection(names))
    if server:
        raise ConfigValidationError(
            f"{what} {server[0]!r} is server-controlled and cannot be set by a caller"
        )
