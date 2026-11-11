"""The jobs configuration: one TOML file the release job and the GC job both read (Phase 5 D7).

A deployment writes one file, reviews it in a PR, and mounts it into both jobs
from one ConfigMap. `load_jobs_config` reads it with the standard library's
`tomllib`, decodes it into frozen values, and checks it; each run logs the
file's SHA-256 (`JobsConfig.sha256`), so two jobs running different copies show
in the logs.

```toml
format_version = 1

[retention]
backup = "30d"
gc_window = "35d"
tombstone_window = "400d"
inventory_window = "36d"
keep_roots = 10
retire_after = "90d"
upload_grace = "24h"
pod_staleness = "5m"

[access]
storage_account = "/subscriptions/.../storageAccounts/acct"
container = "catalogs"
allowlist = [{ object_id = "...", name = "release job" }]
sentinels = []

[expiry]
enabled = false
```

A duration is a string ``<n><unit>``, *n* a whole number and *unit* one of
``s``, ``m``, ``h`` and ``d``. An instant is RFC 3339 with a zone. Unknown keys,
a missing ``format_version`` and a missing ``[retention]`` are refused, and so is
any value of the wrong type. ``[access]`` is optional here, since GC reads none
of it; the release job requires it. ``[expiry]`` defaults to
``enabled = false``. Every refusal raises `JobsConfigError`, naming the file and
the key.

`decode_registration` reads a ``pending/`` registration as the release job's fold
and the GC job both read it. `_emit` logs a job event (D14) that ``statcat release``
and ``statcat gc`` print as one JSON line.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tomllib
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from attrs import frozen

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

__all__ = [
    "DEFAULT_KEEP_ROOTS",
    "DEFAULT_UPLOAD_GRACE",
    "FORMAT_VERSION",
    "AccessPolicy",
    "ExpiryPolicy",
    "JobsConfig",
    "JobsConfigError",
    "Principal",
    "Registration",
    "RetentionPolicy",
    "decode_registration",
    "format_duration",
    "load_jobs_config",
    "parse_duration",
]

#: The ``format_version`` this statcatlib reads.
FORMAT_VERSION = 1

_UNITS = {"s": timedelta(seconds=1), "m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}

_DURATION = re.compile(r"^([0-9]+)([smhd])$")

_TOP_KEYS = frozenset({"format_version", "retention", "access", "expiry"})
_RETENTION_KEYS = frozenset(
    {
        "backup",
        "gc_window",
        "tombstone_window",
        "inventory_window",
        "keep_roots",
        "retire_after",
        "upload_grace",
        "pod_staleness",
    }
)
_ACCESS_KEYS = frozenset({"storage_account", "container", "allowlist", "sentinels", "scopes"})
_PRINCIPAL_KEYS = frozenset({"object_id", "name"})
_EXPIRY_KEYS = frozenset({"enabled", "not_before", "namespace", "deployments", "port"})

#: ``keep_roots``'s default, the specification's K (§11.6).
DEFAULT_KEEP_ROOTS = 10

#: ``upload_grace``'s default, the specification's upload grace period (§11.6).
DEFAULT_UPLOAD_GRACE = timedelta(hours=24)

#: The keys ``[expiry]`` requires when ``enabled = true``.
_EXPIRY_ENABLED_KEYS = ("not_before", "namespace", "deployments", "port")

_MAX_PORT = 65535

#: What a product id from a ``pending/`` path may be: one plain path segment, as
#: `statcatlib.store.blob` requires of an id that names a directory.
_PRODUCT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: The two keys of a ``pending/`` document, in their order (spec §11.2 step 1).
_REGISTRATION_KEYS = ["edition_id", "release_at"]

#: The `logging.LogRecord` attribute that holds a job event (D14), set by `_emit`.
_EVENT_ATTRIBUTE = "statcat_event"


class JobsConfigError(ValueError):
    """A jobs configuration the jobs refuse: unreadable, malformed, or failing a retention inequality.

    The message names the file, where there is one, and the key or the values
    compared.
    """


def parse_duration(text: str) -> timedelta:
    """A duration written ``<n><unit>``: ``"24h"``, ``"35d"``, ``"5m"`` or ``"30s"``.

    Args:
        text: The value.

    Returns:
        The duration.

    Raises:
        JobsConfigError: *text* is not a whole number followed by one of
            ``s``, ``m``, ``h`` and ``d``.
    """
    found = _DURATION.fullmatch(text)
    if found is None:
        msg = f"{text!r} is no duration; write a whole number and one of s, m, h, d, such as '24h' or '35d'"
        raise JobsConfigError(msg)
    return int(found.group(1)) * _UNITS[found.group(2)]


def format_duration(value: timedelta) -> str:
    """*value* in `parse_duration`'s form, in the largest unit that divides it: ``"35d"``, ``"36h"``, ``"90s"``.

    A value with a fraction of a second, or below zero, is written as Python
    writes a `timedelta`.
    """
    seconds = value.total_seconds()
    if seconds != int(seconds) or seconds < 0:
        return str(value)
    for unit in ("d", "h", "m", "s"):
        size = int(_UNITS[unit].total_seconds())
        if int(seconds) % size == 0:
            return f"{int(seconds) // size}{unit}"
    return f"{int(seconds)}s"


@frozen
class RetentionPolicy:
    """``[retention]``: how long each job keeps what it keeps (spec §11.6).

    Only `keep_roots` and `upload_grace` have defaults, the specification's.
    Every window that decides a deletion is written out.

    Attributes:
        backup: Backup retention. It governs nothing directly; `check` holds
            it at or below `gc_window`.
        gc_window: How far back GC reads the Inventory, and when a retired
            edition's directory and a folded registration may go.
        tombstone_window: The fold refuses a registration older than this,
            and GC deletes such a registration.
        inventory_window: When expiry may take a root (W).
        retire_after: When the release job retires a superseded or withdrawn
            entry: its ``state_changed_at`` is older than this by the storage
            clock (the retire window of spec §6.4).
        pod_staleness: The longest pod poll interval plus fetch time. It
            governs nothing directly; `check` holds it below
            `inventory_window`.
        keep_roots: The newest roots expiry never takes and GC always reads
            (K).
        upload_grace: The age below which GC deletes no ``data/`` file, longer
            than the longest upload.
    """

    backup: timedelta
    gc_window: timedelta
    tombstone_window: timedelta
    inventory_window: timedelta
    retire_after: timedelta
    pod_staleness: timedelta
    keep_roots: int = DEFAULT_KEEP_ROOTS
    upload_grace: timedelta = DEFAULT_UPLOAD_GRACE

    def durations(self) -> dict[str, timedelta]:
        """Every duration, by its key."""
        return {
            "backup": self.backup,
            "gc_window": self.gc_window,
            "tombstone_window": self.tombstone_window,
            "inventory_window": self.inventory_window,
            "retire_after": self.retire_after,
            "upload_grace": self.upload_grace,
            "pod_staleness": self.pod_staleness,
        }

    def check(self) -> None:
        """Refuse a policy that breaks one of the specification's inequalities (§11.6).

        1. ``backup <= gc_window <= tombstone_window``;
        2. ``inventory_window >= gc_window + 1d``;
        3. ``pod_staleness < inventory_window``;
        4. ``keep_roots >= 2``;
        5. every duration is positive.

        Raises:
            JobsConfigError: The first that fails, naming both values compared.
        """
        for key, value in self.durations().items():
            if value <= timedelta(0):
                msg = f"retention: {key} ({format_duration(value)}) must be positive"
                raise JobsConfigError(msg)
        _at_most("backup", self.backup, "gc_window", self.gc_window)
        _at_most("gc_window", self.gc_window, "tombstone_window", self.tombstone_window)
        if self.inventory_window < self.gc_window + timedelta(days=1):
            msg = (
                f"retention: inventory_window ({format_duration(self.inventory_window)}) must be at least "
                f"gc_window ({format_duration(self.gc_window)}) plus 1d"
            )
            raise JobsConfigError(msg)
        if self.pod_staleness >= self.inventory_window:
            msg = (
                f"retention: pod_staleness ({format_duration(self.pod_staleness)}) must be below "
                f"inventory_window ({format_duration(self.inventory_window)})"
            )
            raise JobsConfigError(msg)
        if self.keep_roots < 2:
            msg = f"retention: keep_roots ({self.keep_roots}) must be at least 2"
            raise JobsConfigError(msg)


def _at_most(low_key: str, low: timedelta, high_key: str, high: timedelta) -> None:
    """Refuse *low* above *high*, naming both keys and values."""
    if low > high:
        msg = f"retention: {low_key} ({format_duration(low)}) must not exceed {high_key} ({format_duration(high)})"
        raise JobsConfigError(msg)


@frozen
class Principal:
    """One entry of ``[access] allowlist``: a principal allowed to read the catalog's blobs.

    Attributes:
        object_id: The principal's object id. A group is one principal, by its
            own object id.
        name: A label for messages and reviews.
    """

    object_id: str
    name: str


@frozen
class AccessPolicy:
    """``[access]``: what the release job's access precondition compares (D8).

    Attributes:
        storage_account: The storage account's resource id.
        container: The catalog's container.
        allowlist: The principals allowed a blob-read role on the container.
        sentinels: Role assignment ids the listing must return, one at each
            scope above the account, so a listing that misses inherited
            assignments fails closed.
        scopes: Scopes above the account where the access-check role is also
            assigned; listed only when the account-scoped role cannot see
            inherited assignments.
    """

    storage_account: str
    container: str
    allowlist: tuple[Principal, ...]
    sentinels: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()


@frozen
class ExpiryPolicy:
    """``[expiry]``: whether an ``--expire`` run may expire Inventory versions, and its gates (D13).

    Attributes:
        enabled: Whether expiry runs at all.
        not_before: Gate 1: no expiry before this instant.
        namespace: The Kubernetes namespace of the pods gate 2 reads.
        deployments: Gate 2's label selectors, such as ``app=statcat-hot``.
        port: The port each pod reports its ``sdmxlib_version`` on.
    """

    enabled: bool = False
    not_before: datetime | None = None
    namespace: str | None = None
    deployments: tuple[str, ...] = ()
    port: int | None = None


@frozen
class JobsConfig:
    """A checked jobs configuration.

    Attributes:
        retention: ``[retention]``, its inequalities checked.
        access: ``[access]``, or ``None`` when the file has none.
        expiry: ``[expiry]``, disabled when the file has none.
        sha256: The SHA-256 of the file's bytes, which each run logs.
        format_version: The file's ``format_version``.
    """

    retention: RetentionPolicy
    access: AccessPolicy | None
    expiry: ExpiryPolicy
    sha256: str
    format_version: int = FORMAT_VERSION


def load_jobs_config(path: str | Path) -> JobsConfig:
    """Read, decode and check the jobs configuration at *path*.

    Args:
        path: The TOML file.

    Returns:
        The configuration, with `RetentionPolicy.check` passed.

    Raises:
        JobsConfigError: The file cannot be read or parsed; it has an unknown
            key, a value of the wrong type, a bad duration or instant, no
            ``format_version`` or another one, or no ``[retention]``; a
            required key is missing; or a retention inequality fails. The
            message starts with the file's path.
    """
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        msg = f"{source}: cannot read the jobs configuration: {exc}"
        raise JobsConfigError(msg) from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
        config = _decode(data, sha256=hashlib.sha256(raw).hexdigest())
    except UnicodeDecodeError as exc:
        msg = f"{source}: the jobs configuration is not UTF-8: {exc}"
        raise JobsConfigError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"{source}: the jobs configuration is not TOML: {exc}"
        raise JobsConfigError(msg) from exc
    except JobsConfigError as exc:
        msg = f"{source}: {exc}"
        raise JobsConfigError(msg) from exc
    return config


def _decode(data: Mapping[str, object], *, sha256: str) -> JobsConfig:
    """The configuration in the parsed TOML *data*, checked; any refusal raises `JobsConfigError`."""
    _refuse_unknown("the top level", data, _TOP_KEYS)
    if "format_version" not in data:
        msg = f"format_version is missing; write format_version = {FORMAT_VERSION} first"
        raise JobsConfigError(msg)
    version = _int("format_version", data["format_version"])
    if version != FORMAT_VERSION:
        msg = f"format_version {version} is not one this statcatlib reads ({FORMAT_VERSION})"
        raise JobsConfigError(msg)
    if "retention" not in data:
        msg = "[retention] is missing; both jobs need it"
        raise JobsConfigError(msg)
    retention = _retention(_table("retention", data["retention"]))
    retention.check()
    access = _access(_table("access", data["access"])) if "access" in data else None
    expiry = _expiry(_table("expiry", data["expiry"])) if "expiry" in data else ExpiryPolicy()
    return JobsConfig(retention=retention, access=access, expiry=expiry, sha256=sha256, format_version=version)


def _retention(table: Mapping[str, object]) -> RetentionPolicy:
    _refuse_unknown("[retention]", table, _RETENTION_KEYS)
    _require(
        "[retention]",
        table,
        ("backup", "gc_window", "tombstone_window", "inventory_window", "retire_after", "pod_staleness"),
    )
    return RetentionPolicy(
        backup=_duration("retention.backup", table["backup"]),
        gc_window=_duration("retention.gc_window", table["gc_window"]),
        tombstone_window=_duration("retention.tombstone_window", table["tombstone_window"]),
        inventory_window=_duration("retention.inventory_window", table["inventory_window"]),
        retire_after=_duration("retention.retire_after", table["retire_after"]),
        pod_staleness=_duration("retention.pod_staleness", table["pod_staleness"]),
        keep_roots=_int("retention.keep_roots", table["keep_roots"]) if "keep_roots" in table else DEFAULT_KEEP_ROOTS,
        upload_grace=(
            _duration("retention.upload_grace", table["upload_grace"])
            if "upload_grace" in table
            else DEFAULT_UPLOAD_GRACE
        ),
    )


def _access(table: Mapping[str, object]) -> AccessPolicy:
    _refuse_unknown("[access]", table, _ACCESS_KEYS)
    _require("[access]", table, ("storage_account", "container", "allowlist", "sentinels"))
    allowlist: list[Principal] = []
    for n, item in enumerate(_list("access.allowlist", table["allowlist"])):
        where = f"access.allowlist[{n}]"
        entry = _table(where, item)
        _refuse_unknown(where, entry, _PRINCIPAL_KEYS)
        _require(where, entry, ("object_id", "name"))
        allowlist.append(
            Principal(
                object_id=_str(f"{where}.object_id", entry["object_id"]), name=_str(f"{where}.name", entry["name"])
            )
        )
    return AccessPolicy(
        storage_account=_str("access.storage_account", table["storage_account"]),
        container=_str("access.container", table["container"]),
        allowlist=tuple(allowlist),
        sentinels=_strings("access.sentinels", table["sentinels"]),
        scopes=_strings("access.scopes", table["scopes"]) if "scopes" in table else (),
    )


def _expiry(table: Mapping[str, object]) -> ExpiryPolicy:
    _refuse_unknown("[expiry]", table, _EXPIRY_KEYS)
    enabled = _bool("expiry.enabled", table["enabled"]) if "enabled" in table else False
    if enabled:
        _require("[expiry] with enabled = true", table, _EXPIRY_ENABLED_KEYS)
    port = _int("expiry.port", table["port"]) if "port" in table else None
    if port is not None and not 0 < port <= _MAX_PORT:
        msg = f"expiry.port {port} is no TCP port"
        raise JobsConfigError(msg)
    return ExpiryPolicy(
        enabled=enabled,
        not_before=_instant("expiry.not_before", table["not_before"]) if "not_before" in table else None,
        namespace=_str("expiry.namespace", table["namespace"]) if "namespace" in table else None,
        deployments=_strings("expiry.deployments", table["deployments"]) if "deployments" in table else (),
        port=port,
    )


def _refuse_unknown(where: str, table: Mapping[str, object], known: Collection[str]) -> None:
    unknown = sorted(set(table) - set(known))
    if unknown:
        msg = f"{where} has unknown key(s) {', '.join(unknown)}; it takes {', '.join(sorted(known))}"
        raise JobsConfigError(msg)


def _require(where: str, table: Mapping[str, object], keys: Collection[str]) -> None:
    missing = [key for key in keys if key not in table]
    if missing:
        msg = f"{where} is missing {', '.join(missing)}"
        raise JobsConfigError(msg)


def _table(key: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{key} must be a table, not {type(value).__name__}"
        raise JobsConfigError(msg)
    return {str(k): v for k, v in value.items()}


def _list(key: str, value: object) -> list[object]:
    if not isinstance(value, list):
        msg = f"{key} must be an array, not {type(value).__name__}"
        raise JobsConfigError(msg)
    return list(value)


def _strings(key: str, value: object) -> tuple[str, ...]:
    return tuple(_str(f"{key}[{n}]", item) for n, item in enumerate(_list(key, value)))


def _str(key: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{key} must be a non-empty string, not {value!r}"
        raise JobsConfigError(msg)
    return value


def _int(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{key} must be an integer, not {value!r}"
        raise JobsConfigError(msg)
    return value


def _bool(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        msg = f"{key} must be true or false, not {value!r}"
        raise JobsConfigError(msg)
    return value


def _duration(key: str, value: object) -> timedelta:
    if not isinstance(value, str):
        msg = f"{key} must be a duration string such as '35d', not {value!r}"
        raise JobsConfigError(msg)
    try:
        return parse_duration(value)
    except JobsConfigError as exc:
        msg = f"{key}: {exc}"
        raise JobsConfigError(msg) from exc


def _instant(key: str, value: object) -> datetime:
    """An RFC 3339 instant with a zone, written as a string or as a TOML offset date-time."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            msg = f"{key} {value!r} is no RFC 3339 time"
            raise JobsConfigError(msg) from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        kind = "a date with no time" if isinstance(value, date) else type(value).__name__
        msg = f"{key} must be an RFC 3339 time with a zone, not {kind}"
        raise JobsConfigError(msg)
    if parsed.tzinfo is None:
        msg = f"{key} {value!s} has no time zone: end it with Z or an offset"
        raise JobsConfigError(msg)
    return parsed


def _stamp(moment: datetime) -> str:
    """*moment* in UTC to the millisecond, ending in ``Z``: the form of a root's ``created_at``."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pending_document(edition_id: str, release_at: datetime | None) -> bytes:
    """The ``pending/`` entry: compact UTF-8 JSON, the time in UTC to the millisecond; equal input gives equal bytes."""
    stamp = None if release_at is None else _stamp(release_at)
    return json.dumps({"edition_id": edition_id, "release_at": stamp}, separators=(",", ":")).encode()


def _uuid7_time(value: str) -> datetime | None:
    """The time a canonical UUIDv7 *value* carries, or ``None`` when *value* is no canonical UUIDv7."""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    if parsed.version != 7 or str(parsed) != value:
        return None
    return datetime.fromtimestamp(parsed.time / 1000, UTC)


@frozen
class Registration:
    """A ``pending/<product_id>/<edition_id>.json`` registration, as `decode_registration` reads it.

    Attributes:
        product_id: The product, from the path.
        edition_id: The edition, a UUIDv7 equal to the file name.
        release_at: When the upload asked the edition to be released, in UTC,
            or ``None`` for no scheduled release.
    """

    product_id: str
    edition_id: str
    release_at: datetime | None

    @property
    def path(self) -> str:
        """The registration's path from the catalog root."""
        return f"pending/{self.product_id}/{self.edition_id}.json"

    @property
    def edition_time(self) -> datetime:
        """The time the edition id carries, which the tombstone window is measured from (spec §11.2 step 2).

        Raises:
            ValueError: `edition_id` is no canonical UUIDv7, which
                `decode_registration` refuses.
        """
        found = _uuid7_time(self.edition_id)
        if found is None:
            msg = f"{self.edition_id!r} is no canonical UUIDv7"
            raise ValueError(msg)
        return found


def decode_registration(path: str, raw: bytes) -> Registration:
    """The registration *raw* holds at *path*, read as the release job's fold and the GC job read it.

    The bytes must be the document ``statcat upload`` writes for its own
    decode (spec §11.2 step 1): two keys, ``edition_id`` then ``release_at``,
    compact, ``edition_id`` equal to the file name and a canonical UUIDv7, and
    ``release_at`` ``null`` or UTC with ``Z`` and milliseconds.

    Args:
        path: ``pending/<product_id>/<edition_id>.json``, from the catalog root.
        raw: The bytes stored there.

    Returns:
        The registration.

    Raises:
        ValueError: *path* is no registration's path, or *raw* is not the
            document ``statcat upload`` writes; the message names the path and
            the difference.
    """
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "pending" or not parts[2].endswith(".json"):
        msg = f"{path} is no registration: expected pending/<product_id>/<edition_id>.json"
        raise ValueError(msg)
    product_id, edition_id = parts[1], parts[2].removesuffix(".json")
    if not _PRODUCT_ID.fullmatch(product_id):
        msg = f"{path} names the product {product_id!r}, which cannot name a directory"
        raise ValueError(msg)
    if _uuid7_time(edition_id) is None:
        msg = f"{path} names the edition {edition_id!r}, which is no canonical UUIDv7"
        raise ValueError(msg)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        msg = f"{path} is not UTF-8 JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(data, dict) or list(data) != _REGISTRATION_KEYS:
        msg = f"{path} must hold one object with the keys edition_id and release_at, in that order"
        raise ValueError(msg)
    if data["edition_id"] != edition_id:
        msg = f"{path} holds the edition_id {data['edition_id']!r}, and its file name names {edition_id!r}"
        raise ValueError(msg)
    release_at = _release_time(path, data["release_at"])
    if _pending_document(edition_id, release_at) != raw:
        msg = (
            f"{path} differs from the document statcat upload writes: compact JSON, with release_at "
            "null or in UTC with Z and milliseconds"
        )
        raise ValueError(msg)
    return Registration(product_id=product_id, edition_id=edition_id, release_at=release_at)


def _release_time(path: str, value: object) -> datetime | None:
    """A registration's ``release_at``: ``None``, or an RFC 3339 time with a zone."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None:
        msg = f"{path}: release_at {value!r} is neither null nor an RFC 3339 time"
        raise ValueError(msg)
    if parsed.tzinfo is None:
        msg = f"{path}: release_at {value!r} has no time zone"
        raise ValueError(msg)
    return parsed


def _emit(logger: logging.Logger, level: int, event: Mapping[str, object], message: str) -> None:
    """Log *message* at *level*, carrying *event* as the record's `_EVENT_ATTRIBUTE` for the command to print.

    *event* holds JSON values only. ``statcat release`` and ``statcat gc`` print
    each such record as one JSON line on standard output (D14); any other handler
    sees *message*.
    """
    logger.log(level, "%s", message, extra={_EVENT_ATTRIBUTE: dict(event)})
