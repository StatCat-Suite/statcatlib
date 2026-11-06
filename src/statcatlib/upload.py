"""Copying a sealed edition to object storage and registering it in ``pending/`` (spec §11.2 step 1, GH-98).

`upload_edition` is what ``statcat upload`` runs. It supports Azure only, since
`obstore` cannot set or read the CRC32C an S3 upload would need. In order, it:

1. reads the manifest and refuses a file above Azure's single-Put limit;
2. creates each data file under ``products/<product_id>/data/``, then each file
   of the edition, then ``manifest.json``, each with one conditional Put, so
   Azure keeps its MD5. A file the store holds stays, and a file it lacks is
   hashed against the manifest first;
3. verifies every listed file against the store's size and kept MD5, the edition
   locally, and the uploaded manifest by SHA-256;
4. creates ``pending/<product_id>/<edition_id>.json``.

A finding stops it before step 4, so a bad upload registers nothing. It reads and
writes nothing under ``catalog.json``, ``inventory/`` or ``CURRENT``. It defers the
reference check of an edition with no bindings, whose references may resolve in
another structure product, and Phase 5's fold owns that check.

Function-level imports with ``# noqa: PLC0415`` load the ``blob`` extra until
Python 3.15 is the minimum version.
"""

from __future__ import annotations

import copy
import hashlib
import json
import posixpath
from datetime import UTC
from typing import TYPE_CHECKING, Protocol

from attrs import frozen
from sdmxlib.edition import (
    MANIFEST_NAME,
    Finding,
    check_edition,
    edition_files,
    read_manifest,
    verify_remote,
)

from statcatlib.bundle import STATCAT_SECTION
from statcatlib.store.blob import (
    AzureBlobRest,
    BlobStoreError,
    _is_plain_name,
    _is_safe_path,
    _load_obstore,
    _statcat_check,
    _token_identity,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime
    from pathlib import Path

    from obstore.exceptions import AlreadyExistsError, PreconditionError
    from sdmxlib.edition import EditionFile, Manifest, RemoteFile

    from statcatlib.store.blob import BlobLocation, _ObjectOps


__all__ = ["UploadCheckError", "UploadReport", "upload_edition"]

#: Put Blob's limit on service version 2019-12-12 and later, in bytes.
MAX_PUT_BYTES = 5_000 * 1024 * 1024

#: Every placement tier, since a publish job uploads the whole edition.
_ALL_TIERS = ("required", "lazy", "remote")


class UploadCheckError(RuntimeError):
    """An upload found something wrong, and registered nothing.

    Attributes:
        findings: Everything it found, each naming its file.
    """

    findings: tuple[Finding, ...]

    def __init__(self, findings: Iterable[Finding]) -> None:
        """Say what was found."""
        self.findings = tuple(findings)
        super().__init__("; ".join(str(finding) for finding in self.findings))


@frozen
class UploadReport:
    """What `upload_edition` did.

    Attributes:
        edition_id: The edition.
        product_id: Its product.
        sent: The store paths this run wrote, under the catalog root.
        kept: The store paths the store already held.
        registered: Whether this run wrote the ``pending/`` entry. It is
            advisory: a retried create can report false against its own earlier
            success.
    """

    edition_id: str
    product_id: str
    sent: tuple[str, ...]
    kept: tuple[str, ...]
    registered: bool


class _DigestReader(Protocol):
    """What an upload needs of `AzureBlobRest`; tests pass another."""

    def head(self, path: str) -> RemoteFile | None:
        """The size and kept digests of the object at *path*, or ``None`` when it is absent."""
        ...


def _condition_errors() -> tuple[type[AlreadyExistsError], type[PreconditionError]]:
    """`obstore`'s two lost-condition errors; a missing ``blob`` extra raises `ImportError` naming it."""
    _load_obstore()
    from obstore.exceptions import AlreadyExistsError, PreconditionError  # noqa: PLC0415

    return AlreadyExistsError, PreconditionError


def _store_path(manifest: Manifest, entry: EditionFile) -> str:
    """The path of *entry* under the catalog root: ``products/<product_id>/data/...`` or ``.../<edition_id>/...``."""
    product = f"products/{manifest.product_id}"
    if entry.root == "data":
        return posixpath.normpath(f"{product}/{manifest.edition_id}/{manifest.data_path}{entry.path}")
    return f"{product}/{manifest.edition_id}/{entry.path}"


def _manifest_path(manifest: Manifest) -> str:
    return f"products/{manifest.product_id}/{manifest.edition_id}/{MANIFEST_NAME}"


def _lookup(rest: _DigestReader, manifest: Manifest) -> Callable[[EditionFile], RemoteFile | None]:
    """Adapt *rest* to sdmxlib's ``Lookup``, asking the store once per file."""
    known: dict[str, RemoteFile | None] = {}

    def lookup(entry: EditionFile) -> RemoteFile | None:
        path = _store_path(manifest, entry)
        if path not in known:
            known[path] = rest.head(path)
        return known[path]

    return lookup


def _refusals(manifest: Manifest) -> list[Finding]:
    """What the manifest must not name: a path leaving its directory, or a file above `MAX_PUT_BYTES`.

    A path holding ``..`` would normalise to anywhere under the catalog root,
    ``inventory/`` included, so it is refused before anything is sent.
    """
    findings = [
        Finding("upload", MANIFEST_NAME, f"{name} {value!r} cannot name a directory")
        for name, value in (("product_id", manifest.product_id), ("edition_id", manifest.edition_id))
        if not _is_plain_name(value)
    ]
    for entry in manifest.files:
        if not _is_safe_path(entry.path):
            findings.append(Finding("upload", entry.path, "the manifest names a path outside its directory"))
        if entry.size > MAX_PUT_BYTES:
            detail = f"{entry.size} bytes, above the {MAX_PUT_BYTES} bytes one Put Blob holds"
            findings.append(Finding("upload", entry.path, detail))
    return findings


def _create(ops: _ObjectOps, path: str, data: bytes) -> bool:
    """Create the object with one conditional Put; false when the store already holds the name."""
    already_exists, precondition = _condition_errors()
    try:
        ops.put(path, data, mode="create")
    except already_exists:
        return False
    except precondition as exc:
        msg = f"create {path}: {type(exc).__name__}: {exc}"
        raise BlobStoreError(msg, path=path) from exc
    return True


def _local_file(edition_dir: Path, manifest: Manifest, entry: EditionFile) -> Path:
    return edition_dir / manifest.data_path / entry.path if entry.root == "data" else edition_dir / entry.path


def _send_listed(edition_dir: Path, manifest: Manifest, entry: EditionFile, ops: _ObjectOps) -> bool:
    """Create the listed file unless the store holds it, after hashing it against the manifest.

    One read serves the hash and the Put, so the bytes sent are the bytes
    checked, and a rotted local file stays out of the shared ``data/``.

    Args:
        edition_dir: The edition directory.
        manifest: Its manifest.
        entry: The file.
        ops: The route to the store.

    Returns:
        Whether this run wrote it.

    Raises:
        UploadCheckError: The local file is unreadable or differs from the manifest.
    """
    path = _store_path(manifest, entry)
    if ops.head(path) is not None:
        return False
    try:
        data = _local_file(edition_dir, manifest, entry).read_bytes()
    except OSError as exc:
        raise UploadCheckError([Finding("upload", entry.path, f"cannot be read here: {exc}")]) from exc
    if hashlib.sha256(data).hexdigest() != entry.digests["sha256"]:
        raise UploadCheckError([Finding("upload", entry.path, "the local file differs from the manifest's sha256")])
    return _create(ops, path, data)


def _send_manifest(raw: bytes, path: str, ops: _ObjectOps) -> bool:
    """Create ``manifest.json`` unless the store holds it; other bytes there raise, as an edition is never rewritten."""
    if ops.head(path) is None and _create(ops, path, raw):
        return True
    got = ops.read(path)
    if got is None or got[0] != raw:
        detail = "the store holds another manifest under this edition id; an edition is never rewritten"
        raise UploadCheckError([Finding("upload", path, detail)])
    return False


def _verify(edition_dir: Path, manifest: Manifest, raw: bytes, ops: _ObjectOps, rest: _DigestReader) -> None:
    """Check the edition and every uploaded file; any finding raises `UploadCheckError`."""
    lookup = _lookup(rest, manifest)
    report = check_edition(
        edition_dir,
        at="receive",
        lookup=lookup,
        references=bool(manifest.bindings),
        extensions={STATCAT_SECTION: _statcat_check()},
    )
    own = copy.replace(manifest, files=tuple(entry for entry in manifest.files if entry.root == "edition"))
    findings = [*report.findings, *verify_remote(own, lookup, tiers=_ALL_TIERS)]
    path = _manifest_path(manifest)
    got = ops.read(path)
    if got is None or hashlib.sha256(got[0]).digest() != hashlib.sha256(raw).digest():
        findings.append(Finding("remote", path, "the uploaded manifest differs from the local one"))
    if findings:
        raise UploadCheckError(findings)


def _pending_document(edition_id: str, release_at: datetime | None) -> bytes:
    """The ``pending/`` entry: compact UTF-8 JSON, the time in UTC to the millisecond; equal input gives equal bytes."""
    stamp = None
    if release_at is not None:
        stamp = release_at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return json.dumps({"edition_id": edition_id, "release_at": stamp}, separators=(",", ":")).encode()


def _register(path: str, document: bytes, ops: _ObjectOps) -> bool:
    """Create the ``pending/`` entry; false when it holds these bytes, and other bytes raise, naming both."""
    if _create(ops, path, document):
        return True
    got = ops.read(path)
    if got is None:
        msg = f"{path} was present and is now absent; run the upload again"
        raise BlobStoreError(msg, path=path)
    if got[0] != document:
        held, wanted = got[0].decode(errors="replace"), document.decode()
        detail = f"it holds {held}, and this upload asks for {wanted}; the fold would use the time it holds"
        raise UploadCheckError([Finding("pending", path, detail)])
    return False


def _upload(
    edition_dir: Path, location: BlobLocation, *, release_at: datetime | None = None, rest: _DigestReader
) -> UploadReport:
    """`upload_edition` with the digest client given, for any store: the steps in the module docstring.

    Args:
        edition_dir: The sealed edition's directory.
        location: The catalog.
        release_at: When the edition may be released; time-zone aware.
        rest: The digest client.

    Returns:
        What the run did.

    Raises:
        ValueError: *release_at* has no time zone.
        UploadCheckError: A finding, an oversize file, or a differing manifest
            or ``release_at``.
    """
    if release_at is not None and release_at.tzinfo is None:
        msg = f"release_at {release_at.isoformat()} has no time zone"
        raise ValueError(msg)
    manifest = read_manifest(edition_dir)
    if refused := _refusals(manifest):
        raise UploadCheckError(refused)
    edition_files(manifest)  # Raises `ManifestError` for a data_path that leads outside the product directory.
    raw = (edition_dir / MANIFEST_NAME).read_bytes()
    ops = location.ops
    sent: list[str] = []
    kept: list[str] = []

    def note(path: str, *, wrote: bool) -> None:
        (sent if wrote else kept).append(path)

    # Data files first, then the edition's own, as `edition_files` orders them.
    for entry in sorted(manifest.files, key=lambda listed: (listed.root != "data", listed.path)):
        note(_store_path(manifest, entry), wrote=_send_listed(edition_dir, manifest, entry, ops))
    note(_manifest_path(manifest), wrote=_send_manifest(raw, _manifest_path(manifest), ops))
    _verify(edition_dir, manifest, raw, ops, rest)
    pending = f"pending/{manifest.product_id}/{manifest.edition_id}.json"
    registered = _register(pending, _pending_document(manifest.edition_id, release_at), ops)
    return UploadReport(manifest.edition_id, manifest.product_id, tuple(sent), tuple(kept), registered)


def upload_edition(edition_dir: Path, location: BlobLocation, *, release_at: datetime | None = None) -> UploadReport:
    """Copy a sealed edition to its catalog in Azure Blob Storage, and register it in ``pending/``.

    A rerun sends only what is missing and leaves one registration. Each file is
    read into memory for its one Put, so peak memory is the largest file, up to
    `MAX_PUT_BYTES`.

    Args:
        edition_dir: The sealed edition's directory, with the shared ``data/``
            where its manifest's ``data_path`` names it.
        location: The catalog, on an `AzureStore` whose credential provider is
            a `TokenIdentity`.
        release_at: When the edition may be released; time-zone aware. ``None``
            schedules no release.

    Returns:
        What the run did.

    Raises:
        TypeError: The store is not an `AzureStore`. Upload is Azure-only in
            Phase 4 (W3), since `obstore` cannot set or read CRC32C.
        ValueError: The store has no `TokenIdentity` or no ``account_name``, or
            *release_at* has no time zone.
    """
    _load_obstore()
    from obstore.store import AzureStore  # noqa: PLC0415

    if not isinstance(location.store, AzureStore):
        msg = (
            f"upload supports AzureStore only in Phase 4 (W3), since obstore cannot set or read CRC32C; "
            f"this store is a {type(location.store).__name__}"
        )
        raise TypeError(msg)
    if _token_identity(location.store) is None:
        msg = "upload reads digests with a bearer token: give the AzureStore credential_provider=TokenIdentity(...)"
        raise ValueError(msg)
    with AzureBlobRest(location) as rest:
        return _upload(edition_dir, location, release_at=release_at, rest=rest)
