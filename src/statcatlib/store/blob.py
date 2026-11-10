"""Object storage: where a catalog's Inventory lives on Azure Blob Storage or S3 (Phase 4, GH-98).

`InventoryWriter` holds the rules, and `BlobInventoryStorage` is its storage
over an `obstore` store. It differs from `LocalInventoryStorage` in these ways:

- **Roots** are created with one conditional Put, ``put(mode="create")``, so a
  root is complete when it wins its name, at any size.
- **Leaves** are written with one unconditional Put and no existence check,
  on every store. A carried-forward leaf gets no Put, since `try_commit` writes
  only the leaves its version changes.
- **``CURRENT``** moves forward by a conditional write on its ``e_tag``. The
  root is the commit point, so a failed move logs a WARNING and the commit
  stands: readers probe forward (spec §6.6).
- **The clock** is the ``Date`` header of an anonymous ``HEAD`` at the store's
  endpoint, since `obstore` exposes no response header. A machine clock more
  than `MAX_CLOCK_SKEW` ahead of it is refused.
- **The actor** is the ``oid`` of the storage identity's token
  (`TokenIdentity`), else the value given to `BlobInventoryStorage`, else a
  refusal.

`AzureBlobRest` reads the digest Azure keeps for a single-shot Put, since
`obstore` reads no ``Content-MD5``. `statcatlib.upload` uses it.

`pull_edition` copies an edition's catalog files to a reader's ``cache_dir``, and
`Catalog.open` over a `BlobLocation` reads through it. A file reaches the cache
only by the rename of a staged directory whose every file was checked against the
manifest and fsynced. Every id and path that names a local file is refused unless
it stays where it belongs, since the Inventory and the manifest are bytes from a store.

`obstore` belongs to the ``blob`` extra, so core statcatlib imports without it.
Function-level imports with ``# noqa: PLC0415`` load the ``blob`` extra until
Python 3.15 is the minimum version, when ``lazy import`` (PEP 810) can replace
them.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import hashlib
import importlib.util
import inspect
import json
import logging
import os
import posixpath
import re
import shutil
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import PurePath
from typing import TYPE_CHECKING, Self, cast
from urllib.parse import quote, urlsplit

import httpx
from attrs import Factory, field, frozen
from sdmxlib.edition import MANIFEST_NAME, ManifestError, RemoteFile, decode_manifest, edition_files
from sdmxlib.inventory import CURRENT_NAME, Current, InventoryError, decode_current, encode, sha256_hex

from statcatlib.bundle import check_catalog, check_catalog_no_index
from statcatlib.store.inventory import (
    MAX_RACES,
    InventoryCommitUnknownError,
    InventoryWriteRefusedError,
    _fsync_dir,
    current_is_older,
)
from statcatlib.store.store import IncompatibleCatalogError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Sequence
    from pathlib import Path
    from types import ModuleType

    from obstore import ObjectMeta, PutMode, PutResult
    from obstore.store import AzureBearerToken, ObjectStore
    from sdmxlib import StoreSecret
    from sdmxlib.edition import Check, EditionFile, Manifest
    from sdmxlib.inventory import Root


__all__ = [
    "MAX_CLOCK_SKEW",
    "AzureBlobRest",
    "BlobInventoryStorage",
    "BlobLocation",
    "BlobStoreError",
    "TokenIdentity",
    "pull_edition",
]

log = logging.getLogger(__name__)

#: How far this machine's clock may run ahead of the storage clock (spec §6.4).
MAX_CLOCK_SKEW = timedelta(seconds=30)

#: How many objects one page of `_ObjectOps.list` asks the store for.
_LIST_PAGE = 1000

#: The Blob service version `AzureBlobRest` asks for, the one `object_store` sends.
_AZURE_VERSION = "2023-11-03"

#: How long `AzureBlobRest` waits for one ``Get Blob Properties``, in seconds.
_REST_TIMEOUT = 30.0

#: What a catalog, product or edition id may be where it names a directory. It
#: starts with a letter or digit, so no id can be a dot path or a staged name.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: The placement tiers a reader holds on disk; it reads the remote tier from the store.
_LOCAL_TIERS = ("required", "lazy")

#: How a staged pull's directory name starts.
_STAGED_PREFIX = ".staged-"

#: A staged directory older than this belongs to a pull that crashed.
_STAGED_MAX_AGE = timedelta(days=1)


def _load_obstore() -> tuple[ModuleType, ModuleType]:
    """`obstore` and `obstore.exceptions`; a missing ``blob`` extra raises `ImportError` naming it."""
    try:
        import obstore  # noqa: PLC0415
        from obstore import exceptions  # noqa: PLC0415
    except ImportError as exc:
        msg = "object storage needs the blob extra: uv add 'statcatlib[blob]' (or pip install 'statcatlib[blob]')"
        raise ImportError(msg) from exc
    return obstore, exceptions


class BlobStoreError(RuntimeError):
    """An object store refused a request, or its outcome is unknown.

    Attributes:
        path: The object the request named.
        outcome_unknown: Whether the request may still have taken effect:
            true for `obstore`'s `GenericError`, the class a timeout raises.
    """

    path: str
    outcome_unknown: bool

    def __init__(self, message: str, *, path: str, outcome_unknown: bool = False) -> None:
        """Say what failed, and name the object."""
        super().__init__(message)
        self.path = path
        self.outcome_unknown = outcome_unknown


@contextlib.contextmanager
def _store_faults(action: str, path: str) -> Generator[None]:
    """Raise `BlobStoreError` for a store fault in the block, except `AlreadyExistsError` and `PreconditionError`."""
    _, exceptions = _load_obstore()
    try:
        yield
    except exceptions.AlreadyExistsError, exceptions.PreconditionError:
        raise
    except exceptions.BaseError as exc:
        msg = f"{action} {path}: {type(exc).__name__}: {exc}"
        raise BlobStoreError(msg, path=path, outcome_unknown=isinstance(exc, exceptions.GenericError)) from exc


class _ObjectOps:
    """The one route to `obstore`. Tests subclass it to record every call or refuse a path.

    `AlreadyExistsError` and `PreconditionError` reach the caller, which decides
    what a lost condition means. An absent object reads as ``None``. Every other
    store fault raises `BlobStoreError`.
    """

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    def read(self, path: str) -> tuple[bytes, str | None] | None:
        """The object's bytes and ``e_tag``, or ``None`` when it is absent."""
        obstore, exceptions = _load_obstore()
        with _store_faults("read", path):
            try:
                result = obstore.get(self._store, path)
                return bytes(result.bytes()), result.meta["e_tag"]
            except FileNotFoundError, exceptions.NotFoundError:
                return None

    def put(self, path: str, data: bytes, *, mode: PutMode | None = None, use_multipart: bool = False) -> str | None:
        """Write the object and return its ``e_tag``."""
        obstore, _ = _load_obstore()
        with _store_faults("write", path):
            written: PutResult = obstore.put(self._store, path, data, mode=mode, use_multipart=use_multipart)
            return written["e_tag"]

    def head(self, path: str) -> ObjectMeta | None:
        """The object's metadata, or ``None`` when it is absent."""
        obstore, exceptions = _load_obstore()
        with _store_faults("read the metadata of", path):
            try:
                meta: ObjectMeta | None = obstore.head(self._store, path)
            except FileNotFoundError, exceptions.NotFoundError:
                meta = None
            return meta

    def delete(self, path: str) -> None:
        """Delete the object."""
        obstore, _ = _load_obstore()
        with _store_faults("delete", path):
            obstore.delete(self._store, path)

    def list(self, prefix: str) -> Iterator[ObjectMeta]:
        """Every object under *prefix*, in pages of `_LIST_PAGE`, each object's path from the catalog root.

        `obstore` lists by whole path segments, so ``pending/a`` lists
        ``pending/a/x.json`` and leaves out ``pending/ab/x.json``. A store fault
        on any page raises `BlobStoreError`, naming *prefix*.
        """
        obstore, _ = _load_obstore()
        with _store_faults("list", prefix):
            for page in obstore.list(self._store, prefix, chunk_size=_LIST_PAGE):
                yield from page


def _default_ops(location: BlobLocation) -> _ObjectOps:
    return _ObjectOps(location.store)


@frozen
class BlobLocation:
    """A catalog in object storage, and how to reach it.

    Attributes:
        uri: The catalog root in DuckDB's form, such as ``az://catalogs/stc/``.
        store: The `obstore` store, prefixed at the catalog root. The caller
            builds it, so every credential option of `obstore` stays available.
        secrets: The credentials DuckDB reads data files with.
        cache_dir: Where a reader keeps pulled editions.
        ops: The route to the store; tests replace it.
    """

    uri: str
    store: ObjectStore
    secrets: Sequence[StoreSecret] = field(default=(), repr=False)
    cache_dir: Path | None = None
    ops: _ObjectOps = field(default=Factory(_default_ops, takes_self=True), repr=False, eq=False)


def _object_id(token: str) -> str | None:
    """The ``oid`` claim of a JWT, unverified, or ``None`` when *token* has none.

    The value only labels an audit record, and no access decision reads it.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        claims: object = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except binascii.Error, ValueError:
        return None
    oid = claims.get("oid") if isinstance(claims, dict) else None
    return oid if isinstance(oid, str) and oid else None


class TokenIdentity:
    """A credential provider that also knows the identity's object id.

    Pass it as ``AzureStore(credential_provider=...)``. It wraps a synchronous
    *provider* returning ``{"token": ..., "expires_at": ...}`` and keeps each
    token's ``oid`` claim, which a blob writer records as ``event.actor``. Any
    other return raises `TypeError`, so an asynchronous provider, a key or a SAS
    is refused. Each call asks the provider, which owns caching. A token never
    appears in its `repr`.
    """

    def __init__(self, provider: Callable[[], object]) -> None:
        """Wrap *provider*."""
        self._provider = provider
        self._fetched = False
        self._object_id: str | None = None

    def __repr__(self) -> str:
        return "TokenIdentity()"

    def __call__(self) -> AzureBearerToken:
        """Ask the provider for a credential, keep its ``oid``, and return it unchanged."""
        credential = self._provider()
        token = credential.get("token") if isinstance(credential, Mapping) else None
        if not isinstance(token, str) or not isinstance(credential, Mapping) or "expires_at" not in credential:
            if inspect.iscoroutine(credential):
                credential.close()
            msg = (
                'TokenIdentity needs a synchronous provider returning {"token": str, "expires_at": datetime | None}; '
                f"this one returned {type(credential).__name__}"
            )
            raise TypeError(msg)
        self._object_id = _object_id(token)
        self._fetched = True
        return cast("AzureBearerToken", credential)

    def token(self) -> str:
        """A bearer token, asking the provider on each use."""
        return self()["token"]

    def object_id(self) -> str | None:
        """The identity's object id, fetching a token first if none has been fetched."""
        if not self._fetched:
            self()
        return self._object_id


def _token_identity(store: ObjectStore) -> TokenIdentity | None:
    """The `TokenIdentity` *store* holds as its credential provider, or ``None``."""
    provider = getattr(store, "credential_provider", None)
    return provider if isinstance(provider, TokenIdentity) else None


def _endpoint(store: ObjectStore) -> str:
    """The URL *store* answers at, from its configuration.

    Args:
        store: The store.

    Returns:
        ``config["endpoint"]`` when set, else Azure's account URL or S3's
        regional URL.

    Raises:
        ValueError: The configuration does not name the account or the
            region. `obstore` leaves a value read from the environment out of
            ``store.config``, so pass it to the store's constructor.
    """
    found = getattr(store, "config", None)
    config: Mapping[str, object] = found if isinstance(found, Mapping) else {}
    endpoint = config.get("endpoint")
    if isinstance(endpoint, str):
        return endpoint
    if "container_name" in config:
        account = config.get("account_name")
        if isinstance(account, str):
            return f"https://{account}.blob.core.windows.net"
        msg = "this AzureStore's config has no account_name; pass account_name= to the store"
        raise ValueError(msg)
    region = config.get("region")
    if "bucket" in config and isinstance(region, str):
        return f"https://s3.{region}.amazonaws.com"
    msg = f"{store!r} names no endpoint; set endpoint= (or region= on an S3Store) on the store"
    raise ValueError(msg)


class AzureBlobRest:
    """Azure's ``Get Blob Properties`` under the identity's bearer token, for the digest `obstore` cannot read.

    The call needs ``blobs/read`` and nothing else. Azure keeps an MD5 for a
    single-shot Put, and this class reads it back. A tokenless store has no route
    here, since no deployment uses a key.
    """

    def __init__(self, location: BlobLocation) -> None:
        """Reach the blobs of *location*'s `AzureStore`; a config with no container or account raises `ValueError`."""
        self._store = location.store
        found = getattr(self._store, "config", None)
        config: Mapping[str, object] = found if isinstance(found, Mapping) else {}
        if "container_name" not in config:
            msg = "AzureBlobRest needs an AzureStore whose config names container_name; this store's config has none"
            raise ValueError(msg)
        prefix = getattr(self._store, "prefix", None)
        parts = [_endpoint(self._store).rstrip("/"), quote(str(config["container_name"]))]
        self._base = "/".join([*parts, *([quote(prefix)] if prefix else [])])
        self._client = httpx.Client(timeout=_REST_TIMEOUT)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def _url(self, path: str) -> str:
        return f"{self._base}/{quote(path)}"

    def _send(self, path: str) -> httpx.Response:
        """Send the ``HEAD``. The account-key test path overrides this one method."""
        identity = _token_identity(self._store)
        if identity is None:
            msg = "AzureBlobRest needs a store whose credential_provider is a TokenIdentity"
            raise ValueError(msg)
        headers = {"Authorization": f"Bearer {identity.token()}", "x-ms-version": _AZURE_VERSION}
        return self._client.head(self._url(path), headers=headers)

    def head(self, path: str) -> RemoteFile | None:
        """The size and kept digest of the blob at *path* under the catalog root.

        Args:
            path: The object's path.

        Returns:
            What the store reports, or ``None`` on a 404 alone, so a missing
            role never reads as a missing file.

        Raises:
            BlobStoreError: Any other status, an unreadable size, or a transport
                failure.
        """
        try:
            response = self._send(path)
        except httpx.HTTPError as exc:
            msg = f"read the properties of {path}: {type(exc).__name__}: {exc}"
            raise BlobStoreError(msg, path=path) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if not response.is_success:
            msg = f"read the properties of {path}: HTTP {response.status_code}"
            raise BlobStoreError(msg, path=path)
        length = response.headers.get("content-length", "")
        if not length.isdecimal():
            msg = f"read the properties of {path}: the response has no readable Content-Length: {length!r}"
            raise BlobStoreError(msg, path=path)
        md5 = response.headers.get("content-md5")
        return RemoteFile(int(length), {} if md5 is None else {"md5": md5})


def _statcat_check() -> Check:
    """The ``statcat`` check this install can run: `check_catalog` with `lancedb`, else `check_catalog_no_index`."""
    return check_catalog if importlib.util.find_spec("lancedb") is not None else check_catalog_no_index


class BlobInventoryStorage:
    """A catalog's Inventory documents in object storage: an `InventoryStorage` over `BlobLocation`."""

    def __init__(
        self,
        location: BlobLocation,
        *,
        actor: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Store the Inventory at *location*.

        Args:
            location: The catalog.
            actor: Recorded as ``event.actor`` when the store's credential
                provider is no `TokenIdentity`, or its token has no ``oid``.
                It is recorded verbatim.
            clock: The time source in place of the storage's ``Date`` header.
        """
        self._location = location
        self._ops = location.ops
        self._given_actor = actor
        self._resolved_actor: str | None = None
        self._clock = clock

    @property
    def location(self) -> str:
        """The catalog's URI, for messages."""
        return self._location.uri

    def read(self, path: str) -> bytes | None:
        """The bytes at *path* under the catalog root, or ``None``."""
        got = self._ops.read(path)
        return None if got is None else got[0]

    def _held(self, path: str) -> bytes | None:
        """What a create found at its name, with an unknown read counting as nothing found yet."""
        try:
            return self.read(path)
        except BlobStoreError as exc:
            if exc.outcome_unknown:
                return None
            raise

    def create(self, path: str, data: bytes) -> None:
        """Create *path* with one conditional Put, and settle an unknown outcome by reading the name.

        A create can raise `AlreadyExistsError` against its own earlier
        success, and a timeout leaves its outcome unknown. Equal bytes at the
        name mean this writer won, or wrote the same version as its twin. An
        absent name is tried once more.

        Args:
            path: The name.
            data: The document.

        Raises:
            FileExistsError: The name holds other bytes.
            InventoryCommitUnknownError: The name stayed absent after the
                second try, and a timed-out create can still land later.
            BlobStoreError: The store gave a definite refusal.
        """
        _, exceptions = _load_obstore()
        for _ in range(2):
            try:
                self._ops.put(path, data, mode="create")
            except exceptions.AlreadyExistsError:
                pass
            except exceptions.PreconditionError as exc:
                msg = f"create {path}: {type(exc).__name__}: {exc}"
                raise BlobStoreError(msg, path=path) from exc
            except BlobStoreError as exc:
                if not exc.outcome_unknown:
                    raise
            else:
                return
            held = self._held(path)
            if held == data:
                return
            if held is not None:
                msg = f"{self.location}: {path} holds other bytes"
                raise FileExistsError(msg)
        msg = f"{self.location}: the outcome of creating {path} is unknown; re-read the head"
        raise InventoryCommitUnknownError(msg)

    def write_leaf(self, path: str, data: bytes) -> None:
        """Write a leaf with one unconditional Put and no existence check, at any size."""
        self._ops.put(path, data, use_multipart=False)

    def advance_current(self, root: Root, data: bytes) -> None:
        """Point ``CURRENT`` at *root* by a conditional write, retrying a lost condition up to `MAX_RACES` times.

        A malformed ``CURRENT`` counts as absent for the decision and is
        replaced by its ``e_tag``. The root is already the commit point, so a
        failure logs a WARNING and returns.
        """
        current = encode(Current(seq=root.seq, root_sha256=sha256_hex(data), restore_epoch=root.restore_epoch))
        try:
            for _ in range(MAX_RACES):
                if self._move_current(root, current):
                    return
        except BlobStoreError as exc:
            log.warning("%s: version %d is committed, and CURRENT did not move: %s", self.location, root.seq, exc)
            return
        log.warning("%s: version %d is committed, and CURRENT lost %d races", self.location, root.seq, MAX_RACES)

    def _move_current(self, root: Root, current: bytes) -> bool:
        """One read and conditional write of ``CURRENT``; false when another writer got between them."""
        _, exceptions = _load_obstore()
        got = self._ops.read(CURRENT_NAME)
        mode: PutMode = "create"
        held = None
        if got is not None:
            raw, e_tag = got
            if e_tag is None:
                msg = f"{self.location}: the store gave {CURRENT_NAME} no e_tag"
                raise BlobStoreError(msg, path=CURRENT_NAME)
            mode = {"e_tag": e_tag}
            try:
                held = decode_current(raw)
            except InventoryError:
                held = None
        if not current_is_older(held, root):
            return True
        try:
            self._ops.put(CURRENT_NAME, current, mode=mode)
        except exceptions.AlreadyExistsError, exceptions.PreconditionError:
            return False
        return True

    def now(self) -> datetime:
        """The storage clock: the ``Date`` of an anonymous ``HEAD`` at the endpoint, or the given clock.

        Returns:
            The storage's time. A machine clock behind it passes, since the
            writer records storage time.

        Raises:
            InventoryWriteRefusedError: The request failed, the ``Date`` is
                missing or malformed, or this machine's clock is more than
                `MAX_CLOCK_SKEW` ahead of it; the message names both times.
        """
        if self._clock is not None:
            return self._clock()
        store = self._location.store
        if getattr(store, "config", None) is None:
            msg = f"{self.location}: {type(store).__name__} has no storage clock; give BlobInventoryStorage a clock="
            raise InventoryWriteRefusedError(msg)
        endpoint = _endpoint(store)
        try:
            response = httpx.head(endpoint)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            msg = f"{self.location}: cannot read the storage clock at {endpoint}: {exc}"
            raise InventoryWriteRefusedError(msg) from exc
        return self._checked(endpoint, response.headers.get("date"))

    def _checked(self, endpoint: str, date: str | None) -> datetime:
        """The time in the ``Date`` header *date*, refused when it is unreadable or this machine runs far ahead."""
        try:
            stamp = parsedate_to_datetime(date) if date is not None else None
        except ValueError:
            stamp = None
        if stamp is None or stamp.tzinfo is None:
            msg = f"{self.location}: {endpoint} sent no readable Date header: {date!r}"
            raise InventoryWriteRefusedError(msg)
        storage = stamp.astimezone(UTC)
        here = datetime.now(UTC)
        if here - storage > MAX_CLOCK_SKEW:
            msg = (
                f"{self.location}: this machine's clock reads {here.isoformat()}, more than "
                f"{MAX_CLOCK_SKEW.total_seconds():.0f} s ahead of the storage clock at {storage.isoformat()}; "
                "fix the clock that is wrong"
            )
            raise InventoryWriteRefusedError(msg)
        return storage

    def actor(self) -> str:
        """Who acts: the token's ``oid``, else the given actor; resolved once.

        Returns:
            The value for ``event.actor``.

        Raises:
            InventoryWriteRefusedError: Neither exists.
        """
        if self._resolved_actor is None:
            identity = _token_identity(self._location.store)
            oid = identity.object_id() if identity is not None else None
            resolved = oid or self._given_actor
            if not resolved:
                msg = (
                    f"{self.location}: no identity to record as event.actor; give the store a TokenIdentity whose "
                    "token has an oid claim, or give BlobInventoryStorage an actor="
                )
                raise InventoryWriteRefusedError(msg)
            self._resolved_actor = resolved
        return self._resolved_actor


def _is_plain_name(name: str) -> bool:
    """Whether *name* is one path segment: not empty, no separator, no NUL, not a dot path."""
    return bool(name) and not {"/", "\\", "\0"} & set(name) and name not in {".", ".."}


def _is_safe_path(path: str) -> bool:
    """Whether *path* stays below the directory it is joined to: relative, with only plain segments."""
    return not PurePath(path).anchor and all(_is_plain_name(part) for part in path.split("/"))


def _refuse_unsafe_ids(product_id: str, edition_id: str) -> None:
    """Refuse an id from a leaf or a root that could name a directory outside the cache."""
    for name, value in (("product_id", product_id), ("edition_id", edition_id)):
        if not _SAFE_ID.fullmatch(value):
            msg = f"the Inventory names {name} {value!r}, which cannot name a directory; nothing was read or written"
            raise IncompatibleCatalogError(msg)


def _refuse_unsafe_files(manifest: Manifest) -> None:
    """Refuse a manifest whose ``files`` or ``data_path`` could reach outside the edition's product."""
    where = f"{manifest.product_id}'s edition {manifest.edition_id}"
    for entry in manifest.files:
        if not _is_safe_path(entry.path) or (entry.root == "edition" and entry.path == MANIFEST_NAME):
            msg = f"{where} lists the unsafe path {entry.path!r} in its manifest; nothing was written"
            raise IncompatibleCatalogError(msg)
    data_path = manifest.data_path
    resolved = posixpath.normpath(posixpath.join(manifest.edition_id, data_path))
    if PurePath(data_path).anchor or urlsplit(data_path).scheme or resolved.split("/")[0] == "..":
        msg = f"{where} has data_path {data_path!r}, which leads outside the product; nothing was written"
        raise IncompatibleCatalogError(msg)


def _decode_checked(raw: bytes, product_id: str, edition_id: str, source: str) -> Manifest:
    """The manifest in *raw*, refused when it is unreadable or names another product or edition."""
    try:
        manifest = decode_manifest(raw, source)
    except ManifestError as exc:
        msg = f"{product_id}'s edition {edition_id} has a manifest this build cannot read: {exc}"
        raise IncompatibleCatalogError(msg) from exc
    if (manifest.product_id, manifest.edition_id) != (product_id, edition_id):
        msg = (
            f"the manifest of {product_id}'s edition {edition_id} names {manifest.product_id!r}'s edition "
            f"{manifest.edition_id!r}; nothing else of the edition was read"
        )
        raise IncompatibleCatalogError(msg)
    return manifest


def _read_remote_manifest(
    location: BlobLocation, product_id: str, edition_id: str, manifest_sha256: str
) -> tuple[bytes, Manifest]:
    """The manifest the Inventory recorded for an edition, read first of the edition's files.

    Args:
        location: The catalog.
        product_id: The product.
        edition_id: The edition.
        manifest_sha256: The SHA-256 the Inventory entry records.

    Returns:
        The manifest's bytes, and the manifest decoded from them.

    Raises:
        IncompatibleCatalogError: An id cannot name a directory, the manifest
            is absent, its SHA-256 is not the entry's, it cannot be decoded, or
            it names another product or edition.
    """
    _refuse_unsafe_ids(product_id, edition_id)
    path = f"products/{product_id}/{edition_id}/{MANIFEST_NAME}"
    got = location.ops.read(path)
    if got is None:
        msg = f"{location.uri}: {path} is missing, and the Inventory records it"
        raise IncompatibleCatalogError(msg)
    raw = got[0]
    found = sha256_hex(raw)
    if found != manifest_sha256:
        msg = (
            f"{location.uri}: {path} has SHA-256 {found}, and the Inventory records {manifest_sha256}; "
            "nothing else of the edition was read"
        )
        raise IncompatibleCatalogError(msg)
    return raw, _decode_checked(raw, product_id, edition_id, f"{location.uri}{path}")


def _make_dirs(path: Path) -> None:
    """Create *path* and its missing parents, and fsync each entry that was added."""
    missing: list[Path] = []
    existing = path
    while not existing.exists():
        missing.append(existing)
        existing = existing.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in [*missing, existing]:
        _fsync_dir(directory)


def _sweep_staged(parent: Path) -> None:
    """Delete the staged directories in *parent* older than `_STAGED_MAX_AGE`, each left by a crashed pull."""
    cutoff = time.time() - _STAGED_MAX_AGE.total_seconds()
    for staged in parent.glob(f"{_STAGED_PREFIX}*"):
        with contextlib.suppress(OSError):
            if staged.stat().st_mtime < cutoff:
                shutil.rmtree(staged)


def _write_synced(path: Path, data: bytes) -> None:
    """Create the file *path* and fsync it; a name already taken raises `FileExistsError`."""
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _pull_file(location: BlobLocation, store_path: str, target: Path, entry: EditionFile) -> None:
    """GET one listed file, check its size and sha256 against the manifest, and write it durably."""
    got = location.ops.read(store_path)
    if got is None:
        msg = f"{location.uri}: {store_path} is missing, and the edition's manifest lists it"
        raise IncompatibleCatalogError(msg)
    data = got[0]
    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.digests.get("sha256"):
        msg = f"{location.uri}: {store_path} differs from the size or sha256 the edition's manifest lists"
        raise IncompatibleCatalogError(msg)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_synced(target, data)
    except (FileExistsError, IsADirectoryError, NotADirectoryError) as exc:
        msg = f"{location.uri}: the edition's manifest lists {entry.path!r} over another listed path"
        raise IncompatibleCatalogError(msg) from exc


def _publish(staged: Path, dest: Path, manifest_bytes: bytes) -> None:
    """Rename the whole *staged* directory to *dest*; a loser of the race uses the winner's copy."""
    try:
        staged.rename(dest)
    except OSError as exc:
        if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
            raise
        if (dest / MANIFEST_NAME).read_bytes() != manifest_bytes:
            msg = f"{dest} holds another manifest than this edition's; delete the directory to pull it again"
            raise IncompatibleCatalogError(msg) from exc
        return
    _fsync_dir(dest.parent)


def pull_edition(location: BlobLocation, product_id: str, edition_id: str, manifest_bytes: bytes, dest: Path) -> Path:
    """Copy an edition's ``required`` and ``lazy`` files to *dest*, all of them or none.

    It stages the copy in a sibling directory: each file is read from the store,
    checked against the manifest's size and sha256, written and fsynced, then
    the manifest, then each directory. Only then does a rename make it visible.
    Staged directories older than a day are swept first, and a process that loses
    the rename uses the winner's copy. Data files stay in the store, and each file
    is held in memory while it is checked, so peak memory is the largest file.

    The caller has compared *manifest_bytes* with the Inventory's ``manifest_sha256``.

    Args:
        location: The catalog.
        product_id: The product, which the manifest must name.
        edition_id: The edition, which the manifest must name.
        manifest_bytes: The edition's manifest.
        dest: The directory to create, named by the edition id.

    Returns:
        *dest*.

    An id that cannot name a directory, a manifest that is unreadable or names
    another edition or an unsafe path, and a file missing from the store or
    differing from the manifest raise `IncompatibleCatalogError`, and leave *dest*
    absent.
    """
    _refuse_unsafe_ids(product_id, edition_id)
    manifest = _decode_checked(manifest_bytes, product_id, edition_id, str(dest / MANIFEST_NAME))
    _refuse_unsafe_files(manifest)
    own = {f"{edition_id}/{entry.path}": entry for entry in manifest.files if entry.root == "edition"}
    listed = [path for path in edition_files(manifest, tiers=_LOCAL_TIERS) if path in own]
    _make_dirs(dest.parent)
    _sweep_staged(dest.parent)
    staged = dest.parent / f"{_STAGED_PREFIX}{edition_id}.{os.getpid()}.{uuid.uuid4().hex}"
    staged.mkdir()
    try:
        for path in listed:
            entry = own[path]
            _pull_file(location, f"products/{product_id}/{path}", staged / entry.path, entry)
        _write_synced(staged / MANIFEST_NAME, manifest_bytes)
        directories = [found for found in staged.rglob("*") if found.is_dir()]
        for directory in sorted(directories, key=lambda found: len(found.parts), reverse=True):
            _fsync_dir(directory)
        _fsync_dir(staged)
        _publish(staged, dest, manifest_bytes)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    return dest


def _cached_edition(
    location: BlobLocation, cache_root: Path, product_id: str, edition_id: str, manifest_sha256: str
) -> tuple[Path, Manifest]:
    """The edition's directory in the cache, pulled when absent, and its manifest: `Product.view`'s steps 2 and 3.

    Args:
        location: The catalog.
        cache_root: ``<cache_dir>/<catalog_id>``.
        product_id: The product.
        edition_id: The edition.
        manifest_sha256: The SHA-256 the Inventory entry records.

    Returns:
        The edition's directory, and the manifest.

    Raises:
        IncompatibleCatalogError: An id cannot name a directory, or the cached
            manifest is not the one the Inventory records; the message names the
            directory to delete. `_read_remote_manifest` and `pull_edition` raise
            it too.
    """
    _refuse_unsafe_ids(product_id, edition_id)
    dest = cache_root / "products" / product_id / edition_id
    if not dest.exists():
        raw, manifest = _read_remote_manifest(location, product_id, edition_id, manifest_sha256)
        return pull_edition(location, product_id, edition_id, raw, dest), manifest
    try:
        raw = (dest / MANIFEST_NAME).read_bytes()
    except OSError as exc:
        msg = f"{dest} holds no readable {MANIFEST_NAME} ({exc}); delete the directory to pull the edition again"
        raise IncompatibleCatalogError(msg) from exc
    found = sha256_hex(raw)
    if found != manifest_sha256:
        msg = (
            f"{dest} holds a manifest with SHA-256 {found}, and the Inventory records {manifest_sha256}; "
            "delete the directory to pull the edition again"
        )
        raise IncompatibleCatalogError(msg)
    return dest, _decode_checked(raw, product_id, edition_id, str(dest / MANIFEST_NAME))
