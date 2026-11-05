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

`obstore` belongs to the ``blob`` extra, so core statcatlib imports without it.
Function-level imports with ``# noqa: PLC0415`` load the ``blob`` extra until
Python 3.15 is the minimum version, when ``lazy import`` (PEP 810) can replace
them.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import inspect
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, cast

import httpx
from attrs import Factory, field, frozen
from sdmxlib.inventory import CURRENT_NAME, Current, InventoryError, decode_current, encode, sha256_hex

from statcatlib.store.inventory import (
    MAX_RACES,
    InventoryCommitUnknownError,
    InventoryWriteRefusedError,
    current_is_older,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from pathlib import Path
    from types import ModuleType

    from obstore import ObjectMeta, PutMode, PutResult
    from obstore.store import AzureBearerToken, ObjectStore
    from sdmxlib import StoreSecret
    from sdmxlib.inventory import Root


__all__ = [
    "MAX_CLOCK_SKEW",
    "BlobInventoryStorage",
    "BlobLocation",
    "BlobStoreError",
    "TokenIdentity",
]

log = logging.getLogger(__name__)

#: How far this machine's clock may run ahead of the storage clock (spec §6.4).
MAX_CLOCK_SKEW = timedelta(seconds=30)


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
            provider = getattr(self._location.store, "credential_provider", None)
            oid = provider.object_id() if isinstance(provider, TokenIdentity) else None
            resolved = oid or self._given_actor
            if not resolved:
                msg = (
                    f"{self.location}: no identity to record as event.actor; give the store a TokenIdentity whose "
                    "token has an oid claim, or give BlobInventoryStorage an actor="
                )
                raise InventoryWriteRefusedError(msg)
            self._resolved_actor = resolved
        return self._resolved_actor
