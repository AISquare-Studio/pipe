"""The shared AWS cross-account credential: assume a customer's IAM role.

Every connector and toolkit in this package reads a customer's AWS account the
same way. The host (aisquare-pipe-service) hands over one server-side blob,
never anything a caller sent:

.. code-block:: json

    {"role_arn": "arn:aws:iam::123456789012:role/AISquareReadOnly",
     "external_id": "<issued by AISquare>",
     "regions": ["us-east-1"],
     "s3": {"bucket": "policy-docs", "prefix": "rulebooks/", "region": "us-east-1"}}

From it, :func:`assume_role_session` builds a boto3 session whose ONLY
credentials are the customer role's temporary ones, from STS ``AssumeRole``
with the ``ExternalId``. The ambient credentials of this process (the
service's own pod role) are used for exactly two STS calls, ``GetCallerIdentity``
and ``AssumeRole``, and never to read customer data:

- a role in the service's own AWS account is refused, so a blob can't point
  the service at its own resources (nor one in an account the host lists in
  ``PIPE_AWS_REFUSED_ACCOUNT_IDS``, for AISquare's other accounts);
- no customer client is ever built from default, empty or ambient
  credentials: without a valid blob, or without a service principal, every
  entry point fails closed with :class:`AwsNotConfiguredError`.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from aisquare.pipe.errors import ConfigValidationError, PipeError

logger = logging.getLogger("aisquare.pipe.aws")

#: ``arn:aws:iam::<account>:role/<path/name>``. Only the commercial partition:
#: the service's principal is a commercial one, and STS can't assume a role
#: across partitions (aws-cn, aws-us-gov), so such a role could never work.
_ROLE_ARN = re.compile(
    r"^arn:(?P<partition>aws):iam::(?P<account>\d{12}):role/[\w+=,.@/-]{1,512}$"
)
#: The shortest external ID accepted. STS allows 2, but AISquare issues random
#: ones of at least this length (``secrets.token_urlsafe(32)`` gives 43), so a
#: short one is not one AISquare issued.
EXTERNAL_ID_MIN = 32
#: STS's ExternalId alphabet, at most 1224 characters.
_EXTERNAL_ID = re.compile(rf"^[\w+=,.@:/-]{{{EXTERNAL_ID_MIN},1224}}$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
#: S3 bucket naming rules (the DNS-compatible ones every new bucket follows).
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_MAX_PREFIX = 1024
_MAX_REGIONS = 32

#: Server-side setting: more AWS account ids (comma-separated) whose roles are
#: refused like the service's own account, e.g. AISquare's other accounts.
REFUSED_ACCOUNTS_ENV = "PIPE_AWS_REFUSED_ACCOUNT_IDS"
_ACCOUNT_ID = re.compile(r"^\d{12}$")

#: What CloudTrail in the customer's account shows for every call we make.
ROLE_SESSION_NAME = "aisquare-pipe"
#: One hour, the default maximum session of an IAM role. Longer reads refresh
#: the session before it lapses (botocore refreshable credentials).
SESSION_SECONDS = 3600

#: The blob's top-level keys. Nothing else in it is read.
CONFIG_KEYS = ("role_arn", "external_id", "regions", "s3")


class AwsNotConfiguredError(ConfigValidationError):
    """The AWS credential blob is missing or malformed, or this host has no
    AWS principal to assume the role from. Names the problem, never a value."""


class AwsAccessError(PipeError):
    """AWS refused a call. ``code`` is one of the toolkit error codes
    (``forbidden``, ``invalid_arguments``, ``not_found``, ``rate_limited``,
    ``unauthorized``, ``upstream_error``); ``aws_code`` is AWS's own error
    code."""

    def __init__(self, message: str, *, code: str, aws_code: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.aws_code = aws_code


@dataclass(frozen=True)
class S3Location:
    bucket: str
    prefix: str
    region: str


@dataclass(frozen=True)
class CrossAccountConfig:
    """A validated credential blob. ``account_id`` and ``partition`` come
    from the role ARN. ``external_id`` is left out of ``repr``."""

    role_arn: str
    external_id: str = field(repr=False)
    regions: tuple[str, ...]
    account_id: str
    partition: str
    s3: S3Location | None = None

    @classmethod
    def from_config(cls, config: Any) -> CrossAccountConfig:
        """Parse the blob. Raises :class:`AwsNotConfiguredError` naming the
        first problem; never echoes a value."""
        if not isinstance(config, Mapping):
            raise AwsNotConfiguredError("AWS is not configured: no credential blob")
        role_arn = config.get("role_arn")
        if not isinstance(role_arn, str) or not role_arn.strip():
            raise AwsNotConfiguredError("AWS is not configured: role_arn is missing")
        match = _ROLE_ARN.match(role_arn.strip())
        if match is None:
            raise AwsNotConfiguredError(
                "AWS is not configured: role_arn is not an IAM role ARN in the AWS commercial "
                "partition (arn:aws:iam::<12-digit account>:role/<name>)"
            )
        external_id = config.get("external_id")
        if not isinstance(external_id, str) or not external_id:
            raise AwsNotConfiguredError("AWS is not configured: external_id is missing")
        if not _EXTERNAL_ID.match(external_id):
            raise AwsNotConfiguredError(
                f"AWS is not configured: external_id must be {EXTERNAL_ID_MIN}-1224 characters "
                "of letters, digits and +=,.@:/-_ (AISquare issues it)"
            )
        regions = _regions(config.get("regions"))
        s3 = _s3_location(config.get("s3"), regions)
        return cls(
            role_arn=role_arn.strip(),
            external_id=external_id,
            regions=regions,
            account_id=match.group("account"),
            partition=match.group("partition"),
            s3=s3,
        )


def _regions(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise AwsNotConfiguredError(
            "AWS is not configured: regions must be a non-empty list"
        )
    if len(value) > _MAX_REGIONS:
        raise AwsNotConfiguredError(
            f"AWS is not configured: at most {_MAX_REGIONS} regions"
        )
    out: list[str] = []
    for region in value:
        if not isinstance(region, str) or not _REGION.match(region):
            raise AwsNotConfiguredError(
                "AWS is not configured: each region must be a region name such as us-east-1"
            )
        if region not in out:
            out.append(region)
    return tuple(out)


def _s3_location(value: Any, regions: tuple[str, ...]) -> S3Location | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AwsNotConfiguredError("AWS is not configured: s3 must be an object")
    bucket = value.get("bucket")
    if not isinstance(bucket, str) or not _BUCKET.match(bucket) or ".." in bucket:
        raise AwsNotConfiguredError(
            "AWS is not configured: s3.bucket is not a bucket name"
        )
    prefix = value.get("prefix", "")
    if not isinstance(prefix, str) or len(prefix) > _MAX_PREFIX:
        raise AwsNotConfiguredError(
            f"AWS is not configured: s3.prefix must be a string of at most {_MAX_PREFIX} characters"
        )
    region = value.get("region", regions[0])
    if not isinstance(region, str) or not _REGION.match(region):
        raise AwsNotConfiguredError(
            "AWS is not configured: s3.region is not a region name"
        )
    return S3Location(bucket=bucket, prefix=prefix, region=region)


def refused_accounts() -> frozenset[str]:
    """The account ids in :data:`REFUSED_ACCOUNTS_ENV`. A malformed entry
    fails closed: :class:`AwsNotConfiguredError`, so nothing is assumed."""
    raw = os.environ.get(REFUSED_ACCOUNTS_ENV, "")
    accounts = {part.strip() for part in raw.split(",") if part.strip()}
    if any(not _ACCOUNT_ID.match(account) for account in accounts):
        raise AwsNotConfiguredError(
            f"AWS is not configured on this host: {REFUSED_ACCOUNTS_ENV} must be "
            "comma-separated 12-digit account ids"
        )
    return frozenset(accounts)


def is_configured(config: Any) -> bool:
    """Offline shape check: True when ``config`` parses (no network)."""
    try:
        CrossAccountConfig.from_config(config)
    except AwsNotConfiguredError:
        return False
    return True


# -- the session --------------------------------------------------------------

#: Builds the session that holds THIS service's credentials (the pod role).
#: Tests swap it; production uses the default boto3 credential chain.
BaseSessionFactory = Callable[[], Any]


def _default_base_session() -> Any:
    import boto3

    return boto3.Session()


def assume_role_session(
    cfg: CrossAccountConfig,
    *,
    base_session: BaseSessionFactory | None = None,
    session_seconds: int = SESSION_SECONDS,
) -> Any:
    """A boto3 ``Session`` holding only the customer role's temporary
    credentials, refreshed before they lapse.

    Raises :class:`AwsNotConfiguredError` when this host has no AWS principal
    to assume from, and :class:`AwsAccessError` when the role is in this
    service's own account (or one in :data:`REFUSED_ACCOUNTS_ENV`) or STS
    refuses the AssumeRole (the role does not trust our principal, the
    external ID does not match, or the trust policy does not allow
    ``sts:TagSession``)."""
    import boto3
    from botocore.credentials import RefreshableCredentials
    from botocore.session import get_session

    refused = refused_accounts()
    if cfg.account_id in refused:
        raise AwsAccessError(
            "refused: the role is in an AWS account this service is set to refuse; a customer "
            "role must be in the customer's account",
            code="forbidden",
        )
    base = (base_session or _default_base_session)()
    if base.get_credentials() is None:
        raise AwsNotConfiguredError(
            "AWS is not configured on this host: no service principal to assume the customer "
            "role from (on EKS, the pod's IAM role)"
        )
    sts = base.client("sts", region_name=cfg.regions[0])
    own_account = _call(sts.get_caller_identity, what="sts:GetCallerIdentity").get(
        "Account"
    )
    if own_account == cfg.account_id:
        raise AwsAccessError(
            "refused: the role is in this service's own AWS account; a customer role must be "
            "in the customer's account",
            code="forbidden",
        )

    def refresh() -> dict[str, str]:
        response = _call(
            sts.assume_role,
            what="sts:AssumeRole",
            RoleArn=cfg.role_arn,
            RoleSessionName=ROLE_SESSION_NAME,
            ExternalId=cfg.external_id,
            DurationSeconds=session_seconds,
        )
        creds = response["Credentials"]
        return {
            "access_key": creds["AccessKeyId"],
            "secret_key": creds["SecretAccessKey"],
            "token": creds["SessionToken"],
            "expiry_time": creds["Expiration"].isoformat(),
        }

    credentials = RefreshableCredentials.create_from_metadata(
        metadata=refresh(), refresh_using=refresh, method="sts-assume-role"
    )
    botocore_session = get_session()
    # The customer session's only credentials: the provider chain (environment,
    # profiles, instance or pod role) is never consulted for it.
    botocore_session._credentials = credentials
    return boto3.Session(botocore_session=botocore_session)


# -- AWS errors ---------------------------------------------------------------

# A HEAD request's error has no body, so botocore's code for it is the HTTP
# status as a string ("403", "404", "400"): S3_HEAD_OBJECT's refusals arrive so.
_FORBIDDEN = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "KMSAccessDeniedException",
        "KMS.AccessDeniedException",
        "AllAccessDisabled",
        "Forbidden",
        "403",
    }
)
#: AWS rejected an argument that passed our own checks: AgentCore's
#: ValidationException (e.g. an inner nextToken it doesn't accept), S3's
#: InvalidArgument (a bad continuation token or version id) and, for a HEAD,
#: a body-less 400 (a bad version id; the session's credentials refresh on
#: their own, so a 400 is not an expired token).
_INVALID_ARGUMENTS = frozenset(
    {
        "ValidationException",
        "InvalidArgument",
        "400",
    }
)
_NOT_FOUND = frozenset(
    {
        "ResourceNotFoundException",
        "NoSuchKey",
        "NoSuchBucket",
        "NoSuchVersion",
        "NotFound",
        "404",
    }
)
_RATE_LIMITED = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "SlowDown",
        "RequestLimitExceeded",
    }
)
_UNAUTHORIZED = frozenset(
    {
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "InvalidAccessKeyId",
    }
)


def classify_aws_error(exc: BaseException) -> tuple[str, str]:
    """``(toolkit error code, AWS error code)`` for a botocore error."""
    response = getattr(exc, "response", None)
    aws_code = ""
    if isinstance(response, Mapping):
        aws_code = str((response.get("Error") or {}).get("Code") or "")
    if aws_code in _FORBIDDEN:
        return "forbidden", aws_code
    if aws_code in _INVALID_ARGUMENTS:
        return "invalid_arguments", aws_code
    if aws_code in _NOT_FOUND:
        return "not_found", aws_code
    if aws_code in _RATE_LIMITED:
        return "rate_limited", aws_code
    if aws_code in _UNAUTHORIZED:
        return "unauthorized", aws_code
    return "upstream_error", aws_code


def _call(fn: Callable[..., Any], *, what: str, **kwargs: Any) -> Any:
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        return fn(**kwargs)
    except ClientError as e:
        code, aws_code = classify_aws_error(e)
        if what == "sts:AssumeRole" and code == "forbidden":
            message = (
                "AWS refused sts:AssumeRole: the customer role does not trust this service's "
                "principal, the external ID does not match, or the trust policy does not allow "
                "sts:TagSession"
            )
        else:
            message = f"AWS refused {what} ({aws_code or 'error'})"
        raise AwsAccessError(message, code=code, aws_code=aws_code) from None
    except BotoCoreError as e:
        raise AwsAccessError(
            f"AWS {what} failed ({type(e).__name__})", code="upstream_error"
        ) from None


def call(fn: Callable[..., Any], *, what: str, **kwargs: Any) -> Any:
    """Run one AWS call, raising :class:`AwsAccessError` for a refusal."""
    return _call(fn, what=what, **kwargs)


def pages(listing: Any, *, what: str) -> Iterator[dict[str, Any]]:
    """Each page of a botocore paginator. A refusal on any page is an
    :class:`AwsAccessError`, like a single call's."""
    iterator = iter(listing)

    def advance() -> dict[str, Any]:
        return next(iterator)

    while True:
        try:
            page = _call(advance, what=what)
        except StopIteration:
            return
        yield page


# -- a per-process session cache, for toolkits (one tool call = one session) ---

#: Reuse a session this long; its credentials refresh on their own before they
#: lapse, so this only bounds how long a changed blob takes to be re-read.
SESSION_REUSE_SECONDS = 900
_SESSION_CACHE_SIZE = 64
_sessions: dict[tuple[str, str, tuple[str, ...]], tuple[float, Any]] = {}


def cached_session(
    cfg: CrossAccountConfig, *, base_session: BaseSessionFactory | None = None
) -> Any:
    """:func:`assume_role_session`, reused for :data:`SESSION_REUSE_SECONDS`
    per (role, external id, regions)."""
    import time

    key = (cfg.role_arn, cfg.external_id, cfg.regions)
    now = time.monotonic()
    hit = _sessions.get(key)
    if hit is not None and now - hit[0] < SESSION_REUSE_SECONDS:
        return hit[1]
    session = assume_role_session(cfg, base_session=base_session)
    if len(_sessions) >= _SESSION_CACHE_SIZE:
        _sessions.pop(next(iter(_sessions)))
    _sessions[key] = (now, session)
    return session


def clear_session_cache() -> None:
    _sessions.clear()
